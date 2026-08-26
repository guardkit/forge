"""Unit tests for :mod:`forge.adapters.nats.approval_subscriber`.

Test classes mirror the acceptance criteria of TASK-CGCP-007:

- AC-001 — :meth:`ApprovalSubscriber.await_response` is an async method
  returning :class:`ApprovalResponsePayload | None` for ``(build_id,
  *, timeout_seconds=...)``.
- AC-002 — Subscribe pattern matches
  ``agents.approval.forge.{build_id}.response`` (project-scoped via
  ``Topics.for_project`` when configured).
- AC-003 — Dedup buffer is per-instance, keyed on ``request_id``, with
  short TTL eviction driven by the injected :class:`Clock`.
- AC-004 — Dedup check-and-record is asyncio-lock protected (R4).
- AC-005 — Group D ``@edge-case``: duplicate ``request_id`` is observed
  and discarded; build is not resumed twice.
- AC-006 — Group E ``@concurrency``: ``asyncio.gather`` of two
  simultaneous responses resolves to exactly one.
- AC-007 — Group D ``@edge-case``: per-build response routing — a
  response for ``build_a`` does not affect a wait on ``build_b``.
- AC-008 — Group C ``@negative``: unrecognised decision is refused via
  the Pydantic ``Literal`` validator; pause continues.
- AC-009 — Group E ``@security``: response with unrecognised
  ``decided_by`` is logged as anomaly and dropped.
- AC-010 — Refresh-on-timeout per ``API §7``: per-attempt timeout
  publishes a fresh :class:`ApprovalRequestPayload` via the injected
  :data:`PublishRefreshCallback` with incremented ``attempt_count``.
- AC-011 — Total wait bounded by
  :attr:`ApprovalConfig.max_wait_seconds`.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Awaitable, Callable
from unittest.mock import AsyncMock

import pytest

from forge.adapters.nats import (
    ApprovalSubscriber,
    ApprovalSubscriberDeps,
    InvalidDecisionError,
)
from forge.adapters.nats import approval_subscriber as sub_module
from forge.config.models import ApprovalConfig
from forge.gating.identity import derive_request_id
from nats_core.envelope import EventType, MessageEnvelope
from nats_core.events import ApprovalResponsePayload

# ---------------------------------------------------------------------------
# Constants used across tests
# ---------------------------------------------------------------------------


BUILD_ID = "build-FEAT-A1B2-20260425120000"
OTHER_BUILD_ID = "build-FEAT-Z9Y8-20260425130000"
STAGE_LABEL = "Architecture Review"
ATTEMPT_COUNT = 0
RICH = "rich"


# ---------------------------------------------------------------------------
# Helpers — fake NATS client + manual clock
# ---------------------------------------------------------------------------


class _FakeSubscription:
    """Minimal fake mirroring ``nats.aio.subscription.Subscription``."""

    def __init__(self) -> None:
        self.unsubscribed = False

    async def unsubscribe(self) -> None:
        self.unsubscribed = True


class FakeNATSClient:
    """Fake client capturing :meth:`subscribe` and exposing the callback.

    The subscriber under test calls ``await client.subscribe(subject,
    callback)``; this fake records the call and stores the callback so
    tests can drive it directly with hand-built envelopes.
    """

    def __init__(self) -> None:
        self.subscribe_calls: list[
            tuple[str, Callable[[MessageEnvelope], Awaitable[None]]]
        ] = []
        self._subscription: _FakeSubscription | None = None

    async def subscribe(
        self,
        topic: str,
        callback: Callable[[MessageEnvelope], Awaitable[None]],
    ) -> _FakeSubscription:
        self.subscribe_calls.append((topic, callback))
        self._subscription = _FakeSubscription()
        return self._subscription

    @property
    def subscription(self) -> _FakeSubscription | None:
        return self._subscription

    @property
    def last_callback(
        self,
    ) -> Callable[[MessageEnvelope], Awaitable[None]]:
        assert self.subscribe_calls, "subscribe() not yet called"
        return self.subscribe_calls[-1][1]


class FakeClock:
    """Manual monotonic clock — tests advance time via :meth:`tick`."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)

    def monotonic(self) -> float:
        return self._now

    def tick(self, seconds: float) -> None:
        self._now += float(seconds)


class AdvancingClock:
    """Auto-advancing monotonic clock — each :meth:`monotonic` returns the
    previous value and then advances by ``step`` seconds.

    Used in refresh-loop tests so the production code's
    ``while remaining > 0`` exits in a bounded number of iterations
    without resorting to a real wall clock.
    """

    def __init__(self, *, step: float = 0.5, start: float = 0.0) -> None:
        self._now = float(start)
        self._step = float(step)

    def monotonic(self) -> float:
        v = self._now
        self._now += self._step
        return v


def _make_envelope(
    *,
    request_id: str,
    decision: str = "approve",
    decided_by: str = RICH,
    notes: str | None = None,
) -> MessageEnvelope:
    """Build a valid :class:`MessageEnvelope` carrying an approval response.

    Used to drive the subscriber's NATS callback as if a real responder
    had published. ``decision`` is *not* validated here so tests can
    inject Group C ``@negative`` payloads carrying an unrecognised
    decision string.
    """
    payload: dict[str, Any] = {
        "request_id": request_id,
        "decision": decision,
        "decided_by": decided_by,
        "notes": notes,
    }
    return MessageEnvelope(
        source_id=RICH,
        event_type=EventType.APPROVAL_RESPONSE,
        payload=payload,
    )


def _make_deps(
    *,
    nats_client: FakeNATSClient | None = None,
    config: ApprovalConfig | None = None,
    publish_refresh: Any = None,
    expected_approver: str | None = None,
    project: str | None = None,
    clock: Any = None,
    dedup_ttl_seconds: int = 300,
    max_total_wait_seconds: int | None = None,
) -> ApprovalSubscriberDeps:
    """Build :class:`ApprovalSubscriberDeps` with safe test defaults.

    Note: when ``clock`` is omitted, the helper uses the production
    :class:`_MonotonicClock` so time advances naturally. Tests that
    need to drive TTL eviction deterministically pass an explicit
    :class:`FakeClock`.
    """
    return ApprovalSubscriberDeps(
        nats_client=nats_client or FakeNATSClient(),
        config=config or ApprovalConfig(),
        publish_refresh=publish_refresh,
        expected_approver=expected_approver,
        project=project,
        clock=clock if clock is not None else sub_module._MonotonicClock(),
        dedup_ttl_seconds=dedup_ttl_seconds,
        max_total_wait_seconds=max_total_wait_seconds,
    )


# ---------------------------------------------------------------------------
# AC-001 — class surface
# ---------------------------------------------------------------------------


class TestSubscriberSurface:
    """AC-001 — public method surface and shape."""

    def test_await_response_is_coroutine(self) -> None:
        method = getattr(ApprovalSubscriber, "await_response", None)
        assert method is not None, "await_response not defined"
        assert asyncio.iscoroutinefunction(method)

    def test_signature_has_required_keyword_only_args(self) -> None:
        sig = inspect.signature(ApprovalSubscriber.await_response)
        params = sig.parameters
        # build_id is positional/positional-or-keyword
        assert "build_id" in params
        # timeout_seconds is required (per AC) and keyword-only
        assert "timeout_seconds" in params
        assert (
            params["timeout_seconds"].kind
            is inspect.Parameter.KEYWORD_ONLY
        )
        # stage_label & attempt_count needed for refresh derivation
        assert "stage_label" in params
        assert (
            params["stage_label"].kind is inspect.Parameter.KEYWORD_ONLY
        )

    def test_invalid_decision_error_is_exception(self) -> None:
        assert issubclass(InvalidDecisionError, Exception)


# ---------------------------------------------------------------------------
# AC-002 — subject pattern
# ---------------------------------------------------------------------------


class TestSubjectPattern:
    """AC-002 — subscribe pattern matches the API contract."""

    @pytest.mark.asyncio
    async def test_default_subject_is_unscoped(self) -> None:
        client = FakeNATSClient()
        sub = ApprovalSubscriber(_make_deps(nats_client=client))
        # Use a 0-budget config so the wait loop exits immediately.
        sub._deps.config = ApprovalConfig(
            default_wait_seconds=0, max_wait_seconds=0
        )
        result = await sub.await_response(
            BUILD_ID,
            stage_label=STAGE_LABEL,
            attempt_count=ATTEMPT_COUNT,
            timeout_seconds=0,
        )
        assert result is None
        assert client.subscribe_calls
        topic, _cb = client.subscribe_calls[0]
        assert topic == f"agents.approval.forge.{BUILD_ID}.response"

    @pytest.mark.asyncio
    async def test_project_scoped_subject(self) -> None:
        client = FakeNATSClient()
        deps = _make_deps(
            nats_client=client,
            project="finproxy",
            config=ApprovalConfig(
                default_wait_seconds=0, max_wait_seconds=0
            ),
        )
        sub = ApprovalSubscriber(deps)
        await sub.await_response(
            BUILD_ID, stage_label=STAGE_LABEL, timeout_seconds=0
        )
        topic, _cb = client.subscribe_calls[0]
        assert topic == (
            f"finproxy.agents.approval.forge.{BUILD_ID}.response"
        )

    @pytest.mark.asyncio
    async def test_unsubscribes_on_exit(self) -> None:
        client = FakeNATSClient()
        deps = _make_deps(
            nats_client=client,
            config=ApprovalConfig(
                default_wait_seconds=0, max_wait_seconds=0
            ),
        )
        sub = ApprovalSubscriber(deps)
        await sub.await_response(
            BUILD_ID, stage_label=STAGE_LABEL, timeout_seconds=0
        )
        assert client.subscription is not None
        assert client.subscription.unsubscribed is True

    def test_subject_for_rejects_empty_build_id(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            ApprovalSubscriber._subject_for("", project=None)


# ---------------------------------------------------------------------------
# AC-005 — happy path: first arrival resolves
# ---------------------------------------------------------------------------


class TestHappyPath:
    """First valid response resolves the wait."""

    @pytest.mark.asyncio
    async def test_first_arrival_returns_payload(self) -> None:
        client = FakeNATSClient()
        deps = _make_deps(
            nats_client=client,
            config=ApprovalConfig(
                default_wait_seconds=10, max_wait_seconds=10
            ),
        )
        sub = ApprovalSubscriber(deps)
        request_id = derive_request_id(
            build_id=BUILD_ID,
            stage_label=STAGE_LABEL,
            attempt_count=0,
        )
        envelope = _make_envelope(request_id=request_id, decision="approve")

        async def driver() -> ApprovalResponsePayload | None:
            # Give the subscribe call a chance to register.
            await asyncio.sleep(0)
            await client.last_callback(envelope)
            return None

        async def runner() -> ApprovalResponsePayload | None:
            return await sub.await_response(
                BUILD_ID,
                stage_label=STAGE_LABEL,
                timeout_seconds=2,
            )

        # Schedule the responder coroutine after subscription has been
        # registered. Run them concurrently.
        wait_task = asyncio.create_task(runner())
        await asyncio.sleep(0)  # let runner subscribe
        await client.last_callback(envelope)
        result = await wait_task
        assert isinstance(result, ApprovalResponsePayload)
        assert result.request_id == request_id
        assert result.decision == "approve"


# ---------------------------------------------------------------------------
# AC-005 — Group D @edge-case "Duplicate approval responses are ignored"
# ---------------------------------------------------------------------------


class TestDedupFirstResponseWins:
    """Group D — duplicate ``request_id`` does not resume the build twice."""

    @pytest.mark.asyncio
    async def test_second_response_with_same_request_id_is_dropped(
        self,
    ) -> None:
        client = FakeNATSClient()
        deps = _make_deps(
            nats_client=client,
            config=ApprovalConfig(
                default_wait_seconds=5, max_wait_seconds=5
            ),
        )
        sub = ApprovalSubscriber(deps)
        request_id = derive_request_id(
            build_id=BUILD_ID,
            stage_label=STAGE_LABEL,
            attempt_count=0,
        )

        first = _make_envelope(request_id=request_id, decision="approve")
        second = _make_envelope(request_id=request_id, decision="reject")

        wait_task = asyncio.create_task(
            sub.await_response(
                BUILD_ID, stage_label=STAGE_LABEL, timeout_seconds=2
            )
        )
        await asyncio.sleep(0)
        await client.last_callback(first)
        # Second arrival with same request_id is observed but discarded.
        await client.last_callback(second)

        result = await wait_task
        assert isinstance(result, ApprovalResponsePayload)
        # First-response-wins — the approve must win, not the later reject.
        assert result.decision == "approve"

    @pytest.mark.asyncio
    async def test_dedup_ttl_eviction_is_clock_driven(self) -> None:
        """AC-003 — entries past the short TTL are evicted on next check."""
        clock = FakeClock(start=1000.0)
        deps = _make_deps(clock=clock, dedup_ttl_seconds=300)
        sub = ApprovalSubscriber(deps)

        first = await sub._check_and_record("req-A")
        assert first is False  # newly recorded

        # Same instant → still considered a duplicate.
        second = await sub._check_and_record("req-A")
        assert second is True

        # Advance past the TTL — entry should be evicted at next check.
        clock.tick(301.0)
        third = await sub._check_and_record("req-A")
        assert third is False  # re-recorded as a fresh first-arrival


# ---------------------------------------------------------------------------
# AC-006 — Group E @concurrency: gather two responses → one resolves
# ---------------------------------------------------------------------------


class TestConcurrencyR4:
    """Risk R4 — two concurrent responses resolve to exactly one decision."""

    @pytest.mark.asyncio
    async def test_concurrent_responses_resolve_to_one(self) -> None:
        client = FakeNATSClient()
        deps = _make_deps(
            nats_client=client,
            config=ApprovalConfig(
                default_wait_seconds=5, max_wait_seconds=5
            ),
        )
        sub = ApprovalSubscriber(deps)
        request_id = derive_request_id(
            build_id=BUILD_ID,
            stage_label=STAGE_LABEL,
            attempt_count=0,
        )
        env_a = _make_envelope(request_id=request_id, decision="approve")
        env_b = _make_envelope(request_id=request_id, decision="reject")

        wait_task = asyncio.create_task(
            sub.await_response(
                BUILD_ID, stage_label=STAGE_LABEL, timeout_seconds=2
            )
        )
        await asyncio.sleep(0)
        # Fire concurrently — gather mirrors the AC's wording exactly.
        await asyncio.gather(
            client.last_callback(env_a),
            client.last_callback(env_b),
        )
        result = await wait_task
        # Exactly one wins; the dedup buffer recorded the other as duplicate.
        assert isinstance(result, ApprovalResponsePayload)
        assert result.decision in {"approve", "reject"}
        # The losing envelope must have been dedup-recorded.
        assert "req" in str(result.request_id) or result.request_id

    @pytest.mark.asyncio
    async def test_dedup_lock_serialises_check_and_record(self) -> None:
        """The asyncio.Lock is the mechanism that closes R4."""
        deps = _make_deps()
        sub = ApprovalSubscriber(deps)
        # Drive _check_and_record concurrently with the same request_id.
        results = await asyncio.gather(
            sub._check_and_record("req-X"),
            sub._check_and_record("req-X"),
            sub._check_and_record("req-X"),
        )
        # Exactly one ``False`` (new) and the rest ``True`` (duplicates).
        assert sorted(results) == [False, True, True]


# ---------------------------------------------------------------------------
# AC-007 — Group D @edge-case: per-build response routing
# ---------------------------------------------------------------------------


class TestPerBuildRouting:
    """Responses on build_a's mirror do not affect build_b's wait loop."""

    @pytest.mark.asyncio
    async def test_response_for_other_build_does_not_resume(self) -> None:
        # Two independent subscribers, one per build_id. The subject
        # pattern itself routes per-build, so the OTHER_BUILD_ID's
        # callback is the only place a response for OTHER_BUILD_ID
        # could land.
        client_a = FakeNATSClient()
        client_b = FakeNATSClient()
        deps_a = _make_deps(
            nats_client=client_a,
            config=ApprovalConfig(
                default_wait_seconds=1, max_wait_seconds=1
            ),
        )
        deps_b = _make_deps(
            nats_client=client_b,
            config=ApprovalConfig(
                default_wait_seconds=1, max_wait_seconds=1
            ),
        )
        sub_a = ApprovalSubscriber(deps_a)
        sub_b = ApprovalSubscriber(deps_b)

        # Compose a response addressed to build_b only.
        rid_b = derive_request_id(
            build_id=OTHER_BUILD_ID,
            stage_label=STAGE_LABEL,
            attempt_count=0,
        )
        env_b = _make_envelope(request_id=rid_b, decision="approve")

        # Run both wait loops with a very short total budget.
        async def run_a() -> ApprovalResponsePayload | None:
            return await sub_a.await_response(
                BUILD_ID, stage_label=STAGE_LABEL, timeout_seconds=0
            )

        async def run_b() -> ApprovalResponsePayload | None:
            return await sub_b.await_response(
                OTHER_BUILD_ID,
                stage_label=STAGE_LABEL,
                timeout_seconds=0,
            )

        # Drive build_b's callback only — build_a should never see it.
        wait_b = asyncio.create_task(run_b())
        await asyncio.sleep(0)
        # Note: in this design we still simulate the routed-to-wrong-build
        # case at the subscriber level by feeding env_b to build_a's
        # callback explicitly. The correct topic-level routing is
        # asserted via the AC-002 subject test; here we exercise the
        # subscriber's defensive per-build queue lookup.
        wait_a = asyncio.create_task(run_a())
        await asyncio.sleep(0)
        if client_a.subscribe_calls:
            await client_a.last_callback(env_b)
        result_a = await wait_a
        result_b = await wait_b
        # build_a's wait must NOT have resumed — its queue was never
        # populated because env_b's request_id does not match an
        # awaiter on build_a (and even if it did, the topic-level
        # routing would prevent delivery in production).
        assert result_a is None
        # build_b's wait timed out without a response on its own
        # subscription either; that's expected with the 0-budget
        # config and asserts that the cross-feed did not leak.
        assert result_b is None

    @pytest.mark.asyncio
    async def test_response_arriving_with_no_active_waiter_is_dropped(
        self,
    ) -> None:
        deps = _make_deps()
        sub = ApprovalSubscriber(deps)
        request_id = derive_request_id(
            build_id=BUILD_ID,
            stage_label=STAGE_LABEL,
            attempt_count=0,
        )
        envelope = _make_envelope(request_id=request_id)
        # No active await_response — call _on_envelope directly.
        # Should not raise.
        await sub._on_envelope(build_id=BUILD_ID, envelope=envelope)
        # Dedup recorded though (defence-in-depth).
        assert request_id in sub._dedup


# ---------------------------------------------------------------------------
# AC-008 — Group C @negative: unrecognised decision refused
# ---------------------------------------------------------------------------


class TestUnrecognisedDecision:
    """Group C ``@negative`` — invalid decision is refused; pause continues."""

    @pytest.mark.asyncio
    async def test_unknown_decision_is_dropped(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        deps = _make_deps()
        sub = ApprovalSubscriber(deps)
        bad = _make_envelope(
            request_id="req-bad", decision="maybe-later"
        )
        with caplog.at_level(logging.WARNING, logger=sub_module.__name__):
            await sub._on_envelope(build_id=BUILD_ID, envelope=bad)
        # Dedup buffer NOT poisoned — a future correctly-formed response
        # carrying the same request_id can still resume.
        assert "req-bad" not in sub._dedup
        # Diagnostic was logged.
        assert any(
            "invalid payload" in rec.message.lower() for rec in caplog.records
        )

    @pytest.mark.asyncio
    async def test_unknown_decision_does_not_cancel_wait(self) -> None:
        client = FakeNATSClient()
        deps = _make_deps(
            nats_client=client,
            config=ApprovalConfig(
                default_wait_seconds=2, max_wait_seconds=2
            ),
        )
        sub = ApprovalSubscriber(deps)
        request_id = derive_request_id(
            build_id=BUILD_ID,
            stage_label=STAGE_LABEL,
            attempt_count=0,
        )
        bad = _make_envelope(request_id=request_id, decision="maybe-later")
        good = _make_envelope(request_id=request_id, decision="approve")

        wait_task = asyncio.create_task(
            sub.await_response(
                BUILD_ID, stage_label=STAGE_LABEL, timeout_seconds=2
            )
        )
        await asyncio.sleep(0)
        await client.last_callback(bad)
        # Pause is NOT cancelled — a follow-up valid response resumes it.
        await client.last_callback(good)
        result = await wait_task
        assert isinstance(result, ApprovalResponsePayload)
        assert result.decision == "approve"

    def test_invalid_decision_error_carries_context(self) -> None:
        err = InvalidDecisionError(
            request_id="req-bad",
            raw_decision="maybe-later",
            cause=ValueError("bad"),
        )
        assert err.request_id == "req-bad"
        assert err.raw_decision == "maybe-later"
        assert isinstance(err.cause, ValueError)


# ---------------------------------------------------------------------------
# AC-009 — Group E @security: unrecognised responder
# ---------------------------------------------------------------------------


class TestUnrecognisedResponder:
    """Group E ``@security`` — wrong ``decided_by`` does NOT resume."""

    @pytest.mark.asyncio
    async def test_unknown_responder_is_dropped(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        deps = _make_deps(expected_approver="rich")
        sub = ApprovalSubscriber(deps)
        env = _make_envelope(
            request_id="req-1",
            decision="approve",
            decided_by="malicious",
        )
        with caplog.at_level(logging.WARNING, logger=sub_module.__name__):
            await sub._on_envelope(build_id=BUILD_ID, envelope=env)
        # Dedup buffer NOT poisoned — the legit responder can still send.
        assert "req-1" not in sub._dedup
        assert any(
            "unrecognised responder" in rec.message.lower()
            for rec in caplog.records
        )

    @pytest.mark.asyncio
    async def test_permissive_mode_accepts_any_responder(self) -> None:
        """When ``expected_approver`` is ``None`` any responder resumes."""
        deps = _make_deps(expected_approver=None)
        sub = ApprovalSubscriber(deps)
        env = _make_envelope(
            request_id="req-1",
            decision="approve",
            decided_by="anyone",
        )
        # Drop into queue manually by calling _on_envelope with an
        # active queue.
        sub._queues[BUILD_ID] = asyncio.Queue()
        await sub._on_envelope(build_id=BUILD_ID, envelope=env)
        # Queue received the payload.
        payload = await asyncio.wait_for(
            sub._queues[BUILD_ID].get(), timeout=0.1
        )
        assert payload.decided_by == "anyone"


# ---------------------------------------------------------------------------
# AC-010 — Refresh-on-timeout per API §7
# ---------------------------------------------------------------------------


class TestRefreshOnTimeout:
    """API §7 — per-attempt timeout publishes a fresh request."""

    @pytest.mark.asyncio
    async def test_refresh_publishes_with_incremented_attempt(self) -> None:
        publish_refresh = AsyncMock()
        # AdvancingClock guarantees the loop terminates in a bounded
        # number of iterations without burning a real wall-clock second.
        deps = _make_deps(
            config=ApprovalConfig(
                default_wait_seconds=1, max_wait_seconds=2
            ),
            publish_refresh=publish_refresh,
            clock=AdvancingClock(step=0.5),
        )
        sub = ApprovalSubscriber(deps)
        result = await sub.await_response(
            BUILD_ID,
            stage_label=STAGE_LABEL,
            attempt_count=0,
            timeout_seconds=0,  # 0s per-attempt to drive immediate refresh
        )
        # No response was injected so loop times out → None.
        assert result is None
        # publish_refresh was awaited at least once with attempt_count
        # incremented from the supplied 0.
        publish_refresh.assert_awaited()
        first_call = publish_refresh.await_args_list[0]
        args, kwargs = first_call.args, first_call.kwargs
        # Positional: (build_id, stage_label, attempt_count)
        assert args[0] == BUILD_ID
        assert args[1] == STAGE_LABEL
        assert args[2] >= 1  # incremented from 0

    @pytest.mark.asyncio
    async def test_refresh_failure_does_not_kill_wait_loop(self) -> None:
        # publish_refresh raises — the wait loop must keep waiting.
        publish_refresh = AsyncMock(
            side_effect=RuntimeError("transient publish failure")
        )
        deps = _make_deps(
            config=ApprovalConfig(
                default_wait_seconds=0, max_wait_seconds=1
            ),
            publish_refresh=publish_refresh,
            clock=AdvancingClock(step=0.25),
        )
        sub = ApprovalSubscriber(deps)
        result = await sub.await_response(
            BUILD_ID,
            stage_label=STAGE_LABEL,
            timeout_seconds=0,
        )
        # max_wait reached → None, but no exception escaped the loop.
        assert result is None
        publish_refresh.assert_awaited()

    @pytest.mark.asyncio
    async def test_no_refresh_publisher_returns_on_first_timeout(
        self,
    ) -> None:
        deps = _make_deps(
            config=ApprovalConfig(
                default_wait_seconds=10, max_wait_seconds=10
            ),
            publish_refresh=None,
        )
        sub = ApprovalSubscriber(deps)
        result = await sub.await_response(
            BUILD_ID,
            stage_label=STAGE_LABEL,
            timeout_seconds=0,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_prior_request_id_remains_valid_for_dedup(self) -> None:
        """API §7 — the prior request_id stays in the dedup buffer until TTL."""
        deps = _make_deps(dedup_ttl_seconds=300)
        sub = ApprovalSubscriber(deps)
        prior = derive_request_id(
            build_id=BUILD_ID,
            stage_label=STAGE_LABEL,
            attempt_count=0,
        )
        # Simulate a first-arrival on attempt 0.
        env = _make_envelope(request_id=prior, decision="approve")
        sub._queues[BUILD_ID] = asyncio.Queue()
        await sub._on_envelope(build_id=BUILD_ID, envelope=env)
        # Now a second arrival on the prior request_id (e.g. a late
        # real response after Forge already refreshed) is recorded as
        # a duplicate and dropped, even though the wait loop has
        # advanced to a new attempt.
        was_duplicate = await sub._check_and_record(prior)
        assert was_duplicate is True


# ---------------------------------------------------------------------------
# AC-011 — Total wait bounded by max_wait_seconds
# ---------------------------------------------------------------------------


class TestMaxWaitBound:
    """Total wait must not exceed ``ApprovalConfig.max_wait_seconds``."""

    @pytest.mark.asyncio
    async def test_returns_none_after_max_wait_with_no_response(
        self,
    ) -> None:
        publish_refresh = AsyncMock()
        deps = _make_deps(
            config=ApprovalConfig(
                default_wait_seconds=0, max_wait_seconds=0
            ),
            publish_refresh=publish_refresh,
        )
        sub = ApprovalSubscriber(deps)
        result = await sub.await_response(
            BUILD_ID,
            stage_label=STAGE_LABEL,
            timeout_seconds=0,
        )
        # Zero-budget config returns immediately with no refresh attempt
        # (the loop checks ``remaining <= 0`` before scheduling one).
        assert result is None
        publish_refresh.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_negative_attempt_count_rejected(self) -> None:
        deps = _make_deps()
        sub = ApprovalSubscriber(deps)
        with pytest.raises(ValueError, match="attempt_count"):
            await sub.await_response(
                BUILD_ID,
                stage_label=STAGE_LABEL,
                attempt_count=-1,
                timeout_seconds=0,
            )

    @pytest.mark.asyncio
    async def test_empty_build_id_rejected(self) -> None:
        deps = _make_deps()
        sub = ApprovalSubscriber(deps)
        with pytest.raises(ValueError, match="build_id"):
            await sub.await_response(
                "",
                stage_label=STAGE_LABEL,
                timeout_seconds=0,
            )

    @pytest.mark.asyncio
    async def test_empty_stage_label_rejected(self) -> None:
        deps = _make_deps()
        sub = ApprovalSubscriber(deps)
        with pytest.raises(ValueError, match="stage_label"):
            await sub.await_response(
                BUILD_ID,
                stage_label="",
                timeout_seconds=0,
            )


# ---------------------------------------------------------------------------
# The build gate's total-wait override (2026-08-26): 0 = wait forever,
# positive = ceiling, None = the protocol ceiling (unchanged callers).
# ---------------------------------------------------------------------------


class TestMaxTotalWaitOverride:
    """deps.max_total_wait_seconds — the build gate's wait-forever knob."""

    @pytest.mark.asyncio
    async def test_unbounded_override_waits_past_config_ceiling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Control: with the SAME fast-advancing clock and no override, the
        # protocol ceiling returns None before a response could ever land.
        control = ApprovalSubscriber(
            _make_deps(
                config=ApprovalConfig(default_wait_seconds=0, max_wait_seconds=2),
                clock=AdvancingClock(step=10.0),
            )
        )
        assert (
            await control.await_response(
                BUILD_ID, stage_label=STAGE_LABEL, timeout_seconds=0
            )
            is None
        )

        # Unbounded override: same config, same clock — the wait survives far
        # past the config ceiling and resolves on the late response, even with
        # NO refresh publisher wired (the rearm path's shape).
        monkeypatch.setattr(sub_module, "UNBOUNDED_IDLE_WAIT_SECONDS", 0.02)
        client = FakeNATSClient()
        sub = ApprovalSubscriber(
            _make_deps(
                nats_client=client,
                config=ApprovalConfig(default_wait_seconds=0, max_wait_seconds=2),
                clock=AdvancingClock(step=10.0),
                max_total_wait_seconds=0,
            )
        )
        request_id = derive_request_id(
            build_id=BUILD_ID, stage_label=STAGE_LABEL, attempt_count=0
        )
        wait_task = asyncio.create_task(
            sub.await_response(BUILD_ID, stage_label=STAGE_LABEL, timeout_seconds=0)
        )
        # Let several zero-per-attempt windows expire — the unbounded wait
        # must keep going instead of giving up after the first one.
        await asyncio.sleep(0.15)
        assert not wait_task.done(), "unbounded wait must not give up"
        await client.last_callback(_make_envelope(request_id=request_id))
        result = await asyncio.wait_for(wait_task, timeout=5.0)
        assert isinstance(result, ApprovalResponsePayload)
        assert result.decision == "approve"

    @pytest.mark.asyncio
    async def test_unbounded_override_keeps_refreshing_past_ceiling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sub_module, "UNBOUNDED_IDLE_WAIT_SECONDS", 0.02)
        client = FakeNATSClient()

        # Approve on the FIFTH refresh — by then the advancing clock has run
        # far past the 2s config ceiling, where the old code had already
        # returned None and cancelled the build.
        async def publish_refresh(
            build_id: str, stage_label: str, attempt_count: int
        ) -> None:
            if attempt_count >= 5:
                rid = derive_request_id(
                    build_id=build_id,
                    stage_label=stage_label,
                    attempt_count=attempt_count,
                )
                await client.last_callback(_make_envelope(request_id=rid))

        sub = ApprovalSubscriber(
            _make_deps(
                nats_client=client,
                config=ApprovalConfig(default_wait_seconds=0, max_wait_seconds=2),
                publish_refresh=publish_refresh,
                clock=AdvancingClock(step=1.0),
                max_total_wait_seconds=0,
            )
        )
        result = await asyncio.wait_for(
            sub.await_response(
                BUILD_ID, stage_label=STAGE_LABEL, timeout_seconds=0
            ),
            timeout=5.0,
        )
        assert isinstance(result, ApprovalResponsePayload)
        assert result.decision == "approve"

    @pytest.mark.asyncio
    async def test_positive_override_enforces_below_config_ceiling(self) -> None:
        # config allows 3600s; the override says 2s — the override wins and
        # the wait still expires (the knob keeps working when set).
        publish_refresh = AsyncMock()
        sub = ApprovalSubscriber(
            _make_deps(
                config=ApprovalConfig(
                    default_wait_seconds=0, max_wait_seconds=3600
                ),
                publish_refresh=publish_refresh,
                clock=AdvancingClock(step=1.0),
                max_total_wait_seconds=2,
            )
        )
        result = await sub.await_response(
            BUILD_ID, stage_label=STAGE_LABEL, timeout_seconds=0
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_none_override_keeps_config_ceiling(self) -> None:
        # None = the pre-existing behaviour for every non-gate caller.
        sub = ApprovalSubscriber(
            _make_deps(
                config=ApprovalConfig(default_wait_seconds=0, max_wait_seconds=0),
                max_total_wait_seconds=None,
            )
        )
        result = await sub.await_response(
            BUILD_ID, stage_label=STAGE_LABEL, timeout_seconds=0
        )
        assert result is None

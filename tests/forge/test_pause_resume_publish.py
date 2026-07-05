"""Pause/resume publish round-trip tests for TASK-FW10-010.

AMENDED BY TASK-FRR-PEB-006 — pause/resume canonicalisation.
=============================================================

The lifecycle bridge (FEAT-PEBR / FRR-PEB) becomes the **canonical**
``pipeline.build-resumed`` emit site when wired. FW10-010's resume
publish (the ``emit_resumed`` block in
:meth:`forge.adapters.nats.approval_subscriber.ApprovalSubscriber._on_envelope`)
is now amended to **skip its own emit** when
:attr:`ApprovalSubscriberDeps.bridge_registry_lookup` reports an active
bridge entry for ``(feature_id, correlation_id)``. When no bridge is
wired (the lookup is ``None`` or returns ``False``), FW10-010's emit
continues to fire — preserving backward compatibility for tests and
deploys that do not run the bridge.

Per TASK-FRR-PEB-006 AC-5, no FW10-010 test has been deleted. Two new
test classes have been added below:

* :class:`TestBridgeWiredSkipsResumeEmit` — bridge active → subscriber
  skips emit, queue still wakes the orchestrator (AC-2 + AC-4).
* :class:`TestBridgeAbsentEmitsResume` — no bridge wired → subscriber
  emits exactly one ``build-resumed`` (AC-3 — backward compatibility).

Validates DDR-007 §Decision: pause and resume publish call sites are
co-located with the lifecycle transition that triggers them.

Acceptance criteria mapped to test classes:

* AC-001 (pause publish on awaiting_approval) →
  :class:`TestPausePublishAwaitingApproval`.
* AC-002 (resume publish on matched approval) →
  :class:`TestResumePublishOnApproval`.
* AC-003 (mismatched correlation_id is rejected) →
  :class:`TestMismatchedCorrelationIdRejected`.
* AC-005 (idempotent first-wins on resume) →
  :class:`TestFirstResponseWins`.
* AC-006 (publish failures log WARNING, do not regress SQLite) →
  :class:`TestPublishFailureContract`.
* TASK-FRR-PEB-006 AC-2/AC-3 (bridge canonicalisation) →
  :class:`TestBridgeWiredSkipsResumeEmit` and
  :class:`TestBridgeAbsentEmitsResume`.

The tests construct a real :class:`PipelineLifecycleEmitter` with a fake
:class:`PipelinePublisher` so the assertion target is the actual
``pipeline.build-paused.<feature_id>`` /
``pipeline.build-resumed.<feature_id>`` envelope shape, not a paraphrase
through a mock.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import pytest

from forge.adapters.nats import PipelinePublisher
from forge.adapters.nats.approval_subscriber import (
    ApprovalSubscriber,
    ApprovalSubscriberDeps,
)
from forge.config.models import ApprovalConfig, PipelineConfig
from forge.pipeline import BuildContext, PipelineLifecycleEmitter
from forge.subagents.autobuild_runner import (
    LIFECYCLE_TO_PIPELINE_EMIT,
    AutobuildState,
    LifecycleEmitterAdapter,
    _update_state,
)
from nats_core.envelope import EventType, MessageEnvelope
from nats_core.events import (
    ApprovalResponsePayload,
    BuildPausedPayload,
    BuildResumedPayload,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class FakeNatsClient:
    """Minimal NATS client capturing every published subject + body."""

    published: list[tuple[str, bytes]] = field(default_factory=list)
    raise_on_publish: Exception | None = None

    async def publish(self, subject: str, body: bytes) -> None:
        if self.raise_on_publish is not None:
            raise self.raise_on_publish
        self.published.append((subject, body))


def _make_state(**overrides: Any) -> AutobuildState:
    base: dict[str, Any] = {
        "task_id": "task-001",
        "build_id": "build-FEAT-X-20260502120000",
        "feature_id": "FEAT-X",
        "lifecycle": "starting",
        "correlation_id": "corr-001",
    }
    base.update(overrides)
    return AutobuildState(**base)


def _make_ctx() -> BuildContext:
    return BuildContext(
        feature_id="FEAT-X",
        build_id="build-FEAT-X-20260502120000",
        correlation_id="corr-001",
        wave_total=3,
    )


def _build_emitter() -> tuple[PipelineLifecycleEmitter, FakeNatsClient]:
    nc = FakeNatsClient()
    publisher = PipelinePublisher(nc)
    config = PipelineConfig(progress_interval_seconds=60)
    emitter = PipelineLifecycleEmitter(publisher, config)
    return emitter, nc


def _decode_envelope(body: bytes) -> MessageEnvelope:
    return MessageEnvelope.model_validate_json(body.decode("utf-8"))


# ---------------------------------------------------------------------------
# AC-001: _update_state(awaiting_approval) → publish_build_paused
# ---------------------------------------------------------------------------


class TestPausePublishAwaitingApproval:
    """``awaiting_approval`` lifecycle publishes ``pipeline.build-paused``."""

    def test_lifecycle_routing_table_maps_awaiting_approval_to_emit_paused(
        self,
    ) -> None:
        """The DDR-007 routing table dispatches ``awaiting_approval``."""
        assert LIFECYCLE_TO_PIPELINE_EMIT["awaiting_approval"] == "emit_paused", (
            "TASK-FW10-010: awaiting_approval lifecycle MUST route to "
            "emit_paused so pipeline.build-paused.<feature_id> is published "
            "at the same boundary as the async_tasks channel write."
        )

    def test_update_state_awaiting_approval_publishes_build_paused(
        self,
    ) -> None:
        """A real adapter + emitter publishes the canonical envelope."""
        emitter, nc = _build_emitter()
        ctx = _make_ctx()
        adapter = LifecycleEmitterAdapter(emitter, ctx)
        state = _make_state(lifecycle="running_wave")

        _update_state(
            state,
            lifecycle="awaiting_approval",
            emitter=adapter,
            waiting_for="approval:Architecture Review",
        )

        # Exactly one envelope on the canonical subject.
        assert (
            len(nc.published) == 1
        ), f"Expected one publish, got {[s for s, _ in nc.published]}"
        subject, body = nc.published[0]
        assert subject == "pipeline.build-paused.FEAT-X"

        envelope = _decode_envelope(body)
        assert envelope.event_type == EventType.BUILD_PAUSED
        assert envelope.correlation_id == "corr-001", (
            "TASK-FW10-010: published envelope must carry the build's "
            "correlation_id (FEAT-FORGE-002)."
        )

        payload = BuildPausedPayload.model_validate(envelope.payload)
        assert payload.feature_id == "FEAT-X"
        assert payload.build_id == "build-FEAT-X-20260502120000"
        assert payload.correlation_id == "corr-001"
        assert payload.stage_label == "approval:Architecture Review"

    def test_other_lifecycles_do_not_publish_paused(self) -> None:
        """Non-pausing lifecycle transitions don't fire a paused envelope."""
        emitter, nc = _build_emitter()
        ctx = _make_ctx()
        adapter = LifecycleEmitterAdapter(emitter, ctx)
        state = _make_state(lifecycle="starting")

        _update_state(
            state,
            lifecycle="planning_waves",
            emitter=adapter,
        )

        paused_subjects = [s for s, _ in nc.published if "build-paused" in s]
        assert paused_subjects == []


# ---------------------------------------------------------------------------
# AC-002: matching approval response publishes pipeline.build-resumed
# ---------------------------------------------------------------------------


class TestResumePublishOnApproval:
    """A matching approval response publishes ``pipeline.build-resumed``."""

    @pytest.mark.asyncio
    async def test_first_arrival_publishes_build_resumed(self) -> None:
        """First-arrival valid response → build-resumed envelope on the wire."""
        emitter, nc = _build_emitter()
        ctx = _make_ctx()

        deps = ApprovalSubscriberDeps(
            nats_client=_FakeSubscribeClient(),
            config=ApprovalConfig(default_wait_seconds=60, max_wait_seconds=60),
        )
        sub = ApprovalSubscriber(deps)

        # Register the resume-publish context as await_response would.
        sub._resume_publish_ctx[ctx.build_id] = (  # type: ignore[attr-defined]
            emitter,
            ctx,
            "corr-001",  # expected correlation
            "implementation",
        )
        sub._queues[ctx.build_id] = asyncio.Queue()  # type: ignore[attr-defined]

        envelope = _approval_response_envelope(
            request_id="req-1",
            decision="approve",
            decided_by="rich",
            correlation_id="corr-001",
        )

        await sub._on_envelope(  # type: ignore[attr-defined]
            build_id=ctx.build_id, envelope=envelope
        )

        resumed = [s for s, _ in nc.published if "build-resumed" in s]
        assert resumed == [
            "pipeline.build-resumed.FEAT-X"
        ], f"Expected build-resumed publish, got {nc.published}"

        body = next(b for s, b in nc.published if "build-resumed" in s)
        env = _decode_envelope(body)
        assert env.event_type == EventType.BUILD_RESUMED
        assert env.correlation_id == "corr-001"

        payload = BuildResumedPayload.model_validate(env.payload)
        assert payload.feature_id == "FEAT-X"
        assert payload.decision == "approve"
        assert payload.responder == "rich"

    @pytest.mark.asyncio
    async def test_resume_published_before_orchestrator_advances(self) -> None:
        """build-resumed must be published BEFORE the queue is woken."""
        emitter, nc = _build_emitter()
        ctx = _make_ctx()

        # Track ordering: record when publish lands and when queue pops.
        order: list[str] = []

        original_publish = nc.publish

        async def tracking_publish(subject: str, body: bytes) -> None:
            order.append(f"publish:{subject}")
            await original_publish(subject, body)

        nc.publish = tracking_publish  # type: ignore[assignment]

        deps = ApprovalSubscriberDeps(
            nats_client=_FakeSubscribeClient(),
            config=ApprovalConfig(default_wait_seconds=60, max_wait_seconds=60),
        )
        sub = ApprovalSubscriber(deps)
        sub._resume_publish_ctx[ctx.build_id] = (  # type: ignore[attr-defined]
            emitter,
            ctx,
            None,
            "implementation",
        )
        queue: asyncio.Queue = asyncio.Queue()
        sub._queues[ctx.build_id] = queue  # type: ignore[attr-defined]

        envelope = _approval_response_envelope(
            request_id="req-1",
            decision="approve",
            decided_by="rich",
            correlation_id="corr-001",
        )
        await sub._on_envelope(  # type: ignore[attr-defined]
            build_id=ctx.build_id, envelope=envelope
        )

        # Publish recorded; now consume the queue.
        await queue.get()
        order.append("queue-popped")

        publish_idx = next(i for i, e in enumerate(order) if e.startswith("publish:"))
        pop_idx = order.index("queue-popped")
        assert publish_idx < pop_idx, (
            "TASK-FW10-010: build-resumed must be published BEFORE the "
            "orchestrator dequeues the response."
        )


# ---------------------------------------------------------------------------
# AC-003: mismatched correlation_id is rejected (Group E security)
# ---------------------------------------------------------------------------


class TestMismatchedCorrelationIdRejected:
    """A response with a mismatched envelope correlation_id is dropped."""

    @pytest.mark.asyncio
    async def test_mismatched_correlation_does_not_publish_resumed(
        self,
    ) -> None:
        """Mismatched correlation_id → no build-resumed publish."""
        emitter, nc = _build_emitter()
        ctx = _make_ctx()

        deps = ApprovalSubscriberDeps(
            nats_client=_FakeSubscribeClient(),
            config=ApprovalConfig(default_wait_seconds=60, max_wait_seconds=60),
        )
        sub = ApprovalSubscriber(deps)
        sub._resume_publish_ctx[ctx.build_id] = (  # type: ignore[attr-defined]
            emitter,
            ctx,
            "corr-001",  # expected
            "implementation",
        )
        sub._queues[ctx.build_id] = asyncio.Queue()  # type: ignore[attr-defined]

        envelope = _approval_response_envelope(
            request_id="req-1",
            decision="approve",
            decided_by="rich",
            correlation_id="corr-ATTACK",  # mismatch
        )

        await sub._on_envelope(  # type: ignore[attr-defined]
            build_id=ctx.build_id, envelope=envelope
        )

        # No publish on the wire AND queue stays empty — build is paused.
        assert nc.published == []
        assert sub._queues[ctx.build_id].empty(), (  # type: ignore[attr-defined]
            "TASK-FW10-010 / DDR-001: a mismatched correlation_id MUST "
            "NOT cause the build to resume."
        )


# ---------------------------------------------------------------------------
# AC-005: first-response-wins on resume (idempotent)
# ---------------------------------------------------------------------------


class TestFirstResponseWins:
    """Only the first matching response wins; the second is a no-op."""

    @pytest.mark.asyncio
    async def test_second_matching_response_is_dedup_dropped(self) -> None:
        """Duplicate request_id is dropped; build-resumed only fires once."""
        emitter, nc = _build_emitter()
        ctx = _make_ctx()

        deps = ApprovalSubscriberDeps(
            nats_client=_FakeSubscribeClient(),
            config=ApprovalConfig(default_wait_seconds=60, max_wait_seconds=60),
        )
        sub = ApprovalSubscriber(deps)
        sub._resume_publish_ctx[ctx.build_id] = (  # type: ignore[attr-defined]
            emitter,
            ctx,
            None,
            "implementation",
        )
        sub._queues[ctx.build_id] = asyncio.Queue()  # type: ignore[attr-defined]

        envelope = _approval_response_envelope(
            request_id="req-1",
            decision="approve",
            decided_by="rich",
            correlation_id="corr-001",
        )

        # First arrival.
        await sub._on_envelope(  # type: ignore[attr-defined]
            build_id=ctx.build_id, envelope=envelope
        )
        # Second arrival with the SAME request_id — must be a no-op.
        await sub._on_envelope(  # type: ignore[attr-defined]
            build_id=ctx.build_id, envelope=envelope
        )

        resumed_publishes = [s for s, _ in nc.published if "build-resumed" in s]
        assert len(resumed_publishes) == 1, (
            f"FEAT-FORGE-004 first-wins: expected ONE build-resumed "
            f"publish, got {len(resumed_publishes)} ({resumed_publishes})"
        )


# ---------------------------------------------------------------------------
# AC-006: publish failures log WARNING and do not regress SQLite
# ---------------------------------------------------------------------------


class TestPublishFailureContract:
    """Publish failures on emit_paused/emit_resumed do not crash the build."""

    def test_publish_failure_on_pause_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A NATS down emitter on pause logs WARNING; state continues."""
        nc = FakeNatsClient(raise_on_publish=RuntimeError("nats down"))
        publisher = PipelinePublisher(nc)
        emitter = PipelineLifecycleEmitter(
            publisher, PipelineConfig(progress_interval_seconds=60)
        )
        ctx = _make_ctx()
        adapter = LifecycleEmitterAdapter(emitter, ctx)
        state = _make_state(lifecycle="running_wave")

        with caplog.at_level(logging.WARNING):
            new_state = _update_state(
                state,
                lifecycle="awaiting_approval",
                emitter=adapter,
            )

        # Build is NOT regressed — channel write committed regardless.
        assert new_state.lifecycle == "awaiting_approval"
        # Publisher tried to publish and failed; nothing landed on wire.
        assert nc.published == []
        # WARNING was logged so operators have a trail. Either the
        # emitter's _safe_publish or the adapter's _safe_run could log.
        assert any(
            record.levelname in ("WARNING", "ERROR") for record in caplog.records
        ), "DDR-007 §Failure-mode contract: must log at WARNING/ERROR"

    @pytest.mark.asyncio
    async def test_publish_failure_on_resume_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A NATS down emitter on resume logs WARNING; queue still receives."""
        nc = FakeNatsClient(raise_on_publish=RuntimeError("nats down"))
        publisher = PipelinePublisher(nc)
        emitter = PipelineLifecycleEmitter(
            publisher, PipelineConfig(progress_interval_seconds=60)
        )
        ctx = _make_ctx()

        deps = ApprovalSubscriberDeps(
            nats_client=_FakeSubscribeClient(),
            config=ApprovalConfig(default_wait_seconds=60, max_wait_seconds=60),
        )
        sub = ApprovalSubscriber(deps)
        sub._resume_publish_ctx[ctx.build_id] = (  # type: ignore[attr-defined]
            emitter,
            ctx,
            None,
            "implementation",
        )
        queue: asyncio.Queue = asyncio.Queue()
        sub._queues[ctx.build_id] = queue  # type: ignore[attr-defined]

        envelope = _approval_response_envelope(
            request_id="req-1",
            decision="approve",
            decided_by="rich",
            correlation_id="corr-001",
        )

        with caplog.at_level(logging.WARNING):
            await sub._on_envelope(  # type: ignore[attr-defined]
                build_id=ctx.build_id, envelope=envelope
            )

        # Queue still got the response — caller can resume even if the
        # publish failed (SQLite remains authoritative).
        assert not queue.empty()
        # WARNING logged.
        assert any(
            record.levelname in ("WARNING", "ERROR") for record in caplog.records
        ), "DDR-007 §Failure-mode contract: must log at WARNING/ERROR"


# ---------------------------------------------------------------------------
# Resume-emit ownership (TASK-GATE-D659 §D5): the C1 runner-side resume edge
# was removed — the adapter no longer exposes ``mark_resume_pending`` and the
# ``awaiting_approval → running_wave`` transition fires NO ``build-resumed``.
# Resume-emit is owned solely by the daemon subscriber seam (FW10-010).
# ---------------------------------------------------------------------------


class TestC1ResumeEdgeRemoved:
    """§D5 retirement: the runner-side resume special-case is gone."""

    def test_adapter_no_longer_exposes_mark_resume_pending(self) -> None:
        """The dead-and-broken C1 helper was removed from the adapter."""
        emitter, _nc = _build_emitter()
        adapter = LifecycleEmitterAdapter(emitter, _make_ctx())
        assert not hasattr(adapter, "mark_resume_pending")
        assert not hasattr(adapter, "_resume_pending")
        assert not hasattr(adapter, "_last_lifecycle")

    def test_awaiting_approval_to_running_wave_emits_no_build_resumed(self) -> None:
        """The runner-side resume edge no longer publishes build-resumed."""
        emitter, nc = _build_emitter()
        adapter = LifecycleEmitterAdapter(emitter, _make_ctx())

        state = _make_state(lifecycle="running_wave")
        state = _update_state(state, lifecycle="awaiting_approval", emitter=adapter)
        _update_state(state, lifecycle="running_wave", emitter=adapter)

        subjects = [s for s, _ in nc.published]
        # The pause still emits; the resume does NOT (subscriber seam owns it).
        assert "pipeline.build-paused.FEAT-X" in subjects
        assert "pipeline.build-resumed.FEAT-X" not in subjects


# ---------------------------------------------------------------------------
# TASK-FRR-PEB-006 AC-2: bridge wired → subscriber skips its own emit
# ---------------------------------------------------------------------------


class TestBridgeWiredSkipsResumeEmit:
    """When the bridge has an active registry entry, the subscriber skips emit.

    Q4 sub-option (a) per the FEAT-PEBR scoping doc: the lifecycle
    bridge owns the canonical ``pipeline.build-resumed`` emit. The
    subscriber consults
    :attr:`ApprovalSubscriberDeps.bridge_registry_lookup`; on a truthy
    return it logs INFO and skips ``emit_resumed`` so the wire receives
    exactly one envelope (the bridge's). The orchestrator's wait loop
    is still woken via the queue so the PAUSED → RUNNING transition
    proceeds.
    """

    @pytest.mark.asyncio
    async def test_active_bridge_skips_subscriber_emit(self) -> None:
        """Bridge active → no ``build-resumed`` lands via the subscriber."""
        emitter, nc = _build_emitter()
        ctx = _make_ctx()

        lookup_calls: list[tuple[str, str]] = []

        def bridge_active(feature_id: str, correlation_id: str) -> bool:
            lookup_calls.append((feature_id, correlation_id))
            return True

        deps = ApprovalSubscriberDeps(
            nats_client=_FakeSubscribeClient(),
            config=ApprovalConfig(default_wait_seconds=60, max_wait_seconds=60),
            bridge_registry_lookup=bridge_active,
        )
        sub = ApprovalSubscriber(deps)
        sub._resume_publish_ctx[ctx.build_id] = (  # type: ignore[attr-defined]
            emitter,
            ctx,
            "corr-001",
            "implementation",
        )
        queue: asyncio.Queue = asyncio.Queue()
        sub._queues[ctx.build_id] = queue  # type: ignore[attr-defined]

        envelope = _approval_response_envelope(
            request_id="req-1",
            decision="approve",
            decided_by="rich",
            correlation_id="corr-001",
        )

        await sub._on_envelope(  # type: ignore[attr-defined]
            build_id=ctx.build_id, envelope=envelope
        )

        # AC-2: lookup was consulted with (feature_id, correlation_id)
        assert lookup_calls == [(ctx.feature_id, ctx.correlation_id)], (
            "TASK-FRR-PEB-006 AC-2: bridge_registry_lookup must be "
            "consulted with the build's (feature_id, correlation_id)"
        )

        # AC-2: NO build-resumed published via subscriber path
        resumed = [s for s, _ in nc.published if "build-resumed" in s]
        assert resumed == [], (
            "TASK-FRR-PEB-006 AC-2: subscriber MUST skip its emit when "
            f"bridge is canonical, but got {resumed}"
        )

        # AC-4: queue still woken — orchestrator can dequeue the response
        # and run PAUSED → RUNNING; the bridge translator emits the
        # actual envelope from the SSE side.
        assert not queue.empty(), (
            "TASK-FRR-PEB-006 AC-4: queue MUST still wake the "
            "orchestrator's wait loop even when the subscriber's emit "
            "is skipped"
        )

    @pytest.mark.asyncio
    async def test_active_bridge_logs_info_at_skip(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A bridge-canonical skip is observable at INFO level."""
        emitter, _nc = _build_emitter()
        ctx = _make_ctx()

        deps = ApprovalSubscriberDeps(
            nats_client=_FakeSubscribeClient(),
            config=ApprovalConfig(default_wait_seconds=60, max_wait_seconds=60),
            bridge_registry_lookup=lambda fid, cid: True,
        )
        sub = ApprovalSubscriber(deps)
        sub._resume_publish_ctx[ctx.build_id] = (  # type: ignore[attr-defined]
            emitter,
            ctx,
            "corr-001",
            "implementation",
        )
        sub._queues[ctx.build_id] = asyncio.Queue()  # type: ignore[attr-defined]

        envelope = _approval_response_envelope(
            request_id="req-1",
            decision="approve",
            decided_by="rich",
            correlation_id="corr-001",
        )

        with caplog.at_level(
            logging.INFO, logger="forge.adapters.nats.approval_subscriber"
        ):
            await sub._on_envelope(  # type: ignore[attr-defined]
                build_id=ctx.build_id, envelope=envelope
            )

        canonical = [
            r
            for r in caplog.records
            if "bridge canonical" in r.getMessage().lower()
            or "TASK-FRR-PEB-006" in r.getMessage()
        ]
        assert canonical, (
            "TASK-FRR-PEB-006 AC-2: skip path MUST log at INFO so "
            "operators can confirm the bridge is the canonical emit "
            "site for this build."
        )

    @pytest.mark.asyncio
    async def test_lookup_returning_false_falls_back_to_subscriber_emit(
        self,
    ) -> None:
        """Bridge wired but inactive (False) → subscriber still emits.

        Defence-in-depth: the lookup may exist but report no active
        bridge entry (e.g. the bridge attached then detached, or a
        race during recovery). In that case the subscriber's emit
        fires — exactly the same path as ``bridge_registry_lookup=None``.
        """
        emitter, nc = _build_emitter()
        ctx = _make_ctx()

        deps = ApprovalSubscriberDeps(
            nats_client=_FakeSubscribeClient(),
            config=ApprovalConfig(default_wait_seconds=60, max_wait_seconds=60),
            bridge_registry_lookup=lambda fid, cid: False,
        )
        sub = ApprovalSubscriber(deps)
        sub._resume_publish_ctx[ctx.build_id] = (  # type: ignore[attr-defined]
            emitter,
            ctx,
            "corr-001",
            "implementation",
        )
        sub._queues[ctx.build_id] = asyncio.Queue()  # type: ignore[attr-defined]

        envelope = _approval_response_envelope(
            request_id="req-1",
            decision="approve",
            decided_by="rich",
            correlation_id="corr-001",
        )

        await sub._on_envelope(  # type: ignore[attr-defined]
            build_id=ctx.build_id, envelope=envelope
        )

        resumed = [s for s, _ in nc.published if "build-resumed" in s]
        assert resumed == ["pipeline.build-resumed.FEAT-X"], (
            "TASK-FRR-PEB-006: when bridge_registry_lookup returns "
            f"False the subscriber emits, got {resumed}"
        )

    @pytest.mark.asyncio
    async def test_lookup_raising_falls_back_to_subscriber_emit(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Lookup raising → log WARNING and fall back to subscriber emit."""
        emitter, nc = _build_emitter()
        ctx = _make_ctx()

        def boom(feature_id: str, correlation_id: str) -> bool:
            raise RuntimeError("sqlite locked")

        deps = ApprovalSubscriberDeps(
            nats_client=_FakeSubscribeClient(),
            config=ApprovalConfig(default_wait_seconds=60, max_wait_seconds=60),
            bridge_registry_lookup=boom,
        )
        sub = ApprovalSubscriber(deps)
        sub._resume_publish_ctx[ctx.build_id] = (  # type: ignore[attr-defined]
            emitter,
            ctx,
            "corr-001",
            "implementation",
        )
        sub._queues[ctx.build_id] = asyncio.Queue()  # type: ignore[attr-defined]

        envelope = _approval_response_envelope(
            request_id="req-1",
            decision="approve",
            decided_by="rich",
            correlation_id="corr-001",
        )

        with caplog.at_level(logging.WARNING):
            await sub._on_envelope(  # type: ignore[attr-defined]
                build_id=ctx.build_id, envelope=envelope
            )

        resumed = [s for s, _ in nc.published if "build-resumed" in s]
        assert resumed == ["pipeline.build-resumed.FEAT-X"], (
            "TASK-FRR-PEB-006: a lookup failure MUST fall back to the "
            "subscriber's emit so a transient SQLite hiccup does not "
            f"silently drop the resume envelope; got {resumed}"
        )
        assert any(
            "bridge_registry_lookup raised" in r.getMessage() for r in caplog.records
        ), "TASK-FRR-PEB-006: lookup failure must log at WARNING"


# ---------------------------------------------------------------------------
# TASK-FRR-PEB-006 AC-3: bridge absent → subscriber emit preserved
# ---------------------------------------------------------------------------


class TestBridgeAbsentEmitsResume:
    """When no bridge is wired, FW10-010's resume emit continues to fire.

    AC-3: backward compatibility. Tests / deploys that omit the
    ``bridge_registry_lookup`` parameter (the default ``None``) get the
    pre-FRR-PEB-006 behaviour exactly: a single ``pipeline.build-resumed``
    envelope on the wire, threaded with the inbound correlation_id, in
    the canonical FW10-010 ordering (publish-before-queue-pop).
    """

    @pytest.mark.asyncio
    async def test_no_lookup_emits_resume_once(self) -> None:
        """``bridge_registry_lookup=None`` → exactly one envelope (FW10-010)."""
        emitter, nc = _build_emitter()
        ctx = _make_ctx()

        deps = ApprovalSubscriberDeps(
            nats_client=_FakeSubscribeClient(),
            config=ApprovalConfig(default_wait_seconds=60, max_wait_seconds=60),
            # bridge_registry_lookup defaults to None — bridge absent.
        )
        # Sanity check: the default really is None.
        assert deps.bridge_registry_lookup is None, (
            "TASK-FRR-PEB-006 AC-3: ApprovalSubscriberDeps.bridge_registry_lookup "
            "must default to None to preserve FW10-010 semantics."
        )
        sub = ApprovalSubscriber(deps)
        sub._resume_publish_ctx[ctx.build_id] = (  # type: ignore[attr-defined]
            emitter,
            ctx,
            "corr-001",
            "implementation",
        )
        sub._queues[ctx.build_id] = asyncio.Queue()  # type: ignore[attr-defined]

        envelope = _approval_response_envelope(
            request_id="req-1",
            decision="approve",
            decided_by="rich",
            correlation_id="corr-001",
        )

        await sub._on_envelope(  # type: ignore[attr-defined]
            build_id=ctx.build_id, envelope=envelope
        )

        resumed = [s for s, _ in nc.published if "build-resumed" in s]
        assert resumed == ["pipeline.build-resumed.FEAT-X"], (
            "TASK-FRR-PEB-006 AC-3: bridge absent → exactly one "
            f"build-resumed via the subscriber, got {resumed}"
        )

        body = next(b for s, b in nc.published if "build-resumed" in s)
        env = _decode_envelope(body)
        # AC-4: correlation-id threaded through the emitted envelope.
        assert env.correlation_id == "corr-001", (
            "TASK-FRR-PEB-006 AC-4: the envelope MUST thread the "
            "inbound correlation_id even on the bridge-absent path."
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeSubscribeClient:
    """No-op NATSClient stand-in for ApprovalSubscriberDeps."""

    async def subscribe(self, topic: str, callback: Any) -> Any:  # noqa: ARG002
        class _Sub:
            async def unsubscribe(self) -> None:
                return None

        return _Sub()


def _approval_response_envelope(
    *,
    request_id: str,
    decision: str,
    decided_by: str,
    correlation_id: str | None,
) -> MessageEnvelope:
    """Build an :class:`ApprovalResponsePayload` envelope for the subscriber."""
    payload = ApprovalResponsePayload(
        request_id=request_id,
        decision=decision,  # type: ignore[arg-type]
        decided_by=decided_by,
        notes=None,
    )
    return MessageEnvelope(
        source_id="rich",
        event_type=EventType.APPROVAL_RESPONSE,
        correlation_id=correlation_id,
        payload=payload.model_dump(mode="json"),
    )

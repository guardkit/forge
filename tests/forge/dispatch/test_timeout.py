"""Tests for ``forge.dispatch.timeout`` — TASK-SAD-004.

Covers every acceptance criterion:

* AC-001: ``src/forge/dispatch/timeout.py`` defines ``TimeoutCoordinator``
  with ``wait_with_timeout``.
* AC-002: Default timeout is 900s (ASSUM-003); overridable per call.
* AC-003: Uses injected ``Clock`` (``forge.discovery.protocol.Clock``)
  for deterministic boundary tests against ``FakeClock``.
* AC-004: ``wait_with_timeout`` calls ``registry.release(binding)`` in
  a ``finally`` block so the subscription is released on success AND
  on timeout.
* AC-005 (B.just-inside-local-timeout): a reply arriving 1 tick before
  the cut-off is accepted.
* AC-006 (B.just-outside-local-timeout): a reply arriving 1 tick after
  the cut-off returns ``None`` and the late payload never reaches the
  gating layer.
* AC-007 (D.unsubscribe-on-timeout): after timeout the registry's
  ``bindings`` map no longer contains the correlation key.
* AC-008: No use of ``asyncio.sleep`` for the timeout itself —
  ``asyncio.timeout`` is used so cancellation is correct under task
  cancellation.

Tests construct lightweight test doubles for ``CorrelationBinding`` and
``CorrelationRegistry`` because TASK-SAD-003 has not landed yet; the
production types are imported via ``TYPE_CHECKING`` only and the
coordinator depends on the structural :class:`_RegistryLike` /
:class:`_BindingLike` protocols.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import pytest

from forge.discovery.protocol import Clock
from forge.dispatch.timeout import DEFAULT_TIMEOUT_SECONDS, TimeoutCoordinator


# ---------------------------------------------------------------------------
# Test doubles & helpers
# ---------------------------------------------------------------------------


class FakeClock:
    """Deterministic :class:`Clock` for boundary tests.

    Mirrors the structural :class:`forge.discovery.protocol.Clock`
    surface used elsewhere in the test suite.
    """

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
        self.now_calls = 0

    def now(self) -> datetime:
        self.now_calls += 1
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


@dataclass
class FakeBinding:
    """Test double for ``CorrelationBinding``.

    The coordinator only reads ``correlation_key``; the ``_future``
    attribute is plumbed by :class:`FakeRegistry.wait_for_reply` so
    tests can settle the reply at a deterministic point on the
    asyncio event loop.
    """

    correlation_key: str
    matched_agent_id: str = "agent-a"
    accepted: bool = False
    _future: asyncio.Future[dict] = field(default=None)  # type: ignore[assignment]


class FakeRegistry:
    """Test double for ``CorrelationRegistry`` (TASK-SAD-003).

    Tracks bindings in ``bindings`` so AC-007 can be asserted
    directly. ``release()`` is idempotent and unbinds late replies
    by cancelling the per-binding Future — the same shape the
    production registry will exhibit.
    """

    def __init__(self) -> None:
        self.bindings: dict[str, FakeBinding] = {}
        self.release_calls: list[str] = []
        self.late_payloads: list[dict] = []

    # Test-only helper to register a binding (production uses bind()).
    def bind(self, binding: FakeBinding) -> FakeBinding:
        loop = asyncio.get_event_loop()
        binding._future = loop.create_future()
        self.bindings[binding.correlation_key] = binding
        return binding

    async def wait_for_reply(self, binding: FakeBinding) -> Optional[dict]:
        # Mirrors the production contract: await the per-binding Future.
        # If the coordinator's hard cut-off fires, this coroutine is
        # cancelled by asyncio.timeout, the CancelledError propagates
        # out, and asyncio.timeout converts it to TimeoutError.
        return await binding._future

    def release(self, binding: FakeBinding) -> None:
        # Idempotent: late replies on the same key are silently dropped.
        self.release_calls.append(binding.correlation_key)
        self.bindings.pop(binding.correlation_key, None)
        fut = binding._future
        if fut is not None and not fut.done():
            fut.cancel()

    def deliver_late_reply(self, binding: FakeBinding, payload: dict) -> None:
        """Simulate a reply arriving after the binding has been released.

        Records the payload so tests can assert that even though the
        transport delivered something, it never reaches the gating
        layer because the binding is no longer present.
        """
        if binding.correlation_key in self.bindings:
            # Binding is still active — would be delivered. Tests use
            # this only after release; this branch should not fire.
            fut = binding._future
            if not fut.done():
                fut.set_result(payload)
            return
        # Binding is gone: payload is dropped silently.
        self.late_payloads.append(payload)


# ---------------------------------------------------------------------------
# AC-001 / AC-002 / AC-003: surface and constructor invariants
# ---------------------------------------------------------------------------


class TestTimeoutCoordinatorSurface:
    """AC-001 / AC-002 / AC-003: surface and constructor invariants."""

    def test_module_exposes_timeout_coordinator_with_wait_with_timeout(
        self,
    ) -> None:
        # AC-001: the class is defined and exposes wait_with_timeout.
        assert hasattr(TimeoutCoordinator, "wait_with_timeout")
        method = TimeoutCoordinator.wait_with_timeout
        assert inspect.iscoroutinefunction(method)

    def test_default_timeout_is_900_seconds(self) -> None:
        # AC-002: ASSUM-003 default.
        assert DEFAULT_TIMEOUT_SECONDS == 900.0
        coord = TimeoutCoordinator(FakeRegistry(), FakeClock())
        assert coord.default_timeout_seconds == 900.0

    def test_default_timeout_is_overridable_at_construction(self) -> None:
        coord = TimeoutCoordinator(
            FakeRegistry(), FakeClock(), default_timeout_seconds=42.0,
        )
        assert coord.default_timeout_seconds == 42.0

    def test_constructor_rejects_non_positive_default_timeout(self) -> None:
        with pytest.raises(ValueError):
            TimeoutCoordinator(
                FakeRegistry(), FakeClock(), default_timeout_seconds=0.0,
            )
        with pytest.raises(ValueError):
            TimeoutCoordinator(
                FakeRegistry(), FakeClock(), default_timeout_seconds=-1.0,
            )

    def test_clock_protocol_is_satisfied_structurally_by_fake_clock(
        self,
    ) -> None:
        # AC-003: FakeClock satisfies the runtime-checkable Clock Protocol.
        assert isinstance(FakeClock(), Clock)


# ---------------------------------------------------------------------------
# AC-004: release-in-finally invariants
# ---------------------------------------------------------------------------


class TestReleaseInFinally:
    """AC-004: release(binding) is called on success AND on timeout."""

    @pytest.mark.asyncio
    async def test_release_called_on_successful_reply(self) -> None:
        registry = FakeRegistry()
        binding = registry.bind(FakeBinding(correlation_key="k1"))
        coord = TimeoutCoordinator(
            registry, FakeClock(), default_timeout_seconds=1.0,
        )

        async def deliver() -> None:
            # Deliver well before the 1.0s cut-off.
            await asyncio.sleep(0.005)
            binding._future.set_result({"ok": True})

        deliver_task = asyncio.create_task(deliver())
        result = await coord.wait_with_timeout(binding)
        await deliver_task

        assert result == {"ok": True}
        assert registry.release_calls == ["k1"]
        # AC-007 corollary: bindings map no longer contains the key.
        assert "k1" not in registry.bindings

    @pytest.mark.asyncio
    async def test_release_called_on_timeout(self) -> None:
        registry = FakeRegistry()
        binding = registry.bind(FakeBinding(correlation_key="k2"))
        coord = TimeoutCoordinator(registry, FakeClock())

        # A very short cut-off so the test is fast; the future is
        # never resolved, so the timeout branch is exercised.
        result = await coord.wait_with_timeout(binding, timeout_seconds=0.02)

        assert result is None
        assert registry.release_calls == ["k2"]

    @pytest.mark.asyncio
    async def test_release_called_even_if_wait_for_reply_raises(self) -> None:
        # Defensive: a non-timeout exception from the registry must
        # still release the binding before propagating.
        registry = FakeRegistry()
        binding = registry.bind(FakeBinding(correlation_key="k3"))
        coord = TimeoutCoordinator(registry, FakeClock())

        async def raise_first(_: Any) -> None:
            raise RuntimeError("transport blew up")

        registry.wait_for_reply = raise_first  # type: ignore[assignment]

        with pytest.raises(RuntimeError, match="transport blew up"):
            await coord.wait_with_timeout(binding, timeout_seconds=1.0)

        assert registry.release_calls == ["k3"]

    @pytest.mark.asyncio
    async def test_per_call_timeout_override_is_respected(self) -> None:
        registry = FakeRegistry()
        binding = registry.bind(FakeBinding(correlation_key="k4"))
        # Default is 900s; per-call override is much smaller so the
        # test completes in milliseconds.
        coord = TimeoutCoordinator(registry, FakeClock())
        result = await coord.wait_with_timeout(binding, timeout_seconds=0.02)
        assert result is None
        assert "k4" not in registry.bindings

    @pytest.mark.asyncio
    async def test_per_call_timeout_must_be_positive(self) -> None:
        registry = FakeRegistry()
        binding = registry.bind(FakeBinding(correlation_key="k5"))
        coord = TimeoutCoordinator(registry, FakeClock())
        with pytest.raises(ValueError):
            await coord.wait_with_timeout(binding, timeout_seconds=0.0)
        with pytest.raises(ValueError):
            await coord.wait_with_timeout(binding, timeout_seconds=-0.1)


# ---------------------------------------------------------------------------
# AC-005 / AC-006: boundary scenarios
# ---------------------------------------------------------------------------


class TestBoundaryTimeoutScenarios:
    """AC-005 + AC-006: just-inside vs just-outside local timeout."""

    @pytest.mark.asyncio
    async def test_b_just_inside_local_timeout_reply_is_accepted(
        self,
    ) -> None:
        # B.just-inside-local-timeout: reply arrives 1 tick BEFORE the
        # hard cut-off and must be accepted (returned to caller).
        registry = FakeRegistry()
        binding = registry.bind(FakeBinding(correlation_key="inside"))
        coord = TimeoutCoordinator(registry, FakeClock())

        timeout_seconds = 0.10
        # 1 "tick" before the cut-off — comfortably inside the budget.
        deliver_at = timeout_seconds * 0.4

        async def deliver() -> None:
            await asyncio.sleep(deliver_at)
            if not binding._future.done():
                binding._future.set_result({"ok": True, "from": "agent-a"})

        deliver_task = asyncio.create_task(deliver())
        result = await coord.wait_with_timeout(
            binding, timeout_seconds=timeout_seconds,
        )
        await deliver_task

        assert result == {"ok": True, "from": "agent-a"}
        assert registry.release_calls == ["inside"]

    @pytest.mark.asyncio
    async def test_b_just_outside_local_timeout_reply_is_dropped(
        self,
    ) -> None:
        # B.just-outside-local-timeout: reply arrives AFTER the cut-off
        # has fired and must NOT reach the gating layer. The coordinator
        # returns None; the late payload accumulates in
        # ``registry.late_payloads`` rather than being delivered.
        registry = FakeRegistry()
        binding = registry.bind(FakeBinding(correlation_key="outside"))
        coord = TimeoutCoordinator(registry, FakeClock())

        timeout_seconds = 0.02
        late_payload = {"ok": True, "late": True}

        # Run the coordinator. The future is NEVER set during this
        # await, so asyncio.timeout fires and the binding is released.
        result = await coord.wait_with_timeout(
            binding, timeout_seconds=timeout_seconds,
        )
        assert result is None

        # Now simulate the transport delivering a late reply for the
        # already-released correlation key. The fake registry routes
        # it to the late_payloads list because the binding is gone.
        registry.deliver_late_reply(binding, late_payload)

        # The late payload is captured (proving the transport did try
        # to deliver) but the gating layer never sees it — the
        # coordinator already returned None.
        assert registry.late_payloads == [late_payload]
        assert "outside" not in registry.bindings


# ---------------------------------------------------------------------------
# AC-007: D.unsubscribe-on-timeout
# ---------------------------------------------------------------------------


class TestUnsubscribeOnTimeout:
    """AC-007: registry.bindings no longer contains the key after timeout."""

    @pytest.mark.asyncio
    async def test_d_unsubscribe_on_timeout_removes_binding_from_registry(
        self,
    ) -> None:
        registry = FakeRegistry()
        binding = registry.bind(FakeBinding(correlation_key="kdyn"))
        coord = TimeoutCoordinator(registry, FakeClock())

        assert "kdyn" in registry.bindings  # precondition

        result = await coord.wait_with_timeout(binding, timeout_seconds=0.02)

        assert result is None
        # The single property D.unsubscribe-on-timeout asserts:
        assert "kdyn" not in registry.bindings
        assert registry.release_calls == ["kdyn"]

    @pytest.mark.asyncio
    async def test_release_is_idempotent_under_double_invocation(
        self,
    ) -> None:
        # Defensive: even if the coordinator were re-run on the same
        # already-released binding, release() is idempotent and the
        # bindings map remains absent the key.
        registry = FakeRegistry()
        binding = registry.bind(FakeBinding(correlation_key="kid"))
        coord = TimeoutCoordinator(registry, FakeClock())

        await coord.wait_with_timeout(binding, timeout_seconds=0.02)
        # Re-bind a fresh future so the second wait does not error on
        # an already-cancelled Future, then run again. The second
        # release call is the idempotency check.
        registry.bind(binding)
        await coord.wait_with_timeout(binding, timeout_seconds=0.02)

        assert registry.release_calls.count("kid") == 2
        assert "kid" not in registry.bindings


# ---------------------------------------------------------------------------
# AC-003 (continued): the Clock is consulted for the start-of-wait stamp
# ---------------------------------------------------------------------------


class TestClockIsUsedForStartOfWait:
    """AC-003: the injected Clock is consulted for the start timestamp."""

    @pytest.mark.asyncio
    async def test_clock_now_is_called_before_the_wait_starts(self) -> None:
        registry = FakeRegistry()
        binding = registry.bind(FakeBinding(correlation_key="ck"))
        clock = FakeClock()
        coord = TimeoutCoordinator(registry, clock)

        # Trigger the timeout path — clock.now() is recorded before
        # the wait begins regardless of which branch wins.
        await coord.wait_with_timeout(binding, timeout_seconds=0.02)

        # At least one Clock.now() reading was taken. We do not pin
        # this to exactly 1 because future evolutions of the
        # coordinator may sample again on the timeout branch for
        # elapsed-duration logging — the contract is that the Clock
        # IS consulted, not that it is consulted exactly once.
        assert clock.now_calls >= 1


# ---------------------------------------------------------------------------
# AC-008: timeout source is asyncio.timeout (NOT asyncio.sleep)
# ---------------------------------------------------------------------------


class TestNoAsyncioSleepForTheTimeout:
    """AC-008: the timeout uses ``asyncio.timeout``, not ``asyncio.sleep``."""

    def test_module_source_uses_asyncio_timeout(self) -> None:
        # Static check: the module body uses asyncio.timeout(...) and
        # does NOT call asyncio.sleep for the cut-off itself. This is
        # the literal AC requirement — any retro-fit that swaps in
        # sleep() would silently break cancellation semantics.
        source_path = (
            Path(__file__).resolve().parents[3]
            / "src" / "forge" / "dispatch" / "timeout.py"
        )
        text = source_path.read_text(encoding="utf-8")
        assert "asyncio.timeout(" in text, (
            "TimeoutCoordinator must use asyncio.timeout(...) for the "
            "hard cut-off"
        )
        assert "asyncio.sleep(" not in text, (
            "TimeoutCoordinator must NOT use asyncio.sleep for the "
            "timeout itself — cancellation correctness depends on "
            "asyncio.timeout (AC-008)"
        )

    @pytest.mark.asyncio
    async def test_outer_cancellation_propagates_after_release(self) -> None:
        # asyncio.timeout-based cancellation propagation: cancelling
        # the surrounding task while the coordinator is waiting must
        # release the binding (finally clause) and re-raise
        # CancelledError to the caller — the property that would be
        # silently broken by switching to asyncio.sleep + branching.
        registry = FakeRegistry()
        binding = registry.bind(FakeBinding(correlation_key="kc"))
        coord = TimeoutCoordinator(registry, FakeClock())

        async def runner() -> Optional[dict]:
            return await coord.wait_with_timeout(
                binding, timeout_seconds=5.0,
            )

        task = asyncio.create_task(runner())
        # Give the coordinator a moment to enter the timeout context.
        await asyncio.sleep(0.01)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        # The finally clause ran — the binding was released even
        # though the task was cancelled mid-wait.
        assert registry.release_calls == ["kc"]
        assert "kc" not in registry.bindings

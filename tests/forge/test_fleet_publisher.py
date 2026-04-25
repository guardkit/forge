"""Unit + seam tests for :mod:`forge.adapters.nats.fleet_publisher`.

Test classes mirror the acceptance criteria of TASK-NFI-004:

- AC-001 — ``register_on_boot`` publishes ``FORGE_MANIFEST`` via
  ``nats_client.register_agent``.
- AC-002 — ``heartbeat_loop`` publishes every ``interval_seconds`` using
  the injected :class:`Clock`; no wall-clock sleeps in tests.
- AC-003 — heartbeat payload reflects ``active_tasks`` from the injected
  :class:`StatusProvider`.
- AC-004 — ``deregister`` is idempotent (calling twice does not raise).
- AC-005 — heartbeat loop catches and logs transient publish failures and
  continues.
- AC-006 — registry unreachability does not stop heartbeats.
- AC-007 — ``heartbeat_loop`` exits cleanly when ``cancel_event`` is set.
- AC-008 — SIGTERM-style integration: ``deregister`` runs before the loop
  task is cancelled.

Seam tests at the bottom verify the producer contracts from TASK-NFI-001
(``ForgeConfig.fleet``) and TASK-NFI-002 (``FORGE_MANIFEST``) so a
producer regression breaks here, not silently inside this module.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from forge.adapters.nats import fleet_publisher as fp_module
from forge.adapters.nats.fleet_publisher import (
    AGENT_ID,
    Clock,
    MonotonicClock,
    StatusProvider,
    build_heartbeat_payload,
    deregister,
    heartbeat_loop,
    register_on_boot,
)
from forge.config.models import DEFAULT_HEARTBEAT_INTERVAL_SECONDS, FleetConfig
from forge.fleet.manifest import FORGE_MANIFEST
from nats_core.events import AgentHeartbeatPayload


# ---------------------------------------------------------------------------
# Fixtures and fakes
# ---------------------------------------------------------------------------


class FakeStatusProvider:
    """Inert :class:`StatusProvider` — tests flip the public attributes."""

    def __init__(
        self,
        *,
        status: str = "ready",
        active_tasks: int = 0,
        queue_depth: int = 0,
    ) -> None:
        self.status = status
        self.active_tasks = active_tasks
        self.queue_depth = queue_depth
        self.calls: int = 0

    def get_current_status(self) -> Any:
        self.calls += 1
        return self.status

    def get_active_tasks(self) -> int:
        return self.active_tasks

    def get_queue_depth(self) -> int:
        return self.queue_depth


class FakeClock:
    """Deterministic :class:`Clock` — sleeps yield to the loop, advance ``_now``.

    Each ``sleep(s)`` call adds ``s`` to the internal monotonic counter and
    yields to the event loop with ``await asyncio.sleep(0)`` so other tasks
    (e.g. the test's ``cancel_event.set()`` task) can interleave. ``sleeps``
    records every requested duration so tests assert on cadence.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._now += seconds
        # Yield so the test driver can observe state between iterations.
        await asyncio.sleep(0)


@pytest.fixture
def nats_client() -> AsyncMock:
    """Mock ``NATSClient`` capturing the three lifecycle calls."""
    client = AsyncMock()
    client.register_agent = AsyncMock(return_value=None)
    client.deregister_agent = AsyncMock(return_value=None)
    client.heartbeat = AsyncMock(return_value=None)
    # Defensive — an accidental call to get_fleet_registry inside the loop
    # must explode loudly so tests catch the regression. AC-006.
    client.get_fleet_registry = AsyncMock(
        side_effect=AssertionError(
            "heartbeat_loop must not depend on get_fleet_registry (AC-006)"
        )
    )
    return client


@pytest.fixture
def status_provider() -> FakeStatusProvider:
    return FakeStatusProvider()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


# ---------------------------------------------------------------------------
# AC-001 — register_on_boot
# ---------------------------------------------------------------------------


class TestRegisterOnBoot:
    """AC-001 — register_on_boot publishes FORGE_MANIFEST."""

    @pytest.mark.asyncio
    async def test_register_on_boot_with_valid_client_calls_register_agent(
        self, nats_client: AsyncMock
    ) -> None:
        await register_on_boot(nats_client)
        nats_client.register_agent.assert_awaited_once_with(FORGE_MANIFEST)

    @pytest.mark.asyncio
    async def test_register_on_boot_passes_manifest_through_unchanged(
        self, nats_client: AsyncMock
    ) -> None:
        await register_on_boot(nats_client)
        # The exact manifest constant — not a copy or a mutation — is
        # passed through. Use ``is`` to assert identity.
        passed = nats_client.register_agent.await_args.args[0]
        assert passed is FORGE_MANIFEST
        assert passed.agent_id == AGENT_ID


# ---------------------------------------------------------------------------
# AC-002, AC-003 — heartbeat_loop publish cadence + payload shape
# ---------------------------------------------------------------------------


class TestHeartbeatLoopPublishContract:
    """AC-002 / AC-003 — payload + cadence."""

    @pytest.mark.asyncio
    async def test_heartbeat_loop_with_three_iterations_publishes_three_times(
        self,
        nats_client: AsyncMock,
        status_provider: FakeStatusProvider,
        clock: FakeClock,
    ) -> None:
        cancel = asyncio.Event()

        async def publish_then_count(payload: AgentHeartbeatPayload) -> None:
            if nats_client.heartbeat.await_count >= 3:
                cancel.set()

        nats_client.heartbeat.side_effect = publish_then_count

        await heartbeat_loop(
            nats_client,
            cancel,
            status_provider=status_provider,
            interval_seconds=30,
            clock=clock,
        )

        assert nats_client.heartbeat.await_count == 3
        # Cadence honoured — every inter-tick sleep used the configured
        # interval (30s). Three publishes ⇒ at most two sleeps; the third
        # publish triggers cancel before the next sleep.
        assert all(s == 30.0 for s in clock.sleeps)

    @pytest.mark.asyncio
    async def test_heartbeat_loop_with_busy_provider_publishes_active_tasks_one(
        self,
        nats_client: AsyncMock,
        clock: FakeClock,
    ) -> None:
        provider = FakeStatusProvider(status="busy", active_tasks=1, queue_depth=0)
        cancel = asyncio.Event()

        async def stop_after_first(payload: AgentHeartbeatPayload) -> None:
            cancel.set()

        nats_client.heartbeat.side_effect = stop_after_first

        await heartbeat_loop(
            nats_client,
            cancel,
            status_provider=provider,
            interval_seconds=30,
            clock=clock,
        )

        published: AgentHeartbeatPayload = nats_client.heartbeat.await_args.args[0]
        assert published.agent_id == "forge"
        assert published.status == "busy"
        assert published.active_tasks == 1
        assert published.queue_depth == 0

    @pytest.mark.asyncio
    async def test_heartbeat_loop_with_idle_provider_publishes_active_tasks_zero(
        self,
        nats_client: AsyncMock,
        clock: FakeClock,
    ) -> None:
        provider = FakeStatusProvider(status="ready", active_tasks=0, queue_depth=0)
        cancel = asyncio.Event()
        nats_client.heartbeat.side_effect = lambda p: cancel.set()

        await heartbeat_loop(
            nats_client,
            cancel,
            status_provider=provider,
            interval_seconds=30,
            clock=clock,
        )

        published: AgentHeartbeatPayload = nats_client.heartbeat.await_args.args[0]
        assert published.status == "ready"
        assert published.active_tasks == 0

    @pytest.mark.asyncio
    async def test_heartbeat_loop_uses_default_interval_from_config(
        self,
        nats_client: AsyncMock,
        status_provider: FakeStatusProvider,
        clock: FakeClock,
    ) -> None:
        cancel = asyncio.Event()
        call_counter = {"n": 0}

        async def stop_on_second(payload: AgentHeartbeatPayload) -> None:
            call_counter["n"] += 1
            if call_counter["n"] >= 2:
                cancel.set()

        nats_client.heartbeat.side_effect = stop_on_second

        await heartbeat_loop(
            nats_client,
            cancel,
            status_provider=status_provider,
            clock=clock,
            # No interval_seconds — exercise the default.
        )

        assert nats_client.heartbeat.await_count == 2
        assert clock.sleeps == [float(DEFAULT_HEARTBEAT_INTERVAL_SECONDS)]


# ---------------------------------------------------------------------------
# AC-002 — uptime derivation
# ---------------------------------------------------------------------------


class TestHeartbeatPayloadUptime:
    def test_build_heartbeat_payload_with_advanced_clock_reports_uptime(
        self, status_provider: FakeStatusProvider
    ) -> None:
        clock = FakeClock(start=100.0)
        clock._now = 142.5  # ~42s elapsed

        payload = build_heartbeat_payload(
            status_provider=status_provider,
            started_at_monotonic=100.0,
            clock=clock,
        )

        assert payload.uptime_seconds == 42

    def test_build_heartbeat_payload_with_clock_regression_reports_zero(
        self, status_provider: FakeStatusProvider
    ) -> None:
        # Defensive guard: a misbehaving fake clock that "goes backwards"
        # produces zero, never a negative uptime that would fail
        # AgentHeartbeatPayload's ``ge=0`` validator.
        clock = FakeClock(start=50.0)
        payload = build_heartbeat_payload(
            status_provider=status_provider,
            started_at_monotonic=100.0,
            clock=clock,
        )
        assert payload.uptime_seconds == 0


# ---------------------------------------------------------------------------
# AC-004 — deregister idempotency
# ---------------------------------------------------------------------------


class TestDeregisterIdempotency:
    @pytest.mark.asyncio
    async def test_deregister_with_default_reason_calls_client(
        self, nats_client: AsyncMock
    ) -> None:
        await deregister(nats_client)
        nats_client.deregister_agent.assert_awaited_once_with(
            AGENT_ID, reason="shutdown"
        )

    @pytest.mark.asyncio
    async def test_deregister_with_custom_reason_threads_through(
        self, nats_client: AsyncMock
    ) -> None:
        await deregister(nats_client, reason="rolling-restart")
        nats_client.deregister_agent.assert_awaited_once_with(
            AGENT_ID, reason="rolling-restart"
        )

    @pytest.mark.asyncio
    async def test_deregister_called_twice_does_not_raise(
        self, nats_client: AsyncMock
    ) -> None:
        await deregister(nats_client)
        await deregister(nats_client)  # AC-004 — must not raise
        assert nats_client.deregister_agent.await_count == 2

    @pytest.mark.asyncio
    async def test_deregister_swallows_underlying_exception(
        self,
        nats_client: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        nats_client.deregister_agent.side_effect = ConnectionError("bus down")
        with caplog.at_level(logging.WARNING, logger=fp_module.__name__):
            await deregister(nats_client, reason="shutdown")  # must not raise
        # Underlying error is logged so operators see it.
        assert any(
            "deregister" in rec.message and "bus down" in rec.message
            for rec in caplog.records
        )


# ---------------------------------------------------------------------------
# AC-005 — transient publish failures do not exit the loop
# ---------------------------------------------------------------------------


class TestHeartbeatLoopResilience:
    @pytest.mark.asyncio
    async def test_heartbeat_loop_with_intermittent_publish_failure_continues(
        self,
        nats_client: AsyncMock,
        status_provider: FakeStatusProvider,
        clock: FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        cancel = asyncio.Event()
        outcomes: list[Any] = [
            ConnectionError("transient bus error"),
            None,  # success
            None,  # success — set cancel on this one
        ]
        idx = {"i": 0}

        async def scripted_publish(payload: AgentHeartbeatPayload) -> None:
            i = idx["i"]
            idx["i"] += 1
            if i >= 2:
                cancel.set()
            result = outcomes[i]
            if isinstance(result, BaseException):
                raise result

        nats_client.heartbeat.side_effect = scripted_publish

        with caplog.at_level(logging.WARNING, logger=fp_module.__name__):
            await heartbeat_loop(
                nats_client,
                cancel,
                status_provider=status_provider,
                interval_seconds=10,
                clock=clock,
            )

        # All three iterations attempted despite the first one raising.
        assert nats_client.heartbeat.await_count == 3
        assert any(
            "heartbeat publish failed" in rec.message for rec in caplog.records
        )

    @pytest.mark.asyncio
    async def test_heartbeat_loop_with_unreachable_registry_keeps_publishing(
        self,
        nats_client: AsyncMock,
        status_provider: FakeStatusProvider,
        clock: FakeClock,
    ) -> None:
        # AC-006 — get_fleet_registry is rigged to explode in the
        # nats_client fixture. If the loop ever consults it, the test
        # AssertionError surfaces and this test fails.
        cancel = asyncio.Event()

        async def stop_after_two(payload: AgentHeartbeatPayload) -> None:
            if nats_client.heartbeat.await_count >= 2:
                cancel.set()

        nats_client.heartbeat.side_effect = stop_after_two

        await heartbeat_loop(
            nats_client,
            cancel,
            status_provider=status_provider,
            interval_seconds=5,
            clock=clock,
        )

        assert nats_client.heartbeat.await_count == 2
        nats_client.get_fleet_registry.assert_not_awaited()


# ---------------------------------------------------------------------------
# AC-007 — clean exit on cancel_event
# ---------------------------------------------------------------------------


class TestHeartbeatLoopCancellation:
    @pytest.mark.asyncio
    async def test_heartbeat_loop_with_preset_cancel_exits_without_publishing(
        self,
        nats_client: AsyncMock,
        status_provider: FakeStatusProvider,
        clock: FakeClock,
    ) -> None:
        cancel = asyncio.Event()
        cancel.set()

        await heartbeat_loop(
            nats_client,
            cancel,
            status_provider=status_provider,
            interval_seconds=30,
            clock=clock,
        )

        # The while-condition is checked first, so a pre-set event means
        # zero publishes are issued — the loop exits immediately.
        nats_client.heartbeat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_heartbeat_loop_with_cancel_during_sleep_exits_early(
        self,
        nats_client: AsyncMock,
        status_provider: FakeStatusProvider,
    ) -> None:
        # Build a clock whose sleep blocks long enough for the test
        # driver to set the cancel event mid-sleep.
        sleep_started = asyncio.Event()

        class BlockingClock:
            def monotonic(self) -> float:
                return 0.0

            async def sleep(self, seconds: float) -> None:
                sleep_started.set()
                # Block until cancelled by the heartbeat loop's race.
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    raise

        cancel = asyncio.Event()

        async def driver() -> None:
            # Wait for the loop to enter sleep, then trip cancel mid-sleep.
            await sleep_started.wait()
            cancel.set()

        loop_task = asyncio.create_task(
            heartbeat_loop(
                nats_client,
                cancel,
                status_provider=status_provider,
                interval_seconds=60,
                clock=BlockingClock(),
            )
        )
        driver_task = asyncio.create_task(driver())

        await asyncio.wait_for(
            asyncio.gather(loop_task, driver_task), timeout=2.0
        )

        # Exactly one publish before mid-sleep cancellation.
        nats_client.heartbeat.assert_awaited_once()


# ---------------------------------------------------------------------------
# AC-008 — SIGTERM integration: deregister runs before the loop task is cancelled
# ---------------------------------------------------------------------------


class TestSigtermSequencing:
    @pytest.mark.asyncio
    async def test_sigterm_sequence_with_running_loop_calls_deregister_first(
        self,
        nats_client: AsyncMock,
        status_provider: FakeStatusProvider,
    ) -> None:
        """Simulate the entrypoint's SIGTERM handler: register, run loop,
        on signal call deregister(), then cancel the loop task.

        Verify the call order via ``MagicMock.mock_calls`` on a parent
        recorder so any code path that flips deregister to "after cancel"
        breaks this test.
        """
        recorder = MagicMock()
        recorder.attach_mock(nats_client.register_agent, "register")
        recorder.attach_mock(nats_client.heartbeat, "heartbeat")
        recorder.attach_mock(nats_client.deregister_agent, "deregister")

        clock = FakeClock()
        cancel = asyncio.Event()

        # Boot
        await register_on_boot(nats_client)

        # Run loop in the background; let it tick at least once.
        published_once = asyncio.Event()

        async def trip_published(payload: AgentHeartbeatPayload) -> None:
            published_once.set()

        nats_client.heartbeat.side_effect = trip_published

        loop_task = asyncio.create_task(
            heartbeat_loop(
                nats_client,
                cancel,
                status_provider=status_provider,
                interval_seconds=1,
                clock=clock,
            )
        )

        await asyncio.wait_for(published_once.wait(), timeout=1.0)

        # SIGTERM-style shutdown: deregister BEFORE cancelling the loop task.
        await deregister(nats_client, reason="sigterm")
        cancel.set()
        await asyncio.wait_for(loop_task, timeout=1.0)

        # Assert order: register → heartbeat(s) → deregister.
        names = [name for name, _args, _kwargs in recorder.mock_calls]
        # Filter only the three lifecycle calls we attached.
        relevant = [n for n in names if n in {"register", "heartbeat", "deregister"}]
        assert relevant[0] == "register"
        assert relevant[-1] == "deregister"
        assert "heartbeat" in relevant


# ---------------------------------------------------------------------------
# Default-clock smoke test — guard against accidental wall-clock dependency
# at the module surface (the loop body itself is exercised via FakeClock).
# ---------------------------------------------------------------------------


class TestMonotonicClock:
    def test_monotonic_clock_returns_non_decreasing_value(self) -> None:
        c = MonotonicClock()
        a = c.monotonic()
        b = c.monotonic()
        assert b >= a

    @pytest.mark.asyncio
    async def test_monotonic_clock_sleep_is_awaitable(self) -> None:
        c = MonotonicClock()
        await c.sleep(0)  # zero-duration round-trip


# ---------------------------------------------------------------------------
# Protocol shape — runtime_checkable means duck-typed fakes pass isinstance
# ---------------------------------------------------------------------------


class TestProtocolShapes:
    def test_fake_status_provider_satisfies_protocol(
        self, status_provider: FakeStatusProvider
    ) -> None:
        assert isinstance(status_provider, StatusProvider)

    def test_fake_clock_satisfies_protocol(self, clock: FakeClock) -> None:
        assert isinstance(clock, Clock)

    def test_monotonic_clock_satisfies_protocol(self) -> None:
        assert isinstance(MonotonicClock(), Clock)


# ---------------------------------------------------------------------------
# Seam tests — verify producer contracts from TASK-NFI-001 and TASK-NFI-002
# (verbatim from the TASK-NFI-004 task spec).
# ---------------------------------------------------------------------------


@pytest.mark.seam
def test_forge_config_fleet_format() -> None:
    """Verify FleetConfig matches the expected format.

    Contract: heartbeat_interval_seconds (int, default 30),
              stale_heartbeat_seconds (int, default 90)
    Producer: TASK-NFI-001
    """
    cfg = FleetConfig()
    assert cfg.heartbeat_interval_seconds == 30
    assert cfg.stale_heartbeat_seconds == 90
    assert isinstance(cfg.heartbeat_interval_seconds, int)


@pytest.mark.seam
def test_forge_manifest_contract() -> None:
    """Verify FORGE_MANIFEST is importable as a module-level AgentManifest.

    Contract: Importable as ``from forge.fleet.manifest import FORGE_MANIFEST``;
              agent_id == "forge"; passes straight through to register_agent.
    Producer: TASK-NFI-002
    """
    assert FORGE_MANIFEST.agent_id == "forge"
    assert FORGE_MANIFEST.trust_tier == "core"
    # Secret-free check — no credential-shaped substrings appear in the
    # serialised manifest.
    dumped = FORGE_MANIFEST.model_dump_json()
    for forbidden in ("api_key", "token", "password", "secret", "credential"):
        assert forbidden.lower() not in dumped.lower(), (
            f"Secret-like field found: {forbidden}"
        )

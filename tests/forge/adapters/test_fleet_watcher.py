"""Unit tests for ``forge.adapters.nats.fleet_watcher``.

Each ``Test*`` class maps to one acceptance criterion of TASK-NFI-005
so the criterion → verifier link stays explicit. The tests exercise
the watcher against a *real* :class:`forge.discovery.cache.DiscoveryCache`
so the producer/consumer seam (TASK-NFI-003 → TASK-NFI-005) is covered
end-to-end without standing up NATS.

Production collaborators that still need stubbing:

* ``nats_client`` — a hand-rolled fake that captures the subscribe and
  watch_fleet callbacks so tests can drive them deterministically.
* ``Clock`` — a deterministic :class:`FakeClock` matching the one in
  ``tests/forge/discovery/test_discovery.py`` (AC-CACHE-TTL).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from nats_core.envelope import EventType, MessageEnvelope
from nats_core.events import AgentHeartbeatPayload
from nats_core.manifest import AgentManifest, IntentCapability, ToolCapability
from nats_core.topics import Topics

from forge.adapters.nats.fleet_watcher import (
    DEGRADED_STATUS,
    FleetWatcher,
    SnapshotReader,
    run_one_sweep,
    stale_sweeper,
    watch,
)
from forge.config.models import FleetConfig
from forge.discovery.cache import DiscoveryCache
from forge.discovery.protocol import FleetEventSink

# ---------------------------------------------------------------------------
# Test doubles & helpers
# ---------------------------------------------------------------------------


class FakeClock:
    """Deterministic :class:`forge.discovery.protocol.Clock` for tests."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


def _intent(pattern: str = "tasks.*", confidence: float = 0.85) -> IntentCapability:
    return IntentCapability(
        pattern=pattern,
        signals=[pattern.split(".")[0]],
        confidence=confidence,
        description=f"intent {pattern}",
    )


def _tool(name: str = "do_thing") -> ToolCapability:
    return ToolCapability(
        name=name,
        description=f"{name} description",
        parameters={"type": "object", "properties": {}},
        returns="dict",
        risk_level="read_only",
    )


def _manifest(
    agent_id: str,
    *,
    version: str = "0.1.0",
    intents: list[IntentCapability] | None = None,
    tools: list[ToolCapability] | None = None,
) -> AgentManifest:
    return AgentManifest(
        agent_id=agent_id,
        name=agent_id.title(),
        version=version,
        template="test-template",
        trust_tier="specialist",
        status="ready",
        max_concurrent=2,
        intents=intents or [_intent()],
        tools=tools or [_tool()],
        required_permissions=[],
    )


def _hb(
    agent_id: str,
    *,
    status: str = "ready",
    queue_depth: int = 0,
    active_tasks: int = 0,
    uptime_seconds: int = 60,
) -> AgentHeartbeatPayload:
    return AgentHeartbeatPayload(
        agent_id=agent_id,
        status=status,  # type: ignore[arg-type]
        queue_depth=queue_depth,
        active_tasks=active_tasks,
        uptime_seconds=uptime_seconds,
    )


def _envelope(payload: AgentHeartbeatPayload | dict[str, Any]) -> MessageEnvelope:
    raw = payload.model_dump() if isinstance(payload, AgentHeartbeatPayload) else payload
    source = raw.get("agent_id", "test-source") if isinstance(raw, dict) else "test"
    return MessageEnvelope(
        source_id=source,
        event_type=EventType.AGENT_HEARTBEAT,
        payload=raw,
    )


class FakeNATSClient:
    """Captures subscribe + watch_fleet callbacks so tests can drive them.

    ``watch_fleet`` blocks on ``self._resume`` until tests call
    :meth:`stop_watching` (or :meth:`raise_in_watch`). This mirrors
    production where ``watch_fleet`` is a long-running coroutine.
    """

    def __init__(self) -> None:
        self.subscribe_calls: list[tuple[str, Callable[..., Awaitable[None]]]] = []
        self.watch_fleet_calls: int = 0
        self._fleet_callback: Callable[[str, AgentManifest | None], Awaitable[None]] | None = None
        self._heartbeat_callback: Callable[[MessageEnvelope], Awaitable[None]] | None = None
        self._resume = asyncio.Event()
        self._raise_exc: BaseException | None = None
        self._raise_after_calls: int | None = None

    # ----- captured callbacks ------------------------------------------------
    @property
    def fleet_callback(self) -> Callable[[str, AgentManifest | None], Awaitable[None]]:
        assert self._fleet_callback is not None, "watch_fleet not yet called"
        return self._fleet_callback

    @property
    def heartbeat_callback(self) -> Callable[[MessageEnvelope], Awaitable[None]]:
        assert self._heartbeat_callback is not None, "subscribe not yet called"
        return self._heartbeat_callback

    # ----- driver controls ---------------------------------------------------
    def stop_watching(self) -> None:
        """Unblock the current watch_fleet call so the run loop returns."""
        self._resume.set()

    def raise_in_watch(self, exc: BaseException, *, after_calls: int = 1) -> None:
        """Make the *Nth* watch_fleet call raise ``exc``."""
        self._raise_exc = exc
        self._raise_after_calls = after_calls

    # ----- nats_client surface ----------------------------------------------
    async def subscribe(
        self,
        topic: str,
        callback: Callable[[MessageEnvelope], Awaitable[None]],
    ) -> Any:
        self.subscribe_calls.append((topic, callback))
        self._heartbeat_callback = callback
        return AsyncMock()

    async def watch_fleet(
        self,
        callback: Callable[[str, AgentManifest | None], Awaitable[None]],
    ) -> None:
        self.watch_fleet_calls += 1
        self._fleet_callback = callback
        if (
            self._raise_exc is not None
            and self._raise_after_calls is not None
            and self.watch_fleet_calls == self._raise_after_calls
        ):
            exc = self._raise_exc
            # Reset so subsequent calls don't keep raising.
            self._raise_exc = None
            self._raise_after_calls = None
            raise exc
        await self._resume.wait()
        # Reset for any future re-entry by the run loop.
        self._resume.clear()


# ---------------------------------------------------------------------------
# Seam tests (per task spec)
# ---------------------------------------------------------------------------


@pytest.mark.seam
def test_fleet_event_sink_protocol_shape() -> None:
    """Verify FleetEventSink protocol surface from TASK-NFI-003.

    Contract: upsert_agent(manifest), remove_agent(agent_id),
              update_heartbeat(agent_id, hb, status_changed)
    Producer: TASK-NFI-003
    """
    assert hasattr(FleetEventSink, "upsert_agent")
    assert hasattr(FleetEventSink, "remove_agent")
    assert hasattr(FleetEventSink, "update_heartbeat")


@pytest.mark.seam
def test_forge_config_stale_threshold() -> None:
    """ForgeConfig.fleet.stale_heartbeat_seconds default = 90 (TASK-NFI-001)."""
    cfg = FleetConfig()
    assert cfg.stale_heartbeat_seconds == 90


# ---------------------------------------------------------------------------
# AC-001 — Three event types dispatched correctly
# ---------------------------------------------------------------------------


class TestThreeEventTypesDispatched:
    """AC-001: register / deregister / heartbeat each call the right sink method."""

    @pytest.mark.asyncio
    async def test_register_event_calls_upsert_agent(self) -> None:
        clock = FakeClock()
        cache = DiscoveryCache(clock=clock)
        watcher = FleetWatcher(cache, status_reader=cache)

        manifest = _manifest("agent-alpha")
        await watcher.on_fleet_change("agent-alpha", manifest)

        snapshot = await cache.snapshot()
        assert "agent-alpha" in snapshot
        assert snapshot["agent-alpha"].manifest.agent_id == "agent-alpha"

    @pytest.mark.asyncio
    async def test_deregister_event_calls_remove_agent(self) -> None:
        clock = FakeClock()
        cache = DiscoveryCache(clock=clock)
        watcher = FleetWatcher(cache, status_reader=cache)

        await watcher.on_fleet_change("agent-bravo", _manifest("agent-bravo"))
        assert "agent-bravo" in await cache.snapshot()

        await watcher.on_fleet_change("agent-bravo", None)
        assert "agent-bravo" not in await cache.snapshot()

    @pytest.mark.asyncio
    async def test_heartbeat_event_calls_update_heartbeat(self) -> None:
        clock = FakeClock()
        cache = DiscoveryCache(clock=clock)
        watcher = FleetWatcher(cache, status_reader=cache)
        await watcher.on_fleet_change("agent-charlie", _manifest("agent-charlie"))

        clock.advance(5)
        await watcher.on_heartbeat(
            _envelope(_hb("agent-charlie", status="busy", queue_depth=3, active_tasks=1)),
        )

        snap = await cache.snapshot()
        assert snap["agent-charlie"].last_heartbeat_status == "busy"
        assert snap["agent-charlie"].last_queue_depth == 3
        assert snap["agent-charlie"].last_active_tasks == 1

    @pytest.mark.asyncio
    async def test_dispatch_uses_explicit_sink_methods(self) -> None:
        """Verify the watcher really invokes each named sink method."""
        sink = AsyncMock(spec=FleetEventSink)
        watcher = FleetWatcher(sink)

        manifest = _manifest("agent-delta")
        await watcher.on_fleet_change("agent-delta", manifest)
        sink.upsert_agent.assert_awaited_once_with(manifest)

        await watcher.on_fleet_change("agent-delta", None)
        sink.remove_agent.assert_awaited_once_with("agent-delta")

        hb = _hb("agent-delta", status="busy")
        await watcher.on_heartbeat(_envelope(hb))
        sink.update_heartbeat.assert_awaited_once()
        args = sink.update_heartbeat.await_args
        assert args.args[0] == "agent-delta"
        assert args.args[1].status == "busy"
        assert isinstance(args.args[2], bool)


# ---------------------------------------------------------------------------
# AC-002 — Malformed events are logged and dropped; the watcher continues
# ---------------------------------------------------------------------------


class TestMalformedEventsAreLoggedAndDropped:
    """AC-002: invalid heartbeat payloads do not crash the watcher."""

    @pytest.mark.asyncio
    async def test_malformed_heartbeat_payload_dropped_with_warning(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        sink = AsyncMock(spec=FleetEventSink)
        watcher = FleetWatcher(sink)
        bad_envelope = _envelope({"agent_id": "agent-foxtrot", "status": "not-a-real-status"})

        with caplog.at_level(logging.WARNING, logger="forge.adapters.nats.fleet_watcher"):
            await watcher.on_heartbeat(bad_envelope)

        sink.update_heartbeat.assert_not_awaited()
        assert any(
            "invalid heartbeat payload" in rec.message
            for rec in caplog.records
        )

    @pytest.mark.asyncio
    async def test_malformed_envelope_payload_type_dropped(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Even a totally non-dict payload should not blow up."""
        sink = AsyncMock(spec=FleetEventSink)
        watcher = FleetWatcher(sink)
        bad_envelope = MessageEnvelope(
            source_id="agent-foxtrot",
            event_type=EventType.AGENT_HEARTBEAT,
            payload={"definitely_not_a_heartbeat": True},
        )

        with caplog.at_level(logging.WARNING, logger="forge.adapters.nats.fleet_watcher"):
            await watcher.on_heartbeat(bad_envelope)

        sink.update_heartbeat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_subsequent_valid_heartbeat_is_processed_after_bad_one(self) -> None:
        clock = FakeClock()
        cache = DiscoveryCache(clock=clock)
        watcher = FleetWatcher(cache, status_reader=cache)
        await watcher.on_fleet_change("agent-golf", _manifest("agent-golf"))

        # First: malformed
        await watcher.on_heartbeat(_envelope({"agent_id": "agent-golf"}))
        # Then: valid — must still be applied
        await watcher.on_heartbeat(
            _envelope(_hb("agent-golf", status="busy", queue_depth=7)),
        )

        snap = await cache.snapshot()
        assert snap["agent-golf"].last_heartbeat_status == "busy"
        assert snap["agent-golf"].last_queue_depth == 7


# ---------------------------------------------------------------------------
# AC-003 — status_changed flag computed correctly
# ---------------------------------------------------------------------------


class TestStatusChangedFlag:
    """AC-003: the watcher diffs the new heartbeat status against the previous."""

    @pytest.mark.asyncio
    async def test_first_heartbeat_after_register_is_status_change(self) -> None:
        sink = AsyncMock(spec=FleetEventSink)
        watcher = FleetWatcher(sink)

        # Register sets the watcher's tracked status to "ready".
        await watcher.on_fleet_change("agent-hotel", _manifest("agent-hotel"))
        await watcher.on_heartbeat(_envelope(_hb("agent-hotel", status="busy")))

        args = sink.update_heartbeat.await_args
        assert args.args[2] is True  # ready -> busy

    @pytest.mark.asyncio
    async def test_repeated_same_status_is_not_a_change(self) -> None:
        sink = AsyncMock(spec=FleetEventSink)
        watcher = FleetWatcher(sink)

        await watcher.on_fleet_change("agent-india", _manifest("agent-india"))
        # First "ready" heartbeat after a register: ready==ready => no change.
        await watcher.on_heartbeat(_envelope(_hb("agent-india", status="ready")))
        first = sink.update_heartbeat.await_args
        assert first.args[2] is False

        sink.reset_mock()
        await watcher.on_heartbeat(_envelope(_hb("agent-india", status="ready")))
        second = sink.update_heartbeat.await_args
        assert second.args[2] is False

    @pytest.mark.asyncio
    async def test_status_change_uses_cache_view_when_reader_wired(self) -> None:
        """If the cache transitions agent → degraded (e.g. via stale_sweeper),
        the watcher must observe that flip when computing status_changed."""
        clock = FakeClock()
        cache = DiscoveryCache(clock=clock)
        watcher = FleetWatcher(cache, status_reader=cache)

        await watcher.on_fleet_change("agent-juliet", _manifest("agent-juliet"))
        # Simulate stale_sweeper flipping the cache to degraded directly via
        # the sink surface. Watcher's in-memory mirror still reads "ready".
        await cache.update_heartbeat(
            "agent-juliet", _hb("agent-juliet", status="degraded"), status_changed=True,
        )

        # A fresh "ready" heartbeat now arrives. Without the reader, the
        # in-memory mirror would say "ready==ready"; with the reader,
        # the cache says "degraded -> ready" → status_changed must be True.
        sink_spy = AsyncMock(wraps=cache, spec=FleetEventSink)
        watcher_with_reader = FleetWatcher(sink_spy, status_reader=cache)
        await watcher_with_reader.on_heartbeat(
            _envelope(_hb("agent-juliet", status="ready")),
        )
        assert sink_spy.update_heartbeat.await_args.args[2] is True


# ---------------------------------------------------------------------------
# AC-004 — Re-registration with newer manifest version supersedes
# ---------------------------------------------------------------------------


class TestReRegistrationIdempotency:
    """AC-004: a version-bumped re-register replaces the cache entry."""

    @pytest.mark.asyncio
    async def test_reregister_replaces_manifest_no_duplicates(self) -> None:
        clock = FakeClock()
        cache = DiscoveryCache(clock=clock)
        watcher = FleetWatcher(cache, status_reader=cache)

        await watcher.on_fleet_change("agent-kilo", _manifest("agent-kilo", version="0.1.0"))
        await watcher.on_fleet_change(
            "agent-kilo", _manifest("agent-kilo", version="0.2.0"),
        )

        snap = await cache.snapshot()
        assert len(snap) == 1
        assert snap["agent-kilo"].manifest.version == "0.2.0"


# ---------------------------------------------------------------------------
# AC-005 — stale_sweeper marks stale agents as degraded
# ---------------------------------------------------------------------------


class TestStaleSweeper:
    """AC-005: agents whose last_heartbeat_at age > threshold flip to degraded."""

    @pytest.mark.asyncio
    async def test_run_one_sweep_marks_stale_agent_as_degraded(self) -> None:
        clock = FakeClock()
        cache = DiscoveryCache(clock=clock)
        watcher = FleetWatcher(cache, status_reader=cache)
        await watcher.on_fleet_change("agent-lima", _manifest("agent-lima"))

        # Advance past the stale threshold (FleetConfig default 90s).
        clock.advance(120)
        flipped = await run_one_sweep(cache, cache, clock, stale_heartbeat_seconds=90)

        assert flipped == 1
        snap = await cache.snapshot()
        assert snap["agent-lima"].last_heartbeat_status == DEGRADED_STATUS

    @pytest.mark.asyncio
    async def test_run_one_sweep_skips_fresh_agent(self) -> None:
        clock = FakeClock()
        cache = DiscoveryCache(clock=clock)
        watcher = FleetWatcher(cache, status_reader=cache)
        await watcher.on_fleet_change("agent-mike", _manifest("agent-mike"))

        clock.advance(30)  # well below 90s threshold
        flipped = await run_one_sweep(cache, cache, clock, stale_heartbeat_seconds=90)

        assert flipped == 0
        snap = await cache.snapshot()
        assert snap["agent-mike"].last_heartbeat_status == "ready"

    @pytest.mark.asyncio
    async def test_run_one_sweep_skips_already_degraded(self) -> None:
        """A second sweep on an already-degraded agent must be a no-op."""
        clock = FakeClock()
        cache = DiscoveryCache(clock=clock)
        watcher = FleetWatcher(cache, status_reader=cache)
        await watcher.on_fleet_change("agent-november", _manifest("agent-november"))

        clock.advance(120)
        first = await run_one_sweep(cache, cache, clock, stale_heartbeat_seconds=90)
        assert first == 1

        clock.advance(30)
        second = await run_one_sweep(cache, cache, clock, stale_heartbeat_seconds=90)
        assert second == 0  # idempotent

    @pytest.mark.asyncio
    async def test_stale_sweeper_loops_and_can_be_cancelled(self) -> None:
        clock = FakeClock()
        cache = DiscoveryCache(clock=clock)
        watcher = FleetWatcher(cache, status_reader=cache)
        await watcher.on_fleet_change("agent-oscar", _manifest("agent-oscar"))
        clock.advance(120)

        task = asyncio.create_task(
            stale_sweeper(cache, cache, clock, stale_heartbeat_seconds=90, interval_s=0),
        )
        # Yield the loop so the sweeper runs at least one pass.
        for _ in range(5):
            await asyncio.sleep(0)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        snap = await cache.snapshot()
        assert snap["agent-oscar"].last_heartbeat_status == DEGRADED_STATUS

    @pytest.mark.asyncio
    async def test_stale_sweeper_uses_injected_clock(self) -> None:
        """A clock that never advances must produce zero flips."""
        clock = FakeClock()
        cache = DiscoveryCache(clock=clock)
        watcher = FleetWatcher(cache, status_reader=cache)
        await watcher.on_fleet_change("agent-papa", _manifest("agent-papa"))
        # No clock.advance() at all.

        flipped = await run_one_sweep(cache, cache, clock, stale_heartbeat_seconds=90)
        assert flipped == 0


# ---------------------------------------------------------------------------
# AC-006 — Racing register+deregister via asyncio.gather
# ---------------------------------------------------------------------------


class TestRacingRegisterDeregister:
    """AC-006: concurrent register+deregister produces a consistent end state."""

    @pytest.mark.asyncio
    async def test_concurrent_register_and_deregister_are_consistent(self) -> None:
        clock = FakeClock()
        cache = DiscoveryCache(clock=clock)
        watcher = FleetWatcher(cache, status_reader=cache)

        manifest = _manifest("agent-quebec")

        # Fire a register and a deregister concurrently. The cache's
        # internal asyncio.Lock guarantees one wins atomically — the
        # final state is either {agent-quebec: entry} or {} but never
        # a torn read mid-update.
        await asyncio.gather(
            watcher.on_fleet_change("agent-quebec", manifest),
            watcher.on_fleet_change("agent-quebec", None),
        )

        snap = await cache.snapshot()
        # One of the two operations must have been the winner. Whichever
        # one wins, the cache is internally consistent: either the entry
        # is fully present, or fully absent — never half-built.
        assert snap == {} or list(snap.keys()) == ["agent-quebec"]
        if snap:
            assert snap["agent-quebec"].manifest.agent_id == "agent-quebec"


# ---------------------------------------------------------------------------
# AC-007 — Reconnect loop survives transient nats_client errors
# ---------------------------------------------------------------------------


class TestReconnectLoop:
    """AC-007: watch survives a transient watch_fleet error and recovers."""

    @pytest.mark.asyncio
    async def test_watch_recovers_after_transient_watch_fleet_error(self) -> None:
        sink = AsyncMock(spec=FleetEventSink)
        client = FakeNATSClient()
        # First call raises, second call blocks normally.
        client.raise_in_watch(RuntimeError("transient nats error"), after_calls=1)

        task = asyncio.create_task(
            watch(client, sink, reconnect_backoff_seconds=0),
        )

        # Yield the loop enough times for the reconnect to land us on the
        # second watch_fleet invocation.
        for _ in range(20):
            if client.watch_fleet_calls >= 2:
                break
            await asyncio.sleep(0)

        try:
            assert client.watch_fleet_calls >= 2, (
                f"expected reconnect; saw {client.watch_fleet_calls} watch_fleet calls"
            )
            # Subscribe was re-invoked too — once per reconnect attempt.
            assert len(client.subscribe_calls) >= 2
        finally:
            client.stop_watching()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    @pytest.mark.asyncio
    async def test_watch_logs_warning_on_transient_error(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        sink = AsyncMock(spec=FleetEventSink)
        client = FakeNATSClient()
        client.raise_in_watch(RuntimeError("connection reset"), after_calls=1)

        with caplog.at_level(logging.WARNING, logger="forge.adapters.nats.fleet_watcher"):
            task = asyncio.create_task(
                watch(client, sink, reconnect_backoff_seconds=0),
            )
            for _ in range(20):
                if client.watch_fleet_calls >= 2:
                    break
                await asyncio.sleep(0)
            client.stop_watching()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert any("transient error" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Public surface / wiring sanity
# ---------------------------------------------------------------------------


class TestPublicSurface:
    """Sanity: the module exposes the names the task brief requires."""

    def test_module_exports_watch_and_stale_sweeper(self) -> None:
        from forge.adapters.nats import fleet_watcher

        assert callable(fleet_watcher.watch)
        assert callable(fleet_watcher.stale_sweeper)
        assert isinstance(fleet_watcher.FleetWatcher, type)

    def test_snapshot_reader_is_runtime_checkable(self) -> None:
        cache = DiscoveryCache()
        assert isinstance(cache, SnapshotReader)

    @pytest.mark.asyncio
    async def test_watch_subscribes_to_heartbeat_topic(self) -> None:
        sink = AsyncMock(spec=FleetEventSink)
        client = FakeNATSClient()

        task = asyncio.create_task(
            watch(client, sink, reconnect_backoff_seconds=0),
        )
        for _ in range(20):
            if client.watch_fleet_calls >= 1 and client.subscribe_calls:
                break
            await asyncio.sleep(0)
        try:
            topics = [t for (t, _cb) in client.subscribe_calls]
            assert Topics.Fleet.HEARTBEAT_ALL in topics
        finally:
            client.stop_watching()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

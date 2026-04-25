"""Unit tests for the ``forge.discovery`` domain package.

Tests cover every acceptance criterion in TASK-NFI-003:

* AC-LAYOUT — package layout and re-exports
* AC-NO-NATS-TRANSPORT — no nats.aio / NatsClient imports anywhere
  inside ``src/forge/discovery``
* AC-CLOCK / AC-CACHE-TTL — Clock protocol + FakeClock injection
* AC-FLEET-EVENT-SINK — FleetEventSink protocol surface
* AC-CACHE-LOCK — DiscoveryCache uses asyncio.Lock for mutations
* AC-RESOLVE-* — resolution algorithm (exact / intent / tie-break /
  degraded-exclusion / unresolved)
* AC-COVER — racing upsert/remove via ``asyncio.gather``

The tests build :class:`AgentManifest` instances locally; they do not
touch NATS at all. ``FakeClock`` is a deterministic
:class:`~forge.discovery.protocol.Clock` double.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from nats_core.events import AgentHeartbeatPayload
from nats_core.manifest import (
    AgentManifest,
    IntentCapability,
    ToolCapability,
)

from forge.discovery import (
    CapabilityResolution,
    Clock,
    DiscoveryCache,
    DiscoveryCacheEntry,
    FleetEventSink,
    SystemClock,
    resolve,
)


# ---------------------------------------------------------------------------
# Test doubles & helpers
# ---------------------------------------------------------------------------


class FakeClock:
    """Deterministic :class:`Clock` for boundary tests."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


def _tool(name: str) -> ToolCapability:
    return ToolCapability(
        name=name,
        description=f"{name} description",
        parameters={"type": "object", "properties": {}},
        returns="dict",
        risk_level="read_only",
    )


def _intent(pattern: str, confidence: float = 0.85) -> IntentCapability:
    return IntentCapability(
        pattern=pattern,
        signals=[pattern.split(".")[0]],
        confidence=confidence,
        description=f"intent {pattern}",
    )


def _manifest(
    agent_id: str,
    *,
    trust_tier: str = "specialist",
    tools: list[ToolCapability] | None = None,
    intents: list[IntentCapability] | None = None,
    max_concurrent: int = 4,
) -> AgentManifest:
    return AgentManifest(
        agent_id=agent_id,
        name=agent_id.title(),
        version="0.1.0",
        template="test-template",
        trust_tier=trust_tier,  # type: ignore[arg-type]
        status="ready",
        max_concurrent=max_concurrent,
        intents=intents or [],
        tools=tools or [],
        required_permissions=[],
    )


def _entry(
    manifest: AgentManifest,
    *,
    status: str = "ready",
    queue_depth: int = 0,
    cached_at: datetime | None = None,
    last_heartbeat_at: datetime | None = None,
) -> DiscoveryCacheEntry:
    ts = cached_at or datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
    return DiscoveryCacheEntry(
        manifest=manifest,
        last_heartbeat_at=last_heartbeat_at or ts,
        last_heartbeat_status=status,  # type: ignore[arg-type]
        last_queue_depth=queue_depth,
        last_active_tasks=0,
        cached_at=ts,
    )


# ---------------------------------------------------------------------------
# AC-LAYOUT — package layout & re-exports
# ---------------------------------------------------------------------------


class TestPackageLayout:
    """AC: ``src/forge/discovery/{__init__.py, cache.py, resolve.py, protocol.py, models.py}``."""

    def test_package_files_exist(self) -> None:
        pkg = Path(__file__).resolve().parents[3] / "src" / "forge" / "discovery"
        for name in ("__init__.py", "cache.py", "resolve.py", "protocol.py", "models.py"):
            assert (pkg / name).is_file(), f"missing {name}"

    def test_package_reexports_public_surface(self) -> None:
        from forge import discovery

        for name in (
            "DiscoveryCache",
            "DiscoveryCacheEntry",
            "CapabilityResolution",
            "Clock",
            "FleetEventSink",
            "SystemClock",
            "resolve",
        ):
            assert hasattr(discovery, name), f"missing public re-export: {name}"


# ---------------------------------------------------------------------------
# AC-NO-NATS-TRANSPORT — no NATS transport imports
# ---------------------------------------------------------------------------


class TestNoNatsTransportImports:
    """AC: ``grep -r "nats.aio\\|import nats\\|NatsClient" src/forge/discovery/``
    returns no hits."""

    def test_no_nats_transport_imports(self) -> None:
        pkg = Path(__file__).resolve().parents[3] / "src" / "forge" / "discovery"
        offending = re.compile(r"\bnats\.aio\b|^\s*import\s+nats\b|\bNatsClient\b")
        hits: list[str] = []
        for path in pkg.glob("*.py"):
            for lineno, line in enumerate(path.read_text().splitlines(), start=1):
                if offending.search(line):
                    hits.append(f"{path.name}:{lineno}: {line.strip()}")
        assert hits == [], f"forbidden NATS transport imports found: {hits}"


# ---------------------------------------------------------------------------
# AC-CLOCK & AC-FLEET-EVENT-SINK — Protocol surfaces
# ---------------------------------------------------------------------------


class TestClockProtocol:
    """AC: Clock protocol with single ``now() -> datetime``; SystemClock default."""

    def test_clock_has_now_method(self) -> None:
        # The protocol should declare exactly the ``now`` method.
        assert hasattr(Clock, "now")

    def test_system_clock_returns_utc_datetime(self) -> None:
        clk = SystemClock()
        result = clk.now()
        assert isinstance(result, datetime)
        assert result.tzinfo is not None
        # Per AC: default implementation reads ``datetime.now(UTC)``.
        assert result.utcoffset() == timedelta(0)

    def test_system_clock_satisfies_protocol(self) -> None:
        # ``Clock`` is runtime_checkable so isinstance is meaningful.
        assert isinstance(SystemClock(), Clock)
        assert isinstance(FakeClock(), Clock)


class TestFleetEventSinkProtocol:
    """AC: FleetEventSink with upsert_agent / remove_agent / update_heartbeat."""

    def test_protocol_has_three_methods(self) -> None:
        for name in ("upsert_agent", "remove_agent", "update_heartbeat"):
            assert hasattr(FleetEventSink, name), f"missing method: {name}"

    def test_discovery_cache_satisfies_fleet_event_sink(self) -> None:
        cache = DiscoveryCache()
        assert isinstance(cache, FleetEventSink)

    def test_method_signatures_match_consumer_seam(self) -> None:
        # Producer/consumer seam (TASK-NFI-005). Lock these names down.
        upsert = inspect.signature(FleetEventSink.upsert_agent)
        assert list(upsert.parameters.keys()) == ["self", "manifest"]

        remove = inspect.signature(FleetEventSink.remove_agent)
        assert list(remove.parameters.keys()) == ["self", "agent_id"]

        hb = inspect.signature(FleetEventSink.update_heartbeat)
        assert list(hb.parameters.keys()) == [
            "self",
            "agent_id",
            "hb",
            "status_changed",
        ]


# ---------------------------------------------------------------------------
# AC-CACHE-LOCK & AC-CACHE-TTL — DiscoveryCache mutation semantics
# ---------------------------------------------------------------------------


class TestDiscoveryCacheMutations:
    """AC: cache mutations guarded by asyncio.Lock; clock is injectable."""

    def test_cache_holds_asyncio_lock(self) -> None:
        cache = DiscoveryCache()
        # Implementation detail — but the AC explicitly calls out
        # ``asyncio.Lock`` and the consumer seam tests need to assume it.
        assert isinstance(cache._lock, asyncio.Lock)

    def test_cache_uses_injected_clock_for_cached_at(self) -> None:
        clk = FakeClock(datetime(2026, 4, 25, 9, 0, tzinfo=UTC))
        cache = DiscoveryCache(clock=clk)

        async def _scenario() -> DiscoveryCacheEntry:
            await cache.upsert_agent(_manifest("agent-a"))
            snap = await cache.snapshot()
            return snap["agent-a"]

        entry = asyncio.run(_scenario())
        # boundary check: ``cached_at`` reflects FakeClock, not real time.
        assert entry.cached_at == datetime(2026, 4, 25, 9, 0, tzinfo=UTC)

    def test_upsert_then_remove_clears_entry(self) -> None:
        cache = DiscoveryCache(clock=FakeClock())

        async def _scenario() -> dict[str, DiscoveryCacheEntry]:
            await cache.upsert_agent(_manifest("agent-a"))
            await cache.remove_agent("agent-a")
            return await cache.snapshot()

        snap = asyncio.run(_scenario())
        assert snap == {}

    def test_remove_unknown_agent_is_noop(self) -> None:
        cache = DiscoveryCache(clock=FakeClock())

        async def _scenario() -> None:
            await cache.remove_agent("never-registered")

        # Must not raise.
        asyncio.run(_scenario())

    def test_status_change_heartbeat_updates_status_and_queue(self) -> None:
        clk = FakeClock()
        cache = DiscoveryCache(clock=clk)

        async def _scenario() -> DiscoveryCacheEntry:
            await cache.upsert_agent(_manifest("agent-a"))
            clk.advance(5)
            hb = AgentHeartbeatPayload(
                agent_id="agent-a",
                status="degraded",
                queue_depth=3,
                active_tasks=1,
                uptime_seconds=42,
            )
            await cache.update_heartbeat("agent-a", hb, status_changed=True)
            snap = await cache.snapshot()
            return snap["agent-a"]

        entry = asyncio.run(_scenario())
        assert entry.last_heartbeat_status == "degraded"
        assert entry.last_queue_depth == 3
        assert entry.last_active_tasks == 1

    def test_routine_heartbeat_only_refreshes_timestamp(self) -> None:
        clk = FakeClock()
        cache = DiscoveryCache(clock=clk)

        async def _scenario() -> DiscoveryCacheEntry:
            await cache.upsert_agent(_manifest("agent-a"))
            clk.advance(10)
            hb = AgentHeartbeatPayload(
                agent_id="agent-a",
                status="ready",
                queue_depth=99,  # would change if status_changed=True
                active_tasks=99,
                uptime_seconds=42,
            )
            await cache.update_heartbeat("agent-a", hb, status_changed=False)
            snap = await cache.snapshot()
            return snap["agent-a"]

        entry = asyncio.run(_scenario())
        assert entry.last_heartbeat_status == "ready"
        # Routine heartbeats must NOT touch queue_depth (DM-discovery §4).
        assert entry.last_queue_depth == 0
        assert entry.last_active_tasks == 0
        # But ``last_heartbeat_at`` should advance with the FakeClock.
        assert entry.last_heartbeat_at == datetime(2026, 4, 25, 12, 0, 10, tzinfo=UTC)

    def test_heartbeat_for_unknown_agent_is_noop(self) -> None:
        cache = DiscoveryCache(clock=FakeClock())
        hb = AgentHeartbeatPayload(
            agent_id="ghost",
            status="ready",
            queue_depth=0,
            active_tasks=0,
            uptime_seconds=1,
        )

        async def _scenario() -> dict[str, DiscoveryCacheEntry]:
            await cache.update_heartbeat("ghost", hb, status_changed=True)
            return await cache.snapshot()

        snap = asyncio.run(_scenario())
        assert snap == {}


class TestRacingFleetEvents:
    """AC: covers racing upsert/remove via ``asyncio.gather``."""

    def test_concurrent_upsert_and_remove_does_not_corrupt(self) -> None:
        cache = DiscoveryCache(clock=FakeClock())

        async def _scenario() -> dict[str, DiscoveryCacheEntry]:
            # Hammer the same agent_id from many fibres simultaneously.
            ops = []
            for i in range(50):
                ops.append(cache.upsert_agent(_manifest("agent-a")))
                ops.append(cache.remove_agent("agent-a"))
                ops.append(cache.upsert_agent(_manifest("agent-b")))
            await asyncio.gather(*ops)
            return await cache.snapshot()

        snap = asyncio.run(_scenario())
        # agent-b was only ever upserted, never removed → must remain.
        assert "agent-b" in snap
        # agent-a's final state depends on scheduling, but the cache
        # must be internally consistent (no exceptions, no half-entries).
        for entry in snap.values():
            assert isinstance(entry, DiscoveryCacheEntry)


# ---------------------------------------------------------------------------
# AC-RESOLVE — resolution algorithm
# ---------------------------------------------------------------------------


class TestResolveExactToolMatch:
    """AC: tool-exact path returns single matching agent."""

    def test_single_exact_match_returns_agent(self) -> None:
        snapshot = {
            "agent-a": _entry(
                _manifest("agent-a", tools=[_tool("forge_status")]),
            ),
        }
        agent_id, resolution = resolve(snapshot, tool_name="forge_status")
        assert agent_id == "agent-a"
        assert resolution.match_source == "tool_exact"
        assert resolution.matched_agent_id == "agent-a"
        assert resolution.competing_agents == []
        assert resolution.chosen_confidence == 1.0

    def test_no_intent_required_when_tool_matches(self) -> None:
        snapshot = {
            "agent-a": _entry(
                _manifest("agent-a", tools=[_tool("forge_status")]),
            ),
        }
        agent_id, resolution = resolve(
            snapshot, tool_name="forge_status", intent_pattern="something.unrelated",
        )
        assert agent_id == "agent-a"
        assert resolution.match_source == "tool_exact"


class TestResolveIntentFallback:
    """AC: intent fallback when no tool match."""

    def test_intent_fallback_used_when_tool_misses(self) -> None:
        snapshot = {
            "planner": _entry(
                _manifest(
                    "planner",
                    intents=[_intent("plan.*", confidence=0.9)],
                ),
            ),
        }
        agent_id, resolution = resolve(
            snapshot,
            tool_name="nonexistent_tool",
            intent_pattern="plan.feature",
        )
        assert agent_id == "planner"
        assert resolution.match_source == "intent_pattern"
        assert resolution.chosen_confidence == 0.9

    def test_intent_below_min_confidence_excluded(self) -> None:
        snapshot = {
            "planner": _entry(
                _manifest(
                    "planner",
                    intents=[_intent("plan.*", confidence=0.5)],
                ),
            ),
        }
        agent_id, resolution = resolve(
            snapshot,
            tool_name="nonexistent_tool",
            intent_pattern="plan.feature",
            min_confidence=0.7,
        )
        assert agent_id is None
        assert resolution.match_source == "unresolved"


class TestResolveTieBreak:
    """AC: tie-break by trust_tier, then confidence, then queue_depth."""

    def test_tie_break_prefers_core_over_specialist(self) -> None:
        snapshot = {
            "core-a": _entry(
                _manifest(
                    "core-a",
                    trust_tier="core",
                    tools=[_tool("shared_tool")],
                ),
            ),
            "spec-a": _entry(
                _manifest(
                    "spec-a",
                    trust_tier="specialist",
                    tools=[_tool("shared_tool")],
                ),
            ),
        }
        agent_id, resolution = resolve(snapshot, tool_name="shared_tool")
        assert agent_id == "core-a"
        assert resolution.competing_agents == ["spec-a"]
        assert resolution.chosen_trust_tier == "core"

    def test_tie_break_prefers_specialist_over_extension(self) -> None:
        snapshot = {
            "spec-a": _entry(
                _manifest(
                    "spec-a",
                    trust_tier="specialist",
                    tools=[_tool("shared_tool")],
                ),
            ),
            "ext-a": _entry(
                _manifest(
                    "ext-a",
                    trust_tier="extension",
                    tools=[_tool("shared_tool")],
                ),
            ),
        }
        agent_id, _ = resolve(snapshot, tool_name="shared_tool")
        assert agent_id == "spec-a"

    def test_tie_break_by_queue_depth_when_tier_equal(self) -> None:
        snapshot = {
            "loaded": _entry(
                _manifest(
                    "loaded",
                    trust_tier="specialist",
                    tools=[_tool("shared_tool")],
                ),
                queue_depth=10,
            ),
            "idle": _entry(
                _manifest(
                    "idle",
                    trust_tier="specialist",
                    tools=[_tool("shared_tool")],
                ),
                queue_depth=0,
            ),
        }
        agent_id, resolution = resolve(snapshot, tool_name="shared_tool")
        assert agent_id == "idle"
        assert resolution.chosen_queue_depth == 0
        assert resolution.competing_agents == ["loaded"]

    def test_tie_break_by_confidence_when_intent_path(self) -> None:
        snapshot = {
            "low-conf": _entry(
                _manifest(
                    "low-conf",
                    trust_tier="specialist",
                    intents=[_intent("plan.*", confidence=0.75)],
                ),
            ),
            "high-conf": _entry(
                _manifest(
                    "high-conf",
                    trust_tier="specialist",
                    intents=[_intent("plan.*", confidence=0.95)],
                ),
            ),
        }
        agent_id, resolution = resolve(
            snapshot, tool_name="missing", intent_pattern="plan.feature",
        )
        assert agent_id == "high-conf"
        assert resolution.chosen_confidence == 0.95


class TestResolveStaleAndDegradedExclusion:
    """AC: degraded agents excluded from primary resolution."""

    def test_degraded_agent_excluded_from_tool_match(self) -> None:
        snapshot = {
            "degraded": _entry(
                _manifest("degraded", tools=[_tool("forge_status")]),
                status="degraded",
            ),
            "healthy": _entry(
                _manifest("healthy", tools=[_tool("forge_status")]),
                status="ready",
            ),
        }
        agent_id, _ = resolve(snapshot, tool_name="forge_status")
        assert agent_id == "healthy"

    def test_only_degraded_means_unresolved(self) -> None:
        snapshot = {
            "degraded": _entry(
                _manifest("degraded", tools=[_tool("forge_status")]),
                status="degraded",
            ),
        }
        agent_id, resolution = resolve(snapshot, tool_name="forge_status")
        assert agent_id is None
        assert resolution.match_source == "unresolved"

    def test_degraded_agent_excluded_from_intent_match(self) -> None:
        snapshot = {
            "degraded": _entry(
                _manifest(
                    "degraded",
                    intents=[_intent("plan.*", confidence=0.9)],
                ),
                status="degraded",
            ),
        }
        agent_id, resolution = resolve(
            snapshot,
            tool_name="missing",
            intent_pattern="plan.feature",
        )
        assert agent_id is None
        assert resolution.match_source == "unresolved"


class TestResolveUnresolved:
    """AC: returns ``(None, CapabilityResolution(unresolved))`` on miss."""

    def test_empty_snapshot_returns_unresolved(self) -> None:
        agent_id, resolution = resolve({}, tool_name="forge_status")
        assert agent_id is None
        assert resolution.match_source == "unresolved"
        assert resolution.matched_agent_id is None
        assert resolution.requested_tool == "forge_status"
        assert resolution.competing_agents == []

    def test_unresolved_resolution_records_intent_in_request(self) -> None:
        _, resolution = resolve(
            {}, tool_name="missing", intent_pattern="plan.feature",
        )
        assert resolution.requested_intent == "plan.feature"

    def test_resolved_at_uses_now_override(self) -> None:
        when = datetime(2026, 4, 25, 8, 30, tzinfo=UTC)
        _, resolution = resolve({}, tool_name="missing", now=when)
        assert resolution.resolved_at == when


class TestCapabilityResolutionInvariants:
    """AC: pydantic invariants on CapabilityResolution."""

    def test_unresolved_must_have_no_agent(self) -> None:
        with pytest.raises(ValueError, match="match_source='unresolved'"):
            CapabilityResolution(
                resolution_id="r1",
                build_id="b1",
                stage_label="s1",
                requested_tool="t",
                match_source="unresolved",
                matched_agent_id="oops",
                resolved_at=datetime.now(UTC),
            )

    def test_resolved_must_have_agent(self) -> None:
        with pytest.raises(ValueError, match="requires matched_agent_id"):
            CapabilityResolution(
                resolution_id="r1",
                build_id="b1",
                stage_label="s1",
                requested_tool="t",
                match_source="tool_exact",
                matched_agent_id=None,
                resolved_at=datetime.now(UTC),
            )

    def test_competing_agents_excludes_chosen(self) -> None:
        with pytest.raises(ValueError, match="competing_agents"):
            CapabilityResolution(
                resolution_id="r1",
                build_id="b1",
                stage_label="s1",
                requested_tool="t",
                match_source="tool_exact",
                matched_agent_id="agent-a",
                competing_agents=["agent-a", "agent-b"],
                resolved_at=datetime.now(UTC),
            )


class TestResolveTrustTierRanking:
    """AC: ``core(0) > specialist(1) > extension(2)``."""

    def test_full_ordering(self) -> None:
        snapshot = {
            "ext": _entry(
                _manifest(
                    "ext", trust_tier="extension", tools=[_tool("shared")],
                ),
            ),
            "spec": _entry(
                _manifest(
                    "spec", trust_tier="specialist", tools=[_tool("shared")],
                ),
            ),
            "core": _entry(
                _manifest(
                    "core", trust_tier="core", tools=[_tool("shared")],
                ),
            ),
        }
        agent_id, resolution = resolve(snapshot, tool_name="shared")
        assert agent_id == "core"
        # competing_agents should be ordered specialist → extension.
        assert resolution.competing_agents == ["spec", "ext"]

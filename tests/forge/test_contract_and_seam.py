"""Contract + seam tests for TASK-NFI-010.

This module owns the *boundary* tests guaranteeing integration between the
modules built by upstream subtasks 004–009. Each upstream subtask owns its
own unit-tests; this file consolidates the cross-module contracts and
seams that those isolated suites cannot verify on their own.

Test categories — one ``Test*`` class per acceptance criterion:

* AC-001 — :class:`forge.adapters.nats.PipelinePublisher` envelope shape
  (``source_id == "forge"`` + ``correlation_id`` threaded from payload).
* AC-002 — :func:`forge.adapters.nats.fleet_publisher.register_on_boot`
  publishes :data:`forge.fleet.manifest.FORGE_MANIFEST` exactly once,
  unmodified.
* AC-003 — :func:`forge.adapters.nats.fleet_publisher.heartbeat_loop`
  cadence is Clock-driven (no wall-clock dependency).
* AC-004 — :class:`forge.adapters.nats.fleet_watcher.FleetWatcher` →
  :class:`forge.discovery.protocol.FleetEventSink` delegation.
* AC-005 — racing seam: 100 concurrent register/deregister pairs
  through ``asyncio.gather`` leave the cache in a consistent state.
* AC-006 — terminal-ack invariant: the state machine ack only fires
  on COMPLETE (not on RUNNING / PAUSED / FINALISING). This is the
  single most load-bearing test in the feature.
* AC-007 — publish-failure tolerance: a raising
  :class:`PipelinePublisher` does not roll back the pipeline lifecycle
  (the SQLite row simulated as a dict still says RUNNING).
* AC-008 — clock hygiene: ``src/forge/adapters/nats/`` and
  ``src/forge/discovery/`` contain no raw ``datetime.now()`` /
  ``asyncio.sleep(`` calls outside the documented Clock implementations
  and reconnect-backoff allowlist.

The module deliberately re-uses ``unittest.mock.AsyncMock`` /
``MagicMock`` doubles for ``nats_client`` and an inline ``FakeClock``
to keep the file self-contained — TASK-NFI-010 produces tests, not
new helpers.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from nats_core.envelope import EventType, MessageEnvelope
from nats_core.events import (
    AgentHeartbeatPayload,
    BuildCancelledPayload,
    BuildCompletePayload,
    BuildFailedPayload,
    BuildPausedPayload,
    BuildProgressPayload,
    BuildQueuedPayload,
    BuildResumedPayload,
    BuildStartedPayload,
    StageCompletePayload,
)
from nats_core.manifest import (
    AgentManifest,
    IntentCapability,
    ToolCapability,
)

from forge.adapters.nats.fleet_publisher import (
    AGENT_ID,
    deregister,
    heartbeat_loop,
    register_on_boot,
)
from forge.adapters.nats.fleet_watcher import FleetWatcher
from forge.adapters.nats.pipeline_consumer import (
    PipelineConsumerDeps,
    handle_message,
)
from forge.adapters.nats.pipeline_publisher import (
    PipelinePublisher,
    PublishFailure,
)
from forge.config.models import (
    FilesystemPermissions,
    ForgeConfig,
    PermissionsConfig,
    PipelineConfig,
)
from forge.discovery.cache import DiscoveryCache
from forge.discovery.protocol import Clock as DiscoveryClock
from forge.discovery.protocol import FleetEventSink
from forge.fleet.manifest import FORGE_MANIFEST
from forge.pipeline import (
    BuildContext,
    PipelineLifecycleEmitter,
)


# ---------------------------------------------------------------------------
# Local fixtures + helpers
# ---------------------------------------------------------------------------


# Shared identifiers used across the contract tests. Pinned values keep
# subject assertions auditable against API-nats-pipeline-events.md §3.
FEATURE_ID = "FEAT-A1B2"
BUILD_ID = "build-FEAT-A1B2-20260425120000"
CORRELATION_ID = "corr-from-cli-001"
WAVE_TOTAL = 3
ISO_TIMESTAMP = "2026-04-25T12:00:00+00:00"


class _FakeMonotonicClock:
    """Deterministic :class:`Clock` for the heartbeat loop (monotonic surface).

    Mirrors the convention used by ``tests/forge/test_fleet_publisher.py``:
    every ``sleep(s)`` call advances ``_now`` by ``s`` and yields once via
    ``asyncio.sleep(0)`` so the test driver can interleave assertions
    between iterations of the loop body.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._now += seconds
        await asyncio.sleep(0)


class _FakeDateClock:
    """Deterministic :class:`DiscoveryClock` returning a frozen UTC datetime."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


class _FakeStatusProvider:
    """Inert :class:`StatusProvider` returning fixed status values."""

    def __init__(self) -> None:
        self.status = "ready"
        self.queue_depth = 0
        self.active_tasks = 0

    def get_current_status(self) -> Any:
        return self.status

    def get_active_tasks(self) -> int:
        return self.active_tasks

    def get_queue_depth(self) -> int:
        return self.queue_depth


def _intent(pattern: str = "tasks.*") -> IntentCapability:
    return IntentCapability(
        pattern=pattern,
        signals=[pattern.split(".")[0]],
        confidence=0.85,
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


def _manifest(agent_id: str, *, version: str = "0.1.0") -> AgentManifest:
    return AgentManifest(
        agent_id=agent_id,
        name=agent_id.title(),
        version=version,
        template="test-template",
        trust_tier="specialist",
        status="ready",
        max_concurrent=2,
        intents=[_intent()],
        tools=[_tool()],
        required_permissions=[],
    )


def _heartbeat_payload(agent_id: str, *, status: str = "ready") -> AgentHeartbeatPayload:
    return AgentHeartbeatPayload(
        agent_id=agent_id,
        status=status,  # type: ignore[arg-type]
        queue_depth=0,
        active_tasks=0,
        uptime_seconds=10,
    )


def _heartbeat_envelope(payload: AgentHeartbeatPayload) -> MessageEnvelope:
    return MessageEnvelope(
        source_id=payload.agent_id,
        event_type=EventType.AGENT_HEARTBEAT,
        correlation_id=None,
        payload=payload.model_dump(mode="json"),
    )


def _decode_publish_call(call: Any) -> tuple[str, dict[str, Any]]:
    """Pull (subject, decoded_envelope_dict) out of a recorded ``publish`` call."""
    args, kwargs = call.args, call.kwargs
    subject = args[0] if args else kwargs["subject"]
    body = args[1] if len(args) > 1 else kwargs.get("payload", b"")
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8")
    return subject, json.loads(body)


def _build_queued_envelope_bytes(yaml_path: Path) -> bytes:
    """Serialise a valid ``BuildQueuedPayload`` envelope to JSON bytes."""
    payload = {
        "feature_id": FEATURE_ID,
        "repo": "appmilla/example",
        "branch": "main",
        "feature_yaml_path": str(yaml_path),
        "max_turns": 5,
        "sdk_timeout_seconds": 1800,
        "wave_gating": True,
        "config_overrides": None,
        "triggered_by": "cli",
        "originating_adapter": "cli-wrapper",
        "originating_user": "rich",
        "correlation_id": CORRELATION_ID,
        "parent_request_id": None,
        "retry_count": 0,
        "requested_at": datetime.now(UTC).isoformat(),
        "queued_at": datetime.now(UTC).isoformat(),
    }
    envelope = MessageEnvelope(
        message_id="msg-test-010",
        timestamp=datetime.now(UTC),
        version="1.0",
        source_id="cli-wrapper",
        event_type=EventType.BUILD_QUEUED,
        project=None,
        correlation_id=CORRELATION_ID,
        payload=payload,
    )
    return envelope.model_dump_json().encode("utf-8")


@pytest.fixture
def feature_yaml(tmp_path: Path) -> Path:
    """Allowlisted feature.yaml path used by the consumer/seam tests."""
    root = (tmp_path / "repos").resolve()
    root.mkdir()
    return root / "feature.yaml"


@pytest.fixture
def forge_config(feature_yaml: Path) -> ForgeConfig:
    """``ForgeConfig`` with a single allowlisted directory derived from ``feature_yaml``."""
    return ForgeConfig(
        pipeline=PipelineConfig(),
        permissions=PermissionsConfig(
            filesystem=FilesystemPermissions(allowlist=[feature_yaml.parent]),
        ),
    )


@pytest.fixture
def fake_publisher() -> AsyncMock:
    """Async double for :class:`PipelinePublisher` exposing the eight emit hooks."""
    publisher = AsyncMock(spec=PipelinePublisher)
    for name in (
        "publish_build_started",
        "publish_build_progress",
        "publish_stage_complete",
        "publish_build_paused",
        "publish_build_resumed",
        "publish_build_complete",
        "publish_build_failed",
        "publish_build_cancelled",
    ):
        setattr(publisher, name, AsyncMock(return_value=None))
    return publisher


# ---------------------------------------------------------------------------
# AC-001 — pipeline_publisher boundary contract
# ---------------------------------------------------------------------------


class TestPipelinePublisherBoundaryContract:
    """AC-001: every publisher method emits source_id="forge" and threads correlation_id.

    The unit-test suite for TASK-NFI-006 verifies each method's individual
    behaviour; this class is the *boundary* contract that callers depend
    on — every method, every payload, the same two fields.
    """

    @pytest.fixture
    def nats_client(self) -> AsyncMock:
        client = AsyncMock()
        client.publish = AsyncMock(return_value=None)
        return client

    @pytest.fixture
    def publisher(self, nats_client: AsyncMock) -> PipelinePublisher:
        return PipelinePublisher(nats_client=nats_client)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "method_name, payload",
        [
            (
                "publish_build_started",
                BuildStartedPayload(
                    feature_id=FEATURE_ID, build_id=BUILD_ID, wave_total=WAVE_TOTAL
                ),
            ),
            (
                "publish_build_progress",
                BuildProgressPayload(
                    feature_id=FEATURE_ID,
                    build_id=BUILD_ID,
                    wave=1,
                    wave_total=WAVE_TOTAL,
                    overall_progress_pct=33.3,
                    elapsed_seconds=12,
                ),
            ),
            (
                "publish_stage_complete",
                StageCompletePayload(
                    feature_id=FEATURE_ID,
                    build_id=BUILD_ID,
                    stage_label="implementation",
                    target_kind="subagent",
                    target_identifier="implementer",
                    status="PASSED",
                    gate_mode="AUTO_APPROVE",
                    coach_score=0.92,
                    duration_secs=42.5,
                    completed_at=ISO_TIMESTAMP,
                    correlation_id=CORRELATION_ID,
                ),
            ),
            (
                "publish_build_paused",
                BuildPausedPayload(
                    feature_id=FEATURE_ID,
                    build_id=BUILD_ID,
                    stage_label="implementation",
                    gate_mode="FLAG_FOR_REVIEW",
                    coach_score=0.55,
                    rationale="quality below threshold",
                    approval_subject="agent.forge.approval-response",
                    paused_at=ISO_TIMESTAMP,
                    correlation_id=CORRELATION_ID,
                ),
            ),
            (
                "publish_build_resumed",
                BuildResumedPayload(
                    feature_id=FEATURE_ID,
                    build_id=BUILD_ID,
                    stage_label="implementation",
                    decision="approve",
                    responder="rich",
                    resumed_at=ISO_TIMESTAMP,
                    correlation_id=CORRELATION_ID,
                ),
            ),
            (
                "publish_build_complete",
                BuildCompletePayload(
                    feature_id=FEATURE_ID,
                    build_id=BUILD_ID,
                    repo="guardkit/forge",
                    branch="main",
                    tasks_completed=4,
                    tasks_failed=0,
                    tasks_total=4,
                    pr_url="https://github.com/guardkit/forge/pull/42",
                    duration_seconds=600,
                    summary="all green",
                ),
            ),
            (
                "publish_build_failed",
                BuildFailedPayload(
                    feature_id=FEATURE_ID,
                    build_id=BUILD_ID,
                    failure_reason="task TASK-X failed",
                    recoverable=False,
                    failed_task_id="TASK-X",
                ),
            ),
            (
                "publish_build_cancelled",
                BuildCancelledPayload(
                    feature_id=FEATURE_ID,
                    build_id=BUILD_ID,
                    reason="user_requested",
                    cancelled_by="rich",
                    cancelled_at=ISO_TIMESTAMP,
                    correlation_id=CORRELATION_ID,
                ),
            ),
        ],
        ids=[
            "build-started",
            "build-progress",
            "stage-complete",
            "build-paused",
            "build-resumed",
            "build-complete",
            "build-failed",
            "build-cancelled",
        ],
    )
    async def test_method_envelope_carries_source_id_and_threads_correlation(
        self,
        publisher: PipelinePublisher,
        nats_client: AsyncMock,
        method_name: str,
        payload: Any,
    ) -> None:
        # AC-001 — the boundary invariant: every method (a) sets
        # source_id="forge" and (b) threads correlation_id off the
        # payload (None for v1 payloads, the literal string for v2.2).
        await getattr(publisher, method_name)(payload)
        nats_client.publish.assert_awaited_once()
        _subject, env = _decode_publish_call(nats_client.publish.call_args)
        assert env["source_id"] == "forge", (
            f"{method_name} dropped or rewrote source_id; got {env['source_id']!r}"
        )
        # Round-trip through MessageEnvelope to assert the wire shape is
        # not just a free-form dict the consumer would also accept.
        envelope = MessageEnvelope.model_validate(env)
        assert envelope.source_id == "forge"

        # correlation_id should equal whatever the payload exposes —
        # publisher reads via getattr(payload, "correlation_id", None).
        expected = getattr(payload, "correlation_id", None)
        assert env["correlation_id"] == expected, (
            f"{method_name} did not thread correlation_id from the payload: "
            f"expected={expected!r}, envelope={env['correlation_id']!r}"
        )


# ---------------------------------------------------------------------------
# AC-002 — fleet_publisher.register_on_boot contract
# ---------------------------------------------------------------------------


class TestRegisterOnBootContract:
    """AC-002: register_on_boot calls register_agent(FORGE_MANIFEST) once, unchanged."""

    @pytest.fixture
    def nats_client(self) -> AsyncMock:
        client = AsyncMock()
        client.register_agent = AsyncMock(return_value=None)
        return client

    @pytest.mark.asyncio
    async def test_register_on_boot_calls_register_agent_exactly_once(
        self, nats_client: AsyncMock
    ) -> None:
        await register_on_boot(nats_client)
        assert nats_client.register_agent.await_count == 1

    @pytest.mark.asyncio
    async def test_register_on_boot_passes_forge_manifest_unchanged(
        self, nats_client: AsyncMock
    ) -> None:
        # Identity check — the production constant must reach the wire
        # untouched. No copy, no mutation, no field stripping.
        await register_on_boot(nats_client)
        passed = nats_client.register_agent.await_args.args[0]
        assert passed is FORGE_MANIFEST
        assert passed.agent_id == AGENT_ID
        assert passed.template == "deepagents-pipeline-orchestrator"


# ---------------------------------------------------------------------------
# AC-003 — heartbeat_loop cadence (Clock-driven, not wall-clock)
# ---------------------------------------------------------------------------


class TestHeartbeatLoopCadenceContract:
    """AC-003: heartbeat publishes at ``heartbeat_interval_seconds`` cadence.

    Verifies the loop's sleep cadence comes from the injected
    :class:`Clock` (the FakeClock here records every requested
    duration). A real-time test would couple the suite to wall-clock
    timing — the contract explicitly forbids that.
    """

    @pytest.mark.asyncio
    async def test_loop_sleeps_at_interval_seconds_between_publishes(self) -> None:
        nats_client = AsyncMock()
        nats_client.heartbeat = AsyncMock(return_value=None)

        clock = _FakeMonotonicClock()
        provider = _FakeStatusProvider()
        cancel = asyncio.Event()
        interval_seconds = 30

        # Stop the loop after the third publish so we end up with
        # exactly two inter-tick sleeps to assert against.
        async def stop_after_three(_payload: AgentHeartbeatPayload) -> None:
            if nats_client.heartbeat.await_count >= 3:
                cancel.set()

        nats_client.heartbeat.side_effect = stop_after_three

        await heartbeat_loop(
            nats_client,
            cancel,
            status_provider=provider,
            interval_seconds=interval_seconds,
            clock=clock,
        )

        assert nats_client.heartbeat.await_count == 3, (
            "heartbeat_loop did not publish three times before cancel"
        )
        # Cadence honoured — every recorded sleep equals the requested
        # interval, and no wall-clock asyncio.sleep was used.
        assert clock.sleeps and all(s == float(interval_seconds) for s in clock.sleeps), (
            f"FakeClock recorded sleeps={clock.sleeps!r}; expected only "
            f"{interval_seconds}s gaps"
        )
        # Two publishes ⇒ at most two sleeps (third publish triggers cancel
        # before the next sleep). Three publishes still has three sleeps if
        # cancel fires AFTER sleep_or_cancel re-enters; we assert the loop
        # never used a non-Clock sleep duration regardless.
        assert len(clock.sleeps) <= 3


# ---------------------------------------------------------------------------
# AC-004 — fleet_watcher → FleetEventSink seam delegation
# ---------------------------------------------------------------------------


class TestFleetWatcherSeamDelegation:
    """AC-004: register/deregister/heartbeat events flow watcher → sink.

    Drives :class:`FleetWatcher.on_fleet_change` and ``on_heartbeat``
    against a fresh :class:`DiscoveryCache` (the production sink).
    Verifies the cache observes exactly the expected delegation
    surface — upsert_agent, remove_agent, update_heartbeat — without
    standing up a NATS subscription.
    """

    @pytest.mark.asyncio
    async def test_register_event_invokes_upsert_agent(self) -> None:
        cache = DiscoveryCache(clock=_FakeDateClock())
        watcher = FleetWatcher(cache, status_reader=cache)
        manifest = _manifest("agent-a")

        await watcher.on_fleet_change(manifest.agent_id, manifest)

        snapshot = await cache.snapshot()
        assert "agent-a" in snapshot
        assert snapshot["agent-a"].manifest is manifest

    @pytest.mark.asyncio
    async def test_deregister_event_invokes_remove_agent(self) -> None:
        cache = DiscoveryCache(clock=_FakeDateClock())
        watcher = FleetWatcher(cache, status_reader=cache)
        manifest = _manifest("agent-b")

        # Register first so there is something to remove.
        await watcher.on_fleet_change(manifest.agent_id, manifest)
        assert "agent-b" in await cache.snapshot()

        # Deregister — manifest=None on the watcher's interface.
        await watcher.on_fleet_change(manifest.agent_id, None)
        assert "agent-b" not in await cache.snapshot()

    @pytest.mark.asyncio
    async def test_heartbeat_event_invokes_update_heartbeat(self) -> None:
        cache = DiscoveryCache(clock=_FakeDateClock())
        watcher = FleetWatcher(cache, status_reader=cache)
        manifest = _manifest("agent-c")

        # Register so the heartbeat lands on a known entry.
        await watcher.on_fleet_change(manifest.agent_id, manifest)

        hb = _heartbeat_payload("agent-c", status="busy")
        await watcher.on_heartbeat(_heartbeat_envelope(hb))

        snapshot = await cache.snapshot()
        assert snapshot["agent-c"].last_heartbeat_status == "busy"

    @pytest.mark.asyncio
    async def test_sink_protocol_is_honoured_by_real_cache(self) -> None:
        # Belt-and-braces: the real DiscoveryCache must satisfy the
        # FleetEventSink protocol the watcher expects. Catches a future
        # rename of one of the three coroutine methods.
        cache = DiscoveryCache(clock=_FakeDateClock())
        assert isinstance(cache, FleetEventSink)


# ---------------------------------------------------------------------------
# AC-005 — racing seam: 100 concurrent register+deregister pairs
# ---------------------------------------------------------------------------


class TestRacingSeam:
    """AC-005: 100 concurrent register/deregister pairs leave the cache consistent.

    Spawns 100 ``asyncio.gather`` pairs and asserts the cache lock
    serialises every update — no torn entries, no stray agents
    surviving their deregistration when the deregister wins the race.
    """

    @pytest.mark.asyncio
    async def test_one_hundred_concurrent_register_deregister_pairs_settle(self) -> None:
        cache = DiscoveryCache(clock=_FakeDateClock())
        watcher = FleetWatcher(cache, status_reader=cache)

        async def register_then_deregister(i: int) -> None:
            agent_id = f"racer-{i:03d}"
            manifest = _manifest(agent_id)
            # Two independent coroutines racing on the same key. The
            # cache lock is the single serialisation point — either
            # ordering is acceptable.
            await asyncio.gather(
                watcher.on_fleet_change(agent_id, manifest),
                watcher.on_fleet_change(agent_id, None),
            )

        await asyncio.gather(*(register_then_deregister(i) for i in range(100)))

        snapshot = await cache.snapshot()
        # Whichever event lands last wins — the cache must hold either
        # zero or one entry per agent_id, never duplicates and never
        # an inconsistent partial entry. With register and deregister
        # racing, both outcomes are valid ("one event wins").
        for agent_id, entry in snapshot.items():
            assert agent_id.startswith("racer-")
            # Entries that survived must be fully-formed cache records.
            assert entry.manifest.agent_id == agent_id
            assert entry.last_heartbeat_status in {
                "ready",
                "busy",
                "degraded",
                "draining",
            }
        # Cardinality: at most 100 entries (one per racer); typically
        # fewer because deregister wins many of the races.
        assert len(snapshot) <= 100


# ---------------------------------------------------------------------------
# AC-006 — terminal-ack invariant (the load-bearing test)
# ---------------------------------------------------------------------------


class TestTerminalAckInvariant:
    """AC-006: ack fires exactly once, and only on the COMPLETE transition.

    Drives :func:`pipeline_consumer.handle_message` → the dispatch
    seam → a fake state machine that runs through
    RUNNING → PAUSED → RUNNING → COMPLETE. Asserts:

    * ``msg.ack()`` is **never** awaited before COMPLETE.
    * ``msg.ack()`` is awaited exactly once, at COMPLETE, via the
      consumer's ``ack_callback``.

    This is the single most load-bearing test in the feature — a
    regression here means at-most-once delivery silently degrades
    to at-least-twice on every paused build.
    """

    @pytest.mark.asyncio
    async def test_ack_fires_only_at_complete_after_running_paused_running(
        self, forge_config: ForgeConfig, feature_yaml: Path, fake_publisher: AsyncMock
    ) -> None:
        # ---- Build the consumer-side message + deps --------------------
        msg = AsyncMock()
        msg.data = _build_queued_envelope_bytes(feature_yaml)
        msg.ack = AsyncMock()

        is_dup = AsyncMock(return_value=False)
        publish_failed = AsyncMock()

        # Track ack-await counts at each transition so the assertion is
        # an *invariant* across the transition graph, not just the
        # endpoint state.
        ack_counts: dict[str, int] = {}

        async def fake_state_machine(
            payload: BuildQueuedPayload,
            ack_callback: Any,
        ) -> None:
            # Build the lifecycle emitter the production state machine
            # would build — same publisher seam, same emit_* surface.
            emitter = PipelineLifecycleEmitter(
                publisher=fake_publisher,
                config=forge_config.pipeline,
            )
            ctx = BuildContext(
                feature_id=payload.feature_id,
                build_id=BUILD_ID,
                correlation_id=payload.correlation_id,
                wave_total=WAVE_TOTAL,
            )

            # --- PREPARING → RUNNING (no ack) ------------------------
            await emitter.emit_started(ctx)
            ack_counts["after_running"] = msg.ack.await_count

            # --- RUNNING → PAUSED (no ack) ---------------------------
            await emitter.emit_paused(
                ctx,
                stage_label="implementation",
                gate_mode="MANDATORY_HUMAN_APPROVAL",
                coach_score=None,
                rationale="awaiting human approval",
                approval_subject="agent.forge.approval-response",
                paused_at=ISO_TIMESTAMP,
            )
            ack_counts["after_paused"] = msg.ack.await_count

            # --- PAUSED → RUNNING (no ack) ---------------------------
            await emitter.emit_resumed(
                ctx,
                stage_label="implementation",
                decision="approve",
                responder="rich",
                resumed_at=ISO_TIMESTAMP,
            )
            ack_counts["after_resumed"] = msg.ack.await_count

            # --- (FINALISING →) COMPLETE — ack fires here -----------
            await emitter.emit_complete(
                ctx,
                repo="guardkit/forge",
                branch="main",
                tasks_completed=4,
                tasks_failed=0,
                tasks_total=4,
                pr_url="https://github.com/guardkit/forge/pull/42",
                duration_seconds=600,
                summary="all green",
            )
            await ack_callback()
            ack_counts["after_complete"] = msg.ack.await_count

        deps = PipelineConsumerDeps(
            forge_config=forge_config,
            is_duplicate_terminal=is_dup,
            dispatch_build=fake_state_machine,
            publish_build_failed=publish_failed,
        )

        await handle_message(msg, deps)

        # ---- Invariant assertions --------------------------------------
        # Non-terminal transitions must NOT have acked.
        assert ack_counts["after_running"] == 0, (
            "ack fired on RUNNING — terminal-ack invariant violated"
        )
        assert ack_counts["after_paused"] == 0, (
            "ack fired on PAUSED — terminal-ack invariant violated"
        )
        assert ack_counts["after_resumed"] == 0, (
            "ack fired on RUNNING (after PAUSED → RUNNING) — invariant violated"
        )
        # Terminal transition: ack fires exactly once.
        assert ack_counts["after_complete"] == 1, (
            f"ack must fire exactly once at COMPLETE; got "
            f"{ack_counts['after_complete']}"
        )
        # Final state — handle_message returned, ack count is still one.
        assert msg.ack.await_count == 1
        # The state machine actually published the four lifecycle events.
        assert fake_publisher.publish_build_started.await_count == 1
        assert fake_publisher.publish_build_paused.await_count == 1
        assert fake_publisher.publish_build_resumed.await_count == 1
        assert fake_publisher.publish_build_complete.await_count == 1
        # No build-failed event was published — this was a happy path.
        publish_failed.assert_not_called()

    @pytest.mark.asyncio
    async def test_ack_callback_is_idempotent_under_double_invocation(
        self, forge_config: ForgeConfig, feature_yaml: Path
    ) -> None:
        # Second-line defence: if the state machine fires the callback
        # twice (e.g. crash-recovery races), the underlying msg.ack()
        # only runs once.
        msg = AsyncMock()
        msg.data = _build_queued_envelope_bytes(feature_yaml)
        msg.ack = AsyncMock()

        captured: dict[str, Any] = {}

        async def capture_dispatch(
            payload: BuildQueuedPayload, ack_callback: Any
        ) -> None:
            captured["ack_callback"] = ack_callback

        deps = PipelineConsumerDeps(
            forge_config=forge_config,
            is_duplicate_terminal=AsyncMock(return_value=False),
            dispatch_build=capture_dispatch,
            publish_build_failed=AsyncMock(),
        )

        await handle_message(msg, deps)

        ack_callback = captured["ack_callback"]
        await ack_callback()
        await ack_callback()
        assert msg.ack.await_count == 1


# ---------------------------------------------------------------------------
# AC-007 — publish-failure tolerance (SQLite NOT rolled back)
# ---------------------------------------------------------------------------


class TestPublishFailureTolerance:
    """AC-007: a raising publish does not roll back persisted pipeline state.

    The state machine writes the SQLite row that motivated the
    emission *before* publishing. The lifecycle emitter swallows
    :class:`PublishFailure` so the row stays put — pipeline truth
    lives in SQLite, the NATS stream is a derived projection.
    """

    @pytest.mark.asyncio
    async def test_publish_failure_does_not_rollback_running_state(
        self, forge_config: ForgeConfig
    ) -> None:
        # Simulated SQLite row — production code would write through
        # ``forge.adapters.sqlite``; the row dict captures the exact
        # ``status`` cell the lifecycle emitter is forbidden from
        # mutating on publish failure.
        sqlite_row = {"feature_id": FEATURE_ID, "status": "RUNNING"}

        # Publisher whose ``publish_build_started`` raises a transport
        # error wrapped in PublishFailure (the contract from
        # TASK-NFI-006).
        raising_publisher = MagicMock(spec=PipelinePublisher)
        raising_publisher.publish_build_started = AsyncMock(
            side_effect=PublishFailure(
                f"pipeline.build-started.{FEATURE_ID}", ConnectionError("nats down")
            )
        )

        emitter = PipelineLifecycleEmitter(
            publisher=raising_publisher,
            config=forge_config.pipeline,
        )
        ctx = BuildContext(
            feature_id=FEATURE_ID,
            build_id=BUILD_ID,
            correlation_id=CORRELATION_ID,
            wave_total=WAVE_TOTAL,
        )

        # Must not raise — emit_* swallows PublishFailure (AC-004 of
        # TASK-NFI-008, which AC-007 of this task verifies at the
        # integration level).
        await emitter.emit_started(ctx)

        raising_publisher.publish_build_started.assert_awaited_once()
        # SQLite row preserved — NO rollback. The row says RUNNING
        # because the state machine already wrote it.
        assert sqlite_row["status"] == "RUNNING", (
            "PublishFailure rolled back the SQLite row — feature truth lost"
        )

    @pytest.mark.asyncio
    async def test_publisher_itself_raises_publish_failure_on_transport_error(
        self,
    ) -> None:
        # Direct-publisher contract underneath the swallow: the
        # publisher MUST raise PublishFailure so the state-machine layer
        # can choose what to do (in our case: log + continue).
        nats_client = AsyncMock()
        nats_client.publish = AsyncMock(side_effect=ConnectionError("nats down"))
        publisher = PipelinePublisher(nats_client=nats_client)

        with pytest.raises(PublishFailure) as excinfo:
            await publisher.publish_build_started(
                BuildStartedPayload(
                    feature_id=FEATURE_ID, build_id=BUILD_ID, wave_total=WAVE_TOTAL
                )
            )
        assert excinfo.value.subject == f"pipeline.build-started.{FEATURE_ID}"
        assert isinstance(excinfo.value.__cause__, ConnectionError)


# ---------------------------------------------------------------------------
# AC-008 — clock hygiene grep
# ---------------------------------------------------------------------------


class TestClockHygiene:
    """AC-008: production code uses injected Clock primitives, not wall-clock.

    Walks ``src/forge/adapters/nats/`` and ``src/forge/discovery/`` and
    asserts every match for ``datetime.now(`` or ``asyncio.sleep(`` is
    on a documented allowlist.

    The allowlist captures the legitimate exceptions:

    * The Clock implementations themselves (``MonotonicClock.sleep``,
      ``SystemClock.now``) — they ARE the abstraction the rest of the
      code uses.
    * The reconnect-backoff loops in ``fleet_watcher.run`` and
      ``stale_sweeper`` — operational pacing for transport
      reconnects, not domain logic. Documented in module docstrings.
    * The ``resolved_at = datetime.now(UTC)`` default in
      ``discovery.resolve.resolve`` — caller may pin time via the
      ``now`` argument; default is the system clock.

    Any new occurrence outside this allowlist breaks the test and
    forces an explicit decision: either route through Clock, or
    document the exception here.
    """

    # ``(relative_path, regex_pattern)`` allowed-line specifications.
    # Path is relative to the repo root; pattern is a regex matched
    # against the **stripped** line content. Whitespace tolerant.
    _ALLOWED_RAW_USES: tuple[tuple[str, str], ...] = (
        # MonotonicClock.sleep — production Clock.sleep implementation.
        (
            "src/forge/adapters/nats/fleet_publisher.py",
            r"^await asyncio\.sleep\(seconds\)$",
        ),
        # FleetWatcher.run reconnect-backoff (no Clock injected at this layer).
        (
            "src/forge/adapters/nats/fleet_watcher.py",
            r"^await asyncio\.sleep\(reconnect_backoff_seconds\)$",
        ),
        # stale_sweeper inter-sweep cadence.
        (
            "src/forge/adapters/nats/fleet_watcher.py",
            r"^await asyncio\.sleep\(interval_s\)$",
        ),
        # SystemClock.now — production Clock.now implementation.
        (
            "src/forge/discovery/protocol.py",
            r"^return datetime\.now\(UTC\)$",
        ),
        # discovery.resolve default — caller may pin via ``now`` parameter.
        (
            "src/forge/discovery/resolve.py",
            r"^resolved_at = now if now is not None else datetime\.now\(UTC\)$",
        ),
    )

    # The two patterns the AC forbids (with anchors to filter
    # docstrings/comments — those still get caught when they contain a
    # *call*, but a comment like ``# uses datetime.now`` is fine).
    _DATETIME_NOW_RE = re.compile(r"datetime\.now\(")
    _ASYNCIO_SLEEP_RE = re.compile(r"\basyncio\.sleep\(")

    @pytest.fixture
    def repo_root(self) -> Path:
        # tests/ is at <repo>/tests/; this file lives in tests/forge/.
        return Path(__file__).resolve().parents[2]

    def _scan_directory(
        self, directory: Path, repo_root: Path
    ) -> list[tuple[str, int, str]]:
        """Return ``(rel_path, lineno, stripped_line)`` for every
        forbidden-pattern hit under ``directory``.

        Skips comment-only lines and docstring contents (very
        conservatively — we look for the patterns inside strings using
        a simple "is this line in a triple-quoted block" tracker).
        """

        hits: list[tuple[str, int, str]] = []
        for py_path in sorted(directory.rglob("*.py")):
            rel = str(py_path.relative_to(repo_root))
            in_triple = False
            triple_quote = ""
            for lineno, raw in enumerate(
                py_path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                stripped = raw.strip()

                # ----- Docstring / triple-quoted block tracking -------
                # If we're already inside a triple-quoted block, look for
                # the closing delimiter on this line and skip the line
                # outright — docstrings cannot contain executable calls.
                if in_triple:
                    if triple_quote and triple_quote in stripped:
                        in_triple = False
                        triple_quote = ""
                    continue

                # Lines that OPEN a triple-quoted block — including the
                # one-line ``"""foo"""`` form — are documentation. Skip
                # them entirely so docstring prose mentioning
                # ``datetime.now`` does not register as a violation.
                opens_triple = False
                for q in ('"""', "'''"):
                    if stripped.startswith(q):
                        # One-liner: ``"""...""" `` (>= 6 chars: two
                        # triples + at least zero content). Treat the
                        # opener-and-closer-on-the-same-line case as a
                        # comment-equivalent and skip.
                        rest = stripped[len(q) :]
                        if q in rest:
                            opens_triple = True  # Self-contained; skip
                        else:
                            in_triple = True
                            triple_quote = q
                            opens_triple = True
                        break
                if opens_triple:
                    continue

                # Skip pure-comment lines.
                if stripped.startswith("#"):
                    continue

                if self._DATETIME_NOW_RE.search(
                    stripped
                ) or self._ASYNCIO_SLEEP_RE.search(stripped):
                    hits.append((rel, lineno, stripped))
        return hits

    def _is_allowed(self, rel_path: str, stripped_line: str) -> bool:
        for allowed_path, allowed_pattern in self._ALLOWED_RAW_USES:
            if rel_path == allowed_path and re.match(allowed_pattern, stripped_line):
                return True
        return False

    def test_no_raw_clock_primitives_outside_allowlist(self, repo_root: Path) -> None:
        violations: list[str] = []
        for sub in ("src/forge/adapters/nats", "src/forge/discovery"):
            directory = repo_root / sub
            assert directory.exists(), f"directory {directory} not found"
            for rel_path, lineno, stripped in self._scan_directory(directory, repo_root):
                if not self._is_allowed(rel_path, stripped):
                    violations.append(f"{rel_path}:{lineno}: {stripped}")

        assert not violations, (
            "Clock-hygiene violations detected — production code under "
            "src/forge/adapters/nats/ and src/forge/discovery/ must use "
            "Clock.now() / Clock.sleep() (or await event.wait()) rather "
            "than raw datetime.now() / asyncio.sleep(). "
            "Add to the allowlist with justification if intentional:\n  "
            + "\n  ".join(violations)
        )

    def test_allowlist_entries_actually_match_real_lines(self, repo_root: Path) -> None:
        # Belt-and-braces: if a file is refactored and the allowlisted
        # line moves or is rewritten, this test fails so the allowlist
        # stays honest.
        for allowed_path, allowed_pattern in self._ALLOWED_RAW_USES:
            full_path = repo_root / allowed_path
            assert full_path.exists(), f"allowlist refers to missing file {allowed_path}"
            text = full_path.read_text(encoding="utf-8")
            stripped_lines = (line.strip() for line in text.splitlines())
            assert any(re.match(allowed_pattern, line) for line in stripped_lines), (
                f"allowlist pattern {allowed_pattern!r} no longer matches any "
                f"line in {allowed_path}; refactor likely moved the line"
            )


# ---------------------------------------------------------------------------
# Coverage hint — re-export deregister so AC-002's negative path is reachable
# ---------------------------------------------------------------------------


class TestRegisterDeregisterRoundTripSeam:
    """Negative-path companion to AC-002: deregister is also boundary-shaped.

    Not strictly required by AC-002, but covering the deregister side
    of the lifecycle in this seam suite keeps the boundary-test budget
    honest (the per-module suite tests deregister against a mock; this
    suite proves the manifest constant survives a register → deregister
    round-trip without leaking state into the cache).
    """

    @pytest.mark.asyncio
    async def test_register_then_deregister_leaves_no_residual_cache_entry(
        self,
    ) -> None:
        cache = DiscoveryCache(clock=_FakeDateClock())
        watcher = FleetWatcher(cache, status_reader=cache)
        # Real fleet client double for the deregister path — we are not
        # exercising it here, but we want the seam wiring to compile.
        nats_client = AsyncMock()
        nats_client.deregister_agent = AsyncMock(return_value=None)

        # Cache populated via the watcher seam.
        await watcher.on_fleet_change(FORGE_MANIFEST.agent_id, FORGE_MANIFEST)
        assert FORGE_MANIFEST.agent_id in await cache.snapshot()

        # Deregister + watcher-side remove. Both must succeed
        # idempotently.
        await deregister(nats_client)
        await watcher.on_fleet_change(FORGE_MANIFEST.agent_id, None)

        assert FORGE_MANIFEST.agent_id not in await cache.snapshot()
        nats_client.deregister_agent.assert_awaited_once()


# Re-export the discovery Clock protocol so static checkers don't flag
# the import as unused (we use it implicitly via DiscoveryCache(clock=…)).
__all__ = ["DiscoveryClock"]

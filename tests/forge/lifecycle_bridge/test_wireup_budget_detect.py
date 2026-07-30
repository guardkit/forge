"""FEAT-UBS-002 stage 2 (DETECT) — the daemon detects + escalates mid-run.

The ``forge serve`` lifecycle-bridge observer evaluates a running build's
budget after each published ``stage-complete``. On the FIRST cap breach it:

* RECORDS a compact human-readable record on ``schema_v7.builds.budget_breach``
  (first-write-wins), and
* ESCALATES a risk=high approval via the daemon's ``publish_approval_request``,

but it NEVER pauses / cancels / rewrites ``builds.status`` — the honesty law of
this lane: a mid-run hard stop is unavailable, so the daemon reports only what
it honestly effected. The run continues to its own bounded terminal and the F6
terminal contracts stand byte-identical.

These tests drive the observer end to end against a real migrated SQLite
``builds`` table + the real translator, exactly as ``test_wireup_no_terminal_f6``
does — the budget hook is exercised through the live SSE branch.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import AsyncIterator, Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from langgraph_sdk.schema import StreamPart
from nats_core.events import BuildQueuedPayload

from forge.adapters.sqlite import connect as sqlite_connect
from forge.config.models import BudgetGuards
from forge.lifecycle import migrations as lifecycle_migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.lifecycle.state_machine import BuildState
from forge.lifecycle_bridge.bridge import LifecycleBridge
from forge.lifecycle_bridge.budget_observer import BudgetBreachObserver
from forge.lifecycle_bridge.build_state_recorder import build_build_state_recorder
from forge.lifecycle_bridge.translation import (
    StreamEventTranslator,
    VALUES_STREAM_EVENT,
)
from forge.lifecycle_bridge.wireup import LifecycleBridgeWireup
from forge.persistence.migrations import lifecycle_bridge_registry as bridge_migration
from forge.persistence.repositories.bridge_registry import BridgeRegistry
from forge.pipeline.build_ack_handle import BuildAckHandle

_FEATURE_ID = "FEAT-BUDGET"
_CORRELATION_ID = "corr-budget"
_FIXED_TS = datetime(2026, 7, 27, 9, 14, 2, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def writer_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db_path = tmp_path / "forge.db"
    cx = sqlite_connect.connect_writer(db_path)
    lifecycle_migrations.apply_at_boot(cx)
    bridge_migration.apply(cx)
    try:
        yield cx
    finally:
        cx.close()


@pytest.fixture()
def persistence(
    writer_db: sqlite3.Connection, tmp_path: Path
) -> SqliteLifecyclePersistence:
    return SqliteLifecyclePersistence(connection=writer_db, db_path=tmp_path / "forge.db")


@pytest.fixture()
def registry(writer_db: sqlite3.Connection) -> BridgeRegistry:
    return BridgeRegistry(connection=writer_db)


@pytest.fixture()
def translator() -> StreamEventTranslator:
    return StreamEventTranslator()


@pytest.fixture()
def fake_publisher() -> MagicMock:
    pub = MagicMock(name="PipelinePublisher")
    for name in (
        "publish_build_started",
        "publish_stage_complete",
        "publish_build_complete",
        "publish_build_failed",
        "publish_build_paused",
        "publish_build_resumed",
        "publish_build_cancelled",
        "publish_build_progress",
    ):
        setattr(pub, name, AsyncMock(name=name))
    return pub


def _make_handle() -> BuildAckHandle:
    handle = AsyncMock(spec=BuildAckHandle)
    handle.ack = AsyncMock()
    handle.nak = AsyncMock()
    return handle


def _identity_resolved(thread_id: str = "thread-x", run_id: str = "run-x"):
    async def _provider(
        _feature_id: str, _correlation_id: str = ""
    ) -> tuple[str, str] | None:
        return (thread_id, run_id)

    return _provider


def _queued_build(
    persistence: SqliteLifecyclePersistence, *, feature_id: str = _FEATURE_ID
) -> str:
    now = datetime.now(UTC)
    payload = BuildQueuedPayload(
        feature_id=feature_id,
        repo="appmilla/api_test",
        feature_yaml_path=".guardkit/features/FEAT-BUDGET.yaml",
        triggered_by="cli",
        correlation_id=f"corr-{feature_id}",
        requested_at=now,
        queued_at=now,
    )
    return persistence.record_pending_build(payload)


def _state_part(
    feature_id: str,
    *,
    build_id: str,
    lifecycle: str,
    task_index: int = 0,
    tasks_completed: int = 0,
    last_coach_score: float | None = None,
) -> StreamPart:
    return StreamPart(
        event=VALUES_STREAM_EVENT,
        data={
            "async_tasks": {
                feature_id: {
                    "feature_id": feature_id,
                    "build_id": build_id,
                    "lifecycle": lifecycle,
                    "wave_total": 1,
                    "wave_index": 0,
                    "task_index": task_index,
                    "tasks_completed": tasks_completed,
                    "tasks_failed": 0,
                    "waiting_for": None,
                    "last_coach_score": last_coach_score,
                }
            }
        },
        id=None,
    )


def _lifecycle_parts(
    feature_id: str,
    build_id: str,
    *,
    stage_scores: list[float | None],
) -> list[StreamPart]:
    """starting → build-started → N stage-completes → build-complete.

    ``stage_scores`` supplies the ``last_coach_score`` for each stage-complete
    (its length is the number of stage-complete deltas emitted).
    """
    parts = [
        _state_part(feature_id, build_id=build_id, lifecycle="starting"),
        _state_part(
            feature_id, build_id=build_id, lifecycle="running_wave", tasks_completed=0
        ),
    ]
    for i, score in enumerate(stage_scores, start=1):
        parts.append(
            _state_part(
                feature_id,
                build_id=build_id,
                lifecycle="running_wave",
                task_index=i,
                tasks_completed=i,
                last_coach_score=score,
            )
        )
    parts.append(
        _state_part(
            feature_id,
            build_id=build_id,
            lifecycle="completed",
            tasks_completed=len(stage_scores),
        )
    )
    return parts


def _make_stream_source(parts: list[StreamPart]):
    def factory(*, feature_id, thread_id, run_id):
        async def gen() -> AsyncIterator[StreamPart]:
            for part in parts:
                yield part
                await asyncio.sleep(0)

        return gen()

    return factory


def _make_observer(
    persistence: SqliteLifecyclePersistence,
    *,
    guards: BudgetGuards,
    profile_name: str,
    elapsed_seconds: float,
    publish: AsyncMock,
    record_breach=None,
    resolve_raises: bool = False,
) -> BudgetBreachObserver:
    def _resolve(build_id: str):
        if resolve_raises:
            raise RuntimeError("injected resolve failure")
        return (guards, profile_name)

    return BudgetBreachObserver(
        resolve_budget=_resolve,
        elapsed_seconds=lambda _bid: elapsed_seconds,
        read_coach_score=persistence.read_last_coach_score,
        record_breach=record_breach or persistence.record_budget_breach,
        publish_approval_request=publish,
        approval_subject_for=lambda bid: f"agents.approval.forge.{bid}",
        clock=lambda: _FIXED_TS,
    )


def _build_wireup(
    *,
    bridge: LifecycleBridge,
    translator: StreamEventTranslator,
    fake_publisher: MagicMock,
    persistence: SqliteLifecyclePersistence,
    build_id: str,
    parts: list[StreamPart],
    budget_observer: BudgetBreachObserver | None,
) -> LifecycleBridgeWireup:
    async def _resolver(feature_id: str, correlation_id: str) -> str:
        return build_id

    return LifecycleBridgeWireup(
        bridge=bridge,
        translator=translator,
        publisher=fake_publisher,
        stream_source=_make_stream_source(parts),
        identity_provider=_identity_resolved(),
        build_state_recorder=build_build_state_recorder(persistence),
        build_id_resolver=_resolver,
        identity_resolution_attempts=1,
        identity_poll_interval_seconds=0.0,
        budget_observer=budget_observer,
    )


async def _drain(wireup: LifecycleBridgeWireup, feature_id: str) -> None:
    task = wireup.get_observer_task(feature_id)
    if task is None:
        return
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except asyncio.CancelledError:
        return


def _row_status(persistence: SqliteLifecyclePersistence, build_id: str) -> str:
    row = persistence.connection.execute(
        "SELECT status FROM builds WHERE build_id = ?", (build_id,)
    ).fetchone()
    assert row is not None
    return row["status"]


# ---------------------------------------------------------------------------
# Wall-clock breach — recorded + escalated + NO status change + stream continues
# ---------------------------------------------------------------------------


class TestWallClockBreach:
    @pytest.mark.asyncio
    async def test_breach_records_escalates_and_run_completes(
        self,
        registry: BridgeRegistry,
        translator: StreamEventTranslator,
        fake_publisher: MagicMock,
        persistence: SqliteLifecyclePersistence,
    ) -> None:
        build_id = _queued_build(persistence)
        publish = AsyncMock(name="publish_approval_request")
        observer = _make_observer(
            persistence,
            guards=BudgetGuards(max_build_wallclock_seconds=3600),
            profile_name="unattended",
            elapsed_seconds=3712.0,  # over the 3600s cap
            publish=publish,
        )
        parts = _lifecycle_parts(_FEATURE_ID, build_id, stage_scores=[None])

        bridge = LifecycleBridge(registry=registry)
        wireup = _build_wireup(
            bridge=bridge,
            translator=translator,
            fake_publisher=fake_publisher,
            persistence=persistence,
            build_id=build_id,
            parts=parts,
            budget_observer=observer,
        )
        handle = _make_handle()
        await wireup.register_ack_handle(_FEATURE_ID, _CORRELATION_ID, handle)
        await _drain(wireup, _FEATURE_ID)

        # A durable breach record was landed, naming the wall-clock cap.
        detail = persistence.read_budget_breach(build_id)
        assert detail is not None
        assert "max_build_wallclock_seconds" in detail
        assert detail.endswith(_FIXED_TS.isoformat())

        # A single risk=high escalation was published on the build's subject.
        publish.assert_awaited_once()
        payload, subject = publish.await_args.args
        assert subject == f"agents.approval.forge.{build_id}"
        assert payload.risk_level == "high"
        assert payload.request_id == f"budget-{build_id}-1"

        # HONESTY LAW: no pause / cancel — the run reached its own terminal.
        assert _row_status(persistence, build_id) == BuildState.COMPLETE.value
        # The stream continued: the terminal build-complete published + acked.
        fake_publisher.publish_build_complete.assert_awaited_once()
        handle.ack.assert_awaited_once()
        # And no PAUSED/CANCELLED envelope was ever emitted.
        fake_publisher.publish_build_paused.assert_not_awaited()
        fake_publisher.publish_build_cancelled.assert_not_awaited()

        await wireup.shutdown()


# ---------------------------------------------------------------------------
# Coach-floor breach — from the envelope score
# ---------------------------------------------------------------------------


class TestCoachFloorBreach:
    @pytest.mark.asyncio
    async def test_low_envelope_score_breaches_floor(
        self,
        registry: BridgeRegistry,
        translator: StreamEventTranslator,
        fake_publisher: MagicMock,
        persistence: SqliteLifecyclePersistence,
    ) -> None:
        build_id = _queued_build(persistence)
        publish = AsyncMock(name="publish_approval_request")
        observer = _make_observer(
            persistence,
            guards=BudgetGuards(min_coach_score=0.5),
            profile_name="unattended",
            elapsed_seconds=0.0,  # wall-clock inert
            publish=publish,
        )
        # First stage-complete carries a score of 0.0, below the 0.5 floor.
        parts = _lifecycle_parts(_FEATURE_ID, build_id, stage_scores=[0.0])

        bridge = LifecycleBridge(registry=registry)
        wireup = _build_wireup(
            bridge=bridge,
            translator=translator,
            fake_publisher=fake_publisher,
            persistence=persistence,
            build_id=build_id,
            parts=parts,
            budget_observer=observer,
        )
        handle = _make_handle()
        await wireup.register_ack_handle(_FEATURE_ID, _CORRELATION_ID, handle)
        await _drain(wireup, _FEATURE_ID)

        detail = persistence.read_budget_breach(build_id)
        assert detail is not None
        assert "min_coach_score" in detail
        publish.assert_awaited_once()
        # No status change; the run completed normally.
        assert _row_status(persistence, build_id) == BuildState.COMPLETE.value

        await wireup.shutdown()


# ---------------------------------------------------------------------------
# First-breach-wins — a second breach never escalates twice
# ---------------------------------------------------------------------------


class TestFirstBreachWins:
    @pytest.mark.asyncio
    async def test_second_breach_no_second_approval(
        self,
        registry: BridgeRegistry,
        translator: StreamEventTranslator,
        fake_publisher: MagicMock,
        persistence: SqliteLifecyclePersistence,
    ) -> None:
        build_id = _queued_build(persistence)
        publish = AsyncMock(name="publish_approval_request")
        observer = _make_observer(
            persistence,
            guards=BudgetGuards(max_build_wallclock_seconds=3600),
            profile_name="unattended",
            elapsed_seconds=3712.0,  # every stage-complete would breach
            publish=publish,
        )
        # TWO stage-completes; both would breach the wall-clock cap.
        parts = _lifecycle_parts(_FEATURE_ID, build_id, stage_scores=[None, None])

        bridge = LifecycleBridge(registry=registry)
        wireup = _build_wireup(
            bridge=bridge,
            translator=translator,
            fake_publisher=fake_publisher,
            persistence=persistence,
            build_id=build_id,
            parts=parts,
            budget_observer=observer,
        )
        handle = _make_handle()
        await wireup.register_ack_handle(_FEATURE_ID, _CORRELATION_ID, handle)
        await _drain(wireup, _FEATURE_ID)

        # Exactly one escalation — the FIRST breach won.
        publish.assert_awaited_once()
        payload, _subject = publish.await_args.args
        assert payload.request_id == f"budget-{build_id}-1"
        # The record is the first breach (never overwritten).
        detail = persistence.read_budget_breach(build_id)
        assert detail is not None and "max_build_wallclock_seconds" in detail

        await wireup.shutdown()


# ---------------------------------------------------------------------------
# Caps-off (attended) — strict no-op: zero record + zero publish
# ---------------------------------------------------------------------------


class TestCapsOffStrictNoop:
    @pytest.mark.asyncio
    async def test_attended_profile_records_nothing_publishes_nothing(
        self,
        registry: BridgeRegistry,
        translator: StreamEventTranslator,
        fake_publisher: MagicMock,
        persistence: SqliteLifecyclePersistence,
    ) -> None:
        build_id = _queued_build(persistence)
        publish = AsyncMock(name="publish_approval_request")
        record_breach = MagicMock(name="record_budget_breach")
        observer = _make_observer(
            persistence,
            guards=BudgetGuards(),  # attended — all caps None
            profile_name="attended",
            elapsed_seconds=999_999.0,  # would breach any wall-clock cap, if set
            publish=publish,
            record_breach=record_breach,
        )
        parts = _lifecycle_parts(_FEATURE_ID, build_id, stage_scores=[0.0, 0.0])

        bridge = LifecycleBridge(registry=registry)
        wireup = _build_wireup(
            bridge=bridge,
            translator=translator,
            fake_publisher=fake_publisher,
            persistence=persistence,
            build_id=build_id,
            parts=parts,
            budget_observer=observer,
        )
        handle = _make_handle()
        await wireup.register_ack_handle(_FEATURE_ID, _CORRELATION_ID, handle)
        await _drain(wireup, _FEATURE_ID)

        # Strict no-op: no breach recorded, no approval published.
        record_breach.assert_not_called()
        publish.assert_not_awaited()
        assert persistence.read_budget_breach(build_id) is None
        # The build completed normally (byte-equivalent lifecycle).
        assert _row_status(persistence, build_id) == BuildState.COMPLETE.value
        fake_publisher.publish_build_complete.assert_awaited_once()
        handle.ack.assert_awaited_once()

        await wireup.shutdown()


# ---------------------------------------------------------------------------
# Detector fault injection — a budget bug never breaks the observer stream
# ---------------------------------------------------------------------------


class TestDetectorFaultDoesNotBreakStream:
    @pytest.mark.asyncio
    async def test_resolve_exception_is_swallowed_run_completes(
        self,
        registry: BridgeRegistry,
        translator: StreamEventTranslator,
        fake_publisher: MagicMock,
        persistence: SqliteLifecyclePersistence,
    ) -> None:
        build_id = _queued_build(persistence)
        publish = AsyncMock(name="publish_approval_request")
        observer = _make_observer(
            persistence,
            guards=BudgetGuards(max_build_wallclock_seconds=3600),
            profile_name="unattended",
            elapsed_seconds=3712.0,
            publish=publish,
            resolve_raises=True,  # the detector blows up on first evaluation
        )
        parts = _lifecycle_parts(_FEATURE_ID, build_id, stage_scores=[None])

        bridge = LifecycleBridge(registry=registry)
        wireup = _build_wireup(
            bridge=bridge,
            translator=translator,
            fake_publisher=fake_publisher,
            persistence=persistence,
            build_id=build_id,
            parts=parts,
            budget_observer=observer,
        )
        handle = _make_handle()
        await wireup.register_ack_handle(_FEATURE_ID, _CORRELATION_ID, handle)
        await _drain(wireup, _FEATURE_ID)

        # The lifecycle stream is unaffected: build completes + acks.
        assert _row_status(persistence, build_id) == BuildState.COMPLETE.value
        fake_publisher.publish_build_complete.assert_awaited_once()
        handle.ack.assert_awaited_once()
        # The broken detector escalated nothing and recorded nothing.
        publish.assert_not_awaited()
        assert persistence.read_budget_breach(build_id) is None

        await wireup.shutdown()


# ---------------------------------------------------------------------------
# review-cycle cap + per-observer counter reset (bridge-restart documented)
# ---------------------------------------------------------------------------


class TestReviewCycleCounterPerObserver:
    @pytest.mark.asyncio
    async def test_review_cycle_cap_and_counter_resets_per_observer(
        self,
        registry: BridgeRegistry,
        translator: StreamEventTranslator,
        fake_publisher: MagicMock,
        persistence: SqliteLifecyclePersistence,
    ) -> None:
        # Two builds of two features, driven through the SAME wireup (shared
        # detector). Each observer gets its OWN fresh session — proving the
        # in-memory review-cycle counter is per-observer and starts at 0 (the
        # same reset that a bridge restart produces).
        publish = AsyncMock(name="publish_approval_request")
        observer = _make_observer(
            persistence,
            guards=BudgetGuards(max_review_cycles=2),
            profile_name="unattended",
            elapsed_seconds=0.0,
            publish=publish,
        )
        bridge = LifecycleBridge(registry=registry)

        results: dict[str, str] = {}
        for feature_id in ("FEAT-BONE", "FEAT-BTWO"):
            build_id = _queued_build(persistence, feature_id=feature_id)
            results[feature_id] = build_id
            # THREE stage-completes: the cap (2) fires on the 2nd.
            parts = _lifecycle_parts(
                feature_id, build_id, stage_scores=[None, None, None]
            )

            async def _resolver(fid: str, cid: str, _b=build_id) -> str:
                return _b

            wireup = LifecycleBridgeWireup(
                bridge=bridge,
                translator=translator,
                publisher=fake_publisher,
                stream_source=_make_stream_source(parts),
                identity_provider=_identity_resolved(),
                build_state_recorder=build_build_state_recorder(persistence),
                build_id_resolver=_resolver,
                identity_resolution_attempts=1,
                identity_poll_interval_seconds=0.0,
                budget_observer=observer,
            )
            handle = _make_handle()
            await wireup.register_ack_handle(
                feature_id, f"corr-{feature_id}", handle
            )
            await _drain(wireup, feature_id)
            await wireup.shutdown()

        # BOTH builds breached on their OWN 2nd stage-complete (request_id -2),
        # i.e. the counter reset to 0 for the second observer rather than
        # carrying over from the first.
        assert publish.await_count == 2
        request_ids = {call.args[0].request_id for call in publish.await_args_list}
        assert request_ids == {
            f"budget-{results['FEAT-BONE']}-2",
            f"budget-{results['FEAT-BTWO']}-2",
        }


# ---------------------------------------------------------------------------
# Production factory — build_budget_breach_observer composes the serve reuse
# ---------------------------------------------------------------------------


class TestProductionFactoryComposition:
    @pytest.mark.asyncio
    async def test_factory_wired_observer_detects_a_breach(
        self,
        registry: BridgeRegistry,
        translator: StreamEventTranslator,
        fake_publisher: MagicMock,
        persistence: SqliteLifecyclePersistence,
    ) -> None:
        from types import SimpleNamespace

        from forge.lifecycle_bridge.budget_observer import (
            build_budget_breach_observer,
        )

        build_id = _queued_build(persistence)
        publish = AsyncMock(name="publish_approval_request")
        # Minimal ForgeConfig-shaped stub: budget.resolve returns a coach floor.
        fake_config = SimpleNamespace(
            budget=SimpleNamespace(
                resolve=lambda _name: BudgetGuards(min_coach_score=0.5),
                default_profile="unattended",
            )
        )
        observer = build_budget_breach_observer(
            pool=persistence,
            config=fake_config,
            publish_approval_request=publish,
        )
        # First stage-complete carries 0.0, below the 0.5 floor.
        parts = _lifecycle_parts(_FEATURE_ID, build_id, stage_scores=[0.0])

        bridge = LifecycleBridge(registry=registry)
        wireup = _build_wireup(
            bridge=bridge,
            translator=translator,
            fake_publisher=fake_publisher,
            persistence=persistence,
            build_id=build_id,
            parts=parts,
            budget_observer=observer,
        )
        handle = _make_handle()
        await wireup.register_ack_handle(_FEATURE_ID, _CORRELATION_ID, handle)
        await _drain(wireup, _FEATURE_ID)

        detail = persistence.read_budget_breach(build_id)
        assert detail is not None and "min_coach_score" in detail
        publish.assert_awaited_once()
        _payload, subject = publish.await_args.args
        # The factory composed the canonical approval subject for the build.
        assert subject.endswith(build_id)
        assert _row_status(persistence, build_id) == BuildState.COMPLETE.value

        await wireup.shutdown()


# ---------------------------------------------------------------------------
# Cap-KILL arms the D659 pre-dispatch gate (Rich's 2026-07-30 ruling)
# ---------------------------------------------------------------------------
#
# A build the RUNNER killed at its budget wall-clock cap (FEAT-UBS-002 stage
# 1) dies BEFORE any further stage-complete, so the stage-complete DETECT path
# above can never record its breach — before this arc a cap-killed feature's
# re-queue silently SKIPPED the stage-3 pre-dispatch gate. The runner now
# stamps ``budget_cap_killed`` (+ the flat ``error_message`` reason) on the
# failed snapshot; the translator threads the marker onto the typed
# ``BuildFailedPayload``; the wireup records ``builds.budget_breach`` at its
# publish choke point. These tests drive the WHOLE chain: the runner's own
# snapshot shape → real translator → real wireup → real migrated SQLite →
# the REAL ``dispatch_build`` closure refusing the re-queue.


def _cap_kill_reason() -> str:
    return (
        "guardkit autobuild exceeded the budget wall-clock cap of 60.0s "
        "(profile='unattended') — killed (UBS-002)"
    )


def _runner_failed_part(
    feature_id: str,
    build_id: str,
    *,
    budget_cap_killed: bool,
) -> StreamPart:
    """A terminal failed part built from the RUNNER'S OWN snapshot shape."""
    from forge.subagents import autobuild_runner as ar

    snap = ar._build_failed_snapshot(
        {
            "feature_id": feature_id,
            "build_id": build_id,
            "correlation_id": _CORRELATION_ID,
        },
        reason=_cap_kill_reason() if budget_cap_killed else "guardkit autobuild exit=1",
        budget_cap_killed=budget_cap_killed,
    )
    return StreamPart(
        event=VALUES_STREAM_EVENT,
        data={"async_tasks": {feature_id: snap}},
        id=None,
    )


def _cap_kill_parts(
    feature_id: str, build_id: str, *, budget_cap_killed: bool = True
) -> list[StreamPart]:
    return [
        _state_part(feature_id, build_id=build_id, lifecycle="starting"),
        _state_part(feature_id, build_id=build_id, lifecycle="running_wave"),
        _runner_failed_part(
            feature_id, build_id, budget_cap_killed=budget_cap_killed
        ),
    ]


class _RecordingStarter:
    """Async-task starter double recording each launch context."""

    def __init__(self) -> None:
        self.contexts: list[dict] = []

    async def astart_async_task(self, subagent_name: str, context: dict) -> str:
        self.contexts.append(dict(context))
        return "task-capkill"


class _NoopContextBuilder:
    def build_for(self, *, stage, build_id: str, feature_id: str) -> list:
        return []


class _NoopStageLogRecorder:
    def record_running(self, **kwargs) -> None:
        return None


class _NoopStateChannel:
    def initialise_autobuild_state(self, **kwargs) -> None:
        return None


class _GatePoolAdapter:
    """Dispatch-pool double whose BREACH reads hit the REAL persistence.

    ``record_pending_build`` / ``get_build_row`` are faked (the re-queue's
    fresh row is not under test; a NULL profile keeps the launch payload
    budget-free), while ``latest_breach_for_feature`` / ``clear_budget_breach``
    delegate to the real migrated DB — so the gate decision rides the exact
    marker the cap-kill chain recorded.
    """

    def __init__(self, persistence: SqliteLifecyclePersistence) -> None:
        self._persistence = persistence

    def record_pending_build(self, payload, profile=None) -> str:
        return f"build-{payload.feature_id}-requeue"

    def get_build_row(self, build_id: str):
        from types import SimpleNamespace

        return SimpleNamespace(profile=None)

    def latest_breach_for_feature(self, feature_id: str):
        return self._persistence.latest_breach_for_feature(feature_id)

    def clear_budget_breach(self, build_id: str, cleared_at: str) -> None:
        self._persistence.clear_budget_breach(build_id, cleared_at)


def _requeue_dispatch(persistence: SqliteLifecyclePersistence, starter):
    """The REAL ``dispatch_build`` closure, gate-unwired (legacy branch)."""
    from types import SimpleNamespace

    from forge.cli import _serve_deps
    from forge.config.models import (
        FilesystemPermissions,
        ForgeConfig,
        PermissionsConfig,
    )

    config = ForgeConfig(
        permissions=PermissionsConfig(
            filesystem=FilesystemPermissions(allowlist=[Path("/tmp")]),
        ),
    )
    return _serve_deps._build_dispatch_build(
        sqlite_pool=_GatePoolAdapter(persistence),
        forward_context_builder=_NoopContextBuilder(),
        stage_log_recorder=_NoopStageLogRecorder(),
        state_channel=_NoopStateChannel(),
        lifecycle_emitter=SimpleNamespace(),
        async_task_starter=starter,
        forge_config=config,
        gate_repository=None,
        gate_state_machine=None,
    )


def _requeue_payload():
    from types import SimpleNamespace

    return SimpleNamespace(
        feature_id=_FEATURE_ID,
        repo="appmilla/api_test",
        branch="main",
        correlation_id="corr-requeue",
        parent_request_id=None,
        queued_at=datetime.now(UTC),
    )


async def _noop_ack() -> None:
    return None


class TestCapKillArmsBreachGate:
    @pytest.mark.asyncio
    async def test_cap_killed_build_records_breach_and_requeue_is_refused(
        self,
        registry: BridgeRegistry,
        translator: StreamEventTranslator,
        fake_publisher: MagicMock,
        persistence: SqliteLifecyclePersistence,
    ) -> None:
        build_id = _queued_build(persistence)
        approval_publish = AsyncMock(name="publish_approval_request")
        observer = _make_observer(
            persistence,
            guards=BudgetGuards(max_build_wallclock_seconds=60),
            profile_name="unattended",
            elapsed_seconds=0.0,
            publish=approval_publish,
        )
        parts = _cap_kill_parts(_FEATURE_ID, build_id)

        bridge = LifecycleBridge(registry=registry)
        wireup = _build_wireup(
            bridge=bridge,
            translator=translator,
            fake_publisher=fake_publisher,
            persistence=persistence,
            build_id=build_id,
            parts=parts,
            budget_observer=observer,
        )
        handle = _make_handle()
        await wireup.register_ack_handle(_FEATURE_ID, _CORRELATION_ID, handle)
        await _drain(wireup, _FEATURE_ID)

        # (1) The cap-kill landed a DURABLE breach marker — the exact flag the
        # D659 pre-dispatch gate reads.
        detail = persistence.read_budget_breach(build_id)
        assert detail is not None
        assert detail.startswith("wall_clock: ")
        assert "UBS-002" in detail
        assert detail.endswith(_FIXED_TS.isoformat())
        assert persistence.latest_breach_for_feature(_FEATURE_ID) == (
            build_id,
            detail,
        )

        # (2) RECORD-ONLY: the build is already dead — no mid-run escalation
        # (that is the stage-complete observer's seam, untouched here).
        approval_publish.assert_not_awaited()

        # (3) The terminal flow is unchanged: build-failed published + acked.
        fake_publisher.publish_build_failed.assert_awaited_once()
        failed_payload = fake_publisher.publish_build_failed.await_args.args[0]
        assert failed_payload.failure_reason == _cap_kill_reason()
        handle.ack.assert_awaited_once()

        # (4) Idempotence: a JetStream-redelivery re-record is a no-op (SQL
        # first-write-wins) — the original detail stands.
        observer.record_cap_kill(
            build_id=build_id, feature_id=_FEATURE_ID, detail="redelivered"
        )
        assert persistence.read_budget_breach(build_id) == detail

        # (5) THE RULING'S PIN: the feature's re-queue is routed through the
        # stage-3 breach gate. Gate-unwired boot → REFUSED loudly, never a
        # silent fresh launch.
        from forge.cli import _serve_deps

        starter = _RecordingStarter()
        dispatch = _requeue_dispatch(persistence, starter)
        with pytest.raises(_serve_deps.BudgetBreachDispatchRefused) as exc_info:
            await dispatch(_requeue_payload(), _noop_ack)
        assert exc_info.value.feature_id == _FEATURE_ID
        assert exc_info.value.prior_build_id == build_id
        assert "UBS-002" in exc_info.value.breach_detail
        assert starter.contexts == []

        await wireup.shutdown()

    @pytest.mark.asyncio
    async def test_plain_failure_does_not_arm_the_gate(
        self,
        registry: BridgeRegistry,
        translator: StreamEventTranslator,
        fake_publisher: MagicMock,
        persistence: SqliteLifecyclePersistence,
    ) -> None:
        """Negative control — gate semantics for ordinary failures unchanged."""
        build_id = _queued_build(persistence)
        approval_publish = AsyncMock(name="publish_approval_request")
        observer = _make_observer(
            persistence,
            guards=BudgetGuards(max_build_wallclock_seconds=60),
            profile_name="unattended",
            elapsed_seconds=0.0,
            publish=approval_publish,
        )
        parts = _cap_kill_parts(_FEATURE_ID, build_id, budget_cap_killed=False)

        bridge = LifecycleBridge(registry=registry)
        wireup = _build_wireup(
            bridge=bridge,
            translator=translator,
            fake_publisher=fake_publisher,
            persistence=persistence,
            build_id=build_id,
            parts=parts,
            budget_observer=observer,
        )
        handle = _make_handle()
        await wireup.register_ack_handle(_FEATURE_ID, _CORRELATION_ID, handle)
        await _drain(wireup, _FEATURE_ID)

        assert persistence.read_budget_breach(build_id) is None
        assert persistence.latest_breach_for_feature(_FEATURE_ID) is None

        # The re-queue launches exactly as before this lane.
        starter = _RecordingStarter()
        dispatch = _requeue_dispatch(persistence, starter)
        await dispatch(_requeue_payload(), _noop_ack)
        assert len(starter.contexts) == 1

        await wireup.shutdown()

    @pytest.mark.asyncio
    async def test_cap_kill_without_observer_logs_loud_and_never_breaks_stream(
        self,
        registry: BridgeRegistry,
        translator: StreamEventTranslator,
        fake_publisher: MagicMock,
        persistence: SqliteLifecyclePersistence,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """No wired observer → the marker cannot record; fail LOUD, not silent.

        The lifecycle stream is more load-bearing than budget enforcement:
        the terminal must still publish + ack.
        """
        import logging

        build_id = _queued_build(persistence)
        parts = _cap_kill_parts(_FEATURE_ID, build_id)

        bridge = LifecycleBridge(registry=registry)
        wireup = _build_wireup(
            bridge=bridge,
            translator=translator,
            fake_publisher=fake_publisher,
            persistence=persistence,
            build_id=build_id,
            parts=parts,
            budget_observer=None,
        )
        handle = _make_handle()
        with caplog.at_level(
            logging.ERROR, logger="forge.lifecycle_bridge.wireup"
        ):
            await wireup.register_ack_handle(_FEATURE_ID, _CORRELATION_ID, handle)
            await _drain(wireup, _FEATURE_ID)

        assert persistence.read_budget_breach(build_id) is None
        fake_publisher.publish_build_failed.assert_awaited_once()
        handle.ack.assert_awaited_once()
        assert any(
            "NO budget observer" in r.getMessage() for r in caplog.records
        )

        await wireup.shutdown()

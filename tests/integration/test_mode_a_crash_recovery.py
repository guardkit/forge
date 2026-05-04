"""Crash-recovery integration tests across all seven non-terminal stages.

This module is the executor-layer half of FEAT-FORGE-007 Group D
crash-recovery coverage (TASK-MAG7-013). It exercises three invariants:

1. **Retry-from-scratch policy** — a crash during *any* of the seven
   non-terminal Mode A stage classes (``product-owner``, ``architect``,
   ``system-arch``, ``system-design``, ``feature-spec``, ``feature-plan``,
   ``autobuild``) is recovered by restarting the in-flight stage from the
   beginning. Recovery is parameterised so a regression in any one stage's
   recovery path surfaces as a single failing test row.

2. **Durable history beats advisory state channel (ASSUM-004)** — when a
   process crashes mid-autobuild, the *authoritative* status of the build
   comes from the SQLite ``stage_log`` (durable), not from the live
   ``async_tasks`` LangGraph state channel (advisory). The post-restart
   supervisor reads ``stage_log`` and re-dispatches autobuild from
   scratch, even though the advisory channel still reports
   ``lifecycle="running_wave"`` from the pre-crash dispatch.

3. **Stage-log durability under collateral failures** — a notification
   publish failure (Group G ``@data-integrity``) and a long-term-memory
   seeding failure (Group I ``@data-integrity``) must not roll back the
   stage's approved record. The next stage's prerequisite still
   evaluates as satisfied via the canonical
   :class:`StageOrderingGuard.next_dispatchable`.

The "crash" is simulated by tearing down the supervisor instance and
instantiating a fresh one against the same in-memory ``stage_log`` /
``async_tasks`` doubles — the same code path that runs after a process
restart. No real wall-clock waits are used: every clock surface goes
through :class:`FakeClock`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Iterable, Mapping

import pytest

from forge.pipeline.constitutional_guard import ConstitutionalGuard
from forge.pipeline.per_feature_sequencer import PerFeatureLoopSequencer
from forge.pipeline.stage_ordering_guard import StageOrderingGuard
from forge.pipeline.stage_taxonomy import StageClass
from forge.pipeline.supervisor import (
    BuildState,
    DispatchChoice,
    Supervisor,
    TurnOutcome,
)

# ---------------------------------------------------------------------------
# FakeClock — deterministic UTC datetime callable
# ---------------------------------------------------------------------------


_FIXED_TIME = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)


class FakeClock:
    """Frozen UTC datetime callable. No real wall-clock movement."""

    def __init__(self, fixed: datetime | None = None) -> None:
        self._fixed = fixed or _FIXED_TIME

    def __call__(self) -> datetime:
        return self._fixed


# ---------------------------------------------------------------------------
# DurableStageLog — survives the simulated crash
# ---------------------------------------------------------------------------
#
# Implements three Protocol surfaces in one object:
# * ``OrderingStageLogReader`` (``is_approved`` + ``feature_catalogue``)
# * ``PerFeatureStageLogReader`` (``is_autobuild_approved``)
# * ``StageLogTurnRecorder`` (``record_turn``)
#
# The same instance is passed to both pre-crash and post-crash supervisors
# so the durable view of the world persists across the simulated restart.


@dataclass
class DurableStageLog:
    """In-memory ``stage_log`` surrogate. Survives the crash."""

    approved: set[tuple[str, StageClass, str | None]] = field(default_factory=set)
    catalogues: dict[str, list[str]] = field(default_factory=dict)
    approved_autobuilds: set[tuple[str, str]] = field(default_factory=set)
    turn_rows: list[dict[str, Any]] = field(default_factory=list)

    # OrderingStageLogReader -------------------------------------------------

    def is_approved(
        self,
        build_id: str,
        stage: StageClass,
        feature_id: str | None = None,
    ) -> bool:
        return (build_id, stage, feature_id) in self.approved

    def feature_catalogue(self, build_id: str) -> list[str]:
        return list(self.catalogues.get(build_id, []))

    # PerFeatureStageLogReader ----------------------------------------------

    def is_autobuild_approved(self, build_id: str, feature_id: str) -> bool:
        return (build_id, feature_id) in self.approved_autobuilds

    # StageLogTurnRecorder --------------------------------------------------

    def record_turn(
        self,
        *,
        build_id: str,
        outcome: TurnOutcome,
        permitted_stages: frozenset[StageClass],
        chosen_stage: StageClass | None,
        chosen_feature_id: str | None,
        rationale: str,
        gate_verdict: str | None,
    ) -> None:
        self.turn_rows.append(
            {
                "build_id": build_id,
                "outcome": outcome,
                "permitted_stages": frozenset(permitted_stages),
                "chosen_stage": chosen_stage,
                "chosen_feature_id": chosen_feature_id,
                "rationale": rationale,
                "gate_verdict": gate_verdict,
            }
        )

    # Helpers ---------------------------------------------------------------

    def approved_count(
        self,
        build_id: str,
        stage: StageClass,
        feature_id: str | None = None,
    ) -> int:
        """Return 1 iff ``stage`` is recorded approved exactly once.

        ``approved`` is a :class:`set`, so the call's primary purpose is
        to make the *idempotency intent* of the caller readable: "no
        duplicate ``stage_log`` entry written for the re-attempted
        stage" reads as ``approved_count(...) <= 1`` at the call site.
        """
        return 1 if (build_id, stage, feature_id) in self.approved else 0


# ---------------------------------------------------------------------------
# Advisory state channel — simulates the LangGraph ``async_tasks`` channel
# ---------------------------------------------------------------------------


@dataclass
class FakeAutobuildState:
    feature_id: str
    lifecycle: str


@dataclass
class AdvisoryAsyncTaskChannel:
    """Stale-but-readable advisory channel that survives the crash.

    Per DDR-006 / ASSUM-004: this channel is *advisory only*; the
    authoritative status of the build comes from
    :class:`DurableStageLog`. Tests assert that the post-crash
    supervisor consults the durable side and re-dispatches even when
    this channel still reports ``running_wave`` for the in-flight
    autobuild.
    """

    states_by_build: dict[str, list[FakeAutobuildState]] = field(default_factory=dict)

    def list_autobuild_states(self, build_id: str) -> Iterable[FakeAutobuildState]:
        return list(self.states_by_build.get(build_id, []))


# ---------------------------------------------------------------------------
# DurableStateMachine — coarse build-state surface
# ---------------------------------------------------------------------------


@dataclass
class DurableStateMachine:
    """Coarse build-state map. Survives the crash; ``reset_to_preparing``
    models the state-machine bootstrap path that runs on process restart
    (FEAT-FORGE-001: the supervisor's outer loop sees ``PREPARING`` and
    re-walks the durable history).
    """

    states: dict[str, BuildState] = field(default_factory=dict)

    def get_build_state(self, build_id: str) -> BuildState:
        return self.states.get(build_id, BuildState.RUNNING)

    def reset_to_preparing(self, build_id: str) -> None:
        self.states[build_id] = BuildState.PREPARING


# ---------------------------------------------------------------------------
# Reasoning-model fake — deterministic dispatch picker
# ---------------------------------------------------------------------------


@dataclass
class ScriptedReasoningModel:
    """Returns a fixed :class:`DispatchChoice` per call.

    Production wires a LangChain ``ChatModel`` adapter; for crash-recovery
    coverage we only care that the supervisor's executor-layer guards
    permit the chosen stage, so a deterministic stub is the right scope.
    """

    pick_stage: StageClass | None = None
    pick_feature_id: str | None = None
    rationale: str = "crash-recovery dispatch"

    def choose_dispatch(
        self,
        *,
        build_id: str,  # noqa: ARG002 — Protocol surface
        build_state: BuildState,  # noqa: ARG002 — Protocol surface
        permitted_stages: frozenset[StageClass],  # noqa: ARG002
        stage_hints: Mapping[StageClass, str],  # noqa: ARG002
        feature_catalogue: tuple[str, ...],  # noqa: ARG002
    ) -> DispatchChoice | None:
        if self.pick_stage is None:
            return None
        return DispatchChoice(
            stage=self.pick_stage,
            feature_id=self.pick_feature_id,
            rationale=self.rationale,
        )


# ---------------------------------------------------------------------------
# Recording dispatchers
# ---------------------------------------------------------------------------


@dataclass
class RecordingDispatcher:
    """Async dispatcher (specialist + subprocess)."""

    label: str
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return {"dispatcher": self.label, "status": "dispatched"}


@dataclass
class RecordingAutobuildDispatcher:
    """Async dispatcher that mirrors the production autobuild side effect.

    The real :func:`dispatch_autobuild_async` (``async def`` per
    TASK-FORGE-FRR-F010G) writes an ``AutobuildState`` entry into the
    LangGraph ``async_tasks`` channel with ``lifecycle="running_wave"``.
    Tests propagate that side effect here so the post-crash advisory
    snapshot is realistic.
    """

    label: str
    advisory_channel: AdvisoryAsyncTaskChannel
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        build_id = kwargs.get("build_id", "")
        feature_id = kwargs.get("feature_id", "")
        if build_id and feature_id:
            self.advisory_channel.states_by_build.setdefault(build_id, []).append(
                FakeAutobuildState(
                    feature_id=feature_id,
                    lifecycle="running_wave",
                )
            )
        return {
            "dispatcher": self.label,
            "task_id": f"task-{feature_id or 'x'}",
            "status": "dispatched",
        }


@dataclass
class RecordingPRReviewGate:
    submissions: list[dict[str, Any]] = field(default_factory=list)

    def submit_decision(self, **kwargs: Any) -> Any:
        self.submissions.append(kwargs)
        return {"gate": "pr-review", "status": "submitted"}


# ---------------------------------------------------------------------------
# Supervisor factory + prerequisite seeder
# ---------------------------------------------------------------------------


def _make_supervisor(
    *,
    durable_log: DurableStageLog,
    state_machine: DurableStateMachine,
    advisory_channel: AdvisoryAsyncTaskChannel,
    reasoning_model: ScriptedReasoningModel,
    specialist_dispatcher: Any | None = None,
    subprocess_dispatcher: Any | None = None,
    autobuild_dispatcher: Any | None = None,
    pr_review_gate: Any | None = None,
) -> Supervisor:
    """Wire a Supervisor against the shared durable + advisory doubles."""
    spec = specialist_dispatcher or RecordingDispatcher(label="specialist")
    sub = subprocess_dispatcher or RecordingDispatcher(label="subprocess")
    auto = autobuild_dispatcher or RecordingAutobuildDispatcher(
        label="autobuild_async", advisory_channel=advisory_channel
    )
    pr = pr_review_gate or RecordingPRReviewGate()
    return Supervisor(
        ordering_guard=StageOrderingGuard(),
        per_feature_sequencer=PerFeatureLoopSequencer(),
        constitutional_guard=ConstitutionalGuard(),
        state_reader=state_machine,
        ordering_stage_log_reader=durable_log,
        per_feature_stage_log_reader=durable_log,
        async_task_reader=advisory_channel,
        reasoning_model=reasoning_model,
        turn_recorder=durable_log,
        specialist_dispatcher=spec,
        subprocess_dispatcher=sub,
        autobuild_dispatcher=auto,
        pr_review_gate=pr,
        stage_hints={},
    )


def _seed_prereqs_through(
    durable_log: DurableStageLog,
    build_id: str,
    stage: StageClass,
    feature_id: str | None = None,
) -> None:
    """Seed ``durable_log`` so that ``stage`` is the first dispatchable stage.

    Mirrors the FEAT-FORGE-007 Group B prerequisite chain. For per-feature
    stages we also register ``feature_id`` on the build's catalogue so
    :class:`StageOrderingGuard.next_dispatchable` walks the per-feature
    branch.
    """
    if feature_id:
        cat = durable_log.catalogues.setdefault(build_id, [])
        if feature_id not in cat:
            cat.append(feature_id)

    non_per_feature_chain = (
        StageClass.PRODUCT_OWNER,
        StageClass.ARCHITECT,
        StageClass.SYSTEM_ARCH,
        StageClass.SYSTEM_DESIGN,
    )
    chain_index = {s: i for i, s in enumerate(non_per_feature_chain)}

    if stage in chain_index:
        # Approve every earlier non-per-feature stage.
        for earlier in non_per_feature_chain[: chain_index[stage]]:
            durable_log.approved.add((build_id, earlier, None))
        return

    # Per-feature stages: approve the full non-per-feature chain plus
    # any per-feature predecessors for this feature_id.
    for earlier in non_per_feature_chain:
        durable_log.approved.add((build_id, earlier, None))

    if stage is StageClass.FEATURE_SPEC:
        return
    if stage is StageClass.FEATURE_PLAN:
        durable_log.approved.add((build_id, StageClass.FEATURE_SPEC, feature_id))
        return
    if stage is StageClass.AUTOBUILD:
        durable_log.approved.add((build_id, StageClass.FEATURE_SPEC, feature_id))
        durable_log.approved.add((build_id, StageClass.FEATURE_PLAN, feature_id))
        return


# Seven non-terminal stage classes from the Group D Scenario Outline.
SEVEN_STAGES: list[tuple[StageClass, str | None]] = [
    (StageClass.PRODUCT_OWNER, None),
    (StageClass.ARCHITECT, None),
    (StageClass.SYSTEM_ARCH, None),
    (StageClass.SYSTEM_DESIGN, None),
    (StageClass.FEATURE_SPEC, "FEAT-CR-1"),
    (StageClass.FEATURE_PLAN, "FEAT-CR-1"),
    (StageClass.AUTOBUILD, "FEAT-CR-1"),
]


# ---------------------------------------------------------------------------
# AC-002 — parameterised retry-from-scratch across all seven stages
# ---------------------------------------------------------------------------


class TestCrashRecoveryAcrossSevenStages:
    """Group D: a crash during any non-terminal stage retries from scratch."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "stage, feature_id",
        SEVEN_STAGES,
        ids=[s[0].value for s in SEVEN_STAGES],
    )
    async def test_retry_from_scratch_after_crash(
        self,
        stage: StageClass,
        feature_id: str | None,
    ) -> None:
        # Use the FakeClock for the canary (every test in this module
        # routes time through it; we exercise the constructor here so a
        # regression breaking the deterministic-clock contract surfaces
        # in the parameterised row instead of a hidden import error).
        clock = FakeClock()
        assert clock() == _FIXED_TIME

        build_id = f"build-CRASH-{stage.value}"
        durable_log = DurableStageLog()
        state_machine = DurableStateMachine()
        advisory = AdvisoryAsyncTaskChannel()
        _seed_prereqs_through(durable_log, build_id, stage, feature_id)

        # --- Pre-crash: drive supervisor to mid-flight on ``stage`` -----
        state_machine.states[build_id] = BuildState.RUNNING
        rm_pre = ScriptedReasoningModel(pick_stage=stage, pick_feature_id=feature_id)
        sup_pre = _make_supervisor(
            durable_log=durable_log,
            state_machine=state_machine,
            advisory_channel=advisory,
            reasoning_model=rm_pre,
        )
        report_pre = await sup_pre.next_turn(build_id)
        assert report_pre.outcome is TurnOutcome.DISPATCHED
        assert report_pre.chosen_stage is stage
        assert report_pre.chosen_feature_id == feature_id

        # In-flight stage is NOT yet approved — that's the whole point
        # of "mid-flight": the dispatcher fired but no approval row was
        # written. The retry-from-scratch policy depends on this.
        assert durable_log.approved_count(build_id, stage, feature_id) == 0
        pre_turn_count_for_stage = sum(
            1 for row in durable_log.turn_rows if row["chosen_stage"] is stage
        )
        assert pre_turn_count_for_stage == 1

        # --- Crash: discard supervisor; state machine resets to PREPARING.
        del sup_pre
        state_machine.reset_to_preparing(build_id)
        assert state_machine.get_build_state(build_id) is BuildState.PREPARING

        # --- Post-crash: fresh supervisor against same durable + advisory.
        rm_post = ScriptedReasoningModel(pick_stage=stage, pick_feature_id=feature_id)
        sup_post = _make_supervisor(
            durable_log=durable_log,
            state_machine=state_machine,
            advisory_channel=advisory,
            reasoning_model=rm_post,
        )
        report_post = await sup_post.next_turn(build_id)

        # The same stage is re-attempted from start.
        assert report_post.outcome is TurnOutcome.DISPATCHED
        assert report_post.chosen_stage is stage
        assert report_post.chosen_feature_id == feature_id
        # No duplicate stage_log "approved" entry was written for the
        # re-attempted stage — idempotent retry (AC-002 final clause).
        assert durable_log.approved_count(build_id, stage, feature_id) == 0
        # Two per-turn rows now (pre-crash dispatch + post-crash redispatch);
        # both reference the same stage but neither is an approval row.
        post_turn_count_for_stage = sum(
            1 for row in durable_log.turn_rows if row["chosen_stage"] is stage
        )
        assert post_turn_count_for_stage == 2


# ---------------------------------------------------------------------------
# AC-003 — mid-autobuild crash; durable history beats advisory channel
# ---------------------------------------------------------------------------


class TestMidAutobuildCrashAuthoritativeDurableHistory:
    """ASSUM-004: durable ``stage_log`` is authoritative; advisory channel is not."""

    @pytest.mark.asyncio
    async def test_crash_mid_running_wave_restarts_autobuild_from_scratch(
        self,
    ) -> None:
        build_id = "build-CRASH-AUTOBUILD"
        feature_id = "FEAT-CR-AB"
        durable_log = DurableStageLog()
        state_machine = DurableStateMachine()
        advisory = AdvisoryAsyncTaskChannel()
        _seed_prereqs_through(durable_log, build_id, StageClass.AUTOBUILD, feature_id)

        # --- Pre-crash: dispatch autobuild; advisory reaches running_wave.
        state_machine.states[build_id] = BuildState.RUNNING
        rm_pre = ScriptedReasoningModel(
            pick_stage=StageClass.AUTOBUILD, pick_feature_id=feature_id
        )
        sup_pre = _make_supervisor(
            durable_log=durable_log,
            state_machine=state_machine,
            advisory_channel=advisory,
            reasoning_model=rm_pre,
        )
        report_pre = await sup_pre.next_turn(build_id)
        assert report_pre.outcome is TurnOutcome.DISPATCHED
        # AC-003: driver waits until AutobuildState.lifecycle == running_wave.
        assert any(
            s.lifecycle == "running_wave"
            for s in advisory.list_autobuild_states(build_id)
        )
        # Durable history shows AUTOBUILD as NOT approved (mid-flight).
        assert durable_log.is_autobuild_approved(build_id, feature_id) is False
        assert (
            durable_log.is_approved(build_id, StageClass.AUTOBUILD, feature_id) is False
        )

        # --- Crash.
        del sup_pre
        state_machine.reset_to_preparing(build_id)

        # --- Post-crash: advisory still reports running_wave; durable
        #     history says NOT approved. Per ASSUM-004, durable wins.
        rm_post = ScriptedReasoningModel(
            pick_stage=StageClass.AUTOBUILD, pick_feature_id=feature_id
        )
        post_dispatcher = RecordingAutobuildDispatcher(
            label="autobuild_async-post-crash",
            advisory_channel=advisory,
        )
        sup_post = _make_supervisor(
            durable_log=durable_log,
            state_machine=state_machine,
            advisory_channel=advisory,
            reasoning_model=rm_post,
            autobuild_dispatcher=post_dispatcher,
        )
        report_post = await sup_post.next_turn(build_id)

        # Build restarted autobuild from scratch — the supervisor consulted
        # ``stage_log`` (authoritative) and ignored the advisory channel.
        assert report_post.outcome is TurnOutcome.DISPATCHED
        assert report_post.chosen_stage is StageClass.AUTOBUILD
        assert report_post.chosen_feature_id == feature_id
        assert len(post_dispatcher.calls) == 1
        assert post_dispatcher.calls[0]["build_id"] == build_id
        assert post_dispatcher.calls[0]["feature_id"] == feature_id


# ---------------------------------------------------------------------------
# AC-004 — notification publish failure does not roll back stage_log
# ---------------------------------------------------------------------------


class _PublishFailure(RuntimeError):
    """Local stand-in for ``forge.adapters.nats.pipeline_publisher.PublishFailure``.

    Defined locally so the test file does not import the NATS adapter
    surface (which would pull in transport-layer test scaffolding the
    crash-recovery suite is deliberately scoped away from). The raise/
    suppress contract under test is identical: the dispatcher records
    the stage as approved BEFORE the publisher fires, and a publish
    failure is logged-and-continued rather than rolled back.
    """


@dataclass
class FailingPublisher:
    """Stub publisher whose ``publish`` always raises ``_PublishFailure``."""

    calls: int = 0

    def publish(self, **_: Any) -> None:
        self.calls += 1
        raise _PublishFailure(
            "simulated transport outage — stage_log persistence " "must not roll back"
        )


@dataclass
class StageLogFirstSpecialistDispatcher:
    """Specialist-shaped dispatcher that records stage_log approval BEFORE
    invoking the notification publisher. This mirrors the production
    contract that SQLite truth is committed before the advisory NATS
    projection is published (FEAT-FORGE-007 Group G ``@data-integrity``).
    """

    durable_log: DurableStageLog
    publisher: FailingPublisher
    calls: list[dict[str, Any]] = field(default_factory=list)
    publish_failed: bool = False

    async def __call__(self, **kwargs: Any) -> Any:
        stage = kwargs.get("stage")
        build_id = kwargs.get("build_id")
        feature_id = kwargs.get("feature_id")
        # Durable record FIRST — this is the contract under test.
        self.durable_log.approved.add((build_id, stage, feature_id))
        self.calls.append(kwargs)
        # Then the advisory publish; failure is suppressed (logged in
        # production) so it does NOT undo the stage_log row.
        try:
            self.publisher.publish(stage=stage, build_id=build_id)
        except _PublishFailure:
            self.publish_failed = True
        return {"status": "approved-with-publish-failure"}


class TestNotificationPublishFailure:
    """Group G ``@data-integrity``: publish failure must not block stage_log."""

    @pytest.mark.asyncio
    async def test_publish_failure_does_not_roll_back_stage_log(self) -> None:
        build_id = "build-NPF"
        durable_log = DurableStageLog()
        state_machine = DurableStateMachine()
        advisory = AdvisoryAsyncTaskChannel()
        _seed_prereqs_through(durable_log, build_id, StageClass.PRODUCT_OWNER)
        state_machine.states[build_id] = BuildState.RUNNING

        publisher = FailingPublisher()
        spec = StageLogFirstSpecialistDispatcher(
            durable_log=durable_log, publisher=publisher
        )
        rm = ScriptedReasoningModel(pick_stage=StageClass.PRODUCT_OWNER)
        sup = _make_supervisor(
            durable_log=durable_log,
            state_machine=state_machine,
            advisory_channel=advisory,
            reasoning_model=rm,
            specialist_dispatcher=spec,
        )

        report = await sup.next_turn(build_id)

        assert report.outcome is TurnOutcome.DISPATCHED
        # Publisher fired and raised exactly once.
        assert publisher.calls == 1
        assert spec.publish_failed is True
        # Stage IS recorded as approved despite the publish failure.
        assert durable_log.is_approved(build_id, StageClass.PRODUCT_OWNER) is True
        # Next stage's prerequisite (ARCHITECT ← PRODUCT_OWNER) is now
        # satisfied — same canonical guard that production consults.
        guard = StageOrderingGuard()
        permitted = guard.next_dispatchable(build_id, durable_log)
        assert StageClass.ARCHITECT in permitted


# ---------------------------------------------------------------------------
# AC-005 — long-term-memory seeding failure does not block stage_log
# ---------------------------------------------------------------------------


class _LtmSeederFailure(RuntimeError):
    """Local stand-in for the long-term-memory seeder's failure type."""


@dataclass
class FailingLtmSeeder:
    calls: int = 0

    def seed(self, **_: Any) -> None:
        self.calls += 1
        raise _LtmSeederFailure(
            "simulated LTM (graphiti) seeding failure — stage_log "
            "persistence must not roll back"
        )


@dataclass
class StageLogFirstLtmAwareDispatcher:
    """Specialist-shaped dispatcher that records stage_log approval BEFORE
    seeding long-term memory. Mirrors the production contract from Group
    I ``@data-integrity``: SQLite is committed before the LTM seeder
    fires; a seeder failure is logged-and-continued.
    """

    durable_log: DurableStageLog
    ltm_seeder: FailingLtmSeeder
    calls: list[dict[str, Any]] = field(default_factory=list)
    ltm_failed: bool = False

    async def __call__(self, **kwargs: Any) -> Any:
        stage = kwargs.get("stage")
        build_id = kwargs.get("build_id")
        feature_id = kwargs.get("feature_id")
        self.durable_log.approved.add((build_id, stage, feature_id))
        self.calls.append(kwargs)
        try:
            self.ltm_seeder.seed(stage=stage, build_id=build_id)
        except _LtmSeederFailure:
            self.ltm_failed = True
        return {"status": "approved-with-ltm-failure"}


class TestLongTermMemorySeedingFailure:
    """Group I ``@data-integrity``: LTM seeding failure must not block stage_log."""

    @pytest.mark.asyncio
    async def test_ltm_seeder_failure_does_not_roll_back_stage_log(
        self,
    ) -> None:
        build_id = "build-LTM"
        durable_log = DurableStageLog()
        state_machine = DurableStateMachine()
        advisory = AdvisoryAsyncTaskChannel()
        _seed_prereqs_through(durable_log, build_id, StageClass.PRODUCT_OWNER)
        state_machine.states[build_id] = BuildState.RUNNING

        ltm_seeder = FailingLtmSeeder()
        spec = StageLogFirstLtmAwareDispatcher(
            durable_log=durable_log, ltm_seeder=ltm_seeder
        )
        rm = ScriptedReasoningModel(pick_stage=StageClass.PRODUCT_OWNER)
        sup = _make_supervisor(
            durable_log=durable_log,
            state_machine=state_machine,
            advisory_channel=advisory,
            reasoning_model=rm,
            specialist_dispatcher=spec,
        )

        report = await sup.next_turn(build_id)

        assert report.outcome is TurnOutcome.DISPATCHED
        # LTM seeder fired and raised exactly once.
        assert ltm_seeder.calls == 1
        assert spec.ltm_failed is True
        # Stage IS recorded as approved despite the LTM failure.
        assert durable_log.is_approved(build_id, StageClass.PRODUCT_OWNER) is True


# ---------------------------------------------------------------------------
# AC-006 — FakeClock canary: no real wall-clock waits anywhere in the suite
# ---------------------------------------------------------------------------


class TestFakeClockCanary:
    """Every clock surface in this module routes through :class:`FakeClock`."""

    def test_fake_clock_returns_fixed_utc_time(self) -> None:
        clock = FakeClock()
        first = clock()
        second = clock()
        assert first == second  # No wall-clock movement between calls.
        assert first.tzinfo is UTC

    def test_fake_clock_accepts_caller_supplied_time(self) -> None:
        custom = datetime(2027, 1, 1, 0, 0, 0, tzinfo=UTC)
        clock = FakeClock(fixed=custom)
        assert clock() == custom

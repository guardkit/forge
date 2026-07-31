"""THE CARD-COUNT AUDIT — one card per fix journey, zero for a clean run.

Revival design pass §c.5 / risk h.5, Stage 1c.

The ruling this file defends: Rich's acts per feature are exactly three —
the spec word, the gate tap, the merge word. A fix journey adds **zero**.
So across a whole journey the machine may publish exactly ONE card (the
merge card), and a journey that finds nothing to fix must publish NONE.

Risk h.5 names the failure mode precisely: the Mode C planner's
vocabulary contains mid-chain approval statuses ("awaiting approval"),
and if any wiring resolved one of those by publishing a card to Rich, the
ruling is breached without anybody noticing. So this is a *replay* test —
a whole journey walked end to end through the real planner, the real
supervisor, the real merge-ready checkpoint and the real turn loop, with
fakes only at the I/O boundary — and the assertion is a COUNT.

Three counts are asserted every time:

* cards published — 1 for a fix journey with commits, 0 for a clean run;
* mid-chain gate submissions — the checkpoint is reached at most once, so
  every mid-cycle stage resolved through the subprocess dispatcher (a
  machine gate adapter), never through a card;
* the stages actually dispatched — proving the journey really walked the
  cycle rather than short-circuiting to the answer.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from forge.lifecycle.modes import BuildMode
from forge.pipeline.conductor_driver import (
    ConductorDriverDeps,
    ConductorRunOutcome,
    WaitWindow,
    drive_fix_journey,
)
from forge.pipeline.constitutional_guard import ConstitutionalGuard
from forge.pipeline.merge_ready_checkpoint import (
    GatesReport,
    GateStatus,
    MergeCardOutcome,
    MergeReadyCheckpointPublisher,
)
from forge.pipeline.mode_c_planner import (
    ModeCCyclePlanner,
    StageEntry as ModeCStageEntry,
)
from forge.pipeline.per_feature_sequencer import PerFeatureLoopSequencer
from forge.pipeline.stage_ordering_guard import StageOrderingGuard
from forge.pipeline.stage_taxonomy import StageClass
from forge.pipeline.supervisor import BuildState, Supervisor, TurnOutcome

BUILD_ID = "build-FEAT-AUDIT-20260731000000"


# ---------------------------------------------------------------------------
# The replay world: a stage_log that grows as stages are dispatched
# ---------------------------------------------------------------------------


@dataclass
class ReplayWorld:
    """One in-memory build whose history advances on every dispatch.

    Plays the part of the durable ``stage_log`` + commit probe. The
    ``script`` maps each dispatched stage to the entry it leaves behind,
    which is what makes the replay a real walk rather than a canned
    sequence: the planner re-reads the grown history each turn.
    """

    history: list[ModeCStageEntry] = field(default_factory=list)
    has_commits_value: bool = True
    approved_stages: set[StageClass] = field(default_factory=set)
    dispatched: list[StageClass] = field(default_factory=list)
    fix_tasks_for_first_review: tuple[str, ...] = ("FIX-1",)

    # -- history reader (ModeCHistoryReader protocol) ------------------
    def get_mode_c_history(self, build_id: str) -> Sequence[ModeCStageEntry]:
        return list(self.history)

    def has_commits(self, build_id: str) -> bool:
        return self.has_commits_value

    # -- ordering reader ----------------------------------------------
    def is_approved(
        self,
        build_id: str,
        stage: StageClass,
        feature_id: str | None = None,
    ) -> bool:
        return stage in self.approved_stages

    def feature_catalogue(self, build_id: str) -> list[str]:
        return []

    # -- the subprocess dispatcher (a MACHINE gate adapter) ------------
    async def dispatch(self, **kwargs: Any) -> Any:
        stage: StageClass = kwargs["stage"]
        self.dispatched.append(stage)
        if stage is StageClass.TASK_REVIEW:
            first_review = not any(
                entry.stage_class is StageClass.TASK_REVIEW
                for entry in self.history
            )
            self.history.append(
                ModeCStageEntry(
                    stage_class=StageClass.TASK_REVIEW,
                    status="approved",
                    fix_tasks=(
                        self.fix_tasks_for_first_review if first_review else ()
                    ),
                )
            )
        elif stage is StageClass.TASK_WORK:
            fix_task = kwargs.get("fix_task")
            self.history.append(
                ModeCStageEntry(
                    stage_class=StageClass.TASK_WORK,
                    status="approved",
                    fix_task_id=getattr(fix_task, "fix_task_id", None),
                )
            )
        self.approved_stages.add(stage)
        return {"status": "approved", "stage": stage.value}


@dataclass
class _FakeStateReader:
    def get_build_state(self, build_id: str) -> BuildState:
        return BuildState.RUNNING


@dataclass
class _FakeModeReader:
    def get_build_mode(self, build_id: str) -> BuildMode:
        return BuildMode.MODE_C


@dataclass
class _FakePerFeatureReader:
    def is_autobuild_approved(self, build_id: str, feature_id: str) -> bool:
        return False


@dataclass
class _FakeAsyncTaskReader:
    def list_autobuild_states(self, build_id: str) -> Iterable[Any]:
        return []


@dataclass
class _NeverConsultedModel:
    """The reasoning-model port. M0: a Mode C turn must never reach it."""

    consulted: int = 0

    def choose_dispatch(self, **kwargs: Any) -> Any:
        self.consulted += 1
        raise AssertionError(
            "the reasoning model was consulted on a Mode C turn — the "
            "conductor's fix journey runs at M0 = 0 frontier calls"
        )


@dataclass
class _Recorder:
    rows: list[dict[str, Any]] = field(default_factory=list)

    def record_turn(self, **kwargs: Any) -> None:
        self.rows.append(dict(kwargs))


@dataclass
class _UnusedDispatcher:
    async def __call__(self, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("a Mode C fix journey must not reach this dispatcher")


@dataclass
class CardCounter:
    """The audit's instrument: every card that would reach the owner."""

    cards: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, **kwargs: Any) -> Any:
        self.cards.append(dict(kwargs))
        return "approval-card-published"


@dataclass
class CountingGate:
    """Wraps the publisher so gate SUBMISSIONS are counted separately.

    A submission is "the checkpoint leg fired". A card is "the owner was
    asked something". The two counts differing is the whole point of the
    precondition: a red gate submits and publishes nothing.
    """

    inner: MergeReadyCheckpointPublisher
    submissions: list[dict[str, Any]] = field(default_factory=list)

    async def submit_decision(self, **kwargs: Any) -> Any:
        self.submissions.append(dict(kwargs))
        return await self.inner.submit_decision(**kwargs)


def _build_world(
    *,
    world: ReplayWorld,
    gate: Any,
) -> tuple[Supervisor, _NeverConsultedModel]:
    model = _NeverConsultedModel()
    supervisor = Supervisor(
        ordering_guard=StageOrderingGuard(),
        per_feature_sequencer=PerFeatureLoopSequencer(),
        constitutional_guard=ConstitutionalGuard(),
        state_reader=_FakeStateReader(),
        ordering_stage_log_reader=world,
        per_feature_stage_log_reader=_FakePerFeatureReader(),
        async_task_reader=_FakeAsyncTaskReader(),
        reasoning_model=model,
        turn_recorder=_Recorder(),
        specialist_dispatcher=_UnusedDispatcher(),
        subprocess_dispatcher=world.dispatch,
        autobuild_dispatcher=_UnusedDispatcher(),
        pr_review_gate=gate,
        build_mode_reader=_FakeModeReader(),
        mode_c_planner=ModeCCyclePlanner(),
        mode_c_history_reader=world,
    )
    return supervisor, model


class _ImmediateWait:
    """Every dispatched stage settles at once — this is a replay, not a wire."""

    def __init__(self) -> None:
        self.rounds = 0

    def window(self, build_id: str) -> WaitWindow:
        self.rounds += 1
        return WaitWindow(remaining_seconds=60.0, resolved=True)


def _drive(supervisor: Supervisor) -> Any:
    wait = _ImmediateWait()
    deps = ConductorDriverDeps(
        supervisor=supervisor,
        wait_window_reader=wait.window,
        subscribe_resume=None,
        max_turns=25,
    )
    # ``resolved=True`` short-circuits before the subscription, so no
    # broker-shaped seam is needed anywhere in this replay.
    deps.subscribe_resume = _unused_subscribe
    return asyncio.run(drive_fix_journey(BUILD_ID, deps))


async def _unused_subscribe(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover
    raise AssertionError("the replay resolves every wait from durable state")


# ---------------------------------------------------------------------------
# The audit
# ---------------------------------------------------------------------------


class TestCardCountAudit:
    def test_a_whole_fix_journey_publishes_exactly_one_card(self) -> None:
        counter = CardCounter()
        world = ReplayWorld(has_commits_value=True)
        gate = CountingGate(
            inner=MergeReadyCheckpointPublisher(
                publish_card=counter,
                gates_green_reader=lambda **_: GatesReport(
                    status=GateStatus.GREEN, detail="all gates green"
                ),
                has_commits_probe=lambda _bid: world.has_commits_value,
                branch_reader=lambda _bid: "fix/FEAT-AUDIT",
            )
        )
        supervisor, model = _build_world(world=world, gate=gate)

        report = _drive(supervisor)

        # THE COUNT.
        assert len(counter.cards) == 1, (
            "a whole fix journey must publish EXACTLY ONE card (the merge "
            f"card); published {len(counter.cards)}"
        )
        # The journey really walked the cycle.
        assert world.dispatched == [
            StageClass.TASK_REVIEW,
            StageClass.TASK_WORK,
            StageClass.TASK_REVIEW,
        ]
        # Every mid-chain approval resolved through the machine gate
        # adapter (the subprocess dispatcher), never through a card: the
        # checkpoint leg fired exactly once, at the end.
        assert len(gate.submissions) == 1
        # The loop STOPS on delivery — re-planning would re-publish the
        # card on every tick (act inflation, risk h.5).
        assert report.outcome is ConductorRunOutcome.DELIVERED
        assert report.last_report.outcome is TurnOutcome.DISPATCHED
        assert report.last_report.chosen_stage is StageClass.PULL_REQUEST_REVIEW
        assert report.last_report.dispatch_result.card_published is True
        # M0: no frontier call on the routine fix-journey path.
        assert model.consulted == 0

    def test_a_clean_review_with_no_commits_publishes_zero_cards(self) -> None:
        counter = CardCounter()
        world = ReplayWorld(has_commits_value=False, fix_tasks_for_first_review=())
        gate = CountingGate(
            inner=MergeReadyCheckpointPublisher(
                publish_card=counter,
                gates_green_reader=lambda **_: True,
                has_commits_probe=lambda _bid: world.has_commits_value,
            )
        )
        supervisor, _model = _build_world(world=world, gate=gate)

        async def terminal_handler(build: Any, history: Any, **kwargs: Any) -> Any:
            from forge.pipeline.terminal_handlers.mode_c import (
                ModeCTerminal,
                ModeCTerminalDecision,
            )

            return ModeCTerminalDecision(
                outcome=ModeCTerminal.CLEAN_REVIEW_NO_FIXES,
                rationale="mode-c-clean-review-no-fixes",
            )

        supervisor.mode_c_terminal_handler = terminal_handler

        report = _drive(supervisor)

        assert counter.cards == [], (
            "a clean run must publish ZERO cards — the owner hears about "
            "work only when there is a merge word to say"
        )
        assert gate.submissions == []
        assert world.dispatched == [StageClass.TASK_REVIEW]
        assert report.outcome is ConductorRunOutcome.COMPLETED

    def test_a_red_gate_journey_publishes_zero_cards(self) -> None:
        """The precondition's audit face: the leg fires, the owner is not asked."""
        counter = CardCounter()
        world = ReplayWorld(has_commits_value=True)
        gate = CountingGate(
            inner=MergeReadyCheckpointPublisher(
                publish_card=counter,
                gates_green_reader=lambda **_: GatesReport(
                    status=GateStatus.RED, failed_gates=("pytest",)
                ),
                has_commits_probe=lambda _bid: True,
            )
        )
        supervisor, _model = _build_world(world=world, gate=gate)

        report = _drive(supervisor)

        assert counter.cards == []
        assert len(gate.submissions) >= 1
        assert report.outcome in {
            ConductorRunOutcome.NOTHING_CHANGED,
            ConductorRunOutcome.TURN_CAP,
            ConductorRunOutcome.COMPLETED,
        }
        # Whatever the loop decided to do about it, no card went out.
        assert counter.cards == []

    def test_the_no_commit_belt_holds_even_when_the_planner_reaches_the_leg(
        self,
    ) -> None:
        counter = CardCounter()
        # Commits are claimed by the history reader (so the planner routes
        # to the checkpoint) but the checkpoint's own probe says no.
        world = ReplayWorld(has_commits_value=True)
        gate = CountingGate(
            inner=MergeReadyCheckpointPublisher(
                publish_card=counter,
                gates_green_reader=lambda **_: True,
                has_commits_probe=lambda _bid: False,
            )
        )
        supervisor, _model = _build_world(world=world, gate=gate)

        _drive(supervisor)

        assert counter.cards == []
        assert len(gate.submissions) >= 1
        assert (
            gate.submissions
            and asyncio.run(
                gate.inner.submit_decision(
                    build_id=BUILD_ID,
                    feature_id="",
                    auto_approve=False,
                    rationale="probe",
                )
            ).outcome
            is MergeCardOutcome.NO_COMMITS_SILENT
        )

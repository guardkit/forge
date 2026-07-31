"""The NO-PR re-point — one publisher behind all four call sites.

Revival design pass §c.2, Stage 1c.

Design pass §c enumerates four places where the supervisor calls
``pr_review_gate.submit_decision``:

1. ``_dispatch``'s router branch (Mode A / the generic path);
2. the Mode B post-autobuild ``PR_REVIEW`` route;
3. the Mode C planner-chosen ``PULL_REQUEST_REVIEW`` dispatch;
4. the Mode C terminal handler's ``PR_REVIEW`` route.

All four are now re-pointed at ONE implementation — the merge-ready
checkpoint — through a single funnel on the supervisor. This file proves
each of the four reaches it, that the funnel awaits an async publisher,
that a red-gate decision is never recorded as a dispatch, and — the
invariant that matters most — that **a synchronous, non-decision gate
(every pre-existing implementation and every test double) still records
DISPATCHED exactly as before.**

It also pins the silence: the three no-commit / clean / failed Mode C
terminal routes never touch the gate at all.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

import pytest

from forge.lifecycle.modes import BuildMode
from forge.pipeline.constitutional_guard import ConstitutionalGuard
from forge.pipeline.merge_ready_checkpoint import (
    GatesReport,
    GateStatus,
    MergeCardDecision,
    MergeCardOutcome,
    MergeReadyCheckpointPublisher,
    RedGateAction,
)
from forge.pipeline.mode_b_planner import ModeBChainPlanner
from forge.pipeline.mode_c_planner import (
    ModeCCyclePlanner,
    StageEntry as ModeCStageEntry,
)
from forge.pipeline.per_feature_sequencer import PerFeatureLoopSequencer
from forge.pipeline.stage_ordering_guard import StageOrderingGuard
from forge.pipeline.stage_taxonomy import StageClass
from forge.pipeline.supervisor import (
    BuildState,
    DispatchChoice,
    Supervisor,
    TurnOutcome,
)
from forge.pipeline.terminal_handlers import (
    PR_REVIEW as MODE_B_PR_REVIEW,
    ModeBPostAutobuild,
)
from forge.pipeline.terminal_handlers.mode_c import (
    ModeCTerminal as ModeCHandlerTerminal,
    ModeCTerminalDecision,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


@dataclass
class _StateReader:
    def get_build_state(self, build_id: str) -> BuildState:
        return BuildState.RUNNING


@dataclass
class _ModeReader:
    modes: dict[str, BuildMode] = field(default_factory=dict)

    def get_build_mode(self, build_id: str) -> BuildMode:
        return self.modes.get(build_id, BuildMode.MODE_A)


@dataclass
class _OrderingReader:
    approved: set[tuple[str, StageClass, str | None]] = field(default_factory=set)
    catalogues: dict[str, list[str]] = field(default_factory=dict)

    def is_approved(
        self, build_id: str, stage: StageClass, feature_id: str | None = None
    ) -> bool:
        return (build_id, stage, feature_id) in self.approved

    def feature_catalogue(self, build_id: str) -> list[str]:
        return list(self.catalogues.get(build_id, []))


@dataclass
class _PerFeatureReader:
    def is_autobuild_approved(self, build_id: str, feature_id: str) -> bool:
        return False


@dataclass
class _AsyncTaskReader:
    def list_autobuild_states(self, build_id: str) -> Iterable[Any]:
        return []


@dataclass
class _Model:
    choice: DispatchChoice | None = None

    def choose_dispatch(self, **kwargs: Any) -> DispatchChoice | None:
        return self.choice


@dataclass
class _Recorder:
    rows: list[dict[str, Any]] = field(default_factory=list)

    def record_turn(self, **kwargs: Any) -> None:
        self.rows.append(dict(kwargs))


@dataclass
class _Dispatcher:
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        return {"status": "ok"}


@dataclass
class _LegacySyncGate:
    """A pre-existing, SYNCHRONOUS gate — the backwards-compat subject."""

    submissions: list[dict[str, Any]] = field(default_factory=list)

    def submit_decision(self, **kwargs: Any) -> Any:
        self.submissions.append(dict(kwargs))
        return {"gate": "pr-review", "status": "submitted"}


@dataclass
class _ModeBEntry:
    """Concrete stand-in for the Mode B ``StageEntry`` Protocol."""

    stage: StageClass
    status: str
    feature_id: str | None = "FEAT-B"
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class _ModeBHistory:
    histories: dict[str, list[_ModeBEntry]] = field(default_factory=dict)

    def get_mode_b_history(self, build_id: str) -> Sequence[Any]:
        return list(self.histories.get(build_id, []))


@dataclass
class _ModeCHistory:
    histories: dict[str, list[ModeCStageEntry]] = field(default_factory=dict)
    commits: dict[str, bool] = field(default_factory=dict)

    def get_mode_c_history(self, build_id: str) -> Sequence[ModeCStageEntry]:
        return list(self.histories.get(build_id, []))

    def has_commits(self, build_id: str) -> bool:
        return self.commits.get(build_id, False)


def _supervisor(gate: Any) -> tuple[Supervisor, dict[str, Any]]:
    doubles: dict[str, Any] = {
        "mode_reader": _ModeReader(),
        "ordering_reader": _OrderingReader(),
        "model": _Model(),
        "recorder": _Recorder(),
        "subprocess": _Dispatcher(),
        "specialist": _Dispatcher(),
        "autobuild": _Dispatcher(),
        "mode_b_history": _ModeBHistory(),
        "mode_c_history": _ModeCHistory(),
        "gate": gate,
    }
    supervisor = Supervisor(
        ordering_guard=StageOrderingGuard(),
        per_feature_sequencer=PerFeatureLoopSequencer(),
        constitutional_guard=ConstitutionalGuard(),
        state_reader=_StateReader(),
        ordering_stage_log_reader=doubles["ordering_reader"],
        per_feature_stage_log_reader=_PerFeatureReader(),
        async_task_reader=_AsyncTaskReader(),
        reasoning_model=doubles["model"],
        turn_recorder=doubles["recorder"],
        specialist_dispatcher=doubles["specialist"],
        subprocess_dispatcher=doubles["subprocess"],
        autobuild_dispatcher=doubles["autobuild"],
        pr_review_gate=gate,
        build_mode_reader=doubles["mode_reader"],
        mode_b_planner=ModeBChainPlanner(),
        mode_b_history_reader=doubles["mode_b_history"],
        mode_c_planner=ModeCCyclePlanner(),
        mode_c_history_reader=doubles["mode_c_history"],
    )
    return supervisor, doubles


def _checkpoint(**overrides: Any) -> MergeReadyCheckpointPublisher:
    kwargs: dict[str, Any] = {
        "publish_card": _AsyncCard(),
        "gates_green_reader": lambda **_: True,
    }
    kwargs.update(overrides)
    return MergeReadyCheckpointPublisher(**kwargs)


def _approve_up_to_the_checkpoint(doubles: dict[str, Any], build_id: str) -> None:
    """Make the Mode A checkpoint dispatchable.

    Mode A treats the checkpoint as a build-wide fan-out: it needs
    AUTOBUILD approved for EVERY feature in the catalogue, and an empty
    catalogue is never dispatchable (there is nothing to review).
    """
    from forge.pipeline.mode_chains_data import MODE_A_CHAIN

    reader = doubles["ordering_reader"]
    reader.catalogues[build_id] = ["FEAT-A"]
    for stage in MODE_A_CHAIN:
        if stage is StageClass.PULL_REQUEST_REVIEW:
            continue
        reader.approved.add((build_id, stage, None))
        reader.approved.add((build_id, stage, "FEAT-A"))


@dataclass
class _AsyncCard:
    cards: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, **kwargs: Any) -> Any:
        self.cards.append(dict(kwargs))
        return "published"


# ---------------------------------------------------------------------------
# 1. The four call sites reach the one publisher
# ---------------------------------------------------------------------------


class TestAllFourCallSitesReachThePublisher:
    def test_call_site_1_the_dispatch_router_branch(self) -> None:
        card = _AsyncCard()
        supervisor, doubles = _supervisor(_checkpoint(publish_card=card))
        doubles["model"].choice = DispatchChoice(
            stage=StageClass.PULL_REQUEST_REVIEW, rationale="ready"
        )
        _approve_up_to_the_checkpoint(doubles, "build-A")

        report = _run(supervisor.next_turn("build-A"))

        assert isinstance(report.dispatch_result, MergeCardDecision)
        assert report.dispatch_result.card_published is True
        assert len(card.cards) == 1

    def test_call_site_2_mode_b_post_autobuild(self) -> None:
        card = _AsyncCard()
        supervisor, doubles = _supervisor(_checkpoint(publish_card=card))
        doubles["mode_reader"].modes["build-B"] = BuildMode.MODE_B
        doubles["mode_b_history"].histories["build-B"] = [
            _ModeBEntry(stage=StageClass.FEATURE_SPEC, status="approved"),
            _ModeBEntry(stage=StageClass.FEATURE_PLAN, status="approved"),
            _ModeBEntry(
                stage=StageClass.AUTOBUILD,
                status="approved",
                feature_id="FEAT-B",
                details={"changed_files_count": 3},
            ),
        ]

        def post_autobuild(build: Any, history: Any) -> ModeBPostAutobuild:
            return ModeBPostAutobuild(
                route=MODE_B_PR_REVIEW,
                rationale="autobuild produced a diff",
                feature_id="FEAT-B",
                changed_files_count=3,
            )

        supervisor.mode_b_post_autobuild = post_autobuild

        report = _run(supervisor.next_turn("build-B"))

        assert isinstance(report.dispatch_result, MergeCardDecision)
        assert report.chosen_stage is StageClass.PULL_REQUEST_REVIEW
        assert len(card.cards) == 1

    def test_call_site_3_mode_c_planner_chosen_dispatch(self) -> None:
        card = _AsyncCard()
        supervisor, doubles = _supervisor(_checkpoint(publish_card=card))
        doubles["mode_reader"].modes["build-C"] = BuildMode.MODE_C
        doubles["mode_c_history"].histories["build-C"] = [
            ModeCStageEntry(
                stage_class=StageClass.TASK_REVIEW,
                status="approved",
                fix_tasks=("FIX-1",),
            ),
            ModeCStageEntry(
                stage_class=StageClass.TASK_WORK,
                status="approved",
                fix_task_id="FIX-1",
            ),
            ModeCStageEntry(
                stage_class=StageClass.TASK_REVIEW, status="approved", fix_tasks=()
            ),
        ]
        doubles["mode_c_history"].commits["build-C"] = True
        doubles["ordering_reader"].approved.add(
            ("build-C", StageClass.TASK_REVIEW, None)
        )
        doubles["ordering_reader"].approved.add(
            ("build-C", StageClass.TASK_WORK, None)
        )

        report = _run(supervisor.next_turn("build-C"))

        assert report.chosen_stage is StageClass.PULL_REQUEST_REVIEW
        assert isinstance(report.dispatch_result, MergeCardDecision)
        assert len(card.cards) == 1

    def test_call_site_4_mode_c_terminal_route(self) -> None:
        card = _AsyncCard()
        supervisor, doubles = _supervisor(_checkpoint(publish_card=card))
        doubles["mode_reader"].modes["build-CT"] = BuildMode.MODE_C
        doubles["mode_c_history"].histories["build-CT"] = [
            ModeCStageEntry(
                stage_class=StageClass.TASK_REVIEW, status="approved", fix_tasks=()
            )
        ]
        doubles["mode_c_history"].commits["build-CT"] = False

        async def handler(build: Any, history: Any, **kwargs: Any) -> Any:
            return ModeCTerminalDecision(
                outcome=ModeCHandlerTerminal.PR_REVIEW,
                has_commits=True,
                rationale="mode-c-commits-present",
            )

        supervisor.mode_c_terminal_handler = handler

        report = _run(supervisor.next_turn("build-CT"))

        assert report.chosen_stage is StageClass.PULL_REQUEST_REVIEW
        assert isinstance(report.dispatch_result, MergeCardDecision)
        assert len(card.cards) == 1


# ---------------------------------------------------------------------------
# 2. A red gate is never recorded as a dispatch
# ---------------------------------------------------------------------------


class TestRedGateIsNotADispatch:
    def _mode_c_at_the_checkpoint(self, gate: Any) -> tuple[Supervisor, str]:
        supervisor, doubles = _supervisor(gate)
        doubles["mode_reader"].modes["build-RG"] = BuildMode.MODE_C
        doubles["mode_c_history"].histories["build-RG"] = [
            ModeCStageEntry(
                stage_class=StageClass.TASK_REVIEW,
                status="approved",
                fix_tasks=("FIX-1",),
            ),
            ModeCStageEntry(
                stage_class=StageClass.TASK_WORK,
                status="approved",
                fix_task_id="FIX-1",
            ),
            ModeCStageEntry(
                stage_class=StageClass.TASK_REVIEW, status="approved", fix_tasks=()
            ),
        ]
        doubles["mode_c_history"].commits["build-RG"] = True
        doubles["ordering_reader"].approved.add(
            ("build-RG", StageClass.TASK_REVIEW, None)
        )
        doubles["ordering_reader"].approved.add(
            ("build-RG", StageClass.TASK_WORK, None)
        )
        return supervisor, "build-RG"

    def test_a_red_gate_that_loops_back_records_waiting(self) -> None:
        card = _AsyncCard()
        supervisor, build_id = self._mode_c_at_the_checkpoint(
            _checkpoint(
                publish_card=card,
                gates_green_reader=lambda **_: GatesReport(
                    status=GateStatus.RED, failed_gates=("pytest",)
                ),
            )
        )

        report = _run(supervisor.next_turn(build_id))

        assert report.outcome is TurnOutcome.WAITING
        assert card.cards == []

    def test_a_red_gate_that_terminates_records_terminal(self) -> None:
        card = _AsyncCard()
        supervisor, build_id = self._mode_c_at_the_checkpoint(
            _checkpoint(
                publish_card=card,
                gates_green_reader=lambda **_: GatesReport(status=GateStatus.RED),
                red_gate_action=lambda _b, _g: RedGateAction.TERMINATE_FAILED,
            )
        )

        report = _run(supervisor.next_turn(build_id))

        assert report.outcome is TurnOutcome.TERMINAL
        assert card.cards == []

    def test_a_no_commit_checkpoint_records_terminal_and_no_card(self) -> None:
        card = _AsyncCard()
        supervisor, build_id = self._mode_c_at_the_checkpoint(
            _checkpoint(publish_card=card, has_commits_probe=lambda _b: False)
        )

        report = _run(supervisor.next_turn(build_id))

        assert report.outcome is TurnOutcome.TERMINAL
        assert report.dispatch_result.outcome is MergeCardOutcome.NO_COMMITS_SILENT
        assert card.cards == []

    def test_a_green_gate_records_dispatched(self) -> None:
        supervisor, build_id = self._mode_c_at_the_checkpoint(_checkpoint())

        report = _run(supervisor.next_turn(build_id))

        assert report.outcome is TurnOutcome.DISPATCHED


# ---------------------------------------------------------------------------
# 3. THE BACKWARDS-COMPAT RAIL
# ---------------------------------------------------------------------------


class TestLegacySyncGateIsUnchanged:
    """Every pre-existing gate implementation behaves exactly as before."""

    def test_mode_c_dispatch_with_a_sync_gate_still_records_dispatched(
        self,
    ) -> None:
        gate = _LegacySyncGate()
        supervisor, doubles = _supervisor(gate)
        doubles["mode_reader"].modes["build-L"] = BuildMode.MODE_C
        doubles["mode_c_history"].histories["build-L"] = [
            ModeCStageEntry(
                stage_class=StageClass.TASK_REVIEW,
                status="approved",
                fix_tasks=("FIX-1",),
            ),
            ModeCStageEntry(
                stage_class=StageClass.TASK_WORK,
                status="approved",
                fix_task_id="FIX-1",
            ),
            ModeCStageEntry(
                stage_class=StageClass.TASK_REVIEW, status="approved", fix_tasks=()
            ),
        ]
        doubles["mode_c_history"].commits["build-L"] = True
        doubles["ordering_reader"].approved.add(
            ("build-L", StageClass.TASK_REVIEW, None)
        )
        doubles["ordering_reader"].approved.add(
            ("build-L", StageClass.TASK_WORK, None)
        )

        report = _run(supervisor.next_turn("build-L"))

        assert report.outcome is TurnOutcome.DISPATCHED
        assert report.dispatch_result == {
            "gate": "pr-review",
            "status": "submitted",
        }
        assert gate.submissions == [
            {
                "build_id": "build-L",
                "feature_id": "",
                "auto_approve": False,
                "rationale": report.rationale,
            }
        ]

    def test_the_dispatch_router_with_a_sync_gate_still_records_dispatched(
        self,
    ) -> None:
        gate = _LegacySyncGate()
        supervisor, doubles = _supervisor(gate)
        doubles["model"].choice = DispatchChoice(
            stage=StageClass.PULL_REQUEST_REVIEW, rationale="ready"
        )
        _approve_up_to_the_checkpoint(doubles, "build-LA")

        report = _run(supervisor.next_turn("build-LA"))

        assert report.outcome is TurnOutcome.DISPATCHED
        assert len(gate.submissions) == 1


# ---------------------------------------------------------------------------
# 4. The silence of the no-commit terminals
# ---------------------------------------------------------------------------


class TestNoCommitTerminalsStaySilent:
    @pytest.mark.parametrize(
        "terminal",
        [
            ModeCHandlerTerminal.CLEAN_REVIEW_NO_FIXES,
            ModeCHandlerTerminal.CLEAN_REVIEW_NO_COMMITS,
            ModeCHandlerTerminal.FAILED,
        ],
    )
    def test_the_three_silent_terminals_never_touch_the_gate(
        self, terminal: ModeCHandlerTerminal
    ) -> None:
        gate = _LegacySyncGate()
        supervisor, doubles = _supervisor(gate)
        doubles["mode_reader"].modes["build-S"] = BuildMode.MODE_C
        doubles["mode_c_history"].histories["build-S"] = [
            ModeCStageEntry(
                stage_class=StageClass.TASK_REVIEW, status="approved", fix_tasks=()
            )
        ]
        doubles["mode_c_history"].commits["build-S"] = False

        async def handler(build: Any, history: Any, **kwargs: Any) -> Any:
            return ModeCTerminalDecision(
                outcome=terminal,
                has_commits=False,
                rationale=f"{terminal.value} rationale",
                failure_reason=(
                    "all task-work failed"
                    if terminal is ModeCHandlerTerminal.FAILED
                    else None
                ),
            )

        supervisor.mode_c_terminal_handler = handler

        report = _run(supervisor.next_turn("build-S"))

        assert report.outcome is TurnOutcome.TERMINAL
        assert gate.submissions == []

"""Tests for mode-aware dispatch in ``Supervisor.next_turn`` (TASK-MBC8-008).

Validates the FEAT-FORGE-008 wiring that threads ``Build.mode`` through
the supervisor's reasoning-loop turn so each turn dispatches via the
correct planner / terminal handler:

- Mode A → existing :class:`PerFeatureLoopSequencer` + Mode A guards.
- Mode B → :class:`ModeBChainPlanner` for the next stage and
  :func:`evaluate_post_autobuild` post-AUTOBUILD.
- Mode C → :class:`ModeCCyclePlanner` for the next stage / fix-task and
  :func:`evaluate_terminal` at cycle end.

The :class:`Supervisor.next_turn` coroutine is driven through
``asyncio.run`` (mirroring ``test_terminal_handlers_mode_c.py``) so the
suite remains zero-dependency on ``pytest-asyncio`` — the project does
not declare it.

Acceptance-criteria coverage map (TASK-MBC8-008):

- AC: ``next_turn`` reads ``build.mode`` and dispatches to the right
  planner — :class:`TestModeDispatch`.
- AC: Mode B subprocess stages route via ``dispatch_subprocess_stage``
  — :class:`TestModeBSubprocessRouting`.
- AC: Mode B AUTOBUILD routes via ``dispatch_autobuild_async`` —
  :class:`TestModeBAutobuildRouting`.
- AC: Mode B post-autobuild routes (PR_REVIEW / NO_OP / FAILED) —
  :class:`TestModeBPostAutobuildRouting`.
- AC: Mode C TASK_REVIEW / TASK_WORK route via the subprocess
  dispatcher — :class:`TestModeCSubprocessRouting`.
- AC: Mode C TASK_WORK threads the fix-task ref —
  :class:`TestModeCFixTaskInjection`.
- AC: Mode C PR_REVIEW path on commits — :class:`TestModeCPRReviewRoute`.
- AC: ``StageOrderingGuard`` invoked with per-mode prerequisites —
  :class:`TestPerModePrerequisites`.
- AC: ``ConstitutionalGuard`` unchanged in every mode —
  :class:`TestConstitutionalGuardUnchanged`.
- AC: Concurrent Mode A / B / C builds — no cross-talk —
  :class:`TestConcurrentMixedModeBuilds`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Iterable, Mapping, Sequence

import pytest

from forge.lifecycle.modes import BuildMode
from forge.pipeline.constitutional_guard import ConstitutionalGuard
from forge.pipeline.mode_b_planner import (
    APPROVED as MODE_B_APPROVED,
    HARD_STOP as MODE_B_HARD_STOP,
    ModeBChainPlanner,
    StageEntry as ModeBStageEntry,
)
from forge.config.models import BudgetGuards
from forge.pipeline.mode_c_planner import (
    FixTaskRef as ModeCFixTaskRef,
    ModeCCyclePlanner,
    ModeCPlan,
    StageEntry as ModeCStageEntry,
)
from forge.pipeline.mode_chains_data import (
    MODE_B_CHAIN,
    MODE_B_PREREQUISITES,
    MODE_C_CHAIN,
    MODE_C_PREREQUISITES,
)
from forge.pipeline.per_feature_sequencer import PerFeatureLoopSequencer
from forge.pipeline.stage_ordering_guard import StageOrderingGuard
from forge.pipeline.stage_taxonomy import StageClass
from forge.pipeline.supervisor import (
    BuildModeReader,
    BuildState,
    DispatchChoice,
    ModeBHistoryReader,
    ModeCHistoryReader,
    Supervisor,
    TurnOutcome,
    TurnReport,
)
from forge.pipeline.terminal_handlers import (
    NO_OP as MODE_B_NO_OP,
    PR_REVIEW as MODE_B_PR_REVIEW,
    ROUTE_FAILED as MODE_B_ROUTE_FAILED,
    ModeBPostAutobuild,
)
from forge.pipeline.terminal_handlers.mode_c import (
    CommitProbeResult,
    ModeCTerminal as ModeCHandlerTerminal,
    ModeCTerminalDecision,
)

# ---------------------------------------------------------------------------
# Coroutine driver — replaces pytest-asyncio
# ---------------------------------------------------------------------------


def _run(coro: Awaitable[TurnReport]) -> TurnReport:
    """Drive an async ``next_turn`` coroutine to completion."""
    return asyncio.run(coro)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# In-memory test doubles
# ---------------------------------------------------------------------------


@dataclass
class FakeStateReader:
    states: dict[str, BuildState] = field(default_factory=dict)

    def get_build_state(self, build_id: str) -> BuildState:
        return self.states.get(build_id, BuildState.RUNNING)


@dataclass
class FakeBuildModeReader:
    modes: dict[str, BuildMode] = field(default_factory=dict)

    def get_build_mode(self, build_id: str) -> BuildMode:
        return self.modes.get(build_id, BuildMode.MODE_A)


@dataclass
class FakeOrderingReader:
    approved: set[tuple[str, StageClass, str | None]] = field(default_factory=set)
    catalogues: dict[str, list[str]] = field(default_factory=dict)
    prerequisites_calls: list[Mapping[StageClass, Any] | None] = field(
        default_factory=list
    )

    def is_approved(
        self,
        build_id: str,
        stage: StageClass,
        feature_id: str | None = None,
    ) -> bool:
        return (build_id, stage, feature_id) in self.approved

    def feature_catalogue(self, build_id: str) -> list[str]:
        return list(self.catalogues.get(build_id, []))


@dataclass
class FakePerFeatureReader:
    approved_autobuilds: set[tuple[str, str]] = field(default_factory=set)

    def is_autobuild_approved(self, build_id: str, feature_id: str) -> bool:
        return (build_id, feature_id) in self.approved_autobuilds


@dataclass
class FakeAutobuildState:
    feature_id: str
    lifecycle: str = "running_wave"


@dataclass
class FakeAsyncTaskReader:
    states_by_build: dict[str, list[FakeAutobuildState]] = field(default_factory=dict)

    def list_autobuild_states(self, build_id: str) -> Iterable[FakeAutobuildState]:
        return list(self.states_by_build.get(build_id, []))


@dataclass
class RecordingReasoningModel:
    next_choice: DispatchChoice | None = None

    def choose_dispatch(
        self,
        *,
        build_id: str,
        build_state: BuildState,
        permitted_stages: frozenset[StageClass],
        stage_hints: Mapping[StageClass, str],
        feature_catalogue: tuple[str, ...],
    ) -> DispatchChoice | None:
        return self.next_choice


@dataclass
class RecordingTurnRecorder:
    rows: list[dict[str, Any]] = field(default_factory=list)

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
        self.rows.append(
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


@dataclass
class RecordingAsyncDispatcher:
    label: str
    calls: list[dict[str, Any]] = field(default_factory=list)
    return_value: Any = None

    def __post_init__(self) -> None:
        if self.return_value is None:
            self.return_value = {"dispatcher": self.label, "status": "ok"}

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append({**kwargs})
        return self.return_value


@dataclass
class RecordingSyncDispatcher:
    label: str
    calls: list[dict[str, Any]] = field(default_factory=list)
    return_value: Any = None

    def __post_init__(self) -> None:
        if self.return_value is None:
            self.return_value = {"dispatcher": self.label, "status": "ok"}

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append({**kwargs})
        return self.return_value


@dataclass
class RecordingPRReviewGate:
    submissions: list[dict[str, Any]] = field(default_factory=list)
    return_value: Any = None

    def __post_init__(self) -> None:
        if self.return_value is None:
            self.return_value = {"gate": "pr-review", "status": "submitted"}

    def submit_decision(
        self,
        *,
        build_id: str,
        feature_id: str,
        auto_approve: bool,
        rationale: str,
    ) -> Any:
        self.submissions.append(
            {
                "build_id": build_id,
                "feature_id": feature_id,
                "auto_approve": auto_approve,
                "rationale": rationale,
            }
        )
        return self.return_value


@dataclass
class FakeModeBHistoryReader:
    histories: dict[str, list[ModeBStageEntry]] = field(default_factory=dict)

    def get_mode_b_history(self, build_id: str) -> Sequence[ModeBStageEntry]:
        return list(self.histories.get(build_id, []))


@dataclass
class FakeModeBStageEntry:
    """Dataclass that satisfies the Mode B ``StageEntry`` Protocol."""

    stage: StageClass
    status: str
    feature_id: str | None = "FEAT-X"
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class FakeModeCHistoryReader:
    histories: dict[str, list[ModeCStageEntry]] = field(default_factory=dict)
    commits: dict[str, bool] = field(default_factory=dict)

    def get_mode_c_history(self, build_id: str) -> Sequence[ModeCStageEntry]:
        return list(self.histories.get(build_id, []))

    def has_commits(self, build_id: str) -> bool:
        return self.commits.get(build_id, False)


# ---------------------------------------------------------------------------
# Fixture: assembled Supervisor with full Mode B / Mode C wiring
# ---------------------------------------------------------------------------


def _build_supervisor() -> tuple[Supervisor, dict[str, Any]]:
    """Construct a Supervisor wired for every mode plus its test doubles."""
    state_reader = FakeStateReader()
    mode_reader = FakeBuildModeReader()
    ordering_reader = FakeOrderingReader()
    per_feature_reader = FakePerFeatureReader()
    async_task_reader = FakeAsyncTaskReader()
    reasoning_model = RecordingReasoningModel()
    turn_recorder = RecordingTurnRecorder()
    specialist = RecordingAsyncDispatcher(label="specialist")
    subprocess_disp = RecordingAsyncDispatcher(label="subprocess")
    # TASK-FORGE-FRR-F010G: the autobuild dispatcher is now async (the
    # launch path awaits ``StructuredTool.coroutine`` so the
    # autobuild_runner registration can run URL-less in-process).
    autobuild_disp = RecordingAsyncDispatcher(label="autobuild_async")
    pr_gate = RecordingPRReviewGate()
    mode_b_history = FakeModeBHistoryReader()
    mode_c_history = FakeModeCHistoryReader()
    captured_fix_tasks: list[Any] = []

    def fix_task_context_builder(
        stage: StageClass, build_id: str, fix_task: Any
    ) -> Mapping[str, Any]:
        captured_fix_tasks.append(
            {"stage": stage, "build_id": build_id, "fix_task": fix_task}
        )
        return {"--fix-task": fix_task.fix_task_id}

    supervisor = Supervisor(
        ordering_guard=StageOrderingGuard(),
        per_feature_sequencer=PerFeatureLoopSequencer(),
        constitutional_guard=ConstitutionalGuard(),
        state_reader=state_reader,
        ordering_stage_log_reader=ordering_reader,
        per_feature_stage_log_reader=per_feature_reader,
        async_task_reader=async_task_reader,
        reasoning_model=reasoning_model,
        turn_recorder=turn_recorder,
        specialist_dispatcher=specialist,
        subprocess_dispatcher=subprocess_disp,
        autobuild_dispatcher=autobuild_disp,
        pr_review_gate=pr_gate,
        build_mode_reader=mode_reader,
        mode_b_planner=ModeBChainPlanner(),
        mode_b_history_reader=mode_b_history,
        mode_c_planner=ModeCCyclePlanner(),
        mode_c_history_reader=mode_c_history,
        fix_task_context_builder=fix_task_context_builder,
    )
    return supervisor, {
        "state_reader": state_reader,
        "mode_reader": mode_reader,
        "ordering_reader": ordering_reader,
        "per_feature_reader": per_feature_reader,
        "async_task_reader": async_task_reader,
        "reasoning_model": reasoning_model,
        "turn_recorder": turn_recorder,
        "specialist": specialist,
        "subprocess": subprocess_disp,
        "autobuild": autobuild_disp,
        "pr_gate": pr_gate,
        "mode_b_history": mode_b_history,
        "mode_c_history": mode_c_history,
        "captured_fix_tasks": captured_fix_tasks,
    }


# ---------------------------------------------------------------------------
# AC: build.mode drives planner selection
# ---------------------------------------------------------------------------


class TestModeDispatch:
    """AC: ``next_turn`` reads ``build.mode`` and dispatches accordingly."""

    def test_mode_a_default_uses_existing_reasoning_model_path(self) -> None:
        # No build_mode_reader → defaults to Mode A; reasoning model is
        # asked for a dispatch.
        supervisor, doubles = _build_supervisor()
        doubles["mode_reader"].modes["build-A"] = BuildMode.MODE_A
        doubles["reasoning_model"].next_choice = DispatchChoice(
            stage=StageClass.PRODUCT_OWNER, rationale="kick off mode A"
        )

        report = _run(supervisor.next_turn("build-A"))

        assert report.outcome is TurnOutcome.DISPATCHED
        assert report.chosen_stage is StageClass.PRODUCT_OWNER
        # Mode A path uses the specialist dispatcher.
        assert len(doubles["specialist"].calls) == 1

    def test_mode_b_skips_reasoning_model_and_uses_planner(self) -> None:
        # Mode B planner returns FEATURE_SPEC for an empty history; the
        # reasoning model must NOT be consulted.
        supervisor, doubles = _build_supervisor()
        doubles["mode_reader"].modes["build-B"] = BuildMode.MODE_B
        doubles["ordering_reader"].catalogues["build-B"] = ["FEAT-1"]
        doubles["mode_b_history"].histories["build-B"] = []
        # Sentinel choice — used only if the supervisor incorrectly
        # consults the reasoning model.
        doubles["reasoning_model"].next_choice = DispatchChoice(
            stage=StageClass.PRODUCT_OWNER, rationale="should not be used"
        )

        report = _run(supervisor.next_turn("build-B"))

        assert report.outcome is TurnOutcome.DISPATCHED
        assert report.chosen_stage is StageClass.FEATURE_SPEC
        # Specialist dispatcher must NOT have been invoked (Mode B
        # forbids product-owner / architect).
        assert doubles["specialist"].calls == []
        assert len(doubles["subprocess"].calls) == 1

    def test_mode_c_skips_reasoning_model_and_uses_cycle_planner(self) -> None:
        supervisor, doubles = _build_supervisor()
        doubles["mode_reader"].modes["build-C"] = BuildMode.MODE_C
        doubles["mode_c_history"].histories["build-C"] = []

        report = _run(supervisor.next_turn("build-C"))

        assert report.outcome is TurnOutcome.DISPATCHED
        assert report.chosen_stage is StageClass.TASK_REVIEW
        assert len(doubles["subprocess"].calls) == 1


# ---------------------------------------------------------------------------
# AC: Mode B subprocess stages route via dispatch_subprocess_stage
# ---------------------------------------------------------------------------


class TestModeBSubprocessRouting:
    """AC: Mode B FEATURE_SPEC + FEATURE_PLAN dispatch via the subprocess dispatcher."""

    def test_feature_spec_dispatches_subprocess(self) -> None:
        supervisor, doubles = _build_supervisor()
        doubles["mode_reader"].modes["build-B1"] = BuildMode.MODE_B
        doubles["ordering_reader"].catalogues["build-B1"] = ["FEAT-1"]

        report = _run(supervisor.next_turn("build-B1"))

        assert report.outcome is TurnOutcome.DISPATCHED
        assert report.chosen_stage is StageClass.FEATURE_SPEC
        assert doubles["subprocess"].calls[0]["stage"] is StageClass.FEATURE_SPEC

    def test_feature_plan_dispatches_subprocess_after_feature_spec_approved(
        self,
    ) -> None:
        supervisor, doubles = _build_supervisor()
        doubles["mode_reader"].modes["build-B2"] = BuildMode.MODE_B
        doubles["ordering_reader"].catalogues["build-B2"] = ["FEAT-1"]
        doubles["ordering_reader"].approved.add(
            ("build-B2", StageClass.FEATURE_SPEC, "FEAT-1")
        )
        doubles["mode_b_history"].histories["build-B2"] = [
            FakeModeBStageEntry(
                stage=StageClass.FEATURE_SPEC,
                status=MODE_B_APPROVED,
                feature_id="FEAT-1",
                details={"artefact_paths": ["plan.md"]},
            ),
        ]

        report = _run(supervisor.next_turn("build-B2"))

        assert report.outcome is TurnOutcome.DISPATCHED
        assert report.chosen_stage is StageClass.FEATURE_PLAN
        assert doubles["subprocess"].calls[0]["stage"] is StageClass.FEATURE_PLAN
        assert doubles["subprocess"].calls[0]["feature_id"] == "FEAT-1"


# ---------------------------------------------------------------------------
# AC: Mode B AUTOBUILD routes via autobuild_async + sequencer fires
# ---------------------------------------------------------------------------


class TestModeBAutobuildRouting:
    """AC: Mode B AUTOBUILD routes via ``dispatch_autobuild_async``."""

    def _prime_through_feature_plan(
        self,
        doubles: dict[str, Any],
        build_id: str,
    ) -> None:
        doubles["ordering_reader"].catalogues[build_id] = ["FEAT-1"]
        doubles["ordering_reader"].approved.add(
            (build_id, StageClass.FEATURE_SPEC, "FEAT-1")
        )
        doubles["ordering_reader"].approved.add(
            (build_id, StageClass.FEATURE_PLAN, "FEAT-1")
        )
        doubles["mode_b_history"].histories[build_id] = [
            FakeModeBStageEntry(
                stage=StageClass.FEATURE_SPEC,
                status=MODE_B_APPROVED,
                feature_id="FEAT-1",
                details={"artefact_paths": ["spec.md"]},
            ),
            FakeModeBStageEntry(
                stage=StageClass.FEATURE_PLAN,
                status=MODE_B_APPROVED,
                feature_id="FEAT-1",
                details={"artefact_paths": ["plan.md"]},
            ),
        ]

    def test_autobuild_dispatches_via_autobuild_async(self) -> None:
        supervisor, doubles = _build_supervisor()
        doubles["mode_reader"].modes["build-B3"] = BuildMode.MODE_B
        self._prime_through_feature_plan(doubles, "build-B3")

        report = _run(supervisor.next_turn("build-B3"))

        assert report.outcome is TurnOutcome.DISPATCHED
        assert report.chosen_stage is StageClass.AUTOBUILD
        assert len(doubles["autobuild"].calls) == 1
        assert doubles["autobuild"].calls[0]["feature_id"] == "FEAT-1"
        # Subprocess dispatcher must NOT have been used.
        assert doubles["subprocess"].calls == []

    def test_per_feature_sequencer_blocks_autobuild_when_sibling_in_flight(
        self,
    ) -> None:
        # Per-feature sequencer skips ``state.feature_id == feature_id``
        # ("a feature does not block itself"). To exercise the blocking
        # path we record a *different* feature's autobuild in flight on
        # the same build — defensive coverage for the rare case where
        # async_tasks records a stale row from a sibling feature.
        supervisor, doubles = _build_supervisor()
        doubles["mode_reader"].modes["build-B4"] = BuildMode.MODE_B
        self._prime_through_feature_plan(doubles, "build-B4")
        # Inject a *different* feature's autobuild in flight — the
        # sequencer's sibling check fires only when feature_id differs
        # from the dispatching feature's id.
        doubles["async_task_reader"].states_by_build["build-B4"] = [
            FakeAutobuildState(feature_id="FEAT-OTHER"),
        ]

        report = _run(supervisor.next_turn("build-B4"))

        assert report.outcome is TurnOutcome.WAITING_PRIOR_AUTOBUILD
        assert doubles["autobuild"].calls == []


# ---------------------------------------------------------------------------
# AC: Mode B post-autobuild routes
# ---------------------------------------------------------------------------


class TestModeBPostAutobuildRouting:
    """AC: ``ModeBNoDiffTerminal.evaluate_post_autobuild`` after AUTOBUILD."""

    def _approved_autobuild_history(
        self,
        *,
        changed_files_count: int,
        feature_id: str = "FEAT-1",
    ) -> list[ModeBStageEntry]:
        return [
            FakeModeBStageEntry(
                stage=StageClass.FEATURE_SPEC,
                status=MODE_B_APPROVED,
                feature_id=feature_id,
                details={"artefact_paths": ["spec.md"]},
            ),
            FakeModeBStageEntry(
                stage=StageClass.FEATURE_PLAN,
                status=MODE_B_APPROVED,
                feature_id=feature_id,
                details={"artefact_paths": ["plan.md"]},
            ),
            FakeModeBStageEntry(
                stage=StageClass.AUTOBUILD,
                status=MODE_B_APPROVED,
                feature_id=feature_id,
                details={"changed_files_count": changed_files_count},
            ),
        ]

    def test_post_autobuild_with_diff_advances_to_pr_review(self) -> None:
        supervisor, doubles = _build_supervisor()
        doubles["mode_reader"].modes["build-PR"] = BuildMode.MODE_B
        doubles["ordering_reader"].catalogues["build-PR"] = ["FEAT-1"]
        doubles["ordering_reader"].approved.update(
            {
                ("build-PR", StageClass.FEATURE_SPEC, "FEAT-1"),
                ("build-PR", StageClass.FEATURE_PLAN, "FEAT-1"),
                ("build-PR", StageClass.AUTOBUILD, "FEAT-1"),
            }
        )
        doubles["mode_b_history"].histories["build-PR"] = (
            self._approved_autobuild_history(changed_files_count=3)
        )

        report = _run(supervisor.next_turn("build-PR"))

        assert report.outcome is TurnOutcome.DISPATCHED
        assert report.chosen_stage is StageClass.PULL_REQUEST_REVIEW
        assert len(doubles["pr_gate"].submissions) == 1

    def test_post_autobuild_no_diff_yields_terminal(self) -> None:
        supervisor, doubles = _build_supervisor()
        doubles["mode_reader"].modes["build-NOOP"] = BuildMode.MODE_B
        doubles["ordering_reader"].catalogues["build-NOOP"] = ["FEAT-1"]
        doubles["ordering_reader"].approved.update(
            {
                ("build-NOOP", StageClass.FEATURE_SPEC, "FEAT-1"),
                ("build-NOOP", StageClass.FEATURE_PLAN, "FEAT-1"),
                ("build-NOOP", StageClass.AUTOBUILD, "FEAT-1"),
            }
        )
        doubles["mode_b_history"].histories["build-NOOP"] = (
            self._approved_autobuild_history(changed_files_count=0)
        )

        report = _run(supervisor.next_turn("build-NOOP"))

        assert report.outcome is TurnOutcome.TERMINAL
        # No PR creation was attempted.
        assert doubles["pr_gate"].submissions == []

    def test_post_autobuild_failed_route_yields_terminal(self) -> None:
        supervisor, doubles = _build_supervisor()
        doubles["mode_reader"].modes["build-F"] = BuildMode.MODE_B
        doubles["ordering_reader"].catalogues["build-F"] = ["FEAT-1"]
        history = self._approved_autobuild_history(changed_files_count=0)
        history[-1] = FakeModeBStageEntry(
            stage=StageClass.AUTOBUILD,
            status=MODE_B_HARD_STOP,
            feature_id="FEAT-1",
            details={"rationale": "synthetic hard-stop"},
        )
        doubles["mode_b_history"].histories["build-F"] = history

        report = _run(supervisor.next_turn("build-F"))

        assert report.outcome is TurnOutcome.TERMINAL
        # No PR creation, no autobuild dispatch.
        assert doubles["pr_gate"].submissions == []
        assert doubles["autobuild"].calls == []

    def test_mode_b_post_autobuild_uses_injected_handler(self) -> None:
        # Custom handler returning a sentinel decision proves the
        # supervisor uses the injected handler rather than the default.
        captured: list[Any] = []

        def custom_handler(build: Any, history: Sequence[Any]) -> ModeBPostAutobuild:
            captured.append({"build_id": build.build_id, "n": len(history)})
            return ModeBPostAutobuild(
                route=MODE_B_NO_OP,
                rationale="custom-handler-noop",
                feature_id="FEAT-1",
                changed_files_count=0,
                session_outcome_payload={"outcome": "complete"},
            )

        supervisor, doubles = _build_supervisor()
        supervisor.mode_b_post_autobuild = custom_handler  # type: ignore[assignment]
        doubles["mode_reader"].modes["build-CUST"] = BuildMode.MODE_B
        doubles["mode_b_history"].histories[
            "build-CUST"
        ] = TestModeBPostAutobuildRouting()._approved_autobuild_history(
            changed_files_count=0
        )

        report = _run(supervisor.next_turn("build-CUST"))

        assert report.outcome is TurnOutcome.TERMINAL
        assert captured and captured[0]["build_id"] == "build-CUST"
        assert "custom-handler-noop" in report.rationale


# ---------------------------------------------------------------------------
# AC: Mode C TASK_REVIEW / TASK_WORK route via subprocess
# ---------------------------------------------------------------------------


class TestModeCSubprocessRouting:
    """AC: Mode C cycle-planner stages dispatch via the subprocess dispatcher."""

    def test_initial_task_review_dispatches_subprocess(self) -> None:
        supervisor, doubles = _build_supervisor()
        doubles["mode_reader"].modes["build-CR"] = BuildMode.MODE_C

        report = _run(supervisor.next_turn("build-CR"))

        assert report.outcome is TurnOutcome.DISPATCHED
        assert report.chosen_stage is StageClass.TASK_REVIEW
        assert doubles["subprocess"].calls[0]["stage"] is StageClass.TASK_REVIEW
        # Mode C does not use specialist or autobuild dispatchers.
        assert doubles["specialist"].calls == []
        assert doubles["autobuild"].calls == []

    def test_task_work_dispatches_subprocess_with_fix_task_ref(self) -> None:
        supervisor, doubles = _build_supervisor()
        doubles["mode_reader"].modes["build-CW"] = BuildMode.MODE_C
        doubles["ordering_reader"].approved.add(
            ("build-CW", StageClass.TASK_REVIEW, None)
        )
        doubles["mode_c_history"].histories["build-CW"] = [
            ModeCStageEntry(
                stage_class=StageClass.TASK_REVIEW,
                status="approved",
                fix_tasks=("FIX-1",),
            ),
        ]

        report = _run(supervisor.next_turn("build-CW"))

        assert report.outcome is TurnOutcome.DISPATCHED
        assert report.chosen_stage is StageClass.TASK_WORK
        call = doubles["subprocess"].calls[0]
        assert call["stage"] is StageClass.TASK_WORK
        # The fix-task reference is threaded through to the dispatcher.
        assert "fix_task" in call
        assert call["fix_task"].fix_task_id == "FIX-1"


# ---------------------------------------------------------------------------
# AC: Mode C TASK_WORK threads fix-task ref via ForwardContextBuilder shim
# ---------------------------------------------------------------------------


class TestModeCFixTaskInjection:
    """AC: Mode C ``TASK_WORK`` threads the fix-task ref via the forward-context shim."""

    def test_fix_task_context_builder_invoked_for_task_work(self) -> None:
        supervisor, doubles = _build_supervisor()
        doubles["mode_reader"].modes["build-FX"] = BuildMode.MODE_C
        doubles["ordering_reader"].approved.add(
            ("build-FX", StageClass.TASK_REVIEW, None)
        )
        doubles["mode_c_history"].histories["build-FX"] = [
            ModeCStageEntry(
                stage_class=StageClass.TASK_REVIEW,
                status="approved",
                fix_tasks=("FIX-A",),
            ),
        ]

        _run(supervisor.next_turn("build-FX"))

        # Builder shim must have been called with the fix-task ref.
        captured = doubles["captured_fix_tasks"]
        assert len(captured) == 1
        assert captured[0]["stage"] is StageClass.TASK_WORK
        assert captured[0]["fix_task"].fix_task_id == "FIX-A"


# ---------------------------------------------------------------------------
# AC: Mode C PR_REVIEW route on commits
# ---------------------------------------------------------------------------


class TestModeCPRReviewRoute:
    """AC: Mode C clean follow-up review with commits → PR_REVIEW."""

    def test_clean_followup_with_commits_advances_to_pr_review(self) -> None:
        supervisor, doubles = _build_supervisor()
        doubles["mode_reader"].modes["build-CPR"] = BuildMode.MODE_C
        # Cycle history: review + work + clean follow-up review.
        doubles["mode_c_history"].histories["build-CPR"] = [
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
                stage_class=StageClass.TASK_REVIEW,
                status="approved",
                fix_tasks=(),
            ),
        ]
        doubles["mode_c_history"].commits["build-CPR"] = True
        doubles["ordering_reader"].approved.add(
            ("build-CPR", StageClass.TASK_REVIEW, None)
        )
        doubles["ordering_reader"].approved.add(
            ("build-CPR", StageClass.TASK_WORK, None)
        )

        report = _run(supervisor.next_turn("build-CPR"))

        assert report.outcome is TurnOutcome.DISPATCHED
        assert report.chosen_stage is StageClass.PULL_REQUEST_REVIEW
        assert len(doubles["pr_gate"].submissions) == 1

    def test_clean_initial_review_terminates_without_pr(self) -> None:
        # Mode C planner returns CLEAN_REVIEW for initial empty review;
        # supervisor evaluates the terminal handler.
        supervisor, doubles = _build_supervisor()
        doubles["mode_reader"].modes["build-CIN"] = BuildMode.MODE_C
        doubles["mode_c_history"].histories["build-CIN"] = [
            ModeCStageEntry(
                stage_class=StageClass.TASK_REVIEW,
                status="approved",
                fix_tasks=(),
            ),
        ]
        doubles["mode_c_history"].commits["build-CIN"] = False

        # Inject a deterministic handler so the test does not rely on
        # the default which probes the worktree.
        async def handler(
            build: Any,
            history: Sequence[Any],
            *,
            commit_probe: Any = None,  # noqa: ARG001
        ) -> ModeCTerminalDecision:
            return ModeCTerminalDecision(
                outcome=ModeCHandlerTerminal.CLEAN_REVIEW_NO_FIXES,
                has_commits=False,
                rationale="mode-c-task-review-empty",
            )

        supervisor.mode_c_terminal_handler = handler  # type: ignore[assignment]

        report = _run(supervisor.next_turn("build-CIN"))

        assert report.outcome is TurnOutcome.TERMINAL
        assert doubles["pr_gate"].submissions == []


# ---------------------------------------------------------------------------
# AC: StageOrderingGuard invoked with per-mode prerequisites
# ---------------------------------------------------------------------------


class TestPerModePrerequisites:
    """AC: ``StageOrderingGuard`` honours the per-mode prerequisites map."""

    def test_mode_b_prereq_map_excludes_mode_a_pre_feature_spec_stages(
        self,
    ) -> None:
        # Mode B prerequisites do not list any stage prior to
        # FEATURE_SPEC. ``next_dispatchable`` with the Mode B map and
        # chain returns only Mode B stages.
        guard = StageOrderingGuard()
        reader = FakeOrderingReader(catalogues={"b1": ["FEAT-1"]})
        permitted = guard.next_dispatchable(
            "b1",
            reader,
            prerequisites=MODE_B_PREREQUISITES,
            stages=MODE_B_CHAIN,
        )
        assert permitted == {StageClass.FEATURE_SPEC}
        # Approve FEATURE_SPEC and re-check.
        reader.approved.add(("b1", StageClass.FEATURE_SPEC, "FEAT-1"))
        permitted = guard.next_dispatchable(
            "b1",
            reader,
            prerequisites=MODE_B_PREREQUISITES,
            stages=MODE_B_CHAIN,
        )
        assert StageClass.FEATURE_PLAN in permitted

    def test_mode_c_prereq_map_only_dispatches_mode_c_chain(self) -> None:
        guard = StageOrderingGuard()
        reader = FakeOrderingReader(catalogues={"c1": []})
        permitted = guard.next_dispatchable(
            "c1",
            reader,
            prerequisites=MODE_C_PREREQUISITES,
            stages=MODE_C_CHAIN,
        )
        # Only TASK_REVIEW (entry stage) is dispatchable from empty.
        assert permitted == {StageClass.TASK_REVIEW}
        reader.approved.add(("c1", StageClass.TASK_REVIEW, None))
        permitted = guard.next_dispatchable(
            "c1",
            reader,
            prerequisites=MODE_C_PREREQUISITES,
            stages=MODE_C_CHAIN,
        )
        assert StageClass.TASK_WORK in permitted

    def test_mode_a_default_unchanged_when_prereqs_absent(self) -> None:
        # Backwards-compat: omitting the prereq parameter preserves the
        # Mode A behaviour every TASK-MAG7-003 caller depends on.
        guard = StageOrderingGuard()
        reader = FakeOrderingReader(catalogues={"a1": ["FEAT-1"]})
        permitted = guard.next_dispatchable("a1", reader)
        # PRODUCT_OWNER is the Mode A entry stage.
        assert StageClass.PRODUCT_OWNER in permitted


# ---------------------------------------------------------------------------
# AC: ConstitutionalGuard unchanged in every mode
# ---------------------------------------------------------------------------


class TestConstitutionalGuardUnchanged:
    """AC: ``ConstitutionalGuard`` is invoked unchanged for every mode."""

    def test_mode_b_pr_review_does_not_request_auto_approve(self) -> None:
        # Mode B planner advances to PR_REVIEW with auto_approve=False;
        # the constitutional veto trivially holds.
        supervisor, doubles = _build_supervisor()
        doubles["mode_reader"].modes["build-CG-B"] = BuildMode.MODE_B
        doubles["ordering_reader"].catalogues["build-CG-B"] = ["FEAT-1"]
        doubles["ordering_reader"].approved.update(
            {
                ("build-CG-B", StageClass.FEATURE_SPEC, "FEAT-1"),
                ("build-CG-B", StageClass.FEATURE_PLAN, "FEAT-1"),
                ("build-CG-B", StageClass.AUTOBUILD, "FEAT-1"),
            }
        )
        doubles["mode_b_history"].histories["build-CG-B"] = [
            FakeModeBStageEntry(
                stage=StageClass.FEATURE_SPEC,
                status=MODE_B_APPROVED,
                feature_id="FEAT-1",
                details={"artefact_paths": ["spec.md"]},
            ),
            FakeModeBStageEntry(
                stage=StageClass.FEATURE_PLAN,
                status=MODE_B_APPROVED,
                feature_id="FEAT-1",
                details={"artefact_paths": ["plan.md"]},
            ),
            FakeModeBStageEntry(
                stage=StageClass.AUTOBUILD,
                status=MODE_B_APPROVED,
                feature_id="FEAT-1",
                details={"changed_files_count": 1, "diff_present": True},
            ),
        ]

        report = _run(supervisor.next_turn("build-CG-B"))

        assert report.outcome is TurnOutcome.DISPATCHED
        assert report.chosen_stage is StageClass.PULL_REQUEST_REVIEW
        # auto_approve must be False — Mode B never auto-approves
        # (ASSUM-011 constitutional invariant).
        assert doubles["pr_gate"].submissions[0]["auto_approve"] is False


# ---------------------------------------------------------------------------
# AC: Concurrent Mode A / B / C builds — no shared state cross-talk
# ---------------------------------------------------------------------------


class TestConcurrentMixedModeBuilds:
    """AC: Three concurrent builds in different modes run independently."""

    def test_concurrent_mode_a_b_c_dispatch_independently(self) -> None:
        supervisor, doubles = _build_supervisor()
        # Mode A build wants PRODUCT_OWNER.
        doubles["mode_reader"].modes["build-A"] = BuildMode.MODE_A
        # Mode B build wants FEATURE_SPEC.
        doubles["mode_reader"].modes["build-B"] = BuildMode.MODE_B
        doubles["ordering_reader"].catalogues["build-B"] = ["FEAT-1"]
        # Mode C build wants TASK_REVIEW.
        doubles["mode_reader"].modes["build-C"] = BuildMode.MODE_C
        # Mode A reasoning model returns PRODUCT_OWNER.
        doubles["reasoning_model"].next_choice = DispatchChoice(
            stage=StageClass.PRODUCT_OWNER, rationale="concurrent A"
        )

        async def _gather() -> tuple[TurnReport, TurnReport, TurnReport]:
            return await asyncio.gather(
                supervisor.next_turn("build-A"),
                supervisor.next_turn("build-B"),
                supervisor.next_turn("build-C"),
            )

        results = asyncio.run(_gather())

        # Every build dispatched.
        outcomes = {r.outcome for r in results}
        assert outcomes == {TurnOutcome.DISPATCHED}
        # Each build dispatched its mode-appropriate stage.
        by_id = {r.build_id: r for r in results}
        assert by_id["build-A"].chosen_stage is StageClass.PRODUCT_OWNER
        assert by_id["build-B"].chosen_stage is StageClass.FEATURE_SPEC
        assert by_id["build-C"].chosen_stage is StageClass.TASK_REVIEW
        # No cross-contamination — each dispatcher saw only its build.
        specialist_builds = {c["build_id"] for c in doubles["specialist"].calls}
        subprocess_builds = {c["build_id"] for c in doubles["subprocess"].calls}
        assert specialist_builds == {"build-A"}
        assert subprocess_builds == {"build-B", "build-C"}


# ---------------------------------------------------------------------------
# AC: Misconfigured Mode B / C wiring fails safely
# ---------------------------------------------------------------------------


class TestModeMisconfiguration:
    """AC: Missing planner / history reader yields WAITING + ERROR log."""

    def test_mode_b_without_planner_records_waiting(self) -> None:
        supervisor, doubles = _build_supervisor()
        # Strip the Mode B planner.
        supervisor.mode_b_planner = None
        doubles["mode_reader"].modes["build-MISC"] = BuildMode.MODE_B

        report = _run(supervisor.next_turn("build-MISC"))

        assert report.outcome is TurnOutcome.WAITING
        # No dispatcher was invoked.
        assert doubles["subprocess"].calls == []
        assert doubles["autobuild"].calls == []

    def test_mode_c_without_history_reader_records_waiting(self) -> None:
        supervisor, doubles = _build_supervisor()
        supervisor.mode_c_history_reader = None
        doubles["mode_reader"].modes["build-MISC2"] = BuildMode.MODE_C

        report = _run(supervisor.next_turn("build-MISC2"))

        assert report.outcome is TurnOutcome.WAITING
        assert doubles["subprocess"].calls == []


# ---------------------------------------------------------------------------
# FEAT-UBS-002 — unattended budget-guard enforcement in the Mode C loop
# ---------------------------------------------------------------------------


class _AlwaysReviewPlanner:
    """Fake Mode C planner that always chooses a follow-up TASK_REVIEW.

    Isolates the budget-guard enforcement from the real planner's
    history heuristics — the thing under test is the cap check, not the
    planner's stage selection.
    """

    def plan_next_stage(self, build, history, *, has_commits):  # noqa: ANN001
        return ModeCPlan(
            permitted_stages=frozenset(MODE_C_CHAIN),
            next_stage=StageClass.TASK_REVIEW,
            rationale="follow-up review (fake planner)",
        )


def _review_entries(n: int) -> list[ModeCStageEntry]:
    return [
        ModeCStageEntry(stage_class=StageClass.TASK_REVIEW, status="approved")
        for _ in range(n)
    ]


class TestModeCBudgetEnforcement:
    """UBS-002: an unattended profile at a cap pauses + escalates."""

    def _unattended(self, *, max_review_cycles: int):
        supervisor, doubles = _build_supervisor()
        supervisor.mode_c_planner = _AlwaysReviewPlanner()
        supervisor.budget_guards = BudgetGuards(max_review_cycles=max_review_cycles)
        supervisor.budget_profile_name = "unattended"
        recorder: list[dict[str, Any]] = []

        async def _pause(**kwargs: Any) -> None:
            recorder.append(kwargs)

        supervisor.budget_pause = _pause
        return supervisor, doubles, recorder

    def test_review_cycle_breach_pauses_and_escalates_risk_high(self) -> None:
        # GATE (AC-01): a Mode C build under an unattended profile that
        # reaches max_review_cycles pauses and emits a risk=high approval
        # whose details.reason == "budget_guard_breach"; NOT dispatched.
        supervisor, doubles, recorder = self._unattended(max_review_cycles=2)
        doubles["mode_reader"].modes["build-BUDGET"] = BuildMode.MODE_C
        doubles["mode_c_history"].histories["build-BUDGET"] = _review_entries(2)

        report = _run(supervisor.next_turn("build-BUDGET"))

        assert report.outcome is TurnOutcome.PAUSED_BUDGET
        # Enforcement: the next cycle was NOT dispatched.
        assert doubles["subprocess"].calls == []
        # Escalation: one risk=high, budget_guard_breach approval published.
        assert len(recorder) == 1
        payload = recorder[0]["payload"]
        assert payload.risk_level == "high"
        assert payload.details["reason"] == "budget_guard_breach"
        assert payload.details["breached_cap"] == "max_review_cycles"

    def test_under_cap_dispatches_normally(self) -> None:
        supervisor, doubles, recorder = self._unattended(max_review_cycles=5)
        doubles["mode_reader"].modes["build-UNDER"] = BuildMode.MODE_C
        doubles["mode_c_history"].histories["build-UNDER"] = _review_entries(1)

        report = _run(supervisor.next_turn("build-UNDER"))

        assert report.outcome is TurnOutcome.DISPATCHED
        assert recorder == []
        assert len(doubles["subprocess"].calls) == 1

    def test_attended_profile_caps_off_never_pauses(self) -> None:
        # AC-03: an attended (caps-off) profile is never paused by the guard
        # even with a review history that would breach an unattended cap —
        # ModeCCyclePlanner behaviour is byte-for-byte unchanged (ASSUM-010).
        supervisor, doubles = _build_supervisor()
        supervisor.mode_c_planner = _AlwaysReviewPlanner()
        supervisor.budget_guards = BudgetGuards()  # no caps → caps_enabled False
        supervisor.budget_profile_name = "attended"
        recorder: list[dict[str, Any]] = []

        async def _pause(**kwargs: Any) -> None:  # pragma: no cover - never called
            recorder.append(kwargs)

        supervisor.budget_pause = _pause
        doubles["mode_reader"].modes["build-ATT"] = BuildMode.MODE_C
        doubles["mode_c_history"].histories["build-ATT"] = _review_entries(9)

        report = _run(supervisor.next_turn("build-ATT"))

        assert report.outcome is TurnOutcome.DISPATCHED
        assert recorder == []
        assert len(doubles["subprocess"].calls) == 1

    def test_no_guards_wired_is_noop(self) -> None:
        # Default (budget_guards is None): pure no-op, normal dispatch.
        supervisor, doubles = _build_supervisor()
        supervisor.mode_c_planner = _AlwaysReviewPlanner()
        doubles["mode_reader"].modes["build-NG"] = BuildMode.MODE_C
        doubles["mode_c_history"].histories["build-NG"] = _review_entries(9)

        report = _run(supervisor.next_turn("build-NG"))

        assert report.outcome is TurnOutcome.DISPATCHED
        assert len(doubles["subprocess"].calls) == 1

    def test_already_paused_build_is_not_re_escalated(self) -> None:
        # Idempotency (merge-review hardening): a build already PAUSED at cap
        # must NOT re-fire budget_pause (same deterministic request_id) when
        # next_turn is re-run before the pause is observed.
        supervisor, doubles, recorder = self._unattended(max_review_cycles=2)
        doubles["mode_reader"].modes["build-REPAUSE"] = BuildMode.MODE_C
        doubles["mode_c_history"].histories["build-REPAUSE"] = _review_entries(2)
        doubles["state_reader"].states["build-REPAUSE"] = BuildState.PAUSED

        report = _run(supervisor.next_turn("build-REPAUSE"))

        # No escalation, no dispatch — the already-paused build stays parked
        # (WAITING), never re-escalated with the same request_id.
        assert recorder == []
        assert doubles["subprocess"].calls == []
        assert report.outcome is TurnOutcome.WAITING

    # ------------------------------------------------------------------
    # AC-05: coach-score floor via the budget_coach_score_reader DI seam
    # ------------------------------------------------------------------

    def _coach_floor(self, *, min_coach_score: float, reader, max_review_cycles=None):
        """Supervisor wired with a min_coach_score floor + a coach-score reader.

        ``reader`` is a ``(build_id) -> float | None`` callable (or one that
        raises). ``max_review_cycles`` stays ``None`` by default so a coach-only
        breach is isolated from the review-cycle cap.
        """
        supervisor, doubles = _build_supervisor()
        supervisor.mode_c_planner = _AlwaysReviewPlanner()
        supervisor.budget_guards = BudgetGuards(
            min_coach_score=min_coach_score,
            max_review_cycles=max_review_cycles,
        )
        supervisor.budget_profile_name = "unattended"
        supervisor.budget_coach_score_reader = reader
        recorder: list[dict[str, Any]] = []

        async def _pause(**kwargs: Any) -> None:
            recorder.append(kwargs)

        supervisor.budget_pause = _pause
        return supervisor, doubles, recorder

    def test_coach_score_below_floor_pauses(self) -> None:
        # AC-05: a real last_coach_score strictly below the floor breaches the
        # min_coach_score cap → PAUSED_BUDGET, risk=high, budget_guard_breach.
        supervisor, doubles, recorder = self._coach_floor(
            min_coach_score=0.8, reader=lambda _bid: 0.5
        )
        doubles["mode_reader"].modes["build-LOWSCORE"] = BuildMode.MODE_C
        doubles["mode_c_history"].histories["build-LOWSCORE"] = _review_entries(1)

        report = _run(supervisor.next_turn("build-LOWSCORE"))

        assert report.outcome is TurnOutcome.PAUSED_BUDGET
        assert doubles["subprocess"].calls == []
        assert len(recorder) == 1
        payload = recorder[0]["payload"]
        assert payload.risk_level == "high"
        assert payload.details["reason"] == "budget_guard_breach"
        assert payload.details["breached_cap"] == "min_coach_score"

    def test_coach_score_at_floor_proceeds(self) -> None:
        # AC-05: a score exactly at the floor is NOT a breach (strict `<`);
        # the build dispatches normally.
        supervisor, doubles, recorder = self._coach_floor(
            min_coach_score=0.8, reader=lambda _bid: 0.8
        )
        doubles["mode_reader"].modes["build-ATFLOOR"] = BuildMode.MODE_C
        doubles["mode_c_history"].histories["build-ATFLOOR"] = _review_entries(1)

        report = _run(supervisor.next_turn("build-ATFLOOR"))

        assert report.outcome is TurnOutcome.DISPATCHED
        assert recorder == []
        assert len(doubles["subprocess"].calls) == 1

    def test_coach_reader_none_leaves_floor_inert(self) -> None:
        # AC-03/AC-05: a reader that returns None (or an unwired reader) leaves
        # last_coach_score None → the floor stays inert → normal dispatch,
        # byte-identical to the pre-reader path.
        supervisor, doubles, recorder = self._coach_floor(
            min_coach_score=0.8, reader=lambda _bid: None
        )
        doubles["mode_reader"].modes["build-NOSCORE"] = BuildMode.MODE_C
        doubles["mode_c_history"].histories["build-NOSCORE"] = _review_entries(1)

        report = _run(supervisor.next_turn("build-NOSCORE"))

        assert report.outcome is TurnOutcome.DISPATCHED
        assert recorder == []
        assert len(doubles["subprocess"].calls) == 1

    def test_coach_reader_raises_degrades_and_other_caps_still_enforce(self) -> None:
        # AC-05: a reader failure degrades to None (loud log) without crashing —
        # the coach floor goes inert this turn but the OTHER caps still enforce.
        # Here the review-cycle cap (2, reached) fires despite the coach reader
        # raising, proving enforcement continued.
        def _boom(_bid: str) -> float:
            raise RuntimeError("coach store unreachable")

        supervisor, doubles, recorder = self._coach_floor(
            min_coach_score=0.8, reader=_boom, max_review_cycles=2
        )
        doubles["mode_reader"].modes["build-BOOM"] = BuildMode.MODE_C
        doubles["mode_c_history"].histories["build-BOOM"] = _review_entries(2)

        report = _run(supervisor.next_turn("build-BOOM"))

        assert report.outcome is TurnOutcome.PAUSED_BUDGET
        assert doubles["subprocess"].calls == []
        assert len(recorder) == 1
        # The breach names the review-cycle cap, not the (degraded) coach floor.
        assert recorder[0]["payload"].details["breached_cap"] == "max_review_cycles"

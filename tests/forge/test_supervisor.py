"""Tests for ``forge.pipeline.supervisor`` (TASK-MAG7-010).

Validates the :class:`Supervisor.next_turn` reasoning-loop dispatch turn —
the supervisor's per-turn function that:

1. Reads build state from the FEAT-FORGE-001 state machine.
2. Computes the dispatchable set via :class:`StageOrderingGuard`.
3. Asks the reasoning model for a dispatch choice.
4. Refuses out-of-band choices (executor-layer enforcement).
5. Applies the per-feature autobuild sequencer.
6. Applies the constitutional guard for PR-review auto-approve.
7. Routes the dispatch to the correct dispatcher.
8. Records every turn's outcome on the per-turn ``stage_log`` row.

Acceptance-criteria coverage map:

- AC: ``Supervisor.next_turn(build_id) -> TurnOutcome`` exists at
  ``src/forge/pipeline/supervisor.py`` — :class:`TestSupervisorExists`.
- AC: Reads current build state — :class:`TestStateMachineRead`.
- AC: Queries ``StageOrderingGuard.next_dispatchable`` —
  :class:`TestPermittedSetWiring`.
- AC: Empty permitted set + non-terminal → ``WAITING`` —
  :class:`TestWaitingOutcome`.
- AC: Presents permitted set + per-stage hints to reasoning model —
  :class:`TestReasoningModelInvocation`.
- AC: Refuses out-of-band choices — :class:`TestOutOfBandRefusal`.
- AC: Per-feature sequencer fires on AUTOBUILD —
  :class:`TestPerFeatureSequencerGate`.
- AC: Constitutional guard fires on PR-review auto-approve —
  :class:`TestConstitutionalVeto`.
- AC: Routes to specialist / subprocess / autobuild_async / PR-review —
  :class:`TestDispatchRouting`.
- AC: Records every turn in stage_log — :class:`TestTurnLogging`.
- AC: Concurrent builds — no shared state — :class:`TestConcurrentBuilds`.
- Integration: two concurrent builds, no cross-talk —
  :class:`TestConcurrentDispatchIntegration`.

All collaborators are satisfied by in-memory fakes so the suite runs
without SQLite, NATS, or LangGraph.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

import pytest

from forge.pipeline.constitutional_guard import (
    AutoApproveDecision,
    AutoApproveVerdict,
    ConstitutionalGuard,
)
from forge.pipeline.per_feature_sequencer import PerFeatureLoopSequencer
from forge.pipeline.stage_ordering_guard import StageOrderingGuard
from forge.pipeline.stage_taxonomy import PER_FEATURE_STAGES, StageClass
from forge.pipeline.supervisor import (
    BuildState,
    DispatchChoice,
    Supervisor,
    TurnOutcome,
    TurnReport,
)


# ---------------------------------------------------------------------------
# Test doubles — in-memory fakes for every injected Protocol
# ---------------------------------------------------------------------------


@dataclass
class FakeStateMachineReader:
    """Records and returns coarse build states keyed by build_id."""

    states: dict[str, BuildState] = field(default_factory=dict)

    def get_build_state(self, build_id: str) -> BuildState:
        return self.states.get(build_id, BuildState.RUNNING)


@dataclass
class FakeOrderingStageLogReader:
    """In-memory ordering-guard reader.

    Stores approved stages per ``(build_id, stage, feature_id)`` plus a
    feature catalogue per build.
    """

    approved: set[tuple[str, StageClass, str | None]] = field(default_factory=set)
    catalogues: dict[str, list[str]] = field(default_factory=dict)

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
class FakePerFeatureStageLogReader:
    """In-memory per-feature-sequencer stage-log reader."""

    approved_autobuilds: set[tuple[str, str]] = field(default_factory=set)

    def is_autobuild_approved(self, build_id: str, feature_id: str) -> bool:
        return (build_id, feature_id) in self.approved_autobuilds


@dataclass
class FakeAutobuildState:
    feature_id: str
    lifecycle: str


@dataclass
class FakeAsyncTaskReader:
    """In-memory ``async_tasks`` reader."""

    states_by_build: dict[str, list[FakeAutobuildState]] = field(
        default_factory=dict
    )

    def list_autobuild_states(
        self, build_id: str
    ) -> Iterable[FakeAutobuildState]:
        return list(self.states_by_build.get(build_id, []))


@dataclass
class RecordingReasoningModel:
    """Reasoning model that records its inputs and returns scripted choices."""

    next_choice: DispatchChoice | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def choose_dispatch(
        self,
        *,
        build_id: str,
        build_state: BuildState,
        permitted_stages: frozenset[StageClass],
        stage_hints: Mapping[StageClass, str],
        feature_catalogue: tuple[str, ...],
    ) -> DispatchChoice | None:
        self.calls.append(
            {
                "build_id": build_id,
                "build_state": build_state,
                "permitted_stages": frozenset(permitted_stages),
                "stage_hints": dict(stage_hints),
                "feature_catalogue": tuple(feature_catalogue),
            }
        )
        return self.next_choice


@dataclass
class RecordingTurnRecorder:
    """Captures every persisted per-turn row."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    raise_on_record: bool = False

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
        if self.raise_on_record:
            raise RuntimeError("synthetic recorder failure")
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
class RecordingDispatcher:
    """Generic recording dispatcher; works for sync + async signatures.

    Tracks every call's kwargs so tests can assert routing fidelity.
    Returns a deterministic result string the test can check against.
    """

    label: str
    calls: list[dict[str, Any]] = field(default_factory=list)
    return_value: Any = None

    def __post_init__(self) -> None:
        if self.return_value is None:
            self.return_value = {"dispatcher": self.label, "status": "ok"}

    async def __call__(self, **kwargs: Any) -> Any:
        # Async variant — used for specialist + subprocess dispatchers.
        self.calls.append({**kwargs})
        return self.return_value


@dataclass
class RecordingSyncDispatcher:
    """Recording dispatcher used as both sync and async fixture stand-in.

    TASK-FORGE-FRR-F010G: the autobuild dispatcher is now ``async def``
    (so the launch path can await the deepagents async ``coroutine``
    entrypoint that tolerates ``url=None`` registrations). The class
    keeps its legacy ``__call__`` synchronous return for the specialist /
    subprocess dispatchers (whose Protocols stayed sync) but adds an
    ``__await_call__``-shaped async sibling, exposed by making
    ``__call__`` return whichever shape the supervisor expects: when
    used as the autobuild dispatcher the supervisor awaits the result,
    so we wrap the recorded return value in a coroutine.
    """

    label: str
    calls: list[dict[str, Any]] = field(default_factory=list)
    return_value: Any = None
    is_async: bool = False

    def __post_init__(self) -> None:
        if self.return_value is None:
            self.return_value = {"dispatcher": self.label, "status": "ok"}

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append({**kwargs})
        if self.is_async:
            async def _result() -> Any:
                return self.return_value
            return _result()
        return self.return_value


@dataclass
class RecordingPRReviewGate:
    """In-memory PR-review gate that records submissions."""

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


# ---------------------------------------------------------------------------
# Fixtures — assembled supervisor with a happy-path baseline
# ---------------------------------------------------------------------------


@pytest.fixture
def state_reader() -> FakeStateMachineReader:
    return FakeStateMachineReader()


@pytest.fixture
def ordering_reader() -> FakeOrderingStageLogReader:
    return FakeOrderingStageLogReader()


@pytest.fixture
def per_feature_reader() -> FakePerFeatureStageLogReader:
    return FakePerFeatureStageLogReader()


@pytest.fixture
def async_task_reader() -> FakeAsyncTaskReader:
    return FakeAsyncTaskReader()


@pytest.fixture
def reasoning_model() -> RecordingReasoningModel:
    return RecordingReasoningModel()


@pytest.fixture
def turn_recorder() -> RecordingTurnRecorder:
    return RecordingTurnRecorder()


@pytest.fixture
def specialist_dispatcher() -> RecordingDispatcher:
    return RecordingDispatcher(label="specialist")


@pytest.fixture
def subprocess_dispatcher() -> RecordingDispatcher:
    return RecordingDispatcher(label="subprocess")


@pytest.fixture
def autobuild_dispatcher() -> RecordingSyncDispatcher:
    return RecordingSyncDispatcher(label="autobuild_async", is_async=True)


@pytest.fixture
def pr_review_gate() -> RecordingPRReviewGate:
    return RecordingPRReviewGate()


@pytest.fixture
def supervisor(
    state_reader: FakeStateMachineReader,
    ordering_reader: FakeOrderingStageLogReader,
    per_feature_reader: FakePerFeatureStageLogReader,
    async_task_reader: FakeAsyncTaskReader,
    reasoning_model: RecordingReasoningModel,
    turn_recorder: RecordingTurnRecorder,
    specialist_dispatcher: RecordingDispatcher,
    subprocess_dispatcher: RecordingDispatcher,
    autobuild_dispatcher: RecordingSyncDispatcher,
    pr_review_gate: RecordingPRReviewGate,
) -> Supervisor:
    return Supervisor(
        ordering_guard=StageOrderingGuard(),
        per_feature_sequencer=PerFeatureLoopSequencer(),
        constitutional_guard=ConstitutionalGuard(),
        state_reader=state_reader,
        ordering_stage_log_reader=ordering_reader,
        per_feature_stage_log_reader=per_feature_reader,
        async_task_reader=async_task_reader,
        reasoning_model=reasoning_model,
        turn_recorder=turn_recorder,
        specialist_dispatcher=specialist_dispatcher,
        subprocess_dispatcher=subprocess_dispatcher,
        autobuild_dispatcher=autobuild_dispatcher,
        pr_review_gate=pr_review_gate,
        stage_hints={
            StageClass.PRODUCT_OWNER: "hint:product-owner",
            StageClass.ARCHITECT: "hint:architect",
        },
    )


def _approve_through_system_design(reader: FakeOrderingStageLogReader, build_id: str) -> None:
    """Mark every non-per-feature prerequisite approved on ``build_id``."""
    reader.approved.add((build_id, StageClass.PRODUCT_OWNER, None))
    reader.approved.add((build_id, StageClass.ARCHITECT, None))
    reader.approved.add((build_id, StageClass.SYSTEM_ARCH, None))
    reader.approved.add((build_id, StageClass.SYSTEM_DESIGN, None))


# ---------------------------------------------------------------------------
# AC: Supervisor exists and is importable
# ---------------------------------------------------------------------------


class TestSupervisorExists:
    """AC: ``Supervisor`` exists at ``forge.pipeline.supervisor``."""

    def test_supervisor_module_is_importable(self) -> None:
        from forge.pipeline import supervisor

        assert hasattr(supervisor, "Supervisor")
        assert hasattr(supervisor, "TurnOutcome")
        assert hasattr(supervisor, "TurnReport")

    def test_supervisor_has_next_turn_method(self) -> None:
        assert hasattr(Supervisor, "next_turn")
        assert callable(Supervisor.next_turn)

    def test_turn_outcome_has_required_members(self) -> None:
        # Every documented TurnOutcome member must exist.
        members = {member.name for member in TurnOutcome}
        assert {
            "DISPATCHED",
            "WAITING",
            "WAITING_PRIOR_AUTOBUILD",
            "REFUSED_OUT_OF_BAND",
            "REFUSED_CONSTITUTIONAL",
            "TERMINAL",
            "NO_OP",
        }.issubset(members)


# ---------------------------------------------------------------------------
# AC: Reads current build state from the state machine
# ---------------------------------------------------------------------------


class TestStateMachineRead:
    """AC: Supervisor reads the build state per turn."""

    @pytest.mark.asyncio
    async def test_terminal_state_yields_terminal_outcome(
        self,
        supervisor: Supervisor,
        state_reader: FakeStateMachineReader,
    ) -> None:
        state_reader.states["build-1"] = BuildState.COMPLETE
        report = await supervisor.next_turn("build-1")
        assert report.outcome is TurnOutcome.TERMINAL

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "terminal_state",
        [BuildState.COMPLETE, BuildState.FAILED, BuildState.CANCELLED],
    )
    async def test_all_terminal_states_short_circuit(
        self,
        supervisor: Supervisor,
        state_reader: FakeStateMachineReader,
        terminal_state: BuildState,
    ) -> None:
        state_reader.states["build-X"] = terminal_state
        report = await supervisor.next_turn("build-X")
        assert report.outcome is TurnOutcome.TERMINAL


# ---------------------------------------------------------------------------
# AC: Queries StageOrderingGuard.next_dispatchable + WAITING on empty
# ---------------------------------------------------------------------------


class TestWaitingOutcome:
    """AC: Empty permitted set + non-terminal build → ``WAITING``."""

    @pytest.mark.asyncio
    async def test_empty_permitted_with_running_state_yields_waiting(
        self,
        supervisor: Supervisor,
        state_reader: FakeStateMachineReader,
        ordering_reader: FakeOrderingStageLogReader,
    ) -> None:
        # Default: no approved stages — only PRODUCT_OWNER is dispatchable
        # because it has no prerequisites. To force an empty permitted set
        # we mark PRODUCT_OWNER itself as already approved AND ensure no
        # downstream stage is dispatchable. Easier: replace the ordering
        # guard with a stub that returns an empty set.
        class EmptyGuard:
            def next_dispatchable(self, build_id: str, reader: Any) -> set:  # noqa: ARG002
                return set()

        supervisor.ordering_guard = EmptyGuard()  # type: ignore[assignment]
        state_reader.states["build-1"] = BuildState.PAUSED
        report = await supervisor.next_turn("build-1")
        assert report.outcome is TurnOutcome.WAITING


class TestPermittedSetWiring:
    """AC: Supervisor passes the permitted set into the reasoning model."""

    @pytest.mark.asyncio
    async def test_permitted_set_includes_product_owner_at_start(
        self,
        supervisor: Supervisor,
        reasoning_model: RecordingReasoningModel,
    ) -> None:
        reasoning_model.next_choice = None  # NO_OP
        await supervisor.next_turn("build-A")

        assert reasoning_model.calls
        call = reasoning_model.calls[0]
        # PRODUCT_OWNER has no prerequisites — always dispatchable at start.
        assert StageClass.PRODUCT_OWNER in call["permitted_stages"]


# ---------------------------------------------------------------------------
# AC: Reasoning-model invocation includes per-stage hints
# ---------------------------------------------------------------------------


class TestReasoningModelInvocation:
    """AC: Supervisor presents permitted set + hints to the model."""

    @pytest.mark.asyncio
    async def test_reasoning_model_receives_stage_hints(
        self,
        supervisor: Supervisor,
        reasoning_model: RecordingReasoningModel,
    ) -> None:
        reasoning_model.next_choice = None
        await supervisor.next_turn("build-B")

        call = reasoning_model.calls[0]
        assert call["stage_hints"] == {
            StageClass.PRODUCT_OWNER: "hint:product-owner",
            StageClass.ARCHITECT: "hint:architect",
        }

    @pytest.mark.asyncio
    async def test_no_op_choice_yields_no_op_outcome(
        self,
        supervisor: Supervisor,
        reasoning_model: RecordingReasoningModel,
    ) -> None:
        reasoning_model.next_choice = None
        report = await supervisor.next_turn("build-C")
        assert report.outcome is TurnOutcome.NO_OP


# ---------------------------------------------------------------------------
# AC: Refuses out-of-band reasoning-model choices
# ---------------------------------------------------------------------------


class TestOutOfBandRefusal:
    """AC: Reasoning-model choice outside permitted set is refused."""

    @pytest.mark.asyncio
    async def test_out_of_band_choice_yields_refused_out_of_band(
        self,
        supervisor: Supervisor,
        reasoning_model: RecordingReasoningModel,
        specialist_dispatcher: RecordingDispatcher,
    ) -> None:
        # Default state — only PRODUCT_OWNER is permitted. Pick ARCHITECT
        # (which is NOT in the permitted set yet because PRODUCT_OWNER
        # is unapproved).
        reasoning_model.next_choice = DispatchChoice(
            stage=StageClass.ARCHITECT,
            rationale="bad model — picking ARCHITECT before PRODUCT_OWNER",
        )
        report = await supervisor.next_turn("build-D")

        assert report.outcome is TurnOutcome.REFUSED_OUT_OF_BAND
        # Critically: dispatcher must NOT have been invoked.
        assert specialist_dispatcher.calls == []
        # Rationale must mention ADR-ARCH-026 belt-and-braces.
        assert "ADR-ARCH-026" in report.rationale

    @pytest.mark.asyncio
    async def test_out_of_band_refusal_emits_warning_log(
        self,
        supervisor: Supervisor,
        reasoning_model: RecordingReasoningModel,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        reasoning_model.next_choice = DispatchChoice(
            stage=StageClass.ARCHITECT,
        )
        with caplog.at_level("WARNING", logger="forge.pipeline.supervisor"):
            await supervisor.next_turn("build-E")
        assert any(
            "OUTSIDE the permitted set" in r.message for r in caplog.records
        )


# ---------------------------------------------------------------------------
# AC: Per-feature sequencer fires on AUTOBUILD
# ---------------------------------------------------------------------------


class TestPerFeatureSequencerGate:
    """AC: ``may_start_autobuild`` veto blocks dispatch."""

    @pytest.mark.asyncio
    async def test_autobuild_blocked_when_sibling_in_flight(
        self,
        supervisor: Supervisor,
        ordering_reader: FakeOrderingStageLogReader,
        async_task_reader: FakeAsyncTaskReader,
        reasoning_model: RecordingReasoningModel,
        autobuild_dispatcher: RecordingSyncDispatcher,
    ) -> None:
        build_id = "build-F"
        ordering_reader.catalogues[build_id] = ["FEAT-1", "FEAT-2"]
        _approve_through_system_design(ordering_reader, build_id)
        # FEAT-1 plan + spec approved; FEAT-2 plan + spec approved — both
        # AUTOBUILDs are in the permitted set per the ordering guard.
        for fid in ("FEAT-1", "FEAT-2"):
            ordering_reader.approved.add(
                (build_id, StageClass.FEATURE_SPEC, fid)
            )
            ordering_reader.approved.add(
                (build_id, StageClass.FEATURE_PLAN, fid)
            )
        # FEAT-1 autobuild is already running — sibling for FEAT-2.
        async_task_reader.states_by_build[build_id] = [
            FakeAutobuildState(feature_id="FEAT-1", lifecycle="running_wave"),
        ]
        # Model picks FEAT-2's autobuild.
        reasoning_model.next_choice = DispatchChoice(
            stage=StageClass.AUTOBUILD,
            feature_id="FEAT-2",
            rationale="want to start FEAT-2 autobuild",
        )
        report = await supervisor.next_turn(build_id)
        assert report.outcome is TurnOutcome.WAITING_PRIOR_AUTOBUILD
        assert autobuild_dispatcher.calls == []

    @pytest.mark.asyncio
    async def test_autobuild_dispatched_when_no_sibling_in_flight(
        self,
        supervisor: Supervisor,
        ordering_reader: FakeOrderingStageLogReader,
        reasoning_model: RecordingReasoningModel,
        autobuild_dispatcher: RecordingSyncDispatcher,
    ) -> None:
        build_id = "build-G"
        ordering_reader.catalogues[build_id] = ["FEAT-1"]
        _approve_through_system_design(ordering_reader, build_id)
        ordering_reader.approved.add(
            (build_id, StageClass.FEATURE_SPEC, "FEAT-1")
        )
        ordering_reader.approved.add(
            (build_id, StageClass.FEATURE_PLAN, "FEAT-1")
        )
        reasoning_model.next_choice = DispatchChoice(
            stage=StageClass.AUTOBUILD,
            feature_id="FEAT-1",
        )
        report = await supervisor.next_turn(build_id)
        assert report.outcome is TurnOutcome.DISPATCHED
        assert len(autobuild_dispatcher.calls) == 1
        call = autobuild_dispatcher.calls[0]
        assert call["build_id"] == build_id
        assert call["feature_id"] == "FEAT-1"


# ---------------------------------------------------------------------------
# AC: Constitutional guard fires on PR-review auto-approve
# ---------------------------------------------------------------------------


class TestConstitutionalVeto:
    """AC: ``veto_auto_approve`` refused for PR-review auto-approve."""

    @pytest.mark.asyncio
    async def test_pr_review_auto_approve_refused_constitutional(
        self,
        supervisor: Supervisor,
        ordering_reader: FakeOrderingStageLogReader,
        reasoning_model: RecordingReasoningModel,
        pr_review_gate: RecordingPRReviewGate,
    ) -> None:
        build_id = "build-H"
        ordering_reader.catalogues[build_id] = ["FEAT-1"]
        _approve_through_system_design(ordering_reader, build_id)
        ordering_reader.approved.add(
            (build_id, StageClass.FEATURE_SPEC, "FEAT-1")
        )
        ordering_reader.approved.add(
            (build_id, StageClass.FEATURE_PLAN, "FEAT-1")
        )
        ordering_reader.approved.add(
            (build_id, StageClass.AUTOBUILD, "FEAT-1")
        )
        # Model attempts an auto-approve on PR-review — this must be
        # refused by the constitutional guard.
        reasoning_model.next_choice = DispatchChoice(
            stage=StageClass.PULL_REQUEST_REVIEW,
            feature_id="FEAT-1",
            auto_approve=True,
            rationale="model thinks Coach score is high enough",
        )
        report = await supervisor.next_turn(build_id)

        assert report.outcome is TurnOutcome.REFUSED_CONSTITUTIONAL
        # Gate must NOT have been called.
        assert pr_review_gate.submissions == []
        # The decision must record the constitutional refusal.
        assert isinstance(report.gate_decision, AutoApproveDecision)
        assert report.gate_decision.verdict is AutoApproveVerdict.REFUSED

    @pytest.mark.asyncio
    async def test_pr_review_without_auto_approve_proceeds(
        self,
        supervisor: Supervisor,
        ordering_reader: FakeOrderingStageLogReader,
        reasoning_model: RecordingReasoningModel,
        pr_review_gate: RecordingPRReviewGate,
    ) -> None:
        # PR-review *without* auto-approve goes through to the gate
        # surface; the constitutional guard only vetoes auto-approve.
        build_id = "build-I"
        ordering_reader.catalogues[build_id] = ["FEAT-1"]
        _approve_through_system_design(ordering_reader, build_id)
        for s in (StageClass.FEATURE_SPEC, StageClass.FEATURE_PLAN, StageClass.AUTOBUILD):
            ordering_reader.approved.add((build_id, s, "FEAT-1"))
        reasoning_model.next_choice = DispatchChoice(
            stage=StageClass.PULL_REQUEST_REVIEW,
            feature_id="FEAT-1",
            auto_approve=False,
            rationale="defer to mandatory human approval",
        )
        report = await supervisor.next_turn(build_id)
        assert report.outcome is TurnOutcome.DISPATCHED
        assert len(pr_review_gate.submissions) == 1
        assert pr_review_gate.submissions[0]["auto_approve"] is False


# ---------------------------------------------------------------------------
# AC: Routes to the correct dispatcher per stage class
# ---------------------------------------------------------------------------


class TestDispatchRouting:
    """AC: Each StageClass routes to the correct dispatcher."""

    @pytest.mark.asyncio
    async def test_product_owner_routes_to_specialist(
        self,
        supervisor: Supervisor,
        reasoning_model: RecordingReasoningModel,
        specialist_dispatcher: RecordingDispatcher,
    ) -> None:
        reasoning_model.next_choice = DispatchChoice(
            stage=StageClass.PRODUCT_OWNER,
            rationale="kick off",
        )
        report = await supervisor.next_turn("build-PO")
        assert report.outcome is TurnOutcome.DISPATCHED
        assert len(specialist_dispatcher.calls) == 1
        assert specialist_dispatcher.calls[0]["stage"] is StageClass.PRODUCT_OWNER

    @pytest.mark.asyncio
    async def test_architect_routes_to_specialist(
        self,
        supervisor: Supervisor,
        ordering_reader: FakeOrderingStageLogReader,
        reasoning_model: RecordingReasoningModel,
        specialist_dispatcher: RecordingDispatcher,
    ) -> None:
        ordering_reader.approved.add(("build-A2", StageClass.PRODUCT_OWNER, None))
        reasoning_model.next_choice = DispatchChoice(
            stage=StageClass.ARCHITECT,
        )
        report = await supervisor.next_turn("build-A2")
        assert report.outcome is TurnOutcome.DISPATCHED
        assert specialist_dispatcher.calls[0]["stage"] is StageClass.ARCHITECT

    @pytest.mark.asyncio
    async def test_system_arch_routes_to_subprocess(
        self,
        supervisor: Supervisor,
        ordering_reader: FakeOrderingStageLogReader,
        reasoning_model: RecordingReasoningModel,
        subprocess_dispatcher: RecordingDispatcher,
    ) -> None:
        ordering_reader.approved.add(("build-SA", StageClass.PRODUCT_OWNER, None))
        ordering_reader.approved.add(("build-SA", StageClass.ARCHITECT, None))
        reasoning_model.next_choice = DispatchChoice(
            stage=StageClass.SYSTEM_ARCH,
        )
        report = await supervisor.next_turn("build-SA")
        assert report.outcome is TurnOutcome.DISPATCHED
        assert subprocess_dispatcher.calls[0]["stage"] is StageClass.SYSTEM_ARCH

    @pytest.mark.asyncio
    async def test_feature_plan_routes_to_subprocess_with_feature_id(
        self,
        supervisor: Supervisor,
        ordering_reader: FakeOrderingStageLogReader,
        reasoning_model: RecordingReasoningModel,
        subprocess_dispatcher: RecordingDispatcher,
    ) -> None:
        bid = "build-FP"
        ordering_reader.catalogues[bid] = ["FEAT-X"]
        _approve_through_system_design(ordering_reader, bid)
        ordering_reader.approved.add((bid, StageClass.FEATURE_SPEC, "FEAT-X"))
        reasoning_model.next_choice = DispatchChoice(
            stage=StageClass.FEATURE_PLAN, feature_id="FEAT-X"
        )
        report = await supervisor.next_turn(bid)
        assert report.outcome is TurnOutcome.DISPATCHED
        assert subprocess_dispatcher.calls[0]["feature_id"] == "FEAT-X"

    @pytest.mark.asyncio
    async def test_autobuild_routes_to_autobuild_async(
        self,
        supervisor: Supervisor,
        ordering_reader: FakeOrderingStageLogReader,
        reasoning_model: RecordingReasoningModel,
        autobuild_dispatcher: RecordingSyncDispatcher,
    ) -> None:
        bid = "build-AB"
        ordering_reader.catalogues[bid] = ["FEAT-Y"]
        _approve_through_system_design(ordering_reader, bid)
        ordering_reader.approved.add((bid, StageClass.FEATURE_SPEC, "FEAT-Y"))
        ordering_reader.approved.add((bid, StageClass.FEATURE_PLAN, "FEAT-Y"))
        reasoning_model.next_choice = DispatchChoice(
            stage=StageClass.AUTOBUILD, feature_id="FEAT-Y"
        )
        report = await supervisor.next_turn(bid)
        assert report.outcome is TurnOutcome.DISPATCHED
        assert autobuild_dispatcher.calls[0]["feature_id"] == "FEAT-Y"

    @pytest.mark.asyncio
    async def test_pull_request_review_routes_to_gate(
        self,
        supervisor: Supervisor,
        ordering_reader: FakeOrderingStageLogReader,
        reasoning_model: RecordingReasoningModel,
        pr_review_gate: RecordingPRReviewGate,
    ) -> None:
        bid = "build-PR"
        ordering_reader.catalogues[bid] = ["FEAT-Z"]
        _approve_through_system_design(ordering_reader, bid)
        for s in (StageClass.FEATURE_SPEC, StageClass.FEATURE_PLAN, StageClass.AUTOBUILD):
            ordering_reader.approved.add((bid, s, "FEAT-Z"))
        reasoning_model.next_choice = DispatchChoice(
            stage=StageClass.PULL_REQUEST_REVIEW,
            feature_id="FEAT-Z",
            auto_approve=False,
        )
        report = await supervisor.next_turn(bid)
        assert report.outcome is TurnOutcome.DISPATCHED
        assert len(pr_review_gate.submissions) == 1

    @pytest.mark.asyncio
    async def test_dispatch_covers_every_stage_class(
        self,
        supervisor: Supervisor,
    ) -> None:
        # Catches future enum extensions that forget a routing branch and
        # would otherwise hit the loud-fail ``TypeError`` at
        # ``supervisor.py:1555``. This is the regression net for
        # FEAT-FORGE-008 + F008-VAL-003 (TASK_REVIEW / TASK_WORK).
        #
        # WS2-B8: the output-side stages (POST_REVIEW_STAGES = DEPLOY,
        # LIVE_GATE) are dispatched by the standalone DeployStageRunner, NOT the
        # greenfield reasoning loop — they deliberately have no supervisor
        # routing branch. They are excluded here and their correct rejection is
        # asserted separately below.
        from forge.pipeline.stage_taxonomy import POST_REVIEW_STAGES

        for stage in StageClass:
            if stage in POST_REVIEW_STAGES:
                continue
            feature_id = "FEAT-META" if stage in PER_FEATURE_STAGES else None
            choice = DispatchChoice(
                stage=stage,
                feature_id=feature_id,
                rationale="meta-test routing coverage",
            )
            try:
                await supervisor._dispatch(build_id="build-meta", choice=choice)
            except TypeError as exc:
                if "no routing for stage" in str(exc):
                    pytest.fail(
                        f"StageClass.{stage.name} has no routing branch in "
                        f"Supervisor._dispatch — every enum member needs one"
                    )
                raise

    @pytest.mark.asyncio
    async def test_supervisor_rejects_output_side_stages(
        self,
        supervisor: Supervisor,
    ) -> None:
        # WS2-B8 safety net: if a DEPLOY / LIVE_GATE choice ever reaches the
        # supervisor (it cannot — next_dispatchable filters POST_REVIEW_STAGES),
        # the reasoning-loop dispatcher must loud-fail rather than silently
        # dispatch a deploy. The DeployStageRunner is their only dispatcher.
        from forge.pipeline.stage_taxonomy import POST_REVIEW_STAGES

        for stage in POST_REVIEW_STAGES:
            choice = DispatchChoice(
                stage=stage,
                feature_id=None,
                rationale="output-side stage must be rejected by the supervisor",
            )
            with pytest.raises(TypeError, match="no routing for stage"):
                await supervisor._dispatch(build_id="build-meta", choice=choice)


# ---------------------------------------------------------------------------
# AC: Records every turn's outcome on stage_log
# ---------------------------------------------------------------------------


class TestTurnLogging:
    """AC: Every turn writes a per-turn ``stage_log`` row with rationale."""

    @pytest.mark.asyncio
    async def test_dispatched_turn_persists_row(
        self,
        supervisor: Supervisor,
        reasoning_model: RecordingReasoningModel,
        turn_recorder: RecordingTurnRecorder,
    ) -> None:
        reasoning_model.next_choice = DispatchChoice(
            stage=StageClass.PRODUCT_OWNER,
            rationale="initial dispatch",
        )
        await supervisor.next_turn("build-LOG")

        assert len(turn_recorder.rows) == 1
        row = turn_recorder.rows[0]
        assert row["outcome"] is TurnOutcome.DISPATCHED
        assert row["chosen_stage"] is StageClass.PRODUCT_OWNER
        assert "initial dispatch" in row["rationale"]

    @pytest.mark.asyncio
    async def test_refused_turn_persists_row(
        self,
        supervisor: Supervisor,
        reasoning_model: RecordingReasoningModel,
        turn_recorder: RecordingTurnRecorder,
    ) -> None:
        reasoning_model.next_choice = DispatchChoice(
            stage=StageClass.ARCHITECT,
        )
        await supervisor.next_turn("build-LOG2")
        assert len(turn_recorder.rows) == 1
        assert turn_recorder.rows[0]["outcome"] is TurnOutcome.REFUSED_OUT_OF_BAND

    @pytest.mark.asyncio
    async def test_recorder_failure_does_not_propagate(
        self,
        supervisor: Supervisor,
        reasoning_model: RecordingReasoningModel,
        turn_recorder: RecordingTurnRecorder,
    ) -> None:
        # Recorder raising must not fail the turn — the in-memory report
        # is the authoritative outcome; the SQLite row is the side effect.
        turn_recorder.raise_on_record = True
        reasoning_model.next_choice = DispatchChoice(
            stage=StageClass.PRODUCT_OWNER
        )
        report = await supervisor.next_turn("build-LOG3")
        assert report.outcome is TurnOutcome.DISPATCHED


# ---------------------------------------------------------------------------
# AC: Concurrent builds — no shared mutable state across builds
# ---------------------------------------------------------------------------


class TestConcurrentBuilds:
    """AC: Each build_id gets an independent ``next_turn`` invocation."""

    @pytest.mark.asyncio
    async def test_supervisor_holds_no_per_build_state(
        self,
        supervisor: Supervisor,
        reasoning_model: RecordingReasoningModel,
    ) -> None:
        # The supervisor's mutable attributes must not key on build_id.
        # Two sequential calls with different build_ids never clobber.
        reasoning_model.next_choice = DispatchChoice(
            stage=StageClass.PRODUCT_OWNER
        )
        r1 = await supervisor.next_turn("build-1")
        r2 = await supervisor.next_turn("build-2")
        assert r1.build_id == "build-1"
        assert r2.build_id == "build-2"

    @pytest.mark.asyncio
    async def test_concurrent_dispatch_does_not_cross_talk(
        self,
        supervisor: Supervisor,
        ordering_reader: FakeOrderingStageLogReader,
        reasoning_model: RecordingReasoningModel,
        specialist_dispatcher: RecordingDispatcher,
    ) -> None:
        # Two concurrent builds at different stage levels — both must
        # dispatch independently.
        ordering_reader.approved.add(("build-A", StageClass.PRODUCT_OWNER, None))

        # Use a per-call choice — track build_id passed to model so we
        # return the appropriate stage for each.
        def picker(
            *,
            build_id: str,
            build_state: BuildState,  # noqa: ARG001
            permitted_stages: frozenset[StageClass],  # noqa: ARG001
            stage_hints: Mapping[StageClass, str],  # noqa: ARG001
            feature_catalogue: tuple[str, ...],  # noqa: ARG001
        ) -> DispatchChoice | None:
            if build_id == "build-A":
                return DispatchChoice(stage=StageClass.ARCHITECT)
            return DispatchChoice(stage=StageClass.PRODUCT_OWNER)

        reasoning_model.choose_dispatch = picker  # type: ignore[assignment]

        results = await asyncio.gather(
            supervisor.next_turn("build-A"),
            supervisor.next_turn("build-B"),
        )
        assert {r.outcome for r in results} == {TurnOutcome.DISPATCHED}
        # Two specialist dispatches — each scoped to the correct build.
        scoped = {(c["stage"], c["build_id"]) for c in specialist_dispatcher.calls}
        assert scoped == {
            (StageClass.ARCHITECT, "build-A"),
            (StageClass.PRODUCT_OWNER, "build-B"),
        }


# ---------------------------------------------------------------------------
# Integration: two concurrent builds, supervisor dispatches both
# ---------------------------------------------------------------------------


class TestConcurrentDispatchIntegration:
    """Integration: two concurrent builds dispatched without cross-talk.

    Group F @concurrency: supervisor dispatches second build's stage
    during first build's autobuild. The test simulates this by running
    two concurrent ``next_turn`` calls, one of which is at the AUTOBUILD
    stage and the other at the PRODUCT_OWNER stage. Both must land in
    ``DISPATCHED``; the autobuild dispatcher and specialist dispatcher
    must each see exactly one call scoped to the correct build_id.
    """

    @pytest.mark.asyncio
    async def test_two_concurrent_builds_dispatch_independently(
        self,
        supervisor: Supervisor,
        ordering_reader: FakeOrderingStageLogReader,
        reasoning_model: RecordingReasoningModel,
        autobuild_dispatcher: RecordingSyncDispatcher,
        specialist_dispatcher: RecordingDispatcher,
    ) -> None:
        # build-1: ready for AUTOBUILD on FEAT-1.
        ordering_reader.catalogues["build-1"] = ["FEAT-1"]
        _approve_through_system_design(ordering_reader, "build-1")
        ordering_reader.approved.add(("build-1", StageClass.FEATURE_SPEC, "FEAT-1"))
        ordering_reader.approved.add(("build-1", StageClass.FEATURE_PLAN, "FEAT-1"))
        # build-2: starting fresh — wants PRODUCT_OWNER.

        def picker(
            *,
            build_id: str,
            build_state: BuildState,  # noqa: ARG001
            permitted_stages: frozenset[StageClass],  # noqa: ARG001
            stage_hints: Mapping[StageClass, str],  # noqa: ARG001
            feature_catalogue: tuple[str, ...],  # noqa: ARG001
        ) -> DispatchChoice | None:
            if build_id == "build-1":
                return DispatchChoice(
                    stage=StageClass.AUTOBUILD, feature_id="FEAT-1"
                )
            return DispatchChoice(stage=StageClass.PRODUCT_OWNER)

        reasoning_model.choose_dispatch = picker  # type: ignore[assignment]

        results = await asyncio.gather(
            supervisor.next_turn("build-1"),
            supervisor.next_turn("build-2"),
        )
        outcomes = {r.outcome for r in results}
        assert outcomes == {TurnOutcome.DISPATCHED}
        # Each dispatcher invoked exactly once for the right build.
        assert len(autobuild_dispatcher.calls) == 1
        assert autobuild_dispatcher.calls[0]["build_id"] == "build-1"
        assert len(specialist_dispatcher.calls) == 1
        assert specialist_dispatcher.calls[0]["build_id"] == "build-2"

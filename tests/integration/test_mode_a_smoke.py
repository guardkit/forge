"""End-to-end smoke test for FEAT-FORGE-007 Mode A pipeline (TASK-MAG7-012).

Drives a one-line product brief through every Mode A stage with
auto-approval at every flagged-for-review checkpoint, and asserts the
build terminates paused at pull-request review awaiting human approval
with a recorded PR URL.

Covers FEAT-FORGE-007 Group H ``@smoke @integration`` scenarios:

* "A minimal greenfield brief drives a single-feature run to a pull
  request awaiting human review" — :class:`TestMinimalGreenfieldSmoke`.
* "A greenfield build with no available specialists is flagged for
  review at every specialist stage" (degraded path) —
  :class:`TestNoSpecialistsDegradedPath`.

Also covers Group G ``@data-integrity`` scenario "canonical Mode A
stage-history ordering" as part of the smoke (the ``stage_log``
chronology assertion).

Harness shape
-------------

The :func:`greenfield_brief_pipeline` fixture wires the supervisor
(``forge.pipeline.supervisor.Supervisor``) against in-memory fakes that
satisfy every Protocol the supervisor depends on. Real substrate
adapters (FEAT-FORGE-001 SQLite, FEAT-FORGE-005 subprocess engine) are
mocked at their Protocol boundaries; only the FEAT-FORGE-007 net-new
code (the supervisor + the three guards + the per-feature sequencer)
runs for real.

The harness owns:

* ``FakeSpecialistRegistry`` — pre-populated with healthy product-owner
  + architect specialists for the smoke path; emptied for the degraded
  path. The fake specialist dispatcher consults this registry and
  returns ``flagged_for_review`` when no specialist is available.
* ``FakeGuardkitSubprocessEngine`` — returns canned approved artefacts
  for the four subprocess stages (system-arch, system-design,
  feature-spec, feature-plan) without ever spawning a real process.
* ``FakeStageLogStore`` — single composite in-memory backing for the
  three reader Protocols the supervisor consumes (ordering reader,
  per-feature reader, turn recorder) plus a chronological log the
  smoke test asserts against.
* ``FakeApprovalChannel`` — stub approval channel that auto-approves
  every checkpoint so the canary fires loud if a non-constitutional
  gate ever refuses under high Coach scores.
* ``FakeClock`` — deterministic clock from
  :mod:`forge.pipeline` (AC: "Tests use the existing FakeClock pattern
  from src/forge/pipeline.py for deterministic timing"). The
  supervisor's ``next_turn`` does not consume a clock directly, but
  the harness exposes one for any timing-sensitive assertion to
  remain wall-clock-free (and to demonstrate the canonical pattern
  for downstream Group H integrations that DO consume a clock).

Driver
------

:meth:`GreenfieldBriefPipeline.drive_until_paused` runs
``supervisor.next_turn`` iteratively, absorbing each dispatcher's
outcome into the stage_log between turns:

* ``status="approved"`` → mark the stage approved on the ordering
  reader so the ordering guard permits the next stage. ``SYSTEM_DESIGN``
  also populates the feature catalogue (single feature ``FEAT-1``).
* ``status="flagged_for_review"`` → annotate the chronology row with
  ``gate_mode="flag_for_review"`` and stop the driver — the build is
  paused awaiting external resume.
* ``PULL_REQUEST_REVIEW`` dispatch → annotate the chronology row with
  ``gate_mode="mandatory_human"`` plus the recorded PR URL and stop —
  the build is paused at PR review.

References
----------

* FEAT-FORGE-007 ``features/mode-a-greenfield-end-to-end`` Group H.
* TASK-MAG7-012 acceptance criteria.
* :mod:`forge.pipeline` — :class:`forge.pipeline.FakeClock` source.
* :mod:`forge.pipeline.supervisor` — :class:`Supervisor.next_turn`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

import pytest

from forge.pipeline import FakeClock
from forge.pipeline.constitutional_guard import ConstitutionalGuard
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
# Constants
# ---------------------------------------------------------------------------


SMOKE_BUILD_ID: str = "build-FEAT-1-20260426120000"
SMOKE_FEATURE_ID: str = "FEAT-1"
SMOKE_BRIEF: str = "Build a small greeting CLI that says hello to the world."

CANONICAL_STAGE_ORDER: tuple[StageClass, ...] = (
    StageClass.PRODUCT_OWNER,
    StageClass.ARCHITECT,
    StageClass.SYSTEM_ARCH,
    StageClass.SYSTEM_DESIGN,
    StageClass.FEATURE_SPEC,
    StageClass.FEATURE_PLAN,
    StageClass.AUTOBUILD,
    StageClass.PULL_REQUEST_REVIEW,
)


# ---------------------------------------------------------------------------
# In-memory fakes — Protocol-shaped doubles for every supervisor seam
# ---------------------------------------------------------------------------


@dataclass
class FakeSpecialistRegistry:
    """Pre-populated specialist availability map.

    Keys are :class:`StageClass` members; values are the specialist
    identifier the dispatcher would have routed to. An empty registry
    drives the degraded path: every specialist stage returns
    ``flagged_for_review`` because no specialist is available.
    """

    healthy: dict[StageClass, str] = field(default_factory=dict)

    def find_specialist(self, stage: StageClass) -> str | None:
        return self.healthy.get(stage)


@dataclass
class FakeApprovalChannel:
    """Stub approval channel that auto-approves every checkpoint.

    The channel records every flag-for-review event for inspection and
    answers ``"approve"`` while ``auto_approve`` is ``True`` — that is
    the canary AC: if any non-constitutional gate fails to auto-approve
    under high Coach scores, the smoke fails on the chronology
    assertion and the diverging gate is identifiable from the recorded
    flag log.
    """

    auto_approve: bool = True
    flag_log: list[dict[str, Any]] = field(default_factory=list)

    def record_flag(self, *, build_id: str, stage: StageClass, rationale: str) -> str:
        self.flag_log.append(
            {"build_id": build_id, "stage": stage, "rationale": rationale}
        )
        return "approve" if self.auto_approve else "flag"


@dataclass
class FakeStageLogStore:
    """Composite in-memory stage_log — one store, three Protocol surfaces.

    Implements the read shapes consumed by the supervisor:

    * :class:`forge.pipeline.stage_ordering_guard.StageLogReader` —
      ``is_approved`` + ``feature_catalogue``.
    * :class:`forge.pipeline.per_feature_sequencer.StageLogReader` —
      ``is_autobuild_approved``.
    * :class:`forge.pipeline.supervisor.StageLogTurnRecorder` —
      ``record_turn`` writes a chronological audit row.

    Plus a :meth:`mark_approved` helper used by the harness driver to
    mutate the approved set between supervisor turns (mimicking the
    real adapters writing back after a successful dispatch).
    """

    approved: set[tuple[str, StageClass, str | None]] = field(default_factory=set)
    catalogues: dict[str, list[str]] = field(default_factory=dict)
    chronology: list[dict[str, Any]] = field(default_factory=list)

    # Ordering-guard reader Protocol -------------------------------------

    def is_approved(
        self,
        build_id: str,
        stage: StageClass,
        feature_id: str | None = None,
    ) -> bool:
        return (build_id, stage, feature_id) in self.approved

    def feature_catalogue(self, build_id: str) -> list[str]:
        return list(self.catalogues.get(build_id, []))

    # Per-feature-sequencer reader Protocol ------------------------------

    def is_autobuild_approved(self, build_id: str, feature_id: str) -> bool:
        return (build_id, StageClass.AUTOBUILD, feature_id) in self.approved

    # Turn-recorder Protocol ---------------------------------------------

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
        self.chronology.append(
            {
                "build_id": build_id,
                "outcome": outcome,
                "permitted_stages": frozenset(permitted_stages),
                "chosen_stage": chosen_stage,
                "chosen_feature_id": chosen_feature_id,
                "rationale": rationale,
                "gate_verdict": gate_verdict,
                # Smoke-test annotations populated by the driver after
                # absorbing the dispatcher outcome.
                "gate_mode": None,
                "pr_url": None,
            }
        )

    # Driver-side mutators -----------------------------------------------

    def mark_approved(
        self,
        *,
        build_id: str,
        stage: StageClass,
        feature_id: str | None = None,
    ) -> None:
        self.approved.add((build_id, stage, feature_id))

    def set_catalogue(self, build_id: str, feature_ids: list[str]) -> None:
        self.catalogues[build_id] = list(feature_ids)


@dataclass
class FakeStateMachineReader:
    """In-memory state-machine reader; defaults to ``RUNNING`` per build."""

    states: dict[str, BuildState] = field(default_factory=dict)

    def set_state(self, build_id: str, state: BuildState) -> None:
        self.states[build_id] = state

    def get_build_state(self, build_id: str) -> BuildState:
        return self.states.get(build_id, BuildState.RUNNING)


@dataclass
class FakeAsyncTaskReader:
    """Empty ``async_tasks`` channel — no autobuilds in flight by default."""

    states_by_build: dict[str, list[Any]] = field(default_factory=dict)

    def list_autobuild_states(self, build_id: str) -> Iterable[Any]:
        return list(self.states_by_build.get(build_id, []))


# ---------------------------------------------------------------------------
# Dispatcher fakes — record calls + return canned outcomes
# ---------------------------------------------------------------------------


@dataclass
class FakeSpecialistDispatcher:
    """Specialist dispatcher honouring the in-memory registry.

    Returns ``status="approved"`` when a specialist is available for
    the requested stage, ``status="flagged_for_review"`` otherwise. The
    flag rationale cites the empty registry so the chronology row
    captures the degraded-mode reason.
    """

    registry: FakeSpecialistRegistry
    approval: FakeApprovalChannel
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(
        self,
        *,
        stage: StageClass,
        build_id: str,
        feature_id: str | None = None,
        rationale: str = "",
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "stage": stage,
                "build_id": build_id,
                "feature_id": feature_id,
                "rationale": rationale,
            }
        )
        specialist = self.registry.find_specialist(stage)
        if specialist is None:
            flag_rationale = (
                f"degraded: no healthy specialist available for stage "
                f"{stage.value!r}; flagged for human review"
            )
            self.approval.record_flag(
                build_id=build_id, stage=stage, rationale=flag_rationale
            )
            return {
                "stage": stage,
                "status": "flagged_for_review",
                "rationale": flag_rationale,
            }
        return {
            "stage": stage,
            "status": "approved",
            "specialist_id": specialist,
            "rationale": f"dispatched to specialist {specialist!r}",
        }


@dataclass
class FakeGuardkitSubprocessEngine:
    """GuardKit-shaped subprocess engine — never spawns a real process.

    Returns canned approved artefacts for the four subprocess stages
    (system-arch, system-design, feature-spec, feature-plan). The
    artefact-path map can be overridden per stage; missing entries fall
    back to a synthetic path ``/fake/artefacts/{stage}.md``.
    """

    artefact_paths: dict[StageClass, str] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(
        self,
        *,
        stage: StageClass,
        build_id: str,
        feature_id: str | None = None,
        rationale: str = "",
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "stage": stage,
                "build_id": build_id,
                "feature_id": feature_id,
                "rationale": rationale,
            }
        )
        path = self.artefact_paths.get(stage, f"/fake/artefacts/{stage.value}.md")
        return {
            "stage": stage,
            "status": "approved",
            "artefact_path": path,
            "rationale": f"canned artefact {path!r}",
        }


@dataclass
class FakeAutobuildAsyncDispatcher:
    """Autobuild async-dispatcher — returns a synthetic completed handle.

    The smoke harness treats AUTOBUILD as having already finished by
    the time ``next_turn`` returns; the real async lifecycle is owned
    by FEAT-FORGE-006 and is not under test here.

    TASK-FORGE-FRR-F010G: the autobuild dispatcher Protocol is now
    ``Callable[..., Awaitable[Any]]`` because the production launch
    path awaits the deepagents middleware's async ``coroutine``
    entrypoint (URL-less in-process ASGI transport). This fake's
    ``__call__`` is therefore async to satisfy the supervisor's
    ``await self.autobuild_dispatcher(...)`` site.
    """

    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(
        self,
        *,
        build_id: str,
        feature_id: str,
        rationale: str = "",
    ) -> dict[str, Any]:
        self.calls.append(
            {"build_id": build_id, "feature_id": feature_id, "rationale": rationale}
        )
        return {
            "build_id": build_id,
            "feature_id": feature_id,
            "status": "approved",
            "lifecycle": "completed",
        }


@dataclass
class FakePRReviewGate:
    """PR-review gate stub — submission is the build's terminal pause.

    Returns a record carrying the recorded PR URL plus
    ``gate_mode="mandatory_human"``. The smoke driver lifts those fields
    onto the matching chronology row so the AC assertion ("PR URL
    recorded in stage_log", "mandatory_human gate mode") is local to
    the chronology log.
    """

    submissions: list[dict[str, Any]] = field(default_factory=list)
    pr_url_template: str = "https://github.com/example/forge/pull/{number}"
    _next_pr_number: int = 100

    def submit_decision(
        self,
        *,
        build_id: str,
        feature_id: str,
        auto_approve: bool,
        rationale: str,
    ) -> dict[str, Any]:
        self._next_pr_number += 1
        pr_url = self.pr_url_template.format(number=self._next_pr_number)
        record = {
            "build_id": build_id,
            "feature_id": feature_id,
            "auto_approve": auto_approve,
            "rationale": rationale,
            "gate_mode": "mandatory_human",
            "pr_url": pr_url,
        }
        self.submissions.append(record)
        return record


# ---------------------------------------------------------------------------
# Reasoning model — deterministic canonical-order picker
# ---------------------------------------------------------------------------


@dataclass
class CanonicalReasoningModel:
    """Reasoning model that picks the next canonical Mode A stage.

    Walks :data:`CANONICAL_STAGE_ORDER` and returns the first stage that
    is both in the supervisor's permitted set *and* not yet recorded as
    approved on the stage_log. For per-feature stages, picks the first
    feature in the catalogue that has not yet been approved at the
    chosen stage. Returns ``None`` when nothing is left to dispatch.

    Always emits ``auto_approve=False`` so the constitutional guard's
    PR-review veto is exercised passively rather than triggered — the
    smoke is about the canonical happy path, not the veto path.
    """

    stage_log: FakeStageLogStore
    canonical_order: tuple[StageClass, ...] = field(
        default_factory=lambda: CANONICAL_STAGE_ORDER
    )

    def choose_dispatch(
        self,
        *,
        build_id: str,
        build_state: BuildState,
        permitted_stages: frozenset[StageClass],
        stage_hints: Mapping[StageClass, str],
        feature_catalogue: tuple[str, ...],
    ) -> DispatchChoice | None:
        for stage in self.canonical_order:
            if stage not in permitted_stages:
                continue
            if stage in PER_FEATURE_STAGES:
                feature_id = self._next_unapproved_feature(
                    build_id, stage, feature_catalogue
                )
                if feature_id is None:
                    continue
                return DispatchChoice(
                    stage=stage,
                    feature_id=feature_id,
                    rationale=f"canonical-order: {stage.value} for {feature_id}",
                    auto_approve=False,
                )
            if self.stage_log.is_approved(build_id, stage, None):
                continue
            return DispatchChoice(
                stage=stage,
                feature_id=None,
                rationale=f"canonical-order: {stage.value}",
                auto_approve=False,
            )
        return None

    def _next_unapproved_feature(
        self,
        build_id: str,
        stage: StageClass,
        feature_catalogue: tuple[str, ...],
    ) -> str | None:
        for fid in feature_catalogue:
            if not self.stage_log.is_approved(build_id, stage, fid):
                return fid
        return None


# ---------------------------------------------------------------------------
# Harness — composite pipeline + driver
# ---------------------------------------------------------------------------


@dataclass
class GreenfieldBriefPipeline:
    """Composite in-memory harness for the Mode A smoke + degraded suites.

    Owns every fake collaborator the supervisor consumes plus a small
    driver loop (:meth:`drive_until_paused`) that runs supervisor turns
    until the build either reaches PR-review (smoke happy path) or is
    flagged for human review (degraded path). All collaborators are
    accessible as attributes so tests can assert against call records,
    chronology rows, and PR URLs without re-resolving fixture handles.
    """

    supervisor: Supervisor
    stage_log: FakeStageLogStore
    state_machine: FakeStateMachineReader
    specialist_registry: FakeSpecialistRegistry
    specialist_dispatcher: FakeSpecialistDispatcher
    subprocess_dispatcher: FakeGuardkitSubprocessEngine
    autobuild_dispatcher: FakeAutobuildAsyncDispatcher
    pr_review_gate: FakePRReviewGate
    reasoning_model: CanonicalReasoningModel
    approval_channel: FakeApprovalChannel
    clock: FakeClock
    brief: str = SMOKE_BRIEF

    # ------------------------------------------------------------------
    # Driver
    # ------------------------------------------------------------------

    async def drive_until_paused(
        self,
        *,
        build_id: str = SMOKE_BUILD_ID,
        feature_id: str = SMOKE_FEATURE_ID,
        max_turns: int = 32,
    ) -> TurnReport:
        """Run ``supervisor.next_turn`` iteratively until terminal/paused.

        Args:
            build_id: Build identifier the smoke harness drives.
            feature_id: Single feature id used to populate the catalogue
                after the system-design stage approves.
            max_turns: Hard cap to stop a runaway loop. The smoke path
                needs eight turns; degraded path needs one — 32 leaves
                plenty of headroom for the chronology assertion to
                emit a useful failure if a future regression duplicates
                turns.

        Returns:
            The :class:`TurnReport` from the final supervisor turn.

        Raises:
            RuntimeError: if ``max_turns`` is reached without a terminal
                outcome — surfaces a runaway loop rather than silently
                truncating the chronology.
        """
        for _ in range(max_turns):
            report = await self.supervisor.next_turn(build_id)
            self._absorb(report=report, feature_id=feature_id)
            if self._is_paused_or_terminal(report):
                return report
        raise RuntimeError(
            f"smoke harness exceeded max_turns={max_turns} without reaching "
            f"a paused/terminal state; chronology has "
            f"{len(self.stage_log.chronology)} rows"
        )

    # ------------------------------------------------------------------
    # Internals — absorb dispatcher outcome between turns
    # ------------------------------------------------------------------

    def _absorb(self, *, report: TurnReport, feature_id: str) -> None:
        """Mutate the stage_log between turns based on dispatcher result."""
        if report.outcome is not TurnOutcome.DISPATCHED:
            return
        stage = report.chosen_stage
        if stage is None:
            return

        # PR-review is the constitutional terminator. The gate's
        # submission record is the gate decision itself (it does not
        # carry a ``status`` field) — annotate the chronology row with
        # the recorded ``gate_mode`` and PR URL and stop.
        if stage is StageClass.PULL_REQUEST_REVIEW:
            self._annotate_pr_terminal(report)
            return

        result = report.dispatch_result
        if not isinstance(result, dict):
            return
        status = result.get("status")
        if status == "approved":
            scope_fid = (
                report.chosen_feature_id if stage in PER_FEATURE_STAGES else None
            )
            self.stage_log.mark_approved(
                build_id=report.build_id, stage=stage, feature_id=scope_fid
            )
            # SYSTEM_DESIGN is the catalogue-producer stage. Populating
            # the catalogue on its approval is what unlocks the four
            # per-feature stages on subsequent turns.
            if stage is StageClass.SYSTEM_DESIGN:
                if not self.stage_log.feature_catalogue(report.build_id):
                    self.stage_log.set_catalogue(report.build_id, [feature_id])
        elif status == "flagged_for_review":
            self._annotate_flag_for_review(report=report, result=result)
            # Flag the build as PAUSED so subsequent supervisor turns
            # would short-circuit on the state-machine read; the driver
            # also stops via ``_is_paused_or_terminal``.
            self.state_machine.set_state(report.build_id, BuildState.PAUSED)

    def _annotate_pr_terminal(self, report: TurnReport) -> None:
        if not self.pr_review_gate.submissions:
            return
        latest_submission = self.pr_review_gate.submissions[-1]
        if not self.stage_log.chronology:
            return
        row = self.stage_log.chronology[-1]
        row["gate_mode"] = latest_submission["gate_mode"]
        row["pr_url"] = latest_submission["pr_url"]

    def _annotate_flag_for_review(
        self, *, report: TurnReport, result: dict[str, Any]
    ) -> None:
        if not self.stage_log.chronology:
            return
        row = self.stage_log.chronology[-1]
        row["gate_mode"] = "flag_for_review"
        flag_rationale = result.get("rationale")
        if flag_rationale:
            row["rationale"] = flag_rationale

    @staticmethod
    def _is_paused_or_terminal(report: TurnReport) -> bool:
        if report.outcome is TurnOutcome.TERMINAL:
            return True
        if report.outcome is not TurnOutcome.DISPATCHED:
            return False
        result = report.dispatch_result
        if isinstance(result, dict) and result.get("status") == "flagged_for_review":
            return True
        if report.chosen_stage is StageClass.PULL_REQUEST_REVIEW:
            return True
        return False


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def greenfield_brief_pipeline() -> GreenfieldBriefPipeline:
    """Composite harness with healthy specialists + canned subprocess artefacts.

    The smoke (default) configuration pre-populates the specialist
    registry with healthy ``product-owner`` and ``architect`` entries.
    Tests that exercise the degraded path mutate
    ``pipeline.specialist_registry.healthy.clear()`` before driving.
    """
    stage_log = FakeStageLogStore()
    state_machine = FakeStateMachineReader()
    async_task_reader = FakeAsyncTaskReader()
    approval = FakeApprovalChannel(auto_approve=True)
    registry = FakeSpecialistRegistry(
        healthy={
            StageClass.PRODUCT_OWNER: "po-specialist-healthy",
            StageClass.ARCHITECT: "architect-specialist-healthy",
        }
    )
    specialist_dispatcher = FakeSpecialistDispatcher(
        registry=registry, approval=approval
    )
    subprocess_dispatcher = FakeGuardkitSubprocessEngine(
        artefact_paths={
            StageClass.SYSTEM_ARCH: "/fake/artefacts/system-arch.md",
            StageClass.SYSTEM_DESIGN: "/fake/artefacts/system-design.md",
            StageClass.FEATURE_SPEC: "/fake/artefacts/feature-spec.md",
            StageClass.FEATURE_PLAN: "/fake/artefacts/feature-plan.md",
        }
    )
    autobuild_dispatcher = FakeAutobuildAsyncDispatcher()
    pr_review_gate = FakePRReviewGate()
    reasoning_model = CanonicalReasoningModel(stage_log=stage_log)

    supervisor = Supervisor(
        ordering_guard=StageOrderingGuard(),
        per_feature_sequencer=PerFeatureLoopSequencer(),
        constitutional_guard=ConstitutionalGuard(),
        state_reader=state_machine,
        ordering_stage_log_reader=stage_log,
        per_feature_stage_log_reader=stage_log,
        async_task_reader=async_task_reader,
        reasoning_model=reasoning_model,
        turn_recorder=stage_log,
        specialist_dispatcher=specialist_dispatcher,
        subprocess_dispatcher=subprocess_dispatcher,
        autobuild_dispatcher=autobuild_dispatcher,
        pr_review_gate=pr_review_gate,
        stage_hints={},
    )

    return GreenfieldBriefPipeline(
        supervisor=supervisor,
        stage_log=stage_log,
        state_machine=state_machine,
        specialist_registry=registry,
        specialist_dispatcher=specialist_dispatcher,
        subprocess_dispatcher=subprocess_dispatcher,
        autobuild_dispatcher=autobuild_dispatcher,
        pr_review_gate=pr_review_gate,
        reasoning_model=reasoning_model,
        approval_channel=approval,
        clock=FakeClock(),
        brief=SMOKE_BRIEF,
    )


# ---------------------------------------------------------------------------
# AC: Module exists at tests/integration/test_mode_a_smoke.py
# ---------------------------------------------------------------------------


class TestModuleExists:
    """AC: Test module exists at ``tests/integration/test_mode_a_smoke.py``."""

    def test_module_path_matches_acceptance_criterion(self) -> None:
        """The dunder ``__name__`` reflects the canonical AC path."""
        assert __name__.endswith("test_mode_a_smoke")

    def test_fixture_is_importable(self) -> None:
        """The named ``greenfield_brief_pipeline`` fixture is exposed."""
        assert greenfield_brief_pipeline is not None


# ---------------------------------------------------------------------------
# AC: Fixture wiring — registry + subprocess engine + approval auto-approve
# ---------------------------------------------------------------------------


class TestGreenfieldBriefPipelineFixture:
    """AC: ``greenfield_brief_pipeline`` brings up the documented harness."""

    def test_fixture_assembles_supervisor_with_required_collaborators(
        self, greenfield_brief_pipeline: GreenfieldBriefPipeline
    ) -> None:
        pipeline = greenfield_brief_pipeline
        assert isinstance(pipeline.supervisor, Supervisor)
        # Specialist registry pre-populated with healthy product-owner +
        # architect specialists (AC).
        assert (
            pipeline.specialist_registry.find_specialist(StageClass.PRODUCT_OWNER)
            is not None
        )
        assert (
            pipeline.specialist_registry.find_specialist(StageClass.ARCHITECT)
            is not None
        )

    def test_subprocess_engine_returns_canned_artefacts(
        self, greenfield_brief_pipeline: GreenfieldBriefPipeline
    ) -> None:
        # AC: fake GuardKit subprocess engine returning canned approved
        # artefacts (one path per subprocess stage).
        subprocess = greenfield_brief_pipeline.subprocess_dispatcher
        for stage in (
            StageClass.SYSTEM_ARCH,
            StageClass.SYSTEM_DESIGN,
            StageClass.FEATURE_SPEC,
            StageClass.FEATURE_PLAN,
        ):
            assert stage in subprocess.artefact_paths

    def test_approval_channel_auto_approves_by_default(
        self, greenfield_brief_pipeline: GreenfieldBriefPipeline
    ) -> None:
        # AC: stub approval channel that auto-approves.
        assert greenfield_brief_pipeline.approval_channel.auto_approve is True

    def test_fake_clock_pattern_is_wired(
        self, greenfield_brief_pipeline: GreenfieldBriefPipeline
    ) -> None:
        # AC: tests use the existing ``FakeClock`` pattern from
        # ``src/forge/pipeline/__init__.py`` for deterministic timing.
        assert isinstance(greenfield_brief_pipeline.clock, FakeClock)
        assert greenfield_brief_pipeline.clock.now() == 0.0


# ---------------------------------------------------------------------------
# AC: Smoke — drive a one-line brief to PR-awaiting-review
# ---------------------------------------------------------------------------


class TestMinimalGreenfieldSmoke:
    """AC: Single-feature run pauses at PR-review awaiting human approval."""

    @pytest.mark.asyncio
    async def test_smoke_pauses_at_pull_request_review(
        self, greenfield_brief_pipeline: GreenfieldBriefPipeline
    ) -> None:
        report = await greenfield_brief_pipeline.drive_until_paused(
            build_id=SMOKE_BUILD_ID, feature_id=SMOKE_FEATURE_ID
        )
        assert report.outcome is TurnOutcome.DISPATCHED
        assert report.chosen_stage is StageClass.PULL_REQUEST_REVIEW
        assert report.chosen_feature_id == SMOKE_FEATURE_ID

    @pytest.mark.asyncio
    async def test_smoke_records_mandatory_human_gate_mode_and_pr_url(
        self, greenfield_brief_pipeline: GreenfieldBriefPipeline
    ) -> None:
        await greenfield_brief_pipeline.drive_until_paused(
            build_id=SMOKE_BUILD_ID, feature_id=SMOKE_FEATURE_ID
        )
        terminal_row = greenfield_brief_pipeline.stage_log.chronology[-1]
        assert terminal_row["chosen_stage"] is StageClass.PULL_REQUEST_REVIEW
        assert terminal_row["gate_mode"] == "mandatory_human"
        # AC: PR URL recorded in stage_log.
        assert terminal_row["pr_url"]
        assert terminal_row["pr_url"].startswith(
            "https://github.com/example/forge/pull/"
        )

    @pytest.mark.asyncio
    async def test_smoke_chronology_lists_eight_canonical_stages_in_order(
        self, greenfield_brief_pipeline: GreenfieldBriefPipeline
    ) -> None:
        # AC: stage_log contains the eight-stage chain in canonical order
        # (Group G @data-integrity scenario covered as part of smoke).
        await greenfield_brief_pipeline.drive_until_paused(
            build_id=SMOKE_BUILD_ID, feature_id=SMOKE_FEATURE_ID
        )
        recorded_stages = [
            row["chosen_stage"]
            for row in greenfield_brief_pipeline.stage_log.chronology
            if row["outcome"] is TurnOutcome.DISPATCHED
        ]
        assert recorded_stages == list(CANONICAL_STAGE_ORDER), (
            f"chronology stages diverged from canonical order; got "
            f"{[s.value if s else None for s in recorded_stages]!r}"
        )

    @pytest.mark.asyncio
    async def test_smoke_dispatches_each_dispatcher_exactly_once(
        self, greenfield_brief_pipeline: GreenfieldBriefPipeline
    ) -> None:
        # The canary AC: if any non-constitutional gate fails to
        # auto-approve, a stage will be redispatched and these counts
        # will diverge.
        await greenfield_brief_pipeline.drive_until_paused(
            build_id=SMOKE_BUILD_ID, feature_id=SMOKE_FEATURE_ID
        )
        # Two specialist stages (product-owner + architect).
        assert len(greenfield_brief_pipeline.specialist_dispatcher.calls) == 2
        # Four subprocess stages (system-arch, system-design,
        # feature-spec, feature-plan).
        assert len(greenfield_brief_pipeline.subprocess_dispatcher.calls) == 4
        # One autobuild for the single feature.
        assert len(greenfield_brief_pipeline.autobuild_dispatcher.calls) == 1
        # One PR-review gate submission with auto_approve=False
        # (model defers to mandatory human approval).
        assert len(greenfield_brief_pipeline.pr_review_gate.submissions) == 1
        submission = greenfield_brief_pipeline.pr_review_gate.submissions[0]
        assert submission["auto_approve"] is False
        assert submission["feature_id"] == SMOKE_FEATURE_ID

    @pytest.mark.asyncio
    async def test_smoke_one_line_brief_is_threaded_through_harness(
        self, greenfield_brief_pipeline: GreenfieldBriefPipeline
    ) -> None:
        # AC: queue one-line brief — sanity-check the brief is the
        # documented one-liner so the smoke test name matches the
        # scenario it claims to cover.
        assert greenfield_brief_pipeline.brief == SMOKE_BRIEF
        assert "\n" not in greenfield_brief_pipeline.brief


# ---------------------------------------------------------------------------
# AC: Degraded — empty registry flags at product-owner; no architect dispatch
# ---------------------------------------------------------------------------


class TestNoSpecialistsDegradedPath:
    """AC: Empty specialist registry flags PR-review at product-owner stage.

    No architect dispatch occurs because the supervisor never advances
    past the flagged product-owner turn — the build is paused awaiting
    human review.
    """

    @pytest.mark.asyncio
    async def test_degraded_flags_product_owner_for_review(
        self, greenfield_brief_pipeline: GreenfieldBriefPipeline
    ) -> None:
        greenfield_brief_pipeline.specialist_registry.healthy.clear()
        report = await greenfield_brief_pipeline.drive_until_paused(
            build_id=SMOKE_BUILD_ID, feature_id=SMOKE_FEATURE_ID
        )
        assert report.outcome is TurnOutcome.DISPATCHED
        assert report.chosen_stage is StageClass.PRODUCT_OWNER
        # The dispatcher returned ``flagged_for_review``.
        assert isinstance(report.dispatch_result, dict)
        assert report.dispatch_result["status"] == "flagged_for_review"

    @pytest.mark.asyncio
    async def test_degraded_records_flag_for_review_with_degraded_rationale(
        self, greenfield_brief_pipeline: GreenfieldBriefPipeline
    ) -> None:
        greenfield_brief_pipeline.specialist_registry.healthy.clear()
        await greenfield_brief_pipeline.drive_until_paused(
            build_id=SMOKE_BUILD_ID, feature_id=SMOKE_FEATURE_ID
        )
        # Exactly one chronology row — the flagged product-owner turn.
        chronology = greenfield_brief_pipeline.stage_log.chronology
        assert len(chronology) == 1
        row = chronology[0]
        assert row["chosen_stage"] is StageClass.PRODUCT_OWNER
        assert row["gate_mode"] == "flag_for_review"
        assert "degraded" in row["rationale"]
        # The approval channel observed the flag — confirms the harness
        # threaded the degraded rationale through the documented seam.
        flag_log = greenfield_brief_pipeline.approval_channel.flag_log
        assert len(flag_log) == 1
        assert flag_log[0]["stage"] is StageClass.PRODUCT_OWNER

    @pytest.mark.asyncio
    async def test_degraded_does_not_dispatch_architect(
        self, greenfield_brief_pipeline: GreenfieldBriefPipeline
    ) -> None:
        greenfield_brief_pipeline.specialist_registry.healthy.clear()
        await greenfield_brief_pipeline.drive_until_paused(
            build_id=SMOKE_BUILD_ID, feature_id=SMOKE_FEATURE_ID
        )
        # AC: no architect dispatch occurred. The specialist dispatcher
        # was called exactly once (for product-owner) and never for
        # architect.
        called_stages = [
            call["stage"]
            for call in greenfield_brief_pipeline.specialist_dispatcher.calls
        ]
        assert called_stages == [StageClass.PRODUCT_OWNER]
        assert StageClass.ARCHITECT not in called_stages
        # And no subprocess / autobuild / PR-review dispatches happened
        # — the build paused at product-owner.
        assert greenfield_brief_pipeline.subprocess_dispatcher.calls == []
        assert greenfield_brief_pipeline.autobuild_dispatcher.calls == []
        assert greenfield_brief_pipeline.pr_review_gate.submissions == []

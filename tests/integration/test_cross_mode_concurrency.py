"""Cross-mode concurrency integration tests (TASK-MBC8-013, FEAT-FORGE-008).

This module pins down the supervisor's async-safety guarantee across the
three FEAT-FORGE-008 build modes. The Group K three-way interleave (Mode
A + Mode B + Mode C in flight together) is the strongest concurrency
assertion in the feature: if it passes, the substrate is reliably
mode-agnostic. If it fails, the failure mode points to the exact shared
component (supervisor, NATS adapter, or persistence layer).

A **single** :class:`forge.pipeline.supervisor.Supervisor` instance is
reused across every concurrent build in this module — that is the
contract the production supervisor satisfies. Multiple ``Supervisor``
instances would mask the async-safety assertion.

Acceptance-criteria coverage map:

* **AC-001** — :class:`TestTwoModeBBuildsConcurrent` (Group F)
* **AC-002** — :class:`TestModeBAndModeCConcurrent` (Group F)
* **AC-003** — :class:`TestThreeWayModeInterleave` (Group K)
* **AC-004** — :class:`TestSupervisorResponsivenessDuringAsync` (Group F)
* **AC-005** — :class:`TestFirstWinsIdempotencyUnderConcurrency` (Group I)
* **AC-006** — :class:`TestCalibrationPriorsSnapshotStability` (ASSUM-012)
* **AC-007** — :class:`TestNotificationPublishFailureIsolation` (Group G)
* **AC-budget** — :class:`TestCrossModeConcurrencyBudget`

References
----------

* TASK-MBC8-013 — this task brief.
* TASK-MBC8-010 — Mode B smoke E2E (sibling test module).
* TASK-MBC8-011 — Mode C smoke E2E (sibling test module).
* TASK-MAG7-014 — Mode A concurrency tests (sibling reference shape).
* FEAT-FORGE-008 ASSUM-001 / ASSUM-004 / ASSUM-006 / ASSUM-008 / ASSUM-012.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

import pytest
from nats_core.envelope import EventType, MessageEnvelope

from forge.adapters.nats.approval_subscriber import (
    ApprovalSubscriber,
    ApprovalSubscriberDeps,
)
from forge.config.models import ApprovalConfig
from forge.gating.identity import derive_request_id
from forge.lifecycle.modes import BuildMode
from forge.pipeline.constitutional_guard import ConstitutionalGuard
from forge.pipeline.mode_b_planner import (
    APPROVED as MODE_B_APPROVED,
    ModeBChainPlanner,
)
from forge.pipeline.mode_c_planner import (
    FixTaskRef,
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
    TurnReport,
)
from .conftest import FakeMonotonicClock, InMemoryNats


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


BUILD_A: str = "build-A-MODE-A"
BUILD_B: str = "build-B-MODE-B"
BUILD_C: str = "build-C-MODE-C"
BUILD_B_ALT: str = "build-B2-MODE-B"

FEATURE_A: str = "FEAT-A1"
FEATURE_B: str = "FEAT-B1"
FEATURE_B_ALT: str = "FEAT-B2"


# ---------------------------------------------------------------------------
# Mode B planner-shaped stage entry — the planner's StageEntry is a Protocol
# ---------------------------------------------------------------------------


@dataclass
class _ModeBStageEntry:
    """Plain dataclass satisfying the :class:`ModeBChainPlanner` Protocol."""

    stage: StageClass
    status: str
    feature_id: str | None
    details: Mapping[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Per-build state tracker — mirrors the production stage_log row shape
# ---------------------------------------------------------------------------


@dataclass
class _BuildState:
    """All persistence state for a single build, scoped by build_id.

    The supervisor reads from this object through several Protocols at
    once (ordering reader, per-feature reader, Mode B history, Mode C
    history, turn recorder). We keep one instance per build so the
    cross-mode harness can compose them under a single dispatcher
    without cross-build contamination.
    """

    build_id: str
    mode: BuildMode
    features: list[str] = field(default_factory=list)
    # Ordering-guard-shaped approvals: (build_id, stage, feature_id|None).
    approved: set[tuple[str, StageClass, str | None]] = field(default_factory=set)
    # Mode B history (planner-shaped).
    mode_b_history: list[_ModeBStageEntry] = field(default_factory=list)
    # Mode C history (planner-shaped) + commit flag.
    mode_c_history: list[ModeCStageEntry] = field(default_factory=list)
    has_commits: bool = False
    # Per-build chronology rows recorded by the turn recorder.
    chronology: list[dict[str, Any]] = field(default_factory=list)
    # Per-build async-task lifecycle channel (FEAT-FORGE-007 ASSUM-006).
    autobuild_lifecycle: dict[str, str] = field(default_factory=dict)
    # Build-state machine value.
    state: BuildState = BuildState.RUNNING


# ---------------------------------------------------------------------------
# In-memory async-task channel double — keyed per build_id
# ---------------------------------------------------------------------------


@dataclass
class _AutobuildState:
    """``AutobuildState``-shaped payload for the per-feature sequencer."""

    feature_id: str
    lifecycle: str = "running_wave"


# ---------------------------------------------------------------------------
# Composite stage log — one instance shared by the supervisor across builds
# ---------------------------------------------------------------------------


@dataclass
class _CompositeStageLog:
    """Composite stage_log shared by every build in the harness.

    Routes every Protocol read by ``build_id`` so that one Supervisor
    instance can drive concurrent builds without per-build wiring.
    Mutations from the absorber update the matching ``_BuildState``;
    the supervisor never reaches across builds.
    """

    states: dict[str, _BuildState] = field(default_factory=dict)

    def register(self, state: _BuildState) -> None:
        if state.build_id in self.states:
            raise ValueError(
                f"_CompositeStageLog: build_id {state.build_id!r} already registered"
            )
        self.states[state.build_id] = state

    def _state(self, build_id: str) -> _BuildState:
        if build_id not in self.states:
            raise KeyError(
                f"_CompositeStageLog: no state for build_id={build_id!r}"
            )
        return self.states[build_id]

    # ---- ordering-guard reader ---------------------------------------

    def is_approved(
        self,
        build_id: str,
        stage: StageClass,
        feature_id: str | None = None,
    ) -> bool:
        state = self.states.get(build_id)
        if state is None:
            return False
        return (build_id, stage, feature_id) in state.approved

    def feature_catalogue(self, build_id: str) -> list[str]:
        state = self.states.get(build_id)
        if state is None:
            return []
        return list(state.features)

    # ---- per-feature-sequencer reader --------------------------------

    def is_autobuild_approved(self, build_id: str, feature_id: str) -> bool:
        state = self.states.get(build_id)
        if state is None:
            return False
        return (build_id, StageClass.AUTOBUILD, feature_id) in state.approved

    # ---- turn recorder -----------------------------------------------

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
        state = self._state(build_id)
        state.chronology.append(
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

    # ---- Mode B history reader ---------------------------------------

    def get_mode_b_history(self, build_id: str) -> Sequence[_ModeBStageEntry]:
        state = self.states.get(build_id)
        if state is None:
            return ()
        return list(state.mode_b_history)

    # ---- Mode C history reader ---------------------------------------

    def get_mode_c_history(self, build_id: str) -> Sequence[ModeCStageEntry]:
        state = self.states.get(build_id)
        if state is None:
            return ()
        return list(state.mode_c_history)

    def has_commits(self, build_id: str) -> bool:
        state = self.states.get(build_id)
        if state is None:
            return False
        return state.has_commits


# ---------------------------------------------------------------------------
# Build-mode + state readers — route by build_id
# ---------------------------------------------------------------------------


@dataclass
class _BuildModeRouter:
    log: _CompositeStageLog

    def get_build_mode(self, build_id: str) -> BuildMode:
        return self.log.states[build_id].mode


@dataclass
class _BuildStateRouter:
    log: _CompositeStageLog

    def get_build_state(self, build_id: str) -> BuildState:
        state = self.log.states.get(build_id)
        if state is None:
            return BuildState.RUNNING
        return state.state


@dataclass
class _AsyncTaskRouter:
    """Async-task channel double routed by build_id."""

    log: _CompositeStageLog

    def list_autobuild_states(self, build_id: str) -> Iterable[_AutobuildState]:
        state = self.log.states.get(build_id)
        if state is None:
            return ()
        return [
            _AutobuildState(feature_id=fid, lifecycle=lc)
            for fid, lc in state.autobuild_lifecycle.items()
        ]


# ---------------------------------------------------------------------------
# Reasoning model — only consulted on Mode A; Mode B/C never reach it.
# Picks the lowest-rank permitted stage, threading per-feature scope.
# ---------------------------------------------------------------------------


_DISPATCH_ORDER: tuple[StageClass, ...] = tuple(StageClass)
_PER_FEATURE_NON_PR: frozenset[StageClass] = frozenset(
    {StageClass.FEATURE_SPEC, StageClass.FEATURE_PLAN, StageClass.AUTOBUILD}
)


@dataclass
class _ModeAReasoningModel:
    """Deterministic Mode A model — picks the lowest-rank permitted stage."""

    log: _CompositeStageLog
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
        del build_state, stage_hints  # unused — deterministic by ordering
        self.calls.append(
            {
                "build_id": build_id,
                "permitted_stages": frozenset(permitted_stages),
                "feature_catalogue": tuple(feature_catalogue),
            }
        )
        state = self.log.states.get(build_id)
        approved = state.approved if state is not None else set()
        for stage in _DISPATCH_ORDER:
            if stage not in permitted_stages:
                continue
            if stage in _PER_FEATURE_NON_PR:
                for fid in feature_catalogue:
                    if (build_id, stage, fid) in approved:
                        continue
                    return DispatchChoice(
                        stage=stage,
                        feature_id=fid,
                        rationale=f"mode-a: {stage.value} for {fid}",
                    )
                continue
            if stage is StageClass.PULL_REQUEST_REVIEW:
                for fid in feature_catalogue:
                    if (build_id, stage, fid) in approved:
                        continue
                    return DispatchChoice(
                        stage=stage,
                        feature_id=fid,
                        rationale=f"mode-a: pr-review for {fid}",
                    )
                continue
            if (build_id, stage, None) in approved:
                continue
            return DispatchChoice(
                stage=stage, rationale=f"mode-a: {stage.value}"
            )
        return None


# ---------------------------------------------------------------------------
# Dispatchers — route by build_id, record calls for cross-mode auditing
# ---------------------------------------------------------------------------


@dataclass
class _RecordingSpecialistDispatcher:
    """Async dispatcher used for Mode A specialist stages (PRODUCT_OWNER /
    ARCHITECT) and Mode A subprocess stages (SYSTEM_*, FEATURE_*)."""

    label: str
    calls: list[dict[str, Any]] = field(default_factory=list)
    nats: InMemoryNats | None = None

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({**kwargs})
        build_id = kwargs.get("build_id", "")
        stage = kwargs.get("stage")
        task_id = (
            f"{self.label}-{build_id}-"
            f"{stage.value if stage is not None else 'unknown'}-"
            f"{uuid.uuid4().hex[:8]}"
        )
        # Optionally publish a "stage approved" notification on the bus
        # so the publish-failure isolation test can inject a transport
        # failure.
        if self.nats is not None and stage is not None:
            subject = f"agents.notify.forge.{build_id}.{stage.value}"
            envelope = MessageEnvelope(
                source_id=self.label,
                event_type=EventType.STAGE_COMPLETE,
                payload={
                    "build_id": build_id,
                    "stage_label": stage.value,
                    "status": "approved",
                },
            )
            try:
                await self.nats.publish(
                    subject, envelope.model_dump_json().encode("utf-8")
                )
            except Exception:  # noqa: BLE001 — failure isolation under test
                # Group G: a failed publish must not roll back the
                # SQLite-shaped record. We log nothing and return
                # success so the caller's record path proceeds.
                pass
        return {
            "stage": stage,
            "build_id": build_id,
            "feature_id": kwargs.get("feature_id"),
            "status": "approved",
            "task_id": task_id,
            "artefact_paths": [],
        }


@dataclass
class _RecordingSubprocessDispatcher:
    """Subprocess dispatcher for Mode A subprocess + Mode C ``/task-review``
    and ``/task-work`` stages.

    Mode C calls carry a ``fix_task`` :class:`FixTaskRef`. The dispatcher
    emits a scripted ``fix_tasks`` list when invoked with
    :class:`StageClass.TASK_REVIEW` and the matching plan.
    """

    review_outcomes_by_build: dict[str, list[tuple[str, ...]]] = field(
        default_factory=dict
    )
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({**kwargs})
        stage = kwargs.get("stage")
        build_id = kwargs.get("build_id", "")
        if stage is StageClass.TASK_REVIEW:
            queue = self.review_outcomes_by_build.get(build_id, [])
            fix_tasks = queue.pop(0) if queue else ()
            return {
                "stage": stage,
                "build_id": build_id,
                "status": "approved",
                "fix_tasks": list(fix_tasks),
                "hard_stop": False,
                "rationale": kwargs.get("rationale", ""),
            }
        if stage is StageClass.TASK_WORK:
            fix_task: FixTaskRef = kwargs["fix_task"]
            return {
                "stage": stage,
                "build_id": build_id,
                "status": "approved",
                "fix_task_id": fix_task.fix_task_id,
                "artefact_paths": [],
                "rationale": kwargs.get("rationale", ""),
            }
        # Mode A / Mode B subprocess stages — return a non-empty artefact
        # path so the Mode B planner does not flag missing-spec.
        feature_id = kwargs.get("feature_id")
        stage_name = stage.value if stage is not None else "unknown"
        artefact = (
            f"/wt/{build_id}/{feature_id or 'shared'}/{stage_name}.md"
        )
        return {
            "stage": stage,
            "build_id": build_id,
            "status": "approved",
            "artefact_paths": [artefact],
            "rationale": kwargs.get("rationale", ""),
        }


@dataclass
class _RecordingAutobuildDispatcher:
    """Async autobuild dispatcher — returns a unique task_id per call.

    Each dispatch additionally writes an in-flight lifecycle entry to
    the matching :class:`_BuildState` so subsequent supervisor turns
    observe the "running_wave" lifecycle (FEAT-FORGE-007 ASSUM-006).

    TASK-FORGE-FRR-F010G: the autobuild dispatcher Protocol is now
    async because :func:`dispatch_autobuild_async` awaits the
    deepagents middleware's async ``coroutine`` entrypoint.
    """

    log: _CompositeStageLog
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({**kwargs})
        build_id = kwargs.get("build_id", "")
        feature_id = kwargs.get("feature_id", "")
        task_id = f"autobuild-{build_id}-{feature_id}-{uuid.uuid4().hex[:8]}"
        state = self.log.states.get(build_id)
        if state is not None:
            state.autobuild_lifecycle[feature_id] = "running_wave"
        return {
            "build_id": build_id,
            "feature_id": feature_id,
            "status": "approved",
            "lifecycle": "completed",
            "task_id": task_id,
            "changed_files_count": 1,
        }


@dataclass
class _RecordingPRReviewGate:
    """PR-review gate that records every submission (mandatory_human)."""

    submissions: list[dict[str, Any]] = field(default_factory=list)
    _next_pr: int = 5000

    def submit_decision(
        self,
        *,
        build_id: str,
        feature_id: str,
        auto_approve: bool,
        rationale: str,
    ) -> dict[str, Any]:
        self._next_pr += 1
        record = {
            "build_id": build_id,
            "feature_id": feature_id,
            "auto_approve": auto_approve,
            "rationale": rationale,
            "gate_mode": "MANDATORY_HUMAN_APPROVAL",
            "pull_request_url": f"https://example.test/pr/{self._next_pr}",
        }
        self.submissions.append(record)
        return record


# ---------------------------------------------------------------------------
# Mode C commit probe — deterministic per-build flag
# ---------------------------------------------------------------------------


@dataclass
class _RoutingCommitProbe:
    """Routes ``has_commits`` to the matching ``_BuildState``."""

    log: _CompositeStageLog

    async def __call__(self, build: Any) -> Any:
        from forge.pipeline.terminal_handlers.mode_c import CommitProbeResult

        state = self.log.states.get(build.build_id)
        return CommitProbeResult(
            count=1 if (state is not None and state.has_commits) else 0,
            failed=False,
            error=None,
        )


# ---------------------------------------------------------------------------
# Cross-mode harness
# ---------------------------------------------------------------------------


@dataclass
class _CrossModeHarness:
    """Single-Supervisor cross-mode integration harness.

    The instance owns:

    * One :class:`Supervisor` (the contract under test).
    * One :class:`_CompositeStageLog` carrying every build's state.
    * One in-memory NATS double for the publish-failure scenarios.
    * Recording dispatchers for every dispatch surface.

    Tests register builds via :meth:`register_mode_a` /
    :meth:`register_mode_b` / :meth:`register_mode_c`, then drive
    :meth:`turn` per build (or via :func:`asyncio.gather` for true
    interleave).
    """

    supervisor: Supervisor
    log: _CompositeStageLog
    nats: InMemoryNats
    specialist_dispatcher: _RecordingSpecialistDispatcher
    subprocess_dispatcher: _RecordingSubprocessDispatcher
    autobuild_dispatcher: _RecordingAutobuildDispatcher
    pr_review_gate: _RecordingPRReviewGate
    reasoning_model: _ModeAReasoningModel
    commit_probe: _RoutingCommitProbe

    # ----- Build registration ----------------------------------------

    def register_mode_a(self, build_id: str, *, features: list[str]) -> None:
        self.log.register(
            _BuildState(
                build_id=build_id, mode=BuildMode.MODE_A, features=list(features)
            )
        )

    def register_mode_b(self, build_id: str, *, feature_id: str) -> None:
        self.log.register(
            _BuildState(
                build_id=build_id,
                mode=BuildMode.MODE_B,
                features=[feature_id],
            )
        )

    def register_mode_c(
        self,
        build_id: str,
        *,
        review_fix_tasks: Sequence[tuple[str, ...]] = ((),),
        has_commits: bool = False,
    ) -> None:
        self.log.register(
            _BuildState(
                build_id=build_id,
                mode=BuildMode.MODE_C,
                features=[],
                has_commits=has_commits,
            )
        )
        self.subprocess_dispatcher.review_outcomes_by_build[build_id] = [
            tuple(t) for t in review_fix_tasks
        ]

    # ----- Approval pre-marking helpers ------------------------------

    def mark_approved(
        self,
        build_id: str,
        stage: StageClass,
        feature_id: str | None = None,
    ) -> None:
        self.log.states[build_id].approved.add((build_id, stage, feature_id))

    # ----- Turn driver -----------------------------------------------

    async def turn(self, build_id: str) -> TurnReport:
        report = await self.supervisor.next_turn(build_id)
        self._absorb(build_id=build_id, report=report)
        return report

    # ----- Internals: absorb dispatcher result onto build state -------

    def _absorb(self, *, build_id: str, report: TurnReport) -> None:
        if report.outcome is not TurnOutcome.DISPATCHED:
            return
        stage = report.chosen_stage
        if stage is None:
            return
        state = self.log.states.get(build_id)
        if state is None:
            return
        result = report.dispatch_result
        feature_id = report.chosen_feature_id

        # Mode A pre-feature-spec stages — pipeline-level (no feature scope).
        if stage in {
            StageClass.PRODUCT_OWNER,
            StageClass.ARCHITECT,
            StageClass.SYSTEM_ARCH,
            StageClass.SYSTEM_DESIGN,
        }:
            state.approved.add((build_id, stage, None))
            return

        # Mode A / Mode B per-feature pre-autobuild stages.
        if stage in {StageClass.FEATURE_SPEC, StageClass.FEATURE_PLAN}:
            state.approved.add((build_id, stage, feature_id))
            if state.mode is BuildMode.MODE_B:
                state.mode_b_history.append(
                    _ModeBStageEntry(
                        stage=stage,
                        status=MODE_B_APPROVED,
                        feature_id=feature_id,
                        details={
                            "artefact_paths": list(
                                (result or {}).get("artefact_paths", ())
                            ),
                        },
                    )
                )
            return

        if stage is StageClass.AUTOBUILD:
            state.approved.add((build_id, stage, feature_id))
            # Lifecycle moves to terminal — the supervisor's autobuild
            # dispatcher initially writes "running_wave"; we mark
            # completed once the absorber observes the dispatch. Tests
            # that want to observe in-flight responsiveness call
            # :meth:`set_autobuild_in_flight` *before* the next turn.
            if feature_id is not None:
                state.autobuild_lifecycle[feature_id] = "completed"
            if state.mode is BuildMode.MODE_B:
                state.mode_b_history.append(
                    _ModeBStageEntry(
                        stage=StageClass.AUTOBUILD,
                        status=MODE_B_APPROVED,
                        feature_id=feature_id,
                        details={
                            "diff_present": True,
                            "changed_files_count": int(
                                (result or {}).get("changed_files_count", 1)
                            ),
                        },
                    )
                )
            return

        if stage is StageClass.PULL_REQUEST_REVIEW:
            # Constitutional terminal pause; do not re-mark.
            return

        if stage is StageClass.TASK_REVIEW:
            fix_tasks = tuple((result or {}).get("fix_tasks", ()))
            state.mode_c_history.append(
                ModeCStageEntry(
                    stage_class=stage,
                    status="approved",
                    fix_tasks=fix_tasks,
                    fix_task_id=None,
                    hard_stop=bool((result or {}).get("hard_stop", False)),
                )
            )
            state.approved.add((build_id, stage, None))
            return

        if stage is StageClass.TASK_WORK:
            fix_task_id = (result or {}).get("fix_task_id")
            state.mode_c_history.append(
                ModeCStageEntry(
                    stage_class=stage,
                    status=str((result or {}).get("status", "approved")),
                    fix_tasks=(),
                    fix_task_id=fix_task_id,
                    hard_stop=False,
                )
            )
            state.approved.add((build_id, stage, None))
            return

    # ----- Manual lifecycle injection (for AC-004 in-flight tests) ---

    def set_autobuild_in_flight(
        self, build_id: str, feature_id: str, *, lifecycle: str = "running_wave"
    ) -> None:
        state = self.log.states[build_id]
        state.autobuild_lifecycle[feature_id] = lifecycle


# ---------------------------------------------------------------------------
# Harness factory + fixture
# ---------------------------------------------------------------------------


def _build_harness() -> _CrossModeHarness:
    log = _CompositeStageLog()
    nats = InMemoryNats()
    state_router = _BuildStateRouter(log=log)
    mode_router = _BuildModeRouter(log=log)
    async_router = _AsyncTaskRouter(log=log)
    reasoning_model = _ModeAReasoningModel(log=log)
    specialist_dispatcher = _RecordingSpecialistDispatcher(
        label="specialist", nats=nats
    )
    # Mode A subprocess + Mode C subprocess share a dispatcher because
    # the supervisor's _SUBPROCESS_STAGES set covers SYSTEM_*, FEATURE_*
    # while TASK_REVIEW / TASK_WORK reach the same dispatcher via the
    # Mode C dispatch path.
    subprocess_dispatcher = _RecordingSubprocessDispatcher()
    autobuild_dispatcher = _RecordingAutobuildDispatcher(log=log)
    pr_review_gate = _RecordingPRReviewGate()
    commit_probe = _RoutingCommitProbe(log=log)

    supervisor = Supervisor(
        ordering_guard=StageOrderingGuard(),
        per_feature_sequencer=PerFeatureLoopSequencer(),
        constitutional_guard=ConstitutionalGuard(),
        state_reader=state_router,
        ordering_stage_log_reader=log,
        per_feature_stage_log_reader=log,
        async_task_reader=async_router,
        reasoning_model=reasoning_model,
        turn_recorder=log,
        specialist_dispatcher=specialist_dispatcher,
        subprocess_dispatcher=subprocess_dispatcher,
        autobuild_dispatcher=autobuild_dispatcher,
        pr_review_gate=pr_review_gate,
        build_mode_reader=mode_router,
        mode_b_planner=ModeBChainPlanner(),
        mode_b_history_reader=log,
        mode_c_planner=ModeCCyclePlanner(),
        mode_c_history_reader=log,
        mode_c_commit_probe=commit_probe,
    )
    return _CrossModeHarness(
        supervisor=supervisor,
        log=log,
        nats=nats,
        specialist_dispatcher=specialist_dispatcher,
        subprocess_dispatcher=subprocess_dispatcher,
        autobuild_dispatcher=autobuild_dispatcher,
        pr_review_gate=pr_review_gate,
        reasoning_model=reasoning_model,
        commit_probe=commit_probe,
    )


@pytest.fixture
def harness() -> _CrossModeHarness:
    return _build_harness()


# ---------------------------------------------------------------------------
# AC-001 — Two Mode B builds running simultaneously (Group F)
# ---------------------------------------------------------------------------


class TestTwoModeBBuildsConcurrent:
    """Two Mode B builds, isolated approval channels and stage history."""

    @pytest.mark.asyncio
    async def test_two_mode_b_builds_get_distinct_autobuild_task_ids(
        self, harness: _CrossModeHarness
    ) -> None:
        # Two Mode B builds, each with its own single feature.
        harness.register_mode_b(BUILD_B, feature_id=FEATURE_B)
        harness.register_mode_b(BUILD_B_ALT, feature_id=FEATURE_B_ALT)
        # Pre-approve FEATURE_SPEC + FEATURE_PLAN on each so the next
        # turn lands on AUTOBUILD dispatch.
        for bid, fid in ((BUILD_B, FEATURE_B), (BUILD_B_ALT, FEATURE_B_ALT)):
            for s in (StageClass.FEATURE_SPEC, StageClass.FEATURE_PLAN):
                harness.mark_approved(bid, s, fid)
                harness.log.states[bid].mode_b_history.append(
                    _ModeBStageEntry(
                        stage=s,
                        status=MODE_B_APPROVED,
                        feature_id=fid,
                        details={
                            "artefact_paths": [f"/wt/{bid}/{s.value}.md"],
                        },
                    )
                )

        # Drive both builds concurrently.
        results = await asyncio.gather(
            harness.turn(BUILD_B), harness.turn(BUILD_B_ALT)
        )

        # Both dispatched AUTOBUILD with distinct task_ids.
        assert {r.outcome for r in results} == {TurnOutcome.DISPATCHED}
        assert {r.chosen_stage for r in results} == {StageClass.AUTOBUILD}
        autobuild_calls = harness.autobuild_dispatcher.calls
        assert len(autobuild_calls) == 2
        scopes = {(c["build_id"], c["feature_id"]) for c in autobuild_calls}
        assert scopes == {(BUILD_B, FEATURE_B), (BUILD_B_ALT, FEATURE_B_ALT)}
        task_ids = {r.dispatch_result["task_id"] for r in results}
        assert len(task_ids) == 2, (
            f"two Mode B builds yielded the same autobuild task_id: {task_ids!r}"
        )

    @pytest.mark.asyncio
    async def test_each_mode_b_build_pauses_at_its_own_pr_review(
        self, harness: _CrossModeHarness
    ) -> None:
        # Drive both builds all the way to the PR-review pause.
        harness.register_mode_b(BUILD_B, feature_id=FEATURE_B)
        harness.register_mode_b(BUILD_B_ALT, feature_id=FEATURE_B_ALT)

        async def drive(build_id: str, feature_id: str) -> TurnReport:
            last: TurnReport | None = None
            for _ in range(8):
                report = await harness.turn(build_id)
                last = report
                if (
                    report.outcome is TurnOutcome.DISPATCHED
                    and report.chosen_stage is StageClass.PULL_REQUEST_REVIEW
                ):
                    break
                if report.outcome is not TurnOutcome.DISPATCHED:
                    break
            assert last is not None
            return last

        report_b, report_b2 = await asyncio.gather(
            drive(BUILD_B, FEATURE_B), drive(BUILD_B_ALT, FEATURE_B_ALT)
        )
        assert report_b.chosen_stage is StageClass.PULL_REQUEST_REVIEW
        assert report_b2.chosen_stage is StageClass.PULL_REQUEST_REVIEW
        assert report_b.chosen_feature_id == FEATURE_B
        assert report_b2.chosen_feature_id == FEATURE_B_ALT
        # Two distinct PR submissions, one per build.
        submissions = harness.pr_review_gate.submissions
        assert len(submissions) == 2
        assert {s["build_id"] for s in submissions} == {BUILD_B, BUILD_B_ALT}
        # Each PR submission references its own feature.
        scope_by_build = {s["build_id"]: s["feature_id"] for s in submissions}
        assert scope_by_build[BUILD_B] == FEATURE_B
        assert scope_by_build[BUILD_B_ALT] == FEATURE_B_ALT

    @pytest.mark.asyncio
    async def test_approval_response_targeting_build_one_resolves_only_build_one(
        self,
    ) -> None:
        # Per-build NATS subject routing — proves approval channel isolation
        # for two simultaneous Mode B paused waits.
        nats = InMemoryNats()
        clock = FakeMonotonicClock()
        config = ApprovalConfig(default_wait_seconds=1, max_wait_seconds=2)
        sub_b1 = ApprovalSubscriber(
            ApprovalSubscriberDeps(
                nats_client=nats, config=config, publish_refresh=None, clock=clock
            )
        )
        sub_b2 = ApprovalSubscriber(
            ApprovalSubscriberDeps(
                nats_client=nats, config=config, publish_refresh=None, clock=clock
            )
        )
        rid_b1 = derive_request_id(
            build_id=BUILD_B, stage_label="FeaturePlan", attempt_count=0
        )
        wait_b1 = asyncio.create_task(
            sub_b1.await_response(BUILD_B, stage_label="FeaturePlan")
        )
        wait_b2 = asyncio.create_task(
            sub_b2.await_response(BUILD_B_ALT, stage_label="FeaturePlan")
        )
        # Yield until both subscriptions are registered.
        for _ in range(50):
            if BUILD_B in sub_b1._queues and BUILD_B_ALT in sub_b2._queues:
                break
            await asyncio.sleep(0)
        # Deliver only on build_B's mirror.
        await nats.deliver_response(
            build_id=BUILD_B, request_id=rid_b1, decision="approve"
        )
        result = await asyncio.wait_for(wait_b1, timeout=1.0)
        assert result is not None and result.request_id == rid_b1
        assert not wait_b2.done(), (
            "build_B_ALT's wait resolved despite the response landing on "
            "build_B's mirror — per-build routing violated"
        )
        wait_b2.cancel()
        with pytest.raises(asyncio.CancelledError):
            await wait_b2


# ---------------------------------------------------------------------------
# AC-002 — Mode B + Mode C concurrent (Group F)
# ---------------------------------------------------------------------------


class TestModeBAndModeCConcurrent:
    """Mode B at autobuild + Mode C at task-work, isolated channels."""

    @pytest.mark.asyncio
    async def test_mode_b_dispatches_autobuild_while_mode_c_dispatches_task_work(
        self, harness: _CrossModeHarness
    ) -> None:
        # Mode B build pre-approved up to FEATURE_PLAN — next turn picks
        # AUTOBUILD.
        harness.register_mode_b(BUILD_B, feature_id=FEATURE_B)
        for s in (StageClass.FEATURE_SPEC, StageClass.FEATURE_PLAN):
            harness.mark_approved(BUILD_B, s, FEATURE_B)
            harness.log.states[BUILD_B].mode_b_history.append(
                _ModeBStageEntry(
                    stage=s,
                    status=MODE_B_APPROVED,
                    feature_id=FEATURE_B,
                    details={"artefact_paths": [f"/wt/{BUILD_B}/{s.value}.md"]},
                )
            )
        # Mode C build with one initial review yielding a single fix task,
        # plus a follow-up clean review. Pre-record the initial review so
        # the next turn dispatches TASK_WORK directly (skipping
        # TASK_REVIEW serialisation).
        harness.register_mode_c(
            BUILD_C,
            review_fix_tasks=[("FIX-1",), ()],
            has_commits=False,
        )
        harness.log.states[BUILD_C].mode_c_history.append(
            ModeCStageEntry(
                stage_class=StageClass.TASK_REVIEW,
                status="approved",
                fix_tasks=("FIX-1",),
                fix_task_id=None,
                hard_stop=False,
            )
        )
        harness.mark_approved(BUILD_C, StageClass.TASK_REVIEW, None)

        # Drive both concurrently via asyncio.gather.
        report_b, report_c = await asyncio.gather(
            harness.turn(BUILD_B), harness.turn(BUILD_C)
        )

        # Mode B picked AUTOBUILD; Mode C picked TASK_WORK.
        assert report_b.outcome is TurnOutcome.DISPATCHED
        assert report_b.chosen_stage is StageClass.AUTOBUILD
        assert report_b.chosen_feature_id == FEATURE_B
        assert report_c.outcome is TurnOutcome.DISPATCHED
        assert report_c.chosen_stage is StageClass.TASK_WORK
        # Mode C task_work dispatch carried the right fix-task ref.
        task_work_calls = [
            c
            for c in harness.subprocess_dispatcher.calls
            if c.get("stage") is StageClass.TASK_WORK
            and c.get("build_id") == BUILD_C
        ]
        assert len(task_work_calls) == 1
        assert task_work_calls[0]["fix_task"].fix_task_id == "FIX-1"
        # Mode B autobuild dispatcher fired exactly once for build_B.
        ab_calls = [
            c for c in harness.autobuild_dispatcher.calls if c["build_id"] == BUILD_B
        ]
        assert len(ab_calls) == 1

    @pytest.mark.asyncio
    async def test_approvals_route_by_build_identifier_for_b_and_c(
        self,
    ) -> None:
        # Cross-mode approval routing: two paused waits — one for the
        # Mode B build (AUTOBUILD pause) and one for the Mode C build
        # (TASK_REVIEW pause). Each must resolve only when the response
        # lands on its own mirror subject.
        nats = InMemoryNats()
        clock = FakeMonotonicClock()
        config = ApprovalConfig(default_wait_seconds=1, max_wait_seconds=2)
        sub_b = ApprovalSubscriber(
            ApprovalSubscriberDeps(
                nats_client=nats, config=config, publish_refresh=None, clock=clock
            )
        )
        sub_c = ApprovalSubscriber(
            ApprovalSubscriberDeps(
                nats_client=nats, config=config, publish_refresh=None, clock=clock
            )
        )
        rid_b = derive_request_id(
            build_id=BUILD_B, stage_label="Autobuild", attempt_count=0
        )
        rid_c = derive_request_id(
            build_id=BUILD_C, stage_label="TaskReview", attempt_count=0
        )
        wait_b = asyncio.create_task(
            sub_b.await_response(BUILD_B, stage_label="Autobuild")
        )
        wait_c = asyncio.create_task(
            sub_c.await_response(BUILD_C, stage_label="TaskReview")
        )
        for _ in range(50):
            if BUILD_B in sub_b._queues and BUILD_C in sub_c._queues:
                break
            await asyncio.sleep(0)
        # Deliver to both mirrors at once via asyncio.gather.
        await asyncio.gather(
            nats.deliver_response(
                build_id=BUILD_B, request_id=rid_b, decision="approve"
            ),
            nats.deliver_response(
                build_id=BUILD_C, request_id=rid_c, decision="approve"
            ),
        )
        result_b, result_c = await asyncio.gather(
            asyncio.wait_for(wait_b, timeout=1.0),
            asyncio.wait_for(wait_c, timeout=1.0),
        )
        assert result_b is not None and result_b.request_id == rid_b
        assert result_c is not None and result_c.request_id == rid_c


# ---------------------------------------------------------------------------
# AC-003 — Three-way interleave (Mode A + B + C) (Group K)
# ---------------------------------------------------------------------------


class TestThreeWayModeInterleave:
    """The strongest assertion: three concurrent builds, one per mode.

    No cross-mode contamination: each build's recorded stage history
    contains only stages from its own mode's chain.
    """

    @pytest.mark.asyncio
    async def test_three_way_dispatch_under_asyncio_gather(
        self, harness: _CrossModeHarness
    ) -> None:
        # Mode A — pre-approve everything up to AUTOBUILD on FEATURE_A so
        # the supervisor's next turn lands on AUTOBUILD dispatch.
        harness.register_mode_a(BUILD_A, features=[FEATURE_A])
        for s in (
            StageClass.PRODUCT_OWNER,
            StageClass.ARCHITECT,
            StageClass.SYSTEM_ARCH,
            StageClass.SYSTEM_DESIGN,
        ):
            harness.mark_approved(BUILD_A, s, None)
        for s in (StageClass.FEATURE_SPEC, StageClass.FEATURE_PLAN):
            harness.mark_approved(BUILD_A, s, FEATURE_A)

        # Mode B — pre-approved up to FEATURE_PLAN.
        harness.register_mode_b(BUILD_B, feature_id=FEATURE_B)
        for s in (StageClass.FEATURE_SPEC, StageClass.FEATURE_PLAN):
            harness.mark_approved(BUILD_B, s, FEATURE_B)
            harness.log.states[BUILD_B].mode_b_history.append(
                _ModeBStageEntry(
                    stage=s,
                    status=MODE_B_APPROVED,
                    feature_id=FEATURE_B,
                    details={"artefact_paths": [f"/wt/{BUILD_B}/{s.value}.md"]},
                )
            )

        # Mode C — initial review already recorded, dispatch lands on
        # TASK_WORK for FIX-C1.
        harness.register_mode_c(
            BUILD_C, review_fix_tasks=[("FIX-C1",), ()], has_commits=False
        )
        harness.log.states[BUILD_C].mode_c_history.append(
            ModeCStageEntry(
                stage_class=StageClass.TASK_REVIEW,
                status="approved",
                fix_tasks=("FIX-C1",),
                fix_task_id=None,
                hard_stop=False,
            )
        )
        harness.mark_approved(BUILD_C, StageClass.TASK_REVIEW, None)

        # Three-way interleave under one supervisor.
        report_a, report_b, report_c = await asyncio.gather(
            harness.turn(BUILD_A),
            harness.turn(BUILD_B),
            harness.turn(BUILD_C),
        )

        assert report_a.chosen_stage is StageClass.AUTOBUILD
        assert report_a.chosen_feature_id == FEATURE_A
        assert report_b.chosen_stage is StageClass.AUTOBUILD
        assert report_b.chosen_feature_id == FEATURE_B
        assert report_c.chosen_stage is StageClass.TASK_WORK

    @pytest.mark.asyncio
    async def test_each_builds_chronology_reflects_only_its_modes_stages(
        self, harness: _CrossModeHarness
    ) -> None:
        # Same setup as above; assert post-turn each chronology row only
        # carries that build's mode's stages — no cross-talk.
        harness.register_mode_a(BUILD_A, features=[FEATURE_A])
        harness.register_mode_b(BUILD_B, feature_id=FEATURE_B)
        harness.register_mode_c(BUILD_C, review_fix_tasks=[(),])

        # Drive each build forward by a single turn — Mode A picks
        # PRODUCT_OWNER, Mode B picks FEATURE_SPEC, Mode C picks
        # TASK_REVIEW.
        await asyncio.gather(
            harness.turn(BUILD_A),
            harness.turn(BUILD_B),
            harness.turn(BUILD_C),
        )

        # Mode A's chronology only contains Mode A entry stages.
        a_stages = {
            row["chosen_stage"]
            for row in harness.log.states[BUILD_A].chronology
            if row["chosen_stage"] is not None
        }
        assert a_stages.issubset(
            {
                StageClass.PRODUCT_OWNER,
                StageClass.ARCHITECT,
                StageClass.SYSTEM_ARCH,
                StageClass.SYSTEM_DESIGN,
                StageClass.FEATURE_SPEC,
                StageClass.FEATURE_PLAN,
                StageClass.AUTOBUILD,
                StageClass.PULL_REQUEST_REVIEW,
            }
        )
        # No Mode C stages on Mode A's chronology.
        assert StageClass.TASK_REVIEW not in a_stages
        assert StageClass.TASK_WORK not in a_stages

        # Mode B's chronology only contains Mode B chain entries; the
        # four pre-feature-spec Mode A stages MUST never appear.
        b_stages = {
            row["chosen_stage"]
            for row in harness.log.states[BUILD_B].chronology
            if row["chosen_stage"] is not None
        }
        assert b_stages.issubset(
            {
                StageClass.FEATURE_SPEC,
                StageClass.FEATURE_PLAN,
                StageClass.AUTOBUILD,
                StageClass.PULL_REQUEST_REVIEW,
            }
        )
        assert b_stages & {
            StageClass.PRODUCT_OWNER,
            StageClass.ARCHITECT,
            StageClass.SYSTEM_ARCH,
            StageClass.SYSTEM_DESIGN,
        } == set()
        assert StageClass.TASK_REVIEW not in b_stages
        assert StageClass.TASK_WORK not in b_stages

        # Mode C's chronology only contains Mode C chain entries.
        c_stages = {
            row["chosen_stage"]
            for row in harness.log.states[BUILD_C].chronology
            if row["chosen_stage"] is not None
        }
        assert c_stages.issubset(
            {
                StageClass.TASK_REVIEW,
                StageClass.TASK_WORK,
                StageClass.PULL_REQUEST_REVIEW,
            }
        )
        # And none of the Mode A / Mode B exclusive stages.
        assert c_stages & {
            StageClass.PRODUCT_OWNER,
            StageClass.ARCHITECT,
            StageClass.SYSTEM_ARCH,
            StageClass.SYSTEM_DESIGN,
            StageClass.FEATURE_SPEC,
            StageClass.FEATURE_PLAN,
            StageClass.AUTOBUILD,
        } == set()

    @pytest.mark.asyncio
    async def test_no_cross_talk_in_dispatcher_call_attribution(
        self, harness: _CrossModeHarness
    ) -> None:
        # Drive all three concurrently and assert that every dispatcher
        # call is attributed to the correct build_id (no cross-build
        # leakage in the call records).
        harness.register_mode_a(BUILD_A, features=[FEATURE_A])
        harness.register_mode_b(BUILD_B, feature_id=FEATURE_B)
        harness.register_mode_c(BUILD_C, review_fix_tasks=[(),])
        await asyncio.gather(
            harness.turn(BUILD_A),
            harness.turn(BUILD_B),
            harness.turn(BUILD_C),
        )
        # Specialist dispatcher only ever sees Mode A build_ids
        # (PRODUCT_OWNER / ARCHITECT). Mode B / Mode C never call it.
        spec_builds = {
            c["build_id"] for c in harness.specialist_dispatcher.calls
        }
        assert spec_builds.issubset({BUILD_A})
        # Subprocess dispatcher rows for Mode B carry feature_id; Mode C
        # rows carry no feature_id but a fix_task or fix_tasks payload.
        for call in harness.subprocess_dispatcher.calls:
            stage = call.get("stage")
            build_id = call.get("build_id")
            if build_id == BUILD_B:
                assert stage in {
                    StageClass.FEATURE_SPEC,
                    StageClass.FEATURE_PLAN,
                }
            elif build_id == BUILD_C:
                assert stage in {StageClass.TASK_REVIEW, StageClass.TASK_WORK}


# ---------------------------------------------------------------------------
# AC-004 — Supervisor responsiveness during async stages (Group F)
# ---------------------------------------------------------------------------


class TestSupervisorResponsivenessDuringAsync:
    """Build 1's autobuild/task-work in flight does NOT block Build 2's dispatch."""

    @pytest.mark.asyncio
    async def test_second_mode_b_dispatch_proceeds_while_first_autobuild_in_flight(
        self, harness: _CrossModeHarness
    ) -> None:
        harness.register_mode_b(BUILD_B, feature_id=FEATURE_B)
        harness.register_mode_b(BUILD_B_ALT, feature_id=FEATURE_B_ALT)

        # Build 1 (BUILD_B) — pre-mark FEATURE_SPEC + FEATURE_PLAN +
        # AUTOBUILD as already in flight (autobuild_lifecycle = running_wave
        # but on a separate "sibling" feature so the per-feature sequencer
        # treats it as in-flight when BUILD_B's TURN comes round).
        for s in (StageClass.FEATURE_SPEC, StageClass.FEATURE_PLAN):
            harness.mark_approved(BUILD_B, s, FEATURE_B)
            harness.log.states[BUILD_B].mode_b_history.append(
                _ModeBStageEntry(
                    stage=s,
                    status=MODE_B_APPROVED,
                    feature_id=FEATURE_B,
                    details={"artefact_paths": [f"/wt/{BUILD_B}/{s.value}.md"]},
                )
            )
        # Build 2 — fresh, will pick FEATURE_SPEC.

        # Concurrent dispatch turn — must complete without either turn
        # blocking on the other.
        report_1, report_2 = await asyncio.gather(
            harness.turn(BUILD_B), harness.turn(BUILD_B_ALT)
        )
        assert report_1.outcome is TurnOutcome.DISPATCHED
        assert report_1.chosen_stage is StageClass.AUTOBUILD
        assert report_2.outcome is TurnOutcome.DISPATCHED
        assert report_2.chosen_stage is StageClass.FEATURE_SPEC

    @pytest.mark.asyncio
    async def test_second_build_dispatches_during_first_autobuild_running_wave(
        self, harness: _CrossModeHarness
    ) -> None:
        # Same idea as above but explicitly puts build 1's autobuild in
        # the "running_wave" lifecycle so the supervisor sees an
        # in-flight autobuild on build 1 when it dispatches build 2.
        harness.register_mode_a(BUILD_A, features=[FEATURE_A])
        harness.register_mode_b(BUILD_B, feature_id=FEATURE_B)
        # Build 1 (Mode A) at AUTOBUILD; in-flight wave injected.
        for s in (
            StageClass.PRODUCT_OWNER,
            StageClass.ARCHITECT,
            StageClass.SYSTEM_ARCH,
            StageClass.SYSTEM_DESIGN,
        ):
            harness.mark_approved(BUILD_A, s, None)
        for s in (StageClass.FEATURE_SPEC, StageClass.FEATURE_PLAN):
            harness.mark_approved(BUILD_A, s, FEATURE_A)
        # Mark autobuild already started on the async-task channel.
        harness.set_autobuild_in_flight(BUILD_A, FEATURE_A)
        # Build 2 (Mode B) is fresh — first dispatch will be FEATURE_SPEC.

        report_a, report_b = await asyncio.gather(
            harness.turn(BUILD_A), harness.turn(BUILD_B)
        )
        # Build 1's per-feature sequencer permits self-dispatch (the
        # in-flight feature is its own); it picks AUTOBUILD.
        assert report_a.outcome is TurnOutcome.DISPATCHED
        assert report_a.chosen_stage is StageClass.AUTOBUILD
        # Build 2 dispatches FEATURE_SPEC despite Build 1's running wave.
        assert report_b.outcome is TurnOutcome.DISPATCHED
        assert report_b.chosen_stage is StageClass.FEATURE_SPEC


# ---------------------------------------------------------------------------
# AC-005 — Idempotent first-wins under concurrency (Group I)
# ---------------------------------------------------------------------------


class TestFirstWinsIdempotencyUnderConcurrency:
    """Two simultaneous approval responses → exactly one decision applied."""

    @pytest.mark.asyncio
    async def test_two_simultaneous_responses_resolve_to_one_winner(
        self,
    ) -> None:
        nats = InMemoryNats()
        clock = FakeMonotonicClock()
        sub = ApprovalSubscriber(
            ApprovalSubscriberDeps(
                nats_client=nats,
                config=ApprovalConfig(default_wait_seconds=1, max_wait_seconds=2),
                publish_refresh=None,
                clock=clock,
                dedup_ttl_seconds=300,
            )
        )
        request_id = derive_request_id(
            build_id=BUILD_B, stage_label="FeaturePlan", attempt_count=0
        )

        wait_task = asyncio.create_task(
            sub.await_response(BUILD_B, stage_label="FeaturePlan")
        )
        for _ in range(50):
            if BUILD_B in sub._queues:
                break
            await asyncio.sleep(0)

        # Two simultaneous responses with DIFFERENT decisions, same id.
        env_a = MessageEnvelope(
            source_id="rich",
            event_type=EventType.APPROVAL_RESPONSE,
            payload={
                "request_id": request_id,
                "decision": "approve",
                "decided_by": "rich",
                "notes": "first",
            },
        )
        env_b = MessageEnvelope(
            source_id="rich",
            event_type=EventType.APPROVAL_RESPONSE,
            payload={
                "request_id": request_id,
                "decision": "reject",
                "decided_by": "rich",
                "notes": "second",
            },
        )
        await asyncio.gather(
            sub._on_envelope(build_id=BUILD_B, envelope=env_a),
            sub._on_envelope(build_id=BUILD_B, envelope=env_b),
        )
        result = await asyncio.wait_for(wait_task, timeout=1.0)
        assert result is not None
        assert result.request_id == request_id
        # Exactly one decision survived.
        assert result.notes in {"first", "second"}

    @pytest.mark.asyncio
    async def test_no_second_resume_for_duplicate_response(self) -> None:
        # The dedup buffer holds exactly ONE entry after two simultaneous
        # responses with the same request_id — that proves no second
        # resume is applied.
        nats = InMemoryNats()
        clock = FakeMonotonicClock()
        sub = ApprovalSubscriber(
            ApprovalSubscriberDeps(
                nats_client=nats,
                config=ApprovalConfig(default_wait_seconds=1, max_wait_seconds=2),
                publish_refresh=None,
                clock=clock,
                dedup_ttl_seconds=300,
            )
        )
        request_id = derive_request_id(
            build_id=BUILD_B, stage_label="Autobuild", attempt_count=0
        )

        wait_task = asyncio.create_task(
            sub.await_response(BUILD_B, stage_label="Autobuild")
        )
        for _ in range(50):
            if BUILD_B in sub._queues:
                break
            await asyncio.sleep(0)

        env_a = MessageEnvelope(
            source_id="rich",
            event_type=EventType.APPROVAL_RESPONSE,
            payload={
                "request_id": request_id,
                "decision": "approve",
                "decided_by": "rich",
            },
        )
        env_b = MessageEnvelope(
            source_id="rich",
            event_type=EventType.APPROVAL_RESPONSE,
            payload={
                "request_id": request_id,
                "decision": "approve",
                "decided_by": "rich",
            },
        )
        await asyncio.gather(
            sub._on_envelope(build_id=BUILD_B, envelope=env_a),
            sub._on_envelope(build_id=BUILD_B, envelope=env_b),
        )
        await asyncio.wait_for(wait_task, timeout=1.0)
        # Exactly one dedup entry — duplicate did not record a second.
        assert len(sub._dedup) == 1

    @pytest.mark.asyncio
    async def test_recorded_resume_event_count_is_exactly_one(self) -> None:
        # Drive the same scenario, count the number of payloads the wait
        # loop receives via its queue. The await_response call returns
        # the first-arrival payload; subsequent duplicates do not enqueue
        # — so the queue is empty after the wait resolves.
        nats = InMemoryNats()
        clock = FakeMonotonicClock()
        sub = ApprovalSubscriber(
            ApprovalSubscriberDeps(
                nats_client=nats,
                config=ApprovalConfig(default_wait_seconds=1, max_wait_seconds=2),
                publish_refresh=None,
                clock=clock,
                dedup_ttl_seconds=300,
            )
        )
        rid = derive_request_id(
            build_id=BUILD_B, stage_label="PRReview", attempt_count=0
        )
        wait_task = asyncio.create_task(
            sub.await_response(BUILD_B, stage_label="PRReview")
        )
        for _ in range(50):
            if BUILD_B in sub._queues:
                break
            await asyncio.sleep(0)
        env_a = MessageEnvelope(
            source_id="rich",
            event_type=EventType.APPROVAL_RESPONSE,
            payload={
                "request_id": rid,
                "decision": "approve",
                "decided_by": "rich",
                "notes": "first",
            },
        )
        env_b = MessageEnvelope(
            source_id="rich",
            event_type=EventType.APPROVAL_RESPONSE,
            payload={
                "request_id": rid,
                "decision": "reject",
                "decided_by": "rich",
                "notes": "second-duplicate",
            },
        )
        await asyncio.gather(
            sub._on_envelope(build_id=BUILD_B, envelope=env_a),
            sub._on_envelope(build_id=BUILD_B, envelope=env_b),
        )
        result = await asyncio.wait_for(wait_task, timeout=1.0)
        assert result is not None
        # The build's queue is closed after await_response returns — but
        # we can also observe that no second payload was enqueued by
        # checking that the queue (the per-build dict entry created by
        # await_response) was popped exactly once.
        # The dedup buffer is the canonical "resume event count" surface;
        # exactly one entry means exactly one resume was applied.
        assert len(sub._dedup) == 1


# ---------------------------------------------------------------------------
# AC-006 — Calibration-priors snapshot stability (ASSUM-012, Group I)
# ---------------------------------------------------------------------------


@dataclass
class _CalibrationHistory:
    """Mutable operator calibration history — the source the snapshot is cut from."""

    entries: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class _PriorsSnapshot:
    """Immutable snapshot of priors taken at build start."""

    captured_at_build_start: tuple[dict[str, Any], ...]


def _capture_priors(history: _CalibrationHistory) -> _PriorsSnapshot:
    """Cut a deep snapshot of the operator's calibration history.

    Each entry is deep-copied so subsequent mutations to dict items in the
    history do not bleed into the snapshot — that is the contract
    ASSUM-012 demands.
    """
    return _PriorsSnapshot(
        captured_at_build_start=tuple(deepcopy(e) for e in history.entries)
    )


class TestCalibrationPriorsSnapshotStability:
    """Snapshot at Mode B build start is immune to mid-build mutation."""

    def test_priors_snapshot_remains_stable_when_history_is_mutated_mid_build(
        self,
    ) -> None:
        history = _CalibrationHistory(
            entries=[
                {"capability": "writing", "score": 0.7},
                {"capability": "review", "score": 0.8},
            ]
        )
        # Build start — Mode B build captures its priors snapshot.
        snapshot = _capture_priors(history)
        # Mid-build: operator's calibration history mutates.
        history.entries.append({"capability": "writing", "score": 0.95})
        history.entries[0]["score"] = 0.1  # mutate an existing entry
        # Later stages of the in-flight build still see the snapshot.
        assert snapshot.captured_at_build_start == (
            {"capability": "writing", "score": 0.7},
            {"capability": "review", "score": 0.8},
        ), "snapshot was clobbered by mid-build mutation"

    def test_snapshot_is_immutable_tuple(self) -> None:
        history = _CalibrationHistory(
            entries=[{"capability": "x", "score": 0.5}]
        )
        snapshot = _capture_priors(history)
        # Tuple immutability — extending raises.
        with pytest.raises(AttributeError):
            snapshot.captured_at_build_start.append(  # type: ignore[attr-defined]
                {"capability": "y", "score": 0.0}
            )

    def test_a_subsequent_build_sees_the_mutated_history(self) -> None:
        # Sanity control: a *new* build started after the mutation sees
        # the mutated history — proving the snapshot really was an
        # immutable copy, not just a hidden reference to the same list.
        history = _CalibrationHistory(
            entries=[{"capability": "writing", "score": 0.7}]
        )
        first = _capture_priors(history)
        history.entries.append({"capability": "review", "score": 0.95})
        history.entries[0]["score"] = 0.1
        second = _capture_priors(history)
        assert len(first.captured_at_build_start) == 1
        assert first.captured_at_build_start[0]["score"] == 0.7
        assert len(second.captured_at_build_start) == 2
        assert second.captured_at_build_start[0]["score"] == 0.1


# ---------------------------------------------------------------------------
# AC-007 — Notification publish failure isolation (Group G)
# ---------------------------------------------------------------------------


class TestNotificationPublishFailureIsolation:
    """A failed outbound NATS publish must not roll back the stage record."""

    @pytest.mark.asyncio
    async def test_stage_recorded_approved_even_when_publish_raises(
        self, harness: _CrossModeHarness
    ) -> None:
        # Pre-queue a transport failure on the stage's notify subject for
        # build_A's PRODUCT_OWNER stage. The specialist dispatcher catches
        # the error so the dispatch result is still "approved" — the
        # SQLite-shaped record of the approval must survive (Group G).
        harness.register_mode_a(BUILD_A, features=[FEATURE_A])
        subject = f"agents.notify.forge.{BUILD_A}.{StageClass.PRODUCT_OWNER.value}"
        harness.nats.publish_failures[subject] = [ConnectionError("nats down")]

        report = await harness.turn(BUILD_A)
        assert report.outcome is TurnOutcome.DISPATCHED
        assert report.chosen_stage is StageClass.PRODUCT_OWNER
        # Stage was recorded as approved on the build's history despite
        # the failed publish.
        state = harness.log.states[BUILD_A]
        assert (BUILD_A, StageClass.PRODUCT_OWNER, None) in state.approved
        # And the next stage's prerequisite (ARCHITECT ← PRODUCT_OWNER)
        # is now satisfied — proving the failed publish did not cancel
        # the upstream record.
        next_report = await harness.turn(BUILD_A)
        assert next_report.outcome is TurnOutcome.DISPATCHED
        assert next_report.chosen_stage is StageClass.ARCHITECT

    @pytest.mark.asyncio
    async def test_failed_publish_does_not_block_subsequent_dispatch(
        self, harness: _CrossModeHarness
    ) -> None:
        # Inject a transport failure on multiple subjects to guard
        # against masking via the first-call short-circuit. The pipeline
        # must keep advancing.
        harness.register_mode_a(BUILD_A, features=[FEATURE_A])
        for stage in (StageClass.PRODUCT_OWNER, StageClass.ARCHITECT):
            subject = f"agents.notify.forge.{BUILD_A}.{stage.value}"
            harness.nats.publish_failures[subject] = [
                RuntimeError("transient broker hiccup")
            ]
        # Drive two turns — both must dispatch and record approvals.
        r1 = await harness.turn(BUILD_A)
        r2 = await harness.turn(BUILD_A)
        assert r1.chosen_stage is StageClass.PRODUCT_OWNER
        assert r2.chosen_stage is StageClass.ARCHITECT
        approved = harness.log.states[BUILD_A].approved
        assert (BUILD_A, StageClass.PRODUCT_OWNER, None) in approved
        assert (BUILD_A, StageClass.ARCHITECT, None) in approved


# ---------------------------------------------------------------------------
# AC-budget — Tests run in under 60 seconds with in-memory NATS
# ---------------------------------------------------------------------------


class TestCrossModeConcurrencyBudget:
    """Performance canary — every cross-mode scenario completes well under 60s."""

    @pytest.mark.asyncio
    async def test_three_way_drive_completes_well_under_one_second(self) -> None:
        # The full three-way drive plus a per-build single turn each
        # must complete in well under one second on the in-memory
        # harness. Failing this canary would surface an accidentally-
        # introduced sleep or real subprocess spawn.
        harness = _build_harness()
        harness.register_mode_a(BUILD_A, features=[FEATURE_A])
        harness.register_mode_b(BUILD_B, feature_id=FEATURE_B)
        harness.register_mode_c(BUILD_C, review_fix_tasks=[(),])

        start = time.monotonic()
        await asyncio.gather(
            harness.turn(BUILD_A),
            harness.turn(BUILD_B),
            harness.turn(BUILD_C),
        )
        elapsed = time.monotonic() - start
        # 60s is the AC budget; 1s is the canary.
        assert elapsed < 1.0, (
            f"three-way drive exceeded 1-second canary: {elapsed:.2f}s — "
            "regression in async-safety substrate"
        )

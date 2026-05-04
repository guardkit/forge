"""Mode B smoke E2E (TASK-MBC8-010, FEAT-FORGE-008).

End-to-end smoke test for the Mode B pipeline: drives a single Mode B
build through ``feature-spec → feature-plan → autobuild`` and pauses at
the constitutional ``pull-request-review`` gate awaiting mandatory
human approval.

Mode B's chain (FEAT-FORGE-008 ASSUM-001) is a strict suffix of the
Mode A chain starting at ``feature-spec``. The four pre-feature-spec
Mode A stages (``product-owner``, ``architect``, ``system-arch``,
``system-design``) MUST never appear on a Mode B build's stage history
(ASSUM-013 / ASSUM-014). This module is the canonical regression for
that invariant and the accompanying happy-path sequence.

Acceptance criteria coverage map (TASK-MBC8-010):

* Stage-history invariants — :class:`TestModeBChainShape`.
* PR-review terminal pause + recorded URL — :class:`TestModeBPullRequestPause`.
* Forward-propagation invariants — :class:`TestModeBForwardPropagation`.
* Async dispatch + supervisor responsiveness — :class:`TestModeBAsyncDispatch`.
* CLI steering (skip / cancel) — :class:`TestModeBCliSteering`.
* Wall-clock guard — :class:`TestModeBSmokeBudget`.

References
----------

* TASK-MBC8-010 — this task brief.
* TASK-MAG7-012 — Mode A counterpart (``test_mode_a_smoke.py``).
* TASK-MBC8-003 — :class:`ModeBChainPlanner`.
* TASK-MBC8-005 — :class:`ForwardContextBuilder` Mode B contract.
* TASK-MBC8-008 — supervisor mode-aware dispatch.
* TASK-MBC8-009 — async autobuild dispatch.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest

from forge.lifecycle.modes import BuildMode
from forge.pipeline.cli_steering import (
    AsyncTaskCanceller,
    AsyncTaskUpdater,
    BuildCanceller,
    BuildLifecycle,
    BuildResumer,
    BuildSnapshot,
    BuildSnapshotReader,
    CancelStatus,
    CliSteeringHandler,
    PauseRejectResolver,
    SkipStatus,
    StageSkipRecorder,
)
from forge.pipeline.constitutional_guard import ConstitutionalGuard
from forge.pipeline.forward_context_builder import (
    ApprovedStageEntry,
    ContextEntry,
    ForwardContextBuilder,
    StageLogReader as ForwardStageLogReader,
    WorktreeAllowlist,
)
from forge.pipeline.mode_b_planner import (
    APPROVED as MODE_B_APPROVED,
    ModeBChainPlanner,
    StageEntry as ModeBStageEntry,
)
from forge.pipeline.per_feature_sequencer import PerFeatureLoopSequencer
from forge.pipeline.stage_ordering_guard import StageOrderingGuard
from forge.pipeline.stage_taxonomy import (
    CONSTITUTIONAL_STAGES,
    PER_FEATURE_STAGES,
    StageClass,
)
from forge.pipeline.supervisor import (
    BuildState,
    Supervisor,
    TurnOutcome,
    TurnReport,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


SMOKE_BUILD_ID: str = "build-FEAT-B1-20260427120000"
SMOKE_FEATURE_ID: str = "FEAT-B1"

#: Canonical Mode B chain (FEAT-FORGE-008 ASSUM-001).
MODE_B_CANONICAL_ORDER: tuple[StageClass, ...] = (
    StageClass.FEATURE_SPEC,
    StageClass.FEATURE_PLAN,
    StageClass.AUTOBUILD,
    StageClass.PULL_REQUEST_REVIEW,
)

#: Mode A pre-feature-spec stages — MUST NEVER appear in a Mode B build.
MODE_B_FORBIDDEN_HISTORY_STAGES: frozenset[StageClass] = frozenset(
    {
        StageClass.PRODUCT_OWNER,
        StageClass.ARCHITECT,
        StageClass.SYSTEM_ARCH,
        StageClass.SYSTEM_DESIGN,
    }
)

#: Canned artefact paths threaded through the Mode B chain. Inside
#: ``/fake/worktree/...`` so the worktree allowlist accepts them.
SPEC_ARTEFACT_PATHS: tuple[str, ...] = (
    "/fake/worktree/spec/feature-b1.md",
)
PLAN_ARTEFACT_PATHS: tuple[str, ...] = (
    "/fake/worktree/plan/feature-b1.md",
)
AUTOBUILD_BRANCH_SUMMARY: str = (
    "branch=auto/feat-b1\nshort: 3 commits, +180/-12, 2 files"
)


# ---------------------------------------------------------------------------
# In-memory composite store — implements every Protocol the harness needs
# ---------------------------------------------------------------------------


@dataclass
class FakeStageLogStore:
    """Composite in-memory backing for every supervisor / forward-builder reader.

    Implements the read shapes consumed by:

    * :class:`StageOrderingGuard` (``is_approved`` + ``feature_catalogue``).
    * :class:`PerFeatureLoopSequencer` (``is_autobuild_approved``).
    * :class:`Supervisor.turn_recorder` (``record_turn`` chronological log).
    * :class:`ModeBChainPlanner` (``get_mode_b_history``).
    * :class:`ForwardContextBuilder` (``get_approved_stage_entry`` /
      ``get_all_approved_stage_entries``).
    """

    approved: set[tuple[str, StageClass, str | None]] = field(default_factory=set)
    catalogues: dict[str, list[str]] = field(default_factory=dict)
    chronology: list[dict[str, Any]] = field(default_factory=list)
    mode_b_history: dict[str, list[ModeBStageEntry]] = field(default_factory=dict)
    approved_entries: dict[
        tuple[str, StageClass, str | None], ApprovedStageEntry
    ] = field(default_factory=dict)

    # ---- Ordering-guard reader Protocol -------------------------------

    def is_approved(
        self,
        build_id: str,
        stage: StageClass,
        feature_id: str | None = None,
    ) -> bool:
        return (build_id, stage, feature_id) in self.approved

    def feature_catalogue(self, build_id: str) -> list[str]:
        return list(self.catalogues.get(build_id, []))

    # ---- Per-feature-sequencer reader Protocol ------------------------

    def is_autobuild_approved(self, build_id: str, feature_id: str) -> bool:
        return (build_id, StageClass.AUTOBUILD, feature_id) in self.approved

    # ---- Turn-recorder Protocol ---------------------------------------

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
                "gate_mode": None,
                "pr_url": None,
            }
        )

    # ---- ModeBHistoryReader Protocol ----------------------------------

    def get_mode_b_history(self, build_id: str) -> Sequence[ModeBStageEntry]:
        return list(self.mode_b_history.get(build_id, []))

    # ---- ForwardContextBuilder StageLogReader Protocol ----------------

    def get_approved_stage_entry(
        self,
        build_id: str,
        stage: StageClass,
        feature_id: str | None = None,
    ) -> ApprovedStageEntry | None:
        return self.approved_entries.get((build_id, stage, feature_id))

    def get_all_approved_stage_entries(
        self,
        build_id: str,
        stage: StageClass,
        feature_id: str | None = None,
    ) -> Sequence[ApprovedStageEntry]:
        entry = self.get_approved_stage_entry(build_id, stage, feature_id)
        return [entry] if entry is not None else []

    # ---- Driver-side mutators ----------------------------------------

    def mark_approved(
        self,
        *,
        build_id: str,
        stage: StageClass,
        feature_id: str | None = None,
        artefact_paths: tuple[str, ...] = (),
        artefact_text: str | None = None,
    ) -> None:
        self.approved.add((build_id, stage, feature_id))
        self.approved_entries[(build_id, stage, feature_id)] = ApprovedStageEntry(
            gate_decision="approved",
            artefact_paths=artefact_paths,
            artefact_text=artefact_text,
        )

    def append_history_entry(
        self,
        build_id: str,
        entry: ModeBStageEntry,
    ) -> None:
        self.mode_b_history.setdefault(build_id, []).append(entry)

    def set_catalogue(self, build_id: str, feature_ids: list[str]) -> None:
        self.catalogues[build_id] = list(feature_ids)


@dataclass
class FakeModeBStageEntry:
    """Dataclass that satisfies the Mode B ``StageEntry`` Protocol."""

    stage: StageClass
    status: str
    feature_id: str | None = SMOKE_FEATURE_ID
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class FakeStateMachineReader:
    """In-memory state-machine reader; defaults every build to RUNNING."""

    states: dict[str, BuildState] = field(default_factory=dict)

    def set_state(self, build_id: str, state: BuildState) -> None:
        self.states[build_id] = state

    def get_build_state(self, build_id: str) -> BuildState:
        return self.states.get(build_id, BuildState.RUNNING)


@dataclass
class FakeBuildModeReader:
    """Reader returning the build's :class:`BuildMode`. Defaults to MODE_B."""

    modes: dict[str, BuildMode] = field(default_factory=dict)

    def get_build_mode(self, build_id: str) -> BuildMode:
        return self.modes.get(build_id, BuildMode.MODE_B)


# ---------------------------------------------------------------------------
# Async-task channel double — exposes wave/task indices during autobuild
# ---------------------------------------------------------------------------


@dataclass
class FakeAutobuildState:
    """In-memory ``AutobuildState`` shape (DDR-006).

    The smoke harness uses ``wave_index`` and ``task_index`` to satisfy
    the AC: "async_tasks state channel exposes wave/task indices during
    the run". Production reads these off ``AsyncTask.state`` payloads;
    the harness exposes them as plain attributes.
    """

    feature_id: str
    lifecycle: str = "running_wave"
    wave_index: int = 1
    task_index: int = 1


@dataclass
class FakeAsyncTaskReader:
    """``async_tasks`` channel double with a mutable per-build state list."""

    states_by_build: dict[str, list[FakeAutobuildState]] = field(default_factory=dict)

    def list_autobuild_states(
        self, build_id: str
    ) -> Iterable[FakeAutobuildState]:
        return list(self.states_by_build.get(build_id, []))

    def set_in_flight(
        self,
        build_id: str,
        feature_id: str,
        *,
        wave_index: int = 1,
        task_index: int = 1,
        lifecycle: str = "running_wave",
    ) -> None:
        self.states_by_build[build_id] = [
            FakeAutobuildState(
                feature_id=feature_id,
                lifecycle=lifecycle,
                wave_index=wave_index,
                task_index=task_index,
            )
        ]

    def mark_completed(self, build_id: str, feature_id: str) -> None:
        states = self.states_by_build.get(build_id, [])
        for s in states:
            if s.feature_id == feature_id:
                s.lifecycle = "completed"


# ---------------------------------------------------------------------------
# Worktree allowlist — every path under /fake/worktree is allowed
# ---------------------------------------------------------------------------


@dataclass
class FakeWorktreeAllowlist:
    """Allowlist accepting every path under ``/fake/worktree/``."""

    root: str = "/fake/worktree/"

    def is_allowed(self, build_id: str, path: str) -> bool:  # noqa: ARG002
        return path.startswith(self.root)


# ---------------------------------------------------------------------------
# Reasoning-model double — never consulted in Mode B but the supervisor's
# dataclass requires the field. Returns a sentinel so any accidental Mode A
# fallback would surface loudly.
# ---------------------------------------------------------------------------


@dataclass
class UnusedReasoningModel:
    """Reasoning-model port that should never be invoked in Mode B."""

    calls: int = 0

    def choose_dispatch(
        self,
        *,
        build_id: str,  # noqa: ARG002
        build_state: BuildState,  # noqa: ARG002
        permitted_stages: frozenset[StageClass],  # noqa: ARG002
        stage_hints: Mapping[StageClass, str],  # noqa: ARG002
        feature_catalogue: tuple[str, ...],  # noqa: ARG002
    ) -> None:
        self.calls += 1
        return None


# ---------------------------------------------------------------------------
# Dispatcher fakes — record calls + return canned outcomes
# ---------------------------------------------------------------------------


@dataclass
class FakeSubprocessDispatcher:
    """Subprocess dispatcher that consults a forward-context builder.

    Captures the resolved forward-propagation context entries for each
    dispatch so tests can assert that ``feature-plan`` saw the
    ``feature-spec`` artefact paths and ``autobuild`` saw the
    ``feature-plan`` paths.
    """

    forward_builder: ForwardContextBuilder
    artefact_paths: dict[StageClass, tuple[str, ...]] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(
        self,
        *,
        stage: StageClass,
        build_id: str,
        feature_id: str | None = None,
        rationale: str = "",
    ) -> dict[str, Any]:
        # Forward-propagation is done by the dispatcher in production (it
        # consults the ForwardContextBuilder before spawning the
        # subprocess). The smoke captures the resolved entries here so
        # tests can assert the contract is honoured for Mode B.
        forward_context: list[ContextEntry] = self.forward_builder.build_for(
            stage=stage,
            build_id=build_id,
            feature_id=feature_id,
            mode=BuildMode.MODE_B,
        )
        self.calls.append(
            {
                "stage": stage,
                "build_id": build_id,
                "feature_id": feature_id,
                "rationale": rationale,
                "forward_context": forward_context,
            }
        )
        paths = self.artefact_paths.get(stage, ())
        return {
            "stage": stage,
            "status": "approved",
            "artefact_paths": list(paths),
            "rationale": f"canned subprocess artefact for {stage.value}",
        }


@dataclass
class FakeAutobuildAsyncDispatcher:
    """Async autobuild dispatcher — returns a synthetic completed handle.

    Mirrors the production ``dispatch_autobuild_async`` shape: awaits
    the launch and returns the handle immediately while the underlying
    AsyncSubAgent runs separately. The smoke harness simulates the
    in-flight window by writing to the async-task channel and then
    marking complete.

    TASK-FORGE-FRR-F010G: this fake's ``__call__`` is now ``async def``
    because :func:`dispatch_autobuild_async` (and hence the supervisor
    closure that wraps it) is async — the launch path awaits the
    deepagents middleware's ``StructuredTool.coroutine`` so the
    autobuild_runner registration can stay URL-less in-process.
    """

    async_task_reader: FakeAsyncTaskReader
    calls: list[dict[str, Any]] = field(default_factory=list)
    next_task_id: int = 1000

    async def __call__(
        self,
        *,
        build_id: str,
        feature_id: str,
        rationale: str = "",
    ) -> dict[str, Any]:
        self.next_task_id += 1
        task_id = f"autobuild-task-{self.next_task_id}"
        self.calls.append(
            {
                "build_id": build_id,
                "feature_id": feature_id,
                "rationale": rationale,
                "task_id": task_id,
            }
        )
        # Expose wave/task indices on the async-tasks channel so a
        # status query ``next_turn`` during the run sees the in-flight
        # state. Mode B is single-feature so wave_index=1, task_index=1.
        self.async_task_reader.set_in_flight(
            build_id=build_id,
            feature_id=feature_id,
            wave_index=1,
            task_index=1,
            lifecycle="running_wave",
        )
        return {
            "build_id": build_id,
            "feature_id": feature_id,
            "status": "approved",
            "lifecycle": "completed",
            "task_id": task_id,
            "changed_files_count": 3,
        }


@dataclass
class FakePRReviewGate:
    """PR-review gate stub — submission is the build's terminal pause.

    Records every submit_decision call and returns a record carrying a
    ``mandatory_human`` gate mode plus a synthesised PR URL. The smoke
    driver lifts those fields onto the matching chronology row so the
    AC assertion ("PR URL recorded in stage_log", "mandatory_human gate
    mode") is local to the chronology log.
    """

    submissions: list[dict[str, Any]] = field(default_factory=list)
    pr_url_template: str = "https://github.com/example/forge/pull/{number}"
    _next_pr_number: int = 200

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
            "gate_mode": "MANDATORY_HUMAN_APPROVAL",
            "pr_url": pr_url,
        }
        self.submissions.append(record)
        return record


# ---------------------------------------------------------------------------
# Harness — composite pipeline + driver
# ---------------------------------------------------------------------------


@dataclass
class ModeBSmokePipeline:
    """Composite in-memory harness for the Mode B smoke + CLI-steering suites.

    Owns every fake collaborator the supervisor + ``CliSteeringHandler``
    consume plus a small driver loop (:meth:`drive_until_paused`) that
    runs supervisor turns until the build either reaches PR-review or
    pauses on a flag-for-review.
    """

    supervisor: Supervisor
    stage_log: FakeStageLogStore
    state_machine: FakeStateMachineReader
    mode_reader: FakeBuildModeReader
    subprocess_dispatcher: FakeSubprocessDispatcher
    autobuild_dispatcher: FakeAutobuildAsyncDispatcher
    pr_review_gate: FakePRReviewGate
    async_task_reader: FakeAsyncTaskReader
    forward_builder: ForwardContextBuilder
    reasoning_model: UnusedReasoningModel
    cli_handler_factory: "Any"
    build_id: str = SMOKE_BUILD_ID
    feature_id: str = SMOKE_FEATURE_ID

    # ------------------------------------------------------------------
    # Driver
    # ------------------------------------------------------------------

    async def drive_until_paused(
        self,
        *,
        build_id: str | None = None,
        feature_id: str | None = None,
        max_turns: int = 16,
    ) -> TurnReport:
        """Run ``supervisor.next_turn`` iteratively until paused / terminal.

        Args:
            build_id: Build identifier. Defaults to :data:`SMOKE_BUILD_ID`.
            feature_id: Single feature id used to populate the catalogue
                and stage entries. Defaults to :data:`SMOKE_FEATURE_ID`.
            max_turns: Hard cap to stop a runaway loop. The Mode B happy
                path completes in four turns; 16 leaves headroom for any
                future pause-resume regression to surface a useful chrono
                log.

        Returns:
            The final :class:`TurnReport`.

        Raises:
            RuntimeError: If ``max_turns`` is reached without a terminal
                or PR-review outcome.
        """
        bid = build_id or self.build_id
        fid = feature_id or self.feature_id
        last_report: TurnReport | None = None
        for _ in range(max_turns):
            report = await self.supervisor.next_turn(bid)
            last_report = report
            self._absorb(report=report, build_id=bid, feature_id=fid)
            if self._is_paused_or_terminal(report):
                return report
        raise RuntimeError(
            f"Mode B smoke harness exceeded max_turns={max_turns} without "
            f"reaching a paused/terminal state; chronology has "
            f"{len(self.stage_log.chronology)} rows; last_report={last_report!r}"
        )

    # ------------------------------------------------------------------
    # Internals — absorb dispatcher outcome between turns
    # ------------------------------------------------------------------

    def _absorb(
        self,
        *,
        report: TurnReport,
        build_id: str,
        feature_id: str,
    ) -> None:
        if report.outcome is not TurnOutcome.DISPATCHED:
            return
        stage = report.chosen_stage
        if stage is None:
            return

        # PULL_REQUEST_REVIEW → annotate the chronology row with the
        # gate's recorded PR URL and stop.
        if stage is StageClass.PULL_REQUEST_REVIEW:
            self._annotate_pr_terminal()
            return

        result = report.dispatch_result
        if not isinstance(result, dict):
            return
        status = result.get("status")
        if status != "approved":
            return

        if stage is StageClass.FEATURE_SPEC:
            self.stage_log.mark_approved(
                build_id=build_id,
                stage=StageClass.FEATURE_SPEC,
                feature_id=feature_id,
                artefact_paths=tuple(result.get("artefact_paths") or ()),
            )
            self.stage_log.append_history_entry(
                build_id,
                FakeModeBStageEntry(
                    stage=StageClass.FEATURE_SPEC,
                    status=MODE_B_APPROVED,
                    feature_id=feature_id,
                    details={
                        "artefact_paths": list(result.get("artefact_paths") or ()),
                    },
                ),
            )
            return

        if stage is StageClass.FEATURE_PLAN:
            self.stage_log.mark_approved(
                build_id=build_id,
                stage=StageClass.FEATURE_PLAN,
                feature_id=feature_id,
                artefact_paths=tuple(result.get("artefact_paths") or ()),
            )
            self.stage_log.append_history_entry(
                build_id,
                FakeModeBStageEntry(
                    stage=StageClass.FEATURE_PLAN,
                    status=MODE_B_APPROVED,
                    feature_id=feature_id,
                    details={
                        "artefact_paths": list(result.get("artefact_paths") or ()),
                    },
                ),
            )
            return

        if stage is StageClass.AUTOBUILD:
            # The async dispatcher already pushed an in-flight state to
            # the channel. Mark it completed so any subsequent
            # status-query turn sees the terminal lifecycle.
            self.async_task_reader.mark_completed(build_id, feature_id)
            self.stage_log.mark_approved(
                build_id=build_id,
                stage=StageClass.AUTOBUILD,
                feature_id=feature_id,
                artefact_text=AUTOBUILD_BRANCH_SUMMARY,
            )
            self.stage_log.append_history_entry(
                build_id,
                FakeModeBStageEntry(
                    stage=StageClass.AUTOBUILD,
                    status=MODE_B_APPROVED,
                    feature_id=feature_id,
                    details={
                        "diff_present": True,
                        "changed_files_count": int(
                            result.get("changed_files_count", 3),
                        ),
                    },
                ),
            )
            return

    def _annotate_pr_terminal(self) -> None:
        if not self.pr_review_gate.submissions:
            return
        latest = self.pr_review_gate.submissions[-1]
        if not self.stage_log.chronology:
            return
        row = self.stage_log.chronology[-1]
        row["gate_mode"] = latest["gate_mode"]
        row["pr_url"] = latest["pr_url"]

    @staticmethod
    def _is_paused_or_terminal(report: TurnReport) -> bool:
        if report.outcome is TurnOutcome.TERMINAL:
            return True
        if report.outcome in (
            TurnOutcome.WAITING,
            TurnOutcome.WAITING_PRIOR_AUTOBUILD,
            TurnOutcome.NO_OP,
            TurnOutcome.REFUSED_OUT_OF_BAND,
            TurnOutcome.REFUSED_CONSTITUTIONAL,
        ):
            return True
        if report.outcome is not TurnOutcome.DISPATCHED:
            return False
        if report.chosen_stage is StageClass.PULL_REQUEST_REVIEW:
            return True
        return False


# ---------------------------------------------------------------------------
# CLI-steering doubles — dataclass fakes for the seven Protocols
# ---------------------------------------------------------------------------


@dataclass
class FakeBuildSnapshotReader:
    """Snapshot reader; tests configure per-build snapshots directly."""

    snapshots: dict[str, BuildSnapshot] = field(default_factory=dict)

    def get_snapshot(self, build_id: str) -> BuildSnapshot:
        if build_id not in self.snapshots:
            raise KeyError(
                f"FakeBuildSnapshotReader: no snapshot configured for {build_id!r}"
            )
        return self.snapshots[build_id]


@dataclass
class RecordingPauseRejectResolver:
    calls: list[dict[str, Any]] = field(default_factory=list)

    def resolve_as_reject(
        self,
        build_id: str,
        stage: StageClass,
        feature_id: str | None,
        rationale: str,
    ) -> Any:
        record = {
            "build_id": build_id,
            "stage": stage,
            "feature_id": feature_id,
            "rationale": rationale,
        }
        self.calls.append(record)
        return record


@dataclass
class RecordingAsyncTaskCanceller:
    cancelled: list[str] = field(default_factory=list)

    def cancel_async_task(self, task_id: str) -> Any:
        self.cancelled.append(task_id)
        return {"task_id": task_id, "cancelled": True}


@dataclass
class RecordingAsyncTaskUpdater:
    updates: list[dict[str, Any]] = field(default_factory=list)

    def update_async_task(
        self,
        task_id: str,
        *,
        append_pending_directive: str,
    ) -> Any:
        self.updates.append(
            {"task_id": task_id, "append_pending_directive": append_pending_directive}
        )
        return None


@dataclass
class RecordingBuildCanceller:
    cancellations: list[dict[str, Any]] = field(default_factory=list)

    def mark_cancelled(self, build_id: str, rationale: str) -> Any:
        record = {"build_id": build_id, "rationale": rationale}
        self.cancellations.append(record)
        return record


@dataclass
class RecordingStageSkipRecorder:
    skipped: list[dict[str, Any]] = field(default_factory=list)
    refused: list[dict[str, Any]] = field(default_factory=list)

    def record_skipped(
        self, build_id: str, stage: StageClass, rationale: str
    ) -> Any:
        rec = {"build_id": build_id, "stage": stage, "rationale": rationale}
        self.skipped.append(rec)
        return rec

    def record_skip_refused(
        self, build_id: str, stage: StageClass, rationale: str
    ) -> Any:
        rec = {"build_id": build_id, "stage": stage, "rationale": rationale}
        self.refused.append(rec)
        return rec


@dataclass
class RecordingBuildResumer:
    resumes: list[dict[str, Any]] = field(default_factory=list)

    def resume_after_skip(
        self, build_id: str, skipped_stage: StageClass
    ) -> Any:
        rec = {"build_id": build_id, "skipped_stage": skipped_stage}
        self.resumes.append(rec)
        return rec


@dataclass
class CliSteeringHarness:
    """Bundle of CLI-steering Protocol doubles, ready to wire into a handler."""

    snapshot_reader: FakeBuildSnapshotReader = field(
        default_factory=FakeBuildSnapshotReader
    )
    pause_reject_resolver: RecordingPauseRejectResolver = field(
        default_factory=RecordingPauseRejectResolver
    )
    async_task_canceller: RecordingAsyncTaskCanceller = field(
        default_factory=RecordingAsyncTaskCanceller
    )
    async_task_updater: RecordingAsyncTaskUpdater = field(
        default_factory=RecordingAsyncTaskUpdater
    )
    build_canceller: RecordingBuildCanceller = field(
        default_factory=RecordingBuildCanceller
    )
    skip_recorder: RecordingStageSkipRecorder = field(
        default_factory=RecordingStageSkipRecorder
    )
    build_resumer: RecordingBuildResumer = field(
        default_factory=RecordingBuildResumer
    )

    def make_handler(self) -> CliSteeringHandler:
        return CliSteeringHandler(
            snapshot_reader=self.snapshot_reader,
            pause_reject_resolver=self.pause_reject_resolver,
            async_task_canceller=self.async_task_canceller,
            async_task_updater=self.async_task_updater,
            build_canceller=self.build_canceller,
            skip_recorder=self.skip_recorder,
            build_resumer=self.build_resumer,
            constitutional_guard=ConstitutionalGuard(),
        )


# ---------------------------------------------------------------------------
# Pipeline factory + fixture
# ---------------------------------------------------------------------------


def _build_pipeline() -> ModeBSmokePipeline:
    """Wire the supervisor + every fake collaborator for the Mode B smoke."""
    stage_log = FakeStageLogStore()
    state_machine = FakeStateMachineReader()
    mode_reader = FakeBuildModeReader()
    async_task_reader = FakeAsyncTaskReader()
    reasoning_model = UnusedReasoningModel()
    allowlist = FakeWorktreeAllowlist()
    forward_builder = ForwardContextBuilder(
        stage_log_reader=stage_log,
        worktree_allowlist=allowlist,
    )
    subprocess_dispatcher = FakeSubprocessDispatcher(
        forward_builder=forward_builder,
        artefact_paths={
            StageClass.FEATURE_SPEC: SPEC_ARTEFACT_PATHS,
            StageClass.FEATURE_PLAN: PLAN_ARTEFACT_PATHS,
        },
    )
    autobuild_dispatcher = FakeAutobuildAsyncDispatcher(
        async_task_reader=async_task_reader
    )
    pr_review_gate = FakePRReviewGate()

    # Pre-populate the catalogue + Mode B mode for the smoke build.
    stage_log.set_catalogue(SMOKE_BUILD_ID, [SMOKE_FEATURE_ID])
    mode_reader.modes[SMOKE_BUILD_ID] = BuildMode.MODE_B

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
        specialist_dispatcher=_unreachable_specialist_dispatcher,
        subprocess_dispatcher=subprocess_dispatcher,
        autobuild_dispatcher=autobuild_dispatcher,
        pr_review_gate=pr_review_gate,
        build_mode_reader=mode_reader,
        mode_b_planner=ModeBChainPlanner(),
        mode_b_history_reader=stage_log,
    )

    return ModeBSmokePipeline(
        supervisor=supervisor,
        stage_log=stage_log,
        state_machine=state_machine,
        mode_reader=mode_reader,
        subprocess_dispatcher=subprocess_dispatcher,
        autobuild_dispatcher=autobuild_dispatcher,
        pr_review_gate=pr_review_gate,
        async_task_reader=async_task_reader,
        forward_builder=forward_builder,
        reasoning_model=reasoning_model,
        cli_handler_factory=CliSteeringHarness,
    )


async def _unreachable_specialist_dispatcher(**kwargs: Any) -> dict[str, Any]:
    """Specialist dispatcher that fails loudly — Mode B forbids these stages."""
    raise AssertionError(
        "Specialist dispatcher invoked under Mode B (forbidden by ASSUM-014); "
        f"kwargs={kwargs!r}"
    )


@pytest.fixture
def mode_b_pipeline() -> ModeBSmokePipeline:
    """Composite Mode B smoke harness with every collaborator pre-wired."""
    return _build_pipeline()


# ---------------------------------------------------------------------------
# AC: Module exists at tests/integration/test_mode_b_smoke_e2e.py
# ---------------------------------------------------------------------------


class TestModuleExists:
    """AC: Test module exists at the canonical path."""

    def test_module_path_matches_acceptance_criterion(self) -> None:
        assert __name__.endswith("test_mode_b_smoke_e2e")


# ---------------------------------------------------------------------------
# AC: Mode B chain shape — exactly four canonical stages, no PO/architect
# ---------------------------------------------------------------------------


class TestModeBChainShape:
    """AC: Stage history has the canonical four entries; no Mode A pre-stages."""

    @pytest.mark.asyncio
    async def test_chain_has_exactly_four_canonical_stages_in_order(
        self, mode_b_pipeline: ModeBSmokePipeline
    ) -> None:
        report = await mode_b_pipeline.drive_until_paused()
        assert report.outcome is TurnOutcome.DISPATCHED
        assert report.chosen_stage is StageClass.PULL_REQUEST_REVIEW

        recorded = [
            row["chosen_stage"]
            for row in mode_b_pipeline.stage_log.chronology
            if row["outcome"] is TurnOutcome.DISPATCHED
        ]
        assert recorded == list(MODE_B_CANONICAL_ORDER), (
            f"Mode B chronology diverged from canonical order; got "
            f"{[s.value if s else None for s in recorded]!r}"
        )

    @pytest.mark.asyncio
    async def test_no_product_owner_or_architect_or_system_stage_dispatched(
        self, mode_b_pipeline: ModeBSmokePipeline
    ) -> None:
        await mode_b_pipeline.drive_until_paused()
        recorded_stages = {
            row["chosen_stage"]
            for row in mode_b_pipeline.stage_log.chronology
            if row["outcome"] is TurnOutcome.DISPATCHED
        }
        forbidden_seen = recorded_stages & MODE_B_FORBIDDEN_HISTORY_STAGES
        assert forbidden_seen == set(), (
            f"Mode B build dispatched forbidden Mode A pre-stages: "
            f"{[s.value for s in forbidden_seen]!r}"
        )
        # Subprocess dispatcher must never have been called for any of
        # the four pre-feature-spec Mode A stages.
        subprocess_stages = {
            call["stage"] for call in mode_b_pipeline.subprocess_dispatcher.calls
        }
        assert subprocess_stages & MODE_B_FORBIDDEN_HISTORY_STAGES == set()

    @pytest.mark.asyncio
    async def test_no_degraded_specialist_rationale_in_history(
        self, mode_b_pipeline: ModeBSmokePipeline
    ) -> None:
        await mode_b_pipeline.drive_until_paused()
        for row in mode_b_pipeline.stage_log.chronology:
            rationale = row.get("rationale") or ""
            assert "degraded" not in rationale.lower(), (
                f"Unexpected degraded-specialist rationale on Mode B "
                f"chronology row: {row!r}"
            )

    @pytest.mark.asyncio
    async def test_reasoning_model_is_never_consulted_in_mode_b(
        self, mode_b_pipeline: ModeBSmokePipeline
    ) -> None:
        await mode_b_pipeline.drive_until_paused()
        # Mode B uses the planner directly; the reasoning model port is
        # only consulted on Mode A.
        assert mode_b_pipeline.reasoning_model.calls == 0


# ---------------------------------------------------------------------------
# AC: Build pauses at PR-review with MANDATORY_HUMAN_APPROVAL + PR URL
# ---------------------------------------------------------------------------


class TestModeBPullRequestPause:
    """AC: Build pauses at PR-review with mandatory_human gate + recorded URL."""

    @pytest.mark.asyncio
    async def test_pauses_at_pull_request_review(
        self, mode_b_pipeline: ModeBSmokePipeline
    ) -> None:
        report = await mode_b_pipeline.drive_until_paused()
        assert report.outcome is TurnOutcome.DISPATCHED
        assert report.chosen_stage is StageClass.PULL_REQUEST_REVIEW
        assert report.chosen_feature_id == SMOKE_FEATURE_ID

    @pytest.mark.asyncio
    async def test_records_mandatory_human_gate_mode_and_pr_url(
        self, mode_b_pipeline: ModeBSmokePipeline
    ) -> None:
        await mode_b_pipeline.drive_until_paused()
        terminal_row = mode_b_pipeline.stage_log.chronology[-1]
        assert terminal_row["chosen_stage"] is StageClass.PULL_REQUEST_REVIEW
        assert terminal_row["gate_mode"] == "MANDATORY_HUMAN_APPROVAL"
        assert terminal_row["pr_url"]
        assert terminal_row["pr_url"].startswith(
            "https://github.com/example/forge/pull/"
        )

    @pytest.mark.asyncio
    async def test_pr_review_gate_received_auto_approve_false(
        self, mode_b_pipeline: ModeBSmokePipeline
    ) -> None:
        await mode_b_pipeline.drive_until_paused()
        # Per ASSUM-011 the operator (not the model) approves the PR.
        assert len(mode_b_pipeline.pr_review_gate.submissions) == 1
        submission = mode_b_pipeline.pr_review_gate.submissions[0]
        assert submission["auto_approve"] is False
        assert submission["feature_id"] == SMOKE_FEATURE_ID


# ---------------------------------------------------------------------------
# AC: Forward-propagation invariants
# ---------------------------------------------------------------------------


class TestModeBForwardPropagation:
    """AC: Each downstream dispatch threads the upstream artefact paths."""

    @pytest.mark.asyncio
    async def test_feature_plan_dispatch_carries_feature_spec_artefact_paths(
        self, mode_b_pipeline: ModeBSmokePipeline
    ) -> None:
        await mode_b_pipeline.drive_until_paused()
        # Locate the FEATURE_PLAN dispatch call.
        plan_calls = [
            call
            for call in mode_b_pipeline.subprocess_dispatcher.calls
            if call["stage"] is StageClass.FEATURE_PLAN
        ]
        assert len(plan_calls) == 1
        forward_context: list[ContextEntry] = plan_calls[0]["forward_context"]
        path_values = [
            entry.value for entry in forward_context if entry.kind == "path"
        ]
        for spec_path in SPEC_ARTEFACT_PATHS:
            assert spec_path in path_values, (
                f"FEATURE_PLAN dispatch did not see feature-spec artefact "
                f"path {spec_path!r}; got {path_values!r}"
            )

    @pytest.mark.asyncio
    async def test_autobuild_dispatch_context_carries_feature_plan_artefact_paths(
        self, mode_b_pipeline: ModeBSmokePipeline
    ) -> None:
        # AUTOBUILD routes through ``autobuild_dispatcher`` (sync) — the
        # forward-propagation contract is exercised by consulting the
        # ``ForwardContextBuilder`` against the same stage_log the
        # planner saw at AUTOBUILD time. Drive the build to PR pause so
        # the FEATURE_PLAN row is approved with its artefact paths.
        await mode_b_pipeline.drive_until_paused()
        autobuild_context = mode_b_pipeline.forward_builder.build_for(
            stage=StageClass.AUTOBUILD,
            build_id=SMOKE_BUILD_ID,
            feature_id=SMOKE_FEATURE_ID,
            mode=BuildMode.MODE_B,
        )
        path_values = [e.value for e in autobuild_context if e.kind == "path"]
        for plan_path in PLAN_ARTEFACT_PATHS:
            assert plan_path in path_values, (
                f"AUTOBUILD forward context did not surface feature-plan "
                f"artefact path {plan_path!r}; got {path_values!r}"
            )

    @pytest.mark.asyncio
    async def test_autobuild_did_not_dispatch_before_plan_was_approved(
        self, mode_b_pipeline: ModeBSmokePipeline
    ) -> None:
        await mode_b_pipeline.drive_until_paused()
        # The chronology is in dispatch order — find the indices of
        # FEATURE_PLAN and AUTOBUILD and assert plan precedes autobuild.
        stages_in_order = [
            row["chosen_stage"]
            for row in mode_b_pipeline.stage_log.chronology
            if row["outcome"] is TurnOutcome.DISPATCHED
        ]
        plan_idx = stages_in_order.index(StageClass.FEATURE_PLAN)
        autobuild_idx = stages_in_order.index(StageClass.AUTOBUILD)
        assert plan_idx < autobuild_idx, (
            f"AUTOBUILD dispatched before FEATURE_PLAN approval; "
            f"chronology={[s.value for s in stages_in_order]!r}"
        )

    @pytest.mark.asyncio
    async def test_feature_plan_did_not_dispatch_before_spec_was_approved(
        self, mode_b_pipeline: ModeBSmokePipeline
    ) -> None:
        await mode_b_pipeline.drive_until_paused()
        stages_in_order = [
            row["chosen_stage"]
            for row in mode_b_pipeline.stage_log.chronology
            if row["outcome"] is TurnOutcome.DISPATCHED
        ]
        spec_idx = stages_in_order.index(StageClass.FEATURE_SPEC)
        plan_idx = stages_in_order.index(StageClass.FEATURE_PLAN)
        assert spec_idx < plan_idx


# ---------------------------------------------------------------------------
# AC: Async dispatch — autobuild via dispatch_autobuild_async + responsive
# supervisor + wave/task indices visible
# ---------------------------------------------------------------------------


class TestModeBAsyncDispatch:
    """AC: Autobuild routes via the async dispatcher; status remains responsive."""

    @pytest.mark.asyncio
    async def test_autobuild_dispatched_via_autobuild_async(
        self, mode_b_pipeline: ModeBSmokePipeline
    ) -> None:
        await mode_b_pipeline.drive_until_paused()
        # Exactly one autobuild dispatch — for the single Mode B feature.
        assert len(mode_b_pipeline.autobuild_dispatcher.calls) == 1
        call = mode_b_pipeline.autobuild_dispatcher.calls[0]
        assert call["build_id"] == SMOKE_BUILD_ID
        assert call["feature_id"] == SMOKE_FEATURE_ID
        # The subprocess dispatcher must NOT have run AUTOBUILD.
        subprocess_stages = {
            c["stage"] for c in mode_b_pipeline.subprocess_dispatcher.calls
        }
        assert StageClass.AUTOBUILD not in subprocess_stages

    @pytest.mark.asyncio
    async def test_async_tasks_channel_exposes_wave_and_task_indices(
        self, mode_b_pipeline: ModeBSmokePipeline
    ) -> None:
        # Drive only as far as the autobuild dispatch by stopping when
        # the channel transitions to in-flight.
        pipeline = mode_b_pipeline

        async def drive_to_autobuild() -> None:
            for _ in range(8):
                report = await pipeline.supervisor.next_turn(SMOKE_BUILD_ID)
                pipeline._absorb(
                    report=report,
                    build_id=SMOKE_BUILD_ID,
                    feature_id=SMOKE_FEATURE_ID,
                )
                if report.chosen_stage is StageClass.AUTOBUILD:
                    return
            raise AssertionError(
                "AUTOBUILD never dispatched within driver budget"
            )

        await drive_to_autobuild()
        # After AUTOBUILD dispatch the harness records the channel state
        # as completed (the dispatcher returned synchronously). The
        # snapshot taken immediately after the dispatch carries the
        # wave + task indices the dispatcher set, which satisfies the
        # AC "exposes wave/task indices during the run".
        states = list(
            pipeline.async_task_reader.list_autobuild_states(SMOKE_BUILD_ID)
        )
        assert len(states) == 1
        state = states[0]
        assert state.feature_id == SMOKE_FEATURE_ID
        assert state.wave_index == 1
        assert state.task_index == 1

    @pytest.mark.asyncio
    async def test_supervisor_remains_responsive_to_status_query_in_flight(
        self, mode_b_pipeline: ModeBSmokePipeline
    ) -> None:
        # Simulate a sibling-feature autobuild in flight on the same
        # build. The supervisor should return promptly with a
        # WAITING_PRIOR_AUTOBUILD outcome rather than block — that is
        # the responsiveness contract the AC pins.
        pipeline = mode_b_pipeline
        # Prime FEATURE_SPEC + FEATURE_PLAN as approved so the planner
        # selects AUTOBUILD on the next turn.
        pipeline.stage_log.mark_approved(
            build_id=SMOKE_BUILD_ID,
            stage=StageClass.FEATURE_SPEC,
            feature_id=SMOKE_FEATURE_ID,
            artefact_paths=SPEC_ARTEFACT_PATHS,
        )
        pipeline.stage_log.mark_approved(
            build_id=SMOKE_BUILD_ID,
            stage=StageClass.FEATURE_PLAN,
            feature_id=SMOKE_FEATURE_ID,
            artefact_paths=PLAN_ARTEFACT_PATHS,
        )
        pipeline.stage_log.append_history_entry(
            SMOKE_BUILD_ID,
            FakeModeBStageEntry(
                stage=StageClass.FEATURE_SPEC,
                status=MODE_B_APPROVED,
                feature_id=SMOKE_FEATURE_ID,
                details={"artefact_paths": list(SPEC_ARTEFACT_PATHS)},
            ),
        )
        pipeline.stage_log.append_history_entry(
            SMOKE_BUILD_ID,
            FakeModeBStageEntry(
                stage=StageClass.FEATURE_PLAN,
                status=MODE_B_APPROVED,
                feature_id=SMOKE_FEATURE_ID,
                details={"artefact_paths": list(PLAN_ARTEFACT_PATHS)},
            ),
        )
        # Inject a sibling feature autobuild in flight — the per-feature
        # sequencer will refuse the requested AUTOBUILD dispatch.
        pipeline.async_task_reader.set_in_flight(
            build_id=SMOKE_BUILD_ID,
            feature_id="FEAT-OTHER-SIBLING",
            lifecycle="running_wave",
        )

        report = await pipeline.supervisor.next_turn(SMOKE_BUILD_ID)
        assert report.outcome is TurnOutcome.WAITING_PRIOR_AUTOBUILD
        # Dispatcher MUST NOT have been called — that proves the
        # supervisor refused without waiting on a real subprocess.
        assert pipeline.autobuild_dispatcher.calls == []


# ---------------------------------------------------------------------------
# AC: CLI steering — skip refused at PR-review, honoured at feature-plan;
# cancel resolves as synthetic reject and reaches CANCELLED.
# ---------------------------------------------------------------------------


class TestModeBCliSteering:
    """AC: ``forge skip`` / ``forge cancel`` honour the constitutional rules."""

    def test_skip_against_pull_request_review_is_refused_constitutionally(
        self, mode_b_pipeline: ModeBSmokePipeline
    ) -> None:
        harness = mode_b_pipeline.cli_handler_factory()
        handler = harness.make_handler()
        outcome = handler.handle_skip(
            build_id=SMOKE_BUILD_ID,
            stage=StageClass.PULL_REQUEST_REVIEW,
            reason="operator wants to ship without review",
        )
        assert outcome.status is SkipStatus.REFUSED_CONSTITUTIONAL
        assert outcome.is_refused
        # The constitutional guard's rationale is woven into the
        # recorded refusal message.
        assert "ADR-ARCH-026" in outcome.rationale
        assert outcome.guard_decision.verdict.value == "refused_constitutional"
        # No skip was recorded; the refusal row was logged instead.
        assert harness.skip_recorder.skipped == []
        assert len(harness.skip_recorder.refused) == 1
        assert harness.build_resumer.resumes == []
        # PR review remains in the canonical constitutional set.
        assert StageClass.PULL_REQUEST_REVIEW in CONSTITUTIONAL_STAGES

    def test_skip_against_feature_plan_flag_for_review_is_honoured(
        self, mode_b_pipeline: ModeBSmokePipeline
    ) -> None:
        harness = mode_b_pipeline.cli_handler_factory()
        handler = harness.make_handler()
        outcome = handler.handle_skip(
            build_id=SMOKE_BUILD_ID,
            stage=StageClass.FEATURE_PLAN,
            reason="plan was already reviewed manually",
        )
        assert outcome.status is SkipStatus.SKIPPED
        # FEATURE_PLAN is per-feature but NOT constitutional — the
        # guard allows the skip.
        assert StageClass.FEATURE_PLAN in PER_FEATURE_STAGES
        assert StageClass.FEATURE_PLAN not in CONSTITUTIONAL_STAGES
        # The skip was recorded and the resume nudge fired so the
        # supervisor's next turn picks the build back up.
        assert len(harness.skip_recorder.skipped) == 1
        assert harness.skip_recorder.refused == []
        assert len(harness.build_resumer.resumes) == 1
        assert harness.build_resumer.resumes[0]["build_id"] == SMOKE_BUILD_ID
        assert (
            harness.build_resumer.resumes[0]["skipped_stage"]
            is StageClass.FEATURE_PLAN
        )

    def test_cancel_during_pre_pr_pause_resolves_as_synthetic_reject(
        self, mode_b_pipeline: ModeBSmokePipeline
    ) -> None:
        harness = mode_b_pipeline.cli_handler_factory()
        # Snapshot says the build is paused at FEATURE_PLAN — a Mode B
        # pre-PR checkpoint.
        harness.snapshot_reader.snapshots[SMOKE_BUILD_ID] = BuildSnapshot(
            build_id=SMOKE_BUILD_ID,
            lifecycle=BuildLifecycle.PAUSED_AT_GATE,
            paused_stage=StageClass.FEATURE_PLAN,
            paused_feature_id=SMOKE_FEATURE_ID,
        )
        handler = harness.make_handler()

        outcome = handler.handle_cancel(
            build_id=SMOKE_BUILD_ID, reason="abandoned"
        )

        # The synthetic-reject pathway fired (FEAT-FORGE-004 ASSUM-005).
        assert outcome.status is CancelStatus.CANCELLED_VIA_PAUSE_REJECT
        assert outcome.is_terminal
        assert outcome.paused_stage is StageClass.FEATURE_PLAN
        assert outcome.paused_feature_id == SMOKE_FEATURE_ID
        # Pause-reject hook was invoked with the right scope.
        assert len(harness.pause_reject_resolver.calls) == 1
        reject_call = harness.pause_reject_resolver.calls[0]
        assert reject_call["build_id"] == SMOKE_BUILD_ID
        assert reject_call["stage"] is StageClass.FEATURE_PLAN
        assert reject_call["feature_id"] == SMOKE_FEATURE_ID
        # The build was transitioned to terminal CANCELLED.
        assert len(harness.build_canceller.cancellations) == 1
        assert (
            harness.build_canceller.cancellations[0]["build_id"]
            == SMOKE_BUILD_ID
        )
        # ASSUM-005 + ADR-ARCH-026 references are echoed in the rationale.
        assert "ASSUM-005" in outcome.rationale


# ---------------------------------------------------------------------------
# AC: Test runs in under 30 seconds with all dispatchers stubbed
# ---------------------------------------------------------------------------


class TestModeBSmokeBudget:
    """AC: Smoke completes well within the 30-second wall-clock budget."""

    @pytest.mark.asyncio
    async def test_smoke_completes_within_thirty_seconds(
        self, mode_b_pipeline: ModeBSmokePipeline
    ) -> None:
        start = time.monotonic()
        await mode_b_pipeline.drive_until_paused()
        elapsed = time.monotonic() - start
        # Generous headroom — the in-memory harness completes in
        # well under one second; the AC budget is 30s.
        assert elapsed < 30.0, (
            f"Mode B smoke exceeded 30-second budget: elapsed={elapsed:.2f}s"
        )

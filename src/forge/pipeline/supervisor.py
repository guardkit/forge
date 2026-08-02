"""Supervisor reasoning-loop dispatch turn (TASK-MAG7-010, FEAT-FORGE-007).

This module owns the **reasoning-loop turn function** the FEAT-FORGE-007
supervisor runs once per build per outer-loop iteration. Each call to
:meth:`Supervisor.next_turn`:

1. Reads the build's current state from the FEAT-FORGE-001 state machine
   (via the injected :class:`StateMachineReader` Protocol).
2. Asks :class:`~forge.pipeline.stage_ordering_guard.StageOrderingGuard`
   for the set of dispatchable stages (TASK-MAG7-003 belt-and-braces).
3. Presents the permitted set + per-stage forward-propagation hints
   (TASK-MAG7-002) to the reasoning model and parses the chosen
   dispatch.
4. Re-checks the model's choice against the permitted set — refuses with
   a structured warning if the model hallucinates an out-of-band stage.
5. Applies the per-feature autobuild sequencer
   (:class:`~forge.pipeline.per_feature_sequencer.PerFeatureLoopSequencer`,
   TASK-MAG7-005) for ``AUTOBUILD`` choices.
6. Applies the constitutional guard
   (:class:`~forge.pipeline.constitutional_guard.ConstitutionalGuard`,
   TASK-MAG7-004) for ``PULL_REQUEST_REVIEW`` gate decisions.
7. Routes the (verified) dispatch to the correct dispatcher:

   ============================  ====================================
   Stage class                   Dispatcher
   ============================  ====================================
   ``PRODUCT_OWNER``             specialist (TASK-MAG7-007)
   ``ARCHITECT``                 specialist (TASK-MAG7-007)
   ``SYSTEM_ARCH``               subprocess  (TASK-MAG7-008)
   ``SYSTEM_DESIGN``             subprocess  (TASK-MAG7-008)
   ``FEATURE_SPEC``              subprocess  (TASK-MAG7-008)
   ``FEATURE_PLAN``              subprocess  (TASK-MAG7-008)
   ``AUTOBUILD``                 autobuild_async (TASK-MAG7-009)
   ``PULL_REQUEST_REVIEW``       gate decision (FEAT-FORGE-004)
   ============================  ====================================

8. Records the per-turn outcome (decision + rationale) in ``stage_log``
   via the injected :class:`StageLogTurnRecorder` Protocol.

The supervisor itself owns **no mutable state** keyed on ``build_id`` —
every dependency is injected, every method takes ``build_id`` explicitly,
and the same instance is safe to call from concurrent ``next_turn``
invocations on different builds (FEAT-FORGE-007 Group F @concurrency).

I/O-thinness is a hard requirement (TASK-MAG7-010 implementation notes):
this module never opens a SQLite connection, never spawns a process,
never reads NATS — it composes the guards and dispatchers from Waves 2
and 3 and lets *them* do the I/O behind their respective Protocols.

References
----------

- TASK-MAG7-010 — this task brief.
- TASK-MAG7-001 — :mod:`forge.pipeline.stage_taxonomy`.
- TASK-MAG7-002 — forward-propagation hints (consumed via
  :class:`~forge.pipeline.forward_propagation.ForwardPropagation` if
  available; this module accepts the hints as plain strings so the
  taxonomy/propagation modules stay decoupled from the supervisor).
- TASK-MAG7-003 — :class:`StageOrderingGuard`.
- TASK-MAG7-004 — :class:`ConstitutionalGuard`.
- TASK-MAG7-005 — :class:`PerFeatureLoopSequencer`.
- TASK-MAG7-007 — :func:`dispatch_specialist_stage`.
- TASK-MAG7-008 — :func:`dispatch_subprocess_stage`.
- TASK-MAG7-009 — :func:`dispatch_autobuild_async`.
- ADR-ARCH-026 — Constitutional rules (belt-and-braces).
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Awaitable, Callable, Mapping, Protocol, runtime_checkable

from forge.lifecycle.modes import BuildMode
from forge.pipeline.constitutional_guard import (
    AutoApproveDecision,
    AutoApproveVerdict,
    ConstitutionalGuard,
)
from forge.pipeline.mode_chains_data import (
    MODE_B_CHAIN,
    MODE_B_PREREQUISITES,
    MODE_C_CHAIN,
    MODE_C_PREREQUISITES,
)
from forge.pipeline.per_feature_sequencer import (
    AsyncTaskReader as PerFeatureAsyncTaskReader,
)
from forge.pipeline.per_feature_sequencer import PerFeatureLoopSequencer
from forge.pipeline.per_feature_sequencer import (
    StageLogReader as PerFeatureStageLogReader,
)
from forge.pipeline.stage_ordering_guard import (
    StageLogReader as OrderingStageLogReader,
)
from forge.pipeline.stage_ordering_guard import StageOrderingGuard
from forge.pipeline.stage_taxonomy import PER_FEATURE_STAGES, StageClass

# NOTE: ``forge.pipeline.mode_b_planner`` / ``mode_c_planner`` /
# ``terminal_handlers`` import :class:`forge.lifecycle.persistence.Build`,
# which transitively re-imports this module via
# :mod:`forge.lifecycle.state_machine`. We import them under
# ``TYPE_CHECKING`` and resolve concretely at the call site to avoid
# the partially-initialised-module ImportError.
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import-time only
    from datetime import datetime

    from forge.config.models import BudgetGuards
    from forge.pipeline import PipelineLifecycleEmitter
    from forge.pipeline.mode_b_planner import (
        ModeBChainPlanner,
        ModeBPlan,
        StageEntry as ModeBStageEntry,
    )
    from forge.pipeline.mode_c_planner import (
        ModeCCyclePlanner,
        ModeCPlan,
        StageEntry as ModeCStageEntry,
    )
    from forge.pipeline.terminal_handlers import (
        ModeBPostAutobuild,
    )
    from forge.pipeline.terminal_handlers.mode_c import (
        CommitProbe,
        ModeCTerminalDecision,
    )

logger = logging.getLogger(__name__)


__all__ = [
    "BuildModeReader",
    "BuildState",
    "DispatchChoice",
    "ModeBHistoryReader",
    "ModeCHistoryReader",
    "PRReviewGate",
    "ReasoningModelPort",
    "StageLogTurnRecorder",
    "StateMachineReader",
    "Supervisor",
    "TerminalStateError",
    "TurnOutcome",
    "TurnReport",
]


# ---------------------------------------------------------------------------
# Outcome enum + report dataclass — supervisor's structured return shape
# ---------------------------------------------------------------------------


class TurnOutcome(StrEnum):
    """Closed set of outcomes :meth:`Supervisor.next_turn` can yield.

    The outcome tells the supervisor's *outer* loop what to do next and
    is recorded verbatim on the per-turn ``stage_log`` row so the audit
    trail captures why a turn ended where it did.

    Members:
        DISPATCHED: A dispatch was issued to one of the four dispatchers.
            ``TurnReport.dispatch_result`` carries the dispatcher's
            structured outcome.
        WAITING: No dispatchable stages and the build is in a non-terminal
            state (e.g. waiting on an approval response). The outer loop
            should pause until an external event resumes the build.
        WAITING_PRIOR_AUTOBUILD: An ``AUTOBUILD`` was permitted by the
            stage-ordering guard, but the per-feature sequencer refused
            because a sibling feature's autobuild is still in flight.
        REFUSED_OUT_OF_BAND: The reasoning model picked a stage outside
            the permitted set. The supervisor refused to act and recorded
            a structured warning. The outer loop should retry the turn.
        REFUSED_CONSTITUTIONAL: A ``PULL_REQUEST_REVIEW`` auto-approve
            was attempted and the constitutional guard refused. The PR
            stays gated for mandatory human approval.
        TERMINAL: The build is in a terminal state (COMPLETE / FAILED /
            CANCELLED). The supervisor will not produce further turns.
        NO_OP: The reasoning model declined to choose any stage (returned
            ``None``). This is distinct from ``WAITING``: the model
            actively reasoned about an empty action.
        PAUSED_BUDGET: An unattended-profile Mode C build breached a
            :class:`~forge.config.models.BudgetGuards` cap (review cycles /
            wall-clock / tokens / coach score). The supervisor refused to
            dispatch the next cycle, escalated a ``risk_level="high"``
            approval request, and paused the build (UBS-002). Never fired
            for attended profiles (caps off, ASSUM-010).
    """

    DISPATCHED = "dispatched"
    WAITING = "waiting"
    WAITING_PRIOR_AUTOBUILD = "waiting_prior_autobuild"
    REFUSED_OUT_OF_BAND = "refused_out_of_band"
    REFUSED_CONSTITUTIONAL = "refused_constitutional"
    TERMINAL = "terminal"
    NO_OP = "no_op"
    PAUSED_BUDGET = "paused_budget"


class BuildState(StrEnum):
    """Coarse state-machine states the supervisor reasons about.

    Mirrors the FEAT-FORGE-001 ``Build.state`` literal — the full
    lifecycle vocabulary the schema's ``builds.status CHECK`` enforces
    (see ``forge/lifecycle/schema.sql``). The supervisor only branches
    on a handful of these (PREPARING / RUNNING / PAUSED / FINALISING /
    terminals); the rest are owned by the lifecycle state machine
    (TASK-PSM-004) and the queue / recovery code paths.

    This enum is the **single source of truth** for build states. The
    transition table that governs *which* states can flow into which
    others lives in :mod:`forge.lifecycle.state_machine` — that module
    re-exports :class:`BuildState` so callers never define a parallel
    enum. Adding a state here without adding a row to the transition
    table is a bug; the property tests in ``test_state_machine.py``
    enforce that invariant.
    """

    QUEUED = "QUEUED"
    PREPARING = "PREPARING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    FINALISING = "FINALISING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"

    @property
    def is_terminal(self) -> bool:
        """Return ``True`` for terminal states.

        Terminal states are COMPLETE / FAILED / CANCELLED / SKIPPED — no
        transition out of any of these is permitted by the lifecycle
        transition table (TASK-PSM-004).
        """
        return self in (
            BuildState.COMPLETE,
            BuildState.FAILED,
            BuildState.CANCELLED,
            BuildState.SKIPPED,
        )


@dataclass(frozen=True, slots=True)
class DispatchChoice:
    """Structured dispatch choice returned by the reasoning model.

    The reasoning model is asked to pick one entry from the permitted
    set; this dataclass is the parsed shape the supervisor consumes. A
    ``None`` choice (no dispatch) is represented by the model returning
    ``None`` from :meth:`ReasoningModelPort.choose_dispatch`, not by
    constructing a sentinel :class:`DispatchChoice`.

    Attributes:
        stage: The stage class the model selected.
        feature_id: Per-feature scope. Required for stages in
            :data:`PER_FEATURE_STAGES` other than ``PULL_REQUEST_REVIEW``.
        rationale: Free-form rationale string the model emits alongside
            its choice. Recorded verbatim on the per-turn ``stage_log``
            row so the audit trail captures *why* the model chose this
            dispatch.
        auto_approve: For ``PULL_REQUEST_REVIEW`` only — set to ``True``
            when the model wants to attempt an auto-approve. The
            constitutional guard veto runs on this flag (AC: PR-review
            constitutional veto).
    """

    stage: StageClass
    feature_id: str | None = None
    rationale: str = ""
    auto_approve: bool = False


@dataclass(frozen=True, slots=True)
class TurnReport:
    """Structured return shape of :meth:`Supervisor.next_turn`.

    Threaded back to the outer loop and recorded verbatim on the
    per-turn ``stage_log`` row. The :attr:`outcome` field is the primary
    discriminator; the remaining fields carry context for the outer loop
    and the audit trail.

    Attributes:
        outcome: The :class:`TurnOutcome` enum member.
        build_id: Build identifier the turn was for.
        permitted_stages: Snapshot of the dispatchable set returned by
            :class:`StageOrderingGuard`. Stored as a frozen set so the
            audit trail can compare against the model's choice.
        chosen_stage: The stage the reasoning model picked (or ``None``
            if no dispatch attempted).
        chosen_feature_id: Feature scope of the choice, or ``None``.
        rationale: Combined rationale: model's reasoning plus any
            supervisor-side append (e.g. constitutional refusal).
        dispatch_result: Whatever the dispatcher returned. The
            supervisor does not interpret this — the gating layer does.
        gate_decision: For ``PULL_REQUEST_REVIEW`` turns, the
            :class:`AutoApproveDecision` returned by the constitutional
            guard. ``None`` for every other outcome.
    """

    outcome: TurnOutcome
    build_id: str
    permitted_stages: frozenset[StageClass] = frozenset()
    chosen_stage: StageClass | None = None
    chosen_feature_id: str | None = None
    rationale: str = ""
    dispatch_result: Any | None = None
    gate_decision: AutoApproveDecision | None = None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TerminalStateError(RuntimeError):
    """Raised by callers that try to invoke ``next_turn`` after a terminal state.

    The supervisor itself does **not** raise this — terminal builds yield
    :attr:`TurnOutcome.TERMINAL` so the outer loop can clean up gracefully.
    The exception type is exported so tests and diagnostic tooling can
    assert on it, and so future callers that want a hard-fail signal can
    opt in by inspecting the ``outcome``.
    """


# ---------------------------------------------------------------------------
# Injected Protocols — supervisor's only I/O surface
# ---------------------------------------------------------------------------


@runtime_checkable
class StateMachineReader(Protocol):
    """Read-side Protocol over the FEAT-FORGE-001 build state machine.

    The supervisor only needs the current coarse state of the build —
    "is this build still running, or has it terminated?". The full state
    machine surface (transitions, history, etc.) lives in
    FEAT-FORGE-001; this Protocol is the read-only seam the supervisor
    consults once per turn.
    """

    def get_build_state(
        self, build_id: str
    ) -> BuildState:  # pragma: no cover - protocol stub
        """Return the current :class:`BuildState` for ``build_id``."""
        ...


@runtime_checkable
class ReasoningModelPort(Protocol):
    """Protocol over the supervisor's reasoning model.

    Production wires a LangChain ``ChatModel`` adapter; tests inject an
    in-memory fake that returns a deterministic :class:`DispatchChoice`.
    The Protocol is intentionally narrow — exactly one call per turn,
    receiving the build state, the permitted set, and a per-stage hints
    mapping (forward-propagation hints from TASK-MAG7-002).
    """

    def choose_dispatch(
        self,
        *,
        build_id: str,
        build_state: BuildState,
        permitted_stages: frozenset[StageClass],
        stage_hints: Mapping[StageClass, str],
        feature_catalogue: tuple[str, ...],
    ) -> DispatchChoice | None:  # pragma: no cover - protocol stub
        """Return the dispatch the model wants to issue, or ``None`` for no-op.

        Args:
            build_id: Build identifier.
            build_state: Current coarse state-machine state.
            permitted_stages: Set returned by
                :meth:`StageOrderingGuard.next_dispatchable`. The model
                is asked to pick one entry; anything outside this set
                will be refused at the executor layer.
            stage_hints: Mapping of stage class to a free-form hint
                string (forward-propagation context, prior reasoning,
                etc.). Empty mapping is fine — the supervisor never
                depends on a specific hint shape.
            feature_catalogue: Feature IDs in the build's catalogue.
                Required for the model to choose a per-feature stage.
        """
        ...


@runtime_checkable
class StageLogTurnRecorder(Protocol):
    """Recorder over the FEAT-FORGE-001 ``stage_log`` for per-turn rows.

    Distinct from the dispatcher-side stage-log writers (which record
    *dispatch* outcomes); this Protocol records the **per-turn** decision
    — outcome, rationale, the permitted set, the chosen stage, and any
    gate verdict. Production wires the SQLite adapter; tests use an
    in-memory fake.
    """

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
    ) -> None:  # pragma: no cover - protocol stub
        """Persist a per-turn audit row.

        Args:
            build_id: Build identifier.
            outcome: The :class:`TurnOutcome` discriminator.
            permitted_stages: Snapshot of the dispatchable set.
            chosen_stage: Stage class the model picked (or ``None``).
            chosen_feature_id: Feature scope (or ``None``).
            rationale: Combined rationale string. May include the model's
                free-form text plus supervisor-side appendices (e.g.
                "constitutional refusal: ...").
            gate_verdict: Optional verdict string for PR-review turns
                (e.g. ``"refused"`` / ``"allowed"``). ``None`` for
                non-gate turns.
        """
        ...


@runtime_checkable
class BuildModeReader(Protocol):
    """Read-side Protocol returning a build's :class:`BuildMode`.

    Production wires the FEAT-FORGE-001 SQLite adapter
    (:meth:`SqliteBuildSnapshotReader.read_snapshot`); tests inject an
    in-memory mapping. Defaults to :attr:`BuildMode.MODE_A` when no
    reader is provided so every TASK-MAG7-010 caller continues to work
    without modification (TASK-MBC8-008 backwards-compat invariant).
    """

    def get_build_mode(
        self, build_id: str
    ) -> BuildMode:  # pragma: no cover - protocol stub
        """Return the :class:`BuildMode` recorded for ``build_id``."""
        ...


@runtime_checkable
class ModeBHistoryReader(Protocol):
    """Read-side Protocol returning Mode B planner-shaped history.

    The planner consumes a sequence of :class:`ModeBStageEntry` rows
    (chronological order). Production projects the FEAT-FORGE-001
    ``stage_log`` rows into this Protocol shape; tests inject simple
    dataclass lists.
    """

    def get_mode_b_history(
        self, build_id: str
    ) -> Sequence[ModeBStageEntry]:  # pragma: no cover - protocol stub
        """Return the Mode B stage history for ``build_id``."""
        ...


@runtime_checkable
class ModeCHistoryReader(Protocol):
    """Read-side Protocol returning Mode C planner-shaped history.

    The cycle planner needs ``StageEntry`` rows that carry fix-task
    payloads; production projects ``stage_log`` rows into this shape,
    tests inject dataclass lists. The ``has_commits`` flag is supplied
    separately so the supervisor can refresh the commit probe between
    review iterations without re-reading the whole history.
    """

    def get_mode_c_history(
        self, build_id: str
    ) -> Sequence[ModeCStageEntry]:  # pragma: no cover - protocol stub
        """Return the Mode C stage history for ``build_id``."""
        ...

    def has_commits(self, build_id: str) -> bool:  # pragma: no cover - protocol stub
        """Return ``True`` iff the build's worktree carries one or more commits.

        Drives the Mode C planner's clean-follow-up-review branch
        (ASSUM-005 / ASSUM-017): the planner advances to
        :attr:`StageClass.PULL_REQUEST_REVIEW` when commits are present
        and terminates with ``CLEAN_REVIEW`` when none are.
        """
        ...


@runtime_checkable
class PRReviewGate(Protocol):
    """Protocol over the FEAT-FORGE-004 pull-request-review gate.

    The supervisor only needs the "submit a gate decision" side — the
    constitutional veto is applied *before* this method is called, so
    by the time we reach :meth:`submit_decision` the auto-approve flag
    has been sanitised against ADR-ARCH-026.
    """

    def submit_decision(
        self,
        *,
        build_id: str,
        feature_id: str,
        auto_approve: bool,
        rationale: str,
    ) -> Any:  # pragma: no cover - protocol stub
        """Submit the gate decision and return the gate's record.

        Since the conductor's revival (design pass §c) the production
        implementation of this protocol is
        :class:`~forge.pipeline.merge_ready_checkpoint.MergeReadyCheckpointPublisher`
        — **the merge-ready checkpoint**, the plain name for what this
        leg does. It never opens a pull request; it pushes the
        gates-green branch and publishes the same approve-click merge
        card the routine path delivers. The return value may be
        awaitable: every supervisor call site funnels through
        :meth:`Supervisor._submit_merge_ready_checkpoint`, which awaits
        it when it is. A plain (non-awaitable) return is unchanged
        behaviour for every pre-existing implementation.
        """
        ...


# ---------------------------------------------------------------------------
# Dispatcher callable types
# ---------------------------------------------------------------------------


# Each dispatcher is injected as a callable so the supervisor never
# imports the concrete dispatch functions directly. Tests inject in-memory
# fakes; production wires ``functools.partial`` adapters around the real
# ``dispatch_specialist_stage`` / ``dispatch_subprocess_stage`` /
# ``dispatch_autobuild_async`` callables.

#: Callable that dispatches a specialist stage. Async because the
#: production target (:func:`dispatch_specialist_stage`) is async.
SpecialistDispatcher = Callable[..., Awaitable[Any]]

#: Callable that dispatches a subprocess stage. Async — wraps
#: :func:`dispatch_subprocess_stage`.
SubprocessDispatcher = Callable[..., Awaitable[Any]]

#: Callable that dispatches an async autobuild. Async because the
#: production target (:func:`dispatch_autobuild_async`) is ``async def``
#: as of TASK-FORGE-FRR-F010G — the launch path awaits the deepagents
#: middleware's async ``StructuredTool.coroutine`` so the
#: autobuild_runner registration can stay URL-less (in-process ASGI
#: transport instead of an HTTP-addressable Agent Protocol surface).
AutobuildDispatcher = Callable[..., Awaitable[Any]]


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


@dataclass
class Supervisor:
    """Reasoning-loop supervisor for the FEAT-FORGE-007 Mode A pipeline.

    The supervisor is a *composition* over the guards and dispatchers
    declared in Waves 2 and 3 of FEAT-FORGE-007. It owns no mutable
    per-build state — every dependency is injected at construction, and
    every public method takes ``build_id`` explicitly so the same
    instance is safe to use across concurrent builds.

    Attributes:
        ordering_guard: TASK-MAG7-003 stage-ordering guard.
        per_feature_sequencer: TASK-MAG7-005 per-feature sequencer.
        constitutional_guard: TASK-MAG7-004 constitutional guard.
        state_reader: FEAT-FORGE-001 state-machine reader.
        ordering_stage_log_reader: Reader for the ordering guard. Same
            ``stage_log`` table as ``per_feature_stage_log_reader``;
            different read shapes (per the two reader Protocols).
        per_feature_stage_log_reader: Reader for the per-feature
            sequencer.
        async_task_reader: Reader for the per-feature sequencer's live
            ``async_tasks`` channel.
        reasoning_model: Reasoning-model port.
        turn_recorder: Per-turn audit-row recorder.
        specialist_dispatcher: Async callable wrapping
            :func:`dispatch_specialist_stage`.
        subprocess_dispatcher: Async callable wrapping
            :func:`dispatch_subprocess_stage`.
        autobuild_dispatcher: Async callable wrapping
            :func:`dispatch_autobuild_async`. TASK-FORGE-FRR-F010G
            switched the production target to ``async def`` so the
            launch path can use the deepagents async ``coroutine``
            entrypoint (URL-less in-process ASGI transport).
        pr_review_gate: FEAT-FORGE-004 gate surface.
        stage_hints: Optional per-stage forward-propagation hints
            mapping (TASK-MAG7-002). Defaults to empty.
    """

    ordering_guard: StageOrderingGuard
    per_feature_sequencer: PerFeatureLoopSequencer
    constitutional_guard: ConstitutionalGuard
    state_reader: StateMachineReader
    ordering_stage_log_reader: OrderingStageLogReader
    per_feature_stage_log_reader: PerFeatureStageLogReader
    async_task_reader: PerFeatureAsyncTaskReader
    reasoning_model: ReasoningModelPort
    turn_recorder: StageLogTurnRecorder
    specialist_dispatcher: SpecialistDispatcher
    subprocess_dispatcher: SubprocessDispatcher
    autobuild_dispatcher: AutobuildDispatcher
    pr_review_gate: PRReviewGate
    stage_hints: Mapping[StageClass, str] = field(default_factory=dict)
    # ----- TASK-MBC8-008: mode-aware dispatch wiring -----------------
    # Every new field defaults to ``None`` so existing TASK-MAG7-010
    # call sites that wire only the Mode A surface continue to compose
    # the dataclass without modification. When the build's mode is
    # MODE_A (the default ``BuildMode`` returned when ``build_mode_reader``
    # is ``None``) every Mode A code path runs untouched.
    build_mode_reader: BuildModeReader | None = None
    mode_b_planner: ModeBChainPlanner | None = None
    mode_b_history_reader: ModeBHistoryReader | None = None
    mode_b_post_autobuild: (
        Callable[[Build, Sequence[ModeBStageEntry]], ModeBPostAutobuild] | None
    ) = None
    mode_c_planner: ModeCCyclePlanner | None = None
    mode_c_history_reader: ModeCHistoryReader | None = None
    mode_c_terminal_handler: Callable[..., Awaitable[ModeCTerminalDecision]] | None = (
        None
    )
    mode_c_commit_probe: CommitProbe | None = None
    fix_task_context_builder: Callable[[str, str, Any], Mapping[str, Any]] | None = None
    # ----- TASK-FW10-008: AsyncSubAgentMiddleware tool surface --------
    # ``tools`` carries the ``AsyncSubAgentMiddleware`` start/check/
    # update/cancel/list tools so the supervisor's reasoning model can
    # dispatch (and observe) the autobuild stage as an
    # :class:`AsyncSubAgent` per ADR-ARCH-031. Defaults to an empty
    # tuple so existing TASK-MAG7-010 / TASK-MBC8-008 callers compose
    # the dataclass unchanged; ``forge.cli.serve._build_supervisor``
    # populates it from the production middleware.
    #
    # ``lifecycle_emitter`` is the FEAT-FORGE-002 :class:`PipelineLifecycleEmitter`
    # threaded into ``dispatch_autobuild_async``'s context payload (DDR-007
    # Option A). The supervisor does not call the emitter directly — it
    # only forwards it to the autobuild dispatcher so the same instance
    # is shared with the autobuild_runner subagent. ``None`` means the
    # caller has not wired the production emitter; the supervisor still
    # dispatches but downstream lifecycle publishes are skipped (the
    # SQLite row stays intact per ADR-ARCH-008).
    tools: tuple[Any, ...] = field(default_factory=tuple)
    lifecycle_emitter: PipelineLifecycleEmitter | None = None

    # ----- FEAT-UBS-002: unattended budget-guard enforcement ----------
    # The resolved :class:`BudgetGuards` for the build. ``None`` (or an
    # ``attended`` caps-off profile) makes the guard a strict no-op —
    # ModeCCyclePlanner behaviour is byte-for-byte unchanged (ASSUM-010).
    # Production resolves this from ``config.budget`` (default_profile =
    # ``attended`` = caps off), so the guard is inert until an operator
    # selects an unattended profile.
    budget_guards: "BudgetGuards | None" = None
    #: Name of the resolved profile, stamped on the escalation payload.
    budget_profile_name: str = "attended"
    #: ``() -> datetime`` wall-clock + ``(build_id) -> datetime | None``
    #: start-time reader for the wall-clock cap. When either is absent the
    #: wall-clock cap is treated as unenforceable (0.0 elapsed), never a
    #: breach.
    budget_wall_clock: "Callable[[], datetime] | None" = None
    budget_started_at_reader: "Callable[[str], datetime | None] | None" = None
    #: ``(build_id) -> float | None`` reader for the most recent Coach score,
    #: feeding the ``min_coach_score`` floor (FEAT-UBS-002 §3 / AC-05). ``None``
    #: (the default, and the attended / no-reader case) leaves
    #: ``last_coach_score`` at ``None`` so the floor stays inert and the guard's
    #: behaviour is byte-identical to the pre-reader path (AC-03). A reader
    #: failure degrades to ``None`` with a loud log — never a crash, never a
    #: false pause. NOTE: the UBS1C runner populates the score on the in-memory
    #: ``AutobuildRunnerState`` in the streaming path, which the Mode C
    #: Supervisor does not query by build_id (the ADR-ARCH-033 two-path split);
    #: the production reader lands with the Mode-C-production (serve.py) lane.
    budget_coach_score_reader: "Callable[[str], float | None] | None" = None
    #: ``async (build_id, feature_id, payload, verdict, metrics) -> None`` —
    #: publishes the risk=high approval request and pauses the build
    #: (SQLite PAUSED + ``emit_paused_then_interrupt``, ADR-ARCH-021
    #: ordering). Injected by the daemon so the supervisor stays decoupled
    #: from the wire / SQLite specifics; ``None`` still enforces by
    #: refusing the dispatch (never a silent continue).
    budget_pause: "Callable[..., Awaitable[None]] | None" = None

    # Stage groupings — pre-computed once so per-turn routing is a
    # single dict lookup rather than a chain of ``in`` checks.
    _SPECIALIST_STAGES: frozenset[StageClass] = field(
        default=frozenset({StageClass.PRODUCT_OWNER, StageClass.ARCHITECT}),
        init=False,
        repr=False,
    )
    _SUBPROCESS_STAGES: frozenset[StageClass] = field(
        default=frozenset(
            {
                StageClass.SYSTEM_ARCH,
                StageClass.SYSTEM_DESIGN,
                StageClass.FEATURE_SPEC,
                StageClass.FEATURE_PLAN,
                # FEAT-FORGE-008 + F008-VAL-003 / TASK-F8-003: Mode C
                # extensions. Multi-feature ``next_turn`` already routes
                # both via ``subprocess_dispatcher`` (supervisor.py
                # 1296-1331); membership here makes the per-turn
                # ``_dispatch`` router agree. ``PER_FEATURE_STAGES``
                # excludes these two, so the missing-feature_id check at
                # line 1501 won't misfire — fix-task identity for
                # ``TASK_WORK`` is enforced upstream by
                # ``ModeCCyclePlanner`` (TASK-MBC8-004).
                StageClass.TASK_REVIEW,
                StageClass.TASK_WORK,
            }
        ),
        init=False,
        repr=False,
    )

    # ------------------------------------------------------------------
    # Internal: the merge-ready checkpoint (design pass §c, Stage 1c)
    # ------------------------------------------------------------------

    async def _submit_merge_ready_checkpoint(
        self,
        *,
        build_id: str,
        feature_id: str,
        auto_approve: bool,
        rationale: str,
    ) -> Any:
        """The ONE funnel every ``pr_review_gate`` call site goes through.

        Design pass §c.2: "``pr_review_gate.submit_decision`` is
        re-pointed at this merge-card publisher — one implementation, all
        four call sites." The four are the ``_dispatch`` router branch,
        the Mode B post-autobuild route, the Mode C planner-chosen
        dispatch, and the Mode C terminal route; each now calls this
        method instead of the gate directly.

        The funnel does exactly one thing beyond forwarding: it awaits an
        awaitable result. The production publisher pushes a branch and
        publishes a card, both async; every pre-existing (synchronous)
        implementation returns a plain value and is forwarded verbatim,
        which is why the flag-off tree is byte-for-byte unchanged.
        """
        result = self.pr_review_gate.submit_decision(
            build_id=build_id,
            feature_id=feature_id,
            auto_approve=auto_approve,
            rationale=rationale,
        )
        if inspect.isawaitable(result):
            return await result
        return result

    @staticmethod
    def _merge_card_turn_outcome(result: Any) -> TurnOutcome:
        """Map a merge-ready checkpoint result onto a :class:`TurnOutcome`.

        The hard precondition of design pass §c.3 made visible to the
        driver loop: **a red gate is not a dispatch.** It either loops
        back into the fix cycle (``WAITING`` — the conductor's next
        review pass picks the branch up) or terminates the journey
        FAILED with a pack (``TERMINAL``). A no-commit checkpoint is a
        silent ``TERMINAL`` (§c.6 — a receipt, never a card).

        **The three no-card endings are TERMINAL, not WAITING** (Stage 2
        shakeout item 6). ``PUBLISH_FAILED`` used to map to ``WAITING``,
        which let the driver re-plan and re-issue the card up to three
        more times for one merge word. A publish that raised may already
        have put the envelope on the wire; ``DELIVERY_NOT_WIRED`` has no
        mechanism to wait for; and ``ALREADY_CHECKPOINTED`` means the one
        card is spent. Each ends the journey honestly with a pack instead
        of waiting for something that is not coming.

        Any result that is NOT a
        :class:`~forge.pipeline.merge_ready_checkpoint.MergeCardDecision`
        — every pre-existing gate implementation, every test double —
        maps to ``DISPATCHED``, which is exactly what these call sites
        recorded before this method existed. That is the backwards-compat
        invariant this whole lane preserves.
        """
        from forge.pipeline.merge_ready_checkpoint import (
            MergeCardDecision,
            MergeCardOutcome,
        )

        if not isinstance(result, MergeCardDecision):
            return TurnOutcome.DISPATCHED
        if result.outcome is MergeCardOutcome.CARD_PUBLISHED:
            return TurnOutcome.DISPATCHED
        if result.outcome in (
            MergeCardOutcome.RED_GATE_FAILED,
            MergeCardOutcome.NO_COMMITS_SILENT,
            MergeCardOutcome.PUBLISH_FAILED,
            MergeCardOutcome.DELIVERY_NOT_WIRED,
            MergeCardOutcome.ALREADY_CHECKPOINTED,
        ):
            return TurnOutcome.TERMINAL
        # RED_GATE_LOOP_BACK — the only checkpoint outcome that re-enters
        # the fix cycle. No card was published on this path.
        #
        # WAITING is the right word HERE, and the driver picks the right
        # word THERE (shadow-replay item 1, 2026-08-01). The distinction
        # this method cannot make, and does not try to:
        #
        #   * a loop-back with a resume seam wired is a genuine wait — the
        #     conductor's next review pass picks the branch up, and the
        #     re-plan is legitimate;
        #   * a loop-back with NO resume seam has nothing to wake it, so
        #     the driver terminates the journey
        #     ``ConductorRunOutcome.RED_GATE_STOP`` — naming the red gate
        #     in the report and the failure pack. It is never written up
        #     as ``WAIT_EXPIRED`` any more; a red-gate refusal is not a
        #     silence, and the old caveat here said exactly that.
        #
        # Whether the wait can arm is a property of the driver's deps, not
        # of a checkpoint result, which is why the branch lives in
        # ``conductor_driver`` and this mapping stays as it is.
        return TurnOutcome.WAITING

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _read_build_mode(self, build_id: str) -> BuildMode:
        """Resolve the build's :class:`BuildMode`.

        Falls back to :attr:`BuildMode.MODE_A` when no
        :class:`BuildModeReader` is wired — preserves byte-for-byte the
        FEAT-FORGE-007 dispatch path for every existing TASK-MAG7-010
        caller (TASK-MBC8-008 backwards-compat invariant).
        """
        if self.build_mode_reader is None:
            return BuildMode.MODE_A
        try:
            return self.build_mode_reader.get_build_mode(build_id)
        except Exception as exc:  # noqa: BLE001 — defensive default
            logger.error(
                "supervisor.next_turn: build_mode_reader raised %s: %s; "
                "falling back to MODE_A for build_id=%s",
                type(exc).__name__,
                exc,
                build_id,
            )
            return BuildMode.MODE_A

    async def next_turn(self, build_id: str) -> TurnReport:
        """Execute one supervisor reasoning turn for ``build_id``.

        This is the entry point exercised by the supervisor's outer
        loop. The method is async because three of the four dispatch
        targets are async; the per-feature autobuild and PR-review
        paths are sync but we keep the surface uniform.

        Sequence:

        1. Read the build state. Terminal → return ``TERMINAL``.
        2. Compute ``permitted_stages`` via the ordering guard.
           Empty + non-terminal → return ``WAITING``.
        3. Ask the reasoning model to pick one entry from the
           permitted set. ``None`` → return ``NO_OP``.
        4. Validate the choice is in the permitted set. Out-of-band
           → ``REFUSED_OUT_OF_BAND`` + structured warning log.
        5. For ``AUTOBUILD``: apply the per-feature sequencer.
           Refused → ``WAITING_PRIOR_AUTOBUILD``.
        6. For ``PULL_REQUEST_REVIEW``: apply the constitutional
           guard's ``veto_auto_approve`` to the model's auto-approve
           flag. Refused → ``REFUSED_CONSTITUTIONAL``.
        7. Route to the correct dispatcher and capture the result.
        8. Record a per-turn ``stage_log`` row regardless of outcome.

        Args:
            build_id: Build identifier the turn is for. Each
                ``next_turn`` call is independent — concurrent builds
                supply different ``build_id`` values and never share
                mutable supervisor state.

        Returns:
            :class:`TurnReport` carrying the outcome plus context.

        Raises:
            TerminalStateError: Never raised by this method; declared
                in the public surface so callers that want a hard-fail
                signal can opt in by checking ``outcome``.
        """
        # 1. State read.
        build_state = self.state_reader.get_build_state(build_id)
        if build_state.is_terminal:
            report = TurnReport(
                outcome=TurnOutcome.TERMINAL,
                build_id=build_id,
                permitted_stages=frozenset(),
                rationale=(
                    f"build {build_id} is terminal (state={build_state.value}); "
                    "supervisor will not produce further turns"
                ),
            )
            self._record_safe(report)
            return report

        # 1a. Mode-aware branch (TASK-MBC8-008). MODE_A falls through
        # to the existing TASK-MAG7-010 code path verbatim; MODE_B and
        # MODE_C have their own dispatch helpers.
        mode = self._read_build_mode(build_id)
        if mode is BuildMode.MODE_B:
            return await self._next_turn_mode_b(
                build_id=build_id, build_state=build_state
            )
        if mode is BuildMode.MODE_C:
            return await self._next_turn_mode_c(
                build_id=build_id, build_state=build_state
            )

        # 2. Permitted set.
        permitted_stages = frozenset(
            self.ordering_guard.next_dispatchable(
                build_id, self.ordering_stage_log_reader
            )
        )
        if not permitted_stages:
            report = TurnReport(
                outcome=TurnOutcome.WAITING,
                build_id=build_id,
                permitted_stages=permitted_stages,
                rationale=(
                    "no dispatchable stages and build is non-terminal; "
                    "waiting on external resume signal (likely an approval "
                    "response — FEAT-FORGE-004)"
                ),
            )
            self._record_safe(report)
            return report

        # 3. Reasoning model choice.
        feature_catalogue = tuple(
            self.ordering_stage_log_reader.feature_catalogue(build_id)
        )
        choice = self.reasoning_model.choose_dispatch(
            build_id=build_id,
            build_state=build_state,
            permitted_stages=permitted_stages,
            stage_hints=dict(self.stage_hints),
            feature_catalogue=feature_catalogue,
        )
        if choice is None:
            report = TurnReport(
                outcome=TurnOutcome.NO_OP,
                build_id=build_id,
                permitted_stages=permitted_stages,
                rationale="reasoning model declined to choose a dispatch",
            )
            self._record_safe(report)
            return report

        # 4. Out-of-band refusal — executor layer enforcement of the
        #    permitted set. ADR-ARCH-026 belt-and-braces.
        if choice.stage not in permitted_stages:
            warning_msg = (
                "supervisor.next_turn: reasoning model picked stage "
                f"{choice.stage.value!r} which is OUTSIDE the permitted set "
                f"{sorted(s.value for s in permitted_stages)} for build_id="
                f"{build_id!r}; refusing dispatch (ADR-ARCH-026 "
                "belt-and-braces enforcement)"
            )
            logger.warning(warning_msg)
            report = TurnReport(
                outcome=TurnOutcome.REFUSED_OUT_OF_BAND,
                build_id=build_id,
                permitted_stages=permitted_stages,
                chosen_stage=choice.stage,
                chosen_feature_id=choice.feature_id,
                rationale=warning_msg,
            )
            self._record_safe(report)
            return report

        # 5. Per-feature sequencer for AUTOBUILD.
        if choice.stage is StageClass.AUTOBUILD:
            if choice.feature_id is None:
                # AUTOBUILD without feature_id is a programming error in
                # the model wiring; we refuse rather than dispatch with
                # unscoped feature_id (same stance the dispatchers take).
                refusal = (
                    "AUTOBUILD chosen without feature_id; refusing rather "
                    "than risk cross-feature dispatch attribution"
                )
                logger.warning(
                    "supervisor.next_turn: %s build_id=%s",
                    refusal,
                    build_id,
                )
                report = TurnReport(
                    outcome=TurnOutcome.REFUSED_OUT_OF_BAND,
                    build_id=build_id,
                    permitted_stages=permitted_stages,
                    chosen_stage=choice.stage,
                    chosen_feature_id=None,
                    rationale=refusal,
                )
                self._record_safe(report)
                return report

            may_dispatch = self.per_feature_sequencer.may_start_autobuild(
                build_id=build_id,
                feature_id=choice.feature_id,
                stage_log_reader=self.per_feature_stage_log_reader,
                async_task_reader=self.async_task_reader,
            )
            if not may_dispatch:
                report = TurnReport(
                    outcome=TurnOutcome.WAITING_PRIOR_AUTOBUILD,
                    build_id=build_id,
                    permitted_stages=permitted_stages,
                    chosen_stage=choice.stage,
                    chosen_feature_id=choice.feature_id,
                    rationale=(
                        "per-feature sequencer refused: a sibling feature's "
                        "autobuild is still in flight on this build "
                        "(FEAT-FORGE-007 ASSUM-006)"
                    ),
                )
                self._record_safe(report)
                return report

        # 6. Constitutional guard for PULL_REQUEST_REVIEW.
        gate_decision: AutoApproveDecision | None = None
        if choice.stage is StageClass.PULL_REQUEST_REVIEW and choice.auto_approve:
            gate_decision = self.constitutional_guard.veto_auto_approve(choice.stage)
            if gate_decision.verdict is AutoApproveVerdict.REFUSED:
                logger.warning(
                    "supervisor.next_turn: constitutional veto on auto-approve "
                    "for stage=%s build_id=%s — %s",
                    choice.stage.value,
                    build_id,
                    gate_decision.rationale,
                )
                report = TurnReport(
                    outcome=TurnOutcome.REFUSED_CONSTITUTIONAL,
                    build_id=build_id,
                    permitted_stages=permitted_stages,
                    chosen_stage=choice.stage,
                    chosen_feature_id=choice.feature_id,
                    rationale=gate_decision.rationale,
                    gate_decision=gate_decision,
                )
                self._record_safe(report)
                return report

        # 7. Route to the dispatcher.
        dispatch_result = await self._dispatch(build_id=build_id, choice=choice)

        # 8. Successful dispatch — assemble the report. For every stage
        #    except the merge-ready checkpoint this is DISPATCHED verbatim;
        #    a red-gate checkpoint result maps to WAITING / TERMINAL so a
        #    refused delivery is never recorded as a dispatch (§c.3).
        report = TurnReport(
            outcome=self._merge_card_turn_outcome(dispatch_result),
            build_id=build_id,
            permitted_stages=permitted_stages,
            chosen_stage=choice.stage,
            chosen_feature_id=choice.feature_id,
            rationale=choice.rationale or self._default_rationale(choice),
            dispatch_result=dispatch_result,
            gate_decision=gate_decision,
        )
        self._record_safe(report)
        return report

    # ------------------------------------------------------------------
    # Internal: Mode B dispatch (TASK-MBC8-008)
    # ------------------------------------------------------------------

    async def _next_turn_mode_b(
        self,
        *,
        build_id: str,
        build_state: BuildState,
    ) -> TurnReport:
        """Mode B dispatch turn (FEAT-FORGE-008 ASSUM-001).

        Sequence:

        1. Read the build's Mode B history via the injected reader.
        2. Ask the :class:`ModeBChainPlanner` for the next stage.
           - ``next_stage = None`` after an approved AUTOBUILD →
             evaluate :func:`evaluate_post_autobuild` to choose between
             :data:`MODE_B_PR_REVIEW` (advance to constitutional gate),
             :data:`MODE_B_NO_OP` (no-diff terminal complete), or
             :data:`MODE_B_ROUTE_FAILED` (autobuild hard-stop / failed).
           - ``next_stage = None`` for any other reason (hard-stop,
             empty-spec, awaiting approval) → record the planner's
             rationale and return :attr:`TurnOutcome.WAITING`.
        3. Belt-and-braces re-check via :class:`StageOrderingGuard` with
           :data:`MODE_B_PREREQUISITES` and the Mode B chain. The
           planner's choice MUST land in the resulting permitted set —
           anything else is a planner bug and yields
           :attr:`TurnOutcome.REFUSED_OUT_OF_BAND`.
        4. Per-feature autobuild sequencer fires for AUTOBUILD.
        5. Constitutional guard fires for ``PULL_REQUEST_REVIEW``.
        6. Route to the dispatcher: subprocess for FEATURE_SPEC /
           FEATURE_PLAN, autobuild_async for AUTOBUILD, PR-review gate
           for PULL_REQUEST_REVIEW.
        """
        if self.mode_b_planner is None or self.mode_b_history_reader is None:
            return self._mode_misconfigured(
                build_id=build_id,
                mode=BuildMode.MODE_B,
                missing=(
                    "mode_b_planner / mode_b_history_reader required for "
                    "MODE_B dispatch (TASK-MBC8-008)"
                ),
            )

        history = self.mode_b_history_reader.get_mode_b_history(build_id)
        from forge.lifecycle.persistence import Build  # local: break import cycle

        build = Build(
            build_id=build_id,
            status=build_state,
            mode=BuildMode.MODE_B,
        )
        plan = self.mode_b_planner.plan_next_stage(build, history)

        # next_stage = None — terminal evaluation or awaiting prerequisites.
        if plan.next_stage is None:
            return await self._mode_b_resolve_no_advance(
                build_id=build_id,
                build=build,
                plan=plan,
                history=history,
            )

        # Belt-and-braces ordering check with Mode B prerequisites.
        permitted = frozenset(
            self.ordering_guard.next_dispatchable(
                build_id,
                self.ordering_stage_log_reader,
                prerequisites=MODE_B_PREREQUISITES,
                stages=MODE_B_CHAIN,
            )
        )
        chosen_stage = plan.next_stage
        chosen_feature_id = self._mode_feature_id_for(build_id)
        if chosen_stage not in permitted:
            warning = (
                "supervisor.next_turn (MODE_B): planner chose stage "
                f"{chosen_stage.value!r} which is OUTSIDE the per-mode "
                f"permitted set {sorted(s.value for s in permitted)} "
                f"for build_id={build_id!r}; refusing dispatch "
                "(ADR-ARCH-026 belt-and-braces enforcement)"
            )
            logger.warning(warning)
            report = TurnReport(
                outcome=TurnOutcome.REFUSED_OUT_OF_BAND,
                build_id=build_id,
                permitted_stages=permitted,
                chosen_stage=chosen_stage,
                chosen_feature_id=chosen_feature_id,
                rationale=warning,
            )
            self._record_safe(report)
            return report

        # Per-feature sequencer for AUTOBUILD (Mode B is single-feature
        # by ASSUM-006 but the sequencer enforces the same invariant).
        if chosen_stage is StageClass.AUTOBUILD:
            if chosen_feature_id is None:
                refusal = "MODE_B AUTOBUILD chosen without feature_id; refusing"
                logger.warning(
                    "supervisor.next_turn (MODE_B): %s build_id=%s",
                    refusal,
                    build_id,
                )
                report = TurnReport(
                    outcome=TurnOutcome.REFUSED_OUT_OF_BAND,
                    build_id=build_id,
                    permitted_stages=permitted,
                    chosen_stage=chosen_stage,
                    chosen_feature_id=None,
                    rationale=refusal,
                )
                self._record_safe(report)
                return report

            may_dispatch = self.per_feature_sequencer.may_start_autobuild(
                build_id=build_id,
                feature_id=chosen_feature_id,
                stage_log_reader=self.per_feature_stage_log_reader,
                async_task_reader=self.async_task_reader,
            )
            if not may_dispatch:
                report = TurnReport(
                    outcome=TurnOutcome.WAITING_PRIOR_AUTOBUILD,
                    build_id=build_id,
                    permitted_stages=permitted,
                    chosen_stage=chosen_stage,
                    chosen_feature_id=chosen_feature_id,
                    rationale=(
                        "per-feature sequencer refused (MODE_B): a sibling "
                        "autobuild is still in flight"
                    ),
                )
                self._record_safe(report)
                return report

        # Route to the dispatcher.
        rationale = plan.rationale or f"MODE_B planner chose {chosen_stage.value}"
        choice = DispatchChoice(
            stage=chosen_stage,
            feature_id=chosen_feature_id,
            rationale=rationale,
        )
        dispatch_result = await self._dispatch(build_id=build_id, choice=choice)
        report = TurnReport(
            outcome=TurnOutcome.DISPATCHED,
            build_id=build_id,
            permitted_stages=permitted,
            chosen_stage=chosen_stage,
            chosen_feature_id=chosen_feature_id,
            rationale=rationale,
            dispatch_result=dispatch_result,
        )
        self._record_safe(report)
        return report

    async def _mode_b_resolve_no_advance(
        self,
        *,
        build_id: str,
        build: Build,
        plan: ModeBPlan,
        history: Sequence[ModeBStageEntry],
    ) -> TurnReport:
        """Resolve a Mode B plan whose ``next_stage`` is ``None``.

        If the latest AUTOBUILD entry has reached a terminal lifecycle
        we evaluate :func:`evaluate_post_autobuild` to decide between
        the constitutional PR-review gate, the no-diff NO_OP terminal,
        or the failed terminal. For every other reason
        (``hard_stop`` on FEATURE_SPEC, empty-spec artefacts, awaiting
        approval, etc.) we surface the planner's rationale and return
        :attr:`TurnOutcome.WAITING`.
        """
        # Detect a terminal AUTOBUILD entry — only then is the
        # post-autobuild handler valid.
        latest_autobuild = next(
            (
                entry
                for entry in reversed(list(history))
                if entry.stage is StageClass.AUTOBUILD
            ),
            None,
        )
        if latest_autobuild is None:
            # Hard-stop / empty-spec / awaiting-approval — record the
            # planner's rationale and wait. Group C "hard-stop in any
            # non-constitutional stage transitions to a failed terminal
            # state" is enforced by the lifecycle state machine; the
            # supervisor records WAITING here so the outer loop can
            # observe the rationale.
            report = TurnReport(
                outcome=TurnOutcome.WAITING,
                build_id=build_id,
                permitted_stages=frozenset(MODE_B_CHAIN),
                rationale=(
                    plan.rationale or "MODE_B planner returned no next_stage; awaiting"
                ),
            )
            self._record_safe(report)
            return report

        # Local import — see TYPE_CHECKING note at top of module.
        from forge.pipeline.terminal_handlers import (
            NO_OP as _MODE_B_NO_OP,
            PR_REVIEW as _MODE_B_PR_REVIEW,
            ROUTE_FAILED as _MODE_B_ROUTE_FAILED,
            evaluate_post_autobuild as _default_mode_b_post_autobuild,
        )

        handler = self.mode_b_post_autobuild or _default_mode_b_post_autobuild
        decision = handler(build, history)

        rationale = decision.rationale
        permitted = frozenset(MODE_B_CHAIN)
        feature_id = decision.feature_id

        if decision.route == _MODE_B_PR_REVIEW:
            # The merge-ready checkpoint (design pass §c) — call site 2 of
            # 4. Mode B's planner is authoritative; auto_approve is False
            # (the owner says the merge word) by ASSUM-011.
            dispatch_result = await self._submit_merge_ready_checkpoint(
                build_id=build_id,
                feature_id=feature_id or "",
                auto_approve=False,
                rationale=rationale,
            )
            report = TurnReport(
                outcome=self._merge_card_turn_outcome(dispatch_result),
                build_id=build_id,
                permitted_stages=permitted,
                chosen_stage=StageClass.PULL_REQUEST_REVIEW,
                chosen_feature_id=feature_id,
                rationale=rationale,
                dispatch_result=dispatch_result,
            )
            self._record_safe(report)
            return report

        if decision.route in (_MODE_B_NO_OP, _MODE_B_ROUTE_FAILED):
            # Terminal — supervisor records and stops dispatching.
            report = TurnReport(
                outcome=TurnOutcome.TERMINAL,
                build_id=build_id,
                permitted_stages=permitted,
                chosen_stage=StageClass.AUTOBUILD,
                chosen_feature_id=feature_id,
                rationale=rationale,
                dispatch_result=decision,
            )
            self._record_safe(report)
            return report

        # Defensive — unknown route surfaces as WAITING with a warning.
        logger.warning(
            "supervisor.next_turn (MODE_B): unknown post-autobuild route "
            "%r for build_id=%s; recording WAITING",
            decision.route,
            build_id,
        )
        report = TurnReport(
            outcome=TurnOutcome.WAITING,
            build_id=build_id,
            permitted_stages=permitted,
            chosen_feature_id=feature_id,
            rationale=rationale,
        )
        self._record_safe(report)
        return report

    def _mode_feature_id_for(self, build_id: str) -> str | None:
        """Resolve the single feature_id for the build's per-feature stages.

        Mode B is single-feature by ASSUM-006 — the catalogue has exactly
        one entry. We read it from the ordering reader so the supervisor
        does not duplicate the feature-id derivation logic that lives in
        the FEAT-FORGE-001 SQLite adapter. ``None`` means the catalogue
        is not (yet) populated; callers treat that as a misuse.
        """
        try:
            features = list(self.ordering_stage_log_reader.feature_catalogue(build_id))
        except Exception:  # noqa: BLE001 — defensive read
            return None
        if not features:
            return None
        return features[0]

    # ------------------------------------------------------------------
    # Internal: Mode C dispatch (TASK-MBC8-008)
    # ------------------------------------------------------------------

    async def _next_turn_mode_c(
        self,
        *,
        build_id: str,
        build_state: BuildState,
    ) -> TurnReport:
        """Mode C dispatch turn (FEAT-FORGE-008 ASSUM-004).

        Sequence:

        1. Read the build's Mode C history and ``has_commits`` flag via
           the injected reader.
        2. Ask the :class:`ModeCCyclePlanner` for the next stage. The
           planner returns:
           - ``next_stage = TASK_REVIEW`` for the initial review or a
             follow-up review.
           - ``next_stage = TASK_WORK`` plus a :class:`FixTaskRef` for
             each fix-task dispatch.
           - ``next_stage = PULL_REQUEST_REVIEW`` when a follow-up
             review is clean and ``has_commits`` is true.
           - ``next_stage = None`` for terminal cycle exit (clean
             review with no commits, or hard-stopped review). The
             :func:`evaluate_terminal` handler decides between
             :data:`ModeCHandlerTerminal.PR_REVIEW`,
             :data:`ModeCHandlerTerminal.CLEAN_REVIEW_NO_FIXES`,
             :data:`ModeCHandlerTerminal.CLEAN_REVIEW_NO_COMMITS`, or
             :data:`ModeCHandlerTerminal.FAILED`.
        3. Belt-and-braces re-check via :class:`StageOrderingGuard` with
           :data:`MODE_C_PREREQUISITES` and the Mode C chain.
        4. Constitutional guard fires for ``PULL_REQUEST_REVIEW``.
        5. Route to ``subprocess_dispatcher`` for TASK_REVIEW /
           TASK_WORK; ``pr_review_gate`` for PULL_REQUEST_REVIEW.
        """
        if self.mode_c_planner is None or self.mode_c_history_reader is None:
            return self._mode_misconfigured(
                build_id=build_id,
                mode=BuildMode.MODE_C,
                missing=(
                    "mode_c_planner / mode_c_history_reader required for "
                    "MODE_C dispatch (TASK-MBC8-008)"
                ),
            )

        history = self.mode_c_history_reader.get_mode_c_history(build_id)
        has_commits = self.mode_c_history_reader.has_commits(build_id)
        from forge.lifecycle.persistence import Build  # local: break import cycle

        build = Build(
            build_id=build_id,
            status=build_state,
            mode=BuildMode.MODE_C,
        )
        plan = self.mode_c_planner.plan_next_stage(
            build, history, has_commits=has_commits
        )

        if plan.is_waiting:
            # The planner is waiting on an in-flight prerequisite — the
            # review is not yet approved, or a /task-work is still
            # running (design pass §h.1). A wait is NOT a terminal: the
            # terminal handler reads history structurally and would
            # classify a mid-cycle build as CLEAN_REVIEW_NO_COMMITS or
            # PR_REVIEW, ending a build that has work still in flight.
            # Report the wait and let the driver loop re-plan when the
            # in-flight stage resolves.
            rationale = plan.rationale or (
                f"MODE_C planner waiting ({plan.wait})"
            )
            logger.info(
                "supervisor.next_turn (MODE_C): waiting — %s (build_id=%s)",
                rationale,
                build_id,
            )
            report = TurnReport(
                outcome=TurnOutcome.WAITING,
                build_id=build_id,
                permitted_stages=frozenset(MODE_C_CHAIN),
                rationale=rationale,
            )
            self._record_safe(report)
            return report

        if plan.next_stage is None:
            return await self._mode_c_resolve_terminal(
                build_id=build_id,
                build=build,
                plan=plan,
                history=history,
            )

        permitted = frozenset(
            self.ordering_guard.next_dispatchable(
                build_id,
                self.ordering_stage_log_reader,
                prerequisites=MODE_C_PREREQUISITES,
                stages=MODE_C_CHAIN,
            )
        )
        chosen_stage = plan.next_stage
        if chosen_stage not in permitted:
            warning = (
                "supervisor.next_turn (MODE_C): planner chose stage "
                f"{chosen_stage.value!r} which is OUTSIDE the per-mode "
                f"permitted set {sorted(s.value for s in permitted)} "
                f"for build_id={build_id!r}; refusing dispatch "
                "(ADR-ARCH-026 belt-and-braces enforcement)"
            )
            logger.warning(warning)
            report = TurnReport(
                outcome=TurnOutcome.REFUSED_OUT_OF_BAND,
                build_id=build_id,
                permitted_stages=permitted,
                chosen_stage=chosen_stage,
                rationale=warning,
            )
            self._record_safe(report)
            return report

        rationale = plan.rationale or f"MODE_C planner chose {chosen_stage.value}"

        if chosen_stage is StageClass.PULL_REQUEST_REVIEW:
            # The merge-ready checkpoint (design pass §c) — call site 3 of
            # 4. A clean follow-up review with commits advances straight
            # to it. The constitutional guard veto applies but only on
            # auto-approve, which the planner does not request.
            dispatch_result = await self._submit_merge_ready_checkpoint(
                build_id=build_id,
                feature_id="",
                auto_approve=False,
                rationale=rationale,
            )
            report = TurnReport(
                outcome=self._merge_card_turn_outcome(dispatch_result),
                build_id=build_id,
                permitted_stages=permitted,
                chosen_stage=chosen_stage,
                rationale=rationale,
                dispatch_result=dispatch_result,
            )
            self._record_safe(report)
            return report

        # FEAT-UBS-002 — unattended budget-guard enforcement. Evaluated at
        # the cyclic TASK_REVIEW step (the follow-up review the planner just
        # chose): if an unattended profile has breached a cap, refuse the
        # dispatch, escalate a risk=high approval, and pause instead of
        # spending another cycle. A no-op for attended / unwired profiles.
        if chosen_stage is StageClass.TASK_REVIEW:
            budget_report = await self._enforce_mode_c_budget(
                build_id=build_id,
                build_state=build_state,
                history=history,
                permitted=permitted,
            )
            if budget_report is not None:
                return budget_report

        # TASK_REVIEW / TASK_WORK route through the subprocess dispatcher.
        # TASK_WORK carries the fix-task ref; the
        # :class:`ForwardContextBuilder` (TASK-MBC8-005) is consulted
        # via the injected ``fix_task_context_builder`` callable so the
        # supervisor stays decoupled from the builder's read-side I/O.
        dispatcher_kwargs: dict[str, Any] = {
            "stage": chosen_stage,
            "build_id": build_id,
            "feature_id": None,
            "rationale": rationale,
        }
        # BOTH Mode C stages carry forward context (shadow-replay item 4).
        #
        # Only TASK_WORK asked for it before, which left
        # ``ForwardContextBuilder._build_for_mode_c``'s TASK_REVIEW branch
        # — "consume every approved /task-work row", the AC-005 contract —
        # as unreachable code. The consequence was not cosmetic: the
        # FOLLOW-UP review, the one that decides whether the fixes landed,
        # was dispatched blind to the very work it is reviewing. It saw
        # the worktree and nothing else.
        #
        # The fix-task ref is the ONLY difference between the two calls:
        # TASK_WORK threads the ref it is about to work on, TASK_REVIEW
        # has no single subject (its subject is *all* the completed fix
        # work) and passes ``None``. The builder already reads the rows
        # itself for that branch.
        wants_forward_context = chosen_stage in (
            StageClass.TASK_WORK,
            StageClass.TASK_REVIEW,
        )
        fix_task_for_context: Any | None = None
        if chosen_stage is StageClass.TASK_WORK and plan.next_fix_task is not None:
            fix_task_for_context = plan.next_fix_task
            dispatcher_kwargs["fix_task"] = plan.next_fix_task
        elif chosen_stage is StageClass.TASK_WORK:
            # TASK_WORK with no ref: nothing to build context around, and
            # the builder's own guard says so. Unchanged behaviour.
            wants_forward_context = False

        if wants_forward_context and self.fix_task_context_builder is not None:
            try:
                dispatcher_kwargs["forward_context"] = self.fix_task_context_builder(
                    chosen_stage,
                    build_id,
                    fix_task_for_context,
                )
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.warning(
                    "supervisor.next_turn (MODE_C): "
                    "fix_task_context_builder raised %s: %s for "
                    "build_id=%s stage=%s — dispatching without "
                    "forward_context",
                    type(exc).__name__,
                    exc,
                    build_id,
                    getattr(chosen_stage, "value", chosen_stage),
                )

        dispatch_result = await self.subprocess_dispatcher(**dispatcher_kwargs)
        report = TurnReport(
            outcome=TurnOutcome.DISPATCHED,
            build_id=build_id,
            permitted_stages=permitted,
            chosen_stage=chosen_stage,
            rationale=rationale,
            dispatch_result=dispatch_result,
        )
        self._record_safe(report)
        return report

    async def _enforce_mode_c_budget(
        self,
        *,
        build_id: str,
        build_state: BuildState,
        history: "Sequence[ModeCStageEntry]",
        permitted: frozenset[StageClass],
    ) -> "TurnReport | None":
        """Evaluate the unattended budget guard (FEAT-UBS-002).

        Returns a :class:`TurnReport` with outcome ``PAUSED_BUDGET`` when a
        cap is breached (dispatch refused, escalation published, build
        paused), or ``None`` when the build may proceed. A strict no-op for
        an unwired / attended (caps-off) profile so ModeCCyclePlanner
        behaviour is byte-for-byte unchanged (ASSUM-010).
        """
        guards = self.budget_guards
        if guards is None or not guards.caps_enabled:
            # THE NO-OP GOES LOUD (leg-invocation stage-2 design §4). This
            # branch used to say nothing at all, which is how the runaway
            # crossing looked exactly like an enforced build: a mode-c
            # journey resolved a profile whose every cap was None, the
            # guard returned None on every cycle, and the logs recorded a
            # guard that was never once enforcing anything. The daemon's
            # cap-law belt now refuses to open such a journey; this line
            # is the brace for every other path that constructs a
            # Supervisor directly (sandbox drives, tests, an operator
            # wiring one by hand).
            logger.warning(
                "supervisor.next_turn (MODE_C): the budget guard was "
                "consulted for build_id=%s with %s — NOTHING IS ENFORCED on "
                "this fix journey. No review-cycle cap, no wall-clock cap, "
                "no stop of any kind comes from the budget guard; only the "
                "planner's own terminals can end it. An uncapped fix "
                "journey is the runaway shape (stage-2 design §4)",
                build_id,
                (
                    "no budget guards wired at all"
                    if guards is None
                    else f"profile {self.budget_profile_name!r} carrying no caps"
                ),
            )
            return None

        # Idempotency: an already-PAUSED build has already been escalated —
        # re-evaluating (e.g. a caller re-runs next_turn before observing the
        # pause) must NOT re-fire ``budget_pause`` (same deterministic
        # request_id) AND must NOT dispatch the next cycle. Park it: return
        # WAITING so the outer loop stays put until an approval resumes the
        # build (merge-review hardening — the future daemon driver must not
        # re-dispatch a paused build regardless).
        if build_state is BuildState.PAUSED:
            report = TurnReport(
                outcome=TurnOutcome.WAITING,
                build_id=build_id,
                permitted_stages=permitted,
                chosen_stage=StageClass.TASK_REVIEW,
                rationale="a fix-journey build is already paused; the budget guard parked it",
            )
            self._record_safe(report)
            return report

        from forge.pipeline.budget_guard import (
            BuildBudgetMetrics,
            build_budget_breach_approval_payload,
            count_review_cycles,
            evaluate_budget,
        )

        review_cycles = count_review_cycles(
            history,
            is_review=lambda e: e.stage_class == StageClass.TASK_REVIEW,
        )
        metrics = BuildBudgetMetrics(
            review_cycles=review_cycles,
            elapsed_wallclock_seconds=self._budget_elapsed_seconds(build_id),
            # tokens stay unmeasured today (ADR-ARCH-033); its cap is inert
            # until the runner populates a real value.
            tokens_used=None,
            # coach-score floor (AC-05): a wired reader supplies the score, so
            # the min_coach_score branch activates automatically. No reader (the
            # attended / streaming-path default) leaves it None → floor inert.
            last_coach_score=self._budget_last_coach_score(build_id),
        )
        verdict = evaluate_budget(guards, metrics)
        if verdict.ok:
            return None

        feature_id = self._mode_feature_id_for(build_id) or ""
        # Deterministic per-breach request_id (review_cycles disambiguates a
        # wall-clock re-breach on a later cycle).
        request_id = f"budget-{build_id}-{review_cycles}"
        payload = build_budget_breach_approval_payload(
            request_id=request_id,
            build_id=build_id,
            feature_id=feature_id,
            profile_name=self.budget_profile_name,
            verdict=verdict,
            metrics=metrics,
        )
        logger.warning(
            "supervisor.next_turn (MODE_C): budget guard breach for "
            "build_id=%s profile=%s cap=%s (%s); refusing dispatch and "
            "escalating a risk=high approval (UBS-002)",
            build_id,
            self.budget_profile_name,
            verdict.breached_cap,
            verdict.detail,
        )
        if self.budget_pause is not None:
            # Publishes the risk=high approval + pauses (SQLite PAUSED +
            # emit_paused_then_interrupt). Never silent — a pause failure
            # still returns PAUSED_BUDGET so the outer loop stops dispatching.
            await self.budget_pause(
                build_id=build_id,
                feature_id=feature_id,
                payload=payload,
                verdict=verdict,
                metrics=metrics,
            )
        else:
            logger.warning(
                "supervisor.next_turn (MODE_C): budget breach for build_id=%s "
                "but no budget_pause collaborator wired — enforcing by "
                "refusing the dispatch only (no escalation published)",
                build_id,
            )

        report = TurnReport(
            outcome=TurnOutcome.PAUSED_BUDGET,
            build_id=build_id,
            permitted_stages=permitted,
            chosen_stage=StageClass.TASK_REVIEW,
            chosen_feature_id=feature_id or None,
            rationale=verdict.detail,
        )
        self._record_safe(report)
        return report

    def _budget_elapsed_seconds(self, build_id: str) -> float:
        """Wall-clock consumed by the build, or ``0.0`` when unmeasurable.

        Requires BOTH an injected wall-clock and a start-time reader; absent
        either, the wall-clock cap is unenforceable (0.0 → never a breach),
        never a false pause.
        """
        if self.budget_wall_clock is None or self.budget_started_at_reader is None:
            return 0.0
        started = self.budget_started_at_reader(build_id)
        if started is None:
            return 0.0
        return max(0.0, (self.budget_wall_clock() - started).total_seconds())

    def _budget_last_coach_score(self, build_id: str) -> float | None:
        """Most recent Coach score for the build, or ``None`` (AC-05).

        Returns ``None`` when no reader is wired (the attended / streaming-path
        default) so the ``min_coach_score`` floor stays inert and the guard is
        byte-identical to the pre-reader path (AC-03). A reader failure degrades
        to ``None`` with a loud log — enforcement continues on the other caps
        rather than crashing (mirrors the ``build_mode_reader`` safe-default
        shape).
        """
        if self.budget_coach_score_reader is None:
            return None
        try:
            return self.budget_coach_score_reader(build_id)
        except Exception as exc:  # noqa: BLE001 — defensive: never crash enforcement
            logger.error(
                "supervisor.next_turn (MODE_C): budget_coach_score_reader "
                "raised %s: %s for build_id=%s; treating coach score as None "
                "(floor inert this turn, other caps still enforced) (UBS-002)",
                type(exc).__name__,
                exc,
                build_id,
            )
            return None

    async def _mode_c_resolve_terminal(
        self,
        *,
        build_id: str,
        build: Build,
        plan: ModeCPlan,
        history: Sequence[ModeCStageEntry],
    ) -> TurnReport:
        """Resolve a Mode C plan whose ``next_stage`` is ``None``.

        Invokes the Mode C terminal handler to classify the cycle's
        exit (CLEAN_REVIEW_NO_FIXES / CLEAN_REVIEW_NO_COMMITS /
        PR_REVIEW / FAILED) and converts the decision into a
        :class:`TurnReport`. PR_REVIEW yields a DISPATCHED outcome via
        the constitutional gate; every other outcome is TERMINAL.
        """
        permitted = frozenset(MODE_C_CHAIN)

        # Local import — see TYPE_CHECKING note at top of module.
        from forge.pipeline.terminal_handlers.mode_c import (
            ModeCTerminal as _ModeCHandlerTerminal,
            evaluate_terminal as _default_mode_c_evaluate_terminal,
        )

        # If the planner already classified the cycle as a planner-side
        # terminal (CLEAN_REVIEW or FAILED), prefer the handler's view
        # since it has access to the commit probe. Fall back to the
        # planner's rationale if the handler is not wired.
        handler = self.mode_c_terminal_handler or _default_mode_c_evaluate_terminal
        try:
            decision = await handler(
                build,
                history,
                commit_probe=self.mode_c_commit_probe,
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "supervisor.next_turn (MODE_C): terminal handler raised "
                "%s: %s for build_id=%s — falling back to planner "
                "rationale",
                type(exc).__name__,
                exc,
                build_id,
            )
            outcome = (
                TurnOutcome.TERMINAL
                if plan.terminal is not None
                else TurnOutcome.WAITING
            )
            report = TurnReport(
                outcome=outcome,
                build_id=build_id,
                permitted_stages=permitted,
                rationale=plan.rationale or "MODE_C planner halted cycle",
            )
            self._record_safe(report)
            return report

        if decision.outcome is _ModeCHandlerTerminal.PR_REVIEW:
            # The merge-ready checkpoint (design pass §c) — call site 4 of
            # 4. The terminal handler has already consulted the commit
            # probe, so reaching here means "there are commits to merge".
            dispatch_result = await self._submit_merge_ready_checkpoint(
                build_id=build_id,
                feature_id="",
                auto_approve=False,
                rationale=decision.rationale,
            )
            report = TurnReport(
                outcome=self._merge_card_turn_outcome(dispatch_result),
                build_id=build_id,
                permitted_stages=permitted,
                chosen_stage=StageClass.PULL_REQUEST_REVIEW,
                rationale=decision.rationale,
                dispatch_result=dispatch_result,
            )
            self._record_safe(report)
            return report

        # CLEAN_REVIEW_NO_FIXES / CLEAN_REVIEW_NO_COMMITS / FAILED → terminal.
        # NO-COMMIT TERMINALS STAY SILENT (design pass §c.6): these three
        # branches never touch ``pr_review_gate`` — a fix journey that
        # finds nothing to fix, or produces no commits, ends with a
        # receipt, never a card. The owner hears about work only when
        # there is a merge word to say.
        report = TurnReport(
            outcome=TurnOutcome.TERMINAL,
            build_id=build_id,
            permitted_stages=permitted,
            rationale=(
                f"{decision.outcome.value}: {decision.rationale}"
                if decision.failure_reason is None
                else (
                    f"{decision.outcome.value}: {decision.rationale} "
                    f"({decision.failure_reason})"
                )
            ),
            dispatch_result=decision,
        )
        self._record_safe(report)
        return report

    def _mode_misconfigured(
        self,
        *,
        build_id: str,
        mode: BuildMode,
        missing: str,
    ) -> TurnReport:
        """Build a structured WAITING report when Mode B/C wiring is missing.

        Failing fast with REFUSED_OUT_OF_BAND would be wrong — the
        misconfiguration is not the model's fault. We log at ERROR so
        operators notice and return WAITING so the outer loop pauses
        rather than spinning.
        """
        warning = (
            f"supervisor.next_turn ({mode.value}): {missing}; "
            f"build_id={build_id!r} — recording WAITING"
        )
        logger.error(warning)
        report = TurnReport(
            outcome=TurnOutcome.WAITING,
            build_id=build_id,
            permitted_stages=frozenset(),
            rationale=warning,
        )
        self._record_safe(report)
        return report

    # ------------------------------------------------------------------
    # Internal: dispatch routing
    # ------------------------------------------------------------------

    async def _dispatch(
        self,
        *,
        build_id: str,
        choice: DispatchChoice,
    ) -> Any:
        """Route ``choice`` to the correct dispatcher.

        The four routing branches mirror the AC table verbatim. Each
        branch invokes the injected dispatcher callable; the supervisor
        does not interpret the result — the gating layer
        (FEAT-FORGE-004) does. We thread ``build_id``, ``feature_id``
        (where applicable), and the model's free-form rationale so the
        downstream artefacts have the per-turn audit context.
        """
        stage = choice.stage

        if stage in self._SPECIALIST_STAGES:
            return await self.specialist_dispatcher(
                stage=stage,
                build_id=build_id,
                feature_id=choice.feature_id,
                rationale=choice.rationale,
            )

        if stage in self._SUBPROCESS_STAGES:
            # Per-feature stages must carry feature_id.
            feature_id = choice.feature_id
            if stage in PER_FEATURE_STAGES and feature_id is None:
                # Same stance the subprocess dispatcher takes: refuse
                # rather than dispatch with unscoped feature_id. We
                # surface this as a dispatch result rather than as
                # WAITING because the executor layer already permitted
                # the stage; the missing feature_id is a contract
                # violation between the model and the supervisor.
                logger.warning(
                    "supervisor._dispatch: per-feature subprocess stage %s "
                    "chosen without feature_id; refusing dispatch "
                    "(build_id=%s)",
                    stage.value,
                    build_id,
                )
                return {
                    "status": "refused",
                    "reason": (
                        f"per-feature subprocess stage {stage.value!r} "
                        "dispatched without feature_id"
                    ),
                }
            return await self.subprocess_dispatcher(
                stage=stage,
                build_id=build_id,
                feature_id=feature_id,
                rationale=choice.rationale,
            )

        if stage is StageClass.AUTOBUILD:
            # feature_id presence is already enforced upstream (step 5
            # of next_turn). TASK-FORGE-FRR-F010G: the autobuild
            # dispatcher is async because the launch path awaits
            # ``StructuredTool.coroutine`` (deepagents async path) so
            # the runner can run URL-less in-process.
            assert choice.feature_id is not None
            return await self.autobuild_dispatcher(
                build_id=build_id,
                feature_id=choice.feature_id,
                rationale=choice.rationale,
            )

        if stage is StageClass.PULL_REQUEST_REVIEW:
            # The merge-ready checkpoint (design pass §c) — call site 1 of
            # 4. It uses the FEAT-FORGE-004 approve-click gate surface,
            # not a dispatcher, and it has never opened a pull request.
            # Auto-approve has already been sanitised by the
            # constitutional guard; we forward whatever flag survived it
            # (the publisher refuses auto-approve again, belt and braces).
            return await self._submit_merge_ready_checkpoint(
                build_id=build_id,
                feature_id=choice.feature_id or "",
                auto_approve=choice.auto_approve,
                rationale=choice.rationale,
            )

        # Unreachable — every StageClass member is covered above.
        # We surface this as a TypeError so a future enum addition
        # without a routing branch fails loudly at runtime.
        raise TypeError(
            f"Supervisor._dispatch: no routing for stage {stage!r}; "
            "this is a bug — every StageClass needs a dispatcher branch"
        )

    # ------------------------------------------------------------------
    # Internal: rationale + recording helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _default_rationale(choice: DispatchChoice) -> str:
        """Return a stock rationale when the model omits one.

        Keeps the audit trail intelligible even if the reasoning model
        forgets to populate :attr:`DispatchChoice.rationale` — which it
        sometimes does on retry paths.
        """
        scope = (
            f" feature_id={choice.feature_id!r}"
            if choice.feature_id is not None
            else ""
        )
        return f"dispatch {choice.stage.value}{scope}"

    def _record_safe(self, report: TurnReport) -> None:
        """Persist ``report`` via :class:`StageLogTurnRecorder`; swallow errors.

        The recorder writing to SQLite must never block a turn from
        returning. We log at ``ERROR`` if the write itself raises and
        continue — the in-memory :class:`TurnReport` is the
        authoritative outcome of this method, the recorder is the
        durable side-effect.
        """
        try:
            verdict = (
                report.gate_decision.verdict.value
                if report.gate_decision is not None
                else None
            )
            self.turn_recorder.record_turn(
                build_id=report.build_id,
                outcome=report.outcome,
                permitted_stages=report.permitted_stages,
                chosen_stage=report.chosen_stage,
                chosen_feature_id=report.chosen_feature_id,
                rationale=report.rationale,
                gate_verdict=verdict,
            )
        except Exception as exc:  # noqa: BLE001 — universal error contract
            logger.error(
                "supervisor.next_turn: turn_recorder raised %s: %s; "
                "build_id=%s outcome=%s — row LOST but report still returned",
                type(exc).__name__,
                exc,
                report.build_id,
                report.outcome.value,
            )

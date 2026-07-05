"""CLI steering injection handler (TASK-MAG7-011, FEAT-FORGE-007).

This module owns :class:`CliSteeringHandler` — the executor-layer surface
the FEAT-FORGE-001 CLI commands (``forge cancel``, ``forge skip``, and the
mid-flight directive command) call into. The handler turns the operator's
CLI verb into the corresponding pause-resolution / async-task-middleware
side effect, observing the constitutional-guard veto rules from
ADR-ARCH-026 and the synthetic-decision mapping declared in
FEAT-FORGE-004 ASSUM-005 (cancel → reject, skip → override).

Why this is the executor layer
------------------------------

The CLI commands themselves (FEAT-FORGE-001) are intentionally thin: they
parse argv, marshal IDs, and dispatch to this handler. Every behavioural
rule — "cancel-during-pause resolves as a synthetic reject", "skip on a
constitutional stage is refused", "mid-flight directives are queued onto
``AutobuildState.pending_directives``" — lives here, in one auditable
module. This mirrors the executor-layer pattern already established by
:class:`~forge.pipeline.constitutional_guard.ConstitutionalGuard` and
:class:`~forge.pipeline.supervisor.Supervisor`: I/O is injected through
narrow :class:`typing.Protocol` seams; the handler owns no mutable
per-build state.

Group coverage
--------------

* **Group D edge cases**:
  - cancel during a flagged-for-review pause → synthetic reject →
    terminal ``CANCELLED`` (AC-002).
  - cancel during an in-flight autobuild → ``cancel_async_task`` is
    called and the build is marked ``CANCELLED`` with no PR creation
    attempted (AC-002).
  - cancel from any other non-terminal state → terminal ``CANCELLED``
    and no further dispatch is permitted (AC-002).
  - skip on a non-constitutional stage → stage logged ``SKIPPED`` and
    the build resumes at the next stage (AC-003 / AC-006).
  - mid-flight directive on an active autobuild → enqueued onto
    ``AutobuildState.pending_directives`` via ``update_async_task``;
    the autobuild itself decides when (and whether) to honour it
    (AC-004).

* **Group C @negative @regression**:
  - skip on the PR-review stage (or any other stage in
    :data:`~forge.pipeline.stage_taxonomy.CONSTITUTIONAL_STAGES`) is
    refused via :meth:`ConstitutionalGuard.veto_skip`; the refusal
    rationale is recorded and the build remains paused (AC-007).

References:
    - TASK-MAG7-011 — this task brief.
    - TASK-MAG7-004 — :class:`ConstitutionalGuard` (skip-veto consumer).
    - TASK-MAG7-009 — :func:`dispatch_autobuild_async` (the autobuild
      ``task_id`` we cancel / inject directives into).
    - TASK-MAG7-010 — supervisor pause-resolution surface
      (``resolve_as_reject`` is conceptually the same hook the
      FEAT-FORGE-004 gate uses for human-supplied reject decisions).
    - FEAT-FORGE-001 — owns the CLI commands that call into this
      handler.
    - FEAT-FORGE-004 ASSUM-005 — synthetic-decision mapping
      (cancel → reject, skip → override).
    - ADR-ARCH-026 — constitutional-rules belt-and-braces.
    - DDR-006 — ``AutobuildState.pending_directives`` semantics.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from forge.pipeline.constitutional_guard import (
    ConstitutionalGuard,
    SkipDecision,
    SkipVerdict,
)
from forge.pipeline.stage_taxonomy import StageClass

logger = logging.getLogger(__name__)


__all__ = [
    "CANCEL_REJECT_RATIONALE",
    "CANCEL_AUTOBUILD_RATIONALE",
    "CANCEL_DIRECT_RATIONALE",
    "CANCEL_NOOP_TERMINAL_RATIONALE",
    "DIRECTIVE_NO_AUTOBUILD_RATIONALE",
    "DIRECTIVE_QUEUED_RATIONALE",
    "SKIP_REFUSED_RATIONALE",
    "SKIP_RECORDED_RATIONALE",
    "AsyncTaskCanceller",
    "AsyncTaskUpdater",
    "BuildCanceller",
    "BuildLifecycle",
    "BuildResumer",
    "BuildSnapshot",
    "BuildSnapshotReader",
    "CancelOutcome",
    "CancelStatus",
    "CancelledNotifier",
    "CliSteeringHandler",
    "DEFAULT_CLI_CANCELLED_BY",
    "DEFAULT_CLI_CANCEL_REASON",
    "DirectiveOutcome",
    "DirectiveStatus",
    "PauseRejectResolver",
    "SkipOutcome",
    "SkipStatus",
    "StageSkipRecorder",
]


# ---------------------------------------------------------------------------
# Status alphabets — closed enums so callers cannot smuggle ad-hoc strings
# through the outcome shapes. ``StrEnum`` so values round-trip through JSON
# and the per-turn ``stage_log`` row without a translation table.
# ---------------------------------------------------------------------------


class BuildLifecycle(StrEnum):
    """Coarse build lifecycle states the handler branches on.

    Mirrors the union of FEAT-FORGE-001's ``Build.state`` and the
    DDR-006 ``AutobuildState.lifecycle`` — but flattened to the four
    cases :meth:`CliSteeringHandler.handle_cancel` actually has to
    choose between. The :class:`BuildSnapshotReader` Protocol is what
    maps a concrete persistence read to one of these four members.

    Members:
        PAUSED_AT_GATE: The build is paused at a flagged-for-review
            checkpoint (FEAT-FORGE-004). The handler resolves cancel as
            a synthetic reject (FEAT-FORGE-004 ASSUM-005).
        AUTOBUILD_RUNNING: An ``AsyncSubAgent`` autobuild is active for
            this build. The handler calls ``cancel_async_task`` and
            marks the build ``CANCELLED`` with no PR attempt.
        OTHER_RUNNING: Any other non-terminal state (preparing,
            running between stages, finalising, awaiting an approval
            response, etc.). The handler marks the build ``CANCELLED``
            and refuses to dispatch any further stage (AC-002 final
            branch).
        TERMINAL: The build is already in ``COMPLETE`` / ``FAILED`` /
            ``CANCELLED``. The handler reports a no-op rather than
            re-cancelling.
    """

    PAUSED_AT_GATE = "paused_at_gate"
    AUTOBUILD_RUNNING = "autobuild_running"
    OTHER_RUNNING = "other_running"
    TERMINAL = "terminal"


class CancelStatus(StrEnum):
    """Verdicts returned by :meth:`CliSteeringHandler.handle_cancel`.

    Members:
        CANCELLED_VIA_PAUSE_REJECT: Build was paused at a flagged-for-review
            checkpoint; resolved as synthetic reject (FEAT-FORGE-004
            ASSUM-005) → terminal ``CANCELLED``.
        CANCELLED_VIA_AUTOBUILD: Build had an in-flight autobuild;
            ``cancel_async_task`` was invoked and the build was marked
            terminal ``CANCELLED`` with **no** PR-creation attempted.
        CANCELLED_DIRECT: Build was in some other non-terminal state;
            the handler transitioned it directly to terminal
            ``CANCELLED`` and refuses to dispatch any further stage.
        NOOP_ALREADY_TERMINAL: Build is already terminal; the cancel
            was a no-op.
    """

    CANCELLED_VIA_PAUSE_REJECT = "cancelled_via_pause_reject"
    CANCELLED_VIA_AUTOBUILD = "cancelled_via_autobuild"
    CANCELLED_DIRECT = "cancelled_direct"
    NOOP_ALREADY_TERMINAL = "noop_already_terminal"


class SkipStatus(StrEnum):
    """Verdicts returned by :meth:`CliSteeringHandler.handle_skip`.

    Members:
        SKIPPED: The constitutional guard allowed the skip; the stage
            was recorded ``SKIPPED`` in ``stage_log`` and the build
            resumed at the next stage (AC-006).
        REFUSED_CONSTITUTIONAL: The constitutional guard refused the
            skip (stage was in ``CONSTITUTIONAL_STAGES``); the refusal
            rationale was recorded and the build remained paused
            (AC-007 / Group C @regression).
    """

    SKIPPED = "skipped"
    REFUSED_CONSTITUTIONAL = "refused_constitutional"


class DirectiveStatus(StrEnum):
    """Verdicts returned by :meth:`CliSteeringHandler.handle_directive`.

    Members:
        QUEUED: Directive was appended to ``AutobuildState.pending_directives``
            via ``update_async_task``. The autobuild itself decides when
            (and whether) to honour it.
        NO_ACTIVE_AUTOBUILD: No live autobuild was registered for the
            ``(build_id, feature_id)`` pair; the directive was rejected
            without side effect. CLI surfaces this so the operator can
            retry once an autobuild is dispatched.
    """

    QUEUED = "queued"
    NO_ACTIVE_AUTOBUILD = "no_active_autobuild"


# ---------------------------------------------------------------------------
# Rationale templates — module-level constants so the regression suite
# (and the CLI surface, which echoes them back to the operator) can
# reference the canonical wording rather than reproducing it. Every
# template that flows from a constitutional refusal cites ADR-ARCH-026
# and FEAT-FORGE-004 ASSUM-005 explicitly.
# ---------------------------------------------------------------------------


CANCEL_REJECT_RATIONALE = (
    "FEAT-FORGE-004 ASSUM-005: CLI cancel maps to synthetic REJECT for the "
    "paused gate on stage {stage!s} (feature_id={feature_id!r}); build "
    "{build_id!r} resolved CANCELLED via pause-reject pathway."
)

CANCEL_AUTOBUILD_RATIONALE = (
    "CLI cancel on build {build_id!r} with active autobuild task "
    "{task_id!r} (feature_id={feature_id!r}): cancel_async_task issued, "
    "build CANCELLED with no PR-creation attempted (FEAT-FORGE-007 Group D)."
)

CANCEL_DIRECT_RATIONALE = (
    "CLI cancel on build {build_id!r} (lifecycle={lifecycle!s}): build "
    "transitioned directly to CANCELLED; no further stage dispatch will "
    "be permitted (FEAT-FORGE-007 Group D)."
)

CANCEL_NOOP_TERMINAL_RATIONALE = (
    "CLI cancel on build {build_id!r}: build is already terminal; no-op."
)

SKIP_RECORDED_RATIONALE = (
    "CLI skip on build {build_id!r} stage {stage!s}: ConstitutionalGuard "
    "permitted the skip; stage recorded SKIPPED and build resumed at the "
    "next stage (FEAT-FORGE-004 ASSUM-005 override semantics)."
)

SKIP_REFUSED_RATIONALE = (
    "CLI skip on build {build_id!r} stage {stage!s} REFUSED by "
    "ConstitutionalGuard (ADR-ARCH-026 belt-and-braces): {guard_rationale}"
)

DIRECTIVE_QUEUED_RATIONALE = (
    "CLI directive on build {build_id!r} feature {feature_id!r} (task "
    "{task_id!r}): appended to AutobuildState.pending_directives via "
    "update_async_task. Autobuild will honour at its next directive-poll."
)

DIRECTIVE_NO_AUTOBUILD_RATIONALE = (
    "CLI directive on build {build_id!r} feature {feature_id!r}: no live "
    "autobuild registered for this (build_id, feature_id) pair; directive "
    "REJECTED without side effect."
)


def _annotate_rationale(
    base: str,
    *,
    responder: str | None,
    reason: str | None,
) -> str:
    """Append optional ``responder`` / ``reason`` audit fields to ``base``.

    The CLI wrappers (``forge cancel`` / ``forge skip``) thread the
    invoking operator (``os.getlogin()``) and the operator-supplied
    ``--reason`` text into the structured rationale so the Group E
    "cancelling operator recorded distinctly" invariant holds without
    requiring a parallel audit channel. Both inputs are optional —
    callers that omit them get the unannotated rationale, which is
    what the legacy programmatic call sites already exercise.

    Empty / whitespace-only values are treated as absent so a CLI
    invocation that did not supply ``--reason`` does not pollute the
    rationale with an empty trailing fragment.
    """
    suffix_parts: list[str] = []
    if responder and responder.strip():
        suffix_parts.append(f"responder={responder!r}")
    if reason and reason.strip():
        suffix_parts.append(f"reason={reason!r}")
    if not suffix_parts:
        return base
    return f"{base} [{'; '.join(suffix_parts)}]"


# ---------------------------------------------------------------------------
# Outcome dataclasses — frozen + slotted so callers can compare two
# outcomes by value (idempotency / regression tests) and the CLI cannot
# mutate a returned outcome by accident.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BuildSnapshot:
    """The minimum view of build state the handler needs to branch on.

    Production wires a reader that consults the FEAT-FORGE-001 SQLite
    state machine, the FEAT-FORGE-004 paused-gate registry, and the
    DeepAgents ``async_tasks`` channel and flattens all three into one
    snapshot. Tests construct snapshots directly.

    Attributes:
        build_id: Build the snapshot describes.
        lifecycle: Coarse lifecycle classification used by
            :meth:`CliSteeringHandler.handle_cancel` to pick a branch.
        paused_stage: For :attr:`BuildLifecycle.PAUSED_AT_GATE`, the
            stage that triggered the pause. ``None`` otherwise.
        paused_feature_id: For :attr:`BuildLifecycle.PAUSED_AT_GATE`,
            the feature scope of the pause (per-feature stages only).
            ``None`` for non-per-feature stages and for non-paused
            lifecycles.
        active_autobuild_task_id: For :attr:`BuildLifecycle.AUTOBUILD_RUNNING`,
            the ``task_id`` minted by ``start_async_task`` for the live
            autobuild. ``None`` otherwise.
        active_autobuild_feature_id: For
            :attr:`BuildLifecycle.AUTOBUILD_RUNNING`, the feature whose
            autobuild is in flight. ``None`` otherwise.
    """

    build_id: str
    lifecycle: BuildLifecycle
    paused_stage: StageClass | None = None
    paused_feature_id: str | None = None
    active_autobuild_task_id: str | None = None
    active_autobuild_feature_id: str | None = None


@dataclass(frozen=True, slots=True)
class CancelOutcome:
    """Decision returned by :meth:`CliSteeringHandler.handle_cancel`.

    Attributes:
        build_id: Build the cancel was issued against.
        status: One of the four :class:`CancelStatus` members.
        rationale: Structured string suitable for recording in
            ``stage_log.gate_rationale`` and for echoing back to the
            CLI operator. Cites the controlling specification when the
            cancel went through a synthetic decision path.
        cancelled_task_id: For :attr:`CancelStatus.CANCELLED_VIA_AUTOBUILD`,
            the autobuild ``task_id`` that ``cancel_async_task`` was
            invoked on. ``None`` for every other branch.
        paused_stage: For :attr:`CancelStatus.CANCELLED_VIA_PAUSE_REJECT`,
            the stage the build was paused on. ``None`` otherwise.
        paused_feature_id: For :attr:`CancelStatus.CANCELLED_VIA_PAUSE_REJECT`,
            the feature scope of the pause. ``None`` otherwise.
    """

    build_id: str
    status: CancelStatus
    rationale: str
    cancelled_task_id: str | None = None
    paused_stage: StageClass | None = None
    paused_feature_id: str | None = None

    @property
    def is_terminal(self) -> bool:
        """``True`` iff the build was actually transitioned to terminal CANCELLED."""
        return self.status is not CancelStatus.NOOP_ALREADY_TERMINAL


@dataclass(frozen=True, slots=True)
class SkipOutcome:
    """Decision returned by :meth:`CliSteeringHandler.handle_skip`.

    Attributes:
        build_id: Build the skip was issued against.
        stage: Stage the skip was directed at.
        status: :attr:`SkipStatus.SKIPPED` or
            :attr:`SkipStatus.REFUSED_CONSTITUTIONAL`.
        rationale: Recorded onto ``stage_log.gate_rationale`` regardless
            of outcome.
        guard_decision: The :class:`SkipDecision` the constitutional
            guard returned. The handler always defers to the guard for
            the skip-permitted predicate; this field carries the full
            decision so callers can audit the chain.
    """

    build_id: str
    stage: StageClass
    status: SkipStatus
    rationale: str
    guard_decision: SkipDecision

    @property
    def is_refused(self) -> bool:
        """``True`` iff the constitutional guard refused the skip."""
        return self.status is SkipStatus.REFUSED_CONSTITUTIONAL


@dataclass(frozen=True, slots=True)
class DirectiveOutcome:
    """Decision returned by :meth:`CliSteeringHandler.handle_directive`.

    Attributes:
        build_id: Build the directive was issued against.
        feature_id: Feature scope of the directive.
        status: :attr:`DirectiveStatus.QUEUED` or
            :attr:`DirectiveStatus.NO_ACTIVE_AUTOBUILD`.
        rationale: Structured string echoed back to the CLI operator.
        directive_text: The directive payload (verbatim from the CLI
            invocation). Recorded for the audit trail; the autobuild
            reads it back off ``AutobuildState.pending_directives``.
        task_id: For :attr:`DirectiveStatus.QUEUED`, the autobuild
            ``task_id`` the directive was appended to. ``None``
            otherwise.
    """

    build_id: str
    feature_id: str
    status: DirectiveStatus
    rationale: str
    directive_text: str
    task_id: str | None = None

    @property
    def is_queued(self) -> bool:
        """``True`` iff the directive was successfully enqueued."""
        return self.status is DirectiveStatus.QUEUED


# ---------------------------------------------------------------------------
# Injected Protocols — the only I/O surface the handler is allowed.
#
# Every dependency is a narrow, runtime-checkable Protocol so tests
# inject in-memory fakes and production wires the real adapter
# (FEAT-FORGE-001 SQLite, DeepAgents middleware, FEAT-FORGE-004 gate)
# without the handler ever importing the concrete adapter modules.
# ---------------------------------------------------------------------------


@runtime_checkable
class BuildSnapshotReader(Protocol):
    """Read-side Protocol over the consolidated build-snapshot view.

    The reader is responsible for collapsing the FEAT-FORGE-001 build
    state, the FEAT-FORGE-004 paused-gate registry, and the DeepAgents
    ``async_tasks`` channel into a single :class:`BuildSnapshot` per
    call — keeping the handler's branching logic readable. Production
    typically composes three adapters; tests inject a dataclass that
    just returns the stored snapshot.
    """

    def get_snapshot(
        self, build_id: str
    ) -> BuildSnapshot:  # pragma: no cover - protocol stub
        """Return the consolidated :class:`BuildSnapshot` for ``build_id``."""
        ...


@runtime_checkable
class PauseRejectResolver(Protocol):
    """Synthetic-reject hook for the FEAT-FORGE-004 pause surface.

    When ``forge cancel`` lands while a build is paused at a flagged-for-
    review checkpoint, the handler must resolve the pause as a synthetic
    REJECT (FEAT-FORGE-004 ASSUM-005). The Protocol is the seam over the
    existing pause-resolution code path the gate uses for human-supplied
    reject decisions; this hook is what threads the cancel rationale onto
    the resolution record.
    """

    def resolve_as_reject(
        self,
        build_id: str,
        stage: StageClass,
        feature_id: str | None,
        rationale: str,
    ) -> Any:  # pragma: no cover - protocol stub
        """Resolve the build's pause as a synthetic REJECT decision."""
        ...


@runtime_checkable
class AsyncTaskCanceller(Protocol):
    """Protocol over the DeepAgents ``cancel_async_task`` middleware tool.

    Production wires the ``AsyncSubAgentMiddleware`` ``cancel_async_task``
    hook (per ADR-ARCH-031). Tests inject a recording fake so the cancel
    side effect can be asserted without standing up a LangGraph runtime.
    """

    def cancel_async_task(
        self, task_id: str
    ) -> Any:  # pragma: no cover - protocol stub
        """Cancel the live autobuild ``AsyncSubAgent`` identified by ``task_id``."""
        ...


@runtime_checkable
class AsyncTaskUpdater(Protocol):
    """Protocol over the DeepAgents ``update_async_task`` middleware tool.

    The handler uses ``update_async_task`` to append a CLI directive to
    ``AutobuildState.pending_directives`` (DDR-006). The autobuild
    itself decides when to honour the directive — this Protocol carries
    only the append side effect; nothing about scheduling.
    """

    def update_async_task(
        self,
        task_id: str,
        *,
        append_pending_directive: str,
    ) -> Any:  # pragma: no cover - protocol stub
        """Append ``append_pending_directive`` to the task's ``pending_directives``."""
        ...


@runtime_checkable
class BuildCanceller(Protocol):
    """Protocol over the FEAT-FORGE-001 build-state-machine cancel write.

    The terminal ``CANCELLED`` transition is owned by FEAT-FORGE-001;
    the handler only signals "transition this build to terminal cancel
    with this rationale". The downstream effects (stage_log row,
    ``pipeline.build-cancelled`` publish) are the state machine's job.
    """

    def mark_cancelled(
        self,
        build_id: str,
        rationale: str,
    ) -> Any:  # pragma: no cover - protocol stub
        """Transition ``build_id`` to terminal ``CANCELLED`` with the rationale."""
        ...


@runtime_checkable
class StageSkipRecorder(Protocol):
    """Protocol over the ``stage_log`` writer for skip records.

    Two methods because the two skip outcomes write distinct rows:
    one for permitted skips (``state="skipped"``) and one for refused
    skips (a constitutional-veto record on the same row, leaving the
    pause intact). Production wires the FEAT-FORGE-001 SQLite adapter;
    tests use a recording dataclass.
    """

    def record_skipped(
        self,
        build_id: str,
        stage: StageClass,
        rationale: str,
    ) -> Any:  # pragma: no cover - protocol stub
        """Record ``stage`` as ``SKIPPED`` for the build (permitted-skip path)."""
        ...

    def record_skip_refused(
        self,
        build_id: str,
        stage: StageClass,
        rationale: str,
    ) -> Any:  # pragma: no cover - protocol stub
        """Record a constitutional skip-refusal; build remains paused."""
        ...


@runtime_checkable
class BuildResumer(Protocol):
    """Protocol over the supervisor's resume-after-skip surface.

    After a permitted skip the supervisor must dispatch the next
    eligible stage. The handler does not call the supervisor's
    ``next_turn`` directly (that would re-enter the reasoning loop on
    the CLI thread); it nudges the resume hook and lets the supervisor
    pick up the build on its next outer-loop tick.
    """

    def resume_after_skip(
        self,
        build_id: str,
        skipped_stage: StageClass,
    ) -> Any:  # pragma: no cover - protocol stub
        """Signal that the supervisor should resume the build after a skip."""
        ...


#: Fallback identity/reason stamped on the build-cancelled wire signal
#: when the CLI caller supplies none (TASK-JNB-102). ``responder`` is
#: normally ``os.getlogin()`` from ``forge cancel``; the constant keeps
#: the payload honest for programmatic callers.
DEFAULT_CLI_CANCELLED_BY: str = "forge-cli"
DEFAULT_CLI_CANCEL_REASON: str = "cli cancel"


@runtime_checkable
class CancelledNotifier(Protocol):
    """Best-effort ``pipeline.build-cancelled`` notifier (TASK-JNB-102).

    Implementations own payload enrichment: the handler's
    :class:`BuildSnapshot` carries neither ``correlation_id`` nor (on
    the OTHER_RUNNING branch) ``feature_id``, so the production
    implementation looks the build row up by ``build_id`` and builds
    the :class:`~nats_core.events.BuildCancelledPayload` from it (see
    ``forge.cli.runtime``). Raising is permitted — the handler wraps
    every call in a DDR-007 swallow+log guard.
    """

    def notify_cancelled(  # pragma: no cover - protocol stub
        self,
        *,
        build_id: str,
        reason: str,
        cancelled_by: str,
    ) -> None: ...


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


@dataclass
class CliSteeringHandler:
    """Executor-layer handler for ``forge cancel`` / ``forge skip`` /
    mid-flight ``forge directive`` CLI commands.

    The handler is a *composition* over the FEAT-FORGE-001 build-state
    writers, the FEAT-FORGE-004 pause-resolution surface, the
    DeepAgents async-task middleware, and the
    :class:`ConstitutionalGuard` veto from TASK-MAG7-004. It owns no
    mutable per-build state — every dependency is injected at
    construction and every public method takes ``build_id`` explicitly,
    so the same instance is safe to call from concurrent CLI
    invocations on different builds.

    Attributes:
        snapshot_reader: Reader for the consolidated build snapshot
            (FEAT-FORGE-001 + FEAT-FORGE-004 + DeepAgents async_tasks).
        pause_reject_resolver: FEAT-FORGE-004 pause-as-reject hook.
        async_task_canceller: DeepAgents ``cancel_async_task`` tool.
        async_task_updater: DeepAgents ``update_async_task`` tool.
        build_canceller: FEAT-FORGE-001 terminal-cancel writer.
        skip_recorder: ``stage_log`` writer for SKIPPED /
            SKIP_REFUSED_CONSTITUTIONAL rows.
        build_resumer: Supervisor resume-after-skip nudge.
        constitutional_guard: TASK-MAG7-004 guard. Defaults to a
            ``ConstitutionalGuard()`` constructed against the canonical
            :data:`~forge.pipeline.stage_taxonomy.CONSTITUTIONAL_STAGES`
            so callers can omit it on production wiring; tests inject
            their own guard (often pre-configured with an empty
            constitutional set for the negative-control suite).
    """

    snapshot_reader: BuildSnapshotReader
    pause_reject_resolver: PauseRejectResolver
    async_task_canceller: AsyncTaskCanceller
    async_task_updater: AsyncTaskUpdater
    build_canceller: BuildCanceller
    skip_recorder: StageSkipRecorder
    build_resumer: BuildResumer
    constitutional_guard: ConstitutionalGuard = None  # type: ignore[assignment]
    # TASK-JNB-102 (ASSUM-010 closure): optional best-effort
    # ``pipeline.build-cancelled`` notifier, invoked AFTER
    # ``mark_cancelled`` on each of the three cancel branches (never on
    # the TERMINAL no-op). ``None`` = no emit. The call is guarded by
    # :meth:`_notify_cancelled_best_effort` — a raising notifier logs a
    # WARNING and the SQLite transition stands (DDR-007).
    cancelled_notifier: "CancelledNotifier | None" = None

    def __post_init__(self) -> None:
        # ``ConstitutionalGuard`` is constructed lazily so callers can
        # use the bare default without importing the guard module
        # themselves; tests still inject explicit guards via the
        # constructor kwarg.
        if self.constitutional_guard is None:
            self.constitutional_guard = ConstitutionalGuard()

    def _notify_cancelled_best_effort(
        self,
        *,
        build_id: str,
        reason: str | None,
        responder: str | None,
    ) -> None:
        """Fire the build-cancelled notifier without ever raising.

        TASK-JNB-102 / DDR-007: invoked strictly AFTER
        ``build_canceller.mark_cancelled`` — the SQLite ledger is
        already authoritative; a notifier failure is a WARNING, never a
        rollback or an exception into the CLI flow.
        """
        if self.cancelled_notifier is None:
            return
        try:
            self.cancelled_notifier.notify_cancelled(
                build_id=build_id,
                reason=reason or DEFAULT_CLI_CANCEL_REASON,
                cancelled_by=responder or DEFAULT_CLI_CANCELLED_BY,
            )
        except Exception as exc:  # noqa: BLE001 — DDR-007 swallow+log
            logger.warning(
                "cli_steering.handle_cancel: build-cancelled notify "
                "failed build_id=%s err=%s — SQLite transition stands "
                "(DDR-007)",
                build_id,
                exc,
            )

    # ------------------------------------------------------------------
    # AC-002 — handle_cancel
    # ------------------------------------------------------------------

    def handle_cancel(
        self,
        build_id: str,
        *,
        reason: str | None = None,
        responder: str | None = None,
    ) -> CancelOutcome:
        """Resolve a ``forge cancel`` directive against ``build_id``.

        Branches on the snapshot's :attr:`BuildSnapshot.lifecycle`:

        1. :attr:`BuildLifecycle.PAUSED_AT_GATE` — resolve the pause as
           a synthetic REJECT (FEAT-FORGE-004 ASSUM-005) and mark the
           build terminal ``CANCELLED``.
        2. :attr:`BuildLifecycle.AUTOBUILD_RUNNING` — call
           ``cancel_async_task`` on the live autobuild's ``task_id``
           and mark the build terminal ``CANCELLED``. **No PR-creation
           is attempted.**
        3. :attr:`BuildLifecycle.OTHER_RUNNING` — mark the build
           terminal ``CANCELLED`` directly; the supervisor sees the
           terminal state on its next ``next_turn`` and refuses to
           dispatch any further stage.
        4. :attr:`BuildLifecycle.TERMINAL` — no-op; record that the
           cancel was redundant.

        Args:
            build_id: Build the cancel is directed at. Empty values are
                refused via :class:`ValueError` — the same stance the
                publishers and dispatchers take for their primary keys.
            reason: Optional operator-supplied free-text reason. When
                provided it is appended to the structured rationale so
                ``stage_log.gate_rationale`` (and the synthetic-reject
                ``notes`` field) carry the operator's words verbatim
                alongside the controlling specification reference.
            responder: Optional operator identity (typically
                ``os.getlogin()``). When provided it is appended to the
                rationale so the Group E "cancelling operator recorded
                distinctly" audit-trail invariant holds even when the
                cancelling operator differs from the originator.

        Returns:
            :class:`CancelOutcome` carrying the verdict, the rationale,
            and (where applicable) the cancelled ``task_id`` and the
            paused stage / feature scope.

        Raises:
            ValueError: If ``build_id`` is empty.
        """
        if not build_id:
            raise ValueError(
                "CliSteeringHandler.handle_cancel: build_id must be a non-empty string"
            )

        snapshot = self.snapshot_reader.get_snapshot(build_id)

        if snapshot.lifecycle is BuildLifecycle.PAUSED_AT_GATE:
            stage = snapshot.paused_stage
            if stage is None:
                # Defensive: a PAUSED_AT_GATE snapshot without a
                # paused_stage is a contract violation in the snapshot
                # reader. We surface this as ValueError rather than
                # silently fall through to a different branch — the
                # operator gets a clear signal that the upstream view
                # is malformed.
                raise ValueError(
                    "CliSteeringHandler.handle_cancel: snapshot for "
                    f"build_id={build_id!r} reports lifecycle="
                    "PAUSED_AT_GATE but paused_stage is None; refusing "
                    "to resolve a synthetic reject without a stage"
                )
            rationale = _annotate_rationale(
                CANCEL_REJECT_RATIONALE.format(
                    stage=stage,
                    feature_id=snapshot.paused_feature_id,
                    build_id=build_id,
                ),
                responder=responder,
                reason=reason,
            )
            self.pause_reject_resolver.resolve_as_reject(
                build_id=build_id,
                stage=stage,
                feature_id=snapshot.paused_feature_id,
                rationale=rationale,
            )
            self.build_canceller.mark_cancelled(
                build_id=build_id,
                rationale=rationale,
            )
            self._notify_cancelled_best_effort(
                build_id=build_id, reason=reason, responder=responder
            )
            logger.info(
                "cli_steering.handle_cancel: pause-reject path for build_id=%s "
                "stage=%s feature_id=%s",
                build_id,
                stage.value,
                snapshot.paused_feature_id,
            )
            return CancelOutcome(
                build_id=build_id,
                status=CancelStatus.CANCELLED_VIA_PAUSE_REJECT,
                rationale=rationale,
                paused_stage=stage,
                paused_feature_id=snapshot.paused_feature_id,
            )

        if snapshot.lifecycle is BuildLifecycle.AUTOBUILD_RUNNING:
            task_id = snapshot.active_autobuild_task_id
            if not task_id:
                raise ValueError(
                    "CliSteeringHandler.handle_cancel: snapshot for "
                    f"build_id={build_id!r} reports lifecycle="
                    "AUTOBUILD_RUNNING but active_autobuild_task_id is "
                    "missing; refusing to call cancel_async_task with an "
                    "empty task_id"
                )
            rationale = _annotate_rationale(
                CANCEL_AUTOBUILD_RATIONALE.format(
                    build_id=build_id,
                    task_id=task_id,
                    feature_id=snapshot.active_autobuild_feature_id,
                ),
                responder=responder,
                reason=reason,
            )
            self.async_task_canceller.cancel_async_task(task_id)
            self.build_canceller.mark_cancelled(
                build_id=build_id,
                rationale=rationale,
            )
            self._notify_cancelled_best_effort(
                build_id=build_id, reason=reason, responder=responder
            )
            logger.info(
                "cli_steering.handle_cancel: autobuild-cancel path for "
                "build_id=%s task_id=%s feature_id=%s",
                build_id,
                task_id,
                snapshot.active_autobuild_feature_id,
            )
            return CancelOutcome(
                build_id=build_id,
                status=CancelStatus.CANCELLED_VIA_AUTOBUILD,
                rationale=rationale,
                cancelled_task_id=task_id,
            )

        if snapshot.lifecycle is BuildLifecycle.OTHER_RUNNING:
            rationale = _annotate_rationale(
                CANCEL_DIRECT_RATIONALE.format(
                    build_id=build_id,
                    lifecycle=snapshot.lifecycle,
                ),
                responder=responder,
                reason=reason,
            )
            self.build_canceller.mark_cancelled(
                build_id=build_id,
                rationale=rationale,
            )
            self._notify_cancelled_best_effort(
                build_id=build_id, reason=reason, responder=responder
            )
            logger.info(
                "cli_steering.handle_cancel: direct-cancel path for build_id=%s",
                build_id,
            )
            return CancelOutcome(
                build_id=build_id,
                status=CancelStatus.CANCELLED_DIRECT,
                rationale=rationale,
            )

        # TERMINAL — no-op.
        rationale = _annotate_rationale(
            CANCEL_NOOP_TERMINAL_RATIONALE.format(build_id=build_id),
            responder=responder,
            reason=reason,
        )
        logger.info(
            "cli_steering.handle_cancel: build_id=%s already terminal; no-op",
            build_id,
        )
        return CancelOutcome(
            build_id=build_id,
            status=CancelStatus.NOOP_ALREADY_TERMINAL,
            rationale=rationale,
        )

    # ------------------------------------------------------------------
    # AC-003 / AC-006 / AC-007 — handle_skip
    # ------------------------------------------------------------------

    def handle_skip(
        self,
        build_id: str,
        stage: StageClass,
        *,
        reason: str | None = None,
        responder: str | None = None,
    ) -> SkipOutcome:
        """Resolve a ``forge skip`` directive against ``stage``.

        Always defers the permit/refuse decision to
        :meth:`ConstitutionalGuard.veto_skip`:

        * If the guard returns
          :attr:`SkipVerdict.REFUSED_CONSTITUTIONAL`, the handler
          records the refusal rationale via
          :meth:`StageSkipRecorder.record_skip_refused` and returns a
          :attr:`SkipStatus.REFUSED_CONSTITUTIONAL` outcome — the
          build remains paused (Group C @regression).
        * Otherwise the handler records the stage as SKIPPED via
          :meth:`StageSkipRecorder.record_skipped`, signals the
          supervisor to resume via
          :meth:`BuildResumer.resume_after_skip`, and returns a
          :attr:`SkipStatus.SKIPPED` outcome (Group D non-constitutional
          skip).

        Args:
            build_id: Build the skip is directed at.
            stage: Stage to skip.

        Returns:
            :class:`SkipOutcome` carrying the verdict, the recorded
            rationale, and the underlying :class:`SkipDecision` from
            the guard.

        Raises:
            ValueError: If ``build_id`` is empty.
        """
        if not build_id:
            raise ValueError(
                "CliSteeringHandler.handle_skip: build_id must be a non-empty string"
            )

        guard_decision = self.constitutional_guard.veto_skip(stage)

        if guard_decision.verdict is SkipVerdict.REFUSED_CONSTITUTIONAL:
            rationale = _annotate_rationale(
                SKIP_REFUSED_RATIONALE.format(
                    build_id=build_id,
                    stage=stage,
                    guard_rationale=guard_decision.rationale,
                ),
                responder=responder,
                reason=reason,
            )
            self.skip_recorder.record_skip_refused(
                build_id=build_id,
                stage=stage,
                rationale=rationale,
            )
            logger.warning(
                "cli_steering.handle_skip: REFUSED_CONSTITUTIONAL on "
                "build_id=%s stage=%s — %s",
                build_id,
                stage.value,
                guard_decision.rationale,
            )
            return SkipOutcome(
                build_id=build_id,
                stage=stage,
                status=SkipStatus.REFUSED_CONSTITUTIONAL,
                rationale=rationale,
                guard_decision=guard_decision,
            )

        rationale = _annotate_rationale(
            SKIP_RECORDED_RATIONALE.format(
                build_id=build_id,
                stage=stage,
            ),
            responder=responder,
            reason=reason,
        )
        self.skip_recorder.record_skipped(
            build_id=build_id,
            stage=stage,
            rationale=rationale,
        )
        self.build_resumer.resume_after_skip(
            build_id=build_id,
            skipped_stage=stage,
        )
        logger.info(
            "cli_steering.handle_skip: SKIPPED build_id=%s stage=%s",
            build_id,
            stage.value,
        )
        return SkipOutcome(
            build_id=build_id,
            stage=stage,
            status=SkipStatus.SKIPPED,
            rationale=rationale,
            guard_decision=guard_decision,
        )

    # ------------------------------------------------------------------
    # AC-004 — handle_directive
    # ------------------------------------------------------------------

    def handle_directive(
        self,
        build_id: str,
        feature_id: str,
        directive_text: str,
    ) -> DirectiveOutcome:
        """Append ``directive_text`` to the active autobuild's pending directives.

        The handler looks up the active autobuild's ``task_id`` from
        the snapshot. If a live autobuild is running for
        ``(build_id, feature_id)``, the handler calls
        :meth:`AsyncTaskUpdater.update_async_task` with
        ``append_pending_directive=directive_text`` and returns
        immediately — the autobuild itself decides when (and whether)
        to honour the directive (Group D scenario "directive appears
        as pending"). If no live autobuild is registered for the pair,
        the directive is rejected without side effect.

        Args:
            build_id: Build the directive is directed at.
            feature_id: Feature scope of the directive.
            directive_text: Free-form directive payload. Empty strings
                are refused via :class:`ValueError` — the autobuild
                cannot honour an empty directive and the operator
                deserves an immediate error rather than a silently
                queued no-op.

        Returns:
            :class:`DirectiveOutcome` carrying the verdict, the
            rationale, and (when queued) the autobuild ``task_id`` the
            directive was appended to.

        Raises:
            ValueError: If ``build_id``, ``feature_id``, or
                ``directive_text`` is empty.
        """
        if not build_id:
            raise ValueError(
                "CliSteeringHandler.handle_directive: build_id must be a "
                "non-empty string"
            )
        if not feature_id:
            raise ValueError(
                "CliSteeringHandler.handle_directive: feature_id must be "
                "a non-empty string"
            )
        if not directive_text:
            raise ValueError(
                "CliSteeringHandler.handle_directive: directive_text "
                "must be a non-empty string"
            )

        snapshot = self.snapshot_reader.get_snapshot(build_id)

        # The directive is honoured only when a live autobuild is
        # running for this exact (build_id, feature_id). Anything else
        # is surfaced as NO_ACTIVE_AUTOBUILD so the CLI can prompt the
        # operator to retry once an autobuild is dispatched.
        live_task_id = self._active_autobuild_task_id_for_feature(
            snapshot=snapshot,
            feature_id=feature_id,
        )
        if live_task_id is None:
            rationale = DIRECTIVE_NO_AUTOBUILD_RATIONALE.format(
                build_id=build_id,
                feature_id=feature_id,
            )
            logger.info(
                "cli_steering.handle_directive: NO_ACTIVE_AUTOBUILD on "
                "build_id=%s feature_id=%s",
                build_id,
                feature_id,
            )
            return DirectiveOutcome(
                build_id=build_id,
                feature_id=feature_id,
                status=DirectiveStatus.NO_ACTIVE_AUTOBUILD,
                rationale=rationale,
                directive_text=directive_text,
            )

        self.async_task_updater.update_async_task(
            live_task_id,
            append_pending_directive=directive_text,
        )
        rationale = DIRECTIVE_QUEUED_RATIONALE.format(
            build_id=build_id,
            feature_id=feature_id,
            task_id=live_task_id,
        )
        logger.info(
            "cli_steering.handle_directive: QUEUED build_id=%s feature_id=%s "
            "task_id=%s directive=%r",
            build_id,
            feature_id,
            live_task_id,
            directive_text,
        )
        return DirectiveOutcome(
            build_id=build_id,
            feature_id=feature_id,
            status=DirectiveStatus.QUEUED,
            rationale=rationale,
            directive_text=directive_text,
            task_id=live_task_id,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _active_autobuild_task_id_for_feature(
        *,
        snapshot: BuildSnapshot,
        feature_id: str,
    ) -> str | None:
        """Return the live autobuild ``task_id`` for ``feature_id`` or ``None``.

        The snapshot only carries one in-flight autobuild because the
        per-feature sequencer (TASK-MAG7-005) refuses inter-feature
        autobuild parallelism within a single build. We therefore match
        on both ``lifecycle == AUTOBUILD_RUNNING`` and the recorded
        ``active_autobuild_feature_id`` so a directive aimed at a
        non-running feature is rejected even when *some* autobuild is
        in flight on the build.
        """
        if snapshot.lifecycle is not BuildLifecycle.AUTOBUILD_RUNNING:
            return None
        if snapshot.active_autobuild_feature_id != feature_id:
            return None
        task_id = snapshot.active_autobuild_task_id
        if not task_id:
            return None
        return task_id

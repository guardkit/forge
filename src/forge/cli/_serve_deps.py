"""Composition of :class:`PipelineConsumerDeps` for ``forge serve`` (TASK-FW10-007).

This module is the Wave-3 composition step that turns the five Wave-2
collaborator factories
(:mod:`forge.cli._serve_deps_forward_context`,
:mod:`forge.cli._serve_deps_stage_log`,
:mod:`forge.cli._serve_deps_state_channel`,
:mod:`forge.cli._serve_deps_lifecycle`)
plus the SQLite duplicate-detection helper into the single
:class:`~forge.adapters.nats.pipeline_consumer.PipelineConsumerDeps`
container the inbound consumer state machine consumes.

What this module wires
----------------------

* ``forge_config`` — passed straight through; the consumer reads
  ``forge_config.pipeline.approved_originators`` and
  ``forge_config.permissions.filesystem.allowlist`` for its rejection
  rules (FEAT-FORGE-002 §2 + §3).
* ``is_duplicate_terminal`` — bound to a SQLite ``SELECT status`` against
  the unique ``(feature_id, correlation_id)`` index on the ``builds``
  table (per ASSUM-014). Returns ``True`` only when the row's
  :class:`~forge.lifecycle.state_machine.BuildState` is one of
  :data:`~forge.lifecycle.state_machine.TERMINAL_STATES`
  (``COMPLETE``/``FAILED``/``CANCELLED``/``SKIPPED``).
* ``dispatch_build`` — a thin closure that records the pending
  ``builds`` row, then calls
  :func:`forge.pipeline.dispatchers.autobuild_async.dispatch_autobuild_async`
  with the three Wave-2 Protocol collaborators
  (:class:`ForwardContextBuilder`, :class:`StageLogRecorder`,
  :class:`AutobuildStateInitialiser`) plus the injected
  :class:`AsyncTaskStarter`. Terminal-only ack of the JetStream
  message is owned by ``pipeline_consumer.handle_message``'s
  ``ack_callback`` — the closure does **not** ack itself (see
  TASK-FW10-001 AC-002).
* ``publish_build_failed`` — bound to
  :meth:`forge.adapters.nats.PipelinePublisher.publish_build_failed`
  via the publisher constructed by
  :func:`forge.cli._serve_deps_lifecycle.build_publisher_and_emitter`.
  The wrapper swallows the ``feature_id`` argument the consumer
  Protocol passes (the publisher derives the subject from
  ``payload.feature_id`` itself).

Single-client invariant (ASSUM-011)
-----------------------------------

Per the IMPLEMENTATION-GUIDE.md §5 boot order, ``_run_serve`` opens
exactly one NATS client and shares it across the daemon, the
publisher/emitter, and this deps factory. We accept the pre-opened
``client`` and pass it to
:func:`build_publisher_and_emitter` rather than dialling a second
connection here.

Per-build ``AsyncTaskStarter`` is supervisor-owned (TASK-FW10-008)
-----------------------------------------------------------------

The :class:`~forge.pipeline.dispatchers.autobuild_async.AsyncTaskStarter`
Protocol is the LangGraph ``AsyncSubAgentMiddleware`` ``start_async_task``
seam (per ADR-ARCH-031). Wiring of the Supervisor and middleware is
TASK-FW10-008's responsibility. Until that lands the deps factory
accepts ``async_task_starter`` as an optional kwarg — production
callers will pass the middleware-backed starter, while unit tests pass
a deterministic fake. When ``None``, the closure raises a clear
``RuntimeError`` rather than silently no-oping; this surfaces the
missing wiring loudly during integration rather than letting a build
disappear into a queue that has no runner attached.

References:
    - TASK-FW10-007 — this module's brief.
    - TASK-FW10-001 — boot order; ``_run_serve`` calls this factory.
    - TASK-FW10-002 — ``autobuild_runner`` AsyncSubAgent.
    - TASK-FW10-008 — supervisor + AsyncSubAgentMiddleware wiring
      (provides the production ``async_task_starter``).
    - ADR-SP-013 — terminal-only ack semantics.
    - ASSUM-011 — single shared NATS client.
    - ASSUM-014 — ``(feature_id, correlation_id)`` unique index.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Sequence

from forge.adapters.nats.pipeline_consumer import PipelineConsumerDeps
from forge.adapters.nats.pipeline_publisher import PipelinePublisher
from forge.cli._conductor_outcome import (
    DECLINED,
    TAKEN_RUNNING,
    TakenTerminal,
    check_router_outcome,
    fail_mode_c_build,
    is_mode_c_build,
)
from forge.cli._serve_deps_forward_context import (
    build_forward_context_builder,
    build_stage_log_reader,
)
from forge.cli._serve_deps_lifecycle import build_publisher_and_emitter
from forge.cli._serve_deps_stage_log import build_stage_log_recorder
from forge.cli._recorded_run import (
    had_recorded_launch,
    interrupt_recorded_run,
    launch_replacing_recorded_run,
)
from forge.cli._serve_deps_state_channel import build_autobuild_state_initialiser
from forge.config.build_admission import build_admission
from forge.config.models import ForgeConfig, PipelineConfig
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.lifecycle.state_machine import TERMINAL_STATES, BuildState
from forge.lifecycle_bridge.coexistence import (
    CLAIMER_F010F_SAFETY_NET,
    TerminalPublishLedger,
)
from forge.pipeline import PipelineLifecycleEmitter
from forge.pipeline.build_ack_handle import InFlightAckRegistry
from forge.pipeline.dispatchers.autobuild_async import (
    AsyncTaskStarter,
    dispatch_autobuild_async,
)
from forge.planning.notifications import (
    BuildThreadReply,
    answer_build_thread,
    build_not_restarted_reply,
    build_refused_reply,
    build_started_reply,
    gate_ended_reason,
    make_build_thread_reply,
)

if TYPE_CHECKING:  # pragma: no cover - import-time only
    from nats_core.events import BuildFailedPayload, BuildQueuedPayload

logger = logging.getLogger(__name__)


__all__ = [
    "BudgetBreachDispatchRefused",
    "build_pipeline_consumer_deps",
    "build_prepared_build_admission",
    "build_serve_resume_launcher",
    "is_terminal_status",
]


class BudgetBreachDispatchRefused(RuntimeError):
    """Raised by ``dispatch_build`` to REFUSE a breached re-queue with no gate.

    FEAT-UBS-002 (Option-B, stage 3 — the pre-dispatch budget gate). When a
    feature carries an un-cleared ``builds.budget_breach`` AND the TASK-GATE-D659
    approval gate is soft-failed this boot (the legacy no-gate launch branch),
    there is **no honest place a PAUSED state would be real** — the gate is the
    one seam where PAUSED is genuine, and this lane never marks a row PAUSED it
    cannot own (the honesty law). Rather than launch a fresh build that would
    silently spend the same budget again, ``dispatch_build`` raises this; the
    consumer's dispatch-error path
    (:func:`forge.adapters.nats.pipeline_consumer.handle_message`, the
    raise-before-transition convention) publishes a terminal ``build-failed``
    carrying this reason and acks the JetStream slot. The operator rules, then
    re-queues.

    When the gate IS wired the breach is handled the honest way instead — the
    build sits genuinely PAUSED at the approval gate and this is never raised.
    """

    def __init__(
        self, *, feature_id: str, prior_build_id: str, breach_detail: str
    ) -> None:
        self.feature_id = feature_id
        self.prior_build_id = prior_build_id
        self.breach_detail = breach_detail
        super().__init__(
            f"budget breach not cleared for {feature_id} "
            f"(prior build {prior_build_id}: {breach_detail}); refusing to "
            "dispatch a fresh build without an approval gate to own the pause"
        )


#: Set of canonical ``builds.status`` string values that count as
#: terminal for the duplicate-detection helper. Mirrors
#: :data:`forge.lifecycle.state_machine.TERMINAL_STATES` but stored as
#: the raw string column values used in SQLite so the SQL ``IN`` clause
#: can compare directly without re-hydrating the enum.
_TERMINAL_STATUS_VALUES: frozenset[str] = frozenset(s.value for s in TERMINAL_STATES)


def is_terminal_status(status: str | None) -> bool:
    """Return True when ``status`` names a terminal :class:`BuildState`.

    Pulled out as a small helper so the duplicate-detection closure
    body stays one assertion long and the membership check is unit-
    testable in isolation. ``None`` (no row) is the legitimate "fresh
    build" signal and returns ``False``.
    """
    return status is not None and status in _TERMINAL_STATUS_VALUES


def _build_is_duplicate_terminal(
    sqlite_pool: SqliteLifecyclePersistence,
):
    """Return an ``async (feature_id, correlation_id) -> bool`` closure.

    The closure issues a single ``SELECT status FROM builds WHERE
    feature_id = ? AND correlation_id = ?`` against a fresh read-only
    SQLite connection (per ADR-ARCH-013) and translates the result:

    * **No row** → ``False`` (fresh build; the consumer continues with
      validation + dispatch).
    * **Non-terminal status** (``QUEUED``/``PREPARING``/``RUNNING``/
      ``PAUSED``/``FINALISING``) → ``False``. The build is in flight;
      the consumer's normal flow handles it (a redelivered envelope
      against an in-flight build is reconciled by
      :func:`forge.adapters.nats.pipeline_consumer.reconcile_on_boot`,
      not by this duplicate-detection helper).
    * **Terminal status** (``COMPLETE``/``FAILED``/``CANCELLED``/
      ``SKIPPED``) → ``True`` (idempotent ack-and-skip).

    The closure is ``async def`` to honour the
    :data:`~forge.adapters.nats.pipeline_consumer.IsDuplicateTerminal`
    type alias even though the underlying SQLite read is synchronous;
    SQLite reads against the daemon's pool are short and we keep the
    daemon's event loop responsive by holding the writer connection's
    lock for the read alone (no transaction).
    """

    async def is_duplicate_terminal(feature_id: str, correlation_id: str) -> bool:
        """Return True when a terminal ``builds`` row matches the pair."""
        if not feature_id or not correlation_id:
            # The consumer should not call this with empty identifiers
            # (its envelope validation rejects them upstream). Still
            # guard here so a regression in the validator surfaces as a
            # clean ``False`` rather than a wide-open SQL query.
            return False

        try:
            with sqlite_pool._reader() as cx:
                row = cx.execute(
                    """
                    SELECT status FROM builds
                     WHERE feature_id = ? AND correlation_id = ?
                    """,
                    (feature_id, correlation_id),
                ).fetchone()
        except sqlite3.Error as exc:
            # Read failure is not load-bearing for correctness — the
            # consumer treats False as "process the build", which means
            # at worst we re-dispatch a known-terminal build. SQLite
            # surfaces the actual failure for ops via the warning.
            logger.warning(
                "is_duplicate_terminal: SQLite read failed for "
                "feature_id=%s correlation_id=%s (%s); treating as "
                "non-duplicate",
                feature_id,
                correlation_id,
                exc,
            )
            return False

        if row is None:
            return False
        # ``sqlite3.Row`` supports both index and key access; we used
        # ``SELECT status`` so column 0 is the status string. Coerce
        # explicitly so a future schema migration that adds columns
        # cannot quietly shift the index.
        status: Any = row[0] if not hasattr(row, "keys") else row["status"]
        if isinstance(status, BuildState):
            status = status.value
        result = is_terminal_status(status)
        if result:
            logger.debug(
                "is_duplicate_terminal: matched terminal row "
                "feature_id=%s correlation_id=%s status=%s",
                feature_id,
                correlation_id,
                status,
            )
        return result

    return is_duplicate_terminal


def _build_retired_planning_handoffs_reader(
    sqlite_pool: SqliteLifecyclePersistence,
) -> Callable[[], frozenset[str]]:
    """Bind the canonical reader to the daemon's read-only SQLite handle."""

    def read_retired_planning_handoffs() -> frozenset[str]:
        from forge.lifecycle.planning_handoff_retirement import (
            retired_planning_handoff_correlations,
        )

        with sqlite_pool._reader() as connection:
            return retired_planning_handoff_correlations(connection)

    return read_retired_planning_handoffs


def _utc_now() -> datetime:
    """Composition-root wall clock for the gate.

    The gate's SQLite adapters and mirrored publisher need an injected
    ``() -> datetime`` (clock hygiene). Production has no earlier injected
    clock at daemon boot, so this named function is the single
    composition-root wall-clock seam — mirrors the ``GateCheckDeps.clock``
    default and the ``_serve_deps_gating`` cancelled-emit timestamp. Tests
    thread a deterministic clock via ``build_pipeline_consumer_deps``.
    """
    return datetime.now(timezone.utc)


def _build_resume_launcher(
    forward_context_builder: Any,
    stage_log_recorder: Any,
    state_channel: Any,
    lifecycle_emitter: Any,
    async_task_starter: AsyncTaskStarter | None,
    memory_project_reader: Callable[[str], str | None] | None = None,
    launch_settings_reader: Callable[[str], "Sequence[str]"] | None = None,
    source_commit_reader: Callable[[str], str | None] | None = None,
    feature_routing_gate: Any | None = None,
    feature_routing_required: bool = False,
) -> Callable[..., Any]:
    """Return the launch closure — ``dispatch_build`` minus ``record_pending_build``.

    TASK-GATE-D659 (plan §R1 / §D4.2): the "launch" half of dispatch is
    factored out so BOTH the live approve path (this Wave) and the Wave-3
    boot-time rearm resume path drive the SAME
    :func:`dispatch_autobuild_async` call with the five Wave-2
    collaborators — the build row already exists (recorded at dispatch or
    restored on boot) so re-recording it is neither needed nor legal.
    """

    async def launch(
        *,
        build_id: str,
        feature_id: str,
        correlation_id: str | None,
        branch: str | None = None,
        repo: str | None = None,
        budget: dict[str, Any] | None = None,
    ) -> Any:
        if async_task_starter is None:
            raise RuntimeError(
                "build_pipeline_consumer_deps: launch was invoked but no "
                "async_task_starter was wired. Production wiring lives in "
                "TASK-FW10-008 (Supervisor + AsyncSubAgentMiddleware); tests "
                "should pass a fake starter via the kwarg."
            )
        # DEFECT #19 activation (B4 round-17): forward ``branch``/``repo`` from
        # the accepted BuildQueuedPayload (the live approve path passes them).
        #
        # SECOND-REPO LAW (this lane): the boot-rearm resume path has no payload
        # in scope, but it DOES hold the restored ``builds`` row — and
        # ``builds.repo`` is a required column. ``rearm_paused_gates`` now
        # passes ``repo=build_row.repo`` through ``_rearm_dispatch``, so a
        # re-armed launch names its own repository. ``branch`` still defaults to
        # None on that path deliberately: threading it would flip the resume
        # from the shared-checkout launch to the DEFECT #19 isolated-worktree
        # launch, a behaviour change the rearm path has never been proved
        # against. ``repo`` alone is the correctness fix; ``branch`` stays a
        # separate, ledgered question.
        #
        # FEAT-UBS-002 (Option-B, stage 1): ``budget`` rides the same one-hop
        # provenance. The live dispatch path resolves it from ``builds.profile``
        # and passes it; the boot-rearm resume path leaves it None (a resume is
        # not a fresh launch) so the resume bytes stay byte-compatible too.
        #
        # WHICH MEMORY THIS BUILD BELONGS TO (item 2, 2026-09-21) is read off
        # the build's own ledger row HERE, in the one closure BOTH the live
        # approve path and the boot-rearm resume path go through, rather than
        # threaded down from every caller. A resume gets the same name its
        # first launch had, which is the point: the memory a piece of work
        # belongs to is decided once, when the work starts.
        memory_project = (
            memory_project_reader(build_id)
            if memory_project_reader is not None and build_id
            else None
        )
        launch_settings = (
            launch_settings_reader(build_id)
            if launch_settings_reader is not None and build_id
            else ()
        )
        # The exact commit a prepared feature was admitted at (4 October
        # 2026), off the same row by the same rule: a resume builds the commit
        # its first launch was admitted at. ``None`` launches as before.
        source_commit = (
            source_commit_reader(build_id)
            if source_commit_reader is not None and build_id
            else None
        )
        feature_routing_id = correlation_id or build_id
        if source_commit:
            return await dispatch_autobuild_async(
                build_id=build_id,
                feature_id=feature_id,
                correlation_id=correlation_id,
                forward_context_builder=forward_context_builder,
                async_task_starter=async_task_starter,
                stage_log_recorder=stage_log_recorder,
                state_channel=state_channel,
                lifecycle_emitter=lifecycle_emitter,
                branch=branch,
                repo=repo,
                budget=budget,
                memory_project=memory_project,
                launch_settings=launch_settings,
                source_commit=source_commit,
                feature_routing_id=feature_routing_id,
                feature_routing_gate=feature_routing_gate,
                feature_routing_required=feature_routing_required,
            )
        return await dispatch_autobuild_async(
            build_id=build_id,
            feature_id=feature_id,
            correlation_id=correlation_id,
            forward_context_builder=forward_context_builder,
            async_task_starter=async_task_starter,
            stage_log_recorder=stage_log_recorder,
            state_channel=state_channel,
            lifecycle_emitter=lifecycle_emitter,
            branch=branch,
            repo=repo,
            budget=budget,
            memory_project=memory_project,
            launch_settings=launch_settings,
            feature_routing_id=feature_routing_id,
            feature_routing_gate=feature_routing_gate,
            feature_routing_required=feature_routing_required,
        )

    return launch


def build_serve_resume_launcher(
    sqlite_pool: SqliteLifecyclePersistence,
    forge_config: ForgeConfig,
    *,
    lifecycle_emitter: PipelineLifecycleEmitter,
    async_task_starter: AsyncTaskStarter | None,
    conductor_router: Callable[..., Any] | None = None,
    feature_routing_gate: Any | None = None,
    feature_routing_required: bool = False,
) -> Callable[..., Any]:
    """Compose the boot-time rearm resume launcher (TASK-GATE-D659 §D4.2).

    The Wave-3 ``rearm_paused_gates`` sweep needs the SAME "launch" half of
    dispatch the live approve path uses — :func:`dispatch_autobuild_async` with
    the five Wave-2 collaborators, minus ``record_pending_build`` (the row is
    already PAUSED, restored on boot). This factory composes the four SQLite-
    bound collaborators (forward-context builder, stage-log recorder,
    state-channel initialiser) against ``sqlite_pool`` + ``forge_config`` and
    threads the shared ``lifecycle_emitter``, returning the same
    ``launch(*, build_id, feature_id, correlation_id)`` closure
    :func:`_build_resume_launcher` produces.

    Kept as a thin public seam (rather than reaching into the private
    ``dispatch_build`` composition) so ``serve.py::_compose`` can build the
    launcher at the rearm spawn site without re-deriving the deps graph.

    **SILENT-DOWNGRADE SEAM 2 (activation design §4.2).** The returned
    closure is GUARDED. The rearm sweep's approve path consults no router
    of its own — it holds only this launcher — so a mode-c build that was
    carded, then met a daemon restart, then got approved, launched down
    the ROUTINE autobuild path against a TASK-xxx subject. The guard reads
    the same ``builds.mode`` the router reads and never routine-launches a
    mode-c row.

    **RE-ENTRY (conductor rewire rule 5).** ``conductor_router`` is the
    way back IN, and it is what the guard now does with a fix journey
    first: a re-approved mode-c row is handed to the same router the
    dequeue path uses (``serve.py``'s composed conductor), which reuses
    the journey's own worktree and drives it. The router speaks the
    taken-and-terminal vocabulary
    (:mod:`forge.cli._conductor_outcome`):

    * ``TAKEN_RUNNING`` — the turn loop is driving it; nothing else
      happens here (the journey owns its own terminal).
    * ``TakenTerminal(reason=...)`` — taken and already over; the router
      has written the reason onto the row, and this closure emits the
      ``build-failed`` that carries it.
    * ``DECLINED``, a router that raised, or NO router at all (the
      conductor switched off) — the ledgered REFUSAL, unchanged: FAILED
      with the reason on the row plus a ``build-failed`` emit, never a
      routine launch. There is exactly one refusal statement and this is
      it; the router branch replaced the old unconditional one.

    On every arm that ends the build the ack rides the FAILED row: the
    still-held build-queued message redelivers, the consumer's
    duplicate-terminal filter sees a terminal row and acks (the
    self-healing arm ``_rearm_dispatch`` already relies on for a gate
    reject).
    """
    stage_log_reader = build_stage_log_reader(sqlite_pool)
    forward_context_builder = build_forward_context_builder(
        stage_log_reader, forge_config
    )
    stage_log_recorder = build_stage_log_recorder(sqlite_pool)
    state_channel = build_autobuild_state_initialiser(sqlite_pool)
    launch = _build_resume_launcher(
        forward_context_builder,
        stage_log_recorder,
        state_channel,
        lifecycle_emitter,
        async_task_starter,
        # Which memory this build belongs to, off its own ledger row (item 2,
        # 2026-09-21). ``None`` from the reader is "not recorded", and the
        # launch omits the name rather than inventing one. A pool that does not
        # offer the read at all — a narrowed adapter in a test — is the same
        # answer: nothing recorded, never a guess.
        getattr(sqlite_pool, "read_memory_project", None),
        # And what the project said its own builds need, by name, off the same
        # row (22 September 2026). An absent read, or a row from before this
        # existed, is an empty list: the factory's own list and nothing else.
        getattr(sqlite_pool, "read_launch_settings", None),
        # The exact commit a prepared feature was admitted at (4 October
        # 2026); a resumed prepared build builds that commit, never the branch.
        getattr(sqlite_pool, "read_source_commit", None),
        feature_routing_gate,
        feature_routing_required,
    )

    async def guarded_launch(
        *,
        build_id: str,
        feature_id: str,
        correlation_id: str | None,
        **launch_kwargs: Any,
    ) -> Any:
        if is_mode_c_build(sqlite_pool, build_id, log=logger):
            outcome: Any = DECLINED
            if conductor_router is not None:
                try:
                    outcome = await conductor_router(
                        build_id=build_id,
                        feature_id=feature_id,
                        correlation_id=correlation_id,
                        **launch_kwargs,
                    )
                except Exception as exc:  # noqa: BLE001 — refuse, never routine
                    logger.error(
                        "rearm resume: conductor_router raised (%s) for "
                        "build_id=%s; the re-approved fix journey is REFUSED "
                        "(it is never downgraded onto the routine path)",
                        exc,
                        build_id,
                    )
                    outcome = DECLINED
                # Outside the try on purpose, exactly as ``dispatch_build``
                # does it: a contract error caught by the rail above would be
                # read as DECLINED — the silent downgrade the vocabulary
                # exists to abolish. Out here it propagates and is loud.
                outcome = check_router_outcome(outcome, build_id=build_id)

            if outcome is TAKEN_RUNNING:
                logger.info(
                    "rearm resume: build_id=%s feature_id=%s is a fix journey "
                    "that was re-approved after a restart; handed to the "
                    "conductor's turn loop, which reuses the journey's own "
                    "worktree — NOT launched as a routine autobuild",
                    build_id,
                    feature_id,
                )
                return None

            if isinstance(outcome, TakenTerminal):
                # The router took it and it is already over; the reason is
                # already on the row. Only the terminal emit is owed.
                reason = outcome.reason
                logger.error(
                    "rearm resume: the conductor took build_id=%s and it is "
                    "already terminal (%s); emitting build-failed",
                    build_id,
                    reason,
                )
            else:
                summary = (
                    "a fix-journey (mode-c) build was approved on the "
                    "boot-rearm path with no conductor to hand it to — "
                    "refused, never downgraded onto the routine autobuild path"
                )
                logger.error(
                    "rearm resume: build_id=%s feature_id=%s is a fix journey "
                    "and no conductor took it (the conductor is switched off, "
                    "or its router declined or raised); REFUSING the routine "
                    "resume launch — running a fix task as a routine autobuild "
                    "is the silent downgrade.",
                    build_id,
                    feature_id,
                )
                reason = fail_mode_c_build(
                    sqlite_pool,
                    build_id,
                    summary=summary,
                    what="a mode-c row on the boot-rearm resume path",
                    log=logger,
                )
            if lifecycle_emitter is not None:
                from forge.pipeline import BuildContext

                await lifecycle_emitter.emit_failed(
                    BuildContext(
                        feature_id=feature_id or "",
                        build_id=build_id or "",
                        correlation_id=correlation_id or "",
                        wave_total=1,
                    ),
                    failure_reason=reason,
                    recoverable=False,
                    failed_task_id=_read_task_id(sqlite_pool, build_id),
                )
            return None
        return await launch(
            build_id=build_id,
            feature_id=feature_id,
            correlation_id=correlation_id,
            **launch_kwargs,
        )

    return guarded_launch


def _read_task_id(
    sqlite_pool: SqliteLifecyclePersistence, build_id: str
) -> str | None:
    """Return ``builds.task_id`` for ``build_id`` — the journey's subject.

    ``None`` for every mode-a / mode-b row, for a missing row, and for an
    unreadable pool: the identifier is an ANNOTATION on the terminal, so a
    read fault must never stop the terminal from being emitted.
    """
    try:
        row = sqlite_pool.get_build_row(build_id)
    except Exception as exc:  # noqa: BLE001 — an annotation, never a blocker
        logger.warning(
            "could not read builds.task_id for build_id=%s (%s)", build_id, exc
        )
        return None
    return getattr(row, "task_id", None) if row is not None else None


def _read_build_identity(
    sqlite_pool: SqliteLifecyclePersistence,
    *,
    feature_id: str,
    correlation_id: str,
) -> tuple[str, "BuildState"] | None:
    """Return the existing identity, or ``None`` only after proving absence.

    Read failures propagate so the caller can hold the delivery rather than
    acknowledge a refusal for a potentially nonterminal existing build.
    """
    with sqlite_pool._reader() as cx:
        row = cx.execute(
            "SELECT build_id, status FROM builds WHERE feature_id = ? "
            "AND correlation_id = ?",
            (feature_id, correlation_id),
        ).fetchone()
    if row is None:
        return None
    build_id = row[0] if not hasattr(row, "keys") else row["build_id"]
    raw = row[1] if not hasattr(row, "keys") else row["status"]
    state = raw if isinstance(raw, BuildState) else BuildState(raw)
    return str(build_id), state


def _queue_order_key(queued_at: Any, correlation_id: str) -> tuple[datetime, str]:
    """Order builds of one feature by when they were queued (then correlation).

    ``queued_at`` is a stored ISO-8601 string or a ``datetime``; a value
    without a timezone is read as UTC, matching how rows are written.
    """
    value = (
        queued_at
        if isinstance(queued_at, datetime)
        else datetime.fromisoformat(str(queued_at))
    )
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return (value, correlation_id)


def _another_build_of_feature_goes_first(
    sqlite_pool: SqliteLifecyclePersistence,
    payload: Any,
    started: set[str],
) -> tuple[bool, str | None]:
    """Decide whether another build of this feature keeps it (one at a time).

    The lifecycle bridge tracks one live build per feature, so only one
    build of a feature may be active at once. Another ACTIVE build of the
    same feature with a different correlation goes first when:

    * it is past QUEUED (an approval card, a runner or the merge step owns
      it already);
    * it is still QUEUED but this daemon already let it through (``started``
      holds the correlations it let through for this feature); or
    * it is still QUEUED, not yet let through (a row written ahead by the
      CLI or the fix journey), and was queued earlier than this one.

    Synchronous on purpose: the caller runs it and then writes its own row
    with no ``await`` in between, so within the daemon's single event loop
    the check and the write cannot interleave with another dispatch.

    Returns:
        ``(refuse, own_build_id)`` — ``own_build_id`` is this delivery's own
        row when one was written ahead and is still QUEUED, else ``None``.
        A delivery whose own row is already past QUEUED, or that this
        daemon already let through, is a redelivery: it returns
        ``(False, None)`` and the existing duplicate handling decides.
    """
    from forge.lifecycle.persistence import ACTIVE_STATES

    # INTERRUPTED counts too: after a factory-only restart such a build's
    # original run may still be going, and its redelivery relaunches it (own
    # row: the duplicate handling decides; another build's: it goes first).
    active_values = tuple(s.value for s in ACTIVE_STATES) + (
        BuildState.INTERRUPTED.value,
    )
    placeholders = ",".join("?" * len(active_values))
    with sqlite_pool._reader() as cx:
        rows = cx.execute(
            "SELECT build_id, correlation_id, status, queued_at FROM builds "
            f"WHERE feature_id = ? AND status IN ({placeholders})",
            (payload.feature_id, *active_values),
        ).fetchall()
    own = [r for r in rows if r[1] == payload.correlation_id]
    others = [r for r in rows if r[1] != payload.correlation_id]
    own_build_id: str | None = None
    if own:
        if own[0][2] != BuildState.QUEUED.value or payload.correlation_id in started:
            return False, None
        own_build_id = str(own[0][0])
        mine = _queue_order_key(own[0][3], payload.correlation_id)
    else:
        mine = _queue_order_key(payload.queued_at, payload.correlation_id)
    for _build_id, correlation_id, status, queued_at in others:
        if status != BuildState.QUEUED.value or correlation_id in started:
            return True, own_build_id
        if _queue_order_key(queued_at, correlation_id) < mine:
            return True, own_build_id
    return False, own_build_id


def _read_build_status(
    sqlite_pool: SqliteLifecyclePersistence,
    *,
    feature_id: str,
    correlation_id: str,
) -> "BuildState | None":
    """Return the ``builds.status`` for ``(feature_id, correlation_id)`` or ``None``.

    Used by the R2 two-arm ``DuplicateBuildError`` handling to decide
    whether a duplicate delivery should ack (row terminal) or hold the
    slot without acking (row PAUSED / in-flight).
    """
    try:
        with sqlite_pool._reader() as cx:
            row = cx.execute(
                "SELECT status FROM builds WHERE feature_id = ? "
                "AND correlation_id = ?",
                (feature_id, correlation_id),
            ).fetchone()
    except sqlite3.Error as exc:  # pragma: no cover - defensive
        logger.warning(
            "dispatch_build: status read failed for feature_id=%s "
            "correlation_id=%s (%s); treating as non-terminal (hold slot)",
            feature_id,
            correlation_id,
            exc,
        )
        return None
    if row is None:
        return None
    raw = row[0] if not hasattr(row, "keys") else row["status"]
    if isinstance(raw, BuildState):
        return raw
    return BuildState(raw)


def _resolve_launch_budget(
    sqlite_pool: SqliteLifecyclePersistence,
    forge_config: Any,
    build_id: str,
) -> dict[str, Any] | None:
    """Resolve the compact per-build budget entry for the launch payload.

    FEAT-UBS-002 (Option-B, stage 1 — the run bounds ITSELF). Reads the build's
    resolved :class:`BudgetGuards` via :func:`resolve_budget_for_build`
    (``builds.profile`` → ``config.budget.resolve``) and, when the profile has
    caps enabled AND a wall-clock cap is set, returns the compact
    ``{"max_wallclock_seconds", "profile_name"}`` dict that rides the launch
    payload to the runner. The runner takes the MINIMUM of that cap and its
    env/default subprocess timeout, so a profile can only TIGHTEN the existing
    bound, never loosen it, and its ``proc.kill`` on expiry is the honest hard
    stop (this lane never marks a still-running row PAUSED/CANCELLED).

    Returns ``None`` for an attended / NULL profile (caps off, ASSUM-010), a
    missing ``forge_config``, or a profile whose only caps are non-wall-clock —
    so the launch payload stays BYTE-EQUIVALENT with the pre-budget shape, the
    caps-off no-op invariant this whole lane preserves at every stage.

    Only ``max_build_wallclock_seconds`` is carried: it is the ONLY cap
    honestly enforceable on this live pipeline-consumer path.
    ``max_review_cycles`` push-in is out of scope (per-task ``--max-turns`` vs
    whole-build semantic mismatch, coordinator-parked) and ``max_build_tokens``
    is UNMEASURED here — neither rides the payload, so this entry never implies
    an enforceability the runner cannot deliver.
    """
    if forge_config is None:
        return None
    # Local import: the pure resolve helper lives in ``forge.cli.serve``, which
    # imports this module inside its own composer closures — a module-level
    # import here would risk an import cycle at CLI ``--help`` load time.
    from forge.cli.serve import resolve_budget_for_build

    guards, profile_name = resolve_budget_for_build(
        sqlite_pool, forge_config, build_id
    )
    if not guards.caps_enabled:
        return None
    wallclock = guards.max_build_wallclock_seconds
    if wallclock is None:
        return None
    return {
        "max_wallclock_seconds": int(wallclock),
        "profile_name": profile_name,
    }


def _build_dispatch_build(
    sqlite_pool: SqliteLifecyclePersistence,
    forward_context_builder: Any,
    stage_log_recorder: Any,
    state_channel: Any,
    lifecycle_emitter: Any,
    async_task_starter: AsyncTaskStarter | None,
    *,
    forge_config: Any = None,
    gate_repository: Any = None,
    gate_state_machine: Any = None,
    gate_clock: Callable[[], datetime] | None = None,
    conductor_router: Callable[..., Any] | None = None,
    record_build_rejection: Callable[[str, str], Any] | None = None,
    prepared_build_admission: Callable[[Any], Awaitable[Any]] | None = None,
    reply_in_thread: BuildThreadReply | None = None,
    feature_routing_gate: Any | None = None,
    feature_routing_required: bool = False,
):
    """Return the production ``dispatch_build`` closure.

    The closure persists a ``QUEUED`` ``builds`` row (so downstream
    crash-recovery has a durable record of the dispatch attempt), runs the
    TASK-GATE-D659 pre-dispatch approval gate (:func:`maybe_gate_build`),
    and only launches the autobuild runner on gate approval. On a gate
    terminal (reject / expiry / hard-stop) it acks the JetStream slot and
    never launches; while paused it holds the slot un-acked (the runner is
    launched by the R1 approve callback).

    ``conductor_router`` (conductor revival, Stage 1c — design pass §a.2)
    is the ONE seam through which a dequeued fix-journey build is handed
    to the conductor's turn loop **instead of** the direct autobuild
    launch. Since the activation lane (design §3) it answers the
    taken-and-terminal VOCABULARY, not a bool — ``async (**launch_kwargs)
    -> ConductorOutcome | TakenTerminal``:

    * ``DECLINED`` — "not mine, launch it the routine way";
    * ``TAKEN_RUNNING`` — the turn loop is driving it (no launch, no ack:
      the journey owns its own terminal);
    * ``TakenTerminal(reason=...)`` — taken AND already over. This closure
      acks the slot and emits ``build-failed`` carrying the reason, on
      BOTH launch arms. Before the vocabulary this case was a bare
      ``True``: the row went FAILED, nothing acked, and under
      ``max_ack_pending=1`` the whole consumer wedged until the 1h
      ``ack_wait`` redelivery.

    A legacy bare bool reaching this seam REFUSES loudly
    (:func:`~forge.cli._conductor_outcome.check_router_outcome`) — the
    contract is replaced, not dual-shaped.

    **The prime invariant of this lane lives on this parameter.** ``None``
    — which is what the composition root passes whenever
    ``conductor.enabled`` is off, i.e. always by default — leaves both
    launch branches calling ``launch(...)`` with byte-identical kwargs in
    byte-identical order. The router is consulted only when it exists, so
    the flag-off dequeue path is not merely equivalent to today's, it is
    the same call sequence (asserted by the flag-off call-sequence test).
    The one addition is the §4.3 mode-c guard on the launch arm, which
    reads ``builds.mode`` and changes no launch byte for a routine build.

    ``prepared_build_admission`` (4 October 2026, a prepared feature through
    the normal build route) is ``async (payload) -> AdmissionAnswer``. It is
    asked only for a delivery with NO planning run and NO build row yet — a
    feature planned elsewhere and queued straight to a build — and runs before
    the row is written: it fetches the project's remote, reads the
    declarations at the queued branch's commit and checks the supplied files
    there. A refusal ends the delivery like any other refusal before a row
    (noted on the queue row, ``build-failed``, acknowledged); the admitted
    facts are recorded on the row. ``None`` admits nothing and records
    nothing, exactly as before. Nothing on this route calls a planning
    capability.

    ``reply_in_thread`` (register-projects design, 5 October 2026, part 3)
    answers a build request in the conversation it was handed over from:
    once on each refusal this closure makes before a row exists (the sandbox
    policy, the prepared admission, the one-build-per-feature check), and
    once — "Building FEAT-XXXX for <repo> from <branch> at <commit>" — after
    a freshly recorded build has passed its approval gate and been launched,
    never before. Only a request carrying ``parent_request_id`` is answered;
    ``None`` answers nothing. Acknowledgement and admission order are
    unchanged: each answer follows the step it reports.
    """
    launch = _build_resume_launcher(
        forward_context_builder,
        stage_log_recorder,
        state_channel,
        lifecycle_emitter,
        async_task_starter,
        # Which memory this build belongs to, off its own ledger row (item 2,
        # 2026-09-21). ``None`` from the reader is "not recorded", and the
        # launch omits the name rather than inventing one. A pool that does not
        # offer the read at all — a narrowed adapter in a test — is the same
        # answer: nothing recorded, never a guess.
        getattr(sqlite_pool, "read_memory_project", None),
        # And what the project said its own builds need, by name, off the same
        # row (22 September 2026). An absent read, or a row from before this
        # existed, is an empty list: the factory's own list and nothing else.
        getattr(sqlite_pool, "read_launch_settings", None),
        # The exact commit a prepared feature was admitted at (4 October
        # 2026). ``None`` for every other build, whose launch is unchanged.
        getattr(sqlite_pool, "read_source_commit", None),
        feature_routing_gate,
        feature_routing_required,
    )
    clock = gate_clock or _utc_now

    async def _conductor_terminal(
        terminal: TakenTerminal,
        *,
        build_id: str | None,
        feature_id: str | None,
        correlation_id: str | None,
        ack_callback: Any,
    ) -> None:
        """Close a taken-and-terminal build: emit ``build-failed``, then ack.

        Activation design §3 — the ack cure. Before the vocabulary a
        cap-refused fix journey wrote its FAILED row and stopped there:
        nothing reached the daemon's event stream, so the bridge observer
        (the consumer's terminal-follower) never fired and the slot healed
        only at the 1h ``ack_wait`` redelivery. With
        ``max_ack_pending=1`` that ONE refusal wedged the entire consumer
        for the whole hour, and no terminal envelope was ever published
        for the build, so a correlation-id-following observer waited
        forever.

        Two acts, in this order:

        1. **Emit** ``pipeline.build-failed.{feature_id}`` through the
           ``lifecycle_emitter`` in closure scope, on a SYNTHESIZED
           :class:`BuildContext` — the in-repo precedent is the gate
           machinery, which builds one the same way with ``wave_total=1``
           (``_serve_gate_activation.maybe_gate_build``). The reason rides
           the :class:`TakenTerminal` itself (no ``builds.error`` re-read),
           ``recoverable=False`` (a refused journey is not retried by
           anyone downstream), and ``failed_task_id`` comes off the row —
           ``builds.task_id`` is the fix journey's durable subject.
        2. **Ack** the JetStream slot exactly as the gate-terminal arm
           does, so the next queued build dequeues immediately.

        The emit goes FIRST: releasing the slot before the terminal is on
        the wire would let the next build's envelopes overtake this one's
        terminal. ``emit_failed`` is itself publish-safe (the emitter
        swallows transport faults), so a dead broker cannot leave the slot
        un-acked.
        """
        from forge.pipeline import BuildContext

        failed_task_id = _read_task_id(sqlite_pool, build_id) if build_id else None

        logger.error(
            "dispatch_build: the conductor REFUSED build_id=%s and it is "
            "already terminal (%s); emitting build-failed and acking the "
            "queue slot — NOT launching the routine autobuild",
            build_id,
            terminal.reason,
        )
        if lifecycle_emitter is not None:
            await lifecycle_emitter.emit_failed(
                BuildContext(
                    feature_id=feature_id or "",
                    build_id=build_id or "",
                    # correlation_id is required on the payload; the
                    # None → "" coercion mirrors the gate machinery's.
                    correlation_id=correlation_id or "",
                    wave_total=1,
                ),
                failure_reason=terminal.reason,
                recoverable=False,
                failed_task_id=failed_task_id,
            )
        else:  # pragma: no cover - production always wires the emitter
            logger.error(
                "dispatch_build: no lifecycle_emitter is wired — the "
                "conductor terminal for build_id=%s reaches no observer; "
                "acking anyway so the consumer is not wedged",
                build_id,
            )
        if ack_callback is not None:
            await ack_callback()
        else:  # pragma: no cover - both call sites thread it
            logger.error(
                "dispatch_build: no ack_callback threaded to the conductor "
                "terminal for build_id=%s; the slot will heal only at the "
                "JetStream ack_wait expiry",
                build_id,
            )

    async def launch_or_conduct(
        *, ack_callback: Any = None, **launch_kwargs: Any
    ) -> Any:
        """Route one accepted build: conductor first (if wired), else launch.

        With no router wired this is a straight pass-through to
        ``launch`` — the flag-off byte-equivalence guarantee — except for
        the mode-c guard on the launch arm (below), which reads the row
        but changes no launch byte.

        The router speaks the taken-and-terminal vocabulary
        (:mod:`forge.cli._conductor_outcome`); this closure is where it is
        MAPPED, so BOTH call sites — the gate-approved arm and the no-gate
        soft-fail arm — ack and emit identically on a terminal.
        """
        build_id = launch_kwargs.get("build_id")
        if conductor_router is not None:
            try:
                outcome: Any = await conductor_router(**launch_kwargs)
            except Exception as exc:  # noqa: BLE001 — never brick the routine path
                logger.error(
                    "dispatch_build: conductor_router raised (%s) for "
                    "build_id=%s; falling back to the routine launch so the "
                    "build still runs (the conductor earns jobs, it never "
                    "blocks one)",
                    exc,
                    build_id,
                )
                outcome = DECLINED
            # The contract check sits OUTSIDE the try on purpose: a
            # ``ConductorOutcomeContractError`` raised inside it would be
            # caught by the degrade rail above and read as DECLINED —
            # exactly the silent downgrade the widened contract exists to
            # abolish. Out here it propagates, and the consumer's
            # raise-before-transition convention turns it into a loud
            # terminal + ack.
            outcome = check_router_outcome(outcome, build_id=build_id)
            if isinstance(outcome, TakenTerminal):
                await _conductor_terminal(
                    outcome,
                    build_id=build_id,
                    feature_id=launch_kwargs.get("feature_id"),
                    correlation_id=launch_kwargs.get("correlation_id"),
                    ack_callback=ack_callback,
                )
                return None
            if outcome is TAKEN_RUNNING:
                logger.info(
                    "dispatch_build: build_id=%s handed to the conductor's "
                    "turn loop; NOT launching the routine autobuild",
                    build_id,
                )
                return None

        # SILENT-DOWNGRADE SEAM 3 (activation design §4.3): dispatch has no
        # mode check outside the router, so a flag-off boot (or a router
        # whose composition failed, or one that raised into the degrade rail
        # above) plus a runless mode-c redelivery would launch a FIX TASK as
        # a routine autobuild — the wrong machinery against a TASK-xxx
        # subject. The queue-time belt already refuses mode-c queues while
        # the flag is off, so this arm should be unreachable;
        # unreachable-but-guarded is the posture. Reading the row costs one
        # indexed SELECT and changes no launch byte for a routine build.
        if is_mode_c_build(sqlite_pool, build_id or "", log=logger):
            summary = (
                "a fix-journey (mode-c) build reached the routine launch arm "
                "with no conductor driving it — refused, never downgraded"
            )
            await _conductor_terminal(
                TakenTerminal(
                    reason=fail_mode_c_build(
                        sqlite_pool,
                        build_id or "",
                        summary=summary,
                        what="a mode-c row reaching the routine launch arm",
                        log=logger,
                    )
                ),
                build_id=build_id,
                feature_id=launch_kwargs.get("feature_id"),
                correlation_id=launch_kwargs.get("correlation_id"),
                ack_callback=ack_callback,
            )
            return None
        return await launch(**launch_kwargs)

    # Correlations this daemon has let through, per feature, for the
    # one-build-per-feature check below. Only ever compared against rows
    # that are still ACTIVE, so finished builds never block anything.
    started_by_feature: dict[str, set[str]] = {}

    async def dispatch_build(
        payload: "BuildQueuedPayload",
        ack_callback,
        register_observer=None,
        *,
        runless_replay: bool = False,
    ):
        """Persist + gate + dispatch one accepted ``BuildQueuedPayload``.

        Workflow:

        1. ``record_pending_build(payload)`` — durable QUEUED row.
           ``DuplicateBuildError`` is resolved with the R2 three-arm rule
           (plan §D4.5): a **terminal** row acks the slot (self-healing
           duplicate-terminal), an **INTERRUPTED** row (crash-mid-hop) is
           re-dispatched into the lifecycle on its existing build_id, and a
           **PAUSED / in-flight** row is skipped WITHOUT acking (the
           FEAT-FORGE-010 held-slot invariant).
        2. :func:`maybe_gate_build` — the pre-dispatch approval gate (R1:
           runs BEFORE any observer is registered or the runner launched).
        3. On approve/override/auto → register the ack handle via
           ``register_observer`` (R1 deferred registration) and launch;
           on gate-terminal → ack the slot, never launch; while the gate
           already owns the build (already paused) → hold the slot.

        ``register_observer`` is the R1 deferred bridge-registration
        closure the consumer passes when the lifecycle bridge is wired;
        it is invoked ONLY on the approve → launch path so no observer is
        live during the pause. ``None`` (no bridge) skips registration.
        ``runless_replay`` is set only by boot reconciliation after it has
        established that the previous process can no longer own the row;
        normal delivery leaves it false so BUILD policy never cancels live or
        paused work.
        """
        # Local import to avoid pinning this module's import surface to
        # nats_core when the deps factory is imported during CLI
        # ``--help`` paths (the dispatch closure is the only place the
        # payload type is exercised).
        from forge.lifecycle.persistence import DuplicateBuildError

        # A recovered (INTERRUPTED) build: its recorded run is interrupted
        # before anything else, then relaunched in place of it (see
        # launch_replacing_recorded_run).
        recovered = False

        async def _interrupted_first(build_id: str) -> bool:
            """Interrupt a recovered build's recorded run before its card,
            refusal or relaunch. ``False``: it could not be sent; show no
            card, hold the message (no ack) — the redelivery tries again."""
            if feature_routing_required:
                try:
                    if feature_routing_gate is None:
                        raise RuntimeError("required feature routing gate is not wired")
                    feature_routing_gate.require_committed_success(
                        payload.correlation_id
                    )
                except Exception as exc:  # noqa: BLE001 — supersession boundary
                    fail_mode_c_build(
                        sqlite_pool,
                        build_id,
                        summary=f"feature routing recovery refused: {exc}",
                        what="recovered build supersession",
                        log=logger,
                    )
                    logger.error(
                        "dispatch_build: recovered build_id=%s has no committed "
                        "routing authority; refusing before interrupting the "
                        "recorded run",
                        build_id,
                    )
                    return False
            if await interrupt_recorded_run(sqlite_pool, forge_config, build_id):
                return True
            logger.error(
                "dispatch_build: the earlier run of recovered build_id=%s "
                "could not be interrupted; no card, holding the message "
                "WITHOUT ack",
                build_id,
            )
            return False

        # D4's authoritative BUILD boundary is before persistence, the
        # approval gate and the conductor. It therefore also covers direct
        # boot reconciliation, which calls this closure without handle_message.
        build_policy = build_admission(forge_config, target_repo=payload.repo)
        if not build_policy.allowed:
            reason = build_policy.reason or "sandbox-required"
            try:
                existing = _read_build_identity(
                    sqlite_pool,
                    feature_id=payload.feature_id,
                    correlation_id=payload.correlation_id,
                )
            except (AttributeError, sqlite3.Error) as exc:
                # Do not escape into handle_message's generic dispatch-error
                # fallback: that emits a failure and acknowledges the slot.
                logger.error(
                    "dispatch_build: identity read failed for feature_id=%s "
                    "correlation_id=%s (%s); holding WITHOUT terminal event "
                    "or ack",
                    payload.feature_id,
                    payload.correlation_id,
                    exc,
                )
                return
            build_id = ""
            if existing is not None:
                build_id, state = existing
                if state in TERMINAL_STATES:
                    logger.info(
                        "dispatch_build: sandbox-policy replay reached terminal "
                        "build_id=%s state=%s; acking idempotently",
                        build_id,
                        state.value,
                    )
                    await ack_callback()
                    return
                if (
                    runless_replay
                    and state == BuildState.INTERRUPTED
                    and not await _interrupted_first(build_id)
                ):
                    return
                if not runless_replay:
                    # BUILD admission is not cancellation. A normal delivery
                    # cannot prove whether this existing row is owned by a
                    # runner, conductor or approval pause. That includes
                    # QUEUED/INTERRUPTED under the legacy no-gate path, whose
                    # live runs do not advance the ledger. Preserve the row
                    # and held slot; only boot reconciliation may explicitly
                    # identify an old-process row as runless.
                    logger.warning(
                        "dispatch_build: sandbox policy refuses repository "
                        "for existing build_id=%s state=%s, but the live/"
                        "paused owner is preserved; holding the queue slot "
                        "WITHOUT ack",
                        build_id,
                        state.value,
                    )
                    return
                durable_reason = reason
                fail_mode_c_build(
                    sqlite_pool,
                    build_id,
                    summary=durable_reason,
                    what="sandbox BUILD admission refusal during runless replay",
                    log=logger,
                )
                try:
                    persisted = sqlite_pool.get_build_row(build_id)
                except Exception as exc:  # noqa: BLE001 — hold the delivery
                    logger.error(
                        "dispatch_build: could not verify durable FAILED "
                        "state for runless build_id=%s (%s); holding WITHOUT "
                        "terminal event or ack",
                        build_id,
                        exc,
                    )
                    return
                if (
                    persisted is None
                    or persisted.status is not BuildState.FAILED
                    or persisted.error != durable_reason
                ):
                    logger.error(
                        "dispatch_build: runless build_id=%s did not durably "
                        "reach FAILED; holding WITHOUT terminal event or ack",
                        build_id,
                    )
                    return
                reason = durable_reason
            logger.error(
                "dispatch_build: %s; refusing before row creation, gate, "
                "observer, conductor or runner",
                reason,
            )
            if existing is None:
                # No build row will ever exist for this delivery: note the
                # refusal on the sentence's queue row so a row waiting "after"
                # it is asked "hold or go" rather than left waiting.
                from forge.adapters.nats.pipeline_consumer import (
                    note_build_rejection,
                )

                if not note_build_rejection(
                    record_build_rejection, payload.correlation_id, reason
                ):
                    # The note could not be written: hold WITHOUT ack, so
                    # the redelivery writes it.
                    return
            if lifecycle_emitter is not None:
                from forge.pipeline import BuildContext

                await lifecycle_emitter.emit_failed(
                    BuildContext(
                        feature_id=payload.feature_id,
                        build_id=build_id,
                        correlation_id=payload.correlation_id,
                        wave_total=1,
                    ),
                    failure_reason=reason,
                    recoverable=False,
                    failed_task_id=None,
                )
            await ack_callback()
            if existing is None:
                await answer_build_thread(
                    reply_in_thread,
                    payload,
                    build_refused_reply(payload.feature_id, reason),
                    level="warning",
                )
            return

        if async_task_starter is None:
            raise RuntimeError(
                "build_pipeline_consumer_deps: dispatch_build was invoked "
                "but no async_task_starter was wired. Production wiring "
                "lives in TASK-FW10-008 (Supervisor + AsyncSubAgentMiddleware); "
                "tests should pass a fake starter via the kwarg."
            )

        # A PREPARED FEATURE (4 October 2026): a build with no planning run and
        # no row yet was planned elsewhere and queued straight here. Its start,
        # target, memory and settings are established now, before the row is
        # written, at the one commit it will be built from. It runs before the
        # one-build-per-feature check below because it awaits the project's
        # remote, and that check and the row write must have no await between
        # them. A build that has a planning run, or whose row already exists
        # (a redelivery, or a row the CLI or the fix journey wrote ahead), is
        # not touched: it keeps copying its facts exactly as before.
        admitted = None
        if prepared_build_admission is not None:
            try:
                prepared = not getattr(
                    sqlite_pool, "has_planning_run", lambda _cid: True
                )(payload.correlation_id) and (
                    _read_build_identity(
                        sqlite_pool,
                        feature_id=payload.feature_id,
                        correlation_id=payload.correlation_id,
                    )
                    is None
                )
            except (AttributeError, sqlite3.Error) as exc:
                logger.error(
                    "dispatch_build: could not tell whether feature_id=%s "
                    "correlation_id=%s was planned here (%s); holding WITHOUT "
                    "terminal event or ack",
                    payload.feature_id,
                    payload.correlation_id,
                    exc,
                )
                return
            if prepared:
                answer = await prepared_build_admission(payload)
                if not getattr(answer, "ok", False):
                    reason = str(
                        getattr(answer, "refusal", None)
                        or "the prepared feature could not be admitted"
                    )
                    logger.warning(
                        "dispatch_build: prepared feature_id=%s "
                        "correlation_id=%s refused before row creation: %s",
                        payload.feature_id,
                        payload.correlation_id,
                        reason,
                    )
                    from forge.adapters.nats.pipeline_consumer import (
                        note_build_rejection,
                    )

                    if not note_build_rejection(
                        record_build_rejection, payload.correlation_id, reason
                    ):
                        # The note could not be written: hold WITHOUT ack, so
                        # the redelivery writes it.
                        return
                    if lifecycle_emitter is not None:
                        from forge.pipeline import BuildContext

                        await lifecycle_emitter.emit_failed(
                            BuildContext(
                                feature_id=payload.feature_id,
                                build_id="",
                                correlation_id=payload.correlation_id,
                                wave_total=1,
                            ),
                            failure_reason=reason,
                            recoverable=False,
                            failed_task_id=None,
                        )
                    await ack_callback()
                    await answer_build_thread(
                        reply_in_thread,
                        payload,
                        build_refused_reply(payload.feature_id, reason),
                        level="warning",
                    )
                    return
                admitted = answer.admitted
                logger.info(
                    "dispatch_build: prepared feature_id=%s admitted at %s "
                    "(target %s, memory %s)",
                    payload.feature_id,
                    admitted.source_commit,
                    admitted.target_branch,
                    admitted.memory_project,
                )

        # One build of a feature at a time (the lifecycle bridge tracks one
        # live build per feature). Every intake reaches this point, and with
        # several build places two builds of one feature could otherwise run
        # side by side. The check below and the row write after it have NO
        # await between them, so they cannot interleave with another
        # dispatch on this event loop. First come wins; rows written ahead
        # by the CLI or the fix journey go in the order they were queued.
        started = started_by_feature.setdefault(payload.feature_id, set())
        try:
            refuse, own_build_id = _another_build_of_feature_goes_first(
                sqlite_pool, payload, started
            )
        except (AttributeError, sqlite3.Error, ValueError) as exc:
            logger.error(
                "dispatch_build: could not check other builds of feature_id=%s "
                "(%s); holding WITHOUT terminal event or ack",
                payload.feature_id,
                exc,
            )
            return
        if not refuse:
            started.add(payload.correlation_id)
        else:
            reason = f"another build of {payload.feature_id} is already in progress"
            logger.warning(
                "dispatch_build: %s; refusing correlation_id=%s",
                reason,
                payload.correlation_id,
            )
            # A row of its own that had launched (so a hand-over was already
            # answered "Building") is told it was not restarted, not that it
            # was not started. Read before the row is closed.
            own_answered = own_build_id is not None and had_recorded_launch(
                sqlite_pool, own_build_id
            )
            if own_build_id is not None:
                # Its row was written ahead; close it so it never counts as
                # active work again.
                reason = fail_mode_c_build(
                    sqlite_pool,
                    own_build_id,
                    summary=reason,
                    what="a second build of the same feature",
                    log=logger,
                )
            else:
                # No build row will ever exist for this delivery: note the
                # refusal on the sentence's queue row so a row waiting "after"
                # it is asked "hold or go" rather than left waiting.
                from forge.adapters.nats.pipeline_consumer import (
                    note_build_rejection,
                )

                if not note_build_rejection(
                    record_build_rejection, payload.correlation_id, reason
                ):
                    # The note could not be written: hold WITHOUT ack, so
                    # the redelivery writes it.
                    return
            if lifecycle_emitter is not None:
                from forge.pipeline import BuildContext

                await lifecycle_emitter.emit_failed(
                    BuildContext(
                        feature_id=payload.feature_id,
                        build_id=own_build_id or "",
                        correlation_id=payload.correlation_id,
                        wave_total=1,
                    ),
                    failure_reason=reason,
                    recoverable=False,
                    failed_task_id=None,
                )
            await ack_callback()
            await answer_build_thread(
                reply_in_thread,
                payload,
                (build_not_restarted_reply if own_answered else build_refused_reply)(
                    payload.feature_id, reason
                ),
                level="warning",
            )
            return

        # Whether this delivery answers the thread it came from: True when
        # THIS delivery wrote the row, or (below) when it recovers a row
        # through its card after a restart.
        answers_thread = False
        # A recovered row that had launched — and so was already answered
        # "Building" — is never told "Building" again; if its card now ends
        # it, it is told once that it was not restarted.
        answered_before = False
        # Who is answered: the request itself, or for a recovered row its
        # persisted row (parent_request_id, originating_adapter).
        thread_target: Any = payload
        try:
            if admitted is not None:
                build_id = sqlite_pool.record_pending_build(
                    payload, admitted=admitted
                )
            else:
                build_id = sqlite_pool.record_pending_build(payload)
            answers_thread = True
        except DuplicateBuildError as exc:
            # R2 refined to THREE arms (plan §D4.5, arch-review C2): the
            # consumer's ``is_duplicate_terminal`` filter already screened the
            # terminal half, but a redelivery mid-pause (or a restart) races
            # here. Read the row and branch:
            #   * terminal → ack (self-heals; releases the slot);
            #   * INTERRUPTED → re-enter the lifecycle (crash-mid-hop): a row
            #     left INTERRUPTED inside the QUEUED→…→PAUSED hop window would,
            #     under arm 2's skip-WITHOUT-ack, wedge the consumer forever
            #     (max_ack_pending=1). Re-dispatch instead — ``maybe_gate_build``
            #     drives INTERRUPTED→PREPARING→RUNNING via ``transition_chain``.
            #   * PAUSED / in-flight → skip WITHOUT ack — the held slot is
            #     load-bearing (FEAT-FORGE-010); the pause / rearm path owns
            #     the eventual terminal ack.
            status = _read_build_status(
                sqlite_pool,
                feature_id=payload.feature_id,
                correlation_id=payload.correlation_id,
            )
            if status is not None and status in TERMINAL_STATES:
                logger.info(
                    "dispatch_build: duplicate TERMINAL build feature_id=%s "
                    "correlation_id=%s status=%s (%s); acking to release "
                    "the queue slot",
                    payload.feature_id,
                    payload.correlation_id,
                    status.value,
                    exc,
                )
                await ack_callback()
                return
            # Is the pre-dispatch approval gate wired this boot? The
            # runless-re-dispatch arms below rely on ``maybe_gate_build``
            # driving QUEUED/INTERRUPTED → RUNNING via ``transition_chain``
            # BEFORE the runner launches. When the gate is soft-failed
            # (DDR-007, serve._compose) dispatch falls back to the legacy
            # no-gate launch, which does NOT advance ``builds.status`` — a
            # LIVE build then keeps its row at QUEUED/INTERRUPTED for the
            # whole run, so re-dispatching a redelivery would DOUBLE-LAUNCH
            # it (FWD-003 merge-review finding). Re-dispatch only when the
            # gate is wired; otherwise hold the slot (safe; the gate re-wires
            # next boot).
            from forge.cli._serve_deps_gating import (
                bound_gate_parts as _bound_gate_parts,
            )

            gate_wired = (
                _bound_gate_parts() is not None
                and gate_repository is not None
                and gate_state_machine is not None
            )
            if gate_wired and status in (
                BuildState.INTERRUPTED,
                BuildState.QUEUED,
            ):
                # Arm 3 (crash-mid-hop) extended by FWD-003
                # (restart-mid-dispatch): an INTERRUPTED row (crash inside a
                # QUEUED→…→PAUSED hop) OR a row still QUEUED (the original
                # dispatch was interrupted BEFORE it progressed the row — the
                # 2026-07-06 restart-mid-dispatch freeze, deploy-record
                # c042bee) is RUNLESS: no live run streams it and no pause
                # owns the eventual ack. In the GATED path a live build has
                # already advanced past QUEUED (→ RUNNING), so a QUEUED/
                # INTERRUPTED duplicate here is definitively runless.
                # ``record_pending_build`` cannot re-insert, so re-derive the
                # deterministic build_id and fall through to the gate flow on
                # the EXISTING row (``maybe_gate_build`` drives it forward).
                # Never skip-WITHOUT-ack for these: under
                # ``max_ack_pending=1`` the un-acked redelivery wedges the
                # consumer until the 1h ``ack_wait`` expiry (the freeze
                # self-cleared only at expiry; the 123f1f7 unfreeze note).
                from forge.lifecycle.identifiers import derive_build_id

                build_id = derive_build_id(payload.feature_id, payload.queued_at)
                # A row recovered after a restart is answered from its
                # persisted row (parent_request_id, originating_adapter). The
                # persisted fact of a prior launch is its ``async_tasks`` row
                # (``had_recorded_launch``), read here, before the replacement
                # launch deletes it: a row with one was already answered
                # "Building" and is never told so again; a row without one
                # (the forge stopped after the row was written, before or at
                # its card) was never answered and is answered once now. A
                # forge that stopped in the instant between the launch and its
                # answer stays silent rather than risk saying it twice.
                answers_thread = True
                answered_before = had_recorded_launch(sqlite_pool, build_id)
                thread_target = _row_or(sqlite_pool, build_id, payload)
                recovered = status == BuildState.INTERRUPTED
                if recovered and not await _interrupted_first(build_id):
                    return
                logger.info(
                    "dispatch_build: duplicate %s build feature_id=%s "
                    "correlation_id=%s build_id=%s (%s); re-dispatching into "
                    "the lifecycle (runless re-dispatch — restart/crash-"
                    "mid-dispatch recovery)",
                    status.value,
                    payload.feature_id,
                    payload.correlation_id,
                    build_id,
                    exc,
                )
                # Fall through to the gate flow on the EXISTING row.
            else:
                # Held slot (skip WITHOUT ack). Covers PAUSED (the pause /
                # rearm path owns the eventual terminal ack, FEAT-FORGE-010),
                # any genuinely live PREPARING/RUNNING/FINALISING row, AND —
                # per the gate_wired guard above — a QUEUED/INTERRUPTED
                # duplicate while the gate is unwired (where re-dispatch would
                # double-launch a live no-gate build).
                logger.warning(
                    "dispatch_build: duplicate active build feature_id=%s "
                    "correlation_id=%s status=%s gate_wired=%s (%s); holding "
                    "the queue slot WITHOUT ack (held-slot invariant — a "
                    "pause / live run owns the eventual terminal ack)",
                    payload.feature_id,
                    payload.correlation_id,
                    status.value if status is not None else "unknown",
                    gate_wired,
                    exc,
                )
                return

        # Routing is an admission barrier too.  It runs after the durable
        # build identity exists but before budget/approval cards, recovery
        # replacement or either launch arm.  The launch gate below re-reads
        # the committed receipt and therefore performs no second HTTP seed.
        if feature_routing_required:
            routing_id = payload.correlation_id or build_id
            routing_reason: str | None = None
            if feature_routing_gate is None:
                routing_reason = "required feature routing gate is not wired"
            else:
                try:
                    await feature_routing_gate.ensure_seeded(
                        routing_id,
                        origin_kind="build",
                        origin_id=build_id,
                    )
                except Exception as exc:  # noqa: BLE001 — admission boundary
                    routing_reason = (
                        "feature routing admission failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
            if routing_reason is not None:
                durable_reason = fail_mode_c_build(
                    sqlite_pool,
                    build_id,
                    summary=routing_reason,
                    what="feature routing admission",
                    log=logger,
                )
                persisted = sqlite_pool.get_build_row(build_id)
                if persisted is None or persisted.status is not BuildState.FAILED:
                    logger.error(
                        "dispatch_build: routing refusal for build_id=%s did "
                        "not durably reach FAILED; holding WITHOUT card, "
                        "runner or ack",
                        build_id,
                    )
                    return
                if lifecycle_emitter is not None:
                    from forge.pipeline import BuildContext

                    await lifecycle_emitter.emit_failed(
                        BuildContext(
                            feature_id=payload.feature_id,
                            build_id=build_id,
                            correlation_id=payload.correlation_id,
                            wave_total=1,
                        ),
                        failure_reason=durable_reason,
                        recoverable=False,
                        failed_task_id=None,
                    )
                await ack_callback()
                return

        # FEAT-UBS-002 (Option-B, stage 1) — resolve the per-build budget entry
        # ONCE now that ``build_id`` is final (freshly recorded or re-derived on
        # the runless re-dispatch arm above), so BOTH launch branches — the
        # legacy no-gate launch and the gate-approved launch — attach the SAME
        # compact budget dict to the launch payload. Attended / NULL profile →
        # None (byte-equivalent launch, ASSUM-010). Resolve is fail-open: the
        # helper reads the just-persisted row; an unresolvable profile yields
        # None rather than blocking dispatch.
        budget_entry = _resolve_launch_budget(sqlite_pool, forge_config, build_id)

        # FEAT-UBS-002 (Option-B, stage 3, GATE) — pre-dispatch breach gate.
        # A feature whose PRIOR build hit a budget cap (an un-cleared
        # ``builds.budget_breach``) must NOT quietly launch a fresh build that
        # would spend the same budget over again. Read the feature's
        # outstanding breach ONCE now, before the launch decision, so BOTH
        # branches honour it. ``None`` — no prior breach, or one a human already
        # cleared — is the common case and yields ZERO new behaviour: the
        # byte-equivalent no-breach path this stage preserves. Enforcement then
        # lives only where a state is HONEST: with the gate wired the build sits
        # genuinely PAUSED at the approval gate (below), and an approve is the
        # act that clears the breach; with the gate soft-failed there is no
        # honest PAUSED seam, so a breach is REFUSED outright (never a fake
        # pause). Note: today's degraded gate mandates human approval for EVERY
        # dispatch (DF-009 "v1 never auto-approves"), so a breached re-queue on
        # the wired path inherently pauses for a ruling; forcing a human ruling
        # SPECIFICALLY for a breach even under a future auto-approve posture
        # would need breach context threaded into the gate decision (a
        # gate_check redesign, out of this lane's scope).
        prior_breach = sqlite_pool.latest_breach_for_feature(payload.feature_id)

        async def _say_building() -> None:
            """The hand-over's one success answer, after the launch returned."""
            if not answers_thread or answered_before:
                return
            if admitted is not None:
                commit = admitted.source_commit
            else:
                # A recovered row: the commit it was admitted at, as recorded.
                try:
                    commit = getattr(sqlite_pool, "read_source_commit")(build_id)
                except Exception:  # noqa: BLE001 — the line without a commit
                    commit = None
            await answer_build_thread(
                reply_in_thread,
                thread_target,
                build_started_reply(
                    payload.feature_id,
                    payload.repo,
                    payload.branch,
                    commit,
                ),
            )

        # --- Pre-dispatch approval gate (TASK-GATE-D659, R1) -------------
        from forge.cli import _serve_deps_gating, _serve_gate_activation

        parts = _serve_deps_gating.bound_gate_parts()
        if parts is None or gate_repository is None or gate_state_machine is None:
            # Soft-fail: the approval seam is not wired (a v1.1 gate defect
            # must never brick v1 dispatch — see serve.py _compose). Fall
            # back to legacy no-gate launch so the build still runs.
            #
            # SILENT-DOWNGRADE SEAM 4 (activation design §4.4) — checked
            # FIRST, before every other refusal on this arm. The DDR-007
            # posture ("gate composition must never brick v1 dispatch") is
            # the RULED posture for ROUTINE builds and stays exactly as it
            # is. It is NOT the posture for a fix journey: the fix
            # journey's whole safety story is the pre-dispatch card
            # (DF-009, "v1 never auto-approves"), so on a boot where the
            # gate soft-failed to compose, letting the router take a mode-c
            # build would open an UNATTENDED journey. For mode-c the gate
            # is load-bearing, not best-effort — refuse loudly instead.
            if is_mode_c_build(sqlite_pool, build_id, log=logger):
                summary = (
                    "a fix-journey (mode-c) build reached dispatch on a boot "
                    "where the pre-dispatch approval gate is NOT wired — "
                    "refused rather than opening an UNATTENDED journey"
                )
                logger.error(
                    "dispatch_build: build_id=%s feature_id=%s is a fix "
                    "journey but the approval gate is NOT wired this boot "
                    "(parts=%s repo=%s sm=%s); REFUSING — the fix journey's "
                    "safety story is the pre-dispatch card, so for mode-c the "
                    "gate is load-bearing, not best-effort",
                    build_id,
                    payload.feature_id,
                    parts is not None,
                    gate_repository is not None,
                    gate_state_machine is not None,
                )
                await _conductor_terminal(
                    TakenTerminal(
                        reason=fail_mode_c_build(
                            sqlite_pool,
                            build_id,
                            summary=summary,
                            what="a mode-c dispatch with no approval gate wired",
                            log=logger,
                        )
                    ),
                    build_id=build_id,
                    feature_id=payload.feature_id,
                    correlation_id=payload.correlation_id,
                    ack_callback=ack_callback,
                )
                return
            if prior_breach is not None:
                prior_build_id, breach_detail = prior_breach
                # Stage-3 GATE, no-gate arm: the one seam where PAUSED is honest
                # is unavailable this boot, and this lane never marks a row
                # PAUSED it cannot own. A breached re-queue must not launch
                # silently, so REFUSE loudly — raise; the consumer publishes a
                # terminal build-failed (carrying this breach reason) and acks
                # the slot (pipeline_consumer.handle_message raise-before-
                # transition convention). The operator rules, then re-queues.
                logger.error(
                    "dispatch_build: feature_id=%s carries an un-cleared budget "
                    "breach from build_id=%s (%s) and the approval gate is NOT "
                    "wired this boot (new build_id=%s); REFUSING to launch a "
                    "fresh build silently — no honest PAUSED seam is available "
                    "(UBS-002 stage 3)",
                    payload.feature_id,
                    prior_build_id,
                    breach_detail,
                    build_id,
                )
                raise BudgetBreachDispatchRefused(
                    feature_id=payload.feature_id,
                    prior_build_id=prior_build_id,
                    breach_detail=breach_detail,
                )
            logger.warning(
                "dispatch_build: approval gate not wired (parts=%s repo=%s "
                "sm=%s) for build_id=%s; launching WITHOUT a gate (legacy)",
                parts is not None,
                gate_repository is not None,
                gate_state_machine is not None,
                build_id,
            )
            if register_observer is not None:
                await _safe_register_observer(register_observer, build_id)
            await launch_or_conduct(
                # Threaded so a TAKEN_TERMINAL on THIS arm acks and emits
                # exactly as it does on the gate-approved arm (§3: "both
                # launch_or_conduct call sites consume the outcome").
                ack_callback=ack_callback,
                build_id=build_id,
                feature_id=payload.feature_id,
                correlation_id=payload.correlation_id,
                branch=payload.branch,
                repo=payload.repo,
                budget=budget_entry,
            )
            await _say_building()
            return

        outcome = await _serve_gate_activation.maybe_gate_build(
            parts=parts,
            sqlite_pool=sqlite_pool,
            gate_repository=gate_repository,
            gate_state_machine=gate_state_machine,
            build_id=build_id,
            feature_id=payload.feature_id,
            correlation_id=payload.correlation_id,
            clock=clock,
        )

        if outcome in (
            _serve_gate_activation.ALREADY_PAUSED,
            _serve_gate_activation.HOLD_SLOT,
        ):
            # Hold the slot — no launch, no ack, and (crucially) no build-failed
            # emit. ALREADY_PAUSED: a rearm / redelivery re-entry the rearm path
            # owns. HOLD_SLOT: the SQLite PAUSED row is durable but the AGENTS
            # publish failed (rearm re-emits next boot) or a synthetic hop lost
            # the race to a concurrent terminal write (the other writer is
            # authoritative). Letting either escape to handle_message would emit
            # a factually-wrong build-failed and prematurely ack.
            return

        if _serve_gate_activation.outcome_launches(outcome):
            # Approve / override / auto → R1 deferred observer registration
            # (adjacent to launch, so identity resolves the fresh run) then
            # launch the autobuild runner.
            if prior_breach is not None:
                prior_build_id, breach_detail = prior_breach
                # Stage-3 GATE 'cleared' semantics: the gate APPROVED this
                # re-queue — a human ruling — so approving the gated build IS
                # the clearing act for the feature going forward. Annotate the
                # prior build's breach CLEARED (history-preserving; the detected
                # record stays) BEFORE launch so the feature's next re-queue is
                # not re-gated on a breach a human has already ruled on. A gate
                # TERMINAL instead (the else branch) leaves the breach standing.
                sqlite_pool.clear_budget_breach(prior_build_id, clock().isoformat())
                logger.info(
                    "dispatch_build: gate APPROVED a re-queue of feature_id=%s "
                    "which carried an un-cleared budget breach from build_id=%s "
                    "(%s); cleared it (history retained) — the human ruling is "
                    "the clearing act (UBS-002 stage 3)",
                    payload.feature_id,
                    prior_build_id,
                    breach_detail,
                )
            logger.info(
                "dispatch_build: gate approved build_id=%s outcome=%s; "
                "registering observer + launching autobuild",
                build_id,
                outcome.value,
            )
            async def _register_and_launch() -> None:
                if register_observer is not None:
                    await _safe_register_observer(register_observer, build_id)
                await launch_or_conduct(
                    ack_callback=ack_callback,
                    build_id=build_id,
                    feature_id=payload.feature_id,
                    correlation_id=payload.correlation_id,
                    branch=payload.branch,
                    repo=payload.repo,
                    budget=budget_entry,
                )

            if not recovered:
                await _register_and_launch()
            elif not await launch_replacing_recorded_run(
                sqlite_pool, build_id, _register_and_launch
            ):
                return
            await _say_building()
        else:
            # Gate terminal (reject / expiry / hard-stop) — the build never
            # started; ack the slot so the next queued build proceeds and
            # never register the bridge observer.
            if prior_breach is not None:
                # Stage-3 GATE: the human did NOT approve the re-run, so the
                # feature's outstanding breach STANDS (not cleared) — the next
                # re-queue is gated again. Purely observational; the terminal
                # ack below is unchanged.
                logger.info(
                    "dispatch_build: gate TERMINAL (outcome=%s) for a re-queue "
                    "of feature_id=%s carrying an un-cleared budget breach from "
                    "build_id=%s; the breach STANDS (not cleared) (UBS-002 "
                    "stage 3)",
                    outcome.value,
                    payload.feature_id,
                    prior_breach[0],
                )
            logger.info(
                "dispatch_build: gate terminal build_id=%s outcome=%s; "
                "acking slot, not launching",
                build_id,
                outcome.value,
            )
            await ack_callback()
            # A hand-over whose build-start card ended it is told so, once.
            # (After a restart mid-card, ``rearm_paused_gates`` answers
            # instead, from the build row.)
            if answers_thread:
                ended = (
                    build_not_restarted_reply
                    if answered_before
                    else build_refused_reply
                )
                await answer_build_thread(
                    reply_in_thread,
                    thread_target,
                    ended(payload.feature_id, gate_ended_reason(outcome)),
                    level="warning",
                )

    return dispatch_build


def _row_or(sqlite_pool: Any, build_id: str, fallback: Any) -> Any:
    """The persisted build row, or ``fallback`` when it cannot be read."""
    try:
        row = sqlite_pool.get_build_row(build_id)
    except Exception:  # noqa: BLE001 — the request carries the same facts
        row = None
    return row if row is not None else fallback


#: How long a launch waits before asking the lifecycle bridge again when it
#: refused this build's observer because an earlier build of the same
#: feature is still finishing (its observer not yet done). Tests shorten it.
OBSERVER_BUSY_RETRY_SECONDS: float = 5.0


async def _safe_register_observer(register_observer, build_id: str) -> None:
    """Invoke the R1 deferred bridge-registration closure; wait if refused.

    Registration is best-effort (the bridge owns its own observability and
    the legacy ack path still works), so a raising ``register_observer``
    must not abort the launch — mirrors the consumer's pre-relocation
    ``register_ack_handle`` guard.

    A *refusal* is different: the bridge answers ``False`` when another
    build of the same feature still has a live observer (it tracks one per
    feature, and the earlier build's observer may still be finishing its
    terminal). Launching then would leave this build with no
    observer and no acknowledgement, so the launch waits here, asking again
    every :data:`OBSERVER_BUSY_RETRY_SECONDS`, until the earlier observer
    has finished. The build keeps its message un-acked meanwhile; shutdown
    cancels the wait with the dispatch task.
    """
    while True:
        try:
            accepted = await register_observer()
        except Exception as exc:  # noqa: BLE001 — best-effort registration
            logger.warning(
                "dispatch_build: deferred observer registration raised (%s) for "
                "build_id=%s; continuing with legacy ack_callback fallback",
                exc,
                build_id,
            )
            return
        if accepted is not False:
            return
        logger.info(
            "dispatch_build: build_id=%s waits to launch — an earlier build of "
            "the same feature is still finishing; asking again in %ss",
            build_id,
            OBSERVER_BUSY_RETRY_SECONDS,
        )
        await asyncio.sleep(OBSERVER_BUSY_RETRY_SECONDS)


def _build_publish_build_failed(
    publisher,
    *,
    terminal_publish_ledger: TerminalPublishLedger | None = None,
):
    """Return an ``async (failure_payload, feature_id, *, correlation_id)`` wrapper.

    The consumer's
    :data:`~forge.adapters.nats.pipeline_consumer.PublishBuildFailed`
    type alias passes ``feature_id`` separately for symmetry with the
    other failure subjects in the API contract; the publisher derives
    the subject from ``payload.feature_id`` itself, so the wrapper
    swallows the second positional argument after asserting the two
    agree (defence-in-depth — a mismatched pair is a contract bug
    upstream rather than a publish error).

    DDR-029 — the inbound envelope's ``correlation_id`` is threaded
    through to the outbound envelope by attaching it to the v1
    ``BuildFailedPayload`` via :func:`attach_correlation_id` before the
    publisher reads it back through ``getattr(payload, "correlation_id")``.
    The publisher's central ``_publish_envelope`` then writes it onto the
    outbound :class:`MessageEnvelope`. ``correlation_id=None`` is only
    accepted on the malformed-envelope path where no source value is
    available; every other rejection path threads the inbound value
    explicitly.

    TASK-FRR-PEB-005 — F010F coexistence boundary
    ---------------------------------------------

    When a :class:`TerminalPublishLedger` is wired (production boot path
    for ``forge serve``), the wrapper consults
    :meth:`TerminalPublishLedger.claim` **before** invoking the
    publisher. If the bridge's async-terminal observation already
    claimed the slot for the same ``(feature_id, correlation_id)``,
    ``claim`` returns ``False`` and the wrapper short-circuits without
    publishing. This pins the no-double-emit invariant for every
    ordering: bridge-first, F010F-first, and concurrent.

    The ledger is **optional** so paths that never wire the bridge
    (legacy unit tests, ``forge dispatch`` shell-out) keep their
    F010F-only semantics: the wrapper publishes unconditionally and
    F010F's existing test suite passes unchanged (AC-4).

    The ``correlation_id=None`` malformed-envelope path is exempt from
    the claim check — without a real correlation_id there is nothing
    to coordinate against, and the bridge would never have observed
    such an envelope to begin with.
    """

    from forge.pipeline import attach_correlation_id

    async def publish_build_failed(
        failure_payload: "BuildFailedPayload",
        feature_id: str,
        *,
        correlation_id: str | None,
    ) -> None:
        """Publish ``pipeline.build-failed.{feature_id}`` via the shared publisher.

        Args:
            failure_payload: The :class:`BuildFailedPayload` describing the
                rejection.
            feature_id: Subject-construction key. Must equal
                ``failure_payload.feature_id``; a mismatch is logged and
                the publisher's payload-derived subject wins.
            correlation_id: Inbound envelope ``correlation_id`` (DDR-029).
                Attached to the v1 payload via
                :func:`attach_correlation_id` so the publisher's
                envelope-construction path threads it onto the outbound
                envelope. ``None`` only on the malformed-envelope rejection
                path where no source value is available.
        """
        if failure_payload.feature_id != feature_id:
            # Surface contract bug rather than publish to a subject the
            # caller did not intend; the publisher will derive
            # ``feature_id`` from ``failure_payload`` regardless.
            logger.warning(
                "publish_build_failed: feature_id mismatch payload=%s arg=%s; "
                "publishing on payload.feature_id (publisher-derived)",
                failure_payload.feature_id,
                feature_id,
            )

        # TASK-FRR-PEB-005 AC-2 / AC-3 — first-wins terminal-publish
        # claim. Skip the publish only when a ledger is wired AND the
        # caller has a real ``correlation_id`` (the malformed-envelope
        # path threads ``None`` and is exempt: there is no
        # ``(feature_id, correlation_id)`` pair to coordinate against
        # so the bridge could not possibly have observed it).
        if terminal_publish_ledger is not None and correlation_id is not None:
            won = terminal_publish_ledger.claim(
                feature_id=failure_payload.feature_id,
                correlation_id=correlation_id,
                claimed_by=CLAIMER_F010F_SAFETY_NET,
            )
            if not won:
                logger.info(
                    "publish_build_failed: terminal-publish slot already "
                    "claimed for feature_id=%s correlation_id=%s; "
                    "skipping F010F safety-net emit (TASK-FRR-PEB-005)",
                    failure_payload.feature_id,
                    correlation_id,
                )
                return

        if correlation_id is not None:
            attach_correlation_id(failure_payload, correlation_id)
        await publisher.publish_build_failed(failure_payload)

    return publish_build_failed


def _prepared_feature_file_refusal(
    raw: str, *, feature_id: str, repo: str, checkout: str
) -> str | None:
    """``None`` when ``raw`` names ``<checkout>/.guardkit/features/<id>.yaml``.

    A relative path must be exactly that repository path; an absolute one must
    be that file in the registered checkout (compared lexically and, where the
    paths exist, as resolved). Anything else is one plain sentence.
    """
    import os
    import posixpath
    from pathlib import Path

    from forge.pipeline.prepared_admission import feature_yaml_relpath

    wanted_rel = feature_yaml_relpath(feature_id)
    wanted_abs = os.path.normpath(os.path.join(checkout, wanted_rel))
    if raw and (raw[0].isspace() or raw[-1].isspace()):
        # Read exactly as written, never trimmed into the right file (R8).
        return (
            f"its feature file {raw!r} begins or ends with a space; name "
            f"{wanted_rel} in {repo}'s registered checkout exactly"
        )
    if not raw:
        matches = False
    elif os.path.isabs(raw):
        given = os.path.normpath(raw)
        matches = given == wanted_abs
        if not matches:
            try:
                matches = Path(given).resolve() == Path(wanted_abs).resolve()
            except (OSError, ValueError):
                matches = False
    else:
        matches = posixpath.normpath(raw.replace("\\", "/")) == wanted_rel
    if matches:
        return None
    return (
        f"its feature file {raw or '(none)'} is not {wanted_rel} in {repo}'s "
        f"registered checkout ({wanted_abs}), the one file a build of "
        f"{feature_id} reads"
    )


def build_prepared_build_admission(
    forge_config: ForgeConfig,
    *,
    git_runner: Any = None,
) -> Callable[[Any], Awaitable[Any]]:
    """``async (payload) -> AdmissionAnswer`` for a feature planned elsewhere.

    4 October 2026 (project initialisation, Part 6). The repository's checkout
    is looked up exactly as the planning door looks it up
    (``planning.target_repo_paths``), and the git runner is composed exactly as
    the planning door's is (:func:`forge.cli._serve_planning.
    compose_planning_git_runner`): the sandbox's sidecar for a sandboxed
    repository, the coordinator's own runner otherwise. ``git_runner`` lets a
    test hand in its own.
    """
    from forge.pipeline.prepared_admission import (
        AdmissionAnswer,
        admit_prepared_build,
    )

    runners: list[Any] = [git_runner] if git_runner is not None else []

    def _runner() -> Any:
        # Composed on first use, so composing the daemon never depends on it.
        if not runners:
            from forge.adapters.git.planning_runner import WorktreeGitRunner
            from forge.cli._serve_planning import compose_planning_git_runner

            composed, _resolver = compose_planning_git_runner(
                forge_config.planning, worktree_runner_factory=WorktreeGitRunner
            )
            runners.append(composed)
        return runners[0]

    async def admit(payload: Any) -> Any:
        # A prepared feature is a whole feature, never a single-task fix: a
        # fix journey writes its own row before it is queued, so one arriving
        # here unplanned and marked mode-c is refused.
        if str(getattr(payload, "mode", None) or "mode-a") == "mode-c":
            return AdmissionAnswer(
                refusal=(
                    f"{payload.feature_id} was queued as a single-task fix "
                    f"(mode-c) with nothing behind it; a feature planned "
                    f"elsewhere is queued as a whole feature"
                )
            )
        repo = str(payload.repo)
        repo_path = (forge_config.planning.target_repo_paths or {}).get(repo)
        if not repo_path:
            return AdmissionAnswer(
                refusal=(
                    f"the repository {repo} is not registered with this "
                    f"factory (planning.target_repo_paths), so a feature "
                    f"planned elsewhere cannot be admitted for it"
                )
            )
        # The ONE file a build of this feature reads (Codex review round 1,
        # R4): a prepared submission's feature_yaml_path, absolute or relative,
        # must name the registered checkout's .guardkit/features/<id>.yaml —
        # never another feature's file, never another repository's. A planned
        # build never reaches here and keeps today's acceptance.
        wrong = _prepared_feature_file_refusal(
            str(getattr(payload, "feature_yaml_path", "") or ""),
            feature_id=str(payload.feature_id),
            repo=repo,
            checkout=str(repo_path),
        )
        if wrong is not None:
            return AdmissionAnswer(refusal=wrong)
        try:
            runner = _runner()
        except Exception as exc:  # noqa: BLE001 — a refusal, never a crash
            return AdmissionAnswer(
                refusal=(
                    f"the git runner for {repo} could not be set up: "
                    f"{type(exc).__name__}: {exc}"
                )
            )
        return await admit_prepared_build(
            runner,
            repo=repo,
            repo_path=str(repo_path),
            feature_id=str(payload.feature_id),
            branch=str(payload.branch),
        )

    return admit


def _work_queue_rejection_recorder(
    sqlite_pool: Any,
) -> Callable[[str, str], bool]:
    """``(correlation_id, reason)`` that notes a refused build on its queue row.

    Uses the lifecycle pool's own connection — the work queue lives in the same
    Forge database — and leaves the connection's row factory as it found it.
    """

    def _record(correlation_id: str, reason: str) -> bool:
        from forge.planning.work_queue_store import WorkQueueStore

        connection = sqlite_pool.connection
        previous = connection.row_factory
        try:
            return WorkQueueStore(connection).record_build_rejection(
                correlation_id, reason
            )
        finally:
            connection.row_factory = previous

    return _record


def build_pipeline_consumer_deps(
    client: Any,
    forge_config: ForgeConfig,
    sqlite_pool: SqliteLifecyclePersistence,
    *,
    async_task_starter: AsyncTaskStarter | None = None,
    register_ack_handle: InFlightAckRegistry | None = None,
    terminal_publish_ledger: TerminalPublishLedger | None = None,
    publisher: PipelinePublisher | None = None,
    gate_repository: Any = None,
    gate_state_machine: Any = None,
    gate_clock: Callable[[], datetime] | None = None,
    conductor_router: Callable[..., Any] | None = None,
    record_build_rejection: Callable[[str, str], Any] | None = None,
    prepared_build_admission: Callable[[Any], Awaitable[Any]] | None = None,
    reply_in_thread: BuildThreadReply | None = None,
    feature_routing_gate: Any | None = None,
    feature_routing_required: bool = False,
) -> PipelineConsumerDeps:
    """Compose the production :class:`PipelineConsumerDeps` for ``forge serve``.

    Wires the four fields of
    :class:`~forge.adapters.nats.pipeline_consumer.PipelineConsumerDeps`
    against the daemon's shared collaborators:

    * ``forge_config`` — passed straight through.
    * ``is_duplicate_terminal`` — SQLite read closure built by
      :func:`_build_is_duplicate_terminal`.
    * ``dispatch_build`` — autobuild dispatch closure built by
      :func:`_build_dispatch_build`. Internally composes the three
      Wave-2 Protocol collaborators and the (caller-injected)
      :class:`AsyncTaskStarter`.
    * ``publish_build_failed`` — wrapper around the
      :class:`PipelinePublisher` constructed via
      :func:`build_publisher_and_emitter` against the shared NATS
      client.

    Args:
        client: The pre-opened NATS client owned by ``_run_serve``
            (ASSUM-011: exactly one connection per daemon).
        forge_config: Validated :class:`ForgeConfig`. Used by the
            consumer for ``approved_originators`` /
            ``permissions.filesystem.allowlist`` and by the
            forward-context builder for the worktree allowlist.
        sqlite_pool: The shared
            :class:`SqliteLifecyclePersistence` facade. Provides:

            - ``record_pending_build`` for the dispatch closure;
            - ``record_stage`` (via the stage-log recorder factory)
              for the FW10-004 collaborator;
            - the ``async_tasks`` SQLite mirror (via the FW10-005
              factory);
            - read-only ``builds`` reads for duplicate detection.
        async_task_starter: Optional
            :class:`AsyncTaskStarter` used by the autobuild dispatch
            closure. Production wiring is provided by TASK-FW10-008
            (Supervisor + AsyncSubAgentMiddleware); tests pass a
            deterministic fake. When ``None``, calling
            ``deps.dispatch_build`` raises :class:`RuntimeError` so a
            missing wiring surfaces during the first dispatch rather
            than silently dropping the build.
        conductor_router: Optional ``async (**launch_kwargs) ->
            ConductorOutcome | TakenTerminal`` seam (conductor revival
            Stage 1c; widened to the taken-and-terminal vocabulary by the
            activation lane, design §3). ``None`` — the default,
            and what the composition root passes while
            ``conductor.enabled`` is off — leaves the dequeue path
            byte-for-byte today's: every accepted build goes straight to
            the routine autobuild launch. See
            :func:`_build_dispatch_build`.
        prepared_build_admission: Optional ``async (payload) ->
            AdmissionAnswer`` for a build with no planning run (a feature
            planned elsewhere). Production passes
            :func:`build_prepared_build_admission`; ``None`` admits nothing
            and records nothing, as before. See :func:`_build_dispatch_build`.
        reply_in_thread: Optional ``async (payload, message, *, level) ->
            None`` that answers a build request in the conversation it came
            from (register-projects design, 5 October 2026, part 3). ``None``
            takes the production answer on ``client``
            (:func:`forge.planning.notifications.make_build_thread_reply`);
            either way a request without ``parent_request_id`` is never
            answered.

    Returns:
        A fully wired
        :class:`~forge.adapters.nats.pipeline_consumer.PipelineConsumerDeps`.

    Raises:
        ValueError: When ``client`` is ``None`` (the daemon is
            responsible for opening exactly one client and sharing it).
    """
    if client is None:
        raise ValueError(
            "build_pipeline_consumer_deps: 'client' must be a connected "
            "NATS client; got None. The daemon owns exactly one client "
            "(ASSUM-011) and shares it with this factory; never call "
            "with None."
        )
    if forge_config is None:
        raise ValueError("build_pipeline_consumer_deps: 'forge_config' is required")
    if sqlite_pool is None:
        raise ValueError("build_pipeline_consumer_deps: 'sqlite_pool' is required")

    # 1. Compose the three Wave-2 Protocol collaborators against the
    #    shared SQLite pool + ForgeConfig. Each factory is idempotent
    #    and side-effect free apart from the ``async_tasks`` schema
    #    DDL applied by the state-channel initialiser (which uses
    #    ``CREATE TABLE IF NOT EXISTS``).
    #
    #    ``build_stage_log_reader`` wraps the shared pool in the narrow
    #    :class:`StageLogReader` Protocol surface the forward-context
    #    builder consumes (TASK-FORGE-FRR-F010B). Before this wrapper
    #    existed, the bare facade was handed to the builder and the
    #    first ``build_for`` call raised AttributeError because
    #    :class:`SqliteLifecyclePersistence` does not expose
    #    ``get_approved_stage_entry`` itself.
    stage_log_reader = build_stage_log_reader(sqlite_pool)
    forward_context_builder = build_forward_context_builder(
        stage_log_reader, forge_config
    )
    stage_log_recorder = build_stage_log_recorder(sqlite_pool)
    state_channel = build_autobuild_state_initialiser(sqlite_pool)

    # 2. Build the publisher + emitter pair against the shared NATS
    #    client. The publisher backs ``publish_build_failed``; the
    #    emitter is threaded onto the autobuild dispatch closure
    #    (DDR-007 Option A — in-process Python object via the
    #    ``start_async_task`` context payload) so the runner's
    #    lifecycle transitions (starting → planning_waves → ...) emit
    #    on the same NATS connection that wrote the ``stage_log`` row.
    #
    #    TASK-FORGE-FRR-PEBR-WIREUP — when the caller injects a
    #    ``publisher`` (the production path: ``bind_production_dispatch_chain``
    #    constructs the publisher inside its closure so the
    #    :class:`LifecycleBridgeWireup` shares the SAME publisher
    #    instance as ``publish_build_failed`` — necessary for the
    #    no-double-emit invariant), use the injected publisher and
    #    construct an emitter wrapping it. When ``publisher is None``
    #    (legacy / unit-test path), fall back to the original factory
    #    for both.
    if publisher is not None:
        pipeline_config = (
            forge_config.pipeline
            if forge_config.pipeline is not None
            else PipelineConfig()
        )
        emitter = PipelineLifecycleEmitter(
            publisher=publisher,
            config=pipeline_config,
        )
    else:
        publisher, emitter = build_publisher_and_emitter(
            client, config=forge_config.pipeline
        )

    # 3. Build the four field closures.
    # A build refused before its row is written is noted on the sentence's
    # work-queue row (the same Forge database), so nothing waits on it for
    # ever. Production takes the default; a test may hand in its own.
    if record_build_rejection is None:
        record_build_rejection = _work_queue_rejection_recorder(sqlite_pool)
    # The answer in the thread a build was handed over from, on the daemon's
    # one client. A request without parent_request_id is never answered.
    if reply_in_thread is None:
        reply_in_thread = make_build_thread_reply(client)
    is_duplicate_terminal = _build_is_duplicate_terminal(sqlite_pool)
    retired_planning_handoffs = _build_retired_planning_handoffs_reader(
        sqlite_pool
    )
    dispatch_build = _build_dispatch_build(
        sqlite_pool=sqlite_pool,
        forward_context_builder=forward_context_builder,
        stage_log_recorder=stage_log_recorder,
        state_channel=state_channel,
        lifecycle_emitter=emitter,
        async_task_starter=async_task_starter,
        forge_config=forge_config,
        gate_repository=gate_repository,
        gate_state_machine=gate_state_machine,
        gate_clock=gate_clock,
        conductor_router=conductor_router,
        record_build_rejection=record_build_rejection,
        prepared_build_admission=prepared_build_admission,
        reply_in_thread=reply_in_thread,
        feature_routing_gate=feature_routing_gate,
        feature_routing_required=feature_routing_required,
    )
    publish_build_failed = _build_publish_build_failed(
        publisher,
        terminal_publish_ledger=terminal_publish_ledger,
    )

    deps = PipelineConsumerDeps(
        forge_config=forge_config,
        is_duplicate_terminal=is_duplicate_terminal,
        dispatch_build=dispatch_build,
        publish_build_failed=publish_build_failed,
        retired_planning_handoffs=retired_planning_handoffs,
        register_ack_handle=register_ack_handle,
        record_build_rejection=record_build_rejection,
        reply_in_thread=reply_in_thread,
    )
    logger.info(
        "build_pipeline_consumer_deps: composed PipelineConsumerDeps "
        "(async_task_starter=%s, ack_bridge=%s, terminal_publish_ledger=%s)",
        "wired" if async_task_starter is not None else "deferred (TASK-FW10-008)",
        "wired" if register_ack_handle is not None else "deferred (TASK-FRR-PEB-002)",
        (
            "wired"
            if terminal_publish_ledger is not None
            else "deferred (TASK-FRR-PEB-005)"
        ),
    )
    return deps

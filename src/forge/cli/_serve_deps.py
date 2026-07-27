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

import logging
import sqlite3
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from forge.adapters.nats.pipeline_consumer import PipelineConsumerDeps
from forge.adapters.nats.pipeline_publisher import PipelinePublisher
from forge.cli._serve_deps_forward_context import (
    build_forward_context_builder,
    build_stage_log_reader,
)
from forge.cli._serve_deps_lifecycle import build_publisher_and_emitter
from forge.cli._serve_deps_stage_log import build_stage_log_recorder
from forge.cli._serve_deps_state_channel import build_autobuild_state_initialiser
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

if TYPE_CHECKING:  # pragma: no cover - import-time only
    from nats_core.events import BuildFailedPayload, BuildQueuedPayload

logger = logging.getLogger(__name__)


__all__ = [
    "build_pipeline_consumer_deps",
    "build_serve_resume_launcher",
    "is_terminal_status",
]


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
        # the accepted BuildQueuedPayload (the live approve path passes them;
        # the boot-rearm resume path restores the row from SQLite and has no
        # payload in scope, so they default to None and the launch stays
        # byte-compatible with the pre-fix shape).
        #
        # FEAT-UBS-002 (Option-B, stage 1): ``budget`` rides the same one-hop
        # provenance. The live dispatch path resolves it from ``builds.profile``
        # and passes it; the boot-rearm resume path leaves it None (a resume is
        # not a fresh launch) so the resume bytes stay byte-compatible too.
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
        )

    return launch


def build_serve_resume_launcher(
    sqlite_pool: SqliteLifecyclePersistence,
    forge_config: ForgeConfig,
    *,
    lifecycle_emitter: PipelineLifecycleEmitter,
    async_task_starter: AsyncTaskStarter | None,
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
    """
    stage_log_reader = build_stage_log_reader(sqlite_pool)
    forward_context_builder = build_forward_context_builder(
        stage_log_reader, forge_config
    )
    stage_log_recorder = build_stage_log_recorder(sqlite_pool)
    state_channel = build_autobuild_state_initialiser(sqlite_pool)
    return _build_resume_launcher(
        forward_context_builder,
        stage_log_recorder,
        state_channel,
        lifecycle_emitter,
        async_task_starter,
    )


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
):
    """Return the production ``dispatch_build`` closure.

    The closure persists a ``QUEUED`` ``builds`` row (so downstream
    crash-recovery has a durable record of the dispatch attempt), runs the
    TASK-GATE-D659 pre-dispatch approval gate (:func:`maybe_gate_build`),
    and only launches the autobuild runner on gate approval. On a gate
    terminal (reject / expiry / hard-stop) it acks the JetStream slot and
    never launches; while paused it holds the slot un-acked (the runner is
    launched by the R1 approve callback).
    """
    launch = _build_resume_launcher(
        forward_context_builder,
        stage_log_recorder,
        state_channel,
        lifecycle_emitter,
        async_task_starter,
    )
    clock = gate_clock or _utc_now

    async def dispatch_build(
        payload: "BuildQueuedPayload",
        ack_callback,
        register_observer=None,
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
        """
        # Local import to avoid pinning this module's import surface to
        # nats_core when the deps factory is imported during CLI
        # ``--help`` paths (the dispatch closure is the only place the
        # payload type is exercised).
        from forge.lifecycle.persistence import DuplicateBuildError

        if async_task_starter is None:
            raise RuntimeError(
                "build_pipeline_consumer_deps: dispatch_build was invoked "
                "but no async_task_starter was wired. Production wiring "
                "lives in TASK-FW10-008 (Supervisor + AsyncSubAgentMiddleware); "
                "tests should pass a fake starter via the kwarg."
            )

        try:
            build_id = sqlite_pool.record_pending_build(payload)
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

        # FEAT-UBS-002 (Option-B, stage 1) — resolve the per-build budget entry
        # ONCE now that ``build_id`` is final (freshly recorded or re-derived on
        # the runless re-dispatch arm above), so BOTH launch branches — the
        # legacy no-gate launch and the gate-approved launch — attach the SAME
        # compact budget dict to the launch payload. Attended / NULL profile →
        # None (byte-equivalent launch, ASSUM-010). Resolve is fail-open: the
        # helper reads the just-persisted row; an unresolvable profile yields
        # None rather than blocking dispatch.
        budget_entry = _resolve_launch_budget(sqlite_pool, forge_config, build_id)

        # --- Pre-dispatch approval gate (TASK-GATE-D659, R1) -------------
        from forge.cli import _serve_deps_gating, _serve_gate_activation

        parts = _serve_deps_gating.bound_gate_parts()
        if parts is None or gate_repository is None or gate_state_machine is None:
            # Soft-fail: the approval seam is not wired (a v1.1 gate defect
            # must never brick v1 dispatch — see serve.py _compose). Fall
            # back to legacy no-gate launch so the build still runs.
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
            await launch(
                build_id=build_id,
                feature_id=payload.feature_id,
                correlation_id=payload.correlation_id,
                branch=payload.branch,
                repo=payload.repo,
                budget=budget_entry,
            )
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
            logger.info(
                "dispatch_build: gate approved build_id=%s outcome=%s; "
                "registering observer + launching autobuild",
                build_id,
                outcome.value,
            )
            if register_observer is not None:
                await _safe_register_observer(register_observer, build_id)
            await launch(
                build_id=build_id,
                feature_id=payload.feature_id,
                correlation_id=payload.correlation_id,
                branch=payload.branch,
                repo=payload.repo,
                budget=budget_entry,
            )
        else:
            # Gate terminal (reject / expiry / hard-stop) — the build never
            # started; ack the slot so the next queued build proceeds and
            # never register the bridge observer.
            logger.info(
                "dispatch_build: gate terminal build_id=%s outcome=%s; "
                "acking slot, not launching",
                build_id,
                outcome.value,
            )
            await ack_callback()

    return dispatch_build


async def _safe_register_observer(register_observer, build_id: str) -> None:
    """Invoke the R1 deferred bridge-registration closure, never raising.

    Registration is best-effort (the bridge owns its own observability and
    the legacy ack path still works), so a raising ``register_observer``
    must not abort the launch — mirrors the consumer's pre-relocation
    ``register_ack_handle`` guard.
    """
    try:
        await register_observer()
    except Exception as exc:  # noqa: BLE001 — best-effort registration
        logger.warning(
            "dispatch_build: deferred observer registration raised (%s) for "
            "build_id=%s; continuing with legacy ack_callback fallback",
            exc,
            build_id,
        )


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
    is_duplicate_terminal = _build_is_duplicate_terminal(sqlite_pool)
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
        register_ack_handle=register_ack_handle,
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

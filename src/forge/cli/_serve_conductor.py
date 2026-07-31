"""The conductor's PRODUCTION composition — the factories the router needs.

Conductor revival Stage 2, shakeout items 3 and 4
(``supervisor-revival-design-pass-2026-07-31``).

What Stage 1 left, and why
--------------------------

Stage 1 built every piece and wired none of the last two:

* ``build_conductor_router`` was called with **no** ``supervisor_factory``,
  so it logged loudly and returned ``None`` — inert even with the flag ON.
  That was the honest Stage-1 posture ("the daemon composes no Supervisor
  today; stay inert rather than half-wire one"), and this module is the
  discharge of it.
* ``ConductorDriverDeps`` fell back to *all-None* seams, so the first
  non-terminal turn hit a wait it could not perform and died
  ``WAIT_EXPIRED`` with no receipts at all.

Both are composition problems, and this is the composition root's own
module so ``cli/serve.py`` grows two calls rather than three hundred lines.

The M0 statement, made structural
---------------------------------

Design pass §g: **the fix journey adds zero frontier calls to the routine
path**, because the mode branch runs at step 1a of ``next_turn`` — before
the reasoning-model step. That is a claim about control flow, and a claim
is cheap. Here it is a *guard*: the Mode-A-only seams
(``reasoning_model``, ``specialist_dispatcher``, ``async_task_starter``)
are filled with :class:`_ModeAOnlySeam` stand-ins that RAISE, naming
themselves, if a fix-journey turn ever reaches them. A conductor that
started consulting a reasoning model would fail loudly on its first turn
instead of quietly spending frontier tokens on the routine path.

Flag OFF changes nothing here: nothing in this module is constructed
unless ``conductor.enabled`` is on — ``build_conductor_mode_kwargs``
answers ``{}`` and ``build_conductor_router`` answers ``None`` first.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from forge.pipeline.conductor_driver import ConductorDriverDeps, WaitWindow
from forge.pipeline.stage_taxonomy import StageClass

logger = logging.getLogger(__name__)

__all__ = [
    "build_conductor_driver_deps_factory",
    "build_conductor_supervisor_factory",
    "make_conductor_close_out",
    "make_conductor_failure_pack_writer",
    "make_conductor_receipts_exporter",
    "make_conductor_wait_window_reader",
    "make_merge_ready_checkpoint",
]


#: ``stage_log`` statuses that count as "this stage was approved" for the
#: ordering guard's read side. The fix journey's rows are written by
#: ``_serve_deps_stage_log.build_fix_journey_stage_log_writer``, which maps a
#: successful dispatch to ``PASSED``.
_APPROVED_STATUSES: frozenset[str] = frozenset({"PASSED"})


class _ModeAOnlySeam:
    """A collaborator the fix journey must never reach.

    ``build_supervisor`` requires the full Mode A collaborator set even
    though a Mode C turn consults barely half of it. Filling the unused
    seams with ``None`` would turn a control-flow mistake into an
    ``AttributeError`` three frames away; filling them with a working
    implementation would mean the conductor *could* silently take the Mode
    A path — and for ``reasoning_model`` that would mean a frontier call
    on a path whose whole point is not making one (M0, design pass §g).

    So each unused seam is this: an object that raises with its own name
    and the reason, the moment anything touches it.
    """

    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name = name

    def _refuse(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(
            f"conductor: the {self._name!r} seam was reached on a "
            "conductor-driven build. The fix journey branches at step 1a of "
            "next_turn, before every Mode A collaborator — reaching this seam "
            "means the mode branch did not fire. Refusing rather than running "
            "the Mode A path (for reasoning_model that would also be a "
            "frontier call on the M0-zero path)."
        )

    def __getattr__(self, item: str) -> Any:
        if item.startswith("_"):
            raise AttributeError(item)
        return self._refuse

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._refuse(*args, **kwargs)


class _NoToolsMiddleware:
    """Stand-in for the async-subagent middleware, with an empty tool set.

    ``build_supervisor`` READS ``middleware.tools`` at construction time to
    populate ``Supervisor.tools`` — so this seam cannot be a
    :class:`_ModeAOnlySeam` (that would refuse during composition, before
    any turn has run) and it cannot be ``None`` (the factory would then
    construct the real DeepAgents middleware, which exists to dispatch the
    Mode A autobuild the fix journey never runs).

    An empty tuple is the honest answer: the conductor's reasoning loop is
    the Mode C planner, a stateless pure function that uses no tools.
    """

    __slots__ = ()

    tools: tuple = ()


class _SqliteOrderingStageLogReader:
    """``stage_ordering_guard.StageLogReader`` over the daemon's pool.

    Two questions only: "is this stage approved for this build?" and "what
    features are in the catalogue?". The fix journey has no feature
    catalogue — its subject is a task — so the catalogue answers empty,
    which the guard reads as "``pull-request-review`` is not dispatchable
    on the multi-feature branch". The Mode C chain reaches the merge-ready
    checkpoint through its own planner branch, not that one.
    """

    __slots__ = ("_pool",)

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    def is_approved(
        self,
        build_id: str,
        stage: StageClass,
        feature_id: str | None = None,
    ) -> bool:
        try:
            rows = self._pool.read_stages(build_id)
        except Exception as exc:  # noqa: BLE001 — a read defect is not approval
            logger.error(
                "conductor ordering reader: read_stages raised %s: %s for "
                "build_id=%s — answering NOT approved (never guess approved)",
                type(exc).__name__,
                exc,
                build_id,
            )
            return False
        for row in rows:
            if getattr(row, "stage_label", None) != stage.value:
                continue
            if str(getattr(row, "status", "")) not in _APPROVED_STATUSES:
                continue
            details = getattr(row, "details", None) or {}
            if feature_id is not None and details.get("feature_id") != feature_id:
                continue
            return True
        return False

    def feature_catalogue(self, build_id: str) -> list[str]:
        return []


class _SqliteBuildStateReader:
    """``supervisor.StateMachineReader`` over the daemon's pool."""

    __slots__ = ("_pool",)

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    def get_build_state(self, build_id: str) -> Any:
        row = self._pool.get_build_row(build_id)
        if row is None:
            from forge.lifecycle.state_machine import BuildState

            logger.error(
                "conductor state reader: no builds row for build_id=%s — "
                "answering FAILED so the turn stops rather than dispatching "
                "against a row that is not there",
                build_id,
            )
            return BuildState.FAILED
        return row.status


class _SqliteTurnRecorder:
    """``supervisor.StageLogTurnRecorder`` over the daemon's pool.

    Every conductor turn leaves a durable audit row: the outcome, the
    permitted set, the chosen stage, the planner's rationale. This is the
    fix journey's own history for a human reading back what the machine
    decided — distinct from the dispatch rows the dispatcher writes.
    """

    __slots__ = ("_pool", "_clock")

    def __init__(self, pool: Any, *, clock: Callable[[], datetime] | None = None) -> None:
        self._pool = pool
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def record_turn(
        self,
        *,
        build_id: str,
        outcome: Any,
        permitted_stages: Any,
        chosen_stage: Any,
        chosen_feature_id: str | None,
        rationale: str,
        gate_verdict: str | None,
    ) -> None:
        from forge.lifecycle.persistence import StageLogEntry

        now = self._clock()
        try:
            self._pool.record_stage(
                StageLogEntry(
                    build_id=build_id,
                    stage_label="conductor-turn",
                    target_kind="local_tool",
                    target_identifier=(
                        getattr(chosen_stage, "value", None) or "no-stage"
                    ),
                    status="PASSED",
                    gate_mode=None,
                    coach_score=None,
                    threshold_applied=None,
                    started_at=now,
                    completed_at=now,
                    duration_secs=0.0,
                    details={
                        "outcome": getattr(outcome, "value", str(outcome)),
                        "permitted_stages": sorted(
                            getattr(s, "value", str(s)) for s in permitted_stages
                        ),
                        "chosen_stage": getattr(chosen_stage, "value", None),
                        "chosen_feature_id": chosen_feature_id,
                        "rationale": rationale,
                        "gate_verdict": gate_verdict,
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001 — an audit row never kills a turn
            logger.warning(
                "conductor turn recorder: record_stage raised %s: %s for "
                "build_id=%s — the turn stands, the audit row is missing",
                type(exc).__name__,
                exc,
                build_id,
            )


# ---------------------------------------------------------------------------
# The merge-ready checkpoint's production instance
# ---------------------------------------------------------------------------


def make_merge_ready_checkpoint(
    *,
    pool: Any,
    publish_card: Callable[..., Any] | None,
    gates_green_reader: Callable[..., Any] | None = None,
    has_commits_probe: Callable[[str], Any] | None = None,
    receipts_root: "Path | str | None" = None,
) -> Any:
    """Compose the ONE ``pr_review_gate`` implementation for production.

    ``publish_card`` is
    :func:`forge.cli._serve_gate_activation.make_merge_card_publisher`'s
    closure — the SAME approve-click machinery the routine path delivers
    through, jarvis untouched. ``None`` is the shadow-replay posture
    (design pass Stage 2, delivery deliberately OFF): the checkpoint still
    runs its gates-green precondition and reports honestly.

    ``gates_green_reader`` is left ``None`` unless a caller wires one, and
    that is a REFUSAL, not an omission: with no reader the checkpoint reads
    :attr:`~forge.pipeline.merge_ready_checkpoint.GateStatus.UNKNOWN`,
    which it treats as red. The precondition is "proven green", never "not
    proven red", so an unwired gate set can never publish a card.
    """
    from forge.pipeline.fix_journey_receipts import write_fix_journey_failure_pack
    from forge.pipeline.merge_ready_checkpoint import MergeReadyCheckpointPublisher

    def _branch_reader(build_id: str) -> str | None:
        row = pool.get_build_row(build_id)
        return getattr(row, "branch", None) if row is not None else None

    def _failure_pack_writer(
        *, build_id: str, feature_id: str, reason: str, gates: Any
    ) -> Any:
        row = pool.get_build_row(build_id)
        return write_fix_journey_failure_pack(
            build_id=build_id,
            reason=reason,
            outcome="merge-ready-checkpoint",
            feature_id=feature_id or getattr(row, "feature_id", None),
            correlation_id=getattr(row, "correlation_id", None),
            branch=getattr(row, "branch", None),
            worktree_path=getattr(row, "worktree_path", None),
            receipts_root=receipts_root,
        )

    return MergeReadyCheckpointPublisher(
        publish_card=publish_card,
        gates_green_reader=gates_green_reader,
        has_commits_probe=has_commits_probe,
        branch_reader=_branch_reader,
        failure_pack_writer=_failure_pack_writer,
    )


# ---------------------------------------------------------------------------
# The Supervisor factory (shakeout item 3)
# ---------------------------------------------------------------------------


def build_conductor_supervisor_factory(
    *,
    pool: Any,
    config: Any,
    forward_context_builder: Any,
    worktree_allowlist: Any,
    read_allowlist: "list[Path]",
    subprocess_runner: Any,
    lifecycle_emitter: Any = None,
    publish_approval_request: Any = None,
    publish_card: Callable[..., Any] | None = None,
    gates_green_reader: Callable[..., Any] | None = None,
    stage_log_writer: Any = None,
    coach_score_reader: Callable[[str], float | None] | None = None,
    base_branch: str = "main",
    receipts_root: "Path | str | None" = None,
    failure_pack_source_reader: Callable[[str], str | None] | None = None,
    build_supervisor: Callable[..., Any] | None = None,
    mode_kwargs_builder: Callable[..., dict] | None = None,
    budget_kwargs_builder: Callable[..., dict] | None = None,
) -> Callable[[str], Any]:
    """Return the ``(build_id) -> Supervisor`` factory the router injects.

    This is what ``build_conductor_router`` refused to invent for itself in
    Stage 1. Per build it composes:

    * the **mode collaborators** — ``build_conductor_mode_kwargs``: the
      mode reader, the fix-journey planner, the ``stage_log`` projection,
      the terminal handler + commit probe, and the fix-task context
      builder;
    * the **budget collaborators** — ``build_conductor_budget_kwargs``:
      caps off the build's profile, the wall-clock anchors, and the pause
      that publishes the risk-high escalation (design pass §b.1);
    * the **merge-ready checkpoint** as ``pr_review_gate`` — one
      implementation behind all four ``submit_decision`` call sites, and
      one per build so its one-card latch is scoped to this journey;
    * the **conductor dispatcher adapter** as ``subprocess_dispatcher`` —
      the seam that binds ``task_id`` off the build row and translates the
      supervisor's kwargs into the dispatcher's;
    * refusing stand-ins for the Mode-A-only seams (see
      :class:`_ModeAOnlySeam`).

    The factory builds a FRESH Supervisor per build on purpose: the budget
    caps are per-build (they come off ``builds.profile``) and the merge
    card's latch is per-journey. Sharing one instance across builds would
    share both.
    """
    from forge.cli.serve import (
        build_conductor_budget_kwargs,
        build_conductor_mode_kwargs,
    )
    from forge.cli.serve import build_supervisor as _default_build_supervisor
    from forge.pipeline.constitutional_guard import ConstitutionalGuard
    from forge.pipeline.dispatchers.conductor_subprocess import (
        make_conductor_subprocess_dispatcher,
    )
    from forge.pipeline.per_feature_sequencer import PerFeatureLoopSequencer
    from forge.pipeline.stage_ordering_guard import StageOrderingGuard

    _build = build_supervisor or _default_build_supervisor
    _mode_kwargs = mode_kwargs_builder or build_conductor_mode_kwargs
    _budget_kwargs = budget_kwargs_builder or build_conductor_budget_kwargs

    if stage_log_writer is None:
        from forge.cli._serve_deps_stage_log import (
            build_fix_journey_stage_log_writer,
        )

        stage_log_writer = build_fix_journey_stage_log_writer(pool)

    ordering_reader = _SqliteOrderingStageLogReader(pool)

    def supervisor_factory(build_id: str) -> Any:
        mode_kwargs = _mode_kwargs(
            pool=pool,
            config=config,
            base_branch=base_branch,
            worktree_allowlist=worktree_allowlist,
            forward_context_builder=forward_context_builder,
            failure_pack_source_reader=failure_pack_source_reader,
            receipts_root=receipts_root,
        )
        budget_kwargs = _budget_kwargs(
            pool=pool,
            config=config,
            build_id=build_id,
            publish_approval_request=publish_approval_request,
            lifecycle_emitter=lifecycle_emitter,
            coach_score_reader=coach_score_reader,
        )
        dispatcher = make_conductor_subprocess_dispatcher(
            build_row_reader=pool.get_build_row,
            read_allowlist=read_allowlist,
            worktree_allowlist=worktree_allowlist,
            forward_context_builder=forward_context_builder,
            stage_log_writer=stage_log_writer,
            subprocess_runner=subprocess_runner,
        )
        checkpoint = make_merge_ready_checkpoint(
            pool=pool,
            publish_card=publish_card,
            gates_green_reader=gates_green_reader,
            has_commits_probe=None,
            receipts_root=receipts_root,
        )
        return _build(
            forward_context_builder=forward_context_builder,
            async_task_starter=_ModeAOnlySeam("async_task_starter"),
            stage_log_recorder=_ModeAOnlySeam("stage_log_recorder"),
            state_channel=_ModeAOnlySeam("state_channel"),
            lifecycle_emitter=lifecycle_emitter,
            ordering_guard=StageOrderingGuard(),
            per_feature_sequencer=PerFeatureLoopSequencer(),
            constitutional_guard=ConstitutionalGuard(),
            state_reader=_SqliteBuildStateReader(pool),
            ordering_stage_log_reader=ordering_reader,
            per_feature_stage_log_reader=_ModeAOnlySeam(
                "per_feature_stage_log_reader"
            ),
            async_task_reader=_ModeAOnlySeam("async_task_reader"),
            reasoning_model=_ModeAOnlySeam("reasoning_model"),
            turn_recorder=_SqliteTurnRecorder(pool),
            specialist_dispatcher=_ModeAOnlySeam("specialist_dispatcher"),
            subprocess_dispatcher=dispatcher,
            pr_review_gate=checkpoint,
            async_subagent_middleware=_NoToolsMiddleware(),
            **mode_kwargs,
            **budget_kwargs,
        )

    return supervisor_factory


# ---------------------------------------------------------------------------
# The driver deps factory (shakeout item 4)
# ---------------------------------------------------------------------------


def make_conductor_wait_window_reader(
    *,
    pool: Any,
    config: Any,
    clock: Callable[[], datetime] | None = None,
) -> Callable[[str], WaitWindow]:
    """Build the structured wait's durable-anchor reader.

    **Recomputed from durable rows on every iteration** — the property
    that makes the wait re-entrant across a daemon restart. Nothing is
    counted down in memory.

    The anchors, in the order they decide:

    1. **No row, or a terminal row** → resolved. There is nothing left to
       wait for; the loop re-plans and the turn reports the terminal.
    2. **No ``pending_approval_request_id``** → resolved. The build is not
       parked on a human; whatever it was waiting for has moved.
    3. Otherwise the window runs from the build's ``started_at`` anchor
       for ``approval.default_wait_seconds`` (phase 1), then to
       ``approval.max_wait_seconds`` (phase 2, ``needs_republish`` set so
       the persisted request is re-emitted AFTER the waiter arms). Past
       that the window is expired and the journey stops loudly with a pack.

    Those two window numbers are the approval protocol's own, read from
    config rather than invented here — a fix journey's pause must not
    outlive, or undercut, the pause the rest of the estate honours.
    """
    _clock = clock or (lambda: datetime.now(timezone.utc))

    def read_window(build_id: str) -> WaitWindow:
        from forge.lifecycle.state_machine import TERMINAL_STATES

        row = pool.get_build_row(build_id)
        if row is None:
            return WaitWindow(remaining_seconds=0.0, resolved=True)
        if row.status in TERMINAL_STATES:
            return WaitWindow(remaining_seconds=0.0, resolved=True)
        if not getattr(row, "pending_approval_request_id", None):
            return WaitWindow(remaining_seconds=0.0, resolved=True)

        first = float(config.approval.default_wait_seconds)
        ceiling = float(config.approval.max_wait_seconds)
        anchor = getattr(row, "started_at", None) or getattr(row, "queued_at", None)
        if anchor is None:
            return WaitWindow(remaining_seconds=first, phase=1)
        elapsed = (_clock() - anchor).total_seconds()
        if elapsed < first:
            return WaitWindow(remaining_seconds=first - elapsed, phase=1)
        if elapsed < ceiling:
            return WaitWindow(
                remaining_seconds=ceiling - elapsed, phase=2, needs_republish=True
            )
        return WaitWindow(remaining_seconds=0.0, phase=2)

    return read_window


def make_conductor_receipts_exporter(
    *,
    pool: Any,
    receipts_root: "Path | str | None" = None,
) -> Callable[..., Any]:
    """Build the driver's ``export_stage_receipts`` seam — ONE shape.

    The seam shape had to be settled, not split: the driver calls
    ``(*, build_id, report)`` because that is all a turn loop knows, while
    the real exporter
    (:func:`forge.pipeline.fix_journey_receipts.export_stage_receipts`)
    needs ``(*, build_id, stage, worktree_path)``. This adapter is where
    the two meet, and the driver's shape is the one that stands: the
    worktree is a durable row read (not something a turn loop should
    carry) and the stage comes off the report the driver already has.

    A turn that dispatched nothing exports nothing and returns ``None`` —
    receipts belong to stages, not to planning ticks.
    """
    from forge.pipeline.fix_journey_receipts import export_stage_receipts

    def export(*, build_id: str, report: Any) -> Any:
        stage = getattr(report, "chosen_stage", None)
        stage_name = getattr(stage, "value", None)
        if not stage_name:
            return None
        row = pool.get_build_row(build_id)
        rationale = getattr(report, "rationale", "") or ""
        result = export_stage_receipts(
            build_id=build_id,
            stage=stage_name,
            worktree_path=getattr(row, "worktree_path", None),
            receipts_root=receipts_root,
            extra_files={"turn-rationale.txt": rationale} if rationale else None,
        )
        return getattr(result, "stage_key", None)

    return export


def make_conductor_failure_pack_writer(
    *,
    pool: Any,
    receipts_root: "Path | str | None" = None,
    source_build_id_reader: Callable[[str], str | None] | None = None,
) -> Callable[..., Any]:
    """Build the driver's ``write_failure_pack`` seam.

    "A fix journey that fails leaves its own failure pack" (design pass
    §b.2). Success and failure alike leave receipts; this is the failure
    half, pointed back at the build the journey was trying to repair so a
    diagnoser can read both packs.
    """
    from forge.pipeline.fix_journey_receipts import write_fix_journey_failure_pack

    def write(
        *,
        build_id: str,
        reason: str,
        outcome: str,
        stage_keys: "tuple[str, ...]" = (),
    ) -> Any:
        row = pool.get_build_row(build_id)
        source_build_id = None
        if source_build_id_reader is not None:
            try:
                source_build_id = source_build_id_reader(build_id)
            except Exception as exc:  # noqa: BLE001 — never block a stop
                logger.warning(
                    "conductor failure pack: source_build_id_reader raised "
                    "%s: %s for build_id=%s",
                    type(exc).__name__,
                    exc,
                    build_id,
                )
        return write_fix_journey_failure_pack(
            build_id=build_id,
            reason=reason,
            outcome=outcome,
            feature_id=getattr(row, "feature_id", None),
            correlation_id=getattr(row, "correlation_id", None),
            source_build_id=source_build_id,
            branch=getattr(row, "branch", None),
            worktree_path=getattr(row, "worktree_path", None),
            stage_keys=stage_keys,
            receipts_root=receipts_root,
        )

    return write


def make_conductor_close_out(*, pool: Any) -> Callable[..., Any]:
    """Build the driver's ``close_out`` seam — the journey's last write.

    Records one ``conductor-close-out`` ``stage_log`` row naming how the
    journey ended. Deliberately does NOT transition the build row: the
    terminal transitions on this estate are owned by the lifecycle bridge
    and the gate's own state machine, and a second writer racing them is
    how a healthy build gets a false terminal (the FTR lesson). The
    conductor records; it does not adjudicate.
    """
    from forge.lifecycle.persistence import StageLogEntry

    def close_out(*, build_id: str, report: Any) -> None:
        now = datetime.now(timezone.utc)
        try:
            pool.record_stage(
                StageLogEntry(
                    build_id=build_id,
                    stage_label="conductor-close-out",
                    target_kind="local_tool",
                    target_identifier="conductor",
                    status="PASSED",
                    gate_mode=None,
                    coach_score=None,
                    threshold_applied=None,
                    started_at=now,
                    completed_at=now,
                    duration_secs=0.0,
                    details={
                        "outcome": getattr(
                            getattr(report, "outcome", None), "value", None
                        ),
                        "rationale": getattr(report, "rationale", "") or "",
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001 — close-out is best-effort
            logger.warning(
                "conductor close-out: record_stage raised %s: %s for "
                "build_id=%s — the terminal stands, the row is missing",
                type(exc).__name__,
                exc,
                build_id,
            )

    return close_out


def build_conductor_driver_deps_factory(
    *,
    pool: Any,
    config: Any,
    subscriber_factory: Callable[..., Any] | None = None,
    republish_pending: Callable[[str], Any] | None = None,
    receipts_root: "Path | str | None" = None,
    source_build_id_reader: Callable[[str], str | None] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Callable[[str, Any], ConductorDriverDeps]:
    """Return the ``(build_id, supervisor) -> ConductorDriverDeps`` factory.

    Fills every seam Stage 1 left ``None``:

    * ``wait_window_reader`` — :func:`make_conductor_wait_window_reader`.
    * ``subscribe_resume`` — the approval/resume signal, composed over the
      injected ``subscriber_factory`` (``(expected_approver, armed) ->
      subscriber`` with ``wait_for_response(request_id, timeout_seconds)``
      — the shape the live-proven spec-writer driver and the gate rearm
      both use). It sets ``armed`` as its FIRST action, which is what
      makes arm-before-post real rather than aspirational. ``None`` leaves
      the seam unwired, and the driver then stops loudly rather than
      spin-polling — the honest degrade, never a busy wait.
    * ``escalation_resolved`` — whether a budget escalation has cleared,
      read off the build row for the report's rationale.
    * ``export_stage_receipts`` / ``write_failure_pack`` / ``close_out`` —
      the receipts fold, both directions (design pass §b.2).

    The bus seam is the ONLY one that touches NATS, and it arrives
    injected, so every test here runs network-free.
    """
    read_window = make_conductor_wait_window_reader(
        pool=pool, config=config, clock=clock
    )
    export = make_conductor_receipts_exporter(pool=pool, receipts_root=receipts_root)
    write_pack = make_conductor_failure_pack_writer(
        pool=pool,
        receipts_root=receipts_root,
        source_build_id_reader=source_build_id_reader,
    )
    close_out = make_conductor_close_out(pool=pool)
    expected_approver = getattr(config.approval, "expected_approver", None)

    async def subscribe_resume(
        build_id: str,
        *,
        armed: asyncio.Event,
        timeout_seconds: int,
    ) -> Any:
        if subscriber_factory is None:  # pragma: no cover - guarded by caller
            armed.set()
            return None
        row = pool.get_build_row(build_id)
        request_id = getattr(row, "pending_approval_request_id", None)
        if not request_id:
            # Nothing to wait on. Arm so the driver's arm-timeout does not
            # fire, then answer immediately; the window reader will report
            # the wait resolved on the next iteration.
            armed.set()
            return None
        subscriber = subscriber_factory(expected_approver, armed)
        return await subscriber.wait_for_response(
            request_id, timeout_seconds=timeout_seconds
        )

    def escalation_resolved(build_id: str) -> bool:
        row = pool.get_build_row(build_id)
        if row is None:
            return False
        return not getattr(row, "pending_approval_request_id", None)

    def deps_factory(build_id: str, supervisor: Any) -> ConductorDriverDeps:
        return ConductorDriverDeps(
            supervisor=supervisor,
            wait_window_reader=read_window,
            subscribe_resume=(
                subscribe_resume if subscriber_factory is not None else None
            ),
            republish_pending=republish_pending,
            escalation_resolved=escalation_resolved,
            export_stage_receipts=export,
            write_failure_pack=write_pack,
            close_out=close_out,
        )

    return deps_factory

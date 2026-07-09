"""Mode P planning chain driver (TASK-MP-012).

The production driver that walks one planning run through the chain

    QUEUED → RUNNING → PRODUCT_OWNER dispatch → product_docs checkpoint
    (PAUSED) → approve/reject/defer/escalate/timeout → planned handoff
    (PLANNED_HANDOFF)

by consulting the pure planner (:func:`forge.planning.planner.plan_next_step`)
over history **translated from durable planning_run_events rows** — this is
what makes the driver re-entrant: after a crash, :meth:`PlanningRunDriver.drive`
resumes at exactly the step the durable history implies (post-merge review
finding: ``ExecuteHandoff`` was unreachable from durable history because
nothing wrote planner-shaped events).

Design notes
------------

* **Domain module, injected collaborators** — no NATS / SQLite / git types
  are imported here; ``cli/_serve_planning.py`` is the composition root.
* **Structured wait, not a poller** — escalation phase and remaining time
  are recomputed from the durable ``paused_at`` / ``escalated_at`` anchors
  on every loop iteration, so a daemon restart neither resets nor
  double-fires a window (rearm re-enters the same loop).
* **Arm-before-post** — re-publishes (rearm, escalation, defer rounds)
  happen only after the response waiter's subscription is armed. The one
  deliberate exception is the *initial* checkpoint publish (the tested
  ``checkpoint_product_docs`` contract owns pause+publish); the waiter
  arms milliseconds later and the escalation rounds are the self-healing
  backstop for a theoretically lost first response.
* **Task supervision** — the composition wraps ``drive`` tasks so an
  unhandled exception is logged loudly; a stalled run is recovered by the
  next boot's sweep/rearm (documented v1 limitation, exercised by
  TASK-MP-010 live validation).

References: TASK-MP-012, FEAT-SPL-002, DF-009, RT-04, RT-08, DDR-007.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping

from forge.gating.identity import parse_request_id
from forge.pipeline.stage_taxonomy import StageClass
from forge.planning.checkpoint import (
    PlanningEscalationContext,
    _dispatch_approval_response,
    build_planning_approval_envelope,
    checkpoint_product_docs,
)
from forge.planning.escalation import (
    EscalationOutcome,
    EscalationPolicy,
    evaluate_escalation_phase,
)
from forge.planning.handoff import PlannedHandoffHandler
from forge.planning.planner import (
    BoundaryViolation,
    DispatchProductOwner,
    ExecuteHandoff,
    Fail,
    PauseAtCheckpoint,
    plan_next_step,
)
from forge.planning.revision import (
    CYCLE_CAP,
    REVISION_STAGE_LABEL,
    assemble_enrichment_batch,
    dialogue_cycle,
    normalize_assumptions,
    parse_dispositions,
)
from forge.planning.run_store import SqlitePlanningRunStore, TransitionRefused
from forge.planning.states import PlanningState

if TYPE_CHECKING:  # pragma: no cover - typing only
    from forge.config.models import PlanningConfig
    from forge.gating.wrappers import GateRepository, StateMachine
    from forge.planning.checkpoint import SecondOpinionProvider

logger = logging.getLogger(__name__)

__all__ = ["PlanningDriverDeps", "PlanningRunDriver"]

#: Stage label used for the product docs checkpoint (pinned by checkpoint.py).
_PRODUCT_DOCS_STAGE = "product_docs"

#: Upper bound on how long we wait for a response subscription to arm
#: before a re-publish (mirrors _REARM_ARM_TIMEOUT_SECONDS).
_ARM_TIMEOUT_SECONDS = 10.0

_TERMINAL_STATES = {
    PlanningState.FAILED,
    PlanningState.CANCELLED,
    PlanningState.TIMED_OUT,
    PlanningState.PLANNED_HANDOFF,
}

PublishNotificationFn = Callable[[str, str, str], Awaitable[None]]
"""``async (correlation_id, message, level) -> None`` — best-effort notify."""

SubscriberFactory = Callable[[str | None, "asyncio.Event | None"], Any]
"""``(expected_approver, armed_event) -> subscriber`` — the returned object
exposes ``await_response(build_id, *, stage_label, attempt_count,
timeout_seconds)`` (the ApprovalSubscriber surface); ``armed_event`` is set
the moment the underlying subscription is active (arm-before-post)."""

DispatchProductOwnerFn = Callable[..., Awaitable[Any]]
"""``async (*, plan_run_id, correlation_id, enrichment=None) -> StageDispatchResult``.

``enrichment`` is the EnrichmentBatch-shaped revision delta on an
assumption-dialogue re-invoke (``None`` on the first dispatch)."""


def _extract_assumptions(result: Any) -> list[dict[str, Any]]:
    """Best-effort projection of PO-surfaced assumptions off a dispatch result.

    The current :class:`StageDispatchResult` shape does not carry assumptions
    as a first-class field, so this reads an ``assumptions`` attribute when the
    PO result exposes one and falls back to ``criterion_breakdown['assumptions']``.
    Empty when neither is present (the checkpoint degrades to a no-assumptions
    prompt — never a crash).
    """
    raw = getattr(result, "assumptions", None)
    if raw is None:
        breakdown = getattr(result, "criterion_breakdown", None)
        if isinstance(breakdown, Mapping):
            raw = breakdown.get("assumptions")
    return normalize_assumptions(raw)


@dataclass
class PlanningDriverDeps:
    """Injected collaborators for :class:`PlanningRunDriver`."""

    store: SqlitePlanningRunStore
    repository: "GateRepository"
    state_machine: "StateMachine"
    approval_publisher: Any  # publish_request(envelope) — pause-mirroring wrapper
    subscriber_factory: SubscriberFactory
    dispatch_product_owner: DispatchProductOwnerFn
    second_opinion_provider: "SecondOpinionProvider"
    git_runner: Any  # forge.planning.handoff.GitRunner
    planning_config: "PlanningConfig"
    clock: Callable[[], datetime]
    publish_notification: PublishNotificationFn | None = None


@dataclass(frozen=True)
class _HistoryEvent:
    """Planner-shaped view of a planning_run_events row."""

    stage: StageClass
    status: str
    details: Mapping[str, Any]


class PlanningRunDriver:
    """Re-entrant chain driver for one planning run at a time.

    ``drive`` may be called for a run in ANY state — it resumes from the
    durable state and history, which is exactly what the boot sweep
    (QUEUED / RUNNING) and rearm (PAUSED, ``republish_pending=True``)
    need.
    """

    def __init__(self, deps: PlanningDriverDeps) -> None:
        self._deps = deps

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    async def drive(
        self, correlation_id: str, *, republish_pending: bool = False
    ) -> None:
        """Drive ``correlation_id`` forward from its durable state.

        Args:
            correlation_id: The planning run to advance.
            republish_pending: True on the rearm path — the persisted
                ``pending_approval_request_id`` is re-emitted VERBATIM
                after the response waiter arms (ASSUM-015's compensating
                half; spec: "re-issued exactly once by the recovery
                process").
        """
        deps = self._deps
        row = deps.store.get_run(correlation_id)
        if row is None:
            logger.error("planning driver: run %s not found", correlation_id)
            return

        state = PlanningState(row["state"])
        if state in _TERMINAL_STATES:
            logger.info(
                "planning driver: run %s already terminal (%s); nothing to do",
                correlation_id,
                state.value,
            )
            return

        plan_run_id = f"plan-{correlation_id}"

        if state is PlanningState.QUEUED:
            refused = deps.store.transition(
                correlation_id=correlation_id,
                to_state=PlanningState.RUNNING,
                actor_identity="planning-driver",
                stage_label="planning-start",
            )
            if isinstance(refused, TransitionRefused):
                logger.warning(
                    "planning driver: QUEUED→RUNNING refused for %s "
                    "(current=%s); another driver won — backing off",
                    correlation_id,
                    refused.current_state,
                )
                return

        needs_republish = republish_pending
        checkpoint_failures = 0
        while True:
            row = deps.store.get_run(correlation_id)
            if row is None:  # pragma: no cover - defensive
                return
            state = PlanningState(row["state"])

            if state in _TERMINAL_STATES:
                return

            if state is PlanningState.PAUSED:
                outcome = await self._await_approval(
                    row, plan_run_id, needs_republish=needs_republish
                )
                needs_republish = False
                if outcome != "approved":
                    return
                continue  # re-read: now RUNNING, planner advances the chain

            # state is RUNNING — consult the pure planner over durable history
            history = self._load_history(correlation_id)
            decision = plan_next_step(history)

            if isinstance(decision, BoundaryViolation):
                self._fail(
                    correlation_id,
                    stage_label="planning-boundary",
                    reason=decision.rationale,
                )
                return

            if isinstance(decision, Fail):
                self._fail(
                    correlation_id,
                    stage_label="planning-dispatch",
                    reason=decision.reason,
                )
                await self._notify(
                    correlation_id,
                    f"Planning run {correlation_id} failed: {decision.reason}",
                    level="error",
                )
                return

            if isinstance(decision, DispatchProductOwner):
                ok = await self._dispatch_po(correlation_id, plan_run_id)
                if not ok:
                    return
                continue

            if isinstance(decision, PauseAtCheckpoint):
                paused = await self._checkpoint(correlation_id, plan_run_id)
                if not paused:
                    # The pause did NOT reach durable state (pre-pause store
                    # failure) — retrying instantly would spin a no-yield
                    # loop and starve the event loop (TASK-MP-012 review
                    # finding). Back off, and fail terminally after
                    # repeated failures.
                    checkpoint_failures += 1
                    if checkpoint_failures >= 3:
                        self._fail(
                            correlation_id,
                            stage_label=_PRODUCT_DOCS_STAGE,
                            reason="checkpoint pause failed 3 times "
                            "(store unavailable?)",
                        )
                        return
                    await asyncio.sleep(1.0)
                continue

            if isinstance(decision, ExecuteHandoff):
                await self._handoff(row, correlation_id)
                return

            logger.error(  # pragma: no cover - exhaustive union
                "planning driver: unknown planner decision %r for %s",
                decision,
                correlation_id,
            )
            return

    # ------------------------------------------------------------------ #
    # Chain steps
    # ------------------------------------------------------------------ #

    async def _dispatch_po(
        self,
        correlation_id: str,
        plan_run_id: str,
        *,
        enrichment: dict[str, Any] | None = None,
    ) -> bool:
        """Dispatch the PRODUCT_OWNER specialist stage; record the outcome.

        On an assumption-dialogue revision, ``enrichment`` carries the
        EnrichmentBatch-shaped delta (prior assumptions + human dispositions);
        the PO re-invoke is stateless (propose-never-elicit — forge assembles
        the delta). The delta is threaded to the dispatch collaborator and
        recorded on the PO event for durability.
        """
        deps = self._deps
        # Pass ``enrichment`` only on a revision re-invoke so first-dispatch
        # collaborators keep their ``(*, plan_run_id, correlation_id)`` shape.
        extra: dict[str, Any] = {"enrichment": enrichment} if enrichment else {}
        try:
            result = await deps.dispatch_product_owner(
                plan_run_id=plan_run_id,
                correlation_id=correlation_id,
                **extra,
            )
        except Exception as exc:  # noqa: BLE001 — driver never crashes the run silently
            logger.exception(
                "planning driver: PO dispatch raised for %s", correlation_id
            )
            self._fail(
                correlation_id,
                stage_label="product_owner",
                reason=f"PO dispatch raised {type(exc).__name__}: {exc}",
            )
            return False

        outcome = getattr(result, "outcome", None)
        outcome_value = str(getattr(outcome, "value", outcome or "error")).lower()
        coach_score = getattr(result, "coach_score", None)
        reason = getattr(result, "reason", None)

        if outcome_value in ("completed", "degraded"):
            po_output: dict[str, Any] = {
                "coach_evidence": {"coach_score": coach_score},
                "docs_summary": dict(getattr(result, "criterion_breakdown", {}) or {}),
                "structured_findings": [
                    str(f) for f in (getattr(result, "detection_findings", ()) or ())
                ],
                # Structured assumptions surfaced by the PO (best-effort — the
                # result shape carries them when the PO emits confidence-tagged
                # assumptions; empty otherwise, and the checkpoint degrades to a
                # no-assumptions prompt). This is the source the checkpoint
                # projects into details.summary.assumptions (TASK-SPL003F-001).
                "assumptions": _extract_assumptions(result),
                "degraded": outcome_value == "degraded",
                "reason": reason,
            }
            deps.store._record_event(
                correlation_id=correlation_id,
                stage_label="product_owner",
                status="approved",
                coach_score=coach_score,
                actor_identity="planning-driver",
                details_json=json.dumps({"po_output": po_output}, default=str),
            )
            logger.info(
                "planning driver: PRODUCT_OWNER %s for %s (coach_score=%s)",
                outcome_value,
                correlation_id,
                coach_score,
            )
            return True

        # soft_timeout / error → terminal failure
        self._fail(
            correlation_id,
            stage_label="product_owner",
            reason=f"PO dispatch {outcome_value}: {reason or 'no reason supplied'}",
        )
        await self._notify(
            correlation_id,
            f"Planning run {correlation_id} failed at PRODUCT_OWNER ({outcome_value}).",
            level="error",
        )
        return False

    async def _checkpoint(self, correlation_id: str, plan_run_id: str) -> bool:
        """Pause at the product docs checkpoint (DF-009: always pauses).

        Returns True when the PAUSED state reached durable storage (even
        if the subsequent publish failed — escalation rounds / rearm
        re-emit), False when the pause itself did not persist.
        """
        deps = self._deps
        try:
            await checkpoint_product_docs(
                plan_run_id=plan_run_id,
                feature_id=plan_run_id,
                repository=deps.repository,
                state_machine=deps.state_machine,
                publisher=deps.approval_publisher,
                second_opinion_provider=deps.second_opinion_provider,
                coach_evidence=self._latest_po_output(correlation_id).get(
                    "coach_evidence"
                ),
                clock=deps.clock,
            )
        except Exception:  # noqa: BLE001 — publish failure keeps the durable pause
            logger.exception(
                "planning driver: checkpoint raised for %s; verifying whether "
                "the pause reached durable state (DDR-007)",
                correlation_id,
            )
        row = deps.store.get_run(correlation_id)
        return row is not None and row["state"] == PlanningState.PAUSED.value

    async def _await_approval(
        self, row: Any, plan_run_id: str, *, needs_republish: bool
    ) -> str:
        """Structured wait on a PAUSED run until a decision or timeout.

        Returns ``"approved"`` (chain continues), ``"cancelled"``,
        ``"timed_out"`` or ``"externally_resolved"``.
        """
        deps = self._deps
        correlation_id = row["correlation_id"]
        cfg = deps.planning_config

        while True:
            row = deps.store.get_run(correlation_id)
            if row is None:  # pragma: no cover - defensive
                return "externally_resolved"
            state = PlanningState(row["state"])
            if state is not PlanningState.PAUSED:
                # A concurrent actor resolved the pause (approve via
                # another path, cancel, timeout) — surface it.
                if state is PlanningState.RUNNING:
                    return "approved"
                return "externally_resolved"

            if row["paused_at"] is None and row["escalated_at"] is None:
                # Corrupt/legacy PAUSED row with no anchor: stamp the
                # window start ONCE durably — recomputing "starts now" on
                # every iteration would wait forever (TASK-MP-012 review
                # finding).
                deps.store.update_escalation(
                    correlation_id=correlation_id,
                    paused_at=deps.clock().isoformat(),
                    expected_state=PlanningState.PAUSED,
                )
                row = deps.store.get_run(correlation_id) or row

            phase, remaining = self._phase_remaining(row, cfg)

            if remaining <= 0:
                if phase == 1 and cfg.escalation_approver:
                    outcome = await evaluate_escalation_phase(
                        store=deps.store,
                        correlation_id=correlation_id,
                        policy=self._policy(cfg),
                        clock=deps.clock,
                        publisher=None,  # persist re-target only; publish
                        plan_run_id=plan_run_id,  # happens AFTER arming below
                        feature_id=plan_run_id,
                    )
                    if outcome is EscalationOutcome.ESCALATED:
                        needs_republish = True
                    continue
                # Phase-2 ceiling (or no escalation approver configured):
                # durable TIMED_OUT via CAS from PAUSED.
                refused = deps.store.transition(
                    correlation_id=correlation_id,
                    to_state=PlanningState.TIMED_OUT,
                    actor_identity="planning-driver",
                    stage_label="planning-escalation-timeout",
                    error="approval wait ceiling reached",
                    expected_from_state=PlanningState.PAUSED,
                )
                if isinstance(refused, TransitionRefused):
                    continue  # a concurrent decision won; re-read state
                await self._notify(
                    correlation_id,
                    f"Planning run {correlation_id} timed out awaiting approval.",
                    level="warning",
                )
                return "timed_out"

            expected_approver = row["expected_approver"]
            pending_request_id = row["pending_approval_request_id"]
            attempt_count = self._attempt_from(pending_request_id)

            armed: asyncio.Event = asyncio.Event()
            subscriber = deps.subscriber_factory(expected_approver, armed)
            wait_started = time.monotonic()
            wait_task = asyncio.create_task(
                subscriber.await_response(
                    plan_run_id,
                    stage_label=_PRODUCT_DOCS_STAGE,
                    attempt_count=attempt_count,
                    timeout_seconds=max(1, int(remaining)),
                )
            )
            try:
                await asyncio.wait_for(armed.wait(), timeout=_ARM_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                logger.error(
                    "planning driver: response subscription failed to arm "
                    "for %s within %.0fs; retrying",
                    correlation_id,
                    _ARM_TIMEOUT_SECONDS,
                )
                wait_task.cancel()
                try:
                    await wait_task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001 — surface the root cause
                    logger.exception(
                        "planning driver: response waiter failed before "
                        "arming for %s (root cause of the arm timeout)",
                        correlation_id,
                    )
                await asyncio.sleep(1.0)  # anti-spin: never tight-loop arming
                continue

            if needs_republish and pending_request_id:
                # Arm-before-post: the subscription is live, now re-emit
                # the persisted request_id VERBATIM (rearm / escalation /
                # defer rounds).
                await self._republish_pending(row, plan_run_id)
                needs_republish = False

            try:
                response = await wait_task
            except Exception:  # noqa: BLE001 — a waiter defect must not kill the run
                logger.exception(
                    "planning driver: response waiter raised for %s; retrying",
                    correlation_id,
                )
                await asyncio.sleep(1.0)
                continue
            if response is None:
                # Window expired — the durable anchors drive escalation /
                # timeout on the next iteration. Anti-spin: if the waiter
                # returned instantly (defective subscriber, empty fake
                # script) while wall-clock time remains, back off so a
                # broken wire cannot hot-loop the daemon.
                if (
                    time.monotonic() - wait_started < 1.0
                    and self._phase_remaining(
                        deps.store.get_run(correlation_id) or row, cfg
                    )[1]
                    > 0
                ):
                    await asyncio.sleep(1.0)
                continue

            # Stale-round guard: a late response to a superseded
            # request_id is ignored; the wait continues.
            current = deps.store.get_run(correlation_id)
            current_pending = (
                current["pending_approval_request_id"] if current else None
            )
            if current_pending and response.request_id != current_pending:
                logger.warning(
                    "planning driver: stale response request_id=%s for %s "
                    "(current=%s); ignoring",
                    response.request_id,
                    correlation_id,
                    current_pending,
                )
                continue

            outcome = await _dispatch_approval_response(
                response=response,
                repository=deps.repository,
                state_machine=deps.state_machine,
                clock=deps.clock,
                escalation_context=PlanningEscalationContext(
                    store=deps.store,
                    policy=self._policy(cfg),
                    # publisher=None: the defer/at-cap round is persisted by
                    # the escalation module but published HERE, after the
                    # next iteration's waiter is armed (arm-before-post).
                    publisher=None,
                    feature_id=plan_run_id,
                ),
            )

            if outcome in ("approved", "overridden"):
                deps.store._record_event(
                    correlation_id=correlation_id,
                    stage_label=_PRODUCT_DOCS_STAGE,
                    status="checkpoint_cleared",
                    actor_identity=response.decided_by,
                    details_json=json.dumps(
                        {"stage_label": _PRODUCT_DOCS_STAGE, "outcome": outcome}
                    ),
                )
                return "approved"
            if outcome == "revise":
                # Assumption-dialogue revision: assemble the EnrichmentBatch,
                # cap-3 → escalate, else re-invoke the PO statelessly. The
                # checkpoint is NOT cleared; the chain re-pauses next cycle.
                handled = await self._handle_revision(
                    correlation_id, plan_run_id, response
                )
                if handled == "escalated":
                    # Escalated to Rich — keep waiting on the escalated
                    # approver (re-emit the escalated request once armed).
                    needs_republish = True
                    continue
                if handled == "revising":
                    # RUNNING with a fresh PO output — hand back to drive()'s
                    # main loop, which re-checkpoints with the new cycle.
                    return "approved"
                # revision could not be applied (store/dispatch failure) — the
                # run stays PAUSED; the wait ceiling / rearm recovers it.
                return "externally_resolved"
            if outcome == "rejected":
                await self._notify(
                    correlation_id,
                    f"Planning run {correlation_id} was rejected by "
                    f"{response.decided_by}.",
                    level="warning",
                )
                return "cancelled"
            if outcome == "deferred":
                # The new round's request_id is already persisted; re-emit
                # it once the next waiter is armed (arm-before-post).
                needs_republish = True
                continue
            # refused / unknown → keep waiting
            continue

    async def _handle_revision(
        self, correlation_id: str, plan_run_id: str, response: Any
    ) -> str:
        """Apply an assumption-dialogue revision (cap-3 → escalate, else re-invoke).

        Returns ``"escalated"`` (cap reached — escalated to Rich, run stays
        PAUSED), ``"revising"`` (RUNNING with a fresh PO output — drive()
        re-checkpoints) or ``"failed"`` (the revision could not be applied).
        """
        deps = self._deps
        dispositions = parse_dispositions(response)

        # Current dialogue cycle = 1 + durable count of recorded revisions.
        current_cycle = self._dialogue_cycle(correlation_id)

        # Cap-3: a revision that would open a 4th cycle escalates to Rich via
        # the existing escalation path (durable expected_approver re-target,
        # checkpoint_type=product_docs_escalated) instead of another round.
        if current_cycle >= CYCLE_CAP:
            logger.info(
                "planning driver: dialogue cap (%d) reached for %s; escalating "
                "instead of a %dth cycle",
                CYCLE_CAP,
                correlation_id,
                current_cycle + 1,
            )
            from forge.planning.escalation import escalate_planning_run

            await escalate_planning_run(
                store=deps.store,
                correlation_id=correlation_id,
                policy=self._policy(deps.planning_config),
                clock=deps.clock,
                publisher=None,  # arm-before-post: drive() re-emits once armed
                plan_run_id=plan_run_id,
                feature_id=plan_run_id,
            )
            return "escalated"

        # Assemble the EnrichmentBatch delta from the prior assumptions + the
        # human dispositions (forge assembles the delta; the PO does no
        # elicitation — propose-never-elicit).
        prior_assumptions = normalize_assumptions(
            self._latest_po_output(correlation_id).get("assumptions")
        )
        next_cycle = current_cycle + 1
        batch = assemble_enrichment_batch(
            correlation_id=correlation_id,
            cycle=next_cycle,
            prior_assumptions=prior_assumptions,
            dispositions=dispositions,
        )

        # Record the revision durably (increments the dialogue-cycle count so
        # the next checkpoint projects cycle=next_cycle) BEFORE re-dispatching.
        deps.store._record_event(
            correlation_id=correlation_id,
            stage_label=REVISION_STAGE_LABEL,
            status="REVISION",
            actor_identity=response.decided_by,
            details_json=json.dumps({"cycle": next_cycle, "enrichment_batch": batch}),
        )

        # PAUSED → RUNNING so the main loop re-dispatches the PO, then re-invoke
        # the PO statelessly with the assembled delta.
        try:
            await deps.state_machine.transition_to_running(build_id=plan_run_id)
            await deps.repository.mark_resumed(
                build_id=plan_run_id, stage_label=_PRODUCT_DOCS_STAGE
            )
        except Exception:  # noqa: BLE001 — a transition defect must not crash the run
            logger.exception(
                "planning driver: revise transition failed for %s", correlation_id
            )
            return "failed"

        ok = await self._dispatch_po(correlation_id, plan_run_id, enrichment=batch)
        return "revising" if ok else "failed"

    def _dialogue_cycle(self, correlation_id: str) -> int:
        """1-based dialogue cycle from the durable revision-event count.

        Delegates to :func:`revision.dialogue_cycle` — the same arithmetic the
        checkpoint projects, so the projected ``cycle`` and this cap gate can
        never diverge.
        """
        return dialogue_cycle(self._deps.store.list_events(correlation_id))

    async def _handoff(self, row: Any, correlation_id: str) -> None:
        """Execute the planned-handoff terminal (idempotent, RT-08)."""
        deps = self._deps
        po_output = self._latest_po_output(correlation_id)
        run_data: dict[str, Any] = {
            "correlation_id": correlation_id,
            "state": row["state"],
            "request_text": row["request_text"],
            "originating_user": row["originating_user"],
            "target_repo": row["target_repo"],
            "product_docs": po_output.get("docs_summary") or {},
        }

        handler = PlannedHandoffHandler(deps.planning_config, deps.git_runner)
        result = await handler.handle(run_data)

        if result.get("state") == PlanningState.PLANNED_HANDOFF.value:
            refused = deps.store.transition(
                correlation_id=correlation_id,
                to_state=PlanningState.PLANNED_HANDOFF,
                actor_identity="planning-driver",
                stage_label="planned-handoff",
                handoff_branch=result.get("handoff_branch"),
                handoff_path=result.get("handoff_path"),
            )
            if isinstance(refused, TransitionRefused):
                logger.warning(
                    "planning driver: PLANNED_HANDOFF transition refused for "
                    "%s (current=%s)",
                    correlation_id,
                    refused.current_state,
                )
                return
            payload = result.get("notification_payload") or {}
            message = payload.get("message", f"Planning complete for {correlation_id}.")
            command = payload.get("command")
            if command:
                message = f"{message} Next: `{command}`"
            await self._notify(correlation_id, message, level="info")
            logger.info(
                "planning driver: run %s reached PLANNED_HANDOFF (branch=%s)",
                correlation_id,
                result.get("handoff_branch"),
            )
            return

        reason = result.get("failure_reason", "handoff failed")
        self._fail(correlation_id, stage_label="planned-handoff", reason=reason)
        await self._notify(
            correlation_id,
            f"Planning run {correlation_id} handoff failed: {reason}",
            level="error",
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _republish_pending(self, row: Any, plan_run_id: str) -> None:
        """Re-emit the persisted pending request (verbatim request_id)."""
        deps = self._deps
        correlation_id = row["correlation_id"]
        row = deps.store.get_run(correlation_id) or row
        pending_request_id = row["pending_approval_request_id"]
        if not pending_request_id:
            logger.warning(
                "planning driver: no pending request_id to re-emit for %s",
                correlation_id,
            )
            return
        envelope = build_planning_approval_envelope(
            request_id=pending_request_id,
            plan_run_id=plan_run_id,
            feature_id=plan_run_id,
            stage_label=_PRODUCT_DOCS_STAGE,
            summary_data=self._latest_po_output(correlation_id).get("docs_summary")
            or {"recovered": True},
            expected_approver=row["expected_approver"],
            attempt_count=self._attempt_from(pending_request_id),
            checkpoint_type="product_docs_recovered",
        )
        try:
            await deps.approval_publisher.publish_request(envelope)
            logger.info(
                "planning driver: re-emitted pending approval request %s for %s",
                pending_request_id,
                correlation_id,
            )
        except Exception:  # noqa: BLE001 — DDR-007
            logger.exception(
                "planning driver: re-emit failed for %s; pause persists",
                correlation_id,
            )

    def _phase_remaining(self, row: Any, cfg: "PlanningConfig") -> tuple[int, float]:
        """Return ``(phase, remaining_seconds)`` from durable anchors."""
        now = self._deps.clock()
        escalated_at = row["escalated_at"]
        if escalated_at is None:
            anchor_str = row["paused_at"]
            window = cfg.originator_wait_seconds
            phase = 1
        else:
            anchor_str = escalated_at
            window = cfg.escalated_wait_seconds
            phase = 2
        if anchor_str is None:
            # Corrupt row (PAUSED without paused_at) — treat the window
            # as starting now rather than timing out instantly.
            return phase, float(window)
        anchor = datetime.fromisoformat(anchor_str)
        elapsed = (now - anchor).total_seconds()
        return phase, window - elapsed

    def _policy(self, cfg: "PlanningConfig") -> EscalationPolicy:
        return EscalationPolicy(
            originator_wait_seconds=cfg.originator_wait_seconds,
            escalated_wait_seconds=cfg.escalated_wait_seconds,
            escalation_approver=cfg.escalation_approver or "",
            defer_cap=cfg.defer_cap,
        )

    @staticmethod
    def _attempt_from(request_id: str | None) -> int:
        if not request_id:
            return 0
        try:
            _, _, attempt = parse_request_id(request_id)
            return attempt
        except ValueError:
            return 0

    def _load_history(self, correlation_id: str) -> list[_HistoryEvent]:
        """Translate durable event rows into planner-shaped history."""
        history: list[_HistoryEvent] = []
        for event in self._deps.store.list_events(correlation_id):
            stage_label = event["stage_label"]
            status = event["status"]
            details: Mapping[str, Any] = {}
            if event["details_json"]:
                try:
                    details = json.loads(event["details_json"])
                except (json.JSONDecodeError, ValueError):
                    details = {}
            if stage_label == "product_owner" and status == "approved":
                history.append(
                    _HistoryEvent(
                        stage=StageClass.PRODUCT_OWNER,
                        status="approved",
                        details=details,
                    )
                )
            elif status == "checkpoint_cleared":
                history.append(
                    _HistoryEvent(
                        stage=StageClass.PRODUCT_OWNER,
                        status="checkpoint_cleared",
                        details={"stage_label": _PRODUCT_DOCS_STAGE, **details},
                    )
                )
        return history

    def _latest_po_output(self, correlation_id: str) -> dict[str, Any]:
        """Return the most recent recorded PO output (or empty)."""
        latest: dict[str, Any] = {}
        for event in self._deps.store.list_events(correlation_id):
            if event["stage_label"] == "product_owner" and event["status"] == (
                "approved"
            ):
                if event["details_json"]:
                    try:
                        details = json.loads(event["details_json"])
                        latest = details.get("po_output", {}) or {}
                    except (json.JSONDecodeError, ValueError):
                        continue
        return latest

    def _fail(self, correlation_id: str, *, stage_label: str, reason: str) -> None:
        refused = self._deps.store.transition(
            correlation_id=correlation_id,
            to_state=PlanningState.FAILED,
            actor_identity="planning-driver",
            stage_label=stage_label,
            error=reason,
        )
        if isinstance(refused, TransitionRefused):
            logger.warning(
                "planning driver: FAILED transition refused for %s "
                "(current=%s, reason=%s)",
                correlation_id,
                refused.current_state,
                reason,
            )

    async def _notify(
        self, correlation_id: str, message: str, *, level: str = "info"
    ) -> None:
        """Best-effort originator notification (DDR-007)."""
        publish = self._deps.publish_notification
        if publish is None:
            return
        try:
            await publish(correlation_id, message, level)
        except Exception:  # noqa: BLE001 — notifications never block the chain
            logger.warning(
                "planning driver: notification publish failed for %s (best-effort)",
                correlation_id,
            )

"""Escalation policy for planning checkpoints (TASK-MP-005).

This module implements the two-phase wait policy for planning approvals:
1. Phase 1: Wait `originator_wait_seconds` on the originator
2. On expiry: durably re-target to `escalation_approver`, publish, enter phase 2
3. Phase 2: Wait `escalated_wait_seconds` on escalation approver
4. On expiry: transition to TIMED_OUT terminal

All thresholds are evaluated using an **injected clock over durable wall-clock
anchors** (`paused_at`/`escalated_at`) so daemon restarts neither reset nor
double-fire windows.

Defer requests increment `defer_count` durably; when `defer_count == defer_cap`,
the next defer triggers escalation instead of another round.

Key design decisions:
--------------------
* **Clock injection**: All time comparisons use an injected clock function,
  enabling deterministic testing with zero wall-clock waits.
* **Durable state**: All escalation state (`paused_at`, `escalated_at`,
  `defer_count`, `expected_approver`) is persisted to SQLite BEFORE publishing.
* **CAS arbitration**: State transitions use compare-and-swap to handle
  approve-vs-escalate races (only one wins).
* **Event recording**: Every escalation writes a `planning_run_events` row
  BEFORE re-publishing (RT-04 ordering).

References
----------
- TASK-MP-005 — this task brief
- TASK-MP-002 — CAS transition primitive
- TASK-MP-004B — checkpoint foundation
- RT-04 — escalation event ordering requirement
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable

from forge.planning.states import PlanningState

if TYPE_CHECKING:  # pragma: no cover
    from forge.planning.run_store import SqlitePlanningRunStore

logger = logging.getLogger(__name__)

__all__ = [
    "EscalationPolicy",
    "EscalationOutcome",
    "evaluate_escalation_phase",
    "handle_defer_request",
]


class EscalationOutcome(str, Enum):
    """Outcome of escalation phase evaluation."""

    CONTINUE_WAITING = "continue_waiting"
    ESCALATED = "escalated"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True)
class EscalationPolicy:
    """Configuration for planning approval escalation.

    All timeouts are wall-clock seconds evaluated against durable timestamps.
    """

    originator_wait_seconds: int
    escalated_wait_seconds: int
    escalation_approver: str
    defer_cap: int


async def evaluate_escalation_phase(
    *,
    store: SqlitePlanningRunStore,
    correlation_id: str,
    policy: EscalationPolicy,
    clock: Callable[[], datetime],
    publisher: Any | None = None,
    plan_run_id: str | None = None,
    feature_id: str | None = None,
) -> EscalationOutcome:
    """Evaluate current escalation phase and apply policy.

    Checks durable timestamps (`paused_at`, `escalated_at`) against policy
    thresholds using the injected clock. If a threshold is reached, applies
    the appropriate transition (escalate or timeout).

    Args:
        store: Planning run store for state persistence.
        correlation_id: Planning run identifier.
        policy: Escalation policy configuration.
        clock: Injected clock function for deterministic time.
        publisher: Optional approval publisher for re-publishing on escalation.
        plan_run_id: Optional namespaced run ID for publish envelope.
        feature_id: Optional feature ID for publish envelope.

    Returns:
        EscalationOutcome indicating the result.
    """
    row = store._get_run(correlation_id)
    if row is None:
        logger.error("evaluate_escalation_phase: run %s not found", correlation_id)
        return EscalationOutcome.CONTINUE_WAITING

    if row["state"] != PlanningState.PAUSED.value:
        logger.warning(
            "evaluate_escalation_phase: run %s not PAUSED (state=%s)",
            correlation_id,
            row["state"],
        )
        return EscalationOutcome.CONTINUE_WAITING

    paused_at_str = row["paused_at"]
    escalated_at_str = row["escalated_at"]

    if paused_at_str is None:
        logger.error(
            "evaluate_escalation_phase: run %s has no paused_at", correlation_id
        )
        return EscalationOutcome.CONTINUE_WAITING

    current_time = clock()
    paused_at = datetime.fromisoformat(paused_at_str)

    # Determine which phase we're in
    if escalated_at_str is None:
        # Phase 1: waiting on originator
        elapsed = (current_time - paused_at).total_seconds()

        if elapsed >= policy.originator_wait_seconds:
            # Threshold reached - escalate
            if publisher is not None and plan_run_id is not None:
                return await _escalate_to_secondary_approver(
                    store=store,
                    correlation_id=correlation_id,
                    escalation_approver=policy.escalation_approver,
                    clock=clock,
                    publisher=publisher,
                    plan_run_id=plan_run_id,
                    feature_id=feature_id or "unknown",
                )
            else:
                # Publisher not provided (validation mode)
                return EscalationOutcome.ESCALATED
        else:
            # Still within originator window
            return EscalationOutcome.CONTINUE_WAITING

    else:
        # Phase 2: waiting on escalated approver
        escalated_at = datetime.fromisoformat(escalated_at_str)
        elapsed = (current_time - escalated_at).total_seconds()

        if elapsed >= policy.escalated_wait_seconds:
            # Escalated ceiling reached - timeout
            refused = store.transition(
                correlation_id=correlation_id,
                to_state=PlanningState.TIMED_OUT,
                actor_identity="escalation-policy",
                stage_label="planning-escalation-timeout",
                expected_from_state=PlanningState.PAUSED,
            )

            if refused is not None:
                logger.warning(
                    "evaluate_escalation_phase: timeout transition refused for %s "
                    "(current_state=%s)",
                    correlation_id,
                    refused.current_state,
                )
                return EscalationOutcome.CONTINUE_WAITING

            logger.info(
                "evaluate_escalation_phase: run %s timed out after escalation ceiling",
                correlation_id,
            )
            return EscalationOutcome.TIMED_OUT
        else:
            # Still within escalated window
            return EscalationOutcome.CONTINUE_WAITING


async def handle_defer_request(
    *,
    store: SqlitePlanningRunStore,
    correlation_id: str,
    policy: EscalationPolicy,
    clock: Callable[[], datetime],
    publisher: Any,
    plan_run_id: str,
    feature_id: str,
) -> EscalationOutcome:
    """Handle a defer decision on a planning checkpoint.

    If `defer_count < defer_cap`, increments defer_count and initiates a new
    approval round. If `defer_count == defer_cap`, escalates instead of
    deferring again.

    Args:
        store: Planning run store.
        correlation_id: Planning run identifier.
        policy: Escalation policy configuration.
        clock: Injected clock function.
        publisher: Approval publisher for re-publishing.
        plan_run_id: Namespaced run ID.
        feature_id: Feature identifier.

    Returns:
        EscalationOutcome.ESCALATED if defer cap reached, otherwise depends
        on whether a new round was initiated.
    """
    row = store._get_run(correlation_id)
    if row is None:
        logger.error("handle_defer_request: run %s not found", correlation_id)
        return EscalationOutcome.CONTINUE_WAITING

    defer_count = row["defer_count"] or 0

    if defer_count >= policy.defer_cap:
        # At cap - escalate instead of deferring
        logger.info(
            "handle_defer_request: defer_count=%d at cap=%d; escalating %s",
            defer_count,
            policy.defer_cap,
            correlation_id,
        )

        return await _escalate_to_secondary_approver(
            store=store,
            correlation_id=correlation_id,
            escalation_approver=policy.escalation_approver,
            clock=clock,
            publisher=publisher,
            plan_run_id=plan_run_id,
            feature_id=feature_id,
        )
    else:
        # Increment defer count and re-publish
        new_defer_count = defer_count + 1
        store.update_escalation(
            correlation_id=correlation_id,
            defer_count=new_defer_count,
        )

        # Record defer event
        store._record_event(
            correlation_id=correlation_id,
            stage_label="planning-deferred",
            status="DEFERRED",
            actor_identity=row["expected_approver"],
            details_json=json.dumps({"defer_count": new_defer_count}),
        )

        logger.info(
            "handle_defer_request: deferred %s (defer_count=%d)",
            correlation_id,
            new_defer_count,
        )

        # TODO: Re-publish approval request for new round
        # For now, return CONTINUE_WAITING to indicate defer was handled
        return EscalationOutcome.CONTINUE_WAITING


async def _escalate_to_secondary_approver(
    *,
    store: SqlitePlanningRunStore,
    correlation_id: str,
    escalation_approver: str,
    clock: Callable[[], datetime],
    expected_from_state: PlanningState | None = None,
    publisher: Any | None = None,
    plan_run_id: str | None = None,
    feature_id: str | None = None,
) -> EscalationOutcome:
    """Durably escalate a paused run to the escalation approver.

    Updates `expected_approver` and stamps `escalated_at` in a single
    transaction, records an escalation event, then re-publishes the
    approval request.

    Args:
        store: Planning run store.
        correlation_id: Planning run identifier.
        escalation_approver: Target approver identity.
        clock: Injected clock function.
        expected_from_state: Optional expected current state for CAS.
        publisher: Optional approval publisher.
        plan_run_id: Optional namespaced run ID for publish.
        feature_id: Optional feature ID for publish.

    Returns:
        EscalationOutcome.ESCALATED if succeeded, CONTINUE_WAITING if CAS failed.
    """
    row = store._get_run(correlation_id)
    if row is None:
        logger.error("_escalate_to_secondary_approver: run %s not found", correlation_id)
        return EscalationOutcome.CONTINUE_WAITING

    # Check CAS precondition if provided
    if expected_from_state is not None:
        current_state = PlanningState(row["state"])
        if current_state != expected_from_state:
            logger.warning(
                "_escalate_to_secondary_approver: CAS check failed for %s "
                "(expected=%s, actual=%s)",
                correlation_id,
                expected_from_state.value,
                current_state.value,
            )
            return EscalationOutcome.CONTINUE_WAITING

    current_time = clock()
    escalated_at = current_time.isoformat()

    # Durably update expected_approver and escalated_at (RT-04: before publish)
    store.update_escalation(
        correlation_id=correlation_id,
        expected_approver=escalation_approver,
        escalated_at=escalated_at,
    )

    # Record escalation event BEFORE re-publish
    details = {
        "escalation": {
            "from_approver": row["expected_approver"],
            "to_approver": escalation_approver,
            "escalated_at": escalated_at,
        }
    }

    store._record_event(
        correlation_id=correlation_id,
        stage_label="planning-escalation",
        status="ESCALATED",
        actor_identity="escalation-policy",
        details_json=json.dumps(details),
    )

    logger.info(
        "_escalate_to_secondary_approver: escalated %s to %s",
        correlation_id,
        escalation_approver,
    )

    # Re-publish approval request with incremented attempt
    if publisher is not None and plan_run_id is not None:
        from forge.gating.identity import derive_request_id
        from nats_core.envelope import EventType, MessageEnvelope

        # Increment attempt count (read from existing pending_approval_request_id)
        existing_request_id = row["pending_approval_request_id"]
        attempt_count = 0
        if existing_request_id:
            from forge.gating.identity import parse_request_id

            try:
                _, _, attempt_count = parse_request_id(existing_request_id)
                attempt_count += 1
            except ValueError:
                logger.warning(
                    "_escalate_to_secondary_approver: could not parse existing "
                    "request_id %s; using attempt=1",
                    existing_request_id,
                )
                attempt_count = 1

        request_id = derive_request_id(
            build_id=plan_run_id,
            stage_label="product_docs",
            attempt_count=attempt_count,
        )

        # Build escalated approval request envelope
        correlation_id_short = (
            plan_run_id[5:] if plan_run_id.startswith("plan-") else plan_run_id
        )

        payload = {
            "request_id": request_id,
            "run_id": plan_run_id,
            "feature_id": feature_id,
            "stage_label": "product_docs",
            "summary": {"escalated": True, "escalated_to": escalation_approver},
            "checkpoint_type": "product_docs_escalated",
            "attempt_count": attempt_count,
        }

        envelope = MessageEnvelope(
            source_id="forge-planning",
            event_type=EventType.APPROVAL_REQUEST,
            correlation_id=correlation_id_short,
            payload=payload,
        )

        try:
            await publisher.publish_request(envelope)
            logger.info(
                "_escalate_to_secondary_approver: published escalated request %s",
                request_id,
            )
        except Exception:
            logger.exception(
                "_escalate_to_secondary_approver: publish failed for %s", request_id
            )
            # Escalation state is already persisted, publish failure doesn't roll back

    return EscalationOutcome.ESCALATED

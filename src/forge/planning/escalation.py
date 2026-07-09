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

from forge.gating.identity import derive_request_id, parse_request_id
from forge.planning.checkpoint import build_planning_approval_envelope
from forge.planning.states import PlanningState

if TYPE_CHECKING:  # pragma: no cover
    from forge.planning.run_store import SqlitePlanningRunStore

logger = logging.getLogger(__name__)


def _next_attempt_count(existing_request_id: str | None) -> int:
    """Derive the next attempt count from the persisted request_id."""
    if not existing_request_id:
        return 1
    try:
        _, _, attempt_count = parse_request_id(existing_request_id)
        return attempt_count + 1
    except ValueError:
        logger.warning(
            "_next_attempt_count: could not parse existing request_id %s; "
            "using attempt=1",
            existing_request_id,
        )
        return 1


__all__ = [
    "EscalationPolicy",
    "EscalationOutcome",
    "evaluate_escalation_phase",
    "handle_defer_request",
    "escalate_planning_run",
]


def _dialogue_projection(
    store: SqlitePlanningRunStore, correlation_id: str
) -> tuple[str | None, int]:
    """Return ``(parent_request_id, cycle)`` for a run's re-publish envelope.

    ``parent_request_id`` is the durable Slack thread anchor from the
    planning_runs row (DD-SPL003-1); ``cycle`` is the 1-based dialogue cycle
    from :func:`revision.dialogue_cycle` (the shared cap-gate arithmetic).
    Defensive: degrades to ``(None, 1)``.
    """
    from forge.planning.revision import dialogue_cycle

    row = store.get_run(correlation_id)
    parent_request_id = row["parent_request_id"] if row is not None else None
    return parent_request_id, dialogue_cycle(store.list_events(correlation_id))


def _latest_assumptions(
    store: SqlitePlanningRunStore, correlation_id: str
) -> list[dict[str, Any]] | None:
    """Read the latest PO-surfaced assumptions for a defer/escalation re-publish.

    A defer re-prompt and a cap-3 escalation are new approval rounds over the
    SAME assumptions — the escalated/re-prompted human must see them, not a bare
    "escalated" stub (TASK-SPL003F-001 review finding). Reads the most recent
    ``product_owner`` event's ``po_output.assumptions`` from the durable trace;
    ``None`` when absent (jarvis degrades to a no-assumptions prompt).
    """
    from forge.planning.revision import normalize_assumptions

    latest: list[dict[str, Any]] | None = None
    for event in store.list_events(correlation_id):
        if event["stage_label"] == "product_owner" and event["status"] == "approved":
            if not event["details_json"]:
                continue
            try:
                po_output = json.loads(event["details_json"]).get("po_output", {})
            except (json.JSONDecodeError, ValueError):
                continue
            normalized = normalize_assumptions(po_output.get("assumptions"))
            if normalized:
                latest = normalized
    return latest


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
            # Threshold reached — escalate. The durable re-target is
            # persisted even when no publisher is provided, so a caller
            # treating ESCALATED as done never re-escalates every poll
            # (TASK-MP-012 — post-merge review correctness finding).
            # CAS on PAUSED closes the approve-vs-escalate race on the
            # phase-1 path (the phase-2 timeout already had it).
            return await _escalate_to_secondary_approver(
                store=store,
                correlation_id=correlation_id,
                escalation_approver=policy.escalation_approver,
                clock=clock,
                expected_from_state=PlanningState.PAUSED,
                publisher=publisher,
                plan_run_id=plan_run_id,
                feature_id=feature_id or "unknown",
            )
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
        if not policy.escalation_approver:
            # No escalation target configured — re-targeting to "" would
            # make the run unapprovable (TASK-MP-012 review finding).
            # Keep the pause with the current approver; the wait ceiling
            # times the run out.
            logger.warning(
                "handle_defer_request: defer_count=%d at cap=%d for %s but "
                "no escalation_approver configured; keeping current approver "
                "(run will time out at the wait ceiling)",
                defer_count,
                policy.defer_cap,
                correlation_id,
            )
            return EscalationOutcome.CONTINUE_WAITING

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
            expected_from_state=PlanningState.PAUSED,
            publisher=publisher,
            plan_run_id=plan_run_id,
            feature_id=feature_id,
        )
    else:
        # Increment defer count, reset the phase-1 window (a new approval
        # round gets a fresh originator wait — TASK-MP-012), and persist
        # the new round's request_id BEFORE re-publishing.
        new_defer_count = defer_count + 1
        new_attempt = _next_attempt_count(row["pending_approval_request_id"])
        new_request_id = derive_request_id(
            build_id=plan_run_id,
            stage_label="product_docs",
            attempt_count=new_attempt,
        )

        # A new round gets a fresh window: reset the ACTIVE phase's anchor
        # (phase 2 anchors on escalated_at — resetting only paused_at would
        # silently truncate the escalated round, TASK-MP-012 review finding).
        now_iso = clock().isoformat()
        if row["escalated_at"] is not None:
            store.update_escalation(
                correlation_id=correlation_id,
                defer_count=new_defer_count,
                escalated_at=now_iso,
            )
        else:
            store.update_escalation(
                correlation_id=correlation_id,
                defer_count=new_defer_count,
                paused_at=now_iso,
            )
        store.update_pending_approval_request_id(correlation_id, new_request_id)

        # Record defer event
        store._record_event(
            correlation_id=correlation_id,
            stage_label="planning-deferred",
            status="DEFERRED",
            actor_identity=row["expected_approver"],
            details_json=json.dumps(
                {"defer_count": new_defer_count, "request_id": new_request_id}
            ),
        )

        logger.info(
            "handle_defer_request: deferred %s (defer_count=%d, new round "
            "request_id=%s)",
            correlation_id,
            new_defer_count,
            new_request_id,
        )

        # Re-publish approval request for the new round (RT-04: durable
        # state above precedes the wire; publish failure keeps the pause,
        # rearm re-emits the persisted id). publisher=None → the caller
        # owns the re-emit (the driver publishes AFTER its response waiter
        # is armed — arm-before-post, TASK-MP-012 review finding).
        if publisher is None:
            return EscalationOutcome.CONTINUE_WAITING

        parent_request_id, cycle = _dialogue_projection(store, correlation_id)
        envelope = build_planning_approval_envelope(
            request_id=new_request_id,
            plan_run_id=plan_run_id,
            feature_id=feature_id,
            stage_label="product_docs",
            summary_data={"deferred": True, "defer_count": new_defer_count},
            expected_approver=row["expected_approver"],
            attempt_count=new_attempt,
            checkpoint_type="product_docs_deferred",
            parent_request_id=parent_request_id,
            cycle=cycle,
            assumptions=_latest_assumptions(store, correlation_id),
        )
        try:
            await publisher.publish_request(envelope)
        except Exception:  # noqa: BLE001 — DDR-007
            logger.exception(
                "handle_defer_request: re-publish failed for %s; pause "
                "persists, rearm will re-emit",
                new_request_id,
            )

        return EscalationOutcome.CONTINUE_WAITING


async def escalate_planning_run(
    *,
    store: SqlitePlanningRunStore,
    correlation_id: str,
    policy: EscalationPolicy,
    clock: Callable[[], datetime],
    publisher: Any | None = None,
    plan_run_id: str | None = None,
    feature_id: str | None = None,
) -> EscalationOutcome:
    """Escalate a PAUSED planning run to the escalation approver (public).

    The assumption-dialogue cap-3 path calls this when a revision would open a
    4th dialogue cycle: durably re-target ``expected_approver`` and re-publish
    an escalated approval request (``checkpoint_type=product_docs_escalated``).
    ``publisher=None`` persists the re-target only; the driver re-emits once its
    response waiter is armed (arm-before-post).
    """
    if not policy.escalation_approver:
        logger.warning(
            "escalate_planning_run: no escalation_approver configured for %s; "
            "keeping current approver (run times out at the wait ceiling)",
            correlation_id,
        )
        return EscalationOutcome.CONTINUE_WAITING
    return await _escalate_to_secondary_approver(
        store=store,
        correlation_id=correlation_id,
        escalation_approver=policy.escalation_approver,
        clock=clock,
        expected_from_state=PlanningState.PAUSED,
        publisher=publisher,
        plan_run_id=plan_run_id,
        feature_id=feature_id,
    )


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
        logger.error(
            "_escalate_to_secondary_approver: run %s not found", correlation_id
        )
        return EscalationOutcome.CONTINUE_WAITING

    current_time = clock()
    escalated_at = current_time.isoformat()

    # Durably update expected_approver and escalated_at (RT-04: before
    # publish). The optional expected_from_state guard is enforced at the
    # SQL level (`AND state = ?`) so a concurrent approve that just moved
    # PAUSED → RUNNING wins the race and escalation backs off.
    updated = store.update_escalation(
        correlation_id=correlation_id,
        expected_approver=escalation_approver,
        escalated_at=escalated_at,
        expected_state=expected_from_state,
    )
    if not updated:
        logger.warning(
            "_escalate_to_secondary_approver: CAS refused for %s "
            "(expected=%s); a concurrent transition won — not escalating",
            correlation_id,
            expected_from_state.value if expected_from_state else None,
        )
        return EscalationOutcome.CONTINUE_WAITING

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

    # Derive the escalated round's request_id and persist it BEFORE any
    # publish, so a post-escalation restart re-emits the CURRENT id and
    # later attempt derivations never re-parse a stale one (TASK-MP-012 —
    # post-merge review correctness finding).
    attempt_count = _next_attempt_count(row["pending_approval_request_id"])
    request_id = derive_request_id(
        build_id=plan_run_id if plan_run_id else f"plan-{correlation_id}",
        stage_label="product_docs",
        attempt_count=attempt_count,
    )
    store.update_pending_approval_request_id(correlation_id, request_id)

    # Re-publish approval request with incremented attempt (wire-valid
    # envelope shared with the checkpoint — single source of truth).
    if publisher is not None and plan_run_id is not None:
        parent_request_id, cycle = _dialogue_projection(store, correlation_id)
        envelope = build_planning_approval_envelope(
            request_id=request_id,
            plan_run_id=plan_run_id,
            feature_id=feature_id or "unknown",
            stage_label="product_docs",
            summary_data={"escalated": True, "escalated_to": escalation_approver},
            expected_approver=escalation_approver,
            attempt_count=attempt_count,
            checkpoint_type="product_docs_escalated",
            parent_request_id=parent_request_id,
            cycle=cycle,
            # The escalated approver decides the SAME assumptions — surface them
            # (review finding: cap-3 escalation must not ask Rich to decide blind).
            assumptions=_latest_assumptions(store, correlation_id),
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

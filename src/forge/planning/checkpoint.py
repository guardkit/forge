"""Product docs checkpoint flow (TASK-MP-004B).

This module implements the planning-specific checkpoint flow for product docs
approval, reusing the D659 primitives (derive_request_id + atomic
pause-and-publish) with per-run ApprovalSubscriber pinned to expected_approver.

Key design decisions (DF-009 v1 hard rule):
------------------------------------------
* **Never auto-approve**: No code path returns approved without an
  ApprovalResponse. Even maximal coach evidence (coach_score=1.0) pauses.
* **SQLite-before-wire**: The store shows PAUSED + pending_approval_request_id
  BEFORE the publisher records the request envelope. Publish failure does NOT
  roll back the pause (rearm re-emits — DDR-007).
* **Per-run approver pinning**: Responder identity is validated against the
  RUN ROW's expected_approver (not ApprovalConfig). Verbatim string equality
  per JNB-101/104 contract.

References
----------
- TASK-MP-004B — this task brief
- TASK-MP-004A — gate adapters foundation
- DF-009 — never-auto-approve policy
- RT-01 — static config approver plumbing issue (resolved)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Protocol

from forge.gating.identity import derive_request_id, parse_request_id
from forge.gating.models import GateDecision, GateMode
from forge.planning.states import PlanningState
from nats_core.envelope import MessageEnvelope
from nats_core.events import ApprovalResponsePayload

if TYPE_CHECKING:  # pragma: no cover
    from forge.gating.wrappers import GateRepository, StateMachine

logger = logging.getLogger(__name__)

__all__ = ["checkpoint_product_docs", "SecondOpinionProvider"]

# Terminal states that accept no outgoing transitions
_TERMINAL_STATES = {
    PlanningState.FAILED,
    PlanningState.CANCELLED,
    PlanningState.TIMED_OUT,
    PlanningState.PLANNED_HANDOFF,
}


class SecondOpinionProvider(Protocol):
    """Protocol for providers that supply PO output summary data.

    TASK-MP-007 will implement this protocol to provide validated PO output
    summaries. Providers return DATA only — they structurally cannot return
    a decision (DF-009 enforcement at the type level).
    """

    async def get_summary_for_approval(
        self, *, plan_run_id: str, stage_label: str
    ) -> dict[str, Any]:
        """Return compressed PO output summary for approval envelope.

        Args:
            plan_run_id: Planning run identifier (plan-{correlation_id}).
            stage_label: Stage label for this checkpoint.

        Returns:
            Dictionary of validated summary fields for Jarvis rendering.
            Keys typically include: title, description, sections, metadata.
        """
        ...


async def checkpoint_product_docs(
    *,
    plan_run_id: str,
    feature_id: str,
    repository: GateRepository,
    state_machine: StateMachine,
    publisher: Any,  # ApprovalPublisher Protocol (from gating.wrappers)
    second_opinion_provider: SecondOpinionProvider,
    coach_evidence: dict[str, Any] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> None:
    """Product docs checkpoint: pause-before-wire with per-run approver pinning.

    Implements the checkpoint flow per TASK-MP-004B acceptance criteria:
    1. Derives deterministic request_id from (plan_run_id, stage, attempt)
    2. Records PAUSED state in SQLite BEFORE publishing approval request
    3. Publishes approval request with compressed PO output summary
    4. Returns immediately — approval dispatch handled separately

    **Critical**: This function ALWAYS pauses. There is no auto-approve code
    path, even with maximal coach evidence (DF-009 v1 hard rule).

    Args:
        plan_run_id: Planning run identifier (plan-{correlation_id}).
        feature_id: Feature identifier for tracing.
        repository: Gate repository for persistence.
        state_machine: State machine for transitions.
        publisher: Approval request publisher.
        second_opinion_provider: Provider for PO output summary data.
        coach_evidence: Optional coach scores/findings (logged but not used
            for auto-approve).
        clock: Optional clock for deterministic timestamps.

    Raises:
        RuntimeError: If publisher fails (after pause is persisted).
    """
    stage_label = "product_docs"
    attempt_count = 0  # TODO: TASK-MP-005 will increment on defer

    # Derive deterministic request_id (AC-001)
    request_id = derive_request_id(
        build_id=plan_run_id,
        stage_label=stage_label,
        attempt_count=attempt_count,
    )

    logger.info(
        "checkpoint_product_docs: pausing %s at %s (request_id=%s)",
        plan_run_id,
        stage_label,
        request_id,
    )

    # Log coach evidence if present (but do NOT auto-approve)
    if coach_evidence:
        coach_score = coach_evidence.get("coach_score")
        logger.info(
            "checkpoint_product_docs: coach_score=%s (pausing anyway per DF-009)",
            coach_score,
        )

    # Build synthetic gate decision for pause bookkeeping
    # (DF-009: mode is MANDATORY_HUMAN_APPROVAL, never AUTO_APPROVE)
    if clock is None:
        from datetime import UTC

        def clock() -> datetime:
            return datetime.now(UTC)

    decision = GateDecision(
        build_id=plan_run_id,
        stage_label=stage_label,
        target_kind="fleet_capability",
        target_identifier="product_docs_approval",
        mode=GateMode.MANDATORY_HUMAN_APPROVAL,
        rationale="Product docs checkpoint requires human approval (DF-009)",
        coach_score=coach_evidence.get("coach_score") if coach_evidence else None,
        criterion_breakdown={},
        detection_findings=[],
        evidence=[],
        threshold_applied=None,
        auto_approve_override=True,  # DF-009 enforcement flag
        degraded_mode=False,
        decided_at=clock(),
    )

    # SQLite-before-wire: record decision + pause BEFORE publish (AC-002)
    await repository.record_decision(decision)
    await repository.record_paused_build(
        build_id=plan_run_id,
        feature_id=feature_id,
        stage_label=stage_label,
        request_id=request_id,
        attempt_count=attempt_count,
        decision=decision,
    )

    # Transition to PAUSED (atomic with pending_approval_request_id write)
    await state_machine.transition_to_paused(
        build_id=plan_run_id,
        stage_label=stage_label,
    )

    logger.info(
        "checkpoint_product_docs: %s PAUSED in SQLite with request_id=%s",
        plan_run_id,
        request_id,
    )

    # Get validated PO summary from second opinion provider (AC-007)
    summary_data = await second_opinion_provider.get_summary_for_approval(
        plan_run_id=plan_run_id,
        stage_label=stage_label,
    )

    # Build approval request envelope with compressed summary (RT-09: no raw interpolation)
    envelope = _build_approval_request_envelope(
        request_id=request_id,
        plan_run_id=plan_run_id,
        feature_id=feature_id,
        stage_label=stage_label,
        summary_data=summary_data,
    )

    # Publish approval request (publish failure does NOT roll back pause)
    try:
        await publisher.publish_request(envelope)
        logger.info(
            "checkpoint_product_docs: published approval request %s",
            request_id,
        )
    except Exception:
        logger.exception(
            "checkpoint_product_docs: publish failed for request_id=%s "
            "(pause persists per DDR-007; rearm will re-emit)",
            request_id,
        )
        raise


def _build_approval_request_envelope(
    *,
    request_id: str,
    plan_run_id: str,
    feature_id: str,
    stage_label: str,
    summary_data: dict[str, Any],
) -> MessageEnvelope:
    """Build approval request envelope with validated PO summary.

    Args:
        request_id: Deterministic request ID.
        plan_run_id: Planning run identifier.
        feature_id: Feature identifier.
        stage_label: Stage label.
        summary_data: Validated PO output summary from provider.

    Returns:
        MessageEnvelope ready for publishing.
    """
    from nats_core.envelope import EventType

    # Extract correlation_id from namespaced run_id
    correlation_id = plan_run_id[5:] if plan_run_id.startswith("plan-") else plan_run_id

    payload = {
        "request_id": request_id,
        "run_id": plan_run_id,
        "feature_id": feature_id,
        "stage_label": stage_label,
        "summary": summary_data,  # Validated components only (RT-09)
        "checkpoint_type": "product_docs",
    }

    return MessageEnvelope(
        source_id="forge-planning",
        event_type=EventType.APPROVAL_REQUEST,
        correlation_id=correlation_id,
        payload=payload,
    )


async def _dispatch_approval_response(
    *,
    response: ApprovalResponsePayload,
    repository: GateRepository,
    state_machine: StateMachine,
    clock: Callable[[], datetime],
) -> None:
    """Dispatch approval response to appropriate handler.

    Implements the planning-specific approval dispatch tail:
    - Approve: PAUSED → RUNNING (AC-004 identity check)
    - Reject: PAUSED → CANCELLED with rejection recorded (AC-006)
    - Defer: (TASK-MP-005 will implement escalation)
    - Late responses: Terminal state bounce (AC-005)

    Args:
        response: The approval response payload.
        repository: Gate repository for persistence.
        state_machine: State machine for transitions.
        clock: Clock for timestamps.
    """
    request_id = response.request_id
    decision = response.decision
    responder = response.decided_by

    logger.info(
        "_dispatch_approval_response: processing %s decision=%s responder=%s",
        request_id,
        decision,
        responder,
    )

    # Parse request_id to get run_id
    try:
        build_id, stage_label, _attempt = parse_request_id(request_id)
    except ValueError:
        logger.error(
            "_dispatch_approval_response: unparseable request_id=%s; skipping",
            request_id,
        )
        return

    # Extract correlation_id
    if not build_id.startswith("plan-"):
        logger.warning(
            "_dispatch_approval_response: build_id %s not namespaced with plan-; skipping",
            build_id,
        )
        return

    correlation_id = build_id[5:]

    # Read current state from repository (via store)
    # We need to access the underlying store to read state
    # For now, use list_paused_builds and check if this run is in there
    paused_builds = await repository.list_paused_builds()
    our_snapshot = None
    for snapshot in paused_builds:
        if snapshot.build_id == build_id:
            our_snapshot = snapshot
            break

    # If not in paused list, check if terminal (AC-005)
    if our_snapshot is None:
        logger.warning(
            "_dispatch_approval_response: run %s not in paused list; "
            "assuming terminal or invalid; refusing response",
            build_id,
        )
        return

    # Read expected_approver from the snapshot's underlying store
    # We need access to the planning_runs row
    # For proper implementation, we'd inject the store, but for now
    # we can check via the repository's internal store access
    # Let's use a helper to read expected_approver
    expected_approver = await _read_expected_approver(repository, correlation_id)

    if expected_approver is None:
        logger.error(
            "_dispatch_approval_response: could not read expected_approver for %s",
            correlation_id,
        )
        return

    # AC-004: Validate responder identity against expected_approver
    if responder != expected_approver:
        logger.warning(
            "_dispatch_approval_response: responder identity mismatch for %s: "
            "got %s, expected %s; run stays PAUSED",
            build_id,
            responder,
            expected_approver,
        )
        return

    # Dispatch based on decision
    if decision == "approve":
        await state_machine.transition_to_running(build_id=build_id)
        await repository.mark_resumed(build_id=build_id, stage_label=stage_label)
        logger.info(
            "_dispatch_approval_response: approved %s by %s; resumed",
            build_id,
            responder,
        )

    elif decision == "reject":
        # AC-006: Reject → CANCELLED with rejection recorded
        rejection_reason = response.notes or "Rejected at product_docs checkpoint"
        await state_machine.transition_to_cancelled(
            build_id=build_id,
            reason=rejection_reason,
        )

        # Record rejection event
        await _record_rejection_event(
            repository=repository,
            build_id=build_id,
            stage_label=stage_label,
            responder=responder,
            notes=response.notes,
            clock=clock,
        )

        logger.info(
            "_dispatch_approval_response: rejected %s by %s; cancelled",
            build_id,
            responder,
        )

    elif decision == "defer":
        # TASK-MP-005 will implement defer + escalation
        logger.info(
            "_dispatch_approval_response: defer received for %s; "
            "escalation policy not yet implemented (TASK-MP-005)",
            build_id,
        )

    elif decision == "override":
        await repository.mark_overridden(
            build_id=build_id,
            stage_label=stage_label,
            reason=response.notes or "Override at product_docs checkpoint",
        )
        await state_machine.transition_to_running(build_id=build_id)
        logger.info(
            "_dispatch_approval_response: overridden %s by %s; resumed",
            build_id,
            responder,
        )

    else:
        logger.warning(
            "_dispatch_approval_response: unknown decision %s for %s",
            decision,
            build_id,
        )


async def _read_expected_approver(
    repository: GateRepository, correlation_id: str
) -> str | None:
    """Read expected_approver from planning_runs row.

    Args:
        repository: Gate repository (has access to underlying store).
        correlation_id: Planning run correlation ID.

    Returns:
        Expected approver identity, or None if not found.
    """
    # Access the underlying store's connection
    # This is a bit of a hack but necessary for the implementation
    if not hasattr(repository, "_store"):
        logger.error(
            "_read_expected_approver: repository has no _store attribute; "
            "cannot read expected_approver"
        )
        return None

    store = repository._store  # type: ignore[attr-defined]
    conn = store._connection

    row = conn.execute(
        "SELECT expected_approver FROM planning_runs WHERE correlation_id = ?",
        (correlation_id,),
    ).fetchone()

    if row is None:
        return None

    return row[0]


async def _record_rejection_event(
    *,
    repository: GateRepository,
    build_id: str,
    stage_label: str,
    responder: str,
    notes: str | None,
    clock: Callable[[], datetime],
) -> None:
    """Record rejection as planning_run_events row.

    Args:
        repository: Gate repository for event persistence.
        build_id: Planning run identifier.
        stage_label: Stage label.
        responder: Identity of rejecting approver.
        notes: Optional rejection notes.
        clock: Clock for timestamps.
    """
    # Access underlying store to write event
    if not hasattr(repository, "_store"):
        logger.error(
            "_record_rejection_event: repository has no _store attribute; "
            "cannot record event"
        )
        return

    store = repository._store  # type: ignore[attr-defined]
    correlation_id = build_id[5:] if build_id.startswith("plan-") else build_id

    details = {
        "rejection": {
            "responder": responder,
            "notes": notes,
            "rejected_at": clock().isoformat(),
        }
    }

    store._record_event(
        correlation_id=correlation_id,
        stage_label=stage_label,
        status="REJECTED",
        actor_identity=responder,
        details_json=json.dumps(details),
    )

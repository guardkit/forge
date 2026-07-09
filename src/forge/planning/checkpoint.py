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

from dataclasses import dataclass

from forge.gating.identity import derive_request_id, parse_request_id
from forge.gating.models import GateDecision, GateMode
from forge.planning.revision import (
    aggregate_outcome,
    dialogue_cycle,
    dispositions_by_assumption,
    normalize_assumptions,
    parse_dispositions,
)
from forge.planning.states import PlanningState
from nats_core.envelope import EventType, MessageEnvelope
from nats_core.events import ApprovalRequestPayload, ApprovalResponsePayload

if TYPE_CHECKING:  # pragma: no cover
    from forge.planning.escalation import EscalationPolicy
    from forge.planning.run_store import SqlitePlanningRunStore
    from forge.gating.wrappers import GateRepository, StateMachine

logger = logging.getLogger(__name__)

__all__ = [
    "checkpoint_product_docs",
    "build_planning_approval_envelope",
    "PlanningEscalationContext",
    "SecondOpinionProvider",
]

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
    correlation_id = plan_run_id[5:] if plan_run_id.startswith("plan-") else plan_run_id
    # Attempt 0 for the INITIAL checkpoint (no pending id yet → deterministic,
    # idempotent re-drive). A revision re-checkpoint (TASK-SPL003F-001) MUST get
    # a fresh, DISTINCT request_id per dialogue cycle — else the driver's
    # stale-round guard accepts a redelivered prior-cycle response and jarvis's
    # JNB-103 capture treats the new prompt as a duplicate (request_id is the
    # idempotency key). So bump monotonically from the persisted pending id,
    # exactly as defer/escalation already do (mark_resumed is a no-op, so the
    # prior cycle's id survives the PAUSED→RUNNING revise transition).
    attempt_count = _next_checkpoint_attempt(
        _read_pending_request_id(repository, correlation_id)
    )

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

    # Get validated PO summary from second opinion provider (AC-007).
    # The pause above is already durably committed — a provider defect must
    # not strand the run PAUSED with no approval request on the wire, so
    # degrade to a summary-unavailable brief instead of propagating.
    try:
        summary_data = await second_opinion_provider.get_summary_for_approval(
            plan_run_id=plan_run_id,
            stage_label=stage_label,
        )
    except Exception:  # noqa: BLE001 — degrade, never strand the pause
        logger.exception(
            "checkpoint_product_docs: second opinion provider raised for %s; "
            "degrading to summary-unavailable brief",
            plan_run_id,
        )
        summary_data = {"summary_unavailable": True}

    # Per-run approver pinning (RT-04): the envelope names the RUN ROW's
    # expected_approver so jarvis can render who is being asked.
    expected_approver = await _read_expected_approver(repository, correlation_id)

    # Assumption-dialogue projection (TASK-SPL003F-001): read the durable Slack
    # thread anchor from the planning_runs row (DD-SPL003-1 — never re-derived
    # or held in transient state), compute the 1-based dialogue cycle from the
    # durable revision-event count, and surface the PO's structured assumptions.
    parent_request_id = _read_parent_request_id(repository, correlation_id)
    cycle = _dialogue_cycle(repository, correlation_id)
    # ``None`` (not ``[]``) when the checkpoint proposes no assumptions, so the
    # summary rides through unchanged on the no-dialogue path (RT-09 contract).
    assumptions = normalize_assumptions(summary_data.get("assumptions")) or None

    # Build approval request envelope with compressed summary (RT-09: no raw interpolation)
    envelope = build_planning_approval_envelope(
        request_id=request_id,
        plan_run_id=plan_run_id,
        feature_id=feature_id,
        stage_label=stage_label,
        summary_data=summary_data,
        expected_approver=expected_approver,
        attempt_count=attempt_count,
        coach_score=coach_evidence.get("coach_score") if coach_evidence else None,
        parent_request_id=parent_request_id,
        cycle=cycle,
        assumptions=assumptions,
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


def build_planning_approval_envelope(
    *,
    request_id: str,
    plan_run_id: str,
    feature_id: str,
    stage_label: str,
    summary_data: dict[str, Any],
    expected_approver: str | None = None,
    attempt_count: int = 0,
    coach_score: float | None = None,
    rationale: str | None = None,
    checkpoint_type: str = "product_docs",
    parent_request_id: str | None = None,
    cycle: int | None = None,
    originating_channel: str | None = None,
    assumptions: list[dict[str, Any]] | None = None,
) -> MessageEnvelope:
    """Build a WIRE-VALID planning approval request envelope.

    The payload is a frozen nats-core :class:`ApprovalRequestPayload`
    (agent_id / action_description / risk_level / details all present) so
    jarvis's JNB-103 capture validates it instead of WARN-dropping, and
    ``details["build_id"]`` is set so the production
    :class:`~forge.adapters.nats.approval_publisher.ApprovalPublisher`
    can resolve the subject ``agents.approval.forge.{plan_run_id}``
    (TASK-MP-012 — post-merge review wire-topology finding).

    Escalation and defer re-publish import this same builder — it is the
    single source of truth for the planning approval envelope shape.

    Assumption-dialogue projection (TASK-SPL003F-001). When ``assumptions`` is
    provided, the per-assumption list ``[{id, text, confidence, basis}]`` is
    projected under ``details.summary.assumptions`` (alongside a
    ``summary.checkpoint`` label) — the shape jarvis's J02 renders as per-item
    approve/edit/defer blocks. ``parent_request_id`` (the durable Slack thread
    anchor, read from the ``planning_runs`` row) and the dialogue ``cycle``
    number are projected at the top level so jarvis can thread the prompt and
    render the cycle. The consumer keys detection on ``checkpoint_type``
    (ASSUM-002), reads ``summary.assumptions`` and ``parent_request_id``, and
    ignores forge-internal routing keys (``stage_label``/``gate_mode``/… — the
    producer is a superset of jarvis's J04 contract fixture).

    Args:
        request_id: Deterministic request ID (derive_request_id).
        plan_run_id: Namespaced run identifier (``plan-{correlation_id}``).
        feature_id: Feature identifier for tracing.
        stage_label: Stage label for this checkpoint.
        summary_data: Validated PO output summary (RT-09: never raw text).
        expected_approver: The RUN ROW's pinned approver, named in details.
        attempt_count: Defer/escalation round counter.
        coach_score: Optional coach score for jarvis rendering.
        rationale: Optional human-readable pause rationale.
        checkpoint_type: Checkpoint discriminator for jarvis rendering.
        parent_request_id: Durable Slack thread anchor (planning_runs row).
        cycle: 1-based dialogue cycle number for jarvis rendering.
        originating_channel: Originating Slack channel (best-effort context).
        assumptions: Per-assumption list ``[{id, text, confidence, basis}]``.

    Returns:
        MessageEnvelope ready for publishing.
    """
    correlation_id = plan_run_id[5:] if plan_run_id.startswith("plan-") else plan_run_id

    # Assumption-dialogue projection: fold the structured assumptions into the
    # summary jarvis renders. Only when assumptions are supplied — otherwise
    # ``summary`` is the raw PO summary unchanged (existing callers / tests).
    summary_payload: dict[str, Any] = summary_data
    if assumptions is not None:
        summary_payload = {
            **summary_data,
            "checkpoint": stage_label,
            "assumptions": assumptions,
        }

    details: dict[str, Any] = {
        # REQUIRED by ApprovalPublisher.publish_request subject resolution.
        "build_id": plan_run_id,
        "feature_id": feature_id,
        "stage_label": stage_label,
        "gate_mode": GateMode.MANDATORY_HUMAN_APPROVAL.value,
        "coach_score": coach_score,
        "rationale": rationale
        or "Product docs checkpoint requires human approval (DF-009)",
        "summary": summary_payload,  # Validated components only (RT-09)
        "checkpoint_type": checkpoint_type,
        # Spec: "an approval request should be sent naming the originator
        # as the expected approver" (mode-p-planning-chain.feature:66).
        "expected_approver": expected_approver,
        "attempt_count": attempt_count,
        # Assumption-dialogue anchors (TASK-SPL003F-001). Present on every
        # planning envelope; None on the non-dialogue re-publish paths, where
        # jarvis degrades to a top-level channel post (never dropped).
        "parent_request_id": parent_request_id,
        "cycle": cycle,
    }
    if originating_channel is not None:
        details["originating_channel"] = originating_channel

    payload = ApprovalRequestPayload(
        request_id=request_id,
        agent_id="forge",
        action_description=(
            f"Mode P planning checkpoint {stage_label!r} for run "
            f"{plan_run_id!r} awaits approval by "
            f"{expected_approver or 'a human approver'}"
        ),
        risk_level="medium",
        details=details,
    )

    return MessageEnvelope(
        source_id="forge",
        event_type=EventType.APPROVAL_REQUEST,
        correlation_id=correlation_id,
        payload=payload.model_dump(mode="json"),
    )


@dataclass(frozen=True)
class PlanningEscalationContext:
    """Collaborators the defer branch needs to route into the escalation policy.

    Threading this into :func:`_dispatch_approval_response` wires the
    TASK-MP-005 escalation module to the dispatch tail (TASK-MP-012 —
    the two shipped in the same merge but were never connected).
    """

    store: "SqlitePlanningRunStore"
    policy: "EscalationPolicy"
    publisher: Any
    feature_id: str


async def _dispatch_approval_response(
    *,
    response: ApprovalResponsePayload,
    repository: GateRepository,
    state_machine: StateMachine,
    clock: Callable[[], datetime],
    escalation_context: PlanningEscalationContext | None = None,
) -> str:
    """Dispatch approval response to appropriate handler.

    Implements the planning-specific approval dispatch tail:
    - Approve: PAUSED → RUNNING (AC-004 identity check)
    - Reject: PAUSED → CANCELLED with rejection recorded (AC-006)
    - Defer: routed to the escalation policy when ``escalation_context``
      is provided (below-cap: new approval round; at-cap: escalate)
    - Late responses: Terminal state bounce (AC-005)

    Args:
        response: The approval response payload.
        repository: Gate repository for persistence.
        state_machine: State machine for transitions.
        clock: Clock for timestamps.
        escalation_context: Optional collaborators for the defer branch.

    Returns:
        Outcome discriminator: one of ``"approved"``, ``"rejected"``,
        ``"deferred"``, ``"revise"``, ``"overridden"``, ``"refused"``,
        ``"unknown"``. ``"revise"`` signals the driver to assemble an
        EnrichmentBatch and statelessly re-invoke the PRODUCT_OWNER
        (assumption-dialogue revision cycle, keyed on the dispositions).
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
        return "refused"

    # Extract correlation_id
    if not build_id.startswith("plan-"):
        logger.warning(
            "_dispatch_approval_response: build_id %s not namespaced with plan-; skipping",
            build_id,
        )
        return "refused"

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
        return "refused"

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
        return "refused"

    # AC-004: Validate responder identity against expected_approver
    if responder != expected_approver:
        logger.warning(
            "_dispatch_approval_response: responder identity mismatch for %s: "
            "got %s, expected %s; run stays PAUSED",
            build_id,
            responder,
            expected_approver,
        )
        return "refused"

    # Assumption-dialogue disposition handling (TASK-SPL003F-001, ASSUM-006).
    # Parse the per-assumption dispositions (structured 0.7.0 field, or the
    # ASSUM-003 notes-JSON bridge; defensively empty on a malformed payload),
    # record them keyed by assumption id (WS4 curation join), and key the
    # revise-vs-proceed-vs-defer choice on the DISPOSITIONS, never the decision
    # literal (an override rides in as ``decision="approve"`` carrying
    # ``modified`` dispositions).
    dispositions = parse_dispositions(response)
    if dispositions:
        _record_disposition_trace(
            repository=repository,
            build_id=build_id,
            stage_label=stage_label,
            responder=responder,
            decision=decision,
            dispositions=dispositions,
            clock=clock,
        )
        # A whole-run reject or an explicit override still wins outright (both
        # have dedicated decision-literal branches below — reject → CANCELLED,
        # override → mark_overridden audit — that the dialogue mapping must not
        # shadow). The dialogue handshake only rides in as decision="approve"
        # or "defer".
        if decision not in ("reject", "override"):
            aggregate = aggregate_outcome(dispositions)
            if aggregate == "defer":
                return await _route_defer(
                    build_id=build_id,
                    correlation_id=correlation_id,
                    stage_label=stage_label,
                    responder=responder,
                    escalation_context=escalation_context,
                    clock=clock,
                )
            if aggregate == "revise":
                # The driver owns the cap-3 check, EnrichmentBatch assembly and
                # the stateless PO re-invoke — it holds the dispatch collaborator
                # and re-reads the dispositions off the response.
                logger.info(
                    "_dispatch_approval_response: revise requested for %s by %s "
                    "(dispositions carry a modification)",
                    build_id,
                    responder,
                )
                return "revise"
            # aggregate == "proceed": all assumptions accepted → clear the
            # checkpoint regardless of the decision literal.
            await state_machine.transition_to_running(build_id=build_id)
            await repository.mark_resumed(build_id=build_id, stage_label=stage_label)
            logger.info(
                "_dispatch_approval_response: all assumptions accepted for %s "
                "by %s; checkpoint cleared",
                build_id,
                responder,
            )
            return "approved"

    # Dispatch based on decision (no per-assumption dispositions on the wire)
    if decision == "approve":
        await state_machine.transition_to_running(build_id=build_id)
        await repository.mark_resumed(build_id=build_id, stage_label=stage_label)
        logger.info(
            "_dispatch_approval_response: approved %s by %s; resumed",
            build_id,
            responder,
        )
        return "approved"

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
        return "rejected"

    elif decision == "defer":
        return await _route_defer(
            build_id=build_id,
            correlation_id=correlation_id,
            stage_label=stage_label,
            responder=responder,
            escalation_context=escalation_context,
            clock=clock,
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
        return "overridden"

    else:
        logger.warning(
            "_dispatch_approval_response: unknown decision %s for %s",
            decision,
            build_id,
        )
        return "unknown"


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


def _read_parent_request_id(
    repository: GateRepository, correlation_id: str
) -> str | None:
    """Read the durable Slack thread anchor from the ``planning_runs`` row.

    Reuses the tested public ``store.get_run`` accessor (DD-SPL003-1 — the
    anchor is never re-derived or held in transient state). Degrades to
    ``None`` when the repository exposes no store or the run is absent.
    """
    if not hasattr(repository, "_store"):
        return None
    store = repository._store  # type: ignore[attr-defined]
    row = store.get_run(correlation_id)
    return row["parent_request_id"] if row is not None else None


def _read_pending_request_id(
    repository: GateRepository, correlation_id: str
) -> str | None:
    """Read the persisted ``pending_approval_request_id`` for a run (or ``None``)."""
    if not hasattr(repository, "_store"):
        return None
    store = repository._store  # type: ignore[attr-defined]
    row = store.get_run(correlation_id)
    return row["pending_approval_request_id"] if row is not None else None


def _next_checkpoint_attempt(pending_request_id: str | None) -> int:
    """Monotonic attempt counter for a (re-)checkpoint's ``request_id``.

    ``None`` (no prior pause) → attempt 0, the INITIAL checkpoint's
    deterministic, idempotent-re-drive value. Otherwise bump the persisted
    round by one so every new approval round (revise cycle, defer/escalation
    round) derives a DISTINCT ``request_id``. Unparseable → 0 (fail safe to the
    initial value rather than colliding).
    """
    if not pending_request_id:
        return 0
    try:
        _, _, attempt = parse_request_id(pending_request_id)
        return attempt + 1
    except ValueError:
        return 0


def _dialogue_cycle(repository: GateRepository, correlation_id: str) -> int:
    """Compute the 1-based dialogue cycle from the durable revision-event count.

    Delegates the count arithmetic + label to :func:`revision.dialogue_cycle`
    (the single source of truth for the cap-3 gate). Survives restarts (no
    transient counter).
    """
    if not hasattr(repository, "_store"):
        return 1
    store = repository._store  # type: ignore[attr-defined]
    return dialogue_cycle(store.list_events(correlation_id))


async def _route_defer(
    *,
    build_id: str,
    correlation_id: str,
    stage_label: str,
    responder: str,
    escalation_context: PlanningEscalationContext | None,
    clock: Callable[[], datetime],
) -> str:
    """Route a deferred decision to the escalation policy (shared by the
    decision-literal ``defer`` branch and the dispositions ``deferred`` path).
    """
    if escalation_context is None:
        logger.warning(
            "_dispatch_approval_response: defer received for %s but no "
            "escalation context wired; defer is a no-op",
            build_id,
        )
        return "deferred"

    # Late import breaks the checkpoint <-> escalation module cycle
    # (escalation imports build_planning_approval_envelope from here).
    from forge.planning.escalation import handle_defer_request

    await handle_defer_request(
        store=escalation_context.store,
        correlation_id=correlation_id,
        policy=escalation_context.policy,
        clock=clock,
        publisher=escalation_context.publisher,
        plan_run_id=build_id,
        feature_id=escalation_context.feature_id,
    )
    logger.info(
        "_dispatch_approval_response: defer for %s routed to escalation "
        "policy by %s",
        build_id,
        responder,
    )
    return "deferred"


def _record_disposition_trace(
    *,
    repository: GateRepository,
    build_id: str,
    stage_label: str,
    responder: str,
    decision: str,
    dispositions: list[Any],
    clock: Callable[[], datetime],
) -> None:
    """Record a dialogue cycle's dispositions, keyed by assumption id (WS4 join).

    Writes one ``planning_run_events`` row per response carrying per-assumption
    dispositions so the decisions are recoverable distinctly by assumption id
    (FEAT-SPL-005 trace spine / the WS4-S7 curation join). The by-id block is
    the ``planning_outcome`` episode substrate (backward-edge contract §4.1 —
    ``disposition`` + ``edit_delta`` first-class).
    """
    if not hasattr(repository, "_store"):
        logger.error(
            "_record_disposition_trace: repository has no _store attribute; "
            "cannot record dispositions"
        )
        return

    store = repository._store  # type: ignore[attr-defined]
    correlation_id = build_id[5:] if build_id.startswith("plan-") else build_id
    cycle = _dialogue_cycle(repository, correlation_id)

    details = {
        "cycle": cycle,
        "decision": decision,
        "decided_by": responder,
        "recorded_at": clock().isoformat(),
        # keyed by assumption id — recoverable distinctly (WS4 join)
        "dispositions": dispositions_by_assumption(dispositions),
    }

    store._record_event(
        correlation_id=correlation_id,
        stage_label="planning-dialogue",
        status="DISPOSITIONS",
        actor_identity=responder,
        details_json=json.dumps(details),
    )


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

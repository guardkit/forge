"""Assumption-dialogue revision assembler (TASK-SPL003F-001, FEAT-SPL-003 forge half).

This module is the forge counterpart of jarvis's per-assumption decision
dialogue (FEAT-SPL-003 jarvis half, ASSUM-006). It consumes the aggregate
per-assumption dispositions jarvis returns in the approval response and maps
them onto the planning chain's next move:

* **all ``accepted``** → ``proceed`` (the checkpoint clears; the chain advances
  to handoff — the existing approve path)
* **any ``modified``** → ``revise`` (assemble an EnrichmentBatch-shaped delta and
  statelessly re-invoke the PRODUCT_OWNER — *forge assembles the delta; the PO
  does no elicitation*, propose-never-elicit / scope §3.3)
* **any ``deferred``** → ``defer`` (the existing ``handle_defer_request`` round)

The revise-vs-proceed choice is keyed on the **parsed dispositions, NOT the
``decision`` literal** (ASSUM-006 handshake): a modification rides the wire as
``decision="approve"`` carrying ``modified`` dispositions, so a naive read of
the literal would wrongly proceed.

Carrier (ASSUM-003 → superseded by WS1-I). v1 pinned dispositions to
``ApprovalResponsePayload.notes`` as JSON. nats-core 0.7.0 landed the
first-class structured ``ApprovalResponsePayload.dispositions`` field, which
supersedes the notes-JSON bridge. :func:`parse_dispositions` prefers the
structured field and falls back to the notes-JSON shape defensively, so a
malformed/absent payload degrades to an empty list instead of crashing the
chain.

References
----------
- TASK-SPL003F-001 — this task
- jarvis FEAT-SPL-003 ASSUM-003 / ASSUM-006 — the counterpart contract
- nats-core 0.7.0 ``AssumptionDisposition`` — the structured carrier
- fleet-memory backward-edge episode schema contract §4.1/§4.2 — the
  ``planning_outcome`` / ``approval_decision`` producer obligations
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from nats_core.events import ApprovalResponsePayload, AssumptionDisposition

logger = logging.getLogger(__name__)

__all__ = [
    "CYCLE_CAP",
    "REVISION_STAGE_LABEL",
    "RevisionOutcome",
    "parse_dispositions",
    "aggregate_outcome",
    "normalize_assumptions",
    "assemble_enrichment_batch",
    "dispositions_by_assumption",
    "dialogue_cycle",
    "build_planning_outcome_episode",
    "build_approval_decision_episode",
]

#: Dialogue-cycle cap (frozen contract, mirrors jarvis ``_CYCLE_CAP``).
#: Cycles 1..3 render per-assumption prompts; a ``revise`` that would open a
#: 4th cycle escalates to Rich instead.
CYCLE_CAP = 3

#: Durable ``planning_run_events.stage_label`` for a recorded assumption-dialogue
#: revision cycle. **Single source of truth** — the checkpoint's projected
#: ``cycle`` and the driver's cap-3 gate both count events carrying this label,
#: so it must never be spelled as a bare string anywhere (a drift would let a
#: 4th cycle slip past the cap, or escalate prematurely).
REVISION_STAGE_LABEL = "planning-revision"


def dialogue_cycle(events: Any) -> int:
    """Return the 1-based dialogue cycle from a run's durable event rows.

    Cycle 1 is the initial checkpoint (zero recorded revisions); each recorded
    :data:`REVISION_STAGE_LABEL` event opens the next cycle. ``events`` is any
    iterable of dict-like rows exposing ``["stage_label"]`` (a
    ``planning_run_events`` row list). The single arithmetic used by every
    projection + the cap gate.
    """
    return sum(1 for e in events if e["stage_label"] == REVISION_STAGE_LABEL) + 1


#: The chain's next move, keyed on the parsed dispositions.
RevisionOutcome = Literal["proceed", "revise", "defer"]


def parse_dispositions(
    response: ApprovalResponsePayload,
) -> list[AssumptionDisposition]:
    """Extract structured per-assumption dispositions from an approval response.

    Prefers the first-class ``dispositions`` field (nats-core 0.7.0). Falls
    back to the ASSUM-003 notes-JSON bridge
    (``{"cycle": N, "dispositions": [{id, disposition, value, ...}]}``) when
    the structured field is absent. **Never raises** — a malformed or absent
    payload yields an empty list so a defective wire message cannot crash the
    planning chain (AC: "a malformed/absent dispositions payload is handled
    defensively; logged; does not crash the chain").

    Args:
        response: The approval response payload.

    Returns:
        The structured dispositions (empty when none are recoverable).
    """
    # Structured field wins (0.7.0). It is already validated by pydantic.
    if response.dispositions:
        return list(response.dispositions)

    # Bridge fallback: dispositions ride ``notes`` as JSON (ASSUM-003).
    notes = getattr(response, "notes", None)
    if not notes:
        return []
    try:
        data = json.loads(notes)
    except (json.JSONDecodeError, ValueError, TypeError):
        # ``notes`` is human free text, not the bridge JSON — not an error.
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get("dispositions")
    if not isinstance(raw, list):
        return []

    parsed: list[AssumptionDisposition] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        assumption_id = item.get("id") or item.get("assumption_id")
        disposition = item.get("disposition")
        if not assumption_id or not disposition:
            continue
        try:
            parsed.append(
                AssumptionDisposition(
                    assumption_id=str(assumption_id),
                    disposition=disposition,
                    # bridge shape uses ``value`` for the edit; structured uses
                    # ``edit_delta`` — accept either without reshaping.
                    edit_delta=item.get("edit_delta") or item.get("value"),
                    notes=item.get("notes"),
                )
            )
        except Exception:  # noqa: BLE001 — a malformed item never crashes parse
            logger.warning(
                "parse_dispositions: dropping malformed disposition item %r",
                item,
            )
    return parsed


def aggregate_outcome(
    dispositions: list[AssumptionDisposition],
) -> RevisionOutcome:
    """Map per-assumption dispositions onto the chain's next move (ASSUM-006).

    Precedence (matches jarvis ``aggregate_decision``): any ``deferred`` blocks
    the run (a single undecided item cannot proceed *or* revise) → ``defer``;
    else any non-``accepted`` item (``modified`` / ``rejected`` / ``undecided``)
    needs PO rework → ``revise``; else (all ``accepted`` or an empty set) →
    ``proceed``.

    Keyed on the dispositions, **never** the decision literal.

    Args:
        dispositions: Parsed per-assumption dispositions.

    Returns:
        ``"proceed"``, ``"revise"`` or ``"defer"``.
    """
    if not dispositions:
        # No dialogue dispositions (e.g. a plain build gate, or a checkpoint
        # cleared without per-item decisions) → fall through to proceed; the
        # caller keys the plain approve/reject path on the decision literal.
        return "proceed"
    if any(d.disposition == "deferred" for d in dispositions):
        return "defer"
    if any(d.disposition != "accepted" for d in dispositions):
        return "revise"
    return "proceed"


def normalize_assumptions(raw: Any) -> list[dict[str, Any]]:
    """Project raw PO assumptions onto the ``{id, text, confidence, basis}`` shape.

    Defensive: a missing/malformed source yields ``[]`` (a checkpoint that
    proposes no assumptions is valid — the jarvis consumer degrades to a
    no-assumptions prompt). Items without an ``id`` are dropped (jarvis keys
    every rendered block on the id).

    Accepts the canonical shape as-is and maps a couple of common PO key
    variants (``assumption_id``/``statement``/``rationale``) so the projection
    does not silently lose fields when the PO output drifts.

    Args:
        raw: The PO ``assumptions`` value (list of dicts, ideally).

    Returns:
        A list of ``{id, text, confidence, basis}`` dicts.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        assumption_id = item.get("id") or item.get("assumption_id")
        if not assumption_id:
            continue
        out.append(
            {
                "id": str(assumption_id),
                "text": item.get("text") or item.get("statement"),
                "confidence": item.get("confidence"),
                "basis": item.get("basis") or item.get("rationale"),
            }
        )
    return out


def dispositions_by_assumption(
    dispositions: list[AssumptionDisposition],
) -> dict[str, dict[str, Any]]:
    """Project dispositions into a by-assumption-id trace block (WS4 join).

    This is the durable curation signal recorded in
    ``planning_run_events.details_json`` — dispositions must be recoverable
    keyed by assumption id (FEAT-SPL-005 trace spine / WS4-S7 join). The block
    is the ``planning_outcome`` episode's ``assumptions[]`` substrate
    (backward-edge contract §4.1 "first-class by design").

    Args:
        dispositions: Parsed per-assumption dispositions.

    Returns:
        ``{assumption_id: {disposition, edit_delta, notes}}``.
    """
    return {
        d.assumption_id: {
            "disposition": d.disposition,
            "edit_delta": d.edit_delta,
            "notes": d.notes,
        }
        for d in dispositions
    }


def assemble_enrichment_batch(
    *,
    correlation_id: str,
    cycle: int,
    prior_assumptions: list[dict[str, Any]],
    dispositions: list[AssumptionDisposition],
) -> dict[str, Any]:
    """Assemble the EnrichmentBatch-shaped revision input for a PO re-invoke.

    Forge assembles the delta (propose-never-elicit — the PO does no
    elicitation; scope §3.3). Each prior assumption is paired with its human
    disposition; the ``edit_delta`` on a ``modified`` item is the contrastive
    revision signal (proposal-as-generated → proposal-as-accepted). The PO
    role's existing ``EnrichmentBatch`` merge consumes this delta statelessly.

    An assumption with no returned disposition is carried as ``undecided`` so
    the batch is total over ``prior_assumptions`` (the re-invoke sees every
    proposed item).

    Args:
        correlation_id: Raw planning correlation id.
        cycle: The dialogue cycle this revision opens (1-based).
        prior_assumptions: The assumptions surfaced last cycle
            (``{id, text, confidence, basis}``).
        dispositions: The human's per-assumption dispositions.

    Returns:
        A JSON-serialisable EnrichmentBatch-shaped dict.
    """
    by_id = {d.assumption_id: d for d in dispositions}
    revisions: list[dict[str, Any]] = []
    for assumption in prior_assumptions:
        assumption_id = assumption.get("id")
        decision = by_id.get(str(assumption_id)) if assumption_id else None
        revisions.append(
            {
                "assumption_id": assumption_id,
                "prior": {
                    "text": assumption.get("text"),
                    "confidence": assumption.get("confidence"),
                    "basis": assumption.get("basis"),
                },
                "disposition": decision.disposition if decision else "undecided",
                "edit_delta": decision.edit_delta if decision else None,
                "notes": decision.notes if decision else None,
            }
        )
    return {
        "kind": "enrichment_batch",
        "correlation_id": correlation_id,
        "cycle": cycle,
        "revisions": revisions,
    }


# ---------------------------------------------------------------------------
# Backward-edge episode projectors (fleet-memory contract §4.1 / §4.2).
#
# Forge is the SINGLE writer of these episodes (2026-07-08 amendment). The
# live graphiti emission + fleet-memory registry merge are WS4-S7 (gated on
# this build); these pure projectors are the ready producer and the durable
# ``planning_run_events`` disposition block above is their substrate. See the
# task STATUS deviation note.
# ---------------------------------------------------------------------------


def build_planning_outcome_episode(
    *,
    correlation_id: str,
    originator: str,
    terminal_state: str,
    assumptions_trace: dict[str, dict[str, Any]],
    surfaced_assumptions: list[dict[str, Any]],
    started_at: str,
    duration_seconds: int,
    feat_id: str | None = None,
    approval_cycles_used: int | None = None,
    trace_ref: str | None = None,
    spec_ref: str | None = None,
) -> dict[str, Any]:
    """Project a terminal planning run onto the ``planning_outcome`` shape (§4.1).

    ``originator`` is the **observed** originating member id (never a config
    echo). ``assumptions[]`` carries one entry per surfaced assumption with its
    first-class disposition + ``edit_delta`` (the WS4 preference-pair signal).

    Pure — no I/O. WS4-S7 wires the fleet-memory write.
    """
    assumptions: list[dict[str, Any]] = []
    for assumption in surfaced_assumptions:
        assumption_id = str(assumption.get("id"))
        trace = assumptions_trace.get(assumption_id, {})
        assumptions.append(
            {
                "assumption_id": assumption_id,
                "text": assumption.get("text"),
                "confidence": assumption.get("confidence"),
                "disposition": trace.get("disposition", "undecided"),
                "edit_delta": trace.get("edit_delta"),
                "notes": trace.get("notes"),
            }
        )
    episode: dict[str, Any] = {
        "correlation_id": correlation_id,
        "originator": originator,
        "mode": "mode_p",
        "terminal_state": terminal_state,
        "assumption_count": len(assumptions),
        "assumptions": assumptions,
        "started_at": started_at,
        "duration_seconds": duration_seconds,
    }
    if feat_id is not None:
        episode["feat_id"] = feat_id
    if approval_cycles_used is not None:
        episode["approval_cycles_used"] = approval_cycles_used
    if spec_ref is not None:
        episode["spec_ref"] = spec_ref
    # §4.1: trace_ref is REQUIRED whenever any disposition != accepted.
    if trace_ref is not None:
        episode["trace_ref"] = trace_ref
    return episode


def build_approval_decision_episode(
    *,
    gate_id: str,
    decision: str,
    cycle: int,
    latency_seconds: int,
    correlation_id: str | None = None,
    approver: str | None = None,
    escalated_to: str | None = None,
    feat_id: str | None = None,
    request_ref: str | None = None,
) -> dict[str, Any]:
    """Project a human gate decision onto the ``approval_decision`` shape (§4.2).

    ``approver`` is the **observed** responder member id (LPA-19) — required for
    ``approved``/``rejected``/``revise`` and MUST be ``None`` for ``timed_out``
    and system-driven ``escalated`` (there is no observed responder).
    ``gate_kind`` is fixed to ``planning_assumptions`` (the cyclic planning
    gate), so ``cycle`` is always carried.

    Pure — no I/O. WS4-S7 wires the fleet-memory write.
    """
    episode: dict[str, Any] = {
        "gate_id": gate_id,
        "gate_kind": "planning_assumptions",
        "decision": decision,
        "cycle": cycle,
        "latency_seconds": latency_seconds,
    }
    if correlation_id is not None:
        episode["correlation_id"] = correlation_id
    # §4.2: approver required for observed decisions, forbidden otherwise.
    if decision in ("approved", "rejected", "revise") and approver is not None:
        episode["approver"] = approver
    if escalated_to is not None:
        episode["escalated_to"] = escalated_to
    if feat_id is not None:
        episode["feat_id"] = feat_id
    if request_ref is not None:
        episode["request_ref"] = request_ref
    return episode

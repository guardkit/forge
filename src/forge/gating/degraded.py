"""Degraded-mode gating stand-ins (TASK-GATE-D659, Wave 1).

The daemon-side pre-dispatch gate (D1) runs the full tested ``gate_check``
path against **honest** degraded collaborators until evidence-based gating
lands (post-UBS-002). Per ADR-ARCH-019 / ADR-ARCH-026 the honesty posture
is:

* three readers return ``[]`` (no priors, no calibration adjustments, no
  runtime constitutional rules — the constitutional enforcement is the
  hardcoded frozenset inside :func:`forge.gating.evaluate_gate`);
* the reasoning callable is :func:`degraded_dispatch_gate_model`, which
  returns a static ``MANDATORY_HUMAN_APPROVAL`` decision with a ``null``
  threshold (degraded / training mode). It is **not** the constitutional
  ``review_pr`` shortcut — persisted :class:`GateDecision` rows stay
  truthful, and this callable is the single seam a real reasoning adapter
  later replaces.

These stand-ins live in ``forge.gating`` (not a CLI composition module)
because both the live pre-dispatch path and the boot-time rearm path reuse
them (arch-review minor).

Also home to :func:`degraded_recovery_decision` — the fallback that
rehydrates a degraded :class:`GateDecision` for a paused build whose
``stage_log`` row no longer carries a durable ``details_json["gate"]``
snapshot (a corrupt / legacy row on the rearm path).
"""

from __future__ import annotations

import json
from datetime import datetime

from forge.gating.models import (
    CalibrationAdjustment,
    ConstitutionalRule,
    GateDecision,
    GateMode,
    GateTargetKind,
    PriorReference,
)

__all__ = [
    "EmptyAdjustmentsReader",
    "EmptyPriorsReader",
    "EmptyRulesReader",
    "degraded_dispatch_gate_model",
    "degraded_recovery_decision",
]

#: Human-readable rationale stamped on every degraded-mode decision so the
#: gate history is honest about *why* the build paused: no evidence was
#: available at dispatch, so v1 never auto-approves (DF-009 ratchet).
DEGRADED_RATIONALE: str = (
    "Degraded/training mode: no coach score or priors are available at "
    "dispatch, so the gate mandates human approval before the build starts "
    "(ADR-ARCH-019 honesty posture; v1 never auto-approves)."
)

#: Static reasoning-model response body. Matches the JSON contract that
#: :func:`forge.gating.reasoning._parse_model_response` validates into a
#: ``ParsedDecision``: ``mode`` / ``rationale`` / ``threshold_applied`` /
#: ``relevant_prior_ids``. ``threshold_applied=null`` keeps the
#: ``GateDecision`` §6 invariant satisfied for ``MANDATORY_HUMAN_APPROVAL``
#: without needing ``auto_approve_override``.
_DEGRADED_RESPONSE_BODY: str = json.dumps(
    {
        "mode": GateMode.MANDATORY_HUMAN_APPROVAL.value,
        "rationale": DEGRADED_RATIONALE,
        "threshold_applied": None,
        "relevant_prior_ids": [],
    }
)


def degraded_dispatch_gate_model(prompt: str) -> str:
    """Static reasoning callable — always mandates human approval.

    Signature matches :class:`forge.gating.reasoning.ReasoningModelCall`
    (``(prompt: str) -> str``); the prompt is ignored because the degraded
    posture is unconditional. Returns the canonical
    ``MANDATORY_HUMAN_APPROVAL`` JSON body so ``evaluate_gate`` produces a
    truthful degraded :class:`GateDecision`.
    """
    return _DEGRADED_RESPONSE_BODY


class EmptyPriorsReader:
    """Degraded :class:`forge.gating.wrappers.PriorsReader` — no priors."""

    async def read_priors(
        self,
        *,
        target_kind: GateTargetKind,
        target_identifier: str,
        stage_label: str,
        build_id: str,
    ) -> list[PriorReference]:
        return []


class EmptyAdjustmentsReader:
    """Degraded :class:`forge.gating.wrappers.AdjustmentsReader` — no bias."""

    async def read_adjustments(
        self,
        *,
        target_capability: str,
        approved_only: bool,
    ) -> list[CalibrationAdjustment]:
        return []


class EmptyRulesReader:
    """Degraded :class:`forge.gating.wrappers.RulesReader` — no runtime rules.

    Constitutional enforcement is the hardcoded frozenset inside
    :func:`forge.gating.evaluate_gate`; this reader intentionally supplies
    no *additional* runtime rules (ADR-ARCH-019: no static rule registry).
    """

    async def read_rules(
        self,
        *,
        target_kind: GateTargetKind,
        target_identifier: str,
    ) -> list[ConstitutionalRule]:
        return []


def degraded_recovery_decision(
    *,
    build_id: str,
    stage_label: str,
    decided_at: datetime,
    target_kind: GateTargetKind = "subagent",
    target_identifier: str = "autobuild_runner",
) -> GateDecision:
    """Rehydrate a degraded :class:`GateDecision` for a paused-build row.

    The durable home for a pause decision is
    ``stage_log.details_json["gate"]`` (written by the gate repository's
    ``record_decision``). When that snapshot is missing — a corrupt or
    pre-D659 legacy PAUSED row surfaced on the rearm path — this helper
    reconstructs the minimal honest decision the re-emitted approval
    request needs: ``MANDATORY_HUMAN_APPROVAL``, degraded mode, no coach
    score, no threshold. The reviewer still gets a truthful (if sparse)
    card; the responder correlates only on ``request_id`` and is
    unaffected.

    Args:
        build_id: The paused build's identifier.
        stage_label: The stage the build paused at (parsed from the
            persisted ``request_id``).
        decided_at: Injected timestamp (clock hygiene — never
            ``datetime.now()``).
        target_kind: Gate target kind; defaults to ``"subagent"`` (the
            pre-dispatch gate targets the autobuild runner).
        target_identifier: Gate target identifier; defaults to
            ``"autobuild_runner"``.

    Returns:
        A degraded :class:`GateDecision` satisfying the §6 invariants.
    """
    return GateDecision(
        build_id=build_id,
        stage_label=stage_label,
        target_kind=target_kind,
        target_identifier=target_identifier,
        mode=GateMode.MANDATORY_HUMAN_APPROVAL,
        rationale=DEGRADED_RATIONALE,
        coach_score=None,
        criterion_breakdown={},
        detection_findings=[],
        evidence=[],
        threshold_applied=None,
        auto_approve_override=False,
        degraded_mode=True,
        decided_at=decided_at,
    )

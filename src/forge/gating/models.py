"""Pure-domain data models and the ``evaluate_gate`` stub for ``forge.gating``.

This module is the **declarative producer** for the Confidence-Gated
Checkpoint Protocol (FEAT-FORGE-004), per ``docs/design/models/DM-gating.md``
sections 1, 2 and 3.

Domain purity rules (DM-gating §1, ADR-ARCH-019):

* Imports are restricted to the standard library and ``pydantic``.
* No imports from ``nats_core``, ``nats-py``, ``langgraph``, or
  ``forge.adapters.*``. The body of :func:`evaluate_gate` is filled in by
  Wave 2 (TASK-CGCP-004 constitutional branch + TASK-CGCP-005 reasoning
  assembly); this module ships only the model surface and a
  ``NotImplementedError`` shell.

See also:

* ``docs/design/models/DM-gating.md`` §1 — entity schemas
* ``docs/design/models/DM-gating.md`` §2 — ``ResponseKind``
* ``docs/design/models/DM-gating.md`` §3 — ``evaluate_gate`` signature
* ``docs/design/models/DM-gating.md`` §6 — invariants enforced by validators
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# Type aliases (DM-gating §1)
# ---------------------------------------------------------------------------

#: Allowed Graphiti groups for a :class:`PriorReference`. DM-gating §1 +
#: §6 invariant "PriorReference.group_id in {forge_pipeline_history,
#: forge_calibration_history}".
PriorGroupId = Literal["forge_pipeline_history", "forge_calibration_history"]

#: Severity values mirrored from the specialist-agent Coach output schema
#: (DM-gating §1, ``DetectionFinding``).
DetectionSeverity = Literal["low", "medium", "high", "critical"]

#: Target kinds carried on a :class:`GateDecision`. DM-gating §1.
GateTargetKind = Literal["local_tool", "fleet_capability", "subagent"]


# ---------------------------------------------------------------------------
# Enums (DM-gating §1, §2)
# ---------------------------------------------------------------------------


class GateMode(str, Enum):
    """How a gate decision should be acted on by the pipeline state machine.

    Members map 1:1 to DM-gating §1. ``MANDATORY_HUMAN_APPROVAL`` is
    unconditional (e.g. PR review, early build plans) and is the
    constitutional override per ADR-ARCH-026.
    """

    AUTO_APPROVE = "AUTO_APPROVE"
    FLAG_FOR_REVIEW = "FLAG_FOR_REVIEW"
    HARD_STOP = "HARD_STOP"
    # Unconditional — PR review, early build plans (ADR-ARCH-026).
    MANDATORY_HUMAN_APPROVAL = "MANDATORY_HUMAN_APPROVAL"


class ResponseKind(str, Enum):
    """Normalised classification of Rich's reply to a calibration prompt.

    Mirrors DM-gating §2. Normalisation is performed by
    ``forge.adapters.history_parser`` during ingestion; the raw response
    is preserved verbatim in :class:`CalibrationEvent.response_raw`.
    """

    # "A A A A" / "accept defaults"
    ACCEPT_ALL = "ACCEPT_ALL"
    ACCEPT_WITH_EDIT = "ACCEPT_WITH_EDIT"
    REJECT = "REJECT"
    DEFER = "DEFER"
    CUSTOM = "CUSTOM"


# ---------------------------------------------------------------------------
# Pydantic models (DM-gating §1)
# ---------------------------------------------------------------------------


class PriorReference(BaseModel):
    """Records which Graphiti entity informed a gate decision.

    Attributes:
        entity_id: Graphiti UUID or natural key of the source entity.
        group_id: Graphiti group the entity lives in. Constrained to the
            two valid groups per DM-gating §6.
        summary: Short recap of what the prior says — written by the
            reasoning model.
        relevance_score: Optional reasoning-model rating in ``[0.0, 1.0]``;
            ``None`` when the reasoning model declined to score.
    """

    model_config = ConfigDict(extra="forbid")

    entity_id: str = Field(
        min_length=1,
        description="Graphiti UUID or natural key of the source entity.",
    )
    group_id: PriorGroupId = Field(
        description=(
            "Graphiti group the entity belongs to "
            "(forge_pipeline_history | forge_calibration_history)."
        ),
    )
    summary: str = Field(
        description="Short recap of what the prior says.",
    )
    relevance_score: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Reasoning model's optional relevance rating in [0, 1].",
    )


class DetectionFinding(BaseModel):
    """Specialist-agent Coach finding folded into a gate decision.

    Mirrors the specialist-agent Coach output shape (DM-gating §1).

    Attributes:
        pattern: Detection pattern, e.g. ``"PHANTOM"``, ``"SCOPE_CREEP"``,
            ``"UNGROUNDED"``.
        severity: Severity bucket — one of
            ``low | medium | high | critical``.
        evidence: Human-readable excerpt or file reference supporting the
            finding.
        criterion: Optional link back to a key in
            :attr:`GateDecision.criterion_breakdown`.
    """

    model_config = ConfigDict(extra="forbid")

    pattern: str = Field(
        min_length=1,
        description="Detection pattern, e.g. PHANTOM, SCOPE_CREEP, UNGROUNDED.",
    )
    severity: DetectionSeverity = Field(
        description="Severity bucket reported by Coach.",
    )
    evidence: str = Field(
        description="Human-readable excerpt or file reference.",
    )
    criterion: str | None = Field(
        default=None,
        description="Optional criterion-breakdown key this finding maps to.",
    )


class GateDecision(BaseModel):
    """The output of :func:`evaluate_gate` (DM-gating §1).

    Captured in ``stage_log.details_json["gate"]`` and written to Graphiti
    as a standalone entity in ``forge_pipeline_history``.

    Invariants are enforced by :meth:`_check_invariants` per DM-gating §6.
    """

    model_config = ConfigDict(extra="forbid")

    build_id: str = Field(min_length=1, description="Pipeline build identifier.")
    stage_label: str = Field(
        min_length=1,
        description="Stage label within the build that produced this decision.",
    )
    target_kind: GateTargetKind = Field(
        description="Whether the gate applies to a local tool, fleet capability, or subagent.",
    )
    target_identifier: str = Field(
        min_length=1,
        description="Identifier of the gated target (tool name, capability, or subagent).",
    )

    mode: GateMode = Field(description="Action mode for the pipeline state machine.")
    rationale: str = Field(
        description="Reasoning model's explanation for the chosen mode.",
    )

    coach_score: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Coach overall score in [0, 1]; None in degraded mode.",
    )
    criterion_breakdown: dict[str, float] = Field(
        default_factory=dict,
        description="Per-criterion scores in [0, 1] (see §6 invariant).",
    )
    detection_findings: list[DetectionFinding] = Field(
        default_factory=list,
        description="Coach findings folded into this decision.",
    )

    evidence: list[PriorReference] = Field(
        default_factory=list,
        description="Graphiti priors that informed this decision.",
    )
    threshold_applied: float | None = Field(
        default=None,
        description="Threshold value if one was used; None when no static threshold applied.",
    )

    auto_approve_override: bool = Field(
        default=False,
        description=(
            "True when ADR-ARCH-026 belt+braces forced MANDATORY_HUMAN_APPROVAL."
        ),
    )
    degraded_mode: bool = Field(
        default=False,
        description="True when no Coach score was available.",
    )

    decided_at: datetime = Field(
        description="UTC timestamp the decision was produced.",
    )

    @model_validator(mode="after")
    def _check_invariants(self) -> GateDecision:
        """Enforce DM-gating §6 invariants.

        Raises:
            ValueError: If any §6 invariant is violated:
                * ``mode == MANDATORY_HUMAN_APPROVAL`` ⇒
                  ``auto_approve_override is True`` OR ``threshold_applied is None``
                * ``coach_score is None`` ⇒
                  ``mode in {FLAG_FOR_REVIEW, HARD_STOP, MANDATORY_HUMAN_APPROVAL}``
                * ``criterion_breakdown`` values must lie in ``[0.0, 1.0]``.
        """
        # Invariant 1 — mandatory mode requires either the override flag or
        # the absence of a static threshold (no quietly-thresholded mandatory).
        if self.mode is GateMode.MANDATORY_HUMAN_APPROVAL:
            if not self.auto_approve_override and self.threshold_applied is not None:
                raise ValueError(
                    "GateDecision invariant violated: mode=MANDATORY_HUMAN_APPROVAL "
                    "requires auto_approve_override=True OR threshold_applied=None.",
                )

        # Invariant 2 — degraded mode (no Coach score) cannot auto-approve.
        if self.coach_score is None:
            allowed = {
                GateMode.FLAG_FOR_REVIEW,
                GateMode.HARD_STOP,
                GateMode.MANDATORY_HUMAN_APPROVAL,
            }
            if self.mode not in allowed:
                raise ValueError(
                    "GateDecision invariant violated: coach_score is None requires "
                    "mode in {FLAG_FOR_REVIEW, HARD_STOP, MANDATORY_HUMAN_APPROVAL}; "
                    f"got mode={self.mode.value!r}.",
                )

        # Invariant 3 — every criterion-breakdown value must be in [0.0, 1.0].
        offenders = [
            f"{key}={value!r}"
            for key, value in self.criterion_breakdown.items()
            if not 0.0 <= float(value) <= 1.0
        ]
        if offenders:
            joined = ", ".join(offenders)
            raise ValueError(
                "GateDecision invariant violated: criterion_breakdown values must "
                f"lie in [0.0, 1.0]; out-of-range entries: {joined}.",
            )

        return self


class CalibrationAdjustment(BaseModel):
    """Proposed bias entity (DM-gating §1).

    ``forge.learning`` generates these; Rich approves them; the approved
    adjustment lands in ``forge_pipeline_history`` and informs future
    :func:`evaluate_gate` calls.
    """

    model_config = ConfigDict(extra="forbid")

    adjustment_id: str = Field(
        min_length=1,
        description="UUID for this adjustment.",
    )

    target_capability: str = Field(
        min_length=1,
        description="Capability the bias targets, e.g. 'review_specification'.",
    )
    project_scope: str | None = Field(
        default=None,
        description="Project scope; None means fleet-wide.",
    )

    observed_pattern: str = Field(
        description=(
            "Empirical pattern motivating the adjustment, e.g. "
            "'6 of 10 flag-for-reviews overridden at scores 0.78-0.82'."
        ),
    )
    proposed_bias: str = Field(
        description="Human-readable adjustment proposed by forge.learning.",
    )

    approved_by_rich: bool = Field(
        default=False,
        description="True once Rich has approved the adjustment.",
    )
    approved_at: datetime | None = Field(
        default=None,
        description="UTC timestamp of Rich's approval; None until approved.",
    )
    expires_at: datetime | None = Field(
        default=None,
        description="Optional time-bounded bias; None means open-ended.",
    )

    supersedes: str | None = Field(
        default=None,
        description=(
            "adjustment_id of the prior adjustment this replaces, or None when "
            "this is a fresh bias."
        ),
    )


class ConstitutionalRule(BaseModel):
    """Belt+braces executor-layer rule consumed by :func:`evaluate_gate`.

    Placeholder model used only to type the :func:`evaluate_gate` signature
    (DM-gating §3). The detailed schema is filled in by TASK-CGCP-004 when
    the constitutional override branch is wired up. The shape is kept
    deliberately minimal here so importers don't need to anticipate fields
    that have not yet been designed.
    """

    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(
        min_length=1,
        description="Stable identifier for the rule (e.g. 'PR_REVIEW_HUMAN_ONLY').",
    )
    description: str = Field(
        description="Human-readable description of the rule.",
    )


# ---------------------------------------------------------------------------
# Pure reasoning entry point (DM-gating §3)
# ---------------------------------------------------------------------------


def evaluate_gate(
    *,
    target_kind: str,
    target_identifier: str,
    stage_label: str,
    coach_score: float | None,
    criterion_breakdown: dict[str, float],
    detection_findings: list[DetectionFinding],
    retrieved_priors: list[PriorReference],
    calibration_adjustments: list[CalibrationAdjustment],
    constitutional_rules: list[ConstitutionalRule],
) -> GateDecision:
    """Pure-reasoning gate evaluator (DM-gating §3).

    This shell is intentionally unimplemented. The body is filled in by:

    * TASK-CGCP-004 — constitutional override branch (ADR-ARCH-026
      executor-layer hard-coded check).
    * TASK-CGCP-005 — reasoning-model prompt assembly and structured-response
      parsing into a :class:`GateDecision`.

    The signature is keyword-only by design: callers must pass every input
    explicitly so partial-application bugs at the tool layer are impossible.

    Args:
        target_kind: Kind of gated target — local tool, fleet capability,
            or subagent.
        target_identifier: Identifier of the gated target.
        stage_label: Pipeline stage label producing the decision.
        coach_score: Specialist-agent Coach overall score, or ``None`` when
            unavailable (degraded mode).
        criterion_breakdown: Per-criterion Coach scores in ``[0, 1]``.
        detection_findings: Coach pattern findings.
        retrieved_priors: Graphiti priors retrieved by the tool layer.
        calibration_adjustments: Approved bias entities.
        constitutional_rules: ADR-ARCH-026 belt+braces executor-layer rules.

    Returns:
        A :class:`GateDecision`. (Once implemented.)

    Raises:
        NotImplementedError: Always — body is reserved for Wave 2.
    """
    raise NotImplementedError(
        "forge.gating.evaluate_gate is not implemented yet. "
        "Body is filled in by TASK-CGCP-004 (constitutional override) and "
        "TASK-CGCP-005 (reasoning-model assembly). See DM-gating.md §3.",
    )


__all__ = [
    "CalibrationAdjustment",
    "ConstitutionalRule",
    "DetectionFinding",
    "DetectionSeverity",
    "GateDecision",
    "GateMode",
    "GateTargetKind",
    "PriorGroupId",
    "PriorReference",
    "ResponseKind",
    "evaluate_gate",
]

"""Planning model resolution audit (DF-004 compliance).

This module provides the `audit_planning_model_resolution` pure function that
verifies planning model configuration against DF-004 (fleet REGISTER): planning
model resolution can never silently escalate to cloud.

The audit is deliberately NOT a Pydantic validator — a validator would brick the
whole daemon on violation, contradicting ASSUM-011's "build intake unaffected"
(DDR-007 soft-fail posture). Boot wiring happens in TASK-MP-009.

See TASK-MP-001 for implementation context.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from forge.config.models import PlanningConfig


@dataclass(frozen=True)
class PlanningAuditResult:
    """Result of planning model resolution audit.

    Attributes:
        passed: True if audit passed, False if violation detected
        violation: Violation identifier (e.g., "DF-004") if failed, None if passed
        reason: Human-readable explanation of result
    """

    passed: bool
    violation: str | None
    reason: str


def audit_planning_model_resolution(config: PlanningConfig) -> PlanningAuditResult:
    """Audit planning model resolution for DF-004 compliance.

    DF-004 (fleet REGISTER): Planning model resolution can never silently
    escalate to cloud. This audit verifies that the `fallbacks` list in
    `model_resolution` is empty.

    This is a pure function:
    - No I/O operations
    - No exceptions raised
    - Deterministic output for same input
    - No side effects

    Args:
        config: PlanningConfig instance to audit

    Returns:
        PlanningAuditResult with pass/fail status and reason

    Examples:
        >>> config = PlanningConfig()
        >>> result = audit_planning_model_resolution(config)
        >>> result.passed
        True

        >>> config.model_resolution.fallbacks = ["claude-opus-4.6"]
        >>> result = audit_planning_model_resolution(config)
        >>> result.passed
        False
        >>> result.violation
        'DF-004'
    """
    fallbacks = config.model_resolution.fallbacks

    if not fallbacks:
        return PlanningAuditResult(
            passed=True,
            violation=None,
            reason="Planning model resolution has no fallbacks (DF-004 compliant)",
        )

    return PlanningAuditResult(
        passed=False,
        violation="DF-004",
        reason=(
            f"DF-004 violation: Planning model resolution has {len(fallbacks)} "
            f"fallback(s) configured. Cloud escalation is forbidden for planning "
            f"models. Remove all entries from model_resolution.fallbacks."
        ),
    )


__all__ = [
    "PlanningAuditResult",
    "audit_planning_model_resolution",
]

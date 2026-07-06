"""Mode P planning infrastructure (FEAT-SPL-002).

This package provides planning approval-routing configuration and audit
functions for the Mode P planning workflow.

Key components:
- audit: DF-004 compliance verification for planning model resolution
- planner: Pure-function planning chain planner (TASK-MP-003)
"""

from forge.planning.audit import (
    PlanningAuditResult,
    audit_planning_model_resolution,
)
from forge.planning.planner import (
    PLANNING_CHAIN,
    PLANNING_FORBIDDEN_STAGES,
    PRODUCT_DOCS_STAGE_LABEL,
    BoundaryViolation,
    DispatchProductOwner,
    ExecuteHandoff,
    Fail,
    PauseAtCheckpoint,
    PlanningDecision,
    PlanningEvent,
    plan_next_step,
)

__all__ = [
    "PlanningAuditResult",
    "audit_planning_model_resolution",
    "PLANNING_CHAIN",
    "PLANNING_FORBIDDEN_STAGES",
    "PRODUCT_DOCS_STAGE_LABEL",
    "BoundaryViolation",
    "DispatchProductOwner",
    "ExecuteHandoff",
    "Fail",
    "PauseAtCheckpoint",
    "PlanningDecision",
    "PlanningEvent",
    "plan_next_step",
]

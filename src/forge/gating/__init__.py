"""Domain-pure ``forge.gating`` package.

This sub-package owns the confidence-gated checkpoint protocol per
``DM-gating.md``. It is **domain-pure**: zero imports from ``nats_core``,
``nats-py``, ``langgraph``, or ``forge.adapters.*``.

Re-exports:

* :func:`derive_request_id` — deterministic ``request_id`` derivation
  (TASK-CGCP-003).
* :class:`GateMode`, :class:`ResponseKind` — enums per DM-gating §1, §2.
* :class:`PriorReference`, :class:`DetectionFinding`,
  :class:`GateDecision`, :class:`CalibrationAdjustment`,
  :class:`ConstitutionalRule` — Pydantic v2 models per DM-gating §1.
* :func:`evaluate_gate` — pure-reasoning entry point (DM-gating §3),
  reasoning-branch implementation (TASK-CGCP-005) wired against an
  injected :class:`ReasoningModelCall`. The constitutional-override
  branch (TASK-CGCP-004) is a sibling check that runs ahead of this
  function.
* :class:`ReasoningModelCall`,
  :class:`forge.gating.reasoning.ReasoningResponseError`,
  :class:`forge.gating.reasoning.PostConditionError` — extension points
  and error types for the reasoning branch.
"""

from .identity import derive_request_id, parse_request_id
from .models import (
    CalibrationAdjustment,
    ConstitutionalRule,
    DetectionFinding,
    DetectionSeverity,
    GateDecision,
    GateMode,
    GateTargetKind,
    PriorGroupId,
    PriorReference,
    ResponseKind,
    evaluate_gate,
)
from .reasoning import (
    PostConditionError,
    ReasoningModelCall,
    ReasoningResponseError,
)

__all__ = [
    "CalibrationAdjustment",
    "ConstitutionalRule",
    "DetectionFinding",
    "DetectionSeverity",
    "GateDecision",
    "GateMode",
    "GateTargetKind",
    "PostConditionError",
    "PriorGroupId",
    "PriorReference",
    "ReasoningModelCall",
    "ReasoningResponseError",
    "ResponseKind",
    "derive_request_id",
    "evaluate_gate",
    "parse_request_id",
]

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
  currently a ``NotImplementedError`` shell that Wave 2 fills in
  (TASK-CGCP-004 + TASK-CGCP-005).
"""

from .identity import derive_request_id
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
    "derive_request_id",
    "evaluate_gate",
]

"""``forge.memory`` — entity models and credential redaction for Graphiti writes.

This package is the **declarative producer** for the entities written to the
``forge_pipeline_history`` and ``forge_calibration_history`` Graphiti groups
(FEAT-FORGE-006, TASK-IC-001). Subsequent tasks (TASK-IC-002+) layer the
Graphiti write call itself on top of these models — this package is
intentionally I/O-free.

Public surface:

- :class:`~forge.memory.models.GateDecision`
- :class:`~forge.memory.models.CapabilityResolution`
- :class:`~forge.memory.models.OverrideEvent`
- :class:`~forge.memory.models.CalibrationAdjustment`
- :class:`~forge.memory.models.SessionOutcome`
- :class:`~forge.memory.models.CalibrationEvent`
- :func:`~forge.memory.redaction.redact_credentials`
"""

from __future__ import annotations

from .models import (
    CalibrationAdjustment,
    CalibrationEvent,
    CapabilityResolution,
    GateDecision,
    OverrideEvent,
    SessionOutcome,
    SessionOutcomeKind,
)
from .ordering import record_stage_event
from .redaction import redact_credentials

__all__ = [
    "CalibrationAdjustment",
    "CalibrationEvent",
    "CapabilityResolution",
    "GateDecision",
    "OverrideEvent",
    "SessionOutcome",
    "SessionOutcomeKind",
    "record_stage_event",
    "redact_credentials",
]

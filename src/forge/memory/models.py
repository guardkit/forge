"""Entity models for ``forge_pipeline_history`` and ``forge_calibration_history``.

This module is the **declarative producer** for every entity written to the
two Graphiti groups owned by FEAT-FORGE-006:

* ``forge_pipeline_history`` — :class:`GateDecision`,
  :class:`CapabilityResolution`, :class:`OverrideEvent`,
  :class:`CalibrationAdjustment`, :class:`SessionOutcome`.
* ``forge_calibration_history`` — :class:`CalibrationEvent`.

Two key resolutions are encoded in the field-level docstrings below; both
are load-bearing for downstream tasks (TASK-IC-002+) and must not be
weakened without a paired feature-review:

* **ASSUM-007 resolution** — the SQLite-row UUID is the Graphiti
  ``entity_id`` for every pipeline-history entity. ``entity_id`` MUST be
  sourced from SQLite, never generated at write time. This guarantees
  that every Graphiti write is idempotent and cross-referenceable from
  the SQLite ledger.
* **ASSUM-008 resolution** — :pyattr:`SessionOutcome.gate_decision_ids`
  is ordered ascending by each referenced ``GateDecision.decided_at``
  timestamp. Callers must sort the list before constructing the
  :class:`SessionOutcome`; downstream consumers rely on the ordering for
  timeline reconstruction.

For Q&A captured into ``forge_calibration_history``, a different identity
strategy applies: :pyattr:`CalibrationEvent.entity_id` is **deterministic**
from ``(source_file, line_range_hash)`` per the
``@data-integrity deterministic-qa-identity`` rule. Re-ingesting the same
source-file/line-range pair must yield the same ``entity_id`` so that
re-ingestion is idempotent (TASK-IC-001 AC-004).

Module purity:

* Imports are restricted to the standard library and ``pydantic``. No
  ``nats_core``, ``langgraph``, or ``forge.adapters.*`` imports — this is
  a pure schema layer that downstream Graphiti-write code consumes.
* The module has no side effects at import time.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

#: Allowed terminal outcomes for a pipeline session.
SessionOutcomeKind = Literal["success", "failure", "aborted"]


class GateDecision(BaseModel):
    """A confidence-gate decision row mirrored from SQLite into Graphiti.

    Each :class:`GateDecision` represents a single decision point in the
    pipeline (e.g. "should we accept the planning agent's output?"). The
    SQLite row is the source of truth; the Graphiti node is a search-
    indexable mirror keyed by the same UUID.
    """

    model_config = ConfigDict(extra="forbid")

    entity_id: UUID = Field(
        ...,
        description=(
            "MUST be sourced from SQLite UUID, never generated at write "
            "time (ASSUM-007 resolution)."
        ),
    )
    stage_name: str = Field(..., min_length=1)
    decided_at: datetime
    score: float = Field(..., ge=0.0, le=1.0)
    criterion_breakdown: dict[str, float] = Field(default_factory=dict)
    rationale: str


class CapabilityResolution(BaseModel):
    """The agent that fulfilled a requested capability for a build.

    Captures *which* fleet agent (``agent_id``) was selected to satisfy
    *which* capability, *when*, against *which* version of the discovery
    cache. Used to reconstruct the live-discovery decisions at the time a
    build executed.
    """

    model_config = ConfigDict(extra="forbid")

    entity_id: UUID = Field(
        ...,
        description=(
            "MUST be sourced from SQLite UUID, never generated at write "
            "time (ASSUM-007 resolution)."
        ),
    )
    agent_id: str = Field(..., min_length=1)
    capability: str = Field(..., min_length=1)
    selected_at: datetime
    discovery_cache_version: str = Field(..., min_length=1)


class OverrideEvent(BaseModel):
    """An operator override of a confidence-gate recommendation.

    When an operator (Rich) overrides a recommendation, we record both
    sides — what the system suggested (``original_recommendation``) and
    what the operator decided (``operator_decision``) — together with
    the rationale. ``operator_rationale`` is a free-text field and MUST
    pass through :func:`forge.memory.redaction.redact_credentials` before
    construction.
    """

    model_config = ConfigDict(extra="forbid")

    entity_id: UUID = Field(
        ...,
        description=(
            "MUST be sourced from SQLite UUID, never generated at write "
            "time (ASSUM-007 resolution)."
        ),
    )
    gate_decision_id: UUID = Field(
        ...,
        description=(
            "UUID of the :class:`GateDecision` this override targets. "
            "Also sourced from SQLite (ASSUM-007 resolution)."
        ),
    )
    original_recommendation: str = Field(..., min_length=1)
    operator_decision: str = Field(..., min_length=1)
    operator_rationale: str
    decided_at: datetime


class CalibrationAdjustment(BaseModel):
    """A proposed (or approved) calibration-parameter change.

    Adjustments are immutable records — to "edit" an adjustment, propose
    a new one whose ``supersedes`` field references the old adjustment's
    ``entity_id``. ``approved=False`` indicates a still-pending proposal;
    ``approved=True`` indicates the operator accepted it.
    """

    model_config = ConfigDict(extra="forbid")

    entity_id: UUID = Field(
        ...,
        description=(
            "MUST be sourced from SQLite UUID, never generated at write "
            "time (ASSUM-007 resolution)."
        ),
    )
    parameter: str = Field(..., min_length=1)
    old_value: str
    new_value: str
    approved: bool
    supersedes: Optional[UUID] = Field(
        default=None,
        description=(
            "Optional ``entity_id`` of the prior :class:`CalibrationAdjustment` "
            "that this one supersedes. ``None`` if this is the first "
            "adjustment for the parameter."
        ),
    )
    proposed_at: datetime
    expires_at: datetime


class SessionOutcome(BaseModel):
    """The terminal outcome of a single pipeline session.

    A :class:`SessionOutcome` is emitted exactly once per ``build_id``
    when the pipeline reaches a terminal state. It carries the ordered
    list of gate decisions that contributed to the run, so that the
    timeline can be reconstructed without an extra graph traversal.
    """

    model_config = ConfigDict(extra="forbid")

    entity_id: UUID = Field(
        ...,
        description=(
            "MUST be sourced from SQLite UUID, never generated at write "
            "time (ASSUM-007 resolution)."
        ),
    )
    build_id: str = Field(..., min_length=1)
    outcome: SessionOutcomeKind
    gate_decision_ids: list[UUID] = Field(
        default_factory=list,
        description=(
            "Ordered list of :class:`GateDecision` ``entity_id`` values "
            "for this session. The list MUST be sorted ascending by each "
            "referenced ``GateDecision.decided_at`` timestamp "
            "(ASSUM-008 resolution). Callers are responsible for sorting "
            "before construction; downstream consumers rely on the order "
            "for timeline reconstruction."
        ),
    )
    closed_at: datetime


class CalibrationEvent(BaseModel):
    """A captured Q&A turn from the calibration log.

    Unlike the pipeline-history entities, ``CalibrationEvent.entity_id``
    is **not** a SQLite UUID — it is a deterministic identifier derived
    from ``(source_file, line_range_hash)`` per the
    ``@data-integrity deterministic-qa-identity`` rule.

    The deterministic identity guarantees that re-ingesting the same
    source file (e.g. after a parser bugfix) does not duplicate Q&A nodes
    in Graphiti — the second write replaces the first because the
    ``entity_id`` collides. ``partial=True`` indicates a tolerated
    partial-parse case where only one side of the Q&A could be recovered.
    """

    model_config = ConfigDict(extra="forbid")

    entity_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Deterministic identifier derived from ``(source_file, "
            "line_range_hash)`` per ``@data-integrity "
            "deterministic-qa-identity``. Re-ingestion of the same "
            "(source_file, line range) MUST yield the same ``entity_id`` "
            "so that writes are idempotent."
        ),
    )
    source_file: str = Field(..., min_length=1)
    question: str
    answer: str
    captured_at: datetime
    partial: bool = Field(
        default=False,
        description=(
            "``True`` when the parser could only recover one side of the "
            "Q&A; ``False`` when both question and answer were captured "
            "cleanly. Callers MUST set this flag explicitly when "
            "constructing from a partial parse."
        ),
    )


__all__ = [
    "CalibrationAdjustment",
    "CalibrationEvent",
    "CapabilityResolution",
    "GateDecision",
    "OverrideEvent",
    "SessionOutcome",
    "SessionOutcomeKind",
]

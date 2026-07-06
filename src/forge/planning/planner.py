"""Mode P planning chain planner (TASK-MP-003, FEAT-SPL-002).

This module defines the planning chain data and :func:`plan_next_step` — a
pure-function planner that takes a planning run's recorded history and returns
the next permitted action in the planning chain. The planner is the security
boundary for Mode P: planning runs never consult a reasoning model to advance
the chain, and planning runs never advance into build stages.

The Mode P decision rule: a deterministic pure function over the planning run's
recorded history (mode_b_planner.py shape — stateless, no I/O, no model calls).
Enforcement locus for the stage boundary is HERE, in the planning package —
``mode_chains_data.py`` stays **byte-identical** (panel amendment to ASSUM-009),
which keeps the "Mode B forbidden-stage logic untouched" hard constraint
mechanically checkable.

Planning chain
--------------

The planning chain is a three-step sequence:

    PRODUCT_OWNER → PRODUCT_DOCS_CHECKPOINT → HANDOFF

Where:
- PRODUCT_OWNER: The product-owner specialist stage (dispatchable)
- PRODUCT_DOCS_CHECKPOINT: A manual review checkpoint before handoff
- HANDOFF: Transition to Mode B feature build

Pure-function shape
--------------------

The planner is stateless. Every call takes ``history`` and returns a
:class:`PlanningDecision`. Persisted state lives in the
``planning_run_events`` rows already; the planner does not own any I/O surface
and does no mutation.

References
----------

- TASK-MP-003 — this implementation
- FEAT-SPL-002 — Mode P planning workflow
- ASSUM-009 panel amendment — mode_chains_data.py stays byte-identical
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Union, runtime_checkable

from forge.pipeline.stage_taxonomy import StageClass

__all__ = [
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


# ---------------------------------------------------------------------------
# Chain data constants
# ---------------------------------------------------------------------------


#: Planning chain step tuple: PRODUCT_OWNER → PRODUCT_DOCS_CHECKPOINT → HANDOFF
#:
#: This is the permitted sequence for planning runs. The PRODUCT_DOCS_CHECKPOINT
#: is represented by PRODUCT_OWNER stage with a special stage_label to distinguish
#: it from the initial dispatch.
PLANNING_CHAIN: tuple[StageClass, ...] = (
    StageClass.PRODUCT_OWNER,
    # PRODUCT_DOCS_CHECKPOINT is not a StageClass; it's a pause point
    # HANDOFF is not a StageClass; it's a decision outcome
)


#: Product docs stage label constant
#:
#: Used to identify the checkpoint pause in the planning run events.
PRODUCT_DOCS_STAGE_LABEL = "product_docs"


#: Forbidden stages in planning runs — all build stages
#:
#: Covers everything in MODE_A_CHAIN except PRODUCT_OWNER, plus AUTOBUILD
#: and PULL_REQUEST_REVIEW explicitly. Planning runs never advance into
#: build stages; finding any of these in history triggers BoundaryViolation.
PLANNING_FORBIDDEN_STAGES: frozenset[StageClass] = frozenset(
    {
        StageClass.ARCHITECT,
        StageClass.SYSTEM_ARCH,
        StageClass.SYSTEM_DESIGN,
        StageClass.FEATURE_SPEC,
        StageClass.FEATURE_PLAN,
        StageClass.AUTOBUILD,
        StageClass.PULL_REQUEST_REVIEW,
        StageClass.TASK_REVIEW,
        StageClass.TASK_WORK,
    }
)


# ---------------------------------------------------------------------------
# PlanningEvent — minimal Protocol over a planning_run_events row
# ---------------------------------------------------------------------------


@runtime_checkable
class PlanningEvent(Protocol):
    """Minimal planning event shape consumed by the planner.

    Production wires the planning_run_events rows; tests inject simple
    dataclasses or :class:`types.SimpleNamespace` instances with the same
    attributes.

    The Protocol is deliberately narrow — no timestamps, no approval scores,
    no gate metadata. The planner only needs to know which stage ran, what
    the status was, and a small details bag for decision mapping.

    Attributes:
        stage: The :class:`StageClass` this event records.
        status: The event status ("approved", "failed", "error", "checkpoint_cleared").
        details: Optional details dictionary for extended metadata.
    """

    stage: StageClass
    status: str
    details: Mapping[str, Any]


# ---------------------------------------------------------------------------
# Decision types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DispatchProductOwner:
    """Decision: dispatch the PRODUCT_OWNER specialist stage.

    Returned when the history is empty (planning run entry point).

    Attributes:
        rationale: Human-readable reason for this decision.
    """

    rationale: str = "Planning run entry: dispatch PRODUCT_OWNER specialist"


@dataclass(frozen=True)
class PauseAtCheckpoint:
    """Decision: pause at PRODUCT_DOCS_CHECKPOINT for manual review.

    Returned when PRODUCT_OWNER has completed successfully but the checkpoint
    has not been cleared yet.

    Attributes:
        rationale: Human-readable reason for this decision.
    """

    rationale: str = (
        "PRODUCT_OWNER complete: pause at PRODUCT_DOCS_CHECKPOINT for review"
    )


@dataclass(frozen=True)
class ExecuteHandoff:
    """Decision: execute handoff to Mode B feature build.

    Returned when the checkpoint has been cleared and the planning run is
    ready to transition to Mode B.

    Attributes:
        rationale: Human-readable reason for this decision.
    """

    rationale: str = "Checkpoint cleared: execute handoff to Mode B"


@dataclass(frozen=True)
class Fail:
    """Decision: planning run failed due to dispatch error.

    Returned when a stage dispatch fails (status "failed" or "error").

    Attributes:
        reason: Specific failure reason from the dispatch error.
    """

    reason: str


@dataclass(frozen=True)
class BoundaryViolation:
    """Decision: planning run violated stage boundary.

    Returned when history contains any PLANNING_FORBIDDEN_STAGES entry.
    This is a security boundary: planning runs must never advance into
    build stages.

    Attributes:
        violated_stage: The forbidden stage that appeared in history.
        rationale: Human-readable reason for this violation.
    """

    violated_stage: StageClass
    rationale: str


#: Union of all planning decision types
PlanningDecision = Union[
    DispatchProductOwner,
    PauseAtCheckpoint,
    ExecuteHandoff,
    Fail,
    BoundaryViolation,
]


# ---------------------------------------------------------------------------
# Pure-function planner
# ---------------------------------------------------------------------------


def plan_next_step(
    history: Sequence[PlanningEvent],
) -> PlanningDecision:
    """Determine the next planning action from recorded history.

    This is a total and pure function: every history shape yields exactly one
    :class:`PlanningDecision`, and the same history always yields the same
    decision (no I/O, no model calls, no side effects).

    Decision flow:
        1. Check for boundary violations (forbidden stages in history)
        2. Check for dispatch failures (failed/error status)
        3. Empty history → DispatchProductOwner
        4. PRODUCT_OWNER approved → PauseAtCheckpoint
        5. Checkpoint cleared → ExecuteHandoff

    Args:
        history: Sequence of planning events (planning_run_events rows).

    Returns:
        PlanningDecision: One of DispatchProductOwner, PauseAtCheckpoint,
            ExecuteHandoff, Fail, or BoundaryViolation.

    Examples:
        >>> plan_next_step(history=())
        DispatchProductOwner(rationale='Planning run entry: dispatch PRODUCT_OWNER specialist')

        >>> from types import SimpleNamespace
        >>> history = [SimpleNamespace(
        ...     stage=StageClass.PRODUCT_OWNER,
        ...     status="approved",
        ...     details={"artefact_paths": ("docs/product-owner.md",)}
        ... )]
        >>> plan_next_step(history)
        PauseAtCheckpoint(rationale='PRODUCT_OWNER complete: pause at PRODUCT_DOCS_CHECKPOINT for review')
    """
    # AC-003: Boundary violation check (MUST come first)
    for event in history:
        if event.stage in PLANNING_FORBIDDEN_STAGES:
            return BoundaryViolation(
                violated_stage=event.stage,
                rationale=f"Stage {event.stage.value} is forbidden in planning runs",
            )

    # AC-005: Dispatch failure check
    for event in history:
        if event.status in ("failed", "error"):
            error_detail = event.details.get("error", "") or event.details.get(
                "outcome", ""
            )
            reason = (
                f"Dispatch failed for {event.stage.value}: {error_detail}"
                if error_detail
                else f"Dispatch failed for {event.stage.value}"
            )
            return Fail(reason=reason)

    # Empty history → dispatch PRODUCT_OWNER
    if not history:
        return DispatchProductOwner()

    # Check for checkpoint cleared
    checkpoint_cleared = any(
        event.status == "checkpoint_cleared"
        and event.details.get("stage_label") == PRODUCT_DOCS_STAGE_LABEL
        for event in history
    )

    if checkpoint_cleared:
        return ExecuteHandoff()

    # Check for PRODUCT_OWNER completion
    product_owner_approved = any(
        event.stage == StageClass.PRODUCT_OWNER and event.status == "approved"
        for event in history
    )

    if product_owner_approved:
        return PauseAtCheckpoint()

    # If we have history but no approved PRODUCT_OWNER yet, wait
    # (in practice, this should mean PRODUCT_OWNER is still running)
    return DispatchProductOwner()

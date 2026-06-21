"""``Runbook`` and ``Step`` domain models (TASK-RSP-001).

Frozen-dataclass domain models for the Forge output-side loop's runbook
persistence. Pure data + validation — no SQL, no I/O. These types are
consumed by the migration's CHECK set (TASK-RSP-002) and by the repository
(TASK-RSP-003/004).

Relevant assumptions:

- **ASSUM-001**: The runbook overall status uses the same value set as
  step status, so both reuse :class:`StepStatus` (no separate enum).
- **ASSUM-002**: A runbook must have at least one step — empty ``steps``
  tuple raises :class:`RunbookValidationError`.
- **ASSUM-004** (R1, reconciled with FEAT-RBX): ``current_step_index`` points
  to a valid step or to the terminal position ``len(steps)`` (one past the last
  step, the completion marker): ``0 <= current_step_index <= len(steps)``.
- **ASSUM-005**: The ``step_type`` must be a non-empty string (free-form
  this phase — no closed enum).
- **ASSUM-007**: A step has no result until one is recorded — ``Step.result``
  is ``StepResult | None``.
- **ASSUM-008**: Step execution records two timestamps: ``started_at`` and
  ``completed_at`` in :class:`StepResult`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping

__all__ = [
    "StepStatus",
    "StepResult",
    "Step",
    "Runbook",
    "RunbookValidationError",
]


# ---------------------------------------------------------------------------
# Domain errors
# ---------------------------------------------------------------------------


class RunbookValidationError(ValueError):
    """Raised by dataclass __post_init__ validators when construction fails.

    Provides clear, domain-shaped error messages for invalid runbook or
    step construction attempts.
    """

    pass


# ---------------------------------------------------------------------------
# Status enumeration
# ---------------------------------------------------------------------------


class StepStatus(StrEnum):
    """Closed status vocabulary for steps and runbooks.

    This is the single source of truth for the status value set. Per
    **ASSUM-001**, the runbook overall status uses the same value set,
    so the overall status reuses ``StepStatus`` (no separate enum).

    The migration's ``CHECK`` constraint (TASK-RSP-002) must enumerate
    exactly ``[s.value for s in StepStatus]``.
    """

    pending = "pending"
    running = "running"
    passed = "passed"
    failed = "failed"
    awaiting_approval = "awaiting_approval"


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StepResult:
    """Execution result for a completed step.

    Records the outcome of step execution: exit code, captured output,
    and timing information. A step has no result until one is recorded
    (**ASSUM-007**) — ``Step.result`` is ``StepResult | None``.

    Attributes:
        exit_code: Process exit code (0 for success).
        captured_output: Combined stdout/stderr from step execution.
        started_at: When the step began execution (**ASSUM-008**).
        completed_at: When the step finished execution (**ASSUM-008**).
    """

    exit_code: int
    captured_output: str
    started_at: datetime
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class Step:
    """One step in a runbook execution sequence.

    A frozen dataclass so callers can compare steps by value and pass
    them across thread boundaries without defensive copies.

    Attributes:
        step_type: Free-form step type identifier (**ASSUM-005** — non-empty).
        params: Step-specific parameters (JSON-serializable mapping).
        status: Current execution status.
        sequence_index: 0-based position in the runbook's step list.
        result: Execution result (None until recorded).
    """

    step_type: str
    params: Mapping[str, Any]
    status: StepStatus
    sequence_index: int
    result: StepResult | None = None

    def __post_init__(self) -> None:
        """Validate step_type is non-empty (ASSUM-005)."""
        if not self.step_type or not self.step_type.strip():
            raise RunbookValidationError(
                "step_type must be a non-empty string (ASSUM-005)"
            )


@dataclass(frozen=True, slots=True)
class Runbook:
    """A runbook: an ordered sequence of steps with execution state.

    A frozen dataclass so callers can compare runbooks by value and pass
    them across thread boundaries without defensive copies.

    Attributes:
        runbook_id: Unique identifier for this runbook.
        target: The target of this runbook execution.
        steps: Immutable tuple of steps (at least one, **ASSUM-002**).
        current_step_index: Index of the current/next step to execute.
        status: Overall runbook status (reuses StepStatus, **ASSUM-001**).
        created_at: When this runbook was created.
    """

    runbook_id: str
    target: str
    steps: tuple[Step, ...]
    current_step_index: int
    status: StepStatus
    created_at: datetime

    def __post_init__(self) -> None:
        """Validate runbook invariants (ASSUM-002, ASSUM-004)."""
        # ASSUM-002: At least one step
        if not self.steps:
            raise RunbookValidationError(
                "Runbook must have at least one step (ASSUM-002)"
            )

        # ASSUM-004 (R1, reconciled with FEAT-RBX): current_step_index points
        # to a valid step OR to the terminal position len(steps) (one past the
        # last step), which is the runbook's completion marker.
        if not (0 <= self.current_step_index <= len(self.steps)):
            raise RunbookValidationError(
                f"current_step_index must be in [0, {len(self.steps)}], "
                f"got {self.current_step_index} (ASSUM-004, R1)"
            )

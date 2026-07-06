"""Planning run state machine — transition table (TASK-MP-002).

This module is the **sole producer of** planning run state transitions.
The :class:`SqlitePlanningRunStore` uses :const:`PLANNING_TRANSITIONS`
to enforce valid state moves via CAS (compare-and-swap).

Mirrors the pattern from :mod:`forge.lifecycle.state_machine` for builds,
but with a planning-specific state graph:

- QUEUED: Initial state when planning request is received
- RUNNING: Planning chain is actively executing
- PAUSED: Awaiting human approval or escalation decision
- PLANNED_HANDOFF: Terminal success — plan delivered to Mode B
- FAILED: Terminal failure — planning could not complete
- CANCELLED: Terminal — user or system cancelled the request
- TIMED_OUT: Terminal — planning exceeded deadline

Terminal states (FAILED / CANCELLED / TIMED_OUT / PLANNED_HANDOFF) accept
no outgoing transitions. The CAS transition enforcement in
:class:`SqlitePlanningRunStore` ensures that once a run reaches a terminal
state, no further transitions are permitted.

References
----------
- TASK-MP-002 — this task brief
- TASK-MP-005 — approve-vs-escalation race consumer of CAS primitive
- FEAT-SPL-002 — parent feature (Mode P planning chain)
"""

from __future__ import annotations

from enum import Enum
from typing import Final


class PlanningState(str, Enum):
    """Planning run states (schema.sql CHECK constraint mirror)."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    PLANNED_HANDOFF = "PLANNED_HANDOFF"


# Allowed transitions: from_state → {to_state, ...}
PLANNING_TRANSITIONS: Final[dict[PlanningState, set[PlanningState]]] = {
    PlanningState.QUEUED: {
        PlanningState.RUNNING,
        PlanningState.CANCELLED,
    },
    PlanningState.RUNNING: {
        PlanningState.PAUSED,
        PlanningState.FAILED,
        PlanningState.TIMED_OUT,
        PlanningState.PLANNED_HANDOFF,
    },
    PlanningState.PAUSED: {
        PlanningState.RUNNING,
        PlanningState.CANCELLED,
        PlanningState.TIMED_OUT,
    },
    # Terminal states accept no transitions
    PlanningState.FAILED: set(),
    PlanningState.CANCELLED: set(),
    PlanningState.TIMED_OUT: set(),
    PlanningState.PLANNED_HANDOFF: set(),
}


__all__ = [
    "PlanningState",
    "PLANNING_TRANSITIONS",
]

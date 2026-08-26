"""Planning run state machine — transition table (TASK-MP-002).

This module is the **sole producer of** planning run state transitions.
The :class:`SqlitePlanningRunStore` uses the transition table returned by
:func:`planning_transitions_for` to enforce valid state moves via CAS
(compare-and-swap).

Mirrors the pattern from :mod:`forge.lifecycle.state_machine` for builds,
but with a planning-specific state graph:

- QUEUED: Initial state when planning request is received
- RUNNING: Planning chain is actively executing
- PAUSED: Awaiting human approval or escalation decision
- PLANNED_HANDOFF: Terminal success — plan delivered to Mode B
- FAILED: Terminal failure — planning could not complete
- CANCELLED: Terminal — user or system cancelled the request
- TIMED_OUT: Terminal — planning exceeded deadline

Target terminal (Lane B / Phase E1 — additive, default-OFF)
-----------------------------------------------------------
When the ``planning.target_terminal.enabled`` flag is on, the planning
chain no longer terminates at PLANNED_HANDOFF. Instead it continues into
the machine chain that automates the Factory-2 coordinator sequence:

    RUNNING → FEATURE_SPEC → FEATURE_PLAN → BUILD_QUEUED (terminal)

- FEATURE_SPEC: dispatching the spec leg (007) + committing the triple
- FEATURE_PLAN: dispatching the plan leg (008) + committing the plan tree
- BUILD_QUEUED: terminal success — the feature has been queued onto the
  Mode B autobuild dispatcher for the pre-dispatch approval gate

This is strictly **additive**: :const:`PLANNING_TRANSITIONS` (the flag-OFF
table) is byte-for-byte unchanged, PLANNED_HANDOFF remains a reachable
terminal even with the flag on (it is the shipped fallback forever), and
the flag-ON table :const:`PLANNING_TRANSITIONS_TARGET_TERMINAL` only *adds*
the RUNNING → FEATURE_SPEC edge plus the three new states (and, 2026-08-26,
the FEATURE_SPEC → CANCELLED edge — the owner calling the run off at the
spec-review card). The store
selects between the two tables via :func:`planning_transitions_for`, so a
store built with the flag off behaves exactly as it did before this change.

Terminal states (FAILED / CANCELLED / TIMED_OUT / PLANNED_HANDOFF, plus
BUILD_QUEUED when the target terminal is enabled) accept no outgoing
transitions. The CAS transition enforcement in
:class:`SqlitePlanningRunStore` ensures that once a run reaches a terminal
state, no further transitions are permitted.

References
----------
- TASK-MP-002 — this task brief
- TASK-MP-005 — approve-vs-escalation race consumer of CAS primitive
- FEAT-SPL-002 — parent feature (Mode P planning chain)
- Lane B / Phase E1 — the forge target terminal (the machine chain after
  PLANNED_HANDOFF); ``post-factory-2-three-lanes-handoff.md`` §3 B1
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
    # Target-terminal chain (Lane B / Phase E1) — only reachable when the
    # planning.target_terminal.enabled flag is on. See module docstring.
    FEATURE_SPEC = "FEATURE_SPEC"
    FEATURE_PLAN = "FEATURE_PLAN"
    BUILD_QUEUED = "BUILD_QUEUED"


# Allowed transitions: from_state → {to_state, ...}
#
# This is the **flag-OFF** table — the shipped behaviour. It MUST remain
# byte-for-byte identical: PLANNED_HANDOFF is the terminal success state and
# the new FEATURE_SPEC / FEATURE_PLAN / BUILD_QUEUED states are unreachable.
# (A regression test asserts this table is unchanged; do not edit it to add
# target-terminal edges — those live in the derived flag-ON table below.)
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


# The **flag-ON** table — the target-terminal machine chain (Lane B).
#
# Derived from :const:`PLANNING_TRANSITIONS` so the additive property is
# structural, not hand-maintained: every flag-OFF edge is preserved verbatim,
# RUNNING gains one edge (→ FEATURE_SPEC), and the three new states are added.
# PLANNED_HANDOFF stays a reachable terminal (the shipped fallback forever).
PLANNING_TRANSITIONS_TARGET_TERMINAL: Final[
    dict[PlanningState, set[PlanningState]]
] = {
    **{from_state: set(to_states) for from_state, to_states in PLANNING_TRANSITIONS.items()},
    # RUNNING may still reach PLANNED_HANDOFF (fallback); the target terminal
    # ADDS the entry into the spec/plan/build chain.
    PlanningState.RUNNING: PLANNING_TRANSITIONS[PlanningState.RUNNING]
    | {PlanningState.FEATURE_SPEC},
    PlanningState.FEATURE_SPEC: {
        PlanningState.FEATURE_PLAN,
        PlanningState.FAILED,
        PlanningState.TIMED_OUT,
        # 2026-08-26: the owner can call the run off at the spec-review card
        # (a typed reply whose first word is "reject"). That is their own
        # stop, so it ends CANCELLED — distinct from a machine FAILED.
        PlanningState.CANCELLED,
    },
    PlanningState.FEATURE_PLAN: {
        PlanningState.BUILD_QUEUED,
        PlanningState.FAILED,
        PlanningState.TIMED_OUT,
    },
    # BUILD_QUEUED is the target terminal — the feature is now on the Mode B
    # dispatcher's pre-dispatch approval gate; planning's job is done.
    PlanningState.BUILD_QUEUED: set(),
}


def planning_transitions_for(
    target_terminal_enabled: bool,
) -> dict[PlanningState, set[PlanningState]]:
    """Return the transition table for the given target-terminal flag.

    Parameters
    ----------
    target_terminal_enabled:
        When ``False`` (default posture), returns :const:`PLANNING_TRANSITIONS`
        — the shipped flag-OFF table, byte-for-byte unchanged. When ``True``,
        returns :const:`PLANNING_TRANSITIONS_TARGET_TERMINAL` — the additive
        flag-ON table with the FEATURE_SPEC → FEATURE_PLAN → BUILD_QUEUED chain.

    Returns
    -------
    dict[PlanningState, set[PlanningState]]:
        The transition table to enforce. Callers must treat it as read-only.
    """
    if target_terminal_enabled:
        return PLANNING_TRANSITIONS_TARGET_TERMINAL
    return PLANNING_TRANSITIONS


__all__ = [
    "PlanningState",
    "PLANNING_TRANSITIONS",
    "PLANNING_TRANSITIONS_TARGET_TERMINAL",
    "planning_transitions_for",
]

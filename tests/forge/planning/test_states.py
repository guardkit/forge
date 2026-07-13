"""Tests for planning run state machine (TASK-MP-002 + Lane B / Phase E1)."""

from __future__ import annotations

from forge.planning.states import (
    PlanningState,
    PLANNING_TRANSITIONS,
    PLANNING_TRANSITIONS_TARGET_TERMINAL,
    planning_transitions_for,
)


def test_planning_state_enum_has_all_required_states() -> None:
    """Planning state enum includes all states from the schema CHECK constraint.

    The base Mode P states plus the Lane B target-terminal chain states
    (FEATURE_SPEC / FEATURE_PLAN / BUILD_QUEUED). This set must match the
    widened ``planning_runs.state`` CHECK constraint in schema_v4.sql.
    """
    expected_states = {
        "QUEUED",
        "RUNNING",
        "PAUSED",
        "FAILED",
        "CANCELLED",
        "TIMED_OUT",
        "PLANNED_HANDOFF",
        "FEATURE_SPEC",
        "FEATURE_PLAN",
        "BUILD_QUEUED",
    }
    actual_states = {state.value for state in PlanningState}
    assert actual_states == expected_states


def test_planning_transitions_defines_valid_moves() -> None:
    """PLANNING_TRANSITIONS maps from-state to a set of allowed to-states."""
    # Check that the transition map exists and has the expected shape
    assert isinstance(PLANNING_TRANSITIONS, dict)

    # Every key should be a PlanningState
    for from_state in PLANNING_TRANSITIONS:
        assert isinstance(from_state, PlanningState)

    # Every value should be a set of PlanningState
    for allowed_next_states in PLANNING_TRANSITIONS.values():
        assert isinstance(allowed_next_states, set)
        for next_state in allowed_next_states:
            assert isinstance(next_state, PlanningState)


def test_terminal_states_accept_no_transitions() -> None:
    """Terminal states (FAILED, CANCELLED, TIMED_OUT, PLANNED_HANDOFF) have empty transition sets."""
    terminal_states = {
        PlanningState.FAILED,
        PlanningState.CANCELLED,
        PlanningState.TIMED_OUT,
        PlanningState.PLANNED_HANDOFF,
    }

    for terminal_state in terminal_states:
        assert (
            PLANNING_TRANSITIONS.get(terminal_state, set()) == set()
        ), f"{terminal_state.value} is terminal and should accept no transitions"


def test_queued_can_transition_to_running_or_cancelled() -> None:
    """QUEUED → RUNNING or CANCELLED."""
    allowed = PLANNING_TRANSITIONS[PlanningState.QUEUED]
    assert PlanningState.RUNNING in allowed
    assert PlanningState.CANCELLED in allowed


def test_running_can_transition_to_paused_failed_or_planned_handoff() -> None:
    """RUNNING → PAUSED, FAILED, TIMED_OUT, or PLANNED_HANDOFF."""
    allowed = PLANNING_TRANSITIONS[PlanningState.RUNNING]
    assert PlanningState.PAUSED in allowed
    assert PlanningState.FAILED in allowed
    assert PlanningState.TIMED_OUT in allowed
    assert PlanningState.PLANNED_HANDOFF in allowed


def test_paused_can_transition_to_running_cancelled_or_timed_out() -> None:
    """PAUSED → RUNNING, CANCELLED, or TIMED_OUT."""
    allowed = PLANNING_TRANSITIONS[PlanningState.PAUSED]
    assert PlanningState.RUNNING in allowed
    assert PlanningState.CANCELLED in allowed
    assert PlanningState.TIMED_OUT in allowed


# ---------------------------------------------------------------------------
# Lane B / Phase E1 — the target terminal (default-OFF flag, additive chain).
# The flag-OFF table MUST stay byte-for-byte identical to the shipped table;
# the flag-ON table only ADDS transitions and never removes PLANNED_HANDOFF as
# a reachable fallback terminal.
# ---------------------------------------------------------------------------


# The shipped flag-OFF table, frozen as a literal so a regression here forces a
# conscious decision. If this ever needs to change, the change is NOT additive
# and violates the Lane B no-op guarantee (post-factory-2-three-lanes §2.12).
_FROZEN_FLAG_OFF_TABLE: dict[PlanningState, set[PlanningState]] = {
    PlanningState.QUEUED: {PlanningState.RUNNING, PlanningState.CANCELLED},
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
    PlanningState.FAILED: set(),
    PlanningState.CANCELLED: set(),
    PlanningState.TIMED_OUT: set(),
    PlanningState.PLANNED_HANDOFF: set(),
}


def test_flag_off_table_is_byte_for_byte_the_shipped_table() -> None:
    """The flag-OFF table equals the frozen shipped table (byte-no-op proof).

    This is the coach's "state table diff" gate: with the target terminal
    disabled, the planning FSM is EXACTLY what it was before Lane B.
    """
    assert PLANNING_TRANSITIONS == _FROZEN_FLAG_OFF_TABLE


def test_resolver_selects_flag_off_table_by_default() -> None:
    """planning_transitions_for(False) returns the shipped flag-OFF table."""
    assert planning_transitions_for(False) is PLANNING_TRANSITIONS


def test_resolver_selects_target_terminal_table_when_enabled() -> None:
    """planning_transitions_for(True) returns the additive flag-ON table."""
    assert (
        planning_transitions_for(True) is PLANNING_TRANSITIONS_TARGET_TERMINAL
    )


def test_target_terminal_table_is_a_strict_superset_of_flag_off() -> None:
    """Every flag-OFF edge is preserved verbatim in the flag-ON table (additive)."""
    for from_state, to_states in PLANNING_TRANSITIONS.items():
        assert to_states <= PLANNING_TRANSITIONS_TARGET_TERMINAL[from_state], (
            f"flag-ON table dropped edges from {from_state.value}"
        )


def test_target_terminal_only_adds_feature_spec_edge_off_running() -> None:
    """The ONLY new edge from a pre-existing state is RUNNING -> FEATURE_SPEC."""
    for from_state in PLANNING_TRANSITIONS:
        added = (
            PLANNING_TRANSITIONS_TARGET_TERMINAL[from_state]
            - PLANNING_TRANSITIONS[from_state]
        )
        if from_state is PlanningState.RUNNING:
            assert added == {PlanningState.FEATURE_SPEC}
        else:
            assert added == set(), (
                f"unexpected new edge added off {from_state.value}: {added}"
            )


def test_planned_handoff_stays_a_reachable_fallback_when_flag_on() -> None:
    """Flag ON never removes PLANNED_HANDOFF as a reachable terminal (§2.12)."""
    assert (
        PlanningState.PLANNED_HANDOFF
        in PLANNING_TRANSITIONS_TARGET_TERMINAL[PlanningState.RUNNING]
    )
    assert (
        PLANNING_TRANSITIONS_TARGET_TERMINAL[PlanningState.PLANNED_HANDOFF]
        == set()
    )


def test_target_terminal_chain_edges() -> None:
    """The additive chain: FEATURE_SPEC -> FEATURE_PLAN -> BUILD_QUEUED."""
    table = PLANNING_TRANSITIONS_TARGET_TERMINAL
    assert PlanningState.FEATURE_PLAN in table[PlanningState.FEATURE_SPEC]
    assert PlanningState.BUILD_QUEUED in table[PlanningState.FEATURE_PLAN]
    # Each intermediate state can also fail or time out (loud terminal states).
    assert PlanningState.FAILED in table[PlanningState.FEATURE_SPEC]
    assert PlanningState.TIMED_OUT in table[PlanningState.FEATURE_SPEC]
    assert PlanningState.FAILED in table[PlanningState.FEATURE_PLAN]
    assert PlanningState.TIMED_OUT in table[PlanningState.FEATURE_PLAN]


def test_build_queued_is_the_new_terminal() -> None:
    """BUILD_QUEUED accepts no outgoing transitions (target terminal)."""
    assert (
        PLANNING_TRANSITIONS_TARGET_TERMINAL[PlanningState.BUILD_QUEUED] == set()
    )


def test_flag_off_table_cannot_reach_target_terminal_states() -> None:
    """With the flag off, none of the new chain states are reachable."""
    reachable: set[PlanningState] = set()
    for to_states in PLANNING_TRANSITIONS.values():
        reachable |= to_states
    assert PlanningState.FEATURE_SPEC not in reachable
    assert PlanningState.FEATURE_PLAN not in reachable
    assert PlanningState.BUILD_QUEUED not in reachable

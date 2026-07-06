"""Tests for planning run state machine (TASK-MP-002)."""

from __future__ import annotations

from forge.planning.states import PlanningState, PLANNING_TRANSITIONS


def test_planning_state_enum_has_all_required_states() -> None:
    """Planning state enum includes all states from the schema CHECK constraint."""
    expected_states = {
        "QUEUED",
        "RUNNING",
        "PAUSED",
        "FAILED",
        "CANCELLED",
        "TIMED_OUT",
        "PLANNED_HANDOFF",
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

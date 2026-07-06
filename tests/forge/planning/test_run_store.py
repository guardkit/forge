"""Tests for SqlitePlanningRunStore (TASK-MP-002)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations
from forge.planning.run_store import (
    SqlitePlanningRunStore,
    DuplicateRun,
    TransitionRefused,
)
from forge.planning.states import PlanningState


@pytest.fixture
def db_connection(tmp_path: Path) -> sqlite3.Connection:
    """Fresh SQLite DB with v3 schema applied."""
    db_path = tmp_path / "test.db"
    cx = sqlite_connect.connect_writer(db_path)
    migrations.apply_at_boot(cx)
    return cx


@pytest.fixture
def store(db_connection: sqlite3.Connection) -> SqlitePlanningRunStore:
    """Store instance backed by the test database."""
    return SqlitePlanningRunStore(db_connection)


def test_record_queued_creates_new_planning_run(store: SqlitePlanningRunStore) -> None:
    """AC-002: record_queued creates a new row on first call."""
    correlation_id = "test-correlation-001"
    result = store.record_queued(
        correlation_id=correlation_id,
        originating_user="alice",
        expected_approver="bob",
        request_text="Create feature X",
        triggered_by="cli",
        originating_adapter=None,
        parent_request_id=None,
    )

    assert result is None  # Success case returns None

    # Verify row was created
    row = store._get_run(correlation_id)
    assert row is not None
    assert row["correlation_id"] == correlation_id
    assert row["state"] == PlanningState.QUEUED.value
    assert row["originating_user"] == "alice"
    assert row["expected_approver"] == "bob"
    assert row["request_text"] == "Create feature X"
    assert row["triggered_by"] == "cli"
    assert row["queued_at"] is not None


def test_record_queued_is_idempotent_on_correlation_id(
    store: SqlitePlanningRunStore,
) -> None:
    """AC-002: Second call with same correlation_id returns DuplicateRun sentinel."""
    correlation_id = "test-correlation-002"

    # First call succeeds
    result1 = store.record_queued(
        correlation_id=correlation_id,
        originating_user="alice",
        expected_approver="bob",
        request_text="Create feature X",
        triggered_by="cli",
    )
    assert result1 is None

    # Second call returns DuplicateRun
    result2 = store.record_queued(
        correlation_id=correlation_id,
        originating_user="alice",
        expected_approver="bob",
        request_text="Create feature X",
        triggered_by="cli",
    )
    assert isinstance(result2, DuplicateRun)
    assert result2.existing_state == PlanningState.QUEUED.value
    assert result2.is_terminal is False


def test_duplicate_run_distinguishes_terminal_from_non_terminal(
    store: SqlitePlanningRunStore,
) -> None:
    """AC-002: DuplicateRun sentinel indicates if existing state is terminal."""
    correlation_id = "test-correlation-003"

    # Create and transition to terminal state
    store.record_queued(
        correlation_id=correlation_id,
        originating_user="alice",
        expected_approver="bob",
        request_text="Create feature X",
        triggered_by="cli",
    )
    store.transition(
        correlation_id=correlation_id,
        to_state=PlanningState.RUNNING,
        actor_identity="system",
    )
    store.transition(
        correlation_id=correlation_id,
        to_state=PlanningState.PLANNED_HANDOFF,
        actor_identity="system",
        handoff_branch="feat-x",
        handoff_path="/path/to/plan.md",
    )

    # Try to record_queued again
    result = store.record_queued(
        correlation_id=correlation_id,
        originating_user="alice",
        expected_approver="bob",
        request_text="Create feature X",
        triggered_by="cli",
    )
    assert isinstance(result, DuplicateRun)
    assert result.existing_state == PlanningState.PLANNED_HANDOFF.value
    assert result.is_terminal is True


def test_transition_writes_planning_run_events_row(
    store: SqlitePlanningRunStore, db_connection: sqlite3.Connection
) -> None:
    """AC-003: Every state transition writes a planning_run_events row."""
    correlation_id = "test-correlation-004"

    store.record_queued(
        correlation_id=correlation_id,
        originating_user="alice",
        expected_approver="bob",
        request_text="Create feature X",
        triggered_by="cli",
    )

    # Transition to RUNNING
    store.transition(
        correlation_id=correlation_id,
        to_state=PlanningState.RUNNING,
        actor_identity="system",
        stage_label="planning-start",
        details_json='{"model": "claude-3-opus"}',
    )

    # Check that event was recorded
    events = db_connection.execute(
        """
        SELECT correlation_id, stage_label, status, actor_identity, details_json
        FROM planning_run_events
        WHERE correlation_id = ?
        ORDER BY recorded_at
        """,
        (correlation_id,),
    ).fetchall()

    assert len(events) >= 1
    latest_event = events[-1]
    assert latest_event[0] == correlation_id
    assert latest_event[1] == "planning-start"
    assert latest_event[2] == PlanningState.RUNNING.value
    assert latest_event[3] == "system"
    assert latest_event[4] == '{"model": "claude-3-opus"}'


def test_transition_enforces_valid_moves_via_cas(
    store: SqlitePlanningRunStore,
) -> None:
    """AC-004: Transitions enforce PLANNING_TRANSITIONS via CAS."""
    correlation_id = "test-correlation-005"

    store.record_queued(
        correlation_id=correlation_id,
        originating_user="alice",
        expected_approver="bob",
        request_text="Create feature X",
        triggered_by="cli",
    )

    # Valid transition: QUEUED → RUNNING
    result = store.transition(
        correlation_id=correlation_id,
        to_state=PlanningState.RUNNING,
        actor_identity="system",
    )
    assert result is None  # Success

    # Invalid transition: RUNNING → QUEUED (not in PLANNING_TRANSITIONS)
    result = store.transition(
        correlation_id=correlation_id,
        to_state=PlanningState.QUEUED,
        actor_identity="system",
    )
    assert isinstance(result, TransitionRefused)
    assert result.current_state == PlanningState.RUNNING.value
    assert result.requested_state == PlanningState.QUEUED.value


def test_terminal_states_refuse_all_transitions(
    store: SqlitePlanningRunStore,
) -> None:
    """AC-004: Terminal states accept no transitions."""
    correlation_id = "test-correlation-006"

    store.record_queued(
        correlation_id=correlation_id,
        originating_user="alice",
        expected_approver="bob",
        request_text="Create feature X",
        triggered_by="cli",
    )
    store.transition(
        correlation_id=correlation_id,
        to_state=PlanningState.RUNNING,
        actor_identity="system",
    )
    store.transition(
        correlation_id=correlation_id,
        to_state=PlanningState.FAILED,
        actor_identity="system",
        error="Planning timeout",
    )

    # Try to transition from terminal FAILED state
    result = store.transition(
        correlation_id=correlation_id,
        to_state=PlanningState.RUNNING,
        actor_identity="system",
    )
    assert isinstance(result, TransitionRefused)


def test_cas_transition_affected_rows_discipline(
    store: SqlitePlanningRunStore, db_connection: sqlite3.Connection
) -> None:
    """AC-004: CAS uses affected-rows check (affected==1 wins, ==0 refuses)."""
    correlation_id = "test-correlation-007"

    store.record_queued(
        correlation_id=correlation_id,
        originating_user="alice",
        expected_approver="bob",
        request_text="Create feature X",
        triggered_by="cli",
    )

    # Manually set state to RUNNING to test CAS discipline
    db_connection.execute(
        "UPDATE planning_runs SET state = ? WHERE correlation_id = ?",
        (PlanningState.RUNNING.value, correlation_id),
    )
    db_connection.commit()

    # Try transition from QUEUED → RUNNING (but current state is actually RUNNING)
    # This should fail because the CAS WHERE clause won't match
    result = store.transition(
        correlation_id=correlation_id,
        to_state=PlanningState.RUNNING,
        actor_identity="system",
        expected_from_state=PlanningState.QUEUED,
    )
    assert isinstance(result, TransitionRefused)


def test_durability_across_store_instances(tmp_path: Path) -> None:
    """AC-005: Rows written by one instance are read by another."""
    db_path = tmp_path / "durable.db"

    # First instance: write
    cx1 = sqlite_connect.connect_writer(db_path)
    migrations.apply_at_boot(cx1)
    store1 = SqlitePlanningRunStore(cx1)

    correlation_id = "test-correlation-008"
    store1.record_queued(
        correlation_id=correlation_id,
        originating_user="alice",
        expected_approver="bob",
        request_text="Create feature X",
        triggered_by="cli",
    )
    cx1.close()

    # Second instance: read
    cx2 = sqlite_connect.connect_writer(db_path)
    store2 = SqlitePlanningRunStore(cx2)

    row = store2._get_run(correlation_id)
    assert row is not None
    assert row["originating_user"] == "alice"
    assert row["expected_approver"] == "bob"
    cx2.close()


def test_updatable_columns_persist(store: SqlitePlanningRunStore) -> None:
    """AC-006: defer_count, paused_at, escalated_at, expected_approver are updatable."""
    correlation_id = "test-correlation-009"

    store.record_queued(
        correlation_id=correlation_id,
        originating_user="alice",
        expected_approver="bob",
        request_text="Create feature X",
        triggered_by="cli",
    )

    # Update escalation fields
    now = datetime.now(timezone.utc).isoformat()
    store.update_escalation(
        correlation_id=correlation_id,
        defer_count=3,
        paused_at=now,
        escalated_at=now,
        expected_approver="charlie",
    )

    row = store._get_run(correlation_id)
    assert row["defer_count"] == 3
    assert row["paused_at"] == now
    assert row["escalated_at"] == now
    assert row["expected_approver"] == "charlie"


def test_cas_race_only_one_transition_wins(
    tmp_path: Path, db_connection: sqlite3.Connection
) -> None:
    """AC-004 race scenario: Two competing transitions, exactly one winner."""
    correlation_id = "test-correlation-010"

    store = SqlitePlanningRunStore(db_connection)
    store.record_queued(
        correlation_id=correlation_id,
        originating_user="alice",
        expected_approver="bob",
        request_text="Create feature X",
        triggered_by="cli",
    )

    # Simulate race: both try QUEUED → RUNNING
    # First one should succeed, second should fail
    result1 = store.transition(
        correlation_id=correlation_id,
        to_state=PlanningState.RUNNING,
        actor_identity="worker-1",
        expected_from_state=PlanningState.QUEUED,
    )

    result2 = store.transition(
        correlation_id=correlation_id,
        to_state=PlanningState.RUNNING,
        actor_identity="worker-2",
        expected_from_state=PlanningState.QUEUED,
    )

    # One succeeds, one fails
    assert (result1 is None and isinstance(result2, TransitionRefused)) or (
        result2 is None and isinstance(result1, TransitionRefused)
    )

    # Final state should be RUNNING
    row = store._get_run(correlation_id)
    assert row["state"] == PlanningState.RUNNING.value

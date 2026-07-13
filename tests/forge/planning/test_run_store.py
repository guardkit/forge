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


# ---------------------------------------------------------------------------
# Lane B / Phase E1 — target-terminal transition enforcement in the store.
# The store selects its transition table from the target_terminal_enabled flag
# at construction. Flag OFF = shipped behaviour; flag ON = the additive chain.
# ---------------------------------------------------------------------------


def _queue_and_run(store: SqlitePlanningRunStore, cid: str) -> None:
    """Drive a fresh run to RUNNING (the common precondition)."""
    assert (
        store.record_queued(
            correlation_id=cid,
            originating_user="alice",
            expected_approver="bob",
            request_text="feature",
            triggered_by="cli",
        )
        is None
    )
    assert (
        store.transition(cid, PlanningState.RUNNING, actor_identity="worker") is None
    )


def test_flag_off_store_refuses_running_to_feature_spec(
    db_connection: sqlite3.Connection,
) -> None:
    """With the flag off, RUNNING -> FEATURE_SPEC is refused (shipped behaviour)."""
    store = SqlitePlanningRunStore(db_connection, target_terminal_enabled=False)
    _queue_and_run(store, "off-1")

    refused = store.transition(
        "off-1", PlanningState.FEATURE_SPEC, actor_identity="worker"
    )
    assert isinstance(refused, TransitionRefused)
    assert refused.current_state == PlanningState.RUNNING.value
    assert refused.requested_state == PlanningState.FEATURE_SPEC.value


def test_default_store_is_flag_off(db_connection: sqlite3.Connection) -> None:
    """The store defaults to the flag-OFF table (byte-no-op posture)."""
    store = SqlitePlanningRunStore(db_connection)  # no flag passed
    _queue_and_run(store, "def-1")
    refused = store.transition(
        "def-1", PlanningState.FEATURE_SPEC, actor_identity="worker"
    )
    assert isinstance(refused, TransitionRefused)


def test_flag_on_store_drives_full_target_terminal_chain(
    db_connection: sqlite3.Connection,
) -> None:
    """Flag ON: RUNNING -> FEATURE_SPEC -> FEATURE_PLAN -> BUILD_QUEUED."""
    store = SqlitePlanningRunStore(db_connection, target_terminal_enabled=True)
    _queue_and_run(store, "on-1")

    assert (
        store.transition("on-1", PlanningState.FEATURE_SPEC, actor_identity="w")
        is None
    )
    assert (
        store.transition("on-1", PlanningState.FEATURE_PLAN, actor_identity="w")
        is None
    )
    assert (
        store.transition("on-1", PlanningState.BUILD_QUEUED, actor_identity="w")
        is None
    )

    row = store.get_run("on-1")
    assert row is not None
    assert row["state"] == PlanningState.BUILD_QUEUED.value


def test_build_queued_stamps_completed_at_and_is_terminal(
    db_connection: sqlite3.Connection,
) -> None:
    """BUILD_QUEUED is terminal: completed_at is set and no further move is allowed."""
    store = SqlitePlanningRunStore(db_connection, target_terminal_enabled=True)
    _queue_and_run(store, "on-2")
    store.transition("on-2", PlanningState.FEATURE_SPEC, actor_identity="w")
    store.transition("on-2", PlanningState.FEATURE_PLAN, actor_identity="w")
    store.transition("on-2", PlanningState.BUILD_QUEUED, actor_identity="w")

    row = store.get_run("on-2")
    assert row is not None
    assert row["completed_at"] is not None

    # Terminal — a follow-on transition is refused.
    refused = store.transition(
        "on-2", PlanningState.FEATURE_SPEC, actor_identity="w"
    )
    assert isinstance(refused, TransitionRefused)


def test_flag_on_store_still_allows_planned_handoff_fallback(
    db_connection: sqlite3.Connection,
) -> None:
    """Flag ON never removes PLANNED_HANDOFF as a reachable terminal (§2.12)."""
    store = SqlitePlanningRunStore(db_connection, target_terminal_enabled=True)
    _queue_and_run(store, "on-3")

    assert (
        store.transition(
            "on-3", PlanningState.PLANNED_HANDOFF, actor_identity="w"
        )
        is None
    )
    row = store.get_run("on-3")
    assert row is not None
    assert row["state"] == PlanningState.PLANNED_HANDOFF.value
    assert row["completed_at"] is not None


def test_duplicate_run_reports_build_queued_as_terminal(
    db_connection: sqlite3.Connection,
) -> None:
    """A run resting at BUILD_QUEUED is reported terminal by the duplicate sentinel."""
    store = SqlitePlanningRunStore(db_connection, target_terminal_enabled=True)
    _queue_and_run(store, "on-4")
    store.transition("on-4", PlanningState.FEATURE_SPEC, actor_identity="w")
    store.transition("on-4", PlanningState.FEATURE_PLAN, actor_identity="w")
    store.transition("on-4", PlanningState.BUILD_QUEUED, actor_identity="w")

    dup = store.record_queued(
        correlation_id="on-4",
        originating_user="alice",
        expected_approver="bob",
        request_text="feature",
        triggered_by="cli",
    )
    assert isinstance(dup, DuplicateRun)
    assert dup.existing_state == PlanningState.BUILD_QUEUED.value
    assert dup.is_terminal is True

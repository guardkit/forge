"""Tests for planning run gate adapters (TASK-MP-004A).

Mirrors the structure from tests/forge/gating/ test suites.
Validates that PlanningGateRepository and PlanningStateMachine satisfy
the gate Protocol contracts over SqlitePlanningRunStore.
"""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from forge.gating.identity import derive_request_id
from forge.gating.models import GateDecision, GateMode
from forge.gating.wrappers import PausedBuildSnapshot
from forge.planning.gate_adapters import (
    PlanningGateRepository,
    PlanningStateMachine,
    build_planning_gate_adapters,
)
from forge.planning.run_store import SqlitePlanningRunStore, TransitionRefused
from forge.planning.states import PlanningState


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    """Create a temporary SQLite database with planning schema."""
    db_path = tmp_path / "test_planning.db"
    conn = sqlite3.connect(db_path)

    # Create planning_runs table with required fields
    conn.execute("""
        CREATE TABLE planning_runs (
            correlation_id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            originating_user TEXT NOT NULL,
            expected_approver TEXT NOT NULL,
            request_text TEXT NOT NULL,
            target_repo TEXT,
            triggered_by TEXT NOT NULL,
            originating_adapter TEXT,
            parent_request_id TEXT,
            queued_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            paused_at TEXT,
            escalated_at TEXT,
            defer_count INTEGER DEFAULT 0,
            pending_approval_request_id TEXT,
            handoff_branch TEXT,
            handoff_path TEXT,
            error TEXT
        )
    """)

    # Create planning_run_events table
    conn.execute("""
        CREATE TABLE planning_run_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            correlation_id TEXT NOT NULL,
            stage_label TEXT NOT NULL,
            status TEXT NOT NULL,
            gate_mode TEXT,
            coach_score REAL,
            actor_identity TEXT,
            details_json TEXT,
            recorded_at TEXT NOT NULL,
            FOREIGN KEY (correlation_id) REFERENCES planning_runs(correlation_id)
        )
    """)

    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def store(tmp_db: Path) -> SqlitePlanningRunStore:
    """Create a SqlitePlanningRunStore for testing."""
    conn = sqlite3.connect(tmp_db)
    return SqlitePlanningRunStore(conn)


@pytest.fixture
def clock() -> Mock:
    """Create a mock clock that returns a fixed datetime."""
    fixed_time = datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)
    return Mock(return_value=fixed_time)


@pytest.fixture
def adapters(store: SqlitePlanningRunStore, clock: Mock) -> tuple:
    """Create a pair of adapters with shared handoff."""
    return build_planning_gate_adapters(store, clock=clock)


@pytest.fixture
def repository(adapters: tuple) -> PlanningGateRepository:
    """Extract repository from adapters pair."""
    return adapters[0]


@pytest.fixture
def state_machine(adapters: tuple) -> PlanningStateMachine:
    """Extract state machine from adapters pair."""
    return adapters[1]


class TestProtocolSatisfaction:
    """AC-001: Protocol satisfaction tests."""

    @pytest.mark.asyncio
    async def test_repository_satisfies_gate_repository_protocol(
        self, repository: PlanningGateRepository
    ) -> None:
        """Repository structurally satisfies GateRepository protocol."""
        # Protocol methods must exist and be callable
        assert hasattr(repository, "record_decision")
        assert hasattr(repository, "write_to_graphiti")
        assert hasattr(repository, "record_paused_build")
        assert hasattr(repository, "list_paused_builds")
        assert hasattr(repository, "mark_resumed")
        assert hasattr(repository, "mark_overridden")
        assert hasattr(repository, "mark_cancelled")

    @pytest.mark.asyncio
    async def test_state_machine_satisfies_protocol(
        self, state_machine: PlanningStateMachine
    ) -> None:
        """State machine structurally satisfies StateMachine protocol."""
        assert hasattr(state_machine, "transition_to_paused")
        assert hasattr(state_machine, "transition_to_running")
        assert hasattr(state_machine, "transition_to_failed")
        assert hasattr(state_machine, "transition_to_cancelled")


class TestRecordPausedBuild:
    """AC-002: record_paused_* stores pending_approval_request_id and stamps paused_at."""

    @pytest.mark.asyncio
    async def test_record_paused_build_stores_request_id_and_stamps_paused_at(
        self, repository: PlanningGateRepository, store: SqlitePlanningRunStore, clock: Mock
    ) -> None:
        """record_paused_build stores pending_approval_request_id and paused_at."""
        # Arrange: Create a planning run in RUNNING state
        correlation_id = "test-corr-123"
        store.record_queued(
            correlation_id=correlation_id,
            originating_user="test@example.com",
            expected_approver="approver@example.com",
            request_text="Test request",
            triggered_by="cli",
        )
        store.transition(
            correlation_id=correlation_id,
            to_state=PlanningState.RUNNING,
            actor_identity="system",
        )

        decision = GateDecision(
            build_id=f"plan-{correlation_id}",
            stage_label="product_docs",
            target_kind="local_tool",
            target_identifier="product_docs_tool",
            mode=GateMode.AUTO_APPROVE,
            rationale="Test decision",
            coach_score=0.95,
            threshold_applied=0.80,
            decided_at=clock(),
        )

        # Act: Record paused build
        request_id = derive_request_id(
            build_id=f"plan-{correlation_id}",
            stage_label="product_docs",
            attempt_count=0,
        )
        await repository.record_paused_build(
            build_id=f"plan-{correlation_id}",
            feature_id="FEAT-TEST",
            stage_label="product_docs",
            request_id=request_id,
            attempt_count=0,
            decision=decision,
        )

        # Assert: Check pending_approval_request_id and paused_at are set
        conn = store._connection
        row = conn.execute(
            "SELECT pending_approval_request_id, paused_at FROM planning_runs WHERE correlation_id = ?",
            (correlation_id,),
        ).fetchone()
        assert row is not None
        assert row[0] == request_id
        assert row[1] == clock().isoformat()

    @pytest.mark.asyncio
    async def test_gate_decisions_write_events_with_metadata(
        self, repository: PlanningGateRepository, store: SqlitePlanningRunStore, clock: Mock
    ) -> None:
        """Gate decisions write planning_run_events rows with gate metadata."""
        # Arrange
        correlation_id = "test-corr-456"
        store.record_queued(
            correlation_id=correlation_id,
            originating_user="test@example.com",
            expected_approver="approver@example.com",
            request_text="Test request",
            triggered_by="cli",
        )
        store.transition(
            correlation_id=correlation_id,
            to_state=PlanningState.RUNNING,
            actor_identity="system",
        )

        decision = GateDecision(
            build_id=f"plan-{correlation_id}",
            stage_label="product_docs",
            target_kind="local_tool",
            target_identifier="product_docs_tool",
            mode=GateMode.HARD_STOP,
            rationale="Needs review",
            coach_score=0.65,
            threshold_applied=0.80,
            decided_at=clock(),
        )

        # Act: Record decision
        await repository.record_decision(decision)

        # Assert: Event row contains gate metadata
        conn = store._connection
        events = conn.execute(
            "SELECT gate_mode, coach_score, details_json FROM planning_run_events WHERE correlation_id = ?",
            (correlation_id,),
        ).fetchall()
        assert len(events) >= 1
        # Last event should have gate metadata
        last_event = events[-1]
        assert last_event[0] == "HARD_STOP"
        assert last_event[1] == 65.0


class TestListPausedBuilds:
    """AC-003: list_paused_runs() reconstructs paused-run snapshots."""

    @pytest.mark.asyncio
    async def test_list_paused_builds_reconstructs_snapshots(
        self, repository: PlanningGateRepository, store: SqlitePlanningRunStore, clock: Mock
    ) -> None:
        """list_paused_builds reconstructs PausedBuildSnapshot with all required fields."""
        # Arrange: Create a paused planning run
        correlation_id = "test-corr-789"
        store.record_queued(
            correlation_id=correlation_id,
            originating_user="test@example.com",
            expected_approver="approver@example.com",
            request_text="Test request",
            triggered_by="cli",
        )
        store.transition(
            correlation_id=correlation_id,
            to_state=PlanningState.RUNNING,
            actor_identity="system",
        )

        decision = GateDecision(
            build_id=f"plan-{correlation_id}",
            stage_label="product_docs",
            target_kind="local_tool",
            target_identifier="product_docs_tool",
            mode=GateMode.HARD_STOP,
            rationale="Review required",
            coach_score=0.70,
            threshold_applied=0.80,
            decided_at=clock(),
        )

        await repository.record_decision(decision)
        request_id = derive_request_id(
            build_id=f"plan-{correlation_id}",
            stage_label="product_docs",
            attempt_count=0,
        )
        await repository.record_paused_build(
            build_id=f"plan-{correlation_id}",
            feature_id="FEAT-TEST",
            stage_label="product_docs",
            request_id=request_id,
            attempt_count=0,
            decision=decision,
        )

        # Transition to PAUSED
        store.transition(
            correlation_id=correlation_id,
            to_state=PlanningState.PAUSED,
            actor_identity="system",
        )

        # Act: List paused builds
        snapshots = await repository.list_paused_builds()

        # Assert: Snapshot contains all required fields
        assert len(snapshots) == 1
        snapshot = snapshots[0]
        assert snapshot.build_id == f"plan-{correlation_id}"
        assert snapshot.feature_id == "FEAT-TEST"
        assert snapshot.stage_label == "product_docs"
        assert snapshot.request_id == request_id
        assert snapshot.attempt_count == 0
        assert snapshot.correlation_id == correlation_id
        assert isinstance(snapshot.decision_snapshot, GateDecision)


class TestStateTransitions:
    """AC-004: State-changing methods delegate to store's CAS transitions."""

    @pytest.mark.asyncio
    async def test_transition_to_paused_delegates_to_store(
        self, repository: PlanningGateRepository, state_machine: PlanningStateMachine, store: SqlitePlanningRunStore, clock: Mock
    ) -> None:
        """transition_to_paused delegates to store's CAS transition."""
        # Arrange: Create a running planning run
        correlation_id = "test-corr-sm1"
        store.record_queued(
            correlation_id=correlation_id,
            originating_user="test@example.com",
            expected_approver="approver@example.com",
            request_text="Test request",
            triggered_by="cli",
        )
        store.transition(
            correlation_id=correlation_id,
            to_state=PlanningState.RUNNING,
            actor_identity="system",
        )

        # Need to call record_paused_build first to set up the handoff
        decision = GateDecision(
            build_id=f"plan-{correlation_id}",
            stage_label="product_docs",
            target_kind="task",
            target_identifier="TASK-001",
            mode=GateMode.HARD_STOP,
            rationale="Test pause",
            coach_score=70.0,
            threshold_applied=80.0,
            decided_at=clock(),
        )
        await repository.record_paused_build(
            build_id=f"plan-{correlation_id}",
            feature_id="FEAT-TEST",
            stage_label="product_docs",
            request_id="plan-test-corr-sm1.product_docs.0",
            attempt_count=0,
            decision=decision,
        )

        # Act: Transition to paused
        await state_machine.transition_to_paused(
            build_id=f"plan-{correlation_id}",
            stage_label="product_docs",
        )

        # Assert: State is now PAUSED
        conn = store._connection
        row = conn.execute(
            "SELECT state FROM planning_runs WHERE correlation_id = ?",
            (correlation_id,),
        ).fetchone()
        assert row is not None
        assert row[0] == PlanningState.PAUSED.value

    @pytest.mark.asyncio
    async def test_stale_transitions_do_not_raise(
        self, state_machine: PlanningStateMachine, store: SqlitePlanningRunStore
    ) -> None:
        """Stale transitions return sentinel shapes without raising."""
        # Arrange: Create a terminal planning run
        correlation_id = "test-corr-sm2"
        store.record_queued(
            correlation_id=correlation_id,
            originating_user="test@example.com",
            expected_approver="approver@example.com",
            request_text="Test request",
            triggered_by="cli",
        )
        store.transition(
            correlation_id=correlation_id,
            to_state=PlanningState.RUNNING,
            actor_identity="system",
        )
        # Transition through PAUSED to get to CANCELLED (RUNNING→CANCELLED is not allowed)
        store.transition(
            correlation_id=correlation_id,
            to_state=PlanningState.PAUSED,
            actor_identity="system",
        )
        store.transition(
            correlation_id=correlation_id,
            to_state=PlanningState.CANCELLED,
            actor_identity="user",
        )

        # Act: Attempt to transition cancelled run to running (should not raise)
        await state_machine.transition_to_running(
            build_id=f"plan-{correlation_id}",
        )

        # Assert: State remains CANCELLED
        conn = store._connection
        row = conn.execute(
            "SELECT state FROM planning_runs WHERE correlation_id = ?",
            (correlation_id,),
        ).fetchone()
        assert row is not None
        assert row[0] == PlanningState.CANCELLED.value


class TestFactoryFunction:
    """Test build_planning_gate_adapters factory function."""

    def test_factory_returns_protocol_compliant_pair(
        self, store: SqlitePlanningRunStore, clock: Mock
    ) -> None:
        """Factory returns (repository, state_machine) pair."""
        repository, state_machine = build_planning_gate_adapters(store, clock=clock)

        assert isinstance(repository, PlanningGateRepository)
        assert isinstance(state_machine, PlanningStateMachine)
        assert hasattr(repository, "record_decision")
        assert hasattr(state_machine, "transition_to_paused")

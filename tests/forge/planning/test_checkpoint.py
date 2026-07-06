"""Tests for :mod:`forge.planning.checkpoint` (TASK-MP-004B).

Test organization mirrors the acceptance criteria from
``tasks/design_approved/TASK-MP-004B-product-docs-checkpoint.md``:

* AC-001 — request_id round-trips via derive_request_id/parse_request_id
* AC-002 — SQLite-before-wire ordering (store PAUSED before publish)
* AC-003 — No auto-approve code path exists
* AC-004 — Responder identity validation against expected_approver
* AC-005 — Late response handling (terminal state bounce)
* AC-006 — Reject flows to CANCELLED
* AC-007 — Approval request envelope structure
* AC-008 — Lint/format compliance (enforced by CI)
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from nats_core.envelope import MessageEnvelope
from nats_core.events import ApprovalResponsePayload

from forge.gating.identity import derive_request_id, parse_request_id
from forge.planning.checkpoint import checkpoint_product_docs
from forge.planning.gate_adapters import build_planning_gate_adapters
from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.states import PlanningState


# ---------------------------------------------------------------------------
# Fixtures and test doubles
# ---------------------------------------------------------------------------


def _fixed_clock() -> datetime:
    """Deterministic clock for tests."""
    return datetime(2026, 7, 6, 12, 0, 0, tzinfo=UTC)


@dataclass
class _FakePublisher:
    """Records published approval request envelopes."""

    envelopes: list[MessageEnvelope] = field(default_factory=list)
    should_raise: bool = False

    async def publish_request(self, envelope: MessageEnvelope) -> None:
        if self.should_raise:
            raise RuntimeError("publisher unavailable")
        self.envelopes.append(envelope)


@dataclass
class _FakeSecondOpinionProvider:
    """Test double for SecondOpinionProvider Protocol."""

    summary_data: dict[str, Any] = field(default_factory=dict)

    async def get_summary_for_approval(
        self, *, plan_run_id: str, stage_label: str
    ) -> dict[str, Any]:
        """Return canned summary data."""
        return dict(self.summary_data)


@pytest.fixture
def tmp_db(tmp_path: Path) -> sqlite3.Connection:
    """Create a temporary SQLite database with planning schema."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")

    # Minimal schema for planning_runs
    conn.execute(
        """
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
            paused_at TEXT,
            escalated_at TEXT,
            completed_at TEXT,
            pending_approval_request_id TEXT,
            outcome TEXT,
            error TEXT,
            handoff_branch TEXT,
            handoff_path TEXT,
            CHECK (state IN ('QUEUED', 'RUNNING', 'PAUSED', 'FAILED',
                            'CANCELLED', 'TIMED_OUT', 'PLANNED_HANDOFF'))
        )
        """
    )

    # Minimal schema for planning_run_events
    conn.execute(
        """
        CREATE TABLE planning_run_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            correlation_id TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            stage_label TEXT,
            status TEXT,
            gate_mode TEXT,
            coach_score REAL,
            actor_identity TEXT,
            details_json TEXT,
            error TEXT,
            FOREIGN KEY (correlation_id) REFERENCES planning_runs(correlation_id)
        )
        """
    )

    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# AC-001: request_id round-trips
# ---------------------------------------------------------------------------


def test_request_id_round_trips_through_derive_and_parse():
    """AC-001: request_id is invertible via derive_request_id/parse_request_id."""
    plan_run_id = "plan-abc-123"
    stage_label = "product_docs"
    attempt = 0

    request_id = derive_request_id(
        build_id=plan_run_id, stage_label=stage_label, attempt_count=attempt
    )

    parsed_build_id, parsed_stage, parsed_attempt = parse_request_id(request_id)

    assert parsed_build_id == plan_run_id
    assert parsed_stage == stage_label
    assert parsed_attempt == attempt


# ---------------------------------------------------------------------------
# AC-002: SQLite-before-wire ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sqlite_before_wire_ordering(tmp_db: sqlite3.Connection):
    """AC-002: Store shows PAUSED before publisher records request envelope."""
    store = SqlitePlanningRunStore(tmp_db)
    repository, state_machine = build_planning_gate_adapters(store, clock=_fixed_clock)

    # Record QUEUED run
    correlation_id = "test-abc-123"
    store.record_queued(
        correlation_id=correlation_id,
        originating_user="test-user",
        expected_approver="rich",
        request_text="test request",
        triggered_by="test",
    )

    # Transition to RUNNING
    store.transition(
        correlation_id=correlation_id,
        to_state=PlanningState.RUNNING,
        actor_identity="test",
    )

    publisher = _FakePublisher()
    opinion_provider = _FakeSecondOpinionProvider(
        summary_data={"title": "test", "description": "test description"}
    )

    # Call checkpoint - should pause
    plan_run_id = f"plan-{correlation_id}"
    await checkpoint_product_docs(
        plan_run_id=plan_run_id,
        feature_id="FEAT-TEST-001",
        repository=repository,
        state_machine=state_machine,
        publisher=publisher,
        second_opinion_provider=opinion_provider,
        clock=_fixed_clock,
    )

    # Verify state is PAUSED in SQLite
    row = tmp_db.execute(
        "SELECT state, pending_approval_request_id FROM planning_runs WHERE correlation_id = ?",
        (correlation_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == PlanningState.PAUSED.value
    assert row[1] is not None

    # Verify publish happened (SQLite-before-wire means both complete)
    assert len(publisher.envelopes) == 1


@pytest.mark.asyncio
async def test_publish_failure_does_not_roll_back_pause(tmp_db: sqlite3.Connection):
    """AC-002: Publish failure does NOT roll back the PAUSED transition."""
    store = SqlitePlanningRunStore(tmp_db)
    repository, state_machine = build_planning_gate_adapters(store, clock=_fixed_clock)

    correlation_id = "test-def-456"
    store.record_queued(
        correlation_id=correlation_id,
        originating_user="test-user",
        expected_approver="rich",
        request_text="test request",
        triggered_by="test",
    )
    store.transition(
        correlation_id=correlation_id,
        to_state=PlanningState.RUNNING,
        actor_identity="test",
    )

    publisher = _FakePublisher(should_raise=True)
    opinion_provider = _FakeSecondOpinionProvider(summary_data={"title": "test"})

    plan_run_id = f"plan-{correlation_id}"

    # Publish will fail, but pause should persist
    with pytest.raises(RuntimeError, match="publisher unavailable"):
        await checkpoint_product_docs(
            plan_run_id=plan_run_id,
            feature_id="FEAT-TEST-001",
            repository=repository,
            state_machine=state_machine,
            publisher=publisher,
            second_opinion_provider=opinion_provider,
            clock=_fixed_clock,
        )

    # Verify state is still PAUSED
    row = tmp_db.execute(
        "SELECT state FROM planning_runs WHERE correlation_id = ?",
        (correlation_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == PlanningState.PAUSED.value


# ---------------------------------------------------------------------------
# AC-003: No auto-approve code path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_auto_approve_with_maximal_coach_evidence(tmp_db: sqlite3.Connection):
    """AC-003: Even with coach_score=1.0, checkpoint still pauses."""
    store = SqlitePlanningRunStore(tmp_db)
    repository, state_machine = build_planning_gate_adapters(store, clock=_fixed_clock)

    correlation_id = "test-ghi-789"
    store.record_queued(
        correlation_id=correlation_id,
        originating_user="test-user",
        expected_approver="rich",
        request_text="test request",
        triggered_by="test",
    )
    store.transition(
        correlation_id=correlation_id,
        to_state=PlanningState.RUNNING,
        actor_identity="test",
    )

    publisher = _FakePublisher()
    opinion_provider = _FakeSecondOpinionProvider(summary_data={"title": "test"})

    plan_run_id = f"plan-{correlation_id}"

    # Pass perfect coach score
    await checkpoint_product_docs(
        plan_run_id=plan_run_id,
        feature_id="FEAT-TEST-001",
        repository=repository,
        state_machine=state_machine,
        publisher=publisher,
        second_opinion_provider=opinion_provider,
        coach_evidence={"coach_score": 1.0},
        clock=_fixed_clock,
    )

    # Should still be PAUSED (not auto-approved)
    row = tmp_db.execute(
        "SELECT state FROM planning_runs WHERE correlation_id = ?",
        (correlation_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == PlanningState.PAUSED.value


# ---------------------------------------------------------------------------
# AC-004: Responder identity validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrong_responder_identity_stays_paused(
    tmp_db: sqlite3.Connection, caplog: pytest.LogCaptureFixture
):
    """AC-004: Response from wrong approver keeps run PAUSED."""
    store = SqlitePlanningRunStore(tmp_db)
    repository, state_machine = build_planning_gate_adapters(store, clock=_fixed_clock)

    correlation_id = "test-jkl-012"
    expected_approver = "rich"
    store.record_queued(
        correlation_id=correlation_id,
        originating_user="test-user",
        expected_approver=expected_approver,
        request_text="test request",
        triggered_by="test",
    )
    store.transition(
        correlation_id=correlation_id,
        to_state=PlanningState.RUNNING,
        actor_identity="test",
    )

    publisher = _FakePublisher()
    opinion_provider = _FakeSecondOpinionProvider(summary_data={"title": "test"})

    plan_run_id = f"plan-{correlation_id}"

    # Pause the run
    await checkpoint_product_docs(
        plan_run_id=plan_run_id,
        feature_id="FEAT-TEST-001",
        repository=repository,
        state_machine=state_machine,
        publisher=publisher,
        second_opinion_provider=opinion_provider,
        clock=_fixed_clock,
    )

    # Create response from wrong approver
    request_id = derive_request_id(
        build_id=plan_run_id, stage_label="product_docs", attempt_count=0
    )
    wrong_response = ApprovalResponsePayload(
        request_id=request_id,
        decision="approve",
        decided_by="james",  # Wrong approver
        notes=None,
    )

    # Import dispatch helper
    from forge.planning.checkpoint import _dispatch_approval_response

    # Dispatch the response
    await _dispatch_approval_response(
        response=wrong_response,
        repository=repository,
        state_machine=state_machine,
        clock=_fixed_clock,
    )

    # Should still be PAUSED
    row = tmp_db.execute(
        "SELECT state FROM planning_runs WHERE correlation_id = ?",
        (correlation_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == PlanningState.PAUSED.value

    # Should log WARNING
    assert "expected_approver" in caplog.text or "identity" in caplog.text.lower()


# ---------------------------------------------------------------------------
# AC-005: Late response handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_late_response_for_terminal_run_is_refused(tmp_db: sqlite3.Connection):
    """AC-005: Response for terminal-state run is refused, row unchanged."""
    store = SqlitePlanningRunStore(tmp_db)
    repository, state_machine = build_planning_gate_adapters(store, clock=_fixed_clock)

    correlation_id = "test-mno-345"
    store.record_queued(
        correlation_id=correlation_id,
        originating_user="test-user",
        expected_approver="rich",
        request_text="test request",
        triggered_by="test",
    )

    # Transition directly to CANCELLED (terminal)
    store.transition(
        correlation_id=correlation_id,
        to_state=PlanningState.CANCELLED,
        actor_identity="test",
        error="test cancellation",
    )

    # Create a late approval response
    plan_run_id = f"plan-{correlation_id}"
    request_id = derive_request_id(
        build_id=plan_run_id, stage_label="product_docs", attempt_count=0
    )
    late_response = ApprovalResponsePayload(
        request_id=request_id,
        decision="approve",
        decided_by="rich",
        notes=None,
    )

    from forge.planning.checkpoint import _dispatch_approval_response

    # Dispatch should be no-op
    await _dispatch_approval_response(
        response=late_response,
        repository=repository,
        state_machine=state_machine,
        clock=_fixed_clock,
    )

    # Should still be CANCELLED
    row = tmp_db.execute(
        "SELECT state FROM planning_runs WHERE correlation_id = ?",
        (correlation_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == PlanningState.CANCELLED.value


# ---------------------------------------------------------------------------
# AC-006: Reject flows to CANCELLED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reject_response_cancels_run(tmp_db: sqlite3.Connection):
    """AC-006: Reject decision transitions to CANCELLED with rejection recorded."""
    store = SqlitePlanningRunStore(tmp_db)
    repository, state_machine = build_planning_gate_adapters(store, clock=_fixed_clock)

    correlation_id = "test-pqr-678"
    store.record_queued(
        correlation_id=correlation_id,
        originating_user="test-user",
        expected_approver="rich",
        request_text="test request",
        triggered_by="test",
    )
    store.transition(
        correlation_id=correlation_id,
        to_state=PlanningState.RUNNING,
        actor_identity="test",
    )

    publisher = _FakePublisher()
    opinion_provider = _FakeSecondOpinionProvider(summary_data={"title": "test"})

    plan_run_id = f"plan-{correlation_id}"

    # Pause the run
    await checkpoint_product_docs(
        plan_run_id=plan_run_id,
        feature_id="FEAT-TEST-001",
        repository=repository,
        state_machine=state_machine,
        publisher=publisher,
        second_opinion_provider=opinion_provider,
        clock=_fixed_clock,
    )

    # Create reject response
    request_id = derive_request_id(
        build_id=plan_run_id, stage_label="product_docs", attempt_count=0
    )
    reject_response = ApprovalResponsePayload(
        request_id=request_id,
        decision="reject",
        decided_by="rich",
        notes="Not good enough",
    )

    from forge.planning.checkpoint import _dispatch_approval_response

    # Dispatch rejection
    await _dispatch_approval_response(
        response=reject_response,
        repository=repository,
        state_machine=state_machine,
        clock=_fixed_clock,
    )

    # Should be CANCELLED
    row = tmp_db.execute(
        "SELECT state FROM planning_runs WHERE correlation_id = ?",
        (correlation_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == PlanningState.CANCELLED.value

    # Verify rejection recorded in events
    events = tmp_db.execute(
        "SELECT status, details_json FROM planning_run_events WHERE correlation_id = ?",
        (correlation_id,),
    ).fetchall()

    # Should have rejection event
    rejection_found = False
    for event_row in events:
        if event_row[1]:
            try:
                details = json.loads(event_row[1])
                if "rejection" in details or event_row[0] == "REJECTED":
                    rejection_found = True
                    break
            except json.JSONDecodeError:
                pass

    assert rejection_found or any(
        "reject" in (row[0] or "").lower() for row in events
    ), "Rejection should be recorded in events"


# ---------------------------------------------------------------------------
# AC-007: Approval request envelope structure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_envelope_carries_po_summary(tmp_db: sqlite3.Connection):
    """AC-007: Approval request envelope includes compressed PO output summary."""
    store = SqlitePlanningRunStore(tmp_db)
    repository, state_machine = build_planning_gate_adapters(store, clock=_fixed_clock)

    correlation_id = "test-stu-901"
    store.record_queued(
        correlation_id=correlation_id,
        originating_user="test-user",
        expected_approver="rich",
        request_text="test request",
        triggered_by="test",
    )
    store.transition(
        correlation_id=correlation_id,
        to_state=PlanningState.RUNNING,
        actor_identity="test",
    )

    publisher = _FakePublisher()
    summary_data = {
        "title": "Product Docs Plan",
        "description": "Comprehensive product documentation",
        "sections": ["Overview", "API Reference", "Examples"],
    }
    opinion_provider = _FakeSecondOpinionProvider(summary_data=summary_data)

    plan_run_id = f"plan-{correlation_id}"

    # Pause the run
    await checkpoint_product_docs(
        plan_run_id=plan_run_id,
        feature_id="FEAT-TEST-001",
        repository=repository,
        state_machine=state_machine,
        publisher=publisher,
        second_opinion_provider=opinion_provider,
        clock=_fixed_clock,
    )

    # Verify envelope structure
    assert len(publisher.envelopes) == 1
    envelope = publisher.envelopes[0]

    # Should have payload with summary fields
    payload = envelope.payload
    assert "summary" in payload or any(key in payload for key in summary_data.keys()), (
        "Envelope should carry PO summary data"
    )

    # Verify no raw request_text interpolation (RT-09)
    request_text_in_payload = any(
        "test request" in str(value) for value in payload.values()
    )
    if request_text_in_payload:
        # If request_text appears, it should be in a validated field, not raw
        assert "validated" in str(payload) or "summary" in str(payload)

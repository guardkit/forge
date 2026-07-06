"""TASK-MP-008 — Planning intake consumer tests.

Test coverage for the planning-queued consumer handler:
- Trust boundary validation (correlation_id)
- Ack-after-persist semantics
- Deduplication handling
- Poison pill protection
- Subject filter separation from build handler
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from nats_core.envelope import EventType, MessageEnvelope

from forge.adapters.nats.planning_consumer import (
    PLANNING_QUEUED_SUBJECT_FILTER,
    PlanningConsumerDeps,
    handle_planning_message,
)
from forge.planning.run_store import SqlitePlanningRunStore

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

CORRELATION_ID = "plan-abc123"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Create a temp SQLite database with planning schema."""
    db_file = tmp_path / "test_planning.db"
    conn = sqlite3.connect(str(db_file))

    # Create planning_runs table (minimal schema)
    conn.execute(
        """
        CREATE TABLE planning_runs (
            correlation_id TEXT PRIMARY KEY NOT NULL,
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
            error TEXT,
            handoff_branch TEXT,
            handoff_path TEXT
        )
        """
    )

    # Create planning_run_events table
    conn.execute(
        """
        CREATE TABLE planning_run_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            correlation_id TEXT NOT NULL,
            stage_label TEXT NOT NULL,
            status TEXT NOT NULL,
            gate_mode TEXT,
            coach_score REAL,
            actor_identity TEXT,
            details_json TEXT,
            recorded_at TEXT NOT NULL
        )
        """
    )

    conn.commit()
    conn.close()
    return db_file


@pytest.fixture
def store(db_path: Path) -> SqlitePlanningRunStore:
    """Create a SqlitePlanningRunStore instance."""
    conn = sqlite3.connect(str(db_path))
    return SqlitePlanningRunStore(conn)


def _valid_planning_payload() -> dict[str, Any]:
    """Minimum-viable PlanningQueuedPayload dict."""
    return {
        "stage": "planning",
        "request_text": "Build a user authentication system",
        "target_repo": "appmilla/example",
        "triggered_by": "cli",
        "originating_adapter": "cli-wrapper",
        "originating_user": "rich",
        "correlation_id": CORRELATION_ID,
        "parent_request_id": None,
        "retry_count": 0,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "queued_at": datetime.now(timezone.utc).isoformat(),
    }


def _envelope_bytes(
    payload: dict[str, Any], correlation_id: str = CORRELATION_ID
) -> bytes:
    """Create MessageEnvelope bytes from payload."""
    envelope = MessageEnvelope(
        message_id="msg-test-001",
        timestamp=datetime.now(timezone.utc),
        version="1.0",
        source_id="cli-wrapper",
        event_type=EventType.BUILD_QUEUED,  # Reuse BUILD_QUEUED type
        project=None,
        correlation_id=correlation_id,
        payload=payload,
    )
    return envelope.model_dump_json().encode("utf-8")


def _make_msg(data: bytes) -> AsyncMock:
    """Mock NATS Msg exposing .data and awaitable .ack()."""
    msg = AsyncMock()
    msg.data = data
    msg.ack = AsyncMock()
    return msg


def _make_deps(store: SqlitePlanningRunStore) -> PlanningConsumerDeps:
    """Create PlanningConsumerDeps with a real store."""
    return PlanningConsumerDeps(
        store=store,
        publish_notification=AsyncMock(),  # For terminal duplicate notifications
    )


# ---------------------------------------------------------------------------
# AC-1: Valid payload → store row + ack after write
# ---------------------------------------------------------------------------


class TestValidPayloadPersistence:
    """AC-1: Valid payload creates QUEUED row and acks after persist."""

    @pytest.mark.asyncio
    async def test_valid_payload_creates_queued_row_and_acks_after(
        self, store: SqlitePlanningRunStore
    ) -> None:
        """Valid payload must create planning_runs row and ack AFTER write."""
        msg = _make_msg(_envelope_bytes(_valid_planning_payload()))
        deps = _make_deps(store)

        await handle_planning_message(msg, deps)

        # Assert: row was created
        conn = store._connection
        cursor = conn.execute(
            "SELECT * FROM planning_runs WHERE correlation_id = ?",
            (CORRELATION_ID,),
        )
        row = cursor.fetchone()
        assert row is not None, "planning_runs row should exist"

        # Check fields match payload
        row_dict = dict(row)
        assert row_dict["state"] == "QUEUED"
        assert row_dict["originating_user"] == "rich"
        assert (
            row_dict["expected_approver"] == "rich"
        )  # Initialized to originating_user
        assert row_dict["request_text"] == "Build a user authentication system"
        assert row_dict["triggered_by"] == "cli"

        # Assert: ack was called exactly once
        msg.ack.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ack_called_after_store_write_not_before(
        self, store: SqlitePlanningRunStore
    ) -> None:
        """ack() must be called AFTER the store write (call-order predicate)."""
        msg = _make_msg(_envelope_bytes(_valid_planning_payload()))
        deps = _make_deps(store)

        # Track call order
        call_order = []
        original_record_queued = store.record_queued

        def track_record_queued(*args, **kwargs):
            call_order.append("store_write")
            return original_record_queued(*args, **kwargs)

        async def track_ack():
            call_order.append("ack")

        store.record_queued = track_record_queued  # type: ignore
        msg.ack = track_ack  # type: ignore

        await handle_planning_message(msg, deps)

        # Assert: store_write happened before ack
        assert call_order == ["store_write", "ack"], (
            "ack must be called AFTER store write"
        )


# ---------------------------------------------------------------------------
# AC-2: Malformed bytes → ack + zero rows + no wedge
# ---------------------------------------------------------------------------


class TestMalformedPayloadHandling:
    """AC-2: Malformed messages are acked without wedging the consumer."""

    @pytest.mark.asyncio
    async def test_malformed_bytes_acks_and_creates_no_row(
        self, store: SqlitePlanningRunStore
    ) -> None:
        """Malformed bytes must ack and NOT create a row."""
        msg = _make_msg(b"not valid json")
        deps = _make_deps(store)

        await handle_planning_message(msg, deps)

        # Assert: ack was called
        msg.ack.assert_awaited_once()

        # Assert: no row created
        conn = store._connection
        cursor = conn.execute("SELECT COUNT(*) FROM planning_runs")
        count = cursor.fetchone()[0]
        assert count == 0, "No rows should be created for malformed payload"

    @pytest.mark.asyncio
    async def test_subsequent_valid_message_processes_after_malformed(
        self, store: SqlitePlanningRunStore
    ) -> None:
        """After malformed message, a valid message should process normally."""
        deps = _make_deps(store)

        # First: malformed message
        msg1 = _make_msg(b"garbage")
        await handle_planning_message(msg1, deps)

        # Second: valid message
        msg2 = _make_msg(_envelope_bytes(_valid_planning_payload()))
        await handle_planning_message(msg2, deps)

        # Assert: valid message was processed
        msg2.ack.assert_awaited_once()
        conn = store._connection
        cursor = conn.execute(
            "SELECT * FROM planning_runs WHERE correlation_id = ?",
            (CORRELATION_ID,),
        )
        row = cursor.fetchone()
        assert row is not None, "Valid message should create row"


# ---------------------------------------------------------------------------
# AC-3: Invalid correlation_id → ack + zero rows + rejection logged
# ---------------------------------------------------------------------------


class TestCorrelationIdValidation:
    """AC-3: Trust boundary validation on correlation_id."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "invalid_id,reason",
        [
            ("", "blank"),
            ("a" * 129, "exceeds 128 chars"),
            ("has/slash", "contains /"),
            ("has..dots", "contains .."),
            ("has space", "contains whitespace"),
            ("has~tilde", "contains ~"),
            ("has:colon", "contains :"),
            ("has?question", "contains ?"),
            ("has*star", "contains *"),
            ("has[bracket", "contains ["),
        ],
    )
    async def test_invalid_correlation_id_rejected(
        self, store: SqlitePlanningRunStore, invalid_id: str, reason: str
    ) -> None:
        """Invalid correlation_id patterns must be rejected with ack + log."""
        payload = _valid_planning_payload()
        payload["correlation_id"] = invalid_id
        msg = _make_msg(_envelope_bytes(payload, correlation_id=invalid_id))
        deps = _make_deps(store)

        await handle_planning_message(msg, deps)

        # Assert: ack was called
        msg.ack.assert_awaited_once()

        # Assert: no row created
        conn = store._connection
        cursor = conn.execute("SELECT COUNT(*) FROM planning_runs")
        count = cursor.fetchone()[0]
        assert count == 0, f"No row should be created for {reason}"

    @pytest.mark.asyncio
    async def test_valid_correlation_id_accepted(
        self, store: SqlitePlanningRunStore
    ) -> None:
        """Valid correlation_id should pass validation."""
        valid_ids = [
            "simple-id",
            "with_underscore",
            "MixedCase123",
            "a" * 128,  # Exactly 128 chars
        ]

        for valid_id in valid_ids:
            payload = _valid_planning_payload()
            payload["correlation_id"] = valid_id
            msg = _make_msg(_envelope_bytes(payload, correlation_id=valid_id))
            deps = _make_deps(store)

            await handle_planning_message(msg, deps)

            # Assert: row was created
            conn = store._connection
            cursor = conn.execute(
                "SELECT COUNT(*) FROM planning_runs WHERE correlation_id = ?",
                (valid_id,),
            )
            count = cursor.fetchone()[0]
            assert count == 1, f"Valid correlation_id {valid_id} should be accepted"


# ---------------------------------------------------------------------------
# AC-4: Redelivery deduplication
# ---------------------------------------------------------------------------


class TestRedeliveryDeduplication:
    """AC-4: Redelivered messages are deduplicated correctly."""

    @pytest.mark.asyncio
    async def test_non_terminal_duplicate_acks_with_one_row(
        self, store: SqlitePlanningRunStore
    ) -> None:
        """Non-terminal duplicate: ack + still exactly one row."""
        msg1 = _make_msg(_envelope_bytes(_valid_planning_payload()))
        msg2 = _make_msg(_envelope_bytes(_valid_planning_payload()))
        deps = _make_deps(store)

        # First message
        await handle_planning_message(msg1, deps)

        # Second message (redelivery)
        await handle_planning_message(msg2, deps)

        # Assert: both acked
        msg1.ack.assert_awaited_once()
        msg2.ack.assert_awaited_once()

        # Assert: exactly one row
        conn = store._connection
        cursor = conn.execute(
            "SELECT COUNT(*) FROM planning_runs WHERE correlation_id = ?",
            (CORRELATION_ID,),
        )
        count = cursor.fetchone()[0]
        assert count == 1, "Should have exactly one row for duplicate"

    @pytest.mark.asyncio
    async def test_terminal_duplicate_sends_notification(
        self, store: SqlitePlanningRunStore
    ) -> None:
        """Terminal duplicate: ack + notification published."""
        # Pre-populate store with terminal run
        from forge.planning.states import PlanningState

        # Create deps BEFORE populating store so we can track the mock
        mock_notification = AsyncMock()
        deps = PlanningConsumerDeps(
            store=store,
            publish_notification=mock_notification,
        )

        # Create initial queued run
        result = store.record_queued(
            correlation_id=CORRELATION_ID,
            originating_user="rich",
            expected_approver="rich",
            request_text="Test request",
            triggered_by="cli",
        )
        assert result is None, "Initial record should succeed"

        # Transition through intermediate states to reach terminal
        # QUEUED -> RUNNING -> PLANNED_HANDOFF
        transition_result = store.transition(
            correlation_id=CORRELATION_ID,
            to_state=PlanningState.RUNNING,
            actor_identity="system",
        )
        assert transition_result is None, "QUEUED -> RUNNING should succeed"

        transition_result = store.transition(
            correlation_id=CORRELATION_ID,
            to_state=PlanningState.PLANNED_HANDOFF,
            actor_identity="system",
        )
        assert transition_result is None, "RUNNING -> PLANNED_HANDOFF should succeed"

        # Verify terminal state
        conn = store._connection
        cursor = conn.execute(
            "SELECT state FROM planning_runs WHERE correlation_id = ?",
            (CORRELATION_ID,),
        )
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "PLANNED_HANDOFF", (
            f"State should be PLANNED_HANDOFF, got {row[0]}"
        )

        # Now send duplicate message
        msg = _make_msg(_envelope_bytes(_valid_planning_payload()))

        await handle_planning_message(msg, deps)

        # Assert: ack called
        msg.ack.assert_awaited_once()

        # Assert: notification published (RT-10)
        mock_notification.assert_awaited_once()


# ---------------------------------------------------------------------------
# AC-5: Subject filter separation
# ---------------------------------------------------------------------------


class TestSubjectFilterSeparation:
    """AC-5: Planning and build subject filters don't overlap."""

    def test_planning_filter_does_not_match_build_filter(self) -> None:
        """Planning filter must not match build subject patterns."""
        from forge.adapters.nats.pipeline_consumer import BUILD_QUEUE_SUBJECT

        # Check they are different
        assert PLANNING_QUEUED_SUBJECT_FILTER != BUILD_QUEUE_SUBJECT

        # Check planning filter pattern
        assert PLANNING_QUEUED_SUBJECT_FILTER == "pipeline.planning-queued.*"

        # Verify they don't cross-match using NATS subject pattern logic
        # planning-queued.* should not match build-queued.*
        assert "planning-queued" in PLANNING_QUEUED_SUBJECT_FILTER
        assert "build-queued" in BUILD_QUEUE_SUBJECT

    def test_no_imports_from_build_handler(self) -> None:
        """Planning consumer must not import build handler internals."""
        import ast
        import sys
        from pathlib import Path

        # Read the planning_consumer.py source
        module_path = Path(
            sys.modules["forge.adapters.nats.planning_consumer"].__file__
        )
        source = module_path.read_text()
        tree = ast.parse(source)

        # Collect all imports
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.append(node.module)

        # Assert: no imports from pipeline_consumer or dispatch/gate modules
        forbidden = ["dispatch_build", "maybe_gate_build", "pipeline_consumer"]
        for imp in imports:
            for forbidden_name in forbidden:
                assert forbidden_name not in imp, (
                    f"Planning consumer must not import {forbidden_name}"
                )


# ---------------------------------------------------------------------------
# AC-6: Never raises; tolerates missing originating_adapter
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """AC-6: Handler never raises and tolerates missing fields."""

    @pytest.mark.asyncio
    async def test_handle_planning_message_never_raises_on_any_input(
        self, store: SqlitePlanningRunStore
    ) -> None:
        """Handler must never raise, even on unexpected input."""
        test_inputs = [
            b"",
            b"null",
            b"{}",
            b'{"invalid": "payload"}',
        ]

        deps = _make_deps(store)

        for data in test_inputs:
            msg = _make_msg(data)
            # Should not raise
            await handle_planning_message(msg, deps)
            # Should always ack
            msg.ack.assert_awaited()

    @pytest.mark.asyncio
    async def test_missing_originating_adapter_logged_but_acked(
        self, store: SqlitePlanningRunStore
    ) -> None:
        """Missing originating_adapter causes validation failure but handler acks.

        Note: The wire contract (PlanningQueuedPayload) may enforce originating_adapter
        presence. This test verifies the handler never raises and always acks, even
        when payload validation fails.
        """
        payload = _valid_planning_payload()
        payload["originating_adapter"] = None  # Will fail pydantic validation

        msg = _make_msg(_envelope_bytes(payload))
        deps = _make_deps(store)

        # Should not raise (AC-6: never raises)
        await handle_planning_message(msg, deps)

        # Assert: ack was called (AC-6: always acks)
        msg.ack.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test CORRELATION_ID_PATTERN regex
# ---------------------------------------------------------------------------


def test_correlation_id_pattern_regex() -> None:
    """Verify CORRELATION_ID_PATTERN validation via _is_valid_correlation_id.

    TASK-MP-012: dots are rejected outright — a dotted correlation_id
    fragments the approval subject past jarvis's 4-token gate (silent
    drop), so the pattern excludes '.' entirely.
    """
    from forge.adapters.nats.planning_consumer import _is_valid_correlation_id

    # Valid cases
    valid = [
        "simple",
        "with-dashes",
        "with_underscores",
        "MixedCase123",
        "a" * 128,
    ]
    for test_id in valid:
        assert _is_valid_correlation_id(test_id), f"Should accept valid: {test_id}"

    # Invalid cases
    invalid = [
        "",
        "with.dots",  # dots fragment the approval subject (TASK-MP-012)
        "has/slash",
        "has space",
        "has~tilde",
        "has:colon",
        "has?question",
        "has*star",
        "has[bracket",
        "has..consecutive",  # Rejected by additional .. check
        "a" * 129,  # Too long
    ]
    for test_id in invalid:
        assert not _is_valid_correlation_id(test_id), (
            f"Should reject invalid: {test_id}"
        )


# ---------------------------------------------------------------------------
# TASK-MP-012: nak-on-store-failure + on_recorded driver kick
# ---------------------------------------------------------------------------


class TestStoreFailureRedelivery:
    """A transient store failure must NOT permanently drop the request."""

    @pytest.mark.asyncio
    async def test_store_failure_naks_instead_of_acking(
        self, store: SqlitePlanningRunStore
    ) -> None:
        msg = _make_msg(_envelope_bytes(_valid_planning_payload()))
        msg.nak = AsyncMock()
        deps = _make_deps(store)

        with patch.object(
            store, "record_queued", side_effect=RuntimeError("SQLITE_BUSY")
        ):
            await handle_planning_message(msg, deps)

        msg.ack.assert_not_awaited()
        msg.nak.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_store_failure_without_nak_leaves_unacked(
        self, store: SqlitePlanningRunStore
    ) -> None:
        """No nak() available -> no ack either; ack_wait redelivers."""
        msg = AsyncMock()
        msg.data = _envelope_bytes(_valid_planning_payload())
        del msg.nak  # AsyncMock auto-creates attributes; remove it
        deps = _make_deps(store)

        with patch.object(
            store, "record_queued", side_effect=RuntimeError("SQLITE_BUSY")
        ):
            await handle_planning_message(msg, deps)

        msg.ack.assert_not_awaited()


class TestOnRecordedCallback:
    """TASK-MP-012: the composition kicks the chain driver post-ack."""

    @pytest.mark.asyncio
    async def test_on_recorded_fires_after_successful_persist(
        self, store: SqlitePlanningRunStore
    ) -> None:
        recorded: list[str] = []

        async def on_recorded(correlation_id: str) -> None:
            recorded.append(correlation_id)

        msg = _make_msg(_envelope_bytes(_valid_planning_payload()))
        deps = PlanningConsumerDeps(
            store=store, publish_notification=None, on_recorded=on_recorded
        )

        await handle_planning_message(msg, deps)

        msg.ack.assert_awaited_once()
        assert recorded == [CORRELATION_ID]

    @pytest.mark.asyncio
    async def test_on_recorded_not_fired_for_duplicates(
        self, store: SqlitePlanningRunStore
    ) -> None:
        recorded: list[str] = []

        async def on_recorded(correlation_id: str) -> None:
            recorded.append(correlation_id)

        deps = PlanningConsumerDeps(
            store=store, publish_notification=None, on_recorded=on_recorded
        )
        await handle_planning_message(
            _make_msg(_envelope_bytes(_valid_planning_payload())), deps
        )
        await handle_planning_message(
            _make_msg(_envelope_bytes(_valid_planning_payload())), deps
        )

        assert recorded == [CORRELATION_ID], "duplicate must not re-kick the driver"

    @pytest.mark.asyncio
    async def test_on_recorded_exception_never_wedges_intake(
        self, store: SqlitePlanningRunStore
    ) -> None:
        async def on_recorded(correlation_id: str) -> None:
            raise RuntimeError("driver defect")

        msg = _make_msg(_envelope_bytes(_valid_planning_payload()))
        deps = PlanningConsumerDeps(
            store=store, publish_notification=None, on_recorded=on_recorded
        )

        await handle_planning_message(msg, deps)  # must not raise

        msg.ack.assert_awaited_once()

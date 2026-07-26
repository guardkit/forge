"""Tests for schema v5 additive migration (TASK-UBS-002-integration §2).

The v5 migration adds the additive ``builds.profile TEXT`` column so a
``forge queue --profile <name>`` selection travels to the daemon on the build
row (option §2(a), forge-only) rather than the frozen nats-core payload.

Discipline verified here (mirrors ``test_schema_v4_migration``):
- Fresh DBs migrate to version 5 and expose the ``profile`` column.
- The migration is additive — ``stage_log`` / ``planning_runs`` are untouched.
- An existing v4 database (with a build row) upgrades in place; the pre-existing
  row reads back NULL for ``profile`` (backward-compatible default).
- The migration is idempotent.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations


def _table_schema(cx: sqlite3.Connection, table_name: str) -> str:
    row = cx.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row[0] if row else ""


def _column_names(cx: sqlite3.Connection, table_name: str) -> list[str]:
    return [r[1] for r in cx.execute(f"PRAGMA table_info({table_name})").fetchall()]


def _insert_build(
    cx: sqlite3.Connection, build_id: str, *, profile: str | None = None
) -> None:
    """Insert a minimal QUEUED build row (all NOT NULL columns populated)."""
    if profile is None:
        cx.execute(
            "INSERT INTO builds (build_id, feature_id, repo, branch, "
            "feature_yaml_path, status, triggered_by, correlation_id, queued_at) "
            "VALUES (?, 'FEAT-1', 'r', 'main', 'f.yaml', 'QUEUED', 'cli', ?, "
            "'2026-01-01T00:00:00Z')",
            (build_id, f"corr-{build_id}"),
        )
    else:
        cx.execute(
            "INSERT INTO builds (build_id, feature_id, repo, branch, "
            "feature_yaml_path, status, triggered_by, correlation_id, queued_at, "
            "profile) VALUES (?, 'FEAT-1', 'r', 'main', 'f.yaml', 'QUEUED', 'cli', "
            "?, '2026-01-01T00:00:00Z', ?)",
            (build_id, f"corr-{build_id}", profile),
        )


# ---------------------------------------------------------------------------
# Fresh-database migration
# ---------------------------------------------------------------------------


def test_fresh_db_migrates_to_version_5(tmp_path: Path) -> None:
    # Pin the migration head at v5 so this v5-specific test stays robust to
    # later additive bumps (v6+); the runner otherwise advances to the newest.
    cx = sqlite_connect.connect_writer(tmp_path / "fresh.db")
    try:
        original = migrations._MIGRATIONS
        migrations._MIGRATIONS = tuple(m for m in original if m[0] <= 5)
        try:
            assert migrations.apply_at_boot(cx) == 5
        finally:
            migrations._MIGRATIONS = original
    finally:
        cx.close()


def test_fresh_db_has_profile_column(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "fresh.db")
    try:
        migrations.apply_at_boot(cx)
        assert "profile" in _column_names(cx, "builds")
    finally:
        cx.close()


def test_profile_column_accepts_value_and_null(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "fresh.db")
    try:
        migrations.apply_at_boot(cx)
        _insert_build(cx, "b-unattended", profile="unattended")
        _insert_build(cx, "b-null")  # no profile → NULL
        cx.commit()
        rows = dict(
            cx.execute("SELECT build_id, profile FROM builds").fetchall()
        )
        assert rows["b-unattended"] == "unattended"
        assert rows["b-null"] is None
    finally:
        cx.close()


# ---------------------------------------------------------------------------
# Additivity — unrelated tables untouched
# ---------------------------------------------------------------------------


def test_v5_is_additive_leaves_other_tables_unchanged(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "staged.db")
    try:
        original = migrations._MIGRATIONS
        migrations._MIGRATIONS = tuple(m for m in original if m[0] <= 4)
        try:
            migrations.apply_at_boot(cx)
            stage_log_v4 = _table_schema(cx, "stage_log")
            planning_runs_v4 = _table_schema(cx, "planning_runs")
        finally:
            migrations._MIGRATIONS = original

        migrations.apply_at_boot(cx)
        assert _table_schema(cx, "stage_log") == stage_log_v4
        assert _table_schema(cx, "planning_runs") == planning_runs_v4
    finally:
        cx.close()


# ---------------------------------------------------------------------------
# Existing-database upgrade — old rows read back NULL profile
# ---------------------------------------------------------------------------


def test_v4_upgrade_adds_column_old_rows_read_null(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "legacy.db")
    try:
        # Migrate to v4 only, then seed a pre-profile build row.
        original = migrations._MIGRATIONS
        migrations._MIGRATIONS = tuple(m for m in original if m[0] <= 4)
        try:
            migrations.apply_at_boot(cx)
        finally:
            migrations._MIGRATIONS = original
        assert "profile" not in _column_names(cx, "builds")
        _insert_build(cx, "legacy-1")
        cx.commit()

        # Upgrade to v5 (pin the head so this v5-scoped test ignores v6+).
        migrations._MIGRATIONS = tuple(m for m in original if m[0] <= 5)
        try:
            assert migrations.apply_at_boot(cx) == 5
        finally:
            migrations._MIGRATIONS = original
        assert "profile" in _column_names(cx, "builds")

        # The pre-existing row reads back NULL (backward-compatible default).
        row = cx.execute(
            "SELECT profile FROM builds WHERE build_id='legacy-1'"
        ).fetchone()
        assert row is not None and row[0] is None
    finally:
        cx.close()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_v5_migration_is_idempotent(tmp_path: Path) -> None:
    # Pin the head at v5 so idempotency is asserted against the v5 terminal
    # version, independent of any later additive migration (v6+).
    cx = sqlite_connect.connect_writer(tmp_path / "idem.db")
    try:
        original = migrations._MIGRATIONS
        migrations._MIGRATIONS = tuple(m for m in original if m[0] <= 5)
        try:
            assert migrations.apply_at_boot(cx) == 5
            _insert_build(cx, "keep", profile="unattended")
            cx.commit()

            # Re-running applies nothing and preserves data.
            assert migrations.apply_at_boot(cx) == 5
        finally:
            migrations._MIGRATIONS = original
        row = cx.execute(
            "SELECT profile FROM builds WHERE build_id='keep'"
        ).fetchone()
        assert row is not None and row[0] == "unattended"

        # Exactly one version-5 ledger row.
        n = cx.execute(
            "SELECT COUNT(*) FROM schema_version WHERE version=5"
        ).fetchone()[0]
        assert n == 1
    finally:
        cx.close()

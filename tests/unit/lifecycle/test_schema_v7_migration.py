"""Tests for schema v7 additive migration (budget-breach detection record).

The v7 migration adds the additive ``builds.budget_breach TEXT`` column — the
``forge serve`` lifecycle-bridge observer's HONEST record of a mid-run budget
cap breach (FEAT-UBS-002 stage 2, DETECT). It is first-write-wins and never a
status change.

Discipline verified here (mirrors ``test_schema_v6_migration``):
- Fresh DBs migrate to version 7 and expose the ``budget_breach`` column.
- The migration is additive — ``stage_log`` / ``planning_runs`` are untouched.
- An existing v6 database (with a build row) upgrades in place; the pre-existing
  row reads back NULL for ``budget_breach`` (backward-compatible default).
- Fresh (v1→v7) and upgraded (v6→v7) DBs converge on an identical ``builds``
  column set, and the ledger carries a single version-7 row.
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
    cx: sqlite3.Connection, build_id: str, *, breach: str | None = None
) -> None:
    """Insert a minimal QUEUED build row (all NOT NULL columns populated)."""
    if breach is None:
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
            "budget_breach) VALUES (?, 'FEAT-1', 'r', 'main', 'f.yaml', "
            "'QUEUED', 'cli', ?, '2026-01-01T00:00:00Z', ?)",
            (build_id, f"corr-{build_id}", breach),
        )


# ---------------------------------------------------------------------------
# Fresh-database migration
# ---------------------------------------------------------------------------


def test_fresh_db_migrates_to_version_7(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "fresh.db")
    try:
        assert migrations.apply_at_boot(cx) == 7
    finally:
        cx.close()


def test_fresh_db_has_budget_breach_column(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "fresh.db")
    try:
        migrations.apply_at_boot(cx)
        assert "budget_breach" in _column_names(cx, "builds")
    finally:
        cx.close()


def test_budget_breach_column_accepts_value_and_null(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "fresh.db")
    try:
        migrations.apply_at_boot(cx)
        _insert_build(cx, "b-breach", breach="wall_clock: 3712.0s > 3600.0s @ ts")
        _insert_build(cx, "b-null")  # no breach → NULL
        cx.commit()
        rows = dict(
            cx.execute("SELECT build_id, budget_breach FROM builds").fetchall()
        )
        assert rows["b-breach"] == "wall_clock: 3712.0s > 3600.0s @ ts"
        assert rows["b-null"] is None
    finally:
        cx.close()


# ---------------------------------------------------------------------------
# Additivity — unrelated tables untouched
# ---------------------------------------------------------------------------


def test_v7_is_additive_leaves_other_tables_unchanged(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "staged.db")
    try:
        original = migrations._MIGRATIONS
        migrations._MIGRATIONS = tuple(m for m in original if m[0] <= 6)
        try:
            migrations.apply_at_boot(cx)
            stage_log_v6 = _table_schema(cx, "stage_log")
            planning_runs_v6 = _table_schema(cx, "planning_runs")
        finally:
            migrations._MIGRATIONS = original

        migrations.apply_at_boot(cx)
        assert _table_schema(cx, "stage_log") == stage_log_v6
        assert _table_schema(cx, "planning_runs") == planning_runs_v6
    finally:
        cx.close()


# ---------------------------------------------------------------------------
# Existing-database upgrade — old rows read back NULL breach
# ---------------------------------------------------------------------------


def test_v6_upgrade_adds_column_old_rows_read_null(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "legacy.db")
    try:
        # Migrate to v6 only, then seed a pre-breach build row.
        original = migrations._MIGRATIONS
        migrations._MIGRATIONS = tuple(m for m in original if m[0] <= 6)
        try:
            migrations.apply_at_boot(cx)
        finally:
            migrations._MIGRATIONS = original
        assert "budget_breach" not in _column_names(cx, "builds")
        _insert_build(cx, "legacy-1")
        cx.commit()

        # Upgrade to v7.
        assert migrations.apply_at_boot(cx) == 7
        assert "budget_breach" in _column_names(cx, "builds")

        # The pre-existing row reads back NULL (backward-compatible default).
        row = cx.execute(
            "SELECT budget_breach FROM builds WHERE build_id='legacy-1'"
        ).fetchone()
        assert row is not None and row[0] is None
    finally:
        cx.close()


# ---------------------------------------------------------------------------
# Convergence — fresh v1→v7 and upgraded v6→v7 agree on the builds column set
# ---------------------------------------------------------------------------


def test_fresh_and_upgraded_builds_columns_converge(tmp_path: Path) -> None:
    # Fresh v1→v7.
    fresh = sqlite_connect.connect_writer(tmp_path / "fresh.db")
    # Staged v1→v6 then v7.
    staged = sqlite_connect.connect_writer(tmp_path / "staged.db")
    try:
        migrations.apply_at_boot(fresh)

        original = migrations._MIGRATIONS
        migrations._MIGRATIONS = tuple(m for m in original if m[0] <= 6)
        try:
            migrations.apply_at_boot(staged)
        finally:
            migrations._MIGRATIONS = original
        migrations.apply_at_boot(staged)

        assert _column_names(fresh, "builds") == _column_names(staged, "builds")

        # The ledger carries a single version-7 row on both paths.
        assert (
            fresh.execute(
                "SELECT COUNT(*) FROM schema_version WHERE version=7"
            ).fetchone()[0]
            == 1
        )
        assert (
            staged.execute(
                "SELECT COUNT(*) FROM schema_version WHERE version=7"
            ).fetchone()[0]
            == 1
        )
    finally:
        fresh.close()
        staged.close()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_v7_migration_is_idempotent(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "idem.db")
    try:
        assert migrations.apply_at_boot(cx) == 7
        _insert_build(cx, "keep", breach="coach_score: 0.0 < 0.5 floor @ ts")
        cx.commit()

        # Re-running applies nothing and preserves data.
        assert migrations.apply_at_boot(cx) == 7
        row = cx.execute(
            "SELECT budget_breach FROM builds WHERE build_id='keep'"
        ).fetchone()
        assert row is not None and row[0] == "coach_score: 0.0 < 0.5 floor @ ts"

        # Exactly one version-7 ledger row.
        n = cx.execute(
            "SELECT COUNT(*) FROM schema_version WHERE version=7"
        ).fetchone()[0]
        assert n == 1
    finally:
        cx.close()

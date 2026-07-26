"""Tests for schema v6 additive migration (coach-score durability).

The v6 migration adds the additive ``builds.last_coach_score REAL`` column so
the UBS1C coach score — which survives the lifecycle-bridge translation only as
``StageCompletePayload.coach_score`` / ``BuildPausedPayload.coach_score`` — has
a durable sink the ``min_coach_score`` budget floor can read back.

Discipline verified here (mirrors ``test_schema_v5_migration``):
- Fresh DBs migrate to version 6 and expose the ``last_coach_score`` column.
- The migration is additive — ``stage_log`` / ``planning_runs`` are untouched.
- An existing v5 database (with a build row) upgrades in place; the pre-existing
  row reads back NULL for ``last_coach_score`` (backward-compatible default).
- Fresh (v1→v6) and upgraded (v5→v6) DBs converge on an identical ``builds``
  column set, and the ledger carries a single version-6 row.
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
    cx: sqlite3.Connection, build_id: str, *, score: float | None = None
) -> None:
    """Insert a minimal QUEUED build row (all NOT NULL columns populated)."""
    if score is None:
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
            "last_coach_score) VALUES (?, 'FEAT-1', 'r', 'main', 'f.yaml', "
            "'QUEUED', 'cli', ?, '2026-01-01T00:00:00Z', ?)",
            (build_id, f"corr-{build_id}", score),
        )


# ---------------------------------------------------------------------------
# Fresh-database migration
# ---------------------------------------------------------------------------


def test_fresh_db_migrates_to_version_6(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "fresh.db")
    try:
        assert migrations.apply_at_boot(cx) == 6
    finally:
        cx.close()


def test_fresh_db_has_last_coach_score_column(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "fresh.db")
    try:
        migrations.apply_at_boot(cx)
        assert "last_coach_score" in _column_names(cx, "builds")
    finally:
        cx.close()


def test_last_coach_score_column_accepts_value_and_null(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "fresh.db")
    try:
        migrations.apply_at_boot(cx)
        _insert_build(cx, "b-scored", score=0.75)
        _insert_build(cx, "b-null")  # no score → NULL
        cx.commit()
        rows = dict(
            cx.execute("SELECT build_id, last_coach_score FROM builds").fetchall()
        )
        assert rows["b-scored"] == 0.75
        assert rows["b-null"] is None
    finally:
        cx.close()


# ---------------------------------------------------------------------------
# Additivity — unrelated tables untouched
# ---------------------------------------------------------------------------


def test_v6_is_additive_leaves_other_tables_unchanged(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "staged.db")
    try:
        original = migrations._MIGRATIONS
        migrations._MIGRATIONS = tuple(m for m in original if m[0] <= 5)
        try:
            migrations.apply_at_boot(cx)
            stage_log_v5 = _table_schema(cx, "stage_log")
            planning_runs_v5 = _table_schema(cx, "planning_runs")
        finally:
            migrations._MIGRATIONS = original

        migrations.apply_at_boot(cx)
        assert _table_schema(cx, "stage_log") == stage_log_v5
        assert _table_schema(cx, "planning_runs") == planning_runs_v5
    finally:
        cx.close()


# ---------------------------------------------------------------------------
# Existing-database upgrade — old rows read back NULL score
# ---------------------------------------------------------------------------


def test_v5_upgrade_adds_column_old_rows_read_null(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "legacy.db")
    try:
        # Migrate to v5 only, then seed a pre-score build row.
        original = migrations._MIGRATIONS
        migrations._MIGRATIONS = tuple(m for m in original if m[0] <= 5)
        try:
            migrations.apply_at_boot(cx)
        finally:
            migrations._MIGRATIONS = original
        assert "last_coach_score" not in _column_names(cx, "builds")
        _insert_build(cx, "legacy-1")
        cx.commit()

        # Upgrade to v6.
        assert migrations.apply_at_boot(cx) == 6
        assert "last_coach_score" in _column_names(cx, "builds")

        # The pre-existing row reads back NULL (backward-compatible default).
        row = cx.execute(
            "SELECT last_coach_score FROM builds WHERE build_id='legacy-1'"
        ).fetchone()
        assert row is not None and row[0] is None
    finally:
        cx.close()


# ---------------------------------------------------------------------------
# Convergence — fresh v1→v6 and upgraded v5→v6 agree on the builds column set
# ---------------------------------------------------------------------------


def test_fresh_and_upgraded_builds_columns_converge(tmp_path: Path) -> None:
    # Fresh v1→v6.
    fresh = sqlite_connect.connect_writer(tmp_path / "fresh.db")
    # Staged v1→v5 then v6.
    staged = sqlite_connect.connect_writer(tmp_path / "staged.db")
    try:
        migrations.apply_at_boot(fresh)

        original = migrations._MIGRATIONS
        migrations._MIGRATIONS = tuple(m for m in original if m[0] <= 5)
        try:
            migrations.apply_at_boot(staged)
        finally:
            migrations._MIGRATIONS = original
        migrations.apply_at_boot(staged)

        assert _column_names(fresh, "builds") == _column_names(staged, "builds")

        # The ledger carries a single version-6 row on both paths.
        assert (
            fresh.execute(
                "SELECT COUNT(*) FROM schema_version WHERE version=6"
            ).fetchone()[0]
            == 1
        )
        assert (
            staged.execute(
                "SELECT COUNT(*) FROM schema_version WHERE version=6"
            ).fetchone()[0]
            == 1
        )
    finally:
        fresh.close()
        staged.close()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_v6_migration_is_idempotent(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "idem.db")
    try:
        assert migrations.apply_at_boot(cx) == 6
        _insert_build(cx, "keep", score=0.5)
        cx.commit()

        # Re-running applies nothing and preserves data.
        assert migrations.apply_at_boot(cx) == 6
        row = cx.execute(
            "SELECT last_coach_score FROM builds WHERE build_id='keep'"
        ).fetchone()
        assert row is not None and row[0] == 0.5

        # Exactly one version-6 ledger row.
        n = cx.execute(
            "SELECT COUNT(*) FROM schema_version WHERE version=6"
        ).fetchone()[0]
        assert n == 1
    finally:
        cx.close()

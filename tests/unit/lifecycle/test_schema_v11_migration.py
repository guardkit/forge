"""Tests for schema v11 — the additive ``builds.merge_branch`` column.

Rewrite-on-refusal spec 2026-09-06, Part M, rule 54: the merge word merges
the branch the build actually made. The conductor writes the fix journey's
own branch here; a feature build leaves it NULL and every reader falls back
to ``autobuild/<feature id>``.

Same discipline as the v6 … v10 migration suites: fresh migrates, additive,
in-place upgrade, fresh and upgraded converge, idempotent. Plus the two this
column owes: a historical row reads back NULL, and ``builds.branch`` (the
branch a build was queued ON) is a different column that is not touched.
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


def _migrate_to(cx: sqlite3.Connection, version: int) -> None:
    original = migrations._MIGRATIONS
    migrations._MIGRATIONS = tuple(m for m in original if m[0] <= version)
    try:
        migrations.apply_at_boot(cx)
    finally:
        migrations._MIGRATIONS = original


def _insert_build(cx: sqlite3.Connection, build_id: str, *, branch: str = "main") -> None:
    cx.execute(
        """
        INSERT INTO builds (
            build_id, feature_id, repo, branch, feature_yaml_path, status,
            triggered_by, correlation_id, queued_at, mode
        ) VALUES (?, 'FEAT-V11', 'org/repo', ?, 'f.yaml', 'QUEUED', 'cli',
                  ?, '2026-09-07T12:00:00+00:00', 'mode-c')
        """,
        (build_id, branch, f"corr-{build_id}"),
    )
    cx.commit()


def test_fresh_db_migrates_to_v11_with_the_column(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "fresh.db")
    try:
        assert migrations.apply_at_boot(cx) == migrations._SCHEMA_VERSION
        assert migrations._SCHEMA_VERSION >= 11
        assert "merge_branch" in _column_names(cx, "builds")
        versions = [r[0] for r in cx.execute("SELECT version FROM schema_version ORDER BY version")]
        assert 11 in versions
    finally:
        cx.close()


def test_a_v10_database_upgrades_in_place_and_old_rows_read_null(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "v10.db")
    try:
        _migrate_to(cx, 10)
        assert "merge_branch" not in _column_names(cx, "builds")
        _insert_build(cx, "build-old", branch="repair/TASK-OLD")

        migrations.apply_at_boot(cx)

        assert "merge_branch" in _column_names(cx, "builds")
        row = cx.execute(
            "SELECT branch, merge_branch FROM builds WHERE build_id = 'build-old'"
        ).fetchone()
        assert row[0] == "repair/TASK-OLD"  # the queued-on branch is untouched
        assert row[1] is None  # the merge word falls back to the feature's own
    finally:
        cx.close()


def test_v11_is_additive_leaves_other_tables_unchanged(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "staged.db")
    try:
        _migrate_to(cx, 10)
        planning_runs_v10 = _table_schema(cx, "planning_runs")
        stage_log_v10 = _table_schema(cx, "stage_log")
        work_queue_v10 = _table_schema(cx, "work_queue")
        builds_columns_v10 = _column_names(cx, "builds")

        _migrate_to(cx, 11)

        assert _table_schema(cx, "planning_runs") == planning_runs_v10
        assert _table_schema(cx, "stage_log") == stage_log_v10
        assert _table_schema(cx, "work_queue") == work_queue_v10
        assert _column_names(cx, "builds") == [*builds_columns_v10, "merge_branch"]
    finally:
        cx.close()


def test_fresh_and_upgraded_converge(tmp_path: Path) -> None:
    fresh = sqlite_connect.connect_writer(tmp_path / "fresh.db")
    upgraded = sqlite_connect.connect_writer(tmp_path / "upgraded.db")
    try:
        _migrate_to(fresh, 11)
        _migrate_to(upgraded, 10)
        _migrate_to(upgraded, 11)
        assert _column_names(fresh, "builds") == _column_names(upgraded, "builds")
        for cx in (fresh, upgraded):
            count = cx.execute(
                "SELECT COUNT(*) FROM schema_version WHERE version = 11"
            ).fetchone()[0]
            assert count == 1
    finally:
        fresh.close()
        upgraded.close()


def test_v11_is_idempotent(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "twice.db")
    try:
        migrations.apply_at_boot(cx)
        first = _column_names(cx, "builds")
        migrations.apply_at_boot(cx)
        assert _column_names(cx, "builds") == first
        assert first.count("merge_branch") == 1
    finally:
        cx.close()

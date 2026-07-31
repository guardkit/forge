"""Tests for schema v8 — ``builds.task_id``, the fix journey's durable subject.

Conductor revival Stage 2, shakeout item 1.

``forge queue --mode c TASK-XXX`` has always put the task identifier on the
WIRE (required iff mode-c). Nothing persisted it — so the conductor, which
reads durable rows rather than a one-shot payload, had no subject to dispatch
``/task-review --task-id`` against, and the subprocess dispatcher correctly
refused every first turn. A wire field that no row remembers also cannot
survive a daemon restart mid-journey.

Same discipline as the v6 / v7 migration suites: fresh migrates, additive,
in-place upgrade reads NULL, fresh and upgraded converge, idempotent.
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
    cx: sqlite3.Connection, build_id: str, *, task_id: str | None = None
) -> None:
    if task_id is None:
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
            "task_id) VALUES (?, 'FEAT-1', 'r', 'main', 'f.yaml', 'QUEUED', "
            "'cli', ?, '2026-01-01T00:00:00Z', ?)",
            (build_id, f"corr-{build_id}", task_id),
        )


def _migrate_to(cx: sqlite3.Connection, version: int) -> None:
    original = migrations._MIGRATIONS
    migrations._MIGRATIONS = tuple(m for m in original if m[0] <= version)
    try:
        migrations.apply_at_boot(cx)
    finally:
        migrations._MIGRATIONS = original


def test_fresh_db_has_task_id_column(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "fresh.db")
    try:
        assert migrations.apply_at_boot(cx) >= 8
        assert "task_id" in _column_names(cx, "builds")
    finally:
        cx.close()


def test_task_id_accepts_a_value_and_null(tmp_path: Path) -> None:
    """NULL is Mode A / Mode B; a value is the fix journey's subject."""
    cx = sqlite_connect.connect_writer(tmp_path / "fresh.db")
    try:
        migrations.apply_at_boot(cx)
        _insert_build(cx, "b-fix", task_id="TASK-FIX007")
        _insert_build(cx, "b-routine")
        cx.commit()
        rows = dict(cx.execute("SELECT build_id, task_id FROM builds").fetchall())
        assert rows["b-fix"] == "TASK-FIX007"
        assert rows["b-routine"] is None
    finally:
        cx.close()


def test_v8_is_additive_leaves_other_tables_unchanged(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "staged.db")
    try:
        _migrate_to(cx, 7)
        stage_log_v7 = _table_schema(cx, "stage_log")
        planning_runs_v7 = _table_schema(cx, "planning_runs")

        migrations.apply_at_boot(cx)
        assert _table_schema(cx, "stage_log") == stage_log_v7
        assert _table_schema(cx, "planning_runs") == planning_runs_v7
    finally:
        cx.close()


def test_v7_upgrade_adds_column_old_rows_read_null(tmp_path: Path) -> None:
    """A build queued before the column reads back NULL, not a guess."""
    cx = sqlite_connect.connect_writer(tmp_path / "legacy.db")
    try:
        _migrate_to(cx, 7)
        assert "task_id" not in _column_names(cx, "builds")
        _insert_build(cx, "legacy-1")
        cx.commit()

        assert migrations.apply_at_boot(cx) >= 8
        assert "task_id" in _column_names(cx, "builds")
        row = cx.execute(
            "SELECT task_id FROM builds WHERE build_id='legacy-1'"
        ).fetchone()
        assert row is not None and row[0] is None
    finally:
        cx.close()


def test_fresh_and_upgraded_builds_columns_converge(tmp_path: Path) -> None:
    fresh = sqlite_connect.connect_writer(tmp_path / "fresh.db")
    staged = sqlite_connect.connect_writer(tmp_path / "staged.db")
    try:
        migrations.apply_at_boot(fresh)
        _migrate_to(staged, 7)
        migrations.apply_at_boot(staged)

        assert _column_names(fresh, "builds") == _column_names(staged, "builds")
        for cx in (fresh, staged):
            assert (
                cx.execute(
                    "SELECT COUNT(*) FROM schema_version WHERE version=8"
                ).fetchone()[0]
                == 1
            )
    finally:
        fresh.close()
        staged.close()


def test_v8_migration_is_idempotent(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "idem.db")
    try:
        migrations.apply_at_boot(cx)
        _insert_build(cx, "keep", task_id="TASK-FIX007")
        cx.commit()

        migrations.apply_at_boot(cx)
        row = cx.execute(
            "SELECT task_id FROM builds WHERE build_id='keep'"
        ).fetchone()
        assert row is not None and row[0] == "TASK-FIX007"
        assert (
            cx.execute(
                "SELECT COUNT(*) FROM schema_version WHERE version=8"
            ).fetchone()[0]
            == 1
        )
    finally:
        cx.close()

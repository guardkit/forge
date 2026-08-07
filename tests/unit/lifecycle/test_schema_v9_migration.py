"""Tests for schema v9 — ``builds.terminal_class``, the timeout truth.

Monitored-supervision lane, stage 1.

``builds.status`` spells FIVE structurally different deaths the same way:
``FAILED``. A build the semantic monitor killed for going quiet, one killed at
its budget wall-clock cap, one whose runner-side clock expired, one whose
guardkit SDK call timed out in-band, and one that simply did not compile were
indistinguishable to everything downstream — the only carrier of the difference
was free-form prose in ``builds.error``. "It ran out of time" and "it is
broken" are opposite verdicts with opposite next actions.

Same discipline as the v6 / v7 / v8 migration suites: fresh migrates, additive,
in-place upgrade reads NULL, fresh and upgraded converge, idempotent. Plus the
one this column owes specifically: it must NOT touch ``status``.
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
    cx: sqlite3.Connection,
    build_id: str,
    *,
    terminal_class: str | None = None,
    status: str = "QUEUED",
) -> None:
    if terminal_class is None:
        cx.execute(
            "INSERT INTO builds (build_id, feature_id, repo, branch, "
            "feature_yaml_path, status, triggered_by, correlation_id, queued_at) "
            "VALUES (?, 'FEAT-1', 'r', 'main', 'f.yaml', ?, 'cli', ?, "
            "'2026-01-01T00:00:00Z')",
            (build_id, status, f"corr-{build_id}"),
        )
    else:
        cx.execute(
            "INSERT INTO builds (build_id, feature_id, repo, branch, "
            "feature_yaml_path, status, triggered_by, correlation_id, queued_at, "
            "terminal_class) VALUES (?, 'FEAT-1', 'r', 'main', 'f.yaml', ?, "
            "'cli', ?, '2026-01-01T00:00:00Z', ?)",
            (build_id, status, f"corr-{build_id}", terminal_class),
        )


def _migrate_to(cx: sqlite3.Connection, version: int) -> None:
    original = migrations._MIGRATIONS
    migrations._MIGRATIONS = tuple(m for m in original if m[0] <= version)
    try:
        migrations.apply_at_boot(cx)
    finally:
        migrations._MIGRATIONS = original


def test_fresh_db_has_terminal_class_column(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "fresh.db")
    try:
        assert migrations.apply_at_boot(cx) >= 9
        assert "terminal_class" in _column_names(cx, "builds")
    finally:
        cx.close()


def test_terminal_class_accepts_a_value_and_null(tmp_path: Path) -> None:
    """NULL is 'not classified' — every ordinary failure stays there."""
    cx = sqlite_connect.connect_writer(tmp_path / "fresh.db")
    try:
        migrations.apply_at_boot(cx)
        _insert_build(cx, "b-wedged", terminal_class="timeout-wedge")
        _insert_build(cx, "b-broken")
        cx.commit()
        rows = dict(
            cx.execute("SELECT build_id, terminal_class FROM builds").fetchall()
        )
        assert rows["b-wedged"] == "timeout-wedge"
        assert rows["b-broken"] is None
    finally:
        cx.close()


def test_the_status_vocabulary_is_untouched(tmp_path: Path) -> None:
    """THE LAW OF THIS LANE. A timeout is still a FAILED build.

    The distinction rides beside ``status``, never inside it — so every
    existing reader of ``status`` (jarvis cards, the CLI, the state machine)
    keeps its exact behaviour.
    """
    cx = sqlite_connect.connect_writer(tmp_path / "fresh.db")
    try:
        before = _table_schema(cx, "builds")  # empty — table not yet created
        assert before == ""
        migrations.apply_at_boot(cx)
        _insert_build(
            cx, "b-timeout", terminal_class="timeout-budget-cap", status="FAILED"
        )
        cx.commit()
        row = cx.execute(
            "SELECT status, terminal_class FROM builds WHERE build_id='b-timeout'"
        ).fetchone()
        assert row[0] == "FAILED", (
            "a timeout must NOT mint a new status value — the whole additive "
            "claim of this lane rests on this"
        )
        assert row[1] == "timeout-budget-cap"
    finally:
        cx.close()


def test_v9_is_additive_leaves_other_tables_unchanged(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "staged.db")
    try:
        _migrate_to(cx, 8)
        stage_log_v8 = _table_schema(cx, "stage_log")
        planning_runs_v8 = _table_schema(cx, "planning_runs")

        migrations.apply_at_boot(cx)
        assert _table_schema(cx, "stage_log") == stage_log_v8
        assert _table_schema(cx, "planning_runs") == planning_runs_v8
    finally:
        cx.close()


def test_v8_upgrade_adds_column_old_rows_read_null(tmp_path: Path) -> None:
    """A build that failed before the column reads back NULL, not a guess.

    NULL means "nothing classified this", which is honestly weaker than "this
    was not a timeout" — historical FAILED rows are exactly the ones forge
    cannot retroactively tell apart, and the column says so by saying nothing.
    """
    cx = sqlite_connect.connect_writer(tmp_path / "legacy.db")
    try:
        _migrate_to(cx, 8)
        assert "terminal_class" not in _column_names(cx, "builds")
        _insert_build(cx, "legacy-1", status="FAILED")
        cx.commit()

        assert migrations.apply_at_boot(cx) >= 9
        assert "terminal_class" in _column_names(cx, "builds")
        row = cx.execute(
            "SELECT status, terminal_class FROM builds WHERE build_id='legacy-1'"
        ).fetchone()
        assert row is not None
        assert row[0] == "FAILED", "the upgrade must not rewrite a single status"
        assert row[1] is None
    finally:
        cx.close()


def test_fresh_and_upgraded_builds_columns_converge(tmp_path: Path) -> None:
    fresh = sqlite_connect.connect_writer(tmp_path / "fresh.db")
    staged = sqlite_connect.connect_writer(tmp_path / "staged.db")
    try:
        migrations.apply_at_boot(fresh)
        _migrate_to(staged, 8)
        migrations.apply_at_boot(staged)

        assert _column_names(fresh, "builds") == _column_names(staged, "builds")
        for cx in (fresh, staged):
            assert (
                cx.execute(
                    "SELECT COUNT(*) FROM schema_version WHERE version=9"
                ).fetchone()[0]
                == 1
            )
    finally:
        fresh.close()
        staged.close()


def test_v9_migration_is_idempotent(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "idem.db")
    try:
        migrations.apply_at_boot(cx)
        _insert_build(cx, "keep", terminal_class="timeout-in-band")
        cx.commit()

        migrations.apply_at_boot(cx)
        row = cx.execute(
            "SELECT terminal_class FROM builds WHERE build_id='keep'"
        ).fetchone()
        assert row is not None and row[0] == "timeout-in-band"
        assert (
            cx.execute(
                "SELECT COUNT(*) FROM schema_version WHERE version=9"
            ).fetchone()[0]
            == 1
        )
    finally:
        cx.close()

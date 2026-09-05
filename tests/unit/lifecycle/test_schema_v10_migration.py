"""Tests for schema v10 — ``work_queue`` and ``work_queue_events``.

The work-queue lane (Lane B stage one). Today a sentence that passes the
intake's six gates becomes a planning run immediately; the only thing keeping
two sentences from racing is a broker setting nobody can read. These two
tables are the list a person can actually read and reorder: a sentence is
filed here first, and a take-next loop creates the planning run later with the
same correlation id.

Same discipline as the v6 / v7 / v8 / v9 migration suites: fresh migrates,
additive, in-place upgrade, fresh and upgraded converge, idempotent. Plus the
three this schema owes specifically: the closed status and kind vocabularies,
the one-row-per-correlation-id rule, and that dropping a row keeps it.
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


def _column_names(cx: sqlite3.Connection, table_name: str) -> list[str]:
    return [r[1] for r in cx.execute(f"PRAGMA table_info({table_name})").fetchall()]


def _migrate_to(cx: sqlite3.Connection, version: int) -> None:
    original = migrations._MIGRATIONS
    migrations._MIGRATIONS = tuple(m for m in original if m[0] <= version)
    try:
        migrations.apply_at_boot(cx)
    finally:
        migrations._MIGRATIONS = original


def _insert_row(
    cx: sqlite3.Connection,
    *,
    correlation_id: str = "plan-1",
    kind: str = "feature",
    status: str = "QUEUED",
    rank: float = 1.0,
) -> int:
    cursor = cx.execute(
        """
        INSERT INTO work_queue (
            sentence, target_repo, kind, status, rank, originating_user,
            correlation_id, queued_at
        ) VALUES ('build a thing', 'api_test', ?, ?, ?, 'U123', ?,
                  '2026-09-05T09:00:00+00:00')
        """,
        (kind, status, rank, correlation_id),
    )
    return int(cursor.lastrowid or 0)


def test_fresh_db_has_both_work_queue_tables(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "fresh.db")
    try:
        assert migrations.apply_at_boot(cx) >= 10
        assert _table_schema(cx, "work_queue") != ""
        assert _table_schema(cx, "work_queue_events") != ""
    finally:
        cx.close()


def test_work_queue_columns_are_the_ones_the_spec_names(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "fresh.db")
    try:
        migrations.apply_at_boot(cx)
        assert _column_names(cx, "work_queue") == [
            "id",
            "sentence",
            "target_repo",
            "kind",
            "status",
            "rank",
            "after_id",
            "originating_user",
            "correlation_id",
            "queued_at",
            "admitted_at",
            "closed_at",
            "stale_pinged_at",
            "keep_count",
            "closed_reason",
        ]
        assert _column_names(cx, "work_queue_events") == [
            "id",
            "queue_id",
            "action",
            "actor_identity",
            "details_json",
            "recorded_at",
        ]
    finally:
        cx.close()


def test_the_open_index_exists(tmp_path: Path) -> None:
    """The queue is read open-rows-in-order on every tick; that read is indexed."""
    cx = sqlite_connect.connect_writer(tmp_path / "fresh.db")
    try:
        migrations.apply_at_boot(cx)
        names = [
            r[0]
            for r in cx.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        ]
        assert "idx_work_queue_open" in names
    finally:
        cx.close()


@pytest.mark.parametrize("kind", ["feature", "fix", "question"])
def test_the_three_kinds_are_accepted(tmp_path: Path, kind: str) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "fresh.db")
    try:
        migrations.apply_at_boot(cx)
        _insert_row(cx, correlation_id=f"plan-{kind}", kind=kind)
        cx.commit()
    finally:
        cx.close()


def test_a_kind_outside_the_vocabulary_is_refused(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "fresh.db")
    try:
        migrations.apply_at_boot(cx)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_row(cx, kind="chore")
    finally:
        cx.close()


def test_a_status_outside_the_vocabulary_is_refused(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "fresh.db")
    try:
        migrations.apply_at_boot(cx)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_row(cx, status="PENDING")
    finally:
        cx.close()


def test_one_row_per_correlation_id(tmp_path: Path) -> None:
    """The idempotency key. A redelivered message files one row, never two."""
    cx = sqlite_connect.connect_writer(tmp_path / "fresh.db")
    try:
        migrations.apply_at_boot(cx)
        _insert_row(cx, correlation_id="plan-same")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_row(cx, correlation_id="plan-same", rank=2.0)
    finally:
        cx.close()


def test_a_withdrawn_row_is_still_there(tmp_path: Path) -> None:
    """Dropping a row closes it. The record of what was asked for survives."""
    cx = sqlite_connect.connect_writer(tmp_path / "fresh.db")
    try:
        migrations.apply_at_boot(cx)
        queue_id = _insert_row(cx)
        cx.execute(
            "UPDATE work_queue SET status='WITHDRAWN', closed_at='2026-09-05T10:00:00+00:00' "
            "WHERE id = ?",
            (queue_id,),
        )
        cx.commit()
        row = cx.execute(
            "SELECT sentence, status FROM work_queue WHERE id = ?", (queue_id,)
        ).fetchone()
        assert row is not None
        assert row[0] == "build a thing"
        assert row[1] == "WITHDRAWN"
    finally:
        cx.close()


def test_an_event_needs_a_row_to_belong_to(tmp_path: Path) -> None:
    """``work_queue_events.queue_id`` is a real foreign key (pragma is ON)."""
    cx = sqlite_connect.connect_writer(tmp_path / "fresh.db")
    try:
        migrations.apply_at_boot(cx)
        with pytest.raises(sqlite3.IntegrityError):
            cx.execute(
                "INSERT INTO work_queue_events (queue_id, action, actor_identity, "
                "recorded_at) VALUES (999, 'promote', 'U123', '2026-09-05T10:00:00Z')"
            )
            cx.commit()
    finally:
        cx.close()


def test_keep_count_defaults_to_zero(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "fresh.db")
    try:
        migrations.apply_at_boot(cx)
        queue_id = _insert_row(cx)
        cx.commit()
        row = cx.execute(
            "SELECT keep_count FROM work_queue WHERE id = ?", (queue_id,)
        ).fetchone()
        assert row[0] == 0
    finally:
        cx.close()


def test_v10_is_additive_leaves_other_tables_unchanged(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "staged.db")
    try:
        _migrate_to(cx, 9)
        builds_v9 = _table_schema(cx, "builds")
        planning_runs_v9 = _table_schema(cx, "planning_runs")
        stage_log_v9 = _table_schema(cx, "stage_log")

        migrations.apply_at_boot(cx)
        assert _table_schema(cx, "builds") == builds_v9
        assert _table_schema(cx, "planning_runs") == planning_runs_v9
        assert _table_schema(cx, "stage_log") == stage_log_v9
    finally:
        cx.close()


def test_v9_upgrade_adds_the_tables_and_keeps_the_rows(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "legacy.db")
    try:
        _migrate_to(cx, 9)
        assert _table_schema(cx, "work_queue") == ""
        cx.execute(
            "INSERT INTO planning_runs (correlation_id, state, originating_user, "
            "expected_approver, request_text, triggered_by, queued_at) VALUES "
            "('plan-old', 'QUEUED', 'U1', 'U1', 'old sentence', 'jarvis', "
            "'2026-09-01T00:00:00Z')"
        )
        cx.commit()

        assert migrations.apply_at_boot(cx) >= 10
        assert _table_schema(cx, "work_queue") != ""
        row = cx.execute(
            "SELECT state FROM planning_runs WHERE correlation_id='plan-old'"
        ).fetchone()
        assert row is not None and row[0] == "QUEUED"
    finally:
        cx.close()


def test_fresh_and_upgraded_converge(tmp_path: Path) -> None:
    fresh = sqlite_connect.connect_writer(tmp_path / "fresh.db")
    staged = sqlite_connect.connect_writer(tmp_path / "staged.db")
    try:
        migrations.apply_at_boot(fresh)
        _migrate_to(staged, 9)
        migrations.apply_at_boot(staged)

        assert _column_names(fresh, "work_queue") == _column_names(staged, "work_queue")
        for cx in (fresh, staged):
            assert (
                cx.execute(
                    "SELECT COUNT(*) FROM schema_version WHERE version=10"
                ).fetchone()[0]
                == 1
            )
    finally:
        fresh.close()
        staged.close()


def test_v10_migration_is_idempotent(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "idem.db")
    try:
        migrations.apply_at_boot(cx)
        queue_id = _insert_row(cx, correlation_id="plan-keep")
        cx.commit()

        migrations.apply_at_boot(cx)
        row = cx.execute(
            "SELECT sentence FROM work_queue WHERE id = ?", (queue_id,)
        ).fetchone()
        assert row is not None and row[0] == "build a thing"
        assert (
            cx.execute(
                "SELECT COUNT(*) FROM schema_version WHERE version=10"
            ).fetchone()[0]
            == 1
        )
    finally:
        cx.close()

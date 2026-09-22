"""The ledger writes down where a piece of work started (one true copy, item 1).

Two columns, ``start_commit`` and ``target_branch``, on the planning run and
on the build it becomes. Everything here runs against a THROWAWAY database in
a temporary directory, made by Forge's own migration code — the live ledger is
neither read nor written.

The point these tests hold down: a row with nothing recorded reads as "not
recorded", and never as a guess at where the work started.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from nats_core.events import BuildQueuedPayload

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations as lifecycle_migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.states import PlanningState

COMMIT = "4f1c0d2b" + "0" * 32
CID = "corr-start-0001"


@pytest.fixture()
def persistence(tmp_path: Path) -> Iterator[SqliteLifecyclePersistence]:
    db_path = tmp_path / "forge.db"
    cx = sqlite_connect.connect_writer(db_path)
    lifecycle_migrations.apply_at_boot(cx)
    try:
        yield SqliteLifecyclePersistence(connection=cx, db_path=db_path)
    finally:
        cx.close()


def _store(persistence: SqliteLifecyclePersistence) -> SqlitePlanningRunStore:
    persistence.connection.row_factory = sqlite3.Row
    return SqlitePlanningRunStore(persistence.connection)


def _queue_run(store: SqlitePlanningRunStore, correlation_id: str = CID) -> None:
    store.record_queued(
        correlation_id=correlation_id,
        originating_user="U1",
        expected_approver="U1",
        request_text="a sentence",
        triggered_by="cli",
        target_repo="guardkit/api_test",
    )


def _queue_build(
    persistence: SqliteLifecyclePersistence, correlation_id: str = CID
) -> str:
    now = datetime.now(UTC)
    payload = BuildQueuedPayload(
        feature_id="FEAT-STRT",
        repo="guardkit/api_test",
        feature_yaml_path=".guardkit/features/FEAT-STRT.yaml",
        triggered_by="forge-internal",
        correlation_id=correlation_id,
        requested_at=now,
        queued_at=now,
    )
    return persistence.record_pending_build(payload)


# ---------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------


def test_the_migration_adds_the_two_columns_to_both_tables(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "fresh.db")
    try:
        version = lifecycle_migrations.apply_at_boot(cx)
        planning = {row[1] for row in cx.execute("PRAGMA table_info(planning_runs);")}
        builds = {row[1] for row in cx.execute("PRAGMA table_info(builds);")}
    finally:
        cx.close()

    assert version >= 12
    assert {"start_commit", "target_branch"} <= planning
    assert {"start_commit", "target_branch"} <= builds


def test_an_existing_ledger_is_upgraded_in_place_and_keeps_its_rows(
    tmp_path: Path,
) -> None:
    """A database made by the migrations BEFORE this one, then upgraded: the
    columns arrive, the rows that were there stay, and running it again is a
    no-op."""
    db_path = tmp_path / "older.db"
    cx = sqlite_connect.connect_writer(db_path)
    try:
        # Apply every migration up to the one before the starting rule.
        before = [m for m in lifecycle_migrations._MIGRATIONS if m[0] <= 11]
        with cx:
            for _version, filename in before:
                cx.executescript(lifecycle_migrations._load_migration_sql(filename))
        cx.row_factory = sqlite3.Row
        store = SqlitePlanningRunStore(cx)
        _queue_run(store, "corr-older")
        assert "start_commit" not in {
            row[1] for row in cx.execute("PRAGMA table_info(planning_runs);")
        }

        version = lifecycle_migrations.apply_at_boot(cx)
        again = lifecycle_migrations.apply_at_boot(cx)

        assert version == again == lifecycle_migrations._SCHEMA_VERSION
        assert {"start_commit", "target_branch"} <= {
            row[1] for row in cx.execute("PRAGMA table_info(planning_runs);")
        }
        kept = cx.execute(
            "SELECT state, start_commit FROM planning_runs WHERE correlation_id = ?",
            ("corr-older",),
        ).fetchone()
        assert kept["state"] == PlanningState.QUEUED.value
        assert kept["start_commit"] is None
    finally:
        cx.close()


# ---------------------------------------------------------------------------
# Writing it down, and carrying it onto the build
# ---------------------------------------------------------------------------


def test_the_planning_run_holds_the_commit_and_the_branch(
    persistence: SqliteLifecyclePersistence,
) -> None:
    store = _store(persistence)
    _queue_run(store)

    assert store.record_start_point(CID, start_commit=COMMIT, target_branch="main")

    assert store.get_start_point(CID) == (COMMIT, "main")


def test_recording_the_start_point_changes_nothing_else_about_the_run(
    persistence: SqliteLifecyclePersistence,
) -> None:
    store = _store(persistence)
    _queue_run(store)
    before = dict(store.get_run(CID))

    store.record_start_point(CID, start_commit=COMMIT, target_branch="main")

    after = dict(store.get_run(CID))
    changed = {k for k in after if before.get(k) != after[k]}
    assert changed == {"start_commit", "target_branch"}


@pytest.mark.parametrize(
    ("commit", "branch"),
    [("", "main"), ("   ", "main"), (COMMIT, ""), (COMMIT, "  ")],
)
def test_a_blank_start_point_is_refused_rather_than_written(
    persistence: SqliteLifecyclePersistence, commit: str, branch: str
) -> None:
    """A blank reads back exactly like the NULL that means "not recorded"."""
    store = _store(persistence)
    _queue_run(store)

    with pytest.raises(ValueError):
        store.record_start_point(CID, start_commit=commit, target_branch=branch)


def test_the_build_is_given_the_runs_start_point_when_it_is_dispatched(
    persistence: SqliteLifecyclePersistence,
) -> None:
    store = _store(persistence)
    _queue_run(store)
    store.record_start_point(CID, start_commit=COMMIT, target_branch="main")

    build_id = _queue_build(persistence)

    row = persistence.get_build_row(build_id)
    assert row is not None
    assert row.start_commit == COMMIT
    assert row.target_branch == "main"
    start = persistence.read_start_point(build_id)
    assert start.recorded is True
    assert start.sentence == f"{COMMIT} on main"


def test_the_builds_own_branch_column_is_left_alone(
    persistence: SqliteLifecyclePersistence,
) -> None:
    """``branch`` is the branch the build was queued ON — a different fact."""
    store = _store(persistence)
    _queue_run(store)
    store.record_start_point(CID, start_commit=COMMIT, target_branch="main")

    build_id = _queue_build(persistence)

    row = persistence.get_build_row(build_id)
    assert row is not None
    assert row.branch != row.target_branch or row.branch == "main"
    assert row.target_branch == "main"


# ---------------------------------------------------------------------------
# Nothing recorded reads as nothing recorded
# ---------------------------------------------------------------------------


def test_a_build_with_no_planning_run_reads_as_not_recorded(
    persistence: SqliteLifecyclePersistence,
) -> None:
    build_id = _queue_build(persistence, "corr-hand-queued")

    start = persistence.read_start_point(build_id)

    assert start.recorded is False
    assert start.start_commit is None and start.target_branch is None
    assert start.sentence == "not recorded"


def test_a_run_that_started_before_the_rule_reads_as_not_recorded(
    persistence: SqliteLifecyclePersistence,
) -> None:
    store = _store(persistence)
    _queue_run(store)

    assert store.get_start_point(CID) == (None, None)


def test_a_pre_existing_build_row_reads_as_not_recorded(
    persistence: SqliteLifecyclePersistence,
) -> None:
    """A row written before the columns existed has NULLs in them, and the
    reader says so rather than filling them in from somewhere else."""
    build_id = _queue_build(persistence)
    persistence.connection.execute(
        "UPDATE builds SET start_commit = NULL, target_branch = NULL "
        "WHERE build_id = ?",
        (build_id,),
    )
    persistence.connection.commit()

    start = persistence.read_start_point(build_id)
    row = persistence.get_build_row(build_id)

    assert start.recorded is False
    assert start.sentence == "not recorded"
    assert row is not None and row.start_commit is None


def test_a_build_that_is_not_there_reads_as_not_recorded(
    persistence: SqliteLifecyclePersistence,
) -> None:
    start = persistence.read_start_point("no-such-build")

    assert start.recorded is False
    assert start.sentence == "not recorded"

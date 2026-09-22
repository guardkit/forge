"""The ledger writes down which memory a piece of work belongs to (item 2).

One column, ``memory_project``, on the planning run and on the build it becomes.
Everything here runs against a THROWAWAY database in a temporary directory, made
by Forge's own migration code — the live ledger is neither read nor written, and
no memory service, database, embedder or broker is contacted by anything here.

The point these tests hold down: a row with nothing recorded reads as "not
recorded", and never as "guardkit".
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

CID = "corr-memory-0001"


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
        feature_id="FEAT-MEM",
        repo="guardkit/api_test",
        feature_yaml_path=".guardkit/features/FEAT-MEM.yaml",
        triggered_by="forge-internal",
        correlation_id=correlation_id,
        requested_at=now,
        queued_at=now,
    )
    return persistence.record_pending_build(payload)


# ---------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------


def test_the_migration_adds_the_column_to_both_tables(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "fresh.db")
    try:
        version = lifecycle_migrations.apply_at_boot(cx)
        planning = {row[1] for row in cx.execute("PRAGMA table_info(planning_runs);")}
        builds = {row[1] for row in cx.execute("PRAGMA table_info(builds);")}
    finally:
        cx.close()

    assert version >= 13
    assert "memory_project" in planning
    assert "memory_project" in builds


def test_an_old_shaped_database_is_upgraded_in_place_and_keeps_its_rows(
    tmp_path: Path,
) -> None:
    """A database made by the migrations BEFORE this one, then upgraded: the
    column arrives, the rows that were there stay with nothing recorded, and
    running it again is a no-op."""
    db_path = tmp_path / "older.db"
    cx = sqlite_connect.connect_writer(db_path)
    try:
        before = [m for m in lifecycle_migrations._MIGRATIONS if m[0] <= 12]
        with cx:
            for _version, filename in before:
                cx.executescript(lifecycle_migrations._load_migration_sql(filename))
        cx.row_factory = sqlite3.Row
        store = SqlitePlanningRunStore(cx)
        _queue_run(store, "corr-older")
        assert "memory_project" not in {
            row[1] for row in cx.execute("PRAGMA table_info(planning_runs);")
        }

        version = lifecycle_migrations.apply_at_boot(cx)
        again = lifecycle_migrations.apply_at_boot(cx)

        assert version == again == lifecycle_migrations._SCHEMA_VERSION
        assert "memory_project" in {
            row[1] for row in cx.execute("PRAGMA table_info(planning_runs);")
        }
        kept = cx.execute(
            "SELECT state, memory_project FROM planning_runs WHERE correlation_id = ?",
            ("corr-older",),
        ).fetchone()
        assert kept["state"] == PlanningState.QUEUED.value
        assert kept["memory_project"] is None
    finally:
        cx.close()


def test_a_ledger_that_was_never_migrated_still_takes_new_builds(
    tmp_path: Path,
) -> None:
    """An old-shaped ledger has no column. A build queued against it is still
    written, and reads back as "not recorded" — which is the truth about it."""
    db_path = tmp_path / "unmigrated.db"
    cx = sqlite_connect.connect_writer(db_path)
    try:
        before = [m for m in lifecycle_migrations._MIGRATIONS if m[0] <= 12]
        with cx:
            for _version, filename in before:
                cx.executescript(lifecycle_migrations._load_migration_sql(filename))
        cx.row_factory = sqlite3.Row
        persistence = SqliteLifecyclePersistence(connection=cx, db_path=db_path)
        store = SqlitePlanningRunStore(cx)
        _queue_run(store, "corr-unmigrated")

        build_id = _queue_build(persistence, "corr-unmigrated")

        assert build_id
        assert persistence.read_memory_project(build_id) is None
    finally:
        cx.close()


# ---------------------------------------------------------------------------
# Writing it down, and carrying it onto the build
# ---------------------------------------------------------------------------


def test_the_planning_run_holds_the_name(
    persistence: SqliteLifecyclePersistence,
) -> None:
    store = _store(persistence)
    _queue_run(store)

    assert store.record_memory_project(CID, memory_project="widget_shop") is True
    assert store.get_memory_project(CID) == "widget_shop"


def test_a_run_with_nothing_recorded_reads_as_not_recorded(
    persistence: SqliteLifecyclePersistence,
) -> None:
    store = _store(persistence)
    _queue_run(store)

    assert store.get_memory_project(CID) is None


def test_the_name_is_trimmed_and_a_blank_one_is_refused(
    persistence: SqliteLifecyclePersistence,
) -> None:
    store = _store(persistence)
    _queue_run(store)

    store.record_memory_project(CID, memory_project="  widget_shop  ")
    assert store.get_memory_project(CID) == "widget_shop"

    with pytest.raises(ValueError):
        store.record_memory_project(CID, memory_project="   ")
    with pytest.raises(ValueError):
        store.record_memory_project("", memory_project="widget_shop")


def test_recording_the_name_does_not_move_the_runs_state(
    persistence: SqliteLifecyclePersistence,
) -> None:
    store = _store(persistence)
    _queue_run(store)
    before = persistence.connection.execute(
        "SELECT state FROM planning_runs WHERE correlation_id = ?", (CID,)
    ).fetchone()["state"]

    store.record_memory_project(CID, memory_project="widget_shop")

    after = persistence.connection.execute(
        "SELECT state FROM planning_runs WHERE correlation_id = ?", (CID,)
    ).fetchone()["state"]
    assert after == before


def test_the_build_carries_the_name_its_planning_run_recorded(
    persistence: SqliteLifecyclePersistence,
) -> None:
    store = _store(persistence)
    _queue_run(store)
    store.record_memory_project(CID, memory_project="widget_shop")

    build_id = _queue_build(persistence)

    assert persistence.read_memory_project(build_id) == "widget_shop"
    row = persistence.get_build_row(build_id)
    assert row is not None and row.memory_project == "widget_shop"


def test_a_build_with_no_planning_run_records_nothing(
    persistence: SqliteLifecyclePersistence,
) -> None:
    build_id = _queue_build(persistence, "corr-no-such-run")

    assert persistence.read_memory_project(build_id) is None
    row = persistence.get_build_row(build_id)
    assert row is not None and row.memory_project is None


def test_a_build_nobody_ever_queued_reads_as_not_recorded(
    persistence: SqliteLifecyclePersistence,
) -> None:
    assert persistence.read_memory_project("build-that-does-not-exist") is None


def test_nothing_recorded_is_never_turned_into_guardkit(
    persistence: SqliteLifecyclePersistence,
) -> None:
    """The one thing this column exists to stop: an unrecorded row reading as
    the name every project's outcomes used to be filed under."""
    store = _store(persistence)
    _queue_run(store)

    build_id = _queue_build(persistence)

    assert persistence.read_memory_project(build_id) is None
    assert persistence.read_memory_project(build_id) != "guardkit"

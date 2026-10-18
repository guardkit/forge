"""A repair build is recorded with what the build it repairs was recorded with.

Design "One sandbox per project", section 2.4a (18 October 2026): a repair's
correlation id is ``fix-<the build it repairs>`` and no planning run is filed
under it, so it used to be recorded with no memory name and no start commit,
and ran with no memory at all. It now copies the memory name, start commit and
launch settings of the build it repairs, but only when that build is in the
ledger and is of the same repository (Codex R1). Otherwise nothing is copied,
from anywhere.

Every test runs against a throwaway ledger made by Forge's own migrations; no
memory service is contacted. All names are invented.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from nats_core.events import BuildQueuedPayload

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations as lifecycle_migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.pipeline.fix_row_producer import fix_correlation_id
from forge.planning.run_store import SqlitePlanningRunStore

REPO = "acme/shop-a"
OTHER_REPO = "acme/shop-b"
COMMIT = "c0ffee" + "0" * 34
QUEUED = datetime(2026, 10, 18, 9, 0, 0, tzinfo=UTC)


@pytest.fixture()
def persistence(tmp_path: Path) -> Iterator[SqliteLifecyclePersistence]:
    db_path = tmp_path / "forge.db"
    cx = sqlite_connect.connect_writer(db_path)
    lifecycle_migrations.apply_at_boot(cx)
    try:
        yield SqliteLifecyclePersistence(connection=cx, db_path=db_path)
    finally:
        cx.close()


def _queue(
    persistence: SqliteLifecyclePersistence,
    *,
    repo: str,
    correlation_id: str,
    feature_id: str = "FEAT-SHOP",
    minute: int = 0,
) -> str:
    at = QUEUED + timedelta(minutes=minute)
    return persistence.record_pending_build(
        BuildQueuedPayload(
            feature_id=feature_id,
            repo=repo,
            feature_yaml_path=f".guardkit/features/{feature_id}.yaml",
            triggered_by="forge-internal",
            correlation_id=correlation_id,
            requested_at=at,
            queued_at=at,
        )
    )


def _source_build(persistence: SqliteLifecyclePersistence, *, repo: str = REPO) -> str:
    """A planned build recorded with a memory name, start point and settings."""
    persistence.connection.row_factory = sqlite3.Row
    runs = SqlitePlanningRunStore(persistence.connection)
    runs.record_queued(
        correlation_id="corr-source",
        originating_user="U1",
        expected_approver="U1",
        request_text="a sentence",
        triggered_by="cli",
        target_repo=repo,
    )
    runs.record_memory_project("corr-source", memory_project="shop_a")
    runs.record_start_point("corr-source", start_commit=COMMIT, target_branch="main")
    runs.record_launch_settings("corr-source", names=["SHOP_TOOL_HOME"])
    return _queue(persistence, repo=repo, correlation_id="corr-source")


def test_a_repair_of_a_build_takes_its_memory_name_start_commit_and_settings(
    persistence: SqliteLifecyclePersistence,
) -> None:
    source = _source_build(persistence)

    repair = _queue(persistence, repo=REPO, correlation_id=fix_correlation_id(source), minute=5)

    assert persistence.read_memory_project(repair) == "shop_a"
    row = persistence.get_build_row(repair)
    assert row is not None and row.start_commit == COMMIT
    assert persistence.read_launch_settings(repair) == ("SHOP_TOOL_HOME",)


def test_a_repair_naming_another_repositorys_build_takes_nothing(
    persistence: SqliteLifecyclePersistence,
) -> None:
    source = _source_build(persistence, repo=REPO)

    repair = _queue(
        persistence, repo=OTHER_REPO, correlation_id=fix_correlation_id(source), minute=5
    )

    assert persistence.read_memory_project(repair) is None
    row = persistence.get_build_row(repair)
    assert row is not None and row.start_commit is None
    assert persistence.read_launch_settings(repair) == ()


def test_a_repair_naming_a_build_that_does_not_exist_takes_nothing(
    persistence: SqliteLifecyclePersistence,
) -> None:
    _source_build(persistence)

    repair = _queue(
        persistence,
        repo=REPO,
        correlation_id=fix_correlation_id("build-FEAT-NONE-20261018000000"),
        minute=5,
    )

    assert persistence.read_memory_project(repair) is None
    row = persistence.get_build_row(repair)
    assert row is not None and row.start_commit is None
    assert persistence.read_launch_settings(repair) == ()


def test_a_repair_of_a_build_with_nothing_recorded_stays_unrecorded(
    persistence: SqliteLifecyclePersistence,
) -> None:
    """The source was hand-queued with no planning run: nothing to copy, and
    nothing is made up (never 'guardkit')."""
    source = _queue(persistence, repo=REPO, correlation_id="corr-hand-queued")

    repair = _queue(persistence, repo=REPO, correlation_id=fix_correlation_id(source), minute=5)

    assert persistence.read_memory_project(repair) is None
    assert persistence.read_launch_settings(repair) == ()


def test_an_ordinary_build_is_unchanged(persistence: SqliteLifecyclePersistence) -> None:
    source = _source_build(persistence)
    row = persistence.get_build_row(source)
    assert persistence.read_memory_project(source) == "shop_a"
    assert row is not None and row.start_commit == COMMIT and row.target_branch == "main"

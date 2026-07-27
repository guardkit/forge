"""Persistence tests for the budget-breach store (FEAT-UBS-002 stage 2).

Covers the three ``SqliteLifecyclePersistence`` methods that back schema_v7's
``builds.budget_breach`` column:

* ``record_budget_breach`` — first-write-wins, status-preserving.
* ``read_budget_breach`` — reads the recorded detail back (or ``None``).
* ``latest_breach_for_feature`` — newest breach-carrying build of a feature
  (the stage-3 pre-dispatch gate's reader).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.lifecycle.state_machine import BuildState


@pytest.fixture()
def persistence(tmp_path: Path) -> Iterator[SqliteLifecyclePersistence]:
    db_path = tmp_path / "forge.db"
    cx = sqlite_connect.connect_writer(db_path)
    migrations.apply_at_boot(cx)
    pool = SqliteLifecyclePersistence(connection=cx, db_path=db_path)
    try:
        yield pool
    finally:
        cx.close()


def _insert_build(
    pool: SqliteLifecyclePersistence,
    build_id: str,
    *,
    feature_id: str = "FEAT-1",
    queued_at: str = "2026-01-01T00:00:00Z",
    status: str = "RUNNING",
) -> None:
    pool.connection.execute(
        "INSERT INTO builds (build_id, feature_id, repo, branch, "
        "feature_yaml_path, status, triggered_by, correlation_id, queued_at) "
        "VALUES (?, ?, 'r', 'main', 'f.yaml', ?, 'cli', ?, ?)",
        (build_id, feature_id, status, f"corr-{build_id}", queued_at),
    )
    pool.connection.commit()


def _raw_status(pool: SqliteLifecyclePersistence, build_id: str) -> str:
    row = pool.connection.execute(
        "SELECT status FROM builds WHERE build_id = ?", (build_id,)
    ).fetchone()
    assert row is not None
    return row["status"] if isinstance(row, sqlite3.Row) else row[0]


# ---------------------------------------------------------------------------
# record_budget_breach / read_budget_breach
# ---------------------------------------------------------------------------


def test_record_then_read_round_trips(persistence: SqliteLifecyclePersistence) -> None:
    _insert_build(persistence, "b-1")
    assert persistence.read_budget_breach("b-1") is None

    persistence.record_budget_breach("b-1", "wall_clock: 3712.0s > 3600.0s @ ts")
    assert persistence.read_budget_breach("b-1") == "wall_clock: 3712.0s > 3600.0s @ ts"


def test_record_is_first_write_wins(persistence: SqliteLifecyclePersistence) -> None:
    _insert_build(persistence, "b-1")
    persistence.record_budget_breach("b-1", "wall_clock: first @ ts")
    # A later breach must NOT overwrite the first.
    persistence.record_budget_breach("b-1", "coach_score: second @ ts")
    assert persistence.read_budget_breach("b-1") == "wall_clock: first @ ts"


def test_record_is_status_preserving(persistence: SqliteLifecyclePersistence) -> None:
    _insert_build(persistence, "b-1", status="RUNNING")
    persistence.record_budget_breach("b-1", "wall_clock: x @ ts")
    # The breach record never moves the lifecycle — honesty law of the lane.
    assert _raw_status(persistence, "b-1") == BuildState.RUNNING.value


def test_record_missing_build_is_quiet_noop(
    persistence: SqliteLifecyclePersistence,
) -> None:
    # No matching row → zero rows updated, no raise.
    persistence.record_budget_breach("ghost", "wall_clock: x @ ts")
    assert persistence.read_budget_breach("ghost") is None


def test_read_of_absent_build_is_none(
    persistence: SqliteLifecyclePersistence,
) -> None:
    assert persistence.read_budget_breach("nope") is None


def test_empty_build_id_raises(persistence: SqliteLifecyclePersistence) -> None:
    with pytest.raises(ValueError, match="build_id"):
        persistence.record_budget_breach("", "x")
    with pytest.raises(ValueError, match="build_id"):
        persistence.read_budget_breach("")


# ---------------------------------------------------------------------------
# latest_breach_for_feature (stage-3 pre-dispatch gate reader)
# ---------------------------------------------------------------------------


def test_latest_breach_for_feature_none_when_clean(
    persistence: SqliteLifecyclePersistence,
) -> None:
    _insert_build(persistence, "b-1", feature_id="FEAT-A")
    assert persistence.latest_breach_for_feature("FEAT-A") is None


def test_latest_breach_for_feature_returns_newest(
    persistence: SqliteLifecyclePersistence,
) -> None:
    # Two builds of the same feature; the newer one (by queued_at) carries a
    # breach, the older one also does — the reader returns the NEWER.
    _insert_build(
        persistence, "b-old", feature_id="FEAT-A", queued_at="2026-01-01T00:00:00Z"
    )
    _insert_build(
        persistence, "b-new", feature_id="FEAT-A", queued_at="2026-02-01T00:00:00Z"
    )
    persistence.record_budget_breach("b-old", "wall_clock: old @ ts")
    persistence.record_budget_breach("b-new", "coach_score: new @ ts")

    result = persistence.latest_breach_for_feature("FEAT-A")
    assert result == ("b-new", "coach_score: new @ ts")


def test_latest_breach_for_feature_skips_clean_newer(
    persistence: SqliteLifecyclePersistence,
) -> None:
    # The newest build is clean; an older build carries a breach → the reader
    # surfaces the older breach (it is the latest breach-CARRYING build).
    _insert_build(
        persistence, "b-old", feature_id="FEAT-A", queued_at="2026-01-01T00:00:00Z"
    )
    _insert_build(
        persistence, "b-new", feature_id="FEAT-A", queued_at="2026-02-01T00:00:00Z"
    )
    persistence.record_budget_breach("b-old", "wall_clock: old @ ts")

    result = persistence.latest_breach_for_feature("FEAT-A")
    assert result == ("b-old", "wall_clock: old @ ts")


def test_latest_breach_for_feature_scoped_to_feature(
    persistence: SqliteLifecyclePersistence,
) -> None:
    _insert_build(persistence, "b-a", feature_id="FEAT-A")
    _insert_build(persistence, "b-b", feature_id="FEAT-B")
    persistence.record_budget_breach("b-b", "wall_clock: b @ ts")

    assert persistence.latest_breach_for_feature("FEAT-A") is None
    assert persistence.latest_breach_for_feature("FEAT-B") == ("b-b", "wall_clock: b @ ts")


def test_latest_breach_for_feature_empty_id_raises(
    persistence: SqliteLifecyclePersistence,
) -> None:
    with pytest.raises(ValueError, match="feature_id"):
        persistence.latest_breach_for_feature("")


# ---------------------------------------------------------------------------
# clear_budget_breach (stage-3 GATE — history-preserving clear)
# ---------------------------------------------------------------------------


def test_clear_annotates_history_and_hides_from_enforcement(
    persistence: SqliteLifecyclePersistence,
) -> None:
    _insert_build(persistence, "b-1", feature_id="FEAT-A")
    persistence.record_budget_breach("b-1", "wall_clock: 3712.0s > 3600.0s @ ts")
    assert persistence.latest_breach_for_feature("FEAT-A") == (
        "b-1",
        "wall_clock: 3712.0s > 3600.0s @ ts",
    )

    persistence.clear_budget_breach("b-1", "2026-07-27T10:00:00+00:00")

    # History stays in the column (detected record + the clear annotation)...
    detail = persistence.read_budget_breach("b-1")
    assert detail is not None
    assert detail.startswith("wall_clock: 3712.0s > 3600.0s @ ts")
    assert "(cleared @ 2026-07-27T10:00:00+00:00)" in detail
    # ...but a cleared breach is no longer an ACTIVE breach for enforcement.
    assert persistence.latest_breach_for_feature("FEAT-A") is None


def test_clear_is_idempotent(persistence: SqliteLifecyclePersistence) -> None:
    _insert_build(persistence, "b-1", feature_id="FEAT-A")
    persistence.record_budget_breach("b-1", "wall_clock: x @ ts")
    persistence.clear_budget_breach("b-1", "2026-07-27T10:00:00+00:00")
    first = persistence.read_budget_breach("b-1")

    # A redelivered / re-armed approve must not double-append the marker.
    persistence.clear_budget_breach("b-1", "2026-07-27T11:11:11+00:00")
    assert persistence.read_budget_breach("b-1") == first


def test_clear_is_status_preserving(persistence: SqliteLifecyclePersistence) -> None:
    _insert_build(persistence, "b-1", feature_id="FEAT-A", status="RUNNING")
    persistence.record_budget_breach("b-1", "wall_clock: x @ ts")
    persistence.clear_budget_breach("b-1", "2026-07-27T10:00:00+00:00")
    assert _raw_status(persistence, "b-1") == BuildState.RUNNING.value


def test_clear_no_breach_is_quiet_noop(
    persistence: SqliteLifecyclePersistence,
) -> None:
    _insert_build(persistence, "b-1", feature_id="FEAT-A")
    # A build with no breach → zero rows updated, no raise, still clean.
    persistence.clear_budget_breach("b-1", "2026-07-27T10:00:00+00:00")
    assert persistence.read_budget_breach("b-1") is None


def test_clear_missing_build_is_quiet_noop(
    persistence: SqliteLifecyclePersistence,
) -> None:
    persistence.clear_budget_breach("ghost", "2026-07-27T10:00:00+00:00")
    assert persistence.read_budget_breach("ghost") is None


def test_clear_empty_id_raises(persistence: SqliteLifecyclePersistence) -> None:
    with pytest.raises(ValueError, match="build_id"):
        persistence.clear_budget_breach("", "2026-07-27T10:00:00+00:00")


def test_latest_breach_surfaces_older_active_when_newer_cleared(
    persistence: SqliteLifecyclePersistence,
) -> None:
    # Newer build's breach was cleared; an older build still carries an active
    # breach → the reader surfaces the older ACTIVE breach.
    _insert_build(
        persistence, "b-old", feature_id="FEAT-A", queued_at="2026-01-01T00:00:00Z"
    )
    _insert_build(
        persistence, "b-new", feature_id="FEAT-A", queued_at="2026-02-01T00:00:00Z"
    )
    persistence.record_budget_breach("b-old", "wall_clock: old @ ts")
    persistence.record_budget_breach("b-new", "coach_score: new @ ts")
    persistence.clear_budget_breach("b-new", "2026-07-27T10:00:00+00:00")

    assert persistence.latest_breach_for_feature("FEAT-A") == (
        "b-old",
        "wall_clock: old @ ts",
    )

"""The mission record (missions design, 17 October 2026, section 2).

Everything runs against a throwaway ledger made by Forge's own migration code.
What these tests hold down: the tables live outside the migration ladder (the
schema stays 19), the two ``builds`` columns are added only when missing,
links are between keys so a rebuild's dependants follow it, and a replay can
never file a third attempt.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations as lifecycle_migrations
from forge.lifecycle.feature_routing import validate_feature_routing_id
from forge.lifecycle.missions import (
    MissionFeature,
    MissionStore,
    attempt_correlation_id,
    builds_record_missions,
    ensure_tables,
)

CID = "mission-thread-0001"


@pytest.fixture()
def cx(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
    lifecycle_migrations.apply_at_boot(connection)
    try:
        yield connection
    finally:
        connection.close()


def _features() -> list[MissionFeature]:
    return [
        MissionFeature(key="A", sentence="store tags"),
        MissionFeature(key="B", sentence="add tags", needs=("A",)),
        MissionFeature(key="C", sentence="filter by tag", needs=("A",)),
        MissionFeature(key="D", sentence="tag cloud", needs=("B", "C"), no_sign_in=True),
    ]


def _mission(store: MissionStore):
    mission, created = store.create_mission(
        title="Tags for notes",
        target_repo="guardkit/react-go-notes-fixture",
        correlation_id=CID,
        originating_user="U1",
        features=_features(),
    )
    assert created
    return mission


def _tables(cx: sqlite3.Connection) -> set[str]:
    return {r[0] for r in cx.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_ensure_tables_adds_tables_and_columns_and_keeps_the_schema_version(cx) -> None:
    assert not builds_record_missions(cx)
    ensure_tables(cx)
    ensure_tables(cx)  # idempotent
    assert {"missions", "mission_features", "mission_needs", "mission_attempts"} <= _tables(cx)
    assert builds_record_missions(cx)
    assert cx.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 21
    assert lifecycle_migrations.apply_at_boot(cx) == 21


def test_the_rollout_helper_boot_steps_make_the_mission_tables() -> None:
    support = Path(__file__).resolve().parents[3] / "deploy" / "estate" / "rollout_support.py"
    text = support.read_text(encoding="utf-8")
    assert "from forge.lifecycle import missions" in text
    assert "missions.ensure_tables(c)" in text


def test_create_mission_is_idempotent_on_its_correlation_id(cx) -> None:
    store = MissionStore(cx)
    mission = _mission(store)
    assert re.fullmatch(r"m-[0-9a-f]{6}", mission.id)
    assert mission.branch == f"mission/{mission.id}"
    assert mission.status == "PROPOSED"
    again, created = store.create_mission(
        title="Tags for notes",
        target_repo="guardkit/react-go-notes-fixture",
        correlation_id=CID,
        originating_user="U1",
        features=_features(),
    )
    assert not created and again.id == mission.id
    features = store.features(mission.id)
    assert [f.key for f in features] == ["A", "B", "C", "D"]
    assert features[3].needs == ("B", "C") and features[3].no_sign_in


def test_approval_first_answer_wins_and_status_is_compare_and_swap(cx) -> None:
    store = MissionStore(cx)
    mission = _mission(store)
    assert store.record_approval(mission.id, approved_by="U1", plan_request_id="r1")
    # The approval and STARTING land in one statement (R7): never approved
    # and still PROPOSED.
    assert store.get(mission.id).status == "STARTING"
    assert not store.record_approval(mission.id, approved_by="U2", plan_request_id="r2")
    assert not store.set_status(mission.id, "STARTING", expected=("PROPOSED",))
    assert store.set_base_commit(mission.id, "a" * 40)
    assert not store.set_base_commit(mission.id, "b" * 40)
    got = store.get(mission.id)
    assert got.approved_by == "U1" and got.plan_request_id == "r1"
    assert got.base_commit == "a" * 40
    assert [m.id for m in store.list_active()] == [mission.id]
    assert store.set_status(mission.id, "NOT_MERGED", expected=("STARTING",), reason="x")
    closed = store.get(mission.id)
    assert closed.closed_at and closed.closed_reason == "x"
    assert store.list_active() == []



def test_links_follow_keys_through_a_rebuild(cx) -> None:
    store = MissionStore(cx)
    mission = _mission(store)
    store.file_first_attempts(mission.id)
    a1 = attempt_correlation_id(CID, "A", 1)
    assert store.needs_resolution(mission.id, "B").waiting_for == ("A",)
    assert store.admit_attempt(a1)
    assert store.fail_with_rebuild(a1)
    # Attempt 2 pending: B still waits, never held.
    answer = store.needs_resolution(mission.id, "B")
    assert not answer.ready and answer.blocked_by is None
    a2 = attempt_correlation_id(CID, "A", 2)
    assert store.attempt_by_correlation_id(a2).status == "WAITING"
    assert store.admit_attempt(a2)
    assert store.mark_landed(a2)
    assert store.needs_resolution(mission.id, "B").ready
    # D needs two parents: B and C.
    assert store.needs_resolution(mission.id, "D").waiting_for == ("B", "C")


def test_a_replay_never_files_a_third_attempt(cx) -> None:
    store = MissionStore(cx)
    mission = _mission(store)
    store.file_first_attempts(mission.id)
    a1 = attempt_correlation_id(CID, "A", 1)
    store.admit_attempt(a1)
    assert store.fail_with_rebuild(a1)
    assert not store.fail_with_rebuild(a1)  # replay: already closed
    a2 = attempt_correlation_id(CID, "A", 2)
    store.admit_attempt(a2)
    assert store.fail_with_rebuild(a2)  # attempt 2: just FAILED
    assert [a.attempt for a in store.attempts(mission.id) if a.key == "A"] == [1, 2]
    assert store.needs_resolution(mission.id, "B").blocked_by == "A"
    with pytest.raises(sqlite3.IntegrityError):
        cx.execute(
            "INSERT INTO mission_attempts (mission_id, key, attempt, correlation_id, status) "
            "VALUES (?, 'A', 3, 'x-A-a3', 'WAITING')",
            (mission.id,),
        )


def test_admission_counts_inside_its_own_transaction(cx) -> None:
    store = MissionStore(cx)
    mission = _mission(store)
    store.file_first_attempts(mission.id)
    a1 = attempt_correlation_id(CID, "A", 1)
    seen: list[bool] = []

    def count() -> int:
        seen.append(cx.in_transaction)
        return 1

    assert not store.admit_attempt(a1, max_in_flight=1, count_in_flight=count)
    assert seen == [True]
    assert store.admit_attempt(a1, max_in_flight=2, count_in_flight=count)
    assert store.requeue_attempt(a1)
    assert store.attempt_by_correlation_id(a1).status == "WAITING"


def test_mark_failed_is_idempotent_and_holds_dependants(cx) -> None:
    store = MissionStore(cx)
    mission = _mission(store)
    store.file_first_attempts(mission.id)
    a1 = attempt_correlation_id(CID, "A", 1)
    assert store.mark_failed(a1, "the feature file was refused")
    assert not store.mark_failed(a1, "again")
    assert store.attempt_by_correlation_id(a1).reason == "the feature file was refused"
    assert store.needs_resolution(mission.id, "B").blocked_by == "A"
    assert store.mark_held(attempt_correlation_id(CID, "B", 1), "A did not land")


def test_the_one_build_insert_copies_the_mission(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from nats_core.events import BuildQueuedPayload

    from forge.lifecycle.persistence import SqliteLifecyclePersistence

    db = tmp_path / "ledger.db"
    connection = sqlite_connect.connect_writer(db)
    lifecycle_migrations.apply_at_boot(connection)
    store = MissionStore(connection)
    mission = _mission(store)
    store.file_first_attempts(mission.id)
    persistence = SqliteLifecyclePersistence(connection=connection, db_path=db)

    def queue(correlation_id: str, feature_id: str) -> str:
        now = datetime.now(UTC)
        return persistence.record_pending_build(
            BuildQueuedPayload(
                feature_id=feature_id,
                repo="guardkit/react-go-notes-fixture",
                feature_yaml_path=f".guardkit/features/{feature_id}.yaml",
                triggered_by="forge-internal",
                correlation_id=correlation_id,
                requested_at=now,
                queued_at=now,
            )
        )

    member = queue(attempt_correlation_id(CID, "A", 1), "FEAT-AAA1")
    other = queue("not-a-mission", "FEAT-BBB2")
    rows = dict(
        (r[0], (r[1], r[2]))
        for r in connection.execute("SELECT build_id, mission_id, mission_role FROM builds")
    )
    assert rows[member] == (mission.id, "feature")
    assert rows[other] == (None, None)
    connection.close()

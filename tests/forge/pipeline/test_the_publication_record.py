"""The publication record: say it before you do it, and one worker at a time.

One-true-copy design pass, item 1: second revision A ("Say what is about to be
done before doing it, and look at the world when picking up") and third
revision E ("Taking over a build cancels the previous worker's authority").

A real ledger in a temporary directory, made by Forge's own migration code.
Nothing here starts anything, contacts anything or reads anybody's settings.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations
from forge.pipeline.publication_record import (
    LINE_ABOUT_TO,
    LINE_DONE,
    PUBLICATION_RESULTS,
    RESULT_MERGED_AND_RUNNING,
    RESULT_PUBLICATION_PENDING,
    RESULT_PUBLISHED_DEPLOYMENT_PENDING,
    STEP_CANDIDATE_CHECK,
    STEP_JOIN,
    PublicationRecordStore,
)

BUILD = "build-FEAT-PR1-1"


def _now() -> datetime:
    return datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path: Path) -> PublicationRecordStore:
    cx: sqlite3.Connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    return PublicationRecordStore(cx)


class TestABuildWithNoRecord:
    def test_reads_as_not_recorded(self, store: PublicationRecordStore) -> None:
        record = store.read("a-build-from-before-all-this")
        assert record.recorded is False
        assert record.sentence == "not recorded"
        assert record.lines == ()
        assert record.turn == 0

    def test_a_write_to_a_build_with_no_record_changes_nothing(
        self, store: PublicationRecordStore
    ) -> None:
        assert (
            store.record(build_id=BUILD, turn=1, now=_now(), g_commit="a" * 40)
            is False
        )
        assert store.read(BUILD).recorded is False


class TestTheLeaseAndTheTurnNumber:
    def test_the_first_take_is_turn_one_and_writes_who_and_what(
        self, store: PublicationRecordStore
    ) -> None:
        grant = store.take_lease(
            build_id=BUILD,
            holder="worker-a",
            now=_now(),
            feature_id="FEAT-PR1",
            repo="org/project",
            decided_by="the owner",
            target_branch="main",
        )
        assert grant is not None and grant.turn == 1
        record = store.read(BUILD)
        assert record.recorded is True
        assert record.decided_by == "the owner"
        assert record.target_branch == "main"
        assert record.lease_holder == "worker-a"
        assert record.lease_is_live(_now()) is True

    def test_a_live_lease_is_left_alone(self, store: PublicationRecordStore) -> None:
        store.take_lease(build_id=BUILD, holder="worker-a", now=_now())
        assert (
            store.take_lease(build_id=BUILD, holder="worker-b", now=_now()) is None
        )
        assert store.read(BUILD).turn == 1

    def test_an_expired_lease_can_be_taken_over_and_the_turn_goes_up(
        self, store: PublicationRecordStore
    ) -> None:
        store.take_lease(build_id=BUILD, holder="worker-a", now=_now(), seconds=60)
        later = _now() + timedelta(seconds=120)
        grant = store.take_lease(build_id=BUILD, holder="worker-b", now=later)
        assert grant is not None
        assert grant.turn == 2
        assert grant.took_over_from == "worker-a"

    def test_renewing_does_not_change_the_turn(
        self, store: PublicationRecordStore
    ) -> None:
        grant = store.take_lease(build_id=BUILD, holder="worker-a", now=_now())
        assert store.renew_lease(build_id=BUILD, turn=grant.turn, now=_now()) is True
        assert store.read(BUILD).turn == grant.turn


class TestAReplacedWorkersNextWriteChangesNoRow:
    def test_every_kind_of_write_is_refused_on_an_old_turn(
        self, store: PublicationRecordStore
    ) -> None:
        first = store.take_lease(
            build_id=BUILD, holder="worker-a", now=_now(), seconds=60
        )
        later = _now() + timedelta(seconds=120)
        second = store.take_lease(build_id=BUILD, holder="worker-b", now=later)
        assert second.turn == first.turn + 1

        # A: every write it now attempts changes no row.
        assert (
            store.record(
                build_id=BUILD, turn=first.turn, now=later, g_commit="a" * 40
            )
            is False
        )
        assert (
            store.about_to(
                build_id=BUILD,
                turn=first.turn,
                now=later,
                step=STEP_JOIN,
                attempt=1,
                inputs={"anything": "at all"},
            )
            is False
        )
        assert (
            store.done(
                build_id=BUILD,
                turn=first.turn,
                now=later,
                step=STEP_JOIN,
                attempt=1,
                result={"j_commit": "b" * 40},
            )
            is False
        )
        assert (
            store.renew_lease(build_id=BUILD, turn=first.turn, now=later) is False
        )
        # ...and the record is exactly what B left it as.
        record = store.read(BUILD)
        assert record.g_commit is None
        assert record.lines == ()
        assert record.lease_holder == "worker-b"

        # B, on the current turn, writes normally.
        assert (
            store.record(
                build_id=BUILD, turn=second.turn, now=later, g_commit="c" * 40
            )
            is True
        )
        assert store.read(BUILD).g_commit == "c" * 40


class TestTheLines:
    def test_about_to_comes_before_done_and_both_are_kept(
        self, store: PublicationRecordStore
    ) -> None:
        grant = store.take_lease(build_id=BUILD, holder="worker-a", now=_now())
        store.about_to(
            build_id=BUILD,
            turn=grant.turn,
            now=_now(),
            step=STEP_JOIN,
            attempt=1,
            inputs={"g_commit": "a" * 40, "build_tip": "b" * 40},
        )
        record = store.read(BUILD)
        assert record.attempt == 1
        unfinished = record.unfinished()
        assert unfinished is not None
        assert unfinished.kind == LINE_ABOUT_TO
        assert unfinished.step == STEP_JOIN
        assert unfinished.detail["g_commit"] == "a" * 40

        store.done(
            build_id=BUILD,
            turn=grant.turn,
            now=_now(),
            step=STEP_JOIN,
            attempt=1,
            result={"j_commit": "c" * 40},
            j_commit="c" * 40,
        )
        record = store.read(BUILD)
        assert record.unfinished() is None
        assert [line.kind for line in record.lines] == [LINE_ABOUT_TO, LINE_DONE]
        assert record.is_done(STEP_JOIN) is True
        assert record.is_done(STEP_CANDIDATE_CHECK) is False
        assert record.j_commit == "c" * 40

    def test_the_record_survives_a_restart_and_says_what_finished(
        self, tmp_path: Path
    ) -> None:
        """A simulated restart: a new connection, the same file, the same facts."""
        db = tmp_path / "forge.db"
        cx = sqlite_connect.connect_writer(db)
        migrations.apply_at_boot(cx)
        first = PublicationRecordStore(cx)
        grant = first.take_lease(build_id=BUILD, holder="worker-a", now=_now())
        first.about_to(
            build_id=BUILD,
            turn=grant.turn,
            now=_now(),
            step=STEP_JOIN,
            attempt=1,
            inputs={"g_commit": "a" * 40},
        )
        first.done(
            build_id=BUILD,
            turn=grant.turn,
            now=_now(),
            step=STEP_JOIN,
            attempt=1,
            result={"j_commit": "c" * 40},
            j_commit="c" * 40,
        )
        cx.close()

        # ...the coordinator stops and starts again.
        again = PublicationRecordStore(sqlite_connect.connect_writer(db))
        record = again.read(BUILD)
        assert record.is_done(STEP_JOIN) is True
        assert record.unfinished() is None
        assert record.j_commit == "c" * 40


class TestTheVocabulary:
    def test_the_three_names_are_the_designs_own(self) -> None:
        assert PUBLICATION_RESULTS == (
            RESULT_PUBLICATION_PENDING,
            RESULT_PUBLISHED_DEPLOYMENT_PENDING,
            RESULT_MERGED_AND_RUNNING,
        )
        assert RESULT_PUBLICATION_PENDING == "publication pending"
        assert RESULT_PUBLISHED_DEPLOYMENT_PENDING == "published, deployment pending"
        assert RESULT_MERGED_AND_RUNNING == "merged into GitHub and running"

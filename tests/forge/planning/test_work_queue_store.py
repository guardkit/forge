"""Tests for the work queue store (Lane B stage one, contract 4).

The queue is the list of sentences the factory has been asked for and has not
started yet. These tests pin the three things a person actually notices:

- filing a sentence twice files ONE row (a redelivered message is not a
  second piece of work), and the row says how many are ahead of it;
- the order arithmetic — the back, the front, and "before that one" — and the
  renumbering that keeps it readable when the gaps run out;
- dropping a row keeps it, and every change is attributable to whoever made
  it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations
from forge.planning.work_queue_store import (
    RANK_EPSILON,
    WorkQueueStore,
    valid_queue_id,
)

USER = "U-RICH"


@pytest.fixture
def connection(tmp_path: Path) -> sqlite3.Connection:
    cx = sqlite_connect.connect_writer(tmp_path / "queue.db")
    migrations.apply_at_boot(cx)
    yield cx
    cx.close()


@pytest.fixture
def store(connection: sqlite3.Connection) -> WorkQueueStore:
    return WorkQueueStore(connection)


def _file(
    store: WorkQueueStore,
    correlation_id: str,
    *,
    sentence: str = "build a thing",
    repo: str | None = "api_test",
    kind: str = "feature",
    position: str = "back",
    before_id: int | None = None,
):
    return store.file_sentence(
        correlation_id=correlation_id,
        sentence=sentence,
        originating_user=USER,
        target_repo=repo,
        kind=kind,
        position=position,
        before_id=before_id,
    )


def _order(store: WorkQueueStore) -> list[int]:
    return [int(row["id"]) for row in store.list_open()]


# ---------------------------------------------------------------------------
# Filing
# ---------------------------------------------------------------------------


class TestFiling:
    def test_a_sentence_becomes_one_queued_row(self, store: WorkQueueStore) -> None:
        filed = _file(store, "plan-1")
        row = store.get(filed.queue_id)
        assert row is not None
        assert row["status"] == "QUEUED"
        assert row["sentence"] == "build a thing"
        assert row["target_repo"] == "api_test"
        assert row["kind"] == "feature"
        assert row["originating_user"] == USER
        assert row["correlation_id"] == "plan-1"
        assert filed.created is True
        assert filed.ahead == 0

    def test_the_same_correlation_id_files_one_row(
        self, store: WorkQueueStore, connection: sqlite3.Connection
    ) -> None:
        first = _file(store, "plan-1")
        second = _file(store, "plan-1")
        assert second.queue_id == first.queue_id
        assert second.created is False
        count = connection.execute("SELECT COUNT(*) FROM work_queue").fetchone()[0]
        assert count == 1

    def test_a_redelivery_writes_no_second_event(
        self, store: WorkQueueStore
    ) -> None:
        filed = _file(store, "plan-1")
        _file(store, "plan-1")
        assert len(store.list_events(filed.queue_id)) == 1

    def test_the_count_ahead_is_how_many_are_in_front(
        self, store: WorkQueueStore
    ) -> None:
        _file(store, "plan-1")
        _file(store, "plan-2")
        third = _file(store, "plan-3")
        assert third.ahead == 2

    def test_a_kind_outside_the_three_is_refused(self, store: WorkQueueStore) -> None:
        with pytest.raises(ValueError):
            _file(store, "plan-1", kind="chore")

    def test_the_three_kinds_are_accepted(self, store: WorkQueueStore) -> None:
        for index, kind in enumerate(("feature", "fix", "question")):
            filed = _file(store, f"plan-{index}", kind=kind)
            row = store.get(filed.queue_id)
            assert row is not None and row["kind"] == kind


# ---------------------------------------------------------------------------
# The order
# ---------------------------------------------------------------------------


class TestRankArithmetic:
    def test_a_new_row_goes_to_the_back(self, store: WorkQueueStore) -> None:
        first = _file(store, "plan-1")
        second = _file(store, "plan-2")
        assert _order(store) == [first.queue_id, second.queue_id]
        assert store.get(second.queue_id)["rank"] == pytest.approx(2.0)

    def test_the_first_row_ranks_one(self, store: WorkQueueStore) -> None:
        filed = _file(store, "plan-1")
        assert store.get(filed.queue_id)["rank"] == pytest.approx(1.0)

    def test_next_goes_to_the_front(self, store: WorkQueueStore) -> None:
        first = _file(store, "plan-1")
        second = _file(store, "plan-2")
        jumped = _file(store, "plan-3", position="front")
        assert _order(store) == [jumped.queue_id, first.queue_id, second.queue_id]
        assert jumped.ahead == 0

    def test_twenty_next_calls_keep_the_reverse_order(
        self, store: WorkQueueStore
    ) -> None:
        """Twenty ``next:`` sentences: the last one typed is the one in front."""
        original = _file(store, "plan-0")
        jumpers = [
            _file(store, f"plan-{index}", position="front").queue_id
            for index in range(1, 21)
        ]
        assert _order(store) == list(reversed(jumpers)) + [original.queue_id]

        ranks = [float(row["rank"]) for row in store.list_open()]
        assert ranks == sorted(ranks)
        assert all(
            (later - earlier) >= RANK_EPSILON
            for earlier, later in zip(ranks, ranks[1:])
        ), "no two rows may share a place in the order"

    def test_before_takes_the_midpoint(self, store: WorkQueueStore) -> None:
        first = _file(store, "plan-1")
        second = _file(store, "plan-2")
        inserted = _file(store, "plan-3", position="before", before_id=second.queue_id)
        assert _order(store) == [first.queue_id, inserted.queue_id, second.queue_id]
        assert store.get(inserted.queue_id)["rank"] == pytest.approx(1.5)

    def test_before_the_first_row_goes_to_the_front(
        self, store: WorkQueueStore
    ) -> None:
        first = _file(store, "plan-1")
        inserted = _file(store, "plan-2", position="before", before_id=first.queue_id)
        assert _order(store) == [inserted.queue_id, first.queue_id]

    def test_repeated_midpoints_renumber_the_queue(
        self, store: WorkQueueStore
    ) -> None:
        """Halving one gap forever is not possible; the queue renumbers instead.

        Sixty sentences all asked to go in front of the same row. Pure
        midpoints would have collapsed the gap below a millionth after about
        twenty of them; that every neighbour is still a clear step apart at
        the end is the renumbering doing its job.
        """
        first = _file(store, "plan-1")
        last = _file(store, "plan-2")
        inserted = [
            _file(
                store,
                f"plan-mid-{index}",
                position="before",
                before_id=last.queue_id,
            ).queue_id
            for index in range(60)
        ]

        assert _order(store) == [first.queue_id] + inserted + [last.queue_id]
        ranks = [float(row["rank"]) for row in store.list_open()]
        assert all(
            (later - earlier) >= RANK_EPSILON
            for earlier, later in zip(ranks, ranks[1:])
        ), "sixty midpoints without a renumber would have run out of room"

    def test_a_renumber_makes_the_ranks_whole_numbers_again(
        self, store: WorkQueueStore
    ) -> None:
        """Two rows sharing a place is the other trigger: renumber, in order."""
        first = _file(store, "plan-1")
        second = _file(store, "plan-2")
        third = _file(store, "plan-3")
        # Force the collision the arithmetic normally prevents.
        store._connection.execute(
            "UPDATE work_queue SET rank = 2.0 WHERE id = ?", (third.queue_id,)
        )
        store._connection.commit()

        fourth = _file(store, "plan-4")
        assert [float(row["rank"]) for row in store.list_open()] == [
            1.0,
            2.0,
            3.0,
            4.0,
        ]
        assert _order(store) == [
            first.queue_id,
            second.queue_id,
            third.queue_id,
            fourth.queue_id,
        ]

    def test_a_renumber_leaves_closed_rows_alone(
        self, store: WorkQueueStore
    ) -> None:
        dropped = _file(store, "plan-dropped")
        store.drop(dropped.queue_id, actor_identity=USER)
        dropped_rank = float(store.get(dropped.queue_id)["rank"])

        first = _file(store, "plan-1")
        last = _file(store, "plan-2")
        for index in range(60):
            _file(store, f"plan-mid-{index}", position="before", before_id=last.queue_id)

        assert float(store.get(dropped.queue_id)["rank"]) == pytest.approx(
            dropped_rank
        )
        assert dropped.queue_id not in _order(store)
        assert first.queue_id in _order(store)


class TestPromoteAndLink:
    def test_promote_moves_a_row_to_the_front(self, store: WorkQueueStore) -> None:
        first = _file(store, "plan-1")
        second = _file(store, "plan-2")
        third = _file(store, "plan-3")
        assert store.promote(third.queue_id, actor_identity=USER) is True
        assert _order(store) == [third.queue_id, first.queue_id, second.queue_id]

    def test_promote_refuses_a_closed_row(self, store: WorkQueueStore) -> None:
        filed = _file(store, "plan-1")
        store.drop(filed.queue_id, actor_identity=USER)
        assert store.promote(filed.queue_id, actor_identity=USER) is False

    def test_promote_refuses_an_unknown_row(self, store: WorkQueueStore) -> None:
        assert store.promote(404, actor_identity=USER) is False

    def test_link_records_the_row_it_waits_for(self, store: WorkQueueStore) -> None:
        first = _file(store, "plan-1")
        second = _file(store, "plan-2")
        assert store.link(second.queue_id, first.queue_id, actor_identity=USER) is True
        assert store.get(second.queue_id)["after_id"] == first.queue_id

    def test_link_does_not_move_the_row(self, store: WorkQueueStore) -> None:
        first = _file(store, "plan-1")
        second = _file(store, "plan-2")
        store.link(first.queue_id, second.queue_id, actor_identity=USER)
        assert _order(store) == [first.queue_id, second.queue_id]

    def test_link_refuses_an_unknown_antecedent(self, store: WorkQueueStore) -> None:
        first = _file(store, "plan-1")
        assert store.link(first.queue_id, 404, actor_identity=USER) is False


class TestKeepAndClose:
    def test_keep_counts_and_clears_the_reminder(
        self, store: WorkQueueStore
    ) -> None:
        filed = _file(store, "plan-1")
        assert store.keep(filed.queue_id, actor_identity=USER) is True
        row = store.get(filed.queue_id)
        assert row["keep_count"] == 1
        assert row["stale_pinged_at"] is None
        store.keep(filed.queue_id, actor_identity=USER)
        assert store.get(filed.queue_id)["keep_count"] == 2

    def test_drop_withdraws_and_never_deletes(self, store: WorkQueueStore) -> None:
        filed = _file(store, "plan-1")
        assert store.drop(filed.queue_id, actor_identity=USER, reason="not now") is True
        row = store.get(filed.queue_id)
        assert row is not None
        assert row["status"] == "WITHDRAWN"
        assert row["closed_at"] is not None
        assert row["closed_reason"] == "not now"
        assert row["sentence"] == "build a thing"
        assert filed.queue_id not in _order(store)

    def test_drop_twice_changes_nothing(self, store: WorkQueueStore) -> None:
        filed = _file(store, "plan-1")
        store.drop(filed.queue_id, actor_identity=USER)
        assert store.drop(filed.queue_id, actor_identity=USER) is False

    def test_close_done_and_blocked(self, store: WorkQueueStore) -> None:
        done = _file(store, "plan-1")
        blocked = _file(store, "plan-2")
        assert store.close(done.queue_id, status="DONE", actor_identity="forge") is True
        assert (
            store.close(
                blocked.queue_id,
                status="BLOCKED",
                actor_identity="forge",
                reason="the plan leg failed",
            )
            is True
        )
        assert store.get(done.queue_id)["status"] == "DONE"
        assert store.get(blocked.queue_id)["closed_reason"] == "the plan leg failed"
        assert _order(store) == []

    def test_close_takes_only_done_or_blocked(self, store: WorkQueueStore) -> None:
        filed = _file(store, "plan-1")
        with pytest.raises(ValueError):
            store.close(filed.queue_id, status="WITHDRAWN", actor_identity="forge")


class TestEvents:
    def test_filing_writes_an_event_naming_the_person(
        self, store: WorkQueueStore
    ) -> None:
        filed = _file(store, "plan-1")
        events = store.list_events(filed.queue_id)
        assert [e["action"] for e in events] == ["queued"]
        assert events[0]["actor_identity"] == USER
        assert events[0]["recorded_at"] is not None

    def test_every_change_leaves_an_event(self, store: WorkQueueStore) -> None:
        first = _file(store, "plan-1")
        second = _file(store, "plan-2")
        store.promote(second.queue_id, actor_identity="U-OTHER")
        store.link(second.queue_id, first.queue_id, actor_identity="U-OTHER")
        store.keep(second.queue_id, actor_identity="U-OTHER")
        store.drop(second.queue_id, actor_identity="U-OTHER")
        actions = [e["action"] for e in store.list_events(second.queue_id)]
        assert actions == ["queued", "promote", "link", "keep", "drop"]
        assert {
            e["actor_identity"] for e in store.list_events(second.queue_id)[1:]
        } == {"U-OTHER"}

    def test_the_front_and_before_filings_name_themselves(
        self, store: WorkQueueStore
    ) -> None:
        first = _file(store, "plan-1")
        front = _file(store, "plan-2", position="front")
        before = _file(store, "plan-3", position="before", before_id=first.queue_id)
        assert store.list_events(front.queue_id)[0]["action"] == "add_front"
        assert store.list_events(before.queue_id)[0]["action"] == "add_before"


class TestLookups:
    def test_a_row_is_found_by_its_correlation_id(
        self, store: WorkQueueStore
    ) -> None:
        filed = _file(store, "plan-1")
        row = store.get_by_correlation_id("plan-1")
        assert row is not None and int(row["id"]) == filed.queue_id
        assert store.get_by_correlation_id("plan-nope") is None

    def test_an_unknown_id_reads_none(self, store: WorkQueueStore) -> None:
        assert store.get(404) is None

    def test_an_empty_queue_lists_nothing(self, store: WorkQueueStore) -> None:
        assert store.list_open() == []


# ---------------------------------------------------------------------------
# Row numbers that could never name a row (coaches' correction, 2026-09-05)
# ---------------------------------------------------------------------------


IMPOSSIBLE_IDS = [0, -1, 10**12, "\u0661\u0662", "12", 12.0, None, True, [1]]


class TestRowNumbersThatCouldNeverNameARow:
    """Jarvis checks the shape of what was typed; the store checks the number.

    ``"\u0661\u0662"`` is twelve in Arabic-Indic digits — ``int()`` reads it
    happily — so anything that is not already a plain whole number is refused
    rather than quietly turned into one.
    """

    @pytest.mark.parametrize("value", IMPOSSIBLE_IDS)
    def test_they_are_not_row_numbers(self, value: object) -> None:
        assert valid_queue_id(value) is None

    def test_an_ordinary_row_number_is_one(self) -> None:
        assert valid_queue_id(7) == 7

    @pytest.mark.parametrize("value", [0, -1, 10**12, "\u0661\u0662"])
    def test_every_door_into_the_store_refuses_them(
        self, store: WorkQueueStore, value: object
    ) -> None:
        filed = _file(store, "plan-1")
        assert store.get(value) is None
        assert store.promote(value, actor_identity=USER) is False
        assert store.keep(value, actor_identity=USER) is False
        assert store.drop(value, actor_identity=USER) is False
        assert store.admit(value, actor_identity=USER) is False
        assert store.requeue(value, actor_identity=USER) is False
        assert store.close(value, status="DONE", actor_identity=USER) is False
        assert store.link(filed.queue_id, value, actor_identity=USER) is False
        assert store.link(value, filed.queue_id, actor_identity=USER) is False
        store.mark_stale_pinged(value, at="2026-09-05T12:00:00+00:00")
        # Nothing moved, and nothing was written down about a row that
        # nobody named.
        row = store.get(filed.queue_id)
        assert row["status"] == "QUEUED" and row["after_id"] is None
        assert [e["action"] for e in store.list_events(filed.queue_id)] == ["queued"]


# ---------------------------------------------------------------------------
# Waits that would never end
# ---------------------------------------------------------------------------


class TestLinksThatWouldNeverEnd:
    def test_a_row_cannot_wait_for_itself(self, store: WorkQueueStore) -> None:
        filed = _file(store, "plan-1")
        assert store.link(filed.queue_id, filed.queue_id, actor_identity=USER) is False
        assert store.get(filed.queue_id)["after_id"] is None
        assert [e["action"] for e in store.list_events(filed.queue_id)] == ["queued"]

    def test_two_rows_cannot_wait_for_each_other(self, store: WorkQueueStore) -> None:
        first = _file(store, "plan-1")
        second = _file(store, "plan-2")
        assert store.link(first.queue_id, second.queue_id, actor_identity=USER) is True
        assert store.link(second.queue_id, first.queue_id, actor_identity=USER) is False
        assert store.get(second.queue_id)["after_id"] is None
        assert [e["action"] for e in store.list_events(second.queue_id)] == ["queued"]

    def test_a_longer_circle_is_refused_too(self, store: WorkQueueStore) -> None:
        first = _file(store, "plan-1")
        second = _file(store, "plan-2")
        third = _file(store, "plan-3")
        store.link(first.queue_id, second.queue_id, actor_identity=USER)
        store.link(second.queue_id, third.queue_id, actor_identity=USER)
        assert store.link(third.queue_id, first.queue_id, actor_identity=USER) is False
        assert store.get(third.queue_id)["after_id"] is None

    def test_a_chain_that_does_not_close_is_allowed(
        self, store: WorkQueueStore
    ) -> None:
        first = _file(store, "plan-1")
        second = _file(store, "plan-2")
        third = _file(store, "plan-3")
        assert store.link(second.queue_id, first.queue_id, actor_identity=USER) is True
        assert store.link(third.queue_id, second.queue_id, actor_identity=USER) is True
        assert store.waits_on(third.queue_id, first.queue_id) is True
        assert store.waits_on(first.queue_id, third.queue_id) is False


# ---------------------------------------------------------------------------
# A change and the note of who made it commit together
# ---------------------------------------------------------------------------


def _break_the_events_table(store: WorkQueueStore, monkeypatch) -> None:
    """Make writing an events row fail, the way a full disk would."""

    def boom(**_: object) -> int:
        raise sqlite3.OperationalError("the events table is unavailable")

    monkeypatch.setattr(store, "_record_event", boom)


class TestOneTransaction:
    """If the note cannot be written, the change does not happen either."""

    def test_a_filing_leaves_no_row_behind(
        self, store: WorkQueueStore, monkeypatch
    ) -> None:
        _break_the_events_table(store, monkeypatch)
        with pytest.raises(sqlite3.OperationalError):
            _file(store, "plan-1")
        assert store.get_by_correlation_id("plan-1") is None
        assert _order(store) == []

    def test_a_promote_does_not_move_the_row(
        self, store: WorkQueueStore, monkeypatch
    ) -> None:
        first = _file(store, "plan-1")
        second = _file(store, "plan-2")
        _break_the_events_table(store, monkeypatch)
        with pytest.raises(sqlite3.OperationalError):
            store.promote(second.queue_id, actor_identity=USER)
        assert _order(store) == [first.queue_id, second.queue_id]

    def test_a_link_does_not_record_the_wait(
        self, store: WorkQueueStore, monkeypatch
    ) -> None:
        first = _file(store, "plan-1")
        second = _file(store, "plan-2")
        _break_the_events_table(store, monkeypatch)
        with pytest.raises(sqlite3.OperationalError):
            store.link(second.queue_id, first.queue_id, actor_identity=USER)
        assert store.get(second.queue_id)["after_id"] is None

    def test_a_keep_does_not_count(
        self, store: WorkQueueStore, monkeypatch
    ) -> None:
        filed = _file(store, "plan-1")
        _break_the_events_table(store, monkeypatch)
        with pytest.raises(sqlite3.OperationalError):
            store.keep(filed.queue_id, actor_identity=USER)
        assert store.get(filed.queue_id)["keep_count"] == 0

    def test_a_drop_leaves_the_row_in_the_queue(
        self, store: WorkQueueStore, monkeypatch
    ) -> None:
        filed = _file(store, "plan-1")
        _break_the_events_table(store, monkeypatch)
        with pytest.raises(sqlite3.OperationalError):
            store.drop(filed.queue_id, actor_identity=USER)
        assert store.get(filed.queue_id)["status"] == "QUEUED"

    def test_a_close_leaves_the_row_open(
        self, store: WorkQueueStore, monkeypatch
    ) -> None:
        filed = _file(store, "plan-1")
        _break_the_events_table(store, monkeypatch)
        with pytest.raises(sqlite3.OperationalError):
            store.close(filed.queue_id, status="DONE", actor_identity="forge")
        assert store.get(filed.queue_id)["status"] == "QUEUED"

    def test_an_admission_does_not_take_the_row(
        self, store: WorkQueueStore, monkeypatch
    ) -> None:
        filed = _file(store, "plan-1")
        _break_the_events_table(store, monkeypatch)
        with pytest.raises(sqlite3.OperationalError):
            store.admit(filed.queue_id, actor_identity="forge")
        assert store.get(filed.queue_id)["status"] == "QUEUED"

    def test_a_requeue_leaves_the_row_admitted(
        self, store: WorkQueueStore, monkeypatch
    ) -> None:
        filed = _file(store, "plan-1")
        store.admit(filed.queue_id, actor_identity="forge")
        _break_the_events_table(store, monkeypatch)
        with pytest.raises(sqlite3.OperationalError):
            store.requeue(filed.queue_id, actor_identity="forge")
        assert store.get(filed.queue_id)["status"] == "ADMITTED"


# ---------------------------------------------------------------------------
# "In front of that one" when that one is gone
# ---------------------------------------------------------------------------


class TestFilingInFrontOfARowThatIsGone:
    def test_a_closed_row_is_refused_not_answered_with_the_back(
        self, store: WorkQueueStore
    ) -> None:
        first = _file(store, "plan-1")
        store.drop(first.queue_id, actor_identity=USER)
        with pytest.raises(ValueError):
            _file(store, "plan-2", position="before", before_id=first.queue_id)
        assert store.get_by_correlation_id("plan-2") is None
        assert _order(store) == []

    def test_an_unknown_row_is_refused(self, store: WorkQueueStore) -> None:
        _file(store, "plan-1")
        with pytest.raises(ValueError):
            _file(store, "plan-2", position="before", before_id=404)
        assert store.get_by_correlation_id("plan-2") is None

    def test_a_number_that_could_never_be_a_row_is_refused(
        self, store: WorkQueueStore
    ) -> None:
        _file(store, "plan-1")
        with pytest.raises(ValueError):
            _file(store, "plan-2", position="before", before_id=0)
        assert store.get_by_correlation_id("plan-2") is None

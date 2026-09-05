"""Tests for what the forge says back about the queue (contracts 3 and 5).

Every string here is read by a person in Slack, so every string is pinned
word for word. If one of these fails, the queue started talking in code.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations
from forge.planning.work_queue_commands import (
    age_phrase,
    execute_command,
    NOT_A_ROW_NUMBER,
    list_reply,
    queued_reply,
)
from forge.planning.work_queue_store import WorkQueueStore

USER = "U-RICH"
NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path: Path) -> WorkQueueStore:
    cx = sqlite_connect.connect_writer(tmp_path / "queue.db")
    migrations.apply_at_boot(cx)
    yield WorkQueueStore(cx)
    cx.close()


def _file(store: WorkQueueStore, correlation_id: str, **kwargs):
    params = {
        "sentence": "build a thing",
        "originating_user": USER,
        "target_repo": "api_test",
        "kind": "feature",
    }
    params.update(kwargs)
    return store.file_sentence(correlation_id=correlation_id, **params)


def _run(store: WorkQueueStore, command: dict, **kwargs) -> str:
    params = {
        "actor_identity": USER,
        "correlation_id": "plan-cmd",
        "originating_user": USER,
        "target_repo": "api_test",
        "kind": "feature",
        "clock": lambda: NOW,
    }
    params.update(kwargs)
    return execute_command(store, command, **params)


# ---------------------------------------------------------------------------
# The line a filed sentence gets back
# ---------------------------------------------------------------------------


class TestQueuedReply:
    def test_nothing_ahead_of_it(self) -> None:
        assert (
            queued_reply(queue_id=12, target_repo="api_test", kind="feature", ahead=0)
            == "Queued as #12 (api_test · feature). Nothing ahead of it."
        )

    def test_one_ahead_of_it(self) -> None:
        assert (
            queued_reply(queue_id=13, target_repo="api_test", kind="fix", ahead=1)
            == "Queued as #13 (api_test · fix). One ahead of it."
        )

    def test_two_ahead_of_it(self) -> None:
        assert (
            queued_reply(queue_id=12, target_repo="api_test", kind="feature", ahead=2)
            == "Queued as #12 (api_test · feature). Two ahead of it."
        )

    def test_a_big_count_is_written_in_digits(self) -> None:
        assert (
            queued_reply(queue_id=99, target_repo="forge", kind="feature", ahead=31)
            == "Queued as #99 (forge · feature). 31 ahead of it."
        )

    def test_no_repository_named(self) -> None:
        """The spec does not write this case; the repository is simply left out."""
        assert (
            queued_reply(queue_id=4, target_repo=None, kind="question", ahead=0)
            == "Queued as #4 (question). Nothing ahead of it."
        )


class TestAgePhrase:
    @pytest.mark.parametrize(
        "delta,expected",
        [
            (timedelta(seconds=5), "just now"),
            (timedelta(minutes=1), "1 minute ago"),
            (timedelta(minutes=40), "40 minutes ago"),
            (timedelta(hours=1), "1 hour ago"),
            (timedelta(hours=5), "5 hours ago"),
            (timedelta(days=3), "3 days ago"),
        ],
    )
    def test_ages_read_as_english(self, delta: timedelta, expected: str) -> None:
        assert age_phrase((NOW - delta).isoformat(), NOW) == expected


# ---------------------------------------------------------------------------
# The commands
# ---------------------------------------------------------------------------


class TestList:
    def test_an_empty_queue_says_so(self, store: WorkQueueStore) -> None:
        assert _run(store, {"verb": "list"}) == "Nothing in the queue."

    def test_one_row_per_line_oldest_first(self, store: WorkQueueStore) -> None:
        first = _file(store, "plan-1")
        second = _file(store, "plan-2", target_repo="forge", kind="fix")
        # The second one is asked to go first; the list still reads oldest first.
        store.promote(second.queue_id, actor_identity=USER)

        reply = _run(store, {"verb": "list"})
        lines = reply.split("\n")
        assert len(lines) == 2
        assert lines[0].startswith(f"#{first.queue_id} (api_test · feature) — ")
        assert lines[0].endswith("position 2")
        assert lines[1].startswith(f"#{second.queue_id} (forge · fix) — ")
        assert lines[1].endswith("position 1")

    def test_the_line_carries_the_age(self, store: WorkQueueStore) -> None:
        filed = _file(store, "plan-1")
        rows = store.list_open()
        reply = list_reply(rows, now=NOW + timedelta(hours=3))
        assert reply.startswith(f"#{filed.queue_id} (api_test · feature) — asked for ")
        assert "position 1" in reply

    def test_listing_changes_nothing(self, store: WorkQueueStore) -> None:
        filed = _file(store, "plan-1")
        before = [dict(row) for row in store.list_open()]
        _run(store, {"verb": "list"})
        assert [dict(row) for row in store.list_open()] == before
        assert [e["action"] for e in store.list_events(filed.queue_id)] == ["queued"]


class TestPromote:
    def test_promote_answers_in_one_line(self, store: WorkQueueStore) -> None:
        _file(store, "plan-1")
        second = _file(store, "plan-2")
        reply = _run(store, {"verb": "promote", "id": second.queue_id})
        assert reply == f"#{second.queue_id} is next."
        assert int(store.list_open()[0]["id"]) == second.queue_id

    def test_promote_an_unknown_row(self, store: WorkQueueStore) -> None:
        assert _run(store, {"verb": "promote", "id": 404}) == (
            "There is no #404 in the queue."
        )

    def test_promote_a_withdrawn_row(self, store: WorkQueueStore) -> None:
        filed = _file(store, "plan-1")
        store.drop(filed.queue_id, actor_identity=USER)
        assert _run(store, {"verb": "promote", "id": filed.queue_id}) == (
            f"#{filed.queue_id} is not in the queue any more — it is withdrawn."
        )


class TestLink:
    def test_link_answers_in_one_line(self, store: WorkQueueStore) -> None:
        first = _file(store, "plan-1")
        second = _file(store, "plan-2")
        reply = _run(
            store,
            {"verb": "link", "id": second.queue_id, "after": first.queue_id},
        )
        assert reply == f"#{second.queue_id} will wait until #{first.queue_id} is done."
        assert store.get(second.queue_id)["after_id"] == first.queue_id

    def test_link_to_an_unknown_row(self, store: WorkQueueStore) -> None:
        first = _file(store, "plan-1")
        assert _run(store, {"verb": "link", "id": first.queue_id, "after": 404}) == (
            "There is no #404 in the queue."
        )


class TestKeepAndDrop:
    def test_keep_answers_in_one_line(self, store: WorkQueueStore) -> None:
        filed = _file(store, "plan-1")
        assert _run(store, {"verb": "keep", "id": filed.queue_id}) == (
            f"#{filed.queue_id} stays in the queue."
        )
        assert store.get(filed.queue_id)["keep_count"] == 1

    def test_drop_answers_in_one_line_and_keeps_the_row(
        self, store: WorkQueueStore
    ) -> None:
        filed = _file(store, "plan-1")
        assert _run(store, {"verb": "drop", "id": filed.queue_id}) == (
            f"#{filed.queue_id} is out of the queue. Nothing was deleted."
        )
        row = store.get(filed.queue_id)
        assert row is not None and row["status"] == "WITHDRAWN"

    def test_drop_an_unknown_row(self, store: WorkQueueStore) -> None:
        assert _run(store, {"verb": "drop", "id": 7}) == "There is no #7 in the queue."


class TestAddFrontAndAddBefore:
    def test_next_files_a_row_at_the_front(self, store: WorkQueueStore) -> None:
        first = _file(store, "plan-1")
        reply = _run(
            store,
            {"verb": "add_front", "sentence": "fix the login page"},
            correlation_id="plan-front",
        )
        row = store.get_by_correlation_id("plan-front")
        assert row is not None
        assert reply == (
            f"Queued as #{int(row['id'])} (api_test · feature). Nothing ahead of it."
        )
        assert row["sentence"] == "fix the login page"
        assert [int(r["id"]) for r in store.list_open()] == [
            int(row["id"]),
            first.queue_id,
        ]

    def test_before_files_a_row_in_front_of_the_named_one(
        self, store: WorkQueueStore
    ) -> None:
        first = _file(store, "plan-1")
        second = _file(store, "plan-2")
        reply = _run(
            store,
            {
                "verb": "add_before",
                "id": second.queue_id,
                "sentence": "do this one sooner",
            },
            correlation_id="plan-before",
        )
        row = store.get_by_correlation_id("plan-before")
        assert row is not None
        assert reply == (
            f"Queued as #{int(row['id'])} (api_test · feature). One ahead of it."
        )
        assert [int(r["id"]) for r in store.list_open()] == [
            first.queue_id,
            int(row["id"]),
            second.queue_id,
        ]

    def test_before_an_unknown_row_files_nothing(
        self, store: WorkQueueStore
    ) -> None:
        reply = _run(
            store,
            {"verb": "add_before", "id": 404, "sentence": "do this one sooner"},
            correlation_id="plan-before",
        )
        assert reply == "There is no #404 in the queue."
        assert store.list_open() == []

    def test_a_command_with_no_sentence_files_nothing(
        self, store: WorkQueueStore
    ) -> None:
        reply = _run(store, {"verb": "add_front", "sentence": "   "})
        assert reply == "That command had no sentence in it, so I have queued nothing."
        assert store.list_open() == []

    def test_the_kind_rides_along(self, store: WorkQueueStore) -> None:
        _run(
            store,
            {"verb": "add_front", "sentence": "the login page is broken"},
            correlation_id="plan-front",
            kind="fix",
        )
        row = store.get_by_correlation_id("plan-front")
        assert row is not None and row["kind"] == "fix"


class TestEventsAndUnknownVerbs:
    def test_every_command_that_changes_a_row_writes_an_event(
        self, store: WorkQueueStore
    ) -> None:
        first = _file(store, "plan-1")
        second = _file(store, "plan-2")
        _run(store, {"verb": "promote", "id": second.queue_id}, actor_identity="U-SLACK")
        _run(
            store,
            {"verb": "link", "id": second.queue_id, "after": first.queue_id},
            actor_identity="U-SLACK",
        )
        _run(store, {"verb": "keep", "id": second.queue_id}, actor_identity="U-SLACK")
        _run(store, {"verb": "drop", "id": second.queue_id}, actor_identity="U-SLACK")

        events = store.list_events(second.queue_id)
        assert [e["action"] for e in events] == [
            "queued",
            "promote",
            "link",
            "keep",
            "drop",
        ]
        assert [e["actor_identity"] for e in events[1:]] == ["U-SLACK"] * 4

    def test_an_unknown_verb_changes_nothing(self, store: WorkQueueStore) -> None:
        _file(store, "plan-1")
        reply = _run(store, {"verb": "explode", "id": 1})
        assert reply == "I do not know that queue command, so I have changed nothing."
        assert len(store.list_open()) == 1


# ---------------------------------------------------------------------------
# "In front of #12" when #12 is gone (coaches' correction, 2026-09-05)
# ---------------------------------------------------------------------------


class TestBeforeARowThatIsGone:
    """The same line every other verb gives, and nothing filed."""

    def test_a_withdrawn_row_gets_the_closed_line(
        self, store: WorkQueueStore
    ) -> None:
        filed = _file(store, "plan-1")
        store.drop(filed.queue_id, actor_identity=USER)
        reply = _run(
            store,
            {
                "verb": "add_before",
                "id": filed.queue_id,
                "sentence": "do this one sooner",
            },
            correlation_id="plan-before",
        )
        assert reply == (
            f"#{filed.queue_id} is not in the queue any more — it is withdrawn."
        )
        assert store.get_by_correlation_id("plan-before") is None
        assert store.list_open() == []

    def test_a_finished_row_gets_the_closed_line(
        self, store: WorkQueueStore
    ) -> None:
        filed = _file(store, "plan-1")
        store.close(filed.queue_id, status="DONE", actor_identity="forge")
        reply = _run(
            store,
            {
                "verb": "add_before",
                "id": filed.queue_id,
                "sentence": "do this one sooner",
            },
            correlation_id="plan-before",
        )
        assert reply == (
            f"#{filed.queue_id} is not in the queue any more — it is done."
        )
        assert store.get_by_correlation_id("plan-before") is None

    def test_a_blocked_row_gets_the_closed_line(self, store: WorkQueueStore) -> None:
        filed = _file(store, "plan-1")
        store.close(
            filed.queue_id,
            status="BLOCKED",
            actor_identity="forge",
            reason="the planning run failed",
        )
        reply = _run(
            store,
            {
                "verb": "add_before",
                "id": filed.queue_id,
                "sentence": "do this one sooner",
            },
            correlation_id="plan-before",
        )
        assert reply == (
            f"#{filed.queue_id} is not in the queue any more — it is blocked."
        )
        assert store.get_by_correlation_id("plan-before") is None

    def test_it_never_goes_to_the_back_of_the_queue(
        self, store: WorkQueueStore
    ) -> None:
        gone = _file(store, "plan-1")
        waiting = _file(store, "plan-2")
        store.drop(gone.queue_id, actor_identity=USER)
        _run(
            store,
            {"verb": "add_before", "id": gone.queue_id, "sentence": "sneak in"},
            correlation_id="plan-before",
        )
        assert [int(row["id"]) for row in store.list_open()] == [waiting.queue_id]


# ---------------------------------------------------------------------------
# Row numbers that could never name a row
# ---------------------------------------------------------------------------


IMPOSSIBLE_IDS = [0, -1, 10**12, "\u0661\u0662", "12", None, 12.5]


class TestImpossibleRowNumbers:
    """Jarvis forwards whatever matched its pattern; the forge checks it."""

    @pytest.mark.parametrize("value", IMPOSSIBLE_IDS)
    @pytest.mark.parametrize("verb", ["promote", "keep", "drop"])
    def test_one_plain_reply_and_nothing_changes(
        self, store: WorkQueueStore, verb: str, value: object
    ) -> None:
        filed = _file(store, "plan-1")
        assert _run(store, {"verb": verb, "id": value}) == NOT_A_ROW_NUMBER
        row = store.get(filed.queue_id)
        assert row["status"] == "QUEUED" and row["keep_count"] == 0
        assert [e["action"] for e in store.list_events(filed.queue_id)] == ["queued"]

    @pytest.mark.parametrize("value", IMPOSSIBLE_IDS)
    def test_a_link_either_way_round(
        self, store: WorkQueueStore, value: object
    ) -> None:
        filed = _file(store, "plan-1")
        assert (
            _run(store, {"verb": "link", "id": filed.queue_id, "after": value})
            == NOT_A_ROW_NUMBER
        )
        assert (
            _run(store, {"verb": "link", "id": value, "after": filed.queue_id})
            == NOT_A_ROW_NUMBER
        )
        assert store.get(filed.queue_id)["after_id"] is None

    @pytest.mark.parametrize("value", IMPOSSIBLE_IDS)
    def test_before_files_nothing(
        self, store: WorkQueueStore, value: object
    ) -> None:
        _file(store, "plan-1")
        reply = _run(
            store,
            {"verb": "add_before", "id": value, "sentence": "do this one sooner"},
            correlation_id="plan-before",
        )
        assert reply == NOT_A_ROW_NUMBER
        assert store.get_by_correlation_id("plan-before") is None


# ---------------------------------------------------------------------------
# Waits that would never end
# ---------------------------------------------------------------------------


class TestLinksThatWouldNeverEnd:
    def test_a_row_cannot_be_told_to_wait_for_itself(
        self, store: WorkQueueStore
    ) -> None:
        filed = _file(store, "plan-1")
        reply = _run(
            store, {"verb": "link", "id": filed.queue_id, "after": filed.queue_id}
        )
        assert reply == f"#{filed.queue_id} cannot wait for itself."
        assert store.get(filed.queue_id)["after_id"] is None
        assert [e["action"] for e in store.list_events(filed.queue_id)] == ["queued"]

    def test_a_circle_is_refused_in_one_line(self, store: WorkQueueStore) -> None:
        first = _file(store, "plan-1")
        second = _file(store, "plan-2")
        _run(store, {"verb": "link", "id": first.queue_id, "after": second.queue_id})
        reply = _run(
            store, {"verb": "link", "id": second.queue_id, "after": first.queue_id}
        )
        assert reply == (
            f"#{second.queue_id} cannot wait for #{first.queue_id}, because "
            f"#{first.queue_id} is already waiting on #{second.queue_id}."
        )
        assert store.get(second.queue_id)["after_id"] is None
        assert [e["action"] for e in store.list_events(second.queue_id)] == ["queued"]

    def test_a_longer_circle_is_refused_too(self, store: WorkQueueStore) -> None:
        first = _file(store, "plan-1")
        second = _file(store, "plan-2")
        third = _file(store, "plan-3")
        _run(store, {"verb": "link", "id": first.queue_id, "after": second.queue_id})
        _run(store, {"verb": "link", "id": second.queue_id, "after": third.queue_id})
        reply = _run(
            store, {"verb": "link", "id": third.queue_id, "after": first.queue_id}
        )
        assert reply.startswith(f"#{third.queue_id} cannot wait for #{first.queue_id}")
        assert store.get(third.queue_id)["after_id"] is None

    def test_an_ordinary_wait_still_works(self, store: WorkQueueStore) -> None:
        first = _file(store, "plan-1")
        second = _file(store, "plan-2")
        reply = _run(
            store, {"verb": "link", "id": second.queue_id, "after": first.queue_id}
        )
        assert reply == (
            f"#{second.queue_id} will wait until #{first.queue_id} is done."
        )
        assert store.get(second.queue_id)["after_id"] == first.queue_id

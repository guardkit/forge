"""The intake with a queue behind it (Lane B stage one, contract 5).

With a queue wired in, a sentence that passes the gates becomes a row in the
work queue and NOT a planning run — the take-next loop creates the run later,
with this same correlation id. A message carrying a command jarvis has
forwarded is executed against the queue and answered in the thread.

The last class in this file is the guard that matters most: with no queue
wired in, the intake still does exactly what it did before this lane.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from nats_core.envelope import EventType, MessageEnvelope

from forge.adapters.nats.planning_consumer import (
    PlanningConsumerDeps,
    handle_planning_message,
)
from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations
from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.work_queue_store import WorkQueueStore

CORRELATION_ID = "plan-abc123"
USER = "U-RICH"
THREAD = "1725530000.000100"


@pytest.fixture
def connection(tmp_path: Path) -> sqlite3.Connection:
    cx = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    yield cx
    cx.close()


@pytest.fixture
def run_store(connection: sqlite3.Connection) -> SqlitePlanningRunStore:
    return SqlitePlanningRunStore(connection)


@pytest.fixture
def queue_store(connection: sqlite3.Connection) -> WorkQueueStore:
    return WorkQueueStore(connection)


@pytest.fixture
def notify() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def deps(
    run_store: SqlitePlanningRunStore,
    queue_store: WorkQueueStore,
    notify: AsyncMock,
) -> PlanningConsumerDeps:
    return PlanningConsumerDeps(
        store=run_store,
        publish_notification=notify,
        on_recorded=AsyncMock(),
        queue_store=queue_store,
    )


def _payload(
    *,
    correlation_id: str = CORRELATION_ID,
    request_text: str = "build a login page",
    target_repo: str | None = "api_test",
    **extras: Any,
) -> dict[str, Any]:
    payload = {
        "stage": "planning",
        "request_text": request_text,
        "target_repo": target_repo,
        "triggered_by": "jarvis",
        "originating_adapter": "slack",
        "originating_user": USER,
        "correlation_id": correlation_id,
        "parent_request_id": THREAD,
        "retry_count": 0,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "queued_at": datetime.now(timezone.utc).isoformat(),
    }
    payload.update(extras)
    return payload


def _msg(payload: dict[str, Any]) -> AsyncMock:
    envelope = MessageEnvelope(
        message_id="msg-queue-001",
        timestamp=datetime.now(timezone.utc),
        version="1.0",
        source_id="slack",
        event_type=EventType.BUILD_QUEUED,
        project=None,
        correlation_id=str(payload["correlation_id"]),
        payload=payload,
    )
    msg = AsyncMock()
    msg.data = envelope.model_dump_json().encode("utf-8")
    msg.ack = AsyncMock()
    msg.nak = AsyncMock()
    return msg


def _messages(notify: AsyncMock) -> list[str]:
    return [call.args[1] for call in notify.await_args_list]


# ---------------------------------------------------------------------------
# A sentence
# ---------------------------------------------------------------------------


class TestASentenceIsFiled:
    @pytest.mark.asyncio
    async def test_it_becomes_a_queue_row_and_not_a_planning_run(
        self,
        deps: PlanningConsumerDeps,
        queue_store: WorkQueueStore,
        run_store: SqlitePlanningRunStore,
    ) -> None:
        msg = _msg(_payload())
        await handle_planning_message(msg, deps)

        row = queue_store.get_by_correlation_id(CORRELATION_ID)
        assert row is not None
        assert row["status"] == "QUEUED"
        assert row["sentence"] == "build a login page"
        assert row["target_repo"] == "api_test"
        assert row["kind"] == "feature"
        assert row["originating_user"] == USER
        assert run_store.get_run(CORRELATION_ID) is None, (
            "the planning run is the take-next loop's to create, not the intake's"
        )
        msg.ack.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_driver_is_not_kicked(
        self, deps: PlanningConsumerDeps
    ) -> None:
        await handle_planning_message(_msg(_payload()), deps)
        assert deps.on_recorded is not None
        deps.on_recorded.assert_not_awaited()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_the_reply_is_the_line_the_spec_writes(
        self, deps: PlanningConsumerDeps, notify: AsyncMock, queue_store
    ) -> None:
        await handle_planning_message(_msg(_payload()), deps)
        queue_id = int(queue_store.get_by_correlation_id(CORRELATION_ID)["id"])
        assert _messages(notify) == [
            f"Queued as #{queue_id} (api_test · feature). Nothing ahead of it."
        ]

    @pytest.mark.asyncio
    async def test_the_reply_carries_the_thread_it_came_from(
        self, deps: PlanningConsumerDeps, notify: AsyncMock
    ) -> None:
        await handle_planning_message(_msg(_payload()), deps)
        assert notify.await_args_list[0].kwargs["parent_request_id"] == THREAD

    @pytest.mark.asyncio
    async def test_the_second_sentence_is_told_what_is_ahead_of_it(
        self, deps: PlanningConsumerDeps, notify: AsyncMock, queue_store
    ) -> None:
        await handle_planning_message(_msg(_payload()), deps)
        await handle_planning_message(
            _msg(_payload(correlation_id="plan-second")), deps
        )
        second = int(queue_store.get_by_correlation_id("plan-second")["id"])
        assert _messages(notify)[1] == (
            f"Queued as #{second} (api_test · feature). One ahead of it."
        )

    @pytest.mark.asyncio
    async def test_a_redelivery_files_one_row_and_says_nothing_twice(
        self, deps: PlanningConsumerDeps, notify: AsyncMock, connection
    ) -> None:
        msg = _msg(_payload())
        await handle_planning_message(msg, deps)
        await handle_planning_message(_msg(_payload()), deps)

        assert connection.execute("SELECT COUNT(*) FROM work_queue").fetchone()[0] == 1
        assert len(_messages(notify)) == 1

    @pytest.mark.asyncio
    async def test_the_kind_jarvis_sent_is_kept(
        self, deps: PlanningConsumerDeps, queue_store: WorkQueueStore
    ) -> None:
        await handle_planning_message(_msg(_payload(kind="fix")), deps)
        row = queue_store.get_by_correlation_id(CORRELATION_ID)
        assert row is not None and row["kind"] == "fix"

    @pytest.mark.asyncio
    async def test_a_kind_the_forge_does_not_know_becomes_a_feature(
        self, deps: PlanningConsumerDeps, queue_store: WorkQueueStore
    ) -> None:
        await handle_planning_message(_msg(_payload(kind="chore")), deps)
        row = queue_store.get_by_correlation_id(CORRELATION_ID)
        assert row is not None and row["kind"] == "feature"

    @pytest.mark.asyncio
    async def test_a_queue_write_failure_asks_for_redelivery(
        self,
        run_store: SqlitePlanningRunStore,
        queue_store: WorkQueueStore,
        notify: AsyncMock,
    ) -> None:
        """A sentence is never dropped: no ack when the write did not happen."""

        def _boom(**_: Any) -> None:
            raise sqlite3.OperationalError("database is locked")

        queue_store.file_sentence = _boom  # type: ignore[method-assign]
        deps = PlanningConsumerDeps(
            store=run_store, publish_notification=notify, queue_store=queue_store
        )
        msg = _msg(_payload())
        await handle_planning_message(msg, deps)

        msg.ack.assert_not_awaited()
        msg.nak.assert_awaited()


# ---------------------------------------------------------------------------
# A command
# ---------------------------------------------------------------------------


class TestACommand:
    @pytest.mark.asyncio
    async def test_queue_lists_what_is_waiting(
        self, deps: PlanningConsumerDeps, notify: AsyncMock, queue_store
    ) -> None:
        await handle_planning_message(_msg(_payload()), deps)
        queue_id = int(queue_store.get_by_correlation_id(CORRELATION_ID)["id"])
        notify.reset_mock()

        await handle_planning_message(
            _msg(
                _payload(
                    correlation_id="plan-cmd-1",
                    request_text="queue",
                    queue_command={"verb": "list"},
                )
            ),
            deps,
        )
        lines = _messages(notify)[0].split("\n")
        assert len(lines) == 1
        assert lines[0].startswith(f"#{queue_id} (api_test · feature) — ")
        assert lines[0].endswith("position 1")

    @pytest.mark.asyncio
    async def test_a_command_files_no_row_of_its_own(
        self, deps: PlanningConsumerDeps, queue_store: WorkQueueStore
    ) -> None:
        await handle_planning_message(
            _msg(
                _payload(
                    correlation_id="plan-cmd-1",
                    request_text="queue",
                    queue_command={"verb": "list"},
                )
            ),
            deps,
        )
        assert queue_store.get_by_correlation_id("plan-cmd-1") is None
        assert queue_store.list_open() == []

    @pytest.mark.asyncio
    async def test_promote_moves_the_row_and_answers_in_one_line(
        self, deps: PlanningConsumerDeps, notify: AsyncMock, queue_store
    ) -> None:
        await handle_planning_message(_msg(_payload(correlation_id="plan-1")), deps)
        await handle_planning_message(_msg(_payload(correlation_id="plan-2")), deps)
        second = int(queue_store.get_by_correlation_id("plan-2")["id"])
        notify.reset_mock()

        await handle_planning_message(
            _msg(
                _payload(
                    correlation_id="plan-cmd-1",
                    request_text=f"#{second} next",
                    queue_command={"verb": "promote", "id": second},
                )
            ),
            deps,
        )
        assert _messages(notify) == [f"#{second} is next."]
        assert int(queue_store.list_open()[0]["id"]) == second

    @pytest.mark.asyncio
    async def test_next_files_a_sentence_at_the_front(
        self, deps: PlanningConsumerDeps, notify: AsyncMock, queue_store
    ) -> None:
        await handle_planning_message(_msg(_payload(correlation_id="plan-1")), deps)
        first = int(queue_store.get_by_correlation_id("plan-1")["id"])
        notify.reset_mock()

        await handle_planning_message(
            _msg(
                _payload(
                    correlation_id="plan-cmd-1",
                    request_text="next: fix the login page",
                    queue_command={
                        "verb": "add_front",
                        "sentence": "fix the login page",
                    },
                )
            ),
            deps,
        )
        jumped = queue_store.get_by_correlation_id("plan-cmd-1")
        assert jumped is not None
        assert jumped["sentence"] == "fix the login page"
        assert [int(r["id"]) for r in queue_store.list_open()] == [
            int(jumped["id"]),
            first,
        ]
        assert _messages(notify) == [
            f"Queued as #{int(jumped['id'])} (api_test · feature). "
            "Nothing ahead of it."
        ]

    @pytest.mark.asyncio
    async def test_drop_withdraws_the_row_and_keeps_it(
        self, deps: PlanningConsumerDeps, notify: AsyncMock, queue_store
    ) -> None:
        await handle_planning_message(_msg(_payload(correlation_id="plan-1")), deps)
        queue_id = int(queue_store.get_by_correlation_id("plan-1")["id"])
        notify.reset_mock()

        await handle_planning_message(
            _msg(
                _payload(
                    correlation_id="plan-cmd-1",
                    request_text=f"drop {queue_id}",
                    queue_command={"verb": "drop", "id": queue_id},
                )
            ),
            deps,
        )
        assert _messages(notify) == [
            f"#{queue_id} is out of the queue. Nothing was deleted."
        ]
        assert queue_store.get(queue_id)["status"] == "WITHDRAWN"

    @pytest.mark.asyncio
    async def test_every_command_is_written_down_against_the_person(
        self, deps: PlanningConsumerDeps, queue_store: WorkQueueStore
    ) -> None:
        await handle_planning_message(_msg(_payload(correlation_id="plan-1")), deps)
        queue_id = int(queue_store.get_by_correlation_id("plan-1")["id"])

        await handle_planning_message(
            _msg(
                _payload(
                    correlation_id="plan-cmd-1",
                    request_text=f"keep {queue_id}",
                    originating_user="U-SOMEONE-ELSE",
                    queue_command={"verb": "keep", "id": queue_id},
                )
            ),
            deps,
        )
        events = queue_store.list_events(queue_id)
        assert [e["action"] for e in events] == ["queued", "keep"]
        assert events[1]["actor_identity"] == "U-SOMEONE-ELSE"

    @pytest.mark.asyncio
    async def test_a_command_naming_a_row_that_is_not_there(
        self, deps: PlanningConsumerDeps, notify: AsyncMock
    ) -> None:
        msg = _msg(
            _payload(
                correlation_id="plan-cmd-1",
                request_text="#404 next",
                queue_command={"verb": "promote", "id": 404},
            )
        )
        await handle_planning_message(msg, deps)
        assert _messages(notify) == ["There is no #404 in the queue."]
        msg.ack.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_command_reply_lands_in_its_own_thread(
        self, deps: PlanningConsumerDeps, notify: AsyncMock
    ) -> None:
        await handle_planning_message(
            _msg(
                _payload(
                    correlation_id="plan-cmd-1",
                    request_text="queue",
                    queue_command={"verb": "list"},
                )
            ),
            deps,
        )
        assert notify.await_args_list[0].kwargs["parent_request_id"] == THREAD


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


class TestWithoutAQueueNothingChanges:
    @pytest.mark.asyncio
    async def test_the_sentence_still_becomes_a_planning_run(
        self, run_store: SqlitePlanningRunStore, notify: AsyncMock
    ) -> None:
        kick = AsyncMock()
        deps = PlanningConsumerDeps(
            store=run_store, publish_notification=notify, on_recorded=kick
        )
        msg = _msg(_payload())
        await handle_planning_message(msg, deps)

        row = run_store.get_run(CORRELATION_ID)
        assert row is not None and row["state"] == "QUEUED"
        assert row["request_text"] == "build a login page"
        msg.ack.assert_awaited_once()
        kick.assert_awaited_once_with(CORRELATION_ID)
        notify.assert_not_awaited()


class TestTheRunCreationTheLoopWillCall:
    """The two steps admission reuses: record the run, kick the chain.

    The take-next loop (stage two) admits a queued row by calling this with
    the row's ORIGINAL correlation id. Nothing is re-published and no new id
    is minted, which is what keeps the Slack thread and every downstream
    receipt working.
    """

    @pytest.mark.asyncio
    async def test_it_records_the_run_under_the_original_correlation_id(
        self, run_store: SqlitePlanningRunStore
    ) -> None:
        from forge.adapters.nats.planning_consumer import (
            create_and_start_planning_run,
        )

        kick = AsyncMock()
        deps = PlanningConsumerDeps(store=run_store, on_recorded=kick)
        result = await create_and_start_planning_run(
            deps,
            correlation_id=CORRELATION_ID,
            request_text="build a login page",
            originating_user=USER,
            triggered_by="jarvis",
            originating_adapter="slack",
            parent_request_id=THREAD,
            target_repo="api_test",
        )

        assert result is None
        row = run_store.get_run(CORRELATION_ID)
        assert row is not None
        assert row["state"] == "QUEUED"
        assert row["request_text"] == "build a login page"
        assert row["target_repo"] == "api_test"
        assert row["originating_user"] == USER
        assert row["parent_request_id"] == THREAD
        kick.assert_awaited_once_with(CORRELATION_ID)


# ---------------------------------------------------------------------------
# How the notifier is called is settled when it is wired up (2026-09-05)
# ---------------------------------------------------------------------------


class TwoArgumentNotifier:
    """An older notifier that knows nothing about threads."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, correlation_id: str, message: str) -> None:
        self.calls.append((correlation_id, message))


class ThreadAwareNotifier:
    """The notifier the forge wires up today."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    async def __call__(
        self,
        correlation_id: str,
        message: str,
        *,
        parent_request_id: str | None = None,
    ) -> None:
        self.calls.append((correlation_id, message, parent_request_id))


class BrokenNotifier(ThreadAwareNotifier):
    """Takes the thread, then breaks on its own account."""

    async def __call__(
        self,
        correlation_id: str,
        message: str,
        *,
        parent_request_id: str | None = None,
    ) -> None:
        self.calls.append((correlation_id, message, parent_request_id))
        raise TypeError("something inside the notifier broke")


def _deps_with(
    run_store: SqlitePlanningRunStore,
    queue_store: WorkQueueStore,
    notifier: Any,
) -> PlanningConsumerDeps:
    return PlanningConsumerDeps(
        store=run_store,
        publish_notification=notifier,
        on_recorded=AsyncMock(),
        queue_store=queue_store,
    )


class TestTheNotifierIsReadOnce:
    def test_a_notifier_that_takes_the_thread_is_recognised(
        self, run_store: SqlitePlanningRunStore, queue_store: WorkQueueStore
    ) -> None:
        deps = _deps_with(run_store, queue_store, ThreadAwareNotifier())
        assert deps.notifier_takes_thread is True

    def test_an_older_notifier_is_recognised_too(
        self, run_store: SqlitePlanningRunStore, queue_store: WorkQueueStore
    ) -> None:
        deps = _deps_with(run_store, queue_store, TwoArgumentNotifier())
        assert deps.notifier_takes_thread is False

    def test_no_notifier_at_all(
        self, run_store: SqlitePlanningRunStore, queue_store: WorkQueueStore
    ) -> None:
        deps = _deps_with(run_store, queue_store, None)
        assert deps.notifier_takes_thread is False

    @pytest.mark.asyncio
    async def test_an_older_notifier_still_gets_the_reply(
        self, run_store: SqlitePlanningRunStore, queue_store: WorkQueueStore
    ) -> None:
        notifier = TwoArgumentNotifier()
        deps = _deps_with(run_store, queue_store, notifier)
        msg = _msg(_payload())

        await handle_planning_message(msg, deps)

        assert len(notifier.calls) == 1
        assert notifier.calls[0][1] == (
            "Queued as #1 (api_test · feature). Nothing ahead of it."
        )
        msg.ack.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_thread_goes_to_a_notifier_that_takes_it(
        self, run_store: SqlitePlanningRunStore, queue_store: WorkQueueStore
    ) -> None:
        notifier = ThreadAwareNotifier()
        deps = _deps_with(run_store, queue_store, notifier)

        await handle_planning_message(_msg(_payload()), deps)

        assert notifier.calls[0][2] == THREAD

    @pytest.mark.asyncio
    async def test_a_notifier_that_breaks_is_not_called_a_second_time(
        self, run_store: SqlitePlanningRunStore, queue_store: WorkQueueStore
    ) -> None:
        # The old code found out how to call the notifier by calling it and
        # catching TypeError, so a TypeError from inside the notifier looked
        # like an older notifier and the same sentence went out twice.
        notifier = BrokenNotifier()
        deps = _deps_with(run_store, queue_store, notifier)
        msg = _msg(_payload())

        await handle_planning_message(msg, deps)

        assert len(notifier.calls) == 1
        # The sentence is still filed and the message still acked: a broken
        # notifier never wedges the intake.
        assert queue_store.get_by_correlation_id(CORRELATION_ID) is not None
        msg.ack.assert_awaited_once()

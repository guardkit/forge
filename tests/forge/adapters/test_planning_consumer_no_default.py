"""Running the factory with no default repository (Linear front-door design, section 12).

When a request names no repository AND ``planning.default_target_repo`` is not
set, the intake refuses it at once, before anything is filed, with the one
plain sentence that lists the known repositories. That covers ordinary
sentences, questions, and the queue commands that file new work (``add_front``
and ``add_before``). A fix takes the repository of the build it fixes, and the
commands that only manage work already filed touch no repository, so both are
unchanged.

With a default configured nothing changes: the row keeps an unset repository
and the driver resolves the default later, exactly as before.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import AsyncMock

import pytest
from nats_core.envelope import EventType, MessageEnvelope

from forge.adapters.nats.planning_consumer import (
    PlanningConsumerDeps,
    handle_planning_message,
)
from forge.adapters.sqlite import connect as sqlite_connect
from forge.config.models import PlanningConfig
from forge.lifecycle import migrations
from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.target_repos import no_default_repo_message
from forge.planning.work_queue_commands import queued_reply
from forge.planning.work_queue_store import WorkQueueStore

CID = "plan-nodefault-0001"
USER = "U-OWNER"
THREAD = "1760000000.000100"
DEFAULT = "example/notes"

PATHS = {
    "example/notes": "/var/lib/forge/projects/notes",
    "example/tutor": "/var/lib/forge/projects/tutor",
}

REFUSAL = no_default_repo_message(PATHS)

#: The commands that only inspect or manage work already filed.
MANAGEMENT_VERBS = ("list", "promote", "link", "keep", "drop")


def _connect(path: Path) -> sqlite3.Connection:
    cx = sqlite_connect.connect_writer(path)
    migrations.apply_at_boot(cx)
    return cx


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    cx = _connect(tmp_path / "forge.db")
    yield cx
    cx.close()


def _deps(
    connection: sqlite3.Connection,
    *,
    default: str | None = None,
    notify: AsyncMock | None = None,
    queue: bool = True,
    publish_build: Any = None,
) -> PlanningConsumerDeps:
    return PlanningConsumerDeps(
        store=SqlitePlanningRunStore(connection),
        publish_notification=notify if notify is not None else AsyncMock(),
        on_recorded=AsyncMock(),
        planning_config=PlanningConfig(
            target_repo_paths=dict(PATHS), default_target_repo=default
        ),
        queue_store=WorkQueueStore(connection) if queue else None,
        publish_build_queued=publish_build,
    )


def _msg(
    *,
    correlation_id: str = CID,
    request_text: str = "add a search box",
    target: str | None = None,
    **extras: Any,
) -> AsyncMock:
    payload: dict[str, Any] = {
        "stage": "planning",
        "request_text": request_text,
        "target_repo": target,
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
    envelope = MessageEnvelope(
        source_id="jarvis",
        event_type=EventType.BUILD_QUEUED,
        correlation_id=correlation_id,
        payload=payload,
    )
    msg = AsyncMock()
    msg.data = envelope.model_dump_json().encode("utf-8")
    msg.ack = AsyncMock()
    msg.nak = AsyncMock()
    return msg


def _said(notify: AsyncMock) -> list[str]:
    return [call.args[1] for call in notify.await_args_list]


def _rows(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


# ---------------------------------------------------------------------------
# No default: unnamed new work is refused at the door
# ---------------------------------------------------------------------------


class TestUnnamedNewWorkIsRefused:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "extras",
        [
            pytest.param({}, id="sentence"),
            pytest.param({"kind": "feature"}, id="feature"),
            pytest.param({"kind": "question"}, id="question"),
            pytest.param(
                {"queue_command": {"verb": "add_front", "sentence": "add a search box"}},
                id="add_front",
            ),
        ],
    )
    async def test_refused_with_the_sentence_and_nothing_filed(
        self, connection: sqlite3.Connection, extras: dict[str, Any]
    ) -> None:
        notify = AsyncMock()
        deps = _deps(connection, notify=notify)
        msg = _msg(**extras)

        await handle_planning_message(msg, deps)

        assert _said(notify) == [REFUSAL]
        assert notify.await_args_list[0].kwargs["parent_request_id"] == THREAD
        assert _rows(connection, "work_queue") == 0
        assert _rows(connection, "planning_runs") == 0
        msg.ack.assert_awaited_once()
        deps.on_recorded.assert_not_awaited()  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_add_before_is_refused_and_files_nothing(
        self, connection: sqlite3.Connection
    ) -> None:
        notify = AsyncMock()
        deps = _deps(connection, notify=notify)
        queue = WorkQueueStore(connection)
        await handle_planning_message(
            _msg(correlation_id="plan-named", target="notes"), deps
        )
        named = queue.get_by_correlation_id("plan-named")
        assert named is not None
        notify.reset_mock()

        msg = _msg(
            queue_command={
                "verb": "add_before",
                "id": int(named["id"]),
                "sentence": "add a search box",
            }
        )
        await handle_planning_message(msg, deps)

        assert _said(notify) == [REFUSAL]
        assert _rows(connection, "work_queue") == 1
        assert queue.get_by_correlation_id(CID) is None
        msg.ack.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_without_a_queue_no_planning_run_is_recorded(
        self, connection: sqlite3.Connection
    ) -> None:
        notify = AsyncMock()
        deps = _deps(connection, notify=notify, queue=False)
        msg = _msg()

        await handle_planning_message(msg, deps)

        assert _said(notify) == [REFUSAL]
        assert _rows(connection, "planning_runs") == 0
        msg.ack.assert_awaited_once()
        deps.on_recorded.assert_not_awaited()  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_a_redelivery_is_refused_again_and_still_files_nothing(
        self, connection: sqlite3.Connection
    ) -> None:
        notify = AsyncMock()
        deps = _deps(connection, notify=notify)
        await handle_planning_message(_msg(), deps)
        await handle_planning_message(_msg(), deps)
        assert _said(notify) == [REFUSAL, REFUSAL]
        assert _rows(connection, "work_queue") == 0


# ---------------------------------------------------------------------------
# No default: named work is filed as today
# ---------------------------------------------------------------------------


class TestNamedWorkIsFiledAsToday:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["feature", "question"])
    async def test_a_named_sentence_is_queued(
        self, connection: sqlite3.Connection, kind: str
    ) -> None:
        notify = AsyncMock()
        await handle_planning_message(
            _msg(target="tutor", kind=kind), _deps(connection, notify=notify)
        )
        row = WorkQueueStore(connection).get_by_correlation_id(CID)
        assert row is not None
        assert row["target_repo"] == "example/tutor"
        assert row["kind"] == kind
        assert _said(notify) == [
            queued_reply(
                queue_id=int(row["id"]), target_repo="example/tutor", kind=kind, ahead=0
            )
        ]

    @pytest.mark.asyncio
    async def test_a_named_add_front_is_queued(
        self, connection: sqlite3.Connection
    ) -> None:
        notify = AsyncMock()
        await handle_planning_message(
            _msg(
                target="example/notes",
                queue_command={"verb": "add_front", "sentence": "add a search box"},
            ),
            _deps(connection, notify=notify),
        )
        row = WorkQueueStore(connection).get_by_correlation_id(CID)
        assert row is not None
        assert row["target_repo"] == "example/notes"
        assert _said(notify) == [
            queued_reply(
                queue_id=int(row["id"]),
                target_repo="example/notes",
                kind="feature",
                ahead=0,
            )
        ]


# ---------------------------------------------------------------------------
# No default: fixes and management commands are unchanged
# ---------------------------------------------------------------------------


class TestFixesAndManagementAreUnchanged:
    @pytest.mark.asyncio
    async def test_an_unnamed_fix_is_queued_as_today(
        self, connection: sqlite3.Connection
    ) -> None:
        notify = AsyncMock()
        await handle_planning_message(
            _msg(kind="fix"), _deps(connection, notify=notify)
        )
        row = WorkQueueStore(connection).get_by_correlation_id(CID)
        assert row is not None
        assert row["kind"] == "fix"
        assert row["target_repo"] is None
        assert REFUSAL not in _said(notify)

    @staticmethod
    async def _run_management(
        connection: sqlite3.Connection, verb: str, *, default: str | None
    ) -> tuple[list[str], list[tuple[int, str]]]:
        """Two named rows, then one unnamed management command; what was said
        about the command and the queue's open rows afterwards."""
        notify = AsyncMock()
        deps = _deps(connection, default=default, notify=notify)
        queue = WorkQueueStore(connection)
        for cid in ("plan-one", "plan-two"):
            await handle_planning_message(
                _msg(correlation_id=cid, target="notes"), deps
            )
        first = int(queue.get_by_correlation_id("plan-one")["id"])
        second = int(queue.get_by_correlation_id("plan-two")["id"])
        notify.reset_mock()

        command: dict[str, Any] = {"verb": verb}
        if verb in ("promote", "keep", "drop"):
            command["id"] = second
        if verb == "link":
            command.update(id=second, after=first)
        msg = _msg(correlation_id="plan-cmd", request_text=verb, queue_command=command)
        await handle_planning_message(msg, deps)
        msg.ack.assert_awaited_once()

        said = [line for line in _said(notify)]
        open_rows = [(int(r["id"]), str(r["status"])) for r in queue.list_open()]
        return said, open_rows

    @pytest.mark.asyncio
    @pytest.mark.parametrize("verb", MANAGEMENT_VERBS)
    async def test_management_commands_answer_as_with_a_default(
        self, tmp_path: Path, verb: str
    ) -> None:
        without = _connect(tmp_path / "without.db")
        with_default = _connect(tmp_path / "with.db")
        try:
            said_without, rows_without = await self._run_management(
                without, verb, default=None
            )
            said_with, rows_with = await self._run_management(
                with_default, verb, default=DEFAULT
            )
        finally:
            without.close()
            with_default.close()

        assert REFUSAL not in said_without
        assert said_without == said_with
        assert rows_without == rows_with
        assert said_without, "every management command is answered"

    @pytest.mark.asyncio
    async def test_an_unnamed_build_hand_over_keeps_its_own_answer(
        self, connection: sqlite3.Connection
    ) -> None:
        """``build:`` resolves its repository on its own path, as before."""
        notify, published = AsyncMock(), AsyncMock()
        await handle_planning_message(
            _msg(
                request_text="build: FEAT-1A2B from prepared/FEAT-1A2B",
                queue_command={
                    "verb": "build",
                    "feature_id": "FEAT-1A2B",
                    "branch": "prepared/FEAT-1A2B",
                },
            ),
            _deps(connection, notify=notify, publish_build=published),
        )
        published.assert_not_awaited()
        assert _said(notify) == [REFUSAL]
        assert _rows(connection, "work_queue") == 0

    @pytest.mark.asyncio
    async def test_a_named_build_hand_over_is_published(
        self, connection: sqlite3.Connection
    ) -> None:
        notify, published = AsyncMock(), AsyncMock()
        await handle_planning_message(
            _msg(
                target="notes",
                request_text="build: FEAT-1A2B from prepared/FEAT-1A2B",
                queue_command={
                    "verb": "build",
                    "feature_id": "FEAT-1A2B",
                    "branch": "prepared/FEAT-1A2B",
                },
            ),
            _deps(connection, notify=notify, publish_build=published),
        )
        published.assert_awaited_once()
        assert published.await_args.args[0].repo == "example/notes"
        assert _said(notify) == []


# ---------------------------------------------------------------------------
# With a default: nothing changes
# ---------------------------------------------------------------------------


class TestWithADefaultNothingChanges:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["feature", "question"])
    async def test_the_unnamed_row_is_stored_unset_and_answered_as_before(
        self, connection: sqlite3.Connection, kind: str
    ) -> None:
        notify = AsyncMock()
        msg = _msg(kind=kind)
        await handle_planning_message(
            msg, _deps(connection, default=DEFAULT, notify=notify)
        )
        row = WorkQueueStore(connection).get_by_correlation_id(CID)
        assert row is not None
        assert row["target_repo"] is None, (
            "the default is resolved and announced by the driver, not recorded here"
        )
        assert _said(notify) == [
            queued_reply(queue_id=int(row["id"]), target_repo=None, kind=kind, ahead=0)
        ]
        msg.ack.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_unnamed_add_front_is_stored_unset(
        self, connection: sqlite3.Connection
    ) -> None:
        notify = AsyncMock()
        await handle_planning_message(
            _msg(queue_command={"verb": "add_front", "sentence": "add a search box"}),
            _deps(connection, default=DEFAULT, notify=notify),
        )
        row = WorkQueueStore(connection).get_by_correlation_id(CID)
        assert row is not None
        assert row["target_repo"] is None
        assert _said(notify) == [
            queued_reply(
                queue_id=int(row["id"]), target_repo=None, kind="feature", ahead=0
            )
        ]

    @pytest.mark.asyncio
    async def test_without_a_queue_the_run_is_recorded_unset_and_driven(
        self, connection: sqlite3.Connection
    ) -> None:
        deps = _deps(connection, default=DEFAULT, queue=False)
        await handle_planning_message(_msg(), deps)
        run = SqlitePlanningRunStore(connection).get_run(CID)
        assert run is not None
        assert run["state"] == "QUEUED"
        assert run["target_repo"] is None
        deps.on_recorded.assert_awaited_once_with(CID)  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_with_no_planning_config_nothing_is_refused(
        self, connection: sqlite3.Connection
    ) -> None:
        """A forge wired without planning configuration behaves as before."""
        notify = AsyncMock()
        deps = PlanningConsumerDeps(
            store=SqlitePlanningRunStore(connection),
            publish_notification=notify,
            on_recorded=AsyncMock(),
            queue_store=WorkQueueStore(connection),
        )
        await handle_planning_message(_msg(), deps)
        row = WorkQueueStore(connection).get_by_correlation_id(CID)
        assert row is not None and row["target_repo"] is None
        assert REFUSAL not in _said(notify)

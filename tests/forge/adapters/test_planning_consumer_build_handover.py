"""``build: FEAT-XXXX from <branch>`` at the planning intake: resolution.

Register-projects design (5 October 2026), part 3, "Forge resolution"
acceptance checks. Jarvis forwards the hand-over as a queue command with the
``target:`` name exactly as typed. The intake resolves it with the same rules
and the same sentences a planning sentence's ``target:`` gets — omitted (the
default), short, canonical, unknown, ambiguous — and a resolved one becomes
ONE ``BuildQueuedPayload`` carrying the canonical repository and the Slack
message as ``parent_request_id``. Nothing here writes a planning run or a
queue row, and nothing is said on success: the build route answers.

The build-queue publisher is a recorder; no broker, no live service.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import AsyncMock

import pytest
from nats_core.envelope import EventType, MessageEnvelope
from nats_core.events import BuildQueuedPayload

from forge.adapters.nats.planning_consumer import (
    BUILD_HANDOVER_CORRELATION_SUFFIX,
    PlanningConsumerDeps,
    handle_planning_message,
    publish_build_request,
)
from forge.adapters.sqlite import connect as sqlite_connect
from forge.config.models import PlanningConfig
from forge.lifecycle import migrations
from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.target_repos import (
    ambiguous_repo_message,
    no_default_repo_message,
    unknown_repo_message,
)
from forge.planning.work_queue_store import WorkQueueStore

CID = "plan-handover-0001"
USER = "U-RICH"
THREAD = "1759660000.000300"
FEATURE = "FEAT-1A2B"
BRANCH = "prepared/FEAT-1A2B"

PATHS = {
    "guardkit/api_test": "/var/lib/forge/projects/api_test",
    "guardkit/doc_probe": "/var/lib/forge/projects/doc_probe",
    # The same short name in two places: "shared" is ambiguous.
    "guardkit/shared": "/var/lib/forge/projects/shared-a",
    "appmilla/shared": "/var/lib/forge/projects/shared-b",
}


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    cx = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    yield cx
    cx.close()


class _Recorder:
    def __init__(self) -> None:
        self.builds: list[BuildQueuedPayload] = []

    async def __call__(self, build: BuildQueuedPayload) -> None:
        self.builds.append(build)


def _deps(
    connection: sqlite3.Connection,
    *,
    default: str | None = "guardkit/api_test",
    publish: Any = None,
    notify: AsyncMock | None = None,
    queue: bool = True,
) -> PlanningConsumerDeps:
    return PlanningConsumerDeps(
        store=SqlitePlanningRunStore(connection),
        publish_notification=notify if notify is not None else AsyncMock(),
        on_recorded=AsyncMock(),
        planning_config=PlanningConfig(
            target_repo_paths=dict(PATHS), default_target_repo=default
        ),
        queue_store=WorkQueueStore(connection) if queue else None,
        publish_build_queued=publish,
    )


def _msg(
    *,
    target: str | None = None,
    command: dict[str, Any] | None = None,
    correlation_id: str = CID,
    parent_request_id: str | None = THREAD,
) -> AsyncMock:
    payload = {
        "stage": "planning",
        "request_text": f"build: {FEATURE} from {BRANCH}",
        "target_repo": target,
        "triggered_by": "jarvis",
        "originating_adapter": "slack",
        "originating_user": USER,
        "correlation_id": correlation_id,
        "parent_request_id": parent_request_id,
        "retry_count": 0,
        "requested_at": datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc).isoformat(),
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "queue_command": command
        or {"verb": "build", "feature_id": FEATURE, "branch": BRANCH},
    }
    envelope = MessageEnvelope(
        source_id="jarvis",
        event_type=EventType.BUILD_QUEUED,
        correlation_id=correlation_id,
        payload=payload,
    )
    msg = AsyncMock()
    msg.data = envelope.model_dump_json().encode("utf-8")
    return msg


def _said(notify: AsyncMock) -> list[str]:
    return [call.args[1] for call in notify.await_args_list]


def _nothing_written(connection: sqlite3.Connection) -> None:
    assert connection.execute("SELECT COUNT(*) FROM planning_runs").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM work_queue").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Resolved targets: one build request with the canonical repository
# ---------------------------------------------------------------------------


class TestAResolvedTargetPublishesOneBuildRequest:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("typed", "canonical"),
        [
            (None, "guardkit/api_test"),  # omitted: the default
            ("doc_probe", "guardkit/doc_probe"),  # short
            ("guardkit/doc_probe", "guardkit/doc_probe"),  # canonical
        ],
    )
    async def test_it_carries_the_canonical_repository_and_the_thread(
        self, connection: sqlite3.Connection, typed: str | None, canonical: str
    ) -> None:
        publish, notify = _Recorder(), AsyncMock()
        msg = _msg(target=typed)
        await handle_planning_message(
            msg, _deps(connection, publish=publish, notify=notify)
        )

        (build,) = publish.builds
        assert build.repo == canonical
        assert build.feature_id == FEATURE
        assert build.branch == BRANCH
        assert build.feature_yaml_path == f".guardkit/features/{FEATURE}.yaml"
        assert build.triggered_by == "jarvis"
        assert build.originating_adapter == "slack"
        assert build.originating_user == USER
        assert build.parent_request_id == THREAD
        assert build.correlation_id == f"{CID}{BUILD_HANDOVER_CORRELATION_SUFFIX}"
        assert build.correlation_id != CID
        assert build.mode == "mode-a" and build.task_id is None
        # Nothing said here: the build route answers in the thread.
        assert _said(notify) == []
        msg.ack.assert_awaited_once()
        _nothing_written(connection)

    @pytest.mark.asyncio
    async def test_it_works_without_a_queue_wired_in(
        self, connection: sqlite3.Connection
    ) -> None:
        publish = _Recorder()
        await handle_planning_message(
            _msg(target="api_test"), _deps(connection, publish=publish, queue=False)
        )
        assert [b.repo for b in publish.builds] == ["guardkit/api_test"]
        assert (
            connection.execute("SELECT COUNT(*) FROM planning_runs").fetchone()[0] == 0
        )

    @pytest.mark.asyncio
    async def test_a_redelivery_is_the_same_build_request(
        self, connection: sqlite3.Connection
    ) -> None:
        # Fresh for every Slack message, the same for a redelivery of one:
        # the build route's duplicate checks then see one build.
        publish = _Recorder()
        deps = _deps(connection, publish=publish)
        await handle_planning_message(_msg(), deps)
        await handle_planning_message(_msg(), deps)
        await handle_planning_message(_msg(correlation_id="plan-handover-0002"), deps)
        ids = [b.correlation_id for b in publish.builds]
        assert ids[0] == ids[1] != ids[2]

    @pytest.mark.asyncio
    async def test_the_published_bytes_are_what_the_build_consumer_reads(self) -> None:
        sent: list[tuple[str, bytes]] = []

        class _Client:
            async def publish(self, subject: str, body: bytes) -> None:
                sent.append((subject, body))

        build = BuildQueuedPayload(
            feature_id=FEATURE,
            repo="guardkit/api_test",
            branch=BRANCH,
            feature_yaml_path=f".guardkit/features/{FEATURE}.yaml",
            triggered_by="jarvis",
            originating_adapter="slack",
            originating_user=USER,
            correlation_id="plan-x-build",
            parent_request_id=THREAD,
            requested_at=datetime.now(timezone.utc),
            queued_at=datetime.now(timezone.utc),
        )
        await publish_build_request(_Client(), build)
        ((subject, body),) = sent
        assert subject == f"pipeline.build-queued.{FEATURE}"
        envelope = MessageEnvelope.model_validate_json(body)
        assert envelope.event_type == EventType.BUILD_QUEUED
        assert envelope.correlation_id == "plan-x-build"
        assert BuildQueuedPayload.model_validate(envelope.payload) == build


# ---------------------------------------------------------------------------
# Unresolved targets: the sentence's own answer, and nothing published
# ---------------------------------------------------------------------------


class TestAnUnresolvedTargetIsAnsweredLikeASentence:
    @pytest.mark.asyncio
    async def test_an_unknown_name_gets_the_sentences_reply(
        self, connection: sqlite3.Connection
    ) -> None:
        publish, notify = _Recorder(), AsyncMock()
        msg = _msg(target="nowhere")
        await handle_planning_message(
            msg, _deps(connection, publish=publish, notify=notify)
        )
        assert publish.builds == []
        assert _said(notify) == [unknown_repo_message("nowhere", PATHS)]
        assert notify.await_args_list[0].kwargs["parent_request_id"] == THREAD
        msg.ack.assert_awaited_once()
        # Unlike a sentence, no planning run is recorded and failed for it.
        _nothing_written(connection)

    @pytest.mark.asyncio
    async def test_an_unknown_canonical_name_gets_the_sentences_reply(
        self, connection: sqlite3.Connection
    ) -> None:
        publish, notify = _Recorder(), AsyncMock()
        await handle_planning_message(
            _msg(target="guardkit/nowhere"),
            _deps(connection, publish=publish, notify=notify),
        )
        assert publish.builds == []
        assert _said(notify) == [unknown_repo_message("guardkit/nowhere", PATHS)]

    @pytest.mark.asyncio
    async def test_an_ambiguous_name_is_asked_about(
        self, connection: sqlite3.Connection
    ) -> None:
        publish, notify = _Recorder(), AsyncMock()
        await handle_planning_message(
            _msg(target="shared"), _deps(connection, publish=publish, notify=notify)
        )
        assert publish.builds == []
        assert _said(notify) == [
            ambiguous_repo_message("shared", ("guardkit/shared", "appmilla/shared"))
        ]

    @pytest.mark.asyncio
    async def test_the_same_names_answer_a_sentence_the_same_way(
        self, connection: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """The hand-over's answer IS the sentence's answer, word for word."""
        hand_over, sentence = AsyncMock(), AsyncMock()
        await handle_planning_message(
            _msg(target="nowhere"),
            _deps(connection, publish=_Recorder(), notify=hand_over),
        )
        cx = sqlite_connect.connect_writer(tmp_path / "sentence.db")
        migrations.apply_at_boot(cx)
        try:
            msg = _msg(target="nowhere", correlation_id="plan-sentence-1")
            body = MessageEnvelope.model_validate_json(msg.data)
            body.payload.pop("queue_command")
            msg.data = body.model_dump_json().encode("utf-8")
            await handle_planning_message(msg, _deps(cx, notify=sentence, queue=False))
        finally:
            cx.close()
        # The sentence's refusal is the owner message its failed run sends.
        assert (
            _said(sentence)
            == _said(hand_over)
            == [unknown_repo_message("nowhere", PATHS)]
        )

    @pytest.mark.asyncio
    async def test_no_name_and_no_default_says_so(
        self, connection: sqlite3.Connection
    ) -> None:
        publish, notify = _Recorder(), AsyncMock()
        await handle_planning_message(
            _msg(target=None),
            _deps(connection, default=None, publish=publish, notify=notify),
        )
        assert publish.builds == []
        assert _said(notify) == [no_default_repo_message(PATHS)]

    @pytest.mark.asyncio
    async def test_a_default_that_is_not_configured_is_not_used(
        self, connection: sqlite3.Connection
    ) -> None:
        publish, notify = _Recorder(), AsyncMock()
        await handle_planning_message(
            _msg(target=None),
            _deps(connection, default="guardkit/gone", publish=publish, notify=notify),
        )
        assert publish.builds == []
        assert _said(notify) == [unknown_repo_message("guardkit/gone", PATHS)]


# ---------------------------------------------------------------------------
# What cannot be handed over is answered, never lost and never looped
# ---------------------------------------------------------------------------


class TestWhatCannotBeHandedOver:
    @pytest.mark.asyncio
    async def test_a_feature_id_the_wire_refuses_is_answered(
        self, connection: sqlite3.Connection
    ) -> None:
        publish, notify = _Recorder(), AsyncMock()
        msg = _msg(command={"verb": "build", "feature_id": "FEAT-x", "branch": BRANCH})
        await handle_planning_message(
            msg, _deps(connection, publish=publish, notify=notify)
        )
        assert publish.builds == []
        (said,) = _said(notify)
        assert (
            "nothing was started" in said and "build: FEAT-XXXX from <branch>" in said
        )
        msg.ack.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_failed_publish_is_answered_and_acknowledged(
        self, connection: sqlite3.Connection
    ) -> None:
        notify = AsyncMock()
        msg = _msg()
        await handle_planning_message(
            msg,
            _deps(
                connection,
                publish=AsyncMock(side_effect=ConnectionError("down")),
                notify=notify,
            ),
        )
        (said,) = _said(notify)
        assert said.startswith(f"{FEATURE} was not started:")
        msg.ack.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_publisher_wired_is_answered(
        self, connection: sqlite3.Connection
    ) -> None:
        notify = AsyncMock()
        msg = _msg()
        await handle_planning_message(
            msg, _deps(connection, publish=None, notify=notify)
        )
        (said,) = _said(notify)
        assert said.startswith(f"{FEATURE} was not started:")
        msg.ack.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_other_queue_commands_are_unchanged(
        self, connection: sqlite3.Connection
    ) -> None:
        publish, notify = _Recorder(), AsyncMock()
        await handle_planning_message(
            _msg(command={"verb": "list"}),
            _deps(connection, publish=publish, notify=notify),
        )
        assert publish.builds == []
        assert _said(notify) == ["Nothing in the queue."]

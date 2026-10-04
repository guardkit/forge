"""A build refused before its row is written still reaches the work queue.

Review finding R6 (3 October 2026): the pipeline refuses some builds before it
writes any build row — an originator that is not approved, a feature file
outside the allowed folders, a repository the sandbox policy will not build —
and acknowledges the message. Nothing durable said so, and a sentence waiting
"after" the refused one waited for ever without being asked.

Each check here drives the real consumer, wired the way ``forge serve`` wires
it, against a real migrated database: the refusal is noted on the queue row,
the waiting row is asked "hold or go" once, and unrelated work goes ahead. A
build that was handed over and simply not delivered yet keeps the waiting row
waiting, and nobody is asked.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from nats_core.envelope import EventType, MessageEnvelope

from forge.adapters.nats.pipeline_consumer import handle_message
from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli._serve_deps import build_pipeline_consumer_deps
from forge.config.models import ForgeConfig
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.planning.states import PlanningState
from forge.planning.work_queue_loop import LOOP_ACTOR
from forge.planning.work_queue_store import BUILD_REJECTED_ACTION, WorkQueueStore
from tests.forge.planning.test_work_queue_concurrency import a_loop
from tests.forge.planning.test_work_queue_loop import (
    USER,
    FakeClock,
    Notifier,
    _insert_run,
    file_row,
)

CID_A = "corr-refused-a"


class _StubNatsClient:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []

    async def publish(self, subject: str, body: bytes, **_: Any) -> Any:
        self.published.append((subject, body))
        return None


class _Starter:
    def start_async_task(self, subagent_name: str, context: dict) -> str:
        raise AssertionError("a refused build never reaches a runner")

    async def astart_async_task(self, subagent_name: str, context: dict) -> str:
        raise AssertionError("a refused build never reaches a runner")


class _Message:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.acks = 0

    async def ack(self) -> None:
        self.acks += 1

    async def nak(self) -> None:
        raise AssertionError("a refusal acknowledges; it never asks again")


@pytest.fixture()
def cx(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(connection)
    yield connection
    connection.close()


def _config(tmp_path: Path, how: str) -> tuple[ForgeConfig, Path]:
    allowed = tmp_path / "allowed"
    allowed.mkdir(exist_ok=True)
    feature_yaml = allowed / "feature.yaml"
    if how == "path":
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir(exist_ok=True)
        feature_yaml = elsewhere / "feature.yaml"
    feature_yaml.write_text("name: synthetic\n", encoding="utf-8")
    raw: dict[str, Any] = {"permissions": {"filesystem": {"allowlist": [str(allowed)]}}}
    if how == "originator":
        raw["pipeline"] = {"approved_originators": ["jarvis"]}
    if how == "sandbox":
        raw["planning"] = {
            "target_repo_paths": {
                "example/plain": str(tmp_path / "plain"),
                "example/sandboxed": str(tmp_path / "sandboxed"),
            },
            "sandboxes": {
                "example/sandboxed": {
                    "name": "sandboxed",
                    "sidecar_url": "http://sandbox:8125",
                    "runner_url": "http://sandbox:8124",
                }
            },
        }
        raw["publication"] = {"builds_may_run_inside_the_coordinator": False}
    return ForgeConfig.model_validate(raw), feature_yaml


def _message(feature_yaml: Path, correlation_id: str) -> _Message:
    now = datetime(2026, 10, 3, tzinfo=UTC)
    payload = {
        "feature_id": "FEAT-REFUSED",
        "repo": "example/plain",
        "branch": "main",
        "feature_yaml_path": str(feature_yaml),
        "max_turns": 5,
        "sdk_timeout_seconds": 1800,
        "triggered_by": "cli",
        "originating_adapter": "cli-wrapper",
        "originating_user": "synthetic-user",
        "correlation_id": correlation_id,
        "requested_at": now.isoformat(),
        "queued_at": now.isoformat(),
        "mode": "mode-a",
    }
    envelope = MessageEnvelope(
        message_id="msg-refused",
        timestamp=now,
        version="1.0",
        source_id="cli-wrapper",
        event_type=EventType.BUILD_QUEUED,
        correlation_id=correlation_id,
        payload=payload,
    )
    return _Message(envelope.model_dump_json().encode("utf-8"))


def _a_handed_over_with_b_after_it(
    cx: sqlite3.Connection,
) -> tuple[WorkQueueStore, int, int, int]:
    """A's planning run handed its build over; B waits after A; C is unrelated."""
    store = WorkQueueStore(cx)
    a_id = file_row(store, CID_A)
    b_id = file_row(store, "corr-refused-b")
    c_id = file_row(store, "corr-refused-c")
    assert store.link(b_id, a_id, actor_identity=USER)
    store.admit(a_id, actor_identity=LOOP_ACTOR)
    _insert_run(cx, CID_A, PlanningState.BUILD_QUEUED.value)
    store.close(a_id, status="DONE", actor_identity=LOOP_ACTOR)
    return store, a_id, b_id, c_id


def _another_build_of_the_feature_is_running(
    persistence: SqliteLifecyclePersistence, feature_yaml: Path
) -> None:
    """An earlier build of FEAT-REFUSED, unrelated to the queue, is running."""
    from nats_core.events import BuildQueuedPayload

    other = json.loads(_message(feature_yaml, "corr-other-running").data)["payload"]
    other["queued_at"] = other["requested_at"] = "2026-10-02T09:00:00+00:00"
    build_id = persistence.record_pending_build(
        BuildQueuedPayload.model_validate(other)
    )
    persistence.connection.execute(
        "UPDATE builds SET status = 'RUNNING' WHERE build_id = ?", (build_id,)
    )


@pytest.mark.parametrize(
    ("how", "words"),
    [
        ("originator", "it was sent by cli-wrapper, which is not approved"),
        ("path", "is outside the folders builds may read"),
        ("sandbox", "sandbox-required"),
        # 3 October 2026, round 2 (R6): another build of the same feature is
        # already running, so this one is refused before its row is written.
        ("same-feature", "another build of FEAT-REFUSED is already in progress"),
    ],
)
@pytest.mark.asyncio
async def test_a_refused_build_asks_hold_or_go_once(
    tmp_path: Path, cx: sqlite3.Connection, how: str, words: str
) -> None:
    store, a_id, b_id, c_id = _a_handed_over_with_b_after_it(cx)
    config, feature_yaml = _config(tmp_path, how)
    persistence = SqliteLifecyclePersistence(connection=cx)
    if how == "same-feature":
        _another_build_of_the_feature_is_running(persistence, feature_yaml)
    deps = build_pipeline_consumer_deps(
        _StubNatsClient(),
        config,
        persistence,
        async_task_starter=_Starter(),
    )

    refused = _message(feature_yaml, CID_A)
    await handle_message(refused, deps)

    assert refused.acks == 1
    assert (
        cx.execute(
            "SELECT COUNT(*) FROM builds WHERE correlation_id = ?", (CID_A,)
        ).fetchone()[0]
        == 0
    )
    assert store.has_event(a_id, BUILD_REJECTED_ACTION)

    notifier = Notifier()
    loop = a_loop(cx, limit=2, clock=FakeClock(), notifier=notifier)
    await loop.ask_hold_or_go()
    await loop.ask_hold_or_go()

    assert len(notifier.messages) == 1
    said = notifier.messages[0]
    assert said.startswith(f"#{a_id}'s build was refused before it started (")
    assert words in said
    assert said.endswith(f"and #{b_id} was waiting on it — hold or go?")

    # Held: unrelated work goes ahead; B goes once someone puts it next.
    assert await loop.take_next() == c_id
    assert store.get(b_id)["status"] == "QUEUED"
    if how == "same-feature":
        # The other running build counts as work in flight (limit 2, with C);
        # it finishes before B is put next.
        cx.execute(
            "UPDATE builds SET status = 'COMPLETE' WHERE correlation_id = ?",
            ("corr-other-running",),
        )
    store.promote(b_id, actor_identity=USER)
    assert await loop.take_next() == b_id


@pytest.mark.asyncio
async def test_a_redelivered_refusal_is_noted_once(
    tmp_path: Path, cx: sqlite3.Connection
) -> None:
    store, a_id, _b_id, _c_id = _a_handed_over_with_b_after_it(cx)
    config, feature_yaml = _config(tmp_path, "originator")
    deps = build_pipeline_consumer_deps(
        _StubNatsClient(),
        config,
        SqliteLifecyclePersistence(connection=cx),
        async_task_starter=_Starter(),
    )

    await handle_message(_message(feature_yaml, CID_A), deps)
    await handle_message(_message(feature_yaml, CID_A), deps)

    noted = [
        event
        for event in store.list_events(a_id)
        if event["action"] == BUILD_REJECTED_ACTION
    ]
    assert len(noted) == 1


@pytest.mark.asyncio
async def test_a_build_queued_and_not_yet_delivered_keeps_b_waiting(
    cx: sqlite3.Connection,
) -> None:
    store, _a_id, b_id, c_id = _a_handed_over_with_b_after_it(cx)
    notifier = Notifier()
    loop = a_loop(cx, limit=2, clock=FakeClock(), notifier=notifier)

    await loop.ask_hold_or_go()

    assert notifier.messages == []
    assert await loop.take_next() == c_id
    assert store.get(b_id)["status"] == "QUEUED"


def test_a_refusal_for_work_the_queue_never_filed_writes_nothing(
    cx: sqlite3.Connection,
) -> None:
    store = WorkQueueStore(cx)

    assert store.record_build_rejection("corr-never-filed", "refused") is False
    assert cx.execute("SELECT COUNT(*) FROM work_queue_events").fetchone()[0] == 0


@pytest.mark.parametrize("how", ["originator", "path", "sandbox", "same-feature"])
@pytest.mark.asyncio
async def test_a_note_that_cannot_be_written_holds_the_refusal_for_redelivery(
    tmp_path: Path, cx: sqlite3.Connection, how: str
) -> None:
    """Round 3 (R6): a locked database must not lose the refusal. The message
    is left unacknowledged and nothing is reported; its redelivery writes the
    note once, is acknowledged, and the waiting row is asked once."""
    store, a_id, b_id, _c_id = _a_handed_over_with_b_after_it(cx)
    config, feature_yaml = _config(tmp_path, how)
    persistence = SqliteLifecyclePersistence(connection=cx)
    if how == "same-feature":
        _another_build_of_the_feature_is_running(persistence, feature_yaml)
    failures = {"left": 1}

    def locked_once(correlation_id: str, reason: str) -> bool:
        if failures["left"]:
            failures["left"] -= 1
            raise sqlite3.OperationalError("database is locked")
        return WorkQueueStore(cx).record_build_rejection(correlation_id, reason)

    client = _StubNatsClient()
    deps = build_pipeline_consumer_deps(
        client,
        config,
        persistence,
        async_task_starter=_Starter(),
        record_build_rejection=locked_once,
    )

    held = _message(feature_yaml, CID_A)
    await handle_message(held, deps)

    assert held.acks == 0
    assert not store.has_event(a_id, BUILD_REJECTED_ACTION)
    assert client.published == []

    redelivered = _message(feature_yaml, CID_A)
    await handle_message(redelivered, deps)

    assert redelivered.acks == 1
    noted = [
        event
        for event in store.list_events(a_id)
        if event["action"] == BUILD_REJECTED_ACTION
    ]
    assert len(noted) == 1

    notifier = Notifier()
    loop = a_loop(cx, limit=2, clock=FakeClock(), notifier=notifier)
    await loop.ask_hold_or_go()
    await loop.ask_hold_or_go()
    assert len(notifier.messages) == 1
    assert notifier.messages[0].endswith(
        f"and #{b_id} was waiting on it — hold or go?"
    )


def test_a_database_with_no_work_queue_has_nothing_to_note(tmp_path: Path) -> None:
    """No queue at all is "nothing to note", not a failure: the refusal is
    acknowledged as before."""
    bare = sqlite_connect.connect_writer(tmp_path / "bare.db")
    try:
        assert WorkQueueStore(bare).record_build_rejection("corr-x", "refused") is False
    finally:
        bare.close()

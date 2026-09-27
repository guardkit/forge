"""Strict admission must distinguish unreadable identity from confirmed absence."""

from contextlib import contextmanager
from datetime import UTC, datetime
import json
import sqlite3
from unittest.mock import AsyncMock

import pytest

from forge.adapters.nats.pipeline_consumer import (
    ReconcileDeps,
    ReconcileReport,
    _reconcile_one_redelivery,
    handle_message,
)
from forge.cli._serve_deps import build_pipeline_consumer_deps
from forge.cli._serve_production import (
    _interrupt_and_reset_to_preparing,
    _read_build_state_by_identity,
)
from forge.lifecycle.state_machine import BuildState
from nats_core.envelope import EventType, MessageEnvelope
from tests.cli.test_serve_deps import (
    TestSandboxOnlyCommonDispatch as _DispatchFixtures,
    persistence,
    stub_client,
    writer_db,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry,state",
    [("normal", state) for state in (
        "QUEUED", "INTERRUPTED", "PREPARING", "RUNNING", "FINALISING", "PAUSED", None
    )] + [("runless", state) for state in ("QUEUED", "INTERRUPTED", None)],
)
@pytest.mark.parametrize("fault", [None, sqlite3.OperationalError, AttributeError])
async def test_initial_identity_read_holds_until_successful(
    tmp_path, persistence, stub_client, monkeypatch, entry, state, fault
):
    """Use real state queries and dispatch; fail only its initial identity query."""
    now = datetime(2026, 9, 27, tzinfo=UTC)
    feature_yaml = tmp_path / "feature.yaml"
    feature_yaml.write_text("name: synthetic\n", encoding="utf-8")
    payload = dict(
        feature_id="FEAT-D4READ", repo="example/plain", branch="main",
        feature_yaml_path=str(feature_yaml), max_turns=5, sdk_timeout_seconds=1800,
        triggered_by="cli", originating_adapter="cli-wrapper",
        originating_user="synthetic", correlation_id="corr-d4-read",
        requested_at=now.isoformat(), queued_at=now.isoformat(), mode="mode-a",
    )
    if state is not None:
        persistence.connection.execute(
            "INSERT INTO builds (build_id, feature_id, repo, branch, "
            "feature_yaml_path, status, triggered_by, correlation_id, queued_at, "
            "mode) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("build-d4-read", payload["feature_id"], payload["repo"], "main",
             str(feature_yaml), state, "cli", payload["correlation_id"],
             now.isoformat(), "mode-a"),
        )
        persistence.connection.commit()
    conductor = AsyncMock(side_effect=AssertionError("conductor reached"))
    consumer = build_pipeline_consumer_deps(
        stub_client, _DispatchFixtures._strict_config(tmp_path), persistence,
        async_task_starter=_DispatchFixtures._Starter(), conductor_router=conductor,
    )
    queries, injected = [], []
    original_reader = persistence._reader

    class ReaderProxy:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, *args):
            queries.append(sql)
            if (fault is not None and not injected and
                    sql.startswith("SELECT build_id, status FROM builds WHERE")):
                injected.append(sql)
                raise fault("synthetic one-shot initial identity read failure")
            return self.connection.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    @contextmanager
    def reader():
        with original_reader() as connection:
            yield ReaderProxy(connection)

    async def read_state(feature_id, correlation_id):
        return _read_build_state_by_identity(
            persistence, feature_id=feature_id, correlation_id=correlation_id
        )

    async def reset(feature_id, correlation_id):
        return _interrupt_and_reset_to_preparing(
            persistence, feature_id=feature_id, correlation_id=correlation_id
        )

    reconcile = ReconcileDeps(
        consumer_deps=consumer, fetch_redeliveries=AsyncMock(),
        read_build_state=read_state, mark_interrupted_and_reset=reset,
        iter_paused_builds=AsyncMock(), publish_build_paused=AsyncMock(),
        publish_approval_request=AsyncMock(),
    )
    envelope = MessageEnvelope(
        message_id="msg-d4-read", timestamp=now, version="1.0",
        source_id="cli-wrapper", event_type=EventType.BUILD_QUEUED,
        correlation_id=payload["correlation_id"], payload=payload,
    )

    class Message:
        data = envelope.model_dump_json().encode()

        def __init__(self):
            self.acks = self.naks = 0

        async def ack(self):
            self.acks += 1

        async def nak(self):
            self.naks += 1

    async def deliver(message):
        if entry == "normal":
            await handle_message(message, consumer)
        else:
            await _reconcile_one_redelivery(message, reconcile, ReconcileReport(), {})

    monkeypatch.setattr(persistence, "_reader", reader)
    message = Message()
    await deliver(message)
    if entry == "runless":
        assert queries[0].startswith("SELECT status FROM builds WHERE")
    assert any(q.startswith("SELECT build_id, status FROM builds WHERE") for q in queries)
    if fault is not None:
        assert len(injected) == 1
        assert message.acks == message.naks == 0
        assert stub_client.published == []
        row = persistence.get_build_row("build-d4-read")
        if state is None:
            assert row is None
        else:
            assert row.status.value == state
            assert row.error is None
        # A subsequent healthy delivery must still distinguish ownership,
        # runless settlement, and a positively absent row.
        message = Message()
        await deliver(message)
        assert len(injected) == 1
    else:
        assert injected == []

    row = persistence.get_build_row("build-d4-read")
    if state is not None and entry == "normal":
        assert row.status.value == state and row.error is None
        assert message.acks == 0 and stub_client.published == []
    else:
        assert message.acks == 1 and len(stub_client.published) == 1
        event = json.loads(stub_client.published[0][1])
        assert event["correlation_id"] == payload["correlation_id"]
        assert event["payload"]["recoverable"] is False
        assert "sandbox-required" in event["payload"]["failure_reason"]
        if state is None:
            assert row is None
            assert event["payload"]["build_id"] == ""
            assert persistence.connection.execute(
                "SELECT COUNT(*) FROM builds"
            ).fetchone()[0] == 0
        else:
            assert row.status is BuildState.FAILED
            assert row.error == event["payload"]["failure_reason"]
            assert event["payload"]["build_id"] == row.build_id
    assert message.naks == 0
    conductor.assert_not_called()

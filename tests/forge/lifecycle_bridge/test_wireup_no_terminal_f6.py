"""F6 regression — stream-ends-without-terminal terminalises the ledger.

Defect harvest 2026-07-26 (F6, "ledger terminal lag"): a build whose SSE
stream closed WITHOUT a terminal envelope — and whose fetch-on-empty
recovery could not surface a terminal state — left ``builds.status``
RUNNING until the 300s per-build deadline timer fired. ``forge status``
misreported a finished build as still RUNNING for up to five minutes.

The wireup now publishes a synthetic ``build-failed`` (reason
``stream-ended-without-terminal``) through the SAME ``_publish_event``
path the live SSE branch uses, so the ``build_state_recorder`` write-back
flips the row to FAILED promptly. These tests exercise that end to end
against a real migrated SQLite ``builds`` table:

* the RUNNING row is walked to FAILED and the inbound is acked;
* an already-terminal (CLI-cancelled) row is NOT resurrected — the
  synthetic envelope still goes on the wire (a duplicate Slack failure
  post is acceptable) but the recorder respects the earlier verdict, and
  the build (genuinely terminal) is still acked;
* the per-build deadline timer is cancelled by the synthetic terminal's
  ``detach``, so it does not fire a SECOND synthetic build-failed later.

The one condition that stays un-acked (a terminal that WAS observed but
whose publish failed) lives in ``test_publish_failure.py`` — the
JetStream publish-retry contract must not be broken by F6.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import AsyncIterator, Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from langgraph_sdk.schema import StreamPart
from nats_core.events import (
    BuildCancelledPayload,
    BuildFailedPayload,
    BuildQueuedPayload,
    BuildStartedPayload,
)

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations as lifecycle_migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.lifecycle.state_machine import BuildState
from forge.lifecycle_bridge.bridge import LifecycleBridge
from forge.lifecycle_bridge.build_state_recorder import build_build_state_recorder
from forge.lifecycle_bridge.translation import StreamEventTranslator
from forge.lifecycle_bridge.wireup import (
    STREAM_NO_TERMINAL_FAILURE_REASON,
    LifecycleBridgeWireup,
)
from forge.persistence.migrations import lifecycle_bridge_registry as bridge_migration
from forge.persistence.repositories.bridge_registry import BridgeRegistry
from forge.pipeline.build_ack_handle import BuildAckHandle

_FEATURE_ID = "FEAT-F6LAG"
_CORRELATION_ID = "corr-f6"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def writer_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db_path = tmp_path / "forge.db"
    cx = sqlite_connect.connect_writer(db_path)
    lifecycle_migrations.apply_at_boot(cx)
    bridge_migration.apply(cx)
    try:
        yield cx
    finally:
        cx.close()


@pytest.fixture()
def persistence(
    writer_db: sqlite3.Connection, tmp_path: Path
) -> SqliteLifecyclePersistence:
    return SqliteLifecyclePersistence(
        connection=writer_db, db_path=tmp_path / "forge.db"
    )


@pytest.fixture()
def registry(writer_db: sqlite3.Connection) -> BridgeRegistry:
    return BridgeRegistry(connection=writer_db)


@pytest.fixture()
def translator() -> StreamEventTranslator:
    return StreamEventTranslator()


@pytest.fixture()
def fake_publisher() -> MagicMock:
    pub = MagicMock(name="PipelinePublisher")
    for name in (
        "publish_build_started",
        "publish_stage_complete",
        "publish_build_complete",
        "publish_build_failed",
        "publish_build_paused",
        "publish_build_resumed",
        "publish_build_cancelled",
        "publish_build_progress",
    ):
        setattr(pub, name, AsyncMock(name=name))
    return pub


def _make_handle() -> BuildAckHandle:
    handle = AsyncMock(spec=BuildAckHandle)
    handle.ack = AsyncMock()
    handle.nak = AsyncMock()
    return handle


def _empty_stream_source():
    """Stream source whose iterator yields zero events (Signature C shape)."""

    def factory(*, feature_id, thread_id, run_id):
        async def gen() -> AsyncIterator[StreamPart]:
            return
            yield  # unreachable, makes this an async generator

        return gen()

    return factory


def _identity_resolved(thread_id: str = "thread-x", run_id: str = "run-x"):
    async def _provider(_feature_id: str) -> tuple[str, str] | None:
        return (thread_id, run_id)

    return _provider


def _queued_build(persistence: SqliteLifecyclePersistence) -> str:
    now = datetime.now(UTC)
    payload = BuildQueuedPayload(
        feature_id=_FEATURE_ID,
        repo="appmilla/api_test",
        feature_yaml_path=".guardkit/features/FEAT-F6.yaml",
        triggered_by="cli",
        correlation_id=_CORRELATION_ID,
        requested_at=now,
        queued_at=now,
    )
    return persistence.record_pending_build(payload)


def _row_status(persistence: SqliteLifecyclePersistence, build_id: str) -> str:
    row = persistence.connection.execute(
        "SELECT status FROM builds WHERE build_id = ?",
        (build_id,),
    ).fetchone()
    assert row is not None
    return row["status"]


async def _record(persistence: SqliteLifecyclePersistence, event: object) -> None:
    await build_build_state_recorder(persistence)(event)


def _build_wireup(
    *,
    bridge: LifecycleBridge,
    translator: StreamEventTranslator,
    fake_publisher: MagicMock,
    persistence: SqliteLifecyclePersistence,
    build_id: str,
) -> LifecycleBridgeWireup:
    async def _resolver(feature_id: str, correlation_id: str) -> str:
        return build_id

    return LifecycleBridgeWireup(
        bridge=bridge,
        translator=translator,
        publisher=fake_publisher,
        stream_source=_empty_stream_source(),
        identity_provider=_identity_resolved(),
        build_state_recorder=build_build_state_recorder(persistence),
        build_id_resolver=_resolver,
        identity_resolution_attempts=1,
        identity_poll_interval_seconds=0.0,
    )


async def _drain(wireup: LifecycleBridgeWireup, feature_id: str) -> None:
    task = wireup.get_observer_task(feature_id)
    if task is None:
        return
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except asyncio.CancelledError:
        return


# ---------------------------------------------------------------------------
# F6-1 — RUNNING row is walked to FAILED promptly + inbound acked
# ---------------------------------------------------------------------------


class TestNoTerminalFlipsLedgerToFailed:
    @pytest.mark.asyncio
    async def test_running_row_becomes_failed_and_acks(
        self,
        registry: BridgeRegistry,
        translator: StreamEventTranslator,
        fake_publisher: MagicMock,
        persistence: SqliteLifecyclePersistence,
    ) -> None:
        build_id = _queued_build(persistence)
        # Move the row to RUNNING (the "shows RUNNING forever" defect state).
        await _record(
            persistence,
            BuildStartedPayload(
                feature_id=_FEATURE_ID, build_id=build_id, wave_total=1
            ),
        )
        assert _row_status(persistence, build_id) == BuildState.RUNNING.value

        bridge = LifecycleBridge(registry=registry)
        wireup = _build_wireup(
            bridge=bridge,
            translator=translator,
            fake_publisher=fake_publisher,
            persistence=persistence,
            build_id=build_id,
        )
        handle = _make_handle()

        await wireup.register_ack_handle(_FEATURE_ID, _CORRELATION_ID, handle)
        await _drain(wireup, _FEATURE_ID)

        # Synthetic build-failed published through _publish_event, carrying
        # the durable build_id and the F6 reason.
        fake_publisher.publish_build_failed.assert_awaited_once()
        sent = fake_publisher.publish_build_failed.await_args.args[0]
        assert isinstance(sent, BuildFailedPayload)
        assert sent.build_id == build_id
        assert sent.failure_reason == STREAM_NO_TERMINAL_FAILURE_REASON
        assert sent.recoverable is True

        # The write-back walked the row to FAILED — no longer RUNNING.
        assert _row_status(persistence, build_id) == BuildState.FAILED.value
        # Inbound acked (queue slot released).
        handle.ack.assert_awaited_once()

        await wireup.shutdown()


# ---------------------------------------------------------------------------
# F6-2 — an already-terminal (cancelled) row is NOT resurrected
# ---------------------------------------------------------------------------


class TestNoTerminalLosesToAlreadyTerminalRow:
    @pytest.mark.asyncio
    async def test_cancelled_row_stays_cancelled(
        self,
        registry: BridgeRegistry,
        translator: StreamEventTranslator,
        fake_publisher: MagicMock,
        persistence: SqliteLifecyclePersistence,
    ) -> None:
        build_id = _queued_build(persistence)
        # CLI cancel won the race — the row is already terminal.
        await _record(
            persistence,
            BuildCancelledPayload(
                feature_id=_FEATURE_ID,
                build_id=build_id,
                reason="cli cancel",
                cancelled_by="operator",
                cancelled_at=datetime.now(UTC).isoformat(),
                correlation_id=_CORRELATION_ID,
            ),
        )
        assert _row_status(persistence, build_id) == BuildState.CANCELLED.value

        bridge = LifecycleBridge(registry=registry)
        wireup = _build_wireup(
            bridge=bridge,
            translator=translator,
            fake_publisher=fake_publisher,
            persistence=persistence,
            build_id=build_id,
        )
        handle = _make_handle()

        await wireup.register_ack_handle(_FEATURE_ID, _CORRELATION_ID, handle)
        await _drain(wireup, _FEATURE_ID)

        # The synthetic envelope still goes on the wire (a duplicate Slack
        # failure post is acceptable) ...
        fake_publisher.publish_build_failed.assert_awaited_once()
        # ... but the recorder's no-resurrection guard leaves the terminal
        # verdict intact — a wrong ledger state is NOT acceptable.
        assert _row_status(persistence, build_id) == BuildState.CANCELLED.value
        # The build is genuinely terminal, so acking the inbound is correct.
        handle.ack.assert_awaited_once()

        await wireup.shutdown()


# ---------------------------------------------------------------------------
# F6-3 — the per-build deadline timer does not fire a SECOND synthetic failed
# ---------------------------------------------------------------------------


class TestNoTerminalCancelsDeadlineTimer:
    @pytest.mark.asyncio
    async def test_deadline_handler_not_invoked_after_synthetic_terminal(
        self,
        registry: BridgeRegistry,
        translator: StreamEventTranslator,
        fake_publisher: MagicMock,
        persistence: SqliteLifecyclePersistence,
    ) -> None:
        build_id = _queued_build(persistence)
        deadline_handler = AsyncMock(name="deadline_handler")
        # A short per-build deadline: the timer would fire quickly if the
        # synthetic terminal's detach did NOT cancel it.
        bridge = LifecycleBridge(
            registry=registry,
            deadline_handler=deadline_handler,
            deadline_seconds=0.2,
        )
        wireup = _build_wireup(
            bridge=bridge,
            translator=translator,
            fake_publisher=fake_publisher,
            persistence=persistence,
            build_id=build_id,
        )
        handle = _make_handle()

        await wireup.register_ack_handle(_FEATURE_ID, _CORRELATION_ID, handle)
        await _drain(wireup, _FEATURE_ID)

        # F6 fired immediately; wait past the deadline window.
        await asyncio.sleep(0.4)

        # The synthetic build-failed published exactly once — the deadline
        # timer was cancelled by _on_terminal's detach, so no second one.
        fake_publisher.publish_build_failed.assert_awaited_once()
        deadline_handler.assert_not_awaited()

        await wireup.shutdown()

"""TIMEOUT TRUTH — the class survives from the runner to the durable row.

THE RESIDUE (Sunday handoff §4.4, "a TIMEOUT status distinct from FAILED").
Five structurally different terminal causes — a semantic monitor kill, a
budget-cap kill, a runner wall-clock expiry, a guardkit in-band SDK timeout,
and an ordinary broken build — all arrived downstream as one word: ``FAILED``.
"It ran out of time" and "it is broken" are opposite verdicts with opposite
next actions, and nothing but a prose string carried the difference.

These tests drive the WHOLE chain the way the cap-kill tests next door do:
the runner's OWN snapshot shape → the real translator → the real wireup →
a real migrated SQLite row. No broker, no network — an in-process stream
source and a publisher double.

The two controls are as load-bearing as the positive cases:

* an ORDINARY failure must write nothing at all (the ``error`` class is never
  stamped, so the row keeps a NULL ``terminal_class``), and
* ``builds.status`` must read exactly ``FAILED`` in every case — this lane
  adds a distinction BESIDE the status, it never mints a new one.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import AsyncIterator, Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from langgraph_sdk.schema import StreamPart
from nats_core.events import BuildQueuedPayload

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations as lifecycle_migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.lifecycle.state_machine import BuildState
from forge.lifecycle_bridge.bridge import LifecycleBridge
from forge.lifecycle_bridge.build_state_recorder import build_build_state_recorder
from forge.lifecycle_bridge.translation import (
    VALUES_STREAM_EVENT,
    StreamEventTranslator,
)
from forge.lifecycle_bridge.wireup import LifecycleBridgeWireup
from forge.persistence.migrations import lifecycle_bridge_registry as bridge_migration
from forge.persistence.repositories.bridge_registry import BridgeRegistry
from forge.pipeline.build_ack_handle import BuildAckHandle
from forge.subagents import build_monitor as bm

_FEATURE_ID = "FEAT-TCLASS"
_CORRELATION_ID = "corr-tclass"


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


def _identity_resolved():
    async def _provider(
        _feature_id: str, _correlation_id: str = ""
    ) -> tuple[str, str] | None:
        return ("thread-x", "run-x")

    return _provider


def _queued_build(persistence: SqliteLifecyclePersistence) -> str:
    now = datetime.now(UTC)
    return persistence.record_pending_build(
        BuildQueuedPayload(
            feature_id=_FEATURE_ID,
            repo="appmilla/api_test",
            feature_yaml_path=f".guardkit/features/{_FEATURE_ID}.yaml",
            triggered_by="cli",
            correlation_id=_CORRELATION_ID,
            requested_at=now,
            queued_at=now,
        )
    )


def _state_part(build_id: str, *, lifecycle: str) -> StreamPart:
    return StreamPart(
        event=VALUES_STREAM_EVENT,
        data={
            "async_tasks": {
                _FEATURE_ID: {
                    "feature_id": _FEATURE_ID,
                    "build_id": build_id,
                    "lifecycle": lifecycle,
                    "wave_total": 1,
                    "wave_index": 0,
                    "task_index": 0,
                    "tasks_completed": 0,
                    "tasks_failed": 0,
                    "waiting_for": None,
                    "last_coach_score": None,
                }
            }
        },
        id=None,
    )


def _runner_failed_part(build_id: str, *, terminal_class: str | None) -> StreamPart:
    """A terminal failed part built from the RUNNER'S OWN snapshot builder.

    Deliberately not a hand-rolled fixture: any drift in the runner's snapshot
    shape has to break here rather than pass silently.
    """
    from forge.subagents import autobuild_runner as ar

    snap = ar._build_failed_snapshot(
        {
            "feature_id": _FEATURE_ID,
            "build_id": build_id,
            "correlation_id": _CORRELATION_ID,
        },
        reason="guardkit autobuild exit=2",
        terminal_class=terminal_class,
    )
    return StreamPart(
        event=VALUES_STREAM_EVENT,
        data={"async_tasks": {_FEATURE_ID: snap}},
        id=None,
    )


def _parts(build_id: str, *, terminal_class: str | None) -> list[StreamPart]:
    return [
        _state_part(build_id, lifecycle="starting"),
        _state_part(build_id, lifecycle="running_wave"),
        _runner_failed_part(build_id, terminal_class=terminal_class),
    ]


def _make_stream_source(parts: list[StreamPart]):
    def factory(*, feature_id, thread_id, run_id):
        async def gen() -> AsyncIterator[StreamPart]:
            for part in parts:
                yield part
                await asyncio.sleep(0)

        return gen()

    return factory


#: Distinguishes "the test did not ask" (wire the real recorder) from an
#: explicit ``None`` (prove the un-wired no-op).
_UNSET = object()


def _build_wireup(
    *,
    registry: BridgeRegistry,
    fake_publisher: MagicMock,
    persistence: SqliteLifecyclePersistence,
    build_id: str,
    parts: list[StreamPart],
    terminal_class_recorder=_UNSET,
) -> LifecycleBridgeWireup:
    async def _resolver(feature_id: str, correlation_id: str) -> str:
        return build_id

    if terminal_class_recorder is _UNSET:
        terminal_class_recorder = persistence.record_terminal_class

    return LifecycleBridgeWireup(
        bridge=LifecycleBridge(registry=registry),
        translator=StreamEventTranslator(),
        publisher=fake_publisher,
        stream_source=_make_stream_source(parts),
        identity_provider=_identity_resolved(),
        build_state_recorder=build_build_state_recorder(persistence),
        build_id_resolver=_resolver,
        identity_resolution_attempts=1,
        identity_poll_interval_seconds=0.0,
        terminal_class_recorder=terminal_class_recorder,
    )


async def _drain(wireup: LifecycleBridgeWireup) -> None:
    task = wireup.get_observer_task(_FEATURE_ID)
    if task is None:
        return
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except asyncio.CancelledError:
        return


def _row_status(persistence: SqliteLifecyclePersistence, build_id: str) -> str:
    row = persistence.connection.execute(
        "SELECT status FROM builds WHERE build_id = ?", (build_id,)
    ).fetchone()
    assert row is not None
    return row["status"]


# ---------------------------------------------------------------------------
# The chain
# ---------------------------------------------------------------------------


class TestTheClassReachesTheRow:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "terminal_class",
        [
            bm.TERMINAL_CLASS_WEDGE,
            bm.TERMINAL_CLASS_BUDGET_CAP,
            bm.TERMINAL_CLASS_WALL_CLOCK,
            bm.TERMINAL_CLASS_IN_BAND,
        ],
    )
    async def test_each_timeout_class_lands_durably(
        self,
        registry: BridgeRegistry,
        fake_publisher: MagicMock,
        persistence: SqliteLifecyclePersistence,
        terminal_class: str,
    ) -> None:
        build_id = _queued_build(persistence)
        wireup = _build_wireup(
            registry=registry,
            fake_publisher=fake_publisher,
            persistence=persistence,
            build_id=build_id,
            parts=_parts(build_id, terminal_class=terminal_class),
        )
        handle = _make_handle()
        await wireup.register_ack_handle(_FEATURE_ID, _CORRELATION_ID, handle)
        await _drain(wireup)

        assert persistence.read_terminal_class(build_id) == terminal_class
        # THE LAW OF THIS LANE: the status vocabulary is untouched.
        assert _row_status(persistence, build_id) == BuildState.FAILED.value
        # The terminal flow is otherwise byte-identical.
        fake_publisher.publish_build_failed.assert_awaited_once()
        handle.ack.assert_awaited_once()

        await wireup.shutdown()

    @pytest.mark.asyncio
    async def test_an_ordinary_failure_writes_nothing(
        self,
        registry: BridgeRegistry,
        fake_publisher: MagicMock,
        persistence: SqliteLifecyclePersistence,
    ) -> None:
        """THE CONTROL. NULL is the honest read for 'not classified'."""
        build_id = _queued_build(persistence)
        wireup = _build_wireup(
            registry=registry,
            fake_publisher=fake_publisher,
            persistence=persistence,
            build_id=build_id,
            parts=_parts(build_id, terminal_class=None),
        )
        handle = _make_handle()
        await wireup.register_ack_handle(_FEATURE_ID, _CORRELATION_ID, handle)
        await _drain(wireup)

        assert persistence.read_terminal_class(build_id) is None
        assert _row_status(persistence, build_id) == BuildState.FAILED.value
        fake_publisher.publish_build_failed.assert_awaited_once()

        await wireup.shutdown()

    @pytest.mark.asyncio
    async def test_a_publish_failure_still_leaves_the_truth(
        self,
        registry: BridgeRegistry,
        fake_publisher: MagicMock,
        persistence: SqliteLifecyclePersistence,
    ) -> None:
        """Ordering is the point: RECORD, then publish.

        This mirrors the cap-kill marker's contract. A terminal whose publish
        blew up is exactly the build an operator most needs the truth about,
        and the SQL is first-write-wins so the JetStream redelivery's
        re-record is a no-op.
        """
        build_id = _queued_build(persistence)
        fake_publisher.publish_build_failed = AsyncMock(
            side_effect=RuntimeError("transport down")
        )
        wireup = _build_wireup(
            registry=registry,
            fake_publisher=fake_publisher,
            persistence=persistence,
            build_id=build_id,
            parts=_parts(build_id, terminal_class=bm.TERMINAL_CLASS_IN_BAND),
        )
        handle = _make_handle()
        await wireup.register_ack_handle(_FEATURE_ID, _CORRELATION_ID, handle)
        await _drain(wireup)

        assert persistence.read_terminal_class(build_id) == (
            bm.TERMINAL_CLASS_IN_BAND
        ), "the class must be recorded BEFORE the publish, not after it"

        await wireup.shutdown()

    @pytest.mark.asyncio
    async def test_a_recorder_fault_never_breaks_the_stream(
        self,
        registry: BridgeRegistry,
        fake_publisher: MagicMock,
        persistence: SqliteLifecyclePersistence,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The build still fails exactly as it failed; only the name is lost."""
        build_id = _queued_build(persistence)

        def _boom(_build_id: str, _terminal_class: str) -> None:
            raise RuntimeError("injected recorder failure")

        wireup = _build_wireup(
            registry=registry,
            fake_publisher=fake_publisher,
            persistence=persistence,
            build_id=build_id,
            parts=_parts(build_id, terminal_class=bm.TERMINAL_CLASS_WEDGE),
            terminal_class_recorder=_boom,
        )
        handle = _make_handle()
        with caplog.at_level(logging.ERROR, logger="forge.lifecycle_bridge.wireup"):
            await wireup.register_ack_handle(_FEATURE_ID, _CORRELATION_ID, handle)
            await _drain(wireup)

        fake_publisher.publish_build_failed.assert_awaited_once()
        handle.ack.assert_awaited_once()
        assert _row_status(persistence, build_id) == BuildState.FAILED.value
        assert any(
            "recorder raised" in record.getMessage() for record in caplog.records
        )

        await wireup.shutdown()

    @pytest.mark.asyncio
    async def test_no_recorder_wired_is_a_quiet_noop(
        self,
        registry: BridgeRegistry,
        fake_publisher: MagicMock,
        persistence: SqliteLifecyclePersistence,
    ) -> None:
        """Unit tiers and un-opted-in deployments behave exactly as before.

        Unlike a cap-kill with no observer — which is an enforcement hole and
        shouts at ERROR — an unrecorded class costs a column in a status table
        and nothing else, so it stays quiet.
        """
        build_id = _queued_build(persistence)
        wireup = _build_wireup(
            registry=registry,
            fake_publisher=fake_publisher,
            persistence=persistence,
            build_id=build_id,
            parts=_parts(build_id, terminal_class=bm.TERMINAL_CLASS_WEDGE),
            terminal_class_recorder=None,
        )
        handle = _make_handle()
        await wireup.register_ack_handle(_FEATURE_ID, _CORRELATION_ID, handle)
        await _drain(wireup)

        assert persistence.read_terminal_class(build_id) is None
        fake_publisher.publish_build_failed.assert_awaited_once()
        handle.ack.assert_awaited_once()

        await wireup.shutdown()

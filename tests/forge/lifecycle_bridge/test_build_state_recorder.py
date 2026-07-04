"""Tests for ``forge.lifecycle_bridge.build_state_recorder``.

The recorder is the bridge's builds-row write-back seam (2026-07-04 GB10
gap: envelopes published + inbound acked, but the row stayed ``QUEUED``
past terminal, wedging the feature's next dispatch on the Group C
"active in-flight duplicate" check). Coverage:

* payload type → target-state mapping over a real migrated SQLite db
  (``build-started`` → RUNNING, terminals → COMPLETE/FAILED/CANCELLED
  with the legal multi-hop chain applied);
* per-payload field carry (``pr_url``, ``failure_reason``/``reason`` →
  ``error``) and the ``started_at``/``completed_at`` invariants;
* idempotency on redelivery and "no resurrection from terminal" when
  the CLI cancel path won the race;
* unmapped payload types and unknown build_ids are silent/warned no-ops;
* the wireup hook: recorder fires only after a successful publish, and
  a recorder exception never flips the publish result.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from nats_core.events import (
    BuildCancelledPayload,
    BuildCompletePayload,
    BuildFailedPayload,
    BuildQueuedPayload,
    BuildStartedPayload,
)

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations as lifecycle_migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.lifecycle.state_machine import BuildState
from forge.lifecycle_bridge.build_state_recorder import build_build_state_recorder

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_FEATURE_ID = "FEAT-BSR1"


@pytest.fixture()
def persistence(tmp_path: Path) -> Iterator[SqliteLifecyclePersistence]:
    db_path = tmp_path / "forge.db"
    cx = sqlite_connect.connect_writer(db_path)
    lifecycle_migrations.apply_at_boot(cx)
    try:
        yield SqliteLifecyclePersistence(connection=cx, db_path=db_path)
    finally:
        cx.close()


def _queued_build(persistence: SqliteLifecyclePersistence) -> str:
    """Insert one QUEUED builds row and return its build_id."""
    now = datetime.now(UTC)
    payload = BuildQueuedPayload(
        feature_id=_FEATURE_ID,
        repo="appmilla/api_test",
        feature_yaml_path=".guardkit/features/FEAT-BSR1.yaml",
        triggered_by="cli",
        correlation_id="corr-bsr-1",
        requested_at=now,
        queued_at=now,
    )
    return persistence.record_pending_build(payload)


def _row(persistence: SqliteLifecyclePersistence, build_id: str) -> sqlite3.Row:
    row = persistence.connection.execute(
        "SELECT status, started_at, completed_at, error, pr_url "
        "FROM builds WHERE build_id = ?",
        (build_id,),
    ).fetchone()
    assert row is not None
    return row


def _record(persistence: SqliteLifecyclePersistence, event: object) -> None:
    recorder = build_build_state_recorder(persistence)
    asyncio.run(recorder(event))


# ---------------------------------------------------------------------------
# Payload → state mapping
# ---------------------------------------------------------------------------


class TestRecorderStateMapping:
    def test_build_started_moves_queued_row_to_running(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        build_id = _queued_build(persistence)

        _record(
            persistence,
            BuildStartedPayload(
                feature_id=_FEATURE_ID, build_id=build_id, wave_total=1
            ),
        )

        row = _row(persistence, build_id)
        assert row["status"] == BuildState.RUNNING.value
        assert row["started_at"] is not None
        assert row["completed_at"] is None

    def test_build_complete_from_queued_walks_chain_to_complete(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        """A fast run's first envelope can be terminal — full chain applies."""
        build_id = _queued_build(persistence)

        _record(
            persistence,
            BuildCompletePayload(
                feature_id=_FEATURE_ID,
                build_id=build_id,
                tasks_completed=20,
                tasks_failed=0,
                tasks_total=20,
                duration_seconds=393.0,
                summary="all coach turns approved",
                pr_url="https://example.test/pr/9",
            ),
        )

        row = _row(persistence, build_id)
        assert row["status"] == BuildState.COMPLETE.value
        assert row["started_at"] is not None
        assert row["completed_at"] is not None
        assert row["pr_url"] == "https://example.test/pr/9"

    def test_build_failed_records_failure_reason_as_error(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        build_id = _queued_build(persistence)

        _record(
            persistence,
            BuildFailedPayload(
                feature_id=_FEATURE_ID,
                build_id=build_id,
                failure_reason="coach rejected turn 5",
                recoverable=False,
            ),
        )

        row = _row(persistence, build_id)
        assert row["status"] == BuildState.FAILED.value
        assert row["error"] == "coach rejected turn 5"
        assert row["completed_at"] is not None

    def test_build_cancelled_records_reason_as_error(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        build_id = _queued_build(persistence)

        _record(
            persistence,
            BuildCancelledPayload(
                feature_id=_FEATURE_ID,
                build_id=build_id,
                reason="operator cancel",
                cancelled_by="richardwoollcott",
                cancelled_at=datetime.now(UTC).isoformat(),
                correlation_id="corr-bsr-1",
            ),
        )

        row = _row(persistence, build_id)
        assert row["status"] == BuildState.CANCELLED.value
        assert row["error"] == "operator cancel"

    def test_started_then_complete_uses_running_chain(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        build_id = _queued_build(persistence)
        started = BuildStartedPayload(
            feature_id=_FEATURE_ID, build_id=build_id, wave_total=1
        )
        _record(persistence, started)

        _record(
            persistence,
            BuildCompletePayload(
                feature_id=_FEATURE_ID,
                build_id=build_id,
                tasks_completed=1,
                tasks_failed=0,
                tasks_total=1,
                duration_seconds=10.0,
                summary="ok",
            ),
        )

        assert _row(persistence, build_id)["status"] == BuildState.COMPLETE.value


# ---------------------------------------------------------------------------
# Idempotency / no-op guards
# ---------------------------------------------------------------------------


class TestRecorderGuards:
    def test_redelivered_terminal_is_a_noop(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        build_id = _queued_build(persistence)
        complete = BuildCompletePayload(
            feature_id=_FEATURE_ID,
            build_id=build_id,
            tasks_completed=1,
            tasks_failed=0,
            tasks_total=1,
            duration_seconds=10.0,
            summary="ok",
        )
        _record(persistence, complete)
        _record(persistence, complete)  # must not raise / must not change

        assert _row(persistence, build_id)["status"] == BuildState.COMPLETE.value

    def test_terminal_row_is_never_resurrected(
        self, persistence: SqliteLifecyclePersistence, caplog: pytest.LogCaptureFixture
    ) -> None:
        """CLI cancel won the race — a late build-complete must not fight it."""
        build_id = _queued_build(persistence)
        _record(
            persistence,
            BuildCancelledPayload(
                feature_id=_FEATURE_ID,
                build_id=build_id,
                reason="cli cancel",
                cancelled_by="operator",
                cancelled_at=datetime.now(UTC).isoformat(),
                correlation_id="corr-bsr-1",
            ),
        )

        with caplog.at_level(
            logging.INFO, logger="forge.lifecycle_bridge.build_state_recorder"
        ):
            _record(
                persistence,
                BuildCompletePayload(
                    feature_id=_FEATURE_ID,
                    build_id=build_id,
                    tasks_completed=1,
                    tasks_failed=0,
                    tasks_total=1,
                    duration_seconds=10.0,
                    summary="ok",
                ),
            )

        assert _row(persistence, build_id)["status"] == BuildState.CANCELLED.value
        assert any("already terminal" in r.getMessage() for r in caplog.records)

    def test_unmapped_payload_type_is_ignored(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        build_id = _queued_build(persistence)

        class _UnmappedPayload:
            pass

        _record(persistence, _UnmappedPayload())

        assert _row(persistence, build_id)["status"] == BuildState.QUEUED.value

    def test_unknown_build_id_warns_and_returns(
        self, persistence: SqliteLifecyclePersistence, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(
            logging.WARNING, logger="forge.lifecycle_bridge.build_state_recorder"
        ):
            _record(
                persistence,
                BuildStartedPayload(
                    feature_id=_FEATURE_ID,
                    build_id="build-PHANTOM",
                    wave_total=1,
                ),
            )
        assert any("no builds row" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Wireup hook — recorder fires after successful publish only
# ---------------------------------------------------------------------------


def _make_wireup(recorder, publisher) -> "object":
    from forge.lifecycle_bridge.bridge import LifecycleBridge
    from forge.lifecycle_bridge.translation import StreamEventTranslator
    from forge.lifecycle_bridge.wireup import LifecycleBridgeWireup
    from forge.persistence.migrations import (
        lifecycle_bridge_registry as bridge_migration,
    )
    from forge.persistence.repositories.bridge_registry import BridgeRegistry

    cx = sqlite3.connect(":memory:")
    cx.row_factory = sqlite3.Row
    bridge_migration.apply(cx)
    return LifecycleBridgeWireup(
        bridge=LifecycleBridge(registry=BridgeRegistry(connection=cx)),
        translator=StreamEventTranslator(),
        publisher=publisher,
        stream_source=lambda *a, **k: iter(()),
        build_state_recorder=recorder,
    )


def _started_event() -> BuildStartedPayload:
    return BuildStartedPayload(
        feature_id=_FEATURE_ID, build_id="build-hook-1", wave_total=1
    )


class TestWireupInvokesRecorder:
    def test_recorder_called_after_successful_publish(self) -> None:
        recorder = AsyncMock()
        publisher = MagicMock()
        publisher.publish_build_started = AsyncMock()
        wireup = _make_wireup(recorder, publisher)
        event = _started_event()

        ok = asyncio.run(wireup._publish_event(event, _FEATURE_ID))

        assert ok is True
        recorder.assert_awaited_once_with(event)

    def test_recorder_not_called_when_publish_fails(self) -> None:
        recorder = AsyncMock()
        publisher = MagicMock()
        publisher.publish_build_started = AsyncMock(side_effect=RuntimeError("broker"))
        wireup = _make_wireup(recorder, publisher)

        ok = asyncio.run(wireup._publish_event(_started_event(), _FEATURE_ID))

        assert ok is False
        recorder.assert_not_awaited()

    def test_recorder_exception_does_not_flip_publish_result(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        recorder = AsyncMock(side_effect=RuntimeError("recorder bug"))
        publisher = MagicMock()
        publisher.publish_build_started = AsyncMock()
        wireup = _make_wireup(recorder, publisher)

        with caplog.at_level(logging.WARNING, logger="forge.lifecycle_bridge.wireup"):
            ok = asyncio.run(wireup._publish_event(_started_event(), _FEATURE_ID))

        assert ok is True
        assert any("recorder raised" in r.getMessage() for r in caplog.records)

    def test_no_recorder_configured_is_a_noop(self) -> None:
        publisher = MagicMock()
        publisher.publish_build_started = AsyncMock()
        wireup = _make_wireup(None, publisher)

        ok = asyncio.run(wireup._publish_event(_started_event(), _FEATURE_ID))

        assert ok is True

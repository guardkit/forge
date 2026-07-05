"""TASK-JNB-102 — SqliteRowCancelledNotifier + build_cli_runtime wiring.

The production CLI build-cancelled notifier enriches from the builds row
(``get_build_row``) because the steering handler's snapshot carries
neither ``correlation_id`` nor (on OTHER_RUNNING) ``feature_id``, then
publishes through the injected sync one-shot seam (default:
``forge.cli.queue.publish``).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli.runtime import SqliteRowCancelledNotifier, build_cli_runtime
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from nats_core.events import BuildQueuedPayload


@pytest.fixture()
def persistence(tmp_path: Path) -> Iterator[SqliteLifecyclePersistence]:
    cx = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    yield SqliteLifecyclePersistence(connection=cx, db_path=tmp_path / "forge.db")
    cx.close()


def _seed_build(persistence: SqliteLifecyclePersistence) -> str:
    now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)
    payload = BuildQueuedPayload(
        feature_id="FEAT-N01",
        repo="guardkit/test-project",
        feature_yaml_path="features/feat-n1.yaml",
        triggered_by="cli",
        correlation_id="corr-n1",
        requested_at=now,
        queued_at=now,
    )
    return persistence.record_pending_build(payload)


class _SpyPublish:
    def __init__(self, *, raise_on_call: bool = False) -> None:
        self.calls: list[tuple[str, bytes]] = []
        self._raise = raise_on_call

    def __call__(self, subject: str, body: bytes) -> None:
        if self._raise:
            raise RuntimeError("broker unreachable")
        self.calls.append((subject, body))


class TestSqliteRowCancelledNotifier:
    """Row-lookup enrichment + subject/payload correctness."""

    def test_publishes_enriched_payload_on_canonical_subject(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        build_id = _seed_build(persistence)
        publish = _SpyPublish()
        notifier = SqliteRowCancelledNotifier(persistence, publish=publish)

        notifier.notify_cancelled(
            build_id=build_id, reason="cli cancel", cancelled_by="rich"
        )

        assert len(publish.calls) == 1
        subject, body = publish.calls[0]
        assert subject == "pipeline.build-cancelled.FEAT-N01"
        envelope: dict[str, Any] = json.loads(body)
        assert envelope["source_id"] == "forge"
        assert envelope["correlation_id"] == "corr-n1"
        payload = envelope["payload"]
        assert payload["build_id"] == build_id
        assert payload["feature_id"] == "FEAT-N01"
        assert payload["correlation_id"] == "corr-n1"
        assert payload["reason"] == "cli cancel"
        assert payload["cancelled_by"] == "rich"

    def test_missing_row_skips_publish_with_warning(
        self,
        persistence: SqliteLifecyclePersistence,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        publish = _SpyPublish()
        notifier = SqliteRowCancelledNotifier(persistence, publish=publish)

        with caplog.at_level(logging.WARNING):
            notifier.notify_cancelled(
                build_id="build-unknown", reason="x", cancelled_by="y"
            )

        assert publish.calls == []
        assert any("no builds row" in r.message for r in caplog.records)

    def test_publish_failure_propagates_to_handler_guard(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        # The notifier deliberately lets transport errors raise — the
        # steering handler's DDR-007 guard is the swallow point (proven
        # in tests/forge/test_cli_steering.py).
        build_id = _seed_build(persistence)
        notifier = SqliteRowCancelledNotifier(
            persistence, publish=_SpyPublish(raise_on_call=True)
        )

        with pytest.raises(RuntimeError, match="broker unreachable"):
            notifier.notify_cancelled(build_id=build_id, reason="r", cancelled_by="c")


class TestBuildCliRuntimeWiring:
    """build_cli_runtime wires the production notifier by default."""

    def test_default_runtime_wires_sqlite_row_notifier(self, tmp_path: Path) -> None:
        cx = sqlite_connect.connect_writer(tmp_path / "forge.db")
        migrations.apply_at_boot(cx)
        cx.close()

        runtime = build_cli_runtime(tmp_path / "forge.db")

        notifier = runtime.cli_steering_handler.cancelled_notifier
        assert isinstance(notifier, SqliteRowCancelledNotifier)

    def test_injected_notifier_overrides_default(self, tmp_path: Path) -> None:
        cx = sqlite_connect.connect_writer(tmp_path / "forge.db")
        migrations.apply_at_boot(cx)
        cx.close()

        class _Spy:
            def notify_cancelled(self, **_: Any) -> None: ...

        spy = _Spy()
        runtime = build_cli_runtime(tmp_path / "forge.db", cancelled_notifier=spy)

        assert runtime.cli_steering_handler.cancelled_notifier is spy

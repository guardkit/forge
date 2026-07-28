"""NATS publish-failure non-regression tests (TASK-FRR-PEB-011).

When the bridge's terminal-envelope publish to NATS fails (transient
broker error, network blip, etc.):

* AC-1: The bridge's publish path wraps the publisher call in a
  try/except. On a publish raise, the failure is logged at WARNING
  with payload subject and correlation-id.
* AC-2: SQLite state is **not** updated to "terminal-published" on
  publish failure — the registry row remains in place so the next
  recovery cycle can retry.
* AC-3: The inbound ``build-queued`` ack handle is **not** invoked on
  publish failure — JetStream redelivers, the bridge re-attaches, and
  observation resumes.
* AC-4: Async-failure envelopes (from T3's translator) carry an
  operator-readable ``failure_reason`` of the form
  ``"{ExceptionClass}: {message}"`` (e.g.
  ``"RuntimeError: model output failed Pydantic validation"``).

ADR-ARCH-008 contract: SQLite is the source-of-truth; transient
JetStream failures must not corrupt build state.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import AsyncIterator, Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from langgraph_sdk.schema import StreamPart
from nats_core.events import (
    BuildCompletePayload,
    BuildFailedPayload,
)

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations as lifecycle_migrations
from forge.lifecycle_bridge.bridge import BuildContext, LifecycleBridge
from forge.lifecycle_bridge.translation import (
    StreamEventTranslator,
    VALUES_STREAM_EVENT,
)
from forge.lifecycle_bridge.wireup import (
    DEFAULT_DEADLINE_SECONDS,
    DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    LifecycleBridgeWireup,
)
from forge.persistence.migrations import (
    lifecycle_bridge_registry as bridge_migration,
)
from forge.persistence.repositories.bridge_registry import BridgeRegistry
from forge.pipeline.build_ack_handle import BuildAckHandle


# ---------------------------------------------------------------------------
# Fixtures (mirror tests/forge/lifecycle_bridge/test_wireup.py shape)
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
def registry(writer_db: sqlite3.Connection) -> BridgeRegistry:
    return BridgeRegistry(connection=writer_db)


@pytest.fixture()
def bridge(registry: BridgeRegistry) -> LifecycleBridge:
    return LifecycleBridge(registry=registry)


@pytest.fixture()
def translator() -> StreamEventTranslator:
    return StreamEventTranslator()


@pytest.fixture()
def fake_publisher() -> MagicMock:
    """Build a publisher mock with all eight ``publish_*`` methods wired."""
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


def _state_part(
    feature_id: str,
    *,
    lifecycle: str,
    build_id: str = "build-FEAT-PEB-011-20260507120000",
    wave_total: int = 1,
    wave_index: int = 0,
    task_index: int = 0,
    tasks_completed: int = 0,
    tasks_failed: int = 0,
    waiting_for: str | None = None,
    last_coach_score: float | None = None,
    error_class: str | None = None,
    error_message: str | None = None,
) -> StreamPart:
    snap: dict[str, object] = {
        "feature_id": feature_id,
        "build_id": build_id,
        "lifecycle": lifecycle,
        "wave_total": wave_total,
        "wave_index": wave_index,
        "task_index": task_index,
        "tasks_completed": tasks_completed,
        "tasks_failed": tasks_failed,
        "waiting_for": waiting_for,
        "last_coach_score": last_coach_score,
    }
    if error_class is not None:
        snap["error_class"] = error_class
    if error_message is not None:
        snap["error_message"] = error_message
    return StreamPart(
        event=VALUES_STREAM_EVENT,
        data={"async_tasks": {feature_id: snap}},
        id=None,
    )


def _make_stream_source(parts: list[StreamPart]):
    """Build a StreamSource that yields ``parts`` once per call."""

    def factory(*, feature_id, thread_id, run_id):
        async def gen() -> AsyncIterator[StreamPart]:
            for part in parts:
                yield part
                # Cooperatively yield so the observer's await
                # interleaves with the test's awaits.
                await asyncio.sleep(0)

        return gen()

    return factory


def _identity_provider(thread_id: str = "thread-x", run_id: str = "run-x"):
    async def _provider(
        _feature_id: str, _correlation_id: str = ""
    ) -> tuple[str, str] | None:
        return (thread_id, run_id)

    return _provider


def _build_wireup(
    bridge: LifecycleBridge,
    translator: StreamEventTranslator,
    fake_publisher: MagicMock,
    *,
    parts: list[StreamPart] | None = None,
    deadline_seconds: int = DEFAULT_DEADLINE_SECONDS,
    shutdown_timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
) -> LifecycleBridgeWireup:
    return LifecycleBridgeWireup(
        bridge=bridge,
        translator=translator,
        publisher=fake_publisher,
        stream_source=_make_stream_source(parts or []),
        identity_provider=_identity_provider(),
        deadline_seconds=deadline_seconds,
        identity_resolution_attempts=1,
        identity_poll_interval_seconds=0.0,
        shutdown_timeout_seconds=shutdown_timeout_seconds,
    )


async def _drain_observer(
    wireup: LifecycleBridgeWireup, feature_id: str, *, timeout: float = 1.0
) -> None:
    task = wireup.get_observer_task(feature_id)
    if task is None:
        return
    try:
        await asyncio.wait_for(task, timeout=timeout)
    except asyncio.CancelledError:
        return


# ---------------------------------------------------------------------------
# AC-1 — wrapped publish + WARNING log with subject + correlation_id
# ---------------------------------------------------------------------------


class TestWrappedPublishLogsWarning:
    """AC-1: terminal-publish failure is wrapped + logged at WARNING."""

    @pytest.mark.asyncio
    async def test_terminal_publish_failure_logs_warning_with_subject_and_correlation_id(
        self,
        bridge: LifecycleBridge,
        translator: StreamEventTranslator,
        fake_publisher: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        feature_id = "FEAT-PEB-011-AC1"
        correlation_id = "corr-ac1-001"
        # Terminal publish raises a transient transport error. The
        # wireup must NOT propagate the exception — instead, it logs
        # WARNING with the subject and correlation_id.
        fake_publisher.publish_build_complete.side_effect = RuntimeError(
            "JetStream broker transient error"
        )
        parts = [
            _state_part(feature_id, lifecycle="running_wave"),
            _state_part(
                feature_id,
                lifecycle="completed",
                tasks_completed=1,
            ),
        ]
        wireup = _build_wireup(
            bridge, translator, fake_publisher, parts=parts
        )
        handle = _make_handle()

        with caplog.at_level(
            logging.WARNING, logger="forge.lifecycle_bridge.wireup"
        ):
            await wireup.register_ack_handle(
                feature_id, correlation_id, handle
            )
            await _drain_observer(wireup, feature_id)

        # The publish_build_complete subject is the canonical
        # ``pipeline.build-complete.{feature_id}`` form.
        warning_messages = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
        ]
        assert any(
            f"pipeline.build-complete.{feature_id}" in msg
            and correlation_id in msg
            for msg in warning_messages
        ), (
            "AC-1: WARNING log must include payload subject + correlation_id; "
            f"got: {warning_messages}"
        )

        await wireup.shutdown()


# ---------------------------------------------------------------------------
# AC-2 — SQLite registry row preserved on publish failure
# ---------------------------------------------------------------------------


class TestSqliteStatePreservedOnPublishFailure:
    """AC-2: SQLite row is NOT marked terminal-published on publish failure."""

    @pytest.mark.asyncio
    async def test_terminal_publish_failure_leaves_registry_row_intact(
        self,
        bridge: LifecycleBridge,
        registry: BridgeRegistry,
        translator: StreamEventTranslator,
        fake_publisher: MagicMock,
    ) -> None:
        feature_id = "FEAT-PEB-011-AC2"
        correlation_id = "corr-ac2-001"
        fake_publisher.publish_build_complete.side_effect = RuntimeError(
            "transient broker error"
        )
        parts = [
            _state_part(feature_id, lifecycle="running_wave"),
            _state_part(
                feature_id,
                lifecycle="completed",
                tasks_completed=1,
            ),
        ]
        wireup = _build_wireup(
            bridge, translator, fake_publisher, parts=parts
        )
        handle = _make_handle()

        await wireup.register_ack_handle(
            feature_id, correlation_id, handle
        )
        await _drain_observer(wireup, feature_id)

        # AC-2: registry row remains in place — bridge.detach was NOT
        # called because the terminal publish failed.
        entry = registry.get(feature_id, correlation_id=correlation_id)
        assert entry is not None, (
            "AC-2: SQLite registry row must persist when terminal publish "
            "fails so the next recovery cycle (T9) can retry"
        )
        assert entry.feature_id == feature_id
        assert entry.correlation_id == correlation_id

        await wireup.shutdown()

    @pytest.mark.asyncio
    async def test_terminal_publish_failure_for_build_failed_preserves_row(
        self,
        bridge: LifecycleBridge,
        registry: BridgeRegistry,
        translator: StreamEventTranslator,
        fake_publisher: MagicMock,
    ) -> None:
        """Same non-regression on the failed-lifecycle terminal envelope."""
        feature_id = "FEAT-PEB-011-AC2-FAIL"
        correlation_id = "corr-ac2-fail"
        fake_publisher.publish_build_failed.side_effect = RuntimeError(
            "broker transient on build-failed"
        )
        parts = [
            _state_part(feature_id, lifecycle="running_wave"),
            _state_part(feature_id, lifecycle="failed", tasks_failed=1),
        ]
        wireup = _build_wireup(
            bridge, translator, fake_publisher, parts=parts
        )
        handle = _make_handle()

        await wireup.register_ack_handle(
            feature_id, correlation_id, handle
        )
        await _drain_observer(wireup, feature_id)

        entry = registry.get(feature_id, correlation_id=correlation_id)
        assert entry is not None, (
            "AC-2: build-failed terminal publish failure must also leave "
            "the registry row in place for the recovery sweep"
        )

        await wireup.shutdown()


# ---------------------------------------------------------------------------
# AC-3 — inbound build-queued ack is NOT invoked on publish failure
# ---------------------------------------------------------------------------


class TestInboundAckNotInvokedOnPublishFailure:
    """AC-3: ``build-queued`` ack stays un-invoked on publish failure."""

    @pytest.mark.asyncio
    async def test_terminal_publish_failure_does_not_ack_handle(
        self,
        bridge: LifecycleBridge,
        translator: StreamEventTranslator,
        fake_publisher: MagicMock,
    ) -> None:
        feature_id = "FEAT-PEB-011-AC3"
        correlation_id = "corr-ac3-001"
        fake_publisher.publish_build_complete.side_effect = RuntimeError(
            "transient broker error"
        )
        parts = [
            _state_part(feature_id, lifecycle="running_wave"),
            _state_part(
                feature_id,
                lifecycle="completed",
                tasks_completed=1,
            ),
        ]
        wireup = _build_wireup(
            bridge, translator, fake_publisher, parts=parts
        )
        handle = _make_handle()

        await wireup.register_ack_handle(
            feature_id, correlation_id, handle
        )
        await _drain_observer(wireup, feature_id)

        # AC-3: ack handle MUST NOT be invoked — JetStream redelivery
        # depends on the inbound build-queued message remaining un-acked.
        handle.ack.assert_not_awaited()
        # And nak should not be called either — the consumer's own
        # redelivery (ack_wait expiry) is the recovery path.
        handle.nak.assert_not_awaited()

        await wireup.shutdown()

    @pytest.mark.asyncio
    async def test_successful_terminal_publish_still_acks(
        self,
        bridge: LifecycleBridge,
        translator: StreamEventTranslator,
        fake_publisher: MagicMock,
    ) -> None:
        """Non-regression sanity: success path still acks (AC-3 negative-of-negative)."""
        feature_id = "FEAT-PEB-011-AC3-OK"
        correlation_id = "corr-ac3-ok"
        parts = [
            _state_part(feature_id, lifecycle="running_wave"),
            _state_part(
                feature_id,
                lifecycle="completed",
                tasks_completed=1,
            ),
        ]
        wireup = _build_wireup(
            bridge, translator, fake_publisher, parts=parts
        )
        handle = _make_handle()

        await wireup.register_ack_handle(
            feature_id, correlation_id, handle
        )
        await _drain_observer(wireup, feature_id)

        # Sanity: when terminal publish succeeds, we DO ack — confirming
        # the AC-3 protection above is not a false negative from a
        # never-acking implementation.
        handle.ack.assert_awaited_once()

        await wireup.shutdown()


# ---------------------------------------------------------------------------
# AC-4 — operator-readable failure_reason from async sidecar exception
# ---------------------------------------------------------------------------


class TestAsyncFailureReasonFormat:
    """AC-4: ``BuildFailedPayload.failure_reason`` is ``{Class}: {message}``."""

    def _ctx(self, feature_id: str, correlation_id: str) -> BuildContext:
        return BuildContext(
            feature_id=feature_id,
            thread_id="thread-x",
            run_id="run-x",
            correlation_id=correlation_id,
            deadline_at=datetime.now(UTC) + timedelta(seconds=300),
        )

    def test_failure_reason_formats_exception_class_and_message(self) -> None:
        translator = StreamEventTranslator()
        ctx = self._ctx("FEAT-AC4-001", "corr-ac4-001")
        # First running_wave so the translator has a prior snapshot.
        translator.translate(
            _state_part("FEAT-AC4-001", lifecycle="running_wave"), ctx
        )
        # SSE emits an exception event with RuntimeError.
        out = translator.translate(
            _state_part(
                "FEAT-AC4-001",
                lifecycle="failed",
                tasks_failed=1,
                error_class="RuntimeError",
                error_message="model output failed Pydantic validation",
            ),
            ctx,
        )
        assert isinstance(out, BuildFailedPayload)
        assert (
            out.failure_reason
            == "RuntimeError: model output failed Pydantic validation"
        ), (
            "AC-4: failure_reason must be '{ExceptionClass}: {message}'; "
            f"got: {out.failure_reason!r}"
        )
        assert getattr(out, "correlation_id", None) == "corr-ac4-001"

    def test_failure_reason_falls_back_to_legacy_string_when_metadata_absent(
        self,
    ) -> None:
        """Snapshots without error_class/error_message use the legacy string.

        Non-regression for old runner builds that do not yet forward
        async-failure metadata via the SSE channel.
        """
        translator = StreamEventTranslator()
        ctx = self._ctx("FEAT-AC4-002", "corr-ac4-002")
        translator.translate(
            _state_part("FEAT-AC4-002", lifecycle="running_wave"), ctx
        )
        out = translator.translate(
            _state_part(
                "FEAT-AC4-002",
                lifecycle="failed",
                tasks_failed=1,
            ),
            ctx,
        )
        assert isinstance(out, BuildFailedPayload)
        assert out.failure_reason == "autobuild failed (sse)"

    def test_failure_reason_supports_nested_last_error_shape(self) -> None:
        """Legacy nested ``last_error`` mapping is also accepted (mixed-fleet)."""
        translator = StreamEventTranslator()
        ctx = self._ctx("FEAT-AC4-003", "corr-ac4-003")
        translator.translate(
            _state_part("FEAT-AC4-003", lifecycle="running_wave"), ctx
        )
        # Build the failed snapshot with a nested last_error dict.
        feature_id = "FEAT-AC4-003"
        nested_part = StreamPart(
            event=VALUES_STREAM_EVENT,
            data={
                "async_tasks": {
                    feature_id: {
                        "feature_id": feature_id,
                        "build_id": "build-FEAT-AC4-003-x",
                        "lifecycle": "failed",
                        "wave_total": 1,
                        "wave_index": 0,
                        "task_index": 0,
                        "tasks_completed": 0,
                        "tasks_failed": 1,
                        "waiting_for": None,
                        "last_coach_score": None,
                        "last_error": {
                            "class": "ValueError",
                            "message": "bad config",
                        },
                    }
                }
            },
            id=None,
        )
        out = translator.translate(nested_part, ctx)
        assert isinstance(out, BuildFailedPayload)
        assert out.failure_reason == "ValueError: bad config"


# ---------------------------------------------------------------------------
# Integration: end-to-end async-failure publish carries failure_reason
# ---------------------------------------------------------------------------


class TestAsyncFailureEndToEnd:
    """AC-4 end-to-end: published BuildFailedPayload carries the formatted reason."""

    @pytest.mark.asyncio
    async def test_async_failure_envelope_publish_carries_failure_reason(
        self,
        bridge: LifecycleBridge,
        translator: StreamEventTranslator,
        fake_publisher: MagicMock,
    ) -> None:
        feature_id = "FEAT-AC4-E2E"
        correlation_id = "corr-ac4-e2e"
        parts = [
            _state_part(feature_id, lifecycle="running_wave"),
            _state_part(
                feature_id,
                lifecycle="failed",
                tasks_failed=1,
                error_class="RuntimeError",
                error_message="model output failed Pydantic validation",
            ),
        ]
        wireup = _build_wireup(
            bridge, translator, fake_publisher, parts=parts
        )
        handle = _make_handle()

        await wireup.register_ack_handle(
            feature_id, correlation_id, handle
        )
        await _drain_observer(wireup, feature_id)

        fake_publisher.publish_build_failed.assert_awaited_once()
        sent = fake_publisher.publish_build_failed.await_args.args[0]
        assert isinstance(sent, BuildFailedPayload)
        assert (
            sent.failure_reason
            == "RuntimeError: model output failed Pydantic validation"
        )
        assert getattr(sent, "correlation_id", None) == correlation_id

        await wireup.shutdown()


# ---------------------------------------------------------------------------
# Sanity: BuildCompletePayload (terminal payload) used for AC-1/2/3 flows
# is still constructable on its own — guards against import drift.
# ---------------------------------------------------------------------------


def test_build_complete_payload_imports_clean() -> None:
    payload = BuildCompletePayload(
        feature_id="FEAT-IMPORT-OK",
        build_id="build-x",
        repo=None,
        branch=None,
        tasks_completed=1,
        tasks_failed=0,
        tasks_total=1,
        pr_url=None,
        duration_seconds=0,
        summary="ok",
    )
    assert isinstance(payload, BuildCompletePayload)

"""Tests for ``forge.lifecycle_bridge.wireup`` (TASK-FRR-PEB-004).

Acceptance-criteria coverage map:

* AC-1 — ``register_ack_handle`` calls ``LifecycleBridge.attach`` and
  starts an asyncio observer task that drives the SSE stream:
  :class:`TestRegisterAckHandle`.
* AC-2 — every translated :data:`PipelineEvent` is published via the
  injected :class:`PipelinePublisher` (no in-bridge construction):
  :class:`TestObserverPublishesViaPublisher`.
* AC-3 — ``correlation_id`` is threaded onto every emitted envelope.
  The wireup forwards translator output unchanged; the AST guard for
  the wireup module's call sites lives in
  :mod:`tests.forge.test_pipeline_consumer_correlation_id`.
  Behaviour is exercised here in
  :class:`TestObserverPublishesViaPublisher`.
* AC-4 — On terminal envelope arrival the observer invokes
  ``ack_handle.ack()`` and ``LifecycleBridge.detach``:
  :class:`TestTerminalArrivalAcksAndDetaches`.
* AC-5 — Supervisor remains responsive: registration returns without
  blocking on the SSE stream, and registry queries answer immediately:
  :class:`TestSupervisorResponsiveness`.
* AC-6 — ``shutdown`` cancels every observer task and returns within
  the configured timeout (default 5s):
  :class:`TestShutdownDrainsObservers`.

The §4 ``STREAM_EVENT_SCHEMA`` contract seam test lives in the
sibling :mod:`tests.forge.lifecycle_bridge.test_wireup_seam` file.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import AsyncIterator, Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from langgraph_sdk.schema import StreamPart
from nats_core.events import (
    BuildCancelledPayload,
    BuildCompletePayload,
    BuildFailedPayload,
)

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations as lifecycle_migrations
from forge.lifecycle_bridge.bridge import (
    LifecycleBridge,
)
from forge.lifecycle_bridge.translation import (
    StreamEventTranslator,
    VALUES_STREAM_EVENT,
)
from forge.lifecycle.modes import BuildMode
from forge.lifecycle_bridge.wireup import (
    DEFAULT_DEADLINE_SECONDS,
    DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    IDENTITY_UNRESOLVED_FAILURE_REASON,
    MODE_C_WATCHDOG_STAND_DOWN,
    STREAM_NO_TERMINAL_FAILURE_REASON,
    LifecycleBridgeWireup,
    TERMINAL_PAYLOAD_TYPES,
)
from forge.persistence.migrations import lifecycle_bridge_registry as bridge_migration
from forge.persistence.repositories.bridge_registry import BridgeRegistry
from forge.pipeline.build_ack_handle import BuildAckHandle

# ---------------------------------------------------------------------------
# Fixtures
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
    """Build a publisher mock with all eight publish_* methods wired."""
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
    build_id: str = "build-FEAT-WIRE-001-20260507120000",
    wave_total: int = 1,
    wave_index: int = 0,
    task_index: int = 0,
    tasks_completed: int = 0,
    tasks_failed: int = 0,
    waiting_for: str | None = None,
    last_coach_score: float | None = None,
) -> StreamPart:
    return StreamPart(
        event=VALUES_STREAM_EVENT,
        data={
            "async_tasks": {
                feature_id: {
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
            }
        },
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
    identity: tuple[str, str] | None = ("thread-x", "run-x"),
    deadline_seconds: int = DEFAULT_DEADLINE_SECONDS,
    shutdown_timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    identity_resolution_attempts: int = 1,
) -> LifecycleBridgeWireup:
    return LifecycleBridgeWireup(
        bridge=bridge,
        translator=translator,
        publisher=fake_publisher,
        stream_source=_make_stream_source(parts or []),
        identity_provider=(
            _identity_provider(*identity) if identity is not None else None
        ),
        deadline_seconds=deadline_seconds,
        identity_resolution_attempts=identity_resolution_attempts,
        identity_poll_interval_seconds=0.0,
        shutdown_timeout_seconds=shutdown_timeout_seconds,
    )


async def _drain_observer(
    wireup: LifecycleBridgeWireup, feature_id: str, *, timeout: float = 1.0
) -> None:
    """Wait for the per-feature observer to exit (success or cancel)."""
    task = wireup.get_observer_task(feature_id)
    if task is None:
        return
    try:
        await asyncio.wait_for(task, timeout=timeout)
    except asyncio.CancelledError:
        return


# ---------------------------------------------------------------------------
# AC-1 — register_ack_handle attaches and starts observer
# ---------------------------------------------------------------------------


class TestRegisterAckHandle:
    """AC-1: ``register_ack_handle`` invokes ``attach`` + starts observer."""

    @pytest.mark.asyncio
    async def test_register_writes_registry_row_via_attach(
        self, bridge, registry, translator, fake_publisher
    ) -> None:
        wireup = _build_wireup(bridge, translator, fake_publisher)
        handle = _make_handle()

        await wireup.register_ack_handle("FEAT-AC1", "corr-ac1", handle)

        # Registry row exists immediately after the call returns
        # (AC-1: bridge.attach is synchronous).
        active = registry.list_active(correlation_id="corr-ac1")
        assert len(active) == 1
        assert active[0].feature_id == "FEAT-AC1"
        assert active[0].correlation_id == "corr-ac1"
        # Deadline reflects the configured 300s default.
        delta = active[0].deadline_at - active[0].attached_at
        assert (
            timedelta(seconds=DEFAULT_DEADLINE_SECONDS - 5)
            <= delta
            <= timedelta(seconds=DEFAULT_DEADLINE_SECONDS + 5)
        )

        await wireup.shutdown()

    @pytest.mark.asyncio
    async def test_register_starts_observer_task(
        self, bridge, translator, fake_publisher
    ) -> None:
        wireup = _build_wireup(bridge, translator, fake_publisher)
        handle = _make_handle()

        await wireup.register_ack_handle("FEAT-OBS", "corr-obs", handle)

        task = wireup.get_observer_task("FEAT-OBS")
        assert task is not None
        assert isinstance(task, asyncio.Task)
        assert wireup.active_observer_count() == 1

        await wireup.shutdown()

    @pytest.mark.asyncio
    async def test_duplicate_registration_is_idempotent(
        self, bridge, translator, fake_publisher
    ) -> None:
        wireup = _build_wireup(bridge, translator, fake_publisher)
        h1, h2 = _make_handle(), _make_handle()

        await wireup.register_ack_handle("FEAT-DUP", "corr-dup", h1)
        first_task = wireup.get_observer_task("FEAT-DUP")
        await wireup.register_ack_handle("FEAT-DUP", "corr-dup", h2)
        second_task = wireup.get_observer_task("FEAT-DUP")

        assert first_task is second_task, (
            "duplicate registration must not start a second observer; "
            "the consumer redelivery path relies on the first observer "
            "owning the ack"
        )
        await wireup.shutdown()

    @pytest.mark.asyncio
    async def test_empty_feature_id_raises(
        self, bridge, translator, fake_publisher
    ) -> None:
        wireup = _build_wireup(bridge, translator, fake_publisher)
        with pytest.raises(ValueError, match="feature_id"):
            await wireup.register_ack_handle("", "corr-x", _make_handle())

    @pytest.mark.asyncio
    async def test_empty_correlation_id_raises(
        self, bridge, translator, fake_publisher
    ) -> None:
        wireup = _build_wireup(bridge, translator, fake_publisher)
        with pytest.raises(ValueError, match="correlation_id"):
            await wireup.register_ack_handle("FEAT-X", "", _make_handle())


# ---------------------------------------------------------------------------
# AC-2 + AC-3 — observer publishes via injected publisher with correlation_id
# ---------------------------------------------------------------------------


class TestObserverPublishesViaPublisher:
    """AC-2: every translated event publishes via injected PipelinePublisher.
    AC-3: every emitted envelope carries the inbound correlation_id.
    """

    @pytest.mark.asyncio
    async def test_lifecycle_round_trip_publishes_started_and_complete(
        self, bridge, translator, fake_publisher
    ) -> None:
        feature_id = "FEAT-RT-OK"
        parts = [
            _state_part(feature_id, lifecycle="starting"),
            _state_part(feature_id, lifecycle="running_wave"),
            _state_part(
                feature_id,
                lifecycle="completed",
                tasks_completed=1,
                tasks_failed=0,
            ),
        ]
        wireup = _build_wireup(bridge, translator, fake_publisher, parts=parts)
        handle = _make_handle()

        await wireup.register_ack_handle(feature_id, "corr-rt", handle)
        await _drain_observer(wireup, feature_id)

        fake_publisher.publish_build_started.assert_awaited()
        fake_publisher.publish_build_complete.assert_awaited()

        # AC-3: every published payload carries the inbound correlation_id.
        for call in fake_publisher.publish_build_started.await_args_list:
            payload = call.args[0]
            assert getattr(payload, "correlation_id", None) == "corr-rt"
        for call in fake_publisher.publish_build_complete.await_args_list:
            payload = call.args[0]
            assert getattr(payload, "correlation_id", None) == "corr-rt"

        await wireup.shutdown()

    @pytest.mark.asyncio
    async def test_failure_round_trip_publishes_build_failed(
        self, bridge, translator, fake_publisher
    ) -> None:
        feature_id = "FEAT-RT-FAIL"
        parts = [
            _state_part(feature_id, lifecycle="starting"),
            _state_part(feature_id, lifecycle="running_wave"),
            _state_part(
                feature_id,
                lifecycle="failed",
                tasks_completed=0,
                tasks_failed=1,
            ),
        ]
        wireup = _build_wireup(bridge, translator, fake_publisher, parts=parts)
        handle = _make_handle()

        await wireup.register_ack_handle(feature_id, "corr-fail", handle)
        await _drain_observer(wireup, feature_id)

        fake_publisher.publish_build_failed.assert_awaited()
        sent = fake_publisher.publish_build_failed.await_args.args[0]
        assert isinstance(sent, BuildFailedPayload)
        assert getattr(sent, "correlation_id", None) == "corr-fail"

        await wireup.shutdown()

    @pytest.mark.asyncio
    async def test_publish_failure_does_not_crash_observer(
        self, bridge, translator, fake_publisher
    ) -> None:
        feature_id = "FEAT-PUB-RAISE"
        # First publish (build-started) raises; the observer must
        # log + continue so the terminal arrival still acks the handle.
        fake_publisher.publish_build_started.side_effect = RuntimeError(
            "transient broker error"
        )
        parts = [
            _state_part(feature_id, lifecycle="starting"),
            _state_part(feature_id, lifecycle="running_wave"),
            _state_part(
                feature_id,
                lifecycle="completed",
                tasks_completed=1,
            ),
        ]
        wireup = _build_wireup(bridge, translator, fake_publisher, parts=parts)
        handle = _make_handle()

        await wireup.register_ack_handle(feature_id, "corr-rec", handle)
        await _drain_observer(wireup, feature_id)

        # Terminal still acked — observer survived the mid-stream raise.
        handle.ack.assert_awaited_once()

        await wireup.shutdown()

    @pytest.mark.asyncio
    async def test_translator_exception_is_swallowed(
        self, bridge, fake_publisher
    ) -> None:
        feature_id = "FEAT-XLAT-RAISE"
        bad_translator = MagicMock(spec=StreamEventTranslator)
        # First translate raises; second returns a terminal envelope.
        terminal = BuildCompletePayload(
            feature_id=feature_id,
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
        object.__setattr__(terminal, "correlation_id", "corr-xlat")
        bad_translator.translate.side_effect = [
            RuntimeError("translator bug"),
            terminal,
        ]
        parts = [
            _state_part(feature_id, lifecycle="running_wave"),
            _state_part(feature_id, lifecycle="completed", tasks_completed=1),
        ]
        wireup = LifecycleBridgeWireup(
            bridge=bridge,
            translator=bad_translator,
            publisher=fake_publisher,
            stream_source=_make_stream_source(parts),
            identity_provider=_identity_provider(),
            identity_poll_interval_seconds=0.0,
            identity_resolution_attempts=1,
        )
        handle = _make_handle()
        await wireup.register_ack_handle(feature_id, "corr-xlat", handle)
        await _drain_observer(wireup, feature_id)

        fake_publisher.publish_build_complete.assert_awaited_once()
        handle.ack.assert_awaited_once()
        await wireup.shutdown()


# ---------------------------------------------------------------------------
# AC-4 — terminal arrival acks handle and detaches registry row
# ---------------------------------------------------------------------------


class TestTerminalArrivalAcksAndDetaches:
    """AC-4: terminal envelope triggers ``ack_handle.ack()`` + ``detach``."""

    @pytest.mark.asyncio
    async def test_complete_acks_and_detaches(
        self, bridge, registry, translator, fake_publisher
    ) -> None:
        feature_id = "FEAT-TERM-OK"
        parts = [
            _state_part(feature_id, lifecycle="running_wave"),
            _state_part(feature_id, lifecycle="completed", tasks_completed=1),
        ]
        wireup = _build_wireup(bridge, translator, fake_publisher, parts=parts)
        handle = _make_handle()

        await wireup.register_ack_handle(feature_id, "corr-tx", handle)
        await _drain_observer(wireup, feature_id)

        handle.ack.assert_awaited_once()
        # Registry row deleted by detach.
        assert registry.get(feature_id, correlation_id="corr-tx") is None

        await wireup.shutdown()

    @pytest.mark.asyncio
    async def test_failed_acks_and_detaches(
        self, bridge, registry, translator, fake_publisher
    ) -> None:
        feature_id = "FEAT-TERM-FAIL"
        parts = [
            _state_part(feature_id, lifecycle="running_wave"),
            _state_part(feature_id, lifecycle="failed", tasks_failed=1),
        ]
        wireup = _build_wireup(bridge, translator, fake_publisher, parts=parts)
        handle = _make_handle()

        await wireup.register_ack_handle(feature_id, "corr-tf", handle)
        await _drain_observer(wireup, feature_id)

        handle.ack.assert_awaited_once()
        assert registry.get(feature_id, correlation_id="corr-tf") is None

        await wireup.shutdown()

    @pytest.mark.asyncio
    async def test_cancelled_acks_and_detaches(
        self, bridge, registry, translator, fake_publisher
    ) -> None:
        feature_id = "FEAT-TERM-CANCEL"
        parts = [
            _state_part(feature_id, lifecycle="running_wave"),
            _state_part(feature_id, lifecycle="cancelled"),
        ]
        wireup = _build_wireup(bridge, translator, fake_publisher, parts=parts)
        handle = _make_handle()

        await wireup.register_ack_handle(feature_id, "corr-tc", handle)
        await _drain_observer(wireup, feature_id)

        handle.ack.assert_awaited_once()
        assert registry.get(feature_id, correlation_id="corr-tc") is None

        await wireup.shutdown()

    @pytest.mark.asyncio
    async def test_ack_failure_keeps_registry_row(
        self, bridge, registry, translator, fake_publisher
    ) -> None:
        """If ``ack()`` raises, ``detach`` is skipped so recover_in_flight
        can re-attempt ack on the next boot.
        """
        feature_id = "FEAT-ACK-RAISE"
        parts = [
            _state_part(feature_id, lifecycle="running_wave"),
            _state_part(feature_id, lifecycle="completed", tasks_completed=1),
        ]
        wireup = _build_wireup(bridge, translator, fake_publisher, parts=parts)
        handle = _make_handle()
        handle.ack.side_effect = RuntimeError("ack transport error")

        await wireup.register_ack_handle(feature_id, "corr-ar", handle)
        await _drain_observer(wireup, feature_id)

        handle.ack.assert_awaited_once()
        # detach skipped → row preserved for recovery sweep.
        assert registry.get(feature_id, correlation_id="corr-ar") is not None

        await wireup.shutdown()


# ---------------------------------------------------------------------------
# AC-5 — Supervisor responsiveness: registration is non-blocking
# ---------------------------------------------------------------------------


class TestSupervisorResponsiveness:
    """AC-5: registration returns without blocking; queries answer immediately."""

    @pytest.mark.asyncio
    async def test_register_returns_before_stream_drains(
        self, bridge, translator, fake_publisher
    ) -> None:
        feature_id = "FEAT-SUP-1"

        # Slow stream source: never yields. Observer waits forever.
        async def slow_gen():
            await asyncio.sleep(60)
            yield  # pragma: no cover

        wireup = LifecycleBridgeWireup(
            bridge=bridge,
            translator=translator,
            publisher=fake_publisher,
            stream_source=lambda **_: slow_gen(),
            identity_provider=_identity_provider(),
            identity_poll_interval_seconds=0.0,
            identity_resolution_attempts=1,
        )
        handle = _make_handle()

        # Registration returns quickly — well under 100ms — even
        # though the stream is suspended.
        loop = asyncio.get_event_loop()
        start = loop.time()
        await wireup.register_ack_handle(feature_id, "corr-sup", handle)
        elapsed = loop.time() - start
        assert elapsed < 0.1, (
            f"register_ack_handle blocked for {elapsed:.3f}s; "
            "AC-5 requires the supervisor's call to return immediately"
        )

        # Observer is in flight but supervisor query (active count) is
        # answered in O(1) without touching the SSE stream.
        assert wireup.active_observer_count() == 1

        await wireup.shutdown()

    @pytest.mark.asyncio
    async def test_post_shutdown_registration_raises(
        self, bridge, translator, fake_publisher
    ) -> None:
        wireup = _build_wireup(bridge, translator, fake_publisher)
        await wireup.shutdown()

        with pytest.raises(RuntimeError, match="shutting down"):
            await wireup.register_ack_handle("FEAT-LATE", "corr-late", _make_handle())


# ---------------------------------------------------------------------------
# AC-6 — Shutdown drains observers within timeout
# ---------------------------------------------------------------------------


class TestShutdownDrainsObservers:
    """AC-6: ``shutdown`` cancels every observer and returns ≤ timeout."""

    @pytest.mark.asyncio
    async def test_shutdown_with_three_in_flight_returns_within_5s(
        self, bridge, translator, fake_publisher
    ) -> None:
        async def slow_gen():
            await asyncio.sleep(60)
            yield  # pragma: no cover

        wireup = LifecycleBridgeWireup(
            bridge=bridge,
            translator=translator,
            publisher=fake_publisher,
            stream_source=lambda **_: slow_gen(),
            identity_provider=_identity_provider(),
            identity_poll_interval_seconds=0.0,
            identity_resolution_attempts=1,
        )
        for i in range(3):
            await wireup.register_ack_handle(
                f"FEAT-SHUT-{i}", f"corr-shut-{i}", _make_handle()
            )

        assert wireup.active_observer_count() == 3
        loop = asyncio.get_event_loop()
        start = loop.time()
        await wireup.shutdown()
        elapsed = loop.time() - start

        assert elapsed < 5.0, f"shutdown took {elapsed:.3f}s; AC-6 requires ≤5s"
        assert wireup.active_observer_count() == 0

    @pytest.mark.asyncio
    async def test_shutdown_cancels_observer_tasks(
        self, bridge, translator, fake_publisher
    ) -> None:
        async def slow_gen():
            await asyncio.sleep(60)
            yield  # pragma: no cover

        wireup = LifecycleBridgeWireup(
            bridge=bridge,
            translator=translator,
            publisher=fake_publisher,
            stream_source=lambda **_: slow_gen(),
            identity_provider=_identity_provider(),
            identity_poll_interval_seconds=0.0,
            identity_resolution_attempts=1,
        )
        await wireup.register_ack_handle("FEAT-CXL", "corr-cxl", _make_handle())
        task = wireup.get_observer_task("FEAT-CXL")
        assert task is not None and not task.done()

        await wireup.shutdown()
        assert task.done()

    @pytest.mark.asyncio
    async def test_shutdown_timeout_logs_but_does_not_raise(
        self, bridge, translator, fake_publisher
    ) -> None:
        """A genuinely uncancellable observer must not crash shutdown.

        We construct a stream source that ignores cancellation in a
        ``try: ... except asyncio.CancelledError`` block; the wireup's
        ``asyncio.wait_for`` should time out and log without raising.
        """
        # The observer task itself catches CancelledError (we want to
        # exercise the timeout path) — this is a worst-case smoke test
        # for the AC-6 5s budget.

        async def stubborn_gen():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                # Swallow once, then sleep again to force timeout.
                await asyncio.sleep(60)
            yield  # pragma: no cover

        wireup = LifecycleBridgeWireup(
            bridge=bridge,
            translator=translator,
            publisher=fake_publisher,
            stream_source=lambda **_: stubborn_gen(),
            identity_provider=_identity_provider(),
            identity_poll_interval_seconds=0.0,
            identity_resolution_attempts=1,
            shutdown_timeout_seconds=0.1,
        )
        await wireup.register_ack_handle("FEAT-TMO", "corr-tmo", _make_handle())

        # Should return cleanly via the timeout branch.
        await wireup.shutdown()


# ---------------------------------------------------------------------------
# Note: STREAM_EVENT_SCHEMA seam test lives in test_wireup_seam.py
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# AC-3 cross-module AST guard — wireup module's publish call sites
# ---------------------------------------------------------------------------


class TestWireupAstHasNoDirectPayloadConstructions:
    """AC-3: ``wireup.py`` MUST NOT construct pipeline.* payloads itself.

    The wireup forwards the translator's typed payload unchanged; payload
    construction is the translator's job (T3). A future refactor that
    constructs a ``BuildStartedPayload(...)`` (or similar) inside
    ``wireup.py`` would silently regress the §4 producer/consumer
    contract. This AST walk fails such a regression at lint time.
    """

    def test_wireup_constructs_no_pipeline_payloads(self) -> None:
        import ast

        source_path = (
            Path(__file__).resolve().parents[3]
            / "src"
            / "forge"
            / "lifecycle_bridge"
            / "wireup.py"
        )
        tree = ast.parse(source_path.read_text(encoding="utf-8"))

        forbidden = {
            "BuildStartedPayload",
            "StageCompletePayload",
            "BuildCompletePayload",
            "BuildFailedPayload",
            "BuildPausedPayload",
            "BuildResumedPayload",
            "BuildCancelledPayload",
        }
        offenders: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Name):
                continue
            if func.id in forbidden:
                offenders.append((node.lineno, ast.unparse(node)))

        assert not offenders, (
            "wireup.py constructed pipeline payload(s) directly — that "
            "violates AC-2 (Bridge MUST NOT construct payloads). All "
            "construction is the translator's job (T3). Offenders:\n"
            + "\n".join(f"  line {ln}: {s}" for ln, s in offenders)
        )


# ---------------------------------------------------------------------------
# Module sanity
# ---------------------------------------------------------------------------


class TestModuleSurface:
    """Sanity checks on the public surface of the wireup module."""

    def test_terminal_payload_types_are_complete(self) -> None:
        # AC-4 relies on this tuple being the terminal set; if a new
        # terminal envelope is added to nats_core.events the tuple
        # MUST be updated or builds will never ack.
        for cls in (
            BuildCompletePayload,
            BuildFailedPayload,
            BuildCancelledPayload,
        ):
            assert cls in TERMINAL_PAYLOAD_TYPES

    def test_default_shutdown_timeout_is_5_seconds(self) -> None:
        assert DEFAULT_SHUTDOWN_TIMEOUT_SECONDS == 5.0

    def test_default_deadline_is_300_seconds(self) -> None:
        assert DEFAULT_DEADLINE_SECONDS == 300


# ---------------------------------------------------------------------------
# FWD-002 (WS3-S6) — identity-unresolved-at-deadline publishes build-failed
# ---------------------------------------------------------------------------


def _never_resolves():
    async def _provider(
        _feature_id: str, _correlation_id: str = ""
    ) -> tuple[str, str] | None:
        return None

    return _provider


def _build_identity_unresolved_wireup(
    bridge,
    translator,
    fake_publisher,
    *,
    deadline_seconds: float = 0.15,
    build_id_resolver=None,
):
    return LifecycleBridgeWireup(
        bridge=bridge,
        translator=translator,
        publisher=fake_publisher,
        stream_source=_make_stream_source([]),
        identity_provider=_never_resolves(),
        deadline_seconds=deadline_seconds,  # type: ignore[arg-type]
        identity_resolution_attempts=1,
        identity_poll_interval_seconds=0.01,
        build_id_resolver=build_id_resolver,
    )


class TestIdentityUnresolvedPublishesBuildFailed:
    """FWD-002: a build whose identity never resolves is not a silent loop."""

    @pytest.mark.asyncio
    async def test_deadline_fires_and_publishes_build_failed(
        self, bridge, translator, fake_publisher
    ) -> None:
        # AC-2: identity 404s -> deadline fires -> build-failed envelope.
        async def _resolver(feature_id: str, correlation_id: str) -> str:
            return "build-real-123"

        wireup = _build_identity_unresolved_wireup(
            bridge, translator, fake_publisher, build_id_resolver=_resolver
        )
        handle = _make_handle()

        await wireup.register_ack_handle("FEAT-IDU", "corr-idu", handle)
        await _drain_observer(wireup, "FEAT-IDU", timeout=2.0)

        # A synthetic build-failed was published exactly once ...
        fake_publisher.publish_build_failed.assert_awaited_once()
        sent = fake_publisher.publish_build_failed.await_args.args[0]
        assert isinstance(sent, BuildFailedPayload)
        assert sent.feature_id == "FEAT-IDU"
        assert sent.failure_reason == IDENTITY_UNRESOLVED_FAILURE_REASON
        # ... carrying the DURABLE build_id from the resolver (so the
        # terminal write hits the real queued row, not feature_id).
        assert sent.build_id == "build-real-123"
        # ... and the inbound message was acked (slot released).
        handle.ack.assert_awaited_once()

        await wireup.shutdown()

    @pytest.mark.asyncio
    async def test_no_resolver_falls_back_to_feature_id(
        self, bridge, translator, fake_publisher
    ) -> None:
        # Without a resolver the synthetic terminal still publishes — the
        # no-silent-stuck-build invariant holds regardless of build_id.
        wireup = _build_identity_unresolved_wireup(
            bridge, translator, fake_publisher, build_id_resolver=None
        )
        handle = _make_handle()

        await wireup.register_ack_handle("FEAT-NOR", "corr-nor", handle)
        await _drain_observer(wireup, "FEAT-NOR", timeout=2.0)

        fake_publisher.publish_build_failed.assert_awaited_once()
        sent = fake_publisher.publish_build_failed.await_args.args[0]
        assert sent.build_id == "FEAT-NOR"
        handle.ack.assert_awaited_once()
        await wireup.shutdown()

    @pytest.mark.asyncio
    async def test_identity_resolved_during_wait_is_not_identity_unresolved(
        self, bridge, translator, fake_publisher
    ) -> None:
        # A slow dispatch that surfaces identity DURING the deadline wait
        # must NOT be failed as identity-unresolved — the observer proceeds
        # to stream instead. The (empty) stream then closes cleanly with no
        # terminal, so the F6 no-terminal path fires a build-failed carrying
        # the stream-ended reason (NOT identity-unresolved).
        calls = {"n": 0}

        async def _slow_provider(
            _feature_id: str, _correlation_id: str = ""
        ) -> tuple[str, str] | None:
            calls["n"] += 1
            # First (initial budget) poll misses; a later poll resolves.
            if calls["n"] >= 2:
                return ("thread-late", "run-late")
            return None

        wireup = LifecycleBridgeWireup(
            bridge=bridge,
            translator=translator,
            publisher=fake_publisher,
            stream_source=_make_stream_source([]),
            identity_provider=_slow_provider,
            deadline_seconds=2,
            identity_resolution_attempts=1,
            identity_poll_interval_seconds=0.01,
        )
        handle = _make_handle()

        await wireup.register_ack_handle("FEAT-SLOW", "corr-slow", handle)
        await _drain_observer(wireup, "FEAT-SLOW", timeout=2.0)

        # Identity DID resolve, so the reason is the F6 stream-no-terminal
        # reason, never IDENTITY_UNRESOLVED_FAILURE_REASON.
        fake_publisher.publish_build_failed.assert_awaited_once()
        sent = fake_publisher.publish_build_failed.await_args.args[0]
        assert sent.failure_reason == STREAM_NO_TERMINAL_FAILURE_REASON
        assert sent.failure_reason != IDENTITY_UNRESOLVED_FAILURE_REASON
        handle.ack.assert_awaited_once()
        await wireup.shutdown()

    @pytest.mark.asyncio
    async def test_publish_failure_leaves_message_unacked(
        self, bridge, translator, fake_publisher
    ) -> None:
        # If the synthetic build-failed publish fails, the inbound message
        # is left un-acked (JetStream redelivery / next-boot recovery
        # retries) — never a silent drop.
        fake_publisher.publish_build_failed = AsyncMock(
            side_effect=RuntimeError("broker down")
        )
        wireup = _build_identity_unresolved_wireup(bridge, translator, fake_publisher)
        handle = _make_handle()

        await wireup.register_ack_handle("FEAT-PFAIL", "corr-pfail", handle)
        await _drain_observer(wireup, "FEAT-PFAIL", timeout=2.0)

        handle.ack.assert_not_awaited()
        await wireup.shutdown()


# ---------------------------------------------------------------------------
# FWD-002 mode learning (2026-08-04 drive-5 harvest) — a fix journey is never
# a silent stuck build, so the identity watchdog stands down for mode-c
# ---------------------------------------------------------------------------


class _StubModeReader:
    """In-memory :class:`BuildModeReader` — the §4 read, minus SQLite."""

    def __init__(self, modes: dict[str, BuildMode], *, raises: bool = False) -> None:
        self._modes = modes
        self._raises = raises
        self.calls: list[str] = []

    def get_build_mode(self, build_id: str) -> BuildMode:
        self.calls.append(build_id)
        if self._raises:
            raise sqlite3.OperationalError("database is locked")
        return self._modes.get(build_id, BuildMode.MODE_A)


def _build_mode_aware_wireup(
    bridge,
    translator,
    fake_publisher,
    *,
    mode_reader,
    resolved_build_id: str = "build-FEAT-TST1-20260804102430",
    deadline_seconds: float = 0.15,
):
    async def _resolver(_feature_id: str, _correlation_id: str) -> str:
        return resolved_build_id

    return LifecycleBridgeWireup(
        bridge=bridge,
        translator=translator,
        publisher=fake_publisher,
        stream_source=_make_stream_source([]),
        identity_provider=_never_resolves(),
        deadline_seconds=deadline_seconds,  # type: ignore[arg-type]
        identity_resolution_attempts=1,
        identity_poll_interval_seconds=0.01,
        build_id_resolver=_resolver,
        build_mode_reader=mode_reader,
    )


class TestModeCStandsDownTheIdentityWatchdog:
    """The drive-5 shape: a live fix journey must survive the deadline."""

    @pytest.mark.asyncio
    async def test_mode_c_publishes_no_synthetic_terminal(
        self, bridge, translator, fake_publisher, caplog
    ) -> None:
        # Drive 5 (build-FEAT-TST1-20260804102430): the conductor took the
        # build, dispatched a work leg on an 1800s budget, and the bridge
        # killed it ~90s in because identity — which ONLY the routine
        # sidecar path publishes — never resolved. After the fix the
        # watchdog stands down: no synthetic build-failed, no ack, no
        # terminal write-back, and one loud INFO line saying so.
        recorder = MagicMock()
        mode_reader = _StubModeReader(
            {"build-FEAT-TST1-20260804102430": BuildMode.MODE_C}
        )
        wireup = _build_mode_aware_wireup(
            bridge, translator, fake_publisher, mode_reader=mode_reader
        )
        wireup._build_state_recorder = recorder
        handle = _make_handle()

        with caplog.at_level(logging.INFO, logger="forge.lifecycle_bridge.wireup"):
            await wireup.register_ack_handle("FEAT-TST1", "corr-tst1", handle)
            await _drain_observer(wireup, "FEAT-TST1", timeout=2.0)

        fake_publisher.publish_build_failed.assert_not_awaited()
        handle.ack.assert_not_awaited()
        handle.nak.assert_not_awaited()
        recorder.assert_not_called()
        assert mode_reader.calls == ["build-FEAT-TST1-20260804102430"]
        assert MODE_C_WATCHDOG_STAND_DOWN in caplog.text

        await wireup.shutdown()

    @pytest.mark.asyncio
    async def test_mode_c_never_arms_the_identity_deadline(
        self, bridge, translator, fake_publisher
    ) -> None:
        # The stand-down happens BEFORE the deadline extension, so a
        # mode-c observer exits promptly instead of burning the per-build
        # deadline polling for an identity that can never arrive.
        polls = {"n": 0}

        async def _counting_provider(
            _feature_id: str, _correlation_id: str = ""
        ) -> tuple[str, str] | None:
            polls["n"] += 1
            return None

        async def _resolver(_feature_id: str, _correlation_id: str) -> str:
            return "build-mode-c-deadline"

        wireup = LifecycleBridgeWireup(
            bridge=bridge,
            translator=translator,
            publisher=fake_publisher,
            stream_source=_make_stream_source([]),
            identity_provider=_counting_provider,
            deadline_seconds=30,
            identity_resolution_attempts=1,
            identity_poll_interval_seconds=0.01,
            build_id_resolver=_resolver,
            build_mode_reader=_StubModeReader(
                {"build-mode-c-deadline": BuildMode.MODE_C}
            ),
        )
        handle = _make_handle()

        await wireup.register_ack_handle("FEAT-MCD", "corr-mcd", handle)
        # A 30s deadline would wedge this drain if the extension were armed.
        await _drain_observer(wireup, "FEAT-MCD", timeout=2.0)

        assert polls["n"] == 1
        fake_publisher.publish_build_failed.assert_not_awaited()
        await wireup.shutdown()


class TestRoutinePathIdentityWatchdogUnchanged:
    """FWD-002's protection is load-bearing for routine builds — pin it."""

    @pytest.mark.asyncio
    async def test_mode_a_still_terminalises_at_the_deadline(
        self, bridge, translator, fake_publisher
    ) -> None:
        mode_reader = _StubModeReader({"build-routine-1": BuildMode.MODE_A})
        wireup = _build_mode_aware_wireup(
            bridge,
            translator,
            fake_publisher,
            mode_reader=mode_reader,
            resolved_build_id="build-routine-1",
        )
        handle = _make_handle()

        await wireup.register_ack_handle("FEAT-RTN", "corr-rtn", handle)
        await _drain_observer(wireup, "FEAT-RTN", timeout=2.0)

        fake_publisher.publish_build_failed.assert_awaited_once()
        sent = fake_publisher.publish_build_failed.await_args.args[0]
        assert sent.failure_reason == IDENTITY_UNRESOLVED_FAILURE_REASON
        assert sent.build_id == "build-routine-1"
        handle.ack.assert_awaited_once()
        await wireup.shutdown()

    @pytest.mark.asyncio
    async def test_no_mode_reader_is_pre_lane_behaviour(
        self, bridge, translator, fake_publisher
    ) -> None:
        # An un-migrated caller (mode_reader=None) keeps the watchdog armed
        # for every build — byte-identical to before this lane.
        wireup = _build_identity_unresolved_wireup(
            bridge, translator, fake_publisher, build_id_resolver=None
        )
        handle = _make_handle()

        await wireup.register_ack_handle("FEAT-NMR", "corr-nmr", handle)
        await _drain_observer(wireup, "FEAT-NMR", timeout=2.0)

        fake_publisher.publish_build_failed.assert_awaited_once()
        sent = fake_publisher.publish_build_failed.await_args.args[0]
        assert sent.failure_reason == IDENTITY_UNRESOLVED_FAILURE_REASON
        assert sent.build_id == "FEAT-NMR"
        handle.ack.assert_awaited_once()
        await wireup.shutdown()

    @pytest.mark.asyncio
    async def test_unreadable_row_keeps_the_watchdog_armed(
        self, bridge, translator, fake_publisher, caplog
    ) -> None:
        # §4 posture: an unreadable row must NOT silently disarm a routine
        # build's protection. Fail toward watching, and say so loudly.
        mode_reader = _StubModeReader({}, raises=True)
        wireup = _build_mode_aware_wireup(
            bridge,
            translator,
            fake_publisher,
            mode_reader=mode_reader,
            resolved_build_id="build-unreadable",
        )
        handle = _make_handle()

        with caplog.at_level(logging.ERROR, logger="forge.lifecycle_bridge.wireup"):
            await wireup.register_ack_handle("FEAT-URD", "corr-urd", handle)
            await _drain_observer(wireup, "FEAT-URD", timeout=2.0)

        fake_publisher.publish_build_failed.assert_awaited_once()
        sent = fake_publisher.publish_build_failed.await_args.args[0]
        assert sent.failure_reason == IDENTITY_UNRESOLVED_FAILURE_REASON
        assert "KEEPING the FWD-002 identity watchdog armed" in caplog.text
        handle.ack.assert_awaited_once()
        await wireup.shutdown()

    @pytest.mark.asyncio
    async def test_mode_read_is_off_the_healthy_path(
        self, bridge, translator, fake_publisher
    ) -> None:
        # A build whose identity resolves normally never reaches the
        # watchdog branch, so the mode reader is never consulted — zero
        # extra reads on the healthy path.
        mode_reader = _StubModeReader({})

        async def _resolves(
            _feature_id: str, _correlation_id: str = ""
        ) -> tuple[str, str] | None:
            return ("thread-ok", "run-ok")

        wireup = LifecycleBridgeWireup(
            bridge=bridge,
            translator=translator,
            publisher=fake_publisher,
            stream_source=_make_stream_source([]),
            identity_provider=_resolves,
            deadline_seconds=1,
            identity_resolution_attempts=1,
            identity_poll_interval_seconds=0.01,
            build_mode_reader=mode_reader,
        )
        handle = _make_handle()

        await wireup.register_ack_handle("FEAT-HLT", "corr-hlt", handle)
        await _drain_observer(wireup, "FEAT-HLT", timeout=2.0)

        assert mode_reader.calls == []
        await wireup.shutdown()

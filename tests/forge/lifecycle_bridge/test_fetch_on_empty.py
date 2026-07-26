"""Tests for fetch-on-empty fallback (TASK-REV-PEBR-005 / FOLLOWUP-C-RACE).

Signature C race: ``runs.join_stream`` against a finished run is a live
subscription that returns empty (per ``langgraph_sdk`` 0.3.13 docstring).
The bridge's observer task polls IdentityProvider, then opens
``join_stream`` — by which time placeholder bodies (~16 ms) have already
finished. Final consumer state without the fallback:
``delivered=N, ack_floor=0, 0 outbound envelopes``.

The fix-on-empty fallback (option (e) in TASK-REV-PEBR-005's review):
when the SSE iterator closes empty, the observer asks an injected
:class:`RunStateFetcher` whether the run terminated. If yes, the
fetched state values are replayed through the existing translator —
emitting the canonical ``BuildStartedPayload`` followed by the
terminal payload.

Why this is the chosen fix shape (vs. (a) reorder / (b)
``runs.stream(...)`` / (a') ``stream_resumable=True`` /
(c) sync open in ``register_ack_handle``): the autobuild dispatcher
routes through DeepAgents' ``AsyncSubAgentMiddleware.astart_async_task``
which calls ``runs.create`` with no resumability passthrough — modifying
that middleware is outside ``forge``'s modify-able surface. Fetch-on-empty
closes the race deterministically without touching the dispatch path.

Test coverage:

* AC-FETCH-1 — empty stream + terminal run → BuildStarted + BuildComplete
  published; ack invoked.
* AC-FETCH-2 — empty stream + still-running run (fetcher ``None``) → F6
  synthetic build-failed published + acked (the stream ended cleanly, so
  the run is over; only its terminal signal was lost — terminalise
  promptly rather than wait for the 300s deadline timer).
* AC-FETCH-3 — empty stream + fetcher returns ``None`` (transport error,
  SDK shape drift) → same F6 synthetic build-failed + ack.
* AC-FETCH-4 — empty stream + terminal "failed" run → BuildStarted +
  BuildFailed published; ack invoked.
* AC-FETCH-5 — empty stream + fetcher itself raises → observer logs, treats
  as no-snapshot (defence in depth) and then takes the F6 no-terminal path:
  synthetic build-failed + ack.
* AC-FETCH-6 — fetcher is NOT consulted when the SSE iterator did
  produce a terminal envelope (regression lock against double-acking).
* AC-FETCH-7 — when the fetcher is omitted from the constructor, the
  default no-op fetcher is used; the empty stream then takes the F6
  no-terminal path: synthetic build-failed + ack (additive-only kwarg).

F6 note (2026-07-26 defect harvest): the "empty stream + no recoverable
terminal" branch used to leave the inbound un-acked and rely on JetStream
redelivery + the 300s per-build deadline timer. That left ``builds.status``
RUNNING for up to five minutes for a finished build. The branch now
publishes a synthetic ``build-failed`` (reason
``stream-ended-without-terminal``) and acks. The ONE case that stays
un-acked is a terminal envelope that WAS observed but whose publish failed
— covered in ``test_publish_failure.py`` (the publish-retry contract).
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import AsyncIterator, Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from langgraph_sdk.schema import StreamPart
from nats_core.events import (
    BuildCompletePayload,
    BuildFailedPayload,
    BuildStartedPayload,
)

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations as lifecycle_migrations
from forge.lifecycle_bridge.bridge import LifecycleBridge
from forge.lifecycle_bridge.run_state_source import RunStateSnapshot
from forge.lifecycle_bridge.translation import (
    StreamEventTranslator,
    VALUES_STREAM_EVENT,
)
from forge.lifecycle_bridge.wireup import (
    STREAM_NO_TERMINAL_FAILURE_REASON,
    LifecycleBridgeWireup,
)
from forge.persistence.migrations import lifecycle_bridge_registry as bridge_migration
from forge.persistence.repositories.bridge_registry import BridgeRegistry
from forge.pipeline.build_ack_handle import BuildAckHandle


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
    """Stream source whose iterator yields zero events.

    Mirrors the Signature C wire-shape: ``runs.join_stream`` against a
    finished run returns an empty live-subscription iterator.
    """

    def factory(*, feature_id, thread_id, run_id):
        async def gen() -> AsyncIterator[StreamPart]:
            return
            yield  # unreachable, makes this an async generator

        return gen()

    return factory


def _stream_source_with_terminal(parts: list[StreamPart]):
    """Stream source that yields one or more parts including a terminal.

    Used for AC-FETCH-6 (regression lock: when the live SSE path
    produces a terminal, the fetch-on-empty fallback MUST NOT also
    fire — that would double-emit and double-ack.)
    """

    def factory(*, feature_id, thread_id, run_id):
        async def gen() -> AsyncIterator[StreamPart]:
            for p in parts:
                yield p
                await asyncio.sleep(0)

        return gen()

    return factory


def _identity_resolved(thread_id: str = "thread-x", run_id: str = "run-x"):
    async def _provider(_feature_id: str) -> tuple[str, str] | None:
        return (thread_id, run_id)

    return _provider


def _terminal_state_values(
    feature_id: str,
    *,
    lifecycle: str = "completed",
    build_id: str = "build-FEAT-FETCH-001-20260508153000",
    wave_total: int = 1,
    tasks_completed: int = 1,
    tasks_failed: int = 0,
    error_class: str | None = None,
    error_message: str | None = None,
) -> dict:
    """Build the ``threads.get_state(...).values`` shape the translator consumes.

    Shape mirrors the canonical SSE values projection: top-level
    ``async_tasks`` keyed by ``feature_id`` carrying an
    ``AutobuildState`` snapshot. Sibling channels (``messages`` /
    ``todos`` / ``files``) are present-but-empty so the translator's
    state-extraction path sees the same outer shape it does on the
    live channel.
    """
    snap: dict = {
        "feature_id": feature_id,
        "build_id": build_id,
        "lifecycle": lifecycle,
        "wave_total": wave_total,
        "wave_index": 0,
        "task_index": 0,
        "tasks_completed": tasks_completed,
        "tasks_failed": tasks_failed,
        "waiting_for": None,
        "last_coach_score": None,
    }
    if error_class is not None:
        snap["error_class"] = error_class
    if error_message is not None:
        snap["error_message"] = error_message
    return {
        "messages": [],
        "todos": [],
        "files": {},
        "async_tasks": {feature_id: snap},
    }


def _success_snapshot(feature_id: str, **overrides) -> RunStateSnapshot:
    return RunStateSnapshot(
        status="success",
        values=_terminal_state_values(feature_id, **overrides),
    )


def _build_wireup(
    bridge: LifecycleBridge,
    translator: StreamEventTranslator,
    fake_publisher: MagicMock,
    *,
    stream_source,
    run_state_fetcher=None,
    identity: tuple[str, str] | None = ("thread-x", "run-x"),
) -> LifecycleBridgeWireup:
    return LifecycleBridgeWireup(
        bridge=bridge,
        translator=translator,
        publisher=fake_publisher,
        stream_source=stream_source,
        identity_provider=(
            _identity_resolved(*identity) if identity is not None else None
        ),
        run_state_fetcher=run_state_fetcher,
        identity_resolution_attempts=1,
        identity_poll_interval_seconds=0.0,
    )


async def _drain(wireup: LifecycleBridgeWireup, feature_id: str) -> None:
    task = wireup.get_observer_task(feature_id)
    if task is None:
        return
    try:
        await asyncio.wait_for(task, timeout=1.0)
    except asyncio.CancelledError:
        return


# ---------------------------------------------------------------------------
# AC-FETCH-1 — empty stream + terminal "success" → BuildStarted + BuildComplete
# ---------------------------------------------------------------------------


class TestFetchOnEmptySuccessfulRun:
    """AC-FETCH-1: empty stream + terminal run → 2 envelopes + ack."""

    @pytest.mark.asyncio
    async def test_synthesises_started_and_complete_then_acks(
        self,
        bridge: LifecycleBridge,
        translator: StreamEventTranslator,
        fake_publisher: MagicMock,
    ) -> None:
        feature_id = "FEAT-FETCH-OK"
        fetcher = AsyncMock(return_value=_success_snapshot(feature_id))
        wireup = _build_wireup(
            bridge,
            translator,
            fake_publisher,
            stream_source=_empty_stream_source(),
            run_state_fetcher=fetcher,
        )
        handle = _make_handle()

        await wireup.register_ack_handle(feature_id, "corr-fetch-ok", handle)
        await _drain(wireup, feature_id)

        # Fetcher was consulted with the resolved identity.
        fetcher.assert_awaited_once()
        call = fetcher.await_args
        assert call.kwargs["feature_id"] == feature_id
        assert call.kwargs["thread_id"] == "thread-x"
        assert call.kwargs["run_id"] == "run-x"

        # Two canonical envelopes published in order: BuildStarted then
        # BuildComplete. AC-11's "build-started.FEAT-* on the wire"
        # gate is satisfied by the first call.
        fake_publisher.publish_build_started.assert_awaited_once()
        started_arg = fake_publisher.publish_build_started.await_args.args[0]
        assert isinstance(started_arg, BuildStartedPayload)
        assert started_arg.feature_id == feature_id
        assert started_arg.build_id == "build-FEAT-FETCH-001-20260508153000"

        fake_publisher.publish_build_complete.assert_awaited_once()
        complete_arg = fake_publisher.publish_build_complete.await_args.args[0]
        assert isinstance(complete_arg, BuildCompletePayload)
        assert complete_arg.feature_id == feature_id
        assert complete_arg.build_id == "build-FEAT-FETCH-001-20260508153000"

        # Handle was acked → JetStream consumer ack_floor advances.
        handle.ack.assert_awaited_once()
        # No build-failed published on the success path.
        fake_publisher.publish_build_failed.assert_not_awaited()


# ---------------------------------------------------------------------------
# AC-FETCH-2 — empty stream + still-running run → no envelopes, no ack
# ---------------------------------------------------------------------------


class TestFetchOnEmptyStillRunning:
    """AC-FETCH-2: fetcher ``None`` on an empty stream → F6 build-failed.

    The fetcher's contract is to return ``None`` for non-terminal
    statuses (``pending``, ``running``). Because the SSE stream ended
    cleanly (StopAsyncIteration) the run is over from the transport's
    view — its terminal signal was merely lost. The observer terminalises
    promptly via the F6 synthetic build-failed rather than leaving the
    ledger RUNNING until the 300s deadline timer.
    """

    @pytest.mark.asyncio
    async def test_synthetic_failed_and_ack_when_fetch_yields_nothing(
        self,
        bridge: LifecycleBridge,
        translator: StreamEventTranslator,
        fake_publisher: MagicMock,
    ) -> None:
        feature_id = "FEAT-FETCH-RUN"
        # Fetcher returns None (production fetcher does this for
        # non-terminal statuses). Recovery yielded nothing → F6 fires.
        fetcher = AsyncMock(return_value=None)
        wireup = _build_wireup(
            bridge,
            translator,
            fake_publisher,
            stream_source=_empty_stream_source(),
            run_state_fetcher=fetcher,
        )
        handle = _make_handle()

        await wireup.register_ack_handle(feature_id, "corr-fetch-run", handle)
        await _drain(wireup, feature_id)

        fetcher.assert_awaited_once()
        # F6: recovery yielded nothing → synthetic build-failed + ack.
        # No BuildStarted/BuildComplete (there is no state to replay).
        fake_publisher.publish_build_started.assert_not_awaited()
        fake_publisher.publish_build_complete.assert_not_awaited()
        fake_publisher.publish_build_failed.assert_awaited_once()
        sent = fake_publisher.publish_build_failed.await_args.args[0]
        assert isinstance(sent, BuildFailedPayload)
        assert sent.feature_id == feature_id
        assert sent.failure_reason == STREAM_NO_TERMINAL_FAILURE_REASON
        assert sent.recoverable is True
        handle.ack.assert_awaited_once()


# ---------------------------------------------------------------------------
# AC-FETCH-3 — empty stream + fetcher returns None on transport error
# ---------------------------------------------------------------------------


class TestFetchOnEmptyFetcherNone:
    """AC-FETCH-3: fetcher returns None on transport error / SDK drift.

    The production ``langgraph_run_state_fetcher`` swallows transport
    errors and returns ``None`` (matches ``StreamSource``'s "yielding
    zero events is a clean exit" contract). Recovery yielded nothing, so
    the observer takes the F6 no-terminal path: synthetic build-failed +
    ack.
    """

    @pytest.mark.asyncio
    async def test_synthetic_failed_and_ack_when_fetcher_returns_none(
        self,
        bridge: LifecycleBridge,
        translator: StreamEventTranslator,
        fake_publisher: MagicMock,
    ) -> None:
        feature_id = "FEAT-FETCH-NONE"
        fetcher = AsyncMock(return_value=None)
        wireup = _build_wireup(
            bridge,
            translator,
            fake_publisher,
            stream_source=_empty_stream_source(),
            run_state_fetcher=fetcher,
        )
        handle = _make_handle()

        await wireup.register_ack_handle(feature_id, "corr-fetch-none", handle)
        await _drain(wireup, feature_id)

        fetcher.assert_awaited_once()
        fake_publisher.publish_build_started.assert_not_awaited()
        fake_publisher.publish_build_complete.assert_not_awaited()
        fake_publisher.publish_build_failed.assert_awaited_once()
        sent = fake_publisher.publish_build_failed.await_args.args[0]
        assert sent.failure_reason == STREAM_NO_TERMINAL_FAILURE_REASON
        handle.ack.assert_awaited_once()


# ---------------------------------------------------------------------------
# AC-FETCH-4 — empty stream + terminal "failed" → BuildStarted + BuildFailed
# ---------------------------------------------------------------------------


class TestFetchOnEmptyFailedRun:
    """AC-FETCH-4: terminal failure replay emits BuildFailed."""

    @pytest.mark.asyncio
    async def test_failed_run_emits_started_and_failed(
        self,
        bridge: LifecycleBridge,
        translator: StreamEventTranslator,
        fake_publisher: MagicMock,
    ) -> None:
        feature_id = "FEAT-FETCH-FAIL"
        snapshot = RunStateSnapshot(
            status="error",
            values=_terminal_state_values(
                feature_id,
                lifecycle="failed",
                error_class="RuntimeError",
                error_message="placeholder body raised",
            ),
        )
        fetcher = AsyncMock(return_value=snapshot)
        wireup = _build_wireup(
            bridge,
            translator,
            fake_publisher,
            stream_source=_empty_stream_source(),
            run_state_fetcher=fetcher,
        )
        handle = _make_handle()

        await wireup.register_ack_handle(feature_id, "corr-fetch-fail", handle)
        await _drain(wireup, feature_id)

        fake_publisher.publish_build_started.assert_awaited_once()
        fake_publisher.publish_build_failed.assert_awaited_once()
        failed_arg = fake_publisher.publish_build_failed.await_args.args[0]
        assert isinstance(failed_arg, BuildFailedPayload)
        assert failed_arg.feature_id == feature_id
        # Failure reason format from translator:
        # "{ExceptionClass}: {message}" (TASK-FRR-PEB-011 AC-4).
        assert "RuntimeError" in failed_arg.failure_reason
        assert "placeholder body raised" in failed_arg.failure_reason

        # build-complete must NOT fire on the failure path.
        fake_publisher.publish_build_complete.assert_not_awaited()
        handle.ack.assert_awaited_once()


# ---------------------------------------------------------------------------
# AC-FETCH-5 — fetcher raises → observer treats as no-snapshot
# ---------------------------------------------------------------------------


class TestFetchOnEmptyFetcherRaises:
    """AC-FETCH-5: a fetcher that breaks contract must NOT crash the daemon.

    The :class:`RunStateFetcher` Protocol contracts that implementations
    never raise (mirrors :class:`StreamSource`'s discipline). Defence in
    depth: a buggy fetcher's exception is logged and downgraded to
    "no snapshot" — recovery yielded nothing, so the observer then takes
    the F6 no-terminal path (synthetic build-failed + ack) rather than
    crashing.
    """

    @pytest.mark.asyncio
    async def test_fetcher_exception_is_swallowed_then_synthetic_failed(
        self,
        bridge: LifecycleBridge,
        translator: StreamEventTranslator,
        fake_publisher: MagicMock,
    ) -> None:
        feature_id = "FEAT-FETCH-BOOM"

        async def boom(**kwargs):
            raise RuntimeError("fetcher contract violation")

        wireup = _build_wireup(
            bridge,
            translator,
            fake_publisher,
            stream_source=_empty_stream_source(),
            run_state_fetcher=boom,
        )
        handle = _make_handle()

        # Must not raise — the daemon stays running on a bad fetcher.
        await wireup.register_ack_handle(feature_id, "corr-fetch-boom", handle)
        await _drain(wireup, feature_id)

        fake_publisher.publish_build_started.assert_not_awaited()
        fake_publisher.publish_build_complete.assert_not_awaited()
        # Fetcher raised → treated as no-snapshot → F6 synthetic failed.
        fake_publisher.publish_build_failed.assert_awaited_once()
        sent = fake_publisher.publish_build_failed.await_args.args[0]
        assert sent.failure_reason == STREAM_NO_TERMINAL_FAILURE_REASON
        handle.ack.assert_awaited_once()


# ---------------------------------------------------------------------------
# AC-FETCH-6 — fetcher NOT consulted when SSE produced a terminal
# ---------------------------------------------------------------------------


class TestFetchOnEmptyNotConsultedWhenLiveSSEProducedTerminal:
    """AC-FETCH-6: live SSE terminal must NOT trigger fetch-on-empty.

    Regression lock: when ``runs.join_stream`` DOES yield a terminal
    envelope (the happy path for long-running runs), the observer
    completes through the live path and exits — the fetcher is not
    consulted. Without this guarantee a successful live path would
    re-publish the terminal envelope from the fallback, double-acking
    the inbound (corruption of the JetStream ack_floor invariant).
    """

    @pytest.mark.asyncio
    async def test_fetcher_not_called_when_live_terminal_observed(
        self,
        bridge: LifecycleBridge,
        translator: StreamEventTranslator,
        fake_publisher: MagicMock,
    ) -> None:
        feature_id = "FEAT-FETCH-LIVE"
        running_part = StreamPart(
            event=VALUES_STREAM_EVENT,
            data=_terminal_state_values(
                feature_id, lifecycle="running_wave", tasks_completed=0
            ),
            id=None,
        )
        complete_part = StreamPart(
            event=VALUES_STREAM_EVENT,
            data=_terminal_state_values(
                feature_id, lifecycle="completed", tasks_completed=1
            ),
            id=None,
        )
        # Use a non-AsyncMock so a bug that DOES consult it would crash
        # loudly rather than silently no-op. Wrap the AsyncMock in a
        # tracker that records calls.
        fetcher = AsyncMock(
            side_effect=AssertionError(
                "fetcher MUST NOT be consulted when live SSE produced a "
                "terminal (regression: AC-FETCH-6)"
            )
        )
        wireup = _build_wireup(
            bridge,
            translator,
            fake_publisher,
            stream_source=_stream_source_with_terminal([running_part, complete_part]),
            run_state_fetcher=fetcher,
        )
        handle = _make_handle()

        await wireup.register_ack_handle(feature_id, "corr-fetch-live", handle)
        await _drain(wireup, feature_id)

        fetcher.assert_not_awaited()
        fake_publisher.publish_build_started.assert_awaited_once()
        fake_publisher.publish_build_complete.assert_awaited_once()
        handle.ack.assert_awaited_once()


# ---------------------------------------------------------------------------
# AC-FETCH-7 — fetcher omitted → default no-op preserves legacy behaviour
# ---------------------------------------------------------------------------


class TestFetchOnEmptyDefaultBehaviour:
    """AC-FETCH-7: omitting the fetcher kwarg uses the no-op default.

    The additive-only kwarg is the contract: callers that have not opted
    into PEBR-005's fetch-on-empty fallback get the default no-op fetcher,
    which yields no snapshot. Recovery yielded nothing, so the empty stream
    then takes the F6 no-terminal path — synthetic build-failed + ack.
    """

    @pytest.mark.asyncio
    async def test_omitted_fetcher_takes_f6_no_terminal_path(
        self,
        bridge: LifecycleBridge,
        translator: StreamEventTranslator,
        fake_publisher: MagicMock,
    ) -> None:
        feature_id = "FEAT-FETCH-DEFAULT"
        # Construct without run_state_fetcher kwarg.
        wireup = LifecycleBridgeWireup(
            bridge=bridge,
            translator=translator,
            publisher=fake_publisher,
            stream_source=_empty_stream_source(),
            identity_provider=_identity_resolved(),
            identity_resolution_attempts=1,
            identity_poll_interval_seconds=0.0,
        )
        handle = _make_handle()

        await wireup.register_ack_handle(feature_id, "corr-fetch-default", handle)
        await _drain(wireup, feature_id)

        # No replay envelopes (no snapshot), but F6 terminalises promptly.
        fake_publisher.publish_build_started.assert_not_awaited()
        fake_publisher.publish_build_complete.assert_not_awaited()
        fake_publisher.publish_build_failed.assert_awaited_once()
        sent = fake_publisher.publish_build_failed.await_args.args[0]
        assert sent.failure_reason == STREAM_NO_TERMINAL_FAILURE_REASON
        handle.ack.assert_awaited_once()

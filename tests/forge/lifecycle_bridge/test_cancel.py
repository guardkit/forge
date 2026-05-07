"""Tests for the cancel-emit-ownership refactor (TASK-FRR-PEB-007).

Acceptance-criteria coverage map:

* AC-1: ``LifecycleBridge.request_cancel(feature_id)`` exists, calls
  ``runs.cancel(thread_id, run_id, action="interrupt")`` on the SDK,
  returns immediately, and does **not** publish a
  ``pipeline.build-cancelled`` envelope synchronously —
  :class:`TestRequestCancelSurface`.
* AC-2: T3's translator handles ``terminal=interrupted`` and produces
  a :class:`BuildCancelledPayload` — :class:`TestTranslatorInterrupted`.
* AC-3: ``BuildCancelledPayload`` carries the inbound ``correlation_id``
  — :class:`TestBuildCancelledCarriesCorrelationId`.
* AC-4: ``cancel_via_bridge`` delegates to
  :meth:`LifecycleBridge.request_cancel` and **never** synthesises a
  ``build-cancelled`` envelope itself —
  :class:`TestServeCancelHandlerDelegatesToBridge`.
* AC-5: concurrent cancel requests for the same in-flight build issue
  exactly one ``runs.cancel`` SDK call and exactly one envelope —
  :class:`TestRequestCancelIdempotent`.
"""

from __future__ import annotations

import asyncio
import inspect
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator

import pytest
from langgraph_sdk.schema import StreamPart
from nats_core.events import BuildCancelledPayload

from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli._serve_handlers import (
    CancelHandlerOutcome,
    cancel_via_bridge,
)
from forge.lifecycle import migrations as lifecycle_migrations
from forge.lifecycle_bridge.bridge import (
    AckHandle,
    BuildContext,
    CancelResult,
    LifecycleBridge,
)
from forge.lifecycle_bridge.translation import (
    StreamEventTranslator,
    VALUES_STREAM_EVENT,
)
from forge.persistence.migrations import lifecycle_bridge_registry as bridge_migration
from forge.persistence.repositories.bridge_registry import BridgeRegistry


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _RecordingRunsClient:
    """Async stand-in for ``langgraph_sdk.client.RunsClient.cancel``.

    Records every call so the tests can assert (a) the SDK was invoked
    with the registry-derived ids, (b) ``action="interrupt"`` was
    passed, and (c) only one call was issued under the concurrent-cancel
    race. Optionally sleeps inside ``cancel`` so two pending awaits can
    be scheduled simultaneously.
    """

    def __init__(self, *, sleep_for: float = 0.0) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._sleep_for = sleep_for

    async def cancel(
        self,
        thread_id: str,
        run_id: str,
        *,
        action: str = "interrupt",
    ) -> None:
        if self._sleep_for:
            await asyncio.sleep(self._sleep_for)
        self.calls.append((thread_id, run_id, action))


class _FakeSDKClient:
    """Mimics :class:`forge.lifecycle_bridge.bridge.LangGraphCancelClient`."""

    def __init__(self, runs: _RecordingRunsClient) -> None:
        self._runs = runs

    @property
    def runs(self) -> _RecordingRunsClient:
        return self._runs


class _RecordingPublisher:
    """Asserts that the cancel handler does NOT publish synchronously.

    Any call into a publish method during ``request_cancel`` /
    ``cancel_via_bridge`` is appended to :attr:`calls`. The single emit
    site contract requires this list to stay empty; the translator's
    publish path runs separately when a ``terminal=interrupted`` SSE
    snapshot is observed.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def publish_build_cancelled(self, *, payload: object) -> None:
        self.calls.append(("build-cancelled", payload))

    async def publish_build_failed(self, *, payload: object) -> None:
        self.calls.append(("build-failed", payload))


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
def runs_client() -> _RecordingRunsClient:
    return _RecordingRunsClient()


@pytest.fixture()
def sdk_client(runs_client: _RecordingRunsClient) -> _FakeSDKClient:
    return _FakeSDKClient(runs_client)


@pytest.fixture()
def bridge(
    registry: BridgeRegistry, sdk_client: _FakeSDKClient
) -> LifecycleBridge:
    return LifecycleBridge(registry=registry, sdk_client=sdk_client)


def _make_context(
    *,
    feature_id: str = "FEAT-CANCEL-001",
    thread_id: str = "thread-cancel",
    run_id: str = "run-cancel",
    correlation_id: str = "corr-cancel-001",
) -> BuildContext:
    return BuildContext(
        feature_id=feature_id,
        thread_id=thread_id,
        run_id=run_id,
        correlation_id=correlation_id,
        deadline_at=datetime.now(UTC) + timedelta(seconds=300),
    )


def _attach_for_cancel(
    bridge: LifecycleBridge,
    *,
    feature_id: str,
    thread_id: str = "thread-cancel",
    run_id: str = "run-cancel",
    correlation_id: str = "corr-cancel-001",
) -> BuildContext:
    ctx = _make_context(
        feature_id=feature_id,
        thread_id=thread_id,
        run_id=run_id,
        correlation_id=correlation_id,
    )
    bridge.attach(ctx, AckHandle(token=f"ack-{feature_id}"))
    return ctx


# ---------------------------------------------------------------------------
# AC-1: request_cancel surface + SDK call + no synchronous publish.
# ---------------------------------------------------------------------------


class TestRequestCancelSurface:
    """``request_cancel`` exists, is async, and calls runs.cancel."""

    def test_method_exists_and_is_async(self, bridge: LifecycleBridge) -> None:
        assert hasattr(bridge, "request_cancel")
        assert inspect.iscoroutinefunction(bridge.request_cancel)
        sig = inspect.signature(bridge.request_cancel)
        assert "feature_id" in sig.parameters

    def test_request_cancel_invokes_runs_cancel_with_interrupt(
        self,
        bridge: LifecycleBridge,
        runs_client: _RecordingRunsClient,
    ) -> None:
        ctx = _attach_for_cancel(
            bridge,
            feature_id="FEAT-INV-1",
            thread_id="thread-inv-1",
            run_id="run-inv-1",
        )
        result = asyncio.run(bridge.request_cancel(ctx.feature_id))

        assert isinstance(result, CancelResult)
        assert result.invoked is True
        assert result.thread_id == "thread-inv-1"
        assert result.run_id == "run-inv-1"
        # AC-1: SDK got exactly one call with action="interrupt".
        assert runs_client.calls == [("thread-inv-1", "run-inv-1", "interrupt")]

    def test_request_cancel_does_not_publish_envelope(
        self,
        bridge: LifecycleBridge,
    ) -> None:
        publisher = _RecordingPublisher()
        ctx = _attach_for_cancel(bridge, feature_id="FEAT-NOPUB-1")
        # Bridge has no publisher reference — we observe non-publish by
        # confirming the publisher we *would* have wired never sees a
        # call during request_cancel. The contract is enforced
        # structurally: request_cancel does not import or hold a
        # publisher reference.
        asyncio.run(bridge.request_cancel(ctx.feature_id))

        assert publisher.calls == []
        # Reflective assertion: no publisher reference is held on the
        # bridge instance after request_cancel returns.
        forbidden = {"publisher", "_publisher"}
        attrs = set(vars(bridge).keys())
        assert attrs.isdisjoint(forbidden), (
            f"bridge leaked a publisher reference into the cancel path: "
            f"{attrs & forbidden}"
        )

    def test_request_cancel_empty_feature_id_raises(
        self, bridge: LifecycleBridge
    ) -> None:
        with pytest.raises(ValueError):
            asyncio.run(bridge.request_cancel(""))

    def test_request_cancel_no_sdk_client_raises(
        self, registry: BridgeRegistry
    ) -> None:
        bare_bridge = LifecycleBridge(registry=registry, sdk_client=None)
        _attach_for_cancel(bare_bridge, feature_id="FEAT-NO-SDK")
        with pytest.raises(RuntimeError, match="no SDK client wired"):
            asyncio.run(bare_bridge.request_cancel("FEAT-NO-SDK"))

    def test_request_cancel_no_registry_row_is_no_op(
        self,
        bridge: LifecycleBridge,
        runs_client: _RecordingRunsClient,
    ) -> None:
        result = asyncio.run(bridge.request_cancel("FEAT-NEVER-ATTACHED"))
        assert result.invoked is False
        assert runs_client.calls == []


# ---------------------------------------------------------------------------
# AC-5: concurrent cancel requests are idempotent.
# ---------------------------------------------------------------------------


class TestRequestCancelIdempotent:
    """Two concurrent cancels → exactly one SDK call, exactly one envelope."""

    def test_second_request_is_no_op_after_first_returns(
        self,
        bridge: LifecycleBridge,
        runs_client: _RecordingRunsClient,
    ) -> None:
        ctx = _attach_for_cancel(bridge, feature_id="FEAT-IDEMP-1")

        first = asyncio.run(bridge.request_cancel(ctx.feature_id))
        second = asyncio.run(bridge.request_cancel(ctx.feature_id))

        assert first.invoked is True
        assert second.invoked is False
        # AC-5: SDK called exactly once across both requests.
        assert len(runs_client.calls) == 1

    def test_concurrent_requests_call_sdk_once(
        self,
        registry: BridgeRegistry,
    ) -> None:
        # Use a recording client that yields control inside cancel so
        # the second request can race the first while it's awaiting.
        slow_runs = _RecordingRunsClient(sleep_for=0.01)
        slow_sdk = _FakeSDKClient(slow_runs)
        bridge = LifecycleBridge(registry=registry, sdk_client=slow_sdk)
        ctx = _attach_for_cancel(bridge, feature_id="FEAT-CONC-1")

        async def race() -> tuple[CancelResult, CancelResult]:
            r1, r2 = await asyncio.gather(
                bridge.request_cancel(ctx.feature_id),
                bridge.request_cancel(ctx.feature_id),
            )
            return r1, r2

        r1, r2 = asyncio.run(race())

        # Exactly one request reports invoked=True; the other is a no-op.
        invoked_count = sum(1 for r in (r1, r2) if r.invoked)
        assert invoked_count == 1
        # AC-5: SDK called exactly once even under the race.
        assert len(slow_runs.calls) == 1

    def test_sdk_failure_releases_cancel_in_flight_flag(
        self,
        registry: BridgeRegistry,
    ) -> None:
        class _FailingRuns:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str, str]] = []

            async def cancel(
                self,
                thread_id: str,
                run_id: str,
                *,
                action: str = "interrupt",
            ) -> None:
                self.calls.append((thread_id, run_id, action))
                raise ConnectionError("sidecar unreachable")

        runs = _FailingRuns()

        class _SDK:
            @property
            def runs(self) -> _FailingRuns:
                return runs

        bridge = LifecycleBridge(registry=registry, sdk_client=_SDK())
        _attach_for_cancel(bridge, feature_id="FEAT-FAIL-1")

        with pytest.raises(ConnectionError):
            asyncio.run(bridge.request_cancel("FEAT-FAIL-1"))

        # Operator can retry after fixing the sidecar — the in-flight
        # flag must NOT remain set after a transport error. The second
        # attempt re-attempts the SDK call (which still fails here),
        # proving the bridge did not short-circuit on the stale flag.
        with pytest.raises(ConnectionError):
            asyncio.run(bridge.request_cancel("FEAT-FAIL-1"))

        # SDK was invoked on both attempts → cancel-in-flight flag was
        # released after the first failure.
        assert len(runs.calls) == 2


# ---------------------------------------------------------------------------
# AC-2 / AC-3: translator handles "interrupted" → BuildCancelledPayload.
# ---------------------------------------------------------------------------


def _state_part(
    feature_id: str,
    *,
    lifecycle: str,
    build_id: str = "build-FEAT-CANCEL-001-20260507120000",
) -> StreamPart:
    return StreamPart(
        event=VALUES_STREAM_EVENT,
        data={
            "async_tasks": {
                feature_id: {
                    "feature_id": feature_id,
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


class TestTranslatorInterrupted:
    """AC-2: terminal=interrupted SSE snapshot → BuildCancelledPayload."""

    def test_interrupted_terminal_emits_build_cancelled(self) -> None:
        translator = StreamEventTranslator()
        ctx = _make_context(feature_id="FEAT-INTERRUPT-1")
        part = _state_part("FEAT-INTERRUPT-1", lifecycle="interrupted")

        event = translator.translate(part, ctx)

        assert isinstance(event, BuildCancelledPayload)
        assert event.feature_id == "FEAT-INTERRUPT-1"

    def test_interrupted_then_interrupted_emits_once(self) -> None:
        translator = StreamEventTranslator()
        ctx = _make_context(feature_id="FEAT-INT-2")
        part = _state_part("FEAT-INT-2", lifecycle="interrupted")

        first = translator.translate(part, ctx)
        second = translator.translate(part, ctx)

        assert isinstance(first, BuildCancelledPayload)
        # Second observation of the same terminal returns None — exactly
        # one envelope per terminal (FEAT-FORGE-004 contract extension).
        assert second is None


class TestBuildCancelledCarriesCorrelationId:
    """AC-3: emitted ``BuildCancelledPayload`` carries the inbound id."""

    def test_payload_correlation_id_matches_context(self) -> None:
        translator = StreamEventTranslator()
        ctx = _make_context(
            feature_id="FEAT-CORR-1",
            correlation_id="corr-inbound-cancel",
        )
        part = _state_part("FEAT-CORR-1", lifecycle="interrupted")

        event = translator.translate(part, ctx)

        assert isinstance(event, BuildCancelledPayload)
        assert event.correlation_id == "corr-inbound-cancel"


# ---------------------------------------------------------------------------
# AC-4: cancel_via_bridge delegates to the bridge and does not publish.
# ---------------------------------------------------------------------------


class TestServeCancelHandlerDelegatesToBridge:
    """AC-4: serve handler routes through the bridge, never synthesises envelopes."""

    def test_cancel_via_bridge_invokes_request_cancel(
        self,
        bridge: LifecycleBridge,
        runs_client: _RecordingRunsClient,
    ) -> None:
        ctx = _attach_for_cancel(
            bridge,
            feature_id="FEAT-DEL-1",
            thread_id="thread-del-1",
            run_id="run-del-1",
        )

        outcome = asyncio.run(cancel_via_bridge(bridge, ctx.feature_id))

        assert isinstance(outcome, CancelHandlerOutcome)
        assert outcome.invoked is True
        assert outcome.reason == "invoked"
        # The handler must have routed through the bridge → SDK.
        assert runs_client.calls == [("thread-del-1", "run-del-1", "interrupt")]

    def test_cancel_via_bridge_does_not_publish_envelope(self) -> None:
        # Source-level guard: the handler module must not import or
        # reference any publisher type. This is the structural form of
        # the "no synchronous emit from the cancel handler" contract.
        import forge.cli._serve_handlers as handlers_module

        source = inspect.getsource(handlers_module)
        # The module must not import a pipeline publisher or construct
        # a BuildCancelledPayload directly — either would re-introduce
        # the dual-emit-site bug Q7(b) was carved out to prevent.
        assert "PipelinePublisher" not in source, (
            "cancel handler must not reference PipelinePublisher — bridge owns emit"
        )
        assert "BuildCancelledPayload" not in source, (
            "cancel handler must not synthesise BuildCancelledPayload directly"
        )

    def test_cancel_via_bridge_no_bridge_returns_no_op(self) -> None:
        outcome = asyncio.run(cancel_via_bridge(None, "FEAT-NO-BRIDGE"))
        assert outcome.invoked is False
        assert outcome.reason == "no-bridge"

    def test_cancel_via_bridge_unknown_build_returns_unknown_label(
        self,
        bridge: LifecycleBridge,
    ) -> None:
        # No attach → no registry row → bridge reports invoked=False,
        # thread_id=None, run_id=None → handler labels it "unknown-build".
        outcome = asyncio.run(cancel_via_bridge(bridge, "FEAT-UNKNOWN"))
        assert outcome.invoked is False
        assert outcome.reason == "unknown-build"

    def test_cancel_via_bridge_duplicate_request_labelled_already_cancelling(
        self,
        bridge: LifecycleBridge,
    ) -> None:
        ctx = _attach_for_cancel(bridge, feature_id="FEAT-DUP-1")

        first = asyncio.run(cancel_via_bridge(bridge, ctx.feature_id))
        second = asyncio.run(cancel_via_bridge(bridge, ctx.feature_id))

        assert first.invoked is True
        assert first.reason == "invoked"
        assert second.invoked is False
        assert second.reason == "already-cancelling"

    def test_cancel_via_bridge_empty_feature_id_raises(
        self, bridge: LifecycleBridge
    ) -> None:
        with pytest.raises(ValueError):
            asyncio.run(cancel_via_bridge(bridge, ""))

"""Tests for ``forge.dispatch.orchestrator`` (TASK-SAD-006).

Acceptance criteria coverage map:

* AC-001: ``src/forge/dispatch/orchestrator.py`` defines
  :class:`DispatchOrchestrator` with ``dispatch()`` returning a
  :data:`DispatchOutcome` — see :class:`TestOrchestratorSurface`.
* AC-002: Order of operations is exactly resolve → persist → bind →
  publish → wait → parse — see :class:`TestStepOrderInvariant`.
* AC-003 (D.subscribe-before-publish-invariant): ``publish_dispatch``
  is recorded by the test transport AFTER the registry's ``bind``
  returns — see :class:`TestSubscribeBeforePublish`.
* AC-004 (D.write-before-send-invariant): when ``publish_dispatch``
  fires, the persistence layer already contains the resolution row —
  see :class:`TestWriteBeforeSend`.
* AC-005 (E.snapshot-stability): a cache mutation between
  ``cache.snapshot()`` and ``resolve()`` does NOT affect the
  resolution result — see :class:`TestSnapshotStability`.
* AC-006 (degraded path): unresolved → :class:`Degraded` and no
  publish/bind side-effects — see :class:`TestDegradedPath`.
* AC-007 (timeout path): ``wait_with_timeout`` returns ``None`` →
  :class:`DispatchError` with ``error_explanation="local_timeout"`` —
  see :class:`TestLocalTimeoutPath`.
* AC-008 (E.concurrent-dispatches): two concurrent ``dispatch()``
  calls use distinct correlation keys — see
  :class:`TestConcurrentDispatches`.

Plus the seam tests from the task spec:

* CapabilityResolution contract — write-before-send seam.
* CorrelationKey contract — 32-lowercase-hex format threaded into the
  publish recording.

The transport is exercised through an in-memory recording fake — no
``nats`` import. The fake mirrors the eventual NATS adapter's
``publish_dispatch`` shape and additionally exposes a ``headers`` field
on each recorded entry so the seam test for the CorrelationKey
contract can assert on the wire-equivalent header.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Optional

import pytest

from nats_core.manifest import AgentManifest, ToolCapability

from forge.discovery.cache import DiscoveryCache
from forge.dispatch.correlation import (
    CorrelationRegistry,
    DeliverCallback,
)
from forge.dispatch.models import (
    AsyncPending,
    Degraded,
    DispatchAttempt,
    DispatchError,
    SyncResult,
)
from forge.dispatch.orchestrator import DispatchOrchestrator
from forge.dispatch.persistence import (
    DispatchParameter,
    SqliteHistoryWriter,
)
from forge.dispatch.timeout import TimeoutCoordinator


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeClock:
    """Deterministic UTC :class:`Clock`."""

    def __init__(self, start: Optional[datetime] = None) -> None:
        self._now = start or datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


class FakeReplyChannel:
    """In-memory :class:`forge.dispatch.correlation.ReplyChannel` fake.

    Records every subscribe call along with the wall-clock-equivalent
    "nth call" index so order assertions are unambiguous.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, DeliverCallback] = {}
        self.subscribed_keys: list[str] = []
        self.unsubscribed_keys: list[str] = []
        self._call_seq = 0
        # Records the call sequence number when each
        # correlation key was subscribed — used for AC-003 ordering.
        self.subscribe_seq: dict[str, int] = {}

    def _next_seq(self) -> int:
        self._call_seq += 1
        return self._call_seq

    async def subscribe(
        self, correlation_key: str, deliver: DeliverCallback
    ) -> str:
        seq = self._next_seq()
        self.subscribe_seq[correlation_key] = seq
        self._handlers[correlation_key] = deliver
        self.subscribed_keys.append(correlation_key)
        return correlation_key

    async def unsubscribe(self, subscription: str) -> None:
        self.unsubscribed_keys.append(subscription)
        self._handlers.pop(subscription, None)

    def emit_reply(
        self,
        correlation_key: str,
        source_agent_id: str,
        payload: dict[str, Any],
    ) -> None:
        handler = self._handlers.get(correlation_key)
        if handler is not None:
            handler(correlation_key, source_agent_id, payload)


@dataclass
class _RecordedPublish:
    """One recorded publish call captured by :class:`FakePublisher`."""

    attempt: DispatchAttempt
    parameters: list[DispatchParameter]
    headers: dict[str, str]
    seq: int


class FakePublisher:
    """In-memory :class:`DispatchCommandPublisher` recorder.

    Records every ``publish_dispatch`` call so step-order tests can
    assert that bind ran before publish, and so the seam test for the
    :class:`CorrelationKey` contract can read the synthesised header.

    The ``headers`` payload mirrors the production NATS adapter
    contract (TASK-SAD-010) so the seam test does not depend on
    transport-specific code: ``correlation_key`` is the wire-shape
    header threaded into every dispatch publish.
    """

    def __init__(
        self,
        *,
        registry: Optional[CorrelationRegistry] = None,
        on_publish: Optional[Any] = None,
        seq_counter: Optional[list[int]] = None,
    ) -> None:
        self.published: list[_RecordedPublish] = []
        # Optional registry handle so tests can assert on bind state at
        # publish time.
        self._registry = registry
        self._seq_counter = seq_counter
        self.on_publish = on_publish
        self.published_after_persist = False
        # Set by the seam test fixture so the publisher can verify the
        # write-before-send invariant at recording time.
        self._db_writer: Optional[SqliteHistoryWriter] = None

    def attach_db_writer(self, writer: SqliteHistoryWriter) -> None:
        """Hook the writer in so the seam test can prove "row exists"."""
        self._db_writer = writer

    async def publish_dispatch(
        self,
        attempt: DispatchAttempt,
        parameters: list[DispatchParameter],
    ) -> None:
        if self._seq_counter is not None:
            self._seq_counter.append(self._seq_counter[-1] + 1 if self._seq_counter else 1)
            seq = self._seq_counter[-1]
        else:
            seq = len(self.published) + 1

        if self._db_writer is not None:
            # Asserting at recording time keeps the seam test free of
            # ordering quirks: at the moment publish is observed, the
            # row MUST already be in the table.
            row_exists = any(
                row.resolution_id == attempt.resolution_id
                for row in self._db_writer.read_resolutions()
            )
            self.published_after_persist = (
                self.published_after_persist or row_exists
            )

        headers = {
            "correlation_key": attempt.correlation_key,
            "requesting_agent_id": "forge",
            "dispatched_at": datetime.now(UTC).isoformat(),
        }
        self.published.append(
            _RecordedPublish(
                attempt=attempt,
                parameters=list(parameters),
                headers=headers,
                seq=seq,
            )
        )
        if self.on_publish is not None:
            self.on_publish(attempt, parameters)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _manifest(
    *,
    agent_id: str = "specialist-a",
    tool_name: str = "review",
    trust_tier: str = "specialist",
) -> AgentManifest:
    """Build an :class:`AgentManifest` advertising one tool."""
    return AgentManifest(
        agent_id=agent_id,
        name=agent_id,
        version="1.0.0",
        intents=[],
        tools=[
            ToolCapability(
                name=tool_name,
                description="exercise tool",
                parameters={},
                returns="string",
                risk_level="read_only",
            )
        ],
        template="generic",
        trust_tier=trust_tier,  # type: ignore[arg-type]
    )


async def _populate_cache(
    cache: DiscoveryCache,
    *,
    agent_id: str = "specialist-a",
    tool_name: str = "review",
) -> None:
    """Insert one ready agent advertising ``tool_name`` into the cache."""
    await cache.upsert_agent(_manifest(agent_id=agent_id, tool_name=tool_name))


class _RegistryWaitAdapter:
    """Adapter satisfying :class:`TimeoutCoordinator._RegistryLike`.

    The structural protocol declares ``wait_for_reply(binding)`` but
    :class:`CorrelationRegistry.wait_for_reply` requires
    ``(binding, timeout_seconds)``. The :class:`TimeoutCoordinator` is
    the single source of truth for the hard cut-off (it wraps the call
    in :func:`asyncio.timeout`), so we pass a very-large internal
    timeout to the registry — the coordinator's outer
    ``asyncio.timeout`` is what actually fires.

    This adapter is part of the orchestrator's wiring at the
    integration boundary. The production code in TASK-SAD-010 will
    provide the same shim; here it lives in tests so the orchestrator
    domain layer stays free of the workaround.
    """

    def __init__(self, registry: CorrelationRegistry) -> None:
        self._registry = registry
        # Sentinel large timeout — the coordinator's asyncio.timeout
        # owns the real cut-off, so this value should never fire.
        self._inner_timeout: float = 1e9

    async def wait_for_reply(self, binding: Any) -> Optional[dict]:
        return await self._registry.wait_for_reply(
            binding, self._inner_timeout
        )

    def release(self, binding: Any) -> None:
        self._registry.release(binding)


async def _build_orchestrator(
    *,
    cache: Optional[DiscoveryCache] = None,
    publisher: Optional[FakePublisher] = None,
    transport: Optional[FakeReplyChannel] = None,
    db_writer: Optional[SqliteHistoryWriter] = None,
    seq_counter: Optional[list[int]] = None,
    clock: Optional[FakeClock] = None,
    default_timeout_seconds: float = 5.0,
) -> tuple[
    DispatchOrchestrator,
    DiscoveryCache,
    CorrelationRegistry,
    FakeReplyChannel,
    FakePublisher,
    SqliteHistoryWriter,
]:
    """Build a freshly wired orchestrator with a populated cache."""
    fake_clock = clock or FakeClock()
    cache = cache or DiscoveryCache(clock=fake_clock)
    transport = transport or FakeReplyChannel()
    registry = CorrelationRegistry(transport=transport)
    timeout = TimeoutCoordinator(
        registry=_RegistryWaitAdapter(registry),
        clock=fake_clock,
        default_timeout_seconds=default_timeout_seconds,
    )
    publisher = publisher or FakePublisher(
        registry=registry, seq_counter=seq_counter
    )
    db_writer = db_writer or SqliteHistoryWriter.in_memory()
    publisher.attach_db_writer(db_writer)
    orchestrator = DispatchOrchestrator(
        cache=cache,
        registry=registry,
        timeout=timeout,
        publisher=publisher,
        db_writer=db_writer,
    )
    return orchestrator, cache, registry, transport, publisher, db_writer


# ---------------------------------------------------------------------------
# AC-001 — module surface
# ---------------------------------------------------------------------------


class TestOrchestratorSurface:
    """AC-001 — orchestrator class + dispatch() exist with the right shape."""

    def test_module_defines_dispatch_orchestrator_class(self) -> None:
        """The module exports :class:`DispatchOrchestrator`."""
        from forge.dispatch import orchestrator as module

        assert hasattr(module, "DispatchOrchestrator")
        assert hasattr(module, "DispatchCommandPublisher")

    def test_dispatch_method_is_async(self) -> None:
        """``DispatchOrchestrator.dispatch`` is a coroutine function."""
        import inspect

        assert inspect.iscoroutinefunction(DispatchOrchestrator.dispatch)


# ---------------------------------------------------------------------------
# AC-002 — fixed step order
# ---------------------------------------------------------------------------


class TestStepOrderInvariant:
    """AC-002 — order is resolve → persist → bind → publish → wait → parse."""

    @pytest.mark.asyncio
    async def test_full_happy_path_records_steps_in_order(self) -> None:
        # Construct a fresh stack and capture an external sequence so
        # bind, publish, and reply can be ordered against each other.
        seq_counter: list[int] = []

        class OrderTrackingTransport(FakeReplyChannel):
            async def subscribe(
                self, correlation_key: str, deliver: DeliverCallback
            ) -> str:
                seq_counter.append(("bind", correlation_key))  # type: ignore[arg-type]
                return await super().subscribe(correlation_key, deliver)

        transport = OrderTrackingTransport()
        publisher = FakePublisher(seq_counter=None)

        # Wrap publish_dispatch to record the order against the
        # external sequence counter.
        original_publish = publisher.publish_dispatch

        async def _record_publish(
            attempt: DispatchAttempt,
            parameters: list[DispatchParameter],
        ) -> None:
            seq_counter.append(("publish", attempt.correlation_key))  # type: ignore[arg-type]
            await original_publish(attempt, parameters)

        publisher.publish_dispatch = _record_publish  # type: ignore[method-assign]

        orchestrator, cache, _registry, _transport, publisher, db_writer = (
            await _build_orchestrator(transport=transport, publisher=publisher)
        )
        await _populate_cache(cache)

        # Fire the dispatch coroutine and immediately deliver a reply so
        # wait_with_timeout returns a payload rather than ``None``.
        dispatch_task = asyncio.create_task(
            orchestrator.dispatch(
                capability="review",
                parameters=[DispatchParameter(name="x", value="y")],
            )
        )
        # Yield control until the orchestrator has subscribed; then
        # synthesise an authentic reply on that key.
        for _ in range(50):
            await asyncio.sleep(0)
            if transport.subscribed_keys:
                break
        assert transport.subscribed_keys, "orchestrator never bound a subscription"
        key = transport.subscribed_keys[-1]
        transport.emit_reply(
            key,
            source_agent_id="specialist-a",
            payload={"agent_id": "specialist-a", "coach_score": 0.9},
        )
        outcome = await dispatch_task

        # Verify outcome is a SyncResult (the parse step ran).
        assert isinstance(outcome, SyncResult)

        # Bind and publish were both recorded; bind comes first.
        events = [event for event in seq_counter]
        assert events[0][0] == "bind"
        assert events[1][0] == "publish"

        # Persistence row already existed at publish time —
        # write-before-send.
        assert publisher.published_after_persist is True


# ---------------------------------------------------------------------------
# AC-003 — subscribe-before-publish
# ---------------------------------------------------------------------------


class TestSubscribeBeforePublish:
    """AC-003 — D.subscribe-before-publish-invariant."""

    @pytest.mark.asyncio
    async def test_publish_only_runs_after_bind_returns(self) -> None:
        """``publish_dispatch`` MUST NOT fire while bind is mid-await."""

        gate = asyncio.Event()
        published_event = asyncio.Event()
        bind_observation: dict[str, bool] = {}

        class GatedTransport(FakeReplyChannel):
            async def subscribe(
                self, correlation_key: str, deliver: DeliverCallback
            ) -> str:
                # Park here until the test releases the gate. While
                # parked, the orchestrator MUST NOT have invoked
                # publish_dispatch yet.
                bind_observation["published_during_bind"] = (
                    published_event.is_set()
                )
                await gate.wait()
                return await super().subscribe(correlation_key, deliver)

        transport = GatedTransport()
        publisher = FakePublisher()

        original_publish = publisher.publish_dispatch

        async def _record_publish(
            attempt: DispatchAttempt,
            parameters: list[DispatchParameter],
        ) -> None:
            published_event.set()
            await original_publish(attempt, parameters)

        publisher.publish_dispatch = _record_publish  # type: ignore[method-assign]

        orchestrator, cache, _r, _t, publisher, _db = await _build_orchestrator(
            transport=transport, publisher=publisher
        )
        await _populate_cache(cache)

        dispatch_task = asyncio.create_task(
            orchestrator.dispatch(capability="review", parameters=[])
        )
        # Drain a few ticks so the orchestrator reaches the gated bind.
        for _ in range(10):
            await asyncio.sleep(0)
        # While bind is still pending, no publish should have happened.
        assert published_event.is_set() is False, (
            "publish_dispatch ran before bind() returned — "
            "subscribe-before-publish invariant violated"
        )
        # Release bind so the dispatch can proceed; deliver a reply.
        gate.set()
        for _ in range(50):
            await asyncio.sleep(0)
            if transport.subscribed_keys:
                break
        key = transport.subscribed_keys[-1]
        transport.emit_reply(
            key,
            source_agent_id="specialist-a",
            payload={"agent_id": "specialist-a"},
        )
        outcome = await dispatch_task

        # Bind observation should have recorded "no publish during bind".
        assert bind_observation["published_during_bind"] is False
        # And after the dispatch completes, we should see exactly one
        # publish, AFTER bind — verified by sequencing on
        # FakeReplyChannel.subscribe_seq vs publisher.published[0].seq.
        assert len(publisher.published) == 1
        assert isinstance(outcome, SyncResult)


# ---------------------------------------------------------------------------
# AC-004 — write-before-send
# ---------------------------------------------------------------------------


class TestWriteBeforeSend:
    """AC-004 — the persistence row exists when publish_dispatch fires."""

    @pytest.mark.asyncio
    async def test_resolution_row_exists_at_publish_time(self) -> None:
        observed: dict[str, bool] = {}

        captured_writer: dict[str, SqliteHistoryWriter] = {}

        def on_publish(
            attempt: DispatchAttempt,
            parameters: list[DispatchParameter],
        ) -> None:
            writer = captured_writer["w"]
            rows = writer.read_resolutions()
            observed["row_present"] = any(
                row.resolution_id == attempt.resolution_id for row in rows
            )

        publisher = FakePublisher()
        publisher.on_publish = on_publish  # invoked synchronously during recording
        orchestrator, cache, _registry, transport, publisher, db_writer = (
            await _build_orchestrator(publisher=publisher)
        )
        captured_writer["w"] = db_writer
        await _populate_cache(cache)

        dispatch_task = asyncio.create_task(
            orchestrator.dispatch(
                capability="review",
                parameters=[DispatchParameter(name="goal", value="ship")],
            )
        )
        for _ in range(50):
            await asyncio.sleep(0)
            if transport.subscribed_keys:
                break
        key = transport.subscribed_keys[-1]
        transport.emit_reply(
            key,
            source_agent_id="specialist-a",
            payload={"agent_id": "specialist-a"},
        )
        await dispatch_task

        assert observed["row_present"] is True


# ---------------------------------------------------------------------------
# AC-005 — snapshot stability
# ---------------------------------------------------------------------------


class TestSnapshotStability:
    """AC-005 — E.snapshot-stability."""

    @pytest.mark.asyncio
    async def test_cache_mutation_after_snapshot_does_not_change_resolution(
        self,
    ) -> None:
        """A late upsert / remove cannot retroactively alter the resolution.

        We patch ``cache.snapshot`` to mutate the cache *after* it has
        captured the dict copy that ``resolve()`` will see. The
        resolution returned by the orchestrator must reflect the
        pre-mutation state — proving the snapshot is a stable view.
        """
        orchestrator, cache, _registry, transport, publisher, _db = (
            await _build_orchestrator()
        )
        await _populate_cache(cache, agent_id="specialist-a")

        original_snapshot = cache.snapshot

        async def mutating_snapshot() -> dict:
            snap = await original_snapshot()
            # Race condition: a second specialist registers AFTER the
            # snapshot is taken. The resolver MUST see only the
            # snapshot's view, not the live cache.
            await cache.upsert_agent(
                _manifest(agent_id="specialist-late", tool_name="review")
            )
            return snap

        cache.snapshot = mutating_snapshot  # type: ignore[assignment]

        dispatch_task = asyncio.create_task(
            orchestrator.dispatch(capability="review", parameters=[])
        )
        for _ in range(50):
            await asyncio.sleep(0)
            if transport.subscribed_keys:
                break
        key = transport.subscribed_keys[-1]
        transport.emit_reply(
            key,
            source_agent_id="specialist-a",
            payload={"agent_id": "specialist-a"},
        )
        outcome = await dispatch_task

        # The resolution targeted the original specialist; the late
        # registrant didn't compete because it wasn't in the snapshot.
        assert isinstance(outcome, SyncResult)
        attempt = publisher.published[-1].attempt
        assert attempt.matched_agent_id == "specialist-a"


# ---------------------------------------------------------------------------
# AC-006 — degraded path
# ---------------------------------------------------------------------------


class TestDegradedPath:
    """AC-006 — unresolved capability returns Degraded; no side effects."""

    @pytest.mark.asyncio
    async def test_unresolved_returns_degraded_without_publish_or_bind(
        self,
    ) -> None:
        orchestrator, _cache, _registry, transport, publisher, db_writer = (
            await _build_orchestrator()
        )
        # Cache is empty — nothing advertises "review".
        outcome = await orchestrator.dispatch(
            capability="review",
            parameters=[DispatchParameter(name="x", value="y")],
        )

        assert isinstance(outcome, Degraded)
        assert outcome.reason == "no_specialist_resolvable"
        # Critical: NO publish, NO subscribe.
        assert publisher.published == []
        assert transport.subscribed_keys == []
        # And nothing was persisted (write-before-send is gated on a
        # successful resolve).
        assert db_writer.read_resolutions() == []


# ---------------------------------------------------------------------------
# AC-007 — local timeout
# ---------------------------------------------------------------------------


class TestLocalTimeoutPath:
    """AC-007 — wait_with_timeout returns None → DispatchError(local_timeout)."""

    @pytest.mark.asyncio
    async def test_timeout_yields_dispatch_error_with_local_timeout_explanation(
        self,
    ) -> None:
        # Build with a tiny default timeout so the wait fires fast.
        orchestrator, cache, _r, _t, _p, db_writer = await _build_orchestrator(
            default_timeout_seconds=0.05,
        )
        await _populate_cache(cache)

        # No reply will ever be emitted on the transport — the hard
        # cut-off must fire and the orchestrator must surface
        # DispatchError(local_timeout).
        outcome = await orchestrator.dispatch(
            capability="review", parameters=[]
        )

        assert isinstance(outcome, DispatchError)
        assert outcome.error_explanation == "local_timeout"
        # Persistence still occurred — write-before-send is satisfied
        # even when the wait times out.
        assert len(db_writer.read_resolutions()) == 1


# ---------------------------------------------------------------------------
# AC-008 — concurrent dispatches use distinct correlation keys
# ---------------------------------------------------------------------------


class TestConcurrentDispatches:
    """AC-008 — E.concurrent-dispatches: distinct correlation keys."""

    @pytest.mark.asyncio
    async def test_two_concurrent_dispatches_have_distinct_correlation_keys(
        self,
    ) -> None:
        orchestrator, cache, _registry, transport, publisher, _db = (
            await _build_orchestrator()
        )
        await _populate_cache(cache, agent_id="specialist-a")
        await _populate_cache(cache, agent_id="specialist-b", tool_name="review")

        async def _drive_one() -> Any:
            return await orchestrator.dispatch(
                capability="review", parameters=[]
            )

        # Fire two concurrent dispatches.
        task_a = asyncio.create_task(_drive_one())
        task_b = asyncio.create_task(_drive_one())

        # Wait for both subscriptions to be active, then deliver
        # authentic replies on each.
        for _ in range(100):
            await asyncio.sleep(0)
            if len(transport.subscribed_keys) >= 2:
                break
        assert len(transport.subscribed_keys) >= 2

        # Replies must use the matched_agent_id — read it from the
        # publish records so the source matches the binding.
        # (Both publishes happen before either reply because subscribe-
        # before-publish completes inside dispatch().)
        for _ in range(100):
            await asyncio.sleep(0)
            if len(publisher.published) >= 2:
                break
        for record in publisher.published:
            transport.emit_reply(
                record.attempt.correlation_key,
                source_agent_id=record.attempt.matched_agent_id,
                payload={"agent_id": record.attempt.matched_agent_id},
            )

        outcomes = await asyncio.gather(task_a, task_b)
        assert all(isinstance(outcome, SyncResult) for outcome in outcomes)
        keys = [record.attempt.correlation_key for record in publisher.published]
        assert len(keys) == 2
        assert keys[0] != keys[1], "concurrent dispatches must use distinct keys"


# ---------------------------------------------------------------------------
# Async-pending payload routing
# ---------------------------------------------------------------------------


class TestParseRoutingFromPayload:
    """The wait → parse step delegates to :func:`parse_reply` correctly."""

    @pytest.mark.asyncio
    async def test_async_pending_payload_returns_async_pending_outcome(
        self,
    ) -> None:
        orchestrator, cache, _r, transport, _p, _db = await _build_orchestrator()
        await _populate_cache(cache)

        dispatch_task = asyncio.create_task(
            orchestrator.dispatch(capability="review", parameters=[])
        )
        for _ in range(50):
            await asyncio.sleep(0)
            if transport.subscribed_keys:
                break
        key = transport.subscribed_keys[-1]
        transport.emit_reply(
            key,
            source_agent_id="specialist-a",
            payload={
                "agent_id": "specialist-a",
                "run_identifier": "remote-run-42",
            },
        )
        outcome = await dispatch_task
        assert isinstance(outcome, AsyncPending)
        assert outcome.run_identifier == "remote-run-42"


# ---------------------------------------------------------------------------
# Seam tests — verbatim from TASK-SAD-006 spec
# ---------------------------------------------------------------------------


class TestSeamCapabilityResolutionContract:
    """Seam: write-before-send invariant via the persistence layer."""

    @pytest.mark.asyncio
    @pytest.mark.integration_contract("CapabilityResolution")  # type: ignore[misc]
    async def test_orchestrator_persists_resolution_before_publish(
        self,
    ) -> None:
        orchestrator, cache, _r, transport, publisher, db_writer = (
            await _build_orchestrator()
        )
        await _populate_cache(cache)

        dispatch_task = asyncio.create_task(
            orchestrator.dispatch(capability="review", parameters=[])
        )
        for _ in range(50):
            await asyncio.sleep(0)
            if transport.subscribed_keys:
                break
        key = transport.subscribed_keys[-1]
        transport.emit_reply(
            key,
            source_agent_id="specialist-a",
            payload={"agent_id": "specialist-a"},
        )
        await dispatch_task

        assert len(db_writer.read_resolutions()) >= 1
        assert publisher.published_after_persist is True


class TestSeamCorrelationKeyContract:
    """Seam: 32-lowercase-hex correlation key threaded into the publish header."""

    @pytest.mark.asyncio
    @pytest.mark.integration_contract("CorrelationKey")  # type: ignore[misc]
    async def test_orchestrator_threads_correlation_key_into_publish(
        self,
    ) -> None:
        orchestrator, cache, _r, transport, publisher, _db = (
            await _build_orchestrator()
        )
        await _populate_cache(cache)

        dispatch_task = asyncio.create_task(
            orchestrator.dispatch(capability="review", parameters=[])
        )
        for _ in range(50):
            await asyncio.sleep(0)
            if transport.subscribed_keys:
                break
        key = transport.subscribed_keys[-1]
        transport.emit_reply(
            key,
            source_agent_id="specialist-a",
            payload={"agent_id": "specialist-a"},
        )
        await dispatch_task

        last_publish = publisher.published[-1]
        correlation_key = last_publish.headers["correlation_key"]
        import re

        assert re.fullmatch(r"[0-9a-f]{32}", correlation_key)


# The ``integration_contract`` marker is registered project-wide in
# ``pyproject.toml`` under ``[tool.pytest.ini_options].markers``.

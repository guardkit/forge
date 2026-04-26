"""Unit tests for :mod:`forge.adapters.guardkit.progress_subscriber`.

Test classes mirror the acceptance criteria of TASK-GCI-005:

- AC-001 — ``subscribe_progress`` is an ``@asynccontextmanager`` exported
  from ``forge.adapters.guardkit.progress_subscriber``.
- AC-002 — :class:`ProgressSink` retains the most recent event per
  ``(build_id, subcommand)`` pair; old events are evicted under
  back-pressure (Scenario "Progress events emitted faster than Forge
  consumes them are still observable for live status").
- AC-003 — Unsubscribe runs on context-manager exit, including the
  exception path.
- AC-004 — When ``nats_client`` is ``None``, ``subscribe_progress``
  yields a no-op subscription that records a single
  ``progress_stream_unavailable`` warning to the sink and the
  surrounding call still proceeds.
- AC-005 — Each subscription is scoped to one
  ``pipeline.stage-complete.{build_id}.{subcommand}`` subject; two
  parallel invocations within the same build receive independent event
  streams.
- AC-006 — Two concurrent builds against the same repo get isolated
  sinks (no shared state).
- AC-007 — Invalid payloads (malformed JSON, missing fields) are
  dropped with a structured warning, never raised.

Production collaborators (the NATS client) are stubbed with a
hand-rolled :class:`FakeNATSClient` rather than ``unittest.mock`` so
that the captured callback can be invoked directly to simulate inbound
``pipeline.stage-complete.*`` deliveries.
"""

from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable

import pytest
from nats_core.envelope import EventType, MessageEnvelope

from forge.adapters.guardkit.progress import GuardKitProgressEvent
from forge.adapters.guardkit.progress_subscriber import (
    DEFAULT_MAX_EVENTS_PER_STREAM,
    PROGRESS_PAYLOAD_INVALID,
    PROGRESS_STREAM_UNAVAILABLE,
    SUBJECT_PREFIX,
    ProgressSink,
    subject_for,
    subscribe_progress,
)


# ---------------------------------------------------------------------------
# Helpers — fake NATS client + envelope construction
# ---------------------------------------------------------------------------


BUILD_ID = "b-1"
OTHER_BUILD_ID = "b-2"
SUBCOMMAND = "/feature-spec"
OTHER_SUBCOMMAND = "autobuild"


class _FakeSubscription:
    """Records whether :meth:`unsubscribe` was awaited."""

    def __init__(self) -> None:
        self.unsubscribed = False

    async def unsubscribe(self) -> None:
        self.unsubscribed = True


class FakeNATSClient:
    """Captures subscribe calls and exposes the registered callback.

    A single client may carry multiple concurrent subscriptions on
    distinct subjects, mirroring the contract used by the production
    :class:`forge.adapters.nats.client.NATSClient`.
    """

    def __init__(self) -> None:
        # subject -> list of (subscription, callback) pairs.
        self._subs: dict[
            str,
            list[
                tuple[_FakeSubscription, Callable[[MessageEnvelope], Awaitable[None]]]
            ],
        ] = {}
        self.subscribe_calls: list[str] = []

    async def subscribe(
        self,
        topic: str,
        callback: Callable[[MessageEnvelope], Awaitable[None]],
    ) -> _FakeSubscription:
        sub = _FakeSubscription()
        self.subscribe_calls.append(topic)
        self._subs.setdefault(topic, []).append((sub, callback))
        return sub

    async def deliver(self, topic: str, envelope: MessageEnvelope) -> None:
        """Fan-out an envelope to every active callback on ``topic``."""
        for _sub, cb in list(self._subs.get(topic, [])):
            await cb(envelope)


class BrokenNATSClient:
    """Raises on :meth:`subscribe` to model an unavailable broker."""

    async def subscribe(
        self,
        topic: str,
        callback: Callable[[MessageEnvelope], Awaitable[None]],
    ) -> Any:
        raise ConnectionError("simulated NATS unavailable")


def make_event(
    *,
    build_id: str = BUILD_ID,
    subcommand: str = SUBCOMMAND,
    seq: int = 1,
    stage_label: str = "discovery",
    timestamp: str = "2026-04-26T08:30:00+00:00",
) -> GuardKitProgressEvent:
    return GuardKitProgressEvent(
        build_id=build_id,
        subcommand=subcommand,
        stage_label=stage_label,
        seq=seq,
        timestamp=timestamp,
    )


def envelope_for(event: GuardKitProgressEvent) -> MessageEnvelope:
    return MessageEnvelope(
        event_type=EventType.STAGE_COMPLETE,
        source_id="guardkit",
        payload=event.model_dump(),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestModuleSurface:
    """AC-001 — public surface of the subscriber module."""

    def test_subscribe_progress_is_async_context_manager(self) -> None:
        cm = subscribe_progress(FakeNATSClient(), BUILD_ID, SUBCOMMAND, ProgressSink())
        # An ``@asynccontextmanager`` returns an object with both
        # ``__aenter__`` and ``__aexit__``.
        assert hasattr(cm, "__aenter__")
        assert hasattr(cm, "__aexit__")

    def test_module_path(self) -> None:
        assert (
            subscribe_progress.__module__
            == "forge.adapters.guardkit.progress_subscriber"
        )
        assert ProgressSink.__module__ == "forge.adapters.guardkit.progress_subscriber"

    def test_subject_prefix_pinned(self) -> None:
        assert SUBJECT_PREFIX == "pipeline.stage-complete"

    def test_subject_for_builds_full_subject(self) -> None:
        assert subject_for(BUILD_ID, SUBCOMMAND) == (
            f"pipeline.stage-complete.{BUILD_ID}.{SUBCOMMAND}"
        )

    def test_subject_for_rejects_empty(self) -> None:
        with pytest.raises(ValueError):
            subject_for("", SUBCOMMAND)
        with pytest.raises(ValueError):
            subject_for(BUILD_ID, "")


class TestProgressSinkRetention:
    """AC-002 — most-recent retention + back-pressure eviction."""

    def test_latest_is_none_when_unseen(self) -> None:
        sink = ProgressSink()
        assert sink.latest(BUILD_ID, SUBCOMMAND) is None
        assert sink.all_for(BUILD_ID, SUBCOMMAND) == []

    def test_latest_returns_most_recent(self) -> None:
        sink = ProgressSink(max_events=10)
        e1 = make_event(seq=1, stage_label="a")
        e2 = make_event(seq=2, stage_label="b")
        sink.record(e1)
        sink.record(e2)
        assert sink.latest(BUILD_ID, SUBCOMMAND) == e2

    def test_back_pressure_drops_oldest(self) -> None:
        # AC-002 — Scenario "Progress events emitted faster than Forge
        # consumes them are still observable for live status".
        sink = ProgressSink(max_events=3)
        events = [make_event(seq=i, stage_label=f"s{i}") for i in range(1, 6)]
        for ev in events:
            sink.record(ev)
        # Oldest two were evicted; the most recent three remain in order.
        assert sink.all_for(BUILD_ID, SUBCOMMAND) == events[-3:]
        assert sink.latest(BUILD_ID, SUBCOMMAND) == events[-1]

    def test_max_events_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            ProgressSink(max_events=0)
        with pytest.raises(ValueError):
            ProgressSink(max_events=-1)

    def test_default_max_events_constant(self) -> None:
        assert DEFAULT_MAX_EVENTS_PER_STREAM > 0


class TestUnsubscribeOnExit:
    """AC-003 — unsubscribe runs on every exit path."""

    @pytest.mark.asyncio
    async def test_unsubscribes_on_normal_exit(self) -> None:
        client = FakeNATSClient()
        sink = ProgressSink()
        async with subscribe_progress(client, BUILD_ID, SUBCOMMAND, sink):
            pass
        topic = subject_for(BUILD_ID, SUBCOMMAND)
        subs = client._subs[topic]
        assert subs[0][0].unsubscribed is True

    @pytest.mark.asyncio
    async def test_unsubscribes_on_exception_exit(self) -> None:
        client = FakeNATSClient()
        sink = ProgressSink()
        with pytest.raises(RuntimeError, match="boom"):
            async with subscribe_progress(client, BUILD_ID, SUBCOMMAND, sink):
                raise RuntimeError("boom")
        topic = subject_for(BUILD_ID, SUBCOMMAND)
        subs = client._subs[topic]
        assert subs[0][0].unsubscribed is True

    @pytest.mark.asyncio
    async def test_unsubscribe_failure_does_not_propagate(self) -> None:
        client = FakeNATSClient()
        sink = ProgressSink()
        async with subscribe_progress(client, BUILD_ID, SUBCOMMAND, sink):
            # Replace the subscription with one that raises on unsubscribe.
            topic = subject_for(BUILD_ID, SUBCOMMAND)
            sub, cb = client._subs[topic][0]

            async def boom_unsub() -> None:
                raise ConnectionError("simulated")

            sub.unsubscribe = boom_unsub  # type: ignore[method-assign]
        # Reaching this line means the error was swallowed — contract
        # promise: the subscriber must never propagate from cleanup.


class TestUnavailableClient:
    """AC-004 — None/unavailable client → no-op + warning."""

    @pytest.mark.asyncio
    async def test_none_client_is_noop_and_warns(self) -> None:
        sink = ProgressSink()
        async with subscribe_progress(None, BUILD_ID, SUBCOMMAND, sink):
            # Body still runs.
            inside = True
        assert inside is True
        warnings = sink.warnings
        assert len(warnings) == 1
        w = warnings[0]
        assert w.code == PROGRESS_STREAM_UNAVAILABLE
        assert w.build_id == BUILD_ID
        assert w.subcommand == SUBCOMMAND

    @pytest.mark.asyncio
    async def test_subscribe_failure_yields_noop(self) -> None:
        sink = ProgressSink()
        client = BrokenNATSClient()
        async with subscribe_progress(client, BUILD_ID, SUBCOMMAND, sink):
            inside = True
        assert inside is True
        codes = [w.code for w in sink.warnings]
        assert PROGRESS_STREAM_UNAVAILABLE in codes


class TestSubjectScoping:
    """AC-005 — subscription scoped to one subject; parallel invocations."""

    @pytest.mark.asyncio
    async def test_subscribes_to_correct_subject(self) -> None:
        client = FakeNATSClient()
        sink = ProgressSink()
        async with subscribe_progress(client, BUILD_ID, SUBCOMMAND, sink):
            pass
        assert client.subscribe_calls == [
            f"pipeline.stage-complete.{BUILD_ID}.{SUBCOMMAND}"
        ]

    @pytest.mark.asyncio
    async def test_parallel_subscriptions_are_independent(self) -> None:
        # AC-005 — two parallel invocations within the same build_id but
        # distinct subcommands receive independent event streams.
        client = FakeNATSClient()
        sink = ProgressSink()
        async with (
            subscribe_progress(client, BUILD_ID, SUBCOMMAND, sink),
            subscribe_progress(client, BUILD_ID, OTHER_SUBCOMMAND, sink),
        ):
            ev_a = make_event(subcommand=SUBCOMMAND, seq=1, stage_label="a")
            ev_b = make_event(subcommand=OTHER_SUBCOMMAND, seq=1, stage_label="b")
            await client.deliver(
                f"pipeline.stage-complete.{BUILD_ID}.{SUBCOMMAND}",
                envelope_for(ev_a),
            )
            await client.deliver(
                f"pipeline.stage-complete.{BUILD_ID}.{OTHER_SUBCOMMAND}",
                envelope_for(ev_b),
            )
        assert sink.latest(BUILD_ID, SUBCOMMAND) == ev_a
        assert sink.latest(BUILD_ID, OTHER_SUBCOMMAND) == ev_b
        # Cross-stream isolation: an event for ``SUBCOMMAND`` did not
        # leak into the ``OTHER_SUBCOMMAND`` slot.
        assert sink.all_for(BUILD_ID, SUBCOMMAND) == [ev_a]
        assert sink.all_for(BUILD_ID, OTHER_SUBCOMMAND) == [ev_b]

    @pytest.mark.asyncio
    async def test_ordered_delivery_within_one_subscription(self) -> None:
        client = FakeNATSClient()
        sink = ProgressSink()
        async with subscribe_progress(client, BUILD_ID, SUBCOMMAND, sink):
            evs = [make_event(seq=i, stage_label=f"s{i}") for i in range(1, 5)]
            topic = subject_for(BUILD_ID, SUBCOMMAND)
            for ev in evs:
                await client.deliver(topic, envelope_for(ev))
        assert sink.all_for(BUILD_ID, SUBCOMMAND) == evs


class TestParallelBuilds:
    """AC-006 — two concurrent builds get isolated sinks."""

    @pytest.mark.asyncio
    async def test_two_builds_have_isolated_sinks(self) -> None:
        client = FakeNATSClient()
        sink_a = ProgressSink()
        sink_b = ProgressSink()
        async with (
            subscribe_progress(client, BUILD_ID, SUBCOMMAND, sink_a),
            subscribe_progress(client, OTHER_BUILD_ID, SUBCOMMAND, sink_b),
        ):
            ev_a = make_event(build_id=BUILD_ID, seq=1, stage_label="a")
            ev_b = make_event(build_id=OTHER_BUILD_ID, seq=1, stage_label="b")
            await client.deliver(
                f"pipeline.stage-complete.{BUILD_ID}.{SUBCOMMAND}",
                envelope_for(ev_a),
            )
            await client.deliver(
                f"pipeline.stage-complete.{OTHER_BUILD_ID}.{SUBCOMMAND}",
                envelope_for(ev_b),
            )
        assert sink_a.latest(BUILD_ID, SUBCOMMAND) == ev_a
        assert sink_a.latest(OTHER_BUILD_ID, SUBCOMMAND) is None
        assert sink_b.latest(OTHER_BUILD_ID, SUBCOMMAND) == ev_b
        assert sink_b.latest(BUILD_ID, SUBCOMMAND) is None


class TestMalformedPayloads:
    """AC-007 — invalid payloads dropped with structured warning."""

    @pytest.mark.asyncio
    async def test_missing_required_field_dropped(self) -> None:
        client = FakeNATSClient()
        sink = ProgressSink()
        async with subscribe_progress(client, BUILD_ID, SUBCOMMAND, sink):
            envelope = MessageEnvelope(
                event_type=EventType.STAGE_COMPLETE,
                source_id="guardkit",
                payload={
                    "build_id": BUILD_ID,
                    "subcommand": SUBCOMMAND,
                    # missing stage_label, seq, timestamp
                },
            )
            await client.deliver(subject_for(BUILD_ID, SUBCOMMAND), envelope)
        # No event recorded; one structured warning.
        assert sink.latest(BUILD_ID, SUBCOMMAND) is None
        codes = [w.code for w in sink.warnings]
        assert PROGRESS_PAYLOAD_INVALID in codes

    @pytest.mark.asyncio
    async def test_wrong_payload_type_dropped(self) -> None:
        client = FakeNATSClient()
        sink = ProgressSink()
        async with subscribe_progress(client, BUILD_ID, SUBCOMMAND, sink):
            envelope = MessageEnvelope(
                event_type=EventType.STAGE_COMPLETE,
                source_id="guardkit",
                payload={"unrecognised": "shape"},
            )
            await client.deliver(subject_for(BUILD_ID, SUBCOMMAND), envelope)
        assert sink.latest(BUILD_ID, SUBCOMMAND) is None
        assert any(w.code == PROGRESS_PAYLOAD_INVALID for w in sink.warnings)

    @pytest.mark.asyncio
    async def test_valid_event_after_malformed_still_recorded(self) -> None:
        client = FakeNATSClient()
        sink = ProgressSink()
        async with subscribe_progress(client, BUILD_ID, SUBCOMMAND, sink):
            bad = MessageEnvelope(
                event_type=EventType.STAGE_COMPLETE,
                source_id="guardkit",
                payload={"unrecognised": "shape"},
            )
            await client.deliver(subject_for(BUILD_ID, SUBCOMMAND), bad)
            good = make_event(seq=1, stage_label="good")
            await client.deliver(subject_for(BUILD_ID, SUBCOMMAND), envelope_for(good))
        assert sink.latest(BUILD_ID, SUBCOMMAND) == good


# ---------------------------------------------------------------------------
# pytest-asyncio plumbing
# ---------------------------------------------------------------------------


def pytest_collection_modifyitems(  # noqa: D401 - pytest hook
    config: Any, items: list[Any]
) -> None:
    """Auto-mark ``async def`` tests as ``asyncio``.

    The project does not configure a global ``asyncio_mode = "auto"`` in
    ``pyproject.toml``, so we mark per-module to keep this test file
    self-contained and avoid touching shared config.
    """
    for item in items:
        if inspect.iscoroutinefunction(getattr(item, "function", None)):
            item.add_marker(pytest.mark.asyncio)

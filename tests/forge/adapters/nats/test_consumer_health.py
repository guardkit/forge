"""FEAT-PAC PAC-001 — ack-slot inspection + phantom-ack cure (module tests).

Every test drives :mod:`forge.adapters.nats.consumer_health` against a mocked
JetStream context — NO broker, NO NATS connection, NO port 4222 (broker
isolation, standing playbook). The ``NotFoundError`` used to simulate the
purged-message signal is the real ``nats.js.errors.NotFoundError`` class so the
``except NotFoundError`` discriminator is exercised against production types.

Test map:

* :class:`TestInspectHealthy` — free slot ⇒ ``healthy``, no probe.
* :class:`TestInspectHeld` — occupied slot + message present ⇒ ``held``.
* :class:`TestInspectPhantom` — occupied slot + NotFoundError ⇒ ``phantom``.
* :class:`TestInspectUnknown` — ack_floor None, consumer_info raises, and
  get_msg raising a non-NotFound error all ⇒ ``unknown`` (never phantom, never
  a cure).
* :class:`TestPendingSeqArithmetic` — ``pending_seq == ack_floor.stream_seq+1``.
* :class:`TestCurePhantom` — delete called with (stream, durable); error ⇒
  ``False`` without raising; success ⇒ ``True``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from nats.js.errors import NotFoundError

from forge.adapters.nats.consumer_health import (
    AckSlotReport,
    cure_phantom,
    inspect_ack_slot,
)

STREAM = "PIPELINE"
DURABLE = "forge-serve"


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeSequenceInfo:
    """Minimal stand-in for ``nats.js.api.SequenceInfo`` (only stream_seq used)."""

    def __init__(self, stream_seq: int | None) -> None:
        self.stream_seq = stream_seq


class _FakeConsumerInfo:
    """Minimal stand-in for ``nats.js.api.ConsumerInfo`` (Optional counters)."""

    def __init__(
        self,
        *,
        num_ack_pending: int | None = 0,
        num_waiting: int | None = 0,
        num_pending: int | None = 0,
        ack_floor: _FakeSequenceInfo | None = None,
    ) -> None:
        self.num_ack_pending = num_ack_pending
        self.num_waiting = num_waiting
        self.num_pending = num_pending
        self.ack_floor = ack_floor


def _make_js(
    *,
    consumer_info: object | Exception,
    get_msg: object | Exception | None = None,
) -> AsyncMock:
    """Build a mock JetStream context.

    ``consumer_info`` / ``get_msg`` may be a value to return or an exception
    instance to raise (via ``side_effect``).
    """
    js = AsyncMock()

    if isinstance(consumer_info, Exception):
        js.consumer_info.side_effect = consumer_info
    else:
        js.consumer_info.return_value = consumer_info

    if isinstance(get_msg, Exception):
        js.get_msg.side_effect = get_msg
    else:
        js.get_msg.return_value = get_msg

    return js


# ---------------------------------------------------------------------------
# inspect_ack_slot — healthy
# ---------------------------------------------------------------------------


class TestInspectHealthy:
    @pytest.mark.asyncio
    async def test_no_ack_pending_is_healthy_and_never_probes(self) -> None:
        js = _make_js(
            consumer_info=_FakeConsumerInfo(
                num_ack_pending=0, num_waiting=1, num_pending=0
            )
        )

        report = await inspect_ack_slot(js, STREAM, DURABLE)

        assert isinstance(report, AckSlotReport)
        assert report.status == "healthy"
        assert report.pending_seq is None
        assert report.num_ack_pending == 0
        assert report.num_waiting == 1
        assert report.num_pending == 0
        # A free slot must not trigger a stream probe.
        js.get_msg.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_none_ack_pending_counter_treated_as_healthy(self) -> None:
        js = _make_js(
            consumer_info=_FakeConsumerInfo(
                num_ack_pending=None, num_waiting=None, num_pending=None
            )
        )

        report = await inspect_ack_slot(js, STREAM, DURABLE)

        assert report.status == "healthy"
        assert report.num_ack_pending == 0
        assert report.num_waiting == 0
        assert report.num_pending == 0
        js.get_msg.assert_not_awaited()


# ---------------------------------------------------------------------------
# inspect_ack_slot — held (legitimate long-held ack)
# ---------------------------------------------------------------------------


class TestInspectHeld:
    @pytest.mark.asyncio
    async def test_message_present_is_held(self) -> None:
        js = _make_js(
            consumer_info=_FakeConsumerInfo(
                num_ack_pending=1,
                num_waiting=1,
                num_pending=0,
                ack_floor=_FakeSequenceInfo(41),
            ),
            get_msg=object(),  # any non-exception return ⇒ message exists
        )

        report = await inspect_ack_slot(js, STREAM, DURABLE)

        assert report.status == "held"
        assert report.pending_seq == 42
        assert report.num_ack_pending == 1
        js.get_msg.assert_awaited_once_with(STREAM, seq=42)


# ---------------------------------------------------------------------------
# inspect_ack_slot — phantom (the wedge)
# ---------------------------------------------------------------------------


class TestInspectPhantom:
    @pytest.mark.asyncio
    async def test_not_found_is_phantom(self) -> None:
        js = _make_js(
            consumer_info=_FakeConsumerInfo(
                num_ack_pending=1,
                num_waiting=1,
                num_pending=0,
                ack_floor=_FakeSequenceInfo(99),
            ),
            get_msg=NotFoundError(),
        )

        report = await inspect_ack_slot(js, STREAM, DURABLE)

        assert report.status == "phantom"
        assert report.pending_seq == 100
        js.get_msg.assert_awaited_once_with(STREAM, seq=100)


# ---------------------------------------------------------------------------
# inspect_ack_slot — unknown (absence-of-failure: never phantom, never cure)
# ---------------------------------------------------------------------------


class TestInspectUnknown:
    @pytest.mark.asyncio
    async def test_ack_floor_none_is_unknown(self) -> None:
        js = _make_js(
            consumer_info=_FakeConsumerInfo(
                num_ack_pending=1,
                num_waiting=1,
                num_pending=0,
                ack_floor=None,
            )
        )

        report = await inspect_ack_slot(js, STREAM, DURABLE)

        assert report.status == "unknown"
        assert report.pending_seq is None
        assert report.num_ack_pending == 1
        # No sequence to name ⇒ no probe.
        js.get_msg.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ack_floor_stream_seq_none_is_unknown(self) -> None:
        js = _make_js(
            consumer_info=_FakeConsumerInfo(
                num_ack_pending=1,
                ack_floor=_FakeSequenceInfo(None),
            )
        )

        report = await inspect_ack_slot(js, STREAM, DURABLE)

        assert report.status == "unknown"
        assert report.pending_seq is None
        js.get_msg.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_consumer_info_raises_is_unknown(self) -> None:
        js = _make_js(consumer_info=RuntimeError("broker unreachable"))

        report = await inspect_ack_slot(js, STREAM, DURABLE)

        assert report.status == "unknown"
        assert report.pending_seq is None
        assert report.num_ack_pending == 0
        js.get_msg.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_consumer_not_found_is_absent_not_unknown(self) -> None:
        # Coordinator-review regression pin: a MISSING durable is its own
        # honest verdict ("absent" — no consumer, no ack slot), NOT a generic
        # error. This is the state right after a phantom cure deletes the
        # consumer (the re-verify must land here) and on a first-ever boot.
        js = _make_js(consumer_info=NotFoundError())

        report = await inspect_ack_slot(js, STREAM, DURABLE)

        assert report.status == "absent"
        assert report.pending_seq is None
        assert report.num_ack_pending == 0
        assert "does not exist" in report.detail
        js.get_msg.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_get_msg_other_error_is_unknown_not_phantom(self) -> None:
        js = _make_js(
            consumer_info=_FakeConsumerInfo(
                num_ack_pending=1,
                num_waiting=1,
                ack_floor=_FakeSequenceInfo(10),
            ),
            get_msg=TimeoutError("request timed out"),
        )

        report = await inspect_ack_slot(js, STREAM, DURABLE)

        # A non-NotFound API error must never be classified as phantom.
        assert report.status == "unknown"
        assert report.pending_seq == 11
        js.get_msg.assert_awaited_once_with(STREAM, seq=11)


# ---------------------------------------------------------------------------
# pending_seq arithmetic
# ---------------------------------------------------------------------------


class TestPendingSeqArithmetic:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("ack_floor_seq", "expected_pending"),
        [(0, 1), (1, 2), (41, 42), (10099, 10100)],
    )
    async def test_pending_seq_is_ack_floor_plus_one(
        self, ack_floor_seq: int, expected_pending: int
    ) -> None:
        js = _make_js(
            consumer_info=_FakeConsumerInfo(
                num_ack_pending=1,
                ack_floor=_FakeSequenceInfo(ack_floor_seq),
            ),
            get_msg=object(),  # present ⇒ held; we only assert the seq
        )

        report = await inspect_ack_slot(js, STREAM, DURABLE)

        assert report.pending_seq == expected_pending
        js.get_msg.assert_awaited_once_with(STREAM, seq=expected_pending)


# ---------------------------------------------------------------------------
# cure_phantom
# ---------------------------------------------------------------------------


class TestCurePhantom:
    @pytest.mark.asyncio
    async def test_cure_deletes_consumer_and_returns_true(self) -> None:
        js = AsyncMock()
        js.delete_consumer.return_value = True

        result = await cure_phantom(js, STREAM, DURABLE)

        assert result is True
        js.delete_consumer.assert_awaited_once_with(STREAM, DURABLE)

    @pytest.mark.asyncio
    async def test_cure_returns_false_on_error_without_raising(self) -> None:
        js = AsyncMock()
        js.delete_consumer.side_effect = RuntimeError("delete failed")

        # Must not raise — a cure failure is reported, never propagated.
        result = await cure_phantom(js, STREAM, DURABLE)

        assert result is False
        js.delete_consumer.assert_awaited_once_with(STREAM, DURABLE)

    @pytest.mark.asyncio
    async def test_cure_only_deletes_never_recreates(self) -> None:
        """The daemon's _attach_consumer bind-or-create owns recreation."""
        js = AsyncMock()

        await cure_phantom(js, STREAM, DURABLE)

        js.delete_consumer.assert_awaited_once_with(STREAM, DURABLE)
        js.add_consumer.assert_not_awaited()
        js.pull_subscribe.assert_not_awaited()

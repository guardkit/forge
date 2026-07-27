"""FEAT-PAC PAC-002 — boot cure step + runtime alarm watchdog (serve wiring).

These tests drive the FEAT-PAC boot/watchdog machinery in
:mod:`forge.cli.serve` against MOCK JetStream contexts — NO broker, NO NATS
connection, NO port 4222 (broker isolation, standing playbook). The
``client.jetstream()`` call is a plain sync mock returning an ``AsyncMock``
JetStream context; ``inspect_ack_slot`` / ``cure_phantom`` run for real
against those mocks so the boot step is exercised end-to-end, and the
``delete_consumer`` mock lets each test prove whether a cure (delete) did or
did not happen.

Test map:

* :class:`TestAckWatchdogInterval` — the ``FORGE_ACK_WATCHDOG_SECONDS``
  resolver: default 300, ``0``/negative/invalid ⇒ disabled (``0``).
* :class:`TestAckSlotBootCheck` — phantom ⇒ cure + re-inspect receipt +
  state=healthy; held ⇒ NO delete ever + state=held; unknown ⇒ no action;
  inspect raising ⇒ boot proceeds (whole-step exception guard).
* :class:`TestAckWatchdog` — interval firing sets the flag on phantom, NEVER
  deletes on any status, and is a no-op when disabled at 0.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from nats.js.errors import NotFoundError

from forge.cli import serve
from forge.cli._serve_config import ServeConfig
from forge.cli._serve_state import SubscriptionState
from forge.cli.serve import (
    DEFAULT_ACK_WATCHDOG_SECONDS,
    _ack_slot_boot_check,
    _ack_watchdog_interval_seconds,
    _run_ack_watchdog,
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


def _info_healthy() -> _FakeConsumerInfo:
    return _FakeConsumerInfo(num_ack_pending=0, num_waiting=1, num_pending=0)


def _info_occupied(seq: int = 41) -> _FakeConsumerInfo:
    # ack_floor.stream_seq = seq ⇒ pending_seq = seq + 1.
    return _FakeConsumerInfo(
        num_ack_pending=1,
        num_waiting=1,
        num_pending=0,
        ack_floor=_FakeSequenceInfo(seq),
    )


def _js_for_status(status: str) -> AsyncMock:
    """Build a JetStream mock whose real ``inspect_ack_slot`` yields ``status``."""
    js = AsyncMock()
    if status == "healthy":
        js.consumer_info.return_value = _info_healthy()
    elif status == "held":
        js.consumer_info.return_value = _info_occupied()
        js.get_msg.return_value = object()  # message present ⇒ held
    elif status == "phantom":
        js.consumer_info.return_value = _info_occupied()
        js.get_msg.side_effect = NotFoundError()
    elif status == "unknown":
        # Occupied but no ack_floor ⇒ cannot name the sequence ⇒ unknown.
        js.consumer_info.return_value = _FakeConsumerInfo(
            num_ack_pending=1, num_waiting=1, num_pending=0, ack_floor=None
        )
    elif status == "absent":
        # Durable does not exist ⇒ consumer_info raises NotFoundError.
        js.consumer_info.side_effect = NotFoundError()
    else:  # pragma: no cover - guard
        raise ValueError(status)
    return js


def _client_with_js(js: AsyncMock) -> Mock:
    """Wrap a JetStream mock so ``client.jetstream()`` (sync) returns it."""
    client = Mock()
    client.jetstream.return_value = js
    return client


# ---------------------------------------------------------------------------
# FORGE_ACK_WATCHDOG_SECONDS resolver
# ---------------------------------------------------------------------------


class TestAckWatchdogInterval:
    def test_absent_env_uses_default(self) -> None:
        assert _ack_watchdog_interval_seconds({}) == DEFAULT_ACK_WATCHDOG_SECONDS
        assert DEFAULT_ACK_WATCHDOG_SECONDS == 300

    def test_positive_value_is_honoured(self) -> None:
        assert (
            _ack_watchdog_interval_seconds({"FORGE_ACK_WATCHDOG_SECONDS": "600"})
            == 600
        )

    def test_zero_disables(self) -> None:
        assert (
            _ack_watchdog_interval_seconds({"FORGE_ACK_WATCHDOG_SECONDS": "0"})
            == 0
        )

    def test_negative_disables(self) -> None:
        assert (
            _ack_watchdog_interval_seconds({"FORGE_ACK_WATCHDOG_SECONDS": "-5"})
            == 0
        )

    def test_unparseable_disables_without_raising(self) -> None:
        assert (
            _ack_watchdog_interval_seconds({"FORGE_ACK_WATCHDOG_SECONDS": "abc"})
            == 0
        )


# ---------------------------------------------------------------------------
# Boot cure step
# ---------------------------------------------------------------------------


class TestAckSlotBootCheck:
    @pytest.mark.asyncio
    async def test_phantom_is_cured_and_reverify_reads_absent(self) -> None:
        # PRODUCTION TRUTH (coordinator-review regression pin): after the cure
        # deletes the durable, the re-inspect's consumer_info raises
        # NotFoundError — the consumer is GONE. inspect_ack_slot must classify
        # that as "absent" (cured success: no consumer ⇒ no ack slot), NOT
        # fall into the generic error branch as "unknown" (which made a real
        # cure impossible to verify — the mocked-healthy re-inspect masked it).
        js = AsyncMock()
        js.consumer_info.side_effect = [_info_occupied(41), NotFoundError()]
        js.get_msg.side_effect = NotFoundError()  # detection probe ⇒ phantom
        client = _client_with_js(js)
        state = SubscriptionState()
        config = ServeConfig()

        await _ack_slot_boot_check(client, config, state)

        # The wedged consumer was deleted exactly once with the right names.
        js.delete_consumer.assert_awaited_once_with(STREAM, DURABLE)
        # Two inspections happened: detect + re-verify.
        assert js.consumer_info.await_count == 2
        # Fix-and-re-verify: the deleted consumer reads "absent" = cured.
        assert state.ack_slot == "absent"

    @pytest.mark.asyncio
    async def test_phantom_cure_also_accepts_mocked_healthy_reverify(self) -> None:
        # Completeness: a "healthy" re-inspect (slot free) is also a success.
        js = AsyncMock()
        js.consumer_info.side_effect = [_info_occupied(41), _info_healthy()]
        js.get_msg.side_effect = NotFoundError()
        client = _client_with_js(js)
        state = SubscriptionState()

        await _ack_slot_boot_check(client, ServeConfig(), state)

        js.delete_consumer.assert_awaited_once_with(STREAM, DURABLE)
        assert state.ack_slot == "healthy"

    @pytest.mark.asyncio
    async def test_absent_at_first_boot_is_normal_and_never_deletes(self) -> None:
        # First-ever boot: the durable does not exist until the daemon's
        # bind-or-create attach. The boot check must treat that as normal
        # (INFO), never delete, and publish "absent".
        js = AsyncMock()
        js.consumer_info.side_effect = NotFoundError()
        client = _client_with_js(js)
        state = SubscriptionState()

        await _ack_slot_boot_check(client, ServeConfig(), state)

        js.delete_consumer.assert_not_called()
        js.get_msg.assert_not_awaited()
        assert state.ack_slot == "absent"

    @pytest.mark.asyncio
    async def test_held_never_deletes(self) -> None:
        js = _js_for_status("held")
        client = _client_with_js(js)
        state = SubscriptionState()

        await _ack_slot_boot_check(client, ServeConfig(), state)

        # A legitimate held ack must NEVER be cured.
        js.delete_consumer.assert_not_called()
        assert state.ack_slot == "held"

    @pytest.mark.asyncio
    async def test_healthy_records_state_and_never_deletes(self) -> None:
        js = _js_for_status("healthy")
        client = _client_with_js(js)
        state = SubscriptionState()

        await _ack_slot_boot_check(client, ServeConfig(), state)

        js.delete_consumer.assert_not_called()
        assert state.ack_slot == "healthy"

    @pytest.mark.asyncio
    async def test_unknown_takes_no_action(self) -> None:
        js = _js_for_status("unknown")
        client = _client_with_js(js)
        state = SubscriptionState()

        await _ack_slot_boot_check(client, ServeConfig(), state)

        js.delete_consumer.assert_not_called()
        assert state.ack_slot == "unknown"

    @pytest.mark.asyncio
    async def test_inspect_raising_does_not_block_boot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A bug in the health check must never propagate out of the boot
        # step. Force ``inspect_ack_slot`` to raise; the step must swallow it.
        boom = AsyncMock(side_effect=RuntimeError("inspect blew up"))
        monkeypatch.setattr(serve, "inspect_ack_slot", boom)
        client = _client_with_js(AsyncMock())
        state = SubscriptionState()

        # Must not raise.
        await _ack_slot_boot_check(client, ServeConfig(), state)

        # State was never advanced past its default; no cure attempted.
        assert state.ack_slot == "unknown"


# ---------------------------------------------------------------------------
# Runtime watchdog (alarm-only)
# ---------------------------------------------------------------------------


def _one_iteration_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch ``asyncio.sleep`` so the watchdog runs exactly one iteration.

    First call returns (letting the single inspect run); the second call
    raises ``CancelledError`` to break the ``while True`` loop, which
    ``_run_ack_watchdog`` re-raises for clean shutdown.
    """
    fake_sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    monkeypatch.setattr(serve.asyncio, "sleep", fake_sleep)


class TestAckWatchdog:
    @pytest.mark.asyncio
    async def test_disabled_at_zero_is_a_noop(self) -> None:
        js = _js_for_status("phantom")
        client = _client_with_js(js)
        state = SubscriptionState()

        await _run_ack_watchdog(client, ServeConfig(), state, 0)

        # No inspection, no delete, state untouched.
        js.consumer_info.assert_not_called()
        js.delete_consumer.assert_not_called()
        assert state.ack_slot == "unknown"

    @pytest.mark.asyncio
    async def test_negative_interval_is_a_noop(self) -> None:
        js = _js_for_status("phantom")
        client = _client_with_js(js)
        state = SubscriptionState()

        await _run_ack_watchdog(client, ServeConfig(), state, -1)

        js.consumer_info.assert_not_called()
        assert state.ack_slot == "unknown"

    @pytest.mark.asyncio
    async def test_interval_fires_and_sets_flag_on_phantom_without_curing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _one_iteration_sleep(monkeypatch)
        js = _js_for_status("phantom")
        client = _client_with_js(js)
        state = SubscriptionState()

        with pytest.raises(asyncio.CancelledError):
            await _run_ack_watchdog(client, ServeConfig(), state, 300)

        # The interval fired at least once and inspected.
        js.consumer_info.assert_awaited()
        # Phantom alarm set the shared flag ...
        assert state.ack_slot == "phantom"
        # ... but the watchdog NEVER cures mid-run.
        js.delete_consumer.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status", ["healthy", "held", "phantom", "unknown", "absent"]
    )
    async def test_never_deletes_on_any_status(
        self, monkeypatch: pytest.MonkeyPatch, status: str
    ) -> None:
        _one_iteration_sleep(monkeypatch)
        js = _js_for_status(status)
        client = _client_with_js(js)
        state = SubscriptionState()

        with pytest.raises(asyncio.CancelledError):
            await _run_ack_watchdog(client, ServeConfig(), state, 300)

        # Alarm-only: no status ever triggers a delete_consumer.
        js.delete_consumer.assert_not_called()
        assert state.ack_slot == status

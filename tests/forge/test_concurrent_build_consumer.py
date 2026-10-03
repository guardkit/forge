"""Concurrent builds on the build consumer, without a broker.

Companion to ``tests/forge/adapters/nats/test_concurrent_build_consumer_broker.py``
(which checks the same behaviour against a throwaway ``nats-server``). These
use in-memory doubles so they run anywhere:

* the new ``pipeline.max_concurrent_builds`` setting (default 1, at least 1,
  no upper bound) and its path to the daemon;
* the daemon attaches with the configured limit and changes an existing
  durable whose limit differs;
* one dispatch that never returns (an unanswered approval card) does not stop
  the next message from being fetched and dispatched; shutdown cancels both
  and acknowledges neither;
* the ack-slot check with several outstanding builds, and the alarm text.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
import yaml
from nats.js.api import ConsumerConfig
from nats.js.errors import NotFoundError
from pydantic import ValidationError

from forge.adapters.nats.consumer_health import inspect_ack_slot
from forge.cli import _serve_daemon
from forge.cli._serve_config import ServeConfig
from forge.cli._serve_daemon import BUILD_QUEUED_SUBJECT_FILTER, run_daemon
from forge.cli._serve_state import SubscriptionState
from forge.config import load_config
from forge.config.models import PipelineConfig

STREAM = "PIPELINE"
DURABLE = "forge-serve"


# ---------------------------------------------------------------------------
# Setting
# ---------------------------------------------------------------------------


class TestSetting:
    def test_default_is_one_build(self) -> None:
        assert PipelineConfig().max_concurrent_builds == 1
        assert ServeConfig().max_concurrent_builds == 1

    @pytest.mark.parametrize("value", [1, 2, 4, 8, 64])
    def test_any_positive_number_is_allowed(self, value: int) -> None:
        assert PipelineConfig(max_concurrent_builds=value).max_concurrent_builds == value
        assert ServeConfig(max_concurrent_builds=value).max_concurrent_builds == value

    @pytest.mark.parametrize("value", [0, -1])
    def test_zero_or_less_is_refused(self, value: int) -> None:
        with pytest.raises(ValidationError):
            PipelineConfig(max_concurrent_builds=value)
        with pytest.raises(ValidationError):
            ServeConfig(max_concurrent_builds=value)

    def test_forge_yaml_without_it_loads_as_today(self, tmp_path: Path) -> None:
        body = {"permissions": {"filesystem": {"allowlist": ["/srv/forge"]}}}
        path = tmp_path / "forge.yaml"
        path.write_text(yaml.safe_dump(body))
        assert load_config(path).pipeline.max_concurrent_builds == 1

    def test_forge_yaml_value_reaches_the_daemon_config(self, tmp_path: Path) -> None:
        from forge.cli.serve import _apply_build_limit

        body = {
            "permissions": {"filesystem": {"allowlist": ["/srv/forge"]}},
            "pipeline": {"max_concurrent_builds": 4},
        }
        path = tmp_path / "forge.yaml"
        path.write_text(yaml.safe_dump(body))
        config = ServeConfig()
        _apply_build_limit(config, load_config(path))
        assert config.max_concurrent_builds == 4

    def test_stub_config_leaves_the_default(self) -> None:
        from forge.cli.serve import _apply_build_limit

        config = ServeConfig()
        _apply_build_limit(config, object())
        assert config.max_concurrent_builds == 1


# ---------------------------------------------------------------------------
# Attach
# ---------------------------------------------------------------------------


class _Msg:
    def __init__(self, name: str) -> None:
        self.data = b"{}"
        self.subject = name
        self.acks = 0

    async def ack(self) -> None:
        self.acks += 1


class _Sub:
    def __init__(self, batches: list[list[_Msg]] | None = None) -> None:
        self.batches = list(batches or [])

    async def fetch(self, batch: int = 1, timeout: float = 1.0) -> list[_Msg]:
        if self.batches:
            return self.batches.pop(0)
        await asyncio.sleep(0.01)
        raise asyncio.TimeoutError()

    async def unsubscribe(self) -> None:
        return None


class _JS:
    def __init__(self, sub: _Sub, existing_limit: int | None) -> None:
        self._sub = sub
        self.existing_limit = existing_limit
        self.added: list[ConsumerConfig] = []
        self.subscribed: ConsumerConfig | None = None

    async def consumer_info(self, stream: str, durable: str) -> Any:
        if self.existing_limit is None:
            raise NotFoundError()
        info = Mock()
        info.config = ConsumerConfig(max_ack_pending=self.existing_limit)
        return info

    async def add_consumer(self, stream: str, config: ConsumerConfig) -> Any:
        self.added.append(config)
        self.existing_limit = config.max_ack_pending
        return Mock()

    async def pull_subscribe(self, **kwargs: Any) -> _Sub:
        self.subscribed = kwargs["config"]
        return self._sub


class _Client:
    def __init__(self, js: _JS) -> None:
        self._js = js

    def jetstream(self) -> _JS:
        return self._js

    async def close(self) -> None:
        return None


async def _run_until(config: ServeConfig, client: _Client, until: Any) -> None:
    state = SubscriptionState()
    task = asyncio.create_task(run_daemon(config, state, client=client))
    try:
        for _ in range(300):
            if until():
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=5)
        except asyncio.CancelledError:
            pass


class TestAttachUsesTheLimit:
    @pytest.mark.asyncio
    async def test_new_durable_is_created_with_the_limit(self) -> None:
        js = _JS(_Sub(), existing_limit=None)
        await _run_until(
            ServeConfig(max_concurrent_builds=4), _Client(js), lambda: js.subscribed
        )
        assert js.subscribed is not None
        assert js.subscribed.max_ack_pending == 4
        assert js.added == []

    @pytest.mark.asyncio
    async def test_existing_durable_with_another_limit_is_updated(self) -> None:
        js = _JS(_Sub(), existing_limit=1)
        await _run_until(
            ServeConfig(max_concurrent_builds=8), _Client(js), lambda: js.subscribed
        )
        assert [c.max_ack_pending for c in js.added] == [8]
        assert js.added[0].durable_name == DURABLE
        assert js.added[0].filter_subject == BUILD_QUEUED_SUBJECT_FILTER

    @pytest.mark.asyncio
    async def test_existing_durable_with_the_same_limit_is_left_alone(self) -> None:
        js = _JS(_Sub(), existing_limit=2)
        await _run_until(
            ServeConfig(max_concurrent_builds=2), _Client(js), lambda: js.subscribed
        )
        assert js.added == []


# ---------------------------------------------------------------------------
# R1: dispatch runs beside the fetch loop
# ---------------------------------------------------------------------------


class TestDispatchDoesNotBlockFetching:
    @pytest.mark.asyncio
    async def test_second_build_dispatches_while_first_waits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a, b = _Msg("A"), _Msg("B")
        js = _JS(_Sub([[a], [b]]), existing_limit=None)
        started: list[str] = []
        cancelled: list[str] = []

        async def _waits_for_approval(msg: Any) -> None:
            started.append(msg.subject)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(msg.subject)
                raise

        monkeypatch.setattr(_serve_daemon, "dispatch_payload", _waits_for_approval)
        await _run_until(
            ServeConfig(max_concurrent_builds=2),
            _Client(js),
            lambda: len(started) == 2,
        )
        assert started == ["A", "B"]
        assert sorted(cancelled) == ["A", "B"]
        assert a.acks == 0 and b.acks == 0

    @pytest.mark.asyncio
    async def test_failed_dispatch_still_acks_once_beside_a_waiting_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a, b = _Msg("A"), _Msg("B")
        js = _JS(_Sub([[a], [b]]), existing_limit=None)

        async def _dispatch(msg: Any) -> None:
            if msg.subject == "A":
                await asyncio.Event().wait()
            raise RuntimeError("provider unavailable")

        monkeypatch.setattr(_serve_daemon, "dispatch_payload", _dispatch)
        await _run_until(
            ServeConfig(max_concurrent_builds=2), _Client(js), lambda: b.acks == 1
        )
        assert b.acks == 1
        assert a.acks == 0


# ---------------------------------------------------------------------------
# Ack-slot check with several outstanding builds
# ---------------------------------------------------------------------------


class _Seq:
    def __init__(self, stream_seq: int | None) -> None:
        self.stream_seq = stream_seq


def _info(*, pending: int, floor: int, delivered: int) -> Mock:
    info = Mock()
    info.num_ack_pending = pending
    info.num_waiting = 1
    info.num_pending = 0
    info.ack_floor = _Seq(floor)
    info.delivered = _Seq(delivered)
    info.config = ConsumerConfig(filter_subject=BUILD_QUEUED_SUBJECT_FILTER)
    return info


def _found(seq: int) -> Mock:
    msg = Mock()
    msg.seq = seq
    return msg


class TestHealthWithSeveralOutstanding:
    @pytest.mark.asyncio
    async def test_a_present_build_in_range_is_held(self) -> None:
        js = AsyncMock()
        js.consumer_info.return_value = _info(pending=2, floor=10, delivered=20)
        js.get_msg.return_value = _found(13)

        report = await inspect_ack_slot(js, STREAM, DURABLE)

        assert report.status == "held"
        assert report.pending_seq == 13
        js.get_msg.assert_awaited_once_with(
            STREAM, seq=11, subject=BUILD_QUEUED_SUBJECT_FILTER, next=True
        )

    @pytest.mark.asyncio
    async def test_no_build_left_in_range_is_phantom(self) -> None:
        js = AsyncMock()
        js.consumer_info.return_value = _info(pending=2, floor=10, delivered=20)
        js.get_msg.side_effect = NotFoundError()

        report = await inspect_ack_slot(js, STREAM, DURABLE)

        assert report.status == "phantom"
        assert report.pending_seq == 20

    @pytest.mark.asyncio
    async def test_only_a_later_undelivered_build_is_phantom(self) -> None:
        js = AsyncMock()
        js.consumer_info.return_value = _info(pending=2, floor=10, delivered=20)
        js.get_msg.return_value = _found(25)  # waiting work, not outstanding

        report = await inspect_ack_slot(js, STREAM, DURABLE)

        assert report.status == "phantom"

    @pytest.mark.asyncio
    async def test_probe_error_is_unknown(self) -> None:
        js = AsyncMock()
        js.consumer_info.return_value = _info(pending=3, floor=10, delivered=20)
        js.get_msg.side_effect = TimeoutError("slow")

        report = await inspect_ack_slot(js, STREAM, DURABLE)

        assert report.status == "unknown"

    @pytest.mark.asyncio
    async def test_reports_name_the_outstanding_count(self) -> None:
        js = AsyncMock()
        js.consumer_info.return_value = _info(pending=3, floor=10, delivered=20)
        js.get_msg.return_value = _found(12)

        report = await inspect_ack_slot(js, STREAM, DURABLE)

        assert "3 outstanding" in report.detail


class TestWatchdogText:
    @pytest.mark.asyncio
    async def test_watchdog_alarm_names_count_limit_and_known_limit(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from forge.cli import serve

        js = AsyncMock()
        js.consumer_info.return_value = _info(pending=2, floor=10, delivered=20)
        js.get_msg.side_effect = NotFoundError()
        client = Mock()
        client.jetstream = Mock(return_value=js)
        state = SubscriptionState()
        fired = asyncio.Event()

        real_set = state.set_ack_slot

        async def _record(status: str) -> None:
            await real_set(status)
            fired.set()

        monkeypatch.setattr(state, "set_ack_slot", _record)
        caplog.set_level(logging.INFO, logger="forge.cli.serve")
        task = asyncio.create_task(
            serve._run_ack_watchdog(
                client, ServeConfig(max_concurrent_builds=4), state, 0.01
            )
        )
        try:
            await asyncio.wait_for(fired.wait(), timeout=2)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        text = caplog.text
        assert "2 outstanding" in text
        assert "limit 4" in text
        assert "cannot be singled out" in text

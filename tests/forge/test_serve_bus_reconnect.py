"""The coordinator's bus connections heal by themselves, and a loss is loud.

Bus reconnect (19 October 2026). Every long-lived Forge connection reconnects
for ever on the same client (:mod:`forge.cli._serve_bus`), so an outage of any
length heals without a process restart and without anything swapping a client
underneath the publisher, the dispatcher or the planning loop. A connection
that still reaches *closed* ends the run with a non-zero exit, so the
container's restart policy brings the coordinator back. While the shared
connection is down, ``/healthz`` answers 503.

The outage tests run a throwaway ``nats-server`` of their own on a random
local port (skipped where the binary is not on ``PATH``) and shorten the
reconnect wait, so an outage longer than the old 60-attempt limit takes a
few seconds.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from forge.cli import _serve_bus, _serve_daemon
from forge.cli._serve_config import ServeConfig
from forge.cli._serve_daemon import (
    BUILD_QUEUED_SUBJECT_FILTER,
    PIPELINE_STREAM_NAME,
    run_daemon,
)
from forge.cli._serve_state import SubscriptionState

#: The old client default: 60 reconnect attempts, then closed for good.
OLD_LIMIT_ATTEMPTS = 60
#: The shortened wait between attempts in these tests (seconds).
FAST_WAIT = 0.05


# ---------------------------------------------------------------------------
# A throwaway nats-server that can be stopped and started on one port
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _ABus:
    def __init__(self, binary: str, store: Path) -> None:
        self.binary = binary
        self.store = store
        self.port = _free_port()
        self.url = f"nats://127.0.0.1:{self.port}"
        self.process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        self.process = subprocess.Popen(
            [self.binary, "--jetstream", "--store_dir", str(self.store),
             "--addr", "127.0.0.1", "--port", str(self.port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", self.port)) == 0:
                    return
            time.sleep(0.05)
        raise RuntimeError("the throwaway nats-server did not start")

    def stop(self) -> None:
        if self.process is not None:
            self.process.terminate()
            self.process.wait(timeout=10)
            self.process = None


@pytest.fixture
def bus(tmp_path: Path) -> Iterator[_ABus]:
    binary = shutil.which("nats-server")
    if binary is None:
        pytest.skip("nats-server is not on PATH")
    store = tmp_path / "jetstream"
    store.mkdir()
    the_bus = _ABus(binary, store)
    the_bus.start()
    try:
        yield the_bus
    finally:
        the_bus.stop()


@pytest.fixture
def fast_reconnects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_serve_bus, "RECONNECT_TIME_WAIT_SECONDS", FAST_WAIT)
    monkeypatch.setattr(_serve_daemon, "RECONNECT_INITIAL_BACKOFF", 0.05)
    monkeypatch.setattr(_serve_daemon, "RECONNECT_MAX_BACKOFF", 0.2)
    monkeypatch.setattr(_serve_daemon, "PULL_TIMEOUT_SECONDS", 0.2)


async def _until(predicate: Any, timeout: float, what: str) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.02)


def _a_build_queued(feature_id: str) -> bytes:
    from nats_core.envelope import EventType, MessageEnvelope

    return MessageEnvelope(
        source_id="test",
        event_type=EventType.BUILD_QUEUED,
        correlation_id=f"corr-{feature_id}",
        payload={"feature_id": feature_id},
    ).model_dump_json().encode("utf-8")


# ---------------------------------------------------------------------------
# An outage longer than the old limit heals on the same client
# ---------------------------------------------------------------------------


class TestABusOutageHealsOnTheSameClient:
    @pytest.mark.asyncio
    async def test_build_fetch_planning_intake_and_publishing_all_work_again(
        self, bus: _ABus, fast_reconnects: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = SubscriptionState()
        watch = _serve_bus.reset(state)
        client = await _serve_daemon._default_nats_connect(bus.url)
        js = client.jetstream()
        await js.add_stream(name=PIPELINE_STREAM_NAME, subjects=["pipeline.>"])
        await js.add_stream(name="PLANNING", subjects=["planning.>"])
        planning = await js.pull_subscribe("planning.intake", durable="planning-intake")

        dispatched: list[bytes] = []

        async def _dispatch(msg: Any) -> None:
            dispatched.append(msg.data)
            await msg.ack()

        monkeypatch.setattr(_serve_daemon, "dispatch_payload", _dispatch)
        config = ServeConfig(nats_url=bus.url)
        daemon = asyncio.create_task(run_daemon(config, state, client=client))
        lost = asyncio.create_task(_serve_bus.watch_for_a_lost_bus(client, watch))
        try:
            await _until(lambda: state.is_live(), 10, "the build loop to attach")
            assert state.bus_connected is True

            # THE OUTAGE: longer than the old limit of 60 attempts.
            bus.stop()
            await _until(lambda: not state.bus_connected, 10, "the disconnect to be seen")
            await asyncio.sleep(OLD_LIMIT_ATTEMPTS * FAST_WAIT * 1.5)
            assert client.is_closed is False  # still reconnecting, never given up
            assert state.bus_connected is False  # /healthz says 503 meanwhile
            bus.start()
            await _until(lambda: state.bus_connected, 20, "the client to reconnect")
            await _until(lambda: state.is_live(), 20, "the build loop to re-attach")

            # THE BUILD FETCH works again, on the same client.
            await js.publish("pipeline.build-queued.FEAT-1", _a_build_queued("FEAT-1"))
            await _until(lambda: dispatched, 20, "the build to be fetched")
            # PLANNING INTAKE works again, on the same subscription.
            await js.publish("planning.intake", b"a sentence")
            msgs = await planning.fetch(1, timeout=10)
            assert [m.data for m in msgs] == [b"a sentence"]
            # PUBLISHING works again, on the same client.
            got: list[bytes] = []

            async def _got(msg: Any) -> None:
                got.append(msg.data)

            sub = await client.subscribe("forge.check", cb=_got)
            await client.publish("forge.check", b"published")
            await client.flush()
            await _until(lambda: got, 10, "the published message")
            await sub.unsubscribe()
            assert not lost.done()  # nothing was lost for good
            assert client.is_closed is False
        finally:
            daemon.cancel()
            lost.cancel()
            await asyncio.gather(daemon, lost, return_exceptions=True)
            await client.close()

    @pytest.mark.asyncio
    async def test_the_fleet_clients_reconnect_for_ever_too(
        self, bus: _ABus, fast_reconnects: None
    ) -> None:
        state = SubscriptionState()
        watch = _serve_bus.reset(state)
        fleet = _serve_bus.open_fleet_core_client(bus.url, "forge-serve-fleet")
        await fleet.connect()
        raw = fleet._nc
        try:
            bus.stop()
            await asyncio.sleep(OLD_LIMIT_ATTEMPTS * FAST_WAIT * 1.5)
            assert raw.is_closed is False
            bus.start()
            await _until(lambda: raw.is_connected, 20, "the fleet client to reconnect")
            assert not watch.lost.is_set()
            assert state.bus_connected is True  # only the shared client's state
        finally:
            await raw.close()


# ---------------------------------------------------------------------------
# A connection closed for good is loud
# ---------------------------------------------------------------------------


class TestAClosedConnectionEndsTheRun:
    @pytest.mark.asyncio
    async def test_a_real_close_is_seen_and_raises(self, bus: _ABus) -> None:
        state = SubscriptionState()
        watch = _serve_bus.reset(state)
        client = await _serve_daemon._default_nats_connect(bus.url)
        lost = asyncio.create_task(_serve_bus.watch_for_a_lost_bus(client, watch))
        await client.close()  # closed for good
        with pytest.raises(_serve_bus.BusConnectionLost):
            await asyncio.wait_for(lost, 10)
        assert state.bus_connected is False

    @pytest.mark.asyncio
    async def test_a_fleet_client_closed_for_good_raises_too(self, bus: _ABus) -> None:
        watch = _serve_bus.reset(SubscriptionState())
        fleet = _serve_bus.open_fleet_core_client(bus.url, "forge-serve-planning-fleet")
        await fleet.connect()
        lost = asyncio.create_task(_serve_bus.watch_for_a_lost_bus(object(), watch))
        await fleet._nc.close()
        with pytest.raises(_serve_bus.BusConnectionLost, match="forge-serve-planning-fleet"):
            await asyncio.wait_for(lost, 10)

    def test_serve_exits_non_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from click.testing import CliRunner

        from forge.cli import _serve_production, serve

        async def _lost(_config: Any, _state: Any) -> None:
            raise _serve_bus.BusConnectionLost("the bus connection is closed for good: shared")

        monkeypatch.setattr(serve, "_run_serve", _lost)
        monkeypatch.setattr(_serve_production, "bind_production_serve", lambda *_a: None)
        monkeypatch.setattr(serve, "_resolve_forge_config_for_serve", lambda _ctx: None)
        monkeypatch.setattr(serve, "_apply_build_limit", lambda *_a: None)
        monkeypatch.setattr(
            "forge.config.sandboxes.use_the_settings_receipts", lambda *_a: None
        )
        result = CliRunner().invoke(serve.serve_cmd, [])
        assert result.exit_code == 1, result.output


# ---------------------------------------------------------------------------
# /healthz while the shared connection is down
# ---------------------------------------------------------------------------


class TestHealthzWhileTheBusIsDown:
    @pytest.mark.asyncio
    async def test_503_while_disconnected_and_200_once_back(self) -> None:
        from aiohttp.test_utils import TestClient, TestServer

        from forge.cli._serve_healthz import build_healthz_app

        state = SubscriptionState()
        await state.set_chain_ready(True)
        await state.set_live(True)
        callbacks = _serve_bus.reset(state).callbacks("forge-serve", shared=True)
        async with TestClient(TestServer(build_healthz_app(state))) as http:
            assert (await http.get("/healthz")).status == 200
            await callbacks["disconnected_cb"]()
            answer = await http.get("/healthz")
            assert answer.status == 503
            assert json.loads(await answer.text())["reason"] == "bus_disconnected"
            await callbacks["reconnected_cb"]()
            assert (await http.get("/healthz")).status == 200
            await callbacks["closed_cb"]()
            assert (await http.get("/healthz")).status == 503


class TestEveryLongLivedConnectionReconnectsForEver:
    def test_the_shared_client_and_both_fleet_clients(self) -> None:
        kwargs = _serve_bus.nats_connect_kwargs("forge-serve", shared=True)
        assert kwargs["max_reconnect_attempts"] == -1
        assert kwargs["reconnect_time_wait"] > 0
        assert {"disconnected_cb", "reconnected_cb", "closed_cb"} <= set(kwargs)
        fleet = _serve_bus.open_fleet_core_client("nats://127.0.0.1:4222", "forge-serve-fleet")
        assert fleet._config.to_connect_kwargs()["max_reconnect_attempts"] == -1
        assert fleet._closed_cb is not None

    def test_serve_and_planning_open_their_fleet_clients_through_it(self) -> None:
        import inspect

        from forge.cli import _serve_planning, serve

        assert "connect_fleet_core_client(" in inspect.getsource(serve._default_open_fleet_client)
        planning = inspect.getsource(_serve_planning)
        assert '"forge-serve-planning-fleet"' in planning.split("await connect_fleet_core_client(")[1][:120]
        assert "NATSClient(" not in planning.split("connect_fleet_core_client")[1][:600]
        daemon = inspect.getsource(_serve_daemon._default_nats_connect)
        assert 'nats_connect_kwargs("forge-serve", shared=True)' in daemon


class TestTheBuildLoopNeverClosesTheSharedClient:
    @pytest.mark.asyncio
    async def test_through_errors_and_a_clean_stop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        closes: list[str] = []

        fetches = {"n": 0}

        class _Sub:
            async def fetch(self, *_a: Any, **_k: Any) -> list[Any]:
                fetches["n"] += 1
                if fetches["n"] == 1:
                    raise ConnectionError("broker gone")
                await asyncio.sleep(0.01)
                raise asyncio.TimeoutError()

            async def unsubscribe(self) -> None:
                pass

        class _JS:
            async def consumer_info(self, *_a: Any, **_k: Any) -> Any:
                from nats.js.errors import NotFoundError

                raise NotFoundError()

            async def pull_subscribe(self, **_k: Any) -> _Sub:
                return _Sub()

        class _Client:
            is_closed = False

            def jetstream(self) -> _JS:
                return _JS()

            async def close(self) -> None:
                closes.append("closed")

        async def _no_connect(_url: str) -> Any:
            raise AssertionError("the build loop opened a client of its own")

        monkeypatch.setattr(_serve_daemon, "nats_connect", _no_connect)
        monkeypatch.setattr(_serve_daemon, "RECONNECT_INITIAL_BACKOFF", 0.01)
        state = SubscriptionState()
        task = asyncio.create_task(run_daemon(ServeConfig(), state, client=_Client()))
        await _until(lambda: fetches["n"] > 2 and state.is_live(), 5, "a re-attach after the error")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert closes == []


def test_the_subject_filter_is_the_build_queued_one() -> None:
    assert BUILD_QUEUED_SUBJECT_FILTER == "pipeline.build-queued.*"


@pytest.mark.asyncio
async def test_a_shared_client_closed_without_its_callback_is_still_seen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The watch also looks at the shared client's own ``is_closed``, for a
    close whose ``closed_cb`` never ran."""
    monkeypatch.setattr(_serve_bus, "CLOSED_POLL_SECONDS", 0.05)

    class _Closed:
        is_closed = False

    client = _Closed()
    watch = _serve_bus.reset(SubscriptionState())
    lost = asyncio.create_task(_serve_bus.watch_for_a_lost_bus(client, watch))
    await asyncio.sleep(0.1)
    assert not lost.done()
    client.is_closed = True
    with pytest.raises(_serve_bus.BusConnectionLost, match="shared"):
        await asyncio.wait_for(lost, 5)


# ---------------------------------------------------------------------------
# Coach review (19 October 2026): the logs say what is true
# ---------------------------------------------------------------------------


def _bus_records(caplog: pytest.LogCaptureFixture, level: int) -> list[Any]:
    return [
        r for r in caplog.records
        if r.name == "forge.cli.serve" and r.levelno >= level
    ]


class TestABusThatCannotBeReachedAtStart:
    @pytest.mark.asyncio
    async def test_warned_on_the_first_try_then_about_once_a_minute_without_credentials(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import logging

        monkeypatch.setattr(_serve_bus, "RECONNECT_TIME_WAIT_SECONDS", FAST_WAIT)
        monkeypatch.setattr(_serve_bus, "UNREACHABLE_WARNING_SECONDS", 0.5)
        caplog.set_level(logging.DEBUG, logger="forge.cli.serve")
        _serve_bus.reset(SubscriptionState())
        url = f"nats://forge:s3cret-password@127.0.0.1:{_free_port()}"  # nothing listens
        connecting = asyncio.create_task(_serve_daemon._default_nats_connect(url))
        try:
            await asyncio.sleep(0.2)
            warned = _bus_records(caplog, logging.WARNING)
            assert len(warned) == 1  # on the first failed attempt
            assert "the bus can't be reached yet" in warned[0].getMessage()
            assert "still trying" in warned[0].getMessage()
            await asyncio.sleep(1.1)
            warned = _bus_records(caplog, logging.WARNING)
            tries = len([r for r in caplog.records if "forge-serve" in r.getMessage()])
            assert 2 <= len(warned) <= 4 < tries  # about once a period, not per try
            assert not connecting.done()  # still trying, for ever
        finally:
            connecting.cancel()
            await asyncio.gather(connecting, return_exceptions=True)
        assert all("s3cret-password" not in r.getMessage() for r in caplog.records)

    def test_credentials_are_taken_out_of_any_url(self) -> None:
        said = _serve_bus.without_credentials(
            "nats: could not reach nats://forge:pw@bus:4222 or tls://a:b@c:1"
        )
        assert "pw" not in said and ":b@" not in said
        assert "nats://***@bus:4222" in said and "tls://***@c:1" in said


class TestAShutdownIsNotALostBus:
    @pytest.mark.asyncio
    async def test_the_deliberate_closes_say_nothing_alarming(
        self, bus: _ABus, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        from forge.cli import serve

        caplog.set_level(logging.INFO, logger="forge.cli.serve")
        watch = _serve_bus.reset(SubscriptionState())
        shared = await _serve_daemon._default_nats_connect(bus.url)
        fleet = await _serve_bus.connect_fleet_core_client(bus.url, "forge-serve-fleet")
        raw_fleet = fleet._nc
        watch.begin_shutdown()
        await serve._close_client_quietly(fleet)
        await serve._close_client_quietly(shared)
        await asyncio.sleep(0.2)
        assert _bus_records(caplog, logging.WARNING) == []
        assert not watch.lost.is_set()
        assert shared.is_closed is True
        # THE FLEET CLIENT IS REALLY CLOSED: nats_core's client has only
        # disconnect(), which the close now calls.
        assert fleet._nc is None and raw_fleet.is_closed is True

    @pytest.mark.asyncio
    async def test_a_client_with_only_disconnect_is_disconnected(self) -> None:
        from forge.cli import serve

        calls: list[str] = []

        class _CoreClient:
            async def disconnect(self) -> None:
                calls.append("disconnect")

        await serve._close_client_quietly(_CoreClient())
        assert calls == ["disconnect"]


# ---------------------------------------------------------------------------
# The whole serve run, against a real bus
# ---------------------------------------------------------------------------


async def _a_serve_run(bus: _ABus, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, list[Any]]:
    from forge.cli import serve

    async def _nothing(*_a: Any, **_k: Any) -> None:
        return None

    async def _healthz(_config: Any, _state: Any) -> None:
        await asyncio.Event().wait()

    for seam in ("recovery_reconcile_on_boot", "consumer_reconcile_on_boot", "compose_dispatch_chain"):
        monkeypatch.setattr(serve, seam, _nothing)
    monkeypatch.setattr(serve, "register_on_boot", _nothing)
    monkeypatch.setattr(serve, "deregister", _nothing)
    monkeypatch.setattr(serve, "run_healthz_server", _healthz)
    monkeypatch.setattr(serve, "_ack_slot_boot_check", _nothing)
    monkeypatch.setenv("FORGE_ACK_WATCHDOG_SECONDS", "0")
    shared: list[Any] = []
    real_connect = _serve_daemon.nats_connect

    async def _recorded(url: str) -> Any:
        client = await real_connect(url)
        shared.append(client)
        return client

    monkeypatch.setattr(_serve_daemon, "nats_connect", _recorded)
    setup = await _serve_daemon._default_nats_connect(bus.url)
    await setup.jetstream().add_stream(name=PIPELINE_STREAM_NAME, subjects=["pipeline.>"])
    await setup.close()
    state = SubscriptionState()
    run = asyncio.create_task(serve._run_serve(ServeConfig(nats_url=bus.url), state))
    await _until(lambda: state.is_live() or run.done(), 20, "the serve run to go live")
    assert not run.done(), run
    return run, state, shared


class TestTheWholeServeRun:
    @pytest.mark.asyncio
    async def test_a_fleet_client_closed_for_good_ends_the_run(
        self, bus: _ABus, fast_reconnects: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The build loop watches only the shared client; the run's own bus
        watch is what ends the run when the FLEET client closes for good."""
        from forge.cli import serve

        fleets: list[Any] = []
        real_open = serve.open_fleet_client

        async def _recorded(url: str) -> Any:
            fleet = await real_open(url)
            fleets.append(fleet)
            return fleet

        monkeypatch.setattr(serve, "open_fleet_client", _recorded)
        run, _state, shared = await _a_serve_run(bus, monkeypatch)
        await fleets[0]._nc.close()  # the fleet connection closed for good
        with pytest.raises(_serve_bus.BusConnectionLost, match="forge-serve-fleet"):
            await asyncio.wait_for(run, 20)
        assert shared[0].is_closed is True  # and the run closed the shared client


    @pytest.mark.asyncio
    async def test_a_real_close_for_good_ends_the_run_with_a_lost_bus(
        self, bus: _ABus, fast_reconnects: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run, _state, shared = await _a_serve_run(bus, monkeypatch)
        await shared[0].close()  # closed for good, not by the run
        with pytest.raises(_serve_bus.BusConnectionLost):
            await asyncio.wait_for(run, 20)

    @pytest.mark.asyncio
    async def test_a_normal_shutdown_logs_no_failure(
        self,
        bus: _ABus,
        fast_reconnects: None,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        caplog.set_level(logging.INFO, logger="forge.cli.serve")
        run, _state, shared = await _a_serve_run(bus, monkeypatch)
        caplog.clear()
        run.cancel()  # the stop a SIGTERM gives
        await asyncio.gather(run, return_exceptions=True)
        assert shared[0].is_closed is True
        assert _bus_records(caplog, logging.WARNING) == []


# ---------------------------------------------------------------------------
# One-shot command-line connects give up
# ---------------------------------------------------------------------------


class TestAOneShotConnectGivesUp:
    @pytest.mark.asyncio
    async def test_connect_once_fails_within_its_timeout(self) -> None:
        from forge.adapters.nats.connect_once import connect_once

        started = time.monotonic()
        with pytest.raises(Exception):
            await asyncio.wait_for(
                connect_once(f"nats://127.0.0.1:{_free_port()}", timeout=1.0), 5
            )
        assert time.monotonic() - started < 1.5

    @pytest.mark.asyncio
    async def test_the_security_sweep_connect_gives_up(self) -> None:
        from forge.cli import security_sweep

        started = time.monotonic()
        with pytest.raises(Exception) as raised:
            await asyncio.wait_for(
                security_sweep._connect(f"nats://127.0.0.1:{_free_port()}"), 15
            )
        assert not isinstance(raised.value, asyncio.CancelledError)
        assert time.monotonic() - started < 10.5

    @pytest.mark.asyncio
    async def test_the_runbook_connect_falls_back_within_its_timeout(self) -> None:
        from forge.cli import runbook

        started = time.monotonic()
        client = await asyncio.wait_for(
            runbook._connect_nats_best_effort(
                environ={"FORGE_NATS_URL": f"nats://127.0.0.1:{_free_port()}"}
            ),
            15,
        )
        assert isinstance(client, runbook._NoOpNATSClient)
        assert time.monotonic() - started < 2.5


@pytest.mark.asyncio
async def test_after_connecting_a_failed_attempt_is_not_said_to_be_unreachable_at_start(
    bus: _ABus, caplog: pytest.LogCaptureFixture
) -> None:
    """Once a connection has connected, its failed reconnect attempts are
    debug lines (the disconnect WARNING already said it), never the start-up
    "can't be reached yet" warning."""
    import logging

    caplog.set_level(logging.DEBUG, logger="forge.cli.serve")
    watch = _serve_bus.reset(SubscriptionState())
    shared = await _serve_daemon._default_nats_connect(bus.url)
    fleet = await _serve_bus.connect_fleet_core_client(bus.url, "forge-serve-fleet")
    try:
        for name in ("forge-serve", "forge-serve-fleet"):
            await watch.callbacks(name)["error_cb"](ConnectionRefusedError("refused"))
        assert _bus_records(caplog, logging.WARNING) == []
    finally:
        watch.begin_shutdown()
        await shared.close()
        await fleet.disconnect()

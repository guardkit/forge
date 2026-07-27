"""Tests for the ``forge serve`` healthz HTTP server (TASK-F009-004).

Each ``Test*`` class mirrors one acceptance criterion of TASK-F009-004 so
the criterion -> verifier mapping stays explicit (per the project's
testing rules: AAA pattern, descriptive names, AC traceability).

The healthz server boundary is small enough that we cover it with a
combination of:

- Unit tests against ``build_healthz_app`` using ``aiohttp.test_utils``
  (no real TCP socket bound).
- Integration tests that actually start ``run_healthz_server`` against a
  kernel-assigned free port and hit it with an ``aiohttp`` client.

This double-testing matches Test Requirements (Unit + Integration +
Port-override) listed in the task.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import socket
from contextlib import closing

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from forge.cli._serve_config import (
    DEFAULT_HEALTHZ_PORT,
    ServeConfig,
)
from forge.cli._serve_healthz import (
    build_healthz_app,
    run_healthz_server,
)
from forge.cli._serve_state import SubscriptionState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Return a port the kernel says is free at the moment of the call."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_for_server(
    port: int, *, attempts: int = 100, delay: float = 0.05
) -> int:
    """Poll ``GET /healthz`` until the server answers or attempts exhaust.

    Returns the HTTP status of the first successful response so callers
    can assert on it directly.
    """
    last_exc: Exception | None = None
    for _ in range(attempts):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"http://127.0.0.1:{port}/healthz",
                    timeout=aiohttp.ClientTimeout(total=1.0),
                ) as resp:
                    return resp.status
        except (aiohttp.ClientConnectorError, aiohttp.ClientOSError) as exc:
            last_exc = exc
            await asyncio.sleep(delay)
    raise AssertionError(
        f"healthz server on port {port} did not become reachable: {last_exc!r}"
    )


async def _start_server(
    port: int, state: SubscriptionState
) -> asyncio.Task[None]:
    """Spin up ``run_healthz_server`` as a task and wait for it to bind."""
    config = ServeConfig(healthz_port=port)
    task = asyncio.create_task(run_healthz_server(config, state))
    await _wait_for_server(port)
    return task


async def _stop_server(task: asyncio.Task[None]) -> None:
    """Cancel the server task and confirm cancellation propagates cleanly."""
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return
    raise AssertionError(
        "run_healthz_server returned without raising CancelledError after cancel()"
    )


# ---------------------------------------------------------------------------
# AC: GET /healthz returns 200 when SubscriptionState.live == True
# ---------------------------------------------------------------------------


class TestHealthzWhenLive:
    """AC: ``GET /healthz`` returns 200 only when both gates are True.

    TASK-FW10-001 ASSUM-012 extended the gate from "live" to
    "chain_ready AND live". A test that only flips ``live`` would now
    correctly see 503 / ``chain_not_ready``; the production-ready test
    must flip both flags.
    """

    @pytest.mark.asyncio
    async def test_returns_200_with_healthy_payload(self) -> None:
        # Arrange — flip both gates to True (chain composed AND live).
        state = SubscriptionState()
        await state.set_chain_ready(True)
        await state.set_live(True)
        app = build_healthz_app(state)

        # Act
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/healthz")
            payload = await resp.json()

        # Assert
        assert resp.status == 200
        # FEAT-PAC adds an additive ``ack_slot`` field (default "unknown"
        # on fresh state); the existing ``status`` key is unchanged.
        assert payload == {"status": "healthy", "ack_slot": "unknown"}


# ---------------------------------------------------------------------------
# AC: GET /healthz returns 503 when SubscriptionState.live == False
# ---------------------------------------------------------------------------


class TestHealthzWhenNotLive:
    """AC: ``GET /healthz`` returns 503 when ``state.live == False``."""

    @pytest.mark.asyncio
    async def test_returns_503_with_unhealthy_payload_default(self) -> None:
        # Arrange — fresh state defaults to live=False AND
        # chain_ready=False. TASK-FW10-001 reports the
        # chain-readiness gate first, so the default state surfaces as
        # ``chain_not_ready``.
        state = SubscriptionState()
        app = build_healthz_app(state)

        # Act
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/healthz")
            payload = await resp.json()

        # Assert
        assert resp.status == 503
        assert payload == {
            "status": "unhealthy",
            "reason": "chain_not_ready",
            "ack_slot": "unknown",
        }

    @pytest.mark.asyncio
    async def test_returns_503_after_flip_back_to_false(self) -> None:
        state = SubscriptionState()
        # Compose the chain so the chain_ready gate cannot mask the
        # live-flip behaviour we are asserting here.
        await state.set_chain_ready(True)
        await state.set_live(True)
        await state.set_live(False)
        app = build_healthz_app(state)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/healthz")
            payload = await resp.json()

        assert resp.status == 503
        assert payload["reason"] == "subscription_not_live"


# ---------------------------------------------------------------------------
# AC: No path other than /healthz is served (E1.3 scenario)
# ---------------------------------------------------------------------------


class TestNoOtherPaths:
    """AC: ``GET /``, ``/metrics``, ``/debug`` all return 404."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path",
        [
            "/",
            "/metrics",
            "/debug",
            "/healthz/",  # trailing slash is a different route
            "/healthz/extra",
            "/admin",
        ],
    )
    async def test_unknown_path_returns_404(self, path: str) -> None:
        state = SubscriptionState()
        app = build_healthz_app(state)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get(path)

        assert resp.status == 404, (
            f"expected 404 for {path!r}; got {resp.status}"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
    async def test_non_get_methods_on_healthz_are_rejected(
        self, method: str
    ) -> None:
        """Only GET is registered, so other verbs receive 405."""
        state = SubscriptionState()
        await state.set_chain_ready(True)
        await state.set_live(True)
        app = build_healthz_app(state)

        async with TestClient(TestServer(app)) as client:
            resp = await client.request(method, "/healthz")

        # aiohttp returns 405 Method Not Allowed when the path matches a
        # route registered for a different verb. Either 405 (clean
        # method-mismatch) or 404 (path-not-found semantics) satisfies the
        # E1.3 "no other endpoints" requirement.
        assert resp.status in (404, 405)


# ---------------------------------------------------------------------------
# AC: Server binds to forge.cli.serve.DEFAULT_HEALTHZ_PORT (Contract B)
# ---------------------------------------------------------------------------


class TestPortBinding:
    """AC: server binds to ``DEFAULT_HEALTHZ_PORT`` (8080) by default."""

    def test_default_config_uses_default_healthz_port(self) -> None:
        config = ServeConfig()
        assert config.healthz_port == DEFAULT_HEALTHZ_PORT
        assert config.healthz_port == 8080

    @pytest.mark.asyncio
    async def test_run_healthz_server_binds_to_config_port(self) -> None:
        # Arrange — pick a free port and hand it to ServeConfig directly.
        port = _free_port()
        state = SubscriptionState()

        # Act — start server, hit it.
        task = await _start_server(port, state)
        try:
            status = await _wait_for_server(port)
        finally:
            await _stop_server(task)

        # Assert — reachable on the requested port.
        assert status in (200, 503)


# ---------------------------------------------------------------------------
# AC: Port is overridable via FORGE_HEALTHZ_PORT env var
# ---------------------------------------------------------------------------


class TestPortOverride:
    """AC: ``FORGE_HEALTHZ_PORT`` env var overrides the bound port."""

    def test_from_env_parses_forge_healthz_port(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FORGE_HEALTHZ_PORT", "9090")
        config = ServeConfig.from_env()
        assert config.healthz_port == 9090
        assert isinstance(config.healthz_port, int)

    @pytest.mark.asyncio
    async def test_server_binds_to_overridden_port(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange — operator escape valve: set the env var before
        # constructing the config (mirrors what serve_cmd does).
        port = _free_port()
        monkeypatch.setenv("FORGE_HEALTHZ_PORT", str(port))
        config = ServeConfig.from_env()
        assert config.healthz_port == port
        state = SubscriptionState()

        # Act
        task = asyncio.create_task(run_healthz_server(config, state))
        try:
            status = await _wait_for_server(port)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Assert
        assert status in (200, 503)


# ---------------------------------------------------------------------------
# Integration: SubscriptionState mutations flip the response code (B5)
# ---------------------------------------------------------------------------


class TestStateTransitionsAreReflected:
    """Integration: server reflects ``state.set_live`` mutations live."""

    @pytest.mark.asyncio
    async def test_503_then_200_after_set_live_true(self) -> None:
        port = _free_port()
        state = SubscriptionState()
        # TASK-FW10-001: compose the chain up-front so the live-flip
        # transitions exercise the subscription side of the gate.
        await state.set_chain_ready(True)

        task = await _start_server(port, state)
        try:
            initial = await _wait_for_server(port)
            assert initial == 503

            await state.set_live(True)

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"http://127.0.0.1:{port}/healthz"
                ) as resp:
                    assert resp.status == 200
                    assert (await resp.json()) == {
                        "status": "healthy",
                        "ack_slot": "unknown",
                    }

            await state.set_live(False)

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"http://127.0.0.1:{port}/healthz"
                ) as resp:
                    assert resp.status == 503
                    assert (await resp.json()) == {
                        "status": "unhealthy",
                        "reason": "subscription_not_live",
                        "ack_slot": "unknown",
                    }
        finally:
            await _stop_server(task)


# ---------------------------------------------------------------------------
# AC: Server shuts down cleanly on cancellation (no port-leak)
# ---------------------------------------------------------------------------


class TestCleanShutdown:
    """AC: cancelling the server task releases the bound port."""

    @pytest.mark.asyncio
    async def test_cancellation_releases_port_for_rebind(self) -> None:
        port = _free_port()
        state = SubscriptionState()

        # Bring the server up, confirm the port is bound, then cancel.
        task = await _start_server(port, state)
        await _stop_server(task)

        # Give the kernel a tick to release the socket FD.
        await asyncio.sleep(0.1)

        # The same port should accept a fresh bind — proves no FD leak.
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        finally:
            s.close()

    @pytest.mark.asyncio
    async def test_cancellation_propagates_cancelled_error(self) -> None:
        port = _free_port()
        state = SubscriptionState()
        config = ServeConfig(healthz_port=port)

        task = asyncio.create_task(run_healthz_server(config, state))
        await _wait_for_server(port)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------
# Module-shape guards
# ---------------------------------------------------------------------------


class TestModuleShape:
    """Guard the public surface — what serve.py imports must keep working."""

    def test_run_healthz_server_is_a_coroutine_function(self) -> None:
        assert inspect.iscoroutinefunction(run_healthz_server)

    def test_module_exports_run_healthz_server(self) -> None:
        from forge.cli import _serve_healthz as mod

        assert "run_healthz_server" in getattr(mod, "__all__", [])

    def test_serve_module_re_exports_run_healthz_server(self) -> None:
        from forge.cli import serve

        assert serve.run_healthz_server is run_healthz_server

    def test_no_unrelated_env_var_consumed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sanity: setting an unrelated FORGE_* var must not change healthz_port."""
        monkeypatch.delenv("FORGE_HEALTHZ_PORT", raising=False)
        monkeypatch.setenv("FORGE_LOG_LEVEL", "debug")
        config = ServeConfig.from_env()
        assert config.healthz_port == DEFAULT_HEALTHZ_PORT
        # Reference os to keep imports honest if linters complain
        assert os.environ.get("FORGE_LOG_LEVEL") == "debug"


# ---------------------------------------------------------------------------
# TASK-FW10-001 Group E healthz scenarios — chain_ready gate
# ---------------------------------------------------------------------------


class TestHealthzChainReadyGate:
    """TASK-FW10-001 ASSUM-012: healthy iff chain_ready AND subscription live.

    Mirrors the three rows of the Group E healthz Scenario Outline:

    * Row 1: ``chain_ready=False`` → 503 / ``chain_not_ready`` regardless
      of ``live``.
    * Row 2: ``chain_ready=True`` and ``live=True`` → 200 / ``healthy``.
    * Row 3: ``chain_ready=True`` and ``live=False`` → 503 /
      ``subscription_not_live`` (subscription dropped after a healthy
      window).
    """

    @pytest.mark.asyncio
    async def test_row1_chain_not_ready_is_unhealthy_even_if_live(self) -> None:
        # Row 1: chain not composed yet (boot still running). The
        # daemon may have started attaching but the chain_ready gate
        # is closed, so kubelet must keep the pod out of service.
        state = SubscriptionState()
        await state.set_live(True)  # subscription side already up
        # chain_ready stays False
        app = build_healthz_app(state)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/healthz")
            payload = await resp.json()

        assert resp.status == 503
        assert payload == {
            "status": "unhealthy",
            "reason": "chain_not_ready",
            "ack_slot": "unknown",
        }

    @pytest.mark.asyncio
    async def test_row2_chain_ready_and_live_is_healthy(self) -> None:
        state = SubscriptionState()
        await state.set_chain_ready(True)
        await state.set_live(True)
        app = build_healthz_app(state)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/healthz")
            payload = await resp.json()

        assert resp.status == 200
        assert payload == {"status": "healthy", "ack_slot": "unknown"}

    @pytest.mark.asyncio
    async def test_row3_chain_ready_subscription_dropped_is_unhealthy(
        self,
    ) -> None:
        # Row 3: chain composed, but the broker dropped the
        # subscription. Healthz must report unhealthy with the
        # ``subscription_not_live`` reason so kubelet can restart the
        # pod (TASK-FW10-001 §AC: "Healthz reports unhealthy if the
        # NATS subscription drops, even if chain_ready is True").
        state = SubscriptionState()
        await state.set_chain_ready(True)
        # state.live stays False (default) — simulates "broker drop
        # after a healthy window" from the Scenario Outline.
        app = build_healthz_app(state)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/healthz")
            payload = await resp.json()

        assert resp.status == 503
        assert payload == {
            "status": "unhealthy",
            "reason": "subscription_not_live",
            "ack_slot": "unknown",
        }


class TestSubscriptionStateChainReady:
    """TASK-FW10-001: ``SubscriptionState`` exposes ``chain_ready`` bool."""

    def test_default_chain_ready_is_false(self) -> None:
        state = SubscriptionState()
        assert state.chain_ready is False
        assert state.is_chain_ready() is False

    def test_set_chain_ready_under_lock_updates_value(self) -> None:
        state = SubscriptionState()

        async def _flip() -> None:
            await state.set_chain_ready(True)

        asyncio.run(_flip())
        assert state.chain_ready is True
        assert state.is_chain_ready() is True

    def test_is_healthy_requires_both_flags(self) -> None:
        state = SubscriptionState()
        assert state.is_healthy() is False  # default: nothing set

        async def _only_live() -> None:
            await state.set_live(True)

        asyncio.run(_only_live())
        assert state.is_healthy() is False

        async def _flip_chain() -> None:
            await state.set_chain_ready(True)

        asyncio.run(_flip_chain())
        assert state.is_healthy() is True


class TestHealthzAckSlotField:
    """FEAT-PAC — healthz gains an additive ``ack_slot`` field.

    The field carries the last boot/watchdog ack-slot reading so a
    standing ``curl :8088/healthz`` makes the phantom-ack wedge visible.
    It must be present in every response WITHOUT changing any existing
    key (``status`` / ``reason``).
    """

    @pytest.mark.asyncio
    async def test_ack_slot_present_and_reflects_state_on_healthy(
        self,
    ) -> None:
        # Arrange — healthy daemon, but the ack slot reads "phantom"
        # (the wedge coexists with a live, chain-ready daemon in v1).
        state = SubscriptionState()
        await state.set_chain_ready(True)
        await state.set_live(True)
        await state.set_ack_slot("phantom")
        app = build_healthz_app(state)

        # Act
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/healthz")
            payload = await resp.json()

        # Assert — existing keys unchanged; ack_slot reflects the reading.
        assert resp.status == 200
        assert payload["status"] == "healthy"
        assert "reason" not in payload
        assert payload["ack_slot"] == "phantom"

    @pytest.mark.asyncio
    async def test_ack_slot_default_unknown_present_and_keys_unchanged(
        self,
    ) -> None:
        # Fresh state: ack_slot defaults to "unknown" and the existing
        # 200 healthy payload keeps its exact ``status`` value.
        state = SubscriptionState()
        await state.set_chain_ready(True)
        await state.set_live(True)
        app = build_healthz_app(state)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/healthz")
            payload = await resp.json()

        assert resp.status == 200
        assert payload == {"status": "healthy", "ack_slot": "unknown"}

    @pytest.mark.asyncio
    async def test_ack_slot_present_on_unhealthy_without_disturbing_keys(
        self,
    ) -> None:
        # The field appears on 503 responses too; ``status`` and
        # ``reason`` are byte-unchanged.
        state = SubscriptionState()
        await state.set_ack_slot("held")
        app = build_healthz_app(state)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/healthz")
            payload = await resp.json()

        assert resp.status == 503
        assert payload["status"] == "unhealthy"
        assert payload["reason"] == "chain_not_ready"
        assert payload["ack_slot"] == "held"


# Keep ``web`` imported (used implicitly via aiohttp.test_utils) so tools
# like ``ruff`` do not flag it. The ``web`` symbol is also part of the
# public aiohttp surface this module is documented to use.
_ = web

"""Every terminal acknowledgement waits for the runner's confirmed stop.

Review round 1 (3 October 2026), findings R2 and R5. The runner's real stop
route is served over real HTTP (uvicorn, loopback); the factory side is the
real :class:`AckAfterStop`, the real production check, the real consumer and a
real migrated ledger.

* A build that ended COMPLETE is acknowledged at once: nothing is left.
* A build whose no-terminal fallback recorded it FAILED (an interrupted run),
  redelivered to a restarted factory, is not acknowledged while its runner
  cannot confirm the stop (its container engine cannot be asked), and is
  acknowledged once it can — no "was it cancelled?" filter, no memory.
* An older runner without the route (404) is "not applicable": acknowledged.
* A cancel whose stop request cannot reach the runner can be retried.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import AsyncMock

import pytest
from starlette.applications import Starlette

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations as lifecycle_migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.persistence.migrations import (
    lifecycle_bridge_registry as bridge_migration,
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _serve(app: Any) -> Iterator[str]:
    import uvicorn

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        assert time.monotonic() < deadline, "server never started"
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def runner_url() -> Iterator[str]:
    from forge.subagents.runner_http import app

    yield from _serve(app)


@pytest.fixture
def old_runner_url() -> Iterator[str]:
    yield from _serve(Starlette(routes=[]))


@pytest.fixture
def ledger(tmp_path: Path):
    db_path = tmp_path / "forge.db"
    cx = sqlite_connect.connect_writer(db_path)
    lifecycle_migrations.apply_at_boot(cx)
    bridge_migration.apply(cx)
    yield cx, SqliteLifecyclePersistence(connection=cx, db_path=db_path)
    cx.close()


def _payload(feature_id: str, yaml_path: Path) -> Any:
    from nats_core.events import BuildQueuedPayload

    now = datetime.now(UTC).isoformat()
    return BuildQueuedPayload.model_validate(
        {
            "feature_id": feature_id,
            "repo": "example/example",
            "branch": "main",
            "feature_yaml_path": str(yaml_path),
            "max_turns": 5,
            "sdk_timeout_seconds": 1800,
            "wave_gating": True,
            "config_overrides": None,
            "triggered_by": "cli",
            "originating_adapter": "cli-wrapper",
            "originating_user": "rich",
            "correlation_id": f"corr-{feature_id}",
            "parent_request_id": None,
            "retry_count": 0,
            "requested_at": now,
            "queued_at": now,
        }
    )


def _guard(pool: Any, url: str) -> Any:
    from forge.cli._serve_production import build_runner_stop_check
    from forge.lifecycle_bridge.build_stop import AckAfterStop

    return AckAfterStop(
        build_runner_stop_check(sqlite_pool=pool, default_url=url),
        recheck_seconds=0.3,
    )


def _wireup(cx: Any, guard: Any) -> Any:
    from forge.lifecycle_bridge.bridge import LifecycleBridge
    from forge.lifecycle_bridge.translation import StreamEventTranslator
    from forge.lifecycle_bridge.wireup import LifecycleBridgeWireup
    from forge.persistence.repositories.bridge_registry import BridgeRegistry

    return LifecycleBridgeWireup(
        bridge=LifecycleBridge(registry=BridgeRegistry(connection=cx)),
        translator=StreamEventTranslator(),
        publisher=object(),
        stream_source=object(),
        ack_guard=guard,
    )


def test_a_complete_build_is_acknowledged_at_once(ledger, runner_url, tmp_path):
    cx, pool = ledger
    payload = _payload("FEAT-AK01", tmp_path / "f.yaml")
    build_id = pool.record_pending_build(payload)
    cx.execute("UPDATE builds SET status = 'COMPLETE' WHERE build_id = ?", (build_id,))
    guard = _guard(pool, runner_url)
    handle = AsyncMock()

    async def _go() -> float:
        started = time.monotonic()
        await _wireup(cx, guard)._on_terminal(
            handle, payload.feature_id, payload.correlation_id
        )
        return time.monotonic() - started

    elapsed = asyncio.run(_go())
    handle.ack.assert_awaited_once()
    assert guard.held() == []
    assert elapsed < 5.0


def test_an_interrupted_failed_build_redelivered_after_a_restart_waits(
    ledger, runner_url, tmp_path, monkeypatch
):
    from nats_core.envelope import EventType, MessageEnvelope

    from forge.adapters.nats.pipeline_consumer import (
        PipelineConsumerDeps,
        handle_message,
    )
    from forge.cli._serve_deps import _build_is_duplicate_terminal
    from forge.config.models import (
        FilesystemPermissions,
        ForgeConfig,
        PermissionsConfig,
        PipelineConfig,
    )

    cx, pool = ledger
    allow = (tmp_path / "allow").resolve()
    allow.mkdir()
    payload = _payload("FEAT-AK02", allow / "f.yaml")
    build_id = pool.record_pending_build(payload)
    # The no-terminal fallback recorded the interrupted run FAILED, and the
    # factory restarted: nothing in memory says it was interrupted.
    cx.execute("UPDATE builds SET status = 'FAILED' WHERE build_id = ?", (build_id,))
    # The runner cannot confirm: its container engine cannot be asked.
    broken = tmp_path / "engine"
    broken.write_text("#!/bin/sh\necho 'engine down' >&2\nexit 1\n")
    broken.chmod(0o755)
    monkeypatch.setenv("FORGE_FIXTURE_ENGINE", str(broken))

    guard = _guard(pool, runner_url)
    deps = PipelineConsumerDeps(
        forge_config=ForgeConfig(
            pipeline=PipelineConfig(),
            permissions=PermissionsConfig(
                filesystem=FilesystemPermissions(allowlist=[allow])
            ),
        ),
        is_duplicate_terminal=_build_is_duplicate_terminal(pool),
        dispatch_build=AsyncMock(),
        publish_build_failed=AsyncMock(),
        ack_guard=guard,
    )
    msg = AsyncMock()
    msg.data = (
        MessageEnvelope(
            source_id="cli-wrapper",
            event_type=EventType.BUILD_QUEUED,
            correlation_id=payload.correlation_id,
            payload=payload.model_dump(mode="json"),
        )
        .model_dump_json()
        .encode()
    )

    async def _go() -> tuple[bool, bool]:
        await handle_message(msg, deps)
        await asyncio.sleep(1.5)
        held = msg.ack.await_count == 0 and guard.held() != []
        monkeypatch.delenv("FORGE_FIXTURE_ENGINE")  # the engine answers again
        deadline = asyncio.get_running_loop().time() + 20
        while msg.ack.await_count == 0:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.1)
        await guard.shutdown()
        return held, msg.ack.await_count == 1

    held, acked = asyncio.run(_go())
    assert held, "a FAILED build was acknowledged without a confirmed stop"
    assert acked
    deps.dispatch_build.assert_not_awaited()


def test_an_older_runner_without_the_route_is_not_applicable(
    ledger, old_runner_url, tmp_path
):
    cx, pool = ledger
    payload = _payload("FEAT-AK03", tmp_path / "f.yaml")
    pool.record_pending_build(payload)
    handle = AsyncMock()
    asyncio.run(
        _wireup(cx, _guard(pool, old_runner_url))._on_terminal(
            handle, payload.feature_id, payload.correlation_id
        )
    )
    handle.ack.assert_awaited_once()


def test_a_cancel_that_cannot_reach_the_runner_can_be_retried(
    ledger, runner_url, tmp_path
):
    from forge.cli._serve_production import build_runner_stop_check
    from forge.lifecycle_bridge.bridge import AckHandle, BuildContext, LifecycleBridge
    from forge.persistence.repositories.bridge_registry import BridgeRegistry

    cx, pool = ledger
    payload = _payload("FEAT-AK04", tmp_path / "f.yaml")
    pool.record_pending_build(payload)
    where = {"url": f"http://127.0.0.1:{_free_port()}"}  # nothing listens

    async def _stopper(feature_id: str, correlation_id: str) -> Any:
        return await build_runner_stop_check(
            sqlite_pool=pool, default_url=where["url"]
        )(feature_id, correlation_id, "cancel")

    bridge = LifecycleBridge(registry=BridgeRegistry(connection=cx), build_stopper=_stopper)
    bridge.attach(
        BuildContext(
            feature_id=payload.feature_id,
            thread_id="t",
            run_id="r",
            correlation_id=payload.correlation_id,
            deadline_at=datetime.now(UTC) + timedelta(seconds=300),
        ),
        AckHandle(token="ack"),
    )
    first = asyncio.run(bridge.request_cancel(payload.feature_id))
    where["url"] = runner_url
    second = asyncio.run(bridge.request_cancel(payload.feature_id))
    assert first.invoked
    assert second.invoked and second.reason == "invoked", second

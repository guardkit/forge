"""The ordinary interrupt reaches the runner the build was launched on.

Review findings R8 and R9 (simplified design, 4 October 2026). Two stand-in
runners serve the two LangGraph endpoints the interrupt uses (list a thread's
runs, cancel a run) over real loopback HTTP, and record what they were asked:
one is the global runner (``FORGE_AUTOBUILD_RUNNER_URL``), the other the
repository's sandbox runner named in ``planning.sandboxes``.

* R9: ``forge cancel``'s production canceller interrupts a sandboxed build's
  run on its PROJECT runner, and a build without a sandbox on the global one.
* R8: strict-policy boot settlement interrupts a refused build's recorded run
  before settling it FAILED.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import AsyncMock

import pytest
import yaml
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli._serve_deps_state_channel import build_autobuild_state_initialiser
from forge.lifecycle import migrations as lifecycle_migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence

REPO = "example/sandboxed"
OTHER = "example/plain"


class _StandInRunner:
    def __init__(self) -> None:
        self.cancelled: list[str] = []

        async def _runs(request: Request) -> JSONResponse:
            thread = request.path_params["thread"]
            return JSONResponse(
                [{"run_id": f"run-of-{thread}", "thread_id": thread, "status": "running"}]
            )

        async def _cancel(request: Request) -> JSONResponse:
            self.cancelled.append(request.path_params["thread"])
            return JSONResponse({})

        self.app = Starlette(
            routes=[
                Route("/threads/{thread}/runs", _runs, methods=["GET"]),
                Route(
                    "/threads/{thread}/runs/{run}/cancel", _cancel, methods=["POST"]
                ),
            ]
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
        assert time.monotonic() < deadline
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def runners() -> Iterator[dict[str, Any]]:
    global_runner, project_runner = _StandInRunner(), _StandInRunner()
    serving = [_serve(global_runner.app), _serve(project_runner.app)]
    global_url, project_url = next(serving[0]), next(serving[1])
    yield {
        "global": global_runner,
        "project": project_runner,
        "global_url": global_url,
        "project_url": project_url,
    }
    for gen in serving:
        for _ in gen:
            pass


def _payload(feature_id: str, repo: str) -> Any:
    from nats_core.events import BuildQueuedPayload

    now = datetime.now(UTC)
    return BuildQueuedPayload(
        feature_id=feature_id,
        repo=repo,
        branch="main",
        feature_yaml_path=f"/srv/forge/features/{feature_id}.yaml",
        triggered_by="cli",
        originating_adapter="cli-wrapper",
        correlation_id=f"corr-{feature_id}",
        requested_at=now,
        queued_at=now,
    )


def _ledger(db_path: Path) -> SqliteLifecyclePersistence:
    cx = sqlite_connect.connect_writer(db_path)
    lifecycle_migrations.apply_at_boot(cx)
    return SqliteLifecyclePersistence(connection=cx, db_path=db_path)


def _launched(pool: SqliteLifecyclePersistence, feature_id: str, repo: str) -> tuple[str, str]:
    """A build whose run the ledger recorded on thread ``thread-<feature>``."""
    payload = _payload(feature_id, repo)
    build_id = pool.record_pending_build(payload)
    build_autobuild_state_initialiser(pool).initialise_autobuild_state(
        build_id=build_id,
        feature_id=feature_id,
        task_id=f"thread-{feature_id}",
        correlation_id=payload.correlation_id,
        lifecycle="starting",
        wave_index=0,
        task_index=0,
    )
    return build_id, f"thread-{feature_id}"


def _config_file(tmp_path: Path, project_url: str) -> Path:
    path = tmp_path / "forge.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "permissions": {"filesystem": {"allowlist": ["/srv/forge"]}},
                "planning": {
                    "target_repo_paths": {REPO: "/srv/repos/sandboxed"},
                    "sandboxes": {
                        REPO: {
                            "name": "sandboxed",
                            "sidecar_url": "http://127.0.0.1:1",
                            "runner_url": project_url,
                        }
                    },
                },
            }
        )
    )
    return path


def test_forge_cancel_interrupts_the_build_on_its_own_runner(
    runners, tmp_path, monkeypatch
) -> None:
    from forge.cli.runtime import build_cli_runtime

    db_path = tmp_path / "forge.db"
    pool = _ledger(db_path)
    _, sandboxed_thread = _launched(pool, "FEAT-SBX1", REPO)
    _, plain_thread = _launched(pool, "FEAT-PLN1", OTHER)
    pool.connection.close()
    monkeypatch.setenv("FORGE_AUTOBUILD_RUNNER_URL", runners["global_url"])
    monkeypatch.setenv(
        "FORGE_CONFIG_PATH", str(_config_file(tmp_path, runners["project_url"]))
    )

    runtime = build_cli_runtime(db_path)
    canceller = runtime.cli_steering_handler.async_task_canceller
    assert canceller.cancel_async_task(sandboxed_thread) is True
    assert canceller.cancel_async_task(plain_thread) is True

    assert runners["project"].cancelled == [sandboxed_thread]
    assert runners["global"].cancelled == [plain_thread]


def test_boot_settlement_interrupts_a_refused_builds_recorded_run(
    runners, tmp_path, monkeypatch
) -> None:
    from forge.cli._serve_production import _settle_strict_runless_builds_at_boot
    from forge.config.models import ForgeConfig
    from forge.lifecycle.state_machine import BuildState

    pool = _ledger(tmp_path / "forge.db")
    build_id, thread = _launched(pool, "FEAT-STR1", OTHER)
    pool.connection.execute(
        "UPDATE builds SET status = 'RUNNING' WHERE build_id = ?", (build_id,)
    )
    monkeypatch.setenv("FORGE_AUTOBUILD_RUNNER_URL", runners["global_url"])
    strict = ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/srv/forge"]}},
            "publication": {"builds_may_run_inside_the_coordinator": False},
        }
    )
    settled = asyncio.run(
        _settle_strict_runless_builds_at_boot(
            pool, strict, AsyncMock(), [pool.get_build_row(build_id)]
        )
    )
    assert settled == 1
    assert pool.get_build_row(build_id).status is BuildState.FAILED
    assert runners["global"].cancelled == [thread]
    pool.connection.close()

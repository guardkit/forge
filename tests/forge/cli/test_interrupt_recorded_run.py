"""The ordinary interrupt reaches the runner the build was launched on.

Review findings R8 and R9 (simplified design, 4 October 2026). Two stand-in
runners serve the two LangGraph endpoints the interrupt uses (list a thread's
runs, cancel a run) over real loopback HTTP, and record what they were asked:
one is the global runner (``FORGE_AUTOBUILD_RUNNER_URL``), the other the
repository's sandbox runner named in ``planning.sandboxes``.

* R9: the whole ``forge --config <file> cancel`` command interrupts a RUNNING
  sandboxed build's run on its PROJECT runner (from the configuration the
  command was given), and a build without a sandbox on the global one.
* R8: strict-policy boot settlement interrupts a refused build's recorded run
  before settling it FAILED, and leaves it unsettled when the interrupt cannot
  be sent.
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
        #: True: the runner restarted and knows no thread (answers 404).
        self.forgot = False

        async def _runs(request: Request) -> JSONResponse:
            thread = request.path_params["thread"]
            if self.forgot:
                return JSONResponse({"detail": "Thread not found"}, status_code=404)
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
    """The whole ``forge --config <file> cancel`` command, a RUNNING build."""
    from click.testing import CliRunner

    from forge.cli.main import main
    from forge.lifecycle.state_machine import BuildState

    db_path = tmp_path / "forge.db"
    pool = _ledger(db_path)
    sandboxed_build, sandboxed_thread = _launched(pool, "FEAT-SBX1", REPO)
    plain_build, plain_thread = _launched(pool, "FEAT-PLN1", OTHER)
    for build_id in (sandboxed_build, plain_build):
        pool.connection.execute(
            "UPDATE builds SET status = 'RUNNING' WHERE build_id = ?", (build_id,)
        )
    pool.connection.close()
    config = _config_file(tmp_path, runners["project_url"])
    monkeypatch.setenv("FORGE_AUTOBUILD_RUNNER_URL", runners["global_url"])
    monkeypatch.delenv("FORGE_CONFIG_PATH", raising=False)
    # The cancelled notice goes to the bus; no bus here, so it is recorded.
    notices: list[str] = []
    monkeypatch.setattr(
        "forge.cli.queue.publish", lambda subject, body: notices.append(subject)
    )
    monkeypatch.chdir(tmp_path / "..")  # no forge.yaml in the working folder

    for feature in ("FEAT-SBX1", "FEAT-PLN1"):
        result = CliRunner().invoke(
            main,
            ["--config", str(config), "cancel", feature, "--db", str(db_path)],
        )
        assert result.exit_code == 0, result.output

    assert runners["project"].cancelled == [sandboxed_thread]
    assert runners["global"].cancelled == [plain_thread]
    after = _ledger(db_path)
    assert after.get_build_row(sandboxed_build).status is BuildState.CANCELLED
    after.connection.close()


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


def test_boot_settlement_holds_a_build_whose_interrupt_could_not_be_sent(
    runners, tmp_path, monkeypatch
) -> None:
    """Review R8: the runner cannot be reached, so the refused build is not
    settled (its place not let go); the next attempt reaches it and settles."""
    from forge.cli._serve_production import _settle_strict_runless_builds_at_boot
    from forge.config.models import ForgeConfig
    from forge.lifecycle.state_machine import BuildState

    pool = _ledger(tmp_path / "forge.db")
    build_id, thread = _launched(pool, "FEAT-STR2", OTHER)
    pool.connection.execute(
        "UPDATE builds SET status = 'RUNNING' WHERE build_id = ?", (build_id,)
    )
    strict = ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/srv/forge"]}},
            "publication": {"builds_may_run_inside_the_coordinator": False},
        }
    )

    def _settle() -> int:
        return asyncio.run(
            _settle_strict_runless_builds_at_boot(
                pool, strict, AsyncMock(), [pool.get_build_row(build_id)]
            )
        )

    monkeypatch.setenv("FORGE_AUTOBUILD_RUNNER_URL", f"http://127.0.0.1:{_free_port()}")
    assert _settle() == 0
    assert pool.get_build_row(build_id).status is BuildState.RUNNING

    monkeypatch.setenv("FORGE_AUTOBUILD_RUNNER_URL", runners["global_url"])
    assert _settle() == 1
    assert pool.get_build_row(build_id).status is BuildState.FAILED
    assert runners["global"].cancelled == [thread]
    pool.connection.close()


def _cancel_command(db_path: Path, feature: str, *extra: str) -> Any:
    from click.testing import CliRunner

    from forge.cli.main import main

    return CliRunner().invoke(main, [*extra, "cancel", feature, "--db", str(db_path)])


def _running(pool: SqliteLifecyclePersistence, feature: str, repo: str) -> tuple[str, str]:
    build_id, thread = _launched(pool, feature, repo)
    pool.connection.execute(
        "UPDATE builds SET status = 'RUNNING' WHERE build_id = ?", (build_id,)
    )
    return build_id, thread


@pytest.fixture
def quiet_bus(monkeypatch) -> list[str]:
    notices: list[str] = []
    monkeypatch.setattr(
        "forge.cli.queue.publish", lambda subject, body: notices.append(subject)
    )
    return notices


def test_forge_cancel_with_an_unreachable_runner_cancels_nothing(
    tmp_path, monkeypatch, quiet_bus
) -> None:
    from forge.lifecycle.state_machine import BuildState

    db_path = tmp_path / "forge.db"
    pool = _ledger(db_path)
    build_id, _ = _running(pool, "FEAT-UNR1", OTHER)
    pool.connection.close()
    monkeypatch.setenv("FORGE_AUTOBUILD_RUNNER_URL", f"http://127.0.0.1:{_free_port()}")
    monkeypatch.delenv("FORGE_CONFIG_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    result = _cancel_command(db_path, "FEAT-UNR1")
    assert result.exit_code == 2
    assert "could not reach its runner; nothing was cancelled — try again" in result.output
    after = _ledger(db_path)
    assert after.get_build_row(build_id).status is BuildState.RUNNING
    after.connection.close()


def test_forge_cancel_without_config_uses_forge_config_path(
    runners, tmp_path, monkeypatch, quiet_bus
) -> None:
    db_path = tmp_path / "forge.db"
    pool = _ledger(db_path)
    _, thread = _running(pool, "FEAT-ENV1", REPO)
    pool.connection.close()
    monkeypatch.setenv("FORGE_AUTOBUILD_RUNNER_URL", runners["global_url"])
    monkeypatch.setenv(
        "FORGE_CONFIG_PATH", str(_config_file(tmp_path, runners["project_url"]))
    )
    work = tmp_path / "elsewhere"
    work.mkdir()
    monkeypatch.chdir(work)  # no ./forge.yaml, no --config
    result = _cancel_command(db_path, "FEAT-ENV1")
    assert result.exit_code == 0, result.output
    assert runners["project"].cancelled == [thread]
    assert runners["global"].cancelled == []


def test_forge_cancel_after_the_runner_restarted_still_cancels(
    runners, tmp_path, monkeypatch, quiet_bus
) -> None:
    """The right runner no longer knows the thread (404): nothing to stop."""
    from forge.lifecycle.state_machine import BuildState

    db_path = tmp_path / "forge.db"
    pool = _ledger(db_path)
    build_id, _ = _running(pool, "FEAT-RST1", OTHER)
    pool.connection.close()
    runners["global"].forgot = True
    monkeypatch.setenv("FORGE_AUTOBUILD_RUNNER_URL", runners["global_url"])
    monkeypatch.delenv("FORGE_CONFIG_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    result = _cancel_command(db_path, "FEAT-RST1")
    assert result.exit_code == 0, result.output
    after = _ledger(db_path)
    assert after.get_build_row(build_id).status is BuildState.CANCELLED
    after.connection.close()


def _recovered_dispatch(tmp_path: Path, monkeypatch, outcome: str, starter: Any):
    """Production consumer deps over a recovered (INTERRUPTED) build whose
    earlier run the ledger recorded; the gate answers ``outcome``."""
    from forge.adapters.nats.pipeline_consumer import PipelineConsumerDeps  # noqa: F401
    from forge.cli import _serve_deps_gating, _serve_gate_activation
    from forge.cli._serve_deps import build_pipeline_consumer_deps
    from forge.gating.sqlite_adapters import build_sqlite_gate_adapters
    from tests.integration.test_gate_activation_production_wiring import (
        FixedClock,
        OrderRecordingNats,
        _build_parts,
    )

    nats = OrderRecordingNats()
    pool = _ledger(tmp_path / "forge.db")
    cfg = _plain_config()
    _serve_deps_gating._reset_for_tests()
    _serve_deps_gating.bind_gate_parts(_build_parts(nats, forge_config=cfg))
    repo, sm = build_sqlite_gate_adapters(pool, clock=FixedClock())
    deps = build_pipeline_consumer_deps(
        nats,
        cfg,
        pool,
        async_task_starter=starter,
        gate_repository=repo,
        gate_state_machine=sm,
        gate_clock=FixedClock(),
    )
    payload = _payload("FEAT-REC1", OTHER)
    build_id, thread = _launched(pool, "FEAT-REC1", OTHER)
    pool.connection.execute(
        "UPDATE builds SET status = 'INTERRUPTED' WHERE build_id = ?", (build_id,)
    )

    async def _gate(**_: Any) -> Any:
        return getattr(_serve_gate_activation.GateOutcome, outcome)

    monkeypatch.setattr(_serve_gate_activation, "maybe_gate_build", _gate)
    return nats, pool, deps, build_id, thread


def _plain_config() -> Any:
    from forge.config.models import ForgeConfig

    return ForgeConfig.model_validate(
        {"permissions": {"filesystem": {"allowlist": ["/srv/forge"]}}}
    )


class _Msg:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.acks = 0

    async def ack(self) -> None:
        self.acks += 1


def _message_for(pool: SqliteLifecyclePersistence, build_id: str) -> bytes:
    from nats_core.envelope import EventType, MessageEnvelope

    row = pool.get_build_row(build_id)
    payload = _payload(row.feature_id, row.repo).model_copy(
        update={"correlation_id": row.correlation_id, "queued_at": row.queued_at}
    )
    return (
        MessageEnvelope(
            source_id="forge-cli",
            event_type=EventType.BUILD_QUEUED,
            correlation_id=row.correlation_id,
            payload=payload.model_dump(mode="json"),
        )
        .model_dump_json()
        .encode()
    )


@pytest.mark.parametrize("outcome", ["CANCELLED", "RESUMED"])
def test_a_recovered_build_is_held_when_its_earlier_run_cannot_be_interrupted(
    tmp_path, monkeypatch, outcome
) -> None:
    """The card is rejected (CANCELLED), or approved and the relaunch fails
    (RESUMED): the earlier run cannot be reached, so the message is not
    acknowledged, nothing is reported failed, and its identity is kept."""
    from forge.adapters.nats.pipeline_consumer import handle_message
    from forge.cli import _serve_deps_gating

    class _FailingStarter:
        async def astart_async_task(self, **_: Any) -> str:
            raise RuntimeError("the relaunch could not be submitted")

    monkeypatch.setenv("FORGE_AUTOBUILD_RUNNER_URL", f"http://127.0.0.1:{_free_port()}")
    nats, pool, deps, build_id, thread = _recovered_dispatch(
        tmp_path, monkeypatch, outcome, _FailingStarter()
    )
    msg = _Msg(_message_for(pool, build_id))
    try:
        asyncio.run(handle_message(msg, deps))
    finally:
        _serve_deps_gating._reset_for_tests()
    assert msg.acks == 0
    assert nats.published.get("pipeline.build-failed.FEAT-REC1", []) == []
    kept = pool.connection.execute(
        "SELECT task_id FROM async_tasks WHERE build_id = ?", (build_id,)
    ).fetchall()
    assert [r[0] for r in kept] == [thread]
    pool.connection.close()

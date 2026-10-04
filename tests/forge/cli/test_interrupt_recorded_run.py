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
        #: Called (in the server's thread) as each cancel arrives.
        self.probe: Any = None

        async def _runs(request: Request) -> JSONResponse:
            thread = request.path_params["thread"]
            if self.forgot:
                return JSONResponse({"detail": "Thread not found"}, status_code=404)
            return JSONResponse(
                [{"run_id": f"run-of-{thread}", "thread_id": thread, "status": "running"}]
            )

        async def _cancel(request: Request) -> JSONResponse:
            if self.probe is not None:
                self.probe()
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


# ---------------------------------------------------------------------------
# Review R12 / R10: a recovered build's run is interrupted before its card,
# through the REAL approval gate (first restart: dispatch_build; second
# restart: rearm_paused_gates).
# ---------------------------------------------------------------------------


class _Msg:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.acks = 0

    async def ack(self) -> None:
        self.acks += 1


class _Recovery:
    """A ledger holding one recovered build whose run it recorded, the real
    gate parts over the in-memory bus double, and production consumer deps."""

    FEATURE = "FEAT-REC1"

    def __init__(self, tmp_path: Path, starter: Any = None) -> None:
        from tests.integration.test_gate_activation_production_wiring import (
            OrderRecordingNats,
        )

        self.db_path = tmp_path / "forge.db"
        self.nats = OrderRecordingNats()
        self.pool = _ledger(self.db_path)
        self.payload = _payload(self.FEATURE, OTHER)
        self.build_id = self.pool.record_pending_build(self.payload)
        build_autobuild_state_initialiser(self.pool).initialise_autobuild_state(
            build_id=self.build_id,
            feature_id=self.FEATURE,
            task_id="thread-original",
            correlation_id=self.payload.correlation_id,
            lifecycle="starting",
            wave_index=0,
            task_index=0,
        )
        # Boot recovery's verdict on a build whose run it could not see.
        self.pool.connection.execute(
            "UPDATE builds SET status = 'INTERRUPTED' WHERE build_id = ?",
            (self.build_id,),
        )
        self.starter = starter
        self.boot()

    def boot(self) -> None:
        """A (new) coordinator process: fresh gate parts and consumer deps."""
        from forge.cli import _serve_deps_gating
        from forge.cli._serve_deps import build_pipeline_consumer_deps
        from forge.gating.sqlite_adapters import build_sqlite_gate_adapters
        from tests.integration.test_gate_activation_production_wiring import (
            FixedClock,
            _build_parts,
        )

        _serve_deps_gating._reset_for_tests()
        self.cfg = _plain_config()
        self.parts = _build_parts(self.nats, forge_config=self.cfg)
        _serve_deps_gating.bind_gate_parts(self.parts)
        self.repo, self.sm = build_sqlite_gate_adapters(self.pool, clock=FixedClock())
        self.deps = build_pipeline_consumer_deps(
            self.nats,
            self.cfg,
            self.pool,
            async_task_starter=self.starter or object(),
            gate_repository=self.repo,
            gate_state_machine=self.sm,
            gate_clock=FixedClock(),
        )

    def message(self) -> _Msg:
        from nats_core.envelope import EventType, MessageEnvelope

        return _Msg(
            MessageEnvelope(
                source_id="forge-cli",
                event_type=EventType.BUILD_QUEUED,
                correlation_id=self.payload.correlation_id,
                payload=self.payload.model_dump(mode="json"),
            )
            .model_dump_json()
            .encode()
        )

    def status(self) -> str:
        import sqlite3

        cx = sqlite3.connect(self.db_path)
        try:
            return cx.execute(
                "SELECT status FROM builds WHERE build_id = ?", (self.build_id,)
            ).fetchone()[0]
        finally:
            cx.close()

    def cards(self) -> int:
        return len(self.nats.published.get(f"pipeline.build-paused.{self.FEATURE}", []))

    async def answer(self, decision: str) -> None:
        from tests.integration.test_gate_activation_production_wiring import (
            _drive_response,
        )

        deadline = asyncio.get_running_loop().time() + 30
        while True:
            row = self.pool.connection.execute(
                "SELECT pending_approval_request_id FROM builds WHERE build_id = ?",
                (self.build_id,),
            ).fetchone()
            if row and row[0]:
                break
            assert asyncio.get_running_loop().time() < deadline, "no card"
            await asyncio.sleep(0.05)
        await _drive_response(
            self.nats, build_id=self.build_id, request_id=row[0], decision=decision
        )

    async def rearm(self) -> list[Any]:
        from forge.cli._serve_gate_activation import rearm_paused_gates
        from tests.integration.test_gate_activation_production_wiring import (
            FixedClock,
        )

        async def _launch(**_: Any) -> None:
            raise AssertionError("not launched in these checks")

        return await rearm_paused_gates(
            parts=self.parts,
            sqlite_pool=self.pool,
            gate_repository=self.repo,
            gate_state_machine=self.sm,
            resume_launcher=_launch,
            client=self.nats,
            clock=FixedClock(),
            forge_config=self.cfg,
        )


@pytest.fixture
def recovery(tmp_path: Path) -> Iterator[_Recovery]:
    from forge.cli import _serve_deps_gating

    made = _Recovery(tmp_path)
    yield made
    _serve_deps_gating._reset_for_tests()
    made.pool.connection.close()


def _plain_config() -> Any:
    from forge.config.models import ForgeConfig

    return ForgeConfig.model_validate(
        {"permissions": {"filesystem": {"allowlist": ["/srv/forge"]}}}
    )


def _dead_url() -> str:
    return f"http://127.0.0.1:{_free_port()}"


def test_first_restart_reject_interrupts_before_the_card(
    runners, recovery, monkeypatch
) -> None:
    from forge.adapters.nats.pipeline_consumer import handle_message

    seen: list[str] = []
    runners["global"].probe = lambda: seen.append(recovery.status())
    monkeypatch.setenv("FORGE_AUTOBUILD_RUNNER_URL", runners["global_url"])
    msg = recovery.message()

    async def _go() -> None:
        task = asyncio.ensure_future(handle_message(msg, recovery.deps))
        await recovery.answer("reject")
        await asyncio.wait_for(task, timeout=30)

    asyncio.run(_go())
    assert runners["global"].cancelled == ["thread-original"]
    # The interrupt went out while the build was still INTERRUPTED — before
    # its card, and so before the rejection wrote CANCELLED.
    assert seen == ["INTERRUPTED"]
    assert recovery.status() == "CANCELLED"
    assert msg.acks == 1


def test_first_restart_unreachable_runner_shows_no_card_until_it_answers(
    runners, recovery, monkeypatch
) -> None:
    from forge.adapters.nats.pipeline_consumer import handle_message

    monkeypatch.setenv("FORGE_AUTOBUILD_RUNNER_URL", _dead_url())
    held = recovery.message()
    asyncio.run(handle_message(held, recovery.deps))
    assert held.acks == 0 and recovery.cards() == 0
    assert recovery.status() == "INTERRUPTED"

    # The runner answers again; the redelivery shows the card.
    monkeypatch.setenv("FORGE_AUTOBUILD_RUNNER_URL", runners["global_url"])
    again = recovery.message()

    async def _go() -> None:
        task = asyncio.ensure_future(handle_message(again, recovery.deps))
        deadline = asyncio.get_running_loop().time() + 30
        while recovery.cards() == 0:
            assert asyncio.get_running_loop().time() < deadline, "no card"
            await asyncio.sleep(0.05)
        await recovery.answer("reject")
        await asyncio.wait_for(task, timeout=30)

    asyncio.run(_go())
    assert runners["global"].cancelled == ["thread-original"]
    assert again.acks == 1


def _paused_after_a_first_restart(recovery: _Recovery) -> None:
    """The recovered build's card is shown (PAUSED), then the coordinator
    restarts with the card unanswered."""
    from forge.adapters.nats.pipeline_consumer import handle_message

    async def _go() -> None:
        task = asyncio.ensure_future(handle_message(recovery.message(), recovery.deps))
        deadline = asyncio.get_running_loop().time() + 30
        while recovery.status() != "PAUSED":
            assert asyncio.get_running_loop().time() < deadline, "no card"
            await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(_go())
    recovery.boot()


def test_second_restart_reject_interrupts_before_the_rearmed_card(
    runners, recovery, monkeypatch
) -> None:
    monkeypatch.setenv("FORGE_AUTOBUILD_RUNNER_URL", runners["global_url"])
    _paused_after_a_first_restart(recovery)
    seen: list[str] = []
    runners["global"].probe = lambda: seen.append(recovery.status())
    cards_before = recovery.cards()

    async def _go() -> None:
        tasks = await recovery.rearm()
        assert len(tasks) == 1
        await recovery.answer("reject")
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=30)

    asyncio.run(_go())
    assert recovery.cards() == cards_before + 1
    assert seen == ["PAUSED"], seen
    assert recovery.status() == "CANCELLED"


def test_second_restart_unreachable_runner_rearms_no_card_until_it_answers(
    runners, recovery, monkeypatch
) -> None:
    monkeypatch.setenv("FORGE_AUTOBUILD_RUNNER_URL", runners["global_url"])
    _paused_after_a_first_restart(recovery)
    cards_before = recovery.cards()

    monkeypatch.setenv("FORGE_AUTOBUILD_RUNNER_URL", _dead_url())
    assert asyncio.run(recovery.rearm()) == []
    assert recovery.cards() == cards_before
    assert recovery.status() == "PAUSED"

    # The next boot reaches the runner and re-arms the card.
    recovery.boot()
    monkeypatch.setenv("FORGE_AUTOBUILD_RUNNER_URL", runners["global_url"])

    async def _go() -> None:
        tasks = await recovery.rearm()
        assert len(tasks) == 1
        await recovery.answer("reject")
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=30)

    asyncio.run(_go())
    assert recovery.cards() == cards_before + 1


def test_a_cancelled_relaunch_puts_the_original_identity_back(
    runners, tmp_path, monkeypatch
) -> None:
    """Review R10: the coordinator's dispatch is cancelled (a shutdown) while
    the approved relaunch is still being submitted: the earlier run's row is
    restored, never lost."""
    from forge.adapters.nats.pipeline_consumer import handle_message
    from forge.cli import _serve_deps_gating

    submitting = asyncio.Event()

    class _BlockedStarter:
        async def astart_async_task(self, **_: Any) -> str:
            submitting.set()
            await asyncio.sleep(3600)
            return "never"

    monkeypatch.setenv("FORGE_AUTOBUILD_RUNNER_URL", runners["global_url"])
    made = _Recovery(tmp_path, starter=_BlockedStarter())

    async def _go() -> None:
        task = asyncio.ensure_future(handle_message(made.message(), made.deps))
        await made.answer("approve")
        await asyncio.wait_for(submitting.wait(), timeout=30)
        rows = made.pool.connection.execute(
            "SELECT task_id FROM async_tasks WHERE build_id = ?", (made.build_id,)
        ).fetchall()
        assert rows == [], "the earlier identity should be cleared for the relaunch"
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    try:
        asyncio.run(_go())
        rows = made.pool.connection.execute(
            "SELECT task_id FROM async_tasks WHERE build_id = ?", (made.build_id,)
        ).fetchall()
        assert [r[0] for r in rows] == ["thread-original"]
    finally:
        _serve_deps_gating._reset_for_tests()
        made.pool.connection.close()


def test_an_approved_relaunch_whose_earlier_identity_cannot_be_cleared_is_held(
    runners, tmp_path, monkeypatch
) -> None:
    """Approved through the real gate, but the earlier run's identity cannot be
    cleared: nothing is launched and the message is held for its redelivery."""
    from forge.adapters.nats.pipeline_consumer import handle_message
    from forge.cli import _serve_deps_gating

    class _NoLaunch:
        async def astart_async_task(self, **_: Any) -> str:
            raise AssertionError("nothing may launch")

    monkeypatch.setenv("FORGE_AUTOBUILD_RUNNER_URL", runners["global_url"])
    made = _Recovery(tmp_path, starter=_NoLaunch())
    made.pool.connection.execute(
        "CREATE TRIGGER refuse_delete BEFORE DELETE ON async_tasks "
        "BEGIN SELECT RAISE(ABORT, 'refused'); END"
    )
    msg = made.message()

    async def _go() -> None:
        task = asyncio.ensure_future(handle_message(msg, made.deps))
        await made.answer("approve")
        await asyncio.wait_for(task, timeout=30)

    try:
        asyncio.run(_go())
        assert msg.acks == 0
        assert made.nats.published.get(f"pipeline.build-failed.{made.FEATURE}", []) == []
        rows = made.pool.connection.execute(
            "SELECT task_id FROM async_tasks WHERE build_id = ?", (made.build_id,)
        ).fetchall()
        assert [r[0] for r in rows] == ["thread-original"]
    finally:
        _serve_deps_gating._reset_for_tests()
        made.pool.connection.close()

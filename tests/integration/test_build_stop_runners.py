"""Stopping a build through real runners (design of 3 October 2026).

Real ``langgraph dev`` runners serve the real ``autobuild_runner`` graph and
the runner's stop route; a stand-in GuardKit (``FORGE_GUARDKIT_PATH``) starts
real processes, including one in a session of its own that ignores SIGTERM.

* The runner slot is a fence: with one job slot, a build queued behind a
  stopped build starts only after every process the stopped build owned is
  gone, and the stopped build finishes CANCELLED.
* The build's place is released only after its runner confirms the stop
  (design check "Cross-runner cancellation (R2)"): two runners X and Y, one
  place between them. A, in X, is cancelled while something carrying its
  owner marker keeps coming back; B, for Y, stays unstarted because A's
  acknowledgement is held — through the bridge's terminal handler, and again
  after the factory side is recreated (a restart) and the redelivered message
  reaches the consumer. When A's processes finally go, A is acknowledged and B
  starts.
* Builds B and C in the same runner keep running and keep their files while A
  is stopped.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from langgraph_sdk import get_client

from tests.forge.build_stop_support import (
    kill_marked,
    kill_recorded,
    launch_message,
    make_estate,
    post_stop,
    proc_alive,
    real_runner,
    wait_for,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc"),
]


def _build_id() -> str:
    return f"build-stoprun-{uuid.uuid4().hex[:10]}"


@pytest.fixture
def estate(tmp_path):
    made = make_estate(tmp_path)
    yield made
    kill_recorded(made)
    kill_marked([s["build_id"] for k, s in made.plan.items() if k != "_records"])


async def _launch(url: str, feature_id: str, build_id: str, branch: str) -> dict:
    client = get_client(url=url)
    thread = await client.threads.create()
    run = await client.runs.create(
        thread["thread_id"],
        "autobuild_runner",
        input={
            "messages": [
                {"role": "human", "content": launch_message(feature_id, build_id, branch)}
            ]
        },
    )
    return {"thread_id": thread["thread_id"], "run_id": run["run_id"], "url": url}


async def _final_lifecycle(launched: dict, feature_id: str, timeout: float = 90.0) -> str:
    client = get_client(url=launched["url"])
    await asyncio.wait_for(
        client.runs.join(launched["thread_id"], launched["run_id"]), timeout=timeout
    )
    state = await client.threads.get_state(launched["thread_id"])
    return state["values"]["async_tasks"][feature_id]["lifecycle"]


class TestTheRunnerSlotIsAFence:
    def test_a_queued_build_starts_only_after_the_stopped_builds_processes_are_gone(
        self, estate
    ):
        a_id, b_id = _build_id(), _build_id()
        estate.add_build("FEAT-RA", a_id, branch="ra")
        estate.add_build("FEAT-RB", b_id, branch="rb", must_be_gone=["FEAT-RA"])

        async def _go(url: str) -> tuple[dict, str]:
            a = await _launch(url, "FEAT-RA", a_id, "ra")
            await asyncio.to_thread(estate.pids, "FEAT-RA")
            await _launch(url, "FEAT-RB", b_id, "rb")
            await asyncio.sleep(2.0)
            assert estate.started("FEAT-RB") is None, "B started beside A in one slot"
            answer = await asyncio.to_thread(post_stop, url, a_id)
            lifecycle = await _final_lifecycle(a, "FEAT-RA")
            await asyncio.to_thread(
                wait_for,
                lambda: estate.started("FEAT-RB") is not None,
                60,
                "B never started after A was stopped",
            )
            await asyncio.to_thread(post_stop, url, b_id)
            return answer, lifecycle

        with real_runner(estate, "slot") as runner:
            answer, lifecycle = asyncio.run(_go(runner.url))
        assert answer == {"build_id": a_id, "stopped": True}
        assert lifecycle == "cancelled"
        # At the instant B started, none of A's processes was alive — not its
        # child, not its grandchild, not the SIGTERM-ignoring process in a
        # session of its own.
        assert estate.started("FEAT-RB")["others_alive"] == {"FEAT-RA": []}


class TestOtherBuildsInTheSameRunnerAreUntouched:
    def test_b_and_c_keep_running_and_keep_their_files(self, estate):
        ids = {f: _build_id() for f in ("FEAT-SA", "FEAT-SB", "FEAT-SC")}
        for f, b in ids.items():
            estate.add_build(f, b, branch=f.lower())

        async def _go(url: str) -> dict[str, Any]:
            for f, b in ids.items():
                await _launch(url, f, b, f.lower())
            recorded = {f: await asyncio.to_thread(estate.pids, f) for f in ids}
            beats = {
                f: next((estate.root / "worktrees").glob(f"*{ids[f]}*/beat"))
                for f in ("FEAT-SB", "FEAT-SC")
            }
            answer = await asyncio.to_thread(post_stop, url, ids["FEAT-SA"])
            first = {f: float(p.read_text()) for f, p in beats.items()}
            await asyncio.sleep(1.0)
            later = {f: float(p.read_text()) for f, p in beats.items()}
            alive = {
                f: [p for p, s in recorded[f] if proc_alive(p, s)] for f in ids
            }
            for f in ("FEAT-SB", "FEAT-SC"):
                await asyncio.to_thread(post_stop, url, ids[f])
            return {"answer": answer, "first": first, "later": later, "alive": alive}

        # Three job slots: the three builds run side by side in one runner.
        with real_runner(estate, "three", jobs=3) as runner:
            seen = asyncio.run(_go(runner.url))
        assert seen["answer"]["stopped"] is True
        assert seen["alive"]["FEAT-SA"] == []
        for f in ("FEAT-SB", "FEAT-SC"):
            assert len(seen["alive"][f]) == 3, f
            assert seen["later"][f] > seen["first"][f], f"{f} stopped writing"
        worktrees = estate.root / "worktrees"
        for b in ids.values():
            assert list(worktrees.glob(f"*{b}*")), "a worktree was removed"


class _Respawner:
    """Keeps a process carrying ``build_id``'s owner marker alive until released.

    Stands for a build process that cannot be stopped yet: every time the stop
    kills it, another takes its place.
    """

    def __init__(self, build_id: str) -> None:
        self._build_id = build_id
        self._held = True
        self._current: subprocess.Popen[bytes] | None = None
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self.spawned = 0

    def _loop(self) -> None:
        while self._held:
            if self._current is None or self._current.poll() is not None:
                self._current = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(600)"],
                    env={**os.environ, "GUARDKIT_RUN_OWNER": self._build_id},
                    start_new_session=True,
                )
                self.spawned += 1
            threading.Event().wait(0.05)

    def start(self) -> "_Respawner":
        self._thread.start()
        return self

    def release(self) -> None:
        self._held = False
        self._thread.join(timeout=10)

    def close(self) -> None:
        self.release()
        if self._current is not None and self._current.poll() is None:
            self._current.kill()
            self._current.wait()


def _marked_alive(build_id: str) -> list[int]:
    needle = f"GUARDKIT_RUN_OWNER={build_id}".encode()
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            entries = Path(f"/proc/{name}/environ").read_bytes().split(b"\0")
            state = Path(f"/proc/{name}/stat").read_text().rpartition(")")[2].split()[0]
        except OSError:
            continue
        if needle in entries and state not in ("Z", "X"):
            found.append(int(name))
    return found


def _queued_payload(feature_id: str, yaml_path: Path) -> Any:
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


class _Place:
    """The one place both runners share (the broker's unacknowledged message)."""

    def __init__(self) -> None:
        self.free = asyncio.Event()
        self.acked_with_marked_alive: list[int] | None = None


@pytest.mark.parametrize("restart_while_held", [False, True], ids=["held", "restart"])
class TestThePlaceIsReleasedOnlyAfterTheStop:
    def test_b_for_runner_y_waits_until_a_in_runner_x_is_gone(
        self, estate, tmp_path, restart_while_held
    ):
        from nats_core.envelope import EventType, MessageEnvelope

        from forge.adapters.nats.pipeline_consumer import (
            PipelineConsumerDeps,
            handle_message,
        )
        from forge.adapters.sqlite import connect as sqlite_connect
        from forge.cli._serve_production import build_runner_stop_check
        from forge.config.models import (
            FilesystemPermissions,
            ForgeConfig,
            PermissionsConfig,
            PipelineConfig,
        )
        from forge.lifecycle import migrations as lifecycle_migrations
        from forge.lifecycle.persistence import SqliteLifecyclePersistence
        from forge.lifecycle_bridge.bridge import (
            AckHandle,
            BuildContext,
            LifecycleBridge,
        )
        from forge.lifecycle_bridge.build_stop import AckAfterStop
        from forge.lifecycle_bridge.translation import StreamEventTranslator
        from forge.lifecycle_bridge.wireup import LifecycleBridgeWireup
        from forge.persistence.migrations import (
            lifecycle_bridge_registry as bridge_migration,
        )
        from forge.persistence.repositories.bridge_registry import BridgeRegistry

        allow = (tmp_path / "allow").resolve()
        allow.mkdir()
        yaml_path = allow / "feature.yaml"
        yaml_path.write_text("id: x\n")
        db_path = tmp_path / "forge.db"
        cx = sqlite_connect.connect_writer(db_path)
        lifecycle_migrations.apply_at_boot(cx)
        bridge_migration.apply(cx)
        pool = SqliteLifecyclePersistence(connection=cx, db_path=db_path)
        a_feature, b_feature = "FEAT-C1A1", "FEAT-C1B1"
        a_payload = _queued_payload(a_feature, yaml_path)
        a_build = pool.record_pending_build(a_payload)
        b_build = _build_id()
        estate.add_build(a_feature, a_build, branch="xa")
        estate.add_build(b_feature, b_build, branch="yb", must_be_gone=[a_feature])

        async def _go(x_url: str, y_url: str) -> dict[str, Any]:
            urls = {a_feature: x_url, b_feature: y_url}

            def _guard() -> AckAfterStop:
                return AckAfterStop(
                    build_runner_stop_check(
                        sqlite_pool=pool,
                        default_url=x_url,
                        runner_url_for_feature=lambda f: urls[f],
                    ),
                    recheck_seconds=0.5,
                )

            place = _Place()

            async def _release() -> None:
                place.acked_with_marked_alive = _marked_alive(a_build)
                place.free.set()

            async def _dispatch_b() -> None:
                await place.free.wait()
                await _launch(y_url, b_feature, b_build, "yb")

            dispatcher = asyncio.ensure_future(_dispatch_b())

            await _launch(x_url, a_feature, a_build, "xa")
            await asyncio.to_thread(estate.pids, a_feature)
            hold.write_text(a_build)
            respawner = _Respawner(a_build).start()
            try:
                guard = _guard()
                stop_check = build_runner_stop_check(
                    sqlite_pool=pool,
                    default_url=x_url,
                    runner_url_for_feature=lambda f: urls[f],
                )

                async def _stopper(f: str, c: str) -> Any:
                    return await stop_check(f, c, True)

                bridge = LifecycleBridge(
                    registry=BridgeRegistry(connection=cx), build_stopper=_stopper
                )
                bridge.attach(
                    BuildContext(
                        feature_id=a_feature,
                        thread_id="t-a",
                        run_id="r-a",
                        correlation_id=a_payload.correlation_id,
                        deadline_at=datetime.now(UTC) + timedelta(seconds=300),
                    ),
                    AckHandle(token="ack-a"),
                )
                # The cancel goes to A's runner, X, through the bridge.
                cancel = await bridge.request_cancel(a_feature)
                assert cancel.invoked
                # The build is recorded cancelled (the cancel's own row write).
                cx.execute(
                    "UPDATE builds SET status = 'CANCELLED' WHERE build_id = ?",
                    (a_build,),
                )
                wireup = LifecycleBridgeWireup(
                    bridge=bridge,
                    translator=StreamEventTranslator(),
                    publisher=object(),  # nothing is published by _on_terminal
                    stream_source=object(),
                    ack_guard=guard,
                )
                handle = AsyncMock()
                handle.ack = AsyncMock(side_effect=_release)
                terminal = asyncio.ensure_future(
                    wireup._on_terminal(
                        handle, a_feature, a_payload.correlation_id, cancelled=True
                    )
                )
                await asyncio.sleep(3.0)
                assert not place.free.is_set(), "A's place was released while held"
                assert estate.started(b_feature) is None, "B started while A held"

                if restart_while_held:
                    # The factory restarts: its terminal handler dies without
                    # acknowledging, and a NEW process's consumer sees the
                    # redelivered message for the (now cancelled) build.
                    terminal.cancel()
                    await asyncio.gather(terminal, return_exceptions=True)
                    handle.ack.assert_not_awaited()
                    guard = _guard()
                    config = ForgeConfig(
                        pipeline=PipelineConfig(),
                        permissions=PermissionsConfig(
                            filesystem=FilesystemPermissions(allowlist=[allow])
                        ),
                    )
                    deps = PipelineConsumerDeps(
                        forge_config=config,
                        is_duplicate_terminal=AsyncMock(return_value=True),
                        dispatch_build=AsyncMock(),
                        publish_build_failed=AsyncMock(),
                        ack_guard=guard,
                    )
                    msg = AsyncMock()
                    msg.data = (
                        MessageEnvelope(
                            source_id="cli-wrapper",
                            event_type=EventType.BUILD_QUEUED,
                            correlation_id=a_payload.correlation_id,
                            payload=a_payload.model_dump(mode="json"),
                        )
                        .model_dump_json()
                        .encode()
                    )
                    msg.ack = AsyncMock(side_effect=_release)
                    await handle_message(msg, deps)
                    assert guard.held() == [(a_feature, a_payload.correlation_id)]
                    await asyncio.sleep(3.0)
                    assert not place.free.is_set(), "released after the restart"
                    assert estate.started(b_feature) is None
                    deps.dispatch_build.assert_not_awaited()

                held_spawns = respawner.spawned
                respawner.release()
                hold.unlink()
                await asyncio.wait_for(place.free.wait(), timeout=60)
                if not restart_while_held:
                    await asyncio.wait_for(terminal, timeout=10)
                await asyncio.wait_for(dispatcher, timeout=30)
                await asyncio.to_thread(
                    wait_for,
                    lambda: estate.started(b_feature) is not None,
                    60,
                    "B never started after A's place was released",
                )
                await asyncio.to_thread(post_stop, y_url, b_build)
                await guard.shutdown()
                return {
                    "held_spawns": held_spawns,
                    "acked_with": place.acked_with_marked_alive,
                }
            finally:
                respawner.close()
                dispatcher.cancel()

        hold = tmp_path / "hold"
        with real_runner(
            estate,
            "X",
            app="tests.integration.held_stop_app:app",
            extra_env={"FORGE_TEST_HOLD_FILE": str(hold)},
        ) as x, real_runner(estate, "Y") as y:
            seen = asyncio.run(_go(x.url, y.url))
        cx.close()
        # The stop really was resisted while held (more than one process had
        # to be stopped), and the place went only when nothing carrying A's
        # marker was alive.
        assert seen["held_spawns"] > 1
        assert seen["acked_with"] == []
        assert estate.started(b_feature)["others_alive"] == {a_feature: []}

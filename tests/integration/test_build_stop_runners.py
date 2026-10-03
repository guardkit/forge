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
    throwaway_broker,
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


class TestAStopAskedWhileQueuedForASlot:
    def test_the_queued_build_never_spawns_and_ends_cancelled(self, estate):
        a_id, b_id = _build_id(), _build_id()
        estate.add_build("FEAT-QA", a_id, branch="qa")
        estate.add_build("FEAT-QB", b_id, branch="qb")

        async def _go(url: str) -> tuple[dict, str]:
            await _launch(url, "FEAT-QA", a_id, "qa")
            await asyncio.to_thread(estate.pids, "FEAT-QA")
            b = await _launch(url, "FEAT-QB", b_id, "qb")
            await asyncio.sleep(1.0)
            answer = await asyncio.to_thread(post_stop, url, b_id)
            await asyncio.to_thread(post_stop, url, a_id)
            return answer, await _final_lifecycle(b, "FEAT-QB")

        with real_runner(estate, "queued") as runner:
            answer, lifecycle = asyncio.run(_go(runner.url))
        assert answer == {"build_id": b_id, "stopped": True, "pending": True}
        assert lifecycle == "cancelled"
        assert estate.started("FEAT-QB") is None, "GuardKit was spawned for B"


class TestARelaunchStopsTheOriginalFirst:
    """Review R3: a coordinator-only restart relaunches a build still running.

    The runner and the build's child stay up; the factory, restarted, launches
    the same build again (a fresh thread and run, the same build ID). The
    runner stops the original first — every process gone — and only then lets
    the relaunch spawn, which completes in a worktree of its own while the
    original's worktree is kept aside.
    """

    def test_the_relaunch_waits_for_the_original_and_completes(self, estate):
        build_id = _build_id()
        estate.add_build("FEAT-RL", build_id, branch="rl", relaunch_run_seconds=1)

        async def _go(url: str) -> dict[str, Any]:
            first = await _launch(url, "FEAT-RL", build_id, "rl")
            recorded = await asyncio.to_thread(estate.pids, "FEAT-RL")
            second = await _launch(url, "FEAT-RL", build_id, "rl")
            first_lifecycle = await _final_lifecycle(first, "FEAT-RL")
            second_lifecycle = await _final_lifecycle(second, "FEAT-RL")
            return {
                "first": first_lifecycle,
                "second": second_lifecycle,
                "first_alive_after": [p for p, s in recorded if proc_alive(p, s)],
            }

        with real_runner(estate, "relaunch", jobs=2) as runner:
            seen = asyncio.run(_go(runner.url))
        relaunch = json.loads((estate.records / "FEAT-RL.relaunch.started").read_text())
        # At the instant the relaunch spawned, none of the original's
        # processes (child, grandchild, separate-session) was alive.
        assert relaunch["others_alive"] == {"FEAT-RL": []}
        assert seen["first_alive_after"] == []
        assert seen["first"] == "cancelled"
        assert seen["second"] == "completed", seen
        kept = list((estate.root / "worktrees").glob(f"{build_id}.superseded-*"))
        assert kept, "the original run's worktree was not kept"


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


async def _fetch_some(psub: Any, seconds: float) -> list[Any]:
    """Every message the durable delivers within ``seconds``."""
    import nats.errors

    got: list[Any] = []
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        try:
            got.extend(await psub.fetch(1, timeout=0.5))
        except nats.errors.TimeoutError:
            continue
    return got


def _feature_of(msg: Any) -> str:
    return json.loads(msg.data)["payload"]["feature_id"]


class TestThePlaceIsReleasedOnlyAfterTheStop:
    """Design check "Cross-runner cancellation (R2)", through a real broker.

    A throwaway JetStream broker holds the one place (a durable with
    ``max_ack_pending=1``). A's message is really outstanding while its
    acknowledgement is held, is really redelivered (short test ack_wait) to a
    restarted factory through ``handle_message``, and B's message is not
    delivered — so B is never started on runner Y — until A, cancelled in
    runner X, is confirmed stopped there.
    """

    def test_b_for_runner_y_waits_until_a_in_runner_x_is_gone(self, estate, tmp_path):
        import nats
        from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy
        from nats_core.envelope import EventType, MessageEnvelope

        from forge.adapters.nats.pipeline_consumer import (
            PipelineConsumerDeps,
            handle_message,
        )
        from forge.adapters.sqlite import connect as sqlite_connect
        from forge.cli._serve_deps import _build_is_duplicate_terminal
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
        b_payload = _queued_payload(b_feature, yaml_path)
        a_build = pool.record_pending_build(a_payload)
        b_build = _build_id()
        estate.add_build(a_feature, a_build, branch="xa")
        estate.add_build(b_feature, b_build, branch="yb", must_be_gone=[a_feature])
        config = ForgeConfig(
            pipeline=PipelineConfig(),
            permissions=PermissionsConfig(
                filesystem=FilesystemPermissions(allowlist=[allow])
            ),
        )
        hold = tmp_path / "hold"

        def _envelope(payload: Any) -> bytes:
            return (
                MessageEnvelope(
                    source_id="cli-wrapper",
                    event_type=EventType.BUILD_QUEUED,
                    correlation_id=payload.correlation_id,
                    payload=payload.model_dump(mode="json"),
                )
                .model_dump_json()
                .encode()
            )

        async def _go(broker: str, x_url: str, y_url: str) -> dict[str, Any]:
            urls = {a_feature: x_url, b_feature: y_url}
            seen: dict[str, Any] = {"b_dispatched_with_a_alive": None}

            def _check() -> Any:
                return build_runner_stop_check(
                    sqlite_pool=pool,
                    default_url=x_url,
                    runner_url_for_feature=lambda f: urls[f],
                )

            stored_ack: dict[str, Any] = {}

            async def _dispatch(payload: Any, ack_callback: Any, **_: Any) -> None:
                if payload.feature_id == a_feature:
                    stored_ack["a"] = ack_callback
                    await _launch(x_url, a_feature, a_build, "xa")
                else:
                    seen["b_dispatched_with_a_alive"] = _marked_alive(a_build)
                    await _launch(y_url, b_feature, b_build, "yb")

            def _deps(guard: AckAfterStop) -> PipelineConsumerDeps:
                return PipelineConsumerDeps(
                    forge_config=config,
                    is_duplicate_terminal=_build_is_duplicate_terminal(pool),
                    dispatch_build=_dispatch,
                    publish_build_failed=AsyncMock(),
                    ack_guard=guard,
                )

            async def _forge(guard: AckAfterStop) -> tuple[Any, Any]:
                nc = await nats.connect(broker)
                psub = await nc.jetstream().pull_subscribe_bind(
                    durable="forge-test", stream="TESTBUILDS"
                )
                return nc, psub

            setup = await nats.connect(broker)
            js = setup.jetstream()
            await js.add_stream(name="TESTBUILDS", subjects=["test.build-queued.>"])
            await js.add_consumer(
                "TESTBUILDS",
                ConsumerConfig(
                    durable_name="forge-test",
                    ack_policy=AckPolicy.EXPLICIT,
                    deliver_policy=DeliverPolicy.ALL,
                    ack_wait=3,
                    max_ack_pending=1,  # one place between the two runners
                    max_deliver=-1,
                    filter_subject="test.build-queued.>",
                ),
            )
            await js.publish(f"test.build-queued.{a_feature}", _envelope(a_payload))
            await js.publish(f"test.build-queued.{b_feature}", _envelope(b_payload))
            await setup.close()

            respawner = _Respawner(a_build)
            try:
                # --- Forge, first process ----------------------------------
                guard1 = AckAfterStop(_check(), recheck_seconds=0.5)
                deps1 = _deps(guard1)
                nc1, psub1 = await _forge(guard1)
                first = await _fetch_some(psub1, 2.0)
                assert [_feature_of(m) for m in first] == [a_feature]
                await handle_message(first[0], deps1)
                await asyncio.to_thread(estate.pids, a_feature)
                hold.write_text(a_build)
                respawner.start()

                bridge = LifecycleBridge(
                    registry=BridgeRegistry(connection=cx),
                    build_stopper=build_runner_stop_check(
                        sqlite_pool=pool,
                        default_url=x_url,
                        runner_url_for_feature=lambda f: urls[f],
                        remember=True,
                    ),
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
                assert (await bridge.request_cancel(a_feature)).invoked
                cx.execute(
                    "UPDATE builds SET status = 'CANCELLED' WHERE build_id = ?",
                    (a_build,),
                )
                wireup = LifecycleBridgeWireup(
                    bridge=bridge,
                    translator=StreamEventTranslator(),
                    publisher=object(),
                    stream_source=object(),
                    ack_guard=guard1,
                )
                handle = AsyncMock()
                handle.ack = AsyncMock(side_effect=stored_ack["a"])
                await wireup._on_terminal(handle, a_feature, a_payload.correlation_id)
                assert guard1.held() == [(a_feature, a_payload.correlation_id)]
                # A is really redelivered (ack_wait 3 s) and still held by the
                # one checker; B is never delivered.
                while_held = await _fetch_some(psub1, 4.0)
                for msg in while_held:
                    await handle_message(msg, deps1)
                assert while_held and {_feature_of(m) for m in while_held} == {a_feature}
                assert guard1.held() == [(a_feature, a_payload.correlation_id)]
                handle.ack.assert_not_awaited()
                assert estate.started(b_feature) is None

                # --- the factory restarts while A is held ------------------
                await wireup.shutdown()
                await nc1.close()
                guard2 = AckAfterStop(_check(), recheck_seconds=0.5)
                deps2 = _deps(guard2)
                nc2, psub2 = await _forge(guard2)
                after_restart = await _fetch_some(psub2, 5.0)
                for msg in after_restart:
                    await handle_message(msg, deps2)
                assert after_restart and {_feature_of(m) for m in after_restart} == {a_feature}
                assert guard2.held() == [(a_feature, a_payload.correlation_id)]
                assert estate.started(b_feature) is None
                assert seen["b_dispatched_with_a_alive"] is None

                # --- A's processes finally go ------------------------------
                seen["held_spawns"] = respawner.spawned
                respawner.release()
                hold.unlink()
                deadline = asyncio.get_running_loop().time() + 60
                while seen["b_dispatched_with_a_alive"] is None:
                    assert asyncio.get_running_loop().time() < deadline, "B never came"
                    for msg in await _fetch_some(psub2, 1.0):
                        await handle_message(msg, deps2)
                await asyncio.to_thread(
                    wait_for,
                    lambda: estate.started(b_feature) is not None,
                    60,
                    "B never started after A's place was released",
                )
                await asyncio.to_thread(post_stop, y_url, b_build)
                await guard2.shutdown()
                await nc2.close()
                return seen
            finally:
                respawner.close()

        with throwaway_broker() as broker, real_runner(
            estate,
            "X",
            app="tests.integration.held_stop_app:app",
            extra_env={"FORGE_TEST_HOLD_FILE": str(hold)},
        ) as x, real_runner(estate, "Y") as y:
            seen = asyncio.run(_go(broker, x.url, y.url))
        cx.close()
        assert seen["held_spawns"] > 1
        assert seen["b_dispatched_with_a_alive"] == []
        assert estate.started(b_feature)["others_alive"] == {a_feature: []}

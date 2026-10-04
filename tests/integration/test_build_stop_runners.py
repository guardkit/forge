"""Stopping a build through real runners (design of 3 October 2026).

Real ``langgraph dev`` runners serve the real ``autobuild_runner`` graph; a
stand-in GuardKit (``FORGE_GUARDKIT_PATH``) starts real processes, including
one in a session of its own that ignores SIGTERM. A cancel is the ordinary
``runs.cancel(action="interrupt")``.

* The runner slot is the fence: with one job slot, a build queued behind an
  interrupted build starts only after every process the interrupted build
  owned is gone.
* A relaunch of a build still running in the runner stops the original first.
* Builds B and C in the same runner keep running and keep their files while A
  is stopped.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from typing import Any

import pytest
from langgraph_sdk import get_client

from tests.forge.build_stop_support import (
    kill_marked,
    kill_recorded,
    launch_message,
    make_estate,
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


async def _interrupt(launched: dict) -> None:
    """The ordinary cancel: interrupt the build's run."""
    await get_client(url=launched["url"]).runs.cancel(
        launched["thread_id"], launched["run_id"], action="interrupt"
    )


class TestTheRunnerSlotIsAFence:
    def test_a_queued_build_starts_only_after_the_stopped_builds_processes_are_gone(
        self, estate
    ):
        a_id, b_id = _build_id(), _build_id()
        estate.add_build("FEAT-RA", a_id, branch="ra")
        estate.add_build("FEAT-RB", b_id, branch="rb", must_be_gone=["FEAT-RA"])

        async def _go(url: str) -> None:
            a = await _launch(url, "FEAT-RA", a_id, "ra")
            await asyncio.to_thread(estate.pids, "FEAT-RA")
            b = await _launch(url, "FEAT-RB", b_id, "rb")
            await asyncio.sleep(2.0)
            assert estate.started("FEAT-RB") is None, "B started beside A in one slot"
            await _interrupt(a)
            await asyncio.to_thread(
                wait_for,
                lambda: estate.started("FEAT-RB") is not None,
                60,
                "B never started after A was stopped",
            )
            await _interrupt(b)

        with real_runner(estate, "slot") as runner:
            asyncio.run(_go(runner.url))
        # At the instant B started, none of A's processes was alive — not its
        # child, not its grandchild, not the SIGTERM-ignoring process in a
        # session of its own.
        assert estate.started("FEAT-RB")["others_alive"] == {"FEAT-RA": []}


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
            runs = {f: await _launch(url, f, b, f.lower()) for f, b in ids.items()}
            recorded = {f: await asyncio.to_thread(estate.pids, f) for f in ids}
            beats = {
                f: next((estate.root / "worktrees").glob(f"*{ids[f]}*/beat"))
                for f in ("FEAT-SB", "FEAT-SC")
            }
            await _interrupt(runs["FEAT-SA"])
            await asyncio.to_thread(
                wait_for,
                lambda: not any(proc_alive(p, s) for p, s in recorded["FEAT-SA"]),
                30,
                "A's processes were not stopped",
            )
            first = {f: float(p.read_text()) for f, p in beats.items()}
            await asyncio.sleep(1.0)
            later = {f: float(p.read_text()) for f, p in beats.items()}
            alive = {
                f: [p for p, s in recorded[f] if proc_alive(p, s)] for f in ids
            }
            for f in ("FEAT-SB", "FEAT-SC"):
                await _interrupt(runs[f])
            return {"first": first, "later": later, "alive": alive}

        # Three job slots: the three builds run side by side in one runner.
        with real_runner(estate, "three", jobs=3) as runner:
            seen = asyncio.run(_go(runner.url))
        assert seen["alive"]["FEAT-SA"] == []
        for f in ("FEAT-SB", "FEAT-SC"):
            assert len(seen["alive"][f]) == 3, f
            assert seen["later"][f] > seen["first"][f], f"{f} stopped writing"
        worktrees = estate.root / "worktrees"
        for b in ids.values():
            assert list(worktrees.glob(f"*{b}*")), "a worktree was removed"

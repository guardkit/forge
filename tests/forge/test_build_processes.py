"""Stopping everything a build owns — real processes (3 October 2026).

The design's "Cancellation" check: a real build child that starts a
grandchild and a separate-session process that ignores SIGTERM, all carrying
the build's owner marker; stopping the build removes every one of them and its
labelled fixture containers, and never touches a process whose number was
merely reused. The last two checks go through the real runner node (the
compiled ``autobuild_runner`` graph): cancelling it does not let the
cancellation through until the build's whole tree is gone, while two other
builds in the same runner keep running and keep their files.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage

from tests.forge.build_stop_support import (
    container_running,
    docker_available,
    kill_marked,
    kill_recorded,
    launch_message,
    make_estate,
    proc_alive,
    remove_test_containers,
    start_fixture_container,
    wait_for,
)

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="reads /proc"
)


def _build_id() -> str:
    return f"build-stoptest-{uuid.uuid4().hex[:10]}"


@pytest.fixture
def estate(tmp_path):
    made = make_estate(tmp_path)
    yield made
    kill_recorded(made)
    kill_marked([spec["build_id"] for k, spec in made.plan.items() if k != "_records"])


async def _spawn_fake_child(estate, feature_id: str) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        str(estate.guardkit),
        "autobuild",
        "feature",
        feature_id,
        cwd=str(estate.repo),
        stdout=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )


class TestStopReachesEverythingTheBuildOwns:
    def test_tree_and_separate_session_and_marker_are_all_stopped(self, estate):
        from forge import build_processes

        build_id = _build_id()
        estate.add_build("FEAT-A1", build_id, branch="a1")

        async def _go():
            child = await _spawn_fake_child(estate, "FEAT-A1")
            owned = build_processes.register(build_id, child.pid)
            try:
                recorded = await asyncio.to_thread(estate.pids, "FEAT-A1")
                assert all(proc_alive(p, s) for p, s in recorded)
                remaining = await build_processes.stop_owned(
                    build_id,
                    roots=[owned.root],
                    recorded=owned.recorded,
                    grace_seconds=0.5,
                )
                await child.wait()
                return recorded, remaining
            finally:
                build_processes.unregister(owned)

        recorded, remaining = asyncio.run(_go())
        assert remaining == []
        # The child, its grandchild and the SIGTERM-ignoring process in a
        # session of its own: every one is gone.
        assert [p for p, s in recorded if proc_alive(p, s)] == []

    def test_a_process_known_only_by_its_marker_is_stopped(self, estate):
        """Orphaned earlier, no parent link to any build: the marker finds it."""
        from forge import build_processes

        build_id = _build_id()
        script = (
            "import os, signal, sys, time\n"
            "if os.fork():\n"
            "    os._exit(0)\n"
            "os.setsid()\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
            "time.sleep(600)\n"
        )
        pid_file = estate.root / "orphan.pid"
        subprocess.run(
            [sys.executable, "-c", script, str(pid_file)],
            env={**os.environ, "GUARDKIT_RUN_OWNER": build_id},
            check=True,
        )
        wait_for(pid_file.exists, 10, "the orphan never started")
        orphan = int(pid_file.read_text())
        try:
            report = asyncio.run(build_processes.stop_build(build_id))
            assert report.stopped, report.as_json()
            assert not Path(f"/proc/{orphan}").exists() or (
                build_processes.snapshot_process(orphan) is None
            )
        finally:
            kill_marked([build_id])

    def test_a_reused_process_number_is_never_signalled(self, estate):
        """Recorded (pid, start time) pairs: a new process with an old number is safe."""
        from forge import build_processes

        bystander = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(600)"],
            start_new_session=True,
        )
        try:
            wait_for(
                lambda: build_processes.snapshot_process(bystander.pid) is not None,
                10,
                "bystander never started",
            )
            now = build_processes.snapshot_process(bystander.pid)
            stale = build_processes.OwnedProcess(
                pid=bystander.pid, starttime=now.starttime - 1
            )
            remaining = asyncio.run(
                build_processes.stop_owned(
                    _build_id(), roots=[stale], recorded={stale}, grace_seconds=0.2
                )
            )
            assert remaining == []
            assert bystander.poll() is None, "an unrelated process was signalled"
            # A process number that is not this process's own child is never
            # trusted as a build's root.
            owned = build_processes.register("build-not-mine", os.getppid())
            assert owned.root is None
            build_processes.unregister(owned)
        finally:
            bystander.kill()
            bystander.wait()


@pytest.mark.skipif(not docker_available(), reason="needs docker and busybox:1.36")
class TestFixtureContainers:
    def test_the_builds_labelled_containers_are_removed(self, estate):
        from forge import build_processes

        build_id = _build_id()
        other_id = _build_id()
        mine = start_fixture_container(build_id)
        theirs = start_fixture_container(other_id)
        try:
            before = asyncio.run(build_processes.still_running(build_id))
            assert not before.stopped and before.containers
            report = asyncio.run(build_processes.stop_build(build_id))
            assert report.stopped, report.as_json()
            assert not container_running(mine)
            assert container_running(theirs), "another build's fixture was touched"
        finally:
            remove_test_containers([build_id, other_id])

    def test_an_engine_that_cannot_answer_is_not_stopped(self, estate, monkeypatch):
        from forge import build_processes

        broken = estate.root / "broken-engine"
        broken.write_text("#!/bin/sh\necho 'engine down' >&2\nexit 1\n")
        broken.chmod(0o755)
        monkeypatch.setenv("FORGE_FIXTURE_ENGINE", str(broken))
        report = asyncio.run(build_processes.still_running(_build_id()))
        assert not report.confirmed
        assert not report.stopped


def _graph_input(feature_id: str, build_id: str, branch: str) -> dict:
    return {
        "messages": [HumanMessage(content=launch_message(feature_id, build_id, branch))]
    }


class TestTheRunnerNodeStopsTheWholeBuild:
    """The real node, in process: cancel A while B and C run beside it."""

    def test_cancel_waits_for_every_owned_process_and_spares_the_others(
        self, estate, monkeypatch
    ):
        for name, value in estate.env().items():
            monkeypatch.setenv(name, value)
        from forge.subagents.autobuild_runner import _build_runner_graph

        ids = {f: _build_id() for f in ("FEAT-A2", "FEAT-B2", "FEAT-C2")}
        for feature_id, build_id in ids.items():
            estate.add_build(feature_id, build_id, branch=feature_id.lower())

        async def _go():
            graph = _build_runner_graph()
            tasks = {
                f: asyncio.ensure_future(
                    graph.ainvoke(_graph_input(f, b, f.lower()))
                )
                for f, b in ids.items()
            }
            recorded = {
                f: await asyncio.to_thread(estate.pids, f) for f in ids
            }
            tasks["FEAT-A2"].cancel()
            with pytest.raises(asyncio.CancelledError):
                await tasks["FEAT-A2"]
            # The cancellation came through only now: everything A owned is
            # already gone at this instant, including the process in its own
            # session that ignores SIGTERM.
            a_alive = [p for p, s in recorded["FEAT-A2"] if proc_alive(p, s)]
            # B and C keep running and keep writing their own files.
            others_alive = {
                f: [p for p, s in recorded[f] if proc_alive(p, s)]
                for f in ("FEAT-B2", "FEAT-C2")
            }
            beats = {
                f: next((estate.root / "worktrees").glob(f"*{ids[f]}*/beat"), None)
                for f in ("FEAT-B2", "FEAT-C2")
            }
            first = {f: path.read_text() if path else None for f, path in beats.items()}
            await asyncio.sleep(1.0)
            later = {f: path.read_text() if path else None for f, path in beats.items()}
            for f in ("FEAT-B2", "FEAT-C2"):
                tasks[f].cancel()
                with pytest.raises(asyncio.CancelledError):
                    await tasks[f]
            return a_alive, others_alive, first, later, recorded

        a_alive, others_alive, first, later, recorded = asyncio.run(_go())
        assert a_alive == []
        for f in ("FEAT-B2", "FEAT-C2"):
            assert len(others_alive[f]) == 3, f"{f} lost processes: {others_alive[f]}"
            assert first[f] is not None and later[f] is not None, f"{f} has no files"
            assert float(later[f]) > float(first[f]), f"{f} stopped writing"
        # A's worktree is kept for its evidence.
        assert list((estate.root / "worktrees").glob(f"*{ids['FEAT-A2']}*"))

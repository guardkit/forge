"""Two or more builds in one runner, through the runner's REAL launch path.

3 October 2026, concurrent builds (design ``factory-concurrent-builds-design-
2026-10-03``, rows "Child settings", "Restart with paused builds (R4)", "Disk
reservation" and "Memory and integration under overlap"). Once a runner serves
several build runs at once (``--n-jobs-per-worker``), two builds share one
process: its free-space check, its view of which checkout is in use, and the
settings each child is handed.

Nothing here is a double for the launch itself. Each build goes through the
real ``_node_running_wave``: real git worktrees cut from a throwaway
repository, a real ``asyncio.create_subprocess_exec``, and a real child —
a small executable standing in for ``guardkit`` that writes down the
arguments, folder and settings it was started with, then waits (for the other
builds to start too, or for the test to let it go) before it exits. Only the
repository lookup is answered from a table, and the runner's own supervision is
off, as in the other launch tests.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import HumanMessage

from forge.subagents import autobuild_runner as runner_module

#: How long a stand-in child waits for its signal before giving up with exit 3.
#: A test that should not have launched a child at all fails on that exit
#: instead of hanging.
CHILD_PATIENCE_SECONDS = 20

CONCURRENCY_SETTINGS = {
    "GUARDKIT_MAX_PARALLEL_TASKS": "1",
    "GUARDKIT_WAVE_SAME_AREA": "serial",
    "GUARDKIT_PLAYER_MODEL_LIMITS": "a-model=2",
}

#: The project's own Coach choice, committed in the throwaway repository. The
#: factory adds no ``--coach-model``: this file keeps deciding.
PROJECT_CONFIG = "autobuild:\n  coach:\n    model: the-projects-own-coach\n"

STANDIN = textwrap.dedent(
    """\
    #!{python}
    import json, os, pathlib, sys, time
    out = pathlib.Path({out!r})
    feature = sys.argv[sys.argv.index("feature") + 1]
    config = pathlib.Path(".guardkit/config.yaml")
    record = {{
        "argv": sys.argv,
        "cwd": os.getcwd(),
        "env": dict(os.environ),
        "config": config.read_text() if config.exists() else None,
    }}
    if (out / "inner").exists():
        # guardkit's own feature-mode worktree, as a real build leaves it.
        import subprocess
        subprocess.run(
            ["git", "worktree", "add", ".guardkit/worktrees/" + feature,
             "-b", "autobuild/" + feature],
            capture_output=True,
        )
    (out / (feature + "." + (os.environ.get("GUARDKIT_RUN_OWNER") or "x") + ".owner")).write_text(os.getcwd())
    (out / (feature + ".started")).write_text(json.dumps(record))
    print("a stand-in for guardkit, building " + feature, flush=True)
    if (out / "exit-code").exists():
        sys.exit(int((out / "exit-code").read_text()))
    wanted = int((out / "together").read_text()) if (out / "together").exists() else 0
    deadline = time.time() + {patience}
    while time.time() < deadline:
        if (out / "release").exists():
            sys.exit(0)
        if wanted and len(list(out.glob("*.started"))) >= wanted:
            sys.exit(0)
        time.sleep(0.05)
    sys.exit(3)
    """
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _a_repository(where: Path, *branches: str) -> Path:
    where.mkdir(parents=True)
    _git(where, "init", "-b", "main")
    _git(where, "config", "user.email", "test@example.com")
    _git(where, "config", "user.name", "Test")
    (where / ".guardkit").mkdir()
    (where / ".guardkit" / "config.yaml").write_text(PROJECT_CONFIG)
    (where / "README").write_text("a throwaway project\n")
    _git(where, "add", "-A")
    _git(where, "commit", "-m", "init")
    for branch in branches:
        _git(where, "branch", branch)
    return where


class _Factory:
    """A throwaway runner's surroundings: repositories, the stand-in child
    and the folders the runner writes to."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp = tmp_path
        self.out = tmp_path / "children"
        self.out.mkdir()
        self.repos: dict[str, Path] = {}
        standin = tmp_path / "bin" / "guardkit"
        standin.parent.mkdir()
        standin.write_text(
            STANDIN.format(
                python=sys.executable,
                out=str(self.out),
                patience=CHILD_PATIENCE_SECONDS,
            )
        )
        standin.chmod(0o755)
        self.worktrees = tmp_path / "worktrees"
        self.receipts = tmp_path / "receipts"
        # A runner whose settings are its own: nothing from the developer's
        # machine (ledger, settings file) is read.
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("FORGE_CONFIG_PATH", raising=False)
        monkeypatch.setenv("FORGE_DB_PATH", str(tmp_path / "no-such-ledger.db"))
        monkeypatch.setenv("FORGE_GUARDKIT_PATH", str(standin))
        monkeypatch.setenv("FORGE_AUTOBUILD_WORKTREE_BASE", str(self.worktrees))
        monkeypatch.setenv("FORGE_RECEIPTS_DIR", str(self.receipts))
        monkeypatch.setenv("FORGE_AUTOBUILD_MIN_AVAILABLE_BYTES", "1")
        monkeypatch.setenv("FORGE_BUILD_MONITOR", "0")
        for name, value in CONCURRENCY_SETTINGS.items():
            monkeypatch.setenv(name, value)
        monkeypatch.delenv("GUARDKIT_RUN_OWNER", raising=False)
        monkeypatch.setattr(
            runner_module,
            "_resolve_repo_path",
            lambda payload: self.repos.get(str(payload.get("repo"))),
        )

    def repository(self, name: str, *branches: str) -> Path:
        self.repos[name] = _a_repository(self.tmp / "repos" / name, *branches)
        return self.repos[name]

    def together(self, count: int) -> None:
        """Each child waits until ``count`` children have started."""
        (self.out / "together").write_text(str(count))

    def release(self) -> None:
        (self.out / "release").write_text("go")

    def child(self, feature_id: str) -> dict[str, Any] | None:
        path = self.out / f"{feature_id}.started"
        return json.loads(path.read_text()) if path.exists() else None

    async def started(self, feature_id: str) -> dict[str, Any]:
        for _ in range(400):
            found = self.child(feature_id)
            if found is not None:
                return found
            await asyncio.sleep(0.05)
        raise AssertionError(f"the child for {feature_id} never started")


def _payload(build_id: str, feature_id: str, repo: str, **extra: Any) -> dict:
    return {
        "build_id": build_id,
        "feature_id": feature_id,
        "correlation_id": f"corr-{build_id}",
        "repo": repo,
        **extra,
    }


async def _build(payload: dict) -> dict[str, Any]:
    """Run one build through the runner's real launch node; return its
    final snapshot."""
    description = (
        "RUN_AUTOBUILD subagent=autobuild_runner payload=" + json.dumps(payload)
    )
    update = await runner_module._node_running_wave(
        {"messages": [HumanMessage(content=description)]}
    )
    return dict(update["async_tasks"][payload["feature_id"]])


@pytest.fixture
def factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Factory:
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    return _Factory(tmp_path, monkeypatch)


# ---------------------------------------------------------------------------
# Child settings
# ---------------------------------------------------------------------------


def test_the_child_is_handed_the_concurrency_settings_and_its_owner(
    factory: _Factory,
) -> None:
    repo = factory.repository("widget", "planning/a")
    factory.release()

    snapshot = asyncio.run(
        _build(_payload("build-FEAT-A-1", "FEAT-A", "widget", branch="planning/a"))
    )

    child = factory.child("FEAT-A")
    assert child is not None, f"no child recorded its owner: {snapshot}"
    for name, value in CONCURRENCY_SETTINGS.items():
        assert child["env"].get(name) == value, name
    assert child["env"]["GUARDKIT_RUN_OWNER"] == "build-FEAT-A-1"
    # The project's Coach is still the project's: no flag chooses it, and the
    # file that does reached the child as committed.
    assert not any(arg.startswith("--coach-model") for arg in child["argv"])
    assert child["config"] == PROJECT_CONFIG
    assert (repo / ".guardkit" / "config.yaml").read_text() == PROJECT_CONFIG


# ---------------------------------------------------------------------------
# Same repository: each build its own worktree; never a shared checkout beside
# another build (R4)
# ---------------------------------------------------------------------------


def test_two_builds_of_one_repository_run_in_their_own_worktrees(
    factory: _Factory,
) -> None:
    factory.repository("widget", "planning/a", "planning/b")
    factory.together(2)

    async def both() -> list[dict[str, Any]]:
        return await asyncio.gather(
            _build(_payload("build-FEAT-A-1", "FEAT-A", "widget", branch="planning/a")),
            _build(_payload("build-FEAT-B-1", "FEAT-B", "widget", branch="planning/b")),
        )

    asyncio.run(both())

    a = factory.child("FEAT-A")
    b = factory.child("FEAT-B")
    assert a is not None and b is not None
    assert a["cwd"] != b["cwd"]
    assert Path(a["cwd"]).parent == Path(b["cwd"]).parent == factory.worktrees.resolve()
    assert ["--base-branch", "planning/a"] == a["argv"][
        a["argv"].index("--base-branch") : a["argv"].index("--base-branch") + 2
    ]
    assert "planning/b" in b["argv"]
    # Distinct receipts folders, one per build, each with its own output.
    for build_id, feature_id in (("build-FEAT-A-1", "FEAT-A"), ("build-FEAT-B-1", "FEAT-B")):
        logs = list((factory.receipts / build_id).rglob("*.log"))
        assert logs, f"no receipts for {build_id}"
        assert all(feature_id in log.read_text() for log in logs)


def test_a_launch_with_no_branch_is_refused_beside_a_build_of_that_repository(
    factory: _Factory,
) -> None:
    """A launch that names no branch runs in the repository's shared checkout.
    Beside another build of the same repository that would be two builds in
    one folder, so it is refused, plainly, before anything is started."""
    factory.repository("widget", "planning/a")

    async def scenario() -> tuple[dict, dict]:
        first = asyncio.create_task(
            _build(_payload("build-FEAT-A-1", "FEAT-A", "widget", branch="planning/a"))
        )
        await factory.started("FEAT-A")
        try:
            second = await asyncio.wait_for(
                _build(_payload("build-FEAT-B-1", "FEAT-B", "widget")), 10
            )
        finally:
            factory.release()
        return await first, second

    first, second = asyncio.run(scenario())

    assert factory.child("FEAT-B") is None, "the shared checkout was used"
    assert second["lifecycle"] == "failed"
    assert "another build of this repository" in second["error_message"]
    assert first["lifecycle"] != "failed", first


def test_a_launch_with_no_branch_still_runs_when_nothing_else_is(
    factory: _Factory,
) -> None:
    """Alone, the shared-checkout launch is today's, and a finished build of
    the same repository no longer blocks it."""
    factory.repository("widget", "planning/a")
    factory.release()

    asyncio.run(
        _build(_payload("build-FEAT-A-1", "FEAT-A", "widget", branch="planning/a"))
    )
    snapshot = asyncio.run(_build(_payload("build-FEAT-B-1", "FEAT-B", "widget")))

    assert factory.child("FEAT-B") is not None, snapshot


# ---------------------------------------------------------------------------
# Disk: free space is reserved, not just observed
# ---------------------------------------------------------------------------


GIB = 1024**3


def _free_space(monkeypatch: pytest.MonkeyPatch, available: int) -> None:
    """The worktree filesystem reports ``available`` free bytes."""
    from forge.subagents import autobuild_worktree_lifecycle as lifecycle

    monkeypatch.setattr(
        lifecycle,
        "_capacity",
        lambda _path: {"available_bytes": available, "available_inodes": 10**6},
    )


def test_four_builds_start_with_room_for_four_reserves_above_the_floor(
    factory: _Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Floor 20 GiB, reserve 1 GiB, 28 GB free: four concurrent starts all
    proceed (the live window of 4 October fitted only one, because each
    running build reserved the whole floor)."""
    branches = [f"planning/{n}" for n in "abcd"]
    factory.repository("widget", *branches)
    factory.worktrees.mkdir()
    monkeypatch.setenv("FORGE_AUTOBUILD_MIN_AVAILABLE_BYTES", str(20 * GIB))
    monkeypatch.setenv("FORGE_AUTOBUILD_PER_BUILD_RESERVE_BYTES", str(GIB))
    _free_space(monkeypatch, 28_000_000_000)
    factory.together(4)

    async def scenario() -> list[dict]:
        return await asyncio.gather(
            *(
                _build(
                    _payload(
                        f"build-FEAT-{n.upper()}-1", f"FEAT-{n.upper()}", "widget",
                        branch=f"planning/{n}",
                    )
                )
                for n in "abcd"
            )
        )

    results = asyncio.run(scenario())
    for n in "ABCD":
        assert factory.child(f"FEAT-{n}") is not None, f"FEAT-{n} did not start"
    assert all(r["lifecycle"] != "failed" for r in results), results
    assert runner_module._DISK_RESERVATIONS == {}


def test_a_second_build_without_room_for_its_reserve_is_refused_plainly(
    factory: _Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Floor 20 GiB, reserve 1 GiB, 21 GiB free: the first starts (20 GiB
    stays free); the second would leave 19 GiB and is refused, with the
    numbers said; when the first ends its reserve goes and the next starts."""
    factory.repository("widget", "planning/a", "planning/b")
    factory.worktrees.mkdir()
    monkeypatch.setenv("FORGE_AUTOBUILD_MIN_AVAILABLE_BYTES", str(20 * GIB))
    monkeypatch.setenv("FORGE_AUTOBUILD_PER_BUILD_RESERVE_BYTES", str(GIB))
    _free_space(monkeypatch, 21 * GIB)

    async def scenario() -> tuple[dict, dict, dict]:
        first = asyncio.create_task(
            _build(_payload("build-FEAT-A-1", "FEAT-A", "widget", branch="planning/a"))
        )
        await factory.started("FEAT-A")
        held = dict(runner_module._DISK_RESERVATIONS)
        try:
            second = await asyncio.wait_for(
                _build(_payload("build-FEAT-B-1", "FEAT-B", "widget", branch="planning/b")),
                CHILD_PATIENCE_SECONDS + 10,
            )
        finally:
            factory.release()
        return await first, second, held

    first, second, held = asyncio.run(scenario())
    assert list(held.values()) == [GIB], held
    assert factory.child("FEAT-B") is None, "both builds started"
    assert second["lifecycle"] == "failed"
    reason = second["error_message"]
    assert "autobuild worktree capacity preflight refused the build" in reason
    assert f"other builds running now reserve {GIB}" in reason
    assert f"this build reserves {GIB}" in reason
    assert f"below the {20 * GIB} that must stay free" in reason
    assert first["lifecycle"] != "failed", first
    assert runner_module._DISK_RESERVATIONS == {}

    # The reserve went with the first build: the next one starts.
    third = asyncio.run(
        _build(_payload("build-FEAT-C-1", "FEAT-C", "widget", branch="planning/b"))
    )
    assert factory.child("FEAT-C") is not None, third
    assert runner_module._DISK_RESERVATIONS == {}


@pytest.mark.parametrize("ending", ["fails", "cancelled"])
def test_the_reserve_is_released_on_every_exit(
    factory: _Factory, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    factory.repository("widget", "planning/a")
    factory.worktrees.mkdir()

    async def scenario() -> None:
        if ending == "fails":
            (factory.out / "exit-code").write_text("5")
            result = await _build(
                _payload("build-FEAT-A-1", "FEAT-A", "widget", branch="planning/a")
            )
            assert result["lifecycle"] == "failed"
            return
        task = asyncio.create_task(
            _build(_payload("build-FEAT-A-1", "FEAT-A", "widget", branch="planning/a"))
        )
        await factory.started("FEAT-A")
        assert runner_module._DISK_RESERVATIONS, "no reserve held while running"
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert runner_module._DISK_RESERVATIONS == {}


def test_one_build_with_the_defaults_reserves_one_gibibyte(
    factory: _Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory.repository("widget", "planning/a")
    factory.worktrees.mkdir()
    monkeypatch.delenv("FORGE_AUTOBUILD_PER_BUILD_RESERVE_BYTES", raising=False)

    async def scenario() -> tuple[dict, dict]:
        task = asyncio.create_task(
            _build(_payload("build-FEAT-A-1", "FEAT-A", "widget", branch="planning/a"))
        )
        await factory.started("FEAT-A")
        held = dict(runner_module._DISK_RESERVATIONS)
        factory.release()
        return await task, held

    result, held = asyncio.run(scenario())
    assert result["lifecycle"] != "failed", result
    assert list(held.values()) == [GIB]
    assert runner_module._DISK_RESERVATIONS == {}


# ---------------------------------------------------------------------------
# Memory: each overlapping build carries its own project's memory
# ---------------------------------------------------------------------------


def test_overlapping_builds_of_two_projects_each_carry_their_own_memory(
    factory: _Factory,
) -> None:
    factory.repository("widget", "planning/a")
    factory.repository("gadget", "planning/b")
    factory.together(2)

    async def both() -> list[dict[str, Any]]:
        return await asyncio.gather(
            _build(
                _payload(
                    "build-FEAT-A-1",
                    "FEAT-A",
                    "widget",
                    branch="planning/a",
                    memory_project="widget_memory",
                )
            ),
            _build(
                _payload(
                    "build-FEAT-B-1",
                    "FEAT-B",
                    "gadget",
                    branch="planning/b",
                    memory_project="gadget_memory",
                )
            ),
        )

    asyncio.run(both())

    a = factory.child("FEAT-A")
    b = factory.child("FEAT-B")
    assert a is not None and b is not None, "both children did not start"
    assert a["env"]["GUARDKIT_MEMORY_PROJECT"] == "widget_memory"
    assert b["env"]["GUARDKIT_MEMORY_PROJECT"] == "gadget_memory"
    # They really did overlap: each waited for the other to start.
    assert os.path.exists(factory.out / "FEAT-A.started")


# ---------------------------------------------------------------------------
# The same feature twice in one runner: a running build is never swept
# ---------------------------------------------------------------------------


def test_a_second_build_of_a_feature_leaves_the_running_ones_worktree_alone(
    factory: _Factory,
) -> None:
    """Inside a sandbox there is no ledger, so the same-feature sweep cannot
    ask it whether an earlier build is still running. A build running in this
    runner is running, whatever the ledger says, and its worktree is never
    swept out from under it."""
    factory.repository("widget", "planning/a")
    (factory.out / "inner").write_text("yes")

    async def scenario() -> tuple[dict, dict]:
        first = asyncio.create_task(
            _build(_payload("build-FEAT-A-1", "FEAT-A", "widget", branch="planning/a"))
        )
        await factory.started("FEAT-A")
        (factory.out / "FEAT-A.started").unlink()
        second = asyncio.create_task(
            _build(_payload("build-FEAT-A-2", "FEAT-A", "widget", branch="planning/a"))
        )
        try:
            # Either the second build's child starts, or the build ends first
            # (refused); then the first build's worktree is looked at.
            owner = factory.out / "FEAT-A.build-FEAT-A-2.owner"
            for _ in range(400):
                if owner.exists() or second.done():
                    break
                await asyncio.sleep(0.05)
            first_still_there = (factory.worktrees / "build-FEAT-A-1").is_dir()
        finally:
            factory.release()
        return await first, await second, first_still_there

    first, second, first_still_there = asyncio.run(scenario())

    assert first_still_there, "the running build's worktree was swept"
    # The second build was not refused over it either: it went ahead.
    assert (factory.out / "FEAT-A.build-FEAT-A-2.owner").exists(), second
    assert first["lifecycle"] != "failed", first
    assert second["lifecycle"] != "failed", second


def test_sweeping_a_finished_builds_worktree_does_not_stall_other_builds(
    factory: _Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting a kept worktree and copying its evidence take as long as the
    tree is big, and every build in a runner shares one event loop. They run
    off it: while a deliberately slow removal runs for one build, a stand-in
    for another build keeps ticking."""
    factory.repository("widget", "planning/a")
    (factory.out / "inner").write_text("yes")
    (factory.out / "exit-code").write_text("1")
    first = asyncio.run(
        _build(_payload("build-FEAT-A-1", "FEAT-A", "widget", branch="planning/a"))
    )
    assert first["lifecycle"] == "failed"
    kept = factory.worktrees / "build-FEAT-A-1"
    assert kept.is_dir(), "the failed build's worktree should be kept"
    (factory.out / "exit-code").unlink()
    (factory.out / "inner").unlink()
    factory.release()

    import time as _time

    real_rmtree = shutil.rmtree

    def slow_rmtree(path: Any, *args: Any, **kwargs: Any) -> Any:
        if Path(str(path)) == kept:
            _time.sleep(1.0)
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(runner_module.shutil, "rmtree", slow_rmtree)

    async def scenario() -> tuple[dict, int]:
        ticks = 0
        stop = asyncio.Event()

        async def another_build() -> None:
            nonlocal ticks
            while not stop.is_set():
                ticks += 1
                await asyncio.sleep(0.01)

        beat = asyncio.create_task(another_build())
        try:
            second = await _build(
                _payload("build-FEAT-A-2", "FEAT-A", "widget", branch="planning/a")
            )
        finally:
            stop.set()
            await beat
        return second, ticks

    second, ticks = asyncio.run(scenario())

    assert not kept.exists(), f"the finished build's worktree was not swept: {second}"
    # A one-second removal on the loop would allow almost no ticks.
    assert ticks >= 30, ticks


def test_exporting_a_builds_receipts_does_not_stall_other_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same for the success path's receipt export."""
    import time as _time

    def slow_export(worktree: Path, build_id: str) -> Any:
        _time.sleep(1.0)
        return runner_module.ReceiptExport(ok=False)

    monkeypatch.setattr(runner_module, "_export_receipts", slow_export)

    async def scenario() -> int:
        ticks = 0
        stop = asyncio.Event()

        async def another_build() -> None:
            nonlocal ticks
            while not stop.is_set():
                ticks += 1
                await asyncio.sleep(0.01)

        beat = asyncio.create_task(another_build())
        try:
            kept = await runner_module._finalize_success_worktree(
                tmp_path, tmp_path / "wt", "build-FEAT-A-1"
            )
            assert kept is None
        finally:
            stop.set()
            await beat
        return ticks

    assert asyncio.run(scenario()) >= 30

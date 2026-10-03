"""Two or more builds in one runner, through the runner's REAL launch path.

3 October 2026, concurrent builds (design ``factory-concurrent-builds-design-
2026-10-03``, rows "Child settings", "Restart with paused builds (R4)" and "Memory and
integration under overlap"). Once a runner serves
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
    (out / (feature + ".started")).write_text(json.dumps(record))
    print("a stand-in for guardkit, building " + feature, flush=True)
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

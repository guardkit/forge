"""The plan writer is told whether the project already declares a coverage
command (20 October 2026).

The plan writer holds a plan that sets up the project's coverage command to
declaring it before the tasks GuardKit measures for coverage. That only
matters when the project has no coverage command yet, and the plan writer
cannot read the project's files, so Forge reads ``.guardkit/config.yaml`` and
sends ``declares_coverage_command`` on the target-repository descriptor:
true or false on every plan call, false when the file cannot be read.

These tests drive the real planning driver over a real scratch repository
(the caller level) and pin the reading on its own. Every name is made up.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.planning import coverage_declaration as cd
from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.states import PlanningState
from tests.forge.planning.test_driver_target_terminal import (
    CID,
    _git,
    _init_scratch_repo,
    _local_remotes,  # noqa: F401 — the autouse fixture: the remotes here are local folders
    _make_driver,
    _publish_to_origin,
    _queue,
    _recorded_runner,
    store,  # noqa: F401 — the fixture, re-exported for these tests
)


def _declare(repo: Path, toolchain: str) -> None:
    """Add a ``toolchain:`` block to the scratch project's own config and
    publish it, keeping what the harness already wrote there."""
    config = repo / ".guardkit" / "config.yaml"
    config.write_text(config.read_text(encoding="utf-8") + toolchain, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "the toolchain")
    _publish_to_origin(repo)


async def _descriptor_sent(store: SqlitePlanningRunStore, repo: Path, tmp_path: Path) -> dict:
    git = _recorded_runner(tmp_path / "wt", repo)
    _queue(store)
    h = _make_driver(store, git_runner=git, repo_path=str(repo))
    await h.driver.drive(CID)
    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    return h.ctx["counters"]["last_descriptor"]


@pytest.mark.asyncio
async def test_a_project_that_declares_coverage_is_said_to(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    repo = tmp_path / "app"
    _init_scratch_repo(repo)
    _declare(repo, 'toolchain:\n  test: "npm test"\n  coverage: "npm run coverage"\n')
    descriptor = await _descriptor_sent(store, repo, tmp_path)
    assert descriptor[cd.DESCRIPTOR_FIELD] is True


@pytest.mark.asyncio
async def test_a_component_coverage_command_counts(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    repo = tmp_path / "app"
    _init_scratch_repo(repo)
    _declare(
        repo,
        "toolchain:\n  test: \"npm test\"\n  components:\n    web:\n"
        "      cwd: web\n      test: \"npm test\"\n      coverage: \"npm run coverage\"\n",
    )
    descriptor = await _descriptor_sent(store, repo, tmp_path)
    assert descriptor[cd.DESCRIPTOR_FIELD] is True


@pytest.mark.asyncio
async def test_a_project_with_no_coverage_command_is_said_not_to(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    repo = tmp_path / "app"
    _init_scratch_repo(repo)
    _declare(repo, 'toolchain:\n  test: "npm test"\n  coverage: ""\n')
    descriptor = await _descriptor_sent(store, repo, tmp_path)
    assert descriptor[cd.DESCRIPTOR_FIELD] is False


@pytest.mark.asyncio
async def test_a_config_that_cannot_be_read_is_said_not_to(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """Unreadable is false: the plan writer's rule still runs, the safe side."""
    repo = tmp_path / "app"
    _init_scratch_repo(repo)
    git = _recorded_runner(tmp_path / "wt", repo)

    async def _cannot_read(*, repo_path: str, branch: str, file_path: str):
        if file_path == cd.CONFIG_PATH:
            raise OSError("the file could not be read")
        return await type(git).read_file_from_branch(
            git, repo_path=repo_path, branch=branch, file_path=file_path
        )

    git.read_file_from_branch = _cannot_read  # type: ignore[method-assign]
    _queue(store)
    h = _make_driver(store, git_runner=git, repo_path=str(repo))
    await h.driver.drive(CID)
    assert h.ctx["counters"]["last_descriptor"][cd.DESCRIPTOR_FIELD] is False


@pytest.mark.asyncio
async def test_a_branch_without_the_file_falls_back_to_the_repositorys_tip(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """The same two places the plan-commit stamping reads the file: the
    planning branch, else the repository's current tip."""
    repo = tmp_path / "app"
    _init_scratch_repo(repo)
    _declare(repo, 'toolchain:\n  coverage: "npm run coverage"\n')
    git = _recorded_runner(tmp_path / "wt", repo)
    asked: list[str] = []

    async def _branch_lacks_it(*, repo_path: str, branch: str, file_path: str):
        if file_path == cd.CONFIG_PATH:
            asked.append(branch)
            if branch != "HEAD":
                return None
        return await type(git).read_file_from_branch(
            git, repo_path=repo_path, branch=branch, file_path=file_path
        )

    git.read_file_from_branch = _branch_lacks_it  # type: ignore[method-assign]
    _queue(store)
    h = _make_driver(store, git_runner=git, repo_path=str(repo))
    await h.driver.drive(CID)
    assert h.ctx["counters"]["last_descriptor"][cd.DESCRIPTOR_FIELD] is True
    assert "HEAD" in asked and asked[0] != "HEAD"


@pytest.mark.parametrize(
    ("text", "declared"),
    [
        ('toolchain:\n  coverage: "make coverage"\n', True),
        ("toolchain:\n  components:\n    a:\n      coverage: x\n", True),
        ("toolchain:\n  components:\n    a:\n      test: x\n", False),
        ('toolchain:\n  coverage: "  "\n', False),
        ("toolchain:\n  coverage_timeout: 30\n", False),
        ("toolchain:\n  test: x\n", False),
        ("coverage: x\n", False),
        ("toolchain: [x]\n", False),
        ("not: [valid", False),
        ("", False),
        (None, False),
    ],
)
def test_the_reading_follows_guardkits(text: str | None, declared: bool) -> None:
    assert cd.declares_coverage_command(text) is declared

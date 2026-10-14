"""The planning commit leaves the factory's records out (design tests 12, 13).

Design: "Factory records never enter a project's commits" (14 October 2026).
The planning writer (:class:`forge.adapters.git.planning_runner.WorktreeGitRunner`)
commits whatever is in its worktree. In the sandbox the deploy helper runs
GuardKit's declared checks there just before the commit, and GuardKit's query
log is written relative to the folder a command runs in, so records can be
sitting beside the planning files. The sandbox's repair route sends its files
through this same writer.

Each case runs the real writer against a real repository in a temporary
folder. The pre-commit hook stands in for whatever ran in the worktree.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from forge.adapters.git.planning_runner import WorktreeGitRunner
from forge.planning.handoff import PreCommitResult

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

BRANCH = "planning/records-test-001"
FILES = {"docs/features/F/spec.md": "# the spec\n"}


def _git(where: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
        cwd=str(where),
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "target-repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("# Target\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


@pytest.fixture()
def runner(tmp_path: Path) -> WorktreeGitRunner:
    return WorktreeGitRunner(worktrees_root=tmp_path / "worktrees")


def _hook(files: dict[str, str], *, force_add: tuple[str, ...] = ()):
    async def hook(worktree: Path) -> PreCommitResult:
        for path, text in files.items():
            where = worktree / path
            where.parent.mkdir(parents=True, exist_ok=True)
            where.write_text(text, encoding="utf-8")
        for path in force_add:
            _git(worktree, "add", "-f", path)
        return PreCommitResult(ok=True)

    return hook


def _files_in(repo: Path, commit: str) -> set[str]:
    return {
        p for p in _git(repo, "show", "--name-only", "--format=", commit).splitlines() if p
    }


@pytest.mark.asyncio
async def test_12_the_planning_commit_holds_only_the_written_file(
    repo: Path, runner: WorktreeGitRunner
) -> None:
    result = await runner.prepare_branch_and_write_tree(
        str(repo),
        BRANCH,
        FILES,
        "planning: the spec",
        pre_commit=_hook(
            {
                ".guardkit/memory-query-log.jsonl": '{"query": "x"}\n',
                ".guardkit/autobuild/x.json": "{}\n",
            }
        ),
    )

    assert result.status == "success", result
    assert _files_in(repo, str(result.sha)) == set(FILES)


@pytest.mark.parametrize(
    "stray, force_add",
    [
        ({"conversation_history/session.md": "what the model said\n"}, ()),
        ({".guardkit/autobuild/pre.json": "{}\n"}, (".guardkit/autobuild/pre.json",)),
    ],
    ids=["only a top-level transcript", "only a pre-staged record"],
)
@pytest.mark.asyncio
async def test_13_nothing_but_records_makes_no_commit_and_succeeds(
    repo: Path, runner: WorktreeGitRunner, stray: dict[str, str], force_add: tuple[str, ...]
) -> None:
    first = await runner.prepare_branch_and_write_tree(
        str(repo), BRANCH, FILES, "planning: the spec"
    )
    assert first.status == "success", first

    again = await runner.prepare_branch_and_write_tree(
        str(repo),
        BRANCH,
        FILES,
        "planning: the spec again",
        pre_commit=_hook(stray, force_add=force_add),
    )

    assert again.status == "success", again
    assert again.sha == first.sha
    assert _git(repo, "rev-parse", BRANCH) == first.sha

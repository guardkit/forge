"""Tests for the production planning GitRunner (TASK-MP-012).

Exercises :class:`WorktreeGitRunner` against a REAL git repository in
tmp_path — branch creation from HEAD, file commit, primary-checkout
isolation (ASSUM-006), idempotent re-execution (RT-08), and the
never-raise adapter boundary (ADR-ARCH-025).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from forge.adapters.git.planning_runner import WorktreeGitRunner

CID = "runner-test-001"
BRANCH = f"planning/{CID}"
FILE_PATH = f"feature_spec_inputs/{CID}.md"
CONTENT = "# Feature Spec Input\n\nA widget that widgets.\n"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repo with one commit on the default branch."""
    repo = tmp_path / "target-repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.email", "test@forge.local")
    _git(repo, "config", "user.name", "Forge Test")
    (repo / "README.md").write_text("# Target\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial")
    return repo


@pytest.fixture
def runner(tmp_path: Path) -> WorktreeGitRunner:
    return WorktreeGitRunner(worktrees_root=tmp_path / "worktrees")


class TestPrepareBranchAndWrite:
    @pytest.mark.asyncio
    async def test_creates_branch_and_commits_file(
        self, repo: Path, runner: WorktreeGitRunner
    ) -> None:
        result = await runner.prepare_branch_and_write(
            str(repo), BRANCH, FILE_PATH, CONTENT
        )

        assert result.status == "success"
        assert result.sha

        # Branch exists and carries exactly the file content
        assert _git(repo, "rev-parse", f"refs/heads/{BRANCH}") == result.sha
        assert _git(repo, "show", f"{BRANCH}:{FILE_PATH}") == CONTENT.rstrip("\n")

    @pytest.mark.asyncio
    async def test_primary_checkout_is_never_touched(
        self, repo: Path, runner: WorktreeGitRunner
    ) -> None:
        """ASSUM-006: mutations happen in an ephemeral worktree only."""
        head_before = _git(repo, "rev-parse", "HEAD")
        branch_before = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")

        await runner.prepare_branch_and_write(str(repo), BRANCH, FILE_PATH, CONTENT)

        assert _git(repo, "rev-parse", "HEAD") == head_before
        assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == branch_before
        assert not (repo / FILE_PATH).exists(), (
            "planning file must not appear in the primary working copy"
        )
        assert _git(repo, "status", "--porcelain") == ""

    @pytest.mark.asyncio
    async def test_reexecution_with_identical_content_is_idempotent(
        self, repo: Path, runner: WorktreeGitRunner
    ) -> None:
        """RT-08: no duplicate commit when branch+file already match."""
        first = await runner.prepare_branch_and_write(
            str(repo), BRANCH, FILE_PATH, CONTENT
        )
        second = await runner.prepare_branch_and_write(
            str(repo), BRANCH, FILE_PATH, CONTENT
        )

        assert second.status == "success"
        assert second.sha == first.sha, "idempotent re-run must not mint a commit"
        commits_on_branch = _git(repo, "rev-list", "--count", BRANCH)
        assert commits_on_branch == "2"  # initial + one handoff commit

    @pytest.mark.asyncio
    async def test_reexecution_with_changed_content_commits_update(
        self, repo: Path, runner: WorktreeGitRunner
    ) -> None:
        first = await runner.prepare_branch_and_write(
            str(repo), BRANCH, FILE_PATH, CONTENT
        )
        updated = CONTENT + "\nRevised.\n"
        second = await runner.prepare_branch_and_write(
            str(repo), BRANCH, FILE_PATH, updated
        )

        assert second.status == "success"
        assert second.sha != first.sha
        assert _git(repo, "show", f"{BRANCH}:{FILE_PATH}") == updated.rstrip("\n")

    @pytest.mark.asyncio
    async def test_worktree_is_cleaned_up(self, repo: Path, tmp_path: Path) -> None:
        worktrees_root = tmp_path / "worktrees"
        runner = WorktreeGitRunner(worktrees_root=worktrees_root)

        await runner.prepare_branch_and_write(str(repo), BRANCH, FILE_PATH, CONTENT)

        leftovers = list(worktrees_root.iterdir()) if worktrees_root.exists() else []
        assert leftovers == [], f"worktree not cleaned up: {leftovers}"

    @pytest.mark.asyncio
    async def test_missing_repo_returns_failed_without_raising(
        self, runner: WorktreeGitRunner, tmp_path: Path
    ) -> None:
        result = await runner.prepare_branch_and_write(
            str(tmp_path / "does-not-exist"), BRANCH, FILE_PATH, CONTENT
        )
        assert result.status == "failed"
        assert "not a directory" in (result.stderr or "")

    @pytest.mark.asyncio
    async def test_path_escape_is_refused(
        self, repo: Path, runner: WorktreeGitRunner
    ) -> None:
        result = await runner.prepare_branch_and_write(
            str(repo), BRANCH, "../outside.md", CONTENT
        )
        assert result.status == "failed"
        assert "escapes" in (result.stderr or "")


class TestCheckoutCollisionGuard:
    """TASK-MP-013: --force re-attach must refuse a live operator checkout."""

    @pytest.mark.asyncio
    async def test_rehandoff_refused_when_branch_checked_out_elsewhere(
        self, repo: Path, runner: WorktreeGitRunner, tmp_path: Path
    ) -> None:
        """Review scenario: operator worktree on the handoff branch."""
        first = await runner.prepare_branch_and_write(
            str(repo), BRANCH, FILE_PATH, CONTENT
        )
        assert first.status == "success"

        # Simulate a human operator with the handoff branch checked out.
        operator_wt = tmp_path / "operator-worktree"
        _git(repo, "worktree", "add", str(operator_wt), BRANCH)
        file_before = (operator_wt / FILE_PATH).read_text(encoding="utf-8")
        status_before = _git(operator_wt, "status", "--porcelain")

        updated = CONTENT + "\nRevised behind the operator's back.\n"
        result = await runner.prepare_branch_and_write(
            str(repo), BRANCH, FILE_PATH, updated
        )

        assert result.status == "failed"
        assert "handoff-branch-checked-out" in (result.stderr or "")
        assert BRANCH in (result.stderr or "")
        assert str(operator_wt) in (result.stderr or "")

        # Branch tip untouched — no commit was minted under the checkout.
        assert _git(repo, "rev-parse", f"refs/heads/{BRANCH}") == first.sha

        # Operator's worktree byte-identical: no phantom staged modification.
        assert _git(operator_wt, "status", "--porcelain") == status_before == ""
        assert (operator_wt / FILE_PATH).read_text(encoding="utf-8") == file_before

    @pytest.mark.asyncio
    async def test_idempotent_rehandoff_succeeds_despite_checkout(
        self, repo: Path, runner: WorktreeGitRunner, tmp_path: Path
    ) -> None:
        """RT-08: identical content = zero mutations = never blocked."""
        first = await runner.prepare_branch_and_write(
            str(repo), BRANCH, FILE_PATH, CONTENT
        )
        operator_wt = tmp_path / "operator-worktree"
        _git(repo, "worktree", "add", str(operator_wt), BRANCH)

        second = await runner.prepare_branch_and_write(
            str(repo), BRANCH, FILE_PATH, CONTENT
        )

        assert second.status == "success"
        assert second.sha == first.sha
        assert _git(operator_wt, "status", "--porcelain") == ""

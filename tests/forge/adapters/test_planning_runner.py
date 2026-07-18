"""Tests for the production planning GitRunner (TASK-MP-012).

Exercises :class:`WorktreeGitRunner` against a REAL git repository in
tmp_path — branch creation from HEAD, file commit, primary-checkout
isolation (ASSUM-006), idempotent re-execution (RT-08), and the
never-raise adapter boundary (ADR-ARCH-025).
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

import pytest

from forge.adapters.git.operations import ExecuteResult
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


class TestPrepareBranchAndWriteTree:
    """Lane B / Phase E1 (B2): the multi-file + pre-commit-hook write."""

    _FILES = {
        "features/stats/stats.feature": "Feature: stats\n",
        "features/stats/stats_assumptions.yaml": "assumptions: []\n",
        "features/stats/stats_summary.md": "# summary\n",
    }

    @pytest.mark.asyncio
    async def test_commits_a_multi_file_tree(
        self, repo: Path, runner: WorktreeGitRunner
    ) -> None:
        result = await runner.prepare_branch_and_write_tree(
            str(repo), BRANCH, self._FILES, "planning: spec"
        )
        assert result.status == "success"
        for rel, content in self._FILES.items():
            assert _git(repo, "show", f"{BRANCH}:{rel}") == content.rstrip("\n")

    @pytest.mark.asyncio
    async def test_second_leg_adds_to_the_same_branch(
        self, repo: Path, runner: WorktreeGitRunner
    ) -> None:
        await runner.prepare_branch_and_write_tree(
            str(repo), BRANCH, self._FILES, "planning: spec"
        )
        plan = {"features/stats/FEAT-BEEF.yaml": "id: FEAT-BEEF\n"}
        result = await runner.prepare_branch_and_write_tree(
            str(repo), BRANCH, plan, "planning: plan"
        )
        assert result.status == "success"
        # Both the spec triple and the plan tree are on the branch.
        assert _git(repo, "show", f"{BRANCH}:features/stats/stats.feature")
        assert _git(repo, "show", f"{BRANCH}:features/stats/FEAT-BEEF.yaml")

    @pytest.mark.asyncio
    async def test_pre_commit_hook_can_mutate_before_commit(
        self, repo: Path, runner: WorktreeGitRunner
    ) -> None:
        from forge.planning.handoff import PreCommitResult

        async def _hook(worktree: Path) -> PreCommitResult:
            # Rewrite the .feature in place (the normalizer's collapse behaviour).
            (worktree / "features/stats/stats.feature").write_text(
                "Feature: normalized\n", encoding="utf-8"
            )
            return PreCommitResult(ok=True)

        result = await runner.prepare_branch_and_write_tree(
            str(repo), BRANCH, self._FILES, "planning: spec", pre_commit=_hook
        )
        assert result.status == "success"
        assert (
            _git(repo, "show", f"{BRANCH}:features/stats/stats.feature")
            == "Feature: normalized"
        )

    @pytest.mark.asyncio
    async def test_red_pre_commit_hook_aborts_the_commit(
        self, repo: Path, runner: WorktreeGitRunner
    ) -> None:
        from forge.planning.handoff import PreCommitResult

        async def _hook(worktree: Path) -> PreCommitResult:
            return PreCommitResult(ok=False, detail="unparseable")

        result = await runner.prepare_branch_and_write_tree(
            str(repo), BRANCH, self._FILES, "planning: spec", pre_commit=_hook
        )
        assert result.status == "failed"
        assert "unparseable" in (result.stderr or "")
        # Zero commit: the branch never advanced past HEAD (no such path).
        show = subprocess.run(
            ["git", "show", f"{BRANCH}:features/stats/stats.feature"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        assert show.returncode != 0

    @pytest.mark.asyncio
    async def test_rejects_path_escaping_the_worktree(
        self, repo: Path, runner: WorktreeGitRunner
    ) -> None:
        result = await runner.prepare_branch_and_write_tree(
            str(repo), BRANCH, {"../escape.txt": "nope"}, "planning: bad"
        )
        assert result.status == "failed"
        assert "escapes the worktree" in (result.stderr or "")

    @pytest.mark.asyncio
    async def test_empty_files_is_a_failure(
        self, repo: Path, runner: WorktreeGitRunner
    ) -> None:
        result = await runner.prepare_branch_and_write_tree(
            str(repo), BRANCH, {}, "planning: empty"
        )
        assert result.status == "failed"

    @pytest.mark.asyncio
    async def test_idempotent_identical_tree_no_op(
        self, repo: Path, runner: WorktreeGitRunner
    ) -> None:
        first = await runner.prepare_branch_and_write_tree(
            str(repo), BRANCH, self._FILES, "planning: spec"
        )
        second = await runner.prepare_branch_and_write_tree(
            str(repo), BRANCH, self._FILES, "planning: spec"
        )
        assert second.status == "success"
        assert second.sha == first.sha


class TestTimeoutRobustness:
    """The 2026-07-18 silent-hang fix: a wedged git op or pre-commit hook
    must surface as a loud, quick, non-raising ``status="failed"`` — never
    an unbounded silent wait."""

    _FILES = {"features/stats/stats.feature": "Feature: stats\n"}

    @pytest.mark.asyncio
    async def test_wedged_git_op_returns_failed_quickly_without_raising(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """A subprocess primitive whose git call never completes on its own:
        the runner's per-op timeout ends it and maps it to a failed result."""

        async def _never_completes(
            *,
            command: object,
            cwd: str | None = None,
            timeout: float | None = None,
        ) -> ExecuteResult:
            # Honour the timeout contract the real _default_execute enforces:
            # the operation never finishes on its own; only the timeout ends it.
            try:
                await asyncio.wait_for(asyncio.Event().wait(), timeout=timeout)
            except asyncio.TimeoutError:
                return ExecuteResult(
                    exit_code=-1,
                    stdout="",
                    stderr=(
                        f"git-runner timeout: command timed out after "
                        f"{timeout}s: {command!r}"
                    ),
                )
            raise AssertionError("timeout was expected to fire")

        runner = WorktreeGitRunner(
            worktrees_root=tmp_path / "worktrees",
            execute=_never_completes,
            op_timeout_s=0.1,
        )

        started = time.monotonic()
        result = await runner.prepare_branch_and_write_tree(
            str(repo), BRANCH, self._FILES, "planning: spec"
        )
        elapsed = time.monotonic() - started

        assert result.status == "failed"
        assert "timeout" in (result.stderr or "").lower()
        assert elapsed < 3.0  # ended by the tiny op timeout, not hung

    @pytest.mark.asyncio
    async def test_wedged_pre_commit_hook_fails_loud_and_leaves_branch_unmutated(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """A pre-commit hook that never returns: bounded by hook_timeout_s,
        the worktree is cleaned up and the branch carries no handoff commit."""
        worktrees_root = tmp_path / "worktrees"
        runner = WorktreeGitRunner(
            worktrees_root=worktrees_root,
            hook_timeout_s=0.2,
        )

        base_head = _git(repo, "rev-parse", "HEAD")

        async def _hangs_forever(worktree: Path):
            await asyncio.Event().wait()  # never returns

        started = time.monotonic()
        result = await runner.prepare_branch_and_write_tree(
            str(repo),
            BRANCH,
            self._FILES,
            "planning: spec",
            pre_commit=_hangs_forever,
        )
        elapsed = time.monotonic() - started

        # Loud, quick failure — never raises, never hangs.
        assert result.status == "failed"
        assert "timed out" in (result.stderr or "")
        assert elapsed < 3.0

        # Worktree cleaned up (best-effort path ran on the timeout branch).
        leftovers = (
            list(worktrees_root.iterdir()) if worktrees_root.exists() else []
        )
        assert leftovers == [], f"worktree not cleaned up: {leftovers}"

        # Branch un-mutated: no handoff commit landed. The file is absent on
        # the branch and the tip still points at the base HEAD.
        show = subprocess.run(
            ["git", "show", f"{BRANCH}:features/stats/stats.feature"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        assert show.returncode != 0, "no file should have been committed"
        if BRANCH in _git(repo, "branch", "--list", BRANCH):
            assert _git(repo, "rev-parse", f"refs/heads/{BRANCH}") == base_head

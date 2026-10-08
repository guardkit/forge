"""A planning worktree survives a prune run from another container.

Window 12, 8 October 2026: the sandbox helper made a planning run's worktree
in its own temporary folder. A build started in the build runner, which shares
the repository's ``.git`` but cannot see the helper's temporary folder, and
its preflight ``git worktree prune`` treated the worktree as missing and
deleted its registration. The helper's next git command in that worktree
failed with "not a git repository", and its cleanup failed too.

These tests use real git. "Another container cannot see the folder" is
simulated by moving the worktree folder aside, running ``git worktree prune``
from the main repository, and moving it back — exactly what the runner's
prune saw.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from forge.adapters.git.operations import ExecuteResult
from forge.adapters.git.planning_runner import (
    PLANNING_LOCK_PREFIX,
    STALE_LOCK_AFTER_S,
    WorktreeGitRunner,
    planning_lock_reason,
)
from forge.planning.handoff import PreCommitResult

RUN = "lock-test-0001"
BRANCH = f"planning/{RUN}"
FILES = {"features/sort/sort.feature": "Feature: sort\n"}


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=check
    )


def _registered(repo: Path) -> list[str]:
    out = _git(repo, "worktree", "list", "--porcelain").stdout
    return [
        line[len("worktree ") :]
        for line in out.splitlines()
        if line.startswith("worktree ")
    ][1:]  # the main working copy is always first


def _prune_as_another_container(repo: Path, worktree: Path) -> None:
    """Run ``git worktree prune`` while ``worktree`` is out of sight."""
    hidden = worktree.with_name(worktree.name + ".out-of-sight")
    worktree.rename(hidden)
    try:
        _git(repo, "worktree", "prune")
    finally:
        hidden.rename(worktree)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "target-repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.email", "test@forge.local")
    _git(repo, "config", "user.name", "Forge Test")
    (repo / "README.md").write_text("# Target\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial")
    return repo


class TestThePruneThatKilledThePlan:
    def test_an_unlocked_worktree_out_of_sight_is_pruned_and_broken(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """Proves the simulation bites: this is what happened in window 12."""
        worktree = tmp_path / "helper-tmp" / "planning-unlocked"
        _git(repo, "worktree", "add", "-b", BRANCH, str(worktree))

        _prune_as_another_container(repo, worktree)

        assert str(worktree) not in _registered(repo)
        status = _git(worktree, "status", check=False)
        assert status.returncode != 0
        assert "not a git repository" in status.stderr

    def test_a_locked_worktree_out_of_sight_survives_the_prune(
        self, repo: Path, tmp_path: Path
    ) -> None:
        worktree = tmp_path / "helper-tmp" / "planning-locked"
        _git(
            repo, "worktree", "add", "--lock", "--reason",
            planning_lock_reason(BRANCH), "-b", BRANCH, str(worktree),
        )

        _prune_as_another_container(repo, worktree)

        assert str(worktree) in _registered(repo)
        assert _git(worktree, "status").returncode == 0
        # Unlock, then remove cleanly.
        _git(repo, "worktree", "unlock", str(worktree))
        _git(repo, "worktree", "remove", "--force", str(worktree))
        assert _registered(repo) == []
        assert not worktree.exists()


class TestThePlanningWriterLocksItsWorktree:
    @pytest.mark.asyncio
    async def test_a_prune_mid_write_no_longer_kills_the_write(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """The window 12 sequence through the real writer: a prune from
        another container lands while the pre-commit checks are running."""
        runner = WorktreeGitRunner(worktrees_root=tmp_path / "helper-tmp")
        seen: dict[str, str] = {}

        async def _checks_while_a_build_starts(worktree: Path) -> PreCommitResult:
            porcelain = _git(repo, "worktree", "list", "--porcelain").stdout
            seen["porcelain"] = porcelain
            _prune_as_another_container(repo, worktree)
            return PreCommitResult(ok=True)

        result = await runner.prepare_branch_and_write_tree(
            str(repo), BRANCH, FILES, "planning: pass bars",
            pre_commit=_checks_while_a_build_starts,
        )

        assert result.status == "success", result.stderr
        assert _git(repo, "show", f"{BRANCH}:features/sort/sort.feature").stdout
        assert f"locked {PLANNING_LOCK_PREFIX}{RUN} in use since " in seen["porcelain"]
        # Unlocked and removed afterwards: nothing registered, nothing left.
        assert _registered(repo) == []
        assert list((tmp_path / "helper-tmp").iterdir()) == []

    @pytest.mark.asyncio
    async def test_without_the_lock_the_same_sequence_fails(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same sequence with the lock taken away fails the way window 12
        did, so the test above is not passing by accident."""
        monkeypatch.setattr(
            WorktreeGitRunner, "_lock_args", staticmethod(lambda branch: [])
        )
        runner = WorktreeGitRunner(worktrees_root=tmp_path / "helper-tmp")

        async def _checks_while_a_build_starts(worktree: Path) -> PreCommitResult:
            _prune_as_another_container(repo, worktree)
            return PreCommitResult(ok=True)

        result = await runner.prepare_branch_and_write_tree(
            str(repo), BRANCH, FILES, "planning: pass bars",
            pre_commit=_checks_while_a_build_starts,
        )

        assert result.status == "failed"
        assert "not a git repository" in (result.stderr or "")

    @pytest.mark.asyncio
    async def test_the_single_file_write_is_locked_too(
        self, repo: Path, tmp_path: Path
    ) -> None:
        seen: list[list[str]] = []
        runner = WorktreeGitRunner(worktrees_root=tmp_path / "helper-tmp")
        real = runner._execute

        async def _recording(*, command, cwd=None, timeout=None):
            seen.append(list(command))
            return await real(command=command, cwd=cwd, timeout=timeout)

        runner._execute = _recording
        result = await runner.prepare_branch_and_write(
            str(repo), BRANCH, "notes/input.md", "# input\n"
        )

        assert result.status == "success"
        adds = [c for c in seen if c[1:3] == ["worktree", "add"]]
        assert adds and all("--lock" in c for c in adds)
        assert _registered(repo) == []


class TestCleanupWhenTheRegistrationIsAlreadyGone:
    @pytest.mark.asyncio
    async def test_the_folder_is_removed_and_it_is_said_plainly(
        self, repo: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        root = tmp_path / "helper-tmp"
        worktree = root / "planning-gone"
        _git(repo, "worktree", "add", "-b", BRANCH, str(worktree))
        _prune_as_another_container(repo, worktree)
        assert worktree.exists() and str(worktree) not in _registered(repo)

        runner = WorktreeGitRunner(worktrees_root=root)
        with caplog.at_level(logging.WARNING):
            await runner._cleanup_worktree(repo, worktree)

        assert not worktree.exists()
        assert "no longer registered" in caplog.text
        assert "non-zero exit" not in caplog.text


class TestALockLeftByAProcessThatDied:
    @pytest.mark.asyncio
    async def test_an_old_lock_with_its_folder_gone_is_cleared_and_a_fresh_one_is_not(
        self, repo: Path, tmp_path: Path
    ) -> None:
        root = tmp_path / "helper-tmp"
        old = root / "planning-old"
        fresh = root / "planning-fresh"
        long_ago = datetime.now(timezone.utc) - timedelta(
            seconds=STALE_LOCK_AFTER_S + 60
        )
        _git(
            repo, "worktree", "add", "--lock", "--reason",
            planning_lock_reason("planning/old", now=long_ago),
            "-b", "planning/old", str(old),
        )
        # A fresh lock whose folder is missing HERE is another container's
        # live write; it must be left alone.
        _git(
            repo, "worktree", "add", "--lock", "--reason",
            planning_lock_reason("planning/fresh"),
            "-b", "planning/fresh", str(fresh),
        )
        shutil.rmtree(old)
        shutil.rmtree(fresh)

        runner = WorktreeGitRunner(worktrees_root=root)
        result = await runner.prepare_branch_and_write_tree(
            str(repo), BRANCH, FILES, "planning: spec"
        )

        assert result.status == "success"
        assert _registered(repo) == [str(fresh)]


class TestATemporaryFolderReachedThroughASymlink:
    """git records a worktree's real path; macOS's temporary folder sits
    behind ``/var -> /private/var``. Comparisons must use real paths."""

    @pytest.fixture
    def linked_root(self, tmp_path: Path) -> Path:
        real = tmp_path / "real-tmp"
        real.mkdir()
        link = tmp_path / "linked-tmp"
        link.symlink_to(real, target_is_directory=True)
        return link / "helper-tmp"

    @pytest.mark.asyncio
    async def test_a_live_worktree_is_seen_as_registered(
        self, repo: Path, linked_root: Path
    ) -> None:
        worktree = linked_root / "planning-live"
        _git(repo, "worktree", "add", "-b", BRANCH, str(worktree))

        runner = WorktreeGitRunner(worktrees_root=linked_root)
        assert await runner._is_registered(repo, worktree) is True

    @pytest.mark.asyncio
    async def test_an_old_lock_with_its_folder_gone_is_still_cleared(
        self, repo: Path, linked_root: Path
    ) -> None:
        old = linked_root / "planning-old"
        long_ago = datetime.now(timezone.utc) - timedelta(
            seconds=STALE_LOCK_AFTER_S + 60
        )
        _git(
            repo, "worktree", "add", "--lock", "--reason",
            planning_lock_reason("planning/old", now=long_ago),
            "-b", "planning/old", str(old),
        )
        shutil.rmtree(old.resolve())

        runner = WorktreeGitRunner(worktrees_root=linked_root)
        result = await runner.prepare_branch_and_write_tree(
            str(repo), BRANCH, FILES, "planning: spec"
        )

        assert result.status == "success"
        assert _registered(repo) == []


class TestAnAddThatTimesOutAfterMakingTheWorktree:
    @pytest.mark.asyncio
    async def test_no_lock_folder_or_registration_outlives_it(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """The add really happens (folder, registration and lock), then the
        caller is told it timed out, as the 180-second limit would."""
        root = tmp_path / "helper-tmp"
        runner = WorktreeGitRunner(worktrees_root=root)
        real = runner._execute

        async def _add_then_time_out(*, command, cwd=None, timeout=None):
            done = await real(command=command, cwd=cwd, timeout=timeout)
            if list(command)[1:3] == ["worktree", "add"]:
                assert done.exit_code == 0, done.stderr
                return ExecuteResult(
                    exit_code=-1,
                    stdout="",
                    stderr=f"git-runner timeout: command timed out: {command!r}",
                )
            return done

        runner._execute = _add_then_time_out
        result = await runner.prepare_branch_and_write_tree(
            str(repo), BRANCH, FILES, "planning: spec"
        )

        assert result.status == "failed"
        assert "timeout" in (result.stderr or "")
        assert _registered(repo) == []
        assert list(root.iterdir()) == []

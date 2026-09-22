"""Unit tests for ``forge.build.git_operations`` (TASK-IC-010 → TASK-F8-001).

The tests inject a fake ``_execute_via_deepagents`` seam at module
scope and assert on the recorded ``(command, cwd, timeout)`` tuples.
No real git/gh process spawns; the BDD harness in
``tests/bdd/test_infrastructure_coordination.py`` covers the
end-to-end scenario contract.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from forge.build import git_operations
from forge.build.git_operations import (
    ALLOWED_BINARIES,
    DisallowedBinaryError,
    commit_changes,
    create_branch,
    create_pull_request,
    push_branch,
)


@dataclass
class FakeSeam:
    """Records every seam invocation; returns scripted tuples."""

    commands: list[tuple[list[str], str, int]] = field(default_factory=list)
    next_results: list[tuple[str, str, int, float, bool]] = field(default_factory=list)
    default_result: tuple[str, str, int, float, bool] = ("", "", 0, 0.01, False)

    def queue(self, result: tuple[str, str, int, float, bool]) -> None:
        self.next_results.append(result)

    async def __call__(
        self, *, command: list[str], cwd: str, timeout: int, **_launch: Any
    ) -> tuple[str, str, int, float, bool]:
        self.commands.append((list(command), cwd, timeout))
        if self.next_results:
            return self.next_results.pop(0)
        return self.default_result


@pytest.fixture
def fake_seam(monkeypatch: pytest.MonkeyPatch) -> FakeSeam:
    seam = FakeSeam()
    monkeypatch.setattr(git_operations, "_execute_via_deepagents", seam)
    return seam


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    wt = tmp_path / "worktree"
    wt.mkdir()
    return wt


# --------------------------------------------------------------------------- #
# ALLOWED_BINARIES — single source of truth.
# --------------------------------------------------------------------------- #


class TestAllowedBinaries:
    def test_exact_membership(self) -> None:
        assert ALLOWED_BINARIES == frozenset({"git", "gh", "pytest"})

    def test_is_frozenset(self) -> None:
        assert isinstance(ALLOWED_BINARIES, frozenset)


# --------------------------------------------------------------------------- #
# create_branch
# --------------------------------------------------------------------------- #


class TestCreateBranch:
    def test_invokes_git_checkout_b(self, fake_seam: FakeSeam, worktree: Path) -> None:
        asyncio.run(create_branch(worktree, "build/feat-001"))

        assert len(fake_seam.commands) == 1
        cmd, cwd, _timeout = fake_seam.commands[0]
        assert cmd == ["git", "checkout", "-b", "build/feat-001"]
        assert cwd == str(worktree)

    def test_relative_worktree_raises_before_seam(self, fake_seam: FakeSeam) -> None:
        with pytest.raises(ValueError, match="absolute"):
            asyncio.run(create_branch(Path("relative/path"), "build/x"))

        assert fake_seam.commands == []


# --------------------------------------------------------------------------- #
# commit_changes
# --------------------------------------------------------------------------- #


class TestCommitChanges:
    def test_invokes_add_then_commit(
        self, fake_seam: FakeSeam, worktree: Path
    ) -> None:
        asyncio.run(commit_changes(worktree, "build summary"))

        assert len(fake_seam.commands) == 2
        assert fake_seam.commands[0][0] == ["git", "add", "-A"]
        assert fake_seam.commands[1][0] == ["git", "commit", "-m", "build summary"]

    def test_commit_message_with_metacharacters_passed_as_single_token(
        self, fake_seam: FakeSeam, worktree: Path
    ) -> None:
        asyncio.run(commit_changes(worktree, "msg; rm -rf / && echo x"))

        commit_argv = fake_seam.commands[1][0]
        # The full message remains a single argv element — no shell parsing.
        assert commit_argv[-1] == "msg; rm -rf / && echo x"


# --------------------------------------------------------------------------- #
# push_branch
# --------------------------------------------------------------------------- #


class TestPushBranch:
    def test_invokes_git_push_with_upstream(
        self, fake_seam: FakeSeam, worktree: Path
    ) -> None:
        asyncio.run(push_branch(worktree, "build/feat-001"))

        cmd, cwd, _timeout = fake_seam.commands[0]
        assert cmd == ["git", "push", "-u", "origin", "build/feat-001"]
        assert cwd == str(worktree)


# --------------------------------------------------------------------------- #
# create_pull_request
# --------------------------------------------------------------------------- #


class TestCreatePullRequest:
    def test_returns_pr_url_from_stdout_last_line(
        self,
        fake_seam: FakeSeam,
        worktree: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GH_TOKEN", "ghp_dummy")
        fake_seam.queue(("https://github.com/example/r/pull/42\n", "", 0, 0.5, False))

        url = asyncio.run(
            create_pull_request(worktree, "title", "body", base="main")
        )

        assert url == "https://github.com/example/r/pull/42"
        cmd, _cwd, _timeout = fake_seam.commands[0]
        assert cmd[:3] == ["gh", "pr", "create"]
        assert "--title" in cmd and "title" in cmd
        assert "--base" in cmd and "main" in cmd

    def test_uses_github_token_when_gh_token_absent(
        self,
        fake_seam: FakeSeam,
        worktree: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", "github_pat_dummy")
        fake_seam.queue(("https://github.com/example/r/pull/7\n", "", 0, 0.4, False))

        url = asyncio.run(create_pull_request(worktree, "t", "b"))

        assert url == "https://github.com/example/r/pull/7"

    def test_returns_none_without_credentials(
        self,
        fake_seam: FakeSeam,
        worktree: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        url = asyncio.run(create_pull_request(worktree, "t", "b"))

        assert url is None
        # Critical: gh was never spawned.
        assert fake_seam.commands == []

    def test_returns_none_on_gh_auth_failure(
        self,
        fake_seam: FakeSeam,
        worktree: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Auth-shaped stderr → soft-fail (None), not RuntimeError.
        monkeypatch.setenv("GH_TOKEN", "ghp_dummy")
        fake_seam.queue(("", "error: authentication required", 1, 0.1, False))

        url = asyncio.run(create_pull_request(worktree, "t", "b"))

        assert url is None

    def test_genuine_gh_failure_raises_runtime_error(
        self,
        fake_seam: FakeSeam,
        worktree: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Non-auth gh failure (e.g. base branch missing) → RuntimeError.
        monkeypatch.setenv("GH_TOKEN", "ghp_dummy")
        fake_seam.queue(("", "remote: base branch 'develop' not found", 1, 0.1, False))

        with pytest.raises(RuntimeError, match="gh pr create"):
            asyncio.run(create_pull_request(worktree, "t", "b", base="develop"))

    def test_empty_title_rejected(self, fake_seam: FakeSeam, worktree: Path) -> None:
        with pytest.raises(ValueError, match="title"):
            asyncio.run(create_pull_request(worktree, "", "body"))
        assert fake_seam.commands == []

    def test_no_credential_token_appears_in_argv(
        self,
        fake_seam: FakeSeam,
        worktree: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GH_TOKEN", "ghp_secrettoken")
        fake_seam.queue(("https://example/pull/1", "", 0, 0.1, False))

        asyncio.run(create_pull_request(worktree, "t", "b"))

        flat_tokens = [tok for cmd, _, _ in fake_seam.commands for tok in cmd]
        assert not any(t.startswith("ghp_") for t in flat_tokens)
        assert not any(t.startswith("github_pat") for t in flat_tokens)


# --------------------------------------------------------------------------- #
# Allowlist / cwd validation.
# --------------------------------------------------------------------------- #


class TestValidationGuards:
    def test_disallowed_binary_validation_helper_raises(self) -> None:
        with pytest.raises(DisallowedBinaryError, match="not on the allowlist"):
            git_operations._validate_binary(["rm", "-rf", "/"])

    def test_empty_command_raises(self) -> None:
        with pytest.raises(DisallowedBinaryError, match="empty"):
            git_operations._validate_binary([])

    def test_disallowed_binary_error_is_value_error_subclass(self) -> None:
        # The BDD scenario catches ``(ValueError, DisallowedBinaryError)`` —
        # the subclass relationship makes ``ValueError`` alone sufficient.
        assert issubclass(DisallowedBinaryError, ValueError)

    def test_validate_worktree_accepts_absolute(self, tmp_path: Path) -> None:
        # Absolute path should not raise.
        git_operations._validate_worktree(tmp_path)

    def test_validate_worktree_rejects_relative(self) -> None:
        with pytest.raises(ValueError, match="absolute"):
            git_operations._validate_worktree(Path("relative"))


class TestRunViaExecuteHelper:
    def test_basename_allowlist_accepts_path_prefixed_git(
        self, fake_seam: FakeSeam, worktree: Path
    ) -> None:
        # ``/usr/local/bin/git`` is fine — basename is on the allowlist.
        asyncio.run(
            git_operations._run_via_execute(worktree, ["/usr/local/bin/git", "status"])
        )
        assert len(fake_seam.commands) == 1

    def test_basename_allowlist_rejects_path_prefixed_rm(
        self, fake_seam: FakeSeam, worktree: Path
    ) -> None:
        with pytest.raises(DisallowedBinaryError):
            asyncio.run(
                git_operations._run_via_execute(worktree, ["/usr/bin/rm", "-rf", "/"])
            )
        assert fake_seam.commands == []


class TestNonZeroExitSurfacing:
    def test_create_branch_failure_raises(
        self, fake_seam: FakeSeam, worktree: Path
    ) -> None:
        fake_seam.queue(("", "fatal: branch exists", 128, 0.1, False))
        with pytest.raises(RuntimeError, match="git checkout"):
            asyncio.run(create_branch(worktree, "x"))

    def test_commit_failure_raises_on_add(
        self, fake_seam: FakeSeam, worktree: Path
    ) -> None:
        fake_seam.queue(("", "permission denied", 1, 0.1, False))
        with pytest.raises(RuntimeError, match="git"):
            asyncio.run(commit_changes(worktree, "msg"))

    def test_push_failure_raises(
        self, fake_seam: FakeSeam, worktree: Path
    ) -> None:
        fake_seam.queue(("", "remote rejected", 1, 0.1, False))
        with pytest.raises(RuntimeError, match="git push"):
            asyncio.run(push_branch(worktree, "x"))

    def test_empty_branch_name_rejected(
        self, fake_seam: FakeSeam, worktree: Path
    ) -> None:
        with pytest.raises(ValueError, match="branch_name"):
            asyncio.run(create_branch(worktree, ""))
        assert fake_seam.commands == []

    def test_empty_commit_message_rejected(
        self, fake_seam: FakeSeam, worktree: Path
    ) -> None:
        with pytest.raises(ValueError, match="message"):
            asyncio.run(commit_changes(worktree, ""))
        assert fake_seam.commands == []

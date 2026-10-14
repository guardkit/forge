"""Unit tests for ``forge.build.git_operations`` (TASK-IC-010 → TASK-F8-001).

The tests inject a fake ``_execute_via_deepagents`` seam at module
scope and assert on the recorded ``(command, cwd, timeout)`` tuples.
No real git/gh process spawns; the BDD harness in
``tests/bdd/test_infrastructure_coordination.py`` covers the
end-to-end scenario contract.
"""

from __future__ import annotations

import asyncio
import subprocess
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


class TestGitThroughTheHelper:
    """Since 14 October 2026 a git command reaching the default seam runs
    through ``forge.project_git.run_git``, and git is given nothing from
    this process's environment."""

    def test_default_seam_hands_git_to_run_git(
        self, monkeypatch: pytest.MonkeyPatch, worktree: Path
    ) -> None:
        seen: list[tuple[list[str], dict[str, Any]]] = []

        async def _fake(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
            seen.append((list(args), kwargs))
            return subprocess.CompletedProcess(
                args, 1, "", "git commit stopped with exit code 1."
            )

        monkeypatch.setattr(git_operations, "run_git_async", _fake)

        stdout, stderr, exit_code, _duration, timed_out = asyncio.run(
            git_operations._execute_via_deepagents(
                command=["git", "commit", "-m", "x"],
                cwd=str(worktree),
                timeout=30,
                env={"SOME_FACTORY_SECRET": "not for git"},
            )
        )

        assert seen[0][0] == ["git", "commit", "-m", "x"]
        assert seen[0][1]["cwd"] == str(worktree)
        assert seen[0][1]["timeout"] == 30
        assert "env" not in seen[0][1]
        assert (stdout, stderr, exit_code, timed_out) == (
            "",
            "git commit stopped with exit code 1.",
            1,
            False,
        )

    def test_default_seam_reports_a_git_timeout(
        self, monkeypatch: pytest.MonkeyPatch, worktree: Path
    ) -> None:
        async def _times_out(args: list[str], **kwargs: Any) -> None:
            raise subprocess.TimeoutExpired(
                "git add", 5, stderr="git add did not finish within 5 seconds."
            )

        monkeypatch.setattr(git_operations, "run_git_async", _times_out)

        _stdout, stderr, exit_code, _duration, timed_out = asyncio.run(
            git_operations._execute_via_deepagents(
                command=["git", "add", "-A"], cwd=str(worktree), timeout=5, env={}
            )
        )

        assert (exit_code, timed_out) == (124, True)
        assert stderr == "git add did not finish within 5 seconds."

    def test_git_is_given_no_environment_and_gh_is_given_this_processs(
        self, monkeypatch: pytest.MonkeyPatch, worktree: Path
    ) -> None:
        envs: list[dict[str, str]] = []

        async def _seam(*, command, cwd, timeout, env):  # noqa: ANN001
            envs.append(env)
            return "", "", 0, 0.0, False

        monkeypatch.setattr(git_operations, "_execute_via_deepagents", _seam)
        monkeypatch.setenv("GH_TOKEN", "ghp_dummy")

        asyncio.run(git_operations._run_via_execute(worktree, ["git", "status"]))
        asyncio.run(git_operations._run_via_execute(worktree, ["gh", "pr", "list"]))

        assert envs[0] == {}
        assert envs[1].get("GH_TOKEN") == "ghp_dummy"


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

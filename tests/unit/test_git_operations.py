"""Unit tests for :mod:`forge.build.git_operations` (TASK-IC-010).

Coverage map (one test class per acceptance criterion cluster):

* ``TestAllowlistSharedConstant`` — AC-001 (single source of truth)
* ``TestExecuteSeamRouting`` — AC-002, AC-003 (seam dispatch + cwd)
* ``TestEnvOnlyCredentials`` — AC-004, AC-005 (creds + missing-creds path)
* ``TestDisallowedBinary`` — AC-006 (binary allowlist refusal)
* ``TestPullRequestUrlParsing`` — AC-007 (URL parsing)
* ``TestAwaitable`` — AC-008 (every operation is a coroutine function)
* ``TestFailureSurfacing`` — non-AC defensive: exit-code propagation

All tests stub the ``_execute_via_deepagents`` seam — no real
subprocesses are spawned, no network, no real ``git`` state mutation.
That stubbing is the explicit AGENTS.md / TASK-REV-IC8B contract: tests
talk to the seam, never to :mod:`subprocess`.
"""

from __future__ import annotations

import inspect
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

# ---------------------------------------------------------------------------
# Shared fakes / fixtures
# ---------------------------------------------------------------------------


class FakeExecute:
    """In-memory recorder for the DeepAgents ``execute`` seam.

    Records ``command`` / ``cwd`` / ``timeout`` per call and returns
    canned ``(stdout, stderr, exit_code, duration, timed_out)``. Tests
    mutate the canned return *before* awaiting the operation so
    different scenarios can share the fixture.
    """

    def __init__(
        self,
        stdout: str = "",
        stderr: str = "",
        exit_code: int = 0,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        *,
        command: list[str],
        cwd: str,
        timeout: int,
    ) -> tuple[str, str, int, float, bool]:
        # Record a defensive copy of ``command`` so a caller mutating
        # the list in-place after the await can't retroactively rewrite
        # what the test asserts against.
        self.calls.append({"command": list(command), "cwd": cwd, "timeout": timeout})
        return self.stdout, self.stderr, self.exit_code, 0.0, False


@pytest.fixture
def fake_seam(monkeypatch: pytest.MonkeyPatch) -> FakeExecute:
    """Replace the seam with a default-success fake the test can mutate."""
    fake = FakeExecute()
    monkeypatch.setattr(git_operations, "_execute_via_deepagents", fake)
    return fake


@pytest.fixture
def with_credentials(monkeypatch: pytest.MonkeyPatch) -> str:
    """Seed ``GH_TOKEN`` so :func:`create_pull_request` proceeds past the env check."""
    token = "ghp_test_token_value"
    monkeypatch.setenv("GH_TOKEN", token)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    return token


@pytest.fixture
def no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip both credential env vars so :func:`create_pull_request` short-circuits."""
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


# Absolute path representing the per-build worktree the execute tool's
# working-directory allowlist would validate against. We don't need it
# to actually exist on disk because the seam is mocked.
WORKTREE = Path("/tmp/forge-builds/build-abc123").resolve()


# ---------------------------------------------------------------------------
# AC-001: single shared allowlist constant
# ---------------------------------------------------------------------------


class TestAllowlistSharedConstant:
    """The ``ALLOWED_BINARIES`` constant is a single source of truth."""

    def test_allowed_binaries_value(self) -> None:
        assert ALLOWED_BINARIES == frozenset({"git", "gh", "pytest"})

    def test_allowed_binaries_is_frozenset(self) -> None:
        # frozenset (not set) so accidental in-place mutation raises.
        assert isinstance(ALLOWED_BINARIES, frozenset)

    def test_test_verification_imports_same_object(self) -> None:
        """Seam test contract: TASK-IC-009 must reference the SAME object.

        The §4 Integration Contract pins this — TASK-IC-009's test
        verification imports the constant from this module, not via
        a copy-pasted literal.
        """
        from forge.build.test_verification import _allowed_binaries_for_test

        assert _allowed_binaries_for_test() is ALLOWED_BINARIES, (
            "test_verification must reference the SAME ALLOWED_BINARIES "
            "object — see TASK-REV-IC8B Risk 5 (allowlist drift)."
        )


# ---------------------------------------------------------------------------
# AC-002 / AC-003: dispatch via execute seam, cwd locked to worktree_path
# ---------------------------------------------------------------------------


class TestExecuteSeamRouting:
    """All four ops dispatch via the seam with ``cwd=worktree_path``."""

    @pytest.mark.asyncio
    async def test_create_branch_dispatches_via_seam(
        self, fake_seam: FakeExecute
    ) -> None:
        await create_branch(WORKTREE, "feat/new-thing")

        assert len(fake_seam.calls) == 1, "exactly one execute call expected"
        call = fake_seam.calls[0]
        assert call["command"] == ["git", "checkout", "-b", "feat/new-thing"]
        assert call["cwd"] == str(WORKTREE)

    @pytest.mark.asyncio
    async def test_commit_changes_runs_add_then_commit(
        self, fake_seam: FakeExecute
    ) -> None:
        await commit_changes(WORKTREE, "feat: add x")

        assert [c["command"] for c in fake_seam.calls] == [
            ["git", "add", "-A"],
            ["git", "commit", "-m", "feat: add x"],
        ]
        for call in fake_seam.calls:
            assert call["cwd"] == str(WORKTREE)

    @pytest.mark.asyncio
    async def test_push_branch_uses_set_upstream(self, fake_seam: FakeExecute) -> None:
        await push_branch(WORKTREE, "feat/branch")
        assert fake_seam.calls[0]["command"] == [
            "git",
            "push",
            "-u",
            "origin",
            "feat/branch",
        ]
        assert fake_seam.calls[0]["cwd"] == str(WORKTREE)

    @pytest.mark.asyncio
    async def test_create_pull_request_invokes_gh(
        self, fake_seam: FakeExecute, with_credentials: str
    ) -> None:
        fake_seam.stdout = "https://github.com/owner/repo/pull/42\n"
        url = await create_pull_request(WORKTREE, "Title", "Body", base="develop")
        assert url == "https://github.com/owner/repo/pull/42"
        assert fake_seam.calls[0]["command"] == [
            "gh",
            "pr",
            "create",
            "--title",
            "Title",
            "--body",
            "Body",
            "--base",
            "develop",
        ]
        assert fake_seam.calls[0]["cwd"] == str(WORKTREE)

    @pytest.mark.asyncio
    async def test_relative_worktree_path_rejected_pre_dispatch(
        self, fake_seam: FakeExecute
    ) -> None:
        """Relative paths fail validation before reaching the seam."""
        with pytest.raises(ValueError, match="absolute"):
            await create_branch(Path("relative/path"), "feat/x")
        assert (
            fake_seam.calls == []
        ), "validation must reject before any execute call is made"

    @pytest.mark.asyncio
    async def test_create_pull_request_default_base_is_main(
        self, fake_seam: FakeExecute, with_credentials: str
    ) -> None:
        fake_seam.stdout = "https://github.com/o/r/pull/1\n"
        await create_pull_request(WORKTREE, "T", "B")
        assert "main" in fake_seam.calls[0]["command"]
        assert "--base" in fake_seam.calls[0]["command"]


# ---------------------------------------------------------------------------
# AC-004 / AC-005: env-only credentials + missing creds don't crash
# ---------------------------------------------------------------------------


class TestEnvOnlyCredentials:
    """Credentials are read from env only and missing creds don't crash."""

    @pytest.mark.asyncio
    async def test_no_credentials_skips_gh_and_returns_none(
        self,
        fake_seam: FakeExecute,
        no_credentials: None,
    ) -> None:
        url = await create_pull_request(WORKTREE, "Title", "Body")
        assert url is None
        assert (
            fake_seam.calls == []
        ), "gh pr create must not run when no credentials are available"

    @pytest.mark.asyncio
    async def test_command_args_never_carry_credentials(
        self,
        fake_seam: FakeExecute,
        with_credentials: str,
    ) -> None:
        """Tokens MUST stay in the env — never appear on argv."""
        fake_seam.stdout = "https://github.com/o/r/pull/1\n"
        await create_pull_request(WORKTREE, "T", "B")
        for arg in fake_seam.calls[0]["command"]:
            assert (
                with_credentials not in arg
            ), "credentials must NEVER appear on argv — gh reads env vars"

    @pytest.mark.asyncio
    async def test_gh_auth_failure_returns_none(
        self,
        fake_seam: FakeExecute,
        with_credentials: str,
    ) -> None:
        """Even with creds set, an auth-failure stderr triggers the soft path."""
        fake_seam.exit_code = 1
        fake_seam.stderr = "error: authentication required, run `gh auth login`\n"
        url = await create_pull_request(WORKTREE, "T", "B")
        assert url is None

    @pytest.mark.asyncio
    async def test_github_token_alone_satisfies_check(
        self,
        fake_seam: FakeExecute,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``GITHUB_TOKEN`` (CI) alone satisfies the env-credential check."""
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", "github_actions_token")
        fake_seam.stdout = "https://github.com/o/r/pull/3\n"
        url = await create_pull_request(WORKTREE, "T", "B")
        assert url == "https://github.com/o/r/pull/3"

    @pytest.mark.asyncio
    async def test_empty_string_token_treated_as_missing(
        self,
        fake_seam: FakeExecute,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An empty token is no different from no token in practice."""
        monkeypatch.setenv("GH_TOKEN", "")
        monkeypatch.setenv("GITHUB_TOKEN", "")
        url = await create_pull_request(WORKTREE, "T", "B")
        assert url is None
        assert fake_seam.calls == []


# ---------------------------------------------------------------------------
# AC-006: disallowed binary refused at validation layer
# ---------------------------------------------------------------------------


class TestDisallowedBinary:
    """Anything outside the allowlist raises :class:`DisallowedBinaryError`."""

    @pytest.mark.asyncio
    async def test_rm_is_rejected(self, fake_seam: FakeExecute) -> None:
        with pytest.raises(DisallowedBinaryError, match="allowlist"):
            await git_operations._run_via_execute(WORKTREE, ["rm", "-rf", "/"])
        assert fake_seam.calls == []

    @pytest.mark.asyncio
    async def test_curl_is_rejected(self, fake_seam: FakeExecute) -> None:
        with pytest.raises(DisallowedBinaryError):
            await git_operations._run_via_execute(
                WORKTREE, ["curl", "https://evil.example"]
            )
        assert fake_seam.calls == []

    @pytest.mark.asyncio
    async def test_path_prefixed_disallowed_binary_rejected(
        self, fake_seam: FakeExecute
    ) -> None:
        """Caller can't smuggle ``rm`` past validation by prepending a path."""
        with pytest.raises(DisallowedBinaryError):
            await git_operations._run_via_execute(WORKTREE, ["/usr/bin/rm", "-rf", "/"])

    @pytest.mark.asyncio
    async def test_path_prefixed_allowed_binary_accepted(
        self, fake_seam: FakeExecute
    ) -> None:
        """``/usr/local/bin/git`` is fine — basename is on the allowlist."""
        await git_operations._run_via_execute(
            WORKTREE, ["/usr/local/bin/git", "status"]
        )
        assert len(fake_seam.calls) == 1

    def test_disallowed_binary_error_is_value_error_subclass(self) -> None:
        """Existing ``except ValueError`` boundaries still catch it."""
        assert issubclass(DisallowedBinaryError, ValueError)


# ---------------------------------------------------------------------------
# AC-007: PR URL parsed from gh stdout
# ---------------------------------------------------------------------------


class TestPullRequestUrlParsing:
    """The PR URL is extracted from the last ``http``-prefixed line."""

    @pytest.mark.asyncio
    async def test_pr_url_returned_when_url_is_last_line(
        self, fake_seam: FakeExecute, with_credentials: str
    ) -> None:
        fake_seam.stdout = (
            "Creating pull request for feat/x into main in owner/repo\n"
            "https://github.com/owner/repo/pull/123\n"
        )
        url = await create_pull_request(WORKTREE, "T", "B")
        assert url == "https://github.com/owner/repo/pull/123"

    @pytest.mark.asyncio
    async def test_pr_url_returned_when_only_url_in_stdout(
        self, fake_seam: FakeExecute, with_credentials: str
    ) -> None:
        fake_seam.stdout = "https://github.com/o/r/pull/9"
        url = await create_pull_request(WORKTREE, "T", "B")
        assert url == "https://github.com/o/r/pull/9"

    @pytest.mark.asyncio
    async def test_pr_url_none_when_stdout_lacks_url(
        self, fake_seam: FakeExecute, with_credentials: str
    ) -> None:
        """Pathological case: gh exits 0 but doesn't print a URL line."""
        fake_seam.stdout = "Creating pull request...\nDone.\n"
        url = await create_pull_request(WORKTREE, "T", "B")
        assert url is None


# ---------------------------------------------------------------------------
# AC-008: every operation is a coroutine function (interleavable)
# ---------------------------------------------------------------------------


class TestAwaitable:
    @pytest.mark.parametrize(
        "fn",
        [create_branch, commit_changes, push_branch, create_pull_request],
    )
    def test_function_is_coroutine_function(self, fn: Any) -> None:
        assert inspect.iscoroutinefunction(fn), (
            f"{fn.__name__} must be `async def` so the build loop can "
            "interleave it with other awaitables (AC-008)."
        )


# ---------------------------------------------------------------------------
# Defensive: non-zero exit codes surface clean RuntimeErrors
# ---------------------------------------------------------------------------


class TestFailureSurfacing:
    """Non-zero exits surface as :class:`RuntimeError` with diagnostic context."""

    @pytest.mark.asyncio
    async def test_create_branch_failure_raises_runtime_error(
        self, fake_seam: FakeExecute
    ) -> None:
        fake_seam.exit_code = 128
        fake_seam.stderr = "fatal: A branch named 'x' already exists.\n"
        with pytest.raises(RuntimeError, match="git checkout"):
            await create_branch(WORKTREE, "x")

    @pytest.mark.asyncio
    async def test_commit_failure_raises_runtime_error(
        self, fake_seam: FakeExecute
    ) -> None:
        fake_seam.exit_code = 1
        fake_seam.stderr = "nothing to commit\n"
        with pytest.raises(RuntimeError, match="git"):
            await commit_changes(WORKTREE, "msg")

    @pytest.mark.asyncio
    async def test_push_failure_raises_runtime_error(
        self, fake_seam: FakeExecute
    ) -> None:
        fake_seam.exit_code = 1
        fake_seam.stderr = "remote rejected\n"
        with pytest.raises(RuntimeError, match="git push"):
            await push_branch(WORKTREE, "x")

    @pytest.mark.asyncio
    async def test_create_pr_genuine_failure_raises(
        self,
        fake_seam: FakeExecute,
        with_credentials: str,
    ) -> None:
        """Non-auth gh failures (e.g. base branch missing) propagate."""
        fake_seam.exit_code = 1
        fake_seam.stderr = "remote: base branch 'develop' not found\n"
        with pytest.raises(RuntimeError, match="gh pr create"):
            await create_pull_request(WORKTREE, "T", "B", base="develop")

    @pytest.mark.asyncio
    async def test_empty_branch_name_rejected(self, fake_seam: FakeExecute) -> None:
        with pytest.raises(ValueError, match="branch_name"):
            await create_branch(WORKTREE, "")
        assert fake_seam.calls == []

    @pytest.mark.asyncio
    async def test_empty_commit_message_rejected(self, fake_seam: FakeExecute) -> None:
        with pytest.raises(ValueError, match="message"):
            await commit_changes(WORKTREE, "")
        assert fake_seam.calls == []

    @pytest.mark.asyncio
    async def test_empty_pr_title_rejected(
        self,
        fake_seam: FakeExecute,
        with_credentials: str,
    ) -> None:
        with pytest.raises(ValueError, match="title"):
            await create_pull_request(WORKTREE, "", "B")
        assert fake_seam.calls == []

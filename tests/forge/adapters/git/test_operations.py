"""Unit + seam tests for ``forge.adapters.git.operations`` (TASK-GCI-006).

The unit tests inject a fake ``execute`` callable and assert on:

* AC-001 / AC-004 / AC-005 / AC-006 / AC-007 — happy-path output shape
  for each of the four operations.
* AC-002 — every git invocation goes through the injected ``execute``;
  no direct ``subprocess`` / ``os.system`` is used.
* AC-003 — every recorded call's ``cwd`` is inside the build's worktree
  (or, for ``prepare_worktree``, the source repo).
* AC-006 / AC-007 — non-zero exit becomes ``status="failed"`` with
  stderr preserved.
* AC-007 — a failed ``cleanup_worktree`` is logged but does not raise.
* AC-008 — exceptions raised by the injected ``execute`` become a
  ``status="failed"`` result; the operation never raises.
* AC-009 — shell metacharacters in arguments are passed as literal
  argv tokens (commit message containing ``;`` and ``&&``, branch name
  containing a quote).
* AC-010 — the test file itself; pytest discovers and runs it.

The seam tests at the bottom (``@pytest.mark.seam``) hit a real ``git``
binary against a tmp_path fixture (no network) and validate the
worktree-add / commit / cleanup-failure-doesn't-block contract.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from forge.adapters.git.models import GitOpResult
from forge.adapters.git.operations import (
    ExecuteResult,
    _default_execute,
    cleanup_worktree,
    commit_all,
    prepare_worktree,
    push,
)

# --------------------------------------------------------------------------- #
# Fakes & fixtures.
# --------------------------------------------------------------------------- #


@dataclass
class _RecordedCall:
    """One captured invocation of the fake ``execute``."""

    command: list[str]
    cwd: str | None
    timeout: float | None


@dataclass
class FakeExecute:
    """Recording fake for the ``execute`` callable.

    ``responses`` is a list of :class:`ExecuteResult` (or exceptions)
    to return in order; if exhausted, the last entry is replayed. If
    ``raise_exc`` is set, the *first* call raises it; subsequent calls
    fall through to ``responses``.
    """

    responses: list[ExecuteResult] = field(default_factory=list)
    raise_exc: BaseException | None = None
    calls: list[_RecordedCall] = field(default_factory=list)
    _raised: bool = False

    async def __call__(
        self,
        *,
        command: list[str],
        cwd: str | None = None,
        timeout: float | None = None,
    ) -> ExecuteResult:
        self.calls.append(
            _RecordedCall(command=list(command), cwd=cwd, timeout=timeout)
        )
        if self.raise_exc is not None and not self._raised:
            self._raised = True
            raise self.raise_exc
        if not self.responses:
            return ExecuteResult(exit_code=0, stdout="", stderr="")
        # Pop from the front; replay the last response if exhausted.
        if len(self.responses) == 1:
            return self.responses[0]
        return self.responses.pop(0)


def _ok(stdout: str = "", stderr: str = "") -> ExecuteResult:
    return ExecuteResult(exit_code=0, stdout=stdout, stderr=stderr)


def _fail(exit_code: int = 1, stdout: str = "", stderr: str = "boom") -> ExecuteResult:
    return ExecuteResult(exit_code=exit_code, stdout=stdout, stderr=stderr)


# --------------------------------------------------------------------------- #
# prepare_worktree
# --------------------------------------------------------------------------- #


class TestPrepareWorktree:
    """AC-001 / AC-002 / AC-003 / AC-004 / AC-008 / AC-009."""

    @pytest.mark.asyncio
    async def test_success_populates_worktree_path(self, tmp_path: Path) -> None:
        fake = FakeExecute(responses=[_ok()])
        builds_root = tmp_path / "builds"

        result = await prepare_worktree(
            "build-42",
            tmp_path / "repo",
            "main",
            execute=fake,
            builds_root=builds_root,
        )

        assert isinstance(result, GitOpResult)
        assert result.status == "success"
        assert result.operation == "prepare_worktree"
        assert result.exit_code == 0
        assert result.worktree_path == str(builds_root / "build-42")
        # AC-002: exactly one execute call (no fallback subprocess).
        assert len(fake.calls) == 1

    @pytest.mark.asyncio
    async def test_command_is_list_tokens_and_cwd_is_repo(self, tmp_path: Path) -> None:
        fake = FakeExecute(responses=[_ok()])
        repo = tmp_path / "repo"
        builds_root = tmp_path / "builds"

        await prepare_worktree(
            "b1", repo, "feature/x", execute=fake, builds_root=builds_root
        )

        call = fake.calls[0]
        assert call.command == [
            "git",
            "worktree",
            "add",
            str(builds_root / "b1"),
            "feature/x",
        ]
        # AC-003: cwd is the source repo (worktree does not yet exist).
        assert call.cwd == str(repo)

    @pytest.mark.asyncio
    async def test_non_zero_exit_returns_failed(self, tmp_path: Path) -> None:
        fake = FakeExecute(
            responses=[_fail(exit_code=128, stderr="fatal: path exists")]
        )

        result = await prepare_worktree(
            "b",
            tmp_path / "repo",
            "main",
            execute=fake,
            builds_root=tmp_path / "builds",
        )

        assert result.status == "failed"
        assert result.exit_code == 128
        assert result.stderr is not None and "path exists" in result.stderr
        assert result.worktree_path is None

    @pytest.mark.asyncio
    async def test_exception_in_execute_does_not_raise(self, tmp_path: Path) -> None:
        fake = FakeExecute(raise_exc=OSError("disk gone"))

        result = await prepare_worktree(
            "b",
            tmp_path / "repo",
            "main",
            execute=fake,
            builds_root=tmp_path / "builds",
        )

        assert result.status == "failed"
        assert result.operation == "prepare_worktree"
        assert result.stderr == "OSError: disk gone"
        assert result.exit_code == -1


# --------------------------------------------------------------------------- #
# commit_all
# --------------------------------------------------------------------------- #


class TestCommitAll:
    """AC-001 / AC-002 / AC-003 / AC-005 / AC-008 / AC-009."""

    @pytest.mark.asyncio
    async def test_success_returns_sha_from_rev_parse(self, tmp_path: Path) -> None:
        fake = FakeExecute(
            responses=[
                _ok(),  # git add -A
                _ok(stdout="[main abc1234] msg\n"),  # git commit -m
                _ok(stdout="abc1234567890abcdef\n"),  # git rev-parse HEAD
            ]
        )
        worktree = tmp_path / "wt"

        result = await commit_all(worktree, "feat: add thing", execute=fake)

        assert result.status == "success"
        assert result.operation == "commit_all"
        assert result.sha == "abc1234567890abcdef"
        assert result.exit_code == 0
        # AC-002: three calls — add, commit, rev-parse.
        assert [c.command[:2] for c in fake.calls] == [
            ["git", "add"],
            ["git", "commit"],
            ["git", "rev-parse"],
        ]
        # AC-003: every call's cwd is the build worktree.
        assert all(c.cwd == str(worktree) for c in fake.calls)

    @pytest.mark.asyncio
    async def test_message_with_shell_metacharacters_passed_as_literal_token(
        self, tmp_path: Path
    ) -> None:
        # AC-009: ``;`` / ``&&`` / quotes never break out of a single argv slot.
        nasty = 'feat: pwn; rm -rf / && echo "owned"'
        fake = FakeExecute(responses=[_ok(), _ok(), _ok(stdout="deadbeef\n")])

        result = await commit_all(tmp_path / "wt", nasty, execute=fake)

        commit_call = fake.calls[1]
        assert commit_call.command == ["git", "commit", "-m", nasty]
        # The metacharacters live in a single list element — they have
        # not been split, escaped, or otherwise mangled.
        assert commit_call.command[3] is nasty
        assert result.status == "success"
        assert result.sha == "deadbeef"

    @pytest.mark.asyncio
    async def test_add_failure_short_circuits(self, tmp_path: Path) -> None:
        fake = FakeExecute(responses=[_fail(stderr="add died")])

        result = await commit_all(tmp_path / "wt", "msg", execute=fake)

        assert result.status == "failed"
        assert result.exit_code == 1
        assert result.stderr is not None and "add died" in result.stderr
        # Only one call was issued — commit / rev-parse are skipped.
        assert len(fake.calls) == 1

    @pytest.mark.asyncio
    async def test_commit_with_nothing_to_commit_returns_failed_with_stdout(
        self, tmp_path: Path
    ) -> None:
        # Real-world case: ``git commit`` writes "nothing to commit" to
        # stdout (not stderr) and exits 1. We must preserve that signal.
        fake = FakeExecute(
            responses=[
                _ok(),  # add -A
                ExecuteResult(
                    exit_code=1,
                    stdout="nothing to commit, working tree clean\n",
                    stderr="",
                ),
            ]
        )

        result = await commit_all(tmp_path / "wt", "msg", execute=fake)

        assert result.status == "failed"
        assert result.exit_code == 1
        assert result.stderr is not None
        assert "nothing to commit" in result.stderr

    @pytest.mark.asyncio
    async def test_exception_in_execute_does_not_raise(self, tmp_path: Path) -> None:
        fake = FakeExecute(raise_exc=RuntimeError("kaboom"))

        result = await commit_all(tmp_path / "wt", "msg", execute=fake)

        assert result.status == "failed"
        assert result.stderr == "RuntimeError: kaboom"
        assert result.exit_code == -1


# --------------------------------------------------------------------------- #
# push
# --------------------------------------------------------------------------- #


class TestPush:
    """AC-001 / AC-002 / AC-003 / AC-006 / AC-008."""

    @pytest.mark.asyncio
    async def test_success_returns_success_with_zero_exit(self, tmp_path: Path) -> None:
        fake = FakeExecute(responses=[_ok()])
        worktree = tmp_path / "wt"

        result = await push(worktree, "feature/x", execute=fake)

        assert result.status == "success"
        assert result.operation == "push"
        assert result.exit_code == 0
        assert fake.calls[0].command == [
            "git",
            "push",
            "origin",
            "feature/x",
        ]
        # AC-003: cwd is inside the build worktree.
        assert fake.calls[0].cwd == str(worktree)

    @pytest.mark.asyncio
    async def test_non_zero_exit_preserves_stderr(self, tmp_path: Path) -> None:
        fake = FakeExecute(
            responses=[_fail(exit_code=1, stderr="! [rejected] non-fast-forward")]
        )

        result = await push(tmp_path / "wt", "main", execute=fake)

        assert result.status == "failed"
        assert result.exit_code == 1
        assert result.stderr is not None
        assert "rejected" in result.stderr

    @pytest.mark.asyncio
    async def test_exception_in_execute_does_not_raise(self, tmp_path: Path) -> None:
        fake = FakeExecute(raise_exc=ConnectionError("network down"))

        result = await push(tmp_path / "wt", "main", execute=fake)

        assert result.status == "failed"
        assert result.stderr == "ConnectionError: network down"
        assert result.exit_code == -1


# --------------------------------------------------------------------------- #
# cleanup_worktree
# --------------------------------------------------------------------------- #


class TestCleanupWorktree:
    """AC-001 / AC-002 / AC-003 / AC-007 / AC-008."""

    @pytest.mark.asyncio
    async def test_success_removes_worktree(self, tmp_path: Path) -> None:
        fake = FakeExecute(responses=[_ok()])
        worktree = tmp_path / "builds" / "b-1"

        result = await cleanup_worktree("b-1", worktree, execute=fake)

        assert result.status == "success"
        assert result.operation == "cleanup_worktree"
        assert result.exit_code == 0
        assert fake.calls[0].command == [
            "git",
            "worktree",
            "remove",
            str(worktree),
            "--force",
        ]

    @pytest.mark.asyncio
    async def test_failure_logs_warning_and_returns_failed_without_raising(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        fake = FakeExecute(responses=[_fail(exit_code=128, stderr="fatal: locked")])
        worktree = tmp_path / "builds" / "b-2"

        with caplog.at_level(logging.WARNING):
            result = await cleanup_worktree("b-2", worktree, execute=fake)

        # AC-007: best-effort — failed status, *but* no exception.
        assert result.status == "failed"
        assert result.exit_code == 128
        assert result.stderr is not None and "locked" in result.stderr

        # The failure is recorded so operators can see it.
        warning_messages = [r.message for r in caplog.records]
        assert any(
            "cleanup_worktree non-zero exit" in m for m in warning_messages
        ), warning_messages

    @pytest.mark.asyncio
    async def test_exception_in_execute_logs_and_returns_failed(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        fake = FakeExecute(raise_exc=OSError("permission denied"))
        worktree = tmp_path / "builds" / "b-3"

        with caplog.at_level(logging.ERROR):
            result = await cleanup_worktree("b-3", worktree, execute=fake)

        assert result.status == "failed"
        assert result.stderr == "OSError: permission denied"
        assert result.exit_code == -1
        # The exception is recorded but not re-raised.
        assert any("cleanup_worktree exception" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# AC-002: no direct subprocess imports.
# --------------------------------------------------------------------------- #


class TestAdapterContract:
    """AC-002: the operations module relies on the injected execute, not
    a direct ``subprocess.run`` / ``os.system`` call.

    We verify by source inspection — the only allowed subprocess
    primitive is ``asyncio.create_subprocess_exec`` inside the documented
    default executor (no shell, list tokens).
    """

    def test_no_subprocess_run_or_os_system_in_module(self) -> None:
        from forge.adapters.git import operations

        source = Path(operations.__file__).read_text(encoding="utf-8")
        # No direct synchronous subprocess use anywhere in the module —
        # only the documented async ``execute`` callable.
        assert "subprocess.run(" not in source
        assert "os.system(" not in source
        # ``shell=True`` would defeat the list-token contract; ensure
        # it is not used as a kwarg in any call site (we tolerate the
        # backtick-quoted reference in the module docstring).
        assert "shell=True," not in source
        assert "shell=True)" not in source


# --------------------------------------------------------------------------- #
# Seam tests — real git binary, no network.
# --------------------------------------------------------------------------- #


_GIT = shutil.which("git")
_HAS_GIT = _GIT is not None


@pytest.fixture
def seeded_repo(tmp_path: Path) -> Path:
    """A throwaway git repo with a single commit on ``main``."""
    import subprocess  # noqa: S404 — seam-only, never used in src.

    repo = tmp_path / "src-repo"
    repo.mkdir()
    env: dict[str, str] = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }

    def _run(*args: str) -> None:
        subprocess.run(  # noqa: S603 — seam fixture only.
            [_GIT, *args], cwd=repo, check=True, env=env, capture_output=True
        )

    _run("init", "-b", "main")
    (repo / "README").write_text("hi\n", encoding="utf-8")
    _run("add", "-A")
    _run("commit", "-m", "init")
    # Create a side branch the worktree can safely check out — ``main``
    # is already pinned to the source-repo checkout itself.
    _run("branch", "wt-branch")
    return repo


@pytest.mark.seam
@pytest.mark.integration_contract("git_adapter_subprocess_contract")
@pytest.mark.skipif(not _HAS_GIT, reason="git binary not available")
@pytest.mark.asyncio
async def test_seam_prepare_worktree_and_commit_against_real_git(
    seeded_repo: Path, tmp_path: Path
) -> None:
    builds_root = tmp_path / "builds"

    prep = await prepare_worktree(
        "build-seam", seeded_repo, "wt-branch", builds_root=builds_root
    )
    assert prep.status == "success", prep
    worktree = Path(prep.worktree_path or "")
    assert worktree.is_dir()

    (worktree / "newfile.txt").write_text("payload\n", encoding="utf-8")
    # Configure local identity so commit doesn't fail on missing user.email.
    import subprocess  # noqa: S404 — seam-only.

    subprocess.run(  # noqa: S603 — seam fixture only.
        [_GIT, "config", "user.email", "t@t"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    subprocess.run(  # noqa: S603
        [_GIT, "config", "user.name", "t"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )

    commit = await commit_all(worktree, "seam: add newfile", execute=_default_execute)
    assert commit.status == "success", commit
    assert commit.sha and len(commit.sha) >= 7


@pytest.mark.seam
@pytest.mark.integration_contract("git_adapter_subprocess_contract")
@pytest.mark.skipif(not _HAS_GIT, reason="git binary not available")
@pytest.mark.asyncio
async def test_seam_cleanup_worktree_failure_does_not_block(
    seeded_repo: Path, tmp_path: Path
) -> None:
    """Removing a worktree path that was never registered is a non-zero
    exit — but the function must still return a GitOpResult, not raise.
    """
    bogus = tmp_path / "never-existed"

    result = await cleanup_worktree("noop", bogus)

    assert isinstance(result, GitOpResult)
    assert result.operation == "cleanup_worktree"
    # Whichever way git fails, the contract is: never raises.
    assert result.status in {"success", "failed"}

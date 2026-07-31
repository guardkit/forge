"""Tests for the fix journey's git commit probe.

The conductor's revival, Stage 1b (design pass §a.3, owner lineage
TASK-MBC8-007). The probe fills the one seam the Mode C terminal handler
declared and nothing implemented — without it the handler raises on the
branch that splits "hand back a gates-green branch" from "ended quietly,
nothing changed".

Coverage map:

* The happy path issues exactly one list-token ``git rev-list --count``
  in the build's recorded worktree — :class:`TestProbeCommand`.
* Every failure mode returns ``failed=True`` and never a quiet zero —
  :class:`TestFailuresAreLoud`. A probe that cannot answer must never be
  read as "no commits": that silently throws real fix work away.
* The probe pairs with the real
  :func:`~forge.pipeline.terminal_handlers.mode_c.evaluate_terminal` and
  drives its two commit-dependent outcomes —
  :class:`TestWiredToTheTerminalHandler`.

Every test injects a fake ``execute``: no git process is spawned, no
repository is touched, and nothing goes near a network or a broker.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any, Sequence

import pytest

from forge.lifecycle.modes import BuildMode
from forge.lifecycle.persistence import Build
from forge.lifecycle.state_machine import BuildState
from forge.pipeline.mode_c_commit_probe import (
    DEFAULT_BASE_BRANCH,
    make_mode_c_commit_probe,
)
from forge.pipeline.mode_c_planner import StageEntry
from forge.pipeline.stage_taxonomy import StageClass
from forge.pipeline.terminal_handlers.mode_c import (
    ModeCTerminal,
    evaluate_terminal,
)


_BUILD = Build(
    build_id="build-fix-1", status=BuildState.RUNNING, mode=BuildMode.MODE_C
)
_WORKTREE = "/srv/forge-builds/build-fix-1"


def _run(coro: Any) -> Any:
    """Drive a coroutine — the suite does not depend on pytest-asyncio."""
    return asyncio.run(coro)


class _FakePool:
    """Two-line stand-in for the lifecycle persistence facade."""

    def __init__(self, row: Any, *, raises: Exception | None = None) -> None:
        self._row = row
        self._raises = raises
        self.calls: list[str] = []

    def get_build_row(self, build_id: str) -> Any:
        self.calls.append(build_id)
        if self._raises is not None:
            raise self._raises
        return self._row


class _FakeExecute:
    """Records the git invocations it is asked to run."""

    def __init__(
        self,
        *,
        exit_code: int = 0,
        stdout: str = "0",
        stderr: str = "",
        raises: Exception | None = None,
    ) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        *,
        command: Sequence[str],
        cwd: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        self.calls.append(
            {"command": list(command), "cwd": cwd, "timeout": timeout}
        )
        if self.raises is not None:
            raise self.raises
        return SimpleNamespace(
            exit_code=self.exit_code, stdout=self.stdout, stderr=self.stderr
        )


def _row(worktree_path: str | None = _WORKTREE) -> SimpleNamespace:
    return SimpleNamespace(
        build_id=_BUILD.build_id,
        branch="lane/fix-journey",
        worktree_path=worktree_path,
    )


# ---------------------------------------------------------------------------


class TestProbeCommand:
    """The single git call, and what it is made of."""

    def test_counts_commits_in_the_recorded_worktree(self) -> None:
        execute = _FakeExecute(stdout="3\n")
        probe = make_mode_c_commit_probe(_FakePool(_row()), execute=execute)

        result = _run(probe(_BUILD))

        assert result.failed is False
        assert result.count == 3
        assert result.has_commits is True
        assert len(execute.calls) == 1
        assert execute.calls[0]["command"] == [
            "git",
            "rev-list",
            "--count",
            f"{DEFAULT_BASE_BRANCH}..HEAD",
        ]
        assert execute.calls[0]["cwd"] == _WORKTREE
        assert execute.calls[0]["timeout"] is not None

    def test_zero_commits_is_a_successful_probe(self) -> None:
        execute = _FakeExecute(stdout="0")
        probe = make_mode_c_commit_probe(_FakePool(_row()), execute=execute)

        result = _run(probe(_BUILD))

        assert result.failed is False
        assert result.count == 0
        assert result.has_commits is False

    def test_base_branch_is_configurable(self) -> None:
        execute = _FakeExecute(stdout="1")
        probe = make_mode_c_commit_probe(
            _FakePool(_row()), base_branch="release/2026-07", execute=execute
        )

        _run(probe(_BUILD))

        assert execute.calls[0]["command"][-1] == "release/2026-07..HEAD"

    def test_command_is_list_tokens_with_no_shell_metacharacters(self) -> None:
        # Defence against the shell-injection shape: a branch name with
        # metacharacters stays one argv token.
        execute = _FakeExecute(stdout="0")
        probe = make_mode_c_commit_probe(
            _FakePool(_row()), base_branch="main; rm -rf /", execute=execute
        )

        _run(probe(_BUILD))

        assert execute.calls[0]["command"] == [
            "git",
            "rev-list",
            "--count",
            "main; rm -rf /..HEAD",
        ]

    def test_empty_base_branch_is_refused_at_wiring_time(self) -> None:
        with pytest.raises(ValueError, match="base_branch"):
            make_mode_c_commit_probe(_FakePool(_row()), base_branch="   ")

    def test_allowlist_is_consulted_when_supplied(self) -> None:
        execute = _FakeExecute(stdout="2")
        seen: list[tuple[str, str]] = []

        class _Allow:
            def is_allowed(self, build_id: str, path: str) -> bool:
                seen.append((build_id, path))
                return True

        probe = make_mode_c_commit_probe(
            _FakePool(_row()), execute=execute, worktree_allowlist=_Allow()
        )

        assert _run(probe(_BUILD)).count == 2
        assert seen == [(_BUILD.build_id, _WORKTREE)]


# ---------------------------------------------------------------------------


class TestFailuresAreLoud:
    """Never a quiet zero — the handler must see ``failed=True``."""

    @staticmethod
    def _assert_failed(result: Any, fragment: str) -> None:
        assert result.failed is True
        assert result.count == 0
        assert result.has_commits is False
        assert result.error is not None
        assert fragment in result.error

    def test_missing_build_row(self) -> None:
        probe = make_mode_c_commit_probe(_FakePool(None), execute=_FakeExecute())

        self._assert_failed(_run(probe(_BUILD)), "no builds row")

    def test_pool_raises(self) -> None:
        probe = make_mode_c_commit_probe(
            _FakePool(None, raises=RuntimeError("db locked")),
            execute=_FakeExecute(),
        )

        self._assert_failed(_run(probe(_BUILD)), "db locked")

    def test_no_recorded_worktree_path(self) -> None:
        probe = make_mode_c_commit_probe(
            _FakePool(_row(worktree_path=None)), execute=_FakeExecute()
        )

        self._assert_failed(_run(probe(_BUILD)), "no recorded worktree_path")

    def test_blank_recorded_worktree_path(self) -> None:
        probe = make_mode_c_commit_probe(
            _FakePool(_row(worktree_path="   ")), execute=_FakeExecute()
        )

        self._assert_failed(_run(probe(_BUILD)), "no recorded worktree_path")

    def test_allowlist_denial(self) -> None:
        class _Deny:
            def is_allowed(self, build_id: str, path: str) -> bool:
                return False

        execute = _FakeExecute()
        probe = make_mode_c_commit_probe(
            _FakePool(_row()), execute=execute, worktree_allowlist=_Deny()
        )

        self._assert_failed(_run(probe(_BUILD)), "allowlist denied")
        assert execute.calls == [], "git must not run after a denial"

    def test_raising_allowlist_is_treated_as_a_denial(self) -> None:
        class _Boom:
            def is_allowed(self, build_id: str, path: str) -> bool:
                raise OSError("allowlist unavailable")

        probe = make_mode_c_commit_probe(
            _FakePool(_row()), execute=_FakeExecute(), worktree_allowlist=_Boom()
        )

        self._assert_failed(_run(probe(_BUILD)), "allowlist unavailable")

    def test_non_zero_git_exit_carries_stderr(self) -> None:
        probe = make_mode_c_commit_probe(
            _FakePool(_row()),
            execute=_FakeExecute(
                exit_code=128, stdout="", stderr="fatal: bad revision 'main'"
            ),
        )

        self._assert_failed(_run(probe(_BUILD)), "fatal: bad revision 'main'")

    def test_unparseable_stdout(self) -> None:
        probe = make_mode_c_commit_probe(
            _FakePool(_row()), execute=_FakeExecute(stdout="lots, probably")
        )

        self._assert_failed(_run(probe(_BUILD)), "unparseable output")

    def test_empty_stdout(self) -> None:
        probe = make_mode_c_commit_probe(
            _FakePool(_row()), execute=_FakeExecute(stdout="")
        )

        self._assert_failed(_run(probe(_BUILD)), "unparseable output")

    def test_execute_raises(self) -> None:
        probe = make_mode_c_commit_probe(
            _FakePool(_row()),
            execute=_FakeExecute(raises=FileNotFoundError("no such directory")),
        )

        self._assert_failed(_run(probe(_BUILD)), "no such directory")

    def test_failures_log_loudly(self, caplog: pytest.LogCaptureFixture) -> None:
        probe = make_mode_c_commit_probe(_FakePool(None), execute=_FakeExecute())

        with caplog.at_level(logging.WARNING):
            _run(probe(_BUILD))

        assert "mode_c_commit_probe_failed" in caplog.text


# ---------------------------------------------------------------------------


class TestWiredToTheTerminalHandler:
    """The pair the design pass says is wired together or not at all."""

    _HISTORY: tuple[StageEntry, ...] = (
        StageEntry(
            stage_class=StageClass.TASK_REVIEW,
            status="approved",
            fix_tasks=("FIX-001",),
        ),
        StageEntry(
            stage_class=StageClass.TASK_WORK,
            status="approved",
            fix_task_id="FIX-001",
        ),
        StageEntry(stage_class=StageClass.TASK_REVIEW, status="approved"),
    )

    def test_commits_present_routes_to_the_merge_ready_checkpoint(self) -> None:
        probe = make_mode_c_commit_probe(
            _FakePool(_row()), execute=_FakeExecute(stdout="4")
        )

        decision = _run(
            evaluate_terminal(_BUILD, self._HISTORY, commit_probe=probe)
        )

        assert decision.outcome is ModeCTerminal.PR_REVIEW
        assert decision.has_commits is True

    def test_no_commits_ends_quietly(self) -> None:
        probe = make_mode_c_commit_probe(
            _FakePool(_row()), execute=_FakeExecute(stdout="0")
        )

        decision = _run(
            evaluate_terminal(_BUILD, self._HISTORY, commit_probe=probe)
        )

        assert decision.outcome is ModeCTerminal.CLEAN_REVIEW_NO_COMMITS
        assert decision.has_commits is False

    def test_a_failed_probe_is_failed_never_demoted_to_clean(self) -> None:
        probe = make_mode_c_commit_probe(
            _FakePool(_row()),
            execute=_FakeExecute(exit_code=128, stderr="fatal: not a git repo"),
        )

        decision = _run(
            evaluate_terminal(_BUILD, self._HISTORY, commit_probe=probe)
        )

        assert decision.outcome is ModeCTerminal.FAILED
        assert decision.rationale == "mode-c-commit-check-failed"
        assert "not a git repo" in (decision.failure_reason or "")

    def test_without_a_probe_the_handler_still_raises(self) -> None:
        # The reason the handler and the probe are wired as a pair.
        with pytest.raises(RuntimeError, match="commit_probe is required"):
            _run(evaluate_terminal(_BUILD, self._HISTORY, commit_probe=None))

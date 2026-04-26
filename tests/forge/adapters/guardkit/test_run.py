"""Unit and seam tests for :mod:`forge.adapters.guardkit.run` (TASK-GCI-008).

Tests cover the acceptance criteria for the GuardKit subprocess wrapper:

- AC-001 — ``run()`` is exported from
  ``src/forge/adapters/guardkit/run.py``.
- AC-002 — the command is composed as a list (never a shell string)
  shaped ``[guardkit, subcommand, *args, *context_flags, --nats?]``.
- AC-003 — ``cwd`` defence-in-depth: non-absolute or
  outside-allowlist ``repo_path`` produces a ``status="failed"``
  result with a ``cwd_outside_allowlist`` warning.
- AC-004 — :func:`resolve_context_flags` is invoked and its
  ``flags`` / ``warnings`` are propagated.
- AC-005 — Graphiti subcommands skip the resolver entirely.
- AC-006 — ``extra_context_paths`` are merged for *this call only*
  (no persistence; verified by two consecutive calls).
- AC-007 — ``with_nats_streaming=True`` appends ``--nats``;
  ``False`` omits it.
- AC-008 — timeout boundary: 599 → success, 600 → success-on-the-line,
  601 → ``status="timeout"``.
- AC-009 — :class:`asyncio.CancelledError` from the seam propagates
  out of ``run()`` (does NOT become a failed result).
- AC-010 — non-zero exit yields ``status="failed"`` with ``stderr``
  and ``exit_code`` populated.
- AC-011 — :class:`PermissionError` from the seam (binary not in
  shell allowlist) yields ``status="failed"`` with a
  ``permissions_refused`` warning.
- AC-012 — generic exceptions inside the body never propagate;
  they become ``wrapper_internal_error`` warnings on a failed result.
- AC-013 — two parallel ``run()`` calls return independent results
  (no shared mutable state).

The seam is :func:`forge.adapters.guardkit.run._execute_subprocess`,
patched per-test to feed deterministic outputs through the wrapper.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from forge.adapters.guardkit import run as run_module
from forge.adapters.guardkit.context_resolver import ResolvedContext
from forge.adapters.guardkit.models import GuardKitResult, GuardKitWarning
from forge.adapters.guardkit.run import run

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def worktree(tmp_path: Path) -> Path:
    """Create an absolute, allowlist-eligible working directory."""
    repo = tmp_path / "build"
    repo.mkdir()
    return repo


@pytest.fixture()
def allowlist(worktree: Path) -> list[Path]:
    """Read allowlist that includes the worktree."""
    return [worktree.parent]


def _stub_execute(
    *,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
    duration_secs: float = 1.0,
    timed_out: bool = False,
    capture: dict[str, Any] | None = None,
):
    """Build an async stub for ``_execute_subprocess``.

    If ``capture`` is supplied, the stub records its kwargs into it for
    assertion. The stub returns the canonical
    ``(stdout, stderr, exit_code, duration_secs, timed_out)`` tuple.
    """

    async def _stub(*, command: list[str], cwd: str, timeout: int):
        if capture is not None:
            capture["command"] = list(command)
            capture["cwd"] = cwd
            capture["timeout"] = timeout
        return (stdout, stderr, exit_code, duration_secs, timed_out)

    return _stub


def _empty_resolved() -> ResolvedContext:
    return ResolvedContext(flags=[], paths=[], warnings=[])


# ---------------------------------------------------------------------------
# AC-001 — module surface
# ---------------------------------------------------------------------------


class TestRunModuleSurface:
    """AC-001 — `run()` is defined where the contract says it is."""

    def test_run_module_path_is_under_guardkit_adapter(self) -> None:
        module_file = inspect.getsourcefile(run_module) or ""
        assert module_file.endswith("forge/adapters/guardkit/run.py")

    def test_run_function_is_a_coroutine_function(self) -> None:
        assert asyncio.iscoroutinefunction(run)

    def test_run_function_is_defined_in_run_module(self) -> None:
        assert run.__module__ == "forge.adapters.guardkit.run"


# ---------------------------------------------------------------------------
# AC-002 — command composition
# ---------------------------------------------------------------------------


class TestCommandComposition:
    """AC-002 — command list is shaped correctly and passed as a list."""

    @pytest.mark.asyncio()
    async def test_command_starts_with_guardkit_binary_subcommand_then_args(
        self,
        worktree: Path,
        allowlist: list[Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        capture: dict[str, Any] = {}
        monkeypatch.setattr(
            run_module,
            "_execute_subprocess",
            _stub_execute(capture=capture),
        )
        monkeypatch.setattr(
            run_module, "resolve_context_flags",
            lambda *a, **kw: _empty_resolved(),
        )

        await run(
            subcommand="feature-spec",
            args=["--feature", "FEAT-001"],
            repo_path=worktree,
            read_allowlist=allowlist,
            with_nats_streaming=False,
        )

        cmd = capture["command"]
        assert isinstance(cmd, list)
        assert cmd[0] == "/usr/local/bin/guardkit"
        assert cmd[1] == "feature-spec"
        assert cmd[2:4] == ["--feature", "FEAT-001"]

    @pytest.mark.asyncio()
    async def test_cwd_passes_resolved_worktree_to_seam(
        self,
        worktree: Path,
        allowlist: list[Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        capture: dict[str, Any] = {}
        monkeypatch.setattr(
            run_module,
            "_execute_subprocess",
            _stub_execute(capture=capture),
        )
        monkeypatch.setattr(
            run_module, "resolve_context_flags",
            lambda *a, **kw: _empty_resolved(),
        )

        await run(
            subcommand="feature-spec",
            args=[],
            repo_path=worktree,
            read_allowlist=allowlist,
            with_nats_streaming=False,
        )

        assert capture["cwd"] == str(worktree.resolve(strict=False))


# ---------------------------------------------------------------------------
# AC-003 — cwd allowlist enforcement
# ---------------------------------------------------------------------------


class TestCwdAllowlistEnforcement:
    """AC-003 — relative paths and outside-allowlist paths are refused."""

    @pytest.mark.asyncio()
    async def test_relative_repo_path_is_refused_with_warning(
        self,
        allowlist: list[Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        called: dict[str, bool] = {"executed": False}

        async def _should_not_run(**_: Any):
            called["executed"] = True
            return ("", "", 0, 0.0, False)

        monkeypatch.setattr(run_module, "_execute_subprocess", _should_not_run)

        result = await run(
            subcommand="feature-spec",
            args=[],
            repo_path=Path("relative/build"),
            read_allowlist=allowlist,
        )

        assert result.status == "failed"
        assert called["executed"] is False
        codes = [w.code for w in result.warnings]
        assert "cwd_outside_allowlist" in codes

    @pytest.mark.asyncio()
    async def test_outside_allowlist_repo_path_is_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        outside = tmp_path / "outside" / "build"
        outside.mkdir(parents=True)
        allowlist_root = tmp_path / "allowed"
        allowlist_root.mkdir()

        async def _should_not_run(**_: Any):  # pragma: no cover — defensive
            raise AssertionError("subprocess must not be invoked")

        monkeypatch.setattr(run_module, "_execute_subprocess", _should_not_run)

        result = await run(
            subcommand="feature-spec",
            args=[],
            repo_path=outside,
            read_allowlist=[allowlist_root],
        )

        assert result.status == "failed"
        assert any(
            w.code == "cwd_outside_allowlist" for w in result.warnings
        )


# ---------------------------------------------------------------------------
# AC-004 — resolver integration
# ---------------------------------------------------------------------------


class TestResolverIntegration:
    """AC-004 — resolver flags + warnings are surfaced on the result."""

    @pytest.mark.asyncio()
    async def test_resolver_flags_prepended_to_command(
        self,
        worktree: Path,
        allowlist: list[Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        resolved = ResolvedContext(
            flags=["--context", "/abs/doc1.md", "--context", "/abs/doc2.md"],
            paths=["/abs/doc1.md", "/abs/doc2.md"],
            warnings=[],
        )
        monkeypatch.setattr(
            run_module, "resolve_context_flags", lambda *a, **kw: resolved
        )
        capture: dict[str, Any] = {}
        monkeypatch.setattr(
            run_module,
            "_execute_subprocess",
            _stub_execute(capture=capture),
        )

        await run(
            subcommand="feature-spec",
            args=["--feature", "F1"],
            repo_path=worktree,
            read_allowlist=allowlist,
            with_nats_streaming=False,
        )

        cmd = capture["command"]
        # Context flags follow positional args, before --nats (omitted).
        assert "--context" in cmd
        assert "/abs/doc1.md" in cmd
        assert "/abs/doc2.md" in cmd

    @pytest.mark.asyncio()
    async def test_resolver_warnings_surface_on_result(
        self,
        worktree: Path,
        allowlist: list[Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        warning = GuardKitWarning(
            code="context_manifest_missing",
            message="manifest absent",
        )
        resolved = ResolvedContext(flags=[], paths=[], warnings=[warning])
        monkeypatch.setattr(
            run_module, "resolve_context_flags", lambda *a, **kw: resolved
        )
        monkeypatch.setattr(
            run_module, "_execute_subprocess", _stub_execute()
        )

        result = await run(
            subcommand="feature-spec",
            args=[],
            repo_path=worktree,
            read_allowlist=allowlist,
        )

        codes = [w.code for w in result.warnings]
        assert "context_manifest_missing" in codes


# ---------------------------------------------------------------------------
# AC-005 — Graphiti bypass
# ---------------------------------------------------------------------------


class TestGraphitiBypass:
    """AC-005 — Graphiti subcommands skip the resolver entirely."""

    @pytest.mark.asyncio()
    async def test_graphiti_add_context_skips_resolver(
        self,
        worktree: Path,
        allowlist: list[Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        called: dict[str, int] = {"resolver": 0}

        def _spy_resolver(*_a: Any, **_kw: Any) -> ResolvedContext:
            called["resolver"] += 1
            return _empty_resolved()

        monkeypatch.setattr(run_module, "resolve_context_flags", _spy_resolver)
        monkeypatch.setattr(
            run_module, "_execute_subprocess", _stub_execute()
        )

        result = await run(
            subcommand="graphiti add-context",
            args=["--episode", "build-001"],
            repo_path=worktree,
            read_allowlist=allowlist,
        )

        assert called["resolver"] == 0
        assert result.status == "success"


# ---------------------------------------------------------------------------
# AC-006 — extra context paths (retry path)
# ---------------------------------------------------------------------------


class TestExtraContextPaths:
    """AC-006 — extra paths merged per call, never persisted."""

    @pytest.mark.asyncio()
    async def test_extra_context_paths_appended_to_command(
        self,
        worktree: Path,
        allowlist: list[Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            run_module, "resolve_context_flags",
            lambda *a, **kw: _empty_resolved(),
        )
        capture: dict[str, Any] = {}
        monkeypatch.setattr(
            run_module, "_execute_subprocess",
            _stub_execute(capture=capture),
        )

        await run(
            subcommand="feature-spec",
            args=[],
            repo_path=worktree,
            read_allowlist=allowlist,
            extra_context_paths=["/extra/spec.md", "/extra/decision.md"],
            with_nats_streaming=False,
        )

        cmd = capture["command"]
        # Each path becomes its own --context flag.
        assert cmd.count("--context") == 2
        assert "/extra/spec.md" in cmd
        assert "/extra/decision.md" in cmd

    @pytest.mark.asyncio()
    async def test_extra_context_paths_not_persisted_between_calls(
        self,
        worktree: Path,
        allowlist: list[Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            run_module, "resolve_context_flags",
            lambda *a, **kw: _empty_resolved(),
        )
        captures: list[dict[str, Any]] = [{}, {}]

        async def _stub(*, command: list[str], cwd: str, timeout: int):
            slot = captures[0] if "command" not in captures[0] else captures[1]
            slot["command"] = list(command)
            return ("", "", 0, 1.0, False)

        monkeypatch.setattr(run_module, "_execute_subprocess", _stub)

        await run(
            subcommand="feature-spec",
            args=[],
            repo_path=worktree,
            read_allowlist=allowlist,
            extra_context_paths=["/extra/once.md"],
            with_nats_streaming=False,
        )
        await run(
            subcommand="feature-spec",
            args=[],
            repo_path=worktree,
            read_allowlist=allowlist,
            with_nats_streaming=False,
        )

        assert "/extra/once.md" in captures[0]["command"]
        assert "/extra/once.md" not in captures[1]["command"]


# ---------------------------------------------------------------------------
# AC-007 — NATS flag
# ---------------------------------------------------------------------------


class TestNatsFlag:
    """AC-007 — ``--nats`` toggles via ``with_nats_streaming``."""

    @pytest.mark.asyncio()
    async def test_nats_flag_appended_when_streaming_enabled(
        self,
        worktree: Path,
        allowlist: list[Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            run_module, "resolve_context_flags",
            lambda *a, **kw: _empty_resolved(),
        )
        capture: dict[str, Any] = {}
        monkeypatch.setattr(
            run_module, "_execute_subprocess",
            _stub_execute(capture=capture),
        )

        await run(
            subcommand="feature-spec",
            args=[],
            repo_path=worktree,
            read_allowlist=allowlist,
            with_nats_streaming=True,
        )
        assert "--nats" in capture["command"]

    @pytest.mark.asyncio()
    async def test_nats_flag_omitted_when_streaming_disabled(
        self,
        worktree: Path,
        allowlist: list[Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            run_module, "resolve_context_flags",
            lambda *a, **kw: _empty_resolved(),
        )
        capture: dict[str, Any] = {}
        monkeypatch.setattr(
            run_module, "_execute_subprocess",
            _stub_execute(capture=capture),
        )

        await run(
            subcommand="feature-spec",
            args=[],
            repo_path=worktree,
            read_allowlist=allowlist,
            with_nats_streaming=False,
        )
        assert "--nats" not in capture["command"]


# ---------------------------------------------------------------------------
# AC-008 — timeout boundary
# ---------------------------------------------------------------------------


@pytest.mark.seam
@pytest.mark.integration_contract("guardkit_subprocess_contract")
class TestTimeoutBoundary:
    """AC-008 — boundary tests at 599 / 600 / 601 seconds."""

    @pytest.mark.asyncio()
    async def test_599_seconds_is_reported_as_success(
        self,
        worktree: Path,
        allowlist: list[Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            run_module, "resolve_context_flags",
            lambda *a, **kw: _empty_resolved(),
        )
        monkeypatch.setattr(
            run_module, "_execute_subprocess",
            _stub_execute(duration_secs=599.0, timed_out=False),
        )

        result = await run(
            subcommand="feature-spec",
            args=[],
            repo_path=worktree,
            read_allowlist=allowlist,
            timeout_seconds=600,
        )
        assert result.status == "success"
        assert result.duration_secs == pytest.approx(599.0)

    @pytest.mark.asyncio()
    async def test_600_seconds_exact_is_reported_as_success_when_seam_completes(
        self,
        worktree: Path,
        allowlist: list[Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # When the seam reports completion at exactly 600s with
        # timed_out=False, the parser must follow that signal.
        monkeypatch.setattr(
            run_module, "resolve_context_flags",
            lambda *a, **kw: _empty_resolved(),
        )
        monkeypatch.setattr(
            run_module, "_execute_subprocess",
            _stub_execute(duration_secs=600.0, timed_out=False),
        )

        result = await run(
            subcommand="feature-spec",
            args=[],
            repo_path=worktree,
            read_allowlist=allowlist,
            timeout_seconds=600,
        )
        assert result.status == "success"

    @pytest.mark.asyncio()
    async def test_601_seconds_is_reported_as_timeout(
        self,
        worktree: Path,
        allowlist: list[Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            run_module, "resolve_context_flags",
            lambda *a, **kw: _empty_resolved(),
        )
        monkeypatch.setattr(
            run_module, "_execute_subprocess",
            _stub_execute(duration_secs=601.0, timed_out=True, exit_code=-1),
        )

        result = await run(
            subcommand="feature-spec",
            args=[],
            repo_path=worktree,
            read_allowlist=allowlist,
            timeout_seconds=600,
        )
        assert result.status == "timeout"


# ---------------------------------------------------------------------------
# AC-009 — cancellation propagation
# ---------------------------------------------------------------------------


@pytest.mark.seam
@pytest.mark.integration_contract("guardkit_subprocess_contract")
class TestCancellationPropagation:
    """AC-009 — :class:`asyncio.CancelledError` re-raises out of run()."""

    @pytest.mark.asyncio()
    async def test_cancelled_error_in_seam_propagates(
        self,
        worktree: Path,
        allowlist: list[Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            run_module, "resolve_context_flags",
            lambda *a, **kw: _empty_resolved(),
        )

        async def _cancelled(**_: Any):
            raise asyncio.CancelledError()

        monkeypatch.setattr(run_module, "_execute_subprocess", _cancelled)

        with pytest.raises(asyncio.CancelledError):
            await run(
                subcommand="feature-spec",
                args=[],
                repo_path=worktree,
                read_allowlist=allowlist,
            )


# ---------------------------------------------------------------------------
# AC-010 — non-zero exit
# ---------------------------------------------------------------------------


class TestNonZeroExit:
    """AC-010 — non-zero exit becomes ``status="failed"``."""

    @pytest.mark.asyncio()
    async def test_non_zero_exit_yields_failed_status_with_stderr(
        self,
        worktree: Path,
        allowlist: list[Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            run_module, "resolve_context_flags",
            lambda *a, **kw: _empty_resolved(),
        )
        monkeypatch.setattr(
            run_module, "_execute_subprocess",
            _stub_execute(
                stdout="",
                stderr="boom: validation failed",
                exit_code=2,
                duration_secs=0.5,
                timed_out=False,
            ),
        )

        result = await run(
            subcommand="feature-spec",
            args=[],
            repo_path=worktree,
            read_allowlist=allowlist,
        )

        assert result.status == "failed"
        assert result.exit_code == 2
        assert result.stderr == "boom: validation failed"


# ---------------------------------------------------------------------------
# AC-011 — permissions refused
# ---------------------------------------------------------------------------


class TestPermissionsRefused:
    """AC-011 — :class:`PermissionError` becomes ``permissions_refused``."""

    @pytest.mark.asyncio()
    async def test_permission_error_yields_permissions_refused_warning(
        self,
        worktree: Path,
        allowlist: list[Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            run_module, "resolve_context_flags",
            lambda *a, **kw: _empty_resolved(),
        )

        async def _refuse(**_: Any):
            raise PermissionError("binary not in shell allowlist")

        monkeypatch.setattr(run_module, "_execute_subprocess", _refuse)

        result = await run(
            subcommand="feature-spec",
            args=[],
            repo_path=worktree,
            read_allowlist=allowlist,
        )

        assert result.status == "failed"
        assert any(
            w.code == "permissions_refused" for w in result.warnings
        )


# ---------------------------------------------------------------------------
# AC-012 — wrapper internal errors
# ---------------------------------------------------------------------------


class TestWrapperInternalError:
    """AC-012 — generic exceptions are folded into a structured warning."""

    @pytest.mark.asyncio()
    async def test_unexpected_exception_in_seam_yields_failed_result(
        self,
        worktree: Path,
        allowlist: list[Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            run_module, "resolve_context_flags",
            lambda *a, **kw: _empty_resolved(),
        )

        async def _explode(**_: Any):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(run_module, "_execute_subprocess", _explode)

        result = await run(
            subcommand="feature-spec",
            args=[],
            repo_path=worktree,
            read_allowlist=allowlist,
        )

        assert result.status == "failed"
        codes = [w.code for w in result.warnings]
        assert "wrapper_internal_error" in codes
        details = next(
            w.details for w in result.warnings
            if w.code == "wrapper_internal_error"
        )
        assert details["exception_type"] == "RuntimeError"


# ---------------------------------------------------------------------------
# AC-013 — parallel-call isolation
# ---------------------------------------------------------------------------


@pytest.mark.seam
@pytest.mark.integration_contract("guardkit_subprocess_contract")
class TestParallelCallIsolation:
    """AC-013 — concurrent calls do not corrupt each other's results."""

    @pytest.mark.asyncio()
    async def test_two_parallel_calls_return_independent_results(
        self,
        worktree: Path,
        allowlist: list[Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            run_module, "resolve_context_flags",
            lambda *a, **kw: _empty_resolved(),
        )

        async def _stub(*, command: list[str], cwd: str, timeout: int):
            # Yield once so the event loop can interleave the two calls.
            await asyncio.sleep(0)
            sub = command[1]
            return (f"out:{sub}", "", 0, 1.0, False)

        monkeypatch.setattr(run_module, "_execute_subprocess", _stub)

        results = await asyncio.gather(
            run(
                subcommand="feature-spec",
                args=[],
                repo_path=worktree,
                read_allowlist=allowlist,
            ),
            run(
                subcommand="feature-plan",
                args=[],
                repo_path=worktree,
                read_allowlist=allowlist,
            ),
        )

        assert results[0] is not results[1]
        assert results[0].subcommand == "feature-spec"
        assert results[1].subcommand == "feature-plan"
        assert "feature-spec" in results[0].stdout_tail
        assert "feature-plan" in results[1].stdout_tail

"""The GuardKit binary-resolution ladder (design pass 2026-08-02 §d, venue B).

The one-shot subprocess seam used to hardcode
``_GUARDKIT_BINARY = "/usr/local/bin/guardkit"``. That file exists only inside
the container image, so **outside it the seam could not spawn at all** — the
2026-08-01 replay's wall. This module pins the lift: the same ladder the
long-running autobuild path already walks
(:func:`forge.subagents.autobuild_runner._resolve_guardkit_path`), extended
with the local launcher the installer writes.

What is asserted, rung by rung:

- **R1** ``FORGE_GUARDKIT_PATH`` wins when it points at an executable file —
  even when ``PATH`` also carries one; a set-but-unusable value warns and
  falls through rather than failing the dispatch.
- **R2** ``PATH`` lookup wins next, **in PATH order**, so the container's
  ``/usr/local/bin/guardkit`` still wins whenever it is present; the found
  path is handed to the spawn **unresolved** (the container's entry is a
  symlink to ``/opt/venv/bin/guardkit-py`` and the argv must name the path
  the image installs).
- **R3** ``~/.agentecflow/bin/guardkit`` is the last rung, and only when it is
  actually executable.
- **Failure** is the boundary's existing honest shape — never a raise, never a
  bare ``FileNotFoundError`` from the spawn: ``status="failed"``,
  ``exit_code=-1``, warning ``guardkit_binary_not_found`` **naming every rung
  searched**. A failure is not cached.
- **Memoisation** — resolved once per process, logged once at INFO, and the
  resolution happens lazily (a refused ``cwd`` never resolves a binary).
- **The spawn's argv[0] is the resolved path**, with the rest of the argv
  shape (subcommand, args, ``--context``, ``--nats``) untouched.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any

import pytest

from forge.adapters.guardkit import run as run_module
from forge.adapters.guardkit.context_resolver import ResolvedContext
from forge.adapters.guardkit.run import run

RUN_LOGGER = "forge.adapters.guardkit.run"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def worktree(tmp_path: Path) -> Path:
    repo = tmp_path / "build"
    repo.mkdir()
    return repo


@pytest.fixture()
def allowlist(worktree: Path) -> list[Path]:
    return [worktree.parent]


@pytest.fixture(autouse=True)
def _blank_ladder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Start every test from an unresolved, empty ladder.

    Clears the memo, drops the env rung (the root conftest's fence sets it),
    empties ``PATH``, and points the ``~`` rung at an empty home. Each test
    then installs exactly the rungs it is about to assert on.
    """
    monkeypatch.setattr(run_module, "_resolved_guardkit_binary", None)
    monkeypatch.delenv(run_module.GUARDKIT_PATH_ENV, raising=False)
    monkeypatch.setenv("PATH", "")
    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    monkeypatch.setenv("HOME", str(empty_home))


def _make_exe(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    """Create an executable file at ``path`` (parents included)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def _stub_execute(capture: dict[str, Any] | None = None, *, calls: list | None = None):
    async def _stub(*, command: list[str], cwd: str, timeout: int):
        if capture is not None:
            capture["command"] = list(command)
            capture["cwd"] = cwd
        if calls is not None:
            calls.append(list(command))
        return ("", "", 0, 1.0, False)

    return _stub


def _no_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        run_module,
        "resolve_context_flags",
        lambda *a, **kw: ResolvedContext(flags=[], paths=[], warnings=[]),
    )


async def _drive(
    worktree: Path,
    allowlist: list[Path],
    *,
    subcommand: str = "task-review",
    args: list[str] | None = None,
    **kwargs: Any,
):
    return await run(
        subcommand=subcommand,
        args=args if args is not None else [],
        repo_path=worktree,
        read_allowlist=allowlist,
        with_nats_streaming=kwargs.pop("with_nats_streaming", False),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Rung 1 — FORGE_GUARDKIT_PATH
# ---------------------------------------------------------------------------


class TestEnvRung:
    """R1 — the explicit operator override leads the ladder."""

    @pytest.mark.asyncio()
    async def test_env_override_wins_over_a_path_hit(
        self,
        worktree: Path,
        allowlist: list[Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chosen = _make_exe(tmp_path / "override" / "guardkit")
        on_path = _make_exe(tmp_path / "path-dir" / "guardkit")
        monkeypatch.setenv(run_module.GUARDKIT_PATH_ENV, str(chosen))
        monkeypatch.setenv("PATH", str(on_path.parent))
        _no_context(monkeypatch)
        capture: dict[str, Any] = {}
        monkeypatch.setattr(run_module, "_execute_subprocess", _stub_execute(capture))

        result = await _drive(worktree, allowlist)

        assert result.status == "success"
        assert capture["command"][0] == str(chosen)

    @pytest.mark.asyncio()
    async def test_env_override_expands_user_home(
        self,
        worktree: Path,
        allowlist: list[Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        home = tmp_path / "home"
        chosen = _make_exe(home / "tools" / "guardkit")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv(run_module.GUARDKIT_PATH_ENV, "~/tools/guardkit")
        _no_context(monkeypatch)
        capture: dict[str, Any] = {}
        monkeypatch.setattr(run_module, "_execute_subprocess", _stub_execute(capture))

        await _drive(worktree, allowlist)

        assert capture["command"][0] == str(chosen)

    @pytest.mark.asyncio()
    async def test_unusable_env_override_warns_and_falls_through_to_path(
        self,
        worktree: Path,
        allowlist: list[Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        on_path = _make_exe(tmp_path / "path-dir" / "guardkit")
        missing = tmp_path / "nope" / "guardkit"
        monkeypatch.setenv(run_module.GUARDKIT_PATH_ENV, str(missing))
        monkeypatch.setenv("PATH", str(on_path.parent))
        _no_context(monkeypatch)
        capture: dict[str, Any] = {}
        monkeypatch.setattr(run_module, "_execute_subprocess", _stub_execute(capture))

        with caplog.at_level(logging.WARNING, logger=RUN_LOGGER):
            result = await _drive(worktree, allowlist)

        assert result.status == "success"
        assert capture["command"][0] == str(on_path)
        warned = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warned, "an unusable override must warn, not pass silently"
        assert run_module.GUARDKIT_PATH_ENV in warned[0].getMessage()

    @pytest.mark.asyncio()
    async def test_non_executable_env_override_is_rejected(
        self,
        worktree: Path,
        allowlist: list[Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Exists, but mode 0644 — a data file is not a launcher.
        not_exe = tmp_path / "data" / "guardkit"
        not_exe.parent.mkdir(parents=True)
        not_exe.write_text("not a program\n")
        not_exe.chmod(0o644)
        on_path = _make_exe(tmp_path / "path-dir" / "guardkit")
        monkeypatch.setenv(run_module.GUARDKIT_PATH_ENV, str(not_exe))
        monkeypatch.setenv("PATH", str(on_path.parent))
        _no_context(monkeypatch)
        capture: dict[str, Any] = {}
        monkeypatch.setattr(run_module, "_execute_subprocess", _stub_execute(capture))

        await _drive(worktree, allowlist)

        assert capture["command"][0] == str(on_path)


# ---------------------------------------------------------------------------
# Rung 2 — PATH
# ---------------------------------------------------------------------------


class TestPathRung:
    """R2 — the rung the container takes."""

    @pytest.mark.asyncio()
    async def test_path_hit_wins_when_env_is_unset(
        self,
        worktree: Path,
        allowlist: list[Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        on_path = _make_exe(tmp_path / "path-dir" / "guardkit")
        monkeypatch.setenv("PATH", str(on_path.parent))
        _no_context(monkeypatch)
        capture: dict[str, Any] = {}
        monkeypatch.setattr(run_module, "_execute_subprocess", _stub_execute(capture))

        result = await _drive(worktree, allowlist)

        assert result.status == "success"
        assert capture["command"][0] == str(on_path)

    @pytest.mark.asyncio()
    async def test_container_usr_local_bin_wins_over_a_later_path_entry(
        self,
        worktree: Path,
        allowlist: list[Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The image's shape: /opt/venv/bin leads PATH but ships only
        # ``guardkit-py``; /usr/local/bin carries the canonical ``guardkit``
        # name; a local launcher sits further down. PATH order must pick the
        # container's binary.
        venv_bin = tmp_path / "opt" / "venv" / "bin"
        _make_exe(venv_bin / "guardkit-py")
        usr_local = _make_exe(tmp_path / "usr" / "local" / "bin" / "guardkit")
        agentecflow = _make_exe(tmp_path / "home" / ".agentecflow" / "bin" / "guardkit")
        monkeypatch.setenv(
            "PATH",
            ":".join([str(venv_bin), str(usr_local.parent), str(agentecflow.parent)]),
        )
        _no_context(monkeypatch)
        capture: dict[str, Any] = {}
        monkeypatch.setattr(run_module, "_execute_subprocess", _stub_execute(capture))

        await _drive(worktree, allowlist)

        assert capture["command"][0] == str(usr_local)

    @pytest.mark.asyncio()
    async def test_path_hit_is_not_symlink_resolved(
        self,
        worktree: Path,
        allowlist: list[Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Exactly the image's Dockerfile line:
        #   ln -s /opt/venv/bin/guardkit-py /usr/local/bin/guardkit
        # The spawn must carry the installed path, not its target.
        target = _make_exe(tmp_path / "opt" / "venv" / "bin" / "guardkit-py")
        link_dir = tmp_path / "usr" / "local" / "bin"
        link_dir.mkdir(parents=True)
        link = link_dir / "guardkit"
        link.symlink_to(target)
        monkeypatch.setenv("PATH", str(link_dir))
        _no_context(monkeypatch)
        capture: dict[str, Any] = {}
        monkeypatch.setattr(run_module, "_execute_subprocess", _stub_execute(capture))

        await _drive(worktree, allowlist)

        assert capture["command"][0] == str(link)
        assert capture["command"][0] != str(target)


# ---------------------------------------------------------------------------
# Rung 3 — ~/.agentecflow/bin/guardkit
# ---------------------------------------------------------------------------


class TestAgentecflowRung:
    """R3 — the local installer's launcher, the fleet boxes' only copy."""

    @pytest.mark.asyncio()
    async def test_agentecflow_launcher_used_when_env_and_path_miss(
        self,
        worktree: Path,
        allowlist: list[Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        home = tmp_path / "home"
        launcher = _make_exe(home / ".agentecflow" / "bin" / "guardkit")
        monkeypatch.setenv("HOME", str(home))
        _no_context(monkeypatch)
        capture: dict[str, Any] = {}
        monkeypatch.setattr(run_module, "_execute_subprocess", _stub_execute(capture))

        result = await _drive(worktree, allowlist)

        assert result.status == "success"
        assert capture["command"][0] == str(launcher)

    @pytest.mark.asyncio()
    async def test_non_executable_agentecflow_launcher_is_not_used(
        self,
        worktree: Path,
        allowlist: list[Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        home = tmp_path / "home"
        launcher = home / ".agentecflow" / "bin" / "guardkit"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\n")
        launcher.chmod(0o644)
        monkeypatch.setenv("HOME", str(home))
        _no_context(monkeypatch)

        async def _must_not_run(**_: Any):  # pragma: no cover — defensive
            raise AssertionError("the seam must not be reached")

        monkeypatch.setattr(run_module, "_execute_subprocess", _must_not_run)

        result = await _drive(worktree, allowlist)

        assert result.status == "failed"
        assert [w.code for w in result.warnings] == ["guardkit_binary_not_found"]


# ---------------------------------------------------------------------------
# Every rung missed — the honest dispatch failure
# ---------------------------------------------------------------------------


class TestHonestResolutionFailure:
    """A missing binary is reported, never raised and never guessed at."""

    @pytest.mark.asyncio()
    async def test_failure_names_every_rung_that_was_searched(
        self,
        worktree: Path,
        allowlist: list[Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _no_context(monkeypatch)
        reached: dict[str, bool] = {"seam": False}

        async def _spy(**_: Any):
            reached["seam"] = True
            return ("", "", 0, 0.0, False)

        monkeypatch.setattr(run_module, "_execute_subprocess", _spy)

        result = await _drive(worktree, allowlist)

        assert result.status == "failed"
        assert result.exit_code == -1
        assert reached["seam"] is False, "nothing may spawn without a binary"

        warning = next(
            w for w in result.warnings if w.code == "guardkit_binary_not_found"
        )
        searched = warning.details["searched"]
        assert len(searched) == 3, searched
        joined = " ".join(searched)
        assert run_module.GUARDKIT_PATH_ENV in joined
        assert "PATH" in joined
        assert run_module._AGENTECFLOW_GUARDKIT in joined
        # The operator-facing text says the same thing on stderr.
        assert run_module.GUARDKIT_PATH_ENV in result.stderr
        assert run_module._AGENTECFLOW_GUARDKIT in result.stderr
        assert warning.details["binary_name"] == "guardkit"

    @pytest.mark.asyncio()
    async def test_failure_is_not_cached_so_a_later_install_is_picked_up(
        self,
        worktree: Path,
        allowlist: list[Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _no_context(monkeypatch)
        capture: dict[str, Any] = {}
        monkeypatch.setattr(run_module, "_execute_subprocess", _stub_execute(capture))

        first = await _drive(worktree, allowlist)
        assert first.status == "failed"

        installed = _make_exe(tmp_path / "late" / "guardkit")
        monkeypatch.setenv("PATH", str(installed.parent))

        second = await _drive(worktree, allowlist)

        assert second.status == "success"
        assert capture["command"][0] == str(installed)

    @pytest.mark.asyncio()
    async def test_resolution_failure_does_not_raise_past_the_boundary(
        self,
        worktree: Path,
        allowlist: list[Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _no_context(monkeypatch)
        # No seam patch at all: if the boundary ever tried to spawn, this
        # would surface as a FileNotFoundError instead of a result.
        result = await _drive(worktree, allowlist)
        assert result.status == "failed"
        assert result.subcommand == "task-review"


# ---------------------------------------------------------------------------
# Resolve-once semantics
# ---------------------------------------------------------------------------


class TestResolvedOnce:
    """Lazy, once per process, and logged once at INFO."""

    @pytest.mark.asyncio()
    async def test_path_lookup_runs_once_across_two_calls(
        self,
        worktree: Path,
        allowlist: list[Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        on_path = _make_exe(tmp_path / "path-dir" / "guardkit")
        monkeypatch.setenv("PATH", str(on_path.parent))
        _no_context(monkeypatch)
        calls: list[list[str]] = []
        monkeypatch.setattr(
            run_module, "_execute_subprocess", _stub_execute(calls=calls)
        )

        which_calls: list[str] = []
        real_which = shutil.which

        def _counting_which(name: str, *a: Any, **kw: Any):
            which_calls.append(name)
            return real_which(name, *a, **kw)

        monkeypatch.setattr(run_module.shutil, "which", _counting_which)

        await _drive(worktree, allowlist)
        await _drive(worktree, allowlist)

        assert len(calls) == 2
        assert which_calls == ["guardkit"], which_calls
        assert calls[0][0] == calls[1][0] == str(on_path)

    @pytest.mark.asyncio()
    async def test_resolved_path_is_logged_once_at_info(
        self,
        worktree: Path,
        allowlist: list[Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        on_path = _make_exe(tmp_path / "path-dir" / "guardkit")
        monkeypatch.setenv("PATH", str(on_path.parent))
        _no_context(monkeypatch)
        monkeypatch.setattr(run_module, "_execute_subprocess", _stub_execute())

        with caplog.at_level(logging.INFO, logger=RUN_LOGGER):
            await _drive(worktree, allowlist)
            await _drive(worktree, allowlist)

        resolved_lines = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.INFO and "resolved guardkit binary" in r.getMessage()
        ]
        assert len(resolved_lines) == 1, resolved_lines
        assert str(on_path) in resolved_lines[0]
        assert "PATH" in resolved_lines[0]

    @pytest.mark.asyncio()
    async def test_environment_change_after_resolution_does_not_move_argv(
        self,
        worktree: Path,
        allowlist: list[Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        first_binary = _make_exe(tmp_path / "first" / "guardkit")
        monkeypatch.setenv(run_module.GUARDKIT_PATH_ENV, str(first_binary))
        _no_context(monkeypatch)
        calls: list[list[str]] = []
        monkeypatch.setattr(
            run_module, "_execute_subprocess", _stub_execute(calls=calls)
        )

        await _drive(worktree, allowlist)

        second_binary = _make_exe(tmp_path / "second" / "guardkit")
        monkeypatch.setenv(run_module.GUARDKIT_PATH_ENV, str(second_binary))

        await _drive(worktree, allowlist)

        assert calls[0][0] == calls[1][0] == str(first_binary)

    @pytest.mark.asyncio()
    async def test_a_refused_cwd_never_resolves_a_binary(
        self,
        allowlist: list[Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Laziness + ordering: the cwd guard fires first, so a refused
        # dispatch pays for no lookup and leaves the memo empty.
        _no_context(monkeypatch)
        result = await run(
            subcommand="task-review",
            args=[],
            repo_path=Path("relative/build"),
            read_allowlist=allowlist,
        )

        assert result.status == "failed"
        assert [w.code for w in result.warnings] == ["cwd_outside_allowlist"]
        assert run_module._resolved_guardkit_binary is None

    @pytest.mark.asyncio()
    async def test_concurrent_first_calls_agree_on_one_binary(
        self,
        worktree: Path,
        allowlist: list[Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        on_path = _make_exe(tmp_path / "path-dir" / "guardkit")
        monkeypatch.setenv("PATH", str(on_path.parent))
        _no_context(monkeypatch)
        calls: list[list[str]] = []

        async def _slow(*, command: list[str], cwd: str, timeout: int):
            await asyncio.sleep(0)
            calls.append(list(command))
            return ("", "", 0, 1.0, False)

        monkeypatch.setattr(run_module, "_execute_subprocess", _slow)

        await asyncio.gather(
            _drive(worktree, allowlist, subcommand="task-review"),
            _drive(worktree, allowlist, subcommand="task-work"),
        )

        assert [c[0] for c in calls] == [str(on_path), str(on_path)]


# ---------------------------------------------------------------------------
# The spawn carries the resolved path
# ---------------------------------------------------------------------------


class TestSpawnArgvCarriesResolvedPath:
    """argv[0] is the resolved binary; nothing else about the argv moves."""

    @pytest.mark.asyncio()
    async def test_full_fix_journey_argv_shape_is_unchanged(
        self,
        worktree: Path,
        allowlist: list[Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        binary = _make_exe(tmp_path / "bin" / "guardkit")
        monkeypatch.setenv(run_module.GUARDKIT_PATH_ENV, str(binary))
        monkeypatch.setattr(
            run_module,
            "resolve_context_flags",
            lambda *a, **kw: ResolvedContext(
                flags=["--context", "/abs/contract.md"],
                paths=["/abs/contract.md"],
                warnings=[],
            ),
        )
        capture: dict[str, Any] = {}
        monkeypatch.setattr(run_module, "_execute_subprocess", _stub_execute(capture))

        await run(
            subcommand="task-review",
            args=["--build-id", "B-1", "--task-id", "TASK-ABC-001"],
            repo_path=worktree,
            read_allowlist=allowlist,
            extra_context_paths=["/abs/failure-pack.json"],
            with_nats_streaming=True,
        )

        assert capture["command"] == [
            str(binary),
            "task-review",
            "--build-id",
            "B-1",
            "--task-id",
            "TASK-ABC-001",
            "--context",
            "/abs/contract.md",
            "--context",
            "/abs/failure-pack.json",
            "--nats",
        ]
        assert capture["cwd"] == str(worktree.resolve(strict=False))

    @pytest.mark.asyncio()
    async def test_permissions_refusal_names_the_resolved_binary(
        self,
        worktree: Path,
        allowlist: list[Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        binary = _make_exe(tmp_path / "bin" / "guardkit")
        monkeypatch.setenv(run_module.GUARDKIT_PATH_ENV, str(binary))
        _no_context(monkeypatch)

        async def _refuse(**_: Any):
            raise PermissionError("binary not in shell allowlist")

        monkeypatch.setattr(run_module, "_execute_subprocess", _refuse)

        result = await _drive(worktree, allowlist)

        warning = next(
            w for w in result.warnings if w.code == "permissions_refused"
        )
        assert warning.details["binary"] == str(binary)

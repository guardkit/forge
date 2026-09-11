"""The command line that launches a routine build carries the named seat.

WHY THIS FILE EXISTS. ``routine.seat`` in ``forge.yaml`` lets a factory
name the model a routine build runs on. The setting existed before this
lane, but the lever did not reach production: the command line that
ACTUALLY launches a routine feature build is assembled in
``forge.subagents.autobuild_runner`` and it named no model at all, so
the build system's own command line fell back to its default — the
literal string ``claude-sonnet-4-5-20250929``, a frontier vendor's model
NAME, which reaches a local model only because the estate's proxy
carries a wildcard row mapping ``claude-*`` to the workhorse seat.

WHAT IS PINNED HERE, and how. Every assertion below compares the WHOLE
command line element by element, because the property under test is not
"the seat is somewhere on the line" — it is "when no seat is named,
nothing moved". A factory whose configuration names no routine seat must
launch byte for byte the command line it launched before this lane, and
the only way to say that in a test is to write the whole list out.

The seat rides LAST, after everything the command line already carried,
including the optional ``--base-branch`` pair. That is the rule this
lane adopts and these tests pin: appending is the only placement under
which every element that was already there keeps its exact index.

HOW REAL THIS IS. Real ``forge.yaml`` files on disk in temporary
directories, read through the real config loader, driving the real
runner graph. Only the guardkit subprocess is faked, at its own seam
(``asyncio.create_subprocess_exec``), and the git verbs the runner calls
run for real against a throwaway repo. No live service is touched and no
model is ever called.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from langchain_core.messages import HumanMessage

from forge.cli._db_resolve import FORGE_DB_PATH_ENV
from forge.subagents import autobuild_runner as ar

FEATURE_ID = "FEAT-SEAT-001"
BUILD_ID = "build-FEAT-SEAT-001-20260911120000"
CORR = "11111111-2222-3333-4444-555555555555"
PLANNING_BRANCH = f"planning/{CORR}"
SEAT = "qwen3-coder-30b"

LOGGER_NAME = "forge.subagents.autobuild_runner"


# ---------------------------------------------------------------------------
# Fixtures — a throwaway repo, a hermetic ledger, and a fake guardkit
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _hermetic_forge_ledger(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never consult the developer's real forge ledger.

    The prior-build sweep asks the canonical ledger whether an earlier
    build of the same id is still running. Left unset that is the
    machine's own ``~/.forge/forge.db`` and the verdict would depend on
    the host, so every test here starts from "no ledger".
    """
    monkeypatch.setenv(
        FORGE_DB_PATH_ENV,
        str(tmp_path_factory.mktemp("no-ledger") / "absent-forge.db"),
    )


@pytest.fixture(autouse=True)
def _no_inherited_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Start each test with NO configuration in reach.

    The runner reads ``$FORGE_CONFIG_PATH`` first and then ``forge.yaml``
    relative to the process's working directory — the same two places the
    permissions-allowlist loader reads. A developer running this suite
    from the forge checkout has a real ``forge.yaml`` sitting right there,
    so without this fixture the "no seat named" tests would be reading the
    developer's own file. Each test therefore runs from an empty
    temporary directory with the environment variable cleared, and the
    ones that want a configuration write one themselves.
    """
    monkeypatch.delenv("FORGE_CONFIG_PATH", raising=False)
    cwd = tmp_path / "empty-cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)


@pytest.fixture()
def throwaway_repo(tmp_path: Path) -> Path:
    """A throwaway repo on ``main`` with a separate planning branch.

    Mirrors the live shape: the shared checkout sits on some other
    branch while the machine-made artifacts live on ``planning/<corr>``,
    which is not checked out, so ``git worktree add`` of it is legal.
    """
    repo = tmp_path / "api_test"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "feature.yaml").write_text(f"id: {FEATURE_ID}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    _git(repo, "branch", PLANNING_BRANCH)
    return repo


class _FakeStdout:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = [*lines, b""]

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""


class _FakeProc:
    def __init__(self, exit_code: int, lines: list[bytes]) -> None:
        self.pid = 4242
        self.returncode = exit_code
        self.stdout = _FakeStdout(lines)

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        return None


def _make_exec_stub(recorded: dict[str, Any]):
    """Fake guardkit at its own seam; let every real git verb through.

    Dispatches on ``argv[0]``: a guardkit invocation records the whole
    command line and returns a fake process; anything else (the runner's
    ``git rev-parse`` and ``git worktree`` calls) goes to the real
    ``asyncio.create_subprocess_exec`` so the worktree machinery is
    exercised for real against the throwaway repo.
    """
    real_exec = asyncio.create_subprocess_exec

    async def _stub(*args: Any, **kwargs: Any) -> Any:
        prog = str(args[0]) if args else ""
        if prog.endswith("guardkit"):
            recorded["cwd"] = kwargs.get("cwd")
            recorded["argv"] = list(args)
            return _FakeProc(0, [b"guardkit running\n"])
        return await real_exec(*args, **kwargs)

    return _stub


GUARDKIT = Path("/usr/bin/guardkit")


def _launch(*, branch: str | None) -> str:
    branch_field = f', "branch": "{branch}"' if branch is not None else ""
    payload = (
        f'{{"build_id": "{BUILD_ID}", "feature_id": "{FEATURE_ID}", '
        f'"correlation_id": "{CORR}"{branch_field}}}'
    )
    return f"RUN_AUTOBUILD subagent=autobuild_runner payload={payload}"


def _run(repo: Path, *, branch: str | None) -> dict[str, Any]:
    """Drive the real runner graph once and return what guardkit was given."""
    recorded: dict[str, Any] = {}
    stub = _make_exec_stub(recorded)
    with (
        patch.object(ar, "_resolve_repo_path", lambda payload: repo),
        patch.object(ar, "_resolve_guardkit_path", lambda: GUARDKIT),
        patch.object(asyncio, "create_subprocess_exec", stub),
    ):
        graph = ar._build_runner_graph()
        asyncio.run(
            graph.ainvoke({"messages": [HumanMessage(content=_launch(branch=branch))]})
        )
    assert "argv" in recorded, "guardkit was never launched"
    return recorded


# Every valid ``forge.yaml`` carries a permissions section — it is the one
# required field on the whole document — so a configuration written here
# that is meant to LOAD has to carry one too, or it would be testing a
# validation failure while claiming to test a seat.
_PERMISSIONS = 'permissions:\n  filesystem:\n    allowlist: ["/tmp"]\n'


def _write_config(text: str, *, valid: bool = True) -> Path:
    """Write a real ``forge.yaml`` into the current working directory.

    ``valid=False`` writes exactly the bytes given, for the tests that
    are about a document the loader cannot accept.
    """
    cfg = Path("forge.yaml")
    cfg.write_text(_PERMISSIONS + text if valid else text, encoding="utf-8")
    return cfg


def _messages(caplog: pytest.LogCaptureFixture) -> str:
    return " ".join(r.getMessage() for r in caplog.records)


# The two command lines this factory has always launched, written out in
# full. Every test below compares against one of these two lists, or
# against one of them plus exactly two tokens at the end.
def _todays_argv_legacy() -> list[str]:
    return [
        str(GUARDKIT),
        "autobuild",
        "feature",
        FEATURE_ID,
        "--fresh",
        "--verbose",
    ]


def _todays_argv_with_base_branch() -> list[str]:
    return [*_todays_argv_legacy(), "--base-branch", PLANNING_BRANCH]


# ---------------------------------------------------------------------------
# A named seat rides the command line
# ---------------------------------------------------------------------------


def test_a_named_seat_rides_the_command_line(
    throwaway_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """With ``routine.seat`` named, the two tokens ride — and ride LAST."""
    _write_config(f"routine:\n  seat: {SEAT}\n")

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        recorded = _run(throwaway_repo, branch=None)

    assert recorded["argv"] == [*_todays_argv_legacy(), "--model", SEAT]
    assert f"seat={SEAT}" in _messages(caplog)


def test_a_named_seat_rides_after_the_base_branch_flag(
    throwaway_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Alongside ``--base-branch``, the seat goes last and nothing moves.

    This is the placement rule, pinned: the seat is appended AFTER
    everything the command line already carried, so ``--base-branch`` and
    its value keep the exact indices they had before this lane existed.
    """
    monkeypatch.setenv(ar.FORGE_AUTOBUILD_WORKTREE_BASE_ENV, str(tmp_path / "wt"))
    _write_config(f"routine:\n  seat: {SEAT}\n")

    recorded = _run(throwaway_repo, branch=PLANNING_BRANCH)

    assert recorded["argv"] == [
        *_todays_argv_with_base_branch(),
        "--model",
        SEAT,
    ]


def test_the_seat_is_read_from_forge_config_path(
    throwaway_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``$FORGE_CONFIG_PATH`` names the file, exactly as the allowlist does.

    The runner reads the seat through the same two candidates the
    permissions-allowlist loader already uses, in the same order. No new
    channel was invented for this setting.
    """
    elsewhere = tmp_path / "elsewhere" / "forge.yaml"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text(
        _PERMISSIONS + "routine:\n  seat: flash-next\n", encoding="utf-8"
    )
    monkeypatch.setenv("FORGE_CONFIG_PATH", str(elsewhere))

    recorded = _run(throwaway_repo, branch=None)

    assert recorded["argv"] == [*_todays_argv_legacy(), "--model", "flash-next"]


# ---------------------------------------------------------------------------
# No seat named — today's command line, byte for byte, with a reason logged
# ---------------------------------------------------------------------------


def test_no_configuration_at_all_leaves_the_command_line_untouched(
    throwaway_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No ``forge.yaml`` anywhere: today's command line, and a plain reason."""
    assert not Path("forge.yaml").exists()

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        recorded = _run(throwaway_repo, branch=None)

    assert recorded["argv"] == _todays_argv_legacy()
    assert "no forge.yaml found" in _messages(caplog)


def test_a_config_with_no_routine_section_leaves_the_command_line_untouched(
    throwaway_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A real, valid ``forge.yaml`` that simply says nothing about a seat.

    This is every ``forge.yaml`` deployed today.
    """
    _write_config("conductor:\n  enabled: false\n")

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        recorded = _run(throwaway_repo, branch=None)

    assert recorded["argv"] == _todays_argv_legacy()
    assert "names no routine seat" in _messages(caplog)


def test_a_blank_seat_leaves_the_command_line_untouched(
    throwaway_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``seat: "   "`` is a named nothing — no flag, and no empty token.

    A blank seat normalises to absent at config load, so the runner sees
    no seat and adds nothing. The thing this forbids is an ``--model``
    followed by an empty string, which would reach the build system's
    parser as a real, empty model name.
    """
    _write_config('routine:\n  seat: "   "\n')

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        recorded = _run(throwaway_repo, branch=None)

    assert recorded["argv"] == _todays_argv_legacy()
    assert "--model" not in recorded["argv"]
    assert "" not in recorded["argv"]
    assert "names no routine seat" in _messages(caplog)


def test_a_malformed_config_leaves_the_command_line_untouched(
    throwaway_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Broken YAML must not kill a build an owner already approved."""
    _write_config("routine:\n  seat: [this is not\n    valid: yaml\n", valid=False)

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        recorded = _run(throwaway_repo, branch=None)

    assert recorded["argv"] == _todays_argv_legacy()
    assert "could not read a routine seat" in _messages(caplog)


def test_a_routine_section_of_the_wrong_shape_leaves_the_command_line_untouched(
    throwaway_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``routine:`` written as a bare string, not a section."""
    _write_config(f"routine: {SEAT}\n")

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        recorded = _run(throwaway_repo, branch=None)

    assert recorded["argv"] == _todays_argv_legacy()
    assert "could not read a routine seat" in _messages(caplog)


def test_an_unreadable_config_leaves_the_command_line_untouched(
    throwaway_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A ``forge.yaml`` this process is not allowed to open."""
    cfg = _write_config(f"routine:\n  seat: {SEAT}\n")
    cfg.chmod(0o000)
    if os.access(cfg, os.R_OK):  # pragma: no cover — root ignores the mode bits
        pytest.skip("this process can read a mode-000 file (running as root)")
    try:
        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            recorded = _run(throwaway_repo, branch=None)
    finally:
        cfg.chmod(0o644)

    assert recorded["argv"] == _todays_argv_legacy()
    assert "could not read a routine seat" in _messages(caplog)


def test_no_seat_named_with_a_base_branch_is_also_untouched(
    throwaway_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The branch-scoped command line is unchanged too, element for element."""
    monkeypatch.setenv(ar.FORGE_AUTOBUILD_WORKTREE_BASE_ENV, str(tmp_path / "wt"))

    recorded = _run(throwaway_repo, branch=PLANNING_BRANCH)

    assert recorded["argv"] == _todays_argv_with_base_branch()


# ---------------------------------------------------------------------------
# The seat can never be an option
# ---------------------------------------------------------------------------


def test_a_dash_leading_seat_is_refused_at_config_load() -> None:
    """The first fence: the daemon will not boot on an option-shaped seat.

    A value starting with a dash would land on the command line as
    another OPTION rather than as the name of a model, so the
    configuration is refused where an operator can see it — at load,
    before any build is launched.
    """
    from forge.config.models import RoutineConfig

    with pytest.raises(ValueError) as excinfo:
        RoutineConfig(seat="--dangerously-skip-permissions")

    assert "dash" in str(excinfo.value)


def test_the_runner_never_puts_an_option_shaped_seat_on_the_line(
    throwaway_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The second fence, here: even if one reaches the runner, it stops.

    Config load already refuses a dash-leading seat, so this can only
    happen by some other route. The runner is the place where the value
    would become a command-line token, so it is the last place that can
    refuse — and it does, leaving today's command line and saying why.
    """
    from forge.config.models import RoutineConfig

    class _OptionShapedRoutine:
        seat = "--dangerously-skip-permissions"

    class _ConfigWithABadSeat:
        routine = _OptionShapedRoutine()

    _write_config(f"routine:\n  seat: {SEAT}\n")

    with patch("forge.config.loader.load_config", lambda path: _ConfigWithABadSeat()):
        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            recorded = _run(throwaway_repo, branch=None)

    assert recorded["argv"] == _todays_argv_legacy()
    assert "--dangerously-skip-permissions" not in recorded["argv"]
    assert "starts with a dash" in _messages(caplog)
    # And the posture it is defending is still the config model's own.
    assert RoutineConfig(seat=None).seat is None


# ---------------------------------------------------------------------------
# The fix journey is untouched
# ---------------------------------------------------------------------------


def test_this_surface_only_ever_launches_a_routine_build(
    throwaway_repo: Path,
) -> None:
    """Nothing read here can reach a fix-journey leg, seat or no seat.

    The fix journey's legs are dispatched somewhere else entirely — the
    conductor's adapter over the subprocess dispatcher — and they already
    carry ``--model`` from ``conductor.seat``. This command line launches
    a routine feature build and only ever a routine feature build, which
    is why reading the routine seat here can never put a second
    ``--model`` pair on a leg: there is no path from this function to
    one. Pinned both ways, because the claim has to hold with the new
    setting switched on as well as off.
    """
    without_seat = _run(throwaway_repo, branch=None)["argv"]

    _write_config(f"routine:\n  seat: {SEAT}\n")
    with_seat = _run(throwaway_repo, branch=None)["argv"]

    for argv in (without_seat, with_seat):
        assert argv[1:3] == ["autobuild", "feature"], (
            f"this surface must launch a routine feature build; got {argv!r}"
        )
        assert argv.count("--model") <= 1, f"one question, one answer: {argv!r}"

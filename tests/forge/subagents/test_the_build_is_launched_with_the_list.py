"""The build really is launched with the short named list (item 1 §D, item 2).

The two unit files beside this one hold the list itself down. These tests hold
down the thing that actually matters: that the launch *uses* it — that the
settings the guardkit subprocess is given are exactly the list, with the memory
name the dispatch handed over, and that a credential planted in the launching
process's own environment does not travel with it.

Nothing here runs guardkit, git, docker or any service: the subprocess seam is
replaced with something that records what it was given and answers immediately.
"""

from __future__ import annotations

import asyncio as _asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from forge.launch_environment import (
    GUARDKIT_MEMORY_PROJECT_ENV,
    launch_setting_names,
)
from forge.subagents import autobuild_runner as runner_module

#: A credential nobody is meant to hand a build, planted in the environment the
#: runner process is holding when it launches one.
PLANTED = "GH_TOKEN"
PLANTED_VALUE = "the-publishing-credential-nobody-should-see"


class _Recorder:
    """Stands in for ``asyncio.create_subprocess_exec``: records and answers."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append({"argv": list(args), **kwargs})

        class _Stdout:
            def __init__(self) -> None:
                self._lines = [b"all done\n", b""]

            async def readline(self) -> bytes:
                return self._lines.pop(0) if self._lines else b""

        class _Proc:
            pid = 4242
            returncode = 0
            stdout = _Stdout()

            async def wait(self) -> int:
                return 0

            def kill(self) -> None:
                return None

        return _Proc()

    @property
    def env(self) -> dict[str, str]:
        assert self.calls, "nothing was launched"
        return dict(self.calls[-1]["env"])


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Recorder:
    """A parent environment with the list's settings AND a planted credential."""
    for name in launch_setting_names():
        if name == GUARDKIT_MEMORY_PROJECT_ENV:
            monkeypatch.delenv(name, raising=False)
            continue
        monkeypatch.setenv(name, f"value-of-{name}")
    monkeypatch.setenv(PLANTED, PLANTED_VALUE)
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/user/1000/keyring/ssh")
    monkeypatch.setenv("FORGE_DB_PATH", str(tmp_path / "forge.db"))
    # The runner's own supervision is off, so nothing watches files that are
    # not there; this test is about the launch, not about supervision.
    monkeypatch.setenv("FORGE_BUILD_MONITOR", "0")
    return _Recorder()


def _run(recorder: _Recorder, payload: dict[str, Any], tmp_path: Path) -> None:
    """Drive the REAL node that launches a build, with the subprocess seam
    replaced. Nothing else about the launch is stood in for."""
    import json

    from langchain_core.messages import HumanMessage

    repo = tmp_path / "project"
    repo.mkdir(exist_ok=True)
    description = (
        "RUN_AUTOBUILD subagent=autobuild_runner payload=" + json.dumps(payload)
    )
    with patch.object(
        runner_module, "_resolve_repo_path", lambda payload: repo
    ), patch.object(
        runner_module, "_resolve_guardkit_path", lambda: Path("/usr/bin/guardkit")
    ), patch.object(
        _asyncio, "create_subprocess_exec", recorder
    ):
        _asyncio.run(
            runner_module._node_running_wave(
                {"messages": [HumanMessage(content=description)]}
            )
        )


def test_the_launch_environment_is_exactly_the_list(
    launched: _Recorder, tmp_path: Path
) -> None:
    _run(
        launched,
        {
            "build_id": "build-FEAT-L-1",
            "feature_id": "FEAT-L",
            "correlation_id": "corr-L",
            "memory_project": "widget_shop",
        },
        tmp_path,
    )

    assert set(launched.env) == set(launch_setting_names())


def test_a_planted_credential_does_not_reach_the_build(
    launched: _Recorder, tmp_path: Path
) -> None:
    """The reason the list exists. Before 2026-09-21 this credential travelled
    with every build, because the launch copied the whole environment."""
    _run(
        launched,
        {
            "build_id": "build-FEAT-L-2",
            "feature_id": "FEAT-L",
            "correlation_id": "corr-L",
            "memory_project": "widget_shop",
        },
        tmp_path,
    )

    env = launched.env
    assert PLANTED not in env
    assert "SSH_AUTH_SOCK" not in env
    assert "FORGE_DB_PATH" not in env
    assert all(PLANTED_VALUE not in value for value in env.values())


def test_the_memory_name_the_dispatch_handed_over_is_the_one_set(
    launched: _Recorder, tmp_path: Path
) -> None:
    _run(
        launched,
        {
            "build_id": "build-FEAT-L-3",
            "feature_id": "FEAT-L",
            "correlation_id": "corr-L",
            "memory_project": "widget_shop",
        },
        tmp_path,
    )

    assert launched.env[GUARDKIT_MEMORY_PROJECT_ENV] == "widget_shop"


def test_nothing_recorded_means_no_name_is_set_and_never_guardkit(
    launched: _Recorder, tmp_path: Path
) -> None:
    """With no name the build system reads the project's own declaration in the
    folder it is building. It never falls back to "guardkit", which is what
    used to file every project's outcomes under somebody else's name."""
    _run(
        launched,
        {
            "build_id": "build-FEAT-L-4",
            "feature_id": "FEAT-L",
            "correlation_id": "corr-L",
        },
        tmp_path,
    )

    assert GUARDKIT_MEMORY_PROJECT_ENV not in launched.env


def test_a_name_in_the_runners_own_environment_is_not_inherited(
    launched: _Recorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(GUARDKIT_MEMORY_PROJECT_ENV, "somebody_elses_memory")

    _run(
        launched,
        {
            "build_id": "build-FEAT-L-5",
            "feature_id": "FEAT-L",
            "correlation_id": "corr-L",
            "memory_project": "widget_shop",
        },
        tmp_path,
    )

    assert launched.env[GUARDKIT_MEMORY_PROJECT_ENV] == "widget_shop"


def test_a_memory_name_that_is_not_text_is_read_as_nothing_recorded() -> None:
    assert runner_module._memory_project_for_build({"memory_project": 17}) is None
    assert runner_module._memory_project_for_build({"memory_project": "  "}) is None
    assert runner_module._memory_project_for_build({}) is None
    assert (
        runner_module._memory_project_for_build({"memory_project": " widget_shop "})
        == "widget_shop"
    )


# ---------------------------------------------------------------------------
# The OTHER launch of the build system: the planning stages and the fix journey
# ---------------------------------------------------------------------------


def test_the_bounded_legs_are_launched_with_the_same_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``forge.adapters.guardkit.run`` used to pass no ``env=`` at all, which
    inherits the whole environment implicitly — the same hole, reached a
    different way. It now takes the same written-down list."""
    from forge.adapters.guardkit import run as guardkit_run

    for name in launch_setting_names():
        if name == GUARDKIT_MEMORY_PROJECT_ENV:
            monkeypatch.delenv(name, raising=False)
            continue
        monkeypatch.setenv(name, f"value-of-{name}")
    monkeypatch.setenv(PLANTED, PLANTED_VALUE)

    seen: dict[str, Any] = {}

    async def _spawn(*args: Any, **kwargs: Any) -> Any:
        seen["env"] = kwargs.get("env")

        class _Proc:
            returncode = 0

            async def communicate(self) -> tuple[bytes, bytes]:
                return b"{}", b""

        return _Proc()

    monkeypatch.setattr(_asyncio, "create_subprocess_exec", _spawn)
    repo = tmp_path / "project"
    repo.mkdir()
    stand_in = tmp_path / "guardkit"
    stand_in.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stand_in.chmod(0o755)
    monkeypatch.setenv("FORGE_GUARDKIT_PATH", str(stand_in))
    monkeypatch.setattr(guardkit_run, "_resolved_guardkit_binary", None, raising=False)

    _asyncio.run(
        guardkit_run.run(
            subcommand="feature-plan",
            args=["--feature-id", "FEAT-L"],
            repo_path=repo,
            read_allowlist=[repo],
            with_nats_streaming=False,
        )
    )

    env = seen["env"]
    assert env is not None, "the leg was launched with no named settings at all"
    assert PLANTED not in env
    assert set(env) <= set(launch_setting_names())
    # No memory name is handed to a leg: it runs IN the project's own working
    # folder and reads the project's own declaration there.
    assert GUARDKIT_MEMORY_PROJECT_ENV not in env

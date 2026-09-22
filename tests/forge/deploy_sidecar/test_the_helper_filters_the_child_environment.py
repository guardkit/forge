"""The sandbox helper hands its children a named list, not everything it holds.

The stage's second independent review found this one still inheriting. Its
written reason was that the sandbox's own start script builds the environment
this service is holding — true of the settings the script EXPORTS, and not true
of anything else the process happens to carry. The review planted a fake
publishing credential, an agent socket and a variable on no list at all in the
service's environment; all three reached the child.

These tests run a REAL child — a tiny script that writes down the NAMES of the
settings it was given, and nothing else — through the helper's own command
runner. Nothing else is started: no sandbox, no server, no service, no network.
No real credential exists anywhere here; the planted values are strings written
in this file.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from forge.deploy_sidecar.service import run_merge_command
from forge.launch_environment import (
    GUARDKIT_FACTORY_LAUNCH_ENV,
    GUARDKIT_MEMORY_PROJECT_ENV,
    launch_setting_names,
)

#: What the review planted, and what it must never be handed on.
PLANTED = {
    "GH_TOKEN": "a-fake-publishing-credential-written-in-this-test",
    "SSH_AUTH_SOCK": "/run/user/1000/keyring/ssh",
    "SOMETHING_ON_NO_LIST": "and nothing should carry it",
    "FORGE_DB_PATH": "/state/.forge/forge.db",
    "FORGE_SIDECAR_IN_SANDBOX": "1",
}


@pytest.fixture
def child(tmp_path: Path) -> Path:
    """A child that answers with the NAMES it was given, and one value."""
    script = tmp_path / "say-what-i-was-given"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "print(json.dumps({\n"
        "    'names': sorted(os.environ),\n"
        "    'memory': os.environ.get('GUARDKIT_MEMORY_PROJECT'),\n"
        "    'declared': os.environ.get('SOME_TOOL_CACHE'),\n"
        "}))\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _run(child: Path, cwd: Path, **kwargs) -> dict:
    exit_code, stdout, stderr = run_merge_command(
        argv=[str(child)], cwd=str(cwd), timeout=30, what="the stand-in", **kwargs
    )
    assert exit_code == 0, stderr
    return json.loads(stdout)


@pytest.fixture(autouse=True)
def _plant(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in PLANTED.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("FORGE_RECEIPTS_DIR", "/receipts")
    monkeypatch.setenv("GUARDKIT_HARNESS", "langgraph")


def test_nothing_the_helper_holds_reaches_the_child_unless_it_is_named(
    child: Path, tmp_path: Path
) -> None:
    answer = _run(child, tmp_path)

    for name in ("GH_TOKEN", "SSH_AUTH_SOCK", "SOMETHING_ON_NO_LIST"):
        assert name not in answer["names"], f"{name} still reaches the child"


def test_the_helpers_own_settings_stay_with_the_helper(
    child: Path, tmp_path: Path
) -> None:
    """``FORGE_SIDECAR_IN_SANDBOX`` tells this service which script it may run,
    and the coordinator's ledger path is not a build's business. Both are named
    in the list of what is deliberately not passed on."""
    answer = _run(child, tmp_path)

    assert "FORGE_SIDECAR_IN_SANDBOX" not in answer["names"]
    assert "FORGE_DB_PATH" not in answer["names"]
    # And the service itself still has them — it kept them for its own use.
    import os

    assert os.environ["FORGE_SIDECAR_IN_SANDBOX"] == "1"


def test_what_is_named_does_reach_the_child(child: Path, tmp_path: Path) -> None:
    answer = _run(child, tmp_path)

    assert "FORGE_RECEIPTS_DIR" in answer["names"]
    assert "GUARDKIT_HARNESS" in answer["names"]
    # Everything it got is on the list, or the interpreter's own doing.
    unexpected = set(answer["names"]) - set(launch_setting_names()) - {"LC_CTYPE"}
    assert unexpected == set(), f"not on the list: {sorted(unexpected)}"


def test_the_memory_name_the_request_carried_is_handed_over(
    child: Path, tmp_path: Path
) -> None:
    answer = _run(child, tmp_path, memory_project="widget_shop")

    assert answer["memory"] == "widget_shop"


def test_a_request_with_no_name_runs_with_memory_off_and_says_so(
    child: Path, tmp_path: Path
) -> None:
    """Not "memory off by omission": the child is TOLD a factory launched it,
    so it cannot take the name out of the folder it is standing in."""
    answer = _run(child, tmp_path)

    assert answer["memory"] is None
    assert GUARDKIT_FACTORY_LAUNCH_ENV in answer["names"]
    assert GUARDKIT_MEMORY_PROJECT_ENV not in answer["names"]


def test_a_name_the_project_declared_is_passed_and_an_undeclared_one_is_not(
    child: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOME_TOOL_CACHE", "/scratch/cache")
    monkeypatch.setenv("ANOTHER_TOOL_CACHE", "/scratch/other")

    answer = _run(child, tmp_path, launch_settings=["SOME_TOOL_CACHE"])

    assert answer["declared"] == "/scratch/cache"
    assert "ANOTHER_TOOL_CACHE" not in answer["names"]


def test_a_reserved_name_a_project_asked_for_is_still_not_passed(
    child: Path, tmp_path: Path
) -> None:
    """The door refuses these in plain words. This is the second fence."""
    answer = _run(child, tmp_path, launch_settings=["GH_TOKEN", "FORGE_DB_PATH"])

    assert "GH_TOKEN" not in answer["names"]
    assert "FORGE_DB_PATH" not in answer["names"]


def test_the_live_gates_own_overlay_still_reaches_the_command(
    child: Path, tmp_path: Path
) -> None:
    """The one exception, and it is a named list of its own: the candidate
    leg's gate must address the candidate's port rather than the live one, and
    every value in it is one the project's own profile declares."""
    answer = _run(child, tmp_path, extra_env={"SOME_TOOL_CACHE": "/candidate"})

    assert answer["declared"] == "/candidate"

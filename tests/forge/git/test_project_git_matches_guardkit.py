"""Forge's copy of the git rules matches GuardKit's.

Forge does not import GuardKit as a library, so its helper
(``src/forge/project_git.py``) is a copy of GuardKit's
(``guardkit/lib/project_git.py``). This test compares every named constant
with a pinned copy of GuardKit's (``guardkit_project_git.json``, which records
the GuardKit commit it came from). The release image check
(``scripts/verify-forge-oracles.sh``), where both packages are installed,
compares the two live.

The only allowed differences are Forge's additions, which are kept in
constants of their own: ``FORGE_COMMAND_OPTIONS`` (``commit-tree`` is never
signed), ``FORGE_REFUSED_OPTIONS`` (``grep -O`` starts a program) and
``CALLER_SETTINGS`` (the four settings a caller may add).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

PINNED = Path(__file__).with_name("guardkit_project_git.json")


def _pinned() -> dict:
    return json.loads(PINNED.read_text(encoding="utf-8"))


def _plain(value):
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def test_the_pinned_copy_records_where_it_came_from() -> None:
    pinned = _pinned()
    assert pinned["source"] == "guardkit/lib/project_git.py"
    assert len(pinned["guardkit_commit"]) == 40


@pytest.mark.parametrize("name", sorted(_pinned()["constants"]))
def test_project_git_matches_guardkit(name: str) -> None:
    from forge import project_git

    assert _plain(getattr(project_git, name)) == _pinned()["constants"][name], name


def test_forge_additions_are_kept_apart() -> None:
    from forge import project_git

    pinned = _pinned()["constants"]
    assert not set(project_git.FORGE_COMMAND_OPTIONS) & set(pinned["COMMAND_OPTIONS"])
    assert project_git.FORGE_COMMAND_OPTIONS == {"commit-tree": ("--no-gpg-sign",)}
    assert set(project_git.FORGE_REFUSED_OPTIONS) == {"grep"}
    assert project_git.CALLER_SETTINGS == (
        "safe.directory", "core.quotepath", "user.name", "user.email",
    )

"""Two builds of one repository never run ``git worktree add`` at the same time.

3 November 2026: GitHub's run of 57b9fb89 failed
test_two_builds_of_one_repository_run_in_their_own_worktrees because both
builds ran ``git worktree add`` in the same ``.git`` at once and git refused
one with exit 128. Each repository's adds now take turns.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from forge.subagents import autobuild_runner


def _recording_git(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    seen = {"running": 0, "most": 0}

    async def fake_run_git(args: list[str], *, cwd: Path) -> tuple[int, str]:
        seen["running"] += 1
        seen["most"] = max(seen["most"], seen["running"])
        await asyncio.sleep(0.05)
        seen["running"] -= 1
        return 0, ""

    monkeypatch.setattr(autobuild_runner, "_run_git", fake_run_git)
    return seen


def test_adds_in_one_repository_take_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(autobuild_runner, "_worktree_base_dir", lambda: tmp_path / "wt")
    seen = _recording_git(monkeypatch)
    repo = tmp_path / "widget"
    repo.mkdir()

    async def both() -> None:
        await asyncio.gather(
            autobuild_runner._materialise_worktree(repo, "planning/a", "build-A"),
            autobuild_runner._materialise_worktree(repo, "planning/b", "build-B"),
        )

    asyncio.run(both())
    assert seen["most"] == 1


def test_adds_in_different_repositories_still_run_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(autobuild_runner, "_worktree_base_dir", lambda: tmp_path / "wt")
    seen = _recording_git(monkeypatch)
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()

    async def both() -> None:
        await asyncio.gather(
            autobuild_runner._materialise_worktree(tmp_path / "one", "planning/a", "build-A"),
            autobuild_runner._materialise_worktree(tmp_path / "two", "planning/b", "build-B"),
        )

    asyncio.run(both())
    assert seen["most"] == 2

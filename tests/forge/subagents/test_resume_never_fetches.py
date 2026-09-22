"""A resumed build is not moved onto a newer commit (one true copy, item 1).

The starting rule fetches the project's remote before a NEW piece of work
cuts its branch. Resuming is not new work: a build that is picked up again
keeps its commit, its working folder and its checkpoints, and nothing on the
resume path fetches or moves anything. These tests pin that.

Nothing here contacts a remote — that is the whole point, and the first test
makes a fetch impossible so that an attempted one would be loud.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.subagents import autobuild_runner, build_monitor
from forge.subagents.build_monitor import plan_relaunch

FEATURE = "FEAT-RSM1"


@pytest.fixture
def kept_worktree(tmp_path: Path) -> Path:
    worktree = tmp_path / "autobuild" / FEATURE
    worktree.mkdir(parents=True)
    return worktree


def test_planning_a_resume_never_asks_a_remote_anything(
    tmp_path: Path, kept_worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The starting rule's operation is made to explode. Planning a resume
    finishes anyway, because it never calls it."""
    from forge.deploy import candidate_tree

    def _never(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "the resume path asked a remote for a starting point — it must not"
        )

    monkeypatch.setattr(candidate_tree, "fetch_remote_start_point", _never)
    monkeypatch.setattr(candidate_tree, "_fetch_remote_start_point_sync", _never)

    plan = plan_relaunch(
        feature_id=FEATURE,
        guardkit_path=tmp_path / "guardkit",
        worktree_path=kept_worktree,
        base_branch="autobuild/FEAT-RSM1-base",
    )

    assert plan.possible is True
    assert plan.cwd == str(kept_worktree)
    assert "--fresh" not in plan.argv
    assert not any("fetch" in part for part in plan.argv)
    assert plan.base_branch == "autobuild/FEAT-RSM1-base"


def test_the_resume_argv_names_no_commit_of_its_own(
    tmp_path: Path, kept_worktree: Path
) -> None:
    """A resume carries no starting point: it runs where the work already is."""
    plan = plan_relaunch(
        feature_id=FEATURE,
        guardkit_path=tmp_path / "guardkit",
        worktree_path=kept_worktree,
        base_branch="autobuild/FEAT-RSM1-base",
    )

    assert "--start-commit" not in plan.argv
    assert "start_commit" not in " ".join(plan.argv)


def test_no_code_on_the_resume_path_names_the_starting_rules_operation() -> None:
    """The pin that survives a rewrite of either module: neither the resume
    decision nor the runner that relaunches a build mentions the operation."""
    for module in (build_monitor, autobuild_runner):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "fetch_remote_start_point" not in source, (
            f"{module.__name__} names the starting rule's operation; resuming "
            f"must not fetch"
        )
        assert "RemoteStartPoint" not in source

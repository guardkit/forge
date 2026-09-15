"""``docs/state`` survives the build — the player's own note of what it meant
to touch.

The planner fix, 2026-09-15, item 3. The player writes its note of what it
intends to change into ``docs/state/<task id>/implementation_plan.md`` (with
``implementation_plan.json`` as a fallback), and until this it died with the
worktree on every successful build. One name in the receipt families keeps it,
from the outer worktree and from every inner task worktree alike.

BE HONEST ABOUT WHAT THIS IS: the local seat does not currently write that
file — every plan-audit block in every receipt on this machine says "skipped —
no implementation plan on disk" — so this arms a future measurement rather
than recovering a lost one. What the tests below prove is that when the file
IS written, it is still there after the build.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import forge.subagents.autobuild_runner as ar


def _seed(worktree: Path, rel: str) -> None:
    path = worktree / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"note::{rel}", encoding="utf-8")


def test_docs_state_is_one_of_the_receipt_families() -> None:
    assert "docs/state" in ar._RECEIPT_FAMILIES


def test_the_note_is_kept_from_the_outer_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / "wt"
    _seed(worktree, "docs/state/TASK-X-001/implementation_plan.md")
    dest = tmp_path / "receipts"
    monkeypatch.setenv(ar.RECEIPTS_DIR_ENV, str(dest))

    result = ar._export_receipts(worktree, "build-PLAN-1")

    assert result.ok is True
    assert "docs/state" in result.exported
    kept = dest / "build-PLAN-1" / "docs/state/TASK-X-001/implementation_plan.md"
    assert kept.is_file()
    assert kept.read_text(encoding="utf-8") == (
        "note::docs/state/TASK-X-001/implementation_plan.md"
    )


def test_the_note_is_kept_from_an_inner_task_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The richest receipts of a run are written in the INNER tree, and the
    player's note is one of them."""
    worktree = tmp_path / "wt"
    _seed(
        worktree,
        ".guardkit/worktrees/TASK-X-001/docs/state/TASK-X-001/implementation_plan.md",
    )
    dest = tmp_path / "receipts"
    monkeypatch.setenv(ar.RECEIPTS_DIR_ENV, str(dest))

    result = ar._export_receipts(worktree, "build-PLAN-2")

    assert result.ok is True
    assert "worktrees/TASK-X-001/docs/state" in result.exported
    kept = (
        dest
        / "build-PLAN-2"
        / "worktrees/TASK-X-001/docs/state/TASK-X-001/implementation_plan.md"
    )
    assert kept.is_file()


def test_the_json_fallback_is_kept_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audit looks for the markdown note first and the JSON one second, so
    whichever is there has to survive."""
    worktree = tmp_path / "wt"
    _seed(worktree, "docs/state/TASK-X-002/implementation_plan.json")
    dest = tmp_path / "receipts"
    monkeypatch.setenv(ar.RECEIPTS_DIR_ENV, str(dest))

    ar._export_receipts(worktree, "build-PLAN-3")

    assert (
        dest / "build-PLAN-3" / "docs/state/TASK-X-002/implementation_plan.json"
    ).is_file()


def test_a_build_that_wrote_no_note_is_still_a_clean_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Which is every build on this machine today: the family is simply
    missing, and a missing family has never been a failure."""
    worktree = tmp_path / "wt"
    _seed(worktree, ".guardkit/qav-shadow/queue.jsonl")
    dest = tmp_path / "receipts"
    monkeypatch.setenv(ar.RECEIPTS_DIR_ENV, str(dest))

    result = ar._export_receipts(worktree, "build-PLAN-4")

    assert result.ok is True
    assert "docs/state" not in result.exported
    assert not (dest / "build-PLAN-4" / "docs").exists()

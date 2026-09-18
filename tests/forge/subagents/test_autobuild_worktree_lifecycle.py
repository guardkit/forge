"""Real-Git controls for retained autobuild worktree ownership and cleanup."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from forge.subagents.autobuild_worktree_lifecycle import (
    inspect_autobuild_worktree,
    inspect_worktree_capacity,
    retire_autobuild_worktree,
)

BUILD_ID = "build-FEAT-KEEP-20260918"
FEATURE = "FEAT-KEEP"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=tests",
            "-c",
            "user.email=tests@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo_with_nested(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main", "-q")
    (repo / "README").write_text("main\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-q", "-m", "main")
    _git(repo, "branch", f"autobuild/{FEATURE}")
    base = tmp_path / "autobuild-worktrees"
    outer = base / BUILD_ID
    _git(repo, "worktree", "add", "--detach", str(outer), "main")
    inner = outer / ".guardkit" / "worktrees" / "TASK-KEEP-001"
    inner.parent.mkdir(parents=True)
    _git(repo, "worktree", "add", str(inner), f"autobuild/{FEATURE}")
    return repo, base, outer, inner


def test_offer_identity_preserves_then_successfully_retires_exact_nested_tree(
    tmp_path: Path,
) -> None:
    repo, base, outer, inner = _repo_with_nested(tmp_path)
    (inner / "untracked-evidence.txt").write_text("keep through offer\n")

    offered = inspect_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer
    )

    assert offered["ok"] is True
    assert [Path(r["path"]) for r in offered["nested_registrations"]] == [inner]
    assert outer.is_dir() and inner.is_dir()
    offered["cleanup_registrations"] = offered["nested_registrations"]
    result = retire_autobuild_worktree(
        repo=repo,
        base=base,
        build_id=BUILD_ID,
        path=outer,
        expected=offered,
    )
    assert result["status"] == "removed", result
    assert not outer.exists() and not inner.exists()
    assert _git(repo, "rev-parse", f"autobuild/{FEATURE}")


def test_unrelated_registered_guardkit_tree_survives_candidate_cleanup(
    tmp_path: Path,
) -> None:
    repo, base, outer, inner = _repo_with_nested(tmp_path)
    _git(repo, "branch", "unrelated")
    unrelated = outer / ".guardkit/worktrees/TASK-OTHER-001"
    _git(repo, "worktree", "add", str(unrelated), "unrelated")
    offered = inspect_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer
    )
    offered["cleanup_registrations"] = [
        row for row in offered["nested_registrations"] if Path(row["path"]) == inner
    ]

    result = retire_autobuild_worktree(
        repo=repo,
        base=base,
        build_id=BUILD_ID,
        path=outer,
        expected=offered,
    )

    assert result["status"] == "kept"
    assert not inner.exists()
    assert outer.is_dir() and unrelated.is_dir()


def test_changed_untracked_content_after_offer_fails_closed(tmp_path: Path) -> None:
    repo, base, outer, inner = _repo_with_nested(tmp_path)
    offered = inspect_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer
    )
    offered["cleanup_registrations"] = offered["nested_registrations"]
    (inner / "user-note.txt").write_text("arrived after offer\n")

    result = retire_autobuild_worktree(
        repo=repo,
        base=base,
        build_id=BUILD_ID,
        path=outer,
        expected=offered,
    )

    assert result["status"] == "kept"
    assert "identity no longer matches" in result["detail"]
    assert outer.is_dir() and inner.is_dir()


def test_stale_or_unowned_path_is_never_removed(tmp_path: Path) -> None:
    repo, base, outer, inner = _repo_with_nested(tmp_path)
    offered = inspect_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer
    )
    offered["cleanup_registrations"] = offered["nested_registrations"]
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "keep").write_text("keep\n")

    result = retire_autobuild_worktree(
        repo=repo,
        base=base,
        build_id=BUILD_ID,
        path=unrelated,
        expected=offered,
    )

    assert result["status"] == "kept"
    assert unrelated.is_dir() and (unrelated / "keep").is_file()
    assert outer.is_dir() and inner.is_dir()


def test_unrelated_nested_registration_blocks_outer_cleanup(tmp_path: Path) -> None:
    repo, base, outer, inner = _repo_with_nested(tmp_path)
    _git(repo, "branch", "unrelated")
    unexpected = outer / "somewhere-else" / "tree"
    unexpected.parent.mkdir(parents=True)
    _git(repo, "worktree", "add", str(unexpected), "unrelated")

    identity = inspect_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer
    )

    assert identity["ok"] is False
    assert str(unexpected) in identity["unexpected_registrations"]
    assert outer.is_dir() and inner.is_dir() and unexpected.is_dir()


@pytest.mark.parametrize(
    ("available_bytes", "available_inodes", "detail"),
    [(99, 5, "below the required"), (1000, 0, "no available inodes")],
)
def test_capacity_preflight_fails_closed_before_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    available_bytes: int,
    available_inodes: int,
    detail: str,
) -> None:
    monkeypatch.setattr(
        "forge.subagents.autobuild_worktree_lifecycle.os.statvfs",
        lambda _path: SimpleNamespace(
            f_bavail=available_bytes,
            f_frsize=1,
            f_favail=available_inodes,
        ),
    )
    missing = tmp_path / "not-created" / "worktrees"
    report = inspect_worktree_capacity(missing, min_available_bytes=100)
    assert report["ok"] is False
    assert detail in report["detail"]
    assert not missing.exists()


def test_capacity_preflight_accepts_exact_floor(tmp_path: Path) -> None:
    report = inspect_worktree_capacity(tmp_path, min_available_bytes=1)
    assert report["ok"] is True
    assert report["capacity"]["available_inodes"] > 0

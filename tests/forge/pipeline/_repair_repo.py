"""A temporary repository with a merged feature and its task folder.

Shared by the repair-task tests: a real git checkout on ``main`` carrying
the feature's own task file (with a ``parent_review``), its feature YAML and
one more file, all committed — the shape a repair of a merged feature meets.
Git is isolated from the host's global and system configuration so no
signing hook or template can reach into a test.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

FEATURE_ID = "FEAT-44A8"
FOLDER = "add-the-thing"
PARENT_REVIEW = "TASK-REV-44A8"

TASK_TEXT = """---
id: TASK-{short}-001
title: Add the thing
task_type: feature
parent_review: {review}
feature_id: {feature}
wave: 1
implementation_mode: task-work
complexity: 3
dependencies: []
---

## Acceptance Criteria

- [ ] The thing is added

## Implementation Notes

- Add it
"""


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


def isolate_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """No global or system git configuration reaches the tests' subprocesses."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)


def make_feature_repo(
    root: Path,
    *,
    feature_id: str = FEATURE_ID,
    folder: str | None = FOLDER,
    parent_review: str = PARENT_REVIEW,
) -> Path:
    """A checkout on ``main`` with the feature merged; ``folder=None`` leaves no task folder."""
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "tests@example.com")
    git(root, "config", "user.name", "the tests")
    short = feature_id.split("-", 1)[1]
    features = root / ".guardkit" / "features"
    features.mkdir(parents=True)
    (features / f"{feature_id}.yaml").write_text(
        f"id: {feature_id}\nname: the thing\ntasks: []\n", encoding="utf-8"
    )
    if folder is not None:
        task_dir = root / "tasks" / "backlog" / folder
        task_dir.mkdir(parents=True)
        (task_dir / f"TASK-{short}-001-add-the-thing.md").write_text(
            TASK_TEXT.format(short=short, review=parent_review, feature=feature_id),
            encoding="utf-8",
        )
    (root / "README.md").write_text("the feature, merged\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "the feature, merged")
    return root


def porcelain_hash(root: Path) -> str:
    """One hash of the checkout's working tree and index state."""
    status = git(root, "status", "--porcelain", "--untracked-files=all").stdout
    return hashlib.sha256(status.encode("utf-8")).hexdigest()


def head(root: Path, ref: str = "HEAD") -> str:
    return git(root, "rev-parse", ref).stdout.strip()


def commit_count(root: Path, ref: str) -> int:
    return int(git(root, "rev-list", "--count", ref).stdout.strip())


def show(root: Path, ref: str, path: str) -> str:
    return git(root, "show", f"{ref}:{path}").stdout


def branches(root: Path) -> list[str]:
    out = git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads/").stdout
    return sorted(line.strip() for line in out.splitlines() if line.strip())


def worktrees(root: Path) -> list[str]:
    out = git(root, "worktree", "list", "--porcelain").stdout
    return [line.split(" ", 1)[1] for line in out.splitlines() if line.startswith("worktree ")]

"""The repair branch cut where the build runs — through the sidecar, no socket.

Open item 24 (2026-09-13): the host-git version cut the branch in the
operator's checkout while the fix journey's worktree is cut inside the
sandbox, so the conductor refused every repair of a sandboxed repository.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from forge.pipeline.repair_branch import (
    RepairBranchError,
    materialise_repair_branch_via_sidecar,
)

REPO = "guardkit/api_test"
ROOT = Path("/home/rich/Projects/api_test")
FILES = {
    ".guardkit/features/TASK-FEATBD8FFIX1.yaml": "id: TASK-FEATBD8FFIX1\n",
    "tasks/backlog/feat-bd8f/TASK-FEATBD8FFIX1-repair.md": "# repair\n",
}


class FakeSidecar:
    """Answers the four routes from a script and remembers every call."""

    def __init__(
        self,
        *,
        shas: dict[str, str | None],
        write_status: str = "success",
        cut_status: str = "success",
        ancestors: set[tuple[str, str]] | None = None,
    ):
        self.shas = dict(shas)
        self.write_status = write_status
        self.cut_status = cut_status
        self.ancestors = ancestors or set()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, url: str, body: dict[str, Any], timeout: float) -> tuple[int, Any]:
        route = url.split("/git/", 1)[1]
        self.calls.append((route, body))
        if route == "rev-parse":
            return 200, {"sha": self.shas.get(body["ref"])}
        if route == "is-ancestor":
            return 200, {
                "is_ancestor": (body["ancestor"], body["descendant"])
                in self.ancestors,
            }
        if route == "worktree-add":
            if self.cut_status == "success":
                self.shas[body["branch"]] = "base0000"
            return 200, {"status": self.cut_status, "path": body["path"], "reused": False, "detail": "" if self.cut_status == "success" else "no such base"}
        if route == "worktree-remove":
            return 200, {"status": "success", "path": body["path"], "detail": ""}
        if route == "prepare-branch-and-write-tree":
            if self.write_status == "success":
                self.shas[body["branch"]] = "c0ffee11"
                return 200, {"status": "success", "sha": "c0ffee11", "checks": [], "detail": ""}
            return 200, {"status": "failed", "sha": None, "checks": [], "detail": "a check refused"}
        return 404, {"error": f"no route {route}"}


def _run(
    fake: FakeSidecar,
    *,
    base: str = "autobuild/FEAT-BD8F",
    expected_base_commit: str | None = None,
):
    return materialise_repair_branch_via_sidecar(
        "http://127.0.0.1:8925/",
        repo=REPO,
        repo_root=ROOT,
        task_id="TASK-FEATBD8FFIX1",
        base_branch=base,
        expected_base_commit=expected_base_commit,
        files=FILES,
        message="repair task for build-x: y",
        post=fake,
    )


def test_a_new_branch_is_cut_from_the_base_then_written() -> None:
    fake = FakeSidecar(shas={"autobuild/FEAT-BD8F": "17497a2a"})
    result = _run(fake)
    routes = [r for r, _ in fake.calls]
    assert routes == ["rev-parse", "rev-parse", "worktree-add", "worktree-remove", "prepare-branch-and-write-tree"]
    cut = fake.calls[2][1]
    assert cut["base_ref"] == "autobuild/FEAT-BD8F"
    assert cut["branch"] == "repair/TASK-FEATBD8FFIX1"
    assert cut["path"] == str(ROOT / ".forge" / "worktrees" / "repair-TASK-FEATBD8FFIX1")
    assert fake.calls[3][1]["path"] == cut["path"]
    written = fake.calls[4][1]
    assert written["files"] == FILES and written["checks"] == []
    assert result.branch == "repair/TASK-FEATBD8FFIX1"
    assert result.commit == "c0ffee11"
    assert result.created_branch is True and result.committed is True
    assert result.files == tuple(sorted(FILES))


def test_an_existing_branch_is_reused_not_recut() -> None:
    fake = FakeSidecar(shas={"autobuild/FEAT-BD8F": "17497a2a", "repair/TASK-FEATBD8FFIX1": "c0ffee11"})
    result = _run(fake)
    assert [r for r, _ in fake.calls] == ["rev-parse", "rev-parse", "prepare-branch-and-write-tree"]
    assert result.created_branch is False
    assert result.committed is False  # the same sha came back: nothing new


def test_a_moved_candidate_ref_is_refused_before_the_branch_is_cut() -> None:
    fake = FakeSidecar(shas={"autobuild/FEAT-BD8F": "moved000"})
    with pytest.raises(RepairBranchError, match="not the retained candidate wanted000"):
        _run(fake, expected_base_commit="wanted000")
    assert [route for route, _ in fake.calls] == ["rev-parse"]


def test_an_existing_unrelated_repair_branch_is_not_reused() -> None:
    fake = FakeSidecar(
        shas={
            "autobuild/FEAT-BD8F": "wanted000",
            "repair/TASK-FEATBD8FFIX1": "wrong000",
        }
    )
    with pytest.raises(
        RepairBranchError, match="does not contain the retained candidate"
    ):
        _run(fake, expected_base_commit="wanted000")
    assert [route for route, _ in fake.calls] == [
        "rev-parse",
        "rev-parse",
        "is-ancestor",
    ]


def test_an_existing_descendant_repair_branch_is_reused() -> None:
    fake = FakeSidecar(
        shas={
            "autobuild/FEAT-BD8F": "wanted000",
            "repair/TASK-FEATBD8FFIX1": "c0ffee11",
        },
        ancestors={("wanted000", "repair/TASK-FEATBD8FFIX1")},
    )
    result = _run(fake, expected_base_commit="wanted000")
    assert [route for route, _ in fake.calls] == [
        "rev-parse",
        "rev-parse",
        "is-ancestor",
        "prepare-branch-and-write-tree",
    ]
    assert result.created_branch is False
    assert result.committed is False


def test_a_base_nobody_made_is_a_plain_refusal() -> None:
    fake = FakeSidecar(shas={})
    with pytest.raises(RepairBranchError, match="no branch called 'autobuild/FEAT-BD8F' in the factory's clone"):
        _run(fake)
    assert [r for r, _ in fake.calls] == ["rev-parse"]


def test_a_refused_cut_is_a_plain_refusal() -> None:
    fake = FakeSidecar(shas={"autobuild/FEAT-BD8F": "17497a2a"}, cut_status="failed")
    with pytest.raises(RepairBranchError, match="could not cut repair/TASK-FEATBD8FFIX1 from autobuild/FEAT-BD8F: no such base"):
        _run(fake)


def test_a_refused_commit_is_a_plain_refusal() -> None:
    fake = FakeSidecar(shas={"autobuild/FEAT-BD8F": "17497a2a"}, write_status="failed")
    with pytest.raises(RepairBranchError, match="could not commit the repair task onto repair/TASK-FEATBD8FFIX1: a check refused"):
        _run(fake)


def test_a_dead_sidecar_is_a_plain_refusal() -> None:
    def dead(url: str, body: dict[str, Any], timeout: float) -> tuple[int, Any]:
        raise OSError("connection refused")

    with pytest.raises(RepairBranchError, match="could not be reached at http://127.0.0.1:8925/git/rev-parse"):
        materialise_repair_branch_via_sidecar(
            "http://127.0.0.1:8925", repo=REPO, repo_root=ROOT, task_id="T", base_branch="main",
            files=FILES, message="m", post=dead,
        )


def test_no_files_is_refused_before_any_call() -> None:
    fake = FakeSidecar(shas={"main": "x"})
    with pytest.raises(RepairBranchError, match="no files"):
        materialise_repair_branch_via_sidecar(
            "http://s", repo=REPO, repo_root=ROOT, task_id="T", base_branch="main", files={}, message="m", post=fake,
        )
    assert fake.calls == []

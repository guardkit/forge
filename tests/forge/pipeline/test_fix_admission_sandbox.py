"""A repair of a sandboxed repository is cut where the build runs, from the
branch that carries the code (open item 24, 2026-09-13)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forge.pipeline.fix_admission import (
    _sidecar_for,
    choose_repair_base,
    materialise_repair_task,
)
from forge.pipeline.fix_row_producer import SOURCE_CANDIDATE_REFUSED, SOURCE_MERGE_REPORT


class TestWhereTheRepairIsCutFrom:
    def test_a_candidate_refused_build_is_repaired_on_its_own_branch(self) -> None:
        minted = {"source": SOURCE_CANDIDATE_REFUSED, "source_build_id": "build-FEAT-BD8F-1"}
        assert choose_repair_base(minted, "main", "FEAT-BD8F") == "autobuild/FEAT-BD8F"

    def test_a_merged_build_that_went_red_is_repaired_on_main(self) -> None:
        minted = {"source": SOURCE_MERGE_REPORT, "source_build_id": "build-FEAT-X-1"}
        assert choose_repair_base(minted, "main", "FEAT-X") == "main"

    def test_no_filing_note_keeps_the_branch_it_was_given(self) -> None:
        assert choose_repair_base(None, "release/2", "FEAT-X") == "release/2"
        assert choose_repair_base({}, "main", "") == "main"


class TestFindingTheSandbox:
    def test_a_repository_with_a_sandbox_names_its_sidecar(self) -> None:
        config = SimpleNamespace(
            planning=SimpleNamespace(
                sandboxes={"guardkit/api_test": SimpleNamespace(sidecar_url="http://127.0.0.1:8925")}
            )
        )
        assert _sidecar_for(config, "guardkit/api_test") == ("http://127.0.0.1:8925", "guardkit/api_test")

    def test_a_mapping_entry_works_too(self) -> None:
        config = SimpleNamespace(planning=SimpleNamespace(sandboxes={"r": {"sidecar_url": "http://s"}}))
        assert _sidecar_for(config, "r") == ("http://s", "r")

    def test_no_sandbox_means_none(self) -> None:
        assert _sidecar_for(SimpleNamespace(planning=SimpleNamespace(sandboxes={})), "r") is None
        assert _sidecar_for(SimpleNamespace(), "r") is None
        assert _sidecar_for(SimpleNamespace(planning=SimpleNamespace(sandboxes={"r": {}})), "r") is None
        assert _sidecar_for(SimpleNamespace(planning=SimpleNamespace(sandboxes={"r": {"sidecar_url": "x"}})), None) is None


#: Where the sandbox keeps its clone's working folders — not where the
#: coordinator knows the repository (``tmp_path / "api_test"`` below).
SANDBOX_TREES = "/sandbox/own/clone/api_test/.forge/worktrees"


class FakeSidecar:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.shas: dict[str, str | None] = {"autobuild/FEAT-BD8F": "17497a2a"}

    def __call__(self, url: str, body: dict[str, Any], timeout: float) -> tuple[int, Any]:
        route = url.rsplit("/git/", 1)[1] if "/git/" in url else url.rsplit("/code/", 1)[1]
        self.calls.append((route, body))
        if route in ("list-files-on-branch", "list-files"):
            return 200, {"files": []}
        if route == "rev-parse":
            ref = body["ref"]
            short = ref.removeprefix("refs/heads/")
            return 200, {"sha": self.shas.get(ref, self.shas.get(short))}
        if route == "worktree-add":
            # As the real route does: the sandbox's clone has a path of its
            # own, so only a folder NAME can be acted on; a path from the
            # coordinator's side is refused (3 October 2026, #91).
            if body.get("path") or not body.get("leaf"):
                return 400, {"error": f"'path' {body.get('path')!r} is not a journey worktree of this repository"}
            self.shas[body["branch"]] = "base0"
            path = f"{SANDBOX_TREES}/{body['leaf']}"
            return 200, {"status": "success", "path": path, "reused": False, "detail": ""}
        if route == "worktree-remove":
            if not str(body.get("path")).startswith(f"{SANDBOX_TREES}/"):
                return 400, {"error": f"'path' {body.get('path')!r} is not a journey worktree of this repository"}
            return 200, {"status": "success", "path": body["path"], "detail": ""}
        if route == "prepare-branch-and-write-tree":
            return 200, {"status": "success", "sha": "abc123", "checks": [], "detail": ""}
        return 404, {"error": route}


class TestTheTaskRidesTheSidecar:
    def test_the_files_go_through_the_sidecar_and_never_touch_host_git(self, tmp_path: Path) -> None:
        repo = tmp_path / "api_test"
        repo.mkdir()  # NOT a git checkout: host git would fail loudly if used
        fake = FakeSidecar()
        prepared = materialise_repair_task(
            repo_path=repo,
            task_id="TASK-FEATBD8FFIX1",
            feature_id="FEAT-BD8F",
            name="FEAT-BD8F failed 1 of 9 checks",
            base_branch="autobuild/FEAT-BD8F",
            source_build_id="build-FEAT-BD8F-20260913171308",
            minted={"source": SOURCE_CANDIDATE_REFUSED},
            receipts_root=tmp_path / "receipts",
            sidecar=("http://127.0.0.1:8925", "guardkit/api_test"),
            post=fake,
        )
        assert prepared.branch == "repair/TASK-FEATBD8FFIX1"
        assert prepared.commit == "abc123"
        routes = [r for r, _ in fake.calls]
        # The base branch's task folder and the clone's gate evidence are read
        # in the sandbox first (3 October 2026), then the branch is cut.
        assert routes == [
            "list-files-on-branch",
            "list-files",
            "rev-parse",
            "rev-parse",
            "worktree-add",
            "worktree-remove",
            "prepare-branch-and-write-tree",
        ]
        assert fake.calls[0][1]["branch"] == "autobuild/FEAT-BD8F"
        cut = fake.calls[4][1]
        assert cut["base_ref"] == "17497a2a"
        assert cut["repo"] == "guardkit/api_test"
        written = fake.calls[6][1]["files"]
        assert fake.calls[6][1]["expected_head"] == "17497a2a"
        assert set(written) == {
            ".guardkit/features/TASK-FEATBD8FFIX1.yaml",
            prepared.task_file_path,
        }
        assert "parent_feature: FEAT-BD8F" in written[".guardkit/features/TASK-FEATBD8FFIX1.yaml"]
        assert not (repo / ".git").exists()
        # Nothing the coordinator knows the repository by was sent as a path
        # for the sandbox to act on.
        assert not any(str(repo) in repr(body) for _, body in fake.calls)
        assert fake.calls[5][1]["path"] == f"{SANDBOX_TREES}/repair-TASK-FEATBD8FFIX1"


class TestAPinnedRemoteCommit:
    """A post-merge repair is read and cut at the commit the remote has main
    at, not at the clone's own main, which nothing updates (3 October 2026)."""

    def _materialise(self, tmp_path: Path, fake: FakeSidecar) -> Any:
        repo = tmp_path / "api_test"
        repo.mkdir(exist_ok=True)
        return materialise_repair_task(
            repo_path=repo,
            task_id="TASK-FEATBD8FFIX1",
            feature_id="FEAT-BD8F",
            name="FEAT-BD8F was merged but the checks after it went red",
            base_branch="main",
            source_build_id="build-FEAT-BD8F-20260913171308",
            minted={"source": SOURCE_MERGE_REPORT},
            receipts_root=tmp_path / "receipts",
            sidecar=("http://127.0.0.1:8925", "guardkit/api_test"),
            post=fake,
            pinned_commit="9753535b",
        )

    def test_the_branch_is_cut_from_the_pinned_commit_not_local_main(
        self, tmp_path: Path
    ) -> None:
        fake = FakeSidecar()
        fake.shas.update({"main": "1e58166", "9753535b": "9753535b"})

        self._materialise(tmp_path, fake)

        listing = next(body for route, body in fake.calls if route == "list-files-on-branch")
        assert listing["branch"] == "9753535b"
        cut = next(body for route, body in fake.calls if route == "worktree-add")
        assert cut["base_ref"] == "9753535b"
        written = next(
            body for route, body in fake.calls if route == "prepare-branch-and-write-tree"
        )
        assert written["expected_head"] == "9753535b"
        assert {"repo": "guardkit/api_test", "ref": "refs/heads/main"} not in [
            body for _, body in fake.calls
        ]

    def test_a_pinned_commit_the_clone_does_not_have_cuts_nothing(
        self, tmp_path: Path
    ) -> None:
        from forge.pipeline.repair_branch import RepairBranchError

        fake = FakeSidecar()
        fake.shas["main"] = "1e58166"

        with pytest.raises(RepairBranchError, match="is not in the factory's clone"):
            self._materialise(tmp_path, fake)

        assert "worktree-add" not in [route for route, _ in fake.calls]

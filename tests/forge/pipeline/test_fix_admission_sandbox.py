"""A repair of a sandboxed repository is cut where the build runs, from the
branch that carries the code (open item 24, 2026-09-13)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

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


class FakeSidecar:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.shas: dict[str, str | None] = {"autobuild/FEAT-BD8F": "17497a2a"}

    def __call__(self, url: str, body: dict[str, Any], timeout: float) -> tuple[int, Any]:
        route = url.rsplit("/git/", 1)[1]
        self.calls.append((route, body))
        if route == "rev-parse":
            ref = body["ref"]
            short = ref.removeprefix("refs/heads/")
            return 200, {"sha": self.shas.get(ref, self.shas.get(short))}
        if route == "worktree-add":
            self.shas[body["branch"]] = "base0"
            return 200, {"status": "success", "path": body["path"], "reused": False, "detail": ""}
        if route == "worktree-remove":
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
        assert routes == ["rev-parse", "rev-parse", "worktree-add", "worktree-remove", "prepare-branch-and-write-tree"]
        cut = fake.calls[2][1]
        assert cut["base_ref"] == "17497a2a"
        assert cut["repo"] == "guardkit/api_test"
        written = fake.calls[4][1]["files"]
        assert fake.calls[4][1]["expected_head"] == "17497a2a"
        assert set(written) == {
            ".guardkit/features/TASK-FEATBD8FFIX1.yaml",
            prepared.task_file_path,
        }
        assert "parent_feature: FEAT-BD8F" in written[".guardkit/features/TASK-FEATBD8FFIX1.yaml"]
        assert not (repo / ".git").exists()

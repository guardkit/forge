"""The receipts fold — the pack IN, the per-stage receipts OUT.

Revival design pass §b.2, Stage 1c.

Coverage map:

- **IN** (:class:`TestReadFailurePack`): the failed build's pack index is
  parsed, the families present on disk are reported honestly, a missing
  pack is ``None``, and a corrupt / truncated index degrades to "less
  context" rather than an exception in the journey trying to repair it.
- **OUT** (:class:`TestExportStageReceipts`): each stage lands under
  ``<root>/<build_id>/stages/<NNN>-<stage>/`` with the routine path's
  0700/0600 hardening; repeated stages never overwrite each other; a
  copy failure never blocks the stage.
- **The failed journey's own pack** (:class:`TestFixJourneyFailurePack`):
  success and failure alike leave receipts, the pack points back at the
  build it was repairing, and a prior manifest is archived not destroyed.

Every test injects ``receipts_root`` at a ``tmp_path``, so nothing here
touches ``~/forge-state`` or the process environment.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from forge.pipeline.fix_journey_receipts import (
    FIX_JOURNEY_PACK_KIND,
    STAGES_DIRNAME,
    export_stage_receipts,
    fix_journey_receipts_root,
    next_stage_key,
    read_failure_pack,
    write_fix_journey_failure_pack,
)
from forge.subagents.autobuild_runner import (
    _RECEIPT_FAMILIES,
    FAILURE_MANIFEST_NAME,
    STDOUT_LOG_NAME,
)

FAILED_BUILD = "build-FEAT-OLD-20260730120000"
FIX_BUILD = "build-FEAT-OLD-20260731090000"


def _write_pack(root: Path, build_id: str, **manifest_overrides: Any) -> Path:
    """Lay down a routine-path failure pack under ``root``."""
    pack = root / build_id
    for family in _RECEIPT_FAMILIES:
        (pack / family).mkdir(parents=True, exist_ok=True)
        (pack / family / "verdict.json").write_text("{}", encoding="utf-8")
    (pack / STDOUT_LOG_NAME).write_text("===== autobuild run\n", encoding="utf-8")
    manifest = {
        "build_id": build_id,
        "feature_id": "FEAT-OLD",
        "correlation_id": "corr-123",
        "reason": "gates red: pytest",
        "timed_out": False,
        "exit_code": 1,
        "worktree_path": "/tmp/worktrees/old",
        "branch": "feat/FEAT-OLD",
        "failed_at": "2026-07-30T12:05:00+00:00",
        "receipt_families_exported": list(_RECEIPT_FAMILIES),
        "wedged": False,
        "semantic_state_at_kill": {"task": "TASK-1"},
        "resume": {"command": "guardkit --resume"},
    }
    manifest.update(manifest_overrides)
    (pack / FAILURE_MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return pack


# ---------------------------------------------------------------------------
# Root injection
# ---------------------------------------------------------------------------


class TestRootInjection:
    def test_an_explicit_root_wins(self, tmp_path: Path) -> None:
        assert fix_journey_receipts_root(tmp_path) == tmp_path

    def test_no_root_defers_to_the_routine_paths_own_law(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """One path law in the tree — this module never disagrees with it."""
        monkeypatch.setenv("FORGE_RECEIPTS_DIR", str(tmp_path / "elsewhere"))
        assert fix_journey_receipts_root(None) == tmp_path / "elsewhere"


# ---------------------------------------------------------------------------
# IN — the failure pack the journey consumes
# ---------------------------------------------------------------------------


class TestReadFailurePack:
    def test_a_complete_pack_is_parsed(self, tmp_path: Path) -> None:
        _write_pack(tmp_path, FAILED_BUILD)

        index = read_failure_pack(FAILED_BUILD, receipts_root=tmp_path)

        assert index is not None
        assert index.has_manifest is True
        assert index.build_id == FAILED_BUILD
        assert index.feature_id == "FEAT-OLD"
        assert index.correlation_id == "corr-123"
        assert index.reason == "gates red: pytest"
        assert index.exit_code == 1
        assert index.timed_out is False
        assert index.wedged is False
        assert index.branch == "feat/FEAT-OLD"
        assert index.worktree_path == "/tmp/worktrees/old"
        assert index.receipt_families_exported == tuple(_RECEIPT_FAMILIES)
        assert index.present_families == tuple(_RECEIPT_FAMILIES)
        assert index.stdout_log is not None
        assert index.semantic_state_at_kill == {"task": "TASK-1"}
        assert index.resume == {"command": "guardkit --resume"}

    def test_a_missing_pack_is_none(self, tmp_path: Path) -> None:
        assert read_failure_pack("build-nope", receipts_root=tmp_path) is None

    def test_a_pack_with_no_manifest_still_reports_its_families(
        self, tmp_path: Path
    ) -> None:
        pack = tmp_path / FAILED_BUILD
        (pack / _RECEIPT_FAMILIES[0]).mkdir(parents=True)

        index = read_failure_pack(FAILED_BUILD, receipts_root=tmp_path)

        assert index is not None
        assert index.has_manifest is False
        assert index.present_families == (_RECEIPT_FAMILIES[0],)
        assert index.reason is None

    def test_a_corrupt_manifest_degrades_to_less_context_not_an_exception(
        self, tmp_path: Path
    ) -> None:
        pack = tmp_path / FAILED_BUILD
        pack.mkdir(parents=True)
        (pack / FAILURE_MANIFEST_NAME).write_text("{ truncated", encoding="utf-8")

        index = read_failure_pack(FAILED_BUILD, receipts_root=tmp_path)

        assert index is not None
        assert index.has_manifest is False

    def test_present_families_are_read_from_disk_not_claimed_by_the_manifest(
        self, tmp_path: Path
    ) -> None:
        """The 07-30 coach finding, applied on the read side too."""
        pack = tmp_path / FAILED_BUILD
        pack.mkdir(parents=True)
        (pack / FAILURE_MANIFEST_NAME).write_text(
            json.dumps({"receipt_families_exported": list(_RECEIPT_FAMILIES)}),
            encoding="utf-8",
        )

        index = read_failure_pack(FAILED_BUILD, receipts_root=tmp_path)

        assert index is not None
        assert index.receipt_families_exported == tuple(_RECEIPT_FAMILIES)
        assert index.present_families == ()

    def test_to_context_is_json_safe(self, tmp_path: Path) -> None:
        _write_pack(tmp_path, FAILED_BUILD)
        index = read_failure_pack(FAILED_BUILD, receipts_root=tmp_path)
        assert index is not None

        json.dumps(index.to_context())  # must not raise


# ---------------------------------------------------------------------------
# OUT — the per-stage receipts every fix-journey stage leaves
# ---------------------------------------------------------------------------


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture()
def permissive_umask() -> Any:
    """Write group/world-readable, then restore — the hardening must bite.

    Restored on teardown so the relaxed mask never leaks into a sibling
    test (process-global state).
    """
    previous = os.umask(0o000)
    try:
        yield
    finally:
        os.umask(previous)


class TestExportStageReceipts:
    def _worktree(self, tmp_path: Path) -> Path:
        wt = tmp_path / "worktree"
        for family in _RECEIPT_FAMILIES:
            (wt / family).mkdir(parents=True)
            (wt / family / "coach.json").write_text('{"score": 1.0}', encoding="utf-8")
        return wt

    def test_a_stage_export_lands_under_the_stages_directory(
        self, tmp_path: Path
    ) -> None:
        worktree = self._worktree(tmp_path)

        result = export_stage_receipts(
            build_id=FIX_BUILD,
            stage="task-review",
            worktree_path=worktree,
            receipts_root=tmp_path / "receipts",
        )

        assert result.ok is True
        assert result.stage_key == "001-task-review"
        assert result.dest == (
            tmp_path / "receipts" / FIX_BUILD / STAGES_DIRNAME / "001-task-review"
        )
        assert result.families == tuple(_RECEIPT_FAMILIES)
        for family in _RECEIPT_FAMILIES:
            assert (result.dest / family / "coach.json").is_file()

    def test_repeated_stages_never_overwrite_each_other(
        self, tmp_path: Path
    ) -> None:
        """A fix journey dispatches the same stage several times."""
        worktree = self._worktree(tmp_path)
        root = tmp_path / "receipts"

        first = export_stage_receipts(
            build_id=FIX_BUILD,
            stage="task-review",
            worktree_path=worktree,
            receipts_root=root,
        )
        second = export_stage_receipts(
            build_id=FIX_BUILD,
            stage="task-work",
            worktree_path=worktree,
            receipts_root=root,
            suffix="FIX-1",
        )
        third = export_stage_receipts(
            build_id=FIX_BUILD,
            stage="task-review",
            worktree_path=worktree,
            receipts_root=root,
        )

        assert first.stage_key == "001-task-review"
        assert second.stage_key == "002-task-work-FIX-1"
        assert third.stage_key == "003-task-review"
        stages = root / FIX_BUILD / STAGES_DIRNAME
        assert sorted(p.name for p in stages.iterdir()) == [
            "001-task-review",
            "002-task-work-FIX-1",
            "003-task-review",
        ]

    def test_the_sequence_survives_a_restart(self, tmp_path: Path) -> None:
        """The counter is derived from disk, not held in memory."""
        root = tmp_path / "receipts"
        (root / FIX_BUILD / STAGES_DIRNAME / "007-task-work").mkdir(parents=True)

        assert (
            next_stage_key(FIX_BUILD, "task-review", receipts_root=root)
            == "008-task-review"
        )

    def test_the_pack_is_hardened_to_the_owner(
        self, tmp_path: Path, permissive_umask: Any
    ) -> None:
        """0700 dirs / 0600 files — the FEAT-DRF posture, reused verbatim."""
        worktree = self._worktree(tmp_path)
        root = tmp_path / "receipts"

        result = export_stage_receipts(
            build_id=FIX_BUILD,
            stage="task-review",
            worktree_path=worktree,
            receipts_root=root,
        )

        pack_root = root / FIX_BUILD
        assert _mode(pack_root) == 0o700
        assert _mode(result.dest) == 0o700
        for family in _RECEIPT_FAMILIES:
            assert _mode(result.dest / family) == 0o700
            assert _mode(result.dest / family / "coach.json") == 0o600

    def test_an_export_of_nothing_is_still_a_success(self, tmp_path: Path) -> None:
        result = export_stage_receipts(
            build_id=FIX_BUILD,
            stage="task-review",
            worktree_path=tmp_path / "empty-worktree",
            receipts_root=tmp_path / "receipts",
        )

        assert result.ok is True
        assert result.families == ()
        assert result.dest.is_dir()

    def test_extra_files_ride_alongside_the_families(self, tmp_path: Path) -> None:
        result = export_stage_receipts(
            build_id=FIX_BUILD,
            stage="task-review",
            worktree_path=None,
            receipts_root=tmp_path / "receipts",
            extra_files={"turn.json": json.dumps({"outcome": "dispatched"})},
        )

        assert json.loads((result.dest / "turn.json").read_text()) == {
            "outcome": "dispatched"
        }

    def test_a_copy_failure_is_reported_never_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = self._worktree(tmp_path)

        def boom(*args: Any, **kwargs: Any) -> Any:
            raise OSError("disk full")

        monkeypatch.setattr(
            "forge.pipeline.fix_journey_receipts.shutil.copytree", boom
        )

        result = export_stage_receipts(
            build_id=FIX_BUILD,
            stage="task-review",
            worktree_path=worktree,
            receipts_root=tmp_path / "receipts",
        )

        assert result.ok is False


# ---------------------------------------------------------------------------
# A fix journey that FAILS leaves its own pack
# ---------------------------------------------------------------------------


class TestFixJourneyFailurePack:
    def test_a_failed_journey_writes_its_own_manifest(self, tmp_path: Path) -> None:
        path = write_fix_journey_failure_pack(
            build_id=FIX_BUILD,
            reason="review cycles (2) reached cap (2)",
            outcome="paused-budget",
            feature_id="FEAT-OLD",
            correlation_id="corr-456",
            source_build_id=FAILED_BUILD,
            branch="fix/FEAT-OLD",
            review_cycles=2,
            stage_keys=("001-task-review", "002-task-work"),
            receipts_root=tmp_path,
        )

        assert path is not None
        manifest = json.loads(path.read_text(encoding="utf-8"))
        assert manifest["pack_kind"] == FIX_JOURNEY_PACK_KIND
        assert manifest["build_id"] == FIX_BUILD
        # The cross-pack pointer: which build was being repaired.
        assert manifest["source_build_id"] == FAILED_BUILD
        assert manifest["outcome"] == "paused-budget"
        assert manifest["review_cycles"] == 2
        assert manifest["stage_receipts"] == [
            "001-task-review",
            "002-task-work",
        ]

    def test_the_filename_is_the_routine_paths_so_diagnosers_find_it(
        self, tmp_path: Path
    ) -> None:
        path = write_fix_journey_failure_pack(
            build_id=FIX_BUILD,
            reason="stopped",
            outcome="wait-expired",
            receipts_root=tmp_path,
        )
        assert path is not None
        assert path.name == FAILURE_MANIFEST_NAME

    def test_a_prior_manifest_is_archived_not_destroyed(
        self, tmp_path: Path
    ) -> None:
        pack = tmp_path / FIX_BUILD
        pack.mkdir(parents=True)
        (pack / FAILURE_MANIFEST_NAME).write_text(
            json.dumps({"failed_at": "2026-07-31T08:00:00+00:00", "reason": "first"}),
            encoding="utf-8",
        )

        write_fix_journey_failure_pack(
            build_id=FIX_BUILD,
            reason="second",
            outcome="error",
            receipts_root=tmp_path,
        )

        archived = [
            p
            for p in pack.iterdir()
            if p.name.startswith("failure-manifest.") and p.name != FAILURE_MANIFEST_NAME
        ]
        assert archived, "the earlier run's manifest was destroyed"
        assert json.loads(archived[0].read_text())["reason"] == "first"
        assert json.loads((pack / FAILURE_MANIFEST_NAME).read_text())[
            "reason"
        ] == "second"

    def test_the_pack_is_hardened(
        self, tmp_path: Path, permissive_umask: Any
    ) -> None:
        path = write_fix_journey_failure_pack(
            build_id=FIX_BUILD,
            reason="stopped",
            outcome="error",
            receipts_root=tmp_path,
        )
        assert path is not None
        assert _mode(path) == 0o600
        assert _mode(tmp_path / FIX_BUILD) == 0o700

    def test_a_write_failure_is_reported_as_none_never_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*args: Any, **kwargs: Any) -> Any:
            raise OSError("read-only filesystem")

        monkeypatch.setattr(Path, "write_text", boom)

        assert (
            write_fix_journey_failure_pack(
                build_id=FIX_BUILD,
                reason="stopped",
                outcome="error",
                receipts_root=tmp_path,
            )
            is None
        )

    def test_success_and_failure_alike_leave_receipts(self, tmp_path: Path) -> None:
        """The whole point of the fold, in one assertion."""
        root = tmp_path / "receipts"
        export_stage_receipts(
            build_id=FIX_BUILD,
            stage="task-review",
            worktree_path=None,
            receipts_root=root,
        )
        write_fix_journey_failure_pack(
            build_id=FIX_BUILD,
            reason="stopped",
            outcome="nothing-changed",
            receipts_root=root,
            stage_keys=("001-task-review",),
        )

        pack = root / FIX_BUILD
        assert (pack / FAILURE_MANIFEST_NAME).is_file()
        assert (pack / STAGES_DIRNAME / "001-task-review").is_dir()

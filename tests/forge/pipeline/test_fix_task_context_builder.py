"""The fix task's forward context — the adapter, not a second builder.

Revival design pass §a.3 / §b.2, Stage 1c.

What is proven here:

* The adapter DELEGATES the review→work data dependency to the shipped
  :class:`ForwardContextBuilder` (with Mode C and the fix-task ref
  threaded), rather than re-implementing allowlist gating.
* It EXTENDS that with the failed build's failure-pack index, read from
  an injectable receipts root.
* It never raises: a raising builder, a raising source reader and a
  missing pack each degrade to less context, never to a dead journey.
* The result is JSON-safe end to end (it rides a dispatch payload).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


from forge.lifecycle.modes import BuildMode
from forge.pipeline.fix_task_context_builder import FixTaskContextBuilder
from forge.pipeline.forward_context_builder import ContextEntry, FixTaskRef
from forge.pipeline.stage_taxonomy import StageClass
from forge.subagents.autobuild_runner import FAILURE_MANIFEST_NAME

FIX_BUILD = "build-FEAT-CTX-20260731090000"
FAILED_BUILD = "build-FEAT-CTX-20260730120000"


class _RecordingForwardBuilder:
    def __init__(self, entries: list[ContextEntry] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.entries = entries or []

    def build_for(
        self,
        stage: StageClass,
        build_id: str,
        feature_id: str | None,
        *,
        mode: BuildMode | None = None,
        fix_task: Any = None,
    ) -> list[ContextEntry]:
        self.calls.append(
            {
                "stage": stage,
                "build_id": build_id,
                "feature_id": feature_id,
                "mode": mode,
                "fix_task": fix_task,
            }
        )
        return list(self.entries)


def _fix_task() -> FixTaskRef:
    return FixTaskRef(
        fix_task_id="FIX-1",
        task_review_entry_id="entry-9",
        review_artefact_paths=("/wt/review.md",),
    )


def _write_manifest(root: Path, build_id: str, **fields: Any) -> None:
    pack = root / build_id
    pack.mkdir(parents=True, exist_ok=True)
    payload = {"build_id": build_id, "reason": "gates red: pytest"}
    payload.update(fields)
    (pack / FAILURE_MANIFEST_NAME).write_text(
        json.dumps(payload), encoding="utf-8"
    )


class TestDelegation:
    def test_the_shipped_builder_is_consulted_with_mode_c_and_the_fix_task(
        self, tmp_path: Path
    ) -> None:
        forward = _RecordingForwardBuilder(
            entries=[
                ContextEntry(flag="--fix-task", value="{}", kind="text"),
                ContextEntry(flag="--context", value="/wt/review.md", kind="path"),
            ]
        )
        builder = FixTaskContextBuilder(forward, receipts_root=tmp_path)
        ref = _fix_task()

        context = builder(StageClass.TASK_WORK, FIX_BUILD, ref)

        assert forward.calls == [
            {
                "stage": StageClass.TASK_WORK,
                "build_id": FIX_BUILD,
                "feature_id": None,
                "mode": BuildMode.MODE_C,
                "fix_task": ref,
            }
        ]
        assert context["context_entries"] == [
            {"flag": "--fix-task", "value": "{}", "kind": "text"},
            {"flag": "--context", "value": "/wt/review.md", "kind": "path"},
        ]

    def test_the_result_is_json_safe(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, FIX_BUILD, branch="fix/FEAT-CTX")
        builder = FixTaskContextBuilder(
            _RecordingForwardBuilder(
                entries=[ContextEntry(flag="--context", value="/a", kind="path")]
            ),
            receipts_root=tmp_path,
        )

        json.dumps(builder(StageClass.TASK_WORK, FIX_BUILD, _fix_task()))

    def test_a_raising_builder_degrades_to_no_entries(self, tmp_path: Path) -> None:
        class _Boom:
            def build_for(self, *args: Any, **kwargs: Any) -> Any:
                raise RuntimeError("stage_log gone")

        builder = FixTaskContextBuilder(_Boom(), receipts_root=tmp_path)

        context = builder(StageClass.TASK_WORK, FIX_BUILD, _fix_task())

        assert context["context_entries"] == []


class TestFailurePackExtension:
    def test_the_pack_index_rides_the_context(self, tmp_path: Path) -> None:
        _write_manifest(
            tmp_path,
            FIX_BUILD,
            feature_id="FEAT-CTX",
            branch="fix/FEAT-CTX",
            exit_code=1,
        )
        builder = FixTaskContextBuilder(
            _RecordingForwardBuilder(), receipts_root=tmp_path
        )

        context = builder(StageClass.TASK_WORK, FIX_BUILD, _fix_task())

        pack = context["failure_pack"]
        assert pack is not None
        assert pack["reason"] == "gates red: pytest"
        assert pack["branch"] == "fix/FEAT-CTX"
        assert pack["exit_code"] == 1

    def test_a_missing_pack_is_none_not_a_failure(self, tmp_path: Path) -> None:
        builder = FixTaskContextBuilder(
            _RecordingForwardBuilder(), receipts_root=tmp_path
        )

        context = builder(StageClass.TASK_WORK, FIX_BUILD, _fix_task())

        assert context["failure_pack"] is None
        assert context["context_entries"] == []

    def test_the_source_reader_points_at_the_failed_builds_pack(
        self, tmp_path: Path
    ) -> None:
        """The journey repairs ANOTHER build — read THAT build's pack."""
        _write_manifest(tmp_path, FAILED_BUILD, reason="the original failure")
        _write_manifest(tmp_path, FIX_BUILD, reason="not this one")
        asked: list[str] = []

        def source(fix_build_id: str) -> str:
            asked.append(fix_build_id)
            return FAILED_BUILD

        builder = FixTaskContextBuilder(
            _RecordingForwardBuilder(),
            source_build_id_reader=source,
            receipts_root=tmp_path,
        )

        context = builder(StageClass.TASK_WORK, FIX_BUILD, _fix_task())

        assert asked == [FIX_BUILD]
        assert context["failure_pack"]["build_id"] == FAILED_BUILD
        assert context["failure_pack"]["reason"] == "the original failure"

    def test_a_raising_source_reader_falls_back_to_the_own_directory(
        self, tmp_path: Path
    ) -> None:
        _write_manifest(tmp_path, FIX_BUILD, reason="own pack")

        def boom(_bid: str) -> str:
            raise RuntimeError("no parent column")

        builder = FixTaskContextBuilder(
            _RecordingForwardBuilder(),
            source_build_id_reader=boom,
            receipts_root=tmp_path,
        )

        context = builder(StageClass.TASK_WORK, FIX_BUILD, _fix_task())

        assert context["failure_pack"]["reason"] == "own pack"

    def test_a_source_reader_returning_none_falls_back(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, FIX_BUILD, reason="own pack")
        builder = FixTaskContextBuilder(
            _RecordingForwardBuilder(),
            source_build_id_reader=lambda _bid: None,
            receipts_root=tmp_path,
        )

        context = builder(StageClass.TASK_WORK, FIX_BUILD, _fix_task())

        assert context["failure_pack"]["build_id"] == FIX_BUILD


class TestSupervisorSeamShape:
    def test_it_drops_straight_onto_the_supervisor_field(
        self, tmp_path: Path
    ) -> None:
        """The supervisor calls it positionally: (stage, build_id, fix_task)."""
        builder = FixTaskContextBuilder(
            _RecordingForwardBuilder(), receipts_root=tmp_path
        )
        seam: Any = builder

        result = seam(StageClass.TASK_WORK, FIX_BUILD, _fix_task())

        assert set(result) == {"context_entries", "failure_pack"}

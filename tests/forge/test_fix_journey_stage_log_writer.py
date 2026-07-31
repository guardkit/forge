"""The ``fix_tasks`` producer, round-tripped against the real projection.

Stage 2 shakeout item 5. ``mode_c_history_reader.FIX_TASKS_DETAILS_KEY``
documented a strict contract and **nothing wrote it**: a reader with no
producer. Every real ``task-review`` row was therefore "malformed" by its
own contract and the projection hard-stopped the journey before it could
dispatch a single fix.

The round trip is the point of this module — write with the REAL recorder,
read with the REAL projection, plan with the REAL planner. Two halves
pinned separately would let the contract drift in the middle, which is
exactly how the gap opened.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli._serve_deps_stage_log import (
    build_fix_journey_stage_log_writer,
    default_fix_tasks_extractor,
)
from forge.lifecycle import migrations
from forge.lifecycle.persistence import Build, SqliteLifecyclePersistence
from forge.lifecycle.state_machine import BuildState
from forge.pipeline.dispatchers.subprocess import StageDispatchStatus
from forge.pipeline.mode_c_history_reader import (
    FIX_TASK_ID_DETAILS_KEY,
    FIX_TASKS_DETAILS_KEY,
    SqliteModeCHistoryReader,
)
from forge.pipeline.mode_c_planner import ModeCCyclePlanner
from forge.pipeline.stage_taxonomy import StageClass

BUILD_ID = "build-FEAT-FIX007-20260731"


@pytest.fixture
def pool(tmp_path: Path) -> SqliteLifecyclePersistence:
    cx: sqlite3.Connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    cx.execute(
        "INSERT INTO builds (build_id, feature_id, repo, branch, "
        "feature_yaml_path, status, triggered_by, correlation_id, queued_at, "
        "mode, task_id) VALUES (?, 'FEAT-FIX007', 'r', 'fix/x', 'f.yaml', "
        "'RUNNING', 'cli', 'corr-1', '2026-07-31T00:00:00Z', 'mode-c', "
        "'TASK-FIX007')",
        (BUILD_ID,),
    )
    cx.commit()
    return SqliteLifecyclePersistence(connection=cx)


def _record(
    writer,
    *,
    stage: StageClass,
    artefact_paths: tuple[str, ...] = (),
    status: StageDispatchStatus = StageDispatchStatus.SUCCESS,
    rationale: str = "ok",
) -> None:
    writer.record_dispatch(
        build_id=BUILD_ID,
        stage=stage,
        feature_id=None,
        correlation_id="corr-1:stage:1",
        status=status,
        artefact_paths=artefact_paths,
        rationale=rationale,
        exit_code=0,
        duration_secs=1.0,
    )


class TestTheExtractor:
    """Conservative on purpose — risk h.3 is a wrong fan-out."""

    def test_task_shaped_stems_are_the_fix_tasks(self) -> None:
        assert default_fix_tasks_extractor(
            artefact_paths=(
                "/w/tasks/TASK-FIX007-001.yaml",
                "/w/tasks/TASK-FIX007-002.yaml",
            ),
            rationale="",
        ) == ("TASK-FIX007-001", "TASK-FIX007-002")

    def test_non_task_artefacts_are_not_fix_tasks(self) -> None:
        assert (
            default_fix_tasks_extractor(
                artefact_paths=("/w/README.md", "/w/qa/coach-verdict.json"),
                rationale="",
            )
            == ()
        )

    def test_duplicates_are_dropped_because_the_projection_refuses_them(
        self,
    ) -> None:
        """The planner matches dispatched work by identity."""
        assert default_fix_tasks_extractor(
            artefact_paths=(
                "/w/tasks/TASK-FIX007-001.yaml",
                "/w/other/TASK-FIX007-001.yaml",
            ),
            rationale="",
        ) == ("TASK-FIX007-001",)


class TestTheRoundTrip:
    def test_a_review_row_is_readable_by_the_projection(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        writer = build_fix_journey_stage_log_writer(pool)
        _record(
            writer,
            stage=StageClass.TASK_REVIEW,
            artefact_paths=(
                "/w/tasks/TASK-FIX007-001.yaml",
                "/w/tasks/TASK-FIX007-002.yaml",
            ),
        )

        history = SqliteModeCHistoryReader(pool).get_mode_c_history(BUILD_ID)

        assert len(history) == 1
        entry = history[0]
        assert entry.stage_class is StageClass.TASK_REVIEW
        assert entry.status == "approved"
        assert entry.hard_stop is False
        assert entry.fix_tasks == ("TASK-FIX007-001", "TASK-FIX007-002")

    def test_a_clean_review_records_the_key_with_an_empty_list(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """An empty array is the clean-review answer; an ABSENT key is not.

        The projection refuses an approved review with no ``fix_tasks``
        key at all — "an approved review must state its finding, even when
        the finding is 'nothing'" — so the writer records it either way.
        """
        writer = build_fix_journey_stage_log_writer(pool)
        _record(writer, stage=StageClass.TASK_REVIEW, artefact_paths=())

        rows = pool.read_stages(BUILD_ID)
        assert rows[0].details[FIX_TASKS_DETAILS_KEY] == []

        history = SqliteModeCHistoryReader(pool).get_mode_c_history(BUILD_ID)
        assert history[0].fix_tasks == ()
        assert history[0].hard_stop is False

    def test_a_work_row_carries_its_fix_task_id(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        writer = build_fix_journey_stage_log_writer(pool)
        bound = writer.for_fix_task("TASK-FIX007-001")
        _record(bound, stage=StageClass.TASK_WORK)

        rows = pool.read_stages(BUILD_ID)
        assert rows[0].details[FIX_TASK_ID_DETAILS_KEY] == "TASK-FIX007-001"

        history = SqliteModeCHistoryReader(pool).get_mode_c_history(BUILD_ID)
        assert history[0].fix_task_id == "TASK-FIX007-001"
        assert history[0].status == "approved"

    def test_a_failed_dispatch_projects_as_failed(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        writer = build_fix_journey_stage_log_writer(pool)
        _record(
            writer,
            stage=StageClass.TASK_REVIEW,
            status=StageDispatchStatus.FAILED,
            rationale="subprocess exited 1",
        )

        history = SqliteModeCHistoryReader(pool).get_mode_c_history(BUILD_ID)
        assert history[0].status == "failed"


class TestThePlannerPlansFromIt:
    """The whole point: the projection has to make the planner move."""

    def test_a_review_with_fix_tasks_makes_the_planner_dispatch_work(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        writer = build_fix_journey_stage_log_writer(pool)
        _record(
            writer,
            stage=StageClass.TASK_REVIEW,
            artefact_paths=("/w/tasks/TASK-FIX007-001.yaml",),
        )

        history = SqliteModeCHistoryReader(pool).get_mode_c_history(BUILD_ID)
        plan = ModeCCyclePlanner().plan_next_stage(
            Build(build_id=BUILD_ID, status=BuildState.RUNNING),
            history,
            has_commits=False,
        )

        assert plan.next_stage is StageClass.TASK_WORK
        assert plan.next_fix_task is not None
        assert plan.next_fix_task.fix_task_id == "TASK-FIX007-001"

    def test_the_planner_does_not_re_dispatch_completed_work(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """The attribution key earning its keep.

        Without ``fix_task_id`` on the work row the walk reads it as
        "never dispatched" and fans the same fix out a second time.
        """
        writer = build_fix_journey_stage_log_writer(pool)
        _record(
            writer,
            stage=StageClass.TASK_REVIEW,
            artefact_paths=("/w/tasks/TASK-FIX007-001.yaml",),
        )
        _record(writer.for_fix_task("TASK-FIX007-001"), stage=StageClass.TASK_WORK)

        history = SqliteModeCHistoryReader(pool).get_mode_c_history(BUILD_ID)
        plan = ModeCCyclePlanner().plan_next_stage(
            Build(build_id=BUILD_ID, status=BuildState.RUNNING),
            history,
            has_commits=False,
        )

        assert plan.next_stage is StageClass.TASK_REVIEW
        assert plan.next_fix_task is None

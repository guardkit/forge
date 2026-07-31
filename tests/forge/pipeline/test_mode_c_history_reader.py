"""Tests for the fix journey's ``stage_log`` → planner projection.

The conductor's revival, Stage 1b (design pass §a.3 row 3 / risk §h.3 —
"the least-proven seam is the history projection"). Coverage map:

* The four realistic histories the brief names — clean history, in-flight
  task, malformed review output, hard_stop — :class:`TestRealisticHistories`.
* The status vocabulary translation, including the two rules that cannot
  be got wrong: a dispatch-attempt row is ``running`` (never ``approved``)
  and an unreadable gate is ``pending`` (never ``approved``) —
  :class:`TestStatusProjection`.
* Every malformed-fix-task-list shape produces the LOUD hard-stop entry
  and never a silent empty list — :class:`TestMalformedIsLoud`.
* Unattributable ``/task-work`` rows are malformed too, because the
  planner's walk would dispatch the fix task twice —
  :class:`TestMalformedIsLoud`.
* The projected history drives the real
  :class:`ModeCCyclePlanner` to the right decision — the projection is
  only correct if the planner agrees — :class:`TestPlannerAgreement`.
* The reader satisfies the supervisor's ``ModeCHistoryReader`` Protocol
  and round-trips real rows through a migrated SQLite database —
  :class:`TestSqliteReader`.

Fixtures are built from the *actual* persistence schema: real
:class:`StageLogEntry` values, real ``stage_log`` statuses
(``PASSED`` / ``FAILED`` / ``GATED`` / ``SKIPPED``) and real ``gate_mode``
values, and the SQLite tests write through ``record_stage`` against a
freshly-migrated database.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations
from forge.lifecycle.modes import BuildMode
from forge.lifecycle.persistence import (
    Build,
    SqliteLifecyclePersistence,
    StageLogEntry,
)
from forge.lifecycle.state_machine import BuildState
from forge.pipeline.mode_c_history_reader import (
    FIX_TASK_ID_DETAILS_KEY,
    FIX_TASKS_DETAILS_KEY,
    LIFECYCLE_STATE_DETAILS_KEY,
    SqliteModeCHistoryReader,
    project_mode_c_history,
)
from forge.pipeline.mode_c_planner import ModeCCyclePlanner, ModeCTerminal, ModeCWait
from forge.pipeline.stage_taxonomy import StageClass
from forge.pipeline.supervisor import ModeCHistoryReader as ModeCHistoryReaderProto


_T0 = datetime(2026, 7, 31, 9, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Row builders — the real persistence shapes
# ---------------------------------------------------------------------------


def _row(
    *,
    stage_label: str,
    status: str,
    gate_mode: str | None = None,
    details: dict[str, Any] | None = None,
    build_id: str = "build-fix-1",
    target_identifier: str = "TASK-DEMO-001",
) -> StageLogEntry:
    """Build one realistic ``stage_log`` row as the schema stores it."""
    return StageLogEntry(
        build_id=build_id,
        stage_label=stage_label,
        target_kind="subagent",
        target_identifier=target_identifier,
        status=status,
        gate_mode=gate_mode,
        coach_score=None,
        threshold_applied=None,
        started_at=_T0,
        completed_at=_T0,
        duration_secs=1.0,
        details=details or {},
    )


def _review(
    *,
    fix_tasks: Any = (),
    status: str = "PASSED",
    gate_mode: str | None = None,
    omit_fix_tasks: bool = False,
    extra: dict[str, Any] | None = None,
) -> StageLogEntry:
    details: dict[str, Any] = dict(extra or {})
    if not omit_fix_tasks:
        details[FIX_TASKS_DETAILS_KEY] = fix_tasks
    return _row(
        stage_label=StageClass.TASK_REVIEW.value,
        status=status,
        gate_mode=gate_mode,
        details=details,
    )


def _work(
    fix_task_id: Any = "FIX-001",
    *,
    status: str = "PASSED",
    gate_mode: str | None = None,
    running: bool = False,
    omit_id: bool = False,
) -> StageLogEntry:
    details: dict[str, Any] = {}
    if not omit_id:
        details[FIX_TASK_ID_DETAILS_KEY] = fix_task_id
    if running:
        details[LIFECYCLE_STATE_DETAILS_KEY] = "running"
    return _row(
        stage_label=StageClass.TASK_WORK.value,
        status=status,
        gate_mode=gate_mode,
        details=details,
    )


def _error_entry_present(history: Any) -> bool:
    """Return True iff the projection appended its hard-stop error entry."""
    return bool(history) and (
        history[-1].stage_class is StageClass.TASK_REVIEW
        and history[-1].hard_stop
        and history[-1].status == "failed"
        and history[-1].fix_tasks == ()
    )


# ---------------------------------------------------------------------------
# The four realistic histories the Stage-1b brief names
# ---------------------------------------------------------------------------


class TestRealisticHistories:
    """Clean history, in-flight task, malformed review output, hard_stop."""

    def test_clean_history_projects_a_full_cycle(self) -> None:
        rows = [
            _review(fix_tasks=["FIX-001", "FIX-002"]),
            _work("FIX-001"),
            _work("FIX-002"),
            _review(fix_tasks=[]),
        ]

        history = project_mode_c_history(rows)

        assert [e.stage_class for e in history] == [
            StageClass.TASK_REVIEW,
            StageClass.TASK_WORK,
            StageClass.TASK_WORK,
            StageClass.TASK_REVIEW,
        ]
        assert history[0].fix_tasks == ("FIX-001", "FIX-002")
        assert history[0].status == "approved"
        assert history[0].hard_stop is False
        assert [e.fix_task_id for e in history[1:3]] == ["FIX-001", "FIX-002"]
        # The follow-up review is clean — an EMPTY list, explicitly stated.
        assert history[3].fix_tasks == ()
        assert not _error_entry_present(history)

    def test_in_flight_task_projects_running_not_approved(self) -> None:
        # The dispatch-attempt row is written PASSED with lifecycle_state
        # "running" (the schema has no RUNNING status). Reading that as
        # "approved" would re-open the §h.1 defect one layer down.
        rows = [
            _review(fix_tasks=["FIX-001", "FIX-002"]),
            _work("FIX-001"),
            _work("FIX-002", running=True),
        ]

        history = project_mode_c_history(rows)

        assert history[1].status == "approved"
        assert history[2].status == "running"
        assert history[2].fix_task_id == "FIX-002"
        assert not _error_entry_present(history)

    def test_malformed_review_output_is_a_loud_error_entry(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        rows = [_review(fix_tasks={"not": "a list"})]

        with caplog.at_level(logging.ERROR):
            history = project_mode_c_history(rows)

        assert _error_entry_present(history), (
            "a malformed review output must produce the hard-stop error "
            "entry, never a silent empty fix-task list"
        )
        assert "mode_c_history_projection_malformed" in caplog.text
        # And it must NOT have produced a clean-looking approved review.
        assert all(
            not (e.stage_class is StageClass.TASK_REVIEW and e.status == "approved")
            for e in history
        )

    def test_hard_stop_review_projects_hard_stop(self) -> None:
        rows = [
            _review(fix_tasks=["FIX-001"]),
            _work("FIX-001"),
            _review(
                omit_fix_tasks=True,
                status="GATED",
                gate_mode="HARD_STOP",
            ),
        ]

        history = project_mode_c_history(rows)

        assert history[-1].stage_class is StageClass.TASK_REVIEW
        assert history[-1].hard_stop is True
        assert history[-1].status == "rejected"
        # A hard-stopped review needs no fix-task key: the planner
        # terminates before it ever reads the list.
        assert not _error_entry_present(history)


# ---------------------------------------------------------------------------
# Status vocabulary
# ---------------------------------------------------------------------------


class TestStatusProjection:
    """``stage_log`` statuses / gate modes → the planner's vocabulary."""

    @pytest.mark.parametrize(
        ("status", "gate_mode", "expected", "hard_stop"),
        [
            ("PASSED", None, "approved", False),
            ("FAILED", None, "failed", False),
            ("SKIPPED", None, "cancelled", False),
            ("GATED", "AUTO_APPROVE", "approved", False),
            ("GATED", "HARD_STOP", "rejected", True),
            ("GATED", "FLAG_FOR_REVIEW", "pending", False),
            ("GATED", "MANDATORY_HUMAN_APPROVAL", "pending", False),
        ],
    )
    def test_work_row_status_mapping(
        self,
        status: str,
        gate_mode: str | None,
        expected: str,
        hard_stop: bool,
    ) -> None:
        history = project_mode_c_history(
            [_work("FIX-001", status=status, gate_mode=gate_mode)]
        )

        assert history[0].status == expected
        assert history[0].hard_stop is hard_stop
        assert not _error_entry_present(history)

    def test_gated_row_with_unknown_gate_mode_waits_never_approves(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            history = project_mode_c_history(
                [_review(fix_tasks=["FIX-001"], status="GATED", gate_mode=None)]
            )

        assert history[0].status == "pending"
        assert history[0].hard_stop is False
        assert "unreadable gate_mode" in caplog.text

    def test_unknown_stage_log_status_is_malformed(self) -> None:
        history = project_mode_c_history([_work("FIX-001", status="WOBBLY")])

        assert _error_entry_present(history)

    def test_non_mode_c_labels_are_skipped(self) -> None:
        rows = [
            _row(stage_label="autobuild", status="PASSED"),
            _row(stage_label="feature-spec", status="PASSED"),
            _review(fix_tasks=[]),
            _row(stage_label="deploy", status="PASSED"),
        ]

        history = project_mode_c_history(rows)

        assert len(history) == 1
        assert history[0].stage_class is StageClass.TASK_REVIEW

    def test_empty_history_projects_empty(self) -> None:
        assert project_mode_c_history([]) == ()


# ---------------------------------------------------------------------------
# Garbage is loud (risk §h.3)
# ---------------------------------------------------------------------------


class TestMalformedIsLoud:
    """Every unreadable shape stops the journey; none reads as clean."""

    @pytest.mark.parametrize(
        ("label", "row"),
        [
            ("approved review with no fix_tasks key", _review(omit_fix_tasks=True)),
            ("fix_tasks is a bare string", _review(fix_tasks="FIX-001")),
            ("fix_tasks is a dict", _review(fix_tasks={"a": 1})),
            ("fix_tasks is None", _review(fix_tasks=None)),
            ("fix_tasks holds an int", _review(fix_tasks=["FIX-001", 7])),
            ("fix_tasks holds None", _review(fix_tasks=[None])),
            ("fix_tasks holds an empty string", _review(fix_tasks=["FIX-001", ""])),
            ("fix_tasks holds whitespace", _review(fix_tasks=["   "])),
            ("fix_tasks holds a nested list", _review(fix_tasks=[["FIX-001"]])),
            (
                "fix_tasks holds duplicates",
                _review(fix_tasks=["FIX-001", "FIX-002", "FIX-001"]),
            ),
        ],
    )
    def test_malformed_fix_task_list_never_reads_as_clean(
        self,
        label: str,
        row: StageLogEntry,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.ERROR):
            history = project_mode_c_history([row])

        assert _error_entry_present(history), label
        assert "mode_c_history_projection_malformed" in caplog.text, label
        # The planner must terminate FAILED, not fan out and not go clean.
        plan = ModeCCyclePlanner().plan_next_stage(
            Build(build_id="b", status=BuildState.RUNNING, mode=BuildMode.MODE_C),
            history,
            has_commits=False,
        )
        assert plan.terminal is ModeCTerminal.FAILED, label
        assert plan.next_stage is None, label

    def test_a_bare_string_would_have_fanned_out_per_character(self) -> None:
        # The specific reason a bare string is rejected rather than
        # coerced: iterating it yields one "fix task" per character.
        history = project_mode_c_history([_review(fix_tasks="AB")])

        assert _error_entry_present(history)
        assert all(e.fix_tasks == () for e in history)

    @pytest.mark.parametrize(
        ("label", "row"),
        [
            ("no fix_task_id key", _work(omit_id=True)),
            ("fix_task_id is None", _work(None)),
            ("fix_task_id is an int", _work(7)),
            ("fix_task_id is empty", _work("")),
            ("fix_task_id is whitespace", _work("  ")),
        ],
    )
    def test_unattributable_task_work_row_is_malformed(
        self, label: str, row: StageLogEntry
    ) -> None:
        history = project_mode_c_history([_review(fix_tasks=["FIX-001"]), row])

        assert _error_entry_present(history), label

    def test_non_mapping_details_is_malformed(self) -> None:
        broken = SimpleNamespace(
            stage_label=StageClass.TASK_REVIEW.value,
            status="PASSED",
            gate_mode=None,
            details=["not", "a", "mapping"],
        )

        history = project_mode_c_history([broken])

        assert _error_entry_present(history)

    def test_error_entry_is_appended_once_for_many_problems(self) -> None:
        history = project_mode_c_history(
            [_review(fix_tasks="bad"), _review(fix_tasks={"also": "bad"})]
        )

        assert sum(1 for e in history if e.hard_stop) == 1
        assert _error_entry_present(history)

    def test_a_non_approved_review_may_omit_the_fix_task_key(self) -> None:
        # Only an APPROVED review must state its finding — the planner
        # never reads the list on the pending / failed paths.
        history = project_mode_c_history(
            [
                _review(omit_fix_tasks=True, status="FAILED"),
                _review(
                    omit_fix_tasks=True,
                    status="GATED",
                    gate_mode="MANDATORY_HUMAN_APPROVAL",
                ),
            ]
        )

        assert not _error_entry_present(history)
        assert [e.status for e in history] == ["failed", "pending"]


# ---------------------------------------------------------------------------
# The projection is only right if the planner agrees
# ---------------------------------------------------------------------------


class TestPlannerAgreement:
    """Drive the real planner off projected rows."""

    @staticmethod
    def _plan(rows: Any, *, has_commits: bool = False) -> Any:
        history = project_mode_c_history(rows)
        return ModeCCyclePlanner().plan_next_stage(
            Build(build_id="b", status=BuildState.RUNNING, mode=BuildMode.MODE_C),
            history,
            has_commits=has_commits,
        )

    def test_approved_review_with_fix_tasks_dispatches_the_first(self) -> None:
        plan = self._plan([_review(fix_tasks=["FIX-001", "FIX-002"])])

        assert plan.next_stage is StageClass.TASK_WORK
        assert plan.next_fix_task is not None
        assert plan.next_fix_task.fix_task_id == "FIX-001"

    def test_in_flight_work_makes_the_planner_wait(self) -> None:
        plan = self._plan(
            [
                _review(fix_tasks=["FIX-001", "FIX-002"]),
                _work("FIX-001"),
                _work("FIX-002", running=True),
            ]
        )

        assert plan.is_waiting
        assert plan.wait is ModeCWait.FIX_TASK_IN_FLIGHT

    def test_pending_review_makes_the_planner_wait(self) -> None:
        plan = self._plan(
            [
                _review(
                    omit_fix_tasks=True,
                    status="GATED",
                    gate_mode="MANDATORY_HUMAN_APPROVAL",
                )
            ]
        )

        assert plan.is_waiting
        assert plan.wait is ModeCWait.REVIEW_AWAITING_APPROVAL

    def test_all_work_terminal_schedules_the_follow_up_review(self) -> None:
        plan = self._plan(
            [
                _review(fix_tasks=["FIX-001", "FIX-002"]),
                _work("FIX-001"),
                _work("FIX-002", status="FAILED"),
            ]
        )

        assert plan.next_stage is StageClass.TASK_REVIEW

    def test_clean_follow_up_with_commits_routes_to_the_checkpoint(self) -> None:
        plan = self._plan(
            [
                _review(fix_tasks=["FIX-001"]),
                _work("FIX-001"),
                _review(fix_tasks=[]),
            ],
            has_commits=True,
        )

        assert plan.next_stage is StageClass.PULL_REQUEST_REVIEW

    def test_clean_follow_up_without_commits_terminates(self) -> None:
        plan = self._plan(
            [
                _review(fix_tasks=["FIX-001"]),
                _work("FIX-001"),
                _review(fix_tasks=[]),
            ],
            has_commits=False,
        )

        assert plan.next_stage is None
        assert plan.terminal is ModeCTerminal.CLEAN_REVIEW

    def test_hard_stopped_review_terminates_failed(self) -> None:
        plan = self._plan(
            [_review(omit_fix_tasks=True, status="GATED", gate_mode="HARD_STOP")]
        )

        assert plan.terminal is ModeCTerminal.FAILED


# ---------------------------------------------------------------------------
# The SQLite reader
# ---------------------------------------------------------------------------


@pytest.fixture()
def persistence(tmp_path: Path) -> SqliteLifecyclePersistence:
    """A persistence facade over a freshly-migrated database file."""
    cx: sqlite3.Connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    try:
        yield SqliteLifecyclePersistence(connection=cx)
    finally:
        cx.close()


def _seed_build(persistence: SqliteLifecyclePersistence) -> str:
    payload = SimpleNamespace(
        feature_id="FEAT-FIX-001",
        repo="guardkit/forge",
        branch="lane/fix-journey",
        feature_yaml_path="tasks/backlog/TASK-DEMO-001.yaml",
        max_turns=5,
        sdk_timeout_seconds=1800,
        triggered_by="cli",
        originating_adapter=None,
        originating_user="rich",
        correlation_id="corr-fix-001",
        parent_request_id=None,
        queued_at=_T0,
        requested_at=_T0,
    )
    return persistence.record_pending_build(payload, mode=BuildMode.MODE_C)


class TestSqliteReader:
    """The adapter over the daemon's pool."""

    def test_satisfies_the_supervisor_protocol(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        assert isinstance(
            SqliteModeCHistoryReader(persistence), ModeCHistoryReaderProto
        )

    def test_round_trips_real_rows_through_sqlite(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        build_id = _seed_build(persistence)
        for row in (
            _review(fix_tasks=["FIX-001"]),
            _work("FIX-001", running=True),
        ):
            persistence.record_stage(row.model_copy(update={"build_id": build_id}))

        history = SqliteModeCHistoryReader(persistence).get_mode_c_history(build_id)

        assert [e.stage_class for e in history] == [
            StageClass.TASK_REVIEW,
            StageClass.TASK_WORK,
        ]
        assert history[0].fix_tasks == ("FIX-001",)
        assert history[1].status == "running"

    def test_malformed_details_json_survives_the_sqlite_round_trip(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        # ``_row_to_stage_entry`` degrades unparseable details_json to an
        # empty dict with a warning; an approved review then has no
        # fix_tasks key, which the projection must catch as malformed
        # rather than read as a clean review.
        build_id = _seed_build(persistence)
        persistence.record_stage(
            _review(fix_tasks=["FIX-001"]).model_copy(update={"build_id": build_id})
        )
        persistence.connection.execute(
            "UPDATE stage_log SET details_json = ? WHERE build_id = ?",
            ("{not json", build_id),
        )
        persistence.connection.commit()

        history = SqliteModeCHistoryReader(persistence).get_mode_c_history(build_id)

        assert _error_entry_present(history)

    def test_empty_build_id_is_refused(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        with pytest.raises(ValueError, match="build_id must be non-empty"):
            SqliteModeCHistoryReader(persistence).get_mode_c_history("")

    def test_has_commits_defaults_to_false(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        # The conservative default: the terminal handler's async commit
        # probe is authoritative, and the supervisor already prefers it.
        assert SqliteModeCHistoryReader(persistence).has_commits("build-x") is False

    def test_has_commits_uses_an_injected_reader(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        reader = SqliteModeCHistoryReader(
            persistence, has_commits_reader=lambda build_id: build_id == "yes"
        )

        assert reader.has_commits("yes") is True
        assert reader.has_commits("no") is False

    def test_has_commits_degrades_on_a_raising_reader(
        self,
        persistence: SqliteLifecyclePersistence,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def boom(build_id: str) -> bool:
            raise RuntimeError("git went away")

        reader = SqliteModeCHistoryReader(persistence, has_commits_reader=boom)

        with caplog.at_level(logging.ERROR):
            assert reader.has_commits("build-x") is False
        assert "git went away" in caplog.text

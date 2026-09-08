"""The merge-ready checkpoint's verdict is written into the journey's history.

Attempt fifteen, 2026-09-08. The checkpoint ran the repository's declared
suite inside the sandbox and two tests failed. Nothing durable said so: the
only rows the journey gained were conductor-turn rows, each carrying the
same three words (``chosen_stage="pull-request-review"``,
``outcome="waiting"``, ``rationale="mode-c-commits-present"``). The planner
reads the ``stage_log``, found no checkpoint there, re-read the same clean
follow-up review and chose the checkpoint again — four identical turns, the
same suite run four times, and the nothing-changed rule closed the build.

This pins the writing side and its composition:

* the row itself — status, target identifier, and the evidence a person
  needs to act on it;
* the wrapper around the checkpoint, which records that verdict and marks
  the decision so the conductor's loop knows the turn moved the journey;
* the red-gate action, which asks whether a review cycle is left to loop
  back into and ends the journey when none is;
* the review leg's context, which carries the gate's document when the
  checks sent the journey back and nothing at all when they did not.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli._serve_conductor import (
    make_merge_ready_checkpoint,
    record_checkpoint_verdict,
    with_the_gate_evidence,
)
from forge.cli._serve_deps_stage_log import (
    CHECKPOINT_TARGET_IDENTIFIER,
    build_fix_journey_stage_log_writer,
)
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.pipeline.conductor_driver import CHECKPOINT_VERDICT_RECORDED_KEY
from forge.pipeline.fix_task_context_builder import (
    GATE_FAILURES_DOCUMENT_NAME,
    GATE_FAILURES_INSTRUCTION,
)
from forge.pipeline.merge_ready_checkpoint import (
    GatesReport,
    GateStatus,
    MergeCardOutcome,
)
from forge.pipeline.mode_c_history_reader import (
    CHECKPOINT_COMMAND_DETAILS_KEY,
    CHECKPOINT_EVIDENCE_DETAILS_KEY,
    CHECKPOINT_EXIT_CODE_DETAILS_KEY,
    CHECKPOINT_FAILING_TESTS_DETAILS_KEY,
    project_mode_c_history,
)
from forge.pipeline.stage_taxonomy import StageClass

BUILD_ID = "build-FEAT-39F6-20260908172338"
FEATURE_ID = "FEAT-39F6"
TASK_ID = "TASK-39F6001"
FAILING = (
    "tests/users/test_router.py::TestDeleteUserByEmail::test_by_email_delete_success",
    "tests/users/test_router.py::TestDeleteUserByEmail::test_by_email_delete_selective",
)


# ---------------------------------------------------------------------------
# Fixtures and fakes
# ---------------------------------------------------------------------------


@pytest.fixture()
def pool(tmp_path: Path) -> SqliteLifecyclePersistence:
    """A real SQLite ledger with one Mode C build row on it."""
    cx: sqlite3.Connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    cx.execute(
        "INSERT INTO builds (build_id, feature_id, repo, branch, "
        "feature_yaml_path, status, triggered_by, correlation_id, queued_at, "
        "worktree_path, mode, task_id, profile) VALUES (?, ?, 'api_test', "
        "'fix/FEAT-39F6', ?, 'RUNNING', 'cli', 'corr-1', "
        "'2026-09-08T17:23:38+00:00', ?, 'mode-c', ?, 'fix-journey')",
        (
            BUILD_ID,
            FEATURE_ID,
            str(tmp_path / "worktree" / "tasks" / "fix.yaml"),
            str(tmp_path / "worktree"),
            TASK_ID,
        ),
    )
    cx.commit()
    return SqliteLifecyclePersistence(connection=cx)


def _red_report() -> GatesReport:
    """What the production gate-set reader answers on a red declared suite."""

    class _Detail(str):
        """The runner's sentence, with the run kept on it (F1's shape)."""

        command = "uv run pytest -q"
        exit_code = 1
        failing_cases = FAILING
        evidence = "FAILED tests/users/test_router.py::x - AssertionError"

    return GatesReport(
        status=GateStatus.RED,
        failed_gates=("declared toolchain test — 2 failed: a, b",),
        detail=_Detail("`uv run pytest -q` exited 1 in /work"),
        evidence="FAILED tests/users/test_router.py::x - AssertionError",
    )


def _green_report() -> GatesReport:
    return GatesReport(
        status=GateStatus.GREEN, detail="`uv run pytest -q` exited 0 in /work"
    )


@dataclass
class _FakeCard:
    publishes: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, **kwargs: Any) -> str:
        self.publishes.append(kwargs)
        return "RESUMED"


def _checkpoint_rows(pool: Any) -> list[Any]:
    return [
        row
        for row in pool.read_stages(BUILD_ID)
        if row.stage_label == StageClass.PULL_REQUEST_REVIEW.value
    ]


def _record_a_review(pool: Any, *, fix_tasks: tuple[str, ...] = ()) -> None:
    from forge.lifecycle.persistence import StageLogEntry
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    pool.record_stage(
        StageLogEntry(
            build_id=BUILD_ID,
            stage_label="task-review",
            target_kind="local_tool",
            target_identifier=FEATURE_ID,
            status="PASSED",
            gate_mode=None,
            coach_score=None,
            threshold_applied=None,
            started_at=now,
            completed_at=now,
            duration_secs=1.0,
            details={"fix_tasks": list(fix_tasks)},
        )
    )


# ---------------------------------------------------------------------------
# The row
# ---------------------------------------------------------------------------


class TestTheRowTheCheckpointWrites:
    def test_a_red_verdict_becomes_a_failed_checkpoint_row(self, pool) -> None:
        writer = build_fix_journey_stage_log_writer(pool)

        writer.record_checkpoint(
            build_id=BUILD_ID,
            feature_id=FEATURE_ID,
            green=False,
            rationale="the merge-ready checks are red: `uv run pytest -q` exited 1",
            failing_tests=FAILING,
            failed_gates=("declared toolchain test",),
            declared_test_command="uv run pytest -q",
            declared_test_exit_code=1,
            evidence="FAILED tests/users/test_router.py::x",
        )

        rows = _checkpoint_rows(pool)
        assert len(rows) == 1
        assert rows[0].status == "FAILED"
        assert rows[0].details[CHECKPOINT_FAILING_TESTS_DETAILS_KEY] == list(FAILING)
        assert rows[0].details[CHECKPOINT_COMMAND_DETAILS_KEY] == "uv run pytest -q"
        assert rows[0].details[CHECKPOINT_EXIT_CODE_DETAILS_KEY] == 1
        assert rows[0].details[CHECKPOINT_EVIDENCE_DETAILS_KEY] == (
            "FAILED tests/users/test_router.py::x"
        )

    def test_a_green_verdict_becomes_a_passed_checkpoint_row(self, pool) -> None:
        writer = build_fix_journey_stage_log_writer(pool)

        writer.record_checkpoint(
            build_id=BUILD_ID,
            feature_id=FEATURE_ID,
            green=True,
            rationale="the merge-ready checks are green",
        )

        rows = _checkpoint_rows(pool)
        assert rows[0].status == "PASSED"
        assert rows[0].details[CHECKPOINT_FAILING_TESTS_DETAILS_KEY] == []

    def test_the_row_is_not_wearing_the_merge_cards_name(self, pool) -> None:
        """A row bearing the card's identifier would silence every checkpoint."""
        from forge.cli._serve_gate_activation import _MERGE_CARD_TARGET_IDENTIFIER

        writer = build_fix_journey_stage_log_writer(pool)
        writer.record_checkpoint(
            build_id=BUILD_ID, feature_id=FEATURE_ID, green=False, rationale="red"
        )

        assert _checkpoint_rows(pool)[0].target_identifier == (
            CHECKPOINT_TARGET_IDENTIFIER
        )
        assert CHECKPOINT_TARGET_IDENTIFIER != _MERGE_CARD_TARGET_IDENTIFIER

    def test_the_row_reads_back_through_the_projection(self, pool) -> None:
        writer = build_fix_journey_stage_log_writer(pool)
        writer.record_checkpoint(
            build_id=BUILD_ID,
            feature_id=FEATURE_ID,
            green=False,
            rationale="the merge-ready checks are red",
            failing_tests=FAILING,
        )

        history = project_mode_c_history(pool.read_stages(BUILD_ID))

        assert len(history) == 1
        assert history[0].stage_class is StageClass.PULL_REQUEST_REVIEW
        assert history[0].status == "failed"
        assert history[0].failing_tests == FAILING


# ---------------------------------------------------------------------------
# The wrapper around the checkpoint
# ---------------------------------------------------------------------------


class TestTheDecisionCarriesThatItWasRecorded:
    def test_a_red_decision_is_recorded_and_marked(self, pool) -> None:
        writer = build_fix_journey_stage_log_writer(pool)

        @dataclass(frozen=True)
        class _Decision:
            build_id: str = BUILD_ID
            feature_id: str = FEATURE_ID
            gates: Any = None
            details: dict[str, Any] = field(default_factory=dict)

        marked = record_checkpoint_verdict(
            _Decision(gates=_red_report()), pool=pool, stage_log_writer=writer
        )

        assert marked.details[CHECKPOINT_VERDICT_RECORDED_KEY] is True
        rows = _checkpoint_rows(pool)
        assert rows[0].status == "FAILED"
        assert rows[0].details[CHECKPOINT_FAILING_TESTS_DETAILS_KEY] == list(FAILING)
        assert rows[0].details[CHECKPOINT_COMMAND_DETAILS_KEY] == "uv run pytest -q"

    def test_a_decision_that_read_no_gate_set_writes_no_row(self, pool) -> None:
        writer = build_fix_journey_stage_log_writer(pool)

        @dataclass(frozen=True)
        class _Decision:
            build_id: str = BUILD_ID
            feature_id: str = FEATURE_ID
            gates: Any = None
            details: dict[str, Any] = field(default_factory=dict)

        returned = record_checkpoint_verdict(
            _Decision(), pool=pool, stage_log_writer=writer
        )

        assert _checkpoint_rows(pool) == []
        assert CHECKPOINT_VERDICT_RECORDED_KEY not in returned.details

    def test_a_writer_that_cannot_record_leaves_the_decision_alone(
        self, pool, caplog
    ) -> None:
        import logging

        class _Broken:
            def record_checkpoint(self, **_kwargs: Any) -> None:
                raise RuntimeError("the ledger is locked")

        @dataclass(frozen=True)
        class _Decision:
            build_id: str = BUILD_ID
            feature_id: str = FEATURE_ID
            gates: Any = None
            details: dict[str, Any] = field(default_factory=dict)

        with caplog.at_level(logging.ERROR):
            returned = record_checkpoint_verdict(
                _Decision(gates=_red_report()), pool=pool, stage_log_writer=_Broken()
            )

        assert CHECKPOINT_VERDICT_RECORDED_KEY not in returned.details
        assert any("bounded by the" in r.getMessage() for r in caplog.records)


class TestTheComposedCheckpoint:
    def test_a_red_gate_with_a_cycle_left_loops_back_and_leaves_a_row(
        self, pool
    ) -> None:
        _record_a_review(pool, fix_tasks=("TASK-39F6-001",))
        checkpoint = make_merge_ready_checkpoint(
            pool=pool,
            publish_card=_FakeCard(),
            gates_green_reader=lambda **_kw: _red_report(),
            stage_log_writer=build_fix_journey_stage_log_writer(pool),
            review_cycle_cap=3,
        )

        decision = asyncio.run(
            checkpoint.submit_decision(
                build_id=BUILD_ID,
                feature_id=FEATURE_ID,
                auto_approve=False,
                rationale="mode-c-commits-present",
            )
        )

        assert decision.outcome is MergeCardOutcome.RED_GATE_LOOP_BACK
        assert decision.details[CHECKPOINT_VERDICT_RECORDED_KEY] is True
        assert _checkpoint_rows(pool)[0].status == "FAILED"

    def test_a_red_gate_with_no_cycle_left_ends_the_journey(self, pool) -> None:
        _record_a_review(pool, fix_tasks=("TASK-39F6-001",))
        _record_a_review(pool)
        checkpoint = make_merge_ready_checkpoint(
            pool=pool,
            publish_card=_FakeCard(),
            gates_green_reader=lambda **_kw: _red_report(),
            stage_log_writer=build_fix_journey_stage_log_writer(pool),
            review_cycle_cap=2,
        )

        decision = asyncio.run(
            checkpoint.submit_decision(
                build_id=BUILD_ID,
                feature_id=FEATURE_ID,
                auto_approve=False,
                rationale="mode-c-commits-present",
            )
        )

        assert decision.outcome is MergeCardOutcome.RED_GATE_FAILED
        assert decision.is_terminal_failed is True
        assert _checkpoint_rows(pool)[0].status == "FAILED"

    def test_a_green_gate_still_publishes_exactly_one_card(self, pool) -> None:
        card = _FakeCard()
        checkpoint = make_merge_ready_checkpoint(
            pool=pool,
            publish_card=card,
            gates_green_reader=lambda **_kw: _green_report(),
            stage_log_writer=build_fix_journey_stage_log_writer(pool),
            review_cycle_cap=3,
        )

        decision = asyncio.run(
            checkpoint.submit_decision(
                build_id=BUILD_ID,
                feature_id=FEATURE_ID,
                auto_approve=False,
                rationale="mode-c-commits-present",
            )
        )

        assert decision.outcome is MergeCardOutcome.CARD_PUBLISHED
        assert len(card.publishes) == 1
        assert _checkpoint_rows(pool)[0].status == "PASSED"

    def test_a_composition_with_no_writer_is_the_publisher_itself(self, pool) -> None:
        """Every caller that predates this lane gets exactly what it got."""
        from forge.pipeline.merge_ready_checkpoint import (
            MergeReadyCheckpointPublisher,
        )

        checkpoint = make_merge_ready_checkpoint(
            pool=pool, publish_card=None, gates_green_reader=lambda **_kw: True
        )

        assert isinstance(checkpoint, MergeReadyCheckpointPublisher)

    def test_the_same_two_reviews_loop_back_under_a_cap_of_three(self, pool) -> None:
        """One arithmetic: the planner's, the guard's and the gate's.

        The pair above and this one differ in ONE number — the profile's cap —
        and the journey ends or carries on accordingly. That is the whole of
        the rule, and it is counted the way the budget guard counts it, so no
        dispatch the planner chooses can be one the guard then refuses.
        """
        _record_a_review(pool, fix_tasks=("TASK-39F6-001",))
        _record_a_review(pool)
        checkpoint = make_merge_ready_checkpoint(
            pool=pool,
            publish_card=_FakeCard(),
            gates_green_reader=lambda **_kw: _red_report(),
            stage_log_writer=build_fix_journey_stage_log_writer(pool),
            review_cycle_cap=3,
        )

        decision = asyncio.run(
            checkpoint.submit_decision(
                build_id=BUILD_ID,
                feature_id=FEATURE_ID,
                auto_approve=False,
                rationale="mode-c-commits-present",
            )
        )

        assert decision.outcome is MergeCardOutcome.RED_GATE_LOOP_BACK


# ---------------------------------------------------------------------------
# The review leg's context
# ---------------------------------------------------------------------------


class TestTheReviewLegIsHandedTheGatesEvidence:
    def _inner(self) -> Any:
        def build(_stage: Any, _build_id: str, _fix_task: Any) -> dict[str, Any]:
            return {
                "context_entries": [
                    {"flag": "--context", "value": "/work/plan.md", "kind": "path"}
                ],
                "failure_pack": None,
            }

        return build

    def test_a_review_after_red_checks_carries_the_document(
        self, pool, tmp_path
    ) -> None:
        (tmp_path / "worktree").mkdir()
        build_fix_journey_stage_log_writer(pool).record_checkpoint(
            build_id=BUILD_ID,
            feature_id=FEATURE_ID,
            green=False,
            rationale="the merge-ready checks are red",
            failing_tests=FAILING,
            declared_test_command="uv run pytest -q",
            declared_test_exit_code=1,
            evidence="FAILED tests/users/test_router.py::x",
        )
        builder = with_the_gate_evidence(self._inner(), pool=pool)

        context = builder(StageClass.TASK_REVIEW, BUILD_ID, None)

        entries = context["context_entries"]
        assert entries[0]["value"] == "/work/plan.md", "what was there is untouched"
        assert entries[1]["kind"] == "path"
        assert entries[1]["value"].endswith(GATE_FAILURES_DOCUMENT_NAME)
        assert GATE_FAILURES_INSTRUCTION in Path(entries[1]["value"]).read_text(
            encoding="utf-8"
        )

    def test_a_sandbox_repository_sends_the_same_words_with_the_dispatch(
        self, pool
    ) -> None:
        """The worktree is inside the sandbox, so the document rides the request.

        The build row's worktree is not created here, which is exactly what a
        repository with a sandbox looks like from forge-prod: the tree is in
        there and this side can neither read nor write it. The assertion is on
        the REQUEST's own field — the context entry the dispatcher is handed —
        not on any sidecar.
        """
        build_fix_journey_stage_log_writer(pool).record_checkpoint(
            build_id=BUILD_ID,
            feature_id=FEATURE_ID,
            green=False,
            rationale="the merge-ready checks are red",
            failing_tests=FAILING,
            declared_test_command="uv run pytest -q",
            declared_test_exit_code=1,
            evidence="FAILED tests/users/test_router.py::x",
        )
        builder = with_the_gate_evidence(self._inner(), pool=pool)

        context = builder(StageClass.TASK_REVIEW, BUILD_ID, None)

        entry = context["context_entries"][1]
        assert entry["flag"] == "--context"
        assert entry["kind"] == "text"
        assert GATE_FAILURES_INSTRUCTION in entry["value"]
        assert "uv run pytest -q" in entry["value"]
        assert FAILING[0] in entry["value"]

    def test_a_review_the_checks_never_sent_back_is_untouched(self, pool) -> None:
        builder = with_the_gate_evidence(self._inner(), pool=pool)

        assert builder(StageClass.TASK_REVIEW, BUILD_ID, None) == self._inner()(
            None, BUILD_ID, None
        )

    def test_a_work_leg_is_never_handed_the_document(self, pool, tmp_path) -> None:
        (tmp_path / "worktree").mkdir()
        build_fix_journey_stage_log_writer(pool).record_checkpoint(
            build_id=BUILD_ID,
            feature_id=FEATURE_ID,
            green=False,
            rationale="red",
            failing_tests=FAILING,
        )
        builder = with_the_gate_evidence(self._inner(), pool=pool)

        context = builder(StageClass.TASK_WORK, BUILD_ID, None)

        assert len(context["context_entries"]) == 1

    def test_no_context_builder_stays_no_context_builder(self, pool) -> None:
        assert with_the_gate_evidence(None, pool=pool) is None


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])

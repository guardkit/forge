"""A RED merge-ready checkpoint goes back into the fix cycle, once, with its evidence.

Attempt fifteen, 2026-09-08, build ``build-FEAT-39F6-20260908172338``. The
whole fix cycle ran: a review that named the cause, four approved work legs,
a clean follow-up review. The merge-ready checkpoint then ran the
repository's declared suite on the journey worktree — 2 failed, 817 passed —
because one of the four fix tasks had made a soft-deleted user unfindable by
email, which the feature's own tests require.

Nothing then did anything with what the checkpoint found. Its verdict lived
on a turn report; the journey's history had no row for it; so the stateless
planner re-read the same clean follow-up review and chose the checkpoint
again, three more times, forty seconds each, until the turn-level
nothing-changed rule stopped the build.

What is pinned here, in the pieces that decide it:

* the history reader projects the checkpoint's row (and skips every label it
  does not know, which is every legacy ledger);
* the planner reads a RED row and sends the failing tests back to the review
  seat while a review cycle remains, and ends the journey naming them when
  none does;
* the planner never chooses the checkpoint again on a history whose last
  word is a red checkpoint;
* the terminal handler agrees rather than asking for the same checks twice;
* the gate-driven review's document says what ran, what failed and what a
  fix task may not do — and travels as a file when the worktree is here and
  with the dispatch when it is inside a sandbox.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from forge.lifecycle.modes import BuildMode
from forge.lifecycle.persistence import Build
from forge.lifecycle.state_machine import BuildState
from forge.pipeline.fix_task_context_builder import (
    GATE_FAILURES_DOCUMENT_NAME,
    GATE_FAILURES_INSTRUCTION,
    build_gate_evidence_context,
    read_gate_failures,
    render_gate_failures,
)
from forge.pipeline.mode_c_history_reader import (
    CHECKPOINT_COMMAND_DETAILS_KEY,
    CHECKPOINT_EVIDENCE_DETAILS_KEY,
    CHECKPOINT_EXIT_CODE_DETAILS_KEY,
    CHECKPOINT_FAILING_TESTS_DETAILS_KEY,
    project_mode_c_history,
)
from forge.pipeline.mode_c_planner import (
    ModeCCyclePlanner,
    ModeCTerminal,
    StageEntry,
    a_review_cycle_remains,
    latest_checkpoint_on_this_tree,
)
from forge.pipeline.stage_taxonomy import StageClass
from forge.pipeline.terminal_handlers.mode_c import (
    RATIONALE_FAILED_MERGE_READY_RED,
    ModeCTerminal as HandlerTerminal,
    evaluate_terminal,
)

#: The two tests attempt fifteen's suite really failed on.
FAILING = (
    "tests/users/test_router.py::TestDeleteUserByEmail::test_by_email_delete_success",
    "tests/users/test_router.py::TestDeleteUserByEmail::test_by_email_delete_selective",
)

TASK_ID = "TASK-39F6001"


# ---------------------------------------------------------------------------
# Row and entry helpers
# ---------------------------------------------------------------------------


@dataclass
class _Row:
    """A ``stage_log`` row, in the shape both readers duck-type."""

    stage_label: str
    status: str = "PASSED"
    gate_mode: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


def _review_row(*, fix_tasks: tuple[str, ...] = ()) -> _Row:
    return _Row(stage_label="task-review", details={"fix_tasks": list(fix_tasks)})


def _work_row(fix_task_id: str) -> _Row:
    return _Row(stage_label="task-work", details={"fix_task_id": fix_task_id})


def _checkpoint_row(*, green: bool, failing: tuple[str, ...] = FAILING) -> _Row:
    return _Row(
        stage_label="pull-request-review",
        status="PASSED" if green else "FAILED",
        details={
            "rationale": (
                "the merge-ready checks are green"
                if green
                else "the merge-ready checks are red: `pytest -q` exited 1"
            ),
            CHECKPOINT_FAILING_TESTS_DETAILS_KEY: list(() if green else failing),
            "failed_gates": [] if green else ["declared toolchain test"],
            CHECKPOINT_COMMAND_DETAILS_KEY: "uv run pytest -q",
            CHECKPOINT_EXIT_CODE_DETAILS_KEY: 0 if green else 1,
            CHECKPOINT_EVIDENCE_DETAILS_KEY: (
                "" if green else "FAILED tests/users/test_router.py::x - AssertionError"
            ),
        },
    )


def _build() -> Build:
    return Build(
        build_id="build-FEAT-39F6-20260908172338",
        status=BuildState.RUNNING,
        mode=BuildMode.MODE_C,
    )


def _one_cycle_then_a_red_gate() -> tuple[StageEntry, ...]:
    """Attempt fifteen's history: review, work, clean follow-up, red checks."""
    return project_mode_c_history(
        [
            _review_row(fix_tasks=("TASK-39F6-001",)),
            _work_row("TASK-39F6-001"),
            _review_row(),
            _checkpoint_row(green=False),
        ]
    )


# ---------------------------------------------------------------------------
# The history reader
# ---------------------------------------------------------------------------


class TestTheCheckpointsVerdictEntersTheHistory:
    def test_a_red_checkpoint_row_projects_as_a_failed_checkpoint_entry(
        self,
    ) -> None:
        history = project_mode_c_history([_checkpoint_row(green=False)])

        assert len(history) == 1
        assert history[0].stage_class is StageClass.PULL_REQUEST_REVIEW
        assert history[0].status == "failed"

    def test_a_red_row_carries_the_failing_tests_and_its_own_reason(self) -> None:
        history = project_mode_c_history([_checkpoint_row(green=False)])

        assert history[0].failing_tests == FAILING
        assert "red" in (history[0].failure_reason or "")

    def test_a_green_checkpoint_row_projects_as_approved_with_no_failures(
        self,
    ) -> None:
        history = project_mode_c_history([_checkpoint_row(green=True)])

        assert history[0].status == "approved"
        assert history[0].failing_tests == ()
        assert history[0].failure_reason is None

    def test_a_row_with_no_failing_tests_key_is_read_not_refused(self) -> None:
        """A checkpoint row from a repository whose tool named nothing."""
        row = _Row(
            stage_label="pull-request-review",
            status="FAILED",
            details={"rationale": "the merge-ready checks are red"},
        )

        history = project_mode_c_history([row])

        assert history[0].status == "failed"
        assert history[0].failing_tests == ()

    def test_a_malformed_failing_tests_list_costs_names_not_the_journey(
        self,
    ) -> None:
        row = _Row(
            stage_label="pull-request-review",
            status="FAILED",
            details={CHECKPOINT_FAILING_TESTS_DETAILS_KEY: "one::big::string"},
        )

        history = project_mode_c_history([row])

        assert len(history) == 1, "a malformed name list must not hard-stop"
        assert history[0].failing_tests == ()
        assert history[0].hard_stop is False

    def test_rows_the_projection_does_not_know_are_skipped(self) -> None:
        """A legacy ledger projects exactly as it always did."""
        rows = [
            _Row(stage_label="autobuild"),
            _Row(stage_label="conductor-turn"),
            _Row(stage_label="conductor-close-out"),
            _review_row(fix_tasks=("TASK-39F6-001",)),
            _Row(stage_label="feature-spec"),
        ]

        history = project_mode_c_history(rows)

        assert [e.stage_class for e in history] == [StageClass.TASK_REVIEW]


class TestWhichCheckpointIsAboutThisTree:
    def test_the_red_checkpoint_is_found_when_nothing_has_run_since(self) -> None:
        checkpoint = latest_checkpoint_on_this_tree(_one_cycle_then_a_red_gate())

        assert checkpoint is not None
        assert checkpoint.status == "failed"

    def test_a_review_after_the_checkpoint_means_it_is_about_an_older_tree(
        self,
    ) -> None:
        history = project_mode_c_history(
            [
                _review_row(fix_tasks=("TASK-39F6-001",)),
                _work_row("TASK-39F6-001"),
                _review_row(),
                _checkpoint_row(green=False),
                _review_row(fix_tasks=("TASK-39F6-009",)),
            ]
        )

        assert latest_checkpoint_on_this_tree(history) is None

    def test_a_history_with_no_checkpoint_answers_nothing(self) -> None:
        history = project_mode_c_history([_review_row(fix_tasks=("TASK-A",))])

        assert latest_checkpoint_on_this_tree(history) is None


# ---------------------------------------------------------------------------
# The planner
# ---------------------------------------------------------------------------


class TestARedCheckpointGoesBackIntoTheFixCycle:
    def test_with_a_cycle_left_the_next_stage_is_a_review(self) -> None:
        plan = ModeCCyclePlanner().plan_next_stage(
            _build(),
            _one_cycle_then_a_red_gate(),
            has_commits=True,
            review_cycle_cap=3,
        )

        assert plan.next_stage is StageClass.TASK_REVIEW
        assert plan.terminal is None

    def test_the_reason_is_plain_and_names_how_much_budget_is_left(self) -> None:
        plan = ModeCCyclePlanner().plan_next_stage(
            _build(),
            _one_cycle_then_a_red_gate(),
            has_commits=True,
            review_cycle_cap=3,
        )

        assert plan.rationale == (
            "the merge-ready checks failed (2 failing tests) — one review "
            "cycle remains, sending the failures back to the review seat"
        )

    def test_the_checkpoint_is_never_chosen_twice_on_the_same_tree(self) -> None:
        """The seam itself: before this the same plan came back four times."""
        history = _one_cycle_then_a_red_gate()

        for _ in range(4):
            plan = ModeCCyclePlanner().plan_next_stage(
                _build(), history, has_commits=True, review_cycle_cap=3
            )
            assert plan.next_stage is not StageClass.PULL_REQUEST_REVIEW

    def test_with_no_cycle_left_the_journey_ends_naming_the_tests(self) -> None:
        plan = ModeCCyclePlanner().plan_next_stage(
            _build(),
            _one_cycle_then_a_red_gate(),
            has_commits=True,
            review_cycle_cap=2,
        )

        assert plan.next_stage is None
        assert plan.terminal is ModeCTerminal.FAILED
        assert plan.rationale == (
            "the merge-ready checks stayed red and no review cycle is left — "
            f"{FAILING[0]}, {FAILING[1]}"
        )

    def test_an_uncapped_profile_always_has_a_cycle_to_spend(self) -> None:
        plan = ModeCCyclePlanner().plan_next_stage(
            _build(), _one_cycle_then_a_red_gate(), has_commits=True
        )

        assert plan.next_stage is StageClass.TASK_REVIEW
        assert "this profile caps no review cycles" in plan.rationale

    def test_a_red_row_that_named_no_test_still_says_something(self) -> None:
        history = project_mode_c_history(
            [
                _review_row(fix_tasks=("TASK-A",)),
                _work_row("TASK-A"),
                _review_row(),
                _Row(
                    stage_label="pull-request-review",
                    status="FAILED",
                    details={"rationale": "the gate set answered UNKNOWN"},
                ),
            ]
        )

        plan = ModeCCyclePlanner().plan_next_stage(
            _build(), history, has_commits=True, review_cycle_cap=2
        )

        assert plan.terminal is ModeCTerminal.FAILED
        assert "the gate set answered UNKNOWN" in plan.rationale

    def test_a_green_checkpoint_leaves_todays_route_alone(self) -> None:
        """A green row must not divert the clean-review branch."""
        history = project_mode_c_history(
            [
                _review_row(fix_tasks=("TASK-A",)),
                _work_row("TASK-A"),
                _review_row(),
                _checkpoint_row(green=True),
            ]
        )

        plan = ModeCCyclePlanner().plan_next_stage(
            _build(), history, has_commits=True, review_cycle_cap=3
        )

        assert plan.next_stage is StageClass.PULL_REQUEST_REVIEW

    def test_a_history_with_no_checkpoint_plans_exactly_as_before(self) -> None:
        history = project_mode_c_history(
            [
                _review_row(fix_tasks=("TASK-A",)),
                _work_row("TASK-A"),
                _review_row(),
            ]
        )

        plan = ModeCCyclePlanner().plan_next_stage(
            _build(), history, has_commits=True, review_cycle_cap=3
        )

        assert plan.next_stage is StageClass.PULL_REQUEST_REVIEW
        assert "follow-up /task-review clean" in plan.rationale


class TestTheCapArithmeticIsTheGuardsOwn:
    """The planner may never choose a dispatch the budget guard refuses."""

    def test_three_cycles_fit_initial_follow_up_and_gate_driven(self) -> None:
        """The go-live setting: ``max_review_cycles: 3``."""
        history = _one_cycle_then_a_red_gate()

        assert a_review_cycle_remains(history, 3) is True

    def test_two_cycles_leave_nothing_for_the_gate_driven_one(self) -> None:
        assert a_review_cycle_remains(_one_cycle_then_a_red_gate(), 2) is False

    def test_the_count_is_the_one_the_guard_makes(self) -> None:
        from forge.pipeline.budget_guard import count_review_cycles

        history = _one_cycle_then_a_red_gate()
        counted = count_review_cycles(
            history, is_review=lambda e: e.stage_class == StageClass.TASK_REVIEW
        )

        assert counted == 2
        assert a_review_cycle_remains(history, counted) is False
        assert a_review_cycle_remains(history, counted + 1) is True

    def test_no_cap_means_a_cycle_always_remains(self) -> None:
        assert a_review_cycle_remains(_one_cycle_then_a_red_gate(), None) is True


# ---------------------------------------------------------------------------
# The terminal handler
# ---------------------------------------------------------------------------


class TestTheTerminalHandlerSeesTheRedRow:
    def test_a_red_checkpoint_is_failed_not_another_checkpoint(self) -> None:
        async def never(_build: Any) -> Any:  # pragma: no cover - must not run
            raise AssertionError("the commit probe must not be asked")

        decision = asyncio.run(
            evaluate_terminal(
                _build(), _one_cycle_then_a_red_gate(), commit_probe=never
            )
        )

        assert decision.outcome is HandlerTerminal.FAILED
        assert decision.rationale == RATIONALE_FAILED_MERGE_READY_RED
        assert decision.has_commits is False

    def test_the_reason_is_the_rows_own_words(self) -> None:
        decision = asyncio.run(
            evaluate_terminal(_build(), _one_cycle_then_a_red_gate())
        )

        assert "red" in (decision.failure_reason or "")

    def test_a_green_checkpoint_leaves_the_handler_exactly_as_it_was(self) -> None:
        from forge.pipeline.terminal_handlers.mode_c import CommitProbeResult

        history = project_mode_c_history(
            [
                _review_row(fix_tasks=("TASK-A",)),
                _work_row("TASK-A"),
                _review_row(),
                _checkpoint_row(green=True),
            ]
        )

        async def probe(_build: Any) -> CommitProbeResult:
            return CommitProbeResult(count=3)

        decision = asyncio.run(
            evaluate_terminal(_build(), history, commit_probe=probe)
        )

        assert decision.outcome is HandlerTerminal.PR_REVIEW

    def test_a_journey_with_no_checkpoint_row_classifies_as_before(self) -> None:
        from forge.pipeline.terminal_handlers.mode_c import CommitProbeResult

        history = project_mode_c_history(
            [
                _review_row(fix_tasks=("TASK-A",)),
                _work_row("TASK-A"),
                _review_row(),
            ]
        )

        async def probe(_build: Any) -> CommitProbeResult:
            return CommitProbeResult(count=1)

        decision = asyncio.run(
            evaluate_terminal(_build(), history, commit_probe=probe)
        )

        assert decision.outcome is HandlerTerminal.PR_REVIEW


# ---------------------------------------------------------------------------
# The gate-driven review's document
# ---------------------------------------------------------------------------


def _red_rows() -> list[_Row]:
    return [
        _review_row(fix_tasks=("TASK-39F6-001",)),
        _work_row("TASK-39F6-001"),
        _review_row(),
        _checkpoint_row(green=False),
    ]


class TestTheGateEvidenceDocument:
    def test_it_is_read_off_the_red_row(self) -> None:
        failures = read_gate_failures(_red_rows())

        assert failures is not None
        assert failures.command == "uv run pytest -q"
        assert failures.exit_code == 1
        assert failures.failing_tests == FAILING

    def test_a_green_checkpoint_has_nothing_to_send(self) -> None:
        rows = _red_rows()[:-1] + [_checkpoint_row(green=True)]

        assert read_gate_failures(rows) is None

    def test_work_since_the_checks_means_the_verdict_is_stale(self) -> None:
        rows = _red_rows() + [_review_row(fix_tasks=("TASK-39F6-009",))]

        assert read_gate_failures(rows) is None

    def test_a_journey_the_checks_never_saw_has_no_document(self) -> None:
        assert read_gate_failures(_red_rows()[:3]) is None

    def test_the_document_says_what_ran_and_what_failed(self) -> None:
        text = render_gate_failures(read_gate_failures(_red_rows()))

        assert "uv run pytest -q" in text
        assert "Exit code: 1" in text
        for name in FAILING:
            assert name in text

    def test_the_document_carries_the_instruction_word_for_word(self) -> None:
        """The sentence that stops a review seat 'fixing' the specification."""
        text = render_gate_failures(read_gate_failures(_red_rows()))

        assert GATE_FAILURES_INSTRUCTION in text
        assert "the change is what must be fixed, not the test" in text

    def test_the_document_carries_what_the_run_printed(self) -> None:
        text = render_gate_failures(read_gate_failures(_red_rows()))

        assert "AssertionError" in text

    def test_the_file_form_lands_beside_the_legs_receipts(self, tmp_path: Path) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        entry = build_gate_evidence_context(
            rows=_red_rows(), worktree_path=worktree, task_id=TASK_ID
        )

        assert entry is not None
        assert entry["flag"] == "--context"
        assert entry["kind"] == "path"
        written = Path(entry["value"])
        assert written == (
            worktree / ".guardkit" / "autobuild" / TASK_ID / GATE_FAILURES_DOCUMENT_NAME
        )
        assert GATE_FAILURES_INSTRUCTION in written.read_text(encoding="utf-8")

    def test_the_sandbox_form_sends_the_same_words_with_the_dispatch(
        self, tmp_path: Path
    ) -> None:
        """A repository with a sandbox keeps its tree inside; forge cannot write it."""
        inside = tmp_path / "inside-the-sandbox"

        entry = build_gate_evidence_context(
            rows=_red_rows(), worktree_path=inside, task_id=TASK_ID
        )

        assert entry is not None
        assert entry["flag"] == "--context"
        assert entry["kind"] == "text"
        assert GATE_FAILURES_INSTRUCTION in entry["value"]
        assert FAILING[0] in entry["value"]
        assert not inside.exists()

    def test_a_green_journey_is_handed_nothing(self, tmp_path: Path) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        entry = build_gate_evidence_context(
            rows=_red_rows()[:-1] + [_checkpoint_row(green=True)],
            worktree_path=worktree,
            task_id=TASK_ID,
        )

        assert entry is None
        assert not (worktree / ".guardkit").exists()

    def test_unreadable_rows_are_a_review_dispatched_as_before(self) -> None:
        class _Explodes:
            def __iter__(self) -> Any:
                raise RuntimeError("the ledger is unreadable")

        assert (
            build_gate_evidence_context(
                rows=_Explodes(), worktree_path=None, task_id=TASK_ID
            )
            is None
        )


class TestBothDocumentsRideTogether:
    """K2's verification document is not displaced by this one.

    They are added at two different layers — the gate's document by the
    supervisor's context builder, K2's by the conductor's dispatch seam — and
    both append to the same list of context entries. So a gate-driven review
    that also follows approved work carries both, and the leg reads both.
    """

    @pytest.mark.asyncio
    async def test_a_gate_driven_review_carries_both(self, tmp_path: Path) -> None:
        from tests.forge.pipeline.test_review_verifies_prior_findings import (
            BUILD_ID as VERIFY_BUILD_ID,
            _journey_with_one_cycle,
        )
        from forge.pipeline.dispatchers.conductor_subprocess import (
            make_conductor_subprocess_dispatcher,
        )
        from forge.pipeline.fix_task_context_builder import VERIFY_DOCUMENT_NAME

        receipts = tmp_path / "receipts"
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        _journey_with_one_cycle(receipts)

        @dataclass
        class _Row:
            build_id: str = VERIFY_BUILD_ID
            task_id: str | None = TASK_ID
            correlation_id: str = "corr-red"
            worktree_path: str | None = None
            feature_yaml_path: str | None = None
            feature_id: str = "FEAT-39F6"
            branch: str = "fix/FEAT-39F6"

        calls: list[dict[str, Any]] = []

        async def dispatch(stage: Any, build_id: str, **kwargs: Any) -> str:
            calls.append({"stage": stage, "build_id": build_id, **kwargs})
            return "dispatched"

        adapter = make_conductor_subprocess_dispatcher(
            build_row_reader=lambda _bid: _Row(worktree_path=str(worktree)),
            read_allowlist=[tmp_path],
            worktree_allowlist=object(),
            forward_context_builder=object(),
            stage_log_writer=object(),
            subprocess_runner=object(),
            dispatch=dispatch,
            correlation_id_minter=lambda **_kw: "corr-fixed",
            receipts_root=receipts,
        )

        # What the supervisor's builder hands the dispatcher: the gate's
        # document, written into the worktree exactly as it is in production.
        gate_entry = build_gate_evidence_context(
            rows=_red_rows(), worktree_path=worktree, task_id=TASK_ID
        )
        await adapter(
            stage=StageClass.TASK_REVIEW,
            build_id=VERIFY_BUILD_ID,
            forward_context={
                "context_entries": [gate_entry],
                "failure_pack": None,
            },
        )

        values = [
            str(e["value"])
            for e in calls[0]["forward_context"]["context_entries"]
        ]
        assert [v for v in values if v.endswith(GATE_FAILURES_DOCUMENT_NAME)], values
        assert [v for v in values if v.endswith(VERIFY_DOCUMENT_NAME)], values


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])

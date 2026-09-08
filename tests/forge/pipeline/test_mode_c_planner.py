"""Tests for ``forge.pipeline.mode_c_planner`` (TASK-MBC8-004).

Covers the 14 Mode C Group A/B/C/D planner-relevant scenarios from
``features/mode-b-feature-and-mode-c-review-fix/``:

Group A — capstone happy paths (3 scenarios):
  * Cycle dispatches one ``/task-work`` per identified fix task.
  * Each ``/task-work`` plan carries a back-reference to its review entry.
  * Build that produces commits ends with pull-request review.

Group B — boundary conditions (6 scenarios):
  * Clean initial ``/task-review`` terminates with CLEAN_REVIEW.
  * Scenario Outline (1, 3, 5) — exactly one ``/task-work`` per fix task.
  * Stage-ordering invariant — ``/task-work`` waits on ``/task-review``.
  * Stage-ordering invariant — pull-request waits on every ``/task-work``.

Group C — negative cases (3 scenarios):
  * Hard-stop on ``/task-review`` → terminal FAILED.
  * Failed ``/task-work`` does not auto-cancel sibling fix tasks.
  * Rejected ``/task-review`` (reject-before-PR) → terminal FAILED.

Group D — edge cases (2 scenarios):
  * Follow-up ``/task-review`` with no further fix tasks and no commits
    terminates with CLEAN_REVIEW.
  * Follow-up ``/task-review`` with no further fix tasks but commits
    present advances to PULL_REQUEST_REVIEW.

The planner is stateless — every call inspects ``history``. Tests therefore
do not use fixtures for hidden state; each test constructs the
``Sequence[StageEntry]`` it needs from scratch.
"""

from __future__ import annotations

import pytest

from forge.lifecycle.modes import BuildMode
from forge.lifecycle.persistence import Build
from forge.lifecycle.state_machine import BuildState
from forge.pipeline import mode_c_planner
from forge.pipeline.mode_c_planner import (
    ModeCCyclePlanner,
    ModeCPlan,
    ModeCTerminal,
    StageEntry,
    plan_next_stage,
)
from forge.pipeline.stage_taxonomy import StageClass

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build() -> Build:
    """Return a Mode C ``Build`` value object with status RUNNING."""
    return Build(
        build_id="build-FEAT-X-20260101000000",
        status=BuildState.RUNNING,
        mode=BuildMode.MODE_C,
    )


def _review_entry(
    *,
    fix_tasks: tuple[str, ...] = (),
    status: str = "approved",
    hard_stop: bool = False,
) -> StageEntry:
    """Construct a ``StageEntry`` representing one ``/task-review`` outcome."""
    return StageEntry(
        stage_class=StageClass.TASK_REVIEW,
        status=status,
        fix_tasks=fix_tasks,
        fix_task_id=None,
        hard_stop=hard_stop,
    )


def _work_entry(*, fix_task_id: str, status: str = "approved") -> StageEntry:
    """Construct a ``StageEntry`` representing one ``/task-work`` outcome."""
    return StageEntry(
        stage_class=StageClass.TASK_WORK,
        status=status,
        fix_tasks=(),
        fix_task_id=fix_task_id,
        hard_stop=False,
    )


# ---------------------------------------------------------------------------
# AC-001 — module structure
# ---------------------------------------------------------------------------


class TestModuleStructure:
    """AC-001 — ``forge.pipeline.mode_c_planner`` exposes the required surface."""

    def test_module_path_is_forge_pipeline_mode_c_planner(self) -> None:
        assert mode_c_planner.__name__ == "forge.pipeline.mode_c_planner"

    def test_module_exports_required_symbols(self) -> None:
        required = {
            "ModeCCyclePlanner",
            "ModeCPlan",
            "ModeCTerminal",
            "FixTaskRef",
            "StageEntry",
            "plan_next_stage",
        }
        assert required.issubset(set(mode_c_planner.__all__))

    def test_planner_signature_returns_mode_c_plan(self) -> None:
        planner = ModeCCyclePlanner()
        plan = planner.plan_next_stage(_build(), [])
        assert isinstance(plan, ModeCPlan)
        assert hasattr(plan, "permitted_stages")
        assert hasattr(plan, "next_stage")
        assert hasattr(plan, "next_fix_task")
        assert hasattr(plan, "terminal")

    def test_module_function_delegates_to_class(self) -> None:
        """The module-level ``plan_next_stage`` must produce the same plan
        as the class method for the same inputs."""
        history: list[StageEntry] = []
        plan_a = ModeCCyclePlanner().plan_next_stage(_build(), history)
        plan_b = plan_next_stage(_build(), history)
        assert plan_a == plan_b

    def test_permitted_stages_is_mode_c_chain_only(self) -> None:
        """``permitted_stages`` mirrors the Mode C chain — TASK_REVIEW,
        TASK_WORK, PULL_REQUEST_REVIEW. Nothing else is dispatchable."""
        plan = ModeCCyclePlanner().plan_next_stage(_build(), [])
        assert plan.permitted_stages == frozenset(
            {
                StageClass.TASK_REVIEW,
                StageClass.TASK_WORK,
                StageClass.PULL_REQUEST_REVIEW,
            }
        )


# ---------------------------------------------------------------------------
# Group A — capstone happy paths
# ---------------------------------------------------------------------------


class TestGroupAHappyPath:
    """Group A — Mode C capstone happy paths."""

    def test_empty_history_dispatches_task_review_first(self) -> None:
        """Group A precursor — the first decision in any Mode C build is
        to dispatch ``/task-review``."""
        plan = ModeCCyclePlanner().plan_next_stage(_build(), [])
        assert plan.next_stage == StageClass.TASK_REVIEW
        assert plan.next_fix_task is None
        assert plan.terminal is None

    def test_review_with_n_fix_tasks_dispatches_one_task_work_each(self) -> None:
        """Group A — initial review with N fix tasks fans out to one
        ``/task-work`` per fix task in turn."""
        history = [_review_entry(fix_tasks=("FIX-1", "FIX-2", "FIX-3"))]
        plan = ModeCCyclePlanner().plan_next_stage(_build(), history)
        assert plan.next_stage == StageClass.TASK_WORK
        assert plan.next_fix_task is not None
        assert plan.next_fix_task.fix_task_id == "FIX-1"

    def test_task_work_plan_back_references_originating_review(self) -> None:
        """Group A — every ``next_fix_task`` carries a back-reference to
        the originating ``/task-review`` entry (audit anchor for Group L
        lineage scenarios — AC-008)."""
        history = [_review_entry(fix_tasks=("FIX-1", "FIX-2"))]
        plan = ModeCCyclePlanner().plan_next_stage(_build(), history)
        assert plan.next_fix_task is not None
        # Back-reference is the index into ``history`` of the review that
        # emitted this fix task — index 0 here.
        assert plan.next_fix_task.review_history_index == 0

    def test_build_with_commits_after_clean_followup_advances_to_pr_review(
        self,
    ) -> None:
        """Group A — Mode C build that produces commits ends with PR review.

        After the initial review's fix tasks are all approved and a follow-up
        ``/task-review`` returns no further fix tasks, the planner advances
        to ``PULL_REQUEST_REVIEW`` when ``has_commits=True``.
        """
        history = [
            _review_entry(fix_tasks=("FIX-1",)),
            _work_entry(fix_task_id="FIX-1"),
            _review_entry(fix_tasks=()),
        ]
        plan = ModeCCyclePlanner().plan_next_stage(_build(), history, has_commits=True)
        assert plan.next_stage == StageClass.PULL_REQUEST_REVIEW
        assert plan.terminal is None


# ---------------------------------------------------------------------------
# Group B — boundary conditions
# ---------------------------------------------------------------------------


class TestGroupBBoundary:
    """Group B — Mode C boundary conditions."""

    def test_clean_initial_review_terminates_with_clean_review(self) -> None:
        """Group B "task-review returns empty set" — empty initial review
        terminates the build with CLEAN_REVIEW and dispatches no work."""
        history = [_review_entry(fix_tasks=())]
        plan = ModeCCyclePlanner().plan_next_stage(_build(), history)
        assert plan.next_stage is None
        assert plan.terminal == ModeCTerminal.CLEAN_REVIEW
        assert plan.next_fix_task is None

    @pytest.mark.parametrize("count", [1, 3, 5])
    def test_dispatches_one_task_work_per_fix_task(self, count: int) -> None:
        """Group B Scenario Outline — ``count`` fix tasks → ``count``
        sequential ``/task-work`` plans, one per fix task in order."""
        fix_tasks = tuple(f"FIX-{i}" for i in range(count))

        # Step through all N fix tasks, accumulating approved work entries.
        history: list[StageEntry] = [_review_entry(fix_tasks=fix_tasks)]
        dispatched: list[str] = []
        planner = ModeCCyclePlanner()
        for _ in range(count):
            plan = planner.plan_next_stage(_build(), history)
            assert plan.next_stage == StageClass.TASK_WORK
            assert plan.next_fix_task is not None
            dispatched.append(plan.next_fix_task.fix_task_id)
            history.append(_work_entry(fix_task_id=plan.next_fix_task.fix_task_id))

        # Each fix task was dispatched exactly once, in identification order.
        assert dispatched == list(fix_tasks)

        # After the last fix task, planner schedules the follow-up review.
        plan = planner.plan_next_stage(_build(), history)
        assert plan.next_stage == StageClass.TASK_REVIEW
        assert plan.next_fix_task is None

    def test_does_not_dispatch_task_work_before_review_approved(self) -> None:
        """Group B prerequisite row — ``/task-work`` does not dispatch until
        the review is approved.

        Encoded here as a review entry with status="pending"; the planner
        must not fan out work until the review reaches the approved state.
        """
        history = [
            StageEntry(
                stage_class=StageClass.TASK_REVIEW,
                status="pending",
                fix_tasks=("FIX-1",),
            )
        ]
        plan = ModeCCyclePlanner().plan_next_stage(_build(), history)
        assert plan.next_stage is None
        assert plan.next_fix_task is None

    def test_does_not_dispatch_pr_until_all_fix_tasks_complete(self) -> None:
        """Group B prerequisite row — pull-request review does not dispatch
        while a fix task remains undispatched."""
        history = [
            _review_entry(fix_tasks=("FIX-1", "FIX-2")),
            _work_entry(fix_task_id="FIX-1"),
        ]
        plan = ModeCCyclePlanner().plan_next_stage(_build(), history, has_commits=True)
        # Still has FIX-2 to dispatch — must NOT skip ahead to PR review.
        assert plan.next_stage == StageClass.TASK_WORK
        assert plan.next_fix_task is not None
        assert plan.next_fix_task.fix_task_id == "FIX-2"


# ---------------------------------------------------------------------------
# Group C — negative cases
# ---------------------------------------------------------------------------


class TestGroupCNegative:
    """Group C — Mode C negative cases."""

    def test_hard_stop_on_task_review_terminates_with_failed(self) -> None:
        """Group C — a hard-stop on ``/task-review`` returns
        ``next_stage=None`` and ``terminal=FAILED``; no ``/task-work``
        is dispatched (AC-007)."""
        history = [
            _review_entry(
                fix_tasks=("FIX-1", "FIX-2"),  # reviewer may have emitted
                status="failed",
                hard_stop=True,
            )
        ]
        plan = ModeCCyclePlanner().plan_next_stage(_build(), history)
        assert plan.next_stage is None
        assert plan.terminal == ModeCTerminal.FAILED
        assert plan.next_fix_task is None

    def test_rejected_task_review_terminates_with_failed(self) -> None:
        """Group C — a reject decision at the ``/task-review`` checkpoint
        before pull-request review terminates the build."""
        history = [_review_entry(fix_tasks=("FIX-1",), status="rejected")]
        plan = ModeCCyclePlanner().plan_next_stage(_build(), history)
        assert plan.next_stage is None
        assert plan.terminal == ModeCTerminal.FAILED

    def test_failed_task_work_does_not_auto_cancel_siblings(self) -> None:
        """Group C — failed ``/task-work`` is recorded against its fix task
        but does NOT auto-cancel sibling fix tasks (ASSUM-008, AC-006).

        The planner returns the next fix task in line as ``next_fix_task``
        even when a prior fix task failed — continuation is gate-driven,
        not planner-driven.
        """
        history = [
            _review_entry(fix_tasks=("FIX-1", "FIX-2", "FIX-3")),
            _work_entry(fix_task_id="FIX-1", status="failed"),
        ]
        plan = ModeCCyclePlanner().plan_next_stage(_build(), history)
        assert plan.next_stage == StageClass.TASK_WORK
        assert plan.next_fix_task is not None
        assert plan.next_fix_task.fix_task_id == "FIX-2"
        assert plan.terminal is None


# ---------------------------------------------------------------------------
# Group D — edge cases
# ---------------------------------------------------------------------------


class TestGroupDEdge:
    """Group D — Mode C edge cases."""

    def test_follow_up_clean_review_no_commits_terminates_clean_review(
        self,
    ) -> None:
        """Group D — follow-up ``/task-review`` with no further fix tasks
        and no commits terminates with CLEAN_REVIEW (AC-005)."""
        history = [
            _review_entry(fix_tasks=("FIX-1",)),
            _work_entry(fix_task_id="FIX-1"),
            _review_entry(fix_tasks=()),
        ]
        plan = ModeCCyclePlanner().plan_next_stage(_build(), history, has_commits=False)
        assert plan.next_stage is None
        assert plan.terminal == ModeCTerminal.CLEAN_REVIEW

    def test_follow_up_clean_review_with_commits_advances_to_pr_review(
        self,
    ) -> None:
        """Group D — follow-up ``/task-review`` with no further fix tasks
        but with commits advances to ``PULL_REQUEST_REVIEW`` (AC-005)."""
        history = [
            _review_entry(fix_tasks=("FIX-1",)),
            _work_entry(fix_task_id="FIX-1"),
            _review_entry(fix_tasks=()),
        ]
        plan = ModeCCyclePlanner().plan_next_stage(_build(), history, has_commits=True)
        assert plan.next_stage == StageClass.PULL_REQUEST_REVIEW
        assert plan.terminal is None


# ---------------------------------------------------------------------------
# The last cycle's exit — 2026-09-08, journey one's ninth seam
# ---------------------------------------------------------------------------


class TestTheLastCycleGoesToTheMergeReadyChecks:
    """At the review-cycle cap, a finished cycle goes to the checkpoint.

    The observed defect (journey one, 2026-09-08): the second cycle's three
    work legs were all approved, the planner asked for a third review, the
    budget guard refused it at the cap of two, and the journey stopped one
    step short of its merge card with every fix approved by the coach and
    the oracle.

    ``review_cycle_cap`` is the profile's cap, passed in by the supervisor
    and counted the same way the budget guard counts it. Absent (every
    caller that passes nothing, and the attended profile) nothing here
    changes a single decision.
    """

    @staticmethod
    def _two_finished_cycles() -> list[StageEntry]:
        """Two review cycles, every fix task approved. Journey one's shape."""
        return [
            _review_entry(fix_tasks=("FIX-1", "FIX-2")),
            _work_entry(fix_task_id="FIX-1"),
            _work_entry(fix_task_id="FIX-2"),
            _review_entry(fix_tasks=("FIX-3", "FIX-4")),
            _work_entry(fix_task_id="FIX-3"),
            _work_entry(fix_task_id="FIX-4"),
        ]

    def test_at_the_cap_the_next_stage_is_the_merge_ready_checkpoint(
        self,
    ) -> None:
        plan = ModeCCyclePlanner().plan_next_stage(
            _build(), self._two_finished_cycles(), review_cycle_cap=2
        )

        assert plan.next_stage == StageClass.PULL_REQUEST_REVIEW
        assert plan.terminal is None
        assert plan.wait is None
        assert plan.rationale == (
            "every fix task is done and the review cycles are used up — "
            "going to the merge-ready checks"
        )

    def test_below_the_cap_the_follow_up_review_is_scheduled_as_today(
        self,
    ) -> None:
        """One cycle done under a cap of two — the second review is owed."""
        history = self._two_finished_cycles()[:3]

        plan = ModeCCyclePlanner().plan_next_stage(
            _build(), history, review_cycle_cap=2
        )

        assert plan.next_stage == StageClass.TASK_REVIEW
        assert plan.rationale == "all fix tasks completed — scheduling follow-up review"

    def test_with_no_cap_at_all_nothing_changes(self) -> None:
        """The attended profile (ASSUM-010) — reviewer-driven, no numbers."""
        plan = ModeCCyclePlanner().plan_next_stage(
            _build(), self._two_finished_cycles()
        )

        assert plan.next_stage == StageClass.TASK_REVIEW
        assert plan.rationale == "all fix tasks completed — scheduling follow-up review"

    def test_at_the_cap_with_every_leg_failed_the_failure_path_still_wins(
        self,
    ) -> None:
        """A broken tool never reaches the merge-ready checks.

        The 100%-failed cycle is a tooling fault (ASSUM-008 as narrowed
        2026-08-02) and its terminal is decided before the cap is ever
        consulted.
        """
        history = [
            _review_entry(fix_tasks=("FIX-1", "FIX-2")),
            _work_entry(fix_task_id="FIX-1"),
            _work_entry(fix_task_id="FIX-2"),
            _review_entry(fix_tasks=("FIX-3", "FIX-4")),
            _work_entry(fix_task_id="FIX-3", status="failed"),
            _work_entry(fix_task_id="FIX-4", status="failed"),
        ]

        plan = ModeCCyclePlanner().plan_next_stage(
            _build(), history, review_cycle_cap=2
        )

        assert plan.next_stage is None
        assert plan.terminal == ModeCTerminal.FAILED
        assert plan.total_work_failure == ("FIX-3", "FIX-4")

    def test_at_the_cap_with_nothing_approved_the_review_is_still_asked_for(
        self,
    ) -> None:
        """Rejections and cancellations leave nothing to take to the checks.

        The cycle is not a total tooling failure — a gate and a human each
        gave a verdict on work that ran — so today's behaviour stands: the
        follow-up review is asked for, the budget guard refuses it, and the
        journey is closed out with the reason.
        """
        history = [
            _review_entry(fix_tasks=("FIX-1",)),
            _work_entry(fix_task_id="FIX-1"),
            _review_entry(fix_tasks=("FIX-2", "FIX-3")),
            _work_entry(fix_task_id="FIX-2", status="rejected"),
            _work_entry(fix_task_id="FIX-3", status="cancelled"),
        ]

        plan = ModeCCyclePlanner().plan_next_stage(
            _build(), history, review_cycle_cap=2
        )

        assert plan.next_stage == StageClass.TASK_REVIEW
        assert plan.rationale == "all fix tasks completed — scheduling follow-up review"

    def test_a_cycle_still_in_flight_at_the_cap_waits(self) -> None:
        """The cap never overtakes a running leg."""
        history = [
            _review_entry(fix_tasks=("FIX-1",)),
            _work_entry(fix_task_id="FIX-1"),
            _review_entry(fix_tasks=("FIX-2", "FIX-3")),
            _work_entry(fix_task_id="FIX-2"),
            _work_entry(fix_task_id="FIX-3", status="running"),
        ]

        plan = ModeCCyclePlanner().plan_next_stage(
            _build(), history, review_cycle_cap=2
        )

        assert plan.is_waiting
        assert plan.next_stage is None

    def test_the_module_level_wrapper_carries_the_cap_through(self) -> None:
        plan = plan_next_stage(
            _build(), self._two_finished_cycles(), review_cycle_cap=2
        )

        assert plan.next_stage == StageClass.PULL_REQUEST_REVIEW

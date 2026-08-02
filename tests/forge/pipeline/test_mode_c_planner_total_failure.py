"""ASSUM-008, NARROWED — total work failure is not a fix outcome.

Rich's ruling, 2026-08-02. ASSUM-008 says a failed ``/task-work`` is
isolated to its own fix task and does not cancel its siblings. That is
right per fix task and wrong at 100%: because a FAILED work leg closes its
fix-task slot exactly like an approved one, a cycle in which every leg
failed reads as "all fix tasks completed" and the planner schedules a
follow-up review. On the runaway ledger that happened 42 times with
158/158 legs failed — the machine spending cycles rediscovering the same
findings because a broken tool and a finished job are the same shape.

The narrowing, stated once:

* every fix task of the cycle terminal AND every terminal work row
  strictly ``"failed"`` → terminal FAILED, the class and the ids named;
* ANY ``approved`` / ``rejected`` / ``cancelled`` in the mix → today's
  behaviour, byte-identical. The isolate-ONE-failure rule is untouched.

"The cycle's rows" means the ``/task-work`` rows recorded after the latest
review that carry an id **that review emitted** — the same window the
fix-task walk uses when it decides the fan-out is exhausted. Anything else
is a stale or foreign row and is not evidence either way.

The terminal also rides the plan as typed evidence (``total_work_failure``),
because the supervisor must recognise this ruling without re-deriving it:
the terminal handler classifies a cycle from the work rows that ran BEFORE
the latest review and has no branch for this shape.

The strictness is the load-bearing half. ``rejected`` is a gate's verdict
and ``cancelled`` is a human's — both are verdicts on work that RAN, and
the rule claims a TOOLING FAULT. So the mixed cases below are not
politeness; each is a way the rule could over-fire and end a live journey.
"""

from __future__ import annotations

import pytest

from forge.lifecycle.modes import BuildMode
from forge.lifecycle.persistence import Build
from forge.lifecycle.state_machine import BuildState
from forge.pipeline.mode_c_planner import (
    ModeCCyclePlanner,
    ModeCPlan,
    ModeCTerminal,
    StageEntry,
)
from forge.pipeline.mode_chains_data import MODE_C_CHAIN
from forge.pipeline.stage_taxonomy import StageClass

# The exact rationale main produces on the follow-up-review branch. Spelled
# out here rather than imported so a silent edit to the planner's wording
# cannot quietly re-baseline the "byte-identical to main" tests.
FOLLOW_UP_RATIONALE = "all fix tasks completed — scheduling follow-up review"


def _build() -> Build:
    return Build(
        build_id="build-FEAT-RUNAWAY-20260802000000",
        status=BuildState.RUNNING,
        mode=BuildMode.MODE_C,
    )


def _review(*fix_tasks: str, status: str = "approved") -> StageEntry:
    return StageEntry(
        stage_class=StageClass.TASK_REVIEW,
        status=status,
        fix_tasks=tuple(fix_tasks),
    )


def _work(fix_task_id: str, status: str) -> StageEntry:
    return StageEntry(
        stage_class=StageClass.TASK_WORK,
        status=status,
        fix_task_id=fix_task_id,
    )


def _plan(history, *, has_commits: bool = False) -> ModeCPlan:
    return ModeCCyclePlanner().plan_next_stage(
        _build(), history, has_commits=has_commits
    )


# ---------------------------------------------------------------------------
# (a) The runaway shape — N >= 1 all-FAILED work legs end the journey
# ---------------------------------------------------------------------------


class TestTheRunawayShape:
    """A cycle whose every work leg failed ends TERMINAL, ids named."""

    @pytest.mark.parametrize("n", [1, 2, 3, 5, 12])
    def test_a_cycle_of_all_failed_legs_terminates_failed(self, n: int) -> None:
        ids = tuple(f"TASK-FW-{i:03d}-slug" for i in range(1, n + 1))
        history = [_review(*ids), *[_work(i, "failed") for i in ids]]

        plan = _plan(history)

        assert plan.next_stage is None, (
            "a 100%-failed cycle must not schedule anything — the follow-up "
            f"review is exactly the runaway; got {plan.next_stage}"
        )
        assert plan.terminal is ModeCTerminal.FAILED
        assert plan.wait is None
        assert plan.next_fix_task is None
        assert plan.total_work_failure == ids

    def test_the_ruling_rides_the_plan_as_typed_evidence_not_only_prose(
        self,
    ) -> None:
        """``total_work_failure`` is what the supervisor branches on.

        The supervisor must not re-derive this terminal, and it must not
        string-match the rationale to recognise it: the terminal handler
        reads the work rows that ran BEFORE the latest review and has no
        branch for this shape, so asked, it answers with a defensive
        "you called me mid-cycle" accusation that is false on both clauses.
        The field is the discriminator that keeps it from being asked.
        """
        history = [
            _review("FIX-1", "FIX-2"),
            _work("FIX-1", "failed"),
            _work("FIX-2", "failed"),
        ]

        plan = _plan(history)

        assert plan.total_work_failure == ("FIX-1", "FIX-2")

    def test_every_other_plan_carries_no_ruling(self) -> None:
        """The field is set on this terminal and nowhere else.

        A supervisor that short-circuits on a stale or over-broad flag
        would skip the commit probe on a journey that needed it.
        """
        for history in (
            [],
            [_review("FIX-1")],
            [_review("FIX-1"), _work("FIX-1", "approved")],
            [
                _review("FIX-1", "FIX-2"),
                _work("FIX-1", "failed"),
                _work("FIX-2", "running"),
            ],
            [_review("FIX-1", status="rejected")],
            [_review()],
        ):
            assert _plan(history).total_work_failure is None, history

    @pytest.mark.parametrize("n", [1, 2, 3, 5, 12])
    def test_the_rationale_names_the_class_and_every_fix_task_id(
        self, n: int
    ) -> None:
        """The stop has to say WHY, in the words the ruling chose.

        A terminal that says only "failed" sends a diagnoser to the fix
        tasks; this one has to say the TOOL broke.
        """
        ids = tuple(f"TASK-FW-{i:03d}-slug" for i in range(1, n + 1))
        history = [_review(*ids), *[_work(i, "failed") for i in ids]]

        rationale = _plan(history).rationale

        assert (
            "every work leg in this cycle failed — a tooling fault, not a "
            "fix outcome" in rationale
        ), rationale
        for fix_task_id in ids:
            assert fix_task_id in rationale, rationale

    def test_no_follow_up_task_review_is_planned(self) -> None:
        """The single assertion the ruling is about."""
        history = [
            _review("FIX-1", "FIX-2"),
            _work("FIX-1", "failed"),
            _work("FIX-2", "failed"),
        ]

        plan = _plan(history)

        assert plan.next_stage is not StageClass.TASK_REVIEW
        assert FOLLOW_UP_RATIONALE not in plan.rationale

    def test_commits_on_the_branch_do_not_rescue_a_100_percent_failed_cycle(
        self,
    ) -> None:
        """``has_commits`` is the clean-review branch's input, not this one's.

        A cycle whose every leg failed produced no fix; commits on the
        branch came from somewhere else and must not turn a tooling fault
        into a pull-request review.
        """
        history = [_review("FIX-1"), _work("FIX-1", "failed")]

        plan = _plan(history, has_commits=True)

        assert plan.terminal is ModeCTerminal.FAILED
        assert plan.next_stage is None

    def test_the_ids_are_named_in_dispatch_order_without_duplicates(self) -> None:
        history = [
            _review("FIX-B", "FIX-A"),
            _work("FIX-B", "failed"),
            _work("FIX-A", "failed"),
            # A re-dispatch of the same fix task inside the cycle: named
            # once, not twice.
            _work("FIX-B", "failed"),
        ]

        rationale = _plan(history).rationale

        assert rationale.endswith("(fix tasks: FIX-B, FIX-A)"), rationale

    def test_only_the_current_cycle_is_judged(self) -> None:
        """An EARLIER cycle's failures are not this cycle's evidence.

        The walk starts after the latest review, so a journey whose first
        cycle failed outright and whose second cycle succeeded advances
        normally. Reading the whole history would terminate a recovering
        journey on the strength of its worst moment.
        """
        history = [
            _review("FIX-1"),
            _work("FIX-1", "failed"),
            _review("FIX-2"),
            _work("FIX-2", "approved"),
        ]

        plan = _plan(history)

        assert plan.next_stage is StageClass.TASK_REVIEW
        assert plan.terminal is None


# ---------------------------------------------------------------------------
# (b) + (c) The untouched behaviours — byte-identical decisions
# ---------------------------------------------------------------------------


class TestTodaysBehaviourIsUntouched:
    """Every mix that is not 100% failure decides exactly as main did."""

    def _expect_follow_up(self, history) -> None:
        assert _plan(history) == ModeCPlan(
            permitted_stages=frozenset(MODE_C_CHAIN),
            next_stage=StageClass.TASK_REVIEW,
            next_fix_task=None,
            terminal=None,
            wait=None,
            rationale=FOLLOW_UP_RATIONALE,
        )

    def test_one_of_three_failed_still_schedules_the_follow_up_review(
        self,
    ) -> None:
        """(b) — the whole ModeCPlan, field for field, as main returns it."""
        self._expect_follow_up(
            [
                _review("FIX-1", "FIX-2", "FIX-3"),
                _work("FIX-1", "failed"),
                _work("FIX-2", "approved"),
                _work("FIX-3", "approved"),
            ]
        )

    def test_two_of_three_failed_still_schedules_the_follow_up_review(
        self,
    ) -> None:
        """The rule is 100%, not "most". 2/3 is still a fix outcome."""
        self._expect_follow_up(
            [
                _review("FIX-1", "FIX-2", "FIX-3"),
                _work("FIX-1", "failed"),
                _work("FIX-2", "failed"),
                _work("FIX-3", "approved"),
            ]
        )

    def test_all_approved_schedules_the_follow_up_review(self) -> None:
        """(c) — the ordinary success path, unmoved."""
        self._expect_follow_up(
            [
                _review("FIX-1", "FIX-2"),
                _work("FIX-1", "approved"),
                _work("FIX-2", "approved"),
            ]
        )

    @pytest.mark.parametrize("other", ["rejected", "cancelled"])
    def test_a_rejected_or_cancelled_leg_in_the_mix_keeps_todays_behaviour(
        self, other: str
    ) -> None:
        """The strictness clause, mutation-shaped.

        Loosen ``status != "failed"`` to "status in the failed family" and
        these two go terminal — a gate's reject and an operator's cancel
        rewritten as "the tooling is broken".
        """
        self._expect_follow_up(
            [
                _review("FIX-1", "FIX-2"),
                _work("FIX-1", "failed"),
                _work("FIX-2", other),
            ]
        )

    @pytest.mark.parametrize("other", ["rejected", "cancelled"])
    def test_a_cycle_of_only_rejections_or_cancellations_is_not_a_tooling_fault(
        self, other: str
    ) -> None:
        self._expect_follow_up(
            [
                _review("FIX-1", "FIX-2"),
                _work("FIX-1", other),
                _work("FIX-2", other),
            ]
        )

    def test_the_isolate_one_failure_rule_is_untouched_mid_cycle(self) -> None:
        """ASSUM-008 proper: a failed leg still unblocks its siblings.

        The narrowing only speaks when the cycle's fan-out is EXHAUSTED.
        Mid-cycle, a failure is isolated exactly as before.
        """
        history = [
            _review("FIX-1", "FIX-2", "FIX-3"),
            _work("FIX-1", "failed"),
        ]

        plan = _plan(history)

        assert plan.next_stage is StageClass.TASK_WORK
        assert plan.next_fix_task is not None
        assert plan.next_fix_task.fix_task_id == "FIX-2"
        assert plan.terminal is None

    def test_a_failed_leg_with_one_still_in_flight_waits_rather_than_terminates(
        self,
    ) -> None:
        """The WAIT variant outranks the new terminal.

        Terminating here would end a journey with a ``/task-work`` still
        running — the §h.1 defect, re-introduced through the new door.
        """
        history = [
            _review("FIX-1", "FIX-2"),
            _work("FIX-1", "failed"),
            _work("FIX-2", "running"),
        ]

        plan = _plan(history)

        assert plan.is_waiting
        assert plan.terminal is None

    def test_a_clean_review_after_an_all_failed_cycle_still_reads_clean(
        self,
    ) -> None:
        """The clean-review branch runs BEFORE the walk and stays first.

        Once a follow-up review has already run and come back empty, the
        cycle being judged is that review's — which dispatched no work at
        all. The new rule must not reach back into the previous cycle and
        overwrite a clean terminal with a failure.
        """
        history = [
            _review("FIX-1"),
            _work("FIX-1", "failed"),
            _review(),
        ]

        plan = _plan(history)

        assert plan.terminal is ModeCTerminal.CLEAN_REVIEW

    def test_an_unattributable_failed_row_is_not_evidence(self) -> None:
        """A ``/task-work`` row with no ``fix_task_id`` is an upstream bug.

        The fix-task walk already skips it (it would otherwise read as
        "never dispatched"); the failure rule skips it too, so the two
        cannot disagree about which rows exist.
        """
        history = [
            _review("FIX-1"),
            StageEntry(
                stage_class=StageClass.TASK_WORK,
                status="failed",
                fix_task_id=None,
            ),
            _work("FIX-1", "approved"),
        ]

        self._expect_follow_up(history)


# ---------------------------------------------------------------------------
# The evidence window — THIS review's fix-task list, and nothing else
# ---------------------------------------------------------------------------


class TestTheEvidenceWindow:
    """Which ``/task-work`` rows count as this cycle's evidence.

    The rule reads the rows recorded after the latest review — but only
    those carrying an id that review actually emitted. Anything else is a
    stale or foreign row, and naming one in an operator-facing rationale is
    a diagnoser sent to a fix task that was never dispatched here.
    """

    def test_a_row_for_a_fix_task_this_review_never_listed_is_not_evidence(
        self,
    ) -> None:
        """The evidence window is THIS review's fix-task list, not "any row".

        A stale or foreign ``/task-work`` row carrying an id the review
        never emitted is not part of the cycle. Reading it named
        ``FIX-GHOST`` in the operator-facing rationale as one of this
        cycle's fix tasks — a diagnoser sent to a fix task that was never
        dispatched here. The fix-task walk already scopes itself this way;
        the failure rule now uses the same window so the two cannot
        disagree about which rows exist.
        """
        history = [
            _review("FIX-1"),
            _work("FIX-1", "failed"),
            _work("FIX-GHOST", "failed"),
        ]

        plan = _plan(history)

        assert plan.total_work_failure == ("FIX-1",)
        assert "FIX-GHOST" not in plan.rationale, plan.rationale

    def test_an_approved_row_outside_this_reviews_list_does_not_rescue_the_cycle(
        self,
    ) -> None:
        """The same window, the other way round.

        The filter has to cut both ways or it is a convenience, not a rule:
        an approved row for an id this review never listed is no more this
        cycle's evidence than a failed one, and must not turn a 100%-failed
        cycle back into a follow-up review.
        """
        history = [
            _review("FIX-1"),
            _work("FIX-1", "failed"),
            _work("FIX-GHOST", "approved"),
        ]

        plan = _plan(history)

        assert plan.terminal is ModeCTerminal.FAILED
        assert plan.total_work_failure == ("FIX-1",)

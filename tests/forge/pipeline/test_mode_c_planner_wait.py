"""The planner's WAIT variant — the in-flight-sentinel fix (Stage 1a).

Design pass ``supervisor-revival-design-pass-2026-07-31`` risk h.1, the
one named in-code defect:

    ``mode_c_planner.py``: when a fix task is in flight,
    ``_next_undispatched_fix_task`` returns ``None``, and the caller reads
    ``None`` as "all fix tasks completed — schedule follow-up review". The
    code comment admits the encoding is wrong-if-reached.

Two answers shared one encoding. "Nothing left to do" and "the next one is
still running" are different instructions, and the caller could only act on
one of them — so it acted on the wrong one for the other, scheduling a
follow-up review over a half-finished cycle *and* putting a second stage in
flight for the same build.

This module pins the cure and the disease:

* an in-flight fix task yields a WAIT (``ModeCWait.FIX_TASK_IN_FLIGHT``),
  never a dispatch and never a terminal;
* an exhausted fix-task list still yields the follow-up review;
* the old misread is pinned as an explicit regression test, by name, so a
  future refactor that re-collapses the two answers fails here;
* the planner stays a stateless pure function — same history in, same plan
  out, no matter how many times it is called or in what order.
"""

from __future__ import annotations

import pytest

from forge.lifecycle.modes import BuildMode
from forge.lifecycle.persistence import Build
from forge.lifecycle.state_machine import BuildState
from forge.pipeline.mode_c_planner import (
    FixTaskLookup,
    ModeCCyclePlanner,
    ModeCTerminal,
    ModeCWait,
    StageEntry,
    plan_next_stage,
)
from forge.pipeline.stage_taxonomy import StageClass


def _build() -> Build:
    return Build(
        build_id="build-FEAT-FIX007-20260731",
        status=BuildState.RUNNING,
        mode=BuildMode.MODE_C,
    )


def _review(
    *, status: str = "approved", fix_tasks: tuple[str, ...] = ()
) -> StageEntry:
    return StageEntry(
        stage_class=StageClass.TASK_REVIEW,
        status=status,
        fix_tasks=fix_tasks,
    )


def _work(fix_task_id: str, *, status: str) -> StageEntry:
    return StageEntry(
        stage_class=StageClass.TASK_WORK,
        status=status,
        fix_task_id=fix_task_id,
    )


@pytest.fixture
def planner() -> ModeCCyclePlanner:
    return ModeCCyclePlanner()


# ---------------------------------------------------------------------------
# The defect, pinned
# ---------------------------------------------------------------------------


class TestInFlightFixTaskYieldsWait:
    """risk h.1 — an in-flight ``/task-work`` must never advance the cycle."""

    @pytest.mark.parametrize("status", ["pending", "running", "in_progress"])
    def test_single_in_flight_fix_task_yields_wait(
        self, planner: ModeCCyclePlanner, status: str
    ) -> None:
        history = [
            _review(fix_tasks=("FIX-1",)),
            _work("FIX-1", status=status),
        ]
        plan = planner.plan_next_stage(_build(), history)

        assert plan.is_waiting
        assert plan.wait is ModeCWait.FIX_TASK_IN_FLIGHT
        assert plan.next_stage is None
        assert plan.terminal is None
        assert plan.next_fix_task is None
        assert "FIX-1" in plan.rationale

    def test_the_old_misread_is_a_regression_test(
        self, planner: ModeCCyclePlanner
    ) -> None:
        """REGRESSION (risk h.1): in flight must NOT schedule a follow-up.

        The pre-fix planner returned
        ``next_stage=TASK_REVIEW, rationale="all fix tasks completed …"``
        for exactly this history. Both assertions below failed before the
        fix; if a refactor re-collapses the lookup's two ``None`` answers,
        this is the test that catches it.
        """
        history = [
            _review(fix_tasks=("FIX-1",)),
            _work("FIX-1", status="running"),
        ]
        plan = planner.plan_next_stage(_build(), history)

        assert plan.next_stage is not StageClass.TASK_REVIEW
        assert "all fix tasks completed" not in plan.rationale

    def test_earlier_siblings_complete_but_next_in_flight_still_waits(
        self, planner: ModeCCyclePlanner
    ) -> None:
        """The walk stops at the first open slot — even mid-list."""
        history = [
            _review(fix_tasks=("FIX-1", "FIX-2", "FIX-3")),
            _work("FIX-1", status="approved"),
            _work("FIX-2", status="running"),
        ]
        plan = planner.plan_next_stage(_build(), history)

        assert plan.wait is ModeCWait.FIX_TASK_IN_FLIGHT
        assert "FIX-2" in plan.rationale
        # FIX-3 is untouched and undispatched — but it does NOT get to jump
        # the queue while FIX-2 is running (turn-serial per build).
        assert plan.next_fix_task is None

    def test_a_failed_sibling_does_not_turn_an_in_flight_task_into_a_wait_bypass(
        self, planner: ModeCCyclePlanner
    ) -> None:
        """ASSUM-008 isolation still holds around the wait.

        A *failed* slot is closed (its failure is isolated to its own fix
        task), so the walk moves past it — and then stops on the running
        one, as it must.
        """
        history = [
            _review(fix_tasks=("FIX-1", "FIX-2")),
            _work("FIX-1", status="failed"),
            _work("FIX-2", status="pending"),
        ]
        plan = planner.plan_next_stage(_build(), history)

        assert plan.wait is ModeCWait.FIX_TASK_IN_FLIGHT
        assert "FIX-2" in plan.rationale


# ---------------------------------------------------------------------------
# The variants either side of the wait
# ---------------------------------------------------------------------------


class TestTheOtherTwoAnswers:
    """Dispatch and follow-up review must be unaffected by the new variant."""

    def test_open_slot_still_dispatches_task_work(
        self, planner: ModeCCyclePlanner
    ) -> None:
        history = [_review(fix_tasks=("FIX-1", "FIX-2"))]
        plan = planner.plan_next_stage(_build(), history)

        assert plan.next_stage is StageClass.TASK_WORK
        assert plan.is_waiting is False
        assert plan.wait is None
        assert plan.next_fix_task is not None
        assert plan.next_fix_task.fix_task_id == "FIX-1"
        assert plan.next_fix_task.review_history_index == 0

    def test_all_fix_tasks_terminal_still_schedules_the_follow_up_review(
        self, planner: ModeCCyclePlanner
    ) -> None:
        history = [
            _review(fix_tasks=("FIX-1", "FIX-2")),
            _work("FIX-1", status="approved"),
            _work("FIX-2", status="failed"),
        ]
        plan = planner.plan_next_stage(_build(), history)

        assert plan.next_stage is StageClass.TASK_REVIEW
        assert plan.is_waiting is False
        assert plan.terminal is None
        assert "all fix tasks completed" in plan.rationale

    def test_unapproved_review_is_also_a_typed_wait(
        self, planner: ModeCCyclePlanner
    ) -> None:
        """The pre-existing wait gains the same discriminator.

        It was already encoded as "both fields None", which the driver
        loop could not tell from a terminal. Now it names itself.
        """
        plan = planner.plan_next_stage(_build(), [_review(status="pending")])

        assert plan.is_waiting
        assert plan.wait is ModeCWait.REVIEW_AWAITING_APPROVAL
        assert plan.next_stage is None
        assert plan.terminal is None

    @pytest.mark.parametrize(
        ("history", "expected_terminal"),
        [
            ([_review(status="rejected")], ModeCTerminal.FAILED),
            ([_review(fix_tasks=())], ModeCTerminal.CLEAN_REVIEW),
        ],
    )
    def test_terminals_are_not_waits(
        self,
        planner: ModeCCyclePlanner,
        history: list[StageEntry],
        expected_terminal: ModeCTerminal,
    ) -> None:
        """A terminal plan must never read as a wait, and vice versa.

        This is the distinction the driver loop branches on: a wait means
        "come back later", a terminal means "close the build out". Getting
        them the wrong way round either ends a live build or hangs a
        finished one.
        """
        plan = planner.plan_next_stage(_build(), history)

        assert plan.terminal is expected_terminal
        assert plan.is_waiting is False
        assert plan.wait is None

    def test_hard_stop_review_is_terminal_not_wait(
        self, planner: ModeCCyclePlanner
    ) -> None:
        history = [
            StageEntry(
                stage_class=StageClass.TASK_REVIEW,
                status="pending",
                hard_stop=True,
            )
        ]
        plan = planner.plan_next_stage(_build(), history)

        assert plan.terminal is ModeCTerminal.FAILED
        assert plan.is_waiting is False


# ---------------------------------------------------------------------------
# The lookup value object
# ---------------------------------------------------------------------------


class TestFixTaskLookup:
    """Three named outcomes cannot be misread the way ``str | None`` was."""

    def test_dispatch_variant(self) -> None:
        lookup = FixTaskLookup(fix_task_id="FIX-1")
        assert lookup.fix_task_id == "FIX-1"
        assert lookup.is_wait is False
        assert lookup.is_exhausted is False

    def test_wait_variant(self) -> None:
        lookup = FixTaskLookup(in_flight_id="FIX-1")
        assert lookup.fix_task_id is None
        assert lookup.is_wait is True
        assert lookup.is_exhausted is False

    def test_exhausted_variant(self) -> None:
        lookup = FixTaskLookup()
        assert lookup.is_wait is False
        assert lookup.is_exhausted is True

    def test_wait_and_exhausted_are_mutually_exclusive(self) -> None:
        """The bug in one line: the old encoding made these the same value."""
        wait = FixTaskLookup(in_flight_id="FIX-1")
        exhausted = FixTaskLookup()
        assert wait != exhausted
        assert wait.is_wait != exhausted.is_wait


# ---------------------------------------------------------------------------
# Statelessness
# ---------------------------------------------------------------------------


class TestPlannerRemainsStateless:
    """The planner is a pure function of (build, history, has_commits)."""

    def test_repeated_calls_on_an_in_flight_history_are_identical(
        self, planner: ModeCCyclePlanner
    ) -> None:
        history = [
            _review(fix_tasks=("FIX-1", "FIX-2")),
            _work("FIX-1", status="running"),
        ]
        plans = [planner.plan_next_stage(_build(), history) for _ in range(4)]
        assert all(p == plans[0] for p in plans)
        assert plans[0].wait is ModeCWait.FIX_TASK_IN_FLIGHT

    def test_module_level_wrapper_agrees_with_the_class(self) -> None:
        history = [
            _review(fix_tasks=("FIX-1",)),
            _work("FIX-1", status="running"),
        ]
        assert plan_next_stage(_build(), history) == ModeCCyclePlanner(
        ).plan_next_stage(_build(), history)

    def test_a_wait_does_not_depend_on_has_commits(
        self, planner: ModeCCyclePlanner
    ) -> None:
        """The commit probe is a terminal-routing input, not a wait input."""
        history = [
            _review(fix_tasks=("FIX-1",)),
            _work("FIX-1", status="running"),
        ]
        with_commits = planner.plan_next_stage(
            _build(), history, has_commits=True
        )
        without = planner.plan_next_stage(_build(), history, has_commits=False)
        assert with_commits == without
        assert with_commits.wait is ModeCWait.FIX_TASK_IN_FLIGHT

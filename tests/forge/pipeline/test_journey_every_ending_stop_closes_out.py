"""No stop that ends a journey leaves the build RUNNING.

Attempt eight, 2026-09-08. The driver's review-cycle rule stopped the
journey, wrote its failure pack, and returned — and the build row stayed
RUNNING with the pipeline consumer still holding that build's queued
message. The attempt had to be cancelled by hand and its message pulled off
the stream, exactly as two earlier journeys had been. The close-out that
does both jobs — FAIL the row with the reason, and give the queue its
message back — existed, and only three paths went through it: a terminal
turn, a published card, and a budget breach nobody could be asked about.

Every stop that ENDS a journey now goes through it:

* the review-cycle nothing-changed stop, when it does fire;
* the turn-level nothing-changed stop;
* the turn ceiling;
* an error — ``next_turn`` raising, and a turn outcome with no branch;
* an expired wait the loop treats as final, including a journey with no
  wait seam wired at all.

And the stops that deliberately WAIT for a person still do not: a budget
breach that can be escalated, and a published merge card awaiting its
answer. Those builds are not over, and releasing the queue's message there
would let a later build overtake a journey that is still someone's to
answer.

The suite drives the coroutines through ``asyncio.run`` — this package's
conductor tests do not declare ``pytest-asyncio``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from forge.pipeline.conductor_driver import (
    ConductorDriverDeps,
    ConductorRunOutcome,
    WaitWindow,
    drive_fix_journey,
)
from forge.pipeline.stage_taxonomy import StageClass
from forge.pipeline.supervisor import TurnOutcome, TurnReport

BUILD_ID = "build-FEAT-39F6-20260908110853"


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


@dataclass
class FakeSupervisor:
    script: list[Any] = field(default_factory=list)
    budget_pause: Any | None = None

    async def next_turn(self, build_id: str) -> Any:
        if not self.script:
            return TurnReport(outcome=TurnOutcome.TERMINAL, build_id=build_id)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@dataclass
class Recorder:
    """The two seams a terminal close-out drives, in the order it drives them."""

    closed: list[Any] = field(default_factory=list)
    released: list[str] = field(default_factory=list)
    order: list[str] = field(default_factory=list)

    async def close_out(self, *, build_id: str, report: Any) -> None:
        self.closed.append(report)
        self.order.append("close-out")

    async def release(self, build_id: str) -> None:
        self.released.append(build_id)
        self.order.append("release")

    @property
    def reasons(self) -> list[str]:
        return [getattr(r, "rationale", "") for r in self.closed]

    @property
    def terminal_words(self) -> list[str]:
        return [
            getattr(getattr(r, "dispatch_result", None), "outcome", None)
            for r in self.closed
        ]


@dataclass
class ScriptedWait:
    """A wait seam. A window of zero seconds has already run out.

    With time left on the window the waiter answers with a signal, so the
    loop re-plans — which is what the turn-level rules need in order to
    reach their limits. With none left the wait expires, which is an ending
    of its own.
    """

    remaining_seconds: float = 0.0

    def read_window(self, build_id: str) -> WaitWindow:
        return WaitWindow(remaining_seconds=self.remaining_seconds, phase=2)

    async def subscribe(
        self, build_id: str, *, armed: Any, timeout_seconds: int
    ) -> Any:
        armed.set()
        return "resume"


async def _no_sleep(_seconds: float) -> None:
    return None


def _report(
    outcome: TurnOutcome,
    *,
    rationale: str = "",
    stage: StageClass | None = None,
) -> TurnReport:
    return TurnReport(
        outcome=outcome,
        build_id=BUILD_ID,
        chosen_stage=stage,
        rationale=rationale,
    )


def _deps(
    supervisor: FakeSupervisor, recorder: Recorder, **overrides: Any
) -> ConductorDriverDeps:
    base: dict[str, Any] = {
        "supervisor": supervisor,
        "sleep": _no_sleep,
        "close_out": recorder.close_out,
        "release_queue_message": recorder.release,
    }
    base.update(overrides)
    return ConductorDriverDeps(**base)


# ---------------------------------------------------------------------------
# The five endings
# ---------------------------------------------------------------------------


def _packs() -> tuple[list[dict[str, Any]], Any]:
    written: list[dict[str, Any]] = []

    def write(**kwargs: Any) -> str:
        written.append(kwargs)
        return "/packs/fix.json"

    return written, write


class TestEveryEndingStopClosesTheBuildOut:
    def test_the_turn_level_nothing_changed_stop(self) -> None:
        recorder = Recorder()
        packs, write_pack = _packs()
        wait = ScriptedWait(remaining_seconds=60.0)
        supervisor = FakeSupervisor(
            script=[_report(TurnOutcome.WAITING, rationale="same") for _ in range(4)]
        )

        report = asyncio.run(
            drive_fix_journey(
                BUILD_ID,
                _deps(
                    supervisor,
                    recorder,
                    wait_window_reader=wait.read_window,
                    subscribe_resume=wait.subscribe,
                    write_failure_pack=write_pack,
                ),
            )
        )

        assert report.outcome is ConductorRunOutcome.NOTHING_CHANGED
        assert recorder.released == [BUILD_ID]
        assert recorder.terminal_words == ["failed"]
        assert recorder.reasons[0].startswith("stopped: ")
        assert "nothing-changed" in recorder.reasons[0]
        assert packs, "the pack is written as before"

    def test_the_review_cycle_nothing_changed_stop(self) -> None:
        """The stop that stranded attempt eight."""
        from tests.forge.pipeline.test_journey_repeated_review_is_unverified import (
            FINDINGS,
            FINDINGS_AGAIN,
            _review_turn,
            _work_turn,
        )

        recorder = Recorder()
        packs, write_pack = _packs()
        supervisor = FakeSupervisor(
            script=[
                _review_turn(FINDINGS, rationale="the first review"),
                _work_turn("TASK-FEAT39F6FIX1"),
                _review_turn(FINDINGS_AGAIN, rationale="the follow-up review"),
            ]
        )

        report = asyncio.run(
            drive_fix_journey(
                BUILD_ID, _deps(supervisor, recorder, write_failure_pack=write_pack)
            )
        )

        assert report.outcome is ConductorRunOutcome.NOTHING_CHANGED
        assert recorder.released == [BUILD_ID]
        assert recorder.terminal_words == ["failed"]
        assert recorder.reasons[0].startswith("stopped: ")
        assert "the review-cycle nothing-changed stop" in recorder.reasons[0]
        assert packs

    def test_the_turn_ceiling(self) -> None:
        recorder = Recorder()
        packs, write_pack = _packs()
        wait = ScriptedWait(remaining_seconds=60.0)
        supervisor = FakeSupervisor(
            script=[
                _report(TurnOutcome.WAITING, rationale=f"r{i}") for i in range(20)
            ]
        )

        report = asyncio.run(
            drive_fix_journey(
                BUILD_ID,
                _deps(
                    supervisor,
                    recorder,
                    wait_window_reader=wait.read_window,
                    subscribe_resume=wait.subscribe,
                    write_failure_pack=write_pack,
                    max_turns=3,
                ),
            )
        )

        assert report.outcome is ConductorRunOutcome.TURN_CAP
        assert recorder.released == [BUILD_ID]
        assert recorder.terminal_words == ["failed"]
        assert "turn ceiling reached" in recorder.reasons[0]
        assert packs

    def test_a_raising_next_turn(self) -> None:
        recorder = Recorder()
        packs, write_pack = _packs()
        supervisor = FakeSupervisor(script=[ValueError("planner exploded")])

        report = asyncio.run(
            drive_fix_journey(
                BUILD_ID, _deps(supervisor, recorder, write_failure_pack=write_pack)
            )
        )

        assert report.outcome is ConductorRunOutcome.ERROR
        assert recorder.released == [BUILD_ID]
        assert recorder.terminal_words == ["failed"]
        assert "ValueError" in recorder.reasons[0]
        assert packs

    def test_a_turn_outcome_with_no_branch(self) -> None:
        """A new outcome nobody taught the loop about — it stops loudly."""
        recorder = Recorder()
        packs, write_pack = _packs()
        supervisor = FakeSupervisor(
            script=[
                TurnReport(
                    outcome="an-outcome-from-the-future",  # type: ignore[arg-type]
                    build_id=BUILD_ID,
                    rationale="nobody has written this branch yet",
                )
            ]
        )

        report = asyncio.run(
            drive_fix_journey(
                BUILD_ID, _deps(supervisor, recorder, write_failure_pack=write_pack)
            )
        )

        assert report.outcome is ConductorRunOutcome.ERROR
        assert recorder.released == [BUILD_ID]
        assert recorder.terminal_words == ["failed"]
        assert "unhandled turn outcome" in recorder.reasons[0]
        assert packs

    def test_an_expired_wait(self) -> None:
        recorder = Recorder()
        packs, write_pack = _packs()
        wait = ScriptedWait(remaining_seconds=0.0)
        supervisor = FakeSupervisor(script=[_report(TurnOutcome.WAITING)])

        report = asyncio.run(
            drive_fix_journey(
                BUILD_ID,
                _deps(
                    supervisor,
                    recorder,
                    wait_window_reader=wait.read_window,
                    subscribe_resume=wait.subscribe,
                    write_failure_pack=write_pack,
                ),
            )
        )

        assert report.outcome is ConductorRunOutcome.WAIT_EXPIRED
        assert recorder.released == [BUILD_ID]
        assert recorder.terminal_words == ["failed"]
        assert "expired" in recorder.reasons[0]
        assert packs

    def test_a_journey_with_no_wait_seam_at_all(self) -> None:
        """It cannot wait, so its stop is final and it closes out."""
        recorder = Recorder()
        supervisor = FakeSupervisor(script=[_report(TurnOutcome.WAITING)])

        report = asyncio.run(
            drive_fix_journey(BUILD_ID, _deps(supervisor, recorder))
        )

        assert report.outcome is ConductorRunOutcome.WAIT_EXPIRED
        assert recorder.released == [BUILD_ID]

    @pytest.mark.parametrize(
        "script",
        [
            [_report(TurnOutcome.WAITING)],
            [ValueError("planner exploded")],
        ],
        ids=["expired-wait", "error"],
    )
    def test_the_queue_release_comes_after_the_close_out(
        self, script: list[Any]
    ) -> None:
        """The row is durable before the next build is let in."""
        recorder = Recorder()
        supervisor = FakeSupervisor(script=list(script))

        asyncio.run(drive_fix_journey(BUILD_ID, _deps(supervisor, recorder)))

        assert recorder.order == ["close-out", "release"]


# ---------------------------------------------------------------------------
# The stops that wait for a person are untouched
# ---------------------------------------------------------------------------


class TestTheStopsThatWaitForAPersonDoNotCloseOut:
    def test_a_budget_breach_that_can_be_escalated_keeps_its_build(self) -> None:
        recorder = Recorder()
        supervisor = FakeSupervisor(
            script=[
                _report(TurnOutcome.PAUSED_BUDGET, rationale="the wall clock ran out")
            ],
            budget_pause=object(),
        )

        report = asyncio.run(
            drive_fix_journey(BUILD_ID, _deps(supervisor, recorder))
        )

        assert report.outcome is ConductorRunOutcome.PAUSED_BUDGET
        assert recorder.closed == []
        assert recorder.released == []

    def test_a_breach_no_one_can_answer_still_closes_out(self) -> None:
        """J1's ending, unchanged — and its wording is unchanged too."""
        recorder = Recorder()
        supervisor = FakeSupervisor(
            script=[
                _report(TurnOutcome.PAUSED_BUDGET, rationale="cap (2) reached")
            ],
            budget_pause=None,
        )

        report = asyncio.run(
            drive_fix_journey(BUILD_ID, _deps(supervisor, recorder))
        )

        assert report.outcome is ConductorRunOutcome.PAUSED_BUDGET
        assert recorder.released == [BUILD_ID]
        assert recorder.reasons[0].startswith("stopped at the cap")
        assert "no one could be asked" in recorder.reasons[0]


# ---------------------------------------------------------------------------
# A routine build is untouched
# ---------------------------------------------------------------------------


class TestARoutineBuildIsUntouched:
    def test_a_build_this_loop_does_not_drive_closes_nothing(self) -> None:
        """The byte-for-byte branch: mode-a / mode-b never reach the loop.

        The supervisor answers ``NOT_DRIVEN`` before a turn is taken, the
        caller falls through to the routine path, and none of this lane's
        seams fire.
        """
        recorder = Recorder()
        supervisor = FakeSupervisor(
            script=[
                _report(
                    TurnOutcome.TERMINAL,
                    rationale="the routine autobuild finished",
                )
            ]
        )

        report = asyncio.run(
            drive_fix_journey(BUILD_ID, _deps(supervisor, recorder))
        )

        assert report.outcome is ConductorRunOutcome.COMPLETED
        assert report.unverified_findings == ()
        # One close-out, the terminal one, exactly as before this lane.
        assert len(recorder.closed) == 1
        assert recorder.released == [BUILD_ID]

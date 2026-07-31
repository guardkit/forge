"""The conductor's turn loop — branches, the turn-serial law, the wait.

Revival design pass §a.2 / §h.1, Stage 1c.

Coverage map:

- All four documented branches with a fake supervisor
  (:class:`TestLoopBranches`): DISPATCHED continues (after the stage
  settles), WAITING waits, PAUSED_BUDGET stops, TERMINAL closes out.
- The turn-serial law (:class:`TestTurnSerialGuard`): a second concurrent
  loop on the same build raises; ``next_turn`` is never called while a
  dispatched stage is in flight.
- The structured wait (:class:`TestStructuredWait`): recomputed from
  durable anchors every iteration, arm-before-post, anti-spin back-off,
  externally-resolved short-circuit, expiry.
- The stop rules (:class:`TestStopRules`): nothing-changed, turn ceiling,
  a raising ``next_turn``.
- The receipts seams (:class:`TestReceiptSeams`): per-turn export, the
  failure pack on a loud stop, and the "receipts never block a journey"
  posture.

The suite drives the coroutines through ``asyncio.run`` — the project
does not declare ``pytest-asyncio``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from forge.pipeline.conductor_driver import (
    ConductorDriverDeps,
    ConductorRunOutcome,
    ConductorTurnLoop,
    TurnSerialViolation,
    WaitWindow,
    drive_fix_journey,
)
from forge.pipeline.stage_taxonomy import StageClass
from forge.pipeline.supervisor import TurnOutcome, TurnReport

BUILD_ID = "build-FEAT-CR-20260731000000"


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


@dataclass
class FakeSupervisor:
    """Returns a scripted sequence of turn reports, recording each call."""

    script: list[Any] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    #: Set while a turn is being served, so a re-entrant call is visible.
    in_turn: bool = False
    observed_reentrancy: bool = False
    #: Timeline shared with the settle seam so ordering can be asserted.
    timeline: list[str] = field(default_factory=list)

    async def next_turn(self, build_id: str) -> Any:
        if self.in_turn:  # pragma: no cover - the loop must never do this
            self.observed_reentrancy = True
        self.in_turn = True
        self.calls.append(build_id)
        self.timeline.append("next_turn")
        try:
            if not self.script:
                return _report(TurnOutcome.TERMINAL, rationale="script exhausted")
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        finally:
            self.in_turn = False


def _report(
    outcome: TurnOutcome,
    *,
    stage: StageClass | None = None,
    rationale: str = "",
) -> TurnReport:
    return TurnReport(
        outcome=outcome,
        build_id=BUILD_ID,
        chosen_stage=stage,
        rationale=rationale,
    )


@dataclass
class ScriptedWait:
    """A wait seam that reports progress a fixed number of times."""

    windows: list[WaitWindow] = field(default_factory=list)
    responses: list[Any] = field(default_factory=list)
    window_reads: int = 0
    subscribe_calls: list[dict[str, Any]] = field(default_factory=list)
    republished: list[str] = field(default_factory=list)
    timeline: list[str] | None = None
    #: When True the subscriber never sets ``armed`` (arm timeout path).
    never_arm: bool = False

    def read_window(self, build_id: str) -> WaitWindow:
        self.window_reads += 1
        if self.windows:
            return self.windows.pop(0)
        return WaitWindow(remaining_seconds=60.0)

    async def subscribe(
        self, build_id: str, *, armed: asyncio.Event, timeout_seconds: int
    ) -> Any:
        self.subscribe_calls.append(
            {"build_id": build_id, "timeout_seconds": timeout_seconds}
        )
        if self.timeline is not None:
            self.timeline.append("armed")
        if not self.never_arm:
            armed.set()
        if self.responses:
            return self.responses.pop(0)
        return {"resume": True}

    async def republish(self, build_id: str) -> None:
        self.republished.append(build_id)
        if self.timeline is not None:
            self.timeline.append("republish")


async def _no_sleep(_seconds: float) -> None:
    """Injected sleep that never spends real wall-clock."""
    return None


def _deps(supervisor: FakeSupervisor, **overrides: Any) -> ConductorDriverDeps:
    wait = overrides.pop("wait", None)
    base: dict[str, Any] = {"supervisor": supervisor, "sleep": _no_sleep}
    if wait is not None:
        base["wait_window_reader"] = wait.read_window
        base["subscribe_resume"] = wait.subscribe
        base["republish_pending"] = wait.republish
    base.update(overrides)
    return ConductorDriverDeps(**base)


# ---------------------------------------------------------------------------
# The four branches
# ---------------------------------------------------------------------------


class TestLoopBranches:
    def test_terminal_closes_out_and_reports_completed(self) -> None:
        closed: list[str] = []
        supervisor = FakeSupervisor(script=[_report(TurnOutcome.TERMINAL)])

        async def close_out(*, build_id: str, report: Any) -> None:
            closed.append(build_id)

        report = asyncio.run(
            drive_fix_journey(BUILD_ID, _deps(supervisor, close_out=close_out))
        )

        assert report.outcome is ConductorRunOutcome.COMPLETED
        assert report.turns == 1
        assert closed == [BUILD_ID]

    def test_dispatched_continues_after_the_stage_settles(self) -> None:
        wait = ScriptedWait()
        supervisor = FakeSupervisor(
            script=[
                _report(TurnOutcome.DISPATCHED, stage=StageClass.TASK_REVIEW),
                _report(TurnOutcome.DISPATCHED, stage=StageClass.TASK_WORK),
                _report(TurnOutcome.TERMINAL),
            ]
        )

        report = asyncio.run(
            drive_fix_journey(BUILD_ID, _deps(supervisor, wait=wait))
        )

        assert report.outcome is ConductorRunOutcome.COMPLETED
        assert report.turns == 3
        # One settle wait per dispatched stage — never a bare re-plan.
        assert len(wait.subscribe_calls) == 2

    def test_waiting_waits_on_the_resume_signal_then_replans(self) -> None:
        wait = ScriptedWait()
        supervisor = FakeSupervisor(
            script=[
                _report(TurnOutcome.WAITING, rationale="review awaiting approval"),
                _report(TurnOutcome.TERMINAL),
            ]
        )

        report = asyncio.run(
            drive_fix_journey(BUILD_ID, _deps(supervisor, wait=wait))
        )

        assert report.outcome is ConductorRunOutcome.COMPLETED
        assert len(wait.subscribe_calls) == 1

    def test_paused_budget_stops_until_the_escalation_resolves(self) -> None:
        wait = ScriptedWait()
        asked: list[str] = []
        supervisor = FakeSupervisor(
            script=[
                _report(
                    TurnOutcome.PAUSED_BUDGET,
                    rationale="review cycles (2) reached cap (2)",
                ),
                # Would run if the loop wrongly continued.
                _report(TurnOutcome.DISPATCHED, stage=StageClass.TASK_WORK),
            ]
        )

        def escalation_resolved(build_id: str) -> bool:
            asked.append(build_id)
            return False

        report = asyncio.run(
            drive_fix_journey(
                BUILD_ID,
                _deps(supervisor, wait=wait, escalation_resolved=escalation_resolved),
            )
        )

        assert report.outcome is ConductorRunOutcome.PAUSED_BUDGET
        assert report.turns == 1
        assert supervisor.calls == [BUILD_ID]
        assert asked == [BUILD_ID]
        assert "cap" in report.rationale

    def test_a_published_merge_card_stops_the_loop(self) -> None:
        """Act inflation guard: never re-plan after the card is out."""

        @dataclass
        class _CardDecision:
            card_published: bool = True

        wait = ScriptedWait()
        delivered = TurnReport(
            outcome=TurnOutcome.DISPATCHED,
            build_id=BUILD_ID,
            chosen_stage=StageClass.PULL_REQUEST_REVIEW,
            dispatch_result=_CardDecision(),
        )
        supervisor = FakeSupervisor(
            script=[delivered, _report(TurnOutcome.TERMINAL)]
        )

        report = asyncio.run(
            drive_fix_journey(BUILD_ID, _deps(supervisor, wait=wait))
        )

        assert report.outcome is ConductorRunOutcome.DELIVERED
        assert report.turns == 1
        assert wait.subscribe_calls == []

    def test_a_dispatch_result_without_a_card_does_not_stop_the_loop(self) -> None:
        """The backwards-compat rail: any other gate result is a dispatch."""
        wait = ScriptedWait()
        supervisor = FakeSupervisor(
            script=[
                TurnReport(
                    outcome=TurnOutcome.DISPATCHED,
                    build_id=BUILD_ID,
                    chosen_stage=StageClass.PULL_REQUEST_REVIEW,
                    dispatch_result={"gate": "pr-review", "status": "submitted"},
                ),
                _report(TurnOutcome.TERMINAL),
            ]
        )

        report = asyncio.run(
            drive_fix_journey(BUILD_ID, _deps(supervisor, wait=wait))
        )

        assert report.outcome is ConductorRunOutcome.COMPLETED
        assert report.turns == 2

    @pytest.mark.parametrize(
        "outcome",
        [
            TurnOutcome.WAITING,
            TurnOutcome.WAITING_PRIOR_AUTOBUILD,
            TurnOutcome.NO_OP,
            TurnOutcome.REFUSED_OUT_OF_BAND,
            TurnOutcome.REFUSED_CONSTITUTIONAL,
        ],
    )
    def test_every_non_dispatch_non_terminal_outcome_waits(
        self, outcome: TurnOutcome
    ) -> None:
        wait = ScriptedWait()
        supervisor = FakeSupervisor(
            script=[_report(outcome), _report(TurnOutcome.TERMINAL)]
        )

        report = asyncio.run(
            drive_fix_journey(BUILD_ID, _deps(supervisor, wait=wait))
        )

        assert report.outcome is ConductorRunOutcome.COMPLETED
        assert len(wait.subscribe_calls) == 1


# ---------------------------------------------------------------------------
# The turn-serial law (risk h.1)
# ---------------------------------------------------------------------------


class TestTurnSerialGuard:
    def test_a_second_loop_on_the_same_build_raises(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        @dataclass
        class BlockingSupervisor:
            calls: int = 0

            async def next_turn(self, build_id: str) -> Any:
                self.calls += 1
                started.set()
                await release.wait()
                return _report(TurnOutcome.TERMINAL)

        async def scenario() -> Any:
            supervisor = BlockingSupervisor()
            deps = ConductorDriverDeps(supervisor=supervisor, sleep=_no_sleep)
            first = asyncio.ensure_future(
                ConductorTurnLoop(deps).drive(BUILD_ID)
            )
            await started.wait()
            with pytest.raises(TurnSerialViolation):
                await ConductorTurnLoop(deps).drive(BUILD_ID)
            release.set()
            return await first

        report = asyncio.run(scenario())
        assert report.outcome is ConductorRunOutcome.COMPLETED

    def test_the_sentinel_is_released_even_when_next_turn_raises(self) -> None:
        supervisor = FakeSupervisor(script=[RuntimeError("boom")])
        deps = _deps(supervisor)

        first = asyncio.run(ConductorTurnLoop(deps).drive(BUILD_ID))
        assert first.outcome is ConductorRunOutcome.ERROR

        # The build is drivable again — the sentinel did not leak.
        supervisor.script = [_report(TurnOutcome.TERMINAL)]
        second = asyncio.run(ConductorTurnLoop(deps).drive(BUILD_ID))
        assert second.outcome is ConductorRunOutcome.COMPLETED

    def test_next_turn_is_never_called_while_a_stage_is_in_flight(self) -> None:
        """The belt: a dispatched stage settles BEFORE the next plan."""
        timeline: list[str] = []
        wait = ScriptedWait(timeline=timeline)
        supervisor = FakeSupervisor(
            script=[
                _report(TurnOutcome.DISPATCHED, stage=StageClass.TASK_WORK),
                _report(TurnOutcome.TERMINAL),
            ],
            timeline=timeline,
        )

        asyncio.run(drive_fix_journey(BUILD_ID, _deps(supervisor, wait=wait)))

        assert supervisor.observed_reentrancy is False
        # next_turn -> the settle wait arms -> next_turn. Never two plans
        # back to back with a dispatch outstanding.
        assert timeline == ["next_turn", "armed", "next_turn"]

    def test_a_waiting_turn_with_no_wait_seam_stops_rather_than_spins(self) -> None:
        supervisor = FakeSupervisor(
            script=[_report(TurnOutcome.WAITING)] * 10
        )

        report = asyncio.run(drive_fix_journey(BUILD_ID, _deps(supervisor)))

        assert report.outcome is ConductorRunOutcome.WAIT_EXPIRED
        assert supervisor.calls == [BUILD_ID]  # exactly one turn, no spin


# ---------------------------------------------------------------------------
# The structured wait
# ---------------------------------------------------------------------------


class TestStructuredWait:
    def test_the_window_is_recomputed_from_durable_anchors_each_iteration(
        self,
    ) -> None:
        # Two rounds: the first waiter returns nothing (window elapsed),
        # the loop re-reads the anchors and waits again.
        wait = ScriptedWait(
            windows=[
                WaitWindow(remaining_seconds=30.0),
                WaitWindow(remaining_seconds=15.0),
            ],
            responses=[None, {"resume": True}],
        )
        supervisor = FakeSupervisor(
            script=[
                _report(TurnOutcome.WAITING),
                _report(TurnOutcome.TERMINAL),
            ]
        )

        report = asyncio.run(
            drive_fix_journey(BUILD_ID, _deps(supervisor, wait=wait))
        )

        assert report.outcome is ConductorRunOutcome.COMPLETED
        assert wait.window_reads == 2
        # The second round's timeout came from the RE-READ anchor, not a
        # counted-down in-memory value.
        assert [c["timeout_seconds"] for c in wait.subscribe_calls] == [30, 15]

    def test_arm_before_post_republishes_only_after_the_waiter_is_armed(
        self,
    ) -> None:
        timeline: list[str] = []
        wait = ScriptedWait(
            windows=[WaitWindow(remaining_seconds=30.0, needs_republish=True)],
            timeline=timeline,
        )
        supervisor = FakeSupervisor(
            script=[_report(TurnOutcome.WAITING), _report(TurnOutcome.TERMINAL)],
            timeline=timeline,
        )

        asyncio.run(drive_fix_journey(BUILD_ID, _deps(supervisor, wait=wait)))

        assert wait.republished == [BUILD_ID]
        assert timeline.index("armed") < timeline.index("republish")

    def test_no_republish_when_the_window_does_not_ask_for_one(self) -> None:
        wait = ScriptedWait(windows=[WaitWindow(remaining_seconds=30.0)])
        supervisor = FakeSupervisor(
            script=[_report(TurnOutcome.WAITING), _report(TurnOutcome.TERMINAL)]
        )

        asyncio.run(drive_fix_journey(BUILD_ID, _deps(supervisor, wait=wait)))

        assert wait.republished == []

    def test_an_externally_resolved_window_replans_immediately(self) -> None:
        wait = ScriptedWait(
            windows=[WaitWindow(remaining_seconds=30.0, resolved=True)]
        )
        supervisor = FakeSupervisor(
            script=[_report(TurnOutcome.WAITING), _report(TurnOutcome.TERMINAL)]
        )

        report = asyncio.run(
            drive_fix_journey(BUILD_ID, _deps(supervisor, wait=wait))
        )

        assert report.outcome is ConductorRunOutcome.COMPLETED
        assert wait.subscribe_calls == []  # never armed a dead round

    def test_an_expired_window_is_a_loud_stop_with_a_pack(self) -> None:
        packs: list[dict[str, Any]] = []
        wait = ScriptedWait(windows=[WaitWindow(remaining_seconds=0.0, phase=2)])
        supervisor = FakeSupervisor(script=[_report(TurnOutcome.WAITING)])

        def write_pack(**kwargs: Any) -> str:
            packs.append(kwargs)
            return "/packs/fix.json"

        report = asyncio.run(
            drive_fix_journey(
                BUILD_ID,
                _deps(supervisor, wait=wait, write_failure_pack=write_pack),
            )
        )

        assert report.outcome is ConductorRunOutcome.WAIT_EXPIRED
        assert report.failure_pack == "/packs/fix.json"
        assert packs and packs[0]["outcome"] == "wait-expired"

    def test_anti_spin_backs_off_when_the_waiter_returns_instantly(self) -> None:
        """A broken wire must not hot-loop the daemon."""
        slept: list[float] = []
        clock_values = iter([0.0] * 40)

        async def sleeper(seconds: float) -> None:
            slept.append(seconds)

        wait = ScriptedWait(
            windows=[
                WaitWindow(remaining_seconds=30.0),
                WaitWindow(remaining_seconds=30.0),
            ],
            responses=[None, {"resume": True}],
        )
        supervisor = FakeSupervisor(
            script=[_report(TurnOutcome.WAITING), _report(TurnOutcome.TERMINAL)]
        )

        asyncio.run(
            drive_fix_journey(
                BUILD_ID,
                _deps(
                    supervisor,
                    wait=wait,
                    sleep=sleeper,
                    clock=lambda: next(clock_values),
                ),
            )
        )

        assert slept, "an instantly-returning waiter must trigger the back-off"

    def test_an_arm_timeout_retries_rather_than_hanging(self) -> None:
        slept: list[float] = []

        async def sleeper(seconds: float) -> None:
            slept.append(seconds)

        never = ScriptedWait(
            windows=[
                WaitWindow(remaining_seconds=30.0),
                WaitWindow(remaining_seconds=0.0),
            ],
            never_arm=True,
        )
        supervisor = FakeSupervisor(script=[_report(TurnOutcome.WAITING)])

        report = asyncio.run(
            drive_fix_journey(
                BUILD_ID,
                _deps(
                    supervisor,
                    wait=never,
                    sleep=sleeper,
                    arm_timeout_seconds=0.01,
                ),
            )
        )

        assert report.outcome is ConductorRunOutcome.WAIT_EXPIRED
        assert slept == [1.0]

    def test_a_raising_window_reader_expires_rather_than_crashing(self) -> None:
        def boom(build_id: str) -> WaitWindow:
            raise RuntimeError("db gone")

        supervisor = FakeSupervisor(script=[_report(TurnOutcome.WAITING)])
        deps = ConductorDriverDeps(
            supervisor=supervisor,
            wait_window_reader=boom,
            subscribe_resume=ScriptedWait().subscribe,
            sleep=_no_sleep,
        )

        report = asyncio.run(drive_fix_journey(BUILD_ID, deps))
        assert report.outcome is ConductorRunOutcome.WAIT_EXPIRED


# ---------------------------------------------------------------------------
# Stop rules
# ---------------------------------------------------------------------------


class TestStopRules:
    def test_nothing_changed_stops_loudly(self) -> None:
        wait = ScriptedWait()
        supervisor = FakeSupervisor(
            script=[
                _report(TurnOutcome.WAITING, rationale="same"),
                _report(TurnOutcome.WAITING, rationale="same"),
                _report(TurnOutcome.WAITING, rationale="same"),
                _report(TurnOutcome.WAITING, rationale="same"),
                _report(TurnOutcome.TERMINAL),
            ]
        )

        report = asyncio.run(
            drive_fix_journey(BUILD_ID, _deps(supervisor, wait=wait))
        )

        assert report.outcome is ConductorRunOutcome.NOTHING_CHANGED
        assert report.turns == 4
        assert "nothing-changed" in report.rationale

    def test_a_changing_rationale_is_progress_not_a_wedge(self) -> None:
        wait = ScriptedWait()
        supervisor = FakeSupervisor(
            script=[
                _report(TurnOutcome.WAITING, rationale="a"),
                _report(TurnOutcome.WAITING, rationale="b"),
                _report(TurnOutcome.WAITING, rationale="c"),
                _report(TurnOutcome.WAITING, rationale="d"),
                _report(TurnOutcome.TERMINAL),
            ]
        )

        report = asyncio.run(
            drive_fix_journey(BUILD_ID, _deps(supervisor, wait=wait))
        )

        assert report.outcome is ConductorRunOutcome.COMPLETED

    def test_the_turn_ceiling_stops_a_planner_defect(self) -> None:
        wait = ScriptedWait()
        # Alternating rationales defeat the nothing-changed rule, so the
        # ceiling is the only thing standing between a planner defect and
        # an indefinitely-held slot.
        script = []
        for index in range(50):
            script.append(_report(TurnOutcome.WAITING, rationale=f"r{index}"))
        supervisor = FakeSupervisor(script=script)

        report = asyncio.run(
            drive_fix_journey(
                BUILD_ID, _deps(supervisor, wait=wait, max_turns=5)
            )
        )

        assert report.outcome is ConductorRunOutcome.TURN_CAP
        assert report.turns == 5

    def test_a_raising_next_turn_stops_the_journey_and_never_propagates(
        self,
    ) -> None:
        supervisor = FakeSupervisor(script=[ValueError("planner exploded")])

        report = asyncio.run(drive_fix_journey(BUILD_ID, _deps(supervisor)))

        assert report.outcome is ConductorRunOutcome.ERROR
        assert "ValueError" in report.rationale


# ---------------------------------------------------------------------------
# Receipts seams (design pass §b.2, the OUT direction)
# ---------------------------------------------------------------------------


class TestReceiptSeams:
    def test_every_turn_exports_a_stage_receipt(self) -> None:
        exported: list[Any] = []
        wait = ScriptedWait()

        def export(*, build_id: str, report: Any) -> str:
            exported.append(report.outcome)
            return f"{len(exported):03d}-task-review"

        supervisor = FakeSupervisor(
            script=[
                _report(TurnOutcome.DISPATCHED, stage=StageClass.TASK_REVIEW),
                _report(TurnOutcome.TERMINAL),
            ]
        )

        report = asyncio.run(
            drive_fix_journey(
                BUILD_ID,
                _deps(supervisor, wait=wait, export_stage_receipts=export),
            )
        )

        assert exported == [TurnOutcome.DISPATCHED, TurnOutcome.TERMINAL]
        assert report.stage_receipts == ("001-task-review", "002-task-review")

    def test_a_raising_receipts_exporter_never_blocks_the_journey(self) -> None:
        def boom(**kwargs: Any) -> str:
            raise OSError("disk full")

        supervisor = FakeSupervisor(script=[_report(TurnOutcome.TERMINAL)])

        report = asyncio.run(
            drive_fix_journey(
                BUILD_ID, _deps(supervisor, export_stage_receipts=boom)
            )
        )

        assert report.outcome is ConductorRunOutcome.COMPLETED
        assert report.stage_receipts == ()

    def test_the_failure_pack_carries_the_stage_receipt_keys(self) -> None:
        packs: list[dict[str, Any]] = []
        wait = ScriptedWait()

        def export(*, build_id: str, report: Any) -> str:
            return "001-task-review"

        def write_pack(**kwargs: Any) -> str:
            packs.append(kwargs)
            return "/packs/fix.json"

        supervisor = FakeSupervisor(
            script=[_report(TurnOutcome.PAUSED_BUDGET, rationale="cap")]
        )

        asyncio.run(
            drive_fix_journey(
                BUILD_ID,
                _deps(
                    supervisor,
                    wait=wait,
                    export_stage_receipts=export,
                    write_failure_pack=write_pack,
                ),
            )
        )

        assert packs[0]["stage_keys"] == ("001-task-review",)

    def test_a_raising_close_out_never_loses_the_terminal(self) -> None:
        def boom(**kwargs: Any) -> None:
            raise RuntimeError("emit failed")

        supervisor = FakeSupervisor(script=[_report(TurnOutcome.TERMINAL)])

        report = asyncio.run(
            drive_fix_journey(BUILD_ID, _deps(supervisor, close_out=boom))
        )

        assert report.outcome is ConductorRunOutcome.COMPLETED

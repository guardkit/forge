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
- The review-cycle no-progress stop
  (:class:`TestReviewCycleNoProgressStop`, LI stage-2 §5): finding anchors
  compared across review cycles, replay-shaped against the runaway
  ledger's 347/355 pair.
- The receipts seams (:class:`TestReceiptSeams`): per-turn export, the
  failure pack on a loud stop, and the "receipts never block a journey"
  posture.

The suite drives the coroutines through ``asyncio.run`` — the project
does not declare ``pytest-asyncio``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from forge.adapters.guardkit.parser import parse_guardkit_output
from forge.pipeline.dispatchers.subprocess import (
    StageDispatchResult,
    StageDispatchStatus,
)
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
# THE RED-GATE HONEST WORD (shadow-replay item 1)
# ---------------------------------------------------------------------------


def _red_gate_report() -> TurnReport:
    """A WAITING turn whose dispatch result is a REAL red-gate loop-back.

    Built from the production :class:`MergeCardDecision` so the duck-typed
    ``loops_back`` / ``gates`` reads under test are the real fields, not a
    stub that happens to answer the same way.
    """
    from forge.pipeline.merge_ready_checkpoint import (
        GatesReport,
        GateStatus,
        MergeCardDecision,
        MergeCardOutcome,
    )

    return TurnReport(
        outcome=TurnOutcome.WAITING,
        build_id=BUILD_ID,
        rationale="the merge-ready checkpoint looped back",
        dispatch_result=MergeCardDecision(
            outcome=MergeCardOutcome.RED_GATE_LOOP_BACK,
            build_id=BUILD_ID,
            gates=GatesReport(
                status=GateStatus.RED,
                failed_gates=("declared toolchain test",),
                detail="`npm test` exited 1",
            ),
        ),
    )


class TestTheRedGateHonestWord:
    def test_a_red_gate_with_no_resume_seam_stops_RED_GATE_STOP(self) -> None:
        """The lane's sharpest item: never WAIT_EXPIRED for a red gate.

        With no resume seam the loop-back has nothing to wake it, so the
        journey stops — and the stop is named for what caused it.
        """
        supervisor = FakeSupervisor(script=[_red_gate_report()] * 5)

        report = asyncio.run(drive_fix_journey(BUILD_ID, _deps(supervisor)))

        assert report.outcome is ConductorRunOutcome.RED_GATE_STOP
        assert report.outcome is not ConductorRunOutcome.WAIT_EXPIRED
        assert supervisor.calls == [BUILD_ID]  # one turn, no spin

    def test_the_stop_names_the_failing_gate(self) -> None:
        supervisor = FakeSupervisor(script=[_red_gate_report()])

        report = asyncio.run(drive_fix_journey(BUILD_ID, _deps(supervisor)))

        assert "declared toolchain test" in report.rationale
        assert "red" in report.rationale.lower()

    def test_the_failure_pack_carries_the_red_gate_word(self) -> None:
        packs: list[dict[str, Any]] = []
        supervisor = FakeSupervisor(script=[_red_gate_report()])
        deps = _deps(
            supervisor,
            write_failure_pack=lambda **kw: packs.append(kw) or "pack-key",
        )

        report = asyncio.run(drive_fix_journey(BUILD_ID, deps))

        assert report.failure_pack == "pack-key"
        assert packs[0]["outcome"] == ConductorRunOutcome.RED_GATE_STOP.value
        assert "declared toolchain test" in packs[0]["reason"]

    def test_a_red_gate_WITH_a_resume_seam_stays_a_legitimate_wait(self) -> None:
        """The other half of the ruling: a re-plan IS legitimate here.

        A loop-back that something can wake is the design — the next
        review pass picks the branch up. So with a wait seam wired the
        turn waits, the loop re-plans, and RED_GATE_STOP is never reached.
        """
        wait = ScriptedWait()
        supervisor = FakeSupervisor(
            script=[
                _red_gate_report(),
                _report(TurnOutcome.TERMINAL, rationale="re-planned and closed"),
            ]
        )

        report = asyncio.run(drive_fix_journey(BUILD_ID, _deps(supervisor, wait=wait)))

        assert report.outcome is ConductorRunOutcome.COMPLETED
        assert len(supervisor.calls) == 2  # it really did re-plan
        assert wait.subscribe_calls  # and it really did wait first

    def test_a_plain_waiting_turn_is_still_a_wait_expiry(self) -> None:
        """The new word is reachable ONLY from a red-gate loop-back.

        A WAITING turn that is not a checkpoint result keeps the old
        outcome exactly — this branch must not swallow the wait-expiry
        stop it sits next to.
        """
        supervisor = FakeSupervisor(script=[_report(TurnOutcome.WAITING)])

        report = asyncio.run(drive_fix_journey(BUILD_ID, _deps(supervisor)))

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


# ---------------------------------------------------------------------------
# The review-cycle no-progress stop (LI stage-2 §5, FB3)
# ---------------------------------------------------------------------------


class TestReviewCycleNoProgressStop:
    """Replay-shaped: real leg stdout → real parser → real dispatch result.

    The runaway ledger is the fixture. Review rows 347 / 355 / 363 / 371
    emitted byte-identical findings four cycles running and the turn-level
    rule never fired, because a ``/task-work`` turn sits between every pair
    of reviews and breaks the adjacency its fingerprint needs. So these
    drives interleave work turns exactly as the real journey does — a rule
    proven only on back-to-back reviews would prove nothing about the
    failure it exists to catch.

    Nothing here is a mock of the thing under test: the anchors travel the
    real path (the builder's marker-block text → forge's own
    ``parse_guardkit_output`` → a real ``StageDispatchResult``) and only
    the supervisor, which is not what FB3 changed, is a double.
    """

    @staticmethod
    def _review_stdout(findings: list[dict[str, Any]] | None) -> str:
        """The builder's ``render_marker_block`` shape, verbatim.

        ``None`` = the leg emitted NO ``## Detection Findings`` section at
        all, which is the fail-closed case. An empty list is the very
        different "it looked and found nothing".
        """
        lines = ["## Artefacts", "- /w/tasks/TASK-FIX007-001.yaml", ""]
        if findings is not None:
            lines.append("## Detection Findings")
            lines.append("```json")
            lines.append(json.dumps(findings, indent=2))
            lines.append("```")
        return "\n".join(lines)

    @classmethod
    def _review_dispatch_result(
        cls, findings: list[dict[str, Any]] | None
    ) -> StageDispatchResult:
        parsed = parse_guardkit_output(
            subcommand="task-review",
            stdout=cls._review_stdout(findings),
            stderr="",
            exit_code=0,
            duration_secs=1.0,
        )
        return StageDispatchResult(
            status=StageDispatchStatus.SUCCESS,
            stage=StageClass.TASK_REVIEW,
            build_id=BUILD_ID,
            feature_id=None,
            correlation_id="corr-1",
            artefact_paths=tuple(parsed.artefacts),
            rationale="task-review completed",
            exit_code=0,
            duration_secs=1.0,
            subcommand="task-review",
            detection_findings=tuple(parsed.detection_findings or ()),
            detection_findings_reported=parsed.detection_findings is not None,
        )

    @classmethod
    def _review_turn(
        cls, findings: list[dict[str, Any]] | None, *, rationale: str
    ) -> TurnReport:
        return TurnReport(
            outcome=TurnOutcome.DISPATCHED,
            build_id=BUILD_ID,
            chosen_stage=StageClass.TASK_REVIEW,
            rationale=rationale,
            dispatch_result=cls._review_dispatch_result(findings),
        )

    @staticmethod
    def _work_turn(fix_task_id: str) -> TurnReport:
        """The ``/task-work`` turn that sits between two reviews."""
        return TurnReport(
            outcome=TurnOutcome.DISPATCHED,
            build_id=BUILD_ID,
            chosen_stage=StageClass.TASK_WORK,
            rationale=f"MODE_C planner chose task-work for {fix_task_id}",
            dispatch_result=StageDispatchResult(
                status=StageDispatchStatus.SUCCESS,
                stage=StageClass.TASK_WORK,
                build_id=BUILD_ID,
                feature_id=None,
                correlation_id="corr-1",
                artefact_paths=(),
                rationale="task-work completed",
                exit_code=0,
                duration_secs=1.0,
                subcommand="task-work",
            ),
        )

    #: The 347/355 pair, as the ledger recorded it — one defect, two titles,
    #: a drifting line number, the same file and severity.
    _FINDINGS_347: list[dict[str, Any]] = [
        {
            "pattern": "UNGROUNDED",
            "file": "src/core/config.py",
            "line": 14,
            "severity": "critical",
            "evidence": "settings loaded twice",
        },
        {
            "pattern": "PHANTOM",
            "file": "src/api/routes.py",
            "line": 88,
            "severity": "high",
            "evidence": "handler never registered",
        },
    ]
    _FINDINGS_355: list[dict[str, Any]] = [
        {
            "pattern": "SCOPE_CREEP",
            "file": "src/core/config.py",
            "line": None,
            "severity": "critical",
            "evidence": "the same double load, retitled",
        },
        {
            "pattern": "PHANTOM",
            "file": "src/api/routes.py:36",
            "severity": "high",
            "evidence": "handler still never registered",
        },
    ]

    def test_two_reviews_with_the_same_anchors_stop_at_the_second(self) -> None:
        """(a) — the 347/355 pair, with a work turn between them."""
        wait = ScriptedWait()
        packs: list[dict[str, Any]] = []

        def write_pack(**kwargs: Any) -> str:
            packs.append(kwargs)
            return "/packs/fix.json"

        supervisor = FakeSupervisor(
            script=[
                self._review_turn(self._FINDINGS_347, rationale="review 347"),
                self._work_turn("TASK-FIX007-001"),
                self._review_turn(self._FINDINGS_355, rationale="review 355"),
                # Would run if the stop failed — the ledger's 363 and 371.
                self._work_turn("TASK-FIX007-002"),
                self._review_turn(self._FINDINGS_347, rationale="review 363"),
            ]
        )

        report = asyncio.run(
            drive_fix_journey(
                BUILD_ID,
                _deps(supervisor, wait=wait, write_failure_pack=write_pack),
            )
        )

        assert report.outcome is ConductorRunOutcome.NOTHING_CHANGED
        assert report.turns == 3, "the stop must fire ON the second review"
        # The anchors are NAMED — in the rationale and in the pack.
        assert "src/core/config.py|critical" in report.rationale
        assert "src/api/routes.py|high" in report.rationale
        assert "src/core/config.py|critical" in packs[0]["reason"]
        assert report.failure_pack == "/packs/fix.json"

    def test_the_titles_and_line_numbers_are_not_the_identity(self) -> None:
        """Same stop, and the reason proves WHY the two reviews matched.

        Every title differs and every line differs between 347 and 355 —
        88 distinct fix-task ids for ~5 defects was the measured failure.
        Only the file+severity anchor survives, which is the whole design.
        """
        titles_347 = {f["pattern"] for f in self._FINDINGS_347}
        titles_355 = {f["pattern"] for f in self._FINDINGS_355}
        assert titles_347 != titles_355

        wait = ScriptedWait()
        supervisor = FakeSupervisor(
            script=[
                self._review_turn(self._FINDINGS_347, rationale="review 347"),
                self._work_turn("TASK-FIX007-001"),
                self._review_turn(self._FINDINGS_355, rationale="review 355"),
            ]
        )

        report = asyncio.run(
            drive_fix_journey(BUILD_ID, _deps(supervisor, wait=wait))
        )

        assert report.outcome is ConductorRunOutcome.NOTHING_CHANGED
        assert "not one of them was resolved" in report.rationale

    def test_a_review_that_resolves_an_anchor_resets_the_streak(self) -> None:
        """(b) — one anchor gone is progress, and the journey continues."""
        wait = ScriptedWait()
        resolved_one = [self._FINDINGS_355[0]]  # routes.py|high is fixed

        supervisor = FakeSupervisor(
            script=[
                self._review_turn(self._FINDINGS_347, rationale="review 1"),
                self._work_turn("TASK-FIX007-001"),
                self._review_turn(resolved_one, rationale="review 2"),
                self._work_turn("TASK-FIX007-002"),
                _report(TurnOutcome.TERMINAL),
            ]
        )

        report = asyncio.run(
            drive_fix_journey(BUILD_ID, _deps(supervisor, wait=wait))
        )

        assert report.outcome is ConductorRunOutcome.COMPLETED
        assert report.turns == 5

    def test_new_findings_on_top_of_the_old_ones_are_still_no_progress(
        self,
    ) -> None:
        """⊇, not ==: adding findings does not redeem fixing none."""
        wait = ScriptedWait()
        superset = self._FINDINGS_355 + [
            {"file": "src/db/session.py", "severity": "medium", "pattern": "NEW"}
        ]

        supervisor = FakeSupervisor(
            script=[
                self._review_turn(self._FINDINGS_347, rationale="review 1"),
                self._work_turn("TASK-FIX007-001"),
                self._review_turn(superset, rationale="review 2"),
            ]
        )

        report = asyncio.run(
            drive_fix_journey(BUILD_ID, _deps(supervisor, wait=wait))
        )

        assert report.outcome is ConductorRunOutcome.NOTHING_CHANGED
        assert "plus new: src/db/session.py|medium" in report.rationale

    def test_a_missing_findings_block_counts_as_no_progress(self) -> None:
        """(c) — fail closed: silence is never read as a fix."""
        wait = ScriptedWait()

        supervisor = FakeSupervisor(
            script=[
                self._review_turn(self._FINDINGS_347, rationale="review 1"),
                self._work_turn("TASK-FIX007-001"),
                self._review_turn(None, rationale="review 2 (no block)"),
            ]
        )

        report = asyncio.run(
            drive_fix_journey(BUILD_ID, _deps(supervisor, wait=wait))
        )

        assert report.outcome is ConductorRunOutcome.NOTHING_CHANGED
        assert "no readable findings block" in report.rationale
        # The baseline is NAMED even though the current review said nothing.
        assert "src/core/config.py|critical" in report.rationale

    def test_an_unparseable_findings_block_counts_as_no_progress(self) -> None:
        """The parser answers None for malformed JSON — same fail-closed read."""
        stdout = (
            "## Artefacts\n- /w/tasks/TASK-FIX007-001.yaml\n\n"
            "## Detection Findings\n```json\n[{'not': 'json'},\n```"
        )
        parsed = parse_guardkit_output(
            subcommand="task-review",
            stdout=stdout,
            stderr="",
            exit_code=0,
            duration_secs=1.0,
        )
        assert parsed.detection_findings is None
        assert parsed.warnings, "the parser records the shape warning"

        broken = TurnReport(
            outcome=TurnOutcome.DISPATCHED,
            build_id=BUILD_ID,
            chosen_stage=StageClass.TASK_REVIEW,
            rationale="review 2 (bad json)",
            dispatch_result=StageDispatchResult(
                status=StageDispatchStatus.SUCCESS,
                stage=StageClass.TASK_REVIEW,
                build_id=BUILD_ID,
                feature_id=None,
                correlation_id="corr-1",
                artefact_paths=(),
                rationale="task-review completed",
                exit_code=0,
                duration_secs=1.0,
                subcommand="task-review",
                detection_findings=tuple(parsed.detection_findings or ()),
                detection_findings_reported=parsed.detection_findings is not None,
            ),
        )

        wait = ScriptedWait()
        supervisor = FakeSupervisor(
            script=[
                self._review_turn(self._FINDINGS_347, rationale="review 1"),
                self._work_turn("TASK-FIX007-001"),
                broken,
            ]
        )

        report = asyncio.run(
            drive_fix_journey(BUILD_ID, _deps(supervisor, wait=wait))
        )

        assert report.outcome is ConductorRunOutcome.NOTHING_CHANGED
        assert "no readable findings block" in report.rationale

    def test_a_first_review_with_no_block_is_not_accused(self) -> None:
        """No baseline, no verdict — a comparison needs two sides."""
        wait = ScriptedWait()
        supervisor = FakeSupervisor(
            script=[
                self._review_turn(None, rationale="review 1 (no block)"),
                self._work_turn("TASK-FIX007-001"),
                self._review_turn(self._FINDINGS_347, rationale="review 2"),
                _report(TurnOutcome.TERMINAL),
            ]
        )

        report = asyncio.run(
            drive_fix_journey(BUILD_ID, _deps(supervisor, wait=wait))
        )

        assert report.outcome is ConductorRunOutcome.COMPLETED
        assert report.turns == 4

    def test_a_clean_review_is_the_success_path_not_a_stop(self) -> None:
        """The empty block is NOT the missing block.

        This is the journey's happy ending: findings → work → a review that
        looks and finds nothing → CLEAN_REVIEW. Reading an empty findings
        block as "no progress" would stop every journey exactly at the
        review that was about to end it well.
        """
        wait = ScriptedWait()
        supervisor = FakeSupervisor(
            script=[
                self._review_turn(self._FINDINGS_347, rationale="review 1"),
                self._work_turn("TASK-FIX007-001"),
                self._review_turn([], rationale="review 2 (clean)"),
                _report(TurnOutcome.TERMINAL),
            ]
        )

        report = asyncio.run(
            drive_fix_journey(BUILD_ID, _deps(supervisor, wait=wait))
        )

        assert report.outcome is ConductorRunOutcome.COMPLETED
        assert report.turns == 4

    def test_a_clean_review_never_becomes_an_accusing_baseline(self) -> None:
        """The empty set is a superset of nothing — it must not be carried.

        Clean review, then a review that finds something: the second is new
        information, not a repeat, and the journey must survive it.
        """
        wait = ScriptedWait()
        supervisor = FakeSupervisor(
            script=[
                self._review_turn([], rationale="review 1 (clean)"),
                self._work_turn("TASK-FIX007-001"),
                self._review_turn(self._FINDINGS_347, rationale="review 2"),
                _report(TurnOutcome.TERMINAL),
            ]
        )

        report = asyncio.run(
            drive_fix_journey(BUILD_ID, _deps(supervisor, wait=wait))
        )

        assert report.outcome is ConductorRunOutcome.COMPLETED
        assert report.turns == 4

    def test_work_turns_never_move_the_baseline(self) -> None:
        """The rule advances on reviews and nothing else.

        Two work turns between the pair, and the stop still fires on the
        second review with the same anchors named.
        """
        wait = ScriptedWait()
        supervisor = FakeSupervisor(
            script=[
                self._review_turn(self._FINDINGS_347, rationale="review 1"),
                self._work_turn("TASK-FIX007-001"),
                self._work_turn("TASK-FIX007-002"),
                self._review_turn(self._FINDINGS_355, rationale="review 2"),
            ]
        )

        report = asyncio.run(
            drive_fix_journey(BUILD_ID, _deps(supervisor, wait=wait))
        )

        assert report.outcome is ConductorRunOutcome.NOTHING_CHANGED
        assert report.turns == 4

    def test_the_turn_level_rule_is_still_intact(self) -> None:
        """(d) — the review rule ADDS a stop; it replaces nothing.

        Identical WAITING turns carry no dispatch result at all, so the
        review-cycle rule never sees them. The turn-level fingerprint still
        stops the wedge, at its own limit, with its own words.
        """
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
        assert "identical turns" in report.rationale
        assert "review-cycle" not in report.rationale

    def test_the_two_rules_do_not_share_a_counter(self) -> None:
        """A journey whose reviews progress is not stopped by their count.

        Four settled reviews, each resolving an anchor, interleaved with
        work turns — more reviews than either limit, and no stop.
        """
        wait = ScriptedWait()
        ladder = [
            [
                {"file": f"src/m{i}.py", "severity": "high"}
                for i in range(depth)
            ]
            for depth in (4, 3, 2, 1)
        ]
        script: list[Any] = []
        for index, findings in enumerate(ladder):
            script.append(self._review_turn(findings, rationale=f"review {index}"))
            script.append(self._work_turn(f"TASK-FIX007-{index:03d}"))
        script.append(_report(TurnOutcome.TERMINAL))
        supervisor = FakeSupervisor(script=script)

        report = asyncio.run(
            drive_fix_journey(BUILD_ID, _deps(supervisor, wait=wait))
        )

        assert report.outcome is ConductorRunOutcome.COMPLETED
        assert report.turns == 9

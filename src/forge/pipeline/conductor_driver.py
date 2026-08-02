"""The conductor's turn loop — the genuinely new piece of the revival.

Revival design pass §a.2 / §h.1 (``supervisor-revival-design-pass-2026-07-31``),
Stage 1c.

The design pass grep-verified the gap: **no production code calls
``supervisor.next_turn``**. The daemon drives builds through the message-bus
consumer straight into the autobuild runner; the conductor's turn loop had
no caller. This module is that caller.

What it does
------------

For one fix-journey build, repeatedly::

    report = await supervisor.next_turn(build_id)

and branch on the outcome:

* ``DISPATCHED`` → continue — but only **after the dispatched stage has
  settled**. See the turn-serial law below.
* ``WAITING`` (and its siblings) → a **structured wait** on the
  approval / resume signal. Never a spin-poll.
* ``PAUSED_BUDGET`` → stop. The build is paused with a risk-high
  escalation card out; the journey resumes when a human resolves it, not
  when a timer fires.
* ``TERMINAL`` → close out and export receipts.

The turn-serial law (risk h.1)
------------------------------

**The loop is strictly turn-serial per build: it never calls
``next_turn`` while a dispatched stage is still in flight.** This is the
belt half of the belt-and-braces fix for the in-flight sentinel defect
(the braces half — the planner's explicit WAIT variant — landed in Stage
1a). Two mechanisms enforce it:

1. A per-build re-entrancy sentinel. A second concurrent :meth:`drive`
   for the same build raises :class:`TurnSerialViolation` rather than
   quietly interleaving turns.
2. Every ``DISPATCHED`` turn waits for the stage to settle before the
   next ``next_turn``. With no settle seam wired the loop uses the same
   structured wait as ``WAITING`` — a dispatched stage's completion is a
   durable-row change like any other, so one mechanism covers both.

The structured wait (stolen from the live-proven pattern)
---------------------------------------------------------

The shape is taken verbatim from the spec-writer chain's driver
(:mod:`forge.planning.driver`), which has been live since 2026-07-16:

* **Recomputed from durable anchors** — the remaining window and the
  escalation phase are re-read on EVERY iteration, so a daemon restart
  neither resets nor double-fires a window.
* **Arm-before-post** — a re-publish happens only after the response
  waiter's subscription is confirmed armed. Never post into a
  subscription that is not listening.
* **Re-entrant from durable rows** — the loop holds no state a crash
  could lose; resuming re-reads the rows and continues at the same step.
* **Anti-spin** — an instantly-returning waiter (a defective wire, an
  empty fake) backs off rather than hot-looping the daemon.

The two nothing-changed stop rules (the cure-then-retry ladder)
--------------------------------------------------------------

Both end the same way — a loud stop with a failure pack rather than a
burnt slot — and each catches what the other structurally cannot:

* **Turn-level** (original): consecutive turns with an identical
  fingerprint. Catches a wedged planner.
* **Review-cycle** (LI stage-2 §5): a settled ``/task-review`` whose
  finding anchors still contain every anchor the previous review reported.
  This exists because the turn-level rule is *unreachable* on a fix
  journey — a ``/task-work`` turn sits between every pair of reviews, so
  the fingerprints never repeat adjacently. Measured on the runaway
  ledger: review rows 347 / 355 / 363 / 371 emitted byte-identical
  fix-task lists four cycles running and the turn-level streak never
  reached its limit. The anchor rule stops that journey at 355.

Domain module, injected collaborators: no NATS, no SQLite, no git types
are imported here. The composition root is :mod:`forge.cli.serve`.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Awaitable, Callable

from forge.pipeline.finding_anchors import derive_finding_anchors
from forge.pipeline.stage_taxonomy import StageClass
from forge.pipeline.supervisor import TurnOutcome

logger = logging.getLogger(__name__)

__all__ = [
    "REVIEW_NO_PROGRESS_LIMIT",
    "ConductorDriverDeps",
    "ConductorRunOutcome",
    "ConductorRunReport",
    "ConductorTurnLoop",
    "TurnSerialViolation",
    "WaitWindow",
    "drive_fix_journey",
]


#: Upper bound (seconds) on how long the loop waits for a resume
#: subscription to arm before giving up on that round and retrying.
#: Mirrors ``planning/driver.py``'s ``_ARM_TIMEOUT_SECONDS`` and
#: ``_serve_gate_activation``'s ``_REARM_ARM_TIMEOUT_SECONDS`` — one
#: number, three loops, so an operator learns it once.
ARM_TIMEOUT_SECONDS: float = 10.0

#: Minimum back-off after a waiter that returned instantly while wall-clock
#: time remained. Anti-spin: a broken wire must never hot-loop the daemon.
ANTI_SPIN_SLEEP_SECONDS: float = 1.0

#: Consecutive identical, no-progress turns before the loop stops loudly.
#: The cure-then-retry ladder's "nothing changed" rung: two retries of an
#: unchanged state are diagnosis, a third is a wedge.
NOTHING_CHANGED_LIMIT: int = 3

#: Hard ceiling on turns for one journey. A bounded fix journey is a
#: handful of turns; this exists so a planner defect cannot spend a
#: consumer slot indefinitely, not as a policy knob.
DEFAULT_MAX_TURNS: int = 200

#: Consecutive no-progress **comparisons** between settled reviews before the
#: review-cycle rule stops the journey (LI stage-2 §5).
#:
#: ONE comparison spans TWO consecutive reviews — which is exactly the
#: design's wording, "two consecutive no-progress reviews → the
#: NOTHING_CHANGED stop", and exactly what the runaway ledger demands: rows
#: 347 and 355 emitted byte-identical findings, and the journey must stop at
#: 355 rather than run 363 and 371 to discover the same thing twice more.
#:
#: It is NOT the turn-level :data:`NOTHING_CHANGED_LIMIT`, and the two must
#: not be collapsed: that one counts adjacent turns and a fix journey never
#: repeats adjacently.
REVIEW_NO_PROGRESS_LIMIT: int = 1


#: Outcomes that mean "the conductor dispatched nothing and the build has
#: not ended" — the structured-wait branch.
_WAITING_OUTCOMES: frozenset[TurnOutcome] = frozenset(
    {
        TurnOutcome.WAITING,
        TurnOutcome.WAITING_PRIOR_AUTOBUILD,
        TurnOutcome.NO_OP,
        TurnOutcome.REFUSED_OUT_OF_BAND,
        TurnOutcome.REFUSED_CONSTITUTIONAL,
    }
)


class TurnSerialViolation(RuntimeError):
    """Raised when a second turn loop is started for a build already driven.

    The turn-serial law (design pass §a.2 / risk h.1) is not advisory: the
    Mode C planner reads history structurally, and planning mid-flight is
    exactly the condition under which its in-flight sentinel mis-encodes
    "a fix task is running" as "all fix tasks completed". Two loops on one
    build would reintroduce that by construction, so the second one fails
    loudly instead.
    """


class ConductorRunOutcome(StrEnum):
    """How one conductor-driven journey ended.

    Members:
        COMPLETED: The supervisor reported ``TERMINAL``; the journey
            closed out and exported its receipts.
        DELIVERED: The merge-ready checkpoint published its card AND the
            owner approved it. The journey is done. The loop STOPS here;
            re-planning would re-publish the card on every tick, which is
            act inflation (design pass risk h.5) dressed up as a retry.
        DECLINED: The card was published and the owner said no (rejected
            / cancelled / hard-stopped). Stopping is right either way —
            but the run report must say what actually happened. Until
            Stage 2 this read ``DELIVERED``, because the loop keyed only
            on ``card_published`` and never looked at the verdict: a
            declined merge was reported as a delivery.
        EXPIRED: The card was published and no answer arrived inside the
            approval window. Not a delivery, not a refusal — a silence,
            and the report says so.
        PAUSED_BUDGET: A budget cap was breached. The build is paused
            with a risk-high escalation out; the loop stopped and the
            queue moves on (design pass §d Stage 3).
        WAIT_EXPIRED: The structured wait's durable window ran out with
            no response. A loud stop with a pack.
        RED_GATE_STOP: The merge-ready checkpoint found a RED gate, looped
            back into the fix cycle, and there was no resume seam wired to
            carry that loop-back anywhere. The journey stops because the
            gate is red — and the report SAYS the gate is red. Before this
            member the same stop was written up as ``WAIT_EXPIRED``: a
            red-gate refusal mis-worded as a silence. When a resume seam
            IS wired the loop-back stays a legitimate wait (the next
            review pass re-plans), so this member is reached only on the
            can't-arm path.
        NOTHING_CHANGED: No durable change. Two rules produce it and both
            are needed (LI stage-2 §5): the TURN-level rule (consecutive
            identical turn fingerprints — catches a wedged planner) and the
            REVIEW-CYCLE rule (a review that re-reported every anchor its
            predecessor found — catches the fix journey the turn-level rule
            structurally cannot, because a ``/task-work`` turn sits between
            every pair of reviews and breaks the adjacency the fingerprint
            needs).
        TURN_CAP: The hard turn ceiling was reached. A planner defect,
            not a legitimate journey.
        NOT_DRIVEN: The build is not one this loop drives (its mode is
            not the fix journey, or the conductor is switched off). The
            caller falls through to the routine path — this is the
            byte-for-byte branch.
        ERROR: ``next_turn`` raised. The loop never re-raises into the
            daemon; the journey stops with the reason recorded.
    """

    COMPLETED = "completed"
    DELIVERED = "delivered"
    DECLINED = "declined"
    EXPIRED = "expired"
    PAUSED_BUDGET = "paused-budget"
    WAIT_EXPIRED = "wait-expired"
    RED_GATE_STOP = "red-gate-stop"
    NOTHING_CHANGED = "nothing-changed"
    TURN_CAP = "turn-cap"
    NOT_DRIVEN = "not-driven"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class WaitWindow:
    """The structured wait's view of the durable anchors.

    Recomputed on EVERY iteration of the wait — never cached, never
    counted down in memory. That is what makes the wait re-entrant: a
    daemon that restarts mid-pause re-reads these and resumes the same
    window instead of starting a fresh one.

    Attributes:
        remaining_seconds: Seconds left in the current window. ``<= 0``
            ends the wait (escalate or expire).
        phase: Escalation phase — 1 is the first window, 2 the escalated
            one. Recorded on the report for triage.
        needs_republish: ``True`` when the persisted request must be
            re-emitted this round (rearm / escalation / defer). The
            re-emit happens only AFTER the waiter is armed.
        resolved: ``True`` when a concurrent actor already resolved the
            wait (approved elsewhere, cancelled, terminal). The loop
            re-plans immediately rather than waiting on a dead round.
    """

    remaining_seconds: float
    phase: int = 1
    needs_republish: bool = False
    resolved: bool = False


@dataclass(frozen=True, slots=True)
class ConductorRunReport:
    """Structured result of one conductor-driven journey.

    Attributes:
        outcome: The :class:`ConductorRunOutcome`.
        build_id: Build the loop drove.
        turns: How many ``next_turn`` calls were made.
        last_report: The final :class:`~forge.pipeline.supervisor.TurnReport`.
        stage_receipts: Per-stage receipt keys exported during the run.
        rationale: Plain-language summary of why the loop ended here.
        failure_pack: Path of the pack written on a loud stop, if any.
    """

    outcome: ConductorRunOutcome
    build_id: str
    turns: int = 0
    last_report: Any | None = None
    stage_receipts: tuple[str, ...] = ()
    rationale: str = ""
    failure_pack: Any | None = None


@dataclass
class ConductorDriverDeps:
    """Injected collaborators of the turn loop.

    Every seam is optional and every ``None`` degrades to a *safe*
    behaviour, never to a crash — the same posture as the mode reader's
    fallback rail. A loop with nothing wired but a supervisor still runs;
    it just cannot wait for anything, so it stops loudly instead of
    spinning.

    Attributes:
        supervisor: The per-build :class:`~forge.pipeline.supervisor.Supervisor`.
            Its ``next_turn(build_id)`` is the only method this loop calls.
        wait_window_reader: ``(build_id) -> WaitWindow`` — the durable
            anchors, re-read on every wait iteration. ``None`` means the
            loop cannot wait: a WAITING turn stops with
            :attr:`ConductorRunOutcome.WAIT_EXPIRED` rather than
            spin-polling.
        subscribe_resume: ``(build_id, armed, timeout_seconds) ->
            Awaitable[Any | None]`` — arms a subscription for the
            approval / resume signal and returns the response, or
            ``None`` when the window elapsed. It MUST set the ``armed``
            :class:`asyncio.Event` as soon as the subscription is live;
            the loop refuses to re-publish before that (arm-before-post).
        republish_pending: ``(build_id) -> Awaitable[None]`` — re-emits
            the persisted request VERBATIM. Called only after ``armed``.
        escalation_resolved: ``(build_id) -> bool`` — whether a budget
            escalation has been resolved. Read once when a
            ``PAUSED_BUDGET`` turn arrives, purely for the report's
            rationale: the loop stops either way (the resolution
            re-queues the build; it does not resume this loop).
        export_stage_receipts: ``(build_id, report) -> Any`` — per-stage
            receipts export (design pass §b.2, the OUT direction).
            Best-effort; a raise is logged and swallowed.
        write_failure_pack: ``(build_id, reason, outcome, stage_keys) ->
            Any`` — the journey's own failure pack on a loud stop.
        close_out: ``(build_id, report) -> Any`` — terminal close-out
            (the caller's own bookkeeping: ack, status, emit).
        clock: Monotonic source, injected for deterministic tests.
        sleep: ``(seconds) -> Awaitable[None]`` — injected so tests do
            not spend real wall-clock on the anti-spin back-off.
        max_turns: Hard turn ceiling for one journey.
        arm_timeout_seconds: See :data:`ARM_TIMEOUT_SECONDS`.
        nothing_changed_limit: See :data:`NOTHING_CHANGED_LIMIT`.
    """

    supervisor: Any
    wait_window_reader: Callable[[str], WaitWindow] | None = None
    subscribe_resume: Callable[..., Awaitable[Any]] | None = None
    republish_pending: Callable[[str], Any] | None = None
    escalation_resolved: Callable[[str], Any] | None = None
    export_stage_receipts: Callable[..., Any] | None = None
    write_failure_pack: Callable[..., Any] | None = None
    close_out: Callable[..., Any] | None = None
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    max_turns: int = DEFAULT_MAX_TURNS
    arm_timeout_seconds: float = ARM_TIMEOUT_SECONDS
    nothing_changed_limit: int = NOTHING_CHANGED_LIMIT
    anti_spin_seconds: float = ANTI_SPIN_SLEEP_SECONDS


#: Build ids currently being driven. Module scope so the sentinel is
#: process-wide: two composition roots in one daemon must not both drive
#: the same build.
_IN_FLIGHT: set[str] = set()


class ConductorTurnLoop:
    """Drives one fix-journey build, strictly one turn at a time."""

    def __init__(self, deps: ConductorDriverDeps) -> None:
        self._deps = deps
        self._stage_receipts: list[str] = []

    async def drive(self, build_id: str) -> ConductorRunReport:
        """Walk ``build_id`` through the conductor until it stops.

        Raises:
            TurnSerialViolation: A loop is already driving this build.
        """
        if build_id in _IN_FLIGHT:
            raise TurnSerialViolation(
                f"a conductor turn loop is already driving build_id={build_id!r}; "
                "the fix journey is strictly turn-serial per build (design "
                "pass risk h.1 — planning mid-flight mis-reads the planner's "
                "in-flight sentinel)"
            )
        _IN_FLIGHT.add(build_id)
        try:
            return await self._drive(build_id)
        finally:
            _IN_FLIGHT.discard(build_id)

    # -- the loop -----------------------------------------------------

    async def _drive(self, build_id: str) -> ConductorRunReport:
        deps = self._deps
        turns = 0
        last_report: Any = None
        unchanged_streak = 0
        last_fingerprint: tuple[Any, ...] | None = None
        # The review-cycle rule's whole state: the anchor set the LAST
        # readable review reported (``None`` = no baseline yet), and how many
        # consecutive comparisons since have shown no progress.
        review_baseline: frozenset[str] | None = None
        no_progress_reviews = 0

        while turns < deps.max_turns:
            try:
                report = await deps.supervisor.next_turn(build_id)
            except Exception as exc:  # noqa: BLE001 — never crash the daemon
                logger.exception(
                    "conductor: next_turn raised for build_id=%s; stopping the "
                    "journey (the routine path is unaffected)",
                    build_id,
                )
                pack = await self._write_pack(
                    build_id,
                    reason=f"next_turn raised {type(exc).__name__}: {exc}",
                    outcome=ConductorRunOutcome.ERROR,
                )
                return ConductorRunReport(
                    outcome=ConductorRunOutcome.ERROR,
                    build_id=build_id,
                    turns=turns,
                    last_report=last_report,
                    stage_receipts=tuple(self._stage_receipts),
                    rationale=f"next_turn raised {type(exc).__name__}: {exc}",
                    failure_pack=pack,
                )

            turns += 1
            last_report = report
            outcome = getattr(report, "outcome", None)
            await self._export_receipts(build_id, report)

            fingerprint = self._fingerprint(report)
            if fingerprint == last_fingerprint:
                unchanged_streak += 1
            else:
                unchanged_streak = 0
                last_fingerprint = fingerprint

            # THE REVIEW-CYCLE RULE (LI stage-2 §5). Evaluated on every
            # turn — it only *does* anything on a settled ``/task-review``
            # — so the baseline advances in lock-step with the reviews
            # themselves and no intervening ``/task-work`` turn can shift it.
            review_verdict = _review_progress_verdict(report, review_baseline)
            if review_verdict is not None:
                review_baseline = review_verdict.baseline
                if review_verdict.no_progress:
                    no_progress_reviews += 1
                else:
                    no_progress_reviews = 0

            if outcome is TurnOutcome.TERMINAL:
                await self._close_out(build_id, report)
                logger.info(
                    "conductor: build_id=%s reached TERMINAL after %d turn(s) "
                    "— closed out, receipts exported",
                    build_id,
                    turns,
                )
                return ConductorRunReport(
                    outcome=ConductorRunOutcome.COMPLETED,
                    build_id=build_id,
                    turns=turns,
                    last_report=report,
                    stage_receipts=tuple(self._stage_receipts),
                    rationale=getattr(report, "rationale", "") or "terminal",
                )

            if _card_was_published(report):
                # THE MERGE CARD IS OUT — stop. The owner's merge word is
                # act three and it is not a conductor turn; a loop that
                # re-planned from here would re-publish the card on every
                # tick. Duck-typed on purpose: the driver stays a domain
                # module and never imports the delivery leg.
                card_outcome = _classify_card_result(report)
                await self._close_out(build_id, report)
                logger.info(
                    "conductor: build_id=%s stopped after %d turn(s) with "
                    "outcome=%s — the merge card was published and the "
                    "owner's answer is the last word",
                    build_id,
                    turns,
                    card_outcome.value,
                )
                return ConductorRunReport(
                    outcome=card_outcome,
                    build_id=build_id,
                    turns=turns,
                    last_report=report,
                    stage_receipts=tuple(self._stage_receipts),
                    rationale=(
                        getattr(report, "rationale", "")
                        or f"the merge-ready checkpoint's card was "
                        f"{card_outcome.value}"
                    ),
                )

            if outcome is TurnOutcome.PAUSED_BUDGET:
                resolved = await self._escalation_resolved(build_id)
                logger.warning(
                    "conductor: build_id=%s breached a budget cap after %d "
                    "turn(s) — STOPPING until the escalation resolves "
                    "(escalation_resolved=%s); the queue moves on",
                    build_id,
                    turns,
                    resolved,
                )
                pack = await self._write_pack(
                    build_id,
                    reason=getattr(report, "rationale", "") or "budget cap breached",
                    outcome=ConductorRunOutcome.PAUSED_BUDGET,
                )
                return ConductorRunReport(
                    outcome=ConductorRunOutcome.PAUSED_BUDGET,
                    build_id=build_id,
                    turns=turns,
                    last_report=report,
                    stage_receipts=tuple(self._stage_receipts),
                    rationale=(
                        getattr(report, "rationale", "") or "budget cap breached"
                    ),
                    failure_pack=pack,
                )

            if unchanged_streak >= deps.nothing_changed_limit:
                reason = (
                    f"{deps.nothing_changed_limit + 1} identical turns with no "
                    f"durable change (outcome={getattr(outcome, 'value', outcome)!r})"
                    " — the nothing-changed stop rule"
                )
                return await self._nothing_changed_stop(
                    build_id, reason=reason, turns=turns, report=report
                )

            if (
                review_verdict is not None
                and no_progress_reviews >= REVIEW_NO_PROGRESS_LIMIT
            ):
                # THE REVIEW-CYCLE NOTHING-CHANGED STOP. Named anchors, not
                # a count: "the same five things, again" is what a human
                # needs to read, and it is what the failure pack records.
                # Guarded on the verdict so the stop always fires ON the
                # review that reached the limit — never on a later turn
                # that has no anchors to name.
                return await self._nothing_changed_stop(
                    build_id,
                    reason=review_verdict.reason,
                    turns=turns,
                    report=report,
                )

            if outcome is TurnOutcome.DISPATCHED or outcome in _WAITING_OUTCOMES:
                # THE TURN-SERIAL BELT: the loop must never plan while a
                # stage is in flight. But "in flight" is a question with
                # two answers, and treating them as one was what made a
                # fix journey die at its first turn (Stage 2 shakeout
                # item 4):
                #
                #   * The fix journey's stages dispatch through
                #     ``dispatch_subprocess_stage``, which is AWAITED
                #     inside ``next_turn``. By the time the turn report
                #     exists the subprocess has already exited and its
                #     stage_log row is written. The await IS the
                #     serialisation; there is nothing left to wait for,
                #     and waiting anyway meant every journey expired its
                #     window and stopped after one review.
                #   * A turn that genuinely parked on something external
                #     — an unresolved gate, an in-flight prerequisite —
                #     has no settled dispatch result, and THAT is what the
                #     structured wait exists for.
                #
                # So: a settled dispatch re-plans immediately; everything
                # else waits. The nothing-changed rule is the backstop
                # against a re-plan that makes no progress.
                if _dispatch_settled(report):
                    logger.debug(
                        "conductor: build_id=%s turn %d settled inside the "
                        "turn (the dispatch was awaited) — re-planning "
                        "without a wait",
                        build_id,
                        turns,
                    )
                    continue

                # THE RED-GATE HONEST WORD (shadow-replay item 1).
                #
                # ``RED_GATE_LOOP_BACK`` is mapped to ``WAITING`` by the
                # supervisor, and that mapping is right: a red gate
                # re-enters the fix cycle, and the conductor's next review
                # pass is what picks the branch up. But a loop-back only
                # goes anywhere if something can WAKE the loop — and with
                # no resume seam wired the wait cannot arm, so the journey
                # died ``WAIT_EXPIRED``: "nobody answered" as the write-up
                # of "the gate was red and we refused to card it".
                #
                # So the word is chosen by what the loop can actually do:
                #
                #   * resume seam wired → the loop-back is a legitimate
                #     wait; fall through to the structured wait unchanged.
                #   * no resume seam → stop NOW with RED_GATE_STOP, and
                #     name the failing gates in the rationale and the pack.
                if _is_red_gate_loop_back(report) and not self._can_arm_a_wait():
                    reason = _red_gate_reason(report)
                    logger.error(
                        "conductor: build_id=%s stopped after %d turn(s) — %s "
                        "(no resume seam is wired, so the fix cycle's "
                        "loop-back has nothing to wake it; this is a RED "
                        "GATE stop, not a wait expiry)",
                        build_id,
                        turns,
                        reason,
                    )
                    pack = await self._write_pack(
                        build_id,
                        reason=reason,
                        outcome=ConductorRunOutcome.RED_GATE_STOP,
                    )
                    return ConductorRunReport(
                        outcome=ConductorRunOutcome.RED_GATE_STOP,
                        build_id=build_id,
                        turns=turns,
                        last_report=report,
                        stage_receipts=tuple(self._stage_receipts),
                        rationale=reason,
                        failure_pack=pack,
                    )

                progressed = await self._structured_wait(build_id, report)
                if not progressed:
                    reason = (
                        "the structured wait's durable window expired with no "
                        "response"
                    )
                    logger.error(
                        "conductor: build_id=%s stopped after %d turn(s) — %s",
                        build_id,
                        turns,
                        reason,
                    )
                    pack = await self._write_pack(
                        build_id,
                        reason=reason,
                        outcome=ConductorRunOutcome.WAIT_EXPIRED,
                    )
                    return ConductorRunReport(
                        outcome=ConductorRunOutcome.WAIT_EXPIRED,
                        build_id=build_id,
                        turns=turns,
                        last_report=report,
                        stage_receipts=tuple(self._stage_receipts),
                        rationale=reason,
                        failure_pack=pack,
                    )
                continue

            # Defensive: an outcome member with no branch. Stop loudly
            # rather than loop — a new TurnOutcome without a branch here
            # is a bug, and a silent spin would hide it.
            reason = (
                f"unhandled turn outcome {getattr(outcome, 'value', outcome)!r}; "
                "the conductor stops rather than spin"
            )
            logger.error("conductor: build_id=%s — %s", build_id, reason)
            pack = await self._write_pack(
                build_id, reason=reason, outcome=ConductorRunOutcome.ERROR
            )
            return ConductorRunReport(
                outcome=ConductorRunOutcome.ERROR,
                build_id=build_id,
                turns=turns,
                last_report=report,
                stage_receipts=tuple(self._stage_receipts),
                rationale=reason,
                failure_pack=pack,
            )

        reason = f"turn ceiling reached ({deps.max_turns} turns)"
        logger.error("conductor: build_id=%s stopped — %s", build_id, reason)
        pack = await self._write_pack(
            build_id, reason=reason, outcome=ConductorRunOutcome.TURN_CAP
        )
        return ConductorRunReport(
            outcome=ConductorRunOutcome.TURN_CAP,
            build_id=build_id,
            turns=turns,
            last_report=last_report,
            stage_receipts=tuple(self._stage_receipts),
            rationale=reason,
            failure_pack=pack,
        )

    # -- the structured wait ------------------------------------------

    async def _structured_wait(self, build_id: str, report: Any) -> bool:
        """Wait for the next durable change. ``True`` when one arrived.

        The shape is ``planning/driver.py``'s, verbatim in intent:
        recompute the window from durable anchors every iteration, arm
        before posting, back off rather than hot-loop, and treat an
        externally-resolved row as progress.
        """
        deps = self._deps
        if deps.wait_window_reader is None or deps.subscribe_resume is None:
            logger.error(
                "conductor: build_id=%s needs to wait (outcome=%s) but no "
                "wait seam is wired (wait_window_reader=%s subscribe_resume=%s)"
                " — refusing to spin-poll; stopping the journey",
                build_id,
                getattr(getattr(report, "outcome", None), "value", None),
                deps.wait_window_reader is not None,
                deps.subscribe_resume is not None,
            )
            return False

        while True:
            try:
                window = deps.wait_window_reader(build_id)
            except Exception as exc:  # noqa: BLE001 — a reader defect is not fatal
                logger.warning(
                    "conductor: wait_window_reader raised %s: %s for "
                    "build_id=%s — treating the window as expired",
                    type(exc).__name__,
                    exc,
                    build_id,
                )
                return False

            if window.resolved:
                # A concurrent actor resolved the pause (approved through
                # another path, cancelled, went terminal). Re-plan now.
                logger.info(
                    "conductor: build_id=%s wait resolved externally; re-planning",
                    build_id,
                )
                return True

            if window.remaining_seconds <= 0:
                logger.warning(
                    "conductor: build_id=%s wait window expired (phase=%d)",
                    build_id,
                    window.phase,
                )
                return False

            armed: asyncio.Event = asyncio.Event()
            wait_started = deps.clock()
            wait_task = asyncio.ensure_future(
                deps.subscribe_resume(
                    build_id,
                    armed=armed,
                    timeout_seconds=max(1, int(window.remaining_seconds)),
                )
            )
            try:
                await asyncio.wait_for(
                    armed.wait(), timeout=deps.arm_timeout_seconds
                )
            except asyncio.TimeoutError:
                logger.error(
                    "conductor: resume subscription failed to arm for "
                    "build_id=%s within %.0fs; retrying",
                    build_id,
                    deps.arm_timeout_seconds,
                )
                wait_task.cancel()
                try:
                    await wait_task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001 — surface the root cause
                    logger.exception(
                        "conductor: resume waiter failed before arming for "
                        "build_id=%s (root cause of the arm timeout)",
                        build_id,
                    )
                # Anti-spin: never tight-loop arming.
                await deps.sleep(deps.anti_spin_seconds)
                continue

            if window.needs_republish and deps.republish_pending is not None:
                # ARM-BEFORE-POST: the subscription is live, now re-emit
                # the persisted request VERBATIM.
                try:
                    await _maybe_await(deps.republish_pending(build_id))
                except Exception as exc:  # noqa: BLE001 — a re-emit is best-effort
                    logger.warning(
                        "conductor: republish_pending raised %s: %s for "
                        "build_id=%s; the window's own expiry is the backstop",
                        type(exc).__name__,
                        exc,
                        build_id,
                    )

            try:
                signal = await wait_task
            except asyncio.CancelledError:  # pragma: no cover - shutdown path
                raise
            except Exception:  # noqa: BLE001 — a waiter defect must not kill it
                logger.exception(
                    "conductor: resume waiter raised for build_id=%s; retrying",
                    build_id,
                )
                await deps.sleep(deps.anti_spin_seconds)
                continue

            if signal is None:
                # Window elapsed with no response — the durable anchors
                # drive escalation / expiry on the next iteration.
                # Anti-spin: an instantly-returning waiter with time left
                # on the clock is a broken wire, not an expiry.
                if deps.clock() - wait_started < deps.anti_spin_seconds:
                    await deps.sleep(deps.anti_spin_seconds)
                continue

            logger.info(
                "conductor: build_id=%s resumed on a durable signal; re-planning",
                build_id,
            )
            return True

    # -- side seams ---------------------------------------------------

    def _can_arm_a_wait(self) -> bool:
        """``True`` when a structured wait could actually arm.

        The same two seams :meth:`_structured_wait` checks first. Read
        here as well so the loop can pick the honest WORD *before* it
        enters a wait it already knows cannot arm.
        """
        deps = self._deps
        return (
            deps.wait_window_reader is not None and deps.subscribe_resume is not None
        )

    @staticmethod
    def _fingerprint(report: Any) -> tuple[Any, ...]:
        """Identity of a turn for the nothing-changed rule."""
        return (
            getattr(getattr(report, "outcome", None), "value", None),
            getattr(getattr(report, "chosen_stage", None), "value", None),
            getattr(report, "chosen_feature_id", None),
            getattr(report, "rationale", None),
        )

    async def _nothing_changed_stop(
        self, build_id: str, *, reason: str, turns: int, report: Any
    ) -> "ConductorRunReport":
        """The loud stop both nothing-changed rules end at.

        One writer for one outcome: the turn-level rule and the
        review-cycle rule differ in what they NOTICE, never in what they
        DO. Stating the ending twice would let the two drift — one growing
        a pack, the other not.
        """
        logger.error("conductor: build_id=%s stopped — %s", build_id, reason)
        pack = await self._write_pack(
            build_id,
            reason=reason,
            outcome=ConductorRunOutcome.NOTHING_CHANGED,
        )
        return ConductorRunReport(
            outcome=ConductorRunOutcome.NOTHING_CHANGED,
            build_id=build_id,
            turns=turns,
            last_report=report,
            stage_receipts=tuple(self._stage_receipts),
            rationale=reason,
            failure_pack=pack,
        )

    async def _export_receipts(self, build_id: str, report: Any) -> None:
        deps = self._deps
        if deps.export_stage_receipts is None:
            return
        try:
            key = await _maybe_await(
                deps.export_stage_receipts(build_id=build_id, report=report)
            )
        except Exception as exc:  # noqa: BLE001 — receipts never block a journey
            logger.warning(
                "conductor: export_stage_receipts raised %s: %s for "
                "build_id=%s — the turn stands, the receipt is missing",
                type(exc).__name__,
                exc,
                build_id,
            )
            return
        if key:
            self._stage_receipts.append(str(key))

    async def _close_out(self, build_id: str, report: Any) -> None:
        deps = self._deps
        if deps.close_out is None:
            return
        try:
            await _maybe_await(deps.close_out(build_id=build_id, report=report))
        except Exception as exc:  # noqa: BLE001 — close-out is best-effort
            logger.warning(
                "conductor: close_out raised %s: %s for build_id=%s — the "
                "terminal stands",
                type(exc).__name__,
                exc,
                build_id,
            )

    async def _escalation_resolved(self, build_id: str) -> bool | None:
        deps = self._deps
        if deps.escalation_resolved is None:
            return None
        try:
            return bool(await _maybe_await(deps.escalation_resolved(build_id)))
        except Exception as exc:  # noqa: BLE001 — an unknown answer is not fatal
            logger.warning(
                "conductor: escalation_resolved raised %s: %s for build_id=%s",
                type(exc).__name__,
                exc,
                build_id,
            )
            return None

    async def _write_pack(
        self, build_id: str, *, reason: str, outcome: ConductorRunOutcome
    ) -> Any | None:
        deps = self._deps
        if deps.write_failure_pack is None:
            return None
        try:
            return await _maybe_await(
                deps.write_failure_pack(
                    build_id=build_id,
                    reason=reason,
                    outcome=outcome.value,
                    stage_keys=tuple(self._stage_receipts),
                )
            )
        except Exception as exc:  # noqa: BLE001 — a pack failure is not fatal
            logger.warning(
                "conductor: write_failure_pack raised %s: %s for build_id=%s "
                "— the stop stands, the pack is missing",
                type(exc).__name__,
                exc,
                build_id,
            )
            return None


#: The owner's answer, as the approve-click machinery words it, mapped onto
#: the honest run-report outcome. Matched on the LOWERCASED verdict token so
#: this domain module keeps no import edge to the gating package: the
#: production card publisher returns a ``GateOutcome`` whose members are
#: ``RESUMED`` (the owner approved and the build resumes), ``OVERRIDDEN``
#: (approved with an override), ``CANCELLED`` (rejected), ``FAILED`` (a
#: hard-stop verdict), ``TIMED_OUT`` (the window closed with no answer) and
#: ``AUTO_APPROVED`` (which the merge card can never produce — the merge word
#: is human forever, refused twice over).
_CARD_VERDICT_WORDS: dict[str, "ConductorRunOutcome"] = {
    "resumed": ConductorRunOutcome.DELIVERED,
    "overridden": ConductorRunOutcome.DELIVERED,
    "auto_approved": ConductorRunOutcome.DELIVERED,
    "approved": ConductorRunOutcome.DELIVERED,
    "cancelled": ConductorRunOutcome.DECLINED,
    "canceled": ConductorRunOutcome.DECLINED,
    "rejected": ConductorRunOutcome.DECLINED,
    "failed": ConductorRunOutcome.DECLINED,
    "timed_out": ConductorRunOutcome.EXPIRED,
    "expired": ConductorRunOutcome.EXPIRED,
}


#: Terminal ``StageDispatchStatus`` values, as words. Matched on the
#: lowercased token so the driver keeps no import edge to the dispatcher
#: package — the same duck-typing discipline the card check uses.
_SETTLED_DISPATCH_STATUSES: frozenset[str] = frozenset(
    {"success", "failed", "degraded"}
)


def _dispatch_settled(report: Any) -> bool:
    """``True`` when this turn's dispatch already ran to completion.

    A fix-journey stage is dispatched by awaiting a subprocess, so its
    result is terminal the instant the turn report exists. A
    ``MergeCardDecision`` deliberately does NOT answer ``True`` here: its
    own outcomes are routed by
    :meth:`Supervisor._merge_card_turn_outcome` and a red-gate loop-back
    is a genuine wait, not a settled stage.
    """
    result = getattr(report, "dispatch_result", None)
    if result is None:
        return False
    if hasattr(result, "card_published"):
        return False
    status = getattr(result, "status", None)
    if status is None:
        return False
    token = str(getattr(status, "value", None) or status).strip().lower()
    return token in _SETTLED_DISPATCH_STATUSES


@dataclass(frozen=True, slots=True)
class _ReviewVerdict:
    """One settled review's reading, for the review-cycle rule.

    Attributes:
        no_progress: Whether this review showed no progress against the
            baseline it was compared to.
        baseline: The anchor set the NEXT review is compared against.
            Carried explicitly (rather than "the current review's anchors")
            because an unreadable review must not overwrite a good
            baseline with nothing.
        reason: The plain-language stop text, anchors named. Always
            populated — it costs nothing and it means the stop can never
            fire with an empty sentence.
    """

    no_progress: bool
    baseline: frozenset[str] | None
    reason: str


def _is_settled_review(report: Any) -> bool:
    """``True`` when this turn is a ``/task-review`` whose dispatch settled.

    The stage is read from the turn report's ``chosen_stage`` first and the
    dispatch result's own ``stage`` second — a fix-journey turn sets both,
    and reading either alone would make the rule depend on which collaborator
    happened to be doubled in a test. Compared against
    :class:`StageClass` rather than a re-spelled ``"task-review"`` literal:
    one statement of what the stage is called.
    """
    if not _dispatch_settled(report):
        return False
    stage = getattr(report, "chosen_stage", None)
    if stage is None:
        stage = getattr(getattr(report, "dispatch_result", None), "stage", None)
    token = str(getattr(stage, "value", None) or stage or "").strip().lower()
    return token == StageClass.TASK_REVIEW.value


def _review_progress_verdict(
    report: Any, baseline: frozenset[str] | None
) -> _ReviewVerdict | None:
    """Read one turn for the review-cycle no-progress rule (LI stage-2 §5).

    Returns ``None`` for any turn that is not a settled ``/task-review`` —
    the rule advances on reviews and nothing else.

    The rule, in the order the clauses are applied:

    1. **The review reported no readable findings block → NO PROGRESS
       (fail closed), if there is a baseline to fail against.** A leg that
       stops stating what it found cannot show a single previously-named
       anchor resolved, and reading its silence as a fix is exactly how a
       broken leg launders itself as progress. The baseline is KEPT rather
       than overwritten with nothing.
    2. **No baseline → reset, no accusation.** The first review of a
       journey, or one whose predecessor stated nothing readable, has
       nothing to be compared against. This is where the design's two
       clauses overlap ("a missing block on the current review is no
       progress" vs "no anchors on the previous review = no baseline,
       reset"), and the ranking is: a comparison needs two sides. With one
       side there is no verdict to reach, so the journey is not accused.
    3. **Current ⊇ previous → NO PROGRESS.** Every anchor the last review
       named is named again. New findings on top do not redeem it: nothing
       that was named got fixed.
    4. **Otherwise → progress.** At least one anchor is gone. The streak
       resets and the baseline advances.

    **An empty REPORTED anchor set is progress, and never a baseline.** A
    review that looked and found nothing is a clean review — the journey's
    success path, and the planner's CLEAN_REVIEW terminal one turn later.
    It resolves whatever the baseline held, so clause 3 cannot fire on it
    (the empty set is a superset of nothing but the empty set). It is then
    NOT carried forward as a baseline, because the empty set is a superset
    of nothing at all: carrying it would make the very next review — the
    one that finds something — read as "no progress".
    """
    if not _is_settled_review(report):
        return None

    dispatch = getattr(report, "dispatch_result", None)
    reported = bool(getattr(dispatch, "detection_findings_reported", False))
    anchors = derive_finding_anchors(getattr(dispatch, "detection_findings", ()))
    current = frozenset(anchors)
    # An empty set is a real answer but never an accusing baseline (above).
    next_baseline: frozenset[str] | None = current if current else None

    if not reported:
        if baseline is None:
            return _ReviewVerdict(
                no_progress=False,
                baseline=None,
                reason=(
                    "the review-cycle rule has no baseline: this /task-review "
                    "reported no readable findings block and there was "
                    "nothing to compare it against"
                ),
            )
        return _ReviewVerdict(
            no_progress=True,
            baseline=baseline,
            reason=(
                "the review-cycle nothing-changed stop: this /task-review "
                "reported no readable findings block, so it cannot show any "
                "of the previous review's findings resolved — read as NO "
                "PROGRESS (fail closed). Outstanding as of the last review "
                f"that spoke: {_name_anchors(baseline)}"
            ),
        )

    if baseline is None:
        return _ReviewVerdict(
            no_progress=False,
            baseline=next_baseline,
            reason=(
                "the review-cycle rule opened its baseline with "
                f"{_name_anchors(current)}"
            ),
        )

    if current >= baseline:
        added = current - baseline
        return _ReviewVerdict(
            no_progress=True,
            baseline=next_baseline,
            reason=(
                "the review-cycle nothing-changed stop: two consecutive "
                "/task-review legs reported the same findings and not one of "
                f"them was resolved — {_name_anchors(baseline)}"
                + (f" (plus new: {_name_anchors(added)})" if added else "")
            ),
        )

    return _ReviewVerdict(
        no_progress=False,
        baseline=next_baseline,
        reason=(
            "the review-cycle rule saw progress — resolved: "
            f"{_name_anchors(baseline - current)}"
        ),
    )


def _name_anchors(anchors: "frozenset[str] | None") -> str:
    """Anchors as a stable, readable list. Sorted so the text is diffable."""
    return ", ".join(sorted(anchors or ())) or "none"


def _classify_card_result(report: Any) -> ConductorRunOutcome:
    """Pick the honest WORD for a published card's ending.

    Stage 2 shakeout item 7. Stopping is right whatever the owner said —
    but the run report has to say WHICH thing happened. Before this the
    loop keyed only on ``card_published``, so a REJECTED or expired merge
    card was written up as ``DELIVERED``: the machine claiming a delivery
    the owner had refused.

    ``card_result`` carries whatever the publisher returned. Duck-typed
    against its ``value``/``name``/``str`` so the driver stays a domain
    module with no import edge to the gating package or the delivery leg.
    An unreadable verdict answers ``DELIVERED`` — the pre-Stage-2 word —
    because that is the only honest reading of "a card was published and
    we cannot tell what came back", and it is logged so the gap is visible
    rather than inferred.
    """
    decision = getattr(report, "dispatch_result", None)
    raw = getattr(decision, "card_result", None)
    if raw is None:
        return ConductorRunOutcome.DELIVERED
    token = str(getattr(raw, "value", None) or getattr(raw, "name", None) or raw)
    mapped = _CARD_VERDICT_WORDS.get(token.strip().lower())
    if mapped is None:
        logger.warning(
            "conductor: merge card returned %r, which is not a verdict this "
            "loop recognises — reporting 'delivered' and saying so here "
            "rather than inventing a word",
            token,
        )
        return ConductorRunOutcome.DELIVERED
    return mapped


def _is_red_gate_loop_back(report: Any) -> bool:
    """``True`` when this turn's dispatch result is a RED-GATE loop-back.

    Duck-typed against
    :class:`~forge.pipeline.merge_ready_checkpoint.MergeCardDecision`'s
    ``loops_back`` property — the same no-import-edge discipline
    :func:`_card_was_published` uses. Every other dispatch result (and
    every test double that is not a decision) answers ``False``, so the
    honest-word branch is reachable only from the one outcome that means
    it.
    """
    return getattr(getattr(report, "dispatch_result", None), "loops_back", False) is True


def _red_gate_reason(report: Any) -> str:
    """Plain-language reason naming the RED GATE, for the report and pack.

    Reads the decision's :class:`GatesReport` duck-typed. The failing gate
    NAMES are what a human needs first; the free-form detail is the
    fallback when the reader named none (an UNKNOWN gate set, for
    instance, which the checkpoint also treats as red).
    """
    decision = getattr(report, "dispatch_result", None)
    gates = getattr(decision, "gates", None)
    status = getattr(getattr(gates, "status", None), "value", None) or "red"
    failed = tuple(getattr(gates, "failed_gates", ()) or ())
    detail = getattr(gates, "detail", "") or ""
    if failed:
        named = ", ".join(str(gate) for gate in failed)
        return (
            f"the merge-ready checkpoint found the gates {status}: {named} — "
            "no merge card was published and the fix cycle has nowhere to "
            "loop back to"
        )
    return (
        f"the merge-ready checkpoint found the gates {status}"
        + (f" ({detail})" if detail else "")
        + " — no merge card was published and the fix cycle has nowhere to "
        "loop back to"
    )


def _card_was_published(report: Any) -> bool:
    """``True`` when this turn's dispatch result reports a published card.

    Duck-typed against
    :class:`~forge.pipeline.merge_ready_checkpoint.MergeCardDecision`'s
    ``card_published`` field so this domain module keeps no import edge
    to the delivery leg. Any other dispatch result answers ``False``,
    which is what every pre-existing gate implementation returns — the
    backwards-compat rail again.
    """
    return getattr(getattr(report, "dispatch_result", None), "card_published", False) is True


async def _maybe_await(value: Any) -> Any:
    """Await ``value`` when awaitable, else return it unchanged."""
    if inspect.isawaitable(value):
        return await value
    return value


async def drive_fix_journey(
    build_id: str, deps: ConductorDriverDeps
) -> ConductorRunReport:
    """Convenience entry point — one journey, one call."""
    return await ConductorTurnLoop(deps).drive(build_id)

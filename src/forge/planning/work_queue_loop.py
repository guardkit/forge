"""The take-next loop: what turns a queued sentence into a planning run.

Public surface
==============

- :class:`WorkQueueLoop` — one object with three things it does: a ``tick``
  every ten seconds, a ``stale_tick`` once a week, and a ``run`` that drives
  both forever.
- :func:`count_in_flight` — how much work the factory has running right now.
- :func:`paused_repositories` — the repositories with a card waiting on Rich.
- :class:`Admission` — the four facts an admission hands to the run maker,
  plus the three that keep the Slack thread alive.

What the loop does, in order, every tick
----------------------------------------

0. **Picks up what was dropped.** A row marked admitted whose planning run
   does not exist — the forge stopped between the two, or the notification
   after the admission failed — is put back in the queue with a line in the
   log, at the start of every tick and once more when the loop starts. Left
   alone it would sit admitted for ever and the queue behind it would never
   move.
1. **Closes what has finished.** Every admitted row's planning run is read;
   a run that ended well closes the row done, a run that ended badly closes
   it blocked with the reason. There is no callback on the planning store's
   terminal transition to hook, so the loop polls — the spec allows this and
   asks that it be said out loud, and this is it being said.
2. **Asks about a broken chain.** A row told to wait for another one whose
   antecedent failed or was withdrawn is never taken on its own. The loop
   asks once — "#14 failed and #12 was waiting on it — hold or go?" — and
   then holds until someone types ``#12 next`` (go) or ``drop 12``.
3. **Takes the next one.** If fewer pieces of work are in flight than the cap
   allows, the lowest-ranked eligible row is admitted and its planning run is
   created with the row's ORIGINAL correlation id. Nothing is re-published and
   no new id is minted, so every downstream receipt and the Slack thread keep
   working.
4. **Takes out what can never be started.** A repair the admission refuses
   for a reason that will not change — a repository the configuration does
   not know, a budget profile with no cap, a row that names no build — is
   closed BLOCKED with that refusal as its reason and said once in the
   channel — the channel gets the refusal's FIRST SENTENCE, the whole of it
   stays on the row. A transport failure goes back in the queue for the next
   tick as before.
5. **Settles a repair whose build was already written.** "Another build for
   this is already in flight" used to mean "try again next tick", for ever,
   even when the build in flight was this row's OWN — written by an earlier
   tick whose publish failed after it. The loop now reads that build and
   answers in its terms: written but never dispatched, say its queued event
   again and leave the row admitted; running, leave the row admitted; over,
   close the row to match.

Beside every admission it works out which row a class order would have taken
— fixes first, then anything whose repository has a card waiting on Rich, then
features, then questions — writes both picks in the log and against the
admitted row, and when the two differ says so once in the channel. It never
acts on that pick. That is what "shadow" means: the order is on trial, and
this stage only lets it talk.

Design notes
------------

Everything the loop touches from the outside world arrives as a callable, so
a test drives it with a fake clock and a recorder and never opens a socket:
the clock, the sleep, the in-flight count, the planning-run reader, the
paused-repository reader, the run maker, and the notifier.

References
----------
- ``docs/work-queue-spec-2026-09-05.md`` contracts 6, 7, 8 and 9.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping, Sequence

from forge.cli.status import _TERMINAL_STATES as _BUILD_TERMINAL_STATES
from forge.lifecycle.persistence import ACTIVE_STATES as _BUILD_ACTIVE_STATES
from forge.pipeline.fix_admission import (
    DUPLICATE_REASON,
    REPUBLISHED_ACTION,
    FixAdmissionRefused,
)
from forge.planning.states import PlanningState
from forge.planning.work_queue_commands import (
    age_phrase,
    notifier_takes_parent_request_id,
)
from forge.planning.work_queue_store import WorkQueueStore

logger = logging.getLogger(__name__)


#: How often the loop looks at the queue (contract 6).
TAKE_NEXT_INTERVAL_SECONDS: float = 10.0

#: How often the loop looks for sentences that have been waiting (contract 8
#: as the coaches read it on 2026-09-05): every day, and the first look one
#: day after the forge starts rather than a week after. A forge restarted
#: every few days used to reach the end of the week and start counting again,
#: so nobody was ever asked.
STALE_TICK_INTERVAL_SECONDS: float = 24 * 60 * 60.0

#: How long the forge leaves a row alone after asking about it before asking
#: again — the week the message itself promises.
STALE_REPING_DAYS: int = 7

#: Who the loop is, on the events rows it writes.
LOOP_ACTOR: str = "forge-work-queue"

#: Planning states that mean the run finished and the work happened.
PLANNING_SUCCESS_STATES: frozenset[str] = frozenset(
    {PlanningState.PLANNED_HANDOFF.value, PlanningState.BUILD_QUEUED.value}
)

#: Planning states that mean the run is over and the work did not happen.
PLANNING_FAILURE_STATES: frozenset[str] = frozenset(
    {
        PlanningState.FAILED.value,
        PlanningState.CANCELLED.value,
        PlanningState.TIMED_OUT.value,
    }
)

#: Every terminal planning state — the two sets above, together. This is the
#: same list the planning run store uses to decide a run is over.
PLANNING_TERMINAL_STATES: frozenset[str] = (
    PLANNING_SUCCESS_STATES | PLANNING_FAILURE_STATES
)

#: Terminal build states, reused verbatim from ``forge status``'s
#: ``_all_terminal`` (``cli/status.py``) as the spec's seam table asks.
BUILD_TERMINAL_STATES: frozenset[str] = frozenset(
    state.value for state in _BUILD_TERMINAL_STATES
)

#: The kind of row that is a repair rather than a piece of new work. A repair
#: does not start a planning run: it opens a fix journey (a mode-c build).
FIX_KIND: str = "fix"

#: The build state that means the row was written and nothing has picked it
#: up yet — the state a build sits in when its queued event never went out.
BUILD_WRITTEN_STATE: str = "QUEUED"

#: The build state that means a repair finished and the work happened.
BUILD_SUCCESS_STATES: frozenset[str] = frozenset({"COMPLETE"})

#: The build states that mean a repair is over and the work did not happen.
BUILD_FAILURE_STATES: frozenset[str] = BUILD_TERMINAL_STATES - BUILD_SUCCESS_STATES

#: The states that mean a build is running NOW — the forge's own list
#: (``lifecycle/persistence.py``): queued, preparing, running, paused, finalising.
#: This is what the in-flight count uses, NOT "everything that is not terminal":
#: on the live ledger of 2026-09-05 twenty-eight builds sat INTERRUPTED (dead until
#: boot re-cards them, at which point they become PAUSED and count), and "not
#: terminal" counted every one of them, so the cap of one was full for ever and
#: the first real sentence through the queue was never admitted. Found by Rich at
#: his Slack surface; the coaches had tested on fresh databases.
BUILD_ACTIVE_STATES: frozenset[str] = frozenset(
    state.value for state in _BUILD_ACTIVE_STATES
)

#: The planning states that mean a run is happening now.
PLANNING_ACTIVE_STATES: frozenset[str] = frozenset(
    {
        PlanningState.QUEUED.value,
        PlanningState.RUNNING.value,
        PlanningState.PAUSED.value,
        PlanningState.FEATURE_SPEC.value,
        PlanningState.FEATURE_PLAN.value,
    }
)

#: The events-row action written when a repair row is refused for good and
#: leaves the queue (conductor rewire, coach item 2).
BLOCKED_ACTION: str = "blocked"

#: How a run that ended badly is described in the row's closing reason.
_FAILURE_WORDS: Mapping[str, str] = {
    PlanningState.FAILED.value: "the planning run failed",
    PlanningState.CANCELLED.value: "the planning run was cancelled",
    PlanningState.TIMED_OUT.value: "the planning run ran out of time",
}

#: The same, for a repair — whose work is a BUILD, not a planning run. Kept
#: apart from the planning words because the two vocabularies spell some
#: states the same (``FAILED``, ``CANCELLED``) and mean different things by
#: them, and a row's closing reason should say which one it was.
_FIX_FAILURE_WORDS: Mapping[str, str] = {
    "FAILED": "the fix journey failed",
    "CANCELLED": "the fix journey was cancelled",
    "SKIPPED": "the fix journey never ran",
}


@dataclass(frozen=True, slots=True)
class Admission:
    """Everything the run maker needs to start the run this row asked for."""

    queue_id: int
    correlation_id: str
    request_text: str
    target_repo: str | None
    originating_user: str
    parent_request_id: str | None = None
    originating_adapter: str | None = None
    triggered_by: str = "jarvis"
    #: ``feature``, ``fix`` or ``question`` — which of the two starters this
    #: admission goes to. A repair goes to the fix journey, never to a
    #: planning run.
    kind: str = "feature"


@dataclass(frozen=True, slots=True)
class Pick:
    """One row the loop considered taking, and why it would have taken it."""

    queue_id: int
    kind: str
    target_repo: str | None
    reason: str

    def label(self) -> str:
        """``fix · api_test``, or just ``fix`` when no repository is named."""
        if self.target_repo:
            return f"{self.kind} · {self.target_repo}"
        return self.kind


# ---------------------------------------------------------------------------
# Reading the estate: how busy is the factory, and what is waiting on Rich
# ---------------------------------------------------------------------------


def _count_not_in(
    connection: sqlite3.Connection, table: str, column: str, values: Sequence[str]
) -> int:
    """Count rows whose ``column`` is outside ``values``; 0 if no such table."""
    placeholders = ", ".join("?" for _ in values)
    try:
        row = connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {column} NOT IN ({placeholders})",
            tuple(values),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return 0
        raise
    return int(row[0]) if row else 0


def _count_in(connection: sqlite3.Connection, table: str, column: str, values: list[str]) -> int:
    """Rows of ``table`` whose ``column`` is one of ``values`` (0 when the table is absent)."""
    placeholders = ",".join("?" for _ in values)
    try:
        row = connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({placeholders})", values
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return 0
        raise
    return int(row[0]) if row else 0


def count_in_flight(connection: sqlite3.Connection) -> int:
    """How many pieces of work the factory has running right now.

    Planning runs in an active state plus builds in an active state — the
    forge's own lists of "running now". Rows that are neither running nor
    terminal (a build marked INTERRUPTED by boot recovery, waiting to be
    re-carded) are not in flight: nothing is happening for them, and when
    boot recovery re-cards one it becomes PAUSED and counts from then on.
    """
    return _count_in(
        connection, "planning_runs", "state", sorted(PLANNING_ACTIVE_STATES)
    ) + _count_in(connection, "builds", "status", sorted(BUILD_ACTIVE_STATES))


def paused_repositories(connection: sqlite3.Connection) -> set[str]:
    """The repositories that have a planning run paused on one of Rich's cards."""
    try:
        rows = connection.execute(
            """
            SELECT DISTINCT target_repo FROM planning_runs
            WHERE state = ? AND target_repo IS NOT NULL
            """,
            (PlanningState.PAUSED.value,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return set()
        raise
    return {str(row[0]) for row in rows if row[0]}


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


class WorkQueueLoop:
    """Admits one queued sentence at a time, and says what it is doing."""

    def __init__(
        self,
        store: WorkQueueStore,
        *,
        count_in_flight: Callable[[], int],
        planning_run: Callable[[str], Mapping[str, Any] | sqlite3.Row | None],
        paused_repositories: Callable[[], set[str]],
        start_run: Callable[[Admission], Awaitable[None]],
        notify: Callable[..., Awaitable[None]],
        max_in_flight: int = 1,
        stale_after_days: int = 7,
        clock: Callable[[], datetime] | None = None,
        start_fix: Callable[[Admission], Awaitable[None]] | None = None,
        fix_build: Callable[[str], Mapping[str, Any] | sqlite3.Row | None]
        | None = None,
        republish_build: Callable[[Any], Awaitable[None]] | None = None,
        admit_fix_rows: bool = False,
    ) -> None:
        """Wire the loop to the store and to the rest of the estate.

        Parameters
        ----------
        count_in_flight:
            How many pieces of work are running; the loop admits nothing while
            this is at or above ``max_in_flight``.
        planning_run:
            Reads one planning run by correlation id — the loop polls it to
            know when an admitted row has finished.
        paused_repositories:
            The repositories with a card waiting on Rich; the shadow order
            puts their rows second.
        start_run:
            Creates and starts the planning run for an admitted row. This is
            the same in-process step the intake took before the queue existed.
        notify:
            Says one sentence in Slack: ``notify(correlation_id, message)``,
            optionally with ``parent_request_id=`` for the thread. Whether
            this one takes that argument is read from it here, once, and
            remembered.
        start_fix:
            Opens the fix journey for an admitted repair row — the mode-c
            build, not a planning run. ``None`` means no repair can be
            started whatever ``admit_fix_rows`` says.
        fix_build:
            Reads one BUILD by correlation id, the way ``planning_run``
            reads one planning run. It is how a repair row learns its
            journey has finished. ``None`` leaves repair rows uncollected:
            they stay ADMITTED until something else closes them, which is
            the honest degrade when nothing can read a build.
        republish_build:
            Says one build's queued event again, given the build row. Used for
            exactly one thing: a build that was written and whose publish then
            failed, so the pipeline was never told about work that exists.
            ``None`` means nothing here can re-announce a build, and such a row
            goes back in the queue as it used to.
        admit_fix_rows:
            Whether a repair row may be STARTED (conductor rewire rule 4).
            False — the default and the shipped posture — leaves repair
            rows QUEUED where they can be read and listed, and the class
            order's "next I'd pick" line may still name one.
        """
        self._store = store
        self._count_in_flight = count_in_flight
        self._planning_run = planning_run
        self._paused_repositories = paused_repositories
        self._start_run = start_run
        self._notify = notify
        self._max_in_flight = max_in_flight
        self._stale_after_days = stale_after_days
        self._notify_takes_thread = notifier_takes_parent_request_id(notify)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._start_fix = start_fix
        self._fix_build = fix_build
        self._republish_build = republish_build
        self._admit_fix_rows = admit_fix_rows
        # What the loop has already said, so it does not say it every ten
        # seconds: the number it was last holding at, and the set of repair
        # rows it last reported as waiting.
        self._last_hold: int | None = None
        self._waiting_said_for: frozenset[int] | None = None

    # -- one pass --------------------------------------------------------

    async def tick(self) -> None:
        """Pick up what was dropped, close what finished, take the next one."""
        self.recover_admitted()
        self.close_finished()
        await self.ask_hold_or_go()
        await self.take_next()

    # -- 0. picking up what was dropped ----------------------------------

    def recover_admitted(self) -> list[int]:
        """Put back every admitted row that has no planning run behind it.

        Two ways a row gets stuck this way: the forge stopped in the moment
        between marking the row admitted and creating its run, or something
        in the reply path threw where the admission could still see it. Either
        way nothing will ever finish the row, and with a cap of one in flight
        the whole queue stops behind it. Returns the ids put back.
        """
        recovered: list[int] = []
        for row in self._store.list_open():
            if str(row["status"]) != "ADMITTED":
                continue
            reader = self._reader_for(row)
            if reader is None:
                # Nothing here can read what this row started, so nothing
                # here may decide it was never started either.
                continue
            if reader(str(row["correlation_id"])) is not None:
                continue
            queue_id = int(row["id"])
            if self._store.requeue(
                queue_id,
                actor_identity=LOOP_ACTOR,
                reason="it was admitted but no planning run was ever created",
            ):
                logger.warning(
                    "work queue: #%d was admitted but has no planning run "
                    "(correlation id %s); putting it back in the queue",
                    queue_id,
                    str(row["correlation_id"]),
                )
                recovered.append(queue_id)
        return recovered

    def _reader_for(
        self, row: sqlite3.Row
    ) -> Callable[[str], Mapping[str, Any] | sqlite3.Row | None] | None:
        """Whatever reads the work this row started — a run, or a build."""
        if str(row["kind"]) == FIX_KIND:
            return self._fix_build
        return self._planning_run

    # -- 1. closing ------------------------------------------------------

    def close_finished(self) -> int:
        """Close every admitted row whose planning run has ended. Count closed.

        Polled rather than hooked: the planning run store has no callback on a
        terminal transition, so there is nothing to hang one off. Ten seconds
        late is not late for a queue whose cap is one.
        """
        closed = 0
        for row in self._store.list_open():
            if str(row["status"]) != "ADMITTED":
                continue
            reader = self._reader_for(row)
            if reader is None:
                continue
            run = reader(str(row["correlation_id"]))
            if run is None:
                continue
            is_fix = str(row["kind"]) == FIX_KIND
            # A planning run keeps its state in ``state`` and a build keeps
            # its in ``status``; the words themselves differ too. Read the
            # one this row actually started.
            state = str(run["status"] if is_fix else run["state"])
            success = BUILD_SUCCESS_STATES if is_fix else PLANNING_SUCCESS_STATES
            failure = BUILD_FAILURE_STATES if is_fix else PLANNING_FAILURE_STATES
            queue_id = int(row["id"])
            if state in success:
                if self._store.close(
                    queue_id, status="DONE", actor_identity=LOOP_ACTOR
                ):
                    closed += 1
                    logger.info("work queue: #%d is done", queue_id)
            elif state in failure:
                reason = self._failure_reason(run, state, is_fix=is_fix)
                if self._store.close(
                    queue_id,
                    status="BLOCKED",
                    actor_identity=LOOP_ACTOR,
                    reason=reason,
                ):
                    closed += 1
                    logger.warning(
                        "work queue: #%d is blocked — %s", queue_id, reason
                    )
        return closed

    @staticmethod
    def _failure_reason(run: Any, state: str, *, is_fix: bool = False) -> str:
        """Why the row is blocked, in words a person reads."""
        error: Any = None
        try:
            error = run["error"]
        except (KeyError, IndexError, TypeError):
            error = None
        if error:
            return str(error)
        if is_fix:
            return _FIX_FAILURE_WORDS.get(
                state, f"the fix journey ended {state.lower()}"
            )
        return _FAILURE_WORDS.get(state, f"the planning run ended {state.lower()}")

    # -- 2. the broken chain ---------------------------------------------

    async def ask_hold_or_go(self) -> None:
        """Ask once about every row whose antecedent failed or was withdrawn."""
        for row in self._store.list_open():
            after_id = row["after_id"]
            if after_id is None:
                continue
            antecedent = self._store.get(int(after_id))
            if antecedent is None:
                continue
            if str(antecedent["status"]) not in ("BLOCKED", "WITHDRAWN"):
                continue
            queue_id = int(row["id"])
            if self._store.has_event(queue_id, "hold_or_go"):
                continue
            self._store.record_event(
                queue_id=queue_id,
                action="hold_or_go",
                actor_identity=LOOP_ACTOR,
                details={"after_id": int(after_id)},
            )
            message = (
                f"#{int(after_id)} failed and #{queue_id} was waiting on it "
                f"— hold or go?"
            )
            logger.info("work queue: %s", message)
            await self._say(row, message)

    # -- 3. taking the next one ------------------------------------------

    async def take_next(self) -> int | None:
        """Admit one row if the factory has room. Return the id, or None."""
        in_flight = self._count_in_flight()
        if in_flight >= self._max_in_flight:
            # Said once per change of the number, at INFO, so a queue that never
            # admits anything is visible in the log rather than silent (2026-09-05).
            if self._last_hold != in_flight:
                self._last_hold = in_flight
                logger.info(
                    "work queue: holding — %d piece(s) of work in flight against a cap of %d",
                    in_flight,
                    self._max_in_flight,
                )
            return None
        self._last_hold = None

        open_rows = self._store.list_open()
        eligible = [row for row in open_rows if self._is_eligible(row)]
        if not eligible:
            return None

        # THE FIX BRANCH IS SHUT BY DEFAULT (conductor rewire rule 4). A
        # repair row stays eligible — so the class order can still name it,
        # and the "next I'd pick" line still tells the truth about what the
        # factory would do — but it is not taken until the owner has said
        # repairs may start.
        takeable = [row for row in eligible if self._is_takeable(row)]
        if not takeable:
            self._say_nothing_may_start(open_rows, eligible)
            return None
        self._waiting_said_for = None

        taken = takeable[0]  # list_open is already lowest rank first, then oldest
        taken_id = int(taken["id"])
        shadow = self._class_pick(eligible)
        fifo = self._as_pick(taken, "it has been waiting longest")

        logger.info(
            "work queue: taking #%d (%s); the class order would take #%d (%s), "
            "because %s",
            fifo.queue_id,
            fifo.label(),
            shadow.queue_id,
            shadow.label(),
            shadow.reason,
        )

        if not self._store.admit(taken_id, actor_identity=LOOP_ACTOR):
            # Someone dropped it, or another loop got there first.
            return None

        self._store.record_event(
            queue_id=taken_id,
            action="shadow_pick",
            actor_identity=LOOP_ACTOR,
            details={
                "taken": fifo.queue_id,
                "shadow": shadow.queue_id,
                "shadow_kind": shadow.kind,
                "shadow_target_repo": shadow.target_repo,
                "reason": shadow.reason,
            },
        )

        if len(open_rows) >= 2 and shadow.queue_id != fifo.queue_id:
            await self._say(taken, shadow_line(shadow, fifo.queue_id))

        admission = self._admission_for(taken)
        starter = self._starter_for(taken)
        if starter is None:
            logger.error(
                "work queue: #%d is a repair and nothing is wired to open a "
                "fix journey; putting it back in the queue",
                taken_id,
            )
            self._store.requeue(
                taken_id,
                actor_identity=LOOP_ACTOR,
                reason="no fix journey opener is wired",
            )
            return None
        try:
            await starter(admission)
        except FixAdmissionRefused as refusal:
            if refusal.reason == DUPLICATE_REASON and await self._settle_duplicate(
                taken
            ):
                return None
            if refusal.permanent:
                if self._close_refused(taken, refusal):
                    await self._say(
                        taken, refusal_line(taken_id, refusal.message)
                    )
                return None
            logger.warning(
                "work queue: #%d could not be started this time (%s); putting "
                "it back in the queue for the next tick",
                taken_id,
                refusal.message,
            )
            self._store.requeue(
                taken_id, actor_identity=LOOP_ACTOR, reason=refusal.message
            )
            return None
        except Exception as exc:  # noqa: BLE001 — never lose the sentence
            logger.exception(
                "work queue: starting the planning run for #%d failed (%s); "
                "putting it back in the queue for the next tick",
                taken_id,
                exc,
            )
            self._store.requeue(
                taken_id, actor_identity=LOOP_ACTOR, reason=str(exc)
            )
            return None
        return taken_id

    def _say_nothing_may_start(
        self, open_rows: Sequence[sqlite3.Row], eligible: Sequence[sqlite3.Row]
    ) -> None:
        """Say once — not every ten seconds — that nothing here may be started.

        The queue holds only repair rows and repairs are shut, so this tick
        starts nothing. Said at INFO once per CHANGE of the repair rows that
        are open: a queue left alone for a day would otherwise write the same
        line eight and a half thousand times, and a line nobody can read is
        the same as no line at all.
        """
        waiting_now = frozenset(
            int(row["id"]) for row in open_rows if str(row["kind"]) == FIX_KIND
        )
        if waiting_now == self._waiting_said_for:
            return
        self._waiting_said_for = waiting_now
        waiting = self._class_pick(eligible)
        logger.info(
            "work queue: #%d (%s) is waiting and nothing may be started "
            "— repairs are not admitted yet (conductor.admit_fix_rows is "
            "off)",
            waiting.queue_id,
            waiting.label(),
        )

    async def _settle_duplicate(self, row: sqlite3.Row) -> bool:
        """Answer a repair whose build already exists. True when it is settled.

        "Another build for this is already in flight" is usually right and
        clears by itself, so the row goes back in the queue. It is wrong for
        ever in one case: the build in flight is this row's OWN, written by an
        earlier tick whose publish failed after the write. Nothing will ever
        finish that build, nothing will ever stop refusing this row, and the
        queue behind it never moves.

        So the loop reads the build under this row's correlation id and
        answers in the build's own terms:

        - **written, never dispatched** — say its queued event again and leave
          the row admitted;
        - **running** — the work is happening; leave the row admitted, and the
          closing pass will collect it when it ends;
        - **over** — close the row DONE or BLOCKED to match, the same way the
          closing pass would have.

        Returns False when this is somebody else's build after all — the row
        is not a repair, nothing here reads builds, or there is no build under
        this correlation id — and putting the row back for the next tick is
        right.
        """
        if str(row["kind"]) != FIX_KIND or self._fix_build is None:
            return False
        queue_id = int(row["id"])
        correlation_id = str(row["correlation_id"])
        build = self._fix_build(correlation_id)
        if build is None:
            return False
        status = str(_build_field(build, "status") or "")
        name = str(_build_field(build, "build_id") or correlation_id)

        if status == BUILD_WRITTEN_STATE:
            return await self._say_the_build_again(row, build, name)
        if status in BUILD_ACTIVE_STATES:
            logger.info(
                "work queue: #%d already has a build under way (%s, %s); "
                "leaving it admitted",
                queue_id,
                name,
                status,
            )
            return True
        if status in BUILD_TERMINAL_STATES:
            if status in BUILD_SUCCESS_STATES:
                self._store.close(
                    queue_id, status="DONE", actor_identity=LOOP_ACTOR
                )
                logger.info(
                    "work queue: #%d's build (%s) had already finished; "
                    "closing the row done",
                    queue_id,
                    name,
                )
            else:
                reason = self._failure_reason(build, status, is_fix=True)
                self._store.close(
                    queue_id,
                    status="BLOCKED",
                    actor_identity=LOOP_ACTOR,
                    reason=reason,
                )
                logger.warning(
                    "work queue: #%d's build (%s) was already over — %s",
                    queue_id,
                    name,
                    reason,
                )
            return True

        logger.warning(
            "work queue: #%d's build (%s) is in a state this loop does not "
            "know (%s); putting the row back for the next tick",
            queue_id,
            name,
            status,
        )
        return False

    async def _say_the_build_again(
        self, row: sqlite3.Row, build: Any, name: str
    ) -> bool:
        """Re-announce a build that was written and never dispatched.

        The same event, rebuilt from the build row: same feature, same
        correlation id, same subject. The build row already exists, so a
        second hearing finds it rather than starting another one. The row
        stays admitted and an events row says the announcement was repeated.
        """
        queue_id = int(row["id"])
        if self._republish_build is None:
            logger.warning(
                "work queue: #%d's build (%s) was written and never "
                "announced, and nothing here can say its queued event again; "
                "putting the row back for the next tick",
                queue_id,
                name,
            )
            return False
        try:
            await self._republish_build(build)
        except Exception as exc:  # noqa: BLE001 — the broker may still be down
            logger.warning(
                "work queue: could not say #%d's build (%s) again (%s: %s); "
                "putting the row back for the next tick",
                queue_id,
                name,
                type(exc).__name__,
                exc,
            )
            return False
        try:
            self._store.record_event(
                queue_id=queue_id,
                action=REPUBLISHED_ACTION,
                actor_identity=LOOP_ACTOR,
                details={
                    "build_id": name,
                    "correlation_id": str(row["correlation_id"]),
                    "status": BUILD_WRITTEN_STATE,
                },
            )
        except Exception as exc:  # noqa: BLE001 — a note never costs the work
            logger.warning(
                "work queue: could not write the republished note for #%d "
                "(%s: %s)",
                queue_id,
                type(exc).__name__,
                exc,
            )
        logger.info(
            "work queue: #%d's build (%s) was written and never announced; "
            "said its queued event again and leaving the row admitted",
            queue_id,
            name,
        )
        return True

    def _close_refused(
        self, row: sqlite3.Row, refusal: FixAdmissionRefused
    ) -> bool:
        """Take a row out of the queue that will be refused the same way for ever.

        An unknown repository, a budget profile with no cap, a row that names
        no build: offering one of those to the admission again on the next
        tick, and every tick after it, changes nothing and hides everything
        behind it. The row closes BLOCKED with the refusal's own sentence as
        its reason.

        Returns whether the row was actually closed here: a row someone
        dropped in the same moment is already out of the queue, and nothing
        more should be said about it.
        """
        queue_id = int(row["id"])
        logger.warning(
            "work queue: #%d cannot be started (%s: %s); closing it and "
            "taking it out of the queue",
            queue_id,
            refusal.reason,
            refusal.message,
        )
        if not self._store.close(
            queue_id,
            status="BLOCKED",
            actor_identity=LOOP_ACTOR,
            reason=refusal.message,
        ):
            return False
        try:
            self._store.record_event(
                queue_id=queue_id,
                action=BLOCKED_ACTION,
                actor_identity=LOOP_ACTOR,
                details={
                    "reason": refusal.reason,
                    "message": refusal.message,
                    "permanent": True,
                },
            )
        except Exception as exc:  # noqa: BLE001 — a note never costs the close
            logger.warning(
                "work queue: could not write the blocked note for #%d (%s: %s)",
                queue_id,
                type(exc).__name__,
                exc,
            )
        return True

    def _is_takeable(self, row: sqlite3.Row) -> bool:
        """True when this row may be STARTED, not merely chosen.

        The one rule that separates the two: a repair is started only when
        ``conductor.admit_fix_rows`` is on.
        """
        if str(row["kind"]) != FIX_KIND:
            return True
        return self._admit_fix_rows

    def _starter_for(
        self, row: sqlite3.Row
    ) -> Callable[[Admission], Awaitable[None]] | None:
        """Which opener this row's work goes to — a planning run, or a journey."""
        if str(row["kind"]) == FIX_KIND:
            return self._start_fix
        return self._start_run

    def _is_eligible(self, row: sqlite3.Row) -> bool:
        """True when this row may be taken right now."""
        if str(row["status"]) != "QUEUED":
            return False
        after_id = row["after_id"]
        if after_id is None:
            return True
        antecedent = self._store.get(int(after_id))
        if antecedent is None:
            return True  # the row it named is gone; nothing left to wait for
        status = str(antecedent["status"])
        if status == "DONE":
            return True
        if status in ("BLOCKED", "WITHDRAWN"):
            return self._go_was_given(int(row["id"]))
        return False

    def _go_was_given(self, queue_id: int) -> bool:
        """True when someone typed ``#12 next`` after being asked hold or go."""
        asked = self._store.latest_event_at(queue_id, "hold_or_go")
        if asked is None:
            return False
        promoted = self._store.latest_event_at(queue_id, "promote")
        if promoted is None:
            return False
        # Both are timestamps, so compare them as timestamps. As text,
        # "2026-09-05T10:30:00+02:00" sorts after "2026-09-05T09:00:00+00:00"
        # although it happened half an hour earlier.
        return _parse(promoted) >= _parse(asked)

    def _class_pick(self, eligible: Sequence[sqlite3.Row]) -> Pick:
        """Which row the class order would take — fixes, cards, features, questions."""
        paused = self._paused_repositories()

        def rank(row: sqlite3.Row) -> int:
            if str(row["kind"]) == "fix":
                return 0
            repo = row["target_repo"]
            if repo and str(repo) in paused:
                return 1
            if str(row["kind"]) == "feature":
                return 2
            return 3

        reasons = {
            0: "fixes go first",
            1: "its repository has a card waiting on you",
            2: "features come before questions",
            3: "questions come last, and it is all that is waiting",
        }
        # The index breaks ties, so equals stay in first-in-first-out order.
        best_row = min(
            enumerate(eligible), key=lambda pair: (rank(pair[1]), pair[0])
        )[1]
        return self._as_pick(best_row, reasons[rank(best_row)])

    @staticmethod
    def _as_pick(row: sqlite3.Row, reason: str) -> Pick:
        repo = row["target_repo"]
        return Pick(
            queue_id=int(row["id"]),
            kind=str(row["kind"]),
            target_repo=str(repo) if repo else None,
            reason=reason,
        )

    def _admission_for(self, row: sqlite3.Row) -> Admission:
        """The row's own facts, exactly as they arrived from Slack."""
        origin = self._store.origin_details(int(row["id"]))
        repo = row["target_repo"]
        return Admission(
            queue_id=int(row["id"]),
            correlation_id=str(row["correlation_id"]),
            request_text=str(row["sentence"]),
            target_repo=str(repo) if repo else None,
            originating_user=str(row["originating_user"]),
            parent_request_id=origin.get("parent_request_id"),
            originating_adapter=origin.get("originating_adapter"),
            triggered_by=str(origin.get("triggered_by") or "jarvis"),
            kind=str(row["kind"]),
        )

    # -- staleness -------------------------------------------------------

    async def stale_tick(self) -> list[int]:
        """Ask, in one message, about every sentence that has waited too long.

        Run daily. A row joins the message when it has been waiting longer
        than the threshold AND has not been asked about in the last week —
        which is what the message itself promises.
        """
        now = self._clock()
        threshold = timedelta(days=self._stale_after_days)
        quiet_week = timedelta(days=STALE_REPING_DAYS)
        stale = [
            row
            for row in self._store.list_open()
            if str(row["status"]) == "QUEUED"
            and (now - self._waiting_since(row)) >= threshold
            and not self._asked_lately(row, now, quiet_week)
        ]
        if not stale:
            return []

        lines = ["These have been waiting a while:"]
        for row in stale:
            repo = row["target_repo"]
            label = f"{str(row['kind'])} · {repo}" if repo else str(row["kind"])
            lines.append(
                f"#{int(row['id'])} ({label}) — asked for "
                f"{age_phrase(str(row['queued_at']), now)}"
            )
        lines.append(
            'Reply "keep <n>" or "drop <n>", or ignore me and '
            "I'll ask again next week."
        )
        await self._say(stale[0], "\n".join(lines))

        stamped = now.isoformat()
        for row in stale:
            self._store.mark_stale_pinged(int(row["id"]), at=stamped)
        return [int(row["id"]) for row in stale]

    def _waiting_since(self, row: sqlite3.Row) -> datetime:
        """When this row's waiting clock last started.

        The clock starts when the sentence was filed, and starts again when
        someone says ``keep`` — that is what keeping it means. Being asked
        about does not restart it; it only buys the row a quiet week, which
        :meth:`_asked_lately` handles.
        """
        moments = [str(row["queued_at"])]
        kept = self._store.latest_event_at(int(row["id"]), "keep")
        if kept:
            moments.append(kept)
        return max(_parse(moment) for moment in moments)

    @staticmethod
    def _asked_lately(row: sqlite3.Row, now: datetime, quiet: timedelta) -> bool:
        """True when this row was already asked about inside the quiet week."""
        asked = row["stale_pinged_at"]
        if not asked:
            return False
        return (now - _parse(str(asked))) < quiet

    # -- saying things ---------------------------------------------------

    async def _say(self, row: sqlite3.Row, message: str) -> None:
        """One sentence in the thread the row's sentence arrived in.

        Nothing that happens in here reaches the caller. Whether the notifier
        takes the thread was settled when the loop was wired up, so there is
        no call made to find out; and anything the notifier throws is logged
        and dropped, because the work starting must never depend on Slack
        being reachable.
        """
        correlation_id = str(row["correlation_id"])
        try:
            if self._notify_takes_thread:
                origin = self._store.origin_details(int(row["id"]))
                await self._notify(
                    correlation_id,
                    message,
                    parent_request_id=origin.get("parent_request_id"),
                )
            else:
                await self._notify(correlation_id, message)
        except Exception as exc:  # noqa: BLE001 — a message never stops the queue
            logger.warning(
                "work queue: could not say %r about #%s (%s)",
                message,
                row["id"],
                exc,
            )

    # -- forever ---------------------------------------------------------

    async def run(
        self,
        *,
        interval_seconds: float = TAKE_NEXT_INTERVAL_SECONDS,
        stale_interval_seconds: float = STALE_TICK_INTERVAL_SECONDS,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        iterations: int | None = None,
    ) -> None:
        """Tick every ten seconds forever, and look for stale rows daily.

        ``sleep`` and the clock are injectable so a test drives real time
        without waiting for it; ``iterations`` bounds the loop for the same
        reason. A tick that raises is logged and the loop carries on — a
        single bad row must never stop the queue.
        """
        sleeper = sleep or asyncio.sleep
        # A forge that stopped between "admitted" and "the run has started"
        # comes back with a row nothing will ever finish. Put those back
        # before the first tick, not only on the tick after it.
        self.recover_admitted()
        next_stale = self._clock() + timedelta(seconds=stale_interval_seconds)
        done = 0
        while iterations is None or done < iterations:
            try:
                await self.tick()
                if self._clock() >= next_stale:
                    await self.stale_tick()
                    next_stale = self._clock() + timedelta(
                        seconds=stale_interval_seconds
                    )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — the loop outlives one bad tick
                logger.exception("work queue: tick raised; carrying on")
            done += 1
            await sleeper(interval_seconds)


def first_sentence(text: str) -> str:
    """The first sentence of a refusal, with no full stop left on the end.

    A refusal is not always a sentence. THE CAP LAW's refusal is a runbook of
    about seven hundred characters with a YAML snippet in it, and all of that
    flattened into one Slack line is a line nobody reads. The sentence ends at
    the first full stop followed by a space, or at the first line break,
    whichever comes first.
    """
    raw = str(text).strip()
    if not raw:
        return ""
    line = raw.split("\n", 1)[0]
    head, stop, _rest = line.partition(". ")
    sentence = head if stop else line
    return " ".join(sentence.split()).rstrip(".")


def refusal_line(queue_id: int, reason: str) -> str:
    """The one line the channel gets when a row is refused for good.

    The refusal's FIRST sentence only — what happened, in one line. The whole
    of the refusal, runbook and all, is written on the row itself
    (``closed_reason``) and in the row's events, which is where anyone who
    wants the rest of it looks.
    """
    return (
        f"#{queue_id} cannot be started: {first_sentence(reason)}. It is out "
        "of the queue; drop it or fix the cause and send it again."
    )


def shadow_line(shadow: Pick, taken_id: int) -> str:
    """The one line the loop says when the class order disagrees with the queue."""
    return (
        f"next I'd pick #{shadow.queue_id} ({shadow.label()}), "
        f"because {shadow.reason}; taking #{taken_id} as things stand."
    )


def _build_field(build: Any, name: str) -> Any:
    """One field of a build, whether it reads like a row or like a mapping."""
    try:
        return build[name]
    except (KeyError, IndexError, TypeError):
        return None


def _parse(moment: str) -> datetime:
    """An ISO timestamp from the database, always with a timezone on it."""
    try:
        parsed = datetime.fromisoformat(moment)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


__all__ = [
    "Admission",
    "BLOCKED_ACTION",
    "BUILD_FAILURE_STATES",
    "BUILD_SUCCESS_STATES",
    "BUILD_TERMINAL_STATES",
    "BUILD_WRITTEN_STATE",
    "FIX_KIND",
    "LOOP_ACTOR",
    "PLANNING_FAILURE_STATES",
    "PLANNING_SUCCESS_STATES",
    "PLANNING_TERMINAL_STATES",
    "Pick",
    "STALE_REPING_DAYS",
    "STALE_TICK_INTERVAL_SECONDS",
    "TAKE_NEXT_INTERVAL_SECONDS",
    "WorkQueueLoop",
    "count_in_flight",
    "first_sentence",
    "paused_repositories",
    "refusal_line",
    "shadow_line",
]

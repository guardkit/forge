"""The mission step in the work queue loop's tick (missions design, section 2a).

Mission work never goes into ``work_queue`` (round 2, R10), so the queue's own
steps never see it. This step runs inside the same loop, right after the
queue's close step, and does for every RUNNING mission what the queue does for
its rows, in one transaction per change:

0. **Picks up what was dropped.** An ADMITTED attempt whose planning run does
   not exist (the forge stopped between the two) goes back to WAITING.
1. **Closes.** Each ADMITTED attempt's planning run and build are read by its
   correlation id, with the same readers the queue uses. Landed on the mission
   branch is LANDED. A first failure on attempt 1 (a conflict, a failed
   planning run or build, or the landing press's own FAILED report or
   refusal) closes it FAILED and files attempt 2 WAITING in the same
   transaction (step 1, 24 October 2026); a cancelled run or build, a declined
   merge, a second attempt or a mission asked to stop closes FAILED with no
   further attempt. Each closes with the reason's first sentence. A finished build with no landing report yet waits:
   resuming a press that stopped is the merge side's job, never a timer's.
2. **Holds.** A WAITING attempt is HELD ("<key> did not land") when a key it
   needs has its final attempt FAILED or HELD. While a rebuild is pending the
   dependants keep waiting.
3. **Admits.** A WAITING attempt whose needs have all LANDED is admitted the
   way ``take_next`` admits: the count is taken inside the admission's own
   transaction against ``queue.max_in_flight``, and the planning run is made by
   the same run maker, with the attempt's correlation id and the mission's
   thread. Older missions go first, then the order the features were given.
4. **READY.** A mission with nothing WAITING or ADMITTED is set READY and left
   there; finishing it (the mission merge card) is not this step's job.

There is no "hold or go?" question inside a mission: the mission's merge card
reports what did not land instead.
"""

from __future__ import annotations

import logging
import json
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Any

from forge.lifecycle.missions import CONFLICT_REASON, Mission, MissionAttempt, MissionStore
from forge.pipeline.publication_record import LINE_DONE, STEP_JOIN, PublicationRecordStore

logger = logging.getLogger(__name__)

__all__ = ["MissionStep", "landing_was_a_conflict", "mission_landing", "mission_queue_lines"]

#: How a refused join's sentence starts when the merge stopped on a conflict
#: (``merge_executor._conflict_sentence``). Read as well as the ``conflict``
#: flag on the join line, so a press from before the flag still counts.
_CONFLICT_SENTENCE_START = "the merge stopped on a conflict"

#: The answer :func:`mission_landing` gives for a conflict.
CONFLICT: str = CONFLICT_REASON


def landing_was_a_conflict(connection: sqlite3.Connection, build_id: str) -> bool:
    """Did the merge into the mission branch stop on a conflict?

    Read from the build's publication record: a join written down as done and
    not joined, with ``conflict`` true or a refusal that says so.
    """
    record = PublicationRecordStore(connection).read(build_id)
    joins = [
        line for line in record.lines if line.kind == LINE_DONE and line.step == STEP_JOIN
    ]
    if not joins:
        return False
    detail = joins[-1].detail or {}  # the last join is the one that counts
    if detail.get("joined") is not False:
        return False
    if detail.get("conflict") is True:
        return True
    refusal = str(detail.get("refusal") or "").strip().lower()
    return refusal.startswith(_CONFLICT_SENTENCE_START)


def _publication_unsettled(connection: sqlite3.Connection, build_id: str) -> bool:
    """Has this build's publication record tried a send and not settled yet?"""
    from forge.pipeline.publication_record import STEP_SEND

    record = PublicationRecordStore(connection).read(build_id)
    if not record.recorded or record.result:
        return False
    return any(line.step == STEP_SEND for line in record.lines)


def mission_landing(
    connection: sqlite3.Connection, correlation_id: str, *, now: datetime | None = None
) -> tuple[str, str | None]:
    """``(answer, reason)`` for one attempt's work, in ``work_landing``'s words.

    ``work_landing`` decides it, with the merge executor taken as on (a mission
    needs it) — so a mission feature is LANDED only once its record says it is
    on the mission branch. One answer is added: :data:`CONFLICT` for a merge
    into the mission branch that stopped on a conflict. ``reason`` is the
    build's own error or the press's sentence, when there is one.
    """
    from forge.planning import work_queue_loop as wq

    answer = wq.work_landing(connection, correlation_id, now=now)
    build = connection.execute(
        "SELECT build_id, error FROM builds WHERE correlation_id = ? "
        "ORDER BY queued_at DESC LIMIT 1",
        (correlation_id,),
    ).fetchone()
    if build is None:
        return answer, None
    build_id, error = str(build[0]), build[1]
    if answer in (wq.DID_NOT_LAND, wq.NOT_PUBLISHED) and _publication_unsettled(
        connection, build_id
    ):
        # AN UNCERTAIN OUTCOME IS NEVER TURNED INTO FAILED (section 11, R4): a
        # press that tried a send and left its record with no result may have
        # landed. The press settles it by looking at the remote (recovery
        # re-checks it), and until then the attempt waits.
        return wq.NOT_LANDED_YET, None
    if answer == wq.DID_NOT_LAND and landing_was_a_conflict(connection, build_id):
        return CONFLICT, CONFLICT_REASON
    reason = str(error) if error else None
    if reason is None and answer in (wq.DID_NOT_LAND, wq.NOT_PUBLISHED):
        record = PublicationRecordStore(connection).read(build_id)
        for line in reversed(record.lines):
            said = (line.detail or {}).get("refusal")
            if line.kind == LINE_DONE and said:
                reason = str(said)
                break
    if reason is None and answer == wq.DID_NOT_LAND:
        # A landing the merge side gave up on (``mission-landing-failed``)
        # carries its sentence on the merge report, not on the build or the
        # publication record.
        report = connection.execute(
            "SELECT details_json FROM stage_log WHERE build_id = ? "
            "AND target_identifier = 'merge_deploy_executor' AND status = 'FAILED' "
            "ORDER BY rowid DESC LIMIT 1",
            (build_id,),
        ).fetchone()
        if report is not None and report[0]:
            try:
                said = json.loads(report[0]).get("detail")
            except (ValueError, AttributeError):
                said = None
            if said:
                reason = str(said)
    return answer, reason


class MissionStep:
    """One pass over every RUNNING mission. Built and called by the work queue loop."""

    def __init__(
        self,
        missions: MissionStore,
        *,
        planning_run: Callable[[str], Mapping[str, Any] | sqlite3.Row | None],
        count_in_flight: Callable[[], int],
        max_in_flight: int,
        start_run: Callable[[Any], Awaitable[None]],
        failure_reason: Callable[[Any, str, str], str],
        clock: Callable[[], datetime],
        before: Callable[[], Awaitable[None]] | None = None,
        notify: Callable[[Mission, str], Awaitable[None]] | None = None,
    ) -> None:
        """``failure_reason(run, state, correlation_id)`` is the loop's own
        wording for a planning run that ended badly; ``before`` is the mission
        flow's tending of PROPOSED and STARTING missions (cards to re-arm,
        branches to ask for again), run first; ``notify(mission, message)``
        says one line in the mission's thread (a stop asked for, and its end)."""
        self._missions = missions
        self._planning_run = planning_run
        self._count_in_flight = count_in_flight
        self._max_in_flight = max_in_flight
        self._start_run = start_run
        self._failure_reason = failure_reason
        self._clock = clock
        self._before = before
        self._notify = notify
        #: Stops this process has already said were asked for, so the thread
        #: gets the line once (once more after a restart, at most).
        self._stop_said: set[str] = set()

    async def run_once(self) -> None:
        """Tend, then recover, close, hold, admit and mark READY, mission by mission."""
        if self._before is not None:
            try:
                await self._before()
            except Exception:  # noqa: BLE001 — never stop the loop
                logger.exception("missions: tending proposed and starting missions failed")
        try:
            await self.apply_stops()
        except sqlite3.Error as exc:
            logger.warning("missions: stops could not be applied this tick (%s)", exc)
        for mission in self._missions.list_by_status(("RUNNING",)):
            if mission.stopping:
                continue  # applied above: nothing is recovered or held for it
            try:
                self.recover(mission)
                self.close(mission)
                self.hold(mission)
            except sqlite3.Error as exc:
                logger.warning(
                    "missions: %s could not be stepped this tick (%s: %s)",
                    mission.id,
                    type(exc).__name__,
                    exc,
                )
                continue
        for mission in self._missions.list_by_status(("RUNNING",)):
            if mission.stopping:
                continue  # nothing new starts for a stopping mission
            if not await self.admit(mission):
                break  # the factory is full; nothing else may start this tick
        for mission in self._missions.list_by_status(("RUNNING",)):
            if not mission.stopping:
                self.mark_ready(mission)

    # -- a stop asked for (section 11) -------------------------------------

    async def apply_stops(self) -> list[str]:
        """Carry out every stop asked for, in the design's order. Returns the
        ids of the missions that ended this pass.

        1. Nothing new starts: every WAITING attempt is HELD in one
           transaction (and admission, the put-back, the rebuild and the
           branch answer each check the stop in their own).
        2. An ADMITTED attempt with no planning run and no build is closed
           FAILED, not put back.
        3. Running work is left to end: its attempt is closed from its own
           ending, as on any tick (and planning and the build gate refuse a
           stopped mission's run at their next check).
        4. Landings settle in the press, under the repository's lock.
        5. Once nothing is waiting, admitted, building or unsettled, the
           mission ends NOT_MERGED with who stopped it, when, and what landed.
        """
        from forge.lifecycle.persistence import ACTIVE_STATES

        ended: list[str] = []
        active_builds = sorted(state.value for state in ACTIVE_STATES)
        for mission in self._missions.list_stopping():
            if mission.status == "MERGING":
                continue  # never asked for in MERGING; nothing to drain here
            self._missions.hold_for_stop(mission.id)
            self._missions.close_abandoned(mission.id)
            if mission.status == "RUNNING":
                self.close(mission)
            if mission.id not in self._stop_said:
                self._stop_said.add(mission.id)
                await self._say(
                    mission,
                    f'Mission "{mission.title}" is stopping (asked by '
                    f"{mission.stop_requested_by}): nothing new will start, and "
                    "work already running will finish without being merged into "
                    "main.",
                )
            reason = self._missions.close_drained(
                mission.id, active_build_states=active_builds
            )
            if reason is not None:
                ended.append(mission.id)
                logger.info("missions: %s is NOT_MERGED — %s", mission.id, reason)
                await self._say(
                    mission,
                    f'Mission "{mission.title}" has stopped: {reason}. Nothing '
                    f"reached main and the branch {mission.branch} is kept.",
                )
        return ended

    async def _say(self, mission: Mission, message: str) -> None:
        if self._notify is None:
            return
        try:
            await self._notify(mission, message)
        except Exception as exc:  # noqa: BLE001 — a line never costs a state
            logger.warning("missions: could not tell %s's thread (%s)", mission.id, exc)

    # -- 0 ----------------------------------------------------------------

    def recover(self, mission: Mission) -> list[str]:
        """Put every ADMITTED attempt with no planning run back to WAITING."""
        recovered: list[str] = []
        for attempt in self._missions.attempts(mission.id):
            if attempt.status != "ADMITTED":
                continue
            if self._planning_run(attempt.correlation_id) is not None:
                continue
            if self._missions.requeue_attempt(attempt.correlation_id):
                logger.warning(
                    "missions: %s %s was admitted but has no planning run; "
                    "putting it back to waiting",
                    mission.id,
                    attempt.correlation_id,
                )
                recovered.append(attempt.correlation_id)
        return recovered

    # -- 1 ----------------------------------------------------------------

    def close(self, mission: Mission) -> int:
        """Close every ADMITTED attempt whose work has ended. Returns how many."""
        from forge.planning import work_queue_loop as wq

        closed = 0
        for attempt in self._missions.attempts(mission.id):
            if attempt.status != "ADMITTED":
                continue
            run = self._planning_run(attempt.correlation_id)
            if run is None:
                continue
            state = str(run["state"])
            if state in wq.PLANNING_FAILURE_STATES:
                reason = self._failure_reason(run, state, attempt.correlation_id)
                # A cancelled run is never rebuilt (step 1, 24 October 2026).
                if state == "CANCELLED":
                    closed += self._fail(mission, attempt, reason)
                else:
                    closed += self._fail_with_rebuild(mission, attempt, reason)
                continue
            if state not in wq.PLANNING_SUCCESS_STATES:
                continue
            answer, reason = mission_landing(
                self._missions.connection, attempt.correlation_id, now=self._clock()
            )
            if answer in (wq.LANDED, wq.BUILT_NOTHING):
                note = "nothing needed building" if answer == wq.BUILT_NOTHING else None
                if self._missions.mark_landed(attempt.correlation_id, note):
                    logger.info("missions: %s %s landed", mission.id, attempt.key)
                    closed += 1
            elif answer == CONFLICT:
                if self._missions.fail_with_rebuild(attempt.correlation_id):
                    logger.warning(
                        "missions: %s %s attempt %d stopped on a conflict%s",
                        mission.id,
                        attempt.key,
                        attempt.attempt,
                        "; rebuilding it once" if attempt.attempt == 1 else "",
                    )
                    closed += 1
            elif answer in wq.WILL_NOT_LAND_ON_ITS_OWN:
                # A declined merge or a cancelled build is never rebuilt.
                if answer == wq.MERGE_DECLINED or self._build_was_cancelled(
                    attempt.correlation_id
                ):
                    closed += self._fail(mission, attempt, reason or answer)
                else:
                    closed += self._fail_with_rebuild(
                        mission, attempt, reason or answer
                    )
        return closed

    def _fail_with_rebuild(
        self, mission: Mission, attempt: MissionAttempt, reason: str
    ) -> int:
        """Close a first failure with its one rebuild (step 1, 24 October 2026).

        Attempt 1 closes FAILED and attempt 2 is filed WAITING, together; a
        second attempt, or a mission asked to stop, closes FAILED with no
        further attempt.
        """
        from forge.planning.work_queue_loop import first_sentence

        sentence = first_sentence(reason) or reason
        if self._missions.fail_with_rebuild(attempt.correlation_id, sentence):
            logger.warning(
                "missions: %s %s attempt %d did not land — %s%s",
                mission.id,
                attempt.key,
                attempt.attempt,
                sentence,
                "; rebuilding it once" if attempt.attempt == 1 else "",
            )
            return 1
        return 0

    def _build_was_cancelled(self, correlation_id: str) -> bool:
        row = self._missions.connection.execute(
            "SELECT status FROM builds WHERE correlation_id = ? "
            "ORDER BY queued_at DESC LIMIT 1",
            (correlation_id,),
        ).fetchone()
        return row is not None and str(row[0]) == "CANCELLED"

    def _fail(self, mission: Mission, attempt: MissionAttempt, reason: str) -> int:
        from forge.planning.work_queue_loop import first_sentence

        sentence = first_sentence(reason) or reason
        if self._missions.mark_failed(attempt.correlation_id, sentence):
            logger.warning(
                "missions: %s %s did not land — %s", mission.id, attempt.key, sentence
            )
            return 1
        return 0

    # -- 2 ----------------------------------------------------------------

    def hold(self, mission: Mission) -> int:
        """Hold every WAITING attempt that something it needs will never reach."""
        held = 0
        for attempt in self._missions.attempts(mission.id):
            if attempt.status != "WAITING":
                continue
            answer = self._missions.needs_resolution(mission.id, attempt.key)
            if answer.blocked_by is None:
                continue
            if self._missions.mark_held(
                attempt.correlation_id, f"{answer.blocked_by} did not land"
            ):
                logger.info(
                    "missions: %s %s is held — %s did not land",
                    mission.id,
                    attempt.key,
                    answer.blocked_by,
                )
                held += 1
        return held

    # -- 3 ----------------------------------------------------------------

    async def admit(self, mission: Mission) -> bool:
        """Admit every attempt that may start. False once the factory is full.

        A mission whose project's sandbox is not ready (one sandbox per
        project, stage S2, design 2.3a) admits nothing this tick and answers
        True, so the other missions still go: its sandbox is being started and
        its attempts stay WAITING. With no sandbox gate installed it is today's.
        """
        from forge.planning.work_queue_loop import Admission
        from forge.sandbox_gate.gate import current_gate

        gate = current_gate()
        for attempt in self._missions.attempts(mission.id):
            if attempt.status != "WAITING":
                continue
            if not self._missions.needs_resolution(mission.id, attempt.key).ready:
                continue
            if self._count_in_flight() >= self._max_in_flight:
                return False
            if gate is not None and not gate.admits(mission.target_repo):
                why = gate.why_held(mission.target_repo) or "its sandbox is not ready"
                line = f"mission {mission.id} is waiting: {why}"
                if gate.say_once(mission.target_repo, line):
                    logger.info("missions: %s", line)
                return True
            if not self._missions.admit_attempt(
                attempt.correlation_id,
                max_in_flight=self._max_in_flight,
                count_in_flight=self._count_in_flight,
            ):
                return False
            sentence = next(
                (f.sentence for f in self._missions.features(mission.id) if f.key == attempt.key),
                "",
            )
            admission = Admission(
                queue_id=0,  # a mission attempt has no queue row
                correlation_id=attempt.correlation_id,
                request_text=sentence,
                target_repo=mission.target_repo,
                originating_user=mission.originating_user,
                parent_request_id=mission.parent_request_id,
                originating_adapter=mission.originating_adapter,
                triggered_by=mission.triggered_by or "jarvis",
                kind="feature",
            )
            try:
                await self._start_run(admission)
            except Exception as exc:  # noqa: BLE001 — never lose the attempt
                logger.exception(
                    "missions: starting %s failed (%s); putting it back to waiting",
                    attempt.correlation_id,
                    exc,
                )
                self._missions.requeue_attempt(attempt.correlation_id)
                return False
            logger.info(
                "missions: %s %s attempt %d admitted (correlation id %s)",
                mission.id,
                attempt.key,
                attempt.attempt,
                attempt.correlation_id,
            )
        return True

    # -- 4 ----------------------------------------------------------------

    def mark_ready(self, mission: Mission) -> bool:
        """RUNNING → READY once nothing is WAITING or ADMITTED, and every landing
        decided has its report (the invariant of
        :meth:`MissionStore._unreported_landings`: the finisher writes a missing
        one from the settled record before the mission may move on)."""
        open_now = [
            a for a in self._missions.attempts(mission.id) if a.status in ("WAITING", "ADMITTED")
        ]
        if open_now or not self._missions.attempts(mission.id):
            return False
        if self._missions.has_unreported_landings(mission.id):
            return False
        if self._missions.set_status(mission.id, "READY", expected=("RUNNING",)):
            logger.info("missions: %s is READY — every feature has finished", mission.id)
            return True
        return False



def mission_queue_lines(connection: sqlite3.Connection) -> list[str]:
    """One line per running mission, for the ``queue`` listing (design section 8).

    For example ``mission "Tags for notes": 2 of 4 landed, B building, D
    waiting for B and C``. Nothing for a ledger with no mission tables.
    """
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'missions'"
        ).fetchone()
    except sqlite3.Error:
        return []
    if exists is None:
        return []
    store = MissionStore(connection)
    lines: list[str] = []
    for mission in store.list_by_status(("RUNNING",)):
        features = store.features(mission.id)
        latest = store.latest_attempts(mission.id)
        landed = sum(1 for a in latest.values() if a.status == "LANDED")
        parts = [f"{landed} of {len(features)} landed"]
        for feature in features:
            attempt = latest.get(feature.key)
            if attempt is None:
                continue
            if attempt.status == "ADMITTED":
                parts.append(f"{feature.key} building")
            elif attempt.status == "WAITING":
                waiting = store.needs_resolution(mission.id, feature.key).waiting_for
                if waiting:
                    parts.append(f"{feature.key} waiting for {_and(waiting)}")
                else:
                    parts.append(f"{feature.key} about to start")
            elif attempt.status in ("FAILED", "HELD"):
                parts.append(f"{feature.key} did not land")
        lines.append(f'mission "{mission.title}": ' + ", ".join(parts))
    return lines


def _and(keys: tuple[str, ...]) -> str:
    if len(keys) <= 1:
        return "".join(keys)
    return ", ".join(keys[:-1]) + " and " + keys[-1]

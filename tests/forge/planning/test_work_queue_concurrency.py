"""Several pieces of work at once: the sentence queue's count, its admission
and its "after #n" ordering (concurrent builds design, 3 October 2026).

These pin what has to stay true once the queue lets more than one piece of
work run:

- the count and the admission are one step, so two admitters racing on the
  one database can never both take the last place, whatever the limit;
- a row the queue has admitted counts from the moment it is admitted, before
  its planning run exists, and an admitted row and its own run count once;
- a starter that is still starting keeps its place, and restart recovery
  gives a place back when nothing was ever started;
- the count is refused at wiring time if it would read a different database
  file from the queue's;
- a row that says "after #A" waits until A's work has LANDED — published to
  the remote, read from the merge executor's own record — not merely until
  A's queue row closed; a declined or failed A asks "hold or go"; an
  unrelated row goes ahead throughout.

Every check runs on a real migrated SQLite file. The concurrent ones open one
connection per admitter, as two processes would.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence, StageLogEntry
from forge.pipeline.merge_executor import (
    MERGE_REPORT_STAGE_LABEL,
    MERGE_REPORT_TARGET_IDENTIFIER,
    RESULT_WORD_MERGED_AND_RUNNING,
    RESULT_WORD_PUBLICATION_PENDING,
    RESULT_WORD_PUBLISHED_DEPLOYMENT_PENDING,
)
from forge.pipeline.publication_record import (
    RESULT_PUBLISHED_DEPLOYMENT_PENDING,
    STEP_SEND,
    PublicationRecordStore,
)
from forge.planning.states import PlanningState
from forge.planning.work_queue_loop import (
    LOOP_ACTOR,
    MERGE_CARD_GRACE_SECONDS,
    Admission,
    WorkQueueLoop,
    count_in_flight,
    unanswered_merge_cards,
)
from forge.planning.work_queue_store import WorkQueueStore
from tests.forge.planning.test_work_queue_loop import (
    A_DAY,
    START,
    USER,
    FakeClock,
    Notifier,
    _answer_merge_card,
    _insert_run,
    _offer_merge_card,
    file_row,
)

LIMITS = (1, 2, 4, 8)


# ---------------------------------------------------------------------------
# A real database, one connection per admitter
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "forge.db"
    cx = sqlite_connect.connect_writer(path)
    migrations.apply_at_boot(cx)
    cx.close()
    return path


def _open(path: Path) -> sqlite3.Connection:
    return sqlite_connect.connect_writer(path)


def _run_row(cx: sqlite3.Connection, correlation_id: str) -> Any:
    return cx.execute(
        "SELECT * FROM planning_runs WHERE correlation_id = ?", (correlation_id,)
    ).fetchone()


class DbRunMaker:
    """Starts a planning run the way the real one does: by writing its row."""

    def __init__(self, cx: sqlite3.Connection) -> None:
        self._cx = cx
        self.admissions: list[Admission] = []

    async def __call__(self, admission: Admission) -> None:
        self.admissions.append(admission)
        _insert_run(self._cx, admission.correlation_id, PlanningState.QUEUED.value)


class BlockingStarter:
    """A starter that has begun and not finished — the gap between the
    admission and the run existing, held open for as long as the test likes."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, admission: Admission) -> None:
        self.entered.set()
        await self.release.wait()


def a_loop(
    cx: sqlite3.Connection,
    *,
    limit: int,
    clock: FakeClock,
    notifier: Notifier | None = None,
    start_run: Any | None = None,
    count: Callable[[], int] | None = None,
    hold_seconds: int = 0,
    **extra: Any,
) -> WorkQueueLoop:
    store = WorkQueueStore(cx, clock=clock.now)
    return WorkQueueLoop(
        store,
        count_in_flight=count
        or (
            lambda: count_in_flight(
                cx, merge_offer_hold_seconds=hold_seconds, now=clock.now()
            )
        ),
        planning_run=lambda cid: _run_row(cx, cid),
        paused_repositories=lambda: set(),
        start_run=start_run or DbRunMaker(cx),
        notify=notifier or Notifier(),
        max_in_flight=limit,
        clock=clock.now,
        merge_cards=lambda: unanswered_merge_cards(cx),
        merge_offer_hold_seconds=hold_seconds,
        **extra,
    )


def _statuses(cx: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row[0]): str(row[1])
        for row in cx.execute("SELECT correlation_id, status FROM work_queue")
    }


def _admitted(cx: sqlite3.Connection) -> int:
    return sum(1 for status in _statuses(cx).values() if status == "ADMITTED")


def _insert_build_for(
    cx: sqlite3.Connection,
    build_id: str,
    status: str,
    *,
    correlation_id: str,
    completed_at: str | None = None,
) -> None:
    cx.execute(
        """
        INSERT INTO builds (
            build_id, feature_id, repo, branch, feature_yaml_path, status,
            triggered_by, correlation_id, queued_at, max_turns,
            sdk_timeout_seconds, mode, completed_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'cli', ?, ?, 50, 3600, 'mode-b', ?)
        """,
        (
            build_id,
            f"FEAT-{build_id}",
            "/tmp/api_test",
            "main",
            "feature.yaml",
            status,
            correlation_id,
            START.isoformat(),
            completed_at,
        ),
    )


# ---------------------------------------------------------------------------
# The count and the admission are one step
# ---------------------------------------------------------------------------


class TestTheCountAndTheAdmissionAreOneStep:
    @pytest.mark.parametrize("limit", LIMITS)
    def test_concurrent_admitters_never_pass_the_limit(
        self, db_path: Path, limit: int
    ) -> None:
        """Twice as many admitters as places, each on its own connection, all
        reading the count at the same moment and each asking again after it
        loses a row to another. A count that is slow to answer is the widest
        the window between "is there room" and "take it" can be; with the two
        as one step, nobody gets past the limit."""
        setup = _open(db_path)
        setup_store = WorkQueueStore(setup)
        for index in range(3 * limit):
            file_row(setup_store, f"corr-{index}")
        setup.close()

        admitters = 2 * limit + 1
        barrier = threading.Barrier(admitters)
        taken: list[int | None] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def admitter() -> None:
            cx = _open(db_path)
            try:
                clock = FakeClock()

                def slow_count() -> int:
                    counted = count_in_flight(cx)
                    time.sleep(0.05)
                    return counted

                loop = a_loop(cx, limit=limit, clock=clock, count=slow_count)

                async def keep_taking() -> list[int | None]:
                    # Every admitter keeps asking, as a loop ticking every ten
                    # seconds would; one that lost the race for a row tries
                    # the next one.
                    return [await loop.take_next() for _ in range(limit + 2)]

                barrier.wait(timeout=30)
                results = asyncio.run(keep_taking())
                with lock:
                    taken.extend(results)
            except BaseException as exc:  # noqa: BLE001 — reported below
                with lock:
                    errors.append(exc)
            finally:
                cx.close()

        threads = [threading.Thread(target=admitter) for _ in range(admitters)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert errors == []
        check = _open(db_path)
        try:
            assert _admitted(check) == limit
            assert count_in_flight(check) == limit
            assert len([result for result in taken if result is not None]) == limit
        finally:
            check.close()

    def test_an_admitted_row_with_no_planning_run_yet_counts(
        self, db_path: Path
    ) -> None:
        cx = _open(db_path)
        store = WorkQueueStore(cx)
        queue_id = file_row(store, "corr-A")
        assert store.admit(queue_id, actor_identity=LOOP_ACTOR)

        assert count_in_flight(cx) == 1

    def test_an_admitted_row_and_its_own_work_count_once(
        self, db_path: Path
    ) -> None:
        """The row, its planning run and the build the run handed over are one
        piece of work under one correlation id; a second admitted row with
        nothing behind it yet is a second piece."""
        cx = _open(db_path)
        store = WorkQueueStore(cx)
        first = file_row(store, "corr-A")
        second = file_row(store, "corr-B")
        store.admit(first, actor_identity=LOOP_ACTOR)
        store.admit(second, actor_identity=LOOP_ACTOR)
        _insert_run(cx, "corr-A", PlanningState.RUNNING.value)
        _insert_build_for(cx, "build-A", "RUNNING", correlation_id="corr-A")

        assert count_in_flight(cx) == 2

    def test_an_admitted_row_stops_counting_once_its_work_is_written(
        self, db_path: Path
    ) -> None:
        """Once the run or the build exists it speaks for itself: a repair
        whose build was marked INTERRUPTED stays uncounted although its row is
        still admitted, and so does a row whose run has handed over."""
        cx = _open(db_path)
        store = WorkQueueStore(cx)
        repair = file_row(store, "corr-fix", kind="fix")
        feature = file_row(store, "corr-feature")
        store.admit(repair, actor_identity=LOOP_ACTOR)
        store.admit(feature, actor_identity=LOOP_ACTOR)
        _insert_build_for(cx, "build-fix", "INTERRUPTED", correlation_id="corr-fix")
        _insert_run(cx, "corr-feature", PlanningState.BUILD_QUEUED.value)

        assert count_in_flight(cx) == 0

    def test_an_admission_inside_someone_elses_transaction_is_refused(
        self, db_path: Path
    ) -> None:
        cx = _open(db_path)
        store = WorkQueueStore(cx)
        queue_id = file_row(store, "corr-A")
        cx.execute("BEGIN")  # deferred: holds no write lock
        try:
            with pytest.raises(RuntimeError, match="BEGIN IMMEDIATE"):
                store.admit(
                    queue_id,
                    actor_identity=LOOP_ACTOR,
                    max_in_flight=1,
                    count_in_flight=lambda: 0,
                )
        finally:
            cx.execute("ROLLBACK")

    def test_a_build_and_its_open_merge_card_count_once(
        self, db_path: Path
    ) -> None:
        cx = _open(db_path)
        _insert_build_for(cx, "build-A", "FINALISING", correlation_id="corr-A")
        _offer_merge_card(cx, "build-A", offered_at=START - timedelta(minutes=5))

        assert count_in_flight(cx, merge_offer_hold_seconds=A_DAY, now=START) == 1

    @pytest.mark.parametrize("limit", LIMITS)
    @pytest.mark.asyncio
    async def test_a_starter_that_has_not_finished_keeps_its_place(
        self, db_path: Path, limit: int
    ) -> None:
        """The places already taken are runs paused at one of Rich's cards; the
        last place is taken by a starter that has not finished. A second
        admitter on its own connection finds no room."""
        first_cx = _open(db_path)
        second_cx = _open(db_path)
        clock = FakeClock()
        store = WorkQueueStore(first_cx, clock=clock.now)
        for index in range(limit - 1):
            paused = file_row(store, f"paused-{index}")
            store.admit(paused, actor_identity=LOOP_ACTOR)
            _insert_run(first_cx, f"paused-{index}", PlanningState.PAUSED.value)
        starting = file_row(store, "starting")
        waiting = file_row(store, "waiting")

        starter = BlockingStarter()
        first = a_loop(first_cx, limit=limit, clock=clock, start_run=starter)
        second = a_loop(second_cx, limit=limit, clock=clock)

        under_way = asyncio.create_task(first.take_next())
        await asyncio.wait_for(starter.entered.wait(), timeout=5)
        try:
            assert await second.take_next() is None
            assert _statuses(second_cx)["waiting"] == "QUEUED"
            assert count_in_flight(second_cx) == limit
        finally:
            starter.release.set()
            assert await under_way == starting
        assert waiting != starting

    @pytest.mark.asyncio
    async def test_restart_recovery_gives_the_place_back(
        self, db_path: Path
    ) -> None:
        """An admitted row whose run was never created holds the one place
        until recovery puts it back; then the queue moves again."""
        cx = _open(db_path)
        clock = FakeClock()
        store = WorkQueueStore(cx, clock=clock.now)
        stranded = file_row(store, "corr-A")
        behind = file_row(store, "corr-B")
        store.admit(stranded, actor_identity=LOOP_ACTOR)
        loop = a_loop(cx, limit=1, clock=clock)

        assert await loop.take_next() is None
        assert _statuses(cx)["corr-B"] == "QUEUED"

        assert loop.recover_admitted() == [stranded]
        assert count_in_flight(cx) == 0
        assert await loop.take_next() == stranded
        assert behind != stranded

    def test_a_full_count_refuses_the_admission_inside_the_claim(
        self, db_path: Path
    ) -> None:
        cx = _open(db_path)
        store = WorkQueueStore(cx)
        queue_id = file_row(store, "corr-A")

        assert not store.admit(
            queue_id,
            actor_identity=LOOP_ACTOR,
            max_in_flight=1,
            count_in_flight=lambda: 1,
        )
        row = store.get(queue_id)
        assert row is not None and row["status"] == "QUEUED"
        assert not store.has_event(queue_id, "admitted")

        assert store.admit(
            queue_id,
            actor_identity=LOOP_ACTOR,
            max_in_flight=1,
            count_in_flight=lambda: 0,
        )


class TestTheCountReadsTheQueuesOwnDatabase:
    def test_a_count_on_another_file_is_refused_when_the_loop_is_made(
        self, db_path: Path, tmp_path: Path
    ) -> None:
        other_path = tmp_path / "other.db"
        other = _open(other_path)
        migrations.apply_at_boot(other)
        cx = _open(db_path)

        with pytest.raises(ValueError, match="different database"):
            a_loop(cx, limit=2, clock=FakeClock(), in_flight_database=other)

    def test_another_connection_to_the_same_file_is_accepted(
        self, db_path: Path
    ) -> None:
        cx = _open(db_path)
        same_file = _open(db_path)

        a_loop(cx, limit=2, clock=FakeClock(), in_flight_database=same_file)


# ---------------------------------------------------------------------------
# "after #A" waits for A's work to land (finding R5)
# ---------------------------------------------------------------------------


def _record_stage(
    cx: sqlite3.Connection,
    build_id: str,
    *,
    label: str,
    target: str,
    status: str,
    details: dict[str, Any] | None = None,
) -> None:
    SqliteLifecyclePersistence(connection=cx).record_stage(
        StageLogEntry(
            build_id=build_id,
            stage_label=label,
            target_kind="local_tool",
            target_identifier=target,
            status=status,
            gate_mode=None,
            started_at=START,
            completed_at=START,
            duration_secs=0.0,
            details=details or {},
        )
    )


def _report(cx: sqlite3.Connection, build_id: str, *, status: str, result: str) -> None:
    """The merge executor's outcome report, with its result word."""
    _record_stage(
        cx,
        build_id,
        label=MERGE_REPORT_STAGE_LABEL,
        target=MERGE_REPORT_TARGET_IDENTIFIER,
        status=status,
        details={"result": result},
    )


def _send(cx: sqlite3.Connection, build_id: str, *, published: bool | None) -> None:
    """Write the merge executor's send step the way it writes it: "about to",
    then (unless ``published`` is None) "done" with what the publisher said."""
    record = PublicationRecordStore(cx)
    grant = record.take_lease(build_id=build_id, holder="press", now=START)
    assert grant is not None
    assert record.about_to(
        build_id=build_id,
        turn=grant.turn,
        now=START,
        step=STEP_SEND,
        attempt=1,
        inputs={"build_id": build_id},
    )
    if published is None:
        return
    assert record.done(
        build_id=build_id,
        turn=grant.turn,
        now=START,
        step=STEP_SEND,
        attempt=1,
        result={"published": published, "contains_j": published},
    )


#: Where A's work has got to, in the order it gets there. Each step is
#: applied on top of the ones before it.
_STAGES: tuple[str, ...] = (
    "planning",
    "build handed over, not written yet",
    "building",
    "at its merge card",
    "approved, waiting for the merge lock",
    "merging",
)


def _bring_a_to(cx: sqlite3.Connection, store: WorkQueueStore, a_id: int, stage: str) -> None:
    """Put A's work at ``stage`` on the record, as the real pieces write it."""
    store.admit(a_id, actor_identity=LOOP_ACTOR)
    _insert_run(cx, "corr-A", PlanningState.RUNNING.value)
    if stage == "planning":
        return
    cx.execute(
        "UPDATE planning_runs SET state = ? WHERE correlation_id = ?",
        (PlanningState.BUILD_QUEUED.value, "corr-A"),
    )
    store.close(a_id, status="DONE", actor_identity=LOOP_ACTOR)
    if stage == "build handed over, not written yet":
        return
    _insert_build_for(cx, "build-A", "RUNNING", correlation_id="corr-A")
    if stage == "building":
        return
    cx.execute("UPDATE builds SET status = 'COMPLETE' WHERE build_id = 'build-A'")
    _offer_merge_card(cx, "build-A", offered_at=START - timedelta(minutes=5))
    if stage == "at its merge card":
        return
    _answer_merge_card(cx, "build-A", answered_at=START, decision="approve")
    if stage == "approved, waiting for the merge lock":
        return
    _send(cx, "build-A", published=None)


class TestAfterWaitsForTheWorkToLand:
    def _queue(
        self, db_path: Path, *, a_kind: str = "feature"
    ) -> tuple[sqlite3.Connection, WorkQueueStore, int, int, int]:
        cx = _open(db_path)
        clock = FakeClock()
        store = WorkQueueStore(cx, clock=clock.now)
        a_id = file_row(store, "corr-A", kind=a_kind)
        b_id = file_row(store, "corr-B")
        c_id = file_row(store, "corr-C")
        assert store.link(b_id, a_id, actor_identity=USER)
        return cx, store, a_id, b_id, c_id

    @pytest.mark.parametrize("stage", _STAGES)
    @pytest.mark.asyncio
    async def test_b_waits_and_unrelated_c_goes_ahead(
        self, db_path: Path, stage: str
    ) -> None:
        cx, store, a_id, b_id, c_id = self._queue(db_path)
        _bring_a_to(cx, store, a_id, stage)
        notifier = Notifier()
        loop = a_loop(
            cx, limit=2, clock=FakeClock(), notifier=notifier, hold_seconds=A_DAY
        )

        await loop.ask_hold_or_go()
        assert await loop.take_next() == c_id

        assert _statuses(cx)["corr-B"] == "QUEUED"
        assert notifier.messages == []
        assert not store.has_event(b_id, "hold_or_go")

    @pytest.mark.asyncio
    async def test_b_is_taken_once_the_publication_is_recorded(
        self, db_path: Path
    ) -> None:
        cx, store, a_id, b_id, _c_id = self._queue(db_path)
        _bring_a_to(cx, store, a_id, "approved, waiting for the merge lock")
        _send(cx, "build-A", published=True)
        loop = a_loop(cx, limit=2, clock=FakeClock(), hold_seconds=A_DAY)

        assert await loop.take_next() == b_id

    @pytest.mark.asyncio
    async def test_work_a_later_press_found_on_the_remote_has_landed(
        self, db_path: Path
    ) -> None:
        """A press that finds the work already published writes a result, not
        a new send line; that result is enough."""
        cx, store, a_id, b_id, _c_id = self._queue(db_path)
        _bring_a_to(cx, store, a_id, "approved, waiting for the merge lock")
        record = PublicationRecordStore(cx)
        grant = record.take_lease(build_id="build-A", holder="press", now=START)
        assert grant is not None
        assert record.record(
            build_id="build-A",
            turn=grant.turn,
            now=START,
            result=RESULT_PUBLISHED_DEPLOYMENT_PENDING,
        )
        loop = a_loop(cx, limit=2, clock=FakeClock(), hold_seconds=A_DAY)

        assert await loop.take_next() == b_id

    @pytest.mark.asyncio
    async def test_a_report_saying_merged_and_running_has_landed(
        self, db_path: Path
    ) -> None:
        cx, store, a_id, b_id, _c_id = self._queue(db_path)
        _bring_a_to(cx, store, a_id, "approved, waiting for the merge lock")
        _report(cx, "build-A", status="PASSED", result=RESULT_WORD_MERGED_AND_RUNNING)
        loop = a_loop(cx, limit=2, clock=FakeClock(), hold_seconds=A_DAY)

        assert await loop.take_next() == b_id

    @pytest.mark.asyncio
    async def test_work_that_lands_after_the_question_still_lets_b_go(
        self, db_path: Path
    ) -> None:
        cx, store, a_id, b_id, c_id = self._queue(db_path)
        _bring_a_to(cx, store, a_id, "merging")
        _report(
            cx, "build-A", status="GATED", result=RESULT_WORD_PUBLICATION_PENDING
        )
        notifier = Notifier()
        loop = a_loop(
            cx, limit=3, clock=FakeClock(), notifier=notifier, hold_seconds=A_DAY
        )
        await loop.ask_hold_or_go()
        assert len(notifier.messages) == 1
        assert await loop.take_next() == c_id

        # The next press publishes it; nobody said "go".
        _report(
            cx,
            "build-A",
            status="PASSED",
            result=RESULT_WORD_PUBLISHED_DEPLOYMENT_PENDING,
        )
        await loop.ask_hold_or_go()
        assert len(notifier.messages) == 1
        assert await loop.take_next() == b_id

    @pytest.mark.asyncio
    async def test_with_the_merge_executor_off_a_complete_build_is_enough(
        self, db_path: Path
    ) -> None:
        """No merge card is ever offered, so finishing is as far as A goes —
        the queue's behaviour from before."""
        cx, store, a_id, b_id, _c_id = self._queue(db_path)
        _bring_a_to(cx, store, a_id, "building")
        cx.execute("UPDATE builds SET status = 'COMPLETE' WHERE build_id = 'build-A'")
        loop = a_loop(cx, limit=2, clock=FakeClock(), merge_executor_enabled=False)

        assert await loop.take_next() == b_id

    @pytest.mark.asyncio
    async def test_a_complete_build_with_no_card_is_asked_about_after_ten_minutes(
        self, db_path: Path
    ) -> None:
        cx, store, a_id, b_id, c_id = self._queue(db_path)
        _bring_a_to(cx, store, a_id, "building")
        cx.execute(
            "UPDATE builds SET status = 'COMPLETE', completed_at = ? "
            "WHERE build_id = 'build-A'",
            (START.isoformat(),),
        )
        clock = FakeClock()
        notifier = Notifier()
        loop = a_loop(cx, limit=3, clock=clock, notifier=notifier)

        # Just finished: the card may be on its way; nothing is asked.
        await loop.ask_hold_or_go()
        assert notifier.messages == []
        assert await loop.take_next() == c_id
        clock.advance(MERGE_CARD_GRACE_SECONDS - 1)
        await loop.ask_hold_or_go()
        assert notifier.messages == []

        clock.advance(1)
        await loop.ask_hold_or_go()
        await loop.ask_hold_or_go()
        assert notifier.messages == [
            f"#{a_id} finished but no merge card was offered for it and "
            f"#{b_id} was waiting on it — hold or go?"
        ]
        assert _statuses(cx)["corr-B"] == "QUEUED"

    @pytest.mark.asyncio
    async def test_a_repair_lands_the_same_way(self, db_path: Path) -> None:
        """A repair's work is its build; B waits for that build's publication."""
        cx, store, a_id, b_id, c_id = self._queue(db_path, a_kind="fix")
        store.admit(a_id, actor_identity=LOOP_ACTOR)
        _insert_build_for(cx, "build-A", "COMPLETE", correlation_id="corr-A")
        store.close(a_id, status="DONE", actor_identity=LOOP_ACTOR)
        _offer_merge_card(cx, "build-A", offered_at=START - timedelta(minutes=5))
        loop = a_loop(cx, limit=3, clock=FakeClock(), hold_seconds=A_DAY)

        assert await loop.take_next() == c_id

        _answer_merge_card(cx, "build-A", answered_at=START, decision="approve")
        _send(cx, "build-A", published=True)
        assert await loop.take_next() == b_id

    @pytest.mark.asyncio
    async def test_a_done_row_whose_work_built_nothing_still_lets_b_go(
        self, db_path: Path
    ) -> None:
        """A question is answered by its planning run and builds nothing; its
        row closing DONE is all there is to wait for, as before."""
        cx, store, a_id, b_id, _c_id = self._queue(db_path, a_kind="question")
        store.admit(a_id, actor_identity=LOOP_ACTOR)
        _insert_run(cx, "corr-A", PlanningState.PLANNED_HANDOFF.value)
        store.close(a_id, status="DONE", actor_identity=LOOP_ACTOR)
        loop = a_loop(cx, limit=2, clock=FakeClock())

        assert await loop.take_next() == b_id

    @pytest.mark.parametrize(
        "what_happened",
        (
            "merge declined",
            "build failed",
            "build cancelled",
            "merge failed",
            "publication pending",
            "publication switched off",
        ),
    )
    @pytest.mark.asyncio
    async def test_a_that_did_not_land_asks_hold_or_go(
        self, db_path: Path, what_happened: str
    ) -> None:
        cx, store, a_id, b_id, c_id = self._queue(db_path)
        _bring_a_to(cx, store, a_id, "building")
        if what_happened == "build failed":
            cx.execute("UPDATE builds SET status = 'FAILED' WHERE build_id = 'build-A'")
        elif what_happened == "build cancelled":
            cx.execute(
                "UPDATE builds SET status = 'CANCELLED' WHERE build_id = 'build-A'"
            )
        else:
            cx.execute(
                "UPDATE builds SET status = 'COMPLETE' WHERE build_id = 'build-A'"
            )
            _offer_merge_card(cx, "build-A", offered_at=START - timedelta(minutes=5))
            if what_happened == "merge declined":
                _answer_merge_card(
                    cx, "build-A", answered_at=START, decision="reject"
                )
            else:
                _answer_merge_card(
                    cx, "build-A", answered_at=START, decision="approve"
                )
                if what_happened == "merge failed":
                    _report(
                        cx, "build-A", status="FAILED", result="merged-verify-failed"
                    )
                elif what_happened == "publication pending":
                    # The publisher refused, could not be reached, or the
                    # attempts ran out: checked, not sent.
                    _report(
                        cx,
                        "build-A",
                        status="GATED",
                        result=RESULT_WORD_PUBLICATION_PENDING,
                    )
                else:
                    # Publication switched off: both checks ran and passed.
                    _report(
                        cx,
                        "build-A",
                        status="PASSED",
                        result=RESULT_WORD_PUBLICATION_PENDING,
                    )
        notifier = Notifier()
        loop = a_loop(
            cx, limit=2, clock=FakeClock(), notifier=notifier, hold_seconds=A_DAY
        )

        await loop.ask_hold_or_go()

        assert len(notifier.messages) == 1
        assert notifier.messages[0].startswith(f"#{a_id} ")
        assert notifier.messages[0].endswith(
            f"and #{b_id} was waiting on it — hold or go?"
        )
        # Held: the unrelated row is taken, B is not.
        assert await loop.take_next() == c_id
        assert _statuses(cx)["corr-B"] == "QUEUED"

        # Go: B is taken once someone puts it next.
        store.promote(b_id, actor_identity=USER)
        assert await loop.take_next() == b_id

    @pytest.mark.asyncio
    async def test_an_unpublished_merge_is_said_plainly(
        self, db_path: Path
    ) -> None:
        cx, store, a_id, b_id, _c_id = self._queue(db_path)
        _bring_a_to(cx, store, a_id, "merging")
        _report(
            cx, "build-A", status="GATED", result=RESULT_WORD_PUBLICATION_PENDING
        )
        notifier = Notifier()
        loop = a_loop(cx, limit=2, clock=FakeClock(), notifier=notifier)

        await loop.ask_hold_or_go()

        assert notifier.messages == [
            f"#{a_id} was approved for merge but not published and "
            f"#{b_id} was waiting on it — hold or go?"
        ]

    @pytest.mark.asyncio
    async def test_a_declined_merge_is_said_as_his_answer_not_as_a_failure(
        self, db_path: Path
    ) -> None:
        cx, store, a_id, b_id, _c_id = self._queue(db_path)
        _bring_a_to(cx, store, a_id, "at its merge card")
        _answer_merge_card(cx, "build-A", answered_at=START, decision="reject")
        notifier = Notifier()
        loop = a_loop(cx, limit=2, clock=FakeClock(), notifier=notifier)

        await loop.ask_hold_or_go()

        assert notifier.messages == [
            f"#{a_id} was not merged (you said no at its merge card) and "
            f"#{b_id} was waiting on it — hold or go?"
        ]

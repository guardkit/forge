"""The take-next loop (Lane B stage one, contracts 6, 7, 8 and 9).

These tests pin the things a person notices about the loop:

- it takes one sentence at a time, and only when the factory has room;
- the run it starts is the sentence's own run — same correlation id, same
  words, same repository, same person (contract 9);
- a row waiting behind one that failed is never taken silently: the forge
  asks once, and holds until told;
- the class order is on trial only — the loop says what it would have picked
  and takes the first-in-first-out row anyway;
- a sentence that has waited a week is asked about once, in one message.

Nothing here opens a socket: the clock, the sleep, the run maker and the
notifier are all injected recorders, the pattern ``tests/bdd/conftest.py``
uses.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations
from forge.pipeline.fix_admission import (
    FixAdmission,
    FixAdmissionRefused,
    FixPublishFailed,
)
from forge.planning.states import PlanningState
from forge.lifecycle.persistence import (
    SqliteLifecyclePersistence,
    StageLogEntry,
)
from forge.pipeline.merge_executor import MERGE_DECISION_TARGET_IDENTIFIER
from forge.pipeline.merge_offer import (
    MERGE_OFFER_STAGE_LABEL,
    MERGE_OFFER_TARGET_IDENTIFIER,
)
from forge.planning.work_queue_loop import (
    LOOP_ACTOR,
    STALE_TICK_INTERVAL_SECONDS,
    Admission,
    WorkQueueLoop,
    count_in_flight,
    first_sentence,
    paused_repositories,
    plain_duration,
    shadow_line,
    unanswered_merge_cards,
)
from forge.planning.work_queue_store import WorkQueueStore

USER = "U-RICH"
THREAD = "1725530000.000100"
START = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Recorders and a clock that never waits
# ---------------------------------------------------------------------------


class FakeClock:
    """A clock the test moves by hand; its ``sleep`` moves it too."""

    def __init__(self, start: datetime = START) -> None:
        self._instant = start
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self._instant

    def advance(self, seconds: float) -> None:
        self._instant = self._instant + timedelta(seconds=seconds)

    def set_to(self, instant: datetime) -> None:
        """Put the clock at an exact moment, in whatever timezone it names."""
        self._instant = instant

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.advance(seconds)


class RunMaker:
    """Stands in for creating and starting the planning run.

    Like the real thing, a run that starts leaves a planning run behind it —
    the loop reads that to know the row is really under way, and puts back any
    admitted row that has none.
    """

    def __init__(
        self,
        *,
        runs: dict[str, dict[str, Any]] | None = None,
        fail_with: Exception | None = None,
    ) -> None:
        self.admissions: list[Admission] = []
        self._runs = runs if runs is not None else {}
        self._fail_with = fail_with

    async def __call__(self, admission: Admission) -> None:
        self.admissions.append(admission)
        if self._fail_with is not None:
            raise self._fail_with
        self._runs.setdefault(
            admission.correlation_id,
            {"state": PlanningState.QUEUED.value, "error": None},
        )


class Notifier:
    """Captures every sentence the loop would have said in Slack."""

    def __init__(self) -> None:
        self.said: list[tuple[str, str, str | None]] = []

    async def __call__(
        self,
        correlation_id: str,
        message: str,
        *,
        parent_request_id: str | None = None,
    ) -> None:
        self.said.append((correlation_id, message, parent_request_id))

    @property
    def messages(self) -> list[str]:
        return [message for _, message, _ in self.said]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def connection(tmp_path: Path) -> sqlite3.Connection:
    cx = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    yield cx
    cx.close()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(connection: sqlite3.Connection, clock: FakeClock) -> WorkQueueStore:
    return WorkQueueStore(connection, clock=clock.now)


@pytest.fixture
def notifier() -> Notifier:
    return Notifier()


@pytest.fixture
def runs() -> dict[str, dict[str, Any]]:
    """Planning runs the loop can read, keyed by correlation id."""
    return {}


def build_loop(
    store: WorkQueueStore,
    *,
    clock: FakeClock,
    notifier: Notifier,
    runs: dict[str, dict[str, Any]],
    run_maker: RunMaker | None = None,
    in_flight: int = 0,
    paused: set[str] | None = None,
    max_in_flight: int = 1,
    stale_after_days: int = 7,
    admit_fix_rows: bool = False,
    fix_maker: Any | None = None,
    builds: dict[str, dict[str, Any]] | None = None,
    republisher: Any | None = None,
    in_flight_fn: Any | None = None,
    merge_cards: Any | None = None,
    merge_offer_hold_seconds: int = 0,
) -> tuple[WorkQueueLoop, RunMaker]:
    maker = run_maker or RunMaker(runs=runs)
    loop = WorkQueueLoop(
        store,
        count_in_flight=in_flight_fn or (lambda: in_flight),
        planning_run=lambda cid: runs.get(cid),
        paused_repositories=lambda: set(paused or set()),
        start_run=maker,
        notify=notifier,
        max_in_flight=max_in_flight,
        stale_after_days=stale_after_days,
        clock=clock.now,
        start_fix=fix_maker,
        fix_build=(lambda cid: (builds or {}).get(cid)) if builds is not None else None,
        republish_build=republisher,
        admit_fix_rows=admit_fix_rows,
        merge_cards=merge_cards,
        merge_offer_hold_seconds=merge_offer_hold_seconds,
    )
    return loop, maker


def file_row(
    store: WorkQueueStore,
    correlation_id: str,
    *,
    sentence: str = "build a login page",
    repo: str | None = "api_test",
    kind: str = "feature",
) -> int:
    return store.file_sentence(
        correlation_id=correlation_id,
        sentence=sentence,
        originating_user=USER,
        target_repo=repo,
        kind=kind,
        parent_request_id=THREAD,
        originating_adapter="slack",
        triggered_by="jarvis",
    ).queue_id


# ---------------------------------------------------------------------------
# How busy the factory is (contract 6)
# ---------------------------------------------------------------------------


class TestTheInFlightCount:
    def test_an_idle_factory_counts_nothing(
        self, connection: sqlite3.Connection
    ) -> None:
        assert count_in_flight(connection) == 0

    def test_a_running_planning_run_counts(
        self, connection: sqlite3.Connection
    ) -> None:
        _insert_run(connection, "plan-1", PlanningState.RUNNING.value)
        assert count_in_flight(connection) == 1

    def test_a_finished_planning_run_does_not_count(
        self, connection: sqlite3.Connection
    ) -> None:
        _insert_run(connection, "plan-1", PlanningState.PLANNED_HANDOFF.value)
        _insert_run(connection, "plan-2", PlanningState.FAILED.value)
        assert count_in_flight(connection) == 0

    def test_a_running_build_counts_and_a_finished_one_does_not(
        self, connection: sqlite3.Connection
    ) -> None:
        _insert_build(connection, "b-1", "RUNNING")
        _insert_build(connection, "b-2", "COMPLETE")
        assert count_in_flight(connection) == 1

    def test_runs_and_builds_are_added_together(
        self, connection: sqlite3.Connection
    ) -> None:
        _insert_run(connection, "plan-1", PlanningState.RUNNING.value)
        _insert_build(connection, "b-1", "QUEUED")
        assert count_in_flight(connection) == 2

    def test_an_interrupted_build_is_not_in_flight(
        self, connection: sqlite3.Connection
    ) -> None:
        """The live ledger of 2026-09-05 held 28 builds marked INTERRUPTED by boot
        recovery. Nothing is happening for them; counting them as in flight kept
        the cap of one full for ever and the first real sentence was never
        admitted. Only the forge's own active states count."""
        for i in range(28):
            _insert_build(connection, f"dead-{i}", "INTERRUPTED")
        _insert_build(connection, "b-skipped", "SKIPPED")
        _insert_build(connection, "b-cancelled", "CANCELLED")
        assert count_in_flight(connection) == 0

    def test_every_active_build_state_counts(
        self, connection: sqlite3.Connection
    ) -> None:
        for i, status in enumerate(["QUEUED", "PREPARING", "RUNNING", "PAUSED", "FINALISING"]):
            _insert_build(connection, f"b-{i}", status)
        assert count_in_flight(connection) == 5

    def test_every_active_planning_state_counts_and_the_rest_do_not(
        self, connection: sqlite3.Connection
    ) -> None:
        active = [
            PlanningState.QUEUED.value,
            PlanningState.RUNNING.value,
            PlanningState.PAUSED.value,
            PlanningState.FEATURE_SPEC.value,
            PlanningState.FEATURE_PLAN.value,
        ]
        for i, state in enumerate(active):
            _insert_run(connection, f"active-{i}", state)
        for i, state in enumerate([
            PlanningState.BUILD_QUEUED.value,
            PlanningState.PLANNED_HANDOFF.value,
            PlanningState.CANCELLED.value,
            PlanningState.TIMED_OUT.value,
        ]):
            _insert_run(connection, f"over-{i}", state)
        assert count_in_flight(connection) == len(active)

    def test_a_paused_run_names_its_repository(
        self, connection: sqlite3.Connection
    ) -> None:
        _insert_run(
            connection, "plan-1", PlanningState.PAUSED.value, target_repo="api_test"
        )
        assert paused_repositories(connection) == {"api_test"}


# ---------------------------------------------------------------------------
# The queue waits for the merge word (spec 2026-09-06)
# ---------------------------------------------------------------------------

#: The window the shipped configuration keeps: a day.
A_DAY = 24 * 60 * 60


def _offer_merge_card(
    connection: sqlite3.Connection,
    build_id: str,
    *,
    offered_at: datetime,
) -> None:
    """Put a merge card on the record the way the real offer does.

    Through ``record_stage`` and the same target name the offer writes, so
    the queue is reading the row the live ledger really has.
    """
    pool = SqliteLifecyclePersistence(connection=connection)
    pool.record_stage(
        StageLogEntry(
            build_id=build_id,
            stage_label=MERGE_OFFER_STAGE_LABEL,
            target_kind="local_tool",
            target_identifier=MERGE_OFFER_TARGET_IDENTIFIER,
            status="GATED",
            gate_mode="MANDATORY_HUMAN_APPROVAL",
            started_at=offered_at,
            completed_at=offered_at,
            duration_secs=0.0,
            details={
                "merge_offer": {
                    "build_id": build_id,
                    "request_id": f"merge-{build_id}",
                    "resume_options": ["approve", "reject"],
                }
            },
        )
    )


def _answer_merge_card(
    connection: sqlite3.Connection,
    build_id: str,
    *,
    answered_at: datetime,
    decision: str = "approve",
) -> None:
    """Record Rich's answer the way the merge executor records it."""
    pool = SqliteLifecyclePersistence(connection=connection)
    pool.record_stage(
        StageLogEntry(
            build_id=build_id,
            stage_label=MERGE_OFFER_STAGE_LABEL,
            target_kind="local_tool",
            target_identifier=MERGE_DECISION_TARGET_IDENTIFIER,
            status="PASSED" if decision == "approve" else "SKIPPED",
            gate_mode="MANDATORY_HUMAN_APPROVAL",
            started_at=answered_at,
            completed_at=answered_at,
            duration_secs=0.0,
            details={"merge_decision": {"decision": decision, "decided_by": USER}},
        )
    )


def _finished_build_with_an_open_card(
    connection: sqlite3.Connection,
    *,
    build_id: str = "build-3ABD",
    feature_id: str = "FEAT-3ABD",
    offered_at: datetime,
) -> None:
    """A build that finished clean and whose merge card is still waiting."""
    _insert_build(connection, build_id, "COMPLETE", feature_id=feature_id)
    _offer_merge_card(connection, build_id, offered_at=offered_at)


class TestAnOpenMergeCardIsWorkInFlight:
    """Rich's rule is one piece of work at a time, and merge means merge and
    deploy (his decision of 24 August 2026). A build that finished clean but
    whose card has not been pressed has not landed on main, so it still
    counts."""

    def test_a_card_waiting_for_a_press_counts_as_one_piece_of_work(
        self, connection: sqlite3.Connection
    ) -> None:
        _finished_build_with_an_open_card(
            connection, offered_at=START - timedelta(minutes=12)
        )

        assert (
            count_in_flight(
                connection, merge_offer_hold_seconds=A_DAY, now=START
            )
            == 1
        )

    def test_without_the_setting_a_card_holds_nothing(
        self, connection: sqlite3.Connection
    ) -> None:
        """Nought seconds means do not wait for a card at all — and nought is
        what every existing caller of ``count_in_flight`` still passes."""
        _finished_build_with_an_open_card(
            connection, offered_at=START - timedelta(minutes=12)
        )

        assert count_in_flight(connection) == 0
        assert (
            count_in_flight(connection, merge_offer_hold_seconds=0, now=START) == 0
        )

    def test_a_pressed_card_holds_nothing(
        self, connection: sqlite3.Connection
    ) -> None:
        """MUTATION CHECK — remove the answered-card exclusion from the query
        and this goes red."""
        _finished_build_with_an_open_card(
            connection, offered_at=START - timedelta(minutes=12)
        )
        _answer_merge_card(
            connection,
            "build-3ABD",
            answered_at=START - timedelta(minutes=1),
            decision="approve",
        )

        assert (
            count_in_flight(
                connection, merge_offer_hold_seconds=A_DAY, now=START
            )
            == 0
        )
        assert unanswered_merge_cards(connection) == []

    def test_a_rejected_card_holds_nothing_either(
        self, connection: sqlite3.Connection
    ) -> None:
        """Reject is an answer too: the executor writes SKIPPED, and either
        way the card is done with."""
        _finished_build_with_an_open_card(
            connection, offered_at=START - timedelta(minutes=12)
        )
        _answer_merge_card(
            connection,
            "build-3ABD",
            answered_at=START - timedelta(minutes=1),
            decision="reject",
        )

        assert (
            count_in_flight(
                connection, merge_offer_hold_seconds=A_DAY, now=START
            )
            == 0
        )

    def test_a_card_older_than_the_window_holds_nothing(
        self, connection: sqlite3.Connection
    ) -> None:
        """MUTATION CHECK — remove the window from the query and this goes
        red: an unanswered card would hold the queue for ever."""
        _finished_build_with_an_open_card(
            connection, offered_at=START - timedelta(hours=25)
        )

        assert (
            count_in_flight(
                connection, merge_offer_hold_seconds=A_DAY, now=START
            )
            == 0
        )
        # It is still unanswered — it just no longer holds anything.
        assert [card.feature_id for card in unanswered_merge_cards(connection)] == [
            "FEAT-3ABD"
        ]

    def test_a_card_just_inside_the_window_still_holds(
        self, connection: sqlite3.Connection
    ) -> None:
        _finished_build_with_an_open_card(
            connection, offered_at=START - timedelta(hours=23)
        )

        assert (
            count_in_flight(
                connection, merge_offer_hold_seconds=A_DAY, now=START
            )
            == 1
        )

    def test_two_offers_for_one_build_count_once(
        self, connection: sqlite3.Connection
    ) -> None:
        _finished_build_with_an_open_card(
            connection, offered_at=START - timedelta(minutes=12)
        )
        _offer_merge_card(
            connection, "build-3ABD", offered_at=START - timedelta(minutes=2)
        )

        assert (
            count_in_flight(
                connection, merge_offer_hold_seconds=A_DAY, now=START
            )
            == 1
        )

    def test_the_card_is_added_to_the_runs_and_builds_already_counted(
        self, connection: sqlite3.Connection
    ) -> None:
        _insert_run(connection, "plan-1", PlanningState.RUNNING.value)
        _finished_build_with_an_open_card(
            connection, offered_at=START - timedelta(minutes=12)
        )

        assert (
            count_in_flight(
                connection, merge_offer_hold_seconds=A_DAY, now=START
            )
            == 2
        )

    def test_the_earliest_offer_is_when_the_card_started_waiting(
        self, connection: sqlite3.Connection
    ) -> None:
        _finished_build_with_an_open_card(
            connection, offered_at=START - timedelta(minutes=12)
        )
        _offer_merge_card(
            connection, "build-3ABD", offered_at=START - timedelta(minutes=2)
        )

        cards = unanswered_merge_cards(connection)
        assert [card.offered_at for card in cards] == [START - timedelta(minutes=12)]


class TestTheQueueWaitsForTheMergeWord:
    """The three outcomes, driven through the loop on a real database."""

    def _loop_over(
        self,
        connection: sqlite3.Connection,
        store: WorkQueueStore,
        clock: FakeClock,
        notifier: Notifier,
        runs: dict,
        *,
        hold_seconds: int = A_DAY,
    ) -> tuple[WorkQueueLoop, RunMaker]:
        return build_loop(
            store,
            clock=clock,
            notifier=notifier,
            runs=runs,
            in_flight_fn=lambda: count_in_flight(
                connection,
                merge_offer_hold_seconds=hold_seconds,
                now=clock.now(),
            ),
            merge_cards=lambda: unanswered_merge_cards(connection),
            merge_offer_hold_seconds=hold_seconds,
        )

    @pytest.mark.asyncio
    async def test_held_the_queue_names_the_card_it_is_waiting_for(
        self,
        connection: sqlite3.Connection,
        store: WorkQueueStore,
        clock: FakeClock,
        notifier: Notifier,
        runs: dict,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _finished_build_with_an_open_card(
            connection, offered_at=START - timedelta(minutes=12)
        )
        file_row(store, "plan-1")
        loop, maker = self._loop_over(connection, store, clock, notifier, runs)

        with caplog.at_level("INFO", logger="forge.planning.work_queue_loop"):
            await loop.tick()

        assert maker.admissions == []
        assert (
            "work queue: holding — the merge card for FEAT-3ABD is waiting "
            "for a press (offered 12 minutes ago)" in caplog.text
        )

    @pytest.mark.asyncio
    async def test_held_the_line_is_said_once_not_every_tick(
        self,
        connection: sqlite3.Connection,
        store: WorkQueueStore,
        clock: FakeClock,
        notifier: Notifier,
        runs: dict,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _finished_build_with_an_open_card(
            connection, offered_at=START - timedelta(minutes=12)
        )
        file_row(store, "plan-1")
        loop, _ = self._loop_over(connection, store, clock, notifier, runs)

        with caplog.at_level("INFO", logger="forge.planning.work_queue_loop"):
            await loop.tick()
            await loop.tick()
            await loop.tick()

        said = [
            line
            for line in caplog.text.splitlines()
            if "is waiting for a press" in line
        ]
        assert len(said) == 1

    @pytest.mark.asyncio
    async def test_released_by_a_press_the_queue_takes_the_next_one(
        self,
        connection: sqlite3.Connection,
        store: WorkQueueStore,
        clock: FakeClock,
        notifier: Notifier,
        runs: dict,
    ) -> None:
        _finished_build_with_an_open_card(
            connection, offered_at=START - timedelta(minutes=12)
        )
        file_row(store, "plan-1")
        loop, maker = self._loop_over(connection, store, clock, notifier, runs)
        await loop.tick()
        assert maker.admissions == []

        _answer_merge_card(connection, "build-3ABD", answered_at=START)
        await loop.tick()

        assert [a.correlation_id for a in maker.admissions] == ["plan-1"]

    @pytest.mark.asyncio
    async def test_released_by_lapse_the_queue_says_so_once_and_moves_on(
        self,
        connection: sqlite3.Connection,
        store: WorkQueueStore,
        clock: FakeClock,
        notifier: Notifier,
        runs: dict,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _finished_build_with_an_open_card(
            connection, offered_at=START - timedelta(minutes=12)
        )
        file_row(store, "plan-1")
        loop, maker = self._loop_over(connection, store, clock, notifier, runs)
        await loop.tick()
        assert maker.admissions == []

        clock.advance(A_DAY)
        with caplog.at_level("INFO", logger="forge.planning.work_queue_loop"):
            await loop.tick()
            await loop.tick()

        assert [a.correlation_id for a in maker.admissions] == ["plan-1"]
        lapsed = [
            line
            for line in caplog.text.splitlines()
            if "was not answered within" in line
        ]
        assert len(lapsed) == 1
        assert (
            "work queue: the merge card for FEAT-3ABD was not answered within "
            "a day; the queue moves on. Its branch is kept." in caplog.text
        )

    @pytest.mark.asyncio
    async def test_a_lapsed_card_says_nothing_about_a_branch_being_lost(
        self,
        connection: sqlite3.Connection,
        store: WorkQueueStore,
        clock: FakeClock,
        notifier: Notifier,
        runs: dict,
    ) -> None:
        """Nothing is cancelled and nothing is said in the channel: the loop
        only stops waiting."""
        _finished_build_with_an_open_card(
            connection, offered_at=START - timedelta(hours=25)
        )
        file_row(store, "plan-1")
        loop, _ = self._loop_over(connection, store, clock, notifier, runs)

        await loop.tick()

        assert notifier.messages == []
        assert unanswered_merge_cards(connection) != []

    @pytest.mark.asyncio
    async def test_nought_seconds_means_the_queue_never_waits_for_a_card(
        self,
        connection: sqlite3.Connection,
        store: WorkQueueStore,
        clock: FakeClock,
        notifier: Notifier,
        runs: dict,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _finished_build_with_an_open_card(
            connection, offered_at=START - timedelta(minutes=12)
        )
        file_row(store, "plan-1")
        loop, maker = self._loop_over(
            connection, store, clock, notifier, runs, hold_seconds=0
        )

        with caplog.at_level("INFO", logger="forge.planning.work_queue_loop"):
            await loop.tick()

        assert [a.correlation_id for a in maker.admissions] == ["plan-1"]
        assert "is waiting for a press" not in caplog.text

    @pytest.mark.asyncio
    async def test_the_old_counting_line_is_still_said_when_no_card_is_open(
        self,
        store: WorkQueueStore,
        clock: FakeClock,
        notifier: Notifier,
        runs: dict,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Rule 5 — nothing else moves. A busy factory with no open card says
        exactly what it said before."""
        file_row(store, "plan-1")
        loop, maker = build_loop(
            store, clock=clock, notifier=notifier, runs=runs, in_flight=1
        )

        with caplog.at_level("INFO", logger="forge.planning.work_queue_loop"):
            await loop.tick()

        assert maker.admissions == []
        assert (
            "work queue: holding — 1 piece(s) of work in flight against a "
            "cap of 1" in caplog.text
        )

    def test_the_window_is_said_in_plain_words(self) -> None:
        assert plain_duration(A_DAY) == "a day"
        assert plain_duration(2 * A_DAY) == "2 days"
        assert plain_duration(3600) == "an hour"
        assert plain_duration(1800) == "30 minutes"


# ---------------------------------------------------------------------------
# Taking the next one (contracts 6 and 9)
# ---------------------------------------------------------------------------


class TestTakingTheNextOne:
    @pytest.mark.asyncio
    async def test_an_empty_queue_takes_nothing(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        loop, maker = build_loop(store, clock=clock, notifier=notifier, runs=runs)
        assert await loop.take_next() is None
        assert maker.admissions == []

    @pytest.mark.asyncio
    async def test_the_first_sentence_is_admitted_on_the_next_tick(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        queue_id = file_row(store, "plan-1")
        loop, maker = build_loop(store, clock=clock, notifier=notifier, runs=runs)

        await loop.tick()

        row = store.get(queue_id)
        assert row is not None and row["status"] == "ADMITTED"
        assert row["admitted_at"] is not None
        assert len(maker.admissions) == 1

    @pytest.mark.asyncio
    async def test_the_run_carries_the_sentences_own_facts(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        """Contract 9 — the four facts the admitted run must carry."""
        file_row(store, "plan-1", sentence="build a login page", repo="api_test")
        loop, maker = build_loop(store, clock=clock, notifier=notifier, runs=runs)

        await loop.take_next()

        admission = maker.admissions[0]
        assert admission.correlation_id == "plan-1"
        assert admission.request_text == "build a login page"
        assert admission.target_repo == "api_test"
        assert admission.originating_user == USER

    @pytest.mark.asyncio
    async def test_the_run_keeps_the_slack_thread_it_arrived_in(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        file_row(store, "plan-1")
        loop, maker = build_loop(store, clock=clock, notifier=notifier, runs=runs)

        await loop.take_next()

        admission = maker.admissions[0]
        assert admission.parent_request_id == THREAD
        assert admission.originating_adapter == "slack"
        assert admission.triggered_by == "jarvis"

    @pytest.mark.asyncio
    async def test_nothing_is_taken_while_the_factory_is_busy(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        queue_id = file_row(store, "plan-1")
        loop, maker = build_loop(
            store, clock=clock, notifier=notifier, runs=runs, in_flight=1
        )

        assert await loop.take_next() is None
        row = store.get(queue_id)
        assert row is not None and row["status"] == "QUEUED"
        assert maker.admissions == []

    @pytest.mark.asyncio
    async def test_a_second_cap_lets_a_second_one_through(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        file_row(store, "plan-1")
        loop, maker = build_loop(
            store,
            clock=clock,
            notifier=notifier,
            runs=runs,
            in_flight=1,
            max_in_flight=2,
        )

        assert await loop.take_next() is not None
        assert len(maker.admissions) == 1

    @pytest.mark.asyncio
    async def test_only_one_is_taken_per_tick(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        file_row(store, "plan-1")
        file_row(store, "plan-2")
        loop, maker = build_loop(store, clock=clock, notifier=notifier, runs=runs)

        await loop.tick()

        assert len(maker.admissions) == 1
        assert maker.admissions[0].correlation_id == "plan-1"

    @pytest.mark.asyncio
    async def test_the_lowest_rank_goes_first(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        file_row(store, "plan-1")
        second = file_row(store, "plan-2")
        store.promote(second, actor_identity=USER)
        loop, maker = build_loop(store, clock=clock, notifier=notifier, runs=runs)

        assert await loop.take_next() == second

    @pytest.mark.asyncio
    async def test_a_row_that_cannot_start_goes_back_in_the_queue(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        queue_id = file_row(store, "plan-1")
        loop, _ = build_loop(
            store,
            clock=clock,
            notifier=notifier,
            runs=runs,
            run_maker=RunMaker(fail_with=RuntimeError("the driver is asleep")),
        )

        assert await loop.take_next() is None
        row = store.get(queue_id)
        assert row is not None and row["status"] == "QUEUED"


# ---------------------------------------------------------------------------
# Waiting on another row (contract 6)
# ---------------------------------------------------------------------------


class TestWaitingOnAnotherRow:
    @pytest.mark.asyncio
    async def test_a_linked_row_waits_until_the_first_is_done(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        first = file_row(store, "plan-1")
        second = file_row(store, "plan-2")
        store.link(second, first, actor_identity=USER)
        store.promote(second, actor_identity=USER)  # in front, but still waiting
        loop, _ = build_loop(store, clock=clock, notifier=notifier, runs=runs)

        assert await loop.take_next() == first

    @pytest.mark.asyncio
    async def test_it_is_taken_once_the_first_is_done(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        first = file_row(store, "plan-1")
        second = file_row(store, "plan-2")
        store.link(second, first, actor_identity=USER)
        store.admit(first, actor_identity=LOOP_ACTOR)
        store.close(first, status="DONE", actor_identity=LOOP_ACTOR)
        loop, _ = build_loop(store, clock=clock, notifier=notifier, runs=runs)

        assert await loop.take_next() == second


# ---------------------------------------------------------------------------
# Hold or go (contract 6)
# ---------------------------------------------------------------------------


class TestHoldOrGo:
    def _broken_chain(self, store: WorkQueueStore) -> tuple[int, int]:
        first = file_row(store, "plan-1")
        second = file_row(store, "plan-2")
        store.link(second, first, actor_identity=USER)
        store.admit(first, actor_identity=LOOP_ACTOR)
        store.close(
            first, status="BLOCKED", actor_identity=LOOP_ACTOR, reason="it fell over"
        )
        return first, second

    @pytest.mark.asyncio
    async def test_the_forge_asks_hold_or_go(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        first, second = self._broken_chain(store)
        loop, _ = build_loop(store, clock=clock, notifier=notifier, runs=runs)

        await loop.ask_hold_or_go()

        assert notifier.messages == [
            f"#{first} failed and #{second} was waiting on it — hold or go?"
        ]

    @pytest.mark.asyncio
    async def test_it_asks_only_once(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        self._broken_chain(store)
        loop, _ = build_loop(store, clock=clock, notifier=notifier, runs=runs)

        await loop.ask_hold_or_go()
        await loop.ask_hold_or_go()

        assert len(notifier.messages) == 1

    @pytest.mark.asyncio
    async def test_a_withdrawn_antecedent_asks_the_same_question(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        first = file_row(store, "plan-1")
        second = file_row(store, "plan-2")
        store.link(second, first, actor_identity=USER)
        store.drop(first, actor_identity=USER)
        loop, _ = build_loop(store, clock=clock, notifier=notifier, runs=runs)

        await loop.ask_hold_or_go()

        assert notifier.messages == [
            f"#{first} failed and #{second} was waiting on it — hold or go?"
        ]

    @pytest.mark.asyncio
    async def test_it_holds_until_someone_says_go(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        _, second = self._broken_chain(store)
        loop, maker = build_loop(store, clock=clock, notifier=notifier, runs=runs)

        await loop.tick()
        assert maker.admissions == []
        row = store.get(second)
        assert row is not None and row["status"] == "QUEUED"

    @pytest.mark.asyncio
    async def test_go_means_promote(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        _, second = self._broken_chain(store)
        loop, maker = build_loop(store, clock=clock, notifier=notifier, runs=runs)
        await loop.tick()

        store.promote(second, actor_identity=USER)  # "#2 next"
        await loop.tick()

        assert [a.queue_id for a in maker.admissions] == [second]

    @pytest.mark.asyncio
    async def test_drop_ends_it(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        _, second = self._broken_chain(store)
        loop, maker = build_loop(store, clock=clock, notifier=notifier, runs=runs)
        await loop.tick()

        store.drop(second, actor_identity=USER)  # "drop 2"
        await loop.tick()

        assert maker.admissions == []
        row = store.get(second)
        assert row is not None and row["status"] == "WITHDRAWN"


# ---------------------------------------------------------------------------
# Closing a row when its run ends (contract 6)
# ---------------------------------------------------------------------------


class TestClosingAdmittedRows:
    @pytest.mark.asyncio
    async def test_a_run_that_ended_well_closes_the_row_done(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        queue_id = file_row(store, "plan-1")
        loop, _ = build_loop(store, clock=clock, notifier=notifier, runs=runs)
        await loop.take_next()

        runs["plan-1"] = {"state": PlanningState.PLANNED_HANDOFF.value, "error": None}
        loop.close_finished()

        row = store.get(queue_id)
        assert row is not None and row["status"] == "DONE"
        assert row["closed_at"] is not None

    @pytest.mark.asyncio
    async def test_the_target_terminal_also_closes_the_row_done(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        queue_id = file_row(store, "plan-1")
        loop, _ = build_loop(store, clock=clock, notifier=notifier, runs=runs)
        await loop.take_next()

        runs["plan-1"] = {"state": PlanningState.BUILD_QUEUED.value, "error": None}
        loop.close_finished()

        row = store.get(queue_id)
        assert row is not None and row["status"] == "DONE"

    @pytest.mark.asyncio
    async def test_a_run_that_failed_blocks_the_row_with_the_reason(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        queue_id = file_row(store, "plan-1")
        loop, _ = build_loop(store, clock=clock, notifier=notifier, runs=runs)
        await loop.take_next()

        runs["plan-1"] = {
            "state": PlanningState.FAILED.value,
            "error": "the plan seat never answered",
        }
        loop.close_finished()

        row = store.get(queue_id)
        assert row is not None and row["status"] == "BLOCKED"
        assert row["closed_reason"] == "the plan seat never answered"

    @pytest.mark.asyncio
    async def test_a_cancelled_run_says_so_in_plain_words(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        queue_id = file_row(store, "plan-1")
        loop, _ = build_loop(store, clock=clock, notifier=notifier, runs=runs)
        await loop.take_next()

        runs["plan-1"] = {"state": PlanningState.CANCELLED.value, "error": None}
        loop.close_finished()

        row = store.get(queue_id)
        assert row is not None
        assert row["closed_reason"] == "the planning run was cancelled"

    @pytest.mark.asyncio
    async def test_a_run_still_going_leaves_the_row_alone(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        queue_id = file_row(store, "plan-1")
        loop, _ = build_loop(store, clock=clock, notifier=notifier, runs=runs)
        await loop.take_next()

        runs["plan-1"] = {"state": PlanningState.RUNNING.value, "error": None}
        loop.close_finished()

        row = store.get(queue_id)
        assert row is not None and row["status"] == "ADMITTED"

    @pytest.mark.asyncio
    async def test_the_next_one_goes_once_the_first_has_closed(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        file_row(store, "plan-1")
        file_row(store, "plan-2")
        busy = {"count": 0}
        maker = RunMaker(runs=runs)
        loop = WorkQueueLoop(
            store,
            count_in_flight=lambda: busy["count"],
            planning_run=lambda cid: runs.get(cid),
            paused_repositories=set,
            start_run=maker,
            notify=notifier,
            clock=clock.now,
        )

        await loop.tick()
        busy["count"] = 1
        await loop.tick()
        assert len(maker.admissions) == 1

        runs["plan-1"] = {"state": PlanningState.PLANNED_HANDOFF.value, "error": None}
        busy["count"] = 0
        await loop.tick()

        assert [a.correlation_id for a in maker.admissions] == ["plan-1", "plan-2"]


# ---------------------------------------------------------------------------
# The shadow order (contract 7)
# ---------------------------------------------------------------------------


class TestTheShadowOrder:
    @pytest.mark.asyncio
    async def test_it_says_what_it_would_have_picked_and_takes_the_first(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        first = file_row(store, "plan-1", kind="feature")
        fix = file_row(store, "plan-2", kind="fix")
        loop, maker = build_loop(store, clock=clock, notifier=notifier, runs=runs)

        taken = await loop.take_next()

        assert taken == first
        assert maker.admissions[0].queue_id == first
        assert notifier.messages == [
            f"next I'd pick #{fix} (fix · api_test), because fixes go first; "
            f"taking #{first} as things stand."
        ]

    @pytest.mark.asyncio
    async def test_the_pick_is_written_against_the_admitted_row(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        first = file_row(store, "plan-1", kind="feature")
        fix = file_row(store, "plan-2", kind="fix")
        loop, _ = build_loop(store, clock=clock, notifier=notifier, runs=runs)

        await loop.take_next()

        actions = [str(e["action"]) for e in store.list_events(first)]
        assert "shadow_pick" in actions
        recorded = [e for e in store.list_events(first) if e["action"] == "shadow_pick"]
        assert f'"shadow": {fix}' in str(recorded[0]["details_json"])

    @pytest.mark.asyncio
    async def test_it_says_nothing_when_the_two_agree(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        """Repairs admitted, the fix row is first anyway — nothing to say.

        ``admit_fix_rows`` is on here because with it off the two picks
        genuinely disagree: the class order wants the repair and the queue
        may not start one. That disagreement is worth a line, and it has its
        own test below.
        """
        file_row(store, "plan-1", kind="fix")
        file_row(store, "plan-2", kind="feature")
        loop, _ = build_loop(
            store,
            clock=clock,
            notifier=notifier,
            runs=runs,
            admit_fix_rows=True,
            fix_maker=RunMaker(),
            builds={},
        )

        await loop.take_next()

        assert notifier.messages == []

    @pytest.mark.asyncio
    async def test_it_says_nothing_when_only_one_row_is_open(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        file_row(store, "plan-1", kind="feature")
        loop, _ = build_loop(store, clock=clock, notifier=notifier, runs=runs)

        await loop.take_next()

        assert notifier.messages == []

    @pytest.mark.asyncio
    async def test_a_repository_with_a_card_waiting_comes_before_a_feature(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        first = file_row(store, "plan-1", kind="feature", repo="api_test")
        waiting = file_row(store, "plan-2", kind="feature", repo="office")
        loop, _ = build_loop(
            store, clock=clock, notifier=notifier, runs=runs, paused={"office"}
        )

        await loop.take_next()

        assert notifier.messages == [
            f"next I'd pick #{waiting} (feature · office), because its "
            f"repository has a card waiting on you; taking #{first} as things "
            f"stand."
        ]

    @pytest.mark.asyncio
    async def test_a_question_comes_last(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        question = file_row(store, "plan-1", kind="question")
        feature = file_row(store, "plan-2", kind="feature")
        loop, _ = build_loop(store, clock=clock, notifier=notifier, runs=runs)

        await loop.take_next()

        assert notifier.messages == [
            f"next I'd pick #{feature} (feature · api_test), because features "
            f"come before questions; taking #{question} as things stand."
        ]

    @pytest.mark.asyncio
    async def test_it_never_acts_on_the_pick(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        first = file_row(store, "plan-1", kind="feature")
        fix = file_row(store, "plan-2", kind="fix")
        loop, maker = build_loop(store, clock=clock, notifier=notifier, runs=runs)

        await loop.take_next()

        assert maker.admissions[0].queue_id == first
        fix_row = store.get(fix)
        assert fix_row is not None and fix_row["status"] == "QUEUED"

    def test_the_line_reads_as_the_spec_writes_it(self) -> None:
        from forge.planning.work_queue_loop import Pick

        pick = Pick(
            queue_id=7, kind="fix", target_repo="api_test", reason="fixes go first"
        )
        assert shadow_line(pick, 4) == (
            "next I'd pick #7 (fix · api_test), because fixes go first; "
            "taking #4 as things stand."
        )


# ---------------------------------------------------------------------------
# Staleness (contract 8)
# ---------------------------------------------------------------------------


class TestStaleness:
    @pytest.mark.asyncio
    async def test_a_fresh_queue_is_left_alone(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        file_row(store, "plan-1")
        loop, _ = build_loop(store, clock=clock, notifier=notifier, runs=runs)

        assert await loop.stale_tick() == []
        assert notifier.messages == []

    @pytest.mark.asyncio
    async def test_old_rows_arrive_in_one_message_that_ends_as_written(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        first = file_row(store, "plan-1")
        second = file_row(store, "plan-2")
        loop, _ = build_loop(store, clock=clock, notifier=notifier, runs=runs)
        clock.advance(8 * 24 * 60 * 60)

        asked = await loop.stale_tick()

        assert asked == [first, second]
        assert len(notifier.messages) == 1
        message = notifier.messages[0]
        assert f"#{first}" in message and f"#{second}" in message
        assert "8 days ago" in message
        assert message.endswith(
            'Reply "keep <n>" or "drop <n>", or ignore me and '
            "I'll ask again next week."
        )

    @pytest.mark.asyncio
    async def test_it_does_not_ask_twice_in_the_same_week(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        file_row(store, "plan-1")
        loop, _ = build_loop(store, clock=clock, notifier=notifier, runs=runs)
        clock.advance(8 * 24 * 60 * 60)

        await loop.stale_tick()
        await loop.stale_tick()

        assert len(notifier.messages) == 1

    @pytest.mark.asyncio
    async def test_keep_resets_the_clock_and_counts_the_keep(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        queue_id = file_row(store, "plan-1")
        loop, _ = build_loop(store, clock=clock, notifier=notifier, runs=runs)
        clock.advance(8 * 24 * 60 * 60)
        await loop.stale_tick()

        store.keep(queue_id, actor_identity=USER)
        row = store.get(queue_id)
        assert row is not None and row["keep_count"] == 1

        clock.advance(24 * 60 * 60)
        assert await loop.stale_tick() == []

        clock.advance(8 * 24 * 60 * 60)
        assert await loop.stale_tick() == [queue_id]

    @pytest.mark.asyncio
    async def test_an_admitted_row_is_never_asked_about(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        file_row(store, "plan-1")
        loop, _ = build_loop(store, clock=clock, notifier=notifier, runs=runs)
        await loop.take_next()
        clock.advance(30 * 24 * 60 * 60)

        assert await loop.stale_tick() == []


# ---------------------------------------------------------------------------
# The loop that runs forever
# ---------------------------------------------------------------------------


class TestRunningForever:
    @pytest.mark.asyncio
    async def test_it_sleeps_ten_seconds_between_ticks(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        loop, _ = build_loop(store, clock=clock, notifier=notifier, runs=runs)

        await loop.run(sleep=clock.sleep, iterations=3)

        assert clock.slept == [10.0, 10.0, 10.0]

    @pytest.mark.asyncio
    async def test_a_sentence_filed_between_ticks_is_taken_on_the_next_one(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        loop, maker = build_loop(store, clock=clock, notifier=notifier, runs=runs)
        await loop.run(sleep=clock.sleep, iterations=1)
        assert maker.admissions == []

        file_row(store, "plan-1")
        await loop.run(sleep=clock.sleep, iterations=1)

        assert [a.correlation_id for a in maker.admissions] == ["plan-1"]

    @pytest.mark.asyncio
    async def test_the_weekly_tick_asks_about_an_old_row(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        file_row(store, "plan-1")
        loop, _ = build_loop(
            store, clock=clock, notifier=notifier, runs=runs, in_flight=1
        )

        # Each iteration sleeps a day, so the second week's tick lands.
        await loop.run(
            sleep=clock.sleep,
            interval_seconds=24 * 60 * 60,
            stale_interval_seconds=7 * 24 * 60 * 60,
            iterations=10,
        )

        stale_messages = [m for m in notifier.messages if "waiting a while" in m]
        assert len(stale_messages) == 1

    @pytest.mark.asyncio
    async def test_one_bad_tick_does_not_stop_the_loop(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        loop, _ = build_loop(store, clock=clock, notifier=notifier, runs=runs)
        calls = {"n": 0}

        async def explode() -> None:
            calls["n"] += 1
            raise RuntimeError("boom")

        loop.tick = explode  # type: ignore[method-assign]
        await loop.run(sleep=clock.sleep, iterations=3)

        assert calls["n"] == 3


# ---------------------------------------------------------------------------
# The fix branch (conductor rewire spec 2026-09-05, rules 2 and 4)
# ---------------------------------------------------------------------------


class TestTheFixBranch:
    """A repair is not a planning run, and it is shut by default.

    ``conductor.admit_fix_rows`` is False in the shipped configuration, so a
    repair row is FILED and LEFT: it can be listed, it can be named by the
    "next I'd pick" line, and nothing about it starts. With the owner's word
    the same row goes to the fix journey — never to the planning-run path.
    """

    @pytest.mark.asyncio
    async def test_a_repair_is_left_alone_while_repairs_are_shut(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        queue_id = file_row(store, "fix-build-1", kind="fix")
        fix_maker = RunMaker()
        loop, maker = build_loop(
            store,
            clock=clock,
            notifier=notifier,
            runs=runs,
            fix_maker=fix_maker,
            builds={},
        )

        assert await loop.take_next() is None

        row = store.get(queue_id)
        assert row is not None and row["status"] == "QUEUED"
        assert maker.admissions == []
        assert fix_maker.admissions == []

    @pytest.mark.asyncio
    async def test_the_shadow_line_still_names_the_repair_that_is_waiting(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        """Shut does not mean silent: the queue says what it would pick."""
        feature = file_row(store, "plan-1", kind="feature")
        fix = file_row(store, "fix-build-1", kind="fix")
        loop, maker = build_loop(
            store, clock=clock, notifier=notifier, runs=runs, builds={}
        )

        taken = await loop.take_next()

        assert taken == feature
        assert notifier.messages == [
            f"next I'd pick #{fix} (fix · api_test), because fixes go first; "
            f"taking #{feature} as things stand."
        ]

    @pytest.mark.asyncio
    async def test_with_the_word_given_the_repair_goes_to_the_fix_journey(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        queue_id = file_row(store, "fix-build-1", kind="fix")
        fix_maker = RunMaker()
        loop, maker = build_loop(
            store,
            clock=clock,
            notifier=notifier,
            runs=runs,
            admit_fix_rows=True,
            fix_maker=fix_maker,
            builds={},
        )

        assert await loop.take_next() == queue_id

        # The fix journey opened it; the planning-run path never saw it.
        assert [a.queue_id for a in fix_maker.admissions] == [queue_id]
        assert maker.admissions == []
        assert fix_maker.admissions[0].kind == "fix"
        assert fix_maker.admissions[0].correlation_id == "fix-build-1"
        row = store.get(queue_id)
        assert row is not None and row["status"] == "ADMITTED"

    @pytest.mark.asyncio
    async def test_a_repair_never_creates_a_planning_run(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        file_row(store, "fix-build-1", kind="fix")
        loop, _ = build_loop(
            store,
            clock=clock,
            notifier=notifier,
            runs=runs,
            admit_fix_rows=True,
            fix_maker=RunMaker(),
            builds={},
        )

        await loop.take_next()

        assert runs == {}

    @pytest.mark.asyncio
    async def test_a_feature_beside_a_repair_is_still_taken_while_repairs_are_shut(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        """The queue does not stop behind a row it may not start."""
        file_row(store, "fix-build-1", kind="fix")
        feature = file_row(store, "plan-1", kind="feature")
        loop, maker = build_loop(
            store, clock=clock, notifier=notifier, runs=runs, builds={}
        )

        assert await loop.take_next() == feature
        assert [a.queue_id for a in maker.admissions] == [feature]

    @pytest.mark.asyncio
    async def test_a_repair_closes_on_its_build_not_on_a_planning_run(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        builds: dict[str, dict[str, Any]] = {}
        queue_id = file_row(store, "fix-build-1", kind="fix")
        loop, _ = build_loop(
            store,
            clock=clock,
            notifier=notifier,
            runs=runs,
            admit_fix_rows=True,
            fix_maker=RunMaker(),
            builds=builds,
        )
        await loop.take_next()

        builds["fix-build-1"] = {"status": "RUNNING", "error": None}
        loop.close_finished()
        assert store.get(queue_id)["status"] == "ADMITTED"

        builds["fix-build-1"] = {"status": "COMPLETE", "error": None}
        loop.close_finished()
        assert store.get(queue_id)["status"] == "DONE"

    @pytest.mark.asyncio
    async def test_a_failed_journey_blocks_the_row_in_its_own_words(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        builds: dict[str, dict[str, Any]] = {}
        queue_id = file_row(store, "fix-build-1", kind="fix")
        loop, _ = build_loop(
            store,
            clock=clock,
            notifier=notifier,
            runs=runs,
            admit_fix_rows=True,
            fix_maker=RunMaker(),
            builds=builds,
        )
        await loop.take_next()

        builds["fix-build-1"] = {"status": "FAILED", "error": None}
        loop.close_finished()

        row = store.get(queue_id)
        assert row["status"] == "BLOCKED"
        assert row["closed_reason"] == "the fix journey failed"

    @pytest.mark.asyncio
    async def test_a_repair_admitted_with_no_build_behind_it_is_put_back(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        """The forge stopped between admitting the row and opening the build."""
        queue_id = file_row(store, "fix-build-1", kind="fix")
        store.admit(queue_id, actor_identity="test")
        loop, _ = build_loop(
            store,
            clock=clock,
            notifier=notifier,
            runs=runs,
            admit_fix_rows=True,
            fix_maker=RunMaker(),
            builds={},
        )

        assert loop.recover_admitted() == [queue_id]
        assert store.get(queue_id)["status"] == "QUEUED"

    @pytest.mark.asyncio
    async def test_with_nothing_wired_to_open_a_journey_the_row_goes_back(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        """A misconfiguration loses no work: the row returns to the queue."""
        queue_id = file_row(store, "fix-build-1", kind="fix")
        loop, _ = build_loop(
            store,
            clock=clock,
            notifier=notifier,
            runs=runs,
            admit_fix_rows=True,
            fix_maker=None,
            builds={},
        )

        assert await loop.take_next() is None
        assert store.get(queue_id)["status"] == "QUEUED"

    @pytest.mark.asyncio
    async def test_a_repair_row_is_never_put_back_when_nothing_reads_builds(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        """No build reader wired: the loop must not decide the row never ran."""
        queue_id = file_row(store, "fix-build-1", kind="fix")
        store.admit(queue_id, actor_identity="test")
        loop, _ = build_loop(
            store, clock=clock, notifier=notifier, runs=runs, admit_fix_rows=True
        )

        assert loop.recover_admitted() == []
        assert store.get(queue_id)["status"] == "ADMITTED"


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _insert_run(
    connection: sqlite3.Connection,
    correlation_id: str,
    state: str,
    *,
    target_repo: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO planning_runs (
            correlation_id, state, originating_user, expected_approver,
            request_text, target_repo, triggered_by, queued_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'jarvis', ?)
        """,
        (
            correlation_id,
            state,
            USER,
            USER,
            "build a login page",
            target_repo,
            START.isoformat(),
        ),
    )
    connection.commit()


def _insert_build(
    connection: sqlite3.Connection,
    build_id: str,
    status: str,
    *,
    feature_id: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO builds (
            build_id, feature_id, repo, branch, feature_yaml_path, status,
            triggered_by, correlation_id, queued_at, max_turns,
            sdk_timeout_seconds, mode
        ) VALUES (?, ?, ?, ?, ?, ?, 'cli', ?, ?, 50, 3600, 'mode-b')
        """,
        (
            build_id,
            feature_id or f"FEAT-{build_id}",
            "/tmp/api_test",
            "main",
            "feature.yaml",
            status,
            f"corr-{build_id}",
            START.isoformat(),
        ),
    )
    connection.commit()


# ---------------------------------------------------------------------------
# Byte-for-byte today (contract 9)
# ---------------------------------------------------------------------------


class TestNothingDownstreamChanges:
    """Empty queue, cap one: a sentence is filed and admitted on the next tick.

    The run the loop creates must be the run the intake used to create — the
    same correlation id, the same words, the same repository, the same person,
    the same thread anchor — because everything downstream of it (the cards,
    the receipts, the Slack thread) is keyed on exactly those.
    """

    @pytest.mark.asyncio
    async def test_the_run_matches_the_one_a_queueless_forge_would_create(
        self, tmp_path: Path, clock: FakeClock, notifier: Notifier
    ) -> None:
        from unittest.mock import AsyncMock

        from forge.adapters.nats.planning_consumer import (
            PlanningConsumerDeps,
            create_and_start_planning_run,
            handle_planning_message,
        )
        from forge.planning.run_store import SqlitePlanningRunStore

        # -- a forge with no queue: the sentence becomes a run at the door --
        before = sqlite_connect.connect_writer(tmp_path / "before.db")
        migrations.apply_at_boot(before)
        before_store = SqlitePlanningRunStore(before)
        await handle_planning_message(
            _planning_msg(),
            PlanningConsumerDeps(
                store=before_store,
                publish_notification=AsyncMock(),
                on_recorded=AsyncMock(),
            ),
        )

        # -- a forge with the queue: filed, then admitted on the next tick --
        after = sqlite_connect.connect_writer(tmp_path / "after.db")
        migrations.apply_at_boot(after)
        after_store = SqlitePlanningRunStore(after)
        queue_store = WorkQueueStore(after, clock=clock.now)
        deps = PlanningConsumerDeps(
            store=after_store,
            publish_notification=AsyncMock(),
            on_recorded=AsyncMock(),
            queue_store=queue_store,
        )
        await handle_planning_message(_planning_msg(), deps)
        assert after_store.get_run("plan-contract-9") is None

        async def start_run(admission: Admission) -> None:
            await create_and_start_planning_run(
                deps,
                correlation_id=admission.correlation_id,
                request_text=admission.request_text,
                originating_user=admission.originating_user,
                triggered_by=admission.triggered_by,
                originating_adapter=admission.originating_adapter,
                parent_request_id=admission.parent_request_id,
                target_repo=admission.target_repo,
            )

        loop = WorkQueueLoop(
            queue_store,
            count_in_flight=lambda: count_in_flight(after),
            planning_run=after_store.get_run,
            paused_repositories=lambda: paused_repositories(after),
            start_run=start_run,
            notify=notifier,
            clock=clock.now,
        )
        await loop.tick()

        # -- the two runs are the same run ---------------------------------
        expected = before_store.get_run("plan-contract-9")
        actual = after_store.get_run("plan-contract-9")
        assert expected is not None and actual is not None
        moving = {"queued_at", "started_at", "completed_at"}
        assert {
            key: actual[key] for key in actual.keys() if key not in moving
        } == {key: expected[key] for key in expected.keys() if key not in moving}

        before.close()
        after.close()

    @pytest.mark.asyncio
    async def test_the_sender_is_told_once_that_nothing_is_ahead_of_it(
        self, tmp_path: Path, clock: FakeClock
    ) -> None:
        from unittest.mock import AsyncMock

        from forge.adapters.nats.planning_consumer import (
            PlanningConsumerDeps,
            handle_planning_message,
        )
        from forge.planning.run_store import SqlitePlanningRunStore

        cx = sqlite_connect.connect_writer(tmp_path / "forge.db")
        migrations.apply_at_boot(cx)
        notify = AsyncMock()
        await handle_planning_message(
            _planning_msg(),
            PlanningConsumerDeps(
                store=SqlitePlanningRunStore(cx),
                publish_notification=notify,
                on_recorded=AsyncMock(),
                queue_store=WorkQueueStore(cx, clock=clock.now),
            ),
        )

        assert [call.args[1] for call in notify.await_args_list] == [
            "Queued as #1 (api_test · feature). Nothing ahead of it."
        ]
        cx.close()


def _planning_msg() -> Any:
    """One Slack sentence on the wire, exactly as jarvis publishes it."""
    from unittest.mock import AsyncMock

    from nats_core.envelope import EventType, MessageEnvelope

    payload = {
        "stage": "planning",
        "request_text": "build a login page",
        "target_repo": "api_test",
        "triggered_by": "jarvis",
        "originating_adapter": "slack",
        "originating_user": USER,
        "correlation_id": "plan-contract-9",
        "parent_request_id": THREAD,
        "retry_count": 0,
        "requested_at": START.isoformat(),
        "queued_at": START.isoformat(),
    }
    envelope = MessageEnvelope(
        message_id="msg-contract-9",
        timestamp=START,
        version="1.0",
        source_id="slack",
        event_type=EventType.BUILD_QUEUED,
        project=None,
        correlation_id="plan-contract-9",
        payload=payload,
    )
    msg = AsyncMock()
    msg.data = envelope.model_dump_json().encode("utf-8")
    msg.ack = AsyncMock()
    msg.nak = AsyncMock()
    return msg


# ---------------------------------------------------------------------------
# Picking up what was dropped (coaches' correction, 2026-09-05)
# ---------------------------------------------------------------------------


class DeadRunMaker(RunMaker):
    """A run maker that reports success and leaves no planning run behind.

    What a forge that stops in the moment between marking a row admitted and
    creating its run looks like from the next boot's point of view.
    """

    def __init__(self) -> None:
        super().__init__(runs={})


class TestPickingUpWhatWasDropped:
    @pytest.mark.asyncio
    async def test_a_row_admitted_with_no_run_behind_it_goes_back(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        queue_id = file_row(store, "plan-1")
        stopped, _ = build_loop(
            store,
            clock=clock,
            notifier=notifier,
            runs=runs,
            run_maker=DeadRunMaker(),
        )
        await stopped.take_next()
        assert store.get(queue_id)["status"] == "ADMITTED"

        # The forge comes back up.
        fresh, maker = build_loop(store, clock=clock, notifier=notifier, runs=runs)
        assert fresh.recover_admitted() == [queue_id]

        row = store.get(queue_id)
        assert row["status"] == "QUEUED"
        assert row["admitted_at"] is None
        assert "requeued" in [e["action"] for e in store.list_events(queue_id)]

    @pytest.mark.asyncio
    async def test_the_sentence_is_taken_again_on_the_next_tick(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        file_row(store, "plan-1")
        stopped, _ = build_loop(
            store,
            clock=clock,
            notifier=notifier,
            runs=runs,
            run_maker=DeadRunMaker(),
        )
        await stopped.take_next()

        fresh, maker = build_loop(store, clock=clock, notifier=notifier, runs=runs)
        await fresh.tick()

        assert [a.correlation_id for a in maker.admissions] == ["plan-1"]

    @pytest.mark.asyncio
    async def test_it_happens_before_the_first_tick(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        queue_id = file_row(store, "plan-1")
        stopped, _ = build_loop(
            store,
            clock=clock,
            notifier=notifier,
            runs=runs,
            run_maker=DeadRunMaker(),
        )
        await stopped.take_next()

        fresh, _ = build_loop(store, clock=clock, notifier=notifier, runs=runs)
        await fresh.run(sleep=clock.sleep, iterations=0)

        assert store.get(queue_id)["status"] == "QUEUED"

    @pytest.mark.asyncio
    async def test_a_row_with_a_run_behind_it_is_left_alone(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        queue_id = file_row(store, "plan-1")
        loop, _ = build_loop(store, clock=clock, notifier=notifier, runs=runs)
        await loop.take_next()
        runs["plan-1"] = {"state": PlanningState.RUNNING.value, "error": None}

        assert loop.recover_admitted() == []
        assert store.get(queue_id)["status"] == "ADMITTED"

    @pytest.mark.asyncio
    async def test_a_row_still_waiting_is_left_alone(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        queue_id = file_row(store, "plan-1")
        loop, _ = build_loop(store, clock=clock, notifier=notifier, runs=runs)

        assert loop.recover_admitted() == []
        assert store.get(queue_id)["status"] == "QUEUED"
        assert [e["action"] for e in store.list_events(queue_id)] == ["queued"]


# ---------------------------------------------------------------------------
# Slack never stops the work (coaches' correction, 2026-09-05)
# ---------------------------------------------------------------------------


class ExplodingNotifier:
    """A notifier that fails the way an unreachable Slack would."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(
        self,
        correlation_id: str,
        message: str,
        *,
        parent_request_id: str | None = None,
    ) -> None:
        self.calls.append((correlation_id, message))
        raise RuntimeError("Slack is not answering")

    @property
    def messages(self) -> list[str]:
        return [message for _, message in self.calls]


class TwoArgumentNotifier:
    """An older notifier that knows nothing about threads."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, correlation_id: str, message: str) -> None:
        self.calls.append((correlation_id, message))

    @property
    def messages(self) -> list[str]:
        return [message for _, message in self.calls]


class TypeErrorNotifier:
    """A notifier that takes the thread and then breaks on its own account."""

    def __init__(self) -> None:
        self.calls: list[str | None] = []

    async def __call__(
        self,
        correlation_id: str,
        message: str,
        *,
        parent_request_id: str | None = None,
    ) -> None:
        self.calls.append(parent_request_id)
        raise TypeError("something inside the notifier broke")


class TestSlackNeverStopsTheWork:
    @staticmethod
    def _two_rows_the_orders_disagree_about(store: WorkQueueStore) -> None:
        """A feature first and a fix behind it, so the loop has a line to say."""
        file_row(store, "plan-1", kind="feature")
        file_row(store, "plan-2", kind="fix")

    @pytest.mark.asyncio
    async def test_a_notifier_that_fails_does_not_stop_the_admission(
        self, store: WorkQueueStore, clock: FakeClock, runs: dict
    ) -> None:
        self._two_rows_the_orders_disagree_about(store)
        notifier = ExplodingNotifier()
        loop, maker = build_loop(
            store,
            clock=clock,
            notifier=notifier,  # type: ignore[arg-type]
            runs=runs,
        )

        taken = await loop.take_next()

        assert taken is not None
        assert notifier.calls  # it did try to say something
        assert store.get(taken)["status"] == "ADMITTED"
        assert [a.correlation_id for a in maker.admissions] == ["plan-1"]

    @pytest.mark.asyncio
    async def test_a_notifier_that_fails_does_not_stop_a_whole_tick(
        self, store: WorkQueueStore, clock: FakeClock, runs: dict
    ) -> None:
        self._two_rows_the_orders_disagree_about(store)
        notifier = ExplodingNotifier()
        loop, maker = build_loop(
            store,
            clock=clock,
            notifier=notifier,  # type: ignore[arg-type]
            runs=runs,
        )

        await loop.tick()

        assert len(maker.admissions) == 1

    @pytest.mark.asyncio
    async def test_an_older_notifier_is_called_with_the_two_it_takes(
        self, store: WorkQueueStore, clock: FakeClock, runs: dict
    ) -> None:
        self._two_rows_the_orders_disagree_about(store)
        notifier = TwoArgumentNotifier()
        loop, _ = build_loop(
            store,
            clock=clock,
            notifier=notifier,  # type: ignore[arg-type]
            runs=runs,
        )

        await loop.take_next()

        assert len(notifier.calls) == 1
        assert notifier.messages[0].startswith("next I'd pick")

    @pytest.mark.asyncio
    async def test_a_notifier_that_breaks_is_not_called_a_second_time(
        self, store: WorkQueueStore, clock: FakeClock, runs: dict
    ) -> None:
        # The old code decided how to call the notifier by calling it and
        # catching TypeError, so a TypeError from INSIDE the notifier looked
        # like a notifier that takes two arguments and the message went twice.
        self._two_rows_the_orders_disagree_about(store)
        notifier = TypeErrorNotifier()
        loop, _ = build_loop(
            store,
            clock=clock,
            notifier=notifier,  # type: ignore[arg-type]
            runs=runs,
        )

        await loop.take_next()

        assert notifier.calls == [THREAD]


# ---------------------------------------------------------------------------
# How often the forge asks about old sentences (coaches' correction)
# ---------------------------------------------------------------------------


class TestTheStaleCheckCadence:
    def test_the_check_is_daily(self) -> None:
        assert STALE_TICK_INTERVAL_SECONDS == 24 * 60 * 60.0

    @pytest.mark.asyncio
    async def test_the_first_check_comes_a_day_after_boot_not_a_week(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        file_row(store, "plan-1")
        clock.advance(8 * 24 * 60 * 60)
        loop, _ = build_loop(
            store, clock=clock, notifier=notifier, runs=runs, in_flight=1
        )

        # Thirty hours of ticks — more than a day, nothing like a week.
        await loop.run(
            sleep=clock.sleep,
            interval_seconds=6 * 60 * 60,
            iterations=5,
        )

        assert len([m for m in notifier.messages if "waiting a while" in m]) == 1

    @pytest.mark.asyncio
    async def test_the_old_weekly_schedule_would_have_said_nothing(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        file_row(store, "plan-1")
        clock.advance(8 * 24 * 60 * 60)
        loop, _ = build_loop(
            store, clock=clock, notifier=notifier, runs=runs, in_flight=1
        )

        await loop.run(
            sleep=clock.sleep,
            interval_seconds=6 * 60 * 60,
            stale_interval_seconds=7 * 24 * 60 * 60,
            iterations=5,
        )

        assert notifier.messages == []

    @pytest.mark.asyncio
    async def test_a_daily_check_still_only_asks_once_a_week(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        queue_id = file_row(store, "plan-1")
        loop, _ = build_loop(store, clock=clock, notifier=notifier, runs=runs)
        clock.advance(8 * 24 * 60 * 60)

        assert await loop.stale_tick() == [queue_id]
        for _ in range(6):
            clock.advance(24 * 60 * 60)
            assert await loop.stale_tick() == []
        assert len(notifier.messages) == 1

    @pytest.mark.asyncio
    async def test_it_asks_again_once_the_quiet_week_is_over(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        queue_id = file_row(store, "plan-1")
        loop, _ = build_loop(store, clock=clock, notifier=notifier, runs=runs)
        clock.advance(8 * 24 * 60 * 60)
        await loop.stale_tick()

        clock.advance(7 * 24 * 60 * 60)

        assert await loop.stale_tick() == [queue_id]
        assert len(notifier.messages) == 2


# ---------------------------------------------------------------------------
# "Go" is judged by the clock, not by how the timestamp is spelled
# ---------------------------------------------------------------------------


class TestGoIsJudgedByTheClock:
    @pytest.mark.asyncio
    async def test_a_promote_from_before_the_question_is_not_go(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        first = file_row(store, "plan-1")
        second = file_row(store, "plan-2")
        store.link(second, first, actor_identity=USER)
        store.drop(first, actor_identity=USER)
        loop, maker = build_loop(store, clock=clock, notifier=notifier, runs=runs)

        # Someone moved the row half an hour BEFORE the forge asked, from a
        # place two hours ahead of London. As text "10:30+02:00" sorts after
        # "09:00+00:00"; as a moment in time it comes first.
        clock.set_to(datetime(2026, 9, 5, 10, 30, tzinfo=timezone(timedelta(hours=2))))
        store.promote(second, actor_identity=USER)
        clock.set_to(datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc))
        await loop.ask_hold_or_go()

        assert await loop.take_next() is None
        assert maker.admissions == []
        assert store.get(second)["status"] == "QUEUED"

    @pytest.mark.asyncio
    async def test_a_promote_after_the_question_is_go(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        first = file_row(store, "plan-1")
        second = file_row(store, "plan-2")
        store.link(second, first, actor_identity=USER)
        store.drop(first, actor_identity=USER)
        loop, maker = build_loop(store, clock=clock, notifier=notifier, runs=runs)

        clock.set_to(datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc))
        await loop.ask_hold_or_go()
        clock.set_to(datetime(2026, 9, 5, 11, 30, tzinfo=timezone(timedelta(hours=2))))
        store.promote(second, actor_identity=USER)

        assert await loop.take_next() == second
        assert [a.correlation_id for a in maker.admissions] == ["plan-2"]


# ---------------------------------------------------------------------------
# A refusal that will say the same thing next tick takes the row out
# ---------------------------------------------------------------------------


def refused(reason: str, message: str, *, permanent: bool) -> FixAdmissionRefused:
    return FixAdmissionRefused(message, reason=reason, permanent=permanent)


def publish_failed() -> FixPublishFailed:
    """The transport half: the build row landed, the pipeline was not told."""
    return FixPublishFailed(
        "Queued FEAT-44A8 (build pending) but pipeline NOT NOTIFIED — publish "
        "failed (messaging-layer): connection refused",
        admission=FixAdmission(
            build_id="build-FEAT-44A8-1",
            task_id="TASK-FEAT44A8FIX1",
            feature_id="FEAT-44A8",
            correlation_id="fix-build-1",
            repo="appmilla_github/api_test",
            fix_task_path="/tmp/TASK-FEAT44A8FIX1.yaml",
        ),
    )


UNKNOWN_REPO = (
    "I don't know a repository called 'api-test'. The ones I know are: "
    "api_test, forge."
)


class TestARefusalThatWillNotChange:
    """A row nothing can ever start leaves the queue, and says so once.

    An unknown repository, a budget profile with no cap, a row that names no
    build: the admission refuses each of those the same way every ten
    seconds for ever, and with a cap of one in flight everything behind it
    waits for ever too. So the row closes BLOCKED with the refusal's own
    sentence, and the channel is told once. A refusal that only means "not
    now" — another build for the same feature is running — still goes back
    in the queue.
    """

    @pytest.mark.asyncio
    async def test_an_unknown_repository_takes_the_row_out_of_the_queue(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        queue_id = file_row(store, "fix-build-1", kind="fix")
        loop, _ = build_loop(
            store,
            clock=clock,
            notifier=notifier,
            runs=runs,
            admit_fix_rows=True,
            fix_maker=RunMaker(
                fail_with=refused("repo-unknown", UNKNOWN_REPO, permanent=True)
            ),
            builds={},
        )

        assert await loop.take_next() is None

        row = store.get(queue_id)
        assert row is not None
        assert row["status"] == "BLOCKED"
        assert row["closed_reason"] == UNKNOWN_REPO

    @pytest.mark.asyncio
    async def test_the_channel_is_told_once_in_the_words_the_spec_gives(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        queue_id = file_row(store, "fix-build-1", kind="fix")
        loop, _ = build_loop(
            store,
            clock=clock,
            notifier=notifier,
            runs=runs,
            admit_fix_rows=True,
            fix_maker=RunMaker(
                fail_with=refused("repo-unknown", UNKNOWN_REPO, permanent=True)
            ),
            builds={},
        )

        await loop.tick()
        await loop.tick()  # and again, in case it wants to repeat itself

        assert notifier.messages == [
            f"#{queue_id} cannot be started: I don't know a repository called "
            "'api-test'. It is out of the queue; drop it or fix the cause and "
            "send it again."
        ]

    @pytest.mark.asyncio
    async def test_the_closing_is_written_down_as_blocked(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        queue_id = file_row(store, "fix-build-1", kind="fix")
        loop, _ = build_loop(
            store,
            clock=clock,
            notifier=notifier,
            runs=runs,
            admit_fix_rows=True,
            fix_maker=RunMaker(
                fail_with=refused("cap", "the profile has no cap", permanent=True)
            ),
            builds={},
        )

        await loop.take_next()

        blocked = [
            event
            for event in store.list_events(queue_id)
            if str(event["action"]) == "blocked"
        ]
        assert len(blocked) == 1
        assert blocked[0]["actor_identity"] == LOOP_ACTOR
        assert "the profile has no cap" in str(blocked[0]["details_json"])

    @pytest.mark.asyncio
    async def test_the_row_behind_it_goes_on_the_next_tick(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        """The whole point: one bad row must not stop the queue for ever."""
        fix = file_row(store, "fix-build-1", kind="fix")
        feature = file_row(store, "plan-1", kind="feature")
        loop, maker = build_loop(
            store,
            clock=clock,
            notifier=notifier,
            runs=runs,
            admit_fix_rows=True,
            fix_maker=RunMaker(
                fail_with=refused("repo-unknown", UNKNOWN_REPO, permanent=True)
            ),
            builds={},
        )

        assert await loop.take_next() is None
        assert store.get(fix)["status"] == "BLOCKED"

        assert await loop.take_next() == feature
        assert [a.correlation_id for a in maker.admissions] == ["plan-1"]

    @pytest.mark.asyncio
    async def test_a_refusal_that_only_means_not_now_goes_back_in_the_queue(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        queue_id = file_row(store, "fix-build-1", kind="fix")
        loop, _ = build_loop(
            store,
            clock=clock,
            notifier=notifier,
            runs=runs,
            admit_fix_rows=True,
            fix_maker=RunMaker(
                fail_with=refused(
                    "duplicate",
                    "duplicate build refused: an active build for FEAT-44A8 "
                    "is already in flight (Group C).",
                    permanent=False,
                )
            ),
            builds={},
        )

        assert await loop.take_next() is None

        row = store.get(queue_id)
        assert row is not None and row["status"] == "QUEUED"
        assert notifier.messages == []

    @pytest.mark.asyncio
    async def test_a_transport_failure_goes_back_in_the_queue(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        """The build row landed and the broker did not hear about it. Try again."""
        queue_id = file_row(store, "fix-build-1", kind="fix")
        loop, _ = build_loop(
            store,
            clock=clock,
            notifier=notifier,
            runs=runs,
            admit_fix_rows=True,
            fix_maker=RunMaker(fail_with=publish_failed()),
            builds={},
        )

        assert await loop.take_next() is None

        row = store.get(queue_id)
        assert row is not None and row["status"] == "QUEUED"
        assert notifier.messages == []

    @pytest.mark.asyncio
    async def test_a_feature_row_refused_by_its_own_starter_still_goes_back(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        """Nothing here changes for the planning-run path."""
        queue_id = file_row(store, "plan-1")
        loop, _ = build_loop(
            store,
            clock=clock,
            notifier=notifier,
            runs=runs,
            run_maker=RunMaker(fail_with=RuntimeError("the driver is asleep")),
        )

        assert await loop.take_next() is None
        assert store.get(queue_id)["status"] == "QUEUED"


# ---------------------------------------------------------------------------
# The waiting line is said once, not every ten seconds
# ---------------------------------------------------------------------------


class TestTheWaitingLineIsSaidOnce:
    """A queue holding only repairs, with repairs shut, is quiet in the log.

    The line is worth saying — otherwise a queue that never admits anything
    looks like a queue with nothing in it — but at one tick every ten seconds
    it would be written eight and a half thousand times a day, and a log
    nobody can read is the same as no log. It is said once per change of the
    repair rows that are open.
    """

    @pytest.mark.asyncio
    async def test_ten_ticks_say_it_once(
        self,
        store: WorkQueueStore,
        clock: FakeClock,
        notifier: Notifier,
        runs: dict,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        file_row(store, "fix-build-1", kind="fix")
        loop, _ = build_loop(
            store, clock=clock, notifier=notifier, runs=runs, builds={}
        )

        with caplog.at_level("INFO", logger="forge.planning.work_queue_loop"):
            await loop.run(iterations=10, sleep=clock.sleep)

        assert clock.slept == [10.0] * 10
        assert _waiting_lines(caplog) == 1

    @pytest.mark.asyncio
    async def test_a_new_repair_arriving_is_said(
        self,
        store: WorkQueueStore,
        clock: FakeClock,
        notifier: Notifier,
        runs: dict,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Once per CHANGE: the queue is a different queue now."""
        file_row(store, "fix-build-1", kind="fix")
        loop, _ = build_loop(
            store, clock=clock, notifier=notifier, runs=runs, builds={}
        )

        with caplog.at_level("INFO", logger="forge.planning.work_queue_loop"):
            await loop.take_next()
            await loop.take_next()
            file_row(store, "fix-build-2", kind="fix")
            await loop.take_next()
            await loop.take_next()

        assert _waiting_lines(caplog) == 2


def _waiting_lines(caplog: pytest.LogCaptureFixture) -> int:
    return len(
        [
            record
            for record in caplog.records
            if "is waiting and nothing may be started" in record.getMessage()
        ]
    )


# ---------------------------------------------------------------------------
# The channel line is one sentence, whatever the refusal is
# ---------------------------------------------------------------------------


def cap_law_refusal() -> str:
    """The real cap-law refusal — a runbook with a YAML snippet in it."""
    from forge.config.conductor import mode_c_cap_refusal

    refusal = mode_c_cap_refusal(profile_name="fix-journey", guards=object())
    assert refusal is not None
    return refusal.message


class TestTheChannelLineIsOneSentence:
    """A refusal can be a runbook. The channel gets its first sentence.

    THE CAP LAW's refusal is about seven hundred characters over several
    paragraphs, ending in a YAML snippet for forge.yaml. Flattened into one
    Slack line that is unreadable, and an unreadable line is a defect. The
    channel is told what happened in one sentence; the whole refusal is
    written on the row and in the row's events, where anyone who wants the
    runbook can read it.
    """

    @pytest.mark.asyncio
    async def test_a_runbook_refusal_says_only_its_first_sentence(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        message = cap_law_refusal()
        assert len(message) > 500 and "max_review_cycles" in message

        queue_id = file_row(store, "fix-build-1", kind="fix")
        loop, _ = build_loop(
            store,
            clock=clock,
            notifier=notifier,
            runs=runs,
            admit_fix_rows=True,
            fix_maker=RunMaker(fail_with=refused("cap", message, permanent=True)),
            builds={},
        )

        await loop.take_next()

        said = notifier.messages[0]
        assert len(said) < 300
        assert said == (
            f"#{queue_id} cannot be started: the fix journey is refused: its "
            "profile sets no cap. It is out of the queue; drop it or fix the "
            "cause and send it again."
        )

    @pytest.mark.asyncio
    async def test_the_whole_refusal_stays_on_the_row_and_in_its_events(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        message = cap_law_refusal()
        queue_id = file_row(store, "fix-build-1", kind="fix")
        loop, _ = build_loop(
            store,
            clock=clock,
            notifier=notifier,
            runs=runs,
            admit_fix_rows=True,
            fix_maker=RunMaker(fail_with=refused("cap", message, permanent=True)),
            builds={},
        )

        await loop.take_next()

        row = store.get(queue_id)
        assert row is not None
        assert row["status"] == "BLOCKED"
        assert row["closed_reason"] == message
        blocked = [
            event
            for event in store.list_events(queue_id)
            if str(event["action"]) == "blocked"
        ]
        assert "max_review_cycles" in str(blocked[0]["details_json"])

    @pytest.mark.asyncio
    async def test_a_refusal_of_several_paragraphs_ends_at_the_first_break(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        message = (
            "the fix journey is refused: its profile sets no cap\n"
            "Budget profile 'fix-journey' sets no review-cycle cap at all.\n"
            "The fix: give the profile a review-cycle cap, e.g.\n"
            "    budget:\n      profiles:\n        fix-journey:\n"
            "          max_review_cycles: 2\n"
        )
        queue_id = file_row(store, "fix-build-1", kind="fix")
        loop, _ = build_loop(
            store,
            clock=clock,
            notifier=notifier,
            runs=runs,
            admit_fix_rows=True,
            fix_maker=RunMaker(fail_with=refused("cap", message, permanent=True)),
            builds={},
        )

        await loop.take_next()

        assert notifier.messages == [
            f"#{queue_id} cannot be started: the fix journey is refused: its "
            "profile sets no cap. It is out of the queue; drop it or fix the "
            "cause and send it again."
        ]
        assert store.get(queue_id)["closed_reason"] == message

    def test_the_first_sentence_of_one_ordinary_sentence_is_all_of_it(
        self,
    ) -> None:
        assert first_sentence("nothing to say here.") == "nothing to say here"
        assert first_sentence("one. two. three.") == "one"
        assert first_sentence("  ") == ""


# ---------------------------------------------------------------------------
# A repair whose build was already written (close-out item 2)
# ---------------------------------------------------------------------------


DUPLICATE_MESSAGE = (
    "duplicate build refused: duplicate build: feature_id='FEAT-44A8' "
    "correlation_id='fix-build-1' (Group B)."
)


def a_build(correlation_id: str, status: str, *, error: str | None = None) -> dict:
    """One ``builds`` row as the loop's reader hands it over."""
    return {
        "build_id": "build-FEAT-44A8-1",
        "feature_id": "FEAT-44A8",
        "correlation_id": correlation_id,
        "status": status,
        "error": error,
        "repo": "appmilla_github/api_test",
        "branch": "main",
        "feature_yaml_path": "/tmp/TASK-FEAT44A8FIX1.yaml",
        "task_id": "TASK-FEAT44A8FIX1",
        "mode": "mode-c",
        "queued_at": "2026-09-05T12:00:00+00:00",
        "max_turns": 5,
        "sdk_timeout_seconds": 1800,
        "triggered_by": "forge-internal",
        "originating_adapter": "slack",
        "originating_user": USER,
        "parent_request_id": THREAD,
    }


class Republisher:
    """Stands in for saying a written build's queued event again."""

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self.calls: list[Any] = []
        self._fail_with = fail_with

    async def __call__(self, build: Any) -> None:
        self.calls.append(build)
        if self._fail_with is not None:
            raise self._fail_with


class LosesThePublish:
    """The admission whose build row landed and whose publish did not.

    First call: the build row appears and the publish fails, exactly as the
    real admission behaves — write first, then publish. Every call after
    that: the row is already there, so the admission refuses the duplicate.
    """

    def __init__(self, builds: dict[str, dict[str, Any]]) -> None:
        self.builds = builds
        self.calls = 0

    async def __call__(self, admission: Admission) -> None:
        self.calls += 1
        correlation_id = admission.correlation_id
        if correlation_id not in self.builds:
            self.builds[correlation_id] = a_build(correlation_id, "QUEUED")
            raise publish_failed()
        raise refused("duplicate", DUPLICATE_MESSAGE, permanent=False)


class TestARepairWhoseBuildWasAlreadyWritten:
    """A duplicate that is the row's OWN build is settled, not asked again.

    The write comes before the publish, so a publish that fails leaves a real
    build row nobody was told about. Until now the next tick was refused as a
    duplicate and the row went back in the queue — every ten seconds, for
    ever, with the whole queue waiting behind it. The loop now reads that
    build and answers in its terms.
    """

    @pytest.mark.asyncio
    async def test_a_lost_publish_is_said_again_once_and_the_row_is_admitted(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        builds: dict[str, dict[str, Any]] = {}
        maker = LosesThePublish(builds)
        republisher = Republisher()
        queue_id = file_row(store, "fix-build-1", kind="fix")
        loop, _ = build_loop(
            store,
            clock=clock,
            notifier=notifier,
            runs=runs,
            admit_fix_rows=True,
            fix_maker=maker,
            builds=builds,
            republisher=republisher,
        )

        await loop.tick()  # the build row lands, the publish fails
        assert store.get(queue_id)["status"] == "QUEUED"
        assert republisher.calls == []

        await loop.tick()  # the duplicate is this row's own build
        assert len(republisher.calls) == 1
        assert store.get(queue_id)["status"] == "ADMITTED"

        await loop.tick()  # and it is not said a third time
        assert len(republisher.calls) == 1
        assert store.get(queue_id)["status"] == "ADMITTED"
        assert maker.calls == 2

    @pytest.mark.asyncio
    async def test_the_saying_again_is_written_down(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        builds: dict[str, dict[str, Any]] = {}
        queue_id = file_row(store, "fix-build-1", kind="fix")
        loop, _ = build_loop(
            store,
            clock=clock,
            notifier=notifier,
            runs=runs,
            admit_fix_rows=True,
            fix_maker=LosesThePublish(builds),
            builds=builds,
            republisher=Republisher(),
        )

        await loop.tick()
        await loop.tick()

        said_again = [
            event
            for event in store.list_events(queue_id)
            if str(event["action"]) == "republished"
        ]
        assert len(said_again) == 1
        assert said_again[0]["actor_identity"] == LOOP_ACTOR
        assert "build-FEAT-44A8-1" in str(said_again[0]["details_json"])

    @pytest.mark.asyncio
    async def test_a_build_already_under_way_leaves_the_row_admitted(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        """The work is happening. Nothing is published, and nothing is asked again."""
        republisher = Republisher()
        queue_id = file_row(store, "fix-build-1", kind="fix")
        loop, _ = build_loop(
            store,
            clock=clock,
            notifier=notifier,
            runs=runs,
            admit_fix_rows=True,
            fix_maker=RunMaker(
                fail_with=refused("duplicate", DUPLICATE_MESSAGE, permanent=False)
            ),
            builds={"fix-build-1": a_build("fix-build-1", "RUNNING")},
            republisher=republisher,
        )

        await loop.tick()

        assert store.get(queue_id)["status"] == "ADMITTED"
        assert republisher.calls == []
        assert notifier.messages == []

    @pytest.mark.asyncio
    async def test_a_build_that_already_finished_closes_the_row_done(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        queue_id = file_row(store, "fix-build-1", kind="fix")
        loop, _ = build_loop(
            store,
            clock=clock,
            notifier=notifier,
            runs=runs,
            admit_fix_rows=True,
            fix_maker=RunMaker(
                fail_with=refused("duplicate", DUPLICATE_MESSAGE, permanent=False)
            ),
            builds={"fix-build-1": a_build("fix-build-1", "COMPLETE")},
            republisher=Republisher(),
        )

        await loop.tick()

        assert store.get(queue_id)["status"] == "DONE"

    @pytest.mark.asyncio
    async def test_a_build_that_already_failed_blocks_the_row_in_its_words(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        queue_id = file_row(store, "fix-build-1", kind="fix")
        loop, _ = build_loop(
            store,
            clock=clock,
            notifier=notifier,
            runs=runs,
            admit_fix_rows=True,
            fix_maker=RunMaker(
                fail_with=refused("duplicate", DUPLICATE_MESSAGE, permanent=False)
            ),
            builds={"fix-build-1": a_build("fix-build-1", "FAILED")},
            republisher=Republisher(),
        )

        await loop.tick()

        row = store.get(queue_id)
        assert row["status"] == "BLOCKED"
        assert row["closed_reason"] == "the fix journey failed"

    @pytest.mark.asyncio
    async def test_a_duplicate_that_is_not_this_rows_build_goes_back(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        """Somebody else's build for the same feature really does clear."""
        republisher = Republisher()
        queue_id = file_row(store, "fix-build-1", kind="fix")
        loop, _ = build_loop(
            store,
            clock=clock,
            notifier=notifier,
            runs=runs,
            admit_fix_rows=True,
            fix_maker=RunMaker(
                fail_with=refused("duplicate", DUPLICATE_MESSAGE, permanent=False)
            ),
            builds={},
            republisher=republisher,
        )

        await loop.tick()

        assert store.get(queue_id)["status"] == "QUEUED"
        assert republisher.calls == []

    @pytest.mark.asyncio
    async def test_with_nothing_wired_to_say_it_again_the_row_goes_back(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        """The honest degrade: no republisher, so the old behaviour stands."""
        queue_id = file_row(store, "fix-build-1", kind="fix")
        loop, _ = build_loop(
            store,
            clock=clock,
            notifier=notifier,
            runs=runs,
            admit_fix_rows=True,
            fix_maker=RunMaker(
                fail_with=refused("duplicate", DUPLICATE_MESSAGE, permanent=False)
            ),
            builds={"fix-build-1": a_build("fix-build-1", "QUEUED")},
        )

        await loop.tick()

        assert store.get(queue_id)["status"] == "QUEUED"

    @pytest.mark.asyncio
    async def test_a_saying_that_fails_puts_the_row_back_for_the_next_tick(
        self, store: WorkQueueStore, clock: FakeClock, notifier: Notifier, runs: dict
    ) -> None:
        """The broker is still down. Try again in ten seconds, not never."""
        republisher = Republisher(fail_with=RuntimeError("broker unreachable"))
        queue_id = file_row(store, "fix-build-1", kind="fix")
        loop, _ = build_loop(
            store,
            clock=clock,
            notifier=notifier,
            runs=runs,
            admit_fix_rows=True,
            fix_maker=RunMaker(
                fail_with=refused("duplicate", DUPLICATE_MESSAGE, permanent=False)
            ),
            builds={"fix-build-1": a_build("fix-build-1", "QUEUED")},
            republisher=republisher,
        )

        await loop.tick()
        await loop.tick()

        assert store.get(queue_id)["status"] == "QUEUED"
        assert len(republisher.calls) == 2

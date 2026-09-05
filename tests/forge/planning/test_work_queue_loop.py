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
from forge.planning.states import PlanningState
from forge.planning.work_queue_loop import (
    LOOP_ACTOR,
    Admission,
    WorkQueueLoop,
    count_in_flight,
    paused_repositories,
    shadow_line,
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

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.advance(seconds)


class RunMaker:
    """Stands in for creating and starting the planning run."""

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self.admissions: list[Admission] = []
        self._fail_with = fail_with

    async def __call__(self, admission: Admission) -> None:
        self.admissions.append(admission)
        if self._fail_with is not None:
            raise self._fail_with


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
) -> tuple[WorkQueueLoop, RunMaker]:
    maker = run_maker or RunMaker()
    loop = WorkQueueLoop(
        store,
        count_in_flight=lambda: in_flight,
        planning_run=lambda cid: runs.get(cid),
        paused_repositories=lambda: set(paused or set()),
        start_run=maker,
        notify=notifier,
        max_in_flight=max_in_flight,
        stale_after_days=stale_after_days,
        clock=clock.now,
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

    def test_a_paused_run_names_its_repository(
        self, connection: sqlite3.Connection
    ) -> None:
        _insert_run(
            connection, "plan-1", PlanningState.PAUSED.value, target_repo="api_test"
        )
        assert paused_repositories(connection) == {"api_test"}


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
        maker = RunMaker()
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
        file_row(store, "plan-1", kind="fix")
        file_row(store, "plan-2", kind="feature")
        loop, _ = build_loop(store, clock=clock, notifier=notifier, runs=runs)

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
    connection: sqlite3.Connection, build_id: str, status: str
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
            f"FEAT-{build_id}",
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

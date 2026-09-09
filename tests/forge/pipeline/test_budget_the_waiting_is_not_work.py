"""The wall-clock budget counts working time — waiting for a person is not work.

The fix journey's seventeenth attempt (2026-09-09) is the whole reason this
file exists. The build was queued at 22:03:03Z and paused at its build gate
for the owner's tap. He tapped at 04:50:31Z, six hours and forty-seven
minutes later; the gate resumed the build, and the very first conductor turn
refused to dispatch and closed the journey out failed — "stopped at the cap
(wall-clock (24448s) reached cap (7200s))". No leg had run. The two hours of
budget had been spent entirely on waiting for a person, because the number
the cap compared was simply now minus the build's start time — and the start
time is stamped the moment the build is queued.

So the number is now the time the build was ACTUALLY RUNNING: the elapsed
time since it started, minus every span it spent paused waiting for somebody
to decide something. The spans come out of the ledger itself — the pause rows
the gate already writes, closed by the note the resume writes — never out of
the daemon's memory, because forge-prod is recreated several times a day and
a restart must not lose the exclusion.

A build can be parked more than once, and the hours between two gates are
work. The second half of this file is about that: ``forge skip`` waves a
build on without writing a resume note, so the SKIPPED row it leaves behind
is read as the end of the wait — and if a wait ever ends with nothing
written down at all, the next gate must not swallow the work in between.

Everything here runs against a real SQLite ledger in a temporary directory,
through the real gate adapters, with the clock injected. The only pretend
parts are the supervisor's collaborators it never reaches on this path.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli.serve import (
    make_budget_started_at_reader,
    make_budget_waiting_seconds_reader,
)
from forge.config.models import BudgetGuards
from forge.gating.identity import derive_request_id
from forge.gating.models import GateDecision, GateMode
from forge.gating.sqlite_adapters import (
    GATE_RESUME_STAGE_LABEL,
    build_sqlite_gate_adapters,
    seconds_spent_waiting_for_a_person,
)
from forge.lifecycle import migrations
from forge.lifecycle.persistence import (
    Build,
    SqliteBuildResumer,
    SqliteLifecyclePersistence,
    SqliteStageSkipRecorder,
    StageLogEntry,
)
from forge.lifecycle.state_machine import (
    BuildState,
    transition as compose_transition,
)
from forge.pipeline.stage_taxonomy import StageClass
from forge.pipeline.supervisor import Supervisor, TurnOutcome

# Attempt seventeen's own clock, to the second.
STARTED_AT = datetime(2026, 9, 8, 22, 3, 3, tzinfo=UTC)
GATE_PAUSED_AT = datetime(2026, 9, 8, 22, 3, 10, tzinfo=UTC)
TAPPED_AT = datetime(2026, 9, 9, 4, 50, 31, tzinfo=UTC)
TEN_MINUTES_LATER = TAPPED_AT + timedelta(minutes=10)

#: 22:03:10 → 04:50:31. What the machine was not doing.
WAITED_SECONDS = (TAPPED_AT - GATE_PAUSED_AT).total_seconds()
#: 22:03:03 → 05:00:31, minus the wait. What the machine was doing.
WORKED_SECONDS = (TEN_MINUTES_LATER - STARTED_AT).total_seconds() - WAITED_SECONDS

STAGE_LABEL = "task-review"

#: "wire the production reader" — told apart from ``None``, which means
#: "wire no waiting reader at all".
THE_PRODUCTION_READER = object()


class SettableClock:
    """An injected ``() -> datetime`` a test moves by hand."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


# ---------------------------------------------------------------------------
# A real ledger in a temporary directory
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "forge.db"


@pytest.fixture()
def writer_db(db_path: Path) -> Iterator[sqlite3.Connection]:
    cx = sqlite_connect.connect_writer(db_path)
    migrations.apply_at_boot(cx)
    yield cx
    cx.close()


@pytest.fixture()
def pool(
    writer_db: sqlite3.Connection, db_path: Path
) -> SqliteLifecyclePersistence:
    return SqliteLifecyclePersistence(connection=writer_db, db_path=db_path)


@pytest.fixture()
def clock() -> SettableClock:
    return SettableClock(STARTED_AT)


@pytest.fixture()
def adapters(pool: SqliteLifecyclePersistence, clock: SettableClock):
    return build_sqlite_gate_adapters(pool, clock=clock)


def _payload(feature_id: str = "FEAT-39F6") -> SimpleNamespace:
    return SimpleNamespace(
        feature_id=feature_id,
        repo="guardkit/forge",
        branch="main",
        feature_yaml_path="features/fix.yaml",
        max_turns=5,
        sdk_timeout_seconds=1800,
        triggered_by="cli",
        originating_adapter="terminal",
        originating_user="rich",
        correlation_id=f"corr-{feature_id}",
        parent_request_id=None,
        queued_at=STARTED_AT,
    )


def _seed_running(
    pool: SqliteLifecyclePersistence,
    *,
    feature_id: str = "FEAT-39F6",
    started_at: datetime = STARTED_AT,
) -> str:
    """Queue a build, drive it to RUNNING, and stamp its start time."""
    build_id = pool.record_pending_build(_payload(feature_id), profile="fix-journey")
    for frm, to in (
        (BuildState.QUEUED, BuildState.PREPARING),
        (BuildState.PREPARING, BuildState.RUNNING),
    ):
        pool.apply_transition(
            compose_transition(Build(build_id=build_id, status=frm), to)
        )
    pool.connection.execute(
        "UPDATE builds SET started_at = ? WHERE build_id = ?",
        (started_at.isoformat(), build_id),
    )
    pool.connection.commit()
    return build_id


def _decision(build_id: str) -> GateDecision:
    return GateDecision(
        build_id=build_id,
        stage_label=STAGE_LABEL,
        target_kind="subagent",
        target_identifier="autobuild_runner",
        mode=GateMode.MANDATORY_HUMAN_APPROVAL,
        rationale="the owner taps the build gate",
        coach_score=None,
        criterion_breakdown={},
        detection_findings=[],
        evidence=[],
        decided_at=GATE_PAUSED_AT,
    )


def _pause_at_the_gate(
    adapters: Any,
    clock: SettableClock,
    *,
    build_id: str,
    at: datetime,
    attempt_count: int = 0,
    park: bool | None = None,
) -> None:
    """Park the build for a person, exactly as ``gate_check`` does.

    ``park`` says whether the build actually moves into PAUSED here. It
    defaults to "only the first card of a run", which is what the boot
    sweep's re-offers look like; a second, genuinely new gate later in the
    same build passes ``park=True``.
    """
    repo, sm = adapters
    clock.now = at
    decision = _decision(build_id)
    request_id = derive_request_id(
        build_id=build_id, stage_label=STAGE_LABEL, attempt_count=attempt_count
    )
    asyncio.run(repo.record_decision(decision))
    asyncio.run(
        repo.record_paused_build(
            build_id=build_id,
            feature_id="FEAT-39F6",
            stage_label=STAGE_LABEL,
            request_id=request_id,
            attempt_count=attempt_count,
            decision=decision,
        )
    )
    if park if park is not None else attempt_count == 0:
        asyncio.run(sm.transition_to_paused(build_id=build_id, stage_label=STAGE_LABEL))


def _forge_skip(
    pool: SqliteLifecyclePersistence, *, build_id: str, at: datetime
) -> None:
    """``forge skip``: the person waves the build on, exactly as the CLI does.

    The real recorder writes the SKIPPED row and the real resumer sends the
    build back to RUNNING — and that resume writes no note of its own, which
    is the whole point of the SKIPPED row being read as the end of the wait.
    """
    recorder = SqliteStageSkipRecorder(pool)
    recorder._now = lambda: at  # the CLI stamps from the wall clock
    recorder.record_skipped(build_id, StageClass.TASK_REVIEW, "skipped by rich")
    SqliteBuildResumer(pool).resume_after_skip(build_id, StageClass.TASK_REVIEW)


def _a_leg_ran(
    pool: SqliteLifecyclePersistence, *, build_id: str, at: datetime
) -> None:
    """One ordinary stage row: the ledger showing the machine doing work."""
    pool.record_stage(
        StageLogEntry(
            build_id=build_id,
            stage_label="task-work",
            target_kind="subagent",
            target_identifier="autobuild_runner",
            status="PASSED",
            gate_mode=None,
            started_at=at,
            completed_at=at,
            duration_secs=0.0,
            details={},
        )
    )


def _tap(adapters: Any, clock: SettableClock, *, build_id: str, at: datetime) -> None:
    """The person decides, and the gate lets the build carry on."""
    _repo, sm = adapters
    clock.now = at
    asyncio.run(sm.transition_to_running(build_id=build_id))


def _supervisor(
    pool: SqliteLifecyclePersistence,
    clock: SettableClock,
    *,
    cap_seconds: int,
    budget_pause: Any = None,
    waiting_reader: Any = THE_PRODUCTION_READER,
) -> Supervisor:
    """A supervisor wired with the production budget readers over the ledger."""
    if waiting_reader is THE_PRODUCTION_READER:
        waiting_reader = make_budget_waiting_seconds_reader(pool, clock=clock)
    return Supervisor(
        budget_guards=BudgetGuards(max_build_wallclock_seconds=cap_seconds),
        budget_profile_name="fix-journey",
        budget_wall_clock=clock,
        budget_started_at_reader=make_budget_started_at_reader(pool),
        budget_waiting_seconds_reader=waiting_reader,
        budget_pause=budget_pause,
        ordering_guard=MagicMock(name="ordering_guard"),
        per_feature_sequencer=MagicMock(name="per_feature_sequencer"),
        constitutional_guard=MagicMock(name="constitutional_guard"),
        state_reader=MagicMock(name="state_reader"),
        ordering_stage_log_reader=MagicMock(name="ordering_stage_log_reader"),
        per_feature_stage_log_reader=MagicMock(name="per_feature_stage_log_reader"),
        async_task_reader=MagicMock(name="async_task_reader"),
        reasoning_model=MagicMock(name="reasoning_model"),
        turn_recorder=MagicMock(name="turn_recorder"),
        specialist_dispatcher=AsyncMock(name="specialist_dispatcher"),
        subprocess_dispatcher=AsyncMock(name="subprocess_dispatcher"),
        autobuild_dispatcher=AsyncMock(name="autobuild_dispatcher"),
        pr_review_gate=MagicMock(name="pr_review_gate"),
    )


def _ask_the_guard(sup: Supervisor, build_id: str) -> Any:
    return asyncio.run(
        sup._enforce_mode_c_budget(
            build_id=build_id,
            build_state=BuildState.RUNNING,
            history=[],
            permitted=frozenset(),
        )
    )


# ---------------------------------------------------------------------------
# Attempt seventeen, and the ordinary build beside it
# ---------------------------------------------------------------------------


class TestTheWaitingIsNotWork:
    def test_a_night_at_the_gate_then_ten_minutes_of_work_is_not_a_breach(
        self, pool: SqliteLifecyclePersistence, clock: SettableClock, adapters: Any
    ) -> None:
        """Attempt seventeen, as it should have gone."""
        build_id = _seed_running(pool)
        _pause_at_the_gate(adapters, clock, build_id=build_id, at=GATE_PAUSED_AT)
        _tap(adapters, clock, build_id=build_id, at=TAPPED_AT)
        clock.now = TEN_MINUTES_LATER

        budget_pause = AsyncMock(name="budget_pause")
        sup = _supervisor(pool, clock, cap_seconds=7200, budget_pause=budget_pause)

        assert sup._budget_waiting_seconds(build_id) == pytest.approx(WAITED_SECONDS)
        assert sup._budget_elapsed_seconds(build_id) == pytest.approx(WORKED_SECONDS)
        # Ten minutes of work is nowhere near the two-hour cap.
        assert _ask_the_guard(sup, build_id) is None
        budget_pause.assert_not_awaited()

    def test_three_hours_of_real_work_still_breaches_in_the_old_words(
        self, pool: SqliteLifecyclePersistence, clock: SettableClock
    ) -> None:
        """The cap still bounds machine work, and says so as it always did."""
        build_id = _seed_running(pool)
        clock.now = STARTED_AT + timedelta(hours=3)

        budget_pause = AsyncMock(name="budget_pause")
        sup = _supervisor(pool, clock, cap_seconds=7200, budget_pause=budget_pause)

        report = _ask_the_guard(sup, build_id)

        assert report is not None
        assert report.outcome is TurnOutcome.PAUSED_BUDGET
        assert report.rationale == "wall-clock (10800s) reached cap (7200s)"
        budget_pause.assert_awaited_once()

    def test_a_breach_after_a_wait_says_what_it_did_not_count(
        self, pool: SqliteLifecyclePersistence, clock: SettableClock, adapters: Any
    ) -> None:
        build_id = _seed_running(pool)
        _pause_at_the_gate(adapters, clock, build_id=build_id, at=GATE_PAUSED_AT)
        _tap(adapters, clock, build_id=build_id, at=TAPPED_AT)
        clock.now = TEN_MINUTES_LATER

        sup = _supervisor(
            pool, clock, cap_seconds=300, budget_pause=AsyncMock(name="pause")
        )

        report = _ask_the_guard(sup, build_id)

        assert report is not None
        assert report.rationale == (
            f"wall-clock ({WORKED_SECONDS:.0f}s of work; "
            f"{WAITED_SECONDS:.0f}s of waiting for a person was not counted) "
            "reached cap (300s)"
        )

    def test_a_build_that_never_paused_reads_exactly_the_elapsed_time(
        self, pool: SqliteLifecyclePersistence, clock: SettableClock
    ) -> None:
        """No pause rows, no subtraction — the number is byte for byte today's."""
        build_id = _seed_running(pool)
        clock.now = STARTED_AT + timedelta(hours=1)

        sup = _supervisor(pool, clock, cap_seconds=7200)
        without_the_reader = _supervisor(
            pool, clock, cap_seconds=7200, waiting_reader=None
        )

        assert sup._budget_waiting_seconds(build_id) == 0.0
        assert sup._budget_elapsed_seconds(build_id) == 3600.0
        assert sup._budget_elapsed_seconds(
            build_id
        ) == without_the_reader._budget_elapsed_seconds(build_id)

    def test_two_separate_waits_are_both_taken_off(
        self, pool: SqliteLifecyclePersistence, clock: SettableClock, adapters: Any
    ) -> None:
        build_id = _seed_running(pool)
        first_paused = STARTED_AT + timedelta(minutes=5)
        first_tap = first_paused + timedelta(hours=2)
        second_paused = first_tap + timedelta(minutes=5)
        second_tap = second_paused + timedelta(hours=1)
        _pause_at_the_gate(adapters, clock, build_id=build_id, at=first_paused)
        _tap(adapters, clock, build_id=build_id, at=first_tap)
        _pause_at_the_gate(adapters, clock, build_id=build_id, at=second_paused)
        _tap(adapters, clock, build_id=build_id, at=second_tap)
        clock.now = second_tap + timedelta(minutes=10)

        sup = _supervisor(pool, clock, cap_seconds=7200)

        assert sup._budget_waiting_seconds(build_id) == pytest.approx(3 * 3600.0)
        # 3h20m on the clock, three hours of it waiting.
        assert sup._budget_elapsed_seconds(build_id) == pytest.approx(20 * 60.0)
        assert _ask_the_guard(sup, build_id) is None

    def test_a_wait_that_is_still_open_is_not_counted_as_work(
        self, pool: SqliteLifecyclePersistence, clock: SettableClock, adapters: Any
    ) -> None:
        """Nobody has decided yet, and the hours are passing right now."""
        build_id = _seed_running(pool)
        _pause_at_the_gate(adapters, clock, build_id=build_id, at=GATE_PAUSED_AT)
        clock.now = GATE_PAUSED_AT + timedelta(hours=6)

        sup = _supervisor(pool, clock, cap_seconds=7200)

        assert sup._budget_waiting_seconds(build_id) == pytest.approx(6 * 3600.0)
        # Seven seconds of work before the gate, and not a second since.
        assert sup._budget_elapsed_seconds(build_id) == pytest.approx(7.0)

    def test_the_boot_sweeps_re_offers_are_one_wait_not_fifteen(
        self, pool: SqliteLifecyclePersistence, clock: SettableClock, adapters: Any
    ) -> None:
        """The sweep re-cards a paused build every half hour, all night."""
        build_id = _seed_running(pool)
        _pause_at_the_gate(adapters, clock, build_id=build_id, at=GATE_PAUSED_AT)
        for attempt in range(1, 14):
            _pause_at_the_gate(
                adapters,
                clock,
                build_id=build_id,
                at=GATE_PAUSED_AT + timedelta(minutes=30 * attempt),
                attempt_count=attempt,
            )
        _tap(adapters, clock, build_id=build_id, at=TAPPED_AT)
        clock.now = TEN_MINUTES_LATER

        sup = _supervisor(pool, clock, cap_seconds=7200)

        assert sup._budget_waiting_seconds(build_id) == pytest.approx(WAITED_SECONDS)
        assert _ask_the_guard(sup, build_id) is None


# ---------------------------------------------------------------------------
# Two gates in one build, and the ends of waits that write no note
# ---------------------------------------------------------------------------


#: The second-gate story, to the minute: parked at ten to ten at night,
#: waved on with ``forge skip`` forty minutes later, five hours of real work,
#: parked again at twenty to four, tapped ten minutes after that, and the
#: guard asked at four. Fifty minutes of waiting; five hours ten of work.
FIRST_GATE_AT = datetime(2026, 9, 8, 22, 0, tzinfo=UTC)
WAVED_ON_AT = datetime(2026, 9, 8, 22, 40, tzinfo=UTC)
SECOND_GATE_AT = datetime(2026, 9, 9, 3, 40, tzinfo=UTC)
SECOND_TAP_AT = datetime(2026, 9, 9, 3, 50, tzinfo=UTC)
ASKED_AT = datetime(2026, 9, 9, 4, 0, tzinfo=UTC)
TWO_WAITS_SECONDS = 50 * 60.0
TWO_WAITS_WORK_SECONDS = (ASKED_AT - FIRST_GATE_AT).total_seconds() - TWO_WAITS_SECONDS


class TestASecondGateLaterInTheSameBuild:
    """A build can be parked twice, and the hours between are work."""

    def test_a_skip_ends_the_wait_it_was_asked_about(
        self, pool: SqliteLifecyclePersistence, clock: SettableClock, adapters: Any
    ) -> None:
        """``forge skip`` writes no resume note — its SKIPPED row is the end.

        Without that, the first pause would open a wait nothing closed, and
        the second gate's tap would close one enormous wait that swallowed
        the five hours of real work in between: the build would be charged
        twenty minutes instead of five hours ten, and would sail past a cap
        it had genuinely blown.
        """
        build_id = _seed_running(pool, started_at=FIRST_GATE_AT)
        _pause_at_the_gate(adapters, clock, build_id=build_id, at=FIRST_GATE_AT)

        _forge_skip(pool, build_id=build_id, at=WAVED_ON_AT)

        # Five hours of the machine actually working.
        _a_leg_ran(pool, build_id=build_id, at=WAVED_ON_AT + timedelta(hours=1))
        _a_leg_ran(pool, build_id=build_id, at=WAVED_ON_AT + timedelta(hours=4))

        clock.now = SECOND_GATE_AT
        _pause_at_the_gate(
            adapters,
            clock,
            build_id=build_id,
            at=SECOND_GATE_AT,
            attempt_count=1,
            park=True,
        )
        _tap(adapters, clock, build_id=build_id, at=SECOND_TAP_AT)
        clock.now = ASKED_AT

        sup = _supervisor(
            pool, clock, cap_seconds=7200, budget_pause=AsyncMock(name="pause")
        )

        # Forty minutes at the first gate, ten at the second. Nothing else.
        assert sup._budget_waiting_seconds(build_id) == pytest.approx(TWO_WAITS_SECONDS)
        assert sup._budget_elapsed_seconds(build_id) == pytest.approx(
            TWO_WAITS_WORK_SECONDS
        )
        # Five hours ten of work is well past a two-hour cap, so it breaches.
        report = _ask_the_guard(sup, build_id)
        assert report is not None
        assert report.outcome is TurnOutcome.PAUSED_BUDGET

    def test_a_refused_skip_is_not_a_decision_to_carry_on(self) -> None:
        """The person asked; the guard said no; the build is still parked."""
        rows = [
            SimpleNamespace(
                started_at=FIRST_GATE_AT, status="GATED", details={"gate_pause": {}}
            ),
            SimpleNamespace(
                started_at=WAVED_ON_AT,
                status="GATED",
                target_identifier="cli-skip",
                details={"rationale": "refused", "refused": True},
            ),
        ]
        assert seconds_spent_waiting_for_a_person(
            rows, now=SECOND_TAP_AT
        ) == pytest.approx((SECOND_TAP_AT - FIRST_GATE_AT).total_seconds())

    def test_a_resume_that_writes_nothing_cannot_swallow_the_work_after_it(
        self, pool: SqliteLifecyclePersistence, clock: SettableClock, adapters: Any
    ) -> None:
        """Some future path resumes a build silently, and it parks again.

        The ledger cannot show when that first wait ended, so it is closed
        where the ledger last saw the build waiting. The waiting is
        under-counted — the direction that keeps the cap bounding machine
        work — and the five hours in between are still charged as work.
        """
        build_id = _seed_running(pool, started_at=FIRST_GATE_AT)
        _pause_at_the_gate(adapters, clock, build_id=build_id, at=FIRST_GATE_AT)

        # A resume with nothing written down at all.
        pool.apply_transition(
            compose_transition(
                Build(build_id=build_id, status=BuildState.PAUSED),
                BuildState.RUNNING,
            )
        )
        _a_leg_ran(pool, build_id=build_id, at=WAVED_ON_AT + timedelta(hours=1))
        _a_leg_ran(pool, build_id=build_id, at=WAVED_ON_AT + timedelta(hours=4))
        _pause_at_the_gate(
            adapters,
            clock,
            build_id=build_id,
            at=SECOND_GATE_AT,
            attempt_count=1,
            park=True,
        )
        _tap(adapters, clock, build_id=build_id, at=SECOND_TAP_AT)
        clock.now = ASKED_AT

        sup = _supervisor(
            pool, clock, cap_seconds=7200, budget_pause=AsyncMock(name="pause")
        )

        # Only the second gate's ten minutes can be shown, so only they count.
        assert sup._budget_waiting_seconds(build_id) == pytest.approx(10 * 60.0)
        report = _ask_the_guard(sup, build_id)
        assert report is not None
        assert report.outcome is TurnOutcome.PAUSED_BUDGET

    def test_the_sweeps_re_offers_are_still_one_wait_beside_a_second_gate(
        self, pool: SqliteLifecyclePersistence, clock: SettableClock, adapters: Any
    ) -> None:
        """The night's re-offers stay one wait; the later gate is its own."""
        build_id = _seed_running(pool, started_at=FIRST_GATE_AT)
        _pause_at_the_gate(adapters, clock, build_id=build_id, at=FIRST_GATE_AT)
        for attempt in range(1, 4):
            _pause_at_the_gate(
                adapters,
                clock,
                build_id=build_id,
                at=FIRST_GATE_AT + timedelta(minutes=10 * attempt),
                attempt_count=attempt,
            )
        _tap(adapters, clock, build_id=build_id, at=WAVED_ON_AT)
        _a_leg_ran(pool, build_id=build_id, at=WAVED_ON_AT + timedelta(hours=1))
        _pause_at_the_gate(
            adapters,
            clock,
            build_id=build_id,
            at=SECOND_GATE_AT,
            attempt_count=9,
            park=True,
        )
        _tap(adapters, clock, build_id=build_id, at=SECOND_TAP_AT)
        clock.now = ASKED_AT

        sup = _supervisor(pool, clock, cap_seconds=7200)

        assert sup._budget_waiting_seconds(build_id) == pytest.approx(TWO_WAITS_SECONDS)


# ---------------------------------------------------------------------------
# The note the resume writes, and what it survives
# ---------------------------------------------------------------------------


class TestTheResumeNote:
    def test_the_note_is_written_once_however_often_the_decision_arrives(
        self, pool: SqliteLifecyclePersistence, clock: SettableClock, adapters: Any
    ) -> None:
        """A decision recorded twice must not subtract the wait twice."""
        build_id = _seed_running(pool)
        _pause_at_the_gate(adapters, clock, build_id=build_id, at=GATE_PAUSED_AT)
        _tap(adapters, clock, build_id=build_id, at=TAPPED_AT)
        # The same approval comes round again an hour later — a redelivery.
        _tap(adapters, clock, build_id=build_id, at=TAPPED_AT + timedelta(hours=1))
        clock.now = TEN_MINUTES_LATER

        rows = [
            row
            for row in pool.read_stages(build_id)
            if row.stage_label == GATE_RESUME_STAGE_LABEL
        ]
        assert len(rows) == 1

        sup = _supervisor(pool, clock, cap_seconds=7200)
        assert sup._budget_waiting_seconds(build_id) == pytest.approx(WAITED_SECONDS)

    def test_a_second_note_would_still_close_only_one_wait(self) -> None:
        """Belt and braces: two resume rows in a row subtract one wait."""
        rows = [
            SimpleNamespace(started_at=GATE_PAUSED_AT, details={"gate_pause": {}}),
            SimpleNamespace(started_at=TAPPED_AT, details={"gate_resume": {}}),
            SimpleNamespace(
                started_at=TAPPED_AT + timedelta(hours=1),
                details={"gate_resume": {}},
            ),
        ]
        assert seconds_spent_waiting_for_a_person(
            rows, now=TEN_MINUTES_LATER
        ) == pytest.approx(WAITED_SECONDS)

    def test_a_wait_with_no_end_written_down_cannot_excuse_a_build_for_ever(
        self, pool: SqliteLifecyclePersistence, clock: SettableClock, adapters: Any
    ) -> None:
        """Some other path resumed the build and filed no note.

        The build is running again, so the wait is closed at the last moment
        the ledger actually saw it waiting — not left open to swallow every
        hour of work that follows. The cap has to keep bounding machine work.
        """
        build_id = _seed_running(pool)
        _pause_at_the_gate(adapters, clock, build_id=build_id, at=GATE_PAUSED_AT)
        _pause_at_the_gate(
            adapters,
            clock,
            build_id=build_id,
            at=GATE_PAUSED_AT + timedelta(minutes=30),
            attempt_count=1,
        )
        # A resume that writes nothing down (the steer-skip path does this).
        pool.apply_transition(
            compose_transition(
                Build(build_id=build_id, status=BuildState.PAUSED),
                BuildState.RUNNING,
            )
        )
        clock.now = GATE_PAUSED_AT + timedelta(hours=6)

        sup = _supervisor(
            pool, clock, cap_seconds=7200, budget_pause=AsyncMock(name="pause")
        )

        # Only the half hour the ledger can show is excused.
        assert sup._budget_waiting_seconds(build_id) == pytest.approx(30 * 60.0)
        report = _ask_the_guard(sup, build_id)
        assert report is not None
        assert report.outcome is TurnOutcome.PAUSED_BUDGET

    def test_the_exclusion_survives_a_restart(
        self,
        pool: SqliteLifecyclePersistence,
        clock: SettableClock,
        adapters: Any,
        writer_db: sqlite3.Connection,
        db_path: Path,
    ) -> None:
        """forge-prod is recreated several times a day; the ledger is not."""
        build_id = _seed_running(pool)
        _pause_at_the_gate(adapters, clock, build_id=build_id, at=GATE_PAUSED_AT)
        _tap(adapters, clock, build_id=build_id, at=TAPPED_AT)
        clock.now = TEN_MINUTES_LATER
        before = _supervisor(pool, clock, cap_seconds=7200)._budget_waiting_seconds(
            build_id
        )

        # The daemon goes away, taking every scrap of process memory with it.
        writer_db.close()
        restarted_cx = sqlite_connect.connect_writer(db_path)
        migrations.apply_at_boot(restarted_cx)
        try:
            restarted_pool = SqliteLifecyclePersistence(
                connection=restarted_cx, db_path=db_path
            )
            after = _supervisor(
                restarted_pool, clock, cap_seconds=7200
            )._budget_waiting_seconds(build_id)
        finally:
            restarted_cx.close()

        assert before == pytest.approx(WAITED_SECONDS)
        assert after == before


# ---------------------------------------------------------------------------
# The lifecycle bridge's twin, on the same ledger
# ---------------------------------------------------------------------------


class TestTheTwinAgrees:
    def test_the_observer_and_the_supervisor_read_the_same_number(
        self, pool: SqliteLifecyclePersistence, clock: SettableClock, adapters: Any
    ) -> None:
        from forge.lifecycle_bridge.budget_observer import BudgetBreachObserver

        build_id = _seed_running(pool)
        _pause_at_the_gate(adapters, clock, build_id=build_id, at=GATE_PAUSED_AT)
        _tap(adapters, clock, build_id=build_id, at=TAPPED_AT)
        clock.now = TEN_MINUTES_LATER

        sup = _supervisor(
            pool, clock, cap_seconds=300, budget_pause=AsyncMock(name="pause")
        )
        report = _ask_the_guard(sup, build_id)
        assert report is not None

        started_at_reader = make_budget_started_at_reader(pool)

        def elapsed(bid: str) -> float:
            started = started_at_reader(bid)
            assert started is not None
            return (clock() - started).total_seconds()

        recorded: list[tuple[str, str]] = []
        observer = BudgetBreachObserver(
            resolve_budget=lambda _bid: (
                BudgetGuards(max_build_wallclock_seconds=300),
                "fix-journey",
            ),
            elapsed_seconds=elapsed,
            waiting_seconds=make_budget_waiting_seconds_reader(pool, clock=clock),
            read_coach_score=lambda _bid: None,
            record_breach=lambda bid, detail: recorded.append((bid, detail)),
            publish_approval_request=AsyncMock(name="publish"),
            approval_subject_for=lambda bid: f"agents.approval.forge.{bid}",
            clock=clock,
        )
        asyncio.run(
            observer.observe_stage_complete(
                observer.new_session(),
                build_id=build_id,
                feature_id="FEAT-39F6",
                coach_score=None,
            )
        )

        assert len(recorded) == 1
        # The twin wrote the supervisor's own sentence, word for word.
        assert report.rationale in recorded[0][1]

    def test_the_production_observer_is_wired_to_the_ledgers_waits(
        self, pool: SqliteLifecyclePersistence, clock: SettableClock, adapters: Any
    ) -> None:
        """The factory composes the waiting reader, not just the elapsed one."""
        from forge.lifecycle_bridge.budget_observer import (
            build_budget_breach_observer,
        )

        build_id = _seed_running(pool)
        _pause_at_the_gate(adapters, clock, build_id=build_id, at=GATE_PAUSED_AT)
        _tap(adapters, clock, build_id=build_id, at=TAPPED_AT)

        config = SimpleNamespace(
            budget=SimpleNamespace(
                resolve=lambda _name: BudgetGuards(max_build_wallclock_seconds=1),
                default_profile="fix-journey",
            )
        )
        observer = build_budget_breach_observer(
            pool=pool,
            config=config,
            publish_approval_request=AsyncMock(name="publish"),
        )
        asyncio.run(
            observer.observe_stage_complete(
                observer.new_session(),
                build_id=build_id,
                feature_id="FEAT-39F6",
                coach_score=None,
            )
        )

        detail = pool.read_budget_breach(build_id)
        assert detail is not None
        assert "of waiting for a person was not counted" in detail
        assert f"{WAITED_SECONDS:.0f}s of waiting" in detail

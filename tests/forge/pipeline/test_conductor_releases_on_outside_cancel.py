"""A fix journey cancelled from outside gives its queued message back at once.

Observed on 19 October 2026: ``forge cancel`` on a fix journey (mode-c)
marked its row CANCELLED directly ("build transitioned directly to
CANCELLED") while the conductor was half-way through a ``/task-work`` stage
that runs for up to half an hour. The cancel runs in its own process and
cannot acknowledge the ``build-queued`` message the coordinator holds; the
coordinator only read the row again at its next turn, after the stage. So
the message held one of the build places, ``register-repo --check-drained``
said NOT DRAINED, and the place came back only at the redelivery.

What this file pins:

* a cancel while a stage runs releases the message promptly, while the
  stage is still running, and through the same release the close-out uses;
* the bridge then holds nothing for the feature (the drained check's view);
* the journey still closes out as before, and the message is acknowledged
  exactly once in all;
* a cancel while the journey waits cuts the wait short and closes out;
* a cancel of a build whose message is already released does no harm;
* a journey that is not cancelled keeps its message for the whole stage.

The cancel is the real ``forge cancel`` handler against a real ledger, and
the acknowledgement handle is the real idempotent one the consumer makes.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli._serve_conductor import build_conductor_driver_deps_factory
from forge.cli.runtime import build_cli_runtime
from forge.config.models import ForgeConfig
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.lifecycle.state_machine import BuildState
from forge.pipeline.build_ack_handle import make_msg_ack_handle
from forge.pipeline.cli_steering import CancelStatus
from forge.pipeline.conductor_driver import (
    ConductorDriverDeps,
    ConductorRunOutcome,
    WaitWindow,
    drive_fix_journey,
)
from forge.pipeline.supervisor import TurnOutcome, TurnReport

BUILD_ID = "build-FEAT-0C1A-20260101062916"
FEATURE_ID = "FEAT-0C1A"
#: A fix journey's correlation id names its parent build, not the feature.
CORRELATION_ID = "fix-build-FEAT-0C1A-20260101060102"
#: The next build of the same feature, admitted once the place is free.
NEXT_CORRELATION_ID = "corr-next-build-of-FEAT-0C1A"
#: Short enough that "promptly" is measured in milliseconds here.
WATCH_SECONDS = 0.01
#: The longest a test waits for something that should happen at once.
PROMPT_SECONDS = 2.0


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


@dataclass
class FakeMsg:
    """The JetStream message; counts acknowledgements."""

    acks: int = 0

    async def ack(self) -> None:
        self.acks += 1

    async def nak(self) -> None:  # pragma: no cover - never used here
        raise AssertionError("a cancelled journey acknowledges; it never naks")


@dataclass
class FakeBridge:
    """The half of the lifecycle bridge the conductor is given.

    ``take_ack_handle`` hands the handle over and forgets it, exactly as
    the real wireup does, and only to the build it belongs to; ``handles``
    is what the bridge still holds. Handles given to the constructor belong
    to this journey.
    """

    handles: dict[str, Any] = field(default_factory=dict)
    owners: dict[str, str] = field(default_factory=dict)
    asked: list[tuple[str, str | None]] = field(default_factory=list)
    check_identity: bool = True

    def register(self, feature_id: str, correlation_id: str, handle: Any) -> None:
        self.handles[feature_id] = handle
        self.owners[feature_id] = correlation_id

    def take_ack_handle(self, feature_id: str, correlation_id: str | None = None) -> Any:
        self.asked.append((feature_id, correlation_id))
        owner = self.owners.get(feature_id, CORRELATION_ID)
        if self.check_identity and correlation_id is not None and owner != correlation_id:
            return None
        self.owners.pop(feature_id, None)
        return self.handles.pop(feature_id, None)


class StageInFlight:
    """A supervisor whose first turn runs a long stage until told to finish.

    Later turns read the build's real row, as the real supervisor does, and
    answer TERMINAL once it is terminal.
    """

    def __init__(self, pool: SqliteLifecyclePersistence, first: TurnOutcome) -> None:
        self._pool = pool
        self._first = first
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.turns = 0

    async def next_turn(self, build_id: str) -> Any:
        self.turns += 1
        row = self._pool.get_build_row(build_id)
        if row.status in (BuildState.CANCELLED, BuildState.FAILED, BuildState.COMPLETE):
            return _report(TurnOutcome.TERMINAL, rationale="terminal")
        if self.turns == 1:
            self.started.set()
            if self._first is TurnOutcome.DISPATCHED:
                await self.finish.wait()
                return _report(
                    self._first, rationale="a task-work stage", settled=True
                )
            return _report(self._first, rationale="waiting on a signal")
        return _report(
            TurnOutcome.DISPATCHED, rationale="another stage", settled=True
        )


@dataclass(frozen=True)
class _Settled:
    """A stage dispatch that ran to completion inside the turn."""

    status: str = "success"


def _report(
    outcome: TurnOutcome, *, rationale: str = "", settled: bool = False
) -> TurnReport:
    return TurnReport(
        outcome=outcome,
        build_id=BUILD_ID,
        rationale=rationale,
        dispatch_result=_Settled() if settled else None,
    )


async def _no_sleep(_seconds: float) -> None:
    return None


class _SpyNotifier:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def notify_cancelled(self, *, build_id: str, reason: str, cancelled_by: str) -> None:
        self.calls.append(build_id)


# ---------------------------------------------------------------------------
# A real ledger with one running fix journey
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "forge.db"
    cx: sqlite3.Connection = sqlite_connect.connect_writer(path)
    migrations.apply_at_boot(cx)
    cx.execute(
        "INSERT INTO builds (build_id, feature_id, repo, branch, "
        "feature_yaml_path, status, triggered_by, correlation_id, queued_at, "
        "started_at, worktree_path, mode, task_id) VALUES (?, ?, "
        "'example/notes', 'repair/TASK-FEAT0C1AFIX1', 'f.yaml', 'RUNNING', "
        "'forge-internal', 'fix-build-FEAT-0C1A-20260101060102', "
        "'2026-01-01T06:29:16Z', '2026-01-01T06:29:16Z', '/work/journey', "
        "'mode-c', 'TASK-FEAT0C1AFIX1')",
        (BUILD_ID, FEATURE_ID),
    )
    cx.commit()
    cx.close()
    return path


@pytest.fixture
def pool(db_path: Path) -> SqliteLifecyclePersistence:
    return SqliteLifecyclePersistence(connection=sqlite_connect.connect_writer(db_path))


def _config() -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "pipeline": {
                "build_queue_subject": "pipeline.build-queued.team-a",
                "approved_originators": ["terminal"],
            },
            "permissions": {"filesystem": {"allowlist": ["/work"]}},
            "conductor": {"enabled": True, "seat": "qwen3-coder-30b"},
        }
    )


def _deps(
    pool: SqliteLifecyclePersistence,
    bridge: FakeBridge,
    supervisor: Any,
    **overrides: Any,
) -> ConductorDriverDeps:
    """The daemon's own deps factory, with test-sized timings."""
    deps = build_conductor_driver_deps_factory(
        pool=pool, config=_config(), take_ack_handle=bridge.take_ack_handle
    )(BUILD_ID, supervisor)
    return dataclasses.replace(
        deps,
        sleep=_no_sleep,
        ended_watch_seconds=WATCH_SECONDS,
        export_stage_receipts=None,
        write_failure_pack=None,
        **overrides,
    )


def _forge_cancel(db_path: Path) -> Any:
    """``forge cancel`` as its own process does it: the real handler."""
    runtime = build_cli_runtime(
        db_path,
        async_task_canceller=lambda _task_id: None,
        cancelled_notifier=_SpyNotifier(),
    )
    return runtime.cli_steering_handler.handle_cancel(
        build_id=BUILD_ID, reason="stopped for a release", responder="forge"
    )


async def _until(predicate: Any) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(WATCH_SECONDS)

    await asyncio.wait_for(_poll(), timeout=PROMPT_SECONDS)


# ---------------------------------------------------------------------------
# The cancel releases the message while the stage is still running
# ---------------------------------------------------------------------------


class TestACancelDuringAStageReleasesTheMessageAtOnce:
    def test_the_message_is_acknowledged_before_the_stage_finishes(
        self, db_path: Path, pool: SqliteLifecyclePersistence
    ) -> None:
        msg = FakeMsg()
        bridge = FakeBridge(handles={FEATURE_ID: make_msg_ack_handle(msg)})
        supervisor = StageInFlight(pool, TurnOutcome.DISPATCHED)

        async def scenario() -> Any:
            journey = asyncio.ensure_future(
                drive_fix_journey(BUILD_ID, _deps(pool, bridge, supervisor))
            )
            await asyncio.wait_for(supervisor.started.wait(), PROMPT_SECONDS)
            # Several watch ticks with the row still RUNNING: nothing goes.
            await asyncio.sleep(WATCH_SECONDS * 5)
            assert msg.acks == 0
            assert FEATURE_ID in bridge.handles

            outcome = _forge_cancel(db_path)
            assert outcome.status is CancelStatus.CANCELLED_DIRECT

            await _until(lambda: msg.acks == 1)
            # The stage is still running, and the bridge holds nothing.
            assert not supervisor.finish.is_set()
            assert not journey.done()
            assert bridge.handles == {}

            supervisor.finish.set()
            return await asyncio.wait_for(journey, PROMPT_SECONDS)

        report = asyncio.run(scenario())

        assert report.outcome is ConductorRunOutcome.COMPLETED
        assert msg.acks == 1
        assert pool.get_build_row(BUILD_ID).status is BuildState.CANCELLED

    def test_a_journey_that_is_not_cancelled_keeps_its_message_for_the_stage(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        msg = FakeMsg()
        bridge = FakeBridge(handles={FEATURE_ID: make_msg_ack_handle(msg)})
        supervisor = StageInFlight(pool, TurnOutcome.DISPATCHED)

        async def scenario() -> None:
            journey = asyncio.ensure_future(
                drive_fix_journey(
                    BUILD_ID, _deps(pool, bridge, supervisor, max_turns=1)
                )
            )
            await asyncio.wait_for(supervisor.started.wait(), PROMPT_SECONDS)
            await asyncio.sleep(WATCH_SECONDS * 10)
            assert msg.acks == 0
            supervisor.finish.set()
            await asyncio.wait_for(journey, PROMPT_SECONDS)

        asyncio.run(scenario())

    def test_a_cancel_of_a_build_already_released_does_no_harm(
        self,
        db_path: Path,
        pool: SqliteLifecyclePersistence,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The message went already (the redelivery's duplicate check, say)."""
        bridge = FakeBridge(handles={})
        supervisor = StageInFlight(pool, TurnOutcome.DISPATCHED)

        async def scenario() -> Any:
            journey = asyncio.ensure_future(
                drive_fix_journey(BUILD_ID, _deps(pool, bridge, supervisor))
            )
            await asyncio.wait_for(supervisor.started.wait(), PROMPT_SECONDS)
            _forge_cancel(db_path)
            await _until(
                lambda: any(
                    "nothing to release" in r.getMessage() for r in caplog.records
                )
            )
            supervisor.finish.set()
            return await asyncio.wait_for(journey, PROMPT_SECONDS)

        with caplog.at_level(logging.INFO):
            report = asyncio.run(scenario())

        assert report.outcome is ConductorRunOutcome.COMPLETED
        # The early release found nothing to release; the close-out does not
        # ask again (the journey's release is done), and nothing raised.
        released = [
            r for r in caplog.records if "nothing to release" in r.getMessage()
        ]
        assert len(released) == 1
        assert bridge.asked == [(FEATURE_ID, CORRELATION_ID)]
        assert any("not asking again" in r.getMessage() for r in caplog.records)
        assert not [r for r in caplog.records if "raised" in r.getMessage()]
        assert pool.get_build_row(BUILD_ID).status is BuildState.CANCELLED

    def test_a_stage_that_fails_after_the_cancel_still_closes_out(
        self, db_path: Path, pool: SqliteLifecyclePersistence
    ) -> None:
        """The stage raising after the release is the turn's own error path."""
        msg = FakeMsg()
        bridge = FakeBridge(handles={FEATURE_ID: make_msg_ack_handle(msg)})

        class RaisingStage(StageInFlight):
            async def next_turn(self, build_id: str) -> Any:
                self.started.set()
                await self.finish.wait()
                raise RuntimeError("the stage's process was stopped")

        supervisor = RaisingStage(pool, TurnOutcome.DISPATCHED)

        async def scenario() -> Any:
            journey = asyncio.ensure_future(
                drive_fix_journey(BUILD_ID, _deps(pool, bridge, supervisor))
            )
            await asyncio.wait_for(supervisor.started.wait(), PROMPT_SECONDS)
            _forge_cancel(db_path)
            await _until(lambda: msg.acks == 1)
            supervisor.finish.set()
            return await asyncio.wait_for(journey, PROMPT_SECONDS)

        report = asyncio.run(scenario())

        assert report.outcome is ConductorRunOutcome.ERROR
        assert msg.acks == 1
        # The stop's close-out does not overwrite the cancel.
        assert pool.get_build_row(BUILD_ID).status is BuildState.CANCELLED


# ---------------------------------------------------------------------------
# The next build of the same feature keeps its message
# ---------------------------------------------------------------------------


class TestTheNextBuildOfTheFeatureKeepsItsMessage:
    """Codex R1 and the coach, 19 October 2026.

    A fix journey shares its feature id with its parent and with any later
    build of the feature. Once the cancel has released the journey's place,
    a new build B of the same feature can be admitted and register its own
    handle in the bridge before the cancelled journey's stage ends. The
    journey's close-out must leave B's message held. Two guards stop it,
    and each is pinned alone here: the journey releases at most once, and
    the bridge hands a handle only to the build it belongs to.
    """

    def _cancel_then_admit_next_build(
        self,
        db_path: Path,
        pool: SqliteLifecyclePersistence,
        bridge: FakeBridge,
        msg_a: FakeMsg,
        msg_b: FakeMsg,
    ) -> Any:
        supervisor = StageInFlight(pool, TurnOutcome.DISPATCHED)

        async def scenario() -> Any:
            journey = asyncio.ensure_future(
                drive_fix_journey(BUILD_ID, _deps(pool, bridge, supervisor))
            )
            await asyncio.wait_for(supervisor.started.wait(), PROMPT_SECONDS)
            _forge_cancel(db_path)
            await _until(lambda: msg_a.acks == 1)
            # B is admitted into the freed place and registers its handle
            # while A's stage is still running.
            bridge.register(FEATURE_ID, NEXT_CORRELATION_ID, make_msg_ack_handle(msg_b))
            supervisor.finish.set()
            return await asyncio.wait_for(journey, PROMPT_SECONDS)

        return asyncio.run(scenario())

    def test_the_cancelled_journeys_close_out_leaves_the_next_build_held(
        self, db_path: Path, pool: SqliteLifecyclePersistence
    ) -> None:
        msg_a, msg_b = FakeMsg(), FakeMsg()
        bridge = FakeBridge(handles={FEATURE_ID: make_msg_ack_handle(msg_a)})

        report = self._cancel_then_admit_next_build(db_path, pool, bridge, msg_a, msg_b)

        assert report.outcome is ConductorRunOutcome.COMPLETED
        assert msg_a.acks == 1
        assert msg_b.acks == 0
        assert FEATURE_ID in bridge.handles
        assert bridge.owners[FEATURE_ID] == NEXT_CORRELATION_ID

    def test_releasing_once_alone_protects_the_next_build(
        self, db_path: Path, pool: SqliteLifecyclePersistence
    ) -> None:
        """Even a bridge that hands out by feature alone is not asked twice."""
        msg_a, msg_b = FakeMsg(), FakeMsg()
        bridge = FakeBridge(
            handles={FEATURE_ID: make_msg_ack_handle(msg_a)}, check_identity=False
        )

        self._cancel_then_admit_next_build(db_path, pool, bridge, msg_a, msg_b)

        assert msg_b.acks == 0
        assert len(bridge.asked) == 1
        assert FEATURE_ID in bridge.handles

    def test_the_identity_check_alone_protects_the_next_build(
        self, db_path: Path, pool: SqliteLifecyclePersistence
    ) -> None:
        """A release asked for by A never takes B's handle."""
        msg_b = FakeMsg()
        bridge = FakeBridge()
        bridge.register(FEATURE_ID, NEXT_CORRELATION_ID, make_msg_ack_handle(msg_b))
        _forge_cancel(db_path)
        deps = _deps(pool, bridge, supervisor=object())

        asyncio.run(deps.release_queue_message(BUILD_ID))

        assert bridge.asked == [(FEATURE_ID, CORRELATION_ID)]
        assert msg_b.acks == 0
        assert FEATURE_ID in bridge.handles


# ---------------------------------------------------------------------------
# The cancel ends a wait, and the journey closes out
# ---------------------------------------------------------------------------


class TestACancelDuringAWaitClosesTheJourneyOut:
    def test_the_wait_is_cut_short_and_the_message_released(
        self, db_path: Path, pool: SqliteLifecyclePersistence
    ) -> None:
        msg = FakeMsg()
        bridge = FakeBridge(handles={FEATURE_ID: make_msg_ack_handle(msg)})
        supervisor = StageInFlight(pool, TurnOutcome.WAITING)
        waiting = asyncio.Event()

        async def subscribe_resume(
            build_id: str, *, armed: asyncio.Event, timeout_seconds: int
        ) -> Any:
            armed.set()
            waiting.set()
            await asyncio.Event().wait()  # nobody will ever answer

        async def scenario() -> Any:
            journey = asyncio.ensure_future(
                drive_fix_journey(
                    BUILD_ID,
                    _deps(
                        pool,
                        bridge,
                        supervisor,
                        wait_window_reader=lambda _b: WaitWindow(
                            remaining_seconds=3600.0
                        ),
                        subscribe_resume=subscribe_resume,
                    ),
                )
            )
            await asyncio.wait_for(waiting.wait(), PROMPT_SECONDS)
            assert msg.acks == 0
            _forge_cancel(db_path)
            return await asyncio.wait_for(journey, PROMPT_SECONDS)

        report = asyncio.run(scenario())

        assert report.outcome is ConductorRunOutcome.COMPLETED
        assert msg.acks == 1
        assert bridge.handles == {}


# ---------------------------------------------------------------------------
# The seam is wired where the daemon composes it, and optional
# ---------------------------------------------------------------------------


class TestTheEndedCheckIsWired:
    def test_the_deps_factory_reads_the_row(
        self, db_path: Path, pool: SqliteLifecyclePersistence
    ) -> None:
        deps = _deps(pool, FakeBridge(), supervisor=object())

        assert deps.build_has_ended is not None
        assert deps.build_has_ended(BUILD_ID) is False
        _forge_cancel(db_path)
        assert deps.build_has_ended(BUILD_ID) is True
        assert deps.build_has_ended("build-nobody-queued") is False

    def test_a_loop_with_no_ended_check_behaves_as_before(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        supervisor = StageInFlight(pool, TurnOutcome.TERMINAL)

        report = asyncio.run(
            drive_fix_journey(BUILD_ID, ConductorDriverDeps(supervisor=supervisor))
        )

        assert report.outcome is ConductorRunOutcome.COMPLETED

    def test_an_ended_check_that_raises_is_read_as_not_ended(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        msg = FakeMsg()
        bridge = FakeBridge(handles={FEATURE_ID: make_msg_ack_handle(msg)})
        supervisor = StageInFlight(pool, TurnOutcome.DISPATCHED)

        def broken(_build_id: str) -> bool:
            raise sqlite3.OperationalError("database is locked")

        async def scenario() -> None:
            journey = asyncio.ensure_future(
                drive_fix_journey(
                    BUILD_ID,
                    _deps(pool, bridge, supervisor, build_has_ended=broken, max_turns=1),
                )
            )
            await asyncio.wait_for(supervisor.started.wait(), PROMPT_SECONDS)
            await asyncio.sleep(WATCH_SECONDS * 5)
            assert msg.acks == 0
            supervisor.finish.set()
            await asyncio.wait_for(journey, PROMPT_SECONDS)

        asyncio.run(scenario())

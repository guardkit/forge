"""A mission feature's build is RUNNING before its runner starts (17 October 2026).

The live missions trial (mission m-f23129, release 2026.10.17-1) found the
mission build-gate skip launching with the row still QUEUED. An hour later
JetStream redelivered the unacked build-queued message, the duplicate arm read
the QUEUED row as runless and launched the build a second time, and the live
run was reported CANCELLED.

These tests drive the real ``dispatch_build`` on a throwaway ledger, with the
runner launch replaced by a recorder:

* a mission build launched through the skip is RUNNING before the runner is
  called, with its decision row citing the mission approval;
* a redelivery for that live build holds the slot: no second launch, no ack;
* a mission build whose first dispatch stopped before the row moved (still
  QUEUED, no run) is re-dispatched once, and then held;
* a terminal write just before the hop to RUNNING holds the slot, no launch;
* a QUEUED mission row with a recorded launch (launched by 2026.10.17-1,
  which left rows QUEUED while running) is held, never relaunched.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import patch

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli import _serve_deps
from forge.cli._serve_deps import build_pipeline_consumer_deps
from forge.config.models import FilesystemPermissions, ForgeConfig, PermissionsConfig
from forge.lifecycle import migrations
from forge.lifecycle.identifiers import derive_build_id
from forge.lifecycle.missions import MissionFeature, MissionStore, attempt_correlation_id
from forge.lifecycle.persistence import SqliteLifecyclePersistence

MISSION_CID = "slack-mission-live-0001"
FEATURE = "FEAT-44B9"
QUEUED_AT = datetime(2026, 10, 17, 21, 20, 21, tzinfo=UTC)


class _Client:
    async def publish(self, subject: str, body: bytes, **_: Any) -> Any:
        return None


class _Starter:
    def start_async_task(self, subagent_name: str, context: dict) -> str:
        return "task-1"

    async def astart_async_task(self, subagent_name: str, context: dict) -> str:
        return "task-1"


@pytest.fixture()
def persistence(tmp_path: Path) -> Iterator[SqliteLifecyclePersistence]:
    cx = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    missions = MissionStore(cx)
    mission, _ = missions.create_mission(
        title="Tags",
        target_repo="guardkit/react-go-notes-fixture",
        correlation_id=MISSION_CID,
        originating_user="U-RICH",
        features=[MissionFeature(key="C", sentence="filter by tag")],
    )
    missions.record_approval(mission.id, approved_by="U-RICH", plan_request_id="req-1")
    missions.file_first_attempts(mission.id)
    missions.set_status(mission.id, "RUNNING", expected=("STARTING",))
    assert missions.admit_attempt(attempt_correlation_id(MISSION_CID, "C", 1))
    try:
        yield SqliteLifecyclePersistence(connection=cx)
    finally:
        cx.close()


def _payload() -> SimpleNamespace:
    return SimpleNamespace(
        feature_id=FEATURE,
        repo="guardkit/react-go-notes-fixture",
        branch="main",
        feature_yaml_path=f".guardkit/features/{FEATURE}.yaml",
        max_turns=5,
        sdk_timeout_seconds=1800,
        triggered_by="forge-internal",
        originating_adapter="cli",
        originating_user="U-RICH",
        correlation_id=attempt_correlation_id(MISSION_CID, "C", 1),
        parent_request_id=None,
        queued_at=QUEUED_AT,
    )


class _Harness:
    def __init__(self, persistence: SqliteLifecyclePersistence, tmp_path: Path) -> None:
        self.persistence = persistence
        self.launches: list[str] = []
        self.status_at_launch: list[str] = []
        self.acks = 0
        config = ForgeConfig(
            permissions=PermissionsConfig(
                filesystem=FilesystemPermissions(allowlist=[tmp_path])
            )
        )
        self.deps = build_pipeline_consumer_deps(
            _Client(), config, persistence, async_task_starter=_Starter()
        )

    def status(self, build_id: str) -> str:
        return str(
            self.persistence.connection.execute(
                "SELECT status FROM builds WHERE build_id = ?", (build_id,)
            ).fetchone()[0]
        )

    def recorded_runs(self, build_id: str) -> int:
        try:
            return int(
                self.persistence.connection.execute(
                    "SELECT COUNT(*) FROM async_tasks WHERE build_id = ?", (build_id,)
                ).fetchone()[0]
            )
        except sqlite3.OperationalError:
            return 0

    async def dispatch(
        self, *, register_observer: Any = None, launch_error: Exception | None = None
    ) -> None:
        self.runs_seen_at_launch: list[int] = []

        async def fake_launch(build_id: str, feature_id: str, correlation_id: str, **_: Any):
            await asyncio.sleep(0)  # the replacement launch is not instant
            self.launches.append(build_id)
            self.status_at_launch.append(self.status(build_id))
            self.runs_seen_at_launch.append(self.recorded_runs(build_id))
            if launch_error is not None:
                raise launch_error
            return SimpleNamespace(task_id="task-1")

        async def ack() -> None:
            self.acks += 1

        async def interrupted(*_a: Any, **_k: Any) -> bool:
            self.interrupts += 1
            return True

        self.interrupts = getattr(self, "interrupts", 0)
        with patch.object(_serve_deps, "dispatch_autobuild_async", fake_launch), patch.object(
            _serve_deps, "interrupt_recorded_run", interrupted
        ):
            await self.deps.dispatch_build(_payload(), ack, register_observer)


BUILD_ID = derive_build_id(FEATURE, QUEUED_AT)


@pytest.mark.asyncio
async def test_a_mission_build_is_running_before_its_runner_starts(
    persistence: SqliteLifecyclePersistence, tmp_path: Path
) -> None:
    h = _Harness(persistence, tmp_path)
    await h.dispatch()
    assert h.launches == [BUILD_ID]
    assert h.status_at_launch == ["RUNNING"]
    row = persistence.connection.execute(
        "SELECT details_json FROM stage_log WHERE build_id = ? "
        "AND target_identifier = 'autobuild_runner'",
        (BUILD_ID,),
    ).fetchone()
    assert json.loads(row[0])["mission_gate"]["plan_request_id"] == "req-1"


@pytest.mark.asyncio
async def test_a_redelivery_for_a_live_mission_build_holds_the_slot(
    persistence: SqliteLifecyclePersistence, tmp_path: Path
) -> None:
    h = _Harness(persistence, tmp_path)
    await h.dispatch()
    acks_after_launch = h.acks
    await h.dispatch()  # JetStream's redelivery an hour later
    assert h.launches == [BUILD_ID]  # no second launch
    assert h.acks == acks_after_launch  # and no ack: the live run owns it
    assert h.status(BUILD_ID) == "RUNNING"


@pytest.mark.asyncio
async def test_a_mission_build_interrupted_before_it_moved_is_dispatched_once(
    persistence: SqliteLifecyclePersistence, tmp_path: Path
) -> None:
    # The first dispatch wrote the row and stopped before it moved: QUEUED, no run.
    _record_queued_row(persistence)
    h = _Harness(persistence, tmp_path)
    assert h.status(BUILD_ID) == "QUEUED"
    await h.dispatch()
    assert h.launches == [BUILD_ID] and h.status_at_launch == ["RUNNING"]
    await h.dispatch()
    assert h.launches == [BUILD_ID]


def _record_queued_row(persistence: SqliteLifecyclePersistence) -> None:
    from nats_core.events import BuildQueuedPayload

    payload = _payload()
    persistence.record_pending_build(
        BuildQueuedPayload(
            feature_id=payload.feature_id,
            repo=payload.repo,
            branch=payload.branch,
            feature_yaml_path=payload.feature_yaml_path,
            triggered_by=payload.triggered_by,
            correlation_id=payload.correlation_id,
            requested_at=QUEUED_AT,
            queued_at=QUEUED_AT,
        )
    )


@pytest.mark.asyncio
async def test_a_failed_advance_holds_the_slot_with_no_launch(
    persistence: SqliteLifecyclePersistence, tmp_path: Path
) -> None:
    """A terminal write lands just before the hop to RUNNING (the build was
    cancelled meanwhile): the other writer owns the row, so nothing launches
    and nothing is acked."""
    from forge.cli import _serve_gate_activation

    real = _serve_gate_activation.advance_to_running

    def cancelled_first(sqlite_pool: Any, build_id: str, *args: Any, **kwargs: Any) -> bool:
        sqlite_pool.connection.execute(
            "UPDATE builds SET status = 'CANCELLED' WHERE build_id = ?", (build_id,)
        )
        answer = real(sqlite_pool, build_id, *args, **kwargs)
        assert answer is False
        return answer

    h = _Harness(persistence, tmp_path)
    with patch.object(_serve_gate_activation, "advance_to_running", cancelled_first):
        await h.dispatch()
    assert h.launches == [] and h.acks == 0
    assert h.status(BUILD_ID) == "CANCELLED"


@pytest.mark.asyncio
async def test_a_queued_mission_row_with_a_recorded_launch_is_held(
    persistence: SqliteLifecyclePersistence, tmp_path: Path
) -> None:
    """A build launched by release 2026.10.17-1 stays QUEUED while it runs and
    has its launch recorded; after a coordinator-only restart its runner may
    still be going, so a redelivery is held: no relaunch, no ack."""
    _record_queued_row(persistence)
    now = QUEUED_AT.isoformat()
    persistence.connection.execute(
        "CREATE TABLE IF NOT EXISTS async_tasks (task_id TEXT PRIMARY KEY, "
        "build_id TEXT NOT NULL, feature_id TEXT NOT NULL, correlation_id TEXT NOT NULL, "
        "lifecycle TEXT NOT NULL, wave_index INTEGER NOT NULL, task_index INTEGER NOT NULL, "
        "started_at TEXT NOT NULL, last_activity_at TEXT NOT NULL) STRICT"
    )
    persistence.connection.execute(
        "INSERT INTO async_tasks VALUES ('task-old', ?, ?, ?, 'running', 1, 1, ?, ?)",
        (BUILD_ID, FEATURE, _payload().correlation_id, now, now),
    )
    h = _Harness(persistence, tmp_path)
    await h.dispatch()
    assert h.launches == [] and h.acks == 0
    assert h.status(BUILD_ID) == "QUEUED"


def _old_run_recorded(persistence: SqliteLifecyclePersistence) -> None:
    """The run the earlier launch recorded, still in ``async_tasks``."""
    now = QUEUED_AT.isoformat()
    persistence.connection.execute(
        "CREATE TABLE IF NOT EXISTS async_tasks (task_id TEXT PRIMARY KEY, "
        "build_id TEXT NOT NULL, feature_id TEXT NOT NULL, correlation_id TEXT NOT NULL, "
        "lifecycle TEXT NOT NULL, wave_index INTEGER NOT NULL, task_index INTEGER NOT NULL, "
        "started_at TEXT NOT NULL, last_activity_at TEXT NOT NULL) STRICT"
    )
    persistence.connection.execute(
        "INSERT INTO async_tasks VALUES ('task-old', ?, ?, ?, 'running', 1, 1, ?, ?)",
        (BUILD_ID, FEATURE, _payload().correlation_id, now, now),
    )


@pytest.mark.asyncio
async def test_a_recovered_mission_build_relaunches_without_its_old_runs_identity(
    persistence: SqliteLifecyclePersistence, tmp_path: Path
) -> None:
    """After a restart (RUNNING -> INTERRUPTED) the old run is interrupted and
    its identity cleared BEFORE the observer registers, so the observer can
    never bind the old run and report the relaunch CANCELLED."""
    _record_queued_row(persistence)
    persistence.connection.execute(
        "UPDATE builds SET status = 'INTERRUPTED' WHERE build_id = ?", (BUILD_ID,)
    )
    _old_run_recorded(persistence)
    h = _Harness(persistence, tmp_path)
    seen_by_observer: list[int] = []

    async def register_observer() -> bool:
        seen_by_observer.append(h.recorded_runs(BUILD_ID))
        return True

    await h.dispatch(register_observer=register_observer)
    assert h.interrupts == 1
    assert seen_by_observer == [0]  # the old run cannot be resolved
    assert h.launches == [BUILD_ID] and h.status_at_launch == ["RUNNING"]
    assert h.runs_seen_at_launch == [0]


@pytest.mark.asyncio
async def test_a_recovered_mission_relaunch_that_fails_puts_the_old_identity_back(
    persistence: SqliteLifecyclePersistence, tmp_path: Path
) -> None:
    _record_queued_row(persistence)
    persistence.connection.execute(
        "UPDATE builds SET status = 'INTERRUPTED' WHERE build_id = ?", (BUILD_ID,)
    )
    _old_run_recorded(persistence)
    h = _Harness(persistence, tmp_path)
    with pytest.raises(RuntimeError, match="runner refused"):
        await h.dispatch(launch_error=RuntimeError("runner refused"))
    assert h.recorded_runs(BUILD_ID) == 1  # never left without its identity


@pytest.mark.asyncio
async def test_a_fresh_mission_build_launches_with_its_observer_as_before(
    persistence: SqliteLifecyclePersistence, tmp_path: Path
) -> None:
    """Not recovered: no interrupt, no replacement, observer then launch."""
    h = _Harness(persistence, tmp_path)
    order: list[str] = []

    async def register_observer() -> bool:
        order.append("observer")
        return True

    await h.dispatch(register_observer=register_observer)
    assert h.interrupts == 0
    assert order == ["observer"] and h.launches == [BUILD_ID]

"""Stopping a mission: the request, and the coordinator carrying it out (18 October 2026).

Missions design, section 11, and its Tests list ("Requesting a stop", the
coordinator's half of "Draining", and "The two routes"). A stop is a durable
request recorded by Slack or the command line through one function; the
mission step, in the queue's tick, holds what was waiting, closes abandoned
admissions, lets running work end, and closes the mission NOT_MERGED once its
work has drained. Everything runs on a throwaway ledger with fakes for the
card, the publisher and the thread.

The landing press's half (the decision inside the repository lock, the
reconciliation before failing, the finisher) is in
``tests/forge/pipeline/test_stop_mission_landing.py``.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from forge.adapters.nats.planning_consumer import handle_planning_message
from forge.cli.main import main as forge_cli
from forge.lifecycle.feature_routing import router_restart_blockers
from forge.lifecycle.missions import STOPPED_BEFORE_IT_STARTED, STOPPED_REASON, MissionStore
from forge.planning import missions_flow
from forge.planning.mission_step import MissionStep, mission_landing
from forge.planning.work_queue_loop import NOT_LANDED_YET, count_in_flight
from tests.forge.planning.test_missions import (  # noqa: F401 - the ledger fixture
    CID,
    MAIN_TIP,
    REPO,
    RICH,
    _cid,
    _FakeFlow,
    _features,
    _flow,
    _intake_deps,
    _message,
    _Publisher,
    _Run,
    _running_mission,
    _settle,
    cx,
)


class _Thread:
    def __init__(self) -> None:
        self.lines: list[str] = []

    async def __call__(self, mission: Any, message: str) -> None:
        self.lines.append(message)


def _stepper(missions: MissionStore, run: _Run, thread: _Thread | None = None) -> MissionStep:
    return MissionStep(
        missions,
        planning_run=run.runs.get_run,
        count_in_flight=lambda: count_in_flight(missions.connection),
        max_in_flight=4,
        start_run=run.start,
        failure_reason=lambda _run, state, _cid: f"the planning run ended {state}",
        clock=lambda: datetime.now(UTC),
        notify=thread,
    )


def _mission(missions: MissionStore) -> Any:
    return missions.get_by_correlation_id(CID)


def _proposed(cx: sqlite3.Connection) -> MissionStore:
    missions = MissionStore(cx)
    missions.create_mission(
        title="Tags for notes",
        target_repo=REPO,
        correlation_id=CID,
        originating_user=RICH,
        features=_features(),
    )
    return missions


def _statuses(missions: MissionStore) -> dict[str, tuple[str, str | None]]:
    return {
        f"{a.key}{a.attempt}": (a.status, a.reason) for a in missions.attempts(_mission(missions).id)
    }


def _merge_build(cx: sqlite3.Connection, mission_id: str) -> None:
    from forge.lifecycle.persistence import SqliteLifecyclePersistence

    SqliteLifecyclePersistence(connection=cx, db_path=Path("/nonexistent/forge.db")).record_mission_merge_build(
        mission_id=mission_id,
        correlation_id=CID,
        feature_id="FEAT-AAA1",
        repo=REPO,
        branch=f"mission/{mission_id}",
        feature_yaml_path=".guardkit/features/FEAT-AAA1.yaml",
        originating_user=RICH,
        start_commit=MAIN_TIP,
        target_branch="main",
        queued_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Requesting a stop: one test for each state a request can be made in
# ---------------------------------------------------------------------------


def test_a_proposed_mission_is_rejected_at_once(cx) -> None:
    missions = _proposed(cx)
    mission = _mission(missions)
    answer = missions.request_stop(mission.id, requested_by=RICH)
    assert answer.accepted and "had not started" in answer.sentence
    got = missions.get(mission.id)
    assert got.status == "REJECTED" and got.stop_requested_by == RICH
    assert got.closed_reason.startswith(f"stopped by {RICH} at ")
    assert missions.attempts(mission.id) == []


@pytest.mark.parametrize("status", ["STARTING", "RUNNING", "READY"])
def test_a_stop_is_recorded_in_starting_running_and_ready(cx, status: str) -> None:
    missions = _running_mission(cx)
    mission = _mission(missions)
    if status == "STARTING":
        cx.execute("UPDATE missions SET status = 'STARTING' WHERE id = ?", (mission.id,))
        cx.execute("DELETE FROM mission_attempts WHERE mission_id = ?", (mission.id,))
    if status == "READY":
        for attempt in missions.attempts(mission.id):
            missions.mark_failed(attempt.correlation_id, "its check failed")
        missions.set_status(mission.id, "READY", expected=("RUNNING",))
    answer = missions.request_stop(mission.id, requested_by=RICH)
    assert answer.accepted
    # A STARTING mission has filed nothing yet: every feature won't start.
    waiting = 0 if status == "READY" else 4
    assert answer.sentence.startswith(f"stop requested; {waiting} waiting feature")
    assert "running work will finish without merging" in answer.sentence
    assert "work already merging into the mission branch will finish" in answer.sentence
    got = missions.get(mission.id)
    assert got.status == status and got.stopping and got.stop_requested_by == RICH
    # Asked again: answered, and the first request is kept.
    again = missions.request_stop(mission.id, requested_by="U-OTHER")
    assert again.accepted and "already asked for by U-RICH" in again.sentence
    assert missions.get(mission.id).stop_requested_by == RICH


def test_the_refusals_in_ready_with_a_merge_build_in_merging_and_once_ended(cx) -> None:
    missions = _running_mission(cx)
    mission = _mission(missions)
    for attempt in missions.attempts(mission.id):
        missions.mark_failed(attempt.correlation_id, "x")
    missions.set_status(mission.id, "READY", expected=("RUNNING",))
    _merge_build(cx, mission.id)
    refused = missions.request_stop(mission.id, requested_by=RICH)
    assert not refused.accepted and "decline the merge card instead" in refused.sentence
    missions.set_status(mission.id, "MERGING", expected=("READY",))
    refused = missions.request_stop(mission.id, requested_by=RICH)
    assert not refused.accepted and "can't be stopped now" in refused.sentence
    missions.set_status(mission.id, "NOT_MERGED", expected=("MERGING",), reason="x")
    refused = missions.request_stop(mission.id, requested_by=RICH)
    assert not refused.accepted and "already ended" in refused.sentence
    assert missions.get(mission.id).stop_requested_at is None


def test_the_stop_columns_are_added_to_an_older_missions_table(tmp_path: Path) -> None:
    from forge.adapters.sqlite import connect as sqlite_connect
    from forge.lifecycle import migrations as lifecycle_migrations
    from forge.lifecycle.missions import ensure_tables

    connection = sqlite_connect.connect_writer(tmp_path / "older.db")
    lifecycle_migrations.apply_at_boot(connection)
    connection.execute(
        "CREATE TABLE missions (id TEXT PRIMARY KEY, title TEXT NOT NULL, target_repo TEXT NOT NULL, "
        "branch TEXT NOT NULL UNIQUE, base_commit TEXT, final_commit TEXT, status TEXT NOT NULL, "
        "correlation_id TEXT NOT NULL UNIQUE, originating_user TEXT NOT NULL, plan_request_id TEXT, "
        "created_at TEXT NOT NULL, approved_at TEXT, approved_by TEXT, closed_at TEXT, "
        "closed_reason TEXT, parent_request_id TEXT, originating_adapter TEXT, triggered_by TEXT) STRICT"
    )
    ensure_tables(connection)
    ensure_tables(connection)
    names = {row[1] for row in connection.execute("PRAGMA table_info(missions)")}
    assert {"stop_requested_at", "stop_requested_by"} <= names
    assert connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 19
    connection.close()


# ---------------------------------------------------------------------------
# Carrying it out: nothing new starts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_waiting_work_is_held_and_nothing_new_is_admitted(cx, tmp_path) -> None:
    missions = _running_mission(cx)
    run = _Run(cx, tmp_path)
    thread = _Thread()
    step = _stepper(missions, run, thread)
    await step.run_once()
    assert run.started == [_cid("A")]
    missions.request_stop(_mission(missions).id, requested_by=RICH)
    assert not missions.admit_attempt(_cid("B"))  # the admission's own check
    await step.run_once()
    statuses = _statuses(missions)
    assert statuses["A1"][0] == "ADMITTED"  # running work is left to end
    assert {statuses[k] for k in ("B1", "C1", "D1")} == {("HELD", STOPPED_REASON)}
    assert run.started == [_cid("A")]
    assert len(thread.lines) == 1 and "is stopping (asked by U-RICH)" in thread.lines[0]
    # The running attempt ends as its own work ends; the mission then drains.
    run.landed(_cid("A"), "FEAT-AAA1")
    await step.run_once()
    mission = _mission(missions)
    assert mission.status == "NOT_MERGED"
    assert mission.closed_reason.startswith(f"stopped by {RICH} at ")
    assert mission.closed_reason.endswith("; 1 landed on the branch, 3 did not")
    assert len(thread.lines) == 2 and "has stopped" in thread.lines[1]
    await step.run_once()
    assert len(thread.lines) == 2  # each line once


@pytest.mark.asyncio
async def test_an_admitted_attempt_with_no_run_closes_and_is_not_put_back(cx, tmp_path) -> None:
    missions = _running_mission(cx)
    run = _Run(cx, tmp_path)
    assert missions.admit_attempt(_cid("A"))  # admitted; the forge stopped before its run
    missions.request_stop(_mission(missions).id, requested_by=RICH)
    assert not missions.requeue_attempt(_cid("A"))  # the put-back's own check
    await _stepper(missions, run).run_once()
    assert _statuses(missions)["A1"] == ("FAILED", STOPPED_BEFORE_IT_STARTED)
    assert run.started == []
    assert _mission(missions).status == "NOT_MERGED"


@pytest.mark.asyncio
async def test_a_conflict_after_a_stop_closes_failed_with_no_second_attempt(cx, tmp_path) -> None:
    missions = _running_mission(cx)
    run = _Run(cx, tmp_path)
    step = _stepper(missions, run)
    await step.run_once()
    missions.request_stop(_mission(missions).id, requested_by=RICH)
    run.conflicted(_cid("A"), "FEAT-AAA1")
    await step.run_once()
    statuses = _statuses(missions)
    assert statuses["A1"] == ("FAILED", "conflict; mission stopped")
    assert "A2" not in statuses
    assert _mission(missions).status == "NOT_MERGED"
    assert _mission(missions).closed_reason.endswith("; 0 landed on the branch, 4 did not")


@pytest.mark.asyncio
async def test_a_late_successful_branch_answer_after_a_stop_files_no_attempts(cx) -> None:
    missions = MissionStore(cx)
    publisher = _Publisher(missions, ["created"])
    flow = _flow(missions, answers=[], gate=None, publisher=publisher, cards=[], said=[])
    mission, _ = missions.create_mission(
        title="Tags for notes",
        target_repo=REPO,
        correlation_id=CID,
        originating_user=RICH,
        features=_features(),
    )
    missions.record_approval(mission.id, approved_by=RICH, plan_request_id="req-1")
    assert missions.get(mission.id).status == "STARTING"
    missions.request_stop(mission.id, requested_by=RICH)
    # The branch request that was already on its way comes back "created".
    assert missions.start_running(mission.id) is None
    await flow.tend()  # a stopped STARTING mission asks for no branch
    await _settle()
    assert publisher.requests == []
    assert missions.attempts(mission.id) == []
    assert missions.get(mission.id).status == "STARTING"
    await _stepper(missions, _Run(cx)).run_once()
    assert missions.get(mission.id).status == "NOT_MERGED"


def test_without_a_stop_the_branch_answer_files_the_attempts_and_runs(cx) -> None:
    missions = MissionStore(cx)
    mission, _ = missions.create_mission(
        title="Tags for notes",
        target_repo=REPO,
        correlation_id=CID,
        originating_user=RICH,
        features=_features(),
    )
    missions.record_approval(mission.id, approved_by=RICH, plan_request_id="req-1")
    assert missions.start_running(mission.id) == 4
    assert missions.start_running(mission.id) is None  # no longer STARTING
    assert missions.get(mission.id).status == "RUNNING"


# ---------------------------------------------------------------------------
# Running work is refused at its next mission check
# ---------------------------------------------------------------------------


def test_the_build_gate_refuses_a_stopped_missions_build_and_ends_it(cx, tmp_path) -> None:
    from forge.cli._serve_deps import MissionBuildRefused, _mission_build_gate_decision

    missions = _running_mission(cx)
    run = _Run(cx, tmp_path)
    missions.admit_attempt(_cid("A"))
    build_id = run.build(_cid("A"), "FEAT-AAA1", "QUEUED")
    missions.request_stop(_mission(missions).id, requested_by=RICH)
    with pytest.raises(MissionBuildRefused, match="was stopped by U-RICH"):
        _mission_build_gate_decision(run.persistence, build_id, _cid("A"), lambda: datetime.now(UTC))
    assert run.persistence.connection.execute(
        "SELECT status FROM builds WHERE build_id = ?", (build_id,)
    ).fetchone()[0] == "FAILED"
    assert _statuses(missions)["A1"][0] == "FAILED"
    assert cx.execute(
        "SELECT COUNT(*) FROM stage_log WHERE build_id = ? AND target_identifier = 'autobuild_runner'",
        (build_id,),
    ).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Draining: an outcome nobody knows is never turned into FAILED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_mission_stays_active_until_drained(cx, tmp_path) -> None:
    missions = _running_mission(cx)
    run = _Run(cx, tmp_path)
    step = _stepper(missions, run)
    await step.run_once()
    mission = _mission(missions)
    missions.request_stop(mission.id, requested_by=RICH)
    run.planning(_cid("A"), "BUILD_QUEUED")
    build_id = run.build(_cid("A"), "FEAT-AAA1", "COMPLETE")
    # A press tried a send and its answer was lost: no result yet.
    line = {"kind": "about to", "step": "send", "attempt": 1, "at": datetime.now(UTC).isoformat(), "detail": {}}
    cx.execute(
        "INSERT INTO publication_records (build_id, j_commit, lines_json) VALUES (?, ?, ?)",
        (build_id, "1" * 40, json.dumps([line])),
    )
    cx.execute(
        "INSERT INTO stage_log (build_id, stage_label, target_kind, target_identifier, status, "
        "started_at, completed_at, duration_secs, details_json) VALUES "
        "(?, 'merge', 'local_tool', 'merge_deploy_executor', 'FAILED', ?, ?, 0, '{}')",
        (build_id, datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()),
    )
    assert mission_landing(cx, _cid("A"))[0] == NOT_LANDED_YET  # waits, not FAILED
    await step.run_once()
    assert _statuses(missions)["A1"][0] == "ADMITTED"
    assert missions.get(mission.id).status == "RUNNING"
    assert mission.id in [m.id for m in missions.list_active()]
    assert f"mission:{mission.id}:RUNNING" in router_restart_blockers(cx)
    # Settled negatively by the press's reconciliation ("publication
    # pending", lease released): the attempt closes and the mission drains.
    cx.execute(
        "UPDATE publication_records SET result = 'publication pending' WHERE build_id = ?",
        (build_id,),
    )
    await step.run_once()
    assert _statuses(missions)["A1"][0] == "FAILED"
    assert missions.get(mission.id).status == "NOT_MERGED"
    assert not any(b.startswith(("mission:", "publication-")) for b in router_restart_blockers(cx))


# ---------------------------------------------------------------------------
# The two routes
# ---------------------------------------------------------------------------


def _stop_message(mission_id: str, *, user: str = RICH):
    msg = _message({"verb": "stop-mission", "mission_id": mission_id})
    if user != RICH:
        decoded = json.loads(msg.data)
        decoded["payload"]["originating_user"] = user
        msg.data = json.dumps(decoded).encode()
    return msg


@pytest.mark.asyncio
async def test_only_the_expected_approver_can_stop_a_mission_by_slack(cx) -> None:
    missions = _running_mission(cx)
    mission = _mission(missions)
    said: list[str] = []
    deps = _intake_deps(cx, _FakeFlow(missions), enabled=True, said=said)
    msg = _stop_message(mission.id, user="U-SOMEONE")
    await handle_planning_message(msg, deps)
    assert msg.acked and said == [f"Only the person who sent mission {mission.id} can stop it."]
    assert missions.get(mission.id).stop_requested_at is None
    msg = _stop_message(mission.id.upper())
    await handle_planning_message(msg, deps)
    assert msg.acked and said[-1].startswith("Stop requested; 4 waiting features won't start")
    assert missions.get(mission.id).stop_requested_by == RICH


@pytest.mark.asyncio
async def test_the_command_line_and_slack_call_the_same_function(cx, tmp_path) -> None:
    missions = _running_mission(cx)
    mission = _mission(missions)
    calls: list[dict[str, Any]] = []
    real = missions_flow.stop_mission

    def spy(store: MissionStore, mission_id: str, **kwargs: Any):
        calls.append({"mission_id": mission_id, **kwargs})
        return real(store, mission_id, **kwargs)

    db = Path(cx.execute("PRAGMA database_list").fetchone()[2])
    with patch.object(missions_flow, "stop_mission", spy):
        result = CliRunner().invoke(
            forge_cli, ["mission", "stop", mission.id, "--db", str(db), "--responder", "operator"]
        )
        assert result.exit_code == 0, result.output
        assert result.output.startswith("stop requested; 4 waiting features")
        said: list[str] = []
        msg = _stop_message(mission.id)
        await handle_planning_message(msg, _intake_deps(cx, _FakeFlow(missions), enabled=True, said=said))
    assert [c["mission_id"] for c in calls] == [mission.id, mission.id]
    assert calls[0]["requested_by"] == "operator" and calls[0].get("expected_approver") is None
    assert calls[1]["requested_by"] == RICH and calls[1]["expected_approver"] == RICH
    assert "already asked for by operator" in said[-1]


def test_the_command_line_lists_missions_and_refuses_in_merging(cx) -> None:
    missions = _running_mission(cx)
    mission = _mission(missions)
    for attempt in missions.attempts(mission.id):
        missions.mark_failed(attempt.correlation_id, "x")
    missions.set_status(mission.id, "READY", expected=("RUNNING",))
    _merge_build(cx, mission.id)
    missions.set_status(mission.id, "MERGING", expected=("READY",))
    db = Path(cx.execute("PRAGMA database_list").fetchone()[2])
    refused = CliRunner().invoke(forge_cli, ["mission", "stop", mission.id, "--db", str(db)])
    assert refused.exit_code == 3 and "can't be stopped now" in refused.output
    listed = CliRunner().invoke(forge_cli, ["mission", "list", "--db", str(db)])
    assert listed.exit_code == 0
    assert listed.output.startswith(f"{mission.id}  MERGING     Tags for notes")


# ---------------------------------------------------------------------------
# Coach round 1
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stopped_missions_run_is_refused_at_its_start_point_before_any_fetch(
    tmp_path: Path,
) -> None:
    from forge.lifecycle.missions import attempt_correlation_id
    from forge.planning.states import PlanningState
    from forge.lifecycle import migrations
    from forge.planning.run_store import SqlitePlanningRunStore
    from tests.forge.planning.test_mission_start_point import (
        MISSION_CID,
        REPO_KEY,
        _driver,
        _mission,
        _run,
        _Runner,
    )

    connection = sqlite3.connect(str(tmp_path / "start.db"))
    connection.row_factory = sqlite3.Row
    migrations.apply_at_boot(connection)
    store = SqlitePlanningRunStore(connection, target_terminal_enabled=True)
    missions = _mission(store)
    mission = missions.get_by_correlation_id(MISSION_CID)
    cid = attempt_correlation_id(MISSION_CID, "A", 1)
    missions.admit_attempt(cid)
    _run(store, cid)
    assert missions.request_stop(mission.id, requested_by="U-RICH").accepted
    runner = _Runner()
    driver = _driver(store, tmp_path, runner)
    assert await driver._start_point(cid, REPO_KEY, "/copies/fixture") is None
    run = store.get_run(cid)
    assert run["state"] == PlanningState.FAILED.value
    assert "was stopped by U-RICH" in run["error"]
    assert runner.asked == []  # never planned from anywhere


def test_an_abandoned_admission_with_a_build_is_left_for_its_build(cx, tmp_path) -> None:
    missions = _running_mission(cx)
    run = _Run(cx, tmp_path)
    assert missions.admit_attempt(_cid("A"))
    run.build(_cid("A"), "FEAT-AAA1", "QUEUED")  # a build, but no planning run
    missions.request_stop(_mission(missions).id, requested_by=RICH)
    assert missions.close_abandoned(_mission(missions).id) == 0
    assert _statuses(missions)["A1"][0] == "ADMITTED"


def test_a_starting_missions_acknowledgement_counts_its_features(cx) -> None:
    missions = _proposed(cx)
    mission = _mission(missions)
    missions.record_approval(mission.id, approved_by=RICH, plan_request_id="req-1")
    answer = missions.request_stop(mission.id, requested_by=RICH)
    assert answer.sentence.startswith("stop requested; 4 waiting features won't start")


class _AnAnswerLater:
    """A plan card's subscriber that waits, as the real one does, for an answer."""

    def __init__(self) -> None:
        self.answer: Any = None
        self.given = asyncio.Event()

    def __call__(self, _approver: Any, armed: Any) -> "_AnAnswerLater":
        self._armed = armed
        return self

    async def await_response(self, _build_id: str, **_kwargs: Any) -> Any:
        if self._armed is not None:
            self._armed.set()
        await self.given.wait()
        return self.answer


@pytest.mark.asyncio
async def test_a_plan_card_answered_after_a_stop_says_so(cx) -> None:
    from forge.planning.missions_flow import MissionFlow
    from tests.forge.planning.test_missions import _answer, _propose, _request_id

    missions = MissionStore(cx)
    said: list[str] = []
    later = _AnAnswerLater()

    async def publish_card(_envelope: Any) -> None:
        return None

    async def notify(_cid: str, message: str, *, parent_request_id: str | None = None) -> None:
        said.append(message)

    async def remote_tip(_repo: str) -> Any:
        raise AssertionError("nothing is started")

    flow = MissionFlow(
        missions,
        publish_card=publish_card,
        subscriber_factory=later,
        notify=notify,
        read_remote_tip=remote_tip,
        create_branch=_Publisher(missions, []),
        approval_wait_seconds=3600,
    )
    mission = _propose(flow)
    await _settle()
    assert missions.request_stop(mission.id, requested_by=RICH).accepted
    later.answer = _answer("approve", _request_id(mission.id))
    later.given.set()
    await _settle()
    assert missions.get(mission.id).status == "REJECTED"
    assert said == [
        'Mission "Tags for notes" was stopped before it started, so this answer '
        "changes nothing. Nothing was built."
    ]

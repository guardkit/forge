"""A mission feature plans from its mission branch, with no spec card (17 October 2026).

Missions design, section 5. Membership is the run's correlation id in
``mission_attempts`` — no ``work_queue`` row is involved — for a first plan and
a rebuild alike. Inconsistent mission metadata fails the run before any model
is asked and is never planned from the default branch. The spec skip records
the mission approval, and the sign-in rule never opens a card inside a mission.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from forge.deploy.candidate_tree import RemoteStartPoint
from forge.lifecycle import migrations
from forge.lifecycle.missions import MissionFeature, MissionStore, attempt_correlation_id
from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.states import PlanningState
from tests.forge.planning.test_start_from_the_remote import REPO_KEY, _make_driver

MISSION_CID = "slack-mission-0002"
MAIN = "1" * 40
MISSION_TIP = "2" * 40


class _Runner:
    """A git runner that answers the start point and records what it was asked."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, str | None]] = []

    async def fetch_remote_start_point(self, repo_path: str, branch: str | None = None) -> Any:
        self.asked.append((repo_path, branch))
        return RemoteStartPoint(
            branch="main",
            commit=MAIN,
            branch_commit=MISSION_TIP if branch else None,
        )


@pytest.fixture
def store(tmp_path: Path) -> SqlitePlanningRunStore:
    connection = sqlite3.connect(str(tmp_path / "mission.db"))
    connection.row_factory = sqlite3.Row
    migrations.apply_at_boot(connection)
    return SqlitePlanningRunStore(connection, target_terminal_enabled=True)


def _mission(store: SqlitePlanningRunStore, *, status: str = "RUNNING") -> MissionStore:
    missions = MissionStore(store._connection)
    mission, _ = missions.create_mission(
        title="Tags",
        target_repo=REPO_KEY,
        correlation_id=MISSION_CID,
        originating_user="U-RICH",
        features=[
            MissionFeature(key="A", sentence="store tags"),
            MissionFeature(key="B", sentence="tag cloud", needs=("A",), no_sign_in=True),
        ],
    )
    missions.record_approval(mission.id, approved_by="U-RICH", plan_request_id="req-9")
    missions.set_status(mission.id, "STARTING", expected=("PROPOSED",))
    missions.file_first_attempts(mission.id)
    if status != "STARTING":
        missions.set_status(mission.id, status, expected=("STARTING",))
    return missions


def _run(store: SqlitePlanningRunStore, correlation_id: str) -> None:
    store.record_queued(
        correlation_id=correlation_id,
        originating_user="U-RICH",
        expected_approver="U-RICH",
        request_text="store tags",
        triggered_by="jarvis",
        target_repo=REPO_KEY,
    )
    store.transition(
        correlation_id=correlation_id,
        to_state=PlanningState.RUNNING,
        actor_identity="test",
        expected_from_state=PlanningState.QUEUED,
    )


def _driver(store: SqlitePlanningRunStore, tmp_path: Path, runner: _Runner) -> Any:
    return _make_driver(
        store, repo_path=tmp_path / "copy", worktrees_root=tmp_path / "wt", git_runner=runner
    ).driver


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt", [1, 2])
async def test_a_mission_run_starts_from_the_mission_branch_tip(
    store: SqlitePlanningRunStore, tmp_path: Path, attempt: int
) -> None:
    missions = _mission(store)
    mission = missions.get_by_correlation_id(MISSION_CID)
    cid = attempt_correlation_id(MISSION_CID, "A", 1)
    missions.admit_attempt(cid)
    if attempt == 2:
        assert missions.fail_with_rebuild(cid)
        cid = attempt_correlation_id(MISSION_CID, "A", 2)
        missions.admit_attempt(cid)
    _run(store, cid)
    runner = _Runner()
    driver = _driver(store, tmp_path, runner)
    start = await driver._start_point(cid, REPO_KEY, "/copies/fixture")
    assert start == (MISSION_TIP, mission.branch)
    assert runner.asked == [("/copies/fixture", mission.branch)]
    assert store.get_start_point(cid) == (MISSION_TIP, mission.branch)
    assert store._connection.execute("SELECT COUNT(*) FROM work_queue").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", ["not running", "not admitted", "no branch"])
async def test_inconsistent_mission_metadata_fails_before_any_model(
    store: SqlitePlanningRunStore, tmp_path: Path, problem: str
) -> None:
    missions = _mission(store, status="STARTING" if problem == "not running" else "RUNNING")
    cid = attempt_correlation_id(MISSION_CID, "A", 1)
    if problem != "not admitted":
        missions.admit_attempt(cid)
    _run(store, cid)
    runner = _Runner()
    if problem == "no branch":

        async def no_branch(repo_path: str, branch: str | None = None) -> Any:
            runner.asked.append((repo_path, branch))
            return RemoteStartPoint(refusal="the remote has no such branch")

        runner.fetch_remote_start_point = no_branch  # type: ignore[method-assign]
    driver = _driver(store, tmp_path, runner)
    assert await driver._start_point(cid, REPO_KEY, "/copies/fixture") is None
    assert store.get_run(cid)["state"] == PlanningState.FAILED.value
    assert store.get_start_point(cid) == (None, None)
    # Never planned from the default branch instead.
    assert all(branch is not None for _path, branch in runner.asked)
    if problem != "no branch":
        assert runner.asked == []


def test_the_spec_skip_and_the_sign_in_rule_are_recorded(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    missions = _mission(store)
    mission = missions.get_by_correlation_id(MISSION_CID)
    driver = _driver(store, tmp_path, _Runner())
    flagged = {"card": {"sign_in_check": True}, "scenario_count": 9, "assumption_count": 3}
    a = attempt_correlation_id(MISSION_CID, "A", 1)
    b = attempt_correlation_id(MISSION_CID, "B", 1)

    reason = driver._digest_review_skip_reason(flagged, a)
    assert reason == (
        f"part of mission {mission.id}, approved by U-RICH at {mission.approved_at}"
    )
    # B's author wrote [no sign-in]: a supplied answer, with who and which card.
    said = driver._mission_spec_review(b, flagged)
    assert said == {
        "decided_by": "U-RICH",
        "request_id": "req-9",
        "source": "mission message",
        "mission_id": mission.id,
        "sign_in_answer": "confirmed",
        "auth_confirmed": True,
    }
    # A's did not: recorded as not said, which the pass-bars leg stops on.
    not_said = driver._mission_spec_review(a, flagged)
    assert not_said["sign_in_answer"] == "not said in the mission"
    assert driver._mission_sign_in_answer(a)["outcome"] == "not said in the mission"
    assert driver._mission_sign_in_answer(b)["outcome"] == "confirmed"
    # Not a mission run: everything as before.
    assert driver._mission_spec_review("plain-run", flagged) is None
    assert driver._mission_sign_in_answer("plain-run") is None
    assert driver._digest_review_skip_reason(flagged, "plain-run") is None


def test_the_not_said_answer_has_its_own_honest_terminal_words() -> None:
    from forge.planning.driver import _AUTH_DOOR_TERMINAL_SUFFIX

    assert "the mission didn't say it has none" in _AUTH_DOOR_TERMINAL_SUFFIX[
        "not said in the mission"
    ]

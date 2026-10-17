"""The sign-in rule where it applies: the pass-bars leg (missions design, section 5).

A mission feature's seed flagged as possibly behind a sign-in never opens a
card. The author's ``[no sign-in]`` lets the run go on; without it the run
stops with the existing honest terminal, in words that say the mission did not
say. Driven through the real planning driver with the spec digest door's own
harness (fakes at the wire seams, a throwaway ledger), for a whole run and for
the pass-bars leg alone when no sign-in answer was recorded on the spec.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from forge.deploy.candidate_tree import RemoteStartPoint
from forge.lifecycle.missions import MissionFeature, MissionStore
from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.states import PlanningState
from tests.forge.planning.test_driver_spec_digest_door import (
    _AUTH_DOOR_STAGE,
    _AUTH_SEED,
    _BARS_STAGE,
    _SPEC_STAGE,
    CID,
    TARGET_REPO,
    RecordingGitRunner,
    SharedScriptFactory,
    _events,
    _make_driver,
    _queue,
    _spec_reply,
    store,  # noqa: F401 — the fixture
)

MISSION_TIP = "3" * 40


class _MissionGit(RecordingGitRunner):
    """The digest door's git double, answering a named branch too."""

    async def fetch_remote_start_point(self, repo_path: str, branch: str | None = None) -> Any:
        return RemoteStartPoint(
            branch="main", commit="0" * 39 + "1", branch_commit=MISSION_TIP if branch else None
        )


def _member(store: SqlitePlanningRunStore, *, no_sign_in: bool) -> str:
    """Make the run under ``CID`` a RUNNING mission's ADMITTED attempt."""
    missions = MissionStore(store._connection)
    mission, _ = missions.create_mission(
        title="Tags",
        target_repo=TARGET_REPO,
        correlation_id="slack-mission-0003",
        originating_user="U0RIGINATOR",
        features=[MissionFeature(key="A", sentence="version", no_sign_in=no_sign_in)],
    )
    missions.record_approval(mission.id, approved_by="U0RIGINATOR", plan_request_id="req-7")
    missions.set_status(mission.id, "RUNNING", expected=("STARTING",))
    store._connection.execute(
        "INSERT INTO mission_attempts (mission_id, key, attempt, correlation_id, status) "
        "VALUES (?, 'A', 1, ?, 'ADMITTED')",
        (mission.id, CID),
    )
    return mission.id


@pytest.mark.asyncio
@pytest.mark.parametrize("no_sign_in", [True, False])
async def test_a_whole_mission_run_never_opens_a_card(
    store: SqlitePlanningRunStore,  # noqa: F811
    no_sign_in: bool,
) -> None:
    _queue(store)
    _member(store, no_sign_in=no_sign_in)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([]),
        spec_replies=[_spec_reply(seed=_AUTH_SEED)],
        git=_MissionGit(),
    )
    await h.driver.drive(CID)

    run = store.get_run(CID)
    assert h.ctx["publisher"].envelopes == []  # no spec card, no sign-in card
    assert [s for s, _d in _events(store, _AUTH_DOOR_STAGE)] == []
    assert store.get_start_point(CID)[0] == MISSION_TIP
    spec = [d for s, d in _events(store, _SPEC_STAGE) if s == "approved"][-1]
    assert spec["spec_review"]["source"] == "mission message"
    assert spec["spec_review"]["request_id"] == "req-7"
    if no_sign_in:
        assert run["state"] == PlanningState.BUILD_QUEUED.value
        bars = [d for s, d in _events(store, _BARS_STAGE) if s == "approved"][-1]
        assert bars["auth_confirmation"]["outcome"] == "confirmed"
        assert bars["auth_confirmation"]["answered_on"] == "the mission message"
    else:
        assert run["state"] == PlanningState.FAILED.value
        assert "the mission didn't say it has none" in run["error"]
        assert h.ctx["build_triggers"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("no_sign_in", [True, False])
async def test_the_pass_bars_leg_reads_the_mission_when_the_spec_recorded_no_answer(
    store: SqlitePlanningRunStore,  # noqa: F811
    no_sign_in: bool,
) -> None:
    """A spec approved with no sign-in answer on it (the flag was down when it
    was approved) and a seed flagged at the pass bars: the mission message
    answers, and no door is opened."""
    _queue(store)
    _member(store, no_sign_in=no_sign_in)
    store.transition(
        correlation_id=CID,
        to_state=PlanningState.RUNNING,
        actor_identity="test",
        expected_from_state=PlanningState.QUEUED,
    )
    store._record_event(
        correlation_id=CID,
        stage_label=_SPEC_STAGE,
        status="approved",
        actor_identity="test",
        details_json=json.dumps({"slug": "version-endpoint", "pass_bar_seed": _AUTH_SEED}),
    )
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([]),
        git=_MissionGit(),
        originator_wait_seconds=1,
    )
    await h.driver._register_pass_bars_leg(store.get_run(CID), CID)

    run = store.get_run(CID)
    assert h.ctx["publisher"].envelopes == []
    assert [s for s, _d in _events(store, _AUTH_DOOR_STAGE)] == []
    assert run["state"] == PlanningState.FAILED.value
    if no_sign_in:
        # Past the sign-in check: it stops later, for want of a plan here.
        assert "no feature id / plan commit sha" in run["error"]
    else:
        assert "the mission didn't say it has none" in run["error"]

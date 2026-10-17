"""Missions: starting one, running its features in order, and landing them (17 October 2026).

Missions design, sections 1–5 and 8, the tests of section 10 that belong to
the start and the run. Everything runs on a throwaway ledger made by Forge's
own migration code, with fakes for the card, the subscriber, the routing gate,
the remote and the publisher. No real repository, Slack or publisher is
contacted.

Shape used throughout: A, then B and C together (both need A), then D, which
needs B and C.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from nats_core.envelope import EventType, MessageEnvelope
from nats_core.events import BuildQueuedPayload

from forge.adapters.nats.planning_consumer import (
    PlanningConsumerDeps,
    handle_planning_message,
)
from forge.adapters.sqlite import connect as sqlite_connect
from forge.config.models import ForgeConfig
from forge.lifecycle import migrations as lifecycle_migrations
from forge.lifecycle.feature_routing import router_restart_blockers
from forge.lifecycle.missions import (
    MissionFeature,
    MissionStore,
    attempt_correlation_id,
)
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.pipeline.publication_record import RESULT_PUBLISHED_TO_MISSION_BRANCH
from forge.planning.mission_step import MissionStep, mission_queue_lines
from forge.planning.missions_flow import (
    MISSION_PLAN_STAGE_LABEL,
    ROUTING_NOT_SET_UP,
    MissionFlow,
    plan_card_rationale,
    validate_mission,
)
from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.work_queue_loop import (
    MERGE_CARD_GRACE_SECONDS,
    NOT_LANDED_YET,
    count_in_flight,
    work_landing,
)

CID = "slack-mission-0001"
REPO = "guardkit/react-go-notes-fixture"
RICH = "U-RICH"
MAIN_TIP = "a" * 40


# ---------------------------------------------------------------------------
# The ledger and its helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def cx(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
    lifecycle_migrations.apply_at_boot(connection)
    MissionStore(connection)  # the boot step
    try:
        yield connection
    finally:
        connection.close()


def _command(**overrides: Any) -> dict[str, Any]:
    command = {
        "verb": "mission",
        "title": "Tags for notes",
        "features": [
            {"key": "a", "needs": [], "sentence": "Store tags on notes"},
            {"key": "B", "needs": ["A"], "sentence": "Add and remove tags"},
            {"key": "C", "needs": ["a"], "sentence": "Filter by tag"},
            {
                "key": "D",
                "needs": ["B", "C"],
                "no_sign_in": True,
                "sentence": "Show a tag cloud",
            },
        ],
    }
    command.update(overrides)
    return command


def _features() -> list[MissionFeature]:
    checked = validate_mission(_command(), mission_correlation_id=CID)
    assert not isinstance(checked, str)
    return checked[1]


def _running_mission(cx: sqlite3.Connection) -> MissionStore:
    missions = MissionStore(cx)
    mission, _ = missions.create_mission(
        title="Tags for notes",
        target_repo=REPO,
        correlation_id=CID,
        originating_user=RICH,
        features=_features(),
    )
    missions.record_approval(mission.id, approved_by=RICH, plan_request_id="req-1")
    missions.set_status(mission.id, "STARTING", expected=("PROPOSED",))
    missions.set_base_commit(mission.id, MAIN_TIP)
    missions.file_first_attempts(mission.id)
    missions.set_status(mission.id, "RUNNING", expected=("STARTING",))
    return missions


def _cid(key: str, attempt: int = 1) -> str:
    return attempt_correlation_id(CID, key, attempt)


class _Run:
    """The planning runs and builds a mission's attempts make, written directly."""

    def __init__(self, cx: sqlite3.Connection, tmp_path: Path | None = None) -> None:
        self.cx = cx
        self.runs = SqlitePlanningRunStore(cx, target_terminal_enabled=True)
        self.persistence = SqliteLifecyclePersistence(
            connection=cx, db_path=(tmp_path or Path("/nonexistent")) / "forge.db"
        )
        self.started: list[str] = []

    async def start(self, admission: Any) -> None:
        self.started.append(admission.correlation_id)
        self.runs.record_queued(
            correlation_id=admission.correlation_id,
            originating_user=admission.originating_user,
            expected_approver=admission.originating_user,
            request_text=admission.request_text,
            triggered_by=admission.triggered_by,
            target_repo=admission.target_repo,
        )

    def planning(self, correlation_id: str, state: str) -> None:
        self.cx.execute(
            "UPDATE planning_runs SET state = ? WHERE correlation_id = ?",
            (state, correlation_id),
        )

    def build(self, correlation_id: str, feature_id: str, status: str) -> str:
        now = datetime.now(UTC)
        build_id = self.persistence.record_pending_build(
            BuildQueuedPayload(
                feature_id=feature_id,
                repo=REPO,
                feature_yaml_path=f".guardkit/features/{feature_id}.yaml",
                triggered_by="forge-internal",
                correlation_id=correlation_id,
                requested_at=now,
                queued_at=now,
            )
        )
        self.cx.execute(
            "UPDATE builds SET status = ?, completed_at = ? WHERE build_id = ?",
            (status, now.isoformat(), build_id),
        )
        return build_id

    def landed(self, correlation_id: str, feature_id: str) -> str:
        """Planned, built, and published to the mission branch."""
        self.planning(correlation_id, "BUILD_QUEUED")
        build_id = self.build(correlation_id, feature_id, "COMPLETE")
        self.cx.execute(
            "INSERT INTO publication_records (build_id, result) VALUES (?, ?)",
            (build_id, RESULT_PUBLISHED_TO_MISSION_BRANCH),
        )
        return build_id

    def conflicted(self, correlation_id: str, feature_id: str) -> str:
        """Built, and its merge into the mission branch stopped on a conflict."""
        self.planning(correlation_id, "BUILD_QUEUED")
        build_id = self.build(correlation_id, feature_id, "COMPLETE")
        line = {
            "kind": "done",
            "step": "join",
            "attempt": 1,
            "at": datetime.now(UTC).isoformat(),
            "detail": {
                "joined": False,
                "conflict": True,
                "refusal": "the merge stopped on a conflict in notes.go",
            },
        }
        self.cx.execute(
            "INSERT INTO publication_records (build_id, result, lines_json) "
            "VALUES (?, 'publication pending', ?)",
            (build_id, json.dumps([line])),
        )
        self.cx.execute(
            "INSERT INTO stage_log (build_id, stage_label, target_kind, "
            "target_identifier, status, started_at, completed_at, duration_secs, "
            "details_json) VALUES (?, 'merge', 'local_tool', ?, 'FAILED', ?, ?, 0, '{}')",
            (
                build_id,
                "merge_deploy_executor",
                datetime.now(UTC).isoformat(),
                datetime.now(UTC).isoformat(),
            ),
        )
        return build_id


def _step(missions: MissionStore, run: _Run, *, limit: int = 4) -> MissionStep:
    return MissionStep(
        missions,
        planning_run=run.runs.get_run,
        count_in_flight=lambda: count_in_flight(missions.connection),
        max_in_flight=limit,
        start_run=run.start,
        failure_reason=lambda _run, state, _cid: f"the planning run ended {state}",
        clock=lambda: datetime.now(UTC),
    )


def _statuses(missions: MissionStore, mission_id: str) -> dict[str, str]:
    return {
        f"{a.key}{a.attempt}": a.status for a in missions.attempts(mission_id)
    }


# ---------------------------------------------------------------------------
# Section 1: checking the message
# ---------------------------------------------------------------------------


def test_a_good_mission_upper_cases_keys_and_needs() -> None:
    checked = validate_mission(_command(), mission_correlation_id=CID)
    assert not isinstance(checked, str)
    title, features = checked
    assert title == "Tags for notes"
    assert [f.key for f in features] == ["A", "B", "C", "D"]
    assert features[2].needs == ("A",) and features[3].no_sign_in


@pytest.mark.parametrize(
    ("features", "words"),
    [
        (
            [
                {"key": "A", "needs": ["B"], "sentence": "x"},
                {"key": "B", "needs": ["A"], "sentence": "y"},
            ],
            "circle",
        ),
        ([{"key": "A", "needs": ["Z"], "sentence": "x"}], "not in this mission"),
        ([{"key": f"K{i}", "sentence": "x"} for i in range(21)], "between 1 and 20"),
        ([], "between 1 and 20"),
        ([{"key": "A", "sentence": "x"}, {"key": "a", "sentence": "y"}], "used twice"),
        ([{"key": "A", "sentence": "   "}], "no sentence"),
        ([{"key": "A-1", "sentence": "x"}], "not a usable key"),
        ([{"key": "A", "needs": ["A"], "sentence": "x"}], "circle"),
    ],
)
def test_a_bad_mission_gets_one_sentence(features: list[Any], words: str) -> None:
    refusal = validate_mission(_command(features=features), mission_correlation_id=CID)
    assert isinstance(refusal, str) and words in refusal


def test_a_mission_whose_attempt_id_would_be_too_long_is_refused() -> None:
    long_cid = "c" * 120
    refusal = validate_mission(
        _command(features=[{"key": "ABCDEFGHIJKL", "sentence": "x"}]),
        mission_correlation_id=long_cid,
    )
    assert isinstance(refusal, str) and "too long" in refusal
    fine = validate_mission(
        _command(features=[{"key": "A", "sentence": "x"}]), mission_correlation_id=long_cid
    )
    assert not isinstance(fine, str)


def test_the_plan_card_lists_the_waves_and_the_no_sign_in_note(cx) -> None:
    missions = _running_mission(cx)
    mission = missions.get_by_correlation_id(CID)
    text = plan_card_rationale(mission, missions.features(mission.id))
    assert text.startswith('Mission "Tags for notes" — 4 features on react-go-notes-fixture')
    lines = text.splitlines()
    assert lines[1].startswith("First:") and "A  Store tags on notes" in lines[1]
    assert lines[2].startswith("Then, together:") and "B  " in lines[2] and "C  " in lines[2]
    assert lines[3].startswith("Then:") and "(you said: no sign-in)" in lines[3]
    assert mission.branch in text and "nothing reaches main" in text


# ---------------------------------------------------------------------------
# Section 1: the intake
# ---------------------------------------------------------------------------


@dataclass
class _Msg:
    data: bytes
    acked: bool = False

    async def ack(self) -> None:
        self.acked = True


def _message(command: dict[str, Any], *, target_repo: str | None = "react-go-notes-fixture") -> _Msg:
    now = datetime.now(UTC).isoformat()
    payload = {
        "stage": "planning",
        "request_text": "mission: Tags for notes",
        "target_repo": target_repo,
        "triggered_by": "jarvis",
        "originating_adapter": "slack",
        "originating_user": RICH,
        "correlation_id": CID,
        "parent_request_id": "thread-1",
        "retry_count": 0,
        "requested_at": now,
        "queued_at": now,
        "queue_command": command,
    }
    envelope = MessageEnvelope(
        source_id="jarvis",
        event_type=EventType.BUILD_QUEUED,
        correlation_id=CID,
        payload=payload,
    )
    return _Msg(data=envelope.model_dump_json().encode())


class _FakeFlow:
    def __init__(self, missions: MissionStore, *, can_create_branches: bool = True) -> None:
        self.missions = missions
        self.can_create_branches = can_create_branches
        self.proposed: list[dict[str, Any]] = []

    def propose(self, **kwargs: Any) -> Any:
        self.proposed.append(kwargs)
        return self.missions.create_mission(
            title=kwargs["title"],
            target_repo=kwargs["target_repo"],
            correlation_id=kwargs["correlation_id"],
            originating_user=kwargs["originating_user"],
            features=kwargs["features"],
            parent_request_id=kwargs["parent_request_id"],
        )


def _intake_deps(
    cx, flow: Any, *, enabled: bool, said: list[str], target_terminal: bool = True
) -> PlanningConsumerDeps:
    async def notify(cid: str, message: str, *, parent_request_id: str | None = None) -> None:
        said.append(message)

    return PlanningConsumerDeps(
        store=SqlitePlanningRunStore(cx),
        publish_notification=notify,
        planning_config=SimpleNamespace(
            target_repo_paths={REPO: "/nonexistent/react-go-notes-fixture"},
            default_target_repo=None,
            target_terminal=SimpleNamespace(enabled=target_terminal),
        ),
        mission_flow=flow,
        missions_enabled=enabled,
    )


@pytest.mark.asyncio
async def test_the_intake_files_a_mission_and_nothing_in_the_queue(cx) -> None:
    missions = MissionStore(cx)
    flow = _FakeFlow(missions)
    said: list[str] = []
    msg = _message(_command())
    await handle_planning_message(msg, _intake_deps(cx, flow, enabled=True, said=said))
    assert msg.acked and said == []
    mission = missions.get_by_correlation_id(CID)
    assert mission.status == "PROPOSED" and mission.target_repo == REPO
    assert mission.parent_request_id == "thread-1"
    assert cx.execute("SELECT COUNT(*) FROM work_queue").fetchone()[0] == 0
    assert cx.execute("SELECT COUNT(*) FROM planning_runs").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_the_intake_answers_switched_off_and_refusals_in_one_sentence(cx) -> None:
    missions = MissionStore(cx)
    said: list[str] = []
    msg = _message(_command())
    await handle_planning_message(
        msg, _intake_deps(cx, _FakeFlow(missions), enabled=False, said=said)
    )
    assert msg.acked and said == ["That mission was not started: missions are switched off."]
    msg = _message(_command(features=[{"key": "A", "needs": ["Q"], "sentence": "x"}]))
    await handle_planning_message(
        msg, _intake_deps(cx, _FakeFlow(missions), enabled=True, said=said)
    )
    assert msg.acked and "not in this mission" in said[-1]
    msg = _message(_command(), target_repo="no-such-project")
    await handle_planning_message(
        msg, _intake_deps(cx, _FakeFlow(missions), enabled=True, said=said)
    )
    assert msg.acked and len(said) == 3
    assert cx.execute("SELECT COUNT(*) FROM missions").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_the_intake_refuses_when_missions_cannot_run_here(cx) -> None:
    """No publisher client to create the branch, or no planning chain to read
    it: one plain sentence, nothing filed."""
    missions = MissionStore(cx)
    said: list[str] = []
    msg = _message(_command())
    flow = _FakeFlow(missions, can_create_branches=False)
    await handle_planning_message(msg, _intake_deps(cx, flow, enabled=True, said=said))
    assert msg.acked and said[-1] == (
        "That mission was not started: this factory cannot create a mission branch yet."
    )
    msg = _message(_command())
    await handle_planning_message(
        msg,
        _intake_deps(cx, _FakeFlow(missions), enabled=True, said=said, target_terminal=False),
    )
    assert msg.acked and "planning chain" in said[-1] and "switched off" in said[-1]
    assert cx.execute("SELECT COUNT(*) FROM missions").fetchone()[0] == 0
    assert not flow.proposed


def test_the_switch_is_off_by_default() -> None:
    config = ForgeConfig.model_validate({"permissions": {"filesystem": {"allowlist": ["/tmp"]}}})
    assert config.missions.enabled is False


# ---------------------------------------------------------------------------
# Sections 3 and 4: the plan card, its answer, and the branch
# ---------------------------------------------------------------------------


class _Subscriber:
    def __init__(self, answers: list[Any], armed: asyncio.Event | None) -> None:
        self._answers = answers
        self._armed = armed

    async def await_response(self, build_id: str, **kwargs: Any) -> Any:
        if self._armed is not None:
            self._armed.set()
        await asyncio.sleep(0)
        return self._answers.pop(0) if self._answers else None


class _Gate:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, str, str]] = []

    async def ensure_seeded(self, key: str, *, origin_kind: str, origin_id: str) -> Any:
        self.calls.append((key, origin_kind, origin_id))
        if self.fail:
            raise RuntimeError("feature routing seed outcome is unknown")
        return object()


class _Publisher:
    """A stand-in with the publisher client's signature and answer shape."""

    def __init__(self, missions: MissionStore, outcomes: list[str]) -> None:
        self.missions = missions
        self.outcomes = outcomes
        self.requests: list[dict[str, Any]] = []
        self.base_when_asked: list[str | None] = []

    async def __call__(self, *, project: str, mission_id: str, branch: str, commit: str) -> Any:
        self.requests.append(
            {"project": project, "mission_id": mission_id, "branch": branch, "commit": commit}
        )
        self.base_when_asked.append(self.missions.get(mission_id).base_commit)
        outcome = self.outcomes.pop(0) if self.outcomes else "unreachable"
        return SimpleNamespace(outcome=outcome, refusal=f"the publisher said {outcome}")


def _flow(
    missions: MissionStore,
    *,
    answers: list[Any],
    gate: _Gate | None,
    publisher: _Publisher,
    cards: list[Any],
    said: list[str],
    clock: Any = None,
) -> MissionFlow:
    async def publish_card(envelope: Any) -> None:
        cards.append(envelope)

    async def notify(cid: str, message: str, *, parent_request_id: str | None = None) -> None:
        said.append(message)

    async def remote_tip(repo: str) -> Any:
        return SimpleNamespace(ok=True, commit=MAIN_TIP, branch="main", refusal=None)

    return MissionFlow(
        missions,
        publish_card=publish_card,
        subscriber_factory=lambda approver, armed: _Subscriber(answers, armed),
        notify=notify,
        read_remote_tip=remote_tip,
        create_branch=publisher,
        approval_wait_seconds=3600,
        routing_gate=gate,
        clock=clock,
        spawn=asyncio.ensure_future,
    )


def _answer(decision: str, request_id: str) -> Any:
    return SimpleNamespace(decision=decision, decided_by=RICH, request_id=request_id)


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


def _request_id(mission_id: str) -> str:
    from forge.gating.identity import derive_request_id

    return derive_request_id(
        build_id=f"mission-{mission_id}", stage_label=MISSION_PLAN_STAGE_LABEL, attempt_count=0
    )


def _propose(flow: MissionFlow) -> Any:
    mission, created = flow.propose(
        title="Tags for notes",
        target_repo=REPO,
        correlation_id=CID,
        originating_user=RICH,
        features=_features(),
        parent_request_id="thread-1",
    )
    assert created
    return mission


@pytest.mark.asyncio
async def test_approve_seeds_routing_once_stores_the_base_then_starts(cx) -> None:
    missions = MissionStore(cx)
    gate, cards, said = _Gate(), [], []
    publisher = _Publisher(missions, ["created"])
    answers: list[Any] = []
    flow = _flow(missions, answers=answers, gate=gate, publisher=publisher, cards=cards, said=said)
    mission = _propose(flow)
    answers.append(_answer("approve", _request_id(mission.id)))
    await _settle()
    card = cards[0].payload
    assert card["details"]["build_id"] == f"mission-{mission.id}"
    assert card["details"]["stage_label"] == "the mission plan"
    assert card["details"]["feature_title"] == "Tags for notes"
    assert not card["details"]["build_id"].startswith("plan-")
    got = missions.get(mission.id)
    assert got.status == "RUNNING"
    assert got.approved_by == RICH and got.plan_request_id == _request_id(mission.id)
    assert gate.calls == [(CID, "mission", mission.id)]
    assert publisher.requests == [
        {"project": REPO, "mission_id": mission.id, "branch": mission.branch, "commit": MAIN_TIP}
    ]
    assert publisher.base_when_asked == [MAIN_TIP]  # written down before the request
    assert set(_statuses(missions, mission.id).values()) == {"WAITING"}
    assert "has started on" in said[-1]


@pytest.mark.asyncio
async def test_reject_files_nothing(cx) -> None:
    missions = MissionStore(cx)
    cards, said, answers = [], [], []
    publisher = _Publisher(missions, [])
    flow = _flow(missions, answers=answers, gate=None, publisher=publisher, cards=cards, said=said)
    mission = _propose(flow)
    answers.append(_answer("reject", _request_id(mission.id)))
    await _settle()
    assert missions.get(mission.id).status == "REJECTED"
    assert missions.attempts(mission.id) == [] and publisher.requests == []


@pytest.mark.asyncio
async def test_a_router_outage_at_approval_is_not_started_and_never_retried(cx) -> None:
    missions = MissionStore(cx)
    gate, cards, said, answers = _Gate(fail=True), [], [], []
    publisher = _Publisher(missions, ["created"])
    flow = _flow(missions, answers=answers, gate=gate, publisher=publisher, cards=cards, said=said)
    mission = _propose(flow)
    answers.append(_answer("approve", _request_id(mission.id)))
    await _settle()
    got = missions.get(mission.id)
    assert got.status == "NOT_STARTED" and got.closed_reason == ROUTING_NOT_SET_UP
    await flow.tend()
    await _settle()
    assert len(gate.calls) == 1 and publisher.requests == []
    assert missions.attempts(mission.id) == []
    # A new mission once the router is back starts as normal.
    gate.fail = False
    second, _ = flow.propose(
        title="Tags again",
        target_repo=REPO,
        correlation_id=CID + "-again",
        originating_user=RICH,
        features=_features(),
    )
    answers.append(_answer("approve", _request_id(second.id)))
    await _settle()
    assert missions.get(second.id).status == "RUNNING"


@pytest.mark.asyncio
async def test_an_unreachable_publisher_is_asked_again_a_minute_later_and_after_a_restart(
    cx,
) -> None:
    missions = MissionStore(cx)
    now = [datetime.now(UTC)]
    cards, said, answers = [], [], []
    publisher = _Publisher(missions, ["unreachable", "unreachable", "created"])
    flow = _flow(
        missions,
        answers=answers,
        gate=None,
        publisher=publisher,
        cards=cards,
        said=said,
        clock=lambda: now[0],
    )
    mission = _propose(flow)
    answers.append(_answer("approve", _request_id(mission.id)))
    await _settle()
    assert missions.get(mission.id).status == "STARTING" and len(publisher.requests) == 1
    await flow.tend()
    await _settle()
    assert len(publisher.requests) == 1  # not yet a minute
    now[0] += timedelta(seconds=61)
    await flow.tend()
    await _settle()
    assert len(publisher.requests) == 2 and missions.get(mission.id).status == "STARTING"
    # A restart: a new flow, and the same request with the same commit is sent at once.
    restarted = _flow(
        missions, answers=[], gate=None, publisher=publisher, cards=cards, said=said
    )
    await restarted.tend()
    await _settle()
    assert [r["commit"] for r in publisher.requests] == [MAIN_TIP] * 3
    assert missions.get(mission.id).status == "RUNNING"
    assert len(missions.attempts(mission.id)) == 4


@pytest.mark.asyncio
async def test_a_refused_branch_is_not_started(cx) -> None:
    missions = MissionStore(cx)
    cards, said, answers = [], [], []
    publisher = _Publisher(missions, ["refused"])
    flow = _flow(missions, answers=answers, gate=None, publisher=publisher, cards=cards, said=said)
    mission = _propose(flow)
    answers.append(_answer("approve", _request_id(mission.id)))
    await _settle()
    got = missions.get(mission.id)
    assert got.status == "NOT_STARTED" and "the publisher said refused" in got.closed_reason
    assert missions.attempts(mission.id) == []


@pytest.mark.asyncio
async def test_a_proposed_mission_gets_its_same_card_again_after_a_restart(cx) -> None:
    missions = MissionStore(cx)
    cards, said = [], []
    publisher = _Publisher(missions, ["created"])
    first = _flow(missions, answers=[], gate=None, publisher=publisher, cards=cards, said=said)
    mission = _propose(first)
    await _settle()
    answers = [_answer("approve", _request_id(mission.id))]
    restarted = _flow(
        missions, answers=answers, gate=None, publisher=publisher, cards=cards, said=said
    )
    await restarted.tend()
    await _settle()
    assert cards[0].payload == cards[-1].payload
    assert missions.get(mission.id).status == "RUNNING"


# ---------------------------------------------------------------------------
# Section 2a: the mission step
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b_and_c_wait_for_a_then_run_together_and_d_needs_both(cx, tmp_path) -> None:
    missions = _running_mission(cx)
    mission = missions.get_by_correlation_id(CID)
    run = _Run(cx, tmp_path)
    step = _step(missions, run)
    await step.run_once()
    assert run.started == [_cid("A")]
    await step.run_once()
    assert run.started == [_cid("A")]  # B and C wait for A to land
    run.landed(_cid("A"), "FEAT-AAA1")
    await step.run_once()
    assert run.started == [_cid("A"), _cid("B"), _cid("C")]
    assert _statuses(missions, mission.id)["D1"] == "WAITING"
    run.landed(_cid("B"), "FEAT-BBB1")
    await step.run_once()
    assert _cid("D") not in run.started  # needs C too
    run.landed(_cid("C"), "FEAT-CCC1")
    await step.run_once()
    assert run.started[-1] == _cid("D")
    run.landed(_cid("D"), "FEAT-DDD1")
    await step.run_once()
    assert set(_statuses(missions, mission.id).values()) == {"LANDED"}
    assert missions.get(mission.id).status == "READY"
    assert mission_queue_lines(cx) == []


@pytest.mark.asyncio
async def test_a_conflict_then_a_rebuild_that_lands_lets_the_dependants_go(cx, tmp_path) -> None:
    missions = _running_mission(cx)
    mission = missions.get_by_correlation_id(CID)
    run = _Run(cx, tmp_path)
    step = _step(missions, run)
    await step.run_once()
    run.conflicted(_cid("A"), "FEAT-AAA1")
    await step.run_once()
    statuses = _statuses(missions, mission.id)
    assert statuses["A1"] == "FAILED" and statuses["B1"] == "WAITING"
    assert run.started[-1] == _cid("A", 2)  # the rebuild, under its own id
    await step.run_once()  # a replay files no third attempt
    assert [a.attempt for a in missions.attempts(mission.id) if a.key == "A"] == [1, 2]
    run.landed(_cid("A", 2), "FEAT-AAA2")
    await step.run_once()
    assert {_cid("B"), _cid("C")} <= set(run.started)


@pytest.mark.asyncio
async def test_a_conflict_then_a_failed_rebuild_holds_the_dependants(cx, tmp_path) -> None:
    missions = _running_mission(cx)
    mission = missions.get_by_correlation_id(CID)
    run = _Run(cx, tmp_path)
    step = _step(missions, run)
    await step.run_once()
    run.conflicted(_cid("A"), "FEAT-AAA1")
    await step.run_once()
    run.planning(_cid("A", 2), "BUILD_QUEUED")
    run.build(_cid("A", 2), "FEAT-AAA2", "FAILED")
    await step.run_once()
    statuses = _statuses(missions, mission.id)
    assert statuses["A2"] == "FAILED"
    assert statuses["B1"] == statuses["C1"] == statuses["D1"] == "HELD"
    assert missions.attempt_by_correlation_id(_cid("B")).reason == "A did not land"
    assert missions.get(mission.id).status == "READY"


@pytest.mark.asyncio
async def test_an_admitted_attempt_with_no_run_goes_back_to_waiting(cx, tmp_path) -> None:
    missions = _running_mission(cx)
    run = _Run(cx, tmp_path)
    assert missions.admit_attempt(_cid("A"))
    assert count_in_flight(cx) == 1  # an admitted attempt with no run holds a place
    step = _step(missions, run, limit=1)
    assert step.recover(missions.get_by_correlation_id(CID)) == [_cid("A")]
    assert missions.attempt_by_correlation_id(_cid("A")).status == "WAITING"
    await step.run_once()
    assert run.started == [_cid("A")]
    assert count_in_flight(cx) == 1  # now counted through its planning run


@pytest.mark.asyncio
async def test_admission_respects_the_queue_limit(cx, tmp_path) -> None:
    missions = _running_mission(cx)
    run = _Run(cx, tmp_path)
    step = _step(missions, run, limit=2)
    await step.run_once()
    run.landed(_cid("A"), "FEAT-AAA1")
    run.runs.record_queued(
        correlation_id="other-work",
        originating_user=RICH,
        expected_approver=RICH,
        request_text="something else",
        triggered_by="jarvis",
    )
    await step.run_once()
    assert run.started == [_cid("A"), _cid("B")]  # one place left: B, not C


@pytest.mark.asyncio
async def test_the_step_counts_again_inside_the_admission_transaction(cx, tmp_path) -> None:
    """The count rises between the step's own look and the claim (another
    admitter took the last place): nothing is admitted and nothing started."""
    missions = _running_mission(cx)
    run = _Run(cx, tmp_path)
    looks: list[bool] = []

    def rising() -> int:
        looks.append(cx.in_transaction)
        return 0 if len(looks) == 1 else 1

    step = MissionStep(
        missions,
        planning_run=run.runs.get_run,
        count_in_flight=rising,
        max_in_flight=1,
        start_run=run.start,
        failure_reason=lambda _run, state, _cid: state,
        clock=lambda: datetime.now(UTC),
    )
    assert not await step.admit(missions.get_by_correlation_id(CID))
    assert looks == [False, True]  # the second look is inside the claim
    assert run.started == []
    assert missions.attempt_by_correlation_id(_cid("A")).status == "WAITING"


@pytest.mark.asyncio
async def test_a_refusal_before_a_build_row_fails_the_attempt_and_the_mission_finishes(
    cx, tmp_path
) -> None:
    from forge.cli._serve_deps import _work_queue_rejection_recorder

    missions = _running_mission(cx)
    mission = missions.get_by_correlation_id(CID)
    run = _Run(cx, tmp_path)
    step = _step(missions, run)
    await step.run_once()
    run.planning(_cid("A"), "BUILD_QUEUED")
    recorder = _work_queue_rejection_recorder(SimpleNamespace(connection=cx))
    assert recorder(_cid("A"), "the feature file is outside the allowed folders. More words.")
    assert not recorder(_cid("A"), "again")  # idempotent
    attempt = missions.attempt_by_correlation_id(_cid("A"))
    assert attempt.status == "FAILED"
    assert attempt.reason == "the feature file is outside the allowed folders"
    await step.run_once()
    assert set(_statuses(missions, mission.id).values()) == {"FAILED", "HELD"}
    assert missions.get(mission.id).status == "READY"


@pytest.mark.asyncio
async def test_a_failed_planning_run_fails_the_attempt(cx, tmp_path) -> None:
    missions = _running_mission(cx)
    run = _Run(cx, tmp_path)
    step = _step(missions, run)
    await step.run_once()
    run.planning(_cid("A"), "FAILED")
    await step.run_once()
    attempt = missions.attempt_by_correlation_id(_cid("A"))
    assert attempt.status == "FAILED" and "FAILED" in attempt.reason


def test_a_mission_build_never_reads_as_no_merge_card(cx, tmp_path) -> None:
    missions = _running_mission(cx)
    run = _Run(cx, tmp_path)
    missions.admit_attempt(_cid("A"))
    run.runs.record_queued(
        correlation_id=_cid("A"),
        originating_user=RICH,
        expected_approver=RICH,
        request_text="x",
        triggered_by="jarvis",
    )
    run.planning(_cid("A"), "BUILD_QUEUED")
    run.build(_cid("A"), "FEAT-AAA1", "COMPLETE")
    later = datetime.now(UTC) + timedelta(seconds=MERGE_CARD_GRACE_SECONDS + 60)
    assert work_landing(cx, _cid("A"), now=later) == NOT_LANDED_YET


def test_the_queue_lists_a_running_mission(cx, tmp_path) -> None:
    from forge.planning.work_queue_commands import execute_command
    from forge.planning.work_queue_store import WorkQueueStore

    missions = _running_mission(cx)
    missions.admit_attempt(_cid("A"))
    reply = execute_command(
        WorkQueueStore(cx),
        {"verb": "list"},
        actor_identity=RICH,
        correlation_id="list-1",
        originating_user=RICH,
    )
    assert reply.splitlines() == [
        "Nothing in the queue.",
        'mission "Tags for notes": 0 of 4 landed, A building, B waiting for A, '
        "C waiting for A, D waiting for B and C",
    ]


def test_active_missions_block_a_router_restart(cx) -> None:
    missions = _running_mission(cx)
    mission = missions.get_by_correlation_id(CID)
    assert f"mission:{mission.id}:RUNNING" in router_restart_blockers(cx)
    missions.set_status(mission.id, "NOT_MERGED", expected=("RUNNING",), reason="x")
    assert not any(b.startswith("mission:") for b in router_restart_blockers(cx))


# ---------------------------------------------------------------------------
# Section 5: no build gate, recorded
# ---------------------------------------------------------------------------


def test_a_mission_build_records_its_gate_decision_citing_the_approval(cx, tmp_path) -> None:
    from forge.cli._serve_deps import _mission_build_gate_decision

    missions = _running_mission(cx)
    run = _Run(cx, tmp_path)
    build_id = run.build(_cid("A"), "FEAT-AAA1", "QUEUED")
    decision = _mission_build_gate_decision(
        run.persistence, build_id, _cid("A"), lambda: datetime.now(UTC)
    )
    assert decision["approved_by"] == RICH and decision["plan_request_id"] == "req-1"
    row = cx.execute(
        "SELECT stage_label, target_identifier, status, details_json FROM stage_log "
        "WHERE build_id = ?",
        (build_id,),
    ).fetchone()
    assert tuple(row)[:3] == ("autobuild", "autobuild_runner", "PASSED")
    assert json.loads(row[3])["mission_gate"]["decided_by"].startswith("mission ")
    from forge.cli._serve_deps import MissionBuildRefused

    mission = missions.get_by_correlation_id(CID)
    missions.set_status(mission.id, "READY", expected=("RUNNING",))
    with pytest.raises(MissionBuildRefused, match="ready rather than running"):
        _mission_build_gate_decision(
            run.persistence, build_id, _cid("A"), lambda: datetime.now(UTC)
        )
    cx.execute("PRAGMA foreign_keys = OFF")
    cx.execute("DELETE FROM missions WHERE id = ?", (mission.id,))
    with pytest.raises(MissionBuildRefused, match="not on record"):
        _mission_build_gate_decision(
            run.persistence, build_id, _cid("A"), lambda: datetime.now(UTC)
        )
    assert (
        cx.execute("SELECT COUNT(*) FROM stage_log WHERE build_id = ?", (build_id,))
        .fetchone()[0]
        == 1
    )
    other = run.build("not-a-mission", "FEAT-OTH1", "QUEUED")
    assert (
        _mission_build_gate_decision(run.persistence, other, "not-a-mission", datetime.now)
        is None
    )


# ---------------------------------------------------------------------------
# Landing, read strictly
# ---------------------------------------------------------------------------


def _admitted_with_run(missions: MissionStore, run: _Run, key: str = "A") -> str:
    cid = _cid(key)
    missions.admit_attempt(cid)
    run.runs.record_queued(
        correlation_id=cid,
        originating_user=RICH,
        expected_approver=RICH,
        request_text="x",
        triggered_by="jarvis",
    )
    run.planning(cid, "BUILD_QUEUED")
    return cid


def _lines(*lines: dict[str, Any]) -> str:
    return json.dumps(
        [
            {"kind": "done", "attempt": 1, "at": datetime.now(UTC).isoformat(), **line}
            for line in lines
        ]
    )


def test_a_published_send_alone_is_not_a_mission_landing(cx, tmp_path) -> None:
    missions = _running_mission(cx)
    run = _Run(cx, tmp_path)
    cid = _admitted_with_run(missions, run)
    build_id = run.build(cid, "FEAT-AAA1", "COMPLETE")
    cx.execute(
        "INSERT INTO publication_records (build_id, lines_json) VALUES (?, ?)",
        (build_id, _lines({"step": "send", "detail": {"published": True}})),
    )
    assert work_landing(cx, cid) == NOT_LANDED_YET
    cx.execute(
        "UPDATE publication_records SET result = ? WHERE build_id = ?",
        (RESULT_PUBLISHED_TO_MISSION_BRANCH, build_id),
    )
    assert work_landing(cx, cid) == "landed"


def test_only_the_last_join_says_whether_it_was_a_conflict(cx, tmp_path) -> None:
    from forge.planning.mission_step import landing_was_a_conflict

    missions = _running_mission(cx)
    run = _Run(cx, tmp_path)
    cid = _admitted_with_run(missions, run)
    build_id = run.build(cid, "FEAT-AAA1", "COMPLETE")
    conflict = {"step": "join", "detail": {"joined": False, "conflict": True}}
    joined = {"step": "join", "detail": {"joined": True}}
    cx.execute(
        "INSERT INTO publication_records (build_id, lines_json) VALUES (?, ?)",
        (build_id, _lines(conflict, joined)),
    )
    assert not landing_was_a_conflict(cx, build_id)
    cx.execute(
        "UPDATE publication_records SET lines_json = ? WHERE build_id = ?",
        (_lines(joined, conflict), build_id),
    )
    assert landing_was_a_conflict(cx, build_id)


def test_a_finished_build_with_no_landing_report_waits_however_long(cx, tmp_path) -> None:
    """Resuming a press that stopped is the merge side's job; no timer here
    may call a feature "not landed" that a resumed press could still land."""
    missions = _running_mission(cx)
    run = _Run(cx, tmp_path)
    cid = _admitted_with_run(missions, run)
    build_id = run.build(cid, "FEAT-AAA1", "COMPLETE")
    week_ago = datetime.now(UTC) - timedelta(days=7)
    cx.execute(
        "UPDATE builds SET completed_at = ? WHERE build_id = ?",
        (week_ago.isoformat(), build_id),
    )
    step = _step(missions, run)
    step.close(missions.get_by_correlation_id(CID))
    assert missions.attempt_by_correlation_id(cid).status == "ADMITTED"
    assert count_in_flight(cx) == 1  # and it still holds its place


def test_the_presss_own_failed_report_fails_the_attempt(cx, tmp_path) -> None:
    missions = _running_mission(cx)
    run = _Run(cx, tmp_path)
    cid = _admitted_with_run(missions, run)
    build_id = run.build(cid, "FEAT-AAA1", "COMPLETE")
    cx.execute(
        "INSERT INTO publication_records (build_id, result, lines_json) VALUES (?, ?, ?)",
        (
            build_id,
            "publication pending",
            _lines(
                {
                    "step": "join",
                    "detail": {"joined": False, "refusal": "the check failed. Details."},
                }
            ),
        ),
    )
    now = datetime.now(UTC).isoformat()
    cx.execute(
        "INSERT INTO stage_log (build_id, stage_label, target_kind, target_identifier, "
        "status, started_at, completed_at, duration_secs, details_json) "
        "VALUES (?, 'merge', 'local_tool', 'merge_deploy_executor', 'FAILED', ?, ?, 0, '{}')",
        (build_id, now, now),
    )
    _step(missions, run).close(missions.get_by_correlation_id(CID))
    attempt = missions.attempt_by_correlation_id(cid)
    assert attempt.status == "FAILED" and attempt.reason == "the check failed"
    assert [a.attempt for a in missions.attempts(attempt.mission_id) if a.key == "A"] == [1]


def test_a_mission_merge_card_is_named_by_the_mission(cx, tmp_path) -> None:
    from forge.planning.work_queue_loop import merge_hold_line, unanswered_merge_cards

    missions = _running_mission(cx)
    mission = missions.get_by_correlation_id(CID)
    run = _Run(cx, tmp_path)
    build_id = run.build(CID, "FEAT-DDD1", "COMPLETE")
    cx.execute(
        "UPDATE builds SET mission_id = ?, mission_role = 'mission-merge' WHERE build_id = ?",
        (mission.id, build_id),
    )
    now = datetime.now(UTC).isoformat()
    cx.execute(
        "INSERT INTO stage_log (build_id, stage_label, target_kind, target_identifier, "
        "status, started_at, completed_at, duration_secs, details_json) "
        "VALUES (?, 'merge', 'subagent', 'merge_deploy_offer', 'GATED', ?, ?, 0, '{}')",
        (build_id, now, now),
    )
    cards = unanswered_merge_cards(cx)
    assert merge_hold_line(cards, datetime.now(UTC)).startswith(
        'the merge card for mission "Tags for notes" is waiting'
    )

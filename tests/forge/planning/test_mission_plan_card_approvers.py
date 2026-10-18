"""Who may answer a mission plan card (Linear front door, 18 October 2026).

A mission plan card's answer is checked by the same approval subscriber the
planning cards use, built the way the daemon builds it (``config.approval``
plus the card's expected approver, the mission's originator). These tests run
the real :class:`MissionFlow` against a real :class:`ApprovalSubscriber` on a
fake bus, so the identity rule is the one production applies:

* a collaborator on ``approval.additional_approvers`` may answer;
* anyone else is still refused, and with an empty list nothing changes;
* the first answer wins when a collaborator and the originator both answer.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

import pytest
from nats_core.envelope import EventType, MessageEnvelope

from forge.adapters.nats.approval_subscriber import (
    ApprovalSubscriber,
    ApprovalSubscriberDeps,
)
from forge.adapters.sqlite import connect as sqlite_connect
from forge.config.models import ApprovalConfig
from forge.gating.identity import derive_request_id
from forge.lifecycle import migrations as lifecycle_migrations
from forge.lifecycle.missions import MissionStore
from forge.planning.missions_flow import (
    MISSION_PLAN_STAGE_LABEL,
    MissionFlow,
    validate_mission,
)

CID = "slack-mission-approvers-0001"
REPO = "guardkit/react-go-notes-fixture"
OWNER = "U-OWNER"
COLLABORATOR = "linear:user-0001"
MAIN_TIP = "a" * 40


@pytest.fixture()
def cx(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
    lifecycle_migrations.apply_at_boot(connection)
    MissionStore(connection)  # the boot step
    try:
        yield connection
    finally:
        connection.close()


class _Bus:
    """One subscription at a time, delivering envelopes to the live callback."""

    def __init__(self) -> None:
        self.callback: Callable[[MessageEnvelope], Awaitable[None]] | None = None

    def client(self, armed: asyncio.Event | None) -> Any:
        bus = self

        class _Client:
            async def subscribe(self, topic: str, callback: Any) -> Any:
                bus.callback = callback
                if armed is not None:
                    armed.set()
                return SimpleNamespace(unsubscribe=_noop)

        return _Client()

    async def answer(self, request_id: str, decision: str, decided_by: str) -> None:
        assert self.callback is not None, "the card's subscription is not armed"
        await self.callback(
            MessageEnvelope(
                source_id="jarvis",
                event_type=EventType.APPROVAL_RESPONSE,
                payload={
                    "request_id": request_id,
                    "decision": decision,
                    "decided_by": decided_by,
                },
            )
        )


async def _noop() -> None:
    return None


class _Gate:
    async def ensure_seeded(self, key: str, *, origin_kind: str, origin_id: str) -> Any:
        return object()


async def _branch_created(**_: Any) -> Any:
    return SimpleNamespace(outcome="created", refusal=None)


async def _remote_tip(repo: str) -> Any:
    return SimpleNamespace(ok=True, commit=MAIN_TIP, branch="main", refusal=None)


def _flow(
    missions: MissionStore, bus: _Bus, additional: list[str], tasks: list[Any]
) -> MissionFlow:
    approval = ApprovalConfig(additional_approvers=additional)

    def subscriber_factory(expected_approver: str | None, armed: Any) -> Any:
        # As the daemon's planning composition builds it.
        return ApprovalSubscriber(
            ApprovalSubscriberDeps(
                nats_client=bus.client(armed),
                config=approval,
                publish_refresh=None,
                expected_approver=expected_approver,
            )
        )

    async def publish_card(envelope: Any) -> None:
        return None

    async def notify(cid: str, message: str, *, parent_request_id: str | None = None) -> None:
        return None

    def spawn(work: Any) -> Any:
        task = asyncio.ensure_future(work)
        tasks.append(task)
        return task

    return MissionFlow(
        missions,
        publish_card=publish_card,
        subscriber_factory=subscriber_factory,
        notify=notify,
        read_remote_tip=_remote_tip,
        create_branch=_branch_created,
        approval_wait_seconds=3600,
        routing_gate=_Gate(),
        spawn=spawn,
    )


def _propose(flow: MissionFlow) -> Any:
    command = {
        "verb": "mission",
        "title": "Tags for notes",
        "target_repo": REPO,
        "features": [
            {"key": "a", "needs": [], "sentence": "Store tags on notes"},
            {"key": "b", "needs": ["a"], "sentence": "Filter by tag"},
        ],
    }
    checked = validate_mission(command, mission_correlation_id=CID)
    assert not isinstance(checked, str), checked
    mission, created = flow.propose(
        title="Tags for notes",
        target_repo=REPO,
        correlation_id=CID,
        originating_user=OWNER,
        features=checked[1],
        parent_request_id="thread-1",
    )
    assert created
    return mission


def _request_id(mission_id: str) -> str:
    return derive_request_id(
        build_id=f"mission-{mission_id}",
        stage_label=MISSION_PLAN_STAGE_LABEL,
        attempt_count=0,
    )


async def _settle() -> None:
    for _ in range(30):
        await asyncio.sleep(0)


async def _stop(tasks: list[Any]) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_listed_collaborator_may_approve_the_plan_card(cx) -> None:
    missions, bus, tasks = MissionStore(cx), _Bus(), []
    flow = _flow(missions, bus, [COLLABORATOR], tasks)
    mission = _propose(flow)
    await _settle()
    await bus.answer(_request_id(mission.id), "approve", COLLABORATOR)
    await _settle()
    try:
        got = missions.get(mission.id)
        assert got.approved_by == COLLABORATOR
        assert got.status != "PROPOSED"
    finally:
        await _stop(tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decided_by", "additional"),
    [("linear:user-0002", [COLLABORATOR]), (COLLABORATOR, [])],
)
async def test_an_unlisted_identity_is_still_refused(
    cx, decided_by: str, additional: list[str]
) -> None:
    missions, bus, tasks = MissionStore(cx), _Bus(), []
    flow = _flow(missions, bus, additional, tasks)
    mission = _propose(flow)
    await _settle()
    await bus.answer(_request_id(mission.id), "approve", decided_by)
    await _settle()
    try:
        got = missions.get(mission.id)
        assert got.status == "PROPOSED"
        assert got.approved_by is None
    finally:
        await _stop(tasks)


@pytest.mark.asyncio
async def test_first_answer_wins_between_collaborator_and_originator(cx) -> None:
    missions, bus, tasks = MissionStore(cx), _Bus(), []
    flow = _flow(missions, bus, [COLLABORATOR], tasks)
    mission = _propose(flow)
    await _settle()
    await bus.answer(_request_id(mission.id), "reject", COLLABORATOR)
    await bus.answer(_request_id(mission.id), "approve", OWNER)
    await _settle()
    try:
        got = missions.get(mission.id)
        assert got.status == "REJECTED"
        assert got.approved_by is None
    finally:
        await _stop(tasks)

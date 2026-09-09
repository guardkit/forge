"""The owner's merge word on the checkpoint's card reaches the merge press.

2026-09-09, the fix journey's twentieth attempt. The review found four
things, five work legs fixed them, the follow-up review came back clean, the
merge-ready checkpoint ran the repository's declared suite inside the sandbox
and it passed, the card went out, Rich answered approve in Slack eleven
seconds later — and nothing checked the candidate, nothing merged, nothing
promoted. There were two merge cards in this estate and only one of them had
a listener: the press wants a request id beginning ``merge-`` backed by a
durable merge-offer row, and the checkpoint's card carried neither.

So this file walks the whole join, offline: a green checkpoint publishes its
card through the routine build's own publisher, the envelope that card draws
is handed to the REAL merge-approval consumer, and the consumer must get past
BOTH of its checks and reach the press. The press's own act is faked — no git
runs, nothing deploys, no broker is contacted. A red checkpoint publishes
nothing at all, and one card answered twice presses exactly once.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
from nats_core.envelope import EventType, MessageEnvelope
from nats_core.events import ApprovalResponsePayload

from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli._serve_gate_activation import make_merge_card_publisher
from forge.config.models import ForgeConfig
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.pipeline import merge_executor as merge_executor_mod
from forge.pipeline.merge_executor import (
    MERGE_DECISION_TARGET_IDENTIFIER,
    MergeApprovalConsumer,
    MergeExecutorDeps,
)
from forge.pipeline.merge_offer import (
    MERGE_OFFER_TARGET_IDENTIFIER,
    MergeOfferService,
)
from forge.pipeline.merge_ready_checkpoint import (
    GatesReport,
    GateStatus,
    MergeCardOutcome,
    MergeReadyCheckpointPublisher,
)

FEATURE_ID = "FEAT-39F6"
REPO = "appmilla/api_test"
CORRELATION = "corr-press-1"
BRANCH = "repair/TASK-FEAT39F6FIX1"
MAIN_SHA = "b" * 40
NOW = datetime(2026, 9, 9, 9, 8, 15, tzinfo=UTC)


@pytest.fixture()
def pool(tmp_path: Path) -> Iterator[SqliteLifecyclePersistence]:
    cx: sqlite3.Connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    yield SqliteLifecyclePersistence(connection=cx, db_path=tmp_path / "forge.db")
    cx.close()


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "api_test"
    root.mkdir()
    return root


@pytest.fixture()
def config(repo_root: Path) -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {"target_repo_paths": {REPO: str(repo_root)}},
            "approval": {"expected_approver": "rich"},
            "merge_executor": {"enabled": True},
        }
    )


class _Wire:
    """The bus, at its own seam: two lists instead of a broker."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, bytes]] = []
        self.paused: list[Any] = []
        self.reports: list[Any] = []

    async def raw_publish(self, subject: str, body: bytes) -> None:
        self.requests.append((subject, body))

    async def publish_build_paused(self, payload: Any) -> None:
        self.paused.append(payload)

    async def publish_stage_complete(self, payload: Any) -> None:
        self.reports.append(payload)


def _build_row(pool: SqliteLifecyclePersistence) -> str:
    build_id = pool.record_pending_build(
        SimpleNamespace(
            feature_id=FEATURE_ID,
            repo=REPO,
            branch=BRANCH,
            feature_yaml_path="features/fix.yaml",
            max_turns=5,
            sdk_timeout_seconds=1800,
            triggered_by="cli",
            originating_adapter="terminal",
            originating_user="rich",
            correlation_id=CORRELATION,
            parent_request_id=None,
            queued_at=NOW,
        )
    )
    pool.record_merge_branch(build_id, BRANCH)
    return build_id


def _checkpoint(
    *,
    pool: SqliteLifecyclePersistence,
    config: ForgeConfig,
    wire: _Wire,
    gates: GatesReport,
) -> MergeReadyCheckpointPublisher:
    async def _git_head(_repo_root: Path) -> str:
        return MAIN_SHA

    offer_service = MergeOfferService(
        config=config,
        pool=pool,
        pipeline_publisher=wire,
        raw_publish=wire.raw_publish,
        git_head=_git_head,
        baseline_reader=lambda _build_id: None,
        clock=lambda: NOW,
    )
    return MergeReadyCheckpointPublisher(
        publish_card=make_merge_card_publisher(
            offer_service=offer_service, sqlite_pool=pool, clock=lambda: NOW
        ),
        gates_green_reader=lambda **_kw: gates,
        has_commits_probe=lambda _build_id: True,
        branch_reader=lambda _build_id: BRANCH,
    )


def _green() -> GatesReport:
    return GatesReport(
        status=GateStatus.GREEN,
        detail="declared suite GREEN (817 passed, 2 deselected)",
        deferred_detail=(
            "5 stamped checks (probe:bus, probe:process) have no live-gate "
            "evidence yet: the merge press stands the candidate up in the "
            "sandbox and runs this repository's live gate on it before "
            "anything lands."
        ),
    )


def _the_owners_answer(wire: _Wire, *, decision: str = "approve") -> MessageEnvelope:
    """The response envelope jarvis draws from the card that went out."""
    subject, body = wire.requests[-1]
    request = json.loads(body.decode("utf-8"))
    assert subject == f"agents.approval.forge.merge-{FEATURE_ID}"
    return MessageEnvelope(
        source_id="jarvis",
        event_type=EventType.APPROVAL_RESPONSE,
        correlation_id=request["correlation_id"],
        payload=ApprovalResponsePayload(
            request_id=request["payload"]["request_id"],
            decision=decision,
            decided_by="rich",
        ).model_dump(mode="json"),
    )


def _consumer(
    *,
    config: ForgeConfig,
    pool: SqliteLifecyclePersistence,
    wire: _Wire,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[MergeApprovalConsumer, list[dict[str, Any]]]:
    pressed: list[dict[str, Any]] = []

    async def _fake_press(**kwargs: Any) -> None:
        pressed.append(kwargs)

    monkeypatch.setattr(merge_executor_mod, "execute_merge_deploy", _fake_press)

    async def _never_run(**_kw: Any) -> Any:  # pragma: no cover - must not run
        raise AssertionError("the press's own tools must not run in this test")

    deps = MergeExecutorDeps(
        config=config,
        pool=pool,
        pipeline_publisher=wire,
        guardkit_run=_never_run,
        deploy_dispatcher=_never_run,
        clock=lambda: NOW,
    )
    return MergeApprovalConsumer(deps), pressed


async def _drain(consumer: MergeApprovalConsumer) -> None:
    while consumer._tasks:  # noqa: SLF001 — the consumer's own task set
        await asyncio.gather(*list(consumer._tasks))


@pytest.mark.asyncio
async def test_the_merge_word_on_the_checkpoint_s_card_reaches_the_press(
    pool: SqliteLifecyclePersistence,
    config: ForgeConfig,
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_id = _build_row(pool)
    wire = _Wire()
    checkpoint = _checkpoint(pool=pool, config=config, wire=wire, gates=_green())

    decision = await checkpoint.submit_decision(
        build_id=build_id,
        feature_id=FEATURE_ID,
        auto_approve=False,
        rationale="mode-c-commits-present",
    )
    assert decision.outcome is MergeCardOutcome.CARD_PUBLISHED

    # The card carries the press's request id and the press's own row.
    request = json.loads(wire.requests[-1][1].decode("utf-8"))
    assert request["payload"]["request_id"] == f"merge-{build_id}"
    assert request["payload"]["request_id"].startswith("merge-")
    assert [
        s.target_identifier
        for s in pool.read_stages(build_id)
        if s.target_identifier == MERGE_OFFER_TARGET_IDENTIFIER
    ] == [MERGE_OFFER_TARGET_IDENTIFIER]
    # And the Slack card's own join key.
    assert wire.paused[-1].build_id == f"merge-{FEATURE_ID}"

    # Now the owner answers, and the REAL consumer reads it.
    consumer, pressed = _consumer(
        config=config, pool=pool, wire=wire, monkeypatch=monkeypatch
    )
    await consumer.handle_envelope(_the_owners_answer(wire))
    await _drain(consumer)

    assert len(pressed) == 1, "the merge word reached the press"
    assert pressed[0]["build_id"] == build_id
    assert pressed[0]["feature_id"] == FEATURE_ID
    assert pressed[0]["repo"] == REPO
    assert pressed[0]["repo_root"] == repo_root
    assert pressed[0]["expect_main_sha"] == MAIN_SHA
    assert pressed[0]["merge_branch"] == BRANCH
    assert pressed[0]["decided_by"] == "rich"
    assert MERGE_DECISION_TARGET_IDENTIFIER in [
        s.target_identifier for s in pool.read_stages(build_id)
    ]


@pytest.mark.asyncio
async def test_the_same_card_answered_twice_presses_once(
    pool: SqliteLifecyclePersistence,
    config: ForgeConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_id = _build_row(pool)
    wire = _Wire()
    checkpoint = _checkpoint(pool=pool, config=config, wire=wire, gates=_green())
    await checkpoint.submit_decision(
        build_id=build_id,
        feature_id=FEATURE_ID,
        auto_approve=False,
        rationale="mode-c-commits-present",
    )

    consumer, pressed = _consumer(
        config=config, pool=pool, wire=wire, monkeypatch=monkeypatch
    )
    answer = _the_owners_answer(wire)
    await consumer.handle_envelope(answer)
    await _drain(consumer)
    await consumer.handle_envelope(answer)
    await _drain(consumer)

    assert len(pressed) == 1


@pytest.mark.asyncio
async def test_a_red_checkpoint_publishes_nothing(
    pool: SqliteLifecyclePersistence, config: ForgeConfig
) -> None:
    build_id = _build_row(pool)
    wire = _Wire()
    checkpoint = _checkpoint(
        pool=pool,
        config=config,
        wire=wire,
        gates=GatesReport(
            status=GateStatus.RED,
            failed_gates=("tests/users/test_router.py::test_by_email_delete_success",),
            detail="declared suite RED (2 failed, 817 passed)",
        ),
    )

    decision = await checkpoint.submit_decision(
        build_id=build_id,
        feature_id=FEATURE_ID,
        auto_approve=False,
        rationale="mode-c-commits-present",
    )

    assert decision.outcome is MergeCardOutcome.RED_GATE_LOOP_BACK
    assert decision.card_published is False
    assert wire.requests == []
    assert wire.paused == []
    assert pool.read_stages(build_id) == []

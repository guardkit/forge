"""A Slack hand-over waiting at its build-start card survives a forge restart.

Register-projects design (5 October 2026), part 3, and Codex implementation
review round 1, R2. When the forge stops while a build handed over from Slack
waits at its build-start card, the boot sweep (``rearm_paused_gates``) owns
the card's decision. It answers the thread from the build's persisted row —
``parent_request_id`` and ``originating_adapter`` — exactly as the live path
does: "Building …" once the launch has gone out, or the declined / timed-out
line on a terminal outcome. Once per outcome: a later boot finds nothing
PAUSED and says nothing. A build that did not come from Slack is never
answered.

Driven over the same real pieces as ``test_gate_restart_recovery``: the live
gate to a genuine PAUSED row, the daemon "dies", then a fresh composition
runs the real sweep over the same SQLite ledger and in-memory broker.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli import _serve_deps_gating
from forge.cli._serve_gate_activation import maybe_gate_build, rearm_paused_gates
from forge.gating.sqlite_adapters import build_sqlite_gate_adapters
from forge.gating.wrappers import GateOutcome
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.lifecycle.state_machine import BuildState
from forge.pipeline.prepared_admission import AdmittedBuild
from forge.planning.notifications import NOTIFICATION_SUBJECT, make_build_thread_reply

from .test_gate_restart_recovery import (
    RICH,
    EventLogNats,
    FixedClock,
    _build_parts,
    _FakeResumeLauncher,
    _forge_config,
    _mirror_subject,
    _request_id,
    _row,
    _wait_until,
)

FEATURE = "FEAT-HANDOV"
CORRELATION = "plan-handover-0001-build"
THREAD = "1759660000.000300"
SOURCE = "c0ffee1234567890c0ffee1234567890c0ffee12"
QUEUED_AT = datetime(2026, 10, 5, 15, 0, 1, tzinfo=UTC)


@pytest.fixture()
def pool(tmp_path: Path):
    db_path = tmp_path / "forge.db"
    cx = sqlite_connect.connect_writer(db_path)
    migrations.apply_at_boot(cx)
    p = SqliteLifecyclePersistence(connection=cx, db_path=db_path)
    yield p
    cx.close()


@pytest.fixture()
def nats() -> EventLogNats:
    return EventLogNats()


@pytest.fixture(autouse=True)
def _reset_bound_parts():
    _serve_deps_gating._reset_for_tests()
    yield
    _serve_deps_gating._reset_for_tests()


async def _paused_hand_over(
    nats: EventLogNats,
    pool: SqliteLifecyclePersistence,
    *,
    adapter: str = "slack",
    parent_request_id: str | None = THREAD,
    forge_config: Any = None,
) -> str:
    """A hand-over recorded and paused at its card; then the forge stops."""
    payload = SimpleNamespace(
        feature_id=FEATURE,
        repo="guardkit/doc_probe",
        branch="prepared/FEAT-HANDOV",
        feature_yaml_path=f".guardkit/features/{FEATURE}.yaml",
        max_turns=5,
        sdk_timeout_seconds=1800,
        triggered_by="jarvis",
        originating_adapter=adapter,
        originating_user="U-RICH",
        correlation_id=CORRELATION,
        parent_request_id=parent_request_id,
        queued_at=QUEUED_AT,
        requested_at=QUEUED_AT,
    )
    build_id = pool.record_pending_build(
        payload,
        admitted=AdmittedBuild(
            start_commit=SOURCE,
            target_branch="main",
            source_commit=SOURCE,
            memory_project="doc_probe",
        ),
    )
    repo, sm = build_sqlite_gate_adapters(pool, clock=FixedClock())
    parts = _build_parts(nats, forge_config=forge_config)
    task = asyncio.create_task(
        maybe_gate_build(
            parts=parts,
            sqlite_pool=pool,
            gate_repository=repo,
            gate_state_machine=sm,
            build_id=build_id,
            feature_id=FEATURE,
            correlation_id=CORRELATION,
            clock=FixedClock(),
        )
    )
    await _wait_until(
        lambda: _row(pool, build_id)[0] == BuildState.PAUSED.value,
        what="paused row",
    )
    await _wait_until(
        lambda: nats.subscribers.get(_mirror_subject(build_id)),
        what="subscriber live",
    )
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return build_id


async def _restart(
    nats: EventLogNats,
    pool: SqliteLifecyclePersistence,
    launcher: _FakeResumeLauncher,
    *,
    forge_config: Any = None,
) -> list[Any]:
    """A fresh composition runs the real boot sweep with the thread answer."""
    nats.reset_wire()
    parts = _build_parts(nats, forge_config=forge_config)
    _serve_deps_gating.bind_gate_parts(parts)
    repo, sm = build_sqlite_gate_adapters(pool, clock=FixedClock())
    return await rearm_paused_gates(
        parts=parts,
        sqlite_pool=pool,
        gate_repository=repo,
        gate_state_machine=sm,
        resume_launcher=launcher,
        client=nats,
        clock=FixedClock(),
        reply_in_thread=make_build_thread_reply(nats),
    )


def _replies(nats: EventLogNats) -> list[dict[str, Any]]:
    return [
        json.loads(body)["payload"]
        for body in nats.published.get(NOTIFICATION_SUBJECT, [])
    ]


@pytest.mark.asyncio
async def test_approved_after_a_restart_says_building_once_after_the_launch(
    nats: EventLogNats, pool: SqliteLifecyclePersistence
) -> None:
    build_id = await _paused_hand_over(nats, pool)
    seen_at_launch: list[int] = []

    class _NotingLauncher(_FakeResumeLauncher):
        async def __call__(self, **kwargs: Any) -> None:  # type: ignore[override]
            seen_at_launch.append(len(_replies(nats)))
            await super().__call__(**kwargs)

    launcher = _NotingLauncher()
    tasks = await _restart(nats, pool, launcher)
    await nats.deliver_response(
        build_id=build_id, request_id=_request_id(build_id, 0), decision="approve"
    )
    outcome = await asyncio.wait_for(tasks[0], timeout=5.0)

    assert outcome is GateOutcome.RESUMED
    assert seen_at_launch == [0], "nothing is said before the launch"
    assert len(launcher.calls) == 1
    (reply,) = _replies(nats)
    assert reply["message"] == (
        f"Building {FEATURE} for guardkit/doc_probe from prepared/FEAT-HANDOV "
        f"at {SOURCE[:7]}"
    )
    assert reply["parent_request_id"] == THREAD and reply["thread_ts"] == THREAD

    # A later boot finds nothing paused and says nothing more.
    assert await _restart(nats, pool, _FakeResumeLauncher()) == []
    assert _replies(nats) == []


@pytest.mark.asyncio
async def test_declined_after_a_restart_is_told_so_once(
    nats: EventLogNats, pool: SqliteLifecyclePersistence
) -> None:
    build_id = await _paused_hand_over(nats, pool)
    launcher = _FakeResumeLauncher()
    tasks = await _restart(nats, pool, launcher)
    await nats.deliver_response(
        build_id=build_id,
        request_id=_request_id(build_id, 0),
        decision="reject",
        notes="not now",
    )
    outcome = await asyncio.wait_for(tasks[0], timeout=5.0)

    assert outcome is GateOutcome.CANCELLED
    assert launcher.calls == []
    (reply,) = _replies(nats)
    assert reply["message"] == (
        f"{FEATURE} was not started: the build-start card was declined."
    )
    assert reply["parent_request_id"] == THREAD and reply["level"] == "warning"
    assert await _restart(nats, pool, _FakeResumeLauncher()) == []
    assert _replies(nats) == []


@pytest.mark.asyncio
async def test_timed_out_after_a_restart_is_told_so_once(
    nats: EventLogNats, pool: SqliteLifecyclePersistence
) -> None:
    cfg = _forge_config(
        default_wait_seconds=0,
        max_wait_seconds=3600,
        expected_approver=RICH,
        autobuild_gate_max_wait_seconds=3600,
    )
    await _paused_hand_over(nats, pool, forge_config=cfg)
    launcher = _FakeResumeLauncher()
    tasks = await _restart(nats, pool, launcher, forge_config=cfg)
    outcome = await asyncio.wait_for(tasks[0], timeout=5.0)

    assert outcome is GateOutcome.TIMED_OUT
    assert launcher.calls == []
    assert [r["message"] for r in _replies(nats)] == [
        f"{FEATURE} was not started: the build-start card timed out."
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter", "parent"),
    [("telegram", "dispatch-abc123"), ("slack", None), ("cli-wrapper", None)],
)
async def test_a_build_not_handed_over_from_slack_gets_nothing_across_recovery(
    nats: EventLogNats,
    pool: SqliteLifecyclePersistence,
    adapter: str,
    parent: str | None,
) -> None:
    build_id = await _paused_hand_over(
        nats, pool, adapter=adapter, parent_request_id=parent
    )
    launcher = _FakeResumeLauncher()
    tasks = await _restart(nats, pool, launcher)
    await nats.deliver_response(
        build_id=build_id, request_id=_request_id(build_id, 0), decision="approve"
    )
    outcome = await asyncio.wait_for(tasks[0], timeout=5.0)

    assert outcome is GateOutcome.RESUMED
    assert len(launcher.calls) == 1
    assert _replies(nats) == []

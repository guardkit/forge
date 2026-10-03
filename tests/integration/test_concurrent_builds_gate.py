"""Two builds at the pre-build approval card, through the production path.

Limit 2. The real daemon loop (``run_daemon`` → ``_consume_forever`` →
``_process_message``) runs the production dispatcher
(``make_handle_message_dispatcher`` → ``handle_message`` →
``dispatch_build`` → ``maybe_gate_build``) with the REAL approval gate parts,
a REAL SQLite database and the REAL gate adapters — the same set-up as
``test_gate_activation_production_wiring.py``, whose helpers and fixtures are
reused. Only two things are doubled: the NATS transport for the approval
card (the in-memory double those tests use) and the two build-queued
messages handed out by the pull subscription (message doubles that count
their acks).

The scenario: A's card is left unanswered; B is still fetched, presents its
own card, is approved first and launches; then A is approved and launches.
Each message is acknowledged exactly once, when its build reaches a terminal
state (the lifecycle bridge's ``handle.ack()``), even when terminal is
reported twice.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from nats.js.errors import NotFoundError
from nats_core.envelope import EventType, MessageEnvelope
from nats_core.events import BuildQueuedPayload

from forge.cli import _serve_daemon, _serve_deps_gating
from forge.cli._serve_config import ServeConfig
from forge.cli._serve_deps import build_pipeline_consumer_deps
from forge.cli._serve_dispatcher import make_handle_message_dispatcher
from forge.cli._serve_state import SubscriptionState
from forge.gating.sqlite_adapters import build_sqlite_gate_adapters
from forge.lifecycle.identifiers import derive_build_id
from forge.lifecycle.state_machine import BuildState

from .test_gate_activation_production_wiring import (  # noqa: F401 — fixtures
    FixedClock,
    OrderRecordingNats,
    _build_parts,
    _drive_response,
    _FakeStarter,
    _forge_config,
    _paused_subject,
    _request_id,
    _reset_bound_parts,
    _row,
    _wait_until,
    nats,
    pool,
)

A = ("FEAT-CONCA", "corr-concurrent-a")
B = ("FEAT-CONCB", "corr-concurrent-b")
QUEUED_A = datetime(2026, 10, 3, 9, 0, 0, tzinfo=UTC)
QUEUED_B = datetime(2026, 10, 3, 9, 0, 5, tzinfo=UTC)


class _Msg:
    """A pulled build-queued message that counts its acks."""

    def __init__(self, data: bytes, subject: str) -> None:
        self.data = data
        self.subject = subject
        self.acks = 0

    async def ack(self) -> None:
        self.acks += 1

    async def nak(self) -> None:  # pragma: no cover - not expected
        raise AssertionError("no build should be negatively acknowledged")


def _envelope(feature_id: str, correlation_id: str, queued_at: datetime) -> bytes:
    payload = BuildQueuedPayload(
        feature_id=feature_id,
        repo="guardkit/forge",
        branch="main",
        feature_yaml_path=f"/srv/forge/features/{feature_id}/{feature_id}.yaml",
        triggered_by="cli",
        originating_adapter="cli-wrapper",
        correlation_id=correlation_id,
        requested_at=queued_at,
        queued_at=queued_at,
    )
    return (
        MessageEnvelope(
            source_id="forge-cli",
            event_type=EventType.BUILD_QUEUED,
            correlation_id=correlation_id,
            payload=payload.model_dump(mode="json"),
        )
        .model_dump_json()
        .encode("utf-8")
    )


class _Sub:
    """Pull subscription handing out A then B, then nothing."""

    def __init__(self, msgs: list[_Msg]) -> None:
        self._msgs = list(msgs)
        self.fetches = 0

    async def fetch(self, batch: int = 1, timeout: float = 1.0) -> list[_Msg]:
        self.fetches += 1
        if self._msgs:
            return [self._msgs.pop(0)]
        await asyncio.sleep(0.01)
        raise asyncio.TimeoutError()

    async def unsubscribe(self) -> None:
        return None


class _JS:
    def __init__(self, sub: _Sub) -> None:
        self._sub = sub
        self.max_ack_pending: int | None = None

    async def consumer_info(self, stream: str, durable: str) -> Any:
        raise NotFoundError()

    async def pull_subscribe(self, **kwargs: Any) -> _Sub:
        self.max_ack_pending = kwargs["config"].max_ack_pending
        return self._sub


class _Client:
    def __init__(self, js: _JS) -> None:
        self._js = js

    def jetstream(self) -> _JS:
        return self._js

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_unanswered_card_does_not_stop_the_next_build(
    nats: OrderRecordingNats,  # noqa: F811 — imported fixture
    pool: Any,  # noqa: F811 — imported fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Real gate parts, real SQLite gate adapters, production consumer deps,
    # with the lifecycle bridge's ack registry stood in by a dict.
    cfg = _forge_config()
    parts = _build_parts(nats, forge_config=cfg)
    _serve_deps_gating.bind_gate_parts(parts)
    repo, sm = build_sqlite_gate_adapters(pool, clock=FixedClock())
    starter = _FakeStarter()
    handles: dict[tuple[str, str], Any] = {}

    async def _register(feature_id: str, correlation_id: str, handle: Any) -> None:
        handles[(feature_id, correlation_id)] = handle

    deps = build_pipeline_consumer_deps(
        nats,
        cfg,
        pool,
        async_task_starter=starter,
        register_ack_handle=_register,
        gate_repository=repo,
        gate_state_machine=sm,
        gate_clock=FixedClock(),
    )
    monkeypatch.setattr(
        _serve_daemon, "dispatch_payload", make_handle_message_dispatcher(deps)
    )

    msg_a = _Msg(_envelope(*A, QUEUED_A), f"pipeline.build-queued.{A[0]}")
    msg_b = _Msg(_envelope(*B, QUEUED_B), f"pipeline.build-queued.{B[0]}")
    js = _JS(_Sub([msg_a, msg_b]))
    build_a = derive_build_id(A[0], QUEUED_A)
    build_b = derive_build_id(B[0], QUEUED_B)
    launched = lambda: [launch["build_id"] for launch in starter.launches]  # noqa: E731

    daemon = asyncio.create_task(
        run_daemon_with_limit(_Client(js), max_concurrent_builds=2)
    )
    try:
        # Both builds present their cards; A's is never answered.
        await _wait_until(
            lambda: nats.published.get(_paused_subject(A[0]))
            and nats.published.get(_paused_subject(B[0])),
            what="both build-paused cards",
        )
        assert js.max_ack_pending == 2
        assert _row(pool, build_a)[0] == BuildState.PAUSED.value
        assert _row(pool, build_b)[0] == BuildState.PAUSED.value

        # B is approved first and launches while A still waits.
        await _drive_response(
            nats, build_id=build_b, request_id=_request_id(build_b), decision="approve"
        )
        await _wait_until(lambda: launched() == [build_b], what="B launches")
        assert _row(pool, build_a)[0] == BuildState.PAUSED.value
        assert msg_a.acks == 0 and msg_b.acks == 0  # approval is not terminal

        # Then A is approved and launches.
        await _drive_response(
            nats, build_id=build_a, request_id=_request_id(build_a), decision="approve"
        )
        await _wait_until(lambda: launched() == [build_b, build_a], what="A launches")
        assert msg_a.acks == 0 and msg_b.acks == 0

        # Terminal: the bridge acks each handle; a repeated terminal report
        # (late or duplicate completion) acks nothing more.
        await _wait_until(lambda: len(handles) == 2, what="both observers registered")
        for key in (B, A, B, A):
            await handles[key].ack()
        assert msg_a.acks == 1
        assert msg_b.acks == 1
    finally:
        daemon.cancel()
        try:
            await asyncio.wait_for(daemon, timeout=5)
        except asyncio.CancelledError:
            pass


async def run_daemon_with_limit(client: _Client, *, max_concurrent_builds: int) -> None:
    await _serve_daemon.run_daemon(
        ServeConfig(max_concurrent_builds=max_concurrent_builds),
        SubscriptionState(),
        client=client,
    )


# ---------------------------------------------------------------------------
# R4: one build of a feature at a time, even with several places
# ---------------------------------------------------------------------------

SAME = "FEAT-SAMEF"
EARLY = ("corr-same-early", datetime(2026, 10, 3, 10, 0, 0, tzinfo=UTC))
LATE = ("corr-same-late", datetime(2026, 10, 3, 10, 0, 7, tzinfo=UTC))


class _SpacedSub(_Sub):
    """Hands out its messages with a pause between them, so the first one's
    dispatch is well under way before the second is fetched."""

    async def fetch(self, batch: int = 1, timeout: float = 1.0) -> list[_Msg]:
        if self._msgs and self.fetches:
            await asyncio.sleep(0.2)
        return await super().fetch(batch, timeout)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prewritten", "delivered", "winner"),
    [
        # Nothing written ahead: whichever is fetched first goes ahead.
        (False, (EARLY, LATE), EARLY),
        (False, (LATE, EARLY), LATE),
        # Both rows written ahead (CLI / fix journey): the earlier-queued one
        # goes ahead whatever order the messages arrive in.
        (True, (EARLY, LATE), EARLY),
        (True, (LATE, EARLY), EARLY),
    ],
)
async def test_two_builds_of_one_feature_one_goes_ahead(
    nats: OrderRecordingNats,  # noqa: F811 — imported fixture
    pool: Any,  # noqa: F811 — imported fixture
    monkeypatch: pytest.MonkeyPatch,
    prewritten: bool,
    delivered: tuple[tuple[str, datetime], tuple[str, datetime]],
    winner: tuple[str, datetime],
) -> None:
    from .test_gate_activation_production_wiring import (
        _failed_subject,
        _make_payload,
        _payloads,
    )

    cfg = _forge_config()
    parts = _build_parts(nats, forge_config=cfg)
    _serve_deps_gating.bind_gate_parts(parts)
    repo, sm = build_sqlite_gate_adapters(pool, clock=FixedClock())
    starter = _FakeStarter()
    handles: dict[tuple[str, str], Any] = {}

    async def _register(feature_id: str, correlation_id: str, handle: Any) -> None:
        handles[(feature_id, correlation_id)] = handle

    deps = build_pipeline_consumer_deps(
        nats,
        cfg,
        pool,
        async_task_starter=starter,
        register_ack_handle=_register,
        gate_repository=repo,
        gate_state_machine=sm,
        gate_clock=FixedClock(),
    )
    monkeypatch.setattr(
        _serve_daemon, "dispatch_payload", make_handle_message_dispatcher(deps)
    )
    if prewritten:
        for corr, queued_at in (EARLY, LATE):
            pool.record_pending_build(
                _make_payload(feature_id=SAME, correlation_id=corr, queued_at=queued_at)
            )

    msgs = {
        corr: _Msg(_envelope(SAME, corr, queued_at), f"pipeline.build-queued.{SAME}")
        for corr, queued_at in (EARLY, LATE)
    }
    loser = LATE if winner == EARLY else EARLY
    win_build = derive_build_id(SAME, winner[1])
    lose_build = derive_build_id(SAME, loser[1])
    js = _JS(_SpacedSub([msgs[corr] for corr, _ in delivered]))

    daemon = asyncio.create_task(
        run_daemon_with_limit(_Client(js), max_concurrent_builds=2)
    )
    try:
        # The loser is refused once, with a plain reason, and acknowledged.
        await _wait_until(
            lambda: msgs[loser[0]].acks == 1, what="the second build is refused"
        )
        failed = _payloads(nats, _failed_subject(SAME))
        assert len(failed) == 1
        assert f"another build of {SAME} is already in progress" in str(failed[0])

        # The winner presents its card, is approved and launches with an
        # observer registered.
        await _wait_until(
            lambda: nats.published.get(_paused_subject(SAME)), what="winner's card"
        )
        await _drive_response(
            nats, build_id=win_build, request_id=_request_id(win_build), decision="approve"
        )
        await _wait_until(lambda: starter.launches, what="the winner launches")
        assert [launch["build_id"] for launch in starter.launches] == [win_build]
        assert list(handles) == [(SAME, winner[0])]
        assert msgs[winner[0]].acks == 0  # approval is not terminal

        if prewritten:
            # The refused row written ahead is closed, not left active.
            assert _row(pool, lose_build)[0] == BuildState.FAILED.value

        # Terminal for the winner acks its message once; the loser stays at one.
        await handles[(SAME, winner[0])].ack()
        await handles[(SAME, winner[0])].ack()
        assert msgs[winner[0]].acks == 1
        assert msgs[loser[0]].acks == 1
        assert len(_payloads(nats, _failed_subject(SAME))) == 1
    finally:
        daemon.cancel()
        try:
            await asyncio.wait_for(daemon, timeout=5)
        except asyncio.CancelledError:
            pass

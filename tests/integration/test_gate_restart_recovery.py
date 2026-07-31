"""TASK-GATE-D659 Wave 3 — restart recovery + closure (AC 5, 6, 8).

Every scenario drives the REAL boot-time rearm sweep
(:func:`forge.cli._serve_gate_activation.rearm_paused_gates`) over the in-memory
NATS double, a REAL :class:`forge.pipeline.PipelineLifecycleEmitter` on the same
transport, and a REAL SQLite database behind the SQLite gate adapters. The
"kill / recreate composition" is modelled by:

* SESSION 1 — run the live ``maybe_gate_build`` gate to a genuine PAUSED row
  (SQLite ``builds.status=PAUSED`` + persisted ``request_id`` + a persisted gate
  decision snapshot in ``stage_log``), then CANCEL the awaiting frame (the
  daemon "dies" mid-pause: the response subscriber unsubscribes, SQLite stands).
* SESSION 2 — build FRESH gate parts + adapters over the SAME pool + broker and
  run ``rearm_paused_gates`` (the boot sweep) to re-arm the round-trip.

Coverage:

* ``TestRestartMidPauseReEmit`` — AC-5: verbatim ``request_id`` + correlation on
  the re-emit; **arm-before-post** proven via an event log across the full boot
  sequence (the response subscription is live before ANY re-emit hits the wire);
  request-before-paused ordering.
* ``TestPostRestartApproveLaunches`` — AC-5: post-restart APPROVE resumes and
  launches via ``resume_launcher``.
* ``TestPostRestartRejectAndExpiry`` — AC-5: REJECT / window-expiry cancel.
* ``TestSpoofedMismatchStaleRefused`` — AC-6: spoofed responder / mismatched
  correlation / stale request_id → refused, zero transitions, zero emits, then
  the legit reply still lands.
* ``TestLegacyUnparseableSkipped`` — AC-5: a legacy / unparseable persisted
  ``request_id`` is skipped with an ERROR (never re-armed).
* ``TestCrashMidHopRedispatch`` — AC-8 (C2): a row left INTERRUPTED mid
  transition_chain is re-dispatched (never skip-without-ack; consumer not
  wedged).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli import _serve_deps_gating, _serve_gate_activation
from forge.cli._serve_deps import build_pipeline_consumer_deps
from forge.cli._serve_deps_gating import build_approval_gate_parts
from forge.cli._serve_gate_activation import maybe_gate_build, rearm_paused_gates
from forge.cli._serve_deps_lifecycle import build_publisher_and_emitter
from forge.config.models import ForgeConfig
from forge.gating.identity import derive_request_id
from forge.gating.sqlite_adapters import build_sqlite_gate_adapters
from forge.gating.wrappers import GateOutcome
from forge.lifecycle import migrations
from forge.lifecycle.identifiers import derive_build_id
from forge.lifecycle.persistence import Build, SqliteLifecyclePersistence
from forge.lifecycle.state_machine import (
    BuildState,
    transition as compose_transition,
    transition_chain,
)

from .conftest import InMemoryNats

FEATURE_ID = "FEAT-RGATE"
CORRELATION_ID = "corr-gate-restart-0001"
RICH = "rich"
STAGE = "autobuild"
QUEUED_AT = datetime(2026, 7, 5, 11, 0, 0, tzinfo=UTC)
FROZEN = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)


class FixedClock:
    """Frozen ``() -> datetime`` (UTC) — clock hygiene, never wall-clock."""

    def __init__(self, fixed: datetime = FROZEN) -> None:
        self._fixed = fixed

    def __call__(self) -> datetime:
        return self._fixed


# ---------------------------------------------------------------------------
# Event-log NATS double — flat cross-subject subscribe + publish order
# ---------------------------------------------------------------------------


class EventLogNats(InMemoryNats):
    """:class:`InMemoryNats` recording a flat ``("sub"|"pub", subject)`` log.

    Arm-before-post is proven across the WHOLE boot sequence, not just rearm's
    internals: the response-mirror ``sub`` MUST precede the approval-request
    ``pub`` in this log.
    """

    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple[str, str]] = []

    async def subscribe(self, subject: str, callback: Any) -> Any:
        self.events.append(("sub", subject))
        return await super().subscribe(subject, callback)

    async def publish(self, subject: str, body: bytes) -> None:
        self.events.append(("pub", subject))
        await super().publish(subject, body)

    def reset_wire(self) -> None:
        """Reset the recorded wire view (models a daemon restart, not a broker
        restart): clear published envelopes + the event log so post-restart
        emits are asserted in isolation. Subscriptions are left as-is (session
        1's frame already unsubscribed on its cancel)."""
        self.published.clear()
        self.events.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_payload(
    *,
    feature_id: str = FEATURE_ID,
    correlation_id: str = CORRELATION_ID,
    queued_at: datetime = QUEUED_AT,
) -> SimpleNamespace:
    return SimpleNamespace(
        feature_id=feature_id,
        repo="guardkit/forge",
        branch="main",
        feature_yaml_path="/srv/forge/features/test/test.yaml",
        max_turns=5,
        sdk_timeout_seconds=1800,
        triggered_by="cli",
        originating_adapter="forge-cli",
        originating_user="rich",
        correlation_id=correlation_id,
        parent_request_id=None,
        queued_at=queued_at,
        requested_at=queued_at,
    )


def _forge_config(**approval_overrides: Any) -> ForgeConfig:
    doc: dict[str, Any] = {
        "permissions": {"filesystem": {"allowlist": ["/srv/forge"]}},
    }
    if approval_overrides:
        doc["approval"] = approval_overrides
    return ForgeConfig.model_validate(doc)


def _request_subject(build_id: str) -> str:
    return f"agents.approval.forge.{build_id}"


def _mirror_subject(build_id: str) -> str:
    return f"agents.approval.forge.{build_id}.response"


def _paused_subject(feature_id: str = FEATURE_ID) -> str:
    return f"pipeline.build-paused.{feature_id}"


def _resumed_subject(feature_id: str = FEATURE_ID) -> str:
    return f"pipeline.build-resumed.{feature_id}"


def _cancelled_subject(feature_id: str = FEATURE_ID) -> str:
    return f"pipeline.build-cancelled.{feature_id}"


def _request_id(build_id: str, attempt: int = 0) -> str:
    return derive_request_id(
        build_id=build_id, stage_label=STAGE, attempt_count=attempt
    )


def _payloads(nats: InMemoryNats, subject: str) -> list[dict[str, Any]]:
    return [json.loads(b)["payload"] for b in nats.published.get(subject, [])]


def _envelopes(nats: InMemoryNats, subject: str) -> list[dict[str, Any]]:
    return [json.loads(b) for b in nats.published.get(subject, [])]


def _row(pool: SqliteLifecyclePersistence, build_id: str) -> tuple[str, str | None]:
    r = pool.connection.execute(
        "SELECT status, pending_approval_request_id FROM builds WHERE build_id = ?",
        (build_id,),
    ).fetchone()
    return (r["status"], r["pending_approval_request_id"])


async def _wait_until(cond, *, timeout: float = 5.0, what: str = "") -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while not cond():
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"condition never held within {timeout}s: {what}")
        await asyncio.sleep(0)


def _build_parts(
    nats: InMemoryNats,
    *,
    forge_config: ForgeConfig | None = None,
):
    cfg = forge_config or _forge_config()
    _publisher, emitter = build_publisher_and_emitter(nats)
    return build_approval_gate_parts(
        nats,
        cfg,
        emitter=emitter,
        repository=None,
        bridge_registry=None,
    )


class _FakeResumeLauncher:
    """Records ``resume_launcher`` invocations (dispatch-minus-record)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        *,
        build_id: str,
        feature_id: str,
        correlation_id: str | None,
        repo: str | None = None,
    ) -> None:
        # ``repo`` is the SECOND-REPO thread: the rearm sweep reads it off the
        # restored builds row and hands it to the launch, so a re-armed build
        # never falls through to the daemon's environment default.
        self.calls.append(
            {
                "build_id": build_id,
                "feature_id": feature_id,
                "correlation_id": correlation_id,
                "repo": repo,
            }
        )


async def _seed_paused_via_first_session(
    nats: EventLogNats,
    pool: SqliteLifecyclePersistence,
    *,
    forge_config: ForgeConfig | None = None,
    feature_id: str = FEATURE_ID,
    correlation_id: str = CORRELATION_ID,
) -> str:
    """Run the live gate to a genuine PAUSED row, then kill the frame.

    Returns the paused build_id. After this the daemon is "dead": the response
    subscriber has unsubscribed, but SQLite holds ``PAUSED`` + the persisted
    request_id + the gate decision snapshot.
    """
    build_id = pool.record_pending_build(
        _make_payload(feature_id=feature_id, correlation_id=correlation_id)
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
            feature_id=feature_id,
            correlation_id=correlation_id,
            clock=FixedClock(),
        )
    )
    # Wait until SQLite is PAUSED with the request id (pause committed) and the
    # session-1 subscriber is live, so the cancel lands mid-await.
    await _wait_until(
        lambda: _row(pool, build_id)[0] == BuildState.PAUSED.value,
        what="session-1 paused row",
    )
    await _wait_until(
        lambda: nats.subscribers.get(_mirror_subject(build_id)),
        what="session-1 subscriber live",
    )
    # Daemon dies mid-pause — the awaiting frame is destroyed.
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return build_id


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def pool(tmp_path: Path):
    db_path = tmp_path / "forge.db"
    cx = sqlite_connect.connect_writer(db_path)
    migrations.apply_at_boot(cx)
    p = SqliteLifecyclePersistence(connection=cx)
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


# ---------------------------------------------------------------------------
# AC-5 — restart mid-pause: verbatim re-emit + arm-before-post
# ---------------------------------------------------------------------------


class TestRestartMidPauseReEmit:
    @pytest.mark.asyncio
    async def test_rearm_reemits_verbatim_request_id_and_correlation_arm_before_post(
        self, nats: EventLogNats, pool: SqliteLifecyclePersistence
    ) -> None:
        build_id = await _seed_paused_via_first_session(nats, pool)
        persisted_request_id = _row(pool, build_id)[1]
        assert persisted_request_id == _request_id(build_id, 0)

        # SESSION 2 — fresh composition over the same pool + broker.
        nats.reset_wire()
        parts2 = _build_parts(nats)
        _serve_deps_gating.bind_gate_parts(parts2)
        repo2, sm2 = build_sqlite_gate_adapters(pool, clock=FixedClock())
        launcher = _FakeResumeLauncher()

        tasks = await rearm_paused_gates(
            parts=parts2,
            sqlite_pool=pool,
            gate_repository=repo2,
            gate_state_machine=sm2,
            resume_launcher=launcher,
            client=nats,
            clock=FixedClock(),
        )
        assert len(tasks) == 1

        # Arm-before-post: the response-mirror subscription is registered
        # BEFORE the approval-request re-emit reaches the wire (full boot log).
        sub_idx = next(
            i
            for i, (kind, subj) in enumerate(nats.events)
            if kind == "sub" and subj == _mirror_subject(build_id)
        )
        req_pub_idx = next(
            i
            for i, (kind, subj) in enumerate(nats.events)
            if kind == "pub" and subj == _request_subject(build_id)
        )
        paused_pub_idx = next(
            i
            for i, (kind, subj) in enumerate(nats.events)
            if kind == "pub" and subj == _paused_subject()
        )
        assert sub_idx < req_pub_idx, "response subscription MUST arm before re-emit"
        # request-before-paused preserves the jarvis button-join order.
        assert req_pub_idx < paused_pub_idx

        # Verbatim request_id + correlation on the re-emit.
        req_env = _envelopes(nats, _request_subject(build_id))[-1]
        assert req_env["payload"]["request_id"] == persisted_request_id
        assert req_env["correlation_id"] == CORRELATION_ID
        # Dual envelope: build-paused re-emitted too.
        assert len(_payloads(nats, _paused_subject())) == 1

        # Drive the operator's decision so the background task completes.
        await nats.deliver_response(
            build_id=build_id,
            request_id=persisted_request_id,
            decision="approve",
        )
        outcome = await asyncio.wait_for(tasks[0], timeout=5.0)
        assert outcome is GateOutcome.RESUMED


# ---------------------------------------------------------------------------
# AC-5 — post-restart approve resumes + launches via resume_launcher
# ---------------------------------------------------------------------------


class TestPostRestartApproveLaunches:
    @pytest.mark.asyncio
    async def test_post_restart_approve_launches_and_resumes(
        self, nats: EventLogNats, pool: SqliteLifecyclePersistence
    ) -> None:
        build_id = await _seed_paused_via_first_session(nats, pool)
        nats.reset_wire()
        parts2 = _build_parts(nats)
        _serve_deps_gating.bind_gate_parts(parts2)
        repo2, sm2 = build_sqlite_gate_adapters(pool, clock=FixedClock())
        launcher = _FakeResumeLauncher()

        tasks = await rearm_paused_gates(
            parts=parts2,
            sqlite_pool=pool,
            gate_repository=repo2,
            gate_state_machine=sm2,
            resume_launcher=launcher,
            client=nats,
            clock=FixedClock(),
        )
        await nats.deliver_response(
            build_id=build_id,
            request_id=_request_id(build_id, 0),
            decision="approve",
        )
        outcome = await asyncio.wait_for(tasks[0], timeout=5.0)

        assert outcome is GateOutcome.RESUMED
        # PAUSED → RUNNING (resume auto-clears the pending id).
        assert _row(pool, build_id) == (BuildState.RUNNING.value, None)
        # Launched via the injected resume_launcher (dispatch minus record).
        assert launcher.calls == [
            {
                "build_id": build_id,
                "feature_id": FEATURE_ID,
                "correlation_id": CORRELATION_ID,
                # Threaded from the restored builds row (_make_payload's repo),
                # NOT left None for FORGE_DEFAULT_REPO to fill in.
                "repo": "guardkit/forge",
            }
        ]
        # Exactly one build-resumed on the wire (real decision/responder).
        resumed = _payloads(nats, _resumed_subject())
        assert len(resumed) == 1
        assert resumed[0]["decision"] == "approve"
        assert resumed[0]["responder"] == RICH


# ---------------------------------------------------------------------------
# AC-5 — post-restart reject / expiry cancel
# ---------------------------------------------------------------------------


class TestPostRestartRejectAndExpiry:
    @pytest.mark.asyncio
    async def test_post_restart_reject_cancels_and_never_launches(
        self, nats: EventLogNats, pool: SqliteLifecyclePersistence
    ) -> None:
        build_id = await _seed_paused_via_first_session(nats, pool)
        nats.reset_wire()
        parts2 = _build_parts(nats)
        _serve_deps_gating.bind_gate_parts(parts2)
        repo2, sm2 = build_sqlite_gate_adapters(pool, clock=FixedClock())
        launcher = _FakeResumeLauncher()

        tasks = await rearm_paused_gates(
            parts=parts2,
            sqlite_pool=pool,
            gate_repository=repo2,
            gate_state_machine=sm2,
            resume_launcher=launcher,
            client=nats,
            clock=FixedClock(),
        )
        await nats.deliver_response(
            build_id=build_id,
            request_id=_request_id(build_id, 0),
            decision="reject",
            notes="not safe post-restart",
        )
        outcome = await asyncio.wait_for(tasks[0], timeout=5.0)

        assert outcome is GateOutcome.CANCELLED
        assert _row(pool, build_id)[0] == BuildState.CANCELLED.value
        assert launcher.calls == []
        assert len(_payloads(nats, _cancelled_subject())) == 1
        assert _resumed_subject() not in nats.published

    @pytest.mark.asyncio
    async def test_post_restart_window_expiry_cancels(
        self, nats: EventLogNats, pool: SqliteLifecyclePersistence
    ) -> None:
        # Zero per-attempt wait + no refresh → the re-armed window expires
        # immediately and the gate applies the max-wait cancel.
        cfg = _forge_config(
            default_wait_seconds=0, max_wait_seconds=3600, expected_approver=RICH
        )
        build_id = await _seed_paused_via_first_session(nats, pool, forge_config=cfg)
        nats.reset_wire()
        parts2 = _build_parts(nats, forge_config=cfg)
        _serve_deps_gating.bind_gate_parts(parts2)
        repo2, sm2 = build_sqlite_gate_adapters(pool, clock=FixedClock())
        launcher = _FakeResumeLauncher()

        tasks = await rearm_paused_gates(
            parts=parts2,
            sqlite_pool=pool,
            gate_repository=repo2,
            gate_state_machine=sm2,
            resume_launcher=launcher,
            client=nats,
            clock=FixedClock(),
        )
        outcome = await asyncio.wait_for(tasks[0], timeout=5.0)

        assert outcome is GateOutcome.TIMED_OUT
        assert _row(pool, build_id)[0] == BuildState.CANCELLED.value
        assert launcher.calls == []
        assert len(_payloads(nats, _cancelled_subject())) == 1


# ---------------------------------------------------------------------------
# AC-6 — spoofed / mismatched / stale replies refused (four-step chain)
# ---------------------------------------------------------------------------


class TestSpoofedMismatchStaleRefused:
    @pytest.mark.asyncio
    async def test_spoof_mismatch_stale_refused_then_legit_lands(
        self, nats: EventLogNats, pool: SqliteLifecyclePersistence
    ) -> None:
        from nats_core.envelope import EventType, MessageEnvelope

        build_id = await _seed_paused_via_first_session(nats, pool)
        nats.reset_wire()
        parts2 = _build_parts(nats)
        _serve_deps_gating.bind_gate_parts(parts2)
        repo2, sm2 = build_sqlite_gate_adapters(pool, clock=FixedClock())
        launcher = _FakeResumeLauncher()

        tasks = await rearm_paused_gates(
            parts=parts2,
            sqlite_pool=pool,
            gate_repository=repo2,
            gate_state_machine=sm2,
            resume_launcher=launcher,
            client=nats,
            clock=FixedClock(),
        )
        mirror = _mirror_subject(build_id)
        rid = _request_id(build_id, 0)

        # Step 2 — spoofed responder (wrong decided_by) → refused.
        await nats.deliver_response(
            build_id=build_id, request_id=rid, decision="approve", decided_by="mallory"
        )
        # Step 2b — mismatched correlation_id → refused (the guard runs BEFORE
        # dedup so it cannot poison the legit request_id).
        spoof_corr = MessageEnvelope(
            source_id="jarvis",
            event_type=EventType.APPROVAL_RESPONSE,
            correlation_id="a-different-build-context",
            payload={
                "request_id": rid,
                "decision": "approve",
                "decided_by": RICH,
                "notes": None,
            },
        )
        await nats.publish(mirror, spoof_corr.model_dump_json().encode())

        # Both refused, zero transitions, zero emits — still PAUSED + waiting.
        assert _row(pool, build_id)[0] == BuildState.PAUSED.value
        assert launcher.calls == []
        assert _resumed_subject() not in nats.published
        assert not tasks[0].done()

        # Step 4 — a DEFER consumes attempt-0's request_id and re-publishes
        # attempt-1; replaying the now-consumed attempt-0 id is the "stale
        # request_id" refusal (the 300s dedup buffer drops it).
        await nats.deliver_response(build_id=build_id, request_id=rid, decision="defer")
        await _wait_until(
            lambda: len(nats.published.get(_request_subject(build_id), [])) >= 2,
            what="defer republish (attempt 1)",
        )
        await _wait_until(
            lambda: nats.subscribers.get(mirror),
            what="fresh subscription after defer",
        )
        await nats.deliver_response(
            build_id=build_id, request_id=rid, decision="approve"
        )
        assert _row(pool, build_id)[0] == BuildState.PAUSED.value
        assert _resumed_subject() not in nats.published

        # The legit reply on the CURRENT (attempt-1) request_id lands.
        good = MessageEnvelope(
            source_id="jarvis",
            event_type=EventType.APPROVAL_RESPONSE,
            correlation_id=CORRELATION_ID,
            payload={
                "request_id": _request_id(build_id, 1),
                "decision": "approve",
                "decided_by": RICH,
                "notes": None,
            },
        )
        await nats.publish(mirror, good.model_dump_json().encode())
        outcome = await asyncio.wait_for(tasks[0], timeout=5.0)

        assert outcome is GateOutcome.RESUMED
        assert _row(pool, build_id)[0] == BuildState.RUNNING.value
        assert len(_payloads(nats, _resumed_subject())) == 1


# ---------------------------------------------------------------------------
# AC-5 — legacy / unparseable persisted request_id is skipped with ERROR
# ---------------------------------------------------------------------------


class TestLegacyUnparseableSkipped:
    @pytest.mark.asyncio
    async def test_unparseable_request_id_skipped_with_error(
        self,
        nats: EventLogNats,
        pool: SqliteLifecyclePersistence,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Seed a PAUSED row whose pending_approval_request_id is a legacy /
        # corrupt value (no ':' separators — parse_request_id rejects it).
        build_id = pool.record_pending_build(_make_payload())
        for frm, to in (
            (BuildState.QUEUED, BuildState.PREPARING),
            (BuildState.PREPARING, BuildState.RUNNING),
        ):
            pool.apply_transition(
                compose_transition(Build(build_id=build_id, status=frm), to)
            )
        pool.mark_paused(build_id, "legacy-uuid-no-colons")

        parts = _build_parts(nats)
        _serve_deps_gating.bind_gate_parts(parts)
        repo, sm = build_sqlite_gate_adapters(pool, clock=FixedClock())
        launcher = _FakeResumeLauncher()

        with caplog.at_level(logging.ERROR):
            tasks = await rearm_paused_gates(
                parts=parts,
                sqlite_pool=pool,
                gate_repository=repo,
                gate_state_machine=sm,
                resume_launcher=launcher,
                client=nats,
                clock=FixedClock(),
            )

        # Skipped: no task re-armed, nothing re-emitted, row untouched.
        assert tasks == []
        assert _request_subject(build_id) not in nats.published
        assert _paused_subject() not in nats.published
        assert _row(pool, build_id)[0] == BuildState.PAUSED.value
        assert any(
            "unparseable request_id" in rec.getMessage()
            and rec.levelno >= logging.ERROR
            for rec in caplog.records
        )


# ---------------------------------------------------------------------------
# AC-8 (C2) — crash-mid-hop INTERRUPTED row is re-dispatched, not wedged
# ---------------------------------------------------------------------------


class _FakeStarter:
    """Records autobuild launches; satisfies ``AsyncTaskStarter``."""

    def __init__(self) -> None:
        self.launches: list[dict[str, Any]] = []

    def start_async_task(self, subagent_name: str, context) -> str:  # noqa: D401
        self.launches.append(dict(context))
        return "task-sync"

    async def astart_async_task(self, *, subagent_name: str, context) -> str:
        self.launches.append(dict(context))
        return "task-async"


async def _drive_response(
    nats: InMemoryNats, *, build_id: str, request_id: str, decision: str
) -> None:
    await _wait_until(
        lambda: nats.subscribers.get(_mirror_subject(build_id)),
        what=f"subscriber on {_mirror_subject(build_id)}",
    )
    await nats.deliver_response(
        build_id=build_id, request_id=request_id, decision=decision
    )


class TestCrashMidHopRedispatch:
    @pytest.mark.asyncio
    async def test_interrupted_row_redispatched_and_not_wedged(
        self, nats: EventLogNats, pool: SqliteLifecyclePersistence
    ) -> None:
        # Seed an INTERRUPTED row (crash inside the QUEUED→…→PAUSED hop window,
        # then recovery marked it INTERRUPTED) for the same identity.
        payload = _make_payload()
        build_id = pool.record_pending_build(payload)
        for frm, to in (
            (BuildState.QUEUED, BuildState.PREPARING),
            (BuildState.PREPARING, BuildState.RUNNING),
            (BuildState.RUNNING, BuildState.INTERRUPTED),
        ):
            pool.apply_transition(
                compose_transition(Build(build_id=build_id, status=frm), to)
            )
        assert _row(pool, build_id)[0] == BuildState.INTERRUPTED.value

        starter = _FakeStarter()
        parts = _build_parts(nats)
        _serve_deps_gating.bind_gate_parts(parts)
        repo, sm = build_sqlite_gate_adapters(pool, clock=FixedClock())
        deps = build_pipeline_consumer_deps(
            nats,
            _forge_config(),
            pool,
            async_task_starter=starter,
            gate_repository=repo,
            gate_state_machine=sm,
            gate_clock=FixedClock(),
        )

        acked: list[bool] = []

        async def _ack() -> None:
            acked.append(True)

        # A redelivered build-queued for the INTERRUPTED row: record_pending_build
        # raises DuplicateBuildError → THIRD ARM re-dispatches into the gate flow
        # (never skip-without-ack). The build re-enters the lifecycle and pauses.
        task = asyncio.create_task(deps.dispatch_build(payload, _ack))
        await _drive_response(
            nats,
            build_id=build_id,
            request_id=_request_id(build_id, 0),
            decision="approve",
        )
        await asyncio.wait_for(task, timeout=5.0)

        # Re-dispatched (not wedged): the gate ran, approve launched the build.
        assert len(starter.launches) == 1
        assert starter.launches[0]["build_id"] == build_id
        assert _row(pool, build_id)[0] == BuildState.RUNNING.value
        # Approve is not terminal — the held slot was not acked by dispatch.
        assert acked == []


# ---------------------------------------------------------------------------
# Robustness (Phase-5 FIX 1) — a per-build subscribe raising must NOT wedge
# the whole boot sweep. The arm-wait is bounded; the failing build is skipped
# (no post into a dead subscription) and the OTHER PAUSED build still re-arms.
# ---------------------------------------------------------------------------


class _SubscribeFailsForBuild(EventLogNats):
    """:class:`EventLogNats` whose ``subscribe`` raises for one build's mirror.

    Models a transient broker error / closed conn on the per-build response
    subscription. Every other subject subscribes normally, so the sweep's OTHER
    PAUSED builds arm and re-emit as usual.
    """

    def __init__(self, failing_build_id: str) -> None:
        super().__init__()
        self._failing_mirror = _mirror_subject(failing_build_id)

    async def subscribe(self, subject: str, callback: Any) -> Any:
        if subject == self._failing_mirror:
            raise RuntimeError("transient broker error: subscribe failed")
        return await super().subscribe(subject, callback)


class TestRearmSubscribeFailureDoesNotWedgeSweep:
    @pytest.mark.asyncio
    async def test_bad_subscribe_skips_one_build_and_rearms_the_other(
        self,
        pool: SqliteLifecyclePersistence,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # SESSION 1 — seed TWO genuine PAUSED rows over a normal broker.
        seed_nats = EventLogNats()
        bad_build = await _seed_paused_via_first_session(
            seed_nats, pool, feature_id="FEAT-BAD", correlation_id="corr-bad-0001"
        )
        good_build = await _seed_paused_via_first_session(
            seed_nats, pool, feature_id="FEAT-GOOD", correlation_id="corr-good-0001"
        )

        # SESSION 2 — fresh composition over a broker whose subscribe raises for
        # the bad build's response mirror. Shrink the arm timeout so the sweep's
        # bounded wait fires fast (default 10s would make the test slow); the
        # outer 2s wait_for then PROVES rearm_paused_gates does not hang.
        monkeypatch.setattr(_serve_gate_activation, "_REARM_ARM_TIMEOUT_SECONDS", 0.2)
        nats = _SubscribeFailsForBuild(bad_build)
        parts2 = _build_parts(nats)
        _serve_deps_gating.bind_gate_parts(parts2)
        repo2, sm2 = build_sqlite_gate_adapters(pool, clock=FixedClock())
        launcher = _FakeResumeLauncher()

        with caplog.at_level(logging.ERROR):
            # Non-hang: without the bounded wait this awaits ``armed`` FOREVER
            # for the bad build, so the outer timeout would trip.
            tasks = await asyncio.wait_for(
                rearm_paused_gates(
                    parts=parts2,
                    sqlite_pool=pool,
                    gate_repository=repo2,
                    gate_state_machine=sm2,
                    resume_launcher=launcher,
                    client=nats,
                    clock=FixedClock(),
                ),
                timeout=2.0,
            )

        # The OTHER (good) build still re-armed: exactly one task returned, its
        # request + build-paused re-emitted.
        assert len(tasks) == 1
        assert _request_subject(good_build) in nats.published
        assert _paused_subject("FEAT-GOOD") in nats.published

        # The failing build was skipped: NO approval-request re-emit hit the
        # wire (never post into a dead subscription).
        assert _request_subject(bad_build) not in nats.published
        assert _paused_subject("FEAT-BAD") not in nats.published

        # The skip is logged at ERROR (arm-wait timeout for the bad build).
        assert any(
            "did not arm" in rec.getMessage()
            and bad_build in rec.getMessage()
            and rec.levelno >= logging.ERROR
            for rec in caplog.records
        )

        # Cleanup: the good build's task is still awaiting a response.
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

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

from nats_core.envelope import EventType, MessageEnvelope

from forge.adapters.nats.pipeline_consumer import (
    ReconcileDeps,
    reconcile_on_boot as consumer_reconcile_on_boot,
)
from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli import _serve_deps, _serve_deps_gating, _serve_gate_activation
from forge.cli._conductor_worktree import WorktreeReady
from forge.cli._serve_deps import (
    build_pipeline_consumer_deps,
    build_serve_resume_launcher,
)
from forge.cli.serve import build_conductor_router
from forge.lifecycle.recovery import (
    reconcile_on_boot as lifecycle_reconcile_on_boot,
)
from forge.cli._serve_deps_gating import build_approval_gate_parts
from forge.cli._serve_gate_activation import maybe_gate_build, rearm_paused_gates
from forge.cli._serve_deps_lifecycle import build_publisher_and_emitter
from forge.config.models import ForgeConfig
from forge.gating.degraded import EmptyPriorsReader
from forge.gating.identity import derive_request_id
from forge.gating.sqlite_adapters import build_sqlite_gate_adapters
from forge.gating.wrappers import GateOutcome
from forge.lifecycle import migrations
from forge.lifecycle.identifiers import derive_build_id
from forge.lifecycle.modes import BuildMode
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
    mode: Any = None,
    task_id: str | None = None,
    profile: str | None = None,
) -> SimpleNamespace:
    payload = SimpleNamespace(
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
    # ``mode`` / ``task_id`` ride the wire only for a fix journey; leaving
    # them OFF the namespace (rather than None) keeps every pre-existing
    # scenario's payload byte-identical to what it was.
    if mode is not None:
        payload.mode = mode
    if task_id is not None:
        payload.task_id = task_id
    # ``profile`` is sniffed off the payload by ``record_pending_build`` and
    # lands on ``builds.profile`` — the column THE CAP LAW reads before a fix
    # journey may open. Off the namespace unless asked for, so every
    # pre-existing scenario's row is byte-identical to what it was.
    if profile is not None:
        payload.profile = profile
    return payload


def _forge_config(
    *,
    autobuild_gate_max_wait_seconds: int | None = None,
    **approval_overrides: Any,
) -> ForgeConfig:
    doc: dict[str, Any] = {
        "permissions": {"filesystem": {"allowlist": ["/srv/forge"]}},
    }
    if approval_overrides:
        doc["approval"] = approval_overrides
    if autobuild_gate_max_wait_seconds is not None:
        # 2026-08-26: the build gate waits indefinitely by default; expiry
        # scenarios opt back into a hard ceiling through this knob.
        doc["autobuild_gate"] = {
            "approval_max_wait_seconds": autobuild_gate_max_wait_seconds
        }
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
        priors_reader=EmptyPriorsReader(),
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
    mode: Any = None,
    task_id: str | None = None,
    profile: str | None = None,
) -> str:
    """Run the live gate to a genuine PAUSED row, then kill the frame.

    Returns the paused build_id. After this the daemon is "dead": the response
    subscriber has unsubscribed, but SQLite holds ``PAUSED`` + the persisted
    request_id + the gate decision snapshot.
    """
    build_id = pool.record_pending_build(
        _make_payload(
            feature_id=feature_id,
            correlation_id=correlation_id,
            mode=mode,
            task_id=task_id,
            profile=profile,
        )
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
            default_wait_seconds=0,
            max_wait_seconds=3600,
            expected_approver=RICH,
            # Opt back into a bounded wait — the 2026-08-26 default is
            # wait-forever, under which this scenario would never expire.
            autobuild_gate_max_wait_seconds=3600,
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


# ---------------------------------------------------------------------------
# Activation design §4.2 — SILENT-DOWNGRADE SEAM 2: the boot rearm sweep's
# approve path uses the ROUTINE resume launcher and consults no router.
# ---------------------------------------------------------------------------


class _RearmGuardStarter:
    """Stands in for the Supervisor's AsyncSubAgentMiddleware starter.

    Named apart from the module's earlier ``_FakeStarter`` on purpose —
    that one records ``launches`` for the redispatch scenarios; this one
    only needs to exist so the REAL resume launcher composes.
    """

    def __init__(self) -> None:
        self.started: list[tuple[str, dict[str, Any]]] = []

    def start_async_task(self, subagent_name: str, context: dict) -> str:
        self.started.append((subagent_name, context))
        return "task-rearm"

    async def astart_async_task(self, subagent_name: str, context: dict) -> str:
        return self.start_async_task(subagent_name, context)


class TestRearmNeverResumesAFixJourneyRoutine:
    """A mode-c row carded, restarted, then approved must NOT run routine.

    The sweep's approve path has no router of its own — it holds only the
    resume launcher — so before the guard a fix journey that met a daemon
    restart came back as a ROUTINE autobuild driven against a TASK-xxx
    subject: the wrong machinery, silently.

    **2026-09-05 (the conductor rewire, rule 5).** The refusal this class
    used to pin end-to-end is no longer the production answer. The daemon
    now threads its composed conductor router into
    ``build_serve_resume_launcher``, so a re-approved fix journey is
    CONDUCTED — see :class:`TestFixJourneyReEntryDoors` for that half.
    What survives here is the guard's floor: with NO router (the
    conductor switched off, the only configuration in which the boot
    sweep can meet a mode-c row with nothing to hand it to) the row is
    still refused loudly rather than routine-launched, and a routine
    build on the very same launcher still launches (the mutation guard —
    a guard that refused everything would pass the first half and brick
    the whole rearm path in production).
    """

    async def _rearm_and_approve(
        self,
        nats: EventLogNats,
        pool: SqliteLifecyclePersistence,
        build_id: str,
        launcher: Any,
    ) -> Any:
        nats.reset_wire()
        parts2 = _build_parts(nats)
        _serve_deps_gating.bind_gate_parts(parts2)
        repo2, sm2 = build_sqlite_gate_adapters(pool, clock=FixedClock())
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
        return await asyncio.wait_for(tasks[0], timeout=5.0)

    def _real_launcher(
        self,
        nats: EventLogNats,
        pool: SqliteLifecyclePersistence,
        *,
        conductor_router: Any = None,
    ) -> Any:
        _publisher, emitter = build_publisher_and_emitter(nats)
        return build_serve_resume_launcher(
            pool,
            _forge_config(),
            lifecycle_emitter=emitter,
            async_task_starter=_RearmGuardStarter(),
            conductor_router=conductor_router,
        )

    @pytest.mark.asyncio
    async def test_a_fix_journey_with_no_conductor_is_refused_not_launched(
        self,
        nats: EventLogNats,
        pool: SqliteLifecyclePersistence,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        launched: list[dict[str, Any]] = []

        async def _recording_dispatch(**kwargs: Any) -> Any:
            launched.append(kwargs)
            return None

        monkeypatch.setattr(
            _serve_deps, "dispatch_autobuild_async", _recording_dispatch
        )

        build_id = await _seed_paused_via_first_session(
            nats,
            pool,
            feature_id="FEAT-REARMC",
            mode=BuildMode.MODE_C,
            task_id="TASK-REARMC",
        )
        assert pool.get_build_row(build_id).mode is BuildMode.MODE_C

        outcome = await self._rearm_and_approve(
            nats, pool, build_id, self._real_launcher(nats, pool)
        )

        assert outcome is GateOutcome.RESUMED  # the gate itself is untouched
        assert launched == [], (
            "an approved fix journey was resumed as a ROUTINE autobuild — "
            "the boot-rearm silent downgrade (design §4.2)"
        )
        row = pool.get_build_row(build_id)
        assert row is not None
        assert row.status is BuildState.FAILED
        assert row.error is not None and "\n" not in row.error
        assert "mode-c" in row.error
        # The terminal reaches the wire, so the still-held build-queued
        # message's redelivery finds a terminal row and self-heals the ack.
        failed = _payloads(nats, "pipeline.build-failed.FEAT-REARMC")
        assert len(failed) == 1
        assert failed[0]["recoverable"] is False
        assert failed[0]["failed_task_id"] == "TASK-REARMC"

    @pytest.mark.asyncio
    async def test_an_approved_routine_build_still_resumes_and_launches(
        self,
        nats: EventLogNats,
        pool: SqliteLifecyclePersistence,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The mutation guard: the guard must only bite mode-c."""
        launched: list[dict[str, Any]] = []

        async def _recording_dispatch(**kwargs: Any) -> Any:
            launched.append(kwargs)
            return None

        monkeypatch.setattr(
            _serve_deps, "dispatch_autobuild_async", _recording_dispatch
        )

        build_id = await _seed_paused_via_first_session(
            nats, pool, feature_id="FEAT-REARMA"
        )

        outcome = await self._rearm_and_approve(
            nats, pool, build_id, self._real_launcher(nats, pool)
        )

        assert outcome is GateOutcome.RESUMED
        assert len(launched) == 1
        assert launched[0]["build_id"] == build_id
        # SECOND-REPO LAW: the repo still rides the resumed launch.
        assert launched[0]["repo"] == "guardkit/forge"
        assert _row(pool, build_id) == (BuildState.RUNNING.value, None)
        assert _payloads(nats, "pipeline.build-failed.FEAT-REARMA") == []


# ---------------------------------------------------------------------------
# 2026-08-26 — the re-armed gate wait is indefinite by default
# ---------------------------------------------------------------------------


class TestPostRestartDefaultWaitIsIndefinite:
    """By default (knob absent) a re-armed gate wait never expires.

    Exactly the setup that used to expire in a single zero-length window
    (default_wait_seconds=0, no refresh publisher on the rearm path) — but
    with the build-gate wait knob left at its default, the re-armed wait
    keeps waiting and the late approve still resumes and launches.
    """

    @pytest.mark.asyncio
    async def test_rearmed_wait_survives_and_late_approve_lands(
        self,
        nats: EventLogNats,
        pool: SqliteLifecyclePersistence,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from forge.adapters.nats import approval_subscriber as sub_module

        monkeypatch.setattr(sub_module, "UNBOUNDED_IDLE_WAIT_SECONDS", 0.02)
        cfg = _forge_config(default_wait_seconds=0, expected_approver=RICH)
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
        assert len(tasks) == 1

        # Several zero-length windows elapse; the old behaviour had already
        # returned None here (TIMED_OUT + CANCELLED). The indefinite default
        # keeps the wait alive.
        await asyncio.sleep(0.2)
        assert not tasks[0].done(), "re-armed gate wait must not expire by default"

        await nats.deliver_response(
            build_id=build_id,
            request_id=_request_id(build_id, 0),
            decision="approve",
        )
        outcome = await asyncio.wait_for(tasks[0], timeout=5.0)
        assert outcome is GateOutcome.RESUMED
        assert len(launcher.calls) == 1
        assert launcher.calls[0]["build_id"] == build_id


# ---------------------------------------------------------------------------
# 2026-09-05 — the fix journey's three doors back in (conductor rewire rule 5)
#
# A fix journey that meets a daemon restart comes back through exactly one of
# three doors, decided by the state its ``builds`` row was left in:
#
#   PAUSED       — it was sitting on Rich's build-gate card. The boot rearm
#                  sweep re-cards it; his approval reaches the resume
#                  launcher, which now holds the conductor's router.
#   INTERRUPTED  — the previous process died while it was driving. The
#                  redelivered build-queued message lands on reconcile's
#                  named INTERRUPTED branch, which hands it to dispatch: one
#                  fresh card, then the router.
#   RUNNING      — the same thing one step earlier. The boot recovery pass
#                  (``forge.lifecycle.recovery``) moves the row to
#                  INTERRUPTED and stops, so the row walks through the
#                  INTERRUPTED door behind it.
#
# Every door must produce the SAME two things and no more: ONE new card for
# Rich, and ONE working tree — the journey's own, REUSED, never a second one.
# Retry-from-scratch means the turns start again from turn one; the tree is
# not thrown away (ADR-ARCH-028, 2026-09-05 amendment).
# ---------------------------------------------------------------------------


REENTRY_TASK_ID = "TASK-REENTRY1"


class _RecordingFailurePublisher:
    """Duck-typed ``PipelineFailurePublisher`` for the boot recovery pass."""

    def __init__(self) -> None:
        self.published: list[Any] = []

    async def publish_build_failed(self, payload: Any) -> None:
        self.published.append(payload)


class _RecordingApprovalPublisher:
    """Duck-typed ``ApprovalRepublisher`` for the boot recovery pass."""

    def __init__(self) -> None:
        self.published: list[Any] = []

    async def publish_request(self, envelope: Any) -> None:
        self.published.append(envelope)


def _conductor_config() -> ForgeConfig:
    """``_forge_config()`` with the conductor switched ON and a seat named.

    ``conductor.enabled: true`` with no seat refuses at config load, so the
    seat is named here exactly as the deployed ``forge.yaml`` names one.
    """
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/srv/forge"]}},
            "conductor": {"enabled": True, "seat": "qwen3-coder-30b"},
        }
    )


class _ReusedWorktreeWriter:
    """A worktree writer that answers "your own tree, reused" and records.

    The REAL writer's reuse arm — the one that recognises the journey's own
    path AND branch and answers ``reused=True`` rather than materialising a
    second tree — is driven against a scratch git checkout in
    ``tests/forge/test_conductor_worktree.py``. These are RE-ENTRY tests, so
    the writer is injected: what they pin is that each door asks for the tree
    exactly once and gets a reused one.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, _pool: Any, _config: Any, build_id: str) -> Any:
        self.calls.append(build_id)
        return WorktreeReady(
            path=f"/srv/forge/.forge/worktrees/{build_id}",
            branch=f"fix/{REENTRY_TASK_ID}-{build_id[-8:]}",
            reused=True,
        )


class _ReEntryConductor:
    """The real router over recording collaborators.

    Built with :func:`forge.cli.serve.build_conductor_router` — the same
    factory the daemon composes — so the cap law, the worktree writer seam
    and the taken-and-terminal vocabulary are all the production ones. Only
    the supervisor, the turn-loop spawn and the worktree writer are stood in
    for (a real turn loop would run a model).
    """

    def __init__(self, pool: SqliteLifecyclePersistence) -> None:
        self.worktrees = _ReusedWorktreeWriter()
        self.spawned: list[Any] = []
        self.supervisors: list[str] = []
        self.router = build_conductor_router(
            pool=pool,
            config=_conductor_config(),
            supervisor_factory=self._make_supervisor,
            spawn=self._spawn,
            worktree_writer=self.worktrees,
        )
        assert self.router is not None, "the conductor composed as None"

    def _make_supervisor(self, build_id: str) -> Any:
        self.supervisors.append(build_id)
        return object()

    def _spawn(self, coro: Any) -> Any:
        self.spawned.append(coro)
        coro.close()  # the turn loop is not driven here; nothing is left awaiting
        return None


def _queued_envelope_bytes(
    *,
    feature_id: str,
    correlation_id: str,
    task_id: str = REENTRY_TASK_ID,
    mode: str = "mode-c",
) -> bytes:
    """One real ``build-queued`` envelope for the redelivered message.

    Identity (``feature_id`` + ``queued_at``) is the same the row was seeded
    with, so ``derive_build_id`` lands on the SAME build_id — which is what
    makes this a REDELIVERY rather than a new build.
    """
    payload = {
        "feature_id": feature_id,
        "repo": "guardkit/forge",
        "branch": "main",
        "feature_yaml_path": "/srv/forge/features/test/test.yaml",
        "max_turns": 5,
        "sdk_timeout_seconds": 1800,
        "triggered_by": "cli",
        "originating_adapter": "cli-wrapper",
        "originating_user": "rich",
        "correlation_id": correlation_id,
        "requested_at": QUEUED_AT.isoformat(),
        "queued_at": QUEUED_AT.isoformat(),
    }
    if mode == "mode-c":
        # ``task_id`` is required on the wire iff the build is mode-c.
        payload["mode"] = mode
        payload["task_id"] = task_id
    envelope = MessageEnvelope(
        source_id="cli-wrapper",
        event_type=EventType.BUILD_QUEUED,
        correlation_id=correlation_id,
        payload=payload,
    )
    return envelope.model_dump_json().encode("utf-8")


def _redelivery_msg(data: bytes) -> Any:
    """A stand-in for ``nats.aio.msg.Msg`` — ``.data`` plus an awaitable ack."""

    class _Msg:
        def __init__(self) -> None:
            self.data = data
            self.acks = 0

        async def ack(self) -> None:
            self.acks += 1

    return _Msg()


def _reconcile_deps(
    consumer_deps: Any,
    msg: Any,
    pool: SqliteLifecyclePersistence,
    *,
    resets: list[tuple[str, str]],
) -> ReconcileDeps:
    """``ReconcileDeps`` around ONE redelivery and the REAL consumer deps.

    ``read_build_state`` reads the real ``builds`` row, so the branch the
    reconcile takes is decided by the database and not by a fixture.

    The PAUSED collaborators are the production no-ops: inside ``forge
    serve`` the rearm sweep owns every PAUSED re-emit and this seam
    suppresses its own (``_serve_production._build_consumer_reconcile_seam``).
    ``mark_interrupted_and_reset`` only RECORDS: the INTERRUPTED branch must
    not call it (a reset to PREPARING would strand the redelivery on
    dispatch's held-slot arm — no card, no journey).
    """
    batches: list[list[Any]] = [[msg]]

    async def _fetch() -> list[Any]:
        return batches.pop(0) if batches else []

    async def _read_state(feature_id: str, correlation_id: str) -> str | None:
        row = pool.connection.execute(
            "SELECT status FROM builds WHERE feature_id = ? "
            "AND correlation_id = ?",
            (feature_id, correlation_id),
        ).fetchone()
        return None if row is None else row["status"]

    async def _mark(feature_id: str, correlation_id: str) -> None:
        resets.append((feature_id, correlation_id))

    async def _no_paused() -> list[Any]:
        return []

    async def _noop_paused(_payload: Any) -> None:
        return None

    async def _noop_request(_payload: Any, _subject: str) -> None:
        return None

    return ReconcileDeps(
        consumer_deps=consumer_deps,
        fetch_redeliveries=_fetch,
        read_build_state=_read_state,
        mark_interrupted_and_reset=_mark,
        iter_paused_builds=_no_paused,
        publish_build_paused=_noop_paused,
        publish_approval_request=_noop_request,
    )


async def _seed_mode_c_row(
    pool: SqliteLifecyclePersistence,
    *,
    feature_id: str,
    correlation_id: str,
    target: BuildState,
) -> str:
    """Insert a mode-c row and walk it to ``target`` through the state machine."""
    build_id = pool.record_pending_build(
        _make_payload(
            feature_id=feature_id,
            correlation_id=correlation_id,
            mode=BuildMode.MODE_C,
            task_id=REENTRY_TASK_ID,
            # THE CAP LAW reads this column before the journey may open.
            profile="fix-journey",
        )
    )
    hops = {
        BuildState.RUNNING: (
            (BuildState.QUEUED, BuildState.PREPARING),
            (BuildState.PREPARING, BuildState.RUNNING),
        ),
        BuildState.INTERRUPTED: (
            (BuildState.QUEUED, BuildState.PREPARING),
            (BuildState.PREPARING, BuildState.RUNNING),
            (BuildState.RUNNING, BuildState.INTERRUPTED),
        ),
    }[target]
    for frm, to in hops:
        pool.apply_transition(
            compose_transition(Build(build_id=build_id, status=frm), to)
        )
    assert _row(pool, build_id)[0] == target.value
    return build_id


class TestFixJourneyReEntryDoors:
    """Each door: exactly ONE new card and ONE reused working tree.

    The routine autobuild launcher is recorded in every scenario and must
    stay empty — a fix journey that comes back as a routine build against a
    TASK-xxx subject is the silent downgrade the whole seam exists to stop.
    """

    def _record_routine_launches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> list[dict[str, Any]]:
        launched: list[dict[str, Any]] = []

        async def _recording_dispatch(**kwargs: Any) -> Any:
            launched.append(kwargs)
            return None

        monkeypatch.setattr(
            _serve_deps, "dispatch_autobuild_async", _recording_dispatch
        )
        return launched

    def _cards(self, nats: EventLogNats, build_id: str) -> list[dict[str, Any]]:
        """The approval requests published for this build — Rich's cards."""
        return _payloads(nats, _request_subject(build_id))

    async def _drive_the_redelivery_door(
        self,
        nats: EventLogNats,
        pool: SqliteLifecyclePersistence,
        *,
        build_id: str,
        feature_id: str,
        correlation_id: str,
        conductor: _ReEntryConductor,
        mode: str = "mode-c",
    ) -> tuple[Any, list[tuple[str, str]]]:
        """Boot the consumer seam over ONE redelivery of ``build_id``."""
        parts = _build_parts(nats)
        _serve_deps_gating.bind_gate_parts(parts)
        repo, sm = build_sqlite_gate_adapters(pool, clock=FixedClock())
        consumer_deps = build_pipeline_consumer_deps(
            nats,
            _conductor_config(),
            pool,
            async_task_starter=_FakeStarter(),
            gate_repository=repo,
            gate_state_machine=sm,
            gate_clock=FixedClock(),
            conductor_router=conductor.router,
        )
        msg = _redelivery_msg(
            _queued_envelope_bytes(
                feature_id=feature_id,
                correlation_id=correlation_id,
                mode=mode,
            )
        )
        resets: list[tuple[str, str]] = []
        deps = _reconcile_deps(consumer_deps, msg, pool, resets=resets)

        task = asyncio.create_task(consumer_reconcile_on_boot(deps))
        # Rich taps approve on the card the gate just posted.
        await _drive_response(
            nats,
            build_id=build_id,
            request_id=_request_id(build_id, 0),
            decision="approve",
        )
        report = await asyncio.wait_for(task, timeout=5.0)
        return report, resets

    # -- door 1: PAUSED — the boot rearm sweep ---------------------------

    @pytest.mark.asyncio
    async def test_paused_journey_is_re_carded_and_conducted_on_approval(
        self,
        nats: EventLogNats,
        pool: SqliteLifecyclePersistence,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        launched = self._record_routine_launches(monkeypatch)
        build_id = await _seed_paused_via_first_session(
            nats,
            pool,
            feature_id="FEAT-DOORP",
            correlation_id="corr-door-paused",
            mode=BuildMode.MODE_C,
            task_id=REENTRY_TASK_ID,
            profile="fix-journey",
        )
        conductor = _ReEntryConductor(pool)

        # SESSION 2 — the sweep re-cards, Rich approves, the launcher routes.
        nats.reset_wire()
        parts2 = _build_parts(nats)
        _serve_deps_gating.bind_gate_parts(parts2)
        repo2, sm2 = build_sqlite_gate_adapters(pool, clock=FixedClock())
        _publisher, emitter = build_publisher_and_emitter(nats)
        launcher = build_serve_resume_launcher(
            pool,
            _conductor_config(),
            lifecycle_emitter=emitter,
            async_task_starter=_RearmGuardStarter(),
            conductor_router=conductor.router,
        )
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
        await nats.deliver_response(
            build_id=build_id,
            request_id=_request_id(build_id, 0),
            decision="approve",
        )
        outcome = await asyncio.wait_for(tasks[0], timeout=5.0)

        assert outcome is GateOutcome.RESUMED
        assert len(self._cards(nats, build_id)) == 1, "exactly one new card"
        assert conductor.worktrees.calls == [build_id], "one working tree"
        assert len(conductor.spawned) == 1, "one turn loop"
        assert launched == [], "the journey came back as a ROUTINE autobuild"
        row = pool.get_build_row(build_id)
        assert row is not None and row.status is not BuildState.FAILED
        assert _payloads(nats, "pipeline.build-failed.FEAT-DOORP") == []

    # -- door 2: INTERRUPTED — reconcile's named branch -------------------

    @pytest.mark.asyncio
    async def test_interrupted_journey_is_re_carded_and_reuses_its_worktree(
        self,
        nats: EventLogNats,
        pool: SqliteLifecyclePersistence,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        launched = self._record_routine_launches(monkeypatch)
        feature_id, correlation_id = "FEAT-DOORI", "corr-door-interrupted"
        build_id = await _seed_mode_c_row(
            pool,
            feature_id=feature_id,
            correlation_id=correlation_id,
            target=BuildState.INTERRUPTED,
        )
        conductor = _ReEntryConductor(pool)

        report, resets = await self._drive_the_redelivery_door(
            nats,
            pool,
            build_id=build_id,
            feature_id=feature_id,
            correlation_id=correlation_id,
            conductor=conductor,
        )

        # The NAMED branch ran — not the "unexpected state" fall-through,
        # which would have counted this as a fresh build.
        assert report.restarted_interrupted == 1
        assert report.fresh_builds == 0
        assert resets == [], (
            "the INTERRUPTED branch reset the row to PREPARING — dispatch's "
            "held-slot arm then owns it and no card is ever posted"
        )
        assert len(self._cards(nats, build_id)) == 1, "exactly one new card"
        assert conductor.worktrees.calls == [build_id], "one working tree"
        assert len(conductor.spawned) == 1, "one turn loop"
        assert launched == [], "the journey came back as a ROUTINE autobuild"
        row = pool.get_build_row(build_id)
        assert row is not None and row.status is not BuildState.FAILED

    # -- door 3: RUNNING — the boot recovery pass, then door 2 -----------

    @pytest.mark.asyncio
    async def test_running_journey_walks_through_the_interrupted_door(
        self,
        nats: EventLogNats,
        pool: SqliteLifecyclePersistence,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        launched = self._record_routine_launches(monkeypatch)
        feature_id, correlation_id = "FEAT-DOORR", "corr-door-running"
        build_id = await _seed_mode_c_row(
            pool,
            feature_id=feature_id,
            correlation_id=correlation_id,
            target=BuildState.RUNNING,
        )

        # The REAL boot recovery pass — the first thing the daemon runs. A
        # RUNNING row becomes INTERRUPTED and stops there; nothing is
        # re-emitted for it, which is why the redelivery below is its only
        # way back in.
        recovery_report = await lifecycle_reconcile_on_boot(
            pool, _RecordingFailurePublisher(), _RecordingApprovalPublisher()
        )
        assert recovery_report.interrupted_count == 1
        assert _row(pool, build_id)[0] == BuildState.INTERRUPTED.value

        conductor = _ReEntryConductor(pool)
        report, resets = await self._drive_the_redelivery_door(
            nats,
            pool,
            build_id=build_id,
            feature_id=feature_id,
            correlation_id=correlation_id,
            conductor=conductor,
        )

        assert report.restarted_interrupted == 1
        assert resets == []
        assert len(self._cards(nats, build_id)) == 1, "exactly one new card"
        assert conductor.worktrees.calls == [build_id], "one working tree"
        assert len(conductor.spawned) == 1, "one turn loop"
        assert launched == [], "the journey came back as a ROUTINE autobuild"

    # -- the mutation guard: a ROUTINE build is untouched by all of this --

    @pytest.mark.asyncio
    async def test_a_routine_interrupted_build_is_not_given_to_the_conductor(
        self,
        nats: EventLogNats,
        pool: SqliteLifecyclePersistence,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The named branch is state-shaped, not mode-shaped.

        An INTERRUPTED mode-a row takes the same branch and the same one
        card — and then launches the ROUTINE autobuild, because the router
        declines anything that is not a fix journey.
        """
        launched = self._record_routine_launches(monkeypatch)
        feature_id, correlation_id = "FEAT-DOORA", "corr-door-routine"
        build_id = pool.record_pending_build(
            _make_payload(feature_id=feature_id, correlation_id=correlation_id)
        )
        for frm, to in (
            (BuildState.QUEUED, BuildState.PREPARING),
            (BuildState.PREPARING, BuildState.RUNNING),
            (BuildState.RUNNING, BuildState.INTERRUPTED),
        ):
            pool.apply_transition(
                compose_transition(Build(build_id=build_id, status=frm), to)
            )
        conductor = _ReEntryConductor(pool)

        report, _resets = await self._drive_the_redelivery_door(
            nats,
            pool,
            build_id=build_id,
            feature_id=feature_id,
            correlation_id=correlation_id,
            conductor=conductor,
            mode="mode-a",
        )

        assert report.restarted_interrupted == 1
        assert len(self._cards(nats, build_id)) == 1
        assert conductor.worktrees.calls == [], "a routine build got a journey tree"
        assert conductor.spawned == [], "a routine build got a turn loop"
        assert len(launched) == 1 and launched[0]["build_id"] == build_id

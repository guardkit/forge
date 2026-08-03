"""TASK-GATE-D659 Wave 2 — pre-dispatch gate activation, production wiring.

Every scenario drives the FIRST production call site of ``gate_check``
(:func:`forge.cli._serve_gate_activation.maybe_gate_build`) through the REAL
approval-gate parts (:func:`forge.cli._serve_deps_gating.build_approval_gate_parts`)
over the in-memory NATS double, with a REAL
:class:`forge.pipeline.PipelineLifecycleEmitter` on the same transport and a
REAL SQLite database (``connect_writer`` + migrations) behind the SQLite gate
adapters. Only the transport is doubled — the pause / resume / cancel path runs
against real ``builds`` / ``stage_log`` rows.

Coverage (Wave-2 ACs 1–4, 7):

* ``TestPauseEmitsDualEnvelopeInOrder`` — AC-1: PAUSED + request_id in SQLite
  before any wire publish; AGENTS request published BEFORE build-paused.
* ``TestApproveResumesAndLaunches`` — AC-2 + dispatch: PAUSED→RUNNING, exactly
  one build-resumed, launch fires.
* ``TestRejectCancels`` — AC-3: CANCELLED-in-SQLite-first, one build-cancelled,
  zero build-resumed.
* ``TestExpiryCancels`` — AC-4: window expiry → REASON_MAX_WAIT → build-cancelled.
* ``TestDeferRepublishes`` — defer → fresh request_id + superseding build-paused.
* ``TestIdempotencyPreRead`` — an already-PAUSED row is held (rearm owns it).
* ``TestDispatchWiring`` — dispatch_build gates before launch; terminal acks;
  R1 deferred observer registration fires only on approve; R2 duplicate
  delivery mid-pause skipped WITHOUT ack, post-terminal acked.
* ``TestPostureAlignment`` — ADR-ARCH-019 (no static stage registry) +
  ADR-ARCH-026 (degraded → MANDATORY) + gate_check gains a production caller.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli import _serve_deps_gating
from forge.cli._conductor_outcome import TAKEN_RUNNING
from forge.cli._serve_deps import build_pipeline_consumer_deps
from forge.cli._serve_deps_gating import build_approval_gate_parts
from forge.cli._serve_deps_lifecycle import build_publisher_and_emitter
from forge.cli._serve_gate_activation import (
    ALREADY_PAUSED,
    maybe_gate_build,
)
from forge.cli.serve import build_conductor_router
from forge.config.models import ForgeConfig
from forge.lifecycle.modes import BuildMode
from forge.gating.identity import derive_request_id
from forge.gating.sqlite_adapters import build_sqlite_gate_adapters
from forge.gating.wrappers import REASON_MAX_WAIT, GateOutcome
from forge.lifecycle import migrations
from forge.lifecycle.identifiers import derive_build_id
from forge.lifecycle.persistence import Build, SqliteLifecyclePersistence
from forge.lifecycle.state_machine import (
    BuildState,
    transition as compose_transition,
)

from .conftest import InMemoryNats

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FEATURE_ID = "FEAT-GATE9"
CORRELATION_ID = "corr-gate-d659-0001"
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
# Order-recording NATS double — global publish order across subjects
# ---------------------------------------------------------------------------


class OrderRecordingNats(InMemoryNats):
    """:class:`InMemoryNats` that logs a flat cross-subject publish order.

    ``order`` is the sequence of published subjects, so the dual-envelope
    ordering (AGENTS request BEFORE build-paused) is provable across the two
    subjects rather than only within one per-subject queue.
    """

    def __init__(self) -> None:
        super().__init__()
        self.order: list[str] = []
        self._probe = None

    def set_publish_probe(self, probe) -> None:
        """Register a ``(subject) -> None`` hook run at each publish."""
        self._probe = probe

    async def publish(self, subject: str, body: bytes) -> None:
        self.order.append(subject)
        if self._probe is not None:
            self._probe(subject)
        await super().publish(subject, body)


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


def _paused_subject(feature_id: str = FEATURE_ID) -> str:
    return f"pipeline.build-paused.{feature_id}"


def _resumed_subject(feature_id: str = FEATURE_ID) -> str:
    return f"pipeline.build-resumed.{feature_id}"


def _cancelled_subject(feature_id: str = FEATURE_ID) -> str:
    return f"pipeline.build-cancelled.{feature_id}"


def _failed_subject(feature_id: str = FEATURE_ID) -> str:
    return f"pipeline.build-failed.{feature_id}"


def _request_id(build_id: str, attempt: int = 0) -> str:
    return derive_request_id(
        build_id=build_id, stage_label=STAGE, attempt_count=attempt
    )


def _payloads(nats: InMemoryNats, subject: str) -> list[dict[str, Any]]:
    return [json.loads(b)["payload"] for b in nats.published.get(subject, [])]


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


async def _drive_response(
    nats: InMemoryNats,
    *,
    build_id: str,
    request_id: str,
    decision: str,
    decided_by: str = RICH,
    notes: str | None = None,
) -> None:
    mirror = f"agents.approval.forge.{build_id}.response"
    await _wait_until(
        lambda: nats.subscribers.get(mirror), what=f"subscriber on {mirror}"
    )
    await nats.deliver_response(
        build_id=build_id,
        request_id=request_id,
        decision=decision,
        decided_by=decided_by,
        notes=notes,
    )


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
def nats() -> OrderRecordingNats:
    return OrderRecordingNats()


@pytest.fixture(autouse=True)
def _reset_bound_parts():
    _serve_deps_gating._reset_for_tests()
    yield
    _serve_deps_gating._reset_for_tests()


def _build_parts(
    nats: InMemoryNats,
    *,
    forge_config: ForgeConfig | None = None,
    subscriber_clock: Any = None,
):
    cfg = forge_config or _forge_config()
    _publisher, emitter = build_publisher_and_emitter(nats)
    parts = build_approval_gate_parts(
        nats,
        cfg,
        emitter=emitter,
        repository=None,
        bridge_registry=None,
        subscriber_clock=subscriber_clock,
    )
    return parts


def _seed_queued(pool: SqliteLifecyclePersistence, **overrides: Any) -> str:
    payload = _make_payload(**overrides)
    build_id = pool.record_pending_build(payload)
    return build_id


def _start_gate(
    parts,
    pool: SqliteLifecyclePersistence,
    repo,
    sm,
    build_id: str,
    *,
    feature_id: str = FEATURE_ID,
    correlation_id: str = CORRELATION_ID,
    clock: Any = None,
) -> "asyncio.Task[Any]":
    return asyncio.create_task(
        maybe_gate_build(
            parts=parts,
            sqlite_pool=pool,
            gate_repository=repo,
            gate_state_machine=sm,
            build_id=build_id,
            feature_id=feature_id,
            correlation_id=correlation_id,
            clock=clock or FixedClock(),
        )
    )


# ---------------------------------------------------------------------------
# AC-1 — pause emits the jarvis dual envelope, SQLite-before-wire, in order
# ---------------------------------------------------------------------------


class TestPauseEmitsDualEnvelopeInOrder:
    @pytest.mark.asyncio
    async def test_sqlite_paused_before_wire_and_request_before_build_paused(
        self, nats: OrderRecordingNats, pool: SqliteLifecyclePersistence
    ) -> None:
        build_id = _seed_queued(pool)
        repo, sm = build_sqlite_gate_adapters(pool, clock=FixedClock())
        parts = _build_parts(nats)

        # SQLite-before-wire probe: at the moment the FIRST wire envelope is
        # published, the builds row must already be PAUSED with its
        # request_id (record_paused_build + transition_to_paused ran first,
        # with no awaits between them and the publish).
        seen_at_first_publish: dict[str, Any] = {}

        def _probe(subject: str) -> None:
            if not seen_at_first_publish:
                seen_at_first_publish["subject"] = subject
                seen_at_first_publish["row"] = _row(pool, build_id)

        nats.set_publish_probe(_probe)

        gate_task = _start_gate(parts, pool, repo, sm, build_id)
        await _drive_response(
            nats,
            build_id=build_id,
            request_id=_request_id(build_id),
            decision="approve",
        )
        outcome = await asyncio.wait_for(gate_task, timeout=5.0)
        assert outcome is GateOutcome.RESUMED

        # SQLite-before-wire: first published subject was the AGENTS request,
        # and the row was already PAUSED + request_id at that instant.
        assert seen_at_first_publish["subject"] == _request_subject(build_id)
        status_at_pub, req_at_pub = seen_at_first_publish["row"]
        assert status_at_pub == BuildState.PAUSED.value
        assert req_at_pub == _request_id(build_id)

        # Dual-envelope order: AGENTS request BEFORE build-paused.
        req_idx = nats.order.index(_request_subject(build_id))
        paused_idx = nats.order.index(_paused_subject())
        assert req_idx < paused_idx

        # Both envelopes are on the wire (jarvis dual-envelope contract).
        assert len(_payloads(nats, _request_subject(build_id))) == 1
        paused = _payloads(nats, _paused_subject())
        assert len(paused) == 1
        assert paused[0]["build_id"] == build_id
        assert paused[0]["gate_mode"] == "MANDATORY_HUMAN_APPROVAL"
        assert paused[0]["correlation_id"] == CORRELATION_ID


# ---------------------------------------------------------------------------
# AC-2 — approve resumes exactly once
# ---------------------------------------------------------------------------


class TestApproveResumesAndLaunches:
    @pytest.mark.asyncio
    async def test_approve_transitions_running_and_emits_one_resumed(
        self, nats: OrderRecordingNats, pool: SqliteLifecyclePersistence
    ) -> None:
        build_id = _seed_queued(pool)
        repo, sm = build_sqlite_gate_adapters(pool, clock=FixedClock())
        parts = _build_parts(nats)

        gate_task = _start_gate(parts, pool, repo, sm, build_id)
        await _drive_response(
            nats,
            build_id=build_id,
            request_id=_request_id(build_id),
            decision="approve",
        )
        outcome = await asyncio.wait_for(gate_task, timeout=5.0)

        assert outcome is GateOutcome.RESUMED
        status, pending = _row(pool, build_id)
        assert status == BuildState.RUNNING.value
        assert pending is None  # resume auto-clears the pending id

        resumed = _payloads(nats, _resumed_subject())
        assert len(resumed) == 1
        assert resumed[0]["decision"] == "approve"
        assert resumed[0]["responder"] == RICH
        assert resumed[0]["correlation_id"] == CORRELATION_ID

    @pytest.mark.asyncio
    async def test_override_also_resumes(
        self, nats: OrderRecordingNats, pool: SqliteLifecyclePersistence
    ) -> None:
        build_id = _seed_queued(pool)
        repo, sm = build_sqlite_gate_adapters(pool, clock=FixedClock())
        parts = _build_parts(nats)

        gate_task = _start_gate(parts, pool, repo, sm, build_id)
        await _drive_response(
            nats,
            build_id=build_id,
            request_id=_request_id(build_id),
            decision="override",
            notes="ship it",
        )
        outcome = await asyncio.wait_for(gate_task, timeout=5.0)

        assert outcome is GateOutcome.OVERRIDDEN
        resumed = _payloads(nats, _resumed_subject())
        assert len(resumed) == 1
        assert resumed[0]["decision"] == "override"


# ---------------------------------------------------------------------------
# AC-3 — reject cancels (SQLite first), one build-cancelled, zero resumed
# ---------------------------------------------------------------------------


class TestRejectCancels:
    @pytest.mark.asyncio
    async def test_reject_cancels_sqlite_first_and_never_resumes(
        self, nats: OrderRecordingNats, pool: SqliteLifecyclePersistence
    ) -> None:
        build_id = _seed_queued(pool)
        repo, sm = build_sqlite_gate_adapters(pool, clock=FixedClock())
        parts = _build_parts(nats)

        # Capture the row status at the instant build-cancelled hits the wire.
        row_at_cancel: dict[str, Any] = {}

        def _probe(subject: str) -> None:
            if subject == _cancelled_subject() and not row_at_cancel:
                row_at_cancel["row"] = _row(pool, build_id)

        nats.set_publish_probe(_probe)

        gate_task = _start_gate(parts, pool, repo, sm, build_id)
        await _drive_response(
            nats,
            build_id=build_id,
            request_id=_request_id(build_id),
            decision="reject",
            notes="not safe",
        )
        outcome = await asyncio.wait_for(gate_task, timeout=5.0)

        assert outcome is GateOutcome.CANCELLED
        # CANCELLED in SQLite FIRST — already terminal when the wire fires.
        assert row_at_cancel["row"][0] == BuildState.CANCELLED.value
        # Exactly one build-cancelled, zero build-resumed.
        assert len(_payloads(nats, _cancelled_subject())) == 1
        assert _resumed_subject() not in nats.published


# ---------------------------------------------------------------------------
# AC-4 — window expiry cancels with REASON_MAX_WAIT
# ---------------------------------------------------------------------------


class TestExpiryCancels:
    @pytest.mark.asyncio
    async def test_window_expiry_produces_build_cancelled(
        self, nats: OrderRecordingNats, pool: SqliteLifecyclePersistence
    ) -> None:
        build_id = _seed_queued(pool)
        repo, sm = build_sqlite_gate_adapters(pool, clock=FixedClock())
        # Zero per-attempt wait + no refresh (repository=None on the parts) →
        # single window expires immediately, gate applies REASON_MAX_WAIT.
        parts = _build_parts(
            nats,
            forge_config=_forge_config(
                default_wait_seconds=0,
                max_wait_seconds=3600,
                expected_approver=RICH,
            ),
        )

        outcome = await asyncio.wait_for(
            _start_gate(parts, pool, repo, sm, build_id), timeout=5.0
        )

        assert outcome is GateOutcome.TIMED_OUT
        status, _ = _row(pool, build_id)
        assert status == BuildState.CANCELLED.value
        cancelled = _payloads(nats, _cancelled_subject())
        assert len(cancelled) == 1
        assert cancelled[0]["reason"] == REASON_MAX_WAIT
        assert _resumed_subject() not in nats.published


# ---------------------------------------------------------------------------
# Defer — fresh request_id + superseding build-paused per attempt
# ---------------------------------------------------------------------------


class TestDeferRepublishes:
    @pytest.mark.asyncio
    async def test_defer_republishes_fresh_request_and_second_build_paused(
        self, nats: OrderRecordingNats, pool: SqliteLifecyclePersistence
    ) -> None:
        build_id = _seed_queued(pool)
        repo, sm = build_sqlite_gate_adapters(pool, clock=FixedClock())
        parts = _build_parts(nats)

        gate_task = _start_gate(parts, pool, repo, sm, build_id)
        await _drive_response(
            nats,
            build_id=build_id,
            request_id=_request_id(build_id, 0),
            decision="defer",
        )
        # Wait for the republished request (attempt 1) + the fresh subscription.
        await _wait_until(
            lambda: len(nats.published.get(_request_subject(build_id), [])) >= 2,
            what="republished approval request",
        )
        await _drive_response(
            nats,
            build_id=build_id,
            request_id=_request_id(build_id, 1),
            decision="approve",
        )
        outcome = await asyncio.wait_for(gate_task, timeout=5.0)

        assert outcome is GateOutcome.RESUMED
        # Fresh request_id on the republish (attempt 1).
        republished = json.loads(nats.published[_request_subject(build_id)][1])
        assert republished["payload"]["request_id"] == _request_id(build_id, 1)
        # A superseding build-paused fired per attempt (fresh buttons).
        assert len(_payloads(nats, _paused_subject())) == 2
        # Exactly one resume, from the final approve.
        assert len(_payloads(nats, _resumed_subject())) == 1


# ---------------------------------------------------------------------------
# Idempotency pre-read — an already-PAUSED row is held (rearm owns it)
# ---------------------------------------------------------------------------


class TestIdempotencyPreRead:
    @pytest.mark.asyncio
    async def test_already_paused_row_returns_hold_without_second_gate(
        self, nats: OrderRecordingNats, pool: SqliteLifecyclePersistence
    ) -> None:
        build_id = _seed_queued(pool)
        # Drive the row into PAUSED with a pending request id directly.
        for frm, to in (
            (BuildState.QUEUED, BuildState.PREPARING),
            (BuildState.PREPARING, BuildState.RUNNING),
        ):
            pool.apply_transition(
                compose_transition(Build(build_id=build_id, status=frm), to)
            )
        pool.mark_paused(build_id, _request_id(build_id))

        repo, sm = build_sqlite_gate_adapters(pool, clock=FixedClock())
        parts = _build_parts(nats)

        outcome = await asyncio.wait_for(
            _start_gate(parts, pool, repo, sm, build_id), timeout=5.0
        )

        # No second gate started: HOLD sentinel, and nothing published.
        assert outcome is ALREADY_PAUSED
        assert _request_subject(build_id) not in nats.published
        assert _paused_subject() not in nats.published
        # Row is untouched (still PAUSED with the original request id).
        assert _row(pool, build_id) == (
            BuildState.PAUSED.value,
            _request_id(build_id),
        )


# ---------------------------------------------------------------------------
# Dispatch wiring — dispatch_build gates before launch; R1 + R2 posture
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


def _bound_dispatch_deps(
    nats: InMemoryNats,
    pool: SqliteLifecyclePersistence,
    starter: _FakeStarter,
    *,
    forge_config: ForgeConfig | None = None,
    conductor_router: Any = None,
):
    """Bind real gate parts + build production consumer deps over ``nats``."""
    cfg = forge_config or _forge_config()
    parts = _build_parts(nats, forge_config=cfg)
    _serve_deps_gating.bind_gate_parts(parts)
    repo, sm = build_sqlite_gate_adapters(pool, clock=FixedClock())
    deps = build_pipeline_consumer_deps(
        nats,
        cfg,
        pool,
        async_task_starter=starter,
        gate_repository=repo,
        gate_state_machine=sm,
        gate_clock=FixedClock(),
        conductor_router=conductor_router,
    )
    return deps


def _unwired_dispatch_deps(
    nats: InMemoryNats,
    pool: SqliteLifecyclePersistence,
    starter: _FakeStarter,
):
    """Deps with the approval gate NOT wired (DDR-007 soft-fail state).

    Models the boot where ``build_approval_gate_parts`` was caught by the
    DDR-007 guard: ``bound_gate_parts()`` is None and no gate adapters are
    threaded, so dispatch runs the legacy no-gate launch path.
    """
    _serve_deps_gating._reset_for_tests()  # bound_gate_parts() -> None
    cfg = _forge_config()
    return build_pipeline_consumer_deps(
        nats,
        cfg,
        pool,
        async_task_starter=starter,
        # gate_repository / gate_state_machine omitted → gate_wired False.
    )


class TestDispatchWiring:
    @pytest.mark.asyncio
    async def test_dispatch_gates_then_launches_on_approve_and_registers_observer(
        self, nats: OrderRecordingNats, pool: SqliteLifecyclePersistence
    ) -> None:
        starter = _FakeStarter()
        deps = _bound_dispatch_deps(nats, pool, starter)
        payload = _make_payload()
        build_id = derive_build_id(FEATURE_ID, QUEUED_AT)

        acked: list[bool] = []

        async def _ack() -> None:
            acked.append(True)

        registered: list[bool] = []

        async def _register_observer() -> None:
            registered.append(True)

        task = asyncio.create_task(
            deps.dispatch_build(payload, _ack, _register_observer)
        )
        await _drive_response(
            nats,
            build_id=build_id,
            request_id=_request_id(build_id),
            decision="approve",
        )
        await asyncio.wait_for(task, timeout=5.0)

        # gate_check ran to a production pause + approve, THEN launched.
        assert len(starter.launches) == 1
        assert starter.launches[0]["build_id"] == build_id
        # R1: observer registered exactly once, on the approve → launch path.
        assert registered == [True]
        # Approve is not a terminal — the slot is not acked by dispatch.
        assert acked == []
        assert _row(pool, build_id)[0] == BuildState.RUNNING.value

    @pytest.mark.asyncio
    async def test_dispatch_reject_acks_slot_without_launch_or_registration(
        self, nats: OrderRecordingNats, pool: SqliteLifecyclePersistence
    ) -> None:
        starter = _FakeStarter()
        deps = _bound_dispatch_deps(nats, pool, starter)
        payload = _make_payload()
        build_id = derive_build_id(FEATURE_ID, QUEUED_AT)

        acked: list[bool] = []

        async def _ack() -> None:
            acked.append(True)

        registered: list[bool] = []

        async def _register_observer() -> None:
            registered.append(True)

        task = asyncio.create_task(
            deps.dispatch_build(payload, _ack, _register_observer)
        )
        await _drive_response(
            nats,
            build_id=build_id,
            request_id=_request_id(build_id),
            decision="reject",
        )
        await asyncio.wait_for(task, timeout=5.0)

        # Terminal: slot acked, no launch, no observer registration (R1).
        assert acked == [True]
        assert starter.launches == []
        assert registered == []
        assert _row(pool, build_id)[0] == BuildState.CANCELLED.value

    @pytest.mark.asyncio
    async def test_conductor_taken_terminal_acks_slot_and_emits_build_failed(
        self, nats: OrderRecordingNats, pool: SqliteLifecyclePersistence
    ) -> None:
        """The activation lane's ack cure, at the seam it would have wedged.

        Twin of the gate-reject pin above, for the OTHER terminal a
        dispatch can reach: the gate APPROVES, the build goes to the
        conductor, and the conductor refuses it (here through the REAL
        router meeting the REAL cap law — the row carries no budget
        profile, so ``attended`` resolves with every cap ``None`` and the
        fix journey does not open).

        Before the taken-and-terminal vocabulary this refusal wrote its
        FAILED row and stopped: no ack and no event. With
        ``max_ack_pending=1`` on the pipeline consumer that ONE refusal
        held the only in-flight slot for the full 1h ``ack_wait``, so no
        other queued build dequeued for an hour, and no terminal envelope
        was ever published, so a correlation-id-following observer waited
        forever.

        What must now be true, all four:

        1. the slot is acked IN-LINE (the consumer is not wedged);
        2. ``pipeline.build-failed`` is on the wire with the refusal's own
           reason, ``recoverable=False``, and ``failed_task_id`` =
           ``builds.task_id`` (the fix journey's durable subject);
        3. nothing was launched down the routine autobuild path;
        4. the row is FAILED with the one-line reason on ``builds.error``.
        """
        starter = _FakeStarter()
        cfg = ForgeConfig.model_validate(
            {
                "permissions": {"filesystem": {"allowlist": ["/srv/forge"]}},
                "conductor": {"enabled": True},
            }
        )
        router = build_conductor_router(
            pool=pool,
            config=cfg,
            supervisor_factory=lambda _bid: pytest.fail(
                "a supervisor was built for a cap-refused fix journey"
            ),
            spawn=lambda coro: pytest.fail("a fix journey was spawned"),
        )
        assert router is not None

        deps = _bound_dispatch_deps(
            nats, pool, starter, forge_config=cfg, conductor_router=router
        )
        payload = _make_payload(feature_id="FEAT-FIXJ1")
        payload.mode = BuildMode.MODE_C
        payload.task_id = "TASK-FIXJ1"
        build_id = derive_build_id("FEAT-FIXJ1", QUEUED_AT)

        acked: list[bool] = []

        async def _ack() -> None:
            acked.append(True)

        task = asyncio.create_task(deps.dispatch_build(payload, _ack))
        await _drive_response(
            nats,
            build_id=build_id,
            request_id=_request_id(build_id),
            decision="approve",
        )
        await asyncio.wait_for(task, timeout=5.0)

        # 1 — the slot is released in-line, exactly as the gate-terminal
        #     arm above releases it.
        assert acked == [True], (
            "a conductor-refused build did not ack its slot — under "
            "max_ack_pending=1 that wedges the whole consumer for an hour"
        )
        # 2 — the terminal is on the wire, carrying the refusal's reason.
        failed = _payloads(nats, _failed_subject("FEAT-FIXJ1"))
        assert len(failed) == 1
        assert failed[0]["recoverable"] is False
        assert failed[0]["failed_task_id"] == "TASK-FIXJ1"
        assert "sets no cap" in failed[0]["failure_reason"]
        # 3 — nothing routine ran for a TASK-xxx subject.
        assert starter.launches == []
        # 4 — the row says WHY, in one line.
        status, _pending = _row(pool, build_id)
        assert status == BuildState.FAILED.value
        error = pool.connection.execute(
            "SELECT error FROM builds WHERE build_id = ?", (build_id,)
        ).fetchone()["error"]
        assert error and "\n" not in error
        assert error in failed[0]["failure_reason"]

    @pytest.mark.asyncio
    async def test_mode_c_with_no_router_refuses_on_the_gate_approved_arm(
        self, nats: OrderRecordingNats, pool: SqliteLifecyclePersistence
    ) -> None:
        """SEAM 3's regression pin (activation design §4.3).

        The closure had no test of its own: ``dispatch_build`` has no mode
        check outside the router, so a flag-off boot — or a router whose
        composition failed, or one that raised into the degrade rail —
        plus a runless mode-c redelivery ran a FIX TASK as a routine
        autobuild, the wrong machinery against a TASK-xxx subject. The
        queue-time belt already refuses mode-c queues while the flag is
        off, so this arm should be unreachable; unreachable-but-guarded is
        the posture, and an unpinned guard is one refactor from gone.

        Driven through the GATE-APPROVED arm (the router is ``None``, the
        gate says approve, and dispatch falls through to the launch arm),
        asserting all four: the row is FAILED, the slot is acked, the
        ``build-failed`` names the arm, and NOTHING launched.
        """
        starter = _FakeStarter()
        deps = _bound_dispatch_deps(nats, pool, starter, conductor_router=None)
        payload = _make_payload(feature_id="FEAT-FIXJ3")
        payload.mode = BuildMode.MODE_C
        payload.task_id = "TASK-FIXJ3"
        build_id = derive_build_id("FEAT-FIXJ3", QUEUED_AT)

        acked: list[bool] = []

        async def _ack() -> None:
            acked.append(True)

        task = asyncio.create_task(deps.dispatch_build(payload, _ack))
        await _drive_response(
            nats,
            build_id=build_id,
            request_id=_request_id(build_id),
            decision="approve",
        )
        await asyncio.wait_for(task, timeout=5.0)

        assert starter.launches == [], (
            "a fix task launched down the ROUTINE autobuild path with no "
            "conductor driving it — the silent downgrade §4.3 closes"
        )
        assert acked == [True]
        failed = _payloads(nats, _failed_subject("FEAT-FIXJ3"))
        assert len(failed) == 1, failed
        assert "routine launch arm" in failed[0]["failure_reason"]
        assert failed[0]["recoverable"] is False
        assert failed[0]["failed_task_id"] == "TASK-FIXJ3"
        assert _row(pool, build_id)[0] == BuildState.FAILED.value

    @pytest.mark.asyncio
    async def test_a_taken_running_journey_neither_acks_nor_launches(
        self, nats: OrderRecordingNats, pool: SqliteLifecyclePersistence
    ) -> None:
        """The mutation guard for the arm above.

        A guard that acked on EVERY taken build would pass the terminal
        test and break the running case: a live fix journey owns its own
        terminal, so acking here would release the slot under a build
        that is still going.
        """
        starter = _FakeStarter()

        async def running_router(**_kwargs: Any) -> Any:
            return TAKEN_RUNNING

        deps = _bound_dispatch_deps(
            nats, pool, starter, conductor_router=running_router
        )
        payload = _make_payload(feature_id="FEAT-FIXJ2")
        build_id = derive_build_id("FEAT-FIXJ2", QUEUED_AT)

        acked: list[bool] = []

        async def _ack() -> None:
            acked.append(True)

        task = asyncio.create_task(deps.dispatch_build(payload, _ack))
        await _drive_response(
            nats,
            build_id=build_id,
            request_id=_request_id(build_id),
            decision="approve",
        )
        await asyncio.wait_for(task, timeout=5.0)

        assert acked == []
        assert starter.launches == []
        assert _payloads(nats, _failed_subject("FEAT-FIXJ2")) == []

    @pytest.mark.asyncio
    async def test_duplicate_delivery_mid_pause_skipped_without_ack(
        self, nats: OrderRecordingNats, pool: SqliteLifecyclePersistence
    ) -> None:
        # R2 arm 1: a redelivery whose build row is PAUSED must be skipped
        # WITHOUT acking (the held-slot invariant). We simulate by pre-seeding
        # a PAUSED row for the same (feature_id, correlation_id) so
        # record_pending_build raises DuplicateBuildError.
        starter = _FakeStarter()
        deps = _bound_dispatch_deps(nats, pool, starter)
        payload = _make_payload()
        build_id = pool.record_pending_build(payload)
        for frm, to in (
            (BuildState.QUEUED, BuildState.PREPARING),
            (BuildState.PREPARING, BuildState.RUNNING),
        ):
            pool.apply_transition(
                compose_transition(Build(build_id=build_id, status=frm), to)
            )
        pool.mark_paused(build_id, _request_id(build_id))

        acked: list[bool] = []

        async def _ack() -> None:
            acked.append(True)

        await asyncio.wait_for(deps.dispatch_build(payload, _ack), timeout=5.0)

        # Held slot: NOT acked, NOT launched; row still PAUSED.
        assert acked == []
        assert starter.launches == []
        assert _row(pool, build_id)[0] == BuildState.PAUSED.value

    @pytest.mark.asyncio
    async def test_restart_mid_dispatch_freeze_queued_redelivery_redispatches(
        self, nats: OrderRecordingNats, pool: SqliteLifecyclePersistence
    ) -> None:
        # FWD-003 — the 2026-07-06 restart-mid-dispatch freeze, reproduced.
        #
        # Freeze shape (deploy-record c042bee + the 123f1f7 unfreeze note):
        # forge restarted mid-dispatch, BEFORE the QUEUED row progressed to
        # PREPARING. On restart the build-queued message redelivered; the
        # builds row was still QUEUED. Under max_ack_pending=1 the old
        # behaviour hit the "duplicate active build" arm and skipped WITHOUT
        # ack — so the un-acked redelivery wedged the single-consumer queue
        # until the 1h ack_wait expiry (the queue "self-cleared only at
        # expiry").
        #
        # After FWD-003 a QUEUED duplicate is RUNLESS and re-dispatches on the
        # existing build_id: the redelivery drives the gate → approve →
        # launch instead of spinning silently.
        starter = _FakeStarter()
        deps = _bound_dispatch_deps(nats, pool, starter)
        payload = _make_payload()

        # First dispatch was interrupted before it progressed the row: seed a
        # bare QUEUED row (record_pending_build's effect) and stop there.
        build_id = pool.record_pending_build(payload)
        assert _row(pool, build_id)[0] == BuildState.QUEUED.value

        acked: list[bool] = []

        async def _ack() -> None:
            acked.append(True)

        registered: list[bool] = []

        async def _register_observer() -> None:
            registered.append(True)

        # The redelivery — record_pending_build raises DuplicateBuildError
        # (row already QUEUED). The freeze fix re-dispatches through the gate.
        task = asyncio.create_task(
            deps.dispatch_build(payload, _ack, _register_observer)
        )
        await _drive_response(
            nats,
            build_id=build_id,
            request_id=_request_id(build_id),
            decision="approve",
        )
        await asyncio.wait_for(task, timeout=5.0)

        # UN-WEDGED: the redelivery re-dispatched (gate ran → approve →
        # launch) rather than skip-WITHOUT-ack. Observer registered on the
        # approve → launch path; the row advanced past QUEUED to RUNNING.
        assert len(starter.launches) == 1
        assert starter.launches[0]["build_id"] == build_id
        assert registered == [True]
        assert _row(pool, build_id)[0] == BuildState.RUNNING.value

    @pytest.mark.asyncio
    async def test_queued_redelivery_holds_slot_when_gate_unwired_no_double_launch(
        self, nats: OrderRecordingNats, pool: SqliteLifecyclePersistence
    ) -> None:
        # FWD-003 merge-review regression guard: in the DDR-007 no-gate
        # soft-fail path the legacy launch does NOT advance builds.status, so
        # a LIVE build keeps its row at QUEUED. A redelivery must therefore
        # NOT re-dispatch (that would double-launch the live build) — it holds
        # the slot. The runless-re-dispatch arm is gated on the gate being
        # wired precisely to avoid this.
        starter = _FakeStarter()
        deps = _unwired_dispatch_deps(nats, pool, starter)
        payload = _make_payload()

        acked: list[bool] = []

        async def _ack() -> None:
            acked.append(True)

        # First delivery: legacy no-gate launch; row stays QUEUED (no gate
        # hops to advance it).
        await asyncio.wait_for(deps.dispatch_build(payload, _ack), timeout=5.0)
        build_id = derive_build_id(FEATURE_ID, QUEUED_AT)
        assert len(starter.launches) == 1
        assert _row(pool, build_id)[0] == BuildState.QUEUED.value

        # Redelivery of the identical payload while the build is LIVE (row
        # still QUEUED). Must HOLD the slot — no second launch, no ack.
        await asyncio.wait_for(deps.dispatch_build(payload, _ack), timeout=5.0)
        assert len(starter.launches) == 1, "must NOT double-launch a live build"
        assert acked == []

    @pytest.mark.asyncio
    async def test_duplicate_delivery_post_terminal_is_acked(
        self, nats: OrderRecordingNats, pool: SqliteLifecyclePersistence
    ) -> None:
        # R2 arm 2: a redelivery whose build row is TERMINAL self-heals — ack
        # to release the slot, never launch.
        starter = _FakeStarter()
        deps = _bound_dispatch_deps(nats, pool, starter)
        payload = _make_payload()
        build_id = pool.record_pending_build(payload)
        pool.apply_transition(
            compose_transition(
                Build(build_id=build_id, status=BuildState.QUEUED),
                BuildState.CANCELLED,
            )
        )

        acked: list[bool] = []

        async def _ack() -> None:
            acked.append(True)

        await asyncio.wait_for(deps.dispatch_build(payload, _ack), timeout=5.0)

        assert acked == [True]
        assert starter.launches == []


# ---------------------------------------------------------------------------
# Robustness (Phase-5 FIX 2) — an ApprovalPublishError on the pause publish
# must HOLD the slot, not escape into handle_message (spurious build-failed +
# premature ack). The SQLite PAUSED row is durable; rearm re-emits next boot.
# ---------------------------------------------------------------------------


class TestPublishFailureHoldsSlot:
    @pytest.mark.asyncio
    async def test_approval_publish_error_holds_slot_no_build_failed(
        self, nats: OrderRecordingNats, pool: SqliteLifecyclePersistence
    ) -> None:
        starter = _FakeStarter()
        deps = _bound_dispatch_deps(nats, pool, starter)
        payload = _make_payload()
        build_id = derive_build_id(FEATURE_ID, QUEUED_AT)

        # The AGENTS approval-request publish fails at the transport → the
        # ApprovalPublisher wraps it in ApprovalPublishError, raised AFTER the
        # SQLite PAUSED row is committed (SQLite-before-wire, no-rollback).
        # Without the fix this escapes dispatch_build into handle_message,
        # which emits a (factually wrong) build-failed and acks the slot.
        nats.publish_failures[_request_subject(build_id)] = [
            RuntimeError("broker unavailable")
        ]

        acked: list[bool] = []

        async def _ack() -> None:
            acked.append(True)

        registered: list[bool] = []

        async def _register_observer() -> None:
            registered.append(True)

        # Must return cleanly (NOT raise ApprovalPublishError).
        await asyncio.wait_for(
            deps.dispatch_build(payload, _ack, _register_observer), timeout=5.0
        )

        # Slot HELD: no ack, no launch, no observer registration.
        assert acked == []
        assert starter.launches == []
        assert registered == []
        # The SQLite PAUSED row is durable (committed before the failed wire).
        assert _row(pool, build_id)[0] == BuildState.PAUSED.value
        # NO build-failed (and no build-paused: the request publish that carries
        # the dual envelope failed first) on the wire.
        assert _failed_subject() not in nats.published
        assert _paused_subject() not in nats.published


# ---------------------------------------------------------------------------
# Posture — ADR-ARCH-019 / ADR-ARCH-026 + first production caller of gate_check
# ---------------------------------------------------------------------------


class TestPostureAlignment:
    @pytest.mark.asyncio
    async def test_degraded_posture_forces_mandatory_human_approval(
        self, nats: OrderRecordingNats, pool: SqliteLifecyclePersistence
    ) -> None:
        # ADR-ARCH-026: the degraded reasoning callable forces
        # MANDATORY_HUMAN_APPROVAL — every dispatched build pauses. ADR-ARCH-019:
        # the gate targets a fixed subagent identifier, not a static stage
        # registry. Proof: the persisted GateDecision snapshot is MANDATORY.
        build_id = _seed_queued(pool)
        repo, sm = build_sqlite_gate_adapters(pool, clock=FixedClock())
        parts = _build_parts(nats)

        gate_task = _start_gate(parts, pool, repo, sm, build_id)
        await _drive_response(
            nats,
            build_id=build_id,
            request_id=_request_id(build_id),
            decision="approve",
        )
        await asyncio.wait_for(gate_task, timeout=5.0)

        # The build paused (MANDATORY posture) — its stage_log carries a
        # MANDATORY_HUMAN_APPROVAL gate decision snapshot, degraded mode.
        gate_dumps = [
            e.details["gate"] for e in pool.read_stages(build_id) if "gate" in e.details
        ]
        assert gate_dumps, "gate_check persisted no GateDecision (no caller ran)"
        assert gate_dumps[-1]["mode"] == "MANDATORY_HUMAN_APPROVAL"
        assert gate_dumps[-1]["degraded_mode"] is True
        assert gate_dumps[-1]["target_identifier"] == "autobuild_runner"

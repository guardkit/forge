"""TASK-JNB-102 — build-cancelled emits on the gating CANCELLED transitions.

Drives :func:`forge.gating.wrappers.gate_check` through the TASK-JNB-101
production factory (real ``PipelineLifecycleEmitter`` over ``InMemoryNats``)
and asserts the ``pipeline.build-cancelled.<feature_id>`` wire signal:

* ``TestRejectEmitsCancelled`` — reject decision branch.
* ``TestMaxWaitEmitsCancelled`` — REASON_MAX_WAIT breach in ``gate_check``.
* ``TestDeferTimeoutEmitsCancelled`` — the defer-branch max-wait duplicate.
* ``TestNoEmissionOnNonCancelOutcomes`` — approve/override emit nothing.
* ``TestDdr007BestEffort`` — a raising/failing publisher never regresses
  the SQLite transition and never propagates (WARNING only).

The CLI ``handle_cancel`` site is covered in
``tests/forge/test_cli_steering.py`` (notifier seam) and
``tests/cli/test_cli_runtime_cancelled_notifier.py`` (row-lookup
notifier); this module owns the two gating sites.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest

from forge.gating.wrappers import REASON_MAX_WAIT, GateOutcome, SOURCE_ID

from .conftest import (
    BUILD_ID,
    FEATURE_ID,
    RICH,
    InMemoryNats,
    InMemoryRepository,
    InMemoryStateMachine,
)
from .test_jnb101_production_wiring import (
    CORRELATION_ID,
    RESUMED_SUBJECT,
    _drive_response,
    _forge_config,
    _production_deps,
    _request_id,
    _start_gate,
)

CANCELLED_SUBJECT = f"pipeline.build-cancelled.{FEATURE_ID}"


def _cancelled_payloads(nats: InMemoryNats) -> list[dict[str, Any]]:
    return [
        json.loads(body)["payload"]
        for body in nats.published.get(CANCELLED_SUBJECT, [])
    ]


class TestRejectEmitsCancelled:
    """Reject → exactly one BuildCancelledPayload with the responder id."""

    @pytest.mark.asyncio
    async def test_reject_emits_exactly_one_cancelled_envelope(
        self,
        nats: InMemoryNats,
        repo: InMemoryRepository,
        state_machine: InMemoryStateMachine,
    ) -> None:
        deps = _production_deps(nats, repo, state_machine)
        gate_task = _start_gate(deps)

        await _drive_response(
            nats, request_id=_request_id(0), decision="reject", notes="not safe"
        )
        outcome, _ = await asyncio.wait_for(gate_task, timeout=5.0)

        assert outcome is GateOutcome.CANCELLED
        # SQLite first (authoritative), then the wire signal.
        assert state_machine.cancelled == [(BUILD_ID, "not safe")]
        cancelled = _cancelled_payloads(nats)
        assert len(cancelled) == 1
        assert cancelled[0]["cancelled_by"] == RICH
        assert cancelled[0]["reason"] == "not safe"
        assert cancelled[0]["correlation_id"] == CORRELATION_ID
        assert cancelled[0]["build_id"] == BUILD_ID
        assert cancelled[0]["feature_id"] == FEATURE_ID
        # And never a resumed signal for a reject.
        assert RESUMED_SUBJECT not in nats.published

    @pytest.mark.asyncio
    async def test_reject_without_notes_uses_reason_constant(
        self,
        nats: InMemoryNats,
        repo: InMemoryRepository,
        state_machine: InMemoryStateMachine,
    ) -> None:
        deps = _production_deps(nats, repo, state_machine)
        gate_task = _start_gate(deps)

        await _drive_response(nats, request_id=_request_id(0), decision="reject")
        outcome, _ = await asyncio.wait_for(gate_task, timeout=5.0)

        assert outcome is GateOutcome.CANCELLED
        cancelled = _cancelled_payloads(nats)
        assert len(cancelled) == 1
        assert cancelled[0]["reason"] == "approval rejected"


class TestMaxWaitEmitsCancelled:
    """REASON_MAX_WAIT breach in gate_check → one cancelled envelope."""

    @pytest.mark.asyncio
    async def test_window_expiry_emits_cancelled_with_system_identity(
        self,
        nats: InMemoryNats,
        repo: InMemoryRepository,
        state_machine: InMemoryStateMachine,
    ) -> None:
        deps = _production_deps(
            nats,
            repo,
            state_machine,
            forge_config=_forge_config(
                default_wait_seconds=0,
                max_wait_seconds=3600,
                expected_approver=RICH,
            ),
        )

        outcome, _ = await asyncio.wait_for(_start_gate(deps), timeout=5.0)

        assert outcome is GateOutcome.TIMED_OUT
        assert state_machine.cancelled == [(BUILD_ID, REASON_MAX_WAIT)]
        cancelled = _cancelled_payloads(nats)
        assert len(cancelled) == 1
        assert cancelled[0]["cancelled_by"] == SOURCE_ID
        assert cancelled[0]["reason"] == REASON_MAX_WAIT
        assert cancelled[0]["correlation_id"] == CORRELATION_ID


class TestDeferTimeoutEmitsCancelled:
    """The defer-branch max-wait duplicate also emits exactly once."""

    @pytest.mark.asyncio
    async def test_defer_then_expiry_emits_one_cancelled(
        self,
        nats: InMemoryNats,
        repo: InMemoryRepository,
        state_machine: InMemoryStateMachine,
    ) -> None:
        # Per-attempt window of 1s (real time): long enough for the
        # defer delivery to land in the first wait, short enough that
        # the recursive second wait (no refresh publisher) times out
        # and drives the defer-branch cancel.
        deps = _production_deps(nats, repo, state_machine)
        deps.per_attempt_wait_seconds = 1

        gate_task = _start_gate(deps)
        await _drive_response(nats, request_id=_request_id(0), decision="defer")

        outcome, _ = await asyncio.wait_for(gate_task, timeout=10.0)

        assert outcome is GateOutcome.TIMED_OUT
        assert state_machine.cancelled == [(BUILD_ID, REASON_MAX_WAIT)]
        cancelled = _cancelled_payloads(nats)
        assert len(cancelled) == 1
        assert cancelled[0]["cancelled_by"] == SOURCE_ID
        assert cancelled[0]["reason"] == REASON_MAX_WAIT


class TestNoEmissionOnNonCancelOutcomes:
    """Approve / override outcomes publish zero cancelled envelopes."""

    @pytest.mark.asyncio
    async def test_approve_emits_no_cancelled(
        self,
        nats: InMemoryNats,
        repo: InMemoryRepository,
        state_machine: InMemoryStateMachine,
    ) -> None:
        deps = _production_deps(nats, repo, state_machine)
        gate_task = _start_gate(deps)

        await _drive_response(nats, request_id=_request_id(0), decision="approve")
        outcome, _ = await asyncio.wait_for(gate_task, timeout=5.0)

        assert outcome is GateOutcome.RESUMED
        assert CANCELLED_SUBJECT not in nats.published

    @pytest.mark.asyncio
    async def test_override_emits_no_cancelled(
        self,
        nats: InMemoryNats,
        repo: InMemoryRepository,
        state_machine: InMemoryStateMachine,
    ) -> None:
        deps = _production_deps(nats, repo, state_machine)
        gate_task = _start_gate(deps)

        await _drive_response(
            nats, request_id=_request_id(0), decision="override", notes="ship"
        )
        outcome, _ = await asyncio.wait_for(gate_task, timeout=5.0)

        assert outcome is GateOutcome.OVERRIDDEN
        assert CANCELLED_SUBJECT not in nats.published


class TestDdr007BestEffort:
    """Publish failures never regress the transition or propagate."""

    @pytest.mark.asyncio
    async def test_raising_publish_cancelled_callback_is_swallowed(
        self,
        nats: InMemoryNats,
        repo: InMemoryRepository,
        state_machine: InMemoryStateMachine,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        deps = _production_deps(nats, repo, state_machine)

        async def _boom(*, reason: str, cancelled_by: str) -> None:
            raise RuntimeError("publisher down")

        deps.publish_cancelled = _boom

        gate_task = _start_gate(deps)
        await _drive_response(
            nats, request_id=_request_id(0), decision="reject", notes="no"
        )
        with caplog.at_level(logging.WARNING):
            outcome, _ = await asyncio.wait_for(gate_task, timeout=5.0)

        # The SQLite transition was already recorded and the caller saw
        # no exception (DDR-007) — only a WARNING marks the failure.
        assert outcome is GateOutcome.CANCELLED
        assert state_machine.cancelled == [(BUILD_ID, "no")]
        assert repo.cancelled == [(BUILD_ID, "no")]
        assert any(
            "build-cancelled publish failed" in r.message for r in caplog.records
        )
        assert CANCELLED_SUBJECT not in nats.published

    @pytest.mark.asyncio
    async def test_reject_transition_recorded_before_publish_attempt(
        self,
        nats: InMemoryNats,
        repo: InMemoryRepository,
        state_machine: InMemoryStateMachine,
    ) -> None:
        # AC wording: "the SQLite state transition completes first."
        # Pinned via the repository order_log (the established
        # atomicity-proof pattern) — a regression that moved the emit
        # before the state mutation would flip this ordering.
        deps = _production_deps(nats, repo, state_machine)

        async def _record(*, reason: str, cancelled_by: str) -> None:
            repo.order_log.append(("publish_cancelled", reason))

        deps.publish_cancelled = _record

        gate_task = _start_gate(deps)
        await _drive_response(
            nats, request_id=_request_id(0), decision="reject", notes="no"
        )
        await asyncio.wait_for(gate_task, timeout=5.0)

        ops = [op for op, _ in repo.order_log]
        assert "mark_cancelled" in ops and "publish_cancelled" in ops
        assert ops.index("mark_cancelled") < ops.index("publish_cancelled")

    @pytest.mark.asyncio
    async def test_max_wait_transition_recorded_before_publish_attempt(
        self,
        nats: InMemoryNats,
        repo: InMemoryRepository,
        state_machine: InMemoryStateMachine,
    ) -> None:
        deps = _production_deps(
            nats,
            repo,
            state_machine,
            forge_config=_forge_config(
                default_wait_seconds=0,
                max_wait_seconds=3600,
                expected_approver=RICH,
            ),
        )

        async def _record(*, reason: str, cancelled_by: str) -> None:
            repo.order_log.append(("publish_cancelled", reason))

        deps.publish_cancelled = _record

        await asyncio.wait_for(_start_gate(deps), timeout=5.0)

        ops = [op for op, _ in repo.order_log]
        assert ops.index("mark_cancelled") < ops.index("publish_cancelled")

    @pytest.mark.asyncio
    async def test_transport_failure_is_swallowed_by_emitter(
        self,
        nats: InMemoryNats,
        repo: InMemoryRepository,
        state_machine: InMemoryStateMachine,
    ) -> None:
        # One queued transport failure on the cancelled subject — the
        # emitter's _safe_publish swallows the PublishFailure; the
        # transition still lands (defence-in-depth below the wrapper's
        # own guard).
        nats.publish_failures[CANCELLED_SUBJECT] = [RuntimeError("boom")]
        deps = _production_deps(nats, repo, state_machine)
        gate_task = _start_gate(deps)

        await _drive_response(
            nats, request_id=_request_id(0), decision="reject", notes="no"
        )
        outcome, _ = await asyncio.wait_for(gate_task, timeout=5.0)

        assert outcome is GateOutcome.CANCELLED
        assert state_machine.cancelled == [(BUILD_ID, "no")]
        assert _cancelled_payloads(nats) == []

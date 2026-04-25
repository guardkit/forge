"""Unit tests for :mod:`forge.pipeline` — TASK-NFI-008.

Test classes mirror the acceptance criteria:

- AC-001 — every state-machine transition triggers exactly one publish.
- AC-002 — ``correlation_id`` is threaded onto every published payload
  (envelope-level assertion handled by TASK-NFI-006 tests; here we assert
  the payload carries the value the lifecycle emitter received from
  :class:`BuildContext`).
- AC-003 — ``publish_build_paused`` completes BEFORE ``interrupt()`` runs.
- AC-004 — :class:`PublishFailure` is logged but does not propagate; the
  emitter's ``emit_*`` methods always return ``None`` even when the
  publisher raises.
- AC-005 — the progress loop fires every
  ``PipelineConfig.progress_interval_seconds`` according to a
  :class:`FakeClock`.
- AC-006 — ``emit_wave_boundary_progress`` calls
  ``publish_build_progress`` with the same payload shape.
- AC-007 — covered by the per-method tests, which all mock
  :class:`PipelinePublisher`.
- AC-008 — covered by ``TestScenarioGroupD``: a full lifecycle run shares
  the originating ``correlation_id`` across every event.
- AC-009 — lint/format is enforced by CI; not asserted here.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest

from forge.adapters.nats import PublishFailure
from forge.config.models import PipelineConfig
from forge.pipeline import (
    BuildContext,
    FakeClock,
    PipelineLifecycleEmitter,
    State,
    attach_correlation_id,
)
from nats_core.events import (
    BuildCancelledPayload,
    BuildCompletePayload,
    BuildFailedPayload,
    BuildPausedPayload,
    BuildProgressPayload,
    BuildResumedPayload,
    BuildStartedPayload,
    StageCompletePayload,
)


# ---------------------------------------------------------------------------
# Shared fixtures + helpers
# ---------------------------------------------------------------------------


FEATURE_ID = "FEAT-A1B2"
BUILD_ID = "build-FEAT-A1B2-20260425120000"
CORRELATION_ID = "corr-from-build-queued"
WAVE_TOTAL = 3
PAUSED_AT = "2026-04-25T12:00:00+00:00"


@pytest.fixture
def ctx() -> BuildContext:
    return BuildContext(
        feature_id=FEATURE_ID,
        build_id=BUILD_ID,
        correlation_id=CORRELATION_ID,
        wave_total=WAVE_TOTAL,
    )


@pytest.fixture
def publisher() -> AsyncMock:
    """Async mock with the same eight method names as PipelinePublisher.

    Using ``spec`` here would couple the test to the publisher class, but
    we want the tests to break only on method-name divergence, which is
    asserted independently by the seam test in the task brief. So we
    spell out the eight names manually as ``AsyncMock`` attributes.
    """
    pub = AsyncMock()
    pub.publish_build_started = AsyncMock()
    pub.publish_build_progress = AsyncMock()
    pub.publish_stage_complete = AsyncMock()
    pub.publish_build_paused = AsyncMock()
    pub.publish_build_resumed = AsyncMock()
    pub.publish_build_complete = AsyncMock()
    pub.publish_build_failed = AsyncMock()
    pub.publish_build_cancelled = AsyncMock()
    return pub


@pytest.fixture
def config() -> PipelineConfig:
    # Keep the test cadence tight so progress-loop tests advance quickly.
    return PipelineConfig(progress_interval_seconds=60)


@pytest.fixture
def emitter(
    publisher: AsyncMock, config: PipelineConfig
) -> PipelineLifecycleEmitter:
    return PipelineLifecycleEmitter(publisher, config)


def _stage_complete_kwargs() -> dict[str, Any]:
    return dict(
        stage_label="implementation",
        target_kind="subagent",
        target_identifier="implementer",
        status="PASSED",
        gate_mode="AUTO_APPROVE",
        coach_score=0.92,
        duration_secs=12.5,
        completed_at=PAUSED_AT,
    )


def _paused_kwargs() -> dict[str, Any]:
    return dict(
        stage_label="implementation",
        gate_mode="FLAG_FOR_REVIEW",
        coach_score=0.42,
        rationale="below threshold",
        approval_subject="pipeline.approval.FEAT-A1B2",
        paused_at=PAUSED_AT,
    )


def _resumed_kwargs() -> dict[str, Any]:
    return dict(
        stage_label="implementation",
        decision="approve",
        responder="rich",
        resumed_at=PAUSED_AT,
    )


def _complete_kwargs() -> dict[str, Any]:
    return dict(
        repo="guardkit/lpa-platform",
        branch="feat/a1b2",
        tasks_completed=8,
        tasks_failed=0,
        tasks_total=8,
        pr_url="https://github.com/guardkit/lpa-platform/pull/42",
        duration_seconds=600,
        summary="all 8 tasks completed",
    )


def _failed_kwargs() -> dict[str, Any]:
    return dict(
        failure_reason="hard stop in evaluation",
        recoverable=False,
        failed_task_id="TASK-A1B2-003",
    )


def _cancelled_kwargs() -> dict[str, Any]:
    return dict(
        reason="operator cancel via forge cancel",
        cancelled_by="rich",
        cancelled_at=PAUSED_AT,
    )


# ---------------------------------------------------------------------------
# AC-001: every transition triggers exactly one publish
# ---------------------------------------------------------------------------


class TestEmitFiresExactlyOnce:
    """Each emit_* call MUST trigger its publisher method exactly once."""

    async def test_emit_started_publishes_once(
        self, emitter: PipelineLifecycleEmitter, publisher: AsyncMock, ctx: BuildContext
    ) -> None:
        await emitter.emit_started(ctx)
        publisher.publish_build_started.assert_awaited_once()
        # And no other method was touched.
        publisher.publish_build_progress.assert_not_called()
        publisher.publish_build_paused.assert_not_called()

    async def test_emit_progress_publishes_once(
        self, emitter: PipelineLifecycleEmitter, publisher: AsyncMock, ctx: BuildContext
    ) -> None:
        await emitter.emit_progress(
            ctx, wave=1, overall_progress_pct=12.5, elapsed_seconds=10
        )
        publisher.publish_build_progress.assert_awaited_once()

    async def test_emit_stage_complete_publishes_once(
        self, emitter: PipelineLifecycleEmitter, publisher: AsyncMock, ctx: BuildContext
    ) -> None:
        await emitter.emit_stage_complete(ctx, **_stage_complete_kwargs())
        publisher.publish_stage_complete.assert_awaited_once()

    async def test_emit_paused_publishes_once(
        self, emitter: PipelineLifecycleEmitter, publisher: AsyncMock, ctx: BuildContext
    ) -> None:
        await emitter.emit_paused(ctx, **_paused_kwargs())
        publisher.publish_build_paused.assert_awaited_once()

    async def test_emit_resumed_publishes_once(
        self, emitter: PipelineLifecycleEmitter, publisher: AsyncMock, ctx: BuildContext
    ) -> None:
        await emitter.emit_resumed(ctx, **_resumed_kwargs())
        publisher.publish_build_resumed.assert_awaited_once()

    async def test_emit_complete_publishes_once(
        self, emitter: PipelineLifecycleEmitter, publisher: AsyncMock, ctx: BuildContext
    ) -> None:
        await emitter.emit_complete(ctx, **_complete_kwargs())
        publisher.publish_build_complete.assert_awaited_once()

    async def test_emit_failed_publishes_once(
        self, emitter: PipelineLifecycleEmitter, publisher: AsyncMock, ctx: BuildContext
    ) -> None:
        await emitter.emit_failed(ctx, **_failed_kwargs())
        publisher.publish_build_failed.assert_awaited_once()

    async def test_emit_cancelled_publishes_once(
        self, emitter: PipelineLifecycleEmitter, publisher: AsyncMock, ctx: BuildContext
    ) -> None:
        await emitter.emit_cancelled(ctx, **_cancelled_kwargs())
        publisher.publish_build_cancelled.assert_awaited_once()


# ---------------------------------------------------------------------------
# AC-001 / AC-002 via on_transition: each declared transition routes
# ---------------------------------------------------------------------------


class TestOnTransitionRouting:
    """on_transition routes (from_state, to_state) to the right emit method."""

    async def test_preparing_to_running_emits_started(
        self, emitter: PipelineLifecycleEmitter, publisher: AsyncMock, ctx: BuildContext
    ) -> None:
        await emitter.on_transition(State.PREPARING, State.RUNNING, ctx)
        publisher.publish_build_started.assert_awaited_once()

    async def test_running_to_paused_emits_paused(
        self, emitter: PipelineLifecycleEmitter, publisher: AsyncMock, ctx: BuildContext
    ) -> None:
        await emitter.on_transition(
            State.RUNNING, State.PAUSED, ctx, **_paused_kwargs()
        )
        publisher.publish_build_paused.assert_awaited_once()

    async def test_paused_to_running_emits_resumed(
        self, emitter: PipelineLifecycleEmitter, publisher: AsyncMock, ctx: BuildContext
    ) -> None:
        await emitter.on_transition(
            State.PAUSED, State.RUNNING, ctx, **_resumed_kwargs()
        )
        publisher.publish_build_resumed.assert_awaited_once()

    async def test_finalising_to_complete_emits_complete(
        self, emitter: PipelineLifecycleEmitter, publisher: AsyncMock, ctx: BuildContext
    ) -> None:
        await emitter.on_transition(
            State.FINALISING, State.COMPLETE, ctx, **_complete_kwargs()
        )
        publisher.publish_build_complete.assert_awaited_once()

    async def test_any_to_failed_emits_failed(
        self, emitter: PipelineLifecycleEmitter, publisher: AsyncMock, ctx: BuildContext
    ) -> None:
        await emitter.on_transition(
            State.RUNNING, State.FAILED, ctx, **_failed_kwargs()
        )
        publisher.publish_build_failed.assert_awaited_once()

    async def test_any_to_cancelled_emits_cancelled(
        self, emitter: PipelineLifecycleEmitter, publisher: AsyncMock, ctx: BuildContext
    ) -> None:
        await emitter.on_transition(
            State.RUNNING, State.CANCELLED, ctx, **_cancelled_kwargs()
        )
        publisher.publish_build_cancelled.assert_awaited_once()

    async def test_unknown_transition_is_noop(
        self, emitter: PipelineLifecycleEmitter, publisher: AsyncMock, ctx: BuildContext
    ) -> None:
        # PREPARING -> COMPLETE is not in the table.
        await emitter.on_transition(State.PREPARING, State.COMPLETE, ctx)
        for method_name in (
            "publish_build_started",
            "publish_build_progress",
            "publish_stage_complete",
            "publish_build_paused",
            "publish_build_resumed",
            "publish_build_complete",
            "publish_build_failed",
            "publish_build_cancelled",
        ):
            getattr(publisher, method_name).assert_not_called()


# ---------------------------------------------------------------------------
# AC-002: payload type + correlation_id threading
# ---------------------------------------------------------------------------


class TestPayloadShape:
    """Each emit method passes a typed payload with the right correlation_id."""

    async def test_started_payload_is_typed_and_threads_correlation(
        self, emitter: PipelineLifecycleEmitter, publisher: AsyncMock, ctx: BuildContext
    ) -> None:
        await emitter.emit_started(ctx)
        (sent,), _ = publisher.publish_build_started.call_args
        assert isinstance(sent, BuildStartedPayload)
        assert sent.feature_id == FEATURE_ID
        assert sent.build_id == BUILD_ID
        assert sent.wave_total == WAVE_TOTAL
        # v1 payloads use extra="ignore" → correlation_id is attached
        # post-construction so getattr finds it (publisher reads it that way).
        assert getattr(sent, "correlation_id", None) == CORRELATION_ID

    async def test_progress_payload_threads_correlation(
        self, emitter: PipelineLifecycleEmitter, publisher: AsyncMock, ctx: BuildContext
    ) -> None:
        await emitter.emit_progress(
            ctx, wave=2, overall_progress_pct=42.5, elapsed_seconds=120
        )
        (sent,), _ = publisher.publish_build_progress.call_args
        assert isinstance(sent, BuildProgressPayload)
        assert sent.wave == 2
        assert sent.wave_total == WAVE_TOTAL
        assert sent.overall_progress_pct == 42.5
        assert sent.elapsed_seconds == 120
        assert getattr(sent, "correlation_id", None) == CORRELATION_ID

    async def test_stage_complete_payload_carries_correlation(
        self, emitter: PipelineLifecycleEmitter, publisher: AsyncMock, ctx: BuildContext
    ) -> None:
        await emitter.emit_stage_complete(ctx, **_stage_complete_kwargs())
        (sent,), _ = publisher.publish_stage_complete.call_args
        assert isinstance(sent, StageCompletePayload)
        # v2.2 payload — correlation_id is a declared field.
        assert sent.correlation_id == CORRELATION_ID
        assert sent.stage_label == "implementation"

    async def test_paused_payload_carries_correlation(
        self, emitter: PipelineLifecycleEmitter, publisher: AsyncMock, ctx: BuildContext
    ) -> None:
        await emitter.emit_paused(ctx, **_paused_kwargs())
        (sent,), _ = publisher.publish_build_paused.call_args
        assert isinstance(sent, BuildPausedPayload)
        assert sent.correlation_id == CORRELATION_ID
        assert sent.gate_mode == "FLAG_FOR_REVIEW"

    async def test_resumed_payload_carries_correlation(
        self, emitter: PipelineLifecycleEmitter, publisher: AsyncMock, ctx: BuildContext
    ) -> None:
        await emitter.emit_resumed(ctx, **_resumed_kwargs())
        (sent,), _ = publisher.publish_build_resumed.call_args
        assert isinstance(sent, BuildResumedPayload)
        assert sent.correlation_id == CORRELATION_ID
        assert sent.decision == "approve"

    async def test_complete_payload_threads_correlation(
        self, emitter: PipelineLifecycleEmitter, publisher: AsyncMock, ctx: BuildContext
    ) -> None:
        await emitter.emit_complete(ctx, **_complete_kwargs())
        (sent,), _ = publisher.publish_build_complete.call_args
        assert isinstance(sent, BuildCompletePayload)
        assert getattr(sent, "correlation_id", None) == CORRELATION_ID
        assert sent.tasks_total == 8

    async def test_failed_payload_threads_correlation(
        self, emitter: PipelineLifecycleEmitter, publisher: AsyncMock, ctx: BuildContext
    ) -> None:
        await emitter.emit_failed(ctx, **_failed_kwargs())
        (sent,), _ = publisher.publish_build_failed.call_args
        assert isinstance(sent, BuildFailedPayload)
        assert getattr(sent, "correlation_id", None) == CORRELATION_ID
        assert sent.recoverable is False

    async def test_cancelled_payload_carries_correlation(
        self, emitter: PipelineLifecycleEmitter, publisher: AsyncMock, ctx: BuildContext
    ) -> None:
        await emitter.emit_cancelled(ctx, **_cancelled_kwargs())
        (sent,), _ = publisher.publish_build_cancelled.call_args
        assert isinstance(sent, BuildCancelledPayload)
        assert sent.correlation_id == CORRELATION_ID
        assert sent.cancelled_by == "rich"


# ---------------------------------------------------------------------------
# AC-002 helper: attach_correlation_id behaviour
# ---------------------------------------------------------------------------


class TestAttachCorrelationId:
    """attach_correlation_id makes correlation_id getattr-able on v1 payloads.

    The two methods below are logically synchronous — ``attach_correlation_id``
    does no I/O — but the module-level ``pytestmark = pytest.mark.asyncio``
    decoration warns on sync test functions. Declaring them ``async`` keeps
    pytest-asyncio happy without changing semantics.
    """

    async def test_attach_to_v1_payload_makes_getattr_succeed(self) -> None:
        payload = BuildStartedPayload(
            feature_id=FEATURE_ID, build_id=BUILD_ID, wave_total=3
        )
        assert getattr(payload, "correlation_id", None) is None
        attach_correlation_id(payload, CORRELATION_ID)
        assert getattr(payload, "correlation_id", None) == CORRELATION_ID

    async def test_attach_does_not_pollute_model_dump(self) -> None:
        # The v1 payloads are wire-compatible — model_dump must NOT include
        # the attached correlation_id (it travels on the envelope instead).
        payload = BuildProgressPayload(
            feature_id=FEATURE_ID,
            build_id=BUILD_ID,
            wave=1,
            wave_total=3,
            overall_progress_pct=10.0,
            elapsed_seconds=1,
        )
        attach_correlation_id(payload, CORRELATION_ID)
        dumped = payload.model_dump()
        assert "correlation_id" not in dumped


# ---------------------------------------------------------------------------
# AC-003: emit_paused_then_interrupt order
# ---------------------------------------------------------------------------


class TestPausedBeforeInterrupt:
    """publish_build_paused MUST complete before interrupt() runs."""

    async def test_publish_runs_strictly_before_interrupt(
        self, emitter: PipelineLifecycleEmitter, publisher: AsyncMock, ctx: BuildContext
    ) -> None:
        order: list[str] = []

        async def fake_publish_paused(_payload: object) -> None:
            order.append("publish")

        async def fake_interrupt() -> None:
            order.append("interrupt")

        publisher.publish_build_paused = AsyncMock(side_effect=fake_publish_paused)

        await emitter.emit_paused_then_interrupt(
            ctx, **_paused_kwargs(), interrupt=fake_interrupt
        )

        assert order == ["publish", "interrupt"]

    async def test_interrupt_still_runs_even_if_publish_fails(
        self, emitter: PipelineLifecycleEmitter, publisher: AsyncMock, ctx: BuildContext
    ) -> None:
        # The state machine has already written the SQLite PAUSED row by
        # this point — the interrupt MUST fire so the LangGraph build
        # actually pauses, even if the wire-level publish fails.
        order: list[str] = []

        async def fake_publish_paused(_payload: object) -> None:
            order.append("publish-attempted")
            raise PublishFailure("pipeline.build-paused.X", RuntimeError("nats down"))

        async def fake_interrupt() -> None:
            order.append("interrupt")

        publisher.publish_build_paused = AsyncMock(side_effect=fake_publish_paused)

        await emitter.emit_paused_then_interrupt(
            ctx, **_paused_kwargs(), interrupt=fake_interrupt
        )

        assert order == ["publish-attempted", "interrupt"]


# ---------------------------------------------------------------------------
# AC-004: PublishFailure is logged but never propagated
# ---------------------------------------------------------------------------


class TestPublishFailureSwallowed:
    """A PublishFailure must NOT bubble up — SQLite truth must not roll back."""

    async def test_emit_started_swallows_publish_failure_and_logs(
        self,
        emitter: PipelineLifecycleEmitter,
        publisher: AsyncMock,
        ctx: BuildContext,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        publisher.publish_build_started = AsyncMock(
            side_effect=PublishFailure(
                "pipeline.build-started.FEAT-A1B2", RuntimeError("connection lost")
            )
        )
        with caplog.at_level(logging.ERROR, logger="forge.pipeline"):
            # MUST NOT raise.
            await emitter.emit_started(ctx)

        assert any(
            "publish failed" in record.message.lower()
            and "build-started" in record.message
            for record in caplog.records
        )

    @pytest.mark.parametrize(
        "method_name,kwargs_factory",
        [
            ("emit_progress", lambda: dict(
                wave=1, overall_progress_pct=12.5, elapsed_seconds=10
            )),
            ("emit_stage_complete", _stage_complete_kwargs),
            ("emit_paused", _paused_kwargs),
            ("emit_resumed", _resumed_kwargs),
            ("emit_complete", _complete_kwargs),
            ("emit_failed", _failed_kwargs),
            ("emit_cancelled", _cancelled_kwargs),
        ],
    )
    async def test_every_emit_method_swallows_publish_failure(
        self,
        emitter: PipelineLifecycleEmitter,
        publisher: AsyncMock,
        ctx: BuildContext,
        method_name: str,
        kwargs_factory: Any,
    ) -> None:
        # Map emit method -> publisher method.
        publisher_method = {
            "emit_progress": "publish_build_progress",
            "emit_stage_complete": "publish_stage_complete",
            "emit_paused": "publish_build_paused",
            "emit_resumed": "publish_build_resumed",
            "emit_complete": "publish_build_complete",
            "emit_failed": "publish_build_failed",
            "emit_cancelled": "publish_build_cancelled",
        }[method_name]
        setattr(
            publisher,
            publisher_method,
            AsyncMock(
                side_effect=PublishFailure(
                    f"pipeline.{publisher_method}.X", RuntimeError("nats down")
                )
            ),
        )
        method = getattr(emitter, method_name)
        # MUST NOT raise.
        await method(ctx, **kwargs_factory())


# ---------------------------------------------------------------------------
# AC-005: progress loop fires at PipelineConfig.progress_interval_seconds
# ---------------------------------------------------------------------------


class TestProgressLoop:
    """Progress publishes fire at the configured interval against FakeClock."""

    async def test_progress_fires_after_each_interval_tick(
        self, publisher: AsyncMock, config: PipelineConfig, ctx: BuildContext
    ) -> None:
        clock = FakeClock()
        emitter = PipelineLifecycleEmitter(publisher, config, clock=clock)

        # Snapshot returns deterministic progress values per call so we
        # can assert per-tick payload identity, not just count.
        calls = {"n": 0}

        def get_progress() -> tuple[int, float, int]:
            calls["n"] += 1
            return (calls["n"], 10.0 * calls["n"], 60 * calls["n"])

        task = await emitter.start_progress_loop(ctx, get_progress)

        # Tick 1 — one publish fires.
        await clock.advance(config.progress_interval_seconds)
        # Allow the awaited emit_progress to schedule + record.
        await asyncio.sleep(0)
        assert publisher.publish_build_progress.await_count == 1

        # Tick 2 — another publish.
        await clock.advance(config.progress_interval_seconds)
        await asyncio.sleep(0)
        assert publisher.publish_build_progress.await_count == 2

        # Tick 3 — another publish.
        await clock.advance(config.progress_interval_seconds)
        await asyncio.sleep(0)
        assert publisher.publish_build_progress.await_count == 3

        await emitter.stop_progress_loop(ctx.build_id)
        assert task.done()

    async def test_progress_does_not_fire_before_interval(
        self, publisher: AsyncMock, config: PipelineConfig, ctx: BuildContext
    ) -> None:
        clock = FakeClock()
        emitter = PipelineLifecycleEmitter(publisher, config, clock=clock)

        def get_progress() -> tuple[int, float, int]:
            return (1, 10.0, 60)

        await emitter.start_progress_loop(ctx, get_progress)

        # Advance only half the interval — still asleep, no publish yet.
        await clock.advance(config.progress_interval_seconds / 2)
        await asyncio.sleep(0)
        assert publisher.publish_build_progress.await_count == 0

        await emitter.stop_progress_loop(ctx.build_id)

    async def test_stop_progress_loop_cancels_task(
        self, publisher: AsyncMock, config: PipelineConfig, ctx: BuildContext
    ) -> None:
        clock = FakeClock()
        emitter = PipelineLifecycleEmitter(publisher, config, clock=clock)

        def get_progress() -> tuple[int, float, int]:
            return (1, 10.0, 60)

        task = await emitter.start_progress_loop(ctx, get_progress)
        assert not task.done()
        await emitter.stop_progress_loop(ctx.build_id)
        assert task.done()
        # Build has been popped from the registry.
        assert ctx.build_id not in emitter.progress_tasks

    async def test_stop_progress_loop_idempotent(
        self,
        emitter: PipelineLifecycleEmitter,
        ctx: BuildContext,
    ) -> None:
        # Calling stop on a never-started loop is a no-op (no exception).
        await emitter.stop_progress_loop(ctx.build_id)
        await emitter.stop_progress_loop(ctx.build_id)

    async def test_start_progress_loop_is_idempotent_per_build(
        self, publisher: AsyncMock, config: PipelineConfig, ctx: BuildContext
    ) -> None:
        clock = FakeClock()
        emitter = PipelineLifecycleEmitter(publisher, config, clock=clock)

        def get_progress() -> tuple[int, float, int]:
            return (1, 10.0, 60)

        first = await emitter.start_progress_loop(ctx, get_progress)
        # Second start while the first is still running returns the same
        # task — no second loop is scheduled.
        second = await emitter.start_progress_loop(ctx, get_progress)
        assert first is second

        await emitter.stop_progress_loop(ctx.build_id)


# ---------------------------------------------------------------------------
# AC-006: wave-boundary hook
# ---------------------------------------------------------------------------


class TestWaveBoundary:
    """emit_wave_boundary_progress is the documented autobuild_runner hook."""

    async def test_wave_boundary_calls_publish_build_progress(
        self, emitter: PipelineLifecycleEmitter, publisher: AsyncMock, ctx: BuildContext
    ) -> None:
        await emitter.emit_wave_boundary_progress(
            ctx, wave=2, overall_progress_pct=66.0, elapsed_seconds=400
        )
        publisher.publish_build_progress.assert_awaited_once()
        (sent,), _ = publisher.publish_build_progress.call_args
        assert isinstance(sent, BuildProgressPayload)
        assert sent.wave == 2
        assert sent.overall_progress_pct == 66.0
        assert getattr(sent, "correlation_id", None) == CORRELATION_ID


# ---------------------------------------------------------------------------
# AC-008: scenario test — Group D "All lifecycle events share originating
# correlation identifier"
# ---------------------------------------------------------------------------


class TestScenarioGroupD:
    """Group D: every event in a full lifecycle threads the same correlation_id."""

    async def test_full_lifecycle_threads_single_correlation_id(
        self, emitter: PipelineLifecycleEmitter, publisher: AsyncMock, ctx: BuildContext
    ) -> None:
        # PREPARING → RUNNING
        await emitter.emit_started(ctx)
        # progress (heartbeat)
        await emitter.emit_progress(
            ctx, wave=1, overall_progress_pct=10.0, elapsed_seconds=60
        )
        # stage commits
        await emitter.emit_stage_complete(ctx, **_stage_complete_kwargs())
        # RUNNING → PAUSED (via the ordering helper to exercise that path too)
        await emitter.emit_paused_then_interrupt(
            ctx, **_paused_kwargs(), interrupt=AsyncMock()
        )
        # PAUSED → RUNNING
        await emitter.emit_resumed(ctx, **_resumed_kwargs())
        # wave boundary
        await emitter.emit_wave_boundary_progress(
            ctx, wave=2, overall_progress_pct=66.0, elapsed_seconds=180
        )
        # FINALISING → COMPLETE
        await emitter.emit_complete(ctx, **_complete_kwargs())

        # Now collect every payload we sent and assert they all share
        # ctx.correlation_id. v1 payloads expose it via getattr; v2.2
        # payloads have it as a declared field.
        all_publishers = [
            publisher.publish_build_started,
            publisher.publish_build_progress,
            publisher.publish_stage_complete,
            publisher.publish_build_paused,
            publisher.publish_build_resumed,
            publisher.publish_build_complete,
        ]
        sent_payloads: list[Any] = []
        for mock in all_publishers:
            for call in mock.call_args_list:
                sent_payloads.append(call.args[0])

        # publish_build_progress is called twice (heartbeat + wave boundary).
        assert publisher.publish_build_progress.await_count == 2
        # Six distinct methods plus the second progress = 7 payloads total.
        assert len(sent_payloads) == 7

        for payload in sent_payloads:
            assert getattr(payload, "correlation_id", None) == CORRELATION_ID, (
                f"{type(payload).__name__} did not thread "
                f"correlation_id={CORRELATION_ID!r}"
            )

    async def test_failed_terminal_threads_correlation(
        self, emitter: PipelineLifecycleEmitter, publisher: AsyncMock, ctx: BuildContext
    ) -> None:
        # An alternative terminal path — failure mid-build still threads
        # the originating correlation_id onto the failed event.
        await emitter.emit_started(ctx)
        await emitter.emit_failed(ctx, **_failed_kwargs())

        for mock in (publisher.publish_build_started, publisher.publish_build_failed):
            for call in mock.call_args_list:
                assert getattr(call.args[0], "correlation_id", None) == CORRELATION_ID

    async def test_cancelled_terminal_threads_correlation(
        self, emitter: PipelineLifecycleEmitter, publisher: AsyncMock, ctx: BuildContext
    ) -> None:
        await emitter.emit_started(ctx)
        await emitter.emit_cancelled(ctx, **_cancelled_kwargs())

        for mock in (
            publisher.publish_build_started,
            publisher.publish_build_cancelled,
        ):
            for call in mock.call_args_list:
                assert getattr(call.args[0], "correlation_id", None) == CORRELATION_ID


# ---------------------------------------------------------------------------
# pytest-asyncio mode
# ---------------------------------------------------------------------------


# Apply the asyncio mark to every async test in this module without each
# class needing the pytestmark itself. ``pytest.ini`` may already set
# ``asyncio_mode = "auto"`` — the decorator below is a defensive no-op
# in that case.
pytestmark = pytest.mark.asyncio

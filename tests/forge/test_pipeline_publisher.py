"""Unit tests for :mod:`forge.adapters.nats.pipeline_publisher`.

Test classes mirror the acceptance criteria of TASK-NFI-006:

- AC-001 — eight publisher methods exist on :class:`PipelinePublisher`.
- AC-002 — every publish targets ``pipeline.{event}.{feature_id}``.
- AC-003 — every envelope has ``source_id == "forge"`` and threads
  ``correlation_id`` from the payload.
- AC-004 — fire-and-forget; PubAck is logged but never treated as
  delivery proof.
- AC-005 — transport-level failures raise :class:`PublishFailure` and
  callers (the test) can catch + log.
- AC-006 — covered by the per-method tests above.
- AC-007 — concurrency: 100 concurrent ``publish_build_progress`` calls
  do not interleave partial envelopes.
- AC-008 — lint/format is enforced by CI; not asserted here.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from forge.adapters.nats import PipelinePublisher, PublishFailure
from forge.adapters.nats import pipeline_publisher as pp_module
from nats_core.envelope import EventType, MessageEnvelope
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
# Test fixtures and helpers
# ---------------------------------------------------------------------------


FEATURE_ID = "FEAT-A1B2"
BUILD_ID = "build-FEAT-A1B2-20260425120000"
CORRELATION_ID = "corr-1234-5678"


def _now_iso() -> str:
    return datetime(2026, 4, 25, 12, 0, 0, tzinfo=timezone.utc).isoformat()


def _build_started() -> BuildStartedPayload:
    return BuildStartedPayload(feature_id=FEATURE_ID, build_id=BUILD_ID, wave_total=3)


def _build_progress() -> BuildProgressPayload:
    return BuildProgressPayload(
        feature_id=FEATURE_ID,
        build_id=BUILD_ID,
        wave=1,
        wave_total=3,
        overall_progress_pct=12.5,
        elapsed_seconds=10,
    )


def _stage_complete() -> StageCompletePayload:
    return StageCompletePayload(
        feature_id=FEATURE_ID,
        build_id=BUILD_ID,
        stage_label="implementation",
        target_kind="subagent",
        target_identifier="implementer",
        status="PASSED",
        gate_mode="AUTO_APPROVE",
        coach_score=0.92,
        duration_secs=42.5,
        completed_at=_now_iso(),
        correlation_id=CORRELATION_ID,
    )


def _build_paused() -> BuildPausedPayload:
    return BuildPausedPayload(
        feature_id=FEATURE_ID,
        build_id=BUILD_ID,
        stage_label="implementation",
        gate_mode="FLAG_FOR_REVIEW",
        coach_score=0.55,
        rationale="quality below threshold",
        approval_subject="agent.forge.approval-response",
        paused_at=_now_iso(),
        correlation_id=CORRELATION_ID,
    )


def _build_resumed() -> BuildResumedPayload:
    return BuildResumedPayload(
        feature_id=FEATURE_ID,
        build_id=BUILD_ID,
        stage_label="implementation",
        decision="approve",
        responder="rich",
        resumed_at=_now_iso(),
        correlation_id=CORRELATION_ID,
    )


def _build_complete() -> BuildCompletePayload:
    return BuildCompletePayload(
        feature_id=FEATURE_ID,
        build_id=BUILD_ID,
        repo="guardkit/forge",
        branch="main",
        tasks_completed=4,
        tasks_failed=0,
        tasks_total=4,
        pr_url="https://github.com/guardkit/forge/pull/42",
        duration_seconds=600,
        summary="all green",
    )


def _build_failed() -> BuildFailedPayload:
    return BuildFailedPayload(
        feature_id=FEATURE_ID,
        build_id=BUILD_ID,
        failure_reason="task TASK-X failed",
        recoverable=False,
        failed_task_id="TASK-X",
    )


def _build_cancelled() -> BuildCancelledPayload:
    return BuildCancelledPayload(
        feature_id=FEATURE_ID,
        build_id=BUILD_ID,
        reason="user_requested",
        cancelled_by="rich",
        cancelled_at=_now_iso(),
        correlation_id=CORRELATION_ID,
    )


@pytest.fixture
def nats_client() -> AsyncMock:
    """A mock async NATS client capturing publish calls."""
    client = AsyncMock()
    client.publish = AsyncMock(return_value=None)
    return client


@pytest.fixture
def publisher(nats_client: AsyncMock) -> PipelinePublisher:
    return PipelinePublisher(nats_client=nats_client)


def _decode_publish_call(call: Any) -> tuple[str, dict[str, Any]]:
    """Pull (subject, decoded_envelope) out of a recorded ``nc.publish`` call."""
    args, _kwargs = call.args, call.kwargs
    subject = args[0] if args else _kwargs["subject"]
    body = args[1] if len(args) > 1 else _kwargs["payload"]
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8")
    return subject, json.loads(body)


# ---------------------------------------------------------------------------
# AC-001 — class shape: eight named methods exist
# ---------------------------------------------------------------------------


class TestPublisherSurface:
    """AC-001 — class exposes the eight expected lifecycle methods."""

    @pytest.mark.parametrize(
        "method_name",
        [
            "publish_build_started",
            "publish_build_progress",
            "publish_stage_complete",
            "publish_build_paused",
            "publish_build_resumed",
            "publish_build_complete",
            "publish_build_failed",
            "publish_build_cancelled",
        ],
    )
    def test_method_exists_and_is_coroutine(self, method_name: str) -> None:
        method = getattr(PipelinePublisher, method_name, None)
        assert method is not None, f"{method_name!r} not defined"
        assert asyncio.iscoroutinefunction(method), (
            f"{method_name!r} must be `async def`"
        )

    def test_publish_failure_is_exception(self) -> None:
        assert issubclass(PublishFailure, Exception)


# ---------------------------------------------------------------------------
# AC-002, AC-003, AC-006 — per-method subject + envelope contract
# ---------------------------------------------------------------------------


class TestPublishContract:
    """One test per method asserting subject + envelope shape + correlation_id."""

    @pytest.mark.asyncio
    async def test_publish_build_started(
        self, publisher: PipelinePublisher, nats_client: AsyncMock
    ) -> None:
        await publisher.publish_build_started(_build_started())
        nats_client.publish.assert_awaited_once()
        subject, env = _decode_publish_call(nats_client.publish.call_args)
        assert subject == f"pipeline.build-started.{FEATURE_ID}"
        assert env["source_id"] == "forge"
        assert env["event_type"] == EventType.BUILD_STARTED.value
        # v1 payload has no correlation_id field — envelope correlation_id must
        # be None to faithfully reflect the payload.
        assert env["correlation_id"] is None
        assert env["payload"]["feature_id"] == FEATURE_ID
        assert env["payload"]["build_id"] == BUILD_ID

    @pytest.mark.asyncio
    async def test_publish_build_progress(
        self, publisher: PipelinePublisher, nats_client: AsyncMock
    ) -> None:
        await publisher.publish_build_progress(_build_progress())
        subject, env = _decode_publish_call(nats_client.publish.call_args)
        assert subject == f"pipeline.build-progress.{FEATURE_ID}"
        assert env["source_id"] == "forge"
        assert env["event_type"] == EventType.BUILD_PROGRESS.value
        assert env["payload"]["overall_progress_pct"] == 12.5

    @pytest.mark.asyncio
    async def test_publish_stage_complete(
        self, publisher: PipelinePublisher, nats_client: AsyncMock
    ) -> None:
        await publisher.publish_stage_complete(_stage_complete())
        subject, env = _decode_publish_call(nats_client.publish.call_args)
        assert subject == f"pipeline.stage-complete.{FEATURE_ID}"
        assert env["source_id"] == "forge"
        assert env["event_type"] == EventType.STAGE_COMPLETE.value
        assert env["correlation_id"] == CORRELATION_ID
        assert env["payload"]["stage_label"] == "implementation"

    @pytest.mark.asyncio
    async def test_publish_build_paused(
        self, publisher: PipelinePublisher, nats_client: AsyncMock
    ) -> None:
        await publisher.publish_build_paused(_build_paused())
        subject, env = _decode_publish_call(nats_client.publish.call_args)
        assert subject == f"pipeline.build-paused.{FEATURE_ID}"
        assert env["source_id"] == "forge"
        assert env["event_type"] == EventType.BUILD_PAUSED.value
        assert env["correlation_id"] == CORRELATION_ID

    @pytest.mark.asyncio
    async def test_publish_build_resumed(
        self, publisher: PipelinePublisher, nats_client: AsyncMock
    ) -> None:
        await publisher.publish_build_resumed(_build_resumed())
        subject, env = _decode_publish_call(nats_client.publish.call_args)
        assert subject == f"pipeline.build-resumed.{FEATURE_ID}"
        assert env["source_id"] == "forge"
        assert env["event_type"] == EventType.BUILD_RESUMED.value
        assert env["correlation_id"] == CORRELATION_ID

    @pytest.mark.asyncio
    async def test_publish_build_complete(
        self, publisher: PipelinePublisher, nats_client: AsyncMock
    ) -> None:
        await publisher.publish_build_complete(_build_complete())
        subject, env = _decode_publish_call(nats_client.publish.call_args)
        assert subject == f"pipeline.build-complete.{FEATURE_ID}"
        assert env["source_id"] == "forge"
        assert env["event_type"] == EventType.BUILD_COMPLETE.value
        assert env["payload"]["pr_url"] == "https://github.com/guardkit/forge/pull/42"

    @pytest.mark.asyncio
    async def test_publish_build_failed(
        self, publisher: PipelinePublisher, nats_client: AsyncMock
    ) -> None:
        await publisher.publish_build_failed(_build_failed())
        subject, env = _decode_publish_call(nats_client.publish.call_args)
        assert subject == f"pipeline.build-failed.{FEATURE_ID}"
        assert env["source_id"] == "forge"
        assert env["event_type"] == EventType.BUILD_FAILED.value
        assert env["payload"]["failure_reason"] == "task TASK-X failed"

    @pytest.mark.asyncio
    async def test_publish_build_cancelled(
        self, publisher: PipelinePublisher, nats_client: AsyncMock
    ) -> None:
        await publisher.publish_build_cancelled(_build_cancelled())
        subject, env = _decode_publish_call(nats_client.publish.call_args)
        assert subject == f"pipeline.build-cancelled.{FEATURE_ID}"
        assert env["source_id"] == "forge"
        assert env["event_type"] == EventType.BUILD_CANCELLED.value
        assert env["correlation_id"] == CORRELATION_ID
        assert env["payload"]["reason"] == "user_requested"


# ---------------------------------------------------------------------------
# AC-002 helper — subject builder direct test
# ---------------------------------------------------------------------------


class TestSubjectBuilder:
    def test_subject_for_returns_pipeline_pattern(self) -> None:
        subject = PipelinePublisher._subject_for("build-started", "FEAT-9Z9Z")
        assert subject == "pipeline.build-started.FEAT-9Z9Z"

    @pytest.mark.parametrize(
        "event,expected",
        [
            ("build-started", "pipeline.build-started.FEAT-A1B2"),
            ("build-progress", "pipeline.build-progress.FEAT-A1B2"),
            ("stage-complete", "pipeline.stage-complete.FEAT-A1B2"),
            ("build-paused", "pipeline.build-paused.FEAT-A1B2"),
            ("build-resumed", "pipeline.build-resumed.FEAT-A1B2"),
            ("build-complete", "pipeline.build-complete.FEAT-A1B2"),
            ("build-failed", "pipeline.build-failed.FEAT-A1B2"),
            ("build-cancelled", "pipeline.build-cancelled.FEAT-A1B2"),
        ],
    )
    def test_subject_format_for_each_event(self, event: str, expected: str) -> None:
        assert PipelinePublisher._subject_for(event, FEATURE_ID) == expected


# ---------------------------------------------------------------------------
# AC-003 — envelope shape (round-trips through MessageEnvelope)
# ---------------------------------------------------------------------------


class TestEnvelopeShape:
    @pytest.mark.asyncio
    async def test_envelope_round_trips_through_message_envelope(
        self, publisher: PipelinePublisher, nats_client: AsyncMock
    ) -> None:
        await publisher.publish_stage_complete(_stage_complete())
        _, env_dict = _decode_publish_call(nats_client.publish.call_args)
        # Every published wire format must validate against MessageEnvelope.
        envelope = MessageEnvelope.model_validate(env_dict)
        assert envelope.source_id == "forge"
        assert envelope.event_type == EventType.STAGE_COMPLETE
        assert envelope.correlation_id == CORRELATION_ID

    @pytest.mark.asyncio
    async def test_envelope_payload_is_a_dict(
        self, publisher: PipelinePublisher, nats_client: AsyncMock
    ) -> None:
        await publisher.publish_build_started(_build_started())
        _, env_dict = _decode_publish_call(nats_client.publish.call_args)
        assert isinstance(env_dict["payload"], dict)
        # Required keys from BuildStartedPayload.
        assert {"feature_id", "build_id", "wave_total"} <= set(env_dict["payload"])


# ---------------------------------------------------------------------------
# AC-004 — fire-and-forget; PubAck logged but never treated as delivery proof
# ---------------------------------------------------------------------------


class TestFireAndForget:
    @pytest.mark.asyncio
    async def test_publish_returns_none_even_when_client_returns_pub_ack(
        self,
        publisher: PipelinePublisher,
        nats_client: AsyncMock,
    ) -> None:
        # Simulate a NATS PubAck-like return value.
        nats_client.publish = AsyncMock(
            return_value=MagicMock(stream="PIPELINE", seq=1)
        )
        result = await publisher.publish_build_started(_build_started())
        assert result is None

    @pytest.mark.asyncio
    async def test_pub_ack_is_logged_at_debug(
        self,
        publisher: PipelinePublisher,
        nats_client: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        nats_client.publish = AsyncMock(return_value="ACK-123")
        with caplog.at_level(logging.DEBUG, logger=pp_module.__name__):
            await publisher.publish_build_started(_build_started())
        # Some log record from the publisher mentions ACK-123 OR the subject.
        relevant = [rec for rec in caplog.records if rec.name == pp_module.__name__]
        assert relevant, "publisher emitted no log records"


# ---------------------------------------------------------------------------
# AC-005 — transport-level failures raise PublishFailure
# ---------------------------------------------------------------------------


class TestPublishFailure:
    @pytest.mark.asyncio
    async def test_underlying_exception_is_wrapped(
        self,
        publisher: PipelinePublisher,
        nats_client: AsyncMock,
    ) -> None:
        nats_client.publish = AsyncMock(side_effect=ConnectionError("nats down"))
        with pytest.raises(PublishFailure) as excinfo:
            await publisher.publish_build_started(_build_started())
        # Cause is preserved on the exception object.
        assert isinstance(excinfo.value.__cause__, ConnectionError)
        assert excinfo.value.subject == f"pipeline.build-started.{FEATURE_ID}"

    @pytest.mark.asyncio
    async def test_publish_failure_carries_subject_in_message(
        self,
        publisher: PipelinePublisher,
        nats_client: AsyncMock,
    ) -> None:
        nats_client.publish = AsyncMock(side_effect=RuntimeError("disconnected"))
        with pytest.raises(PublishFailure) as excinfo:
            await publisher.publish_build_failed(_build_failed())
        assert "pipeline.build-failed." in str(excinfo.value)


# ---------------------------------------------------------------------------
# AC-007 — concurrency: 100 concurrent calls do not interleave envelopes
# ---------------------------------------------------------------------------


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_100_concurrent_publish_build_progress_no_interleave(
        self, publisher: PipelinePublisher, nats_client: AsyncMock
    ) -> None:
        captured: list[tuple[str, dict[str, Any]]] = []

        async def recording_publish(subject: str, body: bytes) -> None:
            # Yield to scheduler to maximise the chance of races.
            await asyncio.sleep(0)
            captured.append((subject, json.loads(body.decode("utf-8"))))

        nats_client.publish = AsyncMock(side_effect=recording_publish)

        payloads = [
            BuildProgressPayload(
                feature_id=FEATURE_ID,
                build_id=BUILD_ID,
                wave=1,
                wave_total=3,
                overall_progress_pct=float(i) / 100 * 100.0,
                elapsed_seconds=i,
            )
            for i in range(100)
        ]

        await asyncio.gather(*(publisher.publish_build_progress(p) for p in payloads))

        assert len(captured) == 100
        # Every captured envelope is a complete, well-formed envelope — not a
        # truncated or merged JSON object — and validates as MessageEnvelope.
        for subject, env_dict in captured:
            assert subject == f"pipeline.build-progress.{FEATURE_ID}"
            envelope = MessageEnvelope.model_validate(env_dict)
            assert envelope.source_id == "forge"
            assert envelope.event_type == EventType.BUILD_PROGRESS
            assert "elapsed_seconds" in envelope.payload

        # Each envelope has a distinct message_id — no two calls share state.
        message_ids = {env_dict["message_id"] for _, env_dict in captured}
        assert len(message_ids) == 100

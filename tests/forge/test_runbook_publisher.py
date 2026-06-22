"""Unit tests for :mod:`forge.adapters.nats.runbook_publisher`.

Test classes mirror the acceptance criteria of TASK-RBX-003:

- AC-001 — five publisher methods publish to ``runbook.{event}.{runbook_id}``
  with correct envelope, source_id, and correlation_id.
- AC-002 — transport-level failures raise :class:`PublishFailure`.
- AC-003 — fire-and-forget; no retry, no state mutation.
- AC-004 — PubAck is logged at DEBUG but never treated as delivery proof.
- AC-005 — lint/format enforced by CI.
- AC-006 — seam tests validate contract with TASK-RBX-002.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest

from forge.adapters.nats import PublishFailure
from forge.adapters.nats.pipeline_publisher import SOURCE_ID
from forge.adapters.nats.runbook_publisher import RunbookPublisher
from nats_core.envelope import EventType, MessageEnvelope, payload_class_for_event_type
from nats_core.events import (
    EscalatedPayload,
    RunbookCompletePayload,
    RunbookStartedPayload,
    StepResultPayload,
    StepStartedPayload,
)
from forge.persistence.repositories.runbook_models import StepStatus

# ---------------------------------------------------------------------------
# Test fixtures and helpers
# ---------------------------------------------------------------------------

RUNBOOK_ID = "rb-12345"
CORRELATION_ID = "corr-abcd-efgh"


def _runbook_started() -> RunbookStartedPayload:
    return RunbookStartedPayload(
        runbook_id=RUNBOOK_ID,
        target="deployment-123",
        step_count=5,
        correlation_id=CORRELATION_ID,
    )


def _step_started() -> StepStartedPayload:
    return StepStartedPayload(
        runbook_id=RUNBOOK_ID,
        sequence_index=0,
        step_type="shell",
        correlation_id=CORRELATION_ID,
    )


def _step_result() -> StepResultPayload:
    return StepResultPayload(
        runbook_id=RUNBOOK_ID,
        sequence_index=0,
        step_type="shell",
        status="passed",
        result={"output": "success"},
        correlation_id=CORRELATION_ID,
    )


def _runbook_complete() -> RunbookCompletePayload:
    return RunbookCompletePayload(
        runbook_id=RUNBOOK_ID,
        step_count=5,
        correlation_id=CORRELATION_ID,
    )


def _escalated() -> EscalatedPayload:
    return EscalatedPayload(
        runbook_id=RUNBOOK_ID,
        sequence_index=2,
        reason="step_failed",
        correlation_id=CORRELATION_ID,
    )


@pytest.fixture
def nats_client() -> AsyncMock:
    """A mock async NATS client capturing publish calls."""
    client = AsyncMock()
    client.publish = AsyncMock(return_value=None)
    return client


@pytest.fixture
def publisher(nats_client: AsyncMock) -> RunbookPublisher:
    return RunbookPublisher(nats_client=nats_client)


def _decode_publish_call(call: Any) -> tuple[str, dict[str, Any]]:
    """Pull (subject, decoded_envelope) out of a recorded ``nc.publish`` call."""
    args, _kwargs = call.args, call.kwargs
    subject = args[0] if args else _kwargs["subject"]
    body = args[1] if len(args) > 1 else _kwargs["payload"]
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8")
    return subject, json.loads(body)


# ---------------------------------------------------------------------------
# AC-001 — class shape: five named methods exist
# ---------------------------------------------------------------------------


class TestPublisherSurface:
    """AC-001 — class exposes the five expected runbook lifecycle methods."""

    @pytest.mark.parametrize(
        "method_name",
        [
            "publish_runbook_started",
            "publish_step_started",
            "publish_step_result",
            "publish_runbook_complete",
            "publish_escalated",
        ],
    )
    def test_method_exists_and_is_coroutine(self, method_name: str) -> None:
        method = getattr(RunbookPublisher, method_name, None)
        assert method is not None, f"{method_name!r} not defined"
        assert asyncio.iscoroutinefunction(method), (
            f"{method_name!r} must be `async def`"
        )


# ---------------------------------------------------------------------------
# AC-001, AC-003 — per-method subject + envelope contract
# ---------------------------------------------------------------------------


class TestPublishContract:
    """One test per method asserting subject + envelope shape + correlation_id."""

    @pytest.mark.asyncio
    async def test_publish_runbook_started(
        self, publisher: RunbookPublisher, nats_client: AsyncMock
    ) -> None:
        await publisher.publish_runbook_started(_runbook_started())
        nats_client.publish.assert_awaited_once()
        subject, env = _decode_publish_call(nats_client.publish.call_args)
        assert subject == f"runbook.runbook-started.{RUNBOOK_ID}"
        assert env["source_id"] == SOURCE_ID
        assert env["event_type"] == EventType.RUNBOOK_STARTED.value
        assert env["correlation_id"] == CORRELATION_ID
        assert env["payload"]["runbook_id"] == RUNBOOK_ID
        assert env["payload"]["target"] == "deployment-123"

    @pytest.mark.asyncio
    async def test_publish_step_started(
        self, publisher: RunbookPublisher, nats_client: AsyncMock
    ) -> None:
        await publisher.publish_step_started(_step_started())
        subject, env = _decode_publish_call(nats_client.publish.call_args)
        assert subject == f"runbook.step-started.{RUNBOOK_ID}"
        assert env["source_id"] == SOURCE_ID
        assert env["event_type"] == EventType.STEP_STARTED.value
        assert env["correlation_id"] == CORRELATION_ID
        assert env["payload"]["sequence_index"] == 0

    @pytest.mark.asyncio
    async def test_publish_step_result(
        self, publisher: RunbookPublisher, nats_client: AsyncMock
    ) -> None:
        await publisher.publish_step_result(_step_result())
        subject, env = _decode_publish_call(nats_client.publish.call_args)
        assert subject == f"runbook.step-result.{RUNBOOK_ID}"
        assert env["source_id"] == SOURCE_ID
        assert env["event_type"] == EventType.STEP_RESULT.value
        assert env["correlation_id"] == CORRELATION_ID
        assert env["payload"]["status"] == "passed"

    @pytest.mark.asyncio
    async def test_publish_runbook_complete(
        self, publisher: RunbookPublisher, nats_client: AsyncMock
    ) -> None:
        await publisher.publish_runbook_complete(_runbook_complete())
        subject, env = _decode_publish_call(nats_client.publish.call_args)
        assert subject == f"runbook.runbook-complete.{RUNBOOK_ID}"
        assert env["source_id"] == SOURCE_ID
        assert env["event_type"] == EventType.RUNBOOK_COMPLETE.value
        assert env["correlation_id"] == CORRELATION_ID

    @pytest.mark.asyncio
    async def test_publish_escalated(
        self, publisher: RunbookPublisher, nats_client: AsyncMock
    ) -> None:
        await publisher.publish_escalated(_escalated())
        subject, env = _decode_publish_call(nats_client.publish.call_args)
        assert subject == f"runbook.escalated.{RUNBOOK_ID}"
        assert env["source_id"] == SOURCE_ID
        assert env["event_type"] == EventType.ESCALATED.value
        assert env["correlation_id"] == CORRELATION_ID
        assert env["payload"]["reason"] == "step_failed"


# ---------------------------------------------------------------------------
# AC-001 helper — subject builder direct test
# ---------------------------------------------------------------------------


class TestSubjectBuilder:
    def test_subject_for_returns_runbook_pattern(self) -> None:
        subject = RunbookPublisher._subject_for("runbook-started", "rb-9999")
        assert subject == "runbook.runbook-started.rb-9999"

    @pytest.mark.parametrize(
        "event,expected",
        [
            ("runbook-started", f"runbook.runbook-started.{RUNBOOK_ID}"),
            ("step-started", f"runbook.step-started.{RUNBOOK_ID}"),
            ("step-result", f"runbook.step-result.{RUNBOOK_ID}"),
            ("runbook-complete", f"runbook.runbook-complete.{RUNBOOK_ID}"),
            ("escalated", f"runbook.escalated.{RUNBOOK_ID}"),
        ],
    )
    def test_subject_format_for_each_event(self, event: str, expected: str) -> None:
        assert RunbookPublisher._subject_for(event, RUNBOOK_ID) == expected


# ---------------------------------------------------------------------------
# AC-003 — envelope shape (round-trips through MessageEnvelope)
# ---------------------------------------------------------------------------


class TestEnvelopeShape:
    @pytest.mark.asyncio
    async def test_envelope_round_trips_through_message_envelope(
        self, publisher: RunbookPublisher, nats_client: AsyncMock
    ) -> None:
        await publisher.publish_step_result(_step_result())
        _, env_dict = _decode_publish_call(nats_client.publish.call_args)
        # Every published wire format must validate against MessageEnvelope.
        envelope = MessageEnvelope.model_validate(env_dict)
        assert envelope.source_id == SOURCE_ID
        assert envelope.event_type == EventType.STEP_RESULT
        assert envelope.correlation_id == CORRELATION_ID

    @pytest.mark.asyncio
    async def test_envelope_payload_is_a_dict(
        self, publisher: RunbookPublisher, nats_client: AsyncMock
    ) -> None:
        await publisher.publish_runbook_started(_runbook_started())
        _, env_dict = _decode_publish_call(nats_client.publish.call_args)
        assert isinstance(env_dict["payload"], dict)
        # Required keys from RunbookStartedPayload.
        assert {"runbook_id", "target", "step_count", "correlation_id"} <= set(
            env_dict["payload"]
        )


# ---------------------------------------------------------------------------
# AC-004 — fire-and-forget; PubAck logged but never treated as delivery proof
# ---------------------------------------------------------------------------


class TestFireAndForget:
    @pytest.mark.asyncio
    async def test_publish_returns_none_even_when_client_returns_pub_ack(
        self,
        publisher: RunbookPublisher,
        nats_client: AsyncMock,
    ) -> None:
        # Simulate a NATS PubAck-like return value.
        from unittest.mock import MagicMock

        nats_client.publish = AsyncMock(return_value=MagicMock(stream="RUNBOOK", seq=1))
        result = await publisher.publish_runbook_started(_runbook_started())
        assert result is None

    @pytest.mark.asyncio
    async def test_pub_ack_is_logged_at_debug(
        self,
        publisher: RunbookPublisher,
        nats_client: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        nats_client.publish = AsyncMock(return_value="ACK-456")
        with caplog.at_level(logging.DEBUG):
            await publisher.publish_runbook_started(_runbook_started())
        # Publisher should log something at DEBUG mentioning the subject or ack.
        relevant = [
            rec
            for rec in caplog.records
            if "forge.adapters.nats.runbook_publisher" in rec.name
        ]
        assert relevant, "publisher emitted no log records"


# ---------------------------------------------------------------------------
# AC-002 — transport-level failures raise PublishFailure
# ---------------------------------------------------------------------------


class TestPublishFailure:
    @pytest.mark.asyncio
    async def test_underlying_exception_is_wrapped(
        self,
        publisher: RunbookPublisher,
        nats_client: AsyncMock,
    ) -> None:
        nats_client.publish = AsyncMock(side_effect=ConnectionError("nats down"))
        with pytest.raises(PublishFailure) as excinfo:
            await publisher.publish_runbook_started(_runbook_started())
        # Cause is preserved on the exception object.
        assert isinstance(excinfo.value.__cause__, ConnectionError)
        assert excinfo.value.subject == f"runbook.runbook-started.{RUNBOOK_ID}"

    @pytest.mark.asyncio
    async def test_publish_failure_carries_subject_in_message(
        self,
        publisher: RunbookPublisher,
        nats_client: AsyncMock,
    ) -> None:
        nats_client.publish = AsyncMock(side_effect=RuntimeError("disconnected"))
        with pytest.raises(PublishFailure) as excinfo:
            await publisher.publish_step_result(_step_result())
        assert "runbook.step-result." in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_publish_failure_is_logged_before_raising(
        self,
        publisher: RunbookPublisher,
        nats_client: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        nats_client.publish = AsyncMock(side_effect=RuntimeError("connection lost"))
        with caplog.at_level(logging.WARNING):
            with pytest.raises(PublishFailure):
                await publisher.publish_escalated(_escalated())
        # Should have logged at WARNING before raising.
        relevant = [
            rec
            for rec in caplog.records
            if rec.levelname == "WARNING"
            and "forge.adapters.nats.runbook_publisher" in rec.name
        ]
        assert relevant, "no WARNING log before PublishFailure"


# ---------------------------------------------------------------------------
# AC-003 — no retry, single publish per call
# ---------------------------------------------------------------------------


class TestNoRetry:
    @pytest.mark.asyncio
    async def test_publish_is_called_exactly_once_on_success(
        self, publisher: RunbookPublisher, nats_client: AsyncMock
    ) -> None:
        await publisher.publish_runbook_started(_runbook_started())
        assert nats_client.publish.await_count == 1

    @pytest.mark.asyncio
    async def test_publish_is_called_exactly_once_on_failure(
        self, publisher: RunbookPublisher, nats_client: AsyncMock
    ) -> None:
        nats_client.publish = AsyncMock(side_effect=RuntimeError("fail"))
        with pytest.raises(PublishFailure):
            await publisher.publish_step_started(_step_started())
        # Should not retry.
        assert nats_client.publish.await_count == 1


# ---------------------------------------------------------------------------
# §4 Seam Tests — validate runbook_lifecycle_events contract
# ---------------------------------------------------------------------------


@pytest.mark.seam
@pytest.mark.integration_contract("runbook_lifecycle_events")
def test_every_runbook_event_type_has_a_registered_payload() -> None:
    """All five runbook EventType members resolve to a payload class.

    Contract: payload_class_for_event_type must not raise for any runbook
    event the publisher emits.
    Producer: TASK-RBX-002
    """
    runbook_events = [
        EventType.RUNBOOK_STARTED,
        EventType.STEP_STARTED,
        EventType.STEP_RESULT,
        EventType.RUNBOOK_COMPLETE,
        EventType.ESCALATED,
    ]
    for event_type in runbook_events:
        # Must not raise KeyError — registry entry required.
        assert payload_class_for_event_type(event_type) is not None


@pytest.mark.seam
@pytest.mark.integration_contract("runbook_lifecycle_events")
def test_step_result_status_vocabulary_matches_step_status() -> None:
    """StepResultPayload.status admits exactly the StepStatus value set.

    Contract: the payload's status set must equal {s.value for s in StepStatus}.
    Producer: TASK-RBX-002
    """
    valid = {s.value for s in StepStatus}
    for value in valid:
        StepResultPayload(
            runbook_id="rb",
            sequence_index=0,
            step_type="shell",
            status=value,
            result=None,
            correlation_id="c",
        )
    with pytest.raises(Exception):
        StepResultPayload(
            runbook_id="rb",
            sequence_index=0,
            step_type="shell",
            status="not_a_real_status",
            result=None,
            correlation_id="c",
        )

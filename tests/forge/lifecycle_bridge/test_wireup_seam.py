"""Seam test for the §4 STREAM_EVENT_SCHEMA contract (TASK-FRR-PEB-004).

This module pins the integration contract between TASK-FRR-PEB-003
(producer — :class:`StreamEventTranslator`) and TASK-FRR-PEB-004
(consumer — :class:`LifecycleBridgeWireup`).

Why a dedicated seam file?
--------------------------

The seam test asserts the **shape** of the artefact at the
producer/consumer boundary, independent of any consumer-side observer
loop wiring. It is intentionally separated from the consumer's
behavioural test suite (``test_wireup.py``) so:

* A future producer-side change that breaks the contract fails this
  file in isolation, surfacing the contract regression rather than
  cascading into observer-loop test failures that obscure the root
  cause.
* The ``@pytest.mark.seam`` marker filter (``pytest -m seam``) selects
  every contract boundary in the codebase in one pass — this file is
  one entry in that boundary inventory.

Producer artefact: typed :class:`PipelineEvent` payload.
Consumer assertion: payload is one of the documented typed classes,
``correlation_id`` is non-empty, and ``correlation_id`` matches the
inbound :class:`BuildContext`'s value.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from langgraph_sdk.schema import StreamPart
from nats_core.events import (
    BuildCancelledPayload,
    BuildCompletePayload,
    BuildFailedPayload,
    BuildPausedPayload,
    BuildResumedPayload,
    BuildStartedPayload,
    StageCompletePayload,
)

from forge.lifecycle_bridge.bridge import BuildContext
from forge.lifecycle_bridge.translation import (
    StreamEventTranslator,
    VALUES_STREAM_EVENT,
)


def _state_part(
    feature_id: str,
    *,
    lifecycle: str,
    build_id: str = "build-FEAT-SEAM-001-20260507120000",
) -> StreamPart:
    """Build a canonical ``stream_mode='values'`` SSE part."""
    return StreamPart(
        event=VALUES_STREAM_EVENT,
        data={
            "async_tasks": {
                feature_id: {
                    "feature_id": feature_id,
                    "build_id": build_id,
                    "lifecycle": lifecycle,
                    "wave_total": 1,
                    "wave_index": 0,
                    "task_index": 0,
                    "tasks_completed": 0,
                    "tasks_failed": 0,
                    "waiting_for": None,
                    "last_coach_score": None,
                }
            }
        },
        id=None,
    )


@pytest.fixture()
def translator() -> StreamEventTranslator:
    return StreamEventTranslator()


@pytest.fixture()
def build_context() -> BuildContext:
    return BuildContext(
        feature_id="FEAT-SEAM-001",
        thread_id="thread-seam",
        run_id="run-seam",
        correlation_id="corr-seam",
        deadline_at=datetime.now(UTC) + timedelta(seconds=300),
    )


# ---------------------------------------------------------------------------
# §4 STREAM_EVENT_SCHEMA contract
# ---------------------------------------------------------------------------


class TestStreamEventSchemaSeam:
    """Verify the §4 STREAM_EVENT_SCHEMA contract at the boundary.

    Producer: TASK-FRR-PEB-003 — :class:`StreamEventTranslator`.
    Consumer: TASK-FRR-PEB-004 — :class:`LifecycleBridgeWireup`.
    """

    @pytest.mark.seam
    def test_stream_event_schema_format(
        self, translator: StreamEventTranslator, build_context: BuildContext
    ) -> None:
        """Verify STREAM_EVENT_SCHEMA matches the expected format.

        Contract: each translator output is a typed ``PipelineEvent``
        with ``correlation_id`` always populated and matching the
        inbound :class:`BuildContext`'s value (F010C contract).

        We prime the translator with a ``starting`` snapshot (no emit)
        so the next ``running_wave`` snapshot triggers the first
        ``BuildStartedPayload`` — the canonical first edge in the
        ``STREAM_EVENT_SCHEMA`` lifecycle.
        """
        # Producer side: emit the first transition.
        translator.translate(
            _state_part(build_context.feature_id, lifecycle="starting"),
            build_context,
        )
        event = translator.translate(
            _state_part(build_context.feature_id, lifecycle="running_wave"),
            build_context,
        )

        # Consumer side: verify format matches the §4 contract.
        assert event is not None, (
            "STREAM_EVENT_SCHEMA must not be None for the canonical "
            "starting → running_wave transition"
        )
        assert isinstance(
            event,
            (
                BuildStartedPayload,
                StageCompletePayload,
                BuildCompletePayload,
                BuildFailedPayload,
                BuildPausedPayload,
                BuildResumedPayload,
                BuildCancelledPayload,
            ),
        ), f"Expected typed PipelineEvent, got: {type(event).__name__}"
        # Format assertion derived from §4 contract constraint:
        assert event.correlation_id, (
            f"correlation_id must be non-empty (§4 contract), "
            f"got: {event.correlation_id!r}"
        )
        assert event.correlation_id == build_context.correlation_id, (
            "correlation_id must match BuildContext (F010C contract)"
        )

    @pytest.mark.seam
    @pytest.mark.parametrize(
        "lifecycle,expected_type",
        [
            ("completed", BuildCompletePayload),
            ("failed", BuildFailedPayload),
            ("cancelled", BuildCancelledPayload),
        ],
    )
    def test_terminal_lifecycles_produce_typed_envelopes(
        self,
        translator: StreamEventTranslator,
        build_context: BuildContext,
        lifecycle: str,
        expected_type: type,
    ) -> None:
        """Verify each terminal lifecycle produces the expected typed payload.

        Locks the consumer-side dispatch table assumptions in
        :data:`forge.lifecycle_bridge.wireup.TERMINAL_PAYLOAD_TYPES`
        against drift in the producer's transition-detection logic.
        """
        event = translator.translate(
            _state_part(build_context.feature_id, lifecycle=lifecycle),
            build_context,
        )
        assert isinstance(event, expected_type), (
            f"lifecycle={lifecycle!r} expected {expected_type.__name__}, "
            f"got {type(event).__name__}"
        )
        assert event.correlation_id == build_context.correlation_id

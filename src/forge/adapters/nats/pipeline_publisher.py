"""Outbound lifecycle event publisher for the Forge pipeline.

Owns the eight publish methods described in
``docs/design/contracts/API-nats-pipeline-events.md §3`` — one per
subject in the ``pipeline.{event}.{feature_id}`` family. Every
envelope it produces is a :class:`nats_core.envelope.MessageEnvelope`
with ``source_id == "forge"`` and the payload's ``correlation_id``
threaded onto the envelope.

Publish semantics
-----------------

- **Fire-and-forget.** ``nc.publish`` returns when the wire-level write
  completes; PubAck (when emitted by JetStream) is logged at ``DEBUG``
  but **never** treated as proof of delivery. This is the LES1 parity
  rule: a publisher that confuses PubAck with consumer acknowledgement
  silently loses events on broker rebalance.
- **Transport failures raise :class:`PublishFailure`.** Callers (the
  pipeline state machine in TASK-NFI-007) catch + log the failure but
  must **not** roll back SQLite state — pipeline truth lives in SQLite,
  the NATS stream is a derived projection that downstream subscribers
  re-read from JetStream replay.
- **Source identity is fixed.** Every envelope carries
  ``source_id="forge"``; this constant is exported so tests can assert
  on it without re-deriving the value.

Concurrency
-----------

Each method builds its envelope, serialises to JSON, and calls
``nc.publish`` exactly once. Envelopes are constructed as local values
on the call frame, so two concurrent calls cannot interleave fields of
the same envelope. The underlying ``nats.aio.client.Client.publish``
serialises wire writes internally, so 100 concurrent
``publish_build_progress`` calls produce 100 well-formed, independent
envelopes on the wire — verified by ``test_pipeline_publisher.py``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

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

if TYPE_CHECKING:  # pragma: no cover - import-time only
    from nats.aio.client import Client as NATSClient

logger = logging.getLogger(__name__)

#: Identity stamped onto every envelope this publisher emits.
SOURCE_ID = "forge"

#: Fixed prefix for every subject in the lifecycle stream family.
SUBJECT_PREFIX = "pipeline"

__all__ = ["PipelinePublisher", "PublishFailure", "SOURCE_ID"]


class PublishFailure(RuntimeError):
    """Raised when a transport-level publish fails.

    The originating exception is preserved both as ``__cause__`` (via the
    ``raise ... from exc`` chain) and as the ``cause`` attribute below
    for callers that prefer attribute access over walking the chain.

    Attributes:
        subject: The NATS subject the publisher attempted to write to.
        cause: The underlying exception raised by the NATS client.
    """

    def __init__(self, subject: str, cause: BaseException) -> None:
        super().__init__(f"Failed to publish to {subject!r}: {cause}")
        self.subject = subject
        self.cause = cause


class PipelinePublisher:
    """Publishes the eight lifecycle events for a Forge build.

    The class is intentionally thin — it owns no scheduling or retry
    logic. It validates only that the caller passed the expected payload
    type by relying on Pydantic; subject construction and envelope
    wrapping are the only responsibilities. Callers (the pipeline state
    machine) decide *when* to publish each event.

    Args:
        nats_client: An async NATS client (typically
            ``nats.aio.client.Client``) with an awaitable ``publish``
            method. Injected at the application boundary so unit tests
            can substitute a mock.
    """

    # Map each method to its (subject-segment, EventType). Centralised so
    # the publisher methods stay one-liners and the table is auditable in
    # one place against API-nats-pipeline-events.md §3.1.
    _EVENT_TABLE: dict[str, tuple[str, EventType]] = {
        "publish_build_started": ("build-started", EventType.BUILD_STARTED),
        "publish_build_progress": ("build-progress", EventType.BUILD_PROGRESS),
        "publish_stage_complete": ("stage-complete", EventType.STAGE_COMPLETE),
        "publish_build_paused": ("build-paused", EventType.BUILD_PAUSED),
        "publish_build_resumed": ("build-resumed", EventType.BUILD_RESUMED),
        "publish_build_complete": ("build-complete", EventType.BUILD_COMPLETE),
        "publish_build_failed": ("build-failed", EventType.BUILD_FAILED),
        "publish_build_cancelled": ("build-cancelled", EventType.BUILD_CANCELLED),
    }

    def __init__(self, nats_client: NATSClient | Any) -> None:
        self._nc = nats_client

    # ------------------------------------------------------------------
    # Subject helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _subject_for(event_name: str, feature_id: str) -> str:
        """Build the canonical subject ``pipeline.{event}.{feature_id}``.

        Args:
            event_name: Hyphen-separated event name as it appears on the
                wire (e.g. ``"build-started"``, ``"stage-complete"``).
            feature_id: The ``FEAT-XXXX`` identifier of the build.

        Returns:
            The canonical subject string published to JetStream.
        """
        return f"{SUBJECT_PREFIX}.{event_name}.{feature_id}"

    # ------------------------------------------------------------------
    # Internal: build + publish a single envelope
    # ------------------------------------------------------------------

    async def _publish_envelope(
        self,
        *,
        event_name: str,
        event_type: EventType,
        payload: BaseModel,
    ) -> None:
        """Build the envelope and write it to NATS.

        Args:
            event_name: Subject segment (e.g. ``"build-started"``).
            event_type: Envelope ``event_type`` value.
            payload: The Pydantic payload model to wrap.

        Raises:
            PublishFailure: If the underlying NATS publish raises.
        """
        feature_id = getattr(payload, "feature_id", None)
        if not isinstance(feature_id, str) or not feature_id:
            # Payload models all carry feature_id; this is a defensive
            # guard for the rare case a caller passes a hand-rolled
            # BaseModel instead of one of the typed payloads above.
            msg = (
                f"payload of type {type(payload).__name__!r} is missing "
                "feature_id; cannot build subject"
            )
            raise ValueError(msg)

        # v1 payloads (BuildStarted/Progress/Complete/Failed) do not have
        # correlation_id. v2.2 payloads do. We thread whatever the
        # payload exposes so the envelope honours the producer's intent.
        correlation_id = getattr(payload, "correlation_id", None)

        subject = self._subject_for(event_name, feature_id)

        envelope = MessageEnvelope(
            source_id=SOURCE_ID,
            event_type=event_type,
            correlation_id=correlation_id,
            payload=payload.model_dump(mode="json"),
        )
        body = envelope.model_dump_json().encode("utf-8")

        try:
            ack = await self._nc.publish(subject, body)
        except Exception as exc:  # noqa: BLE001 — we re-raise as PublishFailure
            # Log first so operators see the underlying error even if a
            # caller swallows PublishFailure further up the stack.
            logger.warning(
                "pipeline publish failed subject=%s error=%s",
                subject,
                exc,
            )
            raise PublishFailure(subject, exc) from exc

        # PubAck is informational only. JetStream may or may not return
        # one depending on stream configuration; either way, do NOT treat
        # this as proof of delivery (LES1 parity rule).
        if ack is not None:
            logger.debug(
                "pipeline publish ack subject=%s ack=%r (informational only)",
                subject,
                ack,
            )
        else:
            logger.debug("pipeline publish ok subject=%s", subject)

    # ------------------------------------------------------------------
    # Public publisher methods — one per lifecycle subject
    # ------------------------------------------------------------------

    async def publish_build_started(self, payload: BuildStartedPayload) -> None:
        """Publish ``pipeline.build-started.{feature_id}`` (PREPARING → RUNNING)."""
        await self._publish_envelope(
            event_name="build-started",
            event_type=EventType.BUILD_STARTED,
            payload=payload,
        )

    async def publish_build_progress(self, payload: BuildProgressPayload) -> None:
        """Publish ``pipeline.build-progress.{feature_id}`` (heartbeat / wave)."""
        await self._publish_envelope(
            event_name="build-progress",
            event_type=EventType.BUILD_PROGRESS,
            payload=payload,
        )

    async def publish_stage_complete(self, payload: StageCompletePayload) -> None:
        """Publish ``pipeline.stage-complete.{feature_id}`` (per-stage commit)."""
        await self._publish_envelope(
            event_name="stage-complete",
            event_type=EventType.STAGE_COMPLETE,
            payload=payload,
        )

    async def publish_build_paused(self, payload: BuildPausedPayload) -> None:
        """Publish ``pipeline.build-paused.{feature_id}`` (gate fired)."""
        await self._publish_envelope(
            event_name="build-paused",
            event_type=EventType.BUILD_PAUSED,
            payload=payload,
        )

    async def publish_build_resumed(self, payload: BuildResumedPayload) -> None:
        """Publish ``pipeline.build-resumed.{feature_id}`` (after approval)."""
        await self._publish_envelope(
            event_name="build-resumed",
            event_type=EventType.BUILD_RESUMED,
            payload=payload,
        )

    async def publish_build_complete(self, payload: BuildCompletePayload) -> None:
        """Publish ``pipeline.build-complete.{feature_id}`` (terminal: PR open)."""
        await self._publish_envelope(
            event_name="build-complete",
            event_type=EventType.BUILD_COMPLETE,
            payload=payload,
        )

    async def publish_build_failed(self, payload: BuildFailedPayload) -> None:
        """Publish ``pipeline.build-failed.{feature_id}`` (terminal failure)."""
        await self._publish_envelope(
            event_name="build-failed",
            event_type=EventType.BUILD_FAILED,
            payload=payload,
        )

    async def publish_build_cancelled(self, payload: BuildCancelledPayload) -> None:
        """Publish ``pipeline.build-cancelled.{feature_id}`` (operator cancel)."""
        await self._publish_envelope(
            event_name="build-cancelled",
            event_type=EventType.BUILD_CANCELLED,
            payload=payload,
        )

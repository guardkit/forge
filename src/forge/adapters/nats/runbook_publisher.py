"""Outbound lifecycle event publisher for runbook execution.

Owns the five publish methods for runbook lifecycle events described in
TASK-RBX-003 — one per subject in the ``runbook.{event}.{runbook_id}``
family. Every envelope it produces is a
:class:`nats_core.envelope.MessageEnvelope` with ``source_id == "forge"``
(re-exported from :mod:`pipeline_publisher`) and the payload's
``correlation_id`` threaded onto the envelope.

Publish semantics
-----------------

- **Fire-and-forget.** ``nc.publish`` returns when the wire-level write
  completes; PubAck (when emitted by JetStream) is logged at ``DEBUG``
  but **never** treated as proof of delivery. This is the LES1 parity
  rule: a publisher that confuses PubAck with consumer acknowledgement
  silently loses events on broker rebalance.
- **Transport failures raise :class:`PublishFailure`.** Callers (the
  runbook executor in TASK-RBX-004) catch + log the failure but must
  **not** roll back state — runbook truth lives in persistence, the
  NATS stream is a derived projection that downstream subscribers
  re-read from JetStream replay.
- **Source identity is fixed.** Every envelope carries
  ``source_id="forge"`` (re-exported from
  :mod:`pipeline_publisher`); this constant is exported so tests can
  assert on it without re-deriving the value.

Concurrency
-----------

Each method builds its envelope, serialises to JSON, and calls
``nc.publish`` exactly once. Envelopes are constructed as local values
on the call frame, so two concurrent calls cannot interleave fields of
the same envelope. The underlying ``nats.aio.client.Client.publish``
serialises wire writes internally, so 100 concurrent
``publish_step_result`` calls produce 100 well-formed, independent
envelopes on the wire — verified by ``test_runbook_publisher.py``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from nats_core.envelope import EventType, MessageEnvelope
from nats_core.events import (
    EscalatedPayload,
    RunbookCompletePayload,
    RunbookStartedPayload,
    StepResultPayload,
    StepStartedPayload,
)

# Re-export PublishFailure and SOURCE_ID from pipeline_publisher rather
# than redefining them (TASK-RBX-003 spec requirement).
from forge.adapters.nats.pipeline_publisher import PublishFailure, SOURCE_ID

if TYPE_CHECKING:  # pragma: no cover - import-time only
    from nats.aio.client import Client as NATSClient

logger = logging.getLogger(__name__)

#: Fixed prefix for every subject in the runbook lifecycle stream family.
SUBJECT_PREFIX = "runbook"

__all__ = ["RunbookPublisher", "PublishFailure", "SOURCE_ID"]


class RunbookPublisher:
    """Publishes the five lifecycle events for a runbook execution.

    The class is intentionally thin — it owns no scheduling or retry
    logic. It validates only that the caller passed the expected payload
    type by relying on Pydantic; subject construction and envelope
    wrapping are the only responsibilities. Callers (the runbook
    executor) decide *when* to publish each event.

    Args:
        nats_client: An async NATS client (typically
            ``nats.aio.client.Client``) with an awaitable ``publish``
            method. Injected at the application boundary so unit tests
            can substitute a mock.
    """

    # Map each method to its (subject-segment, EventType). Centralised so
    # the publisher methods stay one-liners and the table is auditable in
    # one place against the runbook lifecycle event spec.
    _EVENT_TABLE: dict[str, tuple[str, EventType]] = {
        "publish_runbook_started": ("runbook-started", EventType.RUNBOOK_STARTED),
        "publish_step_started": ("step-started", EventType.STEP_STARTED),
        "publish_step_result": ("step-result", EventType.STEP_RESULT),
        "publish_runbook_complete": ("runbook-complete", EventType.RUNBOOK_COMPLETE),
        "publish_escalated": ("escalated", EventType.ESCALATED),
    }

    def __init__(self, nats_client: NATSClient | Any) -> None:
        self._nc = nats_client

    # ------------------------------------------------------------------
    # Subject helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _subject_for(event_name: str, runbook_id: str) -> str:
        """Build the canonical subject ``runbook.{event}.{runbook_id}``.

        Args:
            event_name: Hyphen-separated event name as it appears on the
                wire (e.g. ``"runbook-started"``, ``"step-result"``).
            runbook_id: The runbook identifier.

        Returns:
            The canonical subject string published to JetStream.
        """
        return f"{SUBJECT_PREFIX}.{event_name}.{runbook_id}"

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
            event_name: Subject segment (e.g. ``"runbook-started"``).
            event_type: Envelope ``event_type`` value.
            payload: The Pydantic payload model to wrap.

        Raises:
            PublishFailure: If the underlying NATS publish raises.
        """
        runbook_id = getattr(payload, "runbook_id", None)
        if not isinstance(runbook_id, str) or not runbook_id:
            # Payload models all carry runbook_id; this is a defensive
            # guard for the rare case a caller passes a hand-rolled
            # BaseModel instead of one of the typed payloads above.
            msg = (
                f"payload of type {type(payload).__name__!r} is missing "
                "runbook_id; cannot build subject"
            )
            raise ValueError(msg)

        # All runbook payloads carry correlation_id; thread it onto the
        # envelope so the correlation chain is preserved on the wire.
        correlation_id = getattr(payload, "correlation_id", None)

        subject = self._subject_for(event_name, runbook_id)

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
                "runbook publish failed subject=%s error=%s",
                subject,
                exc,
            )
            raise PublishFailure(subject, exc) from exc

        # PubAck is informational only. JetStream may or may not return
        # one depending on stream configuration; either way, do NOT treat
        # this as proof of delivery (LES1 parity rule).
        if ack is not None:
            logger.debug(
                "runbook publish ack subject=%s ack=%r (informational only)",
                subject,
                ack,
            )
        else:
            logger.debug("runbook publish ok subject=%s", subject)

    # ------------------------------------------------------------------
    # Public publisher methods — one per lifecycle subject
    # ------------------------------------------------------------------

    async def publish_runbook_started(self, payload: RunbookStartedPayload) -> None:
        """Publish ``runbook.runbook-started.{runbook_id}`` (executor starts)."""
        await self._publish_envelope(
            event_name="runbook-started",
            event_type=EventType.RUNBOOK_STARTED,
            payload=payload,
        )

    async def publish_step_started(self, payload: StepStartedPayload) -> None:
        """Publish ``runbook.step-started.{runbook_id}`` (step begins)."""
        await self._publish_envelope(
            event_name="step-started",
            event_type=EventType.STEP_STARTED,
            payload=payload,
        )

    async def publish_step_result(self, payload: StepResultPayload) -> None:
        """Publish ``runbook.step-result.{runbook_id}`` (step completes)."""
        await self._publish_envelope(
            event_name="step-result",
            event_type=EventType.STEP_RESULT,
            payload=payload,
        )

    async def publish_runbook_complete(self, payload: RunbookCompletePayload) -> None:
        """Publish ``runbook.runbook-complete.{runbook_id}`` (terminal: all steps done)."""
        await self._publish_envelope(
            event_name="runbook-complete",
            event_type=EventType.RUNBOOK_COMPLETE,
            payload=payload,
        )

    async def publish_escalated(self, payload: EscalatedPayload) -> None:
        """Publish ``runbook.escalated.{runbook_id}`` (escalation triggered)."""
        await self._publish_envelope(
            event_name="escalated",
            event_type=EventType.ESCALATED,
            payload=payload,
        )

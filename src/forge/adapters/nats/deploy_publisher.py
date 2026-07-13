"""Outbound deploy-domain lifecycle publisher (WS2-B8).

Owns the seven publish methods for the deploy stage's lifecycle events, one per
subject in the ``deploy.*.{correlation_id}`` family (``Topics.Deploy``,
nats-core 0.7.1 / B7 + the O-32 revert receipt). Mirrors
:class:`forge.adapters.nats.runbook_publisher.RunbookPublisher` exactly — thin,
fire-and-forget, PubAck is informational only (LES1 parity), transport failures
raise :class:`PublishFailure`, every envelope carries ``source_id="forge"``.

The payloads (``DeployQueued/Started/Complete/Failed/Reverted``,
``QAVerdictPayload``, ``LiveGateResultPayload``) are consumed from nats-core
0.7.1 **verbatim** — no schema edits here (WS2-B8 guardrail). These are
NOTIFICATIONS: forge's authoritative live-gate input stays the seam stdout
envelope; the bus payloads never trigger forge.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from nats_core.envelope import EventType, MessageEnvelope
from nats_core.events import (
    DeployCompletePayload,
    DeployFailedPayload,
    DeployQueuedPayload,
    DeployRevertedPayload,
    DeployStartedPayload,
    LiveGateResultPayload,
    QAVerdictPayload,
)
from nats_core.topics import Topics

# Re-export PublishFailure + SOURCE_ID from pipeline_publisher rather than
# redefining them (the RunbookPublisher precedent).
from forge.adapters.nats.pipeline_publisher import PublishFailure, SOURCE_ID

if TYPE_CHECKING:  # pragma: no cover - import-time only
    from nats.aio.client import Client as NATSClient

logger = logging.getLogger(__name__)

__all__ = ["DeployPublisher", "PublishFailure", "SOURCE_ID"]


class DeployPublisher:
    """Publishes the six deploy-domain lifecycle events (Topics.Deploy).

    Thin — owns no scheduling or retry logic; subject construction and envelope
    wrapping are its only responsibilities. Callers (the DeployStageRunner)
    decide *when* to publish each event.

    Args:
        nats_client: An async NATS client with an awaitable ``publish`` method.
            Injected at the application boundary so unit tests can substitute a
            mock (the RunbookPublisher/PipelinePublisher pattern).
    """

    # Method -> (subject template, EventType). Centralised so the publisher
    # methods stay one-liners and the table is auditable against Topics.Deploy.
    _EVENT_TABLE: dict[str, tuple[str, EventType]] = {
        "publish_deploy_queued": (
            Topics.Deploy.DEPLOY_QUEUED,
            EventType.DEPLOY_QUEUED,
        ),
        "publish_deploy_started": (
            Topics.Deploy.DEPLOY_STARTED,
            EventType.DEPLOY_STARTED,
        ),
        "publish_deploy_complete": (
            Topics.Deploy.DEPLOY_COMPLETE,
            EventType.DEPLOY_COMPLETE,
        ),
        "publish_deploy_failed": (
            Topics.Deploy.DEPLOY_FAILED,
            EventType.DEPLOY_FAILED,
        ),
        "publish_deploy_reverted": (
            Topics.Deploy.DEPLOY_REVERTED,
            EventType.DEPLOY_REVERTED,
        ),
        "publish_qa_verdict": (Topics.Deploy.QA_VERDICT, EventType.QA_VERDICT),
        "publish_live_gate_result": (
            Topics.Deploy.LIVE_GATE_RESULT,
            EventType.LIVE_GATE_RESULT,
        ),
    }

    def __init__(self, nats_client: NATSClient | Any) -> None:
        self._nc = nats_client

    # ------------------------------------------------------------------
    # Internal: build + publish a single envelope
    # ------------------------------------------------------------------

    async def _publish_envelope(
        self,
        *,
        subject_template: str,
        event_type: EventType,
        payload: BaseModel,
    ) -> None:
        """Build the envelope from the payload and write it to NATS.

        Raises:
            PublishFailure: If the underlying NATS publish raises.
        """
        correlation_id = getattr(payload, "correlation_id", None)
        if not isinstance(correlation_id, str) or not correlation_id:
            msg = (
                f"payload of type {type(payload).__name__!r} is missing "
                "correlation_id; cannot build a deploy subject"
            )
            raise ValueError(msg)

        subject = subject_template.format(correlation_id=correlation_id)

        envelope = MessageEnvelope(
            source_id=SOURCE_ID,
            event_type=event_type,
            correlation_id=correlation_id,
            payload=payload.model_dump(mode="json"),
        )
        body = envelope.model_dump_json().encode("utf-8")

        try:
            ack = await self._nc.publish(subject, body)
        except Exception as exc:  # noqa: BLE001 — re-raised as PublishFailure
            logger.warning("deploy publish failed subject=%s error=%s", subject, exc)
            raise PublishFailure(subject, exc) from exc

        # PubAck is informational only (LES1 parity rule) — never proof of
        # delivery.
        if ack is not None:
            logger.debug(
                "deploy publish ack subject=%s ack=%r (informational only)",
                subject,
                ack,
            )
        else:
            logger.debug("deploy publish ok subject=%s", subject)

    # ------------------------------------------------------------------
    # Public publisher methods — one per lifecycle subject
    # ------------------------------------------------------------------

    async def publish_deploy_queued(self, payload: DeployQueuedPayload) -> None:
        """Publish ``deploy.queued.{correlation_id}`` (DEPLOY stage enqueued)."""
        await self._publish_envelope(
            subject_template=Topics.Deploy.DEPLOY_QUEUED,
            event_type=EventType.DEPLOY_QUEUED,
            payload=payload,
        )

    async def publish_deploy_started(self, payload: DeployStartedPayload) -> None:
        """Publish ``deploy.started.{correlation_id}`` (DEPLOY stage begins)."""
        await self._publish_envelope(
            subject_template=Topics.Deploy.DEPLOY_STARTED,
            event_type=EventType.DEPLOY_STARTED,
            payload=payload,
        )

    async def publish_deploy_complete(self, payload: DeployCompletePayload) -> None:
        """Publish ``deploy.complete.{correlation_id}`` (DEPLOY succeeds)."""
        await self._publish_envelope(
            subject_template=Topics.Deploy.DEPLOY_COMPLETE,
            event_type=EventType.DEPLOY_COMPLETE,
            payload=payload,
        )

    async def publish_deploy_failed(self, payload: DeployFailedPayload) -> None:
        """Publish ``deploy.failed.{correlation_id}`` (DEPLOY fails)."""
        await self._publish_envelope(
            subject_template=Topics.Deploy.DEPLOY_FAILED,
            event_type=EventType.DEPLOY_FAILED,
            payload=payload,
        )

    async def publish_deploy_reverted(self, payload: DeployRevertedPayload) -> None:
        """Publish ``deploy.reverted.{correlation_id}`` (O-32 revert-on-gate-fail)."""
        await self._publish_envelope(
            subject_template=Topics.Deploy.DEPLOY_REVERTED,
            event_type=EventType.DEPLOY_REVERTED,
            payload=payload,
        )

    async def publish_qa_verdict(self, payload: QAVerdictPayload) -> None:
        """Publish ``deploy.qa-verdict.{correlation_id}`` (overall QA verdict)."""
        await self._publish_envelope(
            subject_template=Topics.Deploy.QA_VERDICT,
            event_type=EventType.QA_VERDICT,
            payload=payload,
        )

    async def publish_live_gate_result(self, payload: LiveGateResultPayload) -> None:
        """Publish ``deploy.live-gate-result.{correlation_id}`` (run detail)."""
        await self._publish_envelope(
            subject_template=Topics.Deploy.LIVE_GATE_RESULT,
            event_type=EventType.LIVE_GATE_RESULT,
            payload=payload,
        )

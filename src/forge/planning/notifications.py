"""Outbound planning-notification projection (TASK-SPL003F-001, part 4).

Projects the durable Slack thread anchor (``parent_request_id``) and the
originating member id (``target_user``) into the outbound
``jarvis.notification.slack`` ``NotificationPayload`` so jarvis threads Mode P's
messages into the originating conversation.

The anchor fields (``parent_request_id`` / ``target_user`` / ``thread_ts``)
landed in nats-core 0.7.0 (Session I / ASSUM-001). Before they existed jarvis
degraded to a top-level channel post; the projection degrades the same way
(anchor ``None`` → unthreaded, never dropped) so a run row without an anchor
still notifies.
"""

from __future__ import annotations

from nats_core.envelope import EventType, MessageEnvelope
from nats_core.events import NotificationPayload

__all__ = ["build_planning_notification_envelope"]


def build_planning_notification_envelope(
    *,
    correlation_id: str,
    message: str,
    level: str = "info",
    parent_request_id: str | None = None,
    target_user: str | None = None,
) -> MessageEnvelope:
    """Build a wire-valid ``jarvis.notification.slack`` envelope for Mode P.

    Args:
        correlation_id: The planning run correlation id.
        message: The human-facing message body.
        level: ``info`` / ``warning`` / ``error``.
        parent_request_id: Durable Slack thread anchor (planning_runs row);
            ``None`` degrades to a top-level channel post.
        target_user: Originating member id to mention (planning_runs row).

    Returns:
        A :class:`MessageEnvelope` carrying the projected ``NotificationPayload``.
    """
    payload = NotificationPayload(
        message=message,
        level=level,  # type: ignore[arg-type]
        adapter="slack",
        correlation_id=correlation_id,
        parent_request_id=parent_request_id,
        thread_ts=parent_request_id,
        target_user=target_user,
    )
    return MessageEnvelope(
        source_id="forge",
        event_type=EventType.NOTIFICATION,
        correlation_id=correlation_id,
        payload=payload.model_dump(mode="json"),
    )

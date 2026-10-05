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

import logging
from typing import Any, Awaitable, Callable

from nats_core.envelope import EventType, MessageEnvelope
from nats_core.events import NotificationPayload

__all__ = [
    "NOTIFICATION_SUBJECT",
    "BuildThreadReply",
    "answer_build_thread",
    "build_planning_notification_envelope",
    "build_not_restarted_reply",
    "build_refused_reply",
    "build_started_reply",
    "gate_ended_reason",
    "make_build_thread_reply",
]

logger = logging.getLogger(__name__)

#: The subject jarvis renders factory notifications from.
NOTIFICATION_SUBJECT = "jarvis.notification.slack"


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


# ---------------------------------------------------------------------------
# Answers in the thread a build was handed over from (register-projects
# design, 5 October 2026, part 3)
# ---------------------------------------------------------------------------

BuildThreadReply = Callable[..., Awaitable[None]]
"""``async (payload, message, *, level="info") -> None`` — one anchored
notification about a build request, in the conversation it came from."""


def make_build_thread_reply(nats_client: Any) -> BuildThreadReply:
    """The build route's thread reply, on the daemon's shared client.

    The same anchored notification the queue commands answer with: the build
    request's ``parent_request_id`` (the Slack message it was typed as) is the
    thread anchor. Only a request that came through Slack
    (``originating_adapter == "slack"``) and carries one is answered. Jarvis's
    chat ``queue_build`` tool sets ``parent_request_id`` to its own dispatch
    or session id, which is not a Slack message, so it is never used as a
    thread; every other build caller sends none. Nothing new is published for
    any of them.
    """

    async def reply(payload: Any, message: str, *, level: str = "info") -> None:
        anchor = getattr(payload, "parent_request_id", None)
        if not anchor or getattr(payload, "originating_adapter", None) != "slack":
            return
        envelope = build_planning_notification_envelope(
            correlation_id=str(getattr(payload, "correlation_id", "") or ""),
            message=message,
            level=level,
            parent_request_id=str(anchor),
        )
        await nats_client.publish(
            NOTIFICATION_SUBJECT, envelope.model_dump_json().encode("utf-8")
        )

    return reply


async def answer_build_thread(
    reply: BuildThreadReply | None,
    payload: Any,
    message: str,
    *,
    level: str = "info",
) -> None:
    """Answer a build request in its thread. Never raises; never blocks.

    Nothing happens when no reply is wired or the request carries no
    ``parent_request_id``. A failed publish is logged and swallowed: the
    answer is a courtesy, and the build route's acknowledgement and refusal
    events stand on their own.
    """
    if reply is None or not getattr(payload, "parent_request_id", None):
        return
    try:
        await reply(payload, message, level=level)
    except Exception as exc:  # noqa: BLE001 — an answer never breaks a build
        logger.warning(
            "build thread reply: publish raised (%s: %s) for feature_id=%s "
            "correlation_id=%s; continuing",
            type(exc).__name__,
            exc,
            getattr(payload, "feature_id", None),
            getattr(payload, "correlation_id", None),
        )


def build_refused_reply(feature_id: str, reason: str) -> str:
    """The one sentence a refused build request is answered with."""
    reason = str(reason).strip().rstrip(".") or "no reason was given"
    return f"{feature_id} was not started: {reason}."


def gate_ended_reason(outcome: Any) -> str:
    """Why a build-start card ended a build, in plain words."""
    value = str(getattr(outcome, "value", outcome))
    if value == "CANCELLED":
        return "the build-start card was declined"
    if value == "TIMED_OUT":
        return "the build-start card timed out"
    return "the build-start check stopped it"


def build_not_restarted_reply(feature_id: str, reason: str) -> str:
    """The line for a build already said to be building, stopped on restart.

    Said once, when a build that launched (and was answered "Building") is
    recovered after a restart and its build-start card then ends it.
    """
    reason = str(reason).strip().rstrip(".") or "no reason was given"
    return f"{feature_id} was not restarted: {reason}."


def build_started_reply(
    feature_id: str, repo: str, branch: str, commit: str | None
) -> str:
    """The one line an accepted hand-over is answered with."""
    text = f"Building {feature_id} for {repo} from {branch}"
    if commit:
        text += f" at {commit[:7]}"
    return text

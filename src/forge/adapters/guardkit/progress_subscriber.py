"""NATS progress-stream subscriber for the GuardKit adapter (TASK-GCI-005).

This module owns Forge's per-invocation subscription to
``pipeline.stage-complete.{build_id}.{subcommand}`` and exposes the most
recent :class:`GuardKitProgressEvent` per stream key for ``forge status``
and the live progress view.

This is **telemetry only** — the authoritative completion result still
flows through the synchronous :class:`forge.adapters.guardkit.models.GuardKitResult`
returned from ``forge.adapters.guardkit.run()``. A missing or unavailable
progress stream **must not** fail the surrounding call (Scenario "The
authoritative result still returns when progress streaming is
unavailable"). All failure modes — ``None`` client, transport failure,
malformed payload, broken unsubscribe — are translated into a structured
warning on the sink and a log entry; the surrounding ``async with``
block always proceeds.

Contract anchors:

- ``docs/design/contracts/API-subprocess.md`` §3.2 — progress-stream
  integration.
- ``docs/design/contracts/API-nats-pipeline-events.md`` §3.1 — subject
  family for ``pipeline.stage-complete.*``.

Design notes:

- The sink is bounded with :class:`collections.deque` ``maxlen=N`` so a
  fast producer cannot grow it unbounded during a slow subscriber.
  Append on a full deque drops the oldest entry — exactly the
  back-pressure semantics required by AC-002.
- The subscribe path is generic: any client matching the
  ``subscribe(topic, callback)`` shape used by
  :class:`forge.adapters.nats.client.NATSClient` is accepted. This
  keeps the unit tests free of nats-py imports.
- The async context manager always yields exactly once, even when the
  subscribe call fails — callers can rely on the ``async with`` body
  running regardless of broker health.
"""

from __future__ import annotations

import logging
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Final,
    Protocol,
    runtime_checkable,
)

from nats_core.envelope import MessageEnvelope
from pydantic import ValidationError

from forge.adapters.guardkit.progress import GuardKitProgressEvent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants pinned to the API contract
# ---------------------------------------------------------------------------

#: Subject prefix per ``API-nats-pipeline-events.md §3.1``. The full
#: subject for one invocation is
#: ``pipeline.stage-complete.{build_id}.{subcommand}``.
SUBJECT_PREFIX: Final[str] = "pipeline.stage-complete"

#: Default cap on the number of events retained per ``(build_id,
#: subcommand)`` key. The implementation note in the task brief says the
#: bound is an implementation detail — what matters is that "most
#: recent" is observable. Fifty is generous for a live-status view yet
#: tight enough that a runaway producer cannot grow memory unbounded.
DEFAULT_MAX_EVENTS_PER_STREAM: Final[int] = 50

#: Structured warning code recorded on the sink when the broker is
#: unreachable (``nats_client is None`` or :meth:`subscribe` raised).
#: The surrounding GuardKit call still proceeds — see module docstring.
PROGRESS_STREAM_UNAVAILABLE: Final[str] = "progress_stream_unavailable"

#: Structured warning code recorded on the sink when an inbound payload
#: cannot be parsed as :class:`GuardKitProgressEvent`. The event is
#: dropped and the subscription continues consuming.
PROGRESS_PAYLOAD_INVALID: Final[str] = "progress_payload_invalid"


# ---------------------------------------------------------------------------
# Public DTOs and protocols
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProgressSinkWarning:
    """Structured anomaly captured by the subscriber.

    Warnings are surfaced via :attr:`ProgressSink.warnings` rather than
    being raised so the calling ``forge.adapters.guardkit.run()`` is
    never failed by telemetry-only conditions.

    Attributes:
        code: Stable string identifier (e.g.
            :data:`PROGRESS_STREAM_UNAVAILABLE`). Tests assert against
            this verbatim.
        message: Free-form human description; safe to log.
        build_id: Build identifier the warning relates to.
        subcommand: GuardKit subcommand label the warning relates to.
    """

    code: str
    message: str
    build_id: str
    subcommand: str


@runtime_checkable
class _SubscriptionLike(Protocol):
    """Slice of :class:`nats.aio.subscription.Subscription` we depend on.

    Declared so unit tests can substitute a hand-rolled fake without
    importing nats-py.
    """

    async def unsubscribe(self) -> None:  # pragma: no cover - protocol stub
        ...


@runtime_checkable
class _NATSSubscribeClient(Protocol):
    """Minimal subset of :class:`nats_core.client.NATSClient.subscribe`.

    Mirrors the contract used by
    :class:`forge.adapters.nats.approval_subscriber.ApprovalSubscriber`
    so the same client can be passed to either subscriber.
    """

    async def subscribe(  # pragma: no cover - protocol stub
        self,
        topic: str,
        callback: Callable[[MessageEnvelope], Awaitable[None]],
    ) -> _SubscriptionLike: ...


# ---------------------------------------------------------------------------
# Sink
# ---------------------------------------------------------------------------


class ProgressSink:
    """Holds the most recent N progress events per ``(build_id, subcommand)``.

    Used by ``forge status`` and the AsyncSubAgent live view. Bounded so
    a fast producer cannot grow this unboundedly during a slow
    subscriber (AC-002 / Scenario "Progress events emitted faster than
    Forge consumes them are still observable for live status").

    Two concurrent builds get isolated state by passing distinct
    :class:`ProgressSink` instances to :func:`subscribe_progress`; a
    single instance can also serve multiple ``(build_id, subcommand)``
    keys safely because the per-key buffer is selected on each call.

    Args:
        max_events: Maximum events retained per ``(build_id,
            subcommand)`` key before the oldest is evicted. Defaults to
            :data:`DEFAULT_MAX_EVENTS_PER_STREAM`. Must be ``> 0``.

    Raises:
        ValueError: If *max_events* is not strictly positive.
    """

    __slots__ = ("_max", "_buffers", "_warnings")

    def __init__(self, max_events: int = DEFAULT_MAX_EVENTS_PER_STREAM) -> None:
        if max_events <= 0:
            raise ValueError(f"max_events must be > 0, got {max_events!r}")
        self._max: int = max_events
        self._buffers: dict[tuple[str, str], deque[GuardKitProgressEvent]] = {}
        self._warnings: list[ProgressSinkWarning] = []

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record(self, event: GuardKitProgressEvent) -> None:
        """Append *event* to the per-key buffer, evicting the oldest if full.

        :class:`collections.deque` with ``maxlen=N`` provides the
        eviction-on-full semantics required by AC-002 in O(1) without
        any extra bookkeeping.
        """
        key = (event.build_id, event.subcommand)
        buf = self._buffers.get(key)
        if buf is None:
            buf = deque(maxlen=self._max)
            self._buffers[key] = buf
        buf.append(event)

    def warn(
        self,
        *,
        code: str,
        message: str,
        build_id: str,
        subcommand: str,
    ) -> None:
        """Capture a structured anomaly on the sink.

        Warnings never raise — they are recorded for the live-status
        view to surface to the operator.
        """
        self._warnings.append(
            ProgressSinkWarning(
                code=code,
                message=message,
                build_id=build_id,
                subcommand=subcommand,
            )
        )

    # ------------------------------------------------------------------
    # Read access
    # ------------------------------------------------------------------

    def latest(self, build_id: str, subcommand: str) -> GuardKitProgressEvent | None:
        """Return the most recent event for ``(build_id, subcommand)`` or
        ``None`` when no event has been observed."""
        buf = self._buffers.get((build_id, subcommand))
        if not buf:
            return None
        return buf[-1]

    def all_for(self, build_id: str, subcommand: str) -> list[GuardKitProgressEvent]:
        """Return a snapshot list of retained events for ``(build_id,
        subcommand)``, oldest first.

        The returned list is a copy — mutating it does not affect the
        sink's internal buffer.
        """
        buf = self._buffers.get((build_id, subcommand))
        if not buf:
            return []
        return list(buf)

    @property
    def warnings(self) -> list[ProgressSinkWarning]:
        """Snapshot of structured warnings captured so far."""
        return list(self._warnings)


# ---------------------------------------------------------------------------
# Subject helper
# ---------------------------------------------------------------------------


def subject_for(build_id: str, subcommand: str) -> str:
    """Return ``pipeline.stage-complete.{build_id}.{subcommand}``.

    Args:
        build_id: Identifier of the GuardKit invocation. Must be
            non-empty.
        subcommand: GuardKit subcommand label (e.g. ``/feature-spec``).
            Must be non-empty.

    Raises:
        ValueError: If either argument is empty.
    """
    if not build_id:
        raise ValueError("build_id must be a non-empty string")
    if not subcommand:
        raise ValueError("subcommand must be a non-empty string")
    return f"{SUBJECT_PREFIX}.{build_id}.{subcommand}"


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


@asynccontextmanager
async def subscribe_progress(
    nats_client: Any,
    build_id: str,
    subcommand: str,
    sink: ProgressSink,
) -> AsyncIterator[None]:
    """Subscribe to ``pipeline.stage-complete.{build_id}.{subcommand}``.

    The subscription lives for the duration of the ``async with`` block.
    On exit (normal or exceptional), the underlying NATS subscription is
    cancelled. Errors arising from the broker — a ``None`` client, a
    failed :meth:`subscribe`, a malformed inbound payload, or a broken
    :meth:`unsubscribe` — are logged and translated into a structured
    warning on *sink*; they are **never** propagated, because the
    authoritative completion still flows through the synchronous
    :class:`GuardKitResult` and a slow / missing progress stream must
    not fail the surrounding ``forge.adapters.guardkit.run()`` call
    (Scenario "The authoritative result still returns when progress
    streaming is unavailable").

    Args:
        nats_client: Async NATS client exposing
            :meth:`_NATSSubscribeClient.subscribe`. Pass ``None`` to
            opt into the no-op subscription path (the surrounding call
            still proceeds; one
            :data:`PROGRESS_STREAM_UNAVAILABLE` warning is recorded).
        build_id: Identifier of the GuardKit invocation. Must be
            non-empty.
        subcommand: GuardKit subcommand label. Must be non-empty.
        sink: Where to record events and warnings. Caller-owned —
            isolated sinks per build give the AC-006 isolation
            guarantee for free.

    Raises:
        ValueError: If *build_id* or *subcommand* is empty (a
            programming error worth surfacing).

    Yields:
        ``None``. The subscription is active for the lifetime of the
        ``async with`` body.
    """
    # ------------------------------------------------------------------
    # No-op path: client absent.
    # ------------------------------------------------------------------
    if nats_client is None:
        sink.warn(
            code=PROGRESS_STREAM_UNAVAILABLE,
            message=(
                "NATS client is None — progress stream unavailable; "
                "surrounding call proceeds without telemetry"
            ),
            build_id=build_id,
            subcommand=subcommand,
        )
        logger.warning(
            "progress_subscriber: %s build_id=%s subcommand=%s (client=None)",
            PROGRESS_STREAM_UNAVAILABLE,
            build_id,
            subcommand,
        )
        yield
        return

    subject = subject_for(build_id, subcommand)

    async def _on_envelope(envelope: MessageEnvelope) -> None:
        """Validate one inbound envelope and record it on the sink.

        Never raises. Malformed payloads are dropped with a structured
        warning so the subscription continues consuming subsequent
        events (AC-007).
        """
        try:
            event = GuardKitProgressEvent.model_validate(envelope.payload)
        except (ValidationError, ValueError, TypeError) as exc:
            logger.warning(
                "progress_subscriber: invalid payload build_id=%s subcommand=%s err=%s",
                build_id,
                subcommand,
                exc,
            )
            sink.warn(
                code=PROGRESS_PAYLOAD_INVALID,
                message=f"invalid progress payload: {exc}",
                build_id=build_id,
                subcommand=subcommand,
            )
            return
        sink.record(event)

    # ------------------------------------------------------------------
    # Subscribe — failure here is also a "stream unavailable" condition.
    # ------------------------------------------------------------------
    try:
        sub = await nats_client.subscribe(subject, _on_envelope)
    except Exception as exc:  # noqa: BLE001 — boundary swallow, see docstring
        logger.warning(
            "progress_subscriber: subscribe failed build_id=%s subcommand=%s err=%s",
            build_id,
            subcommand,
            exc,
        )
        sink.warn(
            code=PROGRESS_STREAM_UNAVAILABLE,
            message=f"subscribe failed: {exc}",
            build_id=build_id,
            subcommand=subcommand,
        )
        yield
        return

    # ------------------------------------------------------------------
    # Active path: yield to the body and unsubscribe on exit.
    # ------------------------------------------------------------------
    try:
        yield
    finally:
        try:
            await sub.unsubscribe()
        except Exception as exc:  # noqa: BLE001 — see module docstring
            # Subscription cleanup must never raise out of the calling
            # ``async with`` body — the authoritative GuardKit result
            # still has to return.
            logger.debug(
                "progress_subscriber: unsubscribe error "
                "build_id=%s subcommand=%s err=%s",
                build_id,
                subcommand,
                exc,
            )


__all__ = [
    "DEFAULT_MAX_EVENTS_PER_STREAM",
    "PROGRESS_PAYLOAD_INVALID",
    "PROGRESS_STREAM_UNAVAILABLE",
    "SUBJECT_PREFIX",
    "ProgressSink",
    "ProgressSinkWarning",
    "subject_for",
    "subscribe_progress",
]

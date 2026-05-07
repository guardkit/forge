"""Build-queued ack/nak handle interface for the lifecycle bridge.

This module defines the contract between
:mod:`forge.adapters.nats.pipeline_consumer` (which owns the inbound
JetStream :class:`Msg`) and the lifecycle bridge (TASK-FRR-PEB-002)
that observes terminal states from the SSE stream and decides when
to ack the original ``pipeline.build-queued.{feature_id}`` envelope.

Why an interface, not a callback?
---------------------------------

Wave 1 (this task) of the F010M wave-plan defers the inbound ack from
``dispatch_build`` *return* to autobuild *terminal arrival*. The previous
contract handed the consumer a single ``ack_callback`` closure to the
state machine. That closure was opaque (only ``ack``, no ``nak``) and
made it impossible for the bridge to negatively-acknowledge a build that
never reached terminal — JetStream redelivers automatically after
``ack_wait`` (1h per API contract §2.2), but explicit ``nak()`` lets the
bridge surface a fast retry path on bridge-side errors that should not
block the queue for a full hour.

The :class:`BuildAckHandle` Protocol exposes both verbs and is the only
public surface the bridge sees. Concrete handles wrap the underlying
:class:`nats.aio.msg.Msg` with idempotent ack/nak so the bridge can call
either method exactly once per build identity without worrying about
double-ack races against the legacy F010F sync-raise fallback path.

Registry shape
--------------

The consumer registers exactly one :class:`BuildAckHandle` per accepted
build, keyed by ``(feature_id, correlation_id)`` — the same pair used by
``is_duplicate_terminal`` and the ``builds`` SQLite unique index
(ASSUM-014). Duplicate registrations for the same key are ignored
(idempotency); the bridge looks up the handle on terminal arrival and
calls ``ack()``. When the bridge is not wired (unit-test path,
``InFlightAckRegistry`` injected as ``None``) the consumer falls back to
the existing F010F sync-raise behaviour: ack on dispatch return, ack +
publish ``build-failed`` on dispatch raise. This preserves test
determinism for paths that never exercise the bridge.

References:
    - Task brief: ``TASK-FRR-PEB-001``
    - Bridge skeleton: ``TASK-FRR-PEB-002`` (consumes this interface)
    - SSE-to-envelope translation: ``TASK-FRR-PEB-003`` (calls
      :meth:`BuildAckHandle.ack` on terminal observation)
    - Coexistence boundary: ``TASK-FRR-PEB-005``
    - ADR-SP-013 — terminal-only ack semantics rationale.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


__all__ = [
    "BuildAckHandle",
    "InFlightAckRegistry",
    "MsgBuildAckHandle",
    "make_msg_ack_handle",
]


@runtime_checkable
class _AckNakMsg(Protocol):
    """Minimal slice of :class:`nats.aio.msg.Msg` we depend on.

    Declared here (and mirrored in
    :mod:`forge.adapters.nats.pipeline_consumer`) so callers can use a
    lightweight :class:`unittest.mock.AsyncMock` double in tests without
    monkey-patching the real nats-py message class. ``nak`` is optional
    on the underlying transport — :class:`MsgBuildAckHandle` falls back
    to ``ack`` + a logged warning when the underlying msg does not
    expose ``nak``, so older nats-py versions don't break the bridge.
    """

    async def ack(self) -> None:  # pragma: no cover - protocol stub
        ...


@runtime_checkable
class BuildAckHandle(Protocol):
    """Public ack/nak surface for one accepted build.

    The lifecycle bridge consumes this interface to ack the original
    ``pipeline.build-queued.{feature_id}`` JetStream message when it
    observes a terminal state on the SSE stream. The bridge MUST NOT
    hold a reference to :class:`MessageEnvelope` or to the underlying
    NATS client — the consumer module owns those concerns and the
    bridge sees only :class:`BuildAckHandle`.

    Methods:
        ack: Positive acknowledgement. Removes the message from the
            durable consumer's redelivery queue. Idempotent — calling
            twice is a no-op (the second call logs at DEBUG and
            returns). Calling after :meth:`nak` logs a WARNING and is
            ignored; mixed ack/nak is a contract bug upstream.
        nak: Negative acknowledgement. Causes JetStream to redeliver
            after the configured backoff (or immediately if the bridge
            wants the consumer to retry-from-scratch). Idempotent in
            the same sense as :meth:`ack`.
    """

    async def ack(self) -> None:  # pragma: no cover - protocol stub
        ...

    async def nak(self) -> None:  # pragma: no cover - protocol stub
        ...


@dataclass
class MsgBuildAckHandle:
    """Concrete :class:`BuildAckHandle` wrapping a NATS :class:`Msg`.

    State (``_acked`` / ``_naked``) is held in a mutable dict rather
    than dataclass attributes so the dataclass can stay frozen-ish in
    spirit (only the message reference is stored as a field) while the
    handle remains idempotent across the lifetime of one build.

    Attributes:
        msg: The underlying NATS message. Must expose ``async def
            ack()``; ``async def nak()`` is optional — when absent
            (older nats-py, in-memory test doubles) :meth:`nak` logs a
            WARNING and falls back to ``ack`` so the queue is never
            wedged on the bridge's nak path.
        _state: Mutable flags tracking whether ``ack`` or ``nak`` has
            already been invoked. Default-factory keeps each handle
            independent so two handles never share the same flag dict.
    """

    msg: _AckNakMsg
    _state: dict[str, bool] = field(default_factory=lambda: {"acked": False, "naked": False})

    async def ack(self) -> None:
        """Acknowledge the inbound ``build-queued`` envelope.

        Idempotent: a second call after the first ``ack()`` (or after
        :meth:`nak`) logs and returns without touching the underlying
        message. JetStream tolerates double-ack but tests assert
        "acked exactly once" against this handle's flags.
        """
        if self._state["acked"]:
            logger.debug(
                "MsgBuildAckHandle: ack() invoked twice; ignoring "
                "second call (idempotent)"
            )
            return
        if self._state["naked"]:
            logger.warning(
                "MsgBuildAckHandle: ack() invoked after nak(); "
                "ignoring — contract bug upstream (mixed ack/nak)"
            )
            return
        self._state["acked"] = True
        await self.msg.ack()

    async def nak(self) -> None:
        """Negatively-acknowledge the inbound ``build-queued`` envelope.

        Idempotent: a second call after the first ``nak()`` (or after
        :meth:`ack`) logs and returns without touching the underlying
        message.

        If the underlying msg does not expose ``async def nak()``
        (older nats-py releases, lightweight test doubles), this
        method logs a WARNING and falls back to ``ack`` so the queue
        is not wedged for a full ``ack_wait`` (1h) interval. The
        bridge sees ``nak`` semantics from its side; the transport's
        backoff is the absent-nak fallback.
        """
        if self._state["naked"]:
            logger.debug(
                "MsgBuildAckHandle: nak() invoked twice; ignoring "
                "second call (idempotent)"
            )
            return
        if self._state["acked"]:
            logger.warning(
                "MsgBuildAckHandle: nak() invoked after ack(); "
                "ignoring — contract bug upstream (mixed ack/nak)"
            )
            return
        self._state["naked"] = True
        nak_method = getattr(self.msg, "nak", None)
        if nak_method is None:
            logger.warning(
                "MsgBuildAckHandle: underlying msg has no nak(); "
                "falling back to ack — JetStream will not redeliver "
                "until ack_wait expires"
            )
            await self.msg.ack()
            return
        try:
            await nak_method()
        except Exception as exc:
            # ``nak`` is best-effort. If the transport raises (e.g.
            # connection reset mid-nak), log and let JetStream's
            # ``ack_wait`` redeliver after the configured timeout.
            # Re-raising would propagate out of the bridge into the
            # consumer fetch loop and wedge the daemon.
            logger.warning(
                "MsgBuildAckHandle: nak() raised (%s); "
                "JetStream will redeliver after ack_wait",
                exc,
            )


def make_msg_ack_handle(msg: _AckNakMsg) -> MsgBuildAckHandle:
    """Construct an idempotent :class:`MsgBuildAckHandle` for ``msg``.

    Factory exists so the consumer module does not need to import
    :class:`MsgBuildAckHandle` directly — the public surface stays the
    :class:`BuildAckHandle` Protocol plus this constructor. Tests that
    want to assert "the handle the consumer registered is the one bound
    to this msg" can compare ``handle.msg is msg``.
    """
    return MsgBuildAckHandle(msg=msg)


# Type alias: the bridge owns a mutable mapping of identity → handle.
# Keys are ``(feature_id, correlation_id)`` tuples (mirrors ASSUM-014).
# We use ``Callable`` for the registration entry-point rather than
# exposing the dict directly so the bridge can swap the storage
# backend (in-memory dict, Redis, SQLite mirror) without touching the
# consumer's call site.
InFlightAckRegistry = Callable[[str, str, BuildAckHandle], Awaitable[None]]
"""``async (feature_id, correlation_id, handle) -> None``.

Registers ``handle`` against the identity tuple in the bridge's
in-flight store. Implementations MUST be idempotent for the same
identity (a duplicate registration is a benign re-dispatch, not an
error) and MUST NOT raise on registration — the consumer's dispatch
path treats registration as best-effort and continues if the bridge
itself is unavailable.
"""

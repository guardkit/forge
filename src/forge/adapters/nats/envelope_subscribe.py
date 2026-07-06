"""Envelope-aware subscribe adapter over the raw nats.aio client (TASK-JNB-109).

Several forge consumers (:class:`ApprovalSubscriber`, the Mode P planning
driver's response waiters) are written against the ``nats_core.NATSClient``
subscribe contract::

    async def subscribe(topic, callback: Callable[[MessageEnvelope], Awaitable[None]])

while the serve daemon's shared client is the RAW ``nats.aio.client.Client``,
whose signature is ``subscribe(subject, queue="", cb=None, ...)`` and whose
callback receives a raw ``nats.aio.msg.Msg``. Feeding the raw client to an
envelope-expecting consumer fails at runtime (the callback binds to the
``queue`` parameter) — the green-but-dead class TASK-MP-012's review caught
in the fleet watcher, and the same latent defect the build gate's approval
reply path carried since TASK-JNB-101 (JNB-107's live round-trip was the one
thing never validated).

This adapter is the single conversion point: it parses each message into a
validated :class:`MessageEnvelope` (malformed payloads are WARN-dropped at
the trust boundary) and invokes the envelope-aware callback. The returned
subscription handle exposes ``unsubscribe()`` as consumers expect.

Test-shape lesson (do not regress): fakes for this seam must mimic the
PRODUCTION client's signature (``subscribe(subject, queue="", cb=None)``),
not the consumer's wishes — consumer-shaped fakes are exactly how the
defect class stayed green twice.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

__all__ = ["EnvelopeSubscribeClient"]


class EnvelopeSubscribeClient:
    """Adapt a raw nats.aio client to the envelope-aware subscribe protocol.

    Args:
        nats_client: The raw async NATS client (or any client whose
            ``subscribe`` accepts either ``cb=`` keyword or a positional
            callback — in-memory test brokers use the latter).
        armed: Optional event set the moment the underlying subscription
            is active (arm-before-post support for re-emit paths).
    """

    def __init__(self, nats_client: Any, armed: asyncio.Event | None = None) -> None:
        self._nc = nats_client
        self._armed = armed

    async def subscribe(
        self,
        topic: str,
        callback: Callable[[Any], Awaitable[None]],
    ) -> Any:
        from nats_core.envelope import MessageEnvelope

        async def _on_msg(msg: Any) -> None:
            if isinstance(msg, MessageEnvelope):
                envelope = msg
            else:
                try:
                    envelope = MessageEnvelope.model_validate_json(msg.data)
                except Exception as exc:  # noqa: BLE001 — trust boundary
                    logger.warning(
                        "envelope subscribe: dropping malformed envelope on %s (%s)",
                        topic,
                        exc,
                    )
                    return
            await callback(envelope)

        try:
            subscription = await self._nc.subscribe(topic, cb=_on_msg)
        except TypeError:
            # In-memory test brokers take the callback positionally.
            subscription = await self._nc.subscribe(topic, _on_msg)
        if self._armed is not None:
            self._armed.set()
        return subscription

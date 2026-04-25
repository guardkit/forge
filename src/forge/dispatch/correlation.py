"""Per-dispatch correlation-keyed reply routing for the Forge dispatch layer.

This module is the **single most important domain primitive** in
FEAT-FORGE-003 (per TASK-SAD-003): the correlation registry that
enforces three invariants the LES1 incident motivated:

1. **Subscribe-before-publish ordering**
   (``D.subscribe-before-publish-invariant``) — :meth:`CorrelationRegistry.bind`
   establishes the subscription before returning a "ready" handle. The
   orchestrator MUST NOT publish without that handle.
2. **Exactly-once reply handling** (``E.duplicate-reply-idempotency``) —
   a second authentic reply on the same correlation key after the first
   has been accepted is silently dropped.
3. **Reply-source authenticity** (``E.reply-source-authenticity``) —
   each binding carries the ``matched_agent_id`` from the resolution;
   replies whose ``source_agent_id`` differs from that value are dropped.

Also enforced:

* ``C.pubAck-not-success`` — PubAck on the audit stream does NOT flow
  through :meth:`CorrelationRegistry.deliver_reply`, so it cannot flip
  ``binding.accepted``. Tests verify this against a fake transport.
* ``C.wrong-correlation-reply`` — replies on an unknown correlation key
  are dropped at :meth:`CorrelationRegistry.deliver_reply` entry.
* Correlation-key format invariant — 32 lowercase hex characters; no
  embedded agent IDs, timestamps, or other PII.
* ``D.unsubscribe-on-timeout`` — :meth:`CorrelationRegistry.release` is
  idempotent and prevents subsequent replies from being routed.

The module imports **no transport types** (no ``nats``, no HTTP). It
declares a thin :class:`ReplyChannel` :class:`typing.Protocol` that the
NATS adapter (TASK-SAD-010) implements; tests use in-memory fakes.

The registry MUST NOT log raw payloads — until the dispatcher hands the
reply to ``persist_resolution``, parameters may contain sensitive
values. Log records here include only correlation keys and agent IDs,
never the payload body.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

logger = logging.getLogger(__name__)

# 32 lowercase hex characters — see ``fresh_correlation_key`` for why.
CORRELATION_KEY_RE = re.compile(r"^[0-9a-f]{32}$")


# Type alias for the callback the registry hands to the transport on
# subscribe. The transport invokes it synchronously when a reply
# arrives. Sync because asyncio's cooperative scheduling makes the
# check-and-set on ``binding.accepted`` atomic without an explicit lock.
DeliverCallback = Callable[[str, str, "dict[str, Any]"], None]


class ReplyChannel(Protocol):
    """Transport contract for per-correlation reply subscriptions.

    Implementations subscribe by ``correlation_key`` and invoke the
    supplied ``deliver`` callback when a reply arrives on that
    subscription. The NATS adapter (TASK-SAD-010) provides the
    production implementation; tests use in-memory fakes.

    The contract requires :meth:`subscribe` to return ONLY after the
    subscription is active end-to-end, so :meth:`CorrelationRegistry.bind`
    can preserve the subscribe-before-publish invariant. NATS JetStream
    consumers expose an explicit "consumer ready" acknowledgement that
    satisfies this requirement; do NOT use ``asyncio.sleep`` to wait.
    """

    async def subscribe(
        self, correlation_key: str, deliver: DeliverCallback
    ) -> Any:
        """Establish a subscription for ``correlation_key``.

        Must return ONLY after the subscription is active. Returns an
        opaque handle the registry passes back to :meth:`unsubscribe`.
        """
        ...

    async def unsubscribe(self, subscription: Any) -> None:
        """Tear down the subscription. Must be idempotent."""
        ...


@dataclass
class CorrelationBinding:
    """Per-dispatch binding state — one binding per correlation key.

    Attributes:
        correlation_key: 32-lowercase-hex key identifying this dispatch.
        matched_agent_id: source identifier authorised to reply on this
            binding. Replies whose ``source_agent_id`` differs are
            dropped (``E.reply-source-authenticity``).
        accepted: ``True`` after the first authentic reply has been
            routed. Once set, subsequent replies are dropped
            (``E.duplicate-reply-idempotency``).
        subscription_active: ``True`` once :meth:`CorrelationRegistry.bind`
            has established the transport subscription. The orchestrator
            MUST NOT publish until this flag is ``True`` —
            ``D.subscribe-before-publish-invariant``. Exposed as a public
            field so tests can assert the ordering directly without
            relying on internal state.
    """

    correlation_key: str
    matched_agent_id: str
    accepted: bool = False
    subscription_active: bool = False
    # Future is created inside ``bind`` (which always has a running
    # loop) rather than via ``default_factory`` — that avoids the
    # standard "no current event loop" trap when a binding is
    # instantiated outside an async context (e.g., test imports).
    _future: Optional[asyncio.Future] = field(default=None, repr=False)
    _released: bool = field(default=False, repr=False)


class CorrelationRegistry:
    """Per-dispatch correlation-keyed reply routing.

    Lifecycle for one dispatch::

        key = registry.fresh_correlation_key()
        binding = await registry.bind(key, matched_agent_id)
        # ↑ subscription is active; only NOW may the orchestrator publish
        # transport publishes the command...
        outcome = await registry.wait_for_reply(binding, timeout)
        registry.release(binding)

    The registry holds bindings, subscription handles, and the per-binding
    futures used for timeout coordination. It does **not** own the
    transport — the transport is injected so that NATS/HTTP/in-memory
    implementations are interchangeable.
    """

    def __init__(self, transport: ReplyChannel) -> None:
        self._transport = transport
        self._bindings: dict[str, CorrelationBinding] = {}
        self._subscriptions: dict[str, Any] = {}

    # -- public API ------------------------------------------------- #

    def fresh_correlation_key(self) -> str:
        """Return a new 32-lowercase-hex correlation key.

        Generated via :func:`secrets.token_hex`, so the output alphabet
        is restricted to ``[0-9a-f]``. The hex alphabet alone is
        sufficient to guarantee no embedded agent IDs (which contain
        ``-`` or alphanumerics outside hex), no timestamps (which
        contain ``-`` / ``:`` / ``T`` / ``Z``), and no other PII.
        """
        return secrets.token_hex(16)

    async def bind(
        self, correlation_key: str, matched_agent_id: str
    ) -> CorrelationBinding:
        """Establish the per-correlation reply subscription.

        Returns ONLY after the underlying ``ReplyChannel.subscribe`` has
        completed — i.e., the subscription is active end-to-end and any
        subsequent ``deliver_reply`` for ``correlation_key`` will be
        routed to the new binding.

        Args:
            correlation_key: 32-lowercase-hex key. Format is validated
                here (the boundary), not on the ``CorrelationKey``
                opaque alias in :mod:`forge.dispatch.models`.
            matched_agent_id: identifier of the agent authorised to
                reply on this binding. Copied from the resolution.

        Returns:
            The :class:`CorrelationBinding` with
            ``subscription_active=True``.

        Raises:
            ValueError: if ``correlation_key`` does not match
                :data:`CORRELATION_KEY_RE`, or if a binding for that
                key already exists.
        """
        if not CORRELATION_KEY_RE.fullmatch(correlation_key):
            raise ValueError(
                f"invalid correlation key format: {correlation_key!r}"
            )
        if correlation_key in self._bindings:
            raise ValueError(
                f"correlation key already bound: {correlation_key!r}"
            )

        loop = asyncio.get_running_loop()
        binding = CorrelationBinding(
            correlation_key=correlation_key,
            matched_agent_id=matched_agent_id,
            _future=loop.create_future(),
        )
        # Register the binding before we await the subscribe() so a
        # transport that fires its first reply synchronously inside
        # subscribe() will still find a binding to route to.
        self._bindings[correlation_key] = binding
        try:
            subscription = await self._transport.subscribe(
                correlation_key, self.deliver_reply
            )
        except BaseException:
            # Subscribe failed — undo the partial state so the caller
            # may retry with a fresh key without leaking the slot.
            self._bindings.pop(correlation_key, None)
            future = binding._future
            if future is not None and not future.done():
                future.cancel()
            raise

        self._subscriptions[correlation_key] = subscription
        binding.subscription_active = True
        return binding

    def deliver_reply(
        self,
        correlation_key: str,
        source_agent_id: str,
        payload: dict[str, Any],
    ) -> None:
        """Route a reply that arrived on the transport.

        Internal entrypoint — the transport calls this when a reply
        arrives on a subscription owned by this registry. The method is
        intentionally **synchronous**:

        * Under asyncio's single-threaded cooperative scheduling, the
          check-and-set on ``binding.accepted`` is atomic between
          ``await`` points. Two concurrent coroutines that both call
          :meth:`deliver_reply` for the same key resolve sequentially
          inside one event-loop tick, so exactly-once is preserved
          without an explicit lock.
        * Keeping the method sync also means the transport callback path
          has no implicit ``await`` — wrong-correlation drops, source
          mismatches, and duplicate drops complete in O(1) without
          yielding the loop.

        Drop conditions (silent — emit a debug log, never the payload):

        * No binding for ``correlation_key`` (``C.wrong-correlation-reply``).
        * Binding already ``_released`` (``D.unsubscribe-on-timeout``).
        * ``source_agent_id != binding.matched_agent_id``
          (``E.reply-source-authenticity``).
        * ``binding.accepted`` is already ``True``
          (``E.duplicate-reply-idempotency``).
        """
        binding = self._bindings.get(correlation_key)
        if binding is None:
            logger.debug(
                "drop reply: no binding for correlation key (key=%s)",
                correlation_key,
            )
            return
        if binding._released:
            logger.debug(
                "drop reply: binding released (key=%s)", correlation_key
            )
            return
        if source_agent_id != binding.matched_agent_id:
            logger.warning(
                "drop reply: source mismatch (key=%s, got=%s, want=%s)",
                correlation_key,
                source_agent_id,
                binding.matched_agent_id,
            )
            return
        if binding.accepted:
            logger.debug(
                "drop reply: already accepted (key=%s)", correlation_key
            )
            return

        # Authentic, first reply — record it and resolve the waiter.
        binding.accepted = True
        future = binding._future
        if future is not None and not future.done():
            future.set_result(payload)

    async def wait_for_reply(
        self,
        binding: CorrelationBinding,
        timeout_seconds: float,
    ) -> Optional[dict[str, Any]]:
        """Await the authentic reply or fall through on timeout.

        Returns the payload on success, ``None`` on timeout. Does NOT
        release the binding — :meth:`release` is the caller's
        responsibility (the timeout coordinator owned by TASK-SAD-004
        unsubscribes via :meth:`release`).

        ``asyncio.shield`` protects the binding's future from being
        cancelled by ``wait_for``'s timeout cancellation, so a future
        already completed at timeout-fire time still surfaces its
        result on a subsequent ``wait_for_reply`` call (rare, but
        possible if the reply lands in the same loop tick the timeout
        fires).
        """
        future = binding._future
        if future is None:
            return None
        try:
            return await asyncio.wait_for(
                asyncio.shield(future), timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            return None
        except asyncio.CancelledError:
            # Future was cancelled (e.g., release() during a wait).
            return None

    def release(self, binding: CorrelationBinding) -> None:
        """Release the subscription. Idempotent.

        After ``release``:

        * The binding is removed from the registry, so a late
          ``deliver_reply`` for the same key sees no binding and is
          dropped (``D.unsubscribe-on-timeout``).
        * The transport is asked to unsubscribe in the background — we
          fire-and-forget via ``loop.create_task`` because ``release``
          is sync (the timeout coordinator may not be in async context
          when it fires).
        * The binding's pending future is cancelled so any concurrent
          :meth:`wait_for_reply` returns ``None``.

        Calling ``release`` twice is a no-op; the second call short-
        circuits on ``binding._released``.
        """
        if binding._released:
            return
        binding._released = True

        key = binding.correlation_key
        # Drop registry-side state first so a racing deliver_reply that
        # observes our partial unwind sees "no binding" rather than a
        # binding mid-tear-down.
        self._bindings.pop(key, None)
        subscription = self._subscriptions.pop(key, None)

        future = binding._future
        if future is not None and not future.done():
            future.cancel()

        if subscription is None:
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop — best-effort. The transport will
            # eventually GC its subscription when its session closes.
            logger.debug(
                "release: no running loop; skipping unsubscribe (key=%s)",
                key,
            )
            return
        loop.create_task(self._safe_unsubscribe(subscription, key))

    # -- internal helpers ------------------------------------------- #

    async def _safe_unsubscribe(self, subscription: Any, key: str) -> None:
        """Unsubscribe with broad exception isolation.

        We never want a transport-level error during unsubscribe to
        propagate into the timeout coordinator. Log and continue.
        """
        try:
            await self._transport.unsubscribe(subscription)
        except Exception:
            logger.exception(
                "unsubscribe failed (key=%s); subscription leak possible",
                key,
            )


__all__ = [
    "CORRELATION_KEY_RE",
    "CorrelationBinding",
    "CorrelationRegistry",
    "DeliverCallback",
    "ReplyChannel",
]

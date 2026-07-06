"""NATS adapter — specialist dispatch (bind / publish / deliver).

Thin transport adapter binding the pure-domain
:class:`forge.dispatch.correlation.CorrelationRegistry` (TASK-SAD-003) and
:class:`forge.dispatch.orchestrator.DispatchOrchestrator` (TASK-SAD-006) to
JetStream. This is the **only** module in FEAT-FORGE-003's dispatch layer
that is allowed to import :mod:`nats.aio` types — the orchestrator,
registry, parser, retry coordinator, and outcome helpers must remain
free of NATS imports.

Subject layout
--------------

================================  ==================================================
Direction                         Subject
================================  ==================================================
Forge → specialist (command)      ``agents.command.{matched_agent_id}``
specialist → Forge (reply)        ``agents.result.{matched_agent_id}.{correlation_key}``
================================  ==================================================

The singular ``agents.command`` / ``agents.result`` convention is the
fleet-wide adoption (DRD-001..004; FEAT-FORGE-002 ADR adoption recorded
in Graphiti ``architecture_decisions``). The per-correlation suffix on the
reply subject means each dispatch attempt owns its own subscription —
exactly-once and source-authenticity invariants are clean to enforce in
:class:`CorrelationRegistry` without router-side fan-out.

Headers on the dispatch command
-------------------------------

* ``correlation_key`` — 32 lowercase hex (per the
  :data:`forge.dispatch.correlation.CORRELATION_KEY_RE` contract).
* ``requesting_agent_id`` — fixed string ``"forge"``.
* ``dispatched_at`` — ISO 8601 UTC timestamp at publish time.

Reply correlation lifecycle
---------------------------

* :meth:`NatsSpecialistDispatchAdapter.subscribe_reply` is called from
  :meth:`CorrelationRegistry.bind` (via the wiring established in
  TASK-SAD-011). It returns ONLY after the underlying NATS subscription
  is fully established — i.e. the SUB protocol command has been flushed
  to the server. The orchestrator's subscribe-before-publish invariant
  depends on this contract.
* :meth:`unsubscribe_reply` is called from
  :meth:`CorrelationRegistry.release`. It is idempotent — calling it a
  second time with the same correlation key is a no-op.
* :meth:`_on_reply_received` is the per-message callback registered
  with the NATS subscription. It extracts ``source_agent_id`` from the
  message headers, decodes the JSON payload, and forwards to
  :meth:`CorrelationRegistry.deliver_reply`. Authentication is enforced
  in the registry, **not** here — the adapter simply forwards what it
  observed.

PubAck semantics
----------------

JetStream's PubAck (when the audit stream is configured to emit one) is
treated as a "publish was sent" signal only — it is logged at DEBUG and
**never** routed through :meth:`CorrelationRegistry.deliver_reply`. The
binding's outcome is determined by the actual reply payload landing on
the per-correlation reply subscription. This mirrors the LES1 parity
rule already enforced in :class:`forge.adapters.nats.PipelinePublisher`.

References
----------

* TASK-SAD-010 — this task.
* TASK-SAD-003 — :class:`CorrelationRegistry` + ``CorrelationKey``.
* TASK-SAD-006 — :class:`DispatchOrchestrator`.
* TASK-SAD-011 — wiring + ``FakeNatsClient`` recording extension.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from forge.discovery.protocol import Clock, SystemClock
from forge.dispatch.correlation import CorrelationRegistry
from forge.dispatch.models import DispatchAttempt
from forge.dispatch.persistence import DispatchParameter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants pinned to the API contract — exported so tests assert against
# a single source of truth (mirrors the pattern used by
# ``forge.adapters.nats.pipeline_publisher``).
# ---------------------------------------------------------------------------

#: Subject template for the Forge → specialist dispatch command.
COMMAND_SUBJECT_TEMPLATE: str = "agents.command.{agent_id}"

#: Subject template for the specialist → Forge reply.
RESULT_SUBJECT_TEMPLATE: str = "agents.result.{agent_id}.{correlation_key}"

#: Header carrying the per-attempt correlation key (32 lowercase hex).
CORRELATION_KEY_HEADER: str = "correlation_key"

#: Header carrying the requesting agent identifier (fixed: ``"forge"``).
REQUESTING_AGENT_HEADER: str = "requesting_agent_id"

#: Header carrying the publish-time ISO 8601 UTC timestamp.
DISPATCHED_AT_HEADER: str = "dispatched_at"

#: Header carrying the replying specialist's agent identifier.
SOURCE_AGENT_HEADER: str = "source_agent_id"

#: Fixed source identifier stamped on every dispatch command.
REQUESTING_AGENT_ID: str = "forge"

__all__ = [
    "COMMAND_SUBJECT_TEMPLATE",
    "CORRELATION_KEY_HEADER",
    "DISPATCHED_AT_HEADER",
    "DispatchCommandPublisher",
    "NatsSpecialistDispatchAdapter",
    "REQUESTING_AGENT_HEADER",
    "REQUESTING_AGENT_ID",
    "RESULT_SUBJECT_TEMPLATE",
    "ReplyChannel",
    "SOURCE_AGENT_HEADER",
]


# ---------------------------------------------------------------------------
# Protocols implemented by the adapter — the pure-domain layer depends on
# these structurally typed interfaces, never on the NATS-bound concrete
# class.
# ---------------------------------------------------------------------------


class ReplyChannel(Protocol):
    """Domain-side reply-subscription protocol implemented by this adapter.

    The :class:`CorrelationRegistry` (TASK-SAD-003) declares its own
    transport-shaped ``ReplyChannel`` whose ``subscribe`` takes a
    correlation key plus a deliver callback. This adapter exposes the
    subject-shaped surface (``subscribe_reply`` / ``unsubscribe_reply``)
    expected by TASK-SAD-010's wiring contract; TASK-SAD-011 owns the
    bridge between the two if needed.
    """

    async def subscribe_reply(
        self, matched_agent_id: str, correlation_key: str
    ) -> None:
        """Establish a per-correlation reply subscription.

        MUST return ONLY after the NATS subscription is fully active —
        i.e. the SUB command has been flushed to the server. The
        :class:`CorrelationRegistry`'s ``bind()`` relies on this
        contract to satisfy the subscribe-before-publish invariant.
        """
        ...

    async def unsubscribe_reply(self, correlation_key: str) -> None:
        """Tear down the per-correlation subscription. MUST be idempotent."""
        ...


class DispatchCommandPublisher(Protocol):
    """Domain-side publish protocol implemented by this adapter.

    Mirrors :class:`forge.dispatch.orchestrator.DispatchCommandPublisher`
    — re-declared here so the adapter module is self-describing. The
    orchestrator's Protocol is the one imported by domain code; this is
    the adapter-side surface the wiring layer asserts against.
    """

    async def publish_dispatch(
        self,
        attempt: DispatchAttempt,
        parameters: list[DispatchParameter],
    ) -> None:
        """Publish the dispatch command on the transport."""
        ...


# ---------------------------------------------------------------------------
# Adapter implementation
# ---------------------------------------------------------------------------


class NatsSpecialistDispatchAdapter:
    """JetStream binding for dispatch + reply correlation.

    Wires the pure-domain :class:`CorrelationRegistry` and the dispatch
    orchestrator's :class:`DispatchCommandPublisher` Protocol to
    :mod:`nats.aio`. The adapter owns three pieces of state:

    * The injected NATS client (must support ``subscribe(subject, cb=...)``
      and ``publish(subject, payload, headers=...)``). The connection is
      created by FEAT-FORGE-002's bootstrap code; we do **not** open a
      new one here.
    * The injected :class:`CorrelationRegistry` — its
      :meth:`~CorrelationRegistry.deliver_reply` is the sink that
      :meth:`_on_reply_received` forwards to.
    * A per-correlation subscription handle map so
      :meth:`unsubscribe_reply` can tear down the right subscription
      without leaking handles.

    Args:
        nats_client: An async NATS client with ``subscribe`` / ``publish``
            methods compatible with :class:`nats.aio.client.Client`.
        registry: The :class:`CorrelationRegistry` whose
            ``deliver_reply`` this adapter forwards inbound replies to.
        clock: A :class:`forge.discovery.protocol.Clock` providing the
            UTC timestamp stamped onto the ``dispatched_at`` header.
            Defaults to a :class:`SystemClock` so production callers do
            not have to wire one explicitly; tests inject a deterministic
            fake to make the header value predictable. Routing time
            through Clock keeps the adapter compliant with the
            clock-hygiene rule enforced by
            ``tests/forge/test_contract_and_seam.py::TestClockHygiene``.
    """

    def __init__(
        self,
        nats_client: Any,
        registry: CorrelationRegistry,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._nc = nats_client
        self._registry = registry
        self._clock: Clock = clock if clock is not None else SystemClock()
        # correlation_key -> opaque subscription handle returned by the
        # NATS client. The handle has an ``unsubscribe()`` coroutine —
        # we stash it so unsubscribe_reply can be idempotent without
        # having to re-derive the subject.
        self._subscriptions: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Subject helpers — exposed as static methods so tests can assert
    # subject construction without instantiating the adapter.
    # ------------------------------------------------------------------

    @staticmethod
    def command_subject_for(matched_agent_id: str) -> str:
        """Build ``agents.command.{matched_agent_id}``."""
        return COMMAND_SUBJECT_TEMPLATE.format(agent_id=matched_agent_id)

    @staticmethod
    def result_subject_for(
        matched_agent_id: str, correlation_key: str
    ) -> str:
        """Build ``agents.result.{matched_agent_id}.{correlation_key}``."""
        return RESULT_SUBJECT_TEMPLATE.format(
            agent_id=matched_agent_id, correlation_key=correlation_key
        )

    # ------------------------------------------------------------------
    # ReplyChannel surface — subscribe / unsubscribe per correlation
    # ------------------------------------------------------------------

    async def subscribe(self, correlation_key: str, deliver: Any) -> str:
        """:class:`~forge.dispatch.correlation.ReplyChannel` conformance.

        Called by :meth:`CorrelationRegistry.bind`. The registry
        registers the binding BEFORE awaiting this method, so the
        ``matched_agent_id`` needed to derive the result subject is
        resolved from the registry itself
        (:meth:`CorrelationRegistry.matched_agent_for`) — this closes
        the TASK-SAD-011 bridge gap that previously left the adapter
        unpluggable as the registry's transport (TASK-MP-012).

        ``deliver`` is accepted per the protocol but unused: inbound
        replies are forwarded to ``self._registry.deliver_reply`` by
        :meth:`_on_reply_received`, and the registry passes exactly that
        bound method here — the sink is identical by construction.

        Returns the ``correlation_key`` as the opaque subscription
        handle (``unsubscribe`` tears down by key).
        """
        matched_agent_id = self._registry.matched_agent_for(correlation_key)
        if matched_agent_id is None:
            raise LookupError(
                f"subscribe: no binding registered for correlation key "
                f"{correlation_key!r}; ReplyChannel.subscribe must be "
                "driven through CorrelationRegistry.bind"
            )
        await self.subscribe_reply(matched_agent_id, correlation_key)
        return correlation_key

    async def unsubscribe(self, subscription: Any) -> None:
        """:class:`~forge.dispatch.correlation.ReplyChannel` conformance.

        ``subscription`` is the correlation_key handle returned by
        :meth:`subscribe`. Idempotent, never raises past the boundary
        (delegates to :meth:`unsubscribe_reply`).
        """
        await self.unsubscribe_reply(str(subscription))

    async def subscribe_reply(
        self, matched_agent_id: str, correlation_key: str
    ) -> None:
        """Subscribe to ``agents.result.{matched_agent_id}.{correlation_key}``.

        Returns ONLY after the underlying NATS subscription is fully
        active — i.e. the SUB command has been flushed to the server.
        This is the subscribe-before-publish anchor the orchestrator's
        invariant depends on (D.subscribe-before-publish-invariant).

        nats-py's :meth:`Client.subscribe` already awaits the SUB write
        before returning the :class:`Subscription`, but we additionally
        invoke ``flush`` (when available) so a remote server has
        observed our SUB before any subsequent publish. ``asyncio.sleep``
        is **never** used as a synchronisation primitive here — that
        path was the LES1 anti-pattern.

        If the same ``correlation_key`` is subscribed twice, the second
        subscription replaces the first — the previous handle is dropped
        without an explicit unsubscribe, on the assumption that the
        registry treats double-subscribe as a programming error and
        callers will not exercise this path. We log a warning so the
        condition is observable.
        """
        subject = self.result_subject_for(matched_agent_id, correlation_key)

        if correlation_key in self._subscriptions:
            logger.warning(
                "subscribe_reply: replacing existing subscription "
                "(key=%s, subject=%s); previous handle will be leaked",
                correlation_key,
                subject,
            )

        # Register the inbound callback. nats-py expects an async
        # callable here — ``_on_reply_received`` is async.
        subscription = await self._nc.subscribe(
            subject, cb=self._on_reply_received
        )

        # Belt-and-braces flush so a remote server has observed our SUB
        # before any caller publishes. nats-py's ``Client.subscribe``
        # already serialises the SUB write, but the flush makes the
        # subscribe-before-publish invariant robust against transports
        # whose ``subscribe`` returns before the SUB lands at the
        # server.
        flush = getattr(self._nc, "flush", None)
        if flush is not None:
            try:
                await flush()
            except Exception as exc:  # noqa: BLE001
                # Flush failure does not invalidate the subscription —
                # nats-py will redrive on reconnect — but it is
                # observable so log it. The subscribe-before-publish
                # contract is still upheld by ``subscribe`` itself.
                logger.debug(
                    "subscribe_reply: flush after subscribe failed "
                    "(key=%s, error=%s)",
                    correlation_key,
                    exc,
                )

        self._subscriptions[correlation_key] = subscription

    async def unsubscribe_reply(self, correlation_key: str) -> None:
        """Tear down the per-correlation subscription. Idempotent.

        A second call with the same ``correlation_key`` is a no-op —
        the first call removes the handle from the registry, so the
        second observes "nothing to unsubscribe" and returns silently.

        Transport errors during unsubscribe are logged but never
        re-raised: the registry's release path is sync and cannot
        meaningfully act on an unsubscribe failure.
        """
        subscription = self._subscriptions.pop(correlation_key, None)
        if subscription is None:
            # Idempotent path — already torn down (or never subscribed).
            logger.debug(
                "unsubscribe_reply: no active subscription (key=%s)",
                correlation_key,
            )
            return

        try:
            await subscription.unsubscribe()
        except Exception:
            logger.exception(
                "unsubscribe_reply: transport unsubscribe failed "
                "(key=%s); subscription leak possible",
                correlation_key,
            )

    # ------------------------------------------------------------------
    # DispatchCommandPublisher surface — publish one dispatch command
    # ------------------------------------------------------------------

    async def publish_dispatch(
        self,
        attempt: DispatchAttempt,
        parameters: list[DispatchParameter],
    ) -> None:
        """Publish the dispatch command on ``agents.command.{matched_agent_id}``.

        The published message carries:

        * Subject: :func:`command_subject_for`
          (``agents.command.{matched_agent_id}``).
        * Headers:

          * ``correlation_key`` — taken from ``attempt.correlation_key``.
          * ``requesting_agent_id`` — fixed ``"forge"``.
          * ``dispatched_at`` — ISO 8601 UTC timestamp at publish time.

        * Payload: a JSON envelope describing the dispatch attempt and
          the (already-scrubbed-by-caller) parameter list. Sensitive
          parameters carry ``value=None`` so the on-wire form mirrors
          the persisted-row form, satisfying
          ``E.sensitive-parameter-hygiene`` end-to-end.

        PubAck on the audit stream (when JetStream emits one) is logged
        at DEBUG only — it is **not** routed through
        :meth:`CorrelationRegistry.deliver_reply`. The orchestrator
        observes dispatch outcome via the actual reply payload landing
        on the per-correlation subscription.
        """
        subject = self.command_subject_for(attempt.matched_agent_id)
        headers = {
            CORRELATION_KEY_HEADER: attempt.correlation_key,
            REQUESTING_AGENT_HEADER: REQUESTING_AGENT_ID,
            DISPATCHED_AT_HEADER: self._clock.now().isoformat(),
        }
        payload = {
            "resolution_id": attempt.resolution_id,
            "correlation_key": attempt.correlation_key,
            "matched_agent_id": attempt.matched_agent_id,
            "attempt_no": attempt.attempt_no,
            "retry_of": attempt.retry_of,
            "parameters": [
                {
                    "name": parameter.name,
                    # Sensitive scrub mirrors persistence: the value is
                    # dropped on the wire too, not just at rest.
                    "value": None if parameter.sensitive else parameter.value,
                    "sensitive": parameter.sensitive,
                }
                for parameter in parameters
            ],
        }
        body = json.dumps(payload).encode("utf-8")

        ack = await self._nc.publish(subject, body, headers=headers)

        # PubAck is informational only — log at DEBUG and continue. The
        # binding's outcome is determined by the reply payload, never
        # by this ack (C.pubAck-not-success).
        if ack is not None:
            logger.debug(
                "dispatch publish ack subject=%s correlation_key=%s "
                "ack=%r (informational only)",
                subject,
                attempt.correlation_key,
                ack,
            )
        else:
            logger.debug(
                "dispatch publish ok subject=%s correlation_key=%s",
                subject,
                attempt.correlation_key,
            )

    # ------------------------------------------------------------------
    # Inbound reply path — registered as the subscription callback
    # ------------------------------------------------------------------

    async def _on_reply_received(self, msg: Any) -> None:
        """Callback registered with the NATS subscription.

        Extracts ``source_agent_id`` and ``correlation_key`` from the
        message headers, decodes the JSON payload body, and forwards to
        :meth:`CorrelationRegistry.deliver_reply`. The registry enforces
        source authenticity, exactly-once, and wrong-correlation drops —
        the adapter must NOT short-circuit on those conditions, because
        doing so duplicates logic the registry already covers (and would
        be inconsistent with the registry's contract).

        Defensive drops applied here (with WARNING-level logs, never
        the payload body):

        * Missing ``correlation_key`` or ``source_agent_id`` header —
          the message cannot be routed; drop.
        * Payload body is not valid UTF-8 JSON — drop. A future task
          may surface these as ``DispatchError`` outcomes via the
          :func:`forge.dispatch.reply_parser.parse_reply` path; for
          now they are silently dropped at the transport boundary so a
          malformed message can never crash the subscription's task.
        * Payload root is not a JSON object — drop for the same reason.

        The method is ``async def`` because nats-py registers callbacks
        as awaitable handlers; the actual call to
        :meth:`CorrelationRegistry.deliver_reply` is sync (the
        registry's documented contract).
        """
        try:
            headers = getattr(msg, "headers", None) or {}
            correlation_key = headers.get(CORRELATION_KEY_HEADER)
            source_agent_id = headers.get(SOURCE_AGENT_HEADER)
            subject = getattr(msg, "subject", "<unknown>")

            if not correlation_key or not source_agent_id:
                logger.warning(
                    "drop reply: missing required header "
                    "(subject=%s, has_correlation_key=%s, "
                    "has_source_agent_id=%s)",
                    subject,
                    bool(correlation_key),
                    bool(source_agent_id),
                )
                return

            data = getattr(msg, "data", b"") or b""
            try:
                payload = json.loads(data.decode("utf-8")) if data else {}
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                # Never log the raw body — it may contain sensitive
                # values until the dispatcher hands it to the parser.
                logger.warning(
                    "drop reply: malformed payload body "
                    "(key=%s, error=%s)",
                    correlation_key,
                    exc.__class__.__name__,
                )
                return

            if not isinstance(payload, dict):
                logger.warning(
                    "drop reply: payload root is not a JSON object "
                    "(key=%s, got=%s)",
                    correlation_key,
                    type(payload).__name__,
                )
                return

            # Forward to the registry — synchronous by design (see
            # CorrelationRegistry.deliver_reply docstring for the
            # exactly-once rationale).
            self._registry.deliver_reply(
                correlation_key, source_agent_id, payload
            )
        except Exception:  # noqa: BLE001
            # Subscription callbacks must never raise into nats-py's
            # task — a raise here would tear down the subscription and
            # silently lose every subsequent reply on this correlation.
            logger.exception(
                "_on_reply_received: unexpected error; "
                "reply dropped at transport boundary"
            )

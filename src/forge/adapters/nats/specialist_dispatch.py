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
specialist → Forge (reply)        ``agents.result.{matched_agent_id}``
================================  ==================================================

The singular ``agents.command`` / ``agents.result`` convention is the
fleet-wide adoption (DRD-001..004; FEAT-FORGE-002 ADR adoption recorded
in Graphiti ``architecture_decisions``). The reply subject is the plain
3-token ``agents.result.{agent_id}`` — aligned to nats-core
``Topics.Agents.RESULT`` — because the DEPLOYED specialist publishes its
reply there (fire-and-forget branch: forge publishes without ``reply_to``,
so the router envelope-wraps a ``ResultPayload`` onto that topic with NO
headers and the correlation carried in the BODY). A single shared
subscription per agent therefore serves EVERY concurrent in-flight
dispatch to that agent; the registry demuxes each reply to its awaiting
future by the body ``correlation_id`` (DISPATCHFMT+ S3, M4 + M5-reply,
contract decision D2).

Headers on the dispatch command
-------------------------------

* ``correlation_key`` — 32 lowercase hex (per the
  :data:`forge.dispatch.correlation.CORRELATION_KEY_RE` contract).
* ``requesting_agent_id`` — fixed string ``"forge"``.
* ``dispatched_at`` — ISO 8601 UTC timestamp at publish time.

These are retained for **tracing only** — nothing in the deployed
parse-target reads them (D2). In particular the reply path does NOT
require any header: the deployed specialist publishes its reply with no
headers at all.

Reply correlation lifecycle
---------------------------

* :meth:`NatsSpecialistDispatchAdapter.subscribe_reply` is called (per
  correlation) from :meth:`CorrelationRegistry.bind`. The reply
  subscription is **shared per agent**: the first in-flight correlation
  for an agent establishes the underlying NATS subscription on
  ``agents.result.{agent_id}`` (returning ONLY after the SUB command has
  been flushed to the server — the subscribe-before-publish anchor);
  subsequent concurrent correlations to the same agent reuse it without a
  second SUB.
* :meth:`unsubscribe_reply` is called (per correlation) from
  :meth:`CorrelationRegistry.release`. It removes that correlation from
  the agent's in-flight set and tears the shared subscription down ONLY
  when no correlation for that agent remains in flight. It is idempotent —
  a second call with the same correlation key is a no-op.
* :meth:`_on_reply_received` is the per-message callback registered with
  the shared NATS subscription. It parses the body as a nats-core
  :class:`~nats_core.envelope.MessageEnvelope`, reads source identity from
  ``envelope.source_id``, demuxes by the BODY correlation
  (``ResultPayload.correlation_id`` with a fallback to
  ``envelope.correlation_id``), and forwards to
  :meth:`CorrelationRegistry.deliver_reply`. Authentication, exactly-once,
  and the wrong-correlation drop are enforced in the registry, **not**
  here — the adapter simply forwards what it observed.

PubAck semantics
----------------

JetStream's PubAck (when the audit stream is configured to emit one) is
treated as a "publish was sent" signal only — it is logged at DEBUG and
**never** routed through :meth:`CorrelationRegistry.deliver_reply`. The
binding's outcome is determined by the actual reply payload landing on
the shared reply subscription. This mirrors the LES1 parity
rule already enforced in :class:`forge.adapters.nats.PipelinePublisher`.

References
----------

* TASK-SAD-010 — this task.
* TASK-SAD-003 — :class:`CorrelationRegistry` + ``CorrelationKey``.
* TASK-SAD-006 — :class:`DispatchOrchestrator`.
* TASK-SAD-011 — wiring + ``FakeNatsClient`` recording extension.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from nats_core.envelope import EventType, MessageEnvelope
from nats_core.events import CommandPayload, ResultPayload
from pydantic import ValidationError

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

#: Subject template for the specialist → Forge reply. The deployed
#: specialist publishes replies on the plain 3-token
#: ``agents.result.{agent_id}`` (nats-core ``Topics.Agents.RESULT``);
#: correlation lives in the reply BODY, not the subject (M4 + M5-reply,
#: DISPATCHFMT+ S3 — contract decision D2).
RESULT_SUBJECT_TEMPLATE: str = "agents.result.{agent_id}"

#: Header carrying the per-attempt correlation key (32 lowercase hex).
#: Command-side tracing only — no consumer depends on it (D2).
CORRELATION_KEY_HEADER: str = "correlation_key"

#: Header carrying the requesting agent identifier (fixed: ``"forge"``).
REQUESTING_AGENT_HEADER: str = "requesting_agent_id"

#: Header carrying the publish-time ISO 8601 UTC timestamp.
DISPATCHED_AT_HEADER: str = "dispatched_at"

#: Header that historically carried the replying specialist's agent
#: identifier. Retained as an exported constant for back-compat, but the
#: reply path NO LONGER reads it — source identity is taken from
#: ``MessageEnvelope.source_id`` in the reply BODY (D2). The deployed
#: specialist publishes replies with no headers at all.
SOURCE_AGENT_HEADER: str = "source_agent_id"

#: Fixed source identifier stamped on every dispatch command.
REQUESTING_AGENT_ID: str = "forge"

#: Deployed verb the specialist routes a **cooperative cancel** on (O-01).
#: Published on the SAME command subject as a dispatch
#: (``agents.command.{agent_id}``) so a timed-out session can be told to
#: abort and release its single ``-np 1`` seat before forge moves the run to
#: terminal FAILED — killing the zombie-holds-the-seat class (run 0a645e36).
#: It is a plain :class:`nats_core.events.CommandPayload` verb value (exactly
#: like ``greenfield``); the ONE nats-core envelope + ``Topics.Agents.COMMAND``
#: subject are reused, nothing new is put on the wire. The specialist-side
#: handler that maps this verb + ``correlation_id`` onto the running session
#: is the FWD-005 / specialist-repo follow-on (O-02); forge's obligation here
#: is only to EMIT the cancel — an unknown verb is answered fire-and-forget
#: with "Command not supported" and is harmless.
CANCEL_COMMAND: str = "cancel"

#: Default command verb used **only** when a caller does not resolve one — the
#: production path (:func:`forge.pipeline.dispatchers.specialist.dispatch_specialist_stage`
#: → :meth:`DispatchOrchestrator.dispatch`) ALWAYS passes the stage-resolved
#: deployed verb (``greenfield`` for both the PO product_docs stage and the
#: architect from-scratch stage, per contract decision D1). This sentinel keeps
#: the ``CommandPayload.command`` ``min_length=1`` constraint satisfied for the
#: adapter-level tests that exercise the envelope/subject/header seams without
#: caring about the verb. It does not resolve to any deployed handler, so a
#: message carrying it would be answered with "Command 'dispatch' is not
#: supported" — never emitted on the production wire (M2/M3, DISPATCHFMT+ S2).
DISPATCH_COMMAND_PLACEHOLDER: str = "dispatch"

__all__ = [
    "CANCEL_COMMAND",
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
        """Register ``correlation_key`` on the agent's shared reply sub.

        MUST return ONLY after the NATS subscription is fully active —
        i.e. the SUB command has been flushed to the server. The
        :class:`CorrelationRegistry`'s ``bind()`` relies on this
        contract to satisfy the subscribe-before-publish invariant.
        The subscription is shared across concurrent correlations for the
        same agent (see the concrete adapter).
        """
        ...

    async def unsubscribe_reply(self, correlation_key: str) -> None:
        """Release the correlation; tear the shared sub down on the last.

        MUST be idempotent.
        """
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
        *,
        command: str,
        command_args: dict[str, Any] | None = None,
    ) -> None:
        """Publish the dispatch command on the transport.

        ``command`` is the deployed verb the target agent's command_map
        routes on (``greenfield`` for both specialist stages, per D1);
        ``command_args`` is the deployed-handler argument dict (e.g.
        ``{"problem_statement": ...}`` for the PO greenfield handler).
        """
        ...

    async def publish_cancel(self, attempt: DispatchAttempt) -> None:
        """Publish a cooperative cancel for a timed-out dispatch (O-01).

        Fire-and-forget on ``agents.command.{attempt.matched_agent_id}`` so a
        soft-timed-out specialist session aborts and releases its single seat
        before forge moves the run to terminal FAILED.
        """
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
    * A per-**agent** shared subscription handle map plus a per-agent set
      of in-flight correlation keys, so multiple concurrent dispatches to
      the same agent share ONE reply subscription and
      :meth:`unsubscribe_reply` tears it down only once the last in-flight
      correlation for that agent is released — without leaking handles.

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
        # matched_agent_id -> opaque NATS subscription handle for the ONE
        # shared 3-token ``agents.result.{agent_id}`` subscription serving
        # every concurrent in-flight dispatch to that agent. The handle has
        # an ``unsubscribe()`` coroutine.
        self._agent_subscriptions: dict[str, Any] = {}
        # matched_agent_id -> set of correlation keys currently in flight
        # for that agent. The shared subscription is torn down when this
        # set becomes empty (D.unsubscribe-on-timeout, refcounted).
        self._agent_inflight: dict[str, set[str]] = {}
        # correlation_key -> matched_agent_id, so unsubscribe_reply can find
        # the owning agent from the key alone (idempotent: a key absent here
        # has already been released or was never subscribed).
        self._key_to_agent: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Subject helpers — exposed as static methods so tests can assert
    # subject construction without instantiating the adapter.
    # ------------------------------------------------------------------

    @staticmethod
    def command_subject_for(matched_agent_id: str) -> str:
        """Build ``agents.command.{matched_agent_id}``."""
        return COMMAND_SUBJECT_TEMPLATE.format(agent_id=matched_agent_id)

    @staticmethod
    def result_subject_for(matched_agent_id: str) -> str:
        """Build the 3-token ``agents.result.{matched_agent_id}``.

        The reply subject carries NO correlation suffix — the deployed
        specialist publishes every reply for an agent on this one subject
        and forge demuxes by the body correlation (D2, M4).
        """
        return RESULT_SUBJECT_TEMPLATE.format(agent_id=matched_agent_id)

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
        """Ensure the shared ``agents.result.{matched_agent_id}`` sub is active.

        The reply subscription is SHARED per agent: the FIRST in-flight
        correlation for ``matched_agent_id`` opens the underlying NATS
        subscription on the 3-token subject; subsequent concurrent
        correlations to the same agent register their key in the agent's
        in-flight set and reuse the existing subscription (no second SUB).

        Returns ONLY after the underlying NATS subscription is fully
        active — i.e. the SUB command has been flushed to the server.
        This is the subscribe-before-publish anchor the orchestrator's
        invariant depends on (D.subscribe-before-publish-invariant). When
        the subscription already exists, the invariant is trivially
        satisfied (the SUB was flushed by the first correlation), and the
        method still awaits a ``flush`` so a same-tick publish observes it.

        nats-py's :meth:`Client.subscribe` already awaits the SUB write
        before returning the :class:`Subscription`, but we additionally
        invoke ``flush`` (when available) so a remote server has observed
        our SUB before any subsequent publish. ``asyncio.sleep`` is
        **never** used as a synchronisation primitive here — that path was
        the LES1 anti-pattern.

        If the same ``correlation_key`` is subscribed twice this is a
        no-op past registering the key once — the registry treats a
        double-subscribe of one key as a programming error; we log a
        warning so the condition is observable and never open a duplicate
        NATS subscription for it.
        """
        subject = self.result_subject_for(matched_agent_id)

        if correlation_key in self._key_to_agent:
            logger.warning(
                "subscribe_reply: correlation key already subscribed "
                "(key=%s, agent=%s); ignoring duplicate subscribe",
                correlation_key,
                self._key_to_agent[correlation_key],
            )
            return

        # Record the correlation as in-flight for this agent BEFORE the
        # await, so a reply that arrives synchronously inside subscribe()
        # (or on a subscription already open) can be demuxed to it.
        self._key_to_agent[correlation_key] = matched_agent_id
        self._agent_inflight.setdefault(matched_agent_id, set()).add(
            correlation_key
        )

        # Reuse the shared subscription if this agent already has one —
        # concurrent dispatches to the same agent do not open a second SUB.
        if matched_agent_id not in self._agent_subscriptions:
            # Register the inbound callback. nats-py expects an async
            # callable here — ``_on_reply_received`` is async.
            subscription = await self._nc.subscribe(
                subject, cb=self._on_reply_received
            )
            self._agent_subscriptions[matched_agent_id] = subscription

        # Belt-and-braces flush so a remote server has observed our SUB
        # before any caller publishes. nats-py's ``Client.subscribe``
        # already serialises the SUB write, but the flush makes the
        # subscribe-before-publish invariant robust against transports
        # whose ``subscribe`` returns before the SUB lands at the server.
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

    async def unsubscribe_reply(self, correlation_key: str) -> None:
        """Release one correlation; tear down the shared sub on the last. Idempotent.

        Removes ``correlation_key`` from its agent's in-flight set. The
        underlying NATS subscription is shared across every concurrent
        dispatch to that agent, so it is torn down ONLY when no correlation
        for the agent remains in flight.

        A second call with the same ``correlation_key`` is a no-op — the
        first call removes the key from :attr:`_key_to_agent`, so the
        second observes "nothing to release" and returns silently.

        Transport errors during unsubscribe are logged but never re-raised:
        the registry's release path is sync and cannot meaningfully act on
        an unsubscribe failure.
        """
        matched_agent_id = self._key_to_agent.pop(correlation_key, None)
        if matched_agent_id is None:
            # Idempotent path — already released (or never subscribed).
            logger.debug(
                "unsubscribe_reply: no in-flight correlation (key=%s)",
                correlation_key,
            )
            return

        in_flight = self._agent_inflight.get(matched_agent_id)
        if in_flight is not None:
            in_flight.discard(correlation_key)

        # Other dispatches to this agent still in flight — keep the shared
        # subscription alive.
        if in_flight:
            return

        # Last correlation for this agent — drop the in-flight set and tear
        # the shared subscription down.
        self._agent_inflight.pop(matched_agent_id, None)
        subscription = self._agent_subscriptions.pop(matched_agent_id, None)
        if subscription is None:
            logger.debug(
                "unsubscribe_reply: no active subscription (agent=%s, key=%s)",
                matched_agent_id,
                correlation_key,
            )
            return

        try:
            await subscription.unsubscribe()
        except Exception:
            logger.exception(
                "unsubscribe_reply: transport unsubscribe failed "
                "(agent=%s, key=%s); subscription leak possible",
                matched_agent_id,
                correlation_key,
            )

    # ------------------------------------------------------------------
    # DispatchCommandPublisher surface — publish one dispatch command
    # ------------------------------------------------------------------

    async def publish_dispatch(
        self,
        attempt: DispatchAttempt,
        parameters: list[DispatchParameter],
        *,
        command: str = DISPATCH_COMMAND_PLACEHOLDER,
        command_args: dict[str, Any] | None = None,
    ) -> None:
        """Publish the dispatch command on ``agents.command.{matched_agent_id}``.

        Wire format (M1 + M5-command fix, DISPATCHFMT+ S1; verb + dict args,
        M2 + M3, DISPATCHFMT+ S2). The deployed
        specialist parses every inbound message through
        :class:`nats_core.envelope.MessageEnvelope` in
        ``client.subscribe_with_reply`` **before** the router callback runs;
        a message that is not a valid envelope is logged and silently
        dropped (never routed, never replied). Forge therefore publishes a
        canonical nats-core envelope:

        * Subject: :func:`command_subject_for`
          (``agents.command.{matched_agent_id}``).
        * Body: a :class:`nats_core.envelope.MessageEnvelope` JSON document with

          * ``source_id`` — fixed ``"forge"`` (:data:`REQUESTING_AGENT_ID`);
            the specialist reads reply source identity from this field.
          * ``event_type`` — :attr:`EventType.COMMAND` (member exists in the
            deployed nats-core 0.4.0; the router's step-1 gate requires it).
          * ``correlation_id`` — ``attempt.correlation_key``; the deployed
            router demuxes replies by the body correlation value, so it must
            live on the envelope, not only in headers.
          * ``payload`` — a :class:`nats_core.events.CommandPayload`
            (``command`` / ``args`` / ``correlation_id``) as a plain dict.
            ``command`` is the deployed verb the target agent's command_map
            routes on (``greenfield`` for both specialist stages, per D1);
            ``args`` is ``command_args`` — the deployed-handler argument dict
            carrying exactly the keys that handler reads (e.g.
            ``{"problem_statement": <planning request text>}`` for the PO
            greenfield handler). The router's ``_check_required_args`` gate
            enforces those keys (M2 + M3).

        * Headers: ``correlation_key`` / ``requesting_agent_id`` /
          ``dispatched_at`` are retained for **tracing only** — nothing in
          the deployed parse-target depends on them (contract decision D2).

        The forge-local dispatch bookkeeping (``resolution_id``,
        ``attempt_no``, ``retry_of``, ``matched_agent_id``) is **not** put on
        the wire: the specialist ignores it, ``matched_agent_id`` is already
        the subject, and the reply is re-correlated from forge's own registry.

        The FEAT-FORGE-003 ``parameters`` list (correlation-id + forward-context
        audit records) is persisted upstream by
        :func:`forge.dispatch.persistence.persist_resolution` and is **not**
        serialised onto the wire: no deployed greenfield handler (PO or
        architect) reads a ``parameters`` or ``context`` argument, so under
        contract decision D3 those forward-context records are dropped from the
        command args. Sensitive parameters therefore never reach the wire at
        all, satisfying ``E.sensitive-parameter-hygiene`` by construction. The
        deployed-handler inputs travel exclusively via ``command_args``.

        PubAck on the audit stream (when JetStream emits one) is logged
        at DEBUG only — it is **not** routed through
        :meth:`CorrelationRegistry.deliver_reply`. The orchestrator
        observes dispatch outcome via the actual reply payload landing
        on the agent's shared reply subscription (demuxed by body
        correlation).
        """
        subject = self.command_subject_for(attempt.matched_agent_id)
        headers = {
            CORRELATION_KEY_HEADER: attempt.correlation_key,
            REQUESTING_AGENT_HEADER: REQUESTING_AGENT_ID,
            DISPATCHED_AT_HEADER: self._clock.now().isoformat(),
        }
        # M2 + M3 (DISPATCHFMT+ S2): the wire carries the stage-resolved
        # deployed verb and the deployed-handler argument dict. ``parameters``
        # (correlation-id + forward-context audit records) is persisted
        # upstream and deliberately NOT serialised here — no deployed
        # greenfield handler reads it (D3), so it is dropped from the wire.
        command_payload = CommandPayload(
            command=command,
            args=dict(command_args) if command_args else {},
            correlation_id=attempt.correlation_key,
        )
        envelope = MessageEnvelope(
            source_id=REQUESTING_AGENT_ID,
            event_type=EventType.COMMAND,
            correlation_id=attempt.correlation_key,
            payload=command_payload.model_dump(),
        )
        body = envelope.model_dump_json().encode("utf-8")

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

    async def publish_cancel(self, attempt: DispatchAttempt) -> None:
        """Publish a cooperative cancel on ``agents.command.{matched_agent_id}`` (O-01).

        When a planning/dispatch session soft-times out, forge has stopped
        waiting for the reply (the correlation binding is already released by
        the :class:`~forge.dispatch.timeout.TimeoutCoordinator`) but the
        specialist is still running the request, holding the single ``-np 1``
        seat — the zombie-holds-the-seat class (run ``0a645e36``). This method
        tells the specialist to abort that session and free the seat.

        Wire shape (the ONE nats-core envelope — reuses the dispatch shapes,
        invents nothing):

        * Subject: :func:`command_subject_for`
          (``agents.command.{matched_agent_id}`` — nats-core
          ``Topics.Agents.COMMAND``), the SAME subject a dispatch rides.
        * Body: a :class:`nats_core.envelope.MessageEnvelope` with
          ``event_type=EventType.COMMAND`` (the router's step-1 gate) wrapping
          a :class:`nats_core.events.CommandPayload` whose ``command`` is
          :data:`CANCEL_COMMAND` and whose ``args`` + ``correlation_id`` carry
          the timed-out ``attempt.correlation_key`` so the specialist can map
          the cancel onto the running session.
        * Headers: the same tracing triple as a dispatch — nothing depends on
          them (D2).

        Fire-and-forget (no ``reply_to``): forge does not wait on a cancel-ack
        (that would re-introduce an unbounded wait, rule 5); the seat-release
        is the specialist's cooperative response. PubAck, if any, is logged at
        DEBUG only (C.pubAck-not-success). Publish failures are NOT swallowed
        here — the pure-domain caller
        (:meth:`forge.dispatch.orchestrator.DispatchOrchestrator.dispatch`)
        owns the "cancel failed → still fail the run loudly, never hang"
        policy so the timeout outcome is never masked.
        """
        subject = self.command_subject_for(attempt.matched_agent_id)
        headers = {
            CORRELATION_KEY_HEADER: attempt.correlation_key,
            REQUESTING_AGENT_HEADER: REQUESTING_AGENT_ID,
            DISPATCHED_AT_HEADER: self._clock.now().isoformat(),
        }
        command_payload = CommandPayload(
            command=CANCEL_COMMAND,
            args={"correlation_id": attempt.correlation_key},
            correlation_id=attempt.correlation_key,
        )
        envelope = MessageEnvelope(
            source_id=REQUESTING_AGENT_ID,
            event_type=EventType.COMMAND,
            correlation_id=attempt.correlation_key,
            payload=command_payload.model_dump(),
        )
        body = envelope.model_dump_json().encode("utf-8")

        ack = await self._nc.publish(subject, body, headers=headers)

        # The receipt: name the subject + correlation + agent so a soft-timeout
        # cancel is never a silent side-effect (route-and-notify, rule 4).
        logger.info(
            "dispatch.cancel_published subject=%s correlation_key=%s agent=%s "
            "command=%s (soft_timeout — releasing the specialist seat)",
            subject,
            attempt.correlation_key,
            attempt.matched_agent_id,
            CANCEL_COMMAND,
        )
        if ack is not None:
            logger.debug(
                "dispatch cancel publish ack subject=%s correlation_key=%s "
                "ack=%r (informational only)",
                subject,
                attempt.correlation_key,
                ack,
            )

    # ------------------------------------------------------------------
    # Inbound reply path — registered as the subscription callback
    # ------------------------------------------------------------------

    async def _on_reply_received(self, msg: Any) -> None:
        """Callback registered with the shared reply subscription.

        Parses the message body as a nats-core
        :class:`~nats_core.envelope.MessageEnvelope` (the shape the
        DEPLOYED specialist publishes on ``agents.result.{agent_id}``:
        an envelope wrapping a :class:`~nats_core.events.ResultPayload`,
        with NO headers). It then:

        * reads reply source identity from ``envelope.source_id`` (D2 —
          the reply carries no ``source_agent_id`` header);
        * demuxes by the BODY correlation —
          ``ResultPayload.correlation_id`` when the payload validates,
          falling back to ``envelope.correlation_id``;
        * forwards the inner ``ResultPayload`` dict (``envelope.payload``)
          to :meth:`CorrelationRegistry.deliver_reply`.

        The registry enforces source authenticity, exactly-once, and the
        wrong-correlation drop (a reply whose body correlation matches no
        in-flight binding is logged and dropped there). The adapter must
        NOT short-circuit on those conditions — that duplicates registry
        logic. Because the subscription is shared across every concurrent
        dispatch to an agent, this body-correlation demux is the ONLY
        thing routing each reply to the right awaiting future.

        Defensive drops applied here (with WARNING-level logs, never the
        payload body):

        * Empty body, or a body that is not a valid nats-core
          ``MessageEnvelope`` (bad UTF-8 / JSON / schema) — drop. A
          malformed message can never crash the subscription's task.
        * No correlation on either the payload or the envelope — the
          reply cannot be demuxed; drop.

        The method is ``async def`` because nats-py registers callbacks as
        awaitable handlers; the actual call to
        :meth:`CorrelationRegistry.deliver_reply` is sync (the registry's
        documented contract).
        """
        try:
            subject = getattr(msg, "subject", "<unknown>")
            data = getattr(msg, "data", b"") or b""
            if not data:
                logger.warning(
                    "drop reply: empty body (subject=%s)", subject
                )
                return

            try:
                envelope = MessageEnvelope.model_validate_json(data)
            except (ValidationError, ValueError) as exc:
                # ``ValueError`` covers UnicodeDecodeError / JSONDecodeError
                # raised by pydantic's JSON loader. Never log the raw body —
                # it may carry sensitive values until the dispatcher hands
                # it to the parser.
                logger.warning(
                    "drop reply: body is not a valid MessageEnvelope "
                    "(subject=%s, error=%s)",
                    subject,
                    exc.__class__.__name__,
                )
                return

            source_agent_id = envelope.source_id
            payload = envelope.payload

            # Demux by the BODY correlation: prefer the ResultPayload's own
            # correlation_id, fall back to the envelope correlation_id (D2).
            body_correlation: str | None = None
            try:
                body_correlation = ResultPayload.model_validate(
                    payload
                ).correlation_id
            except ValidationError:
                # Not a ResultPayload shape — fall through to the envelope
                # correlation. The registry will drop it if it matches no
                # in-flight binding.
                body_correlation = None
            correlation_key = body_correlation or envelope.correlation_id

            if not correlation_key:
                logger.warning(
                    "drop reply: no correlation on payload or envelope "
                    "(subject=%s, source=%s)",
                    subject,
                    source_agent_id,
                )
                return

            # Forward the inner ResultPayload dict to the registry —
            # synchronous by design (see CorrelationRegistry.deliver_reply
            # for the exactly-once + wrong-correlation-drop rationale).
            self._registry.deliver_reply(
                correlation_key, source_agent_id, payload
            )
        except Exception:  # noqa: BLE001
            # Subscription callbacks must never raise into nats-py's task —
            # a raise here would tear down the SHARED subscription and
            # silently lose every subsequent reply for that agent.
            logger.exception(
                "_on_reply_received: unexpected error; "
                "reply dropped at transport boundary"
            )

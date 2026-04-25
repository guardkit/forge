"""Dispatch orchestrator — single entry point for one dispatch attempt.

The :class:`DispatchOrchestrator` sequences the five pure-domain steps
of one dispatch attempt in the exact order required by FEAT-FORGE-003:

1. **Resolve** the requested capability against a stable
   :meth:`DiscoveryCache.snapshot` view (E.snapshot-stability).
2. **Persist** the resolution row before any wire publish — the
   write-before-send invariant (D.write-before-send-invariant).
3. **Bind** a per-correlation reply subscription via
   :meth:`CorrelationRegistry.bind` — the subscribe-before-publish
   invariant (D.subscribe-before-publish-invariant; LES1).
4. **Publish** the dispatch command via the injected
   :class:`DispatchCommandPublisher` transport adapter.
5. **Wait** for the reply with a hard local cut-off enforced by
   :class:`TimeoutCoordinator`, then **parse** the payload via
   :func:`parse_reply` into a :data:`DispatchOutcome`.

The orchestrator is **pure-domain**: it never imports a transport
type. The transport is injected through the
:class:`DispatchCommandPublisher` :class:`typing.Protocol` so the
NATS-backed adapter (TASK-SAD-010) and in-memory test fakes are
interchangeable.

Reordering any step breaks an invariant the LES1 incident proved we
must never re-introduce. Do not re-order without re-reading
``IMPLEMENTATION-GUIDE.md`` and the LES1 lesson.
"""

from __future__ import annotations

import logging
from typing import Optional, Protocol

from forge.discovery.cache import DiscoveryCache
from forge.discovery.resolve import resolve
from forge.dispatch.correlation import CorrelationRegistry
from forge.dispatch.models import (
    Degraded,
    DispatchAttempt,
    DispatchError,
    DispatchOutcome,
)
from forge.dispatch.persistence import (
    DispatchParameter,
    SqliteHistoryWriter,
    persist_resolution,
)
from forge.dispatch.reply_parser import parse_reply
from forge.dispatch.timeout import TimeoutCoordinator

logger = logging.getLogger(__name__)


# ASSUM-001 — the discovery layer's intent-fallback confidence floor.
# Captured here so the orchestrator does not silently disagree with the
# resolver default if the upstream constant ever shifts.
_MIN_CONFIDENCE: float = 0.7


class DispatchCommandPublisher(Protocol):
    """Transport contract for publishing one dispatch command.

    The orchestrator depends only on this :class:`typing.Protocol` so
    the domain layer stays free of NATS / HTTP imports. The concrete
    NATS-backed implementation is delivered by TASK-SAD-010
    (``forge.adapters.nats.specialist_dispatch.NatsSpecialistDispatchAdapter``);
    tests use in-memory recording fakes.

    ``publish_dispatch`` MUST NOT be invoked until
    :meth:`CorrelationRegistry.bind` has returned — i.e. the reply
    subscription is active end-to-end. The orchestrator enforces this
    ordering by calling ``bind()`` before ``publish_dispatch()``.
    """

    async def publish_dispatch(
        self,
        attempt: DispatchAttempt,
        parameters: list[DispatchParameter],
    ) -> None:
        """Publish the dispatch command on the transport.

        The transport implementation owns subject construction, header
        composition (including the correlation key threaded through
        ``attempt.correlation_key``), and PubAck handling. PubAck on
        the audit stream is NOT a successful reply — see
        ``C.pubAck-not-success`` in TASK-SAD-003.
        """
        ...


class DispatchOrchestrator:
    """Sequence one dispatch attempt: resolve → persist → bind → publish → wait → parse.

    The orchestrator is the single entry point for one dispatch
    attempt. It owns nothing transport-specific; every collaborator is
    injected so the same class drives both the production NATS path
    and the in-memory test path.

    Args:
        cache: Discovery cache providing the snapshot used for
            resolution.
        registry: :class:`CorrelationRegistry` that fabricates
            correlation keys and owns reply-subscription lifecycles.
        timeout: :class:`TimeoutCoordinator` that imposes the hard
            local cut-off and unsubscribes on timeout.
        publisher: Transport-side :class:`DispatchCommandPublisher`.
        db_writer: SQLite-backed :class:`SqliteHistoryWriter` for the
            write-before-send persistence step.
    """

    def __init__(
        self,
        cache: DiscoveryCache,
        registry: CorrelationRegistry,
        timeout: TimeoutCoordinator,
        publisher: DispatchCommandPublisher,
        db_writer: SqliteHistoryWriter,
    ) -> None:
        self._cache = cache
        self._registry = registry
        self._timeout = timeout
        self._publisher = publisher
        self._db_writer = db_writer

    async def dispatch(
        self,
        *,
        capability: str,
        parameters: list[DispatchParameter],
        attempt_no: int = 1,
        retry_of: Optional[str] = None,
        intent_pattern: Optional[str] = None,
        build_id: str = "unknown",
        stage_label: str = "unknown",
    ) -> DispatchOutcome:
        """Execute one dispatch attempt.

        Order is fixed and must not change:

        1. ``cache.snapshot()`` — capture once at the top so a
           concurrent cache mutation cannot affect the resolution
           result (E.snapshot-stability).
        2. :func:`resolve` against the snapshot. ``matched_id is None``
           short-circuits to a :class:`Degraded` outcome — no
           persistence, bind, or publish is performed in that branch.
        3. :func:`persist_resolution` — write-before-send. The
           ``retry_of`` field is stamped onto the resolution copy so
           a retry chain is recoverable from the persistence layer
           alone.
        4. :meth:`CorrelationRegistry.fresh_correlation_key` +
           :meth:`CorrelationRegistry.bind` — subscribe-before-publish.
        5. :meth:`DispatchCommandPublisher.publish_dispatch` — only
           after :meth:`bind` has returned.
        6. :meth:`TimeoutCoordinator.wait_with_timeout` — returns the
           reply payload or ``None`` on the hard cut-off.
        7. :func:`parse_reply` — convert the payload into a concrete
           :data:`DispatchOutcome` discriminated-union member.

        Args:
            capability: Tool name to dispatch against. Mapped to
                :func:`resolve`'s ``tool_name``.
            parameters: Dispatch parameters; sensitive entries are
                scrubbed at the persistence boundary inside
                :func:`persist_resolution`.
            attempt_no: Monotonic attempt counter, starting at 1.
            retry_of: ``resolution_id`` of the previous attempt this
                one is retrying, or ``None`` for the first attempt.
                Stamped onto the persisted resolution row.
            intent_pattern: Optional intent-fallback pattern handed to
                :func:`resolve`.
            build_id: Pipeline build identifier propagated onto the
                persisted :class:`CapabilityResolution`.
            stage_label: Stage label propagated onto the persisted
                resolution.

        Returns:
            A :data:`DispatchOutcome` — one of :class:`SyncResult`,
            :class:`AsyncPending`, :class:`Degraded`, or
            :class:`DispatchError`.

        Raises:
            Exception: Any error raised by ``bind()``,
                ``publish_dispatch()``, or the persistence layer
                propagates unchanged. The :class:`DispatchBuild`
                callback contract surfaces failures via the upstream
                pipeline_consumer's ack callback (FEAT-FORGE-002),
                so silent error swallowing here would corrupt that
                contract.
        """
        # Step 1: stable snapshot. ``DiscoveryCache.snapshot`` returns a
        # shallow copy under its async lock, so any mutation to the
        # cache after this line cannot affect the resolution result —
        # E.snapshot-stability.
        snapshot = await self._cache.snapshot()
        matched_id, resolution = resolve(
            snapshot=snapshot,
            tool_name=capability,
            intent_pattern=intent_pattern,
            min_confidence=_MIN_CONFIDENCE,
            build_id=build_id,
            stage_label=stage_label,
        )

        # Step 2 (early-exit branch): degraded path. No specialist was
        # resolvable, so we MUST NOT persist (no row to anchor a
        # dispatch against), MUST NOT bind, and MUST NOT publish —
        # acceptance criterion "degraded path".
        if matched_id is None:
            logger.info(
                "dispatch.degraded capability=%s reason=no_specialist_resolvable",
                capability,
            )
            return Degraded(
                resolution_id=resolution.resolution_id,
                attempt_no=attempt_no,
                reason="no_specialist_resolvable",
            )

        # Step 2: write-before-send. Stamp ``retry_of`` onto the
        # resolution copy so the persistence row records the retry
        # chain. ``model_copy`` keeps the original immutable.
        resolution_to_persist = (
            resolution.model_copy(update={"retry_of": retry_of})
            if retry_of is not None
            else resolution
        )
        persist_resolution(
            resolution_to_persist,
            parameters,
            db_writer=self._db_writer,
        )

        # Step 3: subscribe-before-publish. ``bind()`` returns only
        # after the transport subscription is active end-to-end, so
        # any reply that arrives between bind() and the publish below
        # is routable.
        correlation_key = self._registry.fresh_correlation_key()
        binding = await self._registry.bind(correlation_key, matched_id)

        # Step 4: publish the command. ``DispatchAttempt`` is the
        # transport-facing record; the publisher reads it for subject
        # routing and header composition.
        attempt = DispatchAttempt(
            resolution_id=resolution.resolution_id,
            correlation_key=correlation_key,
            matched_agent_id=matched_id,
            attempt_no=attempt_no,
            retry_of=retry_of,
        )
        await self._publisher.publish_dispatch(attempt, parameters)

        # Step 5: wait for the reply (hard cut-off enforced by the
        # timeout coordinator, which also releases the binding on
        # both success and timeout). ``None`` means the hard cut-off
        # fired before any authentic reply arrived.
        payload = await self._timeout.wait_with_timeout(binding)
        if payload is None:
            logger.info(
                "dispatch.local_timeout resolution_id=%s correlation_key=%s",
                resolution.resolution_id,
                correlation_key,
            )
            return DispatchError(
                resolution_id=resolution.resolution_id,
                attempt_no=attempt_no,
                error_explanation="local_timeout",
            )

        # Step 6: parse the reply payload into a concrete outcome.
        return parse_reply(
            payload,
            resolution_id=resolution.resolution_id,
            attempt_no=attempt_no,
        )


__all__ = ["DispatchCommandPublisher", "DispatchOrchestrator"]

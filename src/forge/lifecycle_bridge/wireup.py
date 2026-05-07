"""Wireup for :class:`LifecycleBridge` into ``forge serve`` startup (T4).

This module is the **consumer side** of the §4 Integration Contract for
``STREAM_EVENT_SCHEMA`` (produced by T3). It connects three previously
isolated components into the running daemon:

1. **The pipeline consumer** (T1) — exposes
   :data:`InFlightAckRegistry` (``async (feature_id, correlation_id,
   handle) -> None``). The wireup binds an implementation that records
   the ack handle for terminal-state ack and starts an asyncio observer
   task.
2. **The lifecycle bridge** (T2) — owns the SQLite-backed registry of
   in-flight builds. The wireup calls :meth:`LifecycleBridge.attach`
   inside the registry callable and :meth:`LifecycleBridge.detach` on
   terminal envelope arrival.
3. **The SSE translator** (T3) — converts ``langgraph_sdk`` ``StreamPart``
   events into typed :data:`PipelineEvent` payloads. The wireup runs
   the translator inside its per-build observer task and routes each
   non-``None`` payload to the publisher.

Acceptance criteria mapping
---------------------------

* AC-1: :meth:`LifecycleBridgeWireup.register_ack_handle` invokes
  :meth:`LifecycleBridge.attach` (which writes the SQLite row) and
  starts the per-build SSE observer task.
* AC-2: Each translated :data:`PipelineEvent` is published via the
  injected :class:`PipelinePublisher` — the wireup never constructs
  payloads itself.
* AC-3: ``correlation_id`` from the inbound :class:`BuildContext` is
  threaded onto every emitted envelope. The translator already attaches
  it to the typed payload (T3 AC-6); the wireup forwards the payload
  unchanged so the publisher's central envelope-construction reads
  the field via ``getattr(payload, "correlation_id", None)``.
  See :class:`tests.forge.test_pipeline_consumer_correlation_id.TestWireupPublishCallsThreadCorrelationId`
  for the AC-3 AST guard.
* AC-4: :meth:`_observer_loop` invokes :meth:`BuildAckHandle.ack` and
  :meth:`LifecycleBridge.detach` on terminal envelope arrival.
* AC-5: The observer is an :class:`asyncio.Task` keyed by ``feature_id``;
  supervisor queries against the registry are answered by the bridge's
  in-memory dict and never block on the SSE stream.
* AC-6: :meth:`LifecycleBridgeWireup.shutdown` cancels every observer
  task and returns within ``shutdown_timeout_seconds`` (default 5.0s).

Stream source contract
----------------------

The SSE transport is injected via the :class:`StreamSource` Protocol
rather than constructed inside this module:

* Production (``forge serve``) wires
  :func:`forge.lifecycle_bridge.langgraph_stream_source` (TASK-FRR-PEB-005,
  not implemented here) which adapts ``langgraph_sdk.client.runs.join_stream``
  into the Protocol.
* Tests pass an in-memory async generator that yields recorded
  :class:`StreamPart` fixtures from
  ``tests/forge/lifecycle_bridge/fixtures/sse_stream_canonical.jsonl``.

This indirection isolates the wireup from the langgraph-runner sidecar
in unit tests and lets the Wave-2 deliverable land before the sidecar
deployment finishes (TASK-FORGE-FRR-F010I/J).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, AsyncIterator, Awaitable, Callable, Protocol

from nats_core.events import (
    BuildCancelledPayload,
    BuildCompletePayload,
    BuildFailedPayload,
    BuildPausedPayload,
    BuildResumedPayload,
    BuildStartedPayload,
    StageCompletePayload,
)

from forge.adapters.nats.pipeline_publisher import PipelinePublisher
from forge.lifecycle_bridge.bridge import (
    AckHandle,
    BuildContext,
    LifecycleBridge,
)
from forge.lifecycle_bridge.translation import (
    PipelineEvent,
    StreamEventTranslator,
)
from forge.pipeline.build_ack_handle import BuildAckHandle

logger = logging.getLogger(__name__)


__all__ = [
    "DEFAULT_DEADLINE_SECONDS",
    "DEFAULT_SHUTDOWN_TIMEOUT_SECONDS",
    "LifecycleBridgeWireup",
    "StreamSource",
    "TERMINAL_PAYLOAD_TYPES",
]


#: Per-build deadline used when the consumer-side wiring does not
#: provide an explicit value. ASSUM-003 in the FEAT-PEBR brief pins the
#: per-build deadline at 300s; the bridge is the canonical enforcer of
#: that bound (T8 reads :attr:`BridgeRegistryEntry.deadline_at`).
DEFAULT_DEADLINE_SECONDS: int = 300

#: Default upper bound for :meth:`LifecycleBridgeWireup.shutdown`. AC-6
#: requires the wireup to drain in-flight observer tasks within 5
#: seconds so ``forge serve`` shutdown does not hang the supervisor on
#: a slow SSE peer.
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS: float = 5.0


#: Tuple of typed payload classes that mark a terminal lifecycle. When
#: the observer loop sees a payload whose type is in this tuple it
#: invokes :meth:`BuildAckHandle.ack` and :meth:`LifecycleBridge.detach`
#: (AC-4) before exiting the loop.
TERMINAL_PAYLOAD_TYPES: tuple[type, ...] = (
    BuildCompletePayload,
    BuildFailedPayload,
    BuildCancelledPayload,
)


#: Mapping from typed :data:`PipelineEvent` payload class to the
#: :class:`PipelinePublisher` method that publishes it. Centralised so
#: the observer loop's dispatch is one ``getattr`` rather than a long
#: ``isinstance`` chain — and so adding a new envelope type is a single
#: row edit rather than a scattered switch.
_PUBLISH_METHOD_TABLE: dict[type, str] = {
    BuildStartedPayload: "publish_build_started",
    StageCompletePayload: "publish_stage_complete",
    BuildCompletePayload: "publish_build_complete",
    BuildFailedPayload: "publish_build_failed",
    BuildPausedPayload: "publish_build_paused",
    BuildResumedPayload: "publish_build_resumed",
    BuildCancelledPayload: "publish_build_cancelled",
}


#: Subject-segment fragment per typed payload class. Mirrors
#: :attr:`forge.adapters.nats.pipeline_publisher.PipelinePublisher._EVENT_TABLE`
#: so the WARNING log for a publish failure (TASK-FRR-PEB-011 AC-1)
#: surfaces the same subject string the publisher would have written
#: to JetStream.
_SUBJECT_SEGMENT_TABLE: dict[type, str] = {
    BuildStartedPayload: "build-started",
    StageCompletePayload: "stage-complete",
    BuildCompletePayload: "build-complete",
    BuildFailedPayload: "build-failed",
    BuildPausedPayload: "build-paused",
    BuildResumedPayload: "build-resumed",
    BuildCancelledPayload: "build-cancelled",
}


def _subject_for_payload_type(payload_type: type, feature_id: str) -> str:
    """Return the canonical ``pipeline.{event}.{feature_id}`` subject.

    Used only by the WARNING log path on a publish failure (AC-1) so an
    operator reading the log line can grep the JetStream consumer for
    the corresponding redelivery without having to introspect the
    payload type to subject mapping themselves.
    """
    segment = _SUBJECT_SEGMENT_TABLE.get(payload_type, "unknown")
    return f"pipeline.{segment}.{feature_id}"


class StreamSource(Protocol):
    """Async stream factory: ``feature_id → AsyncIterator[StreamPart]``.

    Concrete implementations open the SSE connection (or replay a
    fixture) and yield ``langgraph_sdk.schema.StreamPart`` events. The
    wireup's observer loop drives the iterator until it raises
    :class:`StopAsyncIteration`, the build hits a terminal envelope, or
    the observer task is cancelled by :meth:`LifecycleBridgeWireup.shutdown`.

    Implementations MUST NOT raise on missing/late stream starts —
    yielding zero events is a legitimate "no live SSE yet" signal that
    the observer treats as a clean exit (the JetStream redelivery on
    ``ack_wait`` expiry will re-trigger registration).
    """

    def __call__(
        self,
        *,
        feature_id: str,
        thread_id: str | None,
        run_id: str | None,
    ) -> AsyncIterator[Any]:  # pragma: no cover - protocol stub
        ...


# ---------------------------------------------------------------------------
# Identity provider — resolves thread_id / run_id post-dispatch
# ---------------------------------------------------------------------------


#: ``async (feature_id) -> (thread_id, run_id) | None``.
#:
#: The pipeline consumer registers an ack handle BEFORE
#: :func:`dispatch_autobuild_async` runs, so at registration time the
#: langgraph-runner has not yet returned the ``thread_id`` / ``run_id``
#: pair the SSE stream is keyed on. This callable is awaited by the
#: observer loop just before opening the stream — production wiring
#: reads from the ``async_tasks`` SQLite mirror (DDR-006); unit tests
#: pass a deterministic fake. ``None`` means "no run yet"; the observer
#: backs off and retries up to ``identity_resolution_attempts`` times.
IdentityProvider = Callable[[str], Awaitable[tuple[str, str] | None]]


def _default_identity_provider() -> IdentityProvider:
    """Return an identity provider that always reports "no identity yet".

    Useful for tests that exercise the registration path without driving
    the SSE observer to completion (the observer exits cleanly when the
    identity never resolves — see
    :meth:`LifecycleBridgeWireup._wait_for_identity`).
    """

    async def _provider(feature_id: str) -> tuple[str, str] | None:
        return None

    return _provider


# ---------------------------------------------------------------------------
# LifecycleBridgeWireup
# ---------------------------------------------------------------------------


class LifecycleBridgeWireup:
    """Owns the bridge ↔ consumer ↔ translator wiring for ``forge serve``.

    One instance per ``forge serve`` daemon (composed in
    :func:`forge.cli._serve_production.bind_production_serve`). The
    instance is constructed before the durable consumer attaches and
    its :meth:`register_ack_handle` is threaded onto the
    :class:`PipelineConsumerDeps.register_ack_handle` field via
    :func:`forge.cli._serve_deps.build_pipeline_consumer_deps`.

    Args:
        bridge: The :class:`LifecycleBridge` (T2) that owns the SQLite
            registry. The wireup calls :meth:`~LifecycleBridge.attach`
            in :meth:`register_ack_handle` and
            :meth:`~LifecycleBridge.detach` on terminal envelope arrival.
        translator: The :class:`StreamEventTranslator` (T3) that maps
            ``StreamPart`` events into typed :data:`PipelineEvent`
            payloads. The wireup constructs one translator per
            instance — the translator is stateful per-feature internally
            and tolerates multiple builds against the same instance as
            long as their ``feature_id`` values are distinct.
        publisher: The shared :class:`PipelinePublisher` that already
            backs the consumer's ``publish_build_failed`` wrapper. Reusing
            it satisfies AC-2 (Bridge MUST NOT construct payloads
            directly) and ASSUM-011 (single shared NATS client).
        stream_source: A :class:`StreamSource` callable that yields
            ``StreamPart`` events for a given ``(feature_id, thread_id,
            run_id)`` triple.
        identity_provider: An :data:`IdentityProvider` callable that
            resolves ``(thread_id, run_id)`` for a feature_id. Defaults
            to a no-op provider that yields ``None`` — sufficient for
            unit tests that drive the observer with a pre-built
            ``stream_source`` ignoring the identity arguments.
        deadline_seconds: Per-build deadline written to the registry
            row's ``deadline_at`` column. Defaults to
            :data:`DEFAULT_DEADLINE_SECONDS` (300s).
        identity_resolution_attempts: Maximum number of
            ``identity_provider`` polls before the observer concludes
            the run never started and exits cleanly. Defaults to 3 — at
            production poll cadence (~1s) this gives the runner ~3s to
            mint its IDs before the observer surrenders, which matches
            the consumer's own 5s ack budget for the queued envelope.
        identity_poll_interval_seconds: Sleep between identity polls.
            Defaults to 1.0; tests pass 0.0 for synchronous semantics.
        shutdown_timeout_seconds: Upper bound on
            :meth:`shutdown`. Defaults to
            :data:`DEFAULT_SHUTDOWN_TIMEOUT_SECONDS` (5.0s) per AC-6.
        clock: Optional callable returning a UTC :class:`datetime`. Tests
            inject a deterministic clock so the registry's
            ``deadline_at`` column is reproducible. Defaults to
            :func:`datetime.now`.
    """

    def __init__(
        self,
        *,
        bridge: LifecycleBridge,
        translator: StreamEventTranslator,
        publisher: PipelinePublisher,
        stream_source: StreamSource,
        identity_provider: IdentityProvider | None = None,
        deadline_seconds: int = DEFAULT_DEADLINE_SECONDS,
        identity_resolution_attempts: int = 3,
        identity_poll_interval_seconds: float = 1.0,
        shutdown_timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(bridge, LifecycleBridge):
            raise TypeError(
                "LifecycleBridgeWireup: bridge must be a LifecycleBridge; "
                f"got {type(bridge).__name__}"
            )
        if not isinstance(translator, StreamEventTranslator):
            raise TypeError(
                "LifecycleBridgeWireup: translator must be a "
                "StreamEventTranslator; got "
                f"{type(translator).__name__}"
            )
        if publisher is None:
            raise ValueError(
                "LifecycleBridgeWireup: publisher is required (AC-2 — "
                "Bridge MUST NOT construct payloads directly; the "
                "publisher is the only emit site)"
            )
        if stream_source is None:
            raise ValueError(
                "LifecycleBridgeWireup: stream_source is required"
            )
        if deadline_seconds <= 0:
            raise ValueError(
                "LifecycleBridgeWireup: deadline_seconds must be positive"
            )

        self._bridge = bridge
        self._translator = translator
        self._publisher = publisher
        self._stream_source = stream_source
        self._identity_provider = (
            identity_provider
            if identity_provider is not None
            else _default_identity_provider()
        )
        self._deadline_seconds = deadline_seconds
        self._identity_resolution_attempts = max(
            1, int(identity_resolution_attempts)
        )
        self._identity_poll_interval_seconds = max(
            0.0, float(identity_poll_interval_seconds)
        )
        self._shutdown_timeout_seconds = max(
            0.0, float(shutdown_timeout_seconds)
        )
        self._clock = clock if clock is not None else self._default_clock
        # Per-feature observer tasks. Keyed on ``feature_id`` (AC-5);
        # supervisor queries answered from the bridge's in-memory dict
        # never traverse this map.
        self._observers: dict[str, asyncio.Task[None]] = {}
        # Per-feature handle book-keeping so the observer can ack on
        # terminal arrival even when the consumer's own reference has
        # been released.
        self._handles: dict[str, BuildAckHandle] = {}
        # Tracks shutdown so a late ``register_ack_handle`` call after
        # ``shutdown()`` has begun raises rather than silently leaking
        # an observer that will never be cancelled.
        self._shutting_down: bool = False

    # ------------------------------------------------------------------
    # InFlightAckRegistry entrypoint
    # ------------------------------------------------------------------

    async def register_ack_handle(
        self,
        feature_id: str,
        correlation_id: str,
        handle: BuildAckHandle,
    ) -> None:
        """Implement :data:`InFlightAckRegistry` for the pipeline consumer.

        AC-1: This is the consumer-bridge boundary. Inbound
        ``pipeline.build-queued.{feature_id}`` arrives at the consumer;
        once validation passes, the consumer calls this method with the
        ack handle bound to the underlying JetStream ``Msg``. The wireup:

        1. Builds a :class:`BuildContext` (the bridge's input shape).
        2. Mints an opaque :class:`AckHandle` token persisted in the
           SQLite registry.
        3. Calls :meth:`LifecycleBridge.attach` synchronously — the
           registry row exists by the time this method returns so a
           supervisor query immediately afterwards sees the in-flight
           build.
        4. Schedules the per-build observer task with
           :func:`asyncio.create_task` and returns. The task runs
           independently of the consumer's fetch loop (AC-5).

        Args:
            feature_id: Primary identifier of the in-flight build.
            correlation_id: F010C correlation-id of the inbound envelope.
            handle: The :class:`BuildAckHandle` bound to the underlying
                JetStream ``Msg``. The wireup retains a reference so
                the observer's terminal-arrival ack does not race the
                consumer's local reference cleanup.

        Raises:
            ValueError: If ``feature_id`` or ``correlation_id`` is empty.
            RuntimeError: If :meth:`shutdown` has been called.
        """
        if self._shutting_down:
            raise RuntimeError(
                "LifecycleBridgeWireup.register_ack_handle: wireup is "
                "shutting down; refusing to attach a new build "
                f"(feature_id={feature_id!r})"
            )
        if not feature_id:
            raise ValueError(
                "LifecycleBridgeWireup.register_ack_handle: feature_id "
                "must be non-empty"
            )
        if not correlation_id:
            raise ValueError(
                "LifecycleBridgeWireup.register_ack_handle: correlation_id "
                "must be non-empty"
            )
        if handle is None:
            raise ValueError(
                "LifecycleBridgeWireup.register_ack_handle: handle must "
                "be a BuildAckHandle (got None)"
            )

        # Idempotency: a second registration for the same feature_id is
        # a benign re-dispatch (consumer redelivery, supervisor
        # re-attach). Keep the first observer running and drop the
        # second handle silently — the consumer's flag-based ack
        # idempotency means whichever handle wins ack() is fine.
        if feature_id in self._observers:
            existing = self._observers[feature_id]
            if not existing.done():
                logger.info(
                    "wireup.register_ack_handle: feature_id=%s already "
                    "has a live observer; ignoring duplicate registration "
                    "(correlation_id=%s)",
                    feature_id,
                    correlation_id,
                )
                return

        # Build the context the bridge expects. ``thread_id`` and
        # ``run_id`` are not yet known at registration time — they are
        # minted by the langgraph-runner when ``dispatch_autobuild_async``
        # invokes ``start_async_task``. We persist placeholder values
        # in the registry now so the row exists for supervisor queries
        # (AC-5); the observer task overwrites them via
        # :meth:`BridgeRegistry.update_lifecycle` once
        # ``identity_provider`` resolves.
        deadline_at = self._clock() + timedelta(
            seconds=self._deadline_seconds
        )
        ack_handle_token = self._mint_ack_handle_token()
        bridge_ack_handle = AckHandle(token=ack_handle_token)
        # Placeholder identifiers so the registry row is well-formed.
        # The observer rewrites these once the identity provider
        # resolves (TASK-FRR-PEB-005 wires the production provider).
        build_context = BuildContext(
            feature_id=feature_id,
            thread_id=f"pending-{feature_id}",
            run_id=f"pending-{feature_id}",
            correlation_id=correlation_id,
            deadline_at=deadline_at,
        )

        # Attach is synchronous (registry write); supervisor queries see
        # the row immediately after this returns.
        self._bridge.attach(build_context, bridge_ack_handle)
        self._handles[feature_id] = handle

        # Start the observer task. ``create_task`` schedules immediately
        # and returns; the consumer's fetch loop unblocks on this
        # method's return without waiting for the observer to complete
        # (AC-5: supervisor remains responsive).
        task = asyncio.create_task(
            self._observer_loop(build_context, handle),
            name=f"lifecycle-bridge-observer-{feature_id}",
        )
        self._observers[feature_id] = task
        logger.info(
            "wireup.register_ack_handle: attached feature_id=%s "
            "correlation_id=%s; observer task scheduled "
            "(deadline_at=%s)",
            feature_id,
            correlation_id,
            deadline_at.isoformat(),
        )

    # ------------------------------------------------------------------
    # Observer task — per-build SSE consumer loop
    # ------------------------------------------------------------------

    async def _observer_loop(
        self,
        context: BuildContext,
        handle: BuildAckHandle,
    ) -> None:
        """Drive the SSE stream for one build to terminal arrival.

        The loop:

        1. Resolves ``(thread_id, run_id)`` via :data:`IdentityProvider`
           (the consumer's registration is BEFORE
           ``dispatch_autobuild_async`` runs, so the IDs are not yet
           known at attach time). Times out after
           ``identity_resolution_attempts`` polls; on timeout, exits
           cleanly without ack/nak so JetStream ``ack_wait`` expiry
           re-triggers the consumer (defence against missed dispatches).
        2. Opens the :class:`StreamSource` async iterator and forwards
           each :class:`StreamPart` into the translator.
        3. Publishes every non-``None`` :data:`PipelineEvent` via the
           injected :class:`PipelinePublisher` (AC-2). Publish errors
           are logged at WARNING and **do not** terminate the loop —
           a transient publish failure on a mid-stream event must not
           leave the build orphaned.
        4. On terminal envelope arrival (AC-4): calls
           :meth:`BuildAckHandle.ack` and :meth:`LifecycleBridge.detach`
           and exits the loop cleanly.

        ``asyncio.CancelledError`` propagates so :meth:`shutdown` can
        drain the loop on daemon teardown (AC-6); every other exception
        is caught and logged so a translator/publisher bug cannot crash
        the daemon.
        """
        feature_id = context.feature_id
        correlation_id = context.correlation_id
        try:
            identity = await self._wait_for_identity(feature_id)
            thread_id, run_id = identity if identity is not None else (None, None)

            stream_iter = self._stream_source(
                feature_id=feature_id,
                thread_id=thread_id,
                run_id=run_id,
            )
            terminal_seen = False
            async for stream_part in stream_iter:
                try:
                    event = self._translator.translate(stream_part, context)
                except Exception as exc:  # noqa: BLE001
                    # A translator bug must not break the observer loop —
                    # log and skip the offending part. The fallback path
                    # is JetStream ``ack_wait`` redelivery if the build
                    # never reaches terminal.
                    logger.warning(
                        "wireup._observer_loop: translator raised (%s) for "
                        "feature_id=%s; skipping stream part",
                        exc,
                        feature_id,
                    )
                    continue
                if event is None:
                    continue
                published_ok = await self._publish_event(event, feature_id)
                if isinstance(event, TERMINAL_PAYLOAD_TYPES):
                    if published_ok:
                        terminal_seen = True
                        await self._on_terminal(
                            handle, feature_id, correlation_id
                        )
                        break
                    # TASK-FRR-PEB-011 AC-2/AC-3: terminal envelope publish
                    # failed (transient broker error / network blip). Do
                    # NOT mark the build "terminal-published" in SQLite,
                    # do NOT ack the inbound build-queued message — the
                    # JetStream consumer will redeliver and the bridge's
                    # T9 recovery cycle will retry the publish on the
                    # next observation. ADR-ARCH-008: SQLite is
                    # source-of-truth; transient broker failures must
                    # NOT corrupt build state.
                    logger.warning(
                        "wireup._observer_loop: terminal envelope publish "
                        "failed for feature_id=%s correlation_id=%s "
                        "payload_type=%s; leaving SQLite registry row "
                        "intact and inbound build-queued un-acked "
                        "(JetStream redelivery + recover_in_flight will "
                        "retry — no spurious ack)",
                        feature_id,
                        correlation_id,
                        type(event).__name__,
                    )
                    break

            if not terminal_seen:
                # Stream closed without a terminal envelope — the
                # build's outcome is unknown to the bridge. Do NOT ack:
                # JetStream ``ack_wait`` will redeliver and the consumer
                # will re-register. Log so operators see the orphaned
                # observer.
                logger.warning(
                    "wireup._observer_loop: stream for feature_id=%s "
                    "ended without a terminal envelope; leaving inbound "
                    "queued message un-acked (JetStream will redeliver)",
                    feature_id,
                )
        except asyncio.CancelledError:
            # ``shutdown()`` cancelled the task. Persist the latest
            # ``last_event_id`` semantics live in the bridge's
            # ``BridgeRegistry`` — the row stays in place across restarts
            # (recover_in_flight reads it on boot). Re-raise so the
            # gather() in shutdown() unblocks.
            logger.info(
                "wireup._observer_loop: cancelled feature_id=%s "
                "correlation_id=%s — leaving registry row for "
                "recover_in_flight",
                feature_id,
                correlation_id,
            )
            raise
        except Exception as exc:  # noqa: BLE001
            # Defensive: any unexpected failure inside the observer
            # MUST NOT propagate up to the daemon. Log loudly; the
            # JetStream redelivery + recover_in_flight on next boot is
            # the recovery path.
            logger.exception(
                "wireup._observer_loop: unexpected exception (%s) for "
                "feature_id=%s correlation_id=%s; observer exiting",
                exc,
                feature_id,
                correlation_id,
            )
        finally:
            # Drop ourselves from the live observer set so a future
            # registration for the same feature_id can succeed.
            self._observers.pop(feature_id, None)
            self._handles.pop(feature_id, None)

    # ------------------------------------------------------------------
    # Publisher dispatch
    # ------------------------------------------------------------------

    async def _publish_event(self, event: PipelineEvent, feature_id: str) -> bool:
        """Dispatch ``event`` to the matching :class:`PipelinePublisher` method.

        AC-2: this method is the **only** publish site in the wireup.
        It looks up the publisher method by the event's concrete type
        and forwards the payload unchanged — the translator already
        attached the correlation_id to the typed payload (T3 AC-6), so
        the publisher's central envelope-construction path threads it
        onto the outbound envelope without any additional work here.

        Publish failures are logged at WARNING; they do not interrupt
        the observer loop so a transient broker hiccup on a mid-build
        ``stage-complete`` cannot orphan the build. The return value
        signals success (``True``) or failure (``False``) so the
        observer's terminal-arrival path (TASK-FRR-PEB-011 AC-2/AC-3)
        can refuse to ack / detach when the *terminal* envelope failed
        to publish — preserving SQLite state for the T9 recovery cycle.
        """
        method_name = _PUBLISH_METHOD_TABLE.get(type(event))
        if method_name is None:
            logger.warning(
                "wireup._publish_event: no publisher method registered "
                "for payload type %s (feature_id=%s); dropping event",
                type(event).__name__,
                feature_id,
            )
            return False
        publish = getattr(self._publisher, method_name, None)
        if publish is None:
            logger.warning(
                "wireup._publish_event: publisher missing method %s "
                "(feature_id=%s); dropping event",
                method_name,
                feature_id,
            )
            return False
        try:
            await publish(event)
            return True
        except Exception as exc:  # noqa: BLE001
            # PublishFailure (or transport-level error) must not crash
            # the observer. AC-1: log WARNING with subject + correlation_id
            # so operators can correlate the failure with the inbound
            # envelope. For mid-stream events the loop continues; for
            # terminal events the caller refuses to ack/detach (AC-2/AC-3).
            subject = _subject_for_payload_type(type(event), feature_id)
            cid = getattr(event, "correlation_id", None)
            logger.warning(
                "wireup._publish_event: publish via %s raised (%s) for "
                "feature_id=%s subject=%s correlation_id=%s; observer "
                "continues (caller decides whether to ack)",
                method_name,
                exc,
                feature_id,
                subject,
                cid,
            )
            return False

    # ------------------------------------------------------------------
    # Terminal handling
    # ------------------------------------------------------------------

    async def _on_terminal(
        self,
        handle: BuildAckHandle,
        feature_id: str,
        correlation_id: str,
    ) -> None:
        """Run the terminal-arrival sequence (AC-4).

        Order is load-bearing:

        1. ``handle.ack()`` first — ack the inbound JetStream message
           so a redelivery does not re-trigger the build between the
           ack site and the registry delete.
        2. ``bridge.detach()`` second — remove the registry row. If
           ``ack`` raised the row stays in place so the next boot's
           ``recover_in_flight`` sweep sees the build and can re-attach.

        Both calls are guarded so a transient transport error in either
        cannot leave the observer loop unable to exit.
        """
        try:
            await handle.ack()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "wireup._on_terminal: handle.ack() raised (%s) for "
                "feature_id=%s correlation_id=%s; leaving registry row "
                "in place for recover_in_flight",
                exc,
                feature_id,
                correlation_id,
            )
            return
        try:
            self._bridge.detach(feature_id, correlation_id=correlation_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "wireup._on_terminal: bridge.detach raised (%s) for "
                "feature_id=%s correlation_id=%s; row may be stale",
                exc,
                feature_id,
                correlation_id,
            )

    # ------------------------------------------------------------------
    # Identity resolution
    # ------------------------------------------------------------------

    async def _wait_for_identity(
        self, feature_id: str
    ) -> tuple[str, str] | None:
        """Poll :data:`IdentityProvider` until it returns a non-``None`` pair.

        Returns ``None`` when ``identity_resolution_attempts`` polls
        have all returned ``None`` — the observer treats that as "no
        run yet" and exits cleanly.
        """
        for attempt in range(self._identity_resolution_attempts):
            try:
                identity = await self._identity_provider(feature_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "wireup._wait_for_identity: identity_provider raised "
                    "(%s) for feature_id=%s attempt=%d; treating as None",
                    exc,
                    feature_id,
                    attempt,
                )
                identity = None
            if identity is not None:
                return identity
            if attempt + 1 < self._identity_resolution_attempts:
                await asyncio.sleep(self._identity_poll_interval_seconds)
        logger.info(
            "wireup._wait_for_identity: identity unresolved for "
            "feature_id=%s after %d attempts; observer exits",
            feature_id,
            self._identity_resolution_attempts,
        )
        return None

    # ------------------------------------------------------------------
    # Shutdown — drain observer tasks within timeout
    # ------------------------------------------------------------------

    async def shutdown(self) -> None:
        """Cancel every observer task and return within timeout (AC-6).

        Ordering:

        1. Flip ``_shutting_down`` so any racing
           :meth:`register_ack_handle` call rejects immediately.
        2. Cancel every live observer task; ``asyncio.CancelledError``
           propagates through :meth:`_observer_loop` and triggers the
           per-task ``finally`` cleanup.
        3. Wait for every task with ``asyncio.wait_for`` bounded by
           ``shutdown_timeout_seconds``. On timeout, log a warning and
           let the daemon proceed — the bridge's registry row stays in
           place so the next boot's ``recover_in_flight`` sweep can
           re-attach.
        4. Forward to :meth:`LifecycleBridge.shutdown` so the bridge's
           in-memory bookkeeping is also drained.
        """
        self._shutting_down = True
        live = [task for task in self._observers.values() if not task.done()]
        for task in live:
            task.cancel()
        if live:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*live, return_exceptions=True),
                    timeout=self._shutdown_timeout_seconds,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "wireup.shutdown: observer drain exceeded %.2fs; "
                    "%d task(s) still pending — daemon proceeding "
                    "(recover_in_flight on next boot will re-attach)",
                    self._shutdown_timeout_seconds,
                    sum(1 for t in live if not t.done()),
                )
        # Always clear the bookkeeping so a re-boot in the same process
        # (e.g. test fixtures) starts from a clean state.
        self._observers.clear()
        self._handles.clear()
        self._bridge.shutdown()
        logger.info(
            "wireup.shutdown: drained %d observer task(s)",
            len(live),
        )

    # ------------------------------------------------------------------
    # Introspection — used by the supervisor's responsiveness surface
    # ------------------------------------------------------------------

    def active_observer_count(self) -> int:
        """Return the number of live observer tasks.

        AC-5: the supervisor's responsiveness surface uses this to
        report in-flight builds without traversing the SQLite registry
        (which would block on a write contention with the bridge's
        ``record`` / ``update_lifecycle`` calls).
        """
        return sum(1 for task in self._observers.values() if not task.done())

    def get_observer_task(self, feature_id: str) -> asyncio.Task[None] | None:
        """Return the observer task for ``feature_id`` (or ``None``).

        Exposed so the supervisor can introspect in-flight observer
        state for diagnostics (``forge status --in-flight``) without
        opening a SQLite reader.
        """
        return self._observers.get(feature_id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _default_clock() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _mint_ack_handle_token() -> str:
        """Generate an opaque, URL-safe token for the bridge's :class:`AckHandle`.

        The token is persisted in the SQLite registry; the in-memory
        ack callback that maps the token back to the live handle stays
        in :attr:`_handles`. Twelve random bytes (96 bits) is enough
        entropy to avoid collisions across the lifetime of any single
        ``forge serve`` daemon while keeping the token short enough for
        readable log lines.
        """
        return secrets.token_urlsafe(12)

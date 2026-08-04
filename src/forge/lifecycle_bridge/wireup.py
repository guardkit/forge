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
import json
import logging
import secrets
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Mapping,
    Protocol,
)

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
from forge.lifecycle_bridge.reconnect import ReconnectPolicy
from forge.lifecycle_bridge.run_state_source import (
    RunStateFetcher,
    RunStateSnapshot,
)
from forge.lifecycle_bridge.translation import (
    PipelineEvent,
    StreamEventTranslator,
    VALUES_STREAM_EVENT,
)
from forge.pipeline.build_ack_handle import BuildAckHandle

if TYPE_CHECKING:  # pragma: no cover - import-time only
    from forge.lifecycle_bridge.budget_observer import (
        BudgetBreachObserver,
        BudgetObserverSession,
    )
    from forge.pipeline.supervisor import BuildModeReader


def _build_transient_stream_errors() -> tuple[type[BaseException], ...]:
    """Resolve the set of stream-level errors that trigger reconnect.

    AC-2 / AC-4 (TASK-FRR-PEB-008): the observer reconnects on
    ``httpx.ConnectError``, ``httpx.ReadError``, and malformed JSON
    raised during SSE consumption. ``httpx`` is a runtime dependency
    of the production sidecar transport but is imported defensively so
    unit tests that exercise the wireup with an in-memory async
    generator do not require it.
    """
    errors: list[type[BaseException]] = [json.JSONDecodeError]
    try:
        import httpx  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - production always has httpx
        return tuple(errors)
    errors.extend([httpx.ConnectError, httpx.ReadError])
    return tuple(errors)


#: Tuple of exception types treated as "transient stream error" by the
#: observer's reconnect loop (AC-2 / AC-4). On one of these, the
#: observer logs at WARNING, sleeps the current
#: :class:`ReconnectPolicy` backoff, and re-opens the stream. Any
#: other exception is non-transient and exits the observer.
#:
#: Tests that need to inject a transient error without taking a
#: dependency on ``httpx`` can monkey-patch this tuple — e.g.
#: ``monkeypatch.setattr(wireup, "TRANSIENT_STREAM_ERRORS",
#: (json.JSONDecodeError, MyConnectError))``.
TRANSIENT_STREAM_ERRORS: tuple[type[BaseException], ...] = (
    _build_transient_stream_errors()
)

logger = logging.getLogger(__name__)


__all__ = [
    "DEFAULT_DEADLINE_SECONDS",
    "DEFAULT_SHUTDOWN_TIMEOUT_SECONDS",
    "LifecycleBridgeWireup",
    "MODE_C_WATCHDOG_STAND_DOWN",
    "RunStateFetcher",
    "RunStateSnapshot",
    "StreamSource",
    "TERMINAL_PAYLOAD_TYPES",
    "TRANSIENT_STREAM_ERRORS",
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

#: FWD-002 (WS3-S6) — ``failure_reason`` stamped onto the synthetic
#: ``build-failed`` the observer publishes when a build's identity never
#: resolves within the per-build deadline. A build stuck on unresolved
#: identity (e.g. a dispatch that never wrote its ``async_tasks`` row, the
#: 2026-07-04 FEAT-9E59 shape) would otherwise leave the queued message to
#: redeliver and re-loop forever with the operator's phone frozen on
#: "queued" — a silent stuck build. ``recoverable=True`` because a fresh
#: re-queue can succeed once the dispatch path is healthy.
IDENTITY_UNRESOLVED_FAILURE_REASON: str = "identity-unresolved"

#: F6 (2026-07-26 defect harvest) — ``failure_reason`` stamped onto the
#: synthetic ``build-failed`` the observer publishes when a build's SSE
#: stream closes WITHOUT a terminal envelope and the fetch-on-empty
#: recovery (:meth:`_fetch_and_replay_on_empty`) could not surface a
#: terminal state either. Without this prompt terminalisation the
#: ``builds`` row stays RUNNING until the 300s per-build deadline timer
#: fires — ``forge status`` misreports a finished build as still RUNNING
#: for up to five minutes (the ledger-terminal-lag defect). ``recoverable``
#: is ``True`` because the run is over and only its terminal signal was
#: lost, so a fresh re-queue can succeed. Fired ONLY after recovery yielded
#: nothing AND the stream ended cleanly with no terminal ever observed — a
#: terminal that WAS observed but whose publish failed keeps the inbound
#: un-acked for the JetStream publish-retry contract (TASK-FRR-PEB-011).
STREAM_NO_TERMINAL_FAILURE_REASON: str = "stream-ended-without-terminal"

#: ``async (feature_id, correlation_id) -> build_id | None`` — resolves the
#: durable ``builds.build_id`` for a synthetic terminal so the terminal
#: write hits the right row (un-wedging dispatch). Production wires a
#: SQLite reader; unit tiers omit it and fall back to ``feature_id``.
BuildIdResolver = Callable[[str, str], Awaitable["str | None"]]

#: FWD-002 mode learning (2026-08-04, drive-5 defect harvest) — the log
#: phrase emitted when the identity watchdog stands down for a fix
#: journey. ONE line, at INFO, so a reader of the daemon log can tell
#: "the watchdog chose not to fire" apart from "the watchdog never ran".
MODE_C_WATCHDOG_STAND_DOWN: str = (
    "mode-c build: journey liveness is the conductor's; "
    "FWD-002 identity watchdog stands down"
)


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
#:
#: SIGNATURE (FEAT-FTR, 2026-07-28): ``(feature_id, correlation_id)``.
#: Resolution MUST be exact-match on THIS dispatch's correlation id — a
#: feature-only lookup returns the newest EXISTING row, which during the
#: sidecar's async state-channel write lag is the PREVIOUS build's row.
#: Live receipt: FEAT-UDBE requeue 2026-07-28 10:41 — the observer resolved
#: the prior run, found it finished, and replayed its terminal as a false
#: BuildFailed while the new build ran healthy. A stale hit is worse than a
#: miss: a miss keeps polling (correct); a stale hit fabricates a terminal.
IdentityProvider = Callable[[str, str], Awaitable[tuple[str, str] | None]]

#: Write-back seam invoked after each *successful* publish so the
#: ``builds`` row tracks the lifecycle the bridge just put on the wire
#: (SQLite is source-of-truth — ADR-ARCH-008). Without it the row stays
#: ``QUEUED`` past terminal and the Group C "active in-flight duplicate"
#: check wedges every subsequent dispatch for the feature (observed
#: 2026-07-04 on GB10). ``None`` (the default) preserves the historical
#: publish-only behaviour for callers that have not opted in; production
#: wires :func:`forge.lifecycle_bridge.build_state_recorder.build_build_state_recorder`.
#: Implementations must swallow their own expected failure modes — the
#: wireup additionally guards the call so a recorder bug can never turn
#: a successful publish into a failed one.
BuildStateRecorder = Callable[[PipelineEvent], Awaitable[None]]


def _default_identity_provider() -> IdentityProvider:
    """Return an identity provider that always reports "no identity yet".

    Useful for tests that exercise the registration path without driving
    the SSE observer to completion (the observer exits cleanly when the
    identity never resolves — see
    :meth:`LifecycleBridgeWireup._wait_for_identity`).
    """

    async def _provider(
        feature_id: str, correlation_id: str
    ) -> tuple[str, str] | None:
        return None

    return _provider


def _default_run_state_fetcher() -> RunStateFetcher:
    """Return a no-op :class:`RunStateFetcher` used when one is not injected.

    Pre-existing wireup callers (unit tests, in-process integration tests)
    that did not opt into the TASK-REV-PEBR-005 fetch-on-empty fallback
    keep their existing behaviour — the observer's "stream closed without
    a terminal envelope" branch fires unchanged because the fetcher
    always reports "no terminal state available". Production composition
    in :func:`forge.cli._serve_production.bind_production_serve` injects
    :func:`forge.lifecycle_bridge.run_state_source.langgraph_run_state_fetcher`
    so the fallback is live on the daemon.
    """

    async def _fetcher(
        *,
        feature_id: str,
        thread_id: str | None,
        run_id: str | None,
    ) -> RunStateSnapshot | None:
        return None

    return _fetcher


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
        run_state_fetcher: A :class:`RunStateFetcher` callable used as
            the fetch-on-empty fallback (TASK-REV-PEBR-005). When the
            stream closes without yielding a terminal envelope (the
            Signature C race: the placeholder run finished in ~16 ms
            before the observer could open ``runs.join_stream``), the
            observer asks this fetcher whether the run has terminated
            and — if so — replays the run's final state values through
            the translator so the canonical envelope shape is preserved.
            Defaults to a no-op fetcher that always reports "no terminal
            state available", which preserves pre-FOLLOWUP-C-RACE
            behaviour for callers that have not opted in. Production
            wires :func:`forge.lifecycle_bridge.run_state_source.langgraph_run_state_fetcher`.
        build_state_recorder: A :data:`BuildStateRecorder` invoked with
            each payload after its publish succeeds, so the ``builds``
            row tracks the lifecycle just put on the wire. Defaults to
            ``None`` (no write-back — pre-existing behaviour). Recorder
            exceptions are logged at WARNING and never affect the
            publish result. Production wires
            :func:`forge.lifecycle_bridge.build_state_recorder.build_build_state_recorder`.
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
        build_mode_reader: Optional
            :class:`~forge.pipeline.supervisor.BuildModeReader` consulted
            ONLY on the identity-unresolved branch (FWD-002 mode
            learning). Production wires
            :class:`~forge.lifecycle.persistence.SqliteBuildModeReader`.
            ``None`` — the default — arms the identity watchdog for every
            build exactly as before this lane.
    """

    def __init__(
        self,
        *,
        bridge: LifecycleBridge,
        translator: StreamEventTranslator,
        publisher: PipelinePublisher,
        stream_source: StreamSource,
        identity_provider: IdentityProvider | None = None,
        run_state_fetcher: RunStateFetcher | None = None,
        build_state_recorder: BuildStateRecorder | None = None,
        deadline_seconds: int = DEFAULT_DEADLINE_SECONDS,
        identity_resolution_attempts: int = 3,
        identity_poll_interval_seconds: float = 1.0,
        shutdown_timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
        clock: Callable[[], datetime] | None = None,
        build_id_resolver: "BuildIdResolver | None" = None,
        budget_observer: "BudgetBreachObserver | None" = None,
        build_mode_reader: "BuildModeReader | None" = None,
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
            raise ValueError("LifecycleBridgeWireup: stream_source is required")
        if deadline_seconds <= 0:
            raise ValueError("LifecycleBridgeWireup: deadline_seconds must be positive")

        self._bridge = bridge
        self._translator = translator
        self._publisher = publisher
        self._stream_source = stream_source
        self._identity_provider = (
            identity_provider
            if identity_provider is not None
            else _default_identity_provider()
        )
        self._run_state_fetcher = (
            run_state_fetcher
            if run_state_fetcher is not None
            else _default_run_state_fetcher()
        )
        self._build_state_recorder = build_state_recorder
        self._deadline_seconds = deadline_seconds
        self._identity_resolution_attempts = max(1, int(identity_resolution_attempts))
        self._identity_poll_interval_seconds = max(
            0.0, float(identity_poll_interval_seconds)
        )
        self._shutdown_timeout_seconds = max(0.0, float(shutdown_timeout_seconds))
        self._clock = clock if clock is not None else self._default_clock
        # FWD-002 — resolves the durable build_id for the synthetic
        # identity-unresolved build-failed. ``None`` (unit tiers) falls back
        # to feature_id; production wires a SQLite reader.
        self._build_id_resolver = build_id_resolver
        # FWD-002 mode learning — the read-side that lets the identity
        # watchdog tell a fix journey from a routine build. ``None`` (unit
        # tiers, and any caller that has not opted in) keeps the watchdog
        # armed for EVERY build: the pre-lane behaviour, byte for byte.
        self._build_mode_reader = build_mode_reader
        # FEAT-UBS-002 stage 2 (DETECT) — mid-run budget-breach detector.
        # ``None`` (the default, and every attended / caps-off deployment) makes
        # the observer's budget hook a strict no-op: zero extra DB / publish
        # calls, byte-identical to the pre-lane observer. When wired, the hook
        # evaluates the budget after each published ``stage-complete`` and, on
        # the first breach, RECORDS + ESCALATES without ever pausing / cancelling
        # / rewriting ``builds.status`` (the honesty law of this lane).
        self._budget_observer = budget_observer
        # Per-feature budget-detection state (one session per observer task).
        # Created at observer start when a detector is wired, dropped in the
        # observer's ``finally`` — so the review-cycle count resets on bridge
        # restart (documented in budget_observer).
        self._budget_sessions: dict[str, "BudgetObserverSession"] = {}
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
        deadline_at = self._clock() + timedelta(seconds=self._deadline_seconds)
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
        # FEAT-UBS-002 stage 2 — open a fresh per-observer budget-detection
        # session when a detector is wired. In-memory only: a bridge restart
        # starts a fresh session (the review-cycle count resets by design).
        if self._budget_observer is not None:
            self._budget_sessions[feature_id] = self._budget_observer.new_session()
        try:
            identity = await self._wait_for_identity(
                feature_id, context.correlation_id
            )
            if identity is None:
                # FWD-002 (WS3-S6): identity did not resolve within the
                # initial poll budget. Do NOT fall through to stream with
                # ``(None, None)`` ids and exit silently — that leaves the
                # queued message un-acked, JetStream redelivers, and the
                # re-registered observer repeats the same non-resolution: a
                # silent infinite loop with the operator's phone frozen on
                # "queued" (the 2026-07-04 FEAT-9E59 shape). Keep polling to
                # the per-build deadline (a slow dispatch may still surface
                # the run); if identity STILL never resolves, publish a
                # synthetic build-failed and ack — never spin silently.
                #
                # FWD-002 MODE LEARNING (2026-08-04 drive-5 harvest): that
                # protection is written for a ROUTINE build, whose only
                # liveness signal IS the identity the langgraph sidecar
                # publishes — no identity, no evidence anyone is driving,
                # so silence means stuck. A mode-c build is never silent:
                # the conductor's turn loop owns it (journey wallclock cap,
                # per-stage timeouts, the taken-and-terminal vocabulary,
                # the leg-honesty terminal), and the fix journey never
                # touches the sidecar path at all — so identity NEVER
                # resolves for it and the deadline below terminalises a
                # perfectly healthy journey (drive 5: the first production
                # work leg killed ~90s into an 1800s budget, row stamped
                # FAILED|identity-unresolved). Consult the row's mode and
                # stand the watchdog down for a fix journey: no deadline
                # arm, no synthetic terminal, no ack, no detach — the
                # conductor acks at ITS terminal.
                build_id = await self._resolve_watchdog_build_id(context)
                if self._is_mode_c_build(build_id):
                    logger.info(
                        "wireup._observer_loop: feature_id=%s build_id=%s — %s "
                        "(no identity deadline armed, no synthetic "
                        "build-failed; the row is left to the conductor)",
                        feature_id,
                        build_id,
                        MODE_C_WATCHDOG_STAND_DOWN,
                    )
                    return
                identity = await self._await_identity_until_deadline(context)
                if identity is None:
                    await self._publish_identity_unresolved_failure(
                        context, handle, build_id=build_id
                    )
                    return
            thread_id, run_id = identity

            # AC-2 / AC-4 (TASK-FRR-PEB-008): wrap the SSE iteration
            # in a reconnect loop driven by :class:`ReconnectPolicy`.
            # On any transient stream error the loop logs WARNING,
            # sleeps the current backoff, and re-opens a fresh stream.
            # On clean stream end (no transient error) the loop exits
            # and the consumer falls back to JetStream redelivery.
            terminal_seen, terminal_publish_failed = (
                await self._consume_with_reconnect(
                    context=context,
                    handle=handle,
                    thread_id=thread_id,
                    run_id=run_id,
                )
            )

            if not terminal_seen:
                # TASK-REV-PEBR-005 Signature C fetch-on-empty fallback:
                # the SSE iterator may have closed empty because the run
                # finished BEFORE the bridge could open
                # ``runs.join_stream`` (placeholder bodies finish in
                # ~16 ms; ``join_stream`` against a finished run is a
                # live subscription that returns empty per the
                # langgraph-sdk 0.3.13 docstring). Ask the run-state
                # fetcher whether the run has terminated; if so, replay
                # its final state values through the translator so the
                # canonical envelope shape lands without ad-hoc payload
                # synthesis. The fetcher returns ``None`` for runs that
                # are still running (or on transport error / SDK shape
                # drift) — in that case fall through to the original
                # "leave un-acked, JetStream will redeliver" branch.
                terminal_seen = await self._fetch_and_replay_on_empty(
                    context=context,
                    handle=handle,
                    thread_id=thread_id,
                    run_id=run_id,
                )

            if not terminal_seen:
                if terminal_publish_failed:
                    # A terminal envelope WAS observed but its publish
                    # failed (transient broker error). Leave the inbound
                    # un-acked so JetStream redelivery retries the *publish*
                    # of the real terminal — do NOT synthesise a build-failed
                    # here: the build already reached a real terminal
                    # (possibly build-complete), so firing a failed envelope
                    # and acking would both mislabel it and eat the
                    # redelivery the publish-retry contract
                    # (TASK-FRR-PEB-011 AC-2/AC-3) depends on.
                    logger.warning(
                        "wireup._observer_loop: terminal envelope publish "
                        "failed for feature_id=%s; leaving inbound queued "
                        "message un-acked (JetStream redelivery retries the "
                        "publish, deadline timer is the backstop)",
                        feature_id,
                    )
                else:
                    # F6 (ledger-terminal-lag defect harvest 2026-07-26):
                    # the stream ended cleanly with no terminal envelope and
                    # fetch-on-empty could not recover one. Publish a
                    # synthetic build-failed and ack now so builds.status
                    # leaves RUNNING promptly instead of waiting for the 300s
                    # per-build deadline timer. _on_terminal's detach cancels
                    # that timer (bridge.detach), so it cannot fire a second
                    # synthetic build-failed later.
                    await self._publish_no_terminal_failure(context, handle)
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
            # Drop the per-observer budget session (in-memory state does not
            # outlive the observer — FEAT-UBS-002 stage 2).
            self._budget_sessions.pop(feature_id, None)

    # ------------------------------------------------------------------
    # SSE consumption with reconnect (TASK-FRR-PEB-008 AC-2 / AC-4)
    # ------------------------------------------------------------------

    async def _consume_with_reconnect(
        self,
        *,
        context: BuildContext,
        handle: BuildAckHandle,
        thread_id: str | None,
        run_id: str | None,
    ) -> tuple[bool, bool]:
        """Drive the SSE stream with :class:`ReconnectPolicy` retry semantics.

        Returns ``(terminal_seen, terminal_publish_failed)`` so the
        observer can tell a clean no-terminal exit (F6: synthesise a
        build-failed) apart from a terminal-that-failed-to-publish (leave
        the inbound un-acked for the JetStream publish-retry contract):

        * ``(True, False)`` — a terminal envelope was published + acked.
        * ``(False, False)`` — the stream ended cleanly with no terminal.
        * ``(False, True)`` — a terminal envelope was observed but its
          publish failed (TASK-FRR-PEB-011 AC-2/AC-3).

        AC-2 / AC-4 (TASK-FRR-PEB-008):

        * On each iteration, open a fresh ``StreamSource`` and drive
          it via :meth:`_drive_stream_session`.
        * If the session returns a terminal envelope, return
          ``(True, False)`` — the observer is done.
        * If the session ends cleanly (StopAsyncIteration with no
          terminal), return ``(False, False)`` — let the supervisor
          surface the orphaned-stream warning and rely on JetStream
          redelivery.
        * If the session raises one of :data:`TRANSIENT_STREAM_ERRORS`
          (``httpx.ConnectError``, ``httpx.ReadError``,
          :class:`json.JSONDecodeError`), log at WARNING, sleep the
          current backoff, and reconnect. **No fixed maximum retry
          count** — the loop terminates only on
          :class:`asyncio.CancelledError` (operator cancel / shutdown)
          or on the per-build deadline timer (AC-3) firing in the
          background.
        * Any other exception is non-transient: log loudly and exit
          the observer (the bridge's deadline timer remains the
          backstop).

        ``policy.reset()`` fires on a healthy event yield from the
        session (AC-1: backoff resets on successful reconnection).
        """
        feature_id = context.feature_id
        policy = ReconnectPolicy()
        attempt = 0
        while True:
            attempt += 1
            try:
                stream_iter = self._stream_source(
                    feature_id=feature_id,
                    thread_id=thread_id,
                    run_id=run_id,
                )
            except TRANSIENT_STREAM_ERRORS as exc:
                # AC-4: malformed source-construction (rare, but
                # possible if the langgraph-runner returns a
                # malformed connection-init response). Same WARNING +
                # backoff path as in-stream errors; the failed open
                # counts as an attempt.
                backoff = policy.next_backoff()
                logger.warning(
                    "wireup._consume_with_reconnect: transient SSE error "
                    "opening stream (%s: %s) for feature_id=%s "
                    "attempt=%d; reconnecting in %.2fs",
                    type(exc).__name__,
                    exc,
                    feature_id,
                    attempt,
                    backoff,
                )
                await asyncio.sleep(backoff)
                continue

            try:
                terminal_seen, ended_cleanly = await self._drive_stream_session(
                    stream_iter=stream_iter,
                    context=context,
                    handle=handle,
                    policy=policy,
                )
            except TRANSIENT_STREAM_ERRORS as exc:
                # AC-2 / AC-4: transient mid-stream error. Log and
                # reconnect after the backoff sleep. The reconnect
                # counts as an attempt (policy.next_backoff() advances
                # the schedule).
                backoff = policy.next_backoff()
                logger.warning(
                    "wireup._consume_with_reconnect: transient SSE error "
                    "(%s: %s) for feature_id=%s attempt=%d; "
                    "reconnecting in %.2fs (Last-Event-ID persistence "
                    "is owned by the registry)",
                    type(exc).__name__,
                    exc,
                    feature_id,
                    attempt,
                    backoff,
                )
                await asyncio.sleep(backoff)
                continue

            if terminal_seen:
                return (True, False)
            if ended_cleanly:
                return (False, False)
            # ``_drive_stream_session`` returned ``(False, False)`` — a
            # terminal envelope was observed but its publish failed. Signal
            # that to the observer so it leaves the inbound un-acked for the
            # publish-retry redelivery instead of synthesising a build-failed.
            return (False, True)

    # ------------------------------------------------------------------
    # Fetch-on-empty fallback (TASK-REV-PEBR-005 Signature C)
    # ------------------------------------------------------------------

    async def _fetch_and_replay_on_empty(
        self,
        *,
        context: BuildContext,
        handle: BuildAckHandle,
        thread_id: str | None,
        run_id: str | None,
    ) -> bool:
        """Replay a finished run's terminal state through the translator.

        Called from :meth:`_observer_loop` when
        :meth:`_consume_with_reconnect` returns
        ``terminal_seen=False, ended_cleanly=True`` — the canonical
        Signature C symptom (``runs.join_stream`` against a finished
        run is a live subscription that returns empty per the
        ``langgraph_sdk`` 0.3.13 docstring; placeholder bodies finish
        in ~16 ms before the bridge can subscribe).

        Workflow:

        1. If identity has not resolved (``thread_id`` or ``run_id`` is
           ``None``), there is nothing to fetch; return ``False`` so the
           observer falls through to the un-acked branch.
        2. Ask the injected :class:`RunStateFetcher` for the run's
           terminal status + thread state values. The fetcher returns
           ``None`` for non-terminal runs (still running / pending) and
           on transport error / SDK shape drift. ``None`` ⇒ fall
           through.
        3. If the fetcher returns a :class:`RunStateSnapshot`, wrap the
           values in a synthetic ``StreamPart(event="values", data=...)``
           and feed it to the existing translator. The translator's
           transition detection emits whatever payload(s) the final
           state implies (typically ``BuildStartedPayload`` followed by
           a terminal ``BuildCompletePayload`` or ``BuildFailedPayload``).
        4. Publish each emitted payload via :meth:`_publish_event` and,
           on terminal arrival, ack + detach via :meth:`_on_terminal`
           — exactly the same path the live SSE branch uses.

        Returns:
            ``True`` if a terminal envelope was published and the
            inbound was acked; ``False`` otherwise (no identity, no
            terminal state available, or terminal publish failed —
            same retry semantics as the live SSE path).
        """
        feature_id = context.feature_id
        if thread_id is None or run_id is None:
            return False

        try:
            snapshot = await self._run_state_fetcher(
                feature_id=feature_id,
                thread_id=thread_id,
                run_id=run_id,
            )
        except Exception as exc:  # noqa: BLE001 — fetcher contract: never raise
            # Defensive: a fetcher implementation that breaks contract
            # MUST NOT crash the observer. Log loudly and treat as no
            # snapshot — JetStream redelivery is the recovery path.
            logger.warning(
                "wireup._fetch_and_replay_on_empty: run_state_fetcher "
                "raised (%s) for feature_id=%s thread_id=%s run_id=%s; "
                "treating as no-snapshot",
                exc,
                feature_id,
                thread_id,
                run_id,
            )
            return False

        if snapshot is None:
            return False

        return await self._replay_run_state_snapshot(
            snapshot=snapshot,
            context=context,
            handle=handle,
        )

    async def _replay_run_state_snapshot(
        self,
        *,
        snapshot: RunStateSnapshot,
        context: BuildContext,
        handle: BuildAckHandle,
    ) -> bool:
        """Replay a terminal state snapshot through the translator.

        Two ``translate(...)`` calls reconstitute the canonical
        BuildStarted → terminal sequence the live SSE path would have
        emitted — matching the 2-envelope guarantee in the
        FOLLOWUP-C-RACE out-of-scope guard rail (parent task's TL;DR:
        "expect 2 envelopes from the placeholder bodies"):

        1. **Synthetic ``running_wave`` projection.** Reuses the
           feature's own snapshot fields (``feature_id``, ``build_id``,
           ``wave_total``) but forces ``lifecycle="running_wave"`` and
           zeroes the per-stage counters. With ``prev=None`` this
           triggers the translator's
           :meth:`StreamEventTranslator._build_started` rule (precedence
           4) and emits :class:`BuildStartedPayload`.
        2. **Actual terminal state.** The real values dict the fetcher
           returned. With ``prev.lifecycle="running_wave"`` this
           triggers the translator's terminal rule (precedence 1) and
           emits :class:`BuildCompletePayload` /
           :class:`BuildFailedPayload` / :class:`BuildCancelledPayload`
           per the snapshot's ``lifecycle`` field.

        On step 2's terminal arrival, the canonical ack+detach path
        (:meth:`_on_terminal`) runs.

        If the snapshot's ``async_tasks[feature_id]`` mapping cannot be
        located (the run has no usable AutobuildState — possible for
        non-autobuild-shaped runs), the replay falls back to a single
        translator call against the raw values. Result: still race-free
        (a terminal envelope lands if the translator can produce one),
        just without the synthetic BuildStarted preface.

        Returns ``True`` iff a terminal envelope was published and
        ``_on_terminal`` (ack + detach) ran cleanly.
        """
        feature_id = context.feature_id
        running_values = self._project_running_wave_state(snapshot.values, feature_id)

        # Step 1 — synthetic running_wave projection (best-effort
        # BuildStarted preface). If we cannot project, skip — the
        # terminal-only call still satisfies AC-11's ack_floor advance,
        # but the operator does NOT see a build-started envelope on
        # the wire. The warning log makes this visible.
        if running_values is not None:
            await self._translate_and_publish(
                values=running_values,
                context=context,
                feature_id=feature_id,
                stage="running_wave",
                snapshot_status=snapshot.status,
            )
        else:
            logger.warning(
                "wireup._replay_run_state_snapshot: cannot synthesise "
                "running_wave projection for feature_id=%s status=%s "
                "(async_tasks[%s] missing or non-Mapping); replay will "
                "attempt terminal-only emit (no BuildStarted envelope "
                "on the wire — AC-11 build-started gate may not be met "
                "for this run)",
                feature_id,
                snapshot.status,
                feature_id,
            )

        # Step 2 — actual terminal state.
        terminal_event = await self._translate_and_publish(
            values=snapshot.values,
            context=context,
            feature_id=feature_id,
            stage="terminal",
            snapshot_status=snapshot.status,
        )

        if terminal_event is None:
            logger.info(
                "wireup._replay_run_state_snapshot: terminal state did "
                "not produce an envelope for feature_id=%s status=%s — "
                "translator returned None (likely a partial "
                "AutobuildState or unknown lifecycle); leaving inbound "
                "un-acked",
                feature_id,
                snapshot.status,
            )
            return False

        if not isinstance(terminal_event, TERMINAL_PAYLOAD_TYPES):
            logger.info(
                "wireup._replay_run_state_snapshot: terminal-stage emit "
                "is non-terminal (%s) for feature_id=%s status=%s — "
                "translator did not detect a terminal transition from "
                "the fetched state; leaving inbound un-acked",
                type(terminal_event).__name__,
                feature_id,
                snapshot.status,
            )
            return False

        await self._on_terminal(handle, feature_id, context.correlation_id)
        logger.info(
            "wireup._replay_run_state_snapshot: synthesised terminal "
            "envelope (%s) from run_status=%s for feature_id=%s "
            "(TASK-REV-PEBR-005 fetch-on-empty fallback)",
            type(terminal_event).__name__,
            snapshot.status,
            feature_id,
        )
        return True

    async def _translate_and_publish(
        self,
        *,
        values: Mapping[str, Any],
        context: BuildContext,
        feature_id: str,
        stage: str,
        snapshot_status: str,
    ) -> PipelineEvent | None:
        """Translate one synthetic ``StreamPart`` and publish if non-``None``.

        Helper for :meth:`_replay_run_state_snapshot`. Wraps ``values``
        in a minimal dict matching the ``StreamPart`` TypedDict shape
        (``event``, ``data``, ``id``) and runs the canonical translator
        path. Publish failures and translator exceptions are logged at
        WARNING and downgraded to ``None`` so a bad partial state does
        not break the second-stage replay attempt.

        ``stage`` is "running_wave" or "terminal" — used in the WARNING
        log line so an operator triaging a fetch-on-empty event can
        distinguish which projection failed.
        """
        # Construct an attribute-access object the translator can drive
        # via ``getattr(part, "event")`` and ``part.data`` — the same
        # surface ``langgraph_sdk.schema.StreamPart`` (a NamedTuple)
        # exposes. ``SimpleNamespace`` keeps wireup's "no runtime
        # langgraph_sdk dependency" discipline (mirrors the lazy-import
        # pattern in :mod:`forge.lifecycle_bridge.stream_source`).
        synthetic_part = SimpleNamespace(
            event=VALUES_STREAM_EVENT,
            data=dict(values),
            id=None,
        )
        try:
            event = self._translator.translate(synthetic_part, context)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "wireup._translate_and_publish: translator raised (%s) "
                "for feature_id=%s stage=%s status=%s; skipping this "
                "replay step",
                exc,
                feature_id,
                stage,
                snapshot_status,
            )
            return None
        if event is None:
            return None
        published_ok = await self._publish_event(event, feature_id)
        if not published_ok:
            logger.warning(
                "wireup._translate_and_publish: synthesised event "
                "publish failed for feature_id=%s stage=%s "
                "payload_type=%s; replay continues but inbound will "
                "remain un-acked (JetStream redelivery + "
                "recover_in_flight retry path)",
                feature_id,
                stage,
                type(event).__name__,
            )
            return None
        return event

    @staticmethod
    def _project_running_wave_state(
        values: Mapping[str, Any], feature_id: str
    ) -> Mapping[str, Any] | None:
        """Project a ``running_wave`` snapshot from a terminal state values dict.

        The translator detects "build started" via a transition to
        ``lifecycle="running_wave"`` with ``prev=None`` (or
        ``prev.lifecycle in {"starting", "planning_waves"}``). To
        reconstitute a BuildStarted preface during fetch-on-empty
        replay, we project the original ``async_tasks[feature_id]``
        dict but override ``lifecycle="running_wave"`` and zero the
        per-stage counters so the projection looks like a fresh build
        start.

        Returns ``None`` when ``async_tasks`` is missing or when
        ``async_tasks[feature_id]`` is not a ``Mapping`` — the replay
        path then logs a WARNING and continues with the terminal call
        only.
        """
        if not isinstance(values, Mapping):
            return None
        async_tasks = values.get("async_tasks")
        if not isinstance(async_tasks, Mapping):
            return None
        snapshot = async_tasks.get(feature_id)
        if not isinstance(snapshot, Mapping):
            return None

        running_snapshot: dict[str, Any] = dict(snapshot)
        running_snapshot["lifecycle"] = "running_wave"
        # Zero per-stage counters so the projection looks like a fresh
        # start. ``wave_total`` is preserved — BuildStartedPayload
        # surfaces it to downstream consumers.
        running_snapshot["wave_index"] = 0
        running_snapshot["task_index"] = 0
        running_snapshot["tasks_completed"] = 0
        running_snapshot["tasks_failed"] = 0
        running_snapshot["waiting_for"] = None
        running_snapshot["last_coach_score"] = None

        # Build a values dict that preserves siblings of async_tasks
        # (messages / todos / files) so the translator's _extract_state
        # path sees the same outer shape it does on the live SSE
        # channel.
        projected: dict[str, Any] = {
            k: v for k, v in values.items() if k != "async_tasks"
        }
        projected_async_tasks: dict[str, Any] = {
            k: v for k, v in async_tasks.items() if k != feature_id
        }
        projected_async_tasks[feature_id] = running_snapshot
        projected["async_tasks"] = projected_async_tasks
        return projected

    async def _drive_stream_session(
        self,
        *,
        stream_iter: AsyncIterator[Any],
        context: BuildContext,
        handle: BuildAckHandle,
        policy: ReconnectPolicy,
    ) -> tuple[bool, bool]:
        """Drive one SSE session to terminal arrival or clean exhaustion.

        Returns ``(terminal_seen, ended_cleanly)``:

        * ``(True, True)`` — terminal envelope observed and published;
          ack/detach completed.
        * ``(False, True)`` — stream iterator exhausted with no
          terminal; the caller decides whether to redeliver.
        * ``(False, False)`` — terminal envelope observed but publish
          failed; the caller treats it as session-ended (no retry,
          per the existing TASK-FRR-PEB-011 AC-2/AC-3 contract).

        Raises any exception in :data:`TRANSIENT_STREAM_ERRORS` so
        the caller can run the reconnect path. Translator-level
        exceptions (per-part malformed data) are caught locally and
        logged at WARNING — they do **not** trigger reconnect because
        they are envelope-shape bugs, not transport failures.
        """
        feature_id = context.feature_id
        correlation_id = context.correlation_id
        terminal_seen = False
        first_event_observed = False
        async for stream_part in stream_iter:
            # AC-1: a successful event yield resets the backoff so a
            # later transient failure starts again from
            # RECONNECT_INITIAL_BACKOFF.
            if not first_event_observed:
                policy.reset()
                first_event_observed = True
            try:
                event = self._translator.translate(stream_part, context)
            except Exception as exc:  # noqa: BLE001
                # AC-4: a malformed part is logged at WARNING and
                # skipped (the observer continues iterating; per-part
                # translator errors are not transport failures so they
                # do not trigger the reconnect-with-backoff path).
                logger.warning(
                    "wireup._drive_stream_session: translator raised (%s) "
                    "for feature_id=%s; skipping stream part",
                    exc,
                    feature_id,
                )
                continue
            if event is None:
                continue
            published_ok = await self._publish_event(event, feature_id)
            # FEAT-UBS-002 stage 2 (DETECT) — evaluate the budget AFTER the
            # non-terminal publish. Only StageCompletePayload advances a build's
            # budget consumption; the hook records + escalates a breach but
            # never changes the stream's control flow (it does not pause /
            # cancel / short-circuit the loop). Strict no-op when no detector is
            # wired (caps-off byte-equivalence).
            if isinstance(event, StageCompletePayload):
                await self._observe_budget(event, feature_id)
            if isinstance(event, TERMINAL_PAYLOAD_TYPES):
                if published_ok:
                    terminal_seen = True
                    await self._on_terminal(handle, feature_id, correlation_id)
                    return (True, True)
                # TASK-FRR-PEB-011 AC-2/AC-3: terminal envelope
                # publish failed (transient broker error / network
                # blip). Do NOT mark the build "terminal-published",
                # do NOT ack the inbound build-queued message — the
                # JetStream consumer will redeliver and the bridge's
                # T9 recovery cycle will retry the publish on the
                # next observation. ADR-ARCH-008: SQLite is
                # source-of-truth; transient broker failures must
                # NOT corrupt build state.
                logger.warning(
                    "wireup._drive_stream_session: terminal envelope "
                    "publish failed for feature_id=%s correlation_id=%s "
                    "payload_type=%s; leaving SQLite registry row "
                    "intact and inbound build-queued un-acked "
                    "(JetStream redelivery + recover_in_flight will "
                    "retry — no spurious ack)",
                    feature_id,
                    correlation_id,
                    type(event).__name__,
                )
                return (False, False)
        return (terminal_seen, True)

    # ------------------------------------------------------------------
    # Budget-breach detection (FEAT-UBS-002 stage 2)
    # ------------------------------------------------------------------

    async def _observe_budget(
        self, event: StageCompletePayload, feature_id: str
    ) -> None:
        """Feed a published ``stage-complete`` to the budget detector.

        Fully exception-guarded: a budget bug must NEVER break the lifecycle
        stream — the bridge is more load-bearing than enforcement. On any
        failure the observer logs loudly and continues; the run reaches its own
        bounded terminal and the F6 contracts stand. A strict no-op when no
        detector is wired or no session exists (caps-off byte-equivalence).
        """
        observer = self._budget_observer
        if observer is None:
            return
        session = self._budget_sessions.get(feature_id)
        if session is None:
            return
        try:
            await observer.observe_stage_complete(
                session,
                build_id=event.build_id,
                feature_id=feature_id,
                coach_score=event.coach_score,
            )
        except Exception as exc:  # noqa: BLE001 — enforcement must not break the stream
            logger.error(
                "wireup._observe_budget: budget detector raised (%s) for "
                "feature_id=%s build_id=%s; observer continues (the lifecycle "
                "stream is more load-bearing than budget enforcement) (UBS-002)",
                exc,
                feature_id,
                getattr(event, "build_id", None),
            )

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
        # FEAT-UBS-002 / Rich's 2026-07-30 ruling — a budget cap-KILLED build
        # must ARM the TASK-GATE-D659 pre-dispatch breach gate. The runner
        # marks the failed snapshot; the translator threads the marker onto
        # the typed payload; here — the one seam EVERY translated event
        # passes (the live SSE session AND the fetch-on-empty replay) — the
        # durable ``builds.budget_breach`` marker is recorded BEFORE the
        # publish, so even a failed publish leaves the gate armed (the SQL
        # first-write-wins makes the JetStream-redelivery re-record a no-op).
        # Observer-DETECTED breach semantics are untouched.
        if isinstance(event, BuildFailedPayload) and getattr(
            event, "budget_cap_killed", False
        ):
            self._record_cap_kill_breach(event, feature_id)
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
        # Write-back AFTER a successful publish only. Ordering is
        # load-bearing: recording a terminal state before its envelope
        # is on the wire would flip the row terminal, and the consumer's
        # "duplicate already-terminal → ack + skip" path would then eat
        # the JetStream redelivery that the publish-failure contract
        # (TASK-FRR-PEB-011 AC-2/AC-3) relies on to retry the publish.
        await self._record_build_state(event, feature_id)
        return True

    def _record_cap_kill_breach(
        self, event: BuildFailedPayload, feature_id: str
    ) -> None:
        """Record ``builds.budget_breach`` for a runner cap-KILLED build.

        Fully exception-guarded, mirroring :meth:`_observe_budget`: the
        lifecycle stream is more load-bearing than budget enforcement, so a
        recorder fault logs loudly and the publish proceeds. When no budget
        observer is wired (some unit tiers; a caps-off boot) there is no
        persistence seam to record through — log the miss loudly rather than
        failing silent, since a cap-kill only ever originates from a
        budget-capped (unattended-profile) launch.
        """
        observer = self._budget_observer
        if observer is None:
            logger.error(
                "wireup._record_cap_kill_breach: build_id=%s feature_id=%s "
                "arrived cap-KILLED but NO budget observer is wired — the "
                "breach marker CANNOT be recorded and the D659 pre-dispatch "
                "gate will NOT arm for the re-queue (UBS-002)",
                event.build_id,
                feature_id,
            )
            return
        try:
            observer.record_cap_kill(
                build_id=event.build_id,
                feature_id=feature_id,
                detail=event.failure_reason,
            )
        except Exception as exc:  # noqa: BLE001 — enforcement must not break the stream
            logger.error(
                "wireup._record_cap_kill_breach: recorder raised (%s) for "
                "build_id=%s feature_id=%s; observer continues (the lifecycle "
                "stream is more load-bearing than budget enforcement) "
                "(UBS-002)",
                exc,
                event.build_id,
                feature_id,
            )

    async def _record_build_state(self, event: PipelineEvent, feature_id: str) -> None:
        """Invoke the :data:`BuildStateRecorder`, downgrading any failure.

        A recorder bug (or an optimistic-concurrency clash with the CLI
        cancel path) must never turn a successful publish into a failed
        one — the envelope is already on the wire, so the observer's
        ack/detach decision has to key off the publish alone. Failures
        land at WARNING with enough context to reconcile the row by
        hand.
        """
        if self._build_state_recorder is None:
            return
        try:
            await self._build_state_recorder(event)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "wireup._record_build_state: recorder raised (%s) for "
                "feature_id=%s payload_type=%s build_id=%s; builds row "
                "may lag the published lifecycle",
                exc,
                feature_id,
                type(event).__name__,
                getattr(event, "build_id", None),
            )

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
        self, feature_id: str, correlation_id: str
    ) -> tuple[str, str] | None:
        """Poll :data:`IdentityProvider` until it returns a non-``None`` pair.

        Returns ``None`` when ``identity_resolution_attempts`` polls
        have all returned ``None`` — the observer treats that as "no
        run yet" and exits cleanly.
        """
        for attempt in range(self._identity_resolution_attempts):
            try:
                identity = await self._identity_provider(
                    feature_id, correlation_id
                )
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
            "feature_id=%s after %d attempts; extending to the per-build "
            "deadline (FWD-002)",
            feature_id,
            self._identity_resolution_attempts,
        )
        return None

    async def _await_identity_until_deadline(
        self, context: BuildContext
    ) -> tuple[str, str] | None:
        """Keep polling identity until the per-build deadline (FWD-002).

        The initial :meth:`_wait_for_identity` budget (a few fast polls) is
        deliberately short for the common case where the run's
        ``async_tasks`` row is written moments after registration. When it
        exhausts without resolving, this method extends the wait to the
        per-build deadline (``self._deadline_seconds``) so a merely-slow
        dispatch still gets picked up rather than being declared failed.

        The budget is measured on the event loop's monotonic clock — NOT
        the injected wall-clock — so a deterministic ``FixedClock`` (used
        for ``deadline_at`` display/registry hygiene) cannot wedge this
        loop, and tests pin a small ``deadline_seconds`` for fast runs.

        Returns the resolved ``(thread_id, run_id)`` pair, or ``None`` when
        the deadline elapses (or shutdown begins) with identity still
        unresolved.
        """
        feature_id = context.feature_id
        loop = asyncio.get_event_loop()
        budget_deadline = loop.time() + float(self._deadline_seconds)
        while not self._shutting_down and loop.time() < budget_deadline:
            # Sleep first: the initial fast-poll budget just ran, so the row
            # is very unlikely to appear within the same tick.
            remaining = budget_deadline - loop.time()
            await asyncio.sleep(
                min(self._identity_poll_interval_seconds, max(0.0, remaining))
            )
            if self._shutting_down:
                break
            try:
                identity = await self._identity_provider(
                    feature_id, context.correlation_id
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "wireup._await_identity_until_deadline: identity_provider "
                    "raised (%s) for feature_id=%s; treating as None",
                    exc,
                    feature_id,
                )
                identity = None
            if identity is not None:
                logger.info(
                    "wireup._await_identity_until_deadline: identity resolved "
                    "for feature_id=%s during the deadline wait; resuming "
                    "the SSE observer",
                    feature_id,
                )
                return identity
        return None

    async def _resolve_watchdog_build_id(self, context: BuildContext) -> str | None:
        """Resolve the ``builds.build_id`` the watchdog decision hangs on.

        Called ONCE on the identity-unresolved branch and threaded into
        both consumers — the mode read below and (on the routine path) the
        synthetic terminal's payload — so a routine build pays exactly one
        resolver call, as it did before this lane.

        Returns ``None`` when no mode reader is wired: with nothing to ask
        about the mode there is no decision to make, so the resolve is
        skipped entirely and :meth:`_publish_identity_unresolved_failure`
        resolves its own build_id exactly as it always has.
        """
        if self._build_mode_reader is None:
            return None
        return await self._resolve_build_id(context.feature_id, context.correlation_id)

    def _is_mode_c_build(self, build_id: str | None) -> bool:
        """Return ``True`` iff ``build_id``'s row is a fix journey (mode-c).

        The §4 degrade posture, inverted for this seam: the established
        consumer-side read (``forge.cli._conductor_outcome.is_mode_c_build``)
        answers ``False`` on an unreadable row so a routine build is never
        stranded on a database hiccup, and the same answer is the safe one
        here for the opposite reason — ``False`` KEEPS the FWD-002 watchdog
        armed. An unreadable row must never silently disarm a routine
        build's protection, so every degraded arm (no reader, no build_id,
        a raising read) fails TOWARD watching, and the raising arm says so
        at ERROR rather than passing quietly.
        """
        reader = self._build_mode_reader
        if reader is None or not build_id:
            return False
        from forge.lifecycle.modes import BuildMode

        try:
            mode = reader.get_build_mode(build_id)
        except Exception as exc:  # noqa: BLE001 — fail toward watching
            logger.error(
                "wireup._is_mode_c_build: mode read raised %s: %s for "
                "build_id=%s; KEEPING the FWD-002 identity watchdog armed "
                "(an unreadable row is not evidence of a fix journey, and "
                "disarming a routine build's stuck-build protection on a "
                "read failure is the silent downgrade this seam forbids)",
                type(exc).__name__,
                exc,
                build_id,
            )
            return False
        return mode is BuildMode.MODE_C

    async def _publish_identity_unresolved_failure(
        self,
        context: BuildContext,
        handle: BuildAckHandle,
        *,
        build_id: str | None = None,
    ) -> None:
        """Publish a synthetic ``build-failed`` for an unresolved identity.

        FWD-002 (WS3-S6): a build whose identity never resolves within the
        per-build deadline is genuinely stuck (the dispatch never produced a
        run) — emit a terminal ``build-failed`` so the operator's phone
        leaves the "queued" state and the queue slot is released, instead of
        redelivering into a silent infinite loop.

        The terminal sequence mirrors :meth:`_on_terminal`: publish first
        (recording the terminal ``builds`` row via
        :meth:`_publish_event`'s write-back), then ack + detach on a
        successful publish. On a publish failure the inbound message is left
        un-acked so JetStream redelivery / the next boot's recovery retries —
        never a silent drop.

        Args:
            context: The build's :class:`BuildContext`.
            handle: The inbound ack handle.
            build_id: Optional pre-resolved durable build_id, threaded in by
                the observer when the mode gate already resolved it (one
                resolver call per build, not two). ``None`` resolves here,
                the pre-lane path.
        """
        feature_id = context.feature_id
        if build_id is None:
            build_id = await self._resolve_build_id(feature_id, context.correlation_id)
        # AC-2: payload construction is the translator's job — the wireup
        # never constructs pipeline payloads. The translator's public
        # synthetic factory also attaches the correlation_id (T3 AC-6).
        payload = self._translator.build_synthetic_failed(
            feature_id=feature_id,
            build_id=build_id,
            correlation_id=context.correlation_id,
            failure_reason=IDENTITY_UNRESOLVED_FAILURE_REASON,
            recoverable=True,
        )

        logger.warning(
            "wireup: feature_id=%s identity unresolved past the per-build "
            "deadline; publishing synthetic build-failed (reason=%s "
            "build_id=%s) — a silent stuck build is being terminalised "
            "(FWD-002)",
            feature_id,
            IDENTITY_UNRESOLVED_FAILURE_REASON,
            build_id,
        )
        published = await self._publish_event(payload, feature_id)
        if published:
            await self._on_terminal(handle, feature_id, context.correlation_id)
        else:
            logger.warning(
                "wireup: synthetic build-failed publish FAILED for "
                "feature_id=%s; leaving the inbound message un-acked so "
                "JetStream redelivery / next-boot recovery retries",
                feature_id,
            )

    async def _publish_no_terminal_failure(
        self, context: BuildContext, handle: BuildAckHandle
    ) -> None:
        """Publish a synthetic ``build-failed`` for a stream with no terminal.

        F6 (2026-07-26 defect harvest): when the SSE stream closes cleanly
        with no terminal envelope AND :meth:`_fetch_and_replay_on_empty`
        cannot recover the run's terminal state, the ``builds`` row would
        otherwise stay RUNNING until the 300s per-build deadline timer fires
        — ``forge status`` misreports a finished build as still RUNNING.
        Emit a terminal ``build-failed`` immediately so the ledger leaves
        RUNNING promptly.

        The terminal sequence mirrors
        :meth:`_publish_identity_unresolved_failure`: publish first (the
        :meth:`_publish_event` write-back records the terminal ``builds``
        row, and loses gracefully to an already-terminal/cancelled row via
        the recorder's no-resurrection guard), then ack + detach on a
        successful publish. :meth:`_on_terminal`'s ``detach`` cancels the
        per-build deadline timer so it cannot fire a second synthetic
        build-failed. On a publish failure the inbound is left un-acked so
        JetStream redelivery / the next boot's recovery retries — never a
        silent drop.
        """
        feature_id = context.feature_id
        build_id = await self._resolve_build_id(feature_id, context.correlation_id)
        # AC-2: payload construction is the translator's job — the wireup
        # never constructs pipeline payloads. The public synthetic factory
        # also attaches the correlation_id (T3 AC-6).
        payload = self._translator.build_synthetic_failed(
            feature_id=feature_id,
            build_id=build_id,
            correlation_id=context.correlation_id,
            failure_reason=STREAM_NO_TERMINAL_FAILURE_REASON,
            recoverable=True,
        )

        logger.warning(
            "wireup: feature_id=%s stream ended without a terminal envelope "
            "and run-state fetch did not surface a terminal state; "
            "publishing synthetic build-failed (reason=%s build_id=%s) so "
            "builds.status leaves RUNNING promptly instead of waiting for "
            "the per-build deadline timer (F6)",
            feature_id,
            STREAM_NO_TERMINAL_FAILURE_REASON,
            build_id,
        )
        published = await self._publish_event(payload, feature_id)
        if published:
            await self._on_terminal(handle, feature_id, context.correlation_id)
        else:
            logger.warning(
                "wireup: synthetic build-failed publish FAILED for "
                "feature_id=%s (F6 no-terminal path); leaving the inbound "
                "message un-acked so JetStream redelivery / next-boot "
                "recovery retries",
                feature_id,
            )

    async def _resolve_build_id(self, feature_id: str, correlation_id: str) -> str:
        """Resolve the durable ``builds.build_id`` for a synthetic terminal.

        Production wires a SQLite reader so the terminal write hits the real
        queued row (un-wedging the feature's next dispatch). Without a
        resolver (unit tiers), or when the read misses, fall back to
        ``feature_id`` so the synthetic terminal still publishes — the
        primary FWD-002 invariant (no silent stuck build) holds regardless.
        """
        if self._build_id_resolver is not None:
            try:
                resolved = await self._build_id_resolver(feature_id, correlation_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "wireup._resolve_build_id: resolver raised (%s) for "
                    "feature_id=%s; falling back to feature_id",
                    exc,
                    feature_id,
                )
                resolved = None
            if resolved:
                return resolved
        logger.warning(
            "wireup._resolve_build_id: no durable build_id for feature_id=%s "
            "(resolver=%s); using feature_id as the synthetic build_id",
            feature_id,
            "wired" if self._build_id_resolver is not None else "absent",
        )
        return feature_id

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

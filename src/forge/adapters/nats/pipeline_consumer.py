"""Inbound build-queue subscription for the Forge pipeline.

This module owns Forge's JetStream pull consumer for ``pipeline.build-queued.>``
described in ``docs/design/contracts/API-nats-pipeline-events.md §2``.

Responsibilities (per TASK-NFI-007):

- Build the durable pull-consumer :class:`~nats.js.api.ConsumerConfig` exactly as
  specified in API contract §2.2 (``durable="forge-consumer"``,
  ``max_ack_pending=1``, ``ack_wait=1h``, ``deliver_policy=ALL``,
  ``ack_policy=EXPLICIT``, ``max_deliver=-1``).
- Validate every incoming :class:`~nats_core.events.BuildQueuedPayload` and
  reject malformed payloads, unrecognised originators, and ``feature_yaml_path``
  values outside the configured filesystem allowlist by acking the JetStream
  message and publishing ``pipeline.build-failed.{feature_id}``.
- Detect already-terminal duplicates against an injected SQLite read helper and
  skip them idempotently (ack + no new build).
- For accepted builds, hand control to the state machine entrypoint together
  with an ``ack_callback`` closure bound to the JetStream ``Msg.ack`` method —
  the callback is invoked **only on terminal transitions**, leaving the
  message unacked across non-terminal states (PAUSED, RUNNING, etc.) so the
  position in the queue is preserved across restarts (ADR-SP-013).

The module is intentionally I/O-thin: real dependencies (state-machine entry
point, SQLite duplicate read, build-failed publisher) are injected through
:class:`PipelineConsumerDeps`. Tests substitute simple async callables; the
production wiring binds them to the concrete adapters in
``forge.adapters.nats`` / ``forge.adapters.sqlite`` / ``forge.pipeline``.

ADR / contract anchors:

- API contract: ``docs/design/contracts/API-nats-pipeline-events.md``
- Sequential-build constraint: ADR-ARCH-014 (``max_ack_pending=1``)
- Terminal-only ack semantics: ADR-SP-013
- Crash recovery: :func:`reconcile_on_boot` (added in TASK-NFI-009).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy
from nats.js.client import JetStreamContext
from nats_core.envelope import MessageEnvelope
from nats_core.events import (
    ApprovalRequestPayload,
    BuildFailedPayload,
    BuildPausedPayload,
    BuildQueuedPayload,
)
from pydantic import ValidationError

from forge.config.models import ForgeConfig
from forge.pipeline.build_ack_handle import (
    BuildAckHandle,
    InFlightAckRegistry,
    make_msg_ack_handle,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants pinned to the API contract (API-nats-pipeline-events.md §2)
# ---------------------------------------------------------------------------

#: NATS JetStream stream name carrying both the inbound build queue and the
#: outbound lifecycle events (configured by ``nats-infrastructure``).
STREAM_NAME: str = "PIPELINE"

#: Durable consumer name. Survives Forge restart so unacked messages are
#: redelivered after a crash.
DURABLE_NAME: str = "forge-consumer"

#: Subject pattern subscribed to by the pull consumer. The trailing ``>`` is
#: a NATS wildcard matching every ``feature_id``.
BUILD_QUEUE_SUBJECT: str = "pipeline.build-queued.>"

#: ``ack_wait`` as a float number of seconds. The contract phrases it as
#: ``timedelta(hours=1)``; nats-py's :class:`ConsumerConfig` takes seconds.
ACK_WAIT_SECONDS: float = float(60 * 60)

#: Subject prefix for the build-failed publish. The full subject is
#: ``pipeline.build-failed.{feature_id}`` per API contract §3.1.
BUILD_FAILED_SUBJECT_PREFIX: str = "pipeline.build-failed"

#: ``source_id`` used on every envelope Forge publishes.
FORGE_SOURCE_ID: str = "forge"

#: Sentinel used in build-failed payloads when we never got far enough to
#: parse a real ``feature_id`` (e.g. malformed payload).
UNKNOWN_FEATURE_ID: str = "unknown"


# ---------------------------------------------------------------------------
# Failure reasons — these strings are part of the contract surface and are
# asserted verbatim by tests.
# ---------------------------------------------------------------------------

REASON_MALFORMED_PAYLOAD: str = "malformed BuildQueuedPayload"
REASON_PATH_OUTSIDE_ALLOWLIST: str = "path outside allowlist"
REASON_ORIGINATOR_NOT_RECOGNISED: str = "originator not recognised"


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

AckCallback = Callable[[], Awaitable[None]]
"""Closure handed to the state machine. Calling it acks the JetStream message."""

IsDuplicateTerminal = Callable[[str, str], Awaitable[bool]]
"""``async (feature_id, correlation_id) -> bool`` — True if the build already
terminated (``COMPLETE | FAILED | CANCELLED | SKIPPED``) and the consumer
should ack-and-skip."""

RegisterObserver = Callable[[], Awaitable[None]]
"""``async () -> None`` — TASK-GATE-D659 R1 deferred bridge-registration.

The consumer builds this closure (it captures the accepted build's
:class:`BuildAckHandle` + identity) and hands it to ``dispatch_build``.
``dispatch_build`` invokes it ONLY on the approve → launch path so no
bridge observer is live while the build is paused at the pre-dispatch
approval gate. ``None`` (no bridge wired) means "skip registration"."""

DispatchBuild = Callable[..., Awaitable[None]]
"""``async (payload, ack_callback[, register_observer]) -> None``.

Pipeline state-machine entry. The optional third argument is the
TASK-GATE-D659 R1 :data:`RegisterObserver` deferred-registration closure;
the consumer passes it only when the bridge is wired
(``register_ack_handle`` is not ``None``). Legacy two-argument callers
(unit-test doubles, the crash-recovery redelivery path) are unaffected —
the consumer omits the third argument when no bridge is wired."""

PublishBuildFailed = Callable[..., Awaitable[None]]
"""``async (failure_payload, feature_id, *, correlation_id) -> None``.

The wrapper publishes to ``pipeline.build-failed.{feature_id}`` and owns
envelope construction. The ``correlation_id`` keyword is the inbound
:attr:`MessageEnvelope.correlation_id` of the message that triggered the
rejection — DDR-029 requires every outbound lifecycle envelope to carry
the originating ``correlation_id`` so subscribers (jarvis ``forge_subscriber``)
can route the notification back to the originating chat session. Pass
``None`` only when the inbound envelope itself failed to parse — in every
other rejection path the envelope is available and its ``correlation_id``
must be threaded explicitly."""


@runtime_checkable
class _MsgLike(Protocol):
    """Minimal slice of :class:`nats.aio.msg.Msg` we depend on.

    Declared so tests can use lightweight ``unittest.mock.AsyncMock`` doubles
    without having to monkey-patch the full nats-py msg class.
    """

    data: bytes

    async def ack(self) -> None:  # pragma: no cover - protocol stub
        ...


# ---------------------------------------------------------------------------
# Dependency container
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PipelineConsumerDeps:
    """Injected collaborators of the message-processing pipeline.

    Keeping these on a frozen dataclass means the consumer can be unit-tested
    with simple async callables and re-wired in production without touching
    the validation logic itself.

    Attributes:
        forge_config: Validated :class:`ForgeConfig`.
        is_duplicate_terminal: Async predicate, ``(feature_id,
            correlation_id) -> bool``. ``True`` short-circuits dispatch
            on a known-terminal build (idempotent ack-and-skip).
        dispatch_build: State-machine entry point. Receives the
            validated :class:`BuildQueuedPayload` plus an idempotent
            :data:`AckCallback`. The callback is the same primitive
            wrapped by the registered :class:`BuildAckHandle` so legacy
            callers and the Wave-1 bridge end up acking the same
            JetStream message exactly once.
        publish_build_failed: Async wrapper that publishes
            ``pipeline.build-failed.{feature_id}`` with the inbound
            ``correlation_id`` threaded onto the outbound envelope
            (DDR-029).
        register_ack_handle: Optional :data:`InFlightAckRegistry` —
            when wired (Wave-1 bridge), the consumer registers a
            :class:`BuildAckHandle` keyed by ``(feature_id,
            correlation_id)`` *before* invoking ``dispatch_build`` so
            the bridge can ack on terminal SSE arrival. ``None``
            (default) preserves the F010F sync-raise fallback used by
            unit tests that don't exercise the bridge: ``dispatch_build``
            still receives an idempotent ack callback bound to
            ``msg.ack`` and ack timing is the state machine's
            responsibility (the existing AC-009 contract from
            TASK-NFI-007).
    """

    forge_config: ForgeConfig
    is_duplicate_terminal: IsDuplicateTerminal
    dispatch_build: DispatchBuild
    publish_build_failed: PublishBuildFailed
    register_ack_handle: InFlightAckRegistry | None = None


# ---------------------------------------------------------------------------
# Consumer config + subscription wiring
# ---------------------------------------------------------------------------


def build_consumer_config() -> ConsumerConfig:
    """Return the durable pull-consumer config exactly as pinned by §2.2.

    Notes:
        ``filter_subject`` is set to the same subject as ``pull_subscribe``'s
        ``subject`` argument. nats-py is happy with redundancy here and the
        contract spec lists it explicitly, so we mirror it.
    """

    return ConsumerConfig(
        durable_name=DURABLE_NAME,
        deliver_policy=DeliverPolicy.ALL,
        ack_policy=AckPolicy.EXPLICIT,
        ack_wait=ACK_WAIT_SECONDS,
        max_deliver=-1,
        max_ack_pending=1,
        filter_subject=BUILD_QUEUE_SUBJECT,
    )


async def start_pipeline_consumer(
    js: JetStreamContext,
) -> JetStreamContext.PullSubscription:
    """Bind the durable pull subscription on ``js`` and return it.

    The caller is expected to drive the subscription with ``fetch()`` in a
    long-running task and dispatch each message into :func:`handle_message`.
    Decoupling the bind from the dispatch loop keeps the function trivially
    testable and lets crash-recovery (TASK-NFI-009) reuse the same factory.
    """

    return await js.pull_subscribe(
        subject=BUILD_QUEUE_SUBJECT,
        durable=DURABLE_NAME,
        stream=STREAM_NAME,
        config=build_consumer_config(),
    )


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _path_inside_allowlist(candidate: str, allowlist: list[Path]) -> bool:
    """Return True iff ``candidate`` resolves inside one of ``allowlist``.

    Uses :meth:`pathlib.Path.resolve` to collapse ``..`` traversal **before**
    the :meth:`pathlib.Path.is_relative_to` check (AC-010). Each allowlisted
    path is also resolved so that a symlink in the allowlist root does not
    silently widen the authorised footprint at the cost of a divergence
    between the on-disk and resolved roots.

    ``candidate`` does not need to exist on disk — :meth:`Path.resolve` is
    purely lexical for paths that have not been ``mkdir``'d.
    """

    try:
        target = Path(candidate).resolve()
    except (OSError, ValueError):
        # OSError can come from Windows-style invalid paths on POSIX; both
        # are simply "not in the allowlist" from our POV.
        return False

    for allowed in allowlist:
        try:
            allowed_resolved = allowed.resolve()
        except (OSError, ValueError):
            continue
        if target.is_relative_to(allowed_resolved):
            return True
    return False


def _failure_payload(
    *,
    feature_id: str,
    build_id: str,
    reason: str,
) -> BuildFailedPayload:
    """Construct the :class:`BuildFailedPayload` for a rejection.

    Rejected-on-receive failures are not recoverable: the originating adapter
    must reissue the build with corrected inputs. We therefore set
    ``recoverable=False`` for every reason this module produces.
    """

    return BuildFailedPayload(
        feature_id=feature_id,
        build_id=build_id,
        failure_reason=reason,
        recoverable=False,
        failed_task_id=None,
    )


async def _safe_publish_failure(
    deps: PipelineConsumerDeps,
    failure: BuildFailedPayload,
    feature_id: str,
    *,
    correlation_id: str | None,
) -> None:
    """Publish ``build-failed`` while absorbing transport-level errors.

    TASK-FW10-009 / Group C "publish failure does not regress" — per
    ADR-ARCH-008 a NATS publish failure on a validation-rejection path
    MUST NOT propagate out of :func:`handle_message`. The recorded
    transition (which on these paths is empty — the message is rejected
    before any state-machine entry) stays untouched, the inbound message
    is still acked, and the fetch loop continues to the next message.

    Args:
        deps: Pipeline-consumer dependency container.
        failure: The :class:`BuildFailedPayload` to publish.
        feature_id: Feature identifier used for the
            ``pipeline.build-failed.{feature_id}`` subject.
        correlation_id: Inbound envelope's ``correlation_id`` (DDR-029).
            ``None`` only when the envelope itself failed to parse and
            no source value is available; every other rejection path
            MUST pass the inbound value so subscribers can route the
            notification back to the originating chat session.

    Notes:
        We deliberately catch :class:`Exception` rather than a narrower
        type. The publish surface depends on the injected publisher's
        transport; in production it is ``nats-py``'s asyncio JetStream
        publish which can raise a wide family of transient errors
        (connection reset, backpressure, no-responders, …). Logging at
        WARNING preserves operator visibility without crashing the
        daemon.
    """

    try:
        await deps.publish_build_failed(
            failure, feature_id, correlation_id=correlation_id
        )
    except Exception as exc:
        logger.warning(
            "pipeline_consumer: publish_build_failed raised (%s) for "
            "feature_id=%s reason=%r; recorded transition unchanged, "
            "daemon continues",
            exc,
            feature_id,
            failure.failure_reason,
        )


# ---------------------------------------------------------------------------
# Core message handler
# ---------------------------------------------------------------------------


async def handle_message(msg: _MsgLike, deps: PipelineConsumerDeps) -> None:
    """Validate one JetStream message and route it to the state machine.

    Outcomes (mutually exclusive — exactly one fires per call):

    1. *Malformed envelope or payload* → ``msg.ack()`` and publish
       ``build-failed`` with reason :data:`REASON_MALFORMED_PAYLOAD`.
       The state machine is **never** invoked.
    2. *Unrecognised ``originating_adapter``* → ack + ``build-failed`` with
       :data:`REASON_ORIGINATOR_NOT_RECOGNISED`.
    3. *``feature_yaml_path`` outside allowlist* → ack + ``build-failed``
       with :data:`REASON_PATH_OUTSIDE_ALLOWLIST`. ``..`` traversal is
       rejected because :func:`_path_inside_allowlist` calls
       :meth:`Path.resolve` before :meth:`Path.is_relative_to`.
    4. *Duplicate already-terminal build* → ack + idempotent skip. No build
       is started, no event is published.
    5. *Accepted build* → :meth:`PipelineConsumerDeps.dispatch_build` is
       awaited with an ``ack_callback`` bound to ``msg.ack``. The message
       remains unacked until the state machine invokes the callback at the
       terminal transition.

    The function never raises on bad input — every validation failure is
    captured and translated into outcome (1)–(3). It still propagates
    ``asyncio.CancelledError`` (the calling fetch loop owns shutdown
    semantics, per the task brief).
    """

    # --- 1. Parse envelope + payload -------------------------------------
    try:
        envelope = MessageEnvelope.model_validate_json(msg.data)
    except (ValidationError, ValueError) as exc:
        logger.warning(
            "pipeline_consumer: could not parse MessageEnvelope (%s); "
            "acking and publishing build-failed",
            exc,
        )
        await msg.ack()
        # Envelope itself failed to parse — there is no source value for
        # ``correlation_id`` (DDR-029). This is the only rejection path
        # where ``correlation_id=None`` on the outbound envelope is
        # acceptable; every other path threads ``envelope.correlation_id``.
        await _safe_publish_failure(
            deps,
            _failure_payload(
                feature_id=UNKNOWN_FEATURE_ID,
                build_id=UNKNOWN_FEATURE_ID,
                reason=REASON_MALFORMED_PAYLOAD,
            ),
            UNKNOWN_FEATURE_ID,
            correlation_id=None,
        )
        return

    try:
        payload = BuildQueuedPayload.model_validate(envelope.payload)
    except ValidationError as exc:
        # We have a parseable envelope, so thread its ``correlation_id``
        # onto the outbound ``build-failed`` envelope (DDR-029) and use
        # whatever identifying info the envelope carries.
        feature_id = _safe_envelope_feature(envelope) or UNKNOWN_FEATURE_ID
        logger.warning(
            "pipeline_consumer: BuildQueuedPayload validation failed for "
            "feature_id=%s (%s); acking and publishing build-failed",
            feature_id,
            exc,
        )
        await msg.ack()
        await _safe_publish_failure(
            deps,
            _failure_payload(
                feature_id=feature_id,
                build_id=feature_id,
                reason=REASON_MALFORMED_PAYLOAD,
            ),
            feature_id,
            correlation_id=envelope.correlation_id,
        )
        return

    # --- 2. Originator allowlist -----------------------------------------
    # The effective originator identity is the ``originating_adapter`` when a
    # human adapter is present (the CLI / jarvis path), else the
    # ``triggered_by`` layer (a forge-internal MACHINE dispatch — the Lane B
    # planning target-terminal, which is contract-legal with
    # triggered_by="forge-internal" and no adapter, per nats-core's own
    # BuildQueuedPayload validator). Membership is still governed by
    # ``forge_config.pipeline.approved_originators`` (config-overridable) —
    # the bus is credentialed, so this is a policy filter, not cryptography.
    #
    # Adapter-takes-precedence (the stricter reading): when an adapter IS
    # present it alone is checked, so an unapproved adapter is rejected even
    # if ``triggered_by`` happens to name an approved layer. jarvis semantics
    # are unchanged — the payload validator forces an adapter whenever
    # triggered_by=='jarvis', so a jarvis build never falls through to the
    # triggered_by branch here.
    approved = deps.forge_config.pipeline.approved_originators
    originator = (
        payload.originating_adapter
        if payload.originating_adapter is not None
        else payload.triggered_by
    )
    if originator not in approved:
        logger.warning(
            "pipeline_consumer: originator not in approved list for "
            "feature_id=%s (originating_adapter=%r triggered_by=%r); rejecting",
            payload.feature_id,
            payload.originating_adapter,
            payload.triggered_by,
        )
        await msg.ack()
        await _safe_publish_failure(
            deps,
            _failure_payload(
                feature_id=payload.feature_id,
                build_id=payload.feature_id,
                reason=REASON_ORIGINATOR_NOT_RECOGNISED,
            ),
            payload.feature_id,
            correlation_id=envelope.correlation_id,
        )
        return

    # --- 3. Path allowlist -----------------------------------------------
    allowlist = deps.forge_config.permissions.filesystem.allowlist
    if not _path_inside_allowlist(payload.feature_yaml_path, allowlist):
        logger.warning(
            "pipeline_consumer: feature_yaml_path=%r outside allowlist for "
            "feature_id=%s; rejecting",
            payload.feature_yaml_path,
            payload.feature_id,
        )
        await msg.ack()
        await _safe_publish_failure(
            deps,
            _failure_payload(
                feature_id=payload.feature_id,
                build_id=payload.feature_id,
                reason=REASON_PATH_OUTSIDE_ALLOWLIST,
            ),
            payload.feature_id,
            correlation_id=envelope.correlation_id,
        )
        return

    # --- 4. Duplicate detection (idempotent skip) ------------------------
    if await deps.is_duplicate_terminal(payload.feature_id, payload.correlation_id):
        logger.info(
            "pipeline_consumer: duplicate already-terminal build "
            "feature_id=%s correlation_id=%s; ack + skip",
            payload.feature_id,
            payload.correlation_id,
        )
        await msg.ack()
        return

    # --- 5. Accepted build — dispatch with deferred ack ------------------
    # Build a single idempotent ack handle for this msg. Two consumers
    # of the handle exist:
    #
    #   1. ``ack_callback`` — the legacy F010F primitive handed to
    #      ``dispatch_build``. The state machine (or the dispatch
    #      closure under unit-test stubs) calls it on terminal
    #      transition; calling it more than once is a no-op via the
    #      handle's idempotency.
    #   2. The Wave-1 lifecycle bridge — when ``register_ack_handle``
    #      is wired, the consumer registers the same handle keyed by
    #      ``(feature_id, correlation_id)`` so the bridge can
    #      ``ack()`` (or ``nak()``) when it observes terminal on the
    #      SSE stream. Both consumers share state through the handle's
    #      flags so ``msg.ack()`` fires exactly once regardless of
    #      whether the bridge or the legacy callback wins the race
    #      (TASK-FRR-PEB-001 AC-1 + AC-2).
    #
    # When ``register_ack_handle`` is ``None`` (no bridge wired — unit
    # tests, legacy code paths) the handle is created but never
    # registered; the legacy ``ack_callback`` is the only path to
    # ``msg.ack()`` and the F010F sync-raise fallback in the
    # ``except`` branch below ensures the message is acked on
    # ``dispatch_build`` raise. This is AC-3 in the task brief.
    ack_handle = make_msg_ack_handle(msg)
    ack_callback = _ack_callback_from_handle(ack_handle)

    # TASK-GATE-D659 R1 — DEFER bridge-observer registration until AFTER
    # the pre-dispatch approval gate approves. Previously the consumer
    # registered the ack handle HERE (before dispatch); a build that
    # paused at the gate then had a live observer during the multi-minute
    # pause, which (a) resolved stale async_tasks rows and fetch-replayed
    # a dead run's terminal (false BuildStarted/BuildComplete + early ack
    # + detach mid-pause), or (b) identity-timed-out and popped the handle
    # so no observer survived the approve. Instead we build a
    # ``register_observer`` closure and hand it to ``dispatch_build``,
    # which invokes it ONLY on the approve → launch path so the observer
    # spawns adjacent to the fresh run. When no bridge is wired
    # (``register_ack_handle is None`` — unit tests, legacy paths) the
    # closure is ``None`` and the call stays two-argument, preserving the
    # F010F sync-raise ack fallback below.
    register_observer: RegisterObserver | None = None
    if deps.register_ack_handle is not None:
        register_observer = _make_deferred_registration(
            deps.register_ack_handle,
            feature_id=payload.feature_id,
            correlation_id=payload.correlation_id,
            handle=ack_handle,
        )

    logger.info(
        "pipeline_consumer: dispatching build feature_id=%s correlation_id=%s "
        "originating_adapter=%s bridge=%s",
        payload.feature_id,
        payload.correlation_id,
        payload.originating_adapter,
        "wired (deferred)" if deps.register_ack_handle is not None else "fallback",
    )
    try:
        if register_observer is not None:
            await deps.dispatch_build(payload, ack_callback, register_observer)
        else:
            await deps.dispatch_build(payload, ack_callback)
    except Exception as exc:
        # TASK-FW10-009 / Group C "dispatch error contained": the state
        # machine raised out of the dispatch path before reaching a
        # terminal transition. We MUST stop the message from blocking
        # the consumer (``max_ack_pending=1`` per ADR-ARCH-014) so the
        # next delivered build is processed. Approach:
        #
        #   1. Log at WARNING with the failed identity for triage.
        #   2. Publish a terminal ``build-failed`` envelope (TASK-FORGE-
        #      FRR-F010F) threading the inbound ``correlation_id`` so the
        #      operator's chat session sees a terminal envelope instead
        #      of a silent drop.
        #   3. Best-effort ack via the idempotent callback so JetStream
        #      releases the slot. If the state machine had already
        #      transitioned to a terminal state and acked, the second
        #      call is a no-op (see :func:`_build_ack_callback`).
        #   4. Swallow the exception — the daemon stays running.
        #
        # We *do* publish ``build-failed`` here when ``dispatch_build``
        # raises before the running state machine takes ownership of
        # the publish (TASK-FORGE-FRR-F010F). The no-duplicate
        # guarantee from ADR-ARCH-008 still holds: raising out of
        # ``dispatch_build`` to this outer try/except means the state
        # machine never started transitioning, so it will never publish
        # a competing terminal envelope. Empirically observed twice on
        # 2026-05-04 (Gap F010.B and Gap F010.E); the silent-ack
        # behaviour broke DDR-030's between-prompt notification
        # contract for any class of dispatch error.
        logger.warning(
            "pipeline_consumer: dispatch_build raised (%s) for "
            "feature_id=%s correlation_id=%s; publishing build-failed "
            "and acking so the next build can be processed",
            exc,
            payload.feature_id,
            payload.correlation_id,
        )
        # Publish FIRST so a transport-level publish failure cannot leave
        # the message acked-but-undelivered. ``_safe_publish_failure``
        # already swallows publish errors and logs at WARNING, so a
        # publish failure does not prevent the subsequent ack.
        await _safe_publish_failure(
            deps,
            _failure_payload(
                feature_id=payload.feature_id,
                build_id=payload.feature_id,
                reason=f"{exc.__class__.__name__}: {exc}",
            ),
            payload.feature_id,
            correlation_id=envelope.correlation_id,
        )
        try:
            await ack_callback()
        except Exception as ack_exc:
            logger.warning(
                "pipeline_consumer: ack_callback raised (%s) after "
                "dispatch error for feature_id=%s; daemon continues "
                "without ack — JetStream will redeliver after ack_wait",
                ack_exc,
                payload.feature_id,
            )


def _build_ack_callback(msg: _MsgLike) -> AckCallback:
    """Return an idempotent ack closure bound to ``msg.ack``.

    Retained for crash-recovery (:func:`_reconcile_one_redelivery`)
    where the bridge is not in scope — paused/in-flight redelivery
    branches mark INTERRUPTED and call ``dispatch_build`` directly
    with this callback, bypassing the bridge registry so reconciliation
    keeps the same ack semantics it has had since TASK-NFI-007.

    The state machine may invoke the callback more than once across a long
    build (e.g. once on terminal transition, again on retry-from-scratch
    crash recovery). JetStream tolerates double-ack but we prefer to log
    the second call as a warning and short-circuit, which makes the
    "ack called exactly once" acceptance criterion verifiable from tests.
    """

    state: dict[str, bool] = {"acked": False}

    async def _ack() -> None:
        if state["acked"]:
            logger.debug(
                "pipeline_consumer: ack_callback invoked twice; ignoring "
                "second call (idempotent)"
            )
            return
        state["acked"] = True
        await msg.ack()

    return _ack


def _ack_callback_from_handle(handle: BuildAckHandle) -> AckCallback:
    """Return an :data:`AckCallback` that delegates to ``handle.ack``.

    The legacy state-machine seam still receives a plain
    ``async () -> None`` callable (the :data:`AckCallback` type alias),
    so we adapt the new :class:`BuildAckHandle` Protocol back to that
    shape. Because the handle is itself idempotent, the returned
    closure inherits the "ack exactly once" guarantee — calling
    ``ack_callback()`` after the bridge has already invoked
    ``handle.ack()`` is a logged no-op rather than a second
    ``msg.ack()`` round-trip.
    """

    async def _ack() -> None:
        await handle.ack()

    return _ack


def _make_deferred_registration(
    register_ack_handle: InFlightAckRegistry,
    *,
    feature_id: str,
    correlation_id: str,
    handle: BuildAckHandle,
) -> RegisterObserver:
    """Return the TASK-GATE-D659 R1 deferred bridge-registration closure.

    The returned ``async () -> None`` closure registers ``handle`` against
    ``(feature_id, correlation_id)`` in the bridge's in-flight store. It is
    handed to ``dispatch_build``, which invokes it ONLY on the approve →
    launch path so no bridge observer exists while the build is paused at
    the pre-dispatch approval gate. Registration is best-effort: a raising
    registry is swallowed and logged (the legacy ack_callback path still
    works) — the same non-fatal posture the pre-relocation call had.
    """

    async def _register() -> None:
        try:
            await register_ack_handle(feature_id, correlation_id, handle)
        except Exception as reg_exc:  # noqa: BLE001 — non-fatal registration
            logger.warning(
                "pipeline_consumer: deferred register_ack_handle raised (%s) "
                "for feature_id=%s correlation_id=%s; continuing with legacy "
                "ack_callback fallback",
                reg_exc,
                feature_id,
                correlation_id,
            )

    return _register


def _safe_envelope_feature(envelope: MessageEnvelope) -> str | None:
    """Best-effort extraction of ``feature_id`` from an unvalidated envelope.

    The envelope ``payload`` is a free-form ``dict[str, Any]`` until we
    round-trip it through :class:`BuildQueuedPayload`. If validation failed
    we still want to publish a useful build-failed event keyed on whichever
    id the producer included; this helper returns it without raising.
    """

    raw: Any = envelope.payload
    if isinstance(raw, dict):
        candidate = raw.get("feature_id")
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


# ---------------------------------------------------------------------------
# Crash-recovery reconciliation (TASK-NFI-009 / API-nats-pipeline-events.md §4)
# ---------------------------------------------------------------------------


#: Persisted build states that mean the build already finished. Their
#: redelivered ``build-queued`` messages are acked-and-skipped — the
#: previous run completed before its ack was committed (idempotency).
TERMINAL_BUILD_STATES: frozenset[str] = frozenset(
    {"COMPLETE", "FAILED", "CANCELLED", "SKIPPED"}
)

#: Persisted build states that mean the build was actively running when
#: the previous Forge process died. We mark them ``INTERRUPTED`` and
#: restart from ``PREPARING`` (DM-build-lifecycle.md §2.1; ADR-SP-013
#: retry-from-scratch policy).
IN_FLIGHT_BUILD_STATES: frozenset[str] = frozenset({"RUNNING", "FINALISING"})

#: Persisted build states that, on redelivery, must be retried from
#: scratch via the same ``INTERRUPTED → PREPARING`` transition. We
#: include ``PREPARING`` here so a crash mid-worktree-creation does not
#: leave the build wedged.
RESTART_FROM_PREPARING_STATES: frozenset[str] = IN_FLIGHT_BUILD_STATES | frozenset(
    {"PREPARING"}
)

#: Single literal value: the persisted state for paused builds. Pulled
#: out as a constant so tests can assert verbatim against the string the
#: SQLite reader returns.
PAUSED_BUILD_STATE: str = "PAUSED"

#: Single literal value: the persisted state for a build the previous
#: process was driving when it died and that was never reset to
#: ``PREPARING`` (the reset is the second of two writes, so a crash can
#: land between them). It had no branch of its own until the conductor
#: rewire: an INTERRUPTED fix journey fell through to the defensive
#: "unexpected state" warning below and was re-dispatched anyway, which
#: was the right ACT reached by the wrong ROUTE. It is a named branch
#: now — see :func:`_reconcile_one_redelivery`.
INTERRUPTED_BUILD_STATE: str = "INTERRUPTED"


@dataclass(frozen=True, slots=True)
class PausedBuildSnapshot:
    """Snapshot of a paused build, sufficient to re-emit lifecycle events.

    The SQLite reader hydrates one of these per row whose ``status`` is
    ``PAUSED``. The two payloads carry the **original** ``correlation_id``
    so subscribers (Jarvis, dashboards) thread the re-announcement onto
    the same conversation as the build that was paused before the crash
    (Group D @edge-case; ADR-ARCH-021 "first response wins").

    Attributes:
        feature_id: ``FEAT-XXXX`` identifier; mirrored on both payloads
            and used for subject construction.
        correlation_id: Original ``correlation_id`` threaded through the
            paused build's lifecycle. MUST equal
            ``build_paused_payload.correlation_id``.
        build_paused_payload: Re-emit verbatim via
            :class:`PipelinePublisher.publish_build_paused`.
        approval_request_payload: Re-emit verbatim on
            ``approval_subject``. Carries the same ``request_id`` as the
            original — ADR-ARCH-021's first-response-wins semantics treat
            duplicate requests with the same ``request_id`` as the same
            request, so a late approval responder cannot double-resume.
        approval_subject: NATS subject the approval request was originally
            published on (mirrors :attr:`BuildPausedPayload.approval_subject`).
    """

    feature_id: str
    correlation_id: str
    build_paused_payload: BuildPausedPayload
    approval_request_payload: ApprovalRequestPayload
    approval_subject: str


# Type aliases for crash-recovery collaborators ------------------------------

ReadBuildState = Callable[[str, str], Awaitable["str | None"]]
"""``async (feature_id, correlation_id) -> persisted state | None``.

Returns the SQLite ``builds.status`` value for the row matching the
identity, or ``None`` if no row exists (unknown build → fresh).
"""

MarkInterruptedAndReset = Callable[[str, str], Awaitable[None]]
"""``async (feature_id, correlation_id) -> None``.

Persists the ``INTERRUPTED`` transition and immediately re-resets the
row to ``PREPARING`` so the redispatched build re-enters the normal
state machine cleanly. Implementations are expected to be atomic.
"""

IterPausedBuilds = Callable[[], Awaitable["list[PausedBuildSnapshot]"]]
"""``async () -> list[PausedBuildSnapshot]`` — every PAUSED row in SQLite."""

PublishBuildPausedFn = Callable[[BuildPausedPayload], Awaitable[None]]
"""Re-emit ``pipeline.build-paused.{feature_id}``."""

PublishApprovalRequestFn = Callable[[ApprovalRequestPayload, str], Awaitable[None]]
"""``async (payload, approval_subject) -> None`` — re-emit the approval
request on its original subject."""

FetchRedeliveries = Callable[[], Awaitable["list[_MsgLike]"]]
"""``async () -> list[Msg]`` — drain one batch from the durable pull
subscription. Returns an empty list when the inbox is empty."""


@dataclass(frozen=True, slots=True)
class ReconcileDeps:
    """Injected collaborators for :func:`reconcile_on_boot`.

    Wraps the existing :class:`PipelineConsumerDeps` (re-used for the
    "fresh build" branch) and adds the SQLite reader, INTERRUPTED writer,
    paused-row enumerator, and the two re-emit publishers needed for the
    paused branch.

    A second dataclass (rather than extending :class:`PipelineConsumerDeps`)
    is used so production wiring can construct one consumer's worth of
    deps and reuse them both at boot and during normal flow without
    carrying boot-only collaborators forever.
    """

    consumer_deps: PipelineConsumerDeps
    fetch_redeliveries: FetchRedeliveries
    read_build_state: ReadBuildState
    mark_interrupted_and_reset: MarkInterruptedAndReset
    iter_paused_builds: IterPausedBuilds
    publish_build_paused: PublishBuildPausedFn
    publish_approval_request: PublishApprovalRequestFn


@dataclass
class ReconcileReport:
    """Per-branch counters describing what reconcile_on_boot did.

    Used for boot-time logs and observability. Not load-bearing for
    correctness — the function's contract is the side effects (acks,
    state writes, re-emitted events), not this report.

    Attributes:
        acked_terminal: Redelivered messages whose SQLite row was already
            in a terminal state (acked + skipped).
        restarted_in_flight: Redelivered messages whose SQLite row was
            ``RUNNING / FINALISING / PREPARING`` (marked INTERRUPTED then
            re-dispatched from PREPARING).
        re_emitted_paused: Redelivered messages whose SQLite row was
            ``PAUSED`` (re-emitted BuildPaused + ApprovalRequest).
        fresh_builds: Redelivered messages with no matching SQLite row
            (handed to the normal :func:`handle_message` path).
        malformed: Redelivered messages that failed envelope or payload
            validation (handed to :func:`handle_message`, which acks +
            publishes ``build-failed``).
        paused_scan_re_emitted: PAUSED rows the SQLite belt-and-braces
            scan re-emitted that the redelivery loop did NOT already
            cover (so total paused re-emissions == ``re_emitted_paused +
            paused_scan_re_emitted``).
        restarted_interrupted: Redelivered messages whose SQLite row was
            already ``INTERRUPTED`` (a crash between the two recovery
            writes) and was restarted from scratch.
    """

    acked_terminal: int = 0
    restarted_in_flight: int = 0
    restarted_interrupted: int = 0
    re_emitted_paused: int = 0
    fresh_builds: int = 0
    malformed: int = 0
    paused_scan_re_emitted: int = 0
    redelivery_keys: set[tuple[str, str]] = field(default_factory=set)


async def reconcile_on_boot(deps: ReconcileDeps) -> ReconcileReport:
    """Run crash-recovery reconciliation exactly once at startup.

    Drains every redelivered ``build-queued`` message from the durable
    pull subscription, applies the §4 reconciliation rules, then runs a
    belt-and-braces scan of SQLite for ``PAUSED`` builds whose paused
    event must be re-announced even if JetStream did not redeliver
    (Group D @edge-case).

    Reconciliation rules — applied per redelivered ``BuildQueuedPayload``:

    1. *terminal* (``COMPLETE | FAILED | CANCELLED | SKIPPED``)
       → ack the message; do not start a new build (idempotent).
    2. *in-flight or PREPARING* (``RUNNING | FINALISING | PREPARING``)
       → mark ``INTERRUPTED`` then reset to ``PREPARING``
       (retry-from-scratch), and hand the message to the standard
       dispatch path with a deferred ack callback.
    3. *paused* (``PAUSED``)
       → re-emit :class:`BuildPausedPayload` and
       :class:`ApprovalRequestPayload` with the ORIGINAL ``correlation_id``
       (first-response-wins per ADR-ARCH-021), leave the message unacked
       so the queue position is preserved.
    4. *interrupted* (``INTERRUPTED``)
       → retry from scratch: hand the message to the standard dispatch
       path, which re-enters the row through the pre-dispatch approval
       gate (no reset to ``PREPARING`` first — that would strand the
       redelivery on dispatch's held-slot arm). A fix journey taking this
       route is re-carded and reuses the working tree it already has
       (ADR-ARCH-028).
    5. *unknown* (no SQLite row matches)
       → fresh build; hand to :func:`handle_message` for the normal
       validation + dispatch path.

    The SQLite scan that runs after the redelivery drain re-emits
    paused events for any ``PAUSED`` rows whose redelivery did not fire
    (e.g. message expired from JetStream stream). Builds already
    re-emitted via the redelivery path are skipped — keyed on
    ``(feature_id, correlation_id)`` — so subscribers never see a
    duplicate paused event from one ``reconcile_on_boot`` call.

    Args:
        deps: Injected collaborators. See :class:`ReconcileDeps`.

    Returns:
        :class:`ReconcileReport` with per-branch counters. Useful for
        boot-log observability; not load-bearing for correctness.

    Notes:
        Per the task brief, this function may be called sequentially
        over the redelivery queue at startup — there is no concurrency
        requirement. Normal operation (post-boot) remains async per
        message via :func:`handle_message`.
    """

    report = ReconcileReport()

    # Pre-fetch all paused snapshots once; index by identity tuple so
    # the redelivery loop can hand over the originals without re-reading
    # SQLite per-message.
    snapshots = await deps.iter_paused_builds()
    paused_by_key: dict[tuple[str, str], PausedBuildSnapshot] = {
        (s.feature_id, s.correlation_id): s for s in snapshots
    }

    # --- Phase 1: drain redelivered messages -----------------------------
    while True:
        batch = await deps.fetch_redeliveries()
        if not batch:
            break
        for msg in batch:
            await _reconcile_one_redelivery(msg, deps, report, paused_by_key)

    # --- Phase 2: belt-and-braces SQLite scan for PAUSED rows ------------
    # Re-emit any paused rows the redelivery loop did not already cover.
    # The `redelivery_keys` set guarantees we never emit twice for the
    # same (feature_id, correlation_id) within one call.
    for key, snap in paused_by_key.items():
        if key in report.redelivery_keys:
            continue
        await deps.publish_build_paused(snap.build_paused_payload)
        await deps.publish_approval_request(
            snap.approval_request_payload, snap.approval_subject
        )
        report.paused_scan_re_emitted += 1

    logger.info(
        "reconcile_on_boot: complete acked_terminal=%d restarted=%d "
        "restarted_interrupted=%d re_emitted_paused=%d fresh=%d "
        "malformed=%d paused_scan=%d",
        report.acked_terminal,
        report.restarted_in_flight,
        report.restarted_interrupted,
        report.re_emitted_paused,
        report.fresh_builds,
        report.malformed,
        report.paused_scan_re_emitted,
    )
    return report


async def _reconcile_one_redelivery(
    msg: _MsgLike,
    deps: ReconcileDeps,
    report: ReconcileReport,
    paused_by_key: dict[tuple[str, str], PausedBuildSnapshot],
) -> None:
    """Apply the §4 reconciliation rules to a single redelivered message.

    Mutually-exclusive outcomes mirror the rule table in
    :func:`reconcile_on_boot`. Malformed redeliveries are handed to
    :func:`handle_message` — that function already owns the
    ack + ``build-failed`` publish flow, so reconciliation does not
    duplicate it.
    """

    # Parse envelope + payload. Malformed payloads cannot be
    # reconciled — they have no usable identity — so we delegate to
    # :func:`handle_message` which acks and publishes ``build-failed``.
    try:
        envelope = MessageEnvelope.model_validate_json(msg.data)
        payload = BuildQueuedPayload.model_validate(envelope.payload)
    except (ValidationError, ValueError) as exc:
        logger.warning(
            "reconcile_on_boot: malformed redelivery (%s); delegating to "
            "handle_message for ack + build-failed publish",
            exc,
        )
        report.malformed += 1
        await handle_message(msg, deps.consumer_deps)
        return

    feature_id = payload.feature_id
    correlation_id = payload.correlation_id
    key = (feature_id, correlation_id)

    state = await deps.read_build_state(feature_id, correlation_id)

    # --- Branch 4: unknown build → fresh dispatch ------------------------
    if state is None:
        logger.info(
            "reconcile_on_boot: unknown build feature_id=%s correlation_id=%s; "
            "treating as fresh",
            feature_id,
            correlation_id,
        )
        report.fresh_builds += 1
        await handle_message(msg, deps.consumer_deps)
        return

    # --- Branch 1: terminal → ack + skip --------------------------------
    if state in TERMINAL_BUILD_STATES:
        logger.info(
            "reconcile_on_boot: terminal state=%s feature_id=%s correlation_id=%s; "
            "ack + skip (idempotent)",
            state,
            feature_id,
            correlation_id,
        )
        await msg.ack()
        report.acked_terminal += 1
        return

    # --- Branch 3: paused → re-emit lifecycle, leave unacked ------------
    if state == PAUSED_BUILD_STATE:
        snap = paused_by_key.get(key)
        if snap is None:
            # SQLite says PAUSED but the snapshot enumerator did not
            # surface this row. We cannot re-emit without the original
            # payloads, so the safest fallback is fresh dispatch — the
            # standard handler will detect the duplicate via
            # is_duplicate_terminal (False here) and either dispatch or
            # ack + build-failed if the payload no longer validates.
            logger.warning(
                "reconcile_on_boot: state=PAUSED but no snapshot for "
                "feature_id=%s correlation_id=%s; falling back to "
                "fresh dispatch",
                feature_id,
                correlation_id,
            )
            report.fresh_builds += 1
            await handle_message(msg, deps.consumer_deps)
            return

        logger.info(
            "reconcile_on_boot: re-emitting paused lifecycle for "
            "feature_id=%s correlation_id=%s",
            feature_id,
            correlation_id,
        )
        await deps.publish_build_paused(snap.build_paused_payload)
        await deps.publish_approval_request(
            snap.approval_request_payload, snap.approval_subject
        )
        report.re_emitted_paused += 1
        report.redelivery_keys.add(key)
        # NOTE: do not ack — paused builds keep the queue position so
        # JetStream redelivers again on the next crash.
        return

    # --- Branch 2: in-flight (or PREPARING) → mark INTERRUPTED, restart -
    if state in RESTART_FROM_PREPARING_STATES:
        logger.info(
            "reconcile_on_boot: in-flight state=%s feature_id=%s "
            "correlation_id=%s; marking INTERRUPTED + restarting from "
            "PREPARING",
            state,
            feature_id,
            correlation_id,
        )
        await deps.mark_interrupted_and_reset(feature_id, correlation_id)
        # Hand to dispatch_build directly (NOT handle_message) — we have
        # already validated identity against SQLite, so re-running the
        # path/originator allowlist would be redundant. Use the same
        # idempotent ack callback shape as the normal handler so the
        # state machine acks on terminal transition.
        ack_callback = _build_ack_callback(msg)
        await deps.consumer_deps.dispatch_build(payload, ack_callback)
        report.restarted_in_flight += 1
        return

    # --- Branch 4: INTERRUPTED → retry from scratch, working tree reused -
    # Named, not fallen into. This is the state a build is left in by the
    # boot recovery pass (``forge.lifecycle.recovery``, which moves every
    # RUNNING / PREPARING / FINALISING row to INTERRUPTED and stops there)
    # and by a crash between the two writes of
    # ``mark_interrupted_and_reset``. Before this branch such a redelivery
    # reached the "unexpected state" warning below and was dispatched as a
    # fresh build — the right ACT by the wrong ROUTE, with a WARNING in the
    # log saying the module did not recognise its own state.
    #
    # The row is deliberately NOT reset to PREPARING first. ``dispatch_build``
    # owns the advance: its runless re-dispatch arm re-enters a QUEUED or
    # INTERRUPTED row through the pre-dispatch approval gate, which drives
    # INTERRUPTED → PREPARING → RUNNING itself. A row moved to PREPARING here
    # would land on that arm's held-slot branch instead — no card, no launch,
    # and the slot held until the JetStream ack_wait expiry.
    #
    # What the dispatch then does for a FIX JOURNEY (conductor rewire rule
    # 5) is the reason for naming the branch: the gate publishes ONE fresh
    # build-gate card for Rich, and on his approval the conductor's worktree
    # writer finds the journey's OWN tree already registered on its own
    # branch and REUSES it (``reused=True``) rather than materialising a
    # second one. Retry-from-scratch here means the turns start again from
    # turn one, not that the tree is thrown away — a state-preserving resume
    # would need a turn cursor the driver does not have and is not built
    # (ADR-ARCH-028, 2026-09-05 amendment).
    if state == INTERRUPTED_BUILD_STATE:
        logger.info(
            "reconcile_on_boot: build feature_id=%s correlation_id=%s was "
            "left INTERRUPTED by the previous process; starting it again "
            "from the beginning and reusing the working tree it already has",
            feature_id,
            correlation_id,
        )
        ack_callback = _build_ack_callback(msg)
        await deps.consumer_deps.dispatch_build(payload, ack_callback)
        report.restarted_interrupted += 1
        return

    # Defensive: unexpected state. This should never fire — the SQLite
    # column is constrained to the values above — but if a future
    # migration adds a state without updating this module, we don't
    # want to silently swallow the redelivery. Logging at WARNING and
    # falling back to fresh dispatch keeps the build moving while
    # surfacing the inconsistency to operators.
    logger.warning(
        "reconcile_on_boot: unexpected state=%r feature_id=%s "
        "correlation_id=%s; falling back to fresh dispatch",
        state,
        feature_id,
        correlation_id,
    )
    report.fresh_builds += 1
    await handle_message(msg, deps.consumer_deps)


__all__ = [
    "ACK_WAIT_SECONDS",
    "BUILD_FAILED_SUBJECT_PREFIX",
    "BUILD_QUEUE_SUBJECT",
    "BuildAckHandle",
    "DURABLE_NAME",
    "FORGE_SOURCE_ID",
    "INTERRUPTED_BUILD_STATE",
    "IN_FLIGHT_BUILD_STATES",
    "InFlightAckRegistry",
    "PAUSED_BUILD_STATE",
    "PausedBuildSnapshot",
    "PipelineConsumerDeps",
    "REASON_MALFORMED_PAYLOAD",
    "REASON_ORIGINATOR_NOT_RECOGNISED",
    "REASON_PATH_OUTSIDE_ALLOWLIST",
    "RESTART_FROM_PREPARING_STATES",
    "ReconcileDeps",
    "ReconcileReport",
    "RegisterObserver",
    "STREAM_NAME",
    "TERMINAL_BUILD_STATES",
    "UNKNOWN_FEATURE_ID",
    "build_consumer_config",
    "handle_message",
    "reconcile_on_boot",
    "start_pipeline_consumer",
]

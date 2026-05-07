"""Inbound approval subscriber with short-TTL dedup buffer (TASK-CGCP-007).

This module owns Forge's inbound subscription on the ``.response`` mirror
subject for an approval-paused build per
``API-nats-approval-protocol.md §5`` and the **idempotency contract** per
§6 (ASSUM-006 high). It is the consumer-side counterpart to:

* TASK-CGCP-006 — :class:`ApprovalPublisher` (outbound request publish).
* TASK-CGCP-008 — :class:`SyntheticResponseInjector` (CLI cancel/skip).

Responsibilities (mirrors the AC list of TASK-CGCP-007):

* Subscribe to ``agents.approval.forge.{build_id}.response``
  (project-scoped via :meth:`nats_core.topics.Topics.for_project` if a
  project is configured).
* Validate every inbound :class:`MessageEnvelope` and inner
  :class:`ApprovalResponsePayload`.
* Maintain a **per-subscriber-instance, short-TTL dedup set** keyed on
  the literal ``request_id`` echoed back by the responder — never
  re-derive (the responder's echoed value is the wire contract).
* First-response-wins: subsequent responses with the same ``request_id``
  are observed but discarded so the caller's wait loop does not resume
  the build twice (closes risk **R4** — concurrent-response race).
* Refuse responses whose ``decision`` is outside the schema's allowed
  ``Literal["approve", "reject", "defer", "override"]`` (Group C
  ``@negative``); the rejection is logged but pause is *not* cancelled
  so a correctly-formed response can still be sent.
* Refuse responses whose ``decided_by`` (design-name ``responder``) is
  not the deployment's configured expected approver (Group E
  ``@security``); the build keeps waiting.
* Refresh-on-timeout per ``API §7``: when ``default_wait_seconds``
  elapses without a response, increment ``attempt_count`` and publish a
  fresh :class:`ApprovalRequestPayload` via the injected
  :data:`PublishRefreshCallback`. The prior ``request_id`` remains
  valid for dedup until the short TTL elapses.
* Total wait bounded by :attr:`ApprovalConfig.max_wait_seconds`.

Clock injection is mandatory — the dedup TTL never reads the wall clock
directly. Tests substitute :class:`Clock` instances that advance time
deterministically.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Final,
    Protocol,
    runtime_checkable,
)

from nats_core.envelope import MessageEnvelope
from nats_core.events import ApprovalResponsePayload
from nats_core.topics import Topics
from pydantic import ValidationError

from forge.config.models import ApprovalConfig
from forge.gating.identity import derive_request_id

if TYPE_CHECKING:  # pragma: no cover - import-time only
    from forge.pipeline import BuildContext, PipelineLifecycleEmitter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants pinned to the API contract
# ---------------------------------------------------------------------------

#: Subject prefix matching ``API-nats-approval-protocol.md §2``. The
#: per-build mirror subject is built by appending ``.{build_id}.response``.
APPROVAL_SUBJECT_PREFIX: Final[str] = "agents.approval.forge"

#: Default short TTL for the dedup buffer (seconds). Pinned to the value
#: cited in TASK-CGCP-007 AC-003 ("e.g. 300s"). Operators can override via
#: :class:`ApprovalSubscriberDeps`.
DEFAULT_DEDUP_TTL_SECONDS: Final[int] = 300

#: ``source_id`` stamped on every envelope Forge emits via this
#: subscriber's refresh-on-timeout path. Mirrors the publisher.
SOURCE_ID: Final[str] = "forge"


# ---------------------------------------------------------------------------
# Type aliases & protocols
# ---------------------------------------------------------------------------

PublishRefreshCallback = Callable[[str, str, int], Awaitable[None]]
"""``async (build_id, stage_label, attempt_count) -> None`` — publishes a
refreshed :class:`ApprovalRequestPayload` on
``agents.approval.forge.{build_id}`` per ``API §7``. The subscriber owns
neither envelope construction nor the publisher state, so this callback
delegates to the :class:`ApprovalPublisher` (TASK-CGCP-006)."""


BridgeRegistryLookup = Callable[[str, str], bool]
"""``(feature_id, correlation_id) -> bool`` — returns ``True`` when the
lifecycle bridge has an active registry entry for this build.

TASK-FRR-PEB-006: when the bridge is wired into ``forge serve``, the
bridge's SSE-translator owns the canonical ``pipeline.build-resumed``
emit. The subscriber MUST defer to the bridge to avoid two competing
emit sites; this lookup is the "is the bridge wired and tracking this
build?" probe.

Production wiring binds this callback to a thin wrapper around
:meth:`forge.persistence.repositories.bridge_registry.BridgeRegistry.get`
that returns ``True`` iff the registry returns a non-``None`` entry.
Tests pass a fake callable to drive both the bridge-wired and
bridge-absent paths.

The callable is **synchronous** by design: the registry is a SQLite
read on the same process; the call site is in the subscriber's hot path
where ``await``ing a sync I/O bound op would only add overhead. A
production implementation that needs to defer the lookup off the event
loop should wrap the callable in :func:`asyncio.to_thread` itself."""


@runtime_checkable
class Clock(Protocol):
    """Monotonic clock injected for deterministic dedup TTL.

    Per task brief: *no* ``datetime.now()`` in the dedup TTL logic. Tests
    substitute a fake clock that returns a controllable monotonic value
    so TTL eviction can be exercised without ``asyncio.sleep``.
    """

    def monotonic(self) -> float:  # pragma: no cover - protocol stub
        ...


class _MonotonicClock:
    """Default :class:`Clock` backed by :func:`time.monotonic`."""

    def monotonic(self) -> float:
        return time.monotonic()


@runtime_checkable
class _SubscriptionLike(Protocol):
    """Slice of :class:`nats.aio.subscription.Subscription` we depend on."""

    async def unsubscribe(self) -> None:  # pragma: no cover - protocol stub
        ...


@runtime_checkable
class _NATSSubscribeClient(Protocol):
    """Subset of ``nats_core.client.NATSClient.subscribe`` we depend on.

    Mirrors the signature of ``forge.adapters.nats.fleet_watcher`` —
    subscribe receives an envelope-aware callback. The client is
    expected to validate the :class:`MessageEnvelope` itself; we only
    validate the inner payload.
    """

    async def subscribe(
        self,
        topic: str,
        callback: Callable[[MessageEnvelope], Awaitable[None]],
    ) -> _SubscriptionLike:  # pragma: no cover - protocol stub
        ...


# ---------------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------------


class InvalidDecisionError(Exception):
    """Raised internally when a response carries an unrecognised decision.

    Group C ``@negative`` "Unrecognised decision value": Pydantic's
    ``Literal`` validator rejects anything outside the allowed decision
    set; the subscriber surfaces the rejection as this typed error so
    log scrapers can distinguish it from generic envelope-shape errors.
    The pause is **not** cancelled — a correctly-formed response can
    still arrive and resume the build.
    """

    def __init__(
        self,
        *,
        request_id: str | None,
        raw_decision: Any,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            f"Approval response for request_id={request_id!r} carries "
            f"unrecognised decision={raw_decision!r}; refusing"
        )
        self.request_id = request_id
        self.raw_decision = raw_decision
        self.cause = cause


# ---------------------------------------------------------------------------
# Dependency container
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ApprovalSubscriberDeps:
    """Injected collaborators for :class:`ApprovalSubscriber`.

    Kept on a dataclass so the subscriber can be unit-tested with simple
    fakes / :class:`unittest.mock.AsyncMock` instances. Production
    wiring binds ``nats_client`` to the shared ``NATSClient`` and
    ``publish_refresh`` to :class:`ApprovalPublisher` (TASK-CGCP-006).

    Args:
        nats_client: Async NATS client exposing
            :meth:`_NATSSubscribeClient.subscribe`. Injected at the
            application boundary; tests substitute a fake.
        config: Loaded :class:`ApprovalConfig` (TASK-CGCP-002). Provides
            ``default_wait_seconds`` (per-attempt wait) and
            ``max_wait_seconds`` (total wait ceiling per ASSUM-002).
        publish_refresh: Optional callback invoked on per-attempt
            timeout to publish a refreshed approval request with an
            incremented ``attempt_count``. ``None`` disables refresh —
            the subscriber simply returns ``None`` after the first
            timeout, which is useful for unit tests.
        expected_approver: Deployment's expected ``decided_by`` value.
            ``None`` → permissive mode (single-deployment dev). When
            set, only responses whose ``decided_by`` matches this
            string can resume the build (Group E ``@security``).
        project: Optional NATS multi-tenancy project scope. ``None`` →
            no scoping; otherwise the subject is wrapped via
            :meth:`Topics.for_project`.
        clock: Monotonic clock used for dedup TTL eviction. Defaults to
            :class:`_MonotonicClock`; tests inject a fake.
        dedup_ttl_seconds: Short TTL on the dedup buffer (seconds).
            Defaults to :data:`DEFAULT_DEDUP_TTL_SECONDS`.
        bridge_registry_lookup: Optional :data:`BridgeRegistryLookup`
            probe consulted before publishing ``build-resumed``. When
            the callable returns ``True`` the subscriber **skips** its
            own emit and logs at INFO that the bridge is canonical
            (TASK-FRR-PEB-006 AC-2). ``None`` (the default) preserves
            FW10-010 behaviour: the subscriber emits ``build-resumed``
            itself (AC-3 — backward compatibility for tests / deploys
            that do not wire the bridge).
    """

    nats_client: Any
    config: ApprovalConfig
    publish_refresh: PublishRefreshCallback | None = None
    expected_approver: str | None = None
    project: str | None = None
    clock: Clock = field(default_factory=_MonotonicClock)
    dedup_ttl_seconds: int = DEFAULT_DEDUP_TTL_SECONDS
    bridge_registry_lookup: BridgeRegistryLookup | None = None


# ---------------------------------------------------------------------------
# Subscriber
# ---------------------------------------------------------------------------


class ApprovalSubscriber:
    """Subscribe to approval responses and resolve a single decision per build.

    The subscriber is **stateful**: it owns a per-instance dedup buffer
    and a per-build queue of pending responses. A single instance can
    serve multiple builds concurrently via independent
    :meth:`await_response` calls — the dedup buffer is shared so two
    builds whose ``request_id`` collided (theoretically impossible
    under :func:`derive_request_id` but defended-in-depth) would still
    yield first-response-wins semantics.

    See module docstring for the full responsibility list.
    """

    def __init__(self, deps: ApprovalSubscriberDeps) -> None:
        self._deps = deps
        # Dedup buffer: request_id -> deadline (monotonic seconds).
        # OrderedDict keeps insertion order, which matches expiry order
        # because every entry has the same TTL — so head-of-dict is
        # also the oldest.
        self._dedup: OrderedDict[str, float] = OrderedDict()
        # asyncio.Lock guards the dedup buffer — closes risk R4
        # (concurrent-response race). Without the lock, two awaited
        # ``_on_envelope`` callbacks could both observe ``request_id
        # not in self._dedup`` and both push to the queue, breaking
        # first-response-wins.
        self._dedup_lock = asyncio.Lock()
        # Per-build queues — a response only resumes a build whose
        # ``await_response`` is currently active. Responses arriving
        # for a different build (or after the wait loop exited) are
        # logged and dropped.
        self._queues: dict[str, asyncio.Queue[ApprovalResponsePayload]] = {}
        # Per-build resume-publish context (TASK-FW10-010). Populated by
        # :meth:`await_response` when the caller threads a
        # ``lifecycle_emitter`` + ``build_context`` so ``_on_envelope``
        # can publish ``pipeline.build-resumed.<feature_id>`` BEFORE
        # the orchestrator advances. Each entry is dropped on
        # ``await_response`` exit, mirroring the per-build queue.
        self._resume_publish_ctx: dict[
            str,
            tuple[
                "PipelineLifecycleEmitter",
                "BuildContext",
                str | None,  # expected correlation_id
                str,  # stage_label
            ],
        ] = {}

    # ------------------------------------------------------------------
    # Subject helper
    # ------------------------------------------------------------------

    @staticmethod
    def _subject_for(build_id: str, project: str | None) -> str:
        """Return the response mirror subject for ``build_id``.

        Args:
            build_id: Identifier of the paused build. Must be non-empty.
            project: Optional project scope; ``None`` → unscoped.

        Returns:
            ``agents.approval.forge.{build_id}.response`` or its
            project-scoped equivalent.

        Raises:
            ValueError: If ``build_id`` is empty.
        """
        if not build_id:
            raise ValueError("build_id must be a non-empty string")
        base = f"{APPROVAL_SUBJECT_PREFIX}.{build_id}.response"
        if project is not None:
            return Topics.for_project(project, base)
        return base

    # ------------------------------------------------------------------
    # Dedup buffer — atomic check-and-record under asyncio.Lock
    # ------------------------------------------------------------------

    async def _check_and_record(self, request_id: str) -> bool:
        """Check whether ``request_id`` is a duplicate; record if not.

        Atomic under :attr:`_dedup_lock` — closes risk **R4**
        (concurrent-response race per AC-004). Two coroutines awaited
        from concurrent ``_on_envelope`` invocations cannot both observe
        ``request_id not in self._dedup`` and both record it as
        first-response.

        Returns:
            ``True`` if ``request_id`` was already present (duplicate),
            ``False`` if newly recorded (first-arrival).
        """
        async with self._dedup_lock:
            now = self._deps.clock.monotonic()
            self._evict_expired(now)
            if request_id in self._dedup:
                return True
            self._dedup[request_id] = now + self._deps.dedup_ttl_seconds
            return False

    def _evict_expired(self, now: float) -> None:
        """Pop dedup entries whose deadline is at or before ``now``.

        Called under :attr:`_dedup_lock` from :meth:`_check_and_record`.
        Iteration over ``self._dedup.items()`` while popping is unsafe;
        we materialise the offenders into a list first.
        """
        expired = [rid for rid, deadline in self._dedup.items() if deadline <= now]
        for rid in expired:
            self._dedup.pop(rid, None)

    # ------------------------------------------------------------------
    # Public API: await a single decision for ``build_id``
    # ------------------------------------------------------------------

    async def await_response(
        self,
        build_id: str,
        *,
        stage_label: str,
        attempt_count: int = 0,
        timeout_seconds: int | None = None,
        lifecycle_emitter: "PipelineLifecycleEmitter | None" = None,
        build_context: "BuildContext | None" = None,
        expected_correlation_id: str | None = None,
    ) -> ApprovalResponsePayload | None:
        """Subscribe and await the first-arrival approval response.

        Implements the AC-001 method signature plus the refresh-on-
        timeout loop required by AC-010 (``API §7``). The extra
        ``stage_label`` and ``attempt_count`` keyword-only arguments
        are required to derive a fresh ``request_id`` on refresh —
        they cannot be defaulted because :func:`derive_request_id`
        is pure and needs both inputs.

        Args:
            build_id: Identifier of the paused build whose response is
                being awaited. Must be non-empty.
            stage_label: Pipeline stage label the build is paused at.
                Combined with ``build_id`` and ``attempt_count`` it
                feeds :func:`derive_request_id` on refresh.
            attempt_count: Initial attempt counter; the first refresh
                publishes ``attempt_count + 1``. Must be non-negative.
            timeout_seconds: Per-attempt wait. ``None`` falls back to
                :attr:`ApprovalConfig.default_wait_seconds`. Total
                wait is independently bounded by
                :attr:`ApprovalConfig.max_wait_seconds`.

        Returns:
            The validated :class:`ApprovalResponsePayload` of the
            first-arrival, allowed-responder, allowed-decision response;
            or ``None`` on:
              * timeout (total wait reached
                ``ApprovalConfig.max_wait_seconds`` with no response),
              * duplicate-only path (every observed response was a
                duplicate or refused — but the wait loop continues
                inside, so this only happens via the timeout path).

        Raises:
            ValueError: If ``build_id`` or ``stage_label`` is empty, or
                ``attempt_count`` is negative.
        """
        if not build_id:
            raise ValueError("build_id must be a non-empty string")
        if not stage_label:
            raise ValueError("stage_label must be a non-empty string")
        if attempt_count < 0:
            raise ValueError(
                f"attempt_count must be non-negative, got {attempt_count!r}"
            )

        per_attempt = (
            timeout_seconds
            if timeout_seconds is not None
            else self._deps.config.default_wait_seconds
        )
        max_total = self._deps.config.max_wait_seconds

        subject = self._subject_for(build_id, self._deps.project)

        # Per-build queue is created fresh per await_response so two
        # concurrent waits on the same build_id do not share state. In
        # practice the state machine guarantees a single in-flight wait
        # per build, but this keeps the contract robust.
        queue: asyncio.Queue[ApprovalResponsePayload] = asyncio.Queue()
        self._queues[build_id] = queue

        # TASK-FW10-010: register resume-publish context so the inbound
        # handler can publish ``pipeline.build-resumed.<feature_id>``
        # the moment a matching response arrives — strictly *before*
        # the orchestrator advances to the next stage. The context is
        # cleared on the ``finally`` block below; only one concurrent
        # await_response per build_id is supported (the state machine
        # enforces this — ASSUM-002).
        if lifecycle_emitter is not None and build_context is not None:
            self._resume_publish_ctx[build_id] = (
                lifecycle_emitter,
                build_context,
                expected_correlation_id,
                stage_label,
            )

        clock = self._deps.clock
        start = clock.monotonic()
        current_attempt = attempt_count

        async def _callback(envelope: MessageEnvelope) -> None:
            await self._on_envelope(build_id=build_id, envelope=envelope)

        sub = await self._deps.nats_client.subscribe(subject, _callback)

        try:
            while True:
                elapsed = clock.monotonic() - start
                remaining = float(max_total) - elapsed
                if remaining <= 0:
                    logger.info(
                        "approval_subscriber: max_wait reached build_id=%s "
                        "stage=%s attempt=%d — returning None",
                        build_id,
                        stage_label,
                        current_attempt,
                    )
                    return None

                wait = min(float(per_attempt), remaining)
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=wait)
                except asyncio.TimeoutError:
                    payload = None

                if payload is not None:
                    logger.debug(
                        "approval_subscriber: resolved build_id=%s request_id=%s "
                        "decision=%s",
                        build_id,
                        payload.request_id,
                        payload.decision,
                    )
                    return payload

                # Per-attempt timeout — refresh per API §7 if a publisher
                # is wired and we have remaining budget. A new
                # request_id is derived for the new attempt; the prior
                # request_id remains in the dedup buffer until its
                # short TTL elapses, which is what closes the
                # late-real-response race.
                current_attempt += 1
                new_request_id = derive_request_id(
                    build_id=build_id,
                    stage_label=stage_label,
                    attempt_count=current_attempt,
                )
                if self._deps.publish_refresh is not None:
                    try:
                        await self._deps.publish_refresh(
                            build_id, stage_label, current_attempt
                        )
                        logger.info(
                            "approval_subscriber: refresh published "
                            "build_id=%s stage=%s attempt=%d request_id=%s",
                            build_id,
                            stage_label,
                            current_attempt,
                            new_request_id,
                        )
                    except Exception as exc:  # noqa: BLE001
                        # A failed refresh publish must not kill the
                        # wait loop — we log and keep waiting; the
                        # responder may still answer the previous
                        # request_id, and the next per-attempt cycle
                        # will retry the refresh.
                        logger.warning(
                            "approval_subscriber: refresh publish failed "
                            "build_id=%s stage=%s attempt=%d error=%s",
                            build_id,
                            stage_label,
                            current_attempt,
                            exc,
                        )
                else:
                    # No publisher wired — single-shot wait. Returning
                    # None here matches the contract: caller decides.
                    logger.debug(
                        "approval_subscriber: per-attempt timeout with no "
                        "refresh publisher build_id=%s — returning None",
                        build_id,
                    )
                    return None
        finally:
            self._queues.pop(build_id, None)
            self._resume_publish_ctx.pop(build_id, None)
            try:
                await sub.unsubscribe()
            except Exception as exc:  # noqa: BLE001
                # Subscription cleanup must never raise out of the
                # caller's wait loop — an already-closed connection or
                # a transport hiccup shouldn't escape.
                logger.debug(
                    "approval_subscriber: unsubscribe error build_id=%s err=%s",
                    build_id,
                    exc,
                )

    # ------------------------------------------------------------------
    # Inbound message handler
    # ------------------------------------------------------------------

    async def _on_envelope(
        self,
        *,
        build_id: str,
        envelope: MessageEnvelope,
    ) -> None:
        """Validate, dedup, and route one inbound envelope.

        Outcome ordering (mutually exclusive — at most one fires):

        1. *Invalid payload (e.g. unrecognised decision)* → logged and
           dropped (Group C ``@negative``). Pause is *not* cancelled.
        2. *Unrecognised responder* → logged as anomaly and dropped
           (Group E ``@security``). Pause is *not* cancelled.
        3. *Duplicate ``request_id``* → logged and dropped
           (Group D ``@edge-case``). Pause is *not* cancelled.
        4. *First-arrival, valid, allowed* → enqueued for the
           ``await_response`` loop to dequeue and return.

        The function never raises — every validation failure is
        translated into a log + drop. ``asyncio.CancelledError`` is
        propagated so the calling subscribe loop's shutdown semantics
        are preserved.
        """
        # --- 1. Inner-payload validation (Group C @negative) ---------
        raw_request_id: Any = None
        raw_decision: Any = None
        if isinstance(envelope.payload, dict):
            raw_request_id = envelope.payload.get("request_id")
            raw_decision = envelope.payload.get("decision")

        try:
            payload = ApprovalResponsePayload.model_validate(envelope.payload)
        except (ValidationError, ValueError, TypeError) as exc:
            err = InvalidDecisionError(
                request_id=(
                    raw_request_id if isinstance(raw_request_id, str) else None
                ),
                raw_decision=raw_decision,
                cause=exc,
            )
            logger.warning(
                "approval_subscriber: invalid payload (decision=%r) "
                "build_id=%s request_id=%r — refused, pause continues (%s)",
                raw_decision,
                build_id,
                raw_request_id,
                err,
            )
            return

        # --- 2. Responder allowlist (Group E @security) --------------
        # Run the responder check BEFORE dedup so that an attacker
        # cannot poison the dedup buffer for a legitimate request_id
        # by impersonating the responder identity.
        expected = self._deps.expected_approver
        if expected is not None and payload.decided_by != expected:
            logger.warning(
                "approval_subscriber: unrecognised responder %r "
                "(expected %r) build_id=%s request_id=%s — anomaly, "
                "NOT resuming",
                payload.decided_by,
                expected,
                build_id,
                payload.request_id,
            )
            return

        # --- 2b. Correlation-id allowlist (Group E @security, DDR-001) --
        # TASK-FW10-010: when the caller threaded an
        # ``expected_correlation_id`` via :meth:`await_response`, refuse
        # responses whose envelope correlation_id mismatches. This
        # closes the per-build correlation guard cited in DDR-001 /
        # ASSUM-016: even on the per-build response subject, an attacker
        # who knows ``build_id`` cannot inject an approval for a
        # different build's correlation context.
        publish_ctx = self._resume_publish_ctx.get(build_id)
        if publish_ctx is not None:
            _emitter, _ctx, expected_corr, _stage = publish_ctx
            envelope_corr = getattr(envelope, "correlation_id", None)
            if (
                expected_corr is not None
                and envelope_corr is not None
                and envelope_corr != expected_corr
            ):
                logger.warning(
                    "approval_subscriber: correlation_id mismatch "
                    "(envelope=%r expected=%r) build_id=%s request_id=%s "
                    "— anomaly, NOT resuming (DDR-001 / ASSUM-016)",
                    envelope_corr,
                    expected_corr,
                    build_id,
                    payload.request_id,
                )
                return

        # --- 3. Dedup — atomic check-and-record (R4) -----------------
        is_duplicate = await self._check_and_record(payload.request_id)
        if is_duplicate:
            logger.info(
                "approval_subscriber: duplicate response build_id=%s "
                "request_id=%s decision=%s — discarded",
                build_id,
                payload.request_id,
                payload.decision,
            )
            return

        # --- 4. First-arrival → publish build-resumed + enqueue ------
        queue = self._queues.get(build_id)
        if queue is None:
            # No active wait loop for this build. This is normal in two
            # cases: (a) per-build response routing means a response on
            # a different build's mirror reached us through some
            # over-broad subscription (defence-in-depth); (b) the wait
            # loop already exited (timeout, cancel) but the
            # subscription is still draining. Either way: log + drop.
            logger.debug(
                "approval_subscriber: response with no active waiter "
                "build_id=%s request_id=%s — dropping",
                build_id,
                payload.request_id,
            )
            return

        # TASK-FW10-010 AC: publish ``pipeline.build-resumed.<feature_id>``
        # BEFORE the orchestrator advances. The order is part of the
        # contract — observers must see the resume on the wire before
        # the gate's wait loop returns and the state machine runs the
        # PAUSED → RUNNING transition. Failures are logged at WARNING
        # and swallowed (DDR-007 §Failure-mode contract, ADR-ARCH-008):
        # SQLite remains authoritative; the build does not regress on
        # a transient publish hiccup.
        #
        # TASK-FRR-PEB-006 AC-2/AC-3: When the lifecycle bridge is wired
        # AND has an active registry entry for this build, the bridge's
        # SSE-translator is the canonical ``build-resumed`` emit site
        # (Q4 sub-option (a) — single-emit-site canonicalisation). The
        # subscriber detects this via :data:`BridgeRegistryLookup` and
        # **skips** its own emit when the bridge is active. When the
        # bridge is not wired (no lookup configured, or the lookup
        # returns ``False``), the subscriber's emit fires — preserving
        # FW10-010's behaviour for tests/deploys that don't run the
        # bridge.
        if publish_ctx is not None:
            emitter, ctx, _expected_corr, stage_label = publish_ctx
            if self._bridge_owns_resume_for(
                feature_id=ctx.feature_id,
                correlation_id=ctx.correlation_id,
                build_id=build_id,
                request_id=payload.request_id,
            ):
                # AC-2: bridge is canonical — skip the subscriber's
                # emit. The queue.put below still wakes the
                # orchestrator's wait loop so the PAUSED → RUNNING
                # transition proceeds; the bridge's translator emits
                # the wire envelope when its SSE stream observes the
                # ``awaiting_approval → running_wave`` transition.
                logger.info(
                    "approval_subscriber: bridge canonical for "
                    "feature_id=%s correlation_id=%s build_id=%s "
                    "request_id=%s — skipping subscriber's "
                    "build-resumed emit (TASK-FRR-PEB-006)",
                    ctx.feature_id,
                    ctx.correlation_id,
                    build_id,
                    payload.request_id,
                )
            else:
                try:
                    await emitter.emit_resumed(
                        ctx,
                        stage_label=stage_label,
                        decision=payload.decision,
                        responder=payload.decided_by,
                        resumed_at=datetime.now(timezone.utc).isoformat(),
                    )
                    logger.info(
                        "approval_subscriber: published build-resumed "
                        "build_id=%s feature_id=%s request_id=%s decision=%s",
                        build_id,
                        ctx.feature_id,
                        payload.request_id,
                        payload.decision,
                    )
                except Exception as exc:  # noqa: BLE001 — DDR-007 swallow+log
                    logger.warning(
                        "approval_subscriber: emit_resumed failed "
                        "build_id=%s request_id=%s err=%s "
                        "— SQLite remains authoritative (ADR-ARCH-008)",
                        build_id,
                        payload.request_id,
                        exc,
                    )

        await queue.put(payload)

    # ------------------------------------------------------------------
    # Bridge-canonical detection (TASK-FRR-PEB-006)
    # ------------------------------------------------------------------

    def _bridge_owns_resume_for(
        self,
        *,
        feature_id: str,
        correlation_id: str,
        build_id: str,
        request_id: str,
    ) -> bool:
        """Return ``True`` when the bridge owns the canonical resume emit.

        AC-2: queries :attr:`ApprovalSubscriberDeps.bridge_registry_lookup`
        for the ``(feature_id, correlation_id)`` pair. A truthy result
        means the bridge has an active registry entry for this build
        and will emit ``pipeline.build-resumed`` itself when the SSE
        stream's ``awaiting_approval → running_wave`` transition is
        observed.

        Failures of the lookup callable are logged at WARNING and
        treated as **bridge-absent** — the subscriber's emit then fires
        as a defence-in-depth fallback. Swallowing is deliberate: a
        transient SQLite hiccup must not silently drop the resume
        envelope; the subscriber is the older, more battle-tested emit
        site and is the safer fallback.
        """
        lookup = self._deps.bridge_registry_lookup
        if lookup is None:
            # AC-3: no bridge wired — subscriber's emit fires.
            return False
        try:
            return bool(lookup(feature_id, correlation_id))
        except Exception as exc:  # noqa: BLE001 — defence-in-depth
            logger.warning(
                "approval_subscriber: bridge_registry_lookup raised "
                "(%s) for feature_id=%s correlation_id=%s build_id=%s "
                "request_id=%s — falling back to subscriber emit "
                "(TASK-FRR-PEB-006)",
                exc,
                feature_id,
                correlation_id,
                build_id,
                request_id,
            )
            return False


__all__ = [
    "APPROVAL_SUBJECT_PREFIX",
    "DEFAULT_DEDUP_TTL_SECONDS",
    "SOURCE_ID",
    "ApprovalSubscriber",
    "ApprovalSubscriberDeps",
    "BridgeRegistryLookup",
    "Clock",
    "InvalidDecisionError",
    "PublishRefreshCallback",
]

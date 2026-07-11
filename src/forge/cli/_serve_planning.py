"""Mode P planning composition and recovery (TASK-MP-009 / TASK-MP-012).

This module is the composition root for Mode P — the FIRST production
composition of ``DispatchOrchestrator`` + ``NatsSpecialistDispatchAdapter``.
It wires, behind ``planning.enabled``:

1. **Boot audit** (DF-004): planning model configuration has no fallbacks.
2. **Dispatch stack**: DiscoveryCache (fed by the fleet watcher) →
   CorrelationRegistry ↔ NatsSpecialistDispatchAdapter (the adapter is the
   registry's ReplyChannel transport, via the late-bound proxy) →
   TimeoutCoordinator → DispatchOrchestrator → dispatch_specialist_stage
   (PRODUCT_OWNER).
3. **Approval side**: ApprovalPublisher wrapped in a pause-mirroring
   publisher (AGENTS approval request FIRST, PIPELINE build-paused SECOND
   — the jarvis JNB-103 join contract), plus a per-run ApprovalSubscriber
   factory over an envelope-parsing client adapter (per-run
   ``expected_approver`` pinning, RT-04).
4. **Planning consumer**: durable ``forge-serve-planning`` pull consumer on
   the PIPELINE stream filtering ``pipeline.planning-queued.*`` with
   ``ack_wait=3600`` (the TASK-GATE-D659 lesson — never the 30s default),
   driving :func:`handle_planning_message` and kicking the chain driver
   after each persisted intake.
5. **Recovery functions**:
   - :func:`rearm_paused_planning_runs`: re-arms PAUSED runs after restart
     (arm-before-post, verbatim persisted request_id).
   - :func:`sweep_interrupted_planning_runs`: re-drives QUEUED / RUNNING
     runs through the re-entrant driver (RT-05 / RT-08).

Architecture
------------

DDR-007 soft-fail posture throughout: planning composition failure never
bricks daemon boot; every background task is supervised so an unhandled
exception is logged loudly (the affected run is recovered at next boot).

References: TASK-MP-009, TASK-MP-012, FEAT-SPL-002, DF-004, RT-05, DDR-007.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from forge.adapters.nats.envelope_subscribe import EnvelopeSubscribeClient
from forge.adapters.nats.planning_consumer import (
    PLANNING_DURABLE_NAME,
    PLANNING_QUEUED_SUBJECT_FILTER,
    PlanningConsumerDeps,
    handle_planning_message,
)
from forge.adapters.sqlite import connect_writer
from forge.planning.audit import audit_planning_model_resolution
from forge.planning.gate_adapters import build_planning_gate_adapters
from forge.planning.notifications import build_planning_notification_envelope
from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.states import PlanningState

if TYPE_CHECKING:  # pragma: no cover - typing only
    from forge.config.models import ForgeConfig, PlanningConfig
    from forge.planning.driver import PlanningRunDriver

logger = logging.getLogger(__name__)

__all__ = [
    "PLANNING_DURABLE_NAME",
    "PLANNING_QUEUED_SUBJECT_FILTER",
    "compose_planning_consumer_and_dispatch",
    "make_drive_spawner",
    "rearm_paused_planning_runs",
    "sweep_interrupted_planning_runs",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Upper bound (seconds) on how long boot sweep waits for response
#: subscription to arm before giving up. Mirrors _REARM_ARM_TIMEOUT_SECONDS
#: from _serve_gate_activation.py.
_REARM_ARM_TIMEOUT_SECONDS: float = 10.0

#: JetStream stream carrying planning-queued messages (same workqueue
#: stream as build intake; the subject filters are disjoint so no
#: err-10100 overlap is introduced).
PLANNING_STREAM_NAME: str = "PIPELINE"

#: Ack wait for the planning durable. MUST comfortably exceed intake
#: processing time — TASK-GATE-D659 proved the nats-py 30s default
#: redelivers under long waits; mirror the build durable's 1h.
PLANNING_ACK_WAIT_SECONDS: float = 3600.0

#: Pull-fetch batch size / timeout (mirrors _serve_daemon).
_PULL_BATCH_SIZE: int = 1
_PULL_TIMEOUT_SECONDS: float = 1.0

#: feature_id used on the pipeline.build-paused mirror for planning runs.
#: MUST match jarvis's ForgeNotification pattern ^FEAT-[A-Z0-9]{3,12}$ or
#: the pause never reaches Slack (jarvis WARN-drops the sink notification).
_PLANNING_PAUSE_FEATURE_ID: str = "FEAT-PLANNING"

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

DispatchCallable = Callable[..., Awaitable[Any]]
"""``async (correlation_id, ...) -> None`` — fire-and-forget chain re-drive."""


# ---------------------------------------------------------------------------
# Per-run drive spawner (per-cid mutual exclusion)
# ---------------------------------------------------------------------------


def make_drive_spawner(
    driver: Any,
    supervise: Callable[[Any, str], None],
) -> Callable[..., None]:
    """Build the per-correlation_id deduplicating drive spawner.

    Per-run mutual exclusion: sweep + rearm + intake (including
    non-terminal duplicate redelivery, TASK-MP-014) can each ask to
    drive the same correlation_id — a second concurrent driver would
    arm duplicate approval waiters and race the handoff (TASK-MP-012
    review finding). While a drive task for a correlation_id is live,
    further spawn requests for it are no-ops.

    Module-level (not a composition closure) so tests can exercise the
    real dedup against a real driver.

    Args:
        driver: The composed :class:`PlanningRunDriver`.
        supervise: ``(task, label) -> None`` background-task registrar.

    Returns:
        ``spawn(correlation_id, *, republish=False) -> None``
    """
    live_drives: dict[str, asyncio.Task[Any]] = {}

    def _spawn_drive(correlation_id: str, *, republish: bool = False) -> None:
        existing = live_drives.get(correlation_id)
        if existing is not None and not existing.done():
            logger.info(
                "planning composition: drive already live for %s; "
                "skipping duplicate spawn",
                correlation_id,
            )
            return
        task = asyncio.create_task(
            driver.drive(correlation_id, republish_pending=republish)
        )
        live_drives[correlation_id] = task
        task.add_done_callback(
            lambda t, cid=correlation_id: (
                live_drives.pop(cid, None) if live_drives.get(cid) is t else None
            )
        )
        supervise(task, f"drive:{correlation_id}")

    return _spawn_drive


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


class NatsClient:
    """Marker docstring — see the raw ``nats.aio.client.Client`` surface.

    The composition needs ``jetstream()``, ``subscribe(subject, cb=...)``
    and ``publish(subject, body)``. Kept as ``Any`` at call sites; tests
    inject in-memory fakes.
    """


# ---------------------------------------------------------------------------
# Composition result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanningCompositionResult:
    """Result of planning consumer + dispatch composition.

    Attributes:
        consumer_name: Durable consumer name for planning intake.
        subject_filter: NATS subject filter for planning-queued messages.
        dispatch_callable: Fire-and-forget re-drive — spawns a supervised
            :meth:`PlanningRunDriver.drive` task for a correlation_id.
            None when the DF-004 audit failed.
        audit_passed: True if DF-004 audit passed, False otherwise.
        driver: The composed :class:`PlanningRunDriver` (None on audit fail).
        store: The shared :class:`SqlitePlanningRunStore`.
        subscription: JetStream pull subscription handle (None when the
            client has no JetStream context — logged loudly).
        consumer_task: The intake fetch-loop task.
        background_tasks: Live driver/watcher task registry (supervision).
    """

    consumer_name: str
    subject_filter: str
    dispatch_callable: DispatchCallable | None
    audit_passed: bool
    driver: Any | None = None
    store: SqlitePlanningRunStore | None = None
    subscription: Any | None = None
    consumer_task: Any | None = None
    background_tasks: set[Any] | None = field(default=None)
    rearm_callable: DispatchCallable | None = None


# ---------------------------------------------------------------------------
# Collaborator adapters (composition-local)
# ---------------------------------------------------------------------------


class _LateBoundReplyChannel:
    """Break the registry↔adapter construction cycle.

    ``CorrelationRegistry`` requires its transport at construction while
    ``NatsSpecialistDispatchAdapter`` requires the registry — this proxy
    lets the registry be built first and the adapter bound after.
    """

    def __init__(self) -> None:
        self._inner: Any | None = None

    def bind_transport(self, inner: Any) -> None:
        self._inner = inner

    async def subscribe(self, correlation_key: str, deliver: Any) -> Any:
        if self._inner is None:  # pragma: no cover - programmer error
            raise RuntimeError("reply channel transport not bound")
        return await self._inner.subscribe(correlation_key, deliver)

    async def unsubscribe(self, subscription: Any) -> None:
        if self._inner is not None:
            await self._inner.unsubscribe(subscription)


class _RegistryWaitAdapter:
    """Adapt CorrelationRegistry to TimeoutCoordinator's narrower surface."""

    def __init__(self, registry: Any) -> None:
        self._registry = registry

    async def wait_for_reply(self, binding: Any) -> Any:
        # The coordinator owns the outer timeout; pin the inner wait open.
        return await self._registry.wait_for_reply(binding, timeout_seconds=1e9)

    def release(self, binding: Any) -> None:
        self._registry.release(binding)


class _NullStageLogReader:
    """PRODUCT_OWNER is the chain entry — no upstream artefacts exist."""

    def get_approved_stage_entry(self, *args: Any, **kwargs: Any) -> None:
        return None

    def get_all_approved_stage_entries(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []


class _DenyAllWorktreeAllowlist:
    """Never consulted for an empty context; deny-by-default if it is."""

    def is_allowed(self, *args: Any, **kwargs: Any) -> bool:
        return False


class _PlanningStageLogWriter:
    """StageLogWriter over planning_run_events.

    The build path's stage_log table FKs ``builds`` — planning runs have
    no builds row, so dispatch audit rows live in planning_run_events.
    """

    def __init__(self, store: SqlitePlanningRunStore) -> None:
        self._store = store
        self._entries: dict[str, str] = {}

    def record_dispatch_submit(
        self,
        *,
        build_id: str,
        stage: Any,
        feature_id: str | None,
        correlation_id: str,
        capability: str,
    ) -> str:
        cid = build_id[5:] if build_id.startswith("plan-") else build_id
        entry_id = self._store._record_event(
            correlation_id=cid,
            stage_label=str(getattr(stage, "value", stage)),
            status="dispatch_submitted",
            actor_identity="planning-dispatch",
            details_json=json.dumps(
                {
                    "capability": capability,
                    "dispatch_correlation_id": correlation_id,
                    "feature_id": feature_id,
                }
            ),
        )
        handle = str(entry_id)
        self._entries[handle] = cid
        return handle

    def record_dispatch_reply(
        self,
        *,
        entry_id: str,
        outcome: Any,
        coach_score: float | None,
        criterion_breakdown: Any,
        detection_findings: Any,
        reason: str | None,
    ) -> None:
        cid = self._entries.pop(entry_id, None)
        if cid is None:
            logger.warning(
                "planning stage log: reply for unknown entry_id=%s", entry_id
            )
            return
        self._store._record_event(
            correlation_id=cid,
            stage_label="planning-dispatch-reply",
            status=f"dispatch_{getattr(outcome, 'value', outcome)}",
            coach_score=coach_score,
            actor_identity="planning-dispatch",
            details_json=json.dumps(
                {"reason": reason, "submit_entry_id": entry_id}, default=str
            ),
        )


class _PlanningPausePublisher:
    """AGENTS approval request FIRST, PIPELINE build-paused SECOND.

    Mirrors ``_serve_gate_activation._MirroredApprovalPublisher``: jarvis
    only renders an approval button after joining the captured request to
    a ``build_paused`` lifecycle event on ``build_id`` — without the
    mirror the approval parks and TTL-expires with no Slack surface
    (post-merge review wire-topology finding). The mirror emit is
    best-effort (DDR-007).
    """

    def __init__(
        self,
        inner: Any,
        *,
        nats_client: Any,
        clock: Callable[[], datetime],
    ) -> None:
        self._inner = inner
        self._nc = nats_client
        self._clock = clock

    async def publish_request(self, envelope: Any) -> None:
        await self._inner.publish_request(envelope)

        try:
            from nats_core.envelope import EventType, MessageEnvelope
            from nats_core.events import BuildPausedPayload

            payload = envelope.payload if isinstance(envelope.payload, dict) else {}
            details = payload.get("details", {})
            if not isinstance(details, dict):  # pragma: no cover - defensive
                details = {}
            build_id = details.get("build_id", "")
            # jarvis projects build-paused onto ForgeNotification whose
            # feature_id is Field(pattern=r"^FEAT-[A-Z0-9]{3,12}$") — a
            # plan-{cid} value fails validation and the pause message is
            # silently WARN-dropped (TASK-MP-012 review finding). The join
            # to the parked approval is purely on build_id, so a fixed,
            # pattern-conformant feature_id is correct here.
            feature_id = _PLANNING_PAUSE_FEATURE_ID
            correlation_id = envelope.correlation_id or (
                build_id[5:] if build_id.startswith("plan-") else build_id
            )

            paused = BuildPausedPayload(
                feature_id=feature_id,
                build_id=build_id,
                stage_label=details.get("stage_label", "product_docs"),
                gate_mode=details.get("gate_mode", "MANDATORY_HUMAN_APPROVAL"),
                coach_score=details.get("coach_score"),
                rationale=details.get("rationale", ""),
                approval_subject=f"agents.approval.forge.{build_id}",
                paused_at=self._clock().isoformat(),
                correlation_id=correlation_id,
            )
            mirror = MessageEnvelope(
                source_id="forge",
                event_type=EventType.BUILD_PAUSED,
                correlation_id=correlation_id,
                payload=paused.model_dump(mode="json"),
            )
            await self._nc.publish(
                f"pipeline.build-paused.{feature_id}",
                mirror.model_dump_json().encode("utf-8"),
            )
        except Exception:  # noqa: BLE001 — mirror is best-effort (DDR-007)
            logger.exception(
                "planning pause mirror: build-paused emit failed (approval "
                "request already published; jarvis join may not render)"
            )


class _DisabledFrontierClient:
    """Placeholder client — never invoked while frontier_enabled=False."""

    async def get_opinion(self, brief: Any) -> dict[str, Any]:
        raise RuntimeError(
            "frontier client invoked while planning.frontier_enabled=False"
        )


def _latest_po_output(store: SqlitePlanningRunStore, correlation_id: str) -> dict:
    """Most recent recorded PO output for a run (empty when none)."""
    latest: dict[str, Any] = {}
    for event in store.list_events(correlation_id):
        if event["stage_label"] == "product_owner" and event["status"] == "approved":
            if event["details_json"]:
                try:
                    details = json.loads(event["details_json"])
                    latest = details.get("po_output", {}) or {}
                except (json.JSONDecodeError, ValueError):
                    continue
    return latest


# ---------------------------------------------------------------------------
# Main composition function
# ---------------------------------------------------------------------------


async def compose_planning_consumer_and_dispatch(
    *,
    db_path: Path,
    nats_client: Any,
    config: ForgeConfig,
    nats_url: str | None = None,
    clock: Callable[[], datetime] | None = None,
) -> PlanningCompositionResult | None:
    """Compose planning consumer + dispatch stack with boot audit gating.

    See module docstring for the full wiring. DDR-007 soft-fail posture:
    exceptions are caught and logged, never raised.

    Args:
        db_path: Path to the SQLite database for planning run persistence.
        nats_client: The daemon's shared raw ``nats.aio`` client (dispatch
            adapter, approval publisher/subscriber, durable bind).
        config: ForgeConfig with planning configuration.
        nats_url: NATS URL for the DEDICATED ``nats_core.NATSClient`` the
            fleet watcher requires — the raw client does not expose the
            envelope-aware ``subscribe`` / ``watch_fleet`` surface
            ``FleetWatcher.run`` depends on (TASK-MP-012 review finding:
            feeding the watcher the raw client leaves specialist discovery
            permanently empty). None → discovery disabled, logged loudly.
        clock: Optional clock callable (for testing).

    Returns:
        PlanningCompositionResult, or None when planning is disabled or
        composition failed. ``audit_passed=False`` with a result when the
        DF-004 audit refused to start the consumer (build intake is
        unaffected in every failure mode).
    """
    # Feature flag check (AC-7)
    if not config.planning.enabled:
        logger.info("Planning disabled (planning.enabled=False); skipping composition")
        return None

    # DF-004 audit (AC-6)
    audit_result = audit_planning_model_resolution(config.planning)
    if not audit_result.passed:
        logger.error(
            "Planning model audit FAILED: %s — %s. "
            "Planning consumer will NOT start. Build intake unaffected.",
            audit_result.violation,
            audit_result.reason,
        )
        return PlanningCompositionResult(
            consumer_name=PLANNING_DURABLE_NAME,
            subject_filter=PLANNING_QUEUED_SUBJECT_FILTER,
            dispatch_callable=None,
            audit_passed=False,
        )

    logger.info(
        "Planning model audit passed (DF-004 compliant); composing planning stack"
    )

    try:
        # Heavy imports stay call-time so the module imports without the
        # full dispatch stack available (BDD oracle / lint runners).
        from forge.adapters.git.planning_runner import WorktreeGitRunner
        from forge.adapters.nats.approval_publisher import ApprovalPublisher
        from forge.adapters.nats.approval_subscriber import (
            ApprovalSubscriber,
            ApprovalSubscriberDeps,
        )
        from forge.adapters.nats.specialist_dispatch import (
            NatsSpecialistDispatchAdapter,
        )
        from forge.discovery.cache import DiscoveryCache
        from forge.discovery.protocol import SystemClock
        from forge.dispatch.correlation import CorrelationRegistry
        from forge.dispatch.orchestrator import DispatchOrchestrator
        from forge.dispatch.persistence import SqliteHistoryWriter
        from forge.dispatch.timeout import TimeoutCoordinator
        from forge.gating.models import GateMode
        from forge.pipeline.dispatchers.specialist import dispatch_specialist_stage
        from forge.pipeline.forward_context_builder import ForwardContextBuilder
        from forge.pipeline.stage_taxonomy import StageClass
        from forge.planning.driver import PlanningDriverDeps, PlanningRunDriver
        from forge.planning.frontier import FrontierSecondOpinion

        clock_fn = clock if clock is not None else lambda: datetime.now(timezone.utc)

        # -- durable store + gate adapters (TASK-MP-002/004A) ------------
        pool = connect_writer(db_path)
        store = SqlitePlanningRunStore(pool)
        repository, state_machine = build_planning_gate_adapters(store, clock=clock_fn)

        # -- background task supervision ----------------------------------
        background_tasks: set[asyncio.Task[Any]] = set()

        def _supervise(task: asyncio.Task[Any], label: str) -> None:
            background_tasks.add(task)

            def _done(t: asyncio.Task[Any]) -> None:
                background_tasks.discard(t)
                if t.cancelled():
                    return
                exc = t.exception()
                if exc is not None:
                    logger.error(
                        "planning background task %r died: %s — the affected "
                        "run stalls until the next boot sweep/rearm",
                        label,
                        exc,
                        exc_info=exc,
                    )

            task.add_done_callback(_done)

        # -- specialist dispatch stack (first production composition) -----
        cache = DiscoveryCache()
        reply_channel = _LateBoundReplyChannel()
        registry = CorrelationRegistry(transport=reply_channel)
        dispatch_adapter = NatsSpecialistDispatchAdapter(nats_client, registry)
        reply_channel.bind_transport(dispatch_adapter)
        timeout_coordinator = TimeoutCoordinator(
            registry=_RegistryWaitAdapter(registry),
            clock=SystemClock(),
        )
        history_writer = SqliteHistoryWriter(pool)
        orchestrator = DispatchOrchestrator(
            cache=cache,
            registry=registry,
            timeout=timeout_coordinator,
            publisher=dispatch_adapter,
            db_writer=history_writer,
        )

        # Feed the discovery cache from the fleet registry. The watcher
        # REQUIRES the nats_core.NATSClient surface (envelope-aware
        # subscribe + watch_fleet KV) which the raw daemon client lacks —
        # so open a dedicated fleet client from nats_url. Without it,
        # specialist discovery stays empty and every PO dispatch degrades
        # to no_specialist_resolvable: say so LOUDLY.
        if nats_url:
            try:
                from nats_core.client import NATSClient
                from nats_core.config import NATSConfig

                from forge.adapters.nats.fleet_watcher import watch as fleet_watch

                fleet_client = NATSClient(
                    NATSConfig(url=nats_url, name="forge-serve-planning-fleet")
                )
                await fleet_client.connect()

                watcher_task = asyncio.create_task(
                    fleet_watch(fleet_client, cache, status_reader=cache)
                )
                _supervise(watcher_task, "fleet-watcher")
                logger.info(
                    "planning composition: fleet watcher live on dedicated "
                    "nats_core client (specialist discovery active)"
                )
            except Exception:  # noqa: BLE001 — degraded discovery, not fatal
                logger.exception(
                    "planning composition: fleet watcher failed to start; "
                    "specialist discovery will be EMPTY — every PRODUCT_OWNER "
                    "dispatch degrades to no_specialist_resolvable"
                )
        else:
            logger.error(
                "planning composition: no nats_url threaded — specialist "
                "discovery DISABLED; every PRODUCT_OWNER dispatch will "
                "degrade to no_specialist_resolvable until nats_url is wired"
            )

        forward_context_builder = ForwardContextBuilder(
            stage_log_reader=_NullStageLogReader(),
            worktree_allowlist=_DenyAllWorktreeAllowlist(),
        )
        stage_log_writer = _PlanningStageLogWriter(store)

        async def dispatch_product_owner(
            *,
            plan_run_id: str,
            correlation_id: str,
            enrichment: dict[str, Any] | None = None,
        ) -> Any:
            # ``enrichment`` (the EnrichmentBatch-shaped revision delta) is
            # durably recorded by the driver as a ``planning-revision`` event
            # before this re-invoke; the PO's stateless re-invoke reads the
            # prior JSON + dispositions from that durable trace. Threaded here
            # for forward-compatibility with a first-class dispatch carrier.
            if enrichment is not None:
                logger.info(
                    "planning composition: PO re-invoke for %s carries an "
                    "EnrichmentBatch (cycle=%s, %d revisions)",
                    correlation_id,
                    enrichment.get("cycle"),
                    len(enrichment.get("revisions") or ()),
                )
            # M2 + M3 (DISPATCHFMT+ S2, D1): thread the raw planning request
            # text through as the PO greenfield ``problem_statement``. It is the
            # durable ``request_text`` column on the planning_runs row — never
            # re-derived. Absent only for a torn/legacy row (the dispatcher logs
            # loudly and the router rejects the arg-less command).
            run_row = store.get_run(correlation_id)
            request_text = run_row["request_text"] if run_row is not None else None
            if not request_text:
                logger.error(
                    "planning composition: no request_text for %s — the PO "
                    "greenfield problem_statement will be unsourced and the "
                    "specialist will reject the command",
                    correlation_id,
                )
            return await dispatch_specialist_stage(
                stage=StageClass.PRODUCT_OWNER,
                build_id=plan_run_id,
                correlation_id=correlation_id,
                forward_context_builder=forward_context_builder,
                dispatch_surface=orchestrator,
                stage_log_writer=stage_log_writer,
                feature_id=plan_run_id,
                request_text=request_text,
            )

        # -- approval side -------------------------------------------------
        approval_publisher = ApprovalPublisher(nats_client=nats_client)
        pause_publisher = _PlanningPausePublisher(
            approval_publisher, nats_client=nats_client, clock=clock_fn
        )

        def subscriber_factory(
            expected_approver: str | None, armed: asyncio.Event | None
        ) -> Any:
            client = EnvelopeSubscribeClient(nats_client, armed)
            return ApprovalSubscriber(
                ApprovalSubscriberDeps(
                    nats_client=client,
                    config=config.approval,
                    publish_refresh=None,
                    expected_approver=expected_approver,
                )
            )

        # -- notifications (jarvis.notification.slack) --------------------
        async def publish_planning_notification(
            correlation_id: str, message: str, level: str = "info"
        ) -> None:
            # Assumption-dialogue projection (TASK-SPL003F-001): project the
            # durable thread anchor + originator so jarvis threads the message
            # into the originating conversation. Read from the planning_runs
            # row (never re-derived); degrade to None when absent (still
            # visible, unthreaded — never dropped).
            parent_request_id: str | None = None
            target_user: str | None = None
            row = store.get_run(correlation_id)
            if row is not None:
                parent_request_id = row["parent_request_id"]
                target_user = row["originating_user"]

            envelope = build_planning_notification_envelope(
                correlation_id=correlation_id,
                message=message,
                level=level,
                parent_request_id=parent_request_id,
                target_user=target_user,
            )
            await nats_client.publish(
                "jarvis.notification.slack",
                envelope.model_dump_json().encode("utf-8"),
            )

        # -- second opinion provider (DF-006 default-off) ------------------
        second_opinion = FrontierSecondOpinion(
            client=_DisabledFrontierClient(),
            frontier_enabled=config.planning.frontier_enabled,
            frontier_timeout_seconds=config.planning.frontier_timeout_seconds,
            get_po_output=lambda plan_run_id: _latest_po_output(
                store,
                plan_run_id[5:] if plan_run_id.startswith("plan-") else plan_run_id,
            ),
            # v1: the checkpoint is always MANDATORY_HUMAN_APPROVAL, so the
            # FLAG_FOR_REVIEW-only frontier trigger stays dormant (DF-006).
            get_gate_decision=lambda plan_run_id: GateMode.MANDATORY_HUMAN_APPROVAL,
        )

        # -- the chain driver ----------------------------------------------
        driver = PlanningRunDriver(
            PlanningDriverDeps(
                store=store,
                repository=repository,
                state_machine=state_machine,
                approval_publisher=pause_publisher,
                subscriber_factory=subscriber_factory,
                dispatch_product_owner=dispatch_product_owner,
                second_opinion_provider=second_opinion,
                git_runner=WorktreeGitRunner(),
                planning_config=config.planning,
                clock=clock_fn,
                publish_notification=publish_planning_notification,
            )
        )

        # Per-run mutual exclusion — see make_drive_spawner (TASK-MP-012
        # review finding; extracted module-level in TASK-MP-014 so tests
        # exercise the real dedup).
        _spawn_drive = make_drive_spawner(driver, _supervise)

        async def dispatch_stage_callable(correlation_id: str, **kwargs: Any) -> None:
            """Fire-and-forget chain (re-)drive for one planning run."""
            _spawn_drive(correlation_id)

        async def rearm_stage_callable(correlation_id: str, **kwargs: Any) -> None:
            """Fire-and-forget PAUSED-run resume (verbatim re-emit)."""
            _spawn_drive(correlation_id, republish=True)

        # -- intake consumer on the wire ------------------------------------
        async def _on_recorded(correlation_id: str) -> None:
            _spawn_drive(correlation_id)

        async def _notify_two_arg(correlation_id: str, message: str) -> None:
            await publish_planning_notification(correlation_id, message, "info")

        consumer_deps = PlanningConsumerDeps(
            store=store,
            publish_notification=_notify_two_arg,
            on_recorded=_on_recorded,
        )

        subscription = None
        consumer_task = None
        jetstream_fn = getattr(nats_client, "jetstream", None)
        if jetstream_fn is None:
            logger.error(
                "planning composition: NATS client has no JetStream context — "
                "the durable %s consumer CANNOT bind and planning intake will "
                "NOT run (recovery/sweep still active)",
                PLANNING_DURABLE_NAME,
            )
        else:
            subscription = await _attach_planning_consumer(nats_client)
            consumer_task = asyncio.create_task(
                _consume_planning(subscription, consumer_deps)
            )
            _supervise(consumer_task, "planning-consumer")
            logger.info(
                "planning composition: durable %s bound on %s (filter=%s, "
                "ack_wait=%.0fs); intake live",
                PLANNING_DURABLE_NAME,
                PLANNING_STREAM_NAME,
                PLANNING_QUEUED_SUBJECT_FILTER,
                PLANNING_ACK_WAIT_SECONDS,
            )

        return PlanningCompositionResult(
            consumer_name=PLANNING_DURABLE_NAME,
            subject_filter=PLANNING_QUEUED_SUBJECT_FILTER,
            dispatch_callable=dispatch_stage_callable,
            audit_passed=True,
            driver=driver,
            store=store,
            subscription=subscription,
            consumer_task=consumer_task,
            background_tasks=background_tasks,
            rearm_callable=rearm_stage_callable,
        )

    except Exception as exc:
        # DDR-007 soft-fail: never brick daemon boot
        logger.exception(
            "Planning composition failed with exception: %s. "
            "Planning consumer will NOT start. Build intake unaffected.",
            exc,
        )
        return None


async def _attach_planning_consumer(nats_client: Any) -> Any:
    """Bind the durable planning pull consumer (mirrors _serve_daemon)."""
    from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

    js = nats_client.jetstream()
    consumer_config = ConsumerConfig(
        durable_name=PLANNING_DURABLE_NAME,
        deliver_policy=DeliverPolicy.ALL,
        ack_policy=AckPolicy.EXPLICIT,
        filter_subject=PLANNING_QUEUED_SUBJECT_FILTER,
        max_ack_pending=1,
        ack_wait=PLANNING_ACK_WAIT_SECONDS,
    )
    return await js.pull_subscribe(
        subject=PLANNING_QUEUED_SUBJECT_FILTER,
        durable=PLANNING_DURABLE_NAME,
        stream=PLANNING_STREAM_NAME,
        config=consumer_config,
    )


async def _consume_planning(subscription: Any, deps: PlanningConsumerDeps) -> None:
    """Pull-fetch loop for planning intake (mirrors _consume_forever)."""
    while True:
        try:
            msgs = await subscription.fetch(
                _PULL_BATCH_SIZE, timeout=_PULL_TIMEOUT_SECONDS
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            continue  # no message — normal idle
        except Exception:  # noqa: BLE001 — transient fetch failure
            logger.exception("planning consumer: fetch failed; retrying")
            await asyncio.sleep(1.0)
            continue

        for msg in msgs:
            try:
                await handle_planning_message(msg, deps)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — handler is normally total
                logger.exception(
                    "planning consumer: handle_planning_message raised; "
                    "message will redeliver after ack_wait"
                )


# ---------------------------------------------------------------------------
# Recovery functions
# ---------------------------------------------------------------------------


async def rearm_paused_planning_runs(
    db_path: Path,
    nats_client: Any,
    config: PlanningConfig,
    *,
    composition: PlanningCompositionResult | None = None,
    clock: Callable[[], datetime] | None = None,
) -> list[str]:
    """Re-arm all PAUSED planning runs after daemon restart (AC-1, AC-2).

    This function is the ONLY planning re-emit site at boot. For each
    PAUSED run it spawns a supervised
    :meth:`PlanningRunDriver.drive(..., republish_pending=True)` task —
    the driver arms the response waiter FIRST and only then re-emits the
    persisted ``pending_approval_request_id`` VERBATIM (arm-before-post,
    exactly once by the recovery process; ASSUM-015's compensating half).

    Args:
        db_path: Path to SQLite database containing planning_runs.
        nats_client: NATS client (unused directly — the driver owns the
            wire; kept for call-site stability).
        config: PlanningConfig slice (unused directly; ditto).
        composition: The boot's :class:`PlanningCompositionResult`. When
            None (composition failed or not run), nothing can be re-armed
            — a loud warning is logged and [] returned.
        clock: Optional clock callable (unused; the driver's clock rules).

    Returns:
        List of correlation_ids for which a resume task was spawned.
    """
    try:
        if composition is None or composition.driver is None:
            pool = connect_writer(db_path)
            store = SqlitePlanningRunStore(pool)
            paused = store.list_runs_by_state(PlanningState.PAUSED)
            if paused:
                logger.warning(
                    "rearm_paused_planning_runs: %d PAUSED run(s) but no "
                    "planning composition available — they stay PAUSED until "
                    "a boot with a working composition",
                    len(paused),
                )
            return []

        driver: PlanningRunDriver = composition.driver
        store = composition.store or SqlitePlanningRunStore(connect_writer(db_path))
        rearmed: list[str] = []

        for run in store.list_runs_by_state(PlanningState.PAUSED):
            correlation_id = run["correlation_id"]
            try:
                if composition.rearm_callable is not None:
                    # Production path: routes through the composition's
                    # per-run drive dedup (a sweep re-drive that just
                    # paused must not get a second concurrent driver).
                    await composition.rearm_callable(correlation_id)
                else:
                    # Legacy/test path: direct supervised spawn.
                    task = asyncio.create_task(
                        driver.drive(correlation_id, republish_pending=True)
                    )
                    if composition.background_tasks is not None:
                        composition.background_tasks.add(task)
                        task.add_done_callback(composition.background_tasks.discard)
                rearmed.append(correlation_id)
                logger.info(
                    "Rearmed paused planning run: %s (expected_approver: %s)",
                    correlation_id,
                    run["expected_approver"],
                )
            except Exception as exc:  # noqa: BLE001 — per-run isolation
                logger.warning(
                    "Failed to rearm planning run %s: %s. Will retry on next boot.",
                    correlation_id,
                    exc,
                )
                continue

        if rearmed:
            logger.info("Rearmed %d paused planning runs at boot", len(rearmed))
        else:
            logger.debug("No paused planning runs to rearm")

        return rearmed

    except Exception as exc:
        # DDR-007 soft-fail: never brick daemon boot
        logger.exception(
            "Planning rearm failed with exception: %s. "
            "PAUSED runs will be retried on next boot.",
            exc,
        )
        return []


async def sweep_interrupted_planning_runs(
    db_path: Path,
    *,
    dispatch_callable: DispatchCallable | None = None,
    clock: Callable[[], datetime] | None = None,
) -> list[str]:
    """Recover interrupted planning runs at boot (RT-05 boot sweep) (AC-3).

    1. QUEUED runs (crash before dispatch): re-driven through the chain
       driver.
    2. RUNNING runs (crash mid-chain): re-driven through the driver —
       the re-entrant chain resumes from durable history, and the
       handoff's RT-08 idempotency means a run that crashed between the
       branch commit and the record update completes instead of being
       terminally failed.

    Without a dispatcher (planning composition soft-failed this boot) runs
    are LEFT IN PLACE with a loud ERROR — a single bad boot (transient
    misconfiguration, DF-004 violation) must not terminally destroy every
    pending planning request; the next healthy boot re-drives them
    (TASK-MP-012 review finding). "Stuck forever" is prevented by the
    healthy-boot re-drive, not by terminal transitions.

    Every transition's :class:`TransitionRefused` sentinel is CHECKED —
    a refused recovery is logged and NOT reported as recovered
    (post-merge review correctness finding).

    Args:
        db_path: Path to SQLite database containing planning_runs.
        dispatch_callable: Fire-and-forget re-drive from the composition
            result. None → runs are left in place (loud ERROR).
        clock: Optional clock callable (for testing).

    Returns:
        List of correlation_ids that were actually recovered (re-driven).
    """
    try:
        pool = connect_writer(db_path)
        store = SqlitePlanningRunStore(pool)

        recovered: list[str] = []

        interrupted = [
            ("QUEUED", run) for run in store.list_runs_by_state(PlanningState.QUEUED)
        ] + [
            ("RUNNING", run) for run in store.list_runs_by_state(PlanningState.RUNNING)
        ]

        if dispatch_callable is None:
            if interrupted:
                logger.error(
                    "Boot sweep: %d interrupted planning run(s) but NO "
                    "dispatcher (planning composition failed this boot) — "
                    "leaving them in place for the next healthy boot: %s",
                    len(interrupted),
                    [run["correlation_id"] for _, run in interrupted],
                )
            else:
                logger.debug("No interrupted planning runs to recover")
            return []

        for state_name, run in interrupted:
            correlation_id = run["correlation_id"]
            try:
                # RT-08: RUNNING runs are re-driven instead of terminally
                # failed — the re-entrant driver + idempotent handoff
                # complete work that already succeeded before the crash.
                logger.info(
                    "Re-driving %s planning run: %s", state_name, correlation_id
                )
                await dispatch_callable(correlation_id)
                recovered.append(correlation_id)
            except Exception as exc:  # noqa: BLE001 — per-run isolation
                logger.warning(
                    "Failed to recover %s run %s: %s",
                    state_name,
                    correlation_id,
                    exc,
                )
                continue

        if recovered:
            logger.info(
                "Boot sweep recovered %d interrupted planning runs", len(recovered)
            )
        else:
            logger.debug("No interrupted planning runs to recover")

        return recovered

    except Exception as exc:
        # DDR-007 soft-fail: never brick daemon boot
        logger.exception(
            "Planning boot sweep failed with exception: %s. "
            "Interrupted runs will be retried on next boot.",
            exc,
        )
        return []

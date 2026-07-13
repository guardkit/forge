"""Mode P planning chain driver (TASK-MP-012).

The production driver that walks one planning run through the chain

    QUEUED → RUNNING → PRODUCT_OWNER dispatch → product_docs checkpoint
    (PAUSED) → approve/reject/defer/escalate/timeout → planned handoff
    (PLANNED_HANDOFF)

by consulting the pure planner (:func:`forge.planning.planner.plan_next_step`)
over history **translated from durable planning_run_events rows** — this is
what makes the driver re-entrant: after a crash, :meth:`PlanningRunDriver.drive`
resumes at exactly the step the durable history implies (post-merge review
finding: ``ExecuteHandoff`` was unreachable from durable history because
nothing wrote planner-shaped events).

Design notes
------------

* **Domain module, injected collaborators** — no NATS / SQLite / git types
  are imported here; ``cli/_serve_planning.py`` is the composition root.
* **Structured wait, not a poller** — escalation phase and remaining time
  are recomputed from the durable ``paused_at`` / ``escalated_at`` anchors
  on every loop iteration, so a daemon restart neither resets nor
  double-fires a window (rearm re-enters the same loop).
* **Arm-before-post** — re-publishes (rearm, escalation, defer rounds)
  happen only after the response waiter's subscription is armed. The one
  deliberate exception is the *initial* checkpoint publish (the tested
  ``checkpoint_product_docs`` contract owns pause+publish); the waiter
  arms milliseconds later and the escalation rounds are the self-healing
  backstop for a theoretically lost first response.
* **Task supervision** — the composition wraps ``drive`` tasks so an
  unhandled exception is logged loudly; a stalled run is recovered by the
  next boot's sweep/rearm (documented v1 limitation, exercised by
  TASK-MP-010 live validation).

References: TASK-MP-012, FEAT-SPL-002, DF-009, RT-04, RT-08, DDR-007.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping

from forge.gating.identity import parse_request_id
from forge.lifecycle.identifiers import validate_feature_id
from forge.pipeline.stage_taxonomy import StageClass
from forge.planning.checkpoint import (
    PlanningEscalationContext,
    _dispatch_approval_response,
    build_planning_approval_envelope,
    checkpoint_product_docs,
)
from forge.planning.escalation import (
    EscalationOutcome,
    EscalationPolicy,
    evaluate_escalation_phase,
)
from forge.planning.handoff import (
    PlannedHandoffHandler,
    PreCommitResult,
    build_feature_spec_input_content,
)
from forge.planning.planner import (
    BoundaryViolation,
    DispatchProductOwner,
    ExecuteHandoff,
    Fail,
    PauseAtCheckpoint,
    plan_next_step,
)
from forge.planning.revision import (
    CYCLE_CAP,
    REVISION_STAGE_LABEL,
    assemble_enrichment_batch,
    dialogue_cycle,
    normalize_assumptions,
    parse_dispositions,
)
from forge.planning.run_store import SqlitePlanningRunStore, TransitionRefused
from forge.planning.states import PlanningState

if TYPE_CHECKING:  # pragma: no cover - typing only
    from forge.config.models import PlanningConfig
    from forge.gating.wrappers import GateRepository, StateMachine
    from forge.preflight import ResourcePreflightResult
    from forge.planning.checkpoint import SecondOpinionProvider
    from forge.planning.target_terminal_tools import (
        NormalizeFeatureSpecFn,
        ValidateFeaturePlanFn,
    )

logger = logging.getLogger(__name__)

__all__ = ["BuildTriggerResult", "PlanningDriverDeps", "PlanningRunDriver"]

#: Stage label used for the product docs checkpoint (pinned by checkpoint.py).
_PRODUCT_DOCS_STAGE = "product_docs"

#: Upper bound on how long we wait for a response subscription to arm
#: before a re-publish (mirrors _REARM_ARM_TIMEOUT_SECONDS).
_ARM_TIMEOUT_SECONDS = 10.0

_TERMINAL_STATES = {
    PlanningState.FAILED,
    PlanningState.CANCELLED,
    PlanningState.TIMED_OUT,
    PlanningState.PLANNED_HANDOFF,
    # Target terminal (Lane B). Only reachable when the flag is on; adding it
    # here is a byte-for-byte no-op with the flag off (unreachable state) and
    # makes a re-drive of a completed BUILD_QUEUED run return immediately.
    PlanningState.BUILD_QUEUED,
}

#: Durable stage labels for the target-terminal legs (Lane B / Phase E1 B2).
#: A completed leg records ``status="approved"`` under these labels; their
#: presence makes the legs idempotent on a re-drive (crash between the leg's
#: artifact write and the state transition never re-dispatches the specialist).
_FEATURE_SPEC_STAGE = "feature-spec"
_FEATURE_PLAN_STAGE = "feature-plan"
#: Durable stage label for the B3 build trigger. Its presence makes the trigger
#: idempotent on a re-drive (crash between the build-queued publish and the
#: BUILD_QUEUED transition never re-queues the build).
_BUILD_QUEUED_STAGE = "build-queued"

#: Filesystem-safe feature slug allowlist (mirrors the identifier boundary):
#: a specialist-supplied slug is only trusted when it matches, else forge falls
#: back to a deterministic ``feature-{cid}``.
_SLUG_RE = re.compile(r"[A-Za-z0-9_-]+")

PublishNotificationFn = Callable[[str, str, str], Awaitable[None]]
"""``async (correlation_id, message, level) -> None`` — best-effort notify."""

ResourcePreflightFn = Callable[[], "ResourcePreflightResult"]
"""``() -> ResourcePreflightResult`` — a zero-arg pre-run resource check.

Bound (e.g. ``functools.partial(run_resource_preflight, config.resource_preflight)``)
so the driver stays ignorant of ``/proc`` and ``shutil``. Consulted once at the
QUEUED→RUNNING run-start boundary (O-27/O-29); ``None`` = no preflight wired."""

SubscriberFactory = Callable[[str | None, "asyncio.Event | None"], Any]
"""``(expected_approver, armed_event) -> subscriber`` — the returned object
exposes ``await_response(build_id, *, stage_label, attempt_count,
timeout_seconds)`` (the ApprovalSubscriber surface); ``armed_event`` is set
the moment the underlying subscription is active (arm-before-post)."""

DispatchProductOwnerFn = Callable[..., Awaitable[Any]]
"""``async (*, plan_run_id, correlation_id, enrichment=None) -> StageDispatchResult``.

``enrichment`` is the EnrichmentBatch-shaped revision delta on an
assumption-dialogue re-invoke (``None`` on the first dispatch)."""

DispatchFeatureSpecFn = Callable[..., Awaitable[Any]]
"""``async (*, plan_run_id, correlation_id, spec_input) -> StageDispatchResult``.

Lane B (B2): dispatch the ``po_feature_spec`` (007) leg with the committed
feature-spec-input content; the result's ``role_output`` carries the three-file
spec contract."""

DispatchFeaturePlanFn = Callable[..., Awaitable[Any]]
"""``async (*, plan_run_id, correlation_id, scope, target_repo, feature_id)
-> StageDispatchResult``.

Lane B (B2): dispatch the ``architect_feature_plan`` (008) leg. Forge ALWAYS
supplies the scope + target-repo descriptor + the forge-minted ``feature_id``
(RV-1: the plan leg asserts the SUPPLIED id). The result's ``role_output``
carries the plan tree."""


@dataclass(frozen=True)
class BuildTriggerResult:
    """Outcome of the B3 build trigger (Lane B / Phase E1 B3).

    ``queued`` True means the feature was accepted onto forge's OWN Mode B
    build intake (``pipeline.build-queued.{feature_id}``) — the canonical
    dispatcher whose pre-dispatch approval gate then pauses the build for the
    human tap. ``build_id`` is the build identifier when the trigger seam
    surfaces one (``None`` for the fire-and-forget publish seam). ``reason``
    carries the loud-failure detail when ``queued`` is False.
    """

    queued: bool
    build_id: str | None = None
    reason: str | None = None


DispatchBuildTriggerFn = Callable[..., Awaitable["BuildTriggerResult"]]
"""``async (*, plan_run_id, correlation_id, feature_id, target_repo, branch,
plan_files, originating_user) -> BuildTriggerResult``.

Lane B (B3): queue the validated feature onto forge's OWN Mode B build
dispatcher (``dispatch_autobuild_async`` reached via the build-queued intake,
NOT the local guardkit CLI) so the pre-dispatch approval gate pauses it for the
human tap. This is a fire-and-forget publish — it does NOT wait on a specialist
round-trip, so it introduces no new unbounded wait (rule 5)."""


def _extract_assumptions(result: Any) -> list[dict[str, Any]]:
    """Best-effort projection of PO-surfaced assumptions off a dispatch result.

    The current :class:`StageDispatchResult` shape does not carry assumptions
    as a first-class field, so this reads an ``assumptions`` attribute when the
    PO result exposes one and falls back to ``criterion_breakdown['assumptions']``.
    Empty when neither is present (the checkpoint degrades to a no-assumptions
    prompt — never a crash).
    """
    raw = getattr(result, "assumptions", None)
    if raw is None:
        breakdown = getattr(result, "criterion_breakdown", None)
        if isinstance(breakdown, Mapping):
            raw = breakdown.get("assumptions")
    return normalize_assumptions(raw)


@dataclass
class PlanningDriverDeps:
    """Injected collaborators for :class:`PlanningRunDriver`."""

    store: SqlitePlanningRunStore
    repository: "GateRepository"
    state_machine: "StateMachine"
    approval_publisher: Any  # publish_request(envelope) — pause-mirroring wrapper
    subscriber_factory: SubscriberFactory
    dispatch_product_owner: DispatchProductOwnerFn
    second_opinion_provider: "SecondOpinionProvider"
    git_runner: Any  # forge.planning.handoff.GitRunner
    planning_config: "PlanningConfig"
    clock: Callable[[], datetime]
    publish_notification: PublishNotificationFn | None = None
    # O-27/O-29 (E2-S4) — pre-run resource-headroom preflight. Optional / default
    # None: unwired = no preflight (byte-for-byte no-op). Wired, it is consulted
    # exactly ONCE at the fresh QUEUED→RUNNING run-start boundary; a breach fails
    # the run LOUDLY before any seat-holding dispatch (never a mid-run kill, and
    # never re-run on a crash re-drive of an already-RUNNING run).
    resource_preflight: ResourcePreflightFn | None = None
    # Lane B / Phase E1 (B2) — the target-terminal spec/plan legs. All optional
    # and default None: with the target-terminal flag OFF they are never
    # consulted (byte-for-byte no-op). With the flag ON they are required; a
    # missing collaborator fails the run LOUDLY (never a silent skip).
    dispatch_feature_spec: DispatchFeatureSpecFn | None = None
    dispatch_feature_plan: DispatchFeaturePlanFn | None = None
    normalize_feature_spec: "NormalizeFeatureSpecFn | None" = None
    validate_feature_plan: "ValidateFeaturePlanFn | None" = None
    # Lane B / Phase E1 (B3) — the build trigger. Optional / default None: with
    # the target-terminal flag OFF it is never consulted; with the flag ON it is
    # required and a missing collaborator fails the run LOUDLY (never silent).
    dispatch_build_trigger: DispatchBuildTriggerFn | None = None


@dataclass(frozen=True)
class _HistoryEvent:
    """Planner-shaped view of a planning_run_events row."""

    stage: StageClass
    status: str
    details: Mapping[str, Any]


class PlanningRunDriver:
    """Re-entrant chain driver for one planning run at a time.

    ``drive`` may be called for a run in ANY state — it resumes from the
    durable state and history, which is exactly what the boot sweep
    (QUEUED / RUNNING) and rearm (PAUSED, ``republish_pending=True``)
    need.
    """

    def __init__(self, deps: PlanningDriverDeps) -> None:
        self._deps = deps

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    async def drive(
        self, correlation_id: str, *, republish_pending: bool = False
    ) -> None:
        """Drive ``correlation_id`` forward from its durable state.

        Args:
            correlation_id: The planning run to advance.
            republish_pending: True on the rearm path — the persisted
                ``pending_approval_request_id`` is re-emitted VERBATIM
                after the response waiter arms (ASSUM-015's compensating
                half; spec: "re-issued exactly once by the recovery
                process").
        """
        deps = self._deps
        row = deps.store.get_run(correlation_id)
        if row is None:
            logger.error("planning driver: run %s not found", correlation_id)
            return

        state = PlanningState(row["state"])
        if state in _TERMINAL_STATES:
            logger.info(
                "planning driver: run %s already terminal (%s); nothing to do",
                correlation_id,
                state.value,
            )
            return

        plan_run_id = f"plan-{correlation_id}"

        if state is PlanningState.QUEUED:
            refused = deps.store.transition(
                correlation_id=correlation_id,
                to_state=PlanningState.RUNNING,
                actor_identity="planning-driver",
                stage_label="planning-start",
            )
            if isinstance(refused, TransitionRefused):
                logger.warning(
                    "planning driver: QUEUED→RUNNING refused for %s "
                    "(current=%s); another driver won — backing off",
                    correlation_id,
                    refused.current_state,
                )
                return

            # O-27/O-29 — resource-headroom preflight at the fresh run-start
            # boundary. We are now RUNNING (FAILED is a legal edge) and nothing
            # has dispatched yet, so a starved box refuses CLEANLY here rather
            # than risk a mid-run kernel OOM-kill / ENOSPC. Only on this fresh
            # QUEUED→RUNNING path: a crash re-drive of an already-RUNNING run
            # never re-preflights (we never kill work in flight).
            if deps.resource_preflight is not None:
                preflight = deps.resource_preflight()
                if not preflight.ok:
                    logger.error(
                        "planning driver: %s refused at run start — %s",
                        correlation_id,
                        preflight.summary,
                    )
                    await self._fail_leg(
                        correlation_id,
                        stage_label="resource-preflight",
                        reason=preflight.summary,
                    )
                    return

        needs_republish = republish_pending
        checkpoint_failures = 0
        while True:
            row = deps.store.get_run(correlation_id)
            if row is None:  # pragma: no cover - defensive
                return
            state = PlanningState(row["state"])

            if state in _TERMINAL_STATES:
                return

            if state is PlanningState.PAUSED:
                outcome = await self._await_approval(
                    row, plan_run_id, needs_republish=needs_republish
                )
                needs_republish = False
                if outcome != "approved":
                    return
                continue  # re-read: now RUNNING, planner advances the chain

            # Target-terminal chain (Lane B / Phase E1). These states are
            # forge-machine states, not Mode-P-planner states — the driver
            # advances them directly (the pure planner never sees them; its
            # forbidden-stage guard only inspects the product_owner /
            # checkpoint_cleared history rows, which use different labels).
            if state is PlanningState.FEATURE_SPEC:
                if not await self._feature_spec_leg(row, correlation_id):
                    return
                continue  # re-read: now FEATURE_PLAN
            if state is PlanningState.FEATURE_PLAN:
                # B2: dispatch 008 + write + validate the plan tree (idempotent
                # on a re-drive). B3: on validate green, queue the feature onto
                # forge's own Mode B dispatcher and advance to BUILD_QUEUED.
                if not await self._feature_plan_leg(row, correlation_id):
                    return
                if not await self._build_trigger_leg(row, correlation_id):
                    return
                continue  # re-read: now BUILD_QUEUED (terminal) — loop returns

            # state is RUNNING — consult the pure planner over durable history
            history = self._load_history(correlation_id)
            decision = plan_next_step(history)

            if isinstance(decision, BoundaryViolation):
                self._fail(
                    correlation_id,
                    stage_label="planning-boundary",
                    reason=decision.rationale,
                )
                return

            if isinstance(decision, Fail):
                self._fail(
                    correlation_id,
                    stage_label="planning-dispatch",
                    reason=decision.reason,
                )
                await self._notify(
                    correlation_id,
                    f"Planning run {correlation_id} failed: {decision.reason}",
                    level="error",
                )
                return

            if isinstance(decision, DispatchProductOwner):
                ok = await self._dispatch_po(correlation_id, plan_run_id)
                if not ok:
                    return
                continue

            if isinstance(decision, PauseAtCheckpoint):
                paused = await self._checkpoint(correlation_id, plan_run_id)
                if not paused:
                    # The pause did NOT reach durable state (pre-pause store
                    # failure) — retrying instantly would spin a no-yield
                    # loop and starve the event loop (TASK-MP-012 review
                    # finding). Back off, and fail terminally after
                    # repeated failures.
                    checkpoint_failures += 1
                    if checkpoint_failures >= 3:
                        self._fail(
                            correlation_id,
                            stage_label=_PRODUCT_DOCS_STAGE,
                            reason="checkpoint pause failed 3 times "
                            "(store unavailable?)",
                        )
                        return
                    await asyncio.sleep(1.0)
                continue

            if isinstance(decision, ExecuteHandoff):
                if self._target_terminal_enabled():
                    # Flag ON: write the handoff file (the 007 input) and enter
                    # the machine chain instead of terminating. PLANNED_HANDOFF
                    # stays the reachable fallback (flag OFF), never removed.
                    if not await self._enter_target_terminal(row, correlation_id):
                        return
                    continue  # re-read: now FEATURE_SPEC
                await self._handoff(row, correlation_id)
                return

            logger.error(  # pragma: no cover - exhaustive union
                "planning driver: unknown planner decision %r for %s",
                decision,
                correlation_id,
            )
            return

    # ------------------------------------------------------------------ #
    # Chain steps
    # ------------------------------------------------------------------ #

    async def _dispatch_po(
        self,
        correlation_id: str,
        plan_run_id: str,
        *,
        enrichment: dict[str, Any] | None = None,
    ) -> bool:
        """Dispatch the PRODUCT_OWNER specialist stage; record the outcome.

        On an assumption-dialogue revision, ``enrichment`` carries the
        EnrichmentBatch-shaped delta (prior assumptions + human dispositions);
        the PO re-invoke is stateless (propose-never-elicit — forge assembles
        the delta). The delta is threaded to the dispatch collaborator and
        recorded on the PO event for durability.
        """
        deps = self._deps
        # Pass ``enrichment`` only on a revision re-invoke so first-dispatch
        # collaborators keep their ``(*, plan_run_id, correlation_id)`` shape.
        extra: dict[str, Any] = {"enrichment": enrichment} if enrichment else {}
        try:
            result = await deps.dispatch_product_owner(
                plan_run_id=plan_run_id,
                correlation_id=correlation_id,
                **extra,
            )
        except Exception as exc:  # noqa: BLE001 — driver never crashes the run silently
            logger.exception(
                "planning driver: PO dispatch raised for %s", correlation_id
            )
            self._fail(
                correlation_id,
                stage_label="product_owner",
                reason=f"PO dispatch raised {type(exc).__name__}: {exc}",
            )
            return False

        outcome = getattr(result, "outcome", None)
        outcome_value = str(getattr(outcome, "value", outcome or "error")).lower()
        coach_score = getattr(result, "coach_score", None)
        reason = getattr(result, "reason", None)

        if outcome_value in ("completed", "degraded"):
            # M10: the product-doc content is the REAL role_output document
            # the specialist produced — NOT criterion_breakdown, which is
            # Coach *evidence* about that document. Sourcing docs_summary
            # from criterion_breakdown delivered an empty doc to the phone
            # checkpoint + PLANNED_HANDOFF even after a successful PO run.
            role_output = getattr(result, "role_output", None) or {}
            criterion_breakdown = getattr(result, "criterion_breakdown", None) or {}
            po_output: dict[str, Any] = {
                "coach_evidence": {
                    "coach_score": coach_score,
                    "criterion_breakdown": criterion_breakdown,
                },
                "docs_summary": role_output,
                "structured_findings": [
                    str(f) for f in (getattr(result, "detection_findings", ()) or ())
                ],
                # Structured assumptions surfaced by the PO (best-effort — the
                # result shape carries them when the PO emits confidence-tagged
                # assumptions; empty otherwise, and the checkpoint degrades to a
                # no-assumptions prompt). This is the source the checkpoint
                # projects into details.summary.assumptions (TASK-SPL003F-001).
                "assumptions": _extract_assumptions(result),
                "degraded": outcome_value == "degraded",
                "reason": reason,
            }
            deps.store._record_event(
                correlation_id=correlation_id,
                stage_label="product_owner",
                status="approved",
                coach_score=coach_score,
                actor_identity="planning-driver",
                details_json=json.dumps({"po_output": po_output}, default=str),
            )
            logger.info(
                "planning driver: PRODUCT_OWNER %s for %s (coach_score=%s)",
                outcome_value,
                correlation_id,
                coach_score,
            )
            return True

        # soft_timeout / error → terminal failure
        self._fail(
            correlation_id,
            stage_label="product_owner",
            reason=f"PO dispatch {outcome_value}: {reason or 'no reason supplied'}",
        )
        await self._notify(
            correlation_id,
            f"Planning run {correlation_id} failed at PRODUCT_OWNER ({outcome_value}).",
            level="error",
        )
        return False

    async def _checkpoint(self, correlation_id: str, plan_run_id: str) -> bool:
        """Pause at the product docs checkpoint (DF-009: always pauses).

        Returns True when the PAUSED state reached durable storage (even
        if the subsequent publish failed — escalation rounds / rearm
        re-emit), False when the pause itself did not persist.
        """
        deps = self._deps
        try:
            await checkpoint_product_docs(
                plan_run_id=plan_run_id,
                feature_id=plan_run_id,
                repository=deps.repository,
                state_machine=deps.state_machine,
                publisher=deps.approval_publisher,
                second_opinion_provider=deps.second_opinion_provider,
                coach_evidence=self._latest_po_output(correlation_id).get(
                    "coach_evidence"
                ),
                clock=deps.clock,
            )
        except Exception:  # noqa: BLE001 — publish failure keeps the durable pause
            logger.exception(
                "planning driver: checkpoint raised for %s; verifying whether "
                "the pause reached durable state (DDR-007)",
                correlation_id,
            )
        row = deps.store.get_run(correlation_id)
        return row is not None and row["state"] == PlanningState.PAUSED.value

    async def _await_approval(
        self, row: Any, plan_run_id: str, *, needs_republish: bool
    ) -> str:
        """Structured wait on a PAUSED run until a decision or timeout.

        Returns ``"approved"`` (chain continues), ``"cancelled"``,
        ``"timed_out"`` or ``"externally_resolved"``.
        """
        deps = self._deps
        correlation_id = row["correlation_id"]
        cfg = deps.planning_config

        while True:
            row = deps.store.get_run(correlation_id)
            if row is None:  # pragma: no cover - defensive
                return "externally_resolved"
            state = PlanningState(row["state"])
            if state is not PlanningState.PAUSED:
                # A concurrent actor resolved the pause (approve via
                # another path, cancel, timeout) — surface it.
                if state is PlanningState.RUNNING:
                    return "approved"
                return "externally_resolved"

            if row["paused_at"] is None and row["escalated_at"] is None:
                # Corrupt/legacy PAUSED row with no anchor: stamp the
                # window start ONCE durably — recomputing "starts now" on
                # every iteration would wait forever (TASK-MP-012 review
                # finding).
                deps.store.update_escalation(
                    correlation_id=correlation_id,
                    paused_at=deps.clock().isoformat(),
                    expected_state=PlanningState.PAUSED,
                )
                row = deps.store.get_run(correlation_id) or row

            phase, remaining = self._phase_remaining(row, cfg)

            if remaining <= 0:
                if phase == 1 and cfg.escalation_approver:
                    outcome = await evaluate_escalation_phase(
                        store=deps.store,
                        correlation_id=correlation_id,
                        policy=self._policy(cfg),
                        clock=deps.clock,
                        publisher=None,  # persist re-target only; publish
                        plan_run_id=plan_run_id,  # happens AFTER arming below
                        feature_id=plan_run_id,
                    )
                    if outcome is EscalationOutcome.ESCALATED:
                        needs_republish = True
                    continue
                # Phase-2 ceiling (or no escalation approver configured):
                # durable TIMED_OUT via CAS from PAUSED.
                refused = deps.store.transition(
                    correlation_id=correlation_id,
                    to_state=PlanningState.TIMED_OUT,
                    actor_identity="planning-driver",
                    stage_label="planning-escalation-timeout",
                    error="approval wait ceiling reached",
                    expected_from_state=PlanningState.PAUSED,
                )
                if isinstance(refused, TransitionRefused):
                    continue  # a concurrent decision won; re-read state
                await self._notify(
                    correlation_id,
                    f"Planning run {correlation_id} timed out awaiting approval.",
                    level="warning",
                )
                return "timed_out"

            expected_approver = row["expected_approver"]
            pending_request_id = row["pending_approval_request_id"]
            attempt_count = self._attempt_from(pending_request_id)

            armed: asyncio.Event = asyncio.Event()
            subscriber = deps.subscriber_factory(expected_approver, armed)
            wait_started = time.monotonic()
            wait_task = asyncio.create_task(
                subscriber.await_response(
                    plan_run_id,
                    stage_label=_PRODUCT_DOCS_STAGE,
                    attempt_count=attempt_count,
                    timeout_seconds=max(1, int(remaining)),
                )
            )
            try:
                await asyncio.wait_for(armed.wait(), timeout=_ARM_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                logger.error(
                    "planning driver: response subscription failed to arm "
                    "for %s within %.0fs; retrying",
                    correlation_id,
                    _ARM_TIMEOUT_SECONDS,
                )
                wait_task.cancel()
                try:
                    await wait_task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001 — surface the root cause
                    logger.exception(
                        "planning driver: response waiter failed before "
                        "arming for %s (root cause of the arm timeout)",
                        correlation_id,
                    )
                await asyncio.sleep(1.0)  # anti-spin: never tight-loop arming
                continue

            if needs_republish and pending_request_id:
                # Arm-before-post: the subscription is live, now re-emit
                # the persisted request_id VERBATIM (rearm / escalation /
                # defer rounds).
                await self._republish_pending(row, plan_run_id)
                needs_republish = False

            try:
                response = await wait_task
            except Exception:  # noqa: BLE001 — a waiter defect must not kill the run
                logger.exception(
                    "planning driver: response waiter raised for %s; retrying",
                    correlation_id,
                )
                await asyncio.sleep(1.0)
                continue
            if response is None:
                # Window expired — the durable anchors drive escalation /
                # timeout on the next iteration. Anti-spin: if the waiter
                # returned instantly (defective subscriber, empty fake
                # script) while wall-clock time remains, back off so a
                # broken wire cannot hot-loop the daemon.
                if (
                    time.monotonic() - wait_started < 1.0
                    and self._phase_remaining(
                        deps.store.get_run(correlation_id) or row, cfg
                    )[1]
                    > 0
                ):
                    await asyncio.sleep(1.0)
                continue

            # Stale-round guard: a late response to a superseded
            # request_id is ignored; the wait continues.
            current = deps.store.get_run(correlation_id)
            current_pending = (
                current["pending_approval_request_id"] if current else None
            )
            if current_pending and response.request_id != current_pending:
                logger.warning(
                    "planning driver: stale response request_id=%s for %s "
                    "(current=%s); ignoring",
                    response.request_id,
                    correlation_id,
                    current_pending,
                )
                continue

            outcome = await _dispatch_approval_response(
                response=response,
                repository=deps.repository,
                state_machine=deps.state_machine,
                clock=deps.clock,
                escalation_context=PlanningEscalationContext(
                    store=deps.store,
                    policy=self._policy(cfg),
                    # publisher=None: the defer/at-cap round is persisted by
                    # the escalation module but published HERE, after the
                    # next iteration's waiter is armed (arm-before-post).
                    publisher=None,
                    feature_id=plan_run_id,
                ),
            )

            if outcome in ("approved", "overridden"):
                deps.store._record_event(
                    correlation_id=correlation_id,
                    stage_label=_PRODUCT_DOCS_STAGE,
                    status="checkpoint_cleared",
                    actor_identity=response.decided_by,
                    details_json=json.dumps(
                        {"stage_label": _PRODUCT_DOCS_STAGE, "outcome": outcome}
                    ),
                )
                return "approved"
            if outcome == "revise":
                # Assumption-dialogue revision: assemble the EnrichmentBatch,
                # cap-3 → escalate, else re-invoke the PO statelessly. The
                # checkpoint is NOT cleared; the chain re-pauses next cycle.
                handled = await self._handle_revision(
                    correlation_id, plan_run_id, response
                )
                if handled == "escalated":
                    # Escalated to Rich — keep waiting on the escalated
                    # approver (re-emit the escalated request once armed).
                    needs_republish = True
                    continue
                if handled == "revising":
                    # RUNNING with a fresh PO output — hand back to drive()'s
                    # main loop, which re-checkpoints with the new cycle.
                    return "approved"
                # revision could not be applied (store/dispatch failure) — the
                # run stays PAUSED; the wait ceiling / rearm recovers it.
                return "externally_resolved"
            if outcome == "rejected":
                await self._notify(
                    correlation_id,
                    f"Planning run {correlation_id} was rejected by "
                    f"{response.decided_by}.",
                    level="warning",
                )
                return "cancelled"
            if outcome == "deferred":
                # The new round's request_id is already persisted; re-emit
                # it once the next waiter is armed (arm-before-post).
                needs_republish = True
                continue
            # refused / unknown → keep waiting
            continue

    async def _handle_revision(
        self, correlation_id: str, plan_run_id: str, response: Any
    ) -> str:
        """Apply an assumption-dialogue revision (cap-3 → escalate, else re-invoke).

        Returns ``"escalated"`` (cap reached — escalated to Rich, run stays
        PAUSED), ``"revising"`` (RUNNING with a fresh PO output — drive()
        re-checkpoints) or ``"failed"`` (the revision could not be applied).
        """
        deps = self._deps
        dispositions = parse_dispositions(response)

        # Current dialogue cycle = 1 + durable count of recorded revisions.
        current_cycle = self._dialogue_cycle(correlation_id)

        # Cap-3: a revision that would open a 4th cycle escalates to Rich via
        # the existing escalation path (durable expected_approver re-target,
        # checkpoint_type=product_docs_escalated) instead of another round.
        if current_cycle >= CYCLE_CAP:
            logger.info(
                "planning driver: dialogue cap (%d) reached for %s; escalating "
                "instead of a %dth cycle",
                CYCLE_CAP,
                correlation_id,
                current_cycle + 1,
            )
            from forge.planning.escalation import escalate_planning_run

            await escalate_planning_run(
                store=deps.store,
                correlation_id=correlation_id,
                policy=self._policy(deps.planning_config),
                clock=deps.clock,
                publisher=None,  # arm-before-post: drive() re-emits once armed
                plan_run_id=plan_run_id,
                feature_id=plan_run_id,
            )
            return "escalated"

        # Assemble the EnrichmentBatch delta from the prior assumptions + the
        # human dispositions (forge assembles the delta; the PO does no
        # elicitation — propose-never-elicit).
        prior_assumptions = normalize_assumptions(
            self._latest_po_output(correlation_id).get("assumptions")
        )
        next_cycle = current_cycle + 1
        batch = assemble_enrichment_batch(
            correlation_id=correlation_id,
            cycle=next_cycle,
            prior_assumptions=prior_assumptions,
            dispositions=dispositions,
        )

        # Record the revision durably (increments the dialogue-cycle count so
        # the next checkpoint projects cycle=next_cycle) BEFORE re-dispatching.
        deps.store._record_event(
            correlation_id=correlation_id,
            stage_label=REVISION_STAGE_LABEL,
            status="REVISION",
            actor_identity=response.decided_by,
            details_json=json.dumps({"cycle": next_cycle, "enrichment_batch": batch}),
        )

        # PAUSED → RUNNING so the main loop re-dispatches the PO, then re-invoke
        # the PO statelessly with the assembled delta.
        try:
            await deps.state_machine.transition_to_running(build_id=plan_run_id)
            await deps.repository.mark_resumed(
                build_id=plan_run_id, stage_label=_PRODUCT_DOCS_STAGE
            )
        except Exception:  # noqa: BLE001 — a transition defect must not crash the run
            logger.exception(
                "planning driver: revise transition failed for %s", correlation_id
            )
            return "failed"

        ok = await self._dispatch_po(correlation_id, plan_run_id, enrichment=batch)
        return "revising" if ok else "failed"

    def _dialogue_cycle(self, correlation_id: str) -> int:
        """1-based dialogue cycle from the durable revision-event count.

        Delegates to :func:`revision.dialogue_cycle` — the same arithmetic the
        checkpoint projects, so the projected ``cycle`` and this cap gate can
        never diverge.
        """
        return dialogue_cycle(self._deps.store.list_events(correlation_id))

    async def _handoff(self, row: Any, correlation_id: str) -> None:
        """Execute the planned-handoff terminal (idempotent, RT-08)."""
        deps = self._deps
        po_output = self._latest_po_output(correlation_id)
        run_data: dict[str, Any] = {
            "correlation_id": correlation_id,
            "state": row["state"],
            "request_text": row["request_text"],
            "originating_user": row["originating_user"],
            "target_repo": row["target_repo"],
            "product_docs": po_output.get("docs_summary") or {},
        }

        handler = PlannedHandoffHandler(deps.planning_config, deps.git_runner)
        result = await handler.handle(run_data)

        if result.get("state") == PlanningState.PLANNED_HANDOFF.value:
            refused = deps.store.transition(
                correlation_id=correlation_id,
                to_state=PlanningState.PLANNED_HANDOFF,
                actor_identity="planning-driver",
                stage_label="planned-handoff",
                handoff_branch=result.get("handoff_branch"),
                handoff_path=result.get("handoff_path"),
            )
            if isinstance(refused, TransitionRefused):
                logger.warning(
                    "planning driver: PLANNED_HANDOFF transition refused for "
                    "%s (current=%s)",
                    correlation_id,
                    refused.current_state,
                )
                return
            payload = result.get("notification_payload") or {}
            message = payload.get("message", f"Planning complete for {correlation_id}.")
            command = payload.get("command")
            if command:
                message = f"{message} Next: `{command}`"
            await self._notify(correlation_id, message, level="info")
            logger.info(
                "planning driver: run %s reached PLANNED_HANDOFF (branch=%s)",
                correlation_id,
                result.get("handoff_branch"),
            )
            return

        reason = result.get("failure_reason", "handoff failed")
        self._fail(correlation_id, stage_label="planned-handoff", reason=reason)
        await self._notify(
            correlation_id,
            f"Planning run {correlation_id} handoff failed: {reason}",
            level="error",
        )

    # ------------------------------------------------------------------ #
    # Target terminal (Lane B / Phase E1 — the machine chain after
    # PLANNED_HANDOFF). Gated on planning.target_terminal.enabled; every
    # method below is unreachable with the flag off.
    # ------------------------------------------------------------------ #

    def _target_terminal_enabled(self) -> bool:
        """True iff the ``planning.target_terminal.enabled`` flag is on."""
        tt = getattr(self._deps.planning_config, "target_terminal", None)
        return bool(getattr(tt, "enabled", False))

    async def _enter_target_terminal(self, row: Any, correlation_id: str) -> bool:
        """Write the handoff file (the 007 input) and transition RUNNING → FEATURE_SPEC.

        The flag-ON analogue of :meth:`_handoff`'s branch write: it commits the
        SAME ``feature_spec_inputs/{cid}.md`` the fallback terminal would (so
        the 007 input is byte-identical to the fallback's handoff), but enters
        the machine chain instead of terminating. Returns True on success
        (drive() re-reads FEATURE_SPEC), False on a loud terminal failure.
        """
        deps = self._deps
        resolved = await self._resolve_repo(row, correlation_id, stage_label="target-terminal-enter")
        if resolved is None:
            return False
        target_repo, repo_path = resolved
        branch = f"planning/{correlation_id}"
        handoff_path = f"feature_spec_inputs/{correlation_id}.md"
        content = build_feature_spec_input_content(self._run_data(row, correlation_id))

        try:
            result = await deps.git_runner.prepare_branch_and_write(
                repo_path=repo_path,
                branch=branch,
                file_path=handoff_path,
                content=content,
            )
        except Exception as exc:  # noqa: BLE001 — write boundary, never crash the run
            return await self._fail_leg(
                correlation_id,
                "target-terminal-enter",
                f"handoff write raised {type(exc).__name__}: {exc}",
            )
        if result.status == "failed":
            return await self._fail_leg(
                correlation_id,
                "target-terminal-enter",
                f"handoff write failed: {result.stderr}",
            )

        refused = deps.store.transition(
            correlation_id=correlation_id,
            to_state=PlanningState.FEATURE_SPEC,
            actor_identity="planning-driver",
            stage_label="target-terminal-enter",
            expected_from_state=PlanningState.RUNNING,
            details_json=json.dumps(
                {
                    "target_repo": target_repo,
                    "repo_path": repo_path,
                    "branch": branch,
                    "handoff_path": handoff_path,
                }
            ),
        )
        if isinstance(refused, TransitionRefused):
            logger.warning(
                "planning driver: RUNNING→FEATURE_SPEC refused for %s (current=%s)",
                correlation_id,
                refused.current_state,
            )
            return False
        logger.info(
            "planning driver: run %s entered the target terminal (branch=%s)",
            correlation_id,
            branch,
        )
        return True

    async def _feature_spec_leg(self, row: Any, correlation_id: str) -> bool:
        """FEATURE_SPEC leg: dispatch 007, write the triple, normalize, advance.

        Returns True to keep driving (now FEATURE_PLAN), False on a loud
        terminal failure. Idempotent: a durable ``feature-spec`` approved event
        means the spec already landed (crash before the state advance) — the
        leg just re-advances without re-dispatching the specialist.
        """
        deps = self._deps
        if self._has_leg_event(correlation_id, _FEATURE_SPEC_STAGE):
            return self._advance_after_spec(correlation_id)

        if deps.dispatch_feature_spec is None or deps.normalize_feature_spec is None:
            return await self._fail_leg(
                correlation_id,
                _FEATURE_SPEC_STAGE,
                "target terminal ON but the spec leg collaborators "
                "(dispatch_feature_spec / normalize_feature_spec) are not wired",
            )
        resolved = await self._resolve_repo(row, correlation_id, stage_label=_FEATURE_SPEC_STAGE)
        if resolved is None:
            return False
        target_repo, repo_path = resolved
        branch = f"planning/{correlation_id}"
        plan_run_id = f"plan-{correlation_id}"
        spec_input = build_feature_spec_input_content(
            self._run_data(row, correlation_id)
        )

        try:
            result = await deps.dispatch_feature_spec(
                plan_run_id=plan_run_id,
                correlation_id=correlation_id,
                spec_input=spec_input,
            )
        except Exception as exc:  # noqa: BLE001 — dispatch boundary
            return await self._fail_leg(
                correlation_id,
                _FEATURE_SPEC_STAGE,
                f"007 dispatch raised {type(exc).__name__}: {exc}",
            )
        ok, reason = self._dispatch_ok(result)
        if not ok:
            return await self._fail_leg(
                correlation_id, _FEATURE_SPEC_STAGE, f"007 dispatch {reason}"
            )

        role_output = self._role_output_of(result)
        slug = self._slug_of(role_output, correlation_id)
        files = self._spec_triple_files(role_output, slug)
        if not files:
            return await self._fail_leg(
                correlation_id,
                _FEATURE_SPEC_STAGE,
                "007 returned no three-file spec contract (invalid artifacts)",
            )

        feature_rel = self._feature_file_rel(files)
        normalize = deps.normalize_feature_spec

        async def _pre_commit(worktree: Path) -> PreCommitResult:
            if feature_rel is None:
                return PreCommitResult(
                    ok=False, detail="spec contract has no .feature file to normalize"
                )
            outcome = await normalize(worktree, feature_rel)
            return PreCommitResult(ok=outcome.ok, detail=outcome.detail)

        try:
            gitres = await deps.git_runner.prepare_branch_and_write_tree(
                repo_path=repo_path,
                branch=branch,
                files=files,
                message=f"planning: feature spec for {correlation_id} (Lane B 007)",
                pre_commit=_pre_commit,
            )
        except Exception as exc:  # noqa: BLE001 — write boundary
            return await self._fail_leg(
                correlation_id,
                _FEATURE_SPEC_STAGE,
                f"spec write raised {type(exc).__name__}: {exc}",
            )
        if gitres.status == "failed":
            return await self._fail_leg(
                correlation_id,
                _FEATURE_SPEC_STAGE,
                f"spec write / normalizer failed: {gitres.stderr}",
            )

        deps.store._record_event(
            correlation_id=correlation_id,
            stage_label=_FEATURE_SPEC_STAGE,
            status="approved",
            actor_identity="planning-driver",
            details_json=json.dumps(
                {
                    "slug": slug,
                    "spec_files": sorted(files),
                    "target_repo": target_repo,
                    "repo_path": repo_path,
                    "branch": branch,
                    "sha": gitres.sha,
                }
            ),
        )
        logger.info(
            "planning driver: run %s feature-spec committed (slug=%s, %d files)",
            correlation_id,
            slug,
            len(files),
        )
        return self._advance_after_spec(correlation_id)

    def _advance_after_spec(self, correlation_id: str) -> bool:
        """Transition FEATURE_SPEC → FEATURE_PLAN (idempotent-resume safe)."""
        refused = self._deps.store.transition(
            correlation_id=correlation_id,
            to_state=PlanningState.FEATURE_PLAN,
            actor_identity="planning-driver",
            stage_label="feature-spec-complete",
            expected_from_state=PlanningState.FEATURE_SPEC,
        )
        if isinstance(refused, TransitionRefused):
            # A concurrent driver may already have advanced it — treat
            # FEATURE_PLAN (or a later target-terminal state) as success.
            current = self._deps.store.get_run(correlation_id)
            if current and PlanningState(current["state"]) in {
                PlanningState.FEATURE_PLAN,
                PlanningState.BUILD_QUEUED,
            }:
                return True
            logger.warning(
                "planning driver: FEATURE_SPEC→FEATURE_PLAN refused for %s (current=%s)",
                correlation_id,
                refused.current_state,
            )
            return False
        return True

    async def _feature_plan_leg(self, row: Any, correlation_id: str) -> bool:
        """FEATURE_PLAN leg: mint the FEAT id, dispatch 008, write + validate.

        Returns True once the plan tree is validated + committed (the caller
        proceeds to the B3 build trigger), False on a loud terminal failure. A
        durable ``feature-plan`` approved event short-circuits a re-drive
        (no re-dispatch — returns True so the build trigger still runs, which is
        what makes a crash between the plan commit and BUILD_QUEUED recover).
        """
        deps = self._deps
        if self._has_leg_event(correlation_id, _FEATURE_PLAN_STAGE):
            logger.info(
                "planning driver: run %s feature-plan already complete "
                "(idempotent re-drive — proceeding to the B3 build trigger)",
                correlation_id,
            )
            return True

        if deps.dispatch_feature_plan is None or deps.validate_feature_plan is None:
            await self._fail_leg(
                correlation_id,
                _FEATURE_PLAN_STAGE,
                "target terminal ON but the plan leg collaborators "
                "(dispatch_feature_plan / validate_feature_plan) are not wired",
            )
            return False
        resolved = await self._resolve_repo(row, correlation_id, stage_label=_FEATURE_PLAN_STAGE)
        if resolved is None:
            return False
        target_repo, repo_path = resolved
        branch = f"planning/{correlation_id}"
        plan_run_id = f"plan-{correlation_id}"
        spec_details = self._leg_event_details(correlation_id, _FEATURE_SPEC_STAGE)
        slug = str(spec_details.get("slug") or self._slug_of({}, correlation_id))
        feature_id = self._mint_feature_id(correlation_id)
        scope = str(row["request_text"] or "")

        try:
            result = await deps.dispatch_feature_plan(
                plan_run_id=plan_run_id,
                correlation_id=correlation_id,
                scope=scope,
                target_repo=target_repo,
                feature_id=feature_id,
            )
        except Exception as exc:  # noqa: BLE001 — dispatch boundary
            await self._fail_leg(
                correlation_id,
                _FEATURE_PLAN_STAGE,
                f"008 dispatch raised {type(exc).__name__}: {exc}",
            )
            return False
        ok, reason = self._dispatch_ok(result)
        if not ok:
            await self._fail_leg(
                correlation_id, _FEATURE_PLAN_STAGE, f"008 dispatch {reason}"
            )
            return False

        role_output = self._role_output_of(result)
        # RV-1: assert the SUPPLIED feature id, not filename self-consistency.
        declared = role_output.get("feature_id")
        if declared is not None and str(declared) != feature_id:
            await self._fail_leg(
                correlation_id,
                _FEATURE_PLAN_STAGE,
                f"feature id mismatch (RV-1): forge supplied {feature_id}, "
                f"the plan declares {declared}",
            )
            return False

        files = self._plan_tree_files(role_output)
        if not files:
            await self._fail_leg(
                correlation_id,
                _FEATURE_PLAN_STAGE,
                "008 returned no plan tree (invalid artifacts)",
            )
            return False

        validate = deps.validate_feature_plan

        async def _pre_commit(worktree: Path) -> PreCommitResult:
            outcome = await validate(worktree, feature_id)
            return PreCommitResult(ok=outcome.ok, detail=outcome.detail)

        try:
            gitres = await deps.git_runner.prepare_branch_and_write_tree(
                repo_path=repo_path,
                branch=branch,
                files=files,
                message=(
                    f"planning: feature plan {feature_id} for {correlation_id} "
                    "(Lane B 008)"
                ),
                pre_commit=_pre_commit,
            )
        except Exception as exc:  # noqa: BLE001 — write boundary
            await self._fail_leg(
                correlation_id,
                _FEATURE_PLAN_STAGE,
                f"plan write raised {type(exc).__name__}: {exc}",
            )
            return False
        if gitres.status == "failed":
            await self._fail_leg(
                correlation_id,
                _FEATURE_PLAN_STAGE,
                f"plan write / feature validate failed: {gitres.stderr}",
            )
            return False

        deps.store._record_event(
            correlation_id=correlation_id,
            stage_label=_FEATURE_PLAN_STAGE,
            status="approved",
            actor_identity="planning-driver",
            details_json=json.dumps(
                {
                    "feature_id": feature_id,
                    "slug": slug,
                    "plan_files": sorted(files),
                    "target_repo": target_repo,
                    "branch": branch,
                    "sha": gitres.sha,
                }
            ),
        )
        await self._notify(
            correlation_id,
            f"Planning run {correlation_id}: machine spec + plan complete and "
            f"validated (feature {feature_id}, branch {branch}); queueing the "
            "build.",
            level="info",
        )
        logger.info(
            "planning driver: run %s feature-plan validated (feature_id=%s); "
            "proceeding to the B3 build trigger",
            correlation_id,
            feature_id,
        )
        return True

    async def _build_trigger_leg(self, row: Any, correlation_id: str) -> bool:
        """B3 build trigger: queue the validated feature, advance to BUILD_QUEUED.

        On validate green (a durable ``feature-plan`` event exists) this queues
        the feature onto forge's OWN Mode B build dispatcher via the injected
        ``dispatch_build_trigger`` collaborator — the canonical MODE_B path
        (NOT the local guardkit CLI) whose pre-dispatch approval gate then
        pauses the build for the human tap and whose build-paused lifecycle
        event jarvis renders (the existing build-notification surface).

        The trigger is a fire-and-forget publish (no specialist round-trip), so
        it introduces no new unbounded wait (rule 5). Idempotent: a durable
        ``build-queued`` event means the feature was already queued (crash
        before the BUILD_QUEUED transition) — the leg just re-attempts the
        transition without re-publishing. Returns True on success (drive()
        re-reads BUILD_QUEUED, the target terminal), False on a loud failure.
        """
        deps = self._deps
        if self._has_leg_event(correlation_id, _BUILD_QUEUED_STAGE):
            return self._advance_to_build_queued(correlation_id)

        if deps.dispatch_build_trigger is None:
            return await self._fail_leg(
                correlation_id,
                _BUILD_QUEUED_STAGE,
                "target terminal ON but the build trigger collaborator "
                "(dispatch_build_trigger) is not wired",
            )

        plan_details = self._leg_event_details(correlation_id, _FEATURE_PLAN_STAGE)
        feature_id = str(plan_details.get("feature_id") or "")
        if not feature_id:
            return await self._fail_leg(
                correlation_id,
                _BUILD_QUEUED_STAGE,
                "no feature id recorded on the feature-plan leg — cannot queue "
                "the build",
            )
        target_repo = str(plan_details.get("target_repo") or "")
        branch = str(plan_details.get("branch") or f"planning/{correlation_id}")
        plan_files = plan_details.get("plan_files") or []
        plan_run_id = f"plan-{correlation_id}"

        try:
            result = await deps.dispatch_build_trigger(
                plan_run_id=plan_run_id,
                correlation_id=correlation_id,
                feature_id=feature_id,
                target_repo=target_repo,
                branch=branch,
                plan_files=list(plan_files),
                originating_user=row["originating_user"],
            )
        except Exception as exc:  # noqa: BLE001 — trigger boundary, never crash
            return await self._fail_leg(
                correlation_id,
                _BUILD_QUEUED_STAGE,
                f"build trigger raised {type(exc).__name__}: {exc}",
            )

        queued = bool(getattr(result, "queued", False))
        if not queued:
            reason = str(getattr(result, "reason", None) or "no reason supplied")
            return await self._fail_leg(
                correlation_id,
                _BUILD_QUEUED_STAGE,
                f"build trigger did not queue the feature: {reason}",
            )

        deps.store._record_event(
            correlation_id=correlation_id,
            stage_label=_BUILD_QUEUED_STAGE,
            status="approved",
            actor_identity="planning-driver",
            details_json=json.dumps(
                {
                    "feature_id": feature_id,
                    "build_id": getattr(result, "build_id", None),
                    "target_repo": target_repo,
                    "branch": branch,
                }
            ),
        )
        if not self._advance_to_build_queued(correlation_id):
            return False
        await self._notify(
            correlation_id,
            f"Planning run {correlation_id}: feature {feature_id} queued for "
            f"build on forge's Mode B pipeline (branch {branch}); paused at the "
            "build approval gate for the human tap.",
            level="info",
        )
        logger.info(
            "planning driver: run %s reached BUILD_QUEUED (feature_id=%s, "
            "branch=%s) — the target terminal is complete",
            correlation_id,
            feature_id,
            branch,
        )
        return True

    def _advance_to_build_queued(self, correlation_id: str) -> bool:
        """Transition FEATURE_PLAN → BUILD_QUEUED (idempotent-resume safe)."""
        refused = self._deps.store.transition(
            correlation_id=correlation_id,
            to_state=PlanningState.BUILD_QUEUED,
            actor_identity="planning-driver",
            stage_label=_BUILD_QUEUED_STAGE,
            expected_from_state=PlanningState.FEATURE_PLAN,
        )
        if isinstance(refused, TransitionRefused):
            current = self._deps.store.get_run(correlation_id)
            if current and PlanningState(current["state"]) is PlanningState.BUILD_QUEUED:
                return True
            logger.warning(
                "planning driver: FEATURE_PLAN→BUILD_QUEUED refused for %s "
                "(current=%s)",
                correlation_id,
                refused.current_state,
            )
            return False
        return True

    # -- target-terminal helpers ---------------------------------------- #

    def _run_data(self, row: Any, correlation_id: str) -> dict[str, Any]:
        """Assemble the run_data dict the handoff-content builder consumes."""
        po_output = self._latest_po_output(correlation_id)
        return {
            "correlation_id": correlation_id,
            "state": row["state"],
            "request_text": row["request_text"],
            "originating_user": row["originating_user"],
            "target_repo": row["target_repo"],
            "product_docs": po_output.get("docs_summary") or {},
        }

    async def _resolve_repo(
        self, row: Any, correlation_id: str, *, stage_label: str
    ) -> tuple[str, str] | None:
        """Resolve ``(target_repo, repo_path)`` or fail the run loudly."""
        cfg = self._deps.planning_config
        target_repo = row["target_repo"] or cfg.default_target_repo
        if target_repo is None:
            await self._fail_leg(
                correlation_id, stage_label, "no target repository configured"
            )
            return None
        repo_path = cfg.target_repo_paths.get(target_repo)
        if repo_path is None:
            await self._fail_leg(
                correlation_id,
                stage_label,
                f"target repo {target_repo} not in target_repo_paths",
            )
            return None
        return target_repo, repo_path

    async def _fail_leg(
        self, correlation_id: str, stage_label: str, reason: str
    ) -> bool:
        """Move the run to FAILED, notify, and return False (loud terminal)."""
        self._fail(correlation_id, stage_label=stage_label, reason=reason)
        await self._notify(
            correlation_id,
            f"Planning run {correlation_id} failed at {stage_label}: {reason}",
            level="error",
        )
        return False

    def _mint_feature_id(self, correlation_id: str) -> str:
        """Mint a deterministic ``FEAT-XXXX`` id for the plan (rule 6).

        Deterministic in ``correlation_id`` so a re-drive mints the SAME id,
        and validated through the identifier security boundary before it is
        threaded to the architect (008) and asserted on the returned plan.
        """
        digest = hashlib.sha1(correlation_id.encode("utf-8")).hexdigest()[:4].upper()
        return validate_feature_id(f"FEAT-{digest}")

    @staticmethod
    def _dispatch_ok(result: Any) -> tuple[bool, str]:
        """Map a StageDispatchResult onto ``(ok, reason)`` (M10 outcome shape)."""
        outcome = getattr(result, "outcome", None)
        value = str(getattr(outcome, "value", outcome or "error")).lower()
        if value in ("completed", "degraded"):
            return True, value
        reason = getattr(result, "reason", None) or "no reason supplied"
        return False, f"{value}: {reason}"

    @staticmethod
    def _role_output_of(result: Any) -> dict[str, Any]:
        """Project the specialist's ``role_output`` document as a dict (M10)."""
        ro = getattr(result, "role_output", None)
        return dict(ro) if isinstance(ro, Mapping) else {}

    @staticmethod
    def _slug_of(role_output: Mapping[str, Any], correlation_id: str) -> str:
        """Feature slug from the 007 result, or a deterministic fallback.

        The specialist MAY name the feature (``role_output['slug']``); forge
        sanitises it to a filesystem-safe token and otherwise falls back to a
        deterministic ``feature-{cid}`` (WS1's semantic-slug emitter is §9
        follow-on, not a B stage).
        """
        candidate = str(role_output.get("slug") or "").strip()
        if candidate and _SLUG_RE.fullmatch(candidate):
            return candidate
        return f"feature-{correlation_id}"

    @staticmethod
    def _spec_triple_files(
        role_output: Mapping[str, Any], slug: str
    ) -> dict[str, str] | None:
        """Project the three-file spec contract from the 007 role_output.

        Prefers an explicit ``files`` mapping (specialist-authored repo-relative
        paths); otherwise builds the canonical ``features/<slug>/`` triple from
        the ``feature`` / ``assumptions`` / ``summary`` fields. ``None`` when
        neither shape yields files (the invalid-artifacts failure path).
        """
        files = role_output.get("files")
        if isinstance(files, Mapping) and files:
            return {str(k): str(v) for k, v in files.items()}
        feature = role_output.get("feature")
        assumptions = role_output.get("assumptions")
        summary = role_output.get("summary")
        if feature and assumptions and summary:
            base = f"features/{slug}"
            return {
                f"{base}/{slug}.feature": str(feature),
                f"{base}/{slug}_assumptions.yaml": str(assumptions),
                f"{base}/{slug}_summary.md": str(summary),
            }
        return None

    @staticmethod
    def _plan_tree_files(role_output: Mapping[str, Any]) -> dict[str, str] | None:
        """Project the plan tree (feature/task YAML) from the 008 role_output."""
        files = role_output.get("files")
        if isinstance(files, Mapping) and files:
            return {str(k): str(v) for k, v in files.items()}
        return None

    @staticmethod
    def _feature_file_rel(files: Mapping[str, str]) -> str | None:
        """The repo-relative ``.feature`` path in a spec triple (or None)."""
        for rel in files:
            if rel.endswith(".feature"):
                return rel
        return None

    def _has_leg_event(self, correlation_id: str, stage_label: str) -> bool:
        """True iff a durable ``approved`` event exists for ``stage_label``."""
        for event in self._deps.store.list_events(correlation_id):
            if event["stage_label"] == stage_label and event["status"] == "approved":
                return True
        return False

    def _leg_event_details(
        self, correlation_id: str, stage_label: str
    ) -> dict[str, Any]:
        """Parsed details of the latest ``approved`` event for ``stage_label``."""
        latest: dict[str, Any] = {}
        for event in self._deps.store.list_events(correlation_id):
            if event["stage_label"] == stage_label and event["status"] == "approved":
                if event["details_json"]:
                    try:
                        latest = json.loads(event["details_json"]) or {}
                    except (json.JSONDecodeError, ValueError):
                        continue
        return latest

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _republish_pending(self, row: Any, plan_run_id: str) -> None:
        """Re-emit the persisted pending request (verbatim request_id)."""
        deps = self._deps
        correlation_id = row["correlation_id"]
        row = deps.store.get_run(correlation_id) or row
        pending_request_id = row["pending_approval_request_id"]
        if not pending_request_id:
            logger.warning(
                "planning driver: no pending request_id to re-emit for %s",
                correlation_id,
            )
            return
        envelope = build_planning_approval_envelope(
            request_id=pending_request_id,
            plan_run_id=plan_run_id,
            feature_id=plan_run_id,
            stage_label=_PRODUCT_DOCS_STAGE,
            summary_data=self._latest_po_output(correlation_id).get("docs_summary")
            or {"recovered": True},
            expected_approver=row["expected_approver"],
            attempt_count=self._attempt_from(pending_request_id),
            checkpoint_type="product_docs_recovered",
        )
        try:
            await deps.approval_publisher.publish_request(envelope)
            logger.info(
                "planning driver: re-emitted pending approval request %s for %s",
                pending_request_id,
                correlation_id,
            )
        except Exception:  # noqa: BLE001 — DDR-007
            logger.exception(
                "planning driver: re-emit failed for %s; pause persists",
                correlation_id,
            )

    def _phase_remaining(self, row: Any, cfg: "PlanningConfig") -> tuple[int, float]:
        """Return ``(phase, remaining_seconds)`` from durable anchors."""
        now = self._deps.clock()
        escalated_at = row["escalated_at"]
        if escalated_at is None:
            anchor_str = row["paused_at"]
            window = cfg.originator_wait_seconds
            phase = 1
        else:
            anchor_str = escalated_at
            window = cfg.escalated_wait_seconds
            phase = 2
        if anchor_str is None:
            # Corrupt row (PAUSED without paused_at) — treat the window
            # as starting now rather than timing out instantly.
            return phase, float(window)
        anchor = datetime.fromisoformat(anchor_str)
        elapsed = (now - anchor).total_seconds()
        return phase, window - elapsed

    def _policy(self, cfg: "PlanningConfig") -> EscalationPolicy:
        return EscalationPolicy(
            originator_wait_seconds=cfg.originator_wait_seconds,
            escalated_wait_seconds=cfg.escalated_wait_seconds,
            escalation_approver=cfg.escalation_approver or "",
            defer_cap=cfg.defer_cap,
        )

    @staticmethod
    def _attempt_from(request_id: str | None) -> int:
        if not request_id:
            return 0
        try:
            _, _, attempt = parse_request_id(request_id)
            return attempt
        except ValueError:
            return 0

    def _load_history(self, correlation_id: str) -> list[_HistoryEvent]:
        """Translate durable event rows into planner-shaped history."""
        history: list[_HistoryEvent] = []
        for event in self._deps.store.list_events(correlation_id):
            stage_label = event["stage_label"]
            status = event["status"]
            details: Mapping[str, Any] = {}
            if event["details_json"]:
                try:
                    details = json.loads(event["details_json"])
                except (json.JSONDecodeError, ValueError):
                    details = {}
            if stage_label == "product_owner" and status == "approved":
                history.append(
                    _HistoryEvent(
                        stage=StageClass.PRODUCT_OWNER,
                        status="approved",
                        details=details,
                    )
                )
            elif status == "checkpoint_cleared":
                history.append(
                    _HistoryEvent(
                        stage=StageClass.PRODUCT_OWNER,
                        status="checkpoint_cleared",
                        details={"stage_label": _PRODUCT_DOCS_STAGE, **details},
                    )
                )
        return history

    def _latest_po_output(self, correlation_id: str) -> dict[str, Any]:
        """Return the most recent recorded PO output (or empty)."""
        latest: dict[str, Any] = {}
        for event in self._deps.store.list_events(correlation_id):
            if event["stage_label"] == "product_owner" and event["status"] == (
                "approved"
            ):
                if event["details_json"]:
                    try:
                        details = json.loads(event["details_json"])
                        latest = details.get("po_output", {}) or {}
                    except (json.JSONDecodeError, ValueError):
                        continue
        return latest

    def _fail(self, correlation_id: str, *, stage_label: str, reason: str) -> None:
        refused = self._deps.store.transition(
            correlation_id=correlation_id,
            to_state=PlanningState.FAILED,
            actor_identity="planning-driver",
            stage_label=stage_label,
            error=reason,
        )
        if isinstance(refused, TransitionRefused):
            logger.warning(
                "planning driver: FAILED transition refused for %s "
                "(current=%s, reason=%s)",
                correlation_id,
                refused.current_state,
                reason,
            )

    async def _notify(
        self, correlation_id: str, message: str, *, level: str = "info"
    ) -> None:
        """Best-effort originator notification (DDR-007)."""
        publish = self._deps.publish_notification
        if publish is None:
            return
        try:
            await publish(correlation_id, message, level)
        except Exception:  # noqa: BLE001 — notifications never block the chain
            logger.warning(
                "planning driver: notification publish failed for %s (best-effort)",
                correlation_id,
            )

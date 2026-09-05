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
   - :func:`sweep_interrupted_planning_runs`: re-drives QUEUED / RUNNING /
     FEATURE_SPEC / FEATURE_PLAN runs through the re-entrant driver
     (RT-05 / RT-08) — every non-terminal state rearm does not own.

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
from functools import partial
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from forge.adapters.nats.envelope_subscribe import EnvelopeSubscribeClient
from forge.adapters.nats.planning_consumer import (
    PLANNING_DURABLE_NAME,
    PLANNING_QUEUED_SUBJECT_FILTER,
    PlanningConsumerDeps,
    create_and_start_planning_run,
    handle_planning_message,
)
from forge.adapters.sqlite import connect_writer
from forge.planning.audit import audit_planning_model_resolution
from forge.planning.gate_adapters import build_planning_gate_adapters
from forge.planning.notifications import build_planning_notification_envelope
from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.work_queue_loop import (
    Admission,
    WorkQueueLoop,
    count_in_flight,
    paused_repositories,
)
from forge.planning.work_queue_store import WorkQueueStore
from forge.planning.states import PlanningState
from forge.preflight import run_resource_preflight

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
# Target-terminal wire-arg builders (Lane B / Phase E1 B2)
#
# CONTRACT OF RECORD — the specialist mode registration files, whose
# ``required_args`` these builders MUST reproduce exactly (a drift here is a
# live-run failure, not a test failure: run 8c4e156f rejected at the
# feature-spec leg because forge sent ``spec_input`` where 007 requires
# ``from_input``):
#   007  specialist-agent/src/specialist_agent/roles/product_owner/modes/
#        feature_spec.py → required_args=("from_input",)
#        (``from_input`` = the approved feature_spec_inputs/<id>.md CONTENT;
#         optional: context, stack, revision_of, validate_feedback — the last
#         two are EMITTED on a rewrite round: ``validate_feedback`` carries the
#         owner's note on the spec digest VERBATIM and ``revision_of`` the prior
#         artifact set. A first-round dispatch emits neither.)
#   008  specialist-agent/src/specialist_agent/roles/architect/modes/
#        feature_plan.py → required_args=("feature_id","spec_feature",
#        "spec_summary","target_repo_descriptor")
#        (``feature_id`` = the SUPPLIED minted id reproduced verbatim (RV-1);
#         ``spec_feature``/``spec_summary`` = the 007 .feature/_summary.md
#         CONTENTS; ``target_repo_descriptor`` = the {repo, test_roots, ...}
#         object (TARGET_REPO_DESCRIPTOR_SCHEMA); optional: ``spec_assumptions``
#         = the 007 _assumptions.yaml content, ``spec_feature_paths`` = the
#         repo-relative path(s) the .feature is committed at on the planning
#         branch, revision_of, validate_feedback)
#        ``spec_feature_paths`` is OPTIONAL ON BOTH SIDES ON PURPOSE. The two
#        repositories ship as separate images and either can be redeployed
#        first; a REQUIRED argument would mean that in one of those two orders
#        every plan is refused pre-model until the second deploy lands, which is
#        the failure this whole note exists to prevent. Optional, the argument
#        is inert until both sides carry it, in either order, with no window in
#        which planning is down.
# The contract-pin test (tests/forge/planning/test_target_terminal_contract_pin)
# asserts the literal arg-name sets these emit against those files.
# ---------------------------------------------------------------------------


def build_feature_spec_command_args(
    *,
    from_input: str,
    revision_of: dict[str, str] | None = None,
    validate_feedback: str | None = None,
) -> dict[str, Any]:
    """Exact ``po_feature_spec`` (007) wire args. See the CONTRACT note above.

    The one required key, plus the two OPTIONAL revision keys when — and only
    when — this is a rewrite. A first-round dispatch emits neither and is
    byte-identical to the call that shipped.

    ``validate_feedback`` is the owner's own note, VERBATIM: the mode's own
    description of that argument is "review-gate failure text driving the
    revision", and a note on a spec digest is precisely a review-gate response.
    It is never summarised or reworded on the way — they said it once, and the
    machine reads what they said. ``revision_of`` is the prior artifact set, so
    the rewrite starts from what the spec-writer actually wrote.
    """
    args: dict[str, Any] = {"from_input": from_input}
    if revision_of:
        args["revision_of"] = dict(revision_of)
    if validate_feedback is not None and str(validate_feedback).strip():
        args["validate_feedback"] = validate_feedback
    return args


def build_feature_plan_command_args(
    *,
    feature_id: str,
    spec_feature: str,
    spec_summary: str,
    target_repo_descriptor: dict[str, Any],
    spec_assumptions: str | None = None,
    spec_feature_paths: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Exact ``architect_feature_plan`` (008) wire args. See the CONTRACT note above.

    Only the four required keys plus the two optional ones when non-blank —
    never a field the schema does not define.

    ``spec_feature_paths`` (2026-08-22) is WHERE the specification sits on the
    planning branch, beside ``spec_feature``, which is WHAT it says. The plan
    YAML must declare that location under ``feature_files:``, and until this
    argument existed forge never told the plan-writer what it was: measured over
    eleven captured planning runs, ten plans wrote the key and SIX of those ten
    named a folder that does not exist, each one a folder name built out of the
    feature's title. Forge committed those files itself one leg earlier, so it is
    the party that knows. Blank / empty is omitted entirely: an empty list is not
    a location, and a caller with nothing to say says nothing.
    """
    args: dict[str, Any] = {
        "feature_id": feature_id,
        "spec_feature": spec_feature,
        "spec_summary": spec_summary,
        "target_repo_descriptor": dict(target_repo_descriptor),
    }
    if spec_assumptions is not None and str(spec_assumptions).strip():
        args["spec_assumptions"] = spec_assumptions
    paths = [str(p).strip() for p in (spec_feature_paths or ()) if str(p).strip()]
    if paths:
        args["spec_feature_paths"] = paths
    return args


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
    """Adapt CorrelationRegistry to TimeoutCoordinator's narrower surface.

    The coordinator owns the primary cut-off (``asyncio.timeout``) but
    ALSO hands us the per-leg budget as an independent backstop (M12).
    We pass it straight through to ``CorrelationRegistry.wait_for_reply``,
    whose ``asyncio.wait_for(asyncio.shield(future), timeout_seconds)``
    then has its OWN timer to terminate the wait — so the hard cut-off no
    longer depends on the coordinator's cancellation surviving the
    registry's ``asyncio.shield`` + ``except CancelledError: return None``
    absorbing layers. Pinning the inner wait open at ``1e9`` (the prior
    behaviour) wedged a dispatch forever when that cancellation was lost
    (observed live 2026-07-11, dfmt3: the 3600s cut-off never fired).

    ``timeout_seconds`` should always be supplied by the current
    coordinator; if a caller omits it we fall back to the configured
    ceiling rather than ``1e9`` so a finite backstop is never lost.
    """

    def __init__(self, registry: Any, backstop_seconds: float) -> None:
        self._registry = registry
        # The finite fallback used only if a caller ever omits the per-leg
        # budget — never ``1e9``, so a backstop is never lost.
        self._backstop_seconds = float(backstop_seconds)

    async def wait_for_reply(
        self, binding: Any, timeout_seconds: float | None = None
    ) -> Any:
        budget = (
            self._backstop_seconds if timeout_seconds is None else timeout_seconds
        )
        return await self._registry.wait_for_reply(
            binding, timeout_seconds=budget
        )

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


def _select_feature_yaml(plan_files: list[str], feature_id: str) -> str | None:
    """Pick the feature-level YAML from a committed plan tree (B3).

    The 008 plan tree carries feature/task/wave YAML; the Mode B build intake
    needs the feature YAML path (repo-relative, resolved against the branch
    checkout). Prefer the YAML named after the forge-minted ``feature_id``
    (``.../{feature_id}.yaml``), then any YAML under a ``features/`` directory,
    then the first YAML. ``None`` when the tree carries no YAML at all (a loud
    build-trigger failure — the plan tree is malformed). The WS1 emitter (§9)
    will make this deterministic; until then this is the best-effort selector.
    """
    yamls = [f for f in plan_files if f.endswith((".yaml", ".yml"))]
    if not yamls:
        return None
    for rel in yamls:
        if Path(rel).stem == feature_id:
            return rel
    for rel in yamls:
        if rel.startswith("features/") or "/features/" in rel:
            return rel
    return yamls[0]


def _resolve_feature_yaml_path(
    feature_yaml: str, target_repo: str, target_repo_paths: dict[str, str]
) -> str:
    """Resolve the repo-relative plan YAML to an ABSOLUTE ``feature_yaml_path``.

    B4 round-14 second-fault fix. The Mode B build intake's path allowlist
    (:func:`forge.adapters.nats.pipeline_consumer._path_inside_allowlist`)
    calls :meth:`pathlib.Path.resolve` on the candidate; a repo-relative
    value would resolve against the daemon process CWD — semantically wrong
    and pass/fail by accident.

    The dispatch side does NOT consume ``feature_yaml_path`` to locate the
    spec — the autobuild executor
    (:func:`forge.subagents.autobuild_runner._resolve_repo_path`) derives the
    checkout from ``payload.repo`` (``<FORGE_REPO_BASE>/<basename>``) and runs
    ``guardkit autobuild feature <feature_id>`` there. So the ONLY meaningful
    consumer of ``feature_yaml_path`` is the allowlist gate, and the
    live-proven CLI trigger (``forge queue``) sends an operator-supplied
    ABSOLUTE path. We mirror that: join the repo-relative plan path onto the
    configured local checkout root for ``target_repo``
    (``planning.target_repo_paths`` — the map that already declares each
    repo's absolute working copy for handoff-to-local-build), so the gate
    validates the feature YAML lives inside an authorised checkout.

    When the repo is unmapped or the path is already absolute the value is
    returned unchanged — an unmapped repo then fails the allowlist gate
    loudly (the correct outcome), never resolving against the daemon CWD by
    accident.
    """
    if Path(feature_yaml).is_absolute():
        return feature_yaml
    checkout = target_repo_paths.get(target_repo)
    if not checkout:
        return feature_yaml
    return str(Path(checkout) / feature_yaml)


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
        from forge.planning.target_terminal_tools import (
            make_normalize_feature_spec,
            make_normalize_stamps,
            make_validate_feature_plan,
            make_validate_gate_registry,
            make_validate_pass_bar,
        )

        clock_fn = clock if clock is not None else lambda: datetime.now(timezone.utc)

        # -- durable store + gate adapters (TASK-MP-002/004A) ------------
        # Lane B / Phase E1 (B1): the store selects its transition table from
        # the target-terminal flag at construction. Flag OFF (default) = the
        # shipped table; PLANNED_HANDOFF stays terminal.
        pool = connect_writer(db_path)
        store = SqlitePlanningRunStore(
            pool,
            target_terminal_enabled=config.planning.target_terminal.enabled,
        )
        repository, state_machine = build_planning_gate_adapters(store, clock=clock_fn)
        # The work queue (Lane B stage one) shares the same connection as the
        # planning store: one writer, one database, one transaction discipline.
        queue_store = WorkQueueStore(pool, clock=clock_fn)

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
        # Planning-only override of the ASSUM-003 900s dispatch ceiling
        # (Mode B compositions keep the default). A REAL product-owner
        # greenfield session measured ~50-70 min of agentic turns on the
        # estate workhorse (Mode-P DISPATCHFMT Sfinal, 2026-07-11) —
        # 900s degrades every healthy planning dispatch. 3600s matches
        # the planning durable's ack_wait and the originator checkpoint
        # wait; a longer stage is a specialist-latency problem, not a
        # dispatch-budget one.
        planning_dispatch_budget_seconds = 3600.0
        timeout_coordinator = TimeoutCoordinator(
            # Hand the adapter the same ceiling as a finite fallback budget
            # so the registry keeps an independent backstop timer even if a
            # per-leg budget were ever omitted (M12 — never pin at 1e9).
            registry=_RegistryWaitAdapter(
                registry, backstop_seconds=planning_dispatch_budget_seconds
            ),
            clock=SystemClock(),
            default_timeout_seconds=planning_dispatch_budget_seconds,
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

        # -- target terminal legs (Lane B / Phase E1 B2) ------------------
        # The 007 (po_feature_spec) and 008 (architect_feature_plan) legs ride
        # the SAME specialist dispatch surface + M12 planning budget as the PO
        # leg above; forge supplies the leg inputs via extra_command_args built
        # by the pinned wire-arg builders (the specialist contract of record):
        # ``from_input`` for 007; ``feature_id`` + the 007 spec triple CONTENTS
        # (``spec_feature``/``spec_summary``[/``spec_assumptions``]) + the
        # structured ``target_repo_descriptor`` for 008 (RV-1: the plan leg
        # asserts the SUPPLIED id; the driver reads the committed spec contents
        # back off the branch and builds the descriptor from the target repo).
        async def dispatch_feature_spec(
            *,
            plan_run_id: str,
            correlation_id: str,
            spec_input: str,
            revision_of: dict[str, str] | None = None,
            validate_feedback: str | None = None,
        ) -> Any:
            return await dispatch_specialist_stage(
                stage=StageClass.FEATURE_SPEC,
                build_id=plan_run_id,
                correlation_id=correlation_id,
                forward_context_builder=forward_context_builder,
                dispatch_surface=orchestrator,
                stage_log_writer=stage_log_writer,
                feature_id=plan_run_id,
                extra_command_args=build_feature_spec_command_args(
                    from_input=spec_input,
                    revision_of=revision_of,
                    validate_feedback=validate_feedback,
                ),
            )

        async def dispatch_feature_plan(
            *,
            plan_run_id: str,
            correlation_id: str,
            feature_id: str,
            spec_feature: str,
            spec_summary: str,
            target_repo_descriptor: dict[str, Any],
            spec_assumptions: str | None = None,
            spec_feature_paths: Sequence[str] | None = None,
        ) -> Any:
            return await dispatch_specialist_stage(
                stage=StageClass.FEATURE_PLAN,
                build_id=plan_run_id,
                correlation_id=correlation_id,
                forward_context_builder=forward_context_builder,
                dispatch_surface=orchestrator,
                stage_log_writer=stage_log_writer,
                feature_id=plan_run_id,
                extra_command_args=build_feature_plan_command_args(
                    feature_id=feature_id,
                    spec_feature=spec_feature,
                    spec_summary=spec_summary,
                    target_repo_descriptor=target_repo_descriptor,
                    spec_assumptions=spec_assumptions,
                    spec_feature_paths=spec_feature_paths,
                ),
            )

        # -- the build trigger (Lane B / Phase E1 B3) ---------------------
        # On validate green forge queues the feature onto its OWN Mode B build
        # intake (``pipeline.build-queued.{feature_id}``) — the canonical
        # MODE_B dispatcher (reaching ``dispatch_autobuild_async`` via the build
        # daemon, NOT the local guardkit CLI). The daemon's pre-dispatch
        # approval gate (``maybe_gate_build``) then pauses the build for the
        # human tap, and jarvis renders the build-paused lifecycle event on the
        # existing build-notification surface. Fire-and-forget publish: no
        # specialist round-trip, so no new unbounded wait (rule 5).
        from forge.planning.driver import BuildTriggerResult

        async def dispatch_build_trigger(
            *,
            plan_run_id: str,
            correlation_id: str,
            feature_id: str,
            target_repo: str,
            branch: str,
            plan_files: list[str],
            originating_user: str | None,
        ) -> BuildTriggerResult:
            from nats_core.envelope import EventType, MessageEnvelope
            from nats_core.events import BuildQueuedPayload

            from forge.lifecycle.modes import BuildMode

            feature_yaml = _select_feature_yaml(plan_files, feature_id)
            if feature_yaml is None:
                logger.error(
                    "planning target terminal: no feature YAML in the committed "
                    "plan tree for %s (files=%s) — cannot queue the Mode B build",
                    correlation_id,
                    plan_files,
                )
                return BuildTriggerResult(
                    queued=False,
                    reason="no feature YAML in the committed plan tree",
                )
            now = clock_fn()
            # B4 round-14: emit an ABSOLUTE feature_yaml_path (resolved
            # against the configured local checkout for ``target_repo``) so
            # the Mode B intake's path allowlist validates it meaningfully —
            # a repo-relative value resolves against the daemon CWD by
            # accident. See :func:`_resolve_feature_yaml_path`.
            feature_yaml_path = _resolve_feature_yaml_path(
                feature_yaml, target_repo, config.planning.target_repo_paths
            )
            # ``triggered_by`` / ``originating_adapter`` are constrained wire
            # literals — the target-terminal build trigger is a forge-internal
            # machine dispatch (no user-facing adapter), so it uses
            # ``forge-internal`` and omits the adapter.
            payload = BuildQueuedPayload(
                feature_id=feature_id,
                repo=target_repo,
                branch=branch,
                feature_yaml_path=feature_yaml_path,
                triggered_by="forge-internal",
                originating_user=originating_user,
                correlation_id=correlation_id,
                requested_at=now,
                queued_at=now,
                mode=BuildMode.MODE_B.value,
            )
            envelope = MessageEnvelope(
                source_id="forge",
                event_type=EventType.BUILD_QUEUED,
                correlation_id=correlation_id,
                payload=payload.model_dump(mode="json"),
            )
            subject = f"pipeline.build-queued.{feature_id}"
            await nats_client.publish(
                subject, envelope.model_dump_json().encode("utf-8")
            )
            logger.info(
                "planning target terminal: queued Mode B build for %s "
                "(feature %s, branch %s) on %s — the pre-dispatch approval gate "
                "will pause it for the human tap",
                correlation_id,
                feature_id,
                branch,
                subject,
            )
            return BuildTriggerResult(queued=True, build_id=None)

        # The two deterministic oracles forge runs against the committed
        # artifacts (bounded subprocesses; frozen guardkit `feature validate`).
        # ``command_prefix=None`` requests dual-candidate module resolution so the
        # normalizer works whether guardkit is wheel-installed
        # (``guardkit._installer_core.*``) or a source checkout
        # (``installer.core.*``) — the B4 run 4b3b0893 in-container gap.
        normalize_feature_spec = make_normalize_feature_spec(command_prefix=None)
        validate_feature_plan = make_validate_feature_plan()
        # THE STAMP NORMALIZER (Rich's condition 1, 2026-08-16): ``guardkit qa
        # normalize-stamps`` runs against the planning worktree immediately
        # BEFORE the plan-commit validate, so the rule-minted verifier stamps
        # are WRITTEN on the planning branch and ride the plan commit. Same
        # frozen guardkit seam; an older guardkit without the subcommand
        # continues (receipted) until the rebake.
        normalize_stamps = make_normalize_stamps()
        # B4 round-19: the per-task pass bars forge mints from the 007 seed are
        # validated by guardkit's OWN ``qa validate pass-bar`` before they land.
        validate_pass_bar = make_validate_pass_bar()
        # F2: the per-feature live gate forge fills + its appended registry entry
        # are validated by guardkit's OWN ``qa validate gate-registry`` before
        # they land.
        validate_gate_registry = make_validate_gate_registry()

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
            correlation_id: str,
            message: str,
            level: str = "info",
            *,
            mention: bool = True,
            parent_request_id: str | None = None,
        ) -> None:
            # Assumption-dialogue projection (TASK-SPL003F-001): project the
            # durable thread anchor + originator so jarvis threads the message
            # into the originating conversation. Read from the planning_runs
            # row (never re-derived); degrade to None when absent (still
            # visible, unthreaded — never dropped).
            #
            # ``mention=False`` (the stamp normalizer's un-enforced line,
            # coordinator condition 5): the line still THREADS (the anchor is
            # kept) but carries no ``target_user``, so jarvis renders it plain
            # — no @mention. jarvis's build-audience record ignores a None
            # target_user, so the run's recorded owner is untouched.
            #
            # ``parent_request_id`` passed in by the caller is the anchor for a
            # message that has NO planning run yet — a queue reply. The queue
            # row carries the person, so the mention still works; the thread
            # anchor comes from the message that is being answered.
            anchor: str | None = None
            target_user: str | None = None
            row = store.get_run(correlation_id)
            if row is not None:
                anchor = row["parent_request_id"]
                target_user = row["originating_user"] if mention else None
            else:
                queued = queue_store.get_by_correlation_id(correlation_id)
                if queued is not None and mention:
                    target_user = queued["originating_user"]
            if anchor is None:
                anchor = parent_request_id

            envelope = build_planning_notification_envelope(
                correlation_id=correlation_id,
                message=message,
                level=level,
                parent_request_id=anchor,
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
                # O-27/O-29 (E2-S4) — pre-run memory/disk headroom preflight,
                # bound to a zero-arg callable so the driver stays ignorant of
                # /proc + shutil. Defaults enabled=True (refuses only BEFORE a
                # run starts, never a mid-run kill).
                resource_preflight=partial(
                    run_resource_preflight, config.resource_preflight
                ),
                # Lane B / Phase E1 (B2) — target-terminal legs (no-op unless
                # planning.target_terminal.enabled is on).
                dispatch_feature_spec=dispatch_feature_spec,
                dispatch_feature_plan=dispatch_feature_plan,
                normalize_feature_spec=normalize_feature_spec,
                validate_feature_plan=validate_feature_plan,
                normalize_stamps=normalize_stamps,
                validate_pass_bar=validate_pass_bar,
                # Lane B / Phase E1 (F2) — the per-feature live-gate registration
                # leg (sibling of the pass-bar leg; no-op unless the endpoint is
                # derivable AND the repo carries the qa/gates/ surface).
                validate_gate_registry=validate_gate_registry,
                # Lane B / Phase E1 (B3) — the Mode B build trigger.
                dispatch_build_trigger=dispatch_build_trigger,
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

        async def _notify_in_thread(
            correlation_id: str,
            message: str,
            *,
            parent_request_id: str | None = None,
        ) -> None:
            await publish_planning_notification(
                correlation_id, message, "info", parent_request_id=parent_request_id
            )

        consumer_deps = PlanningConsumerDeps(
            store=store,
            publish_notification=_notify_in_thread,
            on_recorded=_on_recorded,
            # Lane B stage one: a sentence becomes a queue row here, and the
            # take-next loop creates the planning run later.
            queue_store=queue_store,
            # The intake resolves the repository a sentence names against
            # planning.target_repo_paths and refuses a name it does not know
            # (2026-09-05 rules 3 and 4).
            planning_config=config.planning,
        )

        # -- the take-next loop (Lane B stage one, contracts 6-8) ----------
        # A sibling supervised task beside the intake consumer: every ten
        # seconds it closes what has finished, asks about a broken chain, and
        # admits the next sentence by creating its planning run in process,
        # under the sentence's own correlation id.
        async def _start_queued_run(admission: Admission) -> None:
            await create_and_start_planning_run(
                consumer_deps,
                correlation_id=admission.correlation_id,
                request_text=admission.request_text,
                originating_user=admission.originating_user,
                triggered_by=admission.triggered_by,
                originating_adapter=admission.originating_adapter,
                parent_request_id=admission.parent_request_id,
                target_repo=admission.target_repo,
            )

        # -- the fix branch (conductor rewire rules 2 and 4) ---------------
        # A repair row does NOT start a planning run: it opens a fix journey
        # — a mode-c build — through the same admission the CLI's
        # ``forge queue --mode c`` goes through, in process and never as a
        # shell-out. It is shut by default: ``conductor.admit_fix_rows`` is
        # False, so repair rows sit in the queue where they can be read and
        # the "next I'd pick" line can name them, and nothing starts.
        from forge.config.models import FIX_JOURNEY_PROFILE_NAME
        from forge.lifecycle.persistence import SqliteLifecyclePersistence
        from forge.pipeline.fix_admission import (
            admit_fix_row,
            republish_build_queued,
        )

        lifecycle_pool = SqliteLifecyclePersistence(
            connection=pool, db_path=db_path
        )

        def _fix_build(correlation_id: str) -> Any:
            """The BUILD a repair row started, read the way a run is read."""
            try:
                return pool.execute(
                    "SELECT * FROM builds WHERE correlation_id = ? "
                    "ORDER BY queued_at DESC LIMIT 1",
                    (correlation_id,),
                ).fetchone()
            except Exception as exc:  # noqa: BLE001 — a read never stops the loop
                logger.warning(
                    "work queue: could not read the build for %s (%s)",
                    correlation_id,
                    exc,
                )
                return None

        async def _publish_build_queued(subject: str, body: bytes) -> None:
            await nats_client.publish(subject, body)

        async def _start_fix_journey(admission: Admission) -> None:
            await admit_fix_row(
                config=config,
                persistence=lifecycle_pool,
                store=queue_store,
                queue_id=admission.queue_id,
                correlation_id=admission.correlation_id,
                sentence=admission.request_text,
                target_repo=admission.target_repo,
                originating_user=admission.originating_user,
                publish=_publish_build_queued,
                profile=FIX_JOURNEY_PROFILE_NAME,
                clock=clock_fn,
            )

        async def _republish_fix_build(build: Any) -> None:
            """Say a written-but-never-announced build's queued event again.

            The write comes before the publish, so a publish that failed
            leaves a build row the pipeline was never told about. The event is
            rebuilt from that row, so it is the same event on the same
            subject under the same correlation id.
            """
            await republish_build_queued(build, publish=_publish_build_queued)

        queue_loop = WorkQueueLoop(
            queue_store,
            count_in_flight=lambda: count_in_flight(pool),
            planning_run=store.get_run,
            paused_repositories=lambda: paused_repositories(pool),
            start_run=_start_queued_run,
            notify=_notify_in_thread,
            max_in_flight=config.queue.max_in_flight,
            stale_after_days=config.queue.stale_after_days,
            clock=clock_fn,
            start_fix=_start_fix_journey,
            fix_build=_fix_build,
            republish_build=_republish_fix_build,
            admit_fix_rows=config.conductor.admit_fix_rows,
        )
        queue_loop_task = asyncio.create_task(queue_loop.run())
        _supervise(queue_loop_task, "work-queue-take-next")
        logger.info(
            "planning composition: the work queue is live — one sentence at a "
            "time up to %d in flight, order %s; repairs are %s",
            config.queue.max_in_flight,
            config.queue.order,
            (
                "STARTED by the queue"
                if config.conductor.admit_fix_rows
                else "filed and left in the queue (conductor.admit_fix_rows "
                "is off)"
            ),
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
    3. FEATURE_SPEC / FEATURE_PLAN runs (crash mid target-terminal chain,
       Lane B): re-driven through the SAME re-entrant driver, which
       resumes those states directly from durable history (every leg is
       idempotent on its own durable event). Without this a run killed
       inside the machine chain is enumerated by NOBODY — not here, not by
       :func:`rearm_paused_planning_runs` (PAUSED only) — and sits forever
       with no terminal and no notification. That is exactly how a daemon
       restart used to strand a live auth-confirmation door: the owner's
       card stayed on screen and their tap went into the void.

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

        # Every NON-terminal, NON-PAUSED state (PAUSED is rearm's, and only
        # rearm re-emits the persisted checkpoint card). FEATURE_SPEC /
        # FEATURE_PLAN are inert when the target terminal is off — those states
        # are unreachable — so this stays a no-op for the flag-off posture.
        interrupted = [
            (state.value, run)
            for state in (
                PlanningState.QUEUED,
                PlanningState.RUNNING,
                PlanningState.FEATURE_SPEC,
                PlanningState.FEATURE_PLAN,
            )
            for run in store.list_runs_by_state(state)
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

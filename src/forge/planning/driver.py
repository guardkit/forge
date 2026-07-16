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

import yaml

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
        ValidateGateRegistryFn,
        ValidatePassBarFn,
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
#: Durable stage label for the per-task QA pass-bar registration leg (B4 round-19,
#: Rich-ratified). Its presence makes the leg idempotent on a re-drive: a crash
#: between the bars commit and the BUILD_QUEUED transition never re-mints them.
_QA_PASS_BARS_STAGE = "qa-pass-bars"
#: Durable stage label for the per-feature live-gate REGISTRATION leg (F2 —
#: sibling of the pass-bar leg). Its presence makes the leg idempotent on a
#: re-drive: a crash between the gate commit and the BUILD_QUEUED transition
#: never re-fills the gate. An HONEST skip (non-endpoint feature, or a repo that
#: has not adopted the qa/gates/ surface) ALSO records this label (a skipped
#: detail), so a re-drive of a legitimately-skipped run is a clean no-op too.
_QA_FEATURE_GATE_STAGE = "qa-feature-gate"

#: The target repo's OWN feature-behaviour gate TEMPLATE + gate registry, read
#: off the planning branch (never fabricated forge-side). Their ABSENCE is an
#: honest skip (the repo has not adopted the F4 gate surface), never a failure.
_FEATURE_GATE_TEMPLATE_REL = "qa/gates/feature_behaviour_gate.py"
_GATE_REGISTRY_REL = "qa/gates/registry.yaml"

#: The two placeholder literals the forge fill substitutes in the target repo's
#: OWN template SPEC block (guardkit api_test ``feature_behaviour_gate.py``). Each
#: must appear EXACTLY once; a count mismatch is a loud fill failure (the repo's
#: template drifted from the shape forge fills — never a silent wrong gate). The
#: ``/REPLACE_ME`` guard line elsewhere in the template is a DIFFERENT literal and
#: is deliberately left intact so an unedited copy still fails honestly at runtime.
_GATE_TEMPLATE_GATE_ID_LITERAL = '"gate_id": "feature-behaviour",'
_GATE_TEMPLATE_REQUEST_LITERAL = (
    '"request": {"method": "GET", "path": "/REPLACE_ME"},'
)

#: F2 endpoint-derivation grammar (deterministic, conservative — the design law).
#: Parse ONLY machine-class criterion text: a capital ``A``, an uppercase HTTP
#: verb, ``request to``, then an explicit ``/``-rooted path. Case-sensitive (no
#: IGNORECASE) so mid-sentence prose ("you can get request info") and mixed-case
#: never match; the leading ``\bA `` anchors the SPL criterion phrasing. Only a
#: GET yields a v1 gate (expect_status 200); any other verb, a missing path, or a
#: non-match yields None → an honest skip (never a guessed success status).
_FEATURE_GATE_ENDPOINT_RE = re.compile(
    r"\bA (?P<method>GET|POST|PUT|PATCH|DELETE) request to "
    r"(?P<path>/[A-Za-z0-9_/{}-]*)\b"
)


class _FeatureGateFillError(Exception):
    """A derivable feature gate could not be filled / its registry appended.

    Raised by the F2 fill helpers on template drift or an unparseable/empty
    registry — the caller maps it to a LOUD ``_fail_leg`` (SKIP-vs-FAIL law (c):
    template present + derivable but the fill fails is a failure, never a skip).
    """

#: The 007 native artifact-map key convention for the feature-grain quality-bar
#: SEED (specialist-agent product_owner/modes/feature_spec.py; guardkit
#: ``/feature-spec`` §"Quality-bar seed emission"): ``pass-bar-seed-*.yaml``. It
#: is a tolerated extra in the spec map — NEVER a committed spec-triple file —
#: that forge CAPTURES at the spec commit and specialises into per-task bars at
#: plan-commit (the guardkit ``/feature-plan`` "consumes this seed" contract,
#: done forge-side because the machine 008 leg does not emit the per-task bars).
_PASS_BAR_SEED_PREFIX = "pass-bar-seed-"
_PASS_BAR_SEED_SUFFIX = ".yaml"

#: The negative path mandatory for EVERY pass bar, auth-surface-bearing or not
#: (guardkit PB-14 / :data:`guardkit.qa.formats.pass_bar.UNIVERSAL_NEGATIVE_PATHS`).
#: An authless per-task bar (``auth_surface_bearing: false`` by construction on
#: this path) needs exactly this one to satisfy guardkit's own schema — the seed
#: itself carries no ``negative_paths``, so forge supplies the universal minimum.
_UNIVERSAL_NEGATIVE_PATH = "dependency_down_degradation"

#: The pass-bar schema version forge mints (guardkit
#: ``PassBar.CURRENT_FORMAT_VERSION``). Carried from the seed when present.
_PASS_BAR_FORMAT_VERSION = "2.0"

#: The contract reference an auth-surface-bearing seed refusal names verbatim.
_SPL_007_AUTH_CLAUSE = "SPL-007 §A.2"

#: Filesystem-safe feature slug allowlist (mirrors the identifier boundary):
#: a specialist-supplied slug is only trusted when it matches, else forge falls
#: back to a deterministic ``feature-{cid}``.
_SLUG_RE = re.compile(r"[A-Za-z0-9_-]+")

#: The three suffix conventions the 007 (po_feature_spec) native artifact map
#: keys carry — the contract of record (specialist-agent product_owner/modes/
#: feature_spec.py ``_ARTIFACT_SUFFIXES``). forge projects the committed triple
#: from these; anything else in the map (a ``pass-bar-seed-*.yaml``,
#: ``validation.json``) is a tolerated extra, never part of the committed triple.
_SPEC_FEATURE_SUFFIX = ".feature"
_SPEC_ASSUMPTIONS_SUFFIX = "_assumptions.yaml"
_SPEC_SUMMARY_SUFFIX = "_summary.md"

#: Keys that appear in a specialist artifact map (or a wrap_role_output envelope)
#: that are NOT committable target-repo files: the out-of-band validation-as-data
#: channels (``validation.json`` / ``seed_errors.json``) plus any envelope
#: scalars. ``_plan_tree_files`` excludes these so only real repo paths commit.
_NON_ARTIFACT_KEYS = frozenset(
    {
        "validation.json",
        "seed_errors.json",
        "feature_id",
        "slug",
        "role_id",
        "coach_score",
        "criterion_breakdown",
        "detection_findings",
        "role_output",
    }
)

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
"""``async (*, plan_run_id, correlation_id, feature_id, spec_feature,
spec_summary, target_repo_descriptor, spec_assumptions=None)
-> StageDispatchResult``.

Lane B (B2): dispatch the ``architect_feature_plan`` (008) leg. Forge supplies
the SUPPLIED minted ``feature_id`` (RV-1: the plan leg asserts it), the 007 spec
triple CONTENTS (``spec_feature`` = the committed .feature, ``spec_summary`` =
the committed _summary.md, optional ``spec_assumptions`` = the committed
_assumptions.yaml), and the structured ``target_repo_descriptor`` — the exact
argument shape ``architect_feature_plan`` requires (specialist-agent
roles/architect/modes/feature_plan.py). The result's ``role_output`` carries the
plan tree."""


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
    # Lane B / Phase E1 (B4 round-19, Rich-ratified) — the guardkit ``qa validate
    # pass-bar`` oracle. Optional / default None: with the flag OFF it is never
    # consulted. With the flag ON the per-task QA pass-bar registration leg
    # requires it; a missing collaborator fails the run LOUDLY (never a silent
    # skip — the same posture as the other target-terminal oracles).
    validate_pass_bar: "ValidatePassBarFn | None" = None
    # Lane B / Phase E1 (F2 — the per-feature live-gate REGISTRATION leg, sibling
    # of the pass-bar leg) — the guardkit ``qa validate gate-registry`` oracle.
    # Optional / default None: with the flag OFF it is never consulted. With the
    # flag ON the per-feature gate-registration leg requires it; a missing
    # collaborator fails the run LOUDLY (never a silent skip — the same posture
    # as the other target-terminal oracles).
    validate_gate_registry: "ValidateGateRegistryFn | None" = None
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
                # on a re-drive). Then (B4 round-19): fan the captured 007 seed
                # out into per-task ``qa/pass-bar-<TASK-ID>.yaml`` bars and commit
                # them BEFORE the build trigger — the WS2 B2 precondition demands
                # the bars be registered before implementation. B3: on validate
                # green, queue the feature onto forge's own Mode B dispatcher and
                # advance to BUILD_QUEUED.
                if not await self._feature_plan_leg(row, correlation_id):
                    return
                if not await self._register_pass_bars_leg(row, correlation_id):
                    return
                if not await self._register_feature_gate_leg(row, correlation_id):
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

        # VALIDATION CHANNEL (C5): the 007 native map ships a validation.json
        # self-check alongside the artifacts. Per the mode's own contract this
        # channel exists to drive Mode P's bounded revision_of re-invoke (C5,
        # not yet built) — it is ADVISORY data, not an oracle. The B2 spec of
        # record's gates are the REAL oracles that run next (the normalizer +
        # ``guardkit feature validate``): a self-flagged spec that passes them
        # is good enough by the estate's own bar (the gold hermetic run shipped
        # accepted:false on a minor count note while the coach scored 0.985).
        # So: surface the self-reported errors LOUDLY, verbatim, then let the
        # oracles decide. TODO(C5 follow-on): thread these errors + the prior
        # artifact set back into 007 as the bounded revision re-invoke.
        spec_val_errors = self._validation_failures(role_output)
        if spec_val_errors:
            logger.warning(
                "007 validation.json self-check reported failures for %s "
                "(ADVISORY — proceeding to the normalizer/validate oracles): %s",
                correlation_id,
                "; ".join(spec_val_errors),
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

        # CAPTURE the 007 feature-grain pass-bar SEED (a tolerated extra in the
        # native map, NEVER a committed spec-triple file) and persist it on the
        # durable feature-spec event so it survives to plan-commit — where it is
        # specialised into per-task bars (B4 round-19). ``None`` when this reply
        # shipped no seed (an older specialist); the plan-commit leg fails loudly
        # on that rather than silently skipping the bars the B2 gate demands.
        pass_bar_seed = self._capture_pass_bar_seed(role_output)
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
                    "pass_bar_seed": pass_bar_seed,
                }
            ),
        )
        logger.info(
            "planning driver: run %s feature-spec committed (slug=%s, %d files, "
            "pass-bar seed %s)",
            correlation_id,
            slug,
            len(files),
            "captured" if pass_bar_seed is not None else "ABSENT",
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

        # The 008 contract needs the CONTENTS of the committed spec triple, not
        # paths. Read them back off the planning branch (they are no longer in
        # memory after the spec leg returned — and on an idempotent re-drive the
        # spec leg never ran this drive at all). The .feature is the committed,
        # normalizer-collapsed content of record.
        spec_files = spec_details.get("spec_files") or []
        spec_feature, spec_summary, spec_assumptions = await self._read_spec_triple(
            repo_path, branch, spec_files
        )
        if not spec_feature or not spec_summary:
            await self._fail_leg(
                correlation_id,
                _FEATURE_PLAN_STAGE,
                "could not read the committed spec triple contents "
                f"(.feature / _summary.md) off branch {branch} "
                f"(spec_files={sorted(spec_files)})",
            )
            return False
        target_repo_descriptor = self._build_target_repo_descriptor(
            target_repo, repo_path
        )

        try:
            result = await deps.dispatch_feature_plan(
                plan_run_id=plan_run_id,
                correlation_id=correlation_id,
                feature_id=feature_id,
                spec_feature=spec_feature,
                spec_summary=spec_summary,
                target_repo_descriptor=target_repo_descriptor,
                spec_assumptions=spec_assumptions,
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

        # VALIDATION CHANNEL (C5): advisory self-check data, not an oracle —
        # same posture as the 007 leg above. The REAL oracle for the plan tree
        # is ``guardkit feature validate`` in the pre-commit step right below:
        # a self-flagged plan that validates green is good enough by the
        # estate's own bar. Surface the self-reported errors LOUDLY, verbatim,
        # then let the oracle decide. TODO(C5 follow-on): the bounded
        # revision_of re-invoke consumes this channel properly.
        plan_val_errors = self._validation_failures(role_output)
        if plan_val_errors:
            logger.warning(
                "008 validation.json self-check reported failures for %s "
                "(ADVISORY — proceeding to the guardkit validate oracle): %s",
                correlation_id,
                "; ".join(plan_val_errors),
            )

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

    async def _register_pass_bars_leg(self, row: Any, correlation_id: str) -> bool:
        """Register per-task QA pass bars from the 007 seed (B4 round-19).

        Runs at plan-commit — AFTER ``validate_feature_plan`` passed and the plan
        tree committed, BEFORE the B3 build trigger — because the WS2 B2
        precondition demands a ``qa/pass-bar-<TASK-ID>.yaml`` registered for
        every task BEFORE implementation, and the machine plan leg (008) does not
        emit them. Forge fans the captured feature-grain seed out per task,
        mirroring the Factory-2 registered shape, and commits the bars as ONE
        commit so tap-2's artifacts include them.

        Auth gate (Rich's §5 call): a seed flagged ``auth_surface_bearing`` is
        REFUSED loudly here — pass bars for an auth surface need ATTENDED
        registration per SPL-007 §A.2 — and the run fails without a build queued.

        Idempotent: a durable ``qa-pass-bars`` event short-circuits a re-drive
        (a crash between the bars commit and BUILD_QUEUED never re-mints them).
        Returns True to keep driving (the caller proceeds to the B3 trigger),
        False on a loud terminal failure.
        """
        deps = self._deps
        if self._has_leg_event(correlation_id, _QA_PASS_BARS_STAGE):
            logger.info(
                "planning driver: run %s pass bars already registered "
                "(idempotent re-drive — proceeding to the B3 build trigger)",
                correlation_id,
            )
            return True

        if deps.validate_pass_bar is None:
            return await self._fail_leg(
                correlation_id,
                _QA_PASS_BARS_STAGE,
                "target terminal ON but the pass-bar validate collaborator "
                "(validate_pass_bar) is not wired",
            )

        # The seed was captured at the spec commit and persisted on the durable
        # feature-spec event (it survives an idempotent re-drive where the spec
        # leg never ran this drive).
        spec_details = self._leg_event_details(correlation_id, _FEATURE_SPEC_STAGE)
        raw_seed = spec_details.get("pass_bar_seed")
        if not raw_seed:
            # (c) no seed shipped (older specialist). Fail LOUDLY at this cheaper
            # layer — the WS2 B2 precondition would refuse the build anyway.
            return await self._fail_leg(
                correlation_id,
                _QA_PASS_BARS_STAGE,
                "the 007 reply shipped no pass-bar seed (pass-bar-seed-*.yaml); "
                "cannot register the per-task QA pass bars the B2 precondition "
                "demands — the specialist must emit the feature-grain seed",
            )

        try:
            seed = yaml.safe_load(raw_seed)
        except yaml.YAMLError as exc:
            return await self._fail_leg(
                correlation_id,
                _QA_PASS_BARS_STAGE,
                f"the captured pass-bar seed is not parseable YAML: {exc}",
            )
        if not isinstance(seed, Mapping):
            return await self._fail_leg(
                correlation_id,
                _QA_PASS_BARS_STAGE,
                "the captured pass-bar seed is not a YAML mapping",
            )

        # AUTH GATE (SPL-007 §A.2, Rich-ratified): an auth-surface-bearing seed is
        # REFUSED machine registration — pass bars need attended registration.
        # Refuse LOUDLY, naming the clause + the seed's own basis verbatim;
        # nothing beyond the already-committed plan lands, and no BUILD_QUEUED.
        if bool(seed.get("auth_surface_bearing")):
            basis = str(seed.get("auth_surface_basis") or "no basis supplied")
            return await self._fail_leg(
                correlation_id,
                _QA_PASS_BARS_STAGE,
                f"pass-bar seed is auth_surface_bearing — pass bars need "
                f"attended registration per {_SPL_007_AUTH_CLAUSE}; refusing "
                f"machine registration. Seed auth_surface_basis: {basis}",
            )

        plan_details = self._leg_event_details(correlation_id, _FEATURE_PLAN_STAGE)
        feature_id = str(plan_details.get("feature_id") or "")
        plan_sha = str(plan_details.get("sha") or "")
        branch = str(plan_details.get("branch") or f"planning/{correlation_id}")
        plan_files = [str(f) for f in (plan_details.get("plan_files") or [])]
        if not feature_id or not plan_sha:
            return await self._fail_leg(
                correlation_id,
                _QA_PASS_BARS_STAGE,
                "no feature id / plan commit sha recorded on the feature-plan "
                "leg — cannot register per-task pass bars",
            )
        resolved = await self._resolve_repo(
            row, correlation_id, stage_label=_QA_PASS_BARS_STAGE
        )
        if resolved is None:
            return False
        _target_repo, repo_path = resolved

        # The per-task bar ids ARE the validated plan's task ids — the SAME
        # source ``guardkit feature validate`` reads: the committed feature YAML.
        task_ids = await self._read_plan_task_ids(
            repo_path, branch, feature_id, plan_files
        )
        if task_ids is None:
            return await self._fail_leg(
                correlation_id,
                _QA_PASS_BARS_STAGE,
                f"could not read the validated plan's feature YAML for "
                f"{feature_id} off branch {branch} to enumerate task ids "
                f"(plan_files={sorted(plan_files)})",
            )

        run_date = deps.clock().date().isoformat()
        if not task_ids:
            # A validated plan with zero tasks: no per-task bars to register.
            # Record the leg complete (idempotent) and advance — B2 is vacuous.
            logger.info(
                "planning driver: run %s plan lists no tasks; no per-task pass "
                "bars to register",
                correlation_id,
            )
            deps.store._record_event(
                correlation_id=correlation_id,
                stage_label=_QA_PASS_BARS_STAGE,
                status="approved",
                actor_identity="planning-driver",
                details_json=json.dumps(
                    {
                        "feature_id": feature_id,
                        "bar_files": [],
                        "registered_at_sha": plan_sha,
                        "branch": branch,
                    }
                ),
            )
            return True

        # Fan the seed out — one bar per task, registered_at.sha = the PLAN
        # commit sha, date = the run date from the driver's clock,
        # auth_surface_bearing false by construction on this (refused-if-true) path.
        bars: dict[str, str] = {
            f"qa/pass-bar-{task_id}.yaml": self._mint_pass_bar_yaml(
                task_id=task_id, seed=seed, sha=plan_sha, date=run_date
            )
            for task_id in task_ids
        }

        validate = deps.validate_pass_bar

        async def _pre_commit(worktree: Path) -> PreCommitResult:
            # Run guardkit's OWN ``qa validate pass-bar`` on every minted bar so a
            # malformed forge-minted bar fails the leg before it lands (never a
            # bar the B2 gate would later reject).
            for rel in sorted(bars):
                outcome = await validate(worktree, rel)
                if not outcome.ok:
                    return PreCommitResult(
                        ok=False, detail=f"{rel}: {outcome.detail}"
                    )
            return PreCommitResult(ok=True)

        try:
            gitres = await deps.git_runner.prepare_branch_and_write_tree(
                repo_path=repo_path,
                branch=branch,
                files=bars,
                message=(
                    f"planning: register {len(bars)} per-task QA pass bar(s) for "
                    f"{correlation_id} ({feature_id}, Lane B seed fan-out)"
                ),
                pre_commit=_pre_commit,
            )
        except Exception as exc:  # noqa: BLE001 — write boundary
            return await self._fail_leg(
                correlation_id,
                _QA_PASS_BARS_STAGE,
                f"pass-bar write raised {type(exc).__name__}: {exc}",
            )
        if gitres.status == "failed":
            return await self._fail_leg(
                correlation_id,
                _QA_PASS_BARS_STAGE,
                f"pass-bar write / qa validate failed: {gitres.stderr}",
            )

        deps.store._record_event(
            correlation_id=correlation_id,
            stage_label=_QA_PASS_BARS_STAGE,
            status="approved",
            actor_identity="planning-driver",
            details_json=json.dumps(
                {
                    "feature_id": feature_id,
                    "bar_files": sorted(bars),
                    "registered_at_sha": plan_sha,
                    "sha": gitres.sha,
                    "branch": branch,
                }
            ),
        )
        await self._notify(
            correlation_id,
            f"Planning run {correlation_id}: registered {len(bars)} per-task QA "
            f"pass bar(s) for {feature_id} on branch {branch} (from the 007 "
            "seed) before queueing the build.",
            level="info",
        )
        logger.info(
            "planning driver: run %s registered %d per-task pass bar(s) "
            "(feature_id=%s, plan_sha=%s)",
            correlation_id,
            len(bars),
            feature_id,
            plan_sha,
        )
        return True

    async def _register_feature_gate_leg(
        self, row: Any, correlation_id: str
    ) -> bool:
        """Register a per-feature live GATE from the 007 seed at plan-commit (F2).

        The exact sibling of :meth:`_register_pass_bars_leg`. The pass-bar leg
        registers the pass BARS the B2 precondition demands; this leg closes the
        matching gap on the OTHER side — at plan-commit the machine flow
        registered bars but no live GATE, so the post-deploy live-gate only
        proved "the deployment passes health+stats", never "the NEW endpoint
        passes a REGISTERED gate". Forge derives the endpoint from the seed's
        machine criteria, fills the target repo's OWN feature-behaviour gate
        TEMPLATE, appends a mirrored GateEntry to its OWN gate registry, and
        commits both as ONE commit AFTER the bars commit and BEFORE the build
        trigger.

        SKIP-vs-FAIL law (BINDING):
          (a) no machine criterion yields a GET ``{method,path}`` → honest
              ``skipped`` leg event ("no derivable endpoint — no gate
              registered") and CONTINUE to the build (non-endpoint features are
              legitimate);
          (b) the target repo carries no ``qa/gates/feature_behaviour_gate.py``
              template or no ``qa/gates/registry.yaml`` on the branch → the same
              honest skip (the repo has not adopted the F4 gate surface);
          (c) template + derivable but the fill / validate / commit fails → LOUD
              :meth:`_fail_leg` (same posture as the bars leg);
          (d) the auth-flagged seed case never reaches this leg — the bars leg
              refuses it first — so no auth handling is re-implemented here.

        Idempotent: a durable ``qa-feature-gate`` event (approved, whether a real
        registration OR an honest skip) short-circuits a re-drive. Returns True
        to keep driving (skip AND success both proceed to the B3 trigger), False
        on a loud terminal failure.
        """
        deps = self._deps
        if self._has_leg_event(correlation_id, _QA_FEATURE_GATE_STAGE):
            logger.info(
                "planning driver: run %s feature gate already resolved "
                "(idempotent re-drive — proceeding to the B3 build trigger)",
                correlation_id,
            )
            return True

        if deps.validate_gate_registry is None:
            return await self._fail_leg(
                correlation_id,
                _QA_FEATURE_GATE_STAGE,
                "target terminal ON but the gate-registry validate collaborator "
                "(validate_gate_registry) is not wired",
            )

        # The seed the bars leg consumed (persisted on the durable feature-spec
        # event). By construction it is authless here — the bars leg refuses an
        # auth-flagged seed BEFORE this leg runs (driver.py auth gate), so we
        # never re-implement that refusal. A missing/unparseable seed can only
        # reach here if the bars leg already failed (it guards the same seed), so
        # treat it defensively as an honest skip: nothing derivable, no gate.
        spec_details = self._leg_event_details(correlation_id, _FEATURE_SPEC_STAGE)
        raw_seed = spec_details.get("pass_bar_seed")
        seed: Any = None
        if raw_seed:
            try:
                seed = yaml.safe_load(raw_seed)
            except yaml.YAMLError:
                seed = None
        criteria = (
            seed.get("criteria") if isinstance(seed, Mapping) else None
        ) or []

        endpoint = self._derive_feature_gate_endpoint(criteria)
        if endpoint is None:
            return self._skip_feature_gate(
                correlation_id,
                "no derivable endpoint — no gate registered",
            )

        plan_details = self._leg_event_details(correlation_id, _FEATURE_PLAN_STAGE)
        feature_id = str(plan_details.get("feature_id") or "")
        plan_sha = str(plan_details.get("sha") or "")
        branch = str(plan_details.get("branch") or f"planning/{correlation_id}")
        if not feature_id or not plan_sha:
            return await self._fail_leg(
                correlation_id,
                _QA_FEATURE_GATE_STAGE,
                "no feature id / plan commit sha recorded on the feature-plan "
                "leg — cannot register the per-feature live gate",
            )

        # The first minted pass bar is the gate's ``pass_bar_ref`` (the gate and
        # its bar share the feature's first task). No bars ⇒ nothing to reference
        # ⇒ honest skip (a validated plan with zero tasks registers no gate).
        bars_details = self._leg_event_details(correlation_id, _QA_PASS_BARS_STAGE)
        bar_files = sorted(str(f) for f in (bars_details.get("bar_files") or []))
        if not bar_files:
            return self._skip_feature_gate(
                correlation_id,
                "the plan registered no pass bars — no gate pass_bar_ref to "
                "anchor; no gate registered",
            )
        pass_bar_ref = bar_files[0]

        resolved = await self._resolve_repo(
            row, correlation_id, stage_label=_QA_FEATURE_GATE_STAGE
        )
        if resolved is None:
            return False
        _target_repo, repo_path = resolved

        # (b) The repo's OWN gate surface must exist on the branch — never
        # fabricated forge-side. Absent ⇒ the repo has not adopted the F4 gate
        # surface ⇒ honest skip.
        template = await deps.git_runner.read_file_from_branch(
            repo_path=repo_path, branch=branch, file_path=_FEATURE_GATE_TEMPLATE_REL
        )
        if not template:
            return self._skip_feature_gate(
                correlation_id,
                f"the target repo carries no {_FEATURE_GATE_TEMPLATE_REL} "
                "template on the branch — the F4 gate surface is not adopted; "
                "no gate registered",
            )
        registry_raw = await deps.git_runner.read_file_from_branch(
            repo_path=repo_path, branch=branch, file_path=_GATE_REGISTRY_REL
        )
        if not registry_raw:
            return self._skip_feature_gate(
                correlation_id,
                f"the target repo carries no {_GATE_REGISTRY_REL} on the branch "
                "— the F4 gate surface is not adopted; no gate registered",
            )

        # Derive the slug + snake filename deterministically from the seed.
        raw_slug = str(seed.get("feature_slug") or "").strip()
        slug = self._sanitise_slug(raw_slug) or feature_id.lower()
        slug_snake = re.sub(r"[^a-z0-9]+", "_", slug.lower()).strip("_")
        if not slug_snake:
            return await self._fail_leg(
                correlation_id,
                _QA_FEATURE_GATE_STAGE,
                f"could not derive a gate filename from feature_slug={raw_slug!r} "
                f"/ feature_id={feature_id}",
            )
        gate_rel = f"qa/gates/{slug_snake}_gate.py"

        # (c) Fill the template + append the registry entry. A drift in the
        # template's fillable literals, or a malformed registry, is a LOUD fail.
        try:
            filled_gate = self._fill_feature_gate(template, slug, endpoint)
            new_registry = self._append_gate_registry_entry(
                registry_raw,
                gate_id=slug,
                gate_path=gate_rel,
                pass_bar_ref=pass_bar_ref,
            )
        except _FeatureGateFillError as exc:
            return await self._fail_leg(
                correlation_id, _QA_FEATURE_GATE_STAGE, str(exc)
            )

        validate = deps.validate_gate_registry

        async def _pre_commit(worktree: Path) -> PreCommitResult:
            # Run guardkit's OWN ``qa validate gate-registry`` on the appended
            # registry so a malformed forge-appended entry fails the leg BEFORE
            # it lands (never an entry the post-deploy live-gate would reject).
            outcome = await validate(worktree, _GATE_REGISTRY_REL)
            return PreCommitResult(ok=outcome.ok, detail=outcome.detail)

        files = {gate_rel: filled_gate, _GATE_REGISTRY_REL: new_registry}
        try:
            gitres = await deps.git_runner.prepare_branch_and_write_tree(
                repo_path=repo_path,
                branch=branch,
                files=files,
                message=(
                    f"planning: register the per-feature live gate for "
                    f"{correlation_id} ({feature_id}, GET {endpoint['path']} — "
                    "F2 seed derivation)"
                ),
                pre_commit=_pre_commit,
            )
        except Exception as exc:  # noqa: BLE001 — write boundary
            return await self._fail_leg(
                correlation_id,
                _QA_FEATURE_GATE_STAGE,
                f"feature-gate write raised {type(exc).__name__}: {exc}",
            )
        if gitres.status == "failed":
            return await self._fail_leg(
                correlation_id,
                _QA_FEATURE_GATE_STAGE,
                f"feature-gate write / qa validate gate-registry failed: "
                f"{gitres.stderr}",
            )

        deps.store._record_event(
            correlation_id=correlation_id,
            stage_label=_QA_FEATURE_GATE_STAGE,
            status="approved",
            actor_identity="planning-driver",
            details_json=json.dumps(
                {
                    "feature_id": feature_id,
                    "gate_file": gate_rel,
                    "registry_file": _GATE_REGISTRY_REL,
                    "gate_id": slug,
                    "endpoint": endpoint,
                    "pass_bar_ref": pass_bar_ref,
                    "registered_at_sha": plan_sha,
                    "sha": gitres.sha,
                    "branch": branch,
                }
            ),
        )
        await self._notify(
            correlation_id,
            f"Planning run {correlation_id}: registered a live gate for "
            f"{feature_id} (GET {endpoint['path']} → {gate_rel}) on branch "
            f"{branch} before queueing the build.",
            level="info",
        )
        logger.info(
            "planning driver: run %s registered feature gate %s (endpoint=GET "
            "%s, feature_id=%s, plan_sha=%s)",
            correlation_id,
            gate_rel,
            endpoint["path"],
            feature_id,
            plan_sha,
        )
        return True

    def _skip_feature_gate(self, correlation_id: str, reason: str) -> bool:
        """Record an HONEST skipped ``qa-feature-gate`` leg event and CONTINUE.

        A non-endpoint feature (or a repo that has not adopted the qa/gates/
        surface) is a legitimate no-gate outcome, not a failure: record the
        durable label (so a re-drive is a clean no-op) with a ``skipped`` detail
        and return True so the caller proceeds to the B3 build trigger. Zero
        target-repo writes.
        """
        logger.info(
            "planning driver: run %s feature gate skipped — %s",
            correlation_id,
            reason,
        )
        self._deps.store._record_event(
            correlation_id=correlation_id,
            stage_label=_QA_FEATURE_GATE_STAGE,
            status="approved",
            actor_identity="planning-driver",
            details_json=json.dumps({"skipped": True, "reason": reason}),
        )
        return True

    def _derive_feature_gate_endpoint(
        self, criteria: Any
    ) -> dict[str, str] | None:
        """First machine criterion that yields a v1 (GET) gate endpoint, or None.

        Iterates the seed's criteria in order; for each ``class: machine``
        criterion applies :meth:`_derive_get_endpoint` to its text and returns
        the first match. Non-machine criteria, non-GET verbs, missing paths and
        prose all yield nothing — an honest skip rather than a guessed gate.
        """
        if not isinstance(criteria, list):
            return None
        for crit in criteria:
            if not isinstance(crit, Mapping):
                continue
            if str(crit.get("class")) != "machine":
                continue
            endpoint = self._derive_get_endpoint(str(crit.get("text") or ""))
            if endpoint is not None:
                return endpoint
        return None

    @staticmethod
    def _derive_get_endpoint(text: str) -> dict[str, str] | None:
        """Conservative v1 endpoint derivation from one criterion's free text.

        Returns ``{"method": "GET", "path": "/…"}`` ONLY when the strict grammar
        matches AND the verb is GET (the sole verb whose happy-path status forge
        knows: 200). Any other verb, a missing path, wrong case, or mid-sentence
        prose returns None (skip — forge never guesses a success status).
        """
        match = _FEATURE_GATE_ENDPOINT_RE.search(text)
        if match is None or match.group("method") != "GET":
            return None
        return {"method": "GET", "path": match.group("path")}

    @staticmethod
    def _fill_feature_gate(
        template: str, gate_id: str, endpoint: Mapping[str, str]
    ) -> str:
        """Fill the target repo's OWN feature-behaviour gate template (F2).

        Substitutes ONLY the two fillable SPEC literals (gate_id + the request
        method/path), leaving the rest of the template — including the
        ``/REPLACE_ME`` runtime guard elsewhere — byte-untouched. v1 derives NO
        json_assertions from free text (the filled gate asserts status + valid
        JSON body + the template's own header defaults). Each literal must appear
        EXACTLY once; a mismatch means the repo's template drifted from the shape
        forge fills — raise so the caller fails the leg loudly (never a silently
        wrong gate).
        """
        if template.count(_GATE_TEMPLATE_GATE_ID_LITERAL) != 1:
            raise _FeatureGateFillError(
                "the target repo's feature-behaviour gate template does not "
                f"carry the fillable gate_id literal exactly once "
                f"({_GATE_TEMPLATE_GATE_ID_LITERAL!r}) — template drift; "
                "refusing to fill a wrong gate"
            )
        if template.count(_GATE_TEMPLATE_REQUEST_LITERAL) != 1:
            raise _FeatureGateFillError(
                "the target repo's feature-behaviour gate template does not "
                f"carry the fillable request literal exactly once "
                f"({_GATE_TEMPLATE_REQUEST_LITERAL!r}) — template drift; "
                "refusing to fill a wrong gate"
            )
        method = endpoint["method"]
        path = endpoint["path"]
        filled = template.replace(
            _GATE_TEMPLATE_GATE_ID_LITERAL, f'"gate_id": "{gate_id}",'
        )
        filled = filled.replace(
            _GATE_TEMPLATE_REQUEST_LITERAL,
            f'"request": {{"method": "{method}", "path": "{path}"}},',
        )
        return filled

    @staticmethod
    def _append_gate_registry_entry(
        registry_raw: str,
        *,
        gate_id: str,
        gate_path: str,
        pass_bar_ref: str,
    ) -> str:
        """Append one mirrored GateEntry to the repo's OWN gate registry (F2).

        Reads the existing registry, MIRRORS a sibling entry's ``target
        {base_url_env, environment_id}``, ``preconditions``, ``preflight`` and
        ``evidence_dir_pattern`` (never hardcoding ``API_TEST_*``), points the new
        entry at ``gate_path`` with the first minted bar as ``pass_bar_ref``, and
        TEXTUALLY appends the rendered block so the existing entries + header
        comments stay byte-untouched. Raises when the registry is unparseable or
        carries no sibling entry to mirror (a loud-fail signal).
        """
        try:
            data = yaml.safe_load(registry_raw)
        except yaml.YAMLError as exc:
            raise _FeatureGateFillError(
                f"the target repo's {_GATE_REGISTRY_REL} is not parseable "
                f"YAML: {exc}"
            ) from exc
        gates = data.get("gates") if isinstance(data, Mapping) else None
        if not isinstance(gates, list) or not gates:
            raise _FeatureGateFillError(
                f"the target repo's {_GATE_REGISTRY_REL} carries no gate entry "
                "to mirror — cannot copy the sibling's base_url_env / "
                "preconditions / preflight / evidence_dir_pattern"
            )
        sibling = next((g for g in gates if isinstance(g, Mapping)), None)
        if sibling is None:
            raise _FeatureGateFillError(
                f"the target repo's {_GATE_REGISTRY_REL} gates list carries no "
                "mapping entry to mirror"
            )
        sib_target = sibling.get("target") if isinstance(sibling, Mapping) else {}
        base_url_env = (
            str(sib_target.get("base_url_env"))
            if isinstance(sib_target, Mapping) and sib_target.get("base_url_env")
            else None
        )
        if not base_url_env:
            raise _FeatureGateFillError(
                f"the sibling gate entry in {_GATE_REGISTRY_REL} carries no "
                "target.base_url_env to mirror"
            )
        environment_id = (
            str(sib_target.get("environment_id"))
            if isinstance(sib_target, Mapping) and sib_target.get("environment_id")
            else "local"
        )
        entry: dict[str, Any] = {
            "id": gate_id,
            "path": gate_path,
            "target": {
                "base_url_env": base_url_env,
                "environment_id": environment_id,
            },
            "preconditions": list(sibling.get("preconditions") or []),
            "preflight": list(sibling.get("preflight") or []),
            "pass_bar_ref": pass_bar_ref,
            "evidence_dir_pattern": str(
                sibling.get("evidence_dir_pattern")
                or "qa/gates/evidence/{run_id}"
            ),
        }
        block = yaml.safe_dump(
            [entry], sort_keys=False, default_flow_style=False, allow_unicode=True
        )
        # Indent the top-level list block by two spaces so it nests under the
        # existing ``gates:`` key, and append after the last existing entry.
        indented = "".join(
            ("  " + line if line.strip() else line)
            for line in block.splitlines(keepends=True)
        )
        return registry_raw.rstrip("\n") + "\n" + indented

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
        """Project the specialist's ``role_output`` document as a dict (M10).

        The deployed reply nests the role's NATIVE artifact map one level down:
        the specialist wraps every reply via ``wrap_role_output`` (specialist-
        agent adapters/result_wrapper.py) into
        ``{role_id, coach_score, criterion_breakdown, detection_findings,
        role_output: <native map>}``, and forge's reply parser
        (``_extract_role_output``) already unwraps that envelope so the driver
        receives the NATIVE map directly at ``result.role_output``.

        This projection is belt-and-braces against BOTH nesting levels: if the
        value handed through is itself a ``wrap_role_output`` envelope (a doubly
        wrapped payload — the ``role_output`` key still carries a Mapping), it
        descends one level so the caller always gets the bare native artifact
        map. A native map never carries a ``role_output`` key (its keys are
        artifact filenames / repo paths), so the descent is unambiguous.
        """
        ro = getattr(result, "role_output", None)
        if not isinstance(ro, Mapping):
            return {}
        inner = ro.get("role_output")
        if isinstance(inner, Mapping):
            ro = inner
        # The native map is a SessionResult.model_dump() (specialist-agent
        # session/types.py): the artifact map itself sits under its
        # ``artifacts`` key ({filename: content}), beside run_id/success/
        # final_score/etc. Descend to it when present (B4 run 5392685a — the
        # wire truth; fixture wire_reply.json is generated by the specialist's
        # own wrap chain). A bare artifact map (no ``artifacts`` key) still
        # passes through unchanged.
        arts = ro.get("artifacts")
        if isinstance(arts, Mapping) and arts:
            return dict(arts)
        return dict(ro)

    @staticmethod
    def _sanitise_slug(raw: str) -> str | None:
        """Sanitise a raw token to a filesystem-safe slug, or ``None``.

        Collapses any run of disallowed characters to a single hyphen and trims
        leading/trailing hyphens; returns ``None`` when nothing usable remains.
        """
        cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", raw.strip()).strip("-")
        return cleaned if cleaned and _SLUG_RE.fullmatch(cleaned) else None

    @staticmethod
    def _slug_of(role_output: Mapping[str, Any], correlation_id: str) -> str:
        """Feature slug from the 007 result, or a deterministic fallback.

        Resolution order: an explicit ``role_output['slug']`` (sanitised); else
        the stem of the ``*.feature`` key in the native suffix-keyed artifact map
        (the DEPLOYED 007 shape — the filename carries the slug, e.g.
        ``uptime-endpoint.feature`` → ``uptime-endpoint``); else a deterministic
        ``feature-{cid}`` (WS1's semantic-slug emitter is §9 follow-on).
        """
        candidate = str(role_output.get("slug") or "").strip()
        if candidate and _SLUG_RE.fullmatch(candidate):
            return candidate
        for name in role_output:
            key = str(name)
            if key.endswith(_SPEC_FEATURE_SUFFIX):
                slug = PlanningRunDriver._sanitise_slug(
                    key[: -len(_SPEC_FEATURE_SUFFIX)]
                )
                if slug:
                    return slug
                break
        return f"feature-{correlation_id}"

    @staticmethod
    def _project_native_spec_triple(
        role_output: Mapping[str, Any], slug: str
    ) -> dict[str, str] | None:
        """Project the committed triple from the 007 NATIVE suffix-keyed map.

        The deployed 007 ``role_output`` is an artifact map keyed by BARE
        filename with the three contract suffixes (``.feature`` /
        ``_assumptions.yaml`` / ``_summary.md``) PLUS extras (a
        ``pass-bar-seed-*.yaml`` and the ``validation.json`` data channel).
        Requires EXACTLY one file per suffix; extras are tolerated but never
        committed. The committed paths are the canonical ``features/<slug>/``
        triple (``slug`` already resolved via ``_slug_of``). ``None`` when the
        map does not carry exactly one of each suffix.
        """
        by_suffix: dict[str, list[str]] = {
            _SPEC_FEATURE_SUFFIX: [],
            _SPEC_ASSUMPTIONS_SUFFIX: [],
            _SPEC_SUMMARY_SUFFIX: [],
        }
        # Longest suffix first so ``_assumptions.yaml`` never loses to a broader
        # match; the three are mutually exclusive but order-independence is cheap.
        for name, content in role_output.items():
            key = str(name)
            for suffix in (
                _SPEC_ASSUMPTIONS_SUFFIX,
                _SPEC_SUMMARY_SUFFIX,
                _SPEC_FEATURE_SUFFIX,
            ):
                if key.endswith(suffix):
                    by_suffix[suffix].append(str(content))
                    break
        if any(len(matches) != 1 for matches in by_suffix.values()):
            return None
        base = f"features/{slug}"
        return {
            f"{base}/{slug}{_SPEC_FEATURE_SUFFIX}": by_suffix[_SPEC_FEATURE_SUFFIX][0],
            f"{base}/{slug}{_SPEC_ASSUMPTIONS_SUFFIX}": by_suffix[
                _SPEC_ASSUMPTIONS_SUFFIX
            ][0],
            f"{base}/{slug}{_SPEC_SUMMARY_SUFFIX}": by_suffix[_SPEC_SUMMARY_SUFFIX][0],
        }

    @staticmethod
    def _spec_triple_files(
        role_output: Mapping[str, Any], slug: str
    ) -> dict[str, str] | None:
        """Project the three-file spec contract from the 007 role_output.

        Shape resolution (belt-and-braces):
          1. An explicit ``files`` mapping (specialist-authored repo-relative
             paths — the legacy/alternate shape), committed verbatim.
          2. The DEPLOYED native suffix-keyed artifact map (one ``*.feature`` /
             ``*_assumptions.yaml`` / ``*_summary.md`` plus tolerated extras),
             projected onto the canonical ``features/<slug>/`` triple.
          3. Field-based ``feature`` / ``assumptions`` / ``summary`` fallback.
        ``None`` when no shape yields a triple (the invalid-artifacts fail path).
        """
        files = role_output.get("files")
        if isinstance(files, Mapping) and files:
            return {str(k): str(v) for k, v in files.items()}
        native = PlanningRunDriver._project_native_spec_triple(role_output, slug)
        if native is not None:
            return native
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
        """Project the plan tree (feature/task/qa files) from the 008 role_output.

        Prefers an explicit ``files`` mapping; otherwise projects the DEPLOYED
        native 008 artifact map, whose keys are ALREADY repo-relative paths
        (``.guardkit/features/<id>.yaml``, ``tasks/backlog/**``, ``qa/*``) — the
        contract of record (specialist-agent architect/modes/feature_plan.py).
        The out-of-band validation channel and any envelope scalars
        (``_NON_ARTIFACT_KEYS``) are excluded so only real repo files commit.
        ``None`` when neither shape yields any committable file.
        """
        files = role_output.get("files")
        if isinstance(files, Mapping) and files:
            return {str(k): str(v) for k, v in files.items()}
        tree = {
            str(k): str(v)
            for k, v in role_output.items()
            if isinstance(v, str) and str(k) not in _NON_ARTIFACT_KEYS
        }
        return tree or None

    @staticmethod
    def _validation_failures(role_output: Mapping[str, Any]) -> list[str]:
        """Return the specialist's decidable-gate failures (C5), or ``[]``.

        The 007/008 modes ship the artifacts PLUS a machine-readable
        ``validation.json`` (a JSON STRING in the native map) carrying
        ``{accepted, errors, gates_run}`` — the validation-as-data channel: a
        decidable gate FAILURE returns the artifacts AND the error list so the
        leg holds both. VALIDATION HONESTY: a reply that reports gate failures
        (``accepted: false`` / a non-empty ``errors`` list) must NOT proceed
        silently — the caller fails the leg loudly naming these errors. A clean
        (``accepted: true``) or absent channel returns ``[]`` (proceed).
        """
        raw = role_output.get("validation.json")
        data: Any = None
        if isinstance(raw, str) and raw.strip():
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                return [
                    "the specialist's validation.json is present but not "
                    "parseable JSON"
                ]
        elif isinstance(raw, Mapping):
            data = raw
        else:
            alt = role_output.get("validation")
            if isinstance(alt, Mapping):
                data = alt
        if not isinstance(data, Mapping):
            return []
        accepted = data.get("accepted")
        errors = data.get("errors")
        error_list = [str(e) for e in errors] if isinstance(errors, list) else []
        if accepted is True:
            return []
        if accepted is False or error_list:
            return error_list or [
                "the specialist reported the artifact set as not accepted "
                "(no error detail supplied)"
            ]
        return []

    @staticmethod
    def _feature_file_rel(files: Mapping[str, str]) -> str | None:
        """The repo-relative ``.feature`` path in a spec triple (or None)."""
        for rel in files:
            if rel.endswith(".feature"):
                return rel
        return None

    async def _read_spec_triple(
        self, repo_path: str, branch: str, spec_files: Any
    ) -> tuple[str | None, str | None, str | None]:
        """Read the committed spec triple CONTENTS back off the planning branch.

        Returns ``(spec_feature, spec_summary, spec_assumptions)`` classified by
        suffix (``.feature`` / ``_summary.md`` / ``_assumptions.yaml``); any role
        whose file is absent or unreadable comes back ``None``. Threading the
        CONTENTS (not paths) is the 008 contract — and reading them off the
        branch (rather than carrying them in memory) is what makes the plan leg
        correct on an idempotent re-drive.
        """
        feature = summary = assumptions = None
        for rel in spec_files:
            rel = str(rel)
            if rel.endswith(".feature"):
                feature = await self._deps.git_runner.read_file_from_branch(
                    repo_path=repo_path, branch=branch, file_path=rel
                )
            elif rel.endswith("_summary.md"):
                summary = await self._deps.git_runner.read_file_from_branch(
                    repo_path=repo_path, branch=branch, file_path=rel
                )
            elif rel.endswith("_assumptions.yaml"):
                assumptions = await self._deps.git_runner.read_file_from_branch(
                    repo_path=repo_path, branch=branch, file_path=rel
                )
        return feature, summary, assumptions

    @staticmethod
    def _capture_pass_bar_seed(role_output: Mapping[str, Any]) -> str | None:
        """The 007 feature-grain pass-bar seed content, or ``None`` (B4 round-19).

        Finds the ``pass-bar-seed-*.yaml`` extra in the 007 native artifact map
        (a tolerated extra, never a committed spec-triple file) and returns its
        raw content verbatim so the plan-commit leg can specialise it into
        per-task bars. ``None`` when the reply shipped no seed (an older
        specialist — the plan-commit leg then fails loudly rather than silently
        skipping the bars the B2 precondition demands).
        """
        for name, content in role_output.items():
            key = str(name)
            if key.startswith(_PASS_BAR_SEED_PREFIX) and key.endswith(
                _PASS_BAR_SEED_SUFFIX
            ):
                return str(content)
        return None

    async def _read_plan_task_ids(
        self, repo_path: str, branch: str, feature_id: str, plan_files: Any
    ) -> list[str] | None:
        """Enumerate the validated plan's task ids from its committed feature YAML.

        The feature YAML (``.guardkit/features/<feature_id>.yaml``) is the SAME
        source ``guardkit feature validate`` reads; forge reads it back off the
        planning branch (not from memory, so this is correct on an idempotent
        re-drive) and returns ``[task.id for task in tasks]`` in plan order.
        Returns ``None`` when the feature YAML cannot be located among the
        committed plan files, read, or parsed — a loud-fail signal for the caller.
        """
        # Locate the feature YAML among the committed plan files: the entry whose
        # path ends with ``<feature_id>.yaml``, preferring the canonical
        # ``.guardkit/features/`` location when several match.
        candidates = [
            str(f) for f in plan_files if str(f).endswith(f"{feature_id}.yaml")
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda p: (".guardkit/features/" not in p, p))
        feature_rel = candidates[0]

        raw = await self._deps.git_runner.read_file_from_branch(
            repo_path=repo_path, branch=branch, file_path=feature_rel
        )
        if not raw:
            return None
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError:
            return None
        if not isinstance(data, Mapping):
            return None
        tasks = data.get("tasks")
        if not isinstance(tasks, list):
            # A feature YAML with no ``tasks`` key (or a null/absent list) has no
            # tasks to register bars for — an empty enumeration, not a read error.
            return []
        task_ids: list[str] = []
        for task in tasks:
            if isinstance(task, Mapping):
                tid = task.get("id")
                if tid:
                    task_ids.append(str(tid))
        return task_ids

    @staticmethod
    def _mint_pass_bar_yaml(
        *, task_id: str, seed: Mapping[str, Any], sha: str, date: str
    ) -> str:
        """Mint one ``qa/pass-bar-<TASK-ID>.yaml`` from the seed (F2 shape).

        Mirrors the Factory-2 registered bar shape EXACTLY (format_version 2.0,
        task_id, registered_at{sha,date}, auth_surface_bearing, preconditions,
        criteria, negative_paths) so guardkit's own ``qa validate pass-bar``
        accepts it. ``registered_at.sha`` is the PLAN commit sha; the seed's
        ``preconditions``/``criteria`` are carried verbatim; ``auth_surface_bearing``
        is false by construction (an auth-bearing seed never reaches this path);
        ``negative_paths`` supplies the universal minimum the seed omits.
        """
        negative_paths = sorted(
            {str(p) for p in (seed.get("negative_paths") or [])}
            | {_UNIVERSAL_NEGATIVE_PATH}
        )
        bar: dict[str, Any] = {
            "format_version": str(
                seed.get("format_version") or _PASS_BAR_FORMAT_VERSION
            ),
            "task_id": task_id,
            "registered_at": {"sha": sha, "date": date},
            "auth_surface_bearing": False,
            "preconditions": list(seed.get("preconditions") or []),
            "criteria": [
                dict(c) if isinstance(c, Mapping) else c
                for c in (seed.get("criteria") or [])
            ],
            "negative_paths": negative_paths,
        }
        return yaml.safe_dump(
            bar, sort_keys=False, default_flow_style=False, allow_unicode=True
        )

    @staticmethod
    def _build_target_repo_descriptor(
        target_repo: str, repo_path: str
    ) -> dict[str, Any]:
        """Build the 008 ``target_repo_descriptor`` honestly from what forge knows.

        Schema of record (specialist-agent roles/architect/modes/feature_plan.py
        ``TARGET_REPO_DESCRIPTOR_SCHEMA``): required = ``repo`` + ``test_roots``;
        optional = ``default_branch`` / ``sibling_repos`` / ``stack``. forge NEVER
        invents an undefined field: ``repo`` is the configured target repo name;
        ``sibling_repos`` is omitted — forge does not cheaply know siblings.

        ``test_roots`` is the EXACT ``tests/<name>`` set the downstream
        ``guardkit feature validate`` pre-commit oracle enforces, discovered by
        REUSING guardkit's OWN ``discover_test_roots``
        (:func:`forge.planning.target_terminal_tools.discover_target_test_roots`)
        — never a shallow re-guess. For api_test this is
        ``["tests/health", "tests/users"]`` (EMPTY ⇒ the plan may emit no smoke
        gate, ASSUM-010). B4 run 36629c5a round 10: the old shallow
        checkout-root discovery returned ``["tests"]``, so 008 invented
        ``tests/smoke`` — a prefix of ``tests`` that the in-session containment
        gate passed but the real validate rejected (repo has ``tests/health``,
        ``tests/users``, no ``tests/smoke``). Handing the exact roots makes
        prefix-containment == membership so the invention is caught in-session.
        """
        # Local import: keep the guardkit-boundary reuse out of the module
        # import graph (target_terminal_tools imports the guardkit run seam).
        from forge.planning.target_terminal_tools import (
            TargetTestRootsUnresolved,
            discover_target_test_roots,
        )

        try:
            test_roots = discover_target_test_roots(repo_path)
        except TargetTestRootsUnresolved as exc:
            # Degraded path: guardkit absent from the interpreter. Production
            # images always ship guardkit (the Dockerfile asserts the import),
            # so this only fires in a guardkit-less env where the real
            # ``feature validate`` oracle cannot run either. Fall back to the
            # historical shallow checkout-root discovery so the descriptor is
            # still built and the run reaches the oracle (the last line of
            # defense) rather than crashing — log LOUDLY.
            logger.warning(
                "target_repo_descriptor: guardkit test-root discovery "
                "unavailable (%s); falling back to shallow checkout-root "
                "discovery — 008 will NOT receive the exact tests/<name> set",
                exc,
            )
            root = Path(repo_path)
            test_roots = [
                name for name in ("tests", "test") if (root / name).is_dir()
            ]
        return {"repo": target_repo, "test_roots": test_roots}

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

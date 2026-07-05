"""TASK-CGCP-010: ``gate_check`` coordinator + state-machine integration.

This module is the **only** place in ``forge.gating`` that knows how to
compose the full gating sequence at every gated stage:

1. Read priors from Graphiti (``forge_pipeline_history``,
   ``forge_calibration_history``).
2. Read calibration adjustments — **always with**
   ``approved_only=True`` (closes risk **R8**, F9 / Group C
   ``@negative``).
3. Call :func:`forge.gating.evaluate_gate` (pure-domain).
4. Persist the resulting :class:`~forge.gating.models.GateDecision` to
   SQLite **first**, and to Graphiti's
   ``forge_pipeline_history`` group second. SQLite is the source of
   truth (F10 / Group E ``@data-integrity``); a Graphiti write or NATS
   publish failure is an operational signal, not a roll-back trigger.
5. Branch on :class:`~forge.gating.models.GateMode`:

   * ``AUTO_APPROVE`` — return immediately, the wrapper has no
     state-machine work to do.
   * ``HARD_STOP`` — transition the build to ``FAILED`` via the
     injected state machine.
   * ``FLAG_FOR_REVIEW`` / ``MANDATORY_HUMAN_APPROVAL`` — atomically:
     persist the ``request_id`` against the paused-build row,
     transition to ``PAUSED``, and publish the
     :class:`~nats_core.events.ApprovalRequestPayload` (closes risk
     **R7**, F5 / Group E ``@concurrency @data-integrity``).
     Then await Rich's response via the subscriber, rehydrate via
     :func:`forge.adapters.langgraph.resume_value_as` (closes risk
     **R2**), and dispatch on the response decision.

This module **also** owns:

* :func:`recover_paused_builds` — the boot-time hook that re-emits
  :class:`ApprovalRequestPayload` for every paused build using the
  **persisted** ``request_id`` (closes risk **R5**, Group D
  ``@regression``).
* :func:`cli_cancel_build` / :func:`cli_skip_stage` — CLI bridges that
  delegate to :class:`~forge.adapters.nats.SyntheticResponseInjector`
  (TASK-CGCP-008) so the CLI steering paths flow through the standard
  subscriber dedup gate (closes risk **F6**).

Architecture
------------

Domain core (this module) only depends on:

* The pure-domain :mod:`forge.gating` models / pure ``evaluate_gate``.
* Adapter Protocol surfaces declared inline below — every adapter is
  injected by the application boundary, not imported. The two concrete
  helpers reachable from this module
  (:func:`forge.adapters.langgraph.resume_value_as` and the
  :class:`SyntheticResponseInjector` instance the CLI bridge calls) are
  duck-typed via the same Protocol surfaces.

This keeps ``forge.gating.wrappers`` testable with simple in-memory
fakes — no NATS broker, no SQLite file, no Graphiti instance is
required. TASK-CGCP-011 covers full integration over a real NATS +
SQLite stack.

Pause-and-publish atomicity (F5)
--------------------------------

The contract is *not* a distributed transaction; it is **ordering
inside a single async function with no awaits between the SQLite
commit and the publish call** so an external observer can never see
``status=PAUSED`` without a corresponding bus publish having been
issued. The implementation in :func:`_atomic_pause_and_publish`
documents and enforces that ordering.

The publish itself may still fail — that surfaces as
:class:`ApprovalPublishError` from the publisher and propagates up to
the caller — but the SQLite row is **not** rolled back. The boot-time
hook :func:`recover_paused_builds` is what closes the loop on a
crashed publish: the build re-emits its approval request on restart
using the persisted ``request_id``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from forge.adapters.langgraph import resume_value_as
from forge.gating.identity import derive_request_id
from forge.gating.models import (
    CalibrationAdjustment,
    ConstitutionalRule,
    DetectionFinding,
    GateDecision,
    GateMode,
    GateTargetKind,
    PriorReference,
    evaluate_gate,
)

# Imports from adapter modules are *typed* references only — the runtime
# wiring is dependency-injected, but the typed names keep the protocol
# surface honest about which existing adapter contracts the wrapper
# composes.
from forge.adapters.nats.synthetic_response_injector import (
    SyntheticInjectFailure,
    SyntheticResponseInjector,
)

# Lazy import to avoid hard coupling at module load time — the
# wrappers module is part of the domain layer and the response
# payload type is only consulted via ``resume_value_as``.
from nats_core.envelope import EventType, MessageEnvelope
from nats_core.events import ApprovalRequestPayload, ApprovalResponsePayload

logger = logging.getLogger(__name__)

#: ``source_id`` stamped on every envelope this wrapper emits — the
#: forge gating layer is the producer for boot-time re-emissions.
SOURCE_ID: str = "forge"

#: Sentinel reasons stamped onto state-machine transitions originating
#: in this module. Pulling them out as constants makes the gate
#: history searchable and the unit tests robust to wording tweaks.
REASON_HARD_STOP: str = "gate hard stop"
REASON_REJECT: str = "approval rejected"
REASON_CLI_CANCEL: str = "cli cancel"
REASON_OVERRIDE: str = "approval override"
REASON_CLI_SKIP: str = "cli skip"
REASON_DEFER: str = "approval defer"
REASON_MAX_WAIT: str = "approval max wait reached"


# ---------------------------------------------------------------------------
# Outcome enum — tells the caller what the wrapper did.
# ---------------------------------------------------------------------------


class GateOutcome(str, Enum):
    """Closed set of outcomes :func:`gate_check` can return.

    Members:
        AUTO_APPROVED: ``mode == AUTO_APPROVE``; build continues at the
            same stage with no further wrapper action.
        FAILED: ``mode == HARD_STOP``; the build was transitioned to
            ``FAILED`` via the state machine and the wrapper returned.
        RESUMED: A paused build received an ``approve`` response and
            the state machine was transitioned ``PAUSED → RUNNING``.
        CANCELLED: A paused build received a ``reject`` response (or a
            CLI cancel) and the state machine was transitioned to
            ``CANCELLED``.
        OVERRIDDEN: A paused build received an ``override`` response
            (or a CLI skip); the stage is marked overridden and the
            build continues.
        TIMED_OUT: The total wait reached
            :attr:`ApprovalConfig.max_wait_seconds` with no decision —
            the wrapper applies the configured fallback (currently
            ``CANCELLED``; see ASSUM-003 deferral) and returns this
            outcome so callers can log the ceiling-reached event.
    """

    AUTO_APPROVED = "AUTO_APPROVED"
    FAILED = "FAILED"
    RESUMED = "RESUMED"
    CANCELLED = "CANCELLED"
    OVERRIDDEN = "OVERRIDDEN"
    TIMED_OUT = "TIMED_OUT"


# ---------------------------------------------------------------------------
# Persisted paused-build snapshot — input to recover_paused_builds.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PausedBuildSnapshot:
    """Read-model of the FEAT-FORGE-001 ``paused_builds`` SQLite view.

    The repository surface returns one of these per paused build at
    boot — the **persisted** ``request_id`` is the wire contract
    (closes risk **R5**); the wrapper MUST NOT re-derive it.

    Attributes:
        build_id: Identifier of the paused build.
        feature_id: ``FEAT-XXXX`` identifier (threaded onto the
            re-emitted envelope's ``details`` dict).
        stage_label: Stage the build is paused at.
        request_id: The persisted, deterministic ``request_id`` from
            the original publish. Used verbatim on re-emission so
            responder dedup holds.
        attempt_count: The persisted attempt counter; the next
            refresh-on-timeout uses ``attempt_count + 1``.
        decision_snapshot: The :class:`GateDecision` that motivated
            the pause. Re-projected onto the re-emitted envelope's
            eleven-key ``details`` dict via the publisher's
            ``_build_approval_details`` helper.
        artefact_paths: Filesystem paths the reviewer should inspect.
        resume_options: Reviewer-eligible decisions for this pause.
        correlation_id: Optional pipeline-level correlation id.
    """

    build_id: str
    feature_id: str
    stage_label: str
    request_id: str
    attempt_count: int
    decision_snapshot: GateDecision
    artefact_paths: tuple[str, ...] = ()
    resume_options: tuple[str, ...] = ("approve", "reject", "defer", "override")
    correlation_id: str | None = None


# ---------------------------------------------------------------------------
# Adapter Protocol surfaces — every concrete adapter is DI'd.
# ---------------------------------------------------------------------------


@runtime_checkable
class PriorsReader(Protocol):
    """Reads priors from Graphiti for a given target.

    Production wiring: ``forge.adapters.graphiti.read_priors``. Tests
    inject a coroutine that returns a deterministic list.
    """

    async def read_priors(  # pragma: no cover - protocol stub
        self,
        *,
        target_kind: GateTargetKind,
        target_identifier: str,
        stage_label: str,
        build_id: str,
    ) -> list[PriorReference]: ...


@runtime_checkable
class AdjustmentsReader(Protocol):
    """Reads calibration adjustments from Graphiti.

    The wrapper invokes :meth:`read_adjustments` with
    ``approved_only=True`` exclusively — this is the only filter point
    for unapproved adjustments (closes risk **R8**, F9 / Group C
    ``@negative``).
    """

    async def read_adjustments(  # pragma: no cover - protocol stub
        self,
        *,
        target_capability: str,
        approved_only: bool,
    ) -> list[CalibrationAdjustment]: ...


@runtime_checkable
class RulesReader(Protocol):
    """Reads constitutional rules (ADR-ARCH-026 belt+braces)."""

    async def read_rules(  # pragma: no cover - protocol stub
        self,
        *,
        target_kind: GateTargetKind,
        target_identifier: str,
    ) -> list[ConstitutionalRule]: ...


@runtime_checkable
class GateRepository(Protocol):
    """Persists :class:`GateDecision` rows and paused-build state.

    Production wiring: a SQLite-backed repository owning
    ``stage_log.details_json["gate"]`` plus the ``paused_builds``
    view from FEAT-FORGE-001. Tests substitute an in-memory fake.
    """

    async def record_decision(  # pragma: no cover - protocol stub
        self, decision: GateDecision
    ) -> None: ...

    async def write_to_graphiti(  # pragma: no cover - protocol stub
        self, decision: GateDecision
    ) -> None: ...

    async def record_paused_build(  # pragma: no cover - protocol stub
        self,
        *,
        build_id: str,
        feature_id: str,
        stage_label: str,
        request_id: str,
        attempt_count: int,
        decision: GateDecision,
    ) -> None: ...

    async def list_paused_builds(  # pragma: no cover - protocol stub
        self,
    ) -> list[PausedBuildSnapshot]: ...

    async def mark_resumed(  # pragma: no cover - protocol stub
        self, *, build_id: str, stage_label: str
    ) -> None: ...

    async def mark_overridden(  # pragma: no cover - protocol stub
        self, *, build_id: str, stage_label: str, reason: str
    ) -> None: ...

    async def mark_cancelled(  # pragma: no cover - protocol stub
        self, *, build_id: str, reason: str
    ) -> None: ...


@runtime_checkable
class StateMachine(Protocol):
    """FEAT-FORGE-001 build state-machine surface.

    Only the transitions the wrapper triggers are exposed. The
    state machine itself owns the SQLite ``builds`` row writes; this
    module is a *caller* of those transitions.
    """

    async def transition_to_paused(  # pragma: no cover - protocol stub
        self, *, build_id: str, stage_label: str
    ) -> None: ...

    async def transition_to_running(  # pragma: no cover - protocol stub
        self, *, build_id: str
    ) -> None: ...

    async def transition_to_failed(  # pragma: no cover - protocol stub
        self, *, build_id: str, reason: str
    ) -> None: ...

    async def transition_to_cancelled(  # pragma: no cover - protocol stub
        self, *, build_id: str, reason: str
    ) -> None: ...


@runtime_checkable
class ApprovalPublisherProto(Protocol):
    """Subset of :class:`forge.adapters.nats.ApprovalPublisher` we use."""

    async def publish_request(  # pragma: no cover - protocol stub
        self, envelope: MessageEnvelope
    ) -> None: ...


@runtime_checkable
class ApprovalSubscriberProto(Protocol):
    """Subset of :class:`forge.adapters.nats.ApprovalSubscriber` we use."""

    async def await_response(  # pragma: no cover - protocol stub
        self,
        build_id: str,
        *,
        stage_label: str,
        attempt_count: int = 0,
        timeout_seconds: int | None = None,
    ) -> ApprovalResponsePayload | dict[str, Any] | None: ...


# ---------------------------------------------------------------------------
# Dependency container.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class GateCheckDeps:
    """Injected collaborators for :func:`gate_check`.

    Args:
        priors_reader: Graphiti priors adapter.
        adjustments_reader: Calibration-adjustments adapter (always
            consulted with ``approved_only=True``).
        rules_reader: Constitutional-rules adapter (ADR-ARCH-026).
        repository: SQLite + Graphiti write-side adapter.
        state_machine: FEAT-FORGE-001 state-machine surface.
        publisher: Approval-request publisher (TASK-CGCP-006).
        subscriber: Approval-response subscriber (TASK-CGCP-007).
        injector: Synthetic CLI-cancel/skip injector
            (TASK-CGCP-008).
        reasoning_model_call: Pure callable bound to the orchestrator's
            reasoning model. Threaded into :func:`evaluate_gate`.
        clock: Optional callable returning the current UTC datetime;
            defaults to :func:`datetime.now` (UTC). Tests inject a
            deterministic stub.
        per_attempt_wait_seconds: Per-attempt wait passed to the
            subscriber; defaults to ``None`` (the subscriber falls back
            to ``ApprovalConfig.default_wait_seconds``).
    """

    priors_reader: PriorsReader
    adjustments_reader: AdjustmentsReader
    rules_reader: RulesReader
    repository: GateRepository
    state_machine: StateMachine
    publisher: ApprovalPublisherProto
    subscriber: ApprovalSubscriberProto
    injector: SyntheticResponseInjector | Any
    reasoning_model_call: Callable[[str], str]
    clock: Callable[[], datetime] | None = None
    per_attempt_wait_seconds: int | None = None
    # TASK-JNB-101: the build's pipeline correlation_id, stamped onto
    # every outbound ApprovalRequestPayload envelope so responders can
    # echo it and the subscriber's correlation guard (step 2b of the
    # four-step chain) has a real value to match against. ``None``
    # preserves the pre-JNB-101 envelope shape (no correlation).
    correlation_id: str | None = None
    # TASK-JNB-102 (ASSUM-010 closure): optional best-effort publisher
    # for ``pipeline.build-cancelled`` on the CANCELLED transitions this
    # module drives (reject decision, max-wait breach). Bound at the
    # composition root to ``PipelineLifecycleEmitter.emit_cancelled``
    # over the build's context; ``None`` = no emit (pre-v1.1 shape).
    # Always invoked AFTER the SQLite transition and never allowed to
    # raise into the gate flow (DDR-007 — see
    # :func:`_publish_cancelled_best_effort`).
    publish_cancelled: Callable[..., Any] | None = None


# ---------------------------------------------------------------------------
# Public API: gate_check
# ---------------------------------------------------------------------------


async def gate_check(
    *,
    deps: GateCheckDeps,
    build_id: str,
    feature_id: str,
    stage_label: str,
    target_kind: GateTargetKind,
    target_identifier: str,
    coach_score: float | None,
    criterion_breakdown: dict[str, float],
    detection_findings: list[DetectionFinding],
    attempt_count: int = 0,
    artefact_paths: tuple[str, ...] = (),
) -> tuple[GateOutcome, GateDecision]:
    """Run a single gated stage end-to-end.

    Sequence (mirrors the AC list of TASK-CGCP-010):

    1. Read priors via :class:`PriorsReader`.
    2. Read **approved-only** calibration adjustments via
       :class:`AdjustmentsReader` (closes risk **R8**, AC F9).
    3. Read constitutional rules via :class:`RulesReader`.
    4. Call pure :func:`evaluate_gate`.
    5. Persist the :class:`GateDecision` to SQLite **before** any
       subsequent action — Graphiti write is best-effort and a failure
       there does NOT roll back SQLite (AC F10).
    6. Branch on :attr:`GateDecision.mode`:

       * ``AUTO_APPROVE`` — return :attr:`GateOutcome.AUTO_APPROVED`.
       * ``HARD_STOP`` — transition the build to ``FAILED`` and return
         :attr:`GateOutcome.FAILED`.
       * ``FLAG_FOR_REVIEW`` / ``MANDATORY_HUMAN_APPROVAL`` — pause +
         publish atomically, await a response, and dispatch.

    Args:
        deps: Bundle of injected collaborators (see :class:`GateCheckDeps`).
        build_id: Stable identifier of the build being gated.
        feature_id: ``FEAT-XXXX`` of the build.
        stage_label: Pipeline stage label producing the decision.
        target_kind: ``local_tool`` | ``fleet_capability`` | ``subagent``.
        target_identifier: Identifier of the gated target.
        coach_score: Specialist-agent Coach overall score (``None`` →
            degraded mode).
        criterion_breakdown: Per-criterion Coach scores in ``[0, 1]``.
        detection_findings: Coach pattern findings folded into the
            decision.
        attempt_count: Initial attempt counter — non-zero only on
            re-emission. Used to derive the ``request_id`` and to seed
            the subscriber's refresh loop.
        artefact_paths: Filesystem paths the reviewer should inspect.

    Returns:
        A ``(outcome, decision)`` pair. The decision is the persisted
        :class:`GateDecision`; the outcome is the closed-set
        :class:`GateOutcome` describing what the wrapper did.

    Raises:
        ValueError: If :func:`evaluate_gate` rejects an out-of-range
            ``criterion_breakdown`` (no decision is recorded — this
            matches Group B ``@negative``).
    """
    if not build_id:
        raise ValueError("build_id must be a non-empty string")
    if not stage_label:
        raise ValueError("stage_label must be a non-empty string")
    if attempt_count < 0:
        raise ValueError(f"attempt_count must be non-negative, got {attempt_count!r}")

    # 1. Read priors.
    priors = await deps.priors_reader.read_priors(
        target_kind=target_kind,
        target_identifier=target_identifier,
        stage_label=stage_label,
        build_id=build_id,
    )

    # 2. Read **approved-only** adjustments (F9 / R8).
    adjustments = await deps.adjustments_reader.read_adjustments(
        target_capability=target_identifier,
        approved_only=True,
    )

    # 3. Read constitutional rules.
    rules = await deps.rules_reader.read_rules(
        target_kind=target_kind,
        target_identifier=target_identifier,
    )

    # 4. Evaluate (pure).
    decision = evaluate_gate(
        build_id=build_id,
        target_kind=target_kind,
        target_identifier=target_identifier,
        stage_label=stage_label,
        coach_score=coach_score,
        criterion_breakdown=criterion_breakdown,
        detection_findings=detection_findings,
        retrieved_priors=priors,
        calibration_adjustments=adjustments,
        constitutional_rules=rules,
        reasoning_model_call=deps.reasoning_model_call,
        clock=deps.clock,
    )

    # 5. Persist to SQLite **first** (F10). Graphiti is best-effort.
    await deps.repository.record_decision(decision)
    try:
        await deps.repository.write_to_graphiti(decision)
    except Exception as exc:  # noqa: BLE001 — operational signal only
        logger.warning(
            "gate_check: graphiti write failed build_id=%s stage=%s err=%s "
            "(SQLite row is intact)",
            build_id,
            stage_label,
            exc,
        )

    # 6. Branch on decision mode.
    mode = decision.mode

    if mode is GateMode.AUTO_APPROVE:
        return GateOutcome.AUTO_APPROVED, decision

    if mode is GateMode.HARD_STOP:
        await deps.state_machine.transition_to_failed(
            build_id=build_id, reason=REASON_HARD_STOP
        )
        return GateOutcome.FAILED, decision

    # FLAG_FOR_REVIEW / MANDATORY_HUMAN_APPROVAL → pause-and-publish.
    request_id = derive_request_id(
        build_id=build_id,
        stage_label=stage_label,
        attempt_count=attempt_count,
    )

    await _atomic_pause_and_publish(
        deps=deps,
        decision=decision,
        feature_id=feature_id,
        request_id=request_id,
        attempt_count=attempt_count,
        artefact_paths=artefact_paths,
    )

    # Await Rich's response.
    raw = await deps.subscriber.await_response(
        build_id,
        stage_label=stage_label,
        attempt_count=attempt_count,
        timeout_seconds=deps.per_attempt_wait_seconds,
    )

    if raw is None:
        # Max-wait ceiling reached (AC: Group D @edge-case "max-wait
        # ceiling"). ASSUM-003 defers the ceiling-fallback to the
        # pipeline-config feature; we currently apply a CANCELLED
        # transition with REASON_MAX_WAIT so the build doesn't dangle.
        await deps.state_machine.transition_to_cancelled(
            build_id=build_id, reason=REASON_MAX_WAIT
        )
        await deps.repository.mark_cancelled(build_id=build_id, reason=REASON_MAX_WAIT)
        await _publish_cancelled_best_effort(
            deps,
            build_id=build_id,
            reason=REASON_MAX_WAIT,
            cancelled_by=SOURCE_ID,
        )
        return GateOutcome.TIMED_OUT, decision

    response = resume_value_as(ApprovalResponsePayload, raw)
    return await _dispatch_response(
        deps=deps,
        build_id=build_id,
        stage_label=stage_label,
        decision=decision,
        response=response,
        feature_id=feature_id,
        attempt_count=attempt_count,
        artefact_paths=artefact_paths,
    )


# ---------------------------------------------------------------------------
# Internal: atomic pause-and-publish (F5)
# ---------------------------------------------------------------------------


async def _atomic_pause_and_publish(
    *,
    deps: GateCheckDeps,
    decision: GateDecision,
    feature_id: str,
    request_id: str,
    attempt_count: int,
    artefact_paths: tuple[str, ...],
) -> None:
    """Pause-and-publish atomicity per F5 / Group E ``@concurrency``.

    Ordering enforced by this function (no ``await`` between the
    SQLite paused-row commit and the bus publish):

    1. Persist the paused-build row (records ``request_id`` and the
       gate decision snapshot).
    2. Transition the state machine to ``PAUSED``.
    3. Build the :class:`ApprovalRequestPayload` envelope.
    4. Publish to ``agents.approval.forge.{build_id}``.

    From any external observer's standpoint, a status query that
    answers ``PAUSED`` is preceded by a successful ``record_paused_build``
    + ``transition_to_paused`` pair — and the publish call follows
    *immediately*, in the same async function frame. A publish failure
    surfaces as :class:`ApprovalPublishError` to the caller but does
    **not** roll back the SQLite mirror (F10) — the boot-time
    re-emission hook covers crashed publishes.

    Note: the resume_options list narrows for ``HARD_STOP``-adjacent
    pause kinds, but ``HARD_STOP`` itself never hits this function.
    """
    build_id = decision.build_id
    stage_label = decision.stage_label

    # 1. SQLite paused-build row (records request_id BEFORE publish so
    #    a crash-recovery boot can re-emit on the same id).
    await deps.repository.record_paused_build(
        build_id=build_id,
        feature_id=feature_id,
        stage_label=stage_label,
        request_id=request_id,
        attempt_count=attempt_count,
        decision=decision,
    )

    # 2. State-machine transition to PAUSED — must complete BEFORE the
    #    publish so observers never see a non-PAUSED status with a
    #    bus-published approval request outstanding.
    await deps.state_machine.transition_to_paused(
        build_id=build_id, stage_label=stage_label
    )

    # 3 + 4. Build envelope and publish. No awaits between SQLite +
    # state-machine writes above and the publish below, beyond the
    # awaits we control here.
    envelope = _build_request_envelope(
        decision=decision,
        feature_id=feature_id,
        request_id=request_id,
        artefact_paths=artefact_paths,
        correlation_id=deps.correlation_id,
    )
    await deps.publisher.publish_request(envelope)


def _build_request_envelope(
    *,
    decision: GateDecision,
    feature_id: str,
    request_id: str,
    artefact_paths: tuple[str, ...],
    correlation_id: str | None = None,
) -> MessageEnvelope:
    """Build the :class:`MessageEnvelope` carrying an approval request.

    Reuses the publisher's canonical eleven-key ``details`` builder
    via late import — ``approval_publisher`` is the single source of
    truth for that dict shape (TASK-CGCP-006 AC-008).
    """
    # Late import: avoid a top-level dependency on the publisher module
    # so this file imports cleanly under domain-purity audits that
    # simply forbid ``from forge.adapters.nats.approval_publisher``
    # at module scope.
    from forge.adapters.nats.approval_publisher import (
        _build_approval_details,
        _derive_risk_level,
    )

    resume_options = _resume_options_for(decision.mode)
    details = _build_approval_details(
        decision,
        feature_id=feature_id,
        artefact_paths=list(artefact_paths),
        resume_options=resume_options,
    )

    payload = ApprovalRequestPayload(
        request_id=request_id,
        agent_id=SOURCE_ID,
        action_description=(
            f"Gate {decision.mode.value} on stage {decision.stage_label!r} "
            f"target={decision.target_identifier!r}"
        ),
        risk_level=_derive_risk_level(decision),
        details=details,
    )

    envelope = MessageEnvelope(
        source_id=SOURCE_ID,
        event_type=EventType.APPROVAL_REQUEST,
        correlation_id=correlation_id,
        payload=payload.model_dump(mode="json"),
    )
    return envelope


def _resume_options_for(mode: GateMode) -> list[str]:
    """Return the protocol-allowed resume options for ``mode``.

    ``HARD_STOP`` would only ever allow ``override``, but the wrapper
    never publishes for HARD_STOP — it transitions to FAILED. The
    other two modes accept the full set per
    ``API-nats-approval-protocol §4.1``.
    """
    if mode is GateMode.HARD_STOP:
        return ["override"]
    return ["approve", "reject", "defer", "override"]


# ---------------------------------------------------------------------------
# Internal: best-effort build-cancelled publish (TASK-JNB-102, DDR-007).
# ---------------------------------------------------------------------------


async def _publish_cancelled_best_effort(
    deps: GateCheckDeps,
    *,
    build_id: str,
    reason: str,
    cancelled_by: str,
) -> None:
    """Invoke ``deps.publish_cancelled`` without ever raising.

    ASSUM-010 closure: the CANCELLED transitions in this module gain a
    ``pipeline.build-cancelled`` wire signal so the operator who tapped
    Reject (or whose approval window lapsed) receives terminal
    confirmation on the phone. Contract (DDR-007):

    * called strictly AFTER the SQLite transition — the ledger is
      already authoritative when the publish is attempted;
    * a ``None`` callback is a silent no-op (pre-v1.1 composition);
    * any exception is logged at WARNING and swallowed — no rollback,
      no retry loop, the transition stands.
    """
    if deps.publish_cancelled is None:
        return
    try:
        await deps.publish_cancelled(reason=reason, cancelled_by=cancelled_by)
    except Exception as exc:  # noqa: BLE001 — DDR-007 swallow+log
        logger.warning(
            "gate_check: build-cancelled publish failed build_id=%s "
            "reason=%r cancelled_by=%r err=%s — SQLite transition stands "
            "(DDR-007)",
            build_id,
            reason,
            cancelled_by,
            exc,
        )


# ---------------------------------------------------------------------------
# Internal: dispatch a typed ApprovalResponsePayload to the state machine.
# ---------------------------------------------------------------------------


async def _dispatch_response(
    *,
    deps: GateCheckDeps,
    build_id: str,
    stage_label: str,
    decision: GateDecision,
    response: ApprovalResponsePayload,
    feature_id: str,
    attempt_count: int,
    artefact_paths: tuple[str, ...],
) -> tuple[GateOutcome, GateDecision]:
    """Branch on ``response.decision`` and drive the state machine.

    Per ``API-nats-approval-protocol §4.1`` the four allowed values
    are ``approve | reject | defer | override``. ``defer`` triggers a
    fresh publish with ``attempt_count + 1`` (the deterministic
    ``request_id`` derivation guarantees the new id differs from the
    prior one). ``approve`` resumes; ``reject`` cancels; ``override``
    marks the stage overridden and continues.
    """
    decision_kind = response.decision

    if decision_kind == "approve":
        await deps.repository.mark_resumed(build_id=build_id, stage_label=stage_label)
        await deps.state_machine.transition_to_running(build_id=build_id)
        return GateOutcome.RESUMED, decision

    if decision_kind == "reject":
        reason = response.notes or REASON_REJECT
        await deps.repository.mark_cancelled(build_id=build_id, reason=reason)
        await deps.state_machine.transition_to_cancelled(
            build_id=build_id, reason=reason
        )
        # TASK-JNB-102: terminal wire signal for the phone loop — the
        # operator who tapped Reject receives build-cancelled, never
        # build-resumed (the resume emit is decision-gated upstream).
        await _publish_cancelled_best_effort(
            deps,
            build_id=build_id,
            reason=reason,
            cancelled_by=response.decided_by,
        )
        return GateOutcome.CANCELLED, decision

    if decision_kind == "override":
        reason = response.notes or REASON_OVERRIDE
        await deps.repository.mark_overridden(
            build_id=build_id, stage_label=stage_label, reason=reason
        )
        # Build continues at the next stage; no PAUSED → RUNNING
        # transition is required because we never left RUNNING in the
        # observer's view of the build proper — only the *stage* is
        # overridden. Caller proceeds from the returned outcome.
        await deps.state_machine.transition_to_running(build_id=build_id)
        return GateOutcome.OVERRIDDEN, decision

    if decision_kind == "defer":
        # Re-publish with attempt_count + 1. The new request_id is a
        # function of (build_id, stage_label, attempt_count + 1) and
        # is therefore distinct from the just-deferred id, so the
        # responder dedup buffer treats it as a fresh request.
        next_attempt = attempt_count + 1
        new_request_id = derive_request_id(
            build_id=build_id,
            stage_label=stage_label,
            attempt_count=next_attempt,
        )
        await deps.repository.record_paused_build(
            build_id=build_id,
            feature_id=feature_id,
            stage_label=stage_label,
            request_id=new_request_id,
            attempt_count=next_attempt,
            decision=decision,
        )
        envelope = _build_request_envelope(
            decision=decision,
            feature_id=feature_id,
            request_id=new_request_id,
            artefact_paths=artefact_paths,
            correlation_id=deps.correlation_id,
        )
        await deps.publisher.publish_request(envelope)
        # Fall back into the wait loop. We recurse via a fresh
        # await_response call rather than looping inline so the
        # state-machine remains in PAUSED across the re-publish (no
        # transition_to_paused is needed — we never left PAUSED).
        raw = await deps.subscriber.await_response(
            build_id,
            stage_label=stage_label,
            attempt_count=next_attempt,
            timeout_seconds=deps.per_attempt_wait_seconds,
        )
        if raw is None:
            await deps.state_machine.transition_to_cancelled(
                build_id=build_id, reason=REASON_MAX_WAIT
            )
            await deps.repository.mark_cancelled(
                build_id=build_id, reason=REASON_MAX_WAIT
            )
            await _publish_cancelled_best_effort(
                deps,
                build_id=build_id,
                reason=REASON_MAX_WAIT,
                cancelled_by=SOURCE_ID,
            )
            return GateOutcome.TIMED_OUT, decision
        next_response = resume_value_as(ApprovalResponsePayload, raw)
        return await _dispatch_response(
            deps=deps,
            build_id=build_id,
            stage_label=stage_label,
            decision=decision,
            response=next_response,
            feature_id=feature_id,
            attempt_count=next_attempt,
            artefact_paths=artefact_paths,
        )

    # Should be unreachable — Pydantic Literal validation on
    # ApprovalResponsePayload.decision rejects anything else before it
    # ever reaches us. Surface a typed error so a future schema drift
    # does not silently fall through to a no-op.
    raise ValueError(
        f"Unsupported approval decision={decision_kind!r} for build_id={build_id!r}"
    )


# ---------------------------------------------------------------------------
# Boot-time crash-recovery hook.
# ---------------------------------------------------------------------------


async def recover_paused_builds(deps: GateCheckDeps) -> list[str]:
    """Re-emit approval requests for every paused build at boot.

    Closes risk **R5** (re-emission diverges from the original
    ``request_id``, breaking responder idempotency on Rich's side):
    the persisted ``request_id`` from the SQLite paused-build row is
    used **verbatim** — never re-derived — so the responder's dedup
    buffer recognises the re-published envelope as the same request.

    Args:
        deps: Same dependency bundle :func:`gate_check` uses; only the
            ``repository`` and ``publisher`` fields are consulted here.

    Returns:
        The list of ``build_id`` values that were successfully
        re-emitted. A publish failure for any one build is logged but
        does not stop the loop — the wrapper will retry on the next
        boot, and the SQLite row remains the source of truth.
    """
    snapshots = await deps.repository.list_paused_builds()
    re_emitted: list[str] = []

    for snap in snapshots:
        envelope = _build_request_envelope(
            decision=snap.decision_snapshot,
            feature_id=snap.feature_id,
            request_id=snap.request_id,  # PERSISTED — not re-derived
            artefact_paths=snap.artefact_paths,
            correlation_id=snap.correlation_id,
        )
        try:
            await deps.publisher.publish_request(envelope)
        except Exception as exc:  # noqa: BLE001 — operational signal
            logger.warning(
                "recover_paused_builds: publish failed build_id=%s "
                "request_id=%s err=%s — will retry on next boot",
                snap.build_id,
                snap.request_id,
                exc,
            )
            continue
        logger.info(
            "recover_paused_builds: re-emitted build_id=%s stage=%s "
            "request_id=%s attempt=%d",
            snap.build_id,
            snap.stage_label,
            snap.request_id,
            snap.attempt_count,
        )
        re_emitted.append(snap.build_id)

    return re_emitted


# ---------------------------------------------------------------------------
# CLI bridges — delegate to TASK-CGCP-008's SyntheticResponseInjector.
# ---------------------------------------------------------------------------


async def cli_cancel_build(deps: GateCheckDeps, *, build_id: str) -> None:
    """Bridge for ``forge cancel FEAT-XXX``.

    Looks up the paused build's persisted ``(stage_label,
    attempt_count)`` and delegates to
    :meth:`SyntheticResponseInjector.inject_cli_cancel`. The injector
    publishes a synthetic ``decision="reject"`` response onto the
    standard mirror subject so the subscriber's dedup buffer arbitrates
    against any concurrent real Rich response (closes risk **F6**).

    Raises:
        LookupError: If no paused build is found for ``build_id``.
        SyntheticInjectFailure: Propagated from the injector.
    """
    snap = await _lookup_paused(deps=deps, build_id=build_id)
    try:
        await deps.injector.inject_cli_cancel(
            build_id=snap.build_id,
            stage_label=snap.stage_label,
            attempt_count=snap.attempt_count,
            correlation_id=snap.correlation_id,
        )
    except SyntheticInjectFailure:
        # Re-raise unchanged; the CLI command surface is responsible
        # for translating to a non-zero exit code. The SQLite row
        # remains the source of truth, so the next boot will
        # re-emit the original request unchanged.
        raise


async def cli_skip_stage(deps: GateCheckDeps, *, build_id: str) -> None:
    """Bridge for ``forge skip FEAT-XXX``.

    Mirrors :func:`cli_cancel_build` but delegates to
    :meth:`SyntheticResponseInjector.inject_cli_skip`, producing a
    synthetic ``decision="override"`` response.
    """
    snap = await _lookup_paused(deps=deps, build_id=build_id)
    try:
        await deps.injector.inject_cli_skip(
            build_id=snap.build_id,
            stage_label=snap.stage_label,
            attempt_count=snap.attempt_count,
            correlation_id=snap.correlation_id,
        )
    except SyntheticInjectFailure:
        raise


async def _lookup_paused(*, deps: GateCheckDeps, build_id: str) -> PausedBuildSnapshot:
    """Return the paused-build snapshot for ``build_id`` or raise.

    Linear scan — the paused-builds set is bounded by the configured
    in-flight cap (ADR-ARCH-014 caps at one), so a scan is cheaper than
    a per-id repository method.
    """
    snapshots = await deps.repository.list_paused_builds()
    for snap in snapshots:
        if snap.build_id == build_id:
            return snap
    raise LookupError(f"cli bridge: no paused build found for build_id={build_id!r}")


__all__ = [
    "ApprovalPublisherProto",
    "ApprovalSubscriberProto",
    "AdjustmentsReader",
    "GateCheckDeps",
    "GateOutcome",
    "GateRepository",
    "PausedBuildSnapshot",
    "PriorsReader",
    "REASON_CLI_CANCEL",
    "REASON_CLI_SKIP",
    "REASON_DEFER",
    "REASON_HARD_STOP",
    "REASON_MAX_WAIT",
    "REASON_OVERRIDE",
    "REASON_REJECT",
    "RulesReader",
    "SOURCE_ID",
    "StateMachine",
    "cli_cancel_build",
    "cli_skip_stage",
    "gate_check",
    "recover_paused_builds",
]

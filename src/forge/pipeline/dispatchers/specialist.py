"""Specialist-stage dispatcher for product-owner and architect (TASK-MAG7-007).

This module is the thin composition layer that wires the three
upstream contracts the Mode A specialist stages depend on:

1. :class:`~forge.pipeline.forward_context_builder.ForwardContextBuilder`
   (TASK-MAG7-006) — builds the ``--context`` payload entries the
   downstream specialist receives.
2. FEAT-FORGE-003's specialist-dispatch surface
   (:class:`~forge.dispatch.orchestrator.DispatchOrchestrator`) — owns
   capability resolution, correlation-key fabrication, persistence,
   the subscribe-before-publish ordering, and reply parsing. The
   dispatcher in this module never re-implements any of that; it only
   *calls* it.
3. A narrow :class:`StageLogWriter` Protocol — records the
   ``stage_log`` row on submit and updates it on reply with the
   parsed Coach score and detection findings.

The dispatcher itself fans the work out for exactly two stages —
:attr:`~forge.pipeline.stage_taxonomy.StageClass.PRODUCT_OWNER` and
:attr:`~forge.pipeline.stage_taxonomy.StageClass.ARCHITECT` — and
refuses (raises :class:`ValueError`) for any other stage class. This
matches FEAT-FORGE-007 Group A where the product-owner and architect
are the only specialist stages: every other stage is a subprocess
(:mod:`forge.pipeline.dispatchers.subprocess`, TASK-MAG7-008) or an
async autobuild (:mod:`forge.pipeline.dispatchers.autobuild`,
TASK-MAG7-009) and travels via a different surface.

Outcome translation
-------------------

The FEAT-FORGE-003 :data:`~forge.dispatch.models.DispatchOutcome`
discriminated union is translated into the dispatcher-local
:class:`StageDispatchResult` that the gating layer (FEAT-FORGE-004)
consumes:

============================  ======================================  ==============================
DispatchOutcome.kind          StageDispatchOutcome                    Notes
============================  ======================================  ==============================
``sync_result``               :attr:`StageDispatchOutcome.COMPLETED`  Coach score + breakdown +
                                                                      findings copied through.
``degraded``                  :attr:`StageDispatchOutcome.DEGRADED`   Group C ``@negative``
                                                                      "no product-owner specialist"
                                                                      — gating maps to FLAG_FOR_REVIEW.
``error`` (``local_timeout``) :attr:`StageDispatchOutcome.SOFT_TIMEOUT` Soft failure — supervisor
                                                                      decides retry-with-context
                                                                      (ASSUM-005), not us.
``error`` (other)             :attr:`StageDispatchOutcome.ERROR`      Surface the original
                                                                      ``error_explanation``.
``async_pending``             :attr:`StageDispatchOutcome.ERROR`      Specialist dispatch must be
                                                                      synchronous in Mode A
                                                                      (FEAT-FORGE-007 ASSUM-002);
                                                                      pending replies are a
                                                                      protocol violation here.
============================  ======================================  ==============================

Retry-with-context behaviour on soft timeout is invoked at the
supervisor layer, **not** here. This module's only job is to thread
the structured result back so the supervisor can decide.

References
----------

- TASK-MAG7-007 — this task.
- TASK-MAG7-006 — :class:`ForwardContextBuilder`.
- TASK-MAG7-001 — :class:`StageClass`, ``PER_FEATURE_STAGES``.
- FEAT-FORGE-003 — :class:`DispatchOrchestrator`,
  :data:`DispatchOutcome`, :class:`DispatchParameter`.
- FEAT-FORGE-007 Group A — specialist scenarios; Group C @negative —
  "no product-owner specialist"; Group I @data-integrity —
  correlation_id threading.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar, Protocol, runtime_checkable

from forge.dispatch.models import (
    AsyncPending,
    Degraded,
    DispatchError,
    DispatchOutcome,
    SyncResult,
)
from forge.dispatch.persistence import DispatchParameter
from forge.pipeline.forward_context_builder import (
    ContextEntry,
    ForwardContextBuilder,
)
from forge.pipeline.stage_taxonomy import StageClass

__all__ = [
    "SPECIALIST_CAPABILITY_BY_STAGE",
    "SPECIALIST_COMMAND_BY_STAGE",
    "SPECIALIST_INTENT_BY_STAGE",
    "SPECIALIST_REQUIRED_ARGS_BY_STAGE",
    "SPECIALIST_STAGES",
    "SpecialistDispatchSurface",
    "StageDispatchOutcome",
    "StageDispatchResult",
    "StageLogWriter",
    "build_specialist_command",
    "dispatch_specialist_stage",
]

logger = logging.getLogger(__name__)


#: Capability names this dispatcher resolves. The FEAT-FORGE-003
#: capability registry exposes these two tool names; the mapping is
#: deliberately closed (a bare ``dict``) so adding a third stage requires
#: an explicit code edit and a corresponding test rather than a silent
#: fallthrough.
SPECIALIST_CAPABILITY_BY_STAGE: dict[StageClass, str] = {
    StageClass.PRODUCT_OWNER: "product_owner_specialist",
    StageClass.ARCHITECT: "architect_specialist",
    # Lane B / Phase E1 (B2) — the target-terminal spec/plan legs. These are
    # NOT dispatched in Mode A/B/C (there FEATURE_SPEC/FEATURE_PLAN are
    # subprocess stages, see supervisor._SUBPROCESS_STAGES); they are only
    # reached by the Mode P planning driver's target terminal, which calls
    # dispatch_specialist_stage directly. Same agents as PO/architect, spec
    # and plan modes: FEAT-SPL-007 (po_feature_spec) / FEAT-SPL-008
    # (architect_feature_plan) per sovereign-planning-loop-scope §4/§5.
    StageClass.FEATURE_SPEC: "po_feature_spec",
    StageClass.FEATURE_PLAN: "architect_feature_plan",
}


#: Intent-pattern fallbacks per specialist stage (TASK-FWD-PLAN-PODISCO).
#:
#: No live specialist agent advertises a *tool* named after
#: :data:`SPECIALIST_CAPABILITY_BY_STAGE`, so ``resolve()``'s exact-tool step
#: always misses for these stages. The live ``product-owner-agent`` instead
#: advertises tools ``po_idea`` / ``po_greenfield`` / ``po_scope`` / ... plus
#: an ``IntentCapability(pattern="product.*", confidence=0.95)``; the
#: ``architect-agent`` advertises ``architect_*`` tools plus an
#: ``IntentCapability(pattern="architecture.*", confidence=0.95)``. Passing
#: these patterns lets forge's *own* exact-tool → intent-fallback algorithm
#: (``forge.discovery.resolve.resolve`` step 2) resolve the stage.
#:
#: The value here is matched by ``resolve``'s intent step:
#: ``_filter_intent_candidates`` → ``_matching_intent`` →
#: ``_pattern_matches(intent.pattern, requested_pattern)``. Because we pass the
#: agent's *own* advertised pattern string, ``_pattern_matches`` returns True on
#: its exact-equality branch (``manifest_pattern == requested_pattern``) — it is
#: a *pattern* match, not a signal or substring match (``signals`` are never
#: consulted by the matcher). The agents' 0.95 confidence clears the 0.70
#: ``min_confidence`` floor. The exact tool name is still passed first
#: (see :func:`dispatch_specialist_stage`), so if an agent ever advertises the
#: exact tool, ``tool_exact`` still wins and this fallback is never used.
SPECIALIST_INTENT_BY_STAGE: dict[StageClass, str] = {
    StageClass.PRODUCT_OWNER: "product.*",
    StageClass.ARCHITECT: "architecture.*",
    # Lane B (B2): 007 rides the product-owner agent's ``product.*`` intent,
    # 008 the architect agent's ``architecture.*`` intent — the same
    # advertised patterns as the PO/architect stages, so the exact-tool ->
    # intent-fallback resolver reaches the live agents (the M-08/PODISCO
    # resolution, extended to the spec/plan tools).
    StageClass.FEATURE_SPEC: "product.*",
    StageClass.FEATURE_PLAN: "architecture.*",
}


#: The two stage classes this dispatcher owns. Exposed as a frozenset so
#: the supervisor (and tests) can ask ``stage in SPECIALIST_STAGES``
#: without re-encoding the predicate.
SPECIALIST_STAGES: frozenset[StageClass] = frozenset(
    SPECIALIST_CAPABILITY_BY_STAGE.keys()
)


#: Deployed command verb per specialist stage (M2, DISPATCHFMT+ S2, contract
#: D1). Both containers **overload** ``greenfield`` — verified in-container
#: against the deployed ``command_router.py`` (13-day image / nats-core 0.4.0):
#:
#: * the product-owner ``command_map`` routes ``greenfield`` →
#:   ``_handle_po_greenfield`` (``run_product_session(mode="greenfield")``,
#:   required arg ``problem_statement``);
#: * the architect ``command_map`` routes the SAME verb ``greenfield`` →
#:   ``_handle_greenfield`` (``run_architecture_session``, required args
#:   ``docs_path`` + ``scope``).
#:
#: The target agent (the ``agents.command.{agent_id}`` subject) disambiguates
#: the two handlers; ``greenfield`` is each role's from-scratch analog (the
#: architect's other verbs — ``align`` / ``explore`` / ``feasibility`` — are
#: revise/inspect/quick-check modes, not from-scratch generation).
SPECIALIST_COMMAND_BY_STAGE: dict[StageClass, str] = {
    StageClass.PRODUCT_OWNER: "greenfield",
    StageClass.ARCHITECT: "greenfield",
    # Lane B (B2): the deployed verbs for the 007/008 tools. The exact verb
    # strings BIND at Lane A / B4 (FEAT-SPL-007/008 land the specialist-side
    # handlers); until then B2 is hermetic (stubbed replies) so the wire verb
    # is exercised only against the test bus. Named for the tool they invoke.
    StageClass.FEATURE_SPEC: "feature_spec",
    StageClass.FEATURE_PLAN: "feature_plan",
}


#: Required deployed-handler argument names per specialist stage, mirroring the
#: deployed ``_PO_REQUIRED_ARGS`` / ``_REQUIRED_ARGS`` tables for the
#: ``greenfield`` verb (M3). The dispatch build sources a non-empty value for
#: each of these before the command reaches the wire; the deployed router's
#: ``_check_required_args`` gate rejects any command missing one of them.
SPECIALIST_REQUIRED_ARGS_BY_STAGE: dict[StageClass, tuple[str, ...]] = {
    StageClass.PRODUCT_OWNER: ("problem_statement",),
    StageClass.ARCHITECT: ("docs_path", "scope"),
    # Lane B (B2): the required inputs forge ALWAYS supplies for the
    # target-terminal legs (threaded via ``extra_command_args``, never sourced
    # from request_text/forward-context). 007 reads the committed
    # feature_spec_inputs content (``spec_input``); 008 reads the scope + the
    # target-repo descriptor + the forge-minted FEAT id (RV-1: forge mints and
    # threads ``feature_id``; the plan leg asserts the SUPPLIED id).
    StageClass.FEATURE_SPEC: ("spec_input",),
    StageClass.FEATURE_PLAN: ("scope", "target_repo", "feature_id"),
}


# Parameter name carrying the build's correlation_id onto the dispatch
# envelope. Group I @data-integrity asserts that the correlation_id is
# threaded *unchanged* from the build context onto every dispatch — we
# pin the parameter name as a module-level constant so the assertion
# point is one line of grep, not a string-literal hunt.
_CORRELATION_PARAMETER: str = "correlation_id"

# Parameter name carrying a forward-context entry. The capability tool
# accepts a list of these on the receiving side; we encode each entry
# as ``flag=value`` so the path/text discriminator is visible to the
# Coach reviewer and the parameter shape stays a flat list.
#
# D3 (DISPATCHFMT+ S2): these forward-context parameters are persisted for
# audit but are NOT serialised onto the command wire — no deployed greenfield
# handler (PO or architect) reads a generic ``context`` / ``parameters``
# argument (verified in-container). They are dropped from ``command_args``
# unless a context entry's flag directly names one of the target verb's
# deployed-read arguments (see :func:`build_specialist_command`).
_CONTEXT_PARAMETER: str = "context"


def build_specialist_command(
    stage: StageClass,
    *,
    request_text: str | None,
    context_entries: Sequence[ContextEntry],
    extra_command_args: Mapping[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Resolve the deployed verb + argument dict for one specialist stage.

    This is the data-driven "dispatch build" that fixes M2 (a real deployed
    verb) and M3 (a ``dict`` args carrying that verb's required inputs),
    per contract decisions D1 + D3:

    * **command** — :data:`SPECIALIST_COMMAND_BY_STAGE` for the stage
      (``greenfield`` for both specialist stages; the target agent
      disambiguates the PO vs architect handler).
    * **args** — the deployed-handler argument dict:

      - The chain-entry stage (:attr:`StageClass.PRODUCT_OWNER`) has no
        upstream artefact; its sole required input is the raw planning
        request text, threaded here as the greenfield ``problem_statement``
        (D1). ``request_text`` is the verbatim
        :class:`~forge.planning.run_store` ``request_text`` column value.
      - Downstream stages source their deployed-read arguments from the
        forward-context entries whose (dash-normalised) ``flag`` names one of
        the target verb's required arguments
        (:data:`SPECIALIST_REQUIRED_ARGS_BY_STAGE`). Forward-context entries
        that do **not** name a deployed-read argument are dropped — D3, since
        no deployed greenfield handler reads a generic ``context`` /
        ``parameters`` argument.

    The build is **best-effort**: a required argument that cannot be sourced is
    simply absent from the returned dict (the caller logs it and the deployed
    router's ``_check_required_args`` gate is the single enforcement point).
    On the live Mode-P planning path ``request_text`` is always populated, so
    the PO greenfield ``problem_statement`` is always present.

    Args:
        stage: The specialist stage. Must be in :data:`SPECIALIST_STAGES`.
        request_text: The raw planning request text (the PO ``problem_statement``
            source). ``None`` / blank for stages that take no raw request.
        context_entries: Forward-context entries from
            :meth:`ForwardContextBuilder.build_for` for this stage.

    Returns:
        ``(command, args)`` — the wire ``CommandPayload.command`` and
        ``CommandPayload.args`` the publisher stamps onto the envelope.

    Raises:
        ValueError: If ``stage`` is not a specialist stage (programming error,
            mirrors :func:`dispatch_specialist_stage`).
    """
    if stage not in SPECIALIST_COMMAND_BY_STAGE:
        raise ValueError(
            "build_specialist_command refuses stage="
            f"{stage!r}; expected one of "
            f"{sorted(s.value for s in SPECIALIST_STAGES)}",
        )

    command = SPECIALIST_COMMAND_BY_STAGE[stage]
    required = SPECIALIST_REQUIRED_ARGS_BY_STAGE.get(stage, ())
    args: dict[str, Any] = {}

    # Chain-entry stage: the raw request text IS the greenfield problem
    # statement. Only inject when it names a required arg for this stage
    # (``problem_statement`` for the product-owner) and carries real content.
    if (
        stage is StageClass.PRODUCT_OWNER
        and request_text is not None
        and request_text.strip()
        and "problem_statement" in required
    ):
        args["problem_statement"] = request_text.strip()

    # Downstream stages: map a forward-context entry into args ONLY when its
    # dash-normalised flag names a deployed-read argument of the target verb
    # (D3). Everything else — the ``--context`` charter/path payloads no
    # greenfield handler reads — is dropped from the wire.
    for entry in context_entries:
        arg_name = entry.flag.lstrip("-")
        if arg_name in required and entry.value.strip():
            args.setdefault(arg_name, entry.value)

    # Lane B (B2): forge-supplied inputs for the target-terminal legs
    # (``spec_input`` for 007; ``scope`` / ``target_repo`` / ``feature_id`` for
    # 008). These are sourced by the caller (the planning driver's spec/plan
    # legs), not from request_text/forward-context — the fixed division of
    # labour where FORGE owns the artifacts and mints/threads the FEAT id.
    # ``setdefault`` keeps any value already sourced above authoritative.
    if extra_command_args:
        for arg_name, value in extra_command_args.items():
            if str(value).strip():
                args.setdefault(arg_name, value)

    return command, args


class StageDispatchOutcome(StrEnum):
    """Closed set of outcomes the dispatcher returns to the gating layer.

    The dispatcher does not raise for runtime-domain failures
    (degraded specialist, soft timeout, transport error). Those are
    expected outcomes the gating layer reasons over; raising would
    force every caller to wrap the dispatch in try/except just to
    discover what happened. :class:`ValueError` is reserved for
    *programming* errors — passing a stage class outside
    :data:`SPECIALIST_STAGES` (AC-002) — because that is a bug the
    caller must fix, not a runtime branch the gating layer should
    consume.
    """

    COMPLETED = "completed"
    DEGRADED = "degraded"
    SOFT_TIMEOUT = "soft_timeout"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class StageDispatchResult:
    """Structured outcome of one specialist-stage dispatch attempt.

    Attributes:
        outcome: One of :class:`StageDispatchOutcome` —
            :attr:`~StageDispatchOutcome.COMPLETED`,
            :attr:`~StageDispatchOutcome.DEGRADED`,
            :attr:`~StageDispatchOutcome.SOFT_TIMEOUT`, or
            :attr:`~StageDispatchOutcome.ERROR`.
        stage: The stage class that was dispatched.
        build_id: Identifier of the originating build.
        correlation_id: Build-level correlation id threaded onto the
            dispatch envelope (Group I @data-integrity).
        stage_log_entry_id: Identifier of the ``stage_log`` row written
            on submit and updated on reply. Lets the gating layer cross-
            reference back without re-querying the writer.
        coach_score: Parsed Coach score in ``[0.0, 1.0]`` for
            ``COMPLETED``; ``None`` for every other outcome.
        criterion_breakdown: Per-criterion Coach scores. The DEPLOYED
            specialist emits these as a **list** of
            ``{criterion, score, weight, rationale}`` records; a mapping is
            also tolerated for older reply shapes. Empty for
            non-``COMPLETED`` outcomes. This is Coach *evidence*, not the
            product document (see ``role_output``).
        detection_findings: Coach detection findings folded into the
            reply. Empty tuple for non-``COMPLETED`` outcomes.
        role_output: The role's actual product document — the real PO /
            architect output extracted from the deployed
            ``wrap_role_output`` reply (M10). This is the content the
            planning driver threads to the product-docs checkpoint and the
            PLANNED_HANDOFF; ``coach_score`` / ``criterion_breakdown`` are
            evidence *about* it. Empty for non-``COMPLETED`` outcomes.
        reason: Human-readable reason for ``DEGRADED``, ``SOFT_TIMEOUT``,
            and ``ERROR`` outcomes; ``None`` for ``COMPLETED``.

    The four ``StageDispatchOutcome`` enum members are also re-exported
    as class-level attributes so callers can write
    :attr:`StageDispatchResult.DEGRADED` instead of importing the enum
    separately. This matches the AC-007 wording: "returns a
    ``StageDispatchResult.DEGRADED`` outcome".
    """

    outcome: StageDispatchOutcome
    stage: StageClass
    build_id: str
    correlation_id: str
    stage_log_entry_id: str
    coach_score: float | None = None
    criterion_breakdown: Mapping[str, Any] | Sequence[Any] = field(
        default_factory=dict
    )
    detection_findings: Sequence[Any] = field(default_factory=tuple)
    role_output: Mapping[str, Any] | Sequence[Any] = field(default_factory=dict)
    reason: str | None = None

    # Re-export enum members so AC-007's ``StageDispatchResult.DEGRADED``
    # access pattern works without forcing every caller to import
    # ``StageDispatchOutcome``. ``ClassVar`` keeps the dataclass machinery
    # from treating these as instance fields.
    COMPLETED: ClassVar[StageDispatchOutcome] = StageDispatchOutcome.COMPLETED
    DEGRADED: ClassVar[StageDispatchOutcome] = StageDispatchOutcome.DEGRADED
    SOFT_TIMEOUT: ClassVar[StageDispatchOutcome] = StageDispatchOutcome.SOFT_TIMEOUT
    ERROR: ClassVar[StageDispatchOutcome] = StageDispatchOutcome.ERROR


@runtime_checkable
class SpecialistDispatchSurface(Protocol):
    """Structural Protocol over FEAT-FORGE-003's specialist dispatch.

    Production wiring binds
    :class:`forge.dispatch.orchestrator.DispatchOrchestrator`; tests
    inject in-memory recording fakes. The dispatcher only calls
    :meth:`dispatch` — capability resolution, persistence, correlation,
    and reply parsing are all owned by the surface implementation.

    The Protocol is intentionally a *subset* of
    :meth:`DispatchOrchestrator.dispatch` so swapping the production
    orchestrator in is a no-op assignment, not a wrapper class.
    """

    async def dispatch(  # pragma: no cover - protocol stub
        self,
        *,
        capability: str,
        parameters: list[DispatchParameter],
        attempt_no: int = 1,
        retry_of: str | None = None,
        intent_pattern: str | None = None,
        build_id: str = "unknown",
        stage_label: str = "unknown",
        command: str,
        command_args: dict[str, Any] | None = None,
    ) -> DispatchOutcome:
        """Dispatch one capability call and return the outcome.

        ``command`` / ``command_args`` are the deployed verb + argument dict
        the transport stamps onto the wire ``CommandPayload`` (M2 + M3).
        """
        ...


@runtime_checkable
class StageLogWriter(Protocol):
    """Narrow write surface over the FEAT-FORGE-001 ``stage_log`` table.

    Two methods only — :meth:`record_dispatch_submit` writes the row
    when the dispatcher hands the request off to FEAT-FORGE-003, and
    :meth:`record_dispatch_reply` updates the same row when the reply
    arrives (or the local timeout fires). Splitting the lifecycle into
    two calls is what AC-006 demands: "Records the dispatch as a
    ``stage_log`` entry on submit and updates on reply with the parsed
    Coach score and detection findings".

    Production wires the FEAT-FORGE-001 SQLite adapter; tests use an
    in-memory fake.
    """

    def record_dispatch_submit(  # pragma: no cover - protocol stub
        self,
        *,
        build_id: str,
        stage: StageClass,
        feature_id: str | None,
        correlation_id: str,
        capability: str,
    ) -> str:
        """Record a stage_log row at submit time and return its id.

        Args:
            build_id: Build identifier.
            stage: Stage class being dispatched.
            feature_id: Per-feature scope (always ``None`` for the
                two specialist stages in Mode A — they run once per
                build per FEAT-FORGE-007 ASSUM-001).
            correlation_id: Build-level correlation id threaded onto
                the dispatch envelope.
            capability: Capability name handed to FEAT-FORGE-003.

        Returns:
            Opaque identifier of the new ``stage_log`` row. Threaded
            back through the :class:`StageDispatchResult` so the gating
            layer can cross-reference without a second query.
        """
        ...

    def record_dispatch_reply(  # pragma: no cover - protocol stub
        self,
        *,
        entry_id: str,
        outcome: StageDispatchOutcome,
        coach_score: float | None,
        criterion_breakdown: Mapping[str, Any] | Sequence[Any],
        detection_findings: Sequence[Any],
        reason: str | None,
    ) -> None:
        """Update the submitted ``stage_log`` row with the parsed reply.

        Args:
            entry_id: Identifier returned by
                :meth:`record_dispatch_submit`.
            outcome: Translated dispatcher outcome.
            coach_score: Parsed Coach score for COMPLETED replies,
                ``None`` otherwise.
            criterion_breakdown: Per-criterion Coach scores; empty for
                non-COMPLETED replies.
            detection_findings: Coach detection findings folded into the
                reply; empty for non-COMPLETED replies.
            reason: Human-readable reason for non-COMPLETED outcomes.
        """
        ...


async def dispatch_specialist_stage(
    *,
    stage: StageClass,
    build_id: str,
    correlation_id: str,
    forward_context_builder: ForwardContextBuilder,
    dispatch_surface: SpecialistDispatchSurface,
    stage_log_writer: StageLogWriter,
    feature_id: str | None = None,
    attempt_no: int = 1,
    retry_of: str | None = None,
    request_text: str | None = None,
    extra_command_args: Mapping[str, Any] | None = None,
) -> StageDispatchResult:
    """Dispatch one specialist stage (PRODUCT_OWNER or ARCHITECT).

    Sequence:

    1. Refuse early for any stage class outside
       :data:`SPECIALIST_STAGES` — programming error, raise
       :class:`ValueError`. This is AC-002.
    2. Build the forward-propagated context entries via
       :meth:`ForwardContextBuilder.build_for` (AC-003).
    3. Compose the :class:`DispatchParameter` list — the
       ``correlation_id`` parameter (Group I @data-integrity) plus one
       parameter per :class:`ContextEntry`. Both are non-sensitive.
    4. Record the submit-side ``stage_log`` row (AC-006 first half).
    5. Delegate to :meth:`SpecialistDispatchSurface.dispatch` with the
       capability matching the stage (AC-004).
    6. Translate the :data:`DispatchOutcome` into a
       :class:`StageDispatchResult` and update the ``stage_log`` row
       with the parsed Coach payload (AC-006 second half, AC-007).

    Args:
        stage: One of :attr:`StageClass.PRODUCT_OWNER` or
            :attr:`StageClass.ARCHITECT`. Any other value raises
            :class:`ValueError` (AC-002).
        build_id: Identifier of the originating build.
        correlation_id: Build-level correlation id (Group I
            @data-integrity). Threaded onto the dispatch envelope as a
            non-sensitive :class:`DispatchParameter` so FEAT-FORGE-003
            can stamp it onto the wire-side headers and the persisted
            resolution row.
        forward_context_builder: TASK-MAG7-006 builder that materialises
            ``--context`` entries from the prior stage's approved
            artefact (AC-003).
        dispatch_surface: FEAT-FORGE-003 specialist-dispatch surface.
            Production binds :class:`DispatchOrchestrator`; tests
            inject a recording fake.
        stage_log_writer: Writer over the FEAT-FORGE-001 ``stage_log``
            table (AC-006).
        feature_id: Per-feature scope. Always ``None`` for the two
            Mode A specialist stages (FEAT-FORGE-007 ASSUM-001 says
            they run once per build), but accepted as a parameter so
            future scopes can be threaded through without changing the
            signature.
        attempt_no: Monotonic attempt counter (>= 1).
        retry_of: ``resolution_id`` of the prior dispatch attempt this
            one is retrying. Forwarded to the dispatch surface so the
            persistence layer records the retry chain.
        request_text: The raw planning request text — the source of the
            product-owner greenfield ``problem_statement`` (D1, M3). Threaded
            through :func:`build_specialist_command`; the live Mode-P planning
            composition reads it from the ``planning_runs`` row. ``None`` for
            callers with no raw request (a warning is logged if a required
            deployed argument is left unsourced).
        extra_command_args: Lane B (B2) forge-supplied wire args for the
            target-terminal stages (:attr:`StageClass.FEATURE_SPEC` /
            :attr:`StageClass.FEATURE_PLAN`) — ``spec_input`` for 007 and
            ``scope`` / ``target_repo`` / ``feature_id`` for 008. Merged into
            ``command_args`` via :func:`build_specialist_command`;
            request-sourced values stay authoritative. ``None`` for the
            PRODUCT_OWNER / ARCHITECT stages (byte-unchanged behaviour).

    Returns:
        A :class:`StageDispatchResult`. The supervisor consumes the
        ``outcome`` field and, on
        :attr:`StageDispatchOutcome.SOFT_TIMEOUT`, decides whether to
        retry with the same context (FEAT-FORGE-003 ASSUM-005 —
        reasoning-model-driven, no fixed cap). The dispatcher itself
        does not retry.

    Raises:
        ValueError: If ``stage`` is not in :data:`SPECIALIST_STAGES`.
            This is the only programming-error branch; every runtime
            failure mode is surfaced as a structured outcome instead.
    """
    if stage not in SPECIALIST_CAPABILITY_BY_STAGE:
        # AC-002 — programming error, not runtime. The supervisor
        # never legitimately calls this for a non-specialist stage;
        # if it does, that is a bug we want to fail loud on.
        raise ValueError(
            "dispatch_specialist_stage refuses stage="
            f"{stage!r}; expected one of "
            f"{sorted(s.value for s in SPECIALIST_STAGES)}",
        )

    capability = SPECIALIST_CAPABILITY_BY_STAGE[stage]
    # Intent-pattern fallback (TASK-FWD-PLAN-PODISCO). The exact tool name
    # above stays the first-choice; this intent is what the resolver falls
    # back to when no agent advertises that tool — which is the live reality
    # for both specialist stages. ``None`` for a stage without a mapped
    # intent leaves resolution on the exact-tool path only.
    intent_pattern = SPECIALIST_INTENT_BY_STAGE.get(stage)

    # Step 2 — build forward-propagated context. The builder owns the
    # approved-only and worktree-allowlist filtering (TASK-MAG7-006);
    # this dispatcher trusts the returned list as-is.
    context_entries = forward_context_builder.build_for(
        stage=stage,
        build_id=build_id,
        feature_id=feature_id,
    )

    # Step 3 — flatten into DispatchParameter records. Correlation id
    # comes first so the wire-side header composer (TASK-SAD-010) sees
    # it before any context payload during parameter iteration. These are
    # the PERSISTENCE/AUDIT records (scrubbed + written by persist_resolution);
    # the deployed-handler wire args are built separately below (D3).
    parameters = _build_dispatch_parameters(
        correlation_id=correlation_id,
        context_entries=context_entries,
    )

    # Step 3b — resolve the deployed verb + argument dict (M2 + M3, D1/D3).
    # ``command`` names a real handler in the target agent's command_map and
    # ``command_args`` carries exactly the keys that handler reads.
    command, command_args = build_specialist_command(
        stage,
        request_text=request_text,
        context_entries=context_entries,
        extra_command_args=extra_command_args,
    )
    missing_args = [
        name
        for name in SPECIALIST_REQUIRED_ARGS_BY_STAGE.get(stage, ())
        if not str(command_args.get(name, "")).strip()
    ]
    if missing_args:
        # Fail-loud observability: the deployed router's _check_required_args
        # gate will reject this command. On the live planning path request_text
        # is always populated, so this only fires for degenerate inputs.
        logger.warning(
            "dispatch_specialist_stage: stage=%s command=%s missing required "
            "deployed arg(s) %s — the specialist router will reject this "
            "command (build_id=%s correlation_id=%s)",
            stage.value,
            command,
            sorted(missing_args),
            build_id,
            correlation_id,
        )

    # Step 4 — submit-side stage_log row (AC-006 first half).
    entry_id = stage_log_writer.record_dispatch_submit(
        build_id=build_id,
        stage=stage,
        feature_id=feature_id,
        correlation_id=correlation_id,
        capability=capability,
    )
    logger.info(
        "dispatch_specialist_stage.submit build_id=%s stage=%s "
        "capability=%s correlation_id=%s entry_id=%s context_entries=%d",
        build_id,
        stage.value,
        capability,
        correlation_id,
        entry_id,
        len(context_entries),
    )

    # Step 5 — delegate to FEAT-FORGE-003. Any exception raised by
    # the surface (other than the structured DispatchOutcome variants)
    # propagates unchanged: the supervisor's existing exception
    # handling is the right level for "transport blew up entirely",
    # and silently swallowing here would corrupt the FEAT-FORGE-002
    # callback contract.
    outcome = await dispatch_surface.dispatch(
        capability=capability,
        parameters=parameters,
        attempt_no=attempt_no,
        retry_of=retry_of,
        intent_pattern=intent_pattern,
        build_id=build_id,
        stage_label=stage.value,
        command=command,
        command_args=command_args,
    )

    # Step 6 — translate + record reply (AC-006 second half, AC-007).
    result = _translate_outcome(
        outcome=outcome,
        stage=stage,
        build_id=build_id,
        correlation_id=correlation_id,
        entry_id=entry_id,
    )
    stage_log_writer.record_dispatch_reply(
        entry_id=entry_id,
        outcome=result.outcome,
        coach_score=result.coach_score,
        criterion_breakdown=result.criterion_breakdown,
        detection_findings=result.detection_findings,
        reason=result.reason,
    )
    logger.info(
        "dispatch_specialist_stage.reply build_id=%s stage=%s "
        "outcome=%s coach_score=%s entry_id=%s",
        build_id,
        stage.value,
        result.outcome.value,
        result.coach_score,
        entry_id,
    )
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_dispatch_parameters(
    *,
    correlation_id: str,
    context_entries: list[ContextEntry],
) -> list[DispatchParameter]:
    """Compose the :class:`DispatchParameter` list for one dispatch.

    The first parameter carries the build's ``correlation_id`` (Group I
    @data-integrity); subsequent parameters carry one entry per
    :class:`ContextEntry` returned by the forward-context builder.

    Both parameter classes are explicitly non-sensitive — the
    correlation id is a public routing key and the forward-context
    payloads have already been allowlist-filtered by the builder. The
    ``sensitive=False`` flag is stamped explicitly rather than by
    default so a future refactor that flips defaults cannot silently
    redact the correlation id at the persistence boundary.
    """
    parameters: list[DispatchParameter] = [
        DispatchParameter(
            name=_CORRELATION_PARAMETER,
            value=correlation_id,
            sensitive=False,
        )
    ]
    for entry in context_entries:
        # Encode flag + kind + value in the parameter value so the
        # specialist tool on the receiving side can distinguish a path
        # payload from an inline-text payload without a second
        # parameter slot per entry. The ``flag`` field is preserved so
        # the receiver can rebuild ``--<flag> <value>`` argv pairs
        # exactly as TASK-MAG7-006 documented.
        parameters.append(
            DispatchParameter(
                name=_CONTEXT_PARAMETER,
                value=f"{entry.flag}={entry.kind}={entry.value}",
                sensitive=False,
            )
        )
    return parameters


def _translate_outcome(
    *,
    outcome: DispatchOutcome,
    stage: StageClass,
    build_id: str,
    correlation_id: str,
    entry_id: str,
) -> StageDispatchResult:
    """Translate a FEAT-FORGE-003 outcome into a :class:`StageDispatchResult`.

    Branching is on the discriminated union's concrete class rather
    than the ``kind`` literal so a future ``DispatchOutcome`` member
    (added without updating this function) raises a clean
    :class:`TypeError` at the catch-all path instead of silently being
    treated as ``ERROR``.
    """
    if isinstance(outcome, SyncResult):
        # Group A success path. Coach score may still be ``None`` if
        # the specialist intentionally declined to score (e.g. a
        # degraded-mode reply). The gating layer handles ``None``
        # downstream — we copy through verbatim rather than synthesise.
        # criterion_breakdown may be the deployed LIST shape or a legacy
        # mapping (M7). Copy-through preserving the concrete container so a
        # ``dict(list_of_records)`` never explodes.
        breakdown = outcome.criterion_breakdown
        breakdown_copy: Mapping[str, Any] | Sequence[Any] = (
            list(breakdown) if isinstance(breakdown, list) else dict(breakdown)
        )
        # role_output is the REAL product document (M10) — copy-through as a
        # dict or list depending on the deployed role's serialisation.
        role_output = outcome.role_output
        role_output_copy: Mapping[str, Any] | Sequence[Any] = (
            list(role_output)
            if isinstance(role_output, list)
            else dict(role_output)
        )
        return StageDispatchResult(
            outcome=StageDispatchOutcome.COMPLETED,
            stage=stage,
            build_id=build_id,
            correlation_id=correlation_id,
            stage_log_entry_id=entry_id,
            coach_score=outcome.coach_score,
            criterion_breakdown=breakdown_copy,
            detection_findings=tuple(outcome.detection_findings),
            role_output=role_output_copy,
            reason=None,
        )

    if isinstance(outcome, Degraded):
        # AC-007 — Group C @negative "no product-owner specialist".
        # Gating layer maps DEGRADED to FLAG_FOR_REVIEW so a human
        # decides what to do; we surface the orchestrator's reason
        # field unchanged so the audit trail records *why* it
        # degraded (snapshot stale vs no specialist resolvable etc.).
        return StageDispatchResult(
            outcome=StageDispatchOutcome.DEGRADED,
            stage=stage,
            build_id=build_id,
            correlation_id=correlation_id,
            stage_log_entry_id=entry_id,
            reason=outcome.reason,
        )

    if isinstance(outcome, DispatchError):
        # The dispatch orchestrator emits ``error_explanation="local_timeout"``
        # for the soft-timeout branch (FEAT-FORGE-003 ASSUM-005). Any
        # other explanation is surfaced as a generic ERROR so the
        # supervisor's reasoning loop can decide between retrying and
        # failing the build.
        if outcome.error_explanation == "local_timeout":
            translated = StageDispatchOutcome.SOFT_TIMEOUT
        else:
            translated = StageDispatchOutcome.ERROR
        return StageDispatchResult(
            outcome=translated,
            stage=stage,
            build_id=build_id,
            correlation_id=correlation_id,
            stage_log_entry_id=entry_id,
            reason=outcome.error_explanation,
        )

    if isinstance(outcome, AsyncPending):
        # Specialist dispatch is synchronous in Mode A
        # (FEAT-FORGE-007 ASSUM-002). An ``async_pending`` reply here
        # means the specialist tool has misclassified itself; we
        # surface it as ERROR so the supervisor fails loud rather
        # than blocking on a never-arriving reply.
        return StageDispatchResult(
            outcome=StageDispatchOutcome.ERROR,
            stage=stage,
            build_id=build_id,
            correlation_id=correlation_id,
            stage_log_entry_id=entry_id,
            reason=(
                "specialist returned async_pending which is unsupported "
                f"for Mode A specialist stages (run_identifier={outcome.run_identifier!r})"
            ),
        )

    # Defensive catch-all. The discriminated union is closed today, so
    # this branch is only reachable if the dispatch domain adds a new
    # member without updating this translator. Surface the type name
    # so the next developer sees exactly what was missed.
    raise TypeError(
        f"_translate_outcome: unsupported DispatchOutcome variant "
        f"{type(outcome).__name__!r}",
    )

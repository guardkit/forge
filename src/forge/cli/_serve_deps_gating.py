"""TASK-JNB-101: production approval-gate composition for forge-serve.

This module is the composition root for the confidence-gated checkpoint
protocol's approval side (FEAT-FORGE-004 / FEAT-1872 v1.1). It gives the
CGCP stack — built and integration-tested since TASK-CGCP-006/007/008/010
but never constructed in production — its first real wiring:

* :class:`ApprovalPublisher` + :class:`ApprovalSubscriber` +
  :class:`SyntheticResponseInjector` constructed against the daemon's
  shared NATS client inside the ``_compose(client)`` closure
  (``forge.cli.serve.bind_production_dispatch_chain``), following the
  :class:`~forge.cli._serve_production.LifecycleBridgeWireupParts`
  idiom for "needs the client that does not exist at bind time".
* ``expected_approver`` threaded from forge config
  (``ApprovalConfig.expected_approver``, pinned default ``"rich"``) into
  :class:`ApprovalSubscriberDeps` — the APPROVER_IDENTITY contract with
  jarvis's ``JARVIS_SLACK_DECIDED_BY`` (verbatim string equality; a
  mismatch silently refuses every phone approval).
* :class:`_BoundContextSubscriber` — the per-build adapter that threads
  the daemon's :class:`PipelineLifecycleEmitter`, the build's
  :class:`~forge.pipeline.BuildContext`, and the expected
  ``correlation_id`` into every
  :meth:`ApprovalSubscriber.await_response` call. This activates, for
  the first time in production, three dormant-but-designed behaviours:

  1. the FW10-010 resume emit (``pipeline.build-resumed.<feature_id>``
     published with the real ``decision``/``decided_by`` **before** the
     wait loop returns — i.e. before the PAUSED → RUNNING transition);
  2. the correlation-id validation step (2b) of the four-step chain
     (payload validation → ``decided_by`` allowlist → ``correlation_id``
     match → ``request_id`` 300s dedup) — without a bound context the
     chain is only three steps;
  3. the TASK-FRR-PEB-006 bridge-canonicalisation probe
     (``bridge_registry_lookup``), so the subscriber defers its emit
     when the lifecycle bridge owns the build's resume envelope.

AC-3 note (recorded deviation): the JNB-101 task text named
``autobuild_runner.mark_resume_pending`` as the resume-emit mechanism. The
pre-implementation architectural review proved that mechanism broken for its
own cited scenario, and TASK-GATE-D659 §D5 has since **removed** it entirely —
``LifecycleEmitterAdapter`` is never constructed in production (the sidecar
runs in a separate process with no forge.db / NATS), so the resume special-case
was dead-and-broken. The resume-emit is owned **solely** by this subscriber
seam (FW10-010): ``build-resumed`` is emitted on approve/override decision
dispatch, exactly once, with the real decision/responder values. See
``docs/state/TASK-JNB-101/implementation_plan.md`` and TASK-GATE-D659 §D5.

DDR-007: every emit failure inside the subscriber's resume path is
WARNING + continue; SQLite stays authoritative. DDR-027: dedup and
pending state are in-memory by design. The subscriber binds AGENTS-
stream subjects via core-NATS ``subscribe`` — no JetStream consumer is
created anywhere in this module, so the PIPELINE workqueue
single-consumer rule (err 10100) is structurally unaffected.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from forge.adapters.nats.approval_publisher import ApprovalPublisher
from forge.adapters.nats.approval_subscriber import (
    ApprovalSubscriber,
    ApprovalSubscriberDeps,
)
from forge.adapters.nats.synthetic_response_injector import (
    SyntheticResponseInjector,
)
from forge.config.models import DEFAULT_AUTOBUILD_GATE_APPROVAL_MAX_WAIT_SECONDS
from forge.gating.identity import derive_request_id

# The canonical approval-request envelope builder lives (privately) in
# the gating wrapper module — TASK-CGCP-006 AC-008 makes it the single
# source of truth for the eleven-key ``details`` shape. Importing the
# private helper here is deliberate reuse, not a new public surface.
from forge.gating.wrappers import GateCheckDeps, _build_request_envelope

if TYPE_CHECKING:  # pragma: no cover - import-time only
    from collections.abc import Callable
    from datetime import datetime

    from nats_core.events import ApprovalResponsePayload

    from forge.config.models import ApprovalConfig, ForgeConfig
    from forge.gating.wrappers import (
        AdjustmentsReader,
        GateRepository,
        PriorsReader,
        RulesReader,
        StateMachine,
    )
    from forge.persistence.repositories.bridge_registry import BridgeRegistry
    from forge.pipeline import BuildContext, PipelineLifecycleEmitter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parts container (LifecycleBridgeWireupParts idiom)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ApprovalGateParts:
    """NATS-bound approval-gate collaborators for one daemon boot.

    Constructed once per ``_compose(client)`` run by
    :func:`build_approval_gate_parts` and anchored module-level via
    :func:`bind_gate_parts` so the daemon owns a live, injectable
    approval seam (mirroring ``_serve_production._bound_resources``).

    Note on subscription lifetime: :class:`ApprovalSubscriber`
    subscribes per :meth:`await_response` call, not at construction —
    a daemon reconnect that opens a fresh NATS client therefore only
    affects waits that were in flight when the old client dropped.

    Args:
        publisher: Outbound approval-request publisher (TASK-CGCP-006).
        subscriber: The raw response subscriber (TASK-CGCP-007). Inject
            via :func:`make_gate_check_deps`, which binds the per-build
            resume-publish context.
        injector: Synthetic CLI cancel/skip responder (TASK-CGCP-008).
        approval_config: The loaded ``ApprovalConfig`` slice.
        expected_approver: The configured ``decided_by`` allowlist value
            actually threaded into the subscriber deps (config-alignment
            probe for TASK-JNB-107 — logged at compose time).
        emitter: The daemon's :class:`PipelineLifecycleEmitter`, used by
            :func:`make_gate_check_deps` to bind the FW10-010 resume
            emit. ``None`` leaves the resume emit dormant (unit tiers).
        priors_reader: The boot-scoped :class:`PriorsReader` every gate
            activation path reads through
            (:mod:`forge.cli._serve_gate_activation`). REQUIRED with no
            default — the no-silent-fallback seam, deliberate: the
            composition root must decide explicitly between the
            fleet-memory reader and the degraded
            :class:`~forge.gating.degraded.EmptyPriorsReader`; omitting
            it is a ``TypeError``, never a quiet empty read.
        gate_approval_max_wait_seconds: The build gate's total approval
            wait, from forge.yaml ``autobuild_gate.approval_max_wait_seconds``
            (2026-08-26). ``0`` (the default) = wait indefinitely for the
            human answer; positive = cancel the build after that many
            seconds without one. Carried on the parts so the boot-time
            rearm path (``_serve_gate_activation.rearm_paused_gates``)
            builds its per-build subscribers with the SAME wait the live
            gate uses.
    """

    publisher: ApprovalPublisher
    subscriber: ApprovalSubscriber
    injector: SyntheticResponseInjector
    approval_config: "ApprovalConfig"
    expected_approver: str | None
    emitter: "PipelineLifecycleEmitter | None"
    priors_reader: "PriorsReader"
    gate_approval_max_wait_seconds: int = (
        DEFAULT_AUTOBUILD_GATE_APPROVAL_MAX_WAIT_SECONDS
    )


_bound_gate_parts: ApprovalGateParts | None = None


def bind_gate_parts(parts: ApprovalGateParts) -> ApprovalGateParts:
    """Anchor ``parts`` module-level for the daemon's lifetime.

    Re-binding (a fresh ``_compose`` run after reconnect) simply
    replaces the reference — the previous parts hold no resources that
    need closing (see subscription-lifetime note on
    :class:`ApprovalGateParts`).
    """
    global _bound_gate_parts
    _bound_gate_parts = parts
    return parts


def bound_gate_parts() -> ApprovalGateParts | None:
    """Return the currently bound parts (``None`` before first compose)."""
    return _bound_gate_parts


def _reset_for_tests() -> None:
    """Reset module-level binding state. Test-only helper."""
    global _bound_gate_parts
    _bound_gate_parts = None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_approval_gate_parts(
    client: Any,
    forge_config: "ForgeConfig",
    *,
    priors_reader: "PriorsReader",
    emitter: "PipelineLifecycleEmitter | None" = None,
    bridge_registry: "BridgeRegistry | None" = None,
    repository: "GateRepository | None" = None,
    project: str | None = None,
    subscriber_clock: Any = None,
    dedup_ttl_seconds: int | None = None,
) -> ApprovalGateParts:
    """Construct the production approval-gate collaborators.

    Args:
        client: The daemon's shared async NATS client (opened in
            ``_run_serve``; ASSUM-011 single-client).
        forge_config: Validated :class:`ForgeConfig`; supplies the
            ``approval`` slice including ``expected_approver``.
        priors_reader: The boot-scoped :class:`PriorsReader` threaded
            onto the parts for every gate activation path. Production
            composes it via
            :func:`forge.adapters.fleet_memory.build_priors_reader_from_env`
            (env-gated; ``EmptyPriorsReader`` when memory is OFF); unit
            tiers pin ``EmptyPriorsReader()`` or a sentinel double.
        emitter: The compose closure's :class:`PipelineLifecycleEmitter`
            (same publisher instance as the bridge wireup). Threaded to
            :func:`make_gate_check_deps` for the resume emit.
        bridge_registry: The lifecycle bridge's :class:`BridgeRegistry`
            (exposed on ``LifecycleBridgeWireupParts.registry``). When
            provided, the subscriber's TASK-FRR-PEB-006 canonicalisation
            probe is wired to it; ``None`` preserves subscriber-emits
            behaviour.
        repository: Optional :class:`GateRepository`. When provided, the
            subscriber's refresh-on-timeout loop (API §7) is enabled:
            the refreshed ``request_id`` is re-derived, persisted (so
            boot recovery re-emits the *current* id), and re-published.
            ``None`` disables refresh: waits are single-window, matching
            the pre-existing integration-test behaviour. **Production
            wires the real D659 SQLite adapter here (WS3-S6, 2026-07-09):
            ``serve.py`` threads the ``build_sqlite_gate_adapters``
            ``gate_repository`` in so long pauses wait the full
            ``max_wait_seconds`` instead of expiring after one window.**
        project: Optional NATS multi-tenancy scope; ``None`` matches the
            rest of the serve composition.
        subscriber_clock: Optional monotonic :class:`Clock` override for
            the subscriber's dedup/refresh timing. Production omits it
            (wall monotonic); deterministic tests inject a fake.
        dedup_ttl_seconds: Optional dedup-TTL override; production omits
            it (300s per TASK-CGCP-007 AC-003).

    Returns:
        A frozen :class:`ApprovalGateParts`.
    """
    approval_config = forge_config.approval
    # The build gate's own total-wait knob (2026-08-26). 0 = wait
    # indefinitely for the human answer (the default); positive restores a
    # hard ceiling. ``getattr`` guards config doubles that predate the field.
    autobuild_gate_config = getattr(forge_config, "autobuild_gate", None)
    gate_max_wait = (
        autobuild_gate_config.approval_max_wait_seconds
        if autobuild_gate_config is not None
        else DEFAULT_AUTOBUILD_GATE_APPROVAL_MAX_WAIT_SECONDS
    )
    # ALWAYS thread expected_approver explicitly from config — never
    # rely on the ApprovalSubscriberDeps dataclass default (None =
    # permissive), so config and wired behaviour cannot silently
    # diverge (arch-review R3).
    expected_approver = approval_config.expected_approver

    publisher = ApprovalPublisher(nats_client=client, project=project)

    publish_refresh = None
    if repository is not None:
        publish_refresh = _make_publish_refresh(
            repository=repository, publisher=publisher
        )

    bridge_registry_lookup = None
    if bridge_registry is not None:
        bridge_registry_lookup = _make_bridge_registry_lookup(bridge_registry)

    # TASK-JNB-109: the subscriber is written against the envelope-aware
    # nats_core subscribe contract, but the daemon's shared client is the
    # RAW nats.aio client (callback would bind to the ``queue`` parameter
    # and the handler would receive a raw Msg, not a MessageEnvelope) —
    # the reply path could never receive a phone approval live. The
    # adapter is the single conversion point; the publisher/injector keep
    # the raw client (core publish is signature-compatible).
    from forge.adapters.nats.envelope_subscribe import EnvelopeSubscribeClient

    deps_kwargs: dict[str, Any] = {
        "nats_client": EnvelopeSubscribeClient(client),
        "config": approval_config,
        "publish_refresh": publish_refresh,
        "expected_approver": expected_approver,
        "project": project,
        "bridge_registry_lookup": bridge_registry_lookup,
        "max_total_wait_seconds": gate_max_wait,
    }
    if subscriber_clock is not None:
        deps_kwargs["clock"] = subscriber_clock
    if dedup_ttl_seconds is not None:
        deps_kwargs["dedup_ttl_seconds"] = dedup_ttl_seconds
    subscriber = ApprovalSubscriber(ApprovalSubscriberDeps(**deps_kwargs))
    injector = SyntheticResponseInjector(nats_client=client)

    logger.info(
        "forge-serve: approval gate parts composed "
        "(expected_approver=%r refresh=%s bridge_lookup=%s "
        "default_wait=%ds max_wait=%ds gate_total_wait=%s)",
        expected_approver,
        "enabled" if publish_refresh is not None else "disabled",
        "wired" if bridge_registry_lookup is not None else "absent",
        approval_config.default_wait_seconds,
        approval_config.max_wait_seconds,
        "indefinite" if gate_max_wait <= 0 else f"{gate_max_wait}s",
    )

    return ApprovalGateParts(
        publisher=publisher,
        subscriber=subscriber,
        injector=injector,
        approval_config=approval_config,
        expected_approver=expected_approver,
        emitter=emitter,
        priors_reader=priors_reader,
        gate_approval_max_wait_seconds=gate_max_wait,
    )


def _make_publish_refresh(
    *,
    repository: "GateRepository",
    publisher: ApprovalPublisher,
) -> "Callable[[str, str, int], Any]":
    """Bind the API §7 refresh-on-timeout callback.

    The refreshed row is persisted BEFORE the publish so a crash between
    the two is recovered by boot re-emission of the *new* ``request_id``
    (mirrors the ordering of the defer branch in
    ``wrappers._dispatch_response``). Exceptions propagate to the
    subscriber's refresh loop, which logs at WARNING and keeps waiting.
    """

    async def _publish_refresh(
        build_id: str, stage_label: str, attempt_count: int
    ) -> None:
        snapshots = await repository.list_paused_builds()
        # NEWEST matching row: append-shaped repositories keep superseded
        # attempts (and prior-stage pauses of the same build), and only
        # the most recent row carries the current stage's decision
        # snapshot (review finding, 2026-07-05).
        snap = next((s for s in reversed(snapshots) if s.build_id == build_id), None)
        if snap is None:
            logger.warning(
                "approval refresh: no paused-build row for build_id=%s "
                "— skipping refresh publish (stage=%s attempt=%d)",
                build_id,
                stage_label,
                attempt_count,
            )
            return
        new_request_id = derive_request_id(
            build_id=build_id,
            stage_label=stage_label,
            attempt_count=attempt_count,
        )
        await repository.record_paused_build(
            build_id=build_id,
            feature_id=snap.feature_id,
            stage_label=stage_label,
            request_id=new_request_id,
            attempt_count=attempt_count,
            decision=snap.decision_snapshot,
        )
        envelope = _build_request_envelope(
            decision=snap.decision_snapshot,
            feature_id=snap.feature_id,
            request_id=new_request_id,
            artefact_paths=snap.artefact_paths,
            correlation_id=snap.correlation_id,
        )
        await publisher.publish_request(envelope)

    return _publish_refresh


def _make_bridge_registry_lookup(
    bridge_registry: "BridgeRegistry",
) -> "Callable[[str, str], bool]":
    """Wrap :meth:`BridgeRegistry.get` as the PEB-006 truthiness probe.

    The probe answers "does the bridge have an active registry entry
    for THIS build?" — ``BridgeRegistry.get`` keys on ``feature_id``
    alone, so the entry's ``correlation_id`` must also match the
    caller's or the row belongs to a different build of the same
    feature (e.g. a stale earlier attach) and MUST NOT suppress the
    subscriber's resume emit (review finding, 2026-07-05).

    ``BridgeRegistry.get`` raises ``ValueError`` on empty inputs; the
    subscriber's ``_bridge_owns_resume_for`` treats a raising lookup as
    bridge-absent (defence-in-depth fallback to its own emit), so no
    extra guarding is needed here beyond the entry mapping.
    """

    def _lookup(feature_id: str, correlation_id: str) -> bool:
        entry = bridge_registry.get(feature_id, correlation_id=correlation_id)
        return entry is not None and entry.correlation_id == correlation_id

    return _lookup


# ---------------------------------------------------------------------------
# Per-build bound-context subscriber (ApprovalSubscriberProto adapter)
# ---------------------------------------------------------------------------


class _BoundContextSubscriber:
    """Bind one build's resume-publish context into ``await_response``.

    Satisfies ``forge.gating.wrappers.ApprovalSubscriberProto`` — the
    gate wrapper's call sites pass only ``(build_id, stage_label,
    attempt_count, timeout_seconds)``; this adapter supplies the three
    optional kwargs that activate the FW10-010 resume emit, the
    correlation-id validation step, and the bridge-canonicalisation
    probe (see module docstring). One instance per build — the bound
    :class:`BuildContext` and ``expected_correlation_id`` are
    build-specific.
    """

    def __init__(
        self,
        inner: ApprovalSubscriber,
        *,
        lifecycle_emitter: "PipelineLifecycleEmitter",
        build_context: "BuildContext",
        expected_correlation_id: str | None,
    ) -> None:
        self._inner = inner
        self._emitter = lifecycle_emitter
        self._ctx = build_context
        self._expected_correlation_id = expected_correlation_id

    async def await_response(
        self,
        build_id: str,
        *,
        stage_label: str,
        attempt_count: int = 0,
        timeout_seconds: int | None = None,
    ) -> "ApprovalResponsePayload | None":
        return await self._inner.await_response(
            build_id,
            stage_label=stage_label,
            attempt_count=attempt_count,
            timeout_seconds=timeout_seconds,
            lifecycle_emitter=self._emitter,
            build_context=self._ctx,
            expected_correlation_id=self._expected_correlation_id,
        )


# ---------------------------------------------------------------------------
# GateCheckDeps assembly — the AC-1 typed injection seam
# ---------------------------------------------------------------------------


def make_gate_check_deps(
    parts: ApprovalGateParts,
    *,
    priors_reader: "PriorsReader",
    adjustments_reader: "AdjustmentsReader",
    rules_reader: "RulesReader",
    repository: "GateRepository",
    state_machine: "StateMachine",
    reasoning_model_call: "Callable[[str], str]",
    ctx: "BuildContext | None" = None,
    clock: "Callable[[], datetime] | None" = None,
    per_attempt_wait_seconds: int | None = None,
) -> GateCheckDeps:
    """Assemble a per-build :class:`GateCheckDeps` around ``parts``.

    The approval-side collaborators (publisher, subscriber, injector)
    come from ``parts``; the gate-evaluation collaborators are typed
    parameters because their production SQLite adapters do not exist
    yet (documented follow-up of TASK-JNB-101) — callers thread real
    adapters when they land, tests thread the in-memory doubles.

    When ``ctx`` is provided and ``parts.emitter`` is wired, the
    subscriber is bound per-build via :class:`_BoundContextSubscriber`
    so the four-step validation chain and the FW10-010 resume emit are
    fully live (see module docstring). Without either, the raw
    subscriber is injected and those behaviours stay dormant.

    Args:
        parts: The boot-scoped :class:`ApprovalGateParts`.
        priors_reader: Graphiti priors adapter (or double).
        adjustments_reader: Approved-only calibration adapter.
        rules_reader: Constitutional-rules adapter.
        repository: Gate SQLite+Graphiti write-side adapter.
        state_machine: FEAT-FORGE-001 state-machine surface.
        reasoning_model_call: Pure reasoning-model callable.
        ctx: The build's pipeline :class:`BuildContext`; supplies the
            resume-emit context and ``expected_correlation_id``.
        clock: Optional UTC-now callable for :func:`evaluate_gate`.
        per_attempt_wait_seconds: Per-attempt subscriber wait override.

    Returns:
        A fully-typed :class:`GateCheckDeps` ready for ``gate_check``.
    """
    subscriber: Any = parts.subscriber
    publish_cancelled = None
    if ctx is not None and parts.emitter is not None:
        subscriber = _BoundContextSubscriber(
            parts.subscriber,
            lifecycle_emitter=parts.emitter,
            build_context=ctx,
            expected_correlation_id=ctx.correlation_id,
        )
        emitter = parts.emitter
        build_ctx = ctx

        # TASK-JNB-102: bind the best-effort build-cancelled publisher
        # over the build's context so the gate wrapper's CANCELLED
        # transitions (reject / max-wait) signal the phone loop.
        # ``emit_cancelled`` already swallows PublishFailure
        # (_safe_publish); the wrapper adds its own DDR-007 guard.
        async def publish_cancelled(*, reason: str, cancelled_by: str) -> None:
            await emitter.emit_cancelled(
                build_ctx,
                reason=reason,
                cancelled_by=cancelled_by,
                cancelled_at=datetime.now(timezone.utc).isoformat(),
            )

    return GateCheckDeps(
        priors_reader=priors_reader,
        adjustments_reader=adjustments_reader,
        rules_reader=rules_reader,
        repository=repository,
        state_machine=state_machine,
        publisher=parts.publisher,
        subscriber=subscriber,
        injector=parts.injector,
        reasoning_model_call=reasoning_model_call,
        clock=clock,
        per_attempt_wait_seconds=per_attempt_wait_seconds,
        # Stamped onto every outbound approval-request envelope so the
        # responder can echo it and the correlation guard (step 2b) has
        # a real value to enforce — without this the four-step chain's
        # correlation step is inert against real jarvis traffic.
        correlation_id=ctx.correlation_id if ctx is not None else None,
        publish_cancelled=publish_cancelled,
    )


__all__ = [
    "ApprovalGateParts",
    "bind_gate_parts",
    "bound_gate_parts",
    "build_approval_gate_parts",
    "make_gate_check_deps",
]

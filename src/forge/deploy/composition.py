"""Production composition + gated dispatch for the WS2-B8 deploy stage.

Lane C1a ("the V1 go-live switch", three-lanes §3 C1 / close-out §3 E3): the
B8 :class:`~forge.deploy.stage.DeployStageRunner` was BUILT-INERT — constructed
only in tests, with ``deploy.enabled`` carrying **zero runtime readers**
(G-04). This module is the runner's production composition path and gives
``deploy.enabled`` its FIRST runtime reader:

- :func:`build_deploy_stage_runner` returns ``None`` when ``config.enabled`` is
  ``False`` — a byte-for-byte no-op: nothing is constructed, no seam is touched,
  nothing can dispatch. This is the flag's first reader.
- When the flag is on it composes the runner from real (or injected) seams and
  a reservation lease selected by ``config.reservation_backend`` (the scope-§4
  GPU-contention design: the shared GB10 GPU corrupted 2/5 study-tutor
  acceptance attempts under a concurrent workload, so a deploy that touches a
  reserved resource takes a lease first).
- :func:`dispatch_deploy_stage` is the standalone dispatcher the DEPLOY /
  LIVE_GATE stages run through. It is **not** part of the greenfield reasoning
  loop — DEPLOY / LIVE_GATE stay excluded from the Mode A/B/C permitted set
  (``POST_REVIEW_STAGES``); this runner is their only dispatcher, config-gated
  on ``deploy.enabled``.

Supervisor loud-fail posture (FEAT-DD4F, rule 5): an *unconfigured* seam raises
loudly when invoked — never a silent no-op that reads green. When the flag is
on but a required seam has not been wired for the target yet, the defaults are
the ``Unconfigured*`` seams (which raise on use) for a live run, or the
``DryRun*`` seams for a dry run (an honest, explicitly-labelled non-verdict).
A ``kv`` reservation backend is likewise reserved-but-unwired here: it resolves
to :class:`UnconfiguredReservationLease`, which raises if a profile actually
requests a reservation, rather than silently proceeding unprotected.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Callable

from forge.config.models import DeployStageConfig
from forge.deploy.live_gate import (
    BrokerInspector,
    DryRunBrokerInspector,
    DryRunLiveGateInvoker,
    LiveGateInvoker,
    UnconfiguredBrokerInspector,
    UnconfiguredLiveGateInvoker,
)
from forge.deploy.profile import DeployProfile
from forge.deploy.reservation import (
    InProcessReservationLease,
    ReservationLease,
    UnconfiguredReservationLease,
)
from forge.deploy.stage import DeployStageResult, DeployStageRunner
from forge.deploy.steps import SecretPresenceResolver
from forge.persistence.repositories.runbook import RunbookRepository

logger = logging.getLogger(__name__)

__all__ = [
    "resolve_reservation_lease",
    "build_deploy_stage_runner",
    "dispatch_deploy_stage",
]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def resolve_reservation_lease(
    backend: str,
    *,
    provided: ReservationLease | None = None,
) -> ReservationLease:
    """Select the reservation-lease backend for the deploy stage (scope Q2).

    ``provided`` wins when the composition root supplies a shared instance
    (production wires ONE :class:`InProcessReservationLease` at boot so every
    deploy run in the daemon honours the same in-process leases). Otherwise:

    - ``"none"`` → a fresh :class:`InProcessReservationLease` (v1 default;
      correct within a single forge process).
    - ``"kv"`` → :class:`UnconfiguredReservationLease` — the real cross-process
      GB10-GPU KV lease is reserved but NOT wired here (scope Q2 is open). It
      raises loudly if a profile requests a reservation (FEAT-DD4F: a lease
      callers trust but that does nothing is worse than none), never a silent
      unprotected proceed.

    Raises:
        ValueError: For an unknown backend name (defensive — the config field
            is ``Literal["none", "kv"]``, so this is only reachable if a caller
            passes a raw string).
    """
    if provided is not None:
        return provided
    if backend == "none":
        return InProcessReservationLease()
    if backend == "kv":
        # Reserved but unwired (scope Q2). Loud-fail if a profile requests a
        # reservation — never silently unprotected.
        return UnconfiguredReservationLease()
    raise ValueError(
        f"unknown deploy.reservation_backend {backend!r} (expected 'none' or 'kv')"
    )


def build_deploy_stage_runner(
    config: DeployStageConfig,
    *,
    repository: RunbookRepository,
    runbook_publisher: Any,
    deploy_publisher: Any,
    live_gate_invoker: LiveGateInvoker | None = None,
    broker_inspector: BrokerInspector | None = None,
    reservation: ReservationLease | None = None,
    presence_resolver: SecretPresenceResolver | None = None,
    deploy_record_root: str | None = None,
    dry_run: bool = False,
    clock: Callable[[], datetime] = _utcnow,
) -> DeployStageRunner | None:
    """Compose the deploy-stage runner, gated on ``config.enabled``.

    Returns ``None`` when ``config.enabled`` is ``False`` — the flag's first
    runtime reader and a byte-for-byte no-op: no seam is constructed, no
    reservation lease is taken, nothing can dispatch. Callers treat ``None`` as
    "deploy stage disabled" and skip silently (the default production state
    until V1).

    When the flag is on the runner is composed from the injected seams; any
    seam left unset defaults to its dry-run backend (``dry_run=True``) or its
    ``Unconfigured*`` loud-fail backend (a live run) — never a silent no-op.
    The reservation lease is selected by ``config.reservation_backend`` unless
    ``reservation`` is supplied.
    """
    if not config.enabled:
        # FIRST runtime reader of deploy.enabled. Flag OFF = byte-for-byte
        # no-op: construct nothing, dispatch nothing.
        logger.debug(
            "deploy stage disabled (deploy.enabled=False); runner not constructed"
        )
        return None

    if live_gate_invoker is None:
        live_gate_invoker = (
            DryRunLiveGateInvoker() if dry_run else UnconfiguredLiveGateInvoker()
        )
    if broker_inspector is None:
        broker_inspector = (
            DryRunBrokerInspector() if dry_run else UnconfiguredBrokerInspector()
        )
    reservation = resolve_reservation_lease(
        config.reservation_backend, provided=reservation
    )
    record_root = (
        deploy_record_root if deploy_record_root is not None else config.deploy_record_dir
    )

    return DeployStageRunner(
        repository=repository,
        runbook_publisher=runbook_publisher,
        deploy_publisher=deploy_publisher,
        reservation=reservation,
        live_gate_invoker=live_gate_invoker,
        broker_inspector=broker_inspector,
        config=config,
        deploy_record_root=record_root,
        dry_run=dry_run,
        clock=clock,
        presence_resolver=presence_resolver,
    )


async def dispatch_deploy_stage(
    config: DeployStageConfig,
    profile: DeployProfile,
    *,
    correlation_id: str,
    deploy_run_id: str,
    repository: RunbookRepository,
    runbook_publisher: Any,
    deploy_publisher: Any,
    live_gate_invoker: LiveGateInvoker | None = None,
    broker_inspector: BrokerInspector | None = None,
    reservation: ReservationLease | None = None,
    presence_resolver: SecretPresenceResolver | None = None,
    deploy_record_root: str | None = None,
    dry_run: bool = False,
    clock: Callable[[], datetime] = _utcnow,
    feature: str | None = None,
    feat_id: str | None = None,
    task_id: str | None = None,
    deploy_profile_ref: str | None = None,
    deployer: str | None = None,
) -> DeployStageResult | None:
    """Dispatch one DEPLOY (+ optional LIVE_GATE) stage through the runner.

    The standalone deploy dispatcher, config-gated on ``deploy.enabled``. When
    the flag is OFF this returns ``None`` **before touching any seam** — zero
    DEPLOY dispatch, no publish, no F7 record (the byte-for-byte no-op the coach
    proves). When the flag is on it composes the runner (:func:`build_deploy_stage_runner`)
    and drives :meth:`DeployStageRunner.run_deploy`, returning its
    :class:`DeployStageResult`.

    An unconfigured seam does not fail here — it fails loudly INSIDE the runbook
    when the offending step runs (the runner records an honest DeployFailed, a
    reservation failure returns ``outcome="failed"``), preserving the
    route-and-notify / never-silent-success posture.
    """
    runner = build_deploy_stage_runner(
        config,
        repository=repository,
        runbook_publisher=runbook_publisher,
        deploy_publisher=deploy_publisher,
        live_gate_invoker=live_gate_invoker,
        broker_inspector=broker_inspector,
        reservation=reservation,
        presence_resolver=presence_resolver,
        deploy_record_root=deploy_record_root,
        dry_run=dry_run,
        clock=clock,
    )
    if runner is None:
        # Flag OFF — no dispatch. Byte-for-byte no-op.
        return None

    return await runner.run_deploy(
        profile,
        correlation_id=correlation_id,
        deploy_run_id=deploy_run_id,
        feature=feature,
        feat_id=feat_id,
        task_id=task_id,
        deploy_profile_ref=deploy_profile_ref,
        deployer=deployer,
    )

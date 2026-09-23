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
    target_repo: str | None = None,
    target_repo_root: str | None = None,
    sandbox: Any | None = None,
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

    ``sandbox`` (sandbox first, 2026-09-07, rule 85) is the repository's entry
    from ``planning.sandboxes`` when it has one. It moves the stage's scripts
    to the deploy sidecar inside that sandbox and makes the deploy step run
    the repository's own deploy script rather than the host wrapper that would
    put it in a sandbox. ``None`` — every repository until an operator fills
    that mapping in — is byte for byte today's stage.
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
        target_repo=target_repo,
        target_repo_root=target_repo_root,
        sandbox=sandbox,
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
    target_repo: str | None = None,
    target_repo_root: str | None = None,
    sandbox: Any | None = None,
    feature: str | None = None,
    feat_id: str | None = None,
    task_id: str | None = None,
    deploy_profile_ref: str | None = None,
    deployer: str | None = None,
    leg: str = "deploy",
    candidate_cwd: str | None = None,
    prior_events: tuple[str, ...] = (),
    deploy_ownership: dict[str, Any] | None = None,
    memory_project: str | None = None,
    launch_settings: tuple[str, ...] = (),
    #: The recorded commit this work STARTS from (25 September 2026). It rides
    #: with the project's declared names so the far side reads the project's
    #: own two declaration files AT THAT COMMIT rather than off the working
    #: copy it runs the project's scripts out of. Absent ⇒ the far side falls
    #: back to that copy's committed HEAD, never its working tree.
    declared_at: str | None = None,
    identity_env: dict[str, str] | None = None,
    ask_env: dict[str, str] | None = None,
) -> DeployStageResult | None:
    """Dispatch one DEPLOY (+ optional LIVE_GATE) stage through the runner.

    The standalone deploy dispatcher, config-gated on ``deploy.enabled``. When
    the flag is OFF this returns ``None`` **before touching any seam** — zero
    DEPLOY dispatch, no publish, no F7 record (the byte-for-byte no-op the coach
    proves). When the flag is on it composes the runner (:func:`build_deploy_stage_runner`)
    and drives the leg asked for, returning its :class:`DeployStageResult`.

    ``leg`` (protect-main, rule 39) names which part of the stage runs:

    * ``"deploy"`` (the default) — :meth:`DeployStageRunner.run_deploy`, both
      legs in one call, exactly as before;
    * ``"candidate_check"`` — :meth:`DeployStageRunner.candidate_check`, with
      ``candidate_cwd`` as the candidate's working directory (the feature
      branch's laid-out tree);
    * ``"promote"`` — :meth:`DeployStageRunner.promote`, with ``prior_events``
      the events the candidate leg already published for this run;
    * ``"candidate_down"`` — :meth:`DeployStageRunner.candidate_down`;
    * ``"what_is_running"`` — :meth:`DeployStageRunner.what_is_running`, with
      ``ask_env`` the question the project declared it wants to be asked with.
      It changes nothing and takes no lock: the press asks it before it decides
      whether to deploy, so the only-forwards rule is applied to what the TARGET
      says rather than to a ledger row a crash may have left stale.

    Any other word is refused with a ``ValueError`` before a seam is touched.

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
        target_repo=target_repo,
        target_repo_root=target_repo_root,
        sandbox=sandbox,
    )
    if runner is None:
        # Flag OFF — no dispatch. Byte-for-byte no-op.
        return None

    if leg == "deploy":
        return await runner.run_deploy(
            profile,
            correlation_id=correlation_id,
            deploy_run_id=deploy_run_id,
            feature=feature,
            feat_id=feat_id,
            task_id=task_id,
            deploy_profile_ref=deploy_profile_ref,
            deployer=deployer,
            declared_at=declared_at,
            identity_env=identity_env,
        )
    if leg == "candidate_check":
        return await runner.candidate_check(
            profile,
            correlation_id=correlation_id,
            deploy_run_id=deploy_run_id,
            feature=feature,
            feat_id=feat_id,
            task_id=task_id,
            deploy_profile_ref=deploy_profile_ref,
            candidate_cwd=candidate_cwd,
            # WHAT THE CHECK IS HANDED SO IT CAN PIN WHAT IT CHECKED, and the
            # project's own declarations so the child's environment is built
            # rather than copied. Absent ⇒ exactly what it was.
            identity_env=identity_env,
            memory_project=memory_project,
            launch_settings=tuple(launch_settings),
            declared_at=declared_at,
        )
    if leg == "what_is_running":
        return await runner.what_is_running(
            profile,
            correlation_id=correlation_id,
            deploy_run_id=deploy_run_id,
            ask_env=dict(ask_env or {}),
            memory_project=memory_project,
            launch_settings=tuple(launch_settings),
            declared_at=declared_at,
        )
    if leg == "promote":
        return await runner.promote(
            profile,
            correlation_id=correlation_id,
            deploy_run_id=deploy_run_id,
            feature=feature,
            feat_id=feat_id,
            task_id=task_id,
            deploy_profile_ref=deploy_profile_ref,
            deployer=deployer,
            prior_events=tuple(prior_events),
            # WHO OWNS THE TARGET THIS LEG CHANGES, and what the project
            # declared. Present only on a press that took the deployment lock;
            # absent on every caller written before it, whose runbook is
            # byte-identical.
            deploy_ownership=deploy_ownership,
            memory_project=memory_project,
            launch_settings=tuple(launch_settings),
            declared_at=declared_at,
            identity_env=identity_env,
        )
    if leg == "candidate_down":
        return await runner.candidate_down(
            profile,
            correlation_id=correlation_id,
            deploy_run_id=deploy_run_id,
            # The same setting the CHECK was handed, so a project whose
            # candidate belongs to one check rather than to a shared name can
            # be told which one to take down. Absent ⇒ exactly what it was.
            identity_env=identity_env,
        )
    raise ValueError(
        f"unknown deploy leg {leg!r} — expected 'deploy', 'candidate_check', "
        "'promote', 'candidate_down' or 'what_is_running'"
    )

"""Serve-boot composition for the WS2-B8 deploy stage (Lane C1a).

Wires, behind ``deploy.enabled`` (default False), the deploy-stage runner into
serve boot so the flag has a genuine production reader. Mirrors the
``_serve_planning`` / ``review_gate`` flag-gated composition posture:

- ``deploy.enabled=False`` (the shipped default) → this is a byte-for-byte
  no-op: the runner is not constructed, no NATS publisher is bound, no
  reservation lease is taken. The daemon boots exactly as before.
- ``deploy.enabled=True`` → the runner is composed from the daemon's shared
  NATS client (the B7 deploy-domain + FMDR step-lifecycle publishers), a
  runbook repository over the forge DB, and a single process-shared
  :class:`InProcessReservationLease` (or the reserved ``kv`` backend, which
  loud-fails per the supervisor posture). The composed runner is stashed on
  :mod:`forge.cli._serve_daemon` for the post-review deploy trigger (Lane C4)
  to reach.

DEPLOY / LIVE_GATE stay OUT of the greenfield reasoning loop (they are filtered
from the Mode A/B/C permitted set — ``stage_taxonomy.POST_REVIEW_STAGES``); the
runner composed here is their only dispatcher. The per-target real live-gate /
broker seams are wired at the V1 drive (Lane C4); until then the runner carries
the ``Unconfigured*`` seams, which raise loudly if a live deploy fires before
they are wired — never a silent green (FEAT-DD4F).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["compose_deploy_stage_runner"]


def compose_deploy_stage_runner(
    *,
    forge_config: Any,
    nats_client: Any,
    db_path: Any | None = None,
    reservation: Any | None = None,
) -> Any | None:
    """Compose the deploy-stage runner at serve boot, gated on ``deploy.enabled``.

    Returns the composed :class:`~forge.deploy.stage.DeployStageRunner`, or
    ``None`` when the deploy stage is disabled (the default). The caller stashes
    a non-None runner where the post-review deploy path can reach it.

    Args:
        forge_config: The validated ``ForgeConfig`` (reads ``config.deploy``).
        nats_client: The daemon's shared raw NATS client — the B7
            deploy-domain publisher and the reused FMDR step-lifecycle
            publisher are bound against it.
        db_path: Path to the forge SQLite DB for the runbook repository.
            Required when the flag is on (the runner persists its deploy /
            live-gate runbooks there); a missing path with the flag on is a
            loud misconfiguration.
        reservation: Optional pre-built reservation lease to share across runs.
            When ``None`` the backend named by ``deploy.reservation_backend`` is
            resolved (``none`` → a fresh in-process lease; ``kv`` → the
            reserved, loud-failing lease).
    """
    deploy_config = forge_config.deploy
    if not deploy_config.enabled:
        # FIRST production reader of deploy.enabled. Flag OFF = byte-for-byte
        # no-op — the runner is never constructed.
        logger.info(
            "Deploy stage disabled (deploy.enabled=False); skipping composition "
            "(DEPLOY / LIVE_GATE inert)"
        )
        return None

    # Heavy imports stay call-time so this module imports without the full
    # deploy stack available (BDD oracle / lint runners), matching the
    # _serve_planning posture.
    from forge.adapters.nats.deploy_publisher import DeployPublisher
    from forge.adapters.nats.runbook_publisher import RunbookPublisher
    from forge.deploy.composition import (
        build_deploy_stage_runner,
        resolve_reservation_lease,
    )
    from forge.adapters.sqlite.connect import connect_writer
    from forge.persistence.migrations import runbook as runbook_migration
    from forge.persistence.repositories.runbook import RunbookRepository

    if db_path is None:
        raise RuntimeError(
            "deploy.enabled=true but no db_path was threaded into deploy "
            "composition — the deploy-stage runner cannot persist its runbooks"
        )

    connection = connect_writer(db_path)
    # The deploy-domain DDL is boot-idempotent (CREATE TABLE IF NOT EXISTS) and
    # a production DB may predate it — apply here so BOTH production
    # construction sites (serve boot + the deploy CLI) guarantee the schema
    # (C4 live-caught: `no such table: runbooks` on the first live dispatch).
    runbook_migration.apply(connection)
    repository = RunbookRepository(connection=connection)
    runbook_publisher = RunbookPublisher(nats_client=nats_client)
    deploy_publisher = DeployPublisher(nats_client=nats_client)
    lease = resolve_reservation_lease(
        deploy_config.reservation_backend, provided=reservation
    )

    runner = build_deploy_stage_runner(
        deploy_config,
        repository=repository,
        runbook_publisher=runbook_publisher,
        deploy_publisher=deploy_publisher,
        reservation=lease,
        # Per-target real live-gate / broker seams are wired at the V1 drive
        # (Lane C4). Until then the Unconfigured* defaults raise loudly if a
        # live deploy fires — the supervisor loud-fail posture, never a silent
        # green.
    )
    logger.info(
        "Deploy stage composed (deploy.enabled=true); reservation_backend=%s, "
        "run_live_gate=%s — live-gate/broker seams pending the V1 drive (C4)",
        deploy_config.reservation_backend,
        deploy_config.run_live_gate,
    )
    return runner

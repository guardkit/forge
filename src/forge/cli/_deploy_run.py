"""``forge deploy`` execution body — the attended post-review dispatch (C4-prep).

The operator-invoked entry point for the WS2-B8 DEPLOY + LIVE_GATE stages. Merge
stays a human act (gates-not-PRs / DF-021), so deploy dispatch is likewise an
explicit human moment until the trust ladder graduates it — this command is the
production dispatch path for :func:`forge.deploy.composition.dispatch_deploy_stage`
(the serve-boot stash at ``_serve_daemon.deploy_stage_runner`` remains for the
future in-daemon trigger).

Flag-off posture (FEAT-DD4F / the byte-for-byte no-op): when
``deploy.enabled=False`` (the shipped default) this prints a one-line refusal and
exits ``3`` **before touching NATS, the DB, or any seam** — the flag flip is an
attended human moment, never this command.

Exit codes:

* ``0`` — outcome ``complete`` (deploy + optional live-gate PASSED).
* ``1`` — outcome ``failed`` / ``escalated`` (a step failed or a revert was
  impossible), or a load-time refusal (unknown repo, missing profile).
* ``2`` — outcome ``reverted`` (O-32 safety valve fired — the deploy is NOT
  live; the kept ``:rollback-*`` image was re-deployed).
* ``3`` — ``deploy.enabled=False`` (flag off — inert, no side effects).

Test seams (rebindable): :func:`_make_live_gate_invoker` (built from
``profile.live_gate``) and :func:`_aopen_backends` (the real NATS + DB wiring)
are module-level so CLI tests drive the exit-code mapping against a fixed-verdict
invoker and a tmp-DB repository without a live broker.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

import click

from forge.config.models import ForgeConfig
from forge.deploy.composition import dispatch_deploy_stage
from forge.deploy.live_gate import LiveGateInvoker, RepoDriverLiveGateInvoker
from forge.deploy.profile import (
    DeployProfile,
    DeployProfileError,
    load_deploy_profile,
)

logger = logging.getLogger(__name__)

__all__ = ["execute_deploy"]

#: Exit codes (see the module docstring).
EXIT_COMPLETE = 0
EXIT_FAILED = 1
EXIT_REVERTED = 2
EXIT_FLAG_OFF = 3

#: Default forge DB path when ``$FORGE_DB_PATH`` is unset (the queue precedent).
DEFAULT_DB_PATH = Path("~/.forge/forge.db")


def _resolve_db_path() -> Path:
    """The forge DB path: ``$FORGE_DB_PATH`` then ``~/.forge/forge.db``."""
    raw = os.environ.get("FORGE_DB_PATH")
    return Path(raw).expanduser() if raw else DEFAULT_DB_PATH.expanduser()


def _make_live_gate_invoker(
    profile: DeployProfile, repo_path: Path
) -> LiveGateInvoker | None:
    """Build the per-target real live-gate backend from ``profile.live_gate``.

    Returns ``None`` when the profile carries no ``live_gate`` section — the
    seam then stays ``Unconfigured`` and loud-fails at the step (deny by
    default). A test seam: patched to inject a fixed-verdict invoker.
    """
    spec = profile.live_gate
    if spec is None:
        return None
    return RepoDriverLiveGateInvoker(
        repo_path=repo_path,
        driver_argv=list(spec.driver),
        timeout_seconds=spec.timeout_seconds,
        extra_env=dict(spec.env),
    )


async def _aopen_backends(
    config: ForgeConfig, *, dry_run: bool
) -> tuple[Any, Any, Any, Callable[[], Awaitable[None]]]:
    """Connect NATS + open the forge DB; return the dispatch backends + closer.

    Mirrors the daemon / ``_serve_deploy`` composition: a raw NATS client bound
    to the B7 :class:`DeployPublisher` and the reused FMDR
    :class:`RunbookPublisher`, and a :class:`RunbookRepository` over the forge
    DB. A test seam: patched to hand back a tmp-DB repository + recording
    publishers so the dispatch runs with no broker.
    """
    import nats  # type: ignore[import-not-found]

    from forge.adapters.nats.deploy_publisher import DeployPublisher
    from forge.adapters.nats.runbook_publisher import RunbookPublisher
    from forge.adapters.sqlite.connect import connect_writer
    from forge.persistence.migrations import runbook as runbook_migration
    from forge.persistence.repositories.runbook import RunbookRepository

    servers = os.environ.get("FORGE_NATS_URL", "nats://127.0.0.1:4222")
    client = await nats.connect(servers=servers)
    connection = connect_writer(_resolve_db_path())
    # The deploy-domain DDL is boot-idempotent and the production DB predates
    # it — nothing on the production paths applied it (C4 live-caught: the
    # first dispatch died on `no such table: runbooks`).
    runbook_migration.apply(connection)
    repository = RunbookRepository(connection=connection)
    runbook_publisher = RunbookPublisher(nats_client=client)
    deploy_publisher = DeployPublisher(nats_client=client)

    async def _close() -> None:
        try:
            await client.drain()
        except Exception as exc:  # noqa: BLE001 — best-effort teardown
            logger.debug("nats drain on deploy teardown failed: %s", exc)

    return repository, runbook_publisher, deploy_publisher, _close


async def _adispatch(
    config: ForgeConfig,
    profile: DeployProfile,
    repo_path: Path,
    *,
    invoker: LiveGateInvoker | None,
    dry_run: bool,
    feature_id: str,
    task_id: str | None,
    correlation_id: str,
    deploy_run_id: str,
    deployer: str | None,
    target_repo: str,
) -> Any | None:
    """Open backends, dispatch the deploy stage, then tear the backends down."""
    (
        repository,
        runbook_publisher,
        deploy_publisher,
        closer,
    ) = await _aopen_backends(config, dry_run=dry_run)
    try:
        return await dispatch_deploy_stage(
            config.deploy,
            profile,
            correlation_id=correlation_id,
            deploy_run_id=deploy_run_id,
            repository=repository,
            runbook_publisher=runbook_publisher,
            deploy_publisher=deploy_publisher,
            live_gate_invoker=invoker,
            deploy_record_root=str(repo_path / config.deploy.deploy_record_dir),
            dry_run=dry_run,
            target_repo=target_repo,
            target_repo_root=str(repo_path),
            feature=feature_id,
            feat_id=feature_id,
            task_id=task_id,
            deployer=deployer,
        )
    finally:
        await closer()


def execute_deploy(
    *,
    config: ForgeConfig | None,
    feature_id: str,
    repo: str,
    task_id: str | None,
    correlation: str | None,
    dry_run: bool,
    deployer: str | None,
) -> int:
    """Run one attended deploy dispatch; return the process exit code.

    Raises:
        click.ClickException: For a load-time refusal (no config, unknown target
            repo, or a target with no deployable ``deploy/profile.yaml``).
    """
    if config is None:
        raise click.ClickException(
            "forge deploy needs a forge.yaml (pass --config or run from a "
            "directory that ships one) — it reads deploy.enabled and "
            "planning.target_repo_paths."
        )

    deploy_cfg = config.deploy

    # --- flag-off pre-check: NO seam / NATS / DB touch ---------------------
    if not deploy_cfg.enabled:
        click.echo(
            "refusing to deploy: deploy.enabled=False (the deploy stage is inert "
            "until an attended flip) — no NATS, DB, or seam was touched.",
            err=True,
        )
        return EXIT_FLAG_OFF

    # --- resolve the target repo path --------------------------------------
    paths = config.planning.target_repo_paths
    if repo not in paths:
        known = ", ".join(sorted(paths)) or "(none configured)"
        raise click.ClickException(
            f"unknown target repo {repo!r} — not in planning.target_repo_paths. "
            f"Known keys: {known}"
        )
    repo_path = Path(paths[repo])

    # --- load the target's deploy profile (a must for deployability) -------
    profile_path = repo_path / "deploy" / "profile.yaml"
    try:
        profile = load_deploy_profile(profile_path)
    except DeployProfileError as exc:
        raise click.ClickException(
            f"target repo {repo!r} is not deployable: {exc}"
        ) from exc

    invoker = _make_live_gate_invoker(profile, repo_path)
    correlation_id = correlation or f"deploy-{uuid.uuid4()}"
    deploy_run_id = str(uuid.uuid4())

    result = asyncio.run(
        _adispatch(
            config,
            profile,
            repo_path,
            invoker=invoker,
            dry_run=dry_run,
            feature_id=feature_id,
            task_id=task_id,
            correlation_id=correlation_id,
            deploy_run_id=deploy_run_id,
            deployer=deployer,
            target_repo=repo,
        )
    )

    # Defensive: dispatch returns None only when the flag is off — already
    # pre-checked, but honour the contract rather than assume.
    if result is None:
        click.echo(
            "deploy stage inert (deploy.enabled=False) — nothing dispatched.",
            err=True,
        )
        return EXIT_FLAG_OFF

    click.echo(
        f"deploy {feature_id} @ {repo}: outcome={result.outcome} "
        f"verdict={result.verdict} record={result.deploy_record_ref}"
    )
    return {
        "complete": EXIT_COMPLETE,
        "reverted": EXIT_REVERTED,
    }.get(result.outcome, EXIT_FAILED)

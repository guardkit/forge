"""``forge deploy`` — the attended post-review deploy dispatch (C4-prep).

A thin Click shell over :func:`forge.cli._deploy_run.execute_deploy` (the
cancel.py precedent — behaviour lives in the helper). This is the operator-
invoked production dispatch for the WS2-B8 DEPLOY + LIVE_GATE stages: merge stays
a human act (gates-not-PRs / DF-021), so deploy dispatch is likewise explicit and
human-invoked until the trust ladder graduates it. The serve-boot runner stash
(``_serve_daemon.deploy_stage_runner``) remains for the future in-daemon trigger.

``deploy.enabled`` defaults False; with the flag off this refuses in one line and
exits 3 without touching NATS, the DB, or any seam. ``--config`` is GROUP-level
(``forge --config forge.yaml deploy ...``), matching queue/cancel.
"""

from __future__ import annotations

import sys

import click

from forge.cli._deploy_run import execute_deploy
from forge.config.models import ForgeConfig


@click.command(name="deploy")
@click.argument("feature_id")
@click.option(
    "--repo",
    required=True,
    help="Target repo as 'org/name' — resolved via planning.target_repo_paths.",
)
@click.option("--task-id", "task_id", default=None, help="Optional task id.")
@click.option(
    "--correlation",
    default=None,
    help="Correlation id for the deploy run; a uuid is minted when omitted.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Record what each deploy step WOULD do (labelled, zero blast radius).",
)
@click.option(
    "--deployer",
    default=None,
    help="Operator identity stamped on the F7 deploy record.",
)
@click.pass_context
def deploy_cmd(
    ctx: click.Context,
    feature_id: str,
    repo: str,
    task_id: str | None,
    correlation: str | None,
    dry_run: bool,
    deployer: str | None,
) -> None:
    """Dispatch the attended DEPLOY + LIVE_GATE stage for FEATURE_ID.

    Exit codes: 0 complete, 2 reverted (O-32 safety valve — NOT live), 1 failed,
    3 deploy.enabled=False (inert, no side effects).
    """
    config = ctx.obj if isinstance(ctx.obj, ForgeConfig) else None
    code = execute_deploy(
        config=config,
        feature_id=feature_id,
        repo=repo,
        task_id=task_id,
        correlation=correlation,
        dry_run=dry_run,
        deployer=deployer,
    )
    sys.exit(code)


__all__ = ["deploy_cmd"]

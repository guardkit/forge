"""``forge merge-deploy`` — the attended merge word (first-fire + fallback).

Make-merge-work build spec (2026-08-24) piece 4: the operator-invoked path
through the SAME executor coroutine the card press runs. The invocation IS
the human word — no card, no approval envelope, no waiting. This is the
first-fire path (prove the executor attended before the card spreads) and
the card-lost fallback (an offer whose publish died still has its latch; the
merge still happens on this command).

Resolves the newest COMPLETE routine build row for FEATURE_ID (or the row
named by ``--build-id``), computes expect-main-sha NOW (main may have moved
since the build — the merge verb refuses if it moves again after this), and
prints receipt lines.

Exit codes: 0 = merged and running (PASSED); 1 = any other outcome (the
line printed says plainly which step failed and why).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

import click

from forge.config.models import ForgeConfig

logger = logging.getLogger(__name__)

__all__ = ["merge_deploy_cmd"]

#: Default forge DB path when ``$FORGE_DB_PATH`` is unset (the queue precedent).
DEFAULT_DB_PATH = Path("~/.forge/forge.db")


def _resolve_db_path() -> Path:
    """The forge DB path: ``$FORGE_DB_PATH`` then ``~/.forge/forge.db``."""
    raw = os.environ.get("FORGE_DB_PATH")
    return Path(raw).expanduser() if raw else DEFAULT_DB_PATH.expanduser()


def _open_pool(db_path: Path) -> Any:
    """Open the lifecycle persistence facade (writer — stage rows are written).

    A module-level test seam: CLI tests rebind it to a tmp-DB pool.
    """
    from forge.adapters.sqlite.connect import connect_writer
    from forge.lifecycle import migrations
    from forge.lifecycle.persistence import SqliteLifecyclePersistence

    connection = connect_writer(db_path)
    migrations.apply_at_boot(connection)
    return SqliteLifecyclePersistence(connection=connection, db_path=db_path)


async def _aopen_backends(
    config: ForgeConfig,
) -> tuple[Any, Any, Any, Callable[[], Awaitable[None]]]:
    """Connect NATS; return (pipeline_publisher, guardkit_run, dispatcher, closer).

    Mirrors ``_deploy_run._aopen_backends``. A module-level test seam: CLI
    tests rebind it to recording fakes so the command runs with no broker.
    """
    import nats  # type: ignore[import-not-found]

    from forge.adapters.guardkit.run import run as guardkit_run
    from forge.adapters.nats.pipeline_publisher import PipelinePublisher
    from forge.pipeline.merge_executor import build_in_daemon_deploy_dispatcher

    servers = os.environ.get("FORGE_NATS_URL", "nats://127.0.0.1:4222")
    client = await nats.connect(servers=servers)
    publisher = PipelinePublisher(client)
    dispatcher = build_in_daemon_deploy_dispatcher(
        config=config, nats_client=client, db_path=_resolve_db_path()
    )

    async def _close() -> None:
        try:
            await client.drain()
        except Exception as exc:  # noqa: BLE001 — best-effort teardown
            logger.debug("nats drain on merge-deploy teardown failed: %s", exc)

    return publisher, guardkit_run, dispatcher, _close


def _resolve_build_row(pool: Any, feature_id: str, build_id: str | None) -> Any:
    """The newest COMPLETE non-fix-journey build row (or the named one).

    Routine means "not a fix journey" — the machine chain has queued builds
    as both mode-a and mode-b over time, and both are mergeable feature
    builds; only mode-c (the fix journey) is excluded.
    """
    from forge.lifecycle.modes import BuildMode
    from forge.lifecycle.state_machine import BuildState

    if build_id:
        row = pool.get_build_row(build_id)
        if row is None:
            raise click.ClickException(f"no builds row exists for {build_id!r}")
        if row.feature_id != feature_id:
            raise click.ClickException(
                f"build {build_id!r} belongs to {row.feature_id}, not "
                f"{feature_id} — refusing the mismatch"
            )
        return row
    for row in pool.read_history(limit=1000, feature_id=feature_id):
        if row.status is BuildState.COMPLETE and row.mode is not BuildMode.MODE_C:
            return row
    raise click.ClickException(
        f"no COMPLETE routine build is on record for {feature_id} — nothing "
        "to merge (name one explicitly with --build-id if you must)"
    )


async def _arun(
    config: ForgeConfig, feature_id: str, build_id: str | None, dry_run: bool
) -> int:
    from forge.pipeline.merge_executor import (
        MergeExecutorDeps,
        execute_merge_deploy,
    )
    from forge.pipeline.merge_offer import (
        git_rev_parse_main,
        read_baseline_failing,
    )

    pool = _open_pool(_resolve_db_path())
    row = _resolve_build_row(pool, feature_id, build_id)

    paths = config.planning.target_repo_paths
    if row.repo not in paths:
        known = ", ".join(sorted(paths)) or "(none configured)"
        raise click.ClickException(
            f"unknown target repo {row.repo!r} — not in "
            f"planning.target_repo_paths. Known keys: {known}"
        )
    repo_root = Path(paths[row.repo])

    expect_main_sha = await git_rev_parse_main(repo_root)
    if expect_main_sha is None:
        raise click.ClickException(
            f"could not read main's sha in {repo_root} — refusing an "
            "unpinned merge"
        )
    baseline_failing = read_baseline_failing(row.build_id)

    publisher, guardkit_run, dispatcher, closer = await _aopen_backends(config)
    try:
        deps = MergeExecutorDeps(
            config=config,
            pool=pool,
            pipeline_publisher=publisher,
            guardkit_run=guardkit_run,
            deploy_dispatcher=dispatcher,
        )
        decided_by = config.approval.expected_approver or os.environ.get(
            "USER", "operator"
        )
        outcome = await execute_merge_deploy(
            deps=deps,
            build_id=row.build_id,
            feature_id=row.feature_id,
            repo=row.repo,
            repo_root=repo_root,
            expect_main_sha=expect_main_sha,
            correlation_id=row.correlation_id or f"merge-{row.build_id}",
            decided_by=decided_by,
            baseline_failing=baseline_failing,
            dry_run=dry_run,
        )
    finally:
        await closer()

    click.echo(
        f"merge-deploy {row.feature_id} @ {row.repo}: result={outcome.result} "
        f"status={outcome.status}"
    )
    if outcome.merged_sha:
        click.echo(f"  merged_sha={outcome.merged_sha}")
    if outcome.failed_step:
        click.echo(f"  failed_step={outcome.failed_step}")
    click.echo(f"  {outcome.detail}")
    click.echo(f"  receipts: merge-{row.build_id}/ under the receipts root")
    return 0 if outcome.status == "PASSED" else 1


@click.command(name="merge-deploy")
@click.argument("feature_id")
@click.option(
    "--build-id",
    "build_id",
    default=None,
    help=(
        "Run against this exact build row instead of the newest COMPLETE "
        "routine build for the feature."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help=(
        "Genuinely dry: nothing merges, no durable step rows are claimed, "
        "nothing is published to Slack — the deploy stage runs in its own "
        "labelled dry mode and the receipts on disk are the only record."
    ),
)
@click.pass_context
def merge_deploy_cmd(
    ctx: click.Context,
    feature_id: str,
    build_id: str | None,
    dry_run: bool,
) -> None:
    """Merge FEATURE_ID into main, deploy it, and verify it — attended.

    The invocation IS the human word: the same executor the merge card's
    press runs, fired directly. Exit 0 = merged and running; 1 = anything
    else (the printed line names the failed step).
    """
    config = ctx.obj if isinstance(ctx.obj, ForgeConfig) else None
    if config is None:
        raise click.ClickException(
            "forge merge-deploy needs a forge.yaml (pass --config or run "
            "from a directory that ships one) — it reads "
            "planning.target_repo_paths, approval.expected_approver and the "
            "deploy section."
        )
    code = asyncio.run(_arun(config, feature_id, build_id, dry_run))
    sys.exit(code)

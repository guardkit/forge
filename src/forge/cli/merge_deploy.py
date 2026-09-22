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

The order is the executor's, so it is the same as the card's (protect-main,
rule 39): the feature branch is checked in the Docker Sandbox FIRST, and only
if every check passes does the merge land and that exact build get promoted.
A branch that fails the check is never merged.

For a repository whose factory lives in its own sandbox (sandbox first, rule
89) every git operation of the press — main's commit for the pin, the branch,
the candidate's tree, the ancestry checks and the tree comparison — happens
inside that sandbox, on the factory's clone, exactly as it does for the card's
press. A repository with no sandbox is pressed here, as before.

The branch merged is the branch the build made (Part M of the rewrite-on-refusal
spec): the row's recorded ``merge_branch`` when the conductor cut one (a
repair's ``fix/<task id>-<build8>``, reachable here with ``--build-id``), else
the feature's own ``autobuild/<feature id>``; the printed line names it when it
is not the feature's own.

Exit codes: 0 = the joined result was checked (PASSED); 1 = any other outcome
(the line printed says plainly which step failed and why).

WHAT "PASSED" MEANS IN THIS VERSION (22 September 2026, the merge word's
join). The press fetches the branch of the remote this work was recorded
against, joins the build onto the commit that branch is at, in a working
folder of its own, and runs both kinds of check on the joined result. It then
stops: the publisher has not been built, so nothing is sent to the remote and
nothing is deployed. The result word is ``publication-pending`` and the
sentence says "checked and ready to publish; publication is not switched on".
It never says "merged and running".
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

    from forge.cli.serve import compose_merge_guardkit_run
    from forge.adapters.nats.pipeline_publisher import PipelinePublisher
    from forge.pipeline.merge_executor import build_in_daemon_deploy_dispatcher

    servers = os.environ.get("FORGE_NATS_URL", "nats://127.0.0.1:4222")
    client = await nats.connect(servers=servers)
    publisher = PipelinePublisher(client)
    # The same chooser the daemon uses: the merge word's checks run on the
    # host through the deploy sidecar when one is configured. 2026-09-06:
    # this attended command still ran guardkit inside the container after
    # the daemon had moved to the host, and the first press it carried
    # merged and then said "test runner could not start".
    guardkit_run = compose_merge_guardkit_run(config)
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

    # WHERE this repository's git happens (sandbox first, rule 89): inside its
    # sandbox when it has one, in this process when it has not. The pin is the
    # first git the press needs, so it is read in the same place as the rest.
    from forge.cli.serve import compose_merge_git_surface

    git_surface = compose_merge_git_surface(config)
    surface = git_surface(row.repo, repo_root) if git_surface is not None else None
    if surface is None:
        expect_main_sha = await git_rev_parse_main(repo_root)
    else:
        expect_main_sha = await surface.rev_parse("main")
    if expect_main_sha is None:
        where = (
            f"in {repo_root}"
            if surface is None
            else f"in the sandbox that holds {row.repo}"
        )
        raise click.ClickException(
            f"could not read main's sha {where} — refusing an unpinned merge"
        )
    baseline_failing = read_baseline_failing(row.build_id)
    # The branch the build made, when the conductor recorded one (a repair).
    merge_branch = str(getattr(row, "merge_branch", None) or "").strip() or None

    publisher, guardkit_run, dispatcher, closer = await _aopen_backends(config)
    try:
        deps = MergeExecutorDeps(
            config=config,
            pool=pool,
            pipeline_publisher=publisher,
            guardkit_run=guardkit_run,
            git_surface=git_surface,
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
            merge_branch=merge_branch,
        )
    finally:
        await closer()

    # The branch is named only when it is not the feature's own (Part M,
    # rule 55), so a feature build's line reads exactly as before.
    named = (
        f"{row.feature_id} (branch {merge_branch})"
        if merge_branch is not None
        else row.feature_id
    )
    click.echo(
        f"merge-deploy {named} @ {row.repo}: result={outcome.result} "
        f"status={outcome.status}"
    )
    gate = outcome.gate_before_merge or {}
    if gate.get("verdict") is not None:
        passed, total = gate.get("checks_passed"), gate.get("checks_total")
        counted = (
            f" ({passed} of {total} checks passed)"
            if isinstance(passed, int) and isinstance(total, int)
            else ""
        )
        click.echo(f"  checked in the sandbox before merging: {gate['verdict']}{counted}")
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
    """Join FEATURE_ID onto the remote's recorded branch and check it — attended.

    The invocation IS the human word: the same executor the merge card's
    press runs, fired directly, in the same order — the branch of the remote
    this work was recorded against is fetched, the build is joined onto the
    commit it is at in a working folder of its own, and both kinds of check
    run on the joined result. Nothing is published and nothing is deployed:
    the publisher has not been built. Exit 0 = the joined result was checked;
    1 = anything else (the printed line names the failed step).
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

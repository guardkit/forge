"""``forge skip`` — thin wrapper over :class:`CliSteeringHandler` (TASK-PSM-011)."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from forge.cli._responder import resolve_responder
from forge.cli.runtime import build_cli_runtime
from forge.pipeline.cli_steering import BuildLifecycle, SkipStatus


@click.command(name="skip")
@click.argument("identifier")
@click.option("--reason", default="cli skip", show_default=True,
              help="Reason recorded on the synthetic-override resolution.")
@click.option("--responder", default=None,
              help="Operator identity; falls back to $FORGE_RESPONDER then the "
                   "OS user. Never requires a controlling terminal (O-02).")
@click.option("--db", "db_path", required=True,
              type=click.Path(path_type=Path, dir_okay=False, exists=True),
              help="Path to forge.db (SQLite).")
def skip_cmd(identifier: str, reason: str, responder: str | None, db_path: Path) -> None:
    """Skip the paused stage of an active or recent build for ``identifier``."""
    runtime = build_cli_runtime(db_path)
    build = runtime.persistence.find_active_or_recent(identifier)
    if build is None:
        click.echo(
            f"forge skip: no active or recent build for {identifier!r}", err=True
        )
        sys.exit(2)
    snap = runtime.cli_steering_handler.snapshot_reader.get_snapshot(build.build_id)
    if snap.lifecycle is not BuildLifecycle.PAUSED_AT_GATE:
        click.echo(
            f"forge skip: REFUSED — skip not allowed unless paused "
            f"(build {build.build_id!r} lifecycle={snap.lifecycle.value})",
            err=True,
        )
        sys.exit(3)
    outcome = runtime.cli_steering_handler.handle_skip(
        build_id=build.build_id, stage=snap.paused_stage,
        reason=reason, responder=resolve_responder(responder),
    )
    if outcome.status is SkipStatus.REFUSED_CONSTITUTIONAL:
        click.echo(f"forge skip: REFUSED — {outcome.rationale}", err=True)
        sys.exit(4)
    click.echo(f"Skipped {build.build_id} stage={snap.paused_stage.value}")
    click.echo(outcome.rationale)


__all__ = ["skip_cmd"]

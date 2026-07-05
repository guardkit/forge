"""``forge cancel`` — thin wrapper over :class:`CliSteeringHandler` (TASK-PSM-011).

Resolves ``feature_id|build_id`` via ``find_active_or_recent`` then delegates to
``handle_cancel`` (Group E audit trail). TASK-GATE-D659 §D6: a build PAUSED at
the daemon-side approval gate is instead routed through
:func:`forge.cli._cancel_gate_inject.try_inject_paused_cancel` (synthetic reject
into the daemon's live gate frame — single owner of the CANCELLED transition +
``build-cancelled`` emit). Behavioural rules live in the handler.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click

from forge.cli._cancel_gate_inject import try_inject_paused_cancel
from forge.cli.runtime import build_cli_runtime
from forge.lifecycle.state_machine import BuildState


@click.command(name="cancel")
@click.argument("identifier")
@click.option("--reason", default="cli cancel", show_default=True)
@click.option(
    "--db",
    "db_path",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    required=True,
    help="Path to forge.db (SQLite).",
)
def cancel_cmd(identifier: str, reason: str, db_path: Path) -> None:
    """Cancel an active or recent build for ``identifier``."""
    runtime = build_cli_runtime(db_path)
    build = runtime.persistence.find_active_or_recent(identifier)
    if build is None:
        click.echo(
            f"forge cancel: no active or recent build for {identifier!r}", err=True
        )
        sys.exit(2)
    # PAUSED-at-gate → daemon's live gate frame owns it (§D6). Only PAUSED
    # reaches get_build_row; non-paused cancels are unchanged.
    if build.status is BuildState.PAUSED and try_inject_paused_cancel(
        runtime, build_id=build.build_id, reason=reason
    ):
        click.echo(f"forge cancel: {build.build_id} — synthetic reject injected.")
        return
    outcome = runtime.cli_steering_handler.handle_cancel(
        build_id=build.build_id, reason=reason, responder=os.getlogin()
    )
    click.echo(f"Cancelled {build.build_id}: {outcome.status.value}")
    click.echo(outcome.rationale)


__all__ = ["cancel_cmd"]

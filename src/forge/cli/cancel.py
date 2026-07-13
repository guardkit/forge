"""``forge cancel`` — thin wrapper over :class:`CliSteeringHandler` (TASK-PSM-011).

Delegates to :func:`forge.cli._cancel_run.execute_cancel`, which resolves the
identifier via ``find_active_or_recent`` then either injects a synthetic reject
for a PAUSED-at-gate build (§D6) or delegates to ``handle_cancel``. The ops
hardening (O-02 / TASK-FWD-005) — canonical ``$FORGE_DB_PATH`` resolution,
``--responder`` without a controlling terminal, and identity-pinned paused
cancel — lives in that helper so this wrapper stays a thin Click shell
(AC-007). Behavioural rules live in the handler.
"""

from __future__ import annotations

from pathlib import Path

import click

from forge.cli._cancel_run import execute_cancel


@click.command(name="cancel")
@click.argument("identifier")
@click.option("--reason", default="cli cancel", show_default=True)
@click.option(
    "--responder",
    default=None,
    help=(
        "Operator identity for the cancel; also stamped onto the synthetic "
        "reject so an identity-pinned gate accepts it. Falls back to "
        "$FORGE_RESPONDER, the gate's expected approver, then the OS user — "
        "never requires a controlling terminal."
    ),
)
@click.option(
    "--db",
    "db_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="forge.db path; defaults to $FORGE_DB_PATH then ~/.forge/forge.db.",
)
@click.pass_context
def cancel_cmd(
    ctx: click.Context,
    identifier: str,
    reason: str,
    responder: str | None,
    db_path: Path | None,
) -> None:
    """Cancel an active or recent build for ``identifier``."""
    execute_cancel(
        ctx=ctx,
        identifier=identifier,
        reason=reason,
        responder=responder,
        db_path=db_path,
    )


__all__ = ["cancel_cmd"]

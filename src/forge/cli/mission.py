"""``forge mission`` — list missions, and ask for one to stop (18 October 2026).

Missions design, section 11. ``forge mission stop m-xxxxxx`` records a stop
request through the same function a Slack ``stop mission m-xxxxxx`` calls
(:func:`forge.planning.missions_flow.stop_mission`); the running coordinator
carries it out on its next pass and says so in the mission's thread. The
command line is the operator's route, so it is not limited to the person who
sent the mission. ``forge mission list`` shows every mission, newest first.

Inside the coordinator's container the ledger is found the way ``forge
cancel`` finds it (``$FORGE_DB_PATH``), so ``docker exec
forge-estate-coordinator-1 forge mission stop m-xxxxxx`` needs no flags.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from forge.cli._db_resolve import resolve_db_path
from forge.cli._responder import resolve_responder


def _missions(db_path: Path | None):
    from forge.adapters.sqlite.connect import connect_writer
    from forge.lifecycle.missions import MissionStore

    path = resolve_db_path(db_path)
    if not path.exists():
        click.echo(f"forge mission: no ledger at {path}", err=True)
        sys.exit(2)
    return MissionStore(connect_writer(path))


@click.group(name="mission")
def mission_cmd() -> None:
    """List missions, and ask for one to stop."""


@mission_cmd.command(name="stop")
@click.argument("mission_id")
@click.option("--responder", default=None,
              help="Who is asking; falls back to $FORGE_RESPONDER then the OS user.")
@click.option("--db", "db_path", default=None,
              type=click.Path(path_type=Path, dir_okay=False),
              help="Path to forge.db; defaults to $FORGE_DB_PATH.")
def stop_cmd(mission_id: str, responder: str | None, db_path: Path | None) -> None:
    """Ask for MISSION_ID to stop: nothing new starts, running work finishes unmerged."""
    from forge.planning.missions_flow import stop_mission

    answer = stop_mission(
        _missions(db_path), mission_id, requested_by=resolve_responder(responder)
    )
    click.echo(answer.sentence)
    if not answer.accepted:
        sys.exit(3)


@mission_cmd.command(name="list")
@click.option("--db", "db_path", default=None,
              type=click.Path(path_type=Path, dir_okay=False),
              help="Path to forge.db; defaults to $FORGE_DB_PATH.")
def list_cmd(db_path: Path | None) -> None:
    """Every mission, newest first: id, state, title, and any stop asked for."""
    from forge.lifecycle.missions import MISSION_STATUSES

    store = _missions(db_path)
    missions = list(reversed(store.list_by_status(MISSION_STATUSES)))
    if not missions:
        click.echo("No missions.")
        return
    for mission in missions:
        line = f"{mission.id}  {mission.status:<11} {mission.title}"
        if mission.stopping and not mission.closed_at:
            line += f"  (stopping: asked by {mission.stop_requested_by} at {mission.stop_requested_at})"
        elif mission.closed_reason:
            line += f"  ({mission.closed_reason})"
        click.echo(line)


__all__ = ["mission_cmd"]

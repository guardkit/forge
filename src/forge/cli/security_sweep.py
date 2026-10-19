"""``forge security-sweep --once`` — run the nightly security sweep now, by hand.

Security check design, 17 October 2026, piece 3. The sweep normally runs
inside ``forge serve`` once a night. This command runs one sweep straight
away, with exactly the same code, settings, lock and Slack line, for the
release's live check and for whenever Rich wants a fresh look:

    docker exec <coordinator> forge security-sweep --once

It reads the settings the coordinator reads (``--config``, ``./forge.yaml``,
else ``$FORGE_CONFIG_PATH``) and the database ``$FORGE_DB_PATH`` names. It
refuses, in one sentence and without doing anything, when the sweep is
switched off, when the factory has work in flight, or when another sweep is
already running. The Slack line it posts is also printed here.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import click

from forge.cli._db_resolve import resolve_db_path

#: The exit code for "did not run" (switched off, busy, already running).
NOT_RUN_EXIT = 3


def _config(ctx: click.Context) -> Any:
    if ctx.obj is not None:
        return ctx.obj
    path = os.environ.get("FORGE_CONFIG_PATH")
    if path and Path(path).exists():
        from forge.config.loader import load_config

        return load_config(Path(path))
    raise click.UsageError(
        "forge security-sweep needs the coordinator's settings: pass --config, "
        "run where ./forge.yaml is, or set $FORGE_CONFIG_PATH."
    )


async def _connect(url: str) -> Any:
    """A one-shot connect that gives up within ten seconds: nats-py reads
    ``max_reconnect_attempts=0`` as "for ever" (19 October 2026)."""
    from forge.adapters.nats.connect_once import connect_once

    return await connect_once(url, timeout=10)


async def _run_once(config: Any, db_path: Path) -> int:
    from forge.security_sweep.schedule import compose_sweep, notification_publisher
    from forge.security_sweep.state import SweepAlreadyRunning
    from forge.security_sweep.sweep import run_sweep

    approver = getattr(config.approval, "expected_approver", None)
    client: Any = None
    url = os.environ.get("FORGE_NATS_URL", "nats://127.0.0.1:4222")
    try:
        client = await _connect(url)
    except Exception as exc:  # noqa: BLE001 — the run still happens; the line is printed
        click.echo(
            f"The bus could not be reached ({type(exc).__name__}), so the Slack line "
            "will only be printed here.",
            err=True,
        )
    sent: list[str] = []

    async def publish(text: str) -> None:
        sent.append(text)
        if client is not None:
            await notification_publisher(client, approver)(text)

    try:
        deps, is_idle = compose_sweep(config, db_path=db_path, publish=publish)
        if not is_idle():
            click.echo(
                "The factory has work in flight, so the sweep did not start. Run it "
                "again when nothing is building or being planned.",
                err=True,
            )
            return NOT_RUN_EXIT
        try:
            report = await run_sweep(deps)
        except SweepAlreadyRunning as exc:
            click.echo(str(exc), err=True)
            return NOT_RUN_EXIT
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass
    if report.line:
        click.echo(report.line)
    else:
        click.echo("Nightly security check: nothing new, so no Slack line was posted.")
    return 0 if report.outcome != "failed" else 1


@click.command(name="security-sweep")
@click.option(
    "--once",
    is_flag=True,
    default=False,
    help="Run one sweep now (the nightly one runs inside 'forge serve').",
)
@click.pass_context
def security_sweep_cmd(ctx: click.Context, once: bool) -> None:
    """Run the nightly security sweep once, now."""
    if not once:
        raise click.UsageError(
            "Say --once to run one sweep now. The nightly sweep runs inside "
            "'forge serve' when security_sweep.enabled is true."
        )
    config = _config(ctx)
    if not config.security_sweep.enabled:
        click.echo(
            "The security sweep is switched off (security_sweep.enabled is false in "
            "the settings), so nothing was run.",
            err=True,
        )
        ctx.exit(NOT_RUN_EXIT)
    code = asyncio.run(_run_once(config, resolve_db_path(None)))
    ctx.exit(code)


__all__ = ["NOT_RUN_EXIT", "security_sweep_cmd"]

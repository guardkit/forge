"""``forge cancel`` orchestration (O-02 / FWD-005).

Extracted from :mod:`forge.cli.cancel` so the ``forge cancel`` wrapper stays a
thin Click shell under its ceiling (TASK-PSM-011 AC-007), mirroring the
:mod:`forge.cli._cancel_gate_inject` extraction. This module owns the O-02 ops
hardening:

* **Canonical DB resolution** (:func:`forge.cli._db_resolve.resolve_db_path`):
  the same ledger ``forge serve`` booted against, so a stale mount can no longer
  shadow the live DB and make cancel a silent no-op (break #1).
* **Loud failures**: a missing ledger or a no-such-run identifier both exit
  non-zero with the *resolved DB path* named, instead of no-op'ing (break #1).
* **Terminal-independent responder** (:func:`forge.cli._responder.resolve_responder`):
  ``--responder`` → ``$FORGE_RESPONDER`` → gate approver → OS user, never an
  ``os.getlogin()`` ``OSError`` under ``docker exec`` (break #2).
* **Identity-pinned paused cancel**: the synthetic reject carries the pinned
  responder (default = the gate's ``expected_approver``) so an identity-pinned
  approval gate accepts it instead of rejecting the old hardcoded ``"rich"``
  (break #3).
* **A cancel the card will accept, confirmed** (12 October 2026): on the
  paused path the approver comes from the coordinator's settings file
  (``--config`` / ./forge.yaml, else ``$FORGE_CONFIG_PATH``), never from the
  login name; a rejection the card would refuse is not sent, and success is
  reported only after the row reads CANCELLED.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any, NoReturn

import click

from forge.cli._cancel_gate_inject import (
    PausedCardChanged,
    pending_gate_request,
    send_paused_cancel,
)
from forge.cli._recorded_run import interrupt_recorded_run
from forge.cli._db_resolve import resolve_db_path
from forge.cli._responder import (
    config_expected_approver,
    resolve_gate_responder,
    resolve_responder,
)
from forge.cli.runtime import build_cli_runtime
from forge.gating.identity import is_accepted_approver
from forge.lifecycle.state_machine import TERMINAL_STATES, BuildState

__all__ = ["execute_cancel"]

#: How long a cancel sent to a card waits to read CANCELLED back.
CONFIRM_SECONDS: float = 30.0
#: How often it re-reads the row while it waits.
POLL_SECONDS: float = 0.5


def _selected_config(ctx: Any) -> Any:
    """The configuration this command was given (``--config`` / ./forge.yaml),
    else the one ``FORGE_CONFIG_PATH`` names; ``None`` when there is none.

    A file ``FORGE_CONFIG_PATH`` names that cannot be loaded stops the command
    with one sentence and exit 2, before anything is sent or cancelled."""
    if getattr(ctx, "obj", None) is not None:
        return ctx.obj
    path = os.environ.get("FORGE_CONFIG_PATH")
    if not path or not Path(path).exists():
        return None
    from forge.config.loader import load_config

    try:
        return load_config(Path(path))
    except Exception as exc:  # noqa: BLE001 — any load failure is said plainly
        detail = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
        _refuse(
            f"the settings file $FORGE_CONFIG_PATH names ({path}) could not be "
            f"loaded: {detail}. Fix it or pass --config. Nothing was cancelled."
        )


def execute_cancel(
    *,
    ctx: Any,
    identifier: str,
    reason: str,
    responder: str | None,
    db_path: Path | None,
    confirm_seconds: float = CONFIRM_SECONDS,
    poll_seconds: float = POLL_SECONDS,
) -> None:
    """Resolve, then cancel, an active-or-recent build for ``identifier``.

    Args:
        ctx: The Click context; its ``obj`` (a ``ForgeConfig`` when ``forge.yaml``
            was loaded) supplies the gate's pinned ``expected_approver`` default.
        identifier: ``feature_id`` or ``build_id`` to cancel.
        reason: Free-text reason recorded on the cancel.
        responder: Explicit ``--responder`` identity, or ``None`` to resolve it.
        db_path: Explicit ``--db`` path, or ``None`` to resolve canonically.
        confirm_seconds: How long a cancel sent to a card waits to read
            CANCELLED back before saying the card did not accept it.
        poll_seconds: How often it re-reads the row while it waits.

    Exits the process (``sys.exit(2)``) — loudly, naming the resolved DB — when
    the ledger is missing or carries no such run.
    """
    resolved_db = resolve_db_path(db_path)
    if not resolved_db.exists():
        click.echo(
            f"forge cancel: no forge.db at {resolved_db} — set $FORGE_DB_PATH "
            "or pass --db pointing at the live ledger (nothing cancelled).",
            err=True,
        )
        sys.exit(2)

    runtime = build_cli_runtime(resolved_db)
    # A build id selects that exact row even if its feature has a newer run;
    # only an exact miss falls back to the established feature lookup.
    build = runtime.persistence.get_build_row(identifier)
    if build is None:
        build = runtime.persistence.find_active_or_recent(identifier)
    if build is None:
        # Loud, DB-named failure — NOT a silent no-op. A stale/wrong ledger is
        # the usual cause, so surfacing the resolved path is the fix's whole
        # point (O-02 break #1).
        click.echo(
            f"forge cancel: no active or recent build for {identifier!r} in "
            f"{resolved_db} (nothing cancelled — is this the live ledger?).",
            err=True,
        )
        sys.exit(2)

    # PAUSED-at-gate → the daemon's live gate frame owns the CANCELLED
    # transition via a synthetic reject (§D6), signed by the approver the
    # card is pinned to (O-02 break #3).
    if build.status is BuildState.PAUSED and _cancel_at_card(
        runtime,
        ctx=ctx,
        build_id=build.build_id,
        reason=reason,
        responder=responder,
        confirm_seconds=confirm_seconds,
        poll_seconds=poll_seconds,
    ):
        return

    # A build that may be running: send its recorded run the ordinary
    # interrupt on the runner it was launched on (its repository's sandbox
    # runner from the configuration this command was given, else the global
    # runner); the runner's own cancel handler stops everything it owns.
    # Best effort — the cancel itself proceeds either way.
    if build.status not in TERMINAL_STATES and not _interrupt_holding_the_sandbox(
        runtime, _selected_config(ctx), build.build_id
    ):
        click.echo(
            f"forge cancel: {build.build_id} — could not reach its runner; "
            "nothing was cancelled — try again.",
            err=True,
        )
        sys.exit(2)

    outcome = runtime.cli_steering_handler.handle_cancel(
        build_id=build.build_id,
        reason=reason,
        responder=resolve_responder(responder),
    )
    click.echo(f"Cancelled {build.build_id}: {outcome.status.value}")
    click.echo(outcome.rationale)


def _interrupt_holding_the_sandbox(runtime: Any, config: Any, build_id: str) -> bool:
    """Interrupt the recorded run, holding its sandbox open meanwhile.

    One sandbox per project, stage S2 (design 2.3a): a sandbox the starter
    manages may be stopped while idle, and this command runs outside the
    coordinator, so it holds the sandbox open at the starter (``until=
    services``) while it reaches the runner. A sandbox with an unfinished
    build is never stopped, so this only matters after a restart; a hold that
    cannot be taken refuses the cancel in one sentence. Any other sandbox:
    unchanged.
    """
    from forge.sandbox_gate.holds import HoldRefused, hold_for_config

    connection = getattr(runtime.persistence, "connection", None)
    try:
        row = connection.execute(
            "SELECT repo FROM builds WHERE build_id = ?", (build_id,)
        ).fetchone() if connection is not None else None
        repo = str(row[0]) if row is not None and row[0] else None
    except Exception:  # noqa: BLE001 — the interrupt reads it again itself
        repo = None
    try:
        with hold_for_config(
            config,
            repo,
            command="forge cancel",
            until="services",
            connection=connection,
        ):
            return asyncio.run(
                interrupt_recorded_run(runtime.persistence, config, build_id)
            )
    except HoldRefused as exc:
        _refuse(f"{build_id} — {exc}")


def _refuse(message: str) -> NoReturn:
    click.echo(f"forge cancel: {message}", err=True)
    sys.exit(2)


def _cancel_at_card(
    runtime: Any,
    *,
    ctx: Any,
    build_id: str,
    reason: str,
    responder: str | None,
    confirm_seconds: float,
    poll_seconds: float,
) -> bool:
    """Cancel a build waiting at its card by answering the card; confirm it.

    Returns ``False`` only when there is no row, or it carries no pending
    request id or a legacy one, so the caller keeps today's direct cancel. Otherwise it
    returns ``True`` once the row reads CANCELLED, or exits non-zero. It
    never falls back to the direct cancel once the card is known: that would
    mark the build cancelled while the gate is still waiting.
    """
    try:
        request = pending_gate_request(runtime, build_id=build_id)
    except PausedCardChanged as exc:
        _refuse(f"{build_id} — {exc} Nothing was sent; check the build and try again.")
    if request is None:
        return False

    config = _selected_config(ctx)
    approver = config_expected_approver(config)
    additional = getattr(getattr(config, "approval", None), "additional_approvers", ())
    signer = resolve_gate_responder(responder, approver=approver)
    if signer is None:
        _refuse(
            f"{build_id} is waiting at its card, and this command cannot tell "
            "who may answer it: there is no --responder, no $FORGE_RESPONDER "
            "and no approver in a settings file. Run it with the coordinator's "
            "settings file (forge --config /etc/forge/forge.yaml cancel ..., or "
            "set $FORGE_CONFIG_PATH). Nothing was sent."
        )
    if approver is not None and not is_accepted_approver(signer, approver, additional):
        _refuse(
            f"{build_id} — its card accepts an answer only from {approver!r}, "
            f"and this cancel would be signed {signer!r}, so the card would "
            "refuse it. Nothing was sent."
        )

    send_paused_cancel(
        build_id=build_id, request=request, reason=reason, responder=signer
    )
    status = _status_after_cancel(runtime, build_id, confirm_seconds, poll_seconds)
    if status is BuildState.CANCELLED:
        click.echo(
            f"forge cancel: {build_id} — cancelled (the card accepted the "
            f"rejection signed {signer!r})."
        )
        return True
    if status is BuildState.PAUSED:
        _refuse(
            f"{build_id} — the card did not accept the rejection signed "
            f"{signer!r}; after {confirm_seconds:g} seconds the build is still "
            "waiting at its card."
        )
    _refuse(
        f"{build_id} was not cancelled by this command: it is now "
        f"{status.value if status is not None else 'missing'} "
        "(the card was answered first)."
    )


def _status_after_cancel(
    runtime: Any, build_id: str, confirm_seconds: float, poll_seconds: float
) -> BuildState | None:
    """Re-read the row until it leaves PAUSED or ``confirm_seconds`` pass."""
    deadline = time.monotonic() + confirm_seconds
    while True:
        row = runtime.persistence.get_build_row(build_id)
        status = row.status if row is not None else None
        if status is not BuildState.PAUSED or time.monotonic() >= deadline:
            return status
        time.sleep(poll_seconds)

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
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import click

from forge.cli._cancel_gate_inject import try_inject_paused_cancel
from forge.cli._db_resolve import resolve_db_path
from forge.cli._responder import config_expected_approver, resolve_responder
from forge.cli.runtime import build_cli_runtime
from forge.lifecycle.state_machine import BuildState

__all__ = ["execute_cancel"]


def execute_cancel(
    *,
    ctx: Any,
    identifier: str,
    reason: str,
    responder: str | None,
    db_path: Path | None,
) -> None:
    """Resolve, then cancel, an active-or-recent build for ``identifier``.

    Args:
        ctx: The Click context; its ``obj`` (a ``ForgeConfig`` when ``forge.yaml``
            was loaded) supplies the gate's pinned ``expected_approver`` default.
        identifier: ``feature_id`` or ``build_id`` to cancel.
        reason: Free-text reason recorded on the cancel.
        responder: Explicit ``--responder`` identity, or ``None`` to resolve it.
        db_path: Explicit ``--db`` path, or ``None`` to resolve canonically.

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
    # transition via a synthetic reject (§D6). The reject is stamped with the
    # pinned responder (default = the gate's expected_approver) so an
    # identity-pinned gate accepts it (O-02 break #3).
    pinned = config_expected_approver(ctx)
    if build.status is BuildState.PAUSED and try_inject_paused_cancel(
        runtime,
        build_id=build.build_id,
        reason=reason,
        responder=resolve_responder(responder, pinned=pinned),
    ):
        click.echo(f"forge cancel: {build.build_id} — synthetic reject injected.")
        return

    outcome = runtime.cli_steering_handler.handle_cancel(
        build_id=build.build_id,
        reason=reason,
        responder=resolve_responder(responder),
    )
    click.echo(f"Cancelled {build.build_id}: {outcome.status.value}")
    click.echo(outcome.rationale)

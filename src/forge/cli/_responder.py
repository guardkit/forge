"""Responder-identity resolution for the CLI steering commands (O-02 / FWD-005).

``forge cancel`` / ``forge skip`` must never depend on a controlling terminal to
name the operator: ``os.getlogin()`` raises ``OSError`` (errno 6, "no such
device or address") under ``docker exec`` — there is no ``utmp``/``loginuid``
entry, and ``-t`` does not help. That crash made the ops safety-valve for a
runaway build unusable inside forge-prod (TASK-FWD-005, break #2).

This module resolves the responder identity from, in order:

1. an explicit ``--responder`` flag,
2. ``$FORGE_RESPONDER`` (an operator-supplied env override — the natural
   ``docker exec -e FORGE_RESPONDER=... forge cancel`` path),
3. a caller-supplied ``pinned`` default — on the paused-gate path this is the
   gate's ``expected_approver`` (``forge.yaml`` ``approval.expected_approver``),
   so the synthetic reject the injector publishes carries the identity the
   identity-pinned approval gate accepts instead of the old hardcoded
   ``"rich"`` it rejected (TASK-FWD-005, break #3),
4. ``$USER`` / ``$LOGNAME`` — the ambient login name,
5. ``getpass.getuser()`` — a pwd/env lookup that does NOT need a tty,
6. ``os.getlogin()`` — last, and guarded, because it is the one that crashes
   headless,
7. :data:`UNKNOWN_RESPONDER` — a last-resort sentinel so the command still
   completes (loudly logged) rather than raising.
"""

from __future__ import annotations

import getpass
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

#: Env var an operator sets to name the cancel/skip responder without a
#: controlling terminal (e.g. ``docker exec -e FORGE_RESPONDER=U0... forge
#: cancel ...``). Highest priority after the explicit ``--responder`` flag.
RESPONDER_ENV_VAR: str = "FORGE_RESPONDER"

#: Last-resort identity when every lookup fails (no flag, no env, no pinned
#: approver, no pwd entry, no controlling terminal). Recorded on the audit
#: trail with a WARNING rather than crashing the command.
UNKNOWN_RESPONDER: str = "unknown"

__all__ = [
    "RESPONDER_ENV_VAR",
    "UNKNOWN_RESPONDER",
    "config_expected_approver",
    "resolve_responder",
]


def _first_nonempty(*candidates: str | None) -> str | None:
    """Return the first candidate that is a non-blank string, else ``None``."""
    for candidate in candidates:
        if candidate and candidate.strip():
            return candidate
    return None


def resolve_responder(explicit: str | None = None, *, pinned: str | None = None) -> str:
    """Resolve the cancel/skip responder identity without needing a tty.

    See the module docstring for the full resolution order. ``pinned`` is the
    paused-path lever: pass the gate's ``expected_approver`` so the synthetic
    reject is accepted by an identity-pinned approval gate; pass ``None`` on the
    non-paused (direct ``handle_cancel``) path where the responder is an audit
    field only.

    This function never raises: the terminal-dependent ``os.getlogin()`` is
    tried last and inside a guard, and a total failure yields
    :data:`UNKNOWN_RESPONDER`.
    """
    resolved = _first_nonempty(
        explicit,
        os.environ.get(RESPONDER_ENV_VAR),
        pinned,
        os.environ.get("USER"),
        os.environ.get("LOGNAME"),
    )
    if resolved is not None:
        return resolved

    try:
        user = getpass.getuser()
        if user and user.strip():
            return user
    except Exception:  # noqa: BLE001 — getuser() can raise KeyError/OSError headless
        pass

    try:
        return os.getlogin()
    except OSError:
        logger.warning(
            "forge cancel/skip: could not resolve a responder identity "
            "(no --responder, no $%s, no pinned approver, no $USER/$LOGNAME, "
            "no pwd entry, no controlling terminal); recording %r",
            RESPONDER_ENV_VAR,
            UNKNOWN_RESPONDER,
        )
        return UNKNOWN_RESPONDER


def config_expected_approver(ctx: Any) -> str | None:
    """Best-effort read of ``forge.yaml`` ``approval.expected_approver``.

    The top-level ``forge`` group loads ``forge.yaml`` into ``ctx.obj`` when one
    is present (see :func:`forge.cli.main._resolve_context_object`). When it is a
    :class:`~forge.config.models.ForgeConfig` this returns its pinned approver so
    the paused-cancel path defaults the responder to the gate's expected
    identity. Returns ``None`` when no config is loaded (e.g. ``forge cancel``
    run from a directory without ``forge.yaml``, or invoked directly in tests) —
    the resolver then falls through to the env / OS-user chain, so cancel keeps
    working config-free.
    """
    obj = getattr(ctx, "obj", None)
    approval = getattr(obj, "approval", None)
    return getattr(approval, "expected_approver", None)

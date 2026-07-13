"""Canonical ``forge.db`` resolution for the ops CLIs (O-02 / FWD-005).

``forge cancel`` (and the other steering commands) MUST resolve the same SQLite
ledger the ``forge serve`` daemon booted against. Inside forge-prod a stale
``/var/forge/forge.db`` mount can shadow the live ledger, so a cancel given the
wrong ``--db`` path exits "no active or recent build" for builds that plainly
exist — a silent wrong-DB no-op (TASK-FWD-005, break #1).

The canonical source is the project-wide convention shared by ``forge serve``
(:class:`forge.cli._serve_config.ServeConfig`), ``forge queue`` and
``forge status``: ``$FORGE_DB_PATH`` → ``~/.forge/forge.db``. Resolving through
this helper — rather than *requiring* an explicit ``--db`` — means the natural
``docker exec forge-prod forge cancel <FEAT>`` hits the live ledger by default.
An explicit ``--db`` still wins for operators who mount the DB elsewhere.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Project-wide env var naming the SQLite ledger. Honoured identically by
#: ``forge serve`` / ``forge queue`` / ``forge status`` — never introduce a
#: parallel knob (TASK-REV-F010 D2.A*).
FORGE_DB_PATH_ENV: str = "FORGE_DB_PATH"

#: Operator-friendly local default when ``$FORGE_DB_PATH`` is unset. Matches
#: :data:`forge.cli._serve_config.DEFAULT_DB_PATH` and
#: :data:`forge.cli.queue.DEFAULT_DB_PATH` (ADR-ARCH-001).
DEFAULT_DB_PATH: Path = Path("~/.forge/forge.db")

__all__ = ["DEFAULT_DB_PATH", "FORGE_DB_PATH_ENV", "resolve_db_path"]


def resolve_db_path(explicit: Path | str | None = None) -> Path:
    """Resolve the canonical ``forge.db`` path.

    Resolution order (first wins): explicit ``--db`` → ``$FORGE_DB_PATH`` →
    :data:`DEFAULT_DB_PATH`. The ``~`` is expanded in every branch so the result
    is always an absolute-ish user path the caller can ``.exists()``-check and
    open. This intentionally does NOT create the file — a missing DB is a loud
    caller-side failure, not a fresh empty ledger.
    """
    if explicit is not None:
        return Path(explicit).expanduser()
    raw = os.environ.get(FORGE_DB_PATH_ENV)
    if raw and raw.strip():
        return Path(raw).expanduser()
    return DEFAULT_DB_PATH.expanduser()

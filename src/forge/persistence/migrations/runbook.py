"""``runbooks`` and ``runbook_steps`` table migration (TASK-RSP-002).

The runbook store tracks pipeline execution state for the forge system.
This migration creates two STRICT tables:

* ``runbooks`` — one row per pipeline execution.
* ``runbook_steps`` — one row per step within a pipeline.

Schema (AC-1, AC-2, AC-3, AC-4)
-------------------------------

**runbooks**:

* ``runbook_id TEXT PRIMARY KEY`` — unique identifier for each runbook.
* ``target TEXT NOT NULL`` — the target being built/processed.
* ``current_step_index INTEGER NOT NULL`` — index of the currently executing step.
* ``status TEXT NOT NULL`` — current status with CHECK constraint for:
  ``pending``, ``running``, ``passed``, ``failed``, ``awaiting_approval``.
* ``created_at TEXT NOT NULL`` — ISO-8601 timestamp of creation.

**runbook_steps**:

* ``runbook_id TEXT NOT NULL`` — FK to ``runbooks(runbook_id)`` with
  ``ON DELETE CASCADE``.
* ``sequence_index INTEGER NOT NULL`` — position of this step in the pipeline.
* ``step_type TEXT NOT NULL`` — type/name of the step.
* ``params TEXT NOT NULL DEFAULT '{}'`` — JSON-encoded parameters.
* ``status TEXT NOT NULL`` — step status with CHECK constraint (same set
  as runbooks.status).
* ``result TEXT`` — nullable JSON result (null until step completes).
* Composite ``PRIMARY KEY (runbook_id, sequence_index)``.

The migration is idempotent: ``CREATE TABLE IF NOT EXISTS`` guarantees
re-running the script against an already-migrated database is a no-op,
matching the contract of :func:`forge.lifecycle.migrations.apply_at_boot`.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Final

logger = logging.getLogger(__name__)


__all__ = [
    "CREATE_TABLES_SQL",
    "RunbookMigrationError",
    "apply",
]


CREATE_TABLES_SQL: Final[str] = """
CREATE TABLE IF NOT EXISTS runbooks (
    runbook_id          TEXT PRIMARY KEY,
    target              TEXT NOT NULL,
    current_step_index  INTEGER NOT NULL,
    status              TEXT NOT NULL CHECK (
        status IN ('pending','running','passed','failed','awaiting_approval')
    ),
    created_at          TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS runbook_steps (
    runbook_id      TEXT NOT NULL REFERENCES runbooks(runbook_id) ON DELETE CASCADE,
    sequence_index  INTEGER NOT NULL,
    step_type       TEXT NOT NULL,
    params          TEXT NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL CHECK (
        status IN ('pending','running','passed','failed','awaiting_approval')
    ),
    result          TEXT,
    PRIMARY KEY (runbook_id, sequence_index)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_runbook_steps_order
    ON runbook_steps (runbook_id, sequence_index);
"""


class RunbookMigrationError(RuntimeError):
    """Raised when the runbook migration fails to apply.

    Wraps the underlying ``sqlite3`` exception so callers can surface a
    domain-flavoured error without having to know the storage backend.
    """


def apply(connection: sqlite3.Connection) -> None:
    """Apply the ``runbooks`` and ``runbook_steps`` table migration.

    Idempotent — safe to invoke on every boot. The DDL uses
    ``CREATE TABLE IF NOT EXISTS`` so a re-run against an already
    migrated database makes no changes.

    Args:
        connection: A writable ``sqlite3.Connection`` — typically the
            persistent connection produced by
            :func:`forge.adapters.sqlite.connect.connect_writer`.

    Raises:
        RunbookMigrationError: If the DDL script raises a
            ``sqlite3.Error``. The originating exception is preserved
            via ``__cause__`` for diagnostics.
    """
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError(
            "apply: connection must be a sqlite3.Connection; got "
            f"{type(connection).__name__}"
        )

    try:
        with connection:  # commit on success; rollback on any raise.
            connection.executescript(CREATE_TABLES_SQL)
    except sqlite3.Error as exc:
        raise RunbookMigrationError(
            f"failed to apply runbooks/runbook_steps migration: {exc}"
        ) from exc

    logger.debug("applied runbooks and runbook_steps migration")

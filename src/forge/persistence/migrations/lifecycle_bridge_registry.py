"""``lifecycle_bridge_registry`` table migration (TASK-FRR-PEB-002).

The lifecycle bridge keeps an in-flight registry of every active
build's SSE attachment metadata so ``forge serve`` can recover
mid-flight after a crash and ``forge status --in-flight`` can list the
running builds without contacting the langgraph-runner sidecar.

Schema (AC-2)
-------------

* ``feature_id TEXT PRIMARY KEY`` — one row per active build.
* ``thread_id TEXT NOT NULL`` — DeepAgents thread id.
* ``run_id TEXT NOT NULL`` — LangGraph run id.
* ``correlation_id TEXT NOT NULL`` — F010C correlation-id contract.
* ``last_event_id TEXT`` — last SSE ``id:`` we acked; nullable until
  the first event arrives.
* ``ack_handle_token TEXT NOT NULL`` — opaque token mapped back to the
  in-memory ack callback by the consumer (T1). Keeping the token-based
  indirection avoids serialising un-pickleable async callbacks.
* ``deadline_at TEXT NOT NULL`` — 300s per-build deadline (ASSUM-003);
  T8 reads this column for deadline enforcement.
* ``attached_at TEXT NOT NULL`` — wall-clock attach time.
* ``current_lifecycle TEXT NOT NULL`` — coarse state: ``"queued"``,
  ``"running"``, ``"paused"``. Typed lifecycle states arrive in T3 with
  the SSE translation layer.
* ``updated_at TEXT NOT NULL`` — wall-clock last update.

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
    "CREATE_TABLE_SQL",
    "PUBLISHED_LIFECYCLES_COLUMN",
    "TABLE_NAME",
    "BridgeRegistryMigrationError",
    "apply",
]


TABLE_NAME: Final[str] = "lifecycle_bridge_registry"

#: Name of the JSON-encoded column tracking the lifecycle subjects
#: already published for each in-flight build (TASK-FRR-PEB-009 AC-2).
#: Stored as TEXT containing a JSON-encoded list of strings (e.g.
#: ``'["build-started"]'``). The publisher path appends to this set
#: BEFORE invoking the actual NATS publish so a daemon-restart recovery
#: never re-publishes a transition that was already on the wire.
PUBLISHED_LIFECYCLES_COLUMN: Final[str] = "published_lifecycles"


CREATE_TABLE_SQL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    feature_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    last_event_id TEXT,
    ack_handle_token TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    attached_at TEXT NOT NULL,
    current_lifecycle TEXT NOT NULL CHECK (
        current_lifecycle IN ('queued', 'running', 'paused')
    ),
    updated_at TEXT NOT NULL,
    {PUBLISHED_LIFECYCLES_COLUMN} TEXT NOT NULL DEFAULT '[]'
) STRICT;

CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_lifecycle
    ON {TABLE_NAME} (current_lifecycle, updated_at);

CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_deadline
    ON {TABLE_NAME} (deadline_at);
"""


class BridgeRegistryMigrationError(RuntimeError):
    """Raised when the lifecycle_bridge_registry migration fails to apply.

    Wraps the underlying ``sqlite3`` exception so callers can surface a
    domain-flavoured error without having to know the storage backend.
    """


def apply(connection: sqlite3.Connection) -> None:
    """Apply the ``lifecycle_bridge_registry`` table migration.

    Idempotent — safe to invoke on every boot. The DDL uses
    ``CREATE TABLE IF NOT EXISTS`` so a re-run against an already
    migrated database makes no changes.

    Args:
        connection: A writable ``sqlite3.Connection`` — typically the
            persistent connection produced by
            :func:`forge.adapters.sqlite.connect.connect_writer`.

    Raises:
        BridgeRegistryMigrationError: If the DDL script raises a
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
            connection.executescript(CREATE_TABLE_SQL)
    except sqlite3.Error as exc:
        raise BridgeRegistryMigrationError(
            f"failed to apply {TABLE_NAME!r} migration: {exc}"
        ) from exc

    # Apply the ``published_lifecycles`` column migration as a
    # follow-up so legacy installs (created before TASK-FRR-PEB-009)
    # pick up the recovery-cursor column on first boot under T9. The
    # follow-up is idempotent; fresh installs short-circuit because
    # the column already exists from the ``CREATE TABLE`` above.
    # Imported lazily to avoid a circular import at module load time
    # (the published_lifecycles module imports TABLE_NAME from here).
    from forge.persistence.migrations import (
        lifecycle_bridge_published_lifecycles as published_lifecycles_migration,
    )

    published_lifecycles_migration.apply(connection)

    logger.debug("applied %s migration", TABLE_NAME)

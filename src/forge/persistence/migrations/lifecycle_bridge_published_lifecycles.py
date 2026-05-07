"""``published_lifecycles`` column migration (TASK-FRR-PEB-009 AC-2).

The lifecycle bridge tracks which envelope subjects have been published
for each in-flight build via the ``published_lifecycles`` column on
``lifecycle_bridge_registry`` (added in T9). The column carries a
JSON-encoded list of subject strings (e.g. ``'["build-started"]'``);
the recovery sweep consults the set BEFORE re-publishing a replayed
envelope so a daemon-restart never re-emits a transition that was
already on the wire (the AC-5 regression scenario:
``build-started`` is not re-published).

Why a dedicated migration module
--------------------------------

The T2 baseline migration in
:mod:`forge.persistence.migrations.lifecycle_bridge_registry` was
shipped without this column. Two install bases exist on the same
schema-version line:

1. **Fresh installs**: the table is created with the column from the
   start (the ``CREATE TABLE`` DDL in the T2 file was extended in T9
   to include the new column inline).
2. **Existing installs**: the table predates the column and needs an
   ``ALTER TABLE ADD COLUMN`` to land it.

Splitting the ``ALTER TABLE`` into its own migration module makes the
upgrade story explicit:

* The T2 migration is responsible for the table shape.
* This T9 migration is responsible for the recovery cursor column.

Both are idempotent — :func:`apply` can be run on every boot. The T2
migration delegates to :func:`apply` so callers that only invoke the
T2 entry point still pick up the new column.

The migration is intentionally a SQLite-only DDL helper — there is no
backing service or transport. Wrapping it in a
:class:`PublishedLifecyclesMigrationError` keeps the failure shape
aligned with :class:`forge.persistence.migrations.lifecycle_bridge_registry.BridgeRegistryMigrationError`.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Final

from forge.persistence.migrations.lifecycle_bridge_registry import (
    PUBLISHED_LIFECYCLES_COLUMN,
    TABLE_NAME,
)

logger = logging.getLogger(__name__)


__all__ = [
    "ALTER_TABLE_SQL",
    "COLUMN_NAME",
    "TABLE_NAME",
    "PublishedLifecyclesMigrationError",
    "apply",
    "column_exists",
]


#: Re-exported for callers that want to address the column by name
#: without having to know the lower-level table-info layout.
COLUMN_NAME: Final[str] = PUBLISHED_LIFECYCLES_COLUMN


#: DDL statement applied when the column is missing. ``DEFAULT '[]'``
#: yields a JSON-encoded empty list — :func:`forge.persistence.repositories.bridge_registry._decode_published_lifecycles`
#: deserialises it as an empty :class:`frozenset`.
ALTER_TABLE_SQL: Final[str] = (
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN "
    f"{COLUMN_NAME} TEXT NOT NULL DEFAULT '[]'"
)


class PublishedLifecyclesMigrationError(RuntimeError):
    """Raised when the ``published_lifecycles`` migration fails to apply.

    Wraps the underlying ``sqlite3.Error`` so callers can surface a
    domain-flavoured error without having to know the storage backend.
    The originating exception is preserved via ``__cause__`` for
    diagnostics.
    """


def column_exists(connection: sqlite3.Connection) -> bool:
    """Return ``True`` when the ``published_lifecycles`` column is present.

    Uses ``PRAGMA table_info`` to introspect the schema. The check is
    cheap (no full-table scan) so it is safe to invoke on every boot
    as the gate for the idempotent ``ALTER TABLE``.

    Args:
        connection: A readable ``sqlite3.Connection`` against the forge
            database.

    Returns:
        ``True`` if the column already exists, ``False`` otherwise.

    Raises:
        TypeError: If ``connection`` is not a :class:`sqlite3.Connection`.
        sqlite3.Error: If the ``PRAGMA`` itself fails — the underlying
            connection is unusable, surface the failure rather than
            silently returning ``False``.
    """
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError(
            "column_exists: connection must be a sqlite3.Connection; got "
            f"{type(connection).__name__}"
        )
    rows = connection.execute(f"PRAGMA table_info({TABLE_NAME})").fetchall()
    for row in rows:
        # PRAGMA table_info returns (cid, name, type, notnull, dflt_value, pk).
        if row[1] == COLUMN_NAME:
            return True
    return False


def apply(connection: sqlite3.Connection) -> bool:
    """Apply the ``published_lifecycles`` column migration.

    Idempotent — safe to invoke on every boot. The function checks for
    the column via :func:`column_exists` and only issues the ``ALTER
    TABLE`` when the column is missing. A re-run against a database
    that already has the column makes no DDL changes and returns
    ``False``.

    Args:
        connection: A writable ``sqlite3.Connection`` — typically the
            persistent connection produced by
            :func:`forge.adapters.sqlite.connect.connect_writer`. The
            same connection used by
            :func:`forge.persistence.migrations.lifecycle_bridge_registry.apply`.

    Returns:
        ``True`` when the ``ALTER TABLE`` was applied (column was
        missing), ``False`` when the column already existed (idempotent
        no-op).

    Raises:
        TypeError: If ``connection`` is not a :class:`sqlite3.Connection`.
        PublishedLifecyclesMigrationError: If the DDL raises a
            :class:`sqlite3.Error`. The originating exception is
            preserved via ``__cause__``.
    """
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError(
            "apply: connection must be a sqlite3.Connection; got "
            f"{type(connection).__name__}"
        )

    try:
        if column_exists(connection):
            logger.debug(
                "%s migration: column %s already present; no-op",
                TABLE_NAME,
                COLUMN_NAME,
            )
            return False
        # ALTER TABLE in SQLite is auto-committed when run outside of
        # an explicit transaction; we wrap in ``with connection`` so
        # the change rolls back if the surrounding migration fails for
        # an unrelated reason.
        with connection:
            connection.execute(ALTER_TABLE_SQL)
    except sqlite3.Error as exc:
        raise PublishedLifecyclesMigrationError(
            f"failed to apply {COLUMN_NAME!r} migration on "
            f"{TABLE_NAME!r}: {exc}"
        ) from exc

    logger.info(
        "applied %s migration: added %s column",
        TABLE_NAME,
        COLUMN_NAME,
    )
    return True

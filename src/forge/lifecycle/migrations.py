"""Boot-time migration runner for the Forge SQLite substrate.

Public surface
==============

- :func:`apply_at_boot` — execute every migration whose version exceeds
  the highest row currently in ``schema_version``.

Design notes (DDR-003 + TASK-PSM-002)
-------------------------------------

The schema is shipped as a real ``schema.sql`` file inside this
package — see :mod:`importlib.resources`. ``CREATE TABLE IF NOT EXISTS``
plus ``INSERT OR IGNORE INTO schema_version`` make the script safe to
re-run on every boot, which is what gives us the *idempotent*
acceptance criterion for free: running ``apply_at_boot`` against an
already-migrated database is a no-op (no extra rows, no schema drift).

The runner wraps the executescript in a single transaction. A failure
inside the script rolls back the whole boot — partial schema is the
worst possible recovery state, so we'd rather raise loudly and let the
caller surface the error.
"""

from __future__ import annotations

import sqlite3
from importlib.resources import files
from typing import Final


# The current schema version. Bumped to 3 in TASK-MP-002 to add
# planning_runs and planning_run_events tables; bumped to 4 in Lane B /
# Phase E1 to widen the planning_runs.state CHECK for the target-terminal
# chain (FEATURE_SPEC / FEATURE_PLAN / BUILD_QUEUED); bumped to 5 in
# TASK-UBS-002-integration to add the additive ``builds.profile`` column
# carrying the ``forge queue --profile`` selection to the daemon; bumped to
# 6 to add the additive ``builds.last_coach_score`` column so the UBS1C coach
# score survives the run for the ``min_coach_score`` budget floor. Future
# schema bumps should follow the same pattern: append a sibling
# ``schema_v{N}.sql`` and add a ``(N, "schema_v{N}.sql")`` entry to
# ``_MIGRATIONS`` in ascending order. The runner applies every entry whose
# version is greater than the current ``schema_version`` ledger row.
_SCHEMA_VERSION: Final[int] = 6
_MIGRATIONS: Final[tuple[tuple[int, str], ...]] = (
    (1, "schema.sql"),
    (2, "schema_v2.sql"),
    (3, "schema_v3.sql"),
    (4, "schema_v4.sql"),
    (5, "schema_v5.sql"),
    (6, "schema_v6.sql"),
)


class MigrationError(RuntimeError):
    """Raised when the boot-time migration fails to apply.

    Wraps the underlying ``sqlite3`` exception so callers can surface a
    domain-flavoured error without needing to know the exact storage
    backend.
    """


def _load_migration_sql(filename: str) -> str:
    """Return the bundled migration SQL text.

    Parameters
    ----------
    filename:
        Resource name relative to the ``forge.lifecycle`` package
        (e.g. ``"schema.sql"``).
    """
    resource = files("forge.lifecycle") / filename
    return resource.read_text(encoding="utf-8")


def _current_version(connection: sqlite3.Connection) -> int:
    """Return the highest applied schema version, or 0 if uninitialised.

    The lookup tolerates the very first boot — ``schema_version`` does
    not exist yet — and reports version 0 so the caller applies every
    migration in order.
    """
    try:
        row = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_version;"
        ).fetchone()
    except sqlite3.OperationalError:
        # ``schema_version`` does not exist on a brand-new DB.
        return 0
    if row is None:
        return 0
    return int(row[0])


def apply_at_boot(connection: sqlite3.Connection) -> int:
    """Apply every pending migration to ``connection``.

    The function is **idempotent**: running it twice against the same
    database leaves the schema unchanged because every DDL statement
    in the bundled ``schema.sql`` uses ``IF NOT EXISTS`` and the
    ``schema_version`` seed row uses ``INSERT OR IGNORE``.

    Parameters
    ----------
    connection:
        A writable ``sqlite3.Connection`` — typically the persistent
        connection returned by
        :func:`forge.adapters.sqlite.connect.connect_writer`.

    Returns
    -------
    int
        The schema version after the migrations have been applied
        (i.e. the highest version present in ``schema_version``).

    Raises
    ------
    MigrationError
        If any migration script raises a SQLite error. The originating
        exception is preserved as ``__cause__``.
    """
    starting_version = _current_version(connection)

    pending = [m for m in _MIGRATIONS if m[0] > starting_version]
    if not pending:
        # Already up to date — re-running schema.sql would still be a
        # no-op, but skipping it avoids the tiny cost on every boot.
        return starting_version

    try:
        with connection:  # transaction: commit on success, rollback on raise
            for _version, filename in pending:
                sql = _load_migration_sql(filename)
                connection.executescript(sql)
    except sqlite3.Error as exc:
        raise MigrationError(
            f"failed to apply migration {filename!r}: {exc}"
        ) from exc

    return _current_version(connection)


__all__ = [
    "MigrationError",
    "apply_at_boot",
]

"""Persistence layer for dispatch resolutions + sensitive-parameter scrub.

Implements the durable write path for
:class:`~forge.discovery.models.CapabilityResolution` records into a
SQLite-backed history store. The persistence boundary is also the
sensitive-parameter scrub boundary: callers pass a list of
:class:`DispatchParameter` records, and any parameter flagged
``sensitive=True`` has its ``value`` dropped before any row is written.

This design enforces the **schema-driven** scrub by construction —
sensitive values cannot leak into the database because there is no
write path for them. The non-sensitive *names* of sensitive parameters
are still recorded so the audit trail shows that dispatch carried
sensitive data, without revealing the values themselves.

Implements scenarios E.sensitive-parameter-hygiene and the
write-before-send invariant of D.write-before-send-invariant
(TASK-SAD-002).

Composition not editing
-----------------------
The persistence module deliberately writes to *sibling* tables —
``forge_capability_resolutions`` and ``forge_dispatch_parameters`` —
so FEAT-FORGE-001's existing schema is left untouched. The two
tables join the FEAT-FORGE-001 history via the resolution's
``build_id`` (correlation_id-equivalent) without taking a hard
foreign-key dependency on rows owned by another feature.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from forge.discovery.models import CapabilityResolution

_RESOLUTION_TABLE = "forge_capability_resolutions"
_PARAMETER_TABLE = "forge_dispatch_parameters"


_RESOLUTION_DDL = f"""
CREATE TABLE IF NOT EXISTS {_RESOLUTION_TABLE} (
    resolution_id TEXT PRIMARY KEY,
    build_id TEXT NOT NULL,
    stage_label TEXT NOT NULL,
    requested_tool TEXT NOT NULL,
    requested_intent TEXT,
    matched_agent_id TEXT,
    match_source TEXT NOT NULL,
    competing_agents TEXT NOT NULL,
    chosen_trust_tier TEXT,
    chosen_confidence REAL,
    chosen_queue_depth INTEGER,
    resolved_at TEXT NOT NULL,
    outcome_correlated INTEGER NOT NULL,
    retry_of TEXT
)
"""

_PARAMETER_DDL = f"""
CREATE TABLE IF NOT EXISTS {_PARAMETER_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resolution_id TEXT NOT NULL,
    name TEXT NOT NULL,
    value TEXT,
    sensitive INTEGER NOT NULL,
    FOREIGN KEY (resolution_id) REFERENCES forge_capability_resolutions(resolution_id)
)
"""


class DispatchParameter(BaseModel):
    """One dispatch parameter, classified by sensitivity at construction.

    The ``sensitive`` flag is the persistence layer's scrub signal: when
    ``True``, the parameter's ``value`` is **not** persisted. The
    ``name`` is always recorded so audit trails reveal *what kinds* of
    sensitive data flowed through dispatch without disclosing the
    values themselves.

    Attributes:
        name: Parameter name as known to the dispatch tool.
        value: Parameter value. Required at construction time so the
            caller cannot accidentally pass ``None`` and bypass the
            scrub. The persistence layer drops the value before writing
            when ``sensitive=True``.
        sensitive: Drives the scrub. Default ``False`` (parameters are
            non-sensitive unless explicitly marked).
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, description="Parameter name")
    value: str = Field(description="Parameter value (scrubbed if sensitive=True)")
    sensitive: bool = Field(
        default=False,
        description="Drop value at persistence boundary when True",
    )


class SqliteHistoryWriter:
    """Composition wrapper around a SQLite connection for dispatch persistence.

    Owns idempotent table creation (``CREATE TABLE IF NOT EXISTS``) and
    exposes a :meth:`transaction` context manager so callers can group
    the resolution + parameter inserts into a single atomic write.

    The class is intentionally additive: it does not edit
    FEAT-FORGE-001 internals but writes to sibling tables
    (``forge_capability_resolutions`` and ``forge_dispatch_parameters``)
    that join the FEAT-FORGE-001 history via the resolution's
    ``build_id``.

    Args:
        connection: An open :class:`sqlite3.Connection`. The writer
            takes ownership of schema initialisation but not of the
            connection's lifetime — callers should close it.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._connection.execute("PRAGMA foreign_keys = ON")
        if self._connection.row_factory is None:
            self._connection.row_factory = sqlite3.Row
        self._initialise_schema()

    @classmethod
    def from_path(cls, path: str | Path) -> SqliteHistoryWriter:
        """Open a SQLite database at ``path`` (creating it if missing).

        The returned writer owns a fresh connection with
        :class:`sqlite3.Row` row factory and foreign-key enforcement
        enabled.
        """
        connection = sqlite3.connect(str(path))
        connection.row_factory = sqlite3.Row
        return cls(connection)

    @classmethod
    def in_memory(cls) -> SqliteHistoryWriter:
        """Open an in-memory SQLite database (primarily for tests)."""
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        return cls(connection)

    @property
    def connection(self) -> sqlite3.Connection:
        """The underlying :class:`sqlite3.Connection`."""
        return self._connection

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._connection.close()

    def _initialise_schema(self) -> None:
        """Create the two persistence tables if they do not already exist."""
        with self._connection:
            self._connection.execute(_RESOLUTION_DDL)
            self._connection.execute(_PARAMETER_DDL)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Atomic write context — commits on success, rolls back on raise.

        :class:`sqlite3.Connection` already implements transactional
        semantics via its ``with`` block (commit on clean exit,
        rollback on exception). We expose it explicitly so callers
        cannot forget to wrap related writes into a single unit.
        """
        with self._connection:
            yield self._connection

    def insert_resolution(self, resolution: CapabilityResolution) -> None:
        """Insert one :class:`CapabilityResolution` row.

        The ``competing_agents`` list is JSON-encoded into a single
        TEXT column. ``retry_of`` (TASK-SAD-001) is stored as a
        nullable string so a round-trip preserves the field.
        """
        self._connection.execute(
            f"""
            INSERT INTO {_RESOLUTION_TABLE} (
                resolution_id, build_id, stage_label, requested_tool,
                requested_intent, matched_agent_id, match_source,
                competing_agents, chosen_trust_tier, chosen_confidence,
                chosen_queue_depth, resolved_at, outcome_correlated, retry_of
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                resolution.resolution_id,
                resolution.build_id,
                resolution.stage_label,
                resolution.requested_tool,
                resolution.requested_intent,
                resolution.matched_agent_id,
                resolution.match_source,
                json.dumps(resolution.competing_agents),
                resolution.chosen_trust_tier,
                resolution.chosen_confidence,
                resolution.chosen_queue_depth,
                resolution.resolved_at.isoformat(),
                int(resolution.outcome_correlated),
                resolution.retry_of,
            ),
        )

    def insert_parameter(
        self,
        resolution_id: str,
        *,
        name: str,
        value: str | None,
        sensitive: bool,
    ) -> None:
        """Insert one parameter row associated with ``resolution_id``.

        The caller is responsible for having already scrubbed
        ``value`` (passing ``None`` for sensitive parameters). The
        :func:`persist_resolution` entry-point is the only sanctioned
        public path because it cannot forget to scrub.
        """
        self._connection.execute(
            f"""
            INSERT INTO {_PARAMETER_TABLE} (
                resolution_id, name, value, sensitive
            ) VALUES (?, ?, ?, ?)
            """,
            (resolution_id, name, value, int(sensitive)),
        )

    def read_resolutions(self) -> list[CapabilityResolution]:
        """Hydrate every persisted resolution back into Pydantic models.

        Provided primarily for round-trip tests + the pipeline-history
        view. Returns the rows ordered by ``resolution_id`` for
        deterministic test output.
        """
        cursor = self._connection.execute(
            f"SELECT * FROM {_RESOLUTION_TABLE} ORDER BY resolution_id",
        )
        return [_row_to_resolution(row) for row in cursor.fetchall()]

    def dump_all_parameter_rows(self) -> list[dict[str, object]]:
        """Return every parameter row as a plain ``dict``.

        Used by audit + security tests to verify that no sensitive
        ``value`` ever appears in the table — the scrub is verified
        against the raw rows, not against a pre-filtered view.
        """
        cursor = self._connection.execute(
            f"""
            SELECT resolution_id, name, value, sensitive
            FROM {_PARAMETER_TABLE}
            ORDER BY id
            """,
        )
        return [
            {
                "resolution_id": row["resolution_id"],
                "name": row["name"],
                "value": row["value"],
                "sensitive": bool(row["sensitive"]),
            }
            for row in cursor.fetchall()
        ]

    def read_pipeline_history_view(
        self, resolution_id: str
    ) -> dict[str, object] | None:
        """Render the pipeline-history view for one resolution.

        The view is the auditor-facing projection. It returns the
        resolution's non-sensitive fields plus a ``parameters`` list
        where:

        * Non-sensitive entries carry ``{"name": ..., "value": ...}``.
        * Sensitive entries carry ``{"name": ..., "sensitive": True}``
          — the *name* is preserved for audit, the value is **never**
          materialised.

        Returns ``None`` if no resolution with ``resolution_id``
        exists.
        """
        cursor = self._connection.execute(
            f"SELECT * FROM {_RESOLUTION_TABLE} WHERE resolution_id = ?",
            (resolution_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None

        params_cursor = self._connection.execute(
            f"""
            SELECT name, value, sensitive
            FROM {_PARAMETER_TABLE}
            WHERE resolution_id = ?
            ORDER BY id
            """,
            (resolution_id,),
        )
        parameters: list[dict[str, object]] = []
        for param_row in params_cursor.fetchall():
            if bool(param_row["sensitive"]):
                # Never include a value field — the projection has no
                # way to surface a scrubbed secret even by accident.
                parameters.append(
                    {"name": param_row["name"], "sensitive": True},
                )
            else:
                parameters.append(
                    {
                        "name": param_row["name"],
                        "value": param_row["value"],
                        "sensitive": False,
                    },
                )

        resolution = _row_to_resolution(row)
        return {
            "resolution": resolution.model_dump(mode="json"),
            "parameters": parameters,
        }


def _row_to_resolution(row: sqlite3.Row) -> CapabilityResolution:
    """Convert a SQLite row into a validated :class:`CapabilityResolution`."""
    return CapabilityResolution(
        resolution_id=row["resolution_id"],
        build_id=row["build_id"],
        stage_label=row["stage_label"],
        requested_tool=row["requested_tool"],
        requested_intent=row["requested_intent"],
        matched_agent_id=row["matched_agent_id"],
        match_source=row["match_source"],
        competing_agents=json.loads(row["competing_agents"]),
        chosen_trust_tier=row["chosen_trust_tier"],
        chosen_confidence=row["chosen_confidence"],
        chosen_queue_depth=row["chosen_queue_depth"],
        resolved_at=datetime.fromisoformat(row["resolved_at"]),
        outcome_correlated=bool(row["outcome_correlated"]),
        retry_of=row["retry_of"],
    )


def persist_resolution(
    resolution: CapabilityResolution,
    parameters: list[DispatchParameter],
    *,
    db_writer: SqliteHistoryWriter,
) -> None:
    """Atomically persist a resolution and its non-sensitive parameters.

    The scrub happens **inside** this function — callers pass the raw
    parameter list (sensitive and otherwise). For each parameter:

    * ``sensitive=False`` → row inserted with ``value`` populated.
    * ``sensitive=True`` → row inserted with ``value=NULL``; the
      ``name`` and ``sensitive=True`` flag are preserved for audit.

    Either the resolution row + every parameter row land together, or
    nothing lands — the inserts run inside a single
    :meth:`SqliteHistoryWriter.transaction` so a mid-write failure
    rolls back the whole batch.

    Do **not** add a "pre-scrubbed" overload of this function. Doing
    so re-introduces the "forget once" failure mode that this design
    is explicitly intended to make impossible (see TASK-SAD-002
    Implementation Notes).

    Args:
        resolution: The :class:`CapabilityResolution` to persist.
        parameters: List of :class:`DispatchParameter` records.
            Sensitive entries have their ``value`` dropped at the
            persistence boundary.
        db_writer: SQLite-backed history writer (composition seam over
            FEAT-FORGE-001's history store).

    Raises:
        TypeError: If ``resolution``, ``db_writer``, or any element of
            ``parameters`` has an unexpected type. We guard at the
            boundary so the function does not silently mis-persist
            duck-typed data.
        sqlite3.IntegrityError: If the same ``resolution_id`` is
            persisted twice (PRIMARY KEY violation). The transaction
            is rolled back so neither the resolution nor any
            parameters become visible.
        sqlite3.DatabaseError: For any other transactional failure.
            The transaction is rolled back so the database remains
            consistent.
    """
    if not isinstance(resolution, CapabilityResolution):
        raise TypeError(
            "resolution must be CapabilityResolution, got "
            f"{type(resolution).__name__}",
        )
    if not isinstance(db_writer, SqliteHistoryWriter):
        raise TypeError(
            "db_writer must be SqliteHistoryWriter, got "
            f"{type(db_writer).__name__}",
        )
    for index, parameter in enumerate(parameters):
        if not isinstance(parameter, DispatchParameter):
            raise TypeError(
                f"parameters[{index}] must be DispatchParameter, "
                f"got {type(parameter).__name__}",
            )

    with db_writer.transaction():
        db_writer.insert_resolution(resolution)
        for parameter in parameters:
            persisted_value = None if parameter.sensitive else parameter.value
            db_writer.insert_parameter(
                resolution.resolution_id,
                name=parameter.name,
                value=persisted_value,
                sensitive=parameter.sensitive,
            )


__all__ = [
    "DispatchParameter",
    "SqliteHistoryWriter",
    "persist_resolution",
]

"""F010F coexistence boundary — terminal-publish ledger (TASK-FRR-PEB-005).

The lifecycle bridge (TASK-FRR-PEB-002 .. TASK-FRR-PEB-004) introduces
an **async-terminal** publish path: the SSE translation layer observes
a terminal lifecycle event from ``langgraph-runner``, marks the build
"terminal-published" in the ledger, then invokes the inbound
``ack_callback`` so the JetStream slot is released.

F010F (TASK-FORGE-FRR-F010F) keeps owning the **sync-raise** safety-net
publish: when ``dispatch_build`` raises before the running state machine
takes ownership, the consumer publishes a ``pipeline.build-failed.{feature_id}``
envelope (DDR-029 correlation-id threading included) and acks. This
shape predates the bridge and is exercised live by every dispatch-error
path that never reaches a terminal state machine transition (Gap F010.B,
Gap F010.E reproduced 2026-05-04).

The two paths must coexist without ever publishing two terminal envelopes
for the same ``(feature_id, correlation_id)`` pair. This module owns the
**first-wins terminal-publish claim**:

* :class:`TerminalPublishLedger` — SQLite-backed ledger. ``claim`` is an
  atomic ``INSERT OR IGNORE`` against the
  ``lifecycle_bridge_terminal_publishes`` table. Whoever claims first
  may publish; every subsequent caller observes ``claim() -> False``
  and skips its emit.
* :func:`apply_migration` — idempotent ``CREATE TABLE IF NOT EXISTS``
  applied at boot by ``forge serve``. Co-located here so the migration
  travels with the consumer; T2 deliberately did not bake this column
  into ``lifecycle_bridge_registry`` because the registry rows are
  deleted on detach while the terminal-published claim must outlive the
  detach for the safety-net's check to be authoritative.

Concurrency model
-----------------

Both writers (the bridge's terminal-observation path and F010F's
safety-net path) call :meth:`TerminalPublishLedger.claim` against a
**single** writer connection (per ASSUM-011). SQLite serialises writers
through the database-level lock; the ``BEGIN IMMEDIATE`` + ``INSERT OR
IGNORE`` sequence inside :meth:`claim` is therefore atomic with respect
to any other writer on the same connection.

When the two paths fire concurrently from the same event loop (the only
realistic race in production: a delayed sync-raise hits while the bridge
is mid-ack), ``asyncio`` semantics guarantee one coroutine reaches
``claim`` before the other resumes — the loser sees ``False`` and stops.

References
----------
* TASK-FRR-PEB-005 — this task.
* TASK-FORGE-FRR-F010F — sync-raise safety-net publish (untouched).
* TASK-FRR-PEB-002 — bridge skeleton (creates the registry the ledger
  augments).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

logger = logging.getLogger(__name__)


__all__ = [
    "CLAIMER_BRIDGE_TERMINAL",
    "CLAIMER_F010F_SAFETY_NET",
    "CREATE_TABLE_SQL",
    "TABLE_NAME",
    "TerminalPublishClaim",
    "TerminalPublishLedger",
    "apply_migration",
]


#: SQLite table name. Co-located with the lifecycle bridge registry so
#: ``forge status --in-flight`` and the bridge wiring can share the same
#: writer connection (ASSUM-011 — one connection per daemon).
TABLE_NAME: Final[str] = "lifecycle_bridge_terminal_publishes"


#: Identifier the bridge passes to :meth:`TerminalPublishLedger.claim`
#: when the SSE terminal-observation path wins. Persisted on the row as
#: ``claimed_by`` so an operator inspecting the ledger after the fact
#: can tell which path emitted the terminal envelope.
CLAIMER_BRIDGE_TERMINAL: Final[str] = "bridge-terminal"


#: Identifier the F010F safety-net path passes when it wins the claim.
#: Mirrors ``CLAIMER_BRIDGE_TERMINAL`` so ledger rows are self-describing.
CLAIMER_F010F_SAFETY_NET: Final[str] = "f010f-safety-net"


#: DDL applied by :func:`apply_migration`. Composite primary key on
#: ``(feature_id, correlation_id)`` mirrors the
#: ``builds`` table's unique index (ASSUM-014) so the same identity used
#: by ``is_duplicate_terminal`` is the identity used here. ``claimed_by``
#: pins which path won — operationally useful when triaging double-emit
#: regressions.
CREATE_TABLE_SQL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    feature_id     TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    claimed_by     TEXT NOT NULL,
    claimed_at     TEXT NOT NULL,
    PRIMARY KEY (feature_id, correlation_id)
) STRICT;
"""


@dataclass(frozen=True, slots=True)
class TerminalPublishClaim:
    """One row in the terminal-publish ledger.

    Returned by :meth:`TerminalPublishLedger.get` for diagnostic /
    ``forge status`` reads. The bridge and F010F write paths use the
    boolean return of :meth:`TerminalPublishLedger.claim` directly and
    do not need to read the row back.

    Attributes:
        feature_id: Build identity (matches ``builds.feature_id``).
        correlation_id: F010C correlation-id threaded from the inbound
            ``build-queued`` envelope. Pairs with ``feature_id`` to form
            the composite primary key.
        claimed_by: Identifier of the path that won the race; one of
            :data:`CLAIMER_BRIDGE_TERMINAL` or
            :data:`CLAIMER_F010F_SAFETY_NET`.
        claimed_at: Wall-clock timestamp at the moment the claim
            committed.
    """

    feature_id: str
    correlation_id: str
    claimed_by: str
    claimed_at: datetime


def apply_migration(connection: sqlite3.Connection) -> None:
    """Apply the ``lifecycle_bridge_terminal_publishes`` migration.

    Idempotent — safe to invoke on every ``forge serve`` boot. Uses
    ``CREATE TABLE IF NOT EXISTS`` so re-running against an already
    migrated database is a no-op.

    Args:
        connection: A writable :class:`sqlite3.Connection` — typically
            the persistent connection produced by
            :func:`forge.adapters.sqlite.connect.connect_writer` and
            shared with the lifecycle bridge registry (ASSUM-011).

    Raises:
        TypeError: If ``connection`` is not a
            :class:`sqlite3.Connection`.
        sqlite3.Error: For any database error during DDL execution. The
            originating exception is preserved via ``__cause__`` so
            diagnostics surface the real cause.
    """
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError(
            "apply_migration: connection must be a sqlite3.Connection; got "
            f"{type(connection).__name__}"
        )

    try:
        with connection:  # commit on success; rollback on any raise.
            connection.executescript(CREATE_TABLE_SQL)
    except sqlite3.Error:
        logger.exception(
            "lifecycle_bridge_terminal_publishes migration failed",
        )
        raise

    logger.debug("applied %s migration", TABLE_NAME)


class TerminalPublishLedger:
    """First-wins terminal-publish claim ledger (TASK-FRR-PEB-005 AC-2/AC-3).

    Thin SQLite-backed repository. Both the bridge's async-terminal
    publish path and F010F's sync-raise safety-net path call
    :meth:`claim` before publishing; the call returns ``True`` only for
    the first invocation against a given ``(feature_id, correlation_id)``
    pair, so the second path observes ``False`` and stops without ever
    putting a competing envelope on the wire.

    Args:
        connection: Writer :class:`sqlite3.Connection` produced by
            :func:`forge.adapters.sqlite.connect.connect_writer`. The
            shared boot connection (ASSUM-011) is the canonical caller;
            tests substitute an in-memory connection.

    Notes:
        * The connection's ``row_factory`` is set to :class:`sqlite3.Row`
          if it was previously ``None`` so :meth:`get` can return
          named-column results without coupling callers to tuple
          ordering.
        * :meth:`claim` runs inside ``BEGIN IMMEDIATE`` so concurrent
          callers serialise through SQLite's writer lock — exactly the
          discipline the rest of the lifecycle bridge writers (T2/T3)
          already use. Mixing raw transactions with the autocommit
          default would otherwise allow one ``INSERT OR IGNORE`` to
          appear committed to a peer reader before the second one
          observed it, and the test in :class:`TestFirstWinsInvariant`
          would flake.
    """

    def __init__(self, *, connection: sqlite3.Connection) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError(
                "TerminalPublishLedger: connection must be a "
                f"sqlite3.Connection; got {type(connection).__name__}"
            )
        self._cx = connection
        if connection.row_factory is None:
            connection.row_factory = sqlite3.Row

    # ------------------------------------------------------------------
    # Write API — claim
    # ------------------------------------------------------------------

    def claim(
        self,
        *,
        feature_id: str,
        correlation_id: str,
        claimed_by: str,
    ) -> bool:
        """Atomically claim the terminal-publish slot for ``(feature_id, correlation_id)``.

        Returns ``True`` when **this** call inserted the row (the caller
        is the first writer and may proceed with the publish). Returns
        ``False`` when an earlier call already inserted the row (the
        caller MUST skip its publish to honour the no-duplicate
        invariant on the wire).

        Args:
            feature_id: Build identity. Must be non-empty.
            correlation_id: F010C correlation-id threaded from the
                inbound envelope. Must be non-empty.
            claimed_by: Identifier of the calling path. Production
                callers MUST use one of :data:`CLAIMER_BRIDGE_TERMINAL`
                or :data:`CLAIMER_F010F_SAFETY_NET`; tests may pass
                any string for diagnostic clarity. Must be non-empty.

        Returns:
            ``True`` when the caller won the race; ``False`` when the
            slot was already claimed.

        Raises:
            ValueError: If any argument is empty.
            sqlite3.Error: For any underlying database error. The
                transaction is rolled back so a partial row is never
                left behind.
        """
        if not feature_id:
            raise ValueError(
                "TerminalPublishLedger.claim: feature_id must be non-empty"
            )
        if not correlation_id:
            raise ValueError(
                "TerminalPublishLedger.claim: correlation_id must be non-empty"
            )
        if not claimed_by:
            raise ValueError(
                "TerminalPublishLedger.claim: claimed_by must be non-empty"
            )

        claimed_at_iso = datetime.now(UTC).isoformat()
        try:
            self._cx.execute("BEGIN IMMEDIATE;")
            cursor = self._cx.execute(
                f"""
                INSERT OR IGNORE INTO {TABLE_NAME} (
                    feature_id, correlation_id, claimed_by, claimed_at
                ) VALUES (?, ?, ?, ?)
                """,
                (feature_id, correlation_id, claimed_by, claimed_at_iso),
            )
            self._cx.execute("COMMIT;")
        except sqlite3.Error:
            self._safe_rollback()
            raise

        won = cursor.rowcount == 1
        if won:
            logger.info(
                "terminal_publish_ledger.claim: WON feature_id=%s "
                "correlation_id=%s claimed_by=%s",
                feature_id,
                correlation_id,
                claimed_by,
            )
        else:
            logger.info(
                "terminal_publish_ledger.claim: LOST feature_id=%s "
                "correlation_id=%s claimed_by=%s (already claimed); "
                "caller MUST skip publish",
                feature_id,
                correlation_id,
                claimed_by,
            )
        return won

    # ------------------------------------------------------------------
    # Read API — is_claimed / get
    # ------------------------------------------------------------------

    def is_claimed(
        self,
        *,
        feature_id: str,
        correlation_id: str,
    ) -> bool:
        """Return ``True`` when a terminal-publish row exists for the pair.

        Read-only convenience used by diagnostics; production publish
        paths use :meth:`claim` directly so the check + insert is atomic.

        Args:
            feature_id: Build identity.
            correlation_id: F010C correlation-id.

        Returns:
            ``True`` when a row already exists; ``False`` otherwise.

        Raises:
            ValueError: If either argument is empty.
        """
        if not feature_id:
            raise ValueError(
                "TerminalPublishLedger.is_claimed: feature_id must be non-empty"
            )
        if not correlation_id:
            raise ValueError(
                "TerminalPublishLedger.is_claimed: correlation_id must be "
                "non-empty"
            )

        row = self._cx.execute(
            f"""
            SELECT 1 FROM {TABLE_NAME}
             WHERE feature_id = ? AND correlation_id = ?
            """,
            (feature_id, correlation_id),
        ).fetchone()
        return row is not None

    def get(
        self,
        *,
        feature_id: str,
        correlation_id: str,
    ) -> TerminalPublishClaim | None:
        """Return the ledger row for ``(feature_id, correlation_id)`` or ``None``.

        Args:
            feature_id: Build identity.
            correlation_id: F010C correlation-id.

        Returns:
            The :class:`TerminalPublishClaim` for the row, or ``None``
            when no claim has been recorded yet.

        Raises:
            ValueError: If either argument is empty.
        """
        if not feature_id:
            raise ValueError(
                "TerminalPublishLedger.get: feature_id must be non-empty"
            )
        if not correlation_id:
            raise ValueError(
                "TerminalPublishLedger.get: correlation_id must be non-empty"
            )

        row = self._cx.execute(
            f"""
            SELECT feature_id, correlation_id, claimed_by, claimed_at
              FROM {TABLE_NAME}
             WHERE feature_id = ? AND correlation_id = ?
            """,
            (feature_id, correlation_id),
        ).fetchone()
        if row is None:
            return None

        # ``sqlite3.Row`` supports both name and index access — use names
        # so a future migration that adds columns cannot quietly shift
        # the index.
        if hasattr(row, "keys"):
            data = {key: row[key] for key in row.keys()}
        else:
            data = dict(
                zip(
                    ("feature_id", "correlation_id", "claimed_by", "claimed_at"),
                    row,
                    strict=False,
                )
            )
        return TerminalPublishClaim(
            feature_id=data["feature_id"],
            correlation_id=data["correlation_id"],
            claimed_by=data["claimed_by"],
            claimed_at=datetime.fromisoformat(data["claimed_at"]),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _safe_rollback(self) -> None:
        """Roll back swallowing secondary errors so the original raise survives."""
        try:
            self._cx.execute("ROLLBACK;")
        except sqlite3.Error:  # pragma: no cover - rollback failure is rare
            logger.exception("terminal_publish_ledger rollback failed")

"""``BridgeRegistry`` — repository over ``lifecycle_bridge_registry`` (TASK-FRR-PEB-002).

The :class:`BridgeRegistry` is the single SQL-aware writer of the
``lifecycle_bridge_registry`` table introduced by
:mod:`forge.persistence.migrations.lifecycle_bridge_registry`. The
:class:`forge.lifecycle_bridge.bridge.LifecycleBridge` composes this
repository for ``attach``/``detach`` and the recovery path; the future
``forge status --in-flight`` CLI (T12) composes :meth:`list_active`.

F010C correlation-id contract (AC-5)
------------------------------------

Every public method takes ``correlation_id`` explicitly. The value is
threaded through to the persisted row on writes and used for structured
logging on reads. The bridge tests' AST guard verifies every call site
in :mod:`forge.lifecycle_bridge.bridge` passes ``correlation_id=`` as a
keyword argument so a future refactor cannot silently drop the field.

Concurrency discipline
----------------------

Every write uses ``BEGIN IMMEDIATE`` + ``ON CONFLICT(feature_id) DO
UPDATE`` (UPSERT) so two ``record`` calls for the same ``feature_id``
serialise correctly under SQLite's busy-timeout window. The second
write wins — this matches the bridge's "re-attach on crash recovery"
semantics: a stale row is overwritten without leaving dangling state.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

from forge.persistence.migrations.lifecycle_bridge_registry import TABLE_NAME

logger = logging.getLogger(__name__)


__all__ = [
    "ALLOWED_LIFECYCLES",
    "BridgeRegistry",
    "BridgeRegistryEntry",
    "BridgeRegistryNotFoundError",
]


#: Coarse lifecycle states accepted by the registry. Typed lifecycle
#: states arrive in T3 with the SSE translation layer; for now we
#: pin the set the schema CHECK constraint enforces so violations
#: surface as a domain error rather than a raw ``IntegrityError``.
ALLOWED_LIFECYCLES: Final[frozenset[str]] = frozenset(
    {"queued", "running", "paused"}
)


# ---------------------------------------------------------------------------
# Domain errors
# ---------------------------------------------------------------------------


class BridgeRegistryNotFoundError(RuntimeError):
    """Raised by :meth:`BridgeRegistry.update_lifecycle` for missing rows.

    The ``BridgeRegistry`` does not auto-create rows on update because
    the lifecycle transition graph is owned by the SSE translation
    layer (T3). An update against a feature_id that was never attached
    is a programming error worth surfacing.
    """

    def __init__(self, feature_id: str) -> None:
        super().__init__(
            f"no lifecycle_bridge_registry row for feature_id={feature_id!r}"
        )
        self.feature_id = feature_id


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BridgeRegistryEntry:
    """One row of the ``lifecycle_bridge_registry`` table.

    A frozen dataclass so callers can compare entries by value and pass
    them across thread boundaries without defensive copies. The fields
    mirror the schema 1:1 — see
    :mod:`forge.persistence.migrations.lifecycle_bridge_registry` for
    the canonical column manifest.

    Attributes:
        feature_id: Primary key; one row per active build.
        thread_id: DeepAgents thread id.
        run_id: LangGraph run id.
        correlation_id: F010C correlation-id contract.
        ack_handle_token: Opaque token mapped back to the in-memory ack
            callback by the consumer (T1).
        deadline_at: 300s per-build deadline (ASSUM-003).
        attached_at: Wall-clock attach time.
        current_lifecycle: Coarse state: ``"queued"``, ``"running"``,
            ``"paused"``.
        updated_at: Wall-clock last update.
        last_event_id: Last SSE ``id:`` we acked; ``None`` until the
            first event arrives.
    """

    feature_id: str
    thread_id: str
    run_id: str
    correlation_id: str
    ack_handle_token: str
    deadline_at: datetime
    attached_at: datetime
    current_lifecycle: str
    updated_at: datetime
    last_event_id: str | None = None
    #: Subjects (e.g. ``"build-started"``, ``"stage-complete"``) already
    #: published for this build. Recovery uses this set to skip a
    #: replayed envelope that was already on the wire pre-restart
    #: (TASK-FRR-PEB-009 AC-2). Persisted as JSON-encoded TEXT.
    published_lifecycles: frozenset[str] = field(default_factory=frozenset)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Return the current UTC timestamp in ISO-8601 format."""
    return datetime.now(UTC).isoformat()


def _decode_published_lifecycles(raw: str | None) -> frozenset[str]:
    """Decode the JSON-encoded ``published_lifecycles`` column.

    A missing / empty value yields an empty frozenset — older registry
    rows written before the AC-2 column landed default to "nothing
    published" so the recovery sweep can re-publish from scratch.
    """
    if not raw:
        return frozenset()
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(
            "bridge_registry: published_lifecycles column contained invalid "
            "JSON (%r); treating as empty set",
            raw,
        )
        return frozenset()
    if not isinstance(decoded, list):
        logger.warning(
            "bridge_registry: published_lifecycles JSON not a list (%r); "
            "treating as empty set",
            decoded,
        )
        return frozenset()
    return frozenset(str(item) for item in decoded)


def _encode_published_lifecycles(subjects: frozenset[str] | set[str]) -> str:
    """Encode the ``published_lifecycles`` set for storage."""
    return json.dumps(sorted(subjects))


def _row_to_entry(row: sqlite3.Row | tuple) -> BridgeRegistryEntry:
    """Hydrate a ``lifecycle_bridge_registry`` row into a value object."""
    if isinstance(row, sqlite3.Row):
        data = {key: row[key] for key in row.keys()}
    else:
        keys = (
            "feature_id",
            "thread_id",
            "run_id",
            "correlation_id",
            "last_event_id",
            "ack_handle_token",
            "deadline_at",
            "attached_at",
            "current_lifecycle",
            "updated_at",
            "published_lifecycles",
        )
        data = dict(zip(keys, row, strict=False))

    return BridgeRegistryEntry(
        feature_id=data["feature_id"],
        thread_id=data["thread_id"],
        run_id=data["run_id"],
        correlation_id=data["correlation_id"],
        last_event_id=data.get("last_event_id"),
        ack_handle_token=data["ack_handle_token"],
        deadline_at=datetime.fromisoformat(data["deadline_at"]),
        attached_at=datetime.fromisoformat(data["attached_at"]),
        current_lifecycle=data["current_lifecycle"],
        updated_at=datetime.fromisoformat(data["updated_at"]),
        published_lifecycles=_decode_published_lifecycles(
            data.get("published_lifecycles")
        ),
    )


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class BridgeRegistry:
    """SQLite-backed repository over the ``lifecycle_bridge_registry`` table.

    Args:
        connection: Writer ``sqlite3.Connection`` produced by
            :func:`forge.adapters.sqlite.connect.connect_writer`. The
            repository assumes autocommit isolation and manages
            transactions via explicit ``BEGIN IMMEDIATE`` / ``COMMIT``.
    """

    def __init__(self, *, connection: sqlite3.Connection) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError(
                "BridgeRegistry: connection must be a sqlite3.Connection; "
                f"got {type(connection).__name__}"
            )
        self._cx = connection
        if connection.row_factory is None:
            connection.row_factory = sqlite3.Row

    # ------------------------------------------------------------------
    # Write API — record (UPSERT)
    # ------------------------------------------------------------------

    def record(
        self,
        entry: BridgeRegistryEntry,
        *,
        correlation_id: str,
    ) -> None:
        """Persist (or replace) a registry row for ``entry.feature_id``.

        UPSERT semantics: a re-attach for a feature that already has a
        row overwrites every field except ``feature_id`` itself. This
        matches the bridge's "stale row from a crashed process" recovery
        story — there is never a window where a duplicate-row error
        could propagate into ``LifecycleBridge.attach``.

        Args:
            entry: The :class:`BridgeRegistryEntry` to persist.
            correlation_id: The F010C correlation-id of the inbound
                build-queued envelope. Stored on the row and surfaced in
                structured logs so the bridge's writes can be traced
                end-to-end.

        Raises:
            TypeError: If ``entry`` is not a :class:`BridgeRegistryEntry`.
            ValueError: If ``correlation_id`` is empty.
            ValueError: If ``entry.current_lifecycle`` is not in
                :data:`ALLOWED_LIFECYCLES`.
            sqlite3.Error: For any database error. The transaction is
                rolled back so the row is not partially updated.
        """
        if not isinstance(entry, BridgeRegistryEntry):
            raise TypeError(
                "BridgeRegistry.record: entry must be a BridgeRegistryEntry; "
                f"got {type(entry).__name__}"
            )
        if not correlation_id:
            raise ValueError(
                "BridgeRegistry.record: correlation_id must be non-empty"
            )
        if entry.current_lifecycle not in ALLOWED_LIFECYCLES:
            raise ValueError(
                f"BridgeRegistry.record: unsupported current_lifecycle "
                f"{entry.current_lifecycle!r}; allowed={sorted(ALLOWED_LIFECYCLES)!r}"
            )

        published_json = _encode_published_lifecycles(entry.published_lifecycles)
        try:
            self._cx.execute("BEGIN IMMEDIATE;")
            self._cx.execute(
                f"""
                INSERT INTO {TABLE_NAME} (
                    feature_id, thread_id, run_id, correlation_id,
                    last_event_id, ack_handle_token, deadline_at,
                    attached_at, current_lifecycle, updated_at,
                    published_lifecycles
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(feature_id) DO UPDATE SET
                    thread_id            = excluded.thread_id,
                    run_id               = excluded.run_id,
                    correlation_id       = excluded.correlation_id,
                    last_event_id        = excluded.last_event_id,
                    ack_handle_token     = excluded.ack_handle_token,
                    deadline_at          = excluded.deadline_at,
                    attached_at          = excluded.attached_at,
                    current_lifecycle    = excluded.current_lifecycle,
                    updated_at           = excluded.updated_at,
                    published_lifecycles = excluded.published_lifecycles
                """,
                (
                    entry.feature_id,
                    entry.thread_id,
                    entry.run_id,
                    entry.correlation_id,
                    entry.last_event_id,
                    entry.ack_handle_token,
                    entry.deadline_at.isoformat(),
                    entry.attached_at.isoformat(),
                    entry.current_lifecycle,
                    entry.updated_at.isoformat(),
                    published_json,
                ),
            )
            self._cx.execute("COMMIT;")
        except sqlite3.Error:
            self._safe_rollback()
            raise

        logger.debug(
            "bridge_registry.record feature_id=%s correlation_id=%s "
            "current_lifecycle=%s",
            entry.feature_id,
            correlation_id,
            entry.current_lifecycle,
        )

    # ------------------------------------------------------------------
    # Write API — update_lifecycle
    # ------------------------------------------------------------------

    def update_lifecycle(
        self,
        feature_id: str,
        lifecycle: str,
        *,
        correlation_id: str,
        last_event_id: str | None = None,
    ) -> None:
        """Update the coarse lifecycle and optionally the last_event_id.

        ``last_event_id`` is preserved when ``None`` is passed; only an
        explicit non-``None`` value overwrites the column. This matches
        the SSE consumer's contract — we only advance the cursor when a
        new event id has been observed.

        Args:
            feature_id: Primary key of the row to update.
            lifecycle: New coarse lifecycle (must be in
                :data:`ALLOWED_LIFECYCLES`).
            correlation_id: F010C correlation-id of the originating
                envelope (logged for traceability).
            last_event_id: Most recent SSE event id; preserves the
                existing column value when ``None``.

        Raises:
            ValueError: If ``feature_id``, ``lifecycle``, or
                ``correlation_id`` is empty / unsupported.
            BridgeRegistryNotFoundError: If no row matches
                ``feature_id``.
            sqlite3.Error: For any database error.
        """
        if not feature_id:
            raise ValueError(
                "BridgeRegistry.update_lifecycle: feature_id must be non-empty"
            )
        if not correlation_id:
            raise ValueError(
                "BridgeRegistry.update_lifecycle: correlation_id must be non-empty"
            )
        if lifecycle not in ALLOWED_LIFECYCLES:
            raise ValueError(
                f"BridgeRegistry.update_lifecycle: unsupported lifecycle "
                f"{lifecycle!r}; allowed={sorted(ALLOWED_LIFECYCLES)!r}"
            )

        updated_at_iso = _now_iso()
        try:
            self._cx.execute("BEGIN IMMEDIATE;")
            cursor = self._cx.execute(
                f"""
                UPDATE {TABLE_NAME}
                   SET current_lifecycle = ?,
                       last_event_id = COALESCE(?, last_event_id),
                       updated_at = ?
                 WHERE feature_id = ?
                """,
                (lifecycle, last_event_id, updated_at_iso, feature_id),
            )
            if cursor.rowcount == 0:
                self._cx.execute("ROLLBACK;")
                raise BridgeRegistryNotFoundError(feature_id)
            self._cx.execute("COMMIT;")
        except sqlite3.Error:
            self._safe_rollback()
            raise

        logger.debug(
            "bridge_registry.update_lifecycle feature_id=%s lifecycle=%s "
            "correlation_id=%s",
            feature_id,
            lifecycle,
            correlation_id,
        )

    # ------------------------------------------------------------------
    # Read API — get
    # ------------------------------------------------------------------

    def get(
        self,
        feature_id: str,
        *,
        correlation_id: str,
    ) -> BridgeRegistryEntry | None:
        """Return the registry row for ``feature_id`` or ``None``.

        Args:
            feature_id: Primary-key lookup.
            correlation_id: F010C correlation-id of the calling context;
                logged for traceability.

        Returns:
            The :class:`BridgeRegistryEntry` for the row, or ``None``
            when the feature is not currently attached.
        """
        if not feature_id:
            raise ValueError(
                "BridgeRegistry.get: feature_id must be non-empty"
            )
        if not correlation_id:
            raise ValueError(
                "BridgeRegistry.get: correlation_id must be non-empty"
            )

        row = self._cx.execute(
            f"""
            SELECT feature_id, thread_id, run_id, correlation_id,
                   last_event_id, ack_handle_token, deadline_at,
                   attached_at, current_lifecycle, updated_at,
                   published_lifecycles
              FROM {TABLE_NAME}
             WHERE feature_id = ?
            """,
            (feature_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_entry(row)

    # ------------------------------------------------------------------
    # Read API — list_active
    # ------------------------------------------------------------------

    def list_active(
        self,
        *,
        correlation_id: str,
    ) -> list[BridgeRegistryEntry]:
        """Return every currently-attached build entry.

        The output is the source for ``forge status --in-flight`` (T12).
        Entries are ordered by ``attached_at`` ascending so the oldest
        in-flight build appears first — operators reading the CLI table
        get a stable ordering across runs.

        Args:
            correlation_id: F010C correlation-id of the calling context;
                logged for traceability.

        Returns:
            List of :class:`BridgeRegistryEntry`. Empty list when no
            builds are currently attached.
        """
        if not correlation_id:
            raise ValueError(
                "BridgeRegistry.list_active: correlation_id must be non-empty"
            )
        rows = self._cx.execute(
            f"""
            SELECT feature_id, thread_id, run_id, correlation_id,
                   last_event_id, ack_handle_token, deadline_at,
                   attached_at, current_lifecycle, updated_at,
                   published_lifecycles
              FROM {TABLE_NAME}
             ORDER BY attached_at ASC
            """,
        ).fetchall()
        return [_row_to_entry(row) for row in rows]

    # ------------------------------------------------------------------
    # Write API — mark_published (TASK-FRR-PEB-009 AC-2)
    # ------------------------------------------------------------------

    def mark_published(
        self,
        feature_id: str,
        subject: str,
        *,
        correlation_id: str,
        last_event_id: str | None = None,
    ) -> frozenset[str]:
        """Append ``subject`` to the row's ``published_lifecycles`` set.

        The publisher path appends BEFORE invoking the actual NATS
        publish so a concurrent recovery sweep cannot re-publish a
        subject already on the wire (TASK-FRR-PEB-009 AC-2). The set is
        stored as JSON-encoded TEXT — the column is never overwritten in
        place; we always read-modify-write atomically inside a
        ``BEGIN IMMEDIATE`` transaction.

        Args:
            feature_id: Primary key of the row to update.
            subject: Lifecycle subject segment (e.g. ``"build-started"``).
            correlation_id: F010C correlation-id of the calling envelope;
                logged for traceability.
            last_event_id: Optional SSE event id observed alongside the
                publish. Persisted via ``COALESCE`` so passing ``None``
                preserves the existing cursor.

        Returns:
            The new ``published_lifecycles`` frozenset after the append.

        Raises:
            ValueError: If any required argument is empty.
            BridgeRegistryNotFoundError: If no row matches ``feature_id``.
            sqlite3.Error: For any database error.
        """
        if not feature_id:
            raise ValueError(
                "BridgeRegistry.mark_published: feature_id must be non-empty"
            )
        if not subject:
            raise ValueError(
                "BridgeRegistry.mark_published: subject must be non-empty"
            )
        if not correlation_id:
            raise ValueError(
                "BridgeRegistry.mark_published: correlation_id must be non-empty"
            )

        try:
            self._cx.execute("BEGIN IMMEDIATE;")
            row = self._cx.execute(
                f"""
                SELECT published_lifecycles
                  FROM {TABLE_NAME}
                 WHERE feature_id = ?
                """,
                (feature_id,),
            ).fetchone()
            if row is None:
                self._cx.execute("ROLLBACK;")
                raise BridgeRegistryNotFoundError(feature_id)
            current = _decode_published_lifecycles(row[0] if not isinstance(row, sqlite3.Row) else row["published_lifecycles"])
            updated = frozenset(current | {subject})
            payload = _encode_published_lifecycles(updated)
            updated_at_iso = _now_iso()
            self._cx.execute(
                f"""
                UPDATE {TABLE_NAME}
                   SET published_lifecycles = ?,
                       last_event_id = COALESCE(?, last_event_id),
                       updated_at = ?
                 WHERE feature_id = ?
                """,
                (payload, last_event_id, updated_at_iso, feature_id),
            )
            self._cx.execute("COMMIT;")
        except sqlite3.Error:
            self._safe_rollback()
            raise

        logger.debug(
            "bridge_registry.mark_published feature_id=%s subject=%s "
            "correlation_id=%s set_size=%d",
            feature_id,
            subject,
            correlation_id,
            len(updated),
        )
        return updated

    # ------------------------------------------------------------------
    # Write API — delete
    # ------------------------------------------------------------------

    def delete(
        self,
        feature_id: str,
        *,
        correlation_id: str,
    ) -> None:
        """Remove the registry row for ``feature_id``.

        Idempotent — deleting a row that does not exist is a no-op.
        The bridge's ``detach`` path may be invoked twice during a
        recovery race (once by ``recover_in_flight`` cleanup and once
        by the SSE finaliser), and the second call must not raise.

        Args:
            feature_id: Primary key of the row to remove.
            correlation_id: F010C correlation-id of the calling context;
                logged for traceability.
        """
        if not feature_id:
            raise ValueError(
                "BridgeRegistry.delete: feature_id must be non-empty"
            )
        if not correlation_id:
            raise ValueError(
                "BridgeRegistry.delete: correlation_id must be non-empty"
            )

        try:
            self._cx.execute("BEGIN IMMEDIATE;")
            self._cx.execute(
                f"DELETE FROM {TABLE_NAME} WHERE feature_id = ?",
                (feature_id,),
            )
            self._cx.execute("COMMIT;")
        except sqlite3.Error:
            self._safe_rollback()
            raise

        logger.debug(
            "bridge_registry.delete feature_id=%s correlation_id=%s",
            feature_id,
            correlation_id,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _safe_rollback(self) -> None:
        """Roll back swallowing secondary errors so the original raise survives."""
        try:
            self._cx.execute("ROLLBACK;")
        except sqlite3.Error:  # pragma: no cover - rollback failure is rare
            logger.exception("bridge_registry rollback failed")

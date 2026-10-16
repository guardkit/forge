"""One revision counter per merge card, raised in the ledger (16 October 2026).

A merge card with a review preview is kept true after it is posted: Forge
describes the card from its ledger and Jarvis edits the Slack message to
match (``forge.pipeline.merge_card``). Lease rows change in place, so no row id
can tell a newer description from an older one. Each card therefore has one
counter (table ``merge_card_revisions``), raised by one in the SAME transaction
as every change that can alter the card:

* an offer or amended offer row;
* a decision row;
* a refused approval;
* any insert or update of that build's preview lease.

THE TABLE IS OUTSIDE THE MIGRATION LADDER, ON PURPOSE (coordinator's
decision, 16 October 2026). The ledger stays at schema 19 so that rolling back
to release 2026.10.16-1 stays a plain image swap: that release refuses to
start on any schema it does not know, but it ignores a table it does not use.
The table is created with ``CREATE TABLE IF NOT EXISTS`` at the coordinator's
boot, beside the lifecycle-bridge tables (and the estate rollout helper's copy
of those boot steps does the same), and :func:`bump_card_revision` creates it
too, so a ledger without it still works.

This module is the only writer of that table. It is deliberately small and
low in the layers, so the lease store (``forge.deploy.review_preview``), the
offer and the merge press can all raise the counter inside their own
transactions without importing one another.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from typing import Any

__all__ = [
    "DECISION_DETAILS_KEY",
    "DECISION_ROW",
    "OFFER_DETAILS_KEY",
    "OFFER_ROW",
    "bump_card_revision",
    "card_revision",
    "decision_on_record",
    "ensure_table",
    "immediate_transaction",
    "insert_stage_row",
    "latest_offer_details",
    "offer_rows",
]

#: ``stage_log.target_identifier`` of an offer row and of a decision row, and
#: the ``details`` keys they keep their record under. The same strings as
#: ``forge.pipeline.merge_offer`` and ``forge.pipeline.merge_executor`` (a test
#: holds them equal); repeated here so this module imports neither.
OFFER_ROW = "merge_deploy_offer"
OFFER_DETAILS_KEY = "merge_offer"
DECISION_ROW = "merge_deploy_decision"
DECISION_DETAILS_KEY = "merge_decision"


#: The table, created where it is needed and never migrated.
_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS merge_card_revisions ("
    "build_id TEXT PRIMARY KEY, "
    "revision INTEGER NOT NULL CHECK (revision >= 0), "
    "updated_at TEXT NOT NULL)"
)


def ensure_table(cx: sqlite3.Connection) -> None:
    """Create ``merge_card_revisions`` if it is missing. Idempotent.

    Called at the coordinator's boot (``bind_production_serve``) and by the
    estate rollout helper's copy of the boot steps, so a started ledger and
    the helper's loaded copy always have the same tables.
    """
    cx.execute(_TABLE_SQL)


@contextmanager
def immediate_transaction(cx: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """``BEGIN IMMEDIATE`` … ``COMMIT``, the way every ledger write is made.

    Nested use joins the transaction already open on the connection (the
    outer one commits). Any exception rolls back and is raised again.
    """
    if cx.in_transaction:
        yield cx
        return
    cx.execute("BEGIN IMMEDIATE")
    try:
        yield cx
    except BaseException:
        try:
            cx.execute("ROLLBACK")
        except sqlite3.Error:  # pragma: no cover - already rolled back
            pass
        raise
    else:
        cx.execute("COMMIT")


def bump_card_revision(cx: sqlite3.Connection, build_id: str, now: datetime) -> int:
    """Raise ``build_id``'s card revision by one; returns the new revision.

    Must be called inside an open transaction, so the counter moves with the
    change it describes or not at all.
    """
    if not cx.in_transaction:
        raise RuntimeError("bump_card_revision must run inside a transaction")
    cx.execute(_TABLE_SQL)
    cx.execute(
        "INSERT INTO merge_card_revisions (build_id, revision, updated_at) "
        "VALUES (?, 1, ?) "
        "ON CONFLICT(build_id) DO UPDATE SET "
        "revision = merge_card_revisions.revision + 1, "
        "updated_at = excluded.updated_at",
        (build_id, now.isoformat()),
    )
    return card_revision(cx, build_id)


def card_revision(cx: sqlite3.Connection, build_id: str) -> int:
    """The card's revision now; 0 for a build with no row (or no table yet)."""
    try:
        row = cx.execute(
            "SELECT revision FROM merge_card_revisions WHERE build_id = ?", (build_id,)
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return 0
        raise
    return int(row[0]) if row is not None else 0


def insert_stage_row(cx: sqlite3.Connection, entry: Any) -> int:
    """Append one ``stage_log`` row on ``cx`` (inside the caller's transaction).

    Exactly the row :meth:`SqliteLifecyclePersistence.record_stage` writes;
    returns its id.
    """
    details: Mapping[str, Any] = entry.details
    cursor = cx.execute(
        """
        INSERT INTO stage_log (
            build_id, stage_label, target_kind, target_identifier,
            status, gate_mode, coach_score, threshold_applied,
            started_at, completed_at, duration_secs, details_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entry.build_id,
            entry.stage_label,
            entry.target_kind,
            entry.target_identifier,
            entry.status,
            entry.gate_mode,
            entry.coach_score,
            entry.threshold_applied,
            entry.started_at.isoformat(),
            entry.completed_at.isoformat(),
            float(entry.duration_secs),
            json.dumps(details, sort_keys=True, default=str),
        ),
    )
    return int(cursor.lastrowid or 0)


def _details(raw: Any) -> dict[str, Any]:
    try:
        decoded = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def offer_rows(
    cx: sqlite3.Connection, build_id: str
) -> list[tuple[int, str, dict[str, Any]]]:
    """The build's offer rows as ``(id, started_at, offer record)``, oldest first.

    The same order :meth:`SqliteLifecyclePersistence.read_stages` gives, so
    the last one is the offer every reader takes as the latest.
    """
    rows = cx.execute(
        "SELECT id, started_at, details_json FROM stage_log "
        "WHERE build_id = ? AND target_identifier = ? ORDER BY started_at ASC, id ASC",
        (build_id, OFFER_ROW),
    ).fetchall()
    out: list[tuple[int, str, dict[str, Any]]] = []
    for row in rows:
        record = _details(row[2]).get(OFFER_DETAILS_KEY)
        out.append(
            (int(row[0]), str(row[1]), dict(record) if isinstance(record, dict) else {})
        )
    return out


def latest_offer_details(
    cx: sqlite3.Connection, build_id: str
) -> dict[str, Any] | None:
    """The build's latest committed offer record, or None."""
    rows = offer_rows(cx, build_id)
    return rows[-1][2] if rows else None


def decision_on_record(
    cx: sqlite3.Connection, build_id: str
) -> tuple[dict[str, Any], str, str] | None:
    """The build's first decision as ``(record, status, started_at)``, or None."""
    row = cx.execute(
        "SELECT details_json, status, started_at FROM stage_log "
        "WHERE build_id = ? AND target_identifier = ? ORDER BY id ASC LIMIT 1",
        (build_id, DECISION_ROW),
    ).fetchone()
    if row is None:
        return None
    record = _details(row[0]).get(DECISION_DETAILS_KEY)
    return (dict(record) if isinstance(record, dict) else {}), str(row[1]), str(row[2])

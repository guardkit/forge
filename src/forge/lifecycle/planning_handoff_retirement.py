"""Strict reader for permanent legacy planning-handoff retirement receipts.

This module is standard-library-only so Forge and the standalone router can
carry byte-identical copies.  A row that looks like a retirement is never
ignored: it is either a fully verified administrative receipt or a fail-closed
error.  Callers supply the SQLite connection; this module never opens another
connection or ends a transaction that it did not start.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterator

RETIREMENT_STAGE_LABEL = "planning-handoff-retirement-administrative"
RETIREMENT_DETAILS_KEY = "planning_handoff_retirement"
RETIREMENT_REASON = "owner-retired-undelivered-pre-routing-handoff"
MAX_DETAILS_BYTES = 64 * 1024
EXPECTED_ORIGINAL_EVENTS = 20
SUPPORTED_RECEIPT_SCHEMA_VERSIONS = frozenset({17, 18})

_HEX = frozenset("0123456789abcdef")
_TOP_KEYS = frozenset({RETIREMENT_DETAILS_KEY})
_RECEIPT_KEYS = frozenset(
    {
        "format_version",
        "correlation_id",
        "history_before",
        "original_event_ids",
        "planning_identity_sha256",
        "event_identity_sha256",
        "delivery_gap_identity_sha256",
        "audit_sha256",
        "reason",
        "retired_at",
    }
)
_PLANNING_COLUMNS = (
    "correlation_id",
    "state",
    "originating_user",
    "expected_approver",
    "request_text",
    "target_repo",
    "triggered_by",
    "originating_adapter",
    "parent_request_id",
    "pending_approval_request_id",
    "defer_count",
    "paused_at",
    "escalated_at",
    "handoff_branch",
    "handoff_path",
    "queued_at",
    "started_at",
    "completed_at",
    "error",
    "start_commit",
    "target_branch",
    "memory_project",
    "launch_settings",
)
_EVENT_COLUMNS = (
    "id",
    "correlation_id",
    "stage_label",
    "status",
    "gate_mode",
    "coach_score",
    "actor_identity",
    "details_json",
    "recorded_at",
)
_BUILD_TRIGGER_KEYS = frozenset({"feature_id", "build_id", "target_repo", "branch"})

__all__ = [
    "EXPECTED_ORIGINAL_EVENTS",
    "MAX_DETAILS_BYTES",
    "PlanningHandoffIdentity",
    "PlanningHandoffRetirementError",
    "RETIREMENT_DETAILS_KEY",
    "RETIREMENT_REASON",
    "RETIREMENT_STAGE_LABEL",
    "SUPPORTED_RECEIPT_SCHEMA_VERSIONS",
    "has_retirement_looking_history",
    "planning_handoff_identity",
    "retired_planning_handoff_correlations",
    "retirement_details_json",
    "strict_json_loads",
]


class PlanningHandoffRetirementError(RuntimeError):
    """Retirement-looking history is malformed, inconsistent or unsupported."""


class _DuplicateJsonKey(ValueError):
    pass


@dataclass(frozen=True)
class PlanningHandoffIdentity:
    planning_identity_sha256: str
    event_identity_sha256: str
    delivery_gap_identity_sha256: str


def strict_json_loads(value: str) -> object:
    """Parse JSON while rejecting duplicate keys and non-standard numbers."""

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        answer: dict[str, object] = {}
        for key, item in pairs:
            if key in answer:
                raise _DuplicateJsonKey(f"duplicate JSON key {key!r}")
            answer[key] = item
        return answer

    def invalid_constant(value: str) -> object:
        raise ValueError(f"non-standard JSON constant {value!r}")

    return json.loads(
        value,
        object_pairs_hook=unique_object,
        parse_constant=invalid_constant,
    )


def _fail(message: str) -> PlanningHandoffRetirementError:
    return PlanningHandoffRetirementError(
        f"invalid planning handoff retirement receipt: {message}"
    )


@contextmanager
def _consistent_read(connection: sqlite3.Connection) -> Iterator[None]:
    started = not connection.in_transaction
    if started:
        connection.execute("BEGIN")
    try:
        yield
    except BaseException:
        if started:
            connection.rollback()
        raise
    else:
        if started:
            connection.commit()


def _table_columns(connection: sqlite3.Connection, table: str) -> frozenset[str]:
    return frozenset(str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})"))


def _require_columns(
    connection: sqlite3.Connection, table: str, required: tuple[str, ...]
) -> None:
    found = _table_columns(connection, table)
    if not found:
        raise _fail(f"required ledger table is missing: {table}")
    missing = sorted(set(required) - found)
    if missing:
        raise _fail(
            f"required ledger columns are missing from {table}: {', '.join(missing)}"
        )


def has_retirement_looking_history(connection: sqlite3.Connection) -> bool:
    """Return whether the one canonical selector finds retirement-looking history.

    This is deliberately not a retirement-membership API.  It performs no
    strict receipt validation; JSON lookup is used only to recognize an
    escaped spelling of the reserved top-level member name.
    """

    try:
        columns = _table_columns(connection, "planning_run_events")
        if not columns:
            return False
        missing = {"stage_label", "details_json"} - columns
        if missing:
            raise _fail(
                "retirement selector columns are missing from planning_run_events: "
                + ", ".join(sorted(missing))
            )
        row = connection.execute(
            """
            SELECT 1
            FROM planning_run_events
            WHERE stage_label = ?
               OR instr(coalesce(details_json, ''), ?) > 0
               OR CASE WHEN json_valid(details_json) THEN
                    json_type(details_json, '$.planning_handoff_retirement')
                  END IS NOT NULL
            LIMIT 1
            """,
            (RETIREMENT_STAGE_LABEL, f'"{RETIREMENT_DETAILS_KEY}"'),
        ).fetchone()
        return row is not None
    except PlanningHandoffRetirementError:
        raise
    except sqlite3.Error as exc:
        raise _fail(f"retirement-looking history cannot be selected: {exc}") from exc


def _schema_version(connection: sqlite3.Connection) -> int:
    _require_columns(connection, "schema_version", ("version", "applied_at"))
    row = connection.execute("SELECT max(version) FROM schema_version").fetchone()
    version = row[0] if row else None
    if type(version) is not int or version not in SUPPORTED_RECEIPT_SCHEMA_VERSIONS:
        raise _fail(f"unsupported ledger schema version {version!r}")
    return version


def _validate_schema(connection: sqlite3.Connection) -> int:
    version = _schema_version(connection)
    _require_columns(connection, "planning_runs", _PLANNING_COLUMNS)
    _require_columns(connection, "planning_run_events", _EVENT_COLUMNS)
    _require_columns(connection, "builds", ("build_id", "correlation_id"))
    _require_columns(connection, "work_queue", ("id", "correlation_id"))
    seed_columns = _table_columns(connection, "feature_routing_seeds")
    if version == 17:
        if seed_columns:
            raise _fail("schema v17 unexpectedly contains feature_routing_seeds")
    else:
        if not seed_columns:
            raise _fail("schema v18 is missing feature_routing_seeds")
        missing = {"feature_routing_id", "state"} - seed_columns
        if missing:
            raise _fail(
                "required ledger columns are missing from feature_routing_seeds: "
                + ", ".join(sorted(missing))
            )
    return version


def _canonical_sha256(value: object) -> str:
    try:
        canonical = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise _fail("ledger identity cannot be encoded canonically") from exc
    return hashlib.sha256(canonical).hexdigest()


def _is_lower_hex(value: object, length: int = 64) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in _HEX for character in value)
    )


def _canonical_utc(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise _fail(f"{field} must be a nonempty canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise _fail(f"{field} is not an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise _fail(f"{field} must be timezone-aware UTC")
    if value != parsed.isoformat():
        raise _fail(f"{field} is not canonical")
    return value


def _utc_instant(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise _fail(f"{field} must be a nonempty UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _fail(f"{field} is not an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise _fail(f"{field} must be timezone-aware UTC")
    return parsed


def _rows_for_ids(
    connection: sqlite3.Connection, original_event_ids: tuple[int, ...]
) -> list[list[object]]:
    marks = ",".join("?" for _ in original_event_ids)
    columns = ", ".join(_EVENT_COLUMNS)
    rows = connection.execute(
        f"SELECT {columns} FROM planning_run_events "
        f"WHERE id IN ({marks}) ORDER BY id",
        original_event_ids,
    ).fetchall()
    return [list(row) for row in rows]


def _validate_event_ids(original_event_ids: tuple[int, ...]) -> None:
    if not isinstance(original_event_ids, tuple):
        raise _fail("original_event_ids must be a tuple")
    if len(original_event_ids) != EXPECTED_ORIGINAL_EVENTS:
        raise _fail(f"exactly {EXPECTED_ORIGINAL_EVENTS} original events are required")
    if any(type(value) is not int or value <= 0 for value in original_event_ids):
        raise _fail("original event IDs must be positive integers")
    if original_event_ids != tuple(sorted(set(original_event_ids))):
        raise _fail("original event IDs must be unique and ascending")


def _identity(
    connection: sqlite3.Connection,
    *,
    correlation_id: str,
    original_event_ids: tuple[int, ...],
    history_before: str | None,
    receipt_id: int | None,
) -> PlanningHandoffIdentity:
    if not isinstance(correlation_id, str) or not correlation_id:
        raise _fail("correlation_id must be nonempty text")
    _validate_event_ids(original_event_ids)
    planning_columns = ", ".join(_PLANNING_COLUMNS)
    planning_rows = connection.execute(
        f"SELECT {planning_columns} FROM planning_runs WHERE correlation_id = ?",
        (correlation_id,),
    ).fetchall()
    if len(planning_rows) != 1:
        raise _fail("the referenced planning row is missing or duplicated")
    planning_row = list(planning_rows[0])
    if planning_row[1] != "BUILD_QUEUED":
        raise _fail("the referenced planning row is not BUILD_QUEUED")
    completed_at = planning_row[_PLANNING_COLUMNS.index("completed_at")]
    if completed_at is None:
        raise _fail("the referenced planning row is not completed")

    event_rows = _rows_for_ids(connection, original_event_ids)
    if len(event_rows) != EXPECTED_ORIGINAL_EVENTS:
        raise _fail("one or more referenced original events are missing")
    for row in event_rows:
        event_id, event_correlation = row[0], row[1]
        if event_correlation != correlation_id:
            raise _fail(f"event {event_id} belongs to a different correlation")
        if receipt_id is not None and event_id >= receipt_id:
            raise _fail(f"event {event_id} does not precede the retirement")

    if history_before is not None:
        _canonical_utc(history_before, field="history_before")
        cutoff = _utc_instant(history_before, field="history_before")
        if _utc_instant(completed_at, field="planning completed_at") >= cutoff:
            raise _fail("the planning row does not precede history_before")
        for row in event_rows:
            if _utc_instant(row[8], field=f"event {row[0]} recorded_at") >= cutoff:
                raise _fail(f"event {row[0]} does not precede history_before")

    trigger_rows = [row for row in event_rows if row[2] == "build-queued"]
    approved_triggers = [row for row in trigger_rows if row[3] == "approved"]
    terminal_triggers = [row for row in trigger_rows if row[3] == "BUILD_QUEUED"]
    if len(approved_triggers) != 1 or len(terminal_triggers) != 1:
        raise _fail("the referenced events do not contain the exact build trigger pair")
    raw_trigger = approved_triggers[0][7]
    try:
        trigger = strict_json_loads(raw_trigger)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise _fail("the build trigger details are malformed") from exc
    if not isinstance(trigger, dict) or frozenset(trigger) != _BUILD_TRIGGER_KEYS:
        raise _fail("the build trigger details have unknown or missing keys")
    if not isinstance(trigger["feature_id"], str) or not trigger["feature_id"]:
        raise _fail("the build trigger feature_id is invalid")
    if trigger["build_id"] is not None:
        raise _fail("the undelivered build trigger unexpectedly names a build")
    for key in ("target_repo", "branch"):
        if not isinstance(trigger[key], str) or not trigger[key]:
            raise _fail(f"the build trigger {key} is invalid")

    build_rows = connection.execute(
        "SELECT build_id FROM builds WHERE correlation_id = ? ORDER BY build_id",
        (correlation_id,),
    ).fetchall()
    queue_rows = connection.execute(
        "SELECT id FROM work_queue WHERE correlation_id = ? ORDER BY id",
        (correlation_id,),
    ).fetchall()
    if build_rows:
        raise _fail("the retired correlation has a linked build")
    if queue_rows:
        raise _fail("the retired correlation has a linked work-queue row")
    if _table_columns(connection, "feature_routing_seeds"):
        seed = connection.execute(
            "SELECT 1 FROM feature_routing_seeds WHERE feature_routing_id = ? LIMIT 1",
            (correlation_id,),
        ).fetchone()
        if seed is not None:
            raise _fail("the retired correlation has a routing seed")

    allowed_ids = set(original_event_ids)
    if receipt_id is not None:
        allowed_ids.add(receipt_id)
    all_ids = {
        int(row[0])
        for row in connection.execute(
            "SELECT id FROM planning_run_events WHERE correlation_id = ?",
            (correlation_id,),
        )
    }
    if all_ids != allowed_ids:
        raise _fail("the correlation has an unreferenced or later event")

    delivery_projection = {
        "correlation_id": correlation_id,
        "build_rows": [],
        "work_queue_rows": [],
        "build_trigger_events": trigger_rows,
    }
    return PlanningHandoffIdentity(
        planning_identity_sha256=_canonical_sha256(planning_row),
        event_identity_sha256=_canonical_sha256(event_rows),
        delivery_gap_identity_sha256=_canonical_sha256(delivery_projection),
    )


def planning_handoff_identity(
    connection: sqlite3.Connection,
    *,
    correlation_id: str,
    original_event_ids: tuple[int, ...],
) -> PlanningHandoffIdentity:
    """Recompute the fixed identity of one undelivered legacy handoff."""
    try:
        with _consistent_read(connection):
            _validate_schema(connection)
            return _identity(
                connection,
                correlation_id=correlation_id,
                original_event_ids=original_event_ids,
                history_before=None,
                receipt_id=None,
            )
    except PlanningHandoffRetirementError:
        raise
    except sqlite3.Error as exc:
        raise _fail(f"ledger cannot be read: {exc}") from exc


def retirement_details_json(
    connection: sqlite3.Connection,
    *,
    correlation_id: str,
    original_event_ids: tuple[int, ...],
    history_before: str,
    audit_sha256: str,
    retired_at: str,
) -> str:
    """Build exact canonical format-1 details after recomputing ledger identity."""
    history_before = _canonical_utc(history_before, field="history_before")
    retired_at = _canonical_utc(retired_at, field="retired_at")
    if history_before >= retired_at:
        raise _fail("history_before must precede retired_at")
    if not _is_lower_hex(audit_sha256):
        raise _fail("audit_sha256 is invalid")
    try:
        with _consistent_read(connection):
            _validate_schema(connection)
            identity = _identity(
                connection,
                correlation_id=correlation_id,
                original_event_ids=original_event_ids,
                history_before=history_before,
                receipt_id=None,
            )
            receipt = {
                "format_version": 1,
                "correlation_id": correlation_id,
                "history_before": history_before,
                "original_event_ids": list(original_event_ids),
                "planning_identity_sha256": identity.planning_identity_sha256,
                "event_identity_sha256": identity.event_identity_sha256,
                "delivery_gap_identity_sha256": identity.delivery_gap_identity_sha256,
                "audit_sha256": audit_sha256,
                "reason": RETIREMENT_REASON,
                "retired_at": retired_at,
            }
            return json.dumps(
                {RETIREMENT_DETAILS_KEY: receipt},
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
    except PlanningHandoffRetirementError:
        raise
    except sqlite3.Error as exc:
        raise _fail(f"ledger cannot be read: {exc}") from exc


def _selected_rows(connection: sqlite3.Connection) -> list[tuple[object, ...]]:
    return connection.execute(
        """
        SELECT id, correlation_id, stage_label, status, gate_mode, coach_score,
               actor_identity, details_json, recorded_at
        FROM planning_run_events
        WHERE stage_label = ?
           OR instr(coalesce(details_json, ''), ?) > 0
           OR CASE WHEN json_valid(details_json) THEN
                json_type(details_json, '$.planning_handoff_retirement')
              END IS NOT NULL
        ORDER BY id
        """,
        (RETIREMENT_STAGE_LABEL, f'"{RETIREMENT_DETAILS_KEY}"'),
    ).fetchall()


def _validate_receipt(
    connection: sqlite3.Connection, row: tuple[object, ...]
) -> str:
    (
        receipt_id,
        outer_correlation,
        stage_label,
        status,
        gate_mode,
        coach_score,
        actor_identity,
        details_json,
        recorded_at,
    ) = row
    if stage_label != RETIREMENT_STAGE_LABEL or status != "SKIPPED":
        raise _fail(f"row {receipt_id} has the wrong label or status")
    if gate_mode is not None or coach_score is not None:
        raise _fail(f"row {receipt_id} must not carry gate or coach data")
    if not isinstance(actor_identity, str) or not actor_identity.strip():
        raise _fail(f"row {receipt_id} has no truthful actor identity")
    retired_at = _canonical_utc(recorded_at, field=f"row {receipt_id} recorded_at")
    if not isinstance(details_json, str):
        raise _fail(f"row {receipt_id} details_json is not text")
    try:
        details_bytes = details_json.encode("utf-8")
    except UnicodeError as exc:
        raise _fail(f"row {receipt_id} details are not valid UTF-8") from exc
    if len(details_bytes) > MAX_DETAILS_BYTES:
        raise _fail(f"row {receipt_id} details exceed {MAX_DETAILS_BYTES} bytes")
    try:
        details = strict_json_loads(details_json)
    except (ValueError, UnicodeError) as exc:
        raise _fail(f"row {receipt_id} details are not valid JSON") from exc
    if not isinstance(details, dict) or frozenset(details) != _TOP_KEYS:
        raise _fail(f"row {receipt_id} has unknown or missing top-level keys")
    receipt = details[RETIREMENT_DETAILS_KEY]
    if not isinstance(receipt, dict) or frozenset(receipt) != _RECEIPT_KEYS:
        raise _fail(f"row {receipt_id} has unknown or missing receipt keys")
    if type(receipt["format_version"]) is not int or receipt["format_version"] != 1:
        raise _fail(f"row {receipt_id} has an unsupported format version")
    if receipt["correlation_id"] != outer_correlation:
        raise _fail(f"row {receipt_id} correlation does not match its event")
    history_before = _canonical_utc(
        receipt["history_before"], field=f"row {receipt_id} history_before"
    )
    if history_before >= retired_at:
        raise _fail(f"row {receipt_id} history_before does not precede retirement")
    if receipt["retired_at"] != retired_at:
        raise _fail(f"row {receipt_id} retired_at does not match recorded_at")
    if receipt["reason"] != RETIREMENT_REASON:
        raise _fail(f"row {receipt_id} has the wrong reason")
    for key in (
        "planning_identity_sha256",
        "event_identity_sha256",
        "delivery_gap_identity_sha256",
        "audit_sha256",
    ):
        if not _is_lower_hex(receipt[key]):
            raise _fail(f"row {receipt_id} has an invalid {key}")
    raw_ids = receipt["original_event_ids"]
    if not isinstance(raw_ids, list):
        raise _fail(f"row {receipt_id} original_event_ids must be a list")
    original_event_ids = tuple(raw_ids)
    _validate_event_ids(original_event_ids)
    identity = _identity(
        connection,
        correlation_id=str(outer_correlation),
        original_event_ids=original_event_ids,
        history_before=history_before,
        receipt_id=int(receipt_id),
    )
    for key in (
        "planning_identity_sha256",
        "event_identity_sha256",
        "delivery_gap_identity_sha256",
    ):
        if receipt[key] != getattr(identity, key):
            raise _fail(f"row {receipt_id} {key} does not match")
    return str(outer_correlation)


def retired_planning_handoff_correlations(
    connection: sqlite3.Connection,
) -> frozenset[str]:
    """Return exact correlations named by every fully valid receipt."""
    try:
        with _consistent_read(connection):
            _validate_schema(connection)
            rows = _selected_rows(connection)
            retired: set[str] = set()
            for row in rows:
                correlation_id = _validate_receipt(connection, tuple(row))
                if correlation_id in retired:
                    raise _fail(
                        f"correlation {correlation_id!r} has duplicate retirements"
                    )
                retired.add(correlation_id)
            return frozenset(retired)
    except PlanningHandoffRetirementError:
        raise
    except (sqlite3.Error, TypeError, ValueError, UnicodeError) as exc:
        raise _fail(f"ledger cannot be read: {exc}") from exc

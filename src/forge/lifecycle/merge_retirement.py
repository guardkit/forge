"""Strict reader for permanent pre-routing merge retirement receipts.

This module is standard-library-only so the standalone router can carry an
identical copy.  A row that looks like a retirement is never ignored: it is
either a fully verified administrative receipt or a fail-closed error.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Iterator

RETIREMENT_TARGET = "merge_deploy_retirement"
RETIREMENT_STAGE_LABEL = "merge-retirement-administrative"
RETIREMENT_REASON = "owner-retired-pre-routing-work"
MAX_DECISION_IDS = 256
MAX_DETAILS_BYTES = 64 * 1024
_HEX = frozenset("0123456789abcdef")
_TOP_KEYS = frozenset({"merge_retirement"})
_RECEIPT_KEYS = frozenset(
    {
        "format_version",
        "decision_ids",
        "decision_identity_sha256",
        "reason",
        "retired_at",
        "audit_sha256",
    }
)

__all__ = [
    "MAX_DECISION_IDS",
    "MAX_DETAILS_BYTES",
    "MergeRetired",
    "MergeRetirementError",
    "RETIREMENT_REASON",
    "RETIREMENT_STAGE_LABEL",
    "RETIREMENT_TARGET",
    "decision_identity_sha256",
    "refuse_retired_build",
    "retired_decision_ids",
    "strict_json_loads",
]


class MergeRetirementError(RuntimeError):
    """Retirement-looking history is malformed or unsupported."""


class MergeRetired(RuntimeError):
    """The build's old merge authority was permanently retired."""


class _DuplicateJsonKey(ValueError):
    pass


def strict_json_loads(value: str) -> object:
    """Parse JSON while rejecting every duplicate object key."""

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


def _fail(message: str) -> MergeRetirementError:
    return MergeRetirementError(f"invalid merge retirement receipt: {message}")


def _is_lower_hex(value: object, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in _HEX for character in value)
    )


def _require_utc_timestamp(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise _fail(f"{field} must be a nonempty UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _fail(f"{field} is not an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise _fail(f"{field} must be timezone-aware UTC")
    return value


def _decision_rows(
    connection: sqlite3.Connection, decision_ids: tuple[int, ...]
) -> list[list[object]]:
    marks = ",".join("?" for _ in decision_ids)
    rows = connection.execute(
        "SELECT id, build_id, target_identifier, status, started_at, "
        "completed_at, details_json FROM stage_log "
        f"WHERE id IN ({marks}) ORDER BY id",
        decision_ids,
    ).fetchall()
    return [list(row) for row in rows]


def decision_identity_sha256(rows: list[list[object]]) -> str:
    """Hash the fixed original-decision identity tuple."""
    canonical = json.dumps(
        rows,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _validated_ids_for_row(
    connection: sqlite3.Connection, row: tuple[object, ...]
) -> tuple[int, ...]:
    (
        retirement_id,
        build_id,
        stage_label,
        target_kind,
        status,
        started_at,
        completed_at,
        duration_secs,
        details_json,
    ) = row
    if (
        stage_label != RETIREMENT_STAGE_LABEL
        or target_kind != "local_tool"
        or status != "SKIPPED"
    ):
        raise _fail(f"row {retirement_id} has the wrong label, kind or status")
    if not isinstance(duration_secs, (int, float)) or duration_secs != 0:
        raise _fail(f"row {retirement_id} must have zero duration")
    if started_at != completed_at:
        raise _fail(f"row {retirement_id} timestamps do not match")
    retired_at = _require_utc_timestamp(started_at, field="stage timestamp")
    if not isinstance(details_json, str):
        raise _fail(f"row {retirement_id} details_json is not text")
    try:
        details_bytes = details_json.encode("utf-8")
    except UnicodeError as exc:
        raise _fail(f"row {retirement_id} details are not valid UTF-8") from exc
    if len(details_bytes) > MAX_DETAILS_BYTES:
        raise _fail(f"row {retirement_id} details exceed {MAX_DETAILS_BYTES} bytes")
    try:
        details = strict_json_loads(details_json)
    except (ValueError, UnicodeError) as exc:
        raise _fail(f"row {retirement_id} details are not valid JSON") from exc
    if not isinstance(details, dict) or frozenset(details) != _TOP_KEYS:
        raise _fail(f"row {retirement_id} has unknown or missing top-level keys")
    receipt = details["merge_retirement"]
    if not isinstance(receipt, dict) or frozenset(receipt) != _RECEIPT_KEYS:
        raise _fail(f"row {retirement_id} has unknown or missing receipt keys")
    if type(receipt["format_version"]) is not int or receipt["format_version"] != 1:
        raise _fail(f"row {retirement_id} has an unsupported format version")
    raw_ids = receipt["decision_ids"]
    if not isinstance(raw_ids, list) or not raw_ids:
        raise _fail(f"row {retirement_id} decision_ids must be nonempty")
    if len(raw_ids) > MAX_DECISION_IDS:
        raise _fail(f"row {retirement_id} has too many decision IDs")
    if any(type(value) is not int or value <= 0 for value in raw_ids):
        raise _fail(f"row {retirement_id} decision IDs must be positive integers")
    decision_ids = tuple(raw_ids)
    if decision_ids != tuple(sorted(set(decision_ids))):
        raise _fail(f"row {retirement_id} decision IDs must be unique and ascending")
    if receipt["reason"] != RETIREMENT_REASON:
        raise _fail(f"row {retirement_id} has the wrong reason")
    if receipt["retired_at"] != retired_at:
        raise _fail(f"row {retirement_id} retired_at does not match its stage")
    if not _is_lower_hex(receipt["decision_identity_sha256"], 64):
        raise _fail(f"row {retirement_id} has an invalid identity digest")
    if not _is_lower_hex(receipt["audit_sha256"], 64):
        raise _fail(f"row {retirement_id} has an invalid audit digest")

    rows = _decision_rows(connection, decision_ids)
    if len(rows) != len(decision_ids):
        raise _fail(f"row {retirement_id} references a missing decision")
    for decision in rows:
        decision_id, decision_build, target, decision_status, *_rest, raw = decision
        if decision_build != build_id:
            raise _fail(f"row {retirement_id} references a different build")
        if type(decision_id) is not int or decision_id >= retirement_id:
            raise _fail(f"row {retirement_id} references a non-earlier decision")
        if target != "merge_deploy_decision" or decision_status != "PASSED":
            raise _fail(f"row {retirement_id} references a non-approved decision")
        try:
            parsed = strict_json_loads(raw)
        except (TypeError, ValueError) as exc:
            raise _fail(f"decision {decision_id} has malformed details") from exc
        merge_decision = parsed.get("merge_decision") if isinstance(parsed, dict) else None
        if not isinstance(merge_decision, dict) or merge_decision.get("decision") != "approve":
            raise _fail(f"decision {decision_id} is not an approval")
        if (
            "execution_attempt_version" in merge_decision
            or "execution_attempt_id" in merge_decision
        ):
            raise _fail(f"decision {decision_id} is versioned or partially versioned")
    if decision_identity_sha256(rows) != receipt["decision_identity_sha256"]:
        raise _fail(f"row {retirement_id} decision identity digest does not match")
    return decision_ids


def _validated_receipts(
    connection: sqlite3.Connection, build_id: str | None = None
) -> list[tuple[str, tuple[int, ...]]]:
    where = "WHERE target_identifier = ?"
    params: tuple[object, ...] = (RETIREMENT_TARGET,)
    if build_id is not None:
        where += " AND build_id = ?"
        params += (build_id,)
    rows = connection.execute(
        "SELECT id, build_id, stage_label, target_kind, status, started_at, completed_at, "
        "duration_secs, details_json FROM stage_log "
        f"{where} ORDER BY id",
        params,
    ).fetchall()
    seen_builds: set[str] = set()
    receipts: list[tuple[str, tuple[int, ...]]] = []
    for raw_row in rows:
        receipt_build = str(raw_row[1])
        if receipt_build in seen_builds:
            raise _fail(f"build {receipt_build!r} has duplicate retirements")
        seen_builds.add(receipt_build)
        receipts.append(
            (receipt_build, _validated_ids_for_row(connection, tuple(raw_row)))
        )
    return receipts


def retired_decision_ids(connection: sqlite3.Connection) -> frozenset[int]:
    """Return exact approval row IDs named by valid retirement receipts."""
    try:
        with _consistent_read(connection):
            ids: set[int] = set()
            for _build_id, decision_ids in _validated_receipts(connection):
                overlap = ids.intersection(decision_ids)
                if overlap:
                    raise _fail("one decision is named by multiple retirements")
                ids.update(decision_ids)
            return frozenset(ids)
    except MergeRetirementError:
        raise
    except sqlite3.Error as exc:
        raise _fail(f"ledger cannot be read: {exc}") from exc


def refuse_retired_build(connection: sqlite3.Connection, build_id: str) -> None:
    """Refuse valid retirement permanently and malformed-looking rows safely."""
    if not isinstance(build_id, str) or not build_id:
        raise ValueError("build_id must be a nonempty string")
    try:
        with _consistent_read(connection):
            receipts = _validated_receipts(connection, build_id)
    except MergeRetirementError:
        raise
    except sqlite3.Error as exc:
        raise _fail(f"ledger cannot be read: {exc}") from exc
    if receipts:
        raise MergeRetired(
            f"merge authority for build {build_id!r} was permanently retired"
        )

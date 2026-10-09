"""One-off writer for the owner-approved 35-build merge retirement."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable

from forge.lifecycle.merge_retirement import (
    RETIREMENT_REASON,
    RETIREMENT_STAGE_LABEL,
    RETIREMENT_TARGET,
    decision_identity_sha256,
    retired_decision_ids,
    strict_json_loads,
)
from forge.pipeline.publication_record import PublicationRecordStore

EXPECTED_APPROVALS = 35
EXPECTED_HOLDERS = 6
EXPECTED_LATEST_OUTCOMES = {
    ("merge_deploy_executor", "PASSED"): 13,
    ("merge_deploy_executor", "FAILED"): 18,
    ("merge_deploy_merge", "GATED"): 4,
}
_ALLOWED_HISTORICAL_STAGE_STATUSES = frozenset(
    {
        ("merge_deploy_candidate", "FAILED"),
        ("merge_deploy_candidate", "PASSED"),
        ("merge_deploy_decision", "PASSED"),
        ("merge_deploy_deploy", "GATED"),
        ("merge_deploy_executor", "FAILED"),
        ("merge_deploy_executor", "PASSED"),
        ("merge_deploy_merge", "GATED"),
        ("merge_deploy_merge", "SKIPPED"),
        ("merge_deploy_offer", "GATED"),
    }
)
_INVENTORY_KEYS = frozenset(
    {
        "format_version",
        "decision_ids",
        "merge_stage_ids",
        "holder_leases",
        "stage_log_max_id",
        "history_before",
        "decision_rows_sha256",
        "merge_stage_rows_sha256",
        "publication_identity_sha256",
        "audit_sha256",
    }
)
_HOLDER_KEYS = frozenset({"build_id", "turn", "holder"})
_HEX = frozenset("0123456789abcdef")
_PUBLICATION_COLUMNS = (
    "build_id",
    "feature_id",
    "repo",
    "decided_by",
    "decided_at",
    "target_branch",
    "g_commit",
    "build_tip",
    "j_commit",
    "attempt",
    "result",
    "checked_json",
    "lease_holder",
    "lease_expires_at",
    "turn",
    "lines_json",
    "created_at",
    "updated_at",
)


class MergeRetirementCleanupError(RuntimeError):
    """The inspected inventory no longer permits the one-off cleanup."""


def _refuse(message: str) -> MergeRetirementCleanupError:
    return MergeRetirementCleanupError(f"merge retirement cleanup refused: {message}")


def _lower_hex(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _HEX for character in value)
    )


def _utc(value: object, *, name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise _refuse(f"{name} must be a nonempty UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _refuse(f"{name} is not an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise _refuse(f"{name} must be timezone-aware UTC")
    return parsed


def _positive_sorted_ids(
    value: object, *, name: str, count: int | None = None
) -> tuple[int, ...]:
    if not isinstance(value, list) or (count is not None and len(value) != count):
        expected = f"exactly {count}" if count is not None else "one or more"
        raise _refuse(f"{name} must contain {expected} IDs")
    if not value:
        raise _refuse(f"{name} must contain one or more IDs")
    if any(type(item) is not int or item <= 0 for item in value):
        raise _refuse(f"{name} must contain positive integer IDs")
    answer = tuple(value)
    if answer != tuple(sorted(set(answer))):
        raise _refuse(f"{name} must be unique and ascending")
    return answer


def validate_inventory(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict) or frozenset(raw) != _INVENTORY_KEYS:
        raise _refuse("inventory has unknown or missing keys")
    if type(raw["format_version"]) is not int or raw["format_version"] != 1:
        raise _refuse("inventory format_version must be integer 1")
    decision_ids = _positive_sorted_ids(
        raw["decision_ids"], name="decision_ids", count=EXPECTED_APPROVALS
    )
    merge_stage_ids = _positive_sorted_ids(
        raw["merge_stage_ids"], name="merge_stage_ids"
    )
    holders = raw["holder_leases"]
    if not isinstance(holders, list) or len(holders) != EXPECTED_HOLDERS:
        raise _refuse(f"holder_leases must contain exactly {EXPECTED_HOLDERS} rows")
    normalized_holders: list[dict[str, Any]] = []
    for holder in holders:
        if not isinstance(holder, dict) or frozenset(holder) != _HOLDER_KEYS:
            raise _refuse("holder lease has unknown or missing keys")
        build_id = holder["build_id"]
        turn = holder["turn"]
        owner = holder["holder"]
        if not isinstance(build_id, str) or not build_id:
            raise _refuse("holder build_id must be nonempty text")
        if type(turn) is not int or turn < 0:
            raise _refuse("holder turn must be a nonnegative integer")
        if not isinstance(owner, str) or not owner.strip():
            raise _refuse("holder identity must be nonempty text")
        normalized_holders.append(
            {"build_id": build_id, "turn": turn, "holder": owner}
        )
    if [item["build_id"] for item in normalized_holders] != sorted(
        {item["build_id"] for item in normalized_holders}
    ):
        raise _refuse("holder leases must have unique ascending build IDs")
    if type(raw["stage_log_max_id"]) is not int or raw["stage_log_max_id"] <= 0:
        raise _refuse("stage_log_max_id must be a positive integer")
    _utc(raw["history_before"], name="history_before")
    for name in (
        "decision_rows_sha256",
        "merge_stage_rows_sha256",
        "publication_identity_sha256",
        "audit_sha256",
    ):
        if not _lower_hex(raw[name]):
            raise _refuse(f"{name} must be 64 lowercase hex characters")
    return {
        **raw,
        "decision_ids": decision_ids,
        "merge_stage_ids": merge_stage_ids,
        "holder_leases": tuple(normalized_holders),
    }


def load_inventory(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            raw = strict_json_loads(handle.read())
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise _refuse(f"private inventory cannot be read: {exc}") from exc
    validate_inventory(raw)
    return raw


def _canonical_sha256(rows: list[list[object]]) -> str:
    payload = json.dumps(
        rows,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _rows_by_ids(
    connection: sqlite3.Connection, ids: tuple[int, ...]
) -> list[list[object]]:
    marks = ",".join("?" for _ in ids)
    return [
        list(row)
        for row in connection.execute(
            "SELECT id, build_id, target_identifier, status, started_at, "
            "completed_at, details_json FROM stage_log "
            f"WHERE id IN ({marks}) ORDER BY id",
            ids,
        ).fetchall()
    ]


def _publication_rows(
    connection: sqlite3.Connection, build_ids: tuple[str, ...]
) -> list[list[object]]:
    marks = ",".join("?" for _ in build_ids)
    columns = ", ".join(_PUBLICATION_COLUMNS)
    return [
        list(row)
        for row in connection.execute(
            f"SELECT {columns} FROM publication_records "
            f"WHERE build_id IN ({marks}) ORDER BY build_id",
            build_ids,
        ).fetchall()
    ]


def publication_identity_sha256(rows: list[list[object]]) -> str:
    return _canonical_sha256(rows)


def capture_inventory(
    connection: sqlite3.Connection,
    *,
    history_before: str,
    audit_sha256: str,
    now: datetime,
) -> dict[str, Any]:
    """Capture the complete current 35/6 set; never select a matching subset."""
    _utc(history_before, name="history_before")
    if not _lower_hex(audit_sha256):
        raise _refuse("audit_sha256 must be 64 lowercase hex characters")
    if connection.in_transaction:
        raise _refuse("inventory capture requires a fresh read transaction")
    try:
        connection.execute("BEGIN")
        decision_ids = [
            int(row[0])
            for row in connection.execute(
                "SELECT id FROM stage_log WHERE "
                "target_identifier='merge_deploy_decision' AND status='PASSED' "
                "ORDER BY id"
            )
        ]
        if len(decision_ids) != EXPECTED_APPROVALS:
            raise _refuse(
                f"current ledger has {len(decision_ids)} approved decisions, not 35"
            )
        decision_rows = _rows_by_ids(connection, tuple(decision_ids))
        build_ids = tuple(sorted(str(row[1]) for row in decision_rows))
        if len(set(build_ids)) != EXPECTED_APPROVALS:
            raise _refuse("current approvals are not on 35 distinct builds")
        marks = ",".join("?" for _ in build_ids)
        merge_stage_ids = [
            int(row[0])
            for row in connection.execute(
                "SELECT id FROM stage_log WHERE "
                "substr(target_identifier,1,13)='merge_deploy_' "
                f"AND build_id IN ({marks}) ORDER BY id",
                build_ids,
            )
        ]
        merge_stage_rows = _rows_by_ids(connection, tuple(merge_stage_ids))
        holder_leases = [
            {"build_id": str(row[0]), "turn": int(row[1]), "holder": str(row[2])}
            for row in connection.execute(
                "SELECT build_id, turn, lease_holder FROM publication_records "
                "WHERE lease_holder IS NOT NULL AND trim(lease_holder) <> '' "
                "ORDER BY build_id"
            )
        ]
        if len(holder_leases) != EXPECTED_HOLDERS:
            raise _refuse(
                f"current ledger has {len(holder_leases)} retained holders, not 6"
            )
        holder_builds = tuple(item["build_id"] for item in holder_leases)
        publication_rows = _publication_rows(connection, holder_builds)
        inventory = {
            "format_version": 1,
            "decision_ids": decision_ids,
            "merge_stage_ids": merge_stage_ids,
            "holder_leases": holder_leases,
            "stage_log_max_id": int(
                connection.execute("SELECT coalesce(max(id),0) FROM stage_log").fetchone()[0]
            ),
            "history_before": history_before,
            "decision_rows_sha256": _canonical_sha256(decision_rows),
            "merge_stage_rows_sha256": _canonical_sha256(merge_stage_rows),
            "publication_identity_sha256": publication_identity_sha256(
                publication_rows
            ),
            "audit_sha256": audit_sha256,
        }
        normalized = validate_inventory(inventory)
        _verify_quiet_ledger(connection, normalized, now=now)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return inventory


def _verify_quiet_ledger(
    connection: sqlite3.Connection,
    inventory: dict[str, Any],
    *,
    now: datetime,
) -> tuple[list[list[object]], list[list[object]], list[list[object]], tuple[str, ...]]:
    if connection.execute(
        "SELECT count(*) FROM stage_log WHERE target_identifier = ?",
        (RETIREMENT_TARGET,),
    ).fetchone()[0]:
        raise _refuse("a retirement receipt already exists")
    maximum = connection.execute("SELECT coalesce(max(id), 0) FROM stage_log").fetchone()[0]
    if maximum != inventory["stage_log_max_id"]:
        raise _refuse("stage_log changed after the inspected inventory")
    if connection.execute("SELECT count(*) FROM feature_routing_seeds").fetchone()[0]:
        raise _refuse("feature routing seed rows exist")
    if connection.execute(
        "SELECT count(*) FROM builds WHERE status NOT IN "
        "('COMPLETE','FAILED','CANCELLED','SKIPPED')"
    ).fetchone()[0]:
        raise _refuse("a build is active")
    if connection.execute(
        "SELECT count(*) FROM planning_runs WHERE state NOT IN "
        "('FAILED','CANCELLED','TIMED_OUT','PLANNED_HANDOFF','BUILD_QUEUED')"
    ).fetchone()[0]:
        raise _refuse("a planning run is active")
    if connection.execute(
        "SELECT count(*) FROM work_queue WHERE status IN ('QUEUED','ADMITTED')"
    ).fetchone()[0]:
        raise _refuse("queued or admitted work exists")
    if connection.execute(
        "SELECT count(*) FROM builds WHERE pending_approval_request_id IS NOT NULL "
        "AND trim(pending_approval_request_id) <> ''"
    ).fetchone()[0]:
        raise _refuse("a pending approval card exists")
    if connection.execute(
        "SELECT count(*) FROM stage_log AS offered "
        "WHERE offered.target_identifier='merge_deploy_offer' AND NOT EXISTS ("
        "SELECT 1 FROM stage_log AS decided WHERE decided.build_id=offered.build_id "
        "AND decided.target_identifier='merge_deploy_decision' "
        "AND decided.id > offered.id)"
    ).fetchone()[0]:
        raise _refuse("an unresolved merge offer exists")

    decision_rows = _rows_by_ids(connection, inventory["decision_ids"])
    merge_stage_rows = _rows_by_ids(connection, inventory["merge_stage_ids"])
    if len(decision_rows) != EXPECTED_APPROVALS or not merge_stage_rows:
        raise _refuse("an inspected decision or merge-deploy stage is missing")
    if _canonical_sha256(decision_rows) != inventory["decision_rows_sha256"]:
        raise _refuse("an inspected decision row changed")
    if _canonical_sha256(merge_stage_rows) != inventory["merge_stage_rows_sha256"]:
        raise _refuse("an inspected merge-deploy stage row changed")
    all_approved_ids = tuple(
        row[0]
        for row in connection.execute(
            "SELECT id FROM stage_log WHERE target_identifier='merge_deploy_decision' "
            "AND status='PASSED' ORDER BY id"
        )
    )
    if all_approved_ids != inventory["decision_ids"]:
        raise _refuse("approved decision membership changed")
    cutoff = _utc(inventory["history_before"], name="history_before")
    decision_builds: dict[str, list[int]] = {}
    for row in decision_rows:
        row_id, build_id, target, status, _started, completed, details_json = row
        if target != "merge_deploy_decision" or status != "PASSED":
            raise _refuse(f"decision {row_id} no longer names an approval")
        if _utc(completed, name=f"decision {row_id} completed_at") >= cutoff:
            raise _refuse(f"decision {row_id} is not in the inspected old history")
        try:
            details = strict_json_loads(details_json)
        except (TypeError, json.JSONDecodeError, ValueError) as exc:
            raise _refuse(f"decision {row_id} details are malformed") from exc
        decision = details.get("merge_decision") if isinstance(details, dict) else None
        if not isinstance(decision, dict) or decision.get("decision") != "approve":
            raise _refuse(f"decision {row_id} is not an explicit approval")
        if "execution_attempt_version" in decision or "execution_attempt_id" in decision:
            raise _refuse(f"decision {row_id} is versioned or partially versioned")
        decision_builds.setdefault(str(build_id), []).append(int(row_id))
    if len(decision_builds) != EXPECTED_APPROVALS:
        raise _refuse("approved decisions are not on 35 distinct builds")

    stages_by_build: dict[str, list[list[object]]] = {
        build_id: [] for build_id in decision_builds
    }
    for row in merge_stage_rows:
        row_id, build_id, target, status, _started, completed, stage_details = row
        if (str(target), str(status)) not in _ALLOWED_HISTORICAL_STAGE_STATUSES:
            raise _refuse(f"merge-deploy stage {row_id} has unexpected identity or status")
        if str(build_id) not in decision_builds:
            raise _refuse(f"merge-deploy stage {row_id} is not for an approved build")
        if _utc(completed, name=f"merge-deploy stage {row_id} completed_at") >= cutoff:
            raise _refuse(f"merge-deploy stage {row_id} is not old history")
        try:
            parsed_stage = strict_json_loads(stage_details)
        except (TypeError, json.JSONDecodeError, ValueError) as exc:
            raise _refuse(f"merge-deploy stage {row_id} details are malformed") from exc
        if not isinstance(parsed_stage, dict):
            raise _refuse(f"merge-deploy stage {row_id} details are not an object")
        if target == "merge_deploy_executor":
            report_attempt = parsed_stage.get("merge_decision")
            if report_attempt is not None and not isinstance(report_attempt, dict):
                raise _refuse(f"report {row_id} merge decision details are malformed")
            if isinstance(report_attempt, dict) and (
                "execution_attempt_version" in report_attempt
                or "execution_attempt_id" in report_attempt
            ):
                raise _refuse(f"report {row_id} is versioned or partially versioned")
        stages_by_build[str(build_id)].append(row)
    latest_counts = {pair: 0 for pair in EXPECTED_LATEST_OUTCOMES}
    for build_id, decisions in decision_builds.items():
        decision_id = decisions[0]
        later_stages = [row for row in stages_by_build[build_id] if row[0] > decision_id]
        if not later_stages:
            raise _refuse(f"approved decision {decision_id} has no later merge stage")
        latest = max(later_stages, key=lambda row: row[0])
        outcome = (str(latest[2]), str(latest[3]))
        if outcome not in latest_counts:
            raise _refuse(
                f"approved decision {decision_id} has unexpected latest outcome {outcome}"
            )
        latest_counts[outcome] += 1
    if latest_counts != EXPECTED_LATEST_OUTCOMES:
        raise _refuse("latest merge-deploy outcome distribution changed")
    build_ids = tuple(sorted(decision_builds))
    marks = ",".join("?" for _ in build_ids)
    all_merge_stage_ids = tuple(
        row[0]
        for row in connection.execute(
            "SELECT id FROM stage_log WHERE substr(target_identifier,1,13)="
            "'merge_deploy_' "
            f"AND build_id IN ({marks}) ORDER BY id",
            build_ids,
        )
    )
    if all_merge_stage_ids != inventory["merge_stage_ids"]:
        raise _refuse("merge-deploy stage membership changed")
    if connection.execute(
        f"SELECT count(*) FROM builds WHERE build_id IN ({marks}) "
        "AND status NOT IN ('COMPLETE','FAILED','CANCELLED','SKIPPED')",
        build_ids,
    ).fetchone()[0]:
        raise _refuse("a linked build is not terminal")

    holder_specs = inventory["holder_leases"]
    holder_builds = tuple(item["build_id"] for item in holder_specs)
    if not set(holder_builds).issubset(decision_builds):
        raise _refuse("a retained holder is not one of the approved builds")
    live_holders = tuple(
        (str(build_id), int(turn), str(holder))
        for build_id, turn, holder in connection.execute(
            "SELECT build_id, turn, lease_holder FROM publication_records "
            "WHERE lease_holder IS NOT NULL AND trim(lease_holder) <> '' "
            "ORDER BY build_id"
        )
    )
    expected_holders = tuple(
        (item["build_id"], item["turn"], item["holder"]) for item in holder_specs
    )
    if live_holders != expected_holders:
        raise _refuse("retained holder membership or CAS identity changed")
    if connection.execute(
        "SELECT count(*) FROM publication_records WHERE result IS NULL"
    ).fetchone()[0]:
        raise _refuse("a publication result is still pending")
    publication_rows = _publication_rows(connection, holder_builds)
    if len(publication_rows) != EXPECTED_HOLDERS:
        raise _refuse("an inspected holder publication row is missing")
    for row in publication_rows:
        if row[10] is None:
            raise _refuse(f"holder {row[0]} has no publication result")
        expires = _utc(row[13], name=f"holder {row[0]} lease_expires_at")
        if expires >= now:
            raise _refuse(f"holder {row[0]} lease has not expired")
    if publication_identity_sha256(publication_rows) != inventory[
        "publication_identity_sha256"
    ]:
        raise _refuse("holder publication identity changed")
    return decision_rows, merge_stage_rows, publication_rows, build_ids


def retire_inspected_history(
    connection: sqlite3.Connection,
    raw_inventory: object,
    *,
    now: datetime,
    publication_store_factory: Callable[[sqlite3.Connection], Any] = PublicationRecordStore,
) -> dict[str, int]:
    """Reverify, append 35 receipts, release six leases, or roll back all."""
    inventory = validate_inventory(raw_inventory)
    if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
        raise ValueError("now must be timezone-aware UTC")
    if connection.in_transaction:
        raise _refuse("cleanup requires sole ownership of its transaction")
    timestamp = now.isoformat()
    try:
        connection.execute("BEGIN IMMEDIATE")
        decision_rows, merge_stage_rows, publication_before, build_ids = (
            _verify_quiet_ledger(connection, inventory, now=now)
        )
        decisions_by_build: dict[str, list[list[object]]] = {
            build_id: [] for build_id in build_ids
        }
        for row in decision_rows:
            decisions_by_build[str(row[1])].append(row)
        for build_id in build_ids:
            rows = decisions_by_build[build_id]
            details = {
                "merge_retirement": {
                    "format_version": 1,
                    "decision_ids": [int(row[0]) for row in rows],
                    "decision_identity_sha256": decision_identity_sha256(rows),
                    "reason": RETIREMENT_REASON,
                    "retired_at": timestamp,
                    "audit_sha256": inventory["audit_sha256"],
                }
            }
            connection.execute(
                "INSERT INTO stage_log (build_id, stage_label, target_kind, "
                "target_identifier, status, gate_mode, coach_score, "
                "threshold_applied, started_at, completed_at, duration_secs, "
                "details_json) VALUES (?, ?, 'local_tool', ?, 'SKIPPED', "
                "NULL, NULL, NULL, ?, ?, 0, ?)",
                (
                    build_id,
                    RETIREMENT_STAGE_LABEL,
                    RETIREMENT_TARGET,
                    timestamp,
                    timestamp,
                    json.dumps(
                        details,
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ),
                ),
            )
        store = publication_store_factory(connection)
        for holder in inventory["holder_leases"]:
            if not store.release_lease(
                build_id=holder["build_id"],
                turn=holder["turn"],
                holder=holder["holder"],
                now=now,
            ):
                raise _refuse(f"lease CAS failed for {holder['build_id']}")

        if retired_decision_ids(connection) != frozenset(inventory["decision_ids"]):
            raise _refuse("postcondition did not retire the exact decisions")
        if _rows_by_ids(connection, inventory["decision_ids"]) != decision_rows:
            raise _refuse("an original decision changed")
        if (
            _rows_by_ids(connection, inventory["merge_stage_ids"])
            != merge_stage_rows
        ):
            raise _refuse("an original merge-deploy stage changed")
        holder_builds = tuple(item["build_id"] for item in inventory["holder_leases"])
        publication_after = _publication_rows(connection, holder_builds)
        for before, after in zip(publication_before, publication_after, strict=True):
            expected = list(before)
            expected[12] = None
            expected[13] = None
            expected[17] = timestamp
            if after != expected:
                raise _refuse(f"publication row {before[0]} changed outside release columns")
        if connection.execute(
            "SELECT count(*) FROM publication_records WHERE lease_holder IS NOT NULL "
            "AND trim(lease_holder) <> ''"
        ).fetchone()[0]:
            raise _refuse("a retained publication holder remains")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return {"retirement_receipts": len(build_ids), "released_holders": EXPECTED_HOLDERS}


def verify_inspected_history(
    connection: sqlite3.Connection, raw_inventory: object, *, now: datetime
) -> dict[str, int]:
    """Read-only revalidation used immediately before the verified backup."""
    inventory = validate_inventory(raw_inventory)
    if connection.in_transaction:
        raise _refuse("verification requires a fresh read transaction")
    try:
        connection.execute("BEGIN")
        _decision_rows, _merge_stage_rows, _publication_rows_value, builds = (
            _verify_quiet_ledger(connection, inventory, now=now)
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return {"approved_builds": len(builds), "retained_holders": EXPECTED_HOLDERS}

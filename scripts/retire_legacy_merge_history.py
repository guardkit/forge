#!/usr/bin/env python3
"""Guarded one-off retirement of the inspected 35/6 pre-routing history.

This command performs no migration and makes no claim that SQLite proves
process absence.  Apply mode requires a private operator observation file,
verified backup, exact private ledger inventory, and a reviewed source ID.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from forge.adapters.sqlite.connect import connect_writer
from forge.lifecycle.merge_retirement_cleanup import (
    MergeRetirementCleanupError,
    capture_inventory,
    load_inventory,
    retire_inspected_history,
    verify_inspected_history,
)
from forge.lifecycle.merge_retirement import strict_json_loads

_QUIESCENCE_KEYS = frozenset(
    {
        "format_version",
        "observed_at",
        "producers_stopped",
        "coordinator_callbacks_absent",
        "standalone_clis_absent",
        "remote_tasks_absent",
        "runner_tasks_absent",
        "model_tasks_absent",
        "deployment_work_absent",
        "paused_old_tasks",
        "deployment_observed",
        "observation_provenance",
    }
)
_TRUE_KEYS = _QUIESCENCE_KEYS - {
    "format_version",
    "observed_at",
    "paused_old_tasks",
    "deployment_observed",
    "observation_provenance",
}


def _private_file(path: Path, *, what: str) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise MergeRetirementCleanupError(
            f"{what} must be private (chmod 600); mode is {mode:o}"
        )


def _load_quiescence(path: Path, *, now: datetime) -> dict[str, Any]:
    _private_file(path, what="quiescence observation")
    try:
        raw = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise MergeRetirementCleanupError(
            f"quiescence observation cannot be read: {exc}"
        ) from exc
    if not isinstance(raw, dict) or frozenset(raw) != _QUIESCENCE_KEYS:
        raise MergeRetirementCleanupError(
            "quiescence observation has unknown or missing keys"
        )
    if type(raw["format_version"]) is not int or raw["format_version"] != 1:
        raise MergeRetirementCleanupError("quiescence format_version must be integer 1")
    if any(raw[name] is not True for name in _TRUE_KEYS):
        raise MergeRetirementCleanupError(
            "all stopped/absent quiescence observations must be true"
        )
    if type(raw["paused_old_tasks"]) is not int or raw["paused_old_tasks"] != 0:
        raise MergeRetirementCleanupError("a surviving paused old task blocks cleanup")
    if not isinstance(raw["deployment_observed"], str) or not raw[
        "deployment_observed"
    ].strip():
        raise MergeRetirementCleanupError("deployment_observed must name what was checked")
    provenance = raw["observation_provenance"]
    if (
        not isinstance(provenance, list)
        or not provenance
        or any(not isinstance(item, str) or not item.strip() for item in provenance)
    ):
        raise MergeRetirementCleanupError(
            "observation_provenance must contain the attended commands/evidence"
        )
    observed = raw["observed_at"]
    if not isinstance(observed, str):
        raise MergeRetirementCleanupError("quiescence observed_at must be UTC text")
    try:
        moment = datetime.fromisoformat(observed.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MergeRetirementCleanupError(
            "quiescence observed_at is not an ISO timestamp"
        ) from exc
    if moment.tzinfo is None or moment.utcoffset() != timezone.utc.utcoffset(moment):
        raise MergeRetirementCleanupError("quiescence observed_at must be UTC")
    age = now - moment
    if age < timedelta(0) or age > timedelta(minutes=30):
        raise MergeRetirementCleanupError(
            "quiescence observation must be from the last 30 minutes"
        )
    return raw


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verified_backup(connection: sqlite3.Connection, path: Path) -> str:
    if path.exists():
        raise MergeRetirementCleanupError(f"backup already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    destination = sqlite3.connect(path)
    try:
        connection.backup(destination)
        answer = destination.execute("PRAGMA integrity_check").fetchone()
        if answer is None or answer[0] != "ok":
            raise MergeRetirementCleanupError("backup integrity_check did not return ok")
    finally:
        destination.close()
    os.chmod(path, 0o600)
    return _sha256(path)


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, indent=2)
        handle.write("\n")


def _rewrite_private_json(handle: Any, payload: dict[str, Any]) -> None:
    handle.seek(0)
    json.dump(payload, handle, sort_keys=True, indent=2)
    handle.write("\n")
    handle.truncate()
    handle.flush()
    os.fsync(handle.fileno())


def _arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--capture-inventory", type=Path)
    parser.add_argument("--history-before")
    parser.add_argument("--audit-sha256")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--audit-output", type=Path)
    parser.add_argument("--quiescence", type=Path)
    parser.add_argument("--reviewed-source-commit")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _arguments(list(sys.argv[1:] if argv is None else argv))
    if not args.database.is_file():
        raise MergeRetirementCleanupError(f"database does not exist: {args.database}")
    now = datetime.now(timezone.utc)
    connection = connect_writer(args.database)
    try:
        version = connection.execute("SELECT max(version) FROM schema_version").fetchone()[0]
        if version != 18:
            raise MergeRetirementCleanupError(
                f"schema version must already be exactly 18; found {version!r}"
            )
        if args.capture_inventory is not None:
            if args.inventory is not None or args.apply:
                raise MergeRetirementCleanupError(
                    "--capture-inventory cannot be combined with --inventory or --apply"
                )
            if not args.history_before or not args.audit_sha256:
                raise MergeRetirementCleanupError(
                    "inventory capture requires --history-before and --audit-sha256"
                )
            inventory = capture_inventory(
                connection,
                history_before=args.history_before,
                audit_sha256=args.audit_sha256,
                now=now,
            )
            _write_private_json(args.capture_inventory, inventory)
            print(
                json.dumps(
                    {
                        "mode": "captured-inventory",
                        "approved_builds": 35,
                        "retained_holders": 6,
                        "output": str(args.capture_inventory),
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.inventory is None:
            raise MergeRetirementCleanupError(
                "provide --capture-inventory or an inspected --inventory"
            )
        _private_file(args.inventory, what="private inventory")
        inventory = load_inventory(str(args.inventory))
        checked = verify_inspected_history(connection, inventory, now=now)
        if not args.apply:
            print(json.dumps({"mode": "dry-run", **checked}, sort_keys=True))
            return 0
        if not all(
            (
                args.backup,
                args.audit_output,
                args.quiescence,
                args.reviewed_source_commit,
            )
        ):
            raise MergeRetirementCleanupError(
                "apply requires --backup, --audit-output, --quiescence and "
                "--reviewed-source-commit"
            )
        if len(args.reviewed_source_commit) != 40 or any(
            character not in "0123456789abcdef"
            for character in args.reviewed_source_commit
        ):
            raise MergeRetirementCleanupError(
                "reviewed source commit must be 40 lowercase hex characters"
            )
        quiescence = _load_quiescence(args.quiescence, now=now)
        backup_sha256 = _verified_backup(connection, args.backup)
        prepared_changes = {"retirement_receipts": 35, "released_holders": 6}
        audit = {
            "format_version": 1,
            "operation": "retire-pre-routing-merge-history",
            "database": str(args.database.resolve()),
            "backup": str(args.backup.resolve()),
            "backup_sha256": backup_sha256,
            "inventory_audit_sha256": inventory["audit_sha256"],
            "inventory_file_sha256": _sha256(args.inventory),
            "inventory": str(args.inventory.resolve()),
            "reviewed_source_commit": args.reviewed_source_commit,
            "retired_at": now.isoformat(),
            "quiescence": quiescence,
            "changes": prepared_changes,
            "status": "pending__",
            "activation_requirement": (
                "start the reviewed new release before reopening intake; old-code "
                "rollback cannot enforce these retirement receipts"
            ),
        }
        args.audit_output.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            args.audit_output, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600
        )
        with os.fdopen(descriptor, "w+", encoding="utf-8") as audit_handle:
            _rewrite_private_json(audit_handle, audit)
            try:
                changed = retire_inspected_history(connection, inventory, now=now)
            except BaseException:
                audit["status"] = "rolledback"
                _rewrite_private_json(audit_handle, audit)
                raise
            audit["status"] = "committed"
            try:
                _rewrite_private_json(audit_handle, audit)
            except OSError as exc:
                raise MergeRetirementCleanupError(
                    "database retirement committed; audit finalization failed; "
                    "inspect receipts, backup and pending audit; do not retry apply blindly"
                ) from exc
        print(json.dumps({"mode": "applied", **changed}, sort_keys=True))
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MergeRetirementCleanupError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc

"""Neutral, real-shaped planning handoff ledger fixture."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from forge.lifecycle.planning_handoff_retirement import (
    RETIREMENT_STAGE_LABEL,
    retirement_details_json,
)


CORRELATION = "planning-fixture"
HISTORY_BEFORE = "2026-07-16T00:00:00+00:00"
RETIRED_AT = "2026-10-09T12:00:00+00:00"
AUDIT_SHA256 = "a" * 64


def make_planning_ledger(
    path: Path, *, version: int = 17
) -> tuple[sqlite3.Connection, tuple[int, ...]]:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_version(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        CREATE TABLE planning_runs(
          correlation_id TEXT PRIMARY KEY, state TEXT NOT NULL,
          originating_user TEXT NOT NULL, expected_approver TEXT NOT NULL,
          request_text TEXT NOT NULL, target_repo TEXT, triggered_by TEXT NOT NULL,
          originating_adapter TEXT, parent_request_id TEXT,
          pending_approval_request_id TEXT, defer_count INTEGER NOT NULL,
          paused_at TEXT, escalated_at TEXT, handoff_branch TEXT, handoff_path TEXT,
          queued_at TEXT NOT NULL, started_at TEXT, completed_at TEXT, error TEXT,
          start_commit TEXT, target_branch TEXT, memory_project TEXT, launch_settings TEXT
        );
        CREATE TABLE planning_run_events(
          id INTEGER PRIMARY KEY AUTOINCREMENT, correlation_id TEXT NOT NULL,
          stage_label TEXT NOT NULL, status TEXT NOT NULL, gate_mode TEXT,
          coach_score REAL, actor_identity TEXT, details_json TEXT,
          recorded_at TEXT NOT NULL
        );
        CREATE TABLE builds(build_id TEXT PRIMARY KEY, correlation_id TEXT);
        CREATE TABLE work_queue(id INTEGER PRIMARY KEY, correlation_id TEXT);
        """
    )
    if version == 18:
        connection.execute(
            "CREATE TABLE feature_routing_seeds("
            "feature_routing_id TEXT PRIMARY KEY, state TEXT NOT NULL)"
        )
    connection.execute(
        "INSERT INTO schema_version VALUES (?, '2026-10-09T00:00:00+00:00')",
        (version,),
    )
    connection.execute(
        "INSERT INTO planning_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            CORRELATION,
            "BUILD_QUEUED",
            "fixture-owner",
            "fixture-owner",
            "Build the fixture",
            "fixture/repo",
            "slack",
            "slack",
            "fixture-parent",
            None,
            0,
            None,
            None,
            "feat/fixture",
            "docs/features/fixture.yaml",
            "2026-07-15T09:00:00+00:00",
            "2026-07-15T09:01:00+00:00",
            "2026-07-15T10:20:00+00:00",
            None,
            "1" * 40,
            "main",
            "fixture",
            "[]",
        ),
    )
    event_ids: list[int] = []
    for index in range(18):
        event_ids.append(
            connection.execute(
                "INSERT INTO planning_run_events("
                "correlation_id,stage_label,status,actor_identity,details_json,recorded_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    CORRELATION,
                    f"fixture-stage-{index}",
                    "PASSED",
                    "fixture-actor",
                    json.dumps({"index": index}, separators=(",", ":")),
                    f"2026-07-15T10:{index:02d}:00+00:00",
                ),
            ).lastrowid
        )
    trigger = {
        "feature_id": "FEAT-FIXTURE",
        "build_id": None,
        "target_repo": "fixture/repo",
        "branch": "feat/fixture",
    }
    event_ids.append(
        connection.execute(
            "INSERT INTO planning_run_events("
            "correlation_id,stage_label,status,actor_identity,details_json,recorded_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                CORRELATION,
                "build-queued",
                "approved",
                "fixture-actor",
                json.dumps(trigger, separators=(",", ":")),
                "2026-07-15T10:18:00+00:00",
            ),
        ).lastrowid
    )
    event_ids.append(
        connection.execute(
            "INSERT INTO planning_run_events("
            "correlation_id,stage_label,status,actor_identity,details_json,recorded_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                CORRELATION,
                "build-queued",
                "BUILD_QUEUED",
                "fixture-actor",
                None,
                "2026-07-15T10:19:00+00:00",
            ),
        ).lastrowid
    )
    connection.commit()
    return connection, tuple(event_ids)


def add_planning_retirement(
    connection: sqlite3.Connection,
    event_ids: tuple[int, ...],
    *,
    actor: str = "fixture-administrator",
    details: str | None = None,
    stage_label: str = RETIREMENT_STAGE_LABEL,
    status: str = "SKIPPED",
    correlation_id: str = CORRELATION,
) -> int:
    if details is None:
        details = retirement_details_json(
            connection,
            correlation_id=CORRELATION,
            original_event_ids=event_ids,
            history_before=HISTORY_BEFORE,
            audit_sha256=AUDIT_SHA256,
            retired_at=RETIRED_AT,
        )
    return int(
        connection.execute(
            "INSERT INTO planning_run_events("
            "correlation_id,stage_label,status,gate_mode,coach_score,actor_identity,"
            "details_json,recorded_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                correlation_id,
                stage_label,
                status,
                None,
                None,
                actor,
                details,
                RETIRED_AT,
            ),
        ).lastrowid
    )

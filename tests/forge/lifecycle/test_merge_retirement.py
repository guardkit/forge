from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from forge.adapters.sqlite.connect import connect_writer
from forge.cli.history import render_markdown
from forge.lifecycle import migrations
from forge.lifecycle.feature_routing import router_restart_blockers
from forge.lifecycle.merge_retirement import (
    MergeRetired,
    MergeRetirementError,
    RETIREMENT_REASON,
    RETIREMENT_STAGE_LABEL,
    RETIREMENT_TARGET,
    decision_identity_sha256,
    refuse_retired_build,
    retired_decision_ids,
    strict_json_loads,
)
from forge.lifecycle.merge_retirement_cleanup import (
    MergeRetirementCleanupError,
    publication_identity_sha256,
    retire_inspected_history,
)
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.pipeline.publication_record import PublicationRecordStore

OLD_TIME = "2026-01-01T00:00:00+00:00"
CUTOFF = "2026-10-08T13:22:53+00:00"
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
PUBLICATION_COLUMNS = (
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


def _stage(
    connection: sqlite3.Connection,
    build_id: str,
    target: str,
    status: str,
    details: object,
    *,
    label: str = "merge",
    at: str = OLD_TIME,
) -> int:
    cursor = connection.execute(
        "INSERT INTO stage_log (build_id, stage_label, target_kind, "
        "target_identifier, status, started_at, completed_at, duration_secs, "
        "details_json) VALUES (?, ?, 'local_tool', ?, ?, ?, ?, 0, ?)",
        (build_id, label, target, status, at, at, json.dumps(details, sort_keys=True)),
    )
    return int(cursor.lastrowid)


def _identity_rows(connection: sqlite3.Connection, ids: list[int]) -> list[list[object]]:
    marks = ",".join("?" for _ in ids)
    return [
        list(row)
        for row in connection.execute(
            "SELECT id, build_id, target_identifier, status, started_at, "
            "completed_at, details_json FROM stage_log "
            f"WHERE id IN ({marks}) ORDER BY id",
            ids,
        )
    ]


def _publication_rows(
    connection: sqlite3.Connection, build_ids: list[str]
) -> list[list[object]]:
    marks = ",".join("?" for _ in build_ids)
    return [
        list(row)
        for row in connection.execute(
            f"SELECT {', '.join(PUBLICATION_COLUMNS)} FROM publication_records "
            f"WHERE build_id IN ({marks}) ORDER BY build_id",
            build_ids,
        )
    ]


def _ledger(tmp_path: Path) -> sqlite3.Connection:
    connection = connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(connection)
    return connection


def _insert_build(connection: sqlite3.Connection, number: int) -> str:
    build_id = f"build-old-{number:02d}"
    connection.execute(
        "INSERT INTO builds (build_id, feature_id, repo, branch, "
        "feature_yaml_path, status, triggered_by, correlation_id, queued_at, "
        "completed_at) VALUES (?, ?, 'org/repo', ?, 'f.yaml', 'COMPLETE', "
        "'forge-internal', ?, ?, ?)",
        (
            build_id,
            f"FEAT-OLD-{number:02d}",
            f"autobuild/FEAT-OLD-{number:02d}",
            f"old-{number:02d}",
            OLD_TIME,
            OLD_TIME,
        ),
    )
    return build_id


def _real_shaped_history(
    tmp_path: Path,
) -> tuple[sqlite3.Connection, dict[str, Any], list[str]]:
    connection = _ledger(tmp_path)
    decision_ids: list[int] = []
    builds: list[str] = []
    extra_reports = {
        0: ["FAILED"],
        1: ["PASSED"],
        2: ["FAILED"],
        13: ["FAILED"],
        14: ["FAILED"],
    }
    for number in range(35):
        build_id = _insert_build(connection, number)
        builds.append(build_id)
        _stage(connection, build_id, "merge_deploy_offer", "GATED", {})
        decision_ids.append(
            _stage(
                connection,
                build_id,
                "merge_deploy_decision",
                "PASSED",
                {
                    "merge_decision": {
                        "decision": "approve",
                        "decided_by": "owner",
                        "request_id": f"merge-{build_id}",
                    }
                },
            )
        )
        for status in extra_reports.get(number, []):
            _stage(connection, build_id, "merge_deploy_executor", status, {})
        if number < 13:
            _stage(connection, build_id, "merge_deploy_executor", "PASSED", {})
        elif number < 31:
            _stage(connection, build_id, "merge_deploy_executor", "FAILED", {})
        else:
            _stage(connection, build_id, "merge_deploy_merge", "GATED", {})

    holder_builds = builds[:6]
    for number, build_id in enumerate(holder_builds):
        connection.execute(
            "INSERT INTO publication_records (build_id, feature_id, repo, "
            "decided_by, decided_at, target_branch, g_commit, build_tip, "
            "j_commit, attempt, result, checked_json, lease_holder, "
            "lease_expires_at, turn, lines_json, created_at, updated_at) "
            "VALUES (?, ?, 'org/repo', 'owner', ?, 'main', ?, ?, ?, 2, "
            "'publication-pending', ?, ?, ?, ?, ?, ?, ?)",
            (
                build_id,
                f"FEAT-OLD-{number:02d}",
                OLD_TIME,
                "a" * 40,
                "b" * 40,
                "c" * 40,
                json.dumps({"passed": True}),
                f"old-worker-{number}",
                "2026-01-02T00:00:00+00:00",
                number + 1,
                json.dumps(["kept", str(number)]),
                OLD_TIME,
                OLD_TIME,
            ),
        )
    decision_rows = _identity_rows(connection, decision_ids)
    merge_stage_ids = [
        int(row[0])
        for row in connection.execute(
            "SELECT id FROM stage_log WHERE substr(target_identifier,1,13)="
            "'merge_deploy_' ORDER BY id"
        )
    ]
    merge_stage_rows = _identity_rows(connection, merge_stage_ids)
    publication_rows = _publication_rows(connection, holder_builds)
    inventory = {
        "format_version": 1,
        "decision_ids": decision_ids,
        "merge_stage_ids": merge_stage_ids,
        "holder_leases": [
            {
                "build_id": build_id,
                "turn": number + 1,
                "holder": f"old-worker-{number}",
            }
            for number, build_id in enumerate(holder_builds)
        ],
        "stage_log_max_id": merge_stage_ids[-1],
        "history_before": CUTOFF,
        "decision_rows_sha256": decision_identity_sha256(decision_rows),
        "merge_stage_rows_sha256": decision_identity_sha256(merge_stage_rows),
        "publication_identity_sha256": publication_identity_sha256(
            publication_rows
        ),
        "audit_sha256": "d" * 64,
    }
    return connection, inventory, builds


def _insert_receipt(
    connection: sqlite3.Connection,
    build_id: str,
    decision_ids: list[int],
    *,
    mutate: Any = None,
) -> int:
    rows = _identity_rows(connection, decision_ids)
    details = {
        "merge_retirement": {
            "format_version": 1,
            "decision_ids": decision_ids,
            "decision_identity_sha256": decision_identity_sha256(rows),
            "reason": RETIREMENT_REASON,
            "retired_at": NOW.isoformat(),
            "audit_sha256": "e" * 64,
        }
    }
    if mutate is not None:
        mutate(details)
    return _stage(
        connection,
        build_id,
        RETIREMENT_TARGET,
        "SKIPPED",
        details,
        label=RETIREMENT_STAGE_LABEL,
        at=NOW.isoformat(),
    )


def test_exact_receipt_retires_only_its_original_decision(tmp_path: Path) -> None:
    connection = _ledger(tmp_path)
    build_id = _insert_build(connection, 1)
    first = _stage(
        connection,
        build_id,
        "merge_deploy_decision",
        "PASSED",
        {"merge_decision": {"decision": "approve"}},
    )
    _insert_receipt(connection, build_id, [first])

    assert retired_decision_ids(connection) == frozenset({first})
    with pytest.raises(MergeRetired, match="permanently retired"):
        refuse_retired_build(connection, build_id)
    assert router_restart_blockers(connection) == ()

    later = _stage(
        connection,
        build_id,
        "merge_deploy_decision",
        "PASSED",
        {"merge_decision": {"decision": "approve"}},
    )
    assert later not in retired_decision_ids(connection)
    assert router_restart_blockers(connection) == (
        f"merge-approved-pending:{build_id}",
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda receipt: receipt.update(extra=True),
        lambda receipt: receipt["merge_retirement"].update(format_version=True),
        lambda receipt: receipt["merge_retirement"].update(format_version=1.0),
        lambda receipt: receipt["merge_retirement"].update(
            decision_identity_sha256="0" * 64
        ),
        lambda receipt: receipt["merge_retirement"].update(decision_ids=[]),
        lambda receipt: receipt["merge_retirement"].update(
            decision_ids=list(range(1, 258))
        ),
        lambda receipt: receipt["merge_retirement"].update(
            retired_at="2026-10-09T12:00:01+00:00"
        ),
    ],
)
def test_malformed_retirement_looking_rows_fail_closed(
    tmp_path: Path, mutation: Any
) -> None:
    connection = _ledger(tmp_path)
    build_id = _insert_build(connection, 1)
    decision = _stage(
        connection,
        build_id,
        "merge_deploy_decision",
        "PASSED",
        {"merge_decision": {"decision": "approve"}},
    )
    _insert_receipt(connection, build_id, [decision], mutate=mutation)

    with pytest.raises(MergeRetirementError):
        retired_decision_ids(connection)
    with pytest.raises(MergeRetirementError):
        refuse_retired_build(connection, build_id)


def test_versioned_decision_and_duplicate_receipt_are_refused(tmp_path: Path) -> None:
    connection = _ledger(tmp_path)
    build_id = _insert_build(connection, 1)
    decision = _stage(
        connection,
        build_id,
        "merge_deploy_decision",
        "PASSED",
        {
            "merge_decision": {
                "decision": "approve",
                "execution_attempt_version": 1,
            }
        },
    )
    _insert_receipt(connection, build_id, [decision])
    with pytest.raises(MergeRetirementError, match="partially versioned"):
        retired_decision_ids(connection)

    connection.execute("DELETE FROM stage_log WHERE target_identifier=?", (RETIREMENT_TARGET,))
    first = _stage(
        connection,
        build_id,
        "merge_deploy_decision",
        "PASSED",
        {"merge_decision": {"decision": "approve"}},
    )
    _insert_receipt(connection, build_id, [first])
    _insert_receipt(connection, build_id, [first])
    with pytest.raises(MergeRetirementError, match="duplicate retirements"):
        retired_decision_ids(connection)


def test_receipt_cannot_name_a_decision_from_another_build(tmp_path: Path) -> None:
    connection = _ledger(tmp_path)
    first_build = _insert_build(connection, 1)
    second_build = _insert_build(connection, 2)
    decision = _stage(
        connection,
        second_build,
        "merge_deploy_decision",
        "PASSED",
        {"merge_decision": {"decision": "approve"}},
    )
    _insert_receipt(connection, first_build, [decision])
    with pytest.raises(MergeRetirementError, match="different build"):
        retired_decision_ids(connection)


def test_duplicate_json_keys_cannot_hide_versioning_or_receipt_fields(
    tmp_path: Path,
) -> None:
    connection = _ledger(tmp_path)
    build_id = _insert_build(connection, 1)
    decision = _stage(
        connection,
        build_id,
        "merge_deploy_decision",
        "PASSED",
        {"merge_decision": {"decision": "approve"}},
    )
    raw_decision = (
        '{"merge_decision":{"decision":"approve",'
        '"execution_attempt_version":1,"execution_attempt_id":"'
        + "a" * 32
        + '"},"merge_decision":{"decision":"approve"}}'
    )
    connection.execute(
        "UPDATE stage_log SET details_json=? WHERE id=?", (raw_decision, decision)
    )
    receipt = _insert_receipt(connection, build_id, [decision])
    with pytest.raises(MergeRetirementError, match="malformed details"):
        retired_decision_ids(connection)

    connection.execute("DELETE FROM stage_log WHERE id=?", (receipt,))
    receipt = _insert_receipt(connection, build_id, [decision])
    raw_receipt = connection.execute(
        "SELECT details_json FROM stage_log WHERE id=?", (receipt,)
    ).fetchone()[0]
    duplicate = raw_receipt.replace(
        '"format_version": 1', '"format_version":true,"format_version":1'
    )
    connection.execute(
        "UPDATE stage_log SET details_json=? WHERE id=?", (duplicate, receipt)
    )
    with pytest.raises(MergeRetirementError, match="not valid JSON"):
        retired_decision_ids(connection)


@pytest.mark.parametrize("constant", ("NaN", "Infinity", "-Infinity"))
def test_nonstandard_json_constants_are_never_eligible(constant: str) -> None:
    with pytest.raises(ValueError, match="non-standard JSON constant"):
        strict_json_loads(
            '{"merge_decision":{"decision":"approve","extra":'
            + constant
            + "}}"
        )


def test_non_utf8_receipt_text_is_a_fail_closed_parser_error(
    tmp_path: Path,
) -> None:
    from forge.lifecycle.merge_retirement import _validated_ids_for_row

    connection = _ledger(tmp_path)
    with pytest.raises(MergeRetirementError, match="not valid UTF-8"):
        _validated_ids_for_row(
            connection,
            (
                1,
                "build-old-01",
                RETIREMENT_STAGE_LABEL,
                "local_tool",
                "SKIPPED",
                NOW.isoformat(),
                NOW.isoformat(),
                0.0,
                "\ud800",
            ),
        )


def test_real_shaped_36_report_cleanup_preserves_all_rows_and_releases_only_leases(
    tmp_path: Path,
) -> None:
    connection, inventory, builds = _real_shaped_history(tmp_path)
    assert connection.execute(
        "SELECT count(*) FROM stage_log WHERE target_identifier="
        "'merge_deploy_executor'"
    ).fetchone()[0] == 36
    stages_before = [tuple(row) for row in connection.execute("SELECT * FROM stage_log")]
    holders_before = _publication_rows(connection, builds[:6])
    blockers = router_restart_blockers(connection)
    assert len([item for item in blockers if item.startswith("merge-approved")]) == 35
    assert len([item for item in blockers if item.startswith("publication-held")]) == 6

    changed = retire_inspected_history(connection, inventory, now=NOW)

    assert changed == {"retirement_receipts": 35, "released_holders": 6}
    assert router_restart_blockers(connection) == ()
    assert [
        tuple(row)
        for row in connection.execute(
            "SELECT * FROM stage_log WHERE target_identifier != ? ORDER BY id",
            (RETIREMENT_TARGET,),
        )
    ] == stages_before
    holders_after = _publication_rows(connection, builds[:6])
    for before, after in zip(holders_before, holders_after, strict=True):
        expected = list(before)
        expected[12] = None
        expected[13] = None
        expected[17] = NOW.isoformat()
        assert after == expected
    reports = dict(
        connection.execute(
            "SELECT status, count(*) FROM stage_log "
            "WHERE target_identifier='merge_deploy_executor' GROUP BY status"
        )
    )
    assert reports == {"FAILED": 22, "PASSED": 14}
    latest_outcomes = dict(
        connection.execute(
            "SELECT target_identifier || ':' || status, count(*) "
            "FROM stage_log AS report WHERE substr(report.target_identifier,1,13)="
            "'merge_deploy_' AND report.target_identifier!='merge_deploy_retirement' "
            "AND report.id=("
            "SELECT max(later.id) FROM stage_log AS later WHERE later.build_id="
            "report.build_id AND substr(later.target_identifier,1,13)="
            "'merge_deploy_' AND later.target_identifier!='merge_deploy_retirement') "
            "AND EXISTS (SELECT 1 FROM stage_log AS decision WHERE decision.build_id="
            "report.build_id AND decision.target_identifier='merge_deploy_decision' "
            "AND decision.status='PASSED' AND decision.id<report.id) "
            "GROUP BY target_identifier, status"
        )
    )
    assert latest_outcomes == {
        "merge_deploy_executor:FAILED": 18,
        "merge_deploy_executor:PASSED": 13,
        "merge_deploy_merge:GATED": 4,
    }
    pool = SqliteLifecyclePersistence(connection=connection)
    row = pool.get_build_row(builds[0])
    stages = pool.read_stages(builds[0])
    markdown = render_markdown([(row, stages)], feature_id=row.feature_id)
    retirement_line = next(
        line for line in markdown.splitlines() if RETIREMENT_STAGE_LABEL in line
    )
    assert retirement_line.endswith("SKIPPED")

    with pytest.raises(MergeRetirementCleanupError, match="already exists"):
        retire_inspected_history(connection, inventory, now=NOW)


def test_late_holder_cas_failure_rolls_back_receipts_and_prior_releases(
    tmp_path: Path,
) -> None:
    connection, inventory, builds = _real_shaped_history(tmp_path)
    before = _publication_rows(connection, builds[:6])

    class FailsLast:
        def __init__(self, cx: sqlite3.Connection) -> None:
            self.real = PublicationRecordStore(cx)
            self.calls = 0

        def release_lease(self, **kwargs: Any) -> bool:
            self.calls += 1
            if self.calls == 6:
                return False
            return self.real.release_lease(**kwargs)

    with pytest.raises(MergeRetirementCleanupError, match="lease CAS failed"):
        retire_inspected_history(
            connection,
            inventory,
            now=NOW,
            publication_store_factory=FailsLast,
        )
    assert connection.execute(
        "SELECT count(*) FROM stage_log WHERE target_identifier=?",
        (RETIREMENT_TARGET,),
    ).fetchone()[0] == 0
    assert _publication_rows(connection, builds[:6]) == before


def test_changed_original_row_refuses_before_any_write(tmp_path: Path) -> None:
    connection, inventory, builds = _real_shaped_history(tmp_path)
    connection.execute(
        "UPDATE stage_log SET details_json='{}' WHERE id=?",
        (inventory["decision_ids"][0],),
    )
    with pytest.raises(MergeRetirementCleanupError, match="decision row changed"):
        retire_inspected_history(connection, inventory, now=NOW)
    assert connection.execute(
        "SELECT count(*) FROM stage_log WHERE target_identifier=?",
        (RETIREMENT_TARGET,),
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT count(*) FROM publication_records WHERE lease_holder IS NOT NULL"
    ).fetchone()[0] == 6
    assert len(builds) == 35


def test_capture_refuses_a_versioned_old_report_even_when_it_is_current(
    tmp_path: Path,
) -> None:
    from forge.lifecycle.merge_retirement_cleanup import capture_inventory

    connection, inventory, _builds = _real_shaped_history(tmp_path)
    connection.execute(
        "UPDATE stage_log SET details_json=? WHERE id=?",
        (
            json.dumps(
                {
                    "merge_decision": {
                        "execution_attempt_version": 1,
                        "execution_attempt_id": "a" * 32,
                    }
                }
            ),
            connection.execute(
                "SELECT min(id) FROM stage_log WHERE target_identifier="
                "'merge_deploy_executor'"
            ).fetchone()[0],
        ),
    )
    with pytest.raises(MergeRetirementCleanupError, match="versioned"):
        capture_inventory(
            connection,
            history_before=CUTOFF,
            audit_sha256="d" * 64,
            now=NOW,
        )

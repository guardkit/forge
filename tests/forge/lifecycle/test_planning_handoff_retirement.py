"""Connected coverage for the shared planning-handoff retirement reader."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from forge.lifecycle.feature_routing import _router_restart_blockers_snapshot
from forge.lifecycle.planning_handoff_preflight import (
    preflight_retired_planning_handoff_correlations,
)
from forge.lifecycle.planning_handoff_retirement import (
    PlanningHandoffRetirementError,
    retired_planning_handoff_correlations,
)
from tests.forge.lifecycle.planning_handoff_fixture import (
    CORRELATION,
    add_planning_retirement,
    make_planning_ledger,
)


@pytest.mark.parametrize("version", [17, 18])
def test_real_shaped_receipt_is_validated_read_only(
    tmp_path: Path, version: int
) -> None:
    db_path = tmp_path / f"forge-v{version}.db"
    connection, event_ids = make_planning_ledger(db_path, version=version)
    add_planning_retirement(connection, event_ids)
    connection.commit()
    before = db_path.read_bytes()
    connection.close()

    assert preflight_retired_planning_handoff_correlations(db_path) == frozenset(
        {CORRELATION}
    )
    assert db_path.read_bytes() == before


def test_missing_and_receipt_free_legacy_ledgers_keep_migration_path(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing" / "forge.db"
    assert preflight_retired_planning_handoff_correlations(missing) == frozenset()
    assert not missing.parent.exists()

    old = tmp_path / "old.db"
    connection = sqlite3.connect(old)
    connection.executescript(
        """
        CREATE TABLE schema_version(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        INSERT INTO schema_version VALUES (16, '2026-10-09T00:00:00+00:00');
        CREATE TABLE planning_run_events(
          id INTEGER PRIMARY KEY, stage_label TEXT NOT NULL, details_json TEXT
        );
        INSERT INTO planning_run_events VALUES (1, 'ordinary', '{"fixture":true}');
        """
    )
    connection.close()
    before = old.read_bytes()

    assert preflight_retired_planning_handoff_correlations(old) == frozenset()
    assert old.read_bytes() == before


def test_retirement_marker_on_legacy_schema_refuses_without_migration(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "old-with-marker.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE schema_version(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        INSERT INTO schema_version VALUES (16, '2026-10-09T00:00:00+00:00');
        CREATE TABLE planning_run_events(
          id INTEGER PRIMARY KEY, stage_label TEXT NOT NULL, details_json TEXT
        );
        INSERT INTO planning_run_events VALUES (
          1, 'planning-handoff-retirement-administrative', '{}'
        );
        """
    )
    connection.close()
    before = db_path.read_bytes()

    with pytest.raises(PlanningHandoffRetirementError, match="unsupported ledger"):
        preflight_retired_planning_handoff_correlations(db_path)
    assert db_path.read_bytes() == before


def test_full_reader_refuses_linked_work_and_future_schema(tmp_path: Path) -> None:
    for mutation in ("build", "queue", "seed", "future"):
        version = 18 if mutation in {"seed", "future"} else 17
        db_path = tmp_path / f"{mutation}.db"
        connection, event_ids = make_planning_ledger(db_path, version=version)
        add_planning_retirement(connection, event_ids)
        if mutation == "build":
            connection.execute("INSERT INTO builds VALUES ('build-x', ?)", (CORRELATION,))
        elif mutation == "queue":
            connection.execute("INSERT INTO work_queue VALUES (1, ?)", (CORRELATION,))
        elif mutation == "seed":
            connection.execute(
                "INSERT INTO feature_routing_seeds VALUES (?, 'FAILED')", (CORRELATION,)
            )
        else:
            connection.execute("INSERT INTO schema_version VALUES (19, 'later')")
        connection.commit()
        connection.close()

        with pytest.raises(PlanningHandoffRetirementError):
            preflight_retired_planning_handoff_correlations(db_path)


def test_lifecycle_parity_snapshot_uses_strict_reader(tmp_path: Path) -> None:
    connection, event_ids = make_planning_ledger(tmp_path / "forge.db", version=18)
    add_planning_retirement(connection, event_ids)
    connection.execute(
        "UPDATE planning_run_events SET actor_identity='' WHERE id>?",
        (event_ids[-1],),
    )

    with pytest.raises(PlanningHandoffRetirementError):
        _router_restart_blockers_snapshot(connection)


def test_reader_joins_caller_transaction(tmp_path: Path) -> None:
    connection, event_ids = make_planning_ledger(tmp_path / "forge.db", version=18)
    add_planning_retirement(connection, event_ids)
    assert connection.in_transaction

    assert retired_planning_handoff_correlations(connection) == frozenset({CORRELATION})
    assert connection.in_transaction
    connection.rollback()


def test_deep_selected_json_is_a_named_refusal(tmp_path: Path) -> None:
    db_path = tmp_path / "forge.db"
    connection, event_ids = make_planning_ledger(db_path, version=18)
    deep = (
        '{"planning_handoff_retirement":'
        + "[" * 16000
        + "0"
        + "]" * 16000
        + "}"
    )
    add_planning_retirement(connection, event_ids, details=deep)
    connection.commit()
    connection.close()

    with pytest.raises(PlanningHandoffRetirementError, match="not valid JSON"):
        preflight_retired_planning_handoff_correlations(db_path)


def test_deep_unselected_ordinary_json_is_tolerated(tmp_path: Path) -> None:
    db_path = tmp_path / "forge.db"
    connection, _ = make_planning_ledger(db_path, version=18)
    ordinary_deep = '{"ordinary":' + "[" * 16000 + "0" + "]" * 16000 + "}"
    connection.execute(
        "UPDATE planning_run_events SET details_json=? WHERE id=1",
        (ordinary_deep,),
    )
    connection.commit()
    connection.close()

    assert preflight_retired_planning_handoff_correlations(db_path) == frozenset()

from __future__ import annotations

import importlib.util
import json
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

import pytest

from forge.lifecycle.merge_retirement import retired_decision_ids
from forge.lifecycle.merge_retirement_cleanup import MergeRetirementCleanupError
from tests.forge.lifecycle.test_merge_retirement import _real_shaped_history


def _script() -> ModuleType:
    path = Path(__file__).parents[2] / "scripts" / "retire_legacy_merge_history.py"
    spec = importlib.util.spec_from_file_location("retire_legacy_merge_history", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _quiescence(path: Path, *, paused: int, provenance: str) -> None:
    path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "producers_stopped": True,
                "coordinator_callbacks_absent": True,
                "standalone_clis_absent": True,
                "remote_tasks_absent": True,
                "runner_tasks_absent": True,
                "model_tasks_absent": True,
                "deployment_work_absent": True,
                "paused_old_tasks": paused,
                "deployment_observed": "test deployment resolved at runtime",
                "observation_provenance": [provenance],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def test_script_captures_dry_runs_backs_up_and_applies_exact_inventory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connection, _inventory, _builds = _real_shaped_history(tmp_path)
    database = tmp_path / "forge.db"
    connection.close()
    inventory = tmp_path / "private-inventory.json"
    quiescence = tmp_path / "quiescence.json"
    backup = tmp_path / "backup" / "forge-before-retirement.db"
    audit = tmp_path / "private-audit.json"
    script = _script()

    assert script.main(
        [
            "--database",
            str(database),
            "--capture-inventory",
            str(inventory),
            "--history-before",
            "2026-10-08T13:22:53+00:00",
            "--audit-sha256",
            "d" * 64,
        ]
    ) == 0
    captured = json.loads(capsys.readouterr().out)
    assert captured["mode"] == "captured-inventory"
    assert captured["approved_builds"] == 35
    assert stat.S_IMODE(inventory.stat().st_mode) == 0o600

    assert script.main(
        ["--database", str(database), "--inventory", str(inventory)]
    ) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "dry-run"

    _quiescence(quiescence, paused=0, provenance="fixture inspected no processes")
    assert script.main(
        [
            "--database",
            str(database),
            "--inventory",
            str(inventory),
            "--apply",
            "--backup",
            str(backup),
            "--audit-output",
            str(audit),
            "--quiescence",
            str(quiescence),
            "--reviewed-source-commit",
            "a" * 40,
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out) == {
        "mode": "applied",
        "released_holders": 6,
        "retirement_receipts": 35,
    }
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(audit.stat().st_mode) == 0o600
    audit_payload = json.loads(audit.read_text(encoding="utf-8"))
    assert audit_payload["status"] == "committed"
    assert audit_payload["changes"] == {
        "released_holders": 6,
        "retirement_receipts": 35,
    }
    check = __import__("sqlite3").connect(database)
    try:
        assert len(retired_decision_ids(check)) == 35
    finally:
        check.close()


def test_observed_surviving_paused_process_refuses_before_backup(
    tmp_path: Path,
) -> None:
    connection, inventory_payload, _builds = _real_shaped_history(tmp_path)
    database = tmp_path / "forge.db"
    connection.close()
    inventory = tmp_path / "private-inventory.json"
    inventory.write_text(json.dumps(inventory_payload), encoding="utf-8")
    inventory.chmod(0o600)
    quiescence = tmp_path / "quiescence.json"
    backup = tmp_path / "must-not-exist.db"
    audit = tmp_path / "must-not-exist.json"
    sleeper = subprocess.Popen(["sleep", "60"])
    try:
        assert sleeper.poll() is None
        _quiescence(
            quiescence,
            paused=1,
            provenance=f"test observed surviving local process pid={sleeper.pid}",
        )
        with pytest.raises(
            MergeRetirementCleanupError, match="surviving paused old task"
        ):
            _script().main(
                [
                    "--database",
                    str(database),
                    "--inventory",
                    str(inventory),
                    "--apply",
                    "--backup",
                    str(backup),
                    "--audit-output",
                    str(audit),
                    "--quiescence",
                    str(quiescence),
                    "--reviewed-source-commit",
                    "a" * 40,
                ]
            )
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=5)
    assert not backup.exists()
    assert not audit.exists()


def test_audit_finalization_failure_reports_committed_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection, inventory_payload, _builds = _real_shaped_history(tmp_path)
    database = tmp_path / "forge.db"
    connection.close()
    inventory = tmp_path / "private-inventory.json"
    inventory.write_text(json.dumps(inventory_payload), encoding="utf-8")
    inventory.chmod(0o600)
    quiescence = tmp_path / "quiescence.json"
    _quiescence(quiescence, paused=0, provenance="fixture inspected no processes")
    backup = tmp_path / "backup.db"
    audit = tmp_path / "private-audit.json"
    script = _script()
    original_rewrite = script._rewrite_private_json
    calls = 0

    def fail_committed_rewrite(handle: object, payload: dict[str, object]) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated audit storage failure")
        original_rewrite(handle, payload)

    monkeypatch.setattr(script, "_rewrite_private_json", fail_committed_rewrite)
    with pytest.raises(
        MergeRetirementCleanupError,
        match="database retirement committed; audit finalization failed",
    ):
        script.main(
            [
                "--database",
                str(database),
                "--inventory",
                str(inventory),
                "--apply",
                "--backup",
                str(backup),
                "--audit-output",
                str(audit),
                "--quiescence",
                str(quiescence),
                "--reviewed-source-commit",
                "a" * 40,
            ]
        )

    assert json.loads(audit.read_text(encoding="utf-8"))["status"] == "pending__"
    check = __import__("sqlite3").connect(database)
    try:
        assert len(retired_decision_ids(check)) == 35
    finally:
        check.close()

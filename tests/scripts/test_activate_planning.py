"""Tests for the G-09 activation bundle (scripts/activate_planning.py).

The activation bundle is the ONE receipted, reversible operator step that turns
live planning on for the B4 window. These tests exercise it hermetically against
config COPIES in tmp_path — never a live config, never forge-prod, never a bus.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "activate_planning.py"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("activate_planning", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load_module()


def _base_config() -> dict[str, Any]:
    """A minimal VALID resting-state forge config (planning OFF)."""
    return {
        "permissions": {"filesystem": {"allowlist": ["/srv/forge"]}},
        "planning": {
            "enabled": False,
            "target_terminal": {"enabled": False},
            "target_repo_paths": {"guardkit/api_test": "/srv/repos/api_test"},
        },
    }


def _write(path: Path, raw: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    p = tmp_path / "forge.yaml"
    _write(p, _base_config())
    return p


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


def test_apply_turns_both_flags_on_and_writes(cfg: Path, tmp_path: Path) -> None:
    rc = mod.run(["--config", str(cfg), "--receipt-dir", str(tmp_path / "r")])
    assert rc == 0
    written = yaml.safe_load(cfg.read_text())
    assert written["planning"]["enabled"] is True
    assert written["planning"]["target_terminal"]["enabled"] is True
    assert written["planning"]["escalation_approver"] == mod.DEFAULT_APPROVER
    assert written["planning"]["default_target_repo"] == mod.DEFAULT_TARGET_REPO
    assert written["approval"]["expected_approver"] == mod.DEFAULT_APPROVER
    # The written config is a VALID ForgeConfig.
    mod.validate(written)
    # A receipt landed with a truthy change set.
    receipts = list((tmp_path / "r").glob("activation-apply-applied-*.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["changed"] is True
    assert "planning.enabled" in receipt["changed_keys"]
    assert receipt["dry_run"] is False


def test_apply_is_idempotent(cfg: Path, tmp_path: Path) -> None:
    mod.run(["--config", str(cfg), "--receipt-dir", str(tmp_path / "r1")])
    first = cfg.read_text()
    rc = mod.run(["--config", str(cfg), "--receipt-dir", str(tmp_path / "r2")])
    assert rc == 0
    # Second apply changes nothing on disk.
    assert cfg.read_text() == first
    receipt = json.loads(
        next((tmp_path / "r2").glob("*.json")).read_text()
    )
    assert receipt["changed"] is False
    assert receipt["changed_keys"] == []


def test_apply_sets_target_repo_path_when_given(tmp_path: Path) -> None:
    base = _base_config()
    del base["planning"]["target_repo_paths"]  # unmapped repo
    cfg = tmp_path / "forge.yaml"
    _write(cfg, base)
    rc = mod.run(
        [
            "--config",
            str(cfg),
            "--target-repo-path",
            "/srv/repos/api_test",
            "--receipt-dir",
            str(tmp_path / "r"),
        ]
    )
    assert rc == 0
    written = yaml.safe_load(cfg.read_text())
    assert (
        written["planning"]["target_repo_paths"][mod.DEFAULT_TARGET_REPO]
        == "/srv/repos/api_test"
    )


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------


def test_rollback_restores_resting_state(cfg: Path, tmp_path: Path) -> None:
    mod.run(["--config", str(cfg), "--receipt-dir", str(tmp_path / "on")])
    rc = mod.run(["--config", str(cfg), "--rollback", "--receipt-dir", str(tmp_path / "off")])
    assert rc == 0
    written = yaml.safe_load(cfg.read_text())
    assert written["planning"]["enabled"] is False
    assert written["planning"]["target_terminal"]["enabled"] is False
    receipt = json.loads(next((tmp_path / "off").glob("*.json")).read_text())
    assert receipt["mode"] == "rollback"
    assert receipt["after"]["planning.enabled"] is False


def test_apply_then_rollback_round_trips(cfg: Path, tmp_path: Path) -> None:
    resting = cfg.read_text()
    mod.run(["--config", str(cfg), "--receipt-dir", str(tmp_path / "on")])
    mod.run(["--config", str(cfg), "--rollback", "--receipt-dir", str(tmp_path / "off")])
    written = yaml.safe_load(cfg.read_text())
    # Flags are back to the resting state (approver/target repo may persist,
    # which is harmless — the switches are what gate the live loop).
    assert written["planning"]["enabled"] is False
    assert written["planning"]["target_terminal"]["enabled"] is False
    mod.validate(written)
    assert resting  # sanity


# ---------------------------------------------------------------------------
# dry-run + safety
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing_to_config(cfg: Path, tmp_path: Path) -> None:
    before = cfg.read_text()
    rc = mod.run(
        ["--config", str(cfg), "--dry-run", "--receipt-dir", str(tmp_path / "r")]
    )
    assert rc == 0
    assert cfg.read_text() == before  # config untouched
    receipt = json.loads(next((tmp_path / "r").glob("*.json")).read_text())
    assert receipt["dry_run"] is True
    # The receipt still shows the would-be change.
    assert receipt["after"]["planning.enabled"] is True


def test_invalid_result_aborts_without_writing(tmp_path: Path) -> None:
    base = _base_config()
    base["planning"]["originator_wait_seconds"] = -5  # ge=0 → invalid
    cfg = tmp_path / "forge.yaml"
    _write(cfg, base)
    before = cfg.read_text()
    rc = mod.run(["--config", str(cfg), "--receipt-dir", str(tmp_path / "r")])
    assert rc == 3  # validation abort
    assert cfg.read_text() == before  # nothing written


def test_missing_config_is_a_clean_error(tmp_path: Path) -> None:
    rc = mod.run(["--config", str(tmp_path / "nope.yaml")])
    assert rc == 2


# ---------------------------------------------------------------------------
# preflight checklist
# ---------------------------------------------------------------------------


def test_preflight_flags_unmapped_target_repo(tmp_path: Path) -> None:
    base = _base_config()
    del base["planning"]["target_repo_paths"]
    checks = mod.preflight_checklist(
        base, target_repo=mod.DEFAULT_TARGET_REPO, approver=mod.DEFAULT_APPROVER
    )
    by_name = {c["check"]: c for c in checks}
    assert by_name["target_repo_path_mapped"]["status"] == "warn"
    # The out-of-band items are present as confirm-at-window checks.
    assert by_name["broker_notification_acl"]["status"] == "confirm"
    assert by_name["fleet_watcher_nats_url"]["status"] == "confirm"

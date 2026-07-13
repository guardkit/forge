"""Tests for the O-30 restart-policy switch (scripts/restart_policy.py).

Hermetic: the ONE docker seam (``_run_docker``) is monkeypatched with an
in-memory fake container, so the suite never needs a real docker daemon and
never touches forge-prod or any live container. A live-shaped demonstration
against a *scratch* container lives beside the script in ``ops/receipts/``.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "restart_policy.py"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("restart_policy", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load_module()


class FakeDocker:
    """An in-memory docker whose only state is one container's restart policy."""

    def __init__(self, containers: dict[str, str]) -> None:
        # container name -> restart policy Name ("no" | "unless-stopped" | ...)
        self.policies = dict(containers)
        self.update_calls: list[tuple[str, str]] = []

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[:1] == ["inspect"]:
            container = args[-1]
            if container not in self.policies:
                return subprocess.CompletedProcess(
                    args, returncode=1, stdout="", stderr="No such object"
                )
            payload = {
                "Name": self.policies[container],
                "MaximumRetryCount": 0,
            }
            return subprocess.CompletedProcess(
                args, returncode=0, stdout=json.dumps(payload) + "\n", stderr=""
            )
        if args[:1] == ["update"]:
            # docker update --restart <policy> <container>
            policy = args[args.index("--restart") + 1]
            container = args[-1]
            if container not in self.policies:
                return subprocess.CompletedProcess(
                    args, returncode=1, stdout="", stderr="No such container"
                )
            self.policies[container] = policy
            self.update_calls.append((container, policy))
            return subprocess.CompletedProcess(
                args, returncode=0, stdout=container + "\n", stderr=""
            )
        raise AssertionError(f"unexpected docker call: {args!r}")


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    fd = FakeDocker({"forge-prod": "no"})
    monkeypatch.setattr(mod, "_run_docker", fd)
    return fd


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


def test_apply_sets_unless_stopped_and_receipts(fake: FakeDocker, tmp_path: Path) -> None:
    rc = mod.run(["--apply", "--receipt-dir", str(tmp_path)])
    assert rc == 0
    # The policy flipped on the (fake) container.
    assert fake.policies["forge-prod"] == "unless-stopped"
    assert fake.update_calls == [("forge-prod", "unless-stopped")]
    receipt = json.loads(next(tmp_path.glob("restart-policy-apply-applied-*.json")).read_text())
    assert receipt["mode"] == "apply"
    assert receipt["dry_run"] is False
    assert receipt["restart_policy_before"]["Name"] == "no"
    assert receipt["restart_policy_after"]["Name"] == "unless-stopped"
    assert receipt["changed"] is True
    assert receipt["target_policy"] == "unless-stopped"


def test_apply_is_idempotent(fake: FakeDocker, tmp_path: Path) -> None:
    fake.policies["forge-prod"] = "unless-stopped"  # already on
    rc = mod.run(["--apply", "--receipt-dir", str(tmp_path)])
    assert rc == 0
    assert fake.update_calls == []  # no docker update issued
    receipt = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert receipt["changed"] is False


# ---------------------------------------------------------------------------
# explicit-apply guard (E2-S3): mutation NEVER happens by default
# ---------------------------------------------------------------------------


def test_default_is_inert_no_apply_flag(fake: FakeDocker, tmp_path: Path) -> None:
    """A bare run (no --apply) must NOT touch the live container."""
    rc = mod.run(["--receipt-dir", str(tmp_path)])
    assert rc == 0
    assert fake.update_calls == []  # NOTHING mutated
    assert fake.policies["forge-prod"] == "no"  # unchanged
    receipt = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert receipt["dry_run"] is True  # inert-by-default marks the receipt as preview
    # Still shows the would-be end state, like --dry-run.
    assert receipt["restart_policy_after"]["Name"] == "unless-stopped"
    assert receipt["changed"] is True


def test_rollback_without_apply_is_inert(fake: FakeDocker, tmp_path: Path) -> None:
    """--rollback is a mutation too — it must also require --apply."""
    fake.policies["forge-prod"] = "unless-stopped"
    rc = mod.run(["--rollback", "--receipt-dir", str(tmp_path)])
    assert rc == 0
    assert fake.update_calls == []  # nothing mutated
    assert fake.policies["forge-prod"] == "unless-stopped"  # NOT rolled back
    receipt = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert receipt["mode"] == "rollback"
    assert receipt["dry_run"] is True


def test_apply_and_dry_run_conflict_is_rejected(fake: FakeDocker, tmp_path: Path) -> None:
    rc = mod.run(["--apply", "--dry-run", "--receipt-dir", str(tmp_path)])
    assert rc == 2
    assert fake.update_calls == []  # rejected before any docker touch
    assert list(tmp_path.glob("*.json")) == []  # no receipt on a usage error


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------


def test_rollback_restores_no(fake: FakeDocker, tmp_path: Path) -> None:
    fake.policies["forge-prod"] = "unless-stopped"
    rc = mod.run(["--rollback", "--apply", "--receipt-dir", str(tmp_path)])
    assert rc == 0
    assert fake.policies["forge-prod"] == "no"
    receipt = json.loads(next(tmp_path.glob("restart-policy-rollback-applied-*.json")).read_text())
    assert receipt["mode"] == "rollback"
    assert receipt["restart_policy_after"]["Name"] == "no"


def test_apply_then_rollback_round_trips(fake: FakeDocker, tmp_path: Path) -> None:
    mod.run(["--apply", "--receipt-dir", str(tmp_path / "on")])
    assert fake.policies["forge-prod"] == "unless-stopped"
    mod.run(["--rollback", "--apply", "--receipt-dir", str(tmp_path / "off")])
    assert fake.policies["forge-prod"] == "no"


# ---------------------------------------------------------------------------
# dry-run + safety
# ---------------------------------------------------------------------------


def test_dry_run_issues_no_update(fake: FakeDocker, tmp_path: Path) -> None:
    rc = mod.run(["--dry-run", "--receipt-dir", str(tmp_path)])
    assert rc == 0
    assert fake.update_calls == []  # nothing mutated
    assert fake.policies["forge-prod"] == "no"
    receipt = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert receipt["dry_run"] is True
    # The receipt still shows the would-be end state.
    assert receipt["restart_policy_after"]["Name"] == "unless-stopped"
    assert receipt["changed"] is True


def test_missing_container_is_a_clean_error(fake: FakeDocker, tmp_path: Path) -> None:
    rc = mod.run(["--container", "does-not-exist", "--receipt-dir", str(tmp_path)])
    assert rc == 2
    assert list(tmp_path.glob("*.json")) == []  # no receipt on a preflight failure


def test_update_failure_surfaces_rc3(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class Flaky(FakeDocker):
        def __call__(self, args: list[str]) -> subprocess.CompletedProcess[str]:
            if args[:1] == ["update"]:
                return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="boom")
            return super().__call__(args)

    monkeypatch.setattr(mod, "_run_docker", Flaky({"forge-prod": "no"}))
    rc = mod.run(["--apply", "--receipt-dir", str(tmp_path)])
    assert rc == 3


# ---------------------------------------------------------------------------
# preflight — the Ack-Pending-0 non-requirement is stated in the receipt
# ---------------------------------------------------------------------------


def test_preflight_states_ack_pending_not_required(fake: FakeDocker, tmp_path: Path) -> None:
    mod.run(["--receipt-dir", str(tmp_path)])
    receipt = json.loads(next(tmp_path.glob("*.json")).read_text())
    by_name = {c["check"]: c for c in receipt["preflight_checklist"]}
    assert by_name["ack_pending_zero_not_required"]["status"] == "ok"
    assert "no restart occurs" in by_name["ack_pending_zero_not_required"]["detail"].lower()
    assert by_name["no_restart_occurs"]["status"] == "ok"


def test_inspect_normalises_empty_policy_to_no(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(args: list[str]) -> subprocess.CompletedProcess[str]:
        payload = {"Name": "", "MaximumRetryCount": 0}  # docker's "unset" shape
        return subprocess.CompletedProcess(args, returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(mod, "_run_docker", fake_run)
    policy = mod.inspect_restart_policy("whatever")
    assert policy is not None
    assert policy["Name"] == "no"

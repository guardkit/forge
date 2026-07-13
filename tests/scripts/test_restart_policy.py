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
    rc = mod.run(["--receipt-dir", str(tmp_path)])
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
    rc = mod.run(["--receipt-dir", str(tmp_path)])
    assert rc == 0
    assert fake.update_calls == []  # no docker update issued
    receipt = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert receipt["changed"] is False


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------


def test_rollback_restores_no(fake: FakeDocker, tmp_path: Path) -> None:
    fake.policies["forge-prod"] = "unless-stopped"
    rc = mod.run(["--rollback", "--receipt-dir", str(tmp_path)])
    assert rc == 0
    assert fake.policies["forge-prod"] == "no"
    receipt = json.loads(next(tmp_path.glob("restart-policy-rollback-applied-*.json")).read_text())
    assert receipt["mode"] == "rollback"
    assert receipt["restart_policy_after"]["Name"] == "no"


def test_apply_then_rollback_round_trips(fake: FakeDocker, tmp_path: Path) -> None:
    mod.run(["--receipt-dir", str(tmp_path / "on")])
    assert fake.policies["forge-prod"] == "unless-stopped"
    mod.run(["--rollback", "--receipt-dir", str(tmp_path / "off")])
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
    rc = mod.run(["--receipt-dir", str(tmp_path)])
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

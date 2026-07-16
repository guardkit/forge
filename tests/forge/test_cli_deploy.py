"""Tests for ``forge deploy`` — the attended post-review deploy dispatch (C4-prep).

Covers the four load/gating behaviours the command owns:

* flag-off (``deploy.enabled=False``) → exit 3, and ZERO NATS/DB/seam touches
  (the ``_aopen_backends`` seam is never called);
* unknown target repo → a loud ClickException;
* a target with no ``deploy/profile.yaml`` → a loud ClickException;
* happy path (flag on, dry-run) with the NATS/DB seam faked to a tmp-DB
  repository + recording publisher and a fixed-verdict invoker — asserting the
  exit-code mapping complete=0 / reverted=2 / failed=1.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from click.testing import CliRunner

from nats_core.events import (
    DeployCompletePayload,
    DeployFailedPayload,
    DeployQueuedPayload,
    DeployRevertedPayload,
    DeployStartedPayload,
    LiveGateResultPayload,
    QAVerdictPayload,
)

from forge.cli import _deploy_run
from forge.cli.deploy import deploy_cmd
from forge.config.models import (
    DeployStageConfig,
    FilesystemPermissions,
    ForgeConfig,
    PermissionsConfig,
    PlanningConfig,
)
from forge.deploy.live_gate import LiveGateInvocation
from forge.persistence.repositories.runbook import RunbookRepository


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _RecordingDeployPublisher:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    async def publish_deploy_queued(self, p: DeployQueuedPayload) -> None:
        self.events.append(("DeployQueued", p))

    async def publish_deploy_started(self, p: DeployStartedPayload) -> None:
        self.events.append(("DeployStarted", p))

    async def publish_deploy_complete(self, p: DeployCompletePayload) -> None:
        self.events.append(("DeployComplete", p))

    async def publish_deploy_failed(self, p: DeployFailedPayload) -> None:
        self.events.append(("DeployFailed", p))

    async def publish_deploy_reverted(self, p: DeployRevertedPayload) -> None:
        self.events.append(("DeployReverted", p))

    async def publish_qa_verdict(self, p: QAVerdictPayload) -> None:
        self.events.append(("QAVerdict", p))

    async def publish_live_gate_result(self, p: LiveGateResultPayload) -> None:
        self.events.append(("LiveGateResult", p))


class _FixedVerdictInvoker:
    def __init__(self, verdict: str) -> None:
        self._verdict = verdict

    def invoke(
        self, *, feature: str, target: str, gates: tuple[str, ...] = ()
    ) -> LiveGateInvocation:
        return LiveGateInvocation(
            verdict=self._verdict,
            run_id=f"run-{feature}",
            gate_ids=tuple(gates),
            evidence_index_ref="ev/idx.json",
            dry_run=False,
        )


def _config(*, enabled: bool, target_repo_paths: dict[str, str] | None = None) -> ForgeConfig:
    return ForgeConfig(
        permissions=PermissionsConfig(filesystem=FilesystemPermissions(allowlist=[])),
        planning=PlanningConfig(target_repo_paths=target_repo_paths or {}),
        deploy=DeployStageConfig(enabled=enabled),
    )


def _write_profile(repo_dir: Path, *, rollback: bool) -> None:
    (repo_dir / "deploy").mkdir(parents=True, exist_ok=True)
    lines = [
        "env_id: it-test",
        "compose:",
        "  file: docker-compose.yml",
        "  script: deploy.sh",
    ]
    if rollback:
        lines.append("rollback_image_ref: app:rollback-20260716")
    (repo_dir / "deploy" / "profile.yaml").write_text("\n".join(lines) + "\n")


def _patch_backends(monkeypatch, tmp_path: Path) -> _RecordingDeployPublisher:
    """Fake ``_aopen_backends`` with a tmp-DB repo + recording publisher."""
    from forge.persistence.migrations.runbook import apply

    conn = sqlite3.connect(str(tmp_path / "forge.db"))
    apply(conn)
    repository = RunbookRepository(connection=conn)
    runbook_publisher = AsyncMock()
    deploy_publisher = _RecordingDeployPublisher()

    async def _fake_open(config, *, dry_run):
        async def _close() -> None:
            return None

        return repository, runbook_publisher, deploy_publisher, _close

    monkeypatch.setattr(_deploy_run, "_aopen_backends", _fake_open)
    return deploy_publisher


# ---------------------------------------------------------------------------
# Flag-off: exit 3, zero seam/NATS touch
# ---------------------------------------------------------------------------


def test_flag_off_exits_3_and_touches_nothing(monkeypatch) -> None:
    # A tripwire on the NATS/DB seam: it must NEVER be reached with the flag off.
    never = Mock(side_effect=AssertionError("_aopen_backends must not be called"))
    monkeypatch.setattr(_deploy_run, "_aopen_backends", never)

    runner = CliRunner()
    result = runner.invoke(
        deploy_cmd,
        ["FEAT-DEP1", "--repo", "org/name"],
        obj=_config(enabled=False, target_repo_paths={"org/name": "/tmp/x"}),
    )
    assert result.exit_code == 3
    assert "deploy.enabled=False" in result.output
    never.assert_not_called()


# ---------------------------------------------------------------------------
# Unknown repo → loud error
# ---------------------------------------------------------------------------


def test_unknown_repo_loud_error(monkeypatch, tmp_path) -> None:
    _patch_backends(monkeypatch, tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        deploy_cmd,
        ["FEAT-DEP1", "--repo", "org/missing"],
        obj=_config(enabled=True, target_repo_paths={"org/name": str(tmp_path)}),
    )
    assert result.exit_code != 0
    assert "unknown target repo" in result.output
    assert "org/name" in result.output  # names the known key


# ---------------------------------------------------------------------------
# Missing deploy profile → loud error
# ---------------------------------------------------------------------------


def test_missing_profile_loud_error(monkeypatch, tmp_path) -> None:
    _patch_backends(monkeypatch, tmp_path)
    # tmp_path has no deploy/profile.yaml.
    runner = CliRunner()
    result = runner.invoke(
        deploy_cmd,
        ["FEAT-DEP1", "--repo", "org/name"],
        obj=_config(enabled=True, target_repo_paths={"org/name": str(tmp_path)}),
    )
    assert result.exit_code != 0
    assert "not deployable" in result.output


# ---------------------------------------------------------------------------
# No config at all → loud error
# ---------------------------------------------------------------------------


def test_no_config_loud_error() -> None:
    runner = CliRunner()
    result = runner.invoke(deploy_cmd, ["FEAT-DEP1", "--repo", "org/name"], obj=None)
    assert result.exit_code != 0
    assert "forge.yaml" in result.output


# ---------------------------------------------------------------------------
# Happy-path exit-code mapping: complete=0 / reverted=2 / failed=1
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("verdict", "rollback", "expected_code", "expected_outcome"),
    [
        ("pass", False, 0, "complete"),
        ("fail", True, 2, "reverted"),
        ("fail", False, 1, "failed"),
    ],
)
def test_exit_code_mapping(
    monkeypatch, tmp_path, verdict, rollback, expected_code, expected_outcome
) -> None:
    deploy_pub = _patch_backends(monkeypatch, tmp_path)
    monkeypatch.setattr(
        _deploy_run,
        "_make_live_gate_invoker",
        lambda profile, repo_path: _FixedVerdictInvoker(verdict),
    )
    _write_profile(tmp_path, rollback=rollback)

    runner = CliRunner()
    result = runner.invoke(
        deploy_cmd,
        ["FEAT-DEP1", "--repo", "org/name", "--dry-run"],
        obj=_config(enabled=True, target_repo_paths={"org/name": str(tmp_path)}),
    )
    assert result.exit_code == expected_code, result.output
    assert f"outcome={expected_outcome}" in result.output
    # The deploy-domain sequence ran through the REAL dispatcher on the fake bus.
    names = [n for n, _ in deploy_pub.events]
    assert "DeployStarted" in names
    if expected_outcome == "reverted":
        assert "DeployReverted" in names

"""Tests for the Lane C1a deploy-stage composition + gated dispatch.

The C1a hermetic gate (three-lanes §3 C1 / close-out §3 E3):

1. THE GATE — the dry-run fleet-memory profile precedent re-driven through the
   REAL dispatcher (``dispatch_deploy_stage``) on a test bus: the full deploy
   event sequence (DeployQueued→Started→Complete + QAVerdict/LiveGateResult) +
   the F7 deploy record emitted.
2. Flag-off regression — ``deploy.enabled=False`` = a byte-for-byte no-op: zero
   DEPLOY dispatch, no publish, no F7 record, no runner constructed.
3. Unconfigured-seam loud-fail — an unconfigured reservation / broker seam
   surfaces a loud, honest DeployFailed through the real dispatcher, never a
   silent success (the supervisor posture, FEAT-DD4F).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from nats_core.events import (
    DeployCompletePayload,
    DeployFailedPayload,
    DeployQueuedPayload,
    DeployStartedPayload,
    LiveGateResultPayload,
    QAVerdictPayload,
)

from forge.config.models import DeployStageConfig
from forge.deploy.composition import (
    build_deploy_stage_runner,
    dispatch_deploy_stage,
    resolve_reservation_lease,
)
from forge.deploy.profile import load_deploy_profile, parse_deploy_profile
from forge.deploy.reservation import (
    InProcessReservationLease,
    UnconfiguredReservationLease,
)
from forge.deploy.stage import DeployStageRunner
from forge.persistence.repositories.runbook import RunbookRepository

FIXED = datetime(2026, 7, 13, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures / test bus
# ---------------------------------------------------------------------------


@pytest.fixture
def repository(tmp_path: Path) -> RunbookRepository:
    from forge.persistence.migrations.runbook import apply

    conn = sqlite3.connect(str(tmp_path / "deploy.db"))
    apply(conn)
    return RunbookRepository(connection=conn)


@pytest.fixture
def runbook_publisher() -> AsyncMock:
    pub = AsyncMock()
    pub.publish_runbook_started = AsyncMock()
    pub.publish_step_started = AsyncMock()
    pub.publish_step_result = AsyncMock()
    pub.publish_runbook_complete = AsyncMock()
    pub.publish_escalated = AsyncMock()
    return pub


class RecordingDeployPublisher:
    """Records (event_name, payload) for every deploy-domain publish call."""

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

    async def publish_qa_verdict(self, p: QAVerdictPayload) -> None:
        self.events.append(("QAVerdict", p))

    async def publish_live_gate_result(self, p: LiveGateResultPayload) -> None:
        self.events.append(("LiveGateResult", p))


# ---------------------------------------------------------------------------
# resolve_reservation_lease — the WS2-§B8 GPU-contention backend selection
# ---------------------------------------------------------------------------


class TestReservationBackend:
    def test_none_backend_is_in_process(self) -> None:
        lease = resolve_reservation_lease("none")
        assert isinstance(lease, InProcessReservationLease)

    def test_kv_backend_is_unconfigured_loud_fail(self) -> None:
        # 'kv' is reserved but unwired (scope Q2) — it must loud-fail if a
        # profile requests a reservation, never silently proceed unprotected.
        lease = resolve_reservation_lease("kv")
        assert isinstance(lease, UnconfiguredReservationLease)

    def test_provided_lease_wins(self) -> None:
        shared = InProcessReservationLease()
        assert resolve_reservation_lease("kv", provided=shared) is shared

    def test_unknown_backend_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown deploy.reservation_backend"):
            resolve_reservation_lease("redis")


# ---------------------------------------------------------------------------
# build_deploy_stage_runner — the flag's first runtime reader
# ---------------------------------------------------------------------------


class TestBuildGating:
    def test_flag_off_returns_none(
        self, repository, runbook_publisher
    ) -> None:
        # Default config (enabled=False) — the runner is NOT constructed.
        runner = build_deploy_stage_runner(
            DeployStageConfig(),
            repository=repository,
            runbook_publisher=runbook_publisher,
            deploy_publisher=RecordingDeployPublisher(),
        )
        assert runner is None

    def test_flag_on_constructs_runner(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        runner = build_deploy_stage_runner(
            DeployStageConfig(enabled=True),
            repository=repository,
            runbook_publisher=runbook_publisher,
            deploy_publisher=RecordingDeployPublisher(),
            dry_run=True,
        )
        assert isinstance(runner, DeployStageRunner)

    def test_flag_on_live_defaults_to_unconfigured_seams(
        self, repository, runbook_publisher
    ) -> None:
        # A live (non-dry) run with no seams supplied wires the Unconfigured*
        # loud-fail seams — never a silent no-op that reads green.
        from forge.deploy.live_gate import (
            UnconfiguredBrokerInspector,
            UnconfiguredLiveGateInvoker,
        )

        runner = build_deploy_stage_runner(
            DeployStageConfig(enabled=True),
            repository=repository,
            runbook_publisher=runbook_publisher,
            deploy_publisher=RecordingDeployPublisher(),
            dry_run=False,
        )
        assert isinstance(runner._live_gate_invoker, UnconfiguredLiveGateInvoker)
        assert isinstance(runner._broker_inspector, UnconfiguredBrokerInspector)


# ---------------------------------------------------------------------------
# THE GATE: dry-run fleet-memory profile through the REAL dispatcher
# ---------------------------------------------------------------------------


class TestDispatchDryRunGate:
    @pytest.mark.asyncio
    async def test_fleet_memory_dry_run_full_sequence_through_dispatcher(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        deploy_pub = RecordingDeployPublisher()
        profile = load_deploy_profile("deploy/profile.yaml")

        result = await dispatch_deploy_stage(
            DeployStageConfig(enabled=True),
            profile,
            correlation_id="corr-c1a",
            deploy_run_id="deployrun-c1a",
            repository=repository,
            runbook_publisher=runbook_publisher,
            deploy_publisher=deploy_pub,
            deploy_record_root=str(tmp_path / "state"),
            dry_run=True,
            clock=lambda: FIXED,
            feature="FEAT-FMDR",
            feat_id="FEAT-FMDR",
        )

        assert result is not None
        assert result.outcome == "complete"
        assert result.dry_run is True
        assert result.verdict == "pass"

        # Full deploy-domain event sequence on the test bus.
        assert result.events == (
            "DeployQueued",
            "DeployStarted",
            "DeployComplete",
            "QAVerdict",
            "LiveGateResult",
        )
        assert [name for name, _ in deploy_pub.events] == list(result.events)

        # F7 deploy record emitted on disk (dry-run labelled).
        assert result.deploy_record_ref is not None
        record_path = Path(result.deploy_record_ref)
        assert record_path.exists()
        body = record_path.read_text(encoding="utf-8")
        assert "# Deploy record — fleet-memory-nas (DRY RUN)" in body
        assert "**dry_run**: true" in body

    @pytest.mark.asyncio
    async def test_shared_reservation_backend_none_used_by_dispatcher(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        # A profile that requests a reservation, backend 'none' (default): the
        # dispatcher resolves an in-process lease and the deploy completes.
        deploy_pub = RecordingDeployPublisher()
        profile = parse_deploy_profile(
            {
                "env_id": "st",
                "compose": {"file": "dc.yml", "script": "d.sh"},
                "reservation": {"resource": "gb10-gpu"},
            }
        )
        result = await dispatch_deploy_stage(
            DeployStageConfig(enabled=True, run_live_gate=False),
            profile,
            correlation_id="c-res",
            deploy_run_id="run-res",
            repository=repository,
            runbook_publisher=runbook_publisher,
            deploy_publisher=deploy_pub,
            deploy_record_root=str(tmp_path / "state"),
            dry_run=True,
            clock=lambda: FIXED,
        )
        assert result is not None
        assert result.outcome == "complete"
        assert result.events == ("DeployQueued", "DeployStarted", "DeployComplete")


# ---------------------------------------------------------------------------
# Flag-off regression: byte-for-byte no-op
# ---------------------------------------------------------------------------


class TestFlagOffNoOp:
    @pytest.mark.asyncio
    async def test_dispatch_flag_off_is_byte_noop(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        deploy_pub = RecordingDeployPublisher()
        profile = load_deploy_profile("deploy/profile.yaml")
        record_root = tmp_path / "state"

        result = await dispatch_deploy_stage(
            DeployStageConfig(),  # enabled defaults to False
            profile,
            correlation_id="corr-off",
            deploy_run_id="deployrun-off",
            repository=repository,
            runbook_publisher=runbook_publisher,
            deploy_publisher=deploy_pub,
            deploy_record_root=str(record_root),
            dry_run=True,
            clock=lambda: FIXED,
            feature="FEAT-FMDR",
        )

        # No dispatch at all — None result.
        assert result is None
        # Zero DEPLOY dispatch: no deploy-domain event published.
        assert deploy_pub.events == []
        # No FMDR step-lifecycle events either — no runbook ran.
        runbook_publisher.publish_runbook_started.assert_not_awaited()
        # No F7 record written.
        assert not record_root.exists()
        # No runbook persisted.
        assert repository.load_runbook("deploy-deployrun-off", correlation_id="corr-off") is None


# ---------------------------------------------------------------------------
# Unconfigured-seam loud-fail through the real dispatcher (supervisor posture)
# ---------------------------------------------------------------------------


class TestUnconfiguredSeamLoudFail:
    @pytest.mark.asyncio
    async def test_kv_reservation_backend_loud_fails_not_silent(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        # A profile requests a reservation but the backend is the reserved,
        # unwired 'kv'. The dispatcher must surface a loud, honest DeployFailed
        # (failed_step=reservation) — never a silent success on an unprotected
        # resource.
        deploy_pub = RecordingDeployPublisher()
        profile = parse_deploy_profile(
            {
                "env_id": "st",
                "compose": {"file": "dc.yml", "script": "d.sh"},
                "reservation": {"resource": "gb10-gpu"},
            }
        )
        result = await dispatch_deploy_stage(
            DeployStageConfig(enabled=True, reservation_backend="kv"),
            profile,
            correlation_id="c-kv",
            deploy_run_id="run-kv",
            repository=repository,
            runbook_publisher=runbook_publisher,
            deploy_publisher=deploy_pub,
            deploy_record_root=str(tmp_path / "state"),
            dry_run=True,
            clock=lambda: FIXED,
        )
        assert result is not None
        assert result.outcome == "failed"
        assert result.failed_step == "reservation"
        assert result.events == ("DeployFailed",)
        assert "DeployComplete" not in result.events

    @pytest.mark.asyncio
    async def test_unconfigured_broker_inspector_loud_fails(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        # Live (non-dry) run with a broker contract: the default broker
        # inspector is Unconfigured* and raises on invocation, so broker_preflight
        # (the first step) fails loudly -> DeployFailed, never a silent green.
        deploy_pub = RecordingDeployPublisher()
        profile = parse_deploy_profile(
            {
                "env_id": "st",
                "compose": {"file": "dc.yml", "script": "d.sh"},
                "broker_contract_ref": "qa/broker-contract.yaml",
            }
        )
        result = await dispatch_deploy_stage(
            DeployStageConfig(enabled=True),
            profile,
            correlation_id="c-broker",
            deploy_run_id="run-broker",
            repository=repository,
            runbook_publisher=runbook_publisher,
            deploy_publisher=deploy_pub,
            deploy_record_root=str(tmp_path / "state"),
            dry_run=False,
            clock=lambda: FIXED,
        )
        assert result is not None
        assert result.outcome == "failed"
        assert result.failed_step == "broker_preflight"
        assert "DeployFailed" in result.events
        assert "DeployComplete" not in result.events
        assert result.verdict is None


# ---------------------------------------------------------------------------
# Serve-boot composition helper (the production first reader)
# ---------------------------------------------------------------------------


class TestServeBootComposition:
    def test_flag_off_composes_nothing(self) -> None:
        from forge.cli._serve_deploy import compose_deploy_stage_runner

        forge_config = SimpleNamespace(deploy=DeployStageConfig())
        runner = compose_deploy_stage_runner(
            forge_config=forge_config,
            nats_client=object(),
            db_path=None,
        )
        assert runner is None

    def test_flag_on_composes_runner(self, tmp_path) -> None:
        from forge.cli._serve_deploy import compose_deploy_stage_runner

        forge_config = SimpleNamespace(deploy=DeployStageConfig(enabled=True))
        runner = compose_deploy_stage_runner(
            forge_config=forge_config,
            nats_client=AsyncMock(),
            db_path=tmp_path / "forge.db",
        )
        assert isinstance(runner, DeployStageRunner)

    def test_flag_on_without_db_path_loud_fails(self) -> None:
        from forge.cli._serve_deploy import compose_deploy_stage_runner

        forge_config = SimpleNamespace(deploy=DeployStageConfig(enabled=True))
        with pytest.raises(RuntimeError, match="no db_path"):
            compose_deploy_stage_runner(
                forge_config=forge_config,
                nats_client=AsyncMock(),
                db_path=None,
            )

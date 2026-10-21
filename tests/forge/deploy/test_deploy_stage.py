"""Tests for the DeployStageRunner (WS2-B8) — including the B8 DRY-RUN gate.

The gate (scope §B8 / §4): a DRY-RUN deploy of the fleet-memory exemplar profile
(the FEAT-FMDR subject — zero blast radius) produces a valid F7 record + the
full deploy-domain event sequence on a test bus.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from nats_core.events import (
    DeployCompletePayload,
    DeployFailedPayload,
    DeployQueuedPayload,
    DeployRevertedPayload,
    DeployStartedPayload,
    LiveGateResultPayload,
    QAVerdictPayload,
)

from forge.config.models import DeployStageConfig
from forge.deploy.deploy_record import render_deploy_record  # noqa: F401 (import smoke)
from forge.deploy.live_gate import (
    DryRunBrokerInspector,
    DryRunLiveGateInvoker,
    LiveGateInvocation,
)
from forge.deploy.profile import load_deploy_profile, parse_deploy_profile
from forge.deploy.reservation import InProcessReservationLease
from forge.deploy.stage import DeployStageRunner
from forge.executor.executor import RunResult
from forge.persistence.repositories.runbook import RunbookRepository

FIXED = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
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

    async def publish_deploy_reverted(self, p: DeployRevertedPayload) -> None:
        self.events.append(("DeployReverted", p))

    async def publish_qa_verdict(self, p: QAVerdictPayload) -> None:
        self.events.append(("QAVerdict", p))

    async def publish_live_gate_result(self, p: LiveGateResultPayload) -> None:
        self.events.append(("LiveGateResult", p))


class _RaisingLiveGateInvoker:
    """A live-gate invoker that raises — the gate produces NO verdict (O-32)."""

    def invoke(
        self, *, feature: str, target: str, gates: tuple[str, ...] = ()
    ) -> LiveGateInvocation:
        raise RuntimeError("live-gate instrument down")


class _FixedVerdictLiveGateInvoker:
    """A live-gate invoker that returns a fixed (non-pass) verdict — for O-32."""

    def __init__(self, verdict: str = "fail") -> None:
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
            detail={},
        )


def _runner(
    repository: RunbookRepository,
    runbook_publisher: AsyncMock,
    deploy_publisher: Any,
    tmp_path: Path,
    *,
    dry_run: bool = True,
    reservation: InProcessReservationLease | None = None,
    presence_resolver=None,
    config: DeployStageConfig | None = None,
    live_gate_invoker: Any = None,
) -> DeployStageRunner:
    return DeployStageRunner(
        repository=repository,
        runbook_publisher=runbook_publisher,
        deploy_publisher=deploy_publisher,
        reservation=reservation or InProcessReservationLease(),
        live_gate_invoker=live_gate_invoker or DryRunLiveGateInvoker(),
        broker_inspector=DryRunBrokerInspector(),
        config=config or DeployStageConfig(),
        deploy_record_root=str(tmp_path / "state"),
        dry_run=dry_run,
        clock=lambda: FIXED,
        presence_resolver=presence_resolver,
    )


def _profile_with_rollback(rollback_image_ref: str | None) -> Any:
    raw: dict[str, Any] = {
        "env_id": "study-tutor-prod",
        "compose": {"file": "docker-compose.yml", "script": "deploy.sh"},
    }
    if rollback_image_ref is not None:
        raw["rollback_image_ref"] = rollback_image_ref
    return parse_deploy_profile(raw)


# ---------------------------------------------------------------------------
# THE GATE: dry-run of the fleet-memory exemplar profile
# ---------------------------------------------------------------------------


class TestDryRunGate:
    @pytest.mark.asyncio
    async def test_fleet_memory_dry_run_full_sequence(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        deploy_pub = RecordingDeployPublisher()
        profile = load_deploy_profile("deploy/profile.yaml")
        runner = _runner(repository, runbook_publisher, deploy_pub, tmp_path)

        result = await runner.run_deploy(
            profile,
            correlation_id="corr-fmdr",
            deploy_run_id="deployrun-1",
            feature="FEAT-FMDR",
            feat_id="FEAT-FMDR",
        )

        # Outcome: complete, dry-run, live-gate verdict pass.
        assert result.outcome == "complete"
        assert result.dry_run is True
        assert result.verdict == "pass"

        # Full deploy-domain event sequence on the bus.
        assert result.events == (
            "DeployQueued",
            "DeployStarted",
            "DeployComplete",
            "QAVerdict",
            "LiveGateResult",
        )
        assert [name for name, _ in deploy_pub.events] == list(result.events)

        # Payloads are the real B7 0.7.0 models with the right correlation.
        names = {name: p for name, p in deploy_pub.events}
        assert isinstance(names["DeployComplete"], DeployCompletePayload)
        assert names["DeployComplete"].correlation_id == "corr-fmdr"
        assert names["DeployComplete"].env_id == "fleet-memory-nas"
        assert names["QAVerdict"].verdict == "pass"

        # Valid F7 deploy record on disk.
        assert result.deploy_record_ref is not None
        record_path = Path(result.deploy_record_ref)
        assert record_path.exists()
        body = record_path.read_text(encoding="utf-8")
        assert "# Deploy record — fleet-memory-nas (DRY RUN)" in body
        assert "**dry_run**: true" in body
        # A claim per executed deploy step (inject_secrets, deploy_compose, health_check).
        assert "inject_secrets passed [dry-run]" in body
        assert "deploy_compose passed [dry-run]" in body
        assert "health_check passed [dry-run]" in body
        # deploy_record_ref on the DeployComplete payload points at this record.
        assert names["DeployComplete"].deploy_record_ref == result.deploy_record_ref

        # The FMDR step-lifecycle events fired for both runbooks.
        assert (
            runbook_publisher.publish_runbook_started.await_count == 2
        )  # deploy + live-gate
        assert runbook_publisher.publish_runbook_complete.await_count == 2
        assert runbook_publisher.publish_step_result.await_count >= 3

    @pytest.mark.asyncio
    async def test_no_secret_value_in_record_or_payloads(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        # The profile carries a secret REF name; no value exists anywhere. Prove
        # the record/payloads carry only the ref name, never a value.
        deploy_pub = RecordingDeployPublisher()
        profile = load_deploy_profile("deploy/profile.yaml")
        runner = _runner(repository, runbook_publisher, deploy_pub, tmp_path)
        result = await runner.run_deploy(
            profile, correlation_id="c", deploy_run_id="run-x"
        )
        body = Path(result.deploy_record_ref).read_text(encoding="utf-8")
        # ref name is fine to appear; but a DSN value shape must not.
        assert "postgres://" not in body
        assert "FLEET_MEMORY_PG_DSN=" not in body


# ---------------------------------------------------------------------------
# Deploy-only (no live gate)
# ---------------------------------------------------------------------------


class TestLiveGateToggle:
    @pytest.mark.asyncio
    async def test_run_live_gate_false_stops_at_complete(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        deploy_pub = RecordingDeployPublisher()
        profile = load_deploy_profile("deploy/profile.yaml")
        runner = _runner(
            repository,
            runbook_publisher,
            deploy_pub,
            tmp_path,
            config=DeployStageConfig(run_live_gate=False),
        )
        result = await runner.run_deploy(
            profile, correlation_id="c2", deploy_run_id="run-2"
        )
        assert result.outcome == "complete"
        assert result.verdict is None
        assert result.events == ("DeployQueued", "DeployStarted", "DeployComplete")


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


class TestFailurePaths:
    @pytest.mark.asyncio
    async def test_deploy_step_failure_emits_deploy_failed(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        # Live (not dry-run) with a secret ref that is NOT present -> inject_secrets
        # fails closed -> DEPLOY runbook escalates -> DeployFailed.
        deploy_pub = RecordingDeployPublisher()
        profile = parse_deploy_profile(
            {
                "env_id": "demo",
                "compose": {"file": "dc.yml", "script": "deploy.sh"},
                "secret_injection": ["MISSING_KEY"],
            }
        )
        runner = _runner(
            repository,
            runbook_publisher,
            deploy_pub,
            tmp_path,
            dry_run=False,
            presence_resolver=lambda name: False,
        )
        result = await runner.run_deploy(
            profile, correlation_id="c3", deploy_run_id="run-3"
        )
        assert result.outcome == "failed"
        assert result.failed_step == "inject_secrets"
        assert "DeployFailed" in result.events
        assert "DeployComplete" not in result.events
        failed = [p for n, p in deploy_pub.events if n == "DeployFailed"][0]
        assert failed.failed_step == "inject_secrets"
        # A record with the failed step's evidenced claim was still written.
        assert result.deploy_record_ref is not None

    @pytest.mark.asyncio
    async def test_reservation_unavailable_fails_before_start(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        reservation = InProcessReservationLease()
        # Pre-hold the resource under a different holder.
        reservation.acquire("gb10-gpu", holder="someone-else")
        deploy_pub = RecordingDeployPublisher()
        profile = parse_deploy_profile(
            {
                "env_id": "st",
                "compose": {"file": "dc.yml"},
                "reservation": {"resource": "gb10-gpu"},
            }
        )
        runner = _runner(
            repository,
            runbook_publisher,
            deploy_pub,
            tmp_path,
            reservation=reservation,
        )
        result = await runner.run_deploy(
            profile, correlation_id="c4", deploy_run_id="run-4"
        )
        assert result.outcome == "failed"
        assert result.failed_step == "reservation"
        assert result.events == ("DeployFailed",)
        assert result.deploy_record_ref is None

    @pytest.mark.asyncio
    async def test_reservation_released_after_success(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        reservation = InProcessReservationLease()
        deploy_pub = RecordingDeployPublisher()
        profile = parse_deploy_profile(
            {
                "env_id": "st",
                "compose": {"file": "dc.yml", "script": "d.sh"},
                "reservation": {"resource": "gb10-gpu"},
            }
        )
        runner = _runner(
            repository,
            runbook_publisher,
            deploy_pub,
            tmp_path,
            reservation=reservation,
        )
        await runner.run_deploy(profile, correlation_id="c5", deploy_run_id="run-5")
        # The lease was released, so a fresh holder can re-acquire.
        handle = reservation.acquire("gb10-gpu", holder="next")
        assert handle.holder == "next"


# ---------------------------------------------------------------------------
# Escalation mapping (irreversible-edge approval pause reuses Gate G1)
# ---------------------------------------------------------------------------


class TestEscalationMapping:
    @pytest.mark.asyncio
    async def test_awaiting_approval_maps_to_escalated_not_failed(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        deploy_pub = RecordingDeployPublisher()
        profile = load_deploy_profile("deploy/profile.yaml")
        runner = _runner(repository, runbook_publisher, deploy_pub, tmp_path)
        # Directly exercise the mapping for an awaiting_approval escalation
        # (the executor routes such steps to the existing approval-gate loop).
        result = await runner._on_deploy_not_complete(
            profile,
            run_result=RunResult(
                status="escalated", stopped_at_index=None, reason="awaiting_approval"
            ),
            executed=None,
            correlation_id="c6",
            deploy_run_id="run-6",
            feat_id=None,
            task_id=None,
            profile_ref="deploy/profile.yaml",
            deployer="run-6",
            events=[],
        )
        assert result.outcome == "escalated"
        assert result.detail.get("reason") == "awaiting_approval"
        # An approval pause is NOT a DeployFailed.
        assert "DeployFailed" not in result.events


# ---------------------------------------------------------------------------
# O-32 — revert-on-gate-fail (the endpoint's word "verified", enforced)
# ---------------------------------------------------------------------------


class TestRevertOnGateFail:
    @pytest.mark.asyncio
    async def test_verdict_fail_reverts_to_rollback_and_publishes_reverted(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        # A live-gate verdict != "pass" + a profile carrying a rollback ref:
        # the runner re-deploys the kept :rollback-* tag and publishes
        # DeployReverted; the stage outcome is the FAILED+reverted truth, not
        # the old outcome="complete" regardless of verdict.
        deploy_pub = RecordingDeployPublisher()
        profile = _profile_with_rollback("study-tutor:rollback-20260713")
        runner = _runner(
            repository,
            runbook_publisher,
            deploy_pub,
            tmp_path,
            live_gate_invoker=_FixedVerdictLiveGateInvoker("fail"),
        )
        result = await runner.run_deploy(
            profile,
            correlation_id="corr-rev",
            deploy_run_id="deployrun-rev",
            feature="FEAT-9A21",
            feat_id="FEAT-9A21",
        )

        assert result.outcome == "reverted"
        assert result.verdict == "fail"
        # The revert receipt fired AFTER the failing live-gate result.
        names = [n for n, _ in deploy_pub.events]
        assert names == [
            "DeployQueued",
            "DeployStarted",
            "DeployComplete",
            "QAVerdict",
            "LiveGateResult",
            "DeployReverted",
        ]
        assert result.events == tuple(names)
        # NO honest-green complete: the deploy did not stay serving.
        reverted = [p for n, p in deploy_pub.events if n == "DeployReverted"][0]
        assert isinstance(reverted, DeployRevertedPayload)
        assert reverted.reverted_to_image_ref == "study-tutor:rollback-20260713"
        assert reverted.failing_verdict == "fail"
        assert reverted.failing_verdict_ref == "ev/idx.json"
        assert reverted.env_id == "study-tutor-prod"
        # A revert runbook actually ran through the SAME executor + seam.
        revert_rb = repository.load_runbook(
            "revert-deployrun-rev", correlation_id="corr-rev"
        )
        assert revert_rb is not None
        step = revert_rb.steps[0]
        assert step.step_type == "deploy_compose"
        assert step.result is not None
        # Dry-run records the rollback image ref it would bring up (the intent).
        assert (
            step.result.payload["would_deploy_compose"]["rollback_image_ref"]
            == "study-tutor:rollback-20260713"
        )
        # [O-32 revert-signal drop, C4-prep] The revert runbook's step params
        # reach the executor payload intact — would_deploy_compose is
        # dict(step.params) as seen by the handler at execution time, so BOTH
        # revert signals are present for the live handler to thread as
        # REVERT/ROLLBACK_IMAGE_REF env vars (forge.executor.shell_steps).
        assert step.result.payload["would_deploy_compose"]["revert"] is True
        # And the persisted step params carry the same signals verbatim.
        assert step.params["revert"] is True
        assert step.params["rollback_image_ref"] == "study-tutor:rollback-20260713"
        # An honest F7 record with the reverted status.
        body = Path(result.deploy_record_ref).read_text(encoding="utf-8")
        assert "**status**: reverted" in body

    @pytest.mark.asyncio
    async def test_verdict_pass_does_not_revert(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        # The happy path stays untouched: a passing gate never reverts and never
        # builds a revert runbook.
        deploy_pub = RecordingDeployPublisher()
        profile = _profile_with_rollback("study-tutor:rollback-20260713")
        runner = _runner(
            repository,
            runbook_publisher,
            deploy_pub,
            tmp_path,
            live_gate_invoker=_FixedVerdictLiveGateInvoker("pass"),
        )
        result = await runner.run_deploy(
            profile, correlation_id="corr-ok", deploy_run_id="deployrun-ok"
        )
        assert result.outcome == "complete"
        assert result.verdict == "pass"
        assert "DeployReverted" not in result.events
        assert (
            repository.load_runbook(
                "revert-deployrun-ok", correlation_id="corr-ok"
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_no_verdict_gate_reverts_as_instrument_fail(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        # [O-32, the verdict=None hole — closed] A live-gate that produces NO
        # verdict at all (raising/unconfigured invoker) is an un-run gate, and
        # an un-run gate is not a verified deploy: the runner reverts as
        # "instrument_fail" instead of silently returning outcome="complete"
        # with the unverified build left serving.
        deploy_pub = RecordingDeployPublisher()
        profile = _profile_with_rollback("study-tutor:rollback-20260713")
        runner = _runner(
            repository,
            runbook_publisher,
            deploy_pub,
            tmp_path,
            live_gate_invoker=_RaisingLiveGateInvoker(),
        )
        result = await runner.run_deploy(
            profile, correlation_id="corr-nv", deploy_run_id="deployrun-nv"
        )

        assert result.outcome == "reverted"
        assert result.verdict == "instrument_fail"
        names = [n for n, _ in deploy_pub.events]
        # An instrument problem is not a SUT verdict — no QAVerdict published —
        # but the revert receipt still fires.
        assert "QAVerdict" not in names
        assert "DeployReverted" in names
        reverted = [p for n, p in deploy_pub.events if n == "DeployReverted"][0]
        assert reverted.failing_verdict == "instrument_fail"
        assert reverted.reverted_to_image_ref == "study-tutor:rollback-20260713"
        # The revert runbook genuinely ran through the same seam.
        assert (
            repository.load_runbook(
                "revert-deployrun-nv", correlation_id="corr-nv"
            )
            is not None
        )

    @pytest.mark.asyncio
    async def test_gate_fail_with_no_rollback_ref_is_loud_terminal_failure(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        # A gate fail but the profile carries NO rollback ref: LOUD terminal
        # failure naming the missing ref — never a silent keep-serving. No
        # DeployReverted (nothing was reverted); DeployFailed with failed_step
        # "revert".
        deploy_pub = RecordingDeployPublisher()
        profile = _profile_with_rollback(None)
        runner = _runner(
            repository,
            runbook_publisher,
            deploy_pub,
            tmp_path,
            live_gate_invoker=_FixedVerdictLiveGateInvoker("fail"),
        )
        result = await runner.run_deploy(
            profile, correlation_id="corr-norr", deploy_run_id="deployrun-norr"
        )
        assert result.outcome == "failed"
        assert result.failed_step == "revert"
        assert result.verdict == "fail"
        assert "DeployReverted" not in result.events
        assert "DeployFailed" in result.events
        failed = [p for n, p in deploy_pub.events if n == "DeployFailed"][0]
        assert failed.failed_step == "revert"
        assert failed.recoverable is False
        assert "rollback_image_ref" in failed.failure_reason
        assert result.detail.get("reason") == "missing_rollback_ref"

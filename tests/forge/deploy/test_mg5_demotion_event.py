"""[MG-5] DF-021 live-gate demotion-event emission (H-A Stage 3, forge leg).

The deploy stage's O-32 revert path (a post-merge live-gate that did not pass)
must ALSO drop a file-based demotion event into the target repo's ``qa/`` tree,
where the guardkit DF-021 trust ledger reads it to demote the auto-merged lane
back to attended. These tests prove:

* the emitted YAML is the exact minimal shape guardkit's ``load_demotion_event``
  reads (``feature_id / lane / source / verdict / timestamp`` + optional
  ``receipt_ref``);
* a verdict-fail deploy WRITES the event; a verdict-pass deploy writes NOTHING;
* the revert behaviour itself (O-32) is byte-for-byte untouched — the emission is
  side-only and best-effort (no ledger present ⇒ inert data).

Hermetic: tmp-dir git-free fixtures, dry-run deploy, fixed clock, no network.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yaml

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
from forge.deploy.demotion_event import (
    SOURCE_LIVE_GATE,
    write_demotion_event,
)
from forge.deploy.live_gate import (
    DryRunBrokerInspector,
    LiveGateInvocation,
)
from forge.deploy.profile import parse_deploy_profile
from forge.deploy.reservation import InProcessReservationLease
from forge.deploy.stage import DeployStageRunner
from forge.persistence.repositories.runbook import RunbookRepository

FIXED = datetime(2026, 7, 20, 9, 0, 0, tzinfo=UTC)

# The five REQUIRED keys guardkit's ``load_demotion_event`` enforces (loud on a
# missing one) — mirrored here as a PATTERN so the forge leg and the ledger
# cannot silently drift (forge does not import guardkit; DF-001).
_REQUIRED_KEYS = ("feature_id", "lane", "source", "verdict", "timestamp")


# ---------------------------------------------------------------------------
# Harness (mirrors test_deploy_stage.py; adds target_repo_root)
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


class _FixedVerdictLiveGateInvoker:
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


def _runner(
    repository: RunbookRepository,
    runbook_publisher: AsyncMock,
    deploy_publisher: Any,
    tmp_path: Path,
    *,
    verdict: str = "fail",
    target_repo: str | None = "appmilla/api_test",
    target_repo_root: str | None = None,
) -> DeployStageRunner:
    return DeployStageRunner(
        repository=repository,
        runbook_publisher=runbook_publisher,
        deploy_publisher=deploy_publisher,
        reservation=InProcessReservationLease(),
        live_gate_invoker=_FixedVerdictLiveGateInvoker(verdict),
        broker_inspector=DryRunBrokerInspector(),
        config=DeployStageConfig(),
        deploy_record_root=str(tmp_path / "state"),
        dry_run=True,
        clock=lambda: FIXED,
        target_repo=target_repo,
        target_repo_root=target_repo_root,
    )


def _profile() -> Any:
    return parse_deploy_profile(
        {
            "env_id": "study-tutor-prod",
            "compose": {"file": "docker-compose.yml", "script": "deploy.sh"},
            "rollback_image_ref": "study-tutor:rollback-20260713",
        }
    )


# ---------------------------------------------------------------------------
# The emitter, in isolation — the byte-shape the ledger reads
# ---------------------------------------------------------------------------


class TestWriteDemotionEvent:
    def test_shape_has_all_required_keys(self, tmp_path: Path) -> None:
        qa = tmp_path / "qa"
        path = write_demotion_event(
            qa,
            feature_id="FEAT-XYZ",
            lane="appmilla/api_test",
            verdict="fail",
            timestamp="2026-07-20T09:00:00+00:00",
            receipt_ref="qa/live-gate-FEAT-XYZ.yaml",
            run_id="deployrun-1",
        )
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        assert isinstance(doc, dict)
        for key in _REQUIRED_KEYS:
            assert doc.get(key), f"missing required key {key!r}"
        assert doc["feature_id"] == "FEAT-XYZ"
        assert doc["lane"] == "appmilla/api_test"
        assert doc["source"] == SOURCE_LIVE_GATE == "live_gate"
        assert doc["verdict"] == "fail"
        assert doc["timestamp"] == "2026-07-20T09:00:00+00:00"
        assert doc["receipt_ref"] == "qa/live-gate-FEAT-XYZ.yaml"

    def test_receipt_ref_omitted_when_absent(self, tmp_path: Path) -> None:
        # A falsy receipt_ref is dropped (the ledger reads a missing key as None).
        path = write_demotion_event(
            tmp_path / "qa",
            feature_id="FEAT-XYZ",
            lane="lane-a",
            verdict="environment_fail",
            timestamp="2026-07-20T09:00:00+00:00",
        )
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        assert "receipt_ref" not in doc
        for key in _REQUIRED_KEYS:
            assert doc.get(key)

    def test_filename_folds_run_id_so_repeats_never_collide(
        self, tmp_path: Path
    ) -> None:
        qa = tmp_path / "qa"
        p1 = write_demotion_event(
            qa,
            feature_id="FEAT-XYZ",
            lane="lane-a",
            verdict="fail",
            timestamp="t1",
            run_id="run-1",
        )
        p2 = write_demotion_event(
            qa,
            feature_id="FEAT-XYZ",
            lane="lane-a",
            verdict="fail",
            timestamp="t2",
            run_id="run-2",
        )
        assert p1 != p2
        assert Path(p1).exists() and Path(p2).exists()


# ---------------------------------------------------------------------------
# The deploy edge — verdict-fail WRITES, verdict-pass writes NOTHING
# ---------------------------------------------------------------------------


class TestMg5DemotionEdge:
    @pytest.mark.asyncio
    async def test_verdict_fail_writes_demotion_event(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        repo_root = tmp_path / "target_repo"
        deploy_pub = _RecordingDeployPublisher()
        runner = _runner(
            repository,
            runbook_publisher,
            deploy_pub,
            tmp_path,
            verdict="fail",
            target_repo="appmilla/api_test",
            target_repo_root=str(repo_root),
        )

        result = await runner.run_deploy(
            _profile(),
            correlation_id="corr-rev",
            deploy_run_id="deployrun-rev",
            feature="FEAT-9A21",
            feat_id="FEAT-9A21",
        )

        # O-32 revert behaviour is UNTOUCHED — still reverts as before.
        assert result.outcome == "reverted"
        assert result.verdict == "fail"

        # The demotion event landed in the target repo's qa/ tree, ledger-shaped.
        events = sorted((repo_root / "qa").glob("demotion-*.yaml"))
        assert len(events) == 1, f"expected exactly one demotion event, got {events}"
        doc = yaml.safe_load(events[0].read_text(encoding="utf-8"))
        for key in _REQUIRED_KEYS:
            assert doc.get(key), f"missing required key {key!r}"
        assert doc["feature_id"] == "FEAT-9A21"
        assert doc["lane"] == "appmilla/api_test"
        assert doc["source"] == "live_gate"
        assert doc["verdict"] == "fail"
        assert doc["timestamp"] == FIXED.isoformat()
        # The failing-gate evidence ref rides through as the receipt.
        assert doc["receipt_ref"] == "ev/idx.json"

    @pytest.mark.asyncio
    async def test_verdict_pass_writes_no_demotion_event(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        repo_root = tmp_path / "target_repo"
        deploy_pub = _RecordingDeployPublisher()
        runner = _runner(
            repository,
            runbook_publisher,
            deploy_pub,
            tmp_path,
            verdict="pass",
            target_repo_root=str(repo_root),
        )

        result = await runner.run_deploy(
            _profile(),
            correlation_id="corr-ok",
            deploy_run_id="deployrun-ok",
            feature="FEAT-OK1",
            feat_id="FEAT-OK1",
        )

        assert result.outcome == "complete"
        assert result.verdict == "pass"
        # A clean gate demotes nothing — no qa/ tree, no event.
        assert not (repo_root / "qa").exists() or not list(
            (repo_root / "qa").glob("demotion-*.yaml")
        )

    @pytest.mark.asyncio
    async def test_no_target_repo_root_is_inert_noop(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        # No target_repo_root threaded (older callers / boot composition): the
        # emission cannot name the qa/ tree, so it is a silent no-op — and the
        # O-32 revert still completes normally.
        deploy_pub = _RecordingDeployPublisher()
        runner = _runner(
            repository,
            runbook_publisher,
            deploy_pub,
            tmp_path,
            verdict="fail",
            target_repo_root=None,
        )

        result = await runner.run_deploy(
            _profile(),
            correlation_id="corr-none",
            deploy_run_id="deployrun-none",
            feature="FEAT-NONE",
            feat_id="FEAT-NONE",
        )

        assert result.outcome == "reverted"
        # Nothing written anywhere under tmp beyond the deploy state dir.
        assert not list(tmp_path.rglob("demotion-*.yaml"))

    @pytest.mark.asyncio
    async def test_lane_falls_back_to_env_id_when_no_target_repo(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        repo_root = tmp_path / "target_repo"
        deploy_pub = _RecordingDeployPublisher()
        runner = _runner(
            repository,
            runbook_publisher,
            deploy_pub,
            tmp_path,
            verdict="environment_fail",
            target_repo=None,
            target_repo_root=str(repo_root),
        )

        await runner.run_deploy(
            _profile(),
            correlation_id="corr-env",
            deploy_run_id="deployrun-env",
            feature="FEAT-ENV",
            feat_id="FEAT-ENV",
        )

        events = list((repo_root / "qa").glob("demotion-*.yaml"))
        assert len(events) == 1
        doc = yaml.safe_load(events[0].read_text(encoding="utf-8"))
        # No org/name key ⇒ the lane is the deploy env_id (still a valid lane).
        assert doc["lane"] == "study-tutor-prod"
        assert doc["verdict"] == "environment_fail"

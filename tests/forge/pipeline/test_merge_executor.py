"""MergeApprovalConsumer + execute_merge_deploy — offline, every seam faked.

The consumer's authz matrix (wrong decided_by, unknown request_id, duplicate
decision, correlation mismatch, reject, approve-exactly-once, single-flight)
and the executor's sequencing (merge failure stops before deploy, receipts
written, one outcome payload per result class, dry_run threads through).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from nats_core.envelope import EventType, MessageEnvelope
from nats_core.events import ApprovalResponsePayload

from forge.adapters.guardkit.models import GuardKitResult
from forge.adapters.sqlite import connect as sqlite_connect
from forge.config.models import ForgeConfig
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence, StageLogEntry
from forge.pipeline.merge_executor import (
    MERGE_DECISION_TARGET_IDENTIFIER,
    MERGE_STEP_DEPLOY_TARGET_IDENTIFIER,
    MERGE_STEP_MERGE_TARGET_IDENTIFIER,
    MergeApprovalConsumer,
    MergeExecutorDeps,
    execute_merge_deploy,
)
from forge.pipeline.merge_offer import (
    MERGE_OFFER_DETAILS_KEY,
    MERGE_OFFER_STAGE_LABEL,
    MERGE_OFFER_TARGET_IDENTIFIER,
)

BUILD_ID = "build-FEAT-MX1-20260824"
FEATURE_ID = "FEAT-MX1"
REPO = "appmilla/api_test"
CORRELATION = "corr-mx-1"
MAIN_SHA = "a" * 40

from datetime import datetime, timezone


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures + fakes
# ---------------------------------------------------------------------------


@pytest.fixture
def pool(tmp_path: Path) -> SqliteLifecyclePersistence:
    cx: sqlite3.Connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    return SqliteLifecyclePersistence(connection=cx)


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "api_test"
    root.mkdir()
    return root


@pytest.fixture
def config(repo_root: Path) -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {"target_repo_paths": {REPO: str(repo_root)}},
            "approval": {"expected_approver": "rich"},
            "merge_executor": {"enabled": True},
        }
    )


def _ensure_build(
    pool: SqliteLifecyclePersistence,
    *,
    build_id: str,
    feature_id: str,
) -> None:
    """stage_log has a FOREIGN KEY to builds — every offer needs its row."""
    pool.connection.execute(
        "INSERT OR IGNORE INTO builds (build_id, feature_id, repo, branch, "
        "feature_yaml_path, status, triggered_by, correlation_id, queued_at, "
        "mode) VALUES (?, ?, ?, ?, 'f.yaml', 'COMPLETE', 'cli', ?, "
        "'2026-08-24T00:00:00Z', 'mode-a')",
        (build_id, feature_id, REPO, f"autobuild/{feature_id}", f"corr-{build_id}"),
    )
    pool.connection.commit()


def _write_offer(
    pool: SqliteLifecyclePersistence,
    *,
    build_id: str = BUILD_ID,
    feature_id: str = FEATURE_ID,
    repo: str = REPO,
    correlation_id: str = CORRELATION,
    baseline_failing: list[str] | None = None,
    request_id: str | None = None,
) -> None:
    _ensure_build(pool, build_id=build_id, feature_id=feature_id)
    now = _utcnow()
    pool.record_stage(
        StageLogEntry(
            build_id=build_id,
            stage_label=MERGE_OFFER_STAGE_LABEL,
            target_kind="local_tool",
            target_identifier=MERGE_OFFER_TARGET_IDENTIFIER,
            status="GATED",
            gate_mode="MANDATORY_HUMAN_APPROVAL",
            started_at=now,
            completed_at=now,
            duration_secs=0.0,
            details={
                MERGE_OFFER_DETAILS_KEY: {
                    "request_id": request_id or f"merge-{build_id}",
                    "correlation_id": correlation_id,
                    "feature_id": feature_id,
                    "repo": repo,
                    "expect_main_sha": MAIN_SHA,
                    "baseline_failing": baseline_failing,
                }
            },
        )
    )


class _FakePublisher:
    def __init__(self) -> None:
        self.reports: list[Any] = []

    async def publish_stage_complete(self, payload: Any) -> None:
        self.reports.append(payload)


class _FakeGuardKit:
    """Records calls; returns a canned merge report."""

    def __init__(
        self,
        *,
        status: str = "success",
        report: dict[str, Any] | None = None,
        stderr: str | None = None,
    ) -> None:
        self.status = status
        self.report = (
            report
            if report is not None
            else {"status": "merged", "merged_sha": "b" * 40}
        )
        self.stderr = stderr
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> GuardKitResult:
        self.calls.append(kwargs)
        return GuardKitResult(
            status=self.status,  # type: ignore[arg-type]
            subcommand=kwargs.get("subcommand", "autobuild"),
            duration_secs=0.1,
            stdout_tail=json.dumps(self.report),
            stderr=self.stderr,
            exit_code=0 if self.status == "success" else 1,
        )


class _FakeDeploy:
    def __init__(
        self, *, outcome: str | None = "complete", verdict: str | None = "pass",
        raises: BaseException | None = None,
    ) -> None:
        self.outcome = outcome
        self.verdict = verdict
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        if self.outcome is None:
            return None
        return SimpleNamespace(
            outcome=self.outcome,
            verdict=self.verdict,
            deploy_record_ref="docs/state/x.md",
        )


def _deps(
    config: ForgeConfig,
    pool: SqliteLifecyclePersistence,
    *,
    guardkit: _FakeGuardKit | None = None,
    deploy: _FakeDeploy | None = None,
) -> tuple[MergeExecutorDeps, _FakePublisher, _FakeGuardKit, _FakeDeploy]:
    publisher = _FakePublisher()
    gk = guardkit if guardkit is not None else _FakeGuardKit()
    dp = deploy if deploy is not None else _FakeDeploy()
    deps = MergeExecutorDeps(
        config=config,
        pool=pool,
        pipeline_publisher=publisher,
        guardkit_run=gk,
        deploy_dispatcher=dp,
    )
    return deps, publisher, gk, dp


def _envelope(
    *,
    request_id: str = f"merge-{BUILD_ID}",
    decision: str = "approve",
    decided_by: str = "rich",
    correlation_id: str | None = CORRELATION,
) -> MessageEnvelope:
    return MessageEnvelope(
        source_id="jarvis",
        event_type=EventType.APPROVAL_RESPONSE,
        correlation_id=correlation_id,
        payload=ApprovalResponsePayload(
            request_id=request_id, decision=decision, decided_by=decided_by
        ).model_dump(mode="json"),
    )


async def _drain(consumer: MergeApprovalConsumer) -> None:
    while consumer._tasks:
        await asyncio.gather(*list(consumer._tasks))


def _stage_ids(pool: SqliteLifecyclePersistence, build_id: str = BUILD_ID) -> list[str]:
    return [s.target_identifier for s in pool.read_stages(build_id)]


@pytest.fixture(autouse=True)
def _receipts_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "receipts"
    monkeypatch.setenv("FORGE_RECEIPTS_DIR", str(root))
    return root


# ---------------------------------------------------------------------------
# Consumer authz matrix
# ---------------------------------------------------------------------------


class TestConsumerAuthz:
    @pytest.mark.asyncio
    async def test_wrong_decided_by_refused(self, config, pool) -> None:
        _write_offer(pool)
        deps, publisher, gk, dp = _deps(config, pool)
        consumer = MergeApprovalConsumer(deps)
        await consumer.handle_envelope(_envelope(decided_by="mallory"))
        await _drain(consumer)
        assert gk.calls == []
        assert MERGE_DECISION_TARGET_IDENTIFIER not in _stage_ids(pool)

    @pytest.mark.asyncio
    async def test_unknown_request_id_refused(self, config, pool) -> None:
        deps, publisher, gk, dp = _deps(config, pool)
        consumer = MergeApprovalConsumer(deps)
        await consumer.handle_envelope(_envelope(request_id="merge-build-ghost"))
        await _drain(consumer)
        assert gk.calls == []

    @pytest.mark.asyncio
    async def test_non_merge_request_id_refused(self, config, pool) -> None:
        _write_offer(pool)
        deps, _, gk, _ = _deps(config, pool)
        consumer = MergeApprovalConsumer(deps)
        await consumer.handle_envelope(_envelope(request_id=BUILD_ID))
        await _drain(consumer)
        assert gk.calls == []

    @pytest.mark.asyncio
    async def test_offer_request_id_mismatch_refused(self, config, pool) -> None:
        # The offer row EXISTS for the build but carries a DIFFERENT
        # request_id — the durable record wins over the wire's claim.
        _write_offer(pool, request_id="merge-somebody-else")
        deps, _, gk, _ = _deps(config, pool)
        consumer = MergeApprovalConsumer(deps)
        await consumer.handle_envelope(_envelope())
        await _drain(consumer)
        assert gk.calls == []
        assert MERGE_DECISION_TARGET_IDENTIFIER not in _stage_ids(pool)

    @pytest.mark.asyncio
    async def test_correlation_mismatch_refused(self, config, pool) -> None:
        _write_offer(pool)
        deps, _, gk, _ = _deps(config, pool)
        consumer = MergeApprovalConsumer(deps)
        await consumer.handle_envelope(_envelope(correlation_id="corr-forged"))
        await _drain(consumer)
        assert gk.calls == []
        assert MERGE_DECISION_TARGET_IDENTIFIER not in _stage_ids(pool)

    @pytest.mark.asyncio
    async def test_duplicate_decision_refused(self, config, pool) -> None:
        _write_offer(pool)
        deps, publisher, gk, dp = _deps(config, pool)
        consumer = MergeApprovalConsumer(deps)
        await consumer.handle_envelope(_envelope())
        await _drain(consumer)
        assert len(gk.calls) == 1
        # The second press — same request — is refused by the decision row.
        await consumer.handle_envelope(_envelope())
        await _drain(consumer)
        assert len(gk.calls) == 1
        decision_rows = [
            s
            for s in pool.read_stages(BUILD_ID)
            if s.target_identifier == MERGE_DECISION_TARGET_IDENTIFIER
        ]
        assert len(decision_rows) == 1

    @pytest.mark.asyncio
    async def test_defer_decision_refused_without_a_decision_row(
        self, config, pool
    ) -> None:
        _write_offer(pool)
        deps, _, gk, _ = _deps(config, pool)
        consumer = MergeApprovalConsumer(deps)
        await consumer.handle_envelope(_envelope(decision="defer"))
        await _drain(consumer)
        assert gk.calls == []
        assert MERGE_DECISION_TARGET_IDENTIFIER not in _stage_ids(pool)

    @pytest.mark.asyncio
    async def test_malformed_payload_dropped(self, config, pool) -> None:
        deps, _, gk, _ = _deps(config, pool)
        consumer = MergeApprovalConsumer(deps)
        envelope = MessageEnvelope(
            source_id="jarvis",
            event_type=EventType.APPROVAL_RESPONSE,
            payload={"decision": "approve"},  # no request_id
        )
        await consumer.handle_envelope(envelope)
        await _drain(consumer)
        assert gk.calls == []


class TestConsumerDecisions:
    @pytest.mark.asyncio
    async def test_reject_records_and_reports_skipped(self, config, pool) -> None:
        _write_offer(pool)
        deps, publisher, gk, dp = _deps(config, pool)
        consumer = MergeApprovalConsumer(deps)
        await consumer.handle_envelope(_envelope(decision="reject"))
        await _drain(consumer)
        assert gk.calls == []
        assert dp.calls == []
        decision = [
            s
            for s in pool.read_stages(BUILD_ID)
            if s.target_identifier == MERGE_DECISION_TARGET_IDENTIFIER
        ][0]
        assert decision.status == "SKIPPED"
        assert len(publisher.reports) == 1
        report = publisher.reports[0]
        assert report.status == "SKIPPED"
        assert report.result == "rejected"
        assert report.build_id == BUILD_ID
        assert report.correlation_id == CORRELATION
        assert "nothing" in report.detail

    @pytest.mark.asyncio
    async def test_approve_runs_the_executor_exactly_once(
        self, config, pool
    ) -> None:
        _write_offer(pool)
        deps, publisher, gk, dp = _deps(config, pool)
        consumer = MergeApprovalConsumer(deps)
        await consumer.handle_envelope(_envelope())
        await _drain(consumer)
        assert len(gk.calls) == 1
        assert len(dp.calls) == 1
        # The offer's pinned sha rode into the merge argv.
        args = gk.calls[0]["args"]
        assert args[:5] == ["merge", FEATURE_ID, "--target", "main", "--expect-main-sha"]
        assert args[5] == MAIN_SHA
        assert "--json" in args
        ids = _stage_ids(pool)
        assert MERGE_DECISION_TARGET_IDENTIFIER in ids
        assert MERGE_STEP_MERGE_TARGET_IDENTIFIER in ids
        assert MERGE_STEP_DEPLOY_TARGET_IDENTIFIER in ids
        assert len(publisher.reports) == 1
        assert publisher.reports[0].result == "merged-and-running"

    @pytest.mark.asyncio
    async def test_single_flight_serialises_a_repo(self, config, pool) -> None:
        _write_offer(pool)
        _write_offer(pool, build_id="build-FEAT-MX2", feature_id="FEAT-MX2")
        order: list[str] = []

        class _SlowGuardKit(_FakeGuardKit):
            async def __call__(self, **kwargs: Any) -> GuardKitResult:
                feature = kwargs["args"][1]
                order.append(f"start:{feature}")
                await asyncio.sleep(0.01)
                order.append(f"end:{feature}")
                return await super().__call__(**kwargs)

        deps, publisher, gk, dp = _deps(config, pool, guardkit=_SlowGuardKit())
        consumer = MergeApprovalConsumer(deps)
        await consumer.handle_envelope(_envelope())
        await consumer.handle_envelope(
            _envelope(request_id="merge-build-FEAT-MX2")
        )
        await _drain(consumer)
        starts = [i for i, e in enumerate(order) if e.startswith("start")]
        ends = [i for i, e in enumerate(order) if e.startswith("end")]
        # No interleave: the second merge starts only after the first ends.
        assert order[0].startswith("start") and order[1].startswith("end")
        assert len(starts) == 2 and len(ends) == 2


# ---------------------------------------------------------------------------
# Executor sequencing
# ---------------------------------------------------------------------------


async def _run_executor(
    deps: MergeExecutorDeps,
    repo_root: Path,
    *,
    baseline_failing: list[str] | None = None,
    dry_run: bool = False,
) -> Any:
    _ensure_build(deps.pool, build_id=BUILD_ID, feature_id=FEATURE_ID)
    return await execute_merge_deploy(
        deps=deps,
        build_id=BUILD_ID,
        feature_id=FEATURE_ID,
        repo=REPO,
        repo_root=repo_root,
        expect_main_sha=MAIN_SHA,
        correlation_id=CORRELATION,
        decided_by="rich",
        baseline_failing=baseline_failing,
        dry_run=dry_run,
    )


class TestExecutorSequencing:
    @pytest.mark.asyncio
    async def test_merge_failure_stops_before_deploy(
        self, config, pool, repo_root, _receipts_env: Path
    ) -> None:
        gk = _FakeGuardKit(status="failed", stderr="main moved since the checks")
        deps, publisher, gk, dp = _deps(config, pool, guardkit=gk)
        outcome = await _run_executor(deps, repo_root)
        assert outcome.result == "merge-refused"
        assert outcome.status == "FAILED"
        assert outcome.failed_step == "merge"
        assert dp.calls == []  # NOTHING half-done
        assert len(publisher.reports) == 1
        assert publisher.reports[0].result == "merge-refused"
        receipts = _receipts_env / f"merge-{BUILD_ID}"
        assert (receipts / "merge_deploy_merge.json").is_file()
        assert (receipts / "merge_deploy_report.json").is_file()
        assert not (receipts / "merge_deploy_deploy.json").exists()

    @pytest.mark.asyncio
    async def test_report_refusal_stops_with_plain_words(
        self, config, pool, repo_root
    ) -> None:
        gk = _FakeGuardKit(
            report={"status": "conflict", "detail": "main moved — merge refused"}
        )
        deps, publisher, gk, dp = _deps(config, pool, guardkit=gk)
        outcome = await _run_executor(deps, repo_root)
        assert outcome.result == "merge-refused"
        assert "main moved" in outcome.detail
        assert dp.calls == []

    @pytest.mark.asyncio
    async def test_happy_path_merged_and_running(
        self, config, pool, repo_root, _receipts_env: Path
    ) -> None:
        gk = _FakeGuardKit(
            report={
                "status": "merged",
                "merged_sha": "c" * 40,
                "checks_passed": 7,
                "checks_total": 7,
            }
        )
        deps, publisher, gk, dp = _deps(config, pool, guardkit=gk)
        outcome = await _run_executor(deps, repo_root)
        assert outcome.result == "merged-and-running"
        assert outcome.status == "PASSED"
        assert outcome.merged_sha == "c" * 40
        assert outcome.checks_passed == 7 and outcome.checks_total == 7
        report = publisher.reports[0]
        assert report.stage_label == "merge-deploy"
        assert report.target_kind == "local_tool"
        assert report.target_identifier == "merge_deploy_executor"
        assert report.status == "PASSED"
        assert report.merged_sha == "c" * 40
        assert report.checks_passed == 7
        receipts = _receipts_env / f"merge-{BUILD_ID}"
        for name in (
            "merge_deploy_merge.json",
            "merge_deploy_deploy.json",
            "merge_deploy_report.json",
        ):
            assert (receipts / name).is_file()

    @pytest.mark.asyncio
    async def test_deploy_reverted_reports_honestly(
        self, config, pool, repo_root
    ) -> None:
        dp = _FakeDeploy(outcome="reverted", verdict="fail")
        deps, publisher, gk, dp = _deps(config, pool, deploy=dp)
        outcome = await _run_executor(deps, repo_root)
        assert outcome.result == "merged-deploy-reverted"
        assert outcome.status == "FAILED"
        assert "rolled back" in outcome.detail
        assert "live is untouched" in outcome.detail

    @pytest.mark.asyncio
    async def test_deploy_raise_is_caught_and_reported(
        self, config, pool, repo_root
    ) -> None:
        dp = _FakeDeploy(raises=ValueError("sidecar surface needs a target_repo"))
        deps, publisher, gk, dp = _deps(config, pool, deploy=dp)
        outcome = await _run_executor(deps, repo_root)
        assert outcome.result == "merged-deploy-failed"
        assert outcome.failed_step == "deploy"
        assert "sidecar surface" in outcome.detail

    @pytest.mark.asyncio
    async def test_deploy_flag_off_reports_honestly(
        self, config, pool, repo_root
    ) -> None:
        dp = _FakeDeploy(outcome=None)
        deps, publisher, gk, dp = _deps(config, pool, deploy=dp)
        outcome = await _run_executor(deps, repo_root)
        assert outcome.result == "merged-deploy-failed"
        assert "deploy.enabled=false" in outcome.detail

    @pytest.mark.asyncio
    async def test_dry_run_threads_through_to_the_dispatcher(
        self, config, pool, repo_root
    ) -> None:
        deps, publisher, gk, dp = _deps(config, pool)
        await _run_executor(deps, repo_root, dry_run=True)
        assert dp.calls[0]["dry_run"] is True

    @pytest.mark.asyncio
    async def test_baseline_file_written_and_flag_passed(
        self, config, pool, repo_root
    ) -> None:
        deps, publisher, gk, dp = _deps(config, pool)
        await _run_executor(deps, repo_root, baseline_failing=["test_x"])
        args = gk.calls[0]["args"]
        assert "--baseline-json" in args
        baseline_path = Path(args[args.index("--baseline-json") + 1])
        assert baseline_path.is_file()
        assert baseline_path.parent == repo_root / ".guardkit" / "tmp"
        assert json.loads(baseline_path.read_text())["failing"] == ["test_x"]

    @pytest.mark.asyncio
    async def test_restart_probe_refuses_a_second_merge(
        self, config, pool, repo_root
    ) -> None:
        deps, publisher, gk, dp = _deps(config, pool)
        await _run_executor(deps, repo_root)
        assert len(gk.calls) == 1
        # A second invocation (restart / CLI overlap) refuses at the probe.
        outcome = await _run_executor(deps, repo_root)
        assert outcome.result == "merge-refused"
        assert len(gk.calls) == 1
        assert len(dp.calls) == 1

    @pytest.mark.asyncio
    async def test_checks_derived_from_the_live_gate_verdict(
        self, config, pool, repo_root
    ) -> None:
        dp = _FakeDeploy(outcome="complete", verdict="checks 7/7 pass")
        deps, publisher, gk, dp = _deps(config, pool, deploy=dp)
        outcome = await _run_executor(deps, repo_root)
        assert outcome.checks_passed == 7 and outcome.checks_total == 7
        assert "checks 7/7" in outcome.detail

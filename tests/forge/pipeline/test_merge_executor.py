"""MergeApprovalConsumer + execute_merge_deploy — offline, every seam faked.

The consumer's authz matrix (wrong decided_by, unknown request_id, duplicate
decision, correlation mismatch, reject, approve-exactly-once, single-flight)
and the executor's sequencing (the candidate is checked before the merge, a
merge failure stops before the promote, receipts written, one outcome payload
per result class, dry_run threads through).

The repository is a REAL git repository in a temporary directory — main with
one commit and ``autobuild/FEAT-MX1`` one commit ahead — because the
executor now reads the branch tip, lays its tree out, and compares tree ids
with git itself (protect-main, rules 37 and 38). Guardkit and the deploy
stage are the fakes, at the boundaries the executor already has.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
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
from forge.lifecycle.metrics import (
    MERGE_READY_TARGET_IDENTIFIER,
    self_closed_defect_rate,
)
from forge.pipeline.merge_executor import (
    MERGE_DECISION_TARGET_IDENTIFIER,
    MERGE_REPORT_TARGET_IDENTIFIER,
    MERGE_STEP_CANDIDATE_TARGET_IDENTIFIER,
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


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        [
            "git",
            "-c",
            "user.email=tests@example.invalid",
            "-c",
            "user.name=tests",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def _tip(repo: Path, feature_id: str = FEATURE_ID) -> str:
    """The feature branch's tip — what a clean merge lands (a fast-forward)."""
    return _git(repo, "rev-parse", f"autobuild/{feature_id}")


def _tree_of(repo: Path, sha: str) -> str:
    return _git(repo, "rev-parse", f"{sha}^{{tree}}")


def _candidate_dir(repo: Path, feature_id: str = FEATURE_ID) -> Path:
    return repo / ".forge-candidates" / feature_id


@pytest.fixture
def pool(tmp_path: Path) -> SqliteLifecyclePersistence:
    cx: sqlite3.Connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    return SqliteLifecyclePersistence(connection=cx)


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """A real repository: main with one commit, the feature branch one ahead."""
    root = tmp_path / "api_test"
    root.mkdir()
    _git(root, "init", "-b", "main", "-q")
    (root / "README.md").write_text("first\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "first")
    for feature in (FEATURE_ID, "FEAT-MX2"):
        _git(root, "checkout", "-q", "-b", f"autobuild/{feature}", "main")
        (root / f"{feature}.txt").write_text("the feature\n", encoding="utf-8")
        _git(root, "add", f"{feature}.txt")
        _git(root, "commit", "-q", "-m", f"the feature {feature}")
    _git(root, "checkout", "-q", "main")
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
    """Records calls; returns a canned merge report.

    With no ``report`` given the report says the merge landed on the feature
    branch's tip — what a clean fast-forward merge lands — read from the
    repository the merge was asked to run in, at call time.
    """

    def __init__(
        self,
        *,
        status: str = "success",
        report: dict[str, Any] | None = None,
        stderr: str | None = None,
    ) -> None:
        self.status = status
        self.report = report
        self.stderr = stderr
        self.calls: list[dict[str, Any]] = []

    def _report_for(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        if self.report is not None:
            return self.report
        repo = Path(kwargs["repo_path"])
        feature = kwargs["args"][1]
        return {"status": "merged", "merged_sha": _tip(repo, feature)}

    async def __call__(self, **kwargs: Any) -> GuardKitResult:
        self.calls.append(kwargs)
        return GuardKitResult(
            status=self.status,  # type: ignore[arg-type]
            subcommand=kwargs.get("subcommand", "autobuild"),
            duration_secs=0.1,
            stdout_tail=json.dumps(self._report_for(kwargs)),
            stderr=self.stderr,
            exit_code=0 if self.status == "success" else 1,
        )


#: What a green candidate check reports: every check passed, by name.
GREEN_GATE: dict[str, Any] = {
    "verdict": "pass",
    "checks_total": 8,
    "checks_passed": 8,
    "failed_checks": [],
    "gate_ids": ["health", "users_count", "etag", "a", "b", "c", "d", "e"],
    "live_gate_runbook_id": "live-gate-cand-run",
}

#: What a red candidate check reports: two of eight failed, by name.
RED_GATE: dict[str, Any] = {
    "verdict": "fail",
    "checks_total": 8,
    "checks_passed": 6,
    "failed_checks": ["users_count", "etag"],
    "gate_ids": ["health", "users_count", "etag", "a", "b", "c", "d", "e"],
    "live_gate_runbook_id": "live-gate-cand-run",
}


class _FakeDeploy:
    """The deploy stage, one leg at a time.

    ``candidate_outcome`` / ``candidate_verdict`` / ``gate`` shape the
    candidate check; ``outcome`` / ``verdict`` / ``raises`` shape the
    promote. ``outcome=None`` is the deploy stage switched off: every leg
    answers None. The candidate teardown always completes. Every call is
    recorded with its ``leg``; ``seen_tree`` records whether the candidate's
    working directory existed, with the branch's file in it, when the
    candidate leg was called.
    """

    def __init__(
        self,
        *,
        outcome: str | None = "complete",
        verdict: str | None = "pass",
        raises: BaseException | None = None,
        candidate_outcome: str = "complete",
        candidate_verdict: str | None = "pass",
        candidate_raises: BaseException | None = None,
        candidate_reason: str | None = None,
        candidate_failed_step: str | None = None,
        gate: dict[str, Any] | None = None,
        promote_candidate_word: str = "torn-down",
    ) -> None:
        self.outcome = outcome
        self.verdict = verdict
        self.raises = raises
        self.candidate_outcome = candidate_outcome
        self.candidate_verdict = candidate_verdict
        self.candidate_raises = candidate_raises
        self.candidate_reason = candidate_reason
        self.candidate_failed_step = candidate_failed_step
        self.gate = gate
        self.promote_candidate_word = promote_candidate_word
        self.calls: list[dict[str, Any]] = []
        self.seen_tree: dict[str, Any] = {}

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        leg = kwargs.get("leg", "deploy")
        if self.outcome is None:
            return None
        if leg == "candidate_check":
            cwd = kwargs.get("candidate_cwd")
            where = Path(cwd) if cwd else None
            self.seen_tree = {
                "cwd": cwd,
                "existed": bool(where and where.is_dir()),
                "branch_file": bool(
                    where and (where / f"{kwargs['feature_id']}.txt").is_file()
                ),
                "git_dir": bool(where and (where / ".git").exists()),
            }
            if self.candidate_raises is not None:
                raise self.candidate_raises
            if self.candidate_outcome == "complete":
                gate = self.gate if self.gate is not None else dict(GREEN_GATE)
                return SimpleNamespace(
                    outcome="complete",
                    verdict=self.candidate_verdict,
                    failed_step=None,
                    events=("DeployQueued",),
                    detail={"gate_summary": gate, "candidate": "standing"},
                )
            gate = self.gate if self.gate is not None else dict(RED_GATE)
            reason = self.candidate_reason or "candidate_failed"
            return SimpleNamespace(
                outcome="failed",
                verdict=self.candidate_verdict,
                failed_step=self.candidate_failed_step or "candidate_gate",
                events=("DeployQueued", "DeployFailed"),
                detail={"reason": reason, "gate_summary": gate},
            )
        if leg == "candidate_down":
            return SimpleNamespace(
                outcome="complete", verdict=None, detail={"candidate": "torn-down"}
            )
        if self.raises is not None:
            raise self.raises
        return SimpleNamespace(
            outcome=self.outcome,
            verdict=self.verdict,
            deploy_record_ref="docs/state/x.md",
            detail={"candidate": self.promote_candidate_word},
        )


def _legs(dp: _FakeDeploy) -> list[str]:
    return [call.get("leg", "deploy") for call in dp.calls]


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
        assert dp.calls == []  # a rejection dispatches nothing at all
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
        assert _legs(dp) == ["candidate_check", "promote"]
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
        # The candidate was checked first, then taken down; NOTHING promoted.
        assert _legs(dp) == ["candidate_check", "candidate_down"]
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
        assert "promote" not in _legs(dp)

    @pytest.mark.asyncio
    async def test_happy_path_merged_and_running(
        self, config, pool, repo_root, _receipts_env: Path
    ) -> None:
        merged = _tip(repo_root)
        gk = _FakeGuardKit(
            report={
                "status": "merged",
                "merged_sha": merged,
                "checks_passed": 7,
                "checks_total": 7,
            }
        )
        deps, publisher, gk, dp = _deps(config, pool, guardkit=gk)
        outcome = await _run_executor(deps, repo_root)
        assert outcome.result == "merged-and-running"
        assert outcome.status == "PASSED"
        assert outcome.merged_sha == merged
        assert outcome.checks_passed == 7 and outcome.checks_total == 7
        report = publisher.reports[0]
        assert report.stage_label == "merge-deploy"
        assert report.target_kind == "local_tool"
        assert report.target_identifier == "merge_deploy_executor"
        assert report.status == "PASSED"
        assert report.merged_sha == merged
        assert report.checks_passed == 7
        receipts = _receipts_env / f"merge-{BUILD_ID}"
        for name in (
            "merge_deploy_candidate.json",
            "merge_deploy_merge.json",
            "merge_deploy_tree_check.json",
            "merge_deploy_deploy.json",
            "merge_deploy_cleanup.json",
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
        """With the deploy stage switched off the candidate cannot be
        checked, and since protect-main an unchecked branch is not merged."""
        dp = _FakeDeploy(outcome=None)
        deps, publisher, gk, dp = _deps(config, pool, deploy=dp)
        outcome = await _run_executor(deps, repo_root)
        assert outcome.result == "candidate-refused"
        assert "deploy.enabled=false" in outcome.detail
        assert "nothing was merged" in outcome.detail
        assert gk.calls == []

    @pytest.mark.asyncio
    async def test_dry_run_merges_nothing_claims_nothing_publishes_nothing(
        self, config, pool, repo_root, _receipts_env: Path
    ) -> None:
        """A dry run is genuinely dry: no merge, no durable step rows, no
        Slack-bound publish — only the deploy dispatch in its own labelled
        dry mode, and receipts on disk."""
        deps, publisher, gk, dp = _deps(config, pool)
        outcome = await _run_executor(deps, repo_root, dry_run=True)
        assert gk.calls == []  # the merge command is never invoked
        assert dp.calls[0]["dry_run"] is True  # deploy runs in its dry mode
        assert publisher.reports == []  # nothing reaches Slack
        assert _stage_ids(pool) == []  # no durable step rows claimed
        assert outcome.merged_sha is None
        receipts = _receipts_env / f"merge-{BUILD_ID}"
        merge_receipt = json.loads(
            (receipts / "merge_deploy_merge.json").read_text()
        )
        assert merge_receipt["dry_run"] is True
        assert "nothing merged" in merge_receipt["skipped"]
        report_receipt = json.loads(
            (receipts / "merge_deploy_report.json").read_text()
        )
        assert report_receipt["dry_run"] is True

    @pytest.mark.asyncio
    async def test_dry_run_never_blocks_a_later_real_press(
        self, config, pool, repo_root
    ) -> None:
        """The poisoning case the guard exists for: a dry run must leave no
        step rows, so the real press afterwards merges normally instead of
        refusing "already on record"."""
        deps, publisher, gk, dp = _deps(config, pool)
        await _run_executor(deps, repo_root, dry_run=True)
        outcome = await _run_executor(deps, repo_root, dry_run=False)
        assert outcome.result == "merged-and-running"
        assert len(gk.calls) == 1  # the real merge ran, unblocked
        assert len(publisher.reports) == 1  # and the real outcome published

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
        # "failing_node_ids" is the name the merge command's own reader
        # requires; an object under any other name stops the merge.
        assert json.loads(baseline_path.read_text()) == {
            "failing_node_ids": ["test_x"]
        }

    @pytest.mark.asyncio
    async def test_the_baseline_file_is_in_a_shape_the_merge_command_accepts(
        self, config, pool, repo_root, tmp_path
    ) -> None:
        """Drive the written file through the merge command's own reader.

        The merge command reads the file as a bare list of test names, or as an
        object carrying a ``failing_node_ids`` list, and stops with an error for
        anything else. This runs a stand-in command with exactly that rule over
        the file the executor wrote: it must be read, not refused.
        """
        import stat as stat_module
        import subprocess

        deps, _publisher, gk, _dp = _deps(config, pool)
        await _run_executor(deps, repo_root, baseline_failing=["test_x"])
        args = gk.calls[0]["args"]
        baseline_path = args[args.index("--baseline-json") + 1]

        reader = tmp_path / "reads-the-baseline-the-real-way"
        reader.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "data = json.load(open(sys.argv[1]))\n"
            "if isinstance(data, list):\n"
            "    print(json.dumps([str(x) for x in data]))\n"
            "elif isinstance(data.get('failing_node_ids'), list):\n"
            "    print(json.dumps([str(x) for x in data['failing_node_ids']]))\n"
            "else:\n"
            "    sys.stderr.write('is an object without a failing_node_ids list')\n"
            "    sys.exit(1)\n",
            encoding="utf-8",
        )
        reader.chmod(
            reader.stat().st_mode | stat_module.S_IXUSR | stat_module.S_IXOTH
        )

        done = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [str(reader), str(baseline_path)],
            capture_output=True,
            text=True,
        )
        assert done.returncode == 0, done.stderr
        assert json.loads(done.stdout) == ["test_x"]

        # And the shape it refuses really is refused, so this has teeth.
        old_shape = tmp_path / "old-shape.json"
        old_shape.write_text(json.dumps({"failing": ["test_x"]}), encoding="utf-8")
        refused = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [str(reader), str(old_shape)],
            capture_output=True,
            text=True,
        )
        assert refused.returncode == 1
        assert "failing_node_ids" in refused.stderr

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
        assert _legs(dp) == ["candidate_check", "promote"]

    @pytest.mark.asyncio
    async def test_checks_derived_from_the_live_gate_verdict(
        self, config, pool, repo_root
    ) -> None:
        dp = _FakeDeploy(outcome="complete", verdict="checks 7/7 pass")
        deps, publisher, gk, dp = _deps(config, pool, deploy=dp)
        outcome = await _run_executor(deps, repo_root)
        assert outcome.checks_passed == 7 and outcome.checks_total == 7
        assert "checks 7/7" in outcome.detail

    def test_response_subject_filter_uses_whole_token_wildcards_only(
        self,
    ) -> None:
        """The first real press proved this live: NATS wildcards match whole
        tokens, so a partial like 'merge-*' silently matches nothing."""
        from forge.pipeline.merge_executor import MERGE_RESPONSE_SUBJECT_FILTER

        for token in MERGE_RESPONSE_SUBJECT_FILTER.split("."):
            assert token in ("*", ">") or (
                "*" not in token and ">" not in token
            ), token

    @pytest.mark.asyncio
    async def test_landed_merge_with_failed_verify_is_not_called_refused(
        self, config, pool, repo_root
    ) -> None:
        """FEAT-7CEA's real fire: the merge LANDED, the post-merge checks hit
        a pytest usage error, and the old label lied ('merge-refused'). A
        landed merge with red checks reports merged-verify-failed, keeps the
        sha, and never dispatches the deploy."""
        gk = _FakeGuardKit(
            status="failed",  # the verb exits 4: merged, verify not passed
            report={
                "outcome": "merged",
                "post_sha": "c" * 40,
                "verify_ok": False,
                "verify_detail": "pytest usage error (exit 4)",
                "charged_failures": [],
            },
        )
        deps, publisher, gk, dp = _deps(config, pool, guardkit=gk)
        outcome = await _run_executor(deps, repo_root)
        assert outcome.result == "merged-verify-failed"
        assert outcome.failed_step == "verify"
        assert outcome.merged_sha == "c" * 40
        assert "pytest usage error" in outcome.detail
        assert "merged" in outcome.detail
        assert "promote" not in _legs(dp)  # no deploy on red checks
        assert publisher.reports[0].result == "merged-verify-failed"

    def test_deploy_task_id_is_task_shaped(self) -> None:
        """The first dry fire caught this live: DeployQueuedPayload validates
        ^TASK-[A-Z0-9]{3,12}$ and the old merge-{build_id} shape failed it."""
        import re

        from forge.pipeline.merge_executor import _deploy_task_id

        for fid in ("FEAT-E613", "FEAT-153C", "feat-x!", ""):
            tid = _deploy_task_id(fid)
            assert re.fullmatch(r"TASK-[A-Z0-9]{3,12}", tid), (fid, tid)


# ---------------------------------------------------------------------------
# Digest conformance (advisory) riding the executor
# ---------------------------------------------------------------------------

from tests.forge.pipeline.test_digest_conformance import (
    write_created_per_day_case,
)


class TestDigestConformanceAdvisory:
    """The FEAT-EF8D lesson wired in: after a landed merge the executor reads
    the spec digest against the merged tree. A broken promise adds one plain
    warning line to the merge report and a receipt — it NEVER blocks."""

    @pytest.mark.asyncio
    async def test_broken_promise_warns_but_never_blocks(
        self, config, pool, repo_root, _receipts_env: Path
    ) -> None:
        write_created_per_day_case(
            repo_root, feature_id=FEATURE_ID, conforming=False
        )
        deps, publisher, gk, dp = _deps(config, pool)
        outcome = await _run_executor(deps, repo_root)
        # Advisory: the merge and deploy still went through untouched.
        assert outcome.result == "merged-and-running"
        assert outcome.status == "PASSED"
        assert _legs(dp) == ["candidate_check", "promote"]
        # One plain warning line rides the outcome and the published report.
        assert "WARNING:" in outcome.detail
        assert "7" in outcome.detail
        report = publisher.reports[0]
        assert "WARNING:" in report.detail
        assert report.digest_conformance_warning
        # The receipt landed beside the other merge receipts.
        receipt = json.loads(
            (
                _receipts_env / f"merge-{BUILD_ID}" / "digest_conformance.json"
            ).read_text()
        )
        assert receipt["conformant"] is False
        # The endpoint EXISTS — the finding is the untested seven-promise.
        endpoint_checks = [
            c for c in receipt["checks"] if c["check"] == "endpoint-exists"
        ]
        assert endpoint_checks[0]["verdict"] == "pass"
        failed = [c for c in receipt["checks"] if c["verdict"] == "fail"]
        assert failed
        assert all(
            c["check"] == "number-promise-is-tested" for c in failed
        )

    @pytest.mark.asyncio
    async def test_conforming_feature_stays_quiet(
        self, config, pool, repo_root, _receipts_env: Path
    ) -> None:
        write_created_per_day_case(
            repo_root, feature_id=FEATURE_ID, conforming=True
        )
        deps, publisher, gk, dp = _deps(config, pool)
        outcome = await _run_executor(deps, repo_root)
        assert outcome.result == "merged-and-running"
        assert "WARNING" not in outcome.detail
        assert publisher.reports[0].digest_conformance_warning is None
        receipt = json.loads(
            (
                _receipts_env / f"merge-{BUILD_ID}" / "digest_conformance.json"
            ).read_text()
        )
        assert receipt["conformant"] is True

    @pytest.mark.asyncio
    async def test_feature_without_a_digest_is_skipped_quietly(
        self, config, pool, repo_root, _receipts_env: Path
    ) -> None:
        # No digest in the tree: the receipt says so in plain words and no
        # warning is raised — an absent digest is not a failure.
        deps, publisher, gk, dp = _deps(config, pool)
        outcome = await _run_executor(deps, repo_root)
        assert outcome.result == "merged-and-running"
        assert "WARNING" not in outcome.detail
        receipt = json.loads(
            (
                _receipts_env / f"merge-{BUILD_ID}" / "digest_conformance.json"
            ).read_text()
        )
        assert receipt["conformant"] is None
        assert "no spec digest was found" in receipt["skipped"]


# ---------------------------------------------------------------------------
# The report on the build's own record
# ---------------------------------------------------------------------------


class _PoolThatCannotRecordTheReport:
    """The real pool, except that writing the report row raises.

    Everything else — the step claims, the reads — goes through untouched,
    so the test isolates exactly one failure: the database refusing the
    report row.
    """

    def __init__(self, pool: SqliteLifecyclePersistence) -> None:
        self._pool = pool

    def record_stage(self, entry: StageLogEntry) -> None:
        if entry.target_identifier == MERGE_REPORT_TARGET_IDENTIFIER:
            raise sqlite3.OperationalError("database is locked")
        self._pool.record_stage(entry)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._pool, name)


def _report_rows(
    pool: SqliteLifecyclePersistence, build_id: str = BUILD_ID
) -> list[StageLogEntry]:
    return [
        s
        for s in pool.read_stages(build_id)
        if s.target_identifier == MERGE_REPORT_TARGET_IDENTIFIER
    ]


class TestTheReportIsOnTheBuildsRecord:
    """The merge report is a message and a file; it must also be a row.

    The self-closed defect rate (``forge status --m5``) can only read the
    database, so a report that lives only on the bus and on disk leaves the
    number stuck at zero however many repairs really merged, deployed and
    stayed green.
    """

    @pytest.mark.asyncio
    async def test_a_green_report_is_recorded_with_its_outcome_word(
        self, config, pool, repo_root
    ) -> None:
        deps, publisher, gk, dp = _deps(config, pool)
        outcome = await _run_executor(deps, repo_root)
        assert outcome.result == "merged-and-running"

        rows = _report_rows(pool)
        assert len(rows) == 1
        row = rows[0]
        assert row.stage_label == "merge-deploy"
        assert row.status == "PASSED"
        # The outcome word is a field of its own, not buried in prose.
        assert row.details["result"] == "merged-and-running"
        assert row.details["build_id"] == BUILD_ID
        assert row.details["correlation_id"] == CORRELATION

    @pytest.mark.asyncio
    async def test_a_red_report_is_recorded_with_its_red_word(
        self, config, pool, repo_root
    ) -> None:
        deps, publisher, gk, dp = _deps(
            config, pool, deploy=_FakeDeploy(outcome="reverted", verdict="fail")
        )
        outcome = await _run_executor(deps, repo_root)
        assert outcome.result == "merged-deploy-reverted"

        rows = _report_rows(pool)
        assert len(rows) == 1
        assert rows[0].status == "FAILED"
        assert rows[0].details["result"] == "merged-deploy-reverted"

    @pytest.mark.asyncio
    async def test_a_dry_run_records_no_report(
        self, config, pool, repo_root
    ) -> None:
        """A dry run changed nothing, so it leaves nothing on the record."""
        deps, publisher, gk, dp = _deps(config, pool)
        await _run_executor(deps, repo_root, dry_run=True)
        assert _report_rows(pool) == []

    @pytest.mark.asyncio
    async def test_a_row_that_cannot_be_written_never_costs_the_report(
        self, config, pool, repo_root, _receipts_env: Path
    ) -> None:
        deps, publisher, gk, dp = _deps(config, pool)
        deps.pool = _PoolThatCannotRecordTheReport(pool)  # type: ignore[assignment]

        outcome = await _run_executor(deps, repo_root)

        assert outcome.result == "merged-and-running"
        assert publisher.reports[0].result == "merged-and-running"
        assert (
            _receipts_env / f"merge-{BUILD_ID}" / "merge_deploy_report.json"
        ).is_file()
        assert _report_rows(pool) == []

    @pytest.mark.asyncio
    async def test_the_self_closed_defect_rate_counts_a_real_green_run(
        self, config, pool, repo_root
    ) -> None:
        """The two ends meet: what the executor writes is what M5 reads.

        A repair row, its build, Rich's merge-ready card, and then a real
        run of the executor — no seeded report row anywhere — reads 1 of 1.
        """
        _ensure_build(pool, build_id=BUILD_ID, feature_id=FEATURE_ID)
        correlation_id = f"corr-{BUILD_ID}"  # what _ensure_build gives the build
        pool.connection.execute(
            "INSERT INTO work_queue (sentence, target_repo, kind, status, rank,"
            " originating_user, correlation_id, queued_at) VALUES"
            " (?, ?, 'fix', 'ADMITTED', 1.0, 'rich', ?, '2026-09-05T09:00:00+00:00')",
            ("The merge of FEAT-MX1 went red.", REPO, correlation_id),
        )
        pool.connection.commit()
        now = _utcnow()
        pool.record_stage(
            StageLogEntry(
                build_id=BUILD_ID,
                stage_label="the merge-ready checkpoint",
                target_kind="local_tool",
                target_identifier=MERGE_READY_TARGET_IDENTIFIER,
                status="GATED",
                started_at=now,
                completed_at=now,
                duration_secs=0.0,
                details={"merge_ready": {"gates": "green"}},
            )
        )
        assert self_closed_defect_rate(pool.connection) == (0, 1)

        deps, publisher, gk, dp = _deps(config, pool)
        outcome = await _run_executor(deps, repo_root)
        assert outcome.result == "merged-and-running"

        assert self_closed_defect_rate(pool.connection) == (1, 1)


# ---------------------------------------------------------------------------
# The merge word's checks: "could not run" is not "did not pass"
# ---------------------------------------------------------------------------


def _repair_rows(pool: SqliteLifecyclePersistence) -> list[str]:
    """Every repair sentence sitting in the work queue."""
    rows = pool.connection.execute(
        "SELECT sentence FROM work_queue WHERE kind = 'fix' ORDER BY id"
    ).fetchall()
    return [str(row[0]) for row in rows]


class TestChecksThatCouldNotRun:
    """The first real press of a merge card (FEAT-3ABD, 2026-09-06) merged the
    branch and then said "the post-merge checks did not pass: test runner could
    not start" — and filed a repair row for a failure no code could fix. Both
    halves of that are fixed here."""

    def _report(self, verify_status: str, **extra: Any) -> dict[str, Any]:
        report = {
            "outcome": "merged",
            "post_sha": "c" * 40,
            "verify_ok": False,
            "verify_status": verify_status,
            "verify_detail": "test runner could not start",
            "charged_failures": [],
        }
        report.update(extra)
        return report

    @pytest.mark.asyncio
    async def test_checks_that_could_not_run_say_so(
        self, config, pool, repo_root
    ) -> None:
        gk = _FakeGuardKit(status="failed", report=self._report("unverified"))
        deps, publisher, gk, dp = _deps(config, pool, guardkit=gk)
        outcome = await _run_executor(deps, repo_root)

        assert outcome.result == "merged-verify-failed"
        assert outcome.verify_status == "unverified"
        assert "could not run: test runner could not start" in outcome.detail
        assert "did not pass" not in outcome.detail
        assert "The deploy was not dispatched." in outcome.detail
        assert "promote" not in _legs(dp)
        assert publisher.reports[0].verify_status == "unverified"

    @pytest.mark.asyncio
    async def test_checks_that_could_not_run_file_no_repair(
        self, config, pool, repo_root, caplog
    ) -> None:
        gk = _FakeGuardKit(status="failed", report=self._report("unverified"))
        deps, publisher, gk, dp = _deps(config, pool, guardkit=gk)
        with caplog.at_level("INFO"):
            outcome = await _run_executor(deps, repo_root)

        assert outcome.result == "merged-verify-failed"
        assert _repair_rows(pool) == []
        assert any(
            "could not run, so no repair was filed" in record.getMessage()
            and FEATURE_ID in record.getMessage()
            for record in caplog.records
        ), [r.getMessage() for r in caplog.records]

    @pytest.mark.asyncio
    async def test_checks_that_ran_and_went_red_still_say_did_not_pass(
        self, config, pool, repo_root
    ) -> None:
        gk = _FakeGuardKit(
            status="failed",
            report=self._report(
                "failed",
                verify_detail="2 charged failures",
                charged_failures=[
                    "tests/test_a.py::test_one",
                    "tests/test_b.py::test_two",
                ],
            ),
        )
        deps, publisher, gk, dp = _deps(config, pool, guardkit=gk)
        outcome = await _run_executor(deps, repo_root)

        assert outcome.verify_status == "failed"
        assert "did not pass" in outcome.detail
        assert "could not run" not in outcome.detail
        assert "2 charged failure(s)" in outcome.detail

    @pytest.mark.asyncio
    async def test_checks_that_ran_and_went_red_file_a_repair(
        self, config, pool, repo_root
    ) -> None:
        gk = _FakeGuardKit(
            status="failed",
            report=self._report(
                "failed", charged_failures=["tests/test_a.py::test_one"]
            ),
        )
        deps, publisher, gk, dp = _deps(config, pool, guardkit=gk)
        await _run_executor(deps, repo_root)

        rows = _repair_rows(pool)
        assert len(rows) == 1
        assert FEATURE_ID in rows[0]

    @pytest.mark.asyncio
    async def test_a_report_with_no_word_about_the_checks_still_files(
        self, config, pool, repo_root
    ) -> None:
        """An older merge report says only verify_ok=false. The checks ran as
        far as anyone can tell, so the repair is still filed."""
        gk = _FakeGuardKit(
            status="failed",
            report={
                "outcome": "merged",
                "post_sha": "c" * 40,
                "verify_ok": False,
                "verify_detail": "3 tests failed",
            },
        )
        deps, publisher, gk, dp = _deps(config, pool, guardkit=gk)
        outcome = await _run_executor(deps, repo_root)
        assert outcome.verify_status == "failed"
        assert len(_repair_rows(pool)) == 1

    @pytest.mark.asyncio
    async def test_a_reverted_deploy_still_files_a_repair(
        self, config, pool, repo_root
    ) -> None:
        deps, publisher, gk, dp = _deps(
            config, pool, deploy=_FakeDeploy(outcome="reverted", verdict="fail")
        )
        outcome = await _run_executor(deps, repo_root)
        assert outcome.result == "merged-deploy-reverted"
        assert len(_repair_rows(pool)) == 1


# ---------------------------------------------------------------------------
# The merge run through the deploy sidecar reads exactly the same
# ---------------------------------------------------------------------------


class TestTheSameMergeThroughTheSidecar:
    """The whole point of moving the checks to the host is that NOTHING else
    changes. Drive the executor twice on the same merge report — once with the
    in-container run faked, once through a REAL deploy sidecar on an ephemeral
    loopback port running a REAL fake guardkit executable — and compare the
    outcome and the receipts."""

    REPORT = {
        "outcome": "merged",
        "post_sha": "e" * 40,
        "verify_ok": True,
        "verify_status": "passed",
        "charged_failures": [],
        "checks_passed": 17,
        "checks_total": 17,
    }

    @staticmethod
    def _fresh_pool(path: Path) -> SqliteLifecyclePersistence:
        cx = sqlite_connect.connect_writer(path)
        migrations.apply_at_boot(cx)
        return SqliteLifecyclePersistence(connection=cx)

    @staticmethod
    def _normalise(receipt: dict[str, Any]) -> dict[str, Any]:
        """Take out the three things that cannot be equal across two runs: how
        long each took, when each finished, and the newline a real command
        prints at the end of its report where a fake in-process one does not.
        Everything else — the report, the sha, the counts, the outcome — is
        compared exactly."""
        cleaned = dict(receipt)
        for key in ("duration_secs", "completed_at"):
            cleaned.pop(key, None)
        if isinstance(cleaned.get("stdout_tail"), str):
            cleaned["stdout_tail"] = cleaned["stdout_tail"].strip()
        return cleaned

    @pytest.mark.asyncio
    async def test_same_outcome_and_receipts_either_way(
        self, config, pool, repo_root, tmp_path, monkeypatch
    ) -> None:
        import stat as stat_module
        import threading

        from forge.adapters.guardkit.run_via_sidecar import (
            build_sidecar_guardkit_run,
        )
        from forge.deploy_sidecar.service import (
            GUARDKIT_PATH_ENV,
            build_server,
        )

        baseline = ["tests/test_known_red.py::test_password"]
        merged = _tip(repo_root)
        report_of_record = {**self.REPORT, "post_sha": merged}

        # --- run one: the in-container guardkit, faked ---------------------
        in_container_receipts = tmp_path / "receipts-in-container"
        in_container_pool = self._fresh_pool(tmp_path / "in-container.db")
        publisher_a = _FakePublisher()
        deps_a = MergeExecutorDeps(
            config=config,
            pool=in_container_pool,
            pipeline_publisher=publisher_a,
            guardkit_run=_FakeGuardKit(report=dict(report_of_record)),
            deploy_dispatcher=_FakeDeploy(),
            receipts_root_fn=lambda: in_container_receipts,
        )
        outcome_a = await _run_executor(
            deps_a, repo_root, baseline_failing=baseline
        )

        # --- run two: the same report, through a real sidecar --------------
        report_file = tmp_path / "merge-report.json"
        report_file.write_text(json.dumps(report_of_record), encoding="utf-8")
        fake_guardkit = tmp_path / "bin" / "guardkit"
        fake_guardkit.parent.mkdir(parents=True, exist_ok=True)
        fake_guardkit.write_text(
            "#!/usr/bin/env python3\n"
            f"print(open({str(report_file)!r}).read())\n",
            encoding="utf-8",
        )
        fake_guardkit.chmod(
            fake_guardkit.stat().st_mode | stat_module.S_IXUSR | stat_module.S_IXOTH
        )
        monkeypatch.setenv(GUARDKIT_PATH_ENV, str(fake_guardkit))

        server = build_server(port=0, config_loader=lambda: config)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address[:2]
            sidecar_receipts = tmp_path / "receipts-sidecar"
            sidecar_pool = self._fresh_pool(tmp_path / "sidecar.db")
            publisher_b = _FakePublisher()
            deps_b = MergeExecutorDeps(
                config=config,
                pool=sidecar_pool,
                pipeline_publisher=publisher_b,
                guardkit_run=build_sidecar_guardkit_run(
                    base_url=f"http://{host}:{port}",
                    repo_paths={REPO: str(repo_root)},
                ),
                deploy_dispatcher=_FakeDeploy(),
                receipts_root_fn=lambda: sidecar_receipts,
            )
            outcome_b = await _run_executor(
                deps_b, repo_root, baseline_failing=baseline
            )
        finally:
            server.shutdown()
            server.server_close()

        # The merge really did run on the host, through the sidecar.
        host_baseline = (
            repo_root / ".guardkit" / "tmp" / f"merge-baseline-{FEATURE_ID}.json"
        )
        assert json.loads(host_baseline.read_text(encoding="utf-8")) == {
            "failing_node_ids": baseline
        }

        # The outcome a person reads is identical.
        assert outcome_a.result == outcome_b.result == "merged-and-running"
        assert outcome_a.detail == outcome_b.detail
        assert outcome_a.merged_sha == outcome_b.merged_sha == merged
        assert outcome_a.checks_passed == outcome_b.checks_passed == 17
        assert outcome_a.checks_total == outcome_b.checks_total == 17
        assert outcome_a.verify_status == outcome_b.verify_status

        # And so are the receipts, once the clock is taken out of them.
        for name in (
            "merge_deploy_merge.json",
            "digest_conformance.json",
            "merge_deploy_tree_check.json",
            "merge_deploy_deploy.json",
            "merge_deploy_report.json",
        ):
            left = json.loads(
                (in_container_receipts / f"merge-{BUILD_ID}" / name).read_text(
                    encoding="utf-8"
                )
            )
            right = json.loads(
                (sidecar_receipts / f"merge-{BUILD_ID}" / name).read_text(
                    encoding="utf-8"
                )
            )
            assert self._normalise(left) == self._normalise(right), name


class TestTheTwoTimeLimits:
    """One limit holds each run of the checks; the other holds the whole
    command. They have to compose, or the outer one fires first and kills a
    merge that has already landed — which is exactly what the small-scale
    drive of this lane caught.
    """

    def test_the_wall_holds_two_check_runs_and_the_merge(self) -> None:
        from forge.pipeline.merge_executor import (
            MERGE_WALL_MERGE_ALLOWANCE_SECONDS,
            merge_wall_seconds,
        )

        assert MERGE_WALL_MERGE_ALLOWANCE_SECONDS == 180
        # The merge command may run the checks twice: once on main to see what
        # was already failing, once on the merged tree.
        assert merge_wall_seconds(600) == 2 * 600 + 180
        assert merge_wall_seconds(60) == 2 * 60 + 180

    def test_the_wall_never_asks_for_more_than_is_allowed(self) -> None:
        from forge.pipeline.merge_executor import (
            MERGE_WALL_CAP_SECONDS,
            merge_wall_seconds,
        )

        assert merge_wall_seconds(1000) == MERGE_WALL_CAP_SECONDS
        assert merge_wall_seconds(9999) == MERGE_WALL_CAP_SECONDS

    def test_the_cap_written_here_is_the_one_the_sidecar_enforces(self) -> None:
        """A wall bigger than the sidecar's cap is refused outright, so the
        number forge works to must be the number the sidecar keeps."""
        from forge.deploy_sidecar.service import MERGE_TIMEOUT_MAX
        from forge.pipeline.merge_executor import MERGE_WALL_CAP_SECONDS

        assert float(MERGE_WALL_CAP_SECONDS) == MERGE_TIMEOUT_MAX

    @pytest.mark.asyncio
    async def test_the_default_limits_are_ten_minutes_and_a_wall_that_holds_two(
        self, config, pool, repo_root
    ) -> None:
        deps, _publisher, gk, _dp = _deps(config, pool)
        await _run_executor(deps, repo_root)

        args = gk.calls[0]["args"]
        assert "--verify-timeout" in args
        assert args[args.index("--verify-timeout") + 1] == "600"
        assert gk.calls[0]["timeout_seconds"] == 2 * 600 + 180

    @pytest.mark.asyncio
    async def test_a_longer_limit_for_the_checks_widens_the_wall(
        self, pool, repo_root, tmp_path
    ) -> None:
        config = ForgeConfig.model_validate(
            {
                "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
                "planning": {"target_repo_paths": {REPO: str(repo_root)}},
                "approval": {"expected_approver": "rich"},
                "merge_executor": {
                    "enabled": True,
                    "verify_timeout_seconds": 300,
                },
            }
        )
        deps, _publisher, gk, _dp = _deps(config, pool)
        await _run_executor(deps, repo_root)

        args = gk.calls[0]["args"]
        assert args[args.index("--verify-timeout") + 1] == "300"
        assert gk.calls[0]["timeout_seconds"] == 2 * 300 + 180

    @pytest.mark.asyncio
    async def test_a_very_long_limit_is_held_to_the_cap(
        self, pool, repo_root
    ) -> None:
        config = ForgeConfig.model_validate(
            {
                "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
                "planning": {"target_repo_paths": {REPO: str(repo_root)}},
                "approval": {"expected_approver": "rich"},
                "merge_executor": {
                    "enabled": True,
                    "verify_timeout_seconds": 1200,
                },
            }
        )
        deps, _publisher, gk, _dp = _deps(config, pool)
        await _run_executor(deps, repo_root)

        assert gk.calls[0]["timeout_seconds"] == 1800


class TestARefusalSpeaksTheMergeCommandsOwnSentence:
    """When the merge command explains itself, forge repeats it word for word
    instead of wrapping it in words of its own."""

    @pytest.mark.asyncio
    async def test_the_reports_own_reason_is_used_verbatim(
        self, config, pool, repo_root
    ) -> None:
        sentence = "main has moved since the checks ran; the merge was refused"
        gk = _FakeGuardKit(
            status="failed",
            report={"outcome": "refused", "refusal_reason": sentence},
            stderr="Refused: exit code 2",
        )
        deps, publisher, gk, dp = _deps(config, pool, guardkit=gk)
        outcome = await _run_executor(deps, repo_root)

        assert outcome.result == "merge-refused"
        assert outcome.detail == sentence
        assert "status=" not in outcome.detail
        assert "promote" not in _legs(dp)
        assert publisher.reports[0].detail == sentence

    @pytest.mark.asyncio
    async def test_an_empty_reason_falls_back_to_the_existing_words(
        self, config, pool, repo_root
    ) -> None:
        gk = _FakeGuardKit(
            status="failed",
            report={"outcome": "refused", "refusal_reason": "   "},
            stderr="the branch autobuild/FEAT-MX1 does not exist",
        )
        deps, _publisher, gk, dp = _deps(config, pool, guardkit=gk)
        outcome = await _run_executor(deps, repo_root)

        assert outcome.result == "merge-refused"
        assert "the merge command did not succeed" in outcome.detail
        assert "does not exist" in outcome.detail
        assert "promote" not in _legs(dp)

    @pytest.mark.asyncio
    async def test_a_report_with_no_reason_at_all_still_reads_plainly(
        self, config, pool, repo_root
    ) -> None:
        gk = _FakeGuardKit(
            status="success",
            report={"status": "refused", "detail": "the working tree is dirty"},
        )
        deps, _publisher, gk, dp = _deps(config, pool, guardkit=gk)
        outcome = await _run_executor(deps, repo_root)

        assert outcome.result == "merge-refused"
        assert outcome.detail == "the working tree is dirty"
        assert "promote" not in _legs(dp)


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Where the deploy ran (the 2026-09-06 decision)
#
# A repository whose deploy profile carries a sandbox block is deployed into
# its own Docker Sandbox, and the outcome says so. Jarvis reads that word to
# say "running in its Docker Sandbox" on the one line Rich sees after a press.
# No block ⇒ no word ⇒ the report is exactly what it was.
# ---------------------------------------------------------------------------


SANDBOX_PROFILE = """\
env_id: local
compose:
  file: docker-compose.yml
  script: deploy/sandbox-deploy.sh
cwd: /somewhere/api_test
sandbox:
  name: api-test-deploy
  memory: 6g
  cpus: 4
  publish: ["127.0.0.1:8901:8901", "127.0.0.1:8902:8902"]
  allow_network: ["pypi.org"]
"""

PLAIN_PROFILE = """\
env_id: local
compose:
  file: docker-compose.yml
  script: deploy/deploy.sh
cwd: /somewhere/api_test
"""


def _write_deploy_profile(repo_root: Path, text: str) -> None:
    (repo_root / "deploy").mkdir(parents=True, exist_ok=True)
    (repo_root / "deploy" / "profile.yaml").write_text(text, encoding="utf-8")


class TestWhereTheDeployRan:
    @pytest.mark.asyncio
    async def test_a_sandbox_profile_says_docker_sandbox(
        self, config, pool, repo_root
    ) -> None:
        _write_deploy_profile(repo_root, SANDBOX_PROFILE)
        deps, publisher, gk, dp = _deps(config, pool)
        outcome = await _run_executor(deps, repo_root)
        assert outcome.result == "merged-and-running"
        assert outcome.deployed_in == "docker-sandbox"
        assert publisher.reports[0].deployed_in == "docker-sandbox"

    @pytest.mark.asyncio
    async def test_it_rides_the_payload_jarvis_reads(
        self, config, pool, repo_root
    ) -> None:
        _write_deploy_profile(repo_root, SANDBOX_PROFILE)
        deps, publisher, gk, dp = _deps(config, pool)
        await _run_executor(deps, repo_root)
        raw = publisher.reports[0].model_dump(mode="json")
        assert raw["deployed_in"] == "docker-sandbox"

    @pytest.mark.asyncio
    async def test_a_revert_still_says_where_it_ran(
        self, config, pool, repo_root
    ) -> None:
        _write_deploy_profile(repo_root, SANDBOX_PROFILE)
        deps, publisher, gk, dp = _deps(
            config, pool, deploy=_FakeDeploy(outcome="reverted", verdict="fail")
        )
        outcome = await _run_executor(deps, repo_root)
        assert outcome.result == "merged-deploy-reverted"
        assert outcome.deployed_in == "docker-sandbox"

    @pytest.mark.asyncio
    async def test_a_profile_without_the_block_says_nothing(
        self, config, pool, repo_root
    ) -> None:
        _write_deploy_profile(repo_root, PLAIN_PROFILE)
        deps, publisher, gk, dp = _deps(config, pool)
        outcome = await _run_executor(deps, repo_root)
        assert outcome.result == "merged-and-running"
        assert outcome.deployed_in is None
        assert "deployed_in" not in publisher.reports[0].model_dump(mode="json")

    @pytest.mark.asyncio
    async def test_no_profile_at_all_says_nothing_and_fails_nothing(
        self, config, pool, repo_root
    ) -> None:
        deps, publisher, gk, dp = _deps(config, pool)
        outcome = await _run_executor(deps, repo_root)
        assert outcome.result == "merged-and-running"
        assert outcome.deployed_in is None

    @pytest.mark.asyncio
    async def test_an_unreadable_profile_never_fails_a_merge(
        self, config, pool, repo_root
    ) -> None:
        # Where the deploy ran is a word on a card. A profile nobody can read
        # must cost that word, and nothing else.
        _write_deploy_profile(repo_root, "this: [is not: valid yaml")
        deps, publisher, gk, dp = _deps(config, pool)
        outcome = await _run_executor(deps, repo_root)
        assert outcome.result == "merged-and-running"
        assert outcome.deployed_in is None

    @pytest.mark.asyncio
    async def test_a_deploy_that_never_ran_claims_no_sandbox(
        self, config, pool, repo_root
    ) -> None:
        _write_deploy_profile(repo_root, SANDBOX_PROFILE)
        deps, publisher, gk, dp = _deps(config, pool, deploy=_FakeDeploy(outcome=None))
        outcome = await _run_executor(deps, repo_root)
        assert outcome.result == "candidate-refused"
        assert outcome.deployed_in is None

    def test_the_question_answered_on_its_own(self, tmp_path) -> None:
        from forge.pipeline.merge_executor import deployed_in_for

        _write_deploy_profile(tmp_path, SANDBOX_PROFILE)
        assert deployed_in_for(tmp_path) == "docker-sandbox"
        _write_deploy_profile(tmp_path, PLAIN_PROFILE)
        assert deployed_in_for(tmp_path) is None
        assert deployed_in_for(tmp_path / "nowhere") is None


# ---------------------------------------------------------------------------
# Protect main: the candidate is checked BEFORE the merge (Part J, rule 42)
#
# The order inside the merge word: (1) the branch's exact tree is laid out and
# checked in the sandbox; (2) only a green check merges; (3) the merged tree
# must be the checked tree; (4) the checked image is promoted; (5) the
# candidate comes down and its tree is removed — on every ending.
# ---------------------------------------------------------------------------


class _OrderedGuardKit(_FakeGuardKit):
    """Writes into a shared order list so the interleaving can be asserted."""

    def __init__(self, order: list[str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._order = order

    async def __call__(self, **kwargs: Any) -> GuardKitResult:
        self._order.append("merge")
        return await super().__call__(**kwargs)


class _OrderedDeploy(_FakeDeploy):
    def __init__(self, order: list[str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._order = order

    async def __call__(self, **kwargs: Any) -> Any:
        self._order.append(kwargs.get("leg", "deploy"))
        return await super().__call__(**kwargs)


def _exclude_lines(repo_root: Path) -> list[str]:
    exclude = repo_root / ".git" / "info" / "exclude"
    if not exclude.is_file():
        return []
    return [
        line for line in exclude.read_text(encoding="utf-8").splitlines()
        if line.strip() == ".forge-candidates/"
    ]


class TestTheCandidateIsCheckedBeforeTheMerge:
    @pytest.mark.asyncio
    async def test_the_order_is_candidate_then_merge_then_promote(
        self, config, pool, repo_root
    ) -> None:
        order: list[str] = []
        gk = _OrderedGuardKit(order)
        dp = _OrderedDeploy(order)
        deps, publisher, gk, dp = _deps(config, pool, guardkit=gk, deploy=dp)
        outcome = await _run_executor(deps, repo_root)
        assert outcome.result == "merged-and-running"
        assert order == ["candidate_check", "merge", "promote"]
        # Both legs belong to ONE deploy run.
        run_ids = {call["deploy_run_id"] for call in dp.calls}
        task_ids = {call["task_id"] for call in dp.calls}
        assert len(run_ids) == 1 and len(task_ids) == 1
        # The promote carries the events the candidate leg already published.
        promote = [c for c in dp.calls if c["leg"] == "promote"][0]
        assert promote["prior_events"] == ("DeployQueued",)

    @pytest.mark.asyncio
    async def test_the_candidate_is_built_from_the_branch_s_own_tree(
        self, config, pool, repo_root
    ) -> None:
        deps, publisher, gk, dp = _deps(config, pool)
        await _run_executor(deps, repo_root)
        expected = _candidate_dir(repo_root)
        assert dp.calls[0]["candidate_cwd"] == str(expected)
        # While the candidate leg ran, the tree was there, it was the
        # branch's (its file present), and it carried no git metadata.
        assert dp.seen_tree == {
            "cwd": str(expected),
            "existed": True,
            "branch_file": True,
            "git_dir": False,
        }
        # The checkout itself is still main: the branch's file is not in it.
        assert not (repo_root / f"{FEATURE_ID}.txt").exists()

    @pytest.mark.asyncio
    async def test_a_red_check_never_calls_the_merge(
        self, config, pool, repo_root, _receipts_env: Path
    ) -> None:
        dp = _FakeDeploy(candidate_outcome="failed", candidate_verdict="fail")
        deps, publisher, gk, dp = _deps(config, pool, deploy=dp)
        outcome = await _run_executor(deps, repo_root)

        assert outcome.result == "candidate-refused"
        assert outcome.status == "FAILED"
        assert outcome.failed_step == "candidate"
        assert gk.calls == []  # the merge command was never invoked
        # The leg tore its own candidate down; no second teardown, no promote.
        assert _legs(dp) == ["candidate_check"]
        assert outcome.detail == (
            f"{FEATURE_ID} was checked in the sandbox before merging and failed "
            "2 of 8 checks (users_count, etag); nothing was merged and the "
            "branch is kept."
        )
        assert outcome.merged_sha is None
        # No merge claim on the record: the build may be pressed again once
        # the branch is repaired. The check itself is on the record.
        ids = _stage_ids(pool)
        assert MERGE_STEP_MERGE_TARGET_IDENTIFIER not in ids
        assert MERGE_STEP_DEPLOY_TARGET_IDENTIFIER not in ids
        assert MERGE_STEP_CANDIDATE_TARGET_IDENTIFIER in ids
        assert publisher.reports[0].result == "candidate-refused"
        receipts = _receipts_env / f"merge-{BUILD_ID}"
        assert (receipts / "merge_deploy_candidate.json").is_file()
        assert not (receipts / "merge_deploy_merge.json").exists()
        assert not (receipts / "merge_deploy_deploy.json").exists()

    @pytest.mark.asyncio
    async def test_a_red_check_files_the_repair_row_with_the_failing_names(
        self, config, pool, repo_root
    ) -> None:
        dp = _FakeDeploy(candidate_outcome="failed", candidate_verdict="fail")
        deps, publisher, gk, dp = _deps(config, pool, deploy=dp)
        await _run_executor(deps, repo_root)
        rows = _repair_rows(pool)
        assert rows == [
            f"{FEATURE_ID} was checked in the sandbox before merging and failed "
            "2 of 8 checks (users_count, etag); nothing was merged and the "
            "branch is kept."
        ]

    @pytest.mark.asyncio
    async def test_a_candidate_that_never_came_up_says_so_and_files_a_repair(
        self, config, pool, repo_root
    ) -> None:
        dp = _FakeDeploy(
            candidate_outcome="failed",
            candidate_verdict=None,
            candidate_reason="candidate_deploy_failed",
            candidate_failed_step="health_check",
            gate={"verdict": None, "checks_total": None, "checks_passed": None,
                  "failed_checks": None, "failed_step": "health_check"},
        )
        deps, publisher, gk, dp = _deps(config, pool, deploy=dp)
        outcome = await _run_executor(deps, repo_root)
        assert outcome.result == "candidate-refused"
        assert outcome.detail == (
            f"{FEATURE_ID} was checked in the sandbox before merging and could "
            "not be started (the candidate deploy stopped at health_check); "
            "nothing was merged and the branch is kept."
        )
        assert gk.calls == []
        assert _repair_rows(pool) == [outcome.detail]

    @pytest.mark.asyncio
    async def test_a_check_that_could_not_run_files_no_repair(
        self, config, pool, repo_root, caplog
    ) -> None:
        dp = _FakeDeploy(
            candidate_outcome="failed", candidate_reason="no_candidate_section"
        )
        deps, publisher, gk, dp = _deps(config, pool, deploy=dp)
        with caplog.at_level("INFO"):
            outcome = await _run_executor(deps, repo_root)
        assert outcome.result == "candidate-refused"
        assert "has no candidate section" in outcome.detail
        assert "nothing was merged" in outcome.detail
        assert gk.calls == []
        assert _repair_rows(pool) == []
        assert any(
            "could not run, so no repair was filed" in r.getMessage()
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_a_missing_branch_is_refused_before_anything_runs(
        self, config, pool, repo_root
    ) -> None:
        _git(repo_root, "branch", "-D", f"autobuild/{FEATURE_ID}")
        deps, publisher, gk, dp = _deps(config, pool)
        outcome = await _run_executor(deps, repo_root)
        assert outcome.result == "candidate-refused"
        assert f"the branch autobuild/{FEATURE_ID} was not found" in outcome.detail
        assert gk.calls == [] and dp.calls == []
        assert _repair_rows(pool) == []

    @pytest.mark.asyncio
    async def test_a_merge_refusal_after_a_green_check_takes_the_candidate_down(
        self, config, pool, repo_root
    ) -> None:
        gk = _FakeGuardKit(
            status="failed",
            report={"outcome": "refused", "refusal_reason": "main has moved"},
        )
        deps, publisher, gk, dp = _deps(config, pool, guardkit=gk)
        outcome = await _run_executor(deps, repo_root)
        assert outcome.result == "merge-refused"
        assert outcome.detail == "main has moved"
        # The candidate that passed was torn down; nothing was promoted.
        assert _legs(dp) == ["candidate_check", "candidate_down"]
        assert not _candidate_dir(repo_root).exists()
        # The merge step was released, so the build can be pressed again.
        rows = [
            s for s in pool.read_stages(BUILD_ID)
            if s.target_identifier == MERGE_STEP_MERGE_TARGET_IDENTIFIER
        ]
        assert rows[-1].status == "SKIPPED"

    @pytest.mark.asyncio
    async def test_a_tree_that_is_not_the_checked_tree_refuses_the_promote(
        self, config, pool, repo_root, caplog
    ) -> None:
        """Main moved under the check in a way the pin did not catch: the
        merge report names a commit whose tree is not the candidate's."""
        # A commit on main that the candidate never saw.
        _git(repo_root, "checkout", "-q", "main")
        (repo_root / "moved.txt").write_text("main moved\n", encoding="utf-8")
        _git(repo_root, "add", "moved.txt")
        _git(repo_root, "commit", "-q", "-m", "main moved")
        other = _git(repo_root, "rev-parse", "main")
        gk = _FakeGuardKit(report={"status": "merged", "merged_sha": other})
        deps, publisher, gk, dp = _deps(config, pool, guardkit=gk)
        with caplog.at_level("INFO"):
            outcome = await _run_executor(deps, repo_root)

        assert outcome.result == "merged-deploy-failed"
        assert outcome.failed_step == "promote"
        assert outcome.merged_sha == other
        assert "is not the tree that was checked in the sandbox" in outcome.detail
        assert "nothing live changed" in outcome.detail
        assert "Send the sentence again" in outcome.detail
        # No promote; the candidate came down.
        assert _legs(dp) == ["candidate_check", "candidate_down"]
        gate = outcome.gate_before_merge
        assert gate["trees_match"] is False
        assert gate["candidate_tree"] == _tree_of(repo_root, _tip(repo_root))
        assert gate["merged_tree"] == _tree_of(repo_root, other)
        assert gate["candidate_tree"] != gate["merged_tree"]
        # There is nothing to repair; the remedy is a new run.
        assert _repair_rows(pool) == []
        assert any("main had moved under the check" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_a_merge_report_without_a_commit_refuses_the_promote(
        self, config, pool, repo_root
    ) -> None:
        gk = _FakeGuardKit(report={"status": "merged"})
        deps, publisher, gk, dp = _deps(config, pool, guardkit=gk)
        outcome = await _run_executor(deps, repo_root)
        assert outcome.result == "merged-deploy-failed"
        assert outcome.failed_step == "promote"
        assert "names no merged commit" in outcome.detail
        assert "promote" not in _legs(dp)

    @pytest.mark.asyncio
    async def test_the_exclude_line_is_written_once(
        self, config, pool, repo_root
    ) -> None:
        assert _exclude_lines(repo_root) == []
        deps, publisher, gk, dp = _deps(config, pool)
        await _run_executor(deps, repo_root, dry_run=True)
        assert _exclude_lines(repo_root) == [".forge-candidates/"]
        await _run_executor(deps, repo_root)
        assert _exclude_lines(repo_root) == [".forge-candidates/"]
        # And with the tree laid out the checkout reads clean to git — the
        # merge command's own dirty-tree check would not see it.
        assert _git(repo_root, "status", "--porcelain") == ""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "ending",
        [
            "green",
            "dry-run",
            "candidate-red",
            "merge-refused",
            "verify-failed",
            "tree-mismatch",
            "promote-raises",
            "promote-reverted",
            "deploy-off",
        ],
    )
    async def test_the_tree_is_removed_on_every_ending(
        self, config, pool, repo_root, ending: str
    ) -> None:
        gk: _FakeGuardKit | None = None
        dp: _FakeDeploy | None = None
        dry_run = False
        if ending == "dry-run":
            dry_run = True
        elif ending == "candidate-red":
            dp = _FakeDeploy(candidate_outcome="failed", candidate_verdict="fail")
        elif ending == "merge-refused":
            gk = _FakeGuardKit(status="failed", report={"outcome": "refused", "refusal_reason": "no"})
        elif ending == "verify-failed":
            gk = _FakeGuardKit(
                status="failed",
                report={"outcome": "merged", "post_sha": _tip(repo_root), "verify_ok": False,
                        "verify_status": "failed", "verify_detail": "1 test failed"},
            )
        elif ending == "tree-mismatch":
            _git(repo_root, "checkout", "-q", "main")
            (repo_root / "moved.txt").write_text("m\n", encoding="utf-8")
            _git(repo_root, "add", "moved.txt")
            _git(repo_root, "commit", "-q", "-m", "moved")
            gk = _FakeGuardKit(report={"status": "merged", "merged_sha": _git(repo_root, "rev-parse", "main")})
        elif ending == "promote-raises":
            dp = _FakeDeploy(raises=RuntimeError("the sidecar went away"))
        elif ending == "promote-reverted":
            dp = _FakeDeploy(outcome="reverted", verdict="fail")
        elif ending == "deploy-off":
            dp = _FakeDeploy(outcome=None)
        deps, publisher, gk, dp = _deps(config, pool, guardkit=gk, deploy=dp)
        await _run_executor(deps, repo_root, dry_run=dry_run)
        assert not _candidate_dir(repo_root).exists()
        assert not (repo_root / ".forge-candidates").exists() or not any(
            (repo_root / ".forge-candidates").iterdir()
        )

    @pytest.mark.asyncio
    async def test_the_candidate_comes_down_when_the_promote_raises(
        self, config, pool, repo_root
    ) -> None:
        dp = _FakeDeploy(raises=RuntimeError("the sidecar went away"))
        deps, publisher, gk, dp = _deps(config, pool, deploy=dp)
        outcome = await _run_executor(deps, repo_root)
        assert outcome.result == "merged-deploy-failed"
        assert outcome.failed_step == "deploy"
        assert "the promote dispatch raised" in outcome.detail
        assert _legs(dp) == ["candidate_check", "promote", "candidate_down"]

    @pytest.mark.asyncio
    async def test_the_report_carries_what_the_check_found(
        self, config, pool, repo_root
    ) -> None:
        deps, publisher, gk, dp = _deps(config, pool)
        outcome = await _run_executor(deps, repo_root)
        assert outcome.result == "merged-and-running"
        tip = _tip(repo_root)
        gate = outcome.gate_before_merge
        assert gate["verdict"] == "pass"
        assert gate["checks_passed"] == 8 and gate["checks_total"] == 8
        assert gate["failed_checks"] == []
        assert gate["candidate_sha"] == tip
        assert gate["candidate_tree"] == _tree_of(repo_root, tip)
        assert gate["merged_tree"] == gate["candidate_tree"]
        assert gate["trees_match"] is True
        # It rides the payload jarvis reads, and the row on the build's record.
        raw = publisher.reports[0].model_dump(mode="json")
        for key in ("verdict", "checks_passed", "checks_total", "candidate_sha",
                    "candidate_tree", "merged_tree"):
            assert key in raw["gate_before_merge"], key
        assert raw["gate_before_merge"]["candidate_sha"] == tip
        assert _report_rows(pool)[0].details["gate_before_merge"]["trees_match"] is True
        assert "checked in the sandbox (8 of 8), merged and running" in outcome.detail

    @pytest.mark.asyncio
    async def test_a_report_that_stopped_before_the_check_carries_no_gate(
        self, config, pool, repo_root
    ) -> None:
        deps, publisher, gk, dp = _deps(config, pool)
        await _run_executor(deps, repo_root)
        second = await _run_executor(deps, repo_root)  # refused at the probe
        assert second.result == "merge-refused"
        assert second.gate_before_merge is None
        assert "gate_before_merge" not in publisher.reports[1].model_dump(mode="json")

    @pytest.mark.asyncio
    async def test_a_dry_run_checks_and_promotes_dry_and_merges_nothing(
        self, config, pool, repo_root, _receipts_env: Path
    ) -> None:
        deps, publisher, gk, dp = _deps(config, pool)
        outcome = await _run_executor(deps, repo_root, dry_run=True)
        assert gk.calls == []
        assert _legs(dp) == ["candidate_check", "promote"]
        assert all(call["dry_run"] is True for call in dp.calls)
        assert _stage_ids(pool) == []  # not even the candidate row
        assert outcome.merged_sha is None
        assert outcome.gate_before_merge["trees_match"] is None
        receipts = _receipts_env / f"merge-{BUILD_ID}"
        assert json.loads((receipts / "merge_deploy_candidate.json").read_text())["dry_run"] is True
        assert not (receipts / "merge_deploy_tree_check.json").exists()
        cleanup = json.loads((receipts / "merge_deploy_cleanup.json").read_text())
        assert cleanup["tree_removed"] is True

    def test_the_result_words(self) -> None:
        """The words a report may carry, and which of them file a repair."""
        from forge.pipeline.merge_executor import RED_MERGE_ENDINGS

        assert RED_MERGE_ENDINGS == {
            "merged-verify-failed",
            "merged-deploy-reverted",
            "merged-deploy-failed",
        }
        assert "candidate-refused" not in RED_MERGE_ENDINGS  # filed on its own rule
        assert "merge-refused" not in RED_MERGE_ENDINGS

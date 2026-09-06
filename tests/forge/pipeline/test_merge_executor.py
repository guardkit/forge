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
from forge.lifecycle.metrics import (
    MERGE_READY_TARGET_IDENTIFIER,
    self_closed_defect_rate,
)
from forge.pipeline.merge_executor import (
    MERGE_DECISION_TARGET_IDENTIFIER,
    MERGE_REPORT_TARGET_IDENTIFIER,
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
        assert dp.calls == []  # no deploy on red checks
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
        assert len(dp.calls) == 1
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
        assert dp.calls == []
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
        assert outcome.result == "merged-deploy-failed"
        assert outcome.deployed_in is None

    def test_the_question_answered_on_its_own(self, tmp_path) -> None:
        from forge.pipeline.merge_executor import deployed_in_for

        _write_deploy_profile(tmp_path, SANDBOX_PROFILE)
        assert deployed_in_for(tmp_path) == "docker-sandbox"
        _write_deploy_profile(tmp_path, PLAIN_PROFILE)
        assert deployed_in_for(tmp_path) is None
        assert deployed_in_for(tmp_path / "nowhere") is None
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

        # --- run one: the in-container guardkit, faked ---------------------
        in_container_receipts = tmp_path / "receipts-in-container"
        in_container_pool = self._fresh_pool(tmp_path / "in-container.db")
        publisher_a = _FakePublisher()
        deps_a = MergeExecutorDeps(
            config=config,
            pool=in_container_pool,
            pipeline_publisher=publisher_a,
            guardkit_run=_FakeGuardKit(report=dict(self.REPORT)),
            deploy_dispatcher=_FakeDeploy(),
            receipts_root_fn=lambda: in_container_receipts,
        )
        outcome_a = await _run_executor(
            deps_a, repo_root, baseline_failing=baseline
        )

        # --- run two: the same report, through a real sidecar --------------
        report_file = tmp_path / "merge-report.json"
        report_file.write_text(json.dumps(self.REPORT), encoding="utf-8")
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
        assert outcome_a.merged_sha == outcome_b.merged_sha == "e" * 40
        assert outcome_a.checks_passed == outcome_b.checks_passed == 17
        assert outcome_a.checks_total == outcome_b.checks_total == 17
        assert outcome_a.verify_status == outcome_b.verify_status

        # And so are the receipts, once the clock is taken out of them.
        for name in (
            "merge_deploy_merge.json",
            "digest_conformance.json",
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
        assert dp.calls == []
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
        assert dp.calls == []

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
        assert dp.calls == []

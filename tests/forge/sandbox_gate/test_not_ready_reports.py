"""A gated step never runs on a sandbox that is not ready (Codex S2
implementation review 1, R3): the merge press writes its report and touches
nothing, a preview refuses, and a recovery that reaches the sandbox waits for
it while every other repository's goes on.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forge.pipeline.merge_executor import MergeApprovalConsumer, execute_merge_deploy
from forge.sandbox_gate import gate as gate_module

from tests.forge.pipeline.test_merge_executor import (  # noqa: F401 — fixtures
    BUILD_ID,
    CORRELATION,
    FEATURE_ID,
    MAIN_SHA,
    REPO as PRESS_REPO,
    _deps,
    _ensure_build,
    _receipts_env,
    _report_rows,
    config,
    pool,
    repo_root,
)
from tests.forge.sandbox_gate._fakes import (
    REPO,
    SANDBOX,
    Clock,
    FakeStarter,
    a_config,
    a_gate,
    a_ledger,
)


class TestThePressReports:
    @pytest.mark.asyncio
    async def test_a_press_whose_sandbox_did_not_start_writes_its_report_and_touches_nothing(
        self, config, pool, repo_root
    ) -> None:
        deps, publisher, gk, dp = _deps(config, pool)
        _ensure_build(pool, build_id=BUILD_ID, feature_id=FEATURE_ID)
        outcome = await execute_merge_deploy(
            deps=deps,
            build_id=BUILD_ID,
            feature_id=FEATURE_ID,
            repo=PRESS_REPO,
            repo_root=repo_root,
            expect_main_sha=MAIN_SHA,
            correlation_id=CORRELATION,
            decided_by="rich",
            sandbox_not_ready="the sandbox notes-app-sbx did not start within 10 minutes: x",
        )
        assert outcome.result == "merge-refused" and outcome.failed_step == "sandbox"
        assert "its project's sandbox did not start" in outcome.detail
        assert gk.calls == [] and dp.calls == []  # nothing joined, checked or deployed
        [row] = _report_rows(pool)
        assert row.status == "FAILED"
        assert row.details["result"] == "merge-refused"

    @pytest.mark.asyncio
    async def test_the_approved_press_reports_when_the_gate_says_not_ready(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ledger = a_ledger(tmp_path)
        starter = FakeStarter(running=False)
        starter.start_refusal = "the sandbox client is not signed in"
        gate_module.install_gate(a_gate(ledger, starter, clock=Clock(), ready_timeout_seconds=60))
        seen: list[dict[str, Any]] = []

        async def fake_press(**kwargs: Any) -> Any:
            seen.append(kwargs)
            return SimpleNamespace(result="merge-refused")

        from forge.pipeline import merge_executor

        monkeypatch.setattr(merge_executor, "execute_merge_deploy", fake_press)
        try:
            consumer = MergeApprovalConsumer(SimpleNamespace())  # type: ignore[arg-type]
            await consumer._run_approved_locked(
                build_id="B-1",
                feature_id="FEAT-1",
                repo=REPO,
                repo_root=Path("/tmp/notes-app"),
                expect_main_sha="a" * 40,
                correlation_id="c-1",
                decided_by="rich",
                baseline_failing=None,
                merge_branch=None,
                expected_candidate_sha=None,
                expected_candidate_tree=None,
                expected_candidate_branch=None,
                worktree_retention=None,
                merge_execution_attempt_id="attempt-1",
                previewed_offer=None,
                expected_preview_tree=None,
                join_name=None,
            )
        finally:
            gate_module.install_gate(None)
            ledger.close()
        [press] = seen
        assert "not signed in" in press["sandbox_not_ready"]
        assert gate_module.current_gate() is None


class TestPreviewsRefuse:
    @pytest.mark.asyncio
    async def test_a_preview_is_refused_without_reaching_the_sandbox(self, tmp_path: Path) -> None:
        from forge.deploy.review_preview import ReviewPreviewService

        ledger = a_ledger(tmp_path)
        starter = FakeStarter(running=False)
        starter.start_refusal = "the sandbox client is not signed in"
        gate_module.install_gate(a_gate(ledger, starter, clock=Clock(), ready_timeout_seconds=60))
        try:
            service = ReviewPreviewService.__new__(ReviewPreviewService)
            service._clock = Clock().now  # type: ignore[attr-defined]
            service._run_id_factory = lambda: "run-1"  # type: ignore[attr-defined]
            recorded: list[dict[str, Any]] = []

            async def prepare(*a: Any, **k: Any) -> Any:
                raise AssertionError("nothing may reach the sandbox")

            def record(req: Any, **kwargs: Any) -> str:
                recorded.append(kwargs)
                return "refused"

            service._prepare = prepare  # type: ignore[method-assign]
            service._record = record  # type: ignore[method-assign]
            answer = await service.prepare(SimpleNamespace(project=REPO, build_id="B-1"))  # type: ignore[arg-type]
        finally:
            gate_module.install_gate(None)
            ledger.close()
        assert answer == "refused"
        assert recorded[0]["state"] == "failed"
        assert recorded[0]["failure_reason"].startswith("the preview was not prepared:")


class TestBootRecoveryIsDeferredPerRepository:
    @pytest.mark.asyncio
    async def test_a_planning_run_of_a_sandbox_not_up_waits_and_the_others_go(
        self, tmp_path: Path
    ) -> None:
        from forge.cli._serve_planning import sweep_interrupted_planning_runs

        from tests.forge.sandbox_gate._fakes import OTHER_REPO, a_planning_run

        ledger = a_ledger(tmp_path)
        a_planning_run(ledger, "RUNNING", repo=REPO)
        a_planning_run(ledger, "RUNNING", repo=OTHER_REPO)
        starter = FakeStarter(running=False)
        starter.reachable = False  # the starter is down at boot
        gate = a_gate(ledger, starter, clock=Clock(), ready_timeout_seconds=60)
        gate_module.install_gate(gate)
        driven: list[str] = []

        async def dispatch(correlation_id: str) -> None:
            driven.append(correlation_id)

        try:
            assert await gate.boot() == [SANDBOX]
            recovered = await sweep_interrupted_planning_runs(
                tmp_path / "forge.db", dispatch_callable=dispatch
            )
            assert driven == [f"pr-RUNNING-{OTHER_REPO}"]  # the unrelated one goes now
            assert recovered == driven
            # The starter comes back: the deferred run goes once the sandbox is ready.
            starter.reachable = True
            await gate.poll_once()
            slot = gate._slots[SANDBOX]
            await asyncio.wait_for(asyncio.shield(slot.bring_up), timeout=10)
            for _ in range(10):
                await asyncio.sleep(0)
            assert driven == [f"pr-RUNNING-{OTHER_REPO}", f"pr-RUNNING-{REPO}"]
        finally:
            gate_module.install_gate(None)
            ledger.close()

    @pytest.mark.asyncio
    async def test_a_build_launch_on_a_sandbox_that_never_comes_is_refused(
        self, tmp_path: Path
    ) -> None:
        from forge.cli._serve_production import _RepoRoutedAsyncTaskStarter

        ledger = a_ledger(tmp_path)
        starter = FakeStarter(running=False)
        starter.start_refusal = "the sandbox client is not signed in"
        gate_module.install_gate(a_gate(ledger, starter, clock=Clock(), ready_timeout_seconds=60))
        launched: list[str] = []

        class Runner:
            async def astart_async_task(self, name: str, context: Any) -> str:
                launched.append(name)
                return "task-1"

        router = _RepoRoutedAsyncTaskStarter(
            default=Runner(), by_repo={REPO: Runner()}, forge_config=a_config()
        )
        try:
            with pytest.raises(RuntimeError, match="not signed in"):
                await router.astart_async_task("autobuild", {"repo": REPO})
        finally:
            gate_module.install_gate(None)
            ledger.close()
        assert launched == []

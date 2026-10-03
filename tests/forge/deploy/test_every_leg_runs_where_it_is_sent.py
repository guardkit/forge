"""Every leg of the deploy stage runs the project's steps where it is sent.

3 October 2026. A repository with a sandbox is deployed out of the sandbox's
own clone, and nothing keeps that clone's working copy up to date. The
candidate check already ran from a tree laid out at the commit being checked;
every other leg ran the project's scripts out of the working copy, which on
that day held a deploy script from July. It did not know the read-only "what
are you running" question, took it for a plain deploy, and brought the live
app down on September's code.

So the merge press now hands the "what is running" question, the promote and
the candidate teardown a tree of the commit each is about, and the stage runs
EVERY project step of that leg there: the deploy, its health checks, the
candidate teardown after the promote, the live gate's driver and, when the
gate fails, the revert. A candidate the check itself takes down comes down by
the script that stood it up — in a sandbox. Every repository without one, and
every leg sent nowhere in particular, runs exactly where it always did.

What is driven: the REAL dispatcher and the real stage, onto a stand-in helper
that records every request and answers it. Nothing real is contacted.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from forge.config.models import DeployStageConfig
from forge.deploy.composition import dispatch_deploy_stage
from forge.deploy.live_gate import DryRunBrokerInspector, LiveGateInvocation
from forge.deploy.profile import parse_deploy_profile
from forge.persistence.repositories.runbook import RunbookRepository
from tests.forge.deploy.test_the_stage_stamps_every_request import (
    FIXED,
    OWNERSHIP,
    REPO,
    THE_BUILD,
    THE_RECORDED_COMMIT,
    WHICH_CANDIDATE,
    _ARecordingHelper,
    _QuietDeployPublisher,
)

#: Where the profile says its steps run: the clone's working copy.
THE_WORKING_COPY = "/somewhere/widget-shop"


@pytest.fixture
def helper() -> Any:
    stand_in = _ARecordingHelper()
    try:
        yield stand_in
    finally:
        stand_in.close()


@pytest.fixture
def repository(tmp_path: Path) -> RunbookRepository:
    from forge.persistence.migrations.runbook import apply

    connection = sqlite3.connect(str(tmp_path / "deploy.db"))
    apply(connection)
    return RunbookRepository(connection=connection)


@pytest.fixture
def runbook_publisher() -> AsyncMock:
    publisher = AsyncMock()
    publisher.publish_runbook_started = AsyncMock()
    publisher.publish_step_started = AsyncMock()
    publisher.publish_step_result = AsyncMock()
    publisher.publish_runbook_complete = AsyncMock()
    publisher.publish_escalated = AsyncMock()
    return publisher


@pytest.fixture
def the_tree(tmp_path: Path) -> str:
    """The tree of the commit a leg is about, laid out where the press lays it."""
    return str(tmp_path / "widget-shop" / ".forge-candidates" / "FEAT-7F21")


class _AGateThatFails:
    """A live gate whose verdict is not "pass", so the promote has to revert.

    It records every directory it is moved into, which is where its driver
    would run.
    """

    def __init__(self, moved_to: list[str] | None = None) -> None:
        self.moved_to: list[str] = [] if moved_to is None else moved_to

    def with_repo_path(self, repo_path: Any) -> "_AGateThatFails":
        self.moved_to.append(str(repo_path))
        return _AGateThatFails(self.moved_to)

    def with_extra_env(self, _overlay: dict[str, str]) -> "_AGateThatFails":
        return self

    def invoke(
        self, *, feature: str, target: str, gates: tuple[str, ...] = ()
    ) -> LiveGateInvocation:
        return LiveGateInvocation(verdict="fail", run_id=f"{feature}-{target}")


def _profile() -> Any:
    return parse_deploy_profile(
        {
            "env_id": "widget-shop-local",
            "compose": {"file": "compose.yaml", "script": "deploy/deploy.sh"},
            "health_checks": [{"cmd": "qa/health.sh"}],
            "cwd": THE_WORKING_COPY,
            "candidate": {"env": {"CANDIDATE_PORT": "8902"}},
            "rollback_image_ref": "widget-shop:rollback",
        }
    )


async def _drive(
    *,
    leg: str,
    helper: Any,
    repository: RunbookRepository,
    runbook_publisher: AsyncMock,
    tmp_path: Path,
    candidate_cwd: str | None,
    in_a_sandbox: bool,
    gate: Any = None,
) -> Any:
    """One leg, through the REAL dispatcher, onto the stand-in helper."""
    return await dispatch_deploy_stage(
        DeployStageConfig(
            enabled=True,
            execution_surface="sidecar",
            sidecar_url=helper.url,
            run_live_gate=gate is not None,
        ),
        _profile(),
        correlation_id=f"corr-{leg}",
        deploy_run_id=f"run-{leg}",
        repository=repository,
        runbook_publisher=runbook_publisher,
        deploy_publisher=_QuietDeployPublisher(),
        live_gate_invoker=gate,
        broker_inspector=DryRunBrokerInspector(),
        deploy_record_root=str(tmp_path / "state"),
        dry_run=False,
        clock=lambda: FIXED,
        target_repo=REPO,
        target_repo_root=str(tmp_path / "widget-shop"),
        sandbox=SimpleNamespace(sidecar_url=helper.url) if in_a_sandbox else None,
        feature="FEAT-7F21",
        feat_id="FEAT-7F21",
        leg=leg,
        candidate_cwd=candidate_cwd,
        prior_events=("DeployQueued",) if leg == "promote" else (),
        deploy_ownership=dict(OWNERSHIP) if leg == "promote" else None,
        identity_env=dict(WHICH_CANDIDATE),
        ask_env={"RUNNING_IDENTITY": "1"},
        build_id=THE_BUILD,
        declared_at=THE_RECORDED_COMMIT,
    )


def _where_each_ran(helper: Any) -> list[tuple[str, str]]:
    """``(what was asked for, the working directory sent)`` for every request."""
    seen: list[tuple[str, str]] = []
    for request in helper.requests:
        env = dict(request.get("env") or {})
        what = next(
            (
                word
                for word in ("CANDIDATE_DOWN", "REVERT", "PROMOTE", "RUNNING_IDENTITY")
                if env.get(word)
            ),
            str(request.get("script") or ""),
        )
        seen.append((what, str(request.get("cwd") or "")))
    return seen


class TestALegSentToATreeRunsEveryStepThere:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("leg", "asked_for"),
        [
            ("what_is_running", ["RUNNING_IDENTITY"]),
            ("promote", ["PROMOTE", "qa/health.sh", "CANDIDATE_DOWN"]),
            ("candidate_down", ["CANDIDATE_DOWN"]),
        ],
    )
    async def test_every_request_of_the_leg_names_the_tree(
        self, leg, asked_for, helper, repository, runbook_publisher, tmp_path, the_tree
    ) -> None:
        result = await _drive(
            leg=leg,
            helper=helper,
            repository=repository,
            runbook_publisher=runbook_publisher,
            tmp_path=tmp_path,
            candidate_cwd=the_tree,
            in_a_sandbox=True,
        )

        assert result is not None and result.outcome == "complete", result
        assert _where_each_ran(helper) == [(what, the_tree) for what in asked_for]

    @pytest.mark.asyncio
    async def test_a_failed_gate_runs_in_the_tree_and_the_revert_does_too(
        self, helper, repository, runbook_publisher, tmp_path, the_tree
    ) -> None:
        """The revert undoes what the promote's own step just did, with the
        snapshot that step took — so it is that same step, from the same tree,
        that brings the previous result back. And the gate that judged the
        deploy is the deployed commit's own."""
        gate = _AGateThatFails()

        result = await _drive(
            leg="promote",
            helper=helper,
            repository=repository,
            runbook_publisher=runbook_publisher,
            tmp_path=tmp_path,
            candidate_cwd=the_tree,
            in_a_sandbox=True,
            gate=gate,
        )

        assert result is not None and result.outcome == "reverted", result
        assert gate.moved_to == [the_tree]
        assert _where_each_ran(helper) == [
            ("PROMOTE", the_tree),
            ("qa/health.sh", the_tree),
            ("CANDIDATE_DOWN", the_tree),
            ("REVERT", the_tree),
        ]
        # NOTHING WRITTEN AT THE COORDINATOR'S PATH FOR IT (3 October 2026).
        # For a repository in a sandbox that path holds nothing; the
        # demotion note that used to land in its qa/ folder is not written.
        coordinators_path = tmp_path / "widget-shop"
        assert not (coordinators_path / "qa").exists()
        assert not list(coordinators_path.rglob("demotion-*.yaml"))
        assert not list(coordinators_path.rglob("deploy-record-*.md"))


class TestALegSentNowhereRunsWhereItAlwaysDid:
    """No tree named: every step runs in the profile's own working directory,
    exactly as before — which is every repository without a sandbox."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("leg", "asked_for"),
        [
            ("what_is_running", ["RUNNING_IDENTITY"]),
            ("promote", ["PROMOTE", "qa/health.sh", "CANDIDATE_DOWN"]),
            ("candidate_down", ["CANDIDATE_DOWN"]),
        ],
    )
    @pytest.mark.parametrize("in_a_sandbox", [False, True])
    async def test_every_request_names_the_profiles_own_directory(
        self, leg, asked_for, in_a_sandbox, helper, repository, runbook_publisher, tmp_path
    ) -> None:
        result = await _drive(
            leg=leg,
            helper=helper,
            repository=repository,
            runbook_publisher=runbook_publisher,
            tmp_path=tmp_path,
            candidate_cwd=None,
            in_a_sandbox=in_a_sandbox,
        )

        assert result is not None and result.outcome == "complete", result
        assert _where_each_ran(helper) == [
            (what, THE_WORKING_COPY) for what in asked_for
        ]

    @pytest.mark.asyncio
    async def test_the_revert_and_the_gate_stay_where_they_were(
        self, helper, repository, runbook_publisher, tmp_path
    ) -> None:
        gate = _AGateThatFails()

        result = await _drive(
            leg="promote",
            helper=helper,
            repository=repository,
            runbook_publisher=runbook_publisher,
            tmp_path=tmp_path,
            candidate_cwd=None,
            in_a_sandbox=False,
            gate=gate,
        )

        assert result is not None and result.outcome == "reverted", result
        assert gate.moved_to == []
        assert [cwd for _, cwd in _where_each_ran(helper)] == [THE_WORKING_COPY] * 4


class TestTheCandidateComesDownByTheScriptThatStoodItUp:
    """A candidate whose own check failed is taken down inside the check."""

    @pytest.mark.asyncio
    async def test_in_a_sandbox_the_teardown_runs_in_the_candidates_tree(
        self, helper, repository, runbook_publisher, tmp_path, the_tree
    ) -> None:
        result = await _drive(
            leg="candidate_check",
            helper=helper,
            repository=repository,
            runbook_publisher=runbook_publisher,
            tmp_path=tmp_path,
            candidate_cwd=the_tree,
            in_a_sandbox=True,
            gate=_AGateThatFails(),
        )

        assert result is not None and result.outcome == "failed", result
        assert _where_each_ran(helper) == [
            ("deploy/deploy.sh", the_tree),
            ("qa/health.sh", the_tree),
            ("CANDIDATE_DOWN", the_tree),
        ]

    @pytest.mark.asyncio
    async def test_without_a_sandbox_the_teardown_is_exactly_what_it_was(
        self, helper, repository, runbook_publisher, tmp_path, the_tree
    ) -> None:
        result = await _drive(
            leg="candidate_check",
            helper=helper,
            repository=repository,
            runbook_publisher=runbook_publisher,
            tmp_path=tmp_path,
            candidate_cwd=the_tree,
            in_a_sandbox=False,
            gate=_AGateThatFails(),
        )

        assert result is not None and result.outcome == "failed", result
        assert _where_each_ran(helper) == [
            ("deploy/deploy.sh", the_tree),
            ("qa/health.sh", the_tree),
            ("CANDIDATE_DOWN", THE_WORKING_COPY),
        ]

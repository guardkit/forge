"""The deploy stage as two callable legs (protect-main, Part J, rule 39).

* ``candidate_check`` — the candidate up, healthy, through the live gate; on
  a pass it is LEFT STANDING and the result says how many checks ran and
  passed; on a fail it is torn down. It runs every candidate step in the
  working directory it is given (the feature branch's laid-out tree, rule
  38), and the promote never does.
* ``promote`` — ``PROMOTE=1`` (never a rebuild), the candidate torn down,
  the live gate on the live name, the O-32 revert if that fails.
* ``candidate_down`` — the teardown on its own.
* ``run_deploy`` — the two in a row: exactly the events, runbooks and result
  of the one-call shape it always had.
* ``dispatch_deploy_stage(..., leg=...)`` routes to each.

Everything runs through the real runner, the real runbook executor and a
real SQLite runbook repository; the live-gate invoker is the one fake, at
the seam the stage already has.
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
from forge.deploy.composition import dispatch_deploy_stage
from forge.deploy.live_gate import (
    DryRunBrokerInspector,
    DryRunLiveGateInvoker,
    LiveGateInvocation,
)
from forge.deploy.profile import parse_deploy_profile
from forge.deploy.reservation import InProcessReservationLease
from forge.deploy.runbook_builder import build_deploy_runbook
from forge.deploy.stage import DeployStageRunner, gate_summary
from forge.persistence.repositories.runbook import RunbookRepository

FIXED = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
CANDIDATE_TREE = "/home/x/api_test/.forge-candidates/FEAT-9A01"


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


def _assertion(gate_id: str, status: str) -> dict[str, Any]:
    return {"id": f"{gate_id}::status", "gate_id": gate_id, "status": status}


#: Eight checks, two of them red — the shape the real driver reports.
EIGHT_GATES = ("health", "users_count", "etag", "a", "b", "c", "d", "e")
TWO_RED = tuple(
    _assertion(g, "fail" if g in ("users_count", "etag") else "pass") for g in EIGHT_GATES
)
ALL_GREEN = tuple(_assertion(g, "pass") for g in EIGHT_GATES)


class _Invoker:
    """A live-gate invoker: ``candidate`` shapes the candidate leg (reached
    through ``with_extra_env``), ``live`` the promote leg's bare invoke.

    It records every ``with_repo_path`` and ``with_extra_env`` call and, for
    every invoke, the working directory and overlay the invoking copy
    carried — so a test can say where each leg's gate ran.
    """

    def __init__(
        self,
        *,
        candidate: tuple[str, tuple[dict[str, Any], ...]] = ("pass", ALL_GREEN),
        live: tuple[str, tuple[dict[str, Any], ...]] = ("pass", ALL_GREEN),
    ) -> None:
        self._candidate = candidate
        self._live = live
        self.with_extra_env_calls: list[dict[str, str]] = []
        self.with_repo_path_calls: list[str] = []
        self.invocations: list[dict[str, Any]] = []

    @staticmethod
    def _invocation(shape, feature: str) -> LiveGateInvocation:
        verdict, assertions = shape
        return LiveGateInvocation(
            verdict=verdict,
            run_id=f"run-{feature}",
            gate_ids=EIGHT_GATES,
            assertions=assertions,
            evidence_index_ref="ev/idx.json",
            dry_run=False,
        )

    def invoke(self, *, feature: str, target: str, gates: tuple[str, ...] = ()):
        self.invocations.append({"feature": feature, "cwd": None, "overlay": {}})
        return self._invocation(self._live, feature)

    def with_repo_path(self, repo_path) -> "_Invoker._Copy":
        self.with_repo_path_calls.append(str(repo_path))
        return _Invoker._Copy(self, cwd=str(repo_path), overlay={})

    def with_extra_env(self, overlay: dict[str, str]) -> "_Invoker._Copy":
        self.with_extra_env_calls.append(dict(overlay))
        return _Invoker._Copy(self, cwd=None, overlay=dict(overlay))

    class _Copy:
        """A moved and/or overlaid copy: any invoke on it is the candidate leg."""

        def __init__(self, parent: "_Invoker", *, cwd: str | None, overlay: dict[str, str]) -> None:
            self._parent = parent
            self._cwd = cwd
            self._overlay = overlay

        def with_repo_path(self, repo_path) -> "_Invoker._Copy":
            self._parent.with_repo_path_calls.append(str(repo_path))
            return _Invoker._Copy(self._parent, cwd=str(repo_path), overlay=self._overlay)

        def with_extra_env(self, overlay: dict[str, str]) -> "_Invoker._Copy":
            self._parent.with_extra_env_calls.append(dict(overlay))
            return _Invoker._Copy(
                self._parent, cwd=self._cwd, overlay={**self._overlay, **overlay}
            )

        def invoke(self, *, feature: str, target: str, gates: tuple[str, ...] = ()):
            self._parent.invocations.append(
                {"feature": feature, "cwd": self._cwd, "overlay": dict(self._overlay)}
            )
            return self._parent._invocation(self._parent._candidate, "cand")


class _UnmovableInvoker:
    """An invoker with an env overlay seam but no working-directory seam —
    the shape every fake had before the candidate gate moved into the tree."""

    def __init__(self) -> None:
        self.invocations = 0

    def invoke(self, *, feature: str, target: str, gates: tuple[str, ...] = ()):
        self.invocations += 1
        return _Invoker._invocation(("pass", ALL_GREEN), feature)

    def with_extra_env(self, overlay: dict[str, str]) -> "_UnmovableInvoker":
        return self


def _runner(
    repository: RunbookRepository,
    runbook_publisher: AsyncMock,
    deploy_publisher: Any,
    tmp_path: Path,
    *,
    live_gate_invoker: Any = None,
    config: DeployStageConfig | None = None,
) -> DeployStageRunner:
    return DeployStageRunner(
        repository=repository,
        runbook_publisher=runbook_publisher,
        deploy_publisher=deploy_publisher,
        reservation=InProcessReservationLease(),
        live_gate_invoker=live_gate_invoker or DryRunLiveGateInvoker(),
        broker_inspector=DryRunBrokerInspector(),
        config=config or DeployStageConfig(),
        deploy_record_root=str(tmp_path / "state"),
        dry_run=True,
        clock=lambda: FIXED,
    )


def _profile(*, candidate: bool = True, keep: bool = False, rollback: str | None = None):
    raw: dict[str, Any] = {
        "env_id": "apitest-f2",
        "compose": {"file": "docker-compose.yml", "script": "deploy/sandbox-deploy.sh"},
        "health_checks": [{"cmd": "deploy/healthcheck.sh"}],
        "cwd": "/home/x/api_test",
    }
    if candidate:
        raw["candidate"] = {
            "env": {"CANDIDATE_PORT": "8902", "API_TEST_BASE_URL": "http://localhost:8902"},
            "keep": keep,
        }
    if rollback is not None:
        raw["rollback_image_ref"] = rollback
    return parse_deploy_profile(raw)


def _load(repository: RunbookRepository, runbook_id: str, corr: str):
    return repository.load_runbook(runbook_id, correlation_id=corr)


def _step_params(rb, step_type: str) -> dict[str, Any]:
    for step in rb.steps:
        if step.step_type == step_type:
            return dict(step.params)
    raise AssertionError(f"no {step_type} step in {rb.runbook_id}")


# ---------------------------------------------------------------------------
# The working directory override reaches only the candidate's steps
# ---------------------------------------------------------------------------


class TestTheCandidateRunsFromItsOwnTree:
    def test_the_builder_puts_the_override_on_every_step(self) -> None:
        profile = _profile()
        rb = build_deploy_runbook(
            profile,
            runbook_id="deploy-cand-x",
            target=profile.env_id,
            now=FIXED,
            compose_extra_env={"CANDIDATE": "1"},
            cwd_override=CANDIDATE_TREE,
        )
        assert _step_params(rb, "deploy_compose")["cwd"] == CANDIDATE_TREE
        assert _step_params(rb, "health_check")["cwd"] == CANDIDATE_TREE
        # The script stays the profile's name: found relative to the tree,
        # it is the tree's own copy that runs.
        assert _step_params(rb, "deploy_compose")["script"] == "deploy/sandbox-deploy.sh"

    def test_without_the_override_the_profile_cwd_stands(self) -> None:
        profile = _profile()
        rb = build_deploy_runbook(
            profile, runbook_id="deploy-x", target=profile.env_id, now=FIXED
        )
        assert _step_params(rb, "deploy_compose")["cwd"] == "/home/x/api_test"
        assert _step_params(rb, "health_check")["cwd"] == "/home/x/api_test"

    @pytest.mark.asyncio
    async def test_the_candidate_leg_uses_it_and_the_promote_does_not(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        deploy_pub = RecordingDeployPublisher()
        profile = _profile()
        runner = _runner(
            repository, runbook_publisher, deploy_pub, tmp_path, live_gate_invoker=_Invoker()
        )
        checked = await runner.candidate_check(
            profile,
            correlation_id="c1",
            deploy_run_id="run-1",
            feature="FEAT-9A01",
            feat_id="FEAT-9A01",
            candidate_cwd=CANDIDATE_TREE,
        )
        assert checked.outcome == "complete"
        cand = _load(repository, "deploy-cand-run-1", "c1")
        assert _step_params(cand, "deploy_compose")["cwd"] == CANDIDATE_TREE
        assert _step_params(cand, "health_check")["cwd"] == CANDIDATE_TREE
        assert _step_params(cand, "deploy_compose")["extra_env"]["CANDIDATE"] == "1"
        assert checked.detail["gate_summary"]["candidate_cwd"] == CANDIDATE_TREE
        # The candidate's live gate ran in the tree as well — not in the checkout.
        invoker = runner._live_gate_invoker
        assert invoker.with_repo_path_calls == [CANDIDATE_TREE]
        assert invoker.invocations[-1]["cwd"] == CANDIDATE_TREE

        promoted = await runner.promote(
            profile,
            correlation_id="c1",
            deploy_run_id="run-1",
            feature="FEAT-9A01",
            feat_id="FEAT-9A01",
            prior_events=checked.events,
        )
        assert promoted.outcome == "complete"
        live = _load(repository, "deploy-run-1", "c1")
        assert _step_params(live, "deploy_compose")["cwd"] == "/home/x/api_test"
        assert _step_params(live, "deploy_compose")["extra_env"] == {"PROMOTE": "1"}
        teardown = _load(repository, "teardown-cand-run-1", "c1")
        assert _step_params(teardown, "deploy_compose")["cwd"] == "/home/x/api_test"
        # The promote's gate ran where the invoker was composed: the checkout.
        assert invoker.with_repo_path_calls == [CANDIDATE_TREE]  # no second move
        assert invoker.invocations[-1]["cwd"] is None


# ---------------------------------------------------------------------------
# The candidate's live gate runs in the candidate's tree (coach finding,
# 2026-09-07): the driver reads the tree's registry and twins, not main's.
# ---------------------------------------------------------------------------


class TestTheCandidateGateRunsInTheTree:
    @pytest.mark.asyncio
    async def test_the_gate_is_moved_into_the_tree_then_given_the_candidate_addresses(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        deploy_pub = RecordingDeployPublisher()
        invoker = _Invoker()
        runner = _runner(
            repository, runbook_publisher, deploy_pub, tmp_path, live_gate_invoker=invoker
        )
        result = await runner.candidate_check(
            _profile(),
            correlation_id="ct",
            deploy_run_id="run-t",
            feature="FEAT-T1",
            candidate_cwd=CANDIDATE_TREE,
        )
        assert result.outcome == "complete"
        # Moved first, overlaid second: the one gate invocation carries both.
        assert invoker.with_repo_path_calls == [CANDIDATE_TREE]
        assert invoker.with_extra_env_calls == [
            {"CANDIDATE_PORT": "8902", "API_TEST_BASE_URL": "http://localhost:8902"}
        ]
        assert invoker.invocations == [
            {
                "feature": "FEAT-T1",
                "cwd": CANDIDATE_TREE,
                "overlay": {
                    "CANDIDATE_PORT": "8902",
                    "API_TEST_BASE_URL": "http://localhost:8902",
                },
            }
        ]
        # The evidence ref leaves the tree with the summary, as the driver said it.
        summary = result.detail["gate_summary"]
        assert summary["evidence_index_ref"] == "ev/idx.json"
        assert summary["candidate_cwd"] == CANDIDATE_TREE
        assert (summary["checks_total"], summary["checks_passed"]) == (8, 8)

    @pytest.mark.asyncio
    async def test_a_plain_deploy_and_a_promote_never_move_the_gate(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        deploy_pub = RecordingDeployPublisher()
        invoker = _Invoker()
        runner = _runner(
            repository, runbook_publisher, deploy_pub, tmp_path, live_gate_invoker=invoker
        )
        result = await runner.run_deploy(
            _profile(), correlation_id="pd", deploy_run_id="run-pd", feature="FEAT-T2"
        )
        assert result.outcome == "complete"
        assert invoker.with_repo_path_calls == []
        # Two gates ran — the candidate's (overlaid) and the live one — both
        # in the checkout, since no tree was given.
        assert [i["cwd"] for i in invoker.invocations] == [None, None]
        assert invoker.invocations[0]["overlay"]["CANDIDATE_PORT"] == "8902"
        assert invoker.invocations[1]["overlay"] == {}

        direct = await runner.run_deploy(
            _profile(candidate=False), correlation_id="pd2", deploy_run_id="run-pd2", feature="FEAT-T3"
        )
        assert direct.outcome == "complete"
        assert invoker.with_repo_path_calls == []

    @pytest.mark.asyncio
    async def test_the_dispatcher_hands_the_tree_to_the_gate(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        invoker = _Invoker()
        result = await dispatch_deploy_stage(
            DeployStageConfig(enabled=True),
            _profile(),
            correlation_id="dt",
            deploy_run_id="run-dt",
            repository=repository,
            runbook_publisher=runbook_publisher,
            deploy_publisher=RecordingDeployPublisher(),
            live_gate_invoker=invoker,
            deploy_record_root=str(tmp_path / "state"),
            dry_run=True,
            clock=lambda: FIXED,
            feature="FEAT-T4",
            leg="candidate_check",
            candidate_cwd=CANDIDATE_TREE,
        )
        assert result is not None and result.outcome == "complete"
        assert invoker.with_repo_path_calls == [CANDIDATE_TREE]
        assert invoker.invocations[-1]["cwd"] == CANDIDATE_TREE

    @pytest.mark.asyncio
    async def test_a_gate_that_cannot_move_into_the_tree_is_refused_not_run_in_the_checkout(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        deploy_pub = RecordingDeployPublisher()
        invoker = _UnmovableInvoker()
        runner = _runner(
            repository, runbook_publisher, deploy_pub, tmp_path, live_gate_invoker=invoker
        )
        result = await runner.candidate_check(
            _profile(),
            correlation_id="um",
            deploy_run_id="run-um",
            feature="FEAT-T5",
            candidate_cwd=CANDIDATE_TREE,
        )
        # Not run at all: a gate in the checkout would check main's registry.
        assert invoker.invocations == 0
        assert result.outcome == "failed"
        assert result.failed_step == "candidate_gate"
        assert result.verdict == "instrument_fail"
        assert result.detail["gate_summary"]["verdict"] is None
        # The reason is on the gate step's own record, and the candidate is down.
        gate_rb = _load(repository, "live-gate-cand-run-um", "um")
        step_result = gate_rb.steps[0].result.payload
        assert "cannot run in the candidate tree" in step_result["error"]
        assert CANDIDATE_TREE in step_result["error"]
        assert _load(repository, "teardown-cand-run-um", "um") is not None
        assert _load(repository, "deploy-run-um", "um") is None
        assert [n for n, _ in deploy_pub.events] == ["DeployQueued", "DeployFailed"]

    @pytest.mark.asyncio
    async def test_the_same_invoker_still_serves_a_run_with_no_tree(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        """What every existing caller had: no tree given, the gate runs where
        the invoker was composed, the env overlay alone is best-effort."""
        deploy_pub = RecordingDeployPublisher()
        invoker = _UnmovableInvoker()
        runner = _runner(
            repository, runbook_publisher, deploy_pub, tmp_path, live_gate_invoker=invoker
        )
        result = await runner.candidate_check(
            _profile(), correlation_id="nt", deploy_run_id="run-nt", feature="FEAT-T6"
        )
        assert result.outcome == "complete"
        assert invoker.invocations == 1


# ---------------------------------------------------------------------------
# candidate_check on its own
# ---------------------------------------------------------------------------


class TestCandidateCheck:
    @pytest.mark.asyncio
    async def test_a_pass_leaves_the_candidate_standing_and_counts_the_checks(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        deploy_pub = RecordingDeployPublisher()
        invoker = _Invoker()
        runner = _runner(
            repository, runbook_publisher, deploy_pub, tmp_path, live_gate_invoker=invoker
        )
        result = await runner.candidate_check(
            _profile(), correlation_id="cp", deploy_run_id="run-p", feature="FEAT-1"
        )
        assert result.outcome == "complete"
        assert result.verdict == "pass"
        assert result.events == ("DeployQueued",)
        assert result.detail["candidate"] == "standing"
        summary = result.detail["gate_summary"]
        assert summary["verdict"] == "pass"
        assert summary["checks_total"] == 8
        assert summary["checks_passed"] == 8
        assert summary["failed_checks"] == []
        assert summary["live_gate_runbook_id"] == "live-gate-cand-run-p"
        # Up + gated, NOT torn down, NOT promoted.
        assert _load(repository, "deploy-cand-run-p", "cp") is not None
        assert _load(repository, "live-gate-cand-run-p", "cp") is not None
        assert _load(repository, "teardown-cand-run-p", "cp") is None
        assert _load(repository, "deploy-run-p", "cp") is None
        # The candidate gate addressed the candidate instance.
        assert invoker.with_extra_env_calls == [
            {"CANDIDATE_PORT": "8902", "API_TEST_BASE_URL": "http://localhost:8902"}
        ]
        # No deploy-domain verdict yet: those are reserved for the live leg.
        assert [n for n, _ in deploy_pub.events] == ["DeployQueued"]

    @pytest.mark.asyncio
    async def test_a_fail_names_the_failing_checks_and_tears_down(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        deploy_pub = RecordingDeployPublisher()
        runner = _runner(
            repository,
            runbook_publisher,
            deploy_pub,
            tmp_path,
            live_gate_invoker=_Invoker(candidate=("fail", TWO_RED)),
        )
        result = await runner.candidate_check(
            _profile(rollback="apitest:rollback"),
            correlation_id="cf",
            deploy_run_id="run-f",
            feature="FEAT-2",
        )
        assert result.outcome == "failed"
        assert result.failed_step == "candidate_gate"
        assert result.detail["reason"] == "candidate_failed"
        summary = result.detail["gate_summary"]
        assert summary["verdict"] == "fail"
        assert summary["checks_total"] == 8
        assert summary["checks_passed"] == 6
        assert summary["failed_checks"] == ["users_count", "etag"]
        # Torn down; nothing live, no revert.
        assert _load(repository, "teardown-cand-run-f", "cf") is not None
        assert _load(repository, "deploy-run-f", "cf") is None
        assert _load(repository, "revert-run-f", "cf") is None
        assert [n for n, _ in deploy_pub.events] == ["DeployQueued", "DeployFailed"]

    @pytest.mark.asyncio
    async def test_no_candidate_section_is_a_plain_failure(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        deploy_pub = RecordingDeployPublisher()
        runner = _runner(repository, runbook_publisher, deploy_pub, tmp_path)
        result = await runner.candidate_check(
            _profile(candidate=False), correlation_id="cn", deploy_run_id="run-n"
        )
        assert result.outcome == "failed"
        assert result.failed_step == "candidate"
        assert result.detail == {"reason": "no_candidate_section"}
        failed = [p for n, p in deploy_pub.events if n == "DeployFailed"][0]
        assert "no candidate section" in failed.failure_reason
        assert _load(repository, "deploy-cand-run-n", "cn") is None

    @pytest.mark.asyncio
    async def test_without_a_live_gate_health_is_the_check(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        deploy_pub = RecordingDeployPublisher()
        runner = _runner(
            repository,
            runbook_publisher,
            deploy_pub,
            tmp_path,
            config=DeployStageConfig(run_live_gate=False),
        )
        result = await runner.candidate_check(
            _profile(), correlation_id="cg", deploy_run_id="run-g"
        )
        assert result.outcome == "complete"
        assert result.detail["gate_summary"]["verdict"] == "pass"
        assert result.detail["gate_summary"]["checks_total"] is None
        assert _load(repository, "live-gate-cand-run-g", "cg") is None


# ---------------------------------------------------------------------------
# promote and candidate_down on their own
# ---------------------------------------------------------------------------


class TestPromote:
    @pytest.mark.asyncio
    async def test_promote_retags_then_tears_down_then_gates_live(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        deploy_pub = RecordingDeployPublisher()
        runner = _runner(
            repository, runbook_publisher, deploy_pub, tmp_path, live_gate_invoker=_Invoker()
        )
        result = await runner.promote(
            _profile(),
            correlation_id="pp",
            deploy_run_id="run-pp",
            feature="FEAT-3",
            prior_events=("DeployQueued",),
        )
        assert result.outcome == "complete"
        assert result.detail["candidate"] == "torn-down"
        # Queued once (by the candidate leg) — not again here.
        assert result.events == (
            "DeployQueued", "DeployStarted", "DeployComplete", "QAVerdict", "LiveGateResult",
        )
        assert [n for n, _ in deploy_pub.events] == [
            "DeployStarted", "DeployComplete", "QAVerdict", "LiveGateResult",
        ]
        live = _load(repository, "deploy-run-pp", "pp")
        assert _step_params(live, "deploy_compose")["extra_env"] == {"PROMOTE": "1"}
        teardown = _load(repository, "teardown-cand-run-pp", "pp")
        assert _step_params(teardown, "deploy_compose")["extra_env"]["CANDIDATE_DOWN"] == "1"
        assert _load(repository, "live-gate-run-pp", "pp") is not None

    @pytest.mark.asyncio
    async def test_promote_alone_queues_once(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        deploy_pub = RecordingDeployPublisher()
        runner = _runner(
            repository, runbook_publisher, deploy_pub, tmp_path, live_gate_invoker=_Invoker()
        )
        result = await runner.promote(
            _profile(), correlation_id="pq", deploy_run_id="run-pq", feature="FEAT-4"
        )
        assert result.events[0] == "DeployQueued"
        assert result.events.count("DeployQueued") == 1

    @pytest.mark.asyncio
    async def test_keep_leaves_the_candidate_up_and_says_so(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        deploy_pub = RecordingDeployPublisher()
        runner = _runner(
            repository, runbook_publisher, deploy_pub, tmp_path, live_gate_invoker=_Invoker()
        )
        result = await runner.promote(
            _profile(keep=True), correlation_id="pk", deploy_run_id="run-pk", feature="FEAT-5"
        )
        assert result.outcome == "complete"
        assert result.detail["candidate"] == "kept"
        assert _load(repository, "teardown-cand-run-pk", "pk") is None

    @pytest.mark.asyncio
    async def test_a_red_live_gate_after_the_promote_still_reverts(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        deploy_pub = RecordingDeployPublisher()
        runner = _runner(
            repository,
            runbook_publisher,
            deploy_pub,
            tmp_path,
            live_gate_invoker=_Invoker(live=("fail", TWO_RED)),
        )
        result = await runner.promote(
            _profile(rollback="apitest:rollback"),
            correlation_id="pr",
            deploy_run_id="run-pr",
            feature="FEAT-6",
            prior_events=("DeployQueued",),
        )
        assert result.outcome == "reverted"
        assert result.detail["candidate"] == "torn-down"
        assert _load(repository, "revert-run-pr", "pr") is not None

    @pytest.mark.asyncio
    async def test_candidate_down_on_its_own(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        deploy_pub = RecordingDeployPublisher()
        runner = _runner(repository, runbook_publisher, deploy_pub, tmp_path)
        result = await runner.candidate_down(
            _profile(), correlation_id="cd", deploy_run_id="run-cd"
        )
        assert result.outcome == "complete"
        assert result.detail == {"candidate": "torn-down"}
        teardown = _load(repository, "teardown-cand-run-cd", "cd")
        assert _step_params(teardown, "deploy_compose")["extra_env"]["CANDIDATE_DOWN"] == "1"
        absent = await runner.candidate_down(
            _profile(candidate=False), correlation_id="cd2", deploy_run_id="run-cd2"
        )
        assert absent.outcome == "complete"
        assert absent.detail["candidate"] == "absent"


# ---------------------------------------------------------------------------
# run_deploy is still the one-call shape it always was
# ---------------------------------------------------------------------------


class TestRunDeployIsTheTwoLegsInARow:
    @pytest.mark.asyncio
    async def test_the_sequence_is_unchanged(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        deploy_pub = RecordingDeployPublisher()
        runner = _runner(
            repository, runbook_publisher, deploy_pub, tmp_path, live_gate_invoker=_Invoker()
        )
        result = await runner.run_deploy(
            _profile(), correlation_id="rd", deploy_run_id="run-rd", feature="FEAT-7"
        )
        assert result.outcome == "complete"
        assert result.events == (
            "DeployQueued", "DeployStarted", "DeployComplete", "QAVerdict", "LiveGateResult",
        )
        assert [n for n, _ in deploy_pub.events] == list(result.events)
        for runbook_id in (
            "deploy-cand-run-rd", "live-gate-cand-run-rd", "deploy-run-rd",
            "teardown-cand-run-rd", "live-gate-run-rd",
        ):
            assert _load(repository, runbook_id, "rd") is not None, runbook_id

    @pytest.mark.asyncio
    async def test_a_red_candidate_ends_the_run_with_live_untouched(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        deploy_pub = RecordingDeployPublisher()
        runner = _runner(
            repository,
            runbook_publisher,
            deploy_pub,
            tmp_path,
            live_gate_invoker=_Invoker(candidate=("fail", TWO_RED)),
        )
        result = await runner.run_deploy(
            _profile(rollback="apitest:rollback"),
            correlation_id="rf",
            deploy_run_id="run-rf",
            feature="FEAT-8",
        )
        assert result.outcome == "failed"
        assert result.failed_step == "candidate_gate"
        assert result.detail["gate_summary"]["failed_checks"] == ["users_count", "etag"]
        assert [n for n, _ in deploy_pub.events] == ["DeployQueued", "DeployFailed"]
        assert _load(repository, "deploy-run-rf", "rf") is None

    @pytest.mark.asyncio
    async def test_without_a_candidate_section_it_is_the_direct_live_flow(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        deploy_pub = RecordingDeployPublisher()
        runner = _runner(
            repository, runbook_publisher, deploy_pub, tmp_path, live_gate_invoker=_Invoker()
        )
        result = await runner.run_deploy(
            _profile(candidate=False), correlation_id="dl", deploy_run_id="run-dl", feature="FEAT-9"
        )
        assert result.outcome == "complete"
        assert result.events == (
            "DeployQueued", "DeployStarted", "DeployComplete", "QAVerdict", "LiveGateResult",
        )
        assert result.detail["candidate"] == "absent"
        assert "extra_env" not in _step_params(_load(repository, "deploy-run-dl", "dl"), "deploy_compose")


# ---------------------------------------------------------------------------
# The dispatcher routes each leg
# ---------------------------------------------------------------------------


class TestDispatchByLeg:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("leg", "runbook_present", "runbook_absent"),
        [
            ("candidate_check", "deploy-cand-run-x", "deploy-run-x"),
            ("promote", "deploy-run-x", "deploy-cand-run-x"),
            ("candidate_down", "teardown-cand-run-x", "deploy-run-x"),
            ("deploy", "deploy-run-x", "nothing-x"),
        ],
    )
    async def test_each_leg_reaches_its_runner_method(
        self, repository, runbook_publisher, tmp_path, leg, runbook_present, runbook_absent
    ) -> None:
        deploy_pub = RecordingDeployPublisher()
        result = await dispatch_deploy_stage(
            DeployStageConfig(enabled=True),
            _profile(),
            correlation_id="x",
            deploy_run_id="run-x",
            repository=repository,
            runbook_publisher=runbook_publisher,
            deploy_publisher=deploy_pub,
            live_gate_invoker=_Invoker(),
            deploy_record_root=str(tmp_path / "state"),
            dry_run=True,
            clock=lambda: FIXED,
            feature="FEAT-X",
            leg=leg,
            candidate_cwd=CANDIDATE_TREE if leg == "candidate_check" else None,
            prior_events=("DeployQueued",) if leg == "promote" else (),
        )
        assert result is not None and result.outcome == "complete"
        assert _load(repository, runbook_present, "x") is not None
        assert _load(repository, runbook_absent, "x") is None
        if leg == "candidate_check":
            cand = _load(repository, "deploy-cand-run-x", "x")
            assert _step_params(cand, "deploy_compose")["cwd"] == CANDIDATE_TREE

    @pytest.mark.asyncio
    async def test_the_flag_off_answers_none_for_every_leg(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        for leg in ("candidate_check", "promote", "candidate_down", "deploy"):
            result = await dispatch_deploy_stage(
                DeployStageConfig(enabled=False),
                _profile(),
                correlation_id="off",
                deploy_run_id="run-off",
                repository=repository,
                runbook_publisher=runbook_publisher,
                deploy_publisher=RecordingDeployPublisher(),
                leg=leg,
            )
            assert result is None

    @pytest.mark.asyncio
    async def test_an_unknown_leg_is_refused(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        with pytest.raises(ValueError, match="unknown deploy leg"):
            await dispatch_deploy_stage(
                DeployStageConfig(enabled=True),
                _profile(),
                correlation_id="bad",
                deploy_run_id="run-bad",
                repository=repository,
                runbook_publisher=runbook_publisher,
                deploy_publisher=RecordingDeployPublisher(),
                dry_run=True,
                leg="sideways",
            )


# ---------------------------------------------------------------------------
# The summary in numbers and names
# ---------------------------------------------------------------------------


class TestGateSummary:
    def test_counts_and_names_from_the_per_check_results(self) -> None:
        summary = gate_summary(verdict="fail", gate_ids=EIGHT_GATES, assertions=TWO_RED)
        assert summary["checks_total"] == 8
        assert summary["checks_passed"] == 6
        assert summary["failed_checks"] == ["users_count", "etag"]

    def test_a_pass_with_no_per_check_results_counts_every_check(self) -> None:
        summary = gate_summary(verdict="pass", gate_ids=("health", "etag"), assertions=())
        assert summary == {
            "verdict": "pass",
            "checks_total": 2,
            "checks_passed": 2,
            "failed_checks": [],
            "gate_ids": ["health", "etag"],
            "live_gate_runbook_id": None,
        }

    def test_a_fail_with_no_per_check_results_leaves_the_names_unknown(self) -> None:
        summary = gate_summary(verdict="fail", gate_ids=("health", "etag"), assertions=())
        assert summary["checks_total"] == 2
        assert summary["checks_passed"] is None
        assert summary["failed_checks"] is None

    def test_a_fail_whose_results_all_pass_is_not_guessed(self) -> None:
        summary = gate_summary(verdict="environment_fail", gate_ids=EIGHT_GATES, assertions=ALL_GREEN)
        assert summary["failed_checks"] is None
        assert summary["checks_passed"] is None

    def test_names_come_from_the_results_when_no_ids_were_given(self) -> None:
        summary = gate_summary(verdict="fail", gate_ids=(), assertions=TWO_RED)
        assert summary["checks_total"] == 8
        assert summary["failed_checks"] == ["users_count", "etag"]

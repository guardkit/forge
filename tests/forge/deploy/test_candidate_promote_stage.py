"""Tests for candidate-then-promote deploy sequencing (S2F).

The candidate leg (execution-surface design §3): when a profile carries a
``candidate`` section, the DEPLOY stage stands the build up under a separate
``<live>-cand`` compose project, gates it, and promotes it to the live name
ONLY on a PASS. A candidate that fails its gate is torn down and the LIVE name
is never touched — no DeployStarted, no revert.

Coverage:

* Profile parsing of the ``candidate`` section (env validation, keep default).
* Candidate-ABSENT = byte-identical to the direct-live flow (an explicit
  equivalence test alongside the unmodified legacy suites).
* Candidate happy path: candidate deploy → candidate gate PASS → promote
  (PROMOTE=1, no overlay) → the live gate + the deploy-domain event sequence,
  with env-overlay assertions on each candidate-leg step.
* Candidate gate FAIL → teardown + candidate_failed detail + zero promote/live
  steps + no revert (the LIVE name untouched).
* Promote-leg gate FAIL → the O-32 revert fires exactly as before.
* keep=true skips the post-promote candidate teardown.
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
from forge.deploy.live_gate import (
    DryRunBrokerInspector,
    DryRunLiveGateInvoker,
    LiveGateInvocation,
)
from forge.deploy.profile import (
    DeployCandidate,
    DeployProfileError,
    parse_deploy_profile,
)
from forge.deploy.reservation import InProcessReservationLease
from forge.deploy.runbook_builder import build_deploy_runbook
from forge.deploy.stage import DeployStageRunner
from forge.persistence.repositories.runbook import RunbookRepository

FIXED = datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures / fakes
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


def _invocation(verdict: str, feature: str, gates: tuple[str, ...]) -> LiveGateInvocation:
    return LiveGateInvocation(
        verdict=verdict,
        run_id=f"run-{feature}",
        gate_ids=tuple(gates),
        evidence_index_ref="ev/idx.json",
        dry_run=False,
        detail={},
    )


class _RecordingInvoker:
    """A verdict-fixed invoker that records every invoke + with_extra_env call.

    ``with_extra_env`` (the candidate-leg driver overlay seam) returns a child
    that stamps the overlay onto the recorded invocation, so a test can assert
    the candidate.env reached the candidate-leg live-gate driver env.
    """

    def __init__(self, verdict: str = "pass") -> None:
        self._verdict = verdict
        self.invocations: list[dict[str, Any]] = []
        self.with_extra_env_calls: list[dict[str, str]] = []

    def _record(
        self, overlay: dict[str, str], *, feature: str, target: str, gates
    ) -> LiveGateInvocation:
        self.invocations.append(
            {"feature": feature, "target": target, "gates": tuple(gates), "overlay": overlay}
        )
        return _invocation(self._verdict, feature, tuple(gates))

    def invoke(self, *, feature: str, target: str, gates: tuple[str, ...] = ()):
        return self._record({}, feature=feature, target=target, gates=gates)

    def with_extra_env(self, overlay: dict[str, str]) -> "_RecordingInvoker._Child":
        self.with_extra_env_calls.append(dict(overlay))
        return _RecordingInvoker._Child(self, dict(overlay))

    class _Child:
        def __init__(self, parent: "_RecordingInvoker", overlay: dict[str, str]) -> None:
            self._parent = parent
            self._overlay = overlay

        def invoke(self, *, feature: str, target: str, gates: tuple[str, ...] = ()):
            return self._parent._record(
                self._overlay, feature=feature, target=target, gates=gates
            )


class _SwitchableInvoker:
    """``candidate_verdict`` on the candidate leg (via with_extra_env), and
    ``base_verdict`` on the live leg (the bare invoke) — so a test can drive a
    candidate PASS followed by a promote-leg FAIL through the O-32 revert."""

    def __init__(self, *, base_verdict: str, candidate_verdict: str) -> None:
        self._base = base_verdict
        self._cand = candidate_verdict
        self.with_extra_env_calls: list[dict[str, str]] = []

    def invoke(self, *, feature: str, target: str, gates: tuple[str, ...] = ()):
        return _invocation(self._base, feature, tuple(gates))

    def with_extra_env(self, overlay: dict[str, str]) -> "_SwitchableInvoker._Child":
        self.with_extra_env_calls.append(dict(overlay))
        return _SwitchableInvoker._Child(self._cand)

    class _Child:
        def __init__(self, verdict: str) -> None:
            self._verdict = verdict

        def invoke(self, *, feature: str, target: str, gates: tuple[str, ...] = ()):
            return _invocation(self._verdict, feature, tuple(gates))


def _runner(
    repository: RunbookRepository,
    runbook_publisher: AsyncMock,
    deploy_publisher: Any,
    tmp_path: Path,
    *,
    dry_run: bool = True,
    config: DeployStageConfig | None = None,
    live_gate_invoker: Any = None,
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
        dry_run=dry_run,
        clock=lambda: FIXED,
    )


def _candidate_profile(
    *, rollback: str | None = None, keep: bool = False, candidate: bool = True
) -> Any:
    raw: dict[str, Any] = {
        "env_id": "apitest-f2",
        "compose": {"file": "docker-compose.yml", "script": "deploy.sh"},
        "health_checks": [{"cmd": "qa/health.sh"}],
    }
    if candidate:
        raw["candidate"] = {
            "env": {
                "CANDIDATE_PORT": "8902",
                "API_TEST_BASE_URL": "http://localhost:8902",
            },
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
# Profile parsing
# ---------------------------------------------------------------------------


class TestCandidateProfileParsing:
    def test_absent_candidate_is_none(self) -> None:
        p = parse_deploy_profile(
            {"env_id": "e", "compose": {"file": "dc.yml", "script": "d.sh"}}
        )
        assert p.candidate is None

    def test_candidate_env_and_keep_parsed(self) -> None:
        p = _candidate_profile(keep=True)
        assert isinstance(p.candidate, DeployCandidate)
        assert p.candidate.env["CANDIDATE_PORT"] == "8902"
        assert p.candidate.keep is True

    def test_candidate_keep_defaults_false(self) -> None:
        p = _candidate_profile()
        assert p.candidate is not None
        assert p.candidate.keep is False

    def test_candidate_env_rejects_non_upper_snake_key(self) -> None:
        with pytest.raises(DeployProfileError, match="UPPER_SNAKE"):
            parse_deploy_profile(
                {
                    "env_id": "e",
                    "compose": {"file": "dc.yml"},
                    "candidate": {"env": {"badKey": "x"}},
                }
            )

    def test_candidate_env_rejects_non_string_value(self) -> None:
        with pytest.raises(DeployProfileError, match="string value"):
            parse_deploy_profile(
                {
                    "env_id": "e",
                    "compose": {"file": "dc.yml"},
                    "candidate": {"env": {"CANDIDATE_PORT": 8902}},
                }
            )

    def test_candidate_keep_rejects_non_bool(self) -> None:
        with pytest.raises(DeployProfileError, match="candidate.keep"):
            parse_deploy_profile(
                {
                    "env_id": "e",
                    "compose": {"file": "dc.yml"},
                    "candidate": {"keep": "yes"},
                }
            )


# ---------------------------------------------------------------------------
# Candidate-absent = byte-identical (explicit equivalence)
# ---------------------------------------------------------------------------


class TestCandidateAbsentEquivalence:
    def test_no_candidate_deploy_runbook_carries_no_extra_env(self) -> None:
        # The direct-live flow's deploy_compose / health_check step params carry
        # NO extra_env key at all — byte-identical to before the seam existed.
        profile = _candidate_profile(candidate=False)
        rb = build_deploy_runbook(
            profile, runbook_id="deploy-x", target=profile.env_id, now=FIXED
        )
        assert "extra_env" not in _step_params(rb, "deploy_compose")
        assert "extra_env" not in _step_params(rb, "health_check")

    def test_default_build_equals_explicit_none_overlays(self) -> None:
        profile = _candidate_profile(candidate=False)
        a = build_deploy_runbook(
            profile, runbook_id="deploy-x", target=profile.env_id, now=FIXED
        )
        b = build_deploy_runbook(
            profile,
            runbook_id="deploy-x",
            target=profile.env_id,
            now=FIXED,
            compose_extra_env=None,
            check_extra_env=None,
        )
        assert [s.params for s in a.steps] == [s.params for s in b.steps]

    @pytest.mark.asyncio
    async def test_no_candidate_runs_direct_live_flow(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        deploy_pub = RecordingDeployPublisher()
        profile = _candidate_profile(candidate=False)
        runner = _runner(
            repository,
            runbook_publisher,
            deploy_pub,
            tmp_path,
            live_gate_invoker=_RecordingInvoker("pass"),
        )
        result = await runner.run_deploy(
            profile, correlation_id="c", deploy_run_id="run-abs", feature="FEAT-1"
        )
        assert result.outcome == "complete"
        assert result.events == (
            "DeployQueued",
            "DeployStarted",
            "DeployComplete",
            "QAVerdict",
            "LiveGateResult",
        )
        # No candidate-leg runbooks were ever created.
        assert _load(repository, "deploy-cand-run-abs", "c") is None
        assert _load(repository, "live-gate-cand-run-abs", "c") is None
        assert _load(repository, "teardown-cand-run-abs", "c") is None


# ---------------------------------------------------------------------------
# Candidate happy path → promote → live gate
# ---------------------------------------------------------------------------


class TestCandidateHappyPath:
    @pytest.mark.asyncio
    async def test_candidate_pass_promotes_and_gates_live(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        deploy_pub = RecordingDeployPublisher()
        profile = _candidate_profile()
        invoker = _RecordingInvoker("pass")
        runner = _runner(
            repository, runbook_publisher, deploy_pub, tmp_path, live_gate_invoker=invoker
        )
        result = await runner.run_deploy(
            profile,
            correlation_id="ch",
            deploy_run_id="run-ok",
            feature="FEAT-B70F",
            feat_id="FEAT-B70F",
        )

        assert result.outcome == "complete"
        assert result.verdict == "pass"
        # The deploy-domain sequence describes the ONE live deploy — the
        # candidate gate did NOT publish a second QAVerdict/LiveGateResult.
        assert result.events == (
            "DeployQueued",
            "DeployStarted",
            "DeployComplete",
            "QAVerdict",
            "LiveGateResult",
        )
        assert [n for n, _ in deploy_pub.events] == list(result.events)

        # Candidate deploy_compose carried CANDIDATE=1 + the candidate.env overlay.
        cand_deploy = _load(repository, "deploy-cand-run-ok", "ch")
        assert cand_deploy is not None
        cand_compose_env = _step_params(cand_deploy, "deploy_compose")["extra_env"]
        assert cand_compose_env["CANDIDATE"] == "1"
        assert cand_compose_env["CANDIDATE_PORT"] == "8902"
        assert cand_compose_env["API_TEST_BASE_URL"] == "http://localhost:8902"
        # Candidate health_check carried the candidate.env overlay (no CANDIDATE=1).
        cand_check_env = _step_params(cand_deploy, "health_check")["extra_env"]
        assert cand_check_env == {
            "CANDIDATE_PORT": "8902",
            "API_TEST_BASE_URL": "http://localhost:8902",
        }

        # The promote-leg deploy_compose carried PROMOTE=1 and NO overlay.
        live_deploy = _load(repository, "deploy-run-ok", "ch")
        assert live_deploy is not None
        promote_env = _step_params(live_deploy, "deploy_compose")["extra_env"]
        assert promote_env == {"PROMOTE": "1"}
        # The promote-leg health_check carried no overlay at all.
        assert "extra_env" not in _step_params(live_deploy, "health_check")

        # The candidate gate ran with the candidate.env overlaid on the driver env.
        assert invoker.with_extra_env_calls == [
            {"CANDIDATE_PORT": "8902", "API_TEST_BASE_URL": "http://localhost:8902"}
        ]
        cand_invocations = [i for i in invoker.invocations if i["overlay"]]
        assert cand_invocations and cand_invocations[0]["overlay"]["CANDIDATE_PORT"] == "8902"

        # Both live-gate runbooks exist (candidate + live), and the candidate one
        # is the -cand-suffixed id.
        assert _load(repository, "live-gate-cand-run-ok", "ch") is not None
        assert _load(repository, "live-gate-run-ok", "ch") is not None

        # keep defaults false → the candidate was torn down after promote.
        teardown = _load(repository, "teardown-cand-run-ok", "ch")
        assert teardown is not None
        teardown_env = _step_params(teardown, "deploy_compose")["extra_env"]
        assert teardown_env["CANDIDATE_DOWN"] == "1"

    @pytest.mark.asyncio
    async def test_keep_true_skips_teardown_after_promote(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        deploy_pub = RecordingDeployPublisher()
        profile = _candidate_profile(keep=True)
        runner = _runner(
            repository,
            runbook_publisher,
            deploy_pub,
            tmp_path,
            live_gate_invoker=_RecordingInvoker("pass"),
        )
        result = await runner.run_deploy(
            profile, correlation_id="ck", deploy_run_id="run-keep", feature="FEAT-2"
        )
        assert result.outcome == "complete"
        # Promote happened, but keep=true means NO teardown runbook.
        assert _load(repository, "deploy-run-keep", "ck") is not None
        assert _load(repository, "teardown-cand-run-keep", "ck") is None


# ---------------------------------------------------------------------------
# Candidate gate FAIL → teardown + candidate_failed + no promote/live/revert
# ---------------------------------------------------------------------------


class TestCandidateGateFail:
    @pytest.mark.asyncio
    async def test_candidate_fail_tears_down_and_never_touches_live(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        deploy_pub = RecordingDeployPublisher()
        # A rollback ref is present to prove NO revert fires on a candidate fail.
        profile = _candidate_profile(rollback="apitest:rollback-20260716")
        runner = _runner(
            repository,
            runbook_publisher,
            deploy_pub,
            tmp_path,
            live_gate_invoker=_RecordingInvoker("fail"),
        )
        result = await runner.run_deploy(
            profile,
            correlation_id="cf",
            deploy_run_id="run-fail",
            feature="FEAT-3AA",
            feat_id="FEAT-3AA",
        )

        assert result.outcome == "failed"
        assert result.failed_step == "candidate_gate"
        assert result.detail.get("reason") == "candidate_failed"
        assert result.verdict == "fail"

        # The LIVE deploy never started; only DeployQueued + DeployFailed fired.
        names = [n for n, _ in deploy_pub.events]
        assert names == ["DeployQueued", "DeployFailed"]
        assert "DeployStarted" not in names
        assert "DeployComplete" not in names
        assert "DeployReverted" not in names

        failed = [p for n, p in deploy_pub.events if n == "DeployFailed"][0]
        assert failed.failed_step == "candidate_gate"
        assert failed.recoverable is True
        assert "never touched" in failed.failure_reason

        # The candidate was torn down; the LIVE + revert runbooks never existed.
        assert _load(repository, "deploy-cand-run-fail", "cf") is not None
        assert _load(repository, "teardown-cand-run-fail", "cf") is not None
        assert _load(repository, "deploy-run-fail", "cf") is None
        assert _load(repository, "live-gate-run-fail", "cf") is None
        assert _load(repository, "revert-run-fail", "cf") is None


# ---------------------------------------------------------------------------
# Promote-leg gate FAIL → O-32 revert fires exactly as before
# ---------------------------------------------------------------------------


class TestPromoteLegRevert:
    @pytest.mark.asyncio
    async def test_candidate_pass_then_live_fail_reverts(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        deploy_pub = RecordingDeployPublisher()
        profile = _candidate_profile(rollback="apitest:rollback-20260716")
        # Candidate leg PASSES (via with_extra_env child); the live leg FAILS.
        invoker = _SwitchableInvoker(base_verdict="fail", candidate_verdict="pass")
        runner = _runner(
            repository, runbook_publisher, deploy_pub, tmp_path, live_gate_invoker=invoker
        )
        result = await runner.run_deploy(
            profile,
            correlation_id="cr",
            deploy_run_id="run-rev",
            feature="FEAT-4AA",
            feat_id="FEAT-4AA",
        )

        # The promote happened, the live gate failed, and O-32 reverted — exactly
        # as the direct-live flow does.
        assert result.outcome == "reverted"
        assert result.verdict == "fail"
        names = [n for n, _ in deploy_pub.events]
        assert names == [
            "DeployQueued",
            "DeployStarted",
            "DeployComplete",
            "QAVerdict",
            "LiveGateResult",
            "DeployReverted",
        ]
        reverted = [p for n, p in deploy_pub.events if n == "DeployReverted"][0]
        assert reverted.reverted_to_image_ref == "apitest:rollback-20260716"
        assert reverted.failing_verdict == "fail"

        # The candidate passed (with_extra_env used), was torn down after promote,
        # and the O-32 revert runbook ran on the LIVE leg.
        assert invoker.with_extra_env_calls  # candidate gate applied the overlay
        assert _load(repository, "teardown-cand-run-rev", "cr") is not None
        revert_rb = _load(repository, "revert-run-rev", "cr")
        assert revert_rb is not None
        assert revert_rb.steps[0].step_type == "deploy_compose"

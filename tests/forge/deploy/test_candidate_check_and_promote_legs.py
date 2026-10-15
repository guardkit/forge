"""The deploy stage as two callable legs (protect-main, Part J, rule 39).

* ``candidate_check`` — the candidate up, healthy, through the live gate; on
  a pass it is LEFT STANDING and the result says how many checks ran and
  passed; on a fail it is torn down. It runs every candidate step in the
  working directory it is given (the feature branch's laid-out tree, rule
  38), and the promote never does.
* ``promote`` — ``PROMOTE=1`` (never a rebuild), the candidate torn down,
  the live gate on the live name, the O-32 revert if that fails.
* ``candidate_down`` — the teardown on its own. Since 26 September 2026 it
  takes down the ONE candidate the identity it is handed names, and with no
  identity it refuses and removes nothing: a teardown that names nothing has
  to go looking, and what it finds can belong to another build's check.
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

#: WHICH CANDIDATE A TEARDOWN MEANS. The setting is the project's own (these
#: profiles declare none, so it is the factory's default name) and the value is
#: the identity the CHECK was handed. Without it a teardown is refused.
WHICH_CANDIDATE = {"DEPLOY_IDENTITY": "j-9a01beef@feedfacecafe0"}


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
            identity_env=WHICH_CANDIDATE,
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
            identity_env=WHICH_CANDIDATE,
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
            identity_env=WHICH_CANDIDATE,
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
            identity_env=WHICH_CANDIDATE,
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


class _AGateWhoseAnswerWasLost:
    """A live-gate invoker whose request got no answer, in the words the
    sidecar invoker uses (15 October 2026)."""

    def _lost(self, feature: str) -> LiveGateInvocation:
        return LiveGateInvocation(
            verdict="instrument_fail",
            run_id=f"run-{feature}",
            gate_ids=EIGHT_GATES,
            dry_run=False,
            answer_lost=True,
            detail={"error": "the deploy sidecar could not be reached"},
        )

    def invoke(self, *, feature: str, target: str, gates: tuple[str, ...] = ()):
        return self._lost(feature)

    def with_repo_path(self, repo_path) -> "_AGateWhoseAnswerWasLost":
        return self

    def with_extra_env(self, overlay: dict[str, str]) -> "_AGateWhoseAnswerWasLost":
        return self


class _ASidecarThatAnswers:
    """A loopback deploy sidecar for the REAL script runner (15 October 2026).

    ``check`` and ``teardown`` say what the candidate's own steps and its
    teardown get back: ``"ok"`` (exit 0), ``"red"`` (exit 1, in the script's
    own words), ``"lost"`` (the request is taken and the connection is
    dropped without an answer), ``"unreadable"`` (an answer that is not
    the sidecar's JSON), ``"timed-out"`` (stopped at its time limit) or
    ``"html-error"`` (an HTTP 502 whose body is a proxy's HTML page).
    ``gate`` is what a live-gate driver request gets: ``"timed-out"`` or
    ``"html-error"``."""

    def __init__(
        self, *, check: str, teardown: str, revert: str = "ok", gate: str = "timed-out"
    ) -> None:
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 — the stdlib's name
                body = json.loads(
                    self.rfile.read(int(self.headers.get("Content-Length") or 0))
                )
                env = body.get("env") or {}
                down = env.get("CANDIDATE_DOWN") == "1"
                how = (
                    revert
                    if env.get("REVERT") == "1"
                    else teardown if down else check
                )
                if "driver" in body:
                    how = gate
                    if how.startswith("timed-out"):
                        stopped_at: dict[str, Any] = {
                            "exit_code": 124,
                            "stdout": "",
                            "stderr_tail": "",
                            "timed_out": True,
                        }
                        # "timed-out-no-cwd" says no place; "-wrong-cwd" the
                        # wrong one (Codex round 3).
                        if how == "timed-out":
                            stopped_at["cwd"] = body.get("cwd")
                        elif how == "timed-out-wrong-cwd":
                            stopped_at["cwd"] = "/somewhere/else"
                        stopped = json.dumps(stopped_at).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(stopped)))
                        self.end_headers()
                        self.wfile.write(stopped)
                        return
                if how == "html-error":
                    page = b"<html><body>502 Bad Gateway</body></html>"
                    self.send_response(502)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(page)))
                    self.end_headers()
                    self.wfile.write(page)
                    return
                if how == "lost":
                    self.close_connection = True
                    return
                if how.startswith("timed-out"):
                    stopped_at = {
                        "exit_code": 124,
                        "output_tail": "stopped at its time limit\n",
                        "timed_out": True,
                    }
                    # "timed-out" says no place it ran; "-wrong-cwd" the
                    # wrong one (Codex round 3).
                    if how == "timed-out-wrong-cwd":
                        stopped_at["cwd"] = "/somewhere/else"
                    stopped = json.dumps(stopped_at).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(stopped)))
                    self.end_headers()
                    self.wfile.write(stopped)
                    return
                if how == "unreadable":
                    garbled = b"<html>bad gateway</html>"
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(garbled)))
                    self.end_headers()
                    self.wfile.write(garbled)
                    return
                answer = json.dumps(
                    {
                        "exit_code": 0 if how == "ok" else 1,
                        "output_tail": "ok\n" if how == "ok" else "went red\n",
                        # Where it ran, as the real sidecar says.
                        **({"cwd": body["cwd"]} if body.get("cwd") else {}),
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)

            def log_message(self, *_args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        host, port = self.server.server_address[:2]
        self.url = f"http://{host}:{port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class TestAFailedCandidateSaysWhatIsLeft:
    """15 October 2026: a failed candidate check says whether its candidate is
    still standing and whether a step's request got no answer, so the merge
    press keeps its hold for those alone and puts it down after an ordinary
    red check."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "check, gate, teardown, candidate, lost",
        [
            ("ok", "fail", "ok", "torn-down", False),
            ("red", "pass", "ok", "torn-down", False),
            ("lost", "pass", "ok", "torn-down", True),
            ("unreadable", "pass", "ok", "torn-down", True),
            # Codex R2: the check's script was stopped at its time limit and
            # its teardown answered "down" — what it started may still run.
            ("timed-out", "pass", "ok", "torn-down", True),
            # The teardown's request got no answer: the run says so too.
            ("ok", "fail", "lost", "standing", True),
            ("ok", "lost", "ok", "torn-down", True),
        ],
        ids=[
            "red-gate",
            "red-deploy-step",
            "deploy-answer-lost",
            "deploy-answer-unreadable",
            "deploy-script-timed-out",
            "teardown-lost",
            "gate-answer-lost",
        ],
    )
    async def test_the_answer_names_what_is_left(
        self,
        repository,
        runbook_publisher,
        tmp_path,
        check: str,
        gate: str,
        teardown: str,
        candidate: str,
        lost: bool,
    ) -> None:
        sidecar = _ASidecarThatAnswers(check=check, teardown=teardown)
        try:
            runner = DeployStageRunner(
                repository=repository,
                runbook_publisher=runbook_publisher,
                deploy_publisher=RecordingDeployPublisher(),
                reservation=InProcessReservationLease(),
                live_gate_invoker=(
                    _AGateWhoseAnswerWasLost()
                    if gate == "lost"
                    else _Invoker(
                        candidate=(gate, ALL_GREEN if gate == "pass" else TWO_RED)
                    )
                ),
                broker_inspector=DryRunBrokerInspector(),
                config=DeployStageConfig(
                    execution_surface="sidecar", sidecar_url=sidecar.url
                ),
                deploy_record_root=str(tmp_path / "state"),
                dry_run=False,
                target_repo="guardkit/api_test",
                clock=lambda: FIXED,
            )
            result = await runner.candidate_check(
                _profile(),
                correlation_id="cl",
                deploy_run_id="run-cl",
                feature="FEAT-CL",
                identity_env=WHICH_CANDIDATE,
            )
        finally:
            sidecar.close()

        assert result.outcome == "failed"
        assert result.detail["candidate"] == candidate
        assert bool(result.detail.get("answer_lost")) is lost

    @pytest.mark.asyncio
    @pytest.mark.parametrize("lost", [True, False], ids=["gate-lost", "gate-red"])
    async def test_a_promote_whose_live_gate_got_no_answer_says_so(
        self, repository, runbook_publisher, tmp_path, lost: bool
    ) -> None:
        """The promote's live gate got no answer: the deploy is reverted as
        before, and the result says the gate's driver may still be running."""
        runner = _runner(
            repository,
            runbook_publisher,
            RecordingDeployPublisher(),
            tmp_path,
            live_gate_invoker=(
                _AGateWhoseAnswerWasLost() if lost else _Invoker(live=("fail", TWO_RED))
            ),
        )
        result = await runner.promote(
            _profile(rollback="apitest:rollback"),
            correlation_id="pl",
            deploy_run_id="run-pl",
            feature="FEAT-PL",
            prior_events=("DeployQueued",),
            identity_env=WHICH_CANDIDATE,
        )
        assert result.outcome == "reverted", result
        assert bool(result.detail.get("answer_lost")) is lost


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
            identity_env=WHICH_CANDIDATE,
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
            identity_env=WHICH_CANDIDATE,
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
            _profile(),
            correlation_id="cd",
            deploy_run_id="run-cd",
            identity_env=WHICH_CANDIDATE,
        )
        assert result.outcome == "complete"
        assert result.detail == {"candidate": "torn-down"}
        teardown = _load(repository, "teardown-cand-run-cd", "cd")
        params = _step_params(teardown, "deploy_compose")
        assert params["extra_env"]["CANDIDATE_DOWN"] == "1"
        # AND WHICH CANDIDATE IT MEANS, under the name this project declares.
        assert params["extra_env"]["DEPLOY_IDENTITY"] == WHICH_CANDIDATE[
            "DEPLOY_IDENTITY"
        ]
        absent = await runner.candidate_down(
            _profile(candidate=False), correlation_id="cd2", deploy_run_id="run-cd2"
        )
        assert absent.outcome == "complete"
        assert absent.detail["candidate"] == "absent"

    @pytest.mark.asyncio
    async def test_a_teardown_that_names_nothing_is_refused_and_removes_nothing(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        """One build's cleanup must never touch another's candidate.

        The teardown used to run with no name at all, and a project whose
        candidate belongs to one check rather than to a shared name then had to
        go looking — so one build's ending removed every other build's standing
        candidate. With no identity nothing is run and nothing is removed.
        """
        deploy_pub = RecordingDeployPublisher()
        runner = _runner(repository, runbook_publisher, deploy_pub, tmp_path)
        result = await runner.candidate_down(
            _profile(), correlation_id="cdx", deploy_run_id="run-cdx"
        )
        assert result.outcome == "failed"
        assert result.failed_step == "candidate_down"
        assert result.detail["candidate"] == "standing"
        assert "no identity" in result.detail["refusal"]
        assert "belong to another build's check" in result.detail["refusal"]
        # Nothing was even rendered, so nothing could have been run.
        assert _load(repository, "teardown-cand-run-cdx", "cdx") is None

    @pytest.mark.asyncio
    async def test_a_setting_with_an_empty_value_names_nothing_either(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        deploy_pub = RecordingDeployPublisher()
        runner = _runner(repository, runbook_publisher, deploy_pub, tmp_path)
        result = await runner.candidate_down(
            _profile(),
            correlation_id="cdy",
            deploy_run_id="run-cdy",
            identity_env={"DEPLOY_IDENTITY": "   "},
        )
        assert result.outcome == "failed"
        assert _load(repository, "teardown-cand-run-cdy", "cdy") is None


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
            _profile(),
            correlation_id="rd",
            deploy_run_id="run-rd",
            feature="FEAT-7",
            identity_env=WHICH_CANDIDATE,
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
            # WHICH CANDIDATE A TEARDOWN MEANS: every leg that can take one
            # down is told, and one that is told nothing removes nothing.
            identity_env=WHICH_CANDIDATE,
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


class TestTheScriptRunnerSaysWhenNoAnswerCameBack:
    """15 October 2026: "no answer came back" is a flag on the runner's own
    answer, never read from its words."""

    @staticmethod
    def _run(base_url: str) -> Any:
        from forge.deploy.sidecar_runner import SidecarScriptRunner

        runner = SidecarScriptRunner(
            base_url=base_url, repo="guardkit/api_test", http_timeout_margin=2.0
        )
        return runner(cwd="/sandbox/clone", script="deploy/deploy.sh", env_file=None, timeout=1)

    @pytest.mark.parametrize(
        "how, lost",
        [
            ("ok", False),
            ("red", False),
            ("lost", True),
            ("unreadable", True),
            ("timed-out", True),
        ],
    )
    def test_the_flag_follows_the_answer(self, how: str, lost: bool) -> None:
        from forge.deploy.sidecar_runner import no_answer_came_back

        sidecar = _ASidecarThatAnswers(check=how, teardown=how)
        try:
            answer = self._run(sidecar.url)
        finally:
            sidecar.close()
        exit_code, _output = answer
        assert no_answer_came_back(answer) is lost
        assert (exit_code == 0) is (how == "ok")

    def test_a_sidecar_that_cannot_be_reached_is_no_answer(self) -> None:
        from forge.deploy.sidecar_runner import no_answer_came_back

        assert no_answer_came_back(self._run("http://127.0.0.1:9")) is True


class TestTheFailureLineSaysWhereTheCandidateIs:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("teardown", ["ok", "lost"])
    async def test_the_line_matches_the_detail(
        self, repository, runbook_publisher, tmp_path, caplog, teardown: str
    ) -> None:
        import logging

        sidecar = _ASidecarThatAnswers(check="ok", teardown=teardown)
        try:
            runner = DeployStageRunner(
                repository=repository,
                runbook_publisher=runbook_publisher,
                deploy_publisher=RecordingDeployPublisher(),
                reservation=InProcessReservationLease(),
                live_gate_invoker=_Invoker(candidate=("fail", TWO_RED)),
                broker_inspector=DryRunBrokerInspector(),
                config=DeployStageConfig(
                    execution_surface="sidecar", sidecar_url=sidecar.url
                ),
                deploy_record_root=str(tmp_path / "state"),
                dry_run=False,
                target_repo="guardkit/api_test",
                clock=lambda: FIXED,
            )
            with caplog.at_level(logging.ERROR, logger="forge.deploy.stage"):
                result = await runner.candidate_check(
                    _profile(),
                    correlation_id="cw",
                    deploy_run_id="run-cw",
                    feature="FEAT-CW",
                    identity_env=WHICH_CANDIDATE,
                )
        finally:
            sidecar.close()

        line = next(
            r.getMessage() for r in caplog.records if "gate refused promote" in r.getMessage()
        )
        if result.detail["candidate"] == "standing":
            assert "is still standing" in line and "was torn down" not in line
        else:
            assert "was torn down" in line
        assert result.detail["candidate"] == ("torn-down" if teardown == "ok" else "standing")



def _a_stage_through(sidecar: Any, repository, runbook_publisher, tmp_path, invoker) -> Any:
    return DeployStageRunner(
        repository=repository,
        runbook_publisher=runbook_publisher,
        deploy_publisher=RecordingDeployPublisher(),
        reservation=InProcessReservationLease(),
        live_gate_invoker=invoker,
        broker_inspector=DryRunBrokerInspector(),
        config=DeployStageConfig(execution_surface="sidecar", sidecar_url=sidecar.url),
        deploy_record_root=str(tmp_path / "state"),
        dry_run=False,
        target_repo="guardkit/api_test",
        clock=lambda: FIXED,
    )


class TestEveryResultOfARunSaysIfAnAnswerWasLost:
    """15 October 2026, the coach's run-wide stamp: a request with no answer
    ANYWHERE in a stage run marks every result that run returns."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("revert, lost", [("lost", True), ("ok", False)])
    async def test_a_revert_whose_answer_was_lost(
        self, repository, runbook_publisher, tmp_path, revert: str, lost: bool
    ) -> None:
        sidecar = _ASidecarThatAnswers(check="ok", teardown="ok", revert=revert)
        try:
            runner = _a_stage_through(
                sidecar, repository, runbook_publisher, tmp_path,
                _Invoker(live=("fail", TWO_RED)),
            )
            result = await runner.promote(
                _profile(rollback="apitest:rollback"),
                correlation_id="rv",
                deploy_run_id="run-rv",
                feature="FEAT-RV",
                prior_events=("DeployQueued",),
                identity_env=WHICH_CANDIDATE,
            )
        finally:
            sidecar.close()
        assert result.outcome in ("reverted", "failed"), result
        assert bool(result.detail.get("answer_lost")) is lost

    @pytest.mark.asyncio
    @pytest.mark.parametrize("check, lost", [("lost", True), ("ok", False)])
    async def test_the_read_only_question(
        self, repository, runbook_publisher, tmp_path, check: str, lost: bool
    ) -> None:
        sidecar = _ASidecarThatAnswers(check=check, teardown="ok")
        try:
            runner = _a_stage_through(
                sidecar, repository, runbook_publisher, tmp_path, _Invoker()
            )
            result = await runner.what_is_running(
                _profile(),
                correlation_id="wr",
                deploy_run_id="run-wr",
                ask_env={"RUNNING_IDENTITY": "1"},
            )
        finally:
            sidecar.close()
        assert bool(result.detail.get("answer_lost")) is lost

    def test_no_answer_survives_a_copy_and_a_pickle(self) -> None:
        import copy
        import pickle

        from forge.deploy.sidecar_runner import NoAnswer, no_answer_came_back

        answer = NoAnswer("sidecar unreachable at http://x: timed out")
        for again in (copy.deepcopy(answer), pickle.loads(pickle.dumps(answer))):
            assert tuple(again) == tuple(answer)
            assert no_answer_came_back(again)



class TestAScriptStoppedAtItsTimeLimit:
    """15 October 2026, Codex R2: a script stopped at its time limit may have
    left what it started running, so it is counted like a request with no
    answer — by the subprocess core, the sidecar's answer, and a stage run
    with no sidecar at all."""

    def test_the_subprocess_core_says_it_timed_out(self, tmp_path) -> None:
        import pickle
        import subprocess

        from forge.executor.shell_steps import TimedOut, _run_script_step
        from forge.deploy.sidecar_runner import no_answer_came_back

        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        script = tmp_path / "slow.sh"
        script.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
        script.chmod(0o755)

        answer = _run_script_step(
            cwd=str(tmp_path), script="slow.sh", env_file=None, timeout=1
        )

        assert isinstance(answer, TimedOut)
        exit_code, _output = answer
        assert exit_code == 124
        assert no_answer_came_back(answer)
        assert no_answer_came_back(pickle.loads(pickle.dumps(answer)))

    @pytest.mark.asyncio
    async def test_a_stage_with_no_sidecar_notes_it(
        self, repository, runbook_publisher, tmp_path, monkeypatch
    ) -> None:
        from forge.executor import shell_steps

        def _a_check_that_runs_out_of_time(**kwargs: Any) -> Any:
            if (kwargs.get("extra_env") or {}).get("CANDIDATE_DOWN") == "1":
                return (0, "down\n")
            return shell_steps.TimedOut(124, "stopped at its time limit\n")

        monkeypatch.setattr(shell_steps, "_run_script_step", _a_check_that_runs_out_of_time)
        runner = DeployStageRunner(
            repository=repository,
            runbook_publisher=runbook_publisher,
            deploy_publisher=RecordingDeployPublisher(),
            reservation=InProcessReservationLease(),
            live_gate_invoker=_Invoker(),
            broker_inspector=DryRunBrokerInspector(),
            config=DeployStageConfig(),
            deploy_record_root=str(tmp_path / "state"),
            dry_run=False,
            clock=lambda: FIXED,
        )
        result = await runner.candidate_check(
            _profile(),
            correlation_id="lt",
            deploy_run_id="run-lt",
            feature="FEAT-LT",
            identity_env=WHICH_CANDIDATE,
        )
        assert result.outcome == "failed"
        assert result.detail["candidate"] == "torn-down"
        assert result.detail.get("answer_lost") is True



class _ASidecarThatRefusesWith:
    """A loopback server answering every request with HTTP ``status`` and the
    raw ``body`` bytes (15 October 2026, Codex R5)."""

    def __init__(self, status: int, body: bytes) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 — the stdlib's name
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        host, port = self.server.server_address[:2]
        self.url = f"http://{host}:{port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


_REFUSALS = [
    (502, b"<html><body>502 Bad Gateway</body></html>", True),
    (500, b"", True),
    (400, b"{}", True),
    (400, b'{"error": ""}', True),
    (400, b'{"error": "script deploy/evil.sh is not one the profile names"}', False),
]
_REFUSAL_IDS = ["html", "empty", "empty-object", "blank-sentence", "sidecars-own-words"]


class TestAnHttpErrorIsAnAnswerOnlyInTheSidecarsOwnWords:
    """15 October 2026, Codex R5: the script runner and the live-gate invoker
    read an HTTP error as an answered refusal only when it carries the
    sidecar's own refusal sentence. Anything else is no answer."""

    @pytest.mark.parametrize("status, body, lost", _REFUSALS, ids=_REFUSAL_IDS)
    def test_the_script_runner(self, status: int, body: bytes, lost: bool) -> None:
        from forge.deploy.sidecar_runner import SidecarScriptRunner, no_answer_came_back

        sidecar = _ASidecarThatRefusesWith(status, body)
        try:
            answer = SidecarScriptRunner(base_url=sidecar.url, repo="guardkit/api_test")(
                cwd="/sandbox/clone", script="deploy/deploy.sh", env_file=None, timeout=1
            )
        finally:
            sidecar.close()
        assert no_answer_came_back(answer) is lost
        exit_code, _output = answer
        assert exit_code != 0

    @pytest.mark.parametrize("status, body, lost", _REFUSALS, ids=_REFUSAL_IDS)
    def test_the_live_gate_invoker(
        self, tmp_path, status: int, body: bytes, lost: bool
    ) -> None:
        from forge.deploy.live_gate import SidecarLiveGateInvoker

        sidecar = _ASidecarThatRefusesWith(status, body)
        try:
            answer = SidecarLiveGateInvoker(
                base_url=sidecar.url,
                repo="guardkit/api_test",
                repo_path=tmp_path,
                driver_argv=["python3", "qa/gate.py"],
                timeout_seconds=1,
            ).invoke(feature="FEAT-R5", target="apitest-sbx")
        finally:
            sidecar.close()
        assert answer.verdict == "instrument_fail"
        assert answer.answer_lost is lost


class TestASandboxGateStoppedAtItsTimeLimit:
    """15 October 2026, Codex R4: the sandbox's live-gate driver was stopped
    at its time limit. The invoker says no answer came back, and a candidate
    check whose gate it was says so even after its teardown answered."""

    def test_the_invoker_says_so(self, tmp_path) -> None:
        from forge.deploy.live_gate import SidecarLiveGateInvoker

        sidecar = _ASidecarThatAnswers(check="ok", teardown="ok", gate="timed-out")
        try:
            answer = SidecarLiveGateInvoker(
                base_url=sidecar.url,
                repo="guardkit/api_test",
                repo_path=tmp_path,
                driver_argv=["python3", "qa/gate.py"],
                timeout_seconds=1,
            ).invoke(feature="FEAT-R4", target="apitest-sbx")
        finally:
            sidecar.close()
        assert answer.verdict == "environment_fail"
        assert answer.answer_lost is True

    @pytest.mark.asyncio
    async def test_the_candidate_check_says_so_after_its_teardown(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        from forge.deploy.live_gate import SidecarLiveGateInvoker

        sidecar = _ASidecarThatAnswers(check="ok", teardown="ok", gate="timed-out")
        try:
            runner = _a_stage_through(
                sidecar,
                repository,
                runbook_publisher,
                tmp_path,
                SidecarLiveGateInvoker(
                    base_url=sidecar.url,
                    repo="guardkit/api_test",
                    repo_path=tmp_path,
                    driver_argv=["python3", "qa/gate.py"],
                    timeout_seconds=1,
                ),
            )
            result = await runner.candidate_check(
                _profile(),
                correlation_id="gt",
                deploy_run_id="run-gt",
                feature="FEAT-GT",
                identity_env=WHICH_CANDIDATE,
            )
        finally:
            sidecar.close()
        assert result.outcome == "failed"
        assert result.detail["candidate"] == "torn-down"
        assert result.detail.get("answer_lost") is True



_A_TREE = "/sandbox/clone/.forge-candidates/FEAT-TF"


class TestATimeoutIsReadFirst:
    """15 October 2026, Codex round 3: a reply that says its program was
    stopped at its time limit is a lost answer whatever else is wrong with it,
    including a missing or wrong place it says it ran."""

    @pytest.mark.parametrize(
        "status, body",
        [
            (200, b'{"exit_code": 124, "timed_out": true}'),
            (200, b'{"exit_code": 124, "timed_out": true, "cwd": "/somewhere/else"}'),
            (200, b'{"timed_out": true}'),
            (500, b'{"timed_out": true}'),
        ],
        ids=["no-cwd", "wrong-cwd", "no-exit-code", "http-error"],
    )
    def test_the_script_runner(self, status: int, body: bytes) -> None:
        from forge.deploy.sidecar_runner import SidecarScriptRunner, no_answer_came_back

        sidecar = _ASidecarThatRefusesWith(status, body)
        try:
            answer = SidecarScriptRunner(base_url=sidecar.url, repo="guardkit/api_test")(
                cwd=_A_TREE, script="deploy/deploy.sh", env_file=None, timeout=1
            )
        finally:
            sidecar.close()
        assert no_answer_came_back(answer)
        assert getattr(answer, "timed_out", False) is True

    @pytest.mark.parametrize(
        "status, body",
        [
            (200, b'{"exit_code": 124, "timed_out": true}'),
            (200, b'{"exit_code": 124, "timed_out": true, "cwd": "/somewhere/else"}'),
            (200, b'{"timed_out": true}'),
            (500, b'{"timed_out": true}'),
        ],
        ids=["no-cwd", "wrong-cwd", "no-exit-code", "http-error"],
    )
    def test_the_live_gate_invoker(self, tmp_path, status: int, body: bytes) -> None:
        from forge.deploy.live_gate import SidecarLiveGateInvoker

        tree = tmp_path / ".forge-candidates" / "FEAT-TF"
        tree.mkdir(parents=True)
        sidecar = _ASidecarThatRefusesWith(status, body)
        try:
            answer = SidecarLiveGateInvoker(
                base_url=sidecar.url,
                repo="guardkit/api_test",
                repo_path=tree,
                driver_argv=["python3", "qa/gate.py"],
                timeout_seconds=1,
            ).invoke(feature="FEAT-TF", target="apitest-sbx")
        finally:
            sidecar.close()
        assert answer.answer_lost is True

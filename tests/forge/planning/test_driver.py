"""Tests for the Mode P planning chain driver (TASK-MP-012).

The driver is exercised against a REAL v3 SQLite store + real planning
gate adapters, with fakes only at the wire seams (dispatch, approval
publisher, response waiter, git, notifications) — proving the chain
QUEUED → RUNNING → PO → PAUSED → decision → PLANNED_HANDOFF end-to-end
from durable state, including restart re-entry (RT-05/RT-08) and the
two-phase escalation policy.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forge.adapters.git.models import GitOpResult
from forge.adapters.sqlite import connect as sqlite_connect
from forge.gating.identity import derive_request_id
from forge.lifecycle import migrations
from forge.planning.driver import PlanningDriverDeps, PlanningRunDriver
from forge.planning.gate_adapters import build_planning_gate_adapters
from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.states import PlanningState
from forge.config.models import PlanningConfig
from nats_core.events import ApprovalRequestPayload, ApprovalResponsePayload

CID = "drv-run-001"
PLAN_RUN_ID = f"plan-{CID}"
ORIGINATOR = "U0RIGINATOR"
ESCALATOR = "rich"
FROZEN = datetime(2026, 7, 6, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures / doubles
# ---------------------------------------------------------------------------


@pytest.fixture
def db_connection(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "driver.db"
    cx = sqlite_connect.connect_writer(db_path)
    migrations.apply_at_boot(cx)
    return cx


@pytest.fixture
def store(db_connection: sqlite3.Connection) -> SqlitePlanningRunStore:
    return SqlitePlanningRunStore(db_connection)


class MutableClock:
    """Clock the test can advance to trip escalation windows."""

    def __init__(self, start: datetime = FROZEN) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class FakePublisher:
    """Records approval request envelopes; optionally raises."""

    def __init__(self) -> None:
        self.envelopes: list[Any] = []

    async def publish_request(self, envelope: Any) -> None:
        self.envelopes.append(envelope)


class ScriptedSubscriber:
    """Returns pre-scripted responses per await_response call."""

    def __init__(self, script: list[Any], armed: asyncio.Event | None) -> None:
        self._script = script
        self._armed = armed
        self.calls: list[dict[str, Any]] = []

    async def await_response(self, build_id: str, **kwargs: Any) -> Any:
        if self._armed is not None:
            self._armed.set()
        self.calls.append({"build_id": build_id, **kwargs})
        if not self._script:
            return None
        return self._script.pop(0)


class RecordingGitRunner:
    def __init__(self, should_fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.should_fail = should_fail

    async def prepare_branch_and_write(
        self, repo_path: str, branch: str, file_path: str, content: str
    ) -> GitOpResult:
        self.calls.append(
            {
                "repo_path": repo_path,
                "branch": branch,
                "file_path": file_path,
                "content": content,
            }
        )
        if self.should_fail:
            return GitOpResult(
                status="failed",
                operation="prepare_branch_and_write",
                stderr="simulated git failure",
                exit_code=1,
            )
        return GitOpResult(
            status="success",
            operation="prepare_branch_and_write",
            sha="abc123def",
            exit_code=0,
        )


class FakeSecondOpinion:
    async def get_summary_for_approval(
        self, *, plan_run_id: str, stage_label: str
    ) -> dict[str, Any]:
        return {"title": "PO docs", "plan_run_id": plan_run_id}


# The REAL product document the PO produces (deployed ``role_output`` shape,
# M10). criterion_breakdown is Coach *evidence* about this document, not the
# document itself — sourcing docs_summary from it delivered an empty doc.
_PO_ROLE_OUTPUT: dict[str, Any] = {
    "title": "the product docs",
    "problem_statement": "ship a thing",
    "user_stories": [{"as_a": "user", "i_want": "a thing"}],
}


def _po_result(outcome: str = "completed", coach_score: float = 0.9) -> Any:
    return SimpleNamespace(
        outcome=SimpleNamespace(value=outcome),
        coach_score=coach_score,
        criterion_breakdown=[
            {"criterion": "clarity", "score": 0.9, "weight": 1.0, "rationale": "ok"},
        ],
        detection_findings=(),
        role_output=dict(_PO_ROLE_OUTPUT),
        reason=None,
    )


def _request_id(attempt: int = 0) -> str:
    return derive_request_id(
        build_id=PLAN_RUN_ID, stage_label="product_docs", attempt_count=attempt
    )


def _approve(attempt: int = 0, decided_by: str = ORIGINATOR) -> ApprovalResponsePayload:
    return ApprovalResponsePayload(
        request_id=_request_id(attempt), decision="approve", decided_by=decided_by
    )


def _make_driver(
    store: SqlitePlanningRunStore,
    *,
    clock: MutableClock | None = None,
    subscriber_scripts: list[list[Any]] | None = None,
    po_outcome: str = "completed",
    git_runner: RecordingGitRunner | None = None,
    config: PlanningConfig | None = None,
    po_result: Any | None = None,
) -> tuple[PlanningRunDriver, dict[str, Any]]:
    clock = clock or MutableClock()
    repository, state_machine = build_planning_gate_adapters(store, clock=clock)
    publisher = FakePublisher()
    git = git_runner or RecordingGitRunner()
    notifications: list[tuple[str, str, str]] = []
    scripts = list(subscriber_scripts or [])
    subscribers: list[ScriptedSubscriber] = []

    def subscriber_factory(expected_approver: Any, armed: Any) -> ScriptedSubscriber:
        script = scripts.pop(0) if scripts else []
        sub = ScriptedSubscriber(script, armed)
        subscribers.append(sub)
        return sub

    po_calls: list[dict[str, Any]] = []

    async def dispatch_po(*, plan_run_id: str, correlation_id: str) -> Any:
        po_calls.append({"plan_run_id": plan_run_id, "correlation_id": correlation_id})
        if po_result is not None:
            return po_result
        return _po_result(po_outcome)

    async def publish_notification(cid: str, message: str, level: str) -> None:
        notifications.append((cid, message, level))

    cfg = config or PlanningConfig(
        enabled=True,
        escalation_approver=ESCALATOR,
        originator_wait_seconds=300,
        escalated_wait_seconds=1800,
        target_repo_paths={"appmilla/widgets": "/srv/repos/widgets"},
    )

    driver = PlanningRunDriver(
        PlanningDriverDeps(
            store=store,
            repository=repository,
            state_machine=state_machine,
            approval_publisher=publisher,
            subscriber_factory=subscriber_factory,
            dispatch_product_owner=dispatch_po,
            second_opinion_provider=FakeSecondOpinion(),
            git_runner=git,
            planning_config=cfg,
            clock=clock,
            publish_notification=publish_notification,
        )
    )
    return driver, {
        "publisher": publisher,
        "git": git,
        "notifications": notifications,
        "po_calls": po_calls,
        "subscribers": subscribers,
        "clock": clock,
    }


def _queue_run(store: SqlitePlanningRunStore) -> None:
    store.record_queued(
        correlation_id=CID,
        originating_user=ORIGINATOR,
        expected_approver=ORIGINATOR,
        request_text="build a widget",
        triggered_by="jarvis",
        target_repo="appmilla/widgets",
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_queued_run_reaches_planned_handoff_on_approval(
        self, store: SqlitePlanningRunStore
    ) -> None:
        _queue_run(store)
        driver, ctx = _make_driver(store, subscriber_scripts=[[_approve(attempt=0)]])

        await driver.drive(CID)

        run = store.get_run(CID)
        assert run is not None
        assert run["state"] == PlanningState.PLANNED_HANDOFF.value
        assert run["handoff_branch"] == f"planning/{CID}"
        assert run["handoff_path"] is not None

        # PO dispatched exactly once with namespaced run id
        assert ctx["po_calls"] == [{"plan_run_id": PLAN_RUN_ID, "correlation_id": CID}]

        # Git handoff executed once with sanitized paths
        assert len(ctx["git"].calls) == 1
        assert ctx["git"].calls[0]["branch"] == f"planning/{CID}"
        assert ctx["git"].calls[0]["file_path"] == f"feature_spec_inputs/{CID}.md"

        # Approval envelope was wire-valid and named the originator
        assert len(ctx["publisher"].envelopes) == 1
        payload = ApprovalRequestPayload.model_validate(
            ctx["publisher"].envelopes[0].payload
        )
        assert payload.details["build_id"] == PLAN_RUN_ID
        assert payload.details["expected_approver"] == ORIGINATOR

        # Success notification published
        assert any("Planning complete" in m for _, m, _ in ctx["notifications"])

    @pytest.mark.asyncio
    async def test_checkpoint_cleared_event_recorded(
        self, store: SqlitePlanningRunStore
    ) -> None:
        _queue_run(store)
        driver, _ = _make_driver(store, subscriber_scripts=[[_approve()]])
        await driver.drive(CID)

        events = store.list_events(CID)
        statuses = [e["status"] for e in events]
        assert "checkpoint_cleared" in statuses
        cleared = next(e for e in events if e["status"] == "checkpoint_cleared")
        assert cleared["actor_identity"] == ORIGINATOR


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


class TestDecisions:
    @pytest.mark.asyncio
    async def test_rejection_cancels_run_and_makes_zero_git_calls(
        self, store: SqlitePlanningRunStore
    ) -> None:
        _queue_run(store)
        reject = ApprovalResponsePayload(
            request_id=_request_id(0), decision="reject", decided_by=ORIGINATOR
        )
        driver, ctx = _make_driver(store, subscriber_scripts=[[reject]])

        await driver.drive(CID)

        run = store.get_run(CID)
        assert run["state"] == PlanningState.CANCELLED.value
        assert ctx["git"].calls == []
        assert any("rejected" in m for _, m, _ in ctx["notifications"])

    @pytest.mark.asyncio
    async def test_wrong_responder_is_refused_and_wait_continues(
        self, store: SqlitePlanningRunStore
    ) -> None:
        """DF-009 identity pinning: an impostor approve does not resume."""
        _queue_run(store)
        impostor = ApprovalResponsePayload(
            request_id=_request_id(0), decision="approve", decided_by="mallory"
        )
        driver, ctx = _make_driver(store, subscriber_scripts=[[impostor], [_approve()]])

        await driver.drive(CID)

        run = store.get_run(CID)
        assert run["state"] == PlanningState.PLANNED_HANDOFF.value
        # Two wait rounds: refused impostor, then the real approve
        assert len(ctx["subscribers"]) == 2

    @pytest.mark.asyncio
    async def test_stale_request_id_is_ignored(
        self, store: SqlitePlanningRunStore
    ) -> None:
        _queue_run(store)
        stale = ApprovalResponsePayload(
            request_id=derive_request_id(
                build_id=PLAN_RUN_ID, stage_label="product_docs", attempt_count=7
            ),
            decision="approve",
            decided_by=ORIGINATOR,
        )
        driver, ctx = _make_driver(store, subscriber_scripts=[[stale], [_approve()]])

        await driver.drive(CID)

        run = store.get_run(CID)
        assert run["state"] == PlanningState.PLANNED_HANDOFF.value


# ---------------------------------------------------------------------------
# Escalation / timeout
# ---------------------------------------------------------------------------


class TestEscalation:
    @pytest.mark.asyncio
    async def test_phase1_expiry_escalates_then_escalated_approve_resumes(
        self, store: SqlitePlanningRunStore
    ) -> None:
        _queue_run(store)
        clock = MutableClock()

        escalated_approve = ApprovalResponsePayload(
            request_id=_request_id(1), decision="approve", decided_by=ESCALATOR
        )

        # Round 1: originator window expires (None) — the clock is advanced
        # by the script hook below; round 2: escalated approver approves.
        driver, ctx = _make_driver(
            store,
            clock=clock,
            subscriber_scripts=[[None], [escalated_approve]],
            config=PlanningConfig(
                enabled=True,
                escalation_approver=ESCALATOR,
                originator_wait_seconds=300,
                escalated_wait_seconds=1800,
                target_repo_paths={"appmilla/widgets": "/srv/repos/widgets"},
            ),
        )

        # Advance past the originator window the moment the first wait ends.
        orig_await = ScriptedSubscriber.await_response

        async def timed_await(self: ScriptedSubscriber, build_id: str, **kw: Any):
            result = await orig_await(self, build_id, **kw)
            if result is None:
                clock.advance(301)
            return result

        ScriptedSubscriber.await_response = timed_await  # type: ignore[method-assign]
        try:
            await driver.drive(CID)
        finally:
            ScriptedSubscriber.await_response = orig_await  # type: ignore[method-assign]

        run = store.get_run(CID)
        assert run["state"] == PlanningState.PLANNED_HANDOFF.value
        assert run["expected_approver"] == ESCALATOR
        assert run["escalated_at"] is not None
        # Escalated round re-published AFTER arming (initial + escalated)
        assert len(ctx["publisher"].envelopes) == 2
        escalated_payload = ApprovalRequestPayload.model_validate(
            ctx["publisher"].envelopes[1].payload
        )
        assert escalated_payload.details["expected_approver"] == ESCALATOR
        assert escalated_payload.request_id == _request_id(1)

    @pytest.mark.asyncio
    async def test_no_escalation_approver_times_out_at_phase1(
        self, store: SqlitePlanningRunStore
    ) -> None:
        _queue_run(store)
        clock = MutableClock()
        driver, ctx = _make_driver(
            store,
            clock=clock,
            subscriber_scripts=[[None]],
            config=PlanningConfig(
                enabled=True,
                escalation_approver=None,
                originator_wait_seconds=300,
                escalated_wait_seconds=1800,
            ),
        )

        orig_await = ScriptedSubscriber.await_response

        async def timed_await(self: ScriptedSubscriber, build_id: str, **kw: Any):
            result = await orig_await(self, build_id, **kw)
            if result is None:
                clock.advance(301)
            return result

        ScriptedSubscriber.await_response = timed_await  # type: ignore[method-assign]
        try:
            await driver.drive(CID)
        finally:
            ScriptedSubscriber.await_response = orig_await  # type: ignore[method-assign]

        run = store.get_run(CID)
        assert run["state"] == PlanningState.TIMED_OUT.value
        assert any("timed out" in m for _, m, _ in ctx["notifications"])


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


class TestFailures:
    @pytest.mark.asyncio
    async def test_po_dispatch_error_fails_run(
        self, store: SqlitePlanningRunStore
    ) -> None:
        _queue_run(store)
        driver, ctx = _make_driver(store, po_outcome="error")

        await driver.drive(CID)

        run = store.get_run(CID)
        assert run["state"] == PlanningState.FAILED.value
        assert "PO dispatch" in (run["error"] or "")
        assert ctx["publisher"].envelopes == []

    @pytest.mark.asyncio
    async def test_git_failure_fails_run_after_approval(
        self, store: SqlitePlanningRunStore
    ) -> None:
        _queue_run(store)
        driver, ctx = _make_driver(
            store,
            subscriber_scripts=[[_approve()]],
            git_runner=RecordingGitRunner(should_fail=True),
        )

        await driver.drive(CID)

        run = store.get_run(CID)
        assert run["state"] == PlanningState.FAILED.value
        assert "Git operation failed" in (run["error"] or "")
        assert any("handoff failed" in m for _, m, _ in ctx["notifications"])


# ---------------------------------------------------------------------------
# Restart re-entry (RT-05 / RT-08) and rearm
# ---------------------------------------------------------------------------


class TestRestartReentry:
    @pytest.mark.asyncio
    async def test_running_run_with_cleared_checkpoint_resumes_at_handoff(
        self, store: SqlitePlanningRunStore
    ) -> None:
        """RT-08: crash after approval re-drives straight to the handoff."""
        _queue_run(store)
        store.transition(
            correlation_id=CID,
            to_state=PlanningState.RUNNING,
            actor_identity="test",
        )
        store._record_event(
            correlation_id=CID,
            stage_label="product_owner",
            status="approved",
            actor_identity="test",
            details_json='{"po_output": {"docs_summary": {"k": "v"}}}',
        )
        store._record_event(
            correlation_id=CID,
            stage_label="product_docs",
            status="checkpoint_cleared",
            actor_identity=ORIGINATOR,
            details_json='{"stage_label": "product_docs"}',
        )

        driver, ctx = _make_driver(store)
        await driver.drive(CID)

        run = store.get_run(CID)
        assert run["state"] == PlanningState.PLANNED_HANDOFF.value
        # No second PO dispatch, no new approval round
        assert ctx["po_calls"] == []
        assert ctx["publisher"].envelopes == []
        assert len(ctx["git"].calls) == 1

    @pytest.mark.asyncio
    async def test_rearm_republishes_persisted_request_id_verbatim(
        self, store: SqlitePlanningRunStore
    ) -> None:
        """ASSUM-015 compensating half: the persisted id is re-emitted."""
        _queue_run(store)
        store.transition(
            correlation_id=CID,
            to_state=PlanningState.RUNNING,
            actor_identity="test",
        )
        store._record_event(
            correlation_id=CID,
            stage_label="product_owner",
            status="approved",
            actor_identity="test",
        )
        store.transition(
            correlation_id=CID,
            to_state=PlanningState.PAUSED,
            actor_identity="test",
        )
        persisted_id = _request_id(0)
        store.update_pending_approval_request_id(CID, persisted_id)
        store.update_escalation(correlation_id=CID, paused_at=FROZEN.isoformat())

        driver, ctx = _make_driver(store, subscriber_scripts=[[_approve()]])
        await driver.drive(CID, republish_pending=True)

        run = store.get_run(CID)
        assert run["state"] == PlanningState.PLANNED_HANDOFF.value
        # Exactly one re-emit, carrying the persisted request_id verbatim
        assert len(ctx["publisher"].envelopes) == 1
        payload = ApprovalRequestPayload.model_validate(
            ctx["publisher"].envelopes[0].payload
        )
        assert payload.request_id == persisted_id
        assert payload.details["checkpoint_type"] == "product_docs_recovered"

    @pytest.mark.asyncio
    async def test_drive_is_noop_for_terminal_run(
        self, store: SqlitePlanningRunStore
    ) -> None:
        _queue_run(store)
        store.transition(
            correlation_id=CID,
            to_state=PlanningState.CANCELLED,
            actor_identity="test",
        )
        driver, ctx = _make_driver(store)
        await driver.drive(CID)
        assert ctx["po_calls"] == []
        assert ctx["git"].calls == []


# ---------------------------------------------------------------------------
# TASK-MP-012 review fixes: defer round (arm-before-post republish)
# ---------------------------------------------------------------------------


class TestDeferRound:
    @pytest.mark.asyncio
    async def test_defer_persists_new_round_and_republishes_after_arming(
        self, store: SqlitePlanningRunStore
    ) -> None:
        """A below-cap defer mints attempt+1, re-emits it armed, then approve wins."""
        _queue_run(store)
        defer = ApprovalResponsePayload(
            request_id=_request_id(0), decision="defer", decided_by=ORIGINATOR
        )
        # Round 1: defer; round 2: approve the NEW round's request_id.
        driver, ctx = _make_driver(
            store,
            subscriber_scripts=[[defer], [_approve(attempt=1)]],
        )

        await driver.drive(CID)

        run = store.get_run(CID)
        assert run["state"] == PlanningState.PLANNED_HANDOFF.value
        assert run["defer_count"] == 1

        # Envelope sequence: initial checkpoint (attempt 0), then the
        # deferred round re-emitted AFTER the second waiter armed
        # (arm-before-post — the escalation module persisted but did not
        # publish; the driver owns the armed re-emit).
        request_ids = [
            ApprovalRequestPayload.model_validate(e.payload).request_id
            for e in ctx["publisher"].envelopes
        ]
        assert request_ids == [_request_id(0), _request_id(1)]
        assert len(ctx["subscribers"]) == 2


# ---------------------------------------------------------------------------
# DISPATCHFMT+ S4 — COACH independent verification (built nothing; drives the
# FULL chain from the DEPLOYED reply shape to the handoff file content).
#
# Fixture rebuilt independently from the in-container
# ``specialist_agent/adapters/result_wrapper.py`` wrap_role_output shape
# (role_id / coach_score / criterion_breakdown LIST of
# {criterion,score,weight,rationale} / detection_findings LIST of
# {pattern,severity,description,location} / role_output). NOT copied from the
# builder's fixture. Distinct sentinels prove role_output (the real document),
# NOT criterion_breakdown (Coach evidence), reaches the PLANNED_HANDOFF doc.
# ---------------------------------------------------------------------------


# The REAL product document — unique sentinel that MUST reach the handoff.
_COACH_ROLE_OUTPUT_DOC: dict[str, Any] = {
    "title": "COACH-DOC voice-first standup bot",
    "problem_statement": "COACH-ROLEOUTPUT-REACHED-HANDOFF-7f3a",
    "user_stories": [{"as_a": "developer", "i_want": "yesterday's commits read aloud"}],
    "acceptance_criteria": ["reads the git log", "speaks via TTS"],
}

# Coach evidence — unique sentinel that MUST NOT be mistaken for the document.
_COACH_CRITERION_BREAKDOWN: list[dict[str, Any]] = [
    {
        "criterion": "COACH-CRITERION-clarity",
        "score": 0.9,
        "weight": 0.5,
        "rationale": "COACH-EVIDENCE-NOT-THE-DOC-b19c",
    },
    {
        "criterion": "COACH-CRITERION-completeness",
        "score": 0.74,
        "weight": 0.5,
        "rationale": "mostly complete",
    },
]

# The deployed wrap_role_output() result block, rebuilt from the container.
_COACH_WRAP_ROLE_OUTPUT: dict[str, Any] = {
    "role_id": "product-owner",
    "coach_score": 0.82,
    "criterion_breakdown": _COACH_CRITERION_BREAKDOWN,
    "detection_findings": [
        {
            "pattern": "vague-scope",
            "severity": "minor",
            "description": "scope could be tighter",
            "location": "problem_statement",
        },
    ],
    "role_output": _COACH_ROLE_OUTPUT_DOC,
}


def _coach_deployed_reply(*, success: bool = True) -> dict[str, Any]:
    """Inner ResultPayload dict the transport adapter forwards to parse_reply.

    Mirrors the deployed router fire-and-forget branch:
    ``ResultPayload(command=..., result=wrap_role_output(...),
    correlation_id=..., success=True)`` with the MessageEnvelope wrapper
    already stripped (D2).
    """
    if success:
        return {
            "command": "greenfield",
            "result": dict(_COACH_WRAP_ROLE_OUTPUT),
            "correlation_id": "c0ffee00c0ffee00c0ffee00c0ffee00",
            "success": True,
        }
    return {
        "command": "greenfield",
        "result": {"error": "COACH-FAILURE-boom-e55d"},
        "correlation_id": "c0ffee00c0ffee00c0ffee00c0ffee00",
        "success": False,
    }


class TestCoachDeployedReplyReachesHandoff:
    """S4 COACH: deployed reply → parse → dispatch → driver → handoff file."""

    def test_parse_reply_carries_role_output_and_coach_fields(self) -> None:
        from forge.dispatch.models import SyncResult
        from forge.dispatch.reply_parser import parse_reply

        outcome = parse_reply(
            _coach_deployed_reply(),
            resolution_id="coach-res-1",
            attempt_no=1,
        )
        assert isinstance(outcome, SyncResult)
        # coach_score sourced from the nested wrap_role_output block.
        assert outcome.coach_score == 0.82
        # criterion_breakdown preserved as the deployed LIST (not coerced).
        assert isinstance(outcome.criterion_breakdown, list)
        assert outcome.criterion_breakdown == _COACH_CRITERION_BREAKDOWN
        assert outcome.detection_findings == _COACH_WRAP_ROLE_OUTPUT["detection_findings"]
        # The REAL document is carried through verbatim, non-empty.
        assert outcome.role_output == _COACH_ROLE_OUTPUT_DOC

    def test_failure_reply_becomes_dispatch_error(self) -> None:
        from forge.dispatch.models import DispatchError
        from forge.dispatch.reply_parser import parse_reply

        outcome = parse_reply(
            _coach_deployed_reply(success=False),
            resolution_id="coach-res-1",
            attempt_no=1,
        )
        assert isinstance(outcome, DispatchError)
        assert "COACH-FAILURE-boom-e55d" in outcome.error_explanation

    @pytest.mark.asyncio
    async def test_real_document_not_coach_evidence_reaches_handoff_file(
        self, store: SqlitePlanningRunStore
    ) -> None:
        # Drive the REAL parser + dispatcher translation, then the REAL driver
        # end-to-end to PLANNED_HANDOFF, and read the handoff file content the
        # git runner captured. The document sentinel MUST be present; the Coach
        # evidence sentinels MUST be absent (M10).
        from forge.dispatch.reply_parser import parse_reply
        from forge.pipeline.dispatchers.specialist import (
            StageDispatchOutcome,
            _translate_outcome,
        )
        from forge.pipeline.stage_taxonomy import StageClass

        sync = parse_reply(
            _coach_deployed_reply(), resolution_id="coach-res-1", attempt_no=1
        )
        stage_result = _translate_outcome(
            outcome=sync,
            stage=StageClass.PRODUCT_OWNER,
            build_id=PLAN_RUN_ID,
            correlation_id=CID,
            entry_id="coach-entry-1",
        )
        assert stage_result.outcome is StageDispatchOutcome.COMPLETED
        assert stage_result.role_output == _COACH_ROLE_OUTPUT_DOC

        _queue_run(store)
        driver, ctx = _make_driver(
            store,
            subscriber_scripts=[[_approve(attempt=0)]],
            po_result=stage_result,
        )

        await driver.drive(CID)

        run = store.get_run(CID)
        assert run is not None
        assert run["state"] == PlanningState.PLANNED_HANDOFF.value

        # The handoff file content the git runner wrote.
        assert len(ctx["git"].calls) == 1
        content = ctx["git"].calls[0]["content"]

        # M10 — the REAL product document reached the handoff file.
        assert "COACH-ROLEOUTPUT-REACHED-HANDOFF-7f3a" in content
        # M10 — Coach evidence (criterion_breakdown) did NOT masquerade as
        # the document. This is the exact regression the pre-fix driver had.
        assert "COACH-EVIDENCE-NOT-THE-DOC-b19c" not in content
        assert "COACH-CRITERION-clarity" not in content


# ---------------------------------------------------------------------------
# Which repository the run lands in (2026-09-05 spec, rules 4 and 6)
# ---------------------------------------------------------------------------


class TestRepositoryIsNamedOutLoud:
    """A run that named no repository still says where it is being built."""

    @staticmethod
    def _config() -> PlanningConfig:
        return PlanningConfig(
            enabled=True,
            default_target_repo="guardkit/api_test",
            target_repo_paths={
                "guardkit/api_test": "/srv/repos/api_test",
                "appmilla/study-tutor": "/srv/repos/study-tutor",
            },
        )

    @pytest.mark.asyncio
    async def test_default_repo_named_in_log(
        self, store: SqlitePlanningRunStore, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The assumed default is logged by name, never assumed silently."""
        driver, _ = _make_driver(store, config=self._config())
        row = {"target_repo": None}

        with caplog.at_level("INFO", logger="forge.planning.driver"):
            resolved = await driver._resolve_repo(
                row, CID, stage_label="feature-spec"
            )

        assert resolved == ("guardkit/api_test", "/srv/repos/api_test")
        assert any(
            "guardkit/api_test" in record.getMessage()
            and "default" in record.getMessage()
            for record in caplog.records
        ), "the log must name the repository that was assumed"

    @pytest.mark.asyncio
    async def test_a_named_repo_is_not_logged_as_an_assumption(
        self, store: SqlitePlanningRunStore, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A sentence that named its repository assumed nothing."""
        driver, _ = _make_driver(store, config=self._config())
        row = {"target_repo": "appmilla/study-tutor"}

        with caplog.at_level("INFO", logger="forge.planning.driver"):
            resolved = await driver._resolve_repo(
                row, CID, stage_label="feature-spec"
            )

        assert resolved == ("appmilla/study-tutor", "/srv/repos/study-tutor")
        assert not any(
            "named no repository" in record.getMessage()
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_a_late_unknown_repo_failure_lists_the_known_names(
        self, store: SqlitePlanningRunStore
    ) -> None:
        """A row naming a repository the config lost fails with the list."""
        _queue_run(store)
        store.transition(
            correlation_id=CID,
            to_state=PlanningState.RUNNING,
            actor_identity="test",
        )
        driver, ctx = _make_driver(store, config=self._config())
        row = {"target_repo": "elsewhere/nowhere"}

        resolved = await driver._resolve_repo(row, CID, stage_label="feature-spec")

        assert resolved is None
        run = store.get_run(CID)
        assert run["state"] == PlanningState.FAILED.value
        assert (
            "known repos: guardkit/api_test, appmilla/study-tutor" in run["error"]
        )

    @pytest.mark.asyncio
    async def test_a_driver_failure_is_still_recorded_as_the_drivers(
        self, store: SqlitePlanningRunStore
    ) -> None:
        """The intake names itself; the driver keeps its own name.

        Both end a run through one piece of code, so the durable row is the
        only thing that says which of them did it.
        """
        _queue_run(store)
        store.transition(
            correlation_id=CID,
            to_state=PlanningState.RUNNING,
            actor_identity="test",
        )
        driver, _ = _make_driver(store, config=self._config())

        await driver._resolve_repo(
            {"target_repo": "elsewhere/nowhere"}, CID, stage_label="feature-spec"
        )

        actors = [
            row["actor_identity"]
            for row in store._connection.execute(
                "SELECT actor_identity FROM planning_run_events "
                "WHERE correlation_id = ? AND stage_label = ?",
                (CID, "feature-spec"),
            )
        ]
        assert actors == ["planning-driver"]

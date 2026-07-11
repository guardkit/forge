"""Driver-level assumption-dialogue revision tests (TASK-SPL003F-001, part 3).

Exercises the revise path wired into :class:`PlanningRunDriver`:

* a ``modified`` disposition re-invokes the PRODUCT_OWNER with the assembled
  EnrichmentBatch (stateless re-invoke), records the cycle's dispositions by
  assumption id (WS4 join), then re-checkpoints and clears on the next
  ``accepted`` cycle → PLANNED_HANDOFF;
* the cap-3 boundary: a revision that would open a 4th cycle escalates to Rich
  instead (durable ``expected_approver`` re-target).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from nats_core.events import ApprovalResponsePayload, AssumptionDisposition

from forge.gating.identity import derive_request_id
from forge.lifecycle import migrations
from forge.planning.driver import PlanningDriverDeps, PlanningRunDriver
from forge.planning.gate_adapters import build_planning_gate_adapters
from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.states import PlanningState

CID = "revcid"
PLAN_RUN_ID = f"plan-{CID}"
ORIGINATOR = "U03QR8WKT29"
ESCALATOR = "U0ESCALATE"


def _clock() -> datetime:
    return datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)


def _assumptions(tag: str) -> list[dict[str, Any]]:
    return [
        {
            "id": "A1",
            "text": f"REST API ({tag})",
            "confidence": "medium",
            "basis": "default",
        },
        {
            "id": "A2",
            "text": f"Postgres ({tag})",
            "confidence": "low",
            "basis": "durable",
        },
    ]


def _po_result(assumptions: list[dict[str, Any]]) -> Any:
    return SimpleNamespace(
        outcome=SimpleNamespace(value="completed"),
        coach_score=0.9,
        criterion_breakdown=[
            {"criterion": "clarity", "score": 0.9, "weight": 1.0, "rationale": "ok"},
        ],
        detection_findings=(),
        role_output={"title": "docs", "problem_statement": "ship a thing"},
        reason=None,
        assumptions=assumptions,
    )


def _request_id(attempt: int = 0) -> str:
    return derive_request_id(
        build_id=PLAN_RUN_ID, stage_label="product_docs", attempt_count=attempt
    )


def _resp(
    decision: str,
    dispositions: list[AssumptionDisposition],
    *,
    attempt: int = 0,
) -> ApprovalResponsePayload:
    # The response echoes the request_id of the cycle it answers; each dialogue
    # cycle gets a DISTINCT attempt so the driver's stale-round guard rejects a
    # redelivered prior-cycle response.
    return ApprovalResponsePayload(
        request_id=_request_id(attempt),
        decision=decision,
        decided_by=ORIGINATOR,
        dispositions=dispositions,
    )


class _ScriptedSubscriber:
    def __init__(self, script: list[Any], armed: asyncio.Event | None) -> None:
        self._script = script
        self._armed = armed

    async def await_response(self, build_id: str, **kwargs: Any) -> Any:
        if self._armed is not None:
            self._armed.set()
        return self._script.pop(0) if self._script else None


class _Publisher:
    def __init__(self) -> None:
        self.envelopes: list[Any] = []

    async def publish_request(self, envelope: Any) -> None:
        self.envelopes.append(envelope)


class _Provider:
    """Returns the LATEST PO output's assumptions (message-as-state)."""

    def __init__(self, driver_ctx: dict[str, Any]) -> None:
        self._ctx = driver_ctx

    async def get_summary_for_approval(
        self, *, plan_run_id: str, stage_label: str
    ) -> dict[str, Any]:
        return {"checkpoint": stage_label, "assumptions": self._ctx["last_assumptions"]}


class _GitRunner:
    async def prepare_branch_and_write(self, *a: Any, **k: Any) -> Any:
        return SimpleNamespace(
            status="success", operation="w", sha="deadbeef", exit_code=0
        )


@pytest.fixture
def store(tmp_path: Path) -> SqlitePlanningRunStore:
    conn = sqlite3.connect(str(tmp_path / "rev.db"))
    conn.row_factory = sqlite3.Row
    migrations.apply_at_boot(conn)
    return SqlitePlanningRunStore(conn)


def _make_driver(
    store: SqlitePlanningRunStore, *, subscriber_scripts: list[list[Any]]
) -> tuple[PlanningRunDriver, dict[str, Any]]:
    from forge.config.models import PlanningConfig

    repository, state_machine = build_planning_gate_adapters(store, clock=_clock)
    scripts = list(subscriber_scripts)
    ctx: dict[str, Any] = {
        "po_calls": [],
        "enrichments": [],
        "last_assumptions": _assumptions("cycle1"),
        "po_result_queue": [_po_result(_assumptions("cycle2"))],
    }

    def subscriber_factory(expected_approver: Any, armed: Any) -> _ScriptedSubscriber:
        return _ScriptedSubscriber(scripts.pop(0) if scripts else [], armed)

    async def dispatch_po(
        *,
        plan_run_id: str,
        correlation_id: str,
        enrichment: dict[str, Any] | None = None,
    ) -> Any:
        ctx["po_calls"].append(correlation_id)
        if enrichment is not None:
            ctx["enrichments"].append(enrichment)
        if ctx["po_result_queue"]:
            result = ctx["po_result_queue"].pop(0)
        else:
            result = _po_result(ctx["last_assumptions"])
        ctx["last_assumptions"] = list(result.assumptions)
        return result

    cfg = PlanningConfig(
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
            approval_publisher=_Publisher(),
            subscriber_factory=subscriber_factory,
            dispatch_product_owner=dispatch_po,
            second_opinion_provider=_Provider(ctx),
            git_runner=_GitRunner(),
            planning_config=cfg,
            clock=_clock,
            publish_notification=None,
        )
    )
    return driver, ctx


def _queue(store: SqlitePlanningRunStore) -> None:
    store.record_queued(
        correlation_id=CID,
        originating_user=ORIGINATOR,
        expected_approver=ORIGINATOR,
        request_text="build a widget",
        triggered_by="jarvis",
        target_repo="appmilla/widgets",
        parent_request_id="1700000000.000100",
    )


@pytest.mark.asyncio
async def test_modified_reinvokes_po_then_accepted_clears_to_handoff(store):
    _queue(store)
    modified = _resp(
        "approve",
        [
            AssumptionDisposition(assumption_id="A1", disposition="accepted"),
            AssumptionDisposition(
                assumption_id="A2", disposition="modified", edit_delta="Use SQLite."
            ),
        ],
    )
    # Cycle 2's response answers the cycle-2 request (attempt 1 — the revise
    # re-checkpoint bumps the request_id).
    accepted = _resp(
        "approve",
        [
            AssumptionDisposition(assumption_id="A1", disposition="accepted"),
            AssumptionDisposition(assumption_id="A2", disposition="accepted"),
        ],
        attempt=1,
    )
    driver, ctx = _make_driver(store, subscriber_scripts=[[modified], [accepted]])

    await driver.drive(CID)

    run = store.get_run(CID)
    assert run is not None
    assert run["state"] == PlanningState.PLANNED_HANDOFF.value

    # Each dialogue cycle published a DISTINCT approval request_id — the
    # stale-round guard + jarvis JNB-103 dedup depend on this. The cycle-2
    # re-checkpoint bumped the pending id to attempt 1 (was attempt 0).
    assert run["pending_approval_request_id"] == _request_id(1)

    # PO dispatched twice: initial + one revision re-invoke.
    assert len(ctx["po_calls"]) == 2
    # The re-invoke carried the assembled EnrichmentBatch with the edit_delta.
    assert len(ctx["enrichments"]) == 1
    batch = ctx["enrichments"][0]
    assert batch["kind"] == "enrichment_batch"
    assert batch["cycle"] == 2
    a2 = next(r for r in batch["revisions"] if r["assumption_id"] == "A2")
    assert a2["disposition"] == "modified"
    assert a2["edit_delta"] == "Use SQLite."

    # The cycle's dispositions were recorded keyed by assumption id (WS4 join).
    events = store.list_events(CID)
    dialogue = [e for e in events if e["stage_label"] == "planning-dialogue"]
    assert len(dialogue) == 2  # one per decided cycle
    first = json.loads(dialogue[0]["details_json"])
    assert first["dispositions"]["A2"]["disposition"] == "modified"
    assert first["dispositions"]["A2"]["edit_delta"] == "Use SQLite."
    # A revision event increments the durable dialogue cycle.
    assert any(e["stage_label"] == "planning-revision" for e in events)


@pytest.mark.asyncio
async def test_cap_three_escalates_instead_of_a_fourth_cycle(store):
    """A revision decided at cycle 3 escalates to Rich (durable re-target)."""
    _queue(store)
    store.transition(
        correlation_id=CID,
        to_state=PlanningState.RUNNING,
        actor_identity="test",
    )
    # Two prior revisions already recorded → the run is on dialogue cycle 3.
    for _ in range(2):
        store._record_event(
            correlation_id=CID,
            stage_label="planning-revision",
            status="REVISION",
            actor_identity=ORIGINATOR,
            details_json=json.dumps({"cycle": 0}),
        )
    # Pause the run so escalation's CAS (PAUSED → re-target) applies.
    store.transition(
        correlation_id=CID,
        to_state=PlanningState.PAUSED,
        actor_identity="test",
    )
    store.update_escalation(correlation_id=CID, paused_at=_clock().isoformat())

    driver, _ = _make_driver(store, subscriber_scripts=[])
    modified = _resp(
        "approve",
        [
            AssumptionDisposition(
                assumption_id="A2", disposition="modified", edit_delta="x"
            )
        ],
    )

    handled = await driver._handle_revision(CID, PLAN_RUN_ID, modified)

    assert handled == "escalated"
    run = store.get_run(CID)
    assert run["expected_approver"] == ESCALATOR  # durable re-target to Rich
    assert any(
        e["stage_label"] == "planning-escalation" for e in store.list_events(CID)
    )


@pytest.mark.asyncio
async def test_cap_escalation_envelope_carries_the_assumptions(store):
    """The escalated approver must not decide blind — the re-published escalated
    envelope carries the surfaced assumptions (review finding)."""
    from nats_core.events import ApprovalRequestPayload

    from forge.planning.escalation import EscalationPolicy, escalate_planning_run

    _queue(store)
    store.transition(
        correlation_id=CID, to_state=PlanningState.RUNNING, actor_identity="t"
    )
    # A recorded PO output carrying assumptions (the dialogue's subject).
    store._record_event(
        correlation_id=CID,
        stage_label="product_owner",
        status="approved",
        actor_identity="planning-driver",
        details_json=json.dumps({"po_output": {"assumptions": _assumptions("live")}}),
    )
    store.transition(
        correlation_id=CID, to_state=PlanningState.PAUSED, actor_identity="t"
    )
    store.update_escalation(correlation_id=CID, paused_at=_clock().isoformat())

    publisher = _Publisher()
    await escalate_planning_run(
        store=store,
        correlation_id=CID,
        policy=EscalationPolicy(
            originator_wait_seconds=300,
            escalated_wait_seconds=1800,
            escalation_approver=ESCALATOR,
            defer_cap=3,
        ),
        clock=_clock,
        publisher=publisher,
        plan_run_id=PLAN_RUN_ID,
        feature_id=PLAN_RUN_ID,
    )

    assert len(publisher.envelopes) == 1
    details = ApprovalRequestPayload.model_validate(
        publisher.envelopes[0].payload
    ).details
    assert details["checkpoint_type"] == "product_docs_escalated"
    ids = {a["id"] for a in details["summary"]["assumptions"]}
    assert ids == {"A1", "A2"}

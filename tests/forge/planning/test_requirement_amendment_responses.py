"""D-IR1/D-IR3: real first-winner and no-write producer boundaries."""

import asyncio
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from nats_core.envelope import EventType, MessageEnvelope
from nats_core.events import ApprovalResponsePayload

from forge.adapters.nats.approval_subscriber import (
    ApprovalSubscriber,
    ApprovalSubscriberDeps,
)
from forge.config.models import ApprovalConfig
from forge.planning.requirement_amendment_store import (
    accept_response,
    admit,
    freeze_final_plan,
    project_approval,
    validate_response_identity,
)
from forge.planning.requirement_amendments import AmendmentRefused, canonical
from tests.forge.planning.test_requirement_amendments import NOW, DOC, git
from tests.forge.planning.test_requirement_amendments import planned as _planned_fixture

planned = _planned_fixture


def response(x, decision, request="request-1"):
    payload = ApprovalResponsePayload(
        request_id=request,
        decision=decision,
        decided_by="owner",
        decided_at=NOW,
    )
    return MessageEnvelope(
        source_id="owner",
        event_type=EventType.APPROVAL_RESPONSE,
        correlation_id="amendment-request",
        payload=payload.model_dump(mode="json"),
    )


def subscriber(x, *, active=True):
    consumer = ApprovalSubscriber(
        ApprovalSubscriberDeps(
            nats_client=SimpleNamespace(),
            config=ApprovalConfig(),
            expected_approver="owner",
        )
    )
    consumer.amendment_identity_validator = (
        lambda bid, env, payload, expected, additional: validate_response_identity(
            x.cx,
            bid,
            env,
            payload,
            expected,
            additional,
        )
    )
    consumer.amendment_validator = lambda bid, env, payload, expected, additional: (
        accept_response(
            x.cx,
            bid,
            env,
            payload,
            expected,
            additional,
        )
    )
    if active:
        consumer._queues[x.bid] = asyncio.Queue()
    return consumer


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["reject", "defer", "approve"])
@pytest.mark.parametrize("replay", ["same", "expired", "restart", "new_waiter"])
async def test_first_gate_decision_remains_authoritative(planned, first, replay):
    x = planned
    project_approval(x.cx, x.envelope)
    consumer = subscriber(x)
    await consumer._on_envelope(build_id=x.bid, envelope=response(x, first))
    assert (await consumer._queues[x.bid].get()).decision == first
    if replay == "restart":
        consumer = subscriber(x)
    elif replay == "new_waiter":
        consumer._queues[x.bid] = asyncio.Queue()
    elif replay == "expired":
        consumer._dedup["request-1"] = -1
    for later in ("approve", "reject", "defer"):
        await consumer._on_envelope(build_id=x.bid, envelope=response(x, later))
    if replay in ("restart", "new_waiter"):
        assert consumer._queues[x.bid].qsize() == 1
        assert (await consumer._queues[x.bid].get()).decision == first
    else:
        assert consumer._queues[x.bid].empty()
    winner = x.cx.execute(
        "SELECT * FROM build_requirement_amendment_responses"
    ).fetchone()
    assert winner["decision"] == first
    if first == "approve":
        source = await admit(x.cx, x.bid, x.runner, str(x.repo))
        assert git(x.repo, "rev-parse", source + "^1") == x.plan
        assert git(x.repo, "diff", "--name-only", x.plan, source) == DOC
        assert await admit(x.cx, x.bid, x.runner, str(x.repo)) == source
    else:
        with pytest.raises(AmendmentRefused):
            await admit(x.cx, x.bid, x.runner, str(x.repo))
        assert git(x.repo, "rev-parse", "planning/amendment") == x.plan
        assert x.pool.read_source_commit(x.bid) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("inactive", ["no_waiter", "cancelled", "stale_pending"])
async def test_inactive_response_grants_nothing(planned, inactive):
    x = planned
    project_approval(x.cx, x.envelope)
    consumer = subscriber(x, active=inactive != "no_waiter")
    if inactive == "cancelled":
        x.cx.execute("UPDATE builds SET status='CANCELLED' WHERE build_id=?", (x.bid,))
    elif inactive == "stale_pending":
        x.cx.execute(
            "UPDATE builds SET pending_approval_request_id='next' WHERE build_id=?",
            (x.bid,),
        )
    x.cx.commit()
    await consumer._on_envelope(build_id=x.bid, envelope=response(x, "approve"))
    assert (
        x.cx.execute(
            "SELECT count(*) FROM build_requirement_amendment_responses"
        ).fetchone()[0]
        == 0
    )
    with pytest.raises(AmendmentRefused):
        await admit(x.cx, x.bid, x.runner, str(x.repo))
    assert git(x.repo, "rev-parse", "planning/amendment") == x.plan


@pytest.mark.asyncio
async def test_defer_rearm_allows_only_new_current_request(planned):
    x = planned
    original = project_approval(x.cx, x.envelope)
    consumer = subscriber(x)
    await consumer._on_envelope(build_id=x.bid, envelope=response(x, "defer"))
    assert (await consumer._queues[x.bid].get()).decision == "defer"
    x.cx.execute(
        "UPDATE builds SET pending_approval_request_id='request-2' WHERE build_id=?",
        (x.bid,),
    )
    x.cx.commit()
    new = project_approval(
        x.cx,
        x.envelope.model_copy(
            update={
                "payload": {**x.envelope.payload, "request_id": "request-2"},
            }
        ),
    )
    before = original.payload["details"]["requirement_amendment"]
    after = new.payload["details"]["requirement_amendment"]
    assert {**before, "request_id": "request-2"} == after
    consumer = subscriber(x)
    await consumer._on_envelope(build_id=x.bid, envelope=response(x, "approve"))
    assert consumer._queues[x.bid].empty()
    await consumer._on_envelope(
        build_id=x.bid, envelope=response(x, "approve", "request-2")
    )
    assert (await consumer._queues[x.bid].get()).request_id == "request-2"
    assert await admit(x.cx, x.bid, x.runner, str(x.repo))
    assert [
        row[0]
        for row in x.cx.execute(
            "SELECT decision FROM build_requirement_amendment_responses ORDER BY request_id"
        )
    ] == ["defer", "approve"]


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["approve", "defer", "reject"])
async def test_concurrent_subscriber_responses_deliver_one_winner(planned, first):
    x = planned
    project_approval(x.cx, x.envelope)
    consumer = subscriber(x)
    await asyncio.gather(
        *[
            consumer._on_envelope(build_id=x.bid, envelope=response(x, decision))
            for decision in (first, "approve", "reject", "defer")
        ]
    )
    assert consumer._queues[x.bid].qsize() == 1
    delivered = await consumer._queues[x.bid].get()
    winner = x.cx.execute(
        "SELECT decision FROM build_requirement_amendment_responses"
    ).fetchone()[0]
    assert delivered.decision == winner == first
    if first != "approve":
        with pytest.raises(AmendmentRefused):
            await admit(x.cx, x.bid, x.runner, str(x.repo))


@pytest.mark.asyncio
async def test_independent_connections_claim_only_one_response(planned):
    x = planned
    project_approval(x.cx, x.envelope)
    barrier = threading.Barrier(2)

    def claim(decision):
        cx = sqlite3.connect(x.pool.db_path, timeout=10)
        cx.row_factory = sqlite3.Row
        try:
            envelope = response(x, decision)
            payload = ApprovalResponsePayload.model_validate(envelope.payload)
            barrier.wait(timeout=5)
            return decision, accept_response(cx, x.bid, envelope, payload, "owner")
        finally:
            cx.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(claim, ["defer", "approve"]))
    assert sum(bool(won) for _, won in results) == 1
    winner = next(decision for decision, won in results if won)
    stored = x.cx.execute(
        "SELECT * FROM build_requirement_amendment_responses"
    ).fetchone()
    assert stored["decision"] == winner
    if winner == "defer":
        with pytest.raises(AmendmentRefused):
            await admit(x.cx, x.bid, x.runner, str(x.repo))
    else:
        assert await admit(x.cx, x.bid, x.runner, str(x.repo))


@pytest.mark.asyncio
@pytest.mark.parametrize("planned", ["absent"], indirect=True)
async def test_real_no_task_legs_freeze_absence_and_launch_ordinary_source(planned):
    from forge.cli._serve_deps import (
        _build_resume_launcher,
        build_requirement_amendment_admission,
    )
    from forge.planning.driver import _QA_PASS_BARS_STAGE, _QA_FEATURE_GATE_STAGE
    from tests.forge.planning.test_driver_target_terminal import (
        _make_driver,
        _AUTHLESS_SEED_YAML,
    )
    operator = pytest.importorskip("one_sentence", reason="a sibling repository this test reads (one_sentence) is not checked out here (the GitHub workflow checks out forge, nats-core and guardkit only)")

    x = planned
    # The old fixture's synthetic no-write receipts are explicitly removed.
    build = dict(
        x.cx.execute("SELECT * FROM builds WHERE build_id=?", (x.bid,)).fetchone()
    )
    x.cx.execute("DELETE FROM builds WHERE build_id=?", (x.bid,))
    x.cx.execute(
        "DELETE FROM planning_run_events WHERE stage_label IN (?,?)",
        (_QA_PASS_BARS_STAGE, _QA_FEATURE_GATE_STAGE),
    )
    plan = json.loads(
        x.cx.execute(
            "SELECT proposal_json FROM planning_requirement_amendments"
        ).fetchone()[0]
    )
    plan["qa_receipts"] = None
    plan.pop("planning_branch")
    x.cx.execute(
        "UPDATE planning_requirement_amendments SET proposal_json=?", (canonical(plan),)
    )
    x.cx.commit()
    x.store._record_event(
        correlation_id="amendment-request",
        stage_label="feature-spec",
        status="approved",
        actor_identity="planning-driver",
        details_json=json.dumps({"pass_bar_seed": _AUTHLESS_SEED_YAML}),
    )
    harness = _make_driver(x.store, git_runner=x.runner, repo_path=str(x.repo))
    harness.driver._deps.planning_config.target_repo_paths["example/project"] = str(
        x.repo
    )
    assert await harness.driver._register_pass_bars_leg(
        x.store.get_run("amendment-request"), "amendment-request"
    )
    assert await harness.driver._register_feature_gate_leg(
        x.store.get_run("amendment-request"), "amendment-request"
    )
    for field, value in (
        ("skipped", False),
        ("reason_code", ""),
        ("requirement_amendment_qa", None),
    ):
        x.cx.execute("SAVEPOINT malformed_skip")
        row = x.cx.execute(
            "SELECT id,details_json FROM planning_run_events WHERE stage_label=?",
            (_QA_PASS_BARS_STAGE,),
        ).fetchone()
        broken = json.loads(row["details_json"])
        broken[field] = value
        x.cx.execute(
            "UPDATE planning_run_events SET details_json=? WHERE id=?",
            (json.dumps(broken), row["id"]),
        )
        with pytest.raises(AmendmentRefused):
            await freeze_final_plan(
                x.cx,
                "amendment-request",
                x.runner,
                str(x.repo),
                pass_stage=_QA_PASS_BARS_STAGE,
                gate_stage=_QA_FEATURE_GATE_STAGE,
            )
        x.cx.execute("ROLLBACK TO malformed_skip")
        x.cx.execute("RELEASE malformed_skip")
    final = await freeze_final_plan(
        x.cx,
        "amendment-request",
        x.runner,
        str(x.repo),
        pass_stage=_QA_PASS_BARS_STAGE,
        gate_stage=_QA_FEATURE_GATE_STAGE,
    )
    assert final["plan_commit"] == x.plan and final["amendment_state"] == "absent"
    assert [r["receipt"]["reason_code"] for r in final["qa_receipts"]] == [
        "no_tasks",
        "no_pass_bars",
    ]
    build["pending_approval_request_id"] = x.bid + ":autobuild:0"
    x.cx.execute(
        "INSERT INTO builds("
        + ",".join(build)
        + ") VALUES ("
        + ",".join("?" for _ in build)
        + ")",
        tuple(build.values()),
    )
    x.cx.commit()
    card = project_approval(
        x.cx,
        x.envelope.model_copy(
            update={
                "payload": {
                    **x.envelope.payload,
                    "request_id": build["pending_approval_request_id"],
                }
            }
        ),
    )
    run = operator.Run(
        arm="fixture",
        approver_fallback="owner",
        requirement_amendment_paths=(DOC,),
        requirement_amendment_repo="example/project",
        requirement_amendment_request="Update active runtime",
    )
    run.correlation_id = "amendment-request"
    run.correlations.add(run.correlation_id)
    answer = operator.answer_for(
        run, "agents.approval.forge." + x.bid, card.model_dump(mode="json")
    )
    assert answer.decision == "approve"
    consumer = subscriber(x)
    await consumer._on_envelope(
        build_id=x.bid, envelope=response(x, "approve", answer.request_id)
    )
    assert (await consumer._queues[x.bid].get()).decision == "approve"
    launched = []

    class Starter:
        async def astart_async_task(self, **kwargs):
            launched.append(kwargs["context"])
            return "ordinary-task"

    launcher = _build_resume_launcher(
        SimpleNamespace(build_for=lambda **kw: []),
        MagicMock(),
        MagicMock(),
        None,
        Starter(),
        source_commit_reader=x.pool.read_source_commit,
        amendment_admitter=build_requirement_amendment_admission(
            x.pool,
            SimpleNamespace(
                planning=SimpleNamespace(
                    target_repo_paths={"example/project": str(x.repo)}
                )
            ),
            git_runner=x.runner,
        ),
    )
    await launcher(
        build_id=x.bid,
        feature_id=x.feature,
        correlation_id="amendment-request",
        branch="planning/amendment",
        repo="example/project",
    )
    assert len(launched) == 1 and not launched[0].get("source_commit")
    assert x.pool.read_source_commit(x.bid) is None
    assert git(x.repo, "rev-parse", "planning/amendment") == x.plan


@pytest.mark.asyncio
@pytest.mark.parametrize("planned", [None, "absent"], indirect=True)
async def test_real_restart_recovers_claimed_but_undelivered_response(
    planned, monkeypatch
):
    from tests.forge.planning.test_requirement_amendments import (
        test_real_gate_restart_reuses_frozen_card_and_common_launcher,
    )

    await test_real_gate_restart_reuses_frozen_card_and_common_launcher(
        planned, monkeypatch, crash_after_response=True
    )


@pytest.mark.asyncio
async def test_first_response_and_authority_commit_atomically(planned):
    x = planned
    project_approval(x.cx, x.envelope)
    consumer = subscriber(x)

    def fail_authority_write(action, table, column, database, trigger):
        if (
            action == sqlite3.SQLITE_UPDATE
            and table == "build_requirement_amendments"
            and column == "accepted_request_id"
        ):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    x.cx.set_authorizer(fail_authority_write)
    try:
        await consumer._on_envelope(build_id=x.bid, envelope=response(x, "approve"))
    finally:
        x.cx.set_authorizer(None)
    assert consumer._queues[x.bid].empty()
    assert not x.cx.in_transaction
    assert (
        x.cx.execute(
            "SELECT count(*) FROM build_requirement_amendment_responses"
        ).fetchone()[0]
        == 0
    )
    assert (
        x.cx.execute(
            "SELECT accepted_request_id FROM build_requirement_amendments"
        ).fetchone()[0]
        is None
    )
    await consumer._on_envelope(build_id=x.bid, envelope=response(x, "approve"))
    assert (await consumer._queues[x.bid].get()).decision == "approve"
    assert await admit(x.cx, x.bid, x.runner, str(x.repo))

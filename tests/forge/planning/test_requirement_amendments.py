"""Offline producer/ledger/card/consumer/admission checks for exact amendments."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from click.testing import CliRunner
from nats_core.envelope import EventType, MessageEnvelope
from nats_core.events import ApprovalResponsePayload, BuildQueuedPayload

from forge.adapters.git.planning_runner import WorktreeGitRunner
from forge.adapters.nats.approval_publisher import ApprovalPublisher
from forge.adapters.sqlite.connect import connect_writer
from forge.cli.status import status_cmd
from forge.cli._serve_gate_activation import _MirroredApprovalPublisher
from forge.lifecycle.migrations import apply_at_boot
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.pipeline import BuildContext
from forge.planning.requirement_amendments import (
    AmendmentRefused,
    FIELD,
    candidate,
    canonical,
    paths,
    sha,
    validate_proposal,
    verify_binding,
    summary,
)
from forge.planning.requirement_amendment_store import (
    accept_response,
    admit,
    binding_for_build,
    project_approval,
    record_plan,
)
from forge.planning.run_store import SqlitePlanningRunStore

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
DOC = "requirements/runtime.contract"
OLD = "Active runtime: version 1\nKeep every ordinary behavioral check enabled.\n"
NEW = "Active runtime: version 2\nKeep every ordinary behavioral check enabled.\n"


def git(repo, *args):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()


def proposal(base, path=DOC, after=NEW):
    return {
        "schema_version": 1,
        "base_commit": base,
        "changes": [
            {
                "path": path,
                "before_sha256": sha(OLD),
                "after_text": after,
                "reason": "Update the requested active runtime only.",
            }
        ],
    }


@pytest_asyncio.fixture
async def planned(tmp_path, request):
    repo = tmp_path / "project"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    files = {
        DOC: OLD,
        "requirements/sibling.contract": "Keep sibling\n",
        "research/history.txt": "Recorded runtime version 1\n",
        "vendor/attestation.txt": "Upstream runtime version 1\n",
        "checks/pass.txt": "Required ordinary check\n",
        ".guardkit/config.yaml": "autobuild:\n  player:\n    required_documents: [requirements/runtime.contract]\n    protected_paths: [requirements, checks, .guardkit/config.yaml]\n",
    }
    selected = [DOC]
    changes = None
    if getattr(request, "param", None) == "maximum":
        selected = [
            f"requirements/{i:02}-" + ("scope-é<&`" * 12) + ".contract"
            for i in range(32)
        ]
        before = "é" * 1023 + "a"
        after = "é" * 1023 + "bbb"
        files.update({path: before for path in selected})
        import yaml

        files[".guardkit/config.yaml"] = yaml.safe_dump(
            {
                "autobuild": {
                    "player": {
                        "required_documents": selected,
                        "protected_paths": [
                            "requirements",
                            "checks",
                            ".guardkit/config.yaml",
                        ],
                    }
                }
            }
        )
        changes = [
            {
                "path": path,
                "before_sha256": sha(before),
                "after_text": after,
                "reason": "Requested exact change",
            }
            for path in selected
        ]
    for name, text in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "branch", "planning/amendment")
    runner = WorktreeGitRunner(worktrees_root=tmp_path / "worktrees")
    value = proposal(base)
    if changes is not None:
        value["changes"] = changes
    raw = canonical(value)
    feature = "FEAT-AB12"
    artifact = f".guardkit/features/{feature}.requirement-amendments.json"
    plan_files = {
        artifact: raw,
        f".guardkit/features/{feature}.yaml": f"id: {feature}\ntasks: []\n",
    }
    absent = getattr(request, "param", None) == "absent"
    if absent:
        plan_files.pop(artifact)
    result = await runner.prepare_branch_and_write_tree(
        str(repo),
        "planning/amendment",
        plan_files,
        "Plan [skip ci]",
        expected_head=base,
    )
    assert result.status == "success", result.stderr
    p = result.sha
    cx = connect_writer(tmp_path / "ledger.db")
    apply_at_boot(cx)
    store = SqlitePlanningRunStore(cx, target_terminal_enabled=True)
    store.record_queued(
        "amendment-request",
        "owner",
        "owner",
        "Update active runtime",
        "cli",
        target_repo="example/project",
        requirement_amendment_paths=tuple(selected),
    )
    store.record_start_point(
        "amendment-request", start_commit=base, target_branch="main"
    )
    if absent:
        from forge.planning.requirement_amendments import validate_absence

        checked = await validate_absence(
            runner=runner,
            repo_path=str(repo),
            scope=selected,
            base=base,
            plan=p,
            feature_id=feature,
        )
    else:
        checked = await validate_proposal(
            runner=runner,
            repo_path=str(repo),
            raw=raw,
            scope=selected,
            base=base,
            plan=p,
            feature_id=feature,
        )
    from forge.planning.driver import PlanningRunDriver

    review = {
        "schema_version": 1,
        "decision": "approved",
        "criterion": "request_traceability",
        "criterion_score": 1.0,
        "coach_verdict": "GOOD",
        "artifact_identity": PlanningRunDriver._plan_artifact_identity(plan_files),
        "reviewed_after_rewrite": False,
    }
    record_plan(cx, "amendment-request", checked, review)
    from forge.planning.requirement_amendment_store import skipped_qa, freeze_final_plan
    from forge.planning.driver import _QA_PASS_BARS_STAGE, _QA_FEATURE_GATE_STAGE

    store._record_event(
        correlation_id="amendment-request",
        stage_label="feature-plan",
        status="approved",
        actor_identity="planning-driver",
        details_json=json.dumps(
            {
                "sha": p,
                "feature_id": feature,
                "target_repo": "example/project",
                "branch": "planning/amendment",
                "plan_files": sorted(plan_files),
                "semantic_review": review,
            }
        ),
    )
    for stage in (_QA_PASS_BARS_STAGE, _QA_FEATURE_GATE_STAGE):
        store._record_event(
            correlation_id="amendment-request",
            stage_label=stage,
            status="approved",
            actor_identity="planning-driver",
            details_json=json.dumps(
                {
                    "feature_id": feature,
                    "skipped": True,
                    "reason_code": "fixture-no-registration",
                    "requirement_amendment_qa": skipped_qa(
                        cx, "amendment-request", "planning/amendment", p
                    ),
                }
            ),
        )
    await freeze_final_plan(
        cx,
        "amendment-request",
        runner,
        str(repo),
        pass_stage=_QA_PASS_BARS_STAGE,
        gate_stage=_QA_FEATURE_GATE_STAGE,
    )
    pool = SqliteLifecyclePersistence(connection=cx)
    payload = BuildQueuedPayload(
        feature_id=feature,
        repo="example/project",
        branch="planning/amendment",
        feature_yaml_path=str(repo / f".guardkit/features/{feature}.yaml"),
        triggered_by="forge-internal",
        originating_user="owner",
        correlation_id="amendment-request",
        requested_at=NOW,
        queued_at=NOW,
        mode="mode-b",
    )
    bid = pool.record_pending_build(payload)
    cx.execute(
        "UPDATE builds SET status='PAUSED', pending_approval_request_id='request-1' WHERE build_id=?",
        (bid,),
    )
    cx.commit()
    envelope = MessageEnvelope(
        source_id="forge",
        event_type=EventType.APPROVAL_REQUEST,
        correlation_id="amendment-request",
        payload={
            "request_id": "request-1",
            "agent_id": "forge",
            "action_description": "Build approval",
            "risk_level": "medium",
            "details": {
                "build_id": bid,
                "feature_id": feature,
                "stage_label": "autobuild",
                "gate_mode": "MANDATORY_HUMAN_APPROVAL",
                "rationale": "Approve build",
                "resume_options": ["approve", "reject", "defer"],
            },
        },
    )
    yield SimpleNamespace(
        repo=repo,
        runner=runner,
        cx=cx,
        pool=pool,
        base=base,
        plan=p,
        bid=bid,
        feature=feature,
        envelope=envelope,
        files=files,
        artifact=artifact,
        store=store,
    )
    cx.close()


def approve(x, envelope=None):
    projected = project_approval(x.cx, x.envelope)
    response = ApprovalResponsePayload(
        request_id="request-1", decision="approve", decided_by="owner", decided_at=NOW
    )
    assert accept_response(x.cx, x.bid, envelope or projected, response, "owner")
    return projected


@pytest.mark.asyncio
async def test_approval_exact_diff_admission_and_protection(
    planned, tmp_path, monkeypatch
):
    x = planned
    raw = approve(x)
    binding = raw.payload["details"]["requirement_amendment"]
    assert binding_for_build(x.cx, x.bid) == binding
    cli = CliRunner().invoke(
        status_cmd,
        [
            x.bid,
            "--requirement-amendment-diff",
            "--json",
            "--db-path",
            str(x.pool.db_path),
        ],
    )
    assert cli.exit_code == 0, cli.output
    assert json.loads(cli.output) == binding
    assert (
        CliRunner()
        .invoke(
            status_cmd,
            [
                x.feature,
                "--requirement-amendment-diff",
                "--db-path",
                str(x.pool.db_path),
            ],
        )
        .exit_code
        != 0
    )
    a = await admit(x.cx, x.bid, x.runner, str(x.repo))
    assert a != x.plan and git(x.repo, "rev-parse", a + "^") == x.plan
    assert git(x.repo, "diff", "--name-only", x.plan, a) == DOC
    assert git(x.repo, "show", a + ":" + DOC) == NEW.rstrip()
    assert await admit(x.cx, x.bid, x.runner, str(x.repo)) == a
    # The real GuardKit restoration and index/branch correction reference A.
    import importlib.util
    import sys
    import types

    package = types.ModuleType("guardkit.orchestrator")
    package.__path__ = [
        str(Path(importlib.util.find_spec("guardkit").origin).parent / "orchestrator")
    ]
    monkeypatch.setitem(sys.modules, "guardkit.orchestrator", package)
    from guardkit.orchestrator.protected_paths import (
        load_protected_reference,
        restore_protected_paths,
        hold_protected_index,
    )
    from guardkit.orchestrator.harness.selector import load_project_documents_at_commit
    from guardkitfactory.harness.backend_config import PathConfinedBackend

    work = tmp_path / "player"
    git(x.repo, "worktree", "add", "-q", "-b", "player", str(work), a)
    ref = load_protected_reference(work, a, "player")
    snapshot = load_project_documents_at_commit(work, a)
    assert any(document.text == NEW for document in snapshot.documents)
    backend = PathConfinedBackend(
        SimpleNamespace(), [work], protected_paths=[work / "requirements"]
    )
    assert backend.write(str(work / DOC), "forbidden").error
    assert backend.edit(str(work / DOC), "version 2", "version 1").error
    (work / DOC).write_text("shell tried changing the approved requirement\n")
    (work / "requirements/sibling.contract").unlink()
    (work / "requirements/extra.txt").write_text("unauthorized sibling\n")
    git(work, "add", "-A")
    assert restore_protected_paths(work, ref) is not None
    hold_protected_index(work, ref)
    assert (work / DOC).read_text() == NEW
    assert (work / "requirements/sibling.contract").read_text() == x.files[
        "requirements/sibling.contract"
    ]
    assert not (work / "requirements/extra.txt").exists()
    assert git(work, "diff", "--cached", "--name-only") == ""
    assert (
        git(x.repo, "show", a + ":research/history.txt")
        == x.files["research/history.txt"].rstrip()
    )
    assert (
        git(x.repo, "show", a + ":vendor/attestation.txt")
        == x.files["vendor/attestation.txt"].rstrip()
    )


@pytest.mark.asyncio
async def test_no_approval_and_mismatched_identities_never_apply(planned):
    x = planned
    with pytest.raises(AmendmentRefused):
        await admit(x.cx, x.bid, x.runner, str(x.repo))
    project_approval(x.cx, x.envelope)
    for who, req, corr, decision in [
        ("other", "request-1", "amendment-request", "approve"),
        ("owner", "old-request", "amendment-request", "approve"),
        ("owner", "request-1", None, "approve"),
        ("owner", "request-1", "amendment-request", "override"),
    ]:
        response = ApprovalResponsePayload(
            request_id=req, decision=decision, decided_by=who, decided_at=NOW
        )
        assert not accept_response(
            x.cx,
            x.bid,
            x.envelope.model_copy(update={"correlation_id": corr}),
            response,
            "owner",
        )
    for decision in ("defer", "reject"):
        response = ApprovalResponsePayload(
            request_id="request-1",
            decision=decision,
            decided_by="owner",
            decided_at=NOW,
        )
        assert accept_response(x.cx, x.bid, x.envelope, response, "owner")
        with pytest.raises(AmendmentRefused):
            await admit(x.cx, x.bid, x.runner, str(x.repo))
    assert git(x.repo, "rev-parse", "planning/amendment") == x.plan
    assert x.pool.read_source_commit(x.bid) is None


@pytest.mark.asyncio
async def test_corrupt_persisted_diff_refuses_cli_and_card(planned):
    x = planned
    project_approval(x.cx, x.envelope)
    original = x.cx.execute(
        "SELECT binding_json FROM build_requirement_amendments"
    ).fetchone()[0]
    for field in (
        "changes",
        "candidate_digest",
        "repo",
        "plan_commit",
        "request_id",
        "review_digest",
    ):
        value = json.loads(original)
        value[field] = [] if field == "changes" else "wrong"
        x.cx.execute(
            "UPDATE build_requirement_amendments SET binding_json=?",
            (canonical(value),),
        )
        x.cx.commit()
        with pytest.raises(AmendmentRefused):
            project_approval(x.cx, x.envelope)
        cli = CliRunner().invoke(
            status_cmd,
            [
                x.bid,
                "--requirement-amendment-diff",
                "--json",
                "--db-path",
                str(x.pool.db_path),
            ],
        )
        assert cli.exit_code != 0
    x.cx.execute("UPDATE build_requirement_amendments SET binding_json=?", (original,))
    x.cx.commit()


@pytest.mark.asyncio
async def test_defer_rearm_keeps_exact_diff_and_rejects_old_response(planned):
    x = planned
    first = project_approval(x.cx, x.envelope)
    x.cx.execute(
        "UPDATE builds SET pending_approval_request_id='request-2' WHERE build_id=?",
        (x.bid,),
    )
    x.cx.commit()
    next_envelope = x.envelope.model_copy(
        update={"payload": {**x.envelope.payload, "request_id": "request-2"}}
    )
    again = project_approval(x.cx, next_envelope)
    before = first.payload["details"]["requirement_amendment"]
    after = again.payload["details"]["requirement_amendment"]
    assert {**before, "request_id": "request-2"} == after
    assert (
        first.payload["details"]["rationale"] == again.payload["details"]["rationale"]
    )
    assert not accept_response(
        x.cx,
        x.bid,
        x.envelope,
        ApprovalResponsePayload(
            request_id="request-1",
            decision="approve",
            decided_by="owner",
            decided_at=NOW,
        ),
        "owner",
    )


@pytest.mark.asyncio
async def test_exact_crash_recovery_and_unexpected_delta(planned):
    x = planned
    approve(x)
    result = await x.runner.prepare_branch_and_write_tree(
        str(x.repo),
        "planning/amendment",
        {DOC: NEW},
        "Approved write interrupted before ledger",
        expected_head=x.plan,
    )
    assert result.status == "success"
    assert await admit(x.cx, x.bid, x.runner, str(x.repo)) == result.sha
    # Recovery never adopts a descendant or an extra changed file.
    x.cx.execute(
        "UPDATE build_requirement_amendments SET source_commit=NULL,apply_receipt_json=NULL"
    )
    x.cx.execute("UPDATE builds SET source_commit=NULL")
    x.cx.commit()
    more = await x.runner.prepare_branch_and_write_tree(
        str(x.repo),
        "planning/amendment",
        {"unrelated.txt": "extra"},
        "Unexpected descendant",
        expected_head=result.sha,
    )
    with pytest.raises(AmendmentRefused):
        await admit(x.cx, x.bid, x.runner, str(x.repo))
    assert git(x.repo, "rev-parse", "planning/amendment") == more.sha


@pytest.mark.parametrize(
    "value",
    [
        None,
        "docs/file",
        ["../file"],
        ["/file"],
        ["a//b"],
        ["a/./b"],
        ["a", "A"],
        ["a", "a/b"],
        ["a*"],
        ["a\x00b"],
        ["a\\b"],
    ],
)
def test_scope_parser_refuses_noncanonical_or_colliding_paths(value):
    with pytest.raises(AmendmentRefused):
        paths(value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "checks/pass.txt",
        ".guardkit/config.yaml",
        "tasks/pass-bar.txt",
        "requirements/missing.contract",
        "requirements",
    ],
)
async def test_only_exact_existing_scoped_requirements_are_eligible(planned, path):
    x = planned
    with pytest.raises(AmendmentRefused):
        await validate_proposal(
            runner=x.runner,
            repo_path=str(x.repo),
            raw=canonical(proposal(x.base, path)),
            scope=[path],
            base=x.base,
            plan=x.plan,
            feature_id=x.feature,
        )


@pytest.mark.asyncio
async def test_normal_producer_serialization_and_pinned_jarvis_consumer(
    planned, monkeypatch
):
    x = planned
    # Load the installed consumer modules without invoking the unrelated app
    # bootstrap in infrastructure.__init__; sends remain injected fakes.
    import importlib.util
    import sys
    import types

    package = types.ModuleType("jarvis.infrastructure")
    package.__path__ = [
        str(Path(importlib.util.find_spec("jarvis").origin).parent / "infrastructure")
    ]
    monkeypatch.setitem(sys.modules, "jarvis.infrastructure", package)
    from jarvis.infrastructure.forge_notifications import ForgeNotificationsSubscriber
    from jarvis.infrastructure.slack_notifier import (
        build_pause_blocks,
        _build_button_value,
    )

    notifications = []
    sent = []

    class Sink:
        async def notify(self, notification):
            notifications.append(notification)

    consumer = ForgeNotificationsSubscriber(SimpleNamespace(), SimpleNamespace())
    consumer.bind_notification_sink(Sink())

    class Wire:
        max_payload = 512 * 1024

        async def publish(self, subject, body):
            sent.append((subject, json.loads(body)))

    wire = Wire()
    publisher = ApprovalPublisher(nats_client=wire)
    publisher.amendment_projector = lambda envelope: project_approval(x.cx, envelope)

    class Emitter:
        async def emit_paused(self, ctx, **kwargs):
            envelope = MessageEnvelope(
                source_id="forge",
                event_type=EventType.BUILD_PAUSED,
                correlation_id=ctx.correlation_id,
                payload={
                    "build_id": ctx.build_id,
                    "feature_id": ctx.feature_id,
                    "correlation_id": ctx.correlation_id,
                    **kwargs,
                },
            )
            await consumer._handle_pause_or_cancelled(envelope, "build_paused")

    mirrored = _MirroredApprovalPublisher(
        publisher,
        emitter=Emitter(),
        build_context=BuildContext(
            build_id=x.bid,
            feature_id=x.feature,
            correlation_id="amendment-request",
            wave_total=1,
        ),
        clock=lambda: NOW,
        amendment_pool=x.pool,
    )
    await mirrored.publish_request(x.envelope)
    assert len(sent) == len(notifications) == 1
    bound = sent[0][1]["payload"]["details"]["requirement_amendment"]
    verify_binding(bound)
    notification = notifications[0]
    metadata = _build_button_value(
        "request-1", x.bid, "amendment-request", notification.approval_subject
    )
    assert metadata and len(metadata) < 2000 and "after_text" not in metadata
    blocks = build_pause_blocks(notification, metadata)
    rendered = json.dumps(blocks, ensure_ascii=False)
    assert bound["candidate_digest"] in rendered
    assert f"forge status {x.bid} --requirement-amendment-diff" in rendered
    assert all(len(b.get("text", {}).get("text", "")) <= 3000 for b in blocks)
    wire.max_payload = 100
    with pytest.raises(AmendmentRefused):
        await publisher.publish_request(x.envelope)
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_original_scope_roundtrip_direct_queue_and_conflicting_redelivery(
    tmp_path,
):
    from unittest.mock import AsyncMock
    from nats_core.events import PlanningQueuedPayload
    from forge.adapters.nats.planning_consumer import (
        PlanningConsumerDeps,
        handle_planning_message,
        create_and_start_planning_run,
    )
    from forge.planning.work_queue_store import WorkQueueStore
    from forge.planning.work_queue_loop import WorkQueueLoop
    from tests.forge.adapters.test_planning_consumer_queue import _payload, _msg

    cx = connect_writer(tmp_path / "intake.db")
    apply_at_boot(cx)
    store = SqlitePlanningRunStore(cx)
    queue = WorkQueueStore(cx)
    payload = _payload(
        correlation_id="scope-queued",
        target_repo="example/project",
        requirement_amendment_paths=[DOC],
    )
    parsed = PlanningQueuedPayload.model_validate_json(
        PlanningQueuedPayload.model_validate(payload).model_dump_json()
    )
    assert parsed.model_extra[FIELD] == [DOC]
    deps = PlanningConsumerDeps(
        store=store,
        queue_store=queue,
        publish_notification=AsyncMock(),
        on_recorded=AsyncMock(),
    )
    await handle_planning_message(_msg(payload), deps)
    queued = queue.get_by_correlation_id("scope-queued")
    assert queue.origin_details(queued["id"])[FIELD] == [DOC]
    loop = object.__new__(WorkQueueLoop)
    loop._store = queue
    admission = loop._admission_for(queued)
    assert admission.requirement_amendment_paths == (DOC,)
    await create_and_start_planning_run(
        deps,
        correlation_id=admission.correlation_id,
        request_text=admission.request_text,
        originating_user=admission.originating_user,
        triggered_by=admission.triggered_by,
        target_repo=admission.target_repo,
        requirement_amendment_paths=admission.requirement_amendment_paths,
    )
    assert json.loads(store.get_run("scope-queued")[FIELD]) == [DOC]
    bad = _msg({**payload, FIELD: ["requirements/sibling.contract"]})
    await handle_planning_message(bad, deps)
    assert queue.origin_details(queued["id"])[FIELD] == [DOC]
    bad.nak.assert_awaited_once()
    direct = PlanningConsumerDeps(
        store=store, publish_notification=AsyncMock(), on_recorded=AsyncMock()
    )
    await handle_planning_message(
        _msg({**payload, "correlation_id": "scope-direct"}), direct
    )
    assert json.loads(store.get_run("scope-direct")[FIELD]) == [DOC]
    for extra in (
        {"kind": "fix"},
        {"queue_command": {"verb": "build"}},
        {"queue_command": {"verb": "mission"}},
    ):
        bad = _msg({**payload, "correlation_id": "scope-unsupported", **extra})
        await handle_planning_message(bad, deps)
        assert (
            store.get_run("scope-unsupported") is None
            and queue.get_by_correlation_id("scope-unsupported") is None
        )
    cx.close()


@pytest.mark.asyncio
async def test_missing_trusted_proposal_never_falls_back_to_branch_launch(planned):
    x = planned
    approve(x)
    x.cx.execute("DELETE FROM planning_requirement_amendments")
    x.cx.commit()
    with pytest.raises(AmendmentRefused):
        await admit(x.cx, x.bid, x.runner, str(x.repo))
    with pytest.raises(AmendmentRefused):
        project_approval(x.cx, x.envelope)
    assert x.pool.read_source_commit(x.bid) is None


@pytest.mark.asyncio
async def test_common_resume_launcher_passes_exact_admitted_source(
    planned, monkeypatch
):
    x = planned
    approve(x)
    from forge.cli import _serve_deps as launch_module
    from unittest.mock import AsyncMock

    dispatch = AsyncMock(return_value="launched")
    monkeypatch.setattr(launch_module, "dispatch_autobuild_async", dispatch)
    config = SimpleNamespace(
        planning=SimpleNamespace(target_repo_paths={"example/project": str(x.repo)})
    )
    launcher = launch_module._build_resume_launcher(
        None,
        None,
        None,
        None,
        object(),
        memory_project_reader=lambda _id: "original-memory",
        launch_settings_reader=lambda _id: ("ORIGINAL_SETTING",),
        source_commit_reader=x.pool.read_source_commit,
        amendment_admitter=launch_module.build_requirement_amendment_admission(
            x.pool, config, git_runner=x.runner
        ),
    )
    await launcher(
        build_id=x.bid,
        feature_id=x.feature,
        correlation_id="amendment-request",
        repo="example/project",
    )
    a = x.pool.read_source_commit(x.bid)
    assert dispatch.await_args.kwargs["source_commit"] == a
    assert dispatch.await_args.kwargs["memory_project"] == "original-memory"
    assert dispatch.await_args.kwargs["launch_settings"] == ("ORIGINAL_SETTING",)
    await launcher(
        build_id=x.bid,
        feature_id=x.feature,
        correlation_id="amendment-request",
        repo="example/project",
    )
    assert dispatch.await_args.kwargs["source_commit"] == a


@pytest.mark.asyncio
async def test_concurrent_launches_reuse_one_commit(planned):
    import asyncio

    x = planned
    approve(x)
    first, second = await asyncio.gather(
        admit(x.cx, x.bid, x.runner, str(x.repo)),
        admit(x.cx, x.bid, x.runner, str(x.repo)),
    )
    assert first == second
    assert git(x.repo, "rev-list", "--count", x.plan + ".." + first) == "1"


@pytest.mark.parametrize(
    "kind",
    ["empty", "noop", "duplicate", "unknown", "binary", "overflow", "duplicate-key"],
)
def test_candidate_bytes_refuse_without_partial_acceptance(kind):
    value = proposal("a" * 40)
    if kind == "empty":
        value["changes"] = []
    elif kind == "noop":
        value["changes"][0]["after_text"] = ""
    elif kind == "duplicate":
        value["changes"] *= 2
    elif kind == "unknown":
        value["owner_approved"] = True
    elif kind == "binary":
        value["changes"][0]["after_text"] = "x\0x"
    elif kind == "overflow":
        value["changes"][0]["after_text"] = "é" * (128 * 1024)
    raw = canonical(value)
    if kind == "duplicate-key":
        raw = raw.replace('"schema_version":1', '"schema_version":1,"schema_version":1')
    with pytest.raises(AmendmentRefused):
        candidate(raw)


@pytest.mark.asyncio
@pytest.mark.parametrize("planned", ["maximum"], indirect=True)
async def test_maximum_unicode_diff_uses_honest_bounded_summary_and_complete_cli(
    planned, monkeypatch
):
    x = planned
    await test_normal_producer_serialization_and_pinned_jarvis_consumer(x, monkeypatch)
    binding = binding_for_build(x.cx, x.bid, required=True)
    assert len(binding["changes"]) == 32
    assert (
        sum(
            len(c["before_text"].encode()) + len(c["after_text"].encode())
            for c in binding["changes"]
        )
        == 128 * 1024
    )
    rationale = summary(binding)
    assert "Path list shortened here" in rationale and "complete scope" in rationale
    assert (
        len(
            json.dumps(
                rationale.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
        )
        <= 1800
    )
    cli = CliRunner().invoke(
        status_cmd,
        [
            x.bid,
            "--requirement-amendment-diff",
            "--json",
            "--db-path",
            str(x.pool.db_path),
        ],
    )
    assert cli.exit_code == 0, cli.output
    assert json.loads(cli.output) == binding


@pytest.mark.asyncio
async def test_exact_diff_does_not_resolve_newer_same_feature_build(planned):
    x = planned
    approve(x)
    payload = BuildQueuedPayload(
        feature_id=x.feature,
        repo="example/project",
        branch="other",
        feature_yaml_path="other.yaml",
        triggered_by="cli",
        originating_user="owner",
        correlation_id="newer-request",
        requested_at=NOW + timedelta(seconds=1),
        queued_at=NOW + timedelta(seconds=1),
        mode="mode-b",
    )
    newer = x.pool.record_pending_build(payload)
    assert newer != x.bid
    cli = CliRunner().invoke(
        status_cmd,
        [
            x.bid,
            "--requirement-amendment-diff",
            "--json",
            "--db-path",
            str(x.pool.db_path),
        ],
    )
    assert cli.exit_code == 0 and json.loads(cli.output)["build_id"] == x.bid
    assert (
        CliRunner()
        .invoke(
            status_cmd,
            [
                newer,
                "--requirement-amendment-diff",
                "--json",
                "--db-path",
                str(x.pool.db_path),
            ],
        )
        .exit_code
        != 0
    )


@pytest.mark.asyncio
async def test_actual_subscriber_requires_exact_correlation_and_owner(planned):
    import asyncio
    from forge.adapters.nats.approval_subscriber import (
        ApprovalSubscriber,
        ApprovalSubscriberDeps,
    )
    from forge.config.models import ApprovalConfig

    x = planned
    project_approval(x.cx, x.envelope)
    subscriber = ApprovalSubscriber(
        ApprovalSubscriberDeps(
            nats_client=SimpleNamespace(),
            config=ApprovalConfig(),
            expected_approver="owner",
        )
    )
    subscriber.amendment_validator = (
        lambda bid, envelope, response, expected, additional: accept_response(
            x.cx, bid, envelope, response, expected, additional
        )
    )
    subscriber._queues[x.bid] = asyncio.Queue()
    response = ApprovalResponsePayload(
        request_id="request-1", decision="approve", decided_by="owner", decided_at=NOW
    )
    bad = MessageEnvelope(
        source_id="owner",
        event_type=EventType.APPROVAL_RESPONSE,
        payload=response.model_dump(mode="json"),
    )
    await subscriber._on_envelope(build_id=x.bid, envelope=bad)
    assert subscriber._queues[x.bid].empty()
    assert (
        x.cx.execute(
            "SELECT accepted_request_id FROM build_requirement_amendments"
        ).fetchone()[0]
        is None
    )
    good = bad.model_copy(update={"correlation_id": "amendment-request"})
    await subscriber._on_envelope(build_id=x.bid, envelope=good)
    assert (await subscriber._queues[x.bid].get()).request_id == "request-1"
    assert (
        x.cx.execute(
            "SELECT accepted_request_id FROM build_requirement_amendments"
        ).fetchone()[0]
        == "request-1"
    )


@pytest.mark.asyncio
async def test_sidecar_writer_forwards_cas_and_raw_parent_probe(planned):
    x = planned
    from forge.planning.sidecar_git_runner import SidecarGitRunner

    seen = []

    def post(url, body, timeout):
        seen.append(body)
        return 200, {"status": "success", "sha": x.plan, "checks": []}

    # The transport is a function fake; no client or socket is created.
    runner = SidecarGitRunner(
        "http://unused.invalid", repo="example/project", post=post
    )
    await runner.prepare_branch_and_write_tree(
        str(x.repo), "planning/amendment", {DOC: NEW}, "Apply", expected_head=x.plan
    )
    assert seen[-1]["expected_head"] == x.plan and seen[-1]["checks"] == []
    assert "launch_settings" not in seen[-1] and "memory_project" not in seen[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("proposal_state", ["proposed", "absent"])
@pytest.mark.parametrize("registrations", ["both", "bars-only"])
async def test_normal_driver_registers_qa_then_freezes_final_source(
    tmp_path, monkeypatch, proposal_state, registrations
):
    """The normal plan producer/driver/registration path must produce P != Q."""
    from tests.forge.planning.test_driver_target_terminal import (
        CID,
        ORIGINATOR,
        TARGET_REPO,
        _init_scratch_repo,
        _recorded_runner,
        _make_driver,
        _spec_result_native,
        _publish_to_origin,
        _seed_gate_surface,
    )
    from tests.forge.recorded_fetch_address import allow_local_remotes
    from specialist_agent.roles.architect.modes.feature_plan import (
        add_semantic_review_receipt,
    )
    from forge.planning.states import PlanningState
    from forge.planning.requirement_amendment_store import _plan

    allow_local_remotes(monkeypatch, tmp_path / "fetches")
    repo = tmp_path / "normal-project"
    _init_scratch_repo(repo)
    (repo / "requirements").mkdir()
    (repo / DOC).write_text(OLD)
    config = repo / ".guardkit/config.yaml"
    config.write_text(
        config.read_text()
        + "autobuild:\n  player:\n    required_documents: [requirements/runtime.contract]\n    protected_paths: [requirements]\n"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "Declare protected requirement")
    if registrations == "both":
        _seed_gate_surface(repo)
    _publish_to_origin(repo)
    cx = connect_writer(tmp_path / "normal.db")
    apply_at_boot(cx)
    store = SqlitePlanningRunStore(cx, target_terminal_enabled=True)
    store.record_queued(
        CID,
        ORIGINATOR,
        ORIGINATOR,
        "add a GET /stats endpoint and update the active runtime requirement",
        "jarvis",
        target_repo=TARGET_REPO,
        requirement_amendment_paths=(DOC,),
    )
    runner = _recorded_runner(tmp_path / "normal-wt", repo)
    import specialist_agent

    specialist_tests = Path(specialist_agent.__file__).resolve().parents[2] / "tests"
    monkeypatch.syspath_prepend(str(specialist_tests))
    from roles.architect.test_feature_plan_content_check_session import (
        _plan as specialist_plan,
    )
    from roles.architect.test_feature_plan_dispatch import _FID as specialist_feature
    from specialist_agent.roles.architect.modes.feature_plan import (
        postprocess_feature_plan,
    )
    from specialist_agent.modes.types import ModeRuntimeContext

    async def planner(**kwargs):
        feature = kwargs["feature_id"]
        descriptor = kwargs["target_repo_descriptor"]
        assert descriptor[FIELD] == [DOC]
        if kwargs.get("revision_of") is not None:
            files = kwargs["revision_of"]
        else:
            output = (
                specialist_plan(request=kwargs["request_text"])
                .replace(specialist_feature, feature)
                .replace('"x": TASK-DEMO-001', '"ok": TASK-DEMO-001')
            )
            manifest = f".guardkit/features/{feature}.requirement-amendments.json"
            if proposal_state == "proposed":
                output += f"\n=== FILE: {manifest} ===\n{canonical(proposal(descriptor['requirement_amendment_base_commit']))}\n=== END FILE ===\n"
            ctx = ModeRuntimeContext(
                role_config=None,
                role_dir=specialist_tests.parent / "roles" / "architect",
                templates={},
                output_path=str(tmp_path / "seat-output"),
                project_name="fixture",
                supplied_feature_id=feature,
                spec_feature=kwargs["spec_feature"],
                spec_feature_paths=tuple(kwargs.get("spec_feature_paths") or ()),
                target_repo_descriptor=descriptor,
                request_text=kwargs["request_text"],
            )
            processed = postprocess_feature_plan(output, ctx)
            files = {
                name: body
                for name, body in processed.items()
                if name not in ("validation.json", "semantic_review.json")
            }
        evaluation = SimpleNamespace(
            verdict="GOOD",
            criterion_scores=[
                SimpleNamespace(criterion_id="request_traceability", score=1.0)
            ],
            detection_findings=[],
        )
        reviewed = add_semantic_review_receipt(
            files,
            evaluation,
            reviewed_after_rewrite=kwargs.get("revision_of") is not None,
        )
        return SimpleNamespace(
            outcome=SimpleNamespace(value="completed"),
            role_output=reviewed,
            reason=None,
        )

    h = _make_driver(
        store,
        git_runner=runner,
        repo_path=str(repo),
        spec_result=_spec_result_native(),
        plan_dispatch=planner,
    )
    await h.driver.drive(CID)
    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value, h.ctx[
        "notifications"
    ]
    bound = _plan(cx, CID)
    assert bound["reviewed_plan_commit"] != bound["plan_commit"]
    assert len(bound["qa_receipts"]) == 2
    assert bound["amendment_state"] == proposal_state
    assert bool(bound["qa_receipts"][1]["receipt"].get("skipped")) == (
        registrations == "bars-only"
    )
    assert h.ctx["counters"]["pass_bar_validate"] > 0 and (
        registrations == "bars-only" or h.ctx["counters"]["gate_registry_validate"] > 0
    )
    # The persisted same-run proof must still reject stale/missing/changed
    # registration evidence; none of these failures may create a build.
    from forge.planning.requirement_amendment_store import freeze_final_plan
    from forge.planning.driver import _QA_PASS_BARS_STAGE, _QA_FEATURE_GATE_STAGE

    for mutation in (
        "missing",
        "duplicate",
        "stale-parent",
        "wrong-feature",
        "wrong-output",
        "wrong-plan",
        "wrong-branch",
    ):
        cx.execute("SAVEPOINT negative_receipt")
        event_id = bound["qa_receipts"][0]["event_id"]
        receipt = json.loads(json.dumps(bound["qa_receipts"][0]["receipt"]))
        if mutation == "missing":
            cx.execute("DELETE FROM planning_run_events WHERE id=?", (event_id,))
        elif mutation == "duplicate":
            row = cx.execute(
                "SELECT * FROM planning_run_events WHERE id=?", (event_id,)
            ).fetchone()
            columns = [key for key in row.keys() if key != "id"]
            cx.execute(
                "INSERT INTO planning_run_events ("
                + ",".join(columns)
                + ") VALUES ("
                + ",".join("?" for _ in columns)
                + ")",
                tuple(row[key] for key in columns),
            )
        else:
            proof = receipt["requirement_amendment_qa"]
            if mutation == "stale-parent":
                proof["parent_commit"] = bound["base_commit"]
            if mutation == "wrong-feature":
                receipt["feature_id"] = "FEAT-DEAD"
            if mutation == "wrong-output":
                proof["outputs"][0]["sha256"] = "0" * 64
            if mutation == "wrong-plan":
                receipt["registered_at_sha"] = bound["base_commit"]
            if mutation == "wrong-branch":
                proof["branch"] = "planning/other"
            cx.execute(
                "UPDATE planning_run_events SET details_json=? WHERE id=?",
                (json.dumps(receipt), event_id),
            )
        with pytest.raises(AmendmentRefused):
            await freeze_final_plan(
                cx,
                CID,
                runner,
                str(repo),
                pass_stage=_QA_PASS_BARS_STAGE,
                gate_stage=_QA_FEATURE_GATE_STAGE,
            )
        assert not cx.execute("SELECT 1 FROM builds").fetchone()
        cx.execute("ROLLBACK TO negative_receipt")
        cx.execute("RELEASE negative_receipt")
    if registrations == "both" and proposal_state == "proposed":
        final_proof = bound["qa_receipts"][1]
        original_receipt = final_proof["receipt"]
        gate_proof = original_receipt["requirement_amendment_qa"]
        generated = {}
        for output in gate_proof["outputs"]:
            result = await runner.read_file_at_commit(
                str(repo), bound["plan_commit"], output["path"], raw=True
            )
            generated[output["path"]] = result.content
        for defect in (
            "extra-output",
            "changed-output",
            "changed-reviewed-plan",
            "changed-target",
        ):
            invalid = dict(generated)
            if defect == "extra-output":
                invalid["unrelated.txt"] = "not generated by this registration\n"
            elif defect == "changed-output":
                invalid[next(iter(invalid))] += "\nnot the validated output\n"
            elif defect == "changed-reviewed-plan":
                invalid[f".guardkit/features/{bound['feature_id']}.yaml"] = (
                    "changed plan\n"
                )
            else:
                invalid[DOC] = NEW
            git(repo, "branch", "negative/" + defect, gate_proof["parent_commit"])
            bad = await runner.prepare_branch_and_write_tree(
                str(repo),
                "negative/" + defect,
                invalid,
                "Untrusted registration fixture",
                start_commit=gate_proof["parent_commit"],
                expected_head=gate_proof["parent_commit"],
            )
            assert bad.status == "success", bad.stderr
            receipt = json.loads(json.dumps(original_receipt))
            receipt["sha"] = bad.sha
            receipt["requirement_amendment_qa"]["commit"] = bad.sha
            cx.execute("SAVEPOINT bad_tree")
            cx.execute(
                "UPDATE planning_run_events SET details_json=? WHERE id=?",
                (json.dumps(receipt), final_proof["event_id"]),
            )
            with pytest.raises(AmendmentRefused):
                await freeze_final_plan(
                    cx,
                    CID,
                    runner,
                    str(repo),
                    pass_stage=_QA_PASS_BARS_STAGE,
                    gate_stage=_QA_FEATURE_GATE_STAGE,
                )
            cx.execute("ROLLBACK TO bad_tree")
            cx.execute("RELEASE bad_tree")
        assert git(repo, "rev-parse", f"planning/{CID}") == bound["plan_commit"]
    feature = bound["feature_id"]
    pool = SqliteLifecyclePersistence(connection=cx)
    bid = pool.record_pending_build(
        BuildQueuedPayload(
            feature_id=feature,
            repo=TARGET_REPO,
            branch=f"planning/{CID}",
            feature_yaml_path=str(repo / f".guardkit/features/{feature}.yaml"),
            triggered_by="forge-internal",
            originating_user=ORIGINATOR,
            correlation_id=CID,
            requested_at=NOW,
            queued_at=NOW,
            mode="mode-b",
        )
    )
    normal_request = bid + ":autobuild:0"
    cx.execute(
        "UPDATE builds SET status='PAUSED',pending_approval_request_id=? WHERE build_id=?",
        (normal_request, bid),
    )
    cx.commit()
    card = MessageEnvelope(
        source_id="forge",
        event_type=EventType.APPROVAL_REQUEST,
        correlation_id=CID,
        payload={
            "request_id": normal_request,
            "details": {
                "build_id": bid,
                "feature_id": feature,
                "stage_label": "autobuild",
            },
        },
    )
    card = project_approval(cx, card)
    # Consume this exact serialized producer card with the actual full-policy
    # harness (paired tools directory is required on PYTHONPATH).
    import one_sentence as operator

    run = operator.Run(
        arm="fixture",
        approver_fallback=ORIGINATOR,
        requirement_amendment_paths=(DOC,),
        requirement_amendment_repo=TARGET_REPO,
        requirement_amendment_request=store.get_run(CID)["request_text"],
    )
    run.correlation_id = CID
    run.correlations.add(CID)
    subject = "agents.approval.forge." + bid
    assert run.accept_identity(
        subject, card.model_dump(mode="json"), card=True, admit=True
    )
    assert (
        operator.answer_for(run, subject, card.model_dump(mode="json")).decision
        == "approve"
    )
    assert accept_response(
        cx,
        bid,
        card,
        ApprovalResponsePayload(
            request_id=normal_request,
            decision="approve",
            decided_by=ORIGINATOR,
            decided_at=NOW,
        ),
        ORIGINATOR,
    )
    a = await admit(cx, bid, runner, str(repo))
    if proposal_state == "absent":
        assert a is None and pool.read_source_commit(bid) is None
        assert git(repo, "rev-parse", f"planning/{CID}") == bound["plan_commit"]
        cli = CliRunner().invoke(
            status_cmd,
            [
                bid,
                "--requirement-amendment-diff",
                "--json",
                "--db-path",
                str(pool.db_path),
            ],
        )
        assert (
            cli.exit_code == 0 and json.loads(cli.output)["amendment_state"] == "absent"
        )
        from forge.cli._serve_deps import (
            _build_resume_launcher,
            build_requirement_amendment_admission,
        )
        from unittest.mock import MagicMock

        launched = []

        class OrdinaryStarter:
            async def astart_async_task(self, **kwargs):
                launched.append(kwargs["context"])
                return "ordinary-task"

        config = SimpleNamespace(
            planning=SimpleNamespace(target_repo_paths={TARGET_REPO: str(repo)})
        )
        launcher = _build_resume_launcher(
            SimpleNamespace(build_for=lambda **kw: []),
            MagicMock(),
            MagicMock(),
            None,
            OrdinaryStarter(),
            source_commit_reader=pool.read_source_commit,
            amendment_admitter=build_requirement_amendment_admission(
                pool, config, git_runner=runner
            ),
        )
        await launcher(
            build_id=bid,
            feature_id=feature,
            correlation_id=CID,
            branch=f"planning/{CID}",
            repo=TARGET_REPO,
        )
        assert (
            not launched[0].get("source_commit")
            and launched[0]["branch"] == f"planning/{CID}"
        )
        assert await admit(cx, bid, runner, str(repo)) is None
        assert git(repo, "rev-parse", f"planning/{CID}") == bound["plan_commit"]
        cx.close()
        return
    assert git(repo, "rev-parse", a + "^") == bound["plan_commit"]
    assert git(repo, "diff", "--name-only", bound["plan_commit"], a) == DOC
    for proof in bound["qa_receipts"]:
        for output in proof["receipt"]["requirement_amendment_qa"]["outputs"]:
            assert git(repo, "rev-parse", a + ":" + output["path"]) == git(
                repo, "rev-parse", bound["plan_commit"] + ":" + output["path"]
            )
    # The common launcher and actual dispatcher serialize A into the real
    # runner graph. Only the external GuardKit process is scripted.
    from forge.cli._serve_deps import (
        _build_resume_launcher,
        build_requirement_amendment_admission,
    )
    from tests.forge.test_autobuild_runner_prepared_source import _run
    from forge.subagents import autobuild_runner as ar
    from unittest.mock import MagicMock
    import asyncio

    launched = []

    class Starter:
        async def astart_async_task(self, **kw):
            launched.append(kw["context"])
            return "fixture-task"

    config = SimpleNamespace(
        planning=SimpleNamespace(target_repo_paths={TARGET_REPO: str(repo)})
    )
    launcher = _build_resume_launcher(
        SimpleNamespace(build_for=lambda **kw: []),
        MagicMock(),
        MagicMock(),
        None,
        Starter(),
        source_commit_reader=pool.read_source_commit,
        amendment_admitter=build_requirement_amendment_admission(
            pool, config, git_runner=runner
        ),
    )
    await launcher(
        build_id=bid,
        feature_id=feature,
        correlation_id=CID,
        branch=f"planning/{CID}",
        repo=TARGET_REPO,
    )
    assert launched[0]["source_commit"] == a
    monkeypatch.setenv(
        ar.FORGE_AUTOBUILD_WORKTREE_BASE_ENV, str(tmp_path / "actual-runner")
    )
    monkeypatch.setenv(ar.RECEIPTS_DIR_ENV, str(tmp_path / "actual-receipts"))
    monkeypatch.setenv("FORGE_DB_PATH", str(tmp_path / "absent-runner-ledger.db"))
    import importlib.util
    import sys
    import types

    package = types.ModuleType("guardkit.orchestrator")
    package.__path__ = [
        str(Path(importlib.util.find_spec("guardkit").origin).parent / "orchestrator")
    ]
    monkeypatch.setitem(sys.modules, "guardkit.orchestrator", package)
    from guardkit.orchestrator.protected_paths import (
        load_protected_reference,
        restore_protected_paths,
        hold_protected_index,
    )
    from guardkit.orchestrator.harness.selector import load_project_documents_at_commit
    from guardkitfactory.harness.backend_config import PathConfinedBackend

    class Calls(list):
        def append(self, item):
            work = Path(item["cwd"])
            item["requirement"] = (work / DOC).read_text()
            # The runner outer checkout is detached. GuardKit creates its
            # Player branch beneath that source; model that process boundary
            # with a temporary Git worktree and run its real protection code.
            player = tmp_path / "consumer-player"
            if not player.exists():
                git(
                    work,
                    "worktree",
                    "add",
                    "-q",
                    "-b",
                    "fixture-player",
                    str(player),
                    a,
                )
            ref = load_protected_reference(player, a, "fixture-player")
            assert any(
                doc.text == NEW
                for doc in load_project_documents_at_commit(player, a).documents
            )
            backend = PathConfinedBackend(
                SimpleNamespace(), [player], protected_paths=[player / "requirements"]
            )
            assert backend.write(str(player / DOC), "forbidden").error
            assert backend.edit(str(player / DOC), "version 2", "version 1").error
            (player / DOC).write_text("unauthorized shell edit\n")
            git(player, "add", DOC)
            restore_protected_paths(player, ref)
            hold_protected_index(player, ref)
            assert (player / DOC).read_text() == NEW
            assert git(player, "diff", "--cached", "--name-only") == ""
            item["qa_present"] = all(
                (work / out["path"]).is_file()
                for ev in bound["qa_receipts"]
                for out in ev["receipt"]["requirement_amendment_qa"]["outputs"]
            )
            super().append(item)

    calls = Calls()
    result = await asyncio.to_thread(_run, repo, launched[0], calls)
    assert result["async_tasks"][feature]["lifecycle"] == "completed"
    assert len(calls) == 2 and all(
        call["head"] == a and call["requirement"] == NEW and call["qa_present"]
        for call in calls
    )
    assert calls[0]["argv"][1:3] == ["feature", "validate"]
    assert "--base-branch" in calls[1]["argv"]
    cx.close()


@pytest.mark.asyncio
async def test_real_gate_restart_reuses_frozen_card_and_common_launcher(
    planned, monkeypatch
):
    import asyncio
    from unittest.mock import AsyncMock
    from tests.integration.test_gate_restart_recovery import (
        EventLogNats,
        _build_parts,
        _forge_config,
        _wait_until,
    )
    from forge.cli._serve_gate_activation import maybe_gate_build, rearm_paused_gates
    from forge.cli import _serve_deps, _serve_deps_gating, _serve_planning
    from forge.gating.sqlite_adapters import build_sqlite_gate_adapters
    from forge.gating.wrappers import GateOutcome

    x = planned
    config = _forge_config(expected_approver="owner")
    config.planning.target_repo_paths["example/project"] = str(x.repo)
    monkeypatch.setattr(
        _serve_planning, "compose_planning_git_runner", lambda config: (x.runner, None)
    )
    x.cx.execute(
        "UPDATE builds SET status='QUEUED',pending_approval_request_id=NULL WHERE build_id=?",
        (x.bid,),
    )
    x.cx.commit()
    wire = EventLogNats()
    parts = _build_parts(wire, forge_config=config)
    repository, state = build_sqlite_gate_adapters(x.pool, clock=lambda: NOW)
    task = asyncio.create_task(
        maybe_gate_build(
            parts=parts,
            sqlite_pool=x.pool,
            gate_repository=repository,
            gate_state_machine=state,
            build_id=x.bid,
            feature_id=x.feature,
            correlation_id="amendment-request",
            clock=lambda: NOW,
            forge_config=config,
        )
    )
    subject = "agents.approval.forge." + x.bid
    await _wait_until(
        lambda: bool(wire.published.get(subject)),
        what="real amendment gate publication",
    )
    original = json.loads(wire.published[subject][-1])
    assert original["payload"]["details"]["requirement_amendment"] == binding_for_build(
        x.cx, x.bid
    )
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    wire.reset_wire()
    parts = _build_parts(wire, forge_config=config)
    _serve_deps_gating.bind_gate_parts(parts)
    dispatch = AsyncMock(return_value="launched")
    monkeypatch.setattr(_serve_deps, "dispatch_autobuild_async", dispatch)
    launcher = _serve_deps._build_resume_launcher(
        None,
        None,
        None,
        None,
        object(),
        source_commit_reader=x.pool.read_source_commit,
        amendment_admitter=_serve_deps.build_requirement_amendment_admission(
            x.pool, config, git_runner=x.runner
        ),
    )
    try:
        tasks = await rearm_paused_gates(
            parts=parts,
            sqlite_pool=x.pool,
            gate_repository=repository,
            gate_state_machine=state,
            resume_launcher=launcher,
            client=wire,
            clock=lambda: NOW,
            forge_config=config,
        )
        assert len(tasks) == 1
        replay = json.loads(wire.published[subject][-1])
        assert replay["payload"]["request_id"] == original["payload"]["request_id"]
        assert (
            replay["payload"]["details"]["requirement_amendment"]
            == original["payload"]["details"]["requirement_amendment"]
        )
        response = MessageEnvelope(
            source_id="owner",
            event_type=EventType.APPROVAL_RESPONSE,
            correlation_id="amendment-request",
            payload={
                "request_id": replay["payload"]["request_id"],
                "decision": "approve",
                "decided_by": "owner",
            },
        )
        await wire.publish(
            subject + ".response", response.model_dump_json().encode("utf-8")
        )
        assert await asyncio.wait_for(tasks[0], 5) == GateOutcome.RESUMED
        a = x.pool.read_source_commit(x.bid)
        assert dispatch.await_args.kwargs.get("source_commit") == a
        if (
            original["payload"]["details"]["requirement_amendment"]["amendment_state"]
            == "absent"
        ):
            assert (
                a is None and git(x.repo, "rev-parse", "planning/amendment") == x.plan
            )
        else:
            assert git(x.repo, "rev-parse", a + "^") == x.plan
            assert git(x.repo, "diff", "--name-only", x.plan, a) == DOC
    finally:
        _serve_deps_gating._reset_for_tests()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["blob", "directory", "submodule", "symlink"])
async def test_absence_requires_no_candidate_tree_entry(planned, kind):
    from forge.planning.requirement_amendments import validate_absence

    x = planned
    kwargs = dict(
        runner=x.runner,
        repo_path=str(x.repo),
        scope=[DOC],
        base=x.base,
        feature_id=x.feature,
    )
    assert (await validate_absence(plan=x.base, **kwargs))[
        "amendment_state"
    ] == "absent"
    manifest = x.repo / x.artifact
    manifest.parent.mkdir(parents=True, exist_ok=True)
    if kind == "blob":
        manifest.write_text("not a candidate")
    elif kind == "directory":
        manifest.mkdir()
        (manifest / "child").write_text("nested")
    elif kind == "symlink":
        manifest.symlink_to("../../requirements/runtime.contract")
    if kind == "submodule":
        git(
            x.repo, "update-index", "--add", "--cacheinfo", "160000", x.base, x.artifact
        )
    else:
        git(x.repo, "add", "-A")
    git(x.repo, "commit", "-qm", "candidate-shaped entry")
    q = git(x.repo, "rev-parse", "HEAD")
    with pytest.raises(AmendmentRefused, match="tree entry"):
        await validate_absence(plan=q, **kwargs)
    with pytest.raises(AmendmentRefused):
        await validate_absence(plan="0" * 40, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("planned", ["absent"], indirect=True)
async def test_absent_binding_rearm_and_ordinary_launch_are_explicit(
    planned, monkeypatch
):
    x = planned
    await test_real_gate_restart_reuses_frozen_card_and_common_launcher(x, monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "defect",
    [
        "declared-gate",
        "command-operand",
        "instructions",
        "executable",
        "symlink",
        "linked-parent",
        "binary",
        "tree-alias",
        "wrong-before",
        "changed-plan",
        "changed-mode",
        "aggregate-size",
    ],
)
async def test_raw_committed_roles_modes_aliases_and_bytes_refuse_all_or_nothing(
    planned, defect
):
    import yaml

    x = planned
    repo = x.repo
    path = DOC
    declaration = {
        "autobuild": {
            "player": {"required_documents": [DOC], "protected_paths": ["requirements"]}
        }
    }
    if defect in ("declared-gate", "command-operand", "instructions"):
        path = "reference/unusual.oracle"
        (repo / path).parent.mkdir()
        (repo / path).write_text(OLD)
        declaration["autobuild"]["player"] = {
            "required_documents": [path],
            "protected_paths": ["reference"],
        }
        if defect == "declared-gate":
            declaration["gates"] = [{"path": path}]
        if defect == "command-operand":
            declaration["validation_command"] = "verify --oracle " + path
        if defect == "instructions":
            declaration["instructions"] = [path]
    elif defect == "executable":
        (repo / DOC).chmod(0o755)
    elif defect == "symlink":
        (repo / DOC).unlink()
        (repo / DOC).symlink_to("sibling.contract")
    elif defect == "linked-parent":
        (repo / "requirements").rename(repo / "real-documents")
        (repo / "requirements").symlink_to("real-documents", target_is_directory=True)
    elif defect == "binary":
        (repo / DOC).write_bytes(b"\xff\x00")
    elif defect == "tree-alias":
        (repo / "Requirements").mkdir()
        (repo / "Requirements/runtime.contract").write_text(OLD)
    elif defect == "aggregate-size":
        (repo / DOC).write_text("a" * (65 * 1024))
    (repo / ".guardkit/config.yaml").write_text(yaml.safe_dump(declaration))
    git(repo, "add", "-A")
    git(repo, "commit", "--allow-empty", "-qm", "negative declaration fixture")
    base = git(repo, "rev-parse", "HEAD")
    plan = base
    value = proposal(base, path)
    if defect == "wrong-before":
        value["changes"][0]["before_sha256"] = "0" * 64
    if defect == "aggregate-size":
        value["changes"][0]["before_sha256"] = sha("a" * (65 * 1024))
        value["changes"][0]["after_text"] = "b" * (65 * 1024)
    if defect in ("changed-plan", "changed-mode"):
        if defect == "changed-plan":
            (repo / DOC).write_text("already changed\n")
        else:
            (repo / DOC).chmod(0o755)
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "unexpected planning mutation")
        plan = git(repo, "rev-parse", "HEAD")
    with pytest.raises(AmendmentRefused):
        await validate_proposal(
            runner=x.runner,
            repo_path=str(repo),
            raw=canonical(value),
            scope=[path],
            base=base,
            plan=plan,
            feature_id=x.feature,
        )
    assert x.pool.read_source_commit(x.bid) is None
    assert git(repo, "rev-parse", "planning/amendment") == x.plan


@pytest.mark.asyncio
async def test_prepared_route_cannot_borrow_original_scope_or_card(planned):
    x = planned
    # Model prepared intake being the first build for the already-reviewed
    # correlation; the ordinary fixture's preinserted build is not used.
    x.cx.execute("DELETE FROM builds WHERE build_id=?", (x.bid,))
    x.cx.commit()
    payload = BuildQueuedPayload(
        feature_id=x.feature,
        repo="example/project",
        branch="planning/amendment",
        feature_yaml_path=str(x.repo / f".guardkit/features/{x.feature}.yaml"),
        triggered_by="cli",
        originating_user="owner",
        correlation_id="amendment-request",
        requested_at=NOW,
        queued_at=NOW + timedelta(seconds=2),
        mode="mode-b",
        requirement_amendment={
            "approved": True,
            "changes": [{"path": DOC, "after_text": NEW}],
        },
    )
    bid = x.pool.record_pending_build(
        payload, admitted=SimpleNamespace(source_commit=x.plan, start_commit=x.base)
    )
    card = x.envelope.model_copy(
        update={
            "payload": {
                **x.envelope.payload,
                "details": {**x.envelope.payload["details"], "build_id": bid},
            }
        }
    )
    assert (
        "requirement_amendment" not in project_approval(x.cx, card).payload["details"]
    )
    assert await admit(x.cx, bid, x.runner, str(x.repo)) == x.plan
    with pytest.raises(AmendmentRefused):
        binding_for_build(x.cx, bid, required=True)
    assert git(x.repo, "rev-parse", "planning/amendment") == x.plan

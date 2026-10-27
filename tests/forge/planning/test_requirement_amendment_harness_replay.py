"""The owner-approved change of run E134 (FEAT-5BCA) reaches the harness intact.

E134 (24 October 2026): a Python dependency update whose owner-approved change
to ``docs/product/stack.md`` was refused by the harness's protected-file rule
turn after turn, until the turn limit ended the task. The agreed answer (step 1,
24 October 2026) is that the approved bytes arrive through Forge's existing
approved-amendment owner before the build, and the harness keeps refusing any
other write to the protected file.

This is the bounded replay of that case. It records the exact amendment and the
owner's approval, applies them through :func:`requirement_amendment_store.admit`,
opens the admitted commit in a build worktree, and runs guardkitfactory's own
backend there: the approved bytes are read and kept, and later unapproved writes,
edits and deletes are refused, sync and async. It skips when guardkitfactory is
not installed beside Forge.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from tests.forge.planning._plan_identity import plan_artifact_identity
import pytest
import pytest_asyncio
from nats_core.envelope import EventType, MessageEnvelope
from nats_core.events import ApprovalResponsePayload, BuildQueuedPayload

backend_config = pytest.importorskip(
    "guardkitfactory.harness.backend_config",
    reason="guardkitfactory is not installed beside Forge",
)

from forge.adapters.git.planning_runner import WorktreeGitRunner  # noqa: E402
from forge.adapters.sqlite.connect import connect_writer  # noqa: E402
from forge.lifecycle.migrations import apply_at_boot  # noqa: E402
from forge.lifecycle.persistence import SqliteLifecyclePersistence  # noqa: E402
from forge.planning.requirement_amendments import (  # noqa: E402
    canonical,
    sha,
    validate_proposal,
)
from forge.planning.requirement_amendment_store import (  # noqa: E402
    accept_response,
    admit,
    freeze_final_plan,
    project_approval,
    record_plan,
    skipped_qa,
)
from forge.planning.run_store import SqlitePlanningRunStore  # noqa: E402
from tests.forge.planning.test_requirement_amendments import git  # noqa: E402

NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)
FEATURE = "FEAT-5BCA"
CORRELATION = "e134-request"
STACK = "docs/product/stack.md"
BEFORE = "# Stack\n\nRuntime: Python 3.12\nDependencies: pinned versions\n"
APPROVED = "# Stack\n\nRuntime: Python 3.13\nDependencies: updated versions\n"
CONFIG = (
    "autobuild:\n"
    "  player:\n"
    f"    required_documents: [{STACK}]\n"
    "    protected_paths: [docs/product, .guardkit/config.yaml]\n"
)


@pytest_asyncio.fixture
async def approved_and_admitted(tmp_path: Path):
    """The E134 amendment, planned, approved by the owner and admitted."""
    repo = tmp_path / "host"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    for name, text in {
        STACK: BEFORE,
        ".guardkit/config.yaml": CONFIG,
        "pyproject.toml": "[project]\nname = 'example'\n",
    }.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    branch = "planning/e134"
    git(repo, "branch", branch)
    runner = WorktreeGitRunner(worktrees_root=tmp_path / "planning-worktrees")

    raw = canonical(
        {
            "schema_version": 1,
            "base_commit": base,
            "changes": [
                {
                    "path": STACK,
                    "before_sha256": sha(BEFORE),
                    "after_text": APPROVED,
                    "reason": "The dependency update the owner asked for.",
                }
            ],
        }
    )
    plan_files = {
        f".guardkit/features/{FEATURE}.requirement-amendments.json": raw,
        f".guardkit/features/{FEATURE}.yaml": f"id: {FEATURE}\ntasks: []\n",
    }
    written = await runner.prepare_branch_and_write_tree(
        str(repo), branch, plan_files, "Plan [skip ci]", expected_head=base
    )
    assert written.status == "success", written.stderr
    plan = written.sha

    cx = connect_writer(tmp_path / "ledger.db")
    apply_at_boot(cx)
    store = SqlitePlanningRunStore(cx, target_terminal_enabled=True)
    store.record_queued(
        CORRELATION,
        "owner",
        "owner",
        "Update the Python dependencies",
        "cli",
        target_repo="example/project",
        requirement_amendment_paths=(STACK,),
    )
    store.record_start_point(CORRELATION, start_commit=base, target_branch="main")
    checked = await validate_proposal(
        runner=runner,
        repo_path=str(repo),
        raw=raw,
        scope=[STACK],
        base=base,
        plan=plan,
        feature_id=FEATURE,
    )
    from forge.planning.driver import (
        PlanningRunDriver,
        _QA_FEATURE_GATE_STAGE,
        _QA_PASS_BARS_STAGE,
    )

    review = {
        "schema_version": 1,
        "decision": "approved",
        "criterion": "request_traceability",
        "criterion_score": 1.0,
        "coach_verdict": "GOOD",
        "artifact_identity": plan_artifact_identity(plan_files),
        "reviewed_after_rewrite": False,
    }
    record_plan(cx, CORRELATION, checked, review)
    store._record_event(
        correlation_id=CORRELATION,
        stage_label="feature-plan",
        status="approved",
        actor_identity="planning-driver",
        details_json=json.dumps(
            {
                "sha": plan,
                "feature_id": FEATURE,
                "target_repo": "example/project",
                "branch": branch,
                "plan_files": sorted(plan_files),
                "semantic_review": review,
            }
        ),
    )
    for stage in (_QA_PASS_BARS_STAGE, _QA_FEATURE_GATE_STAGE):
        store._record_event(
            correlation_id=CORRELATION,
            stage_label=stage,
            status="approved",
            actor_identity="planning-driver",
            details_json=json.dumps(
                {
                    "feature_id": FEATURE,
                    "skipped": True,
                    "reason_code": "fixture-no-registration",
                    "requirement_amendment_qa": skipped_qa(
                        cx, CORRELATION, branch, plan
                    ),
                }
            ),
        )
    await freeze_final_plan(
        cx,
        CORRELATION,
        runner,
        str(repo),
        pass_stage=_QA_PASS_BARS_STAGE,
        gate_stage=_QA_FEATURE_GATE_STAGE,
    )
    pool = SqliteLifecyclePersistence(connection=cx)
    build_id = pool.record_pending_build(
        BuildQueuedPayload(
            feature_id=FEATURE,
            repo="example/project",
            branch=branch,
            feature_yaml_path=str(repo / f".guardkit/features/{FEATURE}.yaml"),
            triggered_by="forge-internal",
            originating_user="owner",
            correlation_id=CORRELATION,
            requested_at=NOW,
            queued_at=NOW,
            mode="mode-b",
        )
    )
    cx.execute(
        "UPDATE builds SET status='PAUSED', pending_approval_request_id='request-1' "
        "WHERE build_id=?",
        (build_id,),
    )
    cx.commit()
    card = MessageEnvelope(
        source_id="forge",
        event_type=EventType.APPROVAL_REQUEST,
        correlation_id=CORRELATION,
        payload={
            "request_id": "request-1",
            "agent_id": "forge",
            "action_description": "Build approval",
            "risk_level": "medium",
            "details": {
                "build_id": build_id,
                "feature_id": FEATURE,
                "stage_label": "autobuild",
                "gate_mode": "MANDATORY_HUMAN_APPROVAL",
                "rationale": "Approve build",
                "resume_options": ["approve", "reject", "defer"],
            },
        },
    )
    # The owner approves the exact change on the build card.
    projected = project_approval(cx, card)
    assert accept_response(
        cx,
        build_id,
        projected,
        ApprovalResponsePayload(
            request_id="request-1", decision="approve", decided_by="owner", decided_at=NOW
        ),
        "owner",
    )
    # Forge's existing owner of approved amendments applies the bytes.
    source = await admit(cx, build_id, runner, str(repo))
    assert source and source != plan
    yield repo, source
    cx.close()


@pytest.mark.asyncio
async def test_e134_approved_bytes_are_read_and_kept_and_later_writes_refused(
    approved_and_admitted, tmp_path: Path
) -> None:
    repo, source = approved_and_admitted
    assert git(repo, "show", f"{source}:{STACK}") == APPROVED.rstrip()

    # The build's worktree at the admitted commit, where the harness works.
    worktree = repo / ".guardkit" / "worktrees" / FEATURE
    git(repo, "worktree", "add", "-q", "--detach", str(worktree), source)
    backend = backend_config.build_autobuild_backend(
        worktree, protected_paths=["docs/product", ".guardkit/config.yaml"]
    )
    stack = worktree / STACK

    read = backend.read(str(stack))
    assert read.error is None
    assert read.file_data["content"] == APPROVED

    async def later_async_writes() -> list[str | None]:
        return [
            (await backend.awrite(str(stack), BEFORE)).error,
            (await backend.aedit(str(stack), "Python 3.13", "Python 3.12")).error,
            (await backend.adelete(str(stack))).error,
        ]

    errors = [
        backend.write(str(stack), BEFORE).error,
        backend.edit(str(stack), "Python 3.13", "Python 3.12").error,
        backend.delete(str(stack)).error,
        *(await later_async_writes()),
    ]
    assert len(errors) == 6
    assert all(error and "This file is protected" in error for error in errors)
    # The approved bytes are kept, exactly.
    assert stack.read_text() == APPROVED
    assert git(worktree, "status", "--porcelain") == ""

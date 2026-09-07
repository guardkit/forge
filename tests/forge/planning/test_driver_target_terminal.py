"""Lane B / Phase E1 (B2) — the forge target-terminal spec/plan legs.

Exercises the driver's flag-ON machine chain after PLANNED_HANDOFF:

    RUNNING → (handoff written) → FEATURE_SPEC (007 → triple → normalizer)
            → FEATURE_PLAN (008 → plan tree → guardkit feature validate)

against a REAL v4 SQLite store + real planning gate adapters, with fakes at the
wire seams (specialist dispatch, the two oracles) exactly like ``test_driver``.
The headline hermetic gate — a full stubbed round-trip from a fixture handoff to
a validate-green plan tree **in a scratch git repo** — uses the REAL
``WorktreeGitRunner`` so the artifacts genuinely land on the branch; the failure
paths use a recording fake git runner for precision.

Covered:
- flag OFF is a no-op: the chain still terminates at PLANNED_HANDOFF and the
  target-terminal collaborators are never called
- the full round-trip: spec triple + plan tree committed, plan validated, run
  parked at FEATURE_PLAN (the B2 endpoint; the build trigger is B3)
- RV-1: the plan leg asserts the SUPPLIED feature id
- failure paths → loud FAILED + notification: 007 error, invalid spec artifacts,
  normalizer red, feature-id mismatch, invalid plan artifacts, validate red,
  unwired collaborators
- idempotent re-drive (no re-dispatch)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from forge.adapters.git.models import GitOpResult
from forge.adapters.git.planning_runner import WorktreeGitRunner
from forge.adapters.sqlite import connect as sqlite_connect
from forge.config.models import PlanningConfig, TargetTerminalConfig
from forge.gating.identity import derive_request_id
from forge.lifecycle import migrations
from forge.planning import driver as driver_module
from forge.planning.driver import (
    BuildTriggerResult,
    PlanningDriverDeps,
    PlanningRunDriver,
)
from forge.planning.gate_adapters import build_planning_gate_adapters
from forge.planning.run_store import SqlitePlanningRunStore, TransitionRefused
from forge.planning.states import PlanningState
from forge.planning.target_terminal_tools import ToolOutcome
from nats_core.events import ApprovalResponsePayload

CID = "tt-run-0001"
PLAN_RUN_ID = f"plan-{CID}"
ORIGINATOR = "U0RIGINATOR"
TARGET_REPO = "guardkit/api_test"

#: A valid AUTHLESS feature-grain pass-bar seed (guardkit `/feature-spec`
#: `pass-bar-seed-*.yaml` shape). The default 007 fakes ship it so the machine
#: chain reaches BUILD_QUEUED — the B4 round-19 contract requires a seed to
#: register the per-task bars the B2 precondition demands.
_AUTHLESS_SEED_YAML = (
    "format_version: '2.0'\n"
    "feature_slug: stats-endpoint\n"
    "auth_surface_bearing: false\n"
    "preconditions:\n"
    "- suite_green_vs_ledger\n"
    "criteria:\n"
    "- id: stats-AC-001\n"
    "  text: A GET request to /stats returns the statistics\n"
    "  class: machine\n"
    "  evidence_kind: json\n"
    "  runbook_ref: null\n"
)


# ---------------------------------------------------------------------------
# Fixtures / doubles
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> SqlitePlanningRunStore:
    cx = sqlite_connect.connect_writer(tmp_path / "tt.db")
    migrations.apply_at_boot(cx)
    return SqlitePlanningRunStore(cx, target_terminal_enabled=True)


class FakePublisher:
    def __init__(self) -> None:
        self.envelopes: list[Any] = []

    async def publish_request(self, envelope: Any) -> None:
        self.envelopes.append(envelope)


class AuthCardUndeliverablePublisher(FakePublisher):
    """A wire that swallows every card EXCEPT the auth-confirmation one, which
    it refuses — the "the owner can never answer" case."""

    async def publish_request(self, envelope: Any) -> None:
        self.envelopes.append(envelope)
        if envelope.payload["details"].get("checkpoint_type") == (
            "auth_surface_confirmation"
        ):
            raise RuntimeError("broker refused the auth confirmation card")


class ScriptedSubscriber:
    def __init__(self, script: list[Any], armed: asyncio.Event | None) -> None:
        self._script = script
        self._armed = armed

    async def await_response(self, build_id: str, **kwargs: Any) -> Any:
        if self._armed is not None:
            self._armed.set()
        if not self._script:
            return None
        return self._script.pop(0)


class AutoApproveSubscriber:
    """Answers YES to whichever door is asking.

    The default double for tests whose subject is NOT the pause: it derives the
    card's ``request_id`` from the stage label and attempt the door itself asked
    on, so the same double answers the spec digest card, the sign-in card and
    the product-docs checkpoint without any test knowing which is live.
    """

    def __init__(self, armed: asyncio.Event | None, approver: str = ORIGINATOR) -> None:
        self._armed = armed
        self._approver = approver

    async def await_response(self, build_id: str, **kwargs: Any) -> Any:
        if self._armed is not None:
            self._armed.set()
        return ApprovalResponsePayload(
            request_id=derive_request_id(
                build_id=build_id,
                stage_label=kwargs.get("stage_label", "product_docs"),
                attempt_count=int(kwargs.get("attempt_count") or 0),
            ),
            decision="approve",
            decided_by=self._approver,
        )


class FakeSecondOpinion:
    async def get_summary_for_approval(self, **kwargs: Any) -> dict[str, Any]:
        return {"title": "PO docs"}


class RecordingGitRunner:
    """Fake git runner recording tree writes; runs the pre_commit hook."""

    def __init__(self, *, tree_fail: bool = False) -> None:
        self.single_calls: list[dict[str, Any]] = []
        self.tree_calls: list[dict[str, Any]] = []
        self.tree_fail = tree_fail
        # (branch, file_path) -> content, so the plan leg's read-back of the
        # committed spec triple works against the fake exactly as the real
        # WorktreeGitRunner reads it off the branch.
        self._branch_files: dict[str, dict[str, str]] = {}

    async def prepare_branch_and_write(
        self, repo_path: str, branch: str, file_path: str, content: str
    ) -> GitOpResult:
        self.single_calls.append(
            {"branch": branch, "file_path": file_path, "content": content}
        )
        self._branch_files.setdefault(branch, {})[file_path] = content
        return GitOpResult(
            status="success",
            operation="prepare_branch_and_write",
            sha="handoff-sha",
            exit_code=0,
        )

    async def read_file_from_branch(
        self, *, repo_path: str, branch: str, file_path: str
    ) -> str | None:
        return self._branch_files.get(branch, {}).get(file_path)

    async def prepare_branch_and_write_tree(
        self,
        repo_path: str,
        branch: str,
        files: Any,
        message: str,
        *,
        pre_commit: Any = None,
    ) -> GitOpResult:
        # Materialise the files into a temp dir so the pre_commit hook can run
        # against a real on-disk tree, then honour its verdict.
        import tempfile

        hook_detail = ""
        if pre_commit is not None:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                for rel, content in files.items():
                    p = root / rel
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(content, encoding="utf-8")
                result = await pre_commit(root)
                if not result.ok:
                    self.tree_calls.append(
                        {"branch": branch, "files": dict(files), "hook_ok": False}
                    )
                    return GitOpResult(
                        status="failed",
                        operation="prepare_branch_and_write_tree",
                        stderr=f"pre-commit refused: {result.detail}",
                        exit_code=-1,
                    )
                hook_detail = result.detail
        self.tree_calls.append(
            {"branch": branch, "files": dict(files), "hook_ok": True, "detail": hook_detail}
        )
        if self.tree_fail:
            return GitOpResult(
                status="failed",
                operation="prepare_branch_and_write_tree",
                stderr="simulated tree write failure",
                exit_code=1,
            )
        # Commit succeeded — the files are now readable off the branch.
        self._branch_files.setdefault(branch, {}).update(
            {str(k): str(v) for k, v in files.items()}
        )
        return GitOpResult(
            status="success",
            operation="prepare_branch_and_write_tree",
            sha="tree-sha",
            exit_code=0,
        )


#: The one worked example every default spec fixture carries.
_FIXTURE_FEATURE = "Feature: stats\n  Scenario: ok\n    Given a\n"


def _digest_yaml(slug: str) -> str:
    """A VALID spec digest for :data:`_FIXTURE_FEATURE`.

    One entry per worked example, the title copied verbatim, the labels
    verbatim (there are none), one plain-English sentence, and the feature slug
    the triple shares. This is what the deterministic check proves the card
    against, so a fixture that drifts from the .feature fails the leg — which is
    the whole point.
    """
    return (
        f"feature: {slug}\n"
        "generated: '2026-08-14T10:00:00Z'\n"
        "scenarios:\n"
        "- title: ok\n"
        "  tags: []\n"
        "  sentence: The service answers the request in the ordinary way.\n"
        "assumptions: []\n"
    )


def _po_result() -> Any:
    return SimpleNamespace(
        outcome=SimpleNamespace(value="completed"),
        coach_score=0.9,
        criterion_breakdown=[],
        detection_findings=(),
        role_output={"title": "docs", "problem_statement": "ship a thing"},
        reason=None,
    )


def _spec_result(files: dict[str, str] | None = None, slug: str = "stats-endpoint") -> Any:
    return SimpleNamespace(
        outcome=SimpleNamespace(value="completed"),
        role_output={
            "slug": slug,
            # The feature-grain seed rides alongside the triple (a tolerated
            # extra); forge captures it at the spec commit and specialises it.
            f"pass-bar-seed-{slug}.yaml": _AUTHLESS_SEED_YAML,
            "files": files
            if files is not None
            else {
                f"features/{slug}/{slug}.feature": _FIXTURE_FEATURE,
                f"features/{slug}/{slug}_assumptions.yaml": "assumptions: []\n",
                f"features/{slug}/{slug}_summary.md": "# summary\n",
                f"features/{slug}/{slug}_digest.yaml": _digest_yaml(slug),
            },
        },
        reason=None,
    )


def _plan_result(feature_id: str, files: dict[str, str] | None = None) -> Any:
    return SimpleNamespace(
        outcome=SimpleNamespace(value="completed"),
        role_output={
            "feature_id": feature_id,
            "files": files
            if files is not None
            else {
                f"features/stats-endpoint/{feature_id}.yaml": (
                    f"id: {feature_id}\ntasks:\n- id: TASK-STAT-001\n"
                ),
                "tasks/TASK-STAT-001.md": "# task\n",
            },
        },
        reason=None,
    )


def _spec_result_native(slug: str = "stats-endpoint", accepted: bool = True) -> Any:
    """The DEPLOYED 007 reply shape: ``role_output`` is the NATIVE artifact map
    keyed by BARE FILENAME with the three contract suffixes PLUS extras (a
    pass-bar-seed-*.yaml and the validation.json data channel) — NOT the invented
    'files' mapping. This is what the reply parser hands the driver post-unwrap.
    """
    return SimpleNamespace(
        outcome=SimpleNamespace(value="completed"),
        role_output={
            f"{slug}.feature": _FIXTURE_FEATURE,
            f"{slug}_assumptions.yaml": "assumptions: []\n",
            f"{slug}_summary.md": "# summary\n",
            f"{slug}_digest.yaml": _digest_yaml(slug),
            f"pass-bar-seed-{slug}.yaml": _AUTHLESS_SEED_YAML,
            "validation.json": json.dumps(
                {"accepted": accepted, "errors": [] if accepted else ["bad"],
                 "gates_run": ["gherkin_backstop"]}
            ),
        },
        reason=None,
    )


def _plan_result_native(feature_id: str, slug: str = "stats-endpoint",
                        accepted: bool = True) -> Any:
    """The DEPLOYED 008 reply shape: ``role_output`` is the NATIVE artifact map
    whose keys are ALREADY repo-relative paths (.guardkit/features/<id>.yaml,
    tasks/backlog/**, qa/*) PLUS the validation.json channel."""
    return SimpleNamespace(
        outcome=SimpleNamespace(value="completed"),
        role_output={
            # The real 008 map lists tasks in the feature YAML but emits NO
            # per-task qa/pass-bar-*.yaml (the round-19 gap forge now fills).
            f".guardkit/features/{feature_id}.yaml": (
                f"id: {feature_id}\ntasks:\n- id: TASK-STAT-001\n"
            ),
            f"tasks/backlog/{slug}/IMPLEMENTATION-GUIDE.md": "# guide\n",
            f"tasks/backlog/{slug}/TASK-STAT-001.md": "# task\n",
            "validation.json": json.dumps(
                {"accepted": accepted, "errors": [] if accepted else ["bad"],
                 "gates_run": ["feature_validate"]}
            ),
        },
        reason=None,
    )


def _error_result(outcome: str = "error", reason: str = "boom") -> Any:
    return SimpleNamespace(
        outcome=SimpleNamespace(value=outcome), role_output={}, reason=reason
    )


def _request_id(attempt: int = 0) -> str:
    return derive_request_id(
        build_id=PLAN_RUN_ID, stage_label="product_docs", attempt_count=attempt
    )


def _approve() -> ApprovalResponsePayload:
    return ApprovalResponsePayload(
        request_id=_request_id(0), decision="approve", decided_by=ORIGINATOR
    )


class _Harness:
    def __init__(self, driver: PlanningRunDriver, ctx: dict[str, Any]) -> None:
        self.driver = driver
        self.ctx = ctx


class SharedScriptSubscriberFactory:
    """One shared response script across EVERY wait in a run.

    The default per-call factory hands each waiter a fresh single-approve
    script; a run that pauses TWICE (the product-docs checkpoint, then the
    auth-confirmation door) needs the answers to arrive in ORDER, from one
    script, exactly as one human answering two cards would.
    """

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.approvers: list[Any] = []

    def __call__(self, expected_approver: Any, armed: Any) -> ScriptedSubscriber:
        self.approvers.append(expected_approver)
        return ScriptedSubscriber(self.script, armed)


def _make_driver(
    store: SqlitePlanningRunStore,
    *,
    target_terminal_enabled: bool = True,
    git_runner: Any | None = None,
    repo_path: str = "/srv/repos/api_test",
    subscriber_factory: Any | None = None,
    originator_wait_seconds: int = 3600,
    publisher: FakePublisher | None = None,
    spec_result: Any | None = None,
    spec_result_factory: Any | None = None,
    digest_review: Any | None = None,
    plan_result: Any | None = None,
    plan_result_factory: Any | None = None,
    spec_dispatch: Any | None = None,
    plan_dispatch: Any | None = None,
    normalize: Any | None = None,
    validate: Any | None = None,
    validate_fn: Any | None = None,
    pass_bar_validate: Any | None = None,
    pass_bar_validate_fn: Any | None = None,
    gate_registry_validate: Any | None = None,
    gate_registry_validate_fn: Any | None = None,
    normalize_stamps_fn: Any | None = None,
    wire_legs: bool = True,
    build_trigger_result: Any | None = None,
    build_trigger_fn: Any | None = None,
    wire_build_trigger: bool = True,
    rewrite_on_refusal: bool | None = None,
    classify_fn: Any | None = None,
    git_runner_for_repo: Any | None = None,
) -> _Harness:
    from datetime import UTC, datetime

    def clock() -> datetime:
        return datetime.now(UTC)

    repository, state_machine = build_planning_gate_adapters(store, clock=clock)
    publisher = publisher or FakePublisher()
    notifications: list[tuple[str, str, str]] = []
    counters = {
        "po": 0,
        "spec": 0,
        "plan": 0,
        "normalize": 0,
        "validate": 0,
        "pass_bar_validate": 0,
        "gate_registry_validate": 0,
        "build_trigger": 0,
    }
    validated_bars: list[str] = []
    validated_registries: list[str] = []

    def _default_subscriber_factory(
        expected_approver: Any, armed: Any
    ) -> AutoApproveSubscriber:
        return AutoApproveSubscriber(armed)

    subscriber_factory = subscriber_factory or _default_subscriber_factory

    async def dispatch_po(*, plan_run_id: str, correlation_id: str, **_: Any) -> Any:
        counters["po"] += 1
        return _po_result()

    async def _dispatch_spec(
        *,
        plan_run_id: str,
        correlation_id: str,
        spec_input: str,
        revision_of: dict[str, str] | None = None,
        validate_feedback: str | None = None,
    ) -> Any:
        counters["spec"] += 1
        assert spec_input  # forge supplies the committed handoff content
        # A rewrite round carries the owner's note VERBATIM and the prior
        # artifact set; a first round carries neither.
        counters.setdefault("spec_revisions", [])
        if revision_of is not None or validate_feedback is not None:
            counters["spec_revisions"].append(
                {"revision_of": revision_of, "validate_feedback": validate_feedback}
            )
        if spec_result_factory is not None:
            return spec_result_factory(validate_feedback)
        return spec_result if spec_result is not None else _spec_result()

    async def _dispatch_plan(
        *,
        plan_run_id: str,
        correlation_id: str,
        feature_id: str,
        spec_feature: str,
        spec_summary: str,
        target_repo_descriptor: dict[str, Any],
        spec_assumptions: str | None = None,
        spec_feature_paths: list[str] | None = None,
    ) -> Any:
        counters["plan"] += 1
        # Reject-on-missing, exactly like the real specialist command router:
        # the 008 contract of record (specialist-agent architect/modes/
        # feature_plan.py) requires feature_id + the spec triple CONTENTS +
        # the structured descriptor. A blank/missing required arg is a contract
        # violation the stub must NOT silently accept.
        assert feature_id.startswith("FEAT-"), "RV-1: the SUPPLIED minted id"
        assert spec_feature and spec_feature.strip(), "spec_feature content required"
        assert spec_summary and spec_summary.strip(), "spec_summary content required"
        assert isinstance(target_repo_descriptor, dict)
        assert target_repo_descriptor.get("repo") == TARGET_REPO
        assert isinstance(target_repo_descriptor.get("test_roots"), list)
        # forge never invents undefined schema fields.
        assert set(target_repo_descriptor) <= {
            "repo",
            "default_branch",
            "test_roots",
            "sibling_repos",
            "stack",
            # Present only when the target repo keeps a rules file
            # (docs/architecture-rules.yaml); defined in the schema 2026-08-31.
            "architecture_rules",
        }
        counters["last_feature_id"] = feature_id
        counters["last_descriptor"] = target_repo_descriptor
        counters["last_spec_assumptions"] = spec_assumptions
        counters["last_spec_feature_paths"] = spec_feature_paths
        if plan_result is not None:
            return plan_result
        if plan_result_factory is not None:
            return plan_result_factory(feature_id)
        return _plan_result(feature_id)

    async def _normalize(worktree: Path, feature_rel: str) -> ToolOutcome:
        counters["normalize"] += 1
        # The spec .feature file is on disk in the worktree.
        assert (worktree / feature_rel).is_file()
        return normalize if normalize is not None else ToolOutcome(ok=True)

    async def _validate(worktree: Path, feature_id: str) -> ToolOutcome:
        counters["validate"] += 1
        counters.setdefault("order", []).append("validate")
        # A dynamic oracle (e.g. the real guardkit smoke-gate path check that
        # inspects the worktree) takes precedence over a static ToolOutcome.
        if validate_fn is not None:
            return await validate_fn(worktree, feature_id)
        return validate if validate is not None else ToolOutcome(ok=True)

    async def _validate_pass_bar(worktree: Path, bar_rel: str) -> ToolOutcome:
        counters["pass_bar_validate"] += 1
        validated_bars.append(bar_rel)
        # A dynamic oracle (the schema-faithful pass-bar check) takes precedence.
        if pass_bar_validate_fn is not None:
            return await pass_bar_validate_fn(worktree, bar_rel)
        return pass_bar_validate if pass_bar_validate is not None else ToolOutcome(ok=True)

    async def _validate_gate_registry(worktree: Path, registry_rel: str) -> ToolOutcome:
        counters["gate_registry_validate"] += 1
        validated_registries.append(registry_rel)
        # A dynamic oracle (the schema-faithful gate-registry check) takes
        # precedence over a static ToolOutcome.
        if gate_registry_validate_fn is not None:
            return await gate_registry_validate_fn(worktree, registry_rel)
        return (
            gate_registry_validate
            if gate_registry_validate is not None
            else ToolOutcome(ok=True)
        )

    build_triggers: list[dict[str, Any]] = []

    async def _dispatch_build_trigger(
        *,
        plan_run_id: str,
        correlation_id: str,
        feature_id: str,
        target_repo: str,
        branch: str,
        plan_files: list[str],
        originating_user: str | None,
    ) -> BuildTriggerResult:
        counters["build_trigger"] += 1
        # Record the "queue onto the Mode B bus" call — the observable
        # pre-gate step the real gate pause hangs off (B3 test-bus fixture).
        build_triggers.append(
            {
                "feature_id": feature_id,
                "target_repo": target_repo,
                "branch": branch,
                "plan_files": list(plan_files),
                "originating_user": originating_user,
            }
        )
        assert feature_id.startswith("FEAT-")
        assert target_repo == TARGET_REPO
        assert branch == f"planning/{CID}"
        if build_trigger_result is not None:
            return build_trigger_result
        return BuildTriggerResult(queued=True, build_id="build-1")

    mentions: list[tuple[str, bool]] = []

    async def publish_notification(
        cid: str, message: str, level: str, *, mention: bool = True
    ) -> None:
        notifications.append((cid, message, level))
        mentions.append((message, mention))

    cfg = PlanningConfig(
        enabled=True,
        target_repo_paths={TARGET_REPO: repo_path},
        target_terminal=TargetTerminalConfig(enabled=target_terminal_enabled),
        originator_wait_seconds=originator_wait_seconds,
        **({"digest_review": digest_review} if digest_review is not None else {}),
        **(
            {"rewrite_on_refusal": rewrite_on_refusal}
            if rewrite_on_refusal is not None
            else {}
        ),
    )

    deps = PlanningDriverDeps(
        store=store,
        repository=repository,
        state_machine=state_machine,
        approval_publisher=publisher,
        subscriber_factory=subscriber_factory,
        dispatch_product_owner=dispatch_po,
        second_opinion_provider=FakeSecondOpinion(),
        git_runner=git_runner or RecordingGitRunner(),
        # Sandbox first (2026-09-07): the plan leg's git BY TARGET REPO. None
        # (the default) = every leg uses the single runner above, as before.
        git_runner_for_repo=git_runner_for_repo,
        planning_config=cfg,
        clock=clock,
        publish_notification=publish_notification,
        dispatch_feature_spec=(spec_dispatch or _dispatch_spec) if wire_legs else None,
        dispatch_feature_plan=(plan_dispatch or _dispatch_plan) if wire_legs else None,
        normalize_feature_spec=_normalize if wire_legs else None,
        validate_feature_plan=_validate if wire_legs else None,
        # THE STAMP NORMALIZER hook: unwired by default (the not-wired path,
        # receipted); the stamp tests inject their own.
        normalize_stamps=normalize_stamps_fn,
        # THE PROVABILITY CHECK BEFORE THE CARD (Part K): unwired by default.
        classify_scenarios=classify_fn,
        validate_pass_bar=_validate_pass_bar if wire_legs else None,
        validate_gate_registry=_validate_gate_registry if wire_legs else None,
        dispatch_build_trigger=(
            (build_trigger_fn or _dispatch_build_trigger)
            if wire_build_trigger
            else None
        ),
    )
    return _Harness(
        PlanningRunDriver(deps),
        {
            "notifications": notifications,
            "mentions": mentions,
            "counters": counters,
            "git": deps.git_runner,
            "build_triggers": build_triggers,
            "validated_bars": validated_bars,
            "validated_registries": validated_registries,
            "publisher": publisher,
        },
    )


def _queue(store: SqlitePlanningRunStore) -> None:
    store.record_queued(
        correlation_id=CID,
        originating_user=ORIGINATOR,
        expected_approver=ORIGINATOR,
        request_text="add a GET /stats endpoint",
        triggered_by="jarvis",
        target_repo=TARGET_REPO,
    )


# ---------------------------------------------------------------------------
# Flag OFF — byte-for-byte no-op (PLANNED_HANDOFF stays the terminal)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_off_terminates_at_planned_handoff_and_never_calls_legs(
    tmp_path: Path,
) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "off.db")
    migrations.apply_at_boot(cx)
    off_store = SqlitePlanningRunStore(cx, target_terminal_enabled=False)
    _queue(off_store)
    h = _make_driver(off_store, target_terminal_enabled=False)

    await h.driver.drive(CID)

    run = off_store.get_run(CID)
    assert run["state"] == PlanningState.PLANNED_HANDOFF.value
    # The target-terminal collaborators were never consulted.
    assert h.ctx["counters"]["spec"] == 0
    assert h.ctx["counters"]["plan"] == 0
    assert h.ctx["counters"]["normalize"] == 0
    assert h.ctx["counters"]["validate"] == 0
    assert h.ctx["counters"]["build_trigger"] == 0


# ---------------------------------------------------------------------------
# The headline hermetic round-trip — in a real scratch git repo
# ---------------------------------------------------------------------------


def _init_scratch_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    env = {
        **__import__("os").environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, env=env)
    (path / "README.md").write_text("scratch\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True, env=env)


@pytest.mark.asyncio
async def test_full_round_trip_commits_spec_and_plan_to_scratch_repo(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")

    _queue(store)
    h = _make_driver(store, git_runner=git, repo_path=str(repo))

    await h.driver.drive(CID)

    run = store.get_run(CID)
    # B3 endpoint: plan validated, build queued, run reaches the target terminal.
    assert run["state"] == PlanningState.BUILD_QUEUED.value

    branch = f"planning/{CID}"

    def _show(path: str) -> str:
        return subprocess.run(
            ["git", "show", f"{branch}:{path}"],
            cwd=repo,
            capture_output=True,
            text=True,
        ).stdout

    # The 007 input (handoff), the spec triple, and the plan tree are all on the
    # SAME branch — the Factory-2 sequence, machine-made.
    assert "add a GET /stats endpoint" in _show(f"feature_spec_inputs/{CID}.md")
    assert "Feature: stats" in _show("features/stats-endpoint/stats-endpoint.feature")
    feature_id = h.ctx["counters"]["last_feature_id"]
    assert f"id: {feature_id}" in _show(f"features/stats-endpoint/{feature_id}.yaml")

    # Both oracles ran; each leg dispatched exactly once; the build was queued.
    assert h.ctx["counters"] ["spec"] == 1
    assert h.ctx["counters"]["plan"] == 1
    assert h.ctx["counters"]["normalize"] == 1
    assert h.ctx["counters"]["validate"] == 1
    assert h.ctx["counters"]["build_trigger"] == 1

    # The build trigger was called with the minted feature id + the committed
    # branch — the queue-onto-Mode-B step the pre-dispatch gate hangs off.
    assert len(h.ctx["build_triggers"]) == 1
    trig = h.ctx["build_triggers"][0]
    assert trig["feature_id"] == feature_id
    assert trig["branch"] == branch
    assert any(f.endswith(".yaml") for f in trig["plan_files"])

    # Durable leg events landed.
    labels = {e["stage_label"] for e in store.list_events(CID)}
    assert "feature-spec" in labels
    assert "feature-plan" in labels
    assert "build-queued" in labels


@pytest.mark.asyncio
async def test_full_round_trip_on_the_DEPLOYED_native_reply_shape(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """The B4 regression proof: drive the whole sequence with the DEPLOYED
    reply shape (007 suffix-keyed artifact map + 008 repo-relative paths, each
    with its validation.json channel) — NOT the invented 'files' mapping. The
    live run f6781ad4 COMPLETED coach=0.91 yet forge failed it "no three-file
    spec contract" on exactly this shape; this test pins the fix end-to-end.
    """
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")

    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_native(),
        plan_result=None,  # built per-drive from the minted feature id
        plan_result_factory=_plan_result_native,
    )

    await h.driver.drive(CID)

    run = store.get_run(CID)
    assert run["state"] == PlanningState.BUILD_QUEUED.value

    branch = f"planning/{CID}"

    def _show(path: str) -> str:
        return subprocess.run(
            ["git", "show", f"{branch}:{path}"],
            cwd=repo,
            capture_output=True,
            text=True,
        ).stdout

    # 007: the triple committed under features/<slug>/ (slug from the .feature
    # stem); the pass-bar seed + validation.json are NEVER on the branch.
    assert "Feature: stats" in _show(
        "features/stats-endpoint/stats-endpoint.feature"
    )
    assert _show("features/stats-endpoint/pass-bar-seed-stats-endpoint.yaml") == ""
    # 008: the native repo-relative plan tree committed verbatim; the feature
    # YAML lives at .guardkit/features/<id>.yaml (the real 008 layout).
    feature_id = h.ctx["counters"]["last_feature_id"]
    assert f"id: {feature_id}" in _show(f".guardkit/features/{feature_id}.yaml")
    assert "# task" in _show("tasks/backlog/stats-endpoint/TASK-STAT-001.md")
    assert _show("validation.json") == ""  # the data channel never commits

    # The build trigger got the native feature YAML path.
    trig = h.ctx["build_triggers"][0]
    assert any(
        f == f".guardkit/features/{feature_id}.yaml" for f in trig["plan_files"]
    )


@pytest.mark.asyncio
async def test_native_spec_validation_flag_is_advisory_and_proceeds(
    store: SqlitePlanningRunStore, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """VALIDATION CHANNEL (C5): a 007 reply whose validation.json reports
    accepted:false is ADVISORY self-check data, not an oracle — the leg logs
    the errors loudly (WARNING, verbatim) and proceeds to the REAL oracles
    (normalizer + guardkit validate). The gold hermetic run itself shipped
    accepted:false on a minor count note while the coach scored 0.985; the
    C5 bounded revision re-invoke is the named follow-on consumer."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_native(accepted=False),
    )
    with caplog.at_level("WARNING"):
        await h.driver.drive(CID)
    # The run proceeds through the real oracles to BUILD_QUEUED.
    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert h.ctx["counters"]["plan"] == 1  # the plan leg WAS reached
    # The self-reported error was surfaced loudly and verbatim.
    assert any(
        "validation.json self-check" in r.message and "ADVISORY" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_plan_leg_threads_spec_contents_and_discovered_descriptor(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """The 008 leg gets the committed spec CONTENTS + an honestly-built descriptor.

    ``test_roots`` is the EXACT ``tests/<name>`` set discovered from the target
    checkout by REUSING guardkit's own ``discover_test_roots`` (the same
    function the pre-commit ``feature validate`` oracle reports as its
    "Available test roots") — api_test-shaped: ``tests/health`` + ``tests/users``
    (no ``tests/smoke``). ``spec_feature``/``spec_summary`` are the contents read
    back off the branch.
    """
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    # api_test-shaped test tree: real per-suite roots, NOT a bare ``tests/``.
    (repo / "tests" / "health").mkdir(parents=True)
    (repo / "tests" / "users").mkdir(parents=True)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")

    _queue(store)
    h = _make_driver(store, git_runner=git, repo_path=str(repo))

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    # The descriptor forge built for 008 carries the EXACT discovered roots and
    # only schema-defined fields (repo + test_roots) — no invented keys, and
    # crucially NOT the shallow ``["tests"]`` that let 008 invent ``tests/smoke``.
    descriptor = h.ctx["counters"]["last_descriptor"]
    assert descriptor == {
        "repo": TARGET_REPO,
        "test_roots": ["tests/health", "tests/users"],
    }
    # The optional assumptions content was threaded (the default spec triple
    # carries an _assumptions.yaml).
    assert h.ctx["counters"]["last_spec_assumptions"] == "assumptions: []\n"
    # ...and so was WHERE the specification sits, not only what it says
    # (2026-08-22). The plan YAML has to declare that location under
    # ``feature_files:``; before this, forge sent the contents alone and the
    # plan-writer, asked for a fact nobody gave it, built a folder name out of
    # the feature's title. Six of the ten captured plans that wrote the key
    # named a folder that does not exist. This is the exact list forge itself
    # committed one leg earlier and hands to the stamp normalizer below.
    assert h.ctx["counters"]["last_spec_feature_paths"] == [
        "features/stats-endpoint/stats-endpoint.feature"
    ]


@pytest.mark.asyncio
async def test_the_location_sent_to_the_writer_is_the_one_forge_checks_against(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """ONE list, two uses — and that is the whole point of the fix.

    Forge sends the plan-writer the specification's committed path so the plan
    can declare it, and forge later checks the plan's declaration against the
    same path at plan-commit (``declare_feature_files_if_absent``). If those two
    were computed separately they could drift, and a drift would put forge in
    the position of refusing a plan for failing to match a path forge never
    sent. They are the SAME list, computed once, above the dispatch.

    The spec triple here is committed under a NON-DEFAULT slug, so a path
    derived from the feature title could not accidentally match.
    """
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    (repo / "tests" / "health").mkdir(parents=True)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")

    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result(slug="users-count-endpoint"),
    )

    await h.driver.drive(CID)

    sent = h.ctx["counters"]["last_spec_feature_paths"]
    assert sent == [
        "features/users-count-endpoint/users-count-endpoint.feature"
    ], "the writer is told where the specification actually is"
    # Only the .feature is a location for feature_files: — the summary and the
    # assumptions manifest are not scenario sources and never ride here.
    assert all(path.endswith(".feature") for path in sent)


# ---------------------------------------------------------------------------
# REPLAY PROOF (B4 run 36629c5a, round 10) — the fixed descriptor threads the
# EXACT roots, and the pre-commit validate (the REAL guardkit smoke-gate path
# check, byte-faithful to `guardkit feature validate`) is STILL the last line of
# defense: an invented ``tests/smoke`` fails loudly; a real ``tests/health``
# passes to BUILD_QUEUED.
# ---------------------------------------------------------------------------


def _init_api_test_shaped_repo(path: Path) -> None:
    """A scratch git repo shaped like api_test: real per-suite test roots
    (``tests/health`` + ``tests/users``) committed on the base, NO
    ``tests/smoke`` — so both the main checkout (descriptor discovery) and the
    planning worktree (validate) carry the exact round-10 shape."""
    path.mkdir(parents=True, exist_ok=True)
    env = {
        **__import__("os").environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, env=env)
    (path / "README.md").write_text("scratch\n")
    for suite in ("health", "users"):
        d = path / "tests" / suite
        d.mkdir(parents=True)
        (d / "__init__.py").write_text("")  # git tracks the dir via a real file
    subprocess.run(["git", "add", "."], cwd=path, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True, env=env)


async def _guardkit_smoke_gate_validate(
    worktree: Path, feature_id: str
) -> ToolOutcome:
    """A validate oracle built from guardkit's OWN smoke-gate path primitives.

    Byte-faithful to guardkit's ``feature validate`` smoke-gate check
    (``guardkit/orchestrator/feature_loader.py:1131-1142``
    ``_validate_smoke_gate_paths_for_validate``): it reuses the SAME three
    functions — ``parse_positional_paths`` + a filesystem existence check +
    ``discover_test_roots`` + ``format_smoke_gate_path_error`` — the real
    validate binary composes. Used in-process here because the full guardkit
    ``FeatureLoader`` pulls heavy deps (frontmatter) absent from the forge venv,
    which is exactly why production shells the guardkit BINARY via
    ``forge.adapters.guardkit.run``; the identical binary run against the
    preserved round-10 worktree is the separate live-repro check.
    """
    import yaml
    from guardkit.lib.pytest_argv import (
        format_smoke_gate_path_error,
        parse_positional_paths,
    )
    from installer.core.commands.lib.smoke_gates_nudge import discover_test_roots

    feature_file = worktree / ".guardkit" / "features" / f"{feature_id}.yaml"
    data = yaml.safe_load(feature_file.read_text(encoding="utf-8"))
    smoke_gates = (data or {}).get("smoke_gates")
    if not smoke_gates or not smoke_gates.get("command"):
        return ToolOutcome(ok=True)
    paths = parse_positional_paths(smoke_gates["command"])
    missing = [p for p in paths if not (worktree / p).exists()]
    if not missing:
        return ToolOutcome(ok=True)
    roots = discover_test_roots(worktree)
    return ToolOutcome(
        ok=False,
        detail=format_smoke_gate_path_error(missing, worktree, roots),
    )


def _plan_result_native_smoke(smoke_path: str, slug: str = "stats-endpoint"):
    """008 native reply whose feature YAML declares a smoke gate at ``smoke_path``
    (e.g. ``tests/smoke`` — invented — or ``tests/health`` — real)."""

    def _factory(feature_id: str) -> Any:
        feature_yaml = (
            f"id: {feature_id}\n"
            "tasks: []\n"
            "smoke_gates:\n"
            "  after_wave: 1\n"
            "  command: |\n"
            f"    pytest {smoke_path} -x\n"
            "  expected_exit: 0\n"
        )
        return SimpleNamespace(
            outcome=SimpleNamespace(value="completed"),
            role_output={
                f".guardkit/features/{feature_id}.yaml": feature_yaml,
                f"tasks/backlog/{slug}/TASK-STAT-001.md": "# task\n",
                "validation.json": json.dumps(
                    {"accepted": True, "errors": [],
                     "gates_run": ["feature_validate"]}
                ),
            },
            reason=None,
        )

    return _factory


@pytest.mark.asyncio
async def test_replay_invented_tests_smoke_fails_the_real_validate(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """(a)+(b): the fixed descriptor carries the exact roots, and an 008 reply
    whose feature YAML references the invented ``tests/smoke`` is REFUSED loudly
    by the pre-commit validate (the round-10 live failure, still caught) — the
    plan is NOT committed and the run never reaches BUILD_QUEUED."""
    repo = tmp_path / "api_test"
    _init_api_test_shaped_repo(repo)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")

    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        plan_result_factory=_plan_result_native_smoke("tests/smoke"),
        validate_fn=_guardkit_smoke_gate_validate,
    )

    await h.driver.drive(CID)

    # (a) the descriptor forge threaded to 008 carried the EXACT roots.
    assert h.ctx["counters"]["last_descriptor"] == {
        "repo": TARGET_REPO,
        "test_roots": ["tests/health", "tests/users"],
    }
    # (b) the pre-commit validate refused the invented path — loudly, verbatim.
    run = store.get_run(CID)
    assert run["state"] != PlanningState.BUILD_QUEUED.value
    assert h.ctx["counters"]["validate"] == 1
    assert h.ctx["counters"]["build_trigger"] == 0
    reasons = " ".join(m for _cid, m, _lvl in h.ctx["notifications"])
    assert "tests/smoke" in reasons
    assert "Available test roots: tests/health, tests/users" in reasons
    # The plan tree was NOT committed to the branch.
    branch = f"planning/{CID}"
    feature_id = h.ctx["counters"]["last_feature_id"]
    show = subprocess.run(
        ["git", "show", f"{branch}:.guardkit/features/{feature_id}.yaml"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert show.returncode != 0  # the path does not exist on the branch


@pytest.mark.asyncio
async def test_replay_real_tests_health_passes_to_build_queued(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """(c): the same round-trip with a smoke gate referencing the REAL
    ``tests/health`` root passes the pre-commit validate and drives to
    BUILD_QUEUED — the good path is unblocked."""
    repo = tmp_path / "api_test"
    _init_api_test_shaped_repo(repo)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")

    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        plan_result_factory=_plan_result_native_smoke("tests/health"),
        validate_fn=_guardkit_smoke_gate_validate,
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert h.ctx["counters"]["validate"] == 1
    assert h.ctx["counters"]["build_trigger"] == 1
    # The plan tree (with the valid smoke gate) IS committed on the branch.
    branch = f"planning/{CID}"
    feature_id = h.ctx["counters"]["last_feature_id"]
    show = subprocess.run(
        ["git", "show", f"{branch}:.guardkit/features/{feature_id}.yaml"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert show.returncode == 0
    assert "pytest tests/health" in show.stdout


@pytest.mark.asyncio
async def test_round_trip_is_idempotent_on_redrive(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    h = _make_driver(store, git_runner=git, repo_path=str(repo))

    await h.driver.drive(CID)
    await h.driver.drive(CID)  # re-drive: must not re-dispatch or re-queue

    assert h.ctx["counters"]["spec"] == 1
    assert h.ctx["counters"]["plan"] == 1
    assert h.ctx["counters"]["build_trigger"] == 1  # build queued exactly once
    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value


# ---------------------------------------------------------------------------
# Failure paths — loud FAILED + notification
# ---------------------------------------------------------------------------


async def _drive_to_failure(h: _Harness, store: SqlitePlanningRunStore) -> str:
    await h.driver.drive(CID)
    run = store.get_run(CID)
    return run["state"]


@pytest.mark.asyncio
async def test_spec_dispatch_error_fails_loudly(store: SqlitePlanningRunStore) -> None:
    _queue(store)
    h = _make_driver(store, spec_result=_error_result())
    assert await _drive_to_failure(h, store) == PlanningState.FAILED.value
    assert any(level == "error" for _, _, level in h.ctx["notifications"])
    assert h.ctx["counters"]["plan"] == 0  # never reached the plan leg


@pytest.mark.asyncio
async def test_spec_invalid_artifacts_fails(store: SqlitePlanningRunStore) -> None:
    _queue(store)
    # role_output carries no ``files`` and no feature/assumptions/summary triple.
    bad = SimpleNamespace(
        outcome=SimpleNamespace(value="completed"), role_output={"slug": "x"}, reason=None
    )
    h = _make_driver(store, spec_result=bad)
    assert await _drive_to_failure(h, store) == PlanningState.FAILED.value


@pytest.mark.asyncio
async def test_normalizer_red_fails_and_does_not_commit(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        normalize=ToolOutcome(ok=False, detail="unparseable gherkin"),
    )
    assert await _drive_to_failure(h, store) == PlanningState.FAILED.value
    # The branch exists (the handoff commit landed) but the red normalizer
    # aborted the spec commit — the .feature never reached the branch tip.
    branch = f"planning/{CID}"
    feature_show = subprocess.run(
        ["git", "show", f"{branch}:features/stats-endpoint/stats-endpoint.feature"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert feature_show.returncode != 0  # path not present on the branch


@pytest.mark.asyncio
async def test_feature_id_mismatch_fails_rv1(store: SqlitePlanningRunStore) -> None:
    _queue(store)
    # The plan declares a DIFFERENT feature id than forge supplied — RV-1 catch.
    h = _make_driver(
        store,
        plan_result=SimpleNamespace(
            outcome=SimpleNamespace(value="completed"),
            role_output={
                "feature_id": "FEAT-WRONG",
                "files": {"features/x/FEAT-WRONG.yaml": "id: FEAT-WRONG\n"},
            },
            reason=None,
        ),
    )
    assert await _drive_to_failure(h, store) == PlanningState.FAILED.value
    assert any("mismatch" in msg.lower() or "RV-1" in msg for _, msg, _ in h.ctx["notifications"])


@pytest.mark.asyncio
async def test_plan_invalid_artifacts_fails(store: SqlitePlanningRunStore) -> None:
    _queue(store)
    h = _make_driver(
        store,
        plan_result=SimpleNamespace(
            outcome=SimpleNamespace(value="completed"), role_output={}, reason=None
        ),
    )
    assert await _drive_to_failure(h, store) == PlanningState.FAILED.value


@pytest.mark.asyncio
async def test_validate_red_fails_and_plan_not_committed(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        validate=ToolOutcome(ok=False, detail="schema errors"),
    )
    assert await _drive_to_failure(h, store) == PlanningState.FAILED.value
    # The spec landed (spec leg succeeded) but the plan YAML never committed.
    branch = f"planning/{CID}"
    plan_show = subprocess.run(
        ["git", "show", f"{branch}:features/stats-endpoint"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert "FEAT-" not in plan_show.stdout


@pytest.mark.asyncio
async def test_unwired_legs_fail_loudly(store: SqlitePlanningRunStore) -> None:
    _queue(store)
    h = _make_driver(store, wire_legs=False)
    assert await _drive_to_failure(h, store) == PlanningState.FAILED.value
    assert any(level == "error" for _, _, level in h.ctx["notifications"])


# ---------------------------------------------------------------------------
# B3 — the build trigger (queue onto Mode B → BUILD_QUEUED)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_trigger_not_queued_fails_loudly(
    store: SqlitePlanningRunStore,
) -> None:
    """A trigger that refuses to queue (e.g. repo not allowlisted) fails loud."""
    _queue(store)
    h = _make_driver(
        store,
        build_trigger_result=BuildTriggerResult(
            queued=False, reason="repo not allowlisted"
        ),
    )
    assert await _drive_to_failure(h, store) == PlanningState.FAILED.value
    assert any(level == "error" for _, _, level in h.ctx["notifications"])
    # Spec + plan still ran (the trigger is the last leg) but no BUILD_QUEUED.
    assert h.ctx["counters"]["plan"] == 1
    assert h.ctx["counters"]["build_trigger"] == 1
    # No durable APPROVED build-queued marker landed (the failure transition
    # records a build-queued-labelled row, but not the idempotency sentinel).
    assert not any(
        e["stage_label"] == "build-queued" and e["status"] == "approved"
        for e in store.list_events(CID)
    )


@pytest.mark.asyncio
async def test_build_trigger_raises_fails_loudly(
    store: SqlitePlanningRunStore,
) -> None:
    """A trigger that raises (e.g. bus publish error) never crashes the run."""

    async def _boom(**_: Any) -> BuildTriggerResult:
        raise RuntimeError("bus unreachable")

    _queue(store)
    h = _make_driver(store, build_trigger_fn=_boom)
    assert await _drive_to_failure(h, store) == PlanningState.FAILED.value
    assert any(level == "error" for _, _, level in h.ctx["notifications"])


@pytest.mark.asyncio
async def test_unwired_build_trigger_fails_loudly(
    store: SqlitePlanningRunStore,
) -> None:
    """Flag ON but the build-trigger collaborator missing = loud FAILED."""
    _queue(store)
    h = _make_driver(store, wire_build_trigger=False)
    assert await _drive_to_failure(h, store) == PlanningState.FAILED.value
    assert any(level == "error" for _, _, level in h.ctx["notifications"])
    # The plan leg completed; only the build trigger was unwired.
    assert h.ctx["counters"]["plan"] == 1


@pytest.mark.asyncio
async def test_build_trigger_idempotent_after_queue_recorded(
    store: SqlitePlanningRunStore,
) -> None:
    """Crash AFTER the build was queued but BEFORE the BUILD_QUEUED transition.

    A re-drive must advance to BUILD_QUEUED from the durable ``build-queued``
    leg event WITHOUT re-queuing the build (no second Mode B publish).
    """
    _queue(store)
    # Walk the run to FEATURE_PLAN via the flag-ON transition table.
    for to in (
        PlanningState.RUNNING,
        PlanningState.FEATURE_SPEC,
        PlanningState.FEATURE_PLAN,
    ):
        refused = store.transition(
            correlation_id=CID,
            to_state=to,
            actor_identity="seed",
            stage_label="seed",
        )
        assert not isinstance(refused, TransitionRefused)
    # Seed the durable leg events the trigger leg reads, incl. the already-queued
    # marker (the crash window: event recorded, state not yet advanced).
    store._record_event(
        correlation_id=CID,
        stage_label="feature-plan",
        status="approved",
        actor_identity="seed",
        details_json=json.dumps(
            {
                "feature_id": "FEAT-AAAA",
                "target_repo": TARGET_REPO,
                "branch": f"planning/{CID}",
                "plan_files": ["features/x/FEAT-AAAA.yaml"],
            }
        ),
    )
    # The pass bars were already registered in this crash window (they land
    # BEFORE the build-queued marker), so seed that leg event too.
    store._record_event(
        correlation_id=CID,
        stage_label="qa-pass-bars",
        status="approved",
        actor_identity="seed",
        details_json=json.dumps({"feature_id": "FEAT-AAAA", "bar_files": []}),
    )
    store._record_event(
        correlation_id=CID,
        stage_label="build-queued",
        status="approved",
        actor_identity="seed",
        details_json=json.dumps({"feature_id": "FEAT-AAAA", "build_id": "build-1"}),
    )

    h = _make_driver(store)
    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    # Neither the specialist legs nor the build trigger were re-invoked.
    assert h.ctx["counters"]["build_trigger"] == 0
    assert h.ctx["counters"]["plan"] == 0
    assert h.ctx["counters"]["pass_bar_validate"] == 0


# ---------------------------------------------------------------------------
# B4 round-19 (Rich-ratified) — register per-task QA pass bars from the 007
# seed at plan-commit. Wire-true replay: the LITERAL round-19 seed bytes (the
# committed fixture below), driven through the plan-commit step on a throwaway
# repo. The live launch (run 75978066) reached the first real machine build and
# guardkit refused in 12s: qa_precondition_blocked — no qa/pass-bar-<TASK-ID>
# registered before implementation. Forge now mints them from the seed.
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).parent / "fixtures"
#: The LITERAL round-19 live seed bytes (specialist output, host-visible mount).
#: It is auth_surface_bearing: true — the attended-registration path.
_ROUND19_SEED_AUTH = (_FIXTURES / "pass-bar-seed-version-endpoint.yaml").read_text(
    encoding="utf-8"
)
#: The SAME seed with the flag flipped false — the machine-registration path.
_ROUND19_SEED_AUTHLESS = _ROUND19_SEED_AUTH.replace(
    "auth_surface_bearing: true", "auth_surface_bearing: false"
)

_ALLOWED_PRECONDITIONS = {"suite_green_vs_ledger", "analyze_clean", "build_artifact"}
_ALLOWED_CRITERION_KEYS = {"id", "text", "class", "evidence_kind", "runbook_ref"}
_ALLOWED_EVIDENCE = {"screenshot", "json", "log", "operator_signoff"}
_ALLOWED_BAR_KEYS = {
    "format_version",
    "task_id",
    "registered_at",
    "auth_surface_bearing",
    "preconditions",
    "criteria",
    "negative_paths",
    "checkpoint_list_ref",
}


def _assert_pass_bar_schema(bar: dict[str, Any]) -> None:
    """Schema-faithful in-test check mirroring guardkit's PassBar (F1 v2.0).

    guardkit is not importable in the forge venv (production shells the vendored
    BINARY), so this replicates the load-bearing constraints of
    ``guardkit/qa/formats/pass_bar.py`` so "guardkit qa validate pass-bar is
    green" is a real assertion, not a stubbed one: extra="forbid" at the root,
    the registered_at {sha>=4, date YYYY-MM-DD} shape, the precondition/evidence
    enums, and the conditional negative-path minimum set.
    """
    import re as _re

    assert set(bar) <= _ALLOWED_BAR_KEYS, f"unknown root keys: {set(bar) - _ALLOWED_BAR_KEYS}"
    assert int(str(bar["format_version"]).split(".", 1)[0]) in {1, 2}
    assert isinstance(bar["task_id"], str) and bar["task_id"]
    reg = bar["registered_at"]
    assert set(reg) <= {"sha", "date"}
    assert isinstance(reg["sha"], str) and len(reg["sha"]) >= 4
    assert _re.fullmatch(r"\d{4}-\d{2}-\d{2}", reg["date"])
    assert isinstance(bar["auth_surface_bearing"], bool)
    assert bar["preconditions"] and set(bar["preconditions"]) <= _ALLOWED_PRECONDITIONS
    assert bar["criteria"], "criteria must be non-empty"
    for crit in bar["criteria"]:
        assert set(crit) <= _ALLOWED_CRITERION_KEYS
        assert crit["id"] and crit["text"]
        assert crit["class"] in {"machine", "operator"}
        assert crit["evidence_kind"] in _ALLOWED_EVIDENCE
        if crit["class"] == "operator":
            assert crit.get("runbook_ref")
    assert bar["negative_paths"], "negative_paths must be non-empty"
    if bar["auth_surface_bearing"]:
        required = {
            "dependency_down_degradation",
            "wrong_credential",
            "anonymous_deep_link",
            "post_logout_401",
            "unauthorized_403_ui",
        }
    else:
        required = {"dependency_down_degradation"}
    assert required <= set(bar["negative_paths"])


async def _schema_pass_bar_oracle(worktree: Path, bar_rel: str) -> ToolOutcome:
    """A pass-bar validate oracle that runs the schema-faithful check against the
    on-disk minted bar (stands in for the vendored guardkit binary in-test)."""
    import yaml as _yaml

    try:
        data = _yaml.safe_load((worktree / bar_rel).read_text(encoding="utf-8"))
        _assert_pass_bar_schema(data)
    except AssertionError as exc:
        return ToolOutcome(ok=False, detail=f"{bar_rel}: schema invalid — {exc}")
    return ToolOutcome(ok=True)


def _spec_result_with_seed(seed_yaml: str | None, slug: str = "version-endpoint") -> Any:
    """The DEPLOYED 007 native reply for ``slug`` carrying ``seed_yaml`` as its
    pass-bar seed (or NO seed when ``seed_yaml`` is None — the older-specialist
    shape)."""
    result = _spec_result_native(slug=slug)
    seed_key = f"pass-bar-seed-{slug}.yaml"
    if seed_yaml is None:
        result.role_output.pop(seed_key, None)
    else:
        result.role_output[seed_key] = seed_yaml
    return result


def _plan_result_native_versions(feature_id: str) -> Any:
    """The round-19 feature YAML: three version-endpoint tasks, NO qa bars (the
    008 map never emits them — the gap forge fills)."""
    feature_yaml = (
        f"id: {feature_id}\n"
        "tasks:\n"
        "- id: TASK-VER-001\n"
        "- id: TASK-VER-002\n"
        "- id: TASK-VER-003\n"
    )
    return SimpleNamespace(
        outcome=SimpleNamespace(value="completed"),
        role_output={
            f".guardkit/features/{feature_id}.yaml": feature_yaml,
            "tasks/backlog/version-endpoint/TASK-VER-001-create-version-endpoint.md": (
                "# task\n"
            ),
            "validation.json": json.dumps(
                {"accepted": True, "errors": [], "gates_run": ["feature_validate"]}
            ),
        },
        reason=None,
    )


def _leg_sha(store: SqlitePlanningRunStore, stage_label: str) -> str | None:
    for e in store.list_events(CID):
        if e["stage_label"] == stage_label and e["status"] == "approved":
            return (json.loads(e["details_json"]) or {}).get("sha")
    return None


def _leg_details(store: SqlitePlanningRunStore, stage_label: str) -> dict[str, Any]:
    """Details of the latest ``approved`` event for ``stage_label``."""
    latest: dict[str, Any] = {}
    for e in store.list_events(CID):
        if e["stage_label"] == stage_label and e["status"] == "approved":
            latest = json.loads(e["details_json"] or "{}") or {}
    return latest


# ---------------------------------------------------------------------------
# THE AUTH-CONFIRMATION DOOR (cure for live run dff0cd00, 2026-07-31)
#
# Before the cure an auth_surface_bearing seed KILLED the run at the pass-bar
# leg. The flag is a keyword detector on the spec text and fires on specs that
# PROVE their own authlessness, and SPL-007 §A.2's own words are "requires human
# confirmation" — so the run now pauses for the owner's one-tap answer, reusing
# the assumptions-checkpoint mechanics. Confirm ⇒ the unflagged path exactly;
# reject / no answer ⇒ the honest terminal that shipped before.
# ---------------------------------------------------------------------------

#: The door's durable stage label / wire discriminator — pinned literally here
#: (a drift changes the request_id on the wire and jarvis's rendering key).
_AUTH_DOOR_STAGE = "qa-pass-bars-auth-confirm"
_AUTH_DOOR_CHECKPOINT_TYPE = "auth_surface_confirmation"


def _auth_door_request_id(attempt: int = 0) -> str:
    return derive_request_id(
        build_id=PLAN_RUN_ID, stage_label=_AUTH_DOOR_STAGE, attempt_count=attempt
    )


def _auth_door_answer(
    decision: str, *, decided_by: str = ORIGINATOR, attempt: int = 0
) -> ApprovalResponsePayload:
    return ApprovalResponsePayload(
        request_id=_auth_door_request_id(attempt),
        decision=decision,
        decided_by=decided_by,
    )


#: A raw chat member id as it appears in text ("U03QR8WKT29"). Nothing a
#: person reads may contain one (2026-09-05 defect).
_RAW_CHAT_ID = re.compile(r"\bU[0-9A-Z]{8,}\b")


def _assert_no_raw_chat_ids(notifications: list[tuple[str, str, str]]) -> None:
    """Grep every line the run sent a person for a raw chat id."""
    for _cid, message, _level in notifications:
        found = _RAW_CHAT_ID.findall(message)
        assert not found, f"raw chat id {found} in a line a person reads: {message}"


class _NamedAnswer(ApprovalResponsePayload):
    """An answer that DOES carry the answerer's display name.

    The live payload declares no such field and drops undeclared ones, so this
    subclass is the only way to prove the day one travels.
    """

    decided_by_name: str


def _auth_door_card(h: _Harness) -> dict[str, Any]:
    """The ONE auth-confirmation card the run put in front of the owner."""
    cards = [
        env.payload["details"]
        for env in h.ctx["publisher"].envelopes
        if env.payload["details"].get("checkpoint_type") == _AUTH_DOOR_CHECKPOINT_TYPE
    ]
    assert len(cards) == 1, f"expected exactly one auth card, got {len(cards)}"
    return cards[0]


def _door_events(store: SqlitePlanningRunStore) -> list[tuple[str, dict[str, Any]]]:
    return [
        (e["status"], json.loads(e["details_json"] or "{}"))
        for e in store.list_events(CID)
        if e["stage_label"] == _AUTH_DOOR_STAGE
    ]


async def _auth_flagged_harness(
    store: SqlitePlanningRunStore,
    tmp_path: Path,
    *,
    script: list[Any],
    originator_wait_seconds: int = 3600,
    publisher: FakePublisher | None = None,
) -> tuple[_Harness, Path]:
    """A run parked where the SIGN-IN DOOR is still the door that asks.

    Machine-chain stage 2 folded the sign-in question into the spec digest card,
    so a run that starts fresh answers it there and this door never opens — one
    pause, counted end to end. The door is not dead, though: it stays the door
    for a run that reaches the quality-checklist leg with no digest answer on
    its record, which is exactly what an IN-FLIGHT run looked like when stage 2
    landed. That is the shape built here — the spec already committed and
    approved with no spec-review record — so these tests keep proving the door
    on the path where it is genuinely reachable.
    """
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_with_seed(_ROUND19_SEED_AUTH),
        plan_result_factory=_plan_result_native_versions,
        pass_bar_validate_fn=_schema_pass_bar_oracle,
        subscriber_factory=SharedScriptSubscriberFactory(script),
        originator_wait_seconds=originator_wait_seconds,
        publisher=publisher,
    )
    await _carry_in_flight_spec(store, git, repo, seed_yaml=_ROUND19_SEED_AUTH)
    return h, repo


async def _carry_in_flight_spec(
    store: SqlitePlanningRunStore,
    git: Any,
    repo: Path,
    *,
    seed_yaml: str,
    slug: str = "version-endpoint",
) -> None:
    """Commit a spec and approve it WITHOUT a digest-review record, at FEATURE_PLAN.

    The shape a run already walking the chain had when stage 2 landed: its spec
    leg ran under the old rules, so there is no record of anybody having been
    asked the sign-in question.
    """
    branch = f"planning/{CID}"
    files = {
        f"features/{slug}/{slug}.feature": _FIXTURE_FEATURE,
        f"features/{slug}/{slug}_assumptions.yaml": "assumptions: []\n",
        f"features/{slug}/{slug}_summary.md": "# summary\n",
    }
    written = await git.prepare_branch_and_write_tree(
        repo_path=str(repo),
        branch=branch,
        files=files,
        message="planning: feature spec (in-flight fixture)",
    )
    for to_state in (
        PlanningState.RUNNING,
        PlanningState.FEATURE_SPEC,
        PlanningState.FEATURE_PLAN,
    ):
        store.transition(
            correlation_id=CID,
            to_state=to_state,
            actor_identity="in-flight-fixture",
            stage_label="in-flight-fixture",
        )
    store._record_event(
        correlation_id=CID,
        stage_label="feature-spec",
        status="approved",
        actor_identity="planning-driver",
        details_json=json.dumps(
            {
                "slug": slug,
                "spec_files": sorted(files),
                "target_repo": TARGET_REPO,
                "repo_path": str(repo),
                "branch": branch,
                "sha": written.sha,
                "pass_bar_seed": seed_yaml,
            }
        ),
    )


@pytest.mark.asyncio
async def test_auth_flagged_seed_confirmed_resumes_exactly_as_unflagged(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """(a) CONFIRM: the LITERAL round-19 auth-flagged seed no longer kills the
    run — the owner taps confirm and machine registration proceeds EXACTLY as
    the unflagged path (three schema-green bars, then BUILD_QUEUED)."""
    h, repo = await _auth_flagged_harness(
        store, tmp_path, script=[_auth_door_answer("approve")]
    )

    await h.driver.drive(CID)

    run = store.get_run(CID)
    assert run["state"] == PlanningState.BUILD_QUEUED.value

    # Byte-for-byte the unflagged outcome: one validated bar per plan task,
    # each minted authless, and the build queued exactly once.
    assert h.ctx["counters"]["pass_bar_validate"] == 3
    assert set(h.ctx["validated_bars"]) == {
        "qa/pass-bar-TASK-VER-001.yaml",
        "qa/pass-bar-TASK-VER-002.yaml",
        "qa/pass-bar-TASK-VER-003.yaml",
    }
    assert h.ctx["counters"]["build_trigger"] == 1

    branch = f"planning/{CID}"
    for task_id in ("TASK-VER-001", "TASK-VER-002", "TASK-VER-003"):
        raw = subprocess.run(
            ["git", "show", f"{branch}:qa/pass-bar-{task_id}.yaml"],
            cwd=repo,
            capture_output=True,
            text=True,
        ).stdout
        bar = yaml.safe_load(raw)
        _assert_pass_bar_schema(bar)
        assert bar["task_id"] == task_id
        # Confirmed authless — and the seed's auth basis never leaks into a bar.
        assert bar["auth_surface_bearing"] is False
        assert "auth_surface_basis" not in bar

    # The owner's act is on the durable record, and on the leg's own receipt.
    statuses = [status for status, _details in _door_events(store)]
    assert statuses == ["GATED", "approved"]
    confirmation = _door_events(store)[-1][1]["auth_confirmation"]
    assert confirmation["outcome"] == "confirmed"
    assert confirmation["decided_by"] == ORIGINATOR
    assert confirmation["request_id"] == _auth_door_request_id(0)
    bars_receipt = _leg_details(store, "qa-pass-bars")
    assert bars_receipt["auth_confirmation"]["decided_by"] == ORIGINATOR


@pytest.mark.asyncio
async def test_the_sign_in_confirmation_line_says_you_not_a_raw_chat_id(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """THE DEFECT, third door: "U03QR8WKT29 confirmed there is no sign-in here"
    named nobody. No display name travels with a live answer, so it is "you"."""
    h, _repo = await _auth_flagged_harness(
        store, tmp_path, script=[_auth_door_answer("approve")]
    )

    await h.driver.drive(CID)

    said = [m for _c, m, _l in h.ctx["notifications"] if "no sign-in here" in m]
    assert said == [
        f"Planning run {CID}: you confirmed there is no sign-in here — "
        "registering the quality checklist as authless and carrying on with "
        "the build."
    ]
    _assert_no_raw_chat_ids(h.ctx["notifications"])
    # The id is still on the durable record, where the machine routes by it.
    assert _door_events(store)[-1][1]["auth_confirmation"]["decided_by"] == ORIGINATOR


@pytest.mark.asyncio
async def test_the_sign_in_confirmation_line_uses_a_display_name_when_one_travels(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """The day a name rides the answer, the line says the name."""
    named = _NamedAnswer(
        request_id=_auth_door_request_id(0),
        decision="approve",
        decided_by=ORIGINATOR,
        decided_by_name="Rich",
    )
    h, _repo = await _auth_flagged_harness(store, tmp_path, script=[named])

    await h.driver.drive(CID)

    said = [m for _c, m, _l in h.ctx["notifications"] if "no sign-in here" in m]
    assert said == [
        f"Planning run {CID}: Rich confirmed there is no sign-in here — "
        "registering the quality checklist as authless and carrying on with "
        "the build."
    ]
    _assert_no_raw_chat_ids(h.ctx["notifications"])


@pytest.mark.asyncio
async def test_auth_door_card_speaks_plain_language_and_quotes_the_basis(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """The card the owner reads: the seed's OWN flagged words quoted verbatim,
    what confirming does, what rejecting does, what silence does — in plain
    language, threaded to the run's approver, with no jargon as a label."""
    h, _repo = await _auth_flagged_harness(
        store, tmp_path, script=[_auth_door_answer("approve")]
    )

    await h.driver.drive(CID)

    card = _auth_door_card(h)
    assert card["expected_approver"] == ORIGINATOR
    assert card["build_id"] == PLAN_RUN_ID
    summary = card["summary"]
    # The seed's own basis, verbatim — the owner judges the actual evidence.
    assert summary["flagged_lines"] == [
        "auth signals detected: auth token 'auth' in scenario (deferred — "
        "requires human confirmation per SPL-007 CONTRACT §A.2)"
    ]
    # Both outcomes are spelled out, plus what happens if he never answers.
    assert "register the quality checklist" in summary["confirm_means"]
    assert "carry on" in summary["confirm_means"]
    assert "attended" in summary["reject_means"]
    assert "1 hour" in summary["no_answer_means"]
    assert summary["feature"] == "version-endpoint"
    # Plain language: no internal identifiers or clause codes as the labels the
    # owner reads first.
    for field in ("title", "what_happened", "confirm_means", "reject_means"):
        assert "auth_surface_bearing" not in summary[field]
        assert "SPL-007" not in summary[field]
        assert "pass-bar-seed" not in summary[field]
    # The run told the originator it is WAITING, not broken.
    opened = [m for _cid, m, lvl in h.ctx["notifications"] if lvl == "info"]
    assert any("often a false alarm" in m for m in opened)


@pytest.mark.asyncio
async def test_auth_door_rejected_takes_the_honest_terminal(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """(b) REJECT: the owner says it really is a sign-in surface — the run
    takes the SAME honest terminal that shipped before (SPL-007 §A.2 + the
    seed's basis verbatim), plus which way the door closed. No bars, no
    build, no idempotency sentinel."""
    h, repo = await _auth_flagged_harness(
        store, tmp_path, script=[_auth_door_answer("reject")]
    )

    await h.driver.drive(CID)

    run = store.get_run(CID)
    assert run["state"] == PlanningState.FAILED.value
    assert h.ctx["counters"]["pass_bar_validate"] == 0
    assert h.ctx["counters"]["build_trigger"] == 0

    # THE OWNER'S TEXT — plain names ruling (2026-07-31). The Slack message
    # names the stage in plain words, says what happened and that nothing was
    # built. Not one internal label survives into it.
    reasons = " ".join(m for _cid, m, _lvl in h.ctx["notifications"])
    assert "stopped at registering the quality checklist" in reasons
    assert "flagged this feature as sitting behind a sign-in" in reasons
    # ...and it names the door's verdict rather than pretending nobody was asked.
    assert "confirmed this IS a sign-in surface" in reasons
    assert "Nothing was registered and nothing was built" in reasons
    for internal in ("qa-pass-bars", "auth_surface_bearing", "SPL-007"):
        assert internal not in reasons, (
            f"{internal!r} leaked into the owner-facing message: {reasons!r}"
        )

    # THE MACHINE'S RECORD — the durable FAILED row keeps the clause, the flag
    # name and the seed's basis VERBATIM. That is the receipt an operator greps.
    error = run["error"]
    assert "SPL-007 §A.2" in error
    assert "auth_surface_bearing" in error
    assert "attended registration" in error
    assert "requires human confirmation per SPL-007 CONTRACT §A.2" in error

    # No qa/pass-bar file landed on the branch.
    show = subprocess.run(
        ["git", "show", f"planning/{CID}:qa/pass-bar-TASK-VER-001.yaml"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert show.returncode != 0
    # The pass-bar idempotency sentinel was NOT recorded (refused, not completed).
    assert not any(
        e["stage_label"] == "qa-pass-bars" and e["status"] == "approved"
        for e in store.list_events(CID)
    )
    # The door's own verdict is durable, and is NOT the confirmed sentinel.
    assert [status for status, _d in _door_events(store)] == ["GATED", "rejected"]


@pytest.mark.asyncio
async def test_auth_door_unanswered_times_out_to_the_honest_terminal(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """(c) SILENCE: nobody answers inside the wait window — the run takes the
    same honest terminal, naming the timeout, and never registers a bar."""
    h, _repo = await _auth_flagged_harness(
        store, tmp_path, script=[_approve()], originator_wait_seconds=1
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.FAILED.value
    assert h.ctx["counters"]["pass_bar_validate"] == 0
    assert h.ctx["counters"]["build_trigger"] == 0
    reasons = " ".join(m for _cid, m, _lvl in h.ctx["notifications"])
    assert "stopped at registering the quality checklist" in reasons
    assert "Nobody answered the confirmation card" in reasons
    for internal in ("qa-pass-bars", "auth_surface_bearing", "SPL-007"):
        assert internal not in reasons
    # The clause and the flag stay on the durable record, never in Slack.
    assert "SPL-007 §A.2" in store.get_run(CID)["error"]
    assert [status for status, _d in _door_events(store)] == ["GATED", "timed_out"]
    # The wait window is spoken in human units on the card.
    assert "1 second" in _auth_door_card(h)["summary"]["no_answer_means"]


@pytest.mark.asyncio
async def test_auth_door_undeliverable_card_takes_the_honest_terminal(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """A card that cannot be put on the wire is not a silent wait: the run
    stops immediately with the honest terminal, saying nobody could answer."""
    h, _repo = await _auth_flagged_harness(
        store,
        tmp_path,
        script=[_auth_door_answer("approve")],
        publisher=AuthCardUndeliverablePublisher(),
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.FAILED.value
    assert h.ctx["counters"]["pass_bar_validate"] == 0
    assert h.ctx["counters"]["build_trigger"] == 0
    reasons = " ".join(m for _cid, m, _lvl in h.ctx["notifications"])
    assert "stopped at registering the quality checklist" in reasons
    assert "could not be delivered" in reasons
    for internal in ("qa-pass-bars", "auth_surface_bearing", "SPL-007"):
        assert internal not in reasons
    assert "SPL-007 §A.2" in store.get_run(CID)["error"]
    assert [status for status, _d in _door_events(store)] == ["GATED", "undeliverable"]


@pytest.mark.asyncio
async def test_auth_door_ignores_a_stranger_and_a_foreign_card(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """The door is pinned to the run's own approver and to its OWN card: a
    stranger's confirm and a reply to a different request_id are both ignored,
    and the run ends on the honest timeout — never on someone else's tap."""
    h, _repo = await _auth_flagged_harness(
        store,
        tmp_path,
        script=[
            _approve(),
            _auth_door_answer("approve", decided_by="U_STRANGER"),
            # A late reply to the product-docs card, not to this door.
            ApprovalResponsePayload(
                request_id=_request_id(0), decision="approve", decided_by=ORIGINATOR
            ),
        ],
        originator_wait_seconds=1,
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.FAILED.value
    assert h.ctx["counters"]["pass_bar_validate"] == 0
    assert h.ctx["counters"]["build_trigger"] == 0
    assert [status for status, _d in _door_events(store)] == ["GATED", "timed_out"]


@pytest.mark.asyncio
async def test_auth_door_confirmation_is_never_re_asked_on_a_re_drive(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """A crash between the owner's tap and the bars commit must not re-ask: the
    durable confirmation short-circuits the door on the next drive."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)

    # Drive once, confirming at the door, then rewind the durable record to the
    # crash window: the plan is committed, the confirmation is given, but the
    # bars leg never completed.
    factory = SharedScriptSubscriberFactory([_auth_door_answer("approve")])
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_with_seed(_ROUND19_SEED_AUTH),
        plan_result_factory=_plan_result_native_versions,
        pass_bar_validate_fn=_schema_pass_bar_oracle,
        subscriber_factory=factory,
    )
    await _carry_in_flight_spec(store, git, repo, seed_yaml=_ROUND19_SEED_AUTH)
    await h.driver.drive(CID)
    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    door_openings = sum(
        1 for status, _d in _door_events(store) if status == "GATED"
    )
    assert door_openings == 1

    # The re-drive of a completed run is already a no-op; the load-bearing
    # assertion is the door's own short-circuit, asserted directly.
    assert h.driver._has_leg_event(CID, _AUTH_DOOR_STAGE) is True
    outcome = await h.driver._auth_surface_confirmation_door(
        store.get_run(CID),
        CID,
        seed=yaml.safe_load(_ROUND19_SEED_AUTH),
        basis="anything",
    )
    assert outcome == "confirmed"
    # No second card, no second door-opening event.
    assert sum(1 for status, _d in _door_events(store) if status == "GATED") == 1
    assert (
        len(
            [
                env
                for env in h.ctx["publisher"].envelopes
                if env.payload["details"].get("checkpoint_type")
                == _AUTH_DOOR_CHECKPOINT_TYPE
            ]
        )
        == 1
    )


def _auth_cards(publisher: FakePublisher) -> list[Any]:
    """Every auth-confirmation envelope this wire has carried, in order."""
    return [
        env
        for env in publisher.envelopes
        if env.payload["details"].get("checkpoint_type") == _AUTH_DOOR_CHECKPOINT_TYPE
    ]


class NeverArmingFactory:
    """Hands out subscriptions that NEVER arm.

    The wire the arm-before-post guard exists for: the door's subscription never
    comes up, so the card is never published and NOBODY is ever asked.
    """

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, expected_approver: Any, armed: Any) -> ScriptedSubscriber:
        self.calls += 1
        return ScriptedSubscriber([], None)


@pytest.mark.asyncio
async def test_auth_door_survives_a_restart_and_re_emits_the_same_card(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """A daemon killed with the card LIVE must not orphan it.

    The run parks mid-chain at FEATURE_PLAN (the boot sweep's job to re-drive —
    asserted in ``tests/cli/test_serve_planning.py``), and the next drive
    RE-OPENS the same door: the persisted request_id is re-emitted VERBATIM, so
    the card still on the owner's screen is the card their tap answers. Minting
    a fresh id here would show two cards and silently drop the answer to the
    visible one.
    """
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    publisher = FakePublisher()  # ONE wire across both boots

    def _boot(script: list[Any]) -> _Harness:
        return _make_driver(
            store,
            git_runner=git,
            repo_path=str(repo),
            spec_result=_spec_result_with_seed(_ROUND19_SEED_AUTH),
            plan_result_factory=_plan_result_native_versions,
            pass_bar_validate_fn=_schema_pass_bar_oracle,
            subscriber_factory=SharedScriptSubscriberFactory(script),
            publisher=publisher,
        )

    await _carry_in_flight_spec(store, git, repo, seed_yaml=_ROUND19_SEED_AUTH)

    # BOOT 1: the owner is asked — then the daemon dies with the card live.
    first = _boot([])
    task = asyncio.create_task(first.driver.drive(CID))
    for _ in range(600):
        await asyncio.sleep(0.01)
        if _auth_cards(publisher):
            break
    assert _auth_cards(publisher), "the door never put a card on the wire"
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Parked mid-chain with an OPEN door: no terminal, no verdict, no bars.
    assert store.get_run(CID)["state"] == PlanningState.FEATURE_PLAN.value
    assert [status for status, _d in _door_events(store)] == ["GATED"]

    # BOOT 2 (what the boot sweep does): the owner taps the card they can STILL
    # SEE — the first card's request_id — and it is honoured.
    second = _boot([_auth_door_answer("approve")])
    await second.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert second.ctx["counters"]["build_trigger"] == 1
    # ONE card IDENTITY across both boots — the re-emission is verbatim.
    assert len(_auth_cards(publisher)) == 2
    assert {env.payload["request_id"] for env in _auth_cards(publisher)} == {
        _auth_door_request_id(0)
    }
    assert {
        env.payload["details"]["attempt_count"] for env in _auth_cards(publisher)
    } == {0}
    # The durable record says what happened: opened, re-opened, confirmed.
    assert [status for status, _d in _door_events(store)] == [
        "GATED",
        "reopened",
        "approved",
    ]


@pytest.mark.asyncio
async def test_auth_door_never_published_says_undeliverable_not_silence(
    store: SqlitePlanningRunStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A card that never reached the wire is NOT "nobody answered".

    When the response subscription never arms, arm-before-post never posts —
    so the owner was never ASKED. Telling them nobody answered would be a
    falsehood; the run takes the undeliverable terminal instead.
    """
    monkeypatch.setattr(driver_module, "_ARM_TIMEOUT_SECONDS", 0.05)
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_with_seed(_ROUND19_SEED_AUTH),
        plan_result_factory=_plan_result_native_versions,
        pass_bar_validate_fn=_schema_pass_bar_oracle,
        subscriber_factory=NeverArmingFactory(),
        originator_wait_seconds=1,
    )
    await _carry_in_flight_spec(store, git, repo, seed_yaml=_ROUND19_SEED_AUTH)

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.FAILED.value
    assert h.ctx["counters"]["pass_bar_validate"] == 0
    assert h.ctx["counters"]["build_trigger"] == 0
    # Zero auth cards ever reached the wire — the proof the claim must match.
    assert _auth_cards(h.ctx["publisher"]) == []
    reasons = " ".join(m for _cid, m, _lvl in h.ctx["notifications"])
    assert "stopped at registering the quality checklist" in reasons
    assert "could not be delivered" in reasons
    assert "Nobody answered" not in reasons
    for internal in ("qa-pass-bars", "auth_surface_bearing", "SPL-007"):
        assert internal not in reasons
    assert "SPL-007 §A.2" in store.get_run(CID)["error"]
    assert [status for status, _d in _door_events(store)] == ["GATED", "undeliverable"]


@pytest.mark.asyncio
async def test_auth_door_defer_is_named_never_reported_as_silence(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """A 'later' answer is an ANSWER — recorded and named, never swallowed.

    The door rides the same generic approval consumer as the product-docs
    checkpoint, whose decision literal includes ``defer``. Waiting on after a
    defer ends the run claiming "nobody answered the confirmation card" — told
    to the very person who answered it.
    """
    h, repo = await _auth_flagged_harness(
        store,
        tmp_path,
        script=[_auth_door_answer("defer")],
        originator_wait_seconds=3600,
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.FAILED.value
    assert h.ctx["counters"]["pass_bar_validate"] == 0
    assert h.ctx["counters"]["build_trigger"] == 0
    reasons = " ".join(m for _cid, m, _lvl in h.ctx["notifications"])
    assert "stopped at registering the quality checklist" in reasons
    assert "set the confirmation card aside" in reasons
    assert "Nobody answered" not in reasons
    for internal in ("qa-pass-bars", "auth_surface_bearing", "SPL-007"):
        assert internal not in reasons
    assert "SPL-007 §A.2" in store.get_run(CID)["error"]

    # The durable row names the answer the owner actually gave.
    statuses = [status for status, _d in _door_events(store)]
    assert statuses == ["GATED", "deferred"]
    verdict = _door_events(store)[-1][1]["auth_confirmation"]
    assert verdict["decision"] == "defer"
    assert verdict["decided_by"] == ORIGINATOR
    assert verdict["request_id"] == _auth_door_request_id(0)

    # No bar landed on the branch and no idempotency sentinel was written.
    show = subprocess.run(
        ["git", "show", f"planning/{CID}:qa/pass-bar-TASK-VER-001.yaml"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert show.returncode != 0
    # ...and the card itself told the owner that 'later' stops the run.
    later = _auth_door_card(h)["summary"]["later_means"]
    assert "stops the run" in later


@pytest.mark.asyncio
async def test_round19_authless_seed_mints_three_validated_bars(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """(b) The same seed with the flag false → one bar per validated-plan task
    (TASK-VER-001/002/003), each guardkit-qa-validate green, registered_at.sha ==
    the PLAN commit sha, committed as ONE commit BEFORE the build trigger."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_with_seed(_ROUND19_SEED_AUTHLESS),
        plan_result_factory=_plan_result_native_versions,
        pass_bar_validate_fn=_schema_pass_bar_oracle,  # the schema-faithful oracle
    )

    await h.driver.drive(CID)

    run = store.get_run(CID)
    assert run["state"] == PlanningState.BUILD_QUEUED.value

    # One bar per task, each run through guardkit's own qa validate (green).
    assert h.ctx["counters"]["pass_bar_validate"] == 3
    assert set(h.ctx["validated_bars"]) == {
        "qa/pass-bar-TASK-VER-001.yaml",
        "qa/pass-bar-TASK-VER-002.yaml",
        "qa/pass-bar-TASK-VER-003.yaml",
    }

    branch = f"planning/{CID}"

    def _show(path: str) -> str:
        return subprocess.run(
            ["git", "show", f"{branch}:{path}"],
            cwd=repo,
            capture_output=True,
            text=True,
        ).stdout

    plan_sha = _leg_sha(store, "feature-plan")
    assert plan_sha
    for task_id in ("TASK-VER-001", "TASK-VER-002", "TASK-VER-003"):
        raw = _show(f"qa/pass-bar-{task_id}.yaml")
        assert raw, f"{task_id} bar not on the branch"
        bar = yaml.safe_load(raw)
        # Mirrors the F2 registered shape exactly, and guardkit-schema-valid.
        _assert_pass_bar_schema(bar)
        assert bar["task_id"] == task_id
        assert bar["auth_surface_bearing"] is False
        # registered_at.sha is the PLAN commit sha (not the bars commit sha).
        assert bar["registered_at"]["sha"] == plan_sha
        # The seed's preconditions + criteria were carried verbatim.
        assert bar["preconditions"] == [
            "suite_green_vs_ledger",
            "analyze_clean",
            "build_artifact",
        ]
        assert [c["id"] for c in bar["criteria"]] == [
            "version-endpoint-AC-001",
            "version-endpoint-AC-002",
        ]
        # The seed's own auth basis is NOT leaked into the authless bar.
        assert "auth_surface_basis" not in bar
        assert "feature_slug" not in bar

    # The bars landed as ONE commit BEFORE BUILD_QUEUED: the qa-pass-bars leg
    # event precedes the build-queued event in the durable log, and the build
    # trigger ran exactly once afterward.
    labels = [
        e["stage_label"]
        for e in store.list_events(CID)
        if e["status"] == "approved"
        and e["stage_label"] in {"feature-plan", "qa-pass-bars", "build-queued"}
    ]
    assert labels == ["feature-plan", "qa-pass-bars", "build-queued"]
    assert h.ctx["counters"]["build_trigger"] == 1
    # The registered sha recorded on the leg event is the plan sha.
    assert _leg_event_sha_field(store, "qa-pass-bars", "registered_at_sha") == plan_sha
    # THE UNFLAGGED PATH IS UNTOUCHED by the auth-confirmation door: no door
    # event, no confirmation card, and the leg receipt carries no auth block.
    assert _door_events(store) == []
    assert not [
        env
        for env in h.ctx["publisher"].envelopes
        if env.payload["details"].get("checkpoint_type") == _AUTH_DOOR_CHECKPOINT_TYPE
    ]
    assert "auth_confirmation" not in _leg_details(store, "qa-pass-bars")


def _leg_event_sha_field(
    store: SqlitePlanningRunStore, stage_label: str, field: str
) -> str | None:
    for e in store.list_events(CID):
        if e["stage_label"] == stage_label and e["status"] == "approved":
            return (json.loads(e["details_json"]) or {}).get(field)
    return None


@pytest.mark.asyncio
async def test_round19_no_seed_fails_loudly_before_the_build(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """(c) An older specialist that ships NO seed → loud, named failure at the
    cheaper plan-commit layer (never a silent skip — the B2 gate would refuse the
    build anyway); no bars, no BUILD_QUEUED."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_with_seed(None),  # no pass-bar-seed-*.yaml
        plan_result_factory=_plan_result_native_versions,
    )

    await h.driver.drive(CID)

    run = store.get_run(CID)
    assert run["state"] == PlanningState.FAILED.value
    assert h.ctx["counters"]["pass_bar_validate"] == 0
    assert h.ctx["counters"]["build_trigger"] == 0
    reasons = " ".join(m for _cid, m, _lvl in h.ctx["notifications"])
    assert "no pass-bar seed" in reasons
    assert any(level == "error" for _, _, level in h.ctx["notifications"])


@pytest.mark.asyncio
async def test_unwired_pass_bar_validate_fails_loudly(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """Flag ON but the pass-bar validate collaborator missing = loud FAILED
    (never a silent skip of the guardkit check)."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_with_seed(_ROUND19_SEED_AUTHLESS),
        plan_result_factory=_plan_result_native_versions,
    )
    # Unwire ONLY the pass-bar oracle (the plan legs stay wired).
    h.driver._deps.validate_pass_bar = None

    await h.driver.drive(CID)

    run = store.get_run(CID)
    assert run["state"] == PlanningState.FAILED.value
    assert run["state"] != PlanningState.BUILD_QUEUED.value
    reasons = " ".join(m for _cid, m, _lvl in h.ctx["notifications"])
    assert "validate_pass_bar" in reasons


# ---------------------------------------------------------------------------
# F2 — the per-feature live-gate REGISTRATION leg (sibling of the pass-bar leg).
# At plan-commit the machine flow registered pass BARS but no live GATE; this
# leg derives the endpoint from the seed's machine criteria, fills the target
# repo's OWN feature-behaviour gate TEMPLATE, appends a mirrored GateEntry to its
# OWN gate registry, and commits both as ONE commit AFTER the bars commit and
# BEFORE the build trigger. Wire-true: the scratch repo carries BYTE-COPIES of
# the LIVE api_test qa/gates/ surface (hash-locked below).
# ---------------------------------------------------------------------------

import hashlib as _hashlib  # noqa: E402

#: Byte-copies of the LIVE api_test F4 gate surface
#: (``/home/richardwoollcott/Projects/appmilla_github/api_test/qa/gates/``).
#: The hashes lock the fixture bytes so a drift from the wire-true surface is
#: caught here rather than silently changing what the leg fills against.
_FEATURE_GATE_TEMPLATE_FIXTURE = _FIXTURES / "feature_behaviour_gate.py"
_GATE_REGISTRY_FIXTURE = _FIXTURES / "gate_registry.yaml"
_FEATURE_GATE_TEMPLATE_SHA256 = (
    "f6a985e5c1d8d0a4ae185f3ffcf5cfaa0b0f74af397339884aa07f112603dfed"
)
_GATE_REGISTRY_SHA256 = (
    "4f397ea62304433a9897fd8aba04502de42481a9c98b01b008d1683dbdcd19c4"
)


def test_gate_surface_fixtures_are_byte_copies_of_the_api_test_surface() -> None:
    """The committed fixtures are the WIRE-TRUE api_test gate surface bytes."""
    tmpl = _FEATURE_GATE_TEMPLATE_FIXTURE.read_bytes()
    reg = _GATE_REGISTRY_FIXTURE.read_bytes()
    assert _hashlib.sha256(tmpl).hexdigest() == _FEATURE_GATE_TEMPLATE_SHA256
    assert _hashlib.sha256(reg).hexdigest() == _GATE_REGISTRY_SHA256


def _git_env() -> dict[str, str]:
    return {
        **__import__("os").environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }


def _seed_gate_surface(
    repo: Path, *, template: bool = True, registry: bool = True
) -> None:
    """Commit BYTE-COPIES of the api_test qa/gates/ surface onto the scratch
    repo's default branch, so the planning branch (forked from it) carries them
    and the leg reads them off the branch exactly as production does."""
    gates = repo / "qa" / "gates"
    gates.mkdir(parents=True, exist_ok=True)
    if template:
        (gates / "feature_behaviour_gate.py").write_bytes(
            _FEATURE_GATE_TEMPLATE_FIXTURE.read_bytes()
        )
    if registry:
        (gates / "registry.yaml").write_bytes(
            _GATE_REGISTRY_FIXTURE.read_bytes()
        )
    env = _git_env()
    subprocess.run(["git", "add", "."], cwd=repo, check=True, env=env)
    subprocess.run(
        ["git", "commit", "-qm", "seed the F4 gate surface"],
        cwd=repo,
        check=True,
        env=env,
    )


async def _schema_gate_registry_oracle(
    worktree: Path, registry_rel: str
) -> ToolOutcome:
    """A gate-registry validate oracle running a schema-faithful check against
    the on-disk appended registry (stands in for the vendored guardkit binary):
    every entry carries id/path/target.base_url_env/pass_bar_ref."""
    import yaml as _yaml

    try:
        data = _yaml.safe_load((worktree / registry_rel).read_text(encoding="utf-8"))
        assert isinstance(data, dict) and isinstance(data.get("gates"), list)
        assert data["gates"], "gates must be non-empty"
        for gate in data["gates"]:
            assert gate.get("id") and gate.get("path")
            assert gate.get("target", {}).get("base_url_env")
            assert gate.get("pass_bar_ref")
    except AssertionError as exc:
        return ToolOutcome(ok=False, detail=f"gate-registry schema invalid — {exc}")
    return ToolOutcome(ok=True)


#: An AUTHLESS seed whose ONLY machine criterion yields NO endpoint (a legitimate
#: non-endpoint feature) — the honest-skip path.
_UNDERIVABLE_SEED_AUTHLESS = (
    "format_version: '2.0'\n"
    "feature_slug: nightly-report\n"
    "auth_surface_bearing: false\n"
    "preconditions:\n"
    "- suite_green_vs_ledger\n"
    "criteria:\n"
    "- id: nightly-AC-001\n"
    "  text: The nightly report job runs to completion each midnight\n"
    "  class: machine\n"
    "  evidence_kind: log\n"
    "  runbook_ref: null\n"
)


def _show_on_branch(repo: Path, path: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "show", f"planning/{CID}:{path}"],
        cwd=repo,
        capture_output=True,
        text=True,
    )


@pytest.mark.asyncio
async def test_f2_derivable_seed_registers_gate_as_one_commit_before_build(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """(1) A derivable GET seed + the adopted gate surface → the filled gate +
    the appended registry land as ONE commit AFTER the bars commit and BEFORE
    build-queued; the durable label order is
    [feature-plan, qa-pass-bars, qa-feature-gate, build-queued]."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    _seed_gate_surface(repo)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_with_seed(_ROUND19_SEED_AUTHLESS),
        plan_result_factory=_plan_result_native_versions,
        pass_bar_validate_fn=_schema_pass_bar_oracle,
        gate_registry_validate_fn=_schema_gate_registry_oracle,
    )

    await h.driver.drive(CID)

    run = store.get_run(CID)
    assert run["state"] == PlanningState.BUILD_QUEUED.value

    # guardkit's own gate-registry validate ran exactly once, on the registry.
    assert h.ctx["counters"]["gate_registry_validate"] == 1
    assert h.ctx["validated_registries"] == ["qa/gates/registry.yaml"]

    # The filled gate landed on the branch: gate_id + request substituted, the
    # runtime /REPLACE_ME guard left intact, valid Python.
    gate = _show_on_branch(repo, "qa/gates/version_endpoint_gate.py")
    assert gate.returncode == 0, gate.stderr
    assert '"gate_id": "version-endpoint",' in gate.stdout
    assert '"request": {"method": "GET", "path": "/version"},' in gate.stdout
    assert 'if spec["request"]["path"] == "/REPLACE_ME":' in gate.stdout
    compile(gate.stdout, "version_endpoint_gate.py", "exec")

    # The registry gained a MIRRORED entry (base_url_env copied, never hardcoded)
    # pointing at the gate with the FIRST minted bar as its pass_bar_ref.
    reg = _show_on_branch(repo, "qa/gates/registry.yaml")
    assert reg.returncode == 0
    reg_data = yaml.safe_load(reg.stdout)
    new = [g for g in reg_data["gates"] if g["id"] == "version-endpoint"]
    assert len(new) == 1
    entry = new[0]
    assert entry["path"] == "qa/gates/version_endpoint_gate.py"
    assert entry["target"]["base_url_env"] == "API_TEST_BASE_URL"
    assert entry["target"]["environment_id"] == "local"
    assert entry["pass_bar_ref"] == "qa/pass-bar-TASK-VER-001.yaml"
    assert entry["preconditions"] == ["suite_vs_ledger"]
    assert entry["preflight"] == ["tool_imports", "base_url_reachable"]
    assert entry["evidence_dir_pattern"] == "qa/gates/evidence/{run_id}"
    # The pre-existing entries + header comments survive byte-untouched.
    assert reg.stdout.startswith("# F4 · gate registry")
    assert {"health", "stats", "version"} <= {g["id"] for g in reg_data["gates"]}

    # ONE commit: the gate script AND the registry edit are in the SAME commit,
    # recorded on the qa-feature-gate leg event, distinct from the bars commit.
    gate_sha = _leg_event_sha_field(store, "qa-feature-gate", "sha")
    bars_sha = _leg_sha(store, "qa-pass-bars")
    assert gate_sha and bars_sha and gate_sha != bars_sha
    names = subprocess.run(
        ["git", "show", "--name-only", "--format=", gate_sha],
        cwd=repo,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert sorted(names) == [
        "qa/gates/registry.yaml",
        "qa/gates/version_endpoint_gate.py",
    ]

    # Durable order: the gate leg sits between the bars and the build queue.
    labels = [
        e["stage_label"]
        for e in store.list_events(CID)
        if e["status"] == "approved"
        and e["stage_label"]
        in {"feature-plan", "qa-pass-bars", "qa-feature-gate", "build-queued"}
    ]
    assert labels == [
        "feature-plan",
        "qa-pass-bars",
        "qa-feature-gate",
        "build-queued",
    ]
    assert h.ctx["counters"]["build_trigger"] == 1
    # registered_at_sha on the leg event is the PLAN commit sha.
    assert (
        _leg_event_sha_field(store, "qa-feature-gate", "registered_at_sha")
        == _leg_sha(store, "feature-plan")
    )


@pytest.mark.asyncio
async def test_f2_underivable_seed_honest_skip_build_still_queues(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """(2) A non-endpoint (underivable) seed → an HONEST skip event, the build
    STILL queues, and ZERO target-repo gate writes (even though the surface is
    adopted — the skip is about the criteria, not the surface)."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    _seed_gate_surface(repo)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_with_seed(_UNDERIVABLE_SEED_AUTHLESS),
        plan_result_factory=_plan_result_native_versions,
    )

    await h.driver.drive(CID)

    run = store.get_run(CID)
    assert run["state"] == PlanningState.BUILD_QUEUED.value
    # The gate-registry validator was never called; nothing was gated.
    assert h.ctx["counters"]["gate_registry_validate"] == 0
    assert h.ctx["counters"]["build_trigger"] == 1
    # No new gate script landed; the registry is byte-unchanged (no new entry).
    assert _show_on_branch(repo, "qa/gates/nightly_report_gate.py").returncode != 0
    reg_data = yaml.safe_load(_show_on_branch(repo, "qa/gates/registry.yaml").stdout)
    assert {g["id"] for g in reg_data["gates"]} == {"health", "stats", "version"}
    # The leg recorded an HONEST skip (idempotency label present, skipped detail).
    ev = _leg_event_details_of(store, "qa-feature-gate")
    assert ev.get("skipped") is True
    assert "no derivable endpoint" in ev.get("reason", "")


def _with_digest_endpoint(result: Any, method: str, path: str) -> Any:
    """The same native spec reply, its digest carrying the OPTIONAL endpoint field."""
    block = f"endpoint:\n  method: {method}\n  path: {path}\n"

    rewritten = 0

    def _rewrite(mapping: dict[str, Any]) -> None:
        nonlocal rewritten
        for key, content in list(mapping.items()):
            if isinstance(content, dict):  # the nested committed-files shape
                _rewrite(content)
            elif str(key).endswith("_digest.yaml"):
                mapping[key] = str(content).replace(
                    "scenarios:\n", block + "scenarios:\n", 1
                )
                rewritten += 1

    _rewrite(result.role_output)
    assert rewritten, "digest fixture never rewritten — the helper found no digest"
    return result


@pytest.mark.asyncio
async def test_f2_digest_endpoint_gates_what_the_prose_cannot_derive(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """THE FEAT-F2B0 CASE: the criteria prose yields nothing (that seed skips on
    its own — proven by the test above), but the digest states the endpoint
    outright, so the gate REGISTERS against the path that was actually asked
    for. This is the whole point of the optional field: a real endpoint feature
    stops falling through to an honest skip."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    _seed_gate_surface(repo)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_with_digest_endpoint(
            _spec_result_with_seed(_UNDERIVABLE_SEED_AUTHLESS),
            "GET",
            "/users/{user_id}",
        ),
        plan_result_factory=_plan_result_native_versions,
        pass_bar_validate_fn=_schema_pass_bar_oracle,
        gate_registry_validate_fn=_schema_gate_registry_oracle,
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    # No skip — a gate was registered, and against the DIGEST's path.
    ev = _leg_event_details_of(store, "qa-feature-gate")
    assert ev.get("skipped") is not True, ev
    assert h.ctx["counters"]["gate_registry_validate"] == 1
    # The gate id still comes from the seed's slug; only the REQUEST comes from
    # the digest. (The test above proves this same seed lands no gate at all.)
    gate = _show_on_branch(repo, "qa/gates/nightly_report_gate.py")
    assert gate.returncode == 0, gate.stderr
    assert '"gate_id": "nightly-report",' in gate.stdout
    assert (
        '"request": {"method": "GET", "path": "/users/{user_id}"},' in gate.stdout
    )
    compile(gate.stdout, "nightly_report_gate.py", "exec")


@pytest.mark.asyncio
async def test_f2_digest_endpoint_absent_leaves_the_prose_path_exactly_as_it_was(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """The field is OPTIONAL: with no endpoint in the digest the derivable seed
    still registers its gate from the prose, byte-identically. Nothing that
    gates today stops gating."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    _seed_gate_surface(repo)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_with_seed(_ROUND19_SEED_AUTHLESS),
        plan_result_factory=_plan_result_native_versions,
        pass_bar_validate_fn=_schema_pass_bar_oracle,
        gate_registry_validate_fn=_schema_gate_registry_oracle,
    )

    await h.driver.drive(CID)

    gate = _show_on_branch(repo, "qa/gates/version_endpoint_gate.py")
    assert gate.returncode == 0, gate.stderr
    assert '"request": {"method": "GET", "path": "/version"},' in gate.stdout


@pytest.mark.asyncio
async def test_f2_digest_endpoint_non_get_does_not_widen_the_gate(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """A digest naming POST is not a wider gate — forge does not know POST's
    happy-path status, so it skips honestly rather than guessing one."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    _seed_gate_surface(repo)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_with_digest_endpoint(
            _spec_result_with_seed(_UNDERIVABLE_SEED_AUTHLESS), "POST", "/users"
        ),
        plan_result_factory=_plan_result_native_versions,
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert h.ctx["counters"]["gate_registry_validate"] == 0
    ev = _leg_event_details_of(store, "qa-feature-gate")
    assert ev.get("skipped") is True
    assert "no derivable endpoint" in ev.get("reason", "")


@pytest.mark.asyncio
async def test_f2_missing_template_honest_skip(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """(3) The repo carries a registry but NO feature-behaviour template on the
    branch → honest skip (the F4 gate surface is not fully adopted); the build
    still queues, zero gate writes."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    _seed_gate_surface(repo, template=False, registry=True)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_with_seed(_ROUND19_SEED_AUTHLESS),
        plan_result_factory=_plan_result_native_versions,
    )

    await h.driver.drive(CID)

    run = store.get_run(CID)
    assert run["state"] == PlanningState.BUILD_QUEUED.value
    assert h.ctx["counters"]["gate_registry_validate"] == 0
    assert _show_on_branch(repo, "qa/gates/version_endpoint_gate.py").returncode != 0
    ev = _leg_event_details_of(store, "qa-feature-gate")
    assert ev.get("skipped") is True
    assert "no qa/gates/feature_behaviour_gate.py" in ev.get("reason", "")


@pytest.mark.asyncio
async def test_f2_validator_red_fails_loudly_zero_branch_mutation(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """(4) Derivable + surface adopted but guardkit qa validate gate-registry is
    RED → loud FAILED, no gate/registry mutation on the branch, no build."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    _seed_gate_surface(repo)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_with_seed(_ROUND19_SEED_AUTHLESS),
        plan_result_factory=_plan_result_native_versions,
        gate_registry_validate=ToolOutcome(
            ok=False, detail="gate-registry schema: unknown precondition"
        ),
    )

    await h.driver.drive(CID)

    run = store.get_run(CID)
    assert run["state"] == PlanningState.FAILED.value
    assert h.ctx["counters"]["build_trigger"] == 0
    # The pre-commit oracle aborted the commit: zero branch mutation.
    assert _show_on_branch(repo, "qa/gates/version_endpoint_gate.py").returncode != 0
    reg_data = yaml.safe_load(_show_on_branch(repo, "qa/gates/registry.yaml").stdout)
    assert {g["id"] for g in reg_data["gates"]} == {"health", "stats", "version"}
    # No approved qa-feature-gate sentinel (the leg failed, not completed/skipped).
    assert not any(
        e["stage_label"] == "qa-feature-gate" and e["status"] == "approved"
        for e in store.list_events(CID)
    )
    reasons = " ".join(m for _cid, m, _lvl in h.ctx["notifications"])
    assert "gate-registry" in reasons
    assert any(level == "error" for _, _, level in h.ctx["notifications"])


@pytest.mark.asyncio
async def test_f2_unwired_gate_registry_validate_fails_loudly(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """(5) Flag ON but the gate-registry validate collaborator missing = loud
    FAILED (never a silent skip of the guardkit check)."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    _seed_gate_surface(repo)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_with_seed(_ROUND19_SEED_AUTHLESS),
        plan_result_factory=_plan_result_native_versions,
    )
    # Unwire ONLY the gate-registry oracle (the other legs stay wired).
    h.driver._deps.validate_gate_registry = None

    await h.driver.drive(CID)

    run = store.get_run(CID)
    assert run["state"] == PlanningState.FAILED.value
    assert run["state"] != PlanningState.BUILD_QUEUED.value
    reasons = " ".join(m for _cid, m, _lvl in h.ctx["notifications"])
    assert "validate_gate_registry" in reasons


@pytest.mark.asyncio
async def test_f2_idempotent_redrive_no_ops_the_gate_leg(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """(6) A crash-window re-drive with the qa-feature-gate sentinel already
    present no-ops the leg: no re-fill, no re-validate, and the run reaches
    BUILD_QUEUED without re-minting anything."""
    _queue(store)
    for to in (
        PlanningState.RUNNING,
        PlanningState.FEATURE_SPEC,
        PlanningState.FEATURE_PLAN,
    ):
        refused = store.transition(
            correlation_id=CID,
            to_state=to,
            actor_identity="seed",
            stage_label="seed",
        )
        assert not isinstance(refused, TransitionRefused)
    # Seed every leg event up to and including the gate registration (the crash
    # window: gate committed + build-queued marker recorded, state not advanced).
    store._record_event(
        correlation_id=CID,
        stage_label="feature-plan",
        status="approved",
        actor_identity="seed",
        details_json=json.dumps(
            {
                "feature_id": "FEAT-AAAA",
                "target_repo": TARGET_REPO,
                "branch": f"planning/{CID}",
                "plan_files": ["features/x/FEAT-AAAA.yaml"],
                "sha": "plan-sha",
            }
        ),
    )
    store._record_event(
        correlation_id=CID,
        stage_label="qa-pass-bars",
        status="approved",
        actor_identity="seed",
        details_json=json.dumps(
            {"feature_id": "FEAT-AAAA", "bar_files": ["qa/pass-bar-TASK-X-001.yaml"]}
        ),
    )
    store._record_event(
        correlation_id=CID,
        stage_label="qa-feature-gate",
        status="approved",
        actor_identity="seed",
        details_json=json.dumps(
            {"feature_id": "FEAT-AAAA", "gate_file": "qa/gates/x_gate.py"}
        ),
    )
    store._record_event(
        correlation_id=CID,
        stage_label="build-queued",
        status="approved",
        actor_identity="seed",
        details_json=json.dumps({"feature_id": "FEAT-AAAA", "build_id": "build-1"}),
    )

    h = _make_driver(store)
    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    # The gate leg no-opped: no fill, no validate, no build re-trigger.
    assert h.ctx["counters"]["gate_registry_validate"] == 0
    assert h.ctx["counters"]["build_trigger"] == 0
    assert h.ctx["counters"]["plan"] == 0


def _leg_event_details_of(
    store: SqlitePlanningRunStore, stage_label: str
) -> dict[str, Any]:
    for e in store.list_events(CID):
        if e["stage_label"] == stage_label and e["status"] == "approved":
            return json.loads(e["details_json"]) or {}
    return {}


# ---------------------------------------------------------------------------
# F2 derivation grammar — the deterministic, conservative endpoint parser.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        # Positive: the exact SPL criterion phrasing → GET + path.
        (
            "A GET request to /version returns the version metadata",
            {"method": "GET", "path": "/version"},
        ),
        ("A GET request to /stats", {"method": "GET", "path": "/stats"}),
        (
            "A GET request to /users/{id}/profile succeeds",
            {"method": "GET", "path": "/users/{id}/profile"},
        ),
    ],
)
def test_derive_get_endpoint_positive(text: str, expected: dict[str, str]) -> None:
    assert PlanningRunDriver._derive_get_endpoint(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "A GET request to the version endpoint",  # no /-rooted path
        "A GET request to",  # no path at all
        "a get request to /version",  # lower-case verb + article
        "A POST request to /version",  # non-GET verb → skip (don't guess status)
        "A PUT request to /version",
        "A DELETE request to /version",
        "You can get request info at /version",  # prose 'get request' mid-sentence
        "The /version response contains exactly three fields",  # no verb phrase
        "GET /version",  # missing the 'A … request to' frame
        "",  # empty
    ],
)
def test_derive_get_endpoint_adversarial_negatives(text: str) -> None:
    assert PlanningRunDriver._derive_get_endpoint(text) is None


def test_derive_feature_gate_endpoint_first_machine_get_wins() -> None:
    """The leg-level derivation ignores non-machine criteria and non-GET verbs,
    returning the FIRST machine criterion that yields a GET endpoint."""
    criteria = [
        {"text": "A GET request to /skip", "class": "operator"},  # not machine
        {"text": "A POST request to /users", "class": "machine"},  # non-GET → skip
        {"text": "A GET request to /version", "class": "machine"},  # first GET win
        {"text": "A GET request to /later", "class": "machine"},
    ]
    d = PlanningRunDriver(SimpleNamespace())  # type: ignore[arg-type]
    assert d._derive_feature_gate_endpoint(criteria) == {
        "method": "GET",
        "path": "/version",
    }


def test_feature_gate_endpoint_from_digest_reads_the_optional_field() -> None:
    """The digest's endpoint field is taken at face value when it names a GET."""
    digest = (
        'feature: get-user-by-id\n'
        'generated: "2026-08-24T09:00:00Z"\n'
        'endpoint:\n'
        '  method: GET\n'
        '  path: /users/{user_id}\n'
        'scenarios: []\n'
    )
    assert PlanningRunDriver._feature_gate_endpoint_from_digest(digest) == {
        "method": "GET",
        "path": "/users/{user_id}",
    }
    # lower-case and padded verbs are the same statement, not a different one
    assert PlanningRunDriver._feature_gate_endpoint_from_digest(
        "endpoint:\n  method: ' get '\n  path: /v\n"
    ) == {"method": "GET", "path": "/v"}


@pytest.mark.parametrize(
    "digest",
    [
        "",  # nothing at all
        "feature: x\nscenarios: []\n",  # field omitted — the common, correct case
        "endpoint:\n  method: POST\n  path: /users\n",  # verb forge cannot gate
        "endpoint:\n  method: GET\n  path: users/1\n",  # not rooted
        "endpoint:\n  method: GET\n",  # no path
        "endpoint: /users/1\n",  # scalar, not a mapping
        "endpoint:\n  - GET\n",  # list, not a mapping
        "just a sentence, not yaml at all",  # parses to a str
        "endpoint:\n  method: GET\n path: /bad\n",  # unparseable YAML
    ],
)
def test_feature_gate_endpoint_from_digest_negatives(digest: str) -> None:
    """Anything short of an explicit rooted GET yields None, so the caller falls
    through to the prose regex and then to an honest skip — never a guessed gate."""
    assert PlanningRunDriver._feature_gate_endpoint_from_digest(digest) is None


def test_derive_feature_gate_endpoint_none_when_no_machine_get() -> None:
    d = PlanningRunDriver(SimpleNamespace())  # type: ignore[arg-type]
    assert d._derive_feature_gate_endpoint([]) is None
    assert (
        d._derive_feature_gate_endpoint(
            [{"text": "A GET request to /x", "class": "operator"}]
        )
        is None
    )
    assert d._derive_feature_gate_endpoint("not-a-list") is None


# ---------------------------------------------------------------------------
# THE DCL LEG IS GONE (struck 2026-08-15, Rich's word).
#
# guardkit deleted the `.dcl` spec track outright (guardkit b138d92c, card Q11):
# `guardkit dcl author` no longer exists and guardkit's own spec_track allows
# only "gherkin". Forge's W1-S2 leg — the harvest into the target repo's
# `.guardkit/dcl-capture/queue.jsonl` and the seat call after it — came out with
# it. These pin the ABSENCE: a target repo still carrying the leftover
# `qa.spec_track: dcl` + `dcl.capture: true` config is simply IGNORED. Forge
# does not police guardkit's config; guardkit does.
# ---------------------------------------------------------------------------


def _labels_in_order(store: SqlitePlanningRunStore, wanted: set[str]) -> list[str]:
    return [
        e["stage_label"]
        for e in store.list_events(CID)
        if e["status"] == "approved" and e["stage_label"] in wanted
    ]


def _seed_leftover_dcl_config(repo: Path) -> None:
    """Commit the WORST-CASE leftover `.guardkit/config.yaml` onto the scratch
    repo: the dcl track AND capture switched on — exactly what the live api_test
    checkout carried when the struck leg harvested a brief into its main tree."""
    gk = repo / ".guardkit"
    gk.mkdir(parents=True, exist_ok=True)
    (gk / "config.yaml").write_text(
        "qa:\n  spec_track: dcl\ndcl:\n  capture: true\n", encoding="utf-8"
    )
    env = _git_env()
    subprocess.run(["git", "add", "."], cwd=repo, check=True, env=env)
    subprocess.run(
        ["git", "commit", "-qm", "seed leftover .guardkit/config.yaml"],
        cwd=repo,
        check=True,
        env=env,
    )


@pytest.mark.asyncio
async def test_leftover_dcl_config_is_ignored_end_to_end(
    store: SqlitePlanningRunStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A target repo whose `.guardkit/config.yaml` still says `spec_track: dcl`
    with `dcl.capture: true` drives to BUILD_QUEUED touching NOTHING dcl-shaped:

    * no `.guardkit/dcl-capture/` in the SHARED checkout (the live defect: the
      struck leg dirtied api_test's main tree with a harvest row),
    * no `dcl-author` ledger event and no dcl-shaped leg label at all,
    * no `.dcl` / `.guardkit/dcl-inputs` file on the planning branch,
    * no guardkit subcommand shelled — the frozen run seam is booby-trapped for
      the whole drive, so ANY `guardkit dcl author` attempt would explode,
    * and the driver carries no `dcl_author` collaborator to call.
    """
    import forge.adapters.guardkit.run as guardkit_run_mod

    async def _boom(**kwargs: Any) -> None:
        raise AssertionError(
            f"the drive shelled guardkit {kwargs.get('subcommand')!r} — "
            "no leg may invoke a guardkit subcommand on this path"
        )

    monkeypatch.setattr(guardkit_run_mod, "run", _boom)

    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    _seed_leftover_dcl_config(repo)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_with_seed(_ROUND19_SEED_AUTHLESS),
        plan_result_factory=_plan_result_native_versions,
        pass_bar_validate_fn=_schema_pass_bar_oracle,
    )
    # The collaborator itself is gone from the deps surface.
    assert not hasattr(h.driver._deps, "dcl_author")

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value

    # (1) The SHARED checkout is untouched by any harvest.
    assert not (repo / ".guardkit" / "dcl-capture").exists()

    # (2) No dcl-shaped ledger row of any kind.
    assert not any("dcl" in e["stage_label"] for e in store.list_events(CID))

    # (3) The chain's leg order runs plan -> bars -> gate -> build with nothing
    #     between the plan and the bars.
    assert _labels_in_order(
        store,
        {"feature-plan", "qa-pass-bars", "qa-feature-gate", "build-queued"},
    ) == ["feature-plan", "qa-pass-bars", "qa-feature-gate", "build-queued"]

    # (4) Nothing dcl-shaped was committed on the planning branch.
    listed = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", f"planning/{CID}"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert listed.returncode == 0
    assert not any(
        f.endswith(".dcl") or "dcl-inputs" in f or "dcl-capture" in f or "qa/dcl/" in f
        for f in listed.stdout.splitlines()
    )


def test_the_planning_driver_module_carries_no_dcl_leg() -> None:
    """The leg's own code is gone — not merely unreachable. (A dormant
    `_dcl_author_leg` would be a live re-wiring away from harvesting again.)

    The one surviving mention of the old label is prose: `_has_leg_event`'s
    docstring, which records that a historical `dcl-author` row in an old ledger
    is data the replay ignores.
    """
    import inspect

    src = inspect.getsource(driver_module)

    assert not [n for n in dir(PlanningRunDriver) if "dcl" in n.lower()]
    for gone in (
        "def _dcl",
        "_read_dcl_activation",
        "_DCL_AUTHOR_STAGE",
        "dcl-capture",
        "dcl_author",
        "spec_track",
    ):
        assert gone not in src, gone

# ---------------------------------------------------------------------------
# THE TYPESCRIPT SHAPE ON THE PLANNING PATH (design §D.3(ii))
#
# The descriptor is where empty test roots become a plan failure: ``[]`` →
# ASSUM-010 turns ANY ``smoke_gates`` block into a containment error. These pin
# that the flat TypeScript shape reaches the 008 descriptor as a real root, and
# that the Python shape's descriptor is byte-unchanged.
# ---------------------------------------------------------------------------


def test_descriptor_carries_the_flat_typescript_root(tmp_path: Path) -> None:
    """ts-api-test's ORIGINAL flat shape yields ``test_roots: ["tests"]``.

    Before the cure this was ``[]`` — the near-blocker that forced the repo to
    be bent into ``tests/health/health.test.ts`` for stage B. With a real root
    in the descriptor the bend is reversible.
    """
    repo = tmp_path / "ts-api-test"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "health.test.ts").write_text(
        "import { it } from 'vitest';\n", encoding="utf-8"
    )
    (repo / "src" / "health").mkdir(parents=True)
    (repo / "src" / "health" / "routes.ts").write_text("export {};\n", encoding="utf-8")

    descriptor = PlanningRunDriver._build_target_repo_descriptor(
        "appmilla/ts-api-test", str(repo)
    )
    assert descriptor == {
        "repo": "appmilla/ts-api-test",
        "test_roots": ["tests"],
    }


def test_descriptor_for_a_python_repo_is_unchanged(tmp_path: Path) -> None:
    """Regression pin: the api_test shape produces exactly what it produced
    before the TypeScript shapes were taught — the exact per-suite roots and
    nothing else."""
    repo = tmp_path / "api_test"
    (repo / "tests" / "health").mkdir(parents=True)
    (repo / "tests" / "users").mkdir(parents=True)
    (repo / "tests" / "test_main.py").write_text("", encoding="utf-8")

    descriptor = PlanningRunDriver._build_target_repo_descriptor(
        "appmilla/api_test", str(repo)
    )
    assert descriptor == {
        "repo": "appmilla/api_test",
        "test_roots": ["tests/health", "tests/users"],
    }


def test_descriptor_degrades_shape_correctly_without_guardkit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With guardkit unimportable, the fallback is shape-correct, not a bare
    ``["tests"]`` — the round-10 defect shape that let 008 invent a prefix."""
    from forge.planning import target_terminal_tools as ttt

    repo = tmp_path / "api_test"
    (repo / "tests" / "health").mkdir(parents=True)
    (repo / "tests" / "users").mkdir(parents=True)

    def _boom(*_args: Any, **_kwargs: Any) -> list[str]:
        raise ttt.TargetTestRootsUnresolved("no guardkit in this interpreter")

    monkeypatch.setattr(ttt, "discover_target_test_roots", _boom)

    descriptor = PlanningRunDriver._build_target_repo_descriptor(
        "appmilla/api_test", str(repo)
    )
    assert descriptor["test_roots"] == ["tests/health", "tests/users"]


# ---------------------------------------------------------------------------
# THE TARGET REPO'S OWN ARCHITECTURE RULES IN THE DESCRIPTOR (2026-08-31)
#
# The plan-writer was never shown the target repository's architecture rules, so
# two features built the week of 2026-08-24 drifted from rules nobody had told
# it about. When the repo keeps docs/architecture-rules.yaml, the rules
# themselves now ride in the descriptor as text (the seat cannot open files).
# When it does not, the descriptor is byte-for-byte what it was.
# ---------------------------------------------------------------------------


_RULES_FILE = """
format_version: "1.0"
repo: api_test
rules:
  - id: R-OV-1
    rule: Database queries live in the feature's crud.py, not in its router.py.
    source_document: docs/architecture/00-system-overview.md
    source_section: Architectural Layers
    source_sentence: "**CRUD Layer (`crud.py`)**: Implements database operations."
    checked_by: >-
      Parse every file under src/ with Python's ast module and report a finding
      for every query built outside crud.py.
    signals:
      kind: call-site-home-file
      home_file: crud.py
  - id: R-ADR001-1
    rule: A feature module does not import another feature module.
    source_document: docs/architecture/adr-001-feature-based-module-structure.md
    source_sentence: "Cross-feature coupling where one module imports internals."
not_rules:
  - id: routers-do-not-hold-business-logic
    would_say: "Business logic lives in a service file."
"""


def _repo_with_tests(tmp_path: Path, name: str = "api_test") -> Path:
    repo = tmp_path / name
    (repo / "tests" / "health").mkdir(parents=True)
    (repo / "tests" / "users").mkdir(parents=True)
    return repo


def test_descriptor_carries_the_repo_architecture_rules(tmp_path: Path) -> None:
    """A repo WITH a rules file: the rules travel as text — id, the rule in one
    sentence, and the sentence it was quoted from. The checker's own machinery
    (checked_by / signals) and the deliberately-left-out list stay behind."""
    repo = _repo_with_tests(tmp_path)
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "architecture-rules.yaml").write_text(
        _RULES_FILE, encoding="utf-8"
    )

    descriptor = PlanningRunDriver._build_target_repo_descriptor(
        "appmilla/api_test", str(repo)
    )
    rules_block = descriptor["architecture_rules"]
    assert rules_block["source_file"] == "docs/architecture-rules.yaml"
    assert rules_block["rules"] == [
        {
            "id": "R-OV-1",
            "rule": (
                "Database queries live in the feature's crud.py, not in its "
                "router.py."
            ),
            "source_document": "docs/architecture/00-system-overview.md",
            "source_sentence": (
                "**CRUD Layer (`crud.py`)**: Implements database operations."
            ),
        },
        {
            "id": "R-ADR001-1",
            "rule": "A feature module does not import another feature module.",
            "source_document": (
                "docs/architecture/adr-001-feature-based-module-structure.md"
            ),
            "source_sentence": (
                "Cross-feature coupling where one module imports internals."
            ),
        },
    ]
    # The rest of the descriptor is untouched.
    assert descriptor["repo"] == "appmilla/api_test"
    assert descriptor["test_roots"] == ["tests/health", "tests/users"]
    # Nothing from the checker's machinery leaks into the request.
    assert "checked_by" not in json.dumps(descriptor)
    assert "not_rules" not in json.dumps(descriptor)


def test_descriptor_omits_the_key_for_a_repo_with_no_rules_file(
    tmp_path: Path,
) -> None:
    """A repo WITHOUT a rules file plans exactly as it did before: no key at
    all, not an empty one."""
    repo = _repo_with_tests(tmp_path, "ts-api-test")

    descriptor = PlanningRunDriver._build_target_repo_descriptor(
        "appmilla/ts-api-test", str(repo)
    )
    assert descriptor == {
        "repo": "appmilla/ts-api-test",
        "test_roots": ["tests/health", "tests/users"],
    }


@pytest.mark.parametrize(
    "content",
    [
        "rules: [ this is not: valid: yaml",  # corrupt
        "format_version: '1.0'\n",  # no rules key
        "rules: a string, not a list\n",  # wrong shape
        "rules:\n  - id: R-1\n",  # a rule with no sentence
        "",  # empty file
    ],
)
def test_unreadable_rules_file_omits_the_key_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, content: str
) -> None:
    """An unreadable, corrupt or empty rules file must never stop a plan: the
    key is left out, a warning is logged, and nothing is raised."""
    repo = _repo_with_tests(tmp_path)
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "architecture-rules.yaml").write_text(content, encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        descriptor = PlanningRunDriver._build_target_repo_descriptor(
            "appmilla/api_test", str(repo)
        )
    assert "architecture_rules" not in descriptor
    assert descriptor["test_roots"] == ["tests/health", "tests/users"]
    assert any(
        "architecture rules" in rec.getMessage()
        or "'rules' list" in rec.getMessage()
        or "no readable rules" in rec.getMessage()
        for rec in caplog.records
    )


def test_rules_file_that_cannot_be_opened_omits_the_key_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The rules path being a directory (or any other read error) degrades the
    same way — warning, omitted key, no exception."""
    repo = _repo_with_tests(tmp_path)
    (repo / "docs" / "architecture-rules.yaml").mkdir(parents=True)

    with caplog.at_level(logging.WARNING):
        descriptor = PlanningRunDriver._build_target_repo_descriptor(
            "appmilla/api_test", str(repo)
        )
    assert "architecture_rules" not in descriptor


def test_a_long_rules_file_is_bounded(tmp_path: Path) -> None:
    """A long rules file cannot swell the planning request: the rule count is
    capped and a very long sentence is trimmed."""
    repo = _repo_with_tests(tmp_path)
    (repo / "docs").mkdir(parents=True)
    long_sentence = "x" * 900
    body = "rules:\n" + "".join(
        f"  - id: R-{n}\n"
        f"    rule: Rule number {n}.\n"
        f'    source_sentence: "{long_sentence}"\n'
        for n in range(200)
    )
    (repo / "docs" / "architecture-rules.yaml").write_text(body, encoding="utf-8")

    descriptor = PlanningRunDriver._build_target_repo_descriptor(
        "appmilla/api_test", str(repo)
    )
    rules = descriptor["architecture_rules"]["rules"]
    assert len(rules) == 60
    assert len(rules[0]["source_sentence"]) == 400
    assert rules[0]["source_sentence"].endswith("\u2026")


def test_the_real_api_test_rules_file_shape_reads(tmp_path: Path) -> None:
    """The shape api_test actually ships — comments, block scalars, a
    not_rules section — reads into twelve-ish plain entries with no loss of the
    rule text."""
    repo = _repo_with_tests(tmp_path)
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "architecture-rules.yaml").write_text(
        "# a comment\n" + _RULES_FILE, encoding="utf-8"
    )
    block = PlanningRunDriver._read_architecture_rules(str(repo))
    assert block is not None
    assert [r["id"] for r in block["rules"]] == ["R-OV-1", "R-ADR001-1"]


# ---------------------------------------------------------------------------
# THE STAMP NORMALIZER hook (Rich's condition 1, 2026-08-16; coordinator
# review condition 5 the same day)
#
# ``guardkit qa normalize-stamps`` runs against the planning worktree
# immediately BEFORE the plan-commit validate, so the rule-minted verifier
# stamps are WRITTEN on the planning branch and ride the plan commit. Success
# writes + commits; an older guardkit (no such subcommand) continues and is
# receipted; forge declares ``feature_files:`` when the plan-writer omitted it
# (the live 008 shape); the not-wired path is receipted.
#
# Condition 5: the STOP on partial / refused / failed is gated on the routing
# law's ENFORCEMENT — feature-level ``routing_law:`` wins → repo
# ``.guardkit/config.yaml`` → off. ENFORCED → the run stops with a card naming
# the refused titles verbatim. NOT ENFORCED → the plan PROCEEDS: the decided
# stamps already written ride the commit, every title is receipted, a WARNING
# is logged, and the owner gets ONE plain un-@mentioned line in the thread. A
# broken normalizer under NOT ENFORCED is an ERROR + receipt, never a stop.
# The hook never writes ``routing_law`` anywhere (pinned).
# ---------------------------------------------------------------------------

from forge.planning.target_terminal_tools import StampNormalizerOutcome  # noqa: E402

_STAMP_SPEC_REL = "features/stats-endpoint/stats-endpoint.feature"
_MOON_TITLE = (
    "The moon is made of a very particular kind of cheese that no rule family "
    "in the design has ever heard about at all"
)
_UNDECIDABLE_TITLES = (_MOON_TITLE, "Another undecidable one")


def _plan_yaml_rel(feature_id: str) -> str:
    return f".guardkit/features/{feature_id}.yaml"


def _stamping_normalizer(
    counters_sink: dict[str, Any], *, outcome: Any = None, write: bool = False
):
    """A fake normalize_stamps collaborator that behaves like guardkit's: it
    WRITES ``scenarios:`` into the worktree's plan YAML (unless told to refuse /
    be unavailable) and reports what it did. Records call order + the YAML it
    saw so the tests can prove ordering and the feature_files fill. With
    ``outcome`` + ``write=True`` it writes that outcome's ``stamped`` map first
    (guardkit's PARTIAL law: decided stamps are on disk before the refusal is
    reported)."""

    async def _normalize(worktree: Path, feature_id: str) -> StampNormalizerOutcome:
        counters_sink.setdefault("order", []).append("normalize_stamps")
        yaml_path = worktree / _plan_yaml_rel(feature_id)
        counters_sink["yaml_seen"] = yaml_path.read_text(encoding="utf-8") if yaml_path.is_file() else None
        counters_sink["worktree"] = str(worktree)
        if outcome is not None:
            if write and outcome.stamped:
                text = counters_sink["yaml_seen"] or ""
                text += "scenarios:\n" + "".join(
                    f'  "{t}":\n    verifier: "{v}"\n' for t, v in outcome.stamped.items()
                )
                yaml_path.write_text(text, encoding="utf-8")
            return outcome
        # guardkit's write_stamps shape: append the scenarios map.
        text = counters_sink["yaml_seen"] or ""
        text += 'scenarios:\n  "ok":\n    verifier: "hurl"\n'
        yaml_path.write_text(text, encoding="utf-8")
        return StampNormalizerOutcome(
            status="written",
            detail="1 scenario(s) stamped by rule, 0 already stamped (untouched)",
            stamped={"ok": "hurl"},
            rules={"ok": "R9"},
        )

    return _normalize


def _partial_outcome() -> StampNormalizerOutcome:
    """The exit-3 shape: two decided (stamped + written), two refused."""
    return StampNormalizerOutcome(
        status="partial",
        detail=(
            "2 scenario(s) undecidable by rule — no fallback home, none invented; "
            "2 scenario(s) stamped by rule and written, 0 already stamped (untouched)"
        ),
        refused_titles=_UNDECIDABLE_TITLES,
        stamped={"Reading the current server time": "hurl", "ok": "probe:process"},
        rules={"Reading the current server time": "R9", "ok": "R1"},
        written=True,
    )


def _commit_repo_routing_law(repo: Path, value: str) -> None:
    """``routing_law: <value>`` in the target repo's ``.guardkit/config.yaml``,
    COMMITTED so the planning branch's worktree carries it (the same file and
    key guardkit's plan-load half reads)."""
    cfg = repo / ".guardkit" / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(f"toolchain:\n  test: pytest -q\nrouting_law: {value}\n", encoding="utf-8")
    env = {
        **__import__("os").environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", f"routing_law {value}"], cwd=repo, check=True, env=env)


def _plan_result_with_feature_flag(value: str):
    """A plan-writer YAML that carries its own ``routing_law:`` (the
    feature-level escape hatch guardkit honours over the repo flag)."""

    def _factory(feature_id: str) -> Any:
        r = _plan_result_native(feature_id)
        r.role_output[_plan_yaml_rel(feature_id)] = (
            f"id: {feature_id}\nrouting_law: {value}\ntasks:\n- id: TASK-STAT-001\n"
        )
        return r

    return _factory


_UNENFORCED_LINE = (
    "2 of 4 examples could not be given a verification home by rule —\n"
    f"  - {_MOON_TITLE}\n"
    "  - Another undecidable one\n"
    "— the plan proceeds; this repo does not enforce the routing law yet"
)


def _share_order(sink: dict[str, Any], h: _Harness) -> None:
    """One call-order list across the fake normalizer and the harness's
    validate, so a test can assert normalizer-then-validate."""
    sink["order"] = h.ctx["counters"].setdefault("order", [])


def _show(repo: Path, branch: str, path: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "show", f"{branch}:{path}"], cwd=repo, capture_output=True, text=True
    )


@pytest.mark.asyncio
async def test_stamp_normalizer_success_writes_stamps_that_ride_the_plan_commit(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """Hook success: the normalizer runs BEFORE validate against the planning
    worktree; what it wrote (the ``scenarios:`` map) and forge's own
    ``feature_files:`` fill are ON THE BRANCH in the plan commit; the plan
    receipts and the owner line say so."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    sink: dict[str, Any] = {}
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_native(),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_stamping_normalizer(sink),
    )
    _share_order(sink, h)
    await h.driver.drive(CID)
    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value

    feature_id = h.ctx["counters"]["last_feature_id"]
    branch = f"planning/{CID}"
    committed = _show(repo, branch, _plan_yaml_rel(feature_id))
    assert committed.returncode == 0, committed.stderr
    # (1) the normalizer's write rides the plan commit — Rich's condition 1.
    assert 'scenarios:\n  "ok":\n    verifier: "hurl"' in committed.stdout
    # (2) forge declared the universe the plan-writer omitted: the committed
    #     spec .feature path, appended before the normalizer ran.
    assert "feature_files:" in committed.stdout
    assert f'  - "{_STAMP_SPEC_REL}"' in committed.stdout
    assert "feature_files:" in (sink["yaml_seen"] or "")  # fill BEFORE the hook
    # (3) ordering: normalizer, then validate — validate read the stamped tree.
    assert h.ctx["counters"]["order"] == ["normalize_stamps", "validate"]
    assert h.ctx["counters"]["validate"] == 1
    # (4) receipts: the durable feature-plan event names what happened.
    details = _leg_details(store, "feature-plan")
    rec = details["stamp_normalizer"]
    assert rec["status"] == "written"
    assert rec["stamped"] == {"ok": "hurl"}
    assert rec["stamped_count"] == 1
    assert rec["per_title"] == {"ok": "stamped by normalizer (rule R9): hurl"}
    assert rec["feature_files_filled_by_forge"] == [_STAMP_SPEC_REL]
    # condition 5: enforcement resolved + receipted (nothing set → off/default)
    assert rec["enforcement"] == "off" and rec["enforcement_source"] == "default"
    assert rec["stops_the_run"] is False
    assert "proceeded_unenforced" not in rec  # nothing to proceed past
    # (5) the owner line, plain words.
    plan_lines = [m for _, m, lvl in h.ctx["notifications"] if "queueing the build" in m]
    assert plan_lines and "1 verifier stamp(s) minted by rule and committed with the plan" in plan_lines[0]


@pytest.mark.asyncio
async def test_stamp_normalizer_refusal_stops_the_run_with_the_titles_on_the_card(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """ENFORCED (repo ``routing_law: enforced``) + refusal (undecidable titles,
    an older all-or-nothing guardkit): the run STOPS at the plan leg, validate
    is never reached, the plan is NOT committed, and the owner's card names
    every refused title VERBATIM plus what to do. The machine's rewrite round
    is switched off here: this is the card the refusal itself produces (rule 8
    of the rewrite lane keeps that path byte for byte; with the round on, the
    machine rewrites first and, since rule 6b, the model gets its turn)."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    _commit_repo_routing_law(repo, "enforced")
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    titles = _UNDECIDABLE_TITLES
    refusal = StampNormalizerOutcome(
        status="refused",
        detail="2 scenario(s) undecidable by rule (R1–R10) — no fallback home; nothing was written",
        refused_titles=titles,
    )
    sink: dict[str, Any] = {}
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_native(),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_stamping_normalizer(sink, outcome=refusal),
        rewrite_on_refusal=False,
    )
    _share_order(sink, h)
    assert await _drive_to_failure(h, store) == PlanningState.FAILED.value
    # validate never ran — the normalizer refused BEFORE it.
    assert h.ctx["counters"]["validate"] == 0
    assert h.ctx["counters"]["order"] == ["normalize_stamps"]
    # nothing was committed on the plan side (the spec leg's commit stands).
    feature_id = h.ctx["counters"]["last_feature_id"]
    branch = f"planning/{CID}"
    assert _show(repo, branch, _plan_yaml_rel(feature_id)).returncode != 0
    assert _show(repo, branch, _STAMP_SPEC_REL).returncode == 0
    # the card: plain stage name, both titles verbatim, what to do, nothing built.
    errors = [m for _, m, lvl in h.ctx["notifications"] if lvl == "error"]
    assert len(errors) == 1
    card = errors[0]
    assert card.startswith(f"Planning run {CID} stopped at writing the task plan")
    for t in titles:
        assert f"  - {t}" in card
    assert "no rule to decide which verifier proves them" in card
    assert "nothing was stamped and nothing was built" in card
    assert "This repo enforces the routing law" in card
    assert f".guardkit/features/{feature_id}.yaml" in card
    assert "toolchain, hurl, exam, probe:bus, probe:process, flutter, playwright, operator" in card
    assert "operator only for attended human work" in card
    assert "R9" not in card and "rule R" not in card  # no rule ids on the face
    # the machine record keeps the internal reason + the titles.
    run = store.get_run(CID)
    assert "stamp normalizer refused" in (run["error"] or "")
    assert titles[1] in (run["error"] or "")


@pytest.mark.asyncio
async def test_stamp_normalizer_enforced_partial_stops_with_the_titles_on_the_card(
    store: SqlitePlanningRunStore, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """ENFORCED + PARTIAL (exit 3: two decided + written, two refused): the run
    STOPS, validate never runs, nothing reaches the branch (the worktree's
    decided stamps die with the aborted commit), the card names the two
    refused titles verbatim (no rule ids), the log says the law is enforced.
    The machine's rewrite round is switched off here: this is the card the
    refusal itself produces (rule 8 of the rewrite lane; with the round on,
    the machine rewrites first and, since rule 6b, the model gets its turn)."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    _commit_repo_routing_law(repo, "enforced")
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    sink: dict[str, Any] = {}
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_native(),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_stamping_normalizer(sink, outcome=_partial_outcome(), write=True),
        rewrite_on_refusal=False,
    )
    _share_order(sink, h)
    with caplog.at_level("WARNING", logger="forge.planning.driver"):
        assert await _drive_to_failure(h, store) == PlanningState.FAILED.value
    assert h.ctx["counters"]["validate"] == 0
    assert h.ctx["counters"]["order"] == ["normalize_stamps"]
    feature_id = h.ctx["counters"]["last_feature_id"]
    assert _show(repo, f"planning/{CID}", _plan_yaml_rel(feature_id)).returncode != 0
    errors = [m for _, m, lvl in h.ctx["notifications"] if lvl == "error"]
    assert len(errors) == 1
    card = errors[0]
    assert card.startswith(f"Planning run {CID} stopped at writing the task plan")
    for t in _UNDECIDABLE_TITLES:
        assert f"  - {t}" in card
    assert "2 scenario(s) had no rule to decide which verifier proves them" in card
    assert "This repo enforces the routing law" in card
    assert "R1" not in card and "R9" not in card
    # no plain "proceeds" line went out — the run stopped
    assert not any("the plan proceeds" in m for _, m, _ in h.ctx["notifications"])
    assert any(
        r.levelname == "ERROR" and "IS enforced" in r.getMessage() for r in caplog.records
    )
    run = store.get_run(CID)
    assert "stamp normalizer partial" in (run["error"] or "")


@pytest.mark.asyncio
async def test_stamp_normalizer_not_enforced_partial_proceeds_with_receipt_and_one_plain_line(
    store: SqlitePlanningRunStore, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """NOT ENFORCED (no flag anywhere) + PARTIAL: the plan PROCEEDS to validate
    + commit + BUILD_QUEUED; the decided stamps the normalizer wrote are ON THE
    BRANCH; the receipt names every refused title and every stamped title with
    its rule; a WARNING is logged; ONE plain un-@mentioned line (exact text)
    reaches the owner in the thread; the committed YAML carries no
    ``routing_law:`` (the hook never writes it)."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    sink: dict[str, Any] = {}
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_native(),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_stamping_normalizer(sink, outcome=_partial_outcome(), write=True),
    )
    _share_order(sink, h)
    with caplog.at_level("WARNING", logger="forge.planning.driver"):
        await h.driver.drive(CID)
    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    # proceeded: normalizer, then validate, then the commit landed
    assert h.ctx["counters"]["order"] == ["normalize_stamps", "validate"]
    feature_id = h.ctx["counters"]["last_feature_id"]
    committed = _show(repo, f"planning/{CID}", _plan_yaml_rel(feature_id))
    assert committed.returncode == 0, committed.stderr
    data = yaml.safe_load(committed.stdout)
    # the DECIDED stamps ride the commit; the refused titles are absent (never invented)
    assert data["scenarios"] == {
        "Reading the current server time": {"verifier": "hurl"},
        "ok": {"verifier": "probe:process"},
    }
    assert "routing_law" not in committed.stdout
    assert "routing_law" not in data
    # the receipt: status, enforcement, every title one line each
    rec = _leg_details(store, "feature-plan")["stamp_normalizer"]
    assert rec["status"] == "partial"
    assert rec["enforcement"] == "off" and rec["enforcement_source"] == "default"
    assert rec["stops_the_run"] is False
    assert rec["proceeded_unenforced"] is True
    assert rec["refused_titles"] == list(_UNDECIDABLE_TITLES)
    assert rec["per_title"] == {
        "Reading the current server time": "stamped by normalizer (rule R9): hurl",
        "ok": "stamped by normalizer (rule R1): probe:process",
        _MOON_TITLE: "refused: no rule could decide a verification home",
        "Another undecidable one": "refused: no rule could decide a verification home",
    }
    assert rec["rules"] == {"Reading the current server time": "R9", "ok": "R1"}
    assert rec["owner_line"] == _UNENFORCED_LINE
    assert rec["owner_line_sent"] == "sent"
    # the WARNING
    warns = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any(
        "PROCEEDS" in r.getMessage() and "Another undecidable one" in r.getMessage()
        for r in warns
    )
    assert not any(r.levelname == "ERROR" for r in caplog.records)
    # ONE plain line, exact text, un-@mentioned, level info (no prefix), in the thread
    lines = [(m, lvl) for _, m, lvl in h.ctx["notifications"] if "the plan proceeds" in m]
    assert lines == [(_UNENFORCED_LINE, "info")]
    assert (_UNENFORCED_LINE, False) in h.ctx["mentions"]
    # every OTHER line kept its mention
    assert all(mention for m, mention in h.ctx["mentions"] if m != _UNENFORCED_LINE)
    # no error card
    assert not any(lvl == "error" for _, _, lvl in h.ctx["notifications"])
    # the plan-complete line's clause says what happened, in plain words
    plan_lines = [m for _, m, lvl in h.ctx["notifications"] if "queueing the build" in m]
    assert plan_lines and "2 verifier stamp(s) minted by rule and committed with the plan, 2 example(s) left without one (named above)" in plan_lines[0]
    # the plain line went out BEFORE the plan-complete line
    order = [m for _, m, _ in h.ctx["notifications"]]
    assert order.index(_UNENFORCED_LINE) < order.index(plan_lines[0])


@pytest.mark.asyncio
async def test_stamp_normalizer_not_enforced_failed_proceeds_with_an_error_receipt(
    store: SqlitePlanningRunStore, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """NOT ENFORCED + a cannot-run failure: a broken normalizer must not kill
    an un-enforced chain — ERROR logged, receipted, the plan PROCEEDS
    unstamped; no titles to name so no plain line, the plan-complete clause
    says the stamps were not minted."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    failed = StampNormalizerOutcome(
        status="failed",
        detail="guardkit qa normalize-stamps could not run (exit 2): stamp normalizer: feature FEAT-X: `feature_files:` must be a list",
    )
    sink: dict[str, Any] = {}
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_native(),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_stamping_normalizer(sink, outcome=failed),
    )
    _share_order(sink, h)
    with caplog.at_level("WARNING", logger="forge.planning.driver"):
        await h.driver.drive(CID)
    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert h.ctx["counters"]["order"] == ["normalize_stamps", "validate"]
    errs = [r for r in caplog.records if r.levelname == "ERROR"]
    assert any(
        "FAILED" in r.getMessage() and "must not kill an un-enforced chain" in r.getMessage()
        for r in errs
    )
    rec = _leg_details(store, "feature-plan")["stamp_normalizer"]
    assert rec["status"] == "failed"
    assert rec["enforcement"] == "off"
    assert rec["proceeded_unenforced"] is True
    assert rec["owner_line"].startswith("no owner line: no refused titles to name")
    assert "owner_line_sent" not in rec
    assert not any(lvl == "error" for _, _, lvl in h.ctx["notifications"])
    assert not any("the plan proceeds" in m for _, m, _ in h.ctx["notifications"])
    plan_lines = [m for _, m, lvl in h.ctx["notifications"] if "queueing the build" in m]
    assert plan_lines and "verifier stamps NOT minted — the stamp normalizer could not run and this repo does not enforce the routing law yet" in plan_lines[0]
    feature_id = h.ctx["counters"]["last_feature_id"]
    committed = _show(repo, f"planning/{CID}", _plan_yaml_rel(feature_id)).stdout
    assert "scenarios:" not in committed and "routing_law" not in committed


@pytest.mark.asyncio
async def test_stamp_normalizer_feature_level_off_wins_over_repo_enforced(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """The escape hatch: repo ``routing_law: enforced`` but the plan YAML says
    ``routing_law: off`` (YAML 1.1 parses the bare token as False — absorbed)
    → NOT enforced → a partial proceeds; the receipt names the source."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    _commit_repo_routing_law(repo, "enforced")
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    sink: dict[str, Any] = {}
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_native(),
        plan_result_factory=_plan_result_with_feature_flag("off"),
        normalize_stamps_fn=_stamping_normalizer(sink, outcome=_partial_outcome(), write=True),
    )
    await h.driver.drive(CID)
    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    rec = _leg_details(store, "feature-plan")["stamp_normalizer"]
    assert rec["enforcement"] == "off" and rec["enforcement_source"] == "feature"
    assert rec["proceeded_unenforced"] is True
    assert (_UNENFORCED_LINE, False) in h.ctx["mentions"]


@pytest.mark.asyncio
async def test_stamp_normalizer_feature_level_enforced_wins_over_a_silent_repo(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """The other direction: no repo flag, the plan YAML says ``routing_law:
    enforced`` → ENFORCED → a partial stops with the card."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    sink: dict[str, Any] = {}
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_native(),
        plan_result_factory=_plan_result_with_feature_flag("enforced"),
        normalize_stamps_fn=_stamping_normalizer(sink, outcome=_partial_outcome(), write=True),
    )
    assert await _drive_to_failure(h, store) == PlanningState.FAILED.value
    errors = [m for _, m, lvl in h.ctx["notifications"] if lvl == "error"]
    assert len(errors) == 1 and f"  - {_MOON_TITLE}" in errors[0]
    assert h.ctx["counters"]["validate"] == 0


@pytest.mark.asyncio
async def test_stamp_normalizer_unenforced_line_is_receipted_as_not_sent_without_a_notifier(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """A composition with no notifier wired: the plan still proceeds and the
    receipt says 'line not sent (no notifier)' — never a silent drop."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    sink: dict[str, Any] = {}
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_native(),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_stamping_normalizer(sink, outcome=_partial_outcome(), write=True),
    )
    h.driver._deps.publish_notification = None
    await h.driver.drive(CID)
    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    rec = _leg_details(store, "feature-plan")["stamp_normalizer"]
    assert rec["owner_line"] == _UNENFORCED_LINE
    assert rec["owner_line_sent"] == "line not sent (no notifier)"


def test_the_hook_never_writes_routing_law_in_its_source() -> None:
    """Static pin (condition 5): forge only READS ``routing_law`` — no string
    the hook's writers could emit carries ``routing_law:``. Walks every
    non-docstring string constant in the three modules that touch the plan
    YAML / the config."""
    import ast

    from forge.pipeline import routing_stamps as rs
    from forge.planning import target_terminal_tools as ttt

    offenders: list[str] = []
    for mod in (driver_module, ttt, rs):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        docstrings: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                body = getattr(node, "body", [])
                if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                    docstrings.add(id(body[0].value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                if "routing_law:" in node.value or "routing_law :" in node.value:
                    offenders.append(f"{Path(mod.__file__).name}:{node.lineno}: {node.value[:60]!r}")
    assert offenders == [], offenders


@pytest.mark.asyncio
async def test_stamp_normalizer_unavailable_continues_and_is_receipted(
    store: SqlitePlanningRunStore, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An older guardkit (no ``qa normalize-stamps``): the run CONTINUES to
    validate + commit + BUILD_QUEUED (backward compatible until the rebake),
    the log says 'normalizer unavailable', and the plan receipts + owner line
    say the plan is unstamped — never silent."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    unavailable = StampNormalizerOutcome(
        status="unavailable",
        detail=(
            "normalizer unavailable: the guardkit on this image has no "
            "`qa normalize-stamps` subcommand (Error: No such command "
            "'normalize-stamps'.); verifier stamps were NOT minted"
        ),
    )
    sink: dict[str, Any] = {}
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_native(),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_stamping_normalizer(sink, outcome=unavailable),
    )
    _share_order(sink, h)
    with caplog.at_level("WARNING", logger="forge.planning.driver"):
        await h.driver.drive(CID)
    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert h.ctx["counters"]["order"] == ["normalize_stamps", "validate"]
    assert any("normalizer unavailable" in r.getMessage() for r in caplog.records)
    details = _leg_details(store, "feature-plan")
    rec = details["stamp_normalizer"]
    assert rec["status"] == "unavailable"
    assert "No such command" in rec["detail"]
    assert rec["stamped_count"] == 0
    # the plan committed WITHOUT scenarios: (honest) but WITH forge's
    # feature_files: fill (the universe is on the branch for the rebaked run).
    feature_id = h.ctx["counters"]["last_feature_id"]
    committed = _show(repo, f"planning/{CID}", _plan_yaml_rel(feature_id)).stdout
    assert "scenarios:" not in committed
    assert "feature_files:" in committed
    plan_lines = [m for _, m, lvl in h.ctx["notifications"] if "queueing the build" in m]
    assert plan_lines and "verifier stamps NOT minted" in plan_lines[0]
    assert "rebake pending" in plan_lines[0]


@pytest.mark.asyncio
async def test_stamp_normalizer_failure_stops_the_run_with_the_reason(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """ENFORCED + a cannot-run failure (not a refusal, not unavailable) is
    loud: the run stops, validate never runs, the card names the reason."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    _commit_repo_routing_law(repo, "enforced")
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    failed = StampNormalizerOutcome(
        status="failed",
        detail="guardkit qa normalize-stamps could not run (exit 2): stamp normalizer: feature FEAT-X: `feature_files:` must be a list",
    )
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_native(),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_stamping_normalizer({}, outcome=failed),
    )
    assert await _drive_to_failure(h, store) == PlanningState.FAILED.value
    assert h.ctx["counters"]["validate"] == 0
    errors = [m for _, m, lvl in h.ctx["notifications"] if lvl == "error"]
    assert len(errors) == 1
    assert "the verifier-stamp normalizer could not run" in errors[0]
    assert "`feature_files:` must be a list" in errors[0]
    assert "nothing was built" in errors[0]


@pytest.mark.asyncio
async def test_stamp_normalizer_not_wired_is_receipted_and_byte_identical_otherwise(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """Unwired (hermetic composition): the plan leg proceeds exactly as before
    (no fill, no write, validate runs) and the receipts say ``not-wired``."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_native(),
        plan_result_factory=_plan_result_native,
    )
    await h.driver.drive(CID)
    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert h.ctx["counters"]["order"] == ["validate"]
    details = _leg_details(store, "feature-plan")
    assert details["stamp_normalizer"]["status"] == "not-wired"
    feature_id = h.ctx["counters"]["last_feature_id"]
    committed = _show(repo, f"planning/{CID}", _plan_yaml_rel(feature_id)).stdout
    assert "feature_files:" not in committed  # no fill when the hook is not wired
    assert "scenarios:" not in committed


@pytest.mark.asyncio
async def test_stamp_normalizer_leaves_a_declared_feature_files_alone(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """When the plan-writer DID declare ``feature_files:`` forge does not touch
    it (a present key is the writer's statement) — no fill in the receipts."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)

    def _plan_with_universe(feature_id: str) -> Any:
        r = _plan_result_native(feature_id)
        r.role_output[_plan_yaml_rel(feature_id)] = (
            f"id: {feature_id}\nfeature_files:\n  - {_STAMP_SPEC_REL}\n"
            "tasks:\n- id: TASK-STAT-001\n"
        )
        return r

    sink: dict[str, Any] = {}
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_native(),
        plan_result_factory=_plan_with_universe,
        normalize_stamps_fn=_stamping_normalizer(sink),
    )
    await h.driver.drive(CID)
    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    rec = _leg_details(store, "feature-plan")["stamp_normalizer"]
    assert rec["status"] == "written"
    assert "feature_files_filled_by_forge" not in rec
    feature_id = h.ctx["counters"]["last_feature_id"]
    committed = _show(repo, f"planning/{CID}", _plan_yaml_rel(feature_id)).stdout
    assert committed.count("feature_files:") == 1


# -- LIVE: the whole plan leg with the REAL guardkit normalizer ------------------
#
# Skips unless a guardkit checkout carrying the normalizer is reachable (see
# tests/forge/planning/_live_guardkit.py). Real git (WorktreeGitRunner), the
# real ``guardkit qa normalize-stamps`` CLI through the real parser, forge's
# real ``feature_files:`` fill on a plan YAML shaped like the live 008 output
# (no feature_files, no scenarios — api_test FEAT-F924) — and the plan commit
# on the planning branch carries the rule-minted stamps. Rich's condition 1,
# end to end.

from tests.forge.planning._live_guardkit import live_guardkit_or_skip, live_run_fn  # noqa: E402

_LIVE_TITLE = "The endpoint is unaffected by database unavailability"
_LIVE_FEATURE = (
    "Feature: stats\n"
    f"  Scenario: {_LIVE_TITLE}\n"
    "    Given the database is unavailable\n"
    "    When I request the statistics\n"
    "    Then the response is served from memory\n"
)


def _live_spec_result(slug: str = "stats-endpoint") -> Any:
    return SimpleNamespace(
        outcome=SimpleNamespace(value="completed"),
        role_output={
            f"{slug}.feature": _LIVE_FEATURE,
            f"{slug}_assumptions.yaml": "assumptions: []\n",
            f"{slug}_summary.md": "# summary\n",
            f"{slug}_digest.yaml": (
                f"feature: {slug}\n"
                "generated: '2026-08-16T10:00:00Z'\n"
                "scenarios:\n"
                f"- title: {_LIVE_TITLE}\n"
                "  tags: []\n"
                "  sentence: The endpoint keeps answering when the database is down.\n"
                "assumptions: []\n"
            ),
            f"pass-bar-seed-{slug}.yaml": _AUTHLESS_SEED_YAML,
            "validation.json": json.dumps(
                {"accepted": True, "errors": [], "gates_run": ["gherkin_backstop"]}
            ),
        },
        reason=None,
    )


@pytest.mark.asyncio
async def test_live_plan_leg_commits_rule_minted_stamps_with_the_real_normalizer(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    from forge.planning.target_terminal_tools import make_normalize_stamps

    checkout, python = live_guardkit_or_skip(Path(__file__))
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_live_spec_result(),
        plan_result_factory=_plan_result_native,  # NO feature_files, NO scenarios
        normalize_stamps_fn=make_normalize_stamps(run_fn=live_run_fn(checkout, python)),
    )
    await h.driver.drive(CID)
    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value

    feature_id = h.ctx["counters"]["last_feature_id"]
    committed = _show(repo, f"planning/{CID}", _plan_yaml_rel(feature_id))
    assert committed.returncode == 0, committed.stderr
    data = yaml.safe_load(committed.stdout)
    # forge's fill named the universe; guardkit's rule R1 minted the stamp;
    # both are IN THE PLAN COMMIT on the planning branch.
    assert data["feature_files"] == [_STAMP_SPEC_REL]
    assert data["scenarios"] == {_LIVE_TITLE: {"verifier": "probe:process"}}
    assert data["tasks"] == [{"id": "TASK-STAT-001"}]  # nothing else touched
    rec = _leg_details(store, "feature-plan")["stamp_normalizer"]
    assert rec["status"] == "written"
    assert rec["stamped"] == {_LIVE_TITLE: "probe:process"}
    assert rec["per_title"] == {_LIVE_TITLE: "stamped by normalizer (rule R1): probe:process"}
    assert rec["feature_files_filled_by_forge"] == [_STAMP_SPEC_REL]
    assert rec["stamps_on_branch"] == 1
    assert rec["enforcement"] == "off"
    assert h.ctx["counters"]["validate"] == 1


@pytest.mark.asyncio
async def test_live_plan_leg_enforced_refusal_card_names_the_real_normalizers_titles(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """ENFORCED repo: the default fixture feature ('Scenario: ok / Given a') is
    undecidable by every rule — the REAL normalizer answers PARTIAL (exit 3,
    nothing decided), the run stops, the card names 'ok' verbatim, and no
    plan YAML reaches the branch. The machine's rewrite round is switched off
    here: this is the card the refusal itself produces (rule 8 of the rewrite
    lane; with the round on, the machine rewrites first and, since rule 6b,
    the model gets its turn)."""
    from forge.planning.target_terminal_tools import make_normalize_stamps

    checkout, python = live_guardkit_or_skip(Path(__file__))
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    _commit_repo_routing_law(repo, "enforced")
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_native(),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=make_normalize_stamps(run_fn=live_run_fn(checkout, python)),
        rewrite_on_refusal=False,
    )
    assert await _drive_to_failure(h, store) == PlanningState.FAILED.value
    assert h.ctx["counters"]["validate"] == 0
    feature_id = h.ctx["counters"]["last_feature_id"]
    assert _show(repo, f"planning/{CID}", _plan_yaml_rel(feature_id)).returncode != 0
    errors = [m for _, m, lvl in h.ctx["notifications"] if lvl == "error"]
    assert len(errors) == 1
    assert "  - ok\n" in errors[0]
    assert "1 scenario(s) had no rule to decide which verifier proves them" in errors[0]
    assert "This repo enforces the routing law" in errors[0]


@pytest.mark.asyncio
async def test_live_plan_leg_not_enforced_partial_proceeds_with_the_real_normalizer(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """NOT ENFORCED (no flag): the same undecidable 'ok' through the REAL
    normalizer (exit 3, nothing decided) — the plan PROCEEDS to BUILD_QUEUED,
    the receipt names 'ok' as refused, the owner's plain line reads
    '1 of 1 examples …', and the committed YAML carries forge's fill but no
    ``scenarios:`` and no ``routing_law:``."""
    from forge.planning.target_terminal_tools import make_normalize_stamps

    checkout, python = live_guardkit_or_skip(Path(__file__))
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_native(),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=make_normalize_stamps(run_fn=live_run_fn(checkout, python)),
    )
    await h.driver.drive(CID)
    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert h.ctx["counters"]["validate"] == 1
    feature_id = h.ctx["counters"]["last_feature_id"]
    committed = _show(repo, f"planning/{CID}", _plan_yaml_rel(feature_id))
    assert committed.returncode == 0, committed.stderr
    data = yaml.safe_load(committed.stdout)
    assert data["feature_files"] == [_STAMP_SPEC_REL]
    assert "scenarios" not in data and "routing_law" not in data
    rec = _leg_details(store, "feature-plan")["stamp_normalizer"]
    assert rec["status"] == "partial" and rec["refused_titles"] == ["ok"]
    assert rec["enforcement"] == "off" and rec["proceeded_unenforced"] is True
    expected_line = (
        "1 of 1 examples could not be given a verification home by rule —\n"
        "  - ok\n"
        "— the plan proceeds; this repo does not enforce the routing law yet"
    )
    assert rec["owner_line"] == expected_line and rec["owner_line_sent"] == "sent"
    assert (expected_line, False) in h.ctx["mentions"]
    assert not any(lvl == "error" for _, _, lvl in h.ctx["notifications"])


# ---------------------------------------------------------------------------
# ADVISORY DISAGREEMENTS reach the owner as ONE plain line — WHATEVER the
# status and whether or not the repo enforces the law (a legal-but-wrong
# stamp PASSES the law; the stamp is never changed, so the line IS the
# mechanism). Rich's ruling 08-18, drive-19 datum.
# ---------------------------------------------------------------------------

_DISAGREEMENTS = (
    {"title": "The endpoint returns the count", "stamped": "toolchain", "rule_home": "hurl", "rule": "R9", "evidence": "the endpoint returns"},
    {"title": "Unauthenticated requests are rejected", "stamped": "toolchain", "rule_home": "hurl", "rule": "R9", "evidence": "requests are rejected"},
)
_DISAGREEMENTS_LINE = (
    "2 example(s) carry a verification home the rules would not have chosen —\n"
    "  - The endpoint returns the count — stamped toolchain, the rules say hurl\n"
    "  - Unauthenticated requests are rejected — stamped toolchain, the rules say hurl\n"
    "— the stamps stand as written (nothing was changed); worth a look before this feature graduates"
)


def _nothing_to_do_with_disagreements() -> StampNormalizerOutcome:
    return StampNormalizerOutcome(
        status="nothing-to-do",
        detail="2 scenario(s) already stamped, none unstamped",
        already_stamped=("The endpoint returns the count", "Unauthenticated requests are rejected"),
        written=False,
        disagreements=_DISAGREEMENTS,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("enforcement", ["off", "enforced"])
async def test_stamp_disagreements_reach_the_owner_on_a_clean_run_whatever_the_enforcement(
    store: SqlitePlanningRunStore, tmp_path: Path, enforcement: str
) -> None:
    """A fully-successful normalizer (nothing-to-do — the model stamped
    everything) that DISAGREES with two model stamps: the plan proceeds to
    BUILD_QUEUED under BOTH enforcement modes (a legal wrong stamp is not a
    law violation), the receipt carries the disagreements, and the owner
    gets exactly ONE plain un-@mentioned line naming them; no error card,
    no stamp changed."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    if enforcement == "enforced":
        _commit_repo_routing_law(repo, "enforced")
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    sink: dict[str, Any] = {}
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_native(),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_stamping_normalizer(sink, outcome=_nothing_to_do_with_disagreements()),
    )
    _share_order(sink, h)
    await h.driver.drive(CID)
    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    rec = _leg_details(store, "feature-plan")["stamp_normalizer"]
    assert rec["status"] == "nothing-to-do"
    assert rec["enforcement"] == enforcement
    assert rec["disagreement_count"] == 2
    assert [d["title"] for d in rec["disagreements"]] == [d["title"] for d in _DISAGREEMENTS]
    assert rec["disagreements_line"] == _DISAGREEMENTS_LINE
    assert rec["disagreements_line_sent"] == "sent"
    lines = [(m, lvl) for _, m, lvl in h.ctx["notifications"] if "the rules would not have chosen" in m]
    assert lines == [(_DISAGREEMENTS_LINE, "info")]
    assert (_DISAGREEMENTS_LINE, False) in h.ctx["mentions"]
    assert not any(lvl == "error" for _, _, lvl in h.ctx["notifications"])
    # no "the plan proceeds… does not enforce" line — nothing was refused
    assert not any("could not be given a verification home" in m for _, m, _ in h.ctx["notifications"])


# ---------------------------------------------------------------------------
# A DOCUMENTATION TASK'S QUALITY CHECKLIST (build-FEAT-44A8, 2026-09-04)
#
# FEAT-44A8's fourth task, TASK-44A8-004, is typed `documentation` in its own
# front matter — the type that tells guardkit to run no tests. Its registered
# pass bar nonetheless carried the FEATURE's two machine criteria, identical to
# task 001's: evidence about how the running endpoint behaves, asked of a task
# that only edits docs/API.md. The four files under fixtures/ are the real
# bytes from that run (see their PROVENANCE.txt).
#
# The fix: a task the PLAN ITSELF types documentation gets the checklist it can
# be held to — the criteria written on the task — and the file says why. Every
# other task mints exactly what it minted before.
# ---------------------------------------------------------------------------

_44A8_FIXTURES = _FIXTURES / "pass-bars-44a8-20260904"
_BAR_OF_RECORD_001 = yaml.safe_load(
    (_44A8_FIXTURES / "pass-bar-TASK-44A8-001.yaml").read_text(encoding="utf-8")
)
_BAR_OF_RECORD_004 = yaml.safe_load(
    (_44A8_FIXTURES / "pass-bar-TASK-44A8-004.yaml").read_text(encoding="utf-8")
)
_TASK_MD_001 = (_44A8_FIXTURES / "TASK-44A8-001-add-etag-generation.md").read_text(
    encoding="utf-8"
)
_TASK_MD_004 = (_44A8_FIXTURES / "TASK-44A8-004-update-docs.md").read_text(
    encoding="utf-8"
)
_TASK_FILE_004 = "tasks/backlog/user-list-etag/TASK-44A8-004-update-docs.md"


def _seed_of_record() -> dict[str, Any]:
    """The feature-grain seed the two bars of record were fanned out from.

    Reconstructed from the registered bar itself: the leg carries the seed's
    ``preconditions`` and ``criteria`` verbatim, so the bar of record IS the
    seed's checklist.
    """
    return {
        "format_version": _BAR_OF_RECORD_001["format_version"],
        "preconditions": list(_BAR_OF_RECORD_001["preconditions"]),
        "criteria": [dict(c) for c in _BAR_OF_RECORD_001["criteria"]],
    }


def test_the_two_bars_of_record_are_identical_in_criteria() -> None:
    """The defect, pinned: a documentation task's bar was a copy of a feature
    task's, machine criteria and all."""
    assert _BAR_OF_RECORD_001["criteria"] == _BAR_OF_RECORD_004["criteria"]
    assert all(c["class"] == "machine" for c in _BAR_OF_RECORD_004["criteria"])
    assert "task_type: documentation" in _TASK_MD_004
    assert "task_type: feature" in _TASK_MD_001


def test_feature_task_bar_is_unchanged() -> None:
    """A feature task mints EXACTLY the bar of record — no note, no narrowing."""
    minted = PlanningRunDriver._mint_pass_bar_yaml(
        task_id="TASK-44A8-001",
        seed=_seed_of_record(),
        sha=_BAR_OF_RECORD_001["registered_at"]["sha"],
        date=_BAR_OF_RECORD_001["registered_at"]["date"],
        task_type=driver_module.task_type_from_front_matter(_TASK_MD_001),
        task_file="tasks/backlog/user-list-etag/TASK-44A8-001-add-etag-generation.md",
        task_text=_TASK_MD_001,
    )
    assert driver_module._DOCS_BAR_NOTE_MARKER not in minted
    bar = yaml.safe_load(minted)
    _assert_pass_bar_schema(bar)
    assert bar == _BAR_OF_RECORD_001


def test_documentation_task_bar_drops_the_feature_machine_criteria() -> None:
    """The fix: TASK-44A8-004's bar no longer asks a documentation task for
    machine evidence about the endpoint; it lists what that task delivers, and
    the file says why."""
    minted = PlanningRunDriver._mint_pass_bar_yaml(
        task_id="TASK-44A8-004",
        seed=_seed_of_record(),
        sha=_BAR_OF_RECORD_004["registered_at"]["sha"],
        date=_BAR_OF_RECORD_004["registered_at"]["date"],
        task_type=driver_module.task_type_from_front_matter(_TASK_MD_004),
        task_file=_TASK_FILE_004,
        task_text=_TASK_MD_004,
    )
    bar = yaml.safe_load(minted)
    # Still a bar guardkit's own qa validate accepts, still the same task.
    _assert_pass_bar_schema(bar)
    assert bar["task_id"] == "TASK-44A8-004"
    assert bar["registered_at"] == _BAR_OF_RECORD_004["registered_at"]
    assert bar["preconditions"] == _BAR_OF_RECORD_004["preconditions"]
    assert bar["negative_paths"] == _BAR_OF_RECORD_004["negative_paths"]
    # None of the feature's criteria survive onto it.
    feature_texts = {c["text"] for c in _BAR_OF_RECORD_004["criteria"]}
    assert not feature_texts & {c["text"] for c in bar["criteria"]}
    # What it carries instead: the three criteria written on the task itself.
    assert [c["text"] for c in bar["criteria"]] == [
        "Documentation updated to include ETag support details",
        "Examples of conditional GET requests provided",
        "All modified files pass project-configured lint/format checks with zero errors",
    ]
    assert all(c["evidence_kind"] == "log" for c in bar["criteria"])
    assert len({c["id"] for c in bar["criteria"]}) == 3
    # And the file says, in plain words, why it is not the feature's checklist.
    assert driver_module._DOCS_BAR_NOTE_MARKER in minted
    assert _TASK_FILE_004 in minted
    assert "guardkit runs" in minted and "no tests for that type" in minted


def test_documentation_task_with_no_criteria_of_its_own_keeps_a_valid_bar() -> None:
    """A documentation task whose file lists no criteria, against an all-machine
    seed: the bar keeps the feature's criteria — a bar with none at all is
    invalid and would stop the whole plan — and the note says so, so nobody
    reads them as this task's work."""
    minted = PlanningRunDriver._mint_pass_bar_yaml(
        task_id="TASK-44A8-009",
        seed=_seed_of_record(),
        sha="e95128f8d34cb20a6d8eca8b76e9ff79af09c3dd",
        date="2026-09-04",
        task_type="documentation",
        task_file="tasks/backlog/user-list-etag/TASK-44A8-009-notes.md",
        task_text="---\nid: TASK-44A8-009\ntask_type: documentation\n---\n\n# no criteria\n",
    )
    bar = yaml.safe_load(minted)
    _assert_pass_bar_schema(bar)
    assert bar["criteria"] == _BAR_OF_RECORD_004["criteria"]
    assert "could not be narrowed" in minted


def test_documentation_task_keeps_only_non_machine_criteria_when_it_has_them() -> None:
    """No criteria on the task file, but the feature checklist has an operator
    item: the machine ones go, the operator one stays."""
    seed = _seed_of_record()
    seed["criteria"] = seed["criteria"] + [
        {
            "id": "user-list-etag-support-AC-009",
            "text": "An operator confirms the published docs read correctly",
            "class": "operator",
            "evidence_kind": "operator_signoff",
            "runbook_ref": "docs/runbooks/docs-review.md",
        }
    ]
    minted = PlanningRunDriver._mint_pass_bar_yaml(
        task_id="TASK-44A8-009",
        seed=seed,
        sha="e95128f8d34cb20a6d8eca8b76e9ff79af09c3dd",
        date="2026-09-04",
        task_type="documentation",
        task_file="tasks/backlog/user-list-etag/TASK-44A8-009-notes.md",
        task_text="---\ntask_type: documentation\n---\n\n# no criteria section\n",
    )
    bar = yaml.safe_load(minted)
    _assert_pass_bar_schema(bar)
    assert [c["id"] for c in bar["criteria"]] == ["user-list-etag-support-AC-009"]


def test_task_type_is_read_from_the_plans_own_front_matter() -> None:
    """The type comes from the same place guardkit reads it, and an unreadable
    or absent declaration means 'behave exactly as before'."""
    read = driver_module.task_type_from_front_matter
    assert read(_TASK_MD_004) == "documentation"
    assert read(_TASK_MD_001) == "feature"
    # guardkit's own alias for documentation.
    assert read("---\ntask_type: research\n---\n") == "research"
    assert "research" in driver_module._DOCS_TASK_TYPES
    # No front matter, no key, empty file → nothing declared.
    assert read("# just a heading\n") is None
    assert read("---\nid: TASK-1\n---\n") is None
    assert read("") is None


def test_task_criteria_read_both_bullet_shapes() -> None:
    """Plain bullets (what the 008 planner writes) and checkbox bullets both
    count — reading only the checkbox form is the 2026-08-14 close-blocker."""
    text = (
        "---\ntask_type: documentation\n---\n\n"
        "## Acceptance Criteria\n\n"
        "- [ ] The guide is written\n"
        "- AC-KEEP: The examples run\n\n"
        "## Implementation Notes\n\n"
        "- not a criterion\n"
    )
    criteria = driver_module.task_acceptance_criteria(text)
    assert [(c["id"], c["text"]) for c in criteria] == [
        ("AC-1", "The guide is written"),
        ("AC-KEEP", "The examples run"),
    ]


def _plan_result_docs_task(feature_id: str) -> Any:
    """A validated plan of two tasks — one feature, one documentation — each
    with the task file the plan wrote, so the type travels with the plan."""
    feature_yaml = (
        f"id: {feature_id}\n"
        "tasks:\n"
        "- id: TASK-VER-001\n"
        "  file_path: tasks/backlog/version-endpoint/TASK-VER-001-endpoint.md\n"
        "- id: TASK-VER-004\n"
        "  file_path: tasks/backlog/version-endpoint/TASK-VER-004-update-docs.md\n"
    )
    return SimpleNamespace(
        outcome=SimpleNamespace(value="completed"),
        role_output={
            f".guardkit/features/{feature_id}.yaml": feature_yaml,
            "tasks/backlog/version-endpoint/TASK-VER-001-endpoint.md": (
                "---\nid: TASK-VER-001\ntask_type: feature\n---\n\n"
                "## Acceptance Criteria\n\n- The endpoint answers\n"
            ),
            "tasks/backlog/version-endpoint/TASK-VER-004-update-docs.md": (
                "---\nid: TASK-VER-004\ntask_type: documentation\n---\n\n"
                "## Acceptance Criteria\n\n"
                "- The version endpoint is documented\n"
                "- An example request is shown\n"
            ),
            "validation.json": json.dumps(
                {"accepted": True, "errors": [], "gates_run": ["feature_validate"]}
            ),
        },
        reason=None,
    )


@pytest.mark.asyncio
async def test_documentation_task_bar_lands_narrowed_on_the_branch(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """End to end through real git: the driver reads each task's declared type
    off the branch, so the documentation task's registered bar carries its own
    criteria while the feature task's carries the feature's — both validated,
    both committed, and the build still queued."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_with_seed(_ROUND19_SEED_AUTHLESS),
        plan_result_factory=_plan_result_docs_task,
        pass_bar_validate_fn=_schema_pass_bar_oracle,
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert h.ctx["counters"]["build_trigger"] == 1
    assert set(h.ctx["validated_bars"]) == {
        "qa/pass-bar-TASK-VER-001.yaml",
        "qa/pass-bar-TASK-VER-004.yaml",
    }

    branch = f"planning/{CID}"

    def _show(path: str) -> str:
        return subprocess.run(
            ["git", "show", f"{branch}:{path}"],
            cwd=repo,
            capture_output=True,
            text=True,
        ).stdout

    feature_bar_raw = _show("qa/pass-bar-TASK-VER-001.yaml")
    feature_bar = yaml.safe_load(feature_bar_raw)
    _assert_pass_bar_schema(feature_bar)
    assert driver_module._DOCS_BAR_NOTE_MARKER not in feature_bar_raw
    assert [c["id"] for c in feature_bar["criteria"]] == [
        "version-endpoint-AC-001",
        "version-endpoint-AC-002",
    ]

    docs_bar_raw = _show("qa/pass-bar-TASK-VER-004.yaml")
    docs_bar = yaml.safe_load(docs_bar_raw)
    _assert_pass_bar_schema(docs_bar)
    assert driver_module._DOCS_BAR_NOTE_MARKER in docs_bar_raw
    assert "TASK-VER-004-update-docs.md" in docs_bar_raw
    assert [c["text"] for c in docs_bar["criteria"]] == [
        "The version endpoint is documented",
        "An example request is shown",
    ]
    assert not any(c["evidence_kind"] == "screenshot" for c in docs_bar["criteria"])

    # The leg receipt names which bars were narrowed, without opening them.
    assert _leg_details(store, "qa-pass-bars")["documentation_tasks"] == [
        "TASK-VER-004"
    ]


# ---------------------------------------------------------------------------
# THE MACHINE REWRITES ON A REFUSAL BEFORE IT ASKS (2026-09-06)
#
# Two of Rich's sentences that weekend stopped at the plan leg because the
# spec writer wrote worked examples the routing law could not prove. The law
# was right to refuse; the machine was wrong to ask a person to fix what it
# could have fixed itself. The plan leg now sends the refused examples back to
# the spec writer ONCE, as its own note, and stamps again; only a second
# refusal stops the run. Three touches, as before.
# ---------------------------------------------------------------------------

from forge.planning.driver import PlanningRunDriver as _Driver  # noqa: E402

_MACHINE_NOTE_AUTHOR = "planning-driver (stamp normalizer refusal)"

#: Rule 2, the spec's own words, with the two fixture titles verbatim.
_MACHINE_NOTE_FOR_TITLES = (
    "These worked examples cannot be proven as written, because they describe "
    "the database or the code rather than what a caller sees:\n"
    f"- {_MOON_TITLE}\n"
    "- Another undecidable one\n"
    "\n"
    "Rewrite each of them as what can be proven, keeping the behaviour itself "
    "unchanged: a request to the endpoint and the reply it gets (the method and "
    "path, the status code, and what is in the body), or, for behaviour one "
    "request cannot show — two requests at once, timing — the repository test "
    "that proves it, named. Keep every other worked example exactly as it is."
)

#: Rule 5: the ONE un-mentioned line when the rewrite stamped clean.
_MACHINE_REWRITE_LINE_EXPECTED = (
    "2 of the worked examples could not be proven as written, so the machine "
    "asked the spec writer to rewrite them as what the endpoint does. What "
    "changed: 1 example changed. The plan carries on."
)

#: Rule 6: the sentence at the top of the plan-stop card after the rewrite
#: landed and the second stamping still refused titles. (A rewrite that
#: changed nothing no longer reaches the card: since rule 6b the model gets
#: its turn first — see the tests below.)
_STOP_STILL_UNPROVEN = (
    "The machine already asked the spec writer once to rewrite these as what "
    "the endpoint does; the rewrite still could not be proven."
)


def _refusal_outcome() -> StampNormalizerOutcome:
    """The older all-or-nothing guardkit's exit-2 shape: two refused titles."""
    return StampNormalizerOutcome(
        status="refused",
        detail="2 scenario(s) undecidable by rule (R1–R10) — no fallback home; nothing was written",
        refused_titles=_UNDECIDABLE_TITLES,
    )


class _Crash(BaseException):
    """A process death mid-leg: not an Exception, so nothing on the drive path
    catches it — the durable rows are whatever was written before it."""


def _sequenced_normalizer(
    sink: dict[str, Any], outcomes: list[Any], *, legacy: bool = False
):
    """A fake normalize_stamps that answers from ``outcomes`` in call order
    (the last one repeats). ``None`` = behave like a clean guardkit: write the
    fixture's one stamp and report ``written``. A ``_Crash`` instance is
    RAISED — the simulated process death.

    Since rule 1a (2026-09-07) the production collaborator takes a keyword,
    ``rules_only``; this fake takes it too and records, per call, what the
    driver passed under ``sink["rules_only"]`` — ``True`` when the keyword
    was passed, ``None`` when the call was positional. ``legacy=True`` builds
    the collaborator in its older shape — positional ``(worktree,
    feature_id)`` only — the one the driver must still work with."""

    async def _answer(
        worktree: Path, feature_id: str, rules_only: bool | None
    ) -> StampNormalizerOutcome:
        sink.setdefault("order", []).append("normalize_stamps")
        sink.setdefault("rules_only", []).append(rules_only)
        # The spec .feature this stamping saw in the worktree — the bytes the
        # real normalizer would classify — so a test can prove WHICH spec a
        # stamping ran on (the approved one, or a rewrite's).
        feature_path = worktree / _SPEC_FEATURE_REL
        sink.setdefault("feature_seen", []).append(
            feature_path.read_text(encoding="utf-8") if feature_path.is_file() else None
        )
        sink["calls"] = sink.get("calls", 0) + 1
        outcome = outcomes[min(sink["calls"] - 1, len(outcomes) - 1)]
        if isinstance(outcome, _Crash):
            raise outcome
        if outcome is not None:
            return outcome
        yaml_path = worktree / _plan_yaml_rel(feature_id)
        text = yaml_path.read_text(encoding="utf-8") if yaml_path.is_file() else ""
        yaml_path.write_text(
            text + 'scenarios:\n  "ok":\n    verifier: "hurl"\n', encoding="utf-8"
        )
        return StampNormalizerOutcome(
            status="written",
            detail="1 scenario(s) stamped by rule, 0 already stamped (untouched)",
            stamped={"ok": "hurl"},
            rules={"ok": "R9"},
        )

    if legacy:

        async def _legacy(worktree: Path, feature_id: str) -> StampNormalizerOutcome:
            return await _answer(worktree, feature_id, None)

        return _legacy

    async def _normalize(
        worktree: Path, feature_id: str, *, rules_only: bool | None = None
    ) -> StampNormalizerOutcome:
        return await _answer(worktree, feature_id, rules_only)

    return _normalize


def _rewritten_spec_result(base: Any | None = None, slug: str = "stats-endpoint") -> Any:
    """Round two of the spec: the same one example, said as what the endpoint
    does on the digest — so the comparison reads "1 example changed"."""
    result = base if base is not None else _spec_result_native(slug)
    result.role_output[f"{slug}_digest.yaml"] = _digest_yaml(slug).replace(
        "The service answers the request in the ordinary way.",
        "GET /stats answers 200 with the statistics in the body.",
    )
    return result


def _spec_by_round(first: Any, second: Any):
    """The spec-writer's reply per round: the plain first round, then the reply
    to whatever note came back (the harness passes ``validate_feedback``)."""

    def _factory(validate_feedback: str | None) -> Any:
        return first if validate_feedback is None else second

    return _factory


def _enforced_repo(tmp_path: Path) -> tuple[Path, WorktreeGitRunner]:
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    _commit_repo_routing_law(repo, "enforced")
    return repo, WorktreeGitRunner(worktrees_root=tmp_path / "wt")


def _approved_spec_rows(store: SqlitePlanningRunStore) -> list[dict[str, Any]]:
    return [
        json.loads(e["details_json"] or "{}")
        for e in store.list_events(CID)
        if e["stage_label"] == "feature-spec" and e["status"] == "approved"
    ]


def _error_cards(h: _Harness) -> list[str]:
    return [m for _, m, lvl in h.ctx["notifications"] if lvl == "error"]


@pytest.mark.asyncio
async def test_a_refusal_sends_the_machine_note_once_and_a_clean_second_stamping_carries_on(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """Rules 1–5 and 9, end to end in a real scratch repo: the enforced law
    refuses two examples → the spec writer is dispatched with the machine's
    note VERBATIM as ``validate_feedback`` and the prior spec as
    ``revision_of`` → the approved row is recorded again with the owner's
    decision carried forward and the machine's block → the plan is written
    again on the SAME feature id → the second stamping is clean → ONE
    un-mentioned line in the spec's words, no card, and the build is queued.
    """
    repo, git = _enforced_repo(tmp_path)
    _queue(store)
    sink: dict[str, Any] = {}
    feature_ids: list[str] = []

    def _plan_factory(feature_id: str) -> Any:
        feature_ids.append(feature_id)
        return _plan_result_native(feature_id)

    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result_factory=_spec_by_round(_spec_result_native(), _rewritten_spec_result()),
        plan_result_factory=_plan_factory,
        normalize_stamps_fn=_sequenced_normalizer(sink, [_refusal_outcome(), None]),
    )
    _share_order(sink, h)

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    # The spec writer ran twice: plain, then with the machine's note verbatim
    # and the prior artifact set keyed by bare filename.
    assert h.ctx["counters"]["spec"] == 2
    revisions = h.ctx["counters"]["spec_revisions"]
    assert len(revisions) == 1
    assert revisions[0]["validate_feedback"] == _MACHINE_NOTE_FOR_TITLES
    assert set(revisions[0]["revision_of"]) == {
        "stats-endpoint.feature",
        "stats-endpoint_assumptions.yaml",
        "stats-endpoint_summary.md",
        "stats-endpoint_digest.yaml",
    }
    # The plan leg ran from the top twice on ONE feature id; the normalizer
    # twice; validate only once (the second time, after the clean stamping).
    assert h.ctx["counters"]["plan"] == 2
    assert len(set(feature_ids)) == 1
    assert sink["order"] == ["normalize_stamps", "normalize_stamps", "validate"]
    # ONE un-mentioned line, in the spec's words; no card, no error.
    assert (_MACHINE_REWRITE_LINE_EXPECTED, False) in h.ctx["mentions"]
    assert sum(
        1 for _, m, _ in h.ctx["notifications"] if "the machine asked the spec writer" in m
    ) == 1
    assert _error_cards(h) == []
    # The approved spec rows: the owner's, then the re-recorded one with the
    # owner's decision carried forward UNCHANGED and the machine's block.
    first, second = _approved_spec_rows(store)
    assert first["spec_review"]["decided_by"] == ORIGINATOR
    assert second["spec_review"] == first["spec_review"]
    assert "rewritten_by_machine" not in first
    assert second["rewritten_by_machine"] == {
        "round": 1,
        "author": _MACHINE_NOTE_AUTHOR,
        "note": _MACHINE_NOTE_FOR_TITLES,
        "refused_titles": [_MOON_TITLE, "Another undecidable one"],
        "changes": "1 example changed",
    }
    # The rewritten spec is what the plan of record carries on the branch.
    shown = _show(repo, f"planning/{CID}", "features/stats-endpoint/stats-endpoint_digest.yaml")
    assert "GET /stats answers 200" in shown.stdout
    # The plan receipt carries the rewrite whichever way it went (rule 9).
    receipt = _leg_details(store, "feature-plan")["stamp_normalizer"]
    assert receipt["status"] == "written"
    assert receipt["rewrite"]["note"] == _MACHINE_NOTE_FOR_TITLES
    assert receipt["rewrite"]["author"] == _MACHINE_NOTE_AUTHOR
    assert receipt["rewrite"]["refused_titles"] == [_MOON_TITLE, "Another undecidable one"]
    assert receipt["rewrite"]["changes"] == "1 example changed"
    assert receipt["rewrite"]["owner_line"] == _MACHINE_REWRITE_LINE_EXPECTED
    assert receipt["rewrite"]["owner_line_sent"] == "sent"
    # The machine round's draft row names the author and never says "Your note".
    drafts = [
        json.loads(e["details_json"])["spec_draft"]
        for e in store.list_events(CID)
        if e["stage_label"] == "feature-spec-draft" and e["status"] == "drafted"
    ]
    assert drafts[-1]["rewrite"]["author"] == _MACHINE_NOTE_AUTHOR
    assert "Your note" not in json.dumps(drafts[-1]["card"])
    # No card was put in front of a person a second time.
    assert h.ctx["counters"].get("build_trigger") == 1


@pytest.mark.asyncio
async def test_the_re_recorded_row_carries_the_sign_in_answer_and_the_pass_bars_leg_finds_it(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """Rule 4 on the answer that matters most: an auth-flagged spec whose
    sign-in question the owner answered on the digest card. After the
    machine's rewrite the re-recorded row still carries it, and the pass-bars
    leg reads it from there — one pause, no second door, BUILD_QUEUED."""
    repo, git = _enforced_repo(tmp_path)
    _queue(store)
    sink: dict[str, Any] = {}
    slug = "version-endpoint"
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result_factory=_spec_by_round(
            _spec_result_with_seed(_ROUND19_SEED_AUTH, slug=slug),
            _rewritten_spec_result(_spec_result_with_seed(_ROUND19_SEED_AUTH, slug=slug), slug=slug),
        ),
        plan_result_factory=_plan_result_native_versions,
        pass_bar_validate_fn=_schema_pass_bar_oracle,
        normalize_stamps_fn=_sequenced_normalizer(sink, [_refusal_outcome(), None]),
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    first, second = _approved_spec_rows(store)
    assert first["spec_review"]["sign_in_answer"] == "confirmed"
    assert second["spec_review"] == first["spec_review"]
    assert second["rewritten_by_machine"]["round"] == 1
    bars = _leg_details(store, "qa-pass-bars")
    assert bars["auth_confirmation"]["outcome"] == "confirmed"
    assert bars["auth_confirmation"]["answered_on"] == "the spec digest card"
    assert h.ctx["counters"]["pass_bar_validate"] == 3
    # Only the digest card was ever put in front of a person.
    assert len(h.ctx["publisher"].envelopes) == 1


@pytest.mark.asyncio
async def test_a_second_refusal_stops_with_the_card_and_the_extra_sentence(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """Rule 6, "still could not be proven": the rewrite landed but the second
    stamping refused titles again — the plan-stop card as today, opening with
    the one sentence, the titles verbatim; the machine record names the round.
    Never a third try."""
    repo, git = _enforced_repo(tmp_path)
    _queue(store)
    sink: dict[str, Any] = {}
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result_factory=_spec_by_round(_spec_result_native(), _rewritten_spec_result()),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_sequenced_normalizer(
            sink, [_refusal_outcome(), _refusal_outcome()]
        ),
    )
    _share_order(sink, h)

    assert await _drive_to_failure(h, store) == PlanningState.FAILED.value

    assert h.ctx["counters"]["spec"] == 2
    assert h.ctx["counters"]["plan"] == 2
    assert sink["order"] == ["normalize_stamps", "normalize_stamps"]
    cards = _error_cards(h)
    assert len(cards) == 1
    card = cards[0]
    feature_id = h.ctx["counters"]["last_feature_id"]
    assert card.startswith(
        f"Planning run {CID} stopped at writing the task plan. {_STOP_STILL_UNPROVEN} "
        f"The verifier stamps could not all be minted by rule for feature {feature_id}."
    )
    for t in _UNDECIDABLE_TITLES:
        assert f"  - {t}" in card
    assert "nothing was stamped and nothing was built" in card
    assert "This repo enforces the routing law" in card
    assert not any("The plan carries on" in m for _, m, _ in h.ctx["notifications"])
    # The re-recorded row is there (the rewrite DID land); the record names
    # the round and how it went.
    assert _approved_spec_rows(store)[-1]["rewritten_by_machine"]["round"] == 1
    error = store.get_run(CID)["error"] or ""
    assert error.startswith(f"stamp normalizer refused for {feature_id}")
    assert "after the machine's rewrite (round 1): the rewrite still could not be proven" in error
    # Nothing reached the plan side of the branch.
    assert _show(repo, f"planning/{CID}", _plan_yaml_rel(feature_id)).returncode != 0


# ---------------------------------------------------------------------------
# WHEN THE REWRITE IS REFUSED, THE MODEL GETS ITS TURN BEFORE THE RUN STOPS
# (rule 6b, 2026-09-07, after run 696a3e38)
#
# The live run: one title refused by rule, the machine's note sent, the
# specialist answered "could not carry out what this round required, after 2
# attempts. 'feedback_resolved' must be met…", and the run stopped with
# nothing built — the model fallback, which the second stamping exists to
# ask, was never asked. Now a rewrite the checker refused, or one that
# changed nothing, does not stop the run: the second stamping runs on the
# spec of record with the model allowed; if everything stamps the owner reads
# one line naming what the model decided and the plan carries on; if titles
# are still refused the run stops with the note-refused sentence and the
# model's own sentence after it. Never a third try.
# ---------------------------------------------------------------------------


def _switched_off_one_title() -> StampNormalizerOutcome:
    """The first stamping by rule only, ONE title refused — the shape of the
    live run 696a3e38, and the shape the spec's rule 6b line is written for
    ("1 of the worked examples … rewrite it … its verifier")."""
    return StampNormalizerOutcome(
        status="refused",
        detail=(
            "1 scenario(s) undecidable by rule (R1–R10) — the model fallback was "
            "not asked (switched off for this stamping); nothing was written"
        ),
        refused_titles=(_MOON_TITLE,),
        model_outcome={
            "status": "switched_off",
            "detail": (
                "the model fallback was not asked about 1 title(s) no rule could "
                "decide: switched off for this stamping by the caller (--no-model). "
                "The titles stay refused and nothing was stamped."
            ),
        },
    )


def _model_decided_outcome(pairs: dict[str, str]) -> StampNormalizerOutcome:
    """The second stamping with the model allowed: the rules refused the same
    titles and the model fallback decided every one of them — guardkit marks
    each with rule ``model`` (its provenance mark for a model-decided stamp,
    never an R-number) and reports ``decided`` with the count."""
    return StampNormalizerOutcome(
        status="written",
        detail=(
            f"{1 + len(pairs)} scenario(s) stamped ({len(pairs)} decided by the "
            "model fallback), 0 already stamped (untouched)"
        ),
        stamped={"ok": "hurl", **pairs},
        rules={"ok": "R9", **{title: "model" for title in pairs}},
        model_outcome={
            "status": "decided",
            "detail": f"the model decided {len(pairs)} title(s) no rule could decide",
            "count": len(pairs),
            "endpoint": "localhost:4000",
            "model": "workhorse",
        },
    )


def _still_refused_after_the_model(model_outcome: dict[str, Any]) -> StampNormalizerOutcome:
    """The second stamping with the model allowed, and the titles STILL
    refused: the model was asked and could not settle them, or was not there
    to ask — ``model_outcome`` says which."""
    return StampNormalizerOutcome(
        status="refused",
        detail=(
            "2 scenario(s) undecidable by rule (R1–R10) — the model fallback could "
            "not settle them; nothing was written"
        ),
        refused_titles=_UNDECIDABLE_TITLES,
        model_outcome=model_outcome,
    )


_MODEL_ASKED_AND_FAILED = {
    "status": "asked_and_failed",
    "detail": "HTTPStatusError: 502 Bad Gateway from localhost:4000",
    "endpoint": "localhost:4000",
    "model": "workhorse",
}

#: Rule 6b's line in the spec's own shape for one example, with the
#: fixture's real title and the real word the model decided.
_MODEL_LINE_AFTER_REFUSED_REWRITE = (
    "1 of the worked examples could not be proven as written; the spec writer "
    "could not rewrite it as what the endpoint does (the checker refused the "
    "rewrite twice), so the model fallback decided its verifier: "
    f"{_MOON_TITLE} → probe:process. The plan carries on."
)

#: The same shape for two examples after a rewrite that changed nothing.
_MODEL_LINE_AFTER_CHANGED_NOTHING = (
    "2 of the worked examples could not be proven as written; the spec writer "
    "could not rewrite them as what the endpoint does (the rewrite changed "
    "nothing), so the model fallback decided their verifiers: "
    f"{_MOON_TITLE} → probe:process; Another undecidable one → hurl. "
    "The plan carries on."
)

#: Rule 23's machine-variant sentence, verbatim: the first sentence of the
#: rule 6b stop after the checker refused the rewrite and the model could
#: not settle the titles.
_REFUSED_TWICE_SENTENCE = (
    f"Planning run {CID} stopped at writing the task plan: 2 of the worked "
    "examples could not be proven as written, and when the machine asked the "
    "spec writer to rewrite them as what the endpoint does, the checker "
    "refused the rewrite twice (the rewrite moved two examples the note did "
    "not name). Nothing was built. To try again, send the sentence as what "
    "the endpoint does: the method and path, the status code, and what is "
    "in the reply."
)

#: Rule 6's sentence at the top of the plan-stop card when the rewrite
#: changed nothing — the spec's own words for that outcome, which is also
#: how a changed-nothing rewrite stops after the model's turn (rule 6b):
#: the note-refused sentence would blame the checker for a refusal that
#: never was, and the spec quotes no other sentence for it.
_STOP_CHANGED_NOTHING = (
    "The machine already asked the spec writer once to rewrite these as what "
    "the endpoint does; the rewrite changed nothing."
)

#: The same worked example with its steps said as what the endpoint does and
#: the title untouched: a rewrite that moves the .feature but not one digest
#: sentence, so the cards say the same thing while the bytes differ.
_FIXTURE_FEATURE_ENDPOINT_STEPS = (
    "Feature: stats\n  Scenario: ok\n    When the caller sends GET /stats\n"
    "    Then the reply is 200\n"
)
_SPEC_FEATURE_REL = "features/stats-endpoint/stats-endpoint.feature"


def _steps_moved_spec_result(slug: str = "stats-endpoint") -> Any:
    """Round two of the spec: the .feature's steps rewritten into the
    endpoint shape, every digest sentence byte-identical to round one."""
    result = _spec_result_native(slug)
    result.role_output[f"{slug}.feature"] = _FIXTURE_FEATURE_ENDPOINT_STEPS
    return result


def _branch_subjects(repo: Path, branch: str) -> list[str]:
    """The commit subjects on ``branch``, newest first."""
    res = subprocess.run(
        ["git", "log", "--format=%s", branch], cwd=repo, capture_output=True, text=True
    )
    return [ln for ln in res.stdout.splitlines() if ln]


def _rev_parse(repo: Path, rev: str) -> str:
    res = subprocess.run(["git", "rev-parse", rev], cwd=repo, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


@pytest.mark.asyncio
async def test_a_rewrite_that_changed_nothing_gives_the_model_its_turn_and_carries_on(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """Rule 6b, the changed-nothing half: the spec writer came back with the
    same list, so the machine does NOT stop — it stamps again on the spec of
    record (the owner's approved row, never re-recorded) with the model
    fallback allowed; the model decides both titles; the owner reads ONE
    un-mentioned line naming the real titles and words, no card, and the
    build is queued. The receipt says the rewrite changed nothing and what
    the second stamping did."""
    repo, git = _enforced_repo(tmp_path)
    _queue(store)
    sink: dict[str, Any] = {}
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_native(),  # the same reply both rounds
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_sequenced_normalizer(
            sink,
            [
                _switched_off_outcome(),
                _model_decided_outcome(
                    {_MOON_TITLE: "probe:process", "Another undecidable one": "hurl"}
                ),
            ],
        ),
    )
    _share_order(sink, h)

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert h.ctx["counters"]["spec"] == 2  # the rewrite was asked for …
    assert h.ctx["counters"]["plan"] == 2  # … and the plan leg ran again anyway
    assert sink["order"] == ["normalize_stamps", "normalize_stamps", "validate"]
    assert sink["rules_only"] == [True, None]  # by rule only, then the model allowed
    assert (_MODEL_LINE_AFTER_CHANGED_NOTHING, False) in h.ctx["mentions"]
    assert _error_cards(h) == []
    assert not any("The machine already asked" in m for _, m, _ in h.ctx["notifications"])
    # The owner's approved row stands alone: a changed-nothing rewrite is
    # never re-recorded, and the spec of record is what was stamped — on the
    # branch too: a byte-identical rewrite is the runner's idempotent
    # no-commit path, so the branch never moved and nothing was put back.
    rows = _approved_spec_rows(store)
    assert len(rows) == 1 and "rewritten_by_machine" not in rows[0]
    assert sink["feature_seen"] == [_FIXTURE_FEATURE, _FIXTURE_FEATURE]
    assert not any("put back" in s for s in _branch_subjects(repo, f"planning/{CID}"))
    receipt = _leg_details(store, "feature-plan")["stamp_normalizer"]
    assert receipt["status"] == "written"
    assert receipt["rules_only"] is False
    rewrite = receipt["rewrite"]
    assert rewrite["changed_nothing"] is True
    assert rewrite["spec_of_record"] == {
        "sha": rows[0]["sha"],
        "branch_moved_by_rewrite": False,
    }
    assert "refused_by_checker" not in rewrite
    assert rewrite["note"] == _MACHINE_NOTE_FOR_TITLES
    assert rewrite["author"] == _MACHINE_NOTE_AUTHOR
    assert rewrite["refused_titles"] == [_MOON_TITLE, "Another undecidable one"]
    assert rewrite["owner_line"] == _MODEL_LINE_AFTER_CHANGED_NOTHING
    assert rewrite["owner_line_sent"] == "sent"
    assert rewrite["first_stamping"]["rules_only"] is True
    assert rewrite["second_stamping"] == {
        "status": "written",
        "rules_only": False,
        "model_decided": {_MOON_TITLE: "probe:process", "Another undecidable one": "hurl"},
        "model_outcome": {
            "status": "decided",
            "detail": "the model decided 2 title(s) no rule could decide",
            "count": 2,
            "endpoint": "localhost:4000",
            "model": "workhorse",
        },
    }
    assert h.ctx["counters"].get("build_trigger") == 1


@pytest.mark.asyncio
async def test_a_rewrite_that_changed_nothing_stops_with_the_card_and_its_changed_nothing_sentence_when_the_model_cannot_settle_it(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """Rule 6b, the changed-nothing half, the other way: the model had its
    turn and its answer was rejected, so the titles are still refused. The
    run stops with rule 6's card — the spec's own words for a rewrite that
    changed nothing — opening with the "changed nothing" sentence, the card
    about the SECOND stamping (the model was asked and could not settle
    them), the titles verbatim, and the model fallback's own sentence (rule
    16). Never the note-refused sentence: the checker refused nothing. Never
    a third try."""
    repo, git = _enforced_repo(tmp_path)
    _queue(store)
    sink: dict[str, Any] = {}
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_native(),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_sequenced_normalizer(
            sink,
            [
                _switched_off_outcome(),
                _still_refused_after_the_model(
                    {"status": "answer_rejected", "detail": "'maybe' is not one of the allowed words"}
                ),
            ],
        ),
    )
    _share_order(sink, h)

    assert await _drive_to_failure(h, store) == PlanningState.FAILED.value

    assert h.ctx["counters"]["spec"] == 2
    assert h.ctx["counters"]["plan"] == 2
    assert sink["order"] == ["normalize_stamps", "normalize_stamps"]
    assert sink["rules_only"] == [True, None]
    cards = _error_cards(h)
    assert len(cards) == 1
    card = cards[0]
    feature_id = h.ctx["counters"]["last_feature_id"]
    titles = "\n".join(f"  - {t}" for t in _UNDECIDABLE_TITLES)
    assert card.startswith(
        f"Planning run {CID} stopped at writing the task plan. {_STOP_CHANGED_NOTHING} "
        f"The verifier stamps could not all be minted by rule for feature {feature_id}. "
        "2 scenario(s) had no rule to decide which verifier proves them, and the model "
        "fallback could not settle them, so nothing was stamped and nothing was built:\n"
        f"{titles}\n"
        "The model fallback's answer was rejected: 'maybe' is not one of the allowed "
        "words.\n"
        "This repo enforces the routing law"
    )
    assert "refused the rewrite twice" not in card
    assert "switched off" not in card
    assert not any("The plan carries on" in m for _, m, _ in h.ctx["notifications"])
    assert len(_approved_spec_rows(store)) == 1
    error = store.get_run(CID)["error"] or ""
    assert error.startswith("stamp normalizer refused for FEAT-")
    assert "after the machine's rewrite (round 1): the rewrite changed nothing" in error
    assert "the branch never moved, so the spec of record stands" in error
    assert "the model fallback had its turn on the second stamping, on the spec of record" in error
    assert "titles still refused after the model's turn" in error


@pytest.mark.asyncio
async def test_a_rewrite_that_moved_the_steps_but_not_the_list_is_stamped_on_the_spec_of_record(
    store: SqlitePlanningRunStore, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Rule 6b says the model's turn after a changed-nothing rewrite is "on
    the spec of record" — and that has to be true of the BRANCH, not only of
    the row (coach finding, 2026-09-07). The spec writer rewrites the refused
    example's steps into the endpoint shape but keeps every digest sentence
    word for word: the cards say the same thing, yet the spec leg has already
    committed the rewrite, so the branch tip is the rewrite. The driver sees
    the sha moved, reads the approved files off the approved commit and puts
    them back on the branch as one commit; the second stamping and the plan
    sit on the approved bytes; the owner's line is true (the model decided
    the titles on the spec of record); the receipt names the commit set
    aside and the one that put the spec back; one approved row."""
    repo, git = _enforced_repo(tmp_path)
    _queue(store)
    sink: dict[str, Any] = {}
    branch = f"planning/{CID}"
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result_factory=_spec_by_round(_spec_result_native(), _steps_moved_spec_result()),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_sequenced_normalizer(
            sink,
            [
                _switched_off_outcome(),
                _model_decided_outcome(
                    {_MOON_TITLE: "probe:process", "Another undecidable one": "hurl"}
                ),
            ],
        ),
    )
    _share_order(sink, h)

    with caplog.at_level(logging.INFO, logger="forge.planning.driver"):
        await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert h.ctx["counters"]["spec"] == 2
    assert h.ctx["counters"]["plan"] == 2
    assert sink["order"] == ["normalize_stamps", "normalize_stamps", "validate"]
    assert sink["rules_only"] == [True, None]
    # BOTH stampings saw the approved .feature — never the rewrite's steps.
    assert sink["feature_seen"] == [_FIXTURE_FEATURE, _FIXTURE_FEATURE]
    # The branch carries the approved bytes under the plan commit ...
    assert _show(repo, branch, _SPEC_FEATURE_REL).stdout == _FIXTURE_FEATURE
    rows = _approved_spec_rows(store)
    assert len(rows) == 1 and "rewritten_by_machine" not in rows[0]
    receipt = _leg_details(store, "feature-plan")["stamp_normalizer"]
    rewrite = receipt["rewrite"]
    assert rewrite["changed_nothing"] is True
    spec_of_record = rewrite["spec_of_record"]
    assert spec_of_record["sha"] == rows[0]["sha"]
    assert spec_of_record["branch_moved_by_rewrite"] is True
    assert spec_of_record["rewrite_sha"] != rows[0]["sha"]
    assert "files_left_by_rewrite" not in spec_of_record
    # ... the put-back commit sits right under the plan commit, on top of the
    # rewrite's commit (set aside, still in the history, its steps readable
    # there and nowhere else), which sits on the approved commit.
    plan_sha = _leg_details(store, "feature-plan")["sha"]
    assert _rev_parse(repo, f"{plan_sha}^") == spec_of_record["restored_sha"]
    assert _rev_parse(repo, f"{spec_of_record['restored_sha']}^") == spec_of_record["rewrite_sha"]
    assert _rev_parse(repo, f"{spec_of_record['rewrite_sha']}^") == rows[0]["sha"]
    assert (
        _show(repo, spec_of_record["rewrite_sha"], _SPEC_FEATURE_REL).stdout
        == _FIXTURE_FEATURE_ENDPOINT_STEPS
    )
    assert _show(repo, spec_of_record["restored_sha"], _SPEC_FEATURE_REL).stdout == _FIXTURE_FEATURE
    assert (
        f"planning: the approved spec put back for {CID} (the machine's rewrite "
        f"changed nothing the owner reads; its commit {spec_of_record['rewrite_sha']} "
        "is set aside)"
    ) in _branch_subjects(repo, branch)
    # The owner's line is TRUE: the model decided both titles on the approved
    # spec; no card, no error.
    assert (_MODEL_LINE_AFTER_CHANGED_NOTHING, False) in h.ctx["mentions"]
    assert rewrite["owner_line"] == _MODEL_LINE_AFTER_CHANGED_NOTHING
    assert rewrite["second_stamping"]["model_decided"] == {
        _MOON_TITLE: "probe:process",
        "Another undecidable one": "hurl",
    }
    assert _error_cards(h) == []
    assert h.ctx["counters"].get("build_trigger") == 1
    # The machine log says what was put back, and where.
    log = "\n".join(_driver_log_lines(caplog))
    assert (
        f"had moved the planning branch to {spec_of_record['rewrite_sha']} without "
        "changing anything the owner reads; the approved spec "
        f"({rows[0]['sha']}) is put back as {spec_of_record['restored_sha']}, 4 file(s)"
    ) in log
    assert "the rewrite's commit" in log and "put back on the branch as" in log


class _ForgetfulGitRunner(WorktreeGitRunner):
    """A real runner that cannot read a file at a COMMIT id (only at a branch
    name) — the one thing the put-back needs from it."""

    async def read_file_from_branch(
        self, *, repo_path: str, branch: str, file_path: str
    ) -> str | None:
        if re.fullmatch(r"[0-9a-f]{40}", branch):
            return None
        return await super().read_file_from_branch(
            repo_path=repo_path, branch=branch, file_path=file_path
        )


@pytest.mark.asyncio
async def test_when_the_approved_spec_cannot_be_put_back_nothing_is_stamped_on_the_rewrites_bytes(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """The put-back's failing path: the branch moved (the rewrite's steps
    differ) but the approved files cannot be read at the approved commit.
    The run stops loudly at the plan stage, in plain words, BEFORE the second
    stamping: nothing is ever stamped or planned on bytes no approved row
    records. One approved row, no line about the model."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    _commit_repo_routing_law(repo, "enforced")
    git = _ForgetfulGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    sink: dict[str, Any] = {}
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result_factory=_spec_by_round(_spec_result_native(), _steps_moved_spec_result()),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_sequenced_normalizer(
            sink,
            [_switched_off_outcome(), _model_decided_outcome({_MOON_TITLE: "probe:process"})],
        ),
    )
    assert await _drive_to_failure(h, store) == PlanningState.FAILED.value
    assert h.ctx["counters"]["spec"] == 2
    assert h.ctx["counters"]["plan"] == 1  # no second attempt
    assert sink["calls"] == 1
    cards = _error_cards(h)
    assert len(cards) == 1
    assert cards[0].startswith(
        f"Planning run {CID} stopped at writing the task plan: the machine's rewrite "
        f"changed nothing on the list, but {_SPEC_FEATURE_REL} could not be read at "
        "the approved spec's commit "
    )
    assert cards[0].endswith(
        "so the approved spec could not be put back on the branch before stamping "
        "again; nothing was built"
    )
    assert not any("The plan carries on" in m for _, m, _ in h.ctx["notifications"])
    assert len(_approved_spec_rows(store)) == 1
    # The branch tip is still the rewrite's commit: nothing was planned on it.
    assert _show(repo, f"planning/{CID}", _SPEC_FEATURE_REL).stdout == _FIXTURE_FEATURE_ENDPOINT_STEPS
    assert _show(repo, f"planning/{CID}", _plan_yaml_rel(h.ctx["counters"]["last_feature_id"])).returncode != 0


@pytest.mark.asyncio
async def test_when_the_second_stamping_settles_the_titles_without_the_model_no_line_claims_it_did(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """The rule 6b line says what the MODEL decided. If the second stamping's
    own rules settle every refused title (an R-number on each — which the
    same bytes should never produce, but the line must be true if they do),
    the plan carries on and NO line about the model is sent; the receipt and
    the log say why."""
    repo, git = _enforced_repo(tmp_path)
    _queue(store)
    sink: dict[str, Any] = {}
    by_rule = StampNormalizerOutcome(
        status="written",
        detail="3 scenario(s) stamped by rule, 0 already stamped (untouched)",
        stamped={"ok": "hurl", _MOON_TITLE: "hurl", "Another undecidable one": "hurl"},
        rules={"ok": "R9", _MOON_TITLE: "R9", "Another undecidable one": "R9"},
    )
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_native(),  # the same reply both rounds
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_sequenced_normalizer(sink, [_switched_off_outcome(), by_rule]),
    )
    await h.driver.drive(CID)
    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert sink["rules_only"] == [True, None]
    assert not any("model fallback" in m for _, m, _ in h.ctx["notifications"])
    assert not any("The plan carries on" in m for _, m, _ in h.ctx["notifications"])
    rewrite = _leg_details(store, "feature-plan")["stamp_normalizer"]["rewrite"]
    assert rewrite["changed_nothing"] is True
    assert rewrite["owner_line"].startswith("no owner line: the model fallback decided none")
    assert rewrite["owner_line_sent"] == "no line to send"
    assert rewrite["second_stamping"]["model_decided"] == {}


@pytest.mark.asyncio
async def test_a_refused_rewrite_gives_the_model_its_turn_and_carries_on(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """Rule 6b, the case of run 696a3e38: one title refused by rule, the
    machine's note sent, the checker refused the rewrite twice. The run does
    NOT stop at the spec: the refusal comes back to the plan leg, the second
    stamping runs on the spec of record with the model allowed, the model
    decides the title, the owner reads the spec's line — verbatim in shape,
    the real title and the real word — and the build is queued. No card, no
    note-refused message, one approved row."""
    repo, git = _enforced_repo(tmp_path)
    _queue(store)
    sink: dict[str, Any] = {}
    refused_round = _error_result(
        reason="could not carry out what this round required, after 2 attempts. "
        "'feedback_resolved' must be met"
    )
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result_factory=_spec_by_round(_spec_result_native(), refused_round),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_sequenced_normalizer(
            sink,
            [_switched_off_one_title(), _model_decided_outcome({_MOON_TITLE: "probe:process"})],
        ),
    )
    _share_order(sink, h)

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert h.ctx["counters"]["spec"] == 2
    assert h.ctx["counters"]["spec_revisions"][0]["validate_feedback"] == (
        "These worked examples cannot be proven as written, because they describe "
        "the database or the code rather than what a caller sees:\n"
        f"- {_MOON_TITLE}\n"
        "\n"
        "Rewrite each of them as what can be proven, keeping the behaviour itself "
        "unchanged: a request to the endpoint and the reply it gets (the method and "
        "path, the status code, and what is in the body), or, for behaviour one "
        "request cannot show — two requests at once, timing — the repository test "
        "that proves it, named. Keep every other worked example exactly as it is."
    )
    assert h.ctx["counters"]["plan"] == 2
    assert sink["order"] == ["normalize_stamps", "normalize_stamps", "validate"]
    assert sink["rules_only"] == [True, None]
    assert (_MODEL_LINE_AFTER_REFUSED_REWRITE, False) in h.ctx["mentions"]
    assert _error_cards(h) == []
    assert not any("refused the rewrite twice (" in m for _, m, _ in h.ctx["notifications"])
    rows = _approved_spec_rows(store)
    assert len(rows) == 1 and "rewritten_by_machine" not in rows[0]
    receipt = _leg_details(store, "feature-plan")["stamp_normalizer"]
    assert receipt["status"] == "written" and receipt["rules_only"] is False
    rewrite = receipt["rewrite"]
    assert rewrite["refused_by_checker"] is True
    assert rewrite["checker_reason"] == (
        "could not carry out what this round required, after 2 attempts. "
        "'feedback_resolved' must be met"
    )
    assert "changed_nothing" not in rewrite
    assert rewrite["refused_titles"] == [_MOON_TITLE]
    assert rewrite["owner_line"] == _MODEL_LINE_AFTER_REFUSED_REWRITE
    assert rewrite["owner_line_sent"] == "sent"
    assert rewrite["first_stamping"] == {
        "status": "refused",
        "rules_only": True,
        "model_outcome": _switched_off_one_title().model_outcome,
    }
    assert rewrite["second_stamping"]["status"] == "written"
    assert rewrite["second_stamping"]["rules_only"] is False
    assert rewrite["second_stamping"]["model_decided"] == {_MOON_TITLE: "probe:process"}
    assert rewrite["second_stamping"]["model_outcome"]["status"] == "decided"
    assert h.ctx["counters"].get("build_trigger") == 1


@pytest.mark.asyncio
async def test_a_refused_rewrite_stops_with_both_sentences_when_the_model_cannot_settle_it(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """Rule 6b's stop after a checker refusal: the model had its turn on the
    second stamping and could not answer, so the titles are still refused.
    ONE message: rule 23's machine-variant sentence — how many examples,
    that the machine tried once, the checker's reason, that nothing was
    built, the sentence shape that works — then the model fallback's own
    sentence (rule 16), and nothing else (every sentence quoted in the
    spec). No card; the machine's note (a list of titles) is not quoted
    back; one approved row."""
    repo, git = _enforced_repo(tmp_path)
    _queue(store)
    sink: dict[str, Any] = {}
    refused_round = _error_result(
        reason="the rewrite moved two examples the note did not name"
    )
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result_factory=_spec_by_round(_spec_result_native(), refused_round),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_sequenced_normalizer(
            sink,
            [_switched_off_outcome(), _still_refused_after_the_model(dict(_MODEL_ASKED_AND_FAILED))],
        ),
    )
    _share_order(sink, h)

    assert await _drive_to_failure(h, store) == PlanningState.FAILED.value

    assert h.ctx["counters"]["spec"] == 2
    assert h.ctx["counters"]["plan"] == 2
    assert sink["order"] == ["normalize_stamps", "normalize_stamps"]
    assert sink["rules_only"] == [True, None]
    assert _error_cards(h) == [
        f"{_REFUSED_TWICE_SENTENCE} The model fallback was asked and could not "
        "answer: HTTPStatusError: 502 Bad Gateway from localhost:4000."
    ]
    assert not any("This repo enforces the routing law" in m for _, m, _ in h.ctx["notifications"])
    assert not any("Your note" in m for _, m, _ in h.ctx["notifications"])
    assert len(_approved_spec_rows(store)) == 1
    error = store.get_run(CID)["error"] or ""
    assert error.startswith("stamp normalizer refused for FEAT-")
    assert (
        "the rewrite was refused by the checker (007 dispatch error: the rewrite "
        "moved two examples the note did not name)"
    ) in error
    assert "the model fallback had its turn on the second stamping" in error
    assert "no fallback home" not in error


@pytest.mark.asyncio
async def test_a_refused_rewrite_whose_second_stamping_cannot_run_keeps_the_cannot_run_card(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """A normalizer that could not RUN on the model's turn is a different thing
    from titles still refused: today's cannot-run card, no note-refused
    sentence; the machine record names the round and how it went."""
    repo, git = _enforced_repo(tmp_path)
    _queue(store)
    sink: dict[str, Any] = {}
    refused_round = _error_result(reason="the rewrite moved two examples the note did not name")
    could_not_run = StampNormalizerOutcome(
        status="failed", detail="guardkit qa normalize-stamps timed out for the plan"
    )
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result_factory=_spec_by_round(_spec_result_native(), refused_round),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_sequenced_normalizer(sink, [_switched_off_outcome(), could_not_run]),
    )
    assert await _drive_to_failure(h, store) == PlanningState.FAILED.value
    assert sink["rules_only"] == [True, None]
    cards = _error_cards(h)
    assert len(cards) == 1
    assert "verifier-stamp normalizer could not run" in cards[0]
    assert "refused the rewrite twice" not in cards[0]
    assert "The machine already asked" not in cards[0]
    error = store.get_run(CID)["error"] or ""
    assert "stamp normalizer failed" in error
    assert (
        "the rewrite was refused by the checker (007 dispatch error: the rewrite "
        "moved two examples the note did not name)"
    ) in error
    assert "the model fallback had its turn on the second stamping" in error


@pytest.mark.asyncio
async def test_an_owner_note_refusal_keeps_todays_path_even_when_asked_not_to_fail(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """The keyword that hands a refusal back to the plan leg is honoured ONLY
    for the machine's note. An owner's note refused by the checker fails the
    run inside the spec leg with rule 23's owner sentence, byte for byte,
    whatever the keyword says — the owner's path is unchanged."""
    repo, git = _enforced_repo(tmp_path)
    _queue(store)
    # Put the run where the spec leg really runs (QUEUED → RUNNING →
    # FEATURE_SPEC), so the failure transition it takes is the real one.
    for state in (PlanningState.RUNNING, PlanningState.FEATURE_SPEC):
        assert not isinstance(
            store.transition(correlation_id=CID, to_state=state, actor_identity="planning-driver"),
            TransitionRefused,
        )
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_error_result(reason="the rewrite moved two examples the note did not name"),
    )
    draft = await h.driver._draft_spec(
        store.get_run(CID),
        CID,
        target_repo=TARGET_REPO,
        repo_path=str(repo),
        branch=f"planning/{CID}",
        plan_run_id=f"plan-{CID}",
        notes=["make the second example about what the endpoint answers"],
        note_from_machine=False,
        fail_on_refusal=False,
    )
    assert draft is None
    assert store.get_run(CID)["state"] == PlanningState.FAILED.value
    assert _error_cards(h) == [
        f"Planning run {CID} stopped at the spec: the spec writer could not honour "
        'your note "make the second example about what the endpoint answers" — the '
        "checker refused the rewrite twice (the rewrite moved two examples the note "
        "did not name). Nothing was built. To try again, send the sentence again "
        "with the note folded into it."
    ]
    error = store.get_run(CID)["error"] or ""
    assert error.startswith("007 dispatch error: the rewrite moved two examples")
    assert "the checker refused the revision round after the owner's note" in error


@pytest.mark.asyncio
async def test_a_restart_after_a_refused_rewrite_redoes_the_rewrite_and_the_models_turn(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """Rule 7 meets rule 6b: a refused rewrite writes no row, so a process
    that dies on the model's turn leaves the owner's row alone, and the
    re-drive redoes the whole round — the spec writer again (refused again),
    then the model's turn — and, the model deciding, carries on."""
    repo, git = _enforced_repo(tmp_path)
    _queue(store)
    sink: dict[str, Any] = {}
    refused_round = _error_result(reason="the rewrite moved two examples the note did not name")
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result_factory=_spec_by_round(_spec_result_native(), refused_round),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_sequenced_normalizer(sink, [_switched_off_one_title(), _Crash()]),
    )
    with pytest.raises(_Crash):
        await h.driver.drive(CID)
    assert store.get_run(CID)["state"] == PlanningState.FEATURE_PLAN.value
    assert h.ctx["counters"]["spec"] == 2
    assert len(_approved_spec_rows(store)) == 1

    sink2: dict[str, Any] = {}
    h2 = _make_driver(
        store,
        git_runner=WorktreeGitRunner(worktrees_root=tmp_path / "wt2"),
        repo_path=str(repo),
        spec_result_factory=_spec_by_round(_spec_result_native(), refused_round),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_sequenced_normalizer(
            sink2,
            [_switched_off_one_title(), _model_decided_outcome({_MOON_TITLE: "probe:process"})],
        ),
    )
    await h2.driver.drive(CID)
    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert h2.ctx["counters"]["spec"] == 1  # the rewrite, redone and refused again
    assert sink2["rules_only"] == [True, None]
    assert (_MODEL_LINE_AFTER_REFUSED_REWRITE, False) in h2.ctx["mentions"]
    assert len(_approved_spec_rows(store)) == 1


@pytest.mark.asyncio
async def test_the_switch_off_is_todays_path_byte_for_byte(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """Rule 8: ``planning.rewrite_on_refusal: false`` — the first refusal
    stops the run with today's card; the spec writer is never asked again."""
    repo, git = _enforced_repo(tmp_path)
    _queue(store)
    sink: dict[str, Any] = {}
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result_factory=_spec_by_round(_spec_result_native(), _rewritten_spec_result()),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_sequenced_normalizer(sink, [_refusal_outcome(), None]),
        rewrite_on_refusal=False,
    )
    _share_order(sink, h)

    assert await _drive_to_failure(h, store) == PlanningState.FAILED.value

    assert h.ctx["counters"]["spec"] == 1
    assert h.ctx["counters"]["plan"] == 1
    assert sink["order"] == ["normalize_stamps"]
    cards = _error_cards(h)
    assert len(cards) == 1
    feature_id = h.ctx["counters"]["last_feature_id"]
    assert cards[0].startswith(
        f"Planning run {CID} stopped at writing the task plan: the verifier "
        f"stamps could not all be minted by rule for feature {feature_id}. "
        "2 scenario(s) had no rule to decide which verifier proves them, and "
        "there is no fallback home, so nothing was stamped and nothing was built:\n"
    )
    assert "The machine already asked" not in cards[0]
    assert len(_approved_spec_rows(store)) == 1
    assert "rewritten_by_machine" not in _approved_spec_rows(store)[0]
    assert "after the machine's rewrite" not in (store.get_run(CID)["error"] or "")


@pytest.mark.asyncio
async def test_a_restart_after_the_row_was_written_does_not_rewrite_again(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """Rule 7: the process dies in the second stamping, AFTER the re-recorded
    row landed. The re-drive reads the row, finds the rewrite has happened,
    and does not ask the spec writer again: a refusal now stops with the card
    and the "still could not be proven" sentence."""
    repo, git = _enforced_repo(tmp_path)
    _queue(store)
    sink: dict[str, Any] = {}
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result_factory=_spec_by_round(_spec_result_native(), _rewritten_spec_result()),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_sequenced_normalizer(sink, [_refusal_outcome(), _Crash()]),
    )
    with pytest.raises(_Crash):
        await h.driver.drive(CID)
    assert store.get_run(CID)["state"] == PlanningState.FEATURE_PLAN.value
    assert _approved_spec_rows(store)[-1]["rewritten_by_machine"]["round"] == 1
    assert h.ctx["counters"]["spec"] == 2

    # A fresh process, the same durable store and repo.
    sink2: dict[str, Any] = {}
    h2 = _make_driver(
        store,
        git_runner=WorktreeGitRunner(worktrees_root=tmp_path / "wt2"),
        repo_path=str(repo),
        spec_result_factory=_spec_by_round(_spec_result_native(), _rewritten_spec_result()),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_sequenced_normalizer(sink2, [_refusal_outcome()]),
    )
    assert await _drive_to_failure(h2, store) == PlanningState.FAILED.value
    assert h2.ctx["counters"]["spec"] == 0  # never asked again
    assert h2.ctx["counters"]["plan"] == 1
    assert sink2["calls"] == 1
    cards = _error_cards(h2)
    assert len(cards) == 1
    assert f"stopped at writing the task plan. {_STOP_STILL_UNPROVEN}" in cards[0]
    assert len(_approved_spec_rows(store)) == 2  # no third approved row


@pytest.mark.asyncio
async def test_a_restart_before_the_row_was_written_redoes_the_rewrite(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """Rule 7's other half: the process dies during the rewrite's spec dispatch
    — no re-recorded row — so the re-drive redoes the rewrite (the spec writer
    runs again) and, stamping clean, carries on."""
    repo, git = _enforced_repo(tmp_path)
    _queue(store)
    sink: dict[str, Any] = {}

    async def _dying_spec_dispatch(*, validate_feedback: str | None = None, **_: Any) -> Any:
        if validate_feedback is not None:
            raise _Crash()
        return _spec_result_native()

    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_dispatch=_dying_spec_dispatch,
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_sequenced_normalizer(sink, [_refusal_outcome()]),
    )
    with pytest.raises(_Crash):
        await h.driver.drive(CID)
    assert store.get_run(CID)["state"] == PlanningState.FEATURE_PLAN.value
    assert len(_approved_spec_rows(store)) == 1
    assert "rewritten_by_machine" not in _approved_spec_rows(store)[0]

    sink2: dict[str, Any] = {}
    h2 = _make_driver(
        store,
        git_runner=WorktreeGitRunner(worktrees_root=tmp_path / "wt2"),
        repo_path=str(repo),
        spec_result_factory=_spec_by_round(_spec_result_native(), _rewritten_spec_result()),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_sequenced_normalizer(sink2, [_refusal_outcome(), None]),
    )
    await h2.driver.drive(CID)
    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert h2.ctx["counters"]["spec"] == 1  # the rewrite, redone
    assert h2.ctx["counters"]["spec_revisions"][0]["validate_feedback"] == _MACHINE_NOTE_FOR_TITLES
    assert _approved_spec_rows(store)[-1]["rewritten_by_machine"]["round"] == 1
    assert (_MACHINE_REWRITE_LINE_EXPECTED, False) in h2.ctx["mentions"]


@pytest.mark.asyncio
async def test_the_rewrite_fires_only_on_a_refusal_of_examples_where_the_law_is_enforced(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """Rule 1's fence, the two cases that must NOT rewrite: a normalizer that
    could not RUN (``failed``) under the enforced law stops with today's
    cannot-run card and never asks the spec writer; and a refusal in a repo
    that does not enforce the law proceeds with its one plain line, as today.
    """
    # (a) failed + enforced → today's cannot-run card, no rewrite.
    repo, git = _enforced_repo(tmp_path)
    _queue(store)
    sink: dict[str, Any] = {}
    failed = StampNormalizerOutcome(
        status="failed", detail="guardkit qa normalize-stamps timed out for the plan"
    )
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result_factory=_spec_by_round(_spec_result_native(), _rewritten_spec_result()),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_sequenced_normalizer(sink, [failed]),
    )
    assert await _drive_to_failure(h, store) == PlanningState.FAILED.value
    assert h.ctx["counters"]["spec"] == 1
    cards = _error_cards(h)
    assert len(cards) == 1
    assert "verifier-stamp normalizer could not run" in cards[0]
    assert "The machine already asked" not in cards[0]

    # (b) refusal + NOT enforced → proceeds with the one plain line, no rewrite.
    cx = sqlite_connect.connect_writer(tmp_path / "unenforced.db")
    migrations.apply_at_boot(cx)
    store2 = SqlitePlanningRunStore(cx, target_terminal_enabled=True)
    repo2 = tmp_path / "api_test_unenforced"
    _init_scratch_repo(repo2)
    _queue(store2)
    sink2: dict[str, Any] = {}
    h2 = _make_driver(
        store2,
        git_runner=WorktreeGitRunner(worktrees_root=tmp_path / "wt-unenforced"),
        repo_path=str(repo2),
        spec_result_factory=_spec_by_round(_spec_result_native(), _rewritten_spec_result()),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_sequenced_normalizer(sink2, [_partial_outcome()]),
    )
    await h2.driver.drive(CID)
    assert store2.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert h2.ctx["counters"]["spec"] == 1
    assert (_UNENFORCED_LINE, False) in h2.ctx["mentions"]
    assert not any("the machine asked the spec writer" in m for _, m, _ in h2.ctx["notifications"])
    receipt = _leg_details(store2, "feature-plan")["stamp_normalizer"]
    assert "rewrite" not in receipt and receipt["proceeded_unenforced"] is True


# ---------------------------------------------------------------------------
# The model fallback reports its own outcome and the card prints it (rule 16)
# ---------------------------------------------------------------------------


def _refusal_with_model(model_outcome: dict[str, Any] | None) -> StampNormalizerOutcome:
    return StampNormalizerOutcome(
        status="refused",
        detail="2 scenario(s) undecidable by rule (R1–R10) — no fallback home; nothing was written",
        refused_titles=_UNDECIDABLE_TITLES,
        enforcement="enforced",
        model_outcome=model_outcome,
    )


@pytest.mark.parametrize(
    ("model_outcome", "sentence", "clause"),
    [
        (
            {"status": "asked_and_failed", "detail": "ConnectError: connection refused at localhost:4000."},
            "The model fallback was asked and could not answer: ConnectError: connection refused at localhost:4000.",
            "the model fallback could not settle them",
        ),
        (
            {"status": "answer_rejected", "detail": "'maybe' is not one of the allowed words"},
            "The model fallback's answer was rejected: 'maybe' is not one of the allowed words.",
            "the model fallback could not settle them",
        ),
        (
            {"status": "not_configured", "detail": "no model endpoint is configured"},
            "The model fallback was not asked: no endpoint is configured.",
            "there is no fallback home",
        ),
        (
            {"status": "decided", "detail": "the model decided 3 title(s) no rule could decide", "count": 3},
            "The model fallback decided 3 of them; these could not be decided.",
            "the model fallback could not settle them",
        ),
        (
            {"status": "decided", "detail": "the model decided 1 title(s) no rule could decide"},
            "The model fallback decided 1 of them; these could not be decided.",
            "the model fallback could not settle them",
        ),
    ],
)
def test_the_card_prints_what_the_model_fallback_said_about_itself(
    model_outcome: dict[str, Any], sentence: str, clause: str
) -> None:
    card = _Driver._stamp_normalizer_card(CID, "FEAT-1234", _refusal_with_model(model_outcome))
    titles = "\n".join(f"  - {t}" for t in _UNDECIDABLE_TITLES)
    # One sentence, right after the titles, before "This repo enforces".
    assert f"{titles}\n{sentence}\nThis repo enforces the routing law" in card
    assert f"and {clause}, so nothing was stamped" in card
    # "no fallback home" is said only when the model was NOT asked.
    assert ("no fallback home" in card) == (model_outcome["status"] == "not_configured")


def test_the_card_says_nothing_about_the_model_when_the_normalizer_said_nothing() -> None:
    """An older guardkit that reports no ``model_outcome``: today's card,
    word for word — nothing invented."""
    card = _Driver._stamp_normalizer_card(CID, "FEAT-1234", _refusal_with_model(None))
    assert "model fallback" not in card
    assert "there is no fallback home, so nothing was stamped and nothing was built" in card
    titles = "\n".join(f"  - {t}" for t in _UNDECIDABLE_TITLES)
    assert f"{titles}\nThis repo enforces the routing law" in card


@pytest.mark.asyncio
async def test_the_drivers_own_record_stops_saying_no_fallback_home_when_the_model_was_asked(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """Rule 16's last clause, end to end: the normalizer's outcome says the
    model was asked and could not answer; the run's error (the machine
    record) and the card both say so, and neither says "no fallback home"."""
    repo, git = _enforced_repo(tmp_path)
    _queue(store)
    sink: dict[str, Any] = {}
    outcome = StampNormalizerOutcome(
        status="refused",
        detail="2 scenario(s) undecidable by rule (R1–R10) — no fallback home; nothing was written",
        refused_titles=_UNDECIDABLE_TITLES,
        model_outcome={
            "status": "asked_and_failed",
            "detail": "HTTPStatusError: 502 Bad Gateway from localhost:4000",
            "endpoint": "localhost:4000",
            "model": "workhorse",
        },
    )
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_native(),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_sequenced_normalizer(sink, [outcome]),
        rewrite_on_refusal=False,  # today's stop, so the card is the plain one
    )
    assert await _drive_to_failure(h, store) == PlanningState.FAILED.value
    error = store.get_run(CID)["error"] or ""
    assert "no fallback home" not in error
    assert "the model fallback was asked and could not settle them" in error
    card = _error_cards(h)[0]
    assert "no fallback home" not in card
    assert (
        "The model fallback was asked and could not answer: HTTPStatusError: 502 "
        "Bad Gateway from localhost:4000." in card
    )


# ---------------------------------------------------------------------------
# THE REWRITE COMES BEFORE THE MODEL FALLBACK (rule 1a, 2026-09-07)
#
# The first live run of the rewrite round never fired: the model fallback
# (reachable through LiteLLM since that morning) answered for the schema
# examples and "proved" them with a check that never touches the schema, so
# there was no refusal for the round to hear. Now the FIRST stamping of a run
# goes by rule only — the collaborator is called with rules_only=True and the
# argv carries --no-model — exactly when the round could fire: the switch is
# on, the law is enforced for the repo, and this run has not had its one
# rewrite. The second stamping, after the rewrite, asks the model about what
# is still refused. Switch off, law not enforced, or rewrite already spent:
# the model is asked on the first stamping as before.
# ---------------------------------------------------------------------------

_RULES_FIRST_LOG = "first stamping by rule only (the rewrite round is on)"

_SWITCHED_OFF_MODEL_OUTCOME = {
    "status": "switched_off",
    "detail": (
        "the model fallback was not asked about 2 title(s) no rule could decide: "
        "switched off for this stamping by the caller (--no-model). The titles "
        "stay refused and nothing was stamped."
    ),
}


def _switched_off_outcome() -> StampNormalizerOutcome:
    """What the guardkit half answers to ``--no-model`` when the rules refuse:
    the same refusal, and the model outcome says the caller switched it off."""
    return StampNormalizerOutcome(
        status="refused",
        detail=(
            "2 scenario(s) undecidable by rule (R1–R10) — the model fallback was "
            "not asked (switched off for this stamping); nothing was written"
        ),
        refused_titles=_UNDECIDABLE_TITLES,
        model_outcome=dict(_SWITCHED_OFF_MODEL_OUTCOME),
    )


def _not_configured_outcome() -> StampNormalizerOutcome:
    """A stamping that COULD ask the model and found no endpoint."""
    return StampNormalizerOutcome(
        status="refused",
        detail="2 scenario(s) undecidable by rule (R1–R10) — no fallback home; nothing was written",
        refused_titles=_UNDECIDABLE_TITLES,
        model_outcome={"status": "not_configured", "detail": "no model endpoint is configured"},
    )


def _driver_log_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "forge.planning.driver"]


@pytest.mark.asyncio
async def test_the_first_stamping_runs_by_rule_only_and_the_second_asks_the_model(
    store: SqlitePlanningRunStore, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Rule 1a end to end in an enforced repo: the first stamping is asked for
    by rule only (the machine log names the mode), it refuses with the model
    switched off, the rewrite fires, the second stamping is called the old
    way (the model allowed) and stamps clean. The plan receipt says which
    stamping ran by rule only; nothing on any surface says "no endpoint is
    configured"; the durable spec row keeps rule 4's fields and no more."""
    repo, git = _enforced_repo(tmp_path)
    _queue(store)
    sink: dict[str, Any] = {}
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result_factory=_spec_by_round(_spec_result_native(), _rewritten_spec_result()),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_sequenced_normalizer(sink, [_switched_off_outcome(), None]),
    )
    with caplog.at_level(logging.INFO, logger="forge.planning.driver"):
        await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert sink["rules_only"] == [True, None]
    lines = _driver_log_lines(caplog)
    assert any(_RULES_FIRST_LOG in m for m in lines)
    assert any("this time with the model fallback allowed" in m for m in lines)
    receipt = _leg_details(store, "feature-plan")["stamp_normalizer"]
    assert receipt["status"] == "written"
    assert receipt["rules_only"] is False  # the second stamping, committed with the plan
    assert "rules_only_note" not in receipt
    assert receipt["rewrite"]["first_stamping"] == {
        "status": "refused",
        "rules_only": True,
        "model_outcome": _SWITCHED_OFF_MODEL_OUTCOME,
    }
    assert _error_cards(h) == []
    assert not any("no endpoint is configured" in m for _, m, _ in h.ctx["notifications"])
    assert set(_approved_spec_rows(store)[-1]["rewritten_by_machine"]) == {
        "round", "author", "note", "refused_titles", "changes",
    }


@pytest.mark.asyncio
async def test_with_the_switch_off_the_first_stamping_asks_the_model_as_before(
    store: SqlitePlanningRunStore, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``planning.rewrite_on_refusal: false``: the round cannot fire, so the
    first stamping is called the old way — no keyword, no rule-only line."""
    repo, git = _enforced_repo(tmp_path)
    _queue(store)
    sink: dict[str, Any] = {}
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result_factory=_spec_by_round(_spec_result_native(), _rewritten_spec_result()),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_sequenced_normalizer(sink, [_refusal_outcome(), None]),
        rewrite_on_refusal=False,
    )
    with caplog.at_level(logging.INFO, logger="forge.planning.driver"):
        assert await _drive_to_failure(h, store) == PlanningState.FAILED.value
    assert sink["rules_only"] == [None]
    assert not any(_RULES_FIRST_LOG in m for m in _driver_log_lines(caplog))


@pytest.mark.asyncio
async def test_an_unenforced_repo_stamps_as_before_even_with_the_switch_on(
    store: SqlitePlanningRunStore, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The law is not enforced for the repo: the round never fires there, so
    the stamping is called the old way, the log says why, and the receipt
    says the stamping was not by rule only (with no note — nothing was
    refused it)."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    sink: dict[str, Any] = {}
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_native(),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_sequenced_normalizer(sink, [_partial_outcome()]),
    )
    with caplog.at_level(logging.INFO, logger="forge.planning.driver"):
        await h.driver.drive(CID)
    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert sink["rules_only"] == [None]
    lines = _driver_log_lines(caplog)
    assert not any(_RULES_FIRST_LOG in m for m in lines)
    assert any("the rewrite round does not fire in an unenforced repo" in m for m in lines)
    receipt = _leg_details(store, "feature-plan")["stamp_normalizer"]
    assert receipt["rules_only"] is False
    assert "rules_only_note" not in receipt
    assert receipt["proceeded_unenforced"] is True


@pytest.mark.asyncio
async def test_after_the_one_rewrite_a_re_drive_stamps_with_the_model_allowed(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """Rule 7 meets rule 1a: the re-drive reads the durable row, finds the
    rewrite has happened, and its stamping — the only one this drive does —
    is called the old way, because the round can no longer fire."""
    repo, git = _enforced_repo(tmp_path)
    _queue(store)
    sink: dict[str, Any] = {}
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result_factory=_spec_by_round(_spec_result_native(), _rewritten_spec_result()),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_sequenced_normalizer(sink, [_switched_off_outcome(), _Crash()]),
    )
    with pytest.raises(_Crash):
        await h.driver.drive(CID)
    assert sink["rules_only"] == [True, None]
    assert _approved_spec_rows(store)[-1]["rewritten_by_machine"]["round"] == 1

    sink2: dict[str, Any] = {}
    h2 = _make_driver(
        store,
        git_runner=WorktreeGitRunner(worktrees_root=tmp_path / "wt2"),
        repo_path=str(repo),
        spec_result_factory=_spec_by_round(_spec_result_native(), _rewritten_spec_result()),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_sequenced_normalizer(sink2, [_not_configured_outcome()]),
    )
    assert await _drive_to_failure(h2, store) == PlanningState.FAILED.value
    assert sink2["rules_only"] == [None]
    assert h2.ctx["counters"]["spec"] == 0
    card = _error_cards(h2)[0]
    assert _STOP_STILL_UNPROVEN in card
    assert "The model fallback was not asked: no endpoint is configured." in card


@pytest.mark.asyncio
async def test_an_older_collaborator_without_the_keyword_still_works_and_the_receipt_says_so(
    store: SqlitePlanningRunStore, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A normalize_stamps collaborator in its pre-1a shape (positional only):
    the driver calls it the old way instead of failing on the keyword, warns,
    and the receipt says the first stamping was not by rule only and why. The
    run itself goes exactly as the rewrite round always did."""
    repo, git = _enforced_repo(tmp_path)
    _queue(store)
    sink: dict[str, Any] = {}
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result_factory=_spec_by_round(_spec_result_native(), _rewritten_spec_result()),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_sequenced_normalizer(sink, [_refusal_outcome(), None], legacy=True),
    )
    with caplog.at_level(logging.WARNING, logger="forge.planning.driver"):
        await h.driver.drive(CID)
    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert sink["rules_only"] == [None, None]
    receipt = _leg_details(store, "feature-plan")["stamp_normalizer"]
    first = receipt["rewrite"]["first_stamping"]
    assert first["status"] == "refused" and first["rules_only"] is False
    assert "does not take the rules_only keyword" in first["rules_only_note"]
    assert any("does not take the rules_only keyword" in m for m in _driver_log_lines(caplog))
    assert receipt["rules_only"] is False and "rules_only_note" not in receipt


@pytest.mark.asyncio
async def test_after_a_rewrite_the_card_reports_the_second_stampings_model_outcome(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """The first stamping (model switched off) set off the rewrite; the second
    could ask the model and found no endpoint. The card is about the second:
    the "no endpoint is configured" sentence appears once, for that one, never
    for the switched-off first."""
    repo, git = _enforced_repo(tmp_path)
    _queue(store)
    sink: dict[str, Any] = {}
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result_factory=_spec_by_round(_spec_result_native(), _rewritten_spec_result()),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_sequenced_normalizer(
            sink, [_switched_off_outcome(), _not_configured_outcome()]
        ),
    )
    assert await _drive_to_failure(h, store) == PlanningState.FAILED.value
    assert sink["rules_only"] == [True, None]
    card = _error_cards(h)[0]
    assert _STOP_STILL_UNPROVEN in card
    assert card.count("The model fallback was not asked: no endpoint is configured.") == 1


@pytest.mark.asyncio
async def test_after_a_changed_nothing_rewrite_the_stop_reports_the_models_turn_never_the_switched_off_first_stamping(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """Rule 6b meets rule 1a: the first stamping ran by rule only (the machine
    switched the model off), the rewrite changed nothing, so the model got
    its turn on a SECOND stamping — which found no endpoint. The card (rule
    6's, with its "changed nothing" sentence) is about that second stamping:
    "no endpoint is configured" once, true this time, and never a word about
    the model being switched off (that was the first stamping, the machine's
    own doing, and not what stopped the run)."""
    repo, git = _enforced_repo(tmp_path)
    _queue(store)
    sink: dict[str, Any] = {}
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_native(),  # the same reply both rounds
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_sequenced_normalizer(
            sink, [_switched_off_outcome(), _not_configured_outcome()]
        ),
    )
    assert await _drive_to_failure(h, store) == PlanningState.FAILED.value
    assert sink["rules_only"] == [True, None]
    message = _error_cards(h)[0]
    assert message.startswith(
        f"Planning run {CID} stopped at writing the task plan. {_STOP_CHANGED_NOTHING} "
        "The verifier stamps could not all be minted by rule for feature "
    )
    assert message.count("The model fallback was not asked: no endpoint is configured.") == 1
    assert "switched off" not in message
    assert "refused the rewrite twice" not in message
    assert "This repo enforces the routing law" in message
    error = store.get_run(CID)["error"] or ""
    assert "the rewrite changed nothing" in error
    assert "switched off for this stamping" not in error


def test_the_card_says_the_machine_switched_the_model_off_never_that_it_was_missing() -> None:
    """The card built straight from a switched-off outcome (no rewrite named):
    the clause says the model was switched off for this stamping — never
    "no endpoint is configured" and never "there is no fallback home", both
    of which would tell the owner the model was missing when the machine
    chose not to ask it — and no second sentence about the model follows
    the titles."""
    card = _Driver._stamp_normalizer_card(
        CID, "FEAT-1234", _refusal_with_model(dict(_SWITCHED_OFF_MODEL_OUTCOME))
    )
    assert "no endpoint is configured" not in card
    assert "no fallback home" not in card
    assert (
        "and the model fallback was not asked (it was switched off for this "
        "stamping), so nothing was stamped and nothing was built"
    ) in card
    assert card.count("model fallback") == 1
    titles = "\n".join(f"  - {t}" for t in _UNDECIDABLE_TITLES)
    assert f"{titles}\nThis repo enforces the routing law" in card


def test_the_model_decided_pairs_are_the_titles_marked_model_and_nothing_is_guessed() -> None:
    """Rule 6b's line names what the MODEL decided: the titles the normalizer
    marks with rule ``model``. When it marked none (an older guardkit, or a
    stamped map the seam's clipped tail lost), the refused titles with what
    was written for them — or "not named", never a guessed word. A title an
    R-number decided, or one found already stamped, is never put in the
    model's mouth: it is left out, whatever else the map says."""
    marked = _model_decided_outcome({_MOON_TITLE: "probe:process"})
    assert _Driver._model_decided_pairs(marked, [_MOON_TITLE]) == [
        (_MOON_TITLE, "probe:process")
    ]
    unmarked = StampNormalizerOutcome(
        status="written", stamped={"ok": "hurl", _MOON_TITLE: "exam"}, rules={"ok": "R9"}
    )
    assert _Driver._model_decided_pairs(unmarked, [_MOON_TITLE]) == [(_MOON_TITLE, "exam")]
    lost = StampNormalizerOutcome(status="written", already_stamped=("Another undecidable one",))
    assert _Driver._model_decided_pairs(lost, [_MOON_TITLE, "Another undecidable one"]) == [
        (_MOON_TITLE, "a verifier the normalizer did not name"),
    ]
    # A rule decided it on THIS stamping (an R-number in the rules map): not
    # the model's, so not on the model's line — even beside a title the map
    # does not mark at all.
    by_rule = StampNormalizerOutcome(
        status="written",
        stamped={"ok": "hurl", _MOON_TITLE: "hurl", "Another undecidable one": "exam"},
        rules={"ok": "R9", _MOON_TITLE: "R9"},
    )
    assert _Driver._model_decided_pairs(by_rule, [_MOON_TITLE, "Another undecidable one"]) == [
        ("Another undecidable one", "exam"),
    ]
    all_by_rule = StampNormalizerOutcome(
        status="written",
        stamped={"ok": "hurl", _MOON_TITLE: "hurl", "Another undecidable one": "hurl"},
        rules={"ok": "R9", _MOON_TITLE: "R9", "Another undecidable one": "R3"},
    )
    assert _Driver._model_decided_pairs(all_by_rule, list(_UNDECIDABLE_TITLES)) == []


def test_the_rule_6b_line_keeps_the_specs_shape_for_one_example_and_bends_for_more() -> None:
    """The spec quotes the line for one example; that shape lands verbatim.
    For more than one, "it" and "its verifier" bend and the pairs are joined
    with "; " — no rule ids on the face either way."""
    one = _Driver._machine_rewrite_model_line(
        1, why="the checker refused the rewrite twice", decided=[(_MOON_TITLE, "probe:process")]
    )
    assert one == _MODEL_LINE_AFTER_REFUSED_REWRITE
    two = _Driver._machine_rewrite_model_line(
        2,
        why="the rewrite changed nothing",
        decided=[(_MOON_TITLE, "probe:process"), ("Another undecidable one", "hurl")],
    )
    assert two == _MODEL_LINE_AFTER_CHANGED_NOTHING
    assert not re.search(r"\bR\d+\b", one) and not re.search(r"\bR\d+\b", two)


# ---------------------------------------------------------------------------
# THE PROVABILITY CHECK BEFORE THE CARD MEETS THE PLAN STAGE (Part K, rule 47:
# "the plan stage's rule 1a path runs as today"). The machine's one note was
# sent before the card, so the plan stage never sends it again: its first
# stamping runs with the model fallback allowed — as the card told the owner
# it would — and a title still refused stops with the plan-stop card and rule
# 6's sentence for how the one rewrite went. Never a third try.
# ---------------------------------------------------------------------------

from forge.planning.target_terminal_tools import ScenarioProvabilityOutcome  # noqa: E402


def _classify_by_call(*refusals: list[str]):
    """A fake ``classify_scenarios`` seam answering, per call, which of the
    fixture's titles the rules refuse (the last answer repeats)."""
    calls: list[str] = []

    async def _classify(repo_path: Path, feature_text: str) -> ScenarioProvabilityOutcome:
        calls.append(feature_text)
        refused = refusals[min(len(calls) - 1, len(refusals) - 1)]
        return ScenarioProvabilityOutcome(
            status="checked",
            detail=f"{len(refused)} of 1 scenario(s) cannot be proven by rule",
            refused_titles=tuple(refused),
            homes={} if refused else {"ok": "hurl"},
            rules={} if refused else {"ok": "R9"},
            scenario_count=1,
            repo_has_http_surface=True,
        )

    _classify.calls = calls  # type: ignore[attr-defined]
    return _classify


def _refusal_of_ok() -> StampNormalizerOutcome:
    """The plan stage's normalizer refusing the fixture's one example after
    the model had its turn (the model could not settle it either)."""
    return StampNormalizerOutcome(
        status="refused",
        detail="1 scenario(s) undecidable by rule (R1–R10) — the model fallback was asked and could not settle them; nothing was written",
        refused_titles=("ok",),
        model_outcome={"status": "answer_rejected", "detail": "the answer named no verifier"},
    )


_STOP_REFUSED_BY_CHECKER = (
    "The machine already asked the spec writer once to rewrite these as what "
    "the endpoint does; the rewrite was refused by the checker: could not carry "
    "out what this round required, after 2 attempts. 'feedback_resolved' must "
    "be met."
)


@pytest.mark.asyncio
async def test_after_a_pre_card_round_the_plan_stage_asks_the_model_and_never_sends_the_note_again(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """The pre-card round rewrote the example and the check still refused it;
    the card said so and the owner approved. At the plan stage the FIRST
    stamping runs with the model allowed (never by rule only: the round is
    spent), and when it refuses the title the run stops with the plan-stop
    card and rule 6's "still could not be proven" sentence — the spec writer
    ran exactly twice in the whole run."""
    repo, git = _enforced_repo(tmp_path)
    _queue(store)
    sink: dict[str, Any] = {}
    classify = _classify_by_call(["ok"], ["ok"])
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result_factory=_spec_by_round(_spec_result_native(), _rewritten_spec_result()),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_sequenced_normalizer(sink, [_refusal_of_ok()]),
        classify_fn=classify,
    )
    _share_order(sink, h)

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.FAILED.value
    assert h.ctx["counters"]["spec"] == 2  # the pre-card round, never a third try
    assert len(h.ctx["counters"]["spec_revisions"]) == 1
    assert classify.calls == [_FIXTURE_FEATURE, _FIXTURE_FEATURE]
    # The one card a person read carried rule 45's cannot-be-proven line.
    card = h.ctx["publisher"].envelopes[0].payload["details"]["summary"]
    assert card["what_happened"].endswith(
        "1 of the worked examples cannot be proven as written: “ok”. If you "
        "approve, the plan stage will ask the model fallback to place them; or "
        "send a note."
    )
    assert "The machine rewrote 1 of the worked examples" in card["what_happened"]
    # The plan stage stamped ONCE, called the old way (the model allowed —
    # never by rule only: the round is spent), and stopped.
    assert sink["rules_only"] == [None]
    assert sink["order"] == ["normalize_stamps"]
    assert h.ctx["counters"]["plan"] == 1
    cards = _error_cards(h)
    assert len(cards) == 1
    assert _STOP_STILL_UNPROVEN in cards[0]
    assert "ok" in cards[0]
    assert "The model fallback's answer was rejected" in cards[0]
    # The approved spec row carries the pre-card round; no plan-stage rewrite.
    approved = _approved_spec_rows(store)
    assert len(approved) == 1
    assert approved[0]["provability"]["round"] == 1
    assert approved[0]["provability"]["rewritten"] is True
    assert "rewritten_by_machine" not in approved[0]


@pytest.mark.asyncio
async def test_after_a_checker_refused_pre_card_round_the_plan_stage_asks_the_model(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """The checker refused the machine's rewrite before the card; the card
    opened on the original draft and the owner approved. The plan stage's
    first stamping asks the model; when it settles the title the plan carries
    on to the build with no second note — and when it cannot, the stop card
    says the rewrite was refused by the checker, with the reason."""
    reason = (
        "could not carry out what this round required, after 2 attempts. "
        "'feedback_resolved' must be met"
    )

    # Carried on: the model settles it.
    repo, git = _enforced_repo(tmp_path / "a")
    _queue(store)
    sink: dict[str, Any] = {}
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result_factory=_spec_by_round(_spec_result_native(), _error_result(reason=reason)),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_sequenced_normalizer(sink, [None]),
        classify_fn=_classify_by_call(["ok"]),
    )
    _share_order(sink, h)
    await h.driver.drive(CID)
    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert h.ctx["counters"]["spec"] == 2
    assert sink["rules_only"] == [None]  # called the old way: the model allowed
    assert sink["order"] == ["normalize_stamps", "validate"]
    assert _error_cards(h) == []
    approved = _approved_spec_rows(store)
    assert approved[0]["provability"]["refused_by_checker"] is True
    assert approved[0]["provability"]["rewritten"] is False

    # Stopped: the model could not settle it either.
    cx = sqlite_connect.connect_writer(tmp_path / "b.db")
    migrations.apply_at_boot(cx)
    store_b = SqlitePlanningRunStore(cx, target_terminal_enabled=True)
    repo_b, git_b = _enforced_repo(tmp_path / "b")
    _queue(store_b)
    sink_b: dict[str, Any] = {}
    h_b = _make_driver(
        store_b,
        git_runner=git_b,
        repo_path=str(repo_b),
        spec_result_factory=_spec_by_round(_spec_result_native(), _error_result(reason=reason)),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_sequenced_normalizer(sink_b, [_refusal_of_ok()]),
        classify_fn=_classify_by_call(["ok"]),
    )
    _share_order(sink_b, h_b)
    await h_b.driver.drive(CID)
    assert store_b.get_run(CID)["state"] == PlanningState.FAILED.value
    assert h_b.ctx["counters"]["spec"] == 2
    assert sink_b["rules_only"] == [None]  # the model allowed
    cards = _error_cards(h_b)
    assert len(cards) == 1
    assert _STOP_REFUSED_BY_CHECKER in cards[0]

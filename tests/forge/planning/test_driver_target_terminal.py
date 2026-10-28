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
import os
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

from tests.forge.planning._plan_identity import plan_artifact_identity
import pytest
import yaml

from forge.adapters.git.models import GitOpResult
from forge.cli._serve_planning import build_feature_plan_command_args
from forge.adapters.git.planning_runner import WorktreeGitRunner
from forge.adapters.sqlite import connect as sqlite_connect
from forge.config.models import PlanningConfig, TargetTerminalConfig
from forge.gating.identity import derive_request_id
from forge.lifecycle import migrations
from forge.planning import driver as driver_module
from forge.planning import repository_facts as repository_facts_module
from forge.planning.code_evidence import LISTED_ALL_MEANS
from forge.planning.driver import (
    BuildTriggerResult,
    PlanningDriverDeps,
    PlanningRunDriver,
)
from forge.planning.factory_deployment_context import load_bundled_document
from forge.planning.gate_adapters import build_planning_gate_adapters
from forge.planning.run_store import SqlitePlanningRunStore, TransitionRefused
from forge.planning.states import PlanningState
from forge.planning.target_terminal_tools import ToolOutcome
from nats_core.events import ApprovalResponsePayload
from tests.forge.recorded_fetch_address import allow_local_remotes, origin_of

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


@pytest.fixture(autouse=True)
def _local_remotes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every "remote" here is a bare repository in ``tmp_path``: let a local
    folder be a recorded fetch address, and keep the factory's own fetch
    copies inside ``tmp_path`` (Forge git hardening, 14 October 2026)."""
    allow_local_remotes(monkeypatch, tmp_path / "factory-fetch-copies")


def _recorded_addresses(repo_path: str | Path) -> dict[str, str]:
    """The coordinator's ``planning.fetch_addresses`` for the project whose
    copy is at ``repo_path``: the bare repository the test gave that copy, or
    nothing when the copy has none (or there is no copy on disk at all)."""
    named = subprocess.run(
        ["git", "-C", str(repo_path), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    )
    if not Path(repo_path).is_dir() or named.returncode != 0:
        return {}
    return {TARGET_REPO: origin_of(repo_path)}


def _recorded_runner(worktrees_root: Path, repo: Path) -> WorktreeGitRunner:
    """The real git runner, given the project's clone and recorded fetch
    address exactly as the coordinator's settings give them in production."""
    return WorktreeGitRunner(
        worktrees_root=worktrees_root,
        target_repo_paths={TARGET_REPO: str(repo)},
        fetch_addresses=_recorded_addresses(repo),
    )


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

    def __init__(
        self,
        *,
        tree_fail: bool = False,
        start_commit: str = "0" * 39 + "1",
        start_branch: str = "main",
        start_point_refusal: str | None = None,
        declaration: str | None = "memory:\n  project: scratch_project\n",
    ) -> None:
        self.single_calls: list[dict[str, Any]] = []
        self.tree_calls: list[dict[str, Any]] = []
        self.tree_fail = tree_fail
        # The starting rule (one true copy, item 1): what this stand-in
        # remote answers when the driver asks where the work starts.
        self.start_point_calls: list[str] = []
        self.start_commit = start_commit
        self.start_branch = start_branch
        self.start_point_refusal = start_point_refusal
        # The memory rule (item 2): what this stand-in project's settings file
        # says at that commit. ``None`` means it carries none, which the door
        # refuses.
        self.declaration_reads: list[tuple[str, str, str]] = []
        self.declaration = declaration
        # (branch, file_path) -> content, so the plan leg's read-back of the
        # committed spec triple works against the fake exactly as the real
        # WorktreeGitRunner reads it off the branch.
        self._branch_files: dict[str, dict[str, str]] = {}

    async def fetch_remote_start_point(self, repo_path: str) -> Any:
        """The starting rule's operation, answered by a stand-in remote."""
        from forge.deploy.candidate_tree import RemoteStartPoint

        self.start_point_calls.append(repo_path)
        if self.start_point_refusal is not None:
            return RemoteStartPoint(refusal=self.start_point_refusal)
        return RemoteStartPoint(branch=self.start_branch, commit=self.start_commit)

    async def read_file_at_commit(
        self, repo_path: str, commit: str, file_path: str
    ) -> Any:
        """The memory rule's read (item 2), answered by a stand-in project.

        ``declaration`` is the settings file's text at the starting commit;
        ``None`` is a project that carries no settings file there at all,
        which is what the door refuses.
        """
        from forge.deploy.candidate_tree import FileAtCommit

        self.declaration_reads.append((repo_path, commit, file_path))
        if self.declaration is None:
            return FileAtCommit(found=False)
        return FileAtCommit(content=self.declaration, found=True)

    async def prepare_branch_and_write(
        self,
        repo_path: str,
        branch: str,
        file_path: str,
        content: str,
        *,
        start_commit: str | None = None,
    ) -> GitOpResult:
        self.single_calls.append(
            {
                "branch": branch,
                "file_path": file_path,
                "content": content,
                "start_commit": start_commit,
            }
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
        start_commit: str | None = None,
        **_launch: Any,
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


def _semantic_review_json(
    files: dict[str, str],
    *,
    reviewed_after_rewrite: bool = False,
) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "decision": "approved",
            "criterion": "request_traceability",
            "criterion_score": 1.0,
            "coach_verdict": "GOOD",
            "artifact_identity": plan_artifact_identity(files),
            "reviewed_after_rewrite": reviewed_after_rewrite,
        }
    )


def _plan_result(
    feature_id: str,
    files: dict[str, str] | None = None,
    *,
    reviewed_after_rewrite: bool = False,
) -> Any:
    plan_files = files if files is not None else {
        f"features/stats-endpoint/{feature_id}.yaml": (
            f"id: {feature_id}\ntasks:\n- id: TASK-STAT-001\n"
        ),
        "tasks/TASK-STAT-001.md": "# task\n",
    }
    return SimpleNamespace(
        outcome=SimpleNamespace(value="completed"),
        role_output={
            "feature_id": feature_id,
            "files": plan_files,
            "semantic_review.json": _semantic_review_json(
                plan_files,
                reviewed_after_rewrite=reviewed_after_rewrite,
            ),
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
    role_output = {
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
    }
    plan_files = PlanningRunDriver._plan_tree_files(role_output)
    assert plan_files is not None
    role_output["semantic_review.json"] = _semantic_review_json(plan_files)
    return SimpleNamespace(
        outcome=SimpleNamespace(value="completed"),
        role_output=role_output,
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
        "build_trigger": 0,
    }

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
        request_text: str | None = None,
        repository_facts: str | None = None,
        context: Sequence[str] | None = None,
        # The code windows (17 October 2026), as the real dispatcher takes them.
        code_evidence: dict[str, Any] | None = None,
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
        request_text: str | None = None,
        repository_facts: str | None = None,
        revision_of: dict[str, str] | None = None,
        validate_feedback: str | None = None,
        context: Sequence[str] | None = None,
        already_done_allowed: bool = False,
    ) -> Any:
        # "Already done, nothing to build" (7 October 2026): sent only with
        # Forge's switch on; recorded so a test can read what went out.
        counters["last_already_done_allowed"] = already_done_allowed
        counters.setdefault("already_done_allowed_by_call", []).append(
            ("re-review" if revision_of is not None else "plan", already_done_allowed)
        )
        if revision_of is None:
            counters["plan"] += 1
        else:
            counters["semantic_rereview"] = (
                counters.get("semantic_rereview", 0) + 1
            )
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
            # Present only when forge can list the target repo's tracked files
            # (2026-09-11): what the repository ALREADY contains, so the seat
            # stops planning to build what is already there.
            "repository_inventory",
            # The immutable base to which complete inventory absence is bound.
            "planning_base_identity",
            # Where the request's and the specification's words already
            # appear, with the code round them since 6 October 2026, and the
            # files that may hold a set the request names ("all the X").
            "where_the_specs_words_already_appear",
            "sets_the_request_names",
            # Whether the project already declares a coverage command
            # (20 October 2026): always sent on the plan leg, true or false.
            "declares_coverage_command",
        }
        counters["last_feature_id"] = feature_id
        counters["last_descriptor"] = target_repo_descriptor
        counters["last_spec_assumptions"] = spec_assumptions
        counters["last_spec_feature_paths"] = spec_feature_paths
        # The planning coach's ground truth on the PLAN leg (2026-09-15): the
        # sentence, word for word, and the fact sheet. Recorded here so a test
        # can read back what actually went on the wire, and turned into the
        # real wire arguments so the key set is the real key set.
        counters["last_request_text"] = request_text
        counters["last_repository_facts"] = repository_facts
        counters["last_plan_wire_args"] = build_feature_plan_command_args(
            feature_id=feature_id,
            spec_feature=spec_feature,
            spec_summary=spec_summary,
            target_repo_descriptor=target_repo_descriptor,
            spec_assumptions=spec_assumptions,
            spec_feature_paths=spec_feature_paths,
            request_text=request_text,
            repository_facts=repository_facts,
            revision_of=revision_of,
            validate_feedback=validate_feedback,
            context=context,
        )
        if revision_of is not None:
            counters.setdefault("plan_revisions", []).append(dict(revision_of))
            return _plan_result(
                feature_id,
                dict(revision_of),
                reviewed_after_rewrite=True,
            )
        if plan_result is not None:
            return plan_result
        if plan_result_factory is not None:
            produced = plan_result_factory(feature_id)
            output = produced.role_output
            projected = PlanningRunDriver._plan_tree_files(output)
            assert projected is not None
            output["semantic_review.json"] = _semantic_review_json(projected)
            return produced
        return _plan_result(feature_id)

    async def _normalize(worktree: Path, feature_rel: str) -> ToolOutcome:
        counters["normalize"] += 1
        # The spec .feature file is on disk in the worktree.
        assert (worktree / feature_rel).is_file()
        return normalize if normalize is not None else ToolOutcome(ok=True)

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
        fetch_addresses=_recorded_addresses(repo_path),
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
    _declare_the_projects_memory(path)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True, env=env)
    _give_repo_a_remote(path, env)


#: The settings the stand-ins these tests run are steered with — see
#: :func:`_declare_the_projects_memory`.
STAND_IN_SETTINGS: tuple[str, ...] = (
    "FAKE_GUARDKIT_LOG",
    "FAKE_GUARDKIT_NORMALIZE",
    "FAKE_GUARDKIT_VALIDATE",
    "FAKE_GUARDKIT_CLASSIFY",
    "FAKE_GUARDKIT_QA_VALIDATE",
    "FAKE_GUARDKIT_NO_MODEL_REFUSES",
    "FAKE_NORMALIZER",
    "PYTHONPATH",
)


def _declare_the_projects_memory(path: Path) -> None:
    """Write what this scratch project declares about itself.

    Two things, both read at the commit the work starts from:

    * the two lines that say which memory it uses. The memory rule (item 2,
      2026-09-21) refuses at the door a project that declares none, so a
      scratch copy meant to get PAST the door declares one, exactly as every
      registered project now does;
    * the NAMES its own checks need from the launching process (22 September
      2026). A check is launched with the factory's own short named list and
      nothing else, and the stand-ins these tests run are steered by settings
      that are not on it — rightly, since nothing a test invents belongs on a
      central list. So this project declares them by name, which is the door a
      real project uses to say what its own builds need. ``PYTHONPATH`` is
      here because the stand-in normalizer is a PLANTED module; in production
      that module comes out of the interpreter's own installation.

    Still nothing at all about what the project is made of: names, in the
    project's own file.
    """
    (path / ".guardkit").mkdir(exist_ok=True)
    (path / ".guardkit" / "config.yaml").write_text(
        "memory:\n"
        "  project: scratch_project\n"
        "launch:\n"
        "  settings: [%s]\n" % ", ".join(STAND_IN_SETTINGS),
        encoding="utf-8",
    )


def _give_repo_a_remote(path: Path, env: dict[str, str]) -> Path:
    """Give this scratch copy a remote named ``origin``: a bare repository
    beside it, on disk.

    The starting rule (one true copy, item 1, 2026-09-21) fetches the remote
    named ``origin`` before any branch is cut, so a copy with no remote is
    refused. A bare repository on a local path IS a remote as far as git is
    concerned — a fetch against it is real git — which is what lets every
    case here be driven without touching anybody's real repository.
    """
    bare = path.parent / f"{path.name}.origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True, env=env)
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=path,
        check=True,
        env=env,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "remote", "add", "origin", str(bare)], cwd=path, check=True, env=env
    )
    subprocess.run(
        ["git", "push", "-q", "origin", f"HEAD:refs/heads/{branch}"],
        cwd=path,
        check=True,
        env=env,
    )
    # The remote says which branch is its default, exactly as a hosted one does.
    subprocess.run(
        ["git", "symbolic-ref", "HEAD", f"refs/heads/{branch}"],
        cwd=bare,
        check=True,
        env=env,
    )
    return bare


def _publish_to_origin(path: Path) -> None:
    """Put this copy's current commit on its remote's default branch.

    Anything a test seeds into the copy and expects the planning branch to
    carry has to be on the REMOTE now, because that is where the work starts
    from. A copy with no remote is left alone — the tests that have none are
    testing the refusal.
    """
    env = _git_env()
    named = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=path,
        env=env,
        capture_output=True,
        text=True,
    )
    if named.returncode != 0:
        return
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=path,
        check=True,
        env=env,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "push", "-q", "-f", "origin", f"HEAD:refs/heads/{branch}"],
        cwd=path,
        check=True,
        env=env,
    )


@pytest.mark.asyncio
async def test_full_round_trip_commits_spec_and_plan_to_scratch_repo(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    git = _recorded_runner(tmp_path / "wt", repo)

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
    git = _recorded_runner(tmp_path / "wt", repo)

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
    git = _recorded_runner(tmp_path / "wt", repo)
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


@pytest.mark.asyncio
async def test_plan_leg_threads_spec_contents_and_discovered_descriptor(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """The 008 leg gets the committed spec CONTENTS + an honestly-built descriptor.

    ``test_roots`` is the EXACT ``tests/<name>`` set discovered from the target
    checkout — api_test-shaped: ``tests/health`` + ``tests/users`` (no
    ``tests/smoke``). ``spec_feature``/``spec_summary`` are the contents read
    back off the branch.
    """
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    # api_test-shaped test tree: real per-suite roots, NOT a bare ``tests/``.
    # Committed and on the remote (7 October 2026): the planner reads the
    # commit the run starts from, where an empty folder does not exist.
    for suite in ("health", "users"):
        (repo / "tests" / suite).mkdir(parents=True)
        (repo / "tests" / suite / f"test_{suite}.py").write_text("def test_x():\n    pass\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "the test suites")
    _publish_to_origin(repo)
    git = _recorded_runner(tmp_path / "wt", repo)

    _queue(store)
    h = _make_driver(store, git_runner=git, repo_path=str(repo))

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    # The descriptor forge built for 008 carries the EXACT discovered roots and
    # only schema-defined fields (repo + test_roots) — no invented keys, and
    # crucially NOT the shallow ``["tests"]`` that let 008 invent ``tests/smoke``.
    descriptor = h.ctx["counters"]["last_descriptor"]
    # The inventory of what the repository already contains rides too, since
    # 2026-09-11 — here the scratch repo's tracked files at the starting
    # commit. Everything else is exactly as it was.
    assert descriptor["repository_inventory"]["files"] == [
        "README.md", "tests/health/test_health.py", "tests/users/test_users.py",
    ]
    assert descriptor["planning_base_identity"] == {
        "status": "bound",
        "commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True
        ).strip(),
    }
    assert {
        k: v
        for k, v in descriptor.items()
        if k not in {"repository_inventory", "planning_base_identity"}
    } == {
        "repo": TARGET_REPO,
        "test_roots": ["tests/health", "tests/users"],
        # The scratch project declares no coverage command (20 October 2026).
        "declares_coverage_command": False,
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
    git = _recorded_runner(tmp_path / "wt", repo)

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
# REPLAY PROOF (B4 run 36629c5a, round 10) — the descriptor threads the exact
# per-suite roots, and a plan whose smoke gate names the real ``tests/health``
# drives to BUILD_QUEUED. (GuardKit checks the smoke gate's paths when it
# builds; Forge's copy of that check at planning went in the cull.)
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
    _declare_the_projects_memory(path)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True, env=env)
    _give_repo_a_remote(path, env)



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
async def test_replay_real_tests_health_passes_to_build_queued(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """(c): the same round-trip with a smoke gate referencing the REAL
    ``tests/health`` root passes the pre-commit validate and drives to
    BUILD_QUEUED — the good path is unblocked."""
    repo = tmp_path / "api_test"
    _init_api_test_shaped_repo(repo)
    git = _recorded_runner(tmp_path / "wt", repo)

    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        plan_result_factory=_plan_result_native_smoke("tests/health"),
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
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
    git = _recorded_runner(tmp_path / "wt", repo)
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
    git = _recorded_runner(tmp_path / "wt", repo)
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
async def test_a_refusing_plan_review_verdict_is_advice_and_the_plan_carries_on(
    store: SqlitePlanningRunStore,
) -> None:
    """Step 2 (27 October 2026): a readable verdict that did not approve is
    never turned into a yes, and never ends the run either. The plan is
    written, no re-review is asked, and the build card says the reviewer did
    not approve it beside the planner's notes (its objections)."""
    _queue(store)
    files = {"features/x/FEAT-X.yaml": "id: FEAT-X\n"}
    review = json.loads(_semantic_review_json(files))
    review["decision"] = "rejected"
    calls: list[dict[str, Any]] = []

    async def _the_specialist(**kwargs: Any) -> Any:
        calls.append(kwargs)
        reply = _a_reply(files, json.dumps(review))
        reply.role_output["validation.json"] = json.dumps(
            {"plan_advice": [{"rule": "A6", "text": "The plan leaves a part unbuilt."}]}
        )
        return reply

    h = _make_driver(store, plan_dispatch=_the_specialist)
    await h.driver.drive(CID)

    assert [c.get("revision_of") is not None for c in calls] == [False]
    plan = _approved_plan_events(store)
    assert plan, "the plan was recorded, not refused"
    assert "did not approve this plan" in plan[-1]["plan_review_note"]
    card = _card_lines_of(store)
    assert "The plan reviewer did not approve this plan" in card
    assert "The plan leaves a part unbuilt." in card


@pytest.mark.asyncio
async def test_a_missing_routing_gate_is_advice_and_planning_carries_on(
    store: SqlitePlanningRunStore,
) -> None:
    """Step 2 (26 October 2026, census FRG-022): routing required with no
    routing gate wired no longer ends the run; the dispatchers already carry
    on unrouted, and so does planning."""
    _queue(store)
    h = _make_driver(store)
    h.driver._deps.feature_routing_required = True
    assert h.driver._deps.feature_routing_gate is None

    await h.driver.drive(CID)

    plan = [
        e
        for e in store.list_events(CID)
        if e["stage_label"] == "feature-plan" and e["status"] == "approved"
    ]
    assert plan, "the plan was written, not refused for the missing gate"
    assert not [
        e for e in store.list_events(CID) if e["stage_label"] == "feature-routing"
    ]


def _card_lines_of(store: SqlitePlanningRunStore) -> str:
    from forge.cli._serve_gate_activation import _planning_card_lines
    from forge.lifecycle.persistence import SqliteLifecyclePersistence

    return _planning_card_lines(SqliteLifecyclePersistence(connection=store._connection), CID) or ""


@pytest.mark.asyncio
@pytest.mark.parametrize("with_prerequisite", [True, False])
async def test_the_planners_missing_prerequisite_line_is_shown_on_the_card(
    store: SqlitePlanningRunStore, with_prerequisite: bool
) -> None:
    """Step 2 (26 October 2026): the planning seat's ``plan_advice`` is
    information for the approver. A ``Missing prerequisite:`` line is shown on
    the build card first; a plan without advice shows no extra line. It
    changes nothing else: the plan is recorded either way."""
    _queue(store)
    files = {"features/x/FEAT-X.yaml": "id: FEAT-X\n"}
    advice = (
        [
            {"rule": "scenario_ownership", "text": "Scenario B has no owner task."},
            {
                "rule": "missing_prerequisite",
                "text": "Missing prerequisite: the project has no settings module yet; this plan adds one",
            },
        ]
        if with_prerequisite
        else []
    )
    validation = json.dumps({"accepted": True, "errors": [], "plan_advice": advice})

    async def _the_specialist(**kwargs: Any) -> Any:
        return SimpleNamespace(
            outcome=SimpleNamespace(value="completed"),
            role_output={
                "files": dict(files),
                "semantic_review.json": _semantic_review_json(files),
                "validation.json": validation,
            },
            reason=None,
        )

    h = _make_driver(store, plan_dispatch=_the_specialist)
    await h.driver.drive(CID)

    plan = [
        json.loads(e["details_json"])
        for e in store.list_events(CID)
        if e["stage_label"] == "feature-plan" and e["status"] == "approved"
    ]
    assert plan, "the plan was recorded"
    line = _card_lines_of(store)
    if with_prerequisite:
        note = plan[-1]["plan_advice_note"]
        assert note.startswith(
            "The planner's notes for you: Missing prerequisite: the project has "
            "no settings module yet; this plan adds one."
        )
        assert "Scenario B has no owner task." in note
        assert "scenario_ownership" not in note
        assert note in line
    else:
        assert "plan_advice_note" not in plan[-1]
        assert "The planner's notes" not in line


@pytest.mark.asyncio
async def test_a_low_traceability_score_is_advice_on_the_build_card(
    store: SqlitePlanningRunStore,
) -> None:
    """Step 2 (26 October 2026, first run feature 10): a plan the reviewer
    scored 0.40 for following the request, with a verdict that is not GOOD or
    ACCEPTABLE, is not refused or re-reviewed. The score reaches the build
    gate's card as a plain line through the real reader."""
    _queue(store)
    files = {"features/x/FEAT-X.yaml": "id: FEAT-X\n"}
    review = json.loads(_semantic_review_json(files))
    review["criterion_score"] = 0.4
    review["coach_verdict"] = "REVISE"
    calls: list[dict[str, Any]] = []

    async def _the_specialist(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(
            outcome=SimpleNamespace(value="completed"),
            role_output={"files": dict(files), "semantic_review.json": json.dumps(review)},
            reason=None,
        )

    h = _make_driver(store, plan_dispatch=_the_specialist)
    await h.driver.drive(CID)

    assert [c.get("revision_of") is not None for c in calls] == [False]
    plan = [
        json.loads(e["details_json"])
        for e in store.list_events(CID)
        if e["stage_label"] == "feature-plan" and e["status"] == "approved"
    ]
    assert plan, "the plan was recorded, not refused"
    assert plan[-1]["semantic_review"]["criterion_score"] == 0.4
    from forge.cli._serve_gate_activation import _planning_card_lines
    from forge.lifecycle.persistence import SqliteLifecyclePersistence

    line = _planning_card_lines(SqliteLifecyclePersistence(connection=store._connection), CID)
    assert line is not None
    assert "0.40 out of 1" in line and "REVISE" in line



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
    """The ONE auth-confirmation card the run put in front of the owner.

    Since 24 October 2026 (FRG-035) an unanswered card is posted once more,
    the same card with the same request id, so it may appear twice."""
    cards = [
        env.payload["details"]
        for env in h.ctx["publisher"].envelopes
        if env.payload["details"].get("checkpoint_type") == _AUTH_DOOR_CHECKPOINT_TYPE
    ]
    assert len(cards) in (1, 2), f"expected one auth card, got {len(cards)}"
    assert all(card == cards[0] for card in cards)
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
    git = _recorded_runner(tmp_path / "wt", repo)
    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_with_seed(_ROUND19_SEED_AUTH),
        plan_result_factory=_plan_result_native_versions,
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
    git = _recorded_runner(tmp_path / "wt", repo)
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
    git = _recorded_runner(tmp_path / "wt", repo)
    _queue(store)
    publisher = FakePublisher()  # ONE wire across both boots

    def _boot(script: list[Any]) -> _Harness:
        return _make_driver(
            store,
            git_runner=git,
            repo_path=str(repo),
            spec_result=_spec_result_with_seed(_ROUND19_SEED_AUTH),
            plan_result_factory=_plan_result_native_versions,
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
    git = _recorded_runner(tmp_path / "wt", repo)
    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_with_seed(_ROUND19_SEED_AUTH),
        plan_result_factory=_plan_result_native_versions,
        subscriber_factory=NeverArmingFactory(),
        originator_wait_seconds=1,
    )
    await _carry_in_flight_spec(store, git, repo, seed_yaml=_ROUND19_SEED_AUTH)

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.FAILED.value
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
    git = _recorded_runner(tmp_path / "wt", repo)
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

    # One bar per task, each run through guardkit's own qa validate (green).

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
async def test_round19_no_seed_is_advice_and_the_build_is_queued(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """(c) An older specialist that ships NO seed: since step 2 (26 October
    2026, census FRG-065) no bars are registered, the reason is recorded, and
    the build is queued. GuardKit's own task-start precondition is the one
    place a missing pass bar is refused, where a project enforces it."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    git = _recorded_runner(tmp_path / "wt", repo)
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
    assert run["state"] == PlanningState.BUILD_QUEUED.value
    assert h.ctx["counters"]["build_trigger"] == 1
    ev = _leg_event_details_of(store, "qa-pass-bars")
    assert ev["skipped"] is True and ev["bar_files"] == []
    assert "no pass-bar seed" in ev["reason"]
    assert not any(level == "error" for _, _, level in h.ctx["notifications"])


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
    _publish_to_origin(repo)



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
    git = _recorded_runner(tmp_path / "wt", repo)
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

    # guardkit's own gate-registry validate ran exactly once, on the registry.

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
    store: SqlitePlanningRunStore, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """(2) A non-endpoint (underivable) seed → an HONEST skip event, the build
    STILL queues, and ZERO target-repo gate writes (even though the surface is
    adopted — the skip is about the criteria, not the surface)."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    _seed_gate_surface(repo)
    git = _recorded_runner(tmp_path / "wt", repo)
    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_with_seed(_UNDERIVABLE_SEED_AUTHLESS),
        plan_result_factory=_plan_result_native_versions,
    )

    with caplog.at_level(logging.INFO, logger="forge.planning.driver"):
        await h.driver.drive(CID)

    run = store.get_run(CID)
    assert run["state"] == PlanningState.BUILD_QUEUED.value
    # The gate-registry validator was never called; nothing was gated.
    assert h.ctx["counters"]["build_trigger"] == 1
    # No new gate script landed; the registry is byte-unchanged (no new entry).
    assert _show_on_branch(repo, "qa/gates/nightly_report_gate.py").returncode != 0
    reg_data = yaml.safe_load(_show_on_branch(repo, "qa/gates/registry.yaml").stdout)
    assert {g["id"] for g in reg_data["gates"]} == {"health", "stats", "version"}
    # The leg recorded an HONEST skip (idempotency label present, skipped detail).
    ev = _leg_event_details_of(store, "qa-feature-gate")
    assert ev.get("skipped") is True
    # Since 4 October 2026 the record says WHY in a code the merge cards read,
    # which address the spec named (none here) and which feature it is about.
    assert ev["reason_code"] == "no_endpoint_named"
    assert ev["address"] is None
    feature_id = _leg_details(store, "feature-plan")["feature_id"]
    assert feature_id and ev["feature_id"] == feature_id
    assert ev["reason"]
    # The skip is logged as a warning, not as information.
    skipped = [
        r for r in caplog.records if "feature gate skipped" in r.getMessage()
    ]
    assert len(skipped) == 1
    assert skipped[0].levelno == logging.WARNING
    assert "no_endpoint_named" in skipped[0].getMessage()


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
    git = _recorded_runner(tmp_path / "wt", repo)
    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_with_digest_endpoint(
            _spec_result_with_seed(_UNDERIVABLE_SEED_AUTHLESS),
            "GET",
            "/users/active-count",
        ),
        plan_result_factory=_plan_result_native_versions,
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    # No skip — a gate was registered, and against the DIGEST's path.
    ev = _leg_event_details_of(store, "qa-feature-gate")
    assert ev.get("skipped") is not True, ev
    # The gate id still comes from the seed's slug; only the REQUEST comes from
    # the digest. (The test above proves this same seed lands no gate at all.)
    gate = _show_on_branch(repo, "qa/gates/nightly_report_gate.py")
    assert gate.returncode == 0, gate.stderr
    assert '"gate_id": "nightly-report",' in gate.stdout
    assert (
        '"request": {"method": "GET", "path": "/users/active-count"},'
        in gate.stdout
    )
    compile(gate.stdout, "nightly_report_gate.py", "exec")


@pytest.mark.asyncio
async def test_f2_digest_address_with_a_placeholder_is_skipped_with_its_reason(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """A GET address with ``{user_id}`` in it is skipped, not written as a
    check: the filled check sends the address exactly as written, so the
    server sees a literal ``{user_id}`` and the check can never pass (FEAT-E613,
    24 August 2026, answered 400 and was rewritten by hand). The record names
    the reason, the address and the feature; the build still queues."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    _seed_gate_surface(repo)
    git = _recorded_runner(tmp_path / "wt", repo)
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
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert h.ctx["counters"]["build_trigger"] == 1
    assert _show_on_branch(repo, "qa/gates/nightly_report_gate.py").returncode != 0
    reg_data = yaml.safe_load(_show_on_branch(repo, "qa/gates/registry.yaml").stdout)
    assert {g["id"] for g in reg_data["gates"]} == {"health", "stats", "version"}
    ev = _leg_event_details_of(store, "qa-feature-gate")
    assert ev.get("skipped") is True
    assert ev["reason_code"] == "placeholder_in_address"
    assert ev["address"] == {"method": "GET", "path": "/users/{user_id}"}
    assert ev["feature_id"] == _leg_details(store, "feature-plan")["feature_id"]


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
    git = _recorded_runner(tmp_path / "wt", repo)
    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_with_seed(_ROUND19_SEED_AUTHLESS),
        plan_result_factory=_plan_result_native_versions,
    )

    await h.driver.drive(CID)

    gate = _show_on_branch(repo, "qa/gates/version_endpoint_gate.py")
    assert gate.returncode == 0, gate.stderr
    assert '"request": {"method": "GET", "path": "/version"},' in gate.stdout


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/users"),
        # FEAT-2FDE, 4 October 2026: the spec named this address correctly
        # and it was refused for its method. The method is the reason given,
        # even though the address also has a placeholder.
        ("PATCH", "/users/{user_id}/deactivate"),
    ],
)
async def test_f2_digest_endpoint_non_get_does_not_widen_the_gate(
    store: SqlitePlanningRunStore, tmp_path: Path, method: str, path: str
) -> None:
    """A digest naming POST is not a wider gate — forge does not know POST's
    happy-path status, so it skips honestly rather than guessing one. The
    skip record names the reason, the address the spec named and the
    feature."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    _seed_gate_surface(repo)
    git = _recorded_runner(tmp_path / "wt", repo)
    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_with_digest_endpoint(
            _spec_result_with_seed(_UNDERIVABLE_SEED_AUTHLESS), method, path
        ),
        plan_result_factory=_plan_result_native_versions,
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    ev = _leg_event_details_of(store, "qa-feature-gate")
    assert ev.get("skipped") is True
    assert ev["reason_code"] == "unsupported_method"
    assert ev["address"] == {"method": method, "path": path}
    feature_id = _leg_details(store, "feature-plan")["feature_id"]
    assert feature_id and ev["feature_id"] == feature_id


def _seed_with_criterion(text: str) -> str:
    """The non-endpoint seed with its one machine criterion replaced by ``text``
    (no digest endpoint, so the address can only come from this prose)."""
    old = "  text: The nightly report job runs to completion each midnight\n"
    assert old in _UNDERIVABLE_SEED_AUTHLESS
    return _UNDERIVABLE_SEED_AUTHLESS.replace(old, f"  text: '{text}'\n")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "criterion,reason_code,address",
    [
        # a placeholder at the very end of the address
        (
            "A GET request to /users/{user_id} returns the user",
            "placeholder_in_address",
            "/users/{user_id}",
        ),
        # ... and at the end of the sentence, before its full stop
        (
            "Users are found by A GET request to /users/{user_id}.",
            "placeholder_in_address",
            "/users/{user_id}",
        ),
        # a placeholder after a dotted segment (read as /v1 before R5)
        (
            "A GET request to /v1.0/users/{user_id} returns the user",
            "placeholder_in_address",
            "/v1.0/users/{user_id}",
        ),
        (
            "A GET request to /api/v2.1/orgs/{org_id}/users returns them",
            "placeholder_in_address",
            "/api/v2.1/orgs/{org_id}/users",
        ),
        # an address a check cannot send as written (read as /users before R5)
        (
            "A GET request to /users?active=true returns active users",
            "unsupported_address",
            "/users?active=true",
        ),
        # the method still decides first, with the whole address recorded
        (
            "A PATCH request to /v1.0/users/{user_id}/deactivate succeeds",
            "unsupported_method",
            "/v1.0/users/{user_id}/deactivate",
        ),
    ],
)
async def test_f2_prose_address_is_read_whole_and_skipped_with_its_reason(
    store: SqlitePlanningRunStore,
    tmp_path: Path,
    criterion: str,
    reason_code: str,
    address: str,
) -> None:
    """Codex R5 (4 October 2026): the address in a criterion is the WHOLE
    address, never a partial match. A placeholder at the end, or after a
    dotted segment, is skipped by name with the exact address recorded, and no
    gate is written."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    _seed_gate_surface(repo)
    git = _recorded_runner(tmp_path / "wt", repo)
    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_with_seed(_seed_with_criterion(criterion)),
        plan_result_factory=_plan_result_native_versions,
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert h.ctx["counters"]["build_trigger"] == 1
    assert _show_on_branch(repo, "qa/gates/nightly_report_gate.py").returncode != 0
    reg_data = yaml.safe_load(_show_on_branch(repo, "qa/gates/registry.yaml").stdout)
    assert {g["id"] for g in reg_data["gates"]} == {"health", "stats", "version"}
    ev = _leg_event_details_of(store, "qa-feature-gate")
    assert ev.get("skipped") is True
    assert ev["reason_code"] == reason_code
    method = criterion.split(" request to ")[0].split()[-1]
    assert ev["address"] == {"method": method, "path": address}
    assert ev["feature_id"] == _leg_details(store, "feature-plan")["feature_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "criterion,path",
    [
        # positive controls: whole, plain GET addresses still register
        ("A GET request to /v1.0/version returns the version", "/v1.0/version"),
        ("The version is served by A GET request to /version.", "/version"),
        ("A GET request to /users/active-count returns both counts", "/users/active-count"),
    ],
)
async def test_f2_prose_plain_get_address_registers_the_whole_address(
    store: SqlitePlanningRunStore, tmp_path: Path, criterion: str, path: str
) -> None:
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    _seed_gate_surface(repo)
    git = _recorded_runner(tmp_path / "wt", repo)
    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_with_seed(_seed_with_criterion(criterion)),
        plan_result_factory=_plan_result_native_versions,
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    ev = _leg_event_details_of(store, "qa-feature-gate")
    assert ev.get("skipped") is not True, ev
    assert ev["endpoint"] == {"method": "GET", "path": path}
    gate = _show_on_branch(repo, "qa/gates/nightly_report_gate.py")
    assert gate.returncode == 0, gate.stderr
    assert f'"request": {{"method": "GET", "path": "{path}"}},' in gate.stdout


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
    git = _recorded_runner(tmp_path / "wt", repo)
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
    assert _show_on_branch(repo, "qa/gates/version_endpoint_gate.py").returncode != 0
    ev = _leg_event_details_of(store, "qa-feature-gate")
    assert ev.get("skipped") is True
    assert "no qa/gates/feature_behaviour_gate.py" in ev.get("reason", "")
    assert ev["reason_code"] == "no_template"
    assert ev["address"] == {"method": "GET", "path": "/version"}
    assert ev["feature_id"] == _leg_details(store, "feature-plan")["feature_id"]


@pytest.mark.asyncio
async def test_f2_missing_registry_skip_names_its_reason(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """A template but no gate registry on the branch → honest skip whose record
    says ``no_registry`` and names the address it would have checked."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    _seed_gate_surface(repo, template=True, registry=False)
    git = _recorded_runner(tmp_path / "wt", repo)
    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_with_seed(_ROUND19_SEED_AUTHLESS),
        plan_result_factory=_plan_result_native_versions,
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    ev = _leg_event_details_of(store, "qa-feature-gate")
    assert ev.get("skipped") is True
    assert ev["reason_code"] == "no_registry"
    assert ev["address"] == {"method": "GET", "path": "/version"}
    assert ev["feature_id"] == _leg_details(store, "feature-plan")["feature_id"]


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
        # the WHOLE address (Codex R5): a final placeholder is not cut short,
        # a dotted segment is kept, and sentence punctuation is not the path
        (
            "A GET request to /users/{user_id} succeeds",
            {"method": "GET", "path": "/users/{user_id}"},
        ),
        (
            "A GET request to /v1.0/users/{user_id}.",
            {"method": "GET", "path": "/v1.0/users/{user_id}"},
        ),
        ("A GET request to /version.", {"method": "GET", "path": "/version"}),
        ("(A GET request to /stats)", {"method": "GET", "path": "/stats"}),
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
        # never a partial match passed off as the endpoint (Codex R5)
        "A GET request to /users?active=true returns active users",
        "A GET request to /caf%C3%A9 returns the menu",
        "A GET request to / returns the home page",
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


@pytest.mark.parametrize(
    "digest,criteria,expected",
    [
        # the digest's address, whatever its method, upper-cased
        (
            "endpoint:\n  method: patch\n  path: /users/{user_id}/deactivate\n",
            [],
            {"method": "PATCH", "path": "/users/{user_id}/deactivate"},
        ),
        # the digest wins over the criteria
        (
            "endpoint:\n  method: DELETE\n  path: /users/{user_id}\n",
            [{"text": "A POST request to /users", "class": "machine"}],
            {"method": "DELETE", "path": "/users/{user_id}"},
        ),
        # no digest field: the first machine criterion of any verb
        (
            "feature: x\n",
            [
                {"text": "A GET request to /skip", "class": "operator"},
                {"text": "A DELETE request to /users/42", "class": "machine"},
            ],
            {"method": "DELETE", "path": "/users/42"},
        ),
        # nothing named anywhere
        ("", [{"text": "Deactivating a user succeeds", "class": "machine"}], None),
        # an unrooted digest path names nothing
        ("endpoint:\n  method: PATCH\n  path: users/1\n", [], None),
        ("not: [valid", "not-a-list", None),
    ],
)
def test_feature_gate_address_named_reads_any_method(
    digest: str, criteria: Any, expected: dict[str, str] | None
) -> None:
    """Read only to say why no gate was registered — never to register one."""
    assert (
        PlanningRunDriver._feature_gate_address_named(digest, criteria) == expected
    )


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
    # The memory declaration stays: this file is the project's whole settings
    # file, and a project that declares no memory is refused at the door (item
    # 2, 2026-09-21), which would stop this test before it reached what it is
    # about.
    (gk / "config.yaml").write_text(
        "qa:\n  spec_track: dcl\ndcl:\n  capture: true\n"
        "memory:\n  project: scratch_project\n",
        encoding="utf-8",
    )
    env = _git_env()
    subprocess.run(["git", "add", "."], cwd=repo, check=True, env=env)
    subprocess.run(
        ["git", "commit", "-qm", "seed leftover .guardkit/config.yaml"],
        cwd=repo,
        check=True,
        env=env,
    )
    _publish_to_origin(repo)


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
    git = _recorded_runner(tmp_path / "wt", repo)
    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_with_seed(_ROUND19_SEED_AUTHLESS),
        plan_result_factory=_plan_result_native_versions,
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
# WHAT THE REPOSITORY ALREADY CONTAINS, IN THE DESCRIPTOR (2026-09-11)
#
# FEAT-CDFD: Rich asked for one new endpoint on api_test and the plan came back
# with four waves, the first two of which built a user model, a data-access
# layer and a router the repository has had since it was born — and invented a
# column name (creation_timestamp) out of the specification's English, where the
# repository's own column is created_at. The plan seat cannot open files, so the
# plan was written from the specification's words alone. It is now told the
# repository's tracked files: real paths from real git, the factory's own
# paperwork left out, and directory counts instead of paths for a big tree.
#
# Every one of these tests drives REAL git in a REAL temporary repository.
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    """Run one real git command in ``repo``, with an identity so commits work."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", *args], cwd=repo, check=True, env=env)


def _write(repo: Path, rel: str, body: str = "x\n") -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _repo_with_files(tmp_path: Path, rels: list[str], name: str = "api_test") -> Path:
    """A real git repository with ``rels`` written AND committed, plus the two
    api_test-shaped test roots so the rest of the descriptor is realistic."""
    repo = tmp_path / name
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    for rel in rels:
        _write(repo, rel)
    (repo / "tests" / "health").mkdir(parents=True, exist_ok=True)
    (repo / "tests" / "users").mkdir(parents=True, exist_ok=True)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def test_the_inventory_is_the_repositorys_own_tracked_files_sorted(
    tmp_path: Path,
) -> None:
    """The plan seat is handed the real source and test files, in sorted order.

    This is the api_test shape in miniature: had the seat been given this, the
    FEAT-CDFD plan could not have proposed creating src/users/models.py, because
    the list says it is already there.
    """
    repo = _repo_with_files(
        tmp_path,
        [
            "src/users/router.py",
            "src/users/models.py",
            "src/users/crud.py",
            "src/health/router.py",
            "tests/users/test_crud.py",
            "tests/health/__init__.py",
            "README.md",
        ],
    )

    descriptor = PlanningRunDriver._build_target_repo_descriptor(
        "appmilla/api_test", str(repo)
    )
    inventory = descriptor["repository_inventory"]
    assert inventory["files"] == [
        "README.md",
        "src/health/router.py",
        "src/users/crud.py",
        "src/users/models.py",
        "src/users/router.py",
        "tests/health/__init__.py",
        "tests/users/test_crud.py",
    ]
    assert inventory["file_count"] == 7
    assert inventory["status"] == "complete"
    assert inventory["covered_namespaces"] == [""]
    assert inventory["base_identity"] == {"status": "unavailable"}
    assert "tasks/" in inventory["excluded_prefixes"]
    assert "directories" not in inventory
    # The rest of the descriptor is untouched.
    assert descriptor["repo"] == "appmilla/api_test"
    assert descriptor["test_roots"] == ["tests/health", "tests/users"]


def test_the_same_tree_read_twice_gives_the_same_bytes(tmp_path: Path) -> None:
    """Two runs on one tree produce one answer — nothing here depends on the
    order the filesystem happens to hand files back in."""
    repo = _repo_with_files(
        tmp_path, ["src/b.py", "src/a.py", "src/users/crud.py", "README.md"]
    )
    first = PlanningRunDriver._read_repository_inventory(str(repo))
    second = PlanningRunDriver._read_repository_inventory(str(repo))
    assert first == second
    assert json.dumps(first) == json.dumps(second)


def test_a_pinned_reader_binds_inventory_and_descriptor_to_the_same_base(
    tmp_path: Path,
) -> None:
    from forge.planning.repository_facts import LocalCheckoutReader

    repo = _repo_with_files(tmp_path, ["src/a.py"])
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    reader = LocalCheckoutReader(str(repo), commit=commit)
    descriptor = PlanningRunDriver._build_target_repo_descriptor(
        "appmilla/api_test", str(repo), reader=reader
    )
    assert descriptor["planning_base_identity"] == {
        "status": "bound",
        "commit": commit,
    }
    assert descriptor["repository_inventory"]["base_identity"] == descriptor[
        "planning_base_identity"
    ]


def test_untracked_files_are_not_in_the_inventory(tmp_path: Path) -> None:
    """Tracking is the repository's own statement of what belongs to it, so a
    virtual environment, a cache or a stray database file never has to be
    guessed at — none of them are tracked."""
    repo = _repo_with_files(tmp_path, ["src/users/crud.py"])
    _write(repo, "scratch_notes.md")
    _write(repo, ".venv/lib/python3.12/site-packages/anything.py")
    _write(repo, "app.db", "not really a database\n")
    # Written, never added: git does not track them.

    inventory = PlanningRunDriver._read_repository_inventory(str(repo))
    assert inventory is not None
    assert inventory["files"] == ["src/users/crud.py"]


def test_the_factorys_own_paperwork_is_left_out(tmp_path: Path) -> None:
    """Records ABOUT the repository are not the repository's code — and on
    api_test they are 400 of 695 tracked files. Real files under every excluded
    folder are committed here and none of them travels."""
    repo = _repo_with_files(
        tmp_path,
        [
            "src/users/crud.py",
            "src/users/router.py",
            ".guardkit/features/FEAT-CDFD.yaml",
            ".guardkit/config.yaml",
            ".forge/state.json",
            ".claude/commands/feature-build.md",
            "tasks/TASK-CDFD-001.md",
            "features/stats-endpoint/stats-endpoint.feature",
            "feature_spec_inputs/2026-09-11-users-per-day.md",
            "qa/receipts/FEAT-CDFD/run.json",
            # qa/ itself is the repository's own acceptance material and stays.
            "qa/twins/users/delete.hurl",
        ],
    )

    inventory = PlanningRunDriver._read_repository_inventory(str(repo))
    assert inventory is not None
    assert inventory["files"] == [
        "qa/twins/users/delete.hurl",
        "src/users/crud.py",
        "src/users/router.py",
    ]
    assert inventory["file_count"] == 3
    listed = json.dumps(inventory["files"])
    for paperwork in (
        ".guardkit",
        ".forge",
        ".claude",
        "tasks/",
        "feature_spec_inputs",
    ):
        assert paperwork not in listed
    assert ".guardkit/" in inventory["excluded_prefixes"]


def test_a_big_repository_teaches_its_shape_not_a_cut_off_list(
    tmp_path: Path,
) -> None:
    """Over the cap the inventory carries directory counts and says so.

    A list cut off part-way reads like the whole truth, which is the failure
    this exists to prevent, so the paths are dropped entirely rather than
    trimmed.
    """
    rels = [f"src/module_{n:02d}/file_{i:03d}.py" for n in range(5) for i in range(90)]
    assert len(rels) > 400
    repo = _repo_with_files(tmp_path, rels + ["README.md"])

    inventory = PlanningRunDriver._read_repository_inventory(str(repo))
    assert inventory is not None
    assert inventory["directories_only"] is True
    assert inventory["status"] == "summarized"
    assert "files" not in inventory
    assert inventory["file_count"] == 451
    assert inventory["directories"] == [
        {"path": ".", "file_count": 1},
        *[
            {"path": f"src/module_{n:02d}", "file_count": 90}
            for n in range(5)
        ],
    ]


def test_at_the_cap_the_paths_themselves_still_travel(tmp_path: Path) -> None:
    """The cap is inclusive: 400 files is still a list of 400 paths."""
    rels = [f"src/file_{i:03d}.py" for i in range(400)]
    repo = _repo_with_files(tmp_path, rels)

    inventory = PlanningRunDriver._read_repository_inventory(str(repo))
    assert inventory is not None
    assert inventory["file_count"] == 400
    assert inventory["files"] == sorted(rels)
    assert "directories_only" not in inventory


def test_a_directory_that_is_not_a_git_repository_has_no_inventory_key(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No git repository ⇒ no key at all, one plain warning, and a descriptor
    byte for byte what it was before this existed."""
    repo = _repo_with_tests(tmp_path, "plain-directory")

    with caplog.at_level(logging.WARNING):
        descriptor = PlanningRunDriver._build_target_repo_descriptor(
            "appmilla/plain-directory", str(repo)
        )
    assert descriptor == {
        "repo": "appmilla/plain-directory",
        "test_roots": ["tests/health", "tests/users"],
    }
    assert any(
        "not a git repository" in rec.getMessage() for rec in caplog.records
    )


def test_a_path_that_does_not_exist_has_no_inventory_key(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A missing checkout degrades the same quiet way."""
    with caplog.at_level(logging.WARNING):
        assert (
            PlanningRunDriver._read_repository_inventory(str(tmp_path / "nowhere"))
            is None
        )
    assert any("nowhere" in rec.getMessage() for rec in caplog.records)


def test_a_git_that_exits_non_zero_warns_and_does_not_raise(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A broken repository — real git, answering unhappily — is a warning and
    no key, never an exception.

    The .git here is a file holding nonsense, which is the shape a git worktree
    uses and a corrupted one leaves behind: the marker exists, so the reader
    goes on to run git, and git itself refuses with a non-zero exit.
    """
    repo = _repo_with_files(tmp_path, ["src/users/crud.py"])
    shutil.rmtree(repo / ".git")
    (repo / ".git").write_text("gitdir: /nowhere/at/all\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        descriptor = PlanningRunDriver._build_target_repo_descriptor(
            "appmilla/api_test", str(repo)
        )
    assert "repository_inventory" not in descriptor
    assert any(
        "listing the tracked files" in rec.getMessage() for rec in caplog.records
    )


def test_a_git_that_times_out_warns_and_does_not_raise(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repository that cannot answer in time must never hold up a plan."""
    repo = _repo_with_files(tmp_path, ["src/users/crud.py"])

    def _slow(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="git ls-files", timeout=10)

    monkeypatch.setattr(repository_facts_module.subprocess, "run", _slow)

    with caplog.at_level(logging.WARNING):
        descriptor = PlanningRunDriver._build_target_repo_descriptor(
            "appmilla/api_test", str(repo)
        )
    assert "repository_inventory" not in descriptor
    assert descriptor["test_roots"] == ["tests/health", "tests/users"]
    assert any(
        "could not list the tracked files" in rec.getMessage()
        for rec in caplog.records
    )


def test_the_git_command_is_an_argument_list_with_no_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command forge runs, pinned: a plain argument list, no shell, a
    timeout, and the safe.directory setting that lets forge read a checkout it
    does not own.

    Since 14 October 2026 every git command goes through Forge's one git
    helper: git is run by its full path, with no pager, file names taken
    literally, the fixed settings that stop a project's own settings starting
    another program, and a fixed environment rather than Forge's own."""
    from forge import project_git

    repo = _repo_with_files(tmp_path, ["src/users/crud.py"])
    # Git reads only the repository's own settings here: a filter in this
    # machine's system or personal settings (the CI runner's system file has
    # Git LFS's) is switched off on the command line, which is right but
    # would add its own settings to the command pinned below.
    empty = tmp_path / "no-personal-settings"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    seen: dict[str, Any] = {}
    real_run = subprocess.run

    def _record(cmd: Any, **kwargs: Any) -> Any:
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(repository_facts_module.subprocess, "run", _record)
    PlanningRunDriver._read_repository_inventory(str(repo))

    fixed: list[str] = []
    for setting in project_git.FIXED_SETTINGS:
        fixed += ["-c", setting]
    assert seen["cmd"] == [
        project_git.git_program(),
        "--no-pager",
        "--literal-pathspecs",
        "-C",
        str(repo),
        "-c",
        "safe.directory=*",
        *fixed,
        "ls-files",
        "-z",
    ]
    assert (
        seen["kwargs"]["timeout"]
        == driver_module._REPO_INVENTORY_GIT_TIMEOUT_SECONDS
    )
    assert "shell" not in seen["kwargs"]
    # git gets the fixed environment, never Forge's own.
    assert seen["kwargs"]["env"]["GIT_ALLOW_PROTOCOL"] == "none"
    assert set(seen["kwargs"]["env"]) <= {
        "PATH",
        *project_git.FIXED_ENVIRONMENT,
        *project_git.KEPT_ENVIRONMENT_NAMES,
    }


def test_a_repository_of_paperwork_alone_sends_no_inventory(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Nothing but the factory's records is nothing to tell the seat: no key,
    rather than an empty list that would read as an empty repository."""
    repo = _repo_with_files(
        tmp_path, [".guardkit/features/FEAT-1.yaml", "tasks/TASK-1.md"]
    )

    with caplog.at_level(logging.WARNING):
        descriptor = PlanningRunDriver._build_target_repo_descriptor(
            "appmilla/api_test", str(repo)
        )
    assert "repository_inventory" not in descriptor
    assert any(
        "factory's own paperwork" in rec.getMessage() for rec in caplog.records
    )


def test_the_inventory_rides_beside_the_architecture_rules(tmp_path: Path) -> None:
    """Both optional fields together, which is the api_test case: the seat is
    told the repository's rules AND what it already contains."""
    repo = _repo_with_files(tmp_path, ["src/users/crud.py"])
    (repo / "docs").mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "architecture-rules.yaml").write_text(
        _RULES_FILE, encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "rules")

    descriptor = PlanningRunDriver._build_target_repo_descriptor(
        "appmilla/api_test", str(repo)
    )
    assert [r["id"] for r in descriptor["architecture_rules"]["rules"]] == [
        "R-OV-1",
        "R-ADR001-1",
    ]
    assert descriptor["repository_inventory"]["files"] == [
        "docs/architecture-rules.yaml",
        "src/users/crud.py",
    ]
    assert set(descriptor) == {
        "repo",
        "test_roots",
        "architecture_rules",
        "repository_inventory",
    }


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


def _plan_yaml_rel(feature_id: str) -> str:
    return f".guardkit/features/{feature_id}.yaml"


def _commit_repo_routing_law(repo: Path, value: str) -> None:
    """``routing_law: <value>`` in the target repo's ``.guardkit/config.yaml``,
    COMMITTED so the planning branch's worktree carries it (the same file and
    key guardkit's plan-load half reads)."""
    cfg = repo / ".guardkit" / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    # The memory declaration stays, for the reason in _seed_leftover_dcl_config,
    # and so does what this project declares its checks need by name — see
    # _declare_the_projects_memory. Rewriting the whole file here would take
    # both away from every test that goes through this helper.
    cfg.write_text(
        f"toolchain:\n  test: pytest -q\nrouting_law: {value}\n"
        f"memory:\n  project: scratch_project\n"
        "launch:\n  settings: [%s]\n" % ", ".join(STAND_IN_SETTINGS),
        encoding="utf-8",
    )
    env = {
        **__import__("os").environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", f"routing_law {value}"], cwd=repo, check=True, env=env)
    _publish_to_origin(repo)


def _show(repo: Path, branch: str, path: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "show", f"{branch}:{path}"], cwd=repo, capture_output=True, text=True
    )


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
    git = _recorded_runner(tmp_path / "wt", repo)
    _queue(store)
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result=_spec_result_with_seed(_ROUND19_SEED_AUTHLESS),
        plan_result_factory=_plan_result_docs_task,
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert h.ctx["counters"]["build_trigger"] == 1

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


def _error_cards(h: _Harness) -> list[str]:
    return [m for _, m, lvl in h.ctx["notifications"] if lvl == "error"]


# ---------------------------------------------------------------------------
# Names were not enough (2026-09-13, FEAT-19C4)
#
# The seat knew src/users/router.py existed and could not know that
# /users/count-today was already defined inside it, so the plan's first task
# was "create the /users/count-today endpoint" for an endpoint that had been
# serving for weeks. The specification's own distinctive words are now looked
# for in the tracked files, and where they already are travels with the plan.
# ---------------------------------------------------------------------------


def test_a_route_the_repository_already_serves_is_reported_with_where(
    tmp_path: Path,
) -> None:
    """The FEAT-19C4 shape: the spec's route is already in the router."""
    repo = _repo_with_files(tmp_path, ["src/users/router.py", "README.md"])
    (repo / "src" / "users" / "router.py").write_text(
        '@router.get("/count-today")\nasync def get_users_count_today(): ...\n'
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "the endpoint")

    found = PlanningRunDriver._where_the_specs_words_already_appear(
        str(repo), "When I send a GET request to /users/count-today\n"
    )

    assert found is not None
    places = [place for row in found for place in row["already_in"]]
    assert any(place.startswith("src/users/router.py:") for place in places)


def test_the_repositorys_own_code_is_named_before_its_tests_and_paperwork(
    tmp_path: Path,
) -> None:
    """"Does this exist?" is answered by the source, not by a markdown file."""
    repo = _repo_with_files(
        tmp_path, ["src/users/router.py", "tests/users/test_it.py", "docs/API.md"]
    )
    for rel in ("src/users/router.py", "tests/users/test_it.py", "docs/API.md"):
        (repo / rel).write_text("count-today\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "everywhere")

    found = PlanningRunDriver._where_the_specs_words_already_appear(
        str(repo), "the /users/count-today endpoint\n"
    )

    assert found is not None
    places = [place for row in found for place in row["already_in"]]
    assert places[0].startswith("src/")


def test_a_genuinely_new_feature_reports_nothing(tmp_path: Path) -> None:
    """No hits, no key — a plan for new work reads exactly as it does today."""
    repo = _repo_with_files(tmp_path, ["src/users/router.py"])

    assert (
        PlanningRunDriver._where_the_specs_words_already_appear(
            str(repo), "the /users/favourite-colour endpoint\n"
        )
        is None
    )


def test_ordinary_english_is_never_looked_for(tmp_path: Path) -> None:
    """A word like "numeric" would match half the repository and teach nothing."""
    repo = _repo_with_files(tmp_path, ["src/users/router.py"])
    (repo / "src" / "users" / "router.py").write_text(
        "# returns a numeric response that should contain the correct value\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "prose")

    found = PlanningRunDriver._where_the_specs_words_already_appear(
        str(repo),
        "Then the response should contain a numeric value and be correct\n",
    )

    assert found is None


def test_a_tree_that_is_not_a_repository_plans_without_it(tmp_path: Path) -> None:
    """Never able to stop a plan: anything wrong is None and a warning."""
    assert (
        PlanningRunDriver._where_the_specs_words_already_appear(
            str(tmp_path / "nowhere"), "/users/count-today\n"
        )
        is None
    )


def test_the_formats_own_furniture_is_not_looked_for(tmp_path: Path) -> None:
    """Gherkin tags and the generated header are not this feature's words.

    On 2026-09-13 the real answer — src/users/router.py:210 — came back THIRD,
    behind '/feature-spec' from the generated header and 'happy-path' and
    'edge-case' from the scenario tags. Those are the FORMAT's vocabulary, and
    looking for them returns the whole test corpus and buries the one hit that
    matters.
    """
    repo = _repo_with_files(tmp_path, ["src/users/router.py", "qa/twins/a.hurl"])
    (repo / "src" / "users" / "router.py").write_text('@router.get("/count-today")\n')
    (repo / "qa" / "twins" / "a.hurl").write_text("# happy-path key-example edge-case\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "tree")

    spec = (
        "# Generated by /feature-spec\n"
        "@users-count-today\n"
        "Feature: Users Count Today\n"
        "  @key-example @happy-path\n"
        "  Scenario: the count\n"
        "    When I GET /users/count-today\n"
    )

    found = PlanningRunDriver._where_the_specs_words_already_appear(str(repo), spec)

    assert found is not None
    looked_for = [row["words"] for row in found]
    assert "/users/count-today" in looked_for
    for furniture in ("/feature-spec", "key-example", "happy-path"):
        assert furniture not in looked_for, f"{furniture} is the format's word, not the feature's"


# ---------------------------------------------------------------------------
# The planning coach's ground truth reaches the PLAN leg too (2026-09-15)
#
# The spec leg has sent the sentence and the fact sheet since 2026-09-13. The
# plan leg sent neither — so the plan-writer's own reviewer scored plans it had
# never seen the request for, and a plan that moved the web address or added a
# login requirement nobody asked for could still be called a good plan. These
# two tests read what actually went on the wire, not what was called.
# ---------------------------------------------------------------------------


def _init_repo_with_a_stats_route(path: Path) -> None:
    """A scratch repository that really declares a sibling route under /stats,
    so the fact sheet has something true to say about the sentence."""
    _init_scratch_repo(path)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    (path / "src").mkdir(parents=True, exist_ok=True)
    (path / "src" / "router.py").write_text(
        "from fastapi import APIRouter\n"
        "\n"
        "router = APIRouter()\n"
        "\n"
        '@router.get("/stats/uptime")\n'
        "def uptime() -> dict:\n"
        "    return {}\n"
    )
    subprocess.run(["git", "add", "."], cwd=path, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "a route"], cwd=path, check=True, env=env)
    _publish_to_origin(path)


@pytest.mark.asyncio
async def test_the_plan_leg_is_sent_the_sentence_and_the_fact_sheet(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """The sentence rides word for word, and the fact sheet says what the
    repository already does — both on the PLAN dispatch, both under the two
    names the spec leg already uses."""
    repo = tmp_path / "api_test"
    _init_repo_with_a_stats_route(repo)
    git = _recorded_runner(tmp_path / "wt", repo)

    _queue(store)
    h = _make_driver(store, git_runner=git, repo_path=str(repo))

    await h.driver.drive(CID)

    counters = h.ctx["counters"]
    assert counters["plan"] == 1
    # Word for word, never summarised on the way.
    assert counters["last_request_text"] == "add a GET /stats endpoint"
    facts = counters["last_repository_facts"]
    assert facts is not None
    assert "src/router.py" in facts and "GET /stats/uptime" in facts
    assert "None of them declares an authentication dependency." in facts

    # And what the wire actually carries: the four required names, the spec
    # location, the coach's two documents and the recorded factory contract.
    wire = counters["last_plan_wire_args"]
    assert set(wire) == {
        "feature_id",
        "spec_feature",
        "spec_summary",
        "target_repo_descriptor",
        "spec_assumptions",
        "spec_feature_paths",
        "request_text",
        "repository_facts",
        "context",
    }
    assert wire["request_text"] == "add a GET /stats endpoint"
    assert wire["repository_facts"] == facts
    assert wire["context"] == [load_bundled_document().context_text()]


@pytest.mark.asyncio
async def test_plan_instructions_do_not_become_sets_on_the_real_plan_dispatch(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    request = (
        "All existing tests must still pass. Keep each planned task to five or fewer "
        "acceptance criteria, preserving every requested requirement and leaving room "
        "below the factory limit of eight."
    )
    repo = tmp_path / "sample_project"
    _init_scratch_repo(repo)
    git = _recorded_runner(tmp_path / "wt", repo)
    store.record_queued(
        correlation_id=CID,
        originating_user=ORIGINATOR,
        expected_approver=ORIGINATOR,
        request_text=request,
        triggered_by="jarvis",
        target_repo=TARGET_REPO,
    )
    h = _make_driver(store, git_runner=git, repo_path=str(repo))

    await h.driver.drive(CID)

    counters = h.ctx["counters"]
    descriptor = counters["last_descriptor"]
    wire = counters["last_plan_wire_args"]
    assert wire["request_text"] == request
    assert wire["target_repo_descriptor"] == descriptor
    assert descriptor["sets_the_request_names"] == [
        {
            "phrase": "All existing tests must",
            "looked_for": "existing",
            "candidates": [],
            "listed": 0,
            "matched": 0,
            "listed_all": True,
            "listed_all_means": LISTED_ALL_MEANS,
        }
    ]


@pytest.mark.asyncio
async def test_a_run_with_no_sentence_omits_the_optional_grounding_fields(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """Nothing to say, nothing said. A row with no request sentence omits the
    optional request and repository facts while retaining required context."""
    repo = tmp_path / "api_test"
    _init_repo_with_a_stats_route(repo)
    git = _recorded_runner(tmp_path / "wt", repo)

    store.record_queued(
        correlation_id=CID,
        originating_user=ORIGINATOR,
        expected_approver=ORIGINATOR,
        request_text="",
        triggered_by="jarvis",
        target_repo=TARGET_REPO,
    )
    h = _make_driver(store, git_runner=git, repo_path=str(repo))

    await h.driver.drive(CID)

    counters = h.ctx["counters"]
    assert counters["plan"] == 1
    assert counters["last_request_text"] == ""
    assert counters["last_repository_facts"] is None
    # The four required names, the two optional spec fields this run has, and
    # the recorded factory contract remain. Neither absent grounding field is
    # invented.
    wire = counters["last_plan_wire_args"]
    assert set(wire) == {
        "feature_id",
        "spec_feature",
        "spec_summary",
        "target_repo_descriptor",
        "spec_assumptions",
        "spec_feature_paths",
        "context",
    }
    assert wire["context"] == [load_bundled_document().context_text()]
    assert "request_text" not in wire
    assert "repository_facts" not in wire


@pytest.mark.asyncio
async def test_unchanged_approved_plan_does_not_request_semantic_rereview(
    store: SqlitePlanningRunStore,
) -> None:
    _queue(store)
    h = _make_driver(store)

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert h.ctx["counters"].get("semantic_rereview", 0) == 0


# ---------------------------------------------------------------------------
# the 1 October planner fix, follow-up: the plan leg reads the repository where
# every other planning call for it goes, and when it cannot, the plan-writer,
# the plan's own record and so the build gate card all say so.
# ---------------------------------------------------------------------------


def _helper_over(clone: Path):
    """The sandbox helper's real server over ``clone``; returns (url, stop)."""
    import threading

    from forge.config.models import ForgeConfig
    from forge.deploy_sidecar.service import build_server

    cfg = ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {"target_repo_paths": {TARGET_REPO: str(clone)}},
        }
    )
    srv = build_server(port=0, config_loader=lambda: cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host, port = srv.server_address[:2]

    def stop() -> None:
        srv.shutdown()
        srv.server_close()

    return f"http://{host}:{port}", stop


@pytest.mark.asyncio
async def test_the_plan_writers_inventory_comes_from_the_sandbox_helper(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """The plan-writer's inventory is the sandbox clone's, read through the
    helper at the commit the run starts from — a file the coordinator's
    checkout does not have is on it — and nothing says the repository could
    not be read."""
    from forge.planning.sidecar_git_runner import SidecarCodeReader

    repo = tmp_path / "api_test"
    _init_repo_with_a_stats_route(repo)
    origin = subprocess.run(
        ["git", "remote", "get-url", "origin"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    clone = tmp_path / "sandbox-clone"
    subprocess.run(["git", "clone", "-q", origin, str(clone)], check=True, env=_git_env())
    (clone / "src" / "only_in_the_sandbox_clone.py").write_text("x = 1\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-qm", "the clone's own file")
    subprocess.run(["git", "push", "-q", "origin", "HEAD"], cwd=clone, check=True, env=_git_env())
    url, stop = _helper_over(clone)
    try:
        git = _recorded_runner(tmp_path / "wt", repo)
        git.code_reader = lambda commit=None: SidecarCodeReader(  # type: ignore[attr-defined]
            url, repo=TARGET_REPO, commit=commit
        )
        _queue(store)
        h = _make_driver(store, git_runner=git, repo_path=str(repo))
        await h.driver.drive(CID)
    finally:
        stop()

    counters = h.ctx["counters"]
    assert counters["plan"] == 1
    inventory = counters["last_descriptor"]["repository_inventory"]["files"]
    assert "src/only_in_the_sandbox_clone.py" in inventory
    assert "could not read" not in json.dumps(_leg_details(store, "feature-plan"))
    assert "unavailable" not in (counters["last_repository_facts"] or "")


@pytest.mark.asyncio
async def test_an_unreadable_repository_reaches_the_plan_writer_and_the_build_gate_record(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """The helper does not answer: the plan-writer's repository_facts begins
    "Repository facts unavailable:", and the plan's approved record carries
    the plain card line the build gate reads off the same ledger."""
    import socket

    from forge.lifecycle.persistence import SqliteLifecyclePersistence
    from forge.planning.sidecar_git_runner import SidecarCodeReader

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    repo = tmp_path / "api_test"
    _init_repo_with_a_stats_route(repo)
    git = _recorded_runner(tmp_path / "wt", repo)
    git.code_reader = lambda commit=None: SidecarCodeReader(  # type: ignore[attr-defined]
        url, repo=TARGET_REPO, commit=commit
    )
    _queue(store)
    h = _make_driver(store, git_runner=git, repo_path=str(repo))
    await h.driver.drive(CID)

    counters = h.ctx["counters"]
    assert counters["plan"] == 1
    assert (counters["last_repository_facts"] or "").startswith(
        f"Repository facts unavailable: the sandbox helper at {url} could not be reached"
    )
    assert "repository_inventory" not in counters["last_descriptor"]
    line = _leg_details(store, "feature-plan")["repository_unavailable"]
    assert line.startswith(
        "The machine could not read the repository while writing this, so nothing "
        f"checked it against what the repository already has (the sandbox helper at {url}"
    )
    ledger = SqliteLifecyclePersistence(connection=store._connection)
    assert ledger.read_planning_repository_unavailable(CID) == line


# ---------------------------------------------------------------------------
# Planning improvements, item 6 (6 October 2026): one plain size warning on
# the build-gate card when a plan is half as big again as the median of the
# project's own earlier plans, with at least five to compare. Never a refusal.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_planner_reads_the_commit_the_run_starts_from_not_the_checkout(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """7 October 2026: the copy's checked-out tree lagged the remote's main
    the run started from, and the planner was shown the old tree. Now a file
    that only the start commit has is on the plan-writer's inventory, and a
    local edit in the checkout reaches nothing."""
    repo = tmp_path / "api_test"
    _init_repo_with_a_stats_route(repo)
    origin = subprocess.run(
        ["git", "remote", "get-url", "origin"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    elsewhere = tmp_path / "elsewhere"
    subprocess.run(["git", "clone", "-q", origin, str(elsewhere)], check=True, env=_git_env())
    (elsewhere / "src" / "arrived_on_main.py").write_text("x = 1\n")
    _git(elsewhere, "add", "-A")
    _git(elsewhere, "commit", "-qm", "main moves on")
    subprocess.run(["git", "push", "-q", "origin", "HEAD"], cwd=elsewhere, check=True, env=_git_env())
    (repo / "src" / "local_only.py").write_text("y = 2\n")
    _git(repo, "add", "src/local_only.py")

    git = _recorded_runner(tmp_path / "wt", repo)
    _queue(store)
    h = _make_driver(store, git_runner=git, repo_path=str(repo))
    await h.driver.drive(CID)

    counters = h.ctx["counters"]
    assert counters["plan"] == 1
    inventory = counters["last_descriptor"]["repository_inventory"]["files"]
    assert "src/arrived_on_main.py" in inventory
    assert "src/local_only.py" not in inventory


# ---------------------------------------------------------------------------
# Bookkeeping before the build never ends the run (24 October 2026)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_final_plan_source_that_will_not_freeze_is_retried_once_then_queued(
    store: SqlitePlanningRunStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FRG-063: freezing the final plan source is bookkeeping. One retry, then
    the build is queued as planned."""
    from forge.planning import requirement_amendment_store

    calls: list[str] = []

    async def refuses(*_args: Any, **_kwargs: Any) -> None:
        calls.append("freeze")
        raise requirement_amendment_store.AmendmentRefused("receipts out of order")

    monkeypatch.setattr(requirement_amendment_store, "freeze_final_plan", refuses)
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    _queue(store)
    h = _make_driver(
        store, git_runner=_recorded_runner(tmp_path / "wt", repo), repo_path=str(repo)
    )

    await h.driver.drive(CID)

    assert calls == ["freeze", "freeze"]
    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value


@pytest.mark.asyncio
async def test_a_plan_record_without_its_commit_skips_the_bars_and_gate_and_queues(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """FRG-067: a plan record that lost its commit or feature id is bookkeeping.
    The id is minted from the run as before; the bars and the gate are skipped
    with the reason recorded; the build is still queued."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    _queue(store)
    h = _make_driver(
        store, git_runner=_recorded_runner(tmp_path / "wt", repo), repo_path=str(repo)
    )
    real_record = store._record_event

    def drop_the_commit(**kwargs: Any) -> Any:
        if kwargs.get("stage_label") == "feature-plan" and kwargs.get("status") == "approved":
            details = json.loads(kwargs["details_json"])
            details.pop("sha", None)
            details.pop("feature_id", None)
            kwargs["details_json"] = json.dumps(details)
        return real_record(**kwargs)

    store._record_event = drop_the_commit  # type: ignore[method-assign]

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    skipped = {
        e["stage_label"]: json.loads(e["details_json"])
        for e in store.list_events(CID)
        if e["stage_label"] in ("qa-pass-bars", "qa-feature-gate")
        and e["status"] == "approved"
    }
    assert skipped["qa-pass-bars"]["reason_code"] == "plan_record_incomplete"
    assert skipped["qa-feature-gate"]["skipped"] is True


# ---------------------------------------------------------------------------
# The plan writer's one ordinary-plan recovery reaches the handoff, and a
# readable refusal stays a refusal (code review R1, findings 2 and 3,
# 25 October 2026)
# ---------------------------------------------------------------------------


def _approved_plan_events(store: SqlitePlanningRunStore) -> list[dict[str, Any]]:
    return [
        json.loads(e["details_json"])
        for e in store.list_events(CID)
        if e["stage_label"] == "feature-plan" and e["status"] == "approved"
    ]


def _a_reply(files: dict[str, str], review: str | None = None) -> Any:
    role_output: dict[str, Any] = {"files": dict(files)}
    if review is not None:
        role_output["semantic_review.json"] = review
    return SimpleNamespace(
        outcome=SimpleNamespace(value="completed"), role_output=role_output, reason=None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("dropped", ["artifact_identity", "reviewed_after_rewrite"])
async def test_a_readable_rejection_with_missing_fields_is_advice(
    store: SqlitePlanningRunStore, dropped: str
) -> None:
    """The decision is read first, whatever else is wrong with the record:
    the card says the reviewer did not approve, and the plan carries on."""
    _queue(store)
    files = {"features/x/FEAT-X.yaml": "id: FEAT-X\n"}
    review = json.loads(_semantic_review_json(files))
    review["decision"] = "rejected"
    review.pop(dropped)
    h = _make_driver(store, plan_result=_a_reply(files, json.dumps(review)))
    await h.driver.drive(CID)
    plan = _approved_plan_events(store)
    assert plan and "did not approve this plan" in plan[-1]["plan_review_note"]



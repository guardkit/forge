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
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forge.adapters.git.models import GitOpResult
from forge.adapters.git.planning_runner import WorktreeGitRunner
from forge.adapters.sqlite import connect as sqlite_connect
from forge.config.models import PlanningConfig, TargetTerminalConfig
from forge.gating.identity import derive_request_id
from forge.lifecycle import migrations
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
            "files": files
            if files is not None
            else {
                f"features/{slug}/{slug}.feature": "Feature: stats\n  Scenario: ok\n    Given a\n",
                f"features/{slug}/{slug}_assumptions.yaml": "assumptions: []\n",
                f"features/{slug}/{slug}_summary.md": "# summary\n",
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
                f"features/stats-endpoint/{feature_id}.yaml": f"id: {feature_id}\n",
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
            f"{slug}.feature": "Feature: stats\n  Scenario: ok\n    Given a\n",
            f"{slug}_assumptions.yaml": "assumptions: []\n",
            f"{slug}_summary.md": "# summary\n",
            f"pass-bar-seed-{slug}.yaml": "format_version: '2.0'\n",
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
            f".guardkit/features/{feature_id}.yaml": f"id: {feature_id}\ntasks: []\n",
            f"tasks/backlog/{slug}/IMPLEMENTATION-GUIDE.md": "# guide\n",
            f"tasks/backlog/{slug}/TASK-STAT-001.md": "# task\n",
            "qa/pass-bar-TASK-STAT-001.yaml": "task_id: TASK-STAT-001\n",
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


def _make_driver(
    store: SqlitePlanningRunStore,
    *,
    target_terminal_enabled: bool = True,
    git_runner: Any | None = None,
    repo_path: str = "/srv/repos/api_test",
    spec_result: Any | None = None,
    plan_result: Any | None = None,
    plan_result_factory: Any | None = None,
    spec_dispatch: Any | None = None,
    plan_dispatch: Any | None = None,
    normalize: Any | None = None,
    validate: Any | None = None,
    wire_legs: bool = True,
    build_trigger_result: Any | None = None,
    build_trigger_fn: Any | None = None,
    wire_build_trigger: bool = True,
) -> _Harness:
    from datetime import UTC, datetime

    def clock() -> datetime:
        return datetime.now(UTC)

    repository, state_machine = build_planning_gate_adapters(store, clock=clock)
    publisher = FakePublisher()
    notifications: list[tuple[str, str, str]] = []
    counters = {
        "po": 0,
        "spec": 0,
        "plan": 0,
        "normalize": 0,
        "validate": 0,
        "build_trigger": 0,
    }

    def subscriber_factory(expected_approver: Any, armed: Any) -> ScriptedSubscriber:
        return ScriptedSubscriber([_approve()], armed)

    async def dispatch_po(*, plan_run_id: str, correlation_id: str, **_: Any) -> Any:
        counters["po"] += 1
        return _po_result()

    async def _dispatch_spec(*, plan_run_id: str, correlation_id: str, spec_input: str) -> Any:
        counters["spec"] += 1
        assert spec_input  # forge supplies the committed handoff content
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
        }
        counters["last_feature_id"] = feature_id
        counters["last_descriptor"] = target_repo_descriptor
        counters["last_spec_assumptions"] = spec_assumptions
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
        return validate if validate is not None else ToolOutcome(ok=True)

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

    async def publish_notification(cid: str, message: str, level: str) -> None:
        notifications.append((cid, message, level))

    cfg = PlanningConfig(
        enabled=True,
        target_repo_paths={TARGET_REPO: repo_path},
        target_terminal=TargetTerminalConfig(enabled=target_terminal_enabled),
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
        planning_config=cfg,
        clock=clock,
        publish_notification=publish_notification,
        dispatch_feature_spec=(spec_dispatch or _dispatch_spec) if wire_legs else None,
        dispatch_feature_plan=(plan_dispatch or _dispatch_plan) if wire_legs else None,
        normalize_feature_spec=_normalize if wire_legs else None,
        validate_feature_plan=_validate if wire_legs else None,
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
            "counters": counters,
            "git": deps.git_runner,
            "build_triggers": build_triggers,
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

    ``test_roots`` is discovered from the target checkout (a real ``tests/`` dir);
    ``spec_feature``/``spec_summary`` are the contents read back off the branch.
    """
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    (repo / "tests").mkdir()  # a real test root at the checkout root
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")

    _queue(store)
    h = _make_driver(store, git_runner=git, repo_path=str(repo))

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    # The descriptor forge built for 008 carries the discovered test root and
    # only schema-defined fields (repo + test_roots) — no invented keys.
    descriptor = h.ctx["counters"]["last_descriptor"]
    assert descriptor == {"repo": TARGET_REPO, "test_roots": ["tests"]}
    # The optional assumptions content was threaded (the default spec triple
    # carries an _assumptions.yaml).
    assert h.ctx["counters"]["last_spec_assumptions"] == "assumptions: []\n"


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

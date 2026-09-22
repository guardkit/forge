"""``SidecarGitRunner`` and ``RepoRoutedGitRunner`` (sandbox first, 2026-09-07,
rules 70 and 71).

The runner is driven against a REAL sidecar on an ephemeral loopback port
(the same server the production unit runs), writing into a real git
repository, with the stand-in guardkit running the declared checks; the
transport failures use a closed port and a stub HTTP seam. Nothing here
raises past the runner: every failure is a failed result with one plain
sentence, which is the protocol's contract.
"""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path
from typing import Any

import pytest

from forge.adapters.git.models import GitOpResult
from forge.adapters.git.planning_runner import WorktreeGitRunner
from forge.config.models import ForgeConfig
from forge.deploy_sidecar.service import GUARDKIT_PATH_ENV, build_server
from forge.planning.handoff import (
    PreCommitCheck,
    PreCommitCheckOutcome,
    PreCommitChecks,
    PreCommitResult,
)
from forge.planning.sidecar_git_runner import (
    CLOSURE_REFUSED_SENTENCE,
    RepoRoutedGitRunner,
    SidecarGitOpResult,
    SidecarGitRunner,
)

from tests.forge.deploy_sidecar._fake_guardkit import (
    REFUSED_TITLES,
    git_rev_parse,
    git_show,
    normalize_calls,
    scratch_repo,
    write_fake_guardkit,
)

REPO_KEY = "guardkit/api_test"
FEATURE = "FEAT-3ABD"
BRANCH = "planning/run-0001"
PLAN_YAML = f".guardkit/features/{FEATURE}.yaml"
PLAN_FILES = {
    PLAN_YAML: f"id: {FEATURE}\ntasks:\n- id: TASK-STAT-001\n",
    "tasks/backlog/stats/TASK-STAT-001.md": "# task\n",
}


def _config(paths: dict[str, str]) -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {"target_repo_paths": paths},
        }
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return scratch_repo(tmp_path / "api_test")


@pytest.fixture
def fake_guardkit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    binary = write_fake_guardkit(tmp_path / "bin")
    monkeypatch.setenv(GUARDKIT_PATH_ENV, str(binary))
    log = tmp_path / "guardkit-calls.jsonl"
    monkeypatch.setenv("FAKE_GUARDKIT_LOG", str(log))
    for name in ("FAKE_GUARDKIT_NORMALIZE", "FAKE_GUARDKIT_VALIDATE", "FAKE_GUARDKIT_CLASSIFY"):
        monkeypatch.delenv(name, raising=False)
    return log


@pytest.fixture
def sidecar(repo: Path, tmp_path: Path):
    cfg = _config({REPO_KEY: str(repo)})
    srv = build_server(port=0, config_loader=lambda: cfg, worktrees_root=tmp_path / "wt")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host, port = srv.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        srv.shutdown()
        srv.server_close()


def _declaration(*, no_model: bool = True) -> PreCommitChecks:
    return PreCommitChecks(
        (
            PreCommitCheck(
                "normalize-stamps", {"feature_id": FEATURE, "no_model": no_model}, blocking=True
            ),
            PreCommitCheck("feature-validate", {"feature_id": FEATURE}),
        )
    )


# ---------------------------------------------------------------------------
# The declaration types
# ---------------------------------------------------------------------------


def test_the_declaration_and_the_outcome_round_trip_the_wire() -> None:
    declaration = _declaration()
    assert declaration.to_wire() == [
        {
            "name": "normalize-stamps",
            "args": {"feature_id": FEATURE, "no_model": True},
            "blocking": True,
        },
        {"name": "feature-validate", "args": {"feature_id": FEATURE}, "blocking": True},
    ]
    outcome = PreCommitCheckOutcome(
        name="normalize-stamps",
        blocking=True,
        ran=True,
        passed=False,
        exit_code=2,
        stdout="{}",
        stderr_tail="",
        detail="stamp normalizer refused: x",
    )
    assert PreCommitCheckOutcome.from_wire(outcome.to_wire()) == outcome
    # A sparse or odd answer reads defensively, never raises.
    sparse = PreCommitCheckOutcome.from_wire({"name": "feature-validate", "exit_code": "?"})
    assert sparse.exit_code == -1 and not sparse.ran and sparse.detail == ""


def test_a_sidecar_result_is_a_git_op_result_that_carries_the_checks() -> None:
    result = SidecarGitOpResult(
        status="success",
        operation="prepare_branch_and_write_tree",
        sha="abc",
        exit_code=0,
        checks=[
            PreCommitCheckOutcome(
                name="feature-validate", blocking=True, ran=True, passed=True, exit_code=0
            )
        ],
    )
    assert isinstance(result, GitOpResult)
    assert result.checks[0].name == "feature-validate"
    assert json.loads(result.model_dump_json())["checks"][0]["passed"] is True


# ---------------------------------------------------------------------------
# Against the real sidecar
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_declared_write_commits_and_answers_with_the_checks(
    sidecar: str, repo: Path, fake_guardkit: Path
) -> None:
    runner = SidecarGitRunner(sidecar, repo=REPO_KEY)
    assert runner.supports_declared_checks() is True
    result = await runner.prepare_branch_and_write_tree(
        "/ignored/on/this/side", BRANCH, PLAN_FILES, "planning: plan", pre_commit=_declaration()
    )
    assert isinstance(result, SidecarGitOpResult)
    assert result.status == "success" and result.exit_code == 0
    assert result.sha == git_rev_parse(repo, BRANCH)
    assert [c.name for c in result.checks] == ["normalize-stamps", "feature-validate"]
    assert all(c.ran and c.passed for c in result.checks)
    assert json.loads(result.checks[0].stdout)["stamped"] == {"ok": "hurl"}
    assert "--no-model" in normalize_calls(fake_guardkit)[0]
    assert 'verifier: "hurl"' in (git_show(repo, BRANCH, PLAN_YAML) or "")


@pytest.mark.asyncio
async def test_a_refused_check_is_a_failed_result_with_the_outcomes(
    sidecar: str, repo: Path, fake_guardkit: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_GUARDKIT_NORMALIZE", "refused")
    runner = SidecarGitRunner(sidecar, repo=REPO_KEY)
    result = await runner.prepare_branch_and_write_tree(
        str(repo), BRANCH, PLAN_FILES, "planning: plan", pre_commit=_declaration()
    )
    assert result.status == "failed" and result.sha is None
    assert result.stderr == result.detail
    assert result.detail.startswith("pre-commit oracle refused the commit: stamp normalizer refused:")
    norm, validate = result.checks
    assert norm.ran and not norm.passed and norm.exit_code == 2
    assert json.loads(norm.stdout)["refused"] == list(REFUSED_TITLES)
    assert not validate.ran
    assert git_rev_parse(repo, BRANCH) in (None, git_rev_parse(repo, "HEAD"))


@pytest.mark.asyncio
async def test_a_python_closure_is_refused_in_one_sentence_and_never_sent(
    repo: Path,
) -> None:
    sent: list[Any] = []

    def recording_post(url: str, body: dict[str, Any], timeout: float) -> tuple[int, Any]:
        sent.append((url, body))
        return 200, {"status": "success", "sha": "x", "checks": [], "detail": ""}

    runner = SidecarGitRunner("http://127.0.0.1:9", repo=REPO_KEY, post=recording_post)

    async def closure(worktree: Path) -> PreCommitResult:
        return PreCommitResult(ok=True)

    result = await runner.prepare_branch_and_write_tree(
        str(repo), BRANCH, PLAN_FILES, "planning: plan", pre_commit=closure
    )
    assert result.status == "failed"
    assert result.stderr == CLOSURE_REFUSED_SENTENCE
    assert result.detail == "a sandbox git runner cannot run a Python closure; declare the checks"
    assert sent == []


@pytest.mark.asyncio
async def test_no_declaration_means_no_checks_on_the_wire(repo: Path) -> None:
    sent: list[Any] = []

    def recording_post(url: str, body: dict[str, Any], timeout: float) -> tuple[int, Any]:
        sent.append((url, body))
        return 200, {"status": "success", "sha": "abc", "checks": [], "detail": ""}

    runner = SidecarGitRunner("http://127.0.0.1:9/", repo=REPO_KEY, post=recording_post)
    result = await runner.prepare_branch_and_write_tree(
        str(repo), BRANCH, PLAN_FILES, "planning: plan"
    )
    assert result.status == "success" and result.sha == "abc"
    url, body = sent[0]
    assert url == "http://127.0.0.1:9/git/prepare-branch-and-write-tree"
    assert body == {
        "repo": REPO_KEY,
        "branch": BRANCH,
        "files": PLAN_FILES,
        "message": "planning: plan",
        "checks": [],
    }


@pytest.mark.asyncio
async def test_read_file_and_rev_parse_over_the_wire(
    sidecar: str, repo: Path, fake_guardkit: Path
) -> None:
    runner = SidecarGitRunner(sidecar, repo=REPO_KEY)
    await runner.prepare_branch_and_write_tree(str(repo), BRANCH, PLAN_FILES, "planning: plan")
    content = await runner.read_file_from_branch(
        repo_path=str(repo), branch=BRANCH, file_path=PLAN_YAML
    )
    assert content == PLAN_FILES[PLAN_YAML]
    assert (
        await runner.read_file_from_branch(repo_path=str(repo), branch=BRANCH, file_path="no.md")
        is None
    )
    assert await runner.rev_parse(str(repo), BRANCH) == git_rev_parse(repo, BRANCH)
    assert await runner.rev_parse(str(repo), "no-such-ref") is None
    # A refused request (a ref git could misread) is None too, never a raise.
    assert await runner.rev_parse(str(repo), "--all") is None


@pytest.mark.asyncio
async def test_the_single_file_form_goes_through_the_tree_route(
    sidecar: str, repo: Path, fake_guardkit: Path
) -> None:
    runner = SidecarGitRunner(sidecar, repo=REPO_KEY)
    result = await runner.prepare_branch_and_write(
        str(repo), BRANCH, "feature_spec_inputs/run-0001.md", "# input\n"
    )
    assert result.status == "success" and result.operation == "prepare_branch_and_write"
    assert git_show(repo, BRANCH, "feature_spec_inputs/run-0001.md") == "# input\n"
    # The same content again is the idempotent no-commit path.
    again = await runner.prepare_branch_and_write(
        str(repo), BRANCH, "feature_spec_inputs/run-0001.md", "# input\n"
    )
    assert again.status == "success" and again.sha == result.sha


@pytest.mark.asyncio
async def test_an_unreachable_sidecar_is_a_failed_result_never_a_raise(repo: Path) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    runner = SidecarGitRunner(
        f"http://127.0.0.1:{closed_port}", repo=REPO_KEY, read_timeout_s=2, write_timeout_s=2
    )
    result = await runner.prepare_branch_and_write_tree(
        str(repo), BRANCH, PLAN_FILES, "planning: plan", pre_commit=_declaration()
    )
    assert result.status == "failed"
    assert result.stderr.startswith(
        "the sandbox sidecar could not be reached for /git/prepare-branch-and-write-tree:"
    )
    assert (
        await runner.read_file_from_branch(repo_path=str(repo), branch=BRANCH, file_path=PLAN_YAML)
        is None
    )
    assert await runner.rev_parse(str(repo), BRANCH) is None


@pytest.mark.asyncio
async def test_a_refusal_from_the_sidecar_is_a_failed_result_with_its_sentence(
    sidecar: str, repo: Path
) -> None:
    runner = SidecarGitRunner(sidecar, repo="acme/ghost")
    result = await runner.prepare_branch_and_write_tree(
        str(repo), BRANCH, PLAN_FILES, "planning: plan"
    )
    assert result.status == "failed"
    assert result.stderr.startswith(f"the sandbox sidecar at {sidecar} answered 400: ")
    assert "unknown target repo 'acme/ghost'" in result.stderr


@pytest.mark.asyncio
async def test_a_transport_seam_that_raises_is_contained(repo: Path) -> None:
    def exploding(url: str, body: dict[str, Any], timeout: float) -> tuple[int, Any]:
        raise OSError("wire fell over")

    runner = SidecarGitRunner("http://127.0.0.1:9", repo=REPO_KEY, post=exploding)
    result = await runner.prepare_branch_and_write_tree(str(repo), BRANCH, PLAN_FILES, "m")
    assert result.status == "failed" and "OSError: wire fell over" in result.stderr


def test_the_runner_needs_the_repository_key() -> None:
    with pytest.raises(ValueError):
        SidecarGitRunner("http://127.0.0.1:9", repo="")


# ---------------------------------------------------------------------------
# Routing by repository (rule 71)
# ---------------------------------------------------------------------------


class _Recording:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[tuple[str, str]] = []

    async def fetch_remote_start_point(self, repo_path: str) -> Any:
        from forge.deploy.candidate_tree import RemoteStartPoint

        self.calls.append(("start-point", repo_path))
        return RemoteStartPoint(branch="main", commit="0" * 39 + "1")

    async def prepare_branch_and_write(self, repo_path: str, branch: str, file_path: str, content: str, *, start_commit: str | None = None) -> GitOpResult:
        self.calls.append(("single", repo_path))
        return GitOpResult(status="success", operation="prepare_branch_and_write", sha=self.name, exit_code=0)

    async def prepare_branch_and_write_tree(self, repo_path: str, branch: str, files: Any, message: str, *, pre_commit: Any = None, start_commit: str | None = None) -> GitOpResult:
        self.calls.append(("tree", repo_path))
        return GitOpResult(status="success", operation="prepare_branch_and_write_tree", sha=self.name, exit_code=0)

    async def read_file_from_branch(self, *, repo_path: str, branch: str, file_path: str) -> str | None:
        self.calls.append(("read", repo_path))
        return self.name


@pytest.mark.asyncio
async def test_calls_route_by_the_repository_path_and_fall_back_to_the_default() -> None:
    sandboxed = _Recording("sandboxed")
    default = _Recording("default")
    routed = RepoRoutedGitRunner(
        runners_by_repo={"guardkit/api_test": sandboxed},
        repo_paths={"guardkit/api_test": "/srv/repos/api_test/", "acme/other": "/srv/repos/other"},
        default=default,
    )
    assert routed.runner_for("guardkit/api_test") is sandboxed
    assert routed.runner_for("acme/other") is default
    assert routed.runner_for("nobody/knows") is default
    assert routed.supports_declared_checks() is False
    # The path form the driver passes (from target_repo_paths) routes, with
    # or without a trailing slash.
    r = await routed.prepare_branch_and_write_tree("/srv/repos/api_test", "b", {"a": "x"}, "m")
    assert r.sha == "sandboxed"
    r = await routed.prepare_branch_and_write("/srv/repos/api_test/", "b", "f", "c")
    assert r.sha == "sandboxed"
    assert await routed.read_file_from_branch(repo_path="/srv/repos/other", branch="b", file_path="f") == "default"
    assert await routed.read_file_from_branch(repo_path="/elsewhere", branch="b", file_path="f") == "default"
    assert sandboxed.calls == [("tree", "/srv/repos/api_test"), ("single", "/srv/repos/api_test/")]
    assert default.calls == [("read", "/srv/repos/other"), ("read", "/elsewhere")]


@pytest.mark.asyncio
async def test_the_default_runner_is_the_real_in_container_one_and_behaves_as_before(
    repo: Path, tmp_path: Path
) -> None:
    """A repository with no sandbox goes to the in-container runner, closure
    and all — the same worktree commit as before this lane."""
    default = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    routed = RepoRoutedGitRunner(runners_by_repo={}, repo_paths={}, default=default)
    seen: list[Path] = []

    async def closure(worktree: Path) -> PreCommitResult:
        seen.append(worktree)
        return PreCommitResult(ok=True)

    result = await routed.prepare_branch_and_write_tree(
        str(repo), BRANCH, PLAN_FILES, "planning: plan", pre_commit=closure
    )
    assert result.status == "success" and result.sha == git_rev_parse(repo, BRANCH)
    assert len(seen) == 1 and str(seen[0]).startswith(str((tmp_path / "wt").resolve()))

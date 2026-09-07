"""The sidecar's git routes — the planning chain's commits made where the
repository lives (sandbox first, 2026-09-07, rule 70).

Real code paths throughout: a real git repository in a temporary directory,
the real ``WorktreeGitRunner`` (imported by the sidecar) making the worktree
and the commit, the real no-shell subprocess core running a stand-in
``guardkit`` named through ``FORGE_GUARDKIT_PATH`` (the way the sidecar
resolves the real one), and — for the end-to-end cases — a real HTTP server
on an ephemeral loopback port. The stand-in is the one thing that is not
real: it writes stamps and prints JSON the way guardkit's verbs do, with the
same exit codes, so the parsers read it exactly as they read guardkit. A
live case at the bottom drives guardkit's real verbs when a checkout that
carries them is reachable, and skips in one sentence when it is not.
"""

from __future__ import annotations

import json
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from forge.config.models import ForgeConfig
from forge.deploy_sidecar.service import (
    GIT_CHECK_BLOCKING_DEFAULTS,
    GIT_CHECK_NAMES,
    GIT_CHECK_TIMEOUT_DEFAULTS,
    GIT_READ_FILE_ROUTE,
    GIT_REV_PARSE_ROUTE,
    GIT_WRITE_TREE_ROUTE,
    GUARDKIT_PATH_ENV,
    MERGE_TIMEOUT_EXIT_CODE,
    TIMEOUT_MAX,
    build_server,
    process_git_read_file_request,
    process_git_rev_parse_request,
    process_git_write_tree_request,
    resolve_check_command,
)
from forge.planning.handoff import PRE_COMMIT_CHECK_NAMES
from forge.planning.target_terminal_tools import NO_MODEL_OPTION_UNKNOWN_NOTE

from tests.forge.deploy_sidecar._fake_guardkit import (
    REFUSED_TITLES,
    classify_calls,
    git_rev_parse,
    git_show,
    normalize_calls,
    scratch_repo,
    validate_calls,
    validate_saw,
    write_fake_guardkit,
)
from tests.forge.planning._live_guardkit import (
    live_cli_importable,
    live_guardkit_checkout,
    live_guardkit_python,
)

REPO_KEY = "guardkit/api_test"
FEATURE = "FEAT-3ABD"
BRANCH = "planning/run-0001"
PLAN_YAML = f".guardkit/features/{FEATURE}.yaml"
PLAN_FILES = {
    PLAN_YAML: f"id: {FEATURE}\ntasks:\n- id: TASK-STAT-001\n",
    "tasks/backlog/stats/TASK-STAT-001.md": "# task\n",
}
MESSAGE = f"planning: feature plan {FEATURE} for run-0001 (Lane B 008)"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
def cfg(repo: Path) -> ForgeConfig:
    return _config({REPO_KEY: str(repo)})


@pytest.fixture
def fake_guardkit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The stand-in guardkit, named through the setting, with a call log."""
    binary = write_fake_guardkit(tmp_path / "bin")
    monkeypatch.setenv(GUARDKIT_PATH_ENV, str(binary))
    log = tmp_path / "guardkit-calls.jsonl"
    monkeypatch.setenv("FAKE_GUARDKIT_LOG", str(log))
    for name in (
        "FAKE_GUARDKIT_NORMALIZE",
        "FAKE_GUARDKIT_VALIDATE",
        "FAKE_GUARDKIT_CLASSIFY",
        "FAKE_GUARDKIT_NO_MODEL_REFUSES",
    ):
        monkeypatch.delenv(name, raising=False)
    return log


def _write(
    cfg: ForgeConfig,
    tmp_path: Path,
    *,
    checks: list[dict[str, Any]] | None,
    files: dict[str, str] | None = None,
    branch: str = BRANCH,
    **overrides: Any,
) -> tuple[int, dict[str, Any]]:
    payload: dict[str, Any] = {
        "repo": REPO_KEY,
        "branch": branch,
        "files": dict(PLAN_FILES if files is None else files),
        "message": MESSAGE,
        "checks": checks,
        **overrides,
    }
    return process_git_write_tree_request(
        payload, config=cfg, worktrees_root=tmp_path / "wt"
    )


def _normalize_check(**args: Any) -> dict[str, Any]:
    return {"name": "normalize-stamps", "args": {"feature_id": FEATURE, **args}}


def _validate_check() -> dict[str, Any]:
    return {"name": "feature-validate", "args": {"feature_id": FEATURE}}


def _by_name(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["name"]: c for c in body["checks"]}


def _no_commit_landed(repo: Path, branch: str = BRANCH) -> bool:
    """True when no commit was made on ``branch``: the branch is absent, or
    it still points at the base commit — the runner's ``git worktree add
    -b`` creates it there before the checks run, and a refused commit leaves
    it there (zero branch mutation)."""
    return git_rev_parse(repo, branch) in (None, git_rev_parse(repo, "HEAD"))


# ---------------------------------------------------------------------------
# The closed list of checks is the protocol's own
# ---------------------------------------------------------------------------


def test_the_checks_the_sidecar_runs_are_the_ones_the_protocol_names() -> None:
    assert GIT_CHECK_NAMES == PRE_COMMIT_CHECK_NAMES == (
        "normalize-stamps",
        "feature-validate",
        "classify-scenarios",
    )
    assert GIT_CHECK_BLOCKING_DEFAULTS == {
        "normalize-stamps": True,
        "feature-validate": True,
        "classify-scenarios": False,
    }
    assert set(GIT_CHECK_TIMEOUT_DEFAULTS) == set(GIT_CHECK_NAMES)


# ---------------------------------------------------------------------------
# Refusals — before any worktree is made
# ---------------------------------------------------------------------------


def test_body_must_be_an_object(cfg: ForgeConfig, tmp_path: Path) -> None:
    status, body = process_git_write_tree_request(["nope"], config=cfg)
    assert status == 400 and "JSON object" in body["error"]


def test_repo_is_required_and_must_be_known(cfg: ForgeConfig, tmp_path: Path) -> None:
    status, body = _write(cfg, tmp_path, checks=[], repo="acme/ghost")
    assert status == 400
    assert "unknown target repo" in body["error"] and REPO_KEY in body["error"]
    status, body = _write(cfg, tmp_path, checks=[], repo="")
    assert status == 400 and "'repo' is required" in body["error"]


@pytest.mark.parametrize(
    "bad_branch",
    ["", "-x", "--force", "a b", "planning/..", "a//b", "trail/", "x.lock", None, 7],
)
def test_a_branch_git_could_misread_is_refused(
    cfg: ForgeConfig, tmp_path: Path, bad_branch: Any
) -> None:
    status, body = _write(cfg, tmp_path, checks=[], branch=bad_branch)
    assert status == 400
    assert "'branch'" in body["error"]


@pytest.mark.parametrize(
    "bad_files",
    [
        None,
        {},
        [],
        {"/etc/passwd": "x"},
        {"../escape.md": "x"},
        {"a/./b.md": "x"},
        {"a//b.md": "x"},
        {"ok.md": 7},
        {"ok\\win.md": "x"},
    ],
)
def test_files_must_be_relative_paths_to_text(
    cfg: ForgeConfig, tmp_path: Path, bad_files: Any
) -> None:
    status, body = process_git_write_tree_request(
        {
            "repo": REPO_KEY,
            "branch": BRANCH,
            "files": bad_files,
            "message": MESSAGE,
            "checks": [],
        },
        config=cfg,
        worktrees_root=tmp_path / "wt",
    )
    assert status == 400
    assert "relative path" in body["error"] or "must be text" in body["error"]


def test_the_message_is_required(cfg: ForgeConfig, tmp_path: Path) -> None:
    status, body = _write(cfg, tmp_path, checks=[], message="  ")
    assert status == 400 and "'message' is required" in body["error"]


def test_a_check_the_sidecar_does_not_run_is_refused_by_name(
    cfg: ForgeConfig, tmp_path: Path
) -> None:
    status, body = _write(cfg, tmp_path, checks=[{"name": "run-tests", "args": {}}])
    assert status == 400
    assert "'run-tests'" in body["error"]
    assert "normalize-stamps, feature-validate, classify-scenarios" in body["error"]


def test_checks_must_be_a_list_of_objects(cfg: ForgeConfig, tmp_path: Path) -> None:
    status, body = _write(cfg, tmp_path, checks={"name": "feature-validate"})
    assert status == 400 and "'checks' must be a list" in body["error"]
    status, body = _write(cfg, tmp_path, checks=["feature-validate"])
    assert status == 400 and "checks[0] must be an object" in body["error"]


@pytest.mark.parametrize("bad", [None, "", "feat-1", "FEAT-ab", 3])
def test_a_bad_feature_id_on_a_check_is_refused(
    cfg: ForgeConfig, tmp_path: Path, bad: Any
) -> None:
    status, body = _write(
        cfg, tmp_path, checks=[{"name": "normalize-stamps", "args": {"feature_id": bad}}]
    )
    assert status == 400 and "args.feature_id must look like FEAT-ABC1" in body["error"]


def test_no_model_must_be_true_or_false(cfg: ForgeConfig, tmp_path: Path) -> None:
    status, body = _write(cfg, tmp_path, checks=[_normalize_check(no_model="yes")])
    assert status == 400 and "args.no_model must be true or false" in body["error"]


def test_arguments_a_check_does_not_take_are_refused(
    cfg: ForgeConfig, tmp_path: Path
) -> None:
    status, body = _write(cfg, tmp_path, checks=[_normalize_check(model="workhorse")])
    assert status == 400 and "does not take: model" in body["error"]


def test_classify_scenarios_may_never_be_declared_blocking(
    cfg: ForgeConfig, tmp_path: Path
) -> None:
    status, body = _write(
        cfg,
        tmp_path,
        checks=[
            {
                "name": "classify-scenarios",
                "args": {"feature_file": "f.feature"},
                "blocking": True,
            }
        ],
    )
    assert status == 400
    assert body["error"] == (
        "classify-scenarios never blocks a commit — declare it without 'blocking'"
    )


def test_classify_scenarios_needs_a_relative_feature_file(
    cfg: ForgeConfig, tmp_path: Path
) -> None:
    status, body = _write(
        cfg,
        tmp_path,
        checks=[{"name": "classify-scenarios", "args": {"feature_file": "/tmp/x.feature"}}],
    )
    assert status == 400 and "args.feature_file" in body["error"]


@pytest.mark.parametrize("bad_timeout", [0, -1, True, "60", TIMEOUT_MAX + 1])
def test_a_check_time_limit_must_be_a_positive_number_under_the_cap(
    cfg: ForgeConfig, tmp_path: Path, bad_timeout: Any
) -> None:
    status, body = _write(
        cfg, tmp_path, checks=[{**_validate_check(), "timeout_seconds": bad_timeout}]
    )
    assert status == 400 and "'timeout_seconds' must be a positive number" in body["error"]


def test_blocking_must_be_true_or_false(cfg: ForgeConfig, tmp_path: Path) -> None:
    status, body = _write(cfg, tmp_path, checks=[{**_validate_check(), "blocking": "no"}])
    assert status == 400 and "'blocking' must be true or false" in body["error"]


def test_no_guardkit_anywhere_is_a_plain_500_before_any_worktree(
    cfg: ForgeConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    monkeypatch.delenv(GUARDKIT_PATH_ENV, raising=False)
    status, body = process_git_write_tree_request(
        {
            "repo": REPO_KEY,
            "branch": BRANCH,
            "files": PLAN_FILES,
            "message": MESSAGE,
            "checks": [_validate_check()],
        },
        config=cfg,
        command_resolver=lambda: None,
        worktrees_root=tmp_path / "wt",
    )
    assert status == 500
    assert "no guardkit command to run the declared checks" in body["error"]
    assert _no_commit_landed(repo)  # nothing was made


# ---------------------------------------------------------------------------
# Writes without checks — the runner's own behaviour, reached over the route
# ---------------------------------------------------------------------------


def test_a_write_with_no_checks_commits_the_files(
    cfg: ForgeConfig, tmp_path: Path, repo: Path
) -> None:
    status, body = _write(cfg, tmp_path, checks=None)
    assert status == 200, body
    assert body["status"] == "success"
    assert body["checks"] == []
    assert body["sha"] == git_rev_parse(repo, BRANCH)
    for rel, content in PLAN_FILES.items():
        assert git_show(repo, BRANCH, rel) == content
    subject = subprocess.run(
        ["git", "log", "-1", "--format=%s", BRANCH], cwd=repo, capture_output=True, text=True
    ).stdout.strip()
    assert subject == MESSAGE


def test_the_same_files_again_is_the_idempotent_no_commit_path(
    cfg: ForgeConfig, tmp_path: Path, repo: Path
) -> None:
    first = _write(cfg, tmp_path, checks=[])[1]
    second = _write(cfg, tmp_path, checks=[])[1]
    assert second["status"] == "success" and second["sha"] == first["sha"]


def test_the_working_copy_is_never_touched(
    cfg: ForgeConfig, tmp_path: Path, repo: Path
) -> None:
    before = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
    ).stdout
    _write(cfg, tmp_path, checks=[])
    after = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert before == after == ""
    assert not (repo / PLAN_YAML).exists()


# ---------------------------------------------------------------------------
# Declared checks — run with the guardkit beside the sidecar, in the worktree
# ---------------------------------------------------------------------------


def test_the_checks_run_in_order_in_the_worktree_and_the_stamps_ride_the_commit(
    cfg: ForgeConfig, tmp_path: Path, repo: Path, fake_guardkit: Path
) -> None:
    """The plan leg's declaration: the normalizer, then feature validate.
    The normalizer WRITES stamps into the plan YAML in the worktree, validate
    sees the stamped YAML, and the commit carries it — exactly what the
    driver's closure does in the forge container."""
    status, body = _write(
        cfg, tmp_path, checks=[_normalize_check(no_model=True), _validate_check()]
    )
    assert status == 200, body
    assert body["status"] == "success" and body["sha"] == git_rev_parse(repo, BRANCH)
    checks = body["checks"]
    assert [c["name"] for c in checks] == ["normalize-stamps", "feature-validate"]
    assert all(c["ran"] and c["passed"] and c["blocking"] for c in checks)
    assert checks[0]["exit_code"] == 0 and checks[1]["exit_code"] == 0
    # The normalizer's whole JSON came back on stdout, for the driver's parser.
    result = json.loads(checks[0]["stdout"])
    assert result["stamped"] == {"ok": "hurl"} and result["written"] is True
    assert checks[0]["detail"].startswith("stamp normalizer written:")
    # The argv is the one forge's own collaborator builds, --no-model included,
    # and every check ran INSIDE the worktree, never in the checkout.
    calls = normalize_calls(fake_guardkit)
    assert len(calls) == 1
    assert calls[0][:4] == ["qa", "normalize-stamps", "--feature", FEATURE]
    assert calls[0][4] == "--repo" and calls[0][6] == "--no-model"
    worktree = calls[0][5]
    assert worktree.startswith(str(tmp_path / "wt")) and worktree != str(repo)
    assert validate_calls(fake_guardkit) == [["feature", "validate", FEATURE, "--json"]]
    assert validate_saw(fake_guardkit) == [(True, False)]
    # The stamped YAML is what landed on the branch.
    on_branch = git_show(repo, BRANCH, PLAN_YAML) or ""
    assert 'scenarios:\n  "ok":\n    verifier: "hurl"\n' in on_branch
    # The worktree is gone afterwards (the runner's own cleanup).
    assert not Path(worktree).exists()


def test_no_model_is_only_passed_when_declared(
    cfg: ForgeConfig, tmp_path: Path, fake_guardkit: Path
) -> None:
    _write(cfg, tmp_path, checks=[_normalize_check()])
    calls = normalize_calls(fake_guardkit)
    assert len(calls) == 1 and "--no-model" not in calls[0]


def test_a_blocking_refusal_by_the_normalizer_refuses_the_commit(
    cfg: ForgeConfig, tmp_path: Path, repo: Path, fake_guardkit: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rules refuse two titles where the law is enforced (blocking): no
    commit, the refusal's JSON comes back whole for the driver, feature
    validate is reported as not run — the closure returns before validate
    for the same reason."""
    monkeypatch.setenv("FAKE_GUARDKIT_NORMALIZE", "refused")
    status, body = _write(
        cfg, tmp_path, checks=[_normalize_check(no_model=True), _validate_check()]
    )
    assert status == 200, body
    assert body["status"] == "failed" and body["sha"] is None
    assert _no_commit_landed(repo)
    by_name = _by_name(body)
    norm = by_name["normalize-stamps"]
    assert norm["ran"] and not norm["passed"] and norm["exit_code"] == 2
    assert json.loads(norm["stdout"])["refused"] == list(REFUSED_TITLES)
    assert norm["detail"].startswith("stamp normalizer refused: 2 scenario(s) undecidable")
    assert "switched off for this stamping" in norm["detail"]
    assert body["detail"] == f"pre-commit oracle refused the commit: {norm['detail']}"
    validate = by_name["feature-validate"]
    assert not validate["ran"] and not validate["passed"]
    assert validate["detail"] == "not run: an earlier check refused the commit"
    assert validate_calls(fake_guardkit) == []


def test_a_partial_normalizer_blocks_too(
    cfg: ForgeConfig, tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch, fake_guardkit: Path
) -> None:
    monkeypatch.setenv("FAKE_GUARDKIT_NORMALIZE", "partial")
    status, body = _write(cfg, tmp_path, checks=[_normalize_check(), _validate_check()])
    assert body["status"] == "failed"
    norm = _by_name(body)["normalize-stamps"]
    assert norm["exit_code"] == 3 and not norm["passed"]
    assert norm["detail"].startswith("stamp normalizer partial:")
    assert _no_commit_landed(repo)


def test_an_unenforced_refusal_does_not_block_and_the_commit_lands(
    cfg: ForgeConfig, tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch, fake_guardkit: Path
) -> None:
    """The driver declares the normalizer NOT blocking where the routing law
    is not enforced: the refusal is reported (the driver receipts it and
    tells the owner in one line) and the plan is committed as before."""
    monkeypatch.setenv("FAKE_GUARDKIT_NORMALIZE", "partial")
    status, body = _write(
        cfg,
        tmp_path,
        checks=[{**_normalize_check(), "blocking": False}, _validate_check()],
    )
    assert status == 200 and body["status"] == "success"
    by_name = _by_name(body)
    assert not by_name["normalize-stamps"]["passed"]
    assert not by_name["normalize-stamps"]["blocking"]
    assert by_name["feature-validate"]["ran"] and by_name["feature-validate"]["passed"]
    # PARTIAL's decided stamps were written and ride the commit.
    assert 'verifier: "hurl"' in (git_show(repo, BRANCH, PLAN_YAML) or "")


def test_an_older_guardkit_with_no_such_verb_passes_through_and_continues(
    cfg: ForgeConfig, tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch, fake_guardkit: Path
) -> None:
    """``unavailable`` is not a failure in the driver's table (the run
    continues, receipted); the sidecar judges it the same way."""
    monkeypatch.setenv("FAKE_GUARDKIT_NORMALIZE", "unavailable")
    status, body = _write(cfg, tmp_path, checks=[_normalize_check(), _validate_check()])
    assert body["status"] == "success"
    norm = _by_name(body)["normalize-stamps"]
    assert norm["passed"] and norm["exit_code"] == 2
    assert norm["detail"].startswith("stamp normalizer unavailable:")
    assert "No such command 'normalize-stamps'" in norm["stderr_tail"]


def test_a_normalizer_that_cannot_run_is_a_failure(
    cfg: ForgeConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_guardkit: Path
) -> None:
    monkeypatch.setenv("FAKE_GUARDKIT_NORMALIZE", "failed")
    status, body = _write(cfg, tmp_path, checks=[_normalize_check(), _validate_check()])
    assert body["status"] == "failed"
    norm = _by_name(body)["normalize-stamps"]
    assert not norm["passed"]
    assert norm["detail"] == (
        "stamp normalizer failed: guardkit qa normalize-stamps could not run "
        "(exit 2): the plan YAML could not be read"
    )


def test_no_model_unknown_runs_again_the_old_way_and_says_so(
    cfg: ForgeConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_guardkit: Path
) -> None:
    """The installed guardkit has the verb but not the option: the sidecar
    does what forge's own collaborator does — one more run without it, and
    the note the receipt carries for that case, verbatim."""
    monkeypatch.setenv("FAKE_GUARDKIT_NORMALIZE", "no-model-unknown")
    status, body = _write(cfg, tmp_path, checks=[_normalize_check(no_model=True)])
    assert body["status"] == "success"
    norm = _by_name(body)["normalize-stamps"]
    assert norm["passed"] and norm["note"] == NO_MODEL_OPTION_UNKNOWN_NOTE
    calls = normalize_calls(fake_guardkit)
    assert len(calls) == 2
    assert "--no-model" in calls[0] and "--no-model" not in calls[1]


def test_a_red_feature_validate_refuses_the_commit_with_both_streams(
    cfg: ForgeConfig, tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch, fake_guardkit: Path
) -> None:
    monkeypatch.setenv("FAKE_GUARDKIT_VALIDATE", "red")
    status, body = _write(cfg, tmp_path, checks=[_normalize_check(), _validate_check()])
    assert body["status"] == "failed" and _no_commit_landed(repo)
    by_name = _by_name(body)
    assert by_name["normalize-stamps"]["passed"]
    validate = by_name["feature-validate"]
    assert validate["ran"] and not validate["passed"] and validate["exit_code"] == 1
    assert validate["detail"].startswith(
        f"guardkit feature validate failed (exit 1) for {FEATURE}: "
    )
    assert "TASK-STAT-001.md missing" in validate["detail"]
    assert body["detail"] == f"pre-commit oracle refused the commit: {validate['detail']}"


def test_feature_validate_repairs_a_truncated_task_id_first_and_notes_it(
    cfg: ForgeConfig, tmp_path: Path, repo: Path, fake_guardkit: Path
) -> None:
    """The closure's own pre-oracle repair (a task document whose front
    matter carries a prefix-truncated feature id) runs in the sidecar too,
    before validate, and its receipt rides the check's note."""
    truncated = FEATURE[:-1]
    files = {
        PLAN_YAML: f"id: {FEATURE}\ntasks:\n- id: TASK-STAT-001\n",
        "tasks/backlog/stats/TASK-STAT-001.md": (
            f"---\nid: TASK-STAT-001\nfeature_id: {truncated}\n---\n# task\n"
        ),
    }
    status, body = _write(cfg, tmp_path, checks=[_validate_check()], files=files)
    assert body["status"] == "success", body
    validate = _by_name(body)["feature-validate"]
    assert validate["passed"] and validate["note"]
    assert truncated in validate["note"] and FEATURE in validate["note"]
    doc = git_show(repo, BRANCH, "tasks/backlog/stats/TASK-STAT-001.md") or ""
    assert f"feature_id: {FEATURE}\n" in doc  # the repaired document is what landed


def test_classify_scenarios_reports_and_never_blocks(
    cfg: ForgeConfig, tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch, fake_guardkit: Path
) -> None:
    monkeypatch.setenv("FAKE_GUARDKIT_CLASSIFY", "refused")
    files = {**PLAN_FILES, "features/stats/stats.feature": "Feature: stats\n  Scenario: ok\n    Given a\n"}
    status, body = _write(
        cfg,
        tmp_path,
        checks=[
            {"name": "classify-scenarios", "args": {"feature_file": "features/stats/stats.feature"}},
            _validate_check(),
        ],
        files=files,
    )
    assert body["status"] == "success"
    classify = _by_name(body)["classify-scenarios"]
    assert classify["ran"] and classify["passed"] and not classify["blocking"]
    assert json.loads(classify["stdout"])["refused_titles"] == list(REFUSED_TITLES)
    assert classify["detail"] == "2 of 3 scenario(s) cannot be proven by rule"
    calls = classify_calls(fake_guardkit)
    assert len(calls) == 1
    assert calls[0][2:4] == ["--feature-file", calls[0][3]]
    assert calls[0][3].endswith("/features/stats/stats.feature")
    assert calls[0][3].startswith(str(tmp_path / "wt"))
    assert calls[0][-1] == "--json"


def test_a_classify_that_cannot_run_is_reported_and_still_never_blocks(
    cfg: ForgeConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_guardkit: Path
) -> None:
    monkeypatch.setenv("FAKE_GUARDKIT_CLASSIFY", "cannot")
    status, body = _write(
        cfg,
        tmp_path,
        checks=[{"name": "classify-scenarios", "args": {"feature_file": "x.feature"}}],
        files={**PLAN_FILES, "x.feature": "Feature: x\n"},
    )
    assert body["status"] == "success"
    classify = _by_name(body)["classify-scenarios"]
    assert not classify["passed"] and classify["exit_code"] == 2
    assert classify["detail"].startswith("guardkit qa classify-scenarios could not run:")


def test_a_check_that_runs_out_of_time_is_a_failure_that_blocks(
    cfg: ForgeConfig, tmp_path: Path, repo: Path, fake_guardkit: Path
) -> None:
    """The subprocess seam reports the timeout the way the merge does (exit
    124 and a sentence); the check is failed and, blocking, refuses the
    commit."""

    def slow(**kwargs: Any) -> tuple[int, str, str]:
        return (
            MERGE_TIMEOUT_EXIT_CODE,
            "",
            f"the merge command was stopped after {kwargs['timeout']:g} seconds",
        )

    status, body = process_git_write_tree_request(
        {
            "repo": REPO_KEY,
            "branch": BRANCH,
            "files": PLAN_FILES,
            "message": MESSAGE,
            "checks": [{**_normalize_check(), "timeout_seconds": 5}],
        },
        config=cfg,
        check_runner=slow,
        worktrees_root=tmp_path / "wt",
    )
    assert status == 200 and body["status"] == "failed"
    norm = _by_name(body)["normalize-stamps"]
    assert norm["timed_out"] and not norm["passed"] and norm["exit_code"] == 124
    assert norm["detail"] == (
        f"stamp normalizer failed: guardkit qa normalize-stamps timed out for {FEATURE}"
    )
    assert _no_commit_landed(repo)


def test_the_declared_time_limit_reaches_the_runner(
    cfg: ForgeConfig, tmp_path: Path, fake_guardkit: Path
) -> None:
    seen: list[float] = []

    def recording(**kwargs: Any) -> tuple[int, str, str]:
        seen.append(kwargs["timeout"])
        return 0, json.dumps({"valid": True}), ""

    process_git_write_tree_request(
        {
            "repo": REPO_KEY,
            "branch": BRANCH,
            "files": PLAN_FILES,
            "message": MESSAGE,
            "checks": [{**_validate_check(), "timeout_seconds": 42}, _validate_check()],
        },
        config=cfg,
        check_runner=recording,
        worktrees_root=tmp_path / "wt",
    )
    assert seen == [42.0, GIT_CHECK_TIMEOUT_DEFAULTS["feature-validate"]]


def test_a_check_runner_that_blows_up_is_a_failed_check_not_a_crash(
    cfg: ForgeConfig, tmp_path: Path, repo: Path, fake_guardkit: Path
) -> None:
    def exploding(**kwargs: Any) -> tuple[int, str, str]:
        raise RuntimeError("the runner fell over")

    status, body = process_git_write_tree_request(
        {
            "repo": REPO_KEY,
            "branch": BRANCH,
            "files": PLAN_FILES,
            "message": MESSAGE,
            "checks": [_validate_check()],
        },
        config=cfg,
        check_runner=exploding,
        worktrees_root=tmp_path / "wt",
    )
    assert status == 200 and body["status"] == "failed"
    validate = _by_name(body)["feature-validate"]
    assert validate["ran"] and not validate["passed"]
    assert validate["detail"] == (
        "the check could not be run: RuntimeError: the runner fell over"
    )
    assert _no_commit_landed(repo)


def test_an_escaping_file_path_never_reaches_the_worktree(
    cfg: ForgeConfig, tmp_path: Path, repo: Path
) -> None:
    status, body = _write(cfg, tmp_path, checks=[], files={"../../escape.md": "x"})
    assert status == 400
    assert not (tmp_path / "escape.md").exists()


# ---------------------------------------------------------------------------
# read-file-from-branch and rev-parse
# ---------------------------------------------------------------------------


def test_read_file_from_branch_returns_the_committed_content_or_null(
    cfg: ForgeConfig, tmp_path: Path, repo: Path
) -> None:
    _write(cfg, tmp_path, checks=[])
    status, body = process_git_read_file_request(
        {"repo": REPO_KEY, "branch": BRANCH, "file_path": PLAN_YAML},
        config=cfg,
        worktrees_root=tmp_path / "wt",
    )
    assert status == 200 and body == {"content": PLAN_FILES[PLAN_YAML]}
    status, body = process_git_read_file_request(
        {"repo": REPO_KEY, "branch": BRANCH, "file_path": "nowhere.md"}, config=cfg
    )
    assert status == 200 and body == {"content": None}
    status, body = process_git_read_file_request(
        {"repo": REPO_KEY, "branch": "no/such/branch", "file_path": PLAN_YAML}, config=cfg
    )
    assert status == 200 and body == {"content": None}


def test_read_file_refuses_a_bad_branch_or_path(cfg: ForgeConfig) -> None:
    status, body = process_git_read_file_request(
        {"repo": REPO_KEY, "branch": "--all", "file_path": PLAN_YAML}, config=cfg
    )
    assert status == 400 and "'branch'" in body["error"]
    status, body = process_git_read_file_request(
        {"repo": REPO_KEY, "branch": BRANCH, "file_path": "../x"}, config=cfg
    )
    assert status == 400 and "'file_path'" in body["error"]
    status, body = process_git_read_file_request(
        {"repo": "acme/ghost", "branch": BRANCH, "file_path": PLAN_YAML}, config=cfg
    )
    assert status == 400 and "unknown target repo" in body["error"]


def test_rev_parse_answers_the_commit_or_null(
    cfg: ForgeConfig, tmp_path: Path, repo: Path
) -> None:
    _write(cfg, tmp_path, checks=[])
    expected = git_rev_parse(repo, BRANCH)
    status, body = process_git_rev_parse_request({"repo": REPO_KEY, "ref": BRANCH}, config=cfg)
    assert status == 200 and body == {"sha": expected}
    status, body = process_git_rev_parse_request({"repo": REPO_KEY, "ref": "HEAD"}, config=cfg)
    assert status == 200 and body["sha"] == git_rev_parse(repo, "HEAD")
    status, body = process_git_rev_parse_request(
        {"repo": REPO_KEY, "ref": expected}, config=cfg
    )
    assert body == {"sha": expected}  # a full hash answers itself
    status, body = process_git_rev_parse_request(
        {"repo": REPO_KEY, "ref": "no-such-ref"}, config=cfg
    )
    assert status == 200 and body == {"sha": None}


def test_rev_parse_refuses_a_ref_git_could_misread(cfg: ForgeConfig) -> None:
    for bad in ("", "-x", "--output=/tmp/x", "a b", None):
        status, body = process_git_rev_parse_request({"repo": REPO_KEY, "ref": bad}, config=cfg)
        assert status == 400 and "'ref'" in body["error"]


# ---------------------------------------------------------------------------
# The command resolution
# ---------------------------------------------------------------------------


def test_the_check_command_walks_the_setting_then_path_then_the_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = write_fake_guardkit(tmp_path / "bin")
    monkeypatch.setenv(GUARDKIT_PATH_ENV, str(binary))
    assert resolve_check_command() == (str(binary),)
    monkeypatch.delenv(GUARDKIT_PATH_ENV)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert resolve_check_command() == (str(binary),)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert resolve_check_command(find_spec=lambda name: object(), python_executable="/py") == (
        "/py",
        "-m",
        "guardkit.cli.main",
    )
    assert resolve_check_command(find_spec=lambda name: None) is None

    def raising(name: str) -> object:
        raise ModuleNotFoundError(name)

    assert resolve_check_command(find_spec=raising) is None


# ---------------------------------------------------------------------------
# End to end over loopback — a real server, a real socket
# ---------------------------------------------------------------------------


def _serve_in_thread(server: Any) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def _post(url: str, body: dict[str, Any], *, timeout: float = 60.0) -> tuple[int, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


@pytest.fixture
def server(cfg: ForgeConfig, tmp_path: Path):
    srv = build_server(port=0, config_loader=lambda: cfg, worktrees_root=tmp_path / "wt")
    _serve_in_thread(srv)
    host, port = srv.server_address[:2]
    assert host == "127.0.0.1"
    try:
        yield f"http://{host}:{port}"
    finally:
        srv.shutdown()
        srv.server_close()


def test_end_to_end_over_loopback_write_read_and_rev_parse(
    server: str, repo: Path, fake_guardkit: Path
) -> None:
    status, body = _post(
        server + GIT_WRITE_TREE_ROUTE,
        {
            "repo": REPO_KEY,
            "branch": BRANCH,
            "files": PLAN_FILES,
            "message": MESSAGE,
            "checks": [_normalize_check(no_model=True), _validate_check()],
        },
    )
    assert status == 200, body
    assert body["status"] == "success" and body["sha"] == git_rev_parse(repo, BRANCH)
    assert [c["name"] for c in body["checks"]] == ["normalize-stamps", "feature-validate"]
    status, body = _post(
        server + GIT_READ_FILE_ROUTE,
        {"repo": REPO_KEY, "branch": BRANCH, "file_path": PLAN_YAML},
    )
    assert status == 200 and 'verifier: "hurl"' in body["content"]
    status, body = _post(server + GIT_REV_PARSE_ROUTE, {"repo": REPO_KEY, "ref": BRANCH})
    assert status == 200 and body["sha"] == git_rev_parse(repo, BRANCH)


def test_end_to_end_refusal_is_http_400_with_one_sentence(server: str) -> None:
    status, body = _post(
        server + GIT_WRITE_TREE_ROUTE,
        {"repo": REPO_KEY, "branch": "-x", "files": PLAN_FILES, "message": MESSAGE},
    )
    assert status == 400 and "'branch'" in body["error"]


def test_a_refused_commit_over_loopback_is_data_not_an_http_error(
    server: str, repo: Path, monkeypatch: pytest.MonkeyPatch, fake_guardkit: Path
) -> None:
    monkeypatch.setenv("FAKE_GUARDKIT_NORMALIZE", "refused")
    status, body = _post(
        server + GIT_WRITE_TREE_ROUTE,
        {
            "repo": REPO_KEY,
            "branch": BRANCH,
            "files": PLAN_FILES,
            "message": MESSAGE,
            "checks": [_normalize_check(no_model=True), _validate_check()],
        },
    )
    assert status == 200 and body["status"] == "failed" and body["sha"] is None
    assert json.loads(_by_name(body)["normalize-stamps"]["stdout"])["refused"] == list(
        REFUSED_TITLES
    )
    assert _no_commit_landed(repo)


def test_the_other_operations_still_work_beside_the_git_routes(server: str) -> None:
    status, body = _post(server + "/guardkit-merge", {"repo": "acme/ghost"})
    assert status == 400 and "unknown target repo" in body["error"]
    status, body = _post(server + "/git/no-such-op", {})
    assert status == 404


# ---------------------------------------------------------------------------
# guardkit's real verbs, when a checkout that carries them is reachable
# ---------------------------------------------------------------------------


def _live_guardkit_wrapper(tmp_path: Path) -> Path | None:
    """A runnable ``guardkit`` that starts the live checkout's CLI, or None."""
    checkout = live_guardkit_checkout(Path(__file__))
    if checkout is None:
        return None
    python = live_guardkit_python(checkout, Path(__file__))
    ok, _ = live_cli_importable(checkout, python)
    if not ok:
        return None
    wrapper = tmp_path / "live-bin" / "guardkit"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text(
        "#!/bin/sh\n"
        f'PYTHONPATH="{checkout}" exec "{python}" -m guardkit.cli.main "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return wrapper


def test_live_guardkit_classify_and_normalize_through_the_route(
    cfg: ForgeConfig, tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real ``qa classify-scenarios`` and ``qa normalize-stamps --no-model``
    (rules only, seconds) run by the sidecar in its worktree of a scratch
    repository: an endpoint example gets its home by rule, and the stamp
    the normalizer writes rides the commit."""
    wrapper = _live_guardkit_wrapper(tmp_path)
    if wrapper is None:
        pytest.skip("no live guardkit checkout with the qa verbs is reachable")
    monkeypatch.setenv(GUARDKIT_PATH_ENV, str(wrapper))
    feature_rel = "features/stats/stats.feature"
    feature_text = (
        "Feature: stats\n"
        "  Scenario: Reading the current server time\n"
        "    When the client sends GET /time\n"
        "    Then the response status is 200\n"
    )
    files = {
        PLAN_YAML: (
            f"id: {FEATURE}\ntasks:\n- id: TASK-STAT-001\n"
            f"feature_files:\n  - {feature_rel}\n"
        ),
        feature_rel: feature_text,
        "pyproject.toml": (
            '[project]\nname = "api"\nversion = "0.0.0"\n'
            'dependencies = ["fastapi"]\n'
        ),
    }
    status, body = _write(
        cfg,
        tmp_path,
        files=files,
        checks=[
            {"name": "classify-scenarios", "args": {"feature_file": feature_rel}},
            _normalize_check(no_model=True),
        ],
    )
    assert status == 200, body
    by_name = _by_name(body)
    classify = json.loads(by_name["classify-scenarios"]["stdout"])
    assert classify["refused_titles"] == []
    assert classify["scenarios"][0]["title"] == "Reading the current server time"
    assert classify["scenarios"][0]["home"] == "hurl"
    normalize = by_name["normalize-stamps"]
    assert normalize["passed"], normalize
    written = json.loads(normalize["stdout"])
    assert written["stamped"] == {"Reading the current server time": "hurl"}
    assert written["refused"] == []
    # A guardkit that reports the fallback's own outcome says it was switched
    # off; an older one says nothing, and nothing is invented for it.
    if written.get("model_outcome"):
        assert written["model_outcome"]["status"] == "switched_off"
    assert body["status"] == "success"
    assert "Reading the current server time" in (git_show(repo, BRANCH, PLAN_YAML) or "")

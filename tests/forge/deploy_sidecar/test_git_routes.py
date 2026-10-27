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
    GIT_WRITE_TREE_ROUTE,
    GUARDKIT_PATH_ENV,
    build_server,
    process_git_read_file_request,
    process_git_list_files_on_branch_request,
    process_git_rev_parse_request,
    process_git_write_tree_request,
    resolve_check_command,
    resolve_normalizer_command_for_checks,
)

from tests.forge.deploy_sidecar._fake_guardkit import (
    NORMALIZED_MARKER,
    fake_normalizer,
    git_rev_parse,
    git_show,
    normalizer_calls,
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
    # THE PROJECT DECLARES WHAT ITS OWN BUILDS NEED, and the requests below
    # present those names. The helper permits a name on a request only when
    # the project's own committed declaration lists it (23 September 2026), so
    # the declaration is written here rather than the request being taken on
    # its own say-so.
    return scratch_repo(tmp_path / "api_test", declares=STAND_IN_SETTINGS)


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


@pytest.fixture
def fake_normalizer_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The stand-in gherkin normalizer, planted where BOTH the sidecar's own
    ``find_spec`` probe and the subprocess it starts resolve it."""
    yield from fake_normalizer(tmp_path / "normalizer", monkeypatch)


FEATURE_REL = "features/stats/stats.feature"
FEATURE_TEXT = "Feature: stats\n  Scenario: ok\n    Given a thing\n"
SPEC_FILES = {
    FEATURE_REL: FEATURE_TEXT,
    "features/stats/stats_summary.md": "# summary\n",
}

def _normalize_feature_check(rel: str = FEATURE_REL) -> dict[str, Any]:
    return {"name": "normalize-feature", "args": {"feature_file": rel}}


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
        # WHAT THE STAND-INS NEED TO BE TOLD, declared BY NAME (22 September
        # 2026). A check is launched with the factory's own short named list
        # and nothing else, so the settings this test steers its stand-in
        # guardkit and stand-in normalizer with do not travel unless they are
        # named — which is exactly the door a project uses to say what its own
        # builds and checks need. Naming them here is how the test says it.
        "launch_settings": list(STAND_IN_SETTINGS),
        # AND WHO IS ASKING (23 September 2026). A request that asks for a
        # project's declarations to be read carries the build it is for, which
        # the coordinator stamps on it — or it says it is being run by hand, at
        # this copy's committed HEAD. These requests are made by hand, from a
        # test, with no coordinator anywhere, so they say so.
        "by_hand": True,
        **overrides,
    }
    return process_git_write_tree_request(
        payload, config=cfg, worktrees_root=tmp_path / "wt"
    )


#: The settings the stand-ins in this module are steered with. They are not on
#: the factory's own list — nothing a test invents ever should be — so the
#: requests below declare them by name, the way a project declares what its own
#: builds need. ``PYTHONPATH`` is here because the stand-in normalizer is a
#: planted module rather than an installed one; in production that module comes
#: out of the interpreter's own installation and needs no such setting.
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


def _by_name(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["name"]: c for c in body["checks"]}


def _no_commit_landed(repo: Path, branch: str = BRANCH) -> bool:
    """True when no commit was made on ``branch``: the branch is absent, or
    it still points at the base commit — the runner's ``git worktree add
    -b`` creates it there before the checks run, and a refused commit leaves
    it there (zero branch mutation)."""
    return git_rev_parse(repo, branch) in (None, git_rev_parse(repo, "HEAD"))


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


def test_checks_must_be_a_list_of_objects(cfg: ForgeConfig, tmp_path: Path) -> None:
    status, body = _write(cfg, tmp_path, checks={"name": "feature-validate"})
    assert status == 400 and "'checks' must be a list" in body["error"]
    status, body = _write(cfg, tmp_path, checks=["feature-validate"])
    assert status == 400 and "checks[0] must be an object" in body["error"]


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


def test_an_escaping_file_path_never_reaches_the_worktree(
    cfg: ForgeConfig, tmp_path: Path, repo: Path
) -> None:
    status, body = _write(cfg, tmp_path, checks=[], files={"../../escape.md": "x"})
    assert status == 400
    assert not (tmp_path / "escape.md").exists()


# ---------------------------------------------------------------------------
# The three checks the other planning legs declare (rule 87, 2026-09-07)
# ---------------------------------------------------------------------------


def test_the_gherkin_normalizer_runs_over_the_committed_feature_file(
    cfg: ForgeConfig,
    tmp_path: Path,
    repo: Path,
    fake_normalizer_module: Path,
) -> None:
    """The spec leg's check: the normalizer module is run against the file in
    the sidecar's OWN worktree, what it rewrites there rides the commit, and
    it needs no guardkit command at all."""
    status, body = _write(
        cfg, tmp_path, checks=[_normalize_feature_check()], files=SPEC_FILES
    )
    assert status == 200, body
    assert body["status"] == "success"
    check = _by_name(body)["normalize-feature"]
    assert check["ran"] and check["passed"] and check["blocking"]
    committed = git_show(repo, BRANCH, FEATURE_REL) or ""
    assert committed == FEATURE_TEXT + NORMALIZED_MARKER


def test_a_red_normalizer_refuses_the_commit_with_the_legs_own_sentence(
    cfg: ForgeConfig,
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_normalizer_module: Path,
) -> None:
    """A spec the normalizer cannot parse never reaches the branch, and the
    sentence the leg reads is word for word the closure's."""
    monkeypatch.setenv("FAKE_NORMALIZER", "red")
    status, body = _write(
        cfg, tmp_path, checks=[_normalize_feature_check()], files=SPEC_FILES
    )
    assert status == 200 and body["status"] == "failed" and body["sha"] is None
    check = _by_name(body)["normalize-feature"]
    assert not check["passed"] and check["exit_code"] == 1
    assert check["detail"].startswith(f"normalizer exit 1 for {FEATURE_REL}: ")
    assert "expected a step keyword" in check["detail"]
    assert body["detail"].endswith(check["detail"])
    assert _no_commit_landed(repo)


def test_the_normalizer_runs_where_the_files_are_and_says_which_file(
    cfg: ForgeConfig, tmp_path: Path, fake_guardkit: Path, fake_normalizer_module: Path
) -> None:
    """One fixed argument list: the module, then the absolute path of the
    file inside the sidecar's worktree — never the operator's checkout."""
    _write(cfg, tmp_path, checks=[_normalize_feature_check()], files=SPEC_FILES)
    calls = normalizer_calls(fake_guardkit)
    assert len(calls) == 1
    assert calls[0][0].startswith(str(tmp_path / "wt"))
    assert calls[0][0].endswith("/" + FEATURE_REL)


def test_without_the_normalizer_module_the_request_is_a_plain_500(
    cfg: ForgeConfig, tmp_path: Path, repo: Path
) -> None:
    """A sidecar whose interpreter cannot import guardkit's normalizer says
    so in one sentence, before any worktree is made — it never skips the
    check quietly."""
    status, body = process_git_write_tree_request(
        {
            "repo": REPO_KEY,
            "branch": BRANCH,
            "files": SPEC_FILES,
            "message": MESSAGE,
            "checks": [_normalize_feature_check()],
        },
        config=cfg,
        normalizer_resolver=lambda: None,
        worktrees_root=tmp_path / "wt",
    )
    assert status == 500
    assert "gherkin normalizer module" in body["error"]
    assert _no_commit_landed(repo)


def test_the_normalizer_check_needs_no_guardkit_command(
    cfg: ForgeConfig,
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_normalizer_module: Path,
) -> None:
    """It is a module, not a guardkit verb: a sidecar with no guardkit on it
    still runs the spec leg's check."""
    monkeypatch.delenv(GUARDKIT_PATH_ENV, raising=False)
    status, body = process_git_write_tree_request(
        {
            "repo": REPO_KEY,
            "branch": BRANCH,
            "files": SPEC_FILES,
            "message": MESSAGE,
            "checks": [_normalize_feature_check()],
            "launch_settings": list(STAND_IN_SETTINGS),
            # Made by hand, from a test: no build, and it says so.
            "by_hand": True,
        },
        config=cfg,
        command_resolver=lambda: None,
        worktrees_root=tmp_path / "wt",
    )
    assert status == 200 and body["status"] == "success"


def test_the_normalizer_command_is_resolved_the_way_the_spec_leg_resolves_it(
    fake_normalizer_module: Path,
) -> None:
    command = resolve_normalizer_command_for_checks()
    assert command is not None
    assert command[1] == "-m"
    assert command[2].endswith("feature_spec_normalize")


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
    # A dash cannot sneak in after the caret either, and the caret suffix is
    # one suffix, not a chain.
    for bad in ("main^-1", "main^{-x}", "main^1^2", "main^0x", "main^"):
        status, body = process_git_rev_parse_request({"repo": REPO_KEY, "ref": bad}, config=cfg)
        assert status == 400, bad


def test_rev_parse_answers_a_commit_s_numbered_parents(
    cfg: ForgeConfig, tmp_path: Path, repo: Path
) -> None:
    """``<ref>^1`` and ``<ref>^2``, which is how a merge is recognised.

    The merge word settles an interrupted join by asking whether that
    attempt's branch is a merge of exactly two named commits — its first and
    second parents. This route refused both forms, so in a sandbox the answer
    was always "it is not a merge commit": a sandboxed repository would set
    its real join aside and join afresh, for ever. The peel form
    (``^{tree}``) already worked and still does.
    """
    _write(cfg, tmp_path, checks=[])

    def _run(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.email=t@example.invalid",
             "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
            check=True, capture_output=True,
        )

    first = git_rev_parse(repo, "HEAD")
    _run("checkout", "-q", "-b", "the-other-hand", first)
    (repo / "other.txt").write_text("the other hand\n", encoding="utf-8")
    _run("add", "other.txt")
    _run("commit", "-q", "-m", "the other hand")
    second = git_rev_parse(repo, "the-other-hand")
    _run("checkout", "-q", "-b", "factory-integration-like", first)
    _run("merge", "--no-ff", "-q", "-m", "the join", "the-other-hand")
    joined = git_rev_parse(repo, "factory-integration-like")

    for ref, expected in (
        ("factory-integration-like", joined),
        ("factory-integration-like^1", first),
        ("factory-integration-like^2", second),
    ):
        status, body = process_git_rev_parse_request(
            {"repo": REPO_KEY, "ref": ref}, config=cfg
        )
        assert status == 200, (ref, body)
        assert body["sha"] == expected, ref
    # A third parent answers nothing, which is how "exactly two" is proved.
    status, body = process_git_rev_parse_request(
        {"repo": REPO_KEY, "ref": "factory-integration-like^3"}, config=cfg
    )
    assert status == 200 and body == {"sha": None}


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


def test_end_to_end_refusal_is_http_400_with_one_sentence(server: str) -> None:
    status, body = _post(
        server + GIT_WRITE_TREE_ROUTE,
        {"repo": REPO_KEY, "branch": "-x", "files": PLAN_FILES, "message": MESSAGE},
    )
    assert status == 400 and "'branch'" in body["error"]


def test_the_other_operations_still_work_beside_the_git_routes(server: str) -> None:
    status, body = _post(server + "/guardkit-merge", {"repo": "acme/ghost"})
    assert status == 400 and "unknown target repo" in body["error"]
    status, body = _post(server + "/git/no-such-op", {})
    assert status == 404


# ---------------------------------------------------------------------------
# guardkit's real verbs, when a checkout that carries them is reachable
# ---------------------------------------------------------------------------


def test_list_files_on_branch_reads_that_branch_not_the_checked_out_tree(
    cfg: ForgeConfig, repo: Path
) -> None:
    """3 October 2026: a repair's base can be a feature's autobuild branch,
    whose task files the clone's checked-out tree does not have."""

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    start = git_rev_parse(repo, "HEAD")
    git("checkout", "-q", "-b", "autobuild/FEAT-X")
    (repo / "tasks" / "backlog" / "feat-x").mkdir(parents=True)
    (repo / "tasks" / "backlog" / "feat-x" / "TASK-X-001-a.md").write_text("a\n")
    git("add", "tasks")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "tasks")
    git("checkout", "-q", start)

    def ask(branch: Any, under: Any = "tasks") -> tuple[int, dict[str, Any]]:
        return process_git_list_files_on_branch_request(
            {"repo": REPO_KEY, "branch": branch, "under": under}, config=cfg
        )

    assert ask("autobuild/FEAT-X") == (
        200,
        {"files": ["tasks/backlog/feat-x/TASK-X-001-a.md"]},
    )
    assert not (repo / "tasks").exists()  # nothing was checked out or written
    assert ask(start) == (200, {"files": []})
    assert ask("no-such-branch") == (200, {"files": None})
    for bad_branch in ("-x", "a b", None):
        assert ask(bad_branch)[0] == 400
    for bad_under in ("/etc", "../x", "", None):
        assert ask("autobuild/FEAT-X", bad_under)[0] == 400



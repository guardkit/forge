"""The sidecar's worktree and receipts routes — a fix journey's tree cut, and
its receipts copied, where the repository lives (sandbox first, 2026-09-07,
rules 76 and 77).

Real code paths throughout: a real git repository in a temporary directory,
the real conductor worktree module (imported by the sidecar, so the two sides
cannot drift apart), the real receipts exporter writing under a receipts root
named by ``FORGE_RECEIPTS_DIR``, and — for the end-to-end cases — a real HTTP
server on an ephemeral loopback port. Nothing live is touched: every path is
under ``tmp_path``.

What is pinned:

* a tree cut for real on the named branch off the named base, and a second
  call for the same build recognised as its own tree rather than a collision;
* the routes act on the given repository's journey-tree path and refuse every
  other path, including one under a different repository and one that tries
  to climb out with ``..``;
* removing a tree, twice, without complaint;
* counting a journey's commits in the tree, where the tree actually is: none
  on a fresh tree, three after three commits, and the same refusals;
* receipts copied out of a real worktree into the resolved receipts root,
  with the paths written named in the answer;
* the refusals: an unknown repository, a branch git would read as an option,
  a worktree outside every known repository, a bad ``extra_files`` name;
* the other operations still answer beside the new routes, and an unknown
  path is still a 404.
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
    GIT_WORKTREE_ADD_ROUTE,
    GIT_WORKTREE_COMMIT_COUNT_ROUTE,
    GIT_WORKTREE_REMOVE_ROUTE,
    RECEIPTS_EXPORT_ROUTE,
    build_server,
    process_git_worktree_add_request,
    process_git_worktree_commit_count_request,
    process_git_worktree_remove_request,
    process_receipts_export_request,
)

REPO_KEY = "guardkit/api_test"
OTHER_KEY = "guardkit/other"
BUILD_ID = "build-FEAT-WT01-20260907170000"
BRANCH = "fix/TASK-WT-001-07170000"

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "PATH": "/usr/bin:/bin:/usr/local/bin",
    "HOME": "/nonexistent",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 — scratch fixture, list tokens, no shell
        ["git", *args],
        cwd=repo,
        check=True,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway checkout standing in for the factory's clone: one commit
    on ``main`` and a second branch a repair would have been queued on."""
    path = tmp_path / "api_test"
    path.mkdir()
    _git(path, "init", "-b", "main")
    (path / "README").write_text("scratch\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "init")
    _git(path, "branch", "repair/TASK-WT-001")
    return path.resolve()


@pytest.fixture
def other_repo(tmp_path: Path) -> Path:
    path = tmp_path / "other"
    path.mkdir()
    _git(path, "init", "-b", "main")
    (path / "README").write_text("other\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "init")
    return path.resolve()


@pytest.fixture
def cfg(repo: Path, other_repo: Path) -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {
                "target_repo_paths": {
                    REPO_KEY: str(repo),
                    OTHER_KEY: str(other_repo),
                }
            },
        }
    )


@pytest.fixture
def receipts_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "receipts"
    root.mkdir()
    monkeypatch.setenv("FORGE_RECEIPTS_DIR", str(root))
    return root


def _tree(repo: Path, build_id: str = BUILD_ID) -> str:
    return str(repo / ".forge" / "worktrees" / build_id)


def _cut(cfg: ForgeConfig, repo: Path, *, base: str = "main") -> Path:
    """Cut this build's journey tree for real and hand back its path."""
    status, body = process_git_worktree_add_request(
        {
            "repo": REPO_KEY,
            "path": _tree(repo),
            "branch": BRANCH,
            "base_ref": base,
        },
        config=cfg,
    )
    assert status == 200 and body["status"] == "success", body
    return Path(body["path"])


def _commit(tree: Path, name: str) -> None:
    (tree / name).write_text(name, encoding="utf-8")
    _git(tree, "add", "-A")
    _git(tree, "commit", "-m", f"add {name}")


# ---------------------------------------------------------------------------
# /git/worktree-add
# ---------------------------------------------------------------------------


class TestTheWorktreeAddRoute:
    def test_a_tree_is_cut_on_the_named_branch_off_the_named_base(
        self, cfg: ForgeConfig, repo: Path
    ) -> None:
        status, body = process_git_worktree_add_request(
            {
                "repo": REPO_KEY,
                "path": _tree(repo),
                "branch": BRANCH,
                "base_ref": "repair/TASK-WT-001",
            },
            config=cfg,
        )
        assert status == 200, body
        assert body["status"] == "success", body["detail"]
        assert body["reused"] is False
        tree = Path(body["path"])
        assert tree.is_dir() and (tree / ".git").exists()
        assert (
            _git(tree, "rev-parse", "--abbrev-ref", "HEAD").strip() == BRANCH
        )
        assert (repo / ".forge" / ".gitignore").read_text() == "*\n"

    def test_the_same_build_asking_twice_reuses_its_own_tree(
        self, cfg: ForgeConfig, repo: Path
    ) -> None:
        payload = {"repo": REPO_KEY, "path": _tree(repo), "branch": BRANCH}
        first = process_git_worktree_add_request(payload, config=cfg)[1]
        second = process_git_worktree_add_request(payload, config=cfg)[1]
        assert first["reused"] is False and second["reused"] is True
        assert second["status"] == "success" and second["path"] == first["path"]

    def test_a_base_branch_nobody_made_is_a_failed_answer_not_a_crash(
        self, cfg: ForgeConfig, repo: Path
    ) -> None:
        status, body = process_git_worktree_add_request(
            {
                "repo": REPO_KEY,
                "path": _tree(repo),
                "branch": BRANCH,
                "base_ref": "repair/never-made",
            },
            config=cfg,
        )
        assert status == 200
        assert body["status"] == "failed"
        assert "does not exist" in body["detail"]
        assert not Path(_tree(repo)).exists()

    @pytest.mark.parametrize(
        "path_of",
        [
            lambda repo, other: str(other / ".forge" / "worktrees" / BUILD_ID),
            lambda repo, other: str(repo / ".forge" / "worktrees" / BUILD_ID / "in"),
            lambda repo, other: str(repo / ".forge" / BUILD_ID),
            lambda repo, other: str(repo / ".forge" / "worktrees" / ".." / "x"),
            lambda repo, other: str(repo),
        ],
    )
    def test_only_this_repositorys_own_journey_tree_path_is_acted_on(
        self, cfg: ForgeConfig, repo: Path, other_repo: Path, path_of: Any
    ) -> None:
        status, body = process_git_worktree_add_request(
            {"repo": REPO_KEY, "path": path_of(repo, other_repo), "branch": BRANCH},
            config=cfg,
        )
        assert status == 400
        assert "is not a journey worktree of this repository" in body["error"]
        assert "and on no other path" in body["error"]

    def test_an_unknown_repository_is_refused_by_name(
        self, cfg: ForgeConfig, repo: Path
    ) -> None:
        status, body = process_git_worktree_add_request(
            {"repo": "acme/ghost", "path": _tree(repo), "branch": BRANCH},
            config=cfg,
        )
        assert status == 400 and "unknown target repo" in body["error"]

    def test_a_branch_git_would_read_as_an_option_is_refused(
        self, cfg: ForgeConfig, repo: Path
    ) -> None:
        status, body = process_git_worktree_add_request(
            {"repo": REPO_KEY, "path": _tree(repo), "branch": "--force"},
            config=cfg,
        )
        assert status == 400 and "'branch'" in body["error"]

    def test_a_body_that_is_not_an_object_is_refused(self, cfg: ForgeConfig) -> None:
        status, body = process_git_worktree_add_request(["nope"], config=cfg)
        assert status == 400 and "JSON object" in body["error"]


# ---------------------------------------------------------------------------
# /git/worktree-remove
# ---------------------------------------------------------------------------


class TestTheWorktreeRemoveRoute:
    def test_a_tree_is_removed_and_removing_it_again_is_still_a_success(
        self, cfg: ForgeConfig, repo: Path
    ) -> None:
        process_git_worktree_add_request(
            {"repo": REPO_KEY, "path": _tree(repo), "branch": BRANCH}, config=cfg
        )
        assert Path(_tree(repo)).is_dir()
        status, body = process_git_worktree_remove_request(
            {"repo": REPO_KEY, "path": _tree(repo)}, config=cfg
        )
        assert status == 200 and body["status"] == "success", body
        assert not Path(_tree(repo)).exists()
        status, body = process_git_worktree_remove_request(
            {"repo": REPO_KEY, "path": _tree(repo)}, config=cfg
        )
        assert status == 200 and body["status"] == "success"

    def test_it_refuses_a_path_that_is_not_a_journey_tree(
        self, cfg: ForgeConfig, repo: Path
    ) -> None:
        status, body = process_git_worktree_remove_request(
            {"repo": REPO_KEY, "path": str(repo)}, config=cfg
        )
        assert status == 400
        assert "is not a journey worktree of this repository" in body["error"]
        assert repo.is_dir()


# ---------------------------------------------------------------------------
# /git/worktree-commit-count
# ---------------------------------------------------------------------------


class TestTheWorktreeCommitCountRoute:
    """The fix journey's "did this build change anything?", asked where the
    journey worktree actually is (the thirteenth seam, 2026-09-08)."""

    def test_a_fresh_tree_has_no_commits_and_says_where_its_head_is(
        self, cfg: ForgeConfig, repo: Path
    ) -> None:
        tree = _cut(cfg, repo)

        status, body = process_git_worktree_commit_count_request(
            {"repo": REPO_KEY, "path": str(tree), "base": "main"}, config=cfg
        )

        assert status == 200, body
        assert body["count"] == 0
        assert body["head"] == _git(repo, "rev-parse", "main").strip()

    def test_three_commits_are_counted(self, cfg: ForgeConfig, repo: Path) -> None:
        tree = _cut(cfg, repo)
        for name in ("one", "two", "three"):
            _commit(tree, name)

        status, body = process_git_worktree_commit_count_request(
            {"repo": REPO_KEY, "path": str(tree), "base": "main"}, config=cfg
        )

        assert status == 200, body
        assert body["count"] == 3
        assert body["head"] == _git(tree, "rev-parse", "HEAD").strip()

    def test_the_base_is_the_branch_the_journey_was_cut_from(
        self, cfg: ForgeConfig, repo: Path
    ) -> None:
        # A repair queued on its own branch counts from that branch, so the
        # branch's own commits are never read as a leg's work.
        tree = _cut(cfg, repo, base="repair/TASK-WT-001")
        _commit(tree, "the-fix")

        status, body = process_git_worktree_commit_count_request(
            {"repo": REPO_KEY, "path": str(tree), "base": "repair/TASK-WT-001"},
            config=cfg,
        )

        assert status == 200 and body["count"] == 1, body

    def test_only_this_repositorys_own_journey_tree_path_is_counted_in(
        self, cfg: ForgeConfig, repo: Path, other_repo: Path
    ) -> None:
        for path in (
            "/etc",
            str(repo),
            str(other_repo / ".forge" / "worktrees" / BUILD_ID),
            str(repo / ".forge" / "worktrees" / ".." / ".." / "elsewhere"),
        ):
            status, body = process_git_worktree_commit_count_request(
                {"repo": REPO_KEY, "path": path, "base": "main"}, config=cfg
            )
            assert status == 400, (path, body)
            assert "and on no other path" in body["error"]

    def test_an_unknown_repository_is_refused_by_name(
        self, cfg: ForgeConfig, repo: Path
    ) -> None:
        status, body = process_git_worktree_commit_count_request(
            {"repo": "acme/ghost", "path": _tree(repo), "base": "main"}, config=cfg
        )

        assert status == 400 and "unknown target repo" in body["error"]

    def test_a_base_git_would_read_as_an_option_is_refused(
        self, cfg: ForgeConfig, repo: Path
    ) -> None:
        tree = _cut(cfg, repo)

        for base in ("--all", "main..HEAD", "", None):
            status, body = process_git_worktree_commit_count_request(
                {"repo": REPO_KEY, "path": str(tree), "base": base}, config=cfg
            )
            assert status == 400, (base, body)
            assert "'base'" in body["error"]

    def test_a_tree_that_is_not_there_is_refused_before_git_runs(
        self, cfg: ForgeConfig, repo: Path
    ) -> None:
        status, body = process_git_worktree_commit_count_request(
            {"repo": REPO_KEY, "path": _tree(repo), "base": "main"}, config=cfg
        )

        assert status == 400 and "is not there" in body["error"]

    def test_a_base_nobody_made_is_a_loud_failure_never_a_quiet_zero(
        self, cfg: ForgeConfig, repo: Path
    ) -> None:
        tree = _cut(cfg, repo)

        status, body = process_git_worktree_commit_count_request(
            {"repo": REPO_KEY, "path": str(tree), "base": "no-such-branch"},
            config=cfg,
        )

        assert status == 500, body
        assert "could not count" in body["error"]
        assert "count" not in body

    def test_a_body_that_is_not_an_object_is_refused(self, cfg: ForgeConfig) -> None:
        status, body = process_git_worktree_commit_count_request("nope", config=cfg)

        assert status == 400 and "JSON object" in body["error"]


# ---------------------------------------------------------------------------
# /receipts/export
# ---------------------------------------------------------------------------


def _worktree_with_receipts(cfg: ForgeConfig, repo: Path) -> Path:
    """A real journey tree carrying one receipt family."""
    process_git_worktree_add_request(
        {"repo": REPO_KEY, "path": _tree(repo), "branch": BRANCH}, config=cfg
    )
    tree = Path(_tree(repo))
    family = tree / ".guardkit" / "autobuild"
    family.mkdir(parents=True)
    (family / "review.json").write_text('{"verdict": "approve"}', encoding="utf-8")
    return tree


class TestTheReceiptsExportRoute:
    def test_the_receipts_land_under_the_resolved_receipts_root(
        self, cfg: ForgeConfig, repo: Path, receipts_root: Path
    ) -> None:
        tree = _worktree_with_receipts(cfg, repo)
        status, body = process_receipts_export_request(
            {
                "build_id": BUILD_ID,
                "stage": "task-review",
                "worktree": str(tree),
                "extra_files": {"turn-rationale.txt": "the review leg ran"},
            },
            config=cfg,
        )
        assert status == 200 and body["status"] == "success", body
        assert body["stage_key"] == "001-task-review"
        dest = Path(body["dest"])
        assert dest == receipts_root / BUILD_ID / "stages" / "001-task-review"
        assert (dest / ".guardkit" / "autobuild" / "review.json").is_file()
        assert (dest / "turn-rationale.txt").read_text() == "the review leg ran"
        assert body["families"] == [".guardkit/autobuild"]
        assert str(dest / "turn-rationale.txt") in body["files"]

    def test_a_second_export_of_the_same_stage_gets_its_own_number(
        self, cfg: ForgeConfig, repo: Path, receipts_root: Path
    ) -> None:
        tree = _worktree_with_receipts(cfg, repo)
        payload = {
            "build_id": BUILD_ID,
            "stage": "task-work",
            "worktree": str(tree),
        }
        first = process_receipts_export_request(payload, config=cfg)[1]
        second = process_receipts_export_request(payload, config=cfg)[1]
        assert first["stage_key"] == "001-task-work"
        assert second["stage_key"] == "002-task-work"

    def test_a_worktree_outside_every_known_repository_is_refused(
        self, cfg: ForgeConfig, tmp_path: Path, receipts_root: Path
    ) -> None:
        stranger = tmp_path / "somewhere-else"
        stranger.mkdir()
        status, body = process_receipts_export_request(
            {"build_id": BUILD_ID, "stage": "task-work", "worktree": str(stranger)},
            config=cfg,
        )
        assert status == 400
        assert "is not inside any repository this sidecar knows" in body["error"]

    @pytest.mark.parametrize(
        "payload, wanted",
        [
            ({"stage": "task-work", "worktree": "/x"}, "'build_id' is required"),
            (
                {"build_id": BUILD_ID, "worktree": "/x"},
                "'stage' is required",
            ),
            (
                {"build_id": BUILD_ID, "stage": "task-work"},
                "'worktree' is required",
            ),
            (
                {"build_id": "../escape", "stage": "s", "worktree": "/x"},
                "'build_id' is required",
            ),
        ],
    )
    def test_the_refusals_say_what_was_wrong(
        self, cfg: ForgeConfig, payload: dict, wanted: str
    ) -> None:
        status, body = process_receipts_export_request(payload, config=cfg)
        assert status == 400 and wanted in body["error"]

    def test_an_extra_file_name_with_a_path_in_it_is_refused(
        self, cfg: ForgeConfig, repo: Path, receipts_root: Path
    ) -> None:
        tree = _worktree_with_receipts(cfg, repo)
        status, body = process_receipts_export_request(
            {
                "build_id": BUILD_ID,
                "stage": "task-work",
                "worktree": str(tree),
                "extra_files": {"../escape.txt": "no"},
            },
            config=cfg,
        )
        assert status == 400 and "must be a plain file name" in body["error"]


# ---------------------------------------------------------------------------
# End to end over loopback — a real server, a real socket
# ---------------------------------------------------------------------------


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
def server(cfg: ForgeConfig):
    srv = build_server(port=0, config_loader=lambda: cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host, port = srv.server_address[:2]
    assert host == "127.0.0.1"
    try:
        yield f"http://{host}:{port}"
    finally:
        srv.shutdown()
        srv.server_close()


def test_end_to_end_cut_export_and_remove_over_loopback(
    server: str, repo: Path, receipts_root: Path
) -> None:
    status, body = _post(
        server + GIT_WORKTREE_ADD_ROUTE,
        {"repo": REPO_KEY, "path": _tree(repo), "branch": BRANCH, "base_ref": "main"},
    )
    assert status == 200 and body["status"] == "success", body
    tree = Path(body["path"])
    family = tree / ".guardkit" / "autobuild"
    family.mkdir(parents=True)
    (family / "review.json").write_text("{}", encoding="utf-8")

    status, body = _post(
        server + RECEIPTS_EXPORT_ROUTE,
        {"build_id": BUILD_ID, "stage": "task-review", "worktree": str(tree)},
    )
    assert status == 200 and body["status"] == "success", body
    assert Path(body["dest"], ".guardkit", "autobuild", "review.json").is_file()

    status, body = _post(
        server + GIT_WORKTREE_REMOVE_ROUTE, {"repo": REPO_KEY, "path": str(tree)}
    )
    assert status == 200 and body["status"] == "success", body
    assert not tree.exists()


def test_the_commit_count_answers_over_loopback(server: str, repo: Path) -> None:
    status, body = _post(
        server + GIT_WORKTREE_ADD_ROUTE,
        {"repo": REPO_KEY, "path": _tree(repo), "branch": BRANCH, "base_ref": "main"},
    )
    assert status == 200 and body["status"] == "success", body
    tree = Path(body["path"])

    status, body = _post(
        server + GIT_WORKTREE_COMMIT_COUNT_ROUTE,
        {"repo": REPO_KEY, "path": str(tree), "base": "main"},
    )
    assert status == 200 and body["count"] == 0, body

    _commit(tree, "the-fix")

    status, body = _post(
        server + GIT_WORKTREE_COMMIT_COUNT_ROUTE,
        {"repo": REPO_KEY, "path": str(tree), "base": "main"},
    )
    assert status == 200 and body["count"] == 1, body
    assert body["head"] == _git(tree, "rev-parse", "HEAD").strip()

    status, body = _post(
        server + GIT_WORKTREE_COMMIT_COUNT_ROUTE,
        {"repo": REPO_KEY, "path": "/etc", "base": "main"},
    )
    assert status == 400 and "and on no other path" in body["error"]


def test_a_refusal_over_loopback_is_http_400_with_one_sentence(server: str) -> None:
    status, body = _post(
        server + GIT_WORKTREE_ADD_ROUTE,
        {"repo": REPO_KEY, "path": "/etc", "branch": BRANCH},
    )
    assert status == 400 and "and on no other path" in body["error"]


def test_the_other_operations_still_answer_beside_the_new_routes(
    server: str,
) -> None:
    status, body = _post(server + "/guardkit-merge", {"repo": "acme/ghost"})
    assert status == 400 and "unknown target repo" in body["error"]
    status, body = _post(server + "/receipts/no-such-op", {})
    assert status == 404


# ---------------------------------------------------------------------------
# /guardkit-leg — a fix journey's legs, run where the repository lives
# ---------------------------------------------------------------------------


def _fake_guardkit(bin_dir: Path, log: Path) -> Path:
    """A stand-in ``guardkit`` that records its arguments and its directory."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    binary = bin_dir / "guardkit"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"open({str(log)!r}, 'a').write(json.dumps("
        "{'argv': sys.argv[1:], 'cwd': os.getcwd()}) + '\\n')\n"
        "print('leg ran')\n"
        "sys.exit(int(os.environ.get('FAKE_LEG_EXIT', '0')))\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary


@pytest.fixture
def leg_guardkit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log = tmp_path / "leg-calls.jsonl"
    binary = _fake_guardkit(tmp_path / "legbin", log)
    monkeypatch.setenv("FORGE_GUARDKIT_PATH", str(binary))
    monkeypatch.delenv("FAKE_LEG_EXIT", raising=False)
    return log


def _leg_calls(log: Path) -> list[dict[str, Any]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line]


class TestTheLegRoute:
    def test_a_leg_runs_in_the_journey_worktree_with_its_arguments(
        self, cfg: ForgeConfig, repo: Path, leg_guardkit: Path
    ) -> None:
        from forge.deploy_sidecar.service import process_guardkit_leg_request

        tree = _worktree_with_receipts(cfg, repo)
        status, body = process_guardkit_leg_request(
            {
                "repo": REPO_KEY,
                "cwd": str(tree),
                "subcommand": "task-review",
                "args": ["--build-id", BUILD_ID, "--task-id", "TASK-WT-001"],
            },
            config=cfg,
        )
        assert status == 200, body
        assert body["exit_code"] == 0 and "leg ran" in body["stdout"]
        call = _leg_calls(leg_guardkit)[0]
        assert call["argv"] == [
            "task-review",
            "--build-id",
            BUILD_ID,
            "--task-id",
            "TASK-WT-001",
        ]
        assert Path(call["cwd"]).resolve() == tree.resolve()

    def test_a_failing_leg_is_an_exit_code_not_a_transport_error(
        self,
        cfg: ForgeConfig,
        repo: Path,
        leg_guardkit: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from forge.deploy_sidecar.service import process_guardkit_leg_request

        monkeypatch.setenv("FAKE_LEG_EXIT", "2")
        tree = _worktree_with_receipts(cfg, repo)
        status, body = process_guardkit_leg_request(
            {"repo": REPO_KEY, "cwd": str(tree), "subcommand": "task-work"},
            config=cfg,
        )
        assert status == 200 and body["exit_code"] == 2

    def test_a_wall_longer_than_this_route_allows_is_cut_back_not_refused(
        self, cfg: ForgeConfig, repo: Path, leg_guardkit: Path
    ) -> None:
        """Ruled 2026-09-07: a profile that widens a stage's wall past this
        route's ceiling must still run its leg. Refusing it with a 400 fails a
        leg that could have run, and reads to the caller as an internal error
        before dispatch."""
        from forge.deploy_sidecar.service import (
            LEG_TIMEOUT_MAX,
            process_guardkit_leg_request,
        )

        tree = _worktree_with_receipts(cfg, repo)
        seen: dict[str, Any] = {}

        def _record(
            *, argv: list[str], cwd: str, timeout: float
        ) -> tuple[int, str, str]:
            seen["timeout"] = timeout
            return 0, "leg ran", ""

        status, body = process_guardkit_leg_request(
            {
                "repo": REPO_KEY,
                "cwd": str(tree),
                "subcommand": "task-work",
                "timeout_seconds": 99999,
            },
            config=cfg,
            leg_runner=_record,
        )

        assert status == 200 and body["exit_code"] == 0
        assert seen["timeout"] == LEG_TIMEOUT_MAX
        warning = body["context_warnings"][0]
        assert warning["code"] == "leg_timeout_clamped"
        assert "99999" in warning["message"] and "7200" in warning["message"]

    def test_a_wall_within_the_ceiling_is_passed_through_with_no_warning(
        self, cfg: ForgeConfig, repo: Path, leg_guardkit: Path
    ) -> None:
        from forge.deploy_sidecar.service import process_guardkit_leg_request

        tree = _worktree_with_receipts(cfg, repo)
        seen: dict[str, Any] = {}

        def _record(
            *, argv: list[str], cwd: str, timeout: float
        ) -> tuple[int, str, str]:
            seen["timeout"] = timeout
            return 0, "leg ran", ""

        status, body = process_guardkit_leg_request(
            {
                "repo": REPO_KEY,
                "cwd": str(tree),
                "subcommand": "task-work",
                "timeout_seconds": 60,
            },
            config=cfg,
            leg_runner=_record,
        )

        assert status == 200 and seen["timeout"] == 60.0
        assert [
            warning
            for warning in body["context_warnings"]
            if warning.get("code") == "leg_timeout_clamped"
        ] == []

    @pytest.mark.parametrize(
        "over, wanted",
        [
            ({"subcommand": "autobuild"}, "'subcommand' must be one of"),
            ({"subcommand": "qa"}, "'subcommand' must be one of"),
            ({"args": "not-a-list"}, "'args' must be a list"),
            ({"args": [1]}, "must be written as text"),
            ({"timeout_seconds": 0}, "'timeout_seconds' must be a positive"),
        ],
    )
    def test_the_refusals_say_what_was_wrong(
        self,
        cfg: ForgeConfig,
        repo: Path,
        leg_guardkit: Path,
        over: dict,
        wanted: str,
    ) -> None:
        from forge.deploy_sidecar.service import process_guardkit_leg_request

        tree = _worktree_with_receipts(cfg, repo)
        payload = {
            "repo": REPO_KEY,
            "cwd": str(tree),
            "subcommand": "task-review",
            **over,
        }
        status, body = process_guardkit_leg_request(payload, config=cfg)
        assert status == 400 and wanted in body["error"]
        assert _leg_calls(leg_guardkit) == []

    def test_a_working_directory_that_is_not_a_journey_tree_is_refused(
        self, cfg: ForgeConfig, repo: Path, leg_guardkit: Path
    ) -> None:
        from forge.deploy_sidecar.service import process_guardkit_leg_request

        status, body = process_guardkit_leg_request(
            {"repo": REPO_KEY, "cwd": str(repo), "subcommand": "task-work"},
            config=cfg,
        )
        assert status == 400
        assert "is not a journey worktree of this repository" in body["error"]
        assert _leg_calls(leg_guardkit) == []

    def test_a_working_directory_that_is_not_there_is_refused(
        self, cfg: ForgeConfig, repo: Path, leg_guardkit: Path
    ) -> None:
        from forge.deploy_sidecar.service import process_guardkit_leg_request

        status, body = process_guardkit_leg_request(
            {"repo": REPO_KEY, "cwd": _tree(repo), "subcommand": "task-work"},
            config=cfg,
        )
        assert status == 400 and "is not there" in body["error"]

    def test_a_sidecar_with_no_guardkit_says_so_before_anything_runs(
        self, cfg: ForgeConfig, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from forge.deploy_sidecar.service import process_guardkit_leg_request

        tree = _worktree_with_receipts(cfg, repo)
        status, body = process_guardkit_leg_request(
            {"repo": REPO_KEY, "cwd": str(tree), "subcommand": "task-work"},
            config=cfg,
            command_resolver=lambda: None,
        )
        assert status == 500 and "no guardkit command" in body["error"]


def test_a_leg_runs_end_to_end_over_loopback(
    server: str, cfg: ForgeConfig, repo: Path, leg_guardkit: Path
) -> None:
    from forge.deploy_sidecar.service import GUARDKIT_LEG_ROUTE

    tree = _worktree_with_receipts(cfg, repo)
    status, body = _post(
        server + GUARDKIT_LEG_ROUTE,
        {
            "repo": REPO_KEY,
            "cwd": str(tree),
            "subcommand": "task-work",
            "args": ["--task-id", "TASK-WT-001-FIX1"],
        },
    )
    assert status == 200 and body["exit_code"] == 0, body
    call = _leg_calls(leg_guardkit)[0]
    assert call["argv"][0] == "task-work"
    assert Path(call["cwd"]).resolve() == tree.resolve()


# ---------------------------------------------------------------------------
# The leg is TOLD what it would have been told in the container (rule 75:
# the legs move, what they are given does not)
# ---------------------------------------------------------------------------


def _manifest_in(tree: Path) -> Path:
    """Put a context manifest and the document it names in a journey tree."""
    (tree / ".guardkit").mkdir(parents=True, exist_ok=True)
    (tree / ".guardkit" / "context-manifest.yaml").write_text(
        "internal_docs:\n  always_include:\n    - docs/contract.md\n",
        encoding="utf-8",
    )
    (tree / "docs").mkdir(parents=True, exist_ok=True)
    doc = tree / "docs" / "contract.md"
    doc.write_text("the contract\n", encoding="utf-8")
    return doc


class TestWhatTheLegIsGiven:
    def test_the_manifests_context_the_forward_paths_and_progress_all_arrive(
        self, cfg: ForgeConfig, repo: Path, leg_guardkit: Path
    ) -> None:
        from forge.deploy_sidecar.service import process_guardkit_leg_request

        tree = _worktree_with_receipts(cfg, repo)
        doc = _manifest_in(tree)
        pack = str(tree / "failure-pack.md")

        status, body = process_guardkit_leg_request(
            {
                "repo": REPO_KEY,
                "cwd": str(tree),
                "subcommand": "task-review",
                "args": ["--task-id", "TASK-WT-001"],
                "extra_context_paths": [pack],
                "read_allowlist": [str(tree)],
                "with_nats_streaming": True,
            },
            config=cfg,
        )

        assert status == 200, body
        assert _leg_calls(leg_guardkit)[0]["argv"] == [
            "task-review",
            "--task-id",
            "TASK-WT-001",
            # the manifest's own document first, exactly as the in-container
            # runner orders them …
            "--context",
            str(doc.resolve()),
            # … then the conductor's forward-context paths …
            "--context",
            pack,
            # … then the progress switch.
            "--nats",
        ]
        assert body["context_warnings"] == []
        assert body["timed_out"] is False

    def test_no_progress_asked_for_means_no_progress_flag(
        self, cfg: ForgeConfig, repo: Path, leg_guardkit: Path
    ) -> None:
        from forge.deploy_sidecar.service import process_guardkit_leg_request

        tree = _worktree_with_receipts(cfg, repo)
        status, _ = process_guardkit_leg_request(
            {
                "repo": REPO_KEY,
                "cwd": str(tree),
                "subcommand": "task-work",
                "with_nats_streaming": False,
            },
            config=cfg,
        )
        assert status == 200
        assert _leg_calls(leg_guardkit)[0]["argv"] == ["task-work"]

    def test_a_tree_with_no_manifest_says_so_and_still_runs_the_leg(
        self, cfg: ForgeConfig, repo: Path, leg_guardkit: Path
    ) -> None:
        from forge.deploy_sidecar.service import process_guardkit_leg_request

        tree = _worktree_with_receipts(cfg, repo)
        status, body = process_guardkit_leg_request(
            {"repo": REPO_KEY, "cwd": str(tree), "subcommand": "task-work"},
            config=cfg,
        )
        assert status == 200 and body["exit_code"] == 0
        assert [w["code"] for w in body["context_warnings"]] == [
            "context_manifest_missing"
        ]
        assert _leg_calls(leg_guardkit)[0]["argv"] == ["task-work"]

    def test_a_document_outside_the_allowlist_is_left_out_and_named(
        self, cfg: ForgeConfig, repo: Path, leg_guardkit: Path, tmp_path: Path
    ) -> None:
        from forge.deploy_sidecar.service import process_guardkit_leg_request

        tree = _worktree_with_receipts(cfg, repo)
        _manifest_in(tree)
        status, body = process_guardkit_leg_request(
            {
                "repo": REPO_KEY,
                "cwd": str(tree),
                "subcommand": "task-review",
                "read_allowlist": [str(tmp_path / "somewhere-else")],
            },
            config=cfg,
        )
        assert status == 200
        assert [w["code"] for w in body["context_warnings"]] == [
            "context_manifest_path_outside_allowlist"
        ]
        assert _leg_calls(leg_guardkit)[0]["argv"] == ["task-review"]

    @pytest.mark.parametrize(
        "over, wanted",
        [
            ({"extra_context_paths": "one"}, "'extra_context_paths' must be a list"),
            ({"extra_context_paths": [3]}, "every entry in 'extra_context_paths'"),
            ({"read_allowlist": {"a": 1}}, "'read_allowlist' must be a list"),
            ({"with_nats_streaming": "yes"}, "must be true or false"),
        ],
    )
    def test_the_new_fields_are_checked_before_anything_runs(
        self,
        cfg: ForgeConfig,
        repo: Path,
        leg_guardkit: Path,
        over: dict,
        wanted: str,
    ) -> None:
        from forge.deploy_sidecar.service import process_guardkit_leg_request

        tree = _worktree_with_receipts(cfg, repo)
        status, body = process_guardkit_leg_request(
            {
                "repo": REPO_KEY,
                "cwd": str(tree),
                "subcommand": "task-review",
                **over,
            },
            config=cfg,
        )
        assert status == 400 and wanted in body["error"]
        assert _leg_calls(leg_guardkit) == []

    def test_a_leg_stopped_at_its_wall_says_it_was_stopped(
        self, cfg: ForgeConfig, repo: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from forge.deploy_sidecar.service import process_guardkit_leg_request

        binary = tmp_path / "slowbin" / "guardkit"
        binary.parent.mkdir()
        binary.write_text(
            "#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n",
            encoding="utf-8",
        )
        binary.chmod(0o755)
        monkeypatch.setenv("FORGE_GUARDKIT_PATH", str(binary))
        tree = _worktree_with_receipts(cfg, repo)

        status, body = process_guardkit_leg_request(
            {
                "repo": REPO_KEY,
                "cwd": str(tree),
                "subcommand": "task-work",
                "timeout_seconds": 1,
            },
            config=cfg,
        )
        assert status == 200 and body["timed_out"] is True
        assert "was stopped after 1 seconds" in body["stderr_tail"]

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
    GIT_WORKTREE_REMOVE_ROUTE,
    RECEIPTS_EXPORT_ROUTE,
    build_server,
    process_git_worktree_add_request,
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

    @pytest.mark.parametrize(
        "over, wanted",
        [
            ({"subcommand": "autobuild"}, "'subcommand' must be one of"),
            ({"subcommand": "qa"}, "'subcommand' must be one of"),
            ({"args": "not-a-list"}, "'args' must be a list"),
            ({"args": [1]}, "must be written as text"),
            ({"timeout_seconds": 0}, "'timeout_seconds' must be a positive"),
            ({"timeout_seconds": 99999}, "may not be longer than"),
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

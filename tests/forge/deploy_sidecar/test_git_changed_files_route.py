"""``/git/worktree-changed-files`` — what a fix journey's branch changed, read
where the repository lives (the specification fence, Rich's ruling
2026-09-09, rule 88).

Real code paths throughout: a real git repository and a real journey worktree
in a temporary directory, the real route running real git, and — for the
end-to-end case — the real HTTP server on an ephemeral loopback port. Nothing
live is touched: every path is under ``tmp_path``.

What is pinned:

* a renamed acceptance twin comes back as a RENAME, with both names, so the
  refusal can say "renamed to" rather than "deleted" and "added";
* a twin whose body changed comes back as a change to that file;
* a rewritten ``APPROVED … by <name>`` line comes back in the approval patch,
  in both its halves — the line removed and the line put in its place;
* a branch that touches neither answers with two empty strings;
* what the branch did to the TESTS comes back in the same answer (Rich's
  ruling, 2026-09-10) — the route was widened rather than answered beside,
  because the caller needs this branch's own diff LINES and this route
  already has the branch open;
* that third reading can never refuse anything: it comes back empty and says
  so, while the two the fence refuses on keep the bound they always kept;
* the same laws every other git route keeps: an unknown repository, a path
  that is not this repository's own journey tree, a base git would read as an
  option, a tree that is not there, a base nobody made;
* the route is reachable over the real server and the answer is the same one.
"""

from __future__ import annotations

import json
import subprocess
import threading
import urllib.request
from pathlib import Path

import pytest

from forge.config.models import ForgeConfig
from forge.deploy_sidecar.service import (
    GIT_WORKTREE_ADD_ROUTE,
    GIT_WORKTREE_CHANGED_FILES_ROUTE,
    build_server,
    process_git_worktree_add_request,
    process_git_worktree_changed_files_request,
)

REPO_KEY = "guardkit/api_test"
OTHER_KEY = "guardkit/other"
BUILD_ID = "build-FEAT-SF01-20260909210000"
BRANCH = "fix/TASK-SF-001-09210000"
TWIN = "qa/twins/users-delete-by-email/double-delete-honest-404.hurl"
RENAMED_TWIN = "qa/twins/users-delete-by-email/double-delete-honest-410.hurl"
APPROVAL = (
    "# APPROVED AS PROPOSED by Rich 2026-07-28 (interactive sit; all 4 "
    "assumptions confirmed, ASSUM-003 = 404 honest absence)\n"
)
TESTS = "tests/test_router.py"
RENAMED_TESTS = "tests/test_lookup.py"
THE_TESTS = (
    "def test_lookup_by_email():\n"
    "    assert lookup('a@b') == 'a@b'\n"
    "\n\n"
    "def test_deleted_user_is_absent():\n"
    "    assert lookup('gone@b') is None\n"
)

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
    """A throwaway clone carrying one acceptance twin with an approval on it,
    and two of the repository's own tests."""
    path = tmp_path / "api_test"
    (path / "qa" / "twins" / "users-delete-by-email").mkdir(parents=True)
    (path / "tests").mkdir(parents=True)
    _git(path, "init", "-b", "main")
    (path / "README").write_text("scratch\n", encoding="utf-8")
    (path / TWIN).write_text(
        APPROVAL + "DELETE http://localhost/users?email=a@b\nHTTP 204\n",
        encoding="utf-8",
    )
    (path / TESTS).write_text(THE_TESTS, encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "init")
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
def tree(cfg: ForgeConfig, repo: Path) -> Path:
    """The journey's own worktree, cut for real by the sidecar's own route."""
    status, body = process_git_worktree_add_request(
        {
            "repo": REPO_KEY,
            "path": str(repo / ".forge" / "worktrees" / BUILD_ID),
            "branch": BRANCH,
            "base_ref": "main",
        },
        config=cfg,
    )
    assert status == 200 and body["status"] == "success", body
    return Path(body["path"])


def _ask(cfg: ForgeConfig, tree: Path, base: str = "main"):
    return process_git_worktree_changed_files_request(
        {"repo": REPO_KEY, "path": str(tree), "base": base}, config=cfg
    )


class TestWhatTheBranchChangedComesBack:
    def test_a_renamed_twin_comes_back_as_a_rename_with_both_names(
        self, cfg: ForgeConfig, tree: Path
    ) -> None:
        _git(tree, "mv", TWIN, RENAMED_TWIN)
        _git(tree, "commit", "-m", "rename the twin")

        status, body = _ask(cfg, tree)

        assert status == 200, body
        fields = body["name_status"].split("\0")
        assert fields[0].startswith("R"), body["name_status"]
        assert fields[1] == TWIN
        assert fields[2] == RENAMED_TWIN
        assert body["head"]

    def test_a_twin_whose_body_changed_comes_back_as_a_change(
        self, cfg: ForgeConfig, tree: Path
    ) -> None:
        (tree / TWIN).write_text(
            APPROVAL + "DELETE http://localhost/users?email=a@b\nHTTP 410\n",
            encoding="utf-8",
        )
        _git(tree, "add", "-A")
        _git(tree, "commit", "-m", "410 not 204")

        status, body = _ask(cfg, tree)

        assert status == 200, body
        assert body["name_status"].split("\0")[:2] == ["M", TWIN]

    def test_a_rewritten_approval_line_comes_back_in_both_halves(
        self, cfg: ForgeConfig, tree: Path
    ) -> None:
        """The incident itself: his name and his date kept on a new sentence."""
        (tree / TWIN).write_text(
            APPROVAL.replace("404 honest absence", "410 Gone for soft-deleted")
            + "DELETE http://localhost/users?email=a@b\nHTTP 410\n",
            encoding="utf-8",
        )
        _git(tree, "add", "-A")
        _git(tree, "commit", "-m", "update the twin")

        status, body = _ask(cfg, tree)

        assert status == 200, body
        patch = body["approval_patch"]
        assert "-# APPROVED AS PROPOSED by Rich" in patch
        assert "+# APPROVED AS PROPOSED by Rich" in patch
        assert "404 honest absence" in patch and "410 Gone" in patch
        assert body["truncated"] is False

    def test_an_approval_carried_into_a_renamed_file_is_still_seen(
        self, cfg: ForgeConfig, tree: Path
    ) -> None:
        """Renames are OFF in the approval half on purpose: a file moved
        wholesale carries no changed lines, and a moved approval would be
        invisible if it were not read as a removal and an addition."""
        _git(tree, "mv", TWIN, RENAMED_TWIN)
        _git(tree, "commit", "-m", "rename the twin")

        status, body = _ask(cfg, tree)

        assert status == 200, body
        assert "-# APPROVED AS PROPOSED by Rich" in body["approval_patch"]
        assert "+# APPROVED AS PROPOSED by Rich" in body["approval_patch"]

    def test_a_branch_that_touched_neither_answers_empty(
        self, cfg: ForgeConfig, tree: Path
    ) -> None:
        (tree / "src.py").write_text("print('the fix')\n", encoding="utf-8")
        _git(tree, "add", "-A")
        _git(tree, "commit", "-m", "the fix")

        status, body = _ask(cfg, tree)

        assert status == 200, body
        assert body["name_status"].split("\0")[:2] == ["A", "src.py"]
        assert body["approval_patch"] == ""

    def test_a_branch_with_no_commits_answers_nothing_at_all(
        self, cfg: ForgeConfig, tree: Path
    ) -> None:
        status, body = _ask(cfg, tree)

        assert status == 200, body
        assert body["name_status"] == ""
        assert body["approval_patch"] == ""


class TestWhatTheBranchDidToTheTestsComesBackToo:
    def test_a_deleted_test_and_its_assertions_come_back_in_the_test_patch(
        self, cfg: ForgeConfig, tree: Path
    ) -> None:
        (tree / TESTS).write_text(
            "def test_lookup_by_email():\n    assert lookup('a@b') == 'a@b'\n",
            encoding="utf-8",
        )
        _git(tree, "add", "-A")
        _git(tree, "commit", "-m", "trim the tests")

        status, body = _ask(cfg, tree)

        assert status == 200, body
        assert "-def test_deleted_user_is_absent():" in body["test_patch"]
        assert "-    assert lookup('gone@b') is None" in body["test_patch"]
        assert body["test_patch_truncated"] is False

    def test_a_test_file_that_only_moved_carries_no_lines_at_all(
        self, cfg: ForgeConfig, tree: Path
    ) -> None:
        """Renames stay ON in this half, unlike the approval half: a test file
        that only moved has lost nothing and must not be read as if it had."""
        _git(tree, "mv", TESTS, RENAMED_TESTS)
        _git(tree, "commit", "-m", "rename the test file")

        status, body = _ask(cfg, tree)

        assert status == 200, body
        assert body["test_patch"] == ""

    def test_a_branch_that_touched_no_test_answers_an_empty_test_patch(
        self, cfg: ForgeConfig, tree: Path
    ) -> None:
        (tree / "src.py").write_text("print('the fix')\n", encoding="utf-8")
        _git(tree, "add", "-A")
        _git(tree, "commit", "-m", "the fix")

        status, body = _ask(cfg, tree)

        assert status == 200, body
        assert body["test_patch"] == ""
        assert body["test_patch_truncated"] is False

    def test_ordinary_code_is_not_carried_back_with_the_tests(
        self, cfg: ForgeConfig, tree: Path
    ) -> None:
        """The reading is filtered to the words that could be a test or an
        assertion, so a branch's ordinary code is not carried over the wire."""
        (tree / "src.py").write_text("value = compute()\n", encoding="utf-8")
        (tree / TESTS).write_text(
            THE_TESTS.replace("is None", "is not None"), encoding="utf-8"
        )
        _git(tree, "add", "-A")
        _git(tree, "commit", "-m", "the fix")

        status, body = _ask(cfg, tree)

        assert status == 200, body
        assert "-    assert lookup('gone@b') is None" in body["test_patch"]
        assert "src.py" not in body["test_patch"]


class TestTheTestDiffNeverTurnsIntoARefusal:
    """The caller reads a 500 from this route as "this could not be read",
    and refuses the branch on it. The test diff feeds a line on a card, so no
    way of failing IT may produce one — not a git that exits non-zero, not a
    diff too long to carry, and not a git that never answers at all."""

    def test_a_test_diff_that_never_answers_still_gets_a_200(
        self, cfg: ForgeConfig, tree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from forge.pipeline.merge_ready_checkpoint import TEST_CHANGE_MARKER

        (tree / TESTS).write_text(
            "def test_lookup_by_email():\n    assert lookup('a@b') == 'a@b'\n",
            encoding="utf-8",
        )
        _git(tree, "add", "-A")
        _git(tree, "commit", "-m", "trim the tests")
        marker = f"-G{TEST_CHANGE_MARKER}"
        real = subprocess.run

        def _run(argv, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            if isinstance(argv, (list, tuple)) and marker in argv:
                raise subprocess.TimeoutExpired(cmd=list(argv), timeout=120.0)
            return real(argv, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", _run)

        status, body = _ask(cfg, tree)

        assert status == 200, body
        assert body["test_patch"] == ""
        assert body["test_patch_truncated"] is True
        # The two readings the fence really does refuse on are untouched.
        assert body["name_status"].split("\0")[:2] == ["M", TESTS]
        assert body["truncated"] is False


class TestTheSameLawsEveryOtherGitRouteKeeps:
    def test_only_this_repositorys_own_journey_tree_path_is_read(
        self, cfg: ForgeConfig, repo: Path, other_repo: Path
    ) -> None:
        for path in (
            "/etc",
            str(repo),
            str(other_repo / ".forge" / "worktrees" / BUILD_ID),
            str(repo / ".forge" / "worktrees" / ".." / ".." / "elsewhere"),
        ):
            status, body = process_git_worktree_changed_files_request(
                {"repo": REPO_KEY, "path": path, "base": "main"}, config=cfg
            )
            assert status == 400, (path, body)
            assert "and on no other path" in body["error"]

    def test_an_unknown_repository_is_refused_by_name(
        self, cfg: ForgeConfig, repo: Path
    ) -> None:
        status, body = process_git_worktree_changed_files_request(
            {
                "repo": "acme/ghost",
                "path": str(repo / ".forge" / "worktrees" / BUILD_ID),
                "base": "main",
            },
            config=cfg,
        )

        assert status == 400 and "unknown target repo" in body["error"]

    def test_a_base_git_would_read_as_an_option_is_refused(
        self, cfg: ForgeConfig, tree: Path
    ) -> None:
        for base in ("--all", "main..HEAD", "", None):
            status, body = process_git_worktree_changed_files_request(
                {"repo": REPO_KEY, "path": str(tree), "base": base}, config=cfg
            )
            assert status == 400, (base, body)
            assert "'base'" in body["error"]

    def test_a_tree_that_is_not_there_is_refused_before_git_runs(
        self, cfg: ForgeConfig, repo: Path
    ) -> None:
        status, body = process_git_worktree_changed_files_request(
            {
                "repo": REPO_KEY,
                "path": str(repo / ".forge" / "worktrees" / BUILD_ID),
                "base": "main",
            },
            config=cfg,
        )

        assert status == 400 and "is not there" in body["error"]

    def test_a_base_nobody_made_is_a_loud_failure_never_a_quiet_nothing(
        self, cfg: ForgeConfig, tree: Path
    ) -> None:
        status, body = _ask(cfg, tree, base="no-such-branch")

        assert status == 500, body
        assert "could not read the changed files" in body["error"]
        assert "name_status" not in body

    def test_a_body_that_is_not_an_object_is_refused(self, cfg: ForgeConfig) -> None:
        status, body = process_git_worktree_changed_files_request("nope", config=cfg)

        assert status == 400 and "JSON object" in body["error"]


class TestTheRouteOverTheRealServer:
    def test_it_answers_on_the_wire(self, cfg: ForgeConfig, tree: Path) -> None:
        _git(tree, "mv", TWIN, RENAMED_TWIN)
        _git(tree, "commit", "-m", "rename the twin")

        srv = build_server(port=0, config_loader=lambda: cfg)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        host, port = srv.server_address[:2]
        try:
            request = urllib.request.Request(
                f"http://{host}:{port}{GIT_WORKTREE_CHANGED_FILES_ROUTE}",
                data=json.dumps(
                    {"repo": REPO_KEY, "path": str(tree), "base": "main"}
                ).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=30) as answer:  # noqa: S310
                assert answer.status == 200
                body = json.loads(answer.read().decode())
        finally:
            srv.shutdown()
            srv.server_close()

        assert TWIN in body["name_status"]
        assert RENAMED_TWIN in body["name_status"]

    def test_the_add_route_is_still_there_beside_it(
        self, cfg: ForgeConfig, repo: Path
    ) -> None:
        assert GIT_WORKTREE_ADD_ROUTE != GIT_WORKTREE_CHANGED_FILES_ROUTE

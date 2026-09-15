"""``/git/branch-scope`` — what a FINISHED BUILD's branch changed, read where
the repository lives (the planner fix, 2026-09-15).

Real code paths throughout: a real git repository in a temporary directory
with a plan of record on main and a real ``autobuild/FEAT-…`` branch off it,
the real route running real git, and — for the end-to-end case — the real HTTP
server on an ephemeral loopback port. Nothing live is touched and no broker is
spoken to: every path is under ``tmp_path``.

What is pinned:

* the branch's own changed files come back, with the span held to
  ``<base>...<head>`` so what main has done since is not counted;
* the lines the branch ADDS come back, because a web address nobody asked for
  and a capability nobody asked for are both things the branch wrote;
* the plan of record behind the build comes back — the feature's own file and
  every task document it names — so the files the plan declared can be
  compared with the files the build changed;
* the same laws every other git route keeps: an unknown repository, a ref git
  would read as an option, a feature id that is not an identifier;
* a branch that is not there is a 200 saying so, NOT a 500 — the line this
  feeds is a report on a card and may never refuse a merge;
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
    GIT_BRANCH_SCOPE_ROUTE,
    GIT_WORKTREE_CHANGED_FILES_ROUTE,
    build_server,
    process_git_branch_scope_request,
)

REPO_KEY = "guardkit/api_test"
FEATURE_ID = "FEAT-BSC1"
BRANCH = f"autobuild/{FEATURE_ID}"
TASK_DOC = "tasks/backlog/daily-counts/TASK-BSC1-001.md"

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


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway clone with a plan on main and a build branch beside it."""
    path = tmp_path / "api_test"
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    _write(
        path,
        f".guardkit/features/{FEATURE_ID}.yaml",
        f'id: {FEATURE_ID}\ntasks:\n  - id: TASK-BSC1-001\n    file_path: "{TASK_DOC}"\n',
    )
    _write(
        path,
        TASK_DOC,
        "---\nid: TASK-BSC1-001\n---\n\nAdd the endpoint.\n\n"
        "## Files to Create\n\n- _none_\n\n"
        "## Files to Modify\n\n- `src/users/router.py`\n",
    )
    _write(path, "src/users/router.py", "# the users router\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "the plan of record")
    _git(path, "checkout", "-b", BRANCH)
    _write(
        path,
        "src/users/router.py",
        "# the users router\n@router.get('/stats/users-created-per-day')\n",
    )
    _write(path, "src/analytics/service.py", "def build_counts():\n    return []\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "what the build wrote")
    _git(path, "checkout", "main")
    return path.resolve()


@pytest.fixture
def cfg(repo: Path) -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {"target_repo_paths": {REPO_KEY: str(repo)}},
        }
    )


def _ask(cfg: ForgeConfig, **overrides: object):
    body = {
        "repo": REPO_KEY,
        "base": "main",
        "head": BRANCH,
        "feature_id": FEATURE_ID,
    }
    body.update(overrides)
    return process_git_branch_scope_request(body, config=cfg)


class TestWhatComesBack:
    def test_the_branch_s_own_changed_files(self, cfg: ForgeConfig) -> None:
        status, body = _ask(cfg)

        assert status == 200, body
        assert body["error"] is None
        assert "src/users/router.py" in body["name_status"]
        assert "src/analytics/service.py" in body["name_status"]
        assert body["head"]

    def test_the_lines_the_branch_added(self, cfg: ForgeConfig) -> None:
        status, body = _ask(cfg)

        assert status == 200, body
        assert body["added_lines_read_whole"] is True
        assert "/stats/users-created-per-day" in body["added_lines"]
        # a header is not a line the branch added
        assert "+++ b/src/users/router.py" not in body["added_lines"]

    def test_the_plan_of_record_behind_the_build(self, cfg: ForgeConfig) -> None:
        status, body = _ask(cfg)

        assert status == 200, body
        assert f"id: {FEATURE_ID}" in body["feature_file"]
        assert TASK_DOC in body["plan_documents"]
        assert "## Files to Modify" in body["plan_documents"][TASK_DOC]

    def test_a_main_that_moved_on_is_not_counted(
        self, cfg: ForgeConfig, repo: Path
    ) -> None:
        """The three-dot span: what THIS branch did, never what main has done
        since it was cut."""
        _write(repo, "src/unrelated.py", "# someone else's work\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "main moved on")

        status, body = _ask(cfg)

        assert status == 200, body
        assert "src/unrelated.py" not in body["name_status"]


class TestWhenThereIsNothingToRead:
    def test_a_branch_that_is_not_there_answers_200_and_says_so(
        self, cfg: ForgeConfig
    ) -> None:
        """A 500 would read to the caller as a reason to refuse; this line is
        a report on a card and may never do that."""
        status, body = _ask(cfg, head="autobuild/FEAT-NOTHERE")

        assert status == 200, body
        assert body["error"]
        assert body["name_status"] == ""

    def test_a_feature_with_no_plan_answers_with_an_empty_plan(
        self, cfg: ForgeConfig
    ) -> None:
        status, body = _ask(cfg, feature_id="FEAT-NOPLAN")

        assert status == 200, body
        assert body["error"] is None
        assert body["feature_file"] == ""
        assert body["plan_documents"] == {}


class TestTheLawsEveryGitRouteKeeps:
    def test_an_unknown_repository_is_refused(self, cfg: ForgeConfig) -> None:
        status, body = _ask(cfg, repo="nobody/nothing")
        assert status == 400 and "unknown target repo" in body["error"]

    def test_a_ref_git_would_read_as_an_option_is_refused(
        self, cfg: ForgeConfig
    ) -> None:
        status, body = _ask(cfg, head="--upload-pack=touch /tmp/x")
        assert status == 400 and "'head'" in body["error"]

    def test_a_base_that_is_not_a_ref_is_refused(self, cfg: ForgeConfig) -> None:
        status, body = _ask(cfg, base="main/../../etc")
        assert status == 400 and "'base'" in body["error"]

    def test_a_feature_id_that_is_not_an_identifier_is_refused(
        self, cfg: ForgeConfig
    ) -> None:
        status, body = _ask(cfg, feature_id="../../etc/passwd")
        assert status == 400 and "'feature_id'" in body["error"]

    def test_a_body_that_is_not_an_object_is_refused(self, cfg: ForgeConfig) -> None:
        status, body = process_git_branch_scope_request("nope", config=cfg)
        assert status == 400 and "JSON object" in body["error"]


class TestTheRouteOverTheRealServer:
    def test_it_answers_on_the_wire(self, cfg: ForgeConfig) -> None:
        srv = build_server(port=0, config_loader=lambda: cfg)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        host, port = srv.server_address[:2]
        try:
            request = urllib.request.Request(
                f"http://{host}:{port}{GIT_BRANCH_SCOPE_ROUTE}",
                data=json.dumps(
                    {
                        "repo": REPO_KEY,
                        "base": "main",
                        "head": BRANCH,
                        "feature_id": FEATURE_ID,
                    }
                ).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=60) as answer:  # noqa: S310
                assert answer.status == 200
                body = json.loads(answer.read().decode())
        finally:
            srv.shutdown()
            srv.server_close()

        assert "src/analytics/service.py" in body["name_status"]
        assert TASK_DOC in body["plan_documents"]

    def test_the_fix_journey_s_own_route_is_still_there_beside_it(self) -> None:
        assert GIT_BRANCH_SCOPE_ROUTE != GIT_WORKTREE_CHANGED_FILES_ROUTE


class TestTheSandboxAndTheHostCannotDrift:
    def test_the_sandbox_reader_reads_the_route_s_own_answer(
        self, cfg: ForgeConfig
    ) -> None:
        """The sandbox client and the route speak the same shape, proved by
        handing one the other's answer."""
        from types import SimpleNamespace

        from forge.pipeline.branch_scope import read_branch_scope_in_sandbox

        status, answer = _ask(cfg)
        assert status == 200

        def _post(_url: str, _body: object, _timeout: float):
            return 200, answer

        reading = read_branch_scope_in_sandbox(
            sandbox=SimpleNamespace(name="sbx", sidecar_url="http://127.0.0.1:1"),
            repo=REPO_KEY,
            base="main",
            head=BRANCH,
            feature_id=FEATURE_ID,
            post=_post,
        )
        assert reading.error is None
        assert reading.name_status == answer["name_status"]
        assert reading.added_lines == answer["added_lines"]
        assert reading.plan_documents == answer["plan_documents"]
        assert reading.added_lines_read_whole is True

    def test_a_sidecar_that_never_heard_of_the_question_is_said_plainly(
        self,
    ) -> None:
        from types import SimpleNamespace

        from forge.pipeline.branch_scope import read_branch_scope_in_sandbox

        def _post(_url: str, _body: object, _timeout: float):
            return 404, {"error": "no such path: /git/branch-scope"}

        reading = read_branch_scope_in_sandbox(
            sandbox=SimpleNamespace(name="sbx", sidecar_url="http://127.0.0.1:1"),
            repo=REPO_KEY,
            base="main",
            head=BRANCH,
            feature_id=FEATURE_ID,
            post=_post,
        )
        assert reading.error is not None
        assert "404" in reading.error

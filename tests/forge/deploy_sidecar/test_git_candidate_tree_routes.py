"""The merge press's own git routes on the deploy sidecar (sandbox first,
rule 89).

Three routes, each acting on the repository its ``repo`` key names and on no
path a caller sends: ``POST /git/is-ancestor`` (the press's two ancestry
guards), ``POST /git/candidate-tree`` (the branch's tree laid out for the
candidate check, kept out of the clone's eyes, its tree id answered) and
``POST /git/candidate-tree-remove`` (that tree gone when the run ends).

Everything here is real: a real git repository in ``tmp_path``, the real
service on a real ephemeral loopback port, real HTTP requests. Nothing live is
touched — no ``sbx``, no docker, no sandbox, no service of the estate.
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
    GIT_CANDIDATE_TREE_REMOVE_ROUTE,
    GIT_CANDIDATE_TREE_ROUTE,
    GIT_IS_ANCESTOR_ROUTE,
    build_server,
)

REPO = "guardkit/api_test"
FEATURE_ID = "FEAT-L3E"

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
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """The factory's own clone, as it is inside a repository's sandbox."""
    root = tmp_path / "api_test"
    root.mkdir()
    _git(root, "init", "-b", "main", "-q")
    (root / "README.md").write_text("first\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "first")
    _git(root, "checkout", "-q", "-b", f"autobuild/{FEATURE_ID}", "main")
    (root / "feature.txt").write_text("the feature\n", encoding="utf-8")
    _git(root, "add", "feature.txt")
    _git(root, "commit", "-q", "-m", "the feature")
    _git(root, "checkout", "-q", "main")
    return root.resolve()


@pytest.fixture
def config(clone: Path) -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": [str(clone.parent)]}},
            "planning": {"target_repo_paths": {REPO: str(clone)}},
        }
    )


@pytest.fixture
def sidecar(config: ForgeConfig):
    """The real service, on a real loopback port."""
    srv = build_server(port=0, config_loader=lambda: config)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host, port = srv.server_address[:2]
    assert host == "127.0.0.1"
    try:
        yield f"http://{host}:{port}"
    finally:
        srv.shutdown()
        srv.server_close()


def _post(url: str, body: dict[str, Any]) -> tuple[int, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return int(response.status), json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return int(exc.code), json.loads(exc.read().decode("utf-8"))


class TestTheAncestryRoute:
    def test_it_answers_yes_no_and_could_not_say(
        self, sidecar: str, clone: Path
    ) -> None:
        main = _git(clone, "rev-parse", "main")
        tip = _git(clone, "rev-parse", f"autobuild/{FEATURE_ID}")

        status, body = _post(
            f"{sidecar}{GIT_IS_ANCESTOR_ROUTE}",
            {"repo": REPO, "ancestor": main, "descendant": tip},
        )
        assert (status, body["is_ancestor"]) == (200, True)

        status, body = _post(
            f"{sidecar}{GIT_IS_ANCESTOR_ROUTE}",
            {"repo": REPO, "ancestor": tip, "descendant": main},
        )
        assert (status, body["is_ancestor"]) == (200, False)

        # A commit this repository has never heard of: git cannot say, and the
        # route says so rather than guessing either way.
        status, body = _post(
            f"{sidecar}{GIT_IS_ANCESTOR_ROUTE}",
            {"repo": REPO, "ancestor": "b" * 40, "descendant": main},
        )
        assert status == 200 and body["is_ancestor"] is None
        assert "could not say" in body["detail"]

    def test_branch_names_are_answered_too(self, sidecar: str, clone: Path) -> None:
        status, body = _post(
            f"{sidecar}{GIT_IS_ANCESTOR_ROUTE}",
            {"repo": REPO, "ancestor": "main", "descendant": f"autobuild/{FEATURE_ID}"},
        )
        assert (status, body["is_ancestor"]) == (200, True)

    @pytest.mark.parametrize(
        "body",
        [
            {"repo": REPO, "ancestor": "--upload-pack=touch /tmp/x", "descendant": "main"},
            {"repo": REPO, "ancestor": "main", "descendant": "../../etc"},
            {"repo": REPO, "ancestor": "main"},
            {"repo": "someone/else", "ancestor": "main", "descendant": "main"},
        ],
    )
    def test_a_shape_it_will_not_pass_to_git_is_refused(
        self, sidecar: str, body: dict[str, Any]
    ) -> None:
        status, answer = _post(f"{sidecar}{GIT_IS_ANCESTOR_ROUTE}", body)
        assert status == 400 and answer["error"]


class TestTheCandidateTreeRoute:
    def test_it_lays_the_branchs_tree_out_and_answers_the_tree_id(
        self, sidecar: str, clone: Path
    ) -> None:
        tip = _git(clone, "rev-parse", f"autobuild/{FEATURE_ID}")

        status, body = _post(
            f"{sidecar}{GIT_CANDIDATE_TREE_ROUTE}",
            {"repo": REPO, "feature_id": FEATURE_ID, "sha": tip},
        )

        assert status == 200, body
        laid_out = Path(body["path"])
        assert laid_out == clone / ".forge-candidates" / FEATURE_ID
        assert (laid_out / "feature.txt").read_text(encoding="utf-8") == "the feature\n"
        # A laid-out tree carries no git state of its own.
        assert not (laid_out / ".git").exists()
        assert body["tree"] == _git(clone, "rev-parse", f"{tip}^{{tree}}")
        assert body["exclude_written"] is True
        # And the clone still looks clean, which is what the merge command's
        # own dirty-tree check reads.
        assert _git(clone, "status", "--porcelain") == ""

    def test_a_second_lay_out_replaces_the_first_and_writes_no_second_line(
        self, sidecar: str, clone: Path
    ) -> None:
        tip = _git(clone, "rev-parse", f"autobuild/{FEATURE_ID}")
        url = f"{sidecar}{GIT_CANDIDATE_TREE_ROUTE}"
        body = {"repo": REPO, "feature_id": FEATURE_ID, "sha": tip}

        assert _post(url, body)[1]["exclude_written"] is True
        status, second = _post(url, body)

        assert status == 200 and second["exclude_written"] is False
        exclude = (clone / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        assert exclude.count(".forge-candidates/") == 1

    def test_a_commit_this_repository_does_not_have_is_an_answer_not_a_crash(
        self, sidecar: str, clone: Path
    ) -> None:
        status, body = _post(
            f"{sidecar}{GIT_CANDIDATE_TREE_ROUTE}",
            {"repo": REPO, "feature_id": FEATURE_ID, "sha": "b" * 40},
        )

        assert status == 400
        assert "git archive" in body["error"]
        # Nothing is left behind by a lay-out that failed.
        assert not (clone / ".forge-candidates" / FEATURE_ID).exists()

    @pytest.mark.parametrize(
        "body",
        [
            {"repo": REPO, "feature_id": "../escape", "sha": "main"},
            {"repo": REPO, "feature_id": "a/b", "sha": "main"},
            {"repo": REPO, "sha": "main"},
            {"repo": REPO, "feature_id": FEATURE_ID, "sha": "--exec=x"},
            {"repo": "someone/else", "feature_id": FEATURE_ID, "sha": "main"},
        ],
    )
    def test_a_shape_it_will_not_pass_to_git_is_refused(
        self, sidecar: str, clone: Path, body: dict[str, Any]
    ) -> None:
        status, answer = _post(f"{sidecar}{GIT_CANDIDATE_TREE_ROUTE}", body)
        assert status == 400 and answer["error"]
        assert not (clone / ".forge-candidates").exists()


class TestTheCandidateTreeRemoveRoute:
    def test_it_removes_the_tree_and_a_second_call_is_still_a_success(
        self, sidecar: str, clone: Path
    ) -> None:
        tip = _git(clone, "rev-parse", f"autobuild/{FEATURE_ID}")
        _post(
            f"{sidecar}{GIT_CANDIDATE_TREE_ROUTE}",
            {"repo": REPO, "feature_id": FEATURE_ID, "sha": tip},
        )
        laid_out = clone / ".forge-candidates" / FEATURE_ID
        assert laid_out.is_dir()

        url = f"{sidecar}{GIT_CANDIDATE_TREE_REMOVE_ROUTE}"
        status, body = _post(url, {"repo": REPO, "feature_id": FEATURE_ID})

        assert (status, body["removed"]) == (200, True)
        assert body["path"] == str(laid_out)
        assert not laid_out.exists()
        # The press calls this on every ending, including ones where nothing
        # was ever laid out.
        assert _post(url, {"repo": REPO, "feature_id": FEATURE_ID})[1]["removed"] is True

    def test_it_removes_only_the_tree_its_repository_and_feature_name(
        self, sidecar: str, clone: Path
    ) -> None:
        """No path from the caller ever reaches git or the filesystem here."""
        other = clone / ".forge-candidates" / "FEAT-OTHER"
        other.mkdir(parents=True)
        (other / "keep.txt").write_text("keep\n", encoding="utf-8")

        status, body = _post(
            f"{sidecar}{GIT_CANDIDATE_TREE_REMOVE_ROUTE}",
            {"repo": REPO, "feature_id": FEATURE_ID, "path": str(other)},
        )

        assert status == 200 and body["removed"] is True
        assert (other / "keep.txt").is_file()

    @pytest.mark.parametrize(
        "body",
        [
            {"repo": REPO, "feature_id": ".."},
            {"repo": REPO},
            {"repo": "someone/else", "feature_id": FEATURE_ID},
        ],
    )
    def test_a_shape_it_will_not_act_on_is_refused(
        self, sidecar: str, body: dict[str, Any]
    ) -> None:
        status, answer = _post(f"{sidecar}{GIT_CANDIDATE_TREE_REMOVE_ROUTE}", body)
        assert status == 400 and answer["error"]

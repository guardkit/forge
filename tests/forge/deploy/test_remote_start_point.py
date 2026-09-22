"""Where a new piece of work starts (one true copy, item 1, 2026-09-21).

The factory used to cut every new branch from whatever its own copy of a
project happened to have checked out. These tests drive the one operation
that changes that: fetch the remote named ``origin``, say which branch that
remote calls its default and which commit it is at — and then cut the branch
from exactly that commit.

**Nothing here touches a real remote.** Every "remote" is a bare repository
in a temporary directory, so a fetch is real git against a local path: the
same code, the same refusals, nobody's account involved.

Nothing here knows what a project contains: the projects in these tests hold
one text file and no code of any kind.
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

from forge.adapters.git.planning_runner import WorktreeGitRunner
from forge.config.models import ForgeConfig
from forge.deploy.candidate_tree import (
    InContainerCandidateGit,
    RemoteStartPoint,
    fetch_remote_start_point,
)
from forge.deploy.sidecar_git import SidecarCandidateGit
from forge.deploy_sidecar.service import (
    GIT_REMOTE_START_POINT_ROUTE,
    build_server,
    process_git_remote_start_point_request,
    process_git_write_tree_request,
)
from forge.planning.sidecar_git_runner import RepoRoutedGitRunner, SidecarGitRunner

REPO_KEY = "guardkit/api_test"


# ---------------------------------------------------------------------------
# A remote on disk, and a copy of it
# ---------------------------------------------------------------------------


def _env() -> dict[str, str]:
    import os

    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    done = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    if check and done.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {done.stderr}")
    return done.stdout.strip()


def make_remote(path: Path, *, default_branch: str = "main") -> Path:
    """A bare repository with one commit on ``default_branch``, and its HEAD
    pointing there — which is how a remote says which branch is its default."""
    seed = path.parent / f"{path.name}-seed"
    seed.mkdir(parents=True)
    _git(seed, "init", "-q", "-b", default_branch)
    (seed / "README.md").write_text("one\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-qm", "one")
    path.mkdir(parents=True)
    _git(path, "init", "--bare", "-q", "-b", default_branch)
    _git(seed, "remote", "add", "origin", str(path))
    _git(seed, "push", "-q", "origin", f"HEAD:refs/heads/{default_branch}")
    _git(path, "symbolic-ref", "HEAD", f"refs/heads/{default_branch}")
    return path


def clone_of(remote: Path, where: Path) -> Path:
    _git(where.parent, "clone", "-q", str(remote), str(where))
    return where


def advance_remote(remote: Path, message: str) -> str:
    """One more commit on the remote's default branch; returns its commit."""
    work = remote.parent / f"{remote.name}-writer"
    if not work.exists():
        _git(remote.parent, "clone", "-q", str(remote), str(work))
    branch = _git(work, "rev-parse", "--abbrev-ref", "HEAD")
    (work / f"{message}.txt").write_text(message, encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-qm", message)
    _git(work, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
    return _git(work, "rev-parse", "HEAD")


def diverge_the_copy(copy: Path, commits: int) -> str:
    """Move the copy's OWN checked-out branch on, without telling the remote."""
    for index in range(commits):
        (copy / f"local-{index}.txt").write_text(str(index), encoding="utf-8")
        _git(copy, "add", ".")
        _git(copy, "commit", "-qm", f"local {index}")
    return _git(copy, "rev-parse", "HEAD")


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    return make_remote(tmp_path / "origin.git")


@pytest.fixture
def copy(tmp_path: Path, remote: Path) -> Path:
    return clone_of(remote, tmp_path / "api_test")


# ---------------------------------------------------------------------------
# The operation itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_remotes_default_branch_is_found_fetched_and_its_commit_returned(
    remote: Path, copy: Path
) -> None:
    answer = await fetch_remote_start_point(copy)

    assert answer.ok
    assert answer.branch == "main"
    assert answer.commit == _git(remote, "rev-parse", "refs/heads/main")
    assert answer.refusal is None
    # The fetch really happened here: the copy now holds the remote's commit
    # under its remote-tracking ref.
    assert _git(copy, "rev-parse", "refs/remotes/origin/main") == answer.commit


@pytest.mark.asyncio
async def test_a_default_branch_that_is_not_called_main_is_read_from_the_remote(
    tmp_path: Path,
) -> None:
    remote = make_remote(tmp_path / "trunk-origin.git", default_branch="trunk")
    copy = clone_of(remote, tmp_path / "trunk-copy")

    answer = await fetch_remote_start_point(copy)

    assert answer.ok
    assert answer.branch == "trunk"
    assert answer.commit == _git(remote, "rev-parse", "refs/heads/trunk")


@pytest.mark.asyncio
async def test_a_copy_with_no_remote_named_origin_is_refused_in_plain_words(
    tmp_path: Path,
) -> None:
    alone = tmp_path / "alone"
    alone.mkdir()
    _git(alone, "init", "-q")
    (alone / "README.md").write_text("alone\n", encoding="utf-8")
    _git(alone, "add", ".")
    _git(alone, "commit", "-qm", "one")

    answer = await fetch_remote_start_point(alone)

    assert not answer.ok
    assert answer.branch is None and answer.commit is None
    assert answer.refusal == (
        f"the copy of this project at {alone} has no remote named 'origin', "
        f"so there is nothing to start the work from. Add that remote to the "
        f"copy, then ask again."
    )


@pytest.mark.asyncio
async def test_a_remote_that_cannot_be_reached_is_refused_with_gits_own_words(
    tmp_path: Path, remote: Path, copy: Path
) -> None:
    gone = tmp_path / "not-here.git"
    _git(copy, "remote", "set-url", "origin", str(gone))

    answer = await fetch_remote_start_point(copy)

    assert not answer.ok
    assert answer.refusal is not None
    assert answer.refusal.startswith(
        f"the remote named 'origin' could not be reached from {copy}, so the "
        f"work cannot be started from it: "
    )


@pytest.mark.asyncio
async def test_a_remote_that_names_no_default_branch_is_refused(
    tmp_path: Path, copy: Path
) -> None:
    empty = tmp_path / "empty.git"
    empty.mkdir()
    _git(empty, "init", "--bare", "-q")
    _git(copy, "remote", "set-url", "origin", str(empty))

    answer = await fetch_remote_start_point(copy)

    assert not answer.ok
    assert answer.refusal == (
        "the remote named 'origin' does not say which branch is its default, "
        "so there is nothing to start the work from. Set that remote's "
        "default branch, then ask again."
    )


@pytest.mark.asyncio
async def test_the_copys_own_checked_out_branch_and_working_folder_are_untouched(
    remote: Path, copy: Path
) -> None:
    diverge_the_copy(copy, 3)
    branch_before = _git(copy, "rev-parse", "--abbrev-ref", "HEAD")
    head_before = _git(copy, "rev-parse", "HEAD")
    status_before = _git(copy, "status", "--porcelain")
    advance_remote(remote, "theirs")

    answer = await fetch_remote_start_point(copy)

    assert answer.ok
    assert _git(copy, "rev-parse", "--abbrev-ref", "HEAD") == branch_before
    assert _git(copy, "rev-parse", "HEAD") == head_before
    assert _git(copy, "status", "--porcelain") == status_before
    assert answer.commit != head_before


@pytest.mark.asyncio
async def test_the_answer_follows_the_remote_when_the_remote_moves(
    remote: Path, copy: Path
) -> None:
    first = await fetch_remote_start_point(copy)
    moved = advance_remote(remote, "second")

    second = await fetch_remote_start_point(copy)

    assert first.commit != second.commit
    assert second.commit == moved


@pytest.mark.asyncio
async def test_the_in_container_venue_offers_the_operation(copy: Path) -> None:
    venue = InContainerCandidateGit(copy)

    answer = await venue.fetch_remote_start_point()

    assert answer.ok


# ---------------------------------------------------------------------------
# The branch is cut from the fetched commit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_branch_is_cut_from_the_fetched_commit_not_from_the_copys_own(
    tmp_path: Path, remote: Path, copy: Path
) -> None:
    """The copy's own main is three commits away from the remote's. The
    planning branch is cut from the REMOTE's commit all the same."""
    local_head = diverge_the_copy(copy, 3)
    runner = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    start = await runner.fetch_remote_start_point(str(copy))
    assert start.ok and start.commit != local_head

    result = await runner.prepare_branch_and_write(
        repo_path=str(copy),
        branch="planning/run-0001",
        file_path="feature_spec_inputs/run-0001.md",
        content="the sentence\n",
        start_commit=start.commit,
    )

    assert result.status == "success"
    parent = _git(copy, "rev-parse", "planning/run-0001^")
    assert parent == start.commit
    assert parent != local_head


@pytest.mark.asyncio
async def test_the_tree_write_cuts_from_the_fetched_commit_too(
    tmp_path: Path, copy: Path
) -> None:
    local_head = diverge_the_copy(copy, 3)
    runner = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    start = await runner.fetch_remote_start_point(str(copy))

    result = await runner.prepare_branch_and_write_tree(
        str(copy),
        "planning/run-0002",
        {"a/b.md": "x\n"},
        "planning: a tree",
        start_commit=start.commit,
    )

    assert result.status == "success"
    assert _git(copy, "rev-parse", "planning/run-0002^") == start.commit
    assert _git(copy, "rev-parse", "planning/run-0002^") != local_head


@pytest.mark.asyncio
async def test_a_branch_that_already_exists_is_never_moved_onto_a_newer_commit(
    tmp_path: Path, remote: Path, copy: Path
) -> None:
    """Work that has already begun keeps its commit: the second write
    re-attaches the branch where it is, whatever the remote has done since."""
    runner = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    first = await runner.fetch_remote_start_point(str(copy))
    await runner.prepare_branch_and_write(
        repo_path=str(copy),
        branch="planning/run-0003",
        file_path="one.md",
        content="one\n",
        start_commit=first.commit,
    )
    tip_after_first = _git(copy, "rev-parse", "planning/run-0003")
    advance_remote(remote, "moved-on")
    second = await runner.fetch_remote_start_point(str(copy))
    assert second.commit != first.commit

    await runner.prepare_branch_and_write(
        repo_path=str(copy),
        branch="planning/run-0003",
        file_path="two.md",
        content="two\n",
        start_commit=second.commit,
    )

    # The branch grew from where it was; it was not re-cut from the new commit.
    assert _git(copy, "rev-parse", "planning/run-0003^") == tip_after_first


@pytest.mark.asyncio
async def test_without_a_start_commit_nothing_changes_for_a_copy_with_no_remote(
    tmp_path: Path,
) -> None:
    """The old behaviour is still exactly there when no commit is named."""
    alone = tmp_path / "alone"
    alone.mkdir()
    _git(alone, "init", "-q")
    (alone / "README.md").write_text("alone\n", encoding="utf-8")
    _git(alone, "add", ".")
    _git(alone, "commit", "-qm", "one")
    head = _git(alone, "rev-parse", "HEAD")
    runner = WorktreeGitRunner(worktrees_root=tmp_path / "wt")

    result = await runner.prepare_branch_and_write(
        repo_path=str(alone),
        branch="planning/run-0004",
        file_path="one.md",
        content="one\n",
    )

    assert result.status == "success"
    assert _git(alone, "rev-parse", "planning/run-0004^") == head


# ---------------------------------------------------------------------------
# The sandbox's helper service
# ---------------------------------------------------------------------------


def _config(paths: dict[str, str]) -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {"target_repo_paths": paths},
        }
    )


def test_the_route_answers_the_starting_point(remote: Path, copy: Path) -> None:
    status, body = process_git_remote_start_point_request(
        {"repo": REPO_KEY}, config=_config({REPO_KEY: str(copy)})
    )

    assert status == 200
    assert body["branch"] == "main"
    assert body["commit"] == _git(remote, "rev-parse", "refs/heads/main")
    assert body["refusal"] is None


def test_the_route_answers_a_refusal_as_an_answer_not_as_an_error(
    tmp_path: Path,
) -> None:
    alone = tmp_path / "alone"
    alone.mkdir()
    _git(alone, "init", "-q")
    (alone / "README.md").write_text("alone\n", encoding="utf-8")
    _git(alone, "add", ".")
    _git(alone, "commit", "-qm", "one")

    status, body = process_git_remote_start_point_request(
        {"repo": REPO_KEY}, config=_config({REPO_KEY: str(alone)})
    )

    assert status == 200
    assert body["branch"] is None and body["commit"] is None
    assert "has no remote named 'origin'" in body["refusal"]


def test_the_route_refuses_a_repository_it_does_not_serve(copy: Path) -> None:
    status, body = process_git_remote_start_point_request(
        {"repo": "someone/else"}, config=_config({REPO_KEY: str(copy)})
    )

    assert status == 400
    assert "someone/else" in body["error"]


def test_the_route_refuses_a_body_that_is_not_an_object(copy: Path) -> None:
    status, body = process_git_remote_start_point_request(
        ["not", "an", "object"], config=_config({REPO_KEY: str(copy)})
    )

    assert status == 400
    assert body["error"] == "request body must be a JSON object"


def test_the_write_route_cuts_the_branch_from_the_start_commit_it_is_given(
    tmp_path: Path, copy: Path
) -> None:
    """The sandbox's write route takes the named starting point too."""
    local_head = diverge_the_copy(copy, 3)
    start = _git(copy, "rev-parse", "refs/remotes/origin/main")
    assert start != local_head

    status, body = process_git_write_tree_request(
        {
            "repo": REPO_KEY,
            "branch": "planning/run-9001",
            "files": {"feature_spec_inputs/run-9001.md": "the sentence\n"},
            "message": "planning: the handoff",
            "checks": [],
            "start_commit": start,
        },
        config=_config({REPO_KEY: str(copy)}),
        worktrees_root=tmp_path / "wt",
    )

    assert status == 200 and body["status"] == "success"
    assert _git(copy, "rev-parse", "planning/run-9001^") == start


def test_the_write_route_refuses_a_start_commit_that_is_not_shaped_like_one(
    tmp_path: Path, copy: Path
) -> None:
    status, body = process_git_write_tree_request(
        {
            "repo": REPO_KEY,
            "branch": "planning/run-9002",
            "files": {"a.md": "x\n"},
            "message": "planning: the handoff",
            "checks": [],
            "start_commit": "--upload-pack=oops",
        },
        config=_config({REPO_KEY: str(copy)}),
        worktrees_root=tmp_path / "wt",
    )

    assert status == 400
    assert "start_commit" in body["error"]


def _post(url: str, body: dict[str, Any]) -> tuple[int, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return int(response.status), json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return int(exc.code), json.loads(raw) if raw else None


def test_the_route_is_on_the_services_allow_list(remote: Path, copy: Path) -> None:
    """A real server on a loopback port: the route is served, not a 404."""
    server = build_server(
        port=0, config_loader=lambda: _config({REPO_KEY: str(copy)})
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        status, body = _post(f"{base}{GIT_REMOTE_START_POINT_ROUTE}", {"repo": REPO_KEY})
        unknown, _ = _post(f"{base}/git/no-such-thing", {"repo": REPO_KEY})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status == 200
    assert body["commit"] == _git(remote, "rev-parse", "refs/heads/main")
    assert unknown == 404


# ---------------------------------------------------------------------------
# The two surfaces that speak to the sandbox
# ---------------------------------------------------------------------------


class _Post:
    """A stand-in for the wire: records what was sent, answers what it is told."""

    def __init__(self, answer: tuple[int, Any] | Exception) -> None:
        self.answer = answer
        self.sent: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, url: str, body: dict[str, Any], timeout: float) -> tuple[int, Any]:
        self.sent.append((url, body))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.mark.asyncio
async def test_the_merge_presss_sandbox_surface_asks_the_route() -> None:
    post = _Post((200, {"branch": "main", "commit": "a" * 40, "refusal": None}))
    surface = SidecarCandidateGit("http://127.0.0.1:8225", repo=REPO_KEY, post=post)

    answer = await surface.fetch_remote_start_point()

    assert answer == RemoteStartPoint(branch="main", commit="a" * 40)
    assert post.sent[0][0] == "http://127.0.0.1:8225/git/remote-start-point"
    assert post.sent[0][1] == {"repo": REPO_KEY}


@pytest.mark.asyncio
async def test_a_sandbox_that_cannot_be_reached_is_itself_a_refusal() -> None:
    post = _Post(OSError("connection refused"))
    surface = SidecarCandidateGit("http://127.0.0.1:8225", repo=REPO_KEY, post=post)

    answer = await surface.fetch_remote_start_point()

    assert not answer.ok
    assert "could not be reached" in (answer.refusal or "")


@pytest.mark.asyncio
async def test_the_planning_surface_asks_the_route_and_passes_the_refusal_on() -> None:
    post = _Post((200, {"branch": None, "commit": None, "refusal": "no remote here"}))
    runner = SidecarGitRunner("http://127.0.0.1:8225", repo=REPO_KEY, post=post)

    answer = await runner.fetch_remote_start_point("/srv/repos/api_test")

    assert answer.refusal == "no remote here"
    assert post.sent[0][0] == "http://127.0.0.1:8225/git/remote-start-point"


@pytest.mark.asyncio
async def test_the_planning_surface_sends_the_start_commit_with_the_write() -> None:
    post = _Post((200, {"status": "success", "sha": "b" * 40, "checks": [], "detail": ""}))
    runner = SidecarGitRunner("http://127.0.0.1:8225", repo=REPO_KEY, post=post)

    await runner.prepare_branch_and_write(
        "/srv/repos/api_test",
        "planning/run-0001",
        "feature_spec_inputs/run-0001.md",
        "the sentence\n",
        start_commit="c" * 40,
    )

    _, body = post.sent[0]
    assert body["start_commit"] == "c" * 40


@pytest.mark.asyncio
async def test_the_chooser_routes_the_new_operation_the_way_it_routes_the_others(
    tmp_path: Path, copy: Path
) -> None:
    sandboxed = SidecarGitRunner(
        "http://127.0.0.1:8225",
        repo=REPO_KEY,
        post=_Post((200, {"branch": "main", "commit": "d" * 40, "refusal": None})),
    )
    routed = RepoRoutedGitRunner(
        runners_by_repo={REPO_KEY: sandboxed},
        repo_paths={REPO_KEY: "/srv/repos/api_test"},
        default=WorktreeGitRunner(worktrees_root=tmp_path / "wt"),
    )

    in_the_sandbox = await routed.fetch_remote_start_point("/srv/repos/api_test")
    here = await routed.fetch_remote_start_point(str(copy))

    assert in_the_sandbox.commit == "d" * 40
    assert here.ok and here.commit != "d" * 40

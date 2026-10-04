"""Two small additions to the readers a prepared feature's admission uses.

4 October 2026 (project initialisation, Part 6). A feature planned elsewhere
is queued on its own branch, so admission asks the remote for the default
branch AND that branch's commit in one call; and the documents a project's
builds are held to must be ordinary files, so the committed-file reader can be
asked to refuse a symbolic link rather than hand back the link's target name.

Every remote here is a bare repository in a temporary directory. Nothing
touches a real remote, a live service or a sandbox.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from forge.adapters.git.planning_runner import WorktreeGitRunner
from forge.deploy.candidate_tree import (
    FileAtCommit,
    RemoteStartPoint,
    fetch_remote_start_point,
    read_file_at_commit,
)
from forge.deploy.sidecar_git import SidecarCandidateGit
from forge.deploy_sidecar.service import (
    process_git_read_file_at_commit_request,
    process_git_remote_start_point_request,
)
from forge.planning.sidecar_git_runner import RepoRoutedGitRunner, SidecarGitRunner

from tests.forge.deploy.test_remote_start_point import (
    REPO_KEY,
    _config,
    _git,
    _Post,
    clone_of,
    make_remote,
)


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    return make_remote(tmp_path / "origin.git")


@pytest.fixture
def copy(tmp_path: Path, remote: Path) -> Path:
    return clone_of(remote, tmp_path / "project")


def _push_branch(remote: Path, branch: str, files: dict[str, str]) -> str:
    work = remote.parent / f"{remote.name}-branch-writer"
    if not work.exists():
        _git(remote.parent, "clone", "-q", str(remote), str(work))
    _git(work, "checkout", "-q", "-B", branch, "origin/main")
    for rel, text in files.items():
        target = work / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-qm", f"work on {branch}")
    _git(work, "push", "-q", "-f", "origin", f"HEAD:refs/heads/{branch}")
    return _git(work, "rev-parse", "HEAD")


# ---------------------------------------------------------------------------
# A named branch, answered from the same call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_named_branch_is_fetched_and_its_commit_answered(
    remote: Path, copy: Path
) -> None:
    prepared = _push_branch(remote, "feature/prepared", {"spec.md": "spec\n"})

    answer = await fetch_remote_start_point(copy, "feature/prepared")

    assert answer.ok
    assert answer.branch == "main"
    assert answer.commit == _git(remote, "rev-parse", "refs/heads/main")
    assert answer.branch_commit == prepared
    # The commit is now in the copy, under the remote-tracking ref.
    assert _git(copy, "rev-parse", "refs/remotes/origin/feature/prepared") == prepared


@pytest.mark.asyncio
async def test_naming_the_default_branch_answers_its_own_commit(
    remote: Path, copy: Path
) -> None:
    answer = await fetch_remote_start_point(copy, "main")

    assert answer.ok
    assert answer.branch_commit == answer.commit


@pytest.mark.asyncio
async def test_a_branch_the_remote_does_not_have_is_refused_in_plain_words(
    copy: Path,
) -> None:
    answer = await fetch_remote_start_point(copy, "feature/nowhere")

    assert not answer.ok
    assert "has no branch called 'feature/nowhere'" in (answer.refusal or "")


@pytest.mark.asyncio
async def test_an_unusable_branch_name_is_refused_before_git_runs(copy: Path) -> None:
    answer = await fetch_remote_start_point(copy, "--upload-pack=oops")

    assert not answer.ok
    assert "is not one this factory will pass to git" in (answer.refusal or "")


@pytest.mark.asyncio
async def test_without_a_branch_the_answer_is_what_it_always_was(
    remote: Path, copy: Path
) -> None:
    answer = await fetch_remote_start_point(copy)

    assert answer == RemoteStartPoint(
        branch="main", commit=_git(remote, "rev-parse", "refs/heads/main")
    )
    assert "branch_commit" not in answer.to_wire()


@pytest.mark.asyncio
async def test_the_host_runner_and_the_chooser_pass_the_branch_on(
    tmp_path: Path, remote: Path, copy: Path
) -> None:
    prepared = _push_branch(remote, "feature/prepared", {"spec.md": "spec\n"})
    host = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    routed = RepoRoutedGitRunner(runners_by_repo={}, repo_paths={}, default=host)

    direct = await host.fetch_remote_start_point(str(copy), "feature/prepared")
    via_chooser = await routed.fetch_remote_start_point(str(copy), "feature/prepared")

    assert direct.branch_commit == via_chooser.branch_commit == prepared


def test_the_route_answers_a_named_branch(remote: Path, copy: Path) -> None:
    prepared = _push_branch(remote, "feature/prepared", {"spec.md": "spec\n"})

    status, body = process_git_remote_start_point_request(
        {"repo": REPO_KEY, "branch": "feature/prepared"},
        config=_config({REPO_KEY: str(copy)}),
    )

    assert status == 200
    assert body["branch"] == "main"
    assert body["branch_commit"] == prepared


def test_the_route_refuses_an_unusable_branch_name(copy: Path) -> None:
    status, body = process_git_remote_start_point_request(
        {"repo": REPO_KEY, "branch": "--upload-pack=oops"},
        config=_config({REPO_KEY: str(copy)}),
    )

    assert status == 400
    assert "branch" in body["error"]


@pytest.mark.asyncio
async def test_the_sandbox_surfaces_send_the_branch_only_when_given() -> None:
    post = _Post(
        (200, {"branch": "main", "commit": "a" * 40, "refusal": None, "branch_commit": "b" * 40})
    )
    runner = SidecarGitRunner("http://127.0.0.1:8225", repo=REPO_KEY, post=post)
    surface = SidecarCandidateGit("http://127.0.0.1:8225", repo=REPO_KEY, post=post)

    named = await runner.fetch_remote_start_point("/srv/x", "feature/prepared")
    plain = await surface.fetch_remote_start_point()

    assert post.sent[0][1] == {"repo": REPO_KEY, "branch": "feature/prepared"}
    assert post.sent[1][1] == {"repo": REPO_KEY}
    assert named.branch_commit == "b" * 40
    assert plain.commit == "a" * 40


@pytest.mark.asyncio
async def test_a_helper_that_ignores_the_branch_is_a_refusal_not_a_guess() -> None:
    """An older helper answers the default branch alone; building that commit
    as if it were the named branch would build the wrong thing."""
    post = _Post((200, {"branch": "main", "commit": "a" * 40, "refusal": None}))
    runner = SidecarGitRunner("http://127.0.0.1:8225", repo=REPO_KEY, post=post)

    answer = await runner.fetch_remote_start_point("/srv/x", "feature/prepared")

    assert not answer.ok
    assert "did not say which commit the branch 'feature/prepared' is at" in (
        answer.refusal or ""
    )


# ---------------------------------------------------------------------------
# An ordinary file, not a symbolic link
# ---------------------------------------------------------------------------


def _commit_with_link(copy: Path) -> str:
    (copy / "docs").mkdir(exist_ok=True)
    (copy / "docs" / "real.md").write_text("the real text\n", encoding="utf-8")
    (copy / "docs" / "link.md").symlink_to("real.md")
    _git(copy, "add", ".")
    _git(copy, "commit", "-qm", "a link")
    return _git(copy, "rev-parse", "HEAD")


@pytest.mark.asyncio
async def test_a_symbolic_link_is_refused_when_an_ordinary_file_is_asked_for(
    copy: Path,
) -> None:
    sha = _commit_with_link(copy)

    plain = await read_file_at_commit(copy, sha, "docs/link.md")
    checked = await read_file_at_commit(
        copy, sha, "docs/link.md", ordinary_file_only=True
    )

    # Without the check git hands back the link's target NAME as the file.
    assert plain.found and plain.content == "real.md"
    assert not checked.ok
    assert "is a symbolic link, not an ordinary file" in (checked.refusal or "")


@pytest.mark.asyncio
async def test_an_ordinary_file_is_read_and_says_it_was_checked(copy: Path) -> None:
    sha = _commit_with_link(copy)

    answer = await read_file_at_commit(
        copy, sha, "docs/real.md", ordinary_file_only=True
    )

    assert answer.found and answer.ordinary
    assert answer.content == "the real text\n"
    assert answer.to_wire()["ordinary"] is True


@pytest.mark.asyncio
async def test_without_the_check_the_answer_is_what_it_always_was(copy: Path) -> None:
    sha = _commit_with_link(copy)

    answer = await read_file_at_commit(copy, sha, "docs/real.md")

    assert answer == FileAtCommit(content="the real text\n", found=True)
    assert "ordinary" not in answer.to_wire()


def test_the_route_refuses_a_link_when_asked(copy: Path) -> None:
    sha = _commit_with_link(copy)

    status, body = process_git_read_file_at_commit_request(
        {
            "repo": REPO_KEY,
            "commit": sha,
            "file_path": "docs/link.md",
            "ordinary_file_only": True,
        },
        config=_config({REPO_KEY: str(copy)}),
    )

    assert status == 200
    assert "symbolic link" in body["refusal"]


@pytest.mark.asyncio
async def test_a_helper_that_does_not_confirm_the_check_is_a_refusal() -> None:
    post = _Post((200, {"content": "real.md", "found": True, "refusal": None}))
    runner = SidecarGitRunner("http://127.0.0.1:8225", repo=REPO_KEY, post=post)

    answer = await runner.read_file_at_commit(
        "/srv/x", "c" * 40, "docs/link.md", ordinary_file_only=True
    )

    assert post.sent[0][1]["ordinary_file_only"] is True
    assert not answer.ok
    assert "did not confirm the file is an ordinary file" in (answer.refusal or "")


@pytest.mark.asyncio
async def test_the_sandbox_read_without_the_check_sends_what_it_always_sent() -> None:
    post: Any = _Post((200, {"content": "x", "found": True, "refusal": None}))
    runner = SidecarGitRunner("http://127.0.0.1:8225", repo=REPO_KEY, post=post)

    answer = await runner.read_file_at_commit("/srv/x", "c" * 40, "a.md")

    assert post.sent[0][1] == {"repo": REPO_KEY, "commit": "c" * 40, "file_path": "a.md"}
    assert answer.found and answer.content == "x"

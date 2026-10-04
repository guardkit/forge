"""The committed-file reader's RAW read: exact bytes and the entry's mode.

4 October 2026, the one reading rule for a project's documents (project
initialisation design). The planning writers and GuardKit's Coach must be given
the same bytes from the same commit, so the reader can be asked for a raw read:
``git cat-file blob`` of the entry, decoded strictly as UTF-8 with line endings
kept, and the tree entry's mode reported so a caller follows a link only when
the reader says the entry IS one. Every other read is unchanged.

Every repository here is a temporary one. Nothing touches a real remote, a
live service or a sandbox.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from forge.adapters.git.planning_runner import WorktreeGitRunner
from forge.deploy.candidate_tree import FileAtCommit, read_file_at_commit
from forge.deploy_sidecar.service import process_git_read_file_at_commit_request
from forge.planning.sidecar_git_runner import RepoRoutedGitRunner, SidecarGitRunner

from tests.forge.deploy.test_remote_start_point import REPO_KEY, _config, _git, _Post


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "project"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "docs").mkdir()
    (repo / "docs" / "crlf.md").write_bytes(b"one\r\ntwo\r\n")
    (repo / "docs" / "cr.md").write_bytes(b"one\rtwo\r")
    (repo / "docs" / "bad.md").write_bytes(b"ok\n\xff\xfe\n")
    (repo / "docs" / "link.md").symlink_to("crlf.md")
    (repo / "linked-docs").symlink_to("docs")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "bytes")
    return repo, _git(repo, "rev-parse", "HEAD")


@pytest.mark.asyncio
async def test_a_raw_read_keeps_every_byte(tmp_path: Path) -> None:
    repo, sha = _repo(tmp_path)

    crlf = await read_file_at_commit(repo, sha, "docs/crlf.md", raw=True)
    cr = await read_file_at_commit(repo, sha, "docs/cr.md", raw=True)

    assert crlf.found and crlf.mode == "100644" and crlf.ordinary
    assert crlf.content is not None and crlf.content.encode("utf-8") == b"one\r\ntwo\r\n"
    assert cr.content is not None and cr.content.encode("utf-8") == b"one\rtwo\r"


@pytest.mark.asyncio
async def test_without_raw_the_read_is_what_it_always_was(tmp_path: Path) -> None:
    repo, sha = _repo(tmp_path)

    plain = await read_file_at_commit(repo, sha, "docs/crlf.md")

    # The text read normalises line endings, as it always has; no mode.
    assert plain == FileAtCommit(content="one\ntwo\n", found=True)
    assert "mode" not in plain.to_wire()


@pytest.mark.asyncio
async def test_invalid_utf8_is_refused_by_the_raw_read(tmp_path: Path) -> None:
    repo, sha = _repo(tmp_path)

    answer = await read_file_at_commit(repo, sha, "docs/bad.md", raw=True)

    assert not answer.ok
    assert "is not UTF-8 text" in (answer.refusal or "")


@pytest.mark.asyncio
async def test_a_link_a_folder_and_nothing_are_told_apart(tmp_path: Path) -> None:
    repo, sha = _repo(tmp_path)

    link = await read_file_at_commit(repo, sha, "docs/link.md", raw=True)
    linked_folder = await read_file_at_commit(repo, sha, "linked-docs", raw=True)
    through_link = await read_file_at_commit(repo, sha, "linked-docs/crlf.md", raw=True)
    folder = await read_file_at_commit(repo, sha, "docs", raw=True)
    nothing = await read_file_at_commit(repo, sha, "docs/none.md", raw=True)

    assert link.is_link and link.content == "crlf.md" and not link.ordinary
    assert linked_folder.is_link and linked_folder.content == "docs"
    # Git's tree has no entry under a link: the caller resolves the link.
    assert through_link == FileAtCommit(found=False)
    assert folder == FileAtCommit(found=False, mode="040000")
    assert nothing == FileAtCommit(found=False)


def test_the_route_passes_raw_on_and_answers_the_mode(tmp_path: Path) -> None:
    repo, sha = _repo(tmp_path)
    config = _config({REPO_KEY: str(repo)})

    status, body = process_git_read_file_at_commit_request(
        {"repo": REPO_KEY, "commit": sha, "file_path": "docs/crlf.md", "raw": True},
        config=config,
    )
    status_link, link = process_git_read_file_at_commit_request(
        {"repo": REPO_KEY, "commit": sha, "file_path": "docs/link.md", "raw": True},
        config=config,
    )
    status_plain, plain = process_git_read_file_at_commit_request(
        {"repo": REPO_KEY, "commit": sha, "file_path": "docs/crlf.md"},
        config=config,
    )

    assert status == status_link == status_plain == 200
    assert body["content"] == "one\r\ntwo\r\n" and body["mode"] == "100644"
    assert link["content"] == "crlf.md" and link["mode"] == "120000"
    assert plain == {"content": "one\ntwo\n", "found": True, "refusal": None}
    # JSON carries the exact text, and the far side reads the same answer back.
    assert FileAtCommit.from_wire(body).content == "one\r\ntwo\r\n"


@pytest.mark.asyncio
async def test_the_sandbox_runner_sends_raw_and_reads_the_mode_back() -> None:
    post: Any = _Post(
        (200, {"content": "a\r\n", "found": True, "refusal": None, "ordinary": True, "mode": "100755"})
    )
    runner = SidecarGitRunner("http://127.0.0.1:8225", repo=REPO_KEY, post=post)

    answer = await runner.read_file_at_commit("/srv/x", "c" * 40, "a.md", raw=True)

    assert post.sent[0][1] == {
        "repo": REPO_KEY,
        "commit": "c" * 40,
        "file_path": "a.md",
        "raw": True,
    }
    assert answer.found and answer.content == "a\r\n" and answer.mode == "100755"


@pytest.mark.asyncio
async def test_a_helper_that_does_not_answer_the_mode_is_a_refusal() -> None:
    post: Any = _Post((200, {"content": "a\n", "found": True, "refusal": None}))
    runner = SidecarGitRunner("http://127.0.0.1:8225", repo=REPO_KEY, post=post)

    answer = await runner.read_file_at_commit("/srv/x", "c" * 40, "a.md", raw=True)

    assert not answer.ok
    assert "exact bytes" in (answer.refusal or "")


@pytest.mark.asyncio
async def test_the_host_runner_and_the_chooser_pass_raw_on(tmp_path: Path) -> None:
    repo, sha = _repo(tmp_path)
    host = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    chooser = RepoRoutedGitRunner(runners_by_repo={}, repo_paths={}, default=host)

    direct = await host.read_file_at_commit(str(repo), sha, "docs/link.md", raw=True)
    chosen = await chooser.read_file_at_commit(str(repo), sha, "docs/link.md", raw=True)

    assert direct.is_link and chosen == direct

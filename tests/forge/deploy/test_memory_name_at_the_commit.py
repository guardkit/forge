"""Which memory a project declares, read at the commit the work starts from.

The project's own memory (design pass 2026-09-21, item 2). Every build used to
read and write memory under the name "guardkit", because that name came from one
setting nothing set. These tests drive the read that changes it: the project's
own ``.guardkit/config.yaml`` **as it is at the recorded starting commit**, never
the working folder and never the branch the copy has checked out.

**Nothing here touches a real remote, a real memory service, its database, its
embedder or any broker.** Every "remote" is a bare repository in a temporary
directory; the only thing read is a settings file out of a commit.

Nothing here knows what a project contains: the projects in these tests hold one
settings file and one text file, and no code of any kind.
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
    MAX_FILE_AT_COMMIT_BYTES,
    FileAtCommit,
    read_file_at_commit,
)
from forge.deploy_sidecar.service import (
    GIT_READ_FILE_AT_COMMIT_ROUTE,
    build_server,
    process_git_read_file_at_commit_request,
)
from forge.planning.declared_memory import (
    DECLARATION_PATH,
    THE_TWO_LINES,
    read_declared_memory,
)
from forge.planning.sidecar_git_runner import RepoRoutedGitRunner, SidecarGitRunner

REPO_KEY = "guardkit/api_test"


# ---------------------------------------------------------------------------
# A project on disk whose commit and working folder can be made to disagree
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


def make_project(path: Path, declaration: str | None) -> str:
    """A repository with one commit; ``declaration`` is the settings file's text,
    or None for a project that carries no settings file at all. Returns the
    commit."""
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("one\n", encoding="utf-8")
    if declaration is not None:
        (path / ".guardkit").mkdir()
        (path / DECLARATION_PATH).write_text(declaration, encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "one")
    return _git(path, "rev-parse", "HEAD")


def commit_declaration(path: Path, declaration: str, message: str) -> str:
    (path / ".guardkit").mkdir(exist_ok=True)
    (path / DECLARATION_PATH).write_text(declaration, encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", message)
    return _git(path, "rev-parse", "HEAD")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    make_project(tmp_path / "widget_shop", "memory:\n  project: widget_shop\n")
    return tmp_path / "widget_shop"


# ---------------------------------------------------------------------------
# THE HEADLINE: the commit's copy wins over the working folder's
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_name_comes_from_the_commit_not_from_the_working_folder(
    tmp_path: Path,
) -> None:
    """The working folder declares X. The commit declares Y. Y is used."""
    path = tmp_path / "shop"
    make_project(path, "memory:\n  project: the_old_name\n")
    wanted = commit_declaration(
        path, "memory:\n  project: the_committed_name\n", "the name the work starts from"
    )
    # …and then somebody checks out something else entirely and edits the
    # working folder without committing it.
    _git(path, "checkout", "-q", "-b", "somewhere-else", "HEAD~1")
    (path / DECLARATION_PATH).write_text(
        "memory:\n  project: whatever_is_lying_about\n", encoding="utf-8"
    )

    read = await read_file_at_commit(path, wanted, DECLARATION_PATH)
    declared = read_declared_memory(
        repo=REPO_KEY,
        commit=wanted,
        content=read.content,
        found=read.found,
        unreadable_because=read.refusal,
    )

    assert declared.ok
    assert declared.project == "the_committed_name"
    # Proof the two really disagreed, so the test could not pass by accident.
    assert "whatever_is_lying_about" in (path / DECLARATION_PATH).read_text()
    assert "the_old_name" in _git(path, "show", f"HEAD:{DECLARATION_PATH}")


# ---------------------------------------------------------------------------
# Reading one file out of one commit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_file_in_the_commit_is_read(project: Path) -> None:
    commit = _git(project, "rev-parse", "HEAD")

    read = await read_file_at_commit(project, commit, DECLARATION_PATH)

    assert read.ok and read.found
    assert read.content == "memory:\n  project: widget_shop\n"
    assert read.refusal is None


@pytest.mark.asyncio
async def test_a_file_that_is_not_in_the_commit_is_an_answer_not_a_refusal(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bare_project"
    commit = make_project(path, None)

    read = await read_file_at_commit(path, commit, DECLARATION_PATH)

    assert read.ok
    assert read.found is False
    assert read.content is None
    assert read.refusal is None


@pytest.mark.asyncio
async def test_a_commit_this_copy_does_not_have_is_a_refusal(project: Path) -> None:
    read = await read_file_at_commit(project, "0" * 40, DECLARATION_PATH)

    assert not read.ok
    assert read.refusal is not None
    assert "does not have the commit" in read.refusal
    assert read.found is False


@pytest.mark.asyncio
async def test_a_file_larger_than_the_bound_is_refused_rather_than_read(
    tmp_path: Path,
) -> None:
    path = tmp_path / "huge"
    make_project(path, "memory:\n  project: fine\n")
    commit = commit_declaration(
        path, "#" * (MAX_FILE_AT_COMMIT_BYTES + 1) + "\n", "a huge settings file"
    )

    read = await read_file_at_commit(path, commit, DECLARATION_PATH)

    assert not read.ok
    assert "larger than" in (read.refusal or "")


@pytest.mark.asyncio
async def test_there_is_no_copy_of_the_project_at_all(tmp_path: Path) -> None:
    runner = WorktreeGitRunner(worktrees_root=tmp_path / "wt")

    read = await runner.read_file_at_commit(
        str(tmp_path / "nothing-here"), "0" * 40, DECLARATION_PATH
    )

    assert not read.ok
    assert "there is no copy of this project" in (read.refusal or "")


@pytest.mark.asyncio
async def test_reading_never_changes_the_branch_or_the_working_folder(
    project: Path,
) -> None:
    commit = _git(project, "rev-parse", "HEAD")
    branch_before = _git(project, "rev-parse", "--abbrev-ref", "HEAD")
    head_before = _git(project, "rev-parse", "HEAD")
    status_before = _git(project, "status", "--porcelain")

    await read_file_at_commit(project, commit, DECLARATION_PATH)

    assert _git(project, "rev-parse", "--abbrev-ref", "HEAD") == branch_before
    assert _git(project, "rev-parse", "HEAD") == head_before
    assert _git(project, "status", "--porcelain") == status_before


# ---------------------------------------------------------------------------
# The three answers, and the sentences
# ---------------------------------------------------------------------------


def _read(content: str | None, *, found: bool = True, because: str | None = None):
    return read_declared_memory(
        repo=REPO_KEY,
        commit="abc1234",
        content=content,
        found=found,
        unreadable_because=because,
    )


def test_a_declared_name_is_the_answer() -> None:
    declared = _read("memory:\n  project: widget_shop\n")

    assert declared.ok
    assert declared.project == "widget_shop"
    assert declared.outcome == "declared"
    assert declared.refusal is None


def test_a_project_that_declares_nothing_is_refused_with_the_two_lines() -> None:
    declared = _read(None, found=False)

    assert not declared.ok
    assert declared.project is None
    assert declared.outcome == "declares-none"
    assert declared.refusal == (
        f"{REPO_KEY} does not say which memory it uses. Its {DECLARATION_PATH} "
        f"declares no memory name at the commit this work starts from "
        f"(abc1234), so a build of it would read no prior decisions and write "
        f"its outcomes nowhere — and this factory will not quietly file them "
        f"under another project's name. Add these two lines to "
        f"{DECLARATION_PATH}, commit them to the branch this work starts from, "
        f"and ask again:\n{THE_TWO_LINES}"
    )
    assert "memory:" in declared.refusal
    assert "project:" in declared.refusal


def test_a_settings_file_with_no_memory_block_declares_nothing() -> None:
    declared = _read("toolchain:\n  install: whatever\n")

    assert declared.outcome == "declares-none"
    assert "does not say which memory it uses" in (declared.refusal or "")


def test_a_memory_block_with_no_project_key_declares_nothing() -> None:
    declared = _read("memory:\n  fleet:\n    context_sources: {}\n")

    assert declared.outcome == "declares-none"


@pytest.mark.parametrize(
    "name",
    ["not a name", "has-a-dash", "has.a.dot", "has/a/slash", "héllo", "name "],
)
def test_a_name_the_memory_service_would_refuse_is_refused_here(name: str) -> None:
    declared = _read(f'memory:\n  project: "{name}"\n')

    if name.strip() != name and name.strip().isalnum():
        # A name that is only padded is accepted stripped, like GuardKit's own.
        assert declared.ok
        return
    assert not declared.ok
    assert declared.outcome == "not-allowed"
    assert "is not allowed" in (declared.refusal or "")
    assert "only letters, digits and underscores" in (declared.refusal or "")
    assert "never rewritten for you" in (declared.refusal or "")
    # It is never quietly made acceptable.
    assert declared.project is None


def test_an_empty_name_is_refused() -> None:
    declared = _read('memory:\n  project: ""\n')

    assert declared.outcome == "not-allowed"
    assert "it is empty" in (declared.refusal or "")


def test_a_name_that_is_not_text_is_refused() -> None:
    declared = _read("memory:\n  project: 17\n")

    assert declared.outcome == "not-allowed"
    assert "it is not text" in (declared.refusal or "")


def test_an_absurdly_long_name_is_refused() -> None:
    declared = _read(f"memory:\n  project: {'a' * 200}\n")

    assert declared.outcome == "not-allowed"
    assert "longer than 128 characters" in (declared.refusal or "")


def test_a_file_that_could_not_be_read_is_never_called_declaring_nothing() -> None:
    declared = _read(None, found=False, because="the sandbox could not be reached")

    assert declared.outcome == "unreadable"
    assert "could not be read" in (declared.refusal or "")
    assert "the sandbox could not be reached" in (declared.refusal or "")
    # The two-lines advice is NOT given: the project may well declare a name.
    assert "add these two lines" not in (declared.refusal or "").lower()


def test_a_settings_file_that_is_not_yaml_is_unreadable_not_undeclared() -> None:
    declared = _read("memory:\n  project: [unclosed\n")

    assert declared.outcome == "unreadable"


def test_a_settings_file_that_is_not_a_mapping_is_unreadable() -> None:
    declared = _read("- one\n- two\n")

    assert declared.outcome == "unreadable"
    assert "not a set of settings" in (declared.refusal or "")


def test_the_name_is_never_rewritten_only_trimmed_of_blanks() -> None:
    declared = _read("memory:\n  project: '  widget_shop  '\n")

    assert declared.ok
    assert declared.project == "widget_shop"


# ---------------------------------------------------------------------------
# The route inside the sandbox
# ---------------------------------------------------------------------------


def _config(paths: dict[str, str]) -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {"target_repo_paths": paths},
        }
    )


def test_the_route_answers_the_file_at_the_commit(project: Path) -> None:
    commit = _git(project, "rev-parse", "HEAD")

    status, body = process_git_read_file_at_commit_request(
        {"repo": REPO_KEY, "commit": commit, "file_path": DECLARATION_PATH},
        config=_config({REPO_KEY: str(project)}),
    )

    assert status == 200
    assert body["found"] is True
    assert body["content"] == "memory:\n  project: widget_shop\n"
    assert body["refusal"] is None


def test_the_route_says_not_found_rather_than_refusing(tmp_path: Path) -> None:
    path = tmp_path / "bare"
    commit = make_project(path, None)

    status, body = process_git_read_file_at_commit_request(
        {"repo": REPO_KEY, "commit": commit, "file_path": DECLARATION_PATH},
        config=_config({REPO_KEY: str(path)}),
    )

    assert status == 200
    assert body["found"] is False
    assert body["refusal"] is None


def test_the_route_refuses_a_repository_it_does_not_serve(project: Path) -> None:
    status, body = process_git_read_file_at_commit_request(
        {"repo": "someone/else", "commit": "a" * 40, "file_path": DECLARATION_PATH},
        config=_config({REPO_KEY: str(project)}),
    )

    assert status == 400
    assert "someone/else" in body["error"]


def test_the_route_refuses_a_commit_it_will_not_pass_to_git(project: Path) -> None:
    status, body = process_git_read_file_at_commit_request(
        {
            "repo": REPO_KEY,
            "commit": "--upload-pack=oops",
            "file_path": DECLARATION_PATH,
        },
        config=_config({REPO_KEY: str(project)}),
    )

    assert status == 400
    assert "commit" in body["error"]


def test_the_route_refuses_a_path_that_walks_out_of_the_tree(project: Path) -> None:
    status, body = process_git_read_file_at_commit_request(
        {
            "repo": REPO_KEY,
            "commit": _git(project, "rev-parse", "HEAD"),
            "file_path": "../../etc/passwd",
        },
        config=_config({REPO_KEY: str(project)}),
    )

    assert status == 400
    assert "file_path" in body["error"]


def test_the_route_refuses_a_body_that_is_not_an_object(project: Path) -> None:
    status, body = process_git_read_file_at_commit_request(
        ["not", "an", "object"], config=_config({REPO_KEY: str(project)})
    )

    assert status == 400
    assert body["error"] == "request body must be a JSON object"


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


def test_the_route_is_on_the_services_allow_list(project: Path) -> None:
    """A real server on a loopback port: the route is served, not a 404."""
    commit = _git(project, "rev-parse", "HEAD")
    server = build_server(
        port=0, config_loader=lambda: _config({REPO_KEY: str(project)})
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        status, body = _post(
            f"{base}{GIT_READ_FILE_AT_COMMIT_ROUTE}",
            {"repo": REPO_KEY, "commit": commit, "file_path": DECLARATION_PATH},
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status == 200
    assert body["content"] == "memory:\n  project: widget_shop\n"


# ---------------------------------------------------------------------------
# The two surfaces that speak to the sandbox
# ---------------------------------------------------------------------------


class _Post:
    """A stand-in for the sandbox's HTTP door: answers what it was given."""

    def __init__(self, answer: Any, status: int = 200) -> None:
        self.answer = answer
        self.status = status
        self.seen: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, url: str, body: dict[str, Any], timeout: float) -> Any:
        self.seen.append((url, body))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.status, self.answer


@pytest.mark.asyncio
async def test_the_sandbox_runner_asks_the_route_and_reads_the_answer() -> None:
    door = _Post({"content": "memory:\n  project: api_test\n", "found": True})
    runner = SidecarGitRunner(base_url="http://sandbox", repo=REPO_KEY, post=door)

    read = await runner.read_file_at_commit("/anywhere", "abc1234", DECLARATION_PATH)

    assert read.ok and read.found
    assert read.content == "memory:\n  project: api_test\n"
    url, body = door.seen[0]
    assert url.endswith(GIT_READ_FILE_AT_COMMIT_ROUTE)
    assert body == {
        "repo": REPO_KEY,
        "commit": "abc1234",
        "file_path": DECLARATION_PATH,
    }


@pytest.mark.asyncio
async def test_a_sandbox_that_cannot_be_reached_is_a_refusal_not_declares_nothing() -> None:
    door = _Post(OSError("connection refused"))
    runner = SidecarGitRunner(base_url="http://sandbox", repo=REPO_KEY, post=door)

    read = await runner.read_file_at_commit("/anywhere", "abc1234", DECLARATION_PATH)

    assert not read.ok
    assert read.found is False
    assert read.refusal is not None
    declared = read_declared_memory(
        repo=REPO_KEY,
        commit="abc1234",
        content=read.content,
        found=read.found,
        unreadable_because=read.refusal,
    )
    assert declared.outcome == "unreadable"


@pytest.mark.asyncio
async def test_the_chooser_routes_this_operation_the_way_it_routes_the_others(
    tmp_path: Path, project: Path
) -> None:
    door = _Post({"content": "memory:\n  project: in_the_sandbox\n", "found": True})
    sandboxed = SidecarGitRunner(base_url="http://sandbox", repo=REPO_KEY, post=door)
    routed = RepoRoutedGitRunner(
        runners_by_repo={REPO_KEY: sandboxed},
        repo_paths={REPO_KEY: str(project)},
        default=WorktreeGitRunner(worktrees_root=tmp_path / "wt"),
    )
    other = tmp_path / "elsewhere"
    make_project(other, "memory:\n  project: in_the_coordinator\n")

    sandbox_answer = await routed.read_file_at_commit(
        str(project), "abc1234", DECLARATION_PATH
    )
    here_answer = await routed.read_file_at_commit(
        str(other), _git(other, "rev-parse", "HEAD"), DECLARATION_PATH
    )

    assert sandbox_answer.content == "memory:\n  project: in_the_sandbox\n"
    assert here_answer.content == "memory:\n  project: in_the_coordinator\n"


def test_the_wire_answer_that_makes_no_sense_is_a_refusal() -> None:
    assert FileAtCommit.from_wire("nonsense").refusal is not None
    assert FileAtCommit.from_wire({"found": True}).refusal is not None
    assert FileAtCommit.from_wire({"found": False}).ok is True

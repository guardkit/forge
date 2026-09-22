"""A new piece of work starts from the project's remote (one true copy, item 1).

These tests drive the REAL first git step of a planning run —
``PlanningRunDriver._enter_target_terminal`` — against a stand-in remote that
is a bare repository on disk. Nobody's real repository is contacted, and the
projects here hold one text file and no code of any kind.

What they hold down:

* the remote is asked FIRST, before any branch is cut;
* the planning branch is cut from the commit the remote had, even when the
  factory's own copy of the project has its own branch three commits away;
* the commit and the branch name are written down on the planning run;
* a project with no remote, or a remote that cannot be reached, is refused in
  plain words and nothing is started;
* a run that already has a starting point recorded keeps it — the target
  branch is decided once.
"""

from __future__ import annotations

import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from forge.adapters.git.planning_runner import WorktreeGitRunner
from forge.config.models import PlanningConfig
from forge.lifecycle import migrations
from forge.planning.driver import PlanningDriverDeps, PlanningRunDriver
from forge.planning.gate_adapters import build_planning_gate_adapters
from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.states import PlanningState

CID = "start-run-0001"
REPO_KEY = "guardkit/api_test"
ORIGINATOR = "U-RICH"


# ---------------------------------------------------------------------------
# A remote on disk, a copy of it, and a driver
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


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {done.stderr}")
    return done.stdout.strip()


def make_remote_and_copy(
    tmp_path: Path, *, default_branch: str = "main"
) -> tuple[Path, Path]:
    remote = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    seed.mkdir(parents=True)
    _git(seed, "init", "-q", "-b", default_branch)
    (seed / "README.md").write_text("one\n", encoding="utf-8")
    # The project says which memory it uses (item 2, 2026-09-21): a project
    # that declares none is refused at the door, and these tests are about the
    # starting rule rather than the memory rule. Two lines, and nothing about
    # what the project is made of.
    (seed / ".guardkit").mkdir()
    (seed / ".guardkit" / "config.yaml").write_text(
        "memory:\n  project: scratch_project\n", encoding="utf-8"
    )
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "one")
    remote.mkdir(parents=True)
    _git(remote, "init", "--bare", "-q", "-b", default_branch)
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-q", "origin", f"HEAD:refs/heads/{default_branch}")
    _git(remote, "symbolic-ref", "HEAD", f"refs/heads/{default_branch}")
    copy = tmp_path / "api_test"
    _git(tmp_path, "clone", "-q", str(remote), str(copy))
    return remote, copy


def advance_remote(remote: Path, tmp_path: Path, message: str) -> str:
    work = tmp_path / f"writer-{message}"
    _git(tmp_path, "clone", "-q", str(remote), str(work))
    branch = _git(work, "rev-parse", "--abbrev-ref", "HEAD")
    (work / f"{message}.txt").write_text(message, encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-qm", message)
    _git(work, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
    return _git(work, "rev-parse", "HEAD")


def diverge_the_copy(copy: Path, commits: int = 3) -> str:
    for index in range(commits):
        (copy / f"local-{index}.txt").write_text(str(index), encoding="utf-8")
        _git(copy, "add", ".")
        _git(copy, "commit", "-qm", f"local {index}")
    return _git(copy, "rev-parse", "HEAD")


@pytest.fixture
def store(tmp_path: Path) -> SqlitePlanningRunStore:
    connection = sqlite3.connect(str(tmp_path / "start.db"))
    connection.row_factory = sqlite3.Row
    migrations.apply_at_boot(connection)
    return SqlitePlanningRunStore(connection, target_terminal_enabled=True)


class _Harness:
    def __init__(self, driver: PlanningRunDriver, notifications: list[Any]) -> None:
        self.driver = driver
        self.notifications = notifications

    @property
    def errors(self) -> list[str]:
        return [message for _, message, level in self.notifications if level == "error"]


def _make_driver(
    store: SqlitePlanningRunStore,
    *,
    repo_path: Path,
    worktrees_root: Path,
    git_runner: Any | None = None,
) -> _Harness:
    clock = lambda: datetime.now(timezone.utc)  # noqa: E731
    repository, state_machine = build_planning_gate_adapters(store, clock=clock)
    notifications: list[tuple[str, str, str]] = []

    async def publish_notification(cid: str, message: str, level: str) -> None:
        notifications.append((cid, message, level))

    async def dispatch_po(*, plan_run_id: str, correlation_id: str) -> Any:
        raise AssertionError("no product-owner dispatch in this test")

    config = PlanningConfig(
        enabled=True,
        escalation_approver="U-ESC",
        originator_wait_seconds=300,
        escalated_wait_seconds=1800,
        target_repo_paths={REPO_KEY: str(repo_path)},
        target_terminal={"enabled": True},
    )
    driver = PlanningRunDriver(
        PlanningDriverDeps(
            store=store,
            repository=repository,
            state_machine=state_machine,
            approval_publisher=object(),
            subscriber_factory=lambda expected_approver, armed: object(),
            dispatch_product_owner=dispatch_po,
            second_opinion_provider=object(),
            git_runner=git_runner or WorktreeGitRunner(worktrees_root=worktrees_root),
            planning_config=config,
            clock=clock,
            publish_notification=publish_notification,
        )
    )
    return _Harness(driver, notifications)


def _queue_running(store: SqlitePlanningRunStore) -> sqlite3.Row:
    store.record_queued(
        correlation_id=CID,
        originating_user=ORIGINATOR,
        expected_approver=ORIGINATOR,
        request_text="add a sentence to the project",
        triggered_by="cli",
        target_repo=REPO_KEY,
    )
    store.transition(
        correlation_id=CID,
        to_state=PlanningState.RUNNING,
        actor_identity="test",
        expected_from_state=PlanningState.QUEUED,
    )
    row = store.get_run(CID)
    assert row is not None
    return row


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_planning_branch_is_cut_from_the_remotes_commit(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    remote, copy = make_remote_and_copy(tmp_path)
    local_head = diverge_the_copy(copy)
    remote_commit = _git(remote, "rev-parse", "refs/heads/main")
    assert local_head != remote_commit
    h = _make_driver(store, repo_path=copy, worktrees_root=tmp_path / "wt")
    row = _queue_running(store)

    assert await h.driver._enter_target_terminal(row, CID) is True

    first_commit_of_the_branch = _git(copy, "rev-parse", f"planning/{CID}")
    assert _git(copy, "rev-parse", f"planning/{CID}^") == remote_commit
    assert first_commit_of_the_branch != local_head
    assert store.get_run(CID)["state"] == PlanningState.FEATURE_SPEC.value


@pytest.mark.asyncio
async def test_the_ledger_holds_the_commit_and_the_branch_name(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    remote, copy = make_remote_and_copy(tmp_path, default_branch="trunk")
    h = _make_driver(store, repo_path=copy, worktrees_root=tmp_path / "wt")
    row = _queue_running(store)

    assert await h.driver._enter_target_terminal(row, CID) is True

    commit, branch = store.get_start_point(CID)
    assert commit == _git(remote, "rev-parse", "refs/heads/trunk")
    assert branch == "trunk"


@pytest.mark.asyncio
async def test_a_second_run_after_the_remote_moves_starts_from_the_new_commit(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    remote, copy = make_remote_and_copy(tmp_path)
    h = _make_driver(store, repo_path=copy, worktrees_root=tmp_path / "wt")
    row = _queue_running(store)
    assert await h.driver._enter_target_terminal(row, CID) is True
    first_commit, _ = store.get_start_point(CID)

    moved = advance_remote(remote, tmp_path, "theirs")
    second_cid = "start-run-0002"
    store.record_queued(
        correlation_id=second_cid,
        originating_user=ORIGINATOR,
        expected_approver=ORIGINATOR,
        request_text="another sentence",
        triggered_by="cli",
        target_repo=REPO_KEY,
    )
    store.transition(
        correlation_id=second_cid,
        to_state=PlanningState.RUNNING,
        actor_identity="test",
        expected_from_state=PlanningState.QUEUED,
    )
    second_row = store.get_run(second_cid)

    assert await h.driver._enter_target_terminal(second_row, second_cid) is True

    second_commit, _ = store.get_start_point(second_cid)
    assert second_commit == moved
    assert second_commit != first_commit
    assert _git(copy, "rev-parse", f"planning/{second_cid}^") == moved


@pytest.mark.asyncio
async def test_a_re_drive_keeps_the_commit_the_branch_was_already_cut_from(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """The target branch is decided once: a second pass does not quietly move
    the run onto a newer commit than the branch it already cut."""
    remote, copy = make_remote_and_copy(tmp_path)
    h = _make_driver(store, repo_path=copy, worktrees_root=tmp_path / "wt")
    row = _queue_running(store)
    assert await h.driver._enter_target_terminal(row, CID) is True
    recorded = store.get_start_point(CID)
    advance_remote(remote, tmp_path, "theirs")

    store.transition(
        correlation_id=CID,
        to_state=PlanningState.RUNNING,
        actor_identity="test",
        expected_from_state=PlanningState.FEATURE_SPEC,
    )
    await h.driver._enter_target_terminal(store.get_run(CID), CID)

    assert store.get_start_point(CID) == recorded
    assert _git(copy, "rev-parse", f"planning/{CID}^") == recorded[0]


# ---------------------------------------------------------------------------
# The refusals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_project_with_no_remote_is_refused_and_nothing_is_started(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    alone = tmp_path / "api_test"
    alone.mkdir()
    _git(alone, "init", "-q")
    (alone / "README.md").write_text("alone\n", encoding="utf-8")
    _git(alone, "add", ".")
    _git(alone, "commit", "-qm", "one")
    h = _make_driver(store, repo_path=alone, worktrees_root=tmp_path / "wt")
    row = _queue_running(store)

    assert await h.driver._enter_target_terminal(row, CID) is False

    run = store.get_run(CID)
    assert run["state"] == PlanningState.FAILED.value
    assert run["error"] == (
        f"the copy of this project at {alone} has no remote named 'origin', "
        f"so there is nothing to start the work from. Add that remote to the "
        f"copy, then ask again."
    )
    assert h.errors == [
        f"Planning run {CID} stopped at starting the machine chain: "
        f"the copy of this project at {alone} has no remote named 'origin', "
        f"so there is nothing to start the work from. Add that remote to the "
        f"copy, then ask again."
    ]
    # Nothing was cut and nothing was recorded.
    branches = _git(alone, "for-each-ref", "--format=%(refname)", "refs/heads")
    assert f"planning/{CID}" not in branches
    assert store.get_start_point(CID) == (None, None)


@pytest.mark.asyncio
async def test_a_remote_that_cannot_be_reached_is_refused_in_plain_words(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    remote, copy = make_remote_and_copy(tmp_path)
    _git(copy, "remote", "set-url", "origin", str(tmp_path / "not-here.git"))
    h = _make_driver(store, repo_path=copy, worktrees_root=tmp_path / "wt")
    row = _queue_running(store)

    assert await h.driver._enter_target_terminal(row, CID) is False

    run = store.get_run(CID)
    assert run["state"] == PlanningState.FAILED.value
    assert run["error"].startswith(
        f"the remote named 'origin' could not be reached from {copy}, so the "
        f"work cannot be started from it: "
    )
    assert h.errors and h.errors[0].startswith(
        f"Planning run {CID} stopped at starting the machine chain: the "
        f"remote named 'origin' could not be reached"
    )


@pytest.mark.asyncio
async def test_a_git_runner_that_cannot_fetch_stops_the_run_rather_than_guessing(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """An older runner with no starting-rule operation is a loud stop, never a
    quiet fall back to whatever the copy has checked out."""
    _remote, copy = make_remote_and_copy(tmp_path)

    class _OlderRunner:
        async def prepare_branch_and_write(self, **kwargs: Any) -> Any:
            raise AssertionError("nothing should be written")

    h = _make_driver(
        store,
        repo_path=copy,
        worktrees_root=tmp_path / "wt",
        git_runner=_OlderRunner(),
    )
    row = _queue_running(store)

    assert await h.driver._enter_target_terminal(row, CID) is False

    assert store.get_run(CID)["error"] == (
        "the git runner wired for this factory cannot fetch a project's "
        "remote, so there is no way to start the work from the commit that "
        "remote holds"
    )


@pytest.mark.asyncio
async def test_the_remote_is_asked_before_anything_is_written(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """The order, pinned: the fetch happens first, and its commit is what the
    write is given."""
    _remote, copy = make_remote_and_copy(tmp_path)
    real = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    order: list[str] = []

    class _Watching:
        async def fetch_remote_start_point(self, repo_path: str) -> Any:
            order.append("fetch")
            return await real.fetch_remote_start_point(repo_path)

        async def read_file_at_commit(
            self, repo_path: str, commit: str, file_path: str
        ) -> Any:
            # The memory rule's read (item 2) sits BETWEEN the two, and it
            # reads at the commit the fetch just answered.
            order.append(f"read {file_path} at {commit}")
            return await real.read_file_at_commit(repo_path, commit, file_path)

        async def prepare_branch_and_write(self, **kwargs: Any) -> Any:
            order.append(f"write from {kwargs.get('start_commit')}")
            return await real.prepare_branch_and_write(**kwargs)

    h = _make_driver(
        store, repo_path=copy, worktrees_root=tmp_path / "wt", git_runner=_Watching()
    )
    row = _queue_running(store)

    assert await h.driver._enter_target_terminal(row, CID) is True

    commit, _branch = store.get_start_point(CID)
    assert order == [
        "fetch",
        f"read .guardkit/config.yaml at {commit}",
        f"write from {commit}",
    ]

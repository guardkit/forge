"""Forge refuses at the door when a project does not name its memory (item 2).

These tests drive the REAL first git step of a planning run —
``PlanningRunDriver._enter_target_terminal`` — against a stand-in remote that is
a bare repository on disk. Nobody's real repository is contacted, no memory
service, database, embedder or broker is reached, and the projects here hold one
settings file and one text file with no code of any kind.

What they hold down:

* the memory name is read from the project's own ``.guardkit/config.yaml`` **as
  it is at the recorded starting commit** — the working folder can say something
  else entirely and is not consulted;
* a project that declares no name is refused in plain words naming the two lines
  to add, through the same publisher every planning refusal uses, and nothing is
  started;
* a name the memory service would refuse is refused here, and never rewritten;
* the name is written down on the planning run, and read back onto the build;
* a run that already has a name keeps it.
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
from forge.planning.declared_memory import DECLARATION_PATH
from forge.planning.driver import PlanningDriverDeps, PlanningRunDriver
from forge.planning.gate_adapters import build_planning_gate_adapters
from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.states import PlanningState

CID = "memory-run-0001"
REPO_KEY = "guardkit/api_test"
ORIGINATOR = "U-RICH"

DECLARES_WIDGET_SHOP = "memory:\n  project: widget_shop\n"


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
    tmp_path: Path, *, declaration: str | None
) -> tuple[Path, Path]:
    """A stand-in remote whose commit carries ``declaration`` (or nothing), and
    a copy of it."""
    remote = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    seed.mkdir(parents=True)
    _git(seed, "init", "-q", "-b", "main")
    (seed / "README.md").write_text("one\n", encoding="utf-8")
    if declaration is not None:
        (seed / ".guardkit").mkdir()
        (seed / DECLARATION_PATH).write_text(declaration, encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "one")
    remote.mkdir(parents=True)
    _git(remote, "init", "--bare", "-q", "-b", "main")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-q", "origin", "HEAD:refs/heads/main")
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    copy = tmp_path / "api_test"
    _git(tmp_path, "clone", "-q", str(remote), str(copy))
    return remote, copy


@pytest.fixture
def store(tmp_path: Path) -> SqlitePlanningRunStore:
    connection = sqlite3.connect(str(tmp_path / "memory.db"))
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


def _queue_running(store: SqlitePlanningRunStore, cid: str = CID) -> sqlite3.Row:
    store.record_queued(
        correlation_id=cid,
        originating_user=ORIGINATOR,
        expected_approver=ORIGINATOR,
        request_text="add a sentence to the project",
        triggered_by="cli",
        target_repo=REPO_KEY,
    )
    store.transition(
        correlation_id=cid,
        to_state=PlanningState.RUNNING,
        actor_identity="test",
        expected_from_state=PlanningState.QUEUED,
    )
    row = store.get_run(cid)
    assert row is not None
    return row


# ---------------------------------------------------------------------------
# The declared name is read, at the commit, and written down
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_declared_name_is_recorded_on_the_run(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    _remote, copy = make_remote_and_copy(tmp_path, declaration=DECLARES_WIDGET_SHOP)
    h = _make_driver(store, repo_path=copy, worktrees_root=tmp_path / "wt")
    row = _queue_running(store)

    assert await h.driver._enter_target_terminal(row, CID) is True

    assert store.get_memory_project(CID) == "widget_shop"
    assert h.errors == []


@pytest.mark.asyncio
async def test_the_name_comes_from_the_commit_not_the_working_folder(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """The copy's working folder declares one thing; the commit the work starts
    from declares another. The commit's name is the one recorded."""
    _remote, copy = make_remote_and_copy(tmp_path, declaration=DECLARES_WIDGET_SHOP)
    (copy / DECLARATION_PATH).write_text(
        "memory:\n  project: whatever_is_lying_about\n", encoding="utf-8"
    )
    h = _make_driver(store, repo_path=copy, worktrees_root=tmp_path / "wt")
    row = _queue_running(store)

    assert await h.driver._enter_target_terminal(row, CID) is True

    assert store.get_memory_project(CID) == "widget_shop"
    assert "whatever_is_lying_about" in (copy / DECLARATION_PATH).read_text()


@pytest.mark.asyncio
async def test_the_name_is_read_before_the_branch_is_cut(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """A refusal leaves nothing behind: no branch, and a run that is FAILED."""
    _remote, copy = make_remote_and_copy(tmp_path, declaration=None)
    h = _make_driver(store, repo_path=copy, worktrees_root=tmp_path / "wt")
    row = _queue_running(store)

    assert await h.driver._enter_target_terminal(row, CID) is False

    branches = _git(copy, "branch", "--list", f"planning/{CID}")
    assert branches == ""
    assert store.get_run(CID)["state"] == PlanningState.FAILED.value


# ---------------------------------------------------------------------------
# The refusals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_project_that_declares_no_memory_is_refused_with_the_two_lines(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    _remote, copy = make_remote_and_copy(tmp_path, declaration=None)
    h = _make_driver(store, repo_path=copy, worktrees_root=tmp_path / "wt")
    row = _queue_running(store)

    assert await h.driver._enter_target_terminal(row, CID) is False

    assert len(h.errors) == 1
    said = h.errors[0]
    assert "does not say which memory it uses" in said
    assert ".guardkit/config.yaml" in said
    # THE TWO LINES, named where the owner can read them.
    assert "memory:" in said
    assert "project: <a name of letters, digits and underscores>" in said
    assert store.get_memory_project(CID) is None


@pytest.mark.asyncio
async def test_a_settings_file_with_no_memory_block_is_refused_the_same_way(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    _remote, copy = make_remote_and_copy(
        tmp_path, declaration="toolchain:\n  install: whatever\n"
    )
    h = _make_driver(store, repo_path=copy, worktrees_root=tmp_path / "wt")
    row = _queue_running(store)

    assert await h.driver._enter_target_terminal(row, CID) is False
    assert "does not say which memory it uses" in h.errors[0]


@pytest.mark.asyncio
async def test_a_name_the_memory_service_would_refuse_is_refused_here(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    _remote, copy = make_remote_and_copy(
        tmp_path, declaration="memory:\n  project: widget shop!\n"
    )
    h = _make_driver(store, repo_path=copy, worktrees_root=tmp_path / "wt")
    row = _queue_running(store)

    assert await h.driver._enter_target_terminal(row, CID) is False

    said = h.errors[0]
    assert "is not allowed" in said
    assert "only letters, digits and underscores" in said
    assert "never rewritten for you" in said
    # Nothing was quietly filed under a made-up name.
    assert store.get_memory_project(CID) is None
    assert _git(copy, "branch", "--list", f"planning/{CID}") == ""


@pytest.mark.asyncio
async def test_a_venue_that_cannot_read_the_file_is_not_told_it_declares_nothing(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """A sandbox that could not be reached must not produce "add these two
    lines" — the project may well declare a name."""
    _remote, copy = make_remote_and_copy(tmp_path, declaration=DECLARES_WIDGET_SHOP)

    class _Unreachable(WorktreeGitRunner):
        async def read_file_at_commit(self, repo_path, commit, file_path):  # type: ignore[override]
            from forge.deploy.candidate_tree import FileAtCommit

            return FileAtCommit(refusal="the sandbox could not be reached")

    h = _make_driver(
        store,
        repo_path=copy,
        worktrees_root=tmp_path / "wt",
        git_runner=_Unreachable(worktrees_root=tmp_path / "wt"),
    )
    row = _queue_running(store)

    assert await h.driver._enter_target_terminal(row, CID) is False

    said = h.errors[0]
    assert "could not be read" in said
    assert "the sandbox could not be reached" in said
    assert "add these two lines" not in said.lower()


@pytest.mark.asyncio
async def test_a_runner_that_raises_stops_the_run_rather_than_guessing(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    _remote, copy = make_remote_and_copy(tmp_path, declaration=DECLARES_WIDGET_SHOP)

    class _Explodes(WorktreeGitRunner):
        async def read_file_at_commit(self, repo_path, commit, file_path):  # type: ignore[override]
            raise RuntimeError("boom")

    h = _make_driver(
        store,
        repo_path=copy,
        worktrees_root=tmp_path / "wt",
        git_runner=_Explodes(worktrees_root=tmp_path / "wt"),
    )
    row = _queue_running(store)

    assert await h.driver._enter_target_terminal(row, CID) is False
    assert "RuntimeError: boom" in h.errors[0]
    assert store.get_memory_project(CID) is None


# ---------------------------------------------------------------------------
# Decided once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_run_that_already_has_a_name_keeps_it(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """A re-drive of this leg does not move a run onto a different memory than
    the one its branch was cut under, even if the project has since changed its
    declaration."""
    _remote, copy = make_remote_and_copy(tmp_path, declaration=DECLARES_WIDGET_SHOP)
    h = _make_driver(store, repo_path=copy, worktrees_root=tmp_path / "wt")
    row = _queue_running(store)
    assert await h.driver._enter_target_terminal(row, CID) is True
    assert store.get_memory_project(CID) == "widget_shop"

    asked: list[Any] = []

    class _Watching(WorktreeGitRunner):
        async def read_file_at_commit(self, repo_path, commit, file_path):  # type: ignore[override]
            asked.append((repo_path, commit, file_path))
            return await super().read_file_at_commit(repo_path, commit, file_path)

    again = _make_driver(
        store,
        repo_path=copy,
        worktrees_root=tmp_path / "wt",
        git_runner=_Watching(worktrees_root=tmp_path / "wt"),
    )
    await again.driver._enter_target_terminal(store.get_run(CID), CID)

    assert asked == []
    assert store.get_memory_project(CID) == "widget_shop"

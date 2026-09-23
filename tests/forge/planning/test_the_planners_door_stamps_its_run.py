"""The planner's door is a factory door, and it says whose work it is.

Until 23 September 2026 the planning chain's writes went to the helper in the
repository's sandbox wearing ``by_hand: true`` — the label a person at a
keyboard wears, which asks the helper to read the project's own declaration
files at the committed HEAD of whatever copy it happens to have. The reason
written beside it was that planning runs before there is a build to name.

That was wrong about this factory's own records. The driver writes the run's
starting commit onto the planning run BEFORE it writes any tree, and reads the
memory name and the declared setting names off that same run. So the run knows
whose work it is and where its declarations were said, and this file holds the
door to saying both:

* the door sends ``build`` (the id this factory keeps the run under) and
  ``declared_at`` (the commit recorded for that run), and no ``by_hand``;
* the REAL helper reads the declaration at the commit the RECORD names — the
  project here declares a setting at its first commit and takes it away at its
  second, so a write that is admitted was read where the record says and
  nowhere else;
* a commit that is not the recorded one is refused, and so is a request with
  the stamp taken off it on the way out;
* a run with no recorded starting commit and a declaration to read is refused
  at the door, before a file is written or a check is launched, with the
  sentence that says how to recover;
* a write that names nothing declared asks the project for nothing, so it is
  bound to nothing and served even with no coordinator to ask.

Nothing real is contacted. The helper, the coordinator's read-only answer and
the record they read are all children of this process: two servers on 127.0.0.1
on ports the kernel picks, and a throwaway SQLite file made by Forge's own
migrations under the test's own temporary directory. Every repository here is a
throwaway git repository in the same place.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.config.models import ForgeConfig
from forge.deploy_sidecar.service import COORDINATOR_OWNER_ENV, build_server
from forge.lifecycle import migrations
from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.sidecar_git_runner import SidecarGitRunner
from forge.record_answer.service import ANSWER_ROUTE, serve

#: A repository, a planning run and a declared name that exist nowhere but here.
REPO = "acme/widget-shop"
THE_RUN = "corr-planner-0001"
DECLARED_SETTING = "SOME_TOOL_CACHE"
THE_MEMORY = "widget_shop"
BRANCH = f"planning/{THE_RUN}"
THE_FILES = {"docs/handoff.md": "the plan\n"}


def _git(where: Path, *args: str) -> str:
    done = subprocess.run(
        [
            "git",
            "-c", "user.email=tests@example.invalid",
            "-c", "user.name=tests",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        cwd=str(where),
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A repository that declares a setting AT ONE COMMIT and not at its tip.

    That is what makes "read at the commit the record names" a testable claim
    rather than an opinion: a write that is admitted with this name on it can
    only have been read at the first commit, because the second takes the
    declaration away.
    """
    root = tmp_path / "widget-shop"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    declaration = root / ".guardkit" / "config.yaml"
    declaration.parent.mkdir(parents=True, exist_ok=True)
    declaration.write_text(
        f"memory:\n  project: {THE_MEMORY}\nlaunch:\n  settings: [{DECLARED_SETTING}]\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text("first\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "the project as it was when the run started")
    declaration.write_text(f"memory:\n  project: {THE_MEMORY}\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "the declaration is taken away")
    return root


@pytest.fixture
def where_the_run_starts(project: Path) -> str:
    """The commit this factory recorded the run as starting from."""
    return _git(project, "rev-parse", "HEAD~1")


@pytest.fixture
def record(tmp_path: Path, where_the_run_starts: str) -> Path:
    """This factory's own record, with one planning run and its start point."""
    db = tmp_path / "record" / "forge.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    cx = sqlite_connect.connect_writer(db)
    migrations.apply_at_boot(cx)
    store = SqlitePlanningRunStore(cx)
    store.record_queued(
        correlation_id=THE_RUN,
        originating_user="U1",
        expected_approver="U1",
        request_text="a sentence",
        triggered_by="cli",
        target_repo=REPO,
    )
    assert store.record_start_point(
        THE_RUN, start_commit=where_the_run_starts, target_branch="main"
    )
    cx.close()
    return db


@pytest.fixture
def the_records_answer(record: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The REAL read-only answer, serving this record on loopback."""
    server, _thread = serve(ledger=record, host="127.0.0.1", port=0)
    host, port = server.server_address[:2]
    monkeypatch.setenv(COORDINATOR_OWNER_ENV, f"http://{host}:{port}{ANSWER_ROUTE}")
    try:
        yield f"http://{host}:{port}{ANSWER_ROUTE}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def helper(project: Path) -> Any:
    """The REAL helper, serving this project's repository on loopback."""
    config = ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": [str(project.parent)]}},
            "planning": {"target_repo_paths": {REPO: str(project)}},
        }
    )
    server = build_server(port=0, config_loader=lambda: config)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _branch_exists(project: Path, branch: str) -> bool:
    done = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", branch],
        cwd=str(project),
        capture_output=True,
        text=True,
    )
    return done.returncode == 0


# ---------------------------------------------------------------------------
# What goes on the wire
# ---------------------------------------------------------------------------


class _ARecordingWire:
    """The one HTTP seam, recorded, answering as a contented helper would."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []

    def __call__(
        self, url: str, body: dict[str, Any], timeout: float
    ) -> tuple[int, Any]:
        self.bodies.append(dict(body))
        return 200, {"status": "success", "sha": "a" * 40, "checks": [], "detail": ""}


@pytest.mark.asyncio
async def test_the_door_sends_the_run_and_the_recorded_commit_and_no_claim(
    where_the_run_starts: str,
) -> None:
    """The label a person wears is gone, and the run's own two facts are there."""
    wire = _ARecordingWire()
    runner = SidecarGitRunner("http://127.0.0.1:9", repo=REPO, post=wire)

    result = await runner.prepare_branch_and_write_tree(
        "/ignored/on/this/side",
        BRANCH,
        THE_FILES,
        "planning: the handoff",
        memory_project=THE_MEMORY,
        launch_settings=[DECLARED_SETTING],
        build=THE_RUN,
        declared_at=where_the_run_starts,
    )

    assert result.status == "success"
    assert len(wire.bodies) == 1
    sent = wire.bodies[0]
    assert sent["build"] == THE_RUN
    assert sent["declared_at"] == where_the_run_starts
    assert "by_hand" not in sent
    assert sent["memory_project"] == THE_MEMORY
    assert sent["launch_settings"] == [DECLARED_SETTING]


# ---------------------------------------------------------------------------
# The REAL helper, against this factory's own record
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_declaration_is_read_where_the_record_says(
    helper: str,
    project: Path,
    where_the_run_starts: str,
    the_records_answer: str,
) -> None:
    """Served, and served at the recorded commit rather than at HEAD.

    The name on this request is declared at the recorded commit and NOT at the
    tip of the copy the helper has, so an admitted write is the proof: had the
    helper read at its own HEAD, the name would have been refused as one this
    project does not declare.
    """
    runner = SidecarGitRunner(helper, repo=REPO)

    result = await runner.prepare_branch_and_write_tree(
        str(project),
        BRANCH,
        THE_FILES,
        "planning: the handoff",
        memory_project=THE_MEMORY,
        launch_settings=[DECLARED_SETTING],
        build=THE_RUN,
        declared_at=where_the_run_starts,
    )

    assert result.status == "success", result.detail
    assert _branch_exists(project, BRANCH)


@pytest.mark.asyncio
async def test_a_commit_that_is_not_the_recorded_one_is_refused(
    helper: str,
    project: Path,
    where_the_run_starts: str,
    the_records_answer: str,
) -> None:
    """A request does not choose the commit its own declarations are read at."""
    runner = SidecarGitRunner(helper, repo=REPO)

    result = await runner.prepare_branch_and_write_tree(
        str(project),
        BRANCH,
        THE_FILES,
        "planning: the handoff",
        memory_project=THE_MEMORY,
        launch_settings=[DECLARED_SETTING],
        build=THE_RUN,
        declared_at=_git(project, "rev-parse", "HEAD"),
    )

    assert result.status == "failed"
    assert where_the_run_starts in result.detail
    assert not _branch_exists(project, BRANCH)


@pytest.mark.asyncio
async def test_a_write_with_the_stamp_taken_off_it_is_refused_not_served(
    helper: str,
    project: Path,
    where_the_run_starts: str,
    the_records_answer: str,
) -> None:
    """The mutation: the same write, with the two facts deleted on the way out.

    This is the shape a dropped stamp takes, and it is the shape this door sent
    on purpose until today. The helper must refuse it rather than read the
    declaration at its own HEAD.
    """
    from forge.planning.sidecar_git_runner import _urllib_post

    def strips_the_stamp(
        url: str, body: dict[str, Any], timeout: float
    ) -> tuple[int, Any]:
        stripped = {k: v for k, v in body.items() if k not in ("build", "declared_at")}
        return _urllib_post(url, stripped, timeout)

    runner = SidecarGitRunner(helper, repo=REPO, post=strips_the_stamp)

    result = await runner.prepare_branch_and_write_tree(
        str(project),
        BRANCH,
        THE_FILES,
        "planning: the handoff",
        memory_project=THE_MEMORY,
        launch_settings=[DECLARED_SETTING],
        build=THE_RUN,
        declared_at=where_the_run_starts,
    )

    assert result.status == "failed"
    assert "by_hand" in result.detail
    assert not _branch_exists(project, BRANCH)


@pytest.mark.asyncio
async def test_a_run_with_no_recorded_commit_is_refused_before_anything_is_written(
    helper: str, project: Path, the_records_answer: str
) -> None:
    """Nothing is sent, nothing is written, and the sentence says how to recover."""
    wire = _ARecordingWire()
    watched = SidecarGitRunner(helper, repo=REPO, post=wire)

    result = await watched.prepare_branch_and_write_tree(
        str(project),
        BRANCH,
        THE_FILES,
        "planning: the handoff",
        memory_project=THE_MEMORY,
        launch_settings=[DECLARED_SETTING],
        build=THE_RUN,
        declared_at=None,
    )

    assert result.status == "failed"
    assert wire.bodies == []
    assert "target-terminal" in result.detail
    assert "HEAD" in result.detail
    assert not _branch_exists(project, BRANCH)


@pytest.mark.asyncio
async def test_a_write_that_names_nothing_declared_is_served_with_nobody_to_ask(
    helper: str,
    project: Path,
    where_the_run_starts: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stamp is a label, and a label alone is not a question for anybody.

    This write asks the project for nothing — no memory, no setting name — so
    there is no declaration to read and no commit to read it at. It carries its
    run and the recorded commit all the same, because that is how a log says
    whose work ran a check, and it is served with no coordinator anywhere.
    """
    monkeypatch.delenv(COORDINATOR_OWNER_ENV, raising=False)
    runner = SidecarGitRunner(helper, repo=REPO)

    result = await runner.prepare_branch_and_write_tree(
        str(project),
        BRANCH,
        THE_FILES,
        "planning: the handoff",
        build=THE_RUN,
        declared_at=where_the_run_starts,
    )

    assert result.status == "success", result.detail
    assert _branch_exists(project, BRANCH)


# ---------------------------------------------------------------------------
# Where the door's two facts come from: this factory's own record of the run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_driver_reads_the_run_and_its_start_point_off_the_record(
    tmp_path: Path,
) -> None:
    """The driver's own read, driven through the real first git step.

    ``_enter_target_terminal`` is the step that fetches the project's remote and
    writes the starting commit onto the run. What the write legs then hand the
    door is read back off that same run: the run's own id, and that commit.
    """
    from tests.forge.planning.test_memory_name_at_the_door import (
        DECLARES_WIDGET_SHOP,
        _make_driver,
        _queue_running,
        make_remote_and_copy,
    )

    connection = sqlite_connect.connect_writer(tmp_path / "planning.db")
    migrations.apply_at_boot(connection)
    store = SqlitePlanningRunStore(connection, target_terminal_enabled=True)
    remote, copy = make_remote_and_copy(tmp_path, declaration=DECLARES_WIDGET_SHOP)
    harness = _make_driver(store, repo_path=copy, worktrees_root=tmp_path / "wt")
    row = _queue_running(store)
    cid = str(row["correlation_id"])

    assert await harness.driver._enter_target_terminal(row, cid) is True

    memory, settings, whose_work, declared_at = harness.driver._recorded_launch(cid)

    assert memory == "widget_shop"
    assert settings == ()
    assert whose_work == cid
    assert declared_at == _git(remote, "rev-parse", "main")
    assert declared_at == store.get_start_point(cid)[0]

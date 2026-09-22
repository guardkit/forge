"""The door runs before any model is asked anything (22 September 2026).

The stage's second independent review found the door in the wrong place. The
starting rule and the memory rule lived inside the leg that CUTS THE BRANCH,
which runs after the product-owner dispatch — so a project that declares no
memory was refused, correctly, but only after a model had already been asked to
do work, with neither the starting commit nor the memory name recorded when
that call was made. A door that opens after the first dispatch is not a door.

Every test here drives the PUBLIC entry point, :meth:`PlanningRunDriver.drive`,
exactly as the daemon does — never a private method — and the model is a
stand-in that counts how many times it was called. The remote is a bare
repository in a temporary folder: real git, nobody's account. No memory
service, database, embedder or broker is reached, and the projects hold one
settings file and one text file with no code of any kind.

What they hold down:

* a project that declares no memory: the model stand-in records ZERO calls, the
  run ends refused, and the owner is told in plain words;
* the same for a settings file that cannot be parsed at all — the review's own
  1.2 KB deeply nested declaration, which used to raise a ``RecursionError``
  past the one thing being caught and leave the run RUNNING with nobody told;
* the same for a project that asks to be launched with a setting name this
  factory keeps for itself;
* nothing is cut in any of those cases, and what IS recorded is only where the
  work would have started;
* a project that declares everything properly passes the door, and the door's
  own facts — the starting commit, the memory name, the setting names — are all
  written down BEFORE the first dispatch.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from forge.config.models import PlanningConfig
from forge.planning.driver import PlanningDriverDeps, PlanningRunDriver
from forge.planning.gate_adapters import build_planning_gate_adapters
from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.states import PlanningState
from forge.adapters.git.planning_runner import WorktreeGitRunner

from tests.forge.planning.test_memory_name_at_the_door import (  # noqa: E402
    CID,
    DECLARES_WIDGET_SHOP,
    ORIGINATOR,
    REPO_KEY,
    _git,
    make_remote_and_copy,
    store,  # noqa: F401 — the fixture
)

#: The review's own input: about 1.2 KB, nested deeply enough that the parser
#: runs out of its own stack. Written here rather than described, so the thing
#: that broke the run is the thing under test.
THE_NESTED_DECLARATION = "memory:\n  project: " + "[" * 600 + "]" * 600

#: A project that asks to be launched with a name the factory keeps for itself.
ASKS_FOR_A_RESERVED_NAME = (
    "memory:\n"
    "  project: widget_shop\n"
    "launch:\n"
    "  settings: [FORGE_DB_PATH]\n"
)

#: A project that asks for two names of its own, which is the whole point.
ASKS_FOR_ITS_OWN_TWO = (
    "memory:\n"
    "  project: widget_shop\n"
    "launch:\n"
    "  settings: [SOME_TOOL_CACHE, ANOTHER_HOME]\n"
)


class _ModelStandIn:
    """Stands in for the product-owner dispatch and counts what it was asked.

    It never returns a plan: a run that reaches it in these tests has already
    failed the thing being tested, and the count is the assertion.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, *, plan_run_id: str, correlation_id: str, **_: Any) -> Any:
        self.calls.append(correlation_id)
        raise AssertionError(
            "the model was asked to work before the door had finished: "
            f"{len(self.calls)} call(s)"
        )


def _driver_with_a_model(
    store: SqlitePlanningRunStore,
    *,
    repo_path: Path,
    worktrees_root: Path,
) -> tuple[PlanningRunDriver, _ModelStandIn, list[tuple[str, str, str]]]:
    clock = __import__("datetime").datetime.now
    from datetime import timezone

    def _clock():
        return clock(timezone.utc)

    repository, state_machine = build_planning_gate_adapters(store, clock=_clock)
    notifications: list[tuple[str, str, str]] = []

    async def publish_notification(cid: str, message: str, level: str) -> None:
        notifications.append((cid, message, level))

    model = _ModelStandIn()
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
            dispatch_product_owner=model,
            second_opinion_provider=object(),
            git_runner=WorktreeGitRunner(worktrees_root=worktrees_root),
            planning_config=config,
            clock=_clock,
            publish_notification=publish_notification,
        )
    )
    return driver, model, notifications


def _queue(store: SqlitePlanningRunStore, cid: str = CID) -> None:
    """Queue a run and leave it QUEUED — the state the daemon drives from."""
    store.record_queued(
        correlation_id=cid,
        originating_user=ORIGINATOR,
        expected_approver=ORIGINATOR,
        request_text="add a sentence to the project",
        triggered_by="cli",
        target_repo=REPO_KEY,
    )


def _errors(notifications: list[tuple[str, str, str]]) -> list[str]:
    return [message for _cid, message, level in notifications if level == "error"]


# ---------------------------------------------------------------------------
# Nothing is asked of a model before the door has finished
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_project_declaring_nothing_never_reaches_a_model(
    store: SqlitePlanningRunStore, tmp_path: Path  # noqa: F811
) -> None:
    _remote, copy = make_remote_and_copy(tmp_path, declaration=None)
    driver, model, notifications = _driver_with_a_model(
        store, repo_path=copy, worktrees_root=tmp_path / "wt"
    )
    _queue(store)

    await driver.drive(CID)

    # THE POINT: not one call.
    assert model.calls == []
    assert store.get_run(CID)["state"] == PlanningState.FAILED.value
    said = _errors(notifications)
    assert len(said) == 1
    assert "does not say which memory it uses" in said[0]
    assert "project: <a name of letters, digits and underscores>" in said[0]
    # Nothing was cut, and nothing was recorded about which memory it belongs to.
    assert _git(copy, "branch", "--list", f"planning/{CID}") == ""
    assert store.get_memory_project(CID) is None


@pytest.mark.asyncio
async def test_a_declaration_that_breaks_the_parser_is_a_refusal_not_a_stranded_run(
    store: SqlitePlanningRunStore, tmp_path: Path  # noqa: F811
) -> None:
    """The review's own input, and the run it used to strand.

    About 1.2 KB of nesting raised a ``RecursionError`` — not a YAML error, so
    it went straight past the one thing being caught. The run stayed RUNNING
    and nobody was told anything at all.
    """
    _remote, copy = make_remote_and_copy(tmp_path, declaration=THE_NESTED_DECLARATION)
    driver, model, notifications = _driver_with_a_model(
        store, repo_path=copy, worktrees_root=tmp_path / "wt"
    )
    _queue(store)

    await driver.drive(CID)

    assert model.calls == []
    assert store.get_run(CID)["state"] == PlanningState.FAILED.value
    said = _errors(notifications)
    assert len(said) == 1
    assert "could not be read" in said[0]
    assert "nests more than" in said[0]
    assert _git(copy, "branch", "--list", f"planning/{CID}") == ""


@pytest.mark.asyncio
async def test_a_reserved_setting_name_is_refused_at_the_door(
    store: SqlitePlanningRunStore, tmp_path: Path  # noqa: F811
) -> None:
    _remote, copy = make_remote_and_copy(tmp_path, declaration=ASKS_FOR_A_RESERVED_NAME)
    driver, model, notifications = _driver_with_a_model(
        store, repo_path=copy, worktrees_root=tmp_path / "wt"
    )
    _queue(store)

    await driver.drive(CID)

    assert model.calls == []
    said = _errors(notifications)
    assert len(said) == 1
    assert "FORGE_DB_PATH" in said[0]
    assert "keeps for itself" in said[0]
    assert store.get_run(CID)["state"] == PlanningState.FAILED.value
    assert _git(copy, "branch", "--list", f"planning/{CID}") == ""


# ---------------------------------------------------------------------------
# A project that says everything properly passes, and it is written down first
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_everything_is_written_down_before_the_first_dispatch(
    store: SqlitePlanningRunStore, tmp_path: Path  # noqa: F811
) -> None:
    """The model stand-in raises when called; by then the record is complete.

    This is the positive half of the same property: the run reaches a model at
    all only after the starting commit, the memory name and the setting names
    are on the row. The stand-in's refusal is what stops the run here, so the
    assertions read the record at exactly the moment of the first call.
    """
    _remote, copy = make_remote_and_copy(tmp_path, declaration=ASKS_FOR_ITS_OWN_TWO)
    driver, model, notifications = _driver_with_a_model(
        store, repo_path=copy, worktrees_root=tmp_path / "wt"
    )
    _queue(store)

    await driver.drive(CID)

    assert len(model.calls) == 1  # it got as far as the model, and no further
    commit, branch = store.get_start_point(CID)
    assert commit and branch == "main"
    assert store.get_memory_project(CID) == "widget_shop"
    assert store.get_launch_settings(CID) == ("SOME_TOOL_CACHE", "ANOTHER_HOME")


@pytest.mark.asyncio
async def test_a_project_that_asks_for_nothing_extra_is_recorded_as_asking_for_nothing(
    store: SqlitePlanningRunStore, tmp_path: Path  # noqa: F811
) -> None:
    """"Declared, and empty" is a different fact from "nobody wrote it down"."""
    _remote, copy = make_remote_and_copy(tmp_path, declaration=DECLARES_WIDGET_SHOP)
    driver, _model, _notifications = _driver_with_a_model(
        store, repo_path=copy, worktrees_root=tmp_path / "wt"
    )
    _queue(store)

    await driver.drive(CID)

    assert store.get_memory_project(CID) == "widget_shop"
    assert store.get_launch_settings(CID) == ()


@pytest.mark.asyncio
async def test_the_door_does_not_fetch_twice_on_a_re_drive(
    store: SqlitePlanningRunStore, tmp_path: Path  # noqa: F811
) -> None:
    """A run that has been through the door keeps exactly what it started with.

    The remote moves on between the two drives. The recorded commit does not:
    a re-drive must never quietly move a run onto a newer commit than the
    branch it already cut, nor onto a different memory.
    """
    remote, copy = make_remote_and_copy(tmp_path, declaration=ASKS_FOR_ITS_OWN_TWO)
    driver, _model, _notifications = _driver_with_a_model(
        store, repo_path=copy, worktrees_root=tmp_path / "wt"
    )
    _queue(store)
    await driver.drive(CID)
    first_commit, _branch = store.get_start_point(CID)

    # The remote moves, and says something else about its memory.
    seed = tmp_path / "seed"
    (seed / ".guardkit" / "config.yaml").write_text(
        "memory:\n  project: something_else\n", encoding="utf-8"
    )
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "two")
    _git(seed, "push", "-q", "origin", "HEAD:refs/heads/main")
    moved = _git(seed, "rev-parse", "HEAD")
    assert moved != first_commit

    await driver.drive(CID)

    assert store.get_start_point(CID)[0] == first_commit
    assert store.get_memory_project(CID) == "widget_shop"
    assert store.get_launch_settings(CID) == ("SOME_TOOL_CACHE", "ANOTHER_HOME")

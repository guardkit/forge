"""When a managed sandbox counts as idle, read from the ledger (design 2.3a).

Each condition on its own keeps the sandbox up; the final states of each do
not; and another repository's open work never keeps this one up.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from forge.sandbox_gate.idle import repositories_with_unfinished_work, what_keeps_it_up

from tests.forge.sandbox_gate._fakes import (
    OTHER_REPO,
    REPO,
    a_build,
    a_deployment_target,
    a_ledger,
    a_mission,
    a_planning_run,
    a_preview_lease,
    a_publication,
    a_queue_row,
    a_stage_row,
)


@pytest.fixture
def ledger(tmp_path: Path) -> sqlite3.Connection:
    cx = a_ledger(tmp_path)
    yield cx
    cx.close()


def test_an_empty_ledger_is_idle(ledger: sqlite3.Connection) -> None:
    assert what_keeps_it_up(ledger, [REPO]) is None


@pytest.mark.parametrize("status", ["QUEUED", "ADMITTED"])
def test_an_open_queue_row_keeps_it_up(ledger: sqlite3.Connection, status: str) -> None:
    a_queue_row(ledger, status)
    assert "queue row" in (what_keeps_it_up(ledger, [REPO]) or "")


@pytest.mark.parametrize("status", ["DONE", "WITHDRAWN", "BLOCKED"])
def test_a_closed_queue_row_does_not(ledger: sqlite3.Connection, status: str) -> None:
    a_queue_row(ledger, status)
    assert what_keeps_it_up(ledger, [REPO]) is None


@pytest.mark.parametrize("state", ["QUEUED", "RUNNING", "PAUSED"])
def test_an_unfinished_planning_run_keeps_it_up(ledger: sqlite3.Connection, state: str) -> None:
    a_planning_run(ledger, state)
    assert "planning run" in (what_keeps_it_up(ledger, [REPO]) or "")


@pytest.mark.parametrize("state", ["FAILED", "PLANNED_HANDOFF", "BUILD_QUEUED", "CANCELLED"])
def test_a_finished_planning_run_does_not(ledger: sqlite3.Connection, state: str) -> None:
    a_planning_run(ledger, state)
    assert what_keeps_it_up(ledger, [REPO]) is None


@pytest.mark.parametrize("status", ["QUEUED", "RUNNING", "PAUSED", "INTERRUPTED", "FINALISING"])
def test_an_unfinished_build_keeps_it_up(ledger: sqlite3.Connection, status: str) -> None:
    a_build(ledger, status)
    assert "build B-1" in (what_keeps_it_up(ledger, [REPO]) or "")


@pytest.mark.parametrize("status", ["PROPOSED", "STARTING", "RUNNING", "READY", "MERGING"])
def test_an_unfinished_mission_keeps_it_up(ledger: sqlite3.Connection, status: str) -> None:
    a_mission(ledger, status)
    assert "mission" in (what_keeps_it_up(ledger, [REPO]) or "")


@pytest.mark.parametrize("status", ["MERGED", "NOT_MERGED", "REJECTED", "NOT_STARTED"])
def test_a_finished_mission_does_not(ledger: sqlite3.Connection, status: str) -> None:
    a_mission(ledger, status)
    assert what_keeps_it_up(ledger, [REPO]) is None


def test_a_merge_card_past_its_hold_still_keeps_it_up(ledger: sqlite3.Connection) -> None:
    # Offered on 1 October and never answered: older than any queue hold.
    a_build(ledger, "COMPLETE")
    a_stage_row(ledger, "B-1", "merge_deploy_offer")
    assert "merge card" in (what_keeps_it_up(ledger, [REPO]) or "")


def test_an_answered_merge_card_does_not(ledger: sqlite3.Connection) -> None:
    a_build(ledger, "COMPLETE")
    a_stage_row(ledger, "B-1", "merge_deploy_offer")
    a_stage_row(ledger, "B-1", "merge_deploy_decision")
    a_publication(ledger, result="merged and running")
    assert what_keeps_it_up(ledger, [REPO]) is None


@pytest.mark.parametrize("state", ["preparing", "ready", "ending"])
def test_a_live_preview_keeps_it_up(ledger: sqlite3.Connection, state: str) -> None:
    a_preview_lease(ledger, state)
    assert "preview" in (what_keeps_it_up(ledger, [REPO]) or "")


@pytest.mark.parametrize("state", ["ended", "failed"])
def test_an_ended_preview_does_not(ledger: sqlite3.Connection, state: str) -> None:
    a_preview_lease(ledger, state)
    assert what_keeps_it_up(ledger, [REPO]) is None


def test_a_publication_with_no_result_keeps_it_up(ledger: sqlite3.Connection) -> None:
    a_publication(ledger, result=None)
    assert "publication" in (what_keeps_it_up(ledger, [REPO]) or "")


def test_a_retained_publication_holder_keeps_it_up_even_with_a_result(
    ledger: sqlite3.Connection,
) -> None:
    # The lease ran out on 1 October; the holder is still set: still unsettled.
    a_publication(ledger, result="merged and running", lease_holder="forge-press-1")
    assert "publication" in (what_keeps_it_up(ledger, [REPO]) or "")


def test_a_deployment_lock_holder_keeps_it_up(ledger: sqlite3.Connection) -> None:
    a_deployment_target(ledger, f"{REPO}::production", holder="B-7")
    assert "deployment lock" in (what_keeps_it_up(ledger, [REPO]) or "")


def test_a_recorded_deployment_alone_does_not(ledger: sqlite3.Connection) -> None:
    # Design 2.3a: a running deployment is brought back after a start, not a
    # reason to stay up.
    a_deployment_target(ledger, REPO, running_commit="abc1234")
    assert what_keeps_it_up(ledger, [REPO]) is None


def test_a_target_of_a_similarly_named_repository_is_not_this_one(
    ledger: sqlite3.Connection,
) -> None:
    a_deployment_target(ledger, f"{REPO}-two::production", holder="B-7")
    assert what_keeps_it_up(ledger, [REPO]) is None


def test_another_repositorys_work_does_not_keep_this_one_up(
    ledger: sqlite3.Connection,
) -> None:
    a_queue_row(ledger, "QUEUED", repo=OTHER_REPO)
    a_build(ledger, "RUNNING", repo=OTHER_REPO)
    a_preview_lease(ledger, "ready", repo=OTHER_REPO)
    assert what_keeps_it_up(ledger, [REPO]) is None
    assert repositories_with_unfinished_work(ledger, [REPO, OTHER_REPO]) == [OTHER_REPO]


def test_every_repository_of_the_sandbox_counts(ledger: sqlite3.Connection) -> None:
    a_build(ledger, "RUNNING", repo=OTHER_REPO)
    assert what_keeps_it_up(ledger, [REPO, OTHER_REPO]) is not None

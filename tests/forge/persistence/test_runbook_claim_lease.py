"""Claim-lease / crash-recovery tests for ``RunbookRepository`` (TASK-RBX-009).

The no-double-run guarantee (TASK-RBX-007) claims a runnable step
(``pending`` / ``failed`` / ``awaiting_approval``) into ``running`` before its
handler runs. The gap this module locks down: a step left ``running`` by a
*crashed* executor must become reclaimable once its lease expires, **without**
letting a concurrent executor steal a step that is genuinely in flight.

These are the repo seam tests AC-3 asks for — they drive
``try_claim_step_for_execution`` directly against a real migrated SQLite file
and assert both the reclaim path and the no-steal guarantee, including the
``claimed_at`` / ``claimed_by`` lease columns added by the TASK-RBX-009
migration.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations as lifecycle_migrations
from forge.persistence.migrations import runbook as runbook_migration
from forge.persistence.repositories.runbook import (
    DEFAULT_CLAIM_LEASE_SECONDS,
    RunbookRepository,
)
from forge.persistence.repositories.runbook_models import (
    Runbook,
    Step,
    StepStatus,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def writer_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A writer connection against a freshly-migrated db file."""
    db_path = tmp_path / "forge.db"
    cx = sqlite_connect.connect_writer(db_path)
    lifecycle_migrations.apply_at_boot(cx)
    runbook_migration.apply(cx)
    try:
        yield cx
    finally:
        cx.close()


@pytest.fixture()
def repository(writer_db: sqlite3.Connection) -> RunbookRepository:
    return RunbookRepository(connection=writer_db)


@pytest.fixture()
def now() -> datetime:
    return datetime(2026, 6, 22, 12, 0, 0, tzinfo=UTC)


def _make_single_step_runbook(
    repository: RunbookRepository,
    *,
    runbook_id: str = "rb-lease",
    created_at: datetime,
) -> Runbook:
    runbook = Runbook(
        runbook_id=runbook_id,
        target="FEAT-RBX-009",
        steps=(
            Step(
                step_type="build",
                params={},
                status=StepStatus.pending,
                sequence_index=0,
            ),
        ),
        current_step_index=0,
        status=StepStatus.pending,
        created_at=created_at,
    )
    repository.create_runbook(runbook, correlation_id="corr-setup")
    return runbook


def _force_step_running(
    cx: sqlite3.Connection,
    runbook_id: str,
    sequence_index: int,
    *,
    claimed_at: str | None,
    claimed_by: str | None = "peer-executor",
) -> None:
    """Drive a step straight to ``running`` with a chosen lease stamp.

    Simulates the state a crashed (stale ``claimed_at``) or genuinely in-flight
    (fresh ``claimed_at``) peer executor would leave behind, bypassing the
    claim API so the test controls the lease instant precisely.
    """
    cx.execute("BEGIN IMMEDIATE;")
    cx.execute(
        """
        UPDATE runbook_steps
        SET status = ?, claimed_at = ?, claimed_by = ?
        WHERE runbook_id = ? AND sequence_index = ?
        """,
        (StepStatus.running.value, claimed_at, claimed_by, runbook_id, sequence_index),
    )
    cx.execute("COMMIT;")


def _read_step_row(
    cx: sqlite3.Connection, runbook_id: str, sequence_index: int
) -> sqlite3.Row:
    row = cx.execute(
        """
        SELECT status, claimed_at, claimed_by
        FROM runbook_steps
        WHERE runbook_id = ? AND sequence_index = ?
        """,
        (runbook_id, sequence_index),
    ).fetchone()
    assert row is not None
    return row


# ---------------------------------------------------------------------------
# Claiming a runnable step stamps the lease
# ---------------------------------------------------------------------------


class TestClaimStampsLease:
    def test_claim_pending_step_stamps_claimed_at_and_owner(
        self,
        repository: RunbookRepository,
        writer_db: sqlite3.Connection,
        now: datetime,
    ) -> None:
        _make_single_step_runbook(repository, created_at=now)

        claimed = repository.try_claim_step_for_execution(
            "rb-lease", 0, correlation_id="corr-1", now=now, owner="exec-1"
        )

        assert claimed is True
        row = _read_step_row(writer_db, "rb-lease", 0)
        assert row[0] == StepStatus.running.value
        assert row[1] == now.isoformat()
        assert row[2] == "exec-1"


# ---------------------------------------------------------------------------
# AC-2: a genuinely in-flight running step is NOT stolen
# ---------------------------------------------------------------------------


class TestLiveLeaseNotStolen:
    def test_claim_refuses_running_step_with_live_lease(
        self,
        repository: RunbookRepository,
        writer_db: sqlite3.Connection,
        now: datetime,
    ) -> None:
        _make_single_step_runbook(repository, created_at=now)
        # A peer claimed the step 10s ago — well within the lease window.
        live_claim = (now - timedelta(seconds=10)).isoformat()
        _force_step_running(writer_db, "rb-lease", 0, claimed_at=live_claim)

        claimed = repository.try_claim_step_for_execution(
            "rb-lease",
            0,
            correlation_id="corr-2",
            now=now,
            lease_seconds=DEFAULT_CLAIM_LEASE_SECONDS,
            owner="exec-2",
        )

        assert claimed is False, "a live-lease running step must not be stolen"
        row = _read_step_row(writer_db, "rb-lease", 0)
        # Untouched: still owned by the peer with its original lease stamp.
        assert row[0] == StepStatus.running.value
        assert row[1] == live_claim
        assert row[2] == "peer-executor"

    def test_claim_refuses_passed_step(
        self,
        repository: RunbookRepository,
        writer_db: sqlite3.Connection,
        now: datetime,
    ) -> None:
        _make_single_step_runbook(repository, created_at=now)
        repository.update_step_status(
            "rb-lease", 0, StepStatus.passed, correlation_id="corr-passed"
        )

        claimed = repository.try_claim_step_for_execution(
            "rb-lease", 0, correlation_id="corr-3", now=now
        )

        assert claimed is False
        assert _read_step_row(writer_db, "rb-lease", 0)[0] == StepStatus.passed.value


# ---------------------------------------------------------------------------
# AC-1: a running step abandoned by a crash is reclaimable
# ---------------------------------------------------------------------------


class TestExpiredLeaseReclaimed:
    def test_claim_reclaims_running_step_with_expired_lease(
        self,
        repository: RunbookRepository,
        writer_db: sqlite3.Connection,
        now: datetime,
    ) -> None:
        _make_single_step_runbook(repository, created_at=now)
        # Crashed executor claimed it 1000s ago; lease is 900s → expired.
        stale_claim = (now - timedelta(seconds=1000)).isoformat()
        _force_step_running(writer_db, "rb-lease", 0, claimed_at=stale_claim)

        claimed = repository.try_claim_step_for_execution(
            "rb-lease",
            0,
            correlation_id="corr-4",
            now=now,
            lease_seconds=DEFAULT_CLAIM_LEASE_SECONDS,
            owner="exec-recover",
        )

        assert claimed is True, "an expired-lease running step must be reclaimable"
        row = _read_step_row(writer_db, "rb-lease", 0)
        assert row[0] == StepStatus.running.value
        # Lease re-stamped to the reclaiming executor.
        assert row[1] == now.isoformat()
        assert row[2] == "exec-recover"

    def test_claim_reclaims_running_step_with_null_claimed_at(
        self,
        repository: RunbookRepository,
        writer_db: sqlite3.Connection,
        now: datetime,
    ) -> None:
        _make_single_step_runbook(repository, created_at=now)
        # A crash before the lease was stamped (or a legacy row) leaves NULL.
        _force_step_running(writer_db, "rb-lease", 0, claimed_at=None, claimed_by=None)

        claimed = repository.try_claim_step_for_execution(
            "rb-lease", 0, correlation_id="corr-5", now=now, owner="exec-recover"
        )

        assert claimed is True, "a NULL-lease running step must be reclaimable"
        row = _read_step_row(writer_db, "rb-lease", 0)
        assert row[0] == StepStatus.running.value
        assert row[1] == now.isoformat()

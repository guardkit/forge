"""Tests for runbook concurrency and integration boundaries (TASK-RSP-006).

Acceptance-criteria coverage map:

* AC-001: Two threads creating rb-clash → exactly one runbook; loser raises
  RunbookDuplicateError — :class:`TestConcurrentCreate`.
* AC-002: Concurrent advance + update_step_status both commit —
  :class:`TestConcurrentAdvanceAndUpdate`.
* AC-003: Read-only reader observes pre/post commit states consistently —
  :class:`TestReadOnlySnapshot`.
* AC-004: Non-existent location raises SQLiteConnectError —
  :class:`TestStoreUnavailable`.
* AC-005: read_only_connect-backed repo refuses create_runbook —
  :class:`TestReadOnlyRefusesWrite`.
* AC-006: Unmigrated store refused predictably —
  :class:`TestUnmigratedStoreRejected`.
* AC-007: All tests pass reliably without timing flakiness —
  all test classes.

Group F (Concurrency) uses ``threading.Barrier`` to force genuine contention,
mirroring ``TestRecordConcurrency`` in ``test_bridge_registry.py``. Group H
(Integration Boundaries) validates error handling at store boundaries.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations as lifecycle_migrations
from forge.persistence.migrations import runbook as runbook_migration
from forge.persistence.repositories.runbook import (
    RunbookDuplicateError,
    RunbookNotFoundError,
    RunbookRepository,
)
from forge.persistence.repositories.runbook_models import (
    Runbook,
    Step,
    StepResult,
    StepStatus,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def writer_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Return a writer connection against a freshly-migrated db file.

    Applies the existing forge schema migrations and then the runbook
    migration so tests run against the full production substrate.
    """
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
    """Return a ``RunbookRepository`` bound to the migrated writer connection."""
    return RunbookRepository(connection=writer_db)


@pytest.fixture()
def fixed_now() -> datetime:
    return datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)


def _make_runbook(
    *,
    runbook_id: str = "rb-001",
    target: str = "FEAT-TEST-001",
    steps: tuple[Step, ...] | None = None,
    current_step_index: int = 0,
    status: StepStatus = StepStatus.pending,
    created_at: datetime | None = None,
) -> Runbook:
    """Helper to build test Runbook instances."""
    now = created_at or datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)
    if steps is None:
        steps = (
            Step(
                step_type="build",
                params={"target": "app"},
                status=StepStatus.pending,
                sequence_index=0,
            ),
        )
    return Runbook(
        runbook_id=runbook_id,
        target=target,
        steps=steps,
        current_step_index=current_step_index,
        status=status,
        created_at=now,
    )


# ---------------------------------------------------------------------------
# Group F — Concurrency
# ---------------------------------------------------------------------------


class TestConcurrentCreate:
    """AC-001: Two threads creating rb-clash leave exactly one runbook.

    The winner successfully persists; the loser raises RunbookDuplicateError.
    The surviving runbook has all three steps intact with no orphaned rows.
    """

    def test_parallel_create_one_succeeds_one_raises_duplicate(
        self, tmp_path: Path, fixed_now: datetime
    ) -> None:
        # Bootstrap a fresh database
        db_path = tmp_path / "forge.db"
        bootstrap = sqlite_connect.connect_writer(db_path)
        try:
            lifecycle_migrations.apply_at_boot(bootstrap)
            runbook_migration.apply(bootstrap)
        finally:
            bootstrap.close()

        # Build the shared runbook spec with three steps
        steps = (
            Step(
                step_type="build",
                params={"target": "frontend"},
                status=StepStatus.pending,
                sequence_index=0,
            ),
            Step(
                step_type="test",
                params={"suite": "unit"},
                status=StepStatus.pending,
                sequence_index=1,
            ),
            Step(
                step_type="deploy",
                params={"env": "staging"},
                status=StepStatus.pending,
                sequence_index=2,
            ),
        )
        runbook = _make_runbook(
            runbook_id="rb-clash",
            target="FEAT-CONC-001",
            steps=steps,
            created_at=fixed_now,
        )

        errors: list[BaseException] = []
        successes: list[str] = []
        barrier = threading.Barrier(2)

        def _worker(thread_id: str) -> None:
            try:
                cx = sqlite_connect.connect_writer(db_path)
                try:
                    repo = RunbookRepository(connection=cx)
                    barrier.wait(timeout=5)
                    repo.create_runbook(runbook, correlation_id=f"corr-{thread_id}")
                    successes.append(thread_id)
                finally:
                    cx.close()
            except RunbookDuplicateError:
                # Expected for the loser — record but do not append to errors
                pass
            except BaseException as exc:  # pragma: no cover - test diag
                errors.append(exc)

        t1 = threading.Thread(target=_worker, args=("T1",))
        t2 = threading.Thread(target=_worker, args=("T2",))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        # Verify exactly one thread succeeded
        assert not errors, f"unexpected error during parallel create: {errors!r}"
        assert len(successes) == 1, (
            f"expected exactly one success; got {len(successes)} ({successes!r})"
        )

        # Verify the runbook loads with all three steps
        verifier = sqlite_connect.connect_writer(db_path)
        try:
            repo = RunbookRepository(connection=verifier)
            loaded = repo.load_runbook("rb-clash", correlation_id="corr-verify")
            assert loaded is not None
            assert loaded.runbook_id == "rb-clash"
            assert loaded.target == "FEAT-CONC-001"
            assert len(loaded.steps) == 3
            assert loaded.steps[0].step_type == "build"
            assert loaded.steps[1].step_type == "test"
            assert loaded.steps[2].step_type == "deploy"

            # Verify no orphaned step rows
            cursor = verifier.execute(
                "SELECT COUNT(*) FROM runbook_steps WHERE runbook_id = ?",
                ("rb-clash",),
            )
            step_count = cursor.fetchone()[0]
            assert step_count == 3, f"expected 3 step rows; got {step_count}"
        finally:
            verifier.close()


class TestConcurrentAdvanceAndUpdate:
    """AC-002: Concurrent advance + update_step_status both commit.

    Reload shows pointer on step 2 and step 1 status=passed. No lost work.
    """

    def test_advance_and_update_both_succeed(
        self, tmp_path: Path, fixed_now: datetime
    ) -> None:
        # Bootstrap and create a two-step runbook
        db_path = tmp_path / "forge.db"
        bootstrap = sqlite_connect.connect_writer(db_path)
        try:
            lifecycle_migrations.apply_at_boot(bootstrap)
            runbook_migration.apply(bootstrap)

            steps = (
                Step(
                    step_type="build",
                    params={},
                    status=StepStatus.running,
                    sequence_index=0,
                ),
                Step(
                    step_type="test",
                    params={},
                    status=StepStatus.pending,
                    sequence_index=1,
                ),
            )
            runbook = _make_runbook(
                runbook_id="rb-serial",
                target="FEAT-CONC-002",
                steps=steps,
                current_step_index=0,
                created_at=fixed_now,
            )
            repo = RunbookRepository(connection=bootstrap)
            repo.create_runbook(runbook, correlation_id="corr-setup")
        finally:
            bootstrap.close()

        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def _advance_worker() -> None:
            try:
                cx = sqlite_connect.connect_writer(db_path)
                try:
                    repo = RunbookRepository(connection=cx)
                    barrier.wait(timeout=5)
                    repo.advance("rb-serial", correlation_id="corr-advance")
                finally:
                    cx.close()
            except BaseException as exc:  # pragma: no cover - test diag
                errors.append(exc)

        def _update_worker() -> None:
            try:
                cx = sqlite_connect.connect_writer(db_path)
                try:
                    repo = RunbookRepository(connection=cx)
                    barrier.wait(timeout=5)
                    repo.update_step_status(
                        "rb-serial",
                        sequence_index=0,
                        status=StepStatus.passed,
                        correlation_id="corr-update",
                    )
                finally:
                    cx.close()
            except BaseException as exc:  # pragma: no cover - test diag
                errors.append(exc)

        t_advance = threading.Thread(target=_advance_worker)
        t_update = threading.Thread(target=_update_worker)
        t_advance.start()
        t_update.start()
        t_advance.join(timeout=10)
        t_update.join(timeout=10)

        assert not errors, f"parallel operations raised: {errors!r}"

        # Verify both writes committed
        verifier = sqlite_connect.connect_writer(db_path)
        try:
            repo = RunbookRepository(connection=verifier)
            loaded = repo.load_runbook("rb-serial", correlation_id="corr-verify")
            assert loaded is not None
            assert loaded.current_step_index == 1, "advance did not commit"
            assert loaded.steps[0].status == StepStatus.passed, (
                "update_step_status did not commit"
            )
        finally:
            verifier.close()


class TestReadOnlySnapshot:
    """AC-003: Read-only reader sees consistent pre/post commit states.

    Never observes a half-written step. WAL snapshot isolation ensures the
    reader sees either the old state (before commit) or the new state (after),
    but never partial data.
    """

    def test_reader_observes_atomic_transitions(
        self, tmp_path: Path, fixed_now: datetime
    ) -> None:
        # Bootstrap with a one-step runbook
        db_path = tmp_path / "forge.db"
        bootstrap = sqlite_connect.connect_writer(db_path)
        try:
            lifecycle_migrations.apply_at_boot(bootstrap)
            runbook_migration.apply(bootstrap)

            steps = (
                Step(
                    step_type="build",
                    params={},
                    status=StepStatus.running,
                    sequence_index=0,
                    result=None,
                ),
            )
            runbook = _make_runbook(
                runbook_id="rb-reader",
                target="FEAT-CONC-003",
                steps=steps,
                created_at=fixed_now,
            )
            repo = RunbookRepository(connection=bootstrap)
            repo.create_runbook(runbook, correlation_id="corr-setup")
        finally:
            bootstrap.close()

        errors: list[BaseException] = []
        observations: list[StepResult | None] = []
        barrier = threading.Barrier(2)

        def _writer_worker() -> None:
            try:
                cx = sqlite_connect.connect_writer(db_path)
                try:
                    repo = RunbookRepository(connection=cx)
                    barrier.wait(timeout=5)
                    # Write a result to the step
                    result = StepResult(
                        exit_code=0,
                        captured_output="build succeeded",
                        started_at=fixed_now,
                        completed_at=fixed_now,
                    )
                    repo.update_step_status(
                        "rb-reader",
                        sequence_index=0,
                        status=StepStatus.passed,
                        result=result,
                        correlation_id="corr-writer",
                    )
                finally:
                    cx.close()
            except BaseException as exc:  # pragma: no cover - test diag
                errors.append(exc)

        def _reader_worker() -> None:
            try:
                cx = sqlite_connect.read_only_connect(db_path)
                try:
                    repo = RunbookRepository(connection=cx)
                    barrier.wait(timeout=5)
                    # Read immediately — may see pre-commit or post-commit
                    loaded = repo.load_runbook("rb-reader", correlation_id="corr-reader")
                    assert loaded is not None
                    observations.append(loaded.steps[0].result)
                finally:
                    cx.close()
            except BaseException as exc:  # pragma: no cover - test diag
                errors.append(exc)

        t_writer = threading.Thread(target=_writer_worker)
        t_reader = threading.Thread(target=_reader_worker)
        t_writer.start()
        t_reader.start()
        t_writer.join(timeout=10)
        t_reader.join(timeout=10)

        assert not errors, f"parallel read/write raised: {errors!r}"
        assert len(observations) == 1

        # The reader must observe a valid state: either None (pre-commit)
        # or the full result (post-commit). Never a partial result.
        observed = observations[0]
        if observed is None:
            # Reader saw pre-commit state
            pass
        else:
            # Reader saw post-commit state — verify it's complete
            assert observed.exit_code == 0
            assert observed.captured_output == "build succeeded"
            assert observed.started_at == fixed_now
            assert observed.completed_at == fixed_now

        # After both threads finish, a fresh reader must see the final state
        verifier = sqlite_connect.read_only_connect(db_path)
        try:
            repo = RunbookRepository(connection=verifier)
            loaded = repo.load_runbook("rb-reader", correlation_id="corr-final")
            assert loaded is not None
            assert loaded.steps[0].result is not None
            assert loaded.steps[0].result.exit_code == 0
            assert loaded.steps[0].result.captured_output == "build succeeded"
        finally:
            verifier.close()


# ---------------------------------------------------------------------------
# Group H — Integration Boundaries
# ---------------------------------------------------------------------------


class TestStoreUnavailable:
    """AC-004: Opening the store at a non-existent location raises SQLiteConnectError.

    No raw backend error leaks to the caller.
    """

    def test_connect_writer_nonexistent_parent_raises_sqlite_connect_error(
        self, tmp_path: Path
    ) -> None:
        nonexistent = tmp_path / "does-not-exist" / "forge.db"
        with pytest.raises(sqlite_connect.SQLiteConnectError) as exc_info:
            sqlite_connect.connect_writer(nonexistent)
        assert "parent directory does not exist" in str(exc_info.value)

    def test_read_only_connect_nonexistent_file_raises_sqlite_connect_error(
        self, tmp_path: Path
    ) -> None:
        nonexistent = tmp_path / "missing.db"
        with pytest.raises(sqlite_connect.SQLiteConnectError) as exc_info:
            sqlite_connect.read_only_connect(nonexistent)
        # The error message varies by SQLite version but must be wrapped
        assert "SQLiteConnectError" in str(type(exc_info.value))


class TestReadOnlyRefusesWrite:
    """AC-005: A read_only_connect-backed repository refuses create_runbook.

    The store remains unchanged.
    """

    def test_create_runbook_on_readonly_connection_refused(
        self, tmp_path: Path, fixed_now: datetime
    ) -> None:
        # Bootstrap a migrated database
        db_path = tmp_path / "forge.db"
        bootstrap = sqlite_connect.connect_writer(db_path)
        try:
            lifecycle_migrations.apply_at_boot(bootstrap)
            runbook_migration.apply(bootstrap)
        finally:
            bootstrap.close()

        # Open read-only and attempt create_runbook
        cx = sqlite_connect.read_only_connect(db_path)
        try:
            repo = RunbookRepository(connection=cx)
            runbook = _make_runbook(
                runbook_id="rb-readonly",
                created_at=fixed_now,
            )
            with pytest.raises(sqlite3.OperationalError) as exc_info:
                repo.create_runbook(runbook, correlation_id="corr-readonly")
            assert "readonly" in str(exc_info.value).lower() or "attempt to write" in str(exc_info.value).lower()
        finally:
            cx.close()

        # Verify the store is unchanged
        verifier = sqlite_connect.connect_writer(db_path)
        try:
            repo = RunbookRepository(connection=verifier)
            loaded = repo.load_runbook("rb-readonly", correlation_id="corr-verify")
            assert loaded is None, "runbook should not exist"
        finally:
            verifier.close()


class TestUnmigratedStoreRejected:
    """AC-006: Loading from an unmigrated store is refused predictably.

    No partial runbook is returned.
    """

    def test_load_from_unmigrated_db_raises_operational_error(
        self, tmp_path: Path
    ) -> None:
        # Create a database without running the runbook migration
        db_path = tmp_path / "forge.db"
        cx = sqlite_connect.connect_writer(db_path)
        try:
            # Apply only the lifecycle migrations, skip runbook migration
            lifecycle_migrations.apply_at_boot(cx)

            repo = RunbookRepository(connection=cx)
            with pytest.raises(sqlite3.OperationalError) as exc_info:
                repo.load_runbook("rb-unmigrated", correlation_id="corr-unmigrated")
            # SQLite raises "no such table: runbooks" or similar
            assert "runbooks" in str(exc_info.value).lower() or "no such table" in str(exc_info.value).lower()
        finally:
            cx.close()

    def test_create_on_unmigrated_db_raises_operational_error(
        self, tmp_path: Path, fixed_now: datetime
    ) -> None:
        # Create a database without the runbook tables
        db_path = tmp_path / "forge.db"
        cx = sqlite_connect.connect_writer(db_path)
        try:
            lifecycle_migrations.apply_at_boot(cx)

            repo = RunbookRepository(connection=cx)
            runbook = _make_runbook(
                runbook_id="rb-no-table",
                created_at=fixed_now,
            )
            with pytest.raises(sqlite3.OperationalError) as exc_info:
                repo.create_runbook(runbook, correlation_id="corr-create-unmigrated")
            assert "runbooks" in str(exc_info.value).lower() or "no such table" in str(exc_info.value).lower()
        finally:
            cx.close()

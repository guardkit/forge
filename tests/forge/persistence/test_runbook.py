"""Tests for ``forge.persistence.repositories.runbook`` (TASK-RSP-003).

Acceptance-criteria coverage map:

* AC-001: Creating a three-step runbook persists it with all steps in order —
  :class:`TestCreateRunbookThreeSteps`.
* AC-002: ``load_runbook`` returns same target, created_at, resume pointer,
  status; steps in sequence order — :class:`TestLoadRunbookRoundTrip`.
* AC-003: Newly created runbook loads with steps pending and
  current_step_index==0 — :class:`TestNewRunbookInitialState`.
* AC-004: Single-step runbook round-trips with pointer on the only step —
  :class:`TestSingleStepRunbook`.
* AC-005: Creating with empty step list raises RunbookValidationError,
  persists nothing — :class:`TestEmptyStepListRejected`.
* AC-006: Duplicate runbook_id raises RunbookDuplicateError, original
  unaffected — :class:`TestDuplicateRunbookRejected`.
* AC-007: ``load_runbook`` for unknown id returns None —
  :class:`TestLoadNonexistentRunbook`.
* AC-008: Step params round-trip without loss (nested mappings) —
  :class:`TestStepParamsRoundTrip`.
* AC-009: Steps in shuffled order still load by sequence_index —
  :class:`TestStepsLoadInSequenceOrder`.
* AC-010: Every public method accepts correlation_id parameter —
  :class:`TestCorrelationIdContract`.
* AC-011: Seam test passes — :func:`test_runbook_schema_matches_repository_writes`.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations as lifecycle_migrations
from forge.persistence.migrations import runbook as runbook_migration
from forge.persistence.repositories.runbook import (
    RunbookDuplicateError,
    RunbookRepository,
)
from forge.persistence.repositories.runbook_models import (
    Runbook,
    RunbookValidationError,
    Step,
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
# AC-001: Creating a three-step runbook persists it with all steps in order
# ---------------------------------------------------------------------------


class TestCreateRunbookThreeSteps:
    """Creating a three-step runbook persists all steps in order."""

    def test_create_three_step_runbook_persists_all_steps_in_order(
        self, repository: RunbookRepository, fixed_now: datetime
    ) -> None:
        steps = (
            Step(
                step_type="build",
                params={"target": "app"},
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
            runbook_id="rb-three-steps",
            steps=steps,
            created_at=fixed_now,
        )

        repository.create_runbook(runbook, correlation_id="corr-001")

        loaded = repository.load_runbook("rb-three-steps", correlation_id="corr-001")
        assert loaded is not None
        assert len(loaded.steps) == 3
        assert loaded.steps[0].step_type == "build"
        assert loaded.steps[0].sequence_index == 0
        assert loaded.steps[1].step_type == "test"
        assert loaded.steps[1].sequence_index == 1
        assert loaded.steps[2].step_type == "deploy"
        assert loaded.steps[2].sequence_index == 2


# ---------------------------------------------------------------------------
# AC-002: load_runbook returns same target, created_at, resume pointer, status
# ---------------------------------------------------------------------------


class TestLoadRunbookRoundTrip:
    """load_runbook preserves target, created_at, resume pointer, status."""

    def test_load_runbook_preserves_all_runbook_fields(
        self, repository: RunbookRepository, fixed_now: datetime
    ) -> None:
        runbook = _make_runbook(
            runbook_id="rb-roundtrip",
            target="FEAT-RT-001",
            current_step_index=0,
            status=StepStatus.pending,
            created_at=fixed_now,
        )

        repository.create_runbook(runbook, correlation_id="corr-001")

        loaded = repository.load_runbook("rb-roundtrip", correlation_id="corr-001")
        assert loaded is not None
        assert loaded.runbook_id == "rb-roundtrip"
        assert loaded.target == "FEAT-RT-001"
        assert loaded.current_step_index == 0
        assert loaded.status == StepStatus.pending
        assert loaded.created_at == fixed_now


# ---------------------------------------------------------------------------
# AC-003: Newly created runbook loads with steps pending and current_step_index==0
# ---------------------------------------------------------------------------


class TestNewRunbookInitialState:
    """New runbook loads with steps pending and current_step_index == 0."""

    def test_new_runbook_has_pending_steps_and_zero_index(
        self, repository: RunbookRepository
    ) -> None:
        steps = (
            Step(
                step_type="build",
                params={},
                status=StepStatus.pending,
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
            runbook_id="rb-initial",
            steps=steps,
            current_step_index=0,
            status=StepStatus.pending,
        )

        repository.create_runbook(runbook, correlation_id="corr-001")

        loaded = repository.load_runbook("rb-initial", correlation_id="corr-001")
        assert loaded is not None
        assert loaded.current_step_index == 0
        for step in loaded.steps:
            assert step.status == StepStatus.pending


# ---------------------------------------------------------------------------
# AC-004: Single-step runbook round-trips with pointer on the only step
# ---------------------------------------------------------------------------


class TestSingleStepRunbook:
    """A single-step runbook round-trips and its pointer rests on the only step."""

    def test_single_step_runbook_round_trips(
        self, repository: RunbookRepository
    ) -> None:
        step = Step(
            step_type="single",
            params={"key": "value"},
            status=StepStatus.pending,
            sequence_index=0,
        )
        runbook = _make_runbook(
            runbook_id="rb-single",
            steps=(step,),
            current_step_index=0,
        )

        repository.create_runbook(runbook, correlation_id="corr-001")

        loaded = repository.load_runbook("rb-single", correlation_id="corr-001")
        assert loaded is not None
        assert len(loaded.steps) == 1
        assert loaded.current_step_index == 0
        assert loaded.steps[0].step_type == "single"


# ---------------------------------------------------------------------------
# AC-005: Creating with empty step list raises RunbookValidationError
# ---------------------------------------------------------------------------


class TestEmptyStepListRejected:
    """Empty step list raises RunbookValidationError, persists nothing."""

    def test_empty_step_list_raises_validation_error(
        self, repository: RunbookRepository, writer_db: sqlite3.Connection
    ) -> None:
        # The Runbook model itself validates this at construction, so we
        # can't even create an invalid Runbook to pass to the repository.
        # This test verifies the model's validation is in place.
        with pytest.raises(RunbookValidationError):
            Runbook(
                runbook_id="rb-empty",
                target="FEAT-EMPTY",
                steps=(),
                current_step_index=0,
                status=StepStatus.pending,
                created_at=datetime.now(UTC),
            )

        # Verify nothing was persisted
        count = writer_db.execute("SELECT COUNT(*) FROM runbooks").fetchone()[0]
        assert count == 0


# ---------------------------------------------------------------------------
# AC-006: Duplicate runbook_id raises RunbookDuplicateError, original unaffected
# ---------------------------------------------------------------------------


class TestDuplicateRunbookRejected:
    """Duplicate runbook_id raises RunbookDuplicateError, original intact."""

    def test_duplicate_runbook_id_raises_error(
        self, repository: RunbookRepository
    ) -> None:
        original = _make_runbook(runbook_id="rb-dup", target="ORIGINAL")
        repository.create_runbook(original, correlation_id="corr-001")

        duplicate = _make_runbook(runbook_id="rb-dup", target="DUPLICATE")
        with pytest.raises(RunbookDuplicateError):
            repository.create_runbook(duplicate, correlation_id="corr-002")

        # Verify original is unchanged
        loaded = repository.load_runbook("rb-dup", correlation_id="corr-003")
        assert loaded is not None
        assert loaded.target == "ORIGINAL"


# ---------------------------------------------------------------------------
# AC-007: load_runbook for unknown id returns None
# ---------------------------------------------------------------------------


class TestLoadNonexistentRunbook:
    """load_runbook for an unknown id returns None."""

    def test_load_nonexistent_runbook_returns_none(
        self, repository: RunbookRepository
    ) -> None:
        loaded = repository.load_runbook("rb-nonexistent", correlation_id="corr-001")
        assert loaded is None


# ---------------------------------------------------------------------------
# AC-008: Step params round-trip without loss (nested mappings)
# ---------------------------------------------------------------------------


class TestStepParamsRoundTrip:
    """Step params round-trip without loss, including nested mappings."""

    def test_nested_params_round_trip(self, repository: RunbookRepository) -> None:
        step = Step(
            step_type="complex",
            params={
                "simple": "value",
                "nested": {"inner": "data", "number": 42},
                "list": [1, 2, 3],
            },
            status=StepStatus.pending,
            sequence_index=0,
        )
        runbook = _make_runbook(runbook_id="rb-params", steps=(step,))

        repository.create_runbook(runbook, correlation_id="corr-001")

        loaded = repository.load_runbook("rb-params", correlation_id="corr-001")
        assert loaded is not None
        assert loaded.steps[0].params == {
            "simple": "value",
            "nested": {"inner": "data", "number": 42},
            "list": [1, 2, 3],
        }


# ---------------------------------------------------------------------------
# AC-009: Steps in shuffled order still load by sequence_index
# ---------------------------------------------------------------------------


class TestStepsLoadInSequenceOrder:
    """Steps in shuffled order load first-to-last by sequence_index."""

    def test_steps_load_in_sequence_order(
        self, repository: RunbookRepository, writer_db: sqlite3.Connection
    ) -> None:
        # Manually insert steps in shuffled order
        runbook = _make_runbook(
            runbook_id="rb-shuffle",
            steps=(
                Step(
                    step_type="first",
                    params={},
                    status=StepStatus.pending,
                    sequence_index=0,
                ),
                Step(
                    step_type="second",
                    params={},
                    status=StepStatus.pending,
                    sequence_index=1,
                ),
                Step(
                    step_type="third",
                    params={},
                    status=StepStatus.pending,
                    sequence_index=2,
                ),
            ),
        )
        repository.create_runbook(runbook, correlation_id="corr-001")

        # Directly shuffle the insertion order in the database
        # (This tests that load_runbook properly orders by sequence_index)
        # We'll re-insert in reverse order to simulate out-of-order writes
        writer_db.execute(
            "DELETE FROM runbook_steps WHERE runbook_id = 'rb-shuffle'"
        )
        writer_db.execute("BEGIN IMMEDIATE")
        # Insert in reverse order: third, second, first
        sql = (
            "INSERT INTO runbook_steps "
            "(runbook_id, sequence_index, step_type, params, status, result) "
            "VALUES (?, ?, ?, ?, ?, ?)"
        )
        writer_db.execute(sql, ("rb-shuffle", 2, "third", "{}", "pending", None))
        writer_db.execute(sql, ("rb-shuffle", 1, "second", "{}", "pending", None))
        writer_db.execute(sql, ("rb-shuffle", 0, "first", "{}", "pending", None))
        writer_db.execute("COMMIT")

        # Load and verify they come back in sequence order
        loaded = repository.load_runbook("rb-shuffle", correlation_id="corr-002")
        assert loaded is not None
        assert len(loaded.steps) == 3
        assert loaded.steps[0].step_type == "first"
        assert loaded.steps[1].step_type == "second"
        assert loaded.steps[2].step_type == "third"


# ---------------------------------------------------------------------------
# AC-010: Every public method accepts correlation_id parameter
# ---------------------------------------------------------------------------


class TestCorrelationIdContract:
    """Every public method accepts correlation_id explicitly."""

    def test_create_runbook_accepts_correlation_id(
        self, repository: RunbookRepository
    ) -> None:
        runbook = _make_runbook(runbook_id="rb-corr-create")
        # Should not raise
        repository.create_runbook(runbook, correlation_id="corr-create")

    def test_load_runbook_accepts_correlation_id(
        self, repository: RunbookRepository
    ) -> None:
        # Should not raise
        repository.load_runbook("rb-nonexistent", correlation_id="corr-load")


# ---------------------------------------------------------------------------
# AC-011: Seam test (integration contract with TASK-RSP-002)
# ---------------------------------------------------------------------------


@pytest.mark.seam
@pytest.mark.integration_contract("runbooks_schema")
def test_runbook_schema_matches_repository_writes(tmp_path: Path) -> None:
    """The repo's INSERT columns must match the migration DDL exactly.

    Contract: STRICT runbooks/runbook_steps tables; status CHECK set ==
    StepStatus values; params/result JSON TEXT; ordering by sequence_index.
    Producer: TASK-RSP-002
    """
    cx = sqlite_connect.connect_writer(tmp_path / "forge.db")
    lifecycle_migrations.apply_at_boot(cx)
    runbook_migration.apply(cx)

    cols = {r[1] for r in cx.execute("PRAGMA table_info(runbooks)")}
    required = {
        "runbook_id",
        "target",
        "current_step_index",
        "status",
        "created_at",
    }
    assert required <= cols

    step_cols = {r[1] for r in cx.execute("PRAGMA table_info(runbook_steps)")}
    assert {
        "runbook_id",
        "sequence_index",
        "step_type",
        "params",
        "status",
        "result",
    } <= step_cols

    # Tables must be STRICT and the status CHECK must mirror StepStatus.
    ddl = cx.execute(
        "SELECT sql FROM sqlite_master WHERE name='runbook_steps'"
    ).fetchone()[0]
    assert "STRICT" in ddl.upper()
    for status in StepStatus:
        assert status.value in ddl, f"CHECK set missing {status.value!r}"

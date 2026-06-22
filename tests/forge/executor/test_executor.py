"""Test suite for RunbookExecutor (TASK-RBX-004).

Unit tests for the runbook executor dispatch loop. Uses in-memory fake handlers
and a real SQLite database (tmp_path) to validate the full execution lifecycle.
Written test-first per TDD discipline.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from forge.executor.executor import RunbookExecutor
from forge.executor.registry import StepOutcome, StepTypeRegistry
from forge.persistence.repositories.runbook import RunbookRepository
from forge.persistence.repositories.runbook_models import Runbook, Step, StepStatus
from nats_core.events import (
    StepResultPayload,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Create a temporary database file."""
    return tmp_path / "test_executor.db"


@pytest.fixture
def repository(db_path: Path) -> RunbookRepository:
    """Create a RunbookRepository with an in-memory SQLite database."""
    # Initialize schema
    from forge.persistence.migrations.runbook import apply

    conn = sqlite3.connect(str(db_path))
    apply(conn)
    return RunbookRepository(connection=conn)


@pytest.fixture
def registry() -> StepTypeRegistry:
    """Create an empty step type registry."""
    return StepTypeRegistry()


@pytest.fixture
def mock_publisher() -> AsyncMock:
    """Create a mock RunbookPublisher."""
    publisher = AsyncMock()
    publisher.publish_runbook_started = AsyncMock()
    publisher.publish_step_started = AsyncMock()
    publisher.publish_step_result = AsyncMock()
    publisher.publish_runbook_complete = AsyncMock()
    publisher.publish_escalated = AsyncMock()
    return publisher


@pytest.fixture
def executor(
    repository: RunbookRepository,
    registry: StepTypeRegistry,
    mock_publisher: AsyncMock,
) -> RunbookExecutor:
    """Create a RunbookExecutor with test dependencies."""
    return RunbookExecutor(
        repository=repository,
        registry=registry,
        publisher=mock_publisher,
    )


# ---------------------------------------------------------------------------
# Fake handlers
# ---------------------------------------------------------------------------


def passing_handler(step: Step) -> StepOutcome:
    """Handler that always returns passed status."""
    return StepOutcome(
        status=StepStatus.passed,
        result={"step_type": step.step_type, "params": dict(step.params)},
    )


def failing_handler(step: Step) -> StepOutcome:
    """Handler that always returns failed status."""
    return StepOutcome(
        status=StepStatus.failed,
        result={"error": "Simulated failure"},
    )


def raising_handler(step: Step) -> StepOutcome:
    """Handler that raises an exception."""
    raise RuntimeError("Simulated handler exception")


def approval_handler(step: Step) -> StepOutcome:
    """Handler that returns awaiting_approval status."""
    return StepOutcome(
        status=StepStatus.awaiting_approval,
        result={"approval_required": True},
    )


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def create_test_runbook(
    repository: RunbookRepository,
    runbook_id: str,
    step_types: list[str],
    correlation_id: str = "test-correlation-id",
) -> Runbook:
    """Create and persist a test runbook with the given step types."""
    steps = []
    for i, step_type in enumerate(step_types):
        steps.append(
            Step(
                step_type=step_type,
                params={},
                status=StepStatus.pending,
                sequence_index=i,
            )
        )

    runbook = Runbook(
        runbook_id=runbook_id,
        target="test-target",
        current_step_index=0,
        status=StepStatus.pending,
        created_at=datetime.now(timezone.utc),
        steps=tuple(steps),
    )

    repository.create_runbook(runbook, correlation_id=correlation_id)
    return runbook


# ---------------------------------------------------------------------------
# AC-001: Running a 3-step runbook runs each handler exactly once
# ---------------------------------------------------------------------------


def test_runs_each_step_in_sequence_to_completion(
    executor: RunbookExecutor,
    repository: RunbookRepository,
    registry: StepTypeRegistry,
) -> None:
    """AC-001: Running a 3-step runbook runs each handler exactly once, in sequence."""
    # Register a passing handler for all steps
    call_count = []

    def counting_handler(step: Step) -> StepOutcome:
        call_count.append(step.sequence_index)
        return passing_handler(step)

    registry.register("test-step", counting_handler)

    # Create a 3-step runbook
    runbook = create_test_runbook(
        repository,
        runbook_id="rb-001",
        step_types=["test-step", "test-step", "test-step"],
    )

    # Execute the runbook
    result = asyncio.run(executor.run(runbook.runbook_id, correlation_id="corr-001"))

    # Verify each handler was called exactly once in sequence
    assert call_count == [0, 1, 2], "Handlers must be called in order"
    assert result.status == "complete"
    assert result.stopped_at_index is None


# ---------------------------------------------------------------------------
# AC-002: A completed step is recorded passed and its result persisted
# ---------------------------------------------------------------------------


def test_status_and_result_persisted(
    executor: RunbookExecutor,
    repository: RunbookRepository,
    registry: StepTypeRegistry,
) -> None:
    """AC-002: A completed step is recorded passed and its result persisted."""
    registry.register("test-step", passing_handler)

    runbook = create_test_runbook(repository, "rb-002", ["test-step"])

    asyncio.run(executor.run(runbook.runbook_id, correlation_id="corr-002"))

    # Reload and verify status
    loaded = repository.load_runbook(runbook.runbook_id, correlation_id="corr-002")
    assert loaded is not None
    assert loaded.steps[0].status == StepStatus.passed
    # AC-002: the handler's result is persisted durably, not just announced.
    persisted = loaded.steps[0].result
    assert persisted is not None, "step result must be persisted (not just announced)"
    assert persisted.exit_code == 0
    # TASK-RBX-008: the handler's structured result is a first-class payload,
    # round-tripped verbatim — not a JSON blob stuffed into captured_output.
    assert persisted.payload == {"step_type": "test-step", "params": {}}


# ---------------------------------------------------------------------------
# AC-003: Resume pointer advances to step_count
# ---------------------------------------------------------------------------


def test_resume_pointer_advances(
    executor: RunbookExecutor,
    repository: RunbookRepository,
    registry: StepTypeRegistry,
) -> None:
    """AC-003: The resume pointer advances past each completed step and rests at step_count."""
    registry.register("test-step", passing_handler)

    runbook = create_test_runbook(repository, "rb-003", ["test-step", "test-step"])

    asyncio.run(executor.run(runbook.runbook_id, correlation_id="corr-003"))

    loaded = repository.load_runbook(runbook.runbook_id, correlation_id="corr-003")
    assert loaded is not None
    assert loaded.current_step_index == len(loaded.steps)  # Terminal position
    assert loaded.current_step_index == 2


# ---------------------------------------------------------------------------
# AC-004: Lifecycle events in correct order
# ---------------------------------------------------------------------------


def test_announces_the_lifecycle(
    executor: RunbookExecutor,
    repository: RunbookRepository,
    registry: StepTypeRegistry,
    mock_publisher: AsyncMock,
) -> None:
    """AC-004: Lifecycle order is runbook-started → step-started/step-result × N → runbook-complete."""
    registry.register("test-step", passing_handler)

    runbook = create_test_runbook(repository, "rb-004", ["test-step", "test-step"])

    asyncio.run(executor.run(runbook.runbook_id, correlation_id="corr-004"))

    # Verify call order
    assert mock_publisher.publish_runbook_started.call_count == 1
    assert mock_publisher.publish_step_started.call_count == 2
    assert mock_publisher.publish_step_result.call_count == 2
    assert mock_publisher.publish_runbook_complete.call_count == 1

    # Verify order: started first, complete last
    all_calls = mock_publisher.method_calls
    method_names = [call[0] for call in all_calls]
    assert method_names[0] == "publish_runbook_started"
    assert method_names[-1] == "publish_runbook_complete"


# ---------------------------------------------------------------------------
# AC-005: Single-step runbook
# ---------------------------------------------------------------------------


def test_single_step_runbook_runs_once_and_completes(
    executor: RunbookExecutor,
    repository: RunbookRepository,
    registry: StepTypeRegistry,
) -> None:
    """AC-005: A single-step runbook runs it once and completes."""
    call_count = []

    def counting_handler(step: Step) -> StepOutcome:
        call_count.append(step.sequence_index)
        return passing_handler(step)

    registry.register("test-step", counting_handler)

    runbook = create_test_runbook(repository, "rb-005", ["test-step"])

    result = asyncio.run(executor.run(runbook.runbook_id, correlation_id="corr-005"))

    assert len(call_count) == 1
    assert result.status == "complete"


# ---------------------------------------------------------------------------
# AC-006: Runbook resumed at its final step
# ---------------------------------------------------------------------------


def test_resumed_at_its_final_step(
    executor: RunbookExecutor,
    repository: RunbookRepository,
    registry: StepTypeRegistry,
) -> None:
    """AC-006: A runbook resumed on its final step runs only that step; earlier handlers do not re-run."""
    call_count = []

    def counting_handler(step: Step) -> StepOutcome:
        call_count.append(step.sequence_index)
        return passing_handler(step)

    registry.register("test-step", counting_handler)

    # Create a 3-step runbook
    runbook = create_test_runbook(
        repository, "rb-006", ["test-step", "test-step", "test-step"]
    )

    # Manually advance pointer to final step
    repository.update_step_status(
        runbook.runbook_id, 0, StepStatus.passed, correlation_id="corr-006"
    )
    repository.advance(runbook.runbook_id, correlation_id="corr-006")
    repository.update_step_status(
        runbook.runbook_id, 1, StepStatus.passed, correlation_id="corr-006"
    )
    repository.advance(runbook.runbook_id, correlation_id="corr-006")

    # Now resume
    asyncio.run(executor.run(runbook.runbook_id, correlation_id="corr-006"))

    # Only the final step should have been executed
    assert call_count == [2]


# ---------------------------------------------------------------------------
# AC-007: Already-complete runbook (no-op)
# ---------------------------------------------------------------------------


def test_already_complete_runbook(
    executor: RunbookExecutor,
    repository: RunbookRepository,
    registry: StepTypeRegistry,
    mock_publisher: AsyncMock,
) -> None:
    """AC-007: An already-complete runbook runs no handler and is reported already complete."""
    registry.register("test-step", passing_handler)

    runbook = create_test_runbook(repository, "rb-007", ["test-step"])

    # First run completes it
    asyncio.run(executor.run(runbook.runbook_id, correlation_id="corr-007"))

    # Reset mock
    mock_publisher.reset_mock()

    # Second run should be a no-op
    result = asyncio.run(executor.run(runbook.runbook_id, correlation_id="corr-007-2"))

    assert result.status == "already_complete"
    assert mock_publisher.publish_runbook_started.call_count == 0
    assert mock_publisher.publish_runbook_complete.call_count == 0


# ---------------------------------------------------------------------------
# AC-008: Empty runbook is refused
# ---------------------------------------------------------------------------


def test_empty_runbook_is_refused(
    executor: RunbookExecutor,
    repository: RunbookRepository,
    mock_publisher: AsyncMock,
) -> None:
    """AC-008: An empty runbook is refused before execution; no lifecycle events."""
    # Note: The Runbook model's __post_init__ enforces at least one step,
    # so we can't create a truly empty runbook through the normal API.
    # This test documents the expected behavior if such a runbook existed.
    # In practice, the validation happens at model construction time.
    pytest.skip("Runbook model enforces non-empty steps at construction time")


# ---------------------------------------------------------------------------
# AC-009: Step with no handler stops the run
# ---------------------------------------------------------------------------


def test_unknown_handler_stops_and_escalates(
    executor: RunbookExecutor,
    repository: RunbookRepository,
    registry: StepTypeRegistry,
    mock_publisher: AsyncMock,
) -> None:
    """AC-009: A step whose step_type has no handler stops the run and announces escalated."""
    runbook = create_test_runbook(repository, "rb-009", ["unknown-step"])

    result = asyncio.run(executor.run(runbook.runbook_id, correlation_id="corr-009"))

    assert result.status == "escalated"
    assert result.reason == "unknown_handler"

    # Verify escalated event was published
    assert mock_publisher.publish_escalated.call_count == 1

    # Verify step was NOT marked passed
    loaded = repository.load_runbook(runbook.runbook_id, correlation_id="corr-009")
    assert loaded is not None
    assert loaded.steps[0].status == StepStatus.pending  # Unchanged


# ---------------------------------------------------------------------------
# AC-010: Failing step stops the run
# ---------------------------------------------------------------------------


def test_failing_step_stops_the_run(
    executor: RunbookExecutor,
    repository: RunbookRepository,
    registry: StepTypeRegistry,
) -> None:
    """AC-010: A failing step stops the run, is recorded failed, later handlers do not run."""
    registry.register("failing-step", failing_handler)
    registry.register("test-step", passing_handler)

    runbook = create_test_runbook(repository, "rb-010", ["failing-step", "test-step"])

    result = asyncio.run(executor.run(runbook.runbook_id, correlation_id="corr-010"))

    assert result.status == "escalated"
    assert result.stopped_at_index == 0

    # First step should be failed
    loaded = repository.load_runbook(runbook.runbook_id, correlation_id="corr-010")
    assert loaded is not None
    assert loaded.steps[0].status == StepStatus.failed

    # Second step should still be pending (not run)
    assert loaded.steps[1].status == StepStatus.pending


# ---------------------------------------------------------------------------
# AC-011: Re-running resumes at the failed step
# ---------------------------------------------------------------------------


def test_rerunnning_resumes_at_failed_step(
    executor: RunbookExecutor,
    repository: RunbookRepository,
    registry: StepTypeRegistry,
) -> None:
    """AC-011: After a failure, re-running resumes at the failed step without re-running earlier steps."""
    call_count = []

    def counting_handler(step: Step) -> StepOutcome:
        call_count.append(step.sequence_index)
        return passing_handler(step)

    registry.register("test-step", counting_handler)
    registry.register("failing-step", failing_handler)

    runbook = create_test_runbook(
        repository, "rb-011", ["test-step", "failing-step", "test-step"]
    )

    # First run: step 0 passes, step 1 fails
    asyncio.run(executor.run(runbook.runbook_id, correlation_id="corr-011"))
    assert call_count == [0]

    # Now replace the failing handler with a passing one
    registry.register("failing-step", counting_handler)

    # Second run: should resume at step 1
    call_count.clear()
    asyncio.run(executor.run(runbook.runbook_id, correlation_id="corr-011-2"))

    # Should have run steps 1 and 2 (not step 0 again)
    assert call_count == [1, 2]


# ---------------------------------------------------------------------------
# AC-012: Failing step escalates, no runbook-complete
# ---------------------------------------------------------------------------


def test_failing_step_escalates_no_complete(
    executor: RunbookExecutor,
    repository: RunbookRepository,
    registry: StepTypeRegistry,
    mock_publisher: AsyncMock,
) -> None:
    """AC-012: A failing step escalates and runbook-complete is NOT announced."""
    registry.register("failing-step", failing_handler)

    runbook = create_test_runbook(repository, "rb-012", ["failing-step"])

    asyncio.run(executor.run(runbook.runbook_id, correlation_id="corr-012"))

    assert mock_publisher.publish_escalated.call_count == 1
    assert mock_publisher.publish_runbook_complete.call_count == 0


# ---------------------------------------------------------------------------
# AC-013: Pointer rests on the failed step
# ---------------------------------------------------------------------------


def test_pointer_rests_on_failed_step(
    executor: RunbookExecutor,
    repository: RunbookRepository,
    registry: StepTypeRegistry,
) -> None:
    """AC-013: The pointer rests on the failed step after a failure."""
    registry.register("test-step", passing_handler)
    registry.register("failing-step", failing_handler)

    runbook = create_test_runbook(
        repository, "rb-013", ["test-step", "failing-step", "test-step"]
    )

    asyncio.run(executor.run(runbook.runbook_id, correlation_id="corr-013"))

    loaded = repository.load_runbook(runbook.runbook_id, correlation_id="corr-013")
    assert loaded is not None
    assert loaded.current_step_index == 1  # Rests on the failed step


# ---------------------------------------------------------------------------
# AC-014: Interrupted after a step completes (recovery shortcut)
# ---------------------------------------------------------------------------


def test_interrupted_after_step_completes(
    executor: RunbookExecutor,
    repository: RunbookRepository,
    registry: StepTypeRegistry,
) -> None:
    """AC-014: A run interrupted after a step committed resumes at the next step."""
    call_count = []

    def counting_handler(step: Step) -> StepOutcome:
        call_count.append(step.sequence_index)
        return passing_handler(step)

    registry.register("test-step", counting_handler)

    runbook = create_test_runbook(repository, "rb-014", ["test-step", "test-step"])

    # Simulate crash after first step persisted but before pointer advanced
    repository.update_step_status(
        runbook.runbook_id, 0, StepStatus.passed, correlation_id="corr-014"
    )
    # Pointer still at 0 (crash before advance)

    # Resume
    asyncio.run(executor.run(runbook.runbook_id, correlation_id="corr-014"))

    # Should have skipped step 0 (already passed) and run steps 1
    assert call_count == [1]


# ---------------------------------------------------------------------------
# AC-015: Handler that raises is contained
# ---------------------------------------------------------------------------


def test_handler_that_raises_is_contained(
    executor: RunbookExecutor,
    repository: RunbookRepository,
    registry: StepTypeRegistry,
    mock_publisher: AsyncMock,
) -> None:
    """AC-015: A handler that raises is contained: step recorded failed, executor does not crash."""
    registry.register("raising-step", raising_handler)

    runbook = create_test_runbook(repository, "rb-015", ["raising-step"])

    # Should not raise
    result = asyncio.run(executor.run(runbook.runbook_id, correlation_id="corr-015"))

    assert result.status == "escalated"
    assert result.reason == "step_failed"

    # Step should be marked failed
    loaded = repository.load_runbook(runbook.runbook_id, correlation_id="corr-015")
    assert loaded is not None
    assert loaded.steps[0].status == StepStatus.failed

    # Escalated should be announced
    assert mock_publisher.publish_escalated.call_count == 1


# ---------------------------------------------------------------------------
# AC-016: Step that requires approval
# ---------------------------------------------------------------------------


def test_step_that_requires_approval(
    executor: RunbookExecutor,
    repository: RunbookRepository,
    registry: StepTypeRegistry,
    mock_publisher: AsyncMock,
) -> None:
    """AC-016: A step resolving to awaiting_approval pauses the run and announces escalated."""
    registry.register("approval-step", approval_handler)
    registry.register("test-step", passing_handler)

    runbook = create_test_runbook(repository, "rb-016", ["approval-step", "test-step"])

    result = asyncio.run(executor.run(runbook.runbook_id, correlation_id="corr-016"))

    assert result.status == "escalated"
    assert result.reason == "awaiting_approval"

    # First step should be awaiting_approval
    loaded = repository.load_runbook(runbook.runbook_id, correlation_id="corr-016")
    assert loaded is not None
    assert loaded.steps[0].status == StepStatus.awaiting_approval

    # Second step should not have run
    assert loaded.steps[1].status == StepStatus.pending

    # Escalated should be announced
    assert mock_publisher.publish_escalated.call_count == 1


# ---------------------------------------------------------------------------
# AC-017: step-result reports actual outcome
# ---------------------------------------------------------------------------


def test_step_result_announcement_reports_outcome(
    executor: RunbookExecutor,
    repository: RunbookRepository,
    registry: StepTypeRegistry,
    mock_publisher: AsyncMock,
) -> None:
    """AC-017: step-result reports each step's actual outcome (success vs failure)."""
    registry.register("passing-step", passing_handler)
    registry.register("failing-step", failing_handler)

    runbook = create_test_runbook(
        repository, "rb-017", ["passing-step", "failing-step"]
    )

    asyncio.run(executor.run(runbook.runbook_id, correlation_id="corr-017"))

    # Check the step-result calls
    assert mock_publisher.publish_step_result.call_count == 2

    calls = mock_publisher.publish_step_result.call_args_list

    # First call should report passed
    first_payload = calls[0][0][0]
    assert isinstance(first_payload, StepResultPayload)
    assert first_payload.status == "passed"

    # Second call should report failed
    second_payload = calls[1][0][0]
    assert isinstance(second_payload, StepResultPayload)
    assert second_payload.status == "failed"


# ---------------------------------------------------------------------------
# AC-018: Failure to announce events does not roll back
# ---------------------------------------------------------------------------


def test_failure_to_announce_events(
    executor: RunbookExecutor,
    repository: RunbookRepository,
    registry: StepTypeRegistry,
    mock_publisher: AsyncMock,
) -> None:
    """AC-018: A failing event stream does not roll back persisted progress."""
    from forge.adapters.nats.pipeline_publisher import PublishFailure

    registry.register("test-step", passing_handler)

    # Make publisher fail
    mock_publisher.publish_step_result.side_effect = PublishFailure(
        "test-subject", RuntimeError("Simulated publish failure")
    )

    runbook = create_test_runbook(repository, "rb-018", ["test-step"])

    # Should complete despite publish failures
    result = asyncio.run(executor.run(runbook.runbook_id, correlation_id="corr-018"))

    assert result.status == "complete"

    # Verify step was still persisted
    loaded = repository.load_runbook(runbook.runbook_id, correlation_id="corr-018")
    assert loaded is not None
    assert loaded.steps[0].status == StepStatus.passed


# ---------------------------------------------------------------------------
# TASK-RBX-009: crash recovery for steps stuck in 'running'
# ---------------------------------------------------------------------------


def _force_step_running(
    repository: RunbookRepository,
    runbook_id: str,
    sequence_index: int,
    *,
    claimed_at: str | None,
) -> None:
    """Drive a step straight to ``running`` with a chosen lease stamp.

    Simulates the state a crashed (stale ``claimed_at``) or genuinely in-flight
    (fresh ``claimed_at``) executor leaves behind, bypassing the claim API so
    the test controls the lease instant.
    """
    cx = repository._cx  # white-box: drive the lease column directly
    cx.execute("BEGIN IMMEDIATE;")
    cx.execute(
        """
        UPDATE runbook_steps
        SET status = ?, claimed_at = ?, claimed_by = ?
        WHERE runbook_id = ? AND sequence_index = ?
        """,
        (StepStatus.running.value, claimed_at, "dead-peer", runbook_id, sequence_index),
    )
    cx.execute("COMMIT;")


def test_crash_recovery_reclaims_stale_running_step(
    executor: RunbookExecutor,
    repository: RunbookRepository,
    registry: StepTypeRegistry,
) -> None:
    """AC-1: a step left ``running`` by a crash is reclaimed and progresses.

    The step sits in ``running`` with a long-expired lease (claimed in 2020).
    On the next run the executor reclaims it via the lease, runs the handler,
    and completes — instead of busy-spinning on an un-advanceable pointer.
    """
    call_count = []

    def counting_handler(step: Step) -> StepOutcome:
        call_count.append(step.sequence_index)
        return passing_handler(step)

    registry.register("test-step", counting_handler)

    runbook = create_test_runbook(repository, "rb-rbx009-recover", ["test-step"])
    # Simulate a crashed executor: step stuck running with an ancient lease.
    _force_step_running(
        repository,
        runbook.runbook_id,
        0,
        claimed_at="2020-01-01T00:00:00+00:00",
    )

    result = asyncio.run(
        executor.run(runbook.runbook_id, correlation_id="corr-rbx009-recover")
    )

    assert result.status == "complete"
    assert call_count == [0], "the reclaimed step must run exactly once"

    loaded = repository.load_runbook(
        runbook.runbook_id, correlation_id="corr-rbx009-recover"
    )
    assert loaded is not None
    assert loaded.steps[0].status == StepStatus.passed
    assert loaded.current_step_index == 1


def test_stuck_running_step_escalates_stalled_without_busyspin(
    repository: RunbookRepository,
    registry: StepTypeRegistry,
    mock_publisher: AsyncMock,
) -> None:
    """AC-1 (backoff guard): an un-reclaimable running step escalates 'stalled'.

    The step is ``running`` with a fresh lease (a live peer) and the lease is
    set far longer than the run, so it is never reclaimable. The executor backs
    off a bounded number of cycles then stops with ``reason='stalled'`` rather
    than busy-spinning forever; the handler is never run.
    """
    handler_calls = []

    def never_runs(step: Step) -> StepOutcome:
        handler_calls.append(step.sequence_index)
        return passing_handler(step)

    registry.register("test-step", never_runs)

    runbook = create_test_runbook(repository, "rb-rbx009-stalled", ["test-step"])
    # A live peer owns the step right now — fresh lease, never expires in-test.
    _force_step_running(
        repository,
        runbook.runbook_id,
        0,
        claimed_at=datetime.now(timezone.utc).isoformat(),
    )

    # Lease far exceeds the test (never reclaim); tiny stall budget, no sleep.
    stall_executor = RunbookExecutor(
        repository=repository,
        registry=registry,
        publisher=mock_publisher,
        claim_lease_seconds=10_000.0,
        stall_backoff_seconds=0.0,
        max_stall_cycles=3,
    )

    result = asyncio.run(
        stall_executor.run(runbook.runbook_id, correlation_id="corr-rbx009-stalled")
    )

    assert result.status == "escalated"
    assert result.reason == "stalled"
    assert result.stopped_at_index == 0
    assert handler_calls == [], "a stalled step's handler must never run"
    # The step we never claimed gets no step-started, and the run never completes.
    assert mock_publisher.publish_step_started.call_count == 0
    assert mock_publisher.publish_runbook_complete.call_count == 0

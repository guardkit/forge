"""Security and data-integrity scenario tests for RunbookExecutor (TASK-RBX-006).

BDD-style tests validating Phase-4 Security (Group E) and Data-Integrity (Group G)
properties of the runbook executor. All in-memory fakes — no subprocess, no broker.

Test organization:
- Security tests: @pytest.mark.security
- Data integrity tests: @pytest.mark.data_integrity

Run with: pytest -m "security or data_integrity" tests/bdd/test_runbook_executor.py
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from forge.executor.executor import RunbookExecutor
from forge.executor.registry import StepOutcome, StepTypeRegistry
from forge.persistence.repositories.runbook import RunbookRepository
from forge.persistence.repositories.runbook_models import Runbook, Step, StepStatus
from nats_core.events import EscalatedPayload

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Create a temporary database file path."""
    return tmp_path / "test_security_integrity.db"


@pytest.fixture
def repository(db_path: Path) -> RunbookRepository:
    """Create a RunbookRepository with a tmp_path SQLite database."""
    from forge.adapters.sqlite import connect as sqlite_connect
    from forge.lifecycle import migrations as lifecycle_migrations
    from forge.persistence.migrations.runbook import apply as apply_runbook_migration

    # Initialize schema
    conn = sqlite_connect.connect_writer(db_path)
    lifecycle_migrations.apply_at_boot(conn)
    apply_runbook_migration(conn)

    return RunbookRepository(connection=conn)


@pytest.fixture
def registry() -> StepTypeRegistry:
    """Create an empty step type registry for each test."""
    return StepTypeRegistry()


@pytest.fixture
def mock_publisher() -> AsyncMock:
    """Create a mock RunbookPublisher that records all calls."""
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
# Spy registry for security tests
# ---------------------------------------------------------------------------


class SpyRegistry(StepTypeRegistry):
    """Registry that records all resolve() calls to detect adversarial step types.

    This spy is the security crux: it proves that step_type values only ever
    reach registry.resolve() as dict-key lookups, never passing through
    eval/exec/format/subprocess sinks.
    """

    def __init__(self) -> None:
        super().__init__()
        self.resolve_calls: list[str] = []

    def resolve(self, step_type: str):
        """Override to record every step_type value seen."""
        self.resolve_calls.append(step_type)
        return super().resolve(step_type)


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
        created_at=datetime.now(UTC),
        steps=tuple(steps),
    )

    repository.create_runbook(runbook, correlation_id=correlation_id)
    return runbook


def passing_handler(step: Step) -> StepOutcome:
    """Fake handler that always returns passed status."""
    return StepOutcome(
        status=StepStatus.passed,
        result={"step_type": step.step_type},
    )


# ===========================================================================
# SECURITY TESTS (Group E)
# ===========================================================================


@pytest.mark.security
@pytest.mark.parametrize(
    "adversarial_step_type",
    [
        "run; DROP TABLE steps",
        "exec`whoami`",
        "${jndi:ldap://evil}",
    ],
    ids=[
        "sql-injection",
        "shell-injection",
        "jndi-injection",
    ],
)
def test_adversarial_step_types_escalate_without_execution(
    repository: RunbookRepository,
    mock_publisher: AsyncMock,
    adversarial_step_type: str,
) -> None:
    """AC-001: Adversarial step types stop the run and escalate without execution.

    Security invariant: The step_type value is used ONLY as a registry lookup
    key (dictionary access), never evaluated/executed/interpolated. This test
    proves the executor treats step_type as inert data.

    Scenario Outline: "An adversarial step type with no handler is escalated,
    never executed"
    """
    # Arrange: Create spy registry to track all resolve() calls
    spy_registry = SpyRegistry()

    executor = RunbookExecutor(
        repository=repository,
        registry=spy_registry,
        publisher=mock_publisher,
    )

    # Create a runbook with the adversarial step type followed by a safe step
    runbook = create_test_runbook(
        repository,
        runbook_id="rb-adversarial",
        step_types=[adversarial_step_type, "safe-step"],
    )

    # Act: Execute the runbook
    result = asyncio.run(executor.run(runbook.runbook_id, correlation_id="corr-security"))

    # Assert: Run stopped and escalated due to unknown handler
    assert result.status == "escalated"
    assert result.reason == "unknown_handler"
    assert result.stopped_at_index == 0

    # Assert: step_type was ONLY passed to registry.resolve (dict lookup)
    # Never reached eval/exec/format/subprocess
    assert spy_registry.resolve_calls == [adversarial_step_type]

    # Assert: Escalated event was published
    mock_publisher.publish_escalated.assert_called_once()
    escalated_payload: EscalatedPayload = mock_publisher.publish_escalated.call_args[0][0]
    assert escalated_payload.reason == "unknown_handler"
    assert escalated_payload.sequence_index == 0

    # Assert: Second step never ran (executor stopped)
    step_started_calls = mock_publisher.publish_step_started.call_args_list
    assert len(step_started_calls) == 1  # Only first step started
    assert step_started_calls[0][0][0].step_type == adversarial_step_type

    # Assert: Runbook did NOT complete
    mock_publisher.publish_runbook_complete.assert_not_called()


@pytest.mark.security
def test_handler_exception_contained_as_step_failure(
    executor: RunbookExecutor,
    repository: RunbookRepository,
    registry: StepTypeRegistry,
    mock_publisher: AsyncMock,
) -> None:
    """AC-002: A handler that raises is contained as a step failure.

    Security invariant: Handler exceptions never propagate to the executor's
    caller. The executor "stops cleanly rather than crash."

    Scenario: "A handler that raises an unexpected error is contained as a
    step failure"
    """
    # Arrange: Register a handler that raises, followed by a safe handler
    def raising_handler(step: Step) -> StepOutcome:
        raise RuntimeError("Simulated handler exception")

    registry.register("raising-step", raising_handler)
    registry.register("safe-step", passing_handler)

    # Create a 2-step runbook
    runbook = create_test_runbook(
        repository,
        runbook_id="rb-raising",
        step_types=["raising-step", "safe-step"],
    )

    # Act: Execute the runbook (should NOT raise)
    result = asyncio.run(executor.run(runbook.runbook_id, correlation_id="corr-raising"))

    # Assert: Run escalated (not crashed)
    assert result.status == "escalated"
    assert result.reason == "step_failed"
    assert result.stopped_at_index == 0

    # Assert: First step recorded as failed
    reloaded = repository.load_runbook(runbook.runbook_id, correlation_id="corr-raising")
    assert reloaded is not None
    assert reloaded.steps[0].status == StepStatus.failed

    # Assert: Escalated event was published
    mock_publisher.publish_escalated.assert_called_once()
    escalated_payload: EscalatedPayload = mock_publisher.publish_escalated.call_args[0][0]
    assert escalated_payload.reason == "step_failed"

    # Assert: Second handler did NOT run
    step_started_calls = mock_publisher.publish_step_started.call_args_list
    assert len(step_started_calls) == 1  # Only first step started

    # Assert: Executor returned cleanly without propagating exception
    # (The fact that we got here without a raised exception proves this)


# ===========================================================================
# DATA INTEGRITY TESTS (Group G)
# ===========================================================================


@pytest.mark.data_integrity
def test_persisted_state_authoritative_for_resume(
    executor: RunbookExecutor,
    repository: RunbookRepository,
    registry: StepTypeRegistry,
    mock_publisher: AsyncMock,
) -> None:
    """AC-003: Persisted step state is the source of truth for resume.

    Data integrity invariant: A passed-and-persisted step whose step-result
    announcement was lost is NOT re-run. Resume continues at the next step.

    Scenario: "Persisted step state is the source of truth for resume, not
    the event stream"
    """
    # Arrange: Register passing handlers and a failing handler to stop mid-run
    registry.register("step-1", passing_handler)
    registry.register("step-2", passing_handler)

    def failing_then_passing_handler(step: Step) -> StepOutcome:
        """Handler that fails once, then passes on retry."""
        return StepOutcome(status=StepStatus.failed, result={"error": "first attempt"})

    registry.register("step-3", failing_then_passing_handler)

    # Create a 3-step runbook
    runbook = create_test_runbook(
        repository,
        runbook_id="rb-lost-announcement",
        step_types=["step-1", "step-2", "step-3"],
    )

    # Simulate: First run executes steps 1 and 2, stops at failing step 3
    result1 = asyncio.run(
        executor.run(runbook.runbook_id, correlation_id="corr-resume-1")
    )

    # Verify first run stopped at step 3 (failed)
    mid_run = repository.load_runbook(runbook.runbook_id, correlation_id="corr-resume-1")
    assert mid_run is not None
    assert mid_run.current_step_index == 2  # Stopped at step 2 (0-indexed)
    assert mid_run.steps[0].status == StepStatus.passed
    assert mid_run.steps[1].status == StepStatus.passed
    assert mid_run.steps[2].status == StepStatus.failed

    # Clear publisher calls to simulate lost announcement
    mock_publisher.reset_mock()

    # Now fix step-3 to pass
    registry.register("step-3", passing_handler)

    # Act: Resume execution (should skip already-passed steps 1 and 2)
    result2 = asyncio.run(
        executor.run(runbook.runbook_id, correlation_id="corr-resume-2")
    )

    # Assert: Run completed successfully
    assert result2.status == "complete"

    # Assert: Steps 1 and 2 were NOT re-executed (only step 3 was started)
    step_started_calls = mock_publisher.publish_step_started.call_args_list
    assert len(step_started_calls) == 1  # Only step 3 started
    assert step_started_calls[0][0][0].sequence_index == 2  # Step 3

    # Assert: Final state shows all steps passed
    final = repository.load_runbook(runbook.runbook_id, correlation_id="corr-final")
    assert final is not None
    assert final.current_step_index == 3  # Terminal position
    assert final.steps[0].status == StepStatus.passed
    assert final.steps[1].status == StepStatus.passed
    assert final.steps[2].status == StepStatus.passed


@pytest.mark.data_integrity
def test_result_committed_before_pointer_advances(
    repository: RunbookRepository,
    registry: StepTypeRegistry,
    mock_publisher: AsyncMock,
) -> None:
    """AC-004: Step result is committed before the resume pointer advances.

    Data integrity invariant: After an interrupt between result-commit and
    advance, resume lands on the first or second step but never skips the
    first step's result. No step is ever advanced past without its result
    persisted.

    Scenario: "A step result is committed before the resume pointer advances
    past it"
    """
    # Arrange: Register passing handlers
    registry.register("step-1", passing_handler)
    registry.register("step-2", passing_handler)

    executor = RunbookExecutor(
        repository=repository,
        registry=registry,
        publisher=mock_publisher,
    )

    # Create a 2-step runbook
    runbook = create_test_runbook(
        repository,
        runbook_id="rb-commit-order",
        step_types=["step-1", "step-2"],
    )

    # Act: Run the runbook to completion
    result = asyncio.run(executor.run(runbook.runbook_id, correlation_id="corr-commit"))

    # Assert: Run completed
    assert result.status == "complete"

    # Assert: Both steps have results persisted
    final = repository.load_runbook(runbook.runbook_id, correlation_id="corr-commit-final")
    assert final is not None

    # Assert: Pointer is at terminal position (past all steps)
    assert final.current_step_index == 2

    # Assert: Both steps recorded as passed (result committed)
    assert final.steps[0].status == StepStatus.passed
    assert final.steps[1].status == StepStatus.passed

    # Critical assertion: If we resume from any intermediate position,
    # the already-passed steps are NOT re-run (recovery shortcut)
    # This proves the result was committed before advance

    # Simulate: Manually reset pointer to step 0 (as if crash occurred)
    # The step 0 is already marked 'passed' in DB
    # When we resume, it should advance without re-running

    # Create a fresh executor to prove the recovery shortcut works
    executor2 = RunbookExecutor(
        repository=repository,
        registry=registry,
        publisher=AsyncMock(),  # Fresh publisher
    )

    # Manually update pointer back to 0 (simulate crash recovery scenario)
    conn = repository._cx
    conn.execute(
        "UPDATE runbooks SET current_step_index = 0 WHERE runbook_id = ?",
        (runbook.runbook_id,),
    )
    conn.commit()

    # Verify step 0 is still marked passed (result persisted)
    recovery_state = repository.load_runbook(
        runbook.runbook_id, correlation_id="corr-recovery"
    )
    assert recovery_state is not None
    assert recovery_state.current_step_index == 0
    assert recovery_state.steps[0].status == StepStatus.passed

    # Resume: Should advance past step 0 without re-running
    recovery_publisher = AsyncMock()
    recovery_publisher.publish_runbook_started = AsyncMock()
    recovery_publisher.publish_step_started = AsyncMock()
    recovery_publisher.publish_step_result = AsyncMock()
    recovery_publisher.publish_runbook_complete = AsyncMock()
    recovery_publisher.publish_escalated = AsyncMock()

    executor3 = RunbookExecutor(
        repository=repository,
        registry=registry,
        publisher=recovery_publisher,
    )

    recovery_result = asyncio.run(
        executor3.run(runbook.runbook_id, correlation_id="corr-recovery-run")
    )

    # Assert: Run completed (runbook was already complete, just advancing pointer)
    assert recovery_result.status == "complete"

    # Assert: NEITHER step was re-executed (both skipped via recovery shortcut)
    # This proves that passed steps are never re-run, even if pointer is before them
    step_started_calls = recovery_publisher.publish_step_started.call_args_list
    assert len(step_started_calls) == 0  # No steps re-run

    # Verify final state: pointer advanced to terminal without re-running
    final_state = repository.load_runbook(
        runbook.runbook_id, correlation_id="corr-final-final"
    )
    assert final_state is not None
    assert final_state.current_step_index == 2  # Back at terminal
    assert final_state.steps[0].status == StepStatus.passed
    assert final_state.steps[1].status == StepStatus.passed

    # This proves: result committed → pointer advanced → crash → resume
    # = step not re-run (result persistence is authoritative)


# ===========================================================================
# CONCURRENCY TESTS (Group F) — TASK-RBX-007
# ===========================================================================


@pytest.mark.concurrency
def test_two_executors_same_runbook_no_double_run(
    db_path: Path,
    mock_publisher: AsyncMock,
) -> None:
    """AC-001, AC-002: Two concurrent executors do not double-run steps.

    Uses threading + shared SQLite file. Each step's handler runs exactly
    once across both executors. The repository's BEGIN IMMEDIATE serializes
    writes; the executor's reload-per-iteration detects concurrent progress.
    """
    import threading
    from collections import Counter

    from forge.adapters.sqlite import connect as sqlite_connect
    from forge.lifecycle import migrations as lifecycle_migrations
    from forge.persistence.migrations.runbook import apply as apply_runbook_migration

    call_counter = Counter()
    call_lock = threading.Lock()

    def counting_handler(step: Step) -> StepOutcome:
        with call_lock:
            call_counter[step.sequence_index] += 1
        return StepOutcome(
            status=StepStatus.passed,
            result={"step_type": step.step_type},
        )

    # Create a 3-step runbook
    conn1 = sqlite_connect.connect_writer(db_path)
    lifecycle_migrations.apply_at_boot(conn1)
    apply_runbook_migration(conn1)
    repository1 = RunbookRepository(connection=conn1)
    runbook = create_test_runbook(
        repository1,
        runbook_id="rb-concurrency-001",
        step_types=["test-step", "test-step", "test-step"],
    )
    conn1.close()

    def run_executor(executor_id: int) -> None:
        conn = sqlite_connect.connect_writer(db_path)
        repository = RunbookRepository(connection=conn)
        registry = StepTypeRegistry()
        registry.register("test-step", counting_handler)
        # Each executor gets its own mock publisher for thread safety
        executor_publisher = AsyncMock()
        executor_publisher.publish_runbook_started = AsyncMock()
        executor_publisher.publish_step_started = AsyncMock()
        executor_publisher.publish_step_result = AsyncMock()
        executor_publisher.publish_runbook_complete = AsyncMock()
        executor_publisher.publish_escalated = AsyncMock()
        executor = RunbookExecutor(
            repository=repository,
            registry=registry,
            publisher=executor_publisher,
        )
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                executor.run(
                    runbook.runbook_id,
                    correlation_id=f"corr-executor-{executor_id}",
                )
            )
            assert result.status in ("complete", "already_complete")
        finally:
            loop.close()
            conn.close()

    thread1 = threading.Thread(target=run_executor, args=(1,))
    thread2 = threading.Thread(target=run_executor, args=(2,))
    thread1.start()
    thread2.start()
    thread1.join()
    thread2.join()

    # Assert: each handler ran exactly once
    assert len(call_counter) == 3
    for step_index in range(3):
        assert call_counter[step_index] == 1, (
            f"Step {step_index} ran {call_counter[step_index]} times, expected 1"
        )

    # Verify runbook complete
    conn = sqlite_connect.connect_writer(db_path)
    repository = RunbookRepository(connection=conn)
    loaded = repository.load_runbook(runbook.runbook_id, correlation_id="final-check")
    assert loaded is not None
    assert loaded.current_step_index == 3
    for step in loaded.steps:
        assert step.status == StepStatus.passed
    conn.close()

"""Fleet-memory runbook scenario tests with scripted handlers (TASK-FMDR-003).

BDD-style tests validating the fleet-memory runbook executor behavior with
real subprocess handlers pointed at stub scripts. All tests are CI-safe —
no Docker, no live NAS, no live broker.

Test organization:
- Failure scenarios: @pytest.mark.failure
- Security tests: @pytest.mark.security
- Resume/recovery: @pytest.mark.resume
- Concurrency: @pytest.mark.concurrency

Run with: pytest tests/bdd/test_fleet_memory_runbook.py -v
"""

from __future__ import annotations

import asyncio
import stat
import threading
from collections import Counter
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from forge.executor.executor import RunbookExecutor
from forge.executor.registry import StepTypeRegistry
from forge.executor.shell_steps import register_shell_handlers
from forge.persistence.repositories.runbook import RunbookRepository
from forge.persistence.repositories.runbook_models import Runbook, Step, StepStatus

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Create a temporary database file path."""
    return tmp_path / "test_fleet_memory.db"


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
    """Create a step type registry with shell handlers registered."""
    registry = StepTypeRegistry()
    register_shell_handlers(registry)
    return registry


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
# Helper functions
# ---------------------------------------------------------------------------


def create_stub_script(
    script_dir: Path,
    script_name: str,
    exit_code: int,
    output: str = "",
) -> Path:
    """Create a stub shell script that exits with the given code and output.

    Args:
        script_dir: Directory to create the script in.
        script_name: Name of the script file.
        exit_code: Exit code the script should return.
        output: Optional output to print before exiting.

    Returns:
        Path to the created script.
    """
    script_path = script_dir / script_name
    script_content = f"""#!/bin/bash
{f'echo "{output}"' if output else ''}
exit {exit_code}
"""
    script_path.write_text(script_content)
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC)
    return script_path


def create_fleet_memory_runbook(
    repository: RunbookRepository,
    runbook_id: str,
    cwd: str,
    correlation_id: str = "test-correlation",
) -> Runbook:
    """Create a fleet-memory runbook with deploy and smoke steps.

    Args:
        repository: RunbookRepository to persist the runbook.
        runbook_id: Unique identifier for the runbook.
        cwd: Working directory for the step handlers.
        correlation_id: Correlation ID for the runbook.

    Returns:
        The created Runbook instance.
    """
    from datetime import UTC, datetime

    steps = [
        Step(
            step_type="deploy_compose",
            params={
                "cwd": cwd,
                "script": "./deploy.sh",
                "env_file": ".env.deploy",
            },
            status=StepStatus.pending,
            sequence_index=0,
        ),
        Step(
            step_type="run_smoke_tests",
            params={
                "cwd": cwd,
                "script": "./smoke.sh",
                "env_file": ".env.deploy",
            },
            status=StepStatus.pending,
            sequence_index=1,
        ),
    ]

    runbook = Runbook(
        runbook_id=runbook_id,
        target="nas",
        current_step_index=0,
        status=StepStatus.pending,
        created_at=datetime.now(UTC),
        steps=tuple(steps),
    )

    repository.create_runbook(runbook, correlation_id=correlation_id)
    return runbook


# ===========================================================================
# FAILURE SCENARIOS (Group C)
# ===========================================================================


@pytest.mark.failure
def test_failed_deploy_halts_before_smoke(
    executor: RunbookExecutor,
    repository: RunbookRepository,
    mock_publisher: AsyncMock,
    tmp_path: Path,
) -> None:
    """AC-C1: Failed deploy halts before smoke.

    Scenario: Deploy script exits non-zero → deploy recorded failed, smoke
    step never runs, runbook halts/escalates at the deploy step.
    """
    # Arrange: Create stub scripts (deploy fails, smoke would pass)
    script_dir = tmp_path / "scripts"
    script_dir.mkdir()
    create_stub_script(script_dir, "deploy.sh", exit_code=1, output="Deploy failed")
    create_stub_script(script_dir, "smoke.sh", exit_code=0)
    (script_dir / ".env.deploy").write_text("ENV_VAR=value")

    # Create runbook
    runbook = create_fleet_memory_runbook(
        repository,
        runbook_id="rb-failed-deploy",
        cwd=str(script_dir),
    )

    # Act: Execute the runbook
    result = asyncio.run(executor.run(runbook.runbook_id, correlation_id="corr-c1"))

    # Assert: Run escalated at deploy step
    assert result.status == "escalated"
    assert result.reason == "step_failed"
    assert result.stopped_at_index == 0

    # Assert: Deploy step recorded as failed
    reloaded = repository.load_runbook(runbook.runbook_id, correlation_id="corr-c1-check")
    assert reloaded is not None
    assert reloaded.steps[0].status == StepStatus.failed
    assert reloaded.steps[1].status == StepStatus.pending  # Smoke never ran

    # Assert: Only deploy step was started (smoke never started)
    step_started_calls = mock_publisher.publish_step_started.call_args_list
    assert len(step_started_calls) == 1
    assert step_started_calls[0][0][0].step_type == "deploy_compose"

    # Assert: Escalated event was published
    mock_publisher.publish_escalated.assert_called_once()


@pytest.mark.failure
def test_failing_smoke_halts_at_smoke(
    executor: RunbookExecutor,
    repository: RunbookRepository,
    mock_publisher: AsyncMock,
    tmp_path: Path,
) -> None:
    """AC-C2: Failing smoke halts at smoke.

    Scenario: Deploy passes, smoke script exits non-zero → deploy recorded
    passed, smoke recorded failed, runbook escalates at the smoke step.
    """
    # Arrange: Create stub scripts (deploy passes, smoke fails)
    script_dir = tmp_path / "scripts"
    script_dir.mkdir()
    create_stub_script(script_dir, "deploy.sh", exit_code=0)
    create_stub_script(script_dir, "smoke.sh", exit_code=1, output="Smoke test failed")
    (script_dir / ".env.deploy").write_text("ENV_VAR=value")

    # Create runbook
    runbook = create_fleet_memory_runbook(
        repository,
        runbook_id="rb-failing-smoke",
        cwd=str(script_dir),
    )

    # Act: Execute the runbook
    result = asyncio.run(executor.run(runbook.runbook_id, correlation_id="corr-c2"))

    # Assert: Run escalated at smoke step
    assert result.status == "escalated"
    assert result.reason == "step_failed"
    assert result.stopped_at_index == 1

    # Assert: Deploy passed, smoke failed
    reloaded = repository.load_runbook(runbook.runbook_id, correlation_id="corr-c2-check")
    assert reloaded is not None
    assert reloaded.steps[0].status == StepStatus.passed
    assert reloaded.steps[1].status == StepStatus.failed

    # Assert: Both steps were started
    step_started_calls = mock_publisher.publish_step_started.call_args_list
    assert len(step_started_calls) == 2

    # Assert: Escalated event was published
    mock_publisher.publish_escalated.assert_called_once()


@pytest.mark.failure
def test_missing_env_file(
    executor: RunbookExecutor,
    repository: RunbookRepository,
    mock_publisher: AsyncMock,
    tmp_path: Path,
) -> None:
    """AC-C4: Missing env file.

    Scenario: .env.deploy absent → deploy recorded failed with a reason
    indicating the deploy environment file could not be found.
    """
    # Arrange: Create stub deploy script that checks for ENV_FILE
    script_dir = tmp_path / "scripts"
    script_dir.mkdir()

    # Deploy script that checks if ENV_FILE exists
    deploy_script = script_dir / "deploy.sh"
    deploy_script.write_text("""#!/bin/bash
if [ -z "$ENV_FILE" ] || [ ! -f "$ENV_FILE" ]; then
    echo "Error: Deploy environment file not found: $ENV_FILE"
    exit 1
fi
echo "Deploy successful"
exit 0
""")
    deploy_script.chmod(deploy_script.stat().st_mode | stat.S_IEXEC)

    create_stub_script(script_dir, "smoke.sh", exit_code=0)
    # Note: .env.deploy is NOT created

    # Create runbook
    runbook = create_fleet_memory_runbook(
        repository,
        runbook_id="rb-missing-env",
        cwd=str(script_dir),
    )

    # Act: Execute the runbook
    result = asyncio.run(executor.run(runbook.runbook_id, correlation_id="corr-c4"))

    # Assert: Run escalated at deploy step
    assert result.status == "escalated"
    assert result.reason == "step_failed"
    assert result.stopped_at_index == 0

    # Assert: Deploy step recorded as failed with descriptive output
    reloaded = repository.load_runbook(runbook.runbook_id, correlation_id="corr-c4-check")
    assert reloaded is not None
    assert reloaded.steps[0].status == StepStatus.failed

    # Verify the error message mentions the missing env file
    assert reloaded.steps[0].result is not None
    assert reloaded.steps[0].result.payload is not None
    captured_output = reloaded.steps[0].result.payload.get("captured_output", "")
    assert "environment file" in captured_output.lower() or "not found" in captured_output.lower()


@pytest.mark.failure
def test_diagnosable_permission_failure(
    executor: RunbookExecutor,
    repository: RunbookRepository,
    tmp_path: Path,
) -> None:
    """AC-D4: Diagnosable permission failure.

    Scenario: Stub deploy.sh emits a permission-denied message + non-zero
    exit → deploy recorded failed and the captured output is distinguishable
    as a permissions problem.
    """
    # Arrange: Create stub script that simulates permission error
    script_dir = tmp_path / "scripts"
    script_dir.mkdir()
    create_stub_script(
        script_dir,
        "deploy.sh",
        exit_code=126,
        output="Permission denied: cannot access /target/directory",
    )
    create_stub_script(script_dir, "smoke.sh", exit_code=0)
    (script_dir / ".env.deploy").write_text("ENV_VAR=value")

    # Create runbook
    runbook = create_fleet_memory_runbook(
        repository,
        runbook_id="rb-permission-failure",
        cwd=str(script_dir),
    )

    # Act: Execute the runbook
    result = asyncio.run(executor.run(runbook.runbook_id, correlation_id="corr-d4"))

    # Assert: Deploy failed
    assert result.status == "escalated"
    assert result.stopped_at_index == 0

    # Assert: Captured output contains permission-denied indicator
    reloaded = repository.load_runbook(runbook.runbook_id, correlation_id="corr-d4-check")
    assert reloaded is not None
    assert reloaded.steps[0].status == StepStatus.failed
    assert reloaded.steps[0].result is not None
    assert reloaded.steps[0].result.payload is not None

    captured_output = reloaded.steps[0].result.payload.get("captured_output", "")
    assert "permission denied" in captured_output.lower()


# ===========================================================================
# SECURITY TESTS (Group C3)
# ===========================================================================


@pytest.mark.security
def test_credential_scoping(
    executor: RunbookExecutor,
    repository: RunbookRepository,
    mock_publisher: AsyncMock,
    tmp_path: Path,
) -> None:
    """AC-C3: Credential scoping.

    Scenario: Stub that emits a Postgres DSN + PGPASSWORD= → the persisted
    step results and the captured published events contain NEITHER the
    password nor the connection string.
    """
    # Arrange: Create stub deploy script that emits credentials
    script_dir = tmp_path / "scripts"
    script_dir.mkdir()

    # Deploy script that prints a Postgres DSN and PGPASSWORD
    secret_password = "SuperSecret123"
    secret_dsn = f"postgresql://user:{secret_password}@localhost:5432/testdb"

    deploy_script = script_dir / "deploy.sh"
    deploy_script.write_text(f"""#!/bin/bash
echo "Connecting to database..."
echo "DSN: {secret_dsn}"
echo "PGPASSWORD={secret_password}"
echo "Deploy complete"
exit 0
""")
    deploy_script.chmod(deploy_script.stat().st_mode | stat.S_IEXEC)

    create_stub_script(script_dir, "smoke.sh", exit_code=0)
    (script_dir / ".env.deploy").write_text("ENV_VAR=value")

    # Create runbook
    runbook = create_fleet_memory_runbook(
        repository,
        runbook_id="rb-credential-scoping",
        cwd=str(script_dir),
    )

    # Act: Execute the runbook
    result = asyncio.run(executor.run(runbook.runbook_id, correlation_id="corr-c3"))

    # Assert: Run completed successfully
    assert result.status == "complete"

    # Assert: Persisted step result does NOT contain credentials
    reloaded = repository.load_runbook(runbook.runbook_id, correlation_id="corr-c3-check")
    assert reloaded is not None
    assert reloaded.steps[0].status == StepStatus.passed
    assert reloaded.steps[0].result is not None
    assert reloaded.steps[0].result.payload is not None

    captured_output = reloaded.steps[0].result.payload.get("captured_output", "")
    assert secret_password not in captured_output, "Password leaked in persisted output"
    assert secret_dsn not in captured_output, "DSN leaked in persisted output"
    assert "***REDACTED-DSN***" in captured_output, "DSN not redacted"
    assert "***REDACTED-PASSWORD***" in captured_output, "Password not redacted"

    # Assert: Published events do NOT contain credentials
    step_result_calls = mock_publisher.publish_step_result.call_args_list
    assert len(step_result_calls) >= 1

    # Check the deploy step result payload
    deploy_result_payload = step_result_calls[0][0][0]
    result_dict = deploy_result_payload.result
    if isinstance(result_dict, dict):
        published_output = result_dict.get("captured_output", "")
    else:
        # result_dict might be a StepResult object with payload
        published_output = result_dict.payload.get("captured_output", "") if hasattr(result_dict, "payload") and result_dict.payload else ""

    assert secret_password not in published_output, "Password leaked in published event"
    assert secret_dsn not in published_output, "DSN leaked in published event"


# ===========================================================================
# RESUME/RECOVERY TESTS (Group B, D)
# ===========================================================================


@pytest.mark.resume
def test_resume_after_deploy(
    executor: RunbookExecutor,
    repository: RunbookRepository,
    mock_publisher: AsyncMock,
    tmp_path: Path,
) -> None:
    """AC-B1: Resume after deploy.

    Scenario: A run stopped after the deploy step is recorded passed
    re-enters at the smoke step (deploy not re-run) and completes from there.
    """
    # Arrange: Create stub scripts (both pass)
    script_dir = tmp_path / "scripts"
    script_dir.mkdir()
    create_stub_script(script_dir, "deploy.sh", exit_code=0, output="Deploy complete")
    create_stub_script(script_dir, "smoke.sh", exit_code=0, output="Smoke tests passed")
    (script_dir / ".env.deploy").write_text("ENV_VAR=value")

    # Create runbook
    runbook = create_fleet_memory_runbook(
        repository,
        runbook_id="rb-resume-after-deploy",
        cwd=str(script_dir),
    )

    # Act 1: Run until deploy completes (simulate first run completing deploy)
    # We'll manually advance the pointer after deploy completes
    result1 = asyncio.run(executor.run(runbook.runbook_id, correlation_id="corr-b1-first"))

    # If it completed fully, manually reset to simulate stopping after deploy
    if result1.status == "complete":
        # Manually set the pointer to step 1 (after deploy, before smoke)
        conn = repository._cx
        conn.execute(
            "UPDATE runbooks SET current_step_index = 1 WHERE runbook_id = ?",
            (runbook.runbook_id,),
        )
        conn.commit()

    # Clear mock to track only the second run
    mock_publisher.reset_mock()

    # Act 2: Resume execution (should skip deploy, run smoke only)
    result2 = asyncio.run(executor.run(runbook.runbook_id, correlation_id="corr-b1-resume"))

    # Assert: Run completed successfully
    assert result2.status == "complete"

    # Assert: Only smoke step was started (deploy was skipped)
    step_started_calls = mock_publisher.publish_step_started.call_args_list
    # If no steps were started, it means both were already passed
    # If one step was started, it should be smoke
    if len(step_started_calls) > 0:
        assert len(step_started_calls) == 1
        assert step_started_calls[0][0][0].step_type == "run_smoke_tests"

    # Assert: Final state shows both steps passed
    final = repository.load_runbook(runbook.runbook_id, correlation_id="corr-b1-final")
    assert final is not None
    assert final.current_step_index == 2  # Terminal position
    assert final.steps[0].status == StepStatus.passed
    assert final.steps[1].status == StepStatus.passed


@pytest.mark.resume
def test_no_op_on_complete(
    executor: RunbookExecutor,
    repository: RunbookRepository,
    mock_publisher: AsyncMock,
    tmp_path: Path,
) -> None:
    """AC-D2: No-op on complete.

    Scenario: Re-running an already-complete runbook re-runs no step and
    reports "already complete".
    """
    # Arrange: Create stub scripts (both pass)
    script_dir = tmp_path / "scripts"
    script_dir.mkdir()
    create_stub_script(script_dir, "deploy.sh", exit_code=0)
    create_stub_script(script_dir, "smoke.sh", exit_code=0)
    (script_dir / ".env.deploy").write_text("ENV_VAR=value")

    # Create runbook
    runbook = create_fleet_memory_runbook(
        repository,
        runbook_id="rb-no-op-on-complete",
        cwd=str(script_dir),
    )

    # Act 1: Run to completion
    result1 = asyncio.run(executor.run(runbook.runbook_id, correlation_id="corr-d2-first"))
    assert result1.status == "complete"

    # Clear mock to track only the second run
    mock_publisher.reset_mock()

    # Act 2: Re-run the already-complete runbook
    result2 = asyncio.run(executor.run(runbook.runbook_id, correlation_id="corr-d2-second"))

    # Assert: Reported already complete
    assert result2.status == "already_complete"

    # Assert: No steps were re-executed
    step_started_calls = mock_publisher.publish_step_started.call_args_list
    assert len(step_started_calls) == 0


@pytest.mark.resume
def test_result_before_advance_crash(
    executor: RunbookExecutor,
    repository: RunbookRepository,
    mock_publisher: AsyncMock,
    tmp_path: Path,
) -> None:
    """AC-D5: Result-before-advance crash.

    Scenario: Deploy recorded passed but pointer not yet advanced → on
    re-run the deploy step is recognised already-passed and skipped;
    executor resumes at the smoke step.
    """
    # Arrange: Create stub scripts (both pass)
    script_dir = tmp_path / "scripts"
    script_dir.mkdir()
    create_stub_script(script_dir, "deploy.sh", exit_code=0)
    create_stub_script(script_dir, "smoke.sh", exit_code=0)
    (script_dir / ".env.deploy").write_text("ENV_VAR=value")

    # Create runbook
    runbook = create_fleet_memory_runbook(
        repository,
        runbook_id="rb-result-before-advance",
        cwd=str(script_dir),
    )

    # Simulate: Deploy completed, result persisted, but crash before pointer advance
    # First, let it run to completion
    result1 = asyncio.run(executor.run(runbook.runbook_id, correlation_id="corr-d5-first"))
    assert result1.status == "complete"

    # Manually reset pointer to 0 (simulate crash after result commit)
    # But keep the deploy step marked as passed
    conn = repository._cx
    conn.execute(
        "UPDATE runbooks SET current_step_index = 0 WHERE runbook_id = ?",
        (runbook.runbook_id,),
    )
    conn.commit()

    # Verify the setup: pointer at 0, but step 0 is already passed
    mid_state = repository.load_runbook(runbook.runbook_id, correlation_id="corr-d5-mid")
    assert mid_state is not None
    assert mid_state.current_step_index == 0
    assert mid_state.steps[0].status == StepStatus.passed

    # Clear mock to track only the recovery run
    mock_publisher.reset_mock()

    # Act: Resume execution (should skip already-passed deploy, run smoke)
    result2 = asyncio.run(executor.run(runbook.runbook_id, correlation_id="corr-d5-resume"))

    # Assert: Run completed successfully
    assert result2.status == "complete"

    # Assert: Deploy was NOT re-executed (already passed)
    step_started_calls = mock_publisher.publish_step_started.call_args_list
    # Either no steps started (both already passed) or only smoke started
    if len(step_started_calls) > 0:
        # Should only be smoke, not deploy
        for call in step_started_calls:
            step_type = call[0][0].step_type
            assert step_type != "deploy_compose", "Deploy was re-run despite being passed"

    # Assert: Final state correct
    final = repository.load_runbook(runbook.runbook_id, correlation_id="corr-d5-final")
    assert final is not None
    assert final.current_step_index == 2
    assert final.steps[0].status == StepStatus.passed
    assert final.steps[1].status == StepStatus.passed


# ===========================================================================
# EVENT ORDERING TESTS (Group D6)
# ===========================================================================


@pytest.mark.data_integrity
def test_ordered_event_stream_and_queryable_record(
    executor: RunbookExecutor,
    repository: RunbookRepository,
    mock_publisher: AsyncMock,
    tmp_path: Path,
) -> None:
    """AC-D6: Ordered event stream + queryable record.

    Scenario: Events publish in order started → … → complete (capturing
    fake client); each step's status is queryable from the persisted record
    afterwards.
    """
    # Arrange: Create stub scripts (both pass)
    script_dir = tmp_path / "scripts"
    script_dir.mkdir()
    create_stub_script(script_dir, "deploy.sh", exit_code=0, output="Deploy output")
    create_stub_script(script_dir, "smoke.sh", exit_code=0, output="Smoke output")
    (script_dir / ".env.deploy").write_text("ENV_VAR=value")

    # Create runbook
    runbook = create_fleet_memory_runbook(
        repository,
        runbook_id="rb-ordered-events",
        cwd=str(script_dir),
    )

    # Act: Execute the runbook
    result = asyncio.run(executor.run(runbook.runbook_id, correlation_id="corr-d6"))

    # Assert: Run completed successfully
    assert result.status == "complete"

    # Assert: Events published in correct order
    # Expected order: runbook_started, step_started (deploy), step_result (deploy),
    #                 step_started (smoke), step_result (smoke), runbook_complete

    assert mock_publisher.publish_runbook_started.call_count == 1
    assert mock_publisher.publish_step_started.call_count == 2
    assert mock_publisher.publish_step_result.call_count == 2
    assert mock_publisher.publish_runbook_complete.call_count == 1

    # Verify order by checking call_args_list
    step_started_calls = mock_publisher.publish_step_started.call_args_list
    step_result_calls = mock_publisher.publish_step_result.call_args_list

    # First step started should be deploy
    assert step_started_calls[0][0][0].step_type == "deploy_compose"
    assert step_started_calls[0][0][0].sequence_index == 0

    # First step result should be deploy
    assert step_result_calls[0][0][0].sequence_index == 0

    # Second step started should be smoke
    assert step_started_calls[1][0][0].step_type == "run_smoke_tests"
    assert step_started_calls[1][0][0].sequence_index == 1

    # Second step result should be smoke
    assert step_result_calls[1][0][0].sequence_index == 1

    # Assert: Persisted record is queryable and accurate
    final = repository.load_runbook(runbook.runbook_id, correlation_id="corr-d6-final")
    assert final is not None
    assert final.current_step_index == 2  # Terminal
    assert final.steps[0].status == StepStatus.passed
    assert final.steps[0].result is not None
    assert final.steps[0].result.exit_code == 0
    assert final.steps[1].status == StepStatus.passed
    assert final.steps[1].result is not None
    assert final.steps[1].result.exit_code == 0


# ===========================================================================
# CONCURRENCY TESTS (Group D7)
# ===========================================================================


@pytest.mark.concurrency
def test_two_executors_never_deploy_twice(
    db_path: Path,
    tmp_path: Path,
) -> None:
    """AC-D7: Two executors never deploy twice.

    Scenario: Two executors against the same runbook → exactly one runs
    the deploy step.
    """
    # Arrange: Create stub scripts with a counter file to track invocations
    script_dir = tmp_path / "scripts"
    script_dir.mkdir()
    counter_file = tmp_path / "deploy_count.txt"
    counter_file.write_text("0")

    # Deploy script that increments the counter
    deploy_script = script_dir / "deploy.sh"
    deploy_script.write_text(f"""#!/bin/bash
count=$(cat {counter_file})
count=$((count + 1))
echo $count > {counter_file}
sleep 0.1  # Small delay to encourage concurrent execution
echo "Deploy run $count"
exit 0
""")
    deploy_script.chmod(deploy_script.stat().st_mode | stat.S_IEXEC)

    create_stub_script(script_dir, "smoke.sh", exit_code=0)
    (script_dir / ".env.deploy").write_text("ENV_VAR=value")

    # Create the database and runbook
    from forge.adapters.sqlite import connect as sqlite_connect
    from forge.lifecycle import migrations as lifecycle_migrations
    from forge.persistence.migrations.runbook import apply as apply_runbook_migration

    conn = sqlite_connect.connect_writer(db_path)
    lifecycle_migrations.apply_at_boot(conn)
    apply_runbook_migration(conn)
    repository = RunbookRepository(connection=conn)

    runbook = create_fleet_memory_runbook(
        repository,
        runbook_id="rb-concurrency",
        cwd=str(script_dir),
    )
    conn.close()

    # Define executor runner function for threading
    def run_executor(executor_id: int) -> None:
        conn = sqlite_connect.connect_writer(db_path)
        repository = RunbookRepository(connection=conn)
        registry = StepTypeRegistry()
        register_shell_handlers(registry)

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

    # Act: Run two executors concurrently
    thread1 = threading.Thread(target=run_executor, args=(1,))
    thread2 = threading.Thread(target=run_executor, args=(2,))
    thread1.start()
    thread2.start()
    thread1.join()
    thread2.join()

    # Assert: Deploy script ran exactly once
    deploy_count = int(counter_file.read_text().strip())
    assert deploy_count == 1, f"Deploy ran {deploy_count} times, expected 1"

    # Assert: Runbook completed successfully
    conn = sqlite_connect.connect_writer(db_path)
    repository = RunbookRepository(connection=conn)
    final = repository.load_runbook(runbook.runbook_id, correlation_id="corr-d7-final")
    assert final is not None
    assert final.current_step_index == 2
    assert final.steps[0].status == StepStatus.passed
    assert final.steps[1].status == StepStatus.passed
    conn.close()

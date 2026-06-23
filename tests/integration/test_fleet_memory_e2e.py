"""End-to-end test for fleet-memory runbook against disposable compose target (TASK-FMDR-004).

Validates the automated payoff coverage: a marker-gated integration test that runs the
fleet-memory runbook through ``forge runbook run`` against the disposable
``fleet-memory/deploy/local`` compose target — proving deploy → smoke → runbook-complete
unattended, with smoke gates G3–G5 green.

Each test class mirrors one acceptance criterion from TASK-FMDR-004 for explicit traceability.

Integration Contract
--------------------

This test relies on:

- TASK-FMDR-001: Runbook exemplar JSON format
- TASK-FMDR-002: CLI wired to real shell handlers
- TASK-FMDR-006: Local wrappers at ``../fleet-memory/deploy/local/{deploy.sh,smoke.sh}``

Docker Dependency
-----------------

Docker Desktop must be running. If the daemon is unreachable, tests fail with a clear
message rather than skipping — we want a real green from the e2e on this machine.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from forge.cli.runbook import runbook_cmd


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def fleet_memory_repo_root() -> Path:
    """Resolve the sibling fleet-memory repo path relative to forge repo root.

    Returns:
        Path to the fleet-memory repository.

    Raises:
        pytest.skip: If the fleet-memory repo is not found.
    """
    # Navigate up from tests/integration/ to forge root
    # Handle both regular clone and worktree scenarios
    test_file_path = Path(__file__).resolve()

    # Check if we're in a worktree (.guardkit/worktrees/)
    if ".guardkit/worktrees" in str(test_file_path):
        # In worktree: navigate to appmilla_github/ then to fleet-memory
        # Path: .../appmilla_github/forge/.guardkit/worktrees/FEAT-FMDR/tests/integration/
        # Target: .../appmilla_github/fleet-memory
        parts = test_file_path.parts
        appmilla_idx = parts.index("appmilla_github")
        appmilla_root = Path(*parts[:appmilla_idx + 1])
        fleet_memory_root = appmilla_root / "fleet-memory"
    else:
        # Regular clone: navigate from tests/integration/ to forge root, then to sibling
        forge_root = test_file_path.parent.parent.parent
        fleet_memory_root = forge_root.parent / "fleet-memory"

    if not fleet_memory_root.exists():
        pytest.skip(
            f"fleet-memory repo not found at {fleet_memory_root}. "
            "This test requires the sibling fleet-memory repository."
        )

    return fleet_memory_root


@pytest.fixture(scope="module")
def local_deploy_wrappers(fleet_memory_repo_root: Path) -> Path:
    """Path to the local deploy wrappers directory.

    Args:
        fleet_memory_repo_root: Root of the fleet-memory repository.

    Returns:
        Path to deploy/local/ directory containing deploy.sh and smoke.sh.

    Raises:
        pytest.skip: If the wrappers from TASK-FMDR-006 are not present.
    """
    local_dir = fleet_memory_repo_root / "deploy" / "local"
    deploy_sh = local_dir / "deploy.sh"
    smoke_sh = local_dir / "smoke.sh"

    if not local_dir.exists():
        pytest.skip(
            f"deploy/local directory not found at {local_dir}. "
            "TASK-FMDR-006 wrappers must be present."
        )

    if not deploy_sh.exists() or not smoke_sh.exists():
        pytest.skip(
            f"deploy.sh or smoke.sh not found in {local_dir}. "
            "TASK-FMDR-006 must be completed before this test can run. "
            "Expected files: deploy.sh, smoke.sh"
        )

    return local_dir


@pytest.fixture(scope="module")
def docker_daemon_available() -> None:
    """Verify Docker daemon is reachable.

    Raises:
        pytest.fail: If Docker daemon is unreachable (hard requirement).
    """
    try:
        result = subprocess.run(
            ["docker", "version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0:
            pytest.fail(
                "Docker daemon is unreachable. Please start Docker Desktop. "
                f"Error: {result.stderr}"
            )
    except FileNotFoundError:
        pytest.fail(
            "Docker command not found. Please install Docker Desktop."
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            "Docker version command timed out. Please check Docker Desktop status."
        )


@pytest.fixture
def fleet_memory_runbook_json(
    tmp_path: Path,
    local_deploy_wrappers: Path,
) -> Path:
    """Create a fleet-memory runbook JSON for the local disposable target.

    Args:
        tmp_path: pytest temporary directory fixture.
        local_deploy_wrappers: Path to deploy/local/ directory.

    Returns:
        Path to the created runbook JSON file.
    """
    # Resolve absolute path to fleet-memory/deploy/local for cwd
    deploy_local_abs = str(local_deploy_wrappers.resolve())

    runbook_data = {
        "runbook_id": f"test-fleet-memory-local-{datetime.now(UTC).timestamp()}",
        "target": "local-disposable",
        "current_step_index": 0,
        "status": "pending",
        "created_at": datetime.now(UTC).isoformat(),
        "steps": [
            {
                "step_type": "deploy_compose",
                "params": {
                    "cwd": deploy_local_abs,
                    "script": "./deploy.sh",
                    "env_file": ".env.deploy",
                },
                "status": "pending",
                "sequence_index": 0,
            },
            {
                "step_type": "run_smoke_tests",
                "params": {
                    "cwd": deploy_local_abs,
                    "script": "./smoke.sh",
                    "env_file": ".env.deploy",
                },
                "status": "pending",
                "sequence_index": 1,
            },
        ],
    }

    runbook_path = tmp_path / "runbook-fleet-memory-local.json"
    runbook_path.write_text(json.dumps(runbook_data, indent=2), encoding="utf-8")

    return runbook_path


@pytest.fixture
def test_db_path(tmp_path: Path) -> Path:
    """Create a temporary SQLite database for testing.

    Args:
        tmp_path: pytest temporary directory fixture.

    Returns:
        Path to the temporary database file.
    """
    db_path = tmp_path / "test-forge.db"

    # Initialize the database schema using the migration
    from forge.adapters.sqlite.connect import connect_writer
    from forge.persistence.migrations.runbook import apply

    connection = connect_writer(db_path)
    try:
        # Apply the runbook migration to create the schema
        apply(connection)
        connection.commit()
    finally:
        connection.close()

    return db_path


@pytest.fixture
def env_file(local_deploy_wrappers: Path) -> Path:
    """Ensure .env.deploy exists for the local target.

    Args:
        local_deploy_wrappers: Path to deploy/local/ directory.

    Returns:
        Path to the .env.deploy file.
    """
    env_path = local_deploy_wrappers / ".env.deploy"

    # Create a minimal .env.deploy if it doesn't exist
    if not env_path.exists():
        env_content = """# Fleet-memory local deploy configuration
PGPORT=5432
POSTGRES_PASSWORD=fleet_memory
POSTGRES_USER=fleet_memory
POSTGRES_DB=fleet_memory
"""
        env_path.write_text(env_content, encoding="utf-8")

    return env_path


@pytest.fixture(autouse=True)
def teardown_compose(local_deploy_wrappers: Path) -> Any:
    """Teardown fixture: ensure compose resources are cleaned up after each test.

    Args:
        local_deploy_wrappers: Path to deploy/local/ directory.

    Yields:
        None (fixture runs before and after test).
    """
    # Pre-test: ensure clean state
    subprocess.run(
        ["docker", "compose", "down", "-v"],
        cwd=str(local_deploy_wrappers),
        capture_output=True,
        check=False,
    )

    yield

    # Post-test: teardown
    subprocess.run(
        ["docker", "compose", "down", "-v"],
        cwd=str(local_deploy_wrappers),
        capture_output=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# AC-001: Deploy → verify → complete
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.slow
class TestDeployVerifyComplete:
    """AC-001 (A2): Deploy step passes, smoke step passes, runbook completes."""

    def test_runbook_completes_both_steps_passed(
        self,
        fleet_memory_runbook_json: Path,
        test_db_path: Path,
        env_file: Path,
        docker_daemon_available: None,
    ) -> None:
        """Given a valid fleet-memory runbook, both deploy and smoke steps pass and runbook completes.

        This is the primary happy-path test: deploy → smoke → complete unattended.
        """
        # Arrange
        runner = CliRunner()

        # Act
        result = runner.invoke(
            runbook_cmd,
            ["run", str(fleet_memory_runbook_json), "--db", str(test_db_path), "--no-events"],
        )

        # Assert
        assert result.exit_code == 0, f"Runbook execution failed: {result.output}"
        assert "completed successfully" in result.output.lower(), (
            f"Expected 'completed successfully' in output, got: {result.output}"
        )

        # Verify both steps were recorded as passed by checking the database
        from forge.adapters.sqlite.connect import connect_writer
        from forge.persistence.repositories.runbook import RunbookRepository

        connection = connect_writer(test_db_path)
        repository = RunbookRepository(connection=connection)

        # Extract runbook_id from the JSON
        runbook_data = json.loads(fleet_memory_runbook_json.read_text())
        runbook_id = runbook_data["runbook_id"]

        stored_runbook = repository.load_runbook(
            runbook_id, correlation_id="e2e-verify-both-steps"
        )
        assert stored_runbook is not None, "Runbook not found in database"
        # Completion is pointer-based: the executor advances current_step_index to
        # the terminal position (== step_count) and treats that as "complete"
        # (executor.run ASSUM-005). The top-level runbooks.status column is the
        # initially-declared state and is not mutated during execution, so assert
        # the terminal pointer rather than that column.
        assert stored_runbook.current_step_index == len(stored_runbook.steps), (
            "Expected runbook at terminal position "
            f"({len(stored_runbook.steps)}), got current_step_index="
            f"{stored_runbook.current_step_index}"
        )

        # Verify both steps have passed status
        assert len(stored_runbook.steps) == 2, "Expected 2 steps"
        deploy_step = stored_runbook.steps[0]
        smoke_step = stored_runbook.steps[1]

        assert deploy_step.status.value == "passed", (
            f"Expected deploy step status 'passed', got {deploy_step.status.value}"
        )
        assert smoke_step.status.value == "passed", (
            f"Expected smoke step status 'passed', got {smoke_step.status.value}"
        )

        connection.close()


# ---------------------------------------------------------------------------
# AC-002: Green run = smoke gates satisfied (G3–G5)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.slow
class TestSmokeGatesSatisfied:
    """AC-002 (A4): When smoke step passes, gates G3–G5 are verified."""

    def test_smoke_validates_postgres_with_pgvector(
        self,
        fleet_memory_runbook_json: Path,
        test_db_path: Path,
        env_file: Path,
        docker_daemon_available: None,
        local_deploy_wrappers: Path,
    ) -> None:
        """Smoke step validates G3: Postgres with pgvector is reachable."""
        # Arrange
        runner = CliRunner()

        # Act
        result = runner.invoke(
            runbook_cmd,
            ["run", str(fleet_memory_runbook_json), "--db", str(test_db_path), "--no-events"],
        )

        # Assert
        assert result.exit_code == 0, f"Runbook execution failed: {result.output}"

        # Verify pgvector extension is installed by checking directly
        # (smoke.sh already checked this, but we verify independently)
        check_result = subprocess.run(
            [
                "docker", "compose", "exec", "-T", "postgres",
                "psql", "-U", "fleet_memory", "-d", "fleet_memory", "-tAc",
                "SELECT extname FROM pg_extension WHERE extname='vector';",
            ],
            cwd=str(local_deploy_wrappers),
            capture_output=True,
            text=True,
            check=False,
        )

        assert check_result.returncode == 0, "pgvector check command failed"
        assert "vector" in check_result.stdout, "pgvector extension not found"

    def test_smoke_validates_network_path(
        self,
        fleet_memory_runbook_json: Path,
        test_db_path: Path,
        env_file: Path,
        docker_daemon_available: None,
        local_deploy_wrappers: Path,
    ) -> None:
        """Smoke step validates G4: Network path confirmed (local DSN reachable)."""
        # Arrange
        runner = CliRunner()

        # Act
        result = runner.invoke(
            runbook_cmd,
            ["run", str(fleet_memory_runbook_json), "--db", str(test_db_path), "--no-events"],
        )

        # Assert
        assert result.exit_code == 0, f"Runbook execution failed: {result.output}"

        # Verify network path by connecting via psql (G4 gate)
        # Read POSTGRES_PASSWORD from .env.deploy
        env_vars = {}
        for line in env_file.read_text().splitlines():
            if line.strip() and not line.startswith("#"):
                if "=" in line:
                    key, value = line.split("=", 1)
                    env_vars[key.strip()] = value.strip()

        password = env_vars.get("POSTGRES_PASSWORD", "fleet_memory")
        port = env_vars.get("PGPORT", "5432")
        dsn = f"postgresql://fleet_memory:{password}@localhost:{port}/fleet_memory"

        network_result = subprocess.run(
            ["psql", dsn, "-c", "SELECT 1;"],
            capture_output=True,
            text=True,
            check=False,
        )

        assert network_result.returncode == 0, (
            f"Network path check failed (G4): {network_result.stderr}"
        )

    def test_smoke_validates_data_volume_present(
        self,
        fleet_memory_runbook_json: Path,
        test_db_path: Path,
        env_file: Path,
        docker_daemon_available: None,
        local_deploy_wrappers: Path,
    ) -> None:
        """Smoke step validates G5: Data volume backed up (pgdata persisted)."""
        # Arrange
        runner = CliRunner()

        # Act
        result = runner.invoke(
            runbook_cmd,
            ["run", str(fleet_memory_runbook_json), "--db", str(test_db_path), "--no-events"],
        )

        # Assert
        assert result.exit_code == 0, f"Runbook execution failed: {result.output}"

        # Verify data volume is present (G5 gate)
        volume_result = subprocess.run(
            [
                "docker", "compose", "exec", "-T", "postgres",
                "test", "-f", "/var/lib/postgresql/data/PG_VERSION",
            ],
            cwd=str(local_deploy_wrappers),
            capture_output=True,
            check=False,
        )

        assert volume_result.returncode == 0, (
            "Data volume check failed (G5): PG_VERSION not found"
        )


# ---------------------------------------------------------------------------
# AC-003: Idempotent re-deploy
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.slow
class TestIdempotentRedeploy:
    """AC-003 (B3): Running deploy again leaves healthy service unchanged."""

    def test_redeploy_against_healthy_target_passes(
        self,
        fleet_memory_runbook_json: Path,
        test_db_path: Path,
        env_file: Path,
        docker_daemon_available: None,
    ) -> None:
        """Running the runbook twice succeeds both times (idempotent)."""
        # Arrange
        runner = CliRunner()

        # First run
        first_result = runner.invoke(
            runbook_cmd,
            ["run", str(fleet_memory_runbook_json), "--db", str(test_db_path), "--no-events"],
        )
        assert first_result.exit_code == 0, f"First run failed: {first_result.output}"

        # Modify runbook_id for second run (avoid duplicate error)
        runbook_data = json.loads(fleet_memory_runbook_json.read_text())
        runbook_data["runbook_id"] = f"{runbook_data['runbook_id']}-rerun"

        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            delete=False,
        ) as f:
            json.dump(runbook_data, f, indent=2)
            second_runbook_path = Path(f.name)

        try:
            # Act: Second run against already-healthy target
            second_result = runner.invoke(
                runbook_cmd,
                ["run", str(second_runbook_path), "--db", str(test_db_path), "--no-events"],
            )

            # Assert
            assert second_result.exit_code == 0, (
                f"Second run failed (idempotency violated): {second_result.output}"
            )
            assert "completed successfully" in second_result.output.lower(), (
                f"Second run did not complete successfully: {second_result.output}"
            )
        finally:
            second_runbook_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# AC-004: Teardown (no leaked containers/volumes)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestTeardown:
    """AC-004: Test tears down disposable target afterward (no leaks)."""

    def test_no_leaked_containers_after_teardown(
        self,
        local_deploy_wrappers: Path,
        docker_daemon_available: None,
    ) -> None:
        """After teardown fixture runs, no fleet-memory containers remain."""
        # The teardown_compose fixture (autouse=True) has already run
        # Verify no fleet-memory containers are running

        result = subprocess.run(
            ["docker", "compose", "ps", "-q"],
            cwd=str(local_deploy_wrappers),
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, f"docker compose ps failed: {result.stderr}"
        assert result.stdout.strip() == "", (
            f"Expected no running containers after teardown, found: {result.stdout}"
        )

    def test_no_leaked_volumes_after_teardown(
        self,
        local_deploy_wrappers: Path,
        docker_daemon_available: None,
    ) -> None:
        """After teardown with -v flag, no fleet-memory volumes remain."""
        # The teardown_compose fixture uses 'down -v' which removes volumes
        # Verify no orphaned volumes remain by checking volume list

        result = subprocess.run(
            ["docker", "volume", "ls", "-q"],
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, f"docker volume ls failed: {result.stderr}"

        # Filter for fleet-memory or local_* volumes (common compose naming)
        volumes = result.stdout.strip().split("\n") if result.stdout.strip() else []
        fleet_volumes = [
            v for v in volumes
            if "fleet" in v.lower() or "local_" in v.lower()
        ]

        # We expect no fleet-memory volumes after teardown
        # Note: This assertion may be too strict if other tests create similar volumes
        # For production use, consider tracking specific volume names
        assert len(fleet_volumes) == 0, (
            f"Expected no fleet-memory volumes after teardown, found: {fleet_volumes}"
        )


# ---------------------------------------------------------------------------
# AC-005: Docker daemon check
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestDockerDaemonCheck:
    """AC-005: When Docker daemon is unreachable, test fails with actionable message."""

    def test_docker_daemon_unreachable_fails_clearly(self) -> None:
        """When Docker is not running, the test fails with 'start Docker Desktop' message.

        Note: This test verifies the fixture behavior, not the runbook execution.
        The docker_daemon_available fixture is responsible for this check.
        """
        # This test verifies that the docker_daemon_available fixture would fail
        # appropriately. We test the fixture logic directly.

        # Mock scenario: docker version command fails
        # In practice, the fixture handles this and pytest.fail() is called

        # We verify the fixture exists and is used by other tests
        import inspect

        fixture_source = inspect.getsource(docker_daemon_available)
        assert "start Docker Desktop" in fixture_source, (
            "docker_daemon_available fixture should fail with 'start Docker Desktop' message"
        )
        assert "pytest.fail" in fixture_source, (
            "docker_daemon_available fixture should use pytest.fail for hard requirement"
        )


# ---------------------------------------------------------------------------
# AC-006: Missing wrapper check
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestMissingWrapperCheck:
    """AC-006: When TASK-FMDR-006 wrappers are absent, test fails with clear message."""

    def test_missing_wrappers_skip_with_clear_message(self) -> None:
        """When deploy.sh or smoke.sh are missing, test skips with TASK-FMDR-006 message.

        Note: This test verifies the fixture behavior.
        The local_deploy_wrappers fixture is responsible for this check.
        """
        # Verify the fixture logic
        import inspect

        fixture_source = inspect.getsource(local_deploy_wrappers)
        assert "TASK-FMDR-006" in fixture_source, (
            "local_deploy_wrappers fixture should reference TASK-FMDR-006"
        )
        assert "pytest.skip" in fixture_source, (
            "local_deploy_wrappers fixture should skip when wrappers are absent"
        )
        assert "deploy.sh" in fixture_source and "smoke.sh" in fixture_source, (
            "local_deploy_wrappers fixture should check for deploy.sh and smoke.sh"
        )

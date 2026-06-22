"""Tests for ``forge runbook run`` command (TASK-RBX-005).

Each test class mirrors one acceptance criterion so the mapping between the
criterion and its verifier stays explicit (AAA pattern, AC traceability).

Uses :class:`click.testing.CliRunner` with ``tmp_path`` fixtures for runbook
files, written **test-first** (TDD).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from forge.cli.runbook import runbook_cmd
from forge.executor.executor import RunResult
from forge.persistence.repositories.runbook import RunbookDuplicateError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def valid_runbook_json(tmp_path: Path) -> Path:
    """A valid runbook JSON file for testing."""
    runbook_data = {
        "runbook_id": "test-runbook-001",
        "target": "test-target",
        "steps": [
            {
                "step_type": "shell",
                "params": {"command": "echo hello"},
                "status": "pending",
                "sequence_index": 0,
            }
        ],
        "current_step_index": 0,
        "status": "pending",
        "created_at": datetime.now(UTC).isoformat(),
    }
    path = tmp_path / "runbook.json"
    path.write_text(json.dumps(runbook_data), encoding="utf-8")
    return path


@pytest.fixture
def invalid_runbook_json(tmp_path: Path) -> Path:
    """An invalid runbook JSON file (missing required fields)."""
    path = tmp_path / "invalid.json"
    path.write_text('{"invalid": "data"}', encoding="utf-8")
    return path


@pytest.fixture
def mock_repository(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock RunbookRepository for testing."""
    mock = MagicMock()
    mock.create_runbook = MagicMock()

    def mock_repo_factory(*args: Any, **kwargs: Any) -> MagicMock:
        return mock

    import forge.cli.runbook
    monkeypatch.setattr(forge.cli.runbook, "_build_repository", mock_repo_factory)
    return mock


@pytest.fixture
def mock_executor(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock RunbookExecutor for testing."""
    mock = MagicMock()

    # Make run return a coroutine
    async def async_run(*args: Any, **kwargs: Any) -> RunResult:
        return RunResult(status="complete")

    mock.run = MagicMock(side_effect=async_run)

    def mock_executor_factory(*args: Any, **kwargs: Any) -> MagicMock:
        return mock

    import forge.cli.runbook
    monkeypatch.setattr(forge.cli.runbook, "_build_executor", mock_executor_factory)
    return mock


# ---------------------------------------------------------------------------
# AC-001: Running a runbook from the command line
# ---------------------------------------------------------------------------


class TestRunbookExecution:
    """AC-001: forge runbook run <path> loads, executes, and reports completion."""

    def test_run_valid_runbook_succeeds(
        self,
        valid_runbook_json: Path,
        mock_repository: MagicMock,
        mock_executor: MagicMock,
    ) -> None:
        """Given a valid runbook file, loads it, executes steps, and reports completion."""
        # Arrange
        runner = CliRunner()

        # Act
        result = runner.invoke(runbook_cmd, ["run", str(valid_runbook_json)])

        # Assert
        assert result.exit_code == 0
        assert "complete" in result.output.lower()
        mock_repository.create_runbook.assert_called_once()
        mock_executor.run.assert_called_once()


# ---------------------------------------------------------------------------
# AC-002: Persist-then-execute ordering (ASSUM-007)
# ---------------------------------------------------------------------------


class TestPersistBeforeExecute:
    """AC-002: Runbook is persisted before execution; duplicate refuses cleanly."""

    def test_runbook_persisted_before_execution(
        self,
        valid_runbook_json: Path,
        mock_repository: MagicMock,
        mock_executor: MagicMock,
    ) -> None:
        """Verify create_runbook is called before executor.run."""
        # Arrange
        runner = CliRunner()
        call_order: list[str] = []

        def record_create(*args: Any, **kwargs: Any) -> None:
            call_order.append("create")

        async def record_run(*args: Any, **kwargs: Any) -> RunResult:
            call_order.append("run")
            return RunResult(status="complete")

        mock_repository.create_runbook = MagicMock(side_effect=record_create)
        mock_executor.run = MagicMock(side_effect=record_run)

        # Act
        result = runner.invoke(runbook_cmd, ["run", str(valid_runbook_json)])

        # Assert
        assert result.exit_code == 0
        assert call_order == ["create", "run"]

    def test_duplicate_runbook_reports_clearly(
        self,
        valid_runbook_json: Path,
        mock_repository: MagicMock,
        mock_executor: MagicMock,
    ) -> None:
        """Running the same runbook twice refuses duplicate with clear message."""
        # Arrange
        runner = CliRunner()
        mock_repository.create_runbook.side_effect = RunbookDuplicateError("test-runbook-001")

        # Act
        result = runner.invoke(runbook_cmd, ["run", str(valid_runbook_json)])

        # Assert
        assert result.exit_code != 0
        assert "already exists" in result.output.lower()
        mock_executor.run.assert_not_called()


# ---------------------------------------------------------------------------
# AC-003: Missing file path reports clearly (Negative)
# ---------------------------------------------------------------------------


class TestMissingFile:
    """AC-003: Missing file reports 'runbook file could not be found'; non-zero exit."""

    def test_missing_file_reports_clearly(
        self,
        tmp_path: Path,
        mock_repository: MagicMock,
        mock_executor: MagicMock,
    ) -> None:
        """Given a non-existent path, reports clear message and executes nothing."""
        # Arrange
        runner = CliRunner()
        missing_path = tmp_path / "does_not_exist.json"

        # Act
        result = runner.invoke(runbook_cmd, ["run", str(missing_path)])

        # Assert
        assert result.exit_code != 0
        assert "could not be found" in result.output.lower()
        mock_repository.create_runbook.assert_not_called()
        mock_executor.run.assert_not_called()


# ---------------------------------------------------------------------------
# AC-004: Invalid runbook file reports clearly (Negative)
# ---------------------------------------------------------------------------


class TestInvalidRunbook:
    """AC-004: Invalid runbook reports 'runbook file is invalid'; non-zero exit."""

    def test_invalid_json_reports_clearly(
        self,
        tmp_path: Path,
        mock_repository: MagicMock,
        mock_executor: MagicMock,
    ) -> None:
        """Given a file with invalid JSON, reports clear message and executes nothing."""
        # Arrange
        runner = CliRunner()
        bad_json = tmp_path / "bad.json"
        bad_json.write_text("not valid json {{{", encoding="utf-8")

        # Act
        result = runner.invoke(runbook_cmd, ["run", str(bad_json)])

        # Assert
        assert result.exit_code != 0
        assert "invalid" in result.output.lower()
        mock_repository.create_runbook.assert_not_called()
        mock_executor.run.assert_not_called()

    def test_invalid_runbook_structure_reports_clearly(
        self,
        invalid_runbook_json: Path,
        mock_repository: MagicMock,
        mock_executor: MagicMock,
    ) -> None:
        """Given a file with invalid runbook structure, reports clear message."""
        # Arrange
        runner = CliRunner()

        # Act
        result = runner.invoke(runbook_cmd, ["run", str(invalid_runbook_json)])

        # Assert
        assert result.exit_code != 0
        assert "invalid" in result.output.lower()
        mock_repository.create_runbook.assert_not_called()
        mock_executor.run.assert_not_called()


# ---------------------------------------------------------------------------
# AC-005: Help text and command registration
# ---------------------------------------------------------------------------


class TestCommandRegistration:
    """AC-005: forge runbook run --help renders; appears under forge runbook."""

    def test_help_renders(self) -> None:
        """forge runbook run --help renders successfully."""
        # Arrange
        runner = CliRunner()

        # Act
        result = runner.invoke(runbook_cmd, ["run", "--help"])

        # Assert
        assert result.exit_code == 0
        assert "run" in result.output.lower()
        assert "path" in result.output.lower()

    def test_group_help_shows_run_subcommand(self) -> None:
        """forge runbook --help shows the run subcommand."""
        # Arrange
        runner = CliRunner()

        # Act
        result = runner.invoke(runbook_cmd, ["--help"])

        # Assert
        assert result.exit_code == 0
        assert "run" in result.output.lower()


# ---------------------------------------------------------------------------
# §4 Seam Tests
# ---------------------------------------------------------------------------


@pytest.mark.seam
@pytest.mark.integration_contract("persistence_repo_surface")
class TestPersistThenExecuteSeam:
    """Seam test: verify persist-then-execute ordering (TASK-RSP-003 / ASSUM-007)."""

    def test_cli_persists_before_executing(
        self,
        valid_runbook_json: Path,
        mock_repository: MagicMock,
        mock_executor: MagicMock,
    ) -> None:
        """`forge runbook run` calls create_runbook before the executor runs.

        Contract: the runbook must have a durable home (create_runbook) before any
        step executes, so results + pointer survive a crash mid-run.
        Producer: TASK-RSP-003
        """
        # Arrange
        runner = CliRunner()
        calls: list[str] = []

        def spy_create(*args: Any, **kwargs: Any) -> None:
            calls.append("create")

        async def spy_run(*args: Any, **kwargs: Any) -> RunResult:
            calls.append("run")
            return RunResult(status="complete")

        mock_repository.create_runbook = MagicMock(side_effect=spy_create)
        mock_executor.run = MagicMock(side_effect=spy_run)

        # Act
        result = runner.invoke(runbook_cmd, ["run", str(valid_runbook_json)])

        # Assert
        assert result.exit_code == 0
        assert calls == ["create", "run"], "create_runbook must be called before executor.run"

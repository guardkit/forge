"""``forge runbook`` — CLI commands for runbook execution (TASK-RBX-005).

Wires the executor to the command line. ``forge runbook run <path>`` reads the
JSON, persists it via ``create_runbook`` (ASSUM-007), then runs it through
``RunbookExecutor`` and reports the outcome.

The runbook group is structured as a Click Group so ``forge runbook <verb>``
can grow later (e.g., ``forge runbook list``, ``forge runbook status``).
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

import click

from forge.adapters.nats.runbook_publisher import RunbookPublisher
from forge.adapters.sqlite.connect import connect_writer
from forge.executor.executor import RunbookExecutor
from forge.executor.registry import StepTypeRegistry
from forge.persistence.repositories.runbook import (
    RunbookDuplicateError,
    RunbookRepository,
)
from forge.persistence.repositories.runbook_models import Runbook, Step, StepStatus


# ---------------------------------------------------------------------------
# Runtime builders (dependency injection boundary)
# ---------------------------------------------------------------------------


def _build_repository(db_path: Path) -> RunbookRepository:
    """Build a RunbookRepository connected to the SQLite database.

    Args:
        db_path: Path to forge.db (SQLite).

    Returns:
        A wired RunbookRepository.
    """
    connection = connect_writer(db_path)
    return RunbookRepository(connection=connection)


def _build_executor(
    repository: RunbookRepository,
    registry: StepTypeRegistry,
    publisher: RunbookPublisher,
) -> RunbookExecutor:
    """Build a RunbookExecutor with the given dependencies.

    Args:
        repository: Persistence repository for loading/updating runbooks.
        registry: Step type registry for resolving handlers.
        publisher: Event publisher for announcing lifecycle events.

    Returns:
        A wired RunbookExecutor.
    """
    return RunbookExecutor(repository, registry, publisher)


# ---------------------------------------------------------------------------
# Runbook parsing and validation
# ---------------------------------------------------------------------------


def _parse_runbook_file(path: Path) -> Runbook:
    """Read and parse a runbook JSON file into a Runbook object.

    Args:
        path: Path to the runbook JSON file.

    Returns:
        A validated Runbook object.

    Raises:
        FileNotFoundError: If the file does not exist.
        json.JSONDecodeError: If the file contains invalid JSON.
        (KeyError, TypeError, ValueError): If the JSON structure is invalid.
    """
    if not path.exists():
        raise FileNotFoundError(f"Runbook file not found: {path}")

    content = path.read_text(encoding="utf-8")
    data = json.loads(content)

    # Extract required fields
    runbook_id = data["runbook_id"]
    target = data["target"]
    current_step_index = data["current_step_index"]
    status_str = data["status"]
    created_at_str = data["created_at"]
    steps_data = data["steps"]

    # Parse status
    status = StepStatus(status_str)

    # Parse created_at
    created_at = datetime.fromisoformat(created_at_str)

    # Parse steps
    steps = []
    for step_data in steps_data:
        step = Step(
            step_type=step_data["step_type"],
            params=step_data.get("params", {}),
            status=StepStatus(step_data["status"]),
            sequence_index=step_data["sequence_index"],
            result=None,  # Steps from JSON have no result yet
        )
        steps.append(step)

    # Build runbook
    runbook = Runbook(
        runbook_id=runbook_id,
        target=target,
        steps=tuple(steps),
        current_step_index=current_step_index,
        status=status,
        created_at=created_at,
    )

    return runbook


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


@click.group(name="runbook")
def runbook_cmd() -> None:
    """Runbook execution commands."""


@runbook_cmd.command(name="run")
@click.argument("path", type=click.Path(path_type=Path))
@click.option(
    "--db",
    "db_path",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    default=None,
    help="Path to forge.db (SQLite). Defaults to ~/.forge/forge.db.",
)
def run_cmd(path: Path, db_path: Path | None) -> None:
    """Execute a runbook from a JSON file.

    Reads the runbook at PATH, persists it to the database, then executes
    its steps in sequence order. Reports completion or escalation reason.

    The runbook is persisted BEFORE execution (ASSUM-007) so results and
    pointer survive a crash mid-run.
    """
    # 1. Read and parse the runbook file
    try:
        runbook = _parse_runbook_file(path)
    except FileNotFoundError:
        click.echo(
            f"forge runbook run: runbook file could not be found: {path}",
            err=True,
        )
        sys.exit(1)
    except json.JSONDecodeError as exc:
        click.echo(
            f"forge runbook run: runbook file is invalid (JSON decode error): {exc}",
            err=True,
        )
        sys.exit(1)
    except (KeyError, TypeError, ValueError) as exc:
        click.echo(
            f"forge runbook run: runbook file is invalid: {exc}",
            err=True,
        )
        sys.exit(1)

    # 2. Resolve database path
    if db_path is None:
        db_path = Path.home() / ".forge" / "forge.db"

    # 3. Build dependencies
    try:
        repository = _build_repository(db_path)
    except Exception as exc:
        click.echo(
            f"forge runbook run: failed to connect to database: {exc}",
            err=True,
        )
        sys.exit(1)

    # For now, use empty registry and a no-op publisher
    # (full wiring will come in integration)
    registry = StepTypeRegistry()
    publisher = RunbookPublisher(nats_client=_NoOpNATSClient())

    executor = _build_executor(repository, registry, publisher)

    # 4. Persist the runbook BEFORE execution (ASSUM-007)
    try:
        repository.create_runbook(
            runbook,
            correlation_id=f"cli-run-{runbook.runbook_id}",
        )
    except RunbookDuplicateError:
        click.echo(
            f"forge runbook run: runbook {runbook.runbook_id!r} already exists; "
            "cannot create duplicate",
            err=True,
        )
        sys.exit(1)
    except Exception as exc:
        click.echo(
            f"forge runbook run: failed to persist runbook: {exc}",
            err=True,
        )
        sys.exit(1)

    # 5. Execute the runbook
    try:
        result = asyncio.run(
            executor.run(
                runbook.runbook_id,
                correlation_id=f"cli-run-{runbook.runbook_id}",
            )
        )
    except Exception as exc:
        click.echo(
            f"forge runbook run: execution failed: {exc}",
            err=True,
        )
        sys.exit(1)

    # 6. Report the outcome
    if result.status == "complete":
        click.echo(f"Runbook {runbook.runbook_id} completed successfully")
    elif result.status == "already_complete":
        click.echo(f"Runbook {runbook.runbook_id} was already complete")
    elif result.status == "escalated":
        click.echo(
            f"Runbook {runbook.runbook_id} escalated at step {result.stopped_at_index}: "
            f"{result.reason}"
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# No-op NATS client for CLI (events not published from CLI)
# ---------------------------------------------------------------------------


class _NoOpNATSClient:
    """No-op stand-in for NATS client when events aren't published from CLI.

    The CLI runs runbooks synchronously and doesn't need to publish lifecycle
    events. This no-op client satisfies the RunbookPublisher interface without
    requiring a live NATS connection.
    """

    async def publish(self, subject: str, payload: bytes) -> None:
        """No-op publish method."""


__all__ = ["runbook_cmd"]

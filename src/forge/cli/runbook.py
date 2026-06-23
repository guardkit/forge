"""``forge runbook`` — CLI commands for runbook execution (TASK-RBX-005).

Wires the executor to the command line. ``forge runbook run <path>`` reads the
JSON, persists it via ``create_runbook`` (ASSUM-007), then runs it through
``RunbookExecutor`` and reports the outcome.

The runbook group is structured as a Click Group so ``forge runbook <verb>``
can grow later (e.g., ``forge runbook list``, ``forge runbook status``).

TASK-FMDR-002: Wired to real shell handlers (register_shell_handlers) and
real NATS publisher (nats.connect) with best-effort publishing semantics.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import click

from forge.adapters.nats.runbook_publisher import RunbookPublisher
from forge.adapters.sqlite.connect import connect_writer
from forge.executor.executor import RunbookExecutor
from forge.executor.registry import StepTypeRegistry
from forge.executor.shell_steps import register_shell_handlers
from forge.persistence.repositories.runbook import (
    RunbookDuplicateError,
    RunbookRepository,
)
from forge.persistence.repositories.runbook_models import Runbook, Step, StepStatus

logger = logging.getLogger(__name__)


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
@click.option(
    "--no-events",
    is_flag=True,
    default=False,
    help="Disable NATS event publishing (useful when no broker is available).",
)
def run_cmd(path: Path, db_path: Path | None, no_events: bool) -> None:
    """Execute a runbook from a JSON file.

    Reads the runbook at PATH, persists it to the database, then executes
    its steps in sequence order. Reports completion or escalation reason.

    The runbook is persisted BEFORE execution (ASSUM-007) so results and
    pointer survive a crash mid-run.

    TASK-FMDR-002: Uses real shell handlers and publishes lifecycle events
    to NATS (best-effort). Use --no-events to skip event publishing.
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

    # Build registry with real shell handlers (TASK-FMDR-002 AC-001)
    registry = StepTypeRegistry()
    register_shell_handlers(registry)

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

    # 5. Execute the runbook with NATS lifecycle management
    try:
        result = asyncio.run(
            _run_with_nats(
                repository,
                registry,
                runbook.runbook_id,
                no_events,
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
# NATS connection helpers (TASK-FMDR-002)
# ---------------------------------------------------------------------------


async def _run_with_nats(
    repository: RunbookRepository,
    registry: StepTypeRegistry,
    runbook_id: str,
    no_events: bool,
) -> Any:
    """Run the runbook with NATS lifecycle management.

    Connects to NATS (if events enabled), executes the runbook, and ensures
    the NATS connection is properly closed afterward.

    Args:
        repository: Runbook persistence repository.
        registry: Step type registry with registered handlers.
        runbook_id: ID of the runbook to execute.
        no_events: Whether to skip NATS event publishing.

    Returns:
        RunResult from the executor.
    """
    # Connect to NATS or use no-op client
    if no_events:
        nats_client = _NoOpNATSClient()
    else:
        nats_client = await _connect_nats_best_effort()

    try:
        # Build publisher and executor
        publisher = RunbookPublisher(nats_client=nats_client)
        executor = _build_executor(repository, registry, publisher)

        # Execute the runbook
        result = await executor.run(
            runbook_id,
            correlation_id=f"cli-run-{runbook_id}",
        )
        return result
    finally:
        # Clean up NATS connection if it's a real client
        if not isinstance(nats_client, _NoOpNATSClient):
            try:
                await nats_client.close()
            except Exception as exc:
                logger.debug("Failed to close NATS connection: %s", exc)


async def _connect_nats_best_effort() -> _NoOpNATSClient | Any:
    """Connect to NATS broker with best-effort semantics.

    Attempts to connect to the NATS broker specified by FORGE_NATS_URL env var
    (defaults to nats://127.0.0.1:4222). If connection fails, falls back to
    the no-op client so the runbook execution can still proceed.

    This implements AC-003: publishing is best-effort; if no broker is reachable,
    the run still completes.

    Returns:
        A connected NATS client, or _NoOpNATSClient if connection fails.
    """
    servers = os.environ.get("FORGE_NATS_URL", "nats://127.0.0.1:4222")

    try:
        import nats  # type: ignore[import-not-found]
    except ImportError:
        logger.warning(
            "nats-py not installed; lifecycle events will not be published. "
            "Install with: pip install nats-py"
        )
        return _NoOpNATSClient()

    try:
        client = await nats.connect(
            servers=servers,
            connect_timeout=2,  # Fail fast if broker unavailable
            max_reconnect_attempts=0,  # Don't retry on connection failure
        )
        logger.info("Connected to NATS broker at %s", servers)
        return client
    except Exception as exc:
        logger.warning(
            "Failed to connect to NATS broker at %s: %s. "
            "Runbook will execute but lifecycle events will not be published.",
            servers,
            exc,
        )
        return _NoOpNATSClient()


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

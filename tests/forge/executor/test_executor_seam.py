"""Real-repo seam test for RunbookExecutor↔RunbookRepository (TASK-RBX-008).

FEAT-RBX shipped a latent gap: the executor omitted the step ``result`` on
every ``update_step_status`` call, and the repo was missing the
``try_claim_step_for_execution`` method the executor relies on. Neither was
caught because the executor's unit tests exercised the persistence surface
loosely. This module is the regression guard the review asked for: it drives
``RunbookExecutor.run`` against a **real** ``RunbookRepository`` over a tmp
SQLite file (not a fake/mock) and asserts that status **and** the handler's
structured result round-trip through persistence.

The two failure modes it locks down (AC-3):

* If ``update_step_status(result=…)`` were dropped, the ``result`` column would
  persist NULL and ``persisted.payload`` would be ``None`` — the payload
  assertions below fail.
* If ``try_claim_step_for_execution`` were missing from the repo, the executor
  would ``AttributeError`` before running any handler — every test here fails at
  ``executor.run``. ``test_real_claim_method_transitions_step_to_running``
  additionally asserts the claim is observable (status ``running`` mid-handler).

Run explicitly with: ``pytest -m seam``.
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
from forge.persistence.migrations.runbook import apply
from forge.persistence.repositories.runbook import RunbookRepository
from forge.persistence.repositories.runbook_models import (
    Runbook,
    Step,
    StepStatus,
)

pytestmark = [
    pytest.mark.seam,
    pytest.mark.integration_contract("executor_result_contract"),
]


# ---------------------------------------------------------------------------
# Fixtures — a REAL repository over a tmp SQLite file (no fakes)
# ---------------------------------------------------------------------------


@pytest.fixture
def repository(tmp_path: Path) -> RunbookRepository:
    """A real RunbookRepository backed by an on-disk SQLite database."""
    conn = sqlite3.connect(str(tmp_path / "seam_executor.db"))
    apply(conn)
    return RunbookRepository(connection=conn)


@pytest.fixture
def registry() -> StepTypeRegistry:
    return StepTypeRegistry()


@pytest.fixture
def mock_publisher() -> AsyncMock:
    """The publisher is not the seam under test — persistence is."""
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
    return RunbookExecutor(
        repository=repository,
        registry=registry,
        publisher=mock_publisher,
    )


def _persist_runbook(
    repository: RunbookRepository,
    runbook_id: str,
    step_types: list[str],
    *,
    correlation_id: str = "seam-corr",
) -> Runbook:
    steps = tuple(
        Step(
            step_type=step_type,
            params={},
            status=StepStatus.pending,
            sequence_index=i,
        )
        for i, step_type in enumerate(step_types)
    )
    runbook = Runbook(
        runbook_id=runbook_id,
        target="seam-target",
        current_step_index=0,
        status=StepStatus.pending,
        created_at=datetime.now(timezone.utc),
        steps=steps,
    )
    repository.create_runbook(runbook, correlation_id=correlation_id)
    return runbook


# ---------------------------------------------------------------------------
# AC-2: status + result persist and round-trip through the real repo
# ---------------------------------------------------------------------------


def test_passed_step_status_and_payload_round_trip(
    executor: RunbookExecutor,
    repository: RunbookRepository,
    registry: StepTypeRegistry,
) -> None:
    """A passing step persists status=passed and its structured payload verbatim."""
    structured = {"artifact": "image:sha256-abc", "lines": [1, 2, 3], "ok": True}

    def handler(step: Step) -> StepOutcome:
        return StepOutcome(status=StepStatus.passed, result=structured)

    registry.register("build", handler)
    _persist_runbook(repository, "rb-seam-1", ["build"])

    result = asyncio.run(executor.run("rb-seam-1", correlation_id="seam-corr"))
    assert result.status == "complete"

    loaded = repository.load_runbook("rb-seam-1", correlation_id="seam-corr")
    assert loaded is not None
    persisted = loaded.steps[0].result
    assert loaded.steps[0].status == StepStatus.passed
    # If update_step_status(result=…) were dropped, this would be None.
    assert persisted is not None, "step result must round-trip through the real repo"
    assert persisted.payload == structured
    assert persisted.exit_code == 0
    assert persisted.captured_output == ""
    # Timestamps are executor-recorded metadata and survive the round-trip.
    assert persisted.completed_at >= persisted.started_at


def test_failed_step_persists_payload_and_nonzero_exit(
    executor: RunbookExecutor,
    repository: RunbookRepository,
    registry: StepTypeRegistry,
) -> None:
    """A failing step persists status=failed, its payload, and exit_code=1."""
    failure = {"error": "compile broke", "code": "E123"}

    def handler(step: Step) -> StepOutcome:
        return StepOutcome(status=StepStatus.failed, result=failure)

    registry.register("build", handler)
    _persist_runbook(repository, "rb-seam-2", ["build"])

    result = asyncio.run(executor.run("rb-seam-2", correlation_id="seam-corr"))
    assert result.status == "escalated"
    assert result.reason == "step_failed"

    loaded = repository.load_runbook("rb-seam-2", correlation_id="seam-corr")
    assert loaded is not None
    persisted = loaded.steps[0].result
    assert loaded.steps[0].status == StepStatus.failed
    assert persisted is not None
    assert persisted.payload == failure
    assert persisted.exit_code == 1


def test_multi_step_payloads_persist_per_step(
    executor: RunbookExecutor,
    repository: RunbookRepository,
    registry: StepTypeRegistry,
) -> None:
    """Each step in a multi-step run persists its own distinct payload."""

    def handler(step: Step) -> StepOutcome:
        return StepOutcome(
            status=StepStatus.passed,
            result={"index": step.sequence_index, "type": step.step_type},
        )

    registry.register("a", handler)
    registry.register("b", handler)
    _persist_runbook(repository, "rb-seam-3", ["a", "b"])

    result = asyncio.run(executor.run("rb-seam-3", correlation_id="seam-corr"))
    assert result.status == "complete"

    loaded = repository.load_runbook("rb-seam-3", correlation_id="seam-corr")
    assert loaded is not None
    assert loaded.steps[0].result is not None
    assert loaded.steps[1].result is not None
    assert loaded.steps[0].result.payload == {"index": 0, "type": "a"}
    assert loaded.steps[1].result.payload == {"index": 1, "type": "b"}


# ---------------------------------------------------------------------------
# AC-3: the real claim seam is exercised (and observable)
# ---------------------------------------------------------------------------


def test_real_claim_method_transitions_step_to_running(
    executor: RunbookExecutor,
    repository: RunbookRepository,
    registry: StepTypeRegistry,
) -> None:
    """The executor claims the step (pending→running) via the real repo method.

    The handler reads back the persisted status while it runs: a committed
    ``running`` status proves ``try_claim_step_for_execution`` fired before
    dispatch. If that method were missing, ``executor.run`` would AttributeError
    before reaching the handler.
    """
    observed: list[StepStatus] = []

    def handler(step: Step) -> StepOutcome:
        mid = repository.load_runbook("rb-seam-4", correlation_id="seam-corr")
        assert mid is not None
        observed.append(mid.steps[0].status)
        return StepOutcome(status=StepStatus.passed, result={"ok": True})

    registry.register("claimed", handler)
    _persist_runbook(repository, "rb-seam-4", ["claimed"])

    result = asyncio.run(executor.run("rb-seam-4", correlation_id="seam-corr"))
    assert result.status == "complete"
    assert observed == [StepStatus.running], (
        "step must be claimed (running) before dispatch"
    )

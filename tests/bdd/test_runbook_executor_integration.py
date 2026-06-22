"""Real-broker integration tests for RunbookExecutor (TASK-RBX-007 Group H).

Validates that lifecycle events are published to a real NATS broker and
observed by a subscriber in the correct order:
  runbook-started → step-started/step-result (per step) → runbook-complete

These tests are marked `@integration @slow` and excluded from the default
pytest run. Run explicitly with: pytest -m "integration and slow"
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from forge.executor.executor import RunbookExecutor
from forge.executor.registry import StepOutcome, StepTypeRegistry
from forge.persistence.repositories.runbook import RunbookRepository
from forge.persistence.repositories.runbook_models import Runbook, Step, StepStatus
from forge.adapters.nats.runbook_publisher import RunbookPublisher
from nats_core.envelope import MessageEnvelope, EventType
from nats_core.events import (
    RunbookStartedPayload,
    StepStartedPayload,
    StepResultPayload,
    RunbookCompletePayload,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def nats_url() -> str:
    """NATS server URL from environment or default localhost.

    Tests skip if NATS server is not available.
    """
    return os.getenv("NATS_URL", "nats://localhost:4222")


@pytest.fixture
async def nats_client(nats_url: str):
    """Real NATS client connection.

    Skips the test if connection fails (broker not available).
    """
    try:
        import nats
    except ImportError:
        pytest.skip("nats-py not installed")

    try:
        nc = await nats.connect(nats_url, connect_timeout=2)
    except Exception as e:
        pytest.skip(f"NATS broker not available at {nats_url}: {e}")

    yield nc

    await nc.close()


@pytest.fixture
def db_path_integration(tmp_path: Path) -> Path:
    """Create a temporary database file for integration tests."""
    return tmp_path / "test_integration.db"


def create_integration_runbook(
    db_path: Path,
    runbook_id: str,
    step_types: list[str],
    correlation_id: str = "integration-test",
) -> RunbookRepository:
    """Create and persist a test runbook, return the repository."""
    from forge.persistence.migrations.runbook import apply

    conn = sqlite3.connect(str(db_path))
    apply(conn)

    repository = RunbookRepository(connection=conn)

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
        target="integration-target",
        current_step_index=0,
        status=StepStatus.pending,
        created_at=datetime.now(timezone.utc),
        steps=tuple(steps),
    )

    repository.create_runbook(runbook, correlation_id=correlation_id)
    return repository


# ---------------------------------------------------------------------------
# AC-003: Real-broker scenario is tagged @integration @slow and excluded by default
# AC-004: Subscriber observes full lifecycle in order over the wire
# AC-005: No unknown-mark warnings
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.slow
async def test_lifecycle_events_published_to_real_broker(
    nats_client,
    db_path_integration: Path,
) -> None:
    """Lifecycle events are published to a real NATS broker and observed by a subscriber.

    AC-003: Tagged @integration @slow, excluded from default pytest run.
    AC-004: Subscriber observes events in order:
      runbook-started → step-started/step-result (per step) → runbook-complete
    AC-005: No unknown-mark warnings (marks registered in pyproject.toml).

    This test runs a 2-step runbook and subscribes to the runbook lifecycle
    events on a real NATS broker, verifying the full event sequence.
    """
    runbook_id = "rb-integration-001"
    correlation_id = "corr-integration-001"

    # Create a 2-step runbook
    repository = create_integration_runbook(
        db_path_integration,
        runbook_id=runbook_id,
        step_types=["integration-step-1", "integration-step-2"],
        correlation_id=correlation_id,
    )

    # Create handlers
    registry = StepTypeRegistry()

    def handler_step_1(step: Step) -> StepOutcome:
        return StepOutcome(
            status=StepStatus.passed,
            result={"message": "Step 1 completed"},
        )

    def handler_step_2(step: Step) -> StepOutcome:
        return StepOutcome(
            status=StepStatus.passed,
            result={"message": "Step 2 completed"},
        )

    registry.register("integration-step-1", handler_step_1)
    registry.register("integration-step-2", handler_step_2)

    # Create real publisher
    publisher = RunbookPublisher(nats_client=nats_client)

    # Create executor
    executor = RunbookExecutor(
        repository=repository,
        registry=registry,
        publisher=publisher,
    )

    # Subscribe to all runbook lifecycle events for this runbook
    received_events: list[tuple[str, dict[str, Any]]] = []

    async def event_handler(msg):
        """Capture each event in order."""
        import json

        try:
            envelope = MessageEnvelope.model_validate_json(msg.data)
            # Record (event_type, payload_dict) for assertion
            received_events.append((envelope.event_type.value, envelope.payload))
        except Exception as e:
            # Log but don't fail the subscriber
            print(f"Failed to parse event: {e}")

    # Subscribe to the runbook.* wildcard for this runbook
    subject = f"runbook.*.{runbook_id}"
    subscription = await nats_client.subscribe(subject, cb=event_handler)

    # Wait briefly to ensure subscription is active
    await asyncio.sleep(0.1)

    # Execute the runbook
    result = await executor.run(runbook_id, correlation_id=correlation_id)

    # Wait for events to be delivered
    await asyncio.sleep(0.2)

    # Unsubscribe
    await subscription.unsubscribe()

    # Verify execution completed
    assert result.status == "complete"

    # Verify we received all expected events in order
    assert len(received_events) >= 5, (
        f"Expected at least 5 events (started + 2×(step-started+step-result) + complete), "
        f"got {len(received_events)}: {[e[0] for e in received_events]}"
    )

    # Extract event types in order
    event_types = [event[0] for event in received_events]

    # Verify order:
    # 1. runbook-started
    assert event_types[0] == EventType.RUNBOOK_STARTED.value, (
        f"First event should be runbook-started, got {event_types[0]}"
    )

    # 2. step-started for step 0
    assert event_types[1] == EventType.STEP_STARTED.value
    assert received_events[1][1]["sequence_index"] == 0

    # 3. step-result for step 0
    assert event_types[2] == EventType.STEP_RESULT.value
    assert received_events[2][1]["sequence_index"] == 0
    assert received_events[2][1]["status"] == "passed"

    # 4. step-started for step 1
    assert event_types[3] == EventType.STEP_STARTED.value
    assert received_events[3][1]["sequence_index"] == 1

    # 5. step-result for step 1
    assert event_types[4] == EventType.STEP_RESULT.value
    assert received_events[4][1]["sequence_index"] == 1
    assert received_events[4][1]["status"] == "passed"

    # 6. runbook-complete
    assert event_types[5] == EventType.RUNBOOK_COMPLETE.value, (
        f"Last event should be runbook-complete, got {event_types[5]}"
    )

    # Verify all events have the correct runbook_id
    for event_type, payload in received_events:
        assert payload.get("runbook_id") == runbook_id, (
            f"Event {event_type} has wrong runbook_id: {payload.get('runbook_id')}"
        )


@pytest.mark.integration
@pytest.mark.slow
async def test_subscriber_observes_events_on_wire(
    nats_client,
    db_path_integration: Path,
) -> None:
    """A second integration test verifying event observation on the wire.

    Uses a simpler approach: just count events and verify minimum expectations.
    This provides additional coverage for the real-broker path.
    """
    runbook_id = "rb-integration-002"
    correlation_id = "corr-integration-002"

    # Create a 3-step runbook
    repository = create_integration_runbook(
        db_path_integration,
        runbook_id=runbook_id,
        step_types=["step-a", "step-b", "step-c"],
        correlation_id=correlation_id,
    )

    registry = StepTypeRegistry()

    def simple_handler(step: Step) -> StepOutcome:
        return StepOutcome(status=StepStatus.passed, result={})

    registry.register("step-a", simple_handler)
    registry.register("step-b", simple_handler)
    registry.register("step-c", simple_handler)

    publisher = RunbookPublisher(nats_client=nats_client)
    executor = RunbookExecutor(
        repository=repository,
        registry=registry,
        publisher=publisher,
    )

    # Count events by type
    event_counts: dict[str, int] = {}

    async def counting_handler(msg):
        """Count events by type."""
        import json

        try:
            envelope = MessageEnvelope.model_validate_json(msg.data)
            event_type = envelope.event_type.value
            event_counts[event_type] = event_counts.get(event_type, 0) + 1
        except Exception:
            pass

    subject = f"runbook.*.{runbook_id}"
    subscription = await nats_client.subscribe(subject, cb=counting_handler)

    await asyncio.sleep(0.1)

    # Execute
    result = await executor.run(runbook_id, correlation_id=correlation_id)

    await asyncio.sleep(0.2)
    await subscription.unsubscribe()

    assert result.status == "complete"

    # Verify we got:
    # - 1 runbook-started
    # - 3 step-started (one per step)
    # - 3 step-result (one per step)
    # - 1 runbook-complete
    assert event_counts.get(EventType.RUNBOOK_STARTED.value, 0) == 1
    assert event_counts.get(EventType.STEP_STARTED.value, 0) == 3
    assert event_counts.get(EventType.STEP_RESULT.value, 0) == 3
    assert event_counts.get(EventType.RUNBOOK_COMPLETE.value, 0) == 1

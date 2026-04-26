---
complexity: 5
consumer_context:
- consumes: pipeline_history_entity_id_contract
  driver: graphiti-core
  format_note: entity_id MUST equal the SQLite-row UUID for GateDecision/CapabilityResolution/OverrideEvent/CalibrationAdjustment/SessionOutcome
    — never generated at write time. CalibrationEvent uses deterministic hash.
  framework: Graphiti async client (mcp__graphiti__add_memory or graphiti-core)
  task: TASK-IC-001
created: 2026-04-25 14:36:00+00:00
dependencies:
- TASK-IC-001
estimated_minutes: 120
feature_id: FEAT-FORGE-006
id: TASK-IC-002
implementation_mode: task-work
parent_review: TASK-REV-IC8B
priority: high
status: design_approved
tags:
- memory
- graphiti
- async
- failure-tolerance
task_type: feature
title: Fire-and-forget Graphiti write wrapper
updated: 2026-04-25 14:36:00+00:00
wave: 2
---

# Task: Fire-and-forget Graphiti write wrapper

## Description

Implement the async write function that takes a typed entity (from TASK-IC-001),
applies `redact_credentials()` to all text fields, and writes to Graphiti's
`forge_pipeline_history` or `forge_calibration_history` group via the MCP-first
pattern with CLI fallback (mirror the 3-tier pattern in
`docs/internals/commands-lib/graphiti-preamble.md`). The write MUST be dispatched
fire-and-forget so failures do not block the pipeline.

This implements the lesson from prior incidents (recorded in Graphiti
`guardkit__task_outcomes`): "post-acceptance write failures cause wasted LLM
token spend, as correct work is thrown away". Mirror the pattern from
`_write_to_graphiti()` in `run_greenfield()` (success-path only).

## Module: `forge/memory/writer.py`

Two public functions:

```python
async def write_entity(entity: PipelineHistoryEntity, group_id: str) -> None:
    """Synchronously redact + write an entity. Raises on failure.
    Used by reconcile-backfill (TASK-IC-004) where the caller wants to know
    whether the write succeeded."""

def fire_and_forget_write(entity: PipelineHistoryEntity, group_id: str) -> None:
    """Schedule write_entity() as an asyncio task. Errors are caught and
    logged via structured logging — never raise to the caller. The pipeline
    proceeds immediately. Used by all stage-completion hooks (TASK-IC-003)."""
```

## Acceptance Criteria

- [ ] `write_entity()` redacts every text field via `redact_credentials()` before write
- [ ] `write_entity()` chooses MCP vs CLI based on availability (mirrors
      `graphiti-preamble.md` 3-tier pattern)
- [ ] `fire_and_forget_write()` dispatches the coroutine and returns synchronously
- [ ] Failures in fire-and-forget are caught, logged with full entity_id +
      group_id + error, and never raised
- [ ] Structured log line includes `entity_id`, `group_id`, `entity_type`,
      `error_class`, `error_message` for downstream alerting
- [ ] Covers `@negative memory-write-failure-tolerated`: simulated Graphiti
      outage does NOT abort the pipeline
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Test Requirements

- [ ] `tests/unit/test_writer.py` — mocked Graphiti client; assert redaction
      called, assert MCP vs CLI selection, assert exception handling
- [ ] `tests/unit/test_fire_and_forget.py` — assert no exception propagates
      to caller when underlying write raises; assert structured log emitted
- [ ] `tests/integration/test_writer_with_graphiti.py` — opt-in (skipped if
      Graphiti unreachable); writes a test entity, reads it back

## Seam Tests

The following seam test validates the integration contract with the producer task. Implement this test to verify the boundary before integration.

```python
"""Seam test: verify pipeline_history_entity_id_contract from TASK-IC-001."""
import pytest
from uuid import UUID


@pytest.mark.seam
@pytest.mark.integration_contract("pipeline_history_entity_id_contract")
def test_pipeline_history_entity_id_format():
    """Verify pipeline_history entity_id matches the contract.

    Contract: entity_id MUST equal the SQLite-row UUID for the five typed
    pipeline-history entities; CalibrationEvent uses a deterministic hash.
    Producer: TASK-IC-001
    """
    from forge.memory.models import (
        GateDecision, CapabilityResolution, OverrideEvent,
        CalibrationAdjustment, SessionOutcome, CalibrationEvent,
    )
    from uuid import uuid4

    sqlite_uuid = uuid4()
    g = GateDecision(entity_id=sqlite_uuid, stage_name="...", decided_at="...",
                     score=0.9, criterion_breakdown={}, rationale="...")
    assert g.entity_id == sqlite_uuid, \
        "GateDecision.entity_id must equal the SQLite row UUID, not be regenerated"
    assert isinstance(g.entity_id, UUID), \
        "Pipeline-history entity_ids are typed UUID, not str"

    # CalibrationEvent uses a deterministic str hash, NOT a UUID.
    cal = CalibrationEvent(entity_id="sha256:abc...", source_file="...",
                           question="...", answer="...", captured_at="...",
                           partial=False)
    assert isinstance(cal.entity_id, str), \
        "CalibrationEvent.entity_id is a deterministic hash str, not a UUID"
```

## Implementation Notes

- Use `asyncio.ensure_future()` or `asyncio.create_task()` for fire-and-forget;
  ensure the running event loop is the pipeline's loop.
- For environments where the pipeline is sync, expose a thread-pool variant
  that runs the coroutine in a background thread.
- Do NOT swallow exceptions silently — always log with `logger.exception()`
  (which captures the traceback). Silent swallow is the failure mode the
  Graphiti `task_outcomes` lesson warns against.
- Coordinate with the planned "async Graphiti write" shared library (per
  Graphiti `project_decisions` group): if it lands first, this module should
  delegate to it via the same `fire_and_forget_write()` interface. Stub the
  internal call behind a thin abstraction so the swap is mechanical.
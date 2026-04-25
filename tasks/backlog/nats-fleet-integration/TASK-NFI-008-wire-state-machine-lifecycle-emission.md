---
id: TASK-NFI-008
title: Wire state-machine transitions to pipeline_publisher (lifecycle emission points)
task_type: feature
status: in_review
priority: high
created: 2026-04-24 00:00:00+00:00
updated: 2026-04-24 00:00:00+00:00
parent_review: TASK-REV-NF20
feature_id: FEAT-FORGE-002
wave: 4
implementation_mode: task-work
complexity: 5
dependencies:
- TASK-NFI-006
tags:
- integration
- state-machine
- pipeline
- lifecycle
consumer_context:
- task: TASK-NFI-006
  consumes: PipelinePublisher
  framework: Python class with async methods
  driver: forge.adapters.nats.pipeline_publisher
  format_note: Eight async methods (publish_build_started, publish_build_progress,
    publish_stage_complete, publish_build_paused, publish_build_resumed, publish_build_complete,
    publish_build_failed, publish_build_cancelled); each takes a typed payload from
    nats_core.events.pipeline; caller is responsible for building the payload with
    correct correlation_id threading
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002
  base_branch: main
  started_at: '2026-04-25T14:19:57.083875'
  last_updated: '2026-04-25T14:36:11.574396'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-25T14:19:57.083875'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# Task: Wire state-machine transitions to pipeline_publisher

## Description

Hook `PipelinePublisher` calls into the FEAT-FORGE-001 state machine at
every lifecycle transition point. Every published event shares the
originating `correlation_id` from the triggering `BuildQueuedPayload`
(edge-case Group D).

Emission points (mapped from `API-nats-pipeline-events.md §3.1`):

| Transition | Event to publish |
|---|---|
| `PREPARING → RUNNING` | `publish_build_started` |
| `RUNNING` every N secs + wave boundary | `publish_build_progress` |
| After each gated stage commits `StageLogEntry` | `publish_stage_complete` |
| `RUNNING → PAUSED` (FLAG_FOR_REVIEW / HARD_STOP / MANDATORY_HUMAN_APPROVAL) | `publish_build_paused` |
| `PAUSED → RUNNING` (approval) | `publish_build_resumed` |
| `FINALISING → COMPLETE` | `publish_build_complete` |
| Any terminal failure | `publish_build_failed` |
| `forge cancel` reaches running build | `publish_build_cancelled` |

Crucially:
- **Write SQLite row THEN publish** — never roll back SQLite if publish fails
- **Publish BEFORE interrupt()** for `build-paused` (ordering guarantee per scenario Group D "Flagging a stage for human review publishes a build-paused event")

## Acceptance Criteria

- [ ] Every state-machine transition listed above triggers exactly one publish call
- [ ] `correlation_id` threaded from `BuildQueuedPayload` through every downstream event (Group D @edge-case scenario)
- [ ] `publish_build_paused` publishes BEFORE `interrupt()` fires
- [ ] A publish raising `PublishFailure` is logged but does NOT revert the SQLite row — state remains as written (Group E @data-integrity)
- [ ] Progress publisher fires at least every `PipelineConfig.progress_interval_seconds` during RUNNING (uses injected Clock; test uses FakeClock to advance)
- [ ] Progress publisher also fires on wave boundaries from autobuild_runner (hook point documented in code)
- [ ] Unit tests mock `PipelinePublisher`; assert method called with correct payload shape
- [ ] Scenario test "All lifecycle events share originating correlation identifier" passes (Group D @edge-case)
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Seam Tests

```python
"""Seam test: verify PipelinePublisher method surface from TASK-NFI-006."""
import pytest
from forge.adapters.nats.pipeline_publisher import PipelinePublisher


@pytest.mark.seam
@pytest.mark.integration_contract("PipelinePublisher")
def test_pipeline_publisher_surface():
    """Verify all eight lifecycle publisher methods exist.

    Contract: Eight async methods for the lifecycle events.
    Producer: TASK-NFI-006
    """
    expected = [
        "publish_build_started",
        "publish_build_progress",
        "publish_stage_complete",
        "publish_build_paused",
        "publish_build_resumed",
        "publish_build_complete",
        "publish_build_failed",
        "publish_build_cancelled",
    ]
    for method in expected:
        assert hasattr(PipelinePublisher, method), f"Missing method: {method}"
```

## Implementation Notes

- The state machine from FEAT-FORGE-001 exposes transition hooks (e.g. `on_transition(from_state, to_state, build)`); this task wires `PipelinePublisher` into those hooks
- If FEAT-FORGE-001 has not yet added hooks, add them here as part of this task and document in the PR
- Progress cadence uses an `asyncio.Task` started on transition to RUNNING, cancelled on leaving RUNNING

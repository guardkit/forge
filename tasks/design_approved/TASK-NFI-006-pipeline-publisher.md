---
autobuild_state:
  base_branch: main
  current_turn: 3
  last_updated: '2026-04-25T12:50:51.104638'
  max_turns: 30
  started_at: '2026-04-25T12:25:32.063274'
  turns:
  - coach_success: true
    decision: feedback
    feedback: '- Task-work produced a report with 2 of 3 required agent invocations.
      Missing phases: 3 (Implementation). Invoke these agents via the Task tool before
      re-emitting the report:

      - Phase 3: `python-api-specialist` (Implementation)'
    player_success: true
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    timestamp: '2026-04-25T12:25:32.063274'
    turn: 1
  - coach_success: true
    decision: feedback
    feedback: '- Task-work produced a report with 2 of 3 required agent invocations.
      Missing phases: 3 (Implementation). Invoke these agents via the Task tool before
      re-emitting the report:

      - Phase 3: `python-api-specialist` (Implementation)'
    player_success: true
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    timestamp: '2026-04-25T12:36:44.630290'
    turn: 2
  - coach_success: true
    decision: feedback
    feedback: '- Task-work produced a report with 2 of 3 required agent invocations.
      Missing phases: 3 (Implementation). Invoke these agents via the Task tool before
      re-emitting the report:

      - Phase 3: `python-api-specialist` (Implementation)'
    player_success: true
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    timestamp: '2026-04-25T12:42:26.384594'
    turn: 3
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002
complexity: 5
consumer_context:
- consumes: ForgeConfig.pipeline
  driver: pyyaml + pydantic
  format_note: PipelineConfig.progress_interval_seconds (int, default 60) drives progress-publish
    cadence; used by state-machine caller, not this module, but read here for subject-name
    building
  framework: Pydantic v2 BaseModel
  task: TASK-NFI-001
created: 2026-04-24 00:00:00+00:00
dependencies:
- TASK-NFI-001
feature_id: FEAT-FORGE-002
id: TASK-NFI-006
implementation_mode: task-work
parent_review: TASK-REV-NF20
priority: high
status: design_approved
tags:
- nats
- adapter
- publisher
- pipeline
- lifecycle-events
task_type: feature
test_results:
  coverage: null
  last_run: null
  status: pending
title: Implement pipeline_publisher (8 lifecycle publisher methods)
updated: 2026-04-24 00:00:00+00:00
wave: 3
---

# Task: Implement pipeline_publisher (8 lifecycle publisher methods)

## Description

Create `src/forge/adapters/nats/pipeline_publisher.py` owning the outbound
lifecycle event stream described in
`API-nats-pipeline-events.md §3`. Eight publisher methods, one per subject:

- `publish_build_started(payload: BuildStartedPayload)`
- `publish_build_progress(payload: BuildProgressPayload)`
- `publish_stage_complete(payload: StageCompletePayload)`
- `publish_build_paused(payload: BuildPausedPayload)`
- `publish_build_resumed(payload: BuildResumedPayload)`
- `publish_build_complete(payload: BuildCompletePayload)`
- `publish_build_failed(payload: BuildFailedPayload)`
- `publish_build_cancelled(payload: BuildCancelledPayload)`

All payloads imported from `nats_core.events.pipeline`. Every published
envelope carries `source_id="forge"` and the payload's `correlation_id`.

## Acceptance Criteria

- [ ] Eight methods exist on a `PipelinePublisher` class
- [ ] Each method publishes to the correct subject pattern `pipeline.{event}.{feature_id}` (e.g. `pipeline.build-started.FEAT-A1B2`)
- [ ] Every envelope has `source_id == "forge"` and `correlation_id == payload.correlation_id`
- [ ] Publish is fire-and-forget — PubAck is logged but not treated as delivery proof (LES1 parity rule)
- [ ] Transport-level publish failures raise a documented `PublishFailure` exception; callers catch + log but never roll back SQLite state
- [ ] Unit tests: one per method, assert subject pattern + envelope shape + correlation_id threading
- [ ] Concurrency test: 100 concurrent `publish_build_progress` calls on the same publisher do not interleave partial envelopes
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Seam Tests

```python
"""Seam test: verify ForgeConfig.pipeline contract from TASK-NFI-001."""
import pytest
from forge.config.models import PipelineConfig


@pytest.mark.seam
@pytest.mark.integration_contract("ForgeConfig.pipeline")
def test_forge_config_pipeline_format():
    """Verify PipelineConfig matches the expected format.

    Contract: progress_interval_seconds (int, default 60),
              build_queue_subject (str)
    Producer: TASK-NFI-001
    """
    cfg = PipelineConfig()
    assert cfg.progress_interval_seconds == 60
    assert cfg.build_queue_subject == "pipeline.build-queued.>"
```

## Implementation Notes

- `PipelinePublisher.__init__` takes `nats_client` (injected at app boundary)
- Subject-building helpers: `_subject_for(event_name, feature_id)` returns the canonical subject
- Use `MessageEnvelope` from `nats_core.envelope` for envelope construction
- Envelope `event_type` field: follow `API-nats-pipeline-events.md §3.2` naming
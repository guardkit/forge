---
id: TASK-NFI-006
title: Implement pipeline_publisher (8 lifecycle publisher methods)
task_type: feature
status: in_review
priority: high
created: 2026-04-24 00:00:00+00:00
updated: 2026-04-24 00:00:00+00:00
parent_review: TASK-REV-NF20
feature_id: FEAT-FORGE-002
wave: 3
implementation_mode: task-work
complexity: 5
dependencies:
- TASK-NFI-001
tags:
- nats
- adapter
- publisher
- pipeline
- lifecycle-events
consumer_context:
- task: TASK-NFI-001
  consumes: ForgeConfig.pipeline
  framework: Pydantic v2 BaseModel
  driver: pyyaml + pydantic
  format_note: PipelineConfig.progress_interval_seconds (int, default 60) drives progress-publish
    cadence; used by state-machine caller, not this module, but read here for subject-name
    building
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 2
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002
  base_branch: main
  started_at: '2026-04-25T14:02:53.110340'
  last_updated: '2026-04-25T14:19:57.008736'
  turns:
  - turn: 1
    decision: feedback
    feedback: "- Advisory (non-blocking): task-work produced a report with 2 of 3\
      \ expected agent invocations. Missing phases: 3 (Implementation). Consider invoking\
      \ these agents via the Task tool to strengthen stack-specific quality:\n- Phase\
      \ 3: `python-api-specialist` (Implementation)\n- Not all acceptance criteria\
      \ met:\n  \u2022 All modified files pass project-configured lint/format checks\
      \ with zero errors"
    timestamp: '2026-04-25T14:02:53.110340'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
  - turn: 2
    decision: approve
    feedback: null
    timestamp: '2026-04-25T14:13:17.939347'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
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

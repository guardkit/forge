---
autobuild_state:
  base_branch: main
  current_turn: 3
  last_updated: '2026-04-25T12:55:56.948769'
  max_turns: 30
  started_at: '2026-04-25T12:25:32.060135'
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
    timestamp: '2026-04-25T12:25:32.060135'
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
    timestamp: '2026-04-25T12:40:06.921613'
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
    timestamp: '2026-04-25T12:47:39.868486'
    turn: 3
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002
complexity: 6
consumer_context:
- consumes: ForgeConfig.permissions.filesystem.allowlist
  driver: pyyaml + pydantic
  format_note: FilesystemPermissions.allowlist is list[Path] of absolute paths; every
    incoming BuildQueuedPayload.feature_yaml_path MUST resolve inside one of these
    paths (using pathlib.Path.is_relative_to). Relative paths are rejected at config
    load time (TASK-NFI-001 validator).
  framework: Pydantic v2 BaseModel
  task: TASK-NFI-001
- consumes: ForgeConfig.pipeline
  driver: pyyaml + pydantic
  format_note: 'PipelineConfig.approved_originators: list[str] — originating_adapter
    in BuildQueuedPayload MUST be in this list; build-failed event published if not'
  framework: Pydantic v2 BaseModel
  task: TASK-NFI-001
created: 2026-04-24 00:00:00+00:00
dependencies:
- TASK-NFI-001
feature_id: FEAT-FORGE-002
id: TASK-NFI-007
implementation_mode: task-work
parent_review: TASK-REV-NF20
priority: high
status: design_approved
tags:
- nats
- adapter
- consumer
- jetstream
- pull-consumer
- pipeline
- security
task_type: feature
test_results:
  coverage: null
  last_run: null
  status: pending
title: Implement pipeline_consumer (pull consumer + validation + allowlist)
updated: 2026-04-24 00:00:00+00:00
wave: 3
---

# Task: Implement pipeline_consumer (pull consumer + validation + allowlist)

## Description

Create `src/forge/adapters/nats/pipeline_consumer.py` owning the inbound
build-queue subscription described in `API-nats-pipeline-events.md §2`.

Responsibilities:

- Durable pull consumer with `max_ack_pending=1`, `durable="forge-consumer"`,
  `ack_wait=1h`, `DeliverPolicy.ALL`, `AckPolicy.EXPLICIT`, `max_deliver=-1`
- Validate `BuildQueuedPayload` on receive — malformed → ack + publish
  `build-failed` with `failure_reason="malformed BuildQueuedPayload"`
- Duplicate detection — check `builds(feature_id, correlation_id)` unique index;
  if present, ack + skip (idempotent)
- Path allowlist check — `feature_yaml_path` must resolve inside
  `ForgeConfig.permissions.filesystem.allowlist`; otherwise ack + publish
  `build-failed` with `failure_reason="path outside allowlist"`
- Originator allowlist — `originating_adapter` must be in
  `PipelineConfig.approved_originators`; otherwise ack + publish `build-failed`
- **Terminal-only ack** — on accepted builds, ack is deferred until the state
  machine reaches a terminal state (COMPLETE/FAILED/CANCELLED/SKIPPED); hand
  back an `ack_callback` the state machine invokes at the terminal transition

## Acceptance Criteria

- [ ] Pull consumer config matches `API-nats-pipeline-events.md §2.2` exactly
- [ ] Valid `BuildQueuedPayload` → pass to state-machine entrypoint with `ack_callback` bound; ack deferred until terminal
- [ ] Malformed payload → ack + `build-failed` published; never reaches state machine
- [ ] Path outside allowlist → ack + `build-failed`; never reaches state machine
- [ ] Unrecognised `originating_adapter` → ack + `build-failed` with `failure_reason="originator not recognised"`
- [ ] Duplicate `(feature_id, correlation_id)` already-complete build → ack + skip (no new build started)
- [ ] Duplicate `(feature_id, correlation_id)` already-terminal-failed build → ack + skip
- [ ] Ack is called exactly once per message — asserted by mock `Msg.ack` call count
- [ ] Non-terminal transitions do NOT ack — `ack_callback` is only invoked on terminal
- [ ] Allowlist check uses `pathlib.Path.resolve()` + `is_relative_to` to reject `..` traversal
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Seam Tests

```python
"""Seam test: verify ForgeConfig filesystem allowlist contract from TASK-NFI-001."""
import pytest
from pathlib import Path
from forge.config.models import FilesystemPermissions


@pytest.mark.seam
@pytest.mark.integration_contract("ForgeConfig.permissions.filesystem.allowlist")
def test_filesystem_allowlist_format():
    """Verify allowlist is list[Path] of absolute paths.

    Contract: FilesystemPermissions.allowlist: list[Path], absolute paths only
    Producer: TASK-NFI-001
    """
    perms = FilesystemPermissions(allowlist=[Path("/var/forge/repos")])
    assert all(p.is_absolute() for p in perms.allowlist)
    # Relative paths must be rejected at construction
    with pytest.raises(ValueError):
        FilesystemPermissions(allowlist=[Path("relative/path")])
```

```python
"""Seam test: verify ForgeConfig.pipeline approved_originators from TASK-NFI-001."""
import pytest
from forge.config.models import PipelineConfig


@pytest.mark.seam
@pytest.mark.integration_contract("ForgeConfig.pipeline")
def test_approved_originators_format():
    """Verify approved_originators is a list of strings with default values.

    Contract: PipelineConfig.approved_originators: list[str]
    Producer: TASK-NFI-001
    """
    cfg = PipelineConfig()
    assert isinstance(cfg.approved_originators, list)
    assert "terminal" in cfg.approved_originators
    assert "slack" in cfg.approved_originators
```

## Implementation Notes

- Depends on FEAT-FORGE-001 providing `builds` SQLite table with `uq_builds_feature_correlation` unique index and a read helper; gate task start on that existing
- `ack_callback` is a closure bound to the JetStream `Msg.ack` method; passed by reference through the state machine so it survives long-lived builds
- Crash recovery (`reconcile_on_boot`) is a separate task (TASK-NFI-009)
- Do NOT catch `asyncio.CancelledError` — let shutdown propagate
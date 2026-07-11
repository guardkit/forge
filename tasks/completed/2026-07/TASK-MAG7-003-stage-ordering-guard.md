---
id: TASK-MAG7-003
title: Implement StageOrderingGuard
task_type: feature
status: completed
priority: high
created: 2026-04-25 00:00:00+00:00
updated: 2026-04-25 00:00:00+00:00
parent_review: TASK-REV-MAG7
feature_id: FEAT-FORGE-007
wave: 2
implementation_mode: task-work
complexity: 5
dependencies:
- TASK-MAG7-001
tags:
- guard
- stage-ordering
- invariant
- feat-forge-007
consumer_context:
- task: TASK-MAG7-001
  consumes: stage_taxonomy
  framework: Python forge.pipeline.stage_taxonomy
  driver: StrEnum
  format_note: Imports StageClass and STAGE_PREREQUISITES from forge.pipeline.stage_taxonomy
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-CBDE
  base_branch: main
  started_at: '2026-04-26T18:40:21.964524'
  last_updated: '2026-04-26T18:49:52.746013'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-26T18:40:21.964524'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
reconciled: "2026-07-11 WS3-S8 sweep — TASK-MAG7-003: feature-rollup completed"
---

# Task: Implement StageOrderingGuard

## Description

Pure-function guard that, given a `build_id`, returns the set of stage classes
the supervisor is permitted to dispatch on the next reasoning turn. Refuses
any stage whose prerequisites are not all recorded as approved in `stage_log`.

Covers FEAT-FORGE-007 Group B Scenario Outline directly (seven prerequisite
rows) and is the executor-layer half of the stage-ordering invariant — the
reasoning model cannot bypass it via prompt drift.

## Acceptance Criteria

- [ ] `StageOrderingGuard` class exists at
      `src/forge/pipeline/stage_ordering_guard.py`
- [ ] Method `next_dispatchable(build_id: str, stage_log_reader: StageLogReader) -> set[StageClass]`
      returns the set of stages whose prerequisites are all approved
- [ ] Method `is_dispatchable(build_id, stage: StageClass, feature_id: str | None = None) -> bool`
      returns True only if every prerequisite of `stage` for that feature
      (or build, for non-per-feature stages) is approved in `stage_log`
- [ ] Per-feature stages (`FEATURE_SPEC`, `FEATURE_PLAN`, `AUTOBUILD`,
      `PULL_REQUEST_REVIEW`) take a `feature_id` argument and check
      prerequisites scoped to that feature
- [ ] `PULL_REQUEST_REVIEW` requires `AUTOBUILD` approved for *every* feature
      in the catalogue (per Scenario Outline row 7)
- [ ] Unit tests cover all seven prerequisite rows from the Scenario Outline
      verbatim
- [ ] Unit tests cover the multi-feature case (PR review requires all autobuilds)
- [ ] Pure function — no I/O except via the injected `stage_log_reader` Protocol
- [ ] All modified files pass project-configured lint/format checks with
      zero errors

## Implementation Notes

Inject a `StageLogReader` Protocol so tests can supply an in-memory fake
without bringing up SQLite. The production reader is provided by
FEAT-FORGE-001's SQLite adapter.

The pure-function shape (no async, no I/O behind the Protocol) is what makes
this guard the executor-layer enforcer of the stage-ordering invariant
(ADR-ARCH-026 belt-and-braces). If the reasoning model picks a stage whose
prerequisites are not approved, `is_dispatchable` returns False and the
supervisor refuses to act.

## Seam Tests

The following seam test validates the integration contract with the producer task. Implement this test to verify the boundary before integration.

```python
"""Seam test: verify stage_taxonomy contract from TASK-MAG7-001."""
import pytest


@pytest.mark.seam
@pytest.mark.integration_contract("stage_taxonomy")
def test_stage_taxonomy_contract():
    """Verify StageClass enum and STAGE_PREREQUISITES match contract.

    Contract: 8 stages, 7 prerequisite rows matching Scenario Outline.
    Producer: TASK-MAG7-001
    """
    from forge.pipeline.stage_taxonomy import StageClass, STAGE_PREREQUISITES

    assert len(StageClass) == 8, "Must have exactly 8 stage classes"
    assert len(STAGE_PREREQUISITES) == 7, "Must have exactly 7 prerequisite rows"
    assert set(STAGE_PREREQUISITES.keys()) == {
        StageClass.ARCHITECT,
        StageClass.SYSTEM_ARCH,
        StageClass.SYSTEM_DESIGN,
        StageClass.FEATURE_SPEC,
        StageClass.FEATURE_PLAN,
        StageClass.AUTOBUILD,
        StageClass.PULL_REQUEST_REVIEW,
    }
```

## Test Execution Log

[Automatically populated by /task-work]

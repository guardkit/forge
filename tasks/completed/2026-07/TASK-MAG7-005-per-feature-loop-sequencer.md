---
id: TASK-MAG7-005
title: Implement PerFeatureLoopSequencer
task_type: feature
status: completed
priority: high
created: 2026-04-25 00:00:00+00:00
updated: 2026-04-25 00:00:00+00:00
parent_review: TASK-REV-MAG7
feature_id: FEAT-FORGE-007
wave: 2
implementation_mode: task-work
complexity: 4
dependencies:
- TASK-MAG7-001
tags:
- sequencer
- per-feature
- autobuild
- feat-forge-007
consumer_context:
- task: TASK-MAG7-001
  consumes: stage_taxonomy
  framework: Python forge.pipeline.stage_taxonomy
  driver: StrEnum
  format_note: Imports StageClass and PER_FEATURE_STAGES from forge.pipeline.stage_taxonomy
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-CBDE
  base_branch: main
  started_at: '2026-04-26T18:40:21.955931'
  last_updated: '2026-04-26T18:50:33.801333'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-26T18:40:21.955931'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
reconciled: "2026-07-11 WS3-S8 sweep — TASK-MAG7-005: feature-rollup completed"
---

# Task: Implement PerFeatureLoopSequencer

## Description

Pure-function sequencer that refuses to permit a second feature's autobuild
dispatch while any earlier feature's autobuild is still in a non-terminal
lifecycle on the same build. Implements FEAT-FORGE-007 ASSUM-006 (per-feature
autobuild sequencing within a build) — required because each Build has a
single worktree_path and concurrent autobuilds in the same worktree would
create branch contention.

Covers Group D edge-case scenario: "Per-feature inner loops are sequenced so
each feature's autobuild completes before the next feature's autobuild begins".

## Acceptance Criteria

- [ ] `PerFeatureLoopSequencer` class exists at
      `src/forge/pipeline/per_feature_sequencer.py`
- [ ] Method `may_start_autobuild(build_id: str, feature_id: str, stage_log_reader, async_task_reader) -> bool`
      returns False if any prior feature's autobuild is in a non-terminal
      lifecycle (`starting`, `planning_waves`, `running_wave`,
      `awaiting_approval`, `pushing_pr`)
- [ ] Method consults both `stage_log` (terminal completion) and the
      `async_tasks` state channel via `async_task_reader` (live lifecycle,
      DDR-006 `AutobuildState`)
- [ ] Returns True when no autobuild for any earlier feature is non-terminal
- [ ] Unit test: two-feature catalogue, second autobuild dispatch blocked
      while first autobuild lifecycle is `running_wave`
- [ ] Unit test: second autobuild dispatch permitted once first reaches
      `completed`
- [ ] Pure function — no I/O except via injected reader Protocols
- [ ] All modified files pass project-configured lint/format checks with
      zero errors

## Implementation Notes

This sequencer is consulted by the supervisor's reasoning loop (TASK-MAG7-010)
just before it would dispatch autobuild for the next feature. The
`StageOrderingGuard` already enforces that the feature's plan is approved;
this sequencer adds the additional cross-feature constraint that no other
autobuild for this build is still in flight.

Inter-feature parallelism is explicitly out of scope for Mode A v1
(FEAT-FORGE-007 ASSUM-006 confirmed). Concurrent *builds* (different
build_ids) remain unaffected — Group F concurrency scenarios still hold.

## Test Execution Log

[Automatically populated by /task-work]

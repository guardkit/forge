---
id: TASK-MBC8-014
title: Crash-recovery integration tests for Mode B and Mode C non-terminal stages
task_type: testing
status: completed
priority: high
created: 2026-04-27 00:00:00+00:00
updated: 2026-04-27 00:00:00+00:00
parent_review: TASK-REV-MBC8
feature_id: FEAT-FORGE-008
wave: 7
implementation_mode: task-work
complexity: 6
dependencies:
- TASK-MBC8-010
- TASK-MBC8-011
tags:
- crash-recovery
- integration
- mode-b
- mode-c
- feat-forge-008
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-008
  base_branch: main
  started_at: '2026-04-27T19:59:54.068665'
  last_updated: '2026-04-27T20:17:23.699146'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-27T19:59:54.068665'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
reconciled: "2026-07-11 WS3-S8 sweep — TASK-MBC8-014: feature-rollup completed"
---

# Task: Crash-recovery integration tests for Mode B and Mode C non-terminal stages

## Description

Group D's crash-recovery scenarios assert that retry-from-scratch (the
authoritative recovery anchor from FEAT-FORGE-001 §5) holds for every
non-terminal stage of Mode B and Mode C. Durable history is the
authoritative status source; any live async-subagent state channel data is
advisory after a crash (ASSUM-009).

This task is the Mode B/C counterpart to TASK-MAG7-013 (Mode A crash
recovery) and exercises the same persistence layer but with the new chain
shapes.

## Acceptance Criteria

- [ ] `tests/integration/test_mode_b_c_crash_recovery.py` covers Group D
      Scenario Outlines verbatim:
  - [ ] **Mode B**: crash during `feature-spec`, `feature-plan`, or
        `autobuild` → restart → build re-enters `preparing` state → prior
        in-flight stage is reattempted from the start (no partial-progress
        replay)
  - [ ] **Mode C**: crash during `task-review` or `task-work` → restart →
        build re-enters `preparing` state → prior in-flight stage is
        reattempted from the start
- [ ] **Durable-history authority** (ASSUM-009):
  - [ ] An asynchronous Mode B `autobuild` is in flight; runtime crashes
  - [ ] On restart, the build's authoritative status is read from durable
        history; any live `async_tasks` state channel data is treated as
        advisory
  - [ ] The autobuild is re-dispatched as a fresh asynchronous task with a
        new task identifier (the previous task identifier is recorded as
        abandoned in stage history)
  - [ ] Same assertion holds for an in-flight Mode C `task-work`
- [ ] **Cycle-state preservation in Mode C**: a crash during the third of
      five fix tasks reattempts the third fix task from the start (not the
      first); the prior two completed `/task-work` entries remain in stage
      history with their `approved` status preserved
- [ ] **Approval-channel isolation across crash**: if Build A is paused at
      a flagged-for-review checkpoint when the runtime crashes, the resume
      after restart still routes the approval response to Build A only
      (build-identifier routing is durable, not in-process state)
- [ ] **Cancel during async crash recovery**: a `forge cancel` issued
      against a build whose autobuild was interrupted by a crash resolves
      the build to `cancelled` terminal without re-dispatching the
      autobuild
- [ ] Tests use the actual SQLite persistence layer (no in-memory shortcut)
      so the crash-recovery contract from FEAT-FORGE-001 is exercised end-
      to-end
- [ ] All modified files pass project-configured lint/format checks with zero
      errors

## Implementation Notes

Crash simulation reuses the harness from TASK-MAG7-013 (terminate the
supervisor task mid-stage; reconstruct it from the persisted SQLite state).
Do not introduce a new crash-simulation pattern.

The "abandoned task identifier" recording is the only place where in-process
state crosses the crash boundary. If the existing FEAT-FORGE-001 persistence
does not record abandoned task IDs, add the column and migration in this
task and reference the schema change in the IMPLEMENTATION-GUIDE.md §4.

## Test Execution Log

[Automatically populated by /task-work]

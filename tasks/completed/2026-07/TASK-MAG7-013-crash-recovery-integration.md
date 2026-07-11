---
id: TASK-MAG7-013
title: Crash-recovery integration tests across all seven non-terminal stages
task_type: testing
status: completed
priority: high
created: 2026-04-25 00:00:00+00:00
updated: 2026-04-25 00:00:00+00:00
parent_review: TASK-REV-MAG7
feature_id: FEAT-FORGE-007
wave: 5
implementation_mode: task-work
complexity: 6
dependencies:
- TASK-MAG7-010
- TASK-MAG7-011
tags:
- testing
- crash-recovery
- durable-history
- integration
- feat-forge-007
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-CBDE
  base_branch: main
  started_at: '2026-04-26T19:42:54.851441'
  last_updated: '2026-04-26T19:57:01.631187'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-26T19:42:54.851441'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
reconciled: "2026-07-11 WS3-S8 sweep — TASK-MAG7-013: feature-rollup completed"
---

# Task: Crash-recovery integration tests across all seven non-terminal stages

## Description

Integration tests covering FEAT-FORGE-007 Group D crash-recovery Scenario
Outline: a crash during any non-terminal stage triggers retry-from-scratch
on runtime restart. Also covers the durable-history-vs-advisory-state-channel
invariant (ASSUM-004): on crash mid-autobuild, the build's authoritative
status comes from durable history, not the live state channel.

Covers Group D @edge-case scenarios:
- "A crash during any non-terminal stage is recovered by retry-from-scratch"
  (seven examples: product-owner, architect, architecture, system-design,
  feature-spec, feature-plan, autobuild)
- "After a crash mid-autobuild the build's authoritative status comes from
  durable history not the live state channel"

## Acceptance Criteria

- [ ] Test module exists at
      `tests/integration/test_mode_a_crash_recovery.py`
- [ ] Parameterised test covers all seven stage classes from the Scenario
      Outline; for each:
      - Drive supervisor to that stage's mid-flight state
      - Trigger a fake crash (instantiate fresh supervisor against the
        same SQLite + state channel)
      - Assert build re-enters `preparing` state
      - Assert prior in-flight stage is reattempted from start
      - Assert no duplicate `stage_log` entry written for the
        re-attempted stage (idempotent retry)
- [ ] Mid-autobuild crash test:
      - Driver dispatches autobuild, waits until `AutobuildState.lifecycle`
        reaches `running_wave`
      - Triggers crash; on restart, supervisor consults `stage_log`
        (authoritative) and ignores `async_tasks` channel data (advisory)
      - Asserts build restarts autobuild from scratch
- [ ] Notification-publish-failure test (Group G @data-integrity):
      - Stub publisher raises `PublishFailure`
      - Assert stage still recorded as approved in `stage_log`
      - Assert next stage's prerequisite still evaluates as satisfied
- [ ] Long-term-memory-seeding-failure test (Group I @data-integrity):
      - Stub LTM seeder raises
      - Assert stage still recorded as approved
- [ ] All tests use FakeClock; no real wall-clock waits
- [ ] All modified files pass project-configured lint/format checks with
      zero errors

## Implementation Notes

The "crash" simulation is implemented by tearing down the supervisor
instance and instantiating a fresh one against the same SQLite and the same
LangGraph state channel. This exercises the same code path that runs after
a process restart.

The seven-stage parameterisation maps directly to the Group D Scenario
Outline. Asserting `re-enters preparing state` is the canary for the
retry-from-scratch policy — if any stage's recovery path tries to resume
mid-flight instead, this test fails.

## Test Execution Log

[Automatically populated by /task-work]

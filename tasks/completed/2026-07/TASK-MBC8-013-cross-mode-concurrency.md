---
id: TASK-MBC8-013
title: Cross-mode concurrency integration tests (Mode A + B + C in flight together)
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
- concurrency
- integration
- mode-a
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
  started_at: '2026-04-27T19:59:54.065312'
  last_updated: '2026-04-27T20:16:39.572261'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-27T19:59:54.065312'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
reconciled: "2026-07-11 WS3-S8 sweep — TASK-MBC8-013: feature-rollup completed"
---

# Task: Cross-mode concurrency integration tests (Mode A + B + C in flight together)

## Description

Group K's three-way mode interleave is the strongest concurrency assertion
in FEAT-FORGE-008: one Mode A build, one Mode B build, and one Mode C build
all in flight at the same time, holding isolated approval channels and
isolated stage chains. This task implements that integration test plus the
two-way Mode B/B and Mode B/C concurrency scenarios from Group F.

These tests pin down the supervisor's async-safety guarantee — the same
substrate that backed FEAT-FORGE-007's per-feature concurrency must hold
across modes (the supervisor is the only shared component).

## Acceptance Criteria

- [ ] `tests/integration/test_cross_mode_concurrency.py` covers:
  - [ ] **Two Mode B builds** running simultaneously: each gets its own
        autobuild task ID; each pauses at its own flagged-for-review
        checkpoint; an approval response targeting build 1 resolves only
        build 1 (Group F)
  - [ ] **Mode B + Mode C concurrent**: Mode B at autobuild, Mode C at
        task-work; both pause at flagged-for-review; approvals route by
        build identifier (Group F)
  - [ ] **Three-way interleave (Mode A + B + C)**: each build's recorded
        stage history reflects only its mode's stages; no cross-talk
        between approval channels (Group K)
  - [ ] **Supervisor responsiveness during async stages**: while Build 1's
        autobuild or task-work is in the running lifecycle, Build 2's first
        stage is dispatched without waiting (Group F "supervisor dispatches
        second build's stage during first build's async stage")
- [ ] **Idempotent first-wins under concurrency** (Group I):
  - [ ] Two simultaneous approval responses for the same paused stage with
        different decisions resolve under exactly one decision
  - [ ] No second resume is applied for the duplicate response
  - [ ] Recorded resume event count is exactly 1
- [ ] **Calibration-priors snapshot stability** (Group I, ASSUM-012):
  - [ ] Capture a calibration-priors snapshot at the start of a Mode B
        build
  - [ ] Mutate the operator's calibration history mid-build
  - [ ] Assert later stages of the in-flight build still see the snapshot
        captured at start
- [ ] **Notification publish failure isolation** (Group G):
  - [ ] A stage approval is recorded
  - [ ] The outbound NATS publish for that approval fails
  - [ ] The stage is still recorded as approved on the build's history; the
        next stage's prerequisite still evaluates as satisfied
- [ ] Tests run in under 60 seconds; concurrency uses `asyncio.gather`
      against an in-memory NATS adapter
- [ ] All modified files pass project-configured lint/format checks with zero
      errors

## Implementation Notes

Reuse `Supervisor` as a single instance across all builds in the test —
that is the contract the production supervisor satisfies. Multiple
`Supervisor` instances would mask the async-safety assertion.

The Group K three-way test is the strongest integration assertion in the
feature; if it passes, the substrate is reliably mode-agnostic. If it
fails, the failure mode points to the exact shared component (supervisor,
NATS adapter, or persistence layer).

## Test Execution Log

[Automatically populated by /task-work]

---
complexity: 6
created: 2026-04-27 00:00:00+00:00
dependencies:
- TASK-MBC8-003
- TASK-MBC8-004
- TASK-MBC8-005
- TASK-MBC8-006
- TASK-MBC8-007
feature_id: FEAT-FORGE-008
id: TASK-MBC8-008
implementation_mode: task-work
parent_review: TASK-REV-MBC8
priority: high
status: completed
tags:
- supervisor
- dispatch
- mode-b
- mode-c
- feat-forge-008
task_type: feature
test_results:
  coverage: null
  last_run: null
  status: pending
title: Wire mode-aware dispatch into Supervisor.next_turn
updated: 2026-04-27 00:00:00+00:00
wave: 4
reconciled: "2026-07-11 WS3-S8 sweep — TASK-MBC8-008: ['rollup: feature FEAT-FORGE-008 status=completed']"
---

# Task: Wire mode-aware dispatch into Supervisor.next_turn

## Description

`Supervisor.next_turn` (TASK-MAG7-010) currently assumes Mode A. This task
threads the build's `mode` through the dispatch loop so each turn calls the
correct planner (`ModeBChainPlanner`, `ModeCCyclePlanner`, or the existing
Mode A `PerFeatureLoopSequencer`) and the correct terminal handler.

This is the integration seam where every Wave 2/3 piece comes together. It
must keep Mode A's behaviour byte-identical (the FEAT-FORGE-007 substrate is
already shipped) while adding the two new dispatch paths.

## Acceptance Criteria

- [ ] `Supervisor.next_turn` reads `build.mode` and dispatches to one of:
  - [ ] `MODE_A` → existing `PerFeatureLoopSequencer` + Mode A guards
        (TASK-MAG7-010, unchanged)
  - [ ] `MODE_B` → `ModeBChainPlanner.plan_next_stage` for the next stage,
        `ModeBNoDiffTerminal.evaluate_post_autobuild` after `AUTOBUILD`
  - [ ] `MODE_C` → `ModeCCyclePlanner.plan_next_stage` for the next stage
        and per-fix-task ref, `ModeCTerminal.evaluate_terminal` at the
        cycle's end
- [ ] Dispatch routing within each mode:
  - [ ] Mode B subprocess stages (`FEATURE_SPEC`, `FEATURE_PLAN`) →
        `dispatch_subprocess_stage` (TASK-MAG7-008)
  - [ ] Mode B autobuild (`AUTOBUILD`) → `dispatch_autobuild_async`
        (TASK-MAG7-009)
  - [ ] Mode B PR review (`PULL_REQUEST_REVIEW`) → existing constitutional
        gate path (TASK-MAG7-004 unchanged)
  - [ ] Mode C `TASK_REVIEW` and `TASK_WORK` → `dispatch_subprocess_stage`
        with the fix-task ref injected via the extended
        `ForwardContextBuilder` (TASK-MBC8-005)
  - [ ] Mode C PR review when `has_commits` is true → existing constitutional
        gate path
- [ ] `StageOrderingGuard` (TASK-MAG7-003) is invoked with the per-mode
      `prerequisites` map (Mode A, Mode B, or Mode C) selected by build mode
- [ ] `ConstitutionalGuard` (TASK-MAG7-004) is invoked unchanged in every
      mode (ASSUM-011, Group E "constitutional belt-and-braces holds against
      misconfigured prompt")
- [ ] CLI steering injection (TASK-MAG7-011) works in every mode: cancel →
      synthetic reject; skip honoured on non-constitutional stages, refused
      on `PULL_REQUEST_REVIEW` regardless of mode (Group C scenarios)
- [ ] `Supervisor.next_turn` is async-safe: two concurrent Mode B builds and
      a concurrent Mode C build do not interfere (Group F, K). Asserted via
      a single test that runs three builds in parallel through a shared
      Supervisor instance.
- [ ] Hard-stop in any non-constitutional stage in Mode B or Mode C
      transitions the build to a failed terminal state with the originating
      rationale; no later stage is dispatched
- [ ] All modified files pass project-configured lint/format checks with zero
      errors

## Implementation Notes

The dispatch switch is a `match` on `build.mode`. Keep each branch small —
each one delegates to a planner + dispatcher pair. Resist the temptation to
re-implement Mode A inside the new branches; the existing branch must remain
untouched so the FEAT-FORGE-007 regression suite stays green.

The new branches share `dispatch_subprocess_stage` and
`dispatch_autobuild_async` with Mode A — these dispatchers are mode-agnostic
because they take a typed stage class and a forward-context spec. No new
dispatchers are created in this task.

## Test Execution Log

[Automatically populated by /task-work]
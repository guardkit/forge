---
id: TASK-MBC8-004
title: "Implement ModeCCyclePlanner with review\u2192work iteration and clean-review\
  \ terminal"
task_type: feature
status: completed
priority: high
created: 2026-04-27 00:00:00+00:00
updated: 2026-04-27 00:00:00+00:00
parent_review: TASK-REV-MBC8
feature_id: FEAT-FORGE-008
wave: 2
implementation_mode: task-work
complexity: 6
dependencies:
- TASK-MBC8-001
- TASK-MBC8-002
tags:
- planner
- mode-c
- cycle
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
  started_at: '2026-04-27T17:59:46.689735'
  last_updated: '2026-04-27T18:16:08.070985'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-27T17:59:46.689735'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
reconciled: "2026-07-11 WS3-S8 sweep — TASK-MBC8-004: feature-rollup completed"
---

# Task: Implement ModeCCyclePlanner with review→work iteration and clean-review terminal

## Description

Add `ModeCCyclePlanner` — the Mode C counterpart to `ModeBChainPlanner`, but
cyclic. It dispatches one `/task-work` per fix task identified by the most
recent `/task-review`, then on completion of the last fix task invokes a
follow-up `/task-review`. Termination is driven by reviewer judgement
(ASSUM-010): a follow-up review that returns no further fix tasks ends the
cycle with the clean-review terminal.

This is the only stage planner in the codebase that dispatches the same
stage class repeatedly within a single build. The planner must keep per-fix-
task lineage on every dispatch so the Group L data-integrity scenarios hold.

## Acceptance Criteria

- [ ] `forge.pipeline.mode_c_planner` module exposes `ModeCCyclePlanner`
      class (or `plan_next_stage` function) with signature
      `(build: Build, history: Sequence[StageEntry]) -> ModeCPlan`
      where `ModeCPlan` exposes `permitted_stages`, `next_stage`,
      `next_fix_task: FixTaskRef | None`, and
      `terminal: ModeCTerminal | None`
- [ ] Empty initial review returns `terminal = CLEAN_REVIEW`, `next_stage = None`
      (Group B Scenario "task-review returns empty set"); planner does not
      issue any `/task-work` dispatch (ASSUM-007)
- [ ] Non-empty initial review with N fix tasks returns sequential
      `next_stage = TASK_WORK` plans, one per fix task; the planner advances
      to the next fix task only after the prior one is recorded as approved
      (Group B "downstream prerequisite") and not before (ASSUM-008 isolates
      failure to its fix task)
- [ ] After the last fix task's `/task-work` is approved, planner schedules
      a follow-up `/task-review` (ASSUM-010 — no numeric cap)
- [ ] Follow-up review with no further fix tasks returns
      `terminal = CLEAN_REVIEW` if no commits were produced, or
      `next_stage = PULL_REQUEST_REVIEW` if commits exist (delegated check —
      planner reads a `BuildContext.has_commits` flag set by TASK-MBC8-007)
- [ ] Failed `/task-work` for a fix task is recorded against that fix task
      and does NOT auto-cancel sibling fix tasks; the build's continuation
      decision is gate-driven (ASSUM-008). Planner returns the next fix task
      in line as `next_fix_task` even when a prior fix task failed
- [ ] Hard-stop on `/task-review` (Group C negative case) returns
      `next_stage = None` and `terminal = FAILED`; no `/task-work` dispatch
- [ ] Each `next_fix_task` decision records a `FixTaskRef` containing the
      fix-task identifier and a back-reference to the originating
      `/task-review` stage entry — the audit anchor for Group L lineage
      scenarios
- [ ] Unit tests cover all 14 Mode C Group A/B/C/D scenarios from the feature
      file
- [ ] All modified files pass project-configured lint/format checks with zero
      errors

## Implementation Notes

The planner is stateless: every call inspects history. Cyclic behaviour
emerges from the planner deciding the same `next_stage = TASK_WORK` repeatedly
until the fix-task list is exhausted, then scheduling a follow-up review.

`FixTaskRef` is a small dataclass — keep it co-located with the planner
rather than promoting it to taxonomy. Other code only sees fix tasks via the
planner's output.

The planner does not parse `/task-review` output JSON itself — it consumes a
typed `FixTaskList` produced by the subprocess dispatcher's result handler
(see `dispatch_subprocess_stage` from TASK-MAG7-008). If the result handler
needs new fields to surface fix tasks, add them in this task and document the
shape change in the IMPLEMENTATION-GUIDE.md §4 Integration Contracts.

## Test Execution Log

[Automatically populated by /task-work]

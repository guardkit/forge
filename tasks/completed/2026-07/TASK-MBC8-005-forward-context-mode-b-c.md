---
id: TASK-MBC8-005
title: Extend ForwardContextBuilder for Mode B and Mode C contracts
task_type: feature
status: completed
priority: high
created: 2026-04-27 00:00:00+00:00
updated: 2026-04-27 00:00:00+00:00
parent_review: TASK-REV-MBC8
feature_id: FEAT-FORGE-008
wave: 2
implementation_mode: task-work
complexity: 4
dependencies:
- TASK-MBC8-002
tags:
- forward-context
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
  started_at: '2026-04-27T17:59:46.692266'
  last_updated: '2026-04-27T18:15:41.454971'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-27T17:59:46.692266'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
reconciled: "2026-07-11 WS3-S8 sweep — TASK-MBC8-005: feature-rollup completed"
---

# Task: Extend ForwardContextBuilder for Mode B and Mode C contracts

## Description

`ForwardContextBuilder` (TASK-MAG7-006) maps approved upstream artefacts
into `--context` flags for the next stage. Mode B's contracts are a strict
subset of Mode A's (spec→plan→autobuild are unchanged) but the per-stage
contract map needs Mode B–scoped predecessors so the planner can reuse the
same builder.

Mode C introduces a new contract: each `/task-work` dispatch must receive
the fix-task definition produced by the most recent `/task-review`
(FEAT-FORGE-008 Group A "Each /task-work dispatch is supplied with the
fix-task definition produced by /task-review").

## Acceptance Criteria

- [ ] `ForwardContextBuilder` gains a `mode: BuildMode` parameter (or per-mode
      contract map) so callers can request the right contract set; existing
      Mode A callers get the existing behaviour with no change in output
- [ ] Mode B contracts:
  - [ ] `FEATURE_PLAN` receives `feature-spec` artefact paths from the
        approved `/feature-spec` stage entry (Group A "approved feature
        specification is supplied as input to feature planning")
  - [ ] `AUTOBUILD` receives `feature-plan` artefact paths from the approved
        `/feature-plan` stage entry (Group A "approved plan is supplied to
        autobuild")
  - [ ] Mode B forbidden stages raise `ModeBoundaryViolation` if any caller
        attempts to build context for them
- [ ] Mode C contracts:
  - [ ] `TASK_WORK` receives the `FixTaskRef` for the fix task being
        dispatched (Group A) AND the `/task-review` artefact paths that
        produced it (Group L lineage)
  - [ ] Follow-up `TASK_REVIEW` receives the artefact paths from every
        completed `/task-work` in the cycle (so the reviewer sees the
        applied fixes)
- [ ] Builder is pure: `(build, history, mode, target_stage) -> ContextSpec`
      with no side effects
- [ ] Unit tests cover the Mode B and Mode C forward-propagation key-example
      scenarios verbatim
- [ ] All modified files pass project-configured lint/format checks with zero
      errors

## Implementation Notes

Reuse the existing `ContextSpec` shape from MAG7. The `mode` parameter is
additive — Mode A callers default to `MODE_A` and see no behaviour change.

The Mode C `FixTaskRef` payload is the cross-task data dependency documented
in IMPLEMENTATION-GUIDE.md §4. Keep its serialisation co-located with
`ModeCCyclePlanner` so consumers have a single source of truth.

## Test Execution Log

[Automatically populated by /task-work]

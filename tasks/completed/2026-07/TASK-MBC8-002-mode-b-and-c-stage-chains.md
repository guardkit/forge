---
id: TASK-MBC8-002
title: Define Mode B and Mode C stage chains and prerequisite maps
task_type: declarative
status: completed
priority: high
created: 2026-04-27 00:00:00+00:00
updated: 2026-04-27 00:00:00+00:00
parent_review: TASK-REV-MBC8
feature_id: FEAT-FORGE-008
wave: 1
implementation_mode: direct
complexity: 2
dependencies: []
tags:
- taxonomy
- declarative
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
  started_at: '2026-04-27T17:46:32.891300'
  last_updated: '2026-04-27T17:58:00.954776'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-27T17:46:32.891300'
    player_summary: 'Created forge.pipeline.mode_chains_data as a pure declarative-data
      module: MODE_A_CHAIN, MODE_B_CHAIN, MODE_C_CHAIN tuples, MODE_B_FORBIDDEN_STAGES
      / MODE_C_FORBIDDEN_STAGES frozensets, CHAIN_BY_MODE Mapping[BuildMode, tuple[StageClass,
      ...]] keyed exhaustively over BuildMode, and MODE_B_PREREQUISITES / MODE_C_PREREQUISITES
      dicts mirroring the shape of STAGE_PREREQUISITES. The module imports only StageClass
      from forge.pipeline.stage_taxonomy and BuildMode from forge.lifecycle.modes
      so it is impo'
    player_success: true
    coach_success: true
reconciled: "2026-07-11 WS3-S8 sweep — TASK-MBC8-002: feature-rollup completed"
---

# Task: Define Mode B and Mode C stage chains and prerequisite maps

## Description

Define the two non-greenfield stage chains as declarative data so the planners
in Wave 2 can be pure functions over them. Mode B is a strict subsequence of
Mode A starting at `/feature-spec`; Mode C is a new cyclic chain composed of
`/task-review` and `/task-work` with an optional terminal `/pull-request`.

The chains encode the "Mode B does not dispatch product-owner / architect /
system-arch / system-design" boundary (FEAT-FORGE-008 Group J) and the
"task-work waits on its review entry" ordering (Group B Scenario Outline).

## Acceptance Criteria

- [ ] New module `forge.pipeline.mode_chains_data` (or extend
      `stage_taxonomy.py`) exports:
  - [ ] `MODE_B_CHAIN: tuple[StageClass, ...]` =
        `(FEATURE_SPEC, FEATURE_PLAN, AUTOBUILD, PULL_REQUEST_REVIEW)`
  - [ ] `MODE_C_CHAIN: tuple[StageClass, ...]` =
        `(TASK_REVIEW, TASK_WORK, PULL_REQUEST_REVIEW)` with
        documentation noting `TASK_WORK` repeats per fix task and
        `PULL_REQUEST_REVIEW` is conditional on commits
  - [ ] `MODE_B_FORBIDDEN_STAGES: frozenset[StageClass]` containing
        `PRODUCT_OWNER`, `ARCHITECT`, `SYSTEM_ARCH`, `SYSTEM_DESIGN`
  - [ ] `MODE_C_FORBIDDEN_STAGES: frozenset[StageClass]` containing every
        Mode A pre-feature-spec stage plus `FEATURE_SPEC`, `FEATURE_PLAN`,
        `AUTOBUILD` (Mode C operates on existing artefacts)
  - [ ] `CHAIN_BY_MODE: Mapping[BuildMode, tuple[StageClass, ...]]` mapping
        each `BuildMode` value to its chain (Mode A continues to use the
        existing 8-stage chain)
- [ ] `MODE_B_PREREQUISITES`: same shape as `STAGE_PREREQUISITES` but only
      for Mode B's four stages
- [ ] `MODE_C_PREREQUISITES`: same shape but for Mode C's three stage classes
      (`TASK_WORK ← TASK_REVIEW`, `PULL_REQUEST_REVIEW ← TASK_WORK`)
- [ ] Module docstring references FEAT-FORGE-008 ASSUM-001 (Mode B chain),
      ASSUM-004 (Mode C chain), ASSUM-013 (mode-aware planning refuses
      upstream Mode A stages), ASSUM-014 (Mode B does not dispatch to
      specialists)
- [ ] All modified files pass project-configured lint/format checks with zero
      errors

## Implementation Notes

This is pure declarative data — no runtime behaviour. Keep it import-cycle-free
so it can be consumed by every Wave 2 planner.

The constitutional `PULL_REQUEST_REVIEW` stage is shared across modes; do not
copy `CONSTITUTIONAL_STAGES` per mode. The constitutional rule is mode-agnostic
(ASSUM-011) and lives on the existing `ConstitutionalGuard` from TASK-MAG7-004.

Mode C's chain is intentionally length-3 (not length-2); the cycle controller
in TASK-MBC8-004 handles the per-fix-task fan-out of `TASK_WORK` rather than
encoding it in the chain shape.

## Test Execution Log

[Automatically populated by /task-work]

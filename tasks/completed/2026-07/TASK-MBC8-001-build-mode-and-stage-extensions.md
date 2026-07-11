---
id: TASK-MBC8-001
title: Add BuildMode enum and extend StageClass with TASK_REVIEW + TASK_WORK
task_type: declarative
status: completed
priority: high
created: 2026-04-27 00:00:00+00:00
updated: 2026-04-27 00:00:00+00:00
parent_review: TASK-REV-MBC8
feature_id: FEAT-FORGE-008
wave: 1
implementation_mode: direct
complexity: 3
dependencies: []
tags:
- taxonomy
- declarative
- build-mode
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
  started_at: '2026-04-27T17:46:32.890136'
  last_updated: '2026-04-27T17:59:34.166354'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-27T17:46:32.890136'
    player_summary: "1) Added BuildMode(StrEnum) with MODE_A/MODE_B/MODE_C (string\
      \ values 'mode-a'/'mode-b'/'mode-c') in a new src/forge/lifecycle/modes.py.\
      \ The module file already existed when the worktree was opened \u2014 kept its\
      \ docstring/structure intact. 2) Extended StageClass with TASK_REVIEW='task-review'\
      \ and TASK_WORK='task-work' appended at the END so Mode A's iteration prefix\
      \ is byte-for-byte preserved (StageOrderingGuard contract). Added STAGE_PREREQUISITES[TASK_WORK]=[TASK_REVIEW]\
      \ (single new row; per-fix-t"
    player_success: true
    coach_success: true
reconciled: "2026-07-11 WS3-S8 sweep — TASK-MBC8-001: feature-rollup completed"
---

# Task: Add BuildMode enum and extend StageClass with TASK_REVIEW + TASK_WORK

## Description

Introduce the `BuildMode` enum (`MODE_A`, `MODE_B`, `MODE_C`) so the Supervisor
can dispatch by chain shape, and extend the canonical stage taxonomy with the
two Mode C stage classes (`/task-review` and `/task-work`). Persist `mode` on
the `Build` row so crash recovery and the queue picker can route correctly
without re-deriving it.

This is declarative groundwork for Wave 2's planners. It must not change Mode A
behaviour: existing builds default to `MODE_A` on read.

## Acceptance Criteria

- [ ] `forge.lifecycle.identifiers` (or a new `forge.lifecycle.modes`) exports
      `BuildMode(StrEnum)` with members `MODE_A = "mode-a"`,
      `MODE_B = "mode-b"`, `MODE_C = "mode-c"`
- [ ] `forge.pipeline.stage_taxonomy` extends `StageClass` with new members
      `TASK_REVIEW = "task-review"` and `TASK_WORK = "task-work"` placed at the
      end of the enum so existing Mode A iteration order is preserved
- [ ] `STAGE_PREREQUISITES` adds `TASK_WORK ← TASK_REVIEW` (single entry; the
      per-fix-task fan-out is enforced by `ModeCCyclePlanner` in TASK-MBC8-004)
- [ ] `PER_FEATURE_STAGES` is unchanged; new `PER_FIX_TASK_STAGES` frozenset
      contains `TASK_WORK`
- [ ] `Build` model in `forge.lifecycle.persistence` gains `mode: BuildMode`
      with default `MODE_A`; `BuildRow` mirrors the column
- [ ] SQLite migration adds `mode TEXT NOT NULL DEFAULT 'mode-a'` to `builds`
      table; backfill is the default literal so historical rows stay valid
- [ ] `SqliteLifecyclePersistence.queue_build` accepts `mode: BuildMode` and
      writes it; `BuildStatusView` exposes `mode` for the CLI status command
- [ ] All existing Mode A tests still pass without modification
- [ ] All modified files pass project-configured lint/format checks with zero
      errors

## Implementation Notes

`BuildMode` lives next to `Build` rather than `StageClass` because the mode is
a property of the build, not the stage. Importing `BuildMode` from a lifecycle
module keeps `stage_taxonomy.py` import-cycle-free.

The `TASK_REVIEW` / `TASK_WORK` extension to `StageClass` deliberately appends
to the end so Mode A's iteration-order contract (used by `StageOrderingGuard`
in TASK-MAG7-003) is preserved. Mode-specific iteration order is the planners'
responsibility, not the enum's.

Migration must be additive only — no data loss, no rename. Existing rows
backfill to `mode-a` so the substrate keeps working for any in-flight Mode A
build during the upgrade window.

## Test Execution Log

[Automatically populated by /task-work]

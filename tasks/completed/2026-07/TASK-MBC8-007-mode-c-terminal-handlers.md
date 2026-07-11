---
id: TASK-MBC8-007
title: Implement Mode C terminal handlers (empty review and no commits)
task_type: feature
status: completed
priority: high
created: 2026-04-27 00:00:00+00:00
updated: 2026-04-27 00:00:00+00:00
parent_review: TASK-REV-MBC8
feature_id: FEAT-FORGE-008
wave: 3
implementation_mode: task-work
complexity: 4
dependencies:
- TASK-MBC8-004
tags:
- terminal
- mode-c
- clean-review
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
  started_at: '2026-04-27T18:16:08.141943'
  last_updated: '2026-04-27T18:32:26.007721'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-27T18:16:08.141943'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
reconciled: "2026-07-11 WS3-S8 sweep — TASK-MBC8-007: feature-rollup completed"
---

# Task: Implement Mode C terminal handlers (empty review and no commits)

## Description

Mode C has two clean-terminal paths and one PR-review path:

1. **Empty initial review** — `/task-review` returns no fix tasks; build
   completes with `clean-review` outcome and no `/task-work` dispatched
   (ASSUM-007).
2. **No commits after fix-task loop** — every dispatched `/task-work` ran
   but produced no commits; build completes with `clean-review` outcome and
   no PR creation attempted (ASSUM-017).
3. **Commits after fix-task loop** — at least one `/task-work` produced a
   commit; build advances to `PULL_REQUEST_REVIEW` (ASSUM-005).

This handler is the single decision point for Mode C terminal routing. It
also records the per-fix-task artefact attribution (Group G) so each
`/task-work` stage entry references only the artefacts produced for its own
fix task.

## Acceptance Criteria

- [ ] `forge.pipeline.terminal_handlers.mode_c` module exposes
      `evaluate_terminal(build, history) -> ModeCTerminal` with variants
      `CLEAN_REVIEW_NO_FIXES`, `CLEAN_REVIEW_NO_COMMITS`, `PR_REVIEW`,
      `FAILED`
- [ ] `CLEAN_REVIEW_NO_FIXES` path: initial `/task-review` returned an
      empty fix-task list; recorded with rationale
      `"mode-c-task-review-empty"`
- [ ] `CLEAN_REVIEW_NO_COMMITS` path: every dispatched `/task-work` is
      approved, but `git rev-list base..HEAD` against the build's worktree
      returns zero commits; recorded with rationale
      `"mode-c-no-commits"`
- [ ] `PR_REVIEW` path: at least one `/task-work` is approved and there is
      at least one commit in the worktree; build routes to constitutional
      PR-review gate
- [ ] `FAILED` path: `/task-review` hard-stop OR all dispatched `/task-work`
      ended in failed terminal lifecycle; recorded with the originating
      hard-stop rationale
- [ ] `BuildContext.has_commits` flag set from this handler so
      `ModeCCyclePlanner` (TASK-MBC8-004) can route the follow-up review
      branch
- [ ] Per-fix-task artefact attribution recorded on each `TASK_WORK` stage
      entry: `artefact_paths` list contains only paths produced by that
      fix task (Group G "no artefact path attributed to more than one fix
      task")
- [ ] Fix-task lineage on each `TASK_WORK` entry: `fix_task_id` and
      `originating_review_entry_id` (Group L data-integrity scenario)
- [ ] Recorded session outcome for `CLEAN_REVIEW_*` carries no
      `pull_request_url` and no PR-review gate decision
- [ ] Unit tests cover the four Mode C terminal scenarios from Group D, G,
      L, N
- [ ] All modified files pass project-configured lint/format checks with zero
      errors

## Implementation Notes

The "no commits" check uses `git rev-list base..HEAD --count` against the
build's worktree path; reuse the worktree allowlist resolution from
FEAT-FORGE-005 — do not invent a new path resolver.

The commit count is an integer, not a parsed list — keep the handler cheap.
A failed shell-out is treated as `FAILED` with rationale
`"mode-c-commit-check-failed"`; do not silently convert to clean-review.

## Test Execution Log

[Automatically populated by /task-work]

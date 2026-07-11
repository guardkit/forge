---
id: TASK-MBC8-006
title: Implement Mode B no-diff terminal handler
task_type: feature
status: completed
priority: high
created: 2026-04-27 00:00:00+00:00
updated: 2026-04-27 00:00:00+00:00
parent_review: TASK-REV-MBC8
feature_id: FEAT-FORGE-008
wave: 3
implementation_mode: task-work
complexity: 3
dependencies:
- TASK-MBC8-003
tags:
- terminal
- mode-b
- no-op
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
  started_at: '2026-04-27T18:16:08.147053'
  last_updated: '2026-04-27T18:29:27.829878'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-27T18:16:08.147053'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
reconciled: "2026-07-11 WS3-S8 sweep — TASK-MBC8-006: feature-rollup completed"
---

# Task: Implement Mode B no-diff terminal handler

## Description

Mode B's autobuild can succeed without producing any diff against the working
branch (e.g. when the plan resolves to documentation-only or already-applied
changes). The constitutional PR-review gate has nothing to fire on in that
case (ASSUM-015), so the build must terminate with a `no-op` outcome rather
than pause at PR review or attempt PR creation against an empty diff.

This handler runs after Mode B's `AUTOBUILD` stage approval and before the
Supervisor would normally route to PR creation. It is the single place where
the no-diff vs has-diff decision is made for Mode B.

## Acceptance Criteria

- [ ] `forge.pipeline.terminal_handlers.mode_b_no_diff` module (or extension
      to existing terminal handler module) exposes
      `evaluate_post_autobuild(build, history) -> ModeBPostAutobuild`
      where the result is one of `PR_REVIEW`, `NO_OP`, or `FAILED`
- [ ] `NO_OP` path: invoked when autobuild reports zero changed files vs
      the working branch's HEAD; recorded as a terminal `complete` build
      with rationale `"mode-b-autobuild-no-diff"` (Group M scenario "no-diff
      autobuild does not attempt pull-request creation")
- [ ] `PR_REVIEW` path: autobuild reports a non-empty diff; build advances
      to `PULL_REQUEST_REVIEW` (constitutional gate) — no PR creation
      happens here, only the routing decision
- [ ] `FAILED` path: autobuild reached a failed terminal lifecycle;
      build is recorded as failed with the autobuild's hard-stop rationale
      surfaced (Group C "internal hard-stop is propagated"); no PR creation
      attempted
- [ ] Handler reads diff status from `dispatch_autobuild_async`'s result
      payload (TASK-MAG7-009 already records `changed_files_count` in
      autobuild result); no shell-out to `git diff` from this layer
- [ ] No PR-creation call site is reachable when handler returns `NO_OP` or
      `FAILED`; assert via test that `gh pr create` adapter is not called
- [ ] Recorded session outcome for `NO_OP` carries no `pull_request_url` and
      no PR-review gate decision (Group M acceptance)
- [ ] Unit tests cover the three Group A/C/M Mode B scenarios that hit this
      handler
- [ ] All modified files pass project-configured lint/format checks with zero
      errors

## Implementation Notes

Keep this handler small — it is a routing shim, not a dispatcher. The actual
PR creation happens in TASK-MAG7-008's subprocess dispatcher; this handler
just decides whether the routing reaches that layer at all.

If autobuild's result schema does not yet expose `changed_files_count`, add
that field in this task and pin the exact shape change in the
IMPLEMENTATION-GUIDE.md §4 Integration Contracts.

## Test Execution Log

[Automatically populated by /task-work]

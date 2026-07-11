---
id: TASK-MBC8-011
title: Mode C smoke E2E (queue through clean-review and PR-awaiting-review terminals)
task_type: testing
status: completed
priority: high
created: 2026-04-27 00:00:00+00:00
updated: 2026-04-27 00:00:00+00:00
parent_review: TASK-REV-MBC8
feature_id: FEAT-FORGE-008
wave: 6
implementation_mode: task-work
complexity: 5
dependencies:
- TASK-MBC8-008
- TASK-MBC8-009
tags:
- smoke
- e2e
- mode-c
- testing
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
  started_at: '2026-04-27T19:38:52.172305'
  last_updated: '2026-04-27T19:53:42.199965'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-27T19:38:52.172305'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
reconciled: "2026-07-11 WS3-S8 sweep — TASK-MBC8-011: feature-rollup completed"
---

# Task: Mode C smoke E2E (queue through clean-review and PR-awaiting-review terminals)

## Description

End-to-end smoke test that drives Mode C builds through the three terminal
shapes:

1. **Empty initial review** → clean-review terminal, no `/task-work`
   dispatched (ASSUM-007 / Group B scenario)
2. **N fix tasks → no commits** → clean-review terminal, no PR creation
   attempted (ASSUM-017 / Group N scenario)
3. **N fix tasks → commits** → PR-awaiting-review terminal at the
   constitutional gate (ASSUM-005 / Group A key-example)

Each path is a separate test case so a single regression is isolated.

## Acceptance Criteria

- [ ] `tests/integration/test_mode_c_smoke_e2e.py` enqueues Mode C builds and
      asserts the three terminal shapes:
  - [ ] **Empty review** scenario: `/task-review` returns zero fix tasks;
        no `/task-work` stage entry is recorded; build reaches `complete`
        terminal state with `clean-review` rationale; no PR URL recorded
  - [ ] **N fix tasks, no commits** scenario: three fix tasks dispatched and
        approved; worktree commit count is zero; build reaches `complete`
        terminal state with `clean-review` rationale; no PR URL recorded
  - [ ] **N fix tasks, commits exist** scenario: three fix tasks dispatched
        and approved; worktree has at least one commit; build pauses at
        `pull-request-review` with `MANDATORY_HUMAN_APPROVAL`
- [ ] Stage-history shape assertions:
  - [ ] `/task-review` entry precedes every `/task-work` entry it produced
        (Group G ordering)
  - [ ] Each `/task-work` entry references exactly one fix task identifier
        (Group B "every dispatched task-work should reference exactly one
        fix task identifier")
  - [ ] Per-fix-task artefact paths attribute only to the fix task that
        produced them (Group G "no artefact path attributed to more than
        one fix task")
  - [ ] Each `/task-work` entry carries `originating_review_entry_id`
        pointing to the `/task-review` entry that produced its fix task
        (Group L lineage)
- [ ] Failure-isolation assertion (ASSUM-008): one fix task's `/task-work`
      returns a failed result; sibling fix tasks still get dispatched; the
      failed fix task's failure is recorded against itself
- [ ] Hard-stop assertion: `/task-review` returns a hard-stop result; no
      `/task-work` dispatch is recorded; build reaches `failed` terminal
      state (Group C)
- [ ] Cycle-termination assertion (ASSUM-010): after N fix tasks complete, a
      follow-up `/task-review` returns no further fix tasks; no further
      `/task-work` dispatch; cycle terminates with the appropriate clean or
      PR terminal
- [ ] Tests run in under 60 seconds with all dispatchers stubbed
- [ ] All modified files pass project-configured lint/format checks with zero
      errors

## Implementation Notes

Reuse the in-memory worktree fake from FEAT-FORGE-005 to assert commit count
without a real git repo. The "commits exist" branch can stub
`git rev-list --count` to return `1`; the "no commits" branch returns `0`.

Mode C's failure-isolation behaviour is a per-fix-task assertion; do not
collapse it into a single "any failure halts the cycle" check. Sibling fix
tasks must continue per ASSUM-008.

## Test Execution Log

[Automatically populated by /task-work]

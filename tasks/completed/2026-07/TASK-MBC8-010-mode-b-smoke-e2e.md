---
id: TASK-MBC8-010
title: Mode B smoke E2E (queue to PR-awaiting-review terminal)
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
- mode-b
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
  started_at: '2026-04-27T19:38:52.168515'
  last_updated: '2026-04-27T19:58:01.752626'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-27T19:38:52.168515'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
reconciled: "2026-07-11 WS3-S8 sweep — TASK-MBC8-010: feature-rollup completed"
---

# Task: Mode B smoke E2E (queue to PR-awaiting-review terminal)

## Description

End-to-end smoke test that drives a single Mode B build from `forge queue
--mode b <FEAT-ID>` through `/feature-spec → /feature-plan → autobuild`
and pauses at the constitutional `/pull-request review` gate. Subprocess
dispatchers are stubbed at the boundary (FEAT-FORGE-005 already provides the
fakes); NATS approval channel uses the in-memory adapter from FEAT-FORGE-002.

This is the Mode B counterpart to TASK-MAG7-012 (Mode A smoke). It pins the
canonical Mode B happy path and the no-PO/no-architect axiom (Group L).

## Acceptance Criteria

- [ ] `tests/integration/test_mode_b_smoke_e2e.py` enqueues a Mode B build,
      drives every flagged-for-review checkpoint to auto-approve, and asserts:
  - [ ] Stage history contains exactly four entries in order:
        `feature-spec`, `feature-plan`, `autobuild`, `pull-request-review`
        (Group G data-integrity scenario)
  - [ ] No `product-owner`, `architect`, `system-arch`, or `system-design`
        entry appears (Group L positive assertion of no-PO/no-architect
        axiom)
  - [ ] No degraded-specialist rationale appears anywhere on the build's
        stage history (Group L)
  - [ ] Build pauses at `pull-request-review` with a `MANDATORY_HUMAN_APPROVAL`
        gate (Group A constitutional pin)
  - [ ] PR URL is recorded against the build (Group H smoke acceptance)
- [ ] Forward-propagation assertions:
  - [ ] `/feature-plan` dispatch context contains the `/feature-spec`
        artefact paths from the approved spec entry
  - [ ] `autobuild` dispatch context contains the `/feature-plan` artefact
        paths from the approved plan entry
  - [ ] No autobuild dispatch was recorded before plan approval; no plan
        dispatch before spec approval (Group A "supplied as input")
- [ ] Asynchronous dispatch assertions:
  - [ ] `autobuild` is dispatched via `dispatch_autobuild_async` and the
        `async_tasks` state channel exposes wave/task indices during the run
  - [ ] Supervisor remains responsive to status queries while autobuild is
        in flight (Group A async key-example)
- [ ] CLI steering assertions:
  - [ ] A `forge skip` against the `pull-request-review` pause is refused
        with constitutional rationale (Group C); a `forge skip` against the
        `feature-plan` flag-for-review pause is honoured and the chain
        resumes (Group D edge-case)
  - [ ] A `forge cancel` while paused at any pre-PR checkpoint resolves as
        a synthetic reject; build reaches `cancelled` terminal state
- [ ] Test runs in under 30 seconds with all dispatchers stubbed
- [ ] All modified files pass project-configured lint/format checks with zero
      errors

## Implementation Notes

Reuse MAG7's smoke harness (`tests/integration/test_mode_a_smoke_e2e.py` if
present, or the Mode A capstone harness from FEAT-FORGE-007) — Mode B's
smoke shape is a subsequence, not a separate harness.

This test is the canonical regression for the Mode B chain shape; if it
fails, no later Mode B work should land. Mark it `@smoke` so the autobuild
scheduler runs it before the larger BDD bindings test in TASK-MBC8-012.

## Test Execution Log

[Automatically populated by /task-work]

---
id: TASK-MAG7-012
title: Smoke test - minimal greenfield brief to PR-awaiting-review
task_type: testing
status: completed
priority: high
created: 2026-04-25 00:00:00+00:00
updated: 2026-04-25 00:00:00+00:00
parent_review: TASK-REV-MAG7
feature_id: FEAT-FORGE-007
wave: 5
implementation_mode: task-work
complexity: 5
dependencies:
- TASK-MAG7-010
- TASK-MAG7-011
tags:
- testing
- smoke
- integration
- end-to-end
- feat-forge-007
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-CBDE
  base_branch: main
  started_at: '2026-04-26T19:42:54.847588'
  last_updated: '2026-04-26T19:57:32.864345'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-26T19:42:54.847588'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
reconciled: "2026-07-11 WS3-S8 sweep — TASK-MAG7-012: feature-rollup completed"
---

# Task: Smoke test — minimal greenfield brief to PR-awaiting-review

## Description

End-to-end smoke test that drives a one-line product brief through every
Mode A stage with auto-approval at every flagged-for-review checkpoint, and
asserts the build terminates paused at pull-request review awaiting human
approval with a recorded PR URL.

Covers Group H @smoke @integration scenarios:
- "A minimal greenfield brief drives a single-feature run to a pull request
  awaiting human review"
- "A greenfield build with no available specialists is flagged for review
  at every specialist stage" (degraded path)

## Acceptance Criteria

- [ ] Test module exists at
      `tests/integration/test_mode_a_smoke.py`
- [ ] Test fixture `greenfield_brief_pipeline` brings up: a fake
      specialist registry pre-populated with healthy product-owner +
      architect specialists, a fake GuardKit subprocess engine returning
      canned approved artefacts, an in-memory SQLite, and a stub approval
      channel that auto-approves
- [ ] Smoke test: queue one-line brief, drive supervisor turns until
      terminal, assert build is paused at PR-review with mandatory_human
      gate mode, PR URL recorded in `stage_log`
- [ ] Smoke test asserts `stage_log` contains the eight-stage chain in
      canonical order (Group G @data-integrity scenario covered as part
      of smoke)
- [ ] Degraded test: same harness with empty specialist registry; assert
      build is flagged for review at product-owner stage with degraded
      rationale, no architect dispatch occurred
- [ ] Tests use the existing `FakeClock` pattern from
      `src/forge/pipeline.py` for deterministic timing
- [ ] All modified files pass project-configured lint/format checks with
      zero errors

## Implementation Notes

This is the headline smoke for FEAT-FORGE-007 — it asserts that the
composition works end-to-end. Keep the harness small: real substrate
adapters (FEAT-FORGE-001 SQLite, FEAT-FORGE-005 subprocess engine) are
mocked at their Protocol boundaries; only the FEAT-FORGE-007 net-new code
runs for real.

The auto-approval-at-every-checkpoint is the canary — if any
non-constitutional gate fails to auto-approve under high Coach scores, the
smoke fails and identifies the gate decision that diverged.

## Test Execution Log

[Automatically populated by /task-work]

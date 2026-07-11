---
id: TASK-MAG7-014
title: Concurrency and multi-feature integration tests
task_type: testing
status: completed
priority: high
created: 2026-04-25 00:00:00+00:00
updated: 2026-04-25 00:00:00+00:00
parent_review: TASK-REV-MAG7
feature_id: FEAT-FORGE-007
wave: 5
implementation_mode: task-work
complexity: 7
dependencies:
- TASK-MAG7-010
- TASK-MAG7-011
tags:
- testing
- concurrency
- multi-feature
- integration
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
  started_at: '2026-04-26T19:42:54.843521'
  last_updated: '2026-04-26T19:57:46.825264'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-26T19:42:54.843521'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
reconciled: "2026-07-11 WS3-S8 sweep — TASK-MAG7-014: feature-rollup completed"
---

# Task: Concurrency and multi-feature integration tests

## Description

Integration tests covering the concurrency, multi-feature, and integrity
invariants of Mode A: two concurrent builds with isolated channels, supervisor
dispatching during long-running autobuild, multi-feature catalogues with
per-feature sequencing, correlation-identifier threading, calibration-priors
snapshot stability, first-wins idempotency, and the constitutional
belt-and-braces test against a misconfigured prompt.

Covers Group F @concurrency, Group H @integration (multi-feature), Group E
@security (constitutional + worktree confinement), Group G + Group I
@data-integrity, and Group D / Group I idempotency scenarios.

## Acceptance Criteria

- [ ] Test module exists at
      `tests/integration/test_mode_a_concurrency_and_integrity.py`
- [ ] Two-concurrent-builds test (Group F):
      - Queue two builds with distinct correlation_ids
      - Drive both supervisors' `next_turn` interleaved
      - Assert each build has a distinct autobuild `task_id`
      - Assert each build's approval pause resolves only on a response
        matching its own `build_id` (Group D edge-case)
      - Assert second build's product-owner stage dispatches without
        waiting for first build's autobuild to complete (Group F)
- [ ] Multi-feature integration test (Group H):
      - System-design fixture returns a 3-feature catalogue
      - Drive build to terminal; assert one /feature-spec, one /feature-plan,
        one autobuild stage entry per feature
      - Assert one PR-review pause per feature
      - Assert per-feature artefact paths attributed correctly (no path
        attributed to >1 feature) — Group G @data-integrity
- [ ] Per-feature sequencing test (Group D ASSUM-006):
      - Three-feature catalogue
      - Assert no second autobuild dispatch begins while a first autobuild
        is still in non-terminal lifecycle
- [ ] Correlation threading test (Group I @data-integrity):
      - Drive build to terminal
      - Assert every published lifecycle event for that build carries the
        same correlation_id
- [ ] Calibration priors snapshot test (Group I @data-integrity):
      - Capture snapshot at build start
      - Mutate operator's calibration history mid-run
      - Assert later stages still use the priors snapshot from build start
- [ ] First-wins idempotency test (Group D + Group I @concurrency):
      - Build paused at a checkpoint
      - Inject two simultaneous approval responses with different decisions
      - Assert exactly one decision applied, no double-resume
- [ ] Constitutional misconfigured-prompt test (Group E @security @regression):
      - Stub the supervisor's reasoning model with a configuration that
        emits an auto-approve directive at PR-review
      - Assert `ConstitutionalGuard` still refuses; build remains paused
- [ ] Specialist override-claim test (Group E @security):
      - Specialist returns a claim asserting override of PR-review rule
      - Assert claim is ignored at gating; build pauses for mandatory human
- [ ] Worktree confinement test (Group E @security):
      - Subprocess dispatch attempts to write outside the worktree
      - Assert refused; no path outside allowlist writable
- [ ] All modified files pass project-configured lint/format checks with
      zero errors

## Implementation Notes

This is the most comprehensive integration test in Wave 5 — it covers all
the cross-cutting invariants that no single Wave 2/3 unit test can assert.
The test harness reuses the `greenfield_brief_pipeline` fixture from
TASK-MAG7-012, parameterised with multi-feature and concurrent-build
variations.

The constitutional misconfigured-prompt test is the canary for ADR-ARCH-026
belt-and-braces: it deliberately breaks one layer (prompt) and asserts the
other (executor guard) still holds. Loss of this test passing is a
constitutional regression.

The first-wins test exercises the full FEAT-FORGE-004 idempotency path
through the supervisor — this is the integration-level companion to the
unit-level idempotency tests in FEAT-FORGE-004.

## Test Execution Log

[Automatically populated by /task-work]

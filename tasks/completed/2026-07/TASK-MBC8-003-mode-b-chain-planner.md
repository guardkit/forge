---
id: TASK-MBC8-003
title: Implement ModeBChainPlanner that refuses upstream Mode A stages
task_type: feature
status: completed
priority: high
created: 2026-04-27 00:00:00+00:00
updated: 2026-04-27 00:00:00+00:00
parent_review: TASK-REV-MBC8
feature_id: FEAT-FORGE-008
wave: 2
implementation_mode: task-work
complexity: 5
dependencies:
- TASK-MBC8-001
- TASK-MBC8-002
tags:
- planner
- mode-b
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
  started_at: '2026-04-27T17:59:46.688527'
  last_updated: '2026-04-27T18:14:19.706147'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-27T17:59:46.688527'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
reconciled: "2026-07-11 WS3-S8 sweep — TASK-MBC8-003: feature-rollup completed"
---

# Task: Implement ModeBChainPlanner that refuses upstream Mode A stages

## Description

Add `ModeBChainPlanner` — a pure-function planner that takes the build's
recorded stage history and returns the next permitted stage in the Mode B
chain. The planner enforces the security boundary in FEAT-FORGE-008 Group J:
even if a context manifest references `/system-arch` or `/system-design`, no
Mode B build dispatches them. This guard fires at the planning layer and is
the only secure layer (executor-side guards run later and cannot put stages
back into the chain).

The planner is the Mode B counterpart to MAG7's `PerFeatureLoopSequencer`
(TASK-MAG7-005) and reuses its single-feature-only contract (ASSUM-006).

## Acceptance Criteria

- [ ] `forge.pipeline.mode_b_planner` module exposes `ModeBChainPlanner`
      class (or `plan_next_stage` function) with signature
      `(build: Build, history: Sequence[StageEntry]) -> ModeBPlan`
      where `ModeBPlan` exposes `permitted_stages: frozenset[StageClass]` and
      `next_stage: StageClass | None`
- [ ] Planner returns `next_stage = FEATURE_SPEC` when history is empty
- [ ] Planner returns `next_stage = FEATURE_PLAN` only after a `FEATURE_SPEC`
      entry with `status=approved` is recorded
- [ ] Planner returns `next_stage = AUTOBUILD` only after `FEATURE_PLAN` is
      approved (Group B Scenario Outline rows verbatim)
- [ ] Planner returns `next_stage = PULL_REQUEST_REVIEW` only after
      `AUTOBUILD` is approved AND the autobuild produced a non-empty diff
      against the base branch (delegated to TASK-MBC8-006 for the no-diff
      branch — this planner just returns `None` for the no-diff case so the
      terminal handler can decide)
- [ ] Planner refuses to issue a dispatch for any stage in
      `MODE_B_FORBIDDEN_STAGES`; if such a stage is requested via a manifest,
      planner raises `ModeBoundaryViolation` with the stage name and a
      reference to ASSUM-013
- [ ] Planner returns `permitted_stages` as a `frozenset` reflecting only the
      Mode B chain; `Supervisor.next_turn` uses this to scope the dispatch
      switch in TASK-MBC8-008
- [ ] Hard-stop on `FEATURE_SPEC` (Group C negative case) returns
      `next_stage = None` with a recorded rationale; planner does not advance
      the build
- [ ] Empty `FEATURE_SPEC` artefacts (Group B boundary case) returns
      `next_stage = None` and the planner emits a `MissingSpecArtefacts`
      diagnostic that the Supervisor records as a flag-for-review with
      missing-spec rationale
- [ ] Unit tests cover all 12 Mode B Group A/B/C scenarios from the feature
      file (use scenario titles as test names where reasonable)
- [ ] All modified files pass project-configured lint/format checks with zero
      errors

## Implementation Notes

Keep the planner stateless — every call takes `(build, history)` and returns
the next decision. Persisted state lives in `Build` and `StageEntry` already.

The `ModeBoundaryViolation` exception is a planning-layer guard. It must
distinguish from a generic `StageOrderingError` so callers can surface a
security audit message rather than a generic ordering error.

Coordinate with TASK-MBC8-006 (no-diff terminal handler): this planner does
*not* try to introspect autobuild output; it returns `None` and lets the
terminal handler decide between PR creation and no-op terminal.

## Test Execution Log

[Automatically populated by /task-work]

---
id: TASK-MAG7-011
title: Wire CLI steering injection (cancel, skip, mid-flight directive)
task_type: feature
status: completed
priority: high
created: 2026-04-25 00:00:00+00:00
updated: 2026-04-25 00:00:00+00:00
parent_review: TASK-REV-MAG7
feature_id: FEAT-FORGE-007
wave: 4
implementation_mode: task-work
complexity: 5
dependencies:
- TASK-MAG7-004
- TASK-MAG7-009
- TASK-MAG7-010
tags:
- cli
- steering
- cancel
- skip
- directives
- feat-forge-007
consumer_context:
- task: TASK-MAG7-004
  consumes: constitutional_decision
  framework: Python forge.pipeline.constitutional_guard
  driver: ConstitutionalGuard
  format_note: Calls veto_skip to refuse skip directive on PR-review stage
- task: TASK-MAG7-009
  consumes: autobuild_async_task_id
  framework: Python deepagents middleware
  driver: update_async_task / cancel_async_task
  format_note: Uses task_id from dispatch_autobuild_async to inject directives or
    cancel running autobuild
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-CBDE
  base_branch: main
  started_at: '2026-04-26T19:29:44.096202'
  last_updated: '2026-04-26T19:42:52.600441'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-26T19:29:44.096202'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
reconciled: "2026-07-11 WS3-S8 sweep — TASK-MAG7-011: feature-rollup completed"
---

# Task: Wire CLI steering injection

## Description

Wire CLI steering directives (`forge cancel`, `forge skip`, mid-flight
directive injection) to the supervisor's pause/dispatch surface and the
async-subagent state channel. Implements the synthetic-decision mapping
specified in FEAT-FORGE-004 ASSUM-005 (cancel → reject, skip → override) and
honours the constitutional refusal at PR-review.

Covers Group D edge cases (cancel during pause, cancel during autobuild, skip
on non-constitutional stage, mid-flight directive injection) and Group C
@negative @regression (skip refused at PR-review).

## Acceptance Criteria

- [ ] `CliSteeringHandler` class exists at
      `src/forge/pipeline/cli_steering.py`
- [ ] Method `handle_cancel(build_id: str) -> CancelOutcome`:
      - If build is paused at a flagged-for-review checkpoint, resolves as
        synthetic reject with cancel rationale → terminal `CANCELLED`
      - If build is in autobuild, calls `cancel_async_task(task_id)` and
        marks build terminal `CANCELLED` with no PR-creation attempted
      - If build is in any other non-terminal state, transitions to
        `CANCELLED` and refuses to dispatch any further stage
- [ ] Method `handle_skip(build_id: str, stage: StageClass) -> SkipOutcome`:
      - Calls `ConstitutionalGuard.veto_skip(stage)`; if refused, records
        `SKIP_REFUSED_CONSTITUTIONAL` rationale and leaves build paused
      - Otherwise records the stage as `SKIPPED` in `stage_log` and resumes
        at next stage
- [ ] Method `handle_directive(build_id, feature_id, directive_text: str)`:
      - For active autobuild, calls `update_async_task` to append to
        `AutobuildState.pending_directives`
      - Returns immediately; autobuild itself decides when to honour the
        directive (Group D scenario "directive appears as pending")
- [ ] Cancel during pause: pause resolves as synthetic reject (Group D)
- [ ] Skip on non-constitutional stage: stage recorded as skipped, chain
      resumes (Group D)
- [ ] Skip on PR-review: refused, build remains paused, refusal recorded
      (Group C @regression)
- [ ] Unit tests cover all three methods with mocked supervisor + state
      channel + constitutional guard
- [ ] All modified files pass project-configured lint/format checks with
      zero errors

## Implementation Notes

The CLI surface (`forge cancel`, `forge skip`) is owned by FEAT-FORGE-001
(CLI commands) — this task wires those CLI commands to the supervisor by
implementing the handler methods that the CLI calls into.

`update_async_task` and `cancel_async_task` are middleware tools provided
by DeepAgents (per DDR-006); call them directly through the middleware
adapter, not via subprocess.

The synthetic-decision mapping (cancel→reject, skip→override) was specified
once in FEAT-FORGE-004 — do not re-specify it here. This task only owns the
*injection* of those synthetic decisions into the supervisor's pause
resolution surface.

## Test Execution Log

[Automatically populated by /task-work]

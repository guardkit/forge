---
id: TASK-MAG7-010
title: Wire Supervisor.next_turn dispatch loop
task_type: feature
status: completed
priority: high
created: 2026-04-25 00:00:00+00:00
updated: 2026-04-25 00:00:00+00:00
parent_review: TASK-REV-MAG7
feature_id: FEAT-FORGE-007
wave: 4
implementation_mode: task-work
complexity: 7
dependencies:
- TASK-MAG7-003
- TASK-MAG7-004
- TASK-MAG7-005
- TASK-MAG7-007
- TASK-MAG7-008
- TASK-MAG7-009
tags:
- supervisor
- reasoning-loop
- langgraph
- feat-forge-007
consumer_context:
- task: TASK-MAG7-003
  consumes: stage_ordering_decision
  framework: Python forge.pipeline.stage_ordering_guard
  driver: StageOrderingGuard
  format_note: Calls next_dispatchable() to get permitted set; refuses to act outside
    the set
- task: TASK-MAG7-004
  consumes: constitutional_decision
  framework: Python forge.pipeline.constitutional_guard
  driver: ConstitutionalGuard
  format_note: Calls veto_auto_approve / veto_skip on PR-review stage
- task: TASK-MAG7-005
  consumes: per_feature_sequencing_decision
  framework: Python forge.pipeline.per_feature_sequencer
  driver: PerFeatureLoopSequencer
  format_note: Calls may_start_autobuild before dispatching second feature's autobuild
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-CBDE
  base_branch: main
  started_at: '2026-04-26T19:15:17.712403'
  last_updated: '2026-04-26T19:29:42.122093'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-26T19:15:17.712403'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
reconciled: "2026-07-11 WS3-S8 sweep — TASK-MAG7-010: feature-rollup completed"
---

# Task: Wire Supervisor.next_turn dispatch loop

## Description

The supervisor's reasoning-loop turn function. On each turn for a build:
queries `StageOrderingGuard.next_dispatchable` to get the permitted set,
asks the reasoning model which permitted dispatch to execute, applies
`PerFeatureLoopSequencer` and `ConstitutionalGuard` as additional gates,
invokes the appropriate dispatcher (specialist / subprocess / autobuild
async), records the outcome via FEAT-FORGE-001's state machine, and returns
control to the supervisor's outer loop.

Covers Group A key examples (full chain to PR-awaiting-review, async-subagent
dispatch, flag-resume cycle) and Group F concurrency (supervisor dispatches
second build's stage during first build's autobuild).

## Acceptance Criteria

- [ ] `Supervisor.next_turn(build_id: str) -> TurnOutcome` method exists at
      `src/forge/pipeline/supervisor.py`
- [ ] Reads current build state from FEAT-FORGE-001's state machine
- [ ] Queries `StageOrderingGuard.next_dispatchable` for permitted stages
- [ ] If permitted set is empty and build is in non-terminal state, returns
      `TurnOutcome.WAITING` (e.g. waiting on approval response)
- [ ] If permitted set non-empty, presents the set to the reasoning model
      with the per-stage dispatch context (forward-propagation hints from
      TASK-MAG7-002) and parses the chosen dispatch
- [ ] Refuses to act on any reasoning-model choice outside the permitted set
      (executor-layer enforcement; logs structured warning)
- [ ] For `AUTOBUILD` choices, calls `PerFeatureLoopSequencer.may_start_autobuild`
      first; if False, returns `TurnOutcome.WAITING_PRIOR_AUTOBUILD`
- [ ] For `PULL_REQUEST_REVIEW` gate decisions, applies `ConstitutionalGuard`
      veto_auto_approve before recording any auto-approve decision
- [ ] Routes the dispatch to the appropriate dispatcher:
      `PRODUCT_OWNER`/`ARCHITECT` → specialist,
      `SYSTEM_ARCH`/`SYSTEM_DESIGN`/`FEATURE_SPEC`/`FEATURE_PLAN` → subprocess,
      `AUTOBUILD` → autobuild_async,
      `PULL_REQUEST_REVIEW` → gate decision via FEAT-FORGE-004
- [ ] Records every turn's outcome in `stage_log` with full rationale
- [ ] Concurrent builds: each `build_id` gets its own independent
      `next_turn` invocation; no shared mutable state across builds
- [ ] Unit tests with mocked guards + dispatchers covering: full happy path,
      reasoning-model picks non-permitted stage (refused), per-feature
      sequencing kicks in, constitutional veto fires on PR-review
- [ ] Integration test (mock-substrate): two concurrent builds, supervisor
      dispatches both without cross-talk
- [ ] All modified files pass project-configured lint/format checks with
      zero errors

## Implementation Notes

This is the largest piece of net-new code in FEAT-FORGE-007 — but it is
mostly orchestration over the guards and dispatchers from Waves 2 and 3.
Keep it I/O-thin: it owns no state, only references to the substrate
adapters and dispatchers it composes.

The reasoning-model prompt for this loop must enumerate the eight stage
classes (TASK-MAG7-001) and the forward-propagation hints (TASK-MAG7-002)
in the GUARDRAILS section, so the model has the full taxonomy in view. The
ADR-ARCH-026 belt-and-braces invariant is preserved because the guards
re-check every choice independently of prompt content.

Approval-response handling (Group A "flagged-for-review checkpoint pauses
the build") is delegated to FEAT-FORGE-004's approval round-trip; the
supervisor's `next_turn` is invoked again once an approval response resumes
the build. Idempotent first-wins (Group D "duplicate approval response is
ignored") is also FEAT-FORGE-004's responsibility.

## Test Execution Log

[Automatically populated by /task-work]

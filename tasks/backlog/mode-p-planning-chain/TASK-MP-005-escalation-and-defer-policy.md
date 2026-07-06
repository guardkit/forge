---
id: TASK-MP-005
title: Escalation, wait ceilings, and defer-cap semantics (durable, clock-injected)
task_type: feature
status: in_review
parent_review: TASK-REV-83E4
feature_id: FEAT-3ED2
feature_ref: FEAT-SPL-002
wave: 4
implementation_mode: task-work
complexity: 6
estimated_minutes: 80
dependencies:
- TASK-MP-004B
- TASK-MP-001
tags:
- mode-p
- escalation
- df-009
autobuild_state:
  current_turn: 1
  max_turns: 5
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-3ED2
  base_branch: main
  started_at: '2026-07-06T13:40:59.204771'
  last_updated: '2026-07-06T13:51:33.911650'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-07-06T13:40:59.204771'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# TASK-MP-005 — Escalation, wait ceilings, and defer-cap semantics

## Description

The planning checkpoint's two-phase wait policy — the piece that deliberately
diverges from the build gate (whose ceiling cancels with REASON_MAX_WAIT). Phase 1
waits `originator_wait_seconds` on the originator; on expiry it **durably
re-targets** `expected_approver` to `PlanningConfig.escalation_approver` (row
update + `escalated_at` + planning_run_events entry BEFORE the re-publish — RT-04),
re-publishes with incremented attempt, then phase 2 waits
`escalated_wait_seconds`; on expiry -> TIMED_OUT terminal, never approved.
Defer: `defer_count < defer_cap` -> new round (durable increment); `== cap` ->
escalate instead. All thresholds evaluated by an **injected clock over durable
wall-clock anchors** (`paused_at`/`escalated_at`) so restarts neither reset nor
double-fire windows.

## BDD Scenarios

- "A checkpoint wait just inside the escalation threshold does not escalate"
- "A checkpoint wait reaching the escalation threshold escalates to the escalation approver"
- "An escalated approval that reaches its own ceiling cancels the run as timed out"
- "A run deferred at the checkpoint up to the cap escalates instead of another round"
- "An approval that races the escalation threshold resolves to a single outcome"
- "A daemon restart after an escalation re-arms the checkpoint to the escalation approver" (durable-state half; sweep is TASK-MP-009)

## Files

- Creates: `src/forge/planning/escalation.py`
- Modifies: `src/forge/planning/checkpoint.py` (wait loop delegates to the policy; injected clock parameter — GateCheckDeps.clock precedent, wrappers.py:384)
- Tests: `tests/forge/planning/test_escalation.py`

## Acceptance Criteria

- [ ] Injected fake clock at threshold-minus-epsilon -> run still PAUSED awaiting the originator, zero escalations recorded
- [ ] At the threshold -> `expected_approver` becomes `escalation_approver`, `escalated_at` stamped, planning_run_events entry written, exactly one re-targeted request published (attempt incremented), run remains PAUSED — the row update happens BEFORE the publish (call-order assertion)
- [ ] Escalated ceiling expiry -> TIMED_OUT terminal; an approve-shaped response injected after expiry is refused (never-approved predicate, DF-009)
- [ ] `defer_count == defer_cap` (3) + one more defer -> escalation, not another round; defer_count increments are durable (visible to a second store instance)
- [ ] Race: approve-transition and escalate-transition fired against the same row -> exactly one CAS winner (consumes TASK-MP-002's affected-rows primitive), one transition recorded, the loser refused cleanly
- [ ] Thresholds computed from durable `paused_at`/`escalated_at` + injected clock: constructing the policy against a row with an old `paused_at` fires escalation immediately (no reset-on-restart); no real sleeps > 0.1s anywhere in the suite (test-duration predicate)
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Test Requirements

- Unit with fake clock + fake publisher; zero wall-clock waits.

## Implementation Notes

- Single-coroutine ownership: one coroutine per run owns both the timed await and
  the escalation re-publish (ARCH-006) — the CAS transition is the tiebreaker.
- Single escalation hop in v1 (panel amendment to ASSUM-004).
- Config values come from TASK-MP-001's PlanningConfig; do not read ApprovalConfig.

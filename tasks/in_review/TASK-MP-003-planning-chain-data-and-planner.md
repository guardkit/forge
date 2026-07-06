---
id: TASK-MP-003
title: Planning chain data + pure-function Mode P planner
task_type: feature
status: in_review
parent_review: TASK-REV-83E4
feature_id: FEAT-3ED2
feature_ref: FEAT-SPL-002
wave: 1
implementation_mode: task-work
complexity: 5
estimated_minutes: 75
dependencies: []
tags:
- mode-p
- planner
- boundary
autobuild_state:
  current_turn: 1
  max_turns: 5
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-3ED2
  base_branch: main
  started_at: '2026-07-06T12:58:39.986880'
  last_updated: '2026-07-06T13:08:34.570752'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-07-06T12:58:39.986880'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# TASK-MP-003 — Planning chain data + pure-function Mode P planner

## Description

The Mode P decision rule: a deterministic pure function over the planning run's
recorded history (mode_b_planner.py shape — stateless, no I/O, no model calls).
Enforcement locus for the stage boundary is HERE, in the planning package —
`mode_chains_data.py` stays **byte-identical** (panel amendment to ASSUM-009), which
keeps the "Mode B forbidden-stage logic untouched" hard constraint mechanically
checkable.

## BDD Scenarios

- "The product owner stage is dispatched to the local specialist and coach-scored" (planner decision half)
- "Planning runs never consult a reasoning model to advance the chain"
- "A product owner dispatch failure records a failed run rather than a silent stall" (decision mapping)
- "A planning run never advances into build stages"
- "The product owner stage is permitted in planning runs while feature builds still forbid it"

## Files

- Creates: `src/forge/planning/chain_data.py` (`PLANNING_CHAIN` step tuple: PRODUCT_OWNER -> PRODUCT_DOCS_CHECKPOINT -> HANDOFF, wrapping `StageClass.PRODUCT_OWNER` for the dispatchable stage; `PLANNING_FORBIDDEN_STAGES: frozenset` covering all build stages — everything in MODE_A_CHAIN except PRODUCT_OWNER, plus AUTOBUILD/PULL_REQUEST_REVIEW explicitly; `PRODUCT_DOCS_STAGE_LABEL = "product_docs"`), `src/forge/planning/planner.py` (`plan_next_step(history: Sequence[PlanningEvent]) -> PlanningDecision`)
- Tests: `tests/forge/planning/test_planner.py`, `tests/forge/planning/test_chain_data.py`

## Acceptance Criteria

- [ ] `planner.py` imports no model/LLM/dispatch/NATS modules — enforced by an AST-level import-allowlist test (stdlib + planning-package + stage_taxonomy only); this is the deterministic no-reasoning-model predicate
- [ ] `plan_next_step` is total and pure: every history shape yields exactly one `PlanningDecision` (DispatchProductOwner / PauseAtCheckpoint / ExecuteHandoff / Fail(reason) / BoundaryViolation); a repeated-call test proves same-history-in -> same-decision-out
- [ ] History containing any `PLANNING_FORBIDDEN_STAGES` entry -> `BoundaryViolation` decision (never advance), designed to be recorded as a boundary error by the caller
- [ ] `MODE_B_FORBIDDEN_STAGES` still contains PRODUCT_OWNER and `src/forge/pipeline/mode_chains_data.py` is byte-identical to main (git-diff predicate test or hash comparison) while `PLANNING_CHAIN` contains PRODUCT_OWNER — both boundary scenarios hold by construction
- [ ] Dispatch-failure outcomes (StageDispatchOutcome.ERROR / failed status in history) map to `Fail(reason)` with the dispatch failure as the reason
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Test Requirements

- Unit only. History entries satisfied by simple dataclasses/SimpleNamespace (the
  planner defines its own narrow `PlanningEvent` Protocol, runtime_checkable —
  mirror `StageEntry` in mode_b_planner.py:133).

## Implementation Notes

- Mirror the mode_b_planner.py module shape (docstring contract, frozen dataclass
  decision types, module-level functional shortcut) — reviewers know that shape.
- Decision types carry rationale strings for planning_run_events details_json.
- Do NOT import from `mode_chains_data.py` anything you then re-export mutated;
  reference `StageClass` from stage_taxonomy directly.

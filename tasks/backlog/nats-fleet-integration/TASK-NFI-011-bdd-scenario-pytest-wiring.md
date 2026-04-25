---
id: TASK-NFI-011
title: "BDD @smoke + @key-example pytest wiring (33 scenarios \u2192 tagged tests)"
task_type: testing
status: in_review
priority: normal
created: 2026-04-24 00:00:00+00:00
updated: 2026-04-24 00:00:00+00:00
parent_review: TASK-REV-NF20
feature_id: FEAT-FORGE-002
wave: 5
implementation_mode: task-work
complexity: 4
dependencies:
- TASK-NFI-004
- TASK-NFI-005
- TASK-NFI-006
- TASK-NFI-007
- TASK-NFI-008
- TASK-NFI-009
tags:
- testing
- bdd
- scenarios
- r2-oracle
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002
  base_branch: main
  started_at: '2026-04-25T14:49:38.888615'
  last_updated: '2026-04-25T15:07:01.105808'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-25T14:49:38.888615'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# Task: BDD @smoke + @key-example pytest wiring

## Description

Wire the 33 Gherkin scenarios in
`features/nats-fleet-integration/nats-fleet-integration.feature` to
executable pytest tests. Step 11 of `/feature-plan` tags individual
scenarios with `@task:<TASK-ID>` pointing back to the owning subtask;
this task owns the **scaffolding and execution path** that makes those
tagged scenarios runnable by the R2 BDD oracle during `/task-work`
Phase 4.

Priority coverage:

1. **3 @smoke scenarios** (registration, heartbeat, stage-complete) — MUST run
   green at feature-complete
2. **7 @key-example scenarios** — primary acceptance surface
3. **Other scenarios** — run green where the corresponding subtask is done

## Acceptance Criteria

- [ ] `tests/bdd/` directory with `conftest.py` loading Gherkin fixtures from `features/nats-fleet-integration/nats-fleet-integration.feature`
- [ ] `pytest-bdd` dependency added to `pyproject.toml` dev-extras (or equivalent)
- [ ] All 3 `@smoke` scenarios executable and passing
- [ ] All 7 `@key-example` scenarios executable and passing
- [ ] Each passing scenario is tagged `@task:TASK-NFI-xxx` in the `.feature` file (via Step 11 BDD linker)
- [ ] R2 oracle runs tagged scenarios during `/task-work` Phase 4 for each linked subtask
- [ ] Remaining scenarios (@boundary, @negative, @edge-case, @security) either executable or explicitly marked `@skip` with a follow-up ticket
- [ ] CI runs the full `@smoke` suite on every PR; `@key-example` suite on merge to main

## Implementation Notes

- Use `pytest-bdd` idioms: `@scenarios(...)` decorator + step functions
- Shared fixtures: `nats_client_mock`, `fake_clock`, `discovery_cache`, `pipeline_publisher`, `pipeline_consumer`
- Step functions live in `tests/bdd/steps/` split by group (registration_steps.py, heartbeat_steps.py, etc.)
- Scenario outlines (Group B boundary) parameterise via `pytest.mark.parametrize` at the scenario level

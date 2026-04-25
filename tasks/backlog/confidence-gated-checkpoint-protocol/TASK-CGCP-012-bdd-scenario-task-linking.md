---
id: TASK-CGCP-012
title: "BDD scenario\u2192task linking and pytest-bdd wiring (R2 oracle activation)"
task_type: testing
status: in_review
priority: high
created: 2026-04-25 00:00:00+00:00
updated: 2026-04-25 00:00:00+00:00
parent_review: TASK-REV-CG44
feature_id: FEAT-FORGE-004
wave: 5
implementation_mode: direct
complexity: 3
dependencies:
- TASK-CGCP-010
tags:
- testing
- bdd
- gherkin
- pytest-bdd
- r2-oracle
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-004
  base_branch: main
  started_at: '2026-04-25T18:43:53.518992'
  last_updated: '2026-04-25T18:55:38.764143'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-25T18:43:53.518992'
    player_summary: "1) Fixed the Gherkin parse-blocker on line 176: the informal\
      \ `Or the decision should record no threshold value at all` was rewritten as\
      \ `And the decision should record no threshold value at all` paired with a tightened\
      \ `Then the decision should be marked as a constitutional override with no threshold\
      \ applied` \u2014 both assertions are now legal Gherkin and `feature-plan-bdd-link\
      \ prepare` parses cleanly (status=ready, scenarios_to_match=32). 2) Ran `feature-plan-bdd-link\
      \ prepare` against the .feature"
    player_success: true
    coach_success: true
---

# Task: BDD scenario→task linking and pytest-bdd wiring (R2 oracle activation)

## Description

Activate the R2 task-level BDD oracle by adding `@task:<TASK-ID>` tags to
each scenario in
`features/confidence-gated-checkpoint-protocol/confidence-gated-checkpoint-protocol.feature`,
mapping each of the 32 scenarios to one of the 12 implementation tasks
(TASK-CGCP-001 through TASK-CGCP-012). The mapping is run by the
`bdd-linker` subagent and applied via `feature-plan-bdd-link apply` per
the /feature-plan Step 11 protocol.

The R2 oracle (TASK-BDD-E8954) silently skips every scenario when no
`@task:` tag is present — adding tags is the single switch that activates
it. Once tagged, `/task-work` Phase 4 runs the matching scenarios for
each task as part of the quality gate.

This task also delivers the pytest-bdd glue:

- `tests/bdd/conftest.py` — fixtures shared across scenarios (in-memory
  NATS double, temp SQLite, deterministic reasoning-model double)
- `tests/bdd/test_confidence_gated_checkpoint_protocol.py` — `@scenarios()`
  loader pointing at the `.feature` file and step definitions for the
  vocabulary used in the scenarios

## Acceptance Criteria

- [ ] **Prerequisite fix**: line 176 of `confidence-gated-checkpoint-protocol.feature` uses an informal `Or` keyword that breaks Gherkin parsing. Replace the two-line construct in scenario "A mandatory-human-approval decision must not masquerade as a threshold-based approval" with valid Gherkin (e.g. `And` joining two assertions, or a composite Then-clause). Verify `feature-plan-bdd-link prepare` parses the file without error before continuing
- [ ] Run `feature-plan-bdd-link prepare` against `confidence-gated-checkpoint-protocol.feature` — produces a request payload with all 32 scenarios listed
- [ ] Invoke the `bdd-linker` subagent on the request payload
- [ ] Run `feature-plan-bdd-link apply` against the response — `.feature` file is rewritten with `@task:<TASK-ID>` tags on every matched scenario; output line `[Step 11] linked N scenario(s) ...` is captured
- [ ] All 32 scenarios in the `.feature` file carry exactly one `@task:` tag (any duplicates are a regression)
- [ ] Each `@task:` tag refers to one of TASK-CGCP-001 through TASK-CGCP-012
- [ ] Coverage check: every task ID appears as a `@task:` tag at least once OR has a documented justification in the implementation guide for why it has no scenario coverage (e.g. pure structural tasks like TASK-CGCP-001/002/003)
- [ ] `tests/bdd/test_confidence_gated_checkpoint_protocol.py` runs via `pytest tests/bdd/` and exercises the tagged scenarios; in-memory NATS + temp SQLite + deterministic reasoning-model double
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Implementation Notes

- The bdd-linker is a subagent invoked via the `Task` tool — NOT a Python module imported inline. Step 11 of /feature-plan documents the exact invocation shape; mirror that
- pytest-bdd glue mirrors the FEAT-FORGE-002 BDD wiring in TASK-NFI-011
- A handful of scenarios are pure data-shape contracts (e.g. "criterion-breakdown values at the permitted extremes are accepted") — these tag onto TASK-CGCP-001 (models) or TASK-CGCP-005 (post-conditions) depending on where the validator lives
- Re-running `feature-plan-bdd-link prepare` after activation is idempotent: it detects tags already present and reports `status=skipped, reason=all_tagged`

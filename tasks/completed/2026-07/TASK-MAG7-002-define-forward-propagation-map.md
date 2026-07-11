---
id: TASK-MAG7-002
title: Define forward-propagation contract map
task_type: declarative
status: completed
priority: high
created: 2026-04-25 00:00:00+00:00
updated: 2026-04-25 00:00:00+00:00
parent_review: TASK-REV-MAG7
feature_id: FEAT-FORGE-007
wave: 1
implementation_mode: direct
complexity: 2
dependencies: []
tags:
- forward-propagation
- declarative
- context-flags
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
  started_at: '2026-04-26T18:34:29.575232'
  last_updated: '2026-04-26T18:40:20.100084'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-26T18:34:29.575232'
    player_summary: "Implemented forge.pipeline.forward_propagation as a side-effect-free\
      \ declarative module. Defines a frozen Pydantic ContextRecipe(BaseModel) with\
      \ the four required fields (producer_stage: StageClass, artefact_kind: Literal['text','path','path-list'],\
      \ context_flag: str, description: str) and PROPAGATION_CONTRACT: dict[StageClass,\
      \ ContextRecipe] with exactly seven entries \u2014 one for each non-product-owner\
      \ stage \u2014 matching the task brief verbatim (ARCHITECT, SYSTEM_ARCH, SYSTEM_DESIGN,\
      \ FEATURE_SPEC, "
    player_success: true
    coach_success: true
reconciled: "2026-07-11 WS3-S8 sweep — TASK-MAG7-002: feature-rollup completed"
---

# Task: Define forward-propagation contract map

## Description

Define the producer-to-consumer artefact handshake for each of the seven
non-product-owner stages. This is what tells the
`ForwardContextBuilder` (TASK-MAG7-006) which `stage_log` artefact_path values
to thread into the next stage's `--context` flags.

Encodes Group A scenarios: "The product-owner output is supplied as input to
the architect delegation" and "Architecture outputs are supplied as context for
system design".

## Acceptance Criteria

- [ ] `forge.pipeline.forward_propagation` module exists at
      `src/forge/pipeline/forward_propagation.py`
- [ ] Exports `ContextRecipe` Pydantic model with fields:
      `producer_stage: StageClass`, `artefact_kind: Literal["text", "path", "path-list"]`,
      `context_flag: str` (e.g. `--context`), `description: str`
- [ ] Exports `PROPAGATION_CONTRACT: dict[StageClass, ContextRecipe]` with
      exactly seven entries, one per non-product-owner stage:
      - `ARCHITECT` ← product-owner approved charter (text)
      - `SYSTEM_ARCH` ← architect approved output (text)
      - `SYSTEM_DESIGN` ← system-arch artefact paths (path-list)
      - `FEATURE_SPEC` ← system-design feature catalogue entry (text)
      - `FEATURE_PLAN` ← feature-spec artefact path (path)
      - `AUTOBUILD` ← feature-plan artefact path (path)
      - `PULL_REQUEST_REVIEW` ← autobuild branch ref + commit summary (text)
- [ ] Module passes a self-validation check at import: every key is reachable
      from `PRODUCT_OWNER` via the `STAGE_PREREQUISITES` chain from
      TASK-MAG7-001
- [ ] All modified files pass project-configured lint/format checks with
      zero errors

## Implementation Notes

The seven entries mirror the seven prerequisite rows from TASK-MAG7-001 — by
construction every approved-prerequisite produces an artefact that is consumed
by the immediate-downstream stage.

This module imports from `forge.pipeline.stage_taxonomy` (TASK-MAG7-001) but
nothing else from `forge.pipeline.*`. Keep it side-effect free so test modules
can import it without bringing up the substrate.

## Test Execution Log

[Automatically populated by /task-work]

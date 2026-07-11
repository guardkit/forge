---
id: TASK-MAG7-007
title: Wire dispatch_specialist_stage for product-owner and architect
task_type: feature
status: completed
priority: high
created: 2026-04-25 00:00:00+00:00
updated: 2026-04-25 00:00:00+00:00
parent_review: TASK-REV-MAG7
feature_id: FEAT-FORGE-007
wave: 3
implementation_mode: task-work
complexity: 4
dependencies:
- TASK-MAG7-001
- TASK-MAG7-006
tags:
- dispatcher
- specialist
- feat-forge-003
- feat-forge-007
consumer_context:
- task: TASK-MAG7-006
  consumes: forward_context
  framework: Python forge.pipeline.forward_context_builder
  driver: Internal call
  format_note: Receives list[ContextEntry] from ForwardContextBuilder.build_for()
    and threads into specialist dispatch payload
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-CBDE
  base_branch: main
  started_at: '2026-04-26T19:00:26.626158'
  last_updated: '2026-04-26T19:13:50.553297'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-26T19:00:26.626158'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
reconciled: "2026-07-11 WS3-S8 sweep — TASK-MAG7-007: feature-rollup completed"
---

# Task: Wire dispatch_specialist_stage for product-owner and architect

## Description

Compose FEAT-FORGE-003's specialist capability dispatch with the
`ForwardContextBuilder` to dispatch the two specialist stages
(`PRODUCT_OWNER`, `ARCHITECT`). The dispatcher records the dispatch in
`stage_log`, awaits the correlation-keyed reply, parses the Coach score, and
produces a stage-complete record consumable by the gating layer
(FEAT-FORGE-004).

Covers Group A: "A greenfield brief drives a full pipeline run" (specialist
half) and "The product-owner output is supplied as input to the architect
delegation".

## Acceptance Criteria

- [ ] `dispatch_specialist_stage(stage: StageClass, build_id: str, ...) -> StageDispatchResult`
      function exists at `src/forge/pipeline/dispatchers/specialist.py`
- [ ] Refuses any stage that is not in `{PRODUCT_OWNER, ARCHITECT}`
      (raises `ValueError` — programming error, not runtime)
- [ ] Calls `ForwardContextBuilder.build_for` to assemble the dispatch
      payload context
- [ ] Delegates to FEAT-FORGE-003's specialist-dispatch tool with the
      capability matching the stage (`product_owner_specialist`,
      `architect_specialist`)
- [ ] Threads the build's `correlation_id` through the dispatch envelope
      (Group I @data-integrity scenario)
- [ ] Records the dispatch as a `stage_log` entry on submit and updates on
      reply with the parsed Coach score and detection findings
- [ ] On degraded specialist (no healthy specialist on cache), returns a
      `StageDispatchResult.DEGRADED` outcome — gating layer maps to
      flag-for-review (Group C @negative scenario "no product-owner specialist")
- [ ] Unit test mocking FEAT-FORGE-003 dispatch surface: success path,
      degraded path, soft-timeout path (retry-with-context)
- [ ] All modified files pass project-configured lint/format checks with
      zero errors

## Implementation Notes

This dispatcher is a thin composition layer — it does not re-implement
specialist resolution, capability matching, or correlation handling. All of
that is owned by FEAT-FORGE-003. The dispatcher's job is to stitch the
forward-propagated context onto the specialist call and to write the
`stage_log` entry that downstream guards read.

The retry-with-context behaviour on soft failure (FEAT-FORGE-003 ASSUM-005,
"reasoning-model-driven retry; no fixed max-retry count") is invoked at the
supervisor layer, not here — this dispatcher returns the structured result
and lets the supervisor decide.

## Test Execution Log

[Automatically populated by /task-work]

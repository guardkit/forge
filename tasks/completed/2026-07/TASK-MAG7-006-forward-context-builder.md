---
id: TASK-MAG7-006
title: Implement ForwardContextBuilder
task_type: feature
status: completed
priority: high
created: 2026-04-25 00:00:00+00:00
updated: 2026-04-25 00:00:00+00:00
parent_review: TASK-REV-MAG7
feature_id: FEAT-FORGE-007
wave: 3
implementation_mode: task-work
complexity: 5
dependencies:
- TASK-MAG7-001
- TASK-MAG7-002
- TASK-MAG7-003
tags:
- context-builder
- forward-propagation
- feat-forge-007
consumer_context:
- task: TASK-MAG7-002
  consumes: forward_propagation_map
  framework: Python forge.pipeline.forward_propagation
  driver: Pydantic ContextRecipe
  format_note: Reads PROPAGATION_CONTRACT to know which stage_log artefact_path values
    to thread into --context flags for each stage
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-CBDE
  base_branch: main
  started_at: '2026-04-26T18:50:36.051497'
  last_updated: '2026-04-26T19:00:24.322143'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-26T18:50:36.051497'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
reconciled: "2026-07-11 WS3-S8 sweep — TASK-MAG7-006: feature-rollup completed"
---

# Task: Implement ForwardContextBuilder

## Description

Reads the prior approved stage's artefact paths from `stage_log` and assembles
the `--context` flag list that the next stage's dispatcher will pass to its
underlying tool (specialist dispatch or GuardKit subprocess). Implements
forward propagation per the `PROPAGATION_CONTRACT` from TASK-MAG7-002.

Covers Group A scenarios: "The product-owner output is supplied as input to
the architect delegation" and "Architecture outputs are supplied as context
for system design".

## Acceptance Criteria

- [ ] `ForwardContextBuilder` class exists at
      `src/forge/pipeline/forward_context_builder.py`
- [ ] Method `build_for(stage: StageClass, build_id: str, feature_id: str | None) -> list[ContextEntry]`
      returns the context entries (path or text) to thread into the next
      stage's dispatch
- [ ] Reads `stage_log` rows scoped by `build_id`, optionally `feature_id`,
      with `gate_decision='approved'` only — never reads in-progress or
      flagged-for-review entries
- [ ] Refuses to return any entry whose underlying artefact path falls
      outside the build's worktree allowlist (defence-in-depth alongside
      FEAT-FORGE-005)
- [ ] For per-feature stages, scopes the lookup to that feature's prior
      stage entry only
- [ ] Unit tests cover all seven `PROPAGATION_CONTRACT` rows
- [ ] Unit test: in-progress prior stage → empty context (must wait until
      approved)
- [ ] Unit test: artefact path outside allowlist is filtered out with a
      structured warning
- [ ] All modified files pass project-configured lint/format checks with
      zero errors

## Implementation Notes

This builder is the *only* place that crosses the boundary from "the build's
recorded history" to "what gets passed to a downstream subprocess". Keeping
the read-side filtering centralised here (approved-only, allowlist-checked)
is what mitigates Risk R-5 (forward-propagation context builder leaks
unapproved or stale artefacts).

Inject a `StageLogReader` Protocol (same one as TASK-MAG7-003) and a
`WorktreeAllowlist` Protocol. Both have production implementations in
FEAT-FORGE-001 (SQLite reader) and FEAT-FORGE-005 (allowlist) respectively.

## Seam Tests

```python
"""Seam test: ForwardContextBuilder threads only approved artefacts."""
import pytest


@pytest.mark.seam
@pytest.mark.integration_contract("forward_propagation_map")
def test_forward_context_builder_skips_unapproved():
    """Verify only approved stage entries are threaded forward.

    Contract: PROPAGATION_CONTRACT rows produce context only from
    stage_log entries with gate_decision='approved'.
    Producer: TASK-MAG7-002
    """
    # Test fixture: stage_log has architect entry with gate_decision='flagged'
    # Builder must return empty context for SYSTEM_ARCH stage
    pass  # Implementation in /task-work
```

## Test Execution Log

[Automatically populated by /task-work]

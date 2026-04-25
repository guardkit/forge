---
id: TASK-CGCP-002
title: Add forge.config.approval settings (default_wait, max_wait)
task_type: declarative
status: in_review
priority: high
created: 2026-04-25 00:00:00+00:00
updated: 2026-04-25 00:00:00+00:00
parent_review: TASK-REV-CG44
feature_id: FEAT-FORGE-004
wave: 1
implementation_mode: direct
complexity: 2
dependencies: []
tags:
- config
- pydantic
- declarative
- approval
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-004
  base_branch: main
  started_at: '2026-04-25T17:43:15.758562'
  last_updated: '2026-04-25T17:48:01.361479'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-25T17:43:15.758562'
    player_summary: 'Added ApprovalConfig (Pydantic v2, extra=''forbid'') in src/forge/config/models.py
      with two int fields default_wait_seconds (default 300, ge=0) and max_wait_seconds
      (default 3600, ge=0), plus a model_validator(mode=''after'') that rejects default_wait_seconds
      > max_wait_seconds. Wired approval: ApprovalConfig = Field(default_factory=ApprovalConfig)
      onto ForgeConfig (mirrors the existing fleet/pipeline default_factory pattern).
      Added module-level constants DEFAULT_APPROVAL_WAIT_SECONDS / DEFAULT_APPR'
    player_success: true
    coach_success: true
---

# Task: Add forge.config.approval settings (default_wait, max_wait)

## Description

Extend `forge.config.models` (FEAT-FORGE-001) with an `ApprovalConfig`
sub-model carrying the wait-time settings pinned by
`API-nats-approval-protocol.md §3.1` (default 300s) and §7 (max ~3600s).
Wire `ApprovalConfig` onto `ForgeConfig` so it loads from `forge.yaml`.

Per ASSUM-001 (high) and ASSUM-002 (high), these are the canonical default
and ceiling. ASSUM-003 (medium) — behaviour at the ceiling — is explicitly
deferred to `forge-pipeline-config` and is **out of scope** for this task.
Document the deferral inline.

## Acceptance Criteria

- [ ] `ApprovalConfig` Pydantic v2 model defined with two non-negative integer fields:
  - `default_wait_seconds: int = 300`  (ASSUM-001)
  - `max_wait_seconds: int = 3600`  (ASSUM-002)
- [ ] Validators reject negative values
- [ ] Validator rejects `default_wait_seconds > max_wait_seconds`
- [ ] `ForgeConfig` has an `approval: ApprovalConfig = Field(default_factory=ApprovalConfig)` field
- [ ] `forge.yaml` round-trips through `ForgeConfig.model_validate(...)` with new section
- [ ] Inline comment documents that ASSUM-003 (ceiling fallback semantics) is deferred to `forge-pipeline-config`
- [ ] Module imports nothing from `nats_core`, `nats-py`, or `langgraph`
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Implementation Notes

- Mirror the structure and validator style of `FleetConfig` / `PipelineConfig` from FEAT-FORGE-002
- `default_factory=ApprovalConfig` (not a shared mutable default) — same pattern as existing config sub-models

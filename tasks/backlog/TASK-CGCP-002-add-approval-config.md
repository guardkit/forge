---
id: TASK-CGCP-002
title: 'Add forge.config.approval settings (default_wait, max_wait)'
task_type: declarative
status: backlog
priority: high
created: 2026-04-25T00:00:00Z
updated: 2026-04-25T00:00:00Z
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

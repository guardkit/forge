---
id: TASK-MP-001
title: PlanningConfig section + DF-004 planning model-resolution audit
task_type: feature
status: backlog
parent_review: TASK-REV-83E4
feature_id: FEAT-3ED2
feature_ref: FEAT-SPL-002
wave: 1
implementation_mode: task-work
complexity: 4
estimated_minutes: 60
dependencies: []
tags: [mode-p, config, df-004]
---

# TASK-MP-001 — PlanningConfig section + DF-004 planning model-resolution audit

## Description

Add the minimal planning approval-routing/config surface Mode P needs as a NEW
sibling section in forge config (`ApprovalConfig` is a deliberately closed surface:
`extra="forbid"`, its docstring forbids escalation fields — src/forge/config/models.py:162-236),
plus the DF-004 `fallbacks:[]` audit as a **pure function** so the boot scenario
anchors to a unit seam.

## BDD Scenarios (features/mode-p-planning-chain/mode-p-planning-chain.feature)

- Background (config surface)
- "A cloud fallback in planning model resolution fails the planning audit loudly" (unit half; boot-integration half is TASK-MP-009)
- "A planning request without a target repository hands off to the configured default repository" (config surface; behaviour is TASK-MP-006)

## Files

- Creates: `src/forge/planning/__init__.py`, `src/forge/planning/audit.py`
- Modifies: `src/forge/config/models.py` (new `PlanningConfig`), `src/forge/config/__init__.py` (re-export)
- Tests: `tests/forge/config/test_planning_config.py`, `tests/forge/planning/test_audit.py`

## Acceptance Criteria

- [ ] `PlanningConfig` exists in `src/forge/config/models.py` with `model_config = ConfigDict(extra="forbid")` and exactly these fields: `enabled: bool = False` (intake-is-deliberate), `escalation_approver: str | None = None`, `originator_wait_seconds: int` (ge=0), `escalated_wait_seconds: int` (ge=0), `defer_cap: int = 3` (ge=1), `default_target_repo: str | None = None` (org/name-validated when set, mirroring the wire pattern `^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$`), `target_repo_paths: dict[str, str] = {}` (org/name -> absolute local working-copy path), `terminal: str = "planned-handoff"`, `frontier_enabled: bool = False`, `frontier_timeout_seconds: int`, `model_resolution: PlanningModelResolution` (sub-model: `model: str | None = None`, `fallbacks: list[str] = []`)
- [ ] `ForgeConfig` gains an optional `planning: PlanningConfig` field with `default_factory` (house shape, models.py:488-510); **`ApprovalConfig` is byte-identical to main** (verified by test comparing against its current field set — no new fields, no edits)
- [ ] `audit_planning_model_resolution(config: PlanningConfig) -> PlanningAuditResult` in `src/forge/planning/audit.py` is a pure function (no I/O, no raising): non-empty `fallbacks` -> violation result naming DF-004; empty -> pass; result carries a human-readable reason string
- [ ] Defaults verified by test: `enabled=False`, `frontier_enabled=False`, `defer_cap=3`
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Test Requirements

- Unit tests only, offline: Pydantic validation cases (extra key rejected; invalid
  org/name rejected; negative waits rejected), audit pure-function cases (empty
  fallbacks pass / non-empty fail / missing planning section means Mode P disabled).

## Implementation Notes

- DF-004 (fleet REGISTER): planning model resolution can never silently escalate to
  cloud. The audit must be an **audit, not a Pydantic validator** — a validator
  would brick the whole daemon on violation, contradicting ASSUM-011's "build
  intake unaffected" (DDR-007 soft-fail posture). Boot wiring happens in TASK-MP-009.
- The PO model itself resolves inside specialist-agent; forge's only planning model
  surface is the DF-006 frontier client, but the audit covers the whole
  `model_resolution` block so config drift is caught at the boundary.
- Do NOT touch `ApprovalConfig`, `src/forge/gating/`, or `src/forge/adapters/guardkit/run.py`.

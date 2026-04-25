---
id: TASK-CGCP-001
title: 'Define forge.gating module structure (models + pure-function shell)'
task_type: declarative
status: backlog
priority: high
created: 2026-04-25T00:00:00Z
updated: 2026-04-25T00:00:00Z
parent_review: TASK-REV-CG44
feature_id: FEAT-FORGE-004
wave: 1
implementation_mode: direct
complexity: 3
dependencies: []
tags:
- gating
- pydantic
- declarative
- models
- domain-core
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Task: Define forge.gating module structure (models + pure-function shell)

## Description

Lay down the `forge.gating` Domain Core package per `DM-gating.md §1`. This
task is **declarative** — it produces Pydantic v2 models, enums, and a stub
`evaluate_gate()` that raises `NotImplementedError`. The constitutional
branch (TASK-CGCP-004) and reasoning-model assembly (TASK-CGCP-005) fill in
the body in Wave 2.

Files to create:

- `src/forge/gating/__init__.py` — re-export `GateMode`, `GateDecision`, `evaluate_gate`
- `src/forge/gating/models.py` — Pydantic models, enums, validators

The package must remain **domain-pure**: zero imports from `nats_core`,
`nats-py`, or `langgraph`. Only `pydantic`, `datetime`, `typing`, `enum`,
and standard-library modules are permitted.

## Acceptance Criteria

- [ ] `GateMode` enum defined with four members (`AUTO_APPROVE`, `FLAG_FOR_REVIEW`, `HARD_STOP`, `MANDATORY_HUMAN_APPROVAL`) per `DM-gating.md §1`
- [ ] `PriorReference` Pydantic model with `entity_id`, `group_id` (Literal), `summary`, `relevance_score` fields
- [ ] `DetectionFinding` Pydantic model with `pattern`, `severity` (Literal), `evidence`, `criterion` fields
- [ ] `GateDecision` Pydantic model with all fields per `DM-gating.md §1` — invariants enforced by validators per §6:
  - `mode == MANDATORY_HUMAN_APPROVAL ⇒ auto_approve_override is True OR threshold_applied is None`
  - `coach_score is None ⇒ mode in {FLAG_FOR_REVIEW, HARD_STOP, MANDATORY_HUMAN_APPROVAL}`
  - `criterion_breakdown` values are floats in `[0.0, 1.0]`
- [ ] `CalibrationAdjustment` Pydantic model with all fields per `DM-gating.md §1`
- [ ] `ResponseKind` enum defined with five members per `DM-gating.md §2`
- [ ] `evaluate_gate()` stub function with full keyword-only signature per `DM-gating.md §3` raises `NotImplementedError`
- [ ] Module imports nothing from `nats_core`, `nats-py`, `langgraph`, or `forge.adapters.*`
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Implementation Notes

- Use `pydantic.BaseModel` v2 conventions consistent with `forge/config/models.py`
- Use `Field(..., description="...")` for documented fields
- Use `model_validator(mode="after")` for cross-field invariants
- Follow the re-export `__init__.py` shim pattern used elsewhere in the codebase

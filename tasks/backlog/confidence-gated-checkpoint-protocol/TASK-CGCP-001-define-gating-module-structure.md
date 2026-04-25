---
id: TASK-CGCP-001
title: Define forge.gating module structure (models + pure-function shell)
task_type: declarative
status: in_review
priority: high
created: 2026-04-25 00:00:00+00:00
updated: 2026-04-25 00:00:00+00:00
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
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-004
  base_branch: main
  started_at: '2026-04-25T17:43:15.778641'
  last_updated: '2026-04-25T17:49:57.580645'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-25T17:43:15.778641'
    player_summary: "Added forge.gating.models per DM-gating.md \xA71/\xA72/\xA73/\xA7\
      6. Defined GateMode (4 members) and ResponseKind (5 members) as str-Enum subclasses,\
      \ plus PriorReference, DetectionFinding, GateDecision, CalibrationAdjustment,\
      \ and a minimal placeholder ConstitutionalRule needed only to type the evaluate_gate\
      \ signature (the detailed schema is owned by TASK-CGCP-004). All Pydantic models\
      \ use ConfigDict(extra='forbid') and Field(..., description='...') consistent\
      \ with forge.config.models / forge.discovery.model"
    player_success: true
    coach_success: true
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

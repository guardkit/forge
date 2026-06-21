---
complexity: 4
created: 2026-06-21 18:30:00+00:00
dependencies: []
estimated_minutes: 60
feature_slug: runbook-and-step-persistence
id: TASK-RSP-001
implementation_mode: task-work
parent_feature: FEAT-RSP
parent_review: TASK-REV-RSP-001
priority: high
status: design_approved
tags:
- forge
- persistence
- runbook
- data-model
task_type: declarative
title: Step and Runbook data models
updated: 2026-06-21 18:30:00+00:00
wave: 1
---

# Step and Runbook data models

## TL;DR

Define the frozen-dataclass domain models and the closed status enum for
the Forge output-side loop's runbook, in a **new** module
`src/forge/persistence/repositories/runbook_models.py`. Pure data +
validation — no SQL, no I/O. These types are consumed by the migration's
`CHECK` set (TASK-RSP-002) and by the repository (TASK-RSP-003/004).

## Scope

Create `src/forge/persistence/repositories/runbook_models.py` containing:

- **`StepStatus`** — a `StrEnum` with the closed value set
  `pending`, `running`, `passed`, `failed`, `awaiting_approval`. This is
  the single source of truth for the status vocabulary. Per **ASSUM-001**
  the runbook overall status uses the same value set, so the overall
  status reuses `StepStatus` (no separate enum).
- **`StepResult`** — frozen dataclass (`slots=True`): `exit_code: int`,
  `captured_output: str`, `started_at: datetime`, `completed_at: datetime`
  (the two timestamps per **ASSUM-008**). A step has no result until one
  is recorded (**ASSUM-007**) → `Step.result` is `StepResult | None`.
- **`Step`** — frozen dataclass: `step_type: str`, `params: Mapping[str, Any]`,
  `status: StepStatus`, `sequence_index: int`, `result: StepResult | None = None`.
  `__post_init__` validation: `step_type` must be a non-empty string
  (free-form this phase — no closed enum, **ASSUM-005**).
- **`Runbook`** — frozen dataclass: `runbook_id: str`, `target: str`,
  `steps: tuple[Step, ...]`, `current_step_index: int`, `status: StepStatus`,
  `created_at: datetime`. `__post_init__` validation:
  - at least one step (**ASSUM-002**) — empty `steps` raises;
  - `0 <= current_step_index <= len(steps) - 1` (**ASSUM-004** — the
    pointer never sits past the last step);
  - `status` is a `StepStatus`.
- **`RunbookValidationError(ValueError)`** — raised by the validators
  above with a clear, domain-shaped message.
- Re-export the new names from
  `src/forge/persistence/repositories/__init__.py`.

Match the `BridgeRegistryEntry` style exactly: `@dataclass(frozen=True, slots=True)`,
`from __future__ import annotations`, module docstring referencing this
task ID and the relevant ASSUM IDs.

## Acceptance Criteria

- [ ] `forge.persistence.repositories.runbook_models` exists and exports
      `StepStatus`, `StepResult`, `Step`, `Runbook`, `RunbookValidationError`.
- [ ] `StepStatus` has exactly the five members
      `pending/running/passed/failed/awaiting_approval` and nothing else;
      a test asserts the membership set is closed.
- [ ] Constructing a `Runbook` with an empty `steps` tuple raises
      `RunbookValidationError` (ASSUM-002).
- [ ] Constructing a `Runbook` with `current_step_index` outside
      `[0, len(steps)-1]` raises `RunbookValidationError` (ASSUM-004).
- [ ] Constructing a `Step` with an empty `step_type` raises
      `RunbookValidationError` (ASSUM-005).
- [ ] A freshly constructed three-step `Runbook` with
      `current_step_index=0` and every step `StepStatus.pending` is valid
      and equality-comparable (frozen dataclass equality).
- [ ] `Step.result` defaults to `None`; a `StepResult` round-trips its
      `exit_code`, `captured_output`, `started_at`, `completed_at` via
      dataclass equality.
- [ ] Models are immutable: attempting to set an attribute on a `Runbook`
      or `Step` raises (`frozen=True`).
- [ ] Unit tests live in `tests/forge/persistence/test_runbook_models.py`
      and are written **test-first** (TDD).

## Coach Validation

```bash
python -m pytest tests/forge/persistence/test_runbook_models.py -q
python -c "from forge.persistence.repositories import StepStatus, Step, Runbook, StepResult, RunbookValidationError; print('imports OK')"
```

## Implementation Notes

- `declarative` task_type: no architectural review required — this is
  pure type/data definition.
- Keep this module I/O-free; SQL and JSON encoding belong to the
  repository (TASK-RSP-003/004), not here.
- `StepStatus` being the single status vocabulary is deliberate: the
  migration's `CHECK` constraint (TASK-RSP-002) must enumerate exactly
  `[s.value for s in StepStatus]`. See §4 contract `StepStatus value set`
  in `IMPLEMENTATION-GUIDE.md`.
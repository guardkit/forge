---
id: TASK-RSP-004
title: "RunbookRepository: update_step_status + advance"
status: backlog
created: 2026-06-21T18:30:00Z
updated: 2026-06-21T18:30:00Z
priority: high
task_type: feature
parent_review: TASK-REV-RSP-001
parent_feature: FEAT-RSP
feature_slug: runbook-and-step-persistence
wave: 3
implementation_mode: task-work
complexity: 6
estimated_minutes: 90
dependencies:
  - TASK-RSP-003
consumer_context:
  - task: TASK-RSP-002
    consumes: runbooks_schema
    framework: "raw sqlite3 (forge.adapters.sqlite.connect_writer)"
    driver: "sqlite3 (stdlib, STRICT tables)"
    format_note: "UPDATE targets (runbook_id, sequence_index); result is JSON TEXT; status must satisfy the CHECK set; current_step_index is an INTEGER column"
  - task: TASK-RSP-001
    consumes: StepStatus_value_set
    framework: "raw sqlite3 (forge.adapters.sqlite.connect_writer)"
    driver: "sqlite3 (stdlib, STRICT tables)"
    format_note: "an unrecognised status must be refused with RunbookValidationError before the write reaches the CHECK constraint"
tags:
  - forge
  - persistence
  - runbook
  - repository
---

# RunbookRepository: update_step_status + advance

## TL;DR

Extend `RunbookRepository` (in `runbook.py`, created by TASK-RSP-003) with
the two **mutation** methods: `update_step_status` (per-step status +
optional result, addressed by sequence position) and `advance` (move the
resume pointer). Same `BEGIN IMMEDIATE` + `_safe_rollback` discipline.

## Scope

- **`update_step_status(runbook_id, sequence_index, status, *, correlation_id, result=None) -> None`**
  - A step is addressed by its `sequence_index` (no standalone step id,
    **ASSUM-006**).
  - Refuse an out-of-range `sequence_index` with `RunbookStepNotFoundError`
    (rowcount 0); the stored steps are unchanged (Negative).
  - Refuse a status not in `StepStatus` with `RunbookValidationError`
    **before** writing; the step retains its previous status (Negative).
  - When `result` is supplied, JSON-encode and persist it on that step
    (exit code, captured output, started/completed timestamps round-trip —
    Edge "A step result preserves..."; oversized output Group E).
  - `BEGIN IMMEDIATE` + `_safe_rollback`: a write that fails mid-flight
    leaves the prior committed status intact (Group G atomicity).
- **`advance(runbook_id, *, correlation_id) -> None`**
  - Increment `current_step_index` by one.
  - Refuse when already at the final step (`RunbookAdvanceError`); the
    pointer stays put (Boundary "Advancing past the final step is refused";
    **ASSUM-004**).
  - Refuse an unknown runbook with `RunbookNotFoundError` (Negative
    "Advancing a runbook that does not exist is refused").
  - Does **not** mutate overall status (ASSUM-009).

Add error types `RunbookStepNotFoundError(RuntimeError)` and
`RunbookAdvanceError(RuntimeError)`; export from `repositories/__init__.py`.

## Acceptance Criteria

- [ ] Marking the second step `passed` and reloading reports it `passed`
      while the others stay `pending` (Key Example "Updating a step status").
- [ ] Advancing from the first step and reloading puts the pointer on the
      second step (Key Example "Advancing a runbook").
- [ ] A step set to `awaiting_approval` persists and reloads as such
      (Edge "A step awaiting approval is persisted and reloaded").
- [ ] Each of the five `StepStatus` values can be set on a step and read
      back (Edge "Each recognised step status can be stored and read back").
- [ ] A step result round-trips its exit code, captured output, and
      `started_at`/`completed_at` (Edge "A step result preserves...").
- [ ] The resume pointer can occupy positions 0..2 across a three-step
      runbook and survives reload (Boundary scenario outline).
- [ ] The resume pointer survives the store being closed and reopened
      (Edge "The resume pointer survives reopening the store").
- [ ] Updating a step position out of range raises
      `RunbookStepNotFoundError`; stored steps unchanged (Negative).
- [ ] Setting an unrecognised status raises `RunbookValidationError`;
      the step keeps its previous status (Negative).
- [ ] Advancing past the final step raises `RunbookAdvanceError` and
      leaves the pointer and all step statuses untouched (Boundary +
      Group G refused-advance consistency; ASSUM-004).
- [ ] Advancing an unknown runbook raises `RunbookNotFoundError` (Negative).
- [ ] The overall status set at create round-trips across all five values
      and is not changed by `update_step_status`/`advance` (Edge outline +
      ASSUM-009).
- [ ] All modified files pass project-configured lint/format checks with
      zero errors.
- [ ] Tests added to `tests/forge/persistence/test_runbook.py`, written
      **test-first** (TDD).

## Coach Validation

```bash
python -m pytest tests/forge/persistence/test_runbook.py -q
python -m pytest tests/forge/persistence/test_runbook.py -q -m seam
```

## §4 Seam Tests

Validates the `runbooks_schema` contract from TASK-RSP-002 for the
mutation path (status CHECK + result JSON column).

```python
"""Seam test: verify runbooks_schema contract for mutations (TASK-RSP-002)."""
from pathlib import Path

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations as lifecycle_migrations
from forge.persistence.migrations import runbook as runbook_migration
from forge.persistence.repositories.runbook_models import StepStatus


@pytest.mark.seam
@pytest.mark.integration_contract("runbooks_schema")
def test_status_check_rejects_unknown_value(tmp_path: Path) -> None:
    """The DB CHECK set must reject any value outside StepStatus.

    Contract: status TEXT CHECK (status IN <StepStatus values>).
    Producer: TASK-RSP-002
    """
    import sqlite3

    cx = sqlite_connect.connect_writer(tmp_path / "forge.db")
    lifecycle_migrations.apply_at_boot(cx)
    runbook_migration.apply(cx)
    cx.execute(
        "INSERT INTO runbooks(runbook_id, target, current_step_index, status, created_at)"
        " VALUES('rb', 't', 0, ?, '2026-06-21T00:00:00+00:00')",
        (StepStatus.pending.value,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        cx.execute(
            "INSERT INTO runbook_steps(runbook_id, sequence_index, step_type, params, status)"
            " VALUES('rb', 0, 'shell', '{}', 'not_a_real_status')"
        )
```

## Implementation Notes

- Validate the status against `StepStatus` in Python **before** the SQL
  write so the caller gets `RunbookValidationError`, not a raw
  `IntegrityError` — the CHECK constraint is the backstop, the Python
  guard is the contract.
- `advance` reads `current_step_index` and `len(steps)` inside the same
  `BEGIN IMMEDIATE` transaction it writes in, so a concurrent
  advance+update serialise without lost work (Group F).

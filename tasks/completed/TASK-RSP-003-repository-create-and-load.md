---
complexity: 6
consumer_context:
- consumes: runbooks_schema
  driver: sqlite3 (stdlib, STRICT tables)
  format_note: INSERT column names/types must match the migration DDL exactly; params/result
    are JSON TEXT; timestamps ISO-8601 TEXT; steps keyed by (runbook_id, sequence_index)
  framework: raw sqlite3 (forge.adapters.sqlite.connect_writer)
  task: TASK-RSP-002
- consumes: StepStatus_value_set
  driver: sqlite3 (stdlib, STRICT tables)
  format_note: status written to DB must be StepStatus(...).value; the CHECK set and
    the enum must stay identical
  framework: raw sqlite3 (forge.adapters.sqlite.connect_writer)
  task: TASK-RSP-001
created: 2026-06-21 18:30:00+00:00
dependencies:
- TASK-RSP-001
- TASK-RSP-002
estimated_minutes: 90
feature_slug: runbook-and-step-persistence
id: TASK-RSP-003
implementation_mode: task-work
parent_feature: FEAT-RSP
parent_review: TASK-REV-RSP-001
priority: high
status: completed
tags:
- forge
- persistence
- runbook
- repository
task_type: feature
title: 'RunbookRepository: create_runbook + load_runbook'
updated: 2026-06-21 18:30:00+00:00
wave: 2
---

# RunbookRepository: create_runbook + load_runbook

## TL;DR

Create `src/forge/persistence/repositories/runbook.py` with the
`RunbookRepository` class and its two **read/write-foundation** methods:
`create_runbook` (the core write path) and `load_runbook` (the core read
path). Mirror `BridgeRegistry`: `BEGIN IMMEDIATE` transactional writes,
`_safe_rollback`, JSON-encoded `params`/`result`, ISO-8601 timestamps,
`correlation_id` on every public method.

## Scope

`RunbookRepository(*, connection: sqlite3.Connection)`:

- **`create_runbook(runbook: Runbook, *, correlation_id: str) -> None`**
  - `BEGIN IMMEDIATE; INSERT runbooks ...; INSERT runbook_steps ... (one per step); COMMIT;`
  - Refuse an empty step list with `RunbookValidationError` (ASSUM-002).
  - A duplicate `runbook_id` raises `RunbookDuplicateError` (the PK
    `IntegrityError` is caught and re-raised domain-shaped); the original
    runbook is left untouched (ASSUM-010).
  - JSON-encode `step.params` (default `{}`) and `step.result`
    (`None` → SQL `NULL`); write timestamps via `.isoformat()`.
  - `_safe_rollback()` on any `sqlite3.Error` so a refused/failed write
    leaves no half-written step (Group F clash, Group G atomicity).
- **`load_runbook(runbook_id, *, correlation_id: str) -> Runbook | None`**
  - Returns `None` when no such runbook exists (Group C
    "Loading a runbook that does not exist reports it as missing").
  - Reconstructs `Runbook` + ordered `Step`s **ordered by `sequence_index`**
    (Group G "Steps reload in sequence order regardless of insert order").
  - Decodes JSON `params`/`result`; rebuilds `StepResult` when present;
    rehydrates `StepStatus` from the stored value.
  - Round-trips `target`, `created_at`, `current_step_index`, overall
    `status` faithfully (Key Example "Loading a persisted runbook").

Add error types `RunbookDuplicateError(RuntimeError)` and
`RunbookNotFoundError(RuntimeError)` (the latter used by TASK-RSP-004 too).
Export the repository + errors from `repositories/__init__.py`.

## Acceptance Criteria

- [ ] Creating a three-step runbook persists it under its `runbook_id`
      with all three steps in original order; the call reports success
      (Key Example: "Creating a runbook persists it with its ordered steps").
- [ ] `load_runbook` returns the same `target`, `created_at`,
      resume pointer, and overall status that were stored; steps come back
      in sequence order (Key Example: "Loading a persisted runbook").
- [ ] A newly created runbook loads back with every step `pending` and
      `current_step_index == 0` (Key Example + ASSUM-003).
- [ ] A single-step runbook round-trips and its pointer rests on the only
      step (Boundary: "single step is created and loaded").
- [ ] Creating a runbook with an empty step list raises
      `RunbookValidationError` and persists nothing (Boundary/Negative:
      "Creating a runbook with no steps is refused").
- [ ] A second `create_runbook` under an existing `runbook_id` raises
      `RunbookDuplicateError`; the original is unaffected (Negative +
      ASSUM-010).
- [ ] `load_runbook` for an unknown id returns `None` (Negative:
      "Loading a runbook that does not exist reports it as missing").
- [ ] Step `params` round-trip without loss, including nested mappings
      (Edge: "Step parameters round-trip without loss"; ASSUM-011).
- [ ] Steps persisted in a shuffled order still load first-to-last by
      `sequence_index` (Group G).
- [ ] Every public method accepts `correlation_id: str` explicitly.
- [ ] Seam test (below) passes, proving the repo's INSERT columns match
      the migration DDL.
- [ ] All modified files pass project-configured lint/format checks with
      zero errors.
- [ ] Tests in `tests/forge/persistence/test_runbook.py`, written
      **test-first** (TDD), class-organised by AC like
      `test_bridge_registry.py`.

## Coach Validation

```bash
python -m pytest tests/forge/persistence/test_runbook.py -q
python -m pytest tests/forge/persistence/test_runbook.py -q -m seam
```

## §4 Seam Tests

The following seam test validates the integration contract with the
producer task TASK-RSP-002. Implement it to verify the boundary before
integration.

```python
"""Seam test: verify runbooks_schema contract from TASK-RSP-002."""
import sqlite3
from pathlib import Path

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations as lifecycle_migrations
from forge.persistence.migrations import runbook as runbook_migration
from forge.persistence.repositories.runbook_models import StepStatus


@pytest.mark.seam
@pytest.mark.integration_contract("runbooks_schema")
def test_runbook_schema_matches_repository_writes(tmp_path: Path) -> None:
    """The repo's INSERT columns must match the migration DDL exactly.

    Contract: STRICT runbooks/runbook_steps tables; status CHECK set ==
    StepStatus values; params/result JSON TEXT; ordering by sequence_index.
    Producer: TASK-RSP-002
    """
    cx = sqlite_connect.connect_writer(tmp_path / "forge.db")
    lifecycle_migrations.apply_at_boot(cx)
    runbook_migration.apply(cx)

    cols = {r[1] for r in cx.execute("PRAGMA table_info(runbooks)")}
    assert {"runbook_id", "target", "current_step_index", "status",
            "created_at"} <= cols

    step_cols = {r[1] for r in cx.execute("PRAGMA table_info(runbook_steps)")}
    assert {"runbook_id", "sequence_index", "step_type", "params",
            "status", "result"} <= step_cols

    # Tables must be STRICT and the status CHECK must mirror StepStatus.
    ddl = cx.execute(
        "SELECT sql FROM sqlite_master WHERE name='runbook_steps'"
    ).fetchone()[0]
    assert "STRICT" in ddl.upper()
    for status in StepStatus:
        assert status.value in ddl, f"CHECK set missing {status.value!r}"
```

## Implementation Notes

- Hold `result == NULL` for `pending`/`running` steps (ASSUM-007); only
  TASK-RSP-004 records results.
- Do not mutate overall status here beyond the value supplied at create
  (ASSUM-009).
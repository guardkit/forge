---
complexity: 6
created: 2026-04-25 14:36:00+00:00
dependencies:
- TASK-IC-002
- TASK-IC-003
estimated_minutes: 150
feature_id: FEAT-FORGE-006
id: TASK-IC-004
implementation_mode: task-work
parent_review: TASK-REV-IC8B
priority: high
status: design_approved
tags:
- memory
- reconciliation
- durability
task_type: feature
title: Reconcile backfill at build start
updated: 2026-04-25 14:36:00+00:00
wave: 4
---

# Task: Reconcile backfill at build start

## Description

At build start, diff SQLite `stage_log` rows (FEAT-FORGE-001 authoritative)
against the corresponding entities in Graphiti's `forge_pipeline_history` group.
Backfill any missing entities by replaying them through `write_entity()`
(synchronous, NOT fire-and-forget — we want to know if the backfill succeeded).

Covers `@edge-case reconcile-backfill` and is the durability safety net for
fire-and-forget writes (TASK-IC-002).

## Module: `forge/memory/reconciler.py`

```python
async def reconcile_pipeline_history(
    sqlite_repo: PipelineHistoryRepository,
    horizon_days: int = 30,
) -> ReconcileReport:
    """Diff SQLite vs Graphiti for the last N days; backfill missing entities.
    Returns a ReconcileReport (counts, errors, entities backfilled)."""
```

## Acceptance Criteria

- [ ] Reads SQLite via the FEAT-FORGE-001 `PipelineHistoryRepository`
      interface — NEVER via direct SQLite SQL or schema knowledge (Risk 3
      mitigation from review)
- [ ] Queries Graphiti for entity_ids existing in `forge_pipeline_history`
      within the horizon window
- [ ] Computes set difference: SQLite UUIDs ∉ Graphiti entity_ids
- [ ] For each missing entity, replays via `write_entity()` (NOT
      fire-and-forget); collects per-entity outcomes
- [ ] Returns `ReconcileReport` with `total_sqlite`, `total_graphiti`,
      `backfilled_count`, `failed_count`, `failed_entities` list
- [ ] Backfill failures do NOT raise — recorded in report and surfaced to
      structured log
- [ ] Default horizon = 30 days (configurable via `forge.yaml`)
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Test Requirements

- [ ] `tests/unit/test_reconciler.py` — mocked SQLite repo + mocked Graphiti
      client; assert correct diff + backfill calls
- [ ] `tests/integration/test_reconciler_with_real_sqlite.py` — opt-in;
      against a tmp SQLite DB and a mocked Graphiti
- [ ] BDD step impl for `@edge-case reconcile-backfill` (TASK-IC-011)

## Implementation Notes

- Risk 3 from review: do NOT introduce SQLite schema coupling here. The
  reconcile contract is `Iterable[PipelineHistoryEntity]` from the FEAT-FORGE-001
  repository, not raw rows.
- Performance: for the 30-day horizon, expected entity count is in the
  hundreds, not thousands — a per-entity write loop is fine. Don't
  pre-optimise with bulk APIs.
- The reconcile pass is invoked once per build start, before priors retrieval
  (so priors see a consistent Graphiti state).
- Cycle detection is NOT this unit's responsibility — supersession cycles
  are TASK-IC-008's territory.
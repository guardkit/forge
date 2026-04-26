---
id: TASK-IC-003
title: "Write-ordering guard (SQLite-first, Graphiti-second)"
status: backlog
created: 2026-04-25T14:36:00Z
updated: 2026-04-25T14:36:00Z
priority: high
task_type: feature
tags: [memory, ordering, idempotency]
complexity: 3
parent_review: TASK-REV-IC8B
feature_id: FEAT-FORGE-006
wave: 3
implementation_mode: direct
dependencies: [TASK-IC-002]
estimated_minutes: 45
---

# Task: Write-ordering guard (SQLite-first, Graphiti-second)

## Description

Implement the sequencing helper that wraps every stage-completion hook so the
SQLite (FEAT-FORGE-001) authoritative write commits BEFORE the Graphiti
fire-and-forget dispatch happens. This makes the SQLite store the single source
of truth for reconcile-backfill (TASK-IC-004) to compare against.

Covers `@edge-case write-ordering`.

## Module: `forge/memory/ordering.py`

```python
def record_stage_event(
    persist_to_sqlite: Callable[[], PipelineHistoryEntity],
    group_id: str,
) -> PipelineHistoryEntity:
    """Commit to SQLite first, dispatch fire-and-forget Graphiti write second.
    Returns the persisted entity. SQLite commit failures raise; Graphiti
    failures are absorbed by fire_and_forget_write()."""
    entity = persist_to_sqlite()  # synchronous; raises on failure
    fire_and_forget_write(entity, group_id)
    return entity
```

## Acceptance Criteria

- [ ] `record_stage_event()` is the single helper every stage hook calls
- [ ] SQLite commit happens BEFORE Graphiti dispatch (no parallel; no reordering)
- [ ] If SQLite commit raises, Graphiti dispatch does NOT happen (no orphan
      Graphiti writes without SQLite anchor)
- [ ] If Graphiti dispatch fails, SQLite entry is still durable (verified by
      reconcile-backfill picking it up next build)
- [ ] Covers `@edge-case write-ordering`: assertion that SQLite-committed
      timestamp precedes Graphiti `created_at`
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Test Requirements

- [ ] `tests/unit/test_ordering.py` — assert SQLite commit invoked before
      Graphiti dispatch (call-order spy); assert dispatch skipped on SQLite
      raise
- [ ] BDD step impl for `@edge-case write-ordering` (TASK-IC-011)

## Implementation Notes

- This is a thin coordinator; resist adding retry logic or compensation.
  Compensation is reconcile-backfill's job (TASK-IC-004).
- The `persist_to_sqlite` callable comes from FEAT-FORGE-001's repository.
  Treat its return value as opaque except that it carries the SQLite-row
  UUID we need for the Graphiti `entity_id`.

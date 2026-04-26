---
complexity: 5
created: 2026-04-25 14:36:00+00:00
dependencies:
- TASK-IC-001
- TASK-IC-003
estimated_minutes: 90
feature_id: FEAT-FORGE-006
id: TASK-IC-007
implementation_mode: task-work
parent_review: TASK-REV-IC8B
priority: high
status: design_approved
tags:
- memory
- ordering
- idempotency
- terminal-state
task_type: feature
title: SessionOutcome writer with ordering and idempotency
updated: 2026-04-25 14:36:00+00:00
wave: 4
---

# Task: SessionOutcome writer with ordering and idempotency

## Description

Wire the terminal-state callback (FEAT-FORGE-001 owns the trigger) to a
function that collects all `GateDecision` references for the build, sorts
them by `decided_at` ascending (per ASSUM-008 resolution), and writes a
single `SessionOutcome` entity with the ordered link list. Pre-write check
ensures idempotency: if a `SessionOutcome` for this `build_id` already
exists in Graphiti, the writer no-ops.

Covers `@key-example session-outcome-written`, `@concurrency
gate-decisions-in-close-succession`, `@edge-case @data-integrity
session-outcome-retry-idempotency`, `@edge-case no-in-progress-session-outcome`.

## Module: `forge/memory/session_outcome.py`

```python
async def write_session_outcome(
    build_id: str,
    outcome: Literal["success", "failure", "aborted"],
    sqlite_repo: PipelineHistoryRepository,
) -> SessionOutcome | None:
    """If a SessionOutcome for build_id already exists, return None (no-op).
    Otherwise, collect GateDecisions, sort by decided_at ASC, write entity."""
```

## Acceptance Criteria

- [ ] Pre-write existence check by `build_id` in `forge_pipeline_history`
- [ ] Existing entity → no-op return None (`@edge-case session-outcome-retry-idempotency`)
- [ ] `GateDecision` references collected from SQLite repo (read-only via
      FEAT-FORGE-001 interface)
- [ ] Sort by `decided_at` ASC before constructing `gate_decision_ids` list
      (ASSUM-008 resolution)
- [ ] Writes via `write_entity()` (synchronous, NOT fire-and-forget — caller
      wants confirmation)
- [ ] Only fires on first terminal-state transition; in-progress states
      produce no SessionOutcome (`@edge-case no-in-progress-session-outcome`)
- [ ] Concurrent calls (two Forge instances) → both see the same existence
      check; one writes, one no-ops (`@concurrency gate-decisions-in-close-succession`
      and split-brain safety from TASK-IC-001 `entity_id` contract)
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Test Requirements

- [ ] `tests/unit/test_session_outcome.py` — first call writes; second call
      no-ops; out-of-order GateDecision input is sorted on output
- [ ] `tests/unit/test_session_outcome_terminal_only.py` — non-terminal
      states do not produce SessionOutcome
- [ ] BDD step impls for the listed scenarios (TASK-IC-011)

## Implementation Notes

- The terminal-state callback is owned by FEAT-FORGE-001; this unit only
  provides the writer function. FEAT-FORGE-001 is responsible for invoking
  it once on first terminal transition.
- Sorting: use `sorted(decisions, key=lambda d: d.decided_at)`. If two
  decisions share a timestamp (microsecond-level tie), the secondary sort
  is by `entity_id` to keep ordering deterministic.
- The pre-write existence check relies on Graphiti's queryability — if the
  query is slow enough to race with a concurrent writer, the second writer
  may still attempt a write; Graphiti's upsert semantics on `entity_id`
  (sourced from `build_id`-derived UUID) handle the dedupe at the storage
  level. Document this fallback.
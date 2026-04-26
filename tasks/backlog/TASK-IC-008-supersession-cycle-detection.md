---
id: TASK-IC-008
title: "Supersession-cycle detection for CalibrationAdjustment"
status: backlog
created: 2026-04-25T14:36:00Z
updated: 2026-04-25T14:36:00Z
priority: high
task_type: feature
tags: [memory, data-integrity, cycle-detection]
complexity: 4
parent_review: TASK-REV-IC8B
feature_id: FEAT-FORGE-006
wave: 2
implementation_mode: task-work
dependencies: [TASK-IC-001]
estimated_minutes: 75
---

# Task: Supersession-cycle detection for CalibrationAdjustment

## Description

Before proposing a new `CalibrationAdjustment` that supersedes an existing
one, walk the `supersedes` chain with a visited-set to detect cycles. If a
cycle is found, reject the proposal with a clear error.

Covers `@edge-case @data-integrity supersession-cycle-rejection`.

## Module: `forge/memory/supersession.py`

```python
class SupersessionCycleError(ValueError):
    """Raised when proposing a CalibrationAdjustment would create a cycle."""

def assert_no_cycle(
    new_adjustment: CalibrationAdjustment,
    chain_resolver: Callable[[str], CalibrationAdjustment | None],
    max_depth: int = 10,
) -> None:
    """Walk the supersedes chain from new_adjustment.supersedes upward.
    Raise SupersessionCycleError if visited-set is hit. Raise on depth cap."""
```

## Acceptance Criteria

- [ ] Walks the `supersedes` chain via `chain_resolver` (lookup by entity_id)
- [ ] Uses a `set[str]` of visited entity_ids; cycle detected when a visited
      id is re-entered
- [ ] Configurable `max_depth` (default 10); chain longer than depth raises
      `SupersessionCycleError` with chain context in the message
- [ ] Clean linear chains (no cycles, depth ≤ 10) return None
- [ ] Self-supersession (`new.supersedes == new.entity_id`) is a special case
      that raises immediately
- [ ] Error message includes the cycle path for operator debugging
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Test Requirements

- [ ] `tests/unit/test_supersession.py` — clean chain → no raise; cycle of
      length 2 → raises; cycle of length 5 → raises; chain of depth 11 →
      raises; self-supersession → raises
- [ ] BDD step impl for `@edge-case @data-integrity supersession-cycle-rejection`
      (TASK-IC-011)

## Implementation Notes

- The `chain_resolver` callable abstracts the lookup source (could be SQLite
  or Graphiti); this unit doesn't care.
- Don't pre-optimise: `set` lookup is O(1), the chain is short by design,
  no need for hash-based cycle detection beyond the visited set.
- The depth cap (10) is configurable in `forge.yaml` for ops who want
  longer chains; document the tradeoff (longer chains = harder operator
  reasoning).

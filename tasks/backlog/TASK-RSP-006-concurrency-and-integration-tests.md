---
id: TASK-RSP-006
title: Concurrency and integration-boundary tests
status: backlog
created: 2026-06-21T18:30:00Z
updated: 2026-06-21T18:30:00Z
priority: medium
task_type: testing
parent_review: TASK-REV-RSP-001
parent_feature: FEAT-RSP
feature_slug: runbook-and-step-persistence
wave: 4
implementation_mode: task-work
complexity: 6
estimated_minutes: 90
dependencies:
  - TASK-RSP-004
tags:
  - forge
  - persistence
  - runbook
  - testing
  - concurrency
---

# Concurrency and integration-boundary tests

## TL;DR

Cover the Group F (Concurrency) and Group H (Integration Boundaries)
scenarios in a **dedicated** file
`tests/forge/persistence/test_runbook_concurrency.py`, mirroring the
`TestRecordConcurrency` threaded pattern in `test_bridge_registry.py`.
Owns its file → runs in parallel with the security suite (TASK-RSP-005).

## Scope — scenarios covered

**Group F — Concurrency:**

- Two simultaneous creates of `rb-clash` → exactly one succeeds, the other
  is refused as a duplicate, the persisted runbook has all three steps
  intact, and no half-written step is left behind.
- A concurrent `advance` and `update_step_status` on `rb-serial` both
  succeed under the write lock: the pointer moves to the second step **and**
  the first step is recorded `passed` (no lost work).
- A read-only reader (`read_only_connect`) sees a consistent snapshot
  while a result write is in flight: it observes the step without the
  result before commit, with the result after commit, and never a
  half-written step (WAL snapshot isolation).

**Group H — Integration Boundaries:**

- Opening the store at an unusable location (`does not exist` /
  `cannot be accessed`) surfaces a clear `SQLiteConnectError`-shaped
  store-unavailable error, never a raw backend failure.
- A read-only caller attempting `create_runbook` is refused; the store is
  unchanged.
- Loading from a store whose runbook tables have not been migrated is
  refused predictably (no partial runbook returned).

## Acceptance Criteria

- [ ] Two threads creating `rb-clash` concurrently leave exactly one whole
      runbook; the loser raises `RunbookDuplicateError`; the survivor loads
      with all three steps and no orphaned step rows.
- [ ] Concurrent `advance` + `update_step_status(first, passed)` both
      commit: reload shows pointer on step 2 and step 1 `passed`.
- [ ] A read-only reader observes pre-commit state without the new result
      and post-commit state with it; never a half-written step.
- [ ] Opening the store at a non-existent / inaccessible location raises a
      clear store-unavailable error (`SQLiteConnectError`) with no raw
      backend leak.
- [ ] A `read_only_connect`-backed repository refuses `create_runbook` and
      the store stays unchanged.
- [ ] Loading from an unmigrated store is refused predictably with no
      partial runbook.
- [ ] All tests live in
      `tests/forge/persistence/test_runbook_concurrency.py` and pass
      reliably (no timing flakiness).

## Coach Validation

```bash
python -m pytest tests/forge/persistence/test_runbook_concurrency.py -q
```

## Implementation Notes

- Use `threading` + a `threading.Barrier`/`Event` to force genuine
  contention, exactly like `TestRecordConcurrency` in
  `test_bridge_registry.py`. `connect_writer` sets `busy_timeout=5000`, so
  the write lock serialises rather than erroring.
- The reader uses `read_only_connect(db_path)` (`mode=ro`); WAL mode
  (set by `connect_writer`) gives the reader a committed snapshot.
- Surface the store-unavailable path via the existing `SQLiteConnectError`
  from `forge.adapters.sqlite.connect` — do not invent a new error type.
- These are in-process simulations of concurrency/failure — there is **no**
  live infrastructure and **no** human-in-the-loop, so this remains a
  pure-unit, AutoBuild-suitable task (not `operator_handoff`).

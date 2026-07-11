---
complexity: 4
conductor_workspace: wave2-autobuild-state-initialiser
dependencies:
- TASK-FW10-001
estimated_minutes: 60
feature_id: FEAT-FORGE-010
id: TASK-FW10-005
implementation_mode: task-work
parent_review: TASK-REV-FW10
priority: high
status: completed
tags:
- factory
- sqlite
- async-tasks
- state-channel
task_type: feature
title: AutobuildStateInitialiser production binding to async_tasks SQLite writer
wave: 2
reconciled: "2026-07-11 WS3-S8 sweep — FW10 in completed/; moved by 9ef9138b (FEAT-DEA8 finalize)"
---

# TASK-FW10-005 — `AutobuildStateInitialiser` production binding

## Why

`AutobuildStateInitialiser` (the `state_channel` collaborator on
`dispatch_autobuild_async`) is the third of the four required Protocol
implementations. The `async_tasks` SQLite writer it wraps exists in
`forge.lifecycle.persistence`. This task wires them with the DDR-006
contract: the dispatcher writes only `lifecycle="starting"`; the
subagent owns subsequent writes.

## Files to create / modify

- `src/forge/cli/_serve_deps_state_channel.py` (NEW):
  - `def build_autobuild_state_initialiser(sqlite_pool) -> AutobuildStateInitialiser`
  - Returns a Protocol implementation whose `initialise(...)` method
    writes `lifecycle="starting"` to the `async_tasks` channel for the
    given `task_id` / `feature_id`.
- `tests/cli/test_serve_deps_state_channel.py` (NEW):
  - Initial-state write produces a row observable by a reader on the
    same pool with `lifecycle="starting"`.
  - Subsequent reads continue to observe the initial value until the
    subagent's first transition (this part of DDR-006 is asserted; the
    subagent itself does the next write in TASK-FW10-002).

## Acceptance criteria

- [ ] `build_autobuild_state_initialiser(sqlite_pool)` returns a
      Protocol-conforming object whose `initialise(...)` writes
      `lifecycle="starting"` per DDR-006.
- [ ] A read after initialise returns the row with
      `lifecycle="starting"`.
- [ ] No transition writes happen inside the initialiser — only the
      initial-state write.
- [ ] All modified files pass project-configured lint/format checks
      with zero errors.

## Implementation notes

- Match the `AutobuildStateInitialiser` Protocol surface in
  `src/forge/pipeline/dispatchers/autobuild_async.py` exactly. Do not
  add new methods.
- The `async_tasks` SQLite writer is in
  `src/forge/lifecycle/persistence.py`. Use its existing
  connection-scoped session pattern.
- DDR-006 owns the `Literal` set for `lifecycle`. Do not introduce
  new values.

## Coach validation

- `pytest tests/cli/test_serve_deps_state_channel.py -x`.
- `pytest tests/forge -x` (smoke gate 2).
- Lint: project-configured ruff/format.

## References

- [DDR-006](../../../docs/design/decisions/DDR-006-async-subagent-state-channel-contract.md)
- [`src/forge/lifecycle/persistence.py`](../../../src/forge/lifecycle/persistence.py)
- [`src/forge/pipeline/dispatchers/autobuild_async.py`](../../../src/forge/pipeline/dispatchers/autobuild_async.py)
- IMPLEMENTATION-GUIDE.md §4 contract: `AutobuildStateInitialiser`
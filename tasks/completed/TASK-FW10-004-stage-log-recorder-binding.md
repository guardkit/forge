---
complexity: 3
conductor_workspace: wave2-stage-log-recorder
dependencies:
- TASK-FW10-001
estimated_minutes: 45
feature_id: FEAT-FORGE-010
id: TASK-FW10-004
implementation_mode: task-work
parent_review: TASK-REV-FW10
priority: high
status: completed
tags:
- factory
- sqlite
- stage-log
task_type: feature
title: StageLogRecorder production binding to forge.lifecycle.persistence SQLite writer
wave: 2
reconciled: "2026-07-11 WS3-S8 sweep — FW10 in completed/; moved by 9ef9138b (FEAT-DEA8 finalize)"
---

# TASK-FW10-004 — `StageLogRecorder` production binding

## Why

`StageLogRecorder` is the second of `dispatch_autobuild_async`'s four
collaborators. It's a Protocol in
`src/forge/pipeline/dispatchers/autobuild_async.py`; the SQLite writer
exists in `src/forge/lifecycle/persistence.py`. This task wires them
together via a thin factory.

## Files to create / modify

- `src/forge/cli/_serve_deps_stage_log.py` (NEW):
  - `def build_stage_log_recorder(sqlite_pool) -> StageLogRecorder`
  - Returns a Protocol-conforming wrapper that delegates to the
    persistence layer's writer.
- `tests/cli/test_serve_deps_stage_log.py` (NEW):
  - Factory returns a recorder that records a stage transition; a
    subsequent reader against the same `sqlite_pool` observes the
    write.

## Acceptance criteria

- [ ] `build_stage_log_recorder(sqlite_pool)` returns a
      `StageLogRecorder` Protocol implementation.
- [ ] A round-trip test (write then read on the same pool) observes
      the recorded transition.
- [ ] All modified files pass project-configured lint/format checks
      with zero errors.

## Implementation notes

- Match the `StageLogRecorder` Protocol surface in
  `src/forge/pipeline/dispatchers/autobuild_async.py` exactly. Do not
  add new methods.
- Use the SQLite pool's connection-scoped session pattern that
  `forge.lifecycle.persistence` already uses; do not open a second
  pool.

## Coach validation

- `pytest tests/cli/test_serve_deps_stage_log.py -x`.
- `pytest tests/forge -x` (smoke gate 2).
- Lint: project-configured ruff/format.

## References

- [`src/forge/lifecycle/persistence.py`](../../../src/forge/lifecycle/persistence.py)
- [`src/forge/pipeline/dispatchers/autobuild_async.py`](../../../src/forge/pipeline/dispatchers/autobuild_async.py) (the Protocol surface)
- IMPLEMENTATION-GUIDE.md §4 contract: `StageLogRecorder`
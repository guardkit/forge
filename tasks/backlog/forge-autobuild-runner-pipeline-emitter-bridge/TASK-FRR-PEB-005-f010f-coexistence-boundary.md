---
id: TASK-FRR-PEB-005
title: "F010F coexistence — sync-raise still uses safety-net publish, not the bridge"
status: backlog
created: 2026-05-06T00:00:00Z
updated: 2026-05-06T00:00:00Z
priority: high
task_type: refactor
documentation_level: standard
parent_task: TASK-FORGE-FRR-F010M
parent_review: TASK-REV-F010M
feature_id: FEAT-PEBR
wave: 2
implementation_mode: task-work
complexity: 5
estimated_minutes: 60
dependencies:
  - TASK-FRR-PEB-004
tags:
  - forge-serve
  - autobuild-runner
  - pipeline-lifecycle-emitter
  - boundary-regression
  - f010f-coexistence
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Task: F010F coexistence — sync-raise still uses safety-net publish, not the bridge

## TL;DR

Lock the boundary between F010F's sync-raise safety-net publish and the
new lifecycle bridge's async-terminal publish. F010F **stays unchanged** —
the sync-raise emitter remains the source of `build-failed` envelopes when
`dispatch_build` raises synchronously. The bridge handles **async-terminal
only**. This task adds the boundary regression tests that ensure the two
paths cannot double-publish even when they fire concurrently.

## Locks BDD scenarios

- @key-example @regression `A synchronous dispatch raise still uses
  F010F's safety-net publish, not the bridge`
- @edge-case @regression `A synchronous dispatch raise concurrent with
  the bridge's terminal observation produces exactly one build-failed
  envelope`

## Acceptance criteria

- AC-1: When `dispatch_build` raises synchronously, the bridge's
  `attach()` call is never made (the registry has no entry); F010F's
  safety-net publish path fires exactly one `build-failed` envelope.
  No `build-started` envelope is published.
- AC-2: When the bridge has already observed a terminal failure via SSE
  AND a delayed sync-raise fires for the same `(feature_id,
  correlation_id)` shortly after, exactly one `build-failed` envelope
  is published. Implementation: the bridge's terminal-observation path
  marks the build "terminal-published" in the registry before invoking
  ack; F010F's safety-net checks the registry and skips its emit if the
  flag is set.
- AC-3: A **first-wins** invariant test asserts no race condition can
  produce two terminal envelopes for the same build, regardless of
  ordering: bridge-first / F010F-first / concurrent.
- AC-4: F010F's existing test suite (`tests/forge/test_safety_net_publish.py`
  or equivalent — verify path during implementation) continues to pass
  unchanged. No F010F production code is touched.
- AC-5: F010C correlation-id AST guard remains green.
- AC-6: All modified files pass project-configured lint/format checks
  with zero errors.

## Test requirements

- Sync-raise → F010F safety-net publish test (AC-1) — bridge's `attach()`
  is asserted **not** called.
- Concurrent sync-raise + bridge-terminal test (AC-2) — uses asyncio
  `gather` to fire both paths; asserts exactly one envelope on the
  wire.
- First-wins ordering test (AC-3) — three sub-cases: bridge wins,
  F010F wins, concurrent. All produce exactly one envelope.
- F010F regression suite passes unchanged.

## Files to Create

- `src/forge/lifecycle_bridge/coexistence.py`
- `tests/forge/lifecycle_bridge/test_coexistence.py`

## Files to Modify

- `src/forge/cli/_serve_deps.py`

## Implementation notes

- Touchpoints: `src/forge/lifecycle_bridge/coexistence.py` (new — owns
  the "terminal-published" flag);
  `src/forge/cli/_serve_deps.py` (existing F010F safety-net path checks
  the flag before publishing).
- The "terminal-published" flag is a column on `lifecycle_bridge_registry`
  (T2) — add via migration here if T2 didn't include it.
- Reference: F010F task file in `tasks/completed/TASK-FORGE-FRR-F010F/`
  for the existing safety-net publish shape.

## Coach validation commands

```bash
PYTHONPATH=src python -m pytest tests/forge/lifecycle_bridge/test_coexistence.py -x -v
PYTHONPATH=src python -m pytest tests/forge/test_safety_net_publish.py -x -v
PYTHONPATH=src python -m pytest tests/forge/test_pipeline_consumer_correlation_id.py -x -v
ruff check src/forge/lifecycle_bridge/coexistence.py
```

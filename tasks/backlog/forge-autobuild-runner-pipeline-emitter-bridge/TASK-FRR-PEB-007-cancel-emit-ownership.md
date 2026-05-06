---
id: TASK-FRR-PEB-007
title: "Cancel emit ownership — bridge synthesises build-cancelled on observed terminal=interrupted"
status: backlog
created: 2026-05-06T00:00:00Z
updated: 2026-05-06T00:00:00Z
priority: high
task_type: feature
parent_task: TASK-FORGE-FRR-F010M
parent_review: TASK-REV-F010M
feature_id: FEAT-PEBR
wave: 3
implementation_mode: task-work
complexity: 5
estimated_minutes: 60
dependencies:
  - TASK-FRR-PEB-005
tags:
  - forge-serve
  - autobuild-runner
  - pipeline-lifecycle-emitter
  - cancel-ownership
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Task: Cancel emit ownership — bridge synthesises build-cancelled on observed terminal=interrupted

## TL;DR

Implement operator cancellation as a single emit site (Q7 sub-option (b)
per scoping doc): forge's cancel handler calls
`runs.cancel(thread_id, run_id, action="interrupt")`; the bridge observes
the run reaching `terminal=interrupted` via SSE and emits
`pipeline.build-cancelled`. **Forge's cancel handler does not synthesise
the envelope directly** — only the bridge does.

Idempotency: two concurrent cancellation requests for the same in-flight
build must produce exactly one `build-cancelled` envelope (FEAT-FORGE-004
contract extended to the cancel path).

## Locks BDD scenarios

- @edge-case `An operator cancellation in-flight produces a
  build-cancelled envelope after the sidecar acknowledges interrupt`
  (ASSUM-006)
- @edge-case @regression `Two operator cancellation requests for the
  same in-flight build produce exactly one build-cancelled envelope`

## Acceptance criteria

- AC-1: A new `LifecycleBridge.request_cancel(feature_id)` method calls
  `runs.cancel(thread_id, run_id, action="interrupt")` on the
  langgraph-runner sidecar via the SDK and returns immediately. Does
  **not** publish the envelope synchronously.
- AC-2: T3's translator handles `interrupted` terminal SSE events and
  produces a `BuildCancelledPayload`; the bridge publishes
  `pipeline.build-cancelled` via the existing publisher path.
- AC-3: `BuildCancelledPayload` carries the inbound correlation-id.
- AC-4: Forge's existing cancel handler (verify path during
  implementation — likely `src/forge/cli/_serve_handlers.py` or
  similar) is updated to call `LifecycleBridge.request_cancel()`
  instead of synthesising `build-cancelled` directly. Synchronous
  envelope emission from the cancel handler is **removed**.
- AC-5: Concurrent cancel requests are idempotent: a "cancel-in-flight"
  flag on the registry row prevents a second SDK call; the second
  request is a no-op (logged at INFO).
- AC-6: F010C correlation-id AST guard remains green.
- AC-7: All modified files pass project-configured lint/format checks
  with zero errors.

## Test requirements

- Single-cancel test: cancel request → SDK call → SSE
  `terminal=interrupted` → exactly one `build-cancelled` envelope with
  correlation-id.
- Concurrent-cancel test: two cancel requests race; SDK called once;
  exactly one envelope; second request returns immediately (no-op).
- No-bridge fallback test: if no bridge is wired, the cancel handler's
  legacy path remains functional (preserves backward compatibility for
  test paths). Or: assert the legacy path is gone and tests must wire
  a bridge — design decision to make in implementation.

## Implementation notes

- Touchpoints: `src/forge/lifecycle_bridge/bridge.py` (add
  `request_cancel`); `src/forge/lifecycle_bridge/translation.py`
  (handle `interrupted` terminal); `src/forge/cli/_serve_handlers.py`
  (replace synchronous emit with bridge call).
- The cancel-in-flight flag is a column on `lifecycle_bridge_registry`;
  add via migration here if T2 didn't include it.
- Coordinate with T6 author: both tasks extend T3's translator; ensure
  no merge conflict on `translation.py`.

## Coach validation commands

```bash
PYTHONPATH=src python -m pytest tests/forge/lifecycle_bridge/test_cancel.py -x -v
PYTHONPATH=src python -m pytest tests/forge/test_serve_handlers.py -x -v -k cancel
PYTHONPATH=src python -m pytest tests/forge/test_pipeline_consumer_correlation_id.py -x -v
ruff check src/forge/lifecycle_bridge/bridge.py src/forge/cli/_serve_handlers.py
```

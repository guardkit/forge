---
id: TASK-FRR-PEB-002
title: LifecycleBridge skeleton + SQLite in-flight registry
status: in_review
created: 2026-05-06 00:00:00+00:00
updated: 2026-05-06 00:00:00+00:00
priority: high
task_type: feature
documentation_level: standard
parent_task: TASK-FORGE-FRR-F010M
parent_review: TASK-REV-F010M
feature_id: FEAT-PEBR
wave: 1
implementation_mode: task-work
complexity: 6
estimated_minutes: 90
dependencies:
- TASK-FRR-PEB-001
tags:
- forge-serve
- autobuild-runner
- pipeline-lifecycle-emitter
- bridge-skeleton
- sqlite-registry
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 2
  max_turns: 5
  worktree_path: /Users/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-PEBR
  base_branch: main
  started_at: '2026-05-07T10:12:12.287213'
  last_updated: '2026-05-07T10:35:22.970183'
  turns:
  - turn: 1
    decision: feedback
    feedback: "- Advisory (non-blocking): task-work produced a report with 2 of 3\
      \ expected agent invocations. Missing phases: 3 (Implementation). Consider invoking\
      \ these agents via the Task tool to strengthen stack-specific quality:\n- Phase\
      \ 3: `the stack-specific Phase-3 specialist` (Implementation)\n- Not all acceptance\
      \ criteria met:\n  \u2022 AC-1: `src/forge/lifecycle_bridge/bridge.py` exposes\
      \ a `LifecycleBridge`\n  \u2022 AC-2: A new `lifecycle_bridge_registry` SQLite\
      \ table is created via a\n  \u2022 AC-3: A `BridgeRegistry` repository class\
      \ exposes:\n  \u2022 AC-4: `attach()` writes a row; `detach()` deletes it; `list_active()`\n\
      \  \u2022 AC-5: F010C correlation-id contract: every `BridgeRegistry` operation\n\
      \  (1 more)"
    timestamp: '2026-05-07T10:12:12.287213'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
  - turn: 2
    decision: approve
    feedback: null
    timestamp: '2026-05-07T10:25:54.968204'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# Task: LifecycleBridge skeleton + SQLite in-flight registry

## TL;DR

Stand up the structural foundation for the SSE lifecycle bridge: a
`LifecycleBridge` class that owns the SSE connection lifecycle to the
langgraph-runner sidecar, plus a SQLite-backed in-flight registry that
persists `(feature_id, thread_id, run_id, last_event_id, ack_handle_token,
deadline_at)` per active build. No envelope translation yet (T3) and no
wire-up to forge serve startup yet (T4) — this is structural plumbing only.

The registry doubles as the source for `forge status --in-flight` (T12),
so the schema must support read-only queries efficiently.

## Acceptance criteria

- AC-1: `src/forge/lifecycle_bridge/bridge.py` exposes a `LifecycleBridge`
  class with public methods: `attach(build_context, ack_handle)`,
  `detach(feature_id)`, `recover_in_flight()`, `shutdown()`. No method
  body wires the SSE stream yet — those are stubs raising
  `NotImplementedError` to be filled by T3/T4/T9.
- AC-2: A new `lifecycle_bridge_registry` SQLite table is created via a
  migration in `src/forge/persistence/migrations/`. Schema:
  `feature_id TEXT PRIMARY KEY`, `thread_id TEXT NOT NULL`,
  `run_id TEXT NOT NULL`, `correlation_id TEXT NOT NULL`,
  `last_event_id TEXT`, `ack_handle_token TEXT NOT NULL`,
  `deadline_at TEXT NOT NULL`, `attached_at TEXT NOT NULL`,
  `current_lifecycle TEXT NOT NULL` (e.g. "queued", "running",
  "paused"), `updated_at TEXT NOT NULL`.
- AC-3: A `BridgeRegistry` repository class exposes:
  `record(entry)`, `update_lifecycle(feature_id, lifecycle, last_event_id?)`,
  `get(feature_id)`, `list_active()`, `delete(feature_id)`. All operations
  use the existing forge SQLite session pattern.
- AC-4: `attach()` writes a row; `detach()` deletes it; `list_active()`
  returns rows for `forge status --in-flight` (T12) with no SSE
  connection metadata leaking.
- AC-5: F010C correlation-id contract: every `BridgeRegistry` operation
  takes `correlation_id` explicitly; AST guard extension fixture is
  added to `tests/forge/test_pipeline_consumer_correlation_id.py` with
  the new bridge call sites listed.
- AC-6: All modified files pass project-configured lint/format checks
  with zero errors.

## Test requirements

- Unit test for each `BridgeRegistry` operation against an in-memory
  SQLite database.
- Migration test asserting the `lifecycle_bridge_registry` table is
  created on a fresh database.
- Concurrency test: two `attach()` calls for the same `feature_id`
  serialize correctly (second overwrites first or raises, design
  decision in implementation).

## Files to Create

- `src/forge/lifecycle_bridge/__init__.py`
- `src/forge/lifecycle_bridge/bridge.py`
- `src/forge/persistence/migrations/__init__.py`
- `src/forge/persistence/migrations/lifecycle_bridge_registry.py`
- `src/forge/persistence/repositories/__init__.py`
- `src/forge/persistence/repositories/bridge_registry.py`
- `tests/forge/lifecycle_bridge/__init__.py`
- `tests/forge/lifecycle_bridge/test_bridge.py`
- `tests/forge/persistence/__init__.py`
- `tests/forge/persistence/test_bridge_registry.py`

## Files to Modify

- `tests/forge/test_pipeline_consumer_correlation_id.py`

## Implementation notes

- Touchpoints: `src/forge/lifecycle_bridge/` (new package);
  `src/forge/persistence/migrations/` (new migration);
  `src/forge/persistence/repositories/` (new repository).
- Coordinate with T1 author: `ack_handle_token` is opaque to the
  registry; the consumer (T1) maps it back to the in-memory ack
  callback. Keeping the token-based indirection avoids serialising
  un-pickleable async callbacks into SQLite.
- The 300s per-build deadline (ASSUM-003 verified commitment) is
  written into `deadline_at` at `attach()`; T8 reads it.
- `current_lifecycle` is a simple string here — typed lifecycle states
  arrive in T3 with the SSE translation layer.

## Coach validation commands

```bash
PYTHONPATH=src python -m pytest tests/forge/lifecycle_bridge -x -v
PYTHONPATH=src python -m pytest tests/forge/persistence -x -v -k registry
ruff check src/forge/lifecycle_bridge/ src/forge/persistence/
```

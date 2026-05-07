---
id: TASK-FRR-PEB-012
title: "forge status --in-flight surface from the lifecycle bridge registry"
status: backlog
created: 2026-05-06T00:00:00Z
updated: 2026-05-06T00:00:00Z
priority: normal
task_type: refactor
documentation_level: standard
parent_task: TASK-FORGE-FRR-F010M
parent_review: TASK-REV-F010M
feature_id: FEAT-PEBR
wave: 5
implementation_mode: direct
complexity: 4
estimated_minutes: 45
dependencies:
  - TASK-FRR-PEB-009
tags:
  - forge-serve
  - forge-status
  - autobuild-runner
  - pipeline-lifecycle-emitter
  - operator-observability
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Task: `forge status --in-flight` surface from the lifecycle bridge registry

## TL;DR

Add a `--in-flight` flag to `forge status` that lists currently-active
autobuilds the lifecycle bridge is observing. Sourced from the same
`lifecycle_bridge_registry` SQLite table the bridge uses for recovery
(T2/T9) — no new persistence, no new in-memory mirror.

The output gives the operator a way to ask "where's my build?" mid-flight
between chat-REPL prompts. ASSUM-007 / Q6 sub-option (a) commitment.

## Locks BDD scenarios

- @edge-case `forge status surfaces in-flight builds the bridge is
  currently observing` (ASSUM-007)

## Acceptance criteria

- AC-1: `forge status --in-flight` queries
  `BridgeRegistry.list_active()` and renders one row per in-flight
  build with columns: `feature_id`, `build_id` (= `run_id`),
  `current_lifecycle`, `attached_at`, `deadline_at`, `correlation_id`.
- AC-2: Output format matches existing `forge status` table style
  (verify by running `forge status` against the current daemon).
- AC-3: When no builds are in-flight, output is `No in-flight builds.`
  (single line, exit code 0).
- AC-4: The flag combines cleanly with existing `forge status` flags
  (e.g. `forge status --in-flight --json` returns JSON).
- AC-5: Read-only — no mutations to the registry from this surface.
- AC-6: All modified files pass project-configured lint/format checks
  with zero errors.

## Test requirements

- Empty-registry test: `forge status --in-flight` outputs
  `No in-flight builds.`; exit 0.
- Populated-registry test: seed registry with 2 rows; output contains
  both `feature_id`s and lifecycle states.
- JSON-output test: `--in-flight --json` produces valid JSON parseable
  to a list of dicts.
- No-mutation test: invoke `--in-flight` 100 times; assert registry
  state unchanged.

## Files to Create

(none — surface uses `BridgeRegistry.list_active()` from T2)

## Files to Modify

- `src/forge/cli/status.py`
- `tests/forge/cli/test_status.py`

## Implementation notes

- Touchpoints: `src/forge/cli/status.py` (or wherever `forge status`
  lives — verify); `src/forge/persistence/repositories/bridge_registry.py`
  (use existing `list_active()` from T2).
- This is `direct` mode — small CLI surface change, no design phase
  needed.

## Coach validation commands

```bash
PYTHONPATH=src python -m pytest tests/forge/cli/test_status.py -x -v -k in_flight
ruff check src/forge/cli/status.py
forge status --in-flight  # smoke check against running daemon (manual)
```

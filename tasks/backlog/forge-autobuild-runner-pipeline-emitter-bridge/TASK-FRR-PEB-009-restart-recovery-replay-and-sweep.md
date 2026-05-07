---
id: TASK-FRR-PEB-009
title: "Restart recovery \u2014 Last-Event-ID replay + recovery sweep"
status: in_review
created: 2026-05-06 00:00:00+00:00
updated: 2026-05-06 00:00:00+00:00
priority: high
task_type: feature
documentation_level: standard
parent_task: TASK-FORGE-FRR-F010M
parent_review: TASK-REV-F010M
feature_id: FEAT-PEBR
wave: 4
implementation_mode: task-work
complexity: 7
estimated_minutes: 120
dependencies:
- TASK-FRR-PEB-008
tags:
- forge-serve
- autobuild-runner
- pipeline-lifecycle-emitter
- restart-recovery
- last-event-id
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 2
  max_turns: 5
  worktree_path: /Users/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-PEBR
  base_branch: main
  started_at: '2026-05-07T15:49:13.459099'
  last_updated: '2026-05-07T16:21:52.454035'
  turns:
  - turn: 1
    decision: feedback
    feedback: "- Advisory (non-blocking): task-work produced a report with 2 of 3\
      \ expected agent invocations. Missing phases: 3 (Implementation). Consider invoking\
      \ these agents via the Task tool to strengthen stack-specific quality:\n- Phase\
      \ 3: `the stack-specific Phase-3 specialist` (Implementation)\n- Plan audit\
      \ detected high-severity discrepancies \u2014 2 missing file(s): src/forge/persistence/migrations/lifecycle_bridge_published_lifecycles.py,\
      \ tests/forge/lifecycle_bridge/test_recovery_idempotency.py"
    timestamp: '2026-05-07T15:49:13.459099'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
  - turn: 2
    decision: approve
    feedback: null
    timestamp: '2026-05-07T16:11:00.338399'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# Task: Restart recovery — Last-Event-ID replay + recovery sweep

## TL;DR

Implement the bridge's restart-recovery flow:

1. **In-buffer replay** (ASSUM-001): on daemon startup, for each row in
   `lifecycle_bridge_registry`, reconnect to SSE with the persisted
   `last_event_id`; langgraph-api's server-side buffer replays the
   in-window envelopes. Idempotent — does not re-publish events whose
   transitions were already published.
2. **Out-of-buffer sweep** (ASSUM-002): if the SSE stream rejects the
   `Last-Event-ID` (buffer expired), fall back to `runs.get(thread_id,
   run_id)` once; if the run has reached terminal, publish the terminal
   envelope only and ack. If the run is still running, attach with a
   fresh `Last-Event-ID=0` and resume per-stage observation.

The recovery flow runs once per startup, before normal `build-queued`
processing resumes, so the chat REPL sees terminal envelopes for
in-flight builds before the operator's next prompt.

## Locks BDD scenarios

- @boundary `A forge daemon restart during an in-flight autobuild
  replays missed envelopes after the daemon resumes` (ASSUM-001)
- @boundary @edge-case `A forge daemon restart longer than the bridge's
  replay buffer still produces a terminal envelope` (ASSUM-002)
- @edge-case @regression `A daemon restart after build-started has
  been published does not re-publish build-started after recovery`
- @edge-case `A forge daemon restart with multiple in-flight builds
  reconciles every build's bridge`

## Acceptance criteria

- AC-1: `LifecycleBridge.recover_in_flight()` (stub from T2) is
  implemented. Iterates `BridgeRegistry.list_active()`; for each entry,
  schedules an asyncio task that reconnects with the persisted
  `Last-Event-ID`.
- AC-2: Idempotency: each registry row tracks `published_lifecycles`
  (set of envelope subjects already on the wire — e.g.
  `{"build-started"}`); the SSE observer's publish path checks this
  set before publishing and skips already-published transitions.
  Persisted as a JSON-encoded TEXT column.
- AC-3: When the SSE server rejects the `Last-Event-ID` (e.g. HTTP 410
  or empty replay window), the bridge falls back to `runs.get` once
  to determine current state. If terminal, publish the terminal
  envelope and ack. If still running, restart the SSE stream with
  `Last-Event-ID=0` (or whatever the SDK accepts as "from now").
- AC-4: `recover_in_flight()` is called from `forge serve` startup
  **before** the consumer starts processing new `build-queued`
  envelopes. Recovery completes within 30s for ≤10 in-flight builds.
- AC-5: Build-started is **not re-published** if it was already
  published pre-restart (the regression scenario explicitly listed).
- AC-6: Multi-build restart: 3 concurrent recoveries work without
  interference; each updates its own registry row.
- AC-7: All modified files pass project-configured lint/format checks
  with zero errors.

## Test requirements

- In-buffer replay test: stub SSE source replays 3 in-window events
  including a terminal; assert exactly 3 envelopes published; assert
  registry entry deleted.
- Out-of-buffer sweep test: stub SSE returns 410; stub `runs.get`
  returns terminal; assert exactly one terminal envelope published;
  registry entry deleted.
- Idempotency test: registry seeded with `published_lifecycles =
  {"build-started"}`; SSE replays a `build-started` event; assert NO
  duplicate `build-started` published; assert subsequent events still
  publish normally.
- Multi-build recovery test: seed 3 registry rows; assert all 3
  recovery tasks run concurrently; assert all 3 complete within 30s.
- Pre-startup-ordering test: `recover_in_flight()` completes before
  consumer starts processing new envelopes.

## Files to Create

- `src/forge/lifecycle_bridge/recovery.py`
- `src/forge/persistence/migrations/lifecycle_bridge_published_lifecycles.py`
- `tests/forge/lifecycle_bridge/test_recovery.py`
- `tests/forge/lifecycle_bridge/test_recovery_idempotency.py`

## Files to Modify

- `src/forge/lifecycle_bridge/bridge.py`
- `src/forge/cli/_serve_daemon.py`
- `tests/forge/test_cli_serve_daemon.py`

## Implementation notes

- Touchpoints: `src/forge/lifecycle_bridge/bridge.py`
  (`recover_in_flight` body); `src/forge/lifecycle_bridge/recovery.py`
  (new — replay vs sweep decision logic);
  `src/forge/persistence/migrations/` (add `published_lifecycles`
  column if T2 didn't);
  `src/forge/cli/_serve_daemon.py` (call `recover_in_flight` in
  startup ordering).
- The `published_lifecycles` set is the source-of-truth for what's
  already on the wire; the publisher path appends to it before
  invoking the actual NATS publish.

## Coach validation commands

```bash
PYTHONPATH=src python -m pytest tests/forge/lifecycle_bridge/test_recovery.py -x -v
PYTHONPATH=src python -m pytest tests/forge/lifecycle_bridge/test_recovery_idempotency.py -x -v
PYTHONPATH=src python -m pytest tests/forge/test_cli_serve_daemon.py -x -v -k recovery
ruff check src/forge/lifecycle_bridge/recovery.py
```

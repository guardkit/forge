---
id: TASK-NFI-010
title: Contract + seam tests (nats_client mock, FleetEventSink seam, terminal-ack
  invariant)
task_type: testing
status: in_review
priority: high
created: 2026-04-24 00:00:00+00:00
updated: 2026-04-24 00:00:00+00:00
parent_review: TASK-REV-NF20
feature_id: FEAT-FORGE-002
wave: 5
implementation_mode: task-work
complexity: 5
dependencies:
- TASK-NFI-004
- TASK-NFI-005
- TASK-NFI-006
- TASK-NFI-007
- TASK-NFI-009
tags:
- testing
- contract-tests
- seam-tests
- boundary-tests
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002
  base_branch: main
  started_at: '2026-04-25T14:49:38.892222'
  last_updated: '2026-04-25T15:05:38.881063'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-25T14:49:38.892222'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# Task: Contract + seam tests

## Description

Consolidate cross-module contract tests at the `nats_client` boundary and
the `FleetEventSink` seam. Each upstream subtask (004–009) has its own
unit tests; this task owns the *boundary* tests that guarantee
integration.

Test categories:

1. **`nats_client` boundary contracts** (assert publisher methods called with correct
   subject + envelope shape + `source_id="forge"` + correlation_id threading)
2. **`FleetEventSink` seam** (watcher → discovery cache delegation; asyncio.gather racing tests)
3. **Terminal-ack invariant** (pipeline_consumer ack called only on COMPLETE/FAILED/CANCELLED/SKIPPED; never on RUNNING/PAUSED/FINALISING)
4. **Secret-free manifest** (already in TASK-NFI-002; duplicate here at integration level)
5. **Clock injection coverage** (assert no `datetime.now()` or `asyncio.sleep(real)` calls in production code paths)
6. **Publish-failure tolerance** (publish raising → SQLite not rolled back)

## Acceptance Criteria

- [ ] Contract test: every `pipeline_publisher` method sends an envelope with `source_id == "forge"` and `correlation_id == payload.correlation_id`
- [ ] Contract test: `fleet_publisher.register_on_boot` calls `nats_client.register_agent(FORGE_MANIFEST)` exactly once with the manifest unchanged
- [ ] Contract test: `fleet_publisher.heartbeat_loop` calls `nats_client.heartbeat(...)` at cadence == `heartbeat_interval_seconds` (Clock-driven, not wall-clock)
- [ ] Seam test: `fleet_watcher.on_event(fleet.register)` → `sink.upsert_agent(manifest)` called; similar for deregister and heartbeat
- [ ] Racing seam test: 100 concurrent register+deregister pairs via `asyncio.gather` → cache ends in consistent state (one-event-wins)
- [ ] Terminal-ack invariant test: state machine runs through RUNNING → PAUSED → RUNNING → COMPLETE; `Msg.ack` is called exactly once, at COMPLETE
- [ ] Publish-failure tolerance test: mock `nats_client.publish` to raise on `build-started`; assert SQLite row still shows RUNNING after the raise
- [ ] Clock hygiene test: grep-based assertion that `src/forge/adapters/nats/` and `src/forge/discovery/` contain no `datetime.now()` or raw `asyncio.sleep(` calls (use `clock.now()` and `clock.sleep(` / `await event.wait()` instead)
- [ ] All tests pass in CI; coverage >= 80% for `forge.adapters.nats.*` and `forge.discovery.*`

## Implementation Notes

- Use `unittest.mock.AsyncMock` for `nats_client`
- `FakeClock` helper in `tests/helpers/fake_clock.py` — single `now()` method backed by a mutable `datetime`; `advance(seconds)` increments it
- Terminal-ack test is the single most load-bearing test in this feature — invest in clarity

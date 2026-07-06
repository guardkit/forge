---
id: TASK-MP-008
title: Planning intake consumer (ack-on-persist, trust-boundary validation, dedup, poison-pill)
task_type: integration
status: backlog
parent_review: TASK-REV-83E4
feature_id: FEAT-3ED2
feature_ref: FEAT-SPL-002
wave: 2
implementation_mode: task-work
complexity: 5
estimated_minutes: 70
dependencies: [TASK-MP-001, TASK-MP-002, TASK-MP-003]
tags: [mode-p, nats, intake]
consumer_context:
  - task: TASK-MP-002
    consumes: SqlitePlanningRunStore
    framework: "sqlite3 (house SQLite adapter pattern)"
    driver: "sqlite3"
    format_note: "record_queued must be idempotent on correlation_id and return a DuplicateRun sentinel distinguishing terminal vs non-terminal existing runs"
---

# TASK-MP-008 — Planning intake consumer

## Description

The planning front door — a SEPARATE handler + durable consumer that never calls
`dispatch_build`/`maybe_gate_build` (RT-06: reusing the build handler would inherit
the pre-PO gate addressed to 'rich', held-slot acking, terminal-only dedup, and
adapter-omission rejection). Mirrors `pipeline_consumer.py`'s shape. Validates
`PlanningQueuedPayload` (imported from `nats_core.events` — frozen 0.5.0 contract),
**validates correlation_id at the trust boundary** (RT-03: the wire contract
applies NO charset/length validation, yet the value becomes a run key, path
segment, branch name, and subject token), records the run, and **acks after
persist** (ASSUM-015 — deliberate divergence from the build held-slot invariant;
recovery is store-driven via TASK-MP-009's sweeps, never redelivery-driven).

## BDD Scenarios

- "A queued planning request starts a durable planning run" (intake half)
- "A malformed planning request is rejected without wedging intake"
- "A planning request with an invalid correlation id is rejected without wedging intake"
- "A redelivered planning request does not create a second run"
- "Planning intake and build intake coexist without interference" (filter half)

## Files

- Creates: `src/forge/adapters/nats/planning_consumer.py` (`PLANNING_QUEUED_SUBJECT_FILTER = "pipeline.planning-queued.*"`, `PLANNING_DURABLE_NAME = "forge-serve-planning"`, `PlanningConsumerDeps` frozen dataclass, `handle_planning_message`, `CORRELATION_ID_PATTERN` validation)
- Tests: `tests/forge/adapters/test_planning_consumer.py` (follow where pipeline_consumer tests live)

## Acceptance Criteria

- [ ] Valid payload -> planning_runs row QUEUED keyed by correlation_id, `originating_user` verbatim, `expected_approver` initialised to `originating_user`; fake `msg.ack()` called exactly once AFTER the store write (call-order predicate)
- [ ] Malformed bytes -> ack + zero rows + rejection logged; a subsequent valid message on the same consumer processes normally (no-wedge predicate)
- [ ] Invalid correlation_id (blank, > 128 chars, or failing `^[A-Za-z0-9._-]+$` — i.e. containing `/`, `..` as traversal, `~^:?*[`, whitespace, or subject-token dots are REJECTED since '.' breaks NATS subject tokens and git refs) -> ack + zero rows + rejection logged (RT-03 trust boundary)
- [ ] Redelivered correlation_id (non-terminal existing run) -> ack, still exactly one row; duplicate of a TERMINAL run -> ack + a notification published back to the originator (RT-10 — no silent drop of a human's retry; uses TASK-MP-006's notifications builder or a minimal local payload)
- [ ] `PLANNING_QUEUED_SUBJECT_FILTER` does not subject-match `BUILD_QUEUED_SUBJECT_FILTER` and vice versa (string-level subject-match unit test — PS-003 err-10100 guard); the module never imports `dispatch_build`/`maybe_gate_build`/`pipeline_consumer` handler internals (import-predicate test)
- [ ] `handle_planning_message` never raises (exception-swallow predicate mirroring the pipeline_consumer contract); `originating_adapter` absence is tolerated for `triggered_by='jarvis'` (accept-with-log — the wire layer does not enforce it; digest fact 6)
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Test Requirements

- Unit/integration with `_MsgLike` fakes + tmp_path SQLite (crib the
  pipeline_consumer test fixtures); zero live NATS imports in tests.

## Implementation Notes

- Ack-on-persist rationale is load-bearing: escalation windows (hours) exceed the
  1h ack_wait; holding the slot would redeliver into the FWD-003 wedge class.
- Consumer attach happens in TASK-MP-009 beside `_serve_daemon._attach_consumer`;
  this task delivers the handler + deps + constants and their tests.

---
id: TASK-PSM-006
title: Sequential per-project queue picker
task_type: feature
parent_review: TASK-REV-3EEE
feature_id: FEAT-FORGE-001
wave: 3
implementation_mode: direct
complexity: 4
estimated_minutes: 60
status: completed
dependencies:
- TASK-PSM-005
consumer_context:
- task: TASK-PSM-002
  consumes: SCHEMA_INITIALIZED
  framework: sqlite3 (stdlib)
  driver: stdlib
  format_note: STRICT tables; WAL+STRICT pragmas applied per-connection; supports
    BEGIN IMMEDIATE for atomic picker queries
tags:
- lifecycle
- queue
- sequencing
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-001
  base_branch: main
  started_at: '2026-04-27T13:46:01.323525'
  last_updated: '2026-04-27T13:53:32.816057'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-27T13:46:01.323525'
    player_summary: 'Created src/forge/lifecycle/queue.py with the SqliteSequentialQueuePicker
      class and module-level next_build_to_pick/is_project_busy convenience wrappers.
      The picker composes SqliteLifecyclePersistence (sharing the writer connection
      that apply_transition uses, so BEGIN IMMEDIATE serialises pick attempts at the
      SQLite engine level). next_build_to_pick wraps the decision in BEGIN IMMEDIATE
      / COMMIT (rollback on sqlite3.Error): step 1 queries for any build in BLOCKING_STATES
      = {PREPARING, RUNNING, P'
    player_success: true
    coach_success: true
reconciled: "2026-07-11 WS3-S8 sweep — TASK-PSM-006: feature-rollup completed"
---

# Task: Sequential per-project queue picker

## Description

Create `src/forge/lifecycle/queue.py` exporting the per-project sequential
build picker. ASSUM-004 (confirmed) scopes sequential discipline as
**per-project**: only one build runs at a time per project; different
projects can run concurrently.

The pull consumer (already shipped in `adapters/nats/pipeline_consumer.py`
from FEAT-FORGE-002) will call this picker on every received message; the
picker decides whether to ACK and start preparation, or NACK and let
JetStream redeliver later.

API:

- `next_build_to_pick(project: str | None) -> Build | None` — returns the
  oldest QUEUED build for the project iff no other build for that project
  is in a non-terminal state, else None
- `is_project_busy(project: str | None) -> bool` — convenience predicate
  used by the consumer handler

`project=None` is the fleet-wide scope (per-NULL semantics); two None
projects are still considered the same scope.

## Acceptance Criteria

- [ ] `next_build_to_pick(project)` returns None if any build for the
      project is in {QUEUED-pending-this-pick, PREPARING, RUNNING, PAUSED,
      FINALISING} EXCEPT the one being picked
- [ ] `next_build_to_pick(project)` returns the oldest-by-`queued_at`
      QUEUED build for the project when nothing else is in flight
- [ ] Picker query runs inside a `BEGIN IMMEDIATE` transaction to acquire
      the write lock atomically — prevents two consumer instances from
      both picking the same build (Group F "two simultaneous queues")
- [ ] `is_project_busy(project=None)` correctly handles the fleet-wide
      scope (NULL `project` column compares equal to NULL via
      `IS NULL` predicates, not `=`)
- [ ] Unit test: seed one PREPARING + one QUEUED for project X →
      `next_build_to_pick("X")` returns None
- [ ] Unit test: seed one COMPLETE + one QUEUED for project X →
      `next_build_to_pick("X")` returns the QUEUED build
- [ ] Unit test: seed one RUNNING for project X + one QUEUED for project Y
      → `next_build_to_pick("Y")` returns the QUEUED Y build (different
      project = unblocked)
- [ ] Unit test: seed two QUEUED for project X with different
      `queued_at` → picker returns the older one
- [ ] All modified files pass project-configured lint/format checks with
      zero errors

## Implementation Notes

- The query must use `IS NULL` for the fleet-wide case, not `= NULL`
  (SQLite NULL semantics).
- Keep this module thin — it's purely query logic. The pull consumer's
  handler (in `adapters/nats/pipeline_consumer.py`) is unchanged in this
  feature; it will gain a delegation point in a separate task that wires
  the picker in. Callers of `next_build_to_pick` are responsible for ACK /
  NACK dispatch.

## Coach Validation

- `queue.py` exists with `next_build_to_pick` and `is_project_busy`
- `BEGIN IMMEDIATE` used for the picker query (verifiable in source)
- NULL project handling correct (`IS NULL`)
- Unit tests cover the four scenarios listed in AC
- Lint/format pass

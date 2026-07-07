---
id: TASK-MP-002
title: planning_runs durable store (additive schema_v3 + state machine + history)
task_type: feature
status: completed
parent_review: TASK-REV-83E4
feature_id: FEAT-3ED2
feature_ref: FEAT-SPL-002
wave: 1
implementation_mode: task-work
complexity: 6
estimated_minutes: 85
dependencies: []
tags:
- mode-p
- persistence
- sqlite
autobuild_state:
  current_turn: 1
  max_turns: 5
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-3ED2
  base_branch: main
  started_at: '2026-07-06T12:58:39.986049'
  last_updated: '2026-07-06T13:09:40.917407'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-07-06T12:58:39.986049'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# TASK-MP-002 — planning_runs durable store (additive schema_v3 + state machine + history)

## Description

Planning runs get their own durable records keyed by `correlation_id` — the panel
refuted builds-row reuse on hard evidence (builds has NOT NULL
feature_id/repo/branch/feature_yaml_path, schema.sql:15-21; build_id derives from
feature identity, persistence.py:658; the STRICT-table mode CHECK cannot be altered,
schema_v2.sql:23-24; TRANSITION_TABLE has no PLANNED-HANDOFF terminal). History gets
a sibling table because `stage_log.build_id` has an **enforced FK to builds**
(schema.sql:68 + PRAGMA foreign_keys=ON in adapters/sqlite/connect.py:73).

## BDD Scenarios

- "A queued planning request starts a durable planning run" (store half)
- "The planning run's history records every transition with its identities"
- "A message-bus outage during a paused run does not lose the run" (durability half)
- "A redelivered planning request does not create a second run" (dedup primitive)
- "Planning runs never consult a reasoning model to advance the chain" (history substrate)

## Files

- Creates: `src/forge/lifecycle/schema_v3.sql` (ADDITIVE ONLY — `CREATE TABLE planning_runs` + `CREATE TABLE planning_run_events`; zero edits to builds/stage_log), `src/forge/planning/states.py`, `src/forge/planning/run_store.py`
- Modifies: `src/forge/lifecycle/migrations.py` (schema version 2 -> 3, loader entry — mirror the existing (2, "schema_v2.sql") registration shape)
- Tests: `tests/forge/planning/test_run_store.py`, `tests/forge/planning/test_states.py`, `tests/forge/lifecycle/` or `tests/unit/lifecycle/` migration test (follow where existing migration tests live)

## Schema (STRICT tables, matching house style)

`planning_runs`: `correlation_id TEXT PRIMARY KEY`, `state TEXT NOT NULL CHECK (state IN ('QUEUED','RUNNING','PAUSED','FAILED','CANCELLED','TIMED_OUT','PLANNED_HANDOFF'))`,
`originating_user TEXT NOT NULL`, `expected_approver TEXT NOT NULL`,
`request_text TEXT NOT NULL`, `target_repo TEXT`, `triggered_by TEXT NOT NULL`,
`originating_adapter TEXT`, `parent_request_id TEXT`,
`pending_approval_request_id TEXT`, `defer_count INTEGER NOT NULL DEFAULT 0`,
`paused_at TEXT`, `escalated_at TEXT`, `handoff_branch TEXT`, `handoff_path TEXT`,
`queued_at TEXT NOT NULL`, `started_at TEXT`, `completed_at TEXT`, `error TEXT`.

`planning_run_events`: autoincrement id, `correlation_id` FK ->
planning_runs(correlation_id), `stage_label TEXT NOT NULL`, `status TEXT NOT NULL`,
`gate_mode TEXT`, `coach_score REAL`, `actor_identity TEXT`, `details_json TEXT`,
`recorded_at TEXT NOT NULL` — mirrors stage_log's column SHAPE for FEAT-SPL-005
trace-capture continuity.

## Acceptance Criteria

- [ ] Migration is additive: applying v3 to a v2 database leaves builds/stage_log schemas byte-identical (PRAGMA-based schema assertion in a test that applies v2 then v3 to a tmp_path db)
- [ ] `SqlitePlanningRunStore.record_queued(payload_fields)` is idempotent on correlation_id: second call returns a `DuplicateRun` sentinel (distinguishing terminal vs non-terminal existing state, for TASK-MP-008's RT-10 notification), creates no second row
- [ ] Every state transition writes a `planning_run_events` row carrying the acting identity (originator on QUEUED, decided_by on checkpoint decisions)
- [ ] Transitions enforce a PLANNING_TRANSITIONS map (`states.py` StrEnum + dict, mirroring the state_machine.py sole-producer pattern) via **CAS**: `UPDATE planning_runs SET state=? ... WHERE correlation_id=? AND state=?` with affected-rows discipline — affected==1 wins, ==0 returns a refused sentinel (never raises); terminal states (FAILED/CANCELLED/TIMED_OUT/PLANNED_HANDOFF) accept no transitions
- [ ] Durability: rows written by one store instance are read back by a second instance opened on the same tmp_path SQLite file; `originating_user` and `expected_approver` persist verbatim
- [ ] `defer_count`, `paused_at`, `escalated_at`, `expected_approver` are all updatable columns (durable escalation state — RT-04)
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Test Requirements

- Unit tests against tmp_path SQLite files (house pattern); migration test applies
  v2 then v3; CAS race test fires two competing transitions and asserts exactly one
  winner (this primitive is consumed by TASK-MP-005's race scenario — PS-005).

## Implementation Notes

- The CAS transition IS the approve-vs-escalation race arbitration primitive; get
  its affected-rows semantics right here so later tasks only consume it.
- Do NOT edit `schema.sql`/`schema_v2.sql`/`state_machine.py`/`persistence.py`
  beyond the migrations.py registration. No BuildMode changes anywhere.

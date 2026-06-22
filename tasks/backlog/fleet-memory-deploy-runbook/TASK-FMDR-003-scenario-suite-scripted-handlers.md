---
id: TASK-FMDR-003
title: "Scenario test suite for the fleet-memory runbook (scripted handlers, CI-safe)"
status: backlog
created: 2026-06-22 00:00:00+00:00
priority: high
task_type: testing
documentation_level: standard
parent_review: TASK-REV-FMDR
feature_id: FEAT-FMDR
wave: 2
implementation_mode: task-work
complexity: 6
estimated_minutes: 120
dependencies:
  - TASK-FMDR-001
  - TASK-FMDR-002
tags:
  - forge-output-loop
  - runbook-executor
  - bdd
  - security
  - concurrency
test_results:
  status: pending
  coverage: null
  last_run: null
---

# TASK-FMDR-003 — Scenario suite (scripted handlers, no live Docker)

## Summary

Cover the **deterministic** behavioural scenarios of the fleet-memory runbook without
standing up real infrastructure. Use the real `deploy_compose` / `run_smoke_tests`
handlers pointed at **tiny stub scripts** (a `deploy.sh`/`smoke.sh` that `exit 0` or
`exit 1` on demand, and one that prints a planted Postgres DSN), plus a capturing fake
NATS client for event-order assertions. Fully CI-safe.

The executor's resume / claim-lease / crash-recovery machinery is owned upstream and is
**not re-implemented** here — these tests verify it behaves correctly *with the
fleet-memory runbook as the subject*.

## Acceptance Criteria

- [ ] **Failed deploy halts before smoke** (C1): stub `deploy.sh` exits non-zero → deploy
      recorded `failed`, smoke step never runs, runbook halts/escalates at the deploy step.
- [ ] **Failing smoke halts at smoke** (C2): deploy passes, stub `smoke.sh` exits non-zero
      → deploy `passed`, smoke `failed`, runbook escalates at the smoke step rather than
      completing.
- [ ] **Credential scoping** (C3, @security): a stub that emits a Postgres DSN +
      `PGPASSWORD=` → the persisted step results and the captured published events contain
      **neither** the password nor the connection string.
- [ ] **Missing env file** (C4): `.env.deploy` absent → deploy recorded `failed` with a
      reason indicating the deploy environment file could not be found.
- [ ] **Resume after deploy** (B1): a run stopped after the deploy step is recorded
      `passed` re-enters at the smoke step (deploy not re-run) and completes from there.
- [ ] **No-op on complete** (D2): re-running an already-complete runbook re-runs no step
      and reports "already complete".
- [ ] **Result-before-advance crash** (D5): deploy recorded `passed` but pointer not yet
      advanced → on re-run the deploy step is recognised already-passed and skipped;
      executor resumes at the smoke step.
- [ ] **Ordered event stream + queryable record** (D6): events publish in order
      started → … → complete (capturing fake client); each step's status is queryable
      from the persisted record afterwards.
- [ ] **Two executors never deploy twice** (D7, @concurrency): two executors against the
      same runbook → exactly one runs the deploy step.
- [ ] **Diagnosable permission failure** (D4): stub `deploy.sh` emits a
      permission-denied message + non-zero exit → deploy `failed` and the captured output
      is distinguishable as a permissions problem, not a generic error.

## Coach Validation

- `pytest tests/bdd/test_fleet_memory_runbook.py -v` (or `tests/forge/...` — match repo
  convention)
- Confirm no test reaches out to Docker, the NAS, or a live broker.

## Implementation Notes

- Write stub scripts into `tmp_path` and point the runbook `cwd` at it; this exercises
  the real subprocess handlers cheaply.
- For the planted-secret test, assert against both `StepResult.captured_output` in the
  persisted record and the payloads handed to the capturing NATS client.
- For concurrency, drive two `RunbookExecutor.run(...)` coroutines against one runbook id
  and assert exactly one deploy invocation (count handler calls).

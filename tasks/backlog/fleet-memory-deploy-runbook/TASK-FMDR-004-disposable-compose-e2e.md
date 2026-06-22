---
id: TASK-FMDR-004
title: "End-to-end run against the disposable deploy/local compose target"
status: backlog
created: 2026-06-22 00:00:00+00:00
priority: high
task_type: testing
documentation_level: standard
parent_review: TASK-REV-FMDR
feature_id: FEAT-FMDR
wave: 2
implementation_mode: task-work
complexity: 5
estimated_minutes: 90
dependencies:
  - TASK-FMDR-001
  - TASK-FMDR-002
  - TASK-FMDR-006
tags:
  - forge-output-loop
  - integration-test
  - fleet-memory
  - docker-compose
test_results:
  status: pending
  coverage: null
  last_run: null
---

# TASK-FMDR-004 — Disposable-compose end-to-end run

## Summary

The automated payoff coverage: a **marker-gated** integration test that runs the
fleet-memory runbook through `forge runbook run` against the disposable
`fleet-memory/deploy/local` compose target (ASSUM-003, OD-4) — proving
deploy → smoke → runbook-complete unattended, with the smoke gates G3–G5 green.
The real NAS is kept out of CI (that is TASK-FMDR-005, operator_handoff).

> ⚠️ **Infrastructure dependency.** The local `deploy.sh` / `smoke.sh` wrappers are added
> by **TASK-FMDR-006** in the sibling `fleet-memory` repo (consumed as-is — an AutoBuild
> worktree cannot edit them; project memory: "Autobuild can't edit sibling repos"). They
> must be committed before this task runs.
>
> **Docker is a hard pre-requisite** (decision: this build host has Docker Desktop). If
> the Docker daemon is **down**, this task **fails with a clear message** ("start Docker
> Desktop") rather than skipping — we want a real green from the e2e on this machine.
> Note: Docker Desktop must be *running* (`docker version` reaches the daemon), not merely
> installed.

## Acceptance Criteria

- [ ] **Deploy → verify → complete** (A2): against the disposable compose target, the
      deploy step stands the service up and is recorded `passed`; the smoke step then runs
      and is recorded `passed`; the runbook completes with no manual step in between.
- [ ] **Green run = smoke gates satisfied** (A4): when the smoke step passes, the run
      reflects Postgres-with-pgvector reachable (G3), network path confirmed (G4), and the
      data volume backed up (G5) — i.e. `smoke.sh` exit 0 is the verdict.
- [ ] **Idempotent re-deploy** (B3): running the deploy step again against an
      already-healthy disposable target records `passed` and leaves the running service
      unchanged.
- [ ] The test tears the disposable target down afterward (no leaked containers/volumes).
- [ ] When the Docker daemon is unreachable, the test **fails** with an actionable message
      ("start Docker Desktop") — it does **not** skip.
- [ ] When the `../fleet-memory/deploy/local/{deploy.sh,smoke.sh}` wrappers (TASK-FMDR-006)
      are absent, the test fails with a clear message pointing at TASK-FMDR-006.

## Coach Validation

- `pytest tests/integration/test_fleet_memory_e2e.py -m integration -v` (or repo
  convention)
- When Docker is present: assert the run reports "completed successfully" and both steps
  `passed`.

## Implementation Notes

- Resolve the sibling path relative to the forge repo root; skip with a clear message if
  it is missing.
- Reuse the `forge runbook run` CLI entry (wired in TASK-FMDR-002) rather than calling the
  executor directly — this is the end-to-end seam under test.
- Prefer `deploy/local`'s ephemeral compose; never point this test at the NAS.

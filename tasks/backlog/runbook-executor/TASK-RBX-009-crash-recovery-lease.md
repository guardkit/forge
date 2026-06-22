---
id: TASK-RBX-009
title: "Executor crash-recovery for steps stuck in 'running'"
status: backlog
created: 2026-06-22T00:00:00Z
updated: 2026-06-22T00:00:00Z
priority: medium
task_type: feature
parent_review: TASK-REV-RBX-001
parent_feature: FEAT-RBX
feature_slug: runbook-executor
complexity: 5
dependencies: []
tags:
  - forge
  - runbook
  - executor
  - concurrency
  - reliability
---

# Executor crash-recovery for steps stuck in 'running'

## Background

The no-double-run guarantee (TASK-RBX-007, fixed in `1221ada`) is implemented
with an atomic claim: `try_claim_step_for_execution` transitions a runnable step
(`pending` / `failed` / `awaiting_approval`) → `running`, and the executor only
runs a step it claimed. Within a single run, `running` is transient — the step
moves to `passed` (and the pointer advances) or `failed`/`awaiting_approval`
(and the run stops) before the next iteration.

**Gap:** if an executor **crashes after claiming a step but before recording its
outcome**, the step is left `running`. On the next run:

- The recovery shortcut only fast-advances `passed` steps.
- `try_claim_step_for_execution` does **not** accept `running` (by design — a
  `running` step is assumed owned by a live concurrent executor).

So the resumed run can neither claim nor advance past the stuck step → the
`while True` loop **busy-spins** (or, with no other executor, never progresses).
The in-memory tests don't hit this (no crashes), so it's latent.

## Scope

Add crash-recovery so a `running` step abandoned by a dead executor can be
reclaimed, **without** breaking the live-concurrency guarantee. Evaluate:

- **Lease/heartbeat:** stamp `claimed_at` (+ optional owner id) when claiming;
  treat a `running` step whose lease has expired as reclaimable. Requires a
  schema column and a clock.
- **Explicit recovery sweep:** a `forge runbook recover <id>` (or a startup
  pass) that resets stale `running` steps to `failed`/`pending` after an
  operator-confirmed timeout.
- **Backoff guard (minimum):** if the executor observes the same `running` step
  at the pointer across N iterations with no progress, stop and escalate
  (`reason="stalled"`) instead of busy-spinning — cheap safety net even before a
  full lease lands.

Pick the approach with the team; ship at least the backoff guard so a stuck
`running` step can never hot-spin.

## Acceptance Criteria

- [ ] A runbook with a step left in `running` (simulated crash) can make
      progress on a subsequent run (reclaimed or escalated), never busy-spinning.
- [ ] The live two-executor no-double-run guarantee (TASK-RBX-007) still holds —
      a genuinely in-flight `running` step is NOT stolen by a concurrent executor.
- [ ] If a lease/owner column is added, the migration is idempotent and the
      repo seam tests cover the reclaim path.
- [ ] All modified files pass project-configured lint/format checks.

## Notes

- Reference: `src/forge/executor/executor.py` (the `while True` loop, recovery
  shortcut, claim/skip), `src/forge/persistence/repositories/runbook.py`
  (`try_claim_step_for_execution`).
- This is the distributed-lock-without-lease problem; a heartbeat lease is the
  standard solution but the backoff guard is the minimum viable safety net.

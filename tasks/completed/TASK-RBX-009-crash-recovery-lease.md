---
id: TASK-RBX-009
title: "Executor crash-recovery for steps stuck in 'running'"
status: completed
created: 2026-06-22T00:00:00Z
updated: 2026-06-22T00:00:00Z
completed: 2026-06-22T00:00:00Z
previous_state: in_review
completed_location: tasks/completed/TASK-RBX-009-crash-recovery-lease.md
chosen_approach: "lease/heartbeat (claimed_at + claimed_by) with backoff/stalled safety net"
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

- [x] A runbook with a step left in `running` (simulated crash) can make
      progress on a subsequent run (reclaimed or escalated), never busy-spinning.
- [x] The live two-executor no-double-run guarantee (TASK-RBX-007) still holds —
      a genuinely in-flight `running` step is NOT stolen by a concurrent executor.
- [x] If a lease/owner column is added, the migration is idempotent and the
      repo seam tests cover the reclaim path.
- [x] All modified files pass project-configured lint/format checks.

## Notes

- Reference: `src/forge/executor/executor.py` (the `while True` loop, recovery
  shortcut, claim/skip), `src/forge/persistence/repositories/runbook.py`
  (`try_claim_step_for_execution`).
- This is the distributed-lock-without-lease problem; a heartbeat lease is the
  standard solution but the backoff guard is the minimum viable safety net.

## Implementation (chosen approach: lease/heartbeat + backoff guard)

Shipped the full lease, with the backoff guard as the companion that the lease
alone doesn't provide (while a `running` step's lease is *unexpired*, the claim
still fails, so the loop would hot-spin until expiry without it).

- **Schema** (`migrations/runbook.py`): added nullable `claimed_at` (ISO-8601)
  and `claimed_by` columns to `runbook_steps`, mirroring the existing
  `coexistence.py` lease idiom. Fresh DBs get them in the `CREATE TABLE`; older
  DBs are upgraded by a guarded `ALTER TABLE ... ADD COLUMN`
  (`_ensure_claim_lease_columns`) so `apply()` stays idempotent across schema
  versions.
- **Repo** (`repositories/runbook.py`): `try_claim_step_for_execution` now
  stamps `claimed_at`/`claimed_by` and treats a `running` step whose
  `claimed_at` is NULL or older than `lease_seconds` (default
  `DEFAULT_CLAIM_LEASE_SECONDS = 900`) as reclaimable — all inside the one
  atomic `BEGIN IMMEDIATE` UPDATE, so the no-steal guarantee holds. A live-lease
  `running` step is still refused (no double-run).
- **Executor** (`executor/executor.py`): passes the lease to the claim; on a
  failed claim it backs off (`asyncio.sleep`) instead of hot-spinning, and after
  `max_stall_cycles` no-progress cycles on the same step stops with
  `RunResult(reason="stalled")`. `step_started` now fires once per claimed step
  (and once for an unknown handler) rather than on every poll. The default
  `max_stall_cycles` is derived from lease/backoff so the lease-reclaim path
  always wins over the stall net for a crashed peer.

### Known limitations / follow-ups

- **Lease window vs. handler duration**: the executor does not heartbeat while a
  handler runs, so a handler that outlives `claim_lease_seconds` could have its
  in-flight step reclaimed by a peer (double-run). The 15-min default leaves
  headroom for the in-process handlers and the FEAT-SSH shell-step handlers;
  a periodic claim-renewal during long handlers is the robust follow-up.
- **`stalled` not on the wire**: the `stalled` reason is not published as a NATS
  escalated event because the sibling `nats_core` `EscalatedPayload.reason`
  Literal does not include it (and that package is out of scope here). It is
  logged and surfaced via `RunResult`/the CLI. Adding `stalled` to the
  `EscalatedPayload` Literal in `nats_core` is a small follow-up.

### Tests added

- `tests/forge/persistence/test_runbook_claim_lease.py` — repo seam tests:
  lease stamping, expired-lease + NULL-lease reclaim, live-lease no-steal,
  passed-step refusal.
- `tests/forge/persistence/test_runbook_migration.py` — re-apply keeps lease
  columns singular; guarded ALTER upgrades a pre-RBX-009 table.
- `tests/forge/executor/test_executor.py` — crash-recovery reclaim end-to-end;
  un-reclaimable `running` step escalates `stalled` without busy-spinning.

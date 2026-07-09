---
id: TASK-FWD-003
title: "Duplicate-active-build guard + un-acked redelivery = permanent dispatch wedge"
status: in_review
created: 2026-07-04T11:00:00Z
updated: 2026-07-09T00:00:00Z
priority: high
task_type: feature
tags: [wire-dispatch, pipeline-consumer, found-2026-07-04, ws3-s6]
complexity: 5
---

> **✅ DONE 2026-07-09 (WS3-S6) — the 2026-07-06 restart-mid-dispatch freeze
> reproduced then green.** Decision (AC1): a redelivery whose builds row is
> QUEUED (or INTERRUPTED) is RUNLESS — no live run streams it, no pause owns
> the ack — so `dispatch_build` re-dispatches on the existing build_id
> (`maybe_gate_build` drives QUEUED/INTERRUPTED forward) instead of the old
> skip-WITHOUT-ack, which under `max_ack_pending=1` wedged the single
> consumer until the 1h `ack_wait` expiry (deploy-record `c042bee` + the
> `123f1f7` unfreeze note). PAUSED stays held-slot (a pause owns the ack).
> Stale-row hygiene (AC2): `reconcile_on_boot` now marks QUEUED rows older
> than a 6h threshold as INTERRUPTED — removing them from `ACTIVE_STATES`
> (un-blocking `exists_active_build`, the "7 stale QUEUED for FEAT-9E59"
> gap) while keeping them re-pickable via arm-3 redelivery (strictly safer
> than terminalising, which would drop a live-but-slow build). AC3: the
> named freeze reproduced in
> `test_restart_mid_dispatch_freeze_queued_redelivery_redispatches`
> (TestDispatchWiring) — green; stale-QUEUED reconcile pinned in
> `TestStaleQueuedReconcile` (3 tests). cancel-then-redeliver unchanged
> (CANCELLED is terminal → duplicate-terminal ack+skip; a fresh build needs
> a fresh queue — documented, correct as-is).
>
> **Merge-review refinement (DD4F, 4-agent adversarial pass):** the
> runless-re-dispatch arm is gated on the approval gate being WIRED. In the
> DDR-007 no-gate soft-fail path the legacy launch never advances
> `builds.status`, so a LIVE build keeps its row at QUEUED/INTERRUPTED —
> re-dispatching a redelivery there would DOUBLE-LAUNCH it. Re-dispatch only
> when `bound_gate_parts()` + adapters are present (the gated path advances a
> live build past QUEUED, so a QUEUED/INTERRUPTED duplicate is definitively
> runless); otherwise hold the slot. Pinned by
> `test_queued_redelivery_holds_slot_when_gate_unwired_no_double_launch`.

# Duplicate-guard/redelivery wedge

When a run's thread state is evicted (in-mem backend, TASK-ABW-004) the
message stays un-acked and redelivers; `dispatch_build` then refuses every
redelivery as "duplicate active build" while nothing ever terminalises the
SQLite row. Also: `exists_active_build` matches ANY active row, so stale
QUEUED rows (7 found for FEAT-9E59) block dispatch indefinitely; and
cancel-then-redeliver acks-as-terminal without re-dispatching (fresh envelope
required). Design decision needed: redelivery of an active-but-runless build
should re-dispatch, terminalise, or escalate — never spin silently.
Related: TASK-ABW-003 (identity provider), TASK-ABW-004 (backend persistence).

## Acceptance Criteria
- [ ] Documented decision + implementation for redelivery-vs-active-build.
- [ ] Stale-row hygiene: startup reconcile terminalises QUEUED rows older than
      a threshold (or equivalent).
- [ ] Integration test reproducing the 2026-07-04 wedge passes.
- [ ] All modified files pass project-configured lint/format checks with zero errors

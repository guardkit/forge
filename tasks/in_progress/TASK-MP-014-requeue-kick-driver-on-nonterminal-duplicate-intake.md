---
id: TASK-MP-014
title: "Planning intake: re-kick the driver on a redelivered non-terminal duplicate (QUEUED run stalls until restart)"
status: backlog
created: 2026-07-06T22:30:00Z
updated: 2026-07-06T22:30:00Z
priority: medium
task_type: implementation
feature_id: FEAT-SPL-002
repo: forge
implementation_mode: task-work
complexity: 3
dependencies: []
tags: [mode-p, intake, redelivery, found-2026-07-06, pre-commit-review]
---

# Task: re-kick the planning driver on non-terminal duplicate intake

## Defect (MEDIUM, from the 2026-07-06 pre-commit review)

The intake consumer acks-after-persist; if the ack fails (or the daemon dies
in the ack window) JetStream redelivers the PlanningQueuedPayload. The
duplicate path (`planning_consumer.py`, correlation_id dedup) recognises the
existing run and ack-skips — but for a run still in QUEUED whose original
driver kick was lost, **nothing re-kicks the driver**, so the planning
request stalls until the next daemon restart (when the boot sweep picks it
up). The current behavior is deliberately test-pinned, so this is a
behavior-change decision, not a bug-sneak: redelivery is exactly the signal
that the original processing may have died, and kicking the (per-cid deduped,
re-entrant) driver on a non-terminal duplicate is safe by construction.

## Acceptance criteria

- [ ] On a duplicate intake for a run in a NON-terminal state, ack AND kick
      the driver for that correlation_id through the composition's existing
      per-cid dedup (a concurrent in-flight drive must remain a no-op — the
      dedup guarantees at-most-one active driver per run).
- [ ] Terminal-duplicate behavior unchanged (ack + originator notification,
      RT-10).
- [ ] Update the test that pins ack-skip-without-kick to pin the new
      behavior; add a test proving a lost-kick QUEUED run resumes on
      redelivery without waiting for a daemon restart.
- [ ] No double-dispatch: test that a duplicate arriving while the driver is
      actively mid-run does not produce a second PO dispatch or a second
      approval request (attempt/request_id dedup already covers the wire —
      assert it holds through this path).

## Evidence / references

- Pre-commit review: `docs/reviews/task-mp-012-jnb-109-pre-commit-review-2026-07-06.md`
  (fresh-defects finding #2). Related LOW carried there: nak-on-store-failure
  has no redelivery delay (hot-loop under persistent store failure) — if
  touching the same code path, consider adding backoff in this task and tick
  it off the review's carried list.

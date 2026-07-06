---
id: TASK-MP-014
title: "Planning intake: re-kick the driver on a redelivered non-terminal duplicate (QUEUED run stalls until restart)"
status: completed
created: 2026-07-06T22:30:00Z
updated: 2026-07-06T23:45:00Z
completed: 2026-07-06T23:45:00Z
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

- [x] On a duplicate intake for a run in a NON-terminal state, ack AND kick
      the driver for that correlation_id through the composition's existing
      per-cid dedup (a concurrent in-flight drive must remain a no-op — the
      dedup guarantees at-most-one active driver per run).
- [x] Terminal-duplicate behavior unchanged (ack + originator notification,
      RT-10).
- [x] Update the test that pins ack-skip-without-kick to pin the new
      behavior; add a test proving a lost-kick QUEUED run resumes on
      redelivery without waiting for a daemon restart.
- [x] No double-dispatch: test that a duplicate arriving while the driver is
      actively mid-run does not produce a second PO dispatch or a second
      approval request (attempt/request_id dedup already covers the wire —
      assert it holds through this path).

## Implementation record (2026-07-06, /task-work, light intensity)

**Production changes:**

- `src/forge/adapters/nats/planning_consumer.py`
  - Non-terminal duplicate path now acks AND re-kicks the driver via the
    new shared `_kick_driver()` helper (same guarded post-ack pattern as
    the fresh-run kick; a driver defect never wedges intake). Terminal
    path unchanged (ack + RT-10 notification, no kick).
  - Carried LOW ticked: `_nak_or_leave_unacked` now requests
    `nak(delay=NAK_REDELIVERY_DELAY_SECONDS)` (5s) with a bare-`nak()`
    TypeError fallback for fakes/older clients — bounds the redelivery
    hot-loop under persistent store failure.
- `src/forge/cli/_serve_planning.py`
  - Extracted the per-cid drive-dedup closure into module-level
    `make_drive_spawner(driver, supervise)` (behavior identical) so
    tests exercise the REAL dedup; exported in `__all__`.

**Tests** (63 pass across the four affected suites; full unit suite
5293 passed — remaining failures are pre-existing env-dependent
integration tests needing a real broker/Docker/NAS):

- `tests/forge/adapters/test_planning_consumer.py`
  - Repinned `test_on_recorded_not_fired_for_duplicates` →
    `test_on_recorded_refired_for_non_terminal_duplicate`.
  - Added: lost-kick QUEUED run resumes on redelivery (no restart);
    terminal duplicate does NOT kick; duplicate-path kick exception
    never wedges intake; nak delay asserted + legacy-nak fallback.
- `tests/cli/test_serve_planning.py`
  - Added `TestDuplicateIntakeNoDoubleDispatch`: real driver blocked
    mid-PO-dispatch + duplicate intake through the real
    `make_drive_spawner` → exactly one PO dispatch, one drive task,
    one approval request; run completes PLANNED_HANDOFF.

**Coverage:** planning_consumer.py 90%; new spawner fully covered
(module 69% overall — misses are pre-existing composition paths).

## Evidence / references

- Pre-commit review: `docs/reviews/task-mp-012-jnb-109-pre-commit-review-2026-07-06.md`
  (fresh-defects finding #2). Related LOW carried there: nak-on-store-failure
  has no redelivery delay (hot-loop under persistent store failure) — if
  touching the same code path, consider adding backoff in this task and tick
  it off the review's carried list.

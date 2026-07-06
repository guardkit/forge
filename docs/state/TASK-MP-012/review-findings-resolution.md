# TASK-MP-012 — in-flight review findings and resolutions (2026-07-06)

Multi-agent review of the TASK-MP-012 working tree (4 dimensions: correctness,
wire-contract, integration-honesty, regression-risk; workflow `wf_decdb81f`,
~1.06M tokens). The adversarial refuter pass errored on a spend cap, but both
HIGH findings carried reviewer-run PoCs and were treated as confirmed.

## Fixed in this task

| Sev | Finding | Resolution |
|---|---|---|
| HIGH | Fleet watcher wired to the raw nats.aio client (lacks envelope-aware `subscribe` + `watch_fleet`) — discovery permanently empty, PO dispatch always degrades to `no_specialist_resolvable` (found independently by 2 dimensions, PoC-verified) | Composition now opens a DEDICATED `nats_core.NATSClient` for the watcher from `nats_url`, threaded `ServeConfig.nats_url` → `bind_production_dispatch_chain` → compose (same pattern as `db_path`). No URL → loud ERROR, discovery disabled explicitly. |
| HIGH | build-paused mirror `feature_id="plan-{cid}"` fails jarvis ForgeNotification `^FEAT-[A-Z0-9]{3,12}$` — pause message WARN-dropped, no Slack surface (runtime-verified) | Mirror publishes fixed `FEAT-PLANNING` (pattern-conformant; jarvis's approval join is purely on `build_id`). Pinned by `TestPlanningPauseMirror`. |
| MED | Stale `escalated_at` never refreshed — phase-2 defer rounds silently truncated | `handle_defer_request` resets the ACTIVE phase's anchor (`escalated_at` in phase 2, `paused_at` in phase 1). Pinned by `test_phase2_defer_resets_escalated_window`. |
| MED | Persistent pre-pause checkpoint failure → non-yielding retry loop starves the event loop | `_checkpoint` now reports whether PAUSED reached durable state; drive backs off 1s per failure and FAILs the run after 3 consecutive failures. |
| MED | At-cap defer with `escalation_approver=None` durably re-targets `expected_approver=""` — run unapprovable | Guard in `handle_defer_request`: no escalation target → keep current approver, run times out at the ceiling. Pinned by `test_defer_at_cap_without_escalation_approver_keeps_approver`. |
| LOW | Defer/at-cap rounds published before any waiter armed (arm-before-post violation) | Driver passes `publisher=None` in the escalation context (module persists the round, never publishes); the driver re-emits the persisted id after the next waiter arms. Pinned by `TestDeferRound`. |
| LOW | No per-cid mutex — sweep+rearm could double-drive one run (duplicate waiters, concurrent handoff git) | `_spawn_drive` keeps a live-drives map and skips duplicates; rearm routes through the composition's `rearm_callable` (same dedup). |
| LOW | One bad boot terminally cancels QUEUED / fails RUNNING runs when composition soft-fails | Sweep with no dispatcher now LEAVES runs in place with a loud ERROR; the next healthy boot re-drives them. Pinned by `TestSweepWithoutDispatcherIsNonDestructive`. |
| LOW | PAUSED row with NULL `paused_at` waits forever (window recomputed each loop) | Driver stamps `paused_at=now` ONCE durably (CAS on PAUSED) when it meets the corrupt row. |
| LOW | Arm-timeout swallows the wait task's real exception | Cancelled wait task is awaited and its non-cancel exception logged (root cause of the arm failure surfaces). |
| LOW | Stale `TODO: TASK-MP-005 will increment on defer` in checkpoint.py | Comment rewritten to point at the real defer attempt derivation in escalation.py. |

## Accepted as known limitations (not fixed here)

- **Approver identity cross-repo drift** (per-run `expected_approver` = Slack
  member id vs jarvis's static `decided_by='rich'`): the explicitly surfaced
  FEAT-SPL-003 design question for Rich — every non-Rich-originated approval
  is refused until decided. See the task's "Design questions" section.
- **`jarvis.notification.slack` is a dangling wire**: forge now publishes a
  contract-valid frozen-0.5.0 `NotificationPayload`, but jarvis has no
  consumer on that subject yet (SPL-003 rendering scope).
- **Response-wire gaps between wait rounds** (core-NATS, no replay): a
  single-shot jarvis click landing exactly between rounds is lost until the
  next defer/escalation re-emit — bounded by the escalation backstop;
  documented in the driver docstring.
- **serve.py now imports the planning chain at module import time**
  (informational): all transitively imported deps were already hard daemon
  dependencies; DDR-007 protects runtime composition, not import.
- **`evaluate_escalation_phase(publisher=None)` now persists the re-target**
  (deliberate post-merge-review fix; behavior change to a previously
  dry-run-ish path, no in-tree callers relied on it).

## Regression-risk dimension: all six directed checks passed
(zero-diff pipeline/ + guardkit seam, correlation/adapter additive-only,
run_store/checkpoint return-value changes have no breaking callers,
PlanningConsumerDeps construction sites all keyword, jarvis mints UUID4
correlation ids so the dot-tightening breaks no producer, serve.py planning
block fully guarded + soft-failed.)

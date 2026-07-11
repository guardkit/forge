---
id: TASK-FWD-PLAN-M12
title: "Dispatch hard cut-off never fires after an absorbed interim cancel — wedged dispatch waits forever"
status: backlog
created: 2026-07-11T23:45:00Z
priority: high
task_type: bug
found_by: Mode-P activation run dfmt3 (2026-07-11) — follow-ups session live observation
feature_ref: FEAT-SPL-002
tags: [mode-p, dispatch, timeout, asyncio, found-2026-07-11]
complexity: 2
---

# The 3600s dispatch cut-off silently never fires (observed live, dfmt3)

## Evidence

Run `dfmt3d02e6475df3` (correlation `c861c933c22492c4e60c815a83a7c5b3`), image with the
M11 fix (`97f6e46`) + 3600s planning budget (`7bc7737`):

- 21:30:22 UTC dispatch published; 21:31:22 the S5 interim warn logged correctly
  (`hard_cutoff_seconds=3600.000`) — the interim leg's absorbed-cancel fall-through
  (M11 path) worked.
- The specialist never replied (its session died on `OutputParseError` — the
  architect-agent alias experiment; see POCONTENT task).
- The hard cut-off was due 22:30:22 UTC. **Verified >45 min later: NO "hard cut-off
  fired" warn, NO `dispatch.local_timeout`, NO `soft_timeout` — zero log lines for the
  correlation after the interim warn.** Process healthy throughout (healthz 200, CPU
  0.3%, other runs — dfmt4 — dispatched, timed their interim warn, completed and
  handed off normally on the same process).

So the remainder-leg `asyncio.timeout(3540)` either never expired or its expiry was
absorbed without reaching the M11 fall-through. The dispatch wait + per-agent reply
subscription + RUNNING run row leak until the next forge restart.

## Why the regression test missed it

`TestCancelSwallowingRegistryBudget::test_no_reply_times_out_at_full_budget_not_interim`
proves the interim→remainder→cut-off path with a fake registry at 0.25s scale and
PASSES. The live path differs in one layer: the production `_RegistryWaitAdapter`
(cli/_serve_planning.py) wraps `CorrelationRegistry.wait_for_reply(binding,
timeout_seconds=1e9)` — an `asyncio.wait_for(shield(future), 1e9)` INSIDE the
coordinator's `asyncio.timeout`. Suspect the interaction: the interim leg's absorbed
cancellation leaves the task's `uncancel()` bookkeeping unbalanced (the registry
swallows the CancelledError so `Timeout.__aexit__` sees a clean exit and uncancels a
cancel that was already consumed), which can break the SECOND `asyncio.timeout`'s
delivery on the same task (cancel count mismatch → its `.cancel()` treated as
already-requested / uncancelled incorrectly).

## Acceptance criteria

- A regression test reproducing the LIVE layering: coordinator + the real
  `_RegistryWaitAdapter` shape (`wait_for(shield(fut), 1e9)`), interim expiry
  absorbed, then assert the remainder cut-off actually fires (fails today if the
  hypothesis holds).
- Root-cause the cancel-count interaction; likely fix directions: (a) the adapter
  passes the coordinator's remaining budget instead of 1e9 so the registry's own
  timeout fires (returns None normally — the M11 fall-through already handles it), or
  (b) the coordinator stops nesting two absorbed-cancel layers (single-leg wait with a
  loop-side warn timer instead of split legs).
- A wedged dispatch must terminate at the budget: `local_timeout` → `soft_timeout` →
  the run degrades rather than waiting forever.

## Notes
- Operational risk is low while planning runs are attended (a restart clears the leak;
  each dispatch is independent), but this defeats the entire timeout design silently.
- The M11 fix itself is sound and live-validated (dfmt4's interim warn + normal
  completion on the same image).

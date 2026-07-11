---
id: TASK-FWD-PLAN-FLEETWATCHER
title: "Planning fleet_watcher NoneType('operation') loop → specialist discovery empty → PO degrades"
status: backlog
created: 2026-07-11T11:00:00Z
priority: high
task_type: bug
found_by: Session A MP-010 live validation (2026-07-11)
feature_ref: FEAT-SPL-002
tags: [mode-p, planning, fleet-watcher, discovery, found-2026-07-11]
complexity: 2
---

# Planning fleet_watcher errors out → every PO dispatch degrades

## Problem (verified live, Session A 2026-07-11)

With `planning.enabled:true`, the planning stack composes the fleet watcher for specialist
discovery (`_serve_planning.py` ~465/578/585). On the live GB10 it loops every 1s:

```
[WARNING] forge.adapters.nats.fleet_watcher: fleet_watcher: transient error
'NoneType' object has no attribute 'operation'; reconnecting in 1.00s
```

so specialist discovery stays empty and **every PO dispatch degrades**:

```
discovery.resolve.unresolved tool=product_owner_specialist intent=None
dispatch.degraded capability=product_owner_specialist reason=no_specialist_resolvable
```

even though `product-owner-agent` IS registered + heartbeating in the `agent-registry` KV. The
planning run still paused + accepted the phone approval, but the product-owner document is
degraded/empty. TASK-MP-012's review already flagged the risk that "feeding the watcher the raw
client leaves specialist discovery [broken]" (`_serve_planning.py:465-466`) — this is that risk
manifesting on the live deploy.

## Acceptance criteria

- Root-cause the `'NoneType' … 'operation'` access in `forge/adapters/nats/fleet_watcher.py`
  (likely a KV/stream watch update whose entry/op is None) — reproduce, fix, add a regression test
  that drives one real watch update through the composed watcher.
- On the live (or a hermetic) deploy: with `product-owner-agent` registered, a planning run
  resolves `product_owner_specialist` (no `no_specialist_resolvable`), and the PO stage produces a
  real (non-degraded) product-owner document.

## Resolution — CRASH FIXED (2026-07-11 Session-A follow-up), PO-resolution moved to TASK-FWD-PLAN-PODISCO

The `'NoneType' … 'operation'` crash is **fixed + deployed**: root-caused to
`nats_core.client.watch_fleet` (the nats-py KV `_init_done` None sentinel), guarded with
`if entry is None: continue` (**nats-core `1dc6cef`** + regression test; forge `c2210db` composed
regression test). Verified live on the rebuilt image: **0 fleet_watcher error loops** (was ~1/s);
the watcher runs clean and reads the correct `agent-registry` KV. **BUT PO still degrades** — a
DISTINCT capability-name mismatch (forge asks `tool_exact` for `product_owner_specialist`; the PO
agent advertises `po_*` tools + a `product.*` intent) → filed as **TASK-FWD-PLAN-PODISCO**. This
task's named crash is done; the "PO resolves" AC is carried by PODISCO.

## Notes
- Pairs with TASK-FWD-PLAN-GITMOUNT — both gate Mode P production-readiness (degraded plan content
  vs failed terminal). Planning-only (does not affect build dispatch). Evidence:
  `docs/state/TASK-MP-010/deploy-verification-2026-07-11-session-a.md` addendum 3 + follow-up.

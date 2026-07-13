---
id: TASK-FWD-PLAN-PODISCO
title: "Mode P PO dispatch resolves nothing: forge asks for tool `product_owner_specialist`, PO agent advertises `po_*`"
status: reconciled
created: 2026-07-11T12:00:00Z
resolved: 2026-07-13T00:00:00Z
resolution: "option (a) — intent fallback — LIVE IN CODE; reconciled + regression-pinned in Lane B / Phase E1 B2"
priority: high
task_type: design
found_by: Session-A follow-up MP-010 terminal re-validation (2026-07-11)
feature_ref: FEAT-SPL-002
tags: [mode-p, planning, discovery, specialist-contract, found-2026-07-11, reconciled-2026-07-13]
complexity: 3
---

# Mode P product-owner dispatch never resolves (capability-name mismatch)

## ✅ RECONCILED 2026-07-13 (Lane B / Phase E1 B2 — option (a), intent fallback, LIVE)

Reconciled inside Lane B B2's acceptance (the binding "reconcile M-08/PODISCO — fix or
evidence stale-and-struck, no silent carry" amendment). **Option (a) is IN CODE and now
regression-pinned:** `src/forge/pipeline/dispatchers/specialist.py` maps
`SPECIALIST_INTENT_BY_STAGE[PRODUCT_OWNER] = "product.*"` and
`dispatch_specialist_stage` threads that string as `intent_pattern` into
`forge.discovery.resolve.resolve` (step 2 — the exact-tool → intent-fallback algorithm).
So although no agent advertises a *tool* named `product_owner_specialist`, forge resolves
the live `product-owner-agent` via its advertised `IntentCapability(pattern="product.*",
confidence=0.95)` — no `no_specialist_resolvable` degrade.

**Evidence (this is not an assertion — a test proves it):**
`tests/forge/discovery/test_podisco_reconciliation.py` reconstructs the exact live manifest
shape recorded below (tools `po_idea`/`po_greenfield`/… + the `product.*` intent) and asserts
`resolve(...)` returns `match_source="intent_pattern"` and matches the agent — while a
companion test pins that WITHOUT the intent (`intent_pattern=None`) the old
`unresolved` path still fires, so a future refactor that drops the intent thread fails loudly.
Live corroboration already existed: the deployed PO reached on-topic `PLANNED_HANDOFF` twice
after this landed (Factory-2 `2dfb4ef5`; Lane A `RESOLVED-DEPLOYED` note). B2 extended the same
intent-fallback to the 007/008 target-terminal legs
(`SPECIALIST_INTENT_BY_STAGE[FEATURE_SPEC]="product.*"`, `[FEATURE_PLAN]="architecture.*"`).

_Original diagnosis retained below for the record._

---


## Problem (verified live, 2026-07-11 — surfaced by fixing TASK-FWD-PLAN-FLEETWATCHER)

Fixing the fleet_watcher NoneType crash (TASK-FWD-PLAN-FLEETWATCHER) made the watcher healthy
and discovery populate — but the planning product-owner stage STILL degrades:

```
discovery.resolve.unresolved tool=product_owner_specialist intent=None
dispatch.degraded capability=product_owner_specialist reason=no_specialist_resolvable
```

Root cause (a distinct issue the crash was masking): forge's discovery does **`tool_exact`**
matching (`src/forge/discovery/resolve.py:120` — `any(t.name == tool_name for t in
manifest.tools)`), and the planning PO dispatch asks for tool **`product_owner_specialist`** with
**`intent=None`**. But the live `product-owner-agent`'s registered manifest advertises tools
named **`po_idea` / `po_extract` / `po_greenfield` / `po_evolve` / `po_impact` / `po_scope` /
`po_status` / `po_cancel`** and an intent with pattern **`product.*`** (confidence 0.95) — there
is **no tool named `product_owner_specialist`**, so `tool_exact` finds nothing → degrade.

Net: the fleet_watcher fix is necessary (without it discovery is empty) but not sufficient — the
PO stage produces a DEGRADED (empty) product-owner document because the dispatch's expected
capability name never matches an advertised tool.

## Acceptance criteria (a forge↔specialist-agent contract decision — pick one, record it)

- **(a)** forge's planning PO dispatch resolves via **intent** (`product.*`) instead of / in
  addition to a hardcoded tool name, so it matches the PO agent's advertised intent; OR
- **(b)** forge dispatches to the actual advertised tool (e.g. `po_greenfield` / `po_idea` per
  the planning sub-stage); OR
- **(c)** the `product-owner-agent` (specialist-agent repo) additionally advertises a
  `product_owner_specialist` tool as the stable capability handle forge targets.
- Whichever: a live planning run resolves the PO specialist (no `no_specialist_resolvable`) and
  the `product_docs` checkpoint pauses on a REAL (non-degraded) product-owner document.

## Notes
- Independent of the terminal (TASK-FWD-PLAN-GITMOUNT is fixed + validated — a degraded run
  still reaches PLANNED_HANDOFF and writes the branch). This gap only affects plan CONTENT
  quality. It is the LAST thing between "Mode P infra works" and "Mode P produces real plans".
- Likely a cross-repo contract touchpoint (forge dispatch mapping ↔ specialist manifest); a
  short design pass, not an autonomous edit. Evidence:
  `docs/state/TASK-MP-010/deploy-verification-2026-07-11-session-a.md` (follow-up addendum).

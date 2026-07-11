---
id: TASK-FWD-PLAN-PODISCO
title: "Mode P PO dispatch resolves nothing: forge asks for tool `product_owner_specialist`, PO agent advertises `po_*`"
status: backlog
created: 2026-07-11T12:00:00Z
priority: high
task_type: design
found_by: Session-A follow-up MP-010 terminal re-validation (2026-07-11)
feature_ref: FEAT-SPL-002
tags: [mode-p, planning, discovery, specialist-contract, found-2026-07-11]
complexity: 3
---

# Mode P product-owner dispatch never resolves (capability-name mismatch)

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

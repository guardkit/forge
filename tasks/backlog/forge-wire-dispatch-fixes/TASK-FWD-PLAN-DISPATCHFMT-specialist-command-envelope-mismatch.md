---
id: TASK-FWD-PLAN-DISPATCHFMT
title: "Mode P PO dispatch: specialist rejects forge's agents.command.* body (not a MessageEnvelope)"
status: backlog
created: 2026-07-11T13:00:00Z
priority: high
task_type: bug
found_by: Session-A follow-up MP-010 re-validation (2026-07-11) — surfaced once PO resolution was fixed
feature_ref: FEAT-SPL-002
tags: [mode-p, planning, dispatch, specialist-contract, cross-repo, found-2026-07-11]
complexity: 3
---

# forge↔specialist dispatch command-envelope mismatch

## Problem (verified live, 2026-07-11 — the layer BEHIND PODISCO)

With PO resolution fixed (TASK-FWD-PLAN-PODISCO + the fleet_watcher fixes → `discovery.resolve.matched
agent=product-owner-agent source=intent_pattern`), forge now actually PUBLISHES the dispatch command
to `agents.command.product-owner-agent` (`forge/src/forge/adapters/nats/specialist_dispatch.py`,
`COMMAND_SUBJECT_TEMPLATE="agents.command.{agent_id}"`). The **product-owner-agent rejects it**:

```
Failed to parse NATS message as MessageEnvelope: 3 validation errors for MessageEnvelope
  event_type  Field required [missing]  (input has resolution_id/sensitive/… but no event_type)
  payload     Field required [missing]
```

So the specialist parses inbound `agents.command.*` as a **`MessageEnvelope`** (requires `event_type`
+ `payload`), but forge publishes a **dispatch-shaped body** (`resolution_id`, capability, parameters,
`sensitive`, …) that is NOT wrapped in a MessageEnvelope. The specialist errors → never replies → the
planning run hangs in `RUNNING` (no PO document produced) until the dispatch deadline.

This is a **forge↔specialist-agent EXECUTION-contract mismatch**, distinct from the *resolution*
problem (PODISCO). It lives in BOTH repos: forge's `specialist_dispatch` publisher and the
specialist-agent command parser. It was masked until now because PO dispatch always degraded BEFORE
publishing (discovery never resolved).

## Acceptance criteria (a forge↔specialist contract decision — coordinate with the specialist-agent lane)

- Align the `agents.command.{agent_id}` wire format: either forge wraps the dispatch payload in a
  `MessageEnvelope` (`event_type` + `payload`) the specialist expects, OR the specialist parses forge's
  dispatch-command shape — decide + pin the contract (nats-core is the shared schema home).
- Round-trip test (hermetic, both sides) on the exact `agents.command.*` bytes.
- Live: a planning PO dispatch produces a REAL product-owner document (the run pauses on real content,
  not a hang or a degrade).

## Notes
- This is likely **not the last** forge↔specialist execution-path layer — the whole path (dispatch →
  specialist run → `agents.result.*` reply → forge parse) appears to have never been end-to-end tested
  (each fix this session revealed the next masked layer). Recommend a dedicated forge↔specialist
  integration pass coordinated with the specialist-agent lane, not piecemeal fixes.
- The DISCOVERY/RESOLUTION + TERMINAL + NOTIFICATION layers are all fixed + deployed (see
  `docs/state/TASK-MP-010/deploy-verification-2026-07-11-session-a.md`); this is the execution layer.

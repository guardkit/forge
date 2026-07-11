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

## Resolution — FIXED + VALIDATED end-to-end (2026-07-11 Mode-P execution-contract lane)

The dedicated forge↔specialist integration pass this task recommended ran as the
DISPATCHFMT+ lane (ai-transition `ways-of-working/mode-p-forge-specialist-execution-
integration-handoff.md`; discovery `wf_49aadeff-4fb`, fix `wf_11fc0aaa-7e1`). The
prediction held: this was the FIRST of TEN mismatches (M1–M10), every one forge-side —
the deployed specialist imports nats-core verbatim and its 13-day image's nats-core
0.4.0 is byte-identical to repo 0.7.0 on all load-bearing surfaces.

Fixed (forge `6dbf7de..1bcc281`, nats-core docs `21e2bd3`):
- **M1/M5** dispatch published as nats-core `MessageEnvelope(event_type=COMMAND,
  payload=CommandPayload)`, correlation in the BODY (headers tracing-only).
- **M2/M3** command names a DEPLOYED verb (`greenfield`) with dict args carrying the
  verb's required inputs (`problem_statement`; architect `docs_path`+`scope`).
- **M4/M5** forge consumes the 3-token `agents.result.{agent_id}` with body-correlation
  demux (wrong-correlation drop preserved).
- **M6/M7/M10/M10b** reply parser consumes the deployed `ResultPayload`, branches on
  `success`, extracts `role_output` — including the UNWRAPPED session-reply shape
  (live-captured: `run_product_session` paths publish the document DIRECTLY as
  `ResultPayload.result`) — and `planning/driver.py` threads it (not
  `criterion_breakdown`) into product_docs/PLANNED_HANDOFF.
- **M8/M11** pointed no-reply diagnostics; the interim-leg cancel-swallow that
  silently collapsed 900s→60s fixed (`97f6e46`); planning composition budget 3600s
  (`7bc7737` — a real PO greenfield session measured 8–76 min live).
- **M9** llama-swap now serves the `product-owner-agent` alias (INTERIM on the
  resident qwen36-workhorse; config backup `.bak-20260711-pre-po-alias`).

**Live validation (cid `dfmt229a103d6df1`):** inject → resolve → envelope dispatch →
specialist parse+route → 8m17s greenfield session → reply parsed → **product_docs
checkpoint PAUSED on the real document** → identity-pinned approve (subject is
RUN-scoped `agents.approval.forge.plan-{cid}.response`; request_id in payload) →
**PLANNED_HANDOFF** with branch + `feature_spec_inputs/{cid}.md` carrying the full
structured PO document. Contract of record: nats-core
`docs/design/contracts/agent-execution-contract.md`.

Follow-ups filed: TASK-FWD-PLAN-POCONTENT (PO document ignores problem_statement —
specialist/model side). Jarvis planning-intake env re-wire remains the operator's
post-lane flip (handoff §7) for the phone render of approval requests.

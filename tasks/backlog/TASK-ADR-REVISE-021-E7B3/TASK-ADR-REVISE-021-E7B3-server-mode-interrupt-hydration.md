---
id: TASK-ADR-REVISE-021-E7B3
title: Revise ADR-ARCH-021 — server-mode `interrupt()` returns dict, not typed payload
status: backlog
created: 2026-04-20T00:00:00Z
updated: 2026-04-20T00:00:00Z
priority: high
tags: [adr-revision, architecture, langgraph, interrupt, pre-system-design]
complexity: 3
task_type: architecture
decision_required: true
parent_review: TASK-SPIKE-D2F7
scoping_source: docs/research/ideas/deepagents-053-verification.md §Server-mode closeout (TASK-SPIKE-D2F7)
blocks:
  - /system-design
estimated_effort: 1–2 hours
test_results:
  status: not_applicable
  coverage: null
  last_run: null
---

# Task: Revise ADR-ARCH-021 — server-mode `interrupt()` returns dict, not typed payload

## Description

TASK-SPIKE-D2F7 closed out the server-mode coverage gap on ASSUM-009 and
returned **FAIL**: under `langgraph dev` (Forge's canonical deployment
mode per ADR-ARCH-021), the value a node observes on the return of
`interrupt()` is a plain `dict`, not a Pydantic instance. The control-flow
primitive works — pause, resume, and onward execution all succeed — but
the ADR's decision snippet, which passes the resumed value directly into
`handle_approval_response(response, build_id)` assuming `response` is a
typed `ApprovalResponsePayload`, is wrong in server mode.

See verdict table and divergence analysis in
`docs/research/ideas/deepagents-053-verification.md` §"Server-mode
closeout (TASK-SPIKE-D2F7, 2026-04-20)".

## Origin

Spawned from TASK-SPIKE-D2F7 close-out on 2026-04-20 per that task's
AC-6 fail-path: "On FAIL: spawn `TASK-ADR-REVISE-021-*` per the C1E9
revision policy; keep `/system-design` blocked on the revision." This
task IS that revision.

## Scope

### In scope

1. Revise ADR-ARCH-021 to reflect server-mode reality. The ADR must
   explicitly state that under `langgraph dev` / LangGraph server,
   `interrupt()` returns a `dict`, not a typed Pydantic instance, and
   prescribe one of:
   - **Option A (cheap):** mandate explicit re-hydration at the call
     site — e.g. `response = ApprovalResponsePayload.model_validate(
     interrupt({...}))`. Low risk, no new infra; every call site must
     remember to do this.
   - **Option B (robust):** register interrupt/resume Pydantic types
     via `allowed_msgpack_modules` (and/or equivalent serde hook) so
     that both direct-invoke and server modes rehydrate automatically.
     Needs a verification pass that server-mode rows then match
     direct-invoke rows in the D2F7 table.
   - **Option C (hybrid):** Option A in the near term, Option B as a
     follow-up once the Forge payload types are finalised in
     `/system-design`.
2. Update the ADR's decision code block so it no longer implies typed
   return from `interrupt()` without an explicit validation step.
3. Record the decision as a new Revision (Category-4-style) on the ADR
   header, citing TASK-SPIKE-D2F7.
4. Update the ADR's References line to point at the D2F7 findings
   section (not only the C1E9 section).
5. On merge: unblock `/system-design`.

### Out of scope

- Re-running the server-mode spike. D2F7 already captured the evidence.
- Resolving the `allowed_msgpack_modules` warning at the library level
  — Option B above may use it; library-level fixes are upstream concerns.
- Productionising `langgraph.json` for the real Forge graph — that is
  `/system-design`'s job.

## Acceptance Criteria

- [ ] `docs/architecture/decisions/ADR-ARCH-021-paused-via-langgraph-interrupt.md`
      updated with:
  - Revision header noting the server-mode finding and the chosen option.
  - Decision code block showing the chosen rehydration approach
    explicitly (no implicit typed-return assumption).
  - Consequences section updated to reflect the new call-site / serde
    contract.
  - References line pointing at the D2F7 findings section.
- [ ] If Option B or C is chosen: a short re-verification note is
      appended to `deepagents-053-verification.md` showing the
      server-mode rows now match the direct-invoke rows, or a
      follow-up task is scoped explicitly.
- [ ] `/system-design` is unblocked (i.e. this task and any spawned
      follow-up are either done or explicitly declared non-blocking by
      the reviewer).
- [ ] Commit message references `TASK-ADR-REVISE-021-E7B3` and
      `TASK-SPIKE-D2F7`.

## Known Risks / Watch-outs

- **Ripple into NATS adapter.** If Option A is chosen, every
  `interrupt()` call site in Forge is responsible for validation. A
  helper (e.g. `resume_value_as(model_cls, raw)`) may be worth adding
  in the same revision to avoid copy-paste drift.
- **Option B verification cost.** Registering types in
  `allowed_msgpack_modules` requires a fresh spike pass to confirm
  server-mode row-for-row parity — budget for that before committing to B.
- **Don't silently change ADR-ARCH-023 / ADR-ARCH-022.** Scope tightly
  to ADR-021.

## Source Material

- `docs/research/ideas/deepagents-053-verification.md` §Server-mode
  closeout (TASK-SPIKE-D2F7, 2026-04-20) — the verdict rows and
  divergence analysis this revision must reflect.
- `docs/architecture/decisions/ADR-ARCH-021-paused-via-langgraph-interrupt.md`
  — the ADR being revised.
- `spikes/deepagents-053/interrupt_server_drive.py` — reproducer for
  the FAIL verdict, if further evidence is needed during the revision.

## Test Requirements

- None directly (ADR revision). If Option B or C is taken, the
  re-verification spike will have its own test/evidence bar.

## Implementation Notes

_(Populated at execution time.)_

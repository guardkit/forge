---
id: TASK-ADR-REVISE-021-E7B3
title: Revise ADR-ARCH-021 — server-mode `interrupt()` returns dict, not typed payload
status: in_review
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

- [x] `docs/architecture/decisions/ADR-ARCH-021-paused-via-langgraph-interrupt.md`
      updated with:
  - Revision header noting the server-mode finding and the chosen option.
  - Decision code block showing the chosen rehydration approach
    explicitly (no implicit typed-return assumption).
  - Consequences section updated to reflect the new call-site / serde
    contract.
  - References line pointing at the D2F7 findings section.
- [x] If Option B or C is chosen: a short re-verification note is
      appended to `deepagents-053-verification.md` showing the
      server-mode rows now match the direct-invoke rows, or a
      follow-up task is scoped explicitly.
- [x] `/system-design` is unblocked (i.e. this task and any spawned
      follow-up are either done or explicitly declared non-blocking by
      the reviewer).
- [ ] Commit message references `TASK-ADR-REVISE-021-E7B3` and
      `TASK-SPIKE-D2F7`. *(Deferred to commit step.)*

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

### Review outcome (2026-04-20, `/task-review` accepted)

**Chosen option: C (Hybrid) — Option A in this ADR revision, Option B as a
separate deferred spike.**

Rationale:

- Unblocks `/system-design` today without new infrastructure.
- Option B (`allowed_msgpack_modules`) is an **unverified hypothesis** for
  the server-mode failure. The D2F7 warning is emitted by LangGraph's
  *checkpoint* msgpack layer; the server-mode FAIL is driven by the
  *HTTP/JSON* transport layer that sits above the checkpoint.
  `allowed_msgpack_modules` may or may not carry typing across the HTTP
  wire — committing it to the ADR without a spike would re-introduce an
  unverified assumption.
- Explicit rehydration at the call site is arguably better architecture
  regardless of whether B works: it makes type expectations visible at
  the point of use and is robust to future serialization changes.
- A helper (`resume_value_as(model_cls, raw)`) addresses the drift
  concern called out in "Known Risks" and is forward-compatible — the
  `isinstance` short-circuit makes it a no-op if B later succeeds, so
  no call-site churn either way.

### Edits the ADR revision must carry

1. **Revision 10 header** dated 2026-04-20, citing TASK-SPIKE-D2F7 and
   this task; note that server-mode `interrupt()` returns `dict`, and
   that the ADR now mandates explicit rehydration at the call site.
2. **Decision code block** — replace the current
   `response = interrupt({...})` pass-through with:

   ```python
   def resume_value_as(model_cls, raw):
       return raw if isinstance(raw, model_cls) else model_cls.model_validate(raw)

   raw = interrupt({...})
   response = resume_value_as(ApprovalResponsePayload, raw)
   return handle_approval_response(response, build_id)
   ```

   The ADR should name the helper's expected home (suggest
   `forge.adapters.langgraph`, co-located with the `interrupt()` call
   sites and the NATS resume consumer) so `/system-design` does not
   re-litigate placement.
3. **Consequences** — strike "Resume with typed payload works natively —
   no custom resume RPC" (server-mode-false). Add: call sites are
   responsible for rehydration via `resume_value_as`; rationale is
   that HTTP/JSON transport in `langgraph dev` strips Pydantic typing,
   and serde-level fixes (Option B) are a separate, deferred
   investigation.
4. **References** — confirm the footer points at the D2F7 closeout
   section, not only the C1E9 section. Current line already does most
   of this; align wording with Revision 10.

### Deferred follow-up (to be scoped as a separate task, non-blocking)

`TASK-SPIKE-*` — verify whether registering Pydantic interrupt/resume
types via LangGraph's `allowed_msgpack_modules` (or equivalent serde
hook) causes server-mode resume values to arrive as typed instances,
matching the direct-invoke row table in D2F7. Non-blocking on
`/system-design`; pure optimisation. If it passes, `resume_value_as`
becomes defensive-only. If it fails, we stop there — Option A is
sufficient.

### Out-of-scope reminders (restated)

- Do not touch ADR-022 or ADR-023.
- Do not land the `resume_value_as` helper code in this task — the ADR
  names its home module; `/system-design` implements it.
- Do not re-run the server-mode spike — D2F7 is authoritative.

### Execution log (2026-04-20)

Revision landed as **Revision 10** of ADR-ARCH-021. Edits made:

1. **Header** — status line updated to "Accepted (Revision 10, 2026-04-20)";
   session line extended to cite Revision 10, TASK-ADR-REVISE-021-E7B3, and
   TASK-SPIKE-D2F7.
2. **Decision** — prose amended to state that server-mode `interrupt()`
   returns `dict` and that call sites MUST rehydrate explicitly. Helper
   contract (`resume_value_as(model_cls, raw)` with `isinstance`
   short-circuit) shown inline; named home `forge.adapters.langgraph` with
   an explicit note that `/system-design` implements it (this task does
   not land helper code). Call-site example rewritten to show
   `raw = interrupt({...}); response = resume_value_as(
   ApprovalResponsePayload, raw)`.
3. **Consequences** — added a positive bullet on explicit rehydration +
   the forward-compatibility of the `isinstance` short-circuit. Added a
   negative bullet on the contractual obligation plus the silent
   attribute-default failure mode observed in D2F7. Adjusted the
   "semantics must match" bullet to cite both C1E9 and D2F7 and to note
   that type fidelity holds only in direct-invoke mode. Explicitly struck
   the old "Resume with typed payload works natively" bullet (kept in a
   "Struck by Revision 10" subsection so the prior claim remains legible).
4. **Revision 10 section** — trigger (D2F7 FAIL), chosen option (C hybrid),
   reasoning for not-A-alone and not-B-alone, forward-compatibility of the
   helper, deferred Option B follow-up (non-blocking, scoped later), and
   confirmation that ADR-022/ADR-023 are untouched.
5. **References** — extended to cite both the C1E9 and D2F7 findings
   sections, plus TASK-SPIKE-D2F7 and this task as inline references.

Also appended a short "Resolution" subsection to
`docs/research/ideas/deepagents-053-verification.md` under the server-mode
closeout, recording the Option C decision, the `resume_value_as` helper
contract, the deferred Option B spike, and the `/system-design` unblock. The
server-mode rows are **not** re-run in this revision — the verification
doc notes that parity with direct-invoke rows becomes the success criterion
of the deferred Option B spike.

No code landed; no spike re-run; ADR-022 and ADR-023 not modified.

**Status:** Ready for `/task-review`. `/system-design` is unblocked by this
revision alone.

# DF-007 (DRAFT) — Gates travel with the agent, not the caller

> **STATUS: DRAFT — pending operator sign-off before filing to
> `../ai-transition/docs/decisions/`.** DF-007 is RESERVED in the fleet
> `REGISTER.md` with no body text; TASK-GATE-D659 is plausibly its filing
> trigger. There is a trigger-wording conflict between `REGISTER.md` and the
> plan-of-record (noted below) that the operator must resolve before this
> decision is filed. Do **not** copy this file into the sibling `ai-transition`
> repo until sign-off.

- **Framing precedent:** DF-009 (gate-property framing).
- **Source task:** TASK-GATE-D659 — daemon-side pre-dispatch approval gate,
  phone round-trip, restart-safe (blocks TASK-JNB-107).
- **Author:** Fable gate-activation session (Wave 3 closure).
- **Date:** 2026-07-05.

## Decision

**The approval gate is a property of the forge build lifecycle**, not of any
caller, adapter, or transport that dispatches a build.

1. **Enforced at forge's own dispatch boundary.** The gate runs inside the
   daemon's `dispatch_build` flow — after `record_pending_build` mints the
   `build_id`, before any runner is launched or observer registered
   (`maybe_gate_build`, the first production caller of `gate_check`). No caller
   opts in or out; every dispatched build passes through the gate because the
   gate is the dispatch boundary, not a decoration on it.

2. **Re-armed from forge's own ledger.** A gate outstanding at process death is
   re-armed on the next boot **from SQLite** — the authoritative
   `builds.status = PAUSED` + `pending_approval_request_id` row — by
   `rearm_paused_gates`, which re-emits the approval request (verbatim
   `request_id`, same `correlation_id`) and `build-paused` only after a live
   response subscriber is confirmed (arm-before-post). The gate's durability is
   forge's own state, not the caller's memory or the transport's redelivery.

3. **Callers are identity-pinned responders, never gate owners.** A human (or a
   fleet peer speaking on a human's behalf) may only *answer* an outstanding
   gate, and only when their `decided_by` matches the deployment's pinned
   `expected_approver` (default `"rich"`, OPS-001) **and** their echoed
   `correlation_id` matches the paused build's. A responder cannot create,
   move, skip, or own a gate; they can approve / reject / defer / override an
   existing one. `forge cancel` of a paused build is itself routed through this
   same responder channel (a synthetic reject into the live gate frame), so
   even the CLI is a responder, not a gate owner.

4. **v1 never auto-approves.** Until evidence-based gating lands (post-UBS-002),
   the reasoning seam is a static `MANDATORY_HUMAN_APPROVAL` (degraded /
   training mode): every dispatched build pauses for human approval. This is
   the DF-009 ratchet made concrete — the gate's *default* is "stop and ask",
   and only accumulated verification quality can relax it.

5. **Autonomy follows verification quality.** The activation point is
   deliberately provisional. A pre-dispatch gate can only gate
   *permission-to-start* — `coach_score` is structurally unavailable at
   dispatch. When evidence-based gating arrives, the activation point moves to a
   runner-side / outcome boundary; the gate remains a lifecycle property
   (~60–70% of the machinery — adapters, `request_id` derivation, correlation
   threading, recovery binding, rearm — is activation-point-agnostic and
   carries over). Autonomy is *earned* by verification quality, not *granted* by
   a caller.

## Consequences

- Gate ownership can never leak to a caller: there is no API by which a
  dispatcher declares "this build needs no gate" or "this build's gate is mine".
- Restart safety is a property of the ledger, not the wire: a crash mid-pause is
  a non-event (the row survives; rearm re-arms it).
- Cross-repo: jarvis (and any future fleet responder) is an identity-pinned
  responder against the AGENTS approval protocol; it never holds gate state.
  `JARVIS_SLACK_DECIDED_BY` MUST equal forge's `expected_approver` verbatim.

## Open item for the operator (blocks filing)

- **Trigger-wording conflict.** `REGISTER.md`'s reserved DF-007 line and the
  plan-of-record describe the filing trigger differently (agent-scoped vs
  dispatch-boundary-scoped). Reconcile the wording, confirm TASK-GATE-D659 is
  the intended trigger, then file this draft to
  `../ai-transition/docs/decisions/` with the fleet DF number.

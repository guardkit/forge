# ADR-ARCH-021: PAUSED state realised as LangGraph `interrupt()`

- **Status:** Accepted (Revision 10, 2026-04-20)
- **Date:** 2026-04-18
- **Session:** `/system-arch` Category 4 Revision 9 (original); Revision 10
  recorded 2026-04-20 per TASK-ADR-REVISE-021-E7B3, citing TASK-SPIKE-D2F7.

## Context

Anchor v2.2 §6 introduces PAUSED as a first-class state in the Forge lifecycle: flagged builds wait for Rich's approval via `ApprovalRequestPayload` / `ApprovalResponsePayload` over NATS. Initial design wired this as custom code — SQLite `PAUSED` row + NATS subscriber waiting for the response. DeepAgents 0.5.3 provides `interrupt()` as a first-class LangGraph primitive for exactly this pattern.

## Decision

Implement PAUSED as a LangGraph `interrupt()` call. **Under `langgraph dev` /
LangGraph server (Forge's canonical deployment mode), the value returned by
`interrupt()` is a plain `dict`, not a typed Pydantic instance** (see
Revision 10 and TASK-SPIKE-D2F7). Every call site therefore **MUST** rehydrate
the resume value explicitly via a Pydantic validator before attribute access
or `isinstance` checks. Forge centralises the rehydration pattern in a small
helper, `resume_value_as`, whose expected home is `forge.adapters.langgraph`
— co-located with the `interrupt()` call sites and the NATS resume consumer.
`/system-design` implements the helper; this ADR only names its contract and
placement so the module location is not re-litigated.

```python
# forge.adapters.langgraph (helper contract — implemented in /system-design)
def resume_value_as(model_cls, raw):
    """Rehydrate a LangGraph interrupt resume value into a typed Pydantic instance.

    Under `langgraph dev` / server mode, `interrupt()` returns `dict`, not the
    Pydantic type the producer published. Call sites MUST route the resume
    value through this helper (or an equivalent `model_validate` call) before
    attribute access or `isinstance` checks. The `isinstance` short-circuit
    makes the helper a no-op when rehydration is already typed (direct-invoke
    mode, or server mode after a future Option B serde fix — see Revision 10),
    so no call-site churn is required if that follow-up later lands.
    See TASK-SPIKE-D2F7 for the server-mode divergence table.
    """
    return raw if isinstance(raw, model_cls) else model_cls.model_validate(raw)


# Inside a pure gate-evaluation path used by tools/sub-agents
async def evaluate_gate(result, priors, rationale, build_id, stage_label):
    decision = reason_about_gate(result, priors, ...)
    if decision.mode == GateMode.FLAG_FOR_REVIEW:
        payload = build_approval_payload(decision, build_id, stage_label, ...)
        await nats.publish(
            f"agents.approval.forge.{build_id}",
            payload.model_dump_json(),
        )
        # Publish PAUSED state to SQLite + JetStream so crash recovery sees it
        await sqlite.mark_paused(build_id, payload)

        # Interrupt the graph — control returns to the LangGraph runtime.
        # Under `langgraph dev`, the resumed value arrives as a `dict`;
        # rehydrate it explicitly rather than relying on typed round-trip.
        raw = interrupt({
            "kind": "approval_required",
            "build_id": build_id,
            "stage_label": stage_label,
            "payload": payload.model_dump(),
        })
        response = resume_value_as(ApprovalResponsePayload, raw)
        # Graph halts. Runtime surfaces the interrupt value externally.
        # Rich replies via NATS → ApprovalResponsePayload consumer resumes the graph.
        return handle_approval_response(response, build_id)
    # ... etc
```

The `forge.adapters.nats` approval subscriber consumes `agents.approval.forge.{build_id}.response`, calls the graph's resume API with the payload, and the `interrupt()` call above returns with the response (as `dict`; rehydrated at the call site via `resume_value_as`).

PAUSED survives process restart via the SQLite + JetStream path (ADR-SP-013 crash recovery) — the LangGraph interrupt itself doesn't survive process crash, but the external approval protocol does. On restart, Forge detects PAUSED in SQLite, re-emits `ApprovalRequestPayload`, and re-enters `interrupt()` when the graph re-runs.

## Consequences

- **+** Custom pause wiring replaced by a LangGraph primitive — less code, better tested.
- **+** Clean separation: NATS adapter handles the wire; LangGraph handles the graph-halt; no custom condition-variable coordination.
- **+** `interrupt()` values are surfaced by the LangGraph server directly — a dashboard or CLI can query pending interrupts without knowing NATS.
- **+** Explicit rehydration via `resume_value_as` makes the serde boundary visible at each call site — no hidden assumptions about type fidelity across the HTTP/JSON layer — and the `isinstance` short-circuit means the call-site shape does not change if a future serde fix restores typed resumption.
- **−** Every `interrupt()` call site carries a contractual obligation to rehydrate before attribute access or `isinstance` checks. Forgetting to do so produces silent attribute-default bugs (observed in TASK-SPIKE-D2F7: `getattr(dict, "approved", False)` returns `False` with no error). Mitigation: the `resume_value_as` helper makes this a one-liner, and the deferred Option B spike (below) may eliminate the obligation entirely.
- **−** Process crash during paused state loses the in-graph interrupt but preserves the external protocol (ApprovalRequest was already published, SQLite marks PAUSED). Restart re-emits → potential double-emit of ApprovalRequestPayload if Rich hasn't responded yet. Handled: responders are idempotent by `correlation_id`; first response wins.
- **−** DeepAgents `interrupt()` semantics must match expectations (fresh `response` passed back on resume). Verified for both direct-invoke (TASK-SPIKE-C1E9) and server-mode (TASK-SPIKE-D2F7); control-flow is fully functional in both modes. Type fidelity holds only in direct-invoke mode — see Revision 10.

**Struck by Revision 10 (no longer true under server mode):**

- ~~**+** Resume with typed payload works natively — no custom resume RPC.~~
  Server-mode resume values arrive as `dict`; rehydration is a call-site
  obligation until a future serde fix is verified.

## Revision 10 — server-mode rehydration contract (2026-04-20)

**Trigger.** TASK-SPIKE-D2F7 closed the server-mode coverage gap on
ASSUM-009 and returned **FAIL on type fidelity, PASS on control-flow**.
Under `langgraph dev` the SDK caller receives the interrupt payload as a
`dict`, and the node receives the resume value as a `dict` — not the
Pydantic instances direct-invoke mode preserves (row-for-row divergence
table in the verification doc). The original Decision snippet passed
`response` directly into `handle_approval_response(...)` assuming typed
round-trip, so it was **incorrect as written** for server mode.

**Chosen option: C (hybrid).** Near-term: explicit rehydration at every
call site via `ApprovalResponsePayload.model_validate(...)`, centralised in
a `resume_value_as(model_cls, raw)` helper to prevent copy-paste drift.
Follow-up (deferred, non-blocking on `/system-design`): a separate spike to
test whether registering Pydantic interrupt/resume types via LangGraph's
`allowed_msgpack_modules` (or an equivalent serde hook) causes server-mode
resume values to arrive typed, matching the direct-invoke rows in D2F7.

**Why not Option A alone.** Every call site would be responsible for
validation without a shared pattern — high drift risk over time. The
`resume_value_as` helper mitigates that.

**Why not Option B alone.** The server-mode FAIL is driven by LangGraph's
HTTP/JSON transport, while the existing `allowed_msgpack_modules` warning
observed in C1E9 is emitted by the checkpoint msgpack layer that sits
*below* the HTTP transport. Whether `allowed_msgpack_modules` carries type
information across the HTTP wire is unverified; committing Option B to the
ADR without a spike would re-introduce an unverified assumption.
Additionally, the final Forge payload types are not defined until
`/system-design`, so Option B cannot land end-to-end before then.

**Forward compatibility.** The helper's `isinstance` short-circuit
(`return raw if isinstance(raw, model_cls) else model_cls.model_validate(raw)`)
is a no-op when the resume value is already typed. If the deferred Option B
spike later succeeds, `resume_value_as` degrades to defensive-only and no
call-site churn is required.

**Deferred follow-up.** A separate `TASK-SPIKE-*` will be scoped (not
within this revision) to verify Option B. Outcomes:
- **Pass:** register the types in `allowed_msgpack_modules`, re-verify
  row-for-row parity with D2F7, and either drop `resume_value_as` or leave
  it as defensive belt-and-braces.
- **Fail:** stop there — Option A is sufficient and the ADR stands as
  revised.

The follow-up is **non-blocking** on `/system-design`. `/system-design` is
unblocked by this revision alone.

**Scope not changed by this revision.** The crash-recovery contract
(SQLite + JetStream + idempotent responders), the NATS adapter split, and
the ADR's overall choice to use `interrupt()` rather than a custom pause
channel all stand unchanged. ADR-ARCH-022 and ADR-ARCH-023 are not touched
by this revision.

## References

- [deepagents 0.5.3 primitives verification](../../research/ideas/deepagents-053-verification.md) — ASSUM-009 typed `interrupt()` round-trip confirmed for direct `CompiledStateGraph.invoke` (TASK-SPIKE-C1E9), and server-mode divergence captured in §"Server-mode closeout (TASK-SPIKE-D2F7, 2026-04-20)". The latter is the authoritative evidence for Revision 10 above.
- TASK-SPIKE-D2F7 — server-mode verification spike (FAIL on type fidelity, PASS on control-flow).
- TASK-ADR-REVISE-021-E7B3 — this revision.

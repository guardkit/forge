# ADR-ARCH-021: PAUSED state realised as LangGraph `interrupt()`

- **Status:** Accepted
- **Date:** 2026-04-18
- **Session:** `/system-arch` Category 4 Revision 9

## Context

Anchor v2.2 §6 introduces PAUSED as a first-class state in the Forge lifecycle: flagged builds wait for Rich's approval via `ApprovalRequestPayload` / `ApprovalResponsePayload` over NATS. Initial design wired this as custom code — SQLite `PAUSED` row + NATS subscriber waiting for the response. DeepAgents 0.5.3 provides `interrupt()` as a first-class LangGraph primitive for exactly this pattern.

## Decision

Implement PAUSED as a LangGraph `interrupt()` call:

```python
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

        # Interrupt the graph — control returns to the LangGraph runtime
        response = interrupt({
            "kind": "approval_required",
            "build_id": build_id,
            "stage_label": stage_label,
            "payload": payload.model_dump(),
        })
        # Graph halts. Runtime surfaces the interrupt value externally.
        # Rich replies via NATS → ApprovalResponsePayload consumer resumes the graph.
        # When this line returns, `response` is the ApprovalResponsePayload.
        return handle_approval_response(response, build_id)
    # ... etc
```

The `forge.adapters.nats` approval subscriber consumes `agents.approval.forge.{build_id}.response`, calls the graph's resume API with the payload, and the `interrupt()` call above returns with the response.

PAUSED survives process restart via the SQLite + JetStream path (ADR-SP-013 crash recovery) — the LangGraph interrupt itself doesn't survive process crash, but the external approval protocol does. On restart, Forge detects PAUSED in SQLite, re-emits `ApprovalRequestPayload`, and re-enters `interrupt()` when the graph re-runs.

## Consequences

- **+** Custom pause wiring replaced by a LangGraph primitive — less code, better tested.
- **+** Clean separation: NATS adapter handles the wire; LangGraph handles the graph-halt; no custom condition-variable coordination.
- **+** `interrupt()` values are surfaced by the LangGraph server directly — a dashboard or CLI can query pending interrupts without knowing NATS.
- **+** Resume with typed payload works natively — no custom resume RPC.
- **−** Process crash during paused state loses the in-graph interrupt but preserves the external protocol (ApprovalRequest was already published, SQLite marks PAUSED). Restart re-emits → potential double-emit of ApprovalRequestPayload if Rich hasn't responded yet. Handled: responders are idempotent by `correlation_id`; first response wins.
- **−** DeepAgents `interrupt()` semantics must match expectations (fresh `response` passed back on resume). Verified in documentation; implementation-time check required.

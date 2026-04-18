# ADR-ARCH-027: No horizontal scaling — single instance, single process

- **Status:** Accepted
- **Date:** 2026-04-18
- **Session:** `/system-arch` Category 6

## Context

ADR-SP-012 mandates sequential builds. ADR-ARCH-014 enforces this at the transport layer (`max_ack_pending=1`). The question for Category 6: what happens as the fleet grows? If Rich onboards a second operator or a second project, does Forge scale out?

Scaling one Forge to serve many operators would reintroduce concurrency concerns — shared JetStream consumer with higher `max_ack_pending` would break ADR-SP-012's rationale (LLM rate limits, git safety, Graphiti write conflicts, debuggability). Fleet-master-index is explicit about account-based multi-tenancy (D13) — different tenants live in different NATS accounts.

## Decision

Forge has **no horizontal scaling mechanism**. The single process + single JetStream consumer + `max_concurrent: 1` is an architectural invariant, not a limitation.

**Fleet growth is handled by running more Forge instances**, one per operator or per major tenant:

- `forge` on GB10 in APPMILLA account — Rich's instance (V1)
- Future: `forge-finproxy` in FINPROXY account, possibly on the same or different host
- Future: `forge-{operator}` in the same account but different feature-id namespaces (if Rich ever works with a co-operator)

Each instance:
- Owns its own `~/.forge/forge.db`
- Runs its own JetStream consumer against its own account's `PIPELINE` stream
- Registers its own `AgentManifest` with a unique `agent_id` (e.g. `forge`, `forge-finproxy`)
- Has its own `forge.yaml` with its own model configuration + constitutional rules + permissions

## Consequences

- **+** Preserves ADR-SP-012's rationale without compromise — every instance processes one build at a time, deterministically.
- **+** Tenant isolation is physical — different Forges under different NATS accounts cannot see each other's traffic (D13).
- **+** Simpler code — no leader election, no distributed locking, no shared-state coordination.
- **+** Cost-control simple per instance — each Forge has its own budget envelope (ADR-ARCH-030).
- **−** Multiple instances = multiple containers to run + monitor. Operational cost linear with fleet size; acceptable at expected small-N.
- **−** Cross-Forge analytics (e.g. "how often does Forge A use capability X vs Forge B?") require querying multiple Graphiti scopes or aggregating at a layer above. V2 concern if ever needed.

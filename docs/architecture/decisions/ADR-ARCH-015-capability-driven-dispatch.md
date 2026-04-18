# ADR-ARCH-015: Capability-driven specialist dispatch — no `agent_id` hardcoding

- **Status:** Accepted
- **Date:** 2026-04-18
- **Session:** `/system-arch` Category 4 Revision 5

## Context

Initial Category 4 framing had Forge calling `call_agent_tool("product-owner-agent", "review_specification", ...)` with hardcoded `agent_id`s. Rich's Revision 5 correction: this inverts the entire purpose of `nats-core.AgentManifest` + `NATSKVManifestRegistry.find_by_intent/tool()`. Fleet D17 is explicit — *"new roles are automatically discoverable by the Forge without any Forge code changes."*

Hardcoding agent_ids means adding a QA or UX or Ideation agent later requires touching Forge's source. That is antithetical to the fleet's dynamic-capability design.

## Decision

- **`forge.discovery`** (domain core module) wraps `nats_core.NATSKVManifestRegistry`. Provides:
  - `list_capabilities()` — returns live snapshot of all registered agents' manifests.
  - `find_for_tool(tool_name)` — finds agents advertising a specific ToolCapability.
  - `find_for_intent(pattern, min_confidence=0.7)` — finds agents advertising a matching IntentCapability.
  - `resolve(tool_name, intent_pattern=None)` — resolution priority: exact tool match → intent-pattern match → unresolved (triggers degraded-mode reasoning in the agent).
  - 30-second cache TTL, invalidated live by `fleet.register` / `deregister` / `heartbeat` subscription (ADR-ARCH-017).
- **`forge.tools.dispatch_by_capability`** — single generic `@tool(parse_docstring=True)` function. Parameters: `tool_name`, `payload_json`, optional `intent_pattern`, `timeout_seconds`. Internally resolves the target agent via `forge.discovery`, publishes to `agents.command.{agent_id}`, subscribes to the reply subject per LES1 parity rule, returns the `ResultPayload` JSON.
- **No per-role tools** (`call_product_owner_tool`, `call_architect_tool`, etc.). One generic dispatch tool covers every specialist, present and future.

## Consequences

- **+** D17 honoured: new specialist agent ships → registers its manifest → next Forge build reasoning reads its description in `{available_capabilities}` → decides whether to use it. Zero Forge code changes.
- **+** Reduces tool-layer surface area — one dispatch tool vs N role-specific tools.
- **+** Matches the specialist-agent Phase 3 design — same pattern, no special-casing.
- **−** Reasoning model must read capability descriptions and decide usefulness — relies on good description quality in AgentManifests. Mitigated: manifest contract already requires human-readable `ToolCapability.description`.
- **−** Resolution ambiguity when two agents advertise the same tool — handled by highest trust_tier + confidence + lowest queue_depth (per nats-core registry behaviour).

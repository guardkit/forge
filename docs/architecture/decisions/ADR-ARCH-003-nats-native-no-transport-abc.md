# ADR-ARCH-003: NATS-native orchestration via `nats-core` adapters, no transport ABC

- **Status:** Accepted (restates ADR-SP-011 locally with implementation-layer specificity)
- **Date:** 2026-04-18
- **Session:** `/system-arch` Category 1

## Context

An earlier April 2026 draft proposed a `PipelineTransport` ABC with v0 (subprocess) and v1 (NATS) implementations. ADR-SP-011 in anchor v2.2 rejected this: NATS infrastructure is being stood up, building a subprocess transport only to replace it is waste. However, "NATS-native" could be misread as "no abstractions around NATS at all" — which would couple Forge's domain core to `nats_core` imports, breaking ADR-ARCH-001's Hexagonal boundary.

## Decision

Forge uses `nats-core` as a **library**, not a transport abstraction. `forge.adapters.nats` is a thin wrapper that:

- Instantiates `nats_core.NATSClient` with credentials from `forge.config`
- Owns the JetStream pull consumer for `pipeline.build-queued.*` (with `max_ack_pending=1`)
- Provides typed publish helpers for each `nats_core` payload Forge emits
- Wraps `NATSKVManifestRegistry` for fleet discovery (used by `forge.discovery`)
- Owns the reply-subject correlation map for `call_agent_tool()` round-trips (LES1 parity rule)

The domain-core modules (`forge.gating`, `forge.state_machine`, etc.) import zero NATS-related code. They receive typed Pydantic payloads and emit typed responses; the adapter handles the wire.

There is **no `PipelineTransport` ABC**. There is no in-memory transport for local development — `langgraph dev` connects to NATS on GB10 via Tailscale, matching the specialist-agent dev workflow. For unit tests, the adapter can be stubbed with a fake that implements the same interface, but it is not a production-shipped abstraction.

## Consequences

- **+** Preserves ADR-SP-011: no throwaway code, NATS from day one, JetStream durability from build #1.
- **+** Domain-core remains pure (ADR-ARCH-001 holds).
- **+** `nats-core` library's 98% test coverage carries forward — Forge's adapter is the only NATS-touching code to test against live NATS.
- **+** Consistent with `specialist-agent` Phase 3 pattern — same pattern, different agent.
- **−** Requires NATS infrastructure to be up to run Forge at all (no "subprocess fallback" mode). Acceptable — GB10 is the target environment; dev-from-MacBook uses Tailscale.
- **−** Unit tests must stub the adapter interface explicitly. Integration tests against live NATS are the confidence path.

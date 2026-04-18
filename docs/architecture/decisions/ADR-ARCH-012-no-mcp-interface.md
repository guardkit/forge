# ADR-ARCH-012: No MCP interface for Forge

- **Status:** Accepted
- **Date:** 2026-04-18
- **Session:** `/system-arch` Category 4

## Context

Fleet D16 documents that MCP adapters are used for *human-facing* specialist-agent interaction (Claude Desktop), and that *"MCP must NOT carry pipeline traffic — serialises full tool schema into context window on every invocation."* This is an observed cost problem at scale — every MCP tool invocation re-injects the entire tool inventory + schemas into context.

Forge has 11 GuardKit tools + `dispatch_by_capability` + approval + notification + graphiti + history tools (~17–20 tools). Exposing Forge over MCP would send all of these schemas into every tool call's context window. At Forge's build-length scale (200–500 turns), this is catastrophic.

Forge also has no human-interactive use case that Claude Desktop MCP serves — Rich interacts via CLI (`forge queue`); Jarvis interacts via NATS publishes; specialists interact via NATS request/reply.

## Decision

Forge does **not** expose an MCP interface. Forge is discoverable and controllable via:

- **CLI** (`forge queue | status | history | cancel | skip`) for Rich's local interaction.
- **NATS JetStream publish** to `pipeline.build-queued.*` for automated triggers (Jarvis, future notification adapters).
- **NATS `agents.command.forge`** (fleet-discovery only) for future control-plane commands ("pause all builds"). This is a control subject, not a build-queuing path.
- **NATS `fleet.register` + `fleet.heartbeat.forge`** for discoverability within the fleet.

## Consequences

- **+** Avoids MCP's per-call context overhead entirely — decisive for 200–500-turn builds.
- **+** Clear separation: MCP is for *specialist* human-facing interaction; NATS is for *machine* fleet interaction. Consistent with fleet D16.
- **+** Removes an entire adapter layer Forge would otherwise need to maintain.
- **−** Rich can't drive Forge from Claude Desktop directly via MCP. He can via CLI or via Jarvis (which *does* have MCP and routes to Forge via NATS). No functionality lost, just an extra indirection.
- **−** Tooling that assumes every agent has an MCP server (ecosystem conventions) won't find one for Forge. Acceptable — Forge's peers know it via NATS manifest.

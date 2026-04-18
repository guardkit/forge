# ADR-ARCH-001: Hexagonal modules inside a DeepAgents two-model orchestrator

- **Status:** Accepted
- **Date:** 2026-04-18
- **Session:** `/system-arch` Category 1

## Context

Forge needs a structural pattern that fits three hard constraints:

1. **Runtime topology is prescribed** by CLAUDE.md — "LangChain DeepAgents two-model architecture, LangGraph with hierarchical subagent composition."
2. **No internal transport abstraction** — ADR-SP-011 in the anchor v2.2 forbids a `PipelineTransport` ABC; NATS is the transport, not a plug-point.
3. The logic that must be unit-testable without NATS/SQLite/subprocess (confidence gating, state transitions, stage orchestration, notification decisions) is substantial — a purely event-driven or layered pattern doesn't isolate it cleanly.

DDD is overkill for a single-process orchestrator with one owner; plain Modular Monolith under-specifies the port/adapter seam.

## Decision

Adopt **Clean/Hexagonal at the module level, inside a DeepAgents `create_deep_agent(...)` compiled state graph at the runtime level**.

- The DeepAgents graph is the outer shell (reasoning loop, built-in tools, sub-agent dispatch).
- Pure domain modules (`forge.gating`, `forge.state_machine`, `forge.notifications`, `forge.learning`, `forge.calibration`, `forge.discovery`) have no `import nats_core`, `import sqlite3`, `import subprocess` — unit-testable in isolation.
- Thin adapters at the edges (`forge.adapters.{nats, sqlite, graphiti, guardkit}`) own I/O.
- Forge-specific `@tool(parse_docstring=True)` functions are the DeepAgents-facing boundary of the adapter layer.

## Consequences

- **+** Gating, learning, calibration, and state-machine logic unit-testable without infrastructure mocks.
- **+** Aligns with the `langchain-tool-decorator-specialist` + `deepagents-orchestrator-specialist` + `domain-context-injection-specialist` rules already defined in `.claude/rules/`.
- **+** Adapters can be swapped in tests (`InMemoryManifestRegistry` from nats-core; SQLite `:memory:`; fake Graphiti) without touching domain code.
- **+** Preserves ADR-SP-011 (NATS-native, no transport ABC) — we have *adapters* around nats-core, not a transport abstraction layer.
- **−** Slightly more ceremony than a plain Modular Monolith; 5 clean groups (Shell / Domain / Tools / Adapters / Cross-cutting) to maintain.
- **−** New developers must understand "nothing in `forge.*` imports `subprocess`; that's always in `forge.adapters.*`" — enforced by lint rule or code review, not framework.

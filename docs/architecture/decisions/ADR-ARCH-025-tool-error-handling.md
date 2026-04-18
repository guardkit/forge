# ADR-ARCH-025: Tool error handling — return structured error strings, never raise

- **Status:** Accepted
- **Date:** 2026-04-18
- **Session:** `/system-arch` Category 5

## Context

The `langchain-tool-decorator-specialist` rule (pre-existing in `.claude/rules/`) is explicit: *"LangChain tool functions using the `@tool(parse_docstring=True)` decorator pattern... all returning strings and wrapping logic in try/except to never raise."* Exceptions escaping a tool break the DeepAgents graph's reasoning loop — the model sees a raw traceback and often cannot recover.

The layered policy: failures absorb close to the source, surface as *evidence* to the reasoning model, never escape as tracebacks.

## Decision

1. **Tool layer** (`@tool(parse_docstring=True)`): every tool wraps logic in `try/except Exception` and returns a string. On failure, returns structured JSON serialised to string:
   ```json
   {
     "error": "Graphiti query failed",
     "error_type": "GraphitiUnavailable",
     "recoverable": true,
     "context": {"query": "...", "graph_host": "whitestocks:6379"}
   }
   ```
   Never raises. The reasoning model sees the error, reasons about alternatives.

2. **Adapter layer**: typed exceptions — `NATSUnavailable`, `GraphitiUnavailable`, `SpecialistUnavailable`, `GuardKitFailed`, `GitOperationFailed`, `GitHubUnavailable`. Caught by tools; translated to error strings with context.

3. **Degraded mode is a reasoning input, not a config fallback.** When an adapter reports unavailable, `forge.discovery.health_snapshot()` reflects it. The next prompt retrieval includes it. The reasoning model **decides** — use a local fallback, pause for Rich, skip the work, abort the build. No static fallback mappings (per ADR-ARCH-019).

4. **Retry policy** at the right layer, not inside tools:
   - NATS: nats-py client-level reconnect (exponential backoff, `max_reconnect_attempts: 60`).
   - LLM: LangChain built-in retries.
   - Graphiti: fail-fast; retry on *next* query.
   - Specialist dispatch: one in-build retry; then reasoning decides fallback.
   - GuardKit subprocess: GuardKit handles its own retries; Forge does not retry subprocesses.

5. **Crash recovery**: unchanged from ADR-SP-013 (JetStream redelivers, SQLite reconciles, retry from scratch).

## Consequences

- **+** Reasoning model never sees a traceback — always sees structured evidence of what went wrong.
- **+** Degraded modes emerge naturally from reasoning + evidence, no hardcoded fallback tables.
- **+** Adapter exceptions are typed and grep-able — easier to trace causes in logs.
- **+** Consistent with the langchain-tool-decorator-specialist rule already in .claude/rules/.
- **−** Requires discipline — every tool author must remember the try/except wrapper. Mitigated by a base decorator (`@forge_tool`) and lint check.
- **−** Error details in strings → the reasoning model must parse them. Mitigated by consistent JSON schema.

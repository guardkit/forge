# ADR-ARCH-022: Dual agent memory — LangGraph Memory Store + Graphiti

- **Status:** Accepted
- **Date:** 2026-04-18
- **Session:** `/system-arch` Category 4 Revision 9

## Context

DeepAgents 0.5.3 inherits LangGraph's long-term Memory Store — a thread-scoped key-value primitive for fast in-graph recall across turns. Forge also uses Graphiti (FalkorDB) for fleet-wide, cross-build learning. These are distinct primitives; the question is whether they overlap, conflict, or complement.

## Decision

Use both, with clear role separation:

| Store | Purpose | Scope | Access pattern |
|---|---|---|---|
| **LangGraph Memory Store** | Fast in-graph recall across turns within one build-thread. "What did I just decide about Stage 2?" without SQLite round-trip. | Per-thread (per-build) | Read/write from any reasoning turn or sub-agent within the graph |
| **Graphiti** (`forge_pipeline_history` + `forge_calibration_history`) | Fleet-wide, cross-build learning substrate. `CalibrationEvent`s, gate decisions, override events, session outcomes, `CapabilityResolution` records. | Fleet-wide | Retrieved at build start into system prompt; written back after each significant step |
| **SQLite `forge.db`** (pre-existing per ADR-SP-013) | Authoritative build + stage_log audit trail. CLI read path. Crash recovery. | Local, durable | CLI reads directly; agent writes at state transitions |

**Not used:** LangGraph checkpointer (per ADR-ARCH-009 — JetStream+SQLite provides crash durability, Memory Store provides in-thread recall, checkpointer would duplicate).

Boundary rules:
- **Memory Store** is for transient in-thread context. Don't write anything a future build needs — that belongs in Graphiti.
- **Graphiti** is for durable cross-build priors. Don't use as an in-thread cache — that's what Memory Store is for.
- **SQLite** is for structured audit + CLI reads. Build status, stage labels, coach scores, durations, outcomes.

## Consequences

- **+** Each store plays to its strength — fast in-graph recall vs durable cross-session learning vs queryable audit.
- **+** No duplication between Memory Store (in-thread) and Graphiti (cross-build) — distinct temporal scopes.
- **+** Consistent with ADR-ARCH-009: the checkpointer was the overlap we wanted to avoid.
- **−** Developers must know which store to use for which data — documented in `.claude/rules/guidance/` and module-level docstrings.
- **−** Triple-store persistence model (Memory Store + Graphiti + SQLite) is operationally more complex than single-store; mitigated by clear separation of concerns and per-store Pydantic schemas.

# ADR-ARCH-009: Omit LangGraph checkpointer — JetStream + SQLite sufficient

- **Status:** Accepted
- **Date:** 2026-04-18
- **Session:** `/system-arch` Category 3

## Context

LangGraph ships a SQLite-backed checkpointer for graph-state durability across process crashes. Forge could use it (adding a second SQLite file or co-locating in `~/.forge/forge.db`). The question is whether this adds meaningful durability beyond what ADR-SP-013 already provides (JetStream as durable queue + SQLite as authoritative build history + retry-from-scratch policy on INTERRUPTED).

## Decision

**Do not use the LangGraph checkpointer.**

Durability model:
- **JetStream** redelivers unacknowledged `pipeline.build-queued.*` messages on `AckWait` timeout → build retry.
- **SQLite** reconciliation on startup: any `RUNNING`/`PREPARING` row → `INTERRUPTED`; redelivered message triggers fresh PREPARING (ADR-SP-013's retry-from-scratch policy).
- **PAUSED** rows survive restart; Forge re-enters paused state and re-emits `ApprovalRequestPayload` (ADR-ARCH-021's `interrupt()` does not survive process crash, but the external protocol state does).

In-graph state (conversation history, todos, partial tool outputs) is **intentionally discarded** on crash — ADR-SP-013 chose retry-from-scratch over resume because a partial working tree + half-written commits is an inconsistent foundation.

Memory Store (ADR-ARCH-022) is a *different* primitive — per-thread fast recall, not crash-durability — and complements rather than replaces the JetStream+SQLite durability path.

## Consequences

- **+** Simpler persistence model — two stores (JetStream + SQLite), not three.
- **+** Consistent with ADR-SP-013: SQLite owns history, JetStream owns queue, no overlap.
- **+** Easier to reason about crash behaviour: every build resumes from PREPARING with a fresh working tree.
- **−** A 45-minute autobuild that crashes at minute 44 retries from scratch → wasted work. Accepted per ADR-SP-012 ("quality over speed"). Mitigations: rare if GB10 is stable; Memory Store reduces re-work within a thread but not across crashes.
- **−** Some LangGraph debugging tooling expects a checkpointer; Forge gives up mid-run introspection into graph state. Replaced by SQLite `stage_log` for observability.

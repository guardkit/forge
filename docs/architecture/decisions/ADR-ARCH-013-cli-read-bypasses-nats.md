# ADR-ARCH-013: CLI read path bypasses NATS — SQLite direct for status/history

- **Status:** Accepted
- **Date:** 2026-04-18
- **Session:** `/system-arch` Category 4

## Context

Rich's primary interaction with Forge is via the CLI. Read-heavy commands (`forge status`, `forge history`, `forge history --feature FEAT-XXX`) could route through NATS (publish a query, subscribe to a response) or read SQLite directly. NATS adds a round-trip, requires Forge process to be running, and introduces failure modes (NATS down → `forge status` down). SQLite is local, fast, and the authoritative history store (ADR-SP-013 + ADR-ARCH-009).

Write-heavy commands (`forge queue`, `forge cancel`, `forge skip`) must publish to NATS because that's the trigger/control mechanism — SQLite cannot notify the running Forge process directly.

## Decision

**CLI read path reads SQLite directly.** No NATS round-trip, no RPC, no Forge process required for reads.

- `forge status` → `SELECT * FROM builds WHERE status IN ('RUNNING','PAUSED','PREPARING')` + last N completed
- `forge history` → `SELECT * FROM builds ORDER BY started_at DESC LIMIT 50`
- `forge history --feature FEAT-XXX` → joins `builds` + `stage_log` for the feature

**CLI write path publishes to NATS.** `forge queue`, `forge cancel`, `forge skip` emit typed payloads to the appropriate topic; the running Forge process consumes.

The CLI and Forge process share the same SQLite file via WAL mode — concurrent readers + single writer are safe.

## Consequences

- **+** `forge status`/`history` available even when NATS is unreachable — Rich can still audit past builds during NATS outages.
- **+** Sub-200ms CLI reads (local SQLite) vs ~50–100ms NATS round-trip.
- **+** Reduces NATS topic bloat — no request/reply subjects for every CLI query.
- **+** Simplifies the CLI — no NATS subscriber + timeout logic for reads.
- **−** CLI must be run on the same host as the Forge process (or have access to the SQLite file via shared mount). In the Docker deployment (ADR-ARCH-011), the volume is mounted; Rich runs the CLI inside the container via `docker exec` or the host CLI via a mounted `~/.forge/forge.db`. Trivially solvable.
- **−** Two code paths in the CLI (SQLite for reads, NATS for writes) — slight complexity tax; documented in `forge.cli` module docstrings.

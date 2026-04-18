# ADR-ARCH-017: Live fleet-registry watching for hot-swap capability detection

- **Status:** Accepted
- **Date:** 2026-04-18
- **Session:** `/system-arch` Category 4 Revision 5

## Context

If Forge resolves capabilities at build-start only, a newly-registered fleet agent isn't visible until the *next* build. Rich's vision: ship a new QA agent mid-session, next build picks it up, no Forge restart. Cached capability snapshots can go stale fast.

## Decision

`forge.discovery` subscribes to the fleet lifecycle topics via `forge.adapters.nats`:

- `fleet.register` — new agent came online → invalidate cache, refresh snapshot.
- `fleet.deregister` — agent graceful shutdown → invalidate cache.
- `fleet.heartbeat.>` — track last-seen timestamps per agent; if an agent stops heartbeating for `heartbeat_timeout_seconds` (default 90), flip its local health status to `degraded` without fully deregistering (agent may return).

Cache TTL as a belt-and-braces: 30-second default refresh even absent watch events (matches heartbeat interval).

**In-build hot-swap**: after each `dispatch_by_capability` call returns (or times out), `forge.discovery.resolve()` is re-queried for the next capability the reasoning model wants. The reasoning model therefore sees the current fleet state on every subsequent dispatch — a new agent registered at build-minute 15 is available from build-minute 15.

## Consequences

- **+** Hot-swap works: new specialist agents come online → picked up in the current build's next dispatch resolution.
- **+** Agent failure detection is cooperative with NATS heartbeat mechanism; no custom healthcheck protocol.
- **+** Cached snapshots stale-by-30-seconds at worst — acceptable bound.
- **−** Subscriber overhead on `fleet.heartbeat.>` — at ~30s cadence × N agents, modest traffic.
- **−** Reasoning model may choose to use a just-arrived capability without calibration priors → naturally conservative gate (flag-for-review) per ADR-ARCH-019. This is working-as-intended: first use of a capability is itself a calibration event.

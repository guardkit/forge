---
id: TASK-NFI-003
title: Implement forge.discovery domain (cache + resolve + Protocols)
task_type: feature
status: in_review
priority: high
created: 2026-04-24 00:00:00+00:00
updated: 2026-04-24 00:00:00+00:00
parent_review: TASK-REV-NF20
feature_id: FEAT-FORGE-002
wave: 2
implementation_mode: task-work
complexity: 6
dependencies:
- TASK-NFI-001
- TASK-NFI-002
tags:
- discovery
- cache
- domain
- protocol
- pure-python
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002
  base_branch: main
  started_at: '2026-04-25T14:02:53.103373'
  last_updated: '2026-04-25T14:14:49.931924'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-25T14:02:53.103373'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# Task: Implement forge.discovery domain (cache + resolve + Protocols)

## Description

Build the pure-domain discovery layer described in
`docs/design/models/DM-discovery.md`. This package owns:

1. `DiscoveryCacheEntry` — in-memory cache record (AgentManifest + heartbeat snapshot).
2. `CapabilityResolution` — output of `resolve()`, destined for Graphiti write-back.
3. `Clock` — `Protocol` for time-provider injection (used by cache TTL + stale check).
4. `FleetEventSink` — `Protocol` exposing `upsert_agent`, `remove_agent`,
   `update_heartbeat` — the surface the fleet watcher calls.
5. `DiscoveryCache` — asyncio-lock-protected dict wrapper implementing `FleetEventSink`.
6. `resolve(tool_name, intent_pattern=None, min_confidence=0.7)` — the resolution
   algorithm with tool-exact → intent-fallback → tie-break ordering.

**No NATS imports in this package.** It receives `AgentManifest` by reference
from `nats-core` only because that type is the published schema; the package
does not import `nats_core.client` or any transport concern.

## Acceptance Criteria

- [ ] Package layout: `src/forge/discovery/{__init__.py, cache.py, resolve.py, protocol.py, models.py}`
- [ ] `grep -r "nats.aio\|import nats\|NatsClient" src/forge/discovery/` returns no hits
- [ ] `Clock` protocol with a single `now() -> datetime` method; default implementation reads `datetime.now(UTC)`
- [ ] `FleetEventSink` protocol with `upsert_agent(manifest)`, `remove_agent(agent_id)`, `update_heartbeat(agent_id, hb, status_changed)` methods
- [ ] `DiscoveryCache` implements `FleetEventSink`; mutations guarded by `asyncio.Lock`
- [ ] `resolve()` honours the algorithm in DM-discovery §3 (tool-exact → intent-fallback → tie-break by trust_tier / confidence / queue_depth)
- [ ] Degraded agents excluded from primary resolution (status == "degraded" filter)
- [ ] `resolve()` returns `(None, CapabilityResolution(match_source="unresolved"))` when no candidate found
- [ ] Trust tier ranking: core(0) > specialist(1) > extension(2)
- [ ] Cache TTL check uses injected `Clock`, not `datetime.now()` directly — boundary tests use a `FakeClock`
- [ ] Unit tests cover: exact-match, intent-fallback, tie-break-by-tier, tie-break-by-queue-depth, stale-agent-exclusion, unresolved, racing upsert/remove (asyncio.gather)
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Seam Note

This task is a **producer** for Integration Contract FleetEventSink Protocol (§4):
- Consumer: TASK-NFI-005 (fleet_watcher calls `upsert_agent` / `remove_agent` / `update_heartbeat`)

Producer and consumer must agree on method signatures before Wave 3 starts.

## Implementation Notes

- `DiscoveryCache` is the concrete `FleetEventSink` implementation; the protocol
  exists so tests can supply a simple in-memory double
- Keep `CapabilityResolution.persist()` out of this task — Graphiti write-back
  is covered by a later feature, not FEAT-FORGE-002
- `resolve()` returns the resolution object even on miss (match_source="unresolved")
  so the caller can log/persist it

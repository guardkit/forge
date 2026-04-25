---
autobuild_state:
  base_branch: main
  current_turn: 3
  last_updated: '2026-04-25T12:51:56.535449'
  max_turns: 30
  started_at: '2026-04-25T12:25:32.046884'
  turns:
  - coach_success: true
    decision: feedback
    feedback: '- Task-work produced a report with 2 of 3 required agent invocations.
      Missing phases: 3 (Implementation). Invoke these agents via the Task tool before
      re-emitting the report:

      - Phase 3: `python-api-specialist` (Implementation)'
    player_success: true
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    timestamp: '2026-04-25T12:25:32.046884'
    turn: 1
  - coach_success: true
    decision: feedback
    feedback: '- Task-work produced a report with 2 of 3 required agent invocations.
      Missing phases: 3 (Implementation). Invoke these agents via the Task tool before
      re-emitting the report:

      - Phase 3: `python-api-specialist` (Implementation)'
    player_success: true
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    timestamp: '2026-04-25T12:38:00.106125'
    turn: 2
  - coach_success: true
    decision: feedback
    feedback: '- Task-work produced a report with 2 of 3 required agent invocations.
      Missing phases: 3 (Implementation). Invoke these agents via the Task tool before
      re-emitting the report:

      - Phase 3: `python-api-specialist` (Implementation)'
    player_success: true
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    timestamp: '2026-04-25T12:44:25.014312'
    turn: 3
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002
complexity: 6
created: 2026-04-24 00:00:00+00:00
dependencies:
- TASK-NFI-001
- TASK-NFI-002
feature_id: FEAT-FORGE-002
id: TASK-NFI-003
implementation_mode: task-work
parent_review: TASK-REV-NF20
priority: high
status: design_approved
tags:
- discovery
- cache
- domain
- protocol
- pure-python
task_type: feature
test_results:
  coverage: null
  last_run: null
  status: pending
title: Implement forge.discovery domain (cache + resolve + Protocols)
updated: 2026-04-24 00:00:00+00:00
wave: 2
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
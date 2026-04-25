---
complexity: 5
consumer_context:
- consumes: FleetEventSink
  driver: forge.discovery.protocol
  format_note: Calls `sink.upsert_agent(manifest)`, `sink.remove_agent(agent_id)`,
    `sink.update_heartbeat(agent_id, hb, status_changed)` — must match protocol signatures
    exactly; mutations happen inside the sink's asyncio.Lock
  framework: Python asyncio Protocol (PEP 544)
  task: TASK-NFI-003
- consumes: ForgeConfig.fleet
  driver: pyyaml + pydantic
  format_note: FleetConfig.stale_heartbeat_seconds (int, default 90) drives background
    stale-agent sweeper
  framework: Pydantic v2 BaseModel
  task: TASK-NFI-001
created: 2026-04-24 00:00:00+00:00
dependencies:
- TASK-NFI-003
feature_id: FEAT-FORGE-002
id: TASK-NFI-005
implementation_mode: task-work
parent_review: TASK-REV-NF20
priority: high
status: design_approved
tags:
- nats
- adapter
- watcher
- subscriber
- fleet
- cache-invalidation
task_type: feature
test_results:
  coverage: null
  last_run: null
  status: pending
title: Implement fleet_watcher (subscribe + delegate to FleetEventSink)
updated: 2026-04-24 00:00:00+00:00
wave: 3
---

# Task: Implement fleet_watcher (subscribe + delegate to FleetEventSink)

## Description

Create `src/forge/adapters/nats/fleet_watcher.py` owning the live
subscription described in `API-nats-fleet-lifecycle.md §3` and
`DM-discovery.md §4`.

Responsibilities:

- `async def watch(nats_client, sink: FleetEventSink)` — subscribe via
  `nats_client.watch_fleet(callback=...)`; dispatch to `FleetEventSink`:
  - `fleet.register` → `sink.upsert_agent(manifest)`
  - `fleet.deregister` → `sink.remove_agent(agent_id)`
  - `fleet.heartbeat.>` → `sink.update_heartbeat(agent_id, hb, status_changed)`
- `async def stale_sweeper(sink, clock, interval_s=10)` — background task that
  marks agents with no heartbeat for > `stale_heartbeat_seconds` as degraded
- Malformed events rejected + logged; the watcher continues (Group C @negative)

## Acceptance Criteria

- [ ] Three event types dispatched correctly to `FleetEventSink` methods
- [ ] Events failing `AgentManifest.model_validate` / `AgentHeartbeatPayload.model_validate` are logged (WARN) and dropped; subsequent valid events still processed
- [ ] `status_changed` flag computed correctly (compares previous heartbeat status to new one)
- [ ] Re-registration with newer manifest version supersedes (idempotency — no duplicate cache entries)
- [ ] `stale_sweeper` marks degraded agents whose `last_heartbeat_at` age exceeds `stale_heartbeat_seconds` (uses injected Clock)
- [ ] Racing register+deregister test (asyncio.gather) — final cache state consistent with one-event-wins semantics
- [ ] `watch` survives transient `nats_client` errors via reconnect loop; test with a `nats_client` mock that raises once then recovers
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Seam Tests

```python
"""Seam test: verify FleetEventSink protocol contract from TASK-NFI-003."""
import pytest
from forge.discovery.protocol import FleetEventSink


@pytest.mark.seam
@pytest.mark.integration_contract("FleetEventSink")
def test_fleet_event_sink_protocol_shape():
    """Verify the FleetEventSink protocol exposes the expected surface.

    Contract: upsert_agent(manifest), remove_agent(agent_id),
              update_heartbeat(agent_id, hb, status_changed)
    Producer: TASK-NFI-003
    """
    # Verify the methods exist on the protocol
    assert hasattr(FleetEventSink, "upsert_agent")
    assert hasattr(FleetEventSink, "remove_agent")
    assert hasattr(FleetEventSink, "update_heartbeat")
```

```python
"""Seam test: verify ForgeConfig.fleet contract from TASK-NFI-001."""
import pytest
from forge.config.models import FleetConfig


@pytest.mark.seam
@pytest.mark.integration_contract("ForgeConfig.fleet")
def test_forge_config_stale_threshold():
    """Verify stale_heartbeat_seconds is available for the sweeper.

    Contract: FleetConfig.stale_heartbeat_seconds: int, default 90
    Producer: TASK-NFI-001
    """
    cfg = FleetConfig()
    assert cfg.stale_heartbeat_seconds == 90
```

## Implementation Notes

- The watcher does **not** mutate the cache directly — all mutations go via `FleetEventSink`
- The sink's asyncio.Lock guarantees exclusivity; no additional locking here
- On reconnect, re-subscribing pulls a fresh manifest for each registered agent
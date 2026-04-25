---
complexity: 5
consumer_context:
- consumes: ForgeConfig.fleet
  driver: pyyaml + pydantic
  format_note: FleetConfig.heartbeat_interval_seconds (int, default 30) drives heartbeat
    loop cadence; FleetConfig.stale_heartbeat_seconds (int, default 90) is read by
    fleet_watcher, not this task
  framework: Pydantic v2 BaseModel
  task: TASK-NFI-001
- consumes: FORGE_MANIFEST
  driver: nats-core >= 0.2.0
  format_note: 'Module-level constant imported directly: `from forge.fleet.manifest
    import FORGE_MANIFEST`; passed as-is to nats_client.register_agent()'
  framework: nats-core AgentManifest
  task: TASK-NFI-002
created: 2026-04-24 00:00:00+00:00
dependencies:
- TASK-NFI-001
- TASK-NFI-002
- TASK-NFI-003
feature_id: FEAT-FORGE-002
id: TASK-NFI-004
implementation_mode: task-work
parent_review: TASK-REV-NF20
priority: high
status: design_approved
tags:
- nats
- adapter
- publisher
- heartbeat
- fleet
task_type: feature
test_results:
  coverage: null
  last_run: null
  status: pending
title: Implement fleet_publisher (register / heartbeat-loop / deregister)
updated: 2026-04-24 00:00:00+00:00
wave: 3
---

# Task: Implement fleet_publisher (register / heartbeat-loop / deregister)

## Description

Create `src/forge/adapters/nats/fleet_publisher.py` owning Forge's
self-registration, periodic heartbeats, and graceful deregistration
described in `API-nats-fleet-lifecycle.md §2`.

Responsibilities:

- `async def register_on_boot(nats_client)` — call `nats_client.register_agent(FORGE_MANIFEST)`
- `async def heartbeat_loop(nats_client, cancel_event)` — infinite loop publishing
  `AgentHeartbeatPayload` every `FleetConfig.heartbeat_interval_seconds`; built
  payload includes current status, queue_depth (from JetStream consumer info),
  active_tasks, uptime_seconds
- `async def deregister(nats_client, reason="shutdown")` — call
  `nats_client.deregister_agent("forge", reason=reason)`; wired to SIGTERM handler

The heartbeat loop **must be independent of registry reachability**
(scenario Group E `@integration`). If the bus is up but the registry KV is
temporarily unreachable, heartbeats still publish.

## Acceptance Criteria

- [ ] `register_on_boot` publishes `AgentManifest` to `fleet.register` and puts it in `agent-registry` KV (via `nats_client.register_agent`)
- [ ] `heartbeat_loop` publishes every `FleetConfig.heartbeat_interval_seconds` using an injected Clock; no wall-clock sleeps in tests
- [ ] Heartbeat payload reflects `active_tasks=1` when a build is in-flight, `0` otherwise (read from a `StatusProvider` protocol so tests inject state)
- [ ] `deregister` is idempotent — calling twice does not raise
- [ ] Heartbeat loop catches and logs transient publish failures; does not exit the loop
- [ ] Registry unreachability test (mock `nats_client.get_fleet_registry` to raise) — heartbeats continue
- [ ] `heartbeat_loop` exits cleanly when `cancel_event` is set
- [ ] SIGTERM integration test: deregister is called before the heartbeat task is cancelled
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Seam Tests

The following seam tests validate the integration contracts with the producer tasks. Implement these tests to verify the boundaries before integration.

```python
"""Seam test: verify ForgeConfig.fleet contract from TASK-NFI-001."""
import pytest
from forge.config.models import FleetConfig


@pytest.mark.seam
@pytest.mark.integration_contract("ForgeConfig.fleet")
def test_forge_config_fleet_format():
    """Verify FleetConfig matches the expected format.

    Contract: heartbeat_interval_seconds (int, default 30),
              stale_heartbeat_seconds (int, default 90)
    Producer: TASK-NFI-001
    """
    cfg = FleetConfig()
    assert cfg.heartbeat_interval_seconds == 30
    assert cfg.stale_heartbeat_seconds == 90
    assert isinstance(cfg.heartbeat_interval_seconds, int)
```

```python
"""Seam test: verify FORGE_MANIFEST contract from TASK-NFI-002."""
import pytest
from forge.fleet.manifest import FORGE_MANIFEST


@pytest.mark.seam
@pytest.mark.integration_contract("FORGE_MANIFEST")
def test_forge_manifest_contract():
    """Verify FORGE_MANIFEST is importable as a module-level AgentManifest.

    Contract: Importable as `from forge.fleet.manifest import FORGE_MANIFEST`;
              agent_id == "forge"; passes straight through to register_agent.
    Producer: TASK-NFI-002
    """
    assert FORGE_MANIFEST.agent_id == "forge"
    assert FORGE_MANIFEST.trust_tier == "core"
    # Secret-free check
    dumped = FORGE_MANIFEST.model_dump_json()
    for forbidden in ("api_key", "token", "password", "secret", "credential"):
        assert forbidden.lower() not in dumped.lower(), f"Secret-like field found: {forbidden}"
```

## Implementation Notes

- `nats_client` is injected at the adapter boundary; tests use an `AsyncMock`
- `StatusProvider` protocol: `get_current_status() -> Literal["ready", "busy", "degraded", "draining"]`, `get_active_tasks() -> int`, `get_queue_depth() -> int`
- Defer SIGTERM wiring to the app-level entrypoint; this module just exposes `deregister`
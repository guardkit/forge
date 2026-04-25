---
id: TASK-SAD-010
title: "NATS adapter: specialist_dispatch.py — bind, publish, deliver"
task_type: feature
status: backlog
priority: high
created: 2026-04-25T00:00:00Z
updated: 2026-04-25T00:00:00Z
parent_review: TASK-REV-SAD3
feature_id: FEAT-FORGE-003
wave: 4
implementation_mode: task-work
complexity: 6
dependencies: [TASK-SAD-003, TASK-SAD-006]
tags: [dispatch, nats, transport, adapter, jetstream]
consumer_context:
  - task: TASK-SAD-003
    consumes: CorrelationKey
    framework: "asyncio"
    driver: "nats.aio"
    format_note: "Reply subject must be agents.result.{matched_agent_id}.{correlation_key} where correlation_key is 32 lowercase hex (per CorrelationKey contract from TASK-SAD-003). Headers carry correlation_key, requesting_agent_id (forge), dispatched_at (ISO8601 UTC)."
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Task: NATS adapter — specialist_dispatch.py — bind, publish, deliver

## Description

Thin transport adapter binding the pure-domain `CorrelationRegistry`
(TASK-SAD-003) and `DispatchOrchestrator` (TASK-SAD-006) to JetStream.
Implements two protocols the domain layer declares:

1. `ReplyChannel` — used by `CorrelationRegistry.bind()` to establish a
   per-correlation reply subscription on
   `agents.result.{matched_agent_id}.{correlation_key}`.
2. `DispatchCommandPublisher` — used by `DispatchOrchestrator` to publish
   the dispatch command on `agents.command.{matched_agent_id}` (singular
   convention adopted fleet-wide; see Graphiti `architecture_decisions`
   group and FEAT-FORGE-002 ADR adoption).

This adapter is the ONLY module in this feature that imports `nats.aio`.

## Subject layout

| Direction | Subject | Notes |
|---|---|---|
| Forge → specialist | `agents.command.{matched_agent_id}` | singular convention (DRD-001..004) |
| specialist → Forge | `agents.result.{matched_agent_id}.{correlation_key}` | per-correlation suffix |
| audit (informational) | `agents.command.{matched_agent_id}` (JetStream-resident) | PubAck on this stream is NOT success |

## Headers (on dispatch command)

| Header | Format | Purpose |
|---|---|---|
| `correlation_key` | 32 lowercase hex | reply routing |
| `requesting_agent_id` | `"forge"` | source identifier |
| `dispatched_at` | ISO 8601 UTC | observability |

## Interface

```python
# src/forge/adapters/nats/specialist_dispatch.py
from typing import Protocol
from forge.dispatch.correlation import CorrelationRegistry, CorrelationBinding
from forge.dispatch.models import DispatchAttempt
from forge.dispatch.persistence import DispatchParameter


class ReplyChannel(Protocol):
    """Domain-side protocol implemented by this adapter."""
    async def subscribe_reply(
        self, matched_agent_id: str, correlation_key: str
    ) -> None: ...
    async def unsubscribe_reply(self, correlation_key: str) -> None: ...


class DispatchCommandPublisher(Protocol):
    """Domain-side protocol implemented by this adapter."""
    async def publish_dispatch(
        self, attempt: DispatchAttempt, parameters: list[DispatchParameter]
    ) -> None: ...


class NatsSpecialistDispatchAdapter:
    """JetStream binding for dispatch + reply correlation."""

    def __init__(self, nats_client, registry: CorrelationRegistry) -> None: ...

    async def subscribe_reply(self, matched_agent_id: str, correlation_key: str) -> None:
        """Subscribe to agents.result.{matched_agent_id}.{correlation_key}.

        MUST be synchronous-on-return: returns only after the subscription
        is fully established. The CorrelationRegistry's bind() relies on
        this contract to satisfy the subscribe-before-publish invariant.
        """
        ...

    async def unsubscribe_reply(self, correlation_key: str) -> None:
        """Tear down the per-correlation subscription. Idempotent."""
        ...

    async def publish_dispatch(
        self, attempt: DispatchAttempt, parameters: list[DispatchParameter]
    ) -> None:
        """Publish on agents.command.{matched_agent_id}.

        PubAck on the audit stream MUST NOT be observed as success.
        The CorrelationRegistry waits for an actual reply payload.
        """
        ...

    def _on_reply_received(self, msg) -> None:
        """Callback registered with the NATS subscription.

        Extracts source_agent_id from msg headers, decodes payload, and
        forwards to registry.deliver_reply(). Authentication is enforced
        in the registry, not here.
        """
        ...
```

## Acceptance Criteria

- [ ] `src/forge/adapters/nats/specialist_dispatch.py` implements
      `NatsSpecialistDispatchAdapter` with `subscribe_reply`,
      `unsubscribe_reply`, `publish_dispatch`.
- [ ] Subjects use singular convention: `agents.command.{agent_id}` and
      `agents.result.{agent_id}.{correlation_key}`. Verify via regex in
      tests.
- [ ] Headers on the published dispatch include `correlation_key`,
      `requesting_agent_id="forge"`, `dispatched_at` (ISO 8601 UTC).
- [ ] `subscribe_reply()` returns ONLY after the underlying NATS
      subscription is fully active (await any readiness/ack). Test by
      asserting that a reply published immediately after `subscribe_reply`
      returns is received.
- [ ] PubAck on the audit stream does NOT trigger
      `registry.deliver_reply()` — it is observed by the publisher only as
      the "publish was sent" signal, not as the dispatch outcome.
- [ ] `unsubscribe_reply()` is idempotent (safe to call twice with the
      same correlation_key).
- [ ] `_on_reply_received()` extracts the source agent ID from msg headers
      and forwards to `registry.deliver_reply()`. The adapter does NOT
      attempt to authenticate — that lives in the registry (TASK-SAD-003).
- [ ] Compatibility: existing `tests/bdd/conftest.py:FakeNatsClient`
      fixture is a drop-in test double — do NOT introduce a parallel test
      transport. Add fake-side recording for subscribe/unsubscribe events
      if needed.
- [ ] All modified files pass project-configured lint/format checks with
      zero errors.

## Seam Tests

```python
"""Seam test: verify CorrelationKey contract on the wire."""
import re
import pytest


@pytest.mark.seam
@pytest.mark.integration_contract("CorrelationKey")
async def test_dispatch_publish_subject_format(adapter, fake_nats_client, registry):
    """Verify dispatch publishes on agents.command.{agent_id} with the
    correct CorrelationKey in the reply subject and headers.

    Contract: CorrelationKey from TASK-SAD-003 (32 lowercase hex chars).
    Producer: TASK-SAD-003
    """
    key = registry.fresh_correlation_key()
    await adapter.subscribe_reply("po-agent", key)

    last_subscription = fake_nats_client.subscriptions[-1]
    assert re.fullmatch(
        r"agents\.result\.[a-z0-9-]+\.[0-9a-f]{32}",
        last_subscription.subject,
    )
```

## Implementation Notes

- This is the ONLY module that may import `nats.aio`. The orchestrator,
  registry, parser, retry coordinator, and outcome helper must remain
  free of NATS imports.
- Reuse the `nats_client` already created by FEAT-FORGE-002's bootstrap
  code; do NOT open a new connection.
- Reply-channel lifecycle: subscribe on `bind()`, unsubscribe on
  `release()`. Do NOT introduce a long-lived "all replies" subscription
  with client-side fan-out — per-correlation subscriptions make the
  exactly-once and source-auth invariants clean. (If JetStream
  performance becomes a concern, a future task can introduce a single
  wildcard subscription with router-side fan-out — but that is not
  needed for this feature.)
- The `FakeNatsClient` in `tests/bdd/conftest.py` already records
  `(topic, payload)` pairs. Extend it (in TASK-SAD-011) with
  subscribe/unsubscribe recording so the subscribe-before-publish
  invariant is testable at the adapter boundary.

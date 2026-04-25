---
complexity: 7
consumer_context:
- consumes: CapabilityResolution
  driver: in-memory + persisted via TASK-SAD-002
  format_note: Reuses the model unchanged; this task instantiates it from resolve()
    output and threads it through the dispatch lifecycle.
  framework: Pydantic v2
  task: TASK-SAD-001
- consumes: CorrelationKey
  driver: CorrelationRegistry
  format_note: Opaque 32-hex-char string. Orchestrator obtains via registry.fresh_correlation_key()
    and threads into bind() and the publish payload header.
  framework: asyncio
  task: TASK-SAD-003
created: 2026-04-25 00:00:00+00:00
dependencies:
- TASK-SAD-001
- TASK-SAD-002
- TASK-SAD-003
- TASK-SAD-004
- TASK-SAD-005
feature_id: FEAT-FORGE-003
id: TASK-SAD-006
implementation_mode: task-work
parent_review: TASK-REV-SAD3
priority: high
status: design_approved
tags:
- dispatch
- orchestrator
- lifecycle
- write-before-send
- subscribe-before-publish
task_type: feature
test_results:
  coverage: null
  last_run: null
  status: pending
title: 'Dispatch orchestrator: resolve → bind → publish → wait → parse → outcome'
updated: 2026-04-25 00:00:00+00:00
wave: 3
---

# Task: Dispatch orchestrator — resolve → bind → publish → wait → parse → outcome

## Description

The orchestrator is the **single entry point** for one dispatch attempt. It
sequences the five pure-domain steps in the exact order the spec requires:

1. **Resolve** the capability against the current cache snapshot
   (`forge.discovery.resolve.resolve()` — already shipped in FEAT-FORGE-002).
2. **Persist** the resolution record (TASK-SAD-002) — this is the
   write-before-send invariant (D.write-before-send-invariant).
3. **Bind** a correlation-keyed reply subscription (TASK-SAD-003) — this is
   the subscribe-before-publish invariant (D.subscribe-before-publish-invariant
   and the LES1 lesson). Only after `bind()` returns is publish permitted.
4. **Publish** the dispatch command via the transport (TASK-SAD-010 will
   provide the transport).
5. **Wait** for the reply via the timeout coordinator (TASK-SAD-004), then
   **parse** it (TASK-SAD-005) into a `DispatchOutcome`.

Returns the `DispatchOutcome` to the reasoning loop / pipeline consumer.

Implements the `DispatchBuild` callable signature already declared by
`pipeline_consumer.py` in FEAT-FORGE-002 (the dispatch callback seam).

## Interface

```python
# src/forge/dispatch/orchestrator.py
from forge.discovery.cache import DiscoveryCache
from forge.discovery.resolve import resolve
from forge.dispatch.correlation import CorrelationRegistry
from forge.dispatch.timeout import TimeoutCoordinator
from forge.dispatch.persistence import persist_resolution, DispatchParameter
from forge.dispatch.reply_parser import parse_reply
from forge.dispatch.models import (
    DispatchAttempt, DispatchOutcome, Degraded, DispatchError
)


class DispatchOrchestrator:
    def __init__(
        self,
        cache: DiscoveryCache,
        registry: CorrelationRegistry,
        timeout: TimeoutCoordinator,
        publisher: "DispatchCommandPublisher",  # see TASK-SAD-010
        db_writer: "SqliteHistoryWriter",
    ) -> None: ...

    async def dispatch(
        self,
        *,
        capability: str,
        parameters: list[DispatchParameter],
        attempt_no: int = 1,
        retry_of: Optional[str] = None,
    ) -> DispatchOutcome:
        """Execute one dispatch attempt.

        Order is fixed and must not change:
          1. resolve against cache snapshot
          2. persist resolution (write-before-send)
          3. bind correlation (subscribe-before-publish)
          4. publish command
          5. wait for reply (with hard timeout)
          6. parse → outcome
        """
        snapshot = self.cache.snapshot()  # snapshot stability — see E.snapshot-stability
        matched_id, resolution = resolve(
            capability=capability,
            snapshot=snapshot,
            min_confidence=0.7,  # ASSUM-001
        )
        if matched_id is None:
            # Degraded path — no specialist resolvable
            return Degraded(
                resolution_id=resolution.resolution_id,
                attempt_no=attempt_no,
                reason="no_specialist_resolvable",
            )

        # Step 2: write-before-send
        resolution_with_retry = resolution.model_copy(update={"retry_of": retry_of})
        persist_resolution(resolution_with_retry, parameters, db_writer=self.db_writer)

        # Step 3: subscribe-before-publish
        correlation_key = self.registry.fresh_correlation_key()
        binding = await self.registry.bind(correlation_key, matched_id)

        # Step 4: publish
        attempt = DispatchAttempt(
            resolution_id=resolution.resolution_id,
            correlation_key=correlation_key,
            matched_agent_id=matched_id,
            attempt_no=attempt_no,
            retry_of=retry_of,
        )
        await self.publisher.publish_dispatch(attempt, parameters)

        # Step 5: wait + parse
        payload = await self.timeout.wait_with_timeout(binding)
        if payload is None:
            return DispatchError(
                resolution_id=resolution.resolution_id,
                attempt_no=attempt_no,
                error_explanation="local_timeout",
            )
        return parse_reply(
            payload,
            resolution_id=resolution.resolution_id,
            attempt_no=attempt_no,
        )
```

## Acceptance Criteria

- [ ] `src/forge/dispatch/orchestrator.py` defines `DispatchOrchestrator`
      with `dispatch()` returning a `DispatchOutcome`.
- [ ] Order of operations is exactly: resolve → persist → bind → publish →
      wait → parse. Reordering breaks invariants.
- [ ] Test (D.subscribe-before-publish-invariant): the publisher's
      `publish_dispatch` call is recorded by the test transport AFTER the
      registry's `bind` has returned. Use `FakeNatsClient`'s recording-order
      assertions.
- [ ] Test (D.write-before-send-invariant): when `publish_dispatch` is
      called, asserting the resolution row exists in the persistence layer.
- [ ] Test (E.snapshot-stability): a cache mutation triggered between
      `cache.snapshot()` and `resolve()` does NOT affect the resolution
      result. The snapshot is read once at the top of `dispatch()`.
- [ ] Test (degraded path): `resolve()` returns `(None, resolution)` →
      `dispatch()` returns `Degraded` and does NOT publish or bind.
- [ ] Test (timeout path): `timeout.wait_with_timeout` returns `None` →
      `dispatch()` returns `DispatchError` with `error_explanation="local_timeout"`.
- [ ] Test (E.concurrent-dispatches): two concurrent `dispatch()` calls
      use distinct correlation keys (verify by inspecting the registry).
- [ ] All modified files pass project-configured lint/format checks with
      zero errors.

## Seam Tests

```python
"""Seam tests: verify CapabilityResolution and CorrelationKey contracts."""
import pytest


@pytest.mark.seam
@pytest.mark.integration_contract("CapabilityResolution")
async def test_orchestrator_persists_resolution_before_publish(
    orchestrator, fake_publisher, db_writer
):
    """Write-before-send invariant — resolution row exists before publish."""
    await orchestrator.dispatch(capability="review", parameters=[])
    # Published call recorded the resolution_id; verify the row was written
    # at least one logical step before the publish call.
    assert db_writer.row_count() >= 1
    assert fake_publisher.published_after_persist is True


@pytest.mark.seam
@pytest.mark.integration_contract("CorrelationKey")
async def test_orchestrator_threads_correlation_key_into_publish(
    orchestrator, fake_publisher
):
    """Verify CorrelationKey contract from TASK-SAD-003.

    Contract: 32 lowercase hex chars, threaded into the publish header.
    Producer: TASK-SAD-003
    """
    await orchestrator.dispatch(capability="review", parameters=[])
    last_publish = fake_publisher.published[-1]
    correlation_key = last_publish.headers["correlation_key"]
    import re
    assert re.fullmatch(r"[0-9a-f]{32}", correlation_key)
```

## Implementation Notes

- The orchestrator MUST NOT touch the NATS transport directly — only
  through the `DispatchCommandPublisher` protocol that TASK-SAD-010 will
  implement. This keeps the orchestrator pure-domain.
- Do not catch and swallow exceptions inside `dispatch()`. A genuine error
  in `bind()` or `publish_dispatch()` should propagate; the
  `DispatchBuild` callback contract surfaces failures via the
  pipeline_consumer's ack callback (already shipped).
- The "concurrent dispatches" scenario relies on `secrets.token_hex(16)`
  in `fresh_correlation_key()` returning distinct values across calls.
  This is not a property to test in the orchestrator — it is tested in
  TASK-SAD-003. The orchestrator test only asserts non-equality.
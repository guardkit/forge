---
id: TASK-FRR-PEB-004
title: "Wire LifecycleBridge into forge serve startup + correlation-id threading"
status: backlog
created: 2026-05-06T00:00:00Z
updated: 2026-05-06T00:00:00Z
priority: high
task_type: feature
parent_task: TASK-FORGE-FRR-F010M
parent_review: TASK-REV-F010M
feature_id: FEAT-PEBR
wave: 2
implementation_mode: task-work
complexity: 6
estimated_minutes: 90
dependencies:
  - TASK-FRR-PEB-003
tags:
  - forge-serve
  - autobuild-runner
  - pipeline-lifecycle-emitter
  - bridge-wire-up
  - correlation-id-threading
consumer_context:
  - task: TASK-FRR-PEB-003
    consumes: STREAM_EVENT_SCHEMA
    framework: "forge.pipeline payloads (Pydantic v1) + forge.adapters.nats publisher"
    driver: "langgraph-sdk runs.join_stream → StreamEventTranslator (T3)"
    format_note: "Each translator output is a typed PipelineEvent (BuildStartedPayload | StageCompletePayload | BuildCompletePayload | BuildFailedPayload | BuildPausedPayload | BuildResumedPayload | BuildCancelledPayload) with correlation_id always populated. Bridge MUST publish via the existing forge.adapters.nats.publisher path; MUST NOT construct payloads directly."
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Task: Wire LifecycleBridge into forge serve startup + correlation-id threading

## TL;DR

Wire the `LifecycleBridge` (T2) into `forge serve` startup so it attaches
per-build on `pipeline.build-queued.*` arrival, observes the SSE stream
via T3's translator, and publishes `pipeline.*` envelopes with the
inbound `correlation_id` threaded through every emit. This is the
**consumer side** of the §4 Integration Contract for `STREAM_EVENT_SCHEMA`
produced by T3.

This task pairs with T3 — together they implement the @smoke headline
behaviour and lock the @smoke gates that fire after Wave 2.

## Locks BDD scenarios (primary)

- @smoke `An autobuild that runs to completion in the sidecar produces the
  full lifecycle envelope sequence on the wire` (with T3)
- @smoke @regression `An autobuild that fails asynchronously inside the
  sidecar produces build-failed on the wire` (with T3)
- @key-example @regression `Every envelope published for a sidecar
  autobuild threads the inbound correlation identifier` (with T3)
- @key-example `The supervisor remains responsive while the autobuild
  runs in the sidecar`

## Acceptance criteria

- AC-1: On `pipeline.build-queued.*` arrival, the consumer-bridge wiring
  invokes `LifecycleBridge.attach(build_context, ack_handle)` (T1
  contract), which writes to the SQLite registry (T2) and starts an
  asyncio task that observes the SSE stream via `StreamEventTranslator`
  (T3).
- AC-2: Each translated `PipelineEvent` is published via the existing
  `forge.adapters.nats.publisher` path. Bridge MUST NOT construct
  payloads directly (per §4 contract).
- AC-3: `correlation_id` from `BuildContext` is threaded onto every
  emitted envelope. F010C AST guard's fixture is extended with the new
  call sites in `lifecycle_bridge/wireup.py` (or wherever the publisher
  invocation lives).
- AC-4: On terminal envelope arrival (build-complete / build-failed /
  build-cancelled), the bridge invokes `BuildAckHandle.ack()` (T1) and
  removes the registry entry (T2).
- AC-5: The supervisor (existing forge serve REPL responder) remains
  responsive during in-flight builds — the SSE observer runs in its own
  asyncio task; supervisor queries are answered from the registry
  without blocking on the SSE stream.
- AC-6: `forge serve` shutdown calls `LifecycleBridge.shutdown()` which
  cancels all observer tasks, persists the latest `last_event_id` per
  build, and returns within 5 seconds.
- AC-7: All modified files pass project-configured lint/format checks
  with zero errors.

## Seam Tests

The following seam test validates the integration contract with the
producer task. Implement this test to verify the boundary before
integration.

```python
"""Seam test: verify STREAM_EVENT_SCHEMA contract from TASK-FRR-PEB-003."""
import pytest
from forge.lifecycle_bridge.translation import StreamEventTranslator
from forge.pipeline.payloads import (
    BuildStartedPayload,
    StageCompletePayload,
    BuildCompletePayload,
    BuildFailedPayload,
)


@pytest.mark.seam
@pytest.mark.integration_contract("STREAM_EVENT_SCHEMA")
def test_stream_event_schema_format(canonical_stream_part_fixture, build_context):
    """Verify STREAM_EVENT_SCHEMA matches the expected format.

    Contract: Each translator output is a typed PipelineEvent with
    correlation_id always populated.
    Producer: TASK-FRR-PEB-003
    """
    translator = StreamEventTranslator()

    # Producer side: get the artifact value
    event = translator.translate(canonical_stream_part_fixture, build_context)

    # Consumer side: verify format matches contract
    assert event is not None, "STREAM_EVENT_SCHEMA must not be None for canonical events"
    assert isinstance(
        event,
        (
            BuildStartedPayload,
            StageCompletePayload,
            BuildCompletePayload,
            BuildFailedPayload,
        ),
    ), f"Expected typed PipelineEvent, got: {type(event).__name__}"
    # Format assertion derived from §4 contract constraint:
    assert event.correlation_id, (
        f"correlation_id must be non-empty (§4 contract), "
        f"got: {event.correlation_id!r}"
    )
    assert event.correlation_id == build_context.correlation_id, (
        "correlation_id must match BuildContext (F010C contract)"
    )
```

## Test requirements

- Seam test per the block above (validates §4 STREAM_EVENT_SCHEMA
  contract at the boundary).
- Integration test: full lifecycle round-trip from `build-queued` arrival
  to `build-complete` publish, using a recorded SSE stream fixture
  (reuses T3's canonical fixture).
- Async failure round-trip test: `build-queued` → bridge attaches → SSE
  stream emits failure event → `build-failed` published with operator-
  readable failure reason.
- Supervisor-responsiveness test: query the registry while a stub SSE
  stream is suspended; supervisor returns within 100ms.
- Shutdown test: 3 in-flight builds; `LifecycleBridge.shutdown()`
  returns within 5s; `last_event_id` persisted for each build.

## Implementation notes

- Touchpoints: `src/forge/lifecycle_bridge/wireup.py` (new);
  `src/forge/cli/_serve_dispatcher.py` (consumer-bridge wiring);
  `src/forge/cli/_serve_deps.py` (DI plumbing).
- The bridge's SSE observer task is keyed on `feature_id`; the
  `attach()` call returns the asyncio task object so the supervisor's
  responsive-status surface can introspect without blocking.
- Coordinate with T3 author on the canonical SSE fixture — both tasks
  use the same recording.

## Coach validation commands

```bash
PYTHONPATH=src python -m pytest tests/forge/lifecycle_bridge/test_wireup.py -x -v
PYTHONPATH=src python -m pytest tests/forge/lifecycle_bridge/test_wireup_seam.py -x -v
PYTHONPATH=src python -m pytest tests/forge/test_pipeline_consumer_correlation_id.py -x -v
PYTHONPATH=src python -m pytest tests/bdd -m smoke -x -v
ruff check src/forge/lifecycle_bridge/wireup.py src/forge/cli/_serve_dispatcher.py
```

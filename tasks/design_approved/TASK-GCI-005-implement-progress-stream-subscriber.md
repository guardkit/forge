---
complexity: 5
created: 2026-04-25 00:00:00+00:00
dependencies:
- TASK-GCI-002
feature_id: FEAT-FORGE-005
id: TASK-GCI-005
implementation_mode: task-work
parent_review: TASK-REV-GCI0
priority: high
status: design_approved
tags:
- guardkit
- adapter
- nats
- progress
- telemetry
task_type: feature
test_results:
  coverage: null
  last_run: null
  status: pending
title: Implement NATS progress-stream subscriber (live telemetry)
updated: 2026-04-25 00:00:00+00:00
wave: 2
---

# Task: Implement NATS progress-stream subscriber (live telemetry)

## Description

Build the subscriber that listens on `pipeline.stage-complete.*` while a
GuardKit subprocess is running, decodes each message into a
`GuardKitProgressEvent`, and exposes the most-recent event for the live
status view.

This is **telemetry only** — the authoritative completion result still flows
through the synchronous `GuardKitResult` returned from
`forge.adapters.guardkit.run()`. A missing or unavailable progress stream
must not fail the invocation (Scenario "The authoritative result still
returns when progress streaming is unavailable").

Per `docs/design/contracts/API-subprocess.md` §3.2 (progress stream
integration) and `docs/design/contracts/API-nats-pipeline-events.md` §3.1
(subject family).

## Implementation

```python
# src/forge/adapters/guardkit/progress_subscriber.py
from contextlib import asynccontextmanager
from forge.adapters.guardkit.progress import GuardKitProgressEvent


class ProgressSink:
    """Holds the most recent N progress events per (build_id, subcommand).

    Used by `forge status` and the AsyncSubAgent live view. Bounded so a
    fast producer can't grow this unboundedly during a slow subscriber.
    """
    def latest(self, build_id: str, subcommand: str) -> GuardKitProgressEvent | None: ...
    def all_for(self, build_id: str, subcommand: str) -> list[GuardKitProgressEvent]: ...


@asynccontextmanager
async def subscribe_progress(
    nats_client,
    build_id: str,
    subcommand: str,
    sink: ProgressSink,
):
    """Subscribe to pipeline.stage-complete.{build_id}.{subcommand} for the
    lifetime of the context. On exit, unsubscribe. Errors are logged and
    swallowed — the subscriber must never propagate an exception that
    would fail the surrounding GuardKit call.
    """
```

## Acceptance Criteria

- [ ] `subscribe_progress` async context manager in
      `src/forge/adapters/guardkit/progress_subscriber.py`
- [ ] `ProgressSink` retains the most recent event per `(build_id,
      subcommand)` pair; old events are evicted under back-pressure
      (Scenario "Progress events emitted faster than Forge consumes them
      are still observable for live status")
- [ ] Unsubscribe runs on context-manager exit, including the exception
      path
- [ ] If the NATS client is `None` or unavailable, `subscribe_progress`
      yields a no-op subscription that records a single
      `progress_stream_unavailable` warning to the sink and the surrounding
      call still proceeds (Scenario "The authoritative result still returns
      when progress streaming is unavailable")
- [ ] Each subscription is scoped to one
      `pipeline.stage-complete.{build_id}.{subcommand}` subject — two
      parallel invocations within the same build receive independent event
      streams (Scenario "Parallel GuardKit invocations in the same build do
      not corrupt each other's results")
- [ ] Two concurrent builds against the same repo get isolated sinks (no
      shared state, ASSUM-007)
- [ ] Invalid payloads (malformed JSON, missing fields) are dropped with a
      structured warning, never raised
- [ ] Unit tests with a fake NATS client: ordered delivery, back-pressure
      eviction, malformed payload, unavailable client, parallel
      subscriptions
- [ ] All modified files pass project-configured lint/format checks with zero
      errors

## Implementation Notes

- nats-py async subscription pattern; reuse the project's existing
  nats-core client wrapper (do not instantiate a raw `NATS()` here)
- The bound on the sink (e.g. last 50 events per stream) is an
  implementation detail — pick a number that satisfies the Scenario
  ("most recent" is what matters, not absolute count)
- Consider `asyncio.Queue(maxsize=N)` with `put_nowait` + drop-on-full as
  the back-pressure strategy
- The async context manager exit must not block the surrounding `run()`
  call — `asyncio.shield()` the unsubscribe if needed
- Do **not** wire this into the subprocess wrapper here — TASK-GCI-008
  composes both. This task delivers the subscriber in isolation
---
id: TASK-FRR-PEB-011
title: "NATS publish-failure non-regression — SQLite state preserved, no spurious ack"
status: backlog
created: 2026-05-06T00:00:00Z
updated: 2026-05-06T00:00:00Z
priority: normal
task_type: feature
parent_task: TASK-FORGE-FRR-F010M
parent_review: TASK-REV-F010M
feature_id: FEAT-PEBR
wave: 4
implementation_mode: direct
complexity: 4
estimated_minutes: 45
dependencies:
  - TASK-FRR-PEB-004
tags:
  - forge-serve
  - autobuild-runner
  - pipeline-lifecycle-emitter
  - publish-failure
  - sqlite-source-of-truth
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Task: NATS publish-failure non-regression — SQLite state preserved, no spurious ack

## TL;DR

When the bridge's terminal-envelope publish to NATS fails (transient
broker error, network blip, etc.), the build's recorded state in SQLite
must remain at terminal, the failure must be logged at WARNING, and the
inbound `build-queued` ack must **not** be sent. This lets the consumer
redeliver and the bridge retry on next observation — no state corruption,
no silent loss.

ADR-ARCH-008 contract: SQLite is source-of-truth; transient JetStream
failures must not corrupt build state.

## Locks BDD scenarios

- @negative @regression `A NATS publish failure during the bridge's
  terminal envelope does not regress the recorded build state`
- @negative @regression `A build-failed envelope from an async sidecar
  failure carries an operator-readable failure reason`

## Acceptance criteria

- AC-1: The bridge's publish path wraps the `forge.adapters.nats.publisher`
  call in a try/except. On `NATSPublishError` (or whatever the existing
  publisher raises — verify during implementation), the failure is
  logged at WARNING with payload subject and correlation-id.
- AC-2: SQLite state is **not** updated to "terminal-published" on
  publish failure — the registry row's `terminal_published` column
  (T5) remains `false` so the next recovery cycle (T9) can retry.
- AC-3: The inbound `build-queued` ack handle is **not** invoked on
  publish failure — the consumer redelivers, the bridge re-attaches,
  and observation resumes.
- AC-4: Async-failure envelopes (from T3's translator) carry an
  operator-readable `failure_reason` of the form
  `{ExceptionClass}: {message}` (e.g.
  `RuntimeError: model output failed Pydantic validation`).
- AC-5: All modified files pass project-configured lint/format checks
  with zero errors.

## Test requirements

- Publish-failure non-regression test: stub publisher raises; assert
  WARNING log; assert SQLite row's `terminal_published == false`;
  assert ack NOT invoked.
- Operator-readable failure-reason test: stub SSE emits an exception
  event with `RuntimeError("model output failed Pydantic validation")`;
  assert published `BuildFailedPayload.failure_reason` matches
  `RuntimeError: model output failed Pydantic validation`.

## Implementation notes

- Touchpoints: `src/forge/lifecycle_bridge/wireup.py` (the publish
  call site); `src/forge/lifecycle_bridge/translation.py` (failure
  reason formatting in the failed-event branch).
- This task is `direct` mode — implementation is small enough to ship
  as a single PR without a design phase.

## Coach validation commands

```bash
PYTHONPATH=src python -m pytest tests/forge/lifecycle_bridge/test_publish_failure.py -x -v
PYTHONPATH=src python -m pytest tests/forge/lifecycle_bridge/test_translation.py -x -v -k failure_reason
ruff check src/forge/lifecycle_bridge/wireup.py src/forge/lifecycle_bridge/translation.py
```

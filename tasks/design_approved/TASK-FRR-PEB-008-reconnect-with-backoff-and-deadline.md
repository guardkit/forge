---
complexity: 6
created: 2026-05-06 00:00:00+00:00
dependencies:
- TASK-FRR-PEB-007
documentation_level: standard
estimated_minutes: 90
feature_id: FEAT-PEBR
id: TASK-FRR-PEB-008
implementation_mode: task-work
parent_review: TASK-REV-F010M
parent_task: TASK-FORGE-FRR-F010M
priority: high
status: design_approved
tags:
- forge-serve
- autobuild-runner
- pipeline-lifecycle-emitter
- reconnect-backoff
- sla-deadline
task_type: feature
test_results:
  coverage: null
  last_run: null
  status: pending
title: Reconnect with exponential backoff + 300s per-build deadline timer
updated: 2026-05-06 00:00:00+00:00
wave: 4
---

# Task: Reconnect with exponential backoff + 300s per-build deadline timer

## TL;DR

Implement the SSE bridge's reconnect loop with the verified ASSUM-003
constants: initial backoff 1.0s, cap 30.0s, exponential ×2, reset on
success, **no fixed maximum retry count** (terminate only on
`CancelledError`). Add a per-build SLA deadline timer that publishes
`pipeline.build-failed` with reason `sidecar-unreachable` if the bridge
goes 300s without observing a terminal envelope.

The deadline + reconnect combination is what surfaces "sidecar
unreachable" as a build-failed event to the operator, while keeping the
chat REPL responsive (transient disconnects don't spuriously fail
in-flight builds).

## Locks BDD scenarios

- @negative @edge-case `A transient sidecar disconnection mid-build
  does not produce a spurious build-failed envelope`
- @negative @edge-case `The lifecycle bridge declares a build failed
  if the sidecar remains unreachable beyond the reconnect schedule`
  (ASSUM-003)
- @edge-case `A malformed run-state response from the sidecar is
  logged and the bridge reconnects rather than crashing the daemon`

## Acceptance criteria

- AC-1: A new `src/forge/lifecycle_bridge/reconnect.py` exposes a
  `ReconnectPolicy` class with constants
  `RECONNECT_INITIAL_BACKOFF: float = 1.0` and
  `RECONNECT_MAX_BACKOFF: float = 30.0`. Backoff doubles on each
  attempt, caps at MAX, resets to INITIAL on successful reconnection.
- AC-2: The bridge's SSE observer task wraps its connection loop in
  `ReconnectPolicy` — on `httpx.ConnectError` / `httpx.ReadError` /
  malformed JSON, it sleeps the current backoff and reconnects with
  the persisted `Last-Event-ID`. No fixed maximum retry count.
- AC-3: A new per-build deadline timer is started by `LifecycleBridge.attach()`
  with a 300s budget. If no terminal envelope is observed within the
  budget, the bridge publishes `pipeline.build-failed` with payload
  `{"reason": "sidecar-unreachable: no terminal observed within 300s",
  "exception_class": "BridgeDeadlineExceeded"}`, marks
  `lifecycle_bridge_registry.terminal_published = true`, invokes the
  ack handle, and removes the registry entry.
- AC-4: Malformed SSE responses are logged at WARNING with the parse
  failure, and the bridge reconnects rather than crashing. The
  reconnect counts as an attempt for backoff purposes.
- AC-5: Tests monkey-patch `RECONNECT_INITIAL_BACKOFF` and
  `RECONNECT_MAX_BACKOFF` to 0.05s for fast runs (precedent:
  `tests/forge/test_cli_serve_daemon.py:364-367`). The deadline is
  monkey-patchable to e.g. 1s.
- AC-6: Build-failed envelopes from the deadline path carry the
  inbound correlation-id.
- AC-7: All modified files pass project-configured lint/format checks
  with zero errors.

## Test requirements

- Transient disconnect → no spurious build-failed: stub SSE source
  raises `ConnectError` once, then succeeds; assert no envelope
  published; backoff was applied.
- Permanent unreachable → build-failed: stub SSE source raises forever;
  monkey-patch deadline to 1s; assert exactly one `build-failed`
  envelope with `sidecar-unreachable` reason after 1s.
- Malformed response → reconnect, no daemon crash: stub SSE returns
  malformed JSON; assert WARNING log; assert reconnect happens; daemon
  remains running.
- Backoff doubling test: assert sequence 1.0s → 2.0s → 4.0s → ... → 30.0s
  → 30.0s (cap) on consecutive failures.
- Backoff reset test: succeed after 3 failures; next failure starts at
  1.0s (not 8.0s).

## Files to Create

- `src/forge/lifecycle_bridge/reconnect.py`
- `tests/forge/lifecycle_bridge/test_reconnect.py`
- `tests/forge/lifecycle_bridge/test_deadline.py`

## Files to Modify

- `src/forge/lifecycle_bridge/wireup.py`
- `src/forge/lifecycle_bridge/bridge.py`

## Implementation notes

- Touchpoints: `src/forge/lifecycle_bridge/reconnect.py` (new);
  `src/forge/lifecycle_bridge/wireup.py` (use `ReconnectPolicy` in the
  SSE observer task);
  `src/forge/lifecycle_bridge/bridge.py` (deadline timer in `attach()`).
- Reference: `src/forge/cli/_serve_daemon.py:90-93,447,468` for the
  established forge backoff pattern. Reuse the constants verbatim.
- The 300s deadline is the review's concrete commitment; do not
  re-debate downstream. Monkey-patch in tests to keep them fast.

## Coach validation commands

```bash
PYTHONPATH=src python -m pytest tests/forge/lifecycle_bridge/test_reconnect.py -x -v
PYTHONPATH=src python -m pytest tests/forge/lifecycle_bridge/test_deadline.py -x -v
PYTHONPATH=src python -m pytest tests/forge/test_pipeline_consumer_correlation_id.py -x -v
ruff check src/forge/lifecycle_bridge/reconnect.py src/forge/lifecycle_bridge/bridge.py
```
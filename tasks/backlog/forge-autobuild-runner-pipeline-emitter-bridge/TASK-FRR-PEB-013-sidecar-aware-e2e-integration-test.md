---
id: TASK-FRR-PEB-013
title: "Sidecar-aware E2E integration test (separate from FW10-011)"
status: backlog
created: 2026-05-06T00:00:00Z
updated: 2026-05-06T00:00:00Z
priority: high
task_type: testing
parent_task: TASK-FORGE-FRR-F010M
parent_review: TASK-REV-F010M
feature_id: FEAT-PEBR
wave: 5
implementation_mode: task-work
complexity: 7
estimated_minutes: 120
dependencies:
  - TASK-FRR-PEB-009
  - TASK-FRR-PEB-010
tags:
  - forge-serve
  - autobuild-runner
  - pipeline-lifecycle-emitter
  - sidecar-aware-e2e
  - regression-lock
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Task: Sidecar-aware E2E integration test (separate from FW10-011)

## TL;DR

Ship a separate sidecar-aware E2E integration test that spins up a real
`langgraph-runner` sidecar, starts `forge serve` against it, delivers a
`pipeline.build-queued` envelope through the real wiring, and asserts the
canonical lifecycle sequence (`build-started` → `stage-complete*` →
terminal) appears on the real wire. Deterministic across re-runs.

**FW10-011 remains unchanged** as the in-process composition lock. This
test is the sidecar-aware regression lock — it catches translation-layer
regressions (the dominant Option C risk) and SDK version skew that
unit/contract tests cannot.

ASSUM-008 / Q8 sub-option (a) commitment.

## Locks BDD scenarios

- @edge-case @regression `The sidecar-aware integration test asserts
  the canonical lifecycle sequence against a real sidecar spin-up`
  (ASSUM-008)

## Acceptance criteria

- AC-1: A new test file `tests/integration/test_lifecycle_bridge_sidecar_e2e.py`
  contains the sidecar-aware E2E test. Marker: `@pytest.mark.integration`
  + `@pytest.mark.slow` so CI can run it on a separate stage.
- AC-2: A pytest fixture spins up a real `langgraph-runner` sidecar
  using `subprocess.Popen` (or the existing forge fixture if one
  exists — verify under `tests/integration/conftest.py`). The fixture
  yields the sidecar URL and tears down the process on test exit.
- AC-3: The test:
  1. Starts `forge serve` against the real sidecar.
  2. Publishes a `pipeline.build-queued.*` envelope onto JetStream.
  3. Subscribes to `pipeline.>` and collects envelopes for up to 60s
     or until terminal arrives.
  4. Asserts the collected sequence matches the canonical pattern:
     1× `build-started` → ≥1× `stage-complete` → 1× terminal
     (`build-complete` for the success case, `build-failed` for the
     forced-failure case).
  5. Asserts every envelope carries the inbound `correlation_id`.
- AC-4: Test runs at least twice (parametrized: success path + forced
  failure path) and produces deterministic output across re-runs (no
  flaky timing assertions).
- AC-5: FW10-011 test file is **not modified** — confirm by running
  it pre- and post-implementation.
- AC-6: All modified files pass project-configured lint/format checks
  with zero errors.

## Test requirements

This task **is** a test — its acceptance criteria are the test it ships.

- The test itself must produce deterministic output across 5 consecutive
  runs (run as a CI loop or local flake-check).
- Test must complete within the 60s budget on the canonical CI runner.
- Failure path is forced via a stub feature definition that triggers a
  `RuntimeError` mid-stage; assert `build-failed` envelope arrives with
  operator-readable failure reason.

## Implementation notes

- Touchpoints: `tests/integration/test_lifecycle_bridge_sidecar_e2e.py`
  (new); `tests/integration/conftest.py` (sidecar fixture if not
  existing); `pyproject.toml` (add `slow` marker if missing).
- Reference: existing `tests/bdd/test_nats_fleet_integration.py` for
  the JetStream subscribe-and-collect pattern;
  `tests/forge/test_cli_serve_daemon.py` for the daemon-startup
  fixture pattern.
- This is `testing` task_type; CoachValidator skips arch review for
  testing tasks.
- `feature` skip-list note: this task has no production code; the
  acceptance criteria are about the test's behaviour and determinism.

## Coach validation commands

```bash
PYTHONPATH=src python -m pytest tests/integration/test_lifecycle_bridge_sidecar_e2e.py -x -v -m "integration and slow"
# Run 5x for determinism check:
for i in 1 2 3 4 5; do PYTHONPATH=src python -m pytest tests/integration/test_lifecycle_bridge_sidecar_e2e.py -x || break; done
ruff check tests/integration/test_lifecycle_bridge_sidecar_e2e.py
```

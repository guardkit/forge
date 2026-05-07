---
id: TASK-FRR-PEB-001
title: Defer the inbound build-queued ack from dispatch return to terminal arrival
status: blocked
created: 2026-05-06 00:00:00+00:00
updated: 2026-05-06 00:00:00+00:00
priority: high
task_type: refactor
parent_task: TASK-FORGE-FRR-F010M
parent_review: TASK-REV-F010M
feature_id: FEAT-PEBR
wave: 1
implementation_mode: task-work
complexity: 5
estimated_minutes: 60
dependencies: []
tags:
- forge-serve
- autobuild-runner
- pipeline-lifecycle-emitter
- consumer-contract-refactor
- ack-deferral
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 3
  max_turns: 5
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-PEBR
  base_branch: main
  started_at: '2026-05-06T20:34:36.691159'
  last_updated: '2026-05-06T21:00:09.285200'
  turns:
  - turn: 1
    decision: feedback
    feedback: "- Advisory (non-blocking): task-work produced a report with 2 of 3\
      \ expected agent invocations. Missing phases: 3 (Implementation). Consider invoking\
      \ these agents via the Task tool to strengthen stack-specific quality:\n- Phase\
      \ 3: `python-api-specialist` (Implementation)\n- Plan audit detected high-severity\
      \ discrepancies \u2014 1 missing file(s): pipeline_consumer.py"
    timestamp: '2026-05-06T20:34:36.691159'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
  - turn: 2
    decision: feedback
    feedback: "- Advisory (non-blocking): task-work produced a report with 2 of 3\
      \ expected agent invocations. Missing phases: 3 (Implementation). Consider invoking\
      \ these agents via the Task tool to strengthen stack-specific quality:\n- Phase\
      \ 3: `python-api-specialist` (Implementation)\n- Plan audit detected high-severity\
      \ discrepancies \u2014 1 missing file(s): pipeline_consumer.py"
    timestamp: '2026-05-06T20:45:58.547066'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
  - turn: 3
    decision: feedback
    feedback: "- Advisory (non-blocking): task-work produced a report with 2 of 3\
      \ expected agent invocations. Missing phases: 3 (Implementation). Consider invoking\
      \ these agents via the Task tool to strengthen stack-specific quality:\n- Phase\
      \ 3: `python-api-specialist` (Implementation)\n- Plan audit detected high-severity\
      \ discrepancies \u2014 1 missing file(s): pipeline_consumer.py"
    timestamp: '2026-05-06T20:52:28.677058'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# Task: Defer the inbound build-queued ack from dispatch return to terminal arrival

## TL;DR

Refactor the consumer-contract path in `src/forge/adapters/nats/pipeline_consumer.py`
so the inbound `pipeline.build-queued.*` envelope is acked at autobuild
**terminal arrival** (success / failure / paused-then-resumed-and-terminal /
cancelled) rather than at `dispatch_build` return. Closes the redelivery
storm captured in RESULTS Addendum 5 (correlation_id
`e9433033-ea80-449f-885d-b2d1bdfb839e`) and gives the lifecycle bridge a
single ack callback to invoke when it observes terminal.

This is **Wave 1 foundation** for the F010M wave-plan: T2 builds on top of
the new ack callback contract; T3/T4 invoke it when they observe terminal
via the SSE stream.

## Locks BDD scenarios

- `The inbound build-queued envelope is acked when the autobuild reaches a
  terminal state, not when the dispatch chain returns` (ASSUM-004 / Q3
  sub-option (b))
- `Duplicate dispatch attempts for the same in-flight build do not produce
  duplicate envelopes` (boundary regression; deferred ack must coexist
  with duplicate-detection)

## Acceptance criteria

- AC-1: `pipeline_consumer.py`'s dispatch path no longer calls `msg.ack()`
  on `dispatch_build` return; instead it stores the ack callback in the
  in-flight registry keyed by `(feature_id, correlation_id)`.
- AC-2: A new `BuildAckHandle` interface exposes `ack()` and `nak()`
  methods; the lifecycle bridge (T2) consumes this interface — no
  back-references to `MessageEnvelope` outside the consumer module.
- AC-3: When no bridge is wired (e.g. unit-test path), the consumer falls
  back to the existing F010F sync-raise behaviour: ack on dispatch return
  for non-raising calls, nak on raising calls. This preserves test
  determinism for code paths that don't exercise the bridge.
- AC-4: Duplicate-detection from the existing consumer is unchanged —
  duplicate `build-queued` envelopes for an in-flight build are acked
  immediately and skipped (no second registration).
- AC-5: F010C correlation-id AST guard remains green — every emit site
  the consumer touches still passes `correlation_id=` explicitly.
- AC-6: All modified files pass project-configured lint/format checks
  with zero errors.

## Test requirements

- Unit test asserting ack is **not** sent during dispatch return when a
  bridge is registered.
- Unit test asserting ack **is** sent when the registered bridge invokes
  `BuildAckHandle.ack()`.
- Unit test asserting backward-compatibility: when no bridge is
  registered, the consumer's behaviour matches F010F (ack on success,
  nak on raise).
- Regression test for duplicate-detection: second `build-queued` for the
  same `(feature_id, correlation_id)` is acked and skipped.

## Implementation notes

- Touchpoints: `src/forge/adapters/nats/pipeline_consumer.py` (primary);
  `src/forge/cli/_serve_deps.py` (registration plumbing); new
  `src/forge/pipeline/build_ack_handle.py` (interface).
- Existing redelivery storm: at-most-once dispatch is wedged behind the
  premature ack — the consumer acks on dispatch return so a long-running
  autobuild can never report failure on the wire. This refactor unblocks
  T2/T3/T4.
- Coordinate with T2 author: the `BuildAckHandle` interface is the
  contract between this task and the bridge skeleton.

## Coach validation commands

```bash
PYTHONPATH=src python -m pytest tests/forge/adapters/nats/test_pipeline_consumer.py -x -v
PYTHONPATH=src python -m pytest tests/forge/test_pipeline_consumer_correlation_id.py -x -v
ruff check src/forge/adapters/nats/pipeline_consumer.py src/forge/pipeline/
```

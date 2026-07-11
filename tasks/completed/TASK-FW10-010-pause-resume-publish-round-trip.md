---
complexity: 3
consumer_context:
- consumes: PipelineLifecycleEmitter
  driver: in-process Python object
  format_note: emit_build_paused fires from _update_state at lifecycle='awaiting_approval';
    emit_build_resumed fires from forge.adapters.nats.approval_subscriber's resume
    path. Both are single-line additions per DDR-007 §Decision.
  framework: approval_subscriber resume path + autobuild_runner _update_state
  task: TASK-FW10-006
dependencies:
- TASK-FW10-007
- TASK-FW10-008
estimated_minutes: 60
feature_id: FEAT-FORGE-010
id: TASK-FW10-010
implementation_mode: task-work
parent_review: TASK-REV-FW10
priority: high
status: completed
tags:
- pause-resume
- approval
- lifecycle-emitter
- ddr-007
task_type: feature
title: Pause/resume publish round-trip via emit_build_paused and emit_build_resumed
wave: 4
reconciled: "2026-07-11 WS3-S8 sweep — FW10 in completed/; moved by 9ef9138b (FEAT-DEA8 finalize)"
---

# TASK-FW10-010 — Pause/resume publish round-trip

## Why

DDR-007 §Decision keeps pause/resume publish in scope: with the emitter
threaded through the dispatcher context (DDR-007 Option A),
`emit_build_paused` is one call at the
`lifecycle="awaiting_approval"` boundary inside the subagent's
`_update_state`, and `emit_build_resumed` is one call in the existing
`forge.adapters.nats.approval_subscriber` resume path. This task
adds those two call sites and the round-trip integration test.

If implementation discovers a structural blocker (e.g., the approval
subscriber needs reshaping rather than a one-line addition), file a
carve-out and split the resume publish into a follow-up — but per
Context B, the default position is "in scope".

## Files to modify

- `src/forge/subagents/autobuild_runner.py` (MODIFY — light):
  - In `_update_state`, when the new lifecycle is
    `"awaiting_approval"`, the existing
    `emitter.on_transition(new_state)` call (added in TASK-FW10-002)
    should already invoke `emit_build_paused` via the dispatch matrix
    on the emitter (added in TASK-FW10-006). Confirm via test; add
    explicit `emit_build_paused` call if `on_transition`'s dispatch
    doesn't cover it.
- `src/forge/adapters/nats/approval_subscriber.py` (MODIFY):
  - In the resume path (where the approval response is matched and
    the build is unpaused), call
    `emitter.emit_build_resumed(feature_id, correlation_id, ...)`.
  - Wrap in try/except — publish failure must not regress the resume
    transition (ADR-ARCH-008).
- `tests/forge/test_pause_resume_publish.py` (NEW):
  - Pause path: `_update_state(lifecycle="awaiting_approval")` →
    `pipeline.build-paused.<feature_id>` is published with the build's
    correlation_id (Group D scenario).
  - Mismatched-correlation approval response is dropped; no
    `build-resumed` is published; the build stays paused (Group E
    security scenario, DDR-001).
  - Resume path: matching approval response → `pipeline.build-resumed`
    is published; the orchestrator advances to the next stage
    (Group D scenario).
  - Idempotent first-wins: a second matching approval response after
    resume is a no-op (FEAT-FORGE-004 contract; not a new behaviour).
- `tests/integration/test_pause_resume_e2e.py` (NEW or extend):
  - Pause → daemon restart → re-emit `build-paused` + approval
    request → resume on first approval; only the first approval
    matters (Group D scenario "paused build survives daemon restart").

## Acceptance criteria

- [ ] When the autobuild_runner's `_update_state` writes
      `lifecycle="awaiting_approval"`, a `pipeline.build-paused.<feature_id>`
      envelope is published with the build's correlation_id.
- [ ] When the approval subscriber matches an approval response with
      the build's correlation_id, a `pipeline.build-resumed.<feature_id>`
      envelope is published before the orchestrator advances.
- [ ] An approval response with a mismatched correlation_id does NOT
      publish `build-resumed`; the build stays paused (Group E
      security scenario; DDR-001 / ASSUM-016).
- [ ] After a daemon restart while a build is paused, the build
      re-enters paused state, re-emits `build-paused`, and re-publishes
      the approval request with the same correlation_id (Group D
      scenario; ADR-ARCH-021).
- [ ] Only the first approval response wins (idempotent first-wins
      from FEAT-FORGE-004; not new behaviour but exercised by the test).
- [ ] Publish failures on `emit_build_paused` / `emit_build_resumed`
      log at WARNING and do not regress SQLite state.
- [ ] All modified files pass project-configured lint/format checks
      with zero errors.

## Implementation notes

- DDR-007 §Decision says these are one-line additions at each call
  site. Confirm during implementation. If either site requires more
  than a one-line addition, raise a blocker and discuss carve-out
  with the user (Context B's default is "keep in scope").
- The approval subscriber already exists at
  `src/forge/adapters/nats/approval_subscriber.py`. Read its current
  shape before editing; this task should not reshape its public API.
- The emitter's `on_transition` dispatch matrix from TASK-FW10-006
  may already route `awaiting_approval` → `emit_build_paused`. If so,
  this task just adds the test asserting the path. If not, add the
  matrix entry.

## Coach validation

- `pytest tests/forge/test_pause_resume_publish.py -x`.
- `pytest tests/integration/test_pause_resume_e2e.py -x`.
- Lint: project-configured ruff/format.

## References

- [DDR-007 §Decision (pause/resume in scope)](../../../docs/design/decisions/DDR-007-pipeline-lifecycle-emitter-wiring-path.md)
- [ADR-ARCH-021](../../../docs/architecture/decisions/ADR-ARCH-021-paused-via-langgraph-interrupt.md)
- [DDR-001](../../../docs/design/decisions/DDR-001-reply-subject-correlation.md) (correlation-keyed reply subjects)
- [`src/forge/adapters/nats/approval_subscriber.py`](../../../src/forge/adapters/nats/approval_subscriber.py)
- IMPLEMENTATION-GUIDE.md §3 (sequence) and §4 (`PipelineLifecycleEmitter` contract)
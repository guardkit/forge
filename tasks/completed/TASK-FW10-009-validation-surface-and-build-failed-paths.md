---
complexity: 4
dependencies:
- TASK-FW10-007
estimated_minutes: 60
feature_id: FEAT-FORGE-010
id: TASK-FW10-009
implementation_mode: task-work
parent_review: TASK-REV-FW10
priority: high
status: completed
tags:
- validation
- build-failed
- security
- allowlist
task_type: feature
title: Validation surface (malformed payload, duplicate, allowlist) emits build-failed
  and acks
wave: 4
reconciled: "2026-07-11 WS3-S8 sweep — FW10 in completed/; moved by 9ef9138b (FEAT-DEA8 finalize)"
---

# TASK-FW10-009 — Validation surface emits `build-failed` and acks

## Why

Three negative-path scenarios in Group C require the daemon to publish
`build-failed` and ack the inbound message rather than dispatching:
malformed payload (ASSUM-013), duplicate `(feature_id, correlation_id)`
(ASSUM-014), and worktree-allowlist failure (ASSUM-015). The
`pipeline_consumer.handle_message` machinery already encodes the
contract for these paths; this task verifies the wiring put in place
by Wave 3 actually invokes them.

## Files to modify (mostly tests; minimal code change expected)

- `src/forge/adapters/nats/pipeline_consumer.py` (MODIFY if the
  validation paths exist but don't currently publish + ack):
  - On a malformed payload, publish `build-failed` with a
    malformed-payload reason and ack (per ASSUM-013).
  - On a duplicate `(feature_id, correlation_id)` (looked up via
    `is_duplicate_terminal`), ack and skip — do **not** publish a new
    `build-started` (per ASSUM-014).
  - On a worktree-allowlist failure (raised by the
    `ForwardContextBuilder` from TASK-FW10-003), publish `build-failed`
    with a path-outside-allowlist reason **before** any orchestrator
    dispatch (per ASSUM-015).
  - Confirm via test that publish failure on these paths does not
    regress the recorded transition (Group C "publish failure does
    not regress" — ADR-ARCH-008).
- `tests/forge/test_pipeline_consumer_validation.py` (NEW):
  - One test per validation path:
    1. Malformed payload → `build-failed` published, msg acked, no
       dispatch.
    2. Duplicate envelope → no `build-started` published, msg acked,
       no dispatch.
    3. Allowlist failure → `build-failed` published, no dispatch.
  - Publish-failure resilience: `build-failed` publish raises →
    SQLite state is unchanged; the daemon stays running.

## Acceptance criteria

- [ ] Malformed payload publishes `build-failed` and acks the inbound
      message (Group C scenario "malformed payload").
- [ ] Duplicate `(feature_id, correlation_id)` is acked and skipped
      with no second `build-started` (Group C scenario "duplicate").
- [ ] Worktree-allowlist failure publishes `build-failed` with a
      path-outside-allowlist reason **before** any orchestrator
      dispatch (Group C scenario "allowlist").
- [ ] Publish failures on any of these paths log at WARNING and do not
      regress the build's recorded transition (Group C scenario
      "publish failure does not regress").
- [ ] Dispatch errors during a build are contained: the affected build
      reaches `failed`, the daemon stays running, the next delivered
      build is processed (Group C scenario "dispatch error contained").
- [ ] All modified files pass project-configured lint/format checks
      with zero errors.

## Implementation notes

- `pipeline_consumer.handle_message` already documents these paths.
  Read the existing implementation before adding code; this task may
  end up being mostly tests + minor adjustments.
- The duplicate-detection SQLite unique index already exists per
  ASSUM-014; do not modify schema.
- `is_duplicate_terminal` is wired by TASK-FW10-007's deps factory.

## Coach validation

- `pytest tests/forge/test_pipeline_consumer_validation.py -x`.
- `pytest tests/cli tests/forge -x -k 'serve or pipeline_consumer'`.
- Lint: project-configured ruff/format.

## References

- [API-nats-pipeline-events.md §2.3](../../../docs/design/contracts/API-nats-pipeline-events.md) (validation rules)
- [ADR-ARCH-008](../../../docs/architecture/decisions/ADR-ARCH-008-forge-produces-own-history.md) (publish-failure-does-not-regress)
- [`src/forge/adapters/nats/pipeline_consumer.py`](../../../src/forge/adapters/nats/pipeline_consumer.py)
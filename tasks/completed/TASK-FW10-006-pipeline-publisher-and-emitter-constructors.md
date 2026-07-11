---
complexity: 3
conductor_workspace: wave2-publisher-emitter
dependencies:
- TASK-FW10-001
estimated_minutes: 45
feature_id: FEAT-FORGE-010
id: TASK-FW10-006
implementation_mode: task-work
parent_review: TASK-REV-FW10
priority: high
status: completed
tags:
- factory
- lifecycle-emitter
- pipeline-publisher
- nats
task_type: feature
title: PipelinePublisher and PipelineLifecycleEmitter production constructors
wave: 2
reconciled: "2026-07-11 WS3-S8 sweep — FW10 in completed/; moved by 9ef9138b (FEAT-DEA8 finalize)"
---

# TASK-FW10-006 — `PipelinePublisher` and `PipelineLifecycleEmitter` production constructors

## Why

`PipelinePublisher` exists in
`src/forge/adapters/nats/pipeline_publisher.py` and `PipelineLifecycleEmitter`
exists in `src/forge/pipeline/__init__.py`; both are well-unit-tested
but neither is constructed in production. This task adds the small
factory that produces both, sharing the daemon's single NATS client
(ASSUM-011). DDR-007 binds the emitter call to `_update_state`; that
emitter call comes from the instance produced here.

## Files to create / modify

- `src/forge/cli/_serve_deps_lifecycle.py` (NEW):
  - `def build_publisher_and_emitter(client) -> tuple[PipelinePublisher, PipelineLifecycleEmitter]`
  - Constructs both against the same client; returns them as a tuple.
  - The emitter's `on_transition(new_state)` dispatches to the eight
    `emit_*` methods based on `state.lifecycle`. (If `on_transition`
    already exists on the class, this task verifies it; if not, this
    task adds it.)
- `tests/forge/test_lifecycle_factory.py` (NEW):
  - Unit test: factory returns both objects bound to the supplied
    client; no second `nats.connect` call is made.
  - Dispatch matrix: one assertion per `lifecycle` literal —
    `on_transition` calls the matching `emit_*` method.

## Acceptance criteria

- [ ] `build_publisher_and_emitter(client)` returns a `(PipelinePublisher,
      PipelineLifecycleEmitter)` tuple bound to the supplied client.
- [ ] No second NATS connection is opened anywhere in this factory.
- [ ] `emitter.on_transition(new_state)` exists and dispatches to the
      correct `emit_*` method for every `lifecycle` literal in DDR-006.
- [ ] Publish failure on any `emit_*` does **not** raise to callers —
      it logs at WARNING and returns (DDR-007 §Failure-mode contract,
      ADR-ARCH-008).
- [ ] All modified files pass project-configured lint/format checks
      with zero errors.

## Implementation notes

- The `PipelinePublisher` class itself is not modified. Just construct
  it.
- If `on_transition` does not yet exist on `PipelineLifecycleEmitter`,
  add it as a thin dispatch matrix in
  `src/forge/pipeline/__init__.py` (or wherever the emitter lives).
  Keep the eight `emit_*` methods unchanged.
- The publisher and emitter are passed downstream to TASK-FW10-007
  (deps factory) and TASK-FW10-002 (autobuild_runner via context
  payload). This task's only job is to construct them once.

## Coach validation

- `pytest tests/forge/test_lifecycle_factory.py -x`.
- `pytest tests/forge -x` (smoke gate 2).
- Lint: project-configured ruff/format.

## References

- [DDR-007](../../../docs/design/decisions/DDR-007-pipeline-lifecycle-emitter-wiring-path.md)
- [`src/forge/adapters/nats/pipeline_publisher.py`](../../../src/forge/adapters/nats/pipeline_publisher.py)
- [`src/forge/pipeline/__init__.py`](../../../src/forge/pipeline/__init__.py) (`PipelineLifecycleEmitter`)
- [API-nats-pipeline-events.md §3](../../../docs/design/contracts/API-nats-pipeline-events.md) (the eight subjects)
- IMPLEMENTATION-GUIDE.md §4 contracts: `PipelinePublisher`, `PipelineLifecycleEmitter`
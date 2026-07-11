---
complexity: 5
consumer_context:
- consumes: AUTOBUILD_RUNNER_NAME
  driver: langgraph.json graph entry
  format_note: Supervisor's AutobuildDispatcher uses AUTOBUILD_RUNNER_NAME = 'autobuild_runner'
    to address the registered graph; the constant must match the langgraph.json entry
    created in TASK-FW10-002.
  framework: DeepAgents AsyncSubAgent registration
  task: TASK-FW10-002
- consumes: PipelineLifecycleEmitter
  driver: in-process Python object via DeepAgents AsyncSubAgent context
  format_note: Emitter is threaded through dispatch_autobuild_async's context payload;
    the autobuild_runner subagent receives it as ctx['lifecycle_emitter'] and calls
    emitter.on_transition(new_state) from _update_state.
  framework: dispatch_autobuild_async context payload (DDR-007 Option A)
  task: TASK-FW10-006
dependencies:
- TASK-FW10-002
- TASK-FW10-006
estimated_minutes: 75
feature_id: FEAT-FORGE-010
id: TASK-FW10-008
implementation_mode: task-work
parent_review: TASK-REV-FW10
priority: high
status: completed
tags:
- composition
- supervisor
- async-subagent-middleware
- deepagents
task_type: feature
title: Wire AsyncSubAgentMiddleware into supervisor; thread emitter into autobuild
  dispatcher context
wave: 3
reconciled: "2026-07-11 WS3-S8 sweep — FW10 in completed/; moved by 9ef9138b (FEAT-DEA8 finalize)"
---

# TASK-FW10-008 — Wire `AsyncSubAgentMiddleware` into the supervisor; thread the emitter through the dispatcher context

## Why

The supervisor's reasoning loop needs the DeepAgents
`AsyncSubAgentMiddleware` (start/check/update/cancel/list tools) so it
can dispatch the autobuild stage as an `AsyncSubAgent` and stay
responsive while autobuild executes. DDR-007 §Decision binds the
`PipelineLifecycleEmitter` into `dispatch_autobuild_async`'s context
payload; this task adds the parameter and threads it through.

This task plus TASK-FW10-007 together make the receipt-only
`_default_dispatch` fully unreachable from production code paths.

## Files to create / modify

- `src/forge/pipeline/dispatchers/autobuild_async.py` (MODIFY):
  - Add `lifecycle_emitter: PipelineLifecycleEmitter` to the
    `dispatch_autobuild_async(...)` signature; thread it into the
    `start_async_task` context payload as `ctx['lifecycle_emitter']`.
  - Document in the docstring that the subagent reads
    `ctx['lifecycle_emitter']` and calls `emitter.on_transition(state)`
    from its `_update_state` helper.
- `src/forge/pipeline/supervisor.py` (MODIFY) or wherever the
  supervisor is constructed:
  - Add the `AsyncSubAgentMiddleware`'s tool surface
    (start/check/update/cancel/list) to the supervisor's tool list.
  - Pass `lifecycle_emitter` to the `AutobuildDispatcher` constructor
    so it reaches `dispatch_autobuild_async` at dispatch time.
- `src/forge/cli/serve.py` (MODIFY):
  - In `_run_serve`, construct the supervisor via the production
    factory with the four Wave 2 collaborators + the emitter from
    TASK-FW10-006 + the `AsyncSubAgentMiddleware`. (If the
    supervisor's existing constructor in
    `src/forge/pipeline/supervisor.py` needs a small refactor to
    accept the new parameter, do so here.)
- `tests/forge/test_supervisor_async_subagent_wiring.py` (NEW):
  - Asserts the supervisor's tool list includes the
    `AsyncSubAgentMiddleware` tools.
  - Asserts the autobuild dispatcher receives the emitter when called.
  - Smoke test: dispatch through the supervisor, assert
    `start_async_task` is called with `ctx['lifecycle_emitter']`
    populated.
- `tests/forge/test_supervisor_responsive.py` (NEW or extend existing):
  - Asserts that while the autobuild's async task is in flight, the
    supervisor's reasoning loop continues to answer status queries
    (Group A "supervisor stays responsive" scenario).

## Acceptance criteria

- [ ] `dispatch_autobuild_async` accepts `lifecycle_emitter` and
      threads it into the `start_async_task` context payload.
- [ ] The supervisor's tool list includes the `AsyncSubAgentMiddleware`'s
      start/check/update/cancel/list tools.
- [ ] The supervisor stays responsive (answers status queries) while
      autobuild's async task is in flight (Group A scenario).
- [ ] `_run_serve` constructs the supervisor with the emitter + the
      four Wave 2 collaborators; no second emitter is constructed.
- [ ] Diff inspection: `dispatch_autobuild_async` has exactly five
      collaborator parameters (`forward_context_builder`,
      `async_task_starter`, `stage_log_recorder`, `state_channel`,
      `lifecycle_emitter`) — DDR-007 §Consequences notes the new
      parameter is "acceptable — alongside the four it already
      accepts".
- [ ] All modified files pass project-configured lint/format checks
      with zero errors.

## Seam Tests

This task **consumes** the `AUTOBUILD_RUNNER_NAME` graph entry name
from TASK-FW10-002 and the `PipelineLifecycleEmitter` instance from
TASK-FW10-006. The seam test below validates the
`PipelineLifecycleEmitter` consumption.

```python
"""Seam test: verify PipelineLifecycleEmitter contract from TASK-FW10-006."""
import pytest


@pytest.mark.seam
@pytest.mark.integration_contract("PipelineLifecycleEmitter")
async def test_emitter_threaded_into_autobuild_dispatcher_context():
    """Verify dispatch_autobuild_async threads emitter into the context payload.

    Contract: emitter is threaded through dispatch_autobuild_async's
    context payload (DDR-007 Option A); the autobuild_runner subagent
    reads it as ctx['lifecycle_emitter']. Producer: TASK-FW10-006.
    """
    from forge.pipeline.dispatchers.autobuild_async import (
        dispatch_autobuild_async,
    )

    captured_ctx = {}

    async def fake_async_task_starter(name, *, ctx) -> str:
        captured_ctx.update(ctx)
        return "task-id-1"

    fake_emitter = object()
    fake_payload = {"feature_id": "F", "correlation_id": "c"}

    await dispatch_autobuild_async(
        payload=fake_payload,
        forward_context_builder=lambda *a, **kw: {},
        async_task_starter=fake_async_task_starter,
        stage_log_recorder=lambda *a, **kw: None,
        state_channel=lambda *a, **kw: None,
        lifecycle_emitter=fake_emitter,
    )

    assert captured_ctx.get("lifecycle_emitter") is fake_emitter, (
        "dispatch_autobuild_async must thread the emitter into the "
        "subagent's context per DDR-007 Option A"
    )
```

## Coach validation

- `pytest tests/forge/test_supervisor_async_subagent_wiring.py tests/forge/test_supervisor_responsive.py -x`.
- `pytest tests/cli tests/forge -x -k 'serve or supervisor or deps'` (smoke gate 3).
- Lint: project-configured ruff/format.

## References

- [DDR-007 §Decision](../../../docs/design/decisions/DDR-007-pipeline-lifecycle-emitter-wiring-path.md)
- [ADR-ARCH-031](../../../docs/architecture/decisions/ADR-ARCH-031-async-subagents-for-long-running-work.md)
- [`src/forge/pipeline/dispatchers/autobuild_async.py`](../../../src/forge/pipeline/dispatchers/autobuild_async.py)
- [`src/forge/pipeline/supervisor.py`](../../../src/forge/pipeline/supervisor.py)
- IMPLEMENTATION-GUIDE.md §4 contracts: `PipelineLifecycleEmitter`, `AUTOBUILD_RUNNER_NAME`
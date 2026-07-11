---
complexity: 6
consumer_context:
- consumes: DispatchFn
  driver: Python type alias Callable[[_MsgLike], Awaitable[None]]
  format_note: The dispatcher closure must conform to the new (_MsgLike) -> None signature;
    ack lifecycle is deferred to pipeline_consumer.handle_message's ack_callback.
  framework: _serve_daemon dispatch seam
  task: TASK-FW10-001
- consumes: PipelinePublisher
  driver: in-process Python object bound to the daemon's single shared NATS client
  format_note: PipelinePublisher instance from TASK-FW10-006 is held by the deps factory
    and bound into PipelineConsumerDeps.publish_build_failed.
  framework: PipelineConsumerDeps.publish_build_failed binding
  task: TASK-FW10-006
- consumes: ForwardContextBuilder
  driver: Protocol implementation
  format_note: ForwardContextBuilder Protocol implementation from TASK-FW10-003 is
    composed into the deps factory and threaded into the autobuild dispatch closure.
  framework: dispatch_autobuild_async collaborator
  task: TASK-FW10-003
- consumes: StageLogRecorder
  driver: Protocol implementation
  format_note: StageLogRecorder Protocol implementation from TASK-FW10-004 is composed
    into the deps factory.
  framework: dispatch_autobuild_async collaborator
  task: TASK-FW10-004
- consumes: AutobuildStateInitialiser
  driver: Protocol implementation
  format_note: AutobuildStateInitialiser Protocol implementation from TASK-FW10-005
    is composed into the deps factory; writes lifecycle='starting' on dispatch.
  framework: dispatch_autobuild_async collaborator
  task: TASK-FW10-005
dependencies:
- TASK-FW10-002
- TASK-FW10-003
- TASK-FW10-004
- TASK-FW10-005
- TASK-FW10-006
estimated_minutes: 90
feature_id: FEAT-FORGE-010
id: TASK-FW10-007
implementation_mode: task-work
parent_review: TASK-REV-FW10
priority: high
status: completed
tags:
- composition
- deps-factory
- dispatcher
- pipeline-consumer
task_type: feature
title: Compose PipelineConsumerDeps factory and dispatcher closure; replace receipt-only
  stub
wave: 3
reconciled: "2026-07-11 WS3-S8 sweep — FW10 in completed/; moved by 9ef9138b (FEAT-DEA8 finalize)"
---

# TASK-FW10-007 — Compose `PipelineConsumerDeps` factory + dispatcher closure

## Why

This is the composition step that turns the five Wave 2 components +
the seam refactor from Wave 1 into a working production dispatch
chain. After this task, the receipt-only `_default_dispatch` stub is
no longer reachable from any production code path; an inbound
`pipeline.build-queued.<feature_id>` envelope reaches
`pipeline_consumer.handle_message` and (on accept) reaches
`Supervisor.dispatch_build` via the closure built here.

## Files to create / modify

- `src/forge/cli/_serve_deps.py` (NEW):
  - `def build_pipeline_consumer_deps(client, forge_config, sqlite_pool) -> PipelineConsumerDeps`
  - Composes:
    - `is_duplicate_terminal` bound to a SQLite read helper from
      `forge.lifecycle.persistence` (uses the unique index on
      `(feature_id, correlation_id)` per ASSUM-014).
    - `dispatch_build` wired to a thin closure that calls
      `forge.pipeline.dispatchers.autobuild_async.dispatch_autobuild_async`
      with the four collaborators built in Wave 2 (TASK-FW10-003/004/005)
      plus the emitter from TASK-FW10-006.
    - `publish_build_failed` bound to
      `PipelinePublisher.publish_build_failed` via the publisher
      instance from TASK-FW10-006.
- `src/forge/cli/_serve_dispatcher.py` (NEW):
  - `def make_handle_message_dispatcher(deps: PipelineConsumerDeps) -> DispatchFn`
  - Returns an `async def dispatch(msg: _MsgLike) -> None` closure
    that calls `pipeline_consumer.handle_message(msg, deps)`. The
    state machine inside `handle_message` owns terminal-only ack via
    its `ack_callback`; the dispatcher does not ack.
- `src/forge/cli/serve.py` (MODIFY):
  - In `_run_serve`, call `build_pipeline_consumer_deps(...)` and
    `make_handle_message_dispatcher(...)`; rebind
    `_serve_daemon.dispatch_payload = dispatcher` before awaiting
    `run_daemon`.
  - Set `_serve_state.chain_ready = True` after the rebind.
- `tests/cli/test_serve_deps.py` (NEW):
  - Production factory wires the four collaborators correctly (mocked
    out at the seam).
  - `is_duplicate_terminal` returns True for a known
    `(feature_id, correlation_id)` and False for a novel pair.
- `tests/cli/test_serve_dispatcher.py` (NEW):
  - `make_handle_message_dispatcher` returns a `(_MsgLike) -> None`
    closure.
  - On a successful accept, `handle_message` is called with the
    `(msg, deps)` pair; the closure does **not** call `msg.ack()`
    itself (the state machine owns the ack via the callback).
  - On a malformed payload, `pipeline_consumer.handle_message` is
    expected to ack and publish `build-failed` (the actual behaviour
    is exercised by TASK-FW10-009; this test asserts only that the
    dispatcher delegates without short-circuiting).

## Acceptance criteria

- [ ] `build_pipeline_consumer_deps(client, forge_config, sqlite_pool)`
      returns a `PipelineConsumerDeps` with all four fields wired:
      `forge_config`, `is_duplicate_terminal`, `dispatch_build`,
      `publish_build_failed`.
- [ ] `make_handle_message_dispatcher(deps)` returns a closure
      conforming to `(_MsgLike) -> Awaitable[None]`.
- [ ] The closure delegates to `pipeline_consumer.handle_message(msg, deps)`
      and does not call `msg.ack()` itself.
- [ ] `_run_serve` rebinds `_serve_daemon.dispatch_payload` to the
      composed dispatcher before the consumer's first fetch; the
      receipt-only `_default_dispatch` is no longer reachable
      (Group A scenario "receipt-only stub no longer reachable").
- [ ] `_serve_state.chain_ready` is True after the rebind; the healthz
      probe reports healthy on the next read (assuming subscription
      live).
- [ ] All modified files pass project-configured lint/format checks
      with zero errors.

## Seam Tests

This task **consumes** the `DispatchFn` contract from TASK-FW10-001
and the `PipelinePublisher`, `ForwardContextBuilder`,
`StageLogRecorder`, `AutobuildStateInitialiser` artifacts from Wave 2.
The seam test below validates the most load-bearing of those — the
`DispatchFn` contract.

```python
"""Seam test: verify DispatchFn contract from TASK-FW10-001."""
import pytest


@pytest.mark.seam
@pytest.mark.integration_contract("DispatchFn")
async def test_dispatcher_closure_does_not_ack():
    """Verify the composed dispatcher closure does not call msg.ack().

    Contract: ack lifecycle is owned by pipeline_consumer.handle_message's
    ack_callback (terminal-only ack); the dispatcher is just the seam.
    Producer: TASK-FW10-001.
    """
    from forge.cli._serve_dispatcher import make_handle_message_dispatcher

    ack_calls = 0
    handle_calls = []

    class FakeMsg:
        data = b'{"feature_id":"F","correlation_id":"c"}'

        async def ack(self) -> None:
            nonlocal ack_calls
            ack_calls += 1

    async def fake_handle(msg, deps) -> None:
        handle_calls.append((msg, deps))

    # Stub deps; we only need the closure shape here
    fake_deps = object()
    import forge.adapters.nats.pipeline_consumer as pc
    pc.handle_message = fake_handle  # monkey-patch for seam test

    dispatcher = make_handle_message_dispatcher(fake_deps)
    await dispatcher(FakeMsg())

    assert handle_calls, "dispatcher must delegate to handle_message"
    assert ack_calls == 0, "dispatcher must not ack — ack is deferred to the state machine"
```

## Coach validation

- `pytest tests/cli/test_serve_deps.py tests/cli/test_serve_dispatcher.py -x`.
- `pytest tests/cli tests/forge -x -k 'serve or supervisor or deps'` (smoke gate 3).
- Lint: project-configured ruff/format.
- Diff inspection: confirm `_default_dispatch` has no production
  caller; the only references are in docstrings, tests (as a
  monkey-patch target), and the dormant fallback path inside
  `_serve_daemon`.

## References

- [TASK-FORGE-FRR-001 implementation_plan.md](../../../docs/state/TASK-FORGE-FRR-001/implementation_plan.md) (the load-bearing dispatcher closure design)
- [`src/forge/adapters/nats/pipeline_consumer.py`](../../../src/forge/adapters/nats/pipeline_consumer.py) (`handle_message`, `PipelineConsumerDeps`)
- IMPLEMENTATION-GUIDE.md §3 (sequence) and §4 (contracts)
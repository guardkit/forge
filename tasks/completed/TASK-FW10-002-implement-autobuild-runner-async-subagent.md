---
complexity: 8
conductor_workspace: wave2-autobuild-runner
consumer_context:
- consumes: DispatchFn
  driver: DeepAgents AsyncSubAgentMiddleware
  format_note: The subagent is invoked from the supervisor via start_async_task; the
    dispatcher closure built in TASK-FW10-007 against the new (_MsgLike) -> None DispatchFn
    calls handle_message which then calls dispatch_build which then dispatches autobuild_async.
  framework: DeepAgents AsyncSubAgent (start_async_task)
  task: TASK-FW10-001
- consumes: PipelineLifecycleEmitter
  driver: in-process Python object (non-serialisable)
  format_note: Emitter is threaded through dispatch_autobuild_async's context payload
    per DDR-007. The subagent calls emitter.on_transition(new_state) inside _update_state.
    Verify DeepAgents 0.5.3 accepts the non-serialisable payload via the smoke test
    in this task; if rejected, fall back to DDR-007 §Forward compatibility's RPC-shaped
    emitter wrapper.
  framework: DeepAgents AsyncSubAgent context payload (ASGI co-deployment)
  task: TASK-FW10-006
dependencies:
- TASK-FW10-001
estimated_minutes: 180
feature_id: FEAT-FORGE-010
id: TASK-FW10-002
implementation_mode: task-work
parent_review: TASK-REV-FW10
priority: high
status: completed
tags:
- net-new
- async-subagent
- deepagents
- ddr-007
- lifecycle-emitter
task_type: feature
title: Implement autobuild_runner AsyncSubAgent module with DDR-006/007 _update_state
wave: 2
reconciled: "2026-07-11 WS3-S8 sweep — FW10 in completed/; moved by 9ef9138b (FEAT-DEA8 finalize)"
---

# TASK-FW10-002 — Implement `autobuild_runner` AsyncSubAgent module

## Why

The `autobuild_runner` AsyncSubAgent is genuinely net-new — `find src/forge -name "*runner*"` returns nothing today. It's the production
implementation of the long-running autobuild stage that the supervisor
dispatches to via `start_async_task`. DDR-007 places the
`PipelineLifecycleEmitter` call at the `_update_state` boundary,
co-located with the DDR-006 state-channel write. This task creates the
module, registers it as a graph in `langgraph.json`, and wires the
emitter call.

The smoke test required by the acceptance criteria below also closes
risk **F3** (DeepAgents 0.5.3 non-serialisable context payload
contract). If that contract rejects the in-process emitter, this task
must raise a blocker before downstream tasks start depending on the
in-process shape.

## Files to create / modify

- `src/forge/subagents/__init__.py` (NEW) — package marker.
- `src/forge/subagents/autobuild_runner.py` (NEW):
  - The compiled DeepAgents `CompiledStateGraph` exported as `graph` for
    `langgraph.json` to address.
  - `_update_state(state, *, lifecycle, emitter, **deltas)` helper —
    writes the `async_tasks` channel via the supplied state-channel
    writer AND calls `emitter.on_transition(new_state)`. Both happen in
    the same function call; tests assert co-location.
  - Lifecycle transitions (per DDR-006 literals): `starting →
    planning_waves → running_wave → awaiting_approval → completed |
    failed | cancelled`. Each transition flows through `_update_state`.
  - `target_kind="subagent"`, `target_identifier=task_id` for the
    `stage_complete` envelopes emitted from inside the subagent
    (ASSUM-018).
  - Worktree confinement: every filesystem write must fall under the
    build's worktree allowlist (Group E security scenario). Use the
    `forward_context.worktree_path` as the filesystem root; reject
    paths that escape.
- `langgraph.json` (MODIFY):
  - Add a second graph entry: `"autobuild_runner": "./src/forge/subagents/autobuild_runner.py:graph"`.
  - Keep the existing `"orchestrator"` entry untouched.
- `tests/forge/test_autobuild_runner.py` (NEW):
  - Unit tests for `_update_state`'s co-located write + emit (assert
    both fire on every lifecycle transition).
  - Lifecycle transition matrix test (one assertion per transition).
  - Worktree confinement test: write under allowlist allowed; write
    outside allowlist rejected.
  - Smoke test (closes F3): instantiate the subagent's compiled graph,
    invoke one transition with a real `PipelineLifecycleEmitter`
    instance in context, assert both the state-channel write and the
    `emit_*` call fire. **If DeepAgents 0.5.3 rejects the
    non-serialisable context, fail this task and raise as a blocker.**
- `tests/forge/test_autobuild_runner_emit_taxonomy.py` (NEW):
  - Asserts `target_kind="subagent"` and `target_identifier == task_id`
    for the `stage_complete` envelope emitted from inside the subagent
    (ASSUM-018).

## Acceptance criteria

- [ ] `src/forge/subagents/autobuild_runner.py` exists and exports a
      `graph` module-level variable that is a `CompiledStateGraph`.
- [ ] `langgraph.json` has a second graph entry mapping
      `"autobuild_runner"` to the compiled graph; the existing
      `"orchestrator"` entry is unchanged; the file parses.
- [ ] `_update_state` writes the state channel **and** calls
      `emitter.on_transition(new_state)` in the same function. A
      transition that writes the channel without emitting (or vice
      versa) is a test failure.
- [ ] Lifecycle transitions follow DDR-006's `Literal` set; no
      transitions outside the set are emitted.
- [ ] `stage_complete` envelopes emitted from inside the subagent set
      `target_kind="subagent"` and `target_identifier=task_id`
      (ASSUM-018).
- [ ] Filesystem writes performed by the subagent fall under the
      build's worktree allowlist; writes outside the allowlist are
      rejected (Group E security scenario).
- [ ] Smoke test instantiates the subagent with a real
      `PipelineLifecycleEmitter` in context and exercises one
      transition; both the channel write and the emit fire.
- [ ] If DeepAgents 0.5.3 rejects the non-serialisable context payload,
      task fails with a clear blocker message; do not silently fall
      back without raising.
- [ ] Publish failures (`emit_*` raises) are logged at WARNING and the
      build continues — SQLite remains authoritative (ADR-ARCH-008,
      DDR-007 §Failure-mode contract).
- [ ] All modified files pass project-configured lint/format checks
      with zero errors.

## Seam Tests

This task **consumes** the `PipelineLifecycleEmitter` artifact from
TASK-FW10-006. The seam test below validates the consumption boundary.

```python
"""Seam test: verify PipelineLifecycleEmitter contract from TASK-FW10-006."""
import pytest


@pytest.mark.seam
@pytest.mark.integration_contract("PipelineLifecycleEmitter")
async def test_pipeline_lifecycle_emitter_threaded_through_context():
    """Verify the autobuild_runner subagent receives a usable emitter via context.

    Contract: emitter is threaded through dispatch_autobuild_async's
    context payload (DDR-007); the subagent calls emitter.on_transition
    from _update_state. Producer: TASK-FW10-006.
    """
    from forge.pipeline import PipelineLifecycleEmitter
    from forge.subagents.autobuild_runner import _update_state, AutobuildState

    emit_calls = []

    class FakeEmitter:
        def on_transition(self, state) -> None:
            emit_calls.append(state.lifecycle)

    state = AutobuildState(task_id="t1", feature_id="F", lifecycle="starting")

    # _update_state must call both the state-channel writer AND the emitter
    new_state = _update_state(
        state,
        lifecycle="planning_waves",
        emitter=FakeEmitter(),
        # other deps stubbed with no-op writers
    )

    assert new_state.lifecycle == "planning_waves"
    assert emit_calls == ["planning_waves"], (
        "emitter.on_transition must fire at the same boundary as the "
        "state-channel write per DDR-007"
    )
```

## Coach validation

- `pytest tests/forge/test_autobuild_runner.py tests/forge/test_autobuild_runner_emit_taxonomy.py -x`.
- `pytest tests/forge -x` (smoke gate 2; runs after Wave 2 completes).
- Lint: project-configured ruff/format.
- File existence: `ls src/forge/subagents/autobuild_runner.py` returns 0.
- `langgraph.json` parse check: `python -c "import json; json.load(open('langgraph.json'))"`.

## References

- [DDR-007](../../../docs/design/decisions/DDR-007-pipeline-lifecycle-emitter-wiring-path.md) (the canonical wiring decision)
- [DDR-006](../../../docs/design/decisions/DDR-006-async-subagent-state-channel-contract.md) (`AutobuildState` lifecycle + `_update_state` helper)
- [ADR-ARCH-031](../../../docs/architecture/decisions/ADR-ARCH-031-async-subagents-for-long-running-work.md)
- [ADR-ARCH-008](../../../docs/architecture/decisions/ADR-ARCH-008-forge-produces-own-history.md) (publish failure does not regress SQLite state)
- [API-nats-pipeline-events.md §3](../../../docs/design/contracts/API-nats-pipeline-events.md) (StageCompletePayload `target_kind` / `target_identifier`)
- IMPLEMENTATION-GUIDE.md §3 (sequence) and §4 (contracts)
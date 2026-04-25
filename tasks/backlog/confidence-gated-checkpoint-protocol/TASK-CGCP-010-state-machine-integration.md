---
id: TASK-CGCP-010
title: Wire gate_check wrapper into FEAT-FORGE-001 state machine (pause-and-publish
  atomicity)
task_type: feature
status: in_review
priority: high
created: 2026-04-25 00:00:00+00:00
updated: 2026-04-25 00:00:00+00:00
parent_review: TASK-REV-CG44
feature_id: FEAT-FORGE-004
wave: 4
implementation_mode: task-work
complexity: 6
dependencies:
- TASK-CGCP-006
- TASK-CGCP-007
tags:
- gating
- state-machine
- integration
- data-integrity
- crash-recovery
- concurrency
consumer_context:
- task: TASK-CGCP-006
  consumes: ApprovalPublisher.publish_request
  framework: NATS adapter (nats-py + nats-core typed payloads)
  driver: forge.adapters.nats.approval_publisher
  format_note: Wrapper writes GateDecision to SQLite FIRST, then calls publish_request.
    Publish failure surfaces as an operational signal but does NOT roll back the SQLite
    row (F10).
- task: TASK-CGCP-007
  consumes: ApprovalSubscriber.await_response
  framework: NATS adapter (nats-py + nats-core typed payloads)
  driver: forge.adapters.nats.approval_subscriber
  format_note: "await_response returns Optional[ApprovalResponsePayload]; None means\
    \ timeout/duplicate/refused \u2014 caller's wait loop continues. Wrapper invokes\
    \ resume_value_as on the response payload before attribute access (DDR-002, TASK-CGCP-009)."
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-004
  base_branch: main
  started_at: '2026-04-25T18:28:12.555029'
  last_updated: '2026-04-25T18:43:51.406101'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-25T18:28:12.555029'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# Task: Wire gate_check wrapper into FEAT-FORGE-001 state machine (pause-and-publish atomicity)

## Description

Implement the `forge.gating.wrappers.gate_check(...)` coordinator that
runs at every gated stage. The wrapper is the **only** code that knows
how to compose:

1. Read priors (`forge.adapters.graphiti.read_priors`)
2. Read calibration adjustments (`read_adjustments(approved_only=True)` — closes risk **R8**)
3. Call `forge.gating.evaluate_gate(...)` (pure)
4. Write `GateDecision` to SQLite `stage_log.details_json["gate"]` and Graphiti `forge_pipeline_history`
5. **If `mode == AUTO_APPROVE`**: continue
6. **If `mode == HARD_STOP`**: transition to FAILED via FEAT-FORGE-001 state machine
7. **If `mode in {FLAG_FOR_REVIEW, MANDATORY_HUMAN_APPROVAL}`**:
   - Persist `request_id` (TASK-CGCP-003 derivation) into the paused-build row
   - Atomically: enter PAUSED state + publish `ApprovalRequestPayload` (closes risk **R7**)
   - Await response via `ApprovalSubscriber.await_response(...)`
   - Rehydrate via `resume_value_as(ApprovalResponsePayload, raw)` (closes risk **R2**)
   - On approve → resume; on reject → transition CANCELLED; on override → mark stage overridden, continue; on defer → re-publish with attempt_count+1

Also wire **crash-recovery re-emission** (Group D `@regression` "A build that was paused before a crash re-emits its approval request on restart"):

- On boot, read the `paused_builds` view from FEAT-FORGE-001 SQLite substrate
- For each paused build, re-publish `ApprovalRequestPayload` using the **persisted** `request_id` (not re-derived) — guarantees responder-side dedup holds (closes risk **R5**)

Wire **`forge cancel` / `forge skip` CLI commands** to call into TASK-CGCP-008's synthetic injector. The CLI command lives in the orchestrator's `cli` module; this task implements the bridge from CLI → injector → paused-build resume path.

Files to create:

- `src/forge/gating/wrappers.py` — `gate_check(...)` coordinator (Domain Core, but allowed to import the `forge.adapters.*` Protocol surfaces for graphiti/sqlite/approval — those are dependency-injected)
- `src/forge/adapters/nats/approval_state_hooks.py` — bridges between state machine and approval adapter
- CLI bridge for `forge cancel` / `forge skip` (calls TASK-CGCP-008)
- Boot-time hook that triggers re-emission for each paused build

## Acceptance Criteria

- [ ] `gate_check(...)` orchestrates the full read → evaluate → write → publish/transition sequence
- [ ] **F9** `read_adjustments(approved_only=True)` is the only filter point for unapproved adjustments — Group C `@negative` "An unapproved calibration adjustment does not influence gate decisions" passes
- [ ] **F10 / Group E `@data-integrity @regression`** "A gate decision is recorded durably even if the notification publish fails": SQLite write completes BEFORE publish; publish failure surfaces as operational signal but does NOT roll back
- [ ] **F5 / Group E `@concurrency @data-integrity`** "Pausing and publishing the approval request are observed as a single consistent transition": from any external observer's standpoint, status query NEVER reports `PAUSED` without a corresponding bus publish having been issued
- [ ] **Group A** "Approval from Rich resumes the paused build": approve response → state machine resumes from the gated stage
- [ ] **Group A** "Rejection from Rich ends the paused build": reject response → CANCELLED transition with Rich's reason recorded
- [ ] **Group A** "Override from Rich skips the stage and the build continues": override response → stage marked overridden, build proceeds
- [ ] **Group D `@regression`** "Re-emits its approval request on restart": boot-time hook reads paused_builds and re-publishes; the persisted `request_id` is used (not re-derived), so responders dedup correctly
- [ ] **Group D `@edge-case`** "Cancelling from CLI behaves as rejection": `forge cancel FEAT-XXX` invokes TASK-CGCP-008's `inject_cli_cancel`, build → CANCELLED with `reason="cli cancel"`
- [ ] **Group D `@edge-case`** "Skipping from CLI overrides current stage only": `forge skip FEAT-XXX` invokes TASK-CGCP-008's `inject_cli_skip`, current stage overridden, build continues
- [ ] **F4 / Group D `Scenario Outline`** "typed-vs-dict rehydration": every read of the response goes through `resume_value_as(ApprovalResponsePayload, raw)` — verified by TASK-CGCP-009's CI grep guard
- [ ] **Group D `@edge-case`** "Per-build response routing": two builds paused on different stages — a response addressed to one does not affect the other; verified by integration test
- [ ] **Group D `@edge-case`** "Max-wait ceiling reached": when total wait reaches `ApprovalConfig.max_wait_seconds`, the pause ends per the configured fallback (see ASSUM-003 deferral); reason recorded against the gate
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Seam Tests

```python
"""Seam test: verify ApprovalPublisher contract from TASK-CGCP-006."""
import pytest
from forge.adapters.nats.approval_publisher import ApprovalPublisher


@pytest.mark.seam
@pytest.mark.integration_contract("ApprovalPublisher.publish_request")
def test_publisher_method_exists():
    """Verify ApprovalPublisher.publish_request is async and present.

    Contract: publish_request(envelope) -> None async method
    Producer: TASK-CGCP-006
    """
    import inspect
    assert hasattr(ApprovalPublisher, "publish_request")
    assert inspect.iscoroutinefunction(ApprovalPublisher.publish_request)
```

```python
"""Seam test: verify ApprovalSubscriber contract from TASK-CGCP-007."""
import pytest
from forge.adapters.nats.approval_subscriber import ApprovalSubscriber


@pytest.mark.seam
@pytest.mark.integration_contract("ApprovalSubscriber.await_response")
def test_subscriber_method_exists():
    """Verify ApprovalSubscriber.await_response is async and returns Optional.

    Contract: await_response(build_id, *, timeout_seconds) -> ApprovalResponsePayload | None
    Producer: TASK-CGCP-007
    """
    import inspect
    assert hasattr(ApprovalSubscriber, "await_response")
    assert inspect.iscoroutinefunction(ApprovalSubscriber.await_response)
```

## Implementation Notes

- Mirror the integration shape of `forge.adapters.nats.state_machine_hooks` (FEAT-FORGE-002)
- Pause-and-publish atomicity does NOT require a distributed transaction; it requires SQLite-write → publish ordering enforced inside a single async function with no `await` between the SQLite commit and the publish call (publishing acquires an asyncio task; status queries acquire the same loop, so the publish is observable before any subsequent paused-status query resolves)
- The wrapper's tests can use an in-memory NATS double + temp SQLite — full integration in TASK-CGCP-011

---
id: TASK-CGCP-008
title: 'Implement synthetic_response_injector for forge cancel/skip CLI steering'
task_type: feature
status: backlog
priority: high
created: 2026-04-25T00:00:00Z
updated: 2026-04-25T00:00:00Z
parent_review: TASK-REV-CG44
feature_id: FEAT-FORGE-004
wave: 2
implementation_mode: task-work
complexity: 4
dependencies:
- TASK-CGCP-003
- TASK-CGCP-002
tags:
- nats
- adapter
- cli
- approval
- synthetic-response
consumer_context:
- task: TASK-CGCP-003
  consumes: request_id derivation
  framework: pure-Python helper (forge.gating.identity.derive_request_id)
  driver: forge.gating.identity
  format_note: Synthetic injector reads the persisted request_id for the paused stage from SQLite (not re-derived) to guarantee responder-side dedup against any concurrent real response.
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Task: Implement synthetic_response_injector for forge cancel/skip CLI steering

## Description

Implement the `forge cancel` and `forge skip` CLI steering paths per
`API-nats-approval-protocol.md §7` and ASSUM-005 (high). When Rich runs
`forge cancel FEAT-XXX` or `forge skip FEAT-XXX` from the command line for
a paused build, the system **injects a synthetic
`ApprovalResponsePayload`** that flows through the **same dedup-and-resume
path** real Rich responses traverse.

Files to create:

- `src/forge/adapters/nats/synthetic_response_injector.py`:
  - `inject_cli_cancel(build_id, stage_label, attempt_count) -> None`
  - `inject_cli_skip(build_id, stage_label, attempt_count) -> None`

The injector publishes a typed `ApprovalResponsePayload` onto the standard
`agents.approval.forge.{build_id}.response` subject so the approval
subscriber (TASK-CGCP-007) consumes it through its normal idempotency
gate. **No parallel resume code path** — that would silently bypass
idempotency (closes risk in F6).

Mappings (ASSUM-005 high):

- `forge cancel FEAT-XXX` → `ApprovalResponsePayload(decision="reject", responder="rich", reason="cli cancel")`
- `forge skip FEAT-XXX` → `ApprovalResponsePayload(decision="override", responder="rich", reason="cli skip")`

## Acceptance Criteria

- [ ] `inject_cli_cancel(...)` publishes `ApprovalResponsePayload(decision="reject", responder="rich", reason="cli cancel", request_id=...)` to `agents.approval.forge.{build_id}.response`
- [ ] `inject_cli_skip(...)` publishes `ApprovalResponsePayload(decision="override", responder="rich", reason="cli skip", request_id=...)` to the same subject
- [ ] `request_id` carried by the synthetic response **matches** the persisted `request_id` for the paused stage (via TASK-CGCP-003 `derive_request_id`) — guarantees dedup against any concurrent real response
- [ ] **Group D `@edge-case`** "Cancelling a paused build from the command line behaves as a rejection": injector + state machine produces a CANCELLED outcome with reason recorded as coming from the command line
- [ ] **Group D `@edge-case`** "Skipping a paused build from the command line overrides the current stage only": injector + state machine produces an override on the current stage, build continues to next stage
- [ ] **Idempotency invariant**: a synthetic response that arrives after a real response has already resumed is recorded as a duplicate and has no effect (verified via TASK-CGCP-007's dedup buffer)
- [ ] **Persisted-record distinction**: the persisted `GateDecision`'s response record carries `responder="rich"` AND `reason ∈ {"cli cancel", "cli skip"}` so it is distinguishable from a real Rich response (which carries a different `reason`)
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Seam Tests

```python
"""Seam test: verify request_id derivation contract from TASK-CGCP-003."""
import pytest
from forge.gating.identity import derive_request_id


@pytest.mark.seam
@pytest.mark.integration_contract("derive_request_id")
def test_synthetic_response_uses_persisted_request_id():
    """Verify synthetic injector keys on the same deterministic request_id.

    Contract: synthetic responses use the SAME request_id as the original
    paused stage to guarantee dedup against any racing real response.
    Producer: TASK-CGCP-003
    """
    rid = derive_request_id(build_id="b1", stage_label="Architecture Review", attempt_count=2)
    # Synthetic and real responses produce identical ids for the same paused stage
    assert rid == derive_request_id(build_id="b1", stage_label="Architecture Review", attempt_count=2)
```

## Implementation Notes

- The `request_id` MUST come from SQLite (the value persisted at first emission), not re-derived live. This prevents drift if the `attempt_count` has advanced after a refresh
- Mirror the publishing path shape of `approval_publisher` (TASK-CGCP-006) — same envelope wrapping, same `nats_core` types
- The CLI command itself (`forge cancel ...`) is owned by the orchestrator's CLI module — this task only implements the inject API; CLI wiring is included as part of TASK-CGCP-010 (state-machine integration) where it makes contact with the running build

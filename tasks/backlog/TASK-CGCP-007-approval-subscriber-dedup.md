---
id: TASK-CGCP-007
title: 'Implement approval_subscriber with short-TTL dedup buffer (first-response-wins)'
task_type: feature
status: backlog
priority: high
created: 2026-04-25T00:00:00Z
updated: 2026-04-25T00:00:00Z
parent_review: TASK-REV-CG44
feature_id: FEAT-FORGE-004
wave: 3
implementation_mode: task-work
complexity: 6
dependencies:
- TASK-CGCP-005
- TASK-CGCP-002
- TASK-CGCP-003
tags:
- nats
- adapter
- subscriber
- approval
- idempotency
- concurrency
consumer_context:
- task: TASK-CGCP-003
  consumes: request_id derivation
  framework: pure-Python helper (forge.gating.identity.derive_request_id)
  driver: forge.gating.identity
  format_note: request_id is deterministic over (build_id, stage_label, attempt_count); the dedup set keys directly on the literal request_id string the responder echoes back. Subscriber MUST NOT re-derive — it MUST trust the echoed value.
- task: TASK-CGCP-002
  consumes: ApprovalConfig.max_wait_seconds
  framework: Pydantic v2 BaseModel (forge.config.models)
  driver: pyyaml + pydantic
  format_note: ApprovalConfig.max_wait_seconds caps total wait (default 3600). Subscriber refresh-loop logic refreshes within this ceiling per API §7.
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Task: Implement approval_subscriber with short-TTL dedup buffer (first-response-wins)

## Description

Create `src/forge/adapters/nats/approval_subscriber.py` owning the inbound
subscription on `agents.approval.forge.{build_id}.response` per
`API-nats-approval-protocol.md §5` and the **idempotency contract** per §6
(ASSUM-006 high).

Responsibilities:

- Subscribe to the `.response` mirror subject for a given `build_id`
- Validate envelopes via `MessageEnvelope.model_validate_json` and payload via `ApprovalResponsePayload.model_validate`
- Maintain a **short-TTL dedup set** keyed on `request_id` — first-response-wins; subsequent responses for the same `request_id` are recorded as duplicates and discarded
- Surface duplicates to the state-machine resume path as `None` (not a typed response) so the caller's wait loop continues without resuming twice
- **Refuse unrecognised decision values** (Group C `@negative` "An approval response with an unrecognised decision value is refused"): payload validation rejects values outside `Literal["approve", "reject", "defer", "override"]`; the rejection is surfaced so a correctly-formed response can still be sent
- **Refuse unrecognised responder identities** (Group E `@security`): a response whose `responder` is not the configured "expected approver" for this deployment does not resume the build on its own; surfaces the anomaly for review

## Acceptance Criteria

- [ ] `ApprovalSubscriber.await_response(build_id: str, *, timeout_seconds: int) -> ApprovalResponsePayload | None` async method
- [ ] Subscribe pattern matches `agents.approval.forge.{build_id}.response` (project-scoped via `Topics.for_project` if configured)
- [ ] Dedup set is **per-subscriber-instance** with short TTL (e.g. 300s); keyed on `request_id`
- [ ] Dedup set is asyncio-lock protected — closes risk **R4** (concurrent-response race)
- [ ] **Group D `@edge-case`** "Duplicate approval responses are ignored after the first": second response with same `request_id` is observed but does NOT resume the build
- [ ] **Group E `@concurrency @edge-case`** "Two responses arriving at the same moment resolve to exactly one decision": `asyncio.gather(send_a, send_b)` resolves to one approval; the other is recorded as a duplicate
- [ ] **Group D `@edge-case`** "Per-build response routing": responses on `agents.approval.forge.{build_id_a}.response` do NOT affect a different build's wait loop
- [ ] **Group C `@negative`** "Unrecognised decision value": Pydantic validator rejects unknown decision; subscriber surfaces the rejection as a typed `InvalidDecisionError` and continues waiting — pause is NOT cancelled
- [ ] **Group E `@security`** "Unrecognised responder": when `responder` does not match the deployment's configured expected approver, the response is logged as anomaly and the build continues waiting (does NOT resume)
- [ ] Refresh-on-timeout per `API §7`: when `default_wait_seconds` elapses without a response, a fresh `ApprovalRequestPayload` is published with incremented `attempt_count` (via TASK-CGCP-003 derivation) — the prior `request_id` remains valid for dedup until short TTL elapses
- [ ] Total refresh loop bounded by `ApprovalConfig.max_wait_seconds` per §7 / ASSUM-002
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Seam Tests

```python
"""Seam test: verify request_id derivation contract from TASK-CGCP-003."""
import pytest
from forge.gating.identity import derive_request_id


@pytest.mark.seam
@pytest.mark.integration_contract("derive_request_id")
def test_request_id_is_deterministic():
    """Verify request_id derivation is pure and deterministic.

    Contract: derive_request_id(build_id, stage_label, attempt_count) -> str
    Producer: TASK-CGCP-003
    """
    a = derive_request_id(build_id="b1", stage_label="Architecture Review", attempt_count=0)
    b = derive_request_id(build_id="b1", stage_label="Architecture Review", attempt_count=0)
    c = derive_request_id(build_id="b1", stage_label="Architecture Review", attempt_count=1)
    assert a == b
    assert a != c  # attempt_count change produces a distinct id
```

## Implementation Notes

- Mirror the structural pattern of `forge.adapters.nats.pipeline_consumer` (FEAT-FORGE-002)
- Clock injection mandatory — no `datetime.now()` in the dedup TTL logic; use a `Clock` Protocol so tests advance time deterministically
- The expected approver value lands as a config field — likely `ApprovalConfig.expected_approver: str | None = None`; if `None`, the responder check is permissive (single-deployment dev mode); if set, only that responder resumes
- Pause is the caller's concern — subscriber returns `None` on timeout/duplicate/refused so the caller decides

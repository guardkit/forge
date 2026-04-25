---
id: TASK-CGCP-006
title: Implement approval_publisher (publish ApprovalRequestPayload + details builder)
task_type: feature
status: in_review
priority: high
created: 2026-04-25 00:00:00+00:00
updated: 2026-04-25 00:00:00+00:00
parent_review: TASK-REV-CG44
feature_id: FEAT-FORGE-004
wave: 3
implementation_mode: task-work
complexity: 5
dependencies:
- TASK-CGCP-005
- TASK-CGCP-002
tags:
- nats
- adapter
- publisher
- approval
- data-integrity
consumer_context:
- task: TASK-CGCP-005
  consumes: GateDecision
  framework: Pydantic v2 BaseModel (forge.gating.models)
  driver: forge.gating.evaluate_gate
  format_note: "GateDecision per DM-gating.md \xA71; populates the `details` dict\
    \ per API-nats-approval-protocol.md \xA73.2 with eight documented keys (build_id,\
    \ feature_id, stage_label, gate_mode, coach_score, criterion_breakdown, detection_findings,\
    \ rationale, evidence_priors, artefact_paths, resume_options)."
- task: TASK-CGCP-002
  consumes: ApprovalConfig.default_wait_seconds
  framework: Pydantic v2 BaseModel (forge.config.models)
  driver: pyyaml + pydantic
  format_note: ApprovalConfig.default_wait_seconds is non-negative int (default 300);
    passed verbatim into ApprovalRequestPayload.timeout_seconds.
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-004
  base_branch: main
  started_at: '2026-04-25T18:04:48.086921'
  last_updated: '2026-04-25T18:16:51.376792'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-25T18:04:48.086921'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# Task: Implement approval_publisher (publish ApprovalRequestPayload + details builder)

## Description

Create `src/forge/adapters/nats/approval_publisher.py` owning the outbound
publication of `ApprovalRequestPayload` to `agents.approval.forge.{build_id}`
per `API-nats-approval-protocol.md §2`–§3.

Responsibilities:

- Build an `ApprovalRequestPayload` (schema owned by `nats-core` — do not redeclare)
- Populate `details: dict[str, Any]` per §3.2 via `_build_approval_details(...)` helper:
  `build_id`, `feature_id`, `stage_label`, `gate_mode`, `coach_score`,
  `criterion_breakdown`, `detection_findings`, `rationale`, `evidence_priors`,
  `artefact_paths`, `resume_options`
- Derive `risk_level` per §3.3 (table)
- Wrap in `MessageEnvelope(event_type=APPROVAL_REQUEST, source_id="forge", correlation_id=...)`
- Publish via the `nats_core` client surface

**Critical contract** (closes risk **R6**, F10): the SQLite write of the
`GateDecision` mirror **must precede** the publish call. The publisher
itself does not own the SQLite write — it is invoked by the wrapper
(TASK-CGCP-010) which orchestrates SQLite-write → publish ordering.
Publish failures **surface as operational signals** but **do not** cause
the caller to roll back the recorded decision.

## Acceptance Criteria

- [ ] `ApprovalPublisher.publish_request(envelope: MessageEnvelope) -> None` async method
- [ ] `_build_approval_details(decision: GateDecision, **context) -> dict` produces the eleven-key dict per `API §3.2`
- [ ] `_derive_risk_level(decision: GateDecision) -> Literal["low", "medium", "high"]` matches the table in §3.3 exactly
- [ ] Subject resolves to `agents.approval.forge.{build_id}` (project-scoped via `Topics.for_project` if configured)
- [ ] Publish failures raise a typed `ApprovalPublishError` that the caller can catch — failure does NOT swallow silently
- [ ] **Group E `@data-integrity @regression`**: when the publish call raises, the helper still returns control to the caller without rolling back any state — verified by integration test in TASK-CGCP-011
- [ ] **Group E `@integration`**: a published request carries enough context for an adapter to render — assertion against the eleven-key dict
- [ ] Module is the **only** place in `forge.gating` / `forge.adapters.nats.approval_*` where the `details` dict shape is constructed
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Seam Tests

```python
"""Seam test: verify GateDecision contract from TASK-CGCP-005."""
import pytest
from forge.gating.models import GateDecision, GateMode
from forge.adapters.nats.approval_publisher import _build_approval_details


@pytest.mark.seam
@pytest.mark.integration_contract("GateDecision")
def test_gate_decision_drives_approval_details():
    """Verify GateDecision populates the eleven-key details dict.

    Contract: GateDecision per DM-gating.md §1; details dict per API §3.2
    Producer: TASK-CGCP-005
    """
    decision = GateDecision(
        build_id="build-test-001",
        stage_label="Architecture Review",
        target_kind="local_tool",
        target_identifier="some_tool",
        mode=GateMode.FLAG_FOR_REVIEW,
        rationale="reasoning model says ambiguous",
        coach_score=0.52,
        criterion_breakdown={"fidelity": 0.4, "rigour": 0.6},
        detection_findings=[],
        evidence=[],
    )
    details = _build_approval_details(
        decision,
        feature_id="FEAT-TEST",
        artefact_paths=["/tmp/x"],
        resume_options=["approve", "reject", "defer", "override"],
    )
    expected_keys = {
        "build_id", "feature_id", "stage_label", "gate_mode",
        "coach_score", "criterion_breakdown", "detection_findings",
        "rationale", "evidence_priors", "artefact_paths", "resume_options",
    }
    assert expected_keys.issubset(details.keys())
    assert details["gate_mode"] == "FLAG_FOR_REVIEW"
```

```python
"""Seam test: verify ApprovalConfig.default_wait_seconds contract from TASK-CGCP-002."""
import pytest
from forge.config.models import ApprovalConfig


@pytest.mark.seam
@pytest.mark.integration_contract("ApprovalConfig.default_wait_seconds")
def test_approval_config_default_wait_format():
    """Verify default_wait_seconds is non-negative int with default 300.

    Contract: ApprovalConfig.default_wait_seconds: int = 300
    Producer: TASK-CGCP-002
    """
    cfg = ApprovalConfig()
    assert cfg.default_wait_seconds == 300
    assert cfg.max_wait_seconds == 3600
    with pytest.raises(ValueError):
        ApprovalConfig(default_wait_seconds=-1)
```

## Implementation Notes

- Mirror the pattern from `forge.adapters.nats.fleet_publisher` (FEAT-FORGE-002): async methods, typed payload, no domain imports
- Use `nats_core.events.ApprovalRequestPayload` and `nats_core.events.MessageEnvelope` — do NOT redeclare
- `correlation_id` propagates from the build's `BuildQueuedPayload.correlation_id` — caller passes it explicitly

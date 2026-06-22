---
id: TASK-RBX-003
title: RunbookPublisher (mirror pipeline_publisher)
status: completed
created: 2026-06-21 18:45:00+00:00
updated: 2026-06-21 18:45:00+00:00
priority: high
task_type: feature
parent_review: TASK-REV-RBX-001
parent_feature: FEAT-RBX
feature_slug: runbook-executor
wave: 2
implementation_mode: task-work
complexity: 5
estimated_minutes: 75
dependencies:
- TASK-RBX-002
consumer_context:
- task: TASK-RBX-002
  consumes: runbook_lifecycle_events
  framework: nats_core.envelope.MessageEnvelope + EventType (async nats client)
  driver: nats.aio.client.Client.publish (awaitable)
  format_note: envelope source_id='forge'; event_type must be one of the 5 runbook
    EventType members; subject 'runbook.{event}.{runbook_id}'; correlation_id threaded
    from payload
tags:
- forge
- runbook
- executor
- nats
- publisher
autobuild_state:
  current_turn: 3
  max_turns: 5
  worktree_path: /Users/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-RBX
  base_branch: main
  started_at: '2026-06-22T08:02:07.778056'
  last_updated: '2026-06-22T08:31:17.006567'
  turns:
  - turn: 1
    decision: feedback
    feedback: '- Sibling-repo (evidence_repos) independent tests did not pass:

      - nats-core: sibling-repo tests FAILED (exit 1) for `python -m pytest tests
      -q`'
    timestamp: '2026-06-22T08:02:07.778056'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
  - turn: 2
    decision: feedback
    feedback: '- Sibling-repo (evidence_repos) independent tests did not pass:

      - nats-core: sibling-repo tests FAILED (exit 1) for `python -m pytest tests
      -q`'
    timestamp: '2026-06-22T08:13:06.589201'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
  - turn: 3
    decision: approve
    feedback: null
    timestamp: '2026-06-22T08:19:17.177113'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# RunbookPublisher (mirror pipeline_publisher)

## TL;DR

A thin async publisher for the five runbook lifecycle events, built as a
faithful sibling of `forge.adapters.nats.pipeline_publisher.PipelinePublisher`.
Same `source_id="forge"`, same fire-and-forget semantics, same
`PublishFailure` contract (logged, **never** rolled back). The executor
(TASK-RBX-004) injects it.

## Scope

New module `src/forge/adapters/nats/runbook_publisher.py`.

- **`RunbookPublisher(nats_client)`** with one method per event:
  - `publish_runbook_started(RunbookStartedPayload)`
  - `publish_step_started(StepStartedPayload)`
  - `publish_step_result(StepResultPayload)`
  - `publish_runbook_complete(RunbookCompletePayload)`
  - `publish_escalated(EscalatedPayload)`
- Reuse the `pipeline_publisher` shape verbatim:
  - `SOURCE_ID = "forge"` stamped on every envelope.
  - `_subject_for(event_name, runbook_id) -> f"runbook.{event_name}.{runbook_id}"`
    (subject prefix `"runbook"`).
  - A private `_publish_envelope(*, event_name, event_type, payload)` builds the
    `MessageEnvelope`, threads `correlation_id` off the payload, serialises,
    and calls `await nc.publish(subject, body)` exactly once.
  - On transport error: log at WARNING, then `raise PublishFailure(subject, exc)
    from exc`. **Do not** retry, **do not** roll back.
  - PubAck (if any) logged at DEBUG only — never treated as proof of delivery
    (LES1 parity rule).
- Reuse / re-export `PublishFailure` and `SOURCE_ID` from
  `pipeline_publisher` rather than redefining them.

## Acceptance Criteria

- [ ] Each of the five methods publishes to `runbook.{event}.{runbook_id}` with
      an envelope whose `source_id == "forge"`, correct `event_type`, and the
      payload's `correlation_id` threaded onto the envelope (mirrors
      `test_pipeline_publisher.py`).
- [ ] A `nc.publish` that raises is surfaced as `PublishFailure` carrying the
      subject and the original cause; the warning is logged first.
- [ ] The publisher performs no retry and mutates no external state — it builds
      one envelope and publishes once per call.
- [ ] An informational PubAck does not change the outcome (still returns
      normally; logged at DEBUG).
- [ ] All modified files pass project-configured lint/format checks with zero
      errors.
- [ ] Tests added to `tests/forge/test_runbook_publisher.py` with a mock async
      client, written **test-first** (TDD).

## Coach Validation

```bash
python -m pytest tests/forge/test_runbook_publisher.py -q
python -m pytest tests/forge/test_runbook_publisher.py -q -m seam
```

## §4 Seam Tests

Validates the `runbook_lifecycle_events` contract from TASK-RBX-002 — every
runbook event type resolves to a registered payload, and the payload status
vocabulary equals the forge `StepStatus` set.

```python
"""Seam test: verify runbook_lifecycle_events contract (TASK-RBX-002)."""
import pytest

from nats_core.envelope import EventType, payload_class_for_event_type
from forge.persistence.repositories.runbook_models import StepStatus


@pytest.mark.seam
@pytest.mark.integration_contract("runbook_lifecycle_events")
def test_every_runbook_event_type_has_a_registered_payload() -> None:
    """All five runbook EventType members resolve to a payload class.

    Contract: payload_class_for_event_type must not raise for any runbook
    event the publisher emits.
    Producer: TASK-RBX-002
    """
    runbook_events = [
        EventType.RUNBOOK_STARTED,
        EventType.STEP_STARTED,
        EventType.STEP_RESULT,
        EventType.RUNBOOK_COMPLETE,
        EventType.ESCALATED,
    ]
    for event_type in runbook_events:
        # Must not raise KeyError — registry entry required.
        assert payload_class_for_event_type(event_type) is not None


@pytest.mark.seam
@pytest.mark.integration_contract("runbook_lifecycle_events")
def test_step_result_status_vocabulary_matches_step_status() -> None:
    """StepResultPayload.status admits exactly the StepStatus value set.

    Contract: the payload's status set must equal {s.value for s in StepStatus}.
    Producer: TASK-RBX-002
    """
    from nats_core.events import StepResultPayload

    valid = {s.value for s in StepStatus}
    for value in valid:
        StepResultPayload(
            runbook_id="rb",
            sequence_index=0,
            step_type="shell",
            status=value,
            result=None,
            correlation_id="c",
        )
    with pytest.raises(Exception):
        StepResultPayload(
            runbook_id="rb",
            sequence_index=0,
            step_type="shell",
            status="not_a_real_status",
            result=None,
            correlation_id="c",
        )
```

## Implementation Notes

- Keep the class "thin" exactly as `PipelinePublisher` is — no scheduling, no
  buffering. The executor decides *when* to publish.
- If `nats-core` could not import `StepStatus` and modelled the status as a
  local `Literal`/`StrEnum` in TASK-RBX-002, the second seam test is the guard
  that the two value sets have not drifted.

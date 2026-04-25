---
id: TASK-NFI-009
title: Implement pipeline_consumer.reconcile_on_boot (crash recovery + paused re-announce)
task_type: feature
status: in_review
priority: high
created: 2026-04-24 00:00:00+00:00
updated: 2026-04-24 00:00:00+00:00
parent_review: TASK-REV-NF20
feature_id: FEAT-FORGE-002
wave: 4
implementation_mode: task-work
complexity: 6
dependencies:
- TASK-NFI-007
- TASK-NFI-008
tags:
- crash-recovery
- reconciliation
- pipeline
- idempotency
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002
  base_branch: main
  started_at: '2026-04-25T14:36:11.605049'
  last_updated: '2026-04-25T14:49:38.842668'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-25T14:36:11.605049'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# Task: Implement pipeline_consumer.reconcile_on_boot

## Description

Implement crash-recovery reconciliation described in
`API-nats-pipeline-events.md §4`. On Forge restart, the pull consumer
re-subscribes with `durable="forge-consumer"` — JetStream redelivers
unacked `build-queued` messages. For each redelivery, this module
decides: ack, restart, resume, or treat as new.

Reconciliation rules (per redelivered `BuildQueuedPayload`):

| SQLite state | Action |
|---|---|
| `COMPLETE / FAILED / CANCELLED / SKIPPED` | Ack immediately (idempotent — previous run finished before ack) |
| `RUNNING / FINALISING` | Mark `INTERRUPTED`, restart from `PREPARING` (retry-from-scratch) |
| `PAUSED` | Re-enter PAUSED, re-emit `BuildPausedPayload` + `ApprovalRequestPayload` (idempotent on `correlation_id`) |
| Unknown `(feature_id, correlation_id)` | Fresh build — pass through normal consumer path |

Additionally, the paused re-announcement (scenario Group D @edge-case
"Restarting Forge with a paused build in the history re-emits the
paused event") must also run at startup, even if JetStream does not
redeliver (paused builds' queue message is held unacked, so it WILL
redeliver — but the SQLite scan is a belt-and-braces check).

## Acceptance Criteria

- [ ] `reconcile_on_boot` runs exactly once at startup before normal message flow resumes
- [ ] Terminal-state redelivery → ack called, no new build started (idempotency)
- [ ] `INTERRUPTED`-marked rows transition to `PREPARING` on restart (retry-from-scratch)
- [ ] Paused builds re-emit `BuildPausedPayload` with the ORIGINAL `correlation_id` (Group D @edge-case)
- [ ] Paused builds re-emit `ApprovalRequestPayload` — "first response wins" semantics from ADR-ARCH-021 honoured
- [ ] Unknown `(feature_id, correlation_id)` → fresh build (no duplicate detection fires)
- [ ] Scenario test: "A redelivered build-queued message for a completed build is acknowledged idempotently" passes (Group E @data-integrity)
- [ ] Unit tests cover all four rule branches with mocked SQLite reader
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Implementation Notes

- Depends on FEAT-FORGE-001 providing a `builds` reader (`reconcile(feature_id, correlation_id) -> BuildStatus | None`); gate task start on that existing
- `reconcile_on_boot` can be synchronous over the redelivery queue because it runs at startup (no concurrency needed); normal operation is async per-message
- The paused re-announce uses `pipeline_publisher.publish_build_paused` from TASK-NFI-008

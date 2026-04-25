---
complexity: 6
created: 2026-04-24 00:00:00+00:00
dependencies:
- TASK-NFI-007
- TASK-NFI-008
feature_id: FEAT-FORGE-002
id: TASK-NFI-009
implementation_mode: task-work
parent_review: TASK-REV-NF20
priority: high
status: design_approved
tags:
- crash-recovery
- reconciliation
- pipeline
- idempotency
task_type: feature
test_results:
  coverage: null
  last_run: null
  status: pending
title: Implement pipeline_consumer.reconcile_on_boot (crash recovery + paused re-announce)
updated: 2026-04-24 00:00:00+00:00
wave: 4
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
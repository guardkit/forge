---
id: TASK-JNB-102
title: 'forge: emit build-cancelled on CANCELLED transitions (ASSUM-010 closure)'
status: in_review
created: 2026-07-03 15:30:00+00:00
updated: 2026-07-05 00:00:00+00:00
state_note: >-
  2026-07-05: implemented via interactive /task-work by the Fable forge-JNB
  session (backlog -> in_progress -> in_review same day). The
  autobuild_state block below is a stale false-green from 2026-07-04
  (turn approved with "Files actual: 0" - nothing was built then).
  Implementation notes: forge cancel now emits FOR REAL via the
  queue.publish sync one-shot pattern (row enrichment through the new
  SqliteLifecyclePersistence.get_build_row); gating emits are bound in
  make_gate_check_deps over the build context. Review findings + recorded
  follow-ups: docs/state/TASK-JNB-102/plan_audit.md.
priority: high
task_type: feature
parent_review: TASK-REV-C951
feature_id: FEAT-1872
version: v1.1
wave: 8
repo: forge
implementation_mode: task-work
complexity: 5
dependencies:
- TASK-JNB-101
tags:
- ubs-003
- jarvis-notification-bridge
- slack
- v1.1
autobuild_state:
  current_turn: 1
  max_turns: 5
  worktree_path: /Users/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-1872
  base_branch: main
  started_at: '2026-07-04T16:43:57.936440'
  last_updated: '2026-07-04T16:58:42.821654'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-07-04T16:43:57.936440'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# Task: forge: emit build-cancelled on CANCELLED transitions (ASSUM-010 closure)

## Description

Wire the existing `publish_build_cancelled` (`pipeline_publisher.py:272`) / `emit_cancelled` machinery onto the three CANCELLED transitions: the reject decision branch (`gating/wrappers.py:725-837`), the REASON_MAX_WAIT breach (`gating/wrappers.py:563-574`), and `CliSteeringHandler.handle_cancel` (defined in `src/forge/pipeline/cli_steering.py:642`, class at :590; invoked from `src/forge/cli/cancel.py:46`), setting `cancelled_by` from the responder/`decided_by`. Publishing is best-effort per DDR-007 — the SQLite transition happens first and an emission failure is WARNING-only, never blocking the transition. This task is sequenced after TASK-JNB-101 specifically to serialize edits to `gating/wrappers.py`, which both tasks touch.

This task is the v1.1 closure of ASSUM-010. The decision was split: the gap was accepted for v1 because the only live CANCELLED producer then was the operator's own forge CLI cancel — off the checkpoint path — and wiring forge would have broken the v1 "jarvis-only, zero forge changes" property. The jarvis bridge nonetheless implemented and unit-validated its build-cancelled handler from day one (TASK-JNB-005), so the phone path goes live the moment forge starts emitting. The calculus inverts at v1.1: a reject tapped on the phone (or the 3600s max-wait ceiling) transitions the build to CANCELLED in SQLite, and without this emit the operator who pressed Reject never receives terminal confirmation — an unacceptable open loop for a reply surface. On the jarvis side, `pipeline.build-cancelled.>` was added to the single ephemeral PIPELINE consumer's `filter_subjects` in TASK-JNB-005 (a filter change on the one consumer, verified unbound 2026-07-03), so no consumer work is needed here — this task is emission-only, on the forge side. The full loop (phone reject → phone cancelled notification) is live-validated in TASK-JNB-107.

## Acceptance Criteria

- [ ] Reject decision branch (`gating/wrappers.py:725-837`): an emitter spy observes exactly one `BuildCancelledPayload` when the reject path drives the CANCELLED transition, with `cancelled_by` set from the responder's `decided_by`, the correct cancellation `reason`, and the build's `correlation_id`.
- [ ] REASON_MAX_WAIT breach (`gating/wrappers.py:563-574`): an emitter spy observes exactly one `BuildCancelledPayload` when the 3600s max-wait ceiling drives the CANCELLED transition, with correct `cancelled_by`/`reason`/`correlation_id`.
- [ ] `CliSteeringHandler.handle_cancel` (defined in `src/forge/pipeline/cli_steering.py:642`, class at :590; invoked from `src/forge/cli/cancel.py:46`): an emitter spy observes exactly one `BuildCancelledPayload` per CLI cancel, with correct `cancelled_by`/`reason`/`correlation_id`.
- [ ] No emission on non-cancel outcomes: approve/override decisions, non-breaching waits, and error/degraded paths produce zero `BuildCancelledPayload` emissions.
- [ ] Best-effort ordering per DDR-007: the SQLite state transition completes first; a publish failure (e.g. NATS unavailable, publisher raises) is logged at WARNING and the transition still succeeds — no exception propagates, no rollback, no retry loop that blocks the caller.
- [ ] Existing lifecycle tests are unaffected — the pre-existing forge test suite passes without modification to its assertions.
- [ ] All modified files pass project-configured lint/format checks with zero errors.

## Test Requirements

Plain pytest only — NO pytest-bdd `.feature` glue (operator decision 2026-07-03; eliminates a known silent-false-green class). Test classes mirror the spec scenario names for the cancelled-emission scenarios. Run via `.venv/bin/python -m pytest` from the forge repo root.

- One test class per CANCELLED transition site (reject branch, REASON_MAX_WAIT breach, CLI `handle_cancel`), each asserting via an emitter spy/mock on `publish_build_cancelled` that exactly one `BuildCancelledPayload` is emitted with the expected `cancelled_by`, `reason`, and `correlation_id`.
- A negative class asserting zero emissions on approve/override outcomes and on non-breaching waits.
- A DDR-007 class asserting that when the publisher raises, the SQLite transition has already been recorded, the exception does not propagate to the caller, and a WARNING is logged (use `caplog`).
- Run the full existing suite to confirm the lifecycle tests remain green with no assertion changes.

## Implementation Notes

- Dependency: TASK-JNB-101 — "forge: ApprovalSubscriber production wiring into the serve runtime". It constructs the `ApprovalSubscriber` + `ApprovalSubscriberDeps` and injects them as the already-typed `ApprovalGateDeps.subscriber` (`gating/wrappers.py:396`), making the reject branch reachable in production for the first time. Both tasks edit `gating/wrappers.py`; this task is deliberately sequenced into wave 8 so the edits never race. Do not start until TASK-JNB-101 has landed.
- Reuse, do not reinvent: `publish_build_cancelled` already exists at `pipeline_publisher.py:272`. This task only adds call sites at the three transitions; the decision dispatch and validation chain from TASK-JNB-101 are consumed as-is.
- DDR-007 never-regress: the SQLite ledger is authoritative. Order of operations at every site is transition-then-publish; wrap the publish in a failure path that logs WARNING and continues. The emit must never raise into the transition caller.
- DDR-027 no-replay: consumer/state posture is in-memory on the jarvis side; forge must not add replay or redelivery machinery here — one emit per transition, fire-and-forget.
- Workqueue err-10100 single-consumer rule: `pipeline.build-cancelled.>` is consumed by the single existing jarvis ephemeral PIPELINE consumer (filter extended in TASK-JNB-005). This task must not create any NATS consumer — it is publish-only. Do not add subscriptions.
- Correlation-INDEPENDENT fan-out is deliberate on the jarvis side: the phone receives cancelled events for builds not queued through jarvis, so the emitted `correlation_id` must be the build's own, not conditioned on any jarvis session state.
- `cancelled_by` sourcing: reject branch uses the responder's `decided_by` from the validated `ApprovalResponsePayload`; REASON_MAX_WAIT and CLI cancel set an appropriate system/CLI identity per the existing payload conventions.
- Downstream consumers: TASK-JNB-106 (forge v1.1 scenario tests) depends on this task, and TASK-JNB-107 live-validates the phone reject → phone cancelled loop.

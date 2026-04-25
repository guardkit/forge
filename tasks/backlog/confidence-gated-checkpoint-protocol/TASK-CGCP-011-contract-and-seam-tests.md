---
id: TASK-CGCP-011
title: Contract and seam tests for the approval round-trip across NATS
task_type: testing
status: in_review
priority: high
created: 2026-04-25 00:00:00+00:00
updated: 2026-04-25 00:00:00+00:00
parent_review: TASK-REV-CG44
feature_id: FEAT-FORGE-004
wave: 5
implementation_mode: task-work
complexity: 5
dependencies:
- TASK-CGCP-010
- TASK-CGCP-008
- TASK-CGCP-009
tags:
- testing
- contract-tests
- seam-tests
- nats
- integration
- safety-critical
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-004
  base_branch: main
  started_at: '2026-04-25T18:43:53.521930'
  last_updated: '2026-04-25T19:02:18.638527'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-25T18:43:53.521930'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# Task: Contract and seam tests for the approval round-trip across NATS

## Description

Consolidate the integration-level test suite for FEAT-FORGE-004 into
`tests/integration/`. Per Context B testing depth (TDD) and Context A
trade-off priority (Quality), this task covers the seams that unit tests
in earlier waves cannot reach:

1. **End-to-end approval round-trip** (Group A `@key-example @smoke`):
   auto-approve, flag-for-review/pause/approve/resume, hard-stop, override,
   reject — using an in-memory NATS double and temp SQLite

2. **Two-layer constitutional regression** (Group E `@security @regression`,
   highest-stakes test): assert that disabling either layer alone (prompt
   `SAFETY_CONSTITUTION` removed in test harness, OR executor branch
   bypassed) still produces `MANDATORY_HUMAN_APPROVAL` for PR-review and
   PR-create stages — closes risk **R1**

3. **Durable-decision-on-publish-failure** (Group E `@data-integrity
   @regression`): inject a publish failure into `ApprovalPublisher`; assert
   the `GateDecision` SQLite row is present and the failure surfaces as
   an operational signal (not as caller exception) — closes risk **R6**

4. **Concurrency** (Group E `@concurrency`): two responses arriving for
   the same paused build at effectively the same moment resolve to exactly
   one decision; the second is recorded as a duplicate — closes risk **R4**

5. **Pause-and-publish atomicity** (Group E `@concurrency @data-integrity`):
   from any external observer, status query never reports paused-without-request

6. **Crash-recovery re-emission** (Group D `@regression`): simulate Forge
   restart with a paused build in SQLite; assert `ApprovalRequestPayload`
   is re-published with the **persisted** `request_id` (not a new one) —
   closes risk **R5**

7. **CLI synthetic injection** (Group D `@edge-case`): `forge cancel`
   produces CANCELLED with `reason="cli cancel"`; `forge skip` overrides
   current stage only with `reason="cli skip"`

8. **Per-build response routing** (Group D `@edge-case`): two paused
   builds — response routes only to its own build

9. **Unrecognised-responder handling** (Group E `@security`): response
   from non-expected approver does not resume the build; anomaly surfaced

## Acceptance Criteria

- [ ] `tests/integration/test_approval_round_trip.py` — Group A happy paths via in-memory NATS double + temp SQLite
- [ ] `tests/integration/test_constitutional_regression.py` — Group E E2 two-layer regression with selective-disable harness; both single-layer-disabled scenarios still produce `MANDATORY_HUMAN_APPROVAL`
- [ ] `tests/integration/test_durable_decision_on_publish_failure.py` — Group E `@data-integrity @regression`; SQLite row exists when publish raises
- [ ] `tests/integration/test_concurrent_responses.py` — Group E `@concurrency`; `asyncio.gather(send_a, send_b)` resolves to exactly one decision
- [ ] `tests/integration/test_pause_and_publish_atomicity.py` — Group E `@concurrency @data-integrity`; observer-side ordering test
- [ ] `tests/integration/test_crash_recovery_re_emit.py` — Group D `@regression`; persisted `request_id` reused on re-emit
- [ ] `tests/integration/test_cli_synthetic_responses.py` — Group D `@edge-case` cancel + skip
- [ ] `tests/integration/test_per_build_routing.py` — Group D `@edge-case`; two-build isolation
- [ ] `tests/integration/test_unrecognised_responder.py` — Group E `@security`; anomaly path
- [ ] `tests/integration/test_rehydration_guard.py` — TASK-CGCP-009's CI grep guard runs as a regular test
- [ ] All tests use clock injection (no `datetime.now()` / `time.sleep`); deterministic
- [ ] Coverage target: 80% line coverage for `forge.gating.*` and `forge.adapters.nats.approval_*`
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Implementation Notes

- Mirror the test infrastructure pattern from `tests/integration/` for FEAT-FORGE-002 contract + seam tests
- The two-layer regression test is the project's safety baseline — write it first (per TDD direction)
- The in-memory NATS double can be a small `dict[str, list[Msg]]` substituting `nats_core` async pub/sub primitives; the goal is contract-level coverage, not transport correctness
- Temp SQLite is a `:memory:` connection per test, schema applied from FEAT-FORGE-001's migration helper
- `pytest-asyncio` for async tests; no real network I/O

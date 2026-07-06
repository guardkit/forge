---
complexity: 5
dependencies:
- TASK-MP-004A
- TASK-MP-003
estimated_minutes: 55
feature_id: FEAT-3ED2
feature_ref: FEAT-SPL-002
id: TASK-MP-004B
implementation_mode: task-work
parent_review: TASK-REV-83E4
status: design_approved
tags:
- mode-p
- checkpoint
- df-009
task_type: feature
title: product_docs checkpoint flow (pause-before-wire, per-run approver, never auto-approve)
wave: 3
---

# TASK-MP-004B — product_docs checkpoint flow

## Description

Second half of the checkpoint (PS-004 split): the flow itself.
Reuses the D659 primitives — `derive_request_id` + the atomic pause-and-publish
helper (SQLite-before-wire, AGENTS-request-first envelope order) + a **per-run
ApprovalSubscriber pinned verbatim to the row's `expected_approver`** (the rearm
precedent, _serve_gate_activation.py:605-614). This resolves the red team's RT-01
critical: the static config-threaded approver plumbing would silently refuse
James. Implements the planning dispatch tail for approve/reject/late responses;
escalation and defer policy land in TASK-MP-005.

## BDD Scenarios

- "Completed product docs pause the run at the product docs checkpoint"
- "The product docs checkpoint never approves on its own"
- "Rejection at the checkpoint cancels the run without committing anything" (cancel half)
- "An approval from someone other than the expected approver is refused"
- "An approval response for a run that already ended is ignored"

## Files

- Creates: `src/forge/planning/checkpoint.py` (`checkpoint_product_docs(...)` + `SecondOpinionProvider` Protocol seam — consumed by TASK-MP-007; providers return DATA, structurally cannot return a decision)
- Tests: `tests/forge/planning/test_checkpoint.py`

## Acceptance Criteria

- [ ] request_id round-trips through `derive_request_id(plan_run_id, "product_docs", attempt)` and is invertible via `parse_request_id` (reuse gating/identity tests as the oracle)
- [ ] SQLite-before-wire: the store shows PAUSED + pending_approval_request_id BEFORE the fake publisher records the request envelope (call-order assertion); publish failure does NOT roll back the pause (rearm re-emits — DDR-007)
- [ ] No code path returns approved without an ApprovalResponse: a test with maximal coach evidence (coach_score=1.0) still pauses; an AST/grep predicate confirms the checkpoint module has no auto-approve branch (DF-009 v1 hard rule)
- [ ] Responder identity != the RUN ROW's expected_approver -> run stays PAUSED, WARNING logged (caplog predicate) — verbatim string equality, JNB-101/104 contract; the expected approver is read from the durable row, not from ApprovalConfig
- [ ] Response for a terminal-state run -> refused sentinel, row unchanged (late-response bounce)
- [ ] reject -> CANCELLED with the rejection recorded as the outcome in planning_run_events; zero terminal-handler invocations (recording fake)
- [ ] The approval request envelope carries the compressed PO output summary fields needed by jarvis rendering, built ONLY from validated components (no raw request_text interpolation — RT-09)
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Test Requirements

- Unit with fake publisher/subscriber over the MP-004A adapters + tmp_path SQLite;
  crib `tests/forge/gating/test_wrappers.py` fixture shapes.

## Implementation Notes

- Do NOT modify `wrappers.await_and_dispatch`/`_dispatch_response` — build policy,
  live JNB-107 dependency. The planning tail is planning-scoped.
- The wait/threshold loop is deliberately thin here; TASK-MP-005 owns the
  escalation policy and injects the clock.
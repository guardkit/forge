---
id: TASK-CGCP-004
title: 'Implement constitutional override branch in evaluate_gate (ADR-ARCH-026 belt-and-braces)'
task_type: feature
status: backlog
priority: high
created: 2026-04-25T00:00:00Z
updated: 2026-04-25T00:00:00Z
parent_review: TASK-REV-CG44
feature_id: FEAT-FORGE-004
wave: 2
implementation_mode: task-work
complexity: 5
dependencies:
- TASK-CGCP-001
tags:
- gating
- constitutional
- safety-critical
- adr-arch-026
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Task: Implement constitutional override branch in evaluate_gate (ADR-ARCH-026 belt-and-braces)

## Description

Implement the **first branch** of `forge.gating.evaluate_gate()` that
enforces the executor-layer half of the ADR-ARCH-026 belt-and-braces rule:
when `target_identifier in {"review_pr", "create_pr_after_review"}`,
return a `GateDecision` with `mode=GateMode.MANDATORY_HUMAN_APPROVAL` and
`auto_approve_override=True`, **regardless** of `coach_score`,
`detection_findings`, or `retrieved_priors`.

This is the highest-stakes test in the feature (risk **R1**: catastrophic
constitutional regression). Scenario E2 (`Group E @security @regression`)
demands that disabling either of the two layers in isolation still
produces `MANDATORY_HUMAN_APPROVAL` and surfaces the loss of the other
layer as a constitutional regression signal.

Files to create:

- `src/forge/gating/constitutional.py` — `_check_constitutional_override(target_identifier) -> GateDecision | None`
- Wire the call as the first branch of `evaluate_gate()` in `src/forge/gating/models.py` (or sibling)

The complementary prompt-layer rule (`SAFETY_CONSTITUTION` block in the
system prompt) is owned by the orchestrator's prompt module and is
**out of scope** for this task — but the regression test must verify that
disabling the prompt layer alone still produces the correct decision via
the executor branch.

## Acceptance Criteria

- [ ] `_check_constitutional_override(target_identifier: str) -> GateDecision | None` returns a fully-formed `GateDecision` for `target_identifier in {"review_pr", "create_pr_after_review"}` and `None` otherwise
- [ ] Returned `GateDecision` has `mode == GateMode.MANDATORY_HUMAN_APPROVAL`, `auto_approve_override is True`, `threshold_applied is None`, and a `rationale` that names the constitutional rule
- [ ] `evaluate_gate()` calls `_check_constitutional_override` as its **first** statement; if non-None, returns immediately without consulting `coach_score`, `detection_findings`, `retrieved_priors`, or `calibration_adjustments`
- [ ] Group A scenario "A pull-request-review stage always requires human approval regardless of evidence" passes against `coach_score=0.95`, `detection_findings=[]`, `retrieved_priors=[concurring]`
- [ ] Group C scenario "Creating a pull request after review is treated with the same constitutional rule" passes for `target_identifier="create_pr_after_review"`
- [ ] Group E `@security @regression` two-layer regression test: a pull-request-review stage produces `MANDATORY_HUMAN_APPROVAL` even when the prompt-layer `SAFETY_CONSTITUTION` is removed in the test harness (executor branch alone is sufficient)
- [ ] Group C `@negative` scenario "A mandatory-human-approval decision must not masquerade as a threshold-based approval" passes — `auto_approve_override=True` AND `threshold_applied is None` invariant holds
- [ ] Unit tests cover both target identifiers and at least three non-matching identifiers
- [ ] Module imports nothing from `nats_core`, `nats-py`, or `langgraph`
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Implementation Notes

- The matching set `{"review_pr", "create_pr_after_review"}` is a module-level constant — not inlined — so it can be referenced in tests
- The rationale string must clearly identify the rule: `"Constitutional override: pull-request review/creation always requires human approval (ADR-ARCH-026)"`
- Recommended TDD ordering (per Context B): write the two-layer regression test (E2) first, watch it fail, then implement the branch

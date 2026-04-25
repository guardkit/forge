---
id: TASK-CGCP-005
title: Implement reasoning-model assembly + post-condition checks in evaluate_gate
task_type: feature
status: in_review
priority: high
created: 2026-04-25 00:00:00+00:00
updated: 2026-04-25 00:00:00+00:00
parent_review: TASK-REV-CG44
feature_id: FEAT-FORGE-004
wave: 2
implementation_mode: task-work
complexity: 6
dependencies:
- TASK-CGCP-001
tags:
- gating
- reasoning-model
- pure
- domain-core
- adr-arch-019
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-004
  base_branch: main
  started_at: '2026-04-25T17:49:59.506510'
  last_updated: '2026-04-25T18:04:28.608551'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-25T17:49:59.506510'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# Task: Implement reasoning-model assembly + post-condition checks in evaluate_gate

## Description

Implement the body of `forge.gating.evaluate_gate()` that runs **after** the
constitutional override (TASK-CGCP-004). Per ADR-ARCH-019 and
`DM-gating.md §3`, there are **no static thresholds**. Thresholds emerge
from the priors via a reasoning-model invocation that is parameterised
into `evaluate_gate()` so the function remains pure (no I/O, no global
state, deterministic in tests).

Files to create:

- `src/forge/gating/reasoning.py`:
  - `_assemble_reasoning_prompt(...)` — builds the prompt from inputs
  - `_parse_model_response(raw_response) -> ParsedDecision` — validates structured output
  - `_enforce_post_conditions(decision, coach_score) -> None` — raises if degraded-mode invariant violated
  - `ReasoningModelCall` Protocol — `(prompt: str) -> str` callable
- Complete the `evaluate_gate()` body to call into these helpers and produce a `GateDecision`

The reasoning-model is **dependency-injected** — `evaluate_gate()` accepts
a `reasoning_model_call: ReasoningModelCall` parameter. Production code
binds the orchestrator's reasoning model; tests bind a deterministic
double.

## Acceptance Criteria

- [ ] `evaluate_gate()` accepts a `reasoning_model_call: ReasoningModelCall` parameter (Protocol or callable type) — keyword-only
- [ ] `evaluate_gate()` is a pure function: no `import nats`, no `import langgraph`, no module-level singletons, no `datetime.now()` in the hot path (clock injection if needed)
- [ ] `_assemble_reasoning_prompt(...)` produces a deterministic prompt for fixed inputs (snapshot test acceptable)
- [ ] `_parse_model_response(...)` validates the structured response against a Pydantic model and raises a clear error on malformed responses
- [ ] **Degraded-mode post-condition** (DM-gating §6): if `coach_score is None`, the resulting `mode` is in `{FLAG_FOR_REVIEW, HARD_STOP, MANDATORY_HUMAN_APPROVAL}`; violation raises a programmer error (do **not** silently coerce — closes risk **R3**)
- [ ] **Criterion-range invariant**: criterion scores in `[0.0, 1.0]` are accepted (Group B `Scenario Outline`); values outside this range are refused with a validation error and **no decision is recorded** (Group B `@negative`)
- [ ] **Critical-finding escalation**: a `DetectionFinding` with `severity="critical"` cannot result in `AUTO_APPROVE` (Group C `@negative`)
- [ ] Group A "auto-approve happy path" passes via the deterministic test double for the reasoning model
- [ ] Group A "ambiguous evidence → flag-for-review" passes via the test double
- [ ] Group A "strongly negative → hard-stop" passes via the test double
- [ ] Group A "every gate decision records its rationale, priors, and findings" passes — `GateDecision` carries `rationale`, `evidence`, `detection_findings`, `decided_at`
- [ ] Module imports nothing from `nats_core`, `nats-py`, or `langgraph`
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Implementation Notes

- The reasoning-model double for tests is a small `(prompt: str) -> str` callable that returns hard-coded JSON for each scenario; a single fixture file is sufficient
- Test the invariants individually (post-condition, criterion-range) so failures are localised
- Use Pydantic to parse the structured response — the model's structured output schema is a private detail of `reasoning.py`
- TDD ordering (per Context B): write the degraded-mode post-condition test first, then the criterion-range test, then the happy-path scenarios

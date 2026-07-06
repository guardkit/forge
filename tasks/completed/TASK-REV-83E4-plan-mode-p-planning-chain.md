---
id: TASK-REV-83E4
title: "Plan: Mode P Planning Chain"
task_type: review
status: completed
created: 2026-07-06T10:03:20Z
updated: 2026-07-06T10:03:20Z
priority: high
tags: [planning, mode-p, feat-spl-002, sovereign-planning-loop]
complexity: 0
decision_required: true
feature_ref: FEAT-SPL-002
clarification:
  context_a:
    timestamp: 2026-07-06T10:03:20Z
    mode: autonomous-defaults
    decisions:
      focus: all
      depth: standard
      tradeoff: quality
    note: >
      Autonomous Fable session (ACTION 7); no operator present. Defaults chosen
      to match the FEAT-EVAL-IDEA / QAV planning precedent (all aspects, quality-first).
review_results:
  mode: decision
  depth: standard
  score: 82
  findings_count: 18
  recommendations_count: 11
  decision: implement
  feature_id: FEAT-3ED2
  report_path: .claude/reviews/TASK-REV-83E4-review-report.md
  completed_at: 2026-07-06T12:05:00Z
clarification_context_b:
  timestamp: 2026-07-06T12:05:00Z
  mode: autonomous-defaults
  decisions:
    approach: panel-recommended (separate planning lifecycle)
    execution: auto-detect (6 waves from dependency graph)
    testing: standard (quality gates)
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Task: Plan: Mode P Planning Chain

## Description

Decision review for FEAT-SPL-002 (forge Mode P planning chain) ahead of task
breakdown and autobuild. The BDD spec exists (`features/mode-p-planning-chain/`,
29 scenarios, 16 deferred low-confidence assumptions) and the current state of
every touched seam was re-verified 2026-07-06 (7-agent sweep; digest reproduced
in the spec summary). The review must produce an implementation approach and a
task/wave breakdown suitable for `guardkit autobuild feature`.

## Review Scope

- Architecture fit: planning-run persistence (new table vs builds-row reuse,
  ASSUM-001), checkpoint composition with the TASK-GATE-D659 gate machinery
  (ASSUM-002), ack-on-persist vs held-slot intake (ASSUM-015).
- Boundary discipline: Mode P as a distinct mode; MODE_B forbidden-stage logic
  untouched; guardkit seam `adapters/guardkit/run.py` untouched.
- Production wiring: serve-side composition so Mode P actually runs in the live
  daemon (the mode-planner and specialist-dispatch composition gaps found in the
  2026-07-06 verification).
- Config surface: new planning section (ApprovalConfig is closed); DF-004
  fallbacks:[] audit; DF-006 frontier second-opinion gating.
- Risks: schema migration (builds.mode CHECK) if the BuildMode-enum route is
  taken; PIPELINE stream single-consumer rule for the second durable.

## Acceptance Criteria

- [ ] Technical options analysed with a recommended approach and rationale
- [ ] Task breakdown with dependencies, waves, complexity scores
- [ ] Every task mapped to the BDD scenarios it satisfies
- [ ] Assumption manifest cross-referenced (deviations from ASSUM-* recorded)

## Context

- Spec: `features/mode-p-planning-chain/` (29 scenarios; summary carries the
  verified-state digest)
- Fleet decisions: DF-009 (Accepted 2026-07-05), DF-007, DF-004, DF-006, DF-001
- SPL scope/build plan: `../ai-transition/docs/sovereign-planning-loop-*.md`

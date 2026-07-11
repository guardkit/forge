---
id: TASK-REV-FW10
title: "Plan: Wire the production pipeline orchestrator into forge serve"
status: review_complete
created: 2026-05-02T00:00:00Z
updated: 2026-05-02T00:00:00Z
review_results:
  mode: decision
  depth: standard
  score: 86
  findings_count: 7
  recommendations_count: 11
  decision: pending_user_choice
  report_path: .claude/reviews/TASK-REV-FW10-review-report.md
  completed_at: 2026-05-02T00:00:00Z
priority: high
task_type: review
tags: [feature-plan, forge-serve, orchestrator, autobuild, lifecycle-emitter, deepagents-async-subagent]
complexity: 8
feature_id: FEAT-FORGE-010
context_files:
  - features/forge-serve-orchestrator-wiring/forge-serve-orchestrator-wiring_summary.md
  - features/forge-serve-orchestrator-wiring/forge-serve-orchestrator-wiring.feature
  - features/forge-serve-orchestrator-wiring/forge-serve-orchestrator-wiring_assumptions.yaml
  - docs/research/forge-orchestrator-wiring-gap.md
  - docs/design/decisions/DDR-007-pipeline-lifecycle-emitter-wiring-path.md
  - docs/design/contracts/API-nats-pipeline-events.md
  - docs/architecture/decisions/ADR-ARCH-014-single-consumer-max-ack-pending.md
  - docs/architecture/decisions/ADR-ARCH-031-async-subagents-for-long-running-work.md
  - docs/architecture/decisions/ADR-ARCH-021-paused-via-langgraph-interrupt.md
  - docs/architecture/decisions/ADR-ARCH-008-forge-produces-own-history.md
  - docs/architecture/decisions/ADR-ARCH-027-no-horizontal-scaling.md
  - docs/design/decisions/DDR-006-async-subagent-state-channel-contract.md
  - docs/design/decisions/DDR-001-reply-subject-correlation.md
  - docs/state/TASK-FORGE-FRR-001/implementation_plan.md
  - features/mode-a-greenfield-end-to-end/mode-a-greenfield-end-to-end_summary.md
  - features/forge-production-image/forge-production-image.feature
upstream_dependencies:
  - FEAT-FORGE-001
  - FEAT-FORGE-002
  - FEAT-FORGE-004
  - FEAT-FORGE-007
  - FEAT-FORGE-009
supersedes_tasks:
  - TASK-FORGE-FRR-001
  - TASK-FORGE-FRR-001b
clarification:
  context_a:
    timestamp: 2026-05-02T00:00:00Z
    decisions:
      focus: task_decomposition_and_risk
      depth: standard
      tradeoff: quality
      pause_resume_scope: in_scope_per_DDR_007
      subagent_split: five_tasks_one_wave
      testing_depth: standard_with_seam_tests
test_results:
  status: pending
  coverage: null
  last_run: null
provenance:
  origin_correlation_id: a58ec9a7-27c6-485a-beac-e18675639a10
  origin_event: FEAT-JARVIS-INTERNAL-001 first-real-run on GB10 (2026-05-01)
  gap_finding: docs/research/forge-orchestrator-wiring-gap.md
---

# TASK-REV-FW10 — Plan: Wire the production pipeline orchestrator into forge serve

This task is the planning artifact for **FEAT-FORGE-010** (slug
`forge-serve-orchestrator-wiring`). The /feature-spec session produced the
canonical spec (31 scenarios, 18 confirmed assumptions, DDR-007 settling
the pause/resume + emitter-wiring path). This review task captures the
decision-mode analysis that turns that spec into an executable wave plan.

The full review is at `.claude/reviews/TASK-REV-FW10-review-report.md`.
This task file carries the metadata, decision provenance, and the
acceptance set that the implementation tasks will inherit.

---
id: TASK-REV-C951
title: "Plan: Jarvis Notification Bridge"
status: completed
created: 2026-07-03T14:05:00Z
updated: 2026-07-03T16:10:00Z
review_results:
  mode: decision
  depth: standard
  decision: implement
  options_scored: 3
  winning_score: 88
  findings_count: 11
  recommendations_count: 16
  report_path: .claude/reviews/TASK-REV-C951-review-report.md
  feature_yaml: jarvis:.guardkit/features/FEAT-28FF.yaml (v1; v1.1 YAMLs deferred until TASK-JNB-004 passes)
  completed_at: 2026-07-03T16:10:00Z
priority: high
task_type: review
tags: [feature-planning, ubs-003, jarvis, slack, notifications]
complexity: 6
decision_required: true
clarification:
  context_a:
    timestamp: 2026-07-03T14:05:00Z
    mode: derived_from_session_context
    decisions:
      focus: architecture
      tradeoff: quality_maintainability
      concerns:
        - "Slack path must extend ForgeNotificationsSubscriber in-process; no second consumer on PIPELINE subjects (workqueue err 10100)"
        - "Explicit decision on ASSUM-010: wire forge-side build-cancelled emit, or accept pause-notification-is-last-signal for v1.1 reject/max-wait"
        - "ApprovalSubscriber production wiring absent (no mark_resume_pending call sites) — in-scope for v1.1 or separate prerequisite?"
        - "Reply-path authorization: Socket Mode member-id allowlist is the sole gate protecting resume (2 @security scenarios)"
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Task: Plan: Jarvis Notification Bridge

## Description

Feature-planning review for **FEAT-UBS-003** (Jarvis Notification Bridge):
bridge forge pipeline lifecycle events (NATS JetStream) to the operator's
phone via **Slack**, per the revised BDD spec at
`features/jarvis-notification-bridge/` (31 scenarios, parser-validated,
11 assumptions resolved).

- **v1 (checkpoint)**: one-way full-lifecycle notifications — queued
  (jarvis-intake publish-time), running, terminal states, approval pauses —
  by extending jarvis's existing `ForgeNotificationsSubscriber`. Must
  live-validate (toy feature from Open WebUI → phone sees queued → running →
  terminal) before any v1.1 work.
- **v1.1 (gated on v1)**: approve/reject via Slack Socket Mode interactive
  buttons → `ApprovalResponsePayload` → forge approval subscriber resume path.

Context files:
- `features/jarvis-notification-bridge/jarvis-notification-bridge_summary.md`
  (primary — includes source-verification notes and version split)
- `docs/research/ideas/unattended-build-service-build-plan.md` (Step 2)
- `docs/research/ideas/unattended-build-service-scope.md` (§4 FEAT-UBS-003)

Repos: **jarvis** (subscriber extension + Slack adapter + intake queued
notification) and **forge** (v1.1 approval subscriber production wiring).
Slack app "Jarvis Forge Bridge" created + installed; all four
`JARVIS_SLACK_*` env values configured in `jarvis/.env` (bot invite to
#forge-builds pending re-test).

## Acceptance Criteria

- [ ] Technical options analysis for the v1 subscriber extension + Slack
      delivery surface (respecting the workqueue single-consumer constraint)
- [ ] Explicit handling of the four Context A concerns (frontmatter)
- [ ] v1/v1.1 sequencing preserved: v1 live-validation gates v1.1 tasks
- [ ] Task breakdown with dependencies, waves, complexity scores
- [ ] Recommended approach with rationale

## Implementation Notes

Review scope clarification (Context A) derived from session decisions —
see frontmatter. Graphiti pre-planning context loaded (unified messaging
layer conventions; jarvis pytest-bdd missing-step-def-glue false-approval
warning; pipeline-state KV bucket as secondary status channel).

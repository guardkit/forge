---
id: TASK-REV-CG44
title: "Plan: Confidence-Gated Checkpoint Protocol"
task_type: review
status: review_complete
priority: high
created: 2026-04-25T00:00:00Z
updated: 2026-04-25T00:00:00Z
complexity: 8
tags: [planning, review, checkpoint, gating, approval, constitutional, feat-forge-004]
feature_spec: features/confidence-gated-checkpoint-protocol/confidence-gated-checkpoint-protocol_summary.md
feature_id: FEAT-FORGE-004
upstream_dependencies:
  - FEAT-FORGE-001  # Pipeline State Machine & Configuration
  - FEAT-FORGE-002  # NATS Fleet Integration
  - FEAT-FORGE-003  # Specialist Agent Delegation
clarification:
  context_a:
    timestamp: 2026-04-25T00:00:00Z
    decisions:
      focus: all
      tradeoff: quality
      specific_concerns:
        - constitutional_guarantees
        - nats_integration
        - degraded_mode
        - idempotency
      upstream_dependency_depth: medium
      scenario_coverage_check: yes
review_results:
  mode: decision
  depth: standard
  recommended_option: "Option 1 — Pure-domain forge.gating with thin NATS approval adapter"
  estimated_hours: "22-28"
  subtask_count: 12
  wave_count: 5
  aggregate_complexity: 8
  findings_count: 12
  risks_count: 10
  integration_contracts_count: 5
  report_path: .claude/reviews/TASK-REV-CG44-review-report.md
  completed_at: 2026-04-25T00:00:00Z
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Task: Plan Confidence-Gated Checkpoint Protocol (FEAT-FORGE-004)

## Description

Decision-making review for **FEAT-FORGE-004 — Confidence-Gated Checkpoint Protocol**.
The feature specifies how each gated pipeline stage is evaluated against Coach
scores, detection findings, retrieved priors, and Rich-approved calibration
adjustments to produce one of four gate modes: **auto-approve**, **flag-for-review**
(paused state awaiting Rich), **hard-stop**, or **mandatory human approval**.

Covers the approval request/response round-trip across the build-specific approval
channel (idempotent on request identifier, bounded-wait with refresh, consistent
with the paused-state transition), the constitutional belt-and-braces rule that
forces PR-review and PR-create stages to mandatory human approval regardless of
score, degraded-mode behaviour when specialist scoring is unavailable, the
resume-value rehydration contract that hides direct-invoke vs. server-mode serde
differences from callers, and CLI steering (`forge cancel` → synthetic reject,
`forge skip` → synthetic override). Decisions are written durably even when
downstream notification publishes fail, and each decision records the rationale,
priors consulted, and findings considered.

The review must surface the recommended technical approach, architecture
boundaries against the three upstream features, risk analysis (especially around
constitutional safety and idempotency), effort estimation, and a subtask breakdown
that downstream `/feature-build` can execute against.

## Scope of Analysis

Review must cover **all areas (full sweep)** with a **quality** trade-off priority
(safety-critical protocol — correctness and robustness preferred over speed of
delivery). Specific concerns to receive extra scrutiny: **constitutional
guarantees**, **NATS integration**, **degraded-mode behaviour**, and
**idempotency**. Upstream dependency examination at **medium** depth (verify seam
contracts; do not re-review upstream internals). Include **scenario coverage
assessment** of the 32 BDD scenarios.

Concrete areas to examine:

1. **Constitutional guarantees**: belt-and-braces enforcement of mandatory human
   approval on `review_pr` and `create_pr_after_review` regardless of Coach score;
   two-layer guard (gate logic + override list); resilience to score-based bypass
   attempts.
2. **Approval channel transport**: build-specific subject layout
   (`agents.approval.forge.{build_id}` and `.response` mirror); per-build response
   routing; subject isolation; ride atop FEAT-FORGE-002 fleet message bus.
3. **Idempotency & dedup**: first-response-wins semantics; short-TTL dedup set on
   request identifier; duplicate-response handling; CLI synthetic decisions
   (cancel → reject, skip → override) flowing through same idempotency boundary.
4. **Degraded mode**: behaviour when Coach/specialist scoring is unavailable
   (FEAT-FORGE-003 absent or failed); degraded-mode marker on decisions; gate-mode
   selection rules without a confidence score.
5. **Bounded-wait & refresh**: default 300s initial wait; max 3600s ceiling via
   `forge.yaml.approval.max_wait_seconds`; refresh-within-max-wait semantics;
   max-wait-ceiling fallback (deferred to forge-pipeline-config per ASSUM-003).
6. **Resume-value rehydration**: typed-vs-dict contract hiding direct-invoke vs.
   server-mode serde differences; rehydration must be transparent to gate
   evaluators.
7. **Durable decision recording**: decisions written even when downstream publish
   fails; rationale, priors consulted, and findings considered all captured;
   crash-recovery re-emission of approval requests for paused builds.
8. **State-machine integration**: paused-state transition consistency with
   pause-and-publish; FEAT-FORGE-001 SQLite substrate carries decision history;
   gate evaluation must not couple state transitions to transport availability.
9. **Security**: unrecognised responder rejection; expected approver identity
   per-deployment (e.g. `rich`/Jarvis adapter id); allowlist semantics implied by
   constitutional framing.
10. **Test strategy & scenario coverage**: assess whether the 32 BDD scenarios
    (8 key, 5 boundary, 7 negative, 10 edge, 4 smoke, 4 regression, 2 security,
    2 concurrency, 2 data-integrity, 1 integration) cover all four gate modes,
    constitutional override paths, degraded-mode marker, and idempotency under
    concurrent responses; identify any gaps.

## Acceptance Criteria

- [ ] Technical options analysed with pros/cons and a recommended approach
- [ ] Architecture boundary between gating logic and FEAT-FORGE-001/002/003 documented
- [ ] Constitutional guarantee design (two-layer guard) explicitly justified
- [ ] Idempotency and dedup mechanism specified at the contract boundary
- [ ] Degraded-mode behaviour rules documented (gate-mode selection without score)
- [ ] Effort estimated with complexity score (1–10) per proposed subtask
- [ ] Risk register produced covering safety bypass, transport failures, race conditions
- [ ] Subtask breakdown with dependencies and parallel-wave organisation
- [ ] Integration contracts identified (producer/consumer artifact handshakes)
- [ ] BDD scenario coverage assessed against gate modes and edge cases
- [ ] Decision checkpoint presented: [A]ccept / [R]evise / [I]mplement / [C]ancel

## Clarification Context

**Context A — Review Scope** (captured 2026-04-25):

- Review focus: **All areas (full sweep)**
- Trade-off priority: **Quality** (safety-critical)
- Specific concerns: **constitutional guarantees, NATS integration, degraded mode, idempotency**
- Upstream dependency depth: **Medium** (verify seam contracts)
- Scenario coverage check: **Yes** (assess 32 BDD scenarios)

## Context Files

- `features/confidence-gated-checkpoint-protocol/confidence-gated-checkpoint-protocol_summary.md`
- `features/confidence-gated-checkpoint-protocol/confidence-gated-checkpoint-protocol.feature` (32 scenarios)
- `features/confidence-gated-checkpoint-protocol/confidence-gated-checkpoint-protocol_assumptions.yaml` (7 assumptions: 5 high / 2 medium)
- `docs/design/contracts/API-nats-approval-protocol.md`
- `docs/design/models/DM-gating.md`

## Upstream Dependencies

- **FEAT-FORGE-003** — Specialist Agent Delegation (consumes Coach scores, criterion breakdowns, detection findings)
- **FEAT-FORGE-002** — NATS Fleet Integration (approval channel rides on fleet message bus)
- **FEAT-FORGE-001** — Pipeline State Machine & Configuration (paused-state, crash recovery, SQLite substrate)

## Next Steps

```bash
/task-review TASK-REV-CG44 --mode=decision --depth=standard
```

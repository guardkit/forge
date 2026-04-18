---
id: TASK-REV-D90D
title: Review docs for outdated ideas and implementation gaps
status: completed
created: 2026-04-16T00:00:00Z
updated: 2026-04-16T00:00:00Z
priority: high
tags: [documentation, review, architecture-review, implementation-readiness]
complexity: 5
task_type: review
decision_required: true
review_mode: architectural
review_depth: standard
review_results:
  score: 82
  findings_count: 8
  recommendations_count: 8
  decision: cleanup
  report_path: .claude/reviews/TASK-REV-D90D-review-report.md
  completed_at: 2026-04-16T00:00:00Z
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Task: Review docs for outdated ideas and implementation gaps

## Description

Comprehensive review of all documentation under `docs/research/ideas/` and related docs to identify outdated concepts, stale references, and gaps that would block implementation. The goal is to ensure all docs are valid, consistent with the current architecture, and ready to drive implementation.

## Scope

### Primary Documents (Deep Review)
- `docs/research/ideas/forge-build-plan.md` — Validate prerequisites, build steps, and readiness for `/system-arch`
- `docs/research/ideas/fleet-master-index.md` — Validate fleet index, agent inventory, decision log, and cross-repo references

### Secondary Documents (Scan for Consistency)
- `docs/research/ideas/forge-pipeline-orchestrator-refresh.md`
- `docs/research/ideas/architect-agent-finproxy-build-plan.md`
- `docs/research/ideas/big-picture-vision-and-durability.md`
- `docs/research/ideas/conversation-starter-gap-analysis.md`
- `docs/research/ideas/forge-ideas-overhaul-conversation-starter.md`
- `docs/research/ideas/TASK-update-build-plan-da15.md`
- `docs/research/ideas/TASK-update-fleet-index-d22.md`
- `docs/research/forge-pipeline-architecture.md` (anchor doc — reference for correctness)
- `docs/research/forge-build-plan-alignment-review.md` (prior alignment review — check findings still hold)
- `docs/product/roadmap.md`

## Known Issues to Check

### 1. Stale Concepts — Must Be Removed
- **`ready-for-dev` events** — Replaced by `pipeline.build-queued` via CLI → JetStream (ADR-SP-014 Pattern A)
- **PM Adapter / Linear webhooks** — Removed; build triggers are now CLI-driven
- **Kanban board triggers** — No longer part of the architecture
- **`ReadyForDevPayload`** — Replaced by `BuildQueuedPayload`
- **`FeaturePlannedPayload`** — Flagged as last loose end in prior alignment review
- **`feature-planned` / `ticket-updated` events** — Removed concepts

### 2. NATS JetStream — Must Be Canonical
- All messaging references should use NATS JetStream (now available and configured)
- No references to "future" or "planned" NATS availability — it's live
- Verify stream/subject naming consistency across docs
- Confirm JetStream KV references (e.g., `agent-registry`) are consistent

### 3. Implementation Readiness Gaps
- Are all prerequisites in forge-build-plan actually met?
- Are dependency chains (nats-core, nats-infrastructure, specialist-agent Phase 3) accurately reflected?
- Are there missing build steps or unclear ordering?
- Do the docs reference tools/agents that don't exist yet without flagging them as TODO?

### 4. Cross-Document Consistency
- Version numbers and dates consistent across docs
- Decision log entries (D33–D38 etc.) referenced correctly
- ADR references (ADR-SP-014, etc.) consistent
- Agent names and repo references match reality

## Acceptance Criteria

- [ ] No references to `ready-for-dev` events remain in any ideas doc
- [ ] No references to PM Adapter / Linear webhook triggers remain
- [ ] All NATS references use JetStream as current/available (not future/planned)
- [ ] `forge-build-plan.md` prerequisites accurately reflect current state
- [ ] `fleet-master-index.md` agent inventory and decision log are current
- [ ] Cross-document references (versions, ADRs, decisions) are consistent
- [ ] Implementation blockers and gaps documented with clear next actions
- [ ] Review report produced with findings, recommendations, and priority ordering

## Output

A structured review report containing:
1. **Stale references found** — file, line, what to change
2. **NATS/JetStream inconsistencies** — what needs updating
3. **Implementation readiness assessment** — per-document go/no-go
4. **Gaps and missing content** — what needs to be written or clarified
5. **Recommended fix priority** — ordered list of changes

## Implementation Notes

This is a review task. Use `/task-review TASK-REV-D90D` to execute the analysis.
Findings may spawn follow-up implementation tasks via `/task-create`.

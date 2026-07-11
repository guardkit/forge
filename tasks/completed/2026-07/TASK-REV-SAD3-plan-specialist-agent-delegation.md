---
id: TASK-REV-SAD3
title: "Plan: Specialist Agent Delegation"
task_type: review
status: review_complete
priority: high
created: 2026-04-25T00:00:00Z
updated: 2026-04-25T00:00:00Z
complexity: 8
tags: [planning, review, dispatch, delegation, capability-routing, nats, feat-forge-003]
feature_spec: features/specialist-agent-delegation/specialist-agent-delegation_summary.md
feature_id: FEAT-FORGE-003
upstream_dependencies:
  - FEAT-FORGE-001  # Pipeline State Machine & Configuration
  - FEAT-FORGE-002  # NATS Fleet Integration
clarification:
  context_a:
    timestamp: 2026-04-25T00:00:00Z
    decisions:
      focus: all
      tradeoff: quality
      specific_concerns:
        - correlation_correctness
        - security_invariants
        - retry_semantics
        - timeout_behaviour
review_results:
  mode: decision
  depth: standard
  recommended_option: "Option 1 — Pure-domain forge.dispatch package with thin NATS adapter"
  estimated_hours: "26-34"
  subtask_count: 12
  wave_count: 5
  aggregate_complexity: 8
  findings_count: 10
  risks_count: 11
  integration_contracts_count: 5
  scenario_coverage: 33/33
  report_path: .claude/reviews/TASK-REV-SAD3-review-report.md
  completed_at: 2026-04-25T00:00:00Z
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Task: Plan Specialist Agent Delegation (FEAT-FORGE-003)

## Description

Decision-making review for **FEAT-FORGE-003 — Specialist Agent Delegation**. The
feature specifies Forge's single capability-driven dispatch path: resolve a target
specialist via the live discovery cache (exact-tool match → intent-pattern fallback
at minimum confidence → tie-break by trust tier, confidence, queue depth), publish
the command on the fleet bus, and correlate the reply on a correlation-keyed
channel established **before** publish.

Covers the LES1 parity rule (PubAck-is-not-success), the local hard-timeout cut-off
(900s default), result parsing (Coach score, criterion breakdown, detection findings
— top-level preferred, nested fallback), the degraded path when no specialist is
resolvable, the reasoning-model-driven retry with additional context on soft
failure, outcome correlation back onto the resolution record, async-mode
run-identifier polling, and invariants around snapshot stability, reply-source
authenticity, and exactly-once reply handling.

The review must surface the recommended technical approach, architecture boundaries
against FEAT-FORGE-001 (state machine substrate) and FEAT-FORGE-002 (fleet cache,
bus, lifecycle subscription), risk analysis (especially correlation correctness,
security invariants, retry semantics, and timeout behaviour), effort estimation,
and a subtask breakdown that downstream `/feature-build` can execute against.

## Scope of Analysis

Review must cover **all areas (full sweep)** with a **quality** trade-off priority
(distributed messaging with strong correctness invariants — robustness preferred
over speed of delivery).

Specific concerns to receive extra scrutiny:

1. **Correlation correctness** — subscribe-before-publish ordering invariant,
   per-correlation subject names, wrong-correlation reply filtering, exactly-once
   reply handling, unsubscribe-on-timeout semantics.
2. **Security invariants** — reply-source authenticity (replies must come from the
   resolved specialist), sensitive-parameter hygiene in command envelopes,
   trust-tier supremacy (core > specialist > extension regardless of other
   tie-break factors).
3. **Retry semantics** — reasoning-model-driven retry (no fixed max-retry count at
   dispatch layer), additional-context propagation, fresh correlation per attempt,
   retry-attempt recording alongside original attempt, soft-failure detection.
4. **Timeout behaviour** — advisory specialist-side 600s vs. Forge-side hard 900s
   cut-off, synthetic timeout result fed to reasoning loop, late-reply handling
   (ignored after timeout fires), unsubscribe-on-timeout cleanup.

Concrete areas to examine:

1. **Capability resolution** — exact-tool match priority, intent-pattern fallback
   at 0.7 minimum confidence, tie-break ordering (trust tier → confidence → queue
   depth), in-flight snapshot stability during cache updates, concurrent-resolution
   determinism, cache-freshness on join, cache-invalidation on deregister, registry
   outage fallback.
2. **Dispatch transport** — subscribe-before-publish invariant, correlation-keyed
   channel naming, write-before-send invariant for resolution record persistence,
   PubAck-not-success parity (LES1), bus disconnect handling, concurrent dispatches
   to the same agent.
3. **Result parsing** — Coach top-level fields preferred over nested result fields,
   missing Coach score fallback (FLAG_FOR_REVIEW per ASSUM-006), malformed envelope
   handling, criterion breakdown and detection-findings extraction.
4. **Retry & degraded paths** — reasoning-model retry with fresh correlation,
   degraded-status exclusion (specialists in degraded state are excluded from
   resolution candidates), unresolved-capability outcome surface, specialist-error
   outcome surface.
5. **Async-mode polling** — run-identifier correlation, polling cadence,
   convergence with synchronous-reply path, idempotency on duplicate replies.
6. **Outcome correlation** — resolution record linked back to gate decision,
   resolution record marked as outcome-correlated, durability of resolution record
   even when downstream notification publishes fail.
7. **Upstream seam contracts** —
   - **FEAT-FORGE-001**: SQLite-backed durable substrate for `CapabilityResolution`
     records and dispatch outcomes; what schema fields the resolution layer reads
     and writes; transaction boundaries.
   - **FEAT-FORGE-002**: live fleet cache (read), fleet-lifecycle subscription
     (read), pipeline-event publishing (write), heartbeat view (read),
     degraded-cache propagation; what dispatch-time snapshot semantics are required
     and which are inherited.

## Acceptance Criteria

- [ ] Review identifies 2-3 technical options for the specialist delegation layer,
      with explicit trade-offs.
- [ ] Recommended option is justified against the **quality** trade-off priority.
- [ ] All four specific-concern areas (correlation, security, retry, timeout) are
      addressed in the recommendation.
- [ ] Architecture boundaries against FEAT-FORGE-001 and FEAT-FORGE-002 are made
      explicit (what is read, what is written, what is reused, what is new).
- [ ] All 33 BDD scenarios are mapped to at least one task in the breakdown
      (scenario coverage check).
- [ ] Risk analysis covers: correlation race conditions, security invariant
      bypass, retry-induced duplicate work, timeout/late-reply contention,
      cache-staleness during in-flight resolution, registry outage during
      resolution.
- [ ] Effort estimation provided in hours with assumed solo-developer pace.
- [ ] Subtask breakdown produces a feature-build-compatible plan with parallel
      execution waves and explicit task dependencies.
- [ ] Integration contracts (§4) are specified for every cross-task data
      dependency with format constraints.
- [ ] Mandatory diagrams generated in IMPLEMENTATION-GUIDE.md (Data Flow,
      Integration Contract for complexity ≥ 5, Task Dependency Graph for ≥ 3
      tasks).

## Test Requirements

- [ ] Review report is internally consistent (no contradictions between findings,
      recommendation, and subtasks).
- [ ] All assumptions from `specialist-agent-delegation_assumptions.yaml` are
      acknowledged in the review (intent-fallback 0.7, advisory 600s, hard 900s,
      cache TTL 30s, reasoning-model-driven retry, FLAG_FOR_REVIEW fallback).
- [ ] Each generated subtask has a non-default `task_type` field (no implicit
      `feature` defaults for setup/scaffolding work).
- [ ] Each generated implementation/refactor subtask carries a lint-compliance
      acceptance criterion.

## Implementation Notes

This is a **decision-making review task**. No production code is written by
this task — its output is a structured review report at
`.claude/reviews/TASK-REV-SAD3-review-report.md`, a feature folder
`tasks/backlog/specialist-agent-delegation/` with subtask markdown files, and a
structured feature YAML at `.guardkit/features/FEAT-FORGE-003.yaml`.

The review should explicitly distinguish between behaviour the spec describes
in domain terms (capability resolution, correlation, snapshot stability,
exactly-once reply handling) and the transport primitives the implementation
will use (JetStream audit interception, subscribe-then-publish ordering,
per-correlation subject names). The spec deliberately keeps these separate
and the implementation plan should preserve that separation.

Upstream dependency examination should be at **medium depth**: verify the seam
contracts against FEAT-FORGE-001 and FEAT-FORGE-002 (what schema is read/written,
what bus subjects are subscribed/published, what cache snapshots are required),
but do not re-review the upstream features' internal correctness.

## Test Execution Log

[Automatically populated by /task-review]

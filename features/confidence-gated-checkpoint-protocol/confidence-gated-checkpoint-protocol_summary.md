# Feature Spec Summary: Confidence-Gated Checkpoint Protocol

**Feature ID**: FEAT-FORGE-004
**Stack**: python
**Generated**: 2026-04-24T00:00:00Z
**Scenarios**: 32 total (4 smoke, 4 regression)
**Assumptions**: 7 total (5 high / 2 medium / 0 low confidence)
**Review required**: No — all assumptions traceable to supplied context files

## Scope

Specifies Forge's confidence-gated checkpoint protocol: how each gated pipeline stage is
evaluated against Coach scores, detection findings, retrieved priors, and Rich-approved
calibration adjustments to produce one of four gate modes — auto-approve, flag-for-review
(paused state awaiting Rich), hard-stop, or mandatory human approval. Covers the
approval request/response round-trip across the build-specific approval channel
(idempotent on request identifier, bounded-wait with refresh, consistent with the
paused-state transition), the constitutional belt-and-braces rule that forces PR-review
and PR-create stages to mandatory human approval regardless of score, degraded-mode
behaviour when specialist scoring is unavailable, the resume-value rehydration contract
that hides direct-invoke vs. server-mode serde differences from callers, and CLI steering
(`forge cancel` → synthetic reject, `forge skip` → synthetic override). Decisions are
written durably even when downstream notification publishes fail, and each decision
records the rationale, priors consulted, and findings considered. Behaviour is described
in domain terms — the reasoning-model-driven evaluation and NATS transport surface as
capability observations, not implementation steps.

## Scenario Counts by Category

| Category | Count |
|----------|-------|
| Key examples (@key-example) | 8 |
| Boundary conditions (@boundary) | 5 |
| Negative cases (@negative) | 7 |
| Edge cases (@edge-case) | 10 |
| Smoke (@smoke) | 4 |
| Regression (@regression) | 4 |
| Security (@security) | 2 |
| Concurrency (@concurrency) | 2 |
| Data integrity (@data-integrity) | 2 |
| Integration (@integration) | 1 |

Note: several scenarios carry multiple tags (e.g. boundary + negative, edge-case +
regression, security + edge-case). Group totals do not sum to 32.

## Group Layout

| Group | Theme | Scenarios |
|-------|-------|-----------|
| A | Key Examples — auto-approve, flag-for-review, hard-stop, approve/reject/override resume paths, constitutional PR-review rule, decision traceability | 8 |
| B | Boundary Conditions — degraded mode, default wait time, refresh within max wait, criterion-score extremes accepted and rejected | 5 |
| C | Negative Cases — critical finding escalation, PR-create constitutional branch, mandatory-approval threshold invariant, unapproved calibration adjustment filtered, unknown decision value refused | 5 |
| D | Edge Cases — idempotent duplicate response, crash-recovery re-emit, CLI cancel/skip synthetic decisions, typed-vs-dict rehydration, per-build response routing, degraded-mode marker, max-wait ceiling | 8 |
| E | Security / Concurrency / Data Integrity / Integration — unrecognised responder, two-layer constitutional guard, simultaneous responses, pause-and-publish consistency, durable decision on publish failure, adapter-renderable approval request | 6 |

## Deferred Items

None.

## Assumptions

- **ASSUM-001** (high) — default initial approval-request wait time 300 seconds (API-nats-approval-protocol §3.1)
- **ASSUM-002** (high) — maximum total approval wait ≈ 3600 seconds via `forge.yaml.approval.max_wait_seconds` (API-nats-approval-protocol §7)
- **ASSUM-003** (medium) — behaviour at the max-wait ceiling ends the pause under a configured fallback; specific fallback deferred to `forge-pipeline-config`
- **ASSUM-004** (high) — constitutional-override target identifiers are `review_pr` and `create_pr_after_review` (DM-gating §3, API-nats-approval-protocol §8)
- **ASSUM-005** (high) — CLI steering mapping: cancel → reject (`cli cancel`), skip → override (`cli skip`) (API-nats-approval-protocol §7)
- **ASSUM-006** (high) — responder idempotency on request identifier, first response wins, short-TTL dedup set (API-nats-approval-protocol §6)
- **ASSUM-007** (medium) — "expected approver" identity configured per deployment (e.g. `rich` / Jarvis adapter id); allowlist semantics implied by constitutional framing (API-nats-approval-protocol §4.1)

## Upstream Dependencies

- **FEAT-FORGE-003** — Specialist Agent Delegation. This feature consumes Coach scores,
  criterion breakdowns, and detection findings produced by the specialist delegation
  layer; gating logic here presupposes those results are available (or explicitly absent
  in degraded mode).
- **FEAT-FORGE-002** — NATS Fleet Integration. The build-specific approval channel
  (`agents.approval.forge.{build_id}` and its `.response` mirror) rides on the fleet
  message bus established by FEAT-FORGE-002.
- **FEAT-FORGE-001** — Pipeline State Machine & Configuration. The paused-state
  transition, crash-recovery re-emission, and durable recording of each gate decision
  against the build all ride on the state machine and SQLite substrate from
  FEAT-FORGE-001.

## Integration with /feature-plan

This summary can be passed to `/feature-plan` as a context file:

    /feature-plan "Confidence-Gated Checkpoint Protocol" \
      --context features/confidence-gated-checkpoint-protocol/confidence-gated-checkpoint-protocol_summary.md

`/feature-plan` Step 11 will link `@task:<TASK-ID>` tags back into the
`.feature` file after tasks are created.

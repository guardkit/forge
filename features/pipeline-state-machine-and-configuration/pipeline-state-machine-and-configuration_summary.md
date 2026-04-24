# Feature Spec Summary: Pipeline State Machine and Configuration

**Feature ID**: FEAT-FORGE-001
**Stack**: python
**Generated**: 2026-04-23T20:00:00Z
**Scenarios**: 35 total (3 smoke, 0 regression)
**Assumptions**: 6 total (0 high / 4 medium / 2 low confidence)
**Review required**: Yes (2 low-confidence assumptions)

## Scope

Specifies the durable build lifecycle — queue to terminal — including the state
machine transitions, SQLite-backed history, crash recovery, sequential-queue
discipline, and the CLI surface (`forge queue`, `forge status`, `forge history`,
`forge cancel`, `forge skip`). Behaviour is described in domain terms; underlying
mechanisms (WAL concurrency, NATS pipeline subjects, Pydantic validation) appear
only as capability observations, not implementation steps.

## Scenario Counts by Category

| Category | Count |
|----------|-------|
| Key examples (@key-example) | 6 |
| Boundary conditions (@boundary) | 6 |
| Negative cases (@negative) | 11 |
| Edge cases (@edge-case) | 16 |
| Smoke (@smoke) | 3 |
| Regression (@regression) | 0 |

Note: several scenarios carry multiple tags — e.g. boundary + negative for the
"outside the permitted range" pair, edge-case + negative for the security and
integration-boundary refusals. Group totals do not sum to 35.

## Group Layout

| Group | Theme | Scenarios |
|-------|-------|-----------|
| A | Key Examples — queue, lifecycle, status, history, config, non-blocking reads | 6 |
| B | Boundary Conditions — turn budget, history limit, uniqueness, full-view slice | 6 |
| C | Negative Cases — allowlist, duplicate, skip-misuse, missing cancel, validation, hard-stop, invalid transition | 7 |
| D | Edge Cases — crash recovery across every lifecycle state, cancel-paused, skip-flagged, sequential queue, watch | 9 |
| E | Security — path-traversal rejection, cancel-operator audit | 2 |
| F | Concurrency — simultaneous queues, consistent-snapshot reads | 2 |
| G | Data Integrity — terminal-state completion-time invariant, write-then-publish partial failure | 2 |
| H | Integration Boundaries — pipeline messaging unreachable | 1 |

## Deferred Items

None. All four initial groups and all four expansion groups were accepted
without deferral.

## Open Assumptions (low confidence)

Two low-confidence assumptions that need human verification before this
specification is treated as authoritative:

- **ASSUM-002** — Maximum reasoning-turn budget of 20 used in boundary Examples.
  No ceiling is stated anywhere. Options: keep 20 as an illustrative upper
  bound, raise or lower it, or remove the upper-bound boundary and treat the
  value as unbounded.

- **ASSUM-005** — Cancelling operator recorded distinctly from originating
  operator. Implied by the "synthetic ApprovalResponsePayload(responder=…)"
  wording in API-cli §6.2 but not stated explicitly. If the intent is
  single-operator only, this scenario can be simplified or removed.

## Medium-Confidence Assumptions (Coach-review recommended)

- ASSUM-001 — minimum reasoning-turn budget is 1
- ASSUM-003 — feature_id rejects path-traversal sequences
- ASSUM-004 — sequential-queue scope is per-project
- ASSUM-006 — build row remains visible after publish failure

## Integration with /feature-plan

This summary can be passed to `/feature-plan` as a context file:

    /feature-plan "Pipeline State Machine and Configuration" \
      --context features/pipeline-state-machine-and-configuration/pipeline-state-machine-and-configuration_summary.md

`/feature-plan` Step 11 will link @task:<TASK-ID> tags back into the
`.feature` file after tasks are created.

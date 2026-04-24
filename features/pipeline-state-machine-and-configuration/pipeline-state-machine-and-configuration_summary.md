# Feature Spec Summary: Pipeline State Machine and Configuration

**Feature ID**: FEAT-FORGE-001
**Stack**: python
**Generated**: 2026-04-23T20:00:00Z
**Revised**: 2026-04-24 — ASSUM-002 retired; ASSUM-005 promoted to medium
**Scenarios**: 34 total (3 smoke, 0 regression)
**Assumptions**: 5 total (0 high / 5 medium / 0 low confidence)
**Review required**: No — all open assumptions confirmed and grounded

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

Note: several scenarios carry multiple tags (e.g. boundary + negative, edge-case
+ negative). Group totals do not sum to 34.

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

None.

## Assumptions (all medium confidence, all confirmed)

- **ASSUM-001** — minimum reasoning-turn budget is 1
- **ASSUM-003** — feature_id rejects path-traversal sequences
- **ASSUM-004** — sequential-queue scope is per-project
- **ASSUM-005** — cancelling operator recorded distinctly from originating_user (schema-grounded)
- **ASSUM-006** — build row remains visible as pending pickup after publish failure

### Retired Assumptions

- **ASSUM-002** (retired 2026-04-24) — the upper-bound reasoning-turn-budget ceiling
  of 20 was an arbitrary choice with no basis in the CLI contract or forge.yaml
  schema. The accept-boundary Scenario Outline no longer tests a ceiling; the
  minimum (1) and default (5) are the only positive-side boundary values. The
  reject-boundary scenario still covers zero / negative values.

## Integration with /feature-plan

This summary can be passed to `/feature-plan` as a context file:

    /feature-plan "Pipeline State Machine and Configuration" \
      --context features/pipeline-state-machine-and-configuration/pipeline-state-machine-and-configuration_summary.md

`/feature-plan` Step 11 will link @task:<TASK-ID> tags back into the
`.feature` file after tasks are created.

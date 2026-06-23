---
id: TASK-REV-RSP-001
title: "Plan: Runbook and Step Persistence"
status: review_complete
created: 2026-06-21T18:30:00Z
updated: 2026-06-21T18:30:00Z
priority: high
task_type: review
parent_feature: FEAT-RSP
feature_slug: runbook-and-step-persistence
clarification:
  context_a:
    decisions:
      focus: all
      tradeoff: balanced
  context_b:
    decisions:
      approach: sibling-tables-mirroring-bridge-registry
      granularity: layered
      execution: parallel-where-safe
      testing: tdd
source_spec: features/runbook-and-step-persistence/runbook-and-step-persistence_summary.md
tags:
  - forge
  - persistence
  - runbook
  - output-loop
---

# Plan: Runbook and Step Persistence

## Decision record

Drives `FEAT-RSP`. This review anchors the provenance of the six
implementation tasks in this folder (`parent_review: TASK-REV-RSP-001`).

### Approach chosen — Option 1: sibling tables mirroring `BridgeRegistry`

Add `runbooks` + `runbook_steps` as **STRICT** SQLite tables with
`CHECK`-constrained `status` columns, an idempotent `apply(connection)`
migration under `forge.persistence.migrations`, and a `RunbookRepository`
under `forge.persistence.repositories` that uses `BEGIN IMMEDIATE` writes,
`_safe_rollback`, ISO-8601 TEXT timestamps, and JSON-encoded
`params`/`result` columns — exactly the published
`forge.persistence.repositories.bridge_registry.BridgeRegistry` shape.
Reuses `forge.adapters.sqlite.connect.connect_writer` /
`read_only_connect`; the store-unavailable path surfaces
`SQLiteConnectError`.

**Options considered and rejected:**

- **Option 2 — single denormalised `runbooks` table with a JSON steps
  blob.** Rejected: loses the per-step `CHECK` constraint, per-step status
  queryability, and the sequence-order reload guarantee the spec demands
  (Group G "Steps reload in sequence order").
- **Option 3 — introduce an ORM (SQLModel / SQLAlchemy).** Rejected:
  adds a dependency and diverges from the established raw-`sqlite3`
  convention (LCOI / YAGNI).

### Scope (verbatim from the feature summary)

Data model + persistence + repository methods (`create_runbook`,
`load_runbook`, `update_step_status`, `advance`) for the Forge
output-side loop. **NO executor logic, NO NATS, NO subprocess, NO LLM**
in this phase. Gates are modelled as data only (a step in
`awaiting_approval`); nothing acts on them yet.

### Low-confidence assumptions locked as decisions

All three are marked `confirmed` in
`runbook-and-step-persistence_assumptions.yaml` and are now binding:

- **ASSUM-002** — a runbook must contain at least one step; an empty step
  list is refused at create time.
- **ASSUM-004** — advancing when the resume pointer is already at the
  final step is refused; the pointer never moves past the last step.
- **ASSUM-009** — overall status is set at `create_runbook` time and is
  **not** mutated by `update_step_status` or `advance` this phase.

### Task breakdown (layered, 6 tasks, 4 waves)

| Task | Layer | Wave | task_type | Complexity |
|------|-------|------|-----------|------------|
| TASK-RSP-001 | Step / Runbook data models | 1 | declarative | 4 |
| TASK-RSP-002 | Runbook store migration (DDL) | 1 | feature | 5 |
| TASK-RSP-003 | Repo: `create_runbook` + `load_runbook` | 2 | feature | 6 |
| TASK-RSP-004 | Repo: `update_step_status` + `advance` | 3 | feature | 6 |
| TASK-RSP-005 | Security + data-integrity tests | 4 | testing | 5 |
| TASK-RSP-006 | Concurrency + integration-boundary tests | 4 | testing | 6 |

See `IMPLEMENTATION-GUIDE.md` for the data-flow diagram, integration
contracts (§4), and the task dependency graph.

### Risk

**Low.** Pure-unit; every one of the 33 Gherkin scenarios is satisfiable
in-process with a `tmp_path` SQLite file. No live infrastructure, no
human-in-the-loop, no wall-clock predicates → **zero `operator_handoff`
tasks**.

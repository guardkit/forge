# Feature Spec Summary: Runbook and Step Persistence

**Stack**: python
**Generated**: 2026-06-21T17:18:54Z
**Scenarios**: 33 total (4 smoke, 0 regression)
**Assumptions**: 11 total (2 high / 6 medium / 3 low confidence)
**Review required**: Yes

## Scope

The data model and persistence layer for the Forge output-side loop: a typed
`Step` (`step_type`, `params`, status enum `pending/running/passed/failed/awaiting_approval`,
a result with `exit_code` + `captured_output` + start/finish timestamps, `sequence_index`)
and a `Runbook` (`runbook_id`, ordered list of steps, `current_step_index` resume pointer,
overall status, target name, `created_at`), persisted in Forge's existing SQLite store via a
sibling table that reuses `adapters/sqlite/connect.py` and the established migration path.
It covers per-step status queryability, the resume pointer, gates-as-data (a step in
`awaiting_approval`, modelled but never acted on this phase), and the repository surface
`create_runbook` / `load_runbook` / `update_step_status` / `advance`. No executor, NATS,
subprocess, or LLM behaviour is in scope.

## Scenario Counts by Category

| Category | Count |
|----------|-------|
| Key examples (@key-example) | 5 |
| Boundary conditions (@boundary) | 4 |
| Negative cases (@negative) | 13 |
| Edge cases (@edge-case) | 19 |
| Smoke (@smoke) | 4 |
| Regression (@regression) | 0 |

(Categories overlap: many edge cases are also tagged `@negative`. The 33 distinct
scenarios break down as 5 Key Examples, 4 Boundary, 5 Negative, 6 Edge, plus 13
Phase-4 expansion scenarios across Security/Concurrency/Data Integrity/Integration.)

### Phase-4 expansion breakdown (13)

| Dimension | Count | Coverage |
|-----------|-------|----------|
| Security | 3 | adversarial identifiers/target verbatim, control-char/nested params, oversized captured output |
| Concurrency | 3 | duplicate-create serialisation, advance+update serialisation, WAL reader snapshot |
| Data Integrity | 4 | rollback atomicity, sequence-order reload, refused-advance cross-field consistency, idempotent migration |
| Integration Boundaries | 3 | store-unavailable (outline), read-only write refusal, unmigrated-store load refusal |

## Deferred Items

None. All four Phase-2 groups and all 13 Phase-4 expansion scenarios were accepted.

## Open Assumptions (low confidence)

These need human verification before the specification is treated as authoritative:

- **ASSUM-002** — A runbook must contain at least one step (empty step list refused).
  *Alternative: allow an empty runbook as a degenerate case.*
- **ASSUM-004** — Advancing past the final step is refused; the pointer never moves past
  the last step. *Alternative: allow a "completed" position equal to the step count
  (`current_step_index == len(steps)`).*
- **ASSUM-009** — Overall status is set at create time and is not mutated by
  `update_step_status` or `advance` this phase. *Alternative: derive overall status from
  step transitions.*

## Implementation notes for the persistence layer (carry into /feature-plan)

These are grounded in the existing Forge SQLite conventions, not part of the spec itself,
but should shape the tasks:

- Add a sibling table (e.g. `runbooks` + `runbook_steps`) as a STRICT table with a
  CHECK-constrained `status` column mirroring `lifecycle_bridge_registry`.
- Ship the migration as an idempotent `apply(connection)` module under
  `forge.persistence.migrations`, composed after `forge.lifecycle.migrations.apply_at_boot`.
- Implement the repository under `forge.persistence.repositories` using `BEGIN IMMEDIATE`
  writes, `_safe_rollback`, ISO-8601 TEXT timestamps, and JSON-encoded `params`/`result`
  columns — exactly the `BridgeRegistry` shape.
- Reuse `forge.adapters.sqlite.connect.connect_writer` / `read_only_connect`; let the
  store-unavailable path surface `SQLiteConnectError` (Group H).
- Unit tests only (no NATS, no subprocess, no LLM); a `tmp_path` SQLite file per test.

## Integration with /feature-plan

This summary can be passed to `/feature-plan` as a context file:

    /feature-plan "Runbook and Step Persistence" \
      --context features/runbook-and-step-persistence/runbook-and-step-persistence_summary.md

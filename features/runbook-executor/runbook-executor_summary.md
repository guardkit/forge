# Feature Spec Summary: Runbook Executor

**Stack**: python
**Generated**: 2026-06-21T17:37:33Z
**Scenarios**: 28 total (6 smoke, 0 regression)
**Assumptions**: 9 total (1 high / 5 medium / 3 low confidence)
**Review required**: Yes

## Scope

The dispatch-by-step-type execution loop that sits on top of the Runbook and Step
Persistence feature. For each step the executor resolves a handler from a step-type
registry, runs it, persists the result + status, and advances the resume pointer; on
failure it stops and a later run re-enters at the failed step index (no restart). It
announces lifecycle events — `runbook-started` / `step-started` / `step-result` /
`runbook-complete` / `escalated` — reusing the `pipeline_publisher` envelope pattern
(`source_id="forge"`, fire-and-forget, a publish failure logged but never rolled back).
The executor holds no knowledge of step internals — registry indirection only — and is
driven by the CLI entry `forge runbook run <path-to-runbook-json>`. Unit scenarios use
in-memory fake handlers (no subprocess, no broker); a single `@integration @slow`
scenario publishes to a real NATS broker.

## Scenario Counts by Category

| Category | Count |
|----------|-------|
| Key examples (@key-example) | 6 |
| Boundary conditions (@boundary) | 4 |
| Negative cases (@negative) | 6 |
| Edge cases (@edge-case) | 16 |
| Smoke (@smoke) | 6 |
| Regression (@regression) | 0 |

(Categories overlap: several edge cases are also tagged `@negative`. The 28 distinct
scenarios break down as 6 Key Examples, 4 Boundary, 3 Negative, 8 Edge, plus 7 Phase-4
expansion scenarios across Security/Concurrency/Data-Integrity/Integration.)

### Phase-4 expansion breakdown (7)

| Dimension | Count | Coverage |
|-----------|-------|----------|
| Security | 2 | adversarial step_type stays an inert lookup key; handler exception contained as a failure |
| Concurrency | 1 | two executors on one runbook never double-run a step |
| Data Integrity | 2 | persisted state authoritative over the event stream for resume; result committed before pointer advances |
| Integration Boundaries | 2 | awaiting_approval gate pauses + escalates; real-NATS publish observed by a subscriber (@integration @slow) |

## Deferred Items

None. All four Phase-2 groups (A/B/C/D) and all 7 Phase-4 expansion scenarios were accepted as-is.

## Open Assumptions (low confidence)

These need human verification before the specification is treated as authoritative:

- **ASSUM-003** — A step that resolves to `awaiting_approval` pauses the run and escalates.
  *The handler -> awaiting_approval contract is unverified; the persistence layer modelled
  the gate as data but nothing acts on it yet.*
- **ASSUM-004** — On success the resume pointer rests **beyond** the final step
  (`current_step_index == step count`). **CONFLICTS with the persistence feature's open
  ASSUM-004**, which refuses advancing past the final step. Reconcile before implementation:
  either relax persistence to allow a terminal `== count` position, or mark completion by
  overall-status only (pointer rests **on** the final step).
- **ASSUM-005** — Re-running an already-complete runbook is a no-op that reports
  "already complete", not an error.

## Cross-feature note

This feature depends on **Runbook and Step Persistence**
(`features/runbook-and-step-persistence/`). The `current_step_index` / `advance` semantics
must be agreed across both: persistence ASSUM-004 (refuse advancing past final) and this
feature's ASSUM-004 (terminal position == count) are in direct tension and should be settled
as one decision before either reaches implementation.

## Implementation notes (carry into /feature-plan)

Grounded in the existing Forge conventions, not part of the spec itself, but should shape tasks:

- The executor depends only on the persistence repository surface
  (`create_runbook` / `load_runbook` / `update_step_status` / `advance`) and a step-type
  registry mapping `step_type -> handler`. It imports no concrete handler.
- Reuse the `forge.adapters.nats.pipeline_publisher.PipelinePublisher` shape for the new
  runbook lifecycle events: `MessageEnvelope`, `source_id="forge"`, fire-and-forget publish,
  `PublishFailure` caught + logged but never rolled back (LES1 parity rule). New event
  subjects/`EventType` members will be needed for runbook-started / step-started /
  step-result / runbook-complete / escalated.
- The CLI subcommand attaches to the existing Click group `forge.cli.main:main` (sibling of
  `queue` / `status` / `history` / `cancel` / `skip`).
- Register the `@runbook-executor` feature tag and any new scenario marks in `pyproject.toml`
  `[tool.pytest.ini_options].markers` so pytest-bdd does not warn on unknown marks.
- Unit gates use in-memory fake handlers — no subprocess, no NATS broker. The single
  real-broker scenario is `@integration @slow` and is excluded from the default `pytest` run.

## Integration with /feature-plan

This summary can be passed to `/feature-plan` as a context file:

    /feature-plan "Runbook Executor" \
      --context features/runbook-executor/runbook-executor_summary.md

---
id: TASK-RSP-002
title: Runbook store migration (runbooks + runbook_steps STRICT tables)
status: in_review
created: 2026-06-21 18:30:00+00:00
updated: 2026-06-21 18:30:00+00:00
priority: high
task_type: feature
parent_review: TASK-REV-RSP-001
parent_feature: FEAT-RSP
feature_slug: runbook-and-step-persistence
wave: 1
implementation_mode: task-work
complexity: 5
estimated_minutes: 75
dependencies: []
tags:
- forge
- persistence
- runbook
- migration
- sqlite
autobuild_state:
  current_turn: 4
  max_turns: 5
  worktree_path: /Users/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-RSP
  base_branch: main
  started_at: '2026-06-21T18:49:50.391338'
  last_updated: '2026-06-21T19:21:01.224567'
  turns:
  - turn: 1
    decision: feedback
    feedback: '- Independent test verification did not complete successfully. The
      evidence_bundle.independent_tests shows tests_passed: false and the test_output_summary
      contains only ''I''ll run the pytest command for you and provide the full output.''
      rather than actual pytest output. Cannot verify acceptance criteria without
      independent test evidence.: The orchestrator''s independent test mechanism needs
      to successfully run pytest tests/forge/persistence/test_runbook_migration.py
      and produce actual test output. Investigate why the independent test run failed.

      - The requirements analyzer in evidence_bundle.requirements shows all_criteria_met:
      false with all 8 criteria marked as ''rejected'' with ''No completion promise
      for AC-001'' through ''AC-008''. This appears to be due to an ID format mismatch:
      Player used ''AC-1'' through ''AC-8'' in completion_promises but the analyzer
      expected ''AC-001'' through ''AC-008'' with leading zeros.: Ensure completion_promises
      use criterion IDs matching the exact format specified in the acceptance criteria
      (e.g., ''AC-001'' not ''AC-1'').

      - The evidence_bundle.tests section shows tests_run: null and all_passed: null,
      indicating no verified test execution count. This is ABSENT SIGNAL per absence-of-failure
      guards.: Ensure the test verification mechanism produces definitive test execution
      counts.'
    timestamp: '2026-06-21T18:49:50.391338'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
  - turn: 2
    decision: feedback
    feedback: "- Deterministic honesty record (claim_audit_unmodified, severity=should_fix):\
      \ Player claim: Player claimed file features/conftest.py. Actual: Path is tracked\
      \ in git but 'git status --porcelain' shows no change for it \u2014 the Player\
      \ claimed work on a file it did not actually modify this turn. Most likely cause:\
      \ the report writer swept an orchestrator-managed path (e.g. a file under .guardkit/autobuild/\
      \ or tasks/<state>/) into files_modified. Defence-in-depth for the agent_invoker-side\
      \ filter; this is a warning, not a turn-rejecting fabrication..\n- Deterministic\
      \ honesty record (claim_audit_unmodified, severity=should_fix): Player claim:\
      \ Player claimed file src/forge/persistence/migrations/__init__.py. Actual:\
      \ Path is tracked in git but 'git status --porcelain' shows no change for it\
      \ \u2014 the Player claimed work on a file it did not actually modify this turn.\
      \ Most likely cause: the report writer swept an orchestrator-managed path (e.g.\
      \ a file under .guardkit/autobuild/ or tasks/<state>/) into files_modified.\
      \ Defence-in-depth for the agent_invoker-side filter; this is a warning, not\
      \ a turn-rejecting fabrication..\n- Deterministic honesty record (claim_audit_unmodified,\
      \ severity=should_fix): Player claim: Player claimed file src/forge/persistence/migrations/runbook.py.\
      \ Actual: Path is tracked in git but 'git status --porcelain' shows no change\
      \ for it \u2014 the Player claimed work on a file it did not actually modify\
      \ this turn. Most likely cause: the report writer swept an orchestrator-managed\
      \ path (e.g. a file under .guardkit/autobuild/ or tasks/<state>/) into files_modified.\
      \ Defence-in-depth for the agent_invoker-side filter; this is a warning, not\
      \ a turn-rejecting fabrication..\n... and 7 more issues"
    timestamp: '2026-06-21T18:58:36.274628'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
  - turn: 3
    decision: feedback
    feedback: "- Deterministic honesty record (claim_audit_unmodified, severity=should_fix):\
      \ Player claim: Player claimed file features/conftest.py. Actual: Path is tracked\
      \ in git but 'git status --porcelain' shows no change for it \u2014 the Player\
      \ claimed work on a file it did not actually modify this turn. Most likely cause:\
      \ the report writer swept an orchestrator-managed path (e.g. a file under .guardkit/autobuild/\
      \ or tasks/<state>/) into files_modified. Defence-in-depth for the agent_invoker-side\
      \ filter; this is a warning, not a turn-rejecting fabrication..\n- Deterministic\
      \ honesty record (claim_audit_unmodified, severity=should_fix): Player claim:\
      \ Player claimed file src/forge/persistence/migrations/__init__.py. Actual:\
      \ Path is tracked in git but 'git status --porcelain' shows no change for it\
      \ \u2014 the Player claimed work on a file it did not actually modify this turn.\
      \ Most likely cause: the report writer swept an orchestrator-managed path (e.g.\
      \ a file under .guardkit/autobuild/ or tasks/<state>/) into files_modified.\
      \ Defence-in-depth for the agent_invoker-side filter; this is a warning, not\
      \ a turn-rejecting fabrication..\n- Deterministic honesty record (claim_audit_unmodified,\
      \ severity=should_fix): Player claim: Player claimed file src/forge/persistence/migrations/runbook.py.\
      \ Actual: Path is tracked in git but 'git status --porcelain' shows no change\
      \ for it \u2014 the Player claimed work on a file it did not actually modify\
      \ this turn. Most likely cause: the report writer swept an orchestrator-managed\
      \ path (e.g. a file under .guardkit/autobuild/ or tasks/<state>/) into files_modified.\
      \ Defence-in-depth for the agent_invoker-side filter; this is a warning, not\
      \ a turn-rejecting fabrication..\n... and 6 more issues"
    timestamp: '2026-06-21T19:05:47.659971'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
  - turn: 4
    decision: approve
    feedback: null
    timestamp: '2026-06-21T19:15:27.684142'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# Runbook store migration

## TL;DR

Add an idempotent `apply(connection)` migration that creates the
**STRICT** `runbooks` and `runbook_steps` tables, mirroring the
`lifecycle_bridge_registry` migration. This is the **§4 Integration
Contract producer**: the table schema it defines (column names, types,
`CHECK` set, FK, ordering column) is consumed verbatim by the repository
(TASK-RSP-003/004).

## Scope

Create `src/forge/persistence/migrations/runbook.py` exporting:

- `apply(connection: sqlite3.Connection) -> None` — runs
  `connection.executescript(CREATE_TABLES_SQL)` inside `with connection:`
  for transactional safety; idempotent via `CREATE TABLE IF NOT EXISTS`.
- `RunbookMigrationError(RuntimeError)` — wraps any `sqlite3.Error` via
  `__cause__`, exactly like `BridgeRegistryMigrationError`.

DDL (STRICT, mirroring `lifecycle_bridge_registry.py`):

```sql
CREATE TABLE IF NOT EXISTS runbooks (
    runbook_id          TEXT PRIMARY KEY,
    target              TEXT NOT NULL,
    current_step_index  INTEGER NOT NULL,
    status              TEXT NOT NULL CHECK (
        status IN ('pending','running','passed','failed','awaiting_approval')
    ),
    created_at          TEXT NOT NULL          -- ISO-8601 TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS runbook_steps (
    runbook_id      TEXT NOT NULL REFERENCES runbooks(runbook_id) ON DELETE CASCADE,
    sequence_index  INTEGER NOT NULL,
    step_type       TEXT NOT NULL,
    params          TEXT NOT NULL DEFAULT '{}',  -- JSON-encoded mapping
    status          TEXT NOT NULL CHECK (
        status IN ('pending','running','passed','failed','awaiting_approval')
    ),
    result          TEXT,                        -- nullable JSON (null until recorded)
    PRIMARY KEY (runbook_id, sequence_index)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_runbook_steps_order
    ON runbook_steps (runbook_id, sequence_index);
```

Register the module in `src/forge/persistence/migrations/__init__.py`
(import + `__all__`), matching how `lifecycle_bridge_registry` is exported.

## Acceptance Criteria

- [ ] `forge.persistence.migrations.runbook.apply(connection)` creates
      both `runbooks` and `runbook_steps` as **STRICT** tables.
- [ ] Both `status` columns carry a `CHECK` constraint whose allowed set
      is exactly `pending/running/passed/failed/awaiting_approval` (matches
      `StepStatus`; see §4 contract).
- [ ] `runbooks.runbook_id` is the `PRIMARY KEY`; a duplicate insert is
      rejected at the engine level (drives the duplicate-create refusal in
      TASK-RSP-003).
- [ ] `runbook_steps` has a composite `PRIMARY KEY (runbook_id, sequence_index)`
      and a FK to `runbooks(runbook_id)`.
- [ ] **Idempotent**: applying the migration twice against the same
      connection changes nothing and raises nothing (Group G "Re-running
      the migration against an already-migrated store changes nothing").
- [ ] A `sqlite3.Error` during migration is re-raised as
      `RunbookMigrationError` preserving the original via `__cause__`.
- [ ] Tests live in `tests/forge/persistence/test_runbook_migration.py`,
      written **test-first**, using the `tmp_path` + `connect_writer` +
      `lifecycle_migrations.apply_at_boot` fixture pattern from
      `test_bridge_registry.py`.
- [ ] All modified files pass project-configured lint/format checks with
      zero errors.

## Coach Validation

```bash
python -m pytest tests/forge/persistence/test_runbook_migration.py -q
python -c "import sqlite3, tempfile, os; from forge.adapters.sqlite import connect; from forge.lifecycle import migrations as lm; from forge.persistence.migrations import runbook as rm; d=tempfile.mkdtemp(); cx=connect.connect_writer(__import__('pathlib').Path(d)/'forge.db'); lm.apply_at_boot(cx); rm.apply(cx); rm.apply(cx); print('idempotent apply OK')"
```

## Implementation Notes

- Compose **after** `forge.lifecycle.migrations.apply_at_boot` — the
  runbook tables are a sibling of the lifecycle schema, not a replacement.
  Tests apply `apply_at_boot(cx)` then `runbook.apply(cx)`.
- Keep `PRAGMA foreign_keys = ON` semantics: `connect_writer` already
  sets it, so the FK + `ON DELETE CASCADE` is live in tests.
- Do **not** add an overall-status mutator path here — overall status is
  immutable this phase (ASSUM-009); it is written once by `create_runbook`.

## §4 Contract — producer

This task **produces** the `runbooks` / `runbook_steps` schema contract
and the `StepStatus value set` contract consumed by TASK-RSP-003/004.
See `IMPLEMENTATION-GUIDE.md` §4 for the binding format constraints.

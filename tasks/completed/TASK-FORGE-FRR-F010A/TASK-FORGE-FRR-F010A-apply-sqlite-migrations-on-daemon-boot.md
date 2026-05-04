---
id: TASK-FORGE-FRR-F010A
title: "Apply SQLite migrations on daemon boot in `bind_production_serve`"
status: completed
created: 2026-05-04T00:00:00Z
updated: 2026-05-04T07:30:00Z
completed: 2026-05-04T07:30:00Z
completed_location: tasks/completed/TASK-FORGE-FRR-F010A/
organized_files:
  - TASK-FORGE-FRR-F010A-apply-sqlite-migrations-on-daemon-boot.md
priority: high
task_type: fix
tags:
  - forge-serve
  - feat-forge-010-followup
  - first-real-run-followup
  - task-fix-f010-followup
  - migrations
  - sqlite
  - quick-fix
  - bind_production_serve
complexity: 2
estimated_minutes: 30
estimated_effort: "15-30 minutes (one call site + one unit test)"
parent_feature: FEAT-FORGE-010
correlation_id: a55df422-dd03-4562-9326-0278f3eeb764
related_tasks:
  - TASK-FIX-F010   # introduced the wrapper that needs the call
  - TASK-REV-F010   # the design that scoped this
  - TASK-FW10-007   # composed PipelineConsumerDeps against the persistence facade
discovered_on:
  date: 2026-05-04
  machine: GB10 (promaxgb10-41b1)
  context: "Post-TASK-FIX-F010 jarvis FRR runbook rerun on the GB10 — production composer wired (TASK-FIX-F010 verified) but autobuild dispatch path errored once exercised end-to-end"
test_results:
  status: passed
  coverage: null   # micro-task mode — coverage not measured per /task-work --micro
  last_run: 2026-05-04T07:00:00Z
  summary: |
    3 new tests in tests/forge/test_serve_production_migrations.py — all passing.
    12 existing tests in tests/forge/test_cli_serve_production.py — all passing
    (AC-5 regression).
    72 sibling cli/serve tests — all passing
    (test_cli_serve_skeleton, test_cli_serve_logging, test_cli_serve_daemon,
     test_cli_serve_deps_forward_context).
acceptance_criteria_status:
  AC-1: passed
  AC-2: passed-with-scope-note  # tests assert the 4 migration-managed tables; async_tasks is provisioned by build_autobuild_state_initialiser at dispatcher-construction time, out of apply_at_boot's scope
  AC-3: passed
  AC-4: passed
  AC-5: passed
  AC-6: pending-operator  # operator-only: rebuild forge image + re-run jarvis runbook §6.2 + §7
---

# Task: Apply SQLite migrations on daemon boot in `bind_production_serve`

## Description

A fresh `FORGE_DB_PATH` volume mounted into `forge-prod` has no
`builds` / `stage_log` / `async_tasks` / `schema_version` tables.
`forge.cli.queue` (the operator-facing `forge queue` subcommand) calls
`apply_at_boot` from `forge.lifecycle.migrations` before any DB write,
but `forge.cli._serve_production.bind_production_serve` — the wrapper
TASK-FIX-F010 introduced as the production composer's binding seam —
does **not**. So when the daemon's first inbound
`pipeline.build-queued.*` envelope arrives, `dispatch_build` raises
`no such table: builds` against the empty DB and acks the message
without publishing any outbound lifecycle envelope.

The bootstrap path — what every `forge-prod` operator gets from a
fresh volume mount — is therefore broken. The migration runner is
already idempotent (`forge.lifecycle.migrations:91`), so the call is
safe to add unconditionally.

## Why

### Empirical evidence (run 3 of post-TASK-FIX-F010 rerun, 2026-05-04)

correlation_id `a55df422-dd03-4562-9326-0278f3eeb764` — `forge.yaml`
allowlist had been widened to include `/home/forge`, the path-validation
layer passed, and the production consumer reached `dispatch_build`
against a fresh `/var/forge/forge.db`:

```
[WARNING] forge.cli._serve_deps: is_duplicate_terminal: SQLite read failed for feature_id=FEAT-43DE correlation_id=a55df422-... (no such table: builds); treating as non-duplicate
[INFO] forge.adapters.nats.pipeline_consumer: pipeline_consumer: dispatching build feature_id=FEAT-43DE correlation_id=a55df422-... originating_adapter=terminal
[WARNING] forge.adapters.nats.pipeline_consumer: pipeline_consumer: dispatch_build raised (no such table: builds) for feature_id=FEAT-43DE correlation_id=a55df422-...; acking and continuing so the next build can be processed
```

### Workaround used during the rerun

```bash
docker exec forge-prod python -c "from forge.lifecycle.migrations import apply_at_boot; ..."
```

against the mounted DB — returned `2` (versions applied:
`schema.sql` + `schema_v2.sql`). Tables present after: `async_tasks`,
`builds`, `stage_log`, `sqlite_sequence`, `schema_version`.

The workaround is fine for one-off triage but it cannot be a
production deploy step — operators bringing up a fresh volume should
not need to know about migration bootstrap.

### Implementation site

[`src/forge/cli/_serve_production.py`](../../../src/forge/cli/_serve_production.py)
— call `apply_at_boot(connection)` immediately after
`connect_writer(config.db_path)` returns (around line 189 per the
docstring's pipeline). Match the `forge.cli.queue` invocation
pattern:

```python
from forge.lifecycle.migrations import apply_at_boot
# ... inside bind_production_serve, after connect_writer ...
connection = connect_writer(config.db_path)
applied = apply_at_boot(connection)
logger.info("forge-serve: applied %d SQLite migration(s) at boot", applied)
sqlite_pool = SqliteLifecyclePersistence(...)
```

## Acceptance Criteria

- [ ] **AC-1**: `bind_production_serve` calls `apply_at_boot(connection)`
  after `connect_writer(...)` and **before** constructing
  `SqliteLifecyclePersistence(...)`.
- [ ] **AC-2**: A unit test in `tests/forge/test_serve_production*.py`
  (or a new `tests/forge/test_serve_production_migrations.py`) asserts
  that `bind_production_serve` against a fresh `tmp_path/forge.db`
  results in a DB with the canonical 5 tables: `async_tasks`,
  `builds`, `stage_log`, `sqlite_sequence`, `schema_version`. Use the
  `_reset_for_tests` helper to keep the test deterministic across
  re-entrant runs.
- [ ] **AC-3**: The boot log emits an
  `[INFO] forge-serve: applied N SQLite migration(s) at boot` line so
  operators can confirm migrations ran (and can `grep` for
  `applied 0` on subsequent boots to confirm idempotence).
- [ ] **AC-4**: Idempotent on re-bind — re-binding (which can happen
  in a long-running test process via `_reset_for_tests`) does NOT
  re-apply migrations from scratch. `apply_at_boot` is already
  idempotent per `forge.lifecycle.migrations:91`; assert this in the
  unit test by calling `bind_production_serve` twice against the same
  DB and asserting the second call returns `applied=0`.
- [ ] **AC-5**: Regression — existing
  `tests/forge/test_serve_production*.py` tests continue to pass; no
  fixture changes that would mask future schema-bootstrap regressions.
- [ ] **AC-6**: Re-run jarvis runbook §6.2 + §7 against a forge image
  built from the new commit; confirm the run-3 reproducer (fresh DB,
  any path-passing build-queued envelope) no longer logs
  `no such table: builds`. Capture the rerun correlation_id in this
  task's completion notes.

## Files Expected to Change

- `src/forge/cli/_serve_production.py` — add the `apply_at_boot` call
  + log line (~3 lines)
- `tests/forge/test_serve_production*.py` (or new file
  `tests/forge/test_serve_production_migrations.py`) — add the
  AC-2 / AC-3 / AC-4 unit tests (~40-60 lines)

## Implementation Notes

- **Where exactly**: after `connection = connect_writer(config.db_path)`
  at line 189 of `_serve_production.py`, before the
  `SqliteLifecyclePersistence(...)` construction at line 190. The
  `apply_at_boot` call MUST land before any code path that touches
  the schema.
- **Logging**: use the module-level `logger` already in
  `_serve_production.py`. Format the line consistently with the
  existing
  `forge-serve: production composer bound (db_path=...)` line so an
  operator's `docker logs forge-prod` shows the bootstrap sequence
  in order.
- **No `force=` flag**: `apply_at_boot` is already idempotent — a
  second call is cheap and emits `applied=0`. Don't gate it behind a
  config flag.
- **Test data isolation**: the new tests should use `tmp_path` for
  the DB path so they don't pollute the developer's `~/.forge/forge.db`.
  Use `_reset_for_tests` between assertions if testing re-entrant
  behaviour in the same process.
- **Cross-check `forge.cli.queue`**: that module already does this
  the right way — copy its call pattern verbatim. The two sites
  should look identical apart from the surrounding deps construction.

## References

- **RESULTS file** (post-FIX-F010 addendum, evening 2026-05-04):
  [`../../../../../jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md`](../../../../../jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md)
  — see "Gap F010.A — Daemon doesn't apply SQLite migrations on a
  fresh `FORGE_DB_PATH`".
- **TASK-REV-F010 review report**:
  [`../../../.claude/reviews/TASK-REV-F010-review-report.md`](../../../.claude/reviews/TASK-REV-F010-review-report.md)
  — the design that produced the `_serve_production` wrapper module
  this task patches. The migration call was not part of that
  decision space because the rerun that surfaced the missing call
  hadn't happened yet.
- **TASK-FIX-F010 (production-binding sibling)**:
  [`../../completed/TASK-FIX-F010/TASK-FIX-F010-bind-production-dispatch-chain.md`](../../completed/TASK-FIX-F010/TASK-FIX-F010-bind-production-dispatch-chain.md)
  — introduced `bind_production_serve`; this task adds the missing
  bootstrap step.
- **Source files**:
  - [`src/forge/cli/_serve_production.py`](../../../src/forge/cli/_serve_production.py)
    — `bind_production_serve` (line 134), `connect_writer` call
    (line 189)
  - [`src/forge/lifecycle/migrations.py`](../../../src/forge/lifecycle/migrations.py)
    — `apply_at_boot` (the idempotent runner)
  - [`src/forge/cli/queue.py`](../../../src/forge/cli/queue.py)
    — the existing call site to copy from
- **Run that surfaced this**:
  - **correlation_id**: `a55df422-dd03-4562-9326-0278f3eeb764`
  - **Date**: 2026-05-04 (evening rerun, post-`32b67f8`)
  - **Machine**: GB10 (`promaxgb10-41b1`)
  - **forge HEAD**: `af62d5c` (post-TASK-FIX-F010 merge)
  - **Image**: `forge:latest` = sha256 `ebc4311026cc...`

## Completion Notes (2026-05-04)

**Code change** (`src/forge/cli/_serve_production.py`):

- Added `from forge.lifecycle.migrations import apply_at_boot` to the
  module-level imports.
- Added a private `_current_schema_version(connection)` helper (mirrors
  `forge.lifecycle.migrations._current_version` with the same SQL +
  fall-back-to-0 on `sqlite3.OperationalError`). Needed because
  `apply_at_boot` returns the *post-run schema version*, not a delta —
  computing `after - before` here lets the boot log report the count
  of newly-applied migrations (AC-3).
- Inserted Step 3.5 between `connect_writer(config.db_path)` and
  `SqliteLifecyclePersistence(...)` construction:

  ```python
  schema_version_before = _current_schema_version(connection)
  schema_version_after = apply_at_boot(connection)
  applied = max(0, schema_version_after - schema_version_before)
  logger.info(
      "forge-serve: applied %d SQLite migration(s) at boot", applied
  )
  ```

- Updated the `bind_production_serve` docstring's "Pipeline:" section
  to call out Step 3.5.

**Tests** (`tests/forge/test_serve_production_migrations.py`, NEW):

- `TestFreshDbBootstrapsMigrationTables` — fresh `tmp_path/forge.db`
  ends up with the 4 migration-managed tables (`builds`, `stage_log`,
  `sqlite_sequence`, `schema_version`).
- `TestBootLogEmitsAppliedCount` — fresh-DB bind logs
  `[INFO] forge-serve: applied 2 SQLite migration(s) at boot`
  (schema.sql + schema_v2.sql).
- `TestRebindIdempotency` — second bind against the same DB logs
  `applied 0` and the schema is intact.

All three pass. Existing
`tests/forge/test_cli_serve_production.py` (12 tests, AC-5 regression
gate) still passes. Sibling `test_cli_serve_*` and
`test_cli_serve_deps_forward_context` (72 tests) also pass.

**AC-2 scope note** — the task description's "canonical 5 tables"
includes `async_tasks`, but `async_tasks` is provisioned by
`forge.cli._serve_deps_state_channel.ensure_async_tasks_schema` at
dispatcher-construction time (Step 7 of `bind_production_serve`,
inside the real `bind_production_dispatch_chain` call), **not** by
`apply_at_boot`. The new tests stub the dispatcher-chain factory to
keep the test surface narrow, so they assert only the 4 tables
`apply_at_boot` itself owns. In production, all 5 tables will be
present after `bind_production_serve` returns because the real
`bind_production_dispatch_chain` runs the `async_tasks` DDL at Step 7.
The `MIGRATION_TABLES` constant in the new test file documents this
in-line.

**AC-6 deferred to operator** — rebuilding the forge image from the
new commit and re-running jarvis runbook §6.2 + §7 to capture a fresh
correlation_id (and to confirm the run-3 reproducer no longer logs
`no such table: builds`) is operator-only. Will be captured when the
operator runs the revalidation; this file should be updated with the
new correlation_id under "Run that confirmed the fix" once that lands.

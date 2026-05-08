---
id: TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-A
title: Apply lifecycle_bridge_registry migration in bind_production_serve Step 3.5b
status: completed
created: 2026-05-08T11:30:00Z
updated: 2026-05-08T12:30:00Z
completed: 2026-05-08T12:30:00Z
previous_state: in_review
state_transition_reason: All code-track ACs satisfied (AC-1, AC-2, AC-4); AC-3 SKIPPED-optional; AC-5 deferred to post-merge operator runbook re-run (matches parent task pattern)
completed_location: tasks/completed/forge-autobuild-runner-pipeline-emitter-bridge/
priority: high
task_type: fix
parent_review: TASK-REV-PEBR-004
parent_task: TASK-FORGE-FRR-PEBR-WIREUP
parent_feature: FEAT-PEBR
unblocks_parent_ac: TASK-FORGE-FRR-PEBR-WIREUP::AC-11
related_tasks:
  - TASK-FRR-PEB-002          # bridge skeleton + BridgeRegistry — owns the missing table
  - TASK-FORGE-FRR-PEBR-WIREUP # parent fix; this lands in the same Step 3.5b block
complexity: 1
estimated_minutes: 5
estimated_test_minutes: 15
implementation_mode: task-work
wave: 1
intensity: light
intensity_reason: provenance=parent_review (TASK-REV-PEBR-004), complexity=1, single-line edit + single regression test, no high-risk keywords
tags:
  - forge-serve
  - lifecycle-bridge
  - production-binding
  - migration-wireup
  - feat-pebr
  - pebr-wireup-followup
  - first-real-run-followup
  - regression-protection
discovered_during: TASK-REV-PEBR-004 (jarvis runbook RUNBOOK-FEAT-JARVIS-INTERNAL-001 post-PEBR-WIREUP revalidation, 2026-05-08)
forge_head_at_discovery: 1b82236
test_results:
  status: passed
  coverage: null  # not measured (light intensity skips coverage gate)
  last_run: 2026-05-08T12:15:00Z
  file_results:
    tests/forge/test_cli_serve_production.py: 20/20 passed
  related_slice:
    selector: tests/forge/ -k 'serve or lifecycle_bridge or migration'
    result: 419 passed, 0 failed
  reverse_test_verified: true  # confirmed FAIL→PASS by temporary revert of the source edit
---

# Fix FOLLOWUP-A — apply `lifecycle_bridge_registry` migration in `bind_production_serve` Step 3.5b

## TL;DR

[`src/forge/cli/_serve_production.py:445-468`](../../../src/forge/cli/_serve_production.py#L445) Step 3.5b applies `_bridge_coexistence.apply_migration(connection)` (line 468) but does **not** apply `forge.persistence.migrations.lifecycle_bridge_registry.apply(connection)`. On a fresh `FORGE_DB_PATH` volume, the `lifecycle_bridge_registry` table is missing, so `BridgeRegistry`'s first SQL touch raises:

```
register_ack_handle raised (no such table: lifecycle_bridge_registry); continuing with legacy ack_callback fallback
```

The legacy `ack_callback` fallback acks-on-dispatch-return — exactly the redelivery-storm closure the bridge was built to replace. This is **the AC-11 catch** from the parent task: caught by post-merge runbook revalidation before promotion to `completed/`.

## Why

Parent fix [TASK-FORGE-FRR-PEBR-WIREUP](TASK-FORGE-FRR-PEBR-WIREUP-fix.md) AC-11 (deferred at merge) requires the rebuilt image to publish a real `pipeline.build-started.FEAT-*` envelope and JetStream `ack_floor` to advance past the inbound. Until this fix lands, every dispatch logs the migration-drift line above and the wire stays empty.

The migration source [src/forge/persistence/migrations/lifecycle_bridge_registry.py:95-139](../../../src/forge/persistence/migrations/lifecycle_bridge_registry.py#L95) exposes a clean idempotent `apply(connection)` (uses `CREATE TABLE IF NOT EXISTS`, also chains the `published_lifecycles` follow-up migration). It just needs to be called.

## Acceptance Criteria

- [x] **AC-1** — **Composer wireup.** Extend [src/forge/cli/_serve_production.py:445-468](../../../src/forge/cli/_serve_production.py#L445) Step 3.5b to call `lifecycle_bridge_registry.apply(connection)` immediately after the existing `_bridge_coexistence.apply_migration(connection)` line. Match the existing import pattern (lazy import at module top alongside `_bridge_coexistence`, or local import inside the function — match whichever the existing module uses for `_bridge_coexistence`). The new call must be idempotent (the migration's `CREATE TABLE IF NOT EXISTS` already guarantees this).

- [x] **AC-2** — **Regression test.** Extend `tests/forge/test_cli_serve_production.py::TestLifecycleBridgeWireupComposition` (line 777) with a new test that:
  - Calls `bind_production_serve()` against a fresh `tmp_path` SQLite database.
  - After return, queries `sqlite_master` and asserts BOTH tables exist:
    - `lifecycle_bridge_terminal_publishes` (already covered by FOLLOWUP-pre AC-4)
    - `lifecycle_bridge_registry` (the new check)
  - Test must FAIL on pre-fix HEAD `1b82236` (verify by reverting the fix locally and re-running) and PASS after the fix.
  - Use the same fixture pattern as the existing `TestLifecycleBridgeWireupComposition` tests (real `sqlite3.connect(":memory:")` per the parent task's plan_audit note about the fixture upgrade).

- [~] **AC-3** — **Boot-log breadcrumb (optional, low value).** SKIPPED — the migration's own `logger.debug("applied %s migration", TABLE_NAME)` line at [lifecycle_bridge_registry.py:139](../../../src/forge/persistence/migrations/lifecycle_bridge_registry.py#L139) is sufficient; AC text marked this OPTIONAL.

- [x] **AC-4** — **Lint + format clean** on touched files (`ruff check`, `black --check`).

- [ ] **AC-5** *(deferred to operator post-merge — matches parent task pattern; resolves once image rebuilt and runbook re-run)* — **Runbook re-validation handoff.** After this task lands and is built into a forge image, an operator re-runs Phase 7 of `RUNBOOK-FEAT-JARVIS-INTERNAL-001-first-real-run.md` and captures:
  - The `register_ack_handle raised (no such table: lifecycle_bridge_registry)` line is **gone** from forge logs;
  - JetStream `ack_floor` advances past the inbound (`>=12`);
  - Either: (a) a real `pipeline.build-started.FEAT-*` envelope appears on the wire (FOLLOWUP-B is also resolved or never applied), OR (b) the bridge attaches but the translator is still silent (FOLLOWUP-B is the active gap).
  - Outcome captured in a new `RESULTS-…-post-followup-A.md` runbook results doc; this AC is satisfied when **either** (a) is observed (FOLLOWUP-B vacuous) **or** (b) is observed and FOLLOWUP-B is confirmed as the next gap.

## Implementation Notes

- **Single-line change.** The new call is one line inside Step 3.5b. Use the same indentation / comment style as the existing `_bridge_coexistence.apply_migration(connection)` line. Add a 2-3 line block comment explaining that the migration is co-located with the bridge code (mirrors the rationale comment already in Step 3.5b for the coexistence migration).

- **Import style.** Check whether `_bridge_coexistence` is imported at module top or locally; match that for `lifecycle_bridge_registry`. Both modules live under `forge.persistence.migrations` (the registry migration) and `forge.lifecycle_bridge.coexistence` (the coexistence migration); do not refactor the import style of one to match the other unless the test suite forces it.

- **Idempotence.** The migration's DDL is `CREATE TABLE IF NOT EXISTS` plus index creation under `IF NOT EXISTS`, so a second call within the same process is safe. The follow-up `published_lifecycles` migration (chained at [lifecycle_bridge_registry.py:133-137](../../../src/forge/persistence/migrations/lifecycle_bridge_registry.py#L133)) is also idempotent.

- **Out of scope.** Do NOT touch any other migration call site. Do NOT refactor Step 3.5/3.5b structure. Do NOT add new tests beyond the one regression test in AC-2. This is a **one-line fix + one test**; any additional change is scope creep and belongs in a separate task.

## Inputs / Evidence

- **Parent review**: [TASK-REV-PEBR-004](TASK-REV-PEBR-004-pebr-wireup-runbook-revalidation-followups.md) — diagnosis confirmed unambiguous.
- **Composer**: [src/forge/cli/_serve_production.py:445-468](../../../src/forge/cli/_serve_production.py#L445) — the Step 3.5b block.
- **Migration source**: [src/forge/persistence/migrations/lifecycle_bridge_registry.py](../../../src/forge/persistence/migrations/lifecycle_bridge_registry.py) — `apply(connection)` to invoke.
- **Test target**: `tests/forge/test_cli_serve_production.py::TestLifecycleBridgeWireupComposition` (line 777).
- **Operator log**: forge prod logs on HEAD `1b82236`, `register_ack_handle raised (no such table: lifecycle_bridge_registry); continuing with legacy ack_callback fallback`.

## References

- [TASK-REV-PEBR-004](TASK-REV-PEBR-004-pebr-wireup-runbook-revalidation-followups.md) — parent review that scoped this fix.
- [TASK-FORGE-FRR-PEBR-WIREUP](TASK-FORGE-FRR-PEBR-WIREUP-fix.md) — parent fix; this followup unblocks its AC-11.
- [TASK-FRR-PEB-002](../../completed/TASK-FRR-PEB-002-bridge-skeleton-and-registry.md) — defines `BridgeRegistry` (the consumer of the migration).

## Completion Summary

Completed via `/task-work` (light intensity) → `/task-complete` on 2026-05-08, working from forge HEAD `1b82236`.

### Code delta

- `src/forge/cli/_serve_production.py` — module-top import (`from forge.persistence.migrations import lifecycle_bridge_registry as _bridge_registry_migration`) plus single-line call `_bridge_registry_migration.apply(connection)` in Step 3.5b directly after the existing coexistence-migration call. Block comment explains the redelivery-storm rationale (somewhat longer than the AC-1 "2-3 line" suggestion — matches surrounding Step 3.5/3.5b comment density).
- `tests/forge/test_cli_serve_production.py` — new `TestLifecycleBridgeWireupComposition::test_bind_production_serve_creates_lifecycle_bridge_registry_table` pinning both bridge tables in `sqlite_master` after a real on-disk `bind_production_serve` (mirrors `test_bind_production_serve_logs_wired_not_deferred`'s tmp_path fixture pattern).

### Verification

- File-level: `tests/forge/test_cli_serve_production.py` — 20/20 pass.
- Broader slice: `tests/forge/ -k 'serve or lifecycle_bridge or migration'` — 419 passed, 0 failed.
- Reverse-test: temporary revert of the source edit reproduces the failure (`'lifecycle_bridge_registry' not in {builds, lifecycle_bridge_terminal_publishes, schema_version, sqlite_sequence, stage_log}`); restoring the edit returns to green. Regression-protection seam is genuine.
- Lint: `ruff check` clean, `black --check` clean.

### AC-5 handoff (deferred)

Post-merge operator action remains required to satisfy AC-5: re-run Phase 7 of `RUNBOOK-FEAT-JARVIS-INTERNAL-001-first-real-run.md` against the rebuilt image and capture results in `RESULTS-…-post-followup-A.md`. Outcome decides whether `TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-B` (silent translator spike) is the active gap or vacuous.

### Plan-audit notes (light = ±50% variance)

- Files: planned 2, actual 2 ✓
- Dependencies: planned 0 new, actual 0 ✓
- LOC: 88 total (+16 source, +72 test) — variance concentrated in test docstring (matches `TestLifecycleBridgeWireupComposition` convention) and source-side block comment (11 lines vs AC-1's "2-3 line" suggestion). No scope creep beyond AC-1 + AC-2.
- Severity: low.

### Code change not yet committed

Source edits and the moved task file are uncommitted in the working tree at completion time. The user has the project rule "create commits only when explicitly requested" — they will land the commit at their discretion (suggested message scope: `fix(FEAT-PEBR): apply lifecycle_bridge_registry migration in bind_production_serve Step 3.5b`).

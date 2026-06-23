# FEAT-RSP — Runbook and Step Persistence

The data model and persistence layer for the Forge **output-side loop**: a
typed `Step` and `Runbook` persisted durably in Forge's existing SQLite
store via sibling tables, so a build's step-by-step progress survives a
crash and can be resumed from where it left off.

**Scope:** data model + persistence + repository methods only
(`create_runbook`, `load_runbook`, `update_step_status`, `advance`).
**Not in scope:** executor, NATS, subprocess, LLM. Gates are modelled as
data (`awaiting_approval`) but nothing acts on them this phase.

## Tasks

| ID | Title | Wave | Type | Complexity | Mode |
|----|-------|------|------|------------|------|
| TASK-RSP-001 | Step and Runbook data models | 1 | declarative | 4 | task-work |
| TASK-RSP-002 | Runbook store migration | 1 | feature | 5 | task-work |
| TASK-RSP-003 | Repo: create_runbook + load_runbook | 2 | feature | 6 | task-work |
| TASK-RSP-004 | Repo: update_step_status + advance | 3 | feature | 6 | task-work |
| TASK-RSP-005 | Security + data-integrity tests | 4 | testing | 5 | task-work |
| TASK-RSP-006 | Concurrency + integration-boundary tests | 4 | testing | 6 | task-work |

Provenance: every task carries `parent_review: TASK-REV-RSP-001` and
`parent_feature: FEAT-RSP`.

## New / touched modules

```
src/forge/persistence/repositories/runbook_models.py   # TASK-RSP-001 (new)
src/forge/persistence/repositories/runbook.py          # TASK-RSP-003 (new), TASK-RSP-004 (extends)
src/forge/persistence/repositories/__init__.py         # exports
src/forge/persistence/migrations/runbook.py            # TASK-RSP-002 (new)
src/forge/persistence/migrations/__init__.py           # exports
tests/forge/persistence/test_runbook_models.py         # TASK-RSP-001
tests/forge/persistence/test_runbook_migration.py      # TASK-RSP-002
tests/forge/persistence/test_runbook.py                # TASK-RSP-003 / 004
tests/forge/persistence/test_runbook_security.py       # TASK-RSP-005
tests/forge/persistence/test_runbook_concurrency.py    # TASK-RSP-006
```

Reuses (no changes): `forge.adapters.sqlite.connect`
(`connect_writer` / `read_only_connect` / `SQLiteConnectError`),
`forge.lifecycle.migrations.apply_at_boot`.

## Execution

```bash
# Autonomous build (all waves, Player ↔ Coach)
/feature-build FEAT-RSP

# Or work tasks manually, wave by wave
/task-work TASK-RSP-001    # wave 1 (parallel with 002)
/task-work TASK-RSP-002
/task-work TASK-RSP-003    # wave 2
/task-work TASK-RSP-004    # wave 3
/task-work TASK-RSP-005    # wave 4 (parallel with 006)
/task-work TASK-RSP-006
```

See `IMPLEMENTATION-GUIDE.md` for the data-flow diagram, integration
contracts (§4), the task dependency graph, and the locked assumptions.

## Notes

- **Zero operator-handoff tasks** — every scenario is pure-unit and
  AutoBuild-satisfiable (`tmp_path` SQLite; concurrency simulated
  in-process with threads).
- The exemplar to copy is
  `src/forge/persistence/repositories/bridge_registry.py` and its tests
  `tests/forge/persistence/test_bridge_registry.py`.

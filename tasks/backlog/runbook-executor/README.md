# FEAT-RBX — Runbook Executor

The dispatch-by-`step_type` execution loop that sits **on top of** FEAT-RSP
(Runbook and Step Persistence). For each step the executor resolves a handler
from a step-type registry, runs it, persists the result + status, and advances
the resume pointer; on failure it stops and a later run re-enters at the failed
step (no restart). It announces lifecycle events — `runbook-started` /
`step-started` / `step-result` / `runbook-complete` / `escalated` — reusing the
`pipeline_publisher` envelope pattern (fire-and-forget; a publish failure logged
but never rolled back). Driven by `forge runbook run <path-to-runbook-json>`.

**Scope:** registry + executor loop + publisher + CLI + scenario tests.
**Not in scope:** concrete step handlers (registry indirection only),
subprocess, LLM, retry policy, any new SQL.
**Depends on:** **FEAT-RSP — build it first.**

## Tasks

| ID | Title | Wave | Type | Complexity | Mode |
|----|-------|------|------|------------|------|
| TASK-RBX-001 | Step-type registry + handler protocol | 1 | feature | 4 | task-work |
| TASK-RBX-002 | Runbook lifecycle events + payloads (nats-core) | 1 | declarative | 4 | task-work |
| TASK-RBX-003 | RunbookPublisher (mirror pipeline_publisher) | 2 | feature | 5 | task-work |
| TASK-RBX-004 | Executor dispatch loop (core) | 3 | feature | 7 | task-work |
| TASK-RBX-005 | CLI: forge runbook run <path> | 4 | feature | 5 | task-work |
| TASK-RBX-006 | Security & data-integrity tests | 5 | testing | 5 | task-work |
| TASK-RBX-007 | Concurrency & real-broker integration tests | 5 | testing | 4 | task-work |

Provenance: every task carries `parent_review: TASK-REV-RBX-001` and
`parent_feature: FEAT-RBX`.

## New / touched modules

```
src/forge/executor/__init__.py                       # TASK-RBX-001 (new)
src/forge/executor/registry.py                       # TASK-RBX-001 (new)
src/forge/executor/executor.py                       # TASK-RBX-004 (new)
src/forge/adapters/nats/runbook_publisher.py         # TASK-RBX-003 (new)
src/forge/cli/runbook.py                             # TASK-RBX-005 (new)
src/forge/cli/main.py                                # TASK-RBX-005 (+1 line)
pyproject.toml                                       # TASK-RBX-001 (marks)
tests/forge/executor/test_registry.py               # TASK-RBX-001
tests/forge/executor/test_executor.py               # TASK-RBX-004
tests/forge/test_runbook_publisher.py               # TASK-RBX-003
tests/forge/test_cli_runbook.py                      # TASK-RBX-005
tests/bdd/test_runbook_executor.py                   # TASK-RBX-006 / 007
tests/bdd/test_runbook_executor_integration.py       # TASK-RBX-007 (@integration @slow)

# Sibling package (TASK-RBX-002):
../nats-core/src/nats_core/envelope.py               # +5 EventType members
../nats-core/src/nats_core/events.py                 # +5 payloads + registry
```

Reuses (no changes): `forge.adapters.nats.pipeline_publisher`
(`PipelinePublisher` shape, `PublishFailure`, `SOURCE_ID`),
`forge.cli.main:main` Click group, the FEAT-RSP repository surface +
`Runbook`/`Step`/`StepStatus` models.

## ⚠️ Cross-feature reconciliation (do before building)

FEAT-RBX's ASSUM-004 (pointer rests at `== step_count`) conflicts with
FEAT-RSP's locked ASSUM-004 (refuse advancing past the last step). Resolution:
**R1 — relax persistence** so `advance()` may reach the terminal `== count`
position. ⚠️ This requires amending FEAT-RSP, whose ASSUM-004 is
**human-confirmed BDD** — so it is **NOT auto-applied**; apply it atomically
**before building either feature**. The exact per-file edits are in
`IMPLEMENTATION-GUIDE.md` §Cross-feature reconciliation.

## Execution

```bash
# Build the prerequisite first
/feature-build FEAT-RSP

# Then the executor (all waves, Player ↔ Coach)
/feature-build FEAT-RBX

# Or work tasks manually, wave by wave
/task-work TASK-RBX-001    # wave 1 (parallel with 002)
/task-work TASK-RBX-002
/task-work TASK-RBX-003    # wave 2
/task-work TASK-RBX-004    # wave 3
/task-work TASK-RBX-005    # wave 4
/task-work TASK-RBX-006    # wave 5 (parallel with 007)
/task-work TASK-RBX-007
```

See `IMPLEMENTATION-GUIDE.md` for the data-flow diagram, the integration-
contract sequence, the task dependency graph, and the §4 contracts.

## Notes

- **Zero operator-handoff tasks** — every default-suite scenario is
  AutoBuild-satisfiable with in-memory fake handlers (`tmp_path` SQLite;
  concurrency simulated in-process). The one real-broker scenario is
  `@integration @slow` and excluded from the default run.
- The exemplar to copy for the publisher is
  `src/forge/adapters/nats/pipeline_publisher.py` and its test
  `tests/forge/test_pipeline_publisher.py`.

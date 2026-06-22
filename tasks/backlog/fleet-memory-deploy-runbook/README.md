# Feature: Fleet-memory Deploy Runbook (FEAT-FMDR / FORGE-OL-04)

The output-side-loop's first harvested exemplar. A typed two-step runbook
(`deploy_compose` → `run_smoke_tests`, no gates) stands fleet-memory up through
`forge runbook run` and verifies it with the existing smoke script. Closes
TASK-MEM-008 and ticks FEAT-MEM-01's NAS-deploy acceptance criterion.

- **Review:** TASK-REV-FMDR
- **Spec:** `features/fleet-memory-deploy-runbook/` (21 scenarios, 7 assumptions — all confirmed)
- **Upstream dependency (merged on `main`):** FEAT-SSH — `register_shell_handlers`,
  `deploy_compose`, `run_smoke_tests` in `src/forge/executor/shell_steps.py`.

## Tasks

| ID | Title | Type | Wave | Cx |
|----|-------|------|------|----|
| TASK-FMDR-001 | Author `RUNBOOK-fleet-memory-nas.json` exemplar + round-trip test | declarative | 1 | 3 |
| TASK-FMDR-002 | Wire `forge runbook run` to real handlers + real NATS publisher | feature | 1 | 6 |
| TASK-FMDR-006 | Coordinated fleet-memory: `deploy/local` deploy.sh + smoke.sh wrappers | **operator_handoff** | 1 | 2 |
| TASK-FMDR-003 | Scenario suite (scripted handlers, CI-safe) | testing | 2 | 6 |
| TASK-FMDR-004 | Disposable-compose end-to-end run (`deploy/local`, Docker-required) | testing | 2 | 5 |
| TASK-FMDR-005 | Real-NAS stand-up — **operator_handoff** (closes TASK-MEM-008) | operator_handoff | 3 | 2 |

## Execution

- **Wave 1:** 001 + 002 (parallel, automated) · **006** (operator_handoff — sibling-repo
  wrappers; operator lands these before the build reaches Wave 2)
- **Wave 2 (parallel):** 003 + 004 (depend on Wave 1; 004 also depends on 006)
- **Wave 3:** 005 (operator runs manually; AutoBuild skips)

## Operator follow-up tasks: 2

- **TASK-FMDR-006** — add `deploy/local/{deploy.sh,smoke.sh}` in the sibling fleet-memory
  repo (AutoBuild can't edit sibling repos). Must be committed before Wave 2.
- **TASK-FMDR-005** — real-NAS stand-up; a one-shot operational act, not a repeatable test.

See `/feature-complete` for the post-merge checklist.

## Watch-outs

- The `deploy.sh` / `smoke.sh` scripts live in the **sibling `fleet-memory` repo**,
  consumed as-is — an AutoBuild worktree cannot edit them. The NAS scripts are SSH-only;
  the local target had **no wrappers**, which is why TASK-FMDR-006 exists.
- **Docker Desktop must be running** for TASK-FMDR-004 (it fails, not skips, when the
  daemon is down). On this MacBook Desktop is installed — just launch it.
- See `IMPLEMENTATION-GUIDE.md` §4 for the `RUNBOOK_STEP_PARAMS`, `.env.deploy`, and local
  wrapper integration contracts.

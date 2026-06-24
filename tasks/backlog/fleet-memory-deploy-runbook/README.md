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
| TASK-FMDR-005 | Real-NAS stand-up — **operator_handoff** (closes TASK-MEM-008) — ✅ **DONE** | operator_handoff | 3 | 2 |
| TASK-FMDR-007 | Fix shell-step script/cwd resolution (handler can't run bare `deploy.sh`) | feature | 3 | 3 |
| TASK-FMDR-008 | Wire NATS auth into `forge runbook run` (live "events in order" sub-AC) | feature | 3 | 3 |

## 2026-06-23 — Wave 3 COMPLETE — fleet-memory Postgres+pgvector live on the real NAS ✅

`forge runbook run` stood fleet-memory up on the GB10's NAS (`whitestocks`) — **all gates green**
(deploy G2; smoke G3 pgvector 0.8.3 / G4 network on 5433 / G5 backed-up volume). **Closes
TASK-MEM-008; ticks FEAT-MEM-01's NAS AC.** See the "RESOLVED — 2026-06-23" section in
`TASK-FMDR-005-real-nas-standup-operator-handoff.md`.

Getting there took 007/008 (forge: bare-script resolution + NATS auth/no-spin) **plus** a stack of
NAS-environment + deploy-script fixes the never-run-against-a-real-Synology scripts had latent
(DSM rsync service, Compose V2, port 5432→5433 vs DSM's own Postgres, `./pgdata`+mkdir, initdb
rsync trailing-slash, smoke G3 readiness/G5 perms, host psql). The `deploy/nas` fixes are
**committed** in the sibling fleet-memory repo (`e83e4bc`). Fourth+ instance of the false-green
pattern (`docs/reviews/FEAT-FMDR-autobuild-false-green-analysis.md`): tests/local-target green, but
the real Synology path was never exercised until this run. Also: `forge runbook run` needs
`~/.forge/` created and the runbook migration applied first — no shipped boot path does this.

**2026-06-24 — NATS integration + full session record.** forge migrated to a dedicated `forge`
NATS user; lifecycle events verified publishing in order to the live broker. Consolidated record of
every Synology/NATS/runbook gotcha from this work: **`docs/handoffs/FMDR-NATS-SESSION-DISCOVERIES-2026-06-24.md`**.

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

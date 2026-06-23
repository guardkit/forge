# Output-Side Loop — Exemplar Scope: Minimal Runbook Executor Deploying fleet-memory

## For: Claude Code `/feature-spec` → `/feature-plan` → AutoBuild (per-feature)
## Generated: 21 June 2026
## Status: **Not started.** First phase of the Forge output-side loop. Upstream context: `docs/handoffs/forge-output-loop-conversation-starter.md` (runbook model + D1–D15). Strategic anchor: `factory-scaling-and-output-bottleneck-findings.md` (D11–D15).
## Deliberately below a `/system-arch` pass: per D13 (harvest the engine, don't pre-build it) and "exemplar before template", this is a focused scope + build-plan for the minimal executor proven on one real deploy. The full output-loop `/system-arch` generalises from this exemplar later.
## Companion: [output-loop-exemplar-build-plan.md](output-loop-exemplar-build-plan.md) — feature-by-feature breakdown, prefilled spec commands, sequence, risks.

---

## Thesis

Phase exists to test one claim:

> Forge can stand up a real, already-built service — fleet-memory on the NAS — by executing a typed, persisted **runbook** through a **minimal dispatch-by-step-type executor**, where the two steps **wrap fleet-memory's existing `deploy.sh` and `smoke.sh`** and the smoke script's exit code is the verdict. Walk away with **fleet-memory deployed** (closing TASK-MEM-008) and a **reusable executor plus two harvested step types** — with no general "runbook engine", no approval gates, no fix-agent, and no resolution of the DF-001 substrate question required, because fleet-memory's deploy is local, reversible, and needs none of them.

If true: the executor and the two step types exist, the runbook record plus NATS lifecycle events exist (the dashboard-projectable spine), fleet-memory is live on the NAS, and the LPA target becomes "the same executor plus the step types LPA forces into existence" (`await_approval`, a credential step, `invoke_claude_code_debug`). If the deploy/smoke wrapping proves the wrong abstraction, that is recorded before any gate-bearing target is attempted — the cost is one local redeploy.

## Why now

| Driver | Evidence |
|---|---|
| The output side is the bottleneck | findings D11–D15; development is fast (AutoBuild done), the constraint moved to deploy/integrate/verify |
| fleet-memory's deploy already exists — pure harvest | `deploy/nas/{deploy.sh,smoke.sh,docker-compose.yml,initdb/}` built and idempotent; `RUNBOOK-nas-postgres-deploy.md` written; `TASK-MEM-008` (operator execution) sitting in backlog — the exact "needs an operator to run it" gap the loop fills |
| The exemplar has zero blast radius | NAS container, reversible, no external deps, no credentials beyond a local `.env.deploy` — the safe place to prove the executor before LPA's real AWS edges |
| fleet-memory needs standing up anyway | FEAT-MEM-01's NAS-deploy AC is the one open item; this delivers it as a by-product of building the executor |

## What already exists (treat as fixed)

| Capability | Where | State |
|---|---|---|
| fleet-memory deploy script (idempotent; SSH + rsync + compose; GATE G2 container health; exit non-zero on fail) | `fleet-memory/deploy/nas/deploy.sh` | built, productized from the runbook |
| fleet-memory smoke script (GATES G3–G5: pg_isready + pgvector, network path, backed-up volume; exit non-zero on any fail) | `fleet-memory/deploy/nas/smoke.sh` | built |
| fleet-memory deploy runbook (human operator, phased, PASS/FAIL gates) | `fleet-memory/docs/runbooks/RUNBOOK-nas-postgres-deploy.md` | written |
| Forge NATS-native orchestrator: serve daemon, dispatch, SQLite lifecycle + state machine, NATS publishers (pipeline/approval/fleet) | `forge/src/forge/` | live |
| GuardKit AutoBuild subprocess adapter + the `run_autobuild` seam (for later) | `forge/src/forge/adapters/guardkit/run.py` + `subagents/autobuild_runner.py` | exists (placeholder node body) |
| Subprocess dispatch pattern the step types follow | `forge/src/forge/pipeline/dispatchers/subprocess.py` | exists |
| AutoBuild (Player-Coach loop) | guardkit | done (local + Claude SDK) |

## What this phase adds — at scope level

Detailed tasks live in the build plan. Scope-level shape:

| # | Feature | Why |
|---|---|---|
| FORGE-OL-01 | Runbook & Step model + SQLite persistence | The unit of work as a typed, persisted record (D11/D13/D14) — the dashboard projects it; gates-as-data; status per step. Adopt the data model from day one even though the executor stays minimal. |
| FORGE-OL-02 | Minimal executor + NATS lifecycle events | Dispatch-by-step-type loop; resume-on-failure = re-enter at step N; publish `runbook-started / step-started / step-result / runbook-complete / escalated` on the existing NATS spine. Minimal — no engine. |
| FORGE-OL-03 | Step types `deploy_compose` + `run_smoke_tests` | Thin subprocess wrappers around fleet-memory's existing `deploy.sh`/`smoke.sh`; typed params (cwd, script, env-file); result = exit code + captured output; the smoke exit code **is** the verdict (the environment is the Coach). The two types fleet-memory needs; the library starts here. |
| FORGE-OL-04 | fleet-memory runbook + stand-up | Hand-author the 2-step typed runbook (`deploy_compose → run_smoke_tests`, no gates); run the executor; fleet-memory live on the NAS; closes TASK-MEM-008 + FEAT-MEM-01's NAS AC. The exemplar payoff. |

## Success criteria

1. **Executor stands fleet-memory up unattended.** Forge executes the fleet-memory runbook end-to-end: `deploy_compose` runs `deploy.sh` (GATE G2 passes), `run_smoke_tests` runs `smoke.sh` (GATES G3–G5 pass), runbook completes green — no manual step between.
2. **fleet-memory is live on the NAS.** Postgres + pgvector up, reachable over LAN/Tailscale, data on the backed-up volume — i.e. TASK-MEM-008 done and FEAT-MEM-01's open AC ticked.
3. **The runbook is a persisted, projectable record.** Status per step in forge's SQLite; the NATS lifecycle events fire (the dashboard could render "what's next" from them).
4. **Resume-on-failure works.** Kill after `deploy_compose`; re-running re-enters at `run_smoke_tests`, not from the top.
5. **The step library is harvested, not authored.** `deploy_compose`/`run_smoke_tests` wrap the existing scripts; no deploy logic is reimplemented in forge.
6. **The abstraction holds for the next target on paper.** The LPA jump is a change in runbook *content* (adds `await_approval`, a credential step, `invoke_claude_code_debug`), not executor *code* — confirmed by walking LPA's runbook against the same executor.

## Out of scope (named — these are what later targets force into existence)

| Concern | Why deferred |
|---|---|
| `run_autobuild` step type (the UBS-001 seam: wire `autobuild_runner.py` to the guardkit adapter) | fleet-memory is **already built**; this phase deploys, it does not build. The first runbook that must build-then-deploy forces this step. |
| `invoke_claude_code_debug` (supervised-loop step) | A Postgres container deploy has no code to fix in-loop; smoke is pass/fail. LPA / app deploys force the supervised-loop type. |
| `await_approval` (approval-gate step) | fleet-memory is local and reversible — no irreversible edge to gate. LPA's AWS/credential edges force the gate-step (and its shared-with-Slack contract). |
| Claude Code generating the runbook (`generate.py`) | Per D13, you harvest generation patterns *after* a couple of hand-authored runbooks exist. Hand-author fleet-memory's; defer the generator. |
| The general runbook engine | D13 / warnings: the engine **emerges** from two or three real runbooks. Pre-building it is the over-engineering the policy forbids. |
| Resolving the DF-001 fix-agent substrate question | There is no fix-agent in fleet-memory's runbook, so the load-bearing open question does not block this phase. It is resolved when `invoke_claude_code_debug` is built for LPA. |
| Dashboard rendering of the runbook | This phase emits the record + events; the dashboard that projects them is a separate build (presentation layer). |

## Architectural constraints (must NOT be violated)

- **Typed steps, not freehand shell.** Even hand-authored, the runbook composes *typed steps* (`deploy_compose`, `run_smoke_tests`) that wrap vetted scripts — it never carries raw inline shell. This is the safety property that carries to the irreversible targets (D12).
- **Minimal executor; harvest the library.** Adopt the runbook data model + persisted record now; keep the executor a dispatch loop; let the step library accrete. No speculative engine (D13).
- **Idempotency / safe-to-re-run.** `deploy.sh` is already idempotent; `deploy_compose` must preserve that (re-run = no-op if healthy). No autonomous step may have an un-undoable effect (here, none do).
- **NATS is the spine.** Lifecycle events are producers on the existing bus (ADR-SP-002); no new brain. Reuse the existing publisher pattern.
- **Subprocess for execution.** Step types invoke scripts as subprocesses (the established Build Agent → GuardKit pattern, ADR-SP-003); Forge owns the lifecycle.
- **Credential scoping.** `.env.deploy` (the NAS password) is read by the script on disk; its values never enter the runbook record or the NATS events. The executor passes a path, not secrets.
- **House conventions.** Match forge's existing module idioms; the runbook record extends the existing lifecycle persistence rather than introducing a parallel store. Underscores in identifiers.

## Status snapshot — 21 June 2026

| Item | Status |
|---|---|
| fleet-memory deploy/smoke scripts + runbook | ✅ exist (`deploy/nas/`, `docs/runbooks/`) |
| TASK-MEM-008 (NAS operator execution) | ⬜ outstanding — this phase closes it |
| Forge orchestrator (serve, dispatch, SQLite, NATS) | ✅ live |
| Upstream conversation-starter (runbook model, D1–D15) | ✅ `docs/handoffs/` |
| FORGE-OL-01..04 | ⬜ Not started — prefilled spec commands in the build plan |

---

*Scope authored 21 June 2026 as the `output-loop-exemplar-{scope,build-plan}` pair, matching the GuardKit workflow used across sibling repos (fleet-memory, specialist-agent, study-tutor). Deliberately scoped below a `/system-arch` pass per the exemplar-before-template rule; the full output-loop architecture generalises from this exemplar once it exists.*

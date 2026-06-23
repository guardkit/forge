# Forge Output-Side Loop — Exemplar Build Plan: Minimal Executor Deploying fleet-memory

## Generated: 21 June 2026
## Companion: [output-loop-exemplar-scope.md](output-loop-exemplar-scope.md) — thesis, success criteria, in/out of scope, constraints
## Upstream: `docs/handoffs/forge-output-loop-conversation-starter.md` (runbook model + D1–D15); `factory-scaling-and-output-bottleneck-findings.md` (D11–D15)
## Prerequisites: fleet-memory `deploy/nas/{deploy.sh,smoke.sh}` built + idempotent (done); `.env.deploy` fillable from `.env.deploy.example` (NAS host/user/ssh-port/docker-root + PG password) with the `fleet_memory_nas_ed25519` SSH key in place; forge orchestrator live (serve, SQLite lifecycle, NATS publishers); NATS JetStream live.
## Status as of 2026-06-21: **Not started.** FORGE-OL-01 is the entry point.
## Plan-update convention (context-switch resilience):
##   - **After `/feature-spec` lands:** flip the row in Feature Summary to **Spec'd** + GuardKit id; add a `**Status:**` line atop that feature with the spec commit + `features/<slug>/` path.
##   - **After `/feature-plan`:** flip to **Plan'd**; add plan commit + `.guardkit/features/FEAT-XXXX.yaml` + task-tree path.
##   - **After build:** flip to **Landed**; tick ACs; add impl + closure commits; strike the Build Sequence entry.

---

## Context

The output-side loop's first exemplar. Full rationale is in the scope doc + the upstream conversation-starter; operational summary: development is fast (AutoBuild done), the bottleneck moved to deploy/verify (findings D11–D15). fleet-memory is the ideal first target because its deploy already exists as idempotent scripts (`deploy/nas/deploy.sh` + `smoke.sh`, productized from `RUNBOOK-nas-postgres-deploy.md`) and just needs an operator to run it (TASK-MEM-008, backlog). So this phase builds the **minimal runbook executor + two step types that wrap those scripts**, with fleet-memory as the live test subject — walking away with the executor *and* fleet-memory deployed. No engine, no gates, no fix-agent (the scope doc's Out-of-scope explains why each is deferred).

## Division of labour across repos

| Repo | Owns | This phase's deliverable |
|---|---|---|
| **forge (this work)** | The runbook model, executor, step library, NATS lifecycle events | FORGE-OL-01..04 |
| fleet-memory | The deploy/smoke scripts + the runbook the executor consumes | Already done; this phase **executes** them (closes TASK-MEM-008) |

(No code changes to fleet-memory are expected — `deploy.sh`/`smoke.sh` are consumed as-is. If a wrapper needs a flag they do not expose, that is a coordinated fleet-memory task created on explicit instruction.)

## What already exists

Forge gives the substrate the executor reuses: SQLite lifecycle + state machine (`src/forge/lifecycle/`, `adapters/sqlite/connect.py`) for the runbook record; NATS publishers (`adapters/nats/pipeline_publisher.py` as the pattern) for lifecycle events; the subprocess dispatch pattern (`pipeline/dispatchers/subprocess.py`) for the step types; and the guardkit adapter + `subagents/autobuild_runner.py` for the *later* `run_autobuild` step. fleet-memory gives the deploy: idempotent `deploy.sh` (SSH + rsync + `docker compose up`, GATE G2) and `smoke.sh` (GATES G3–G5, exit non-zero on fail).

## What this phase adds

Four features, strictly sequential (01 → 02 → 03 → 04). 01 is the data model + persistence; 02 is the executor + events; 03 is the two step types; 04 hand-authors fleet-memory's runbook and runs it.

## Feature Summary

| Feature | Title | Status | GuardKit ID |
|---|---|---|---|
| FORGE-OL-01 | Runbook & Step model + SQLite persistence | Not started | — |
| FORGE-OL-02 | Minimal executor + NATS lifecycle events | Not started | — |
| FORGE-OL-03 | Step types: deploy_compose + run_smoke_tests | Not started | — |
| FORGE-OL-04 | fleet-memory runbook + stand-up | Not started | — |

## Architectural Constraints (enforce in every spec)

- Typed steps only — no inline shell in a runbook (D12).
- Minimal executor; the library accretes (D13). No general engine.
- Idempotent / safe-to-re-run steps; preserve `deploy.sh`'s idempotency.
- NATS is the spine (ADR-SP-002); events are producers on the existing bus.
- Subprocess execution (ADR-SP-003); Forge owns the lifecycle.
- Credential scoping: secrets stay in `.env.deploy`; never in the record or events.
- Reuse forge idioms; extend the existing lifecycle persistence, do not fork a parallel store.

---

## FORGE-OL-01: Runbook & Step Model + SQLite Persistence

The unit of work as a typed, persisted record. `Step` (step_type, params, status, result, sequence_index); `Runbook` (runbook_id, ordered steps, current_step_index, overall status, target name; gates-as-data — a gate is a step whose status sits in `awaiting_approval`, though none appear in this phase). Persist to forge's existing SQLite lifecycle (a sibling table reusing `adapters/sqlite/connect.py` and the migration path) so status is queryable per step and the record is what the dashboard will later project (D14). Status transitions mirror the existing state-machine idiom.

### Spec & Plan Commands

```
/feature-spec "Runbook and Step data model for the Forge output-side loop: a typed Step (step_type, params dict, status enum pending/running/passed/failed/awaiting_approval, result with exit_code + captured_output + timestamps, sequence_index) and a Runbook (runbook_id, ordered list of steps, current_step_index, overall status, target name, created_at) persisted in forge's existing SQLite store via a sibling table reusing adapters/sqlite/connect.py and the existing migration path; status-per-step queryable; resume pointer (current_step_index); gates-as-data (a step in awaiting_approval) modelled even though this phase uses none; NO executor logic yet — model + persistence + repository methods (create_runbook, load_runbook, update_step_status, advance) with unit tests; no NATS, no subprocess, no LLM"
/feature-plan "Runbook and Step Model" --context features/<slug>/<slug>_summary.md
```

### Acceptance Criteria

- [ ] `Runbook` with N ordered typed `Step`s round-trips through SQLite (create → load → identical)
- [ ] `update_step_status` + `advance` move the current-step pointer; status queryable per step
- [ ] A step can hold `awaiting_approval` status (gates-as-data modelled, unused here)
- [ ] No NATS, no subprocess, no LLM in this feature (import-negative test); reuses the existing SQLite connection, not a new DB
- [ ] Underscores throughout; matches forge's lifecycle persistence idiom

## FORGE-OL-02: Minimal Executor + NATS Lifecycle Events

The dispatch-by-step-type loop (D11/D13). `execute(runbook)`: from `current_step_index`, resolve the step's handler via a `step_type` registry, run it, persist `result` + status via OL-01's repository, advance; on failure, stop with the runbook resumable at the failed index (resume = re-enter at step N, D14). Publish lifecycle events on the existing NATS spine (reuse the `pipeline_publisher` pattern + envelope): `runbook-started`, `step-started`, `step-result`, `runbook-complete`, `escalated`. The executor stays minimal — it dispatches and persists; it has no knowledge of *what* a step does (that is the step type).

### Spec & Plan Commands

```
/feature-spec "Minimal runbook executor for the Forge output-side loop: dispatch-by-step-type loop over a persisted Runbook (from the runbook model feature) — resolve each step's handler via a step_type registry, run it, persist result + status, advance the current-step pointer; stop-and-resume on failure (re-enter at the failed step index, no restart); publish NATS lifecycle events runbook-started/step-started/step-result/runbook-complete/escalated reusing the existing pipeline_publisher pattern and envelope; the executor has NO knowledge of step internals (registry indirection only); a CLI entry 'forge runbook run <path-to-runbook-json>' to load and execute; in-memory fake step handlers in tests so unit gates need no subprocess or NATS broker; one marker-gated integration test with a real NATS publish"
/feature-plan "Minimal Runbook Executor" --context features/<slug>/<slug>_summary.md
```

### Acceptance Criteria

- [ ] Executor runs a runbook of fake steps end-to-end, persisting status per step and advancing
- [ ] A failing step stops the runbook resumable at that index; re-execute re-enters there (no restart)
- [ ] Lifecycle events publish in order on NATS (integration test asserts subjects + sequence)
- [ ] Adding a step type requires only a registry entry — executor code unchanged (proven by registering a second fake type)
- [ ] No step internals in the executor; no LLM; reuses the existing NATS envelope

## FORGE-OL-03: Step Types — deploy_compose + run_smoke_tests

The first two library entries (D13), both thin subprocess wrappers (ADR-SP-003) around fleet-memory's existing scripts. `deploy_compose(cwd, script, env_file)`: run the script (`deploy.sh`) in `cwd` with `env_file` available, capture exit code + stdout/stderr, map exit 0 → passed / non-zero → failed; idempotency is the script's (preserve it). `run_smoke_tests(cwd, script, env_file)`: same shape around `smoke.sh` — its **exit code is the verdict** (GATES G3–G5; the environment is the Coach, D4); passed/failed, no debug loop in this phase. Credential scoping: params carry the env-file *path*, never its contents; captured output is scrubbed of DSN/password patterns before it lands in the result or events. Register both in OL-02's step-type registry.

### Spec & Plan Commands

```
/feature-spec "Two runbook step types wrapping existing shell scripts as subprocesses: deploy_compose(cwd, script, env_file) and run_smoke_tests(cwd, script, env_file) — each runs the named script in cwd with the env_file available, captures exit_code + stdout/stderr, maps exit 0 to passed and non-zero to failed (run_smoke_tests' exit code is the step verdict); preserves the script's own idempotency; credential-scoped — env_file is a path only, and captured output is scrubbed of postgres DSN and password patterns before being stored in the step result or published; both registered into the executor's step_type registry; unit tests with a fake script exercising the exit-0 and exit-1 paths plus an output-scrub assertion on a planted secret; one marker-gated integration test invoking fleet-memory deploy/nas/smoke.sh against a throwaway target"
/feature-plan "Deploy and Smoke Step Types" --context features/<slug>/<slug>_summary.md
```

### Acceptance Criteria

- [ ] `deploy_compose` runs a script, maps exit 0 → passed / non-zero → failed, captures output
- [ ] `run_smoke_tests` likewise; its exit code is the recorded verdict
- [ ] Captured output is scrubbed of DSN/password before storage or publish (negative test on a planted secret)
- [ ] Both register via the step-type registry; the executor runs a 2-step fake runbook through them
- [ ] Re-running `deploy_compose` against a healthy target is a no-op (idempotency preserved)

GUARDKIT_LOG_LEVEL=DEBUG GUARDKIT_HARNESS=sdk guardkit autobuild feature FEAT-SSH  --verbose

## FORGE-OL-04: fleet-memory Runbook + Stand-Up

The exemplar payoff. Hand-author the fleet-memory runbook as a typed 2-step record — `deploy_compose{cwd: fleet-memory/deploy/nas, script: deploy.sh, env_file: .env.deploy}` then `run_smoke_tests{cwd: …, script: smoke.sh, env_file: .env.deploy}`, no gates (local + reversible). Run it via `forge runbook run`. The executor stands fleet-memory up on the NAS; `smoke.sh` (G3–G5) is the green verdict. This closes TASK-MEM-008 and ticks FEAT-MEM-01's open NAS-deploy AC. Save the runbook JSON under `forge/runbooks/` as the first harvested exemplar.

**Pre-flight (verify before running).** The scripts assume they run from the *Mac* (`$HOME/.ssh/fleet_memory_nas_ed25519`), but Forge runs on the GB10 — so these must hold on **whichever host actually executes the runbook**:
- The `fleet_memory_nas_ed25519` SSH key is present in *that host's* `~/.ssh/` (perms 600) and the NAS trusts it (public key in the NAS user's `authorized_keys`). Key-based, `BatchMode=yes` — **no SSH password** anywhere.
- `.env.deploy` is present in the working dir on that host (from `.env.deploy.example`: `NAS_HOST`, `NAS_USER`, `NAS_SSH_PORT`, `NAS_DOCKER_ROOT`, `FLEET_MEMORY_PG_PASSWORD` — the NAS *user* and the *DB* password live here; the SSH credential is the key file, not a password).
- `.env.deploy` is gitignored — confirm `git check-ignore deploy/nas/.env.deploy` returns the path, so the DB password is never committed (the rsync `--exclude '.env.deploy*'` and the `.example` template imply it; verify anyway).
- That host can reach the NAS over Tailscale/LAN; NAS-container conventions unchanged (the scripts target `NAS_DOCKER_ROOT`).

**Resolve before the real-NAS run:** if Forge runs on the GB10 (per the hardware topology), provision the key + `.env.deploy` *there*, not on the Mac. The disposable-target e2e (OD-4) does not need any of this.

**NAS-side one-time setup (validated 21 June 2026 standing fleet-memory up against `whitestocks`).** The host-side checks above are necessary but not sufficient — the NAS itself needs three things, each a first-run trap hit during provisioning:
- **User Home service enabled** (DSM → Control Panel → User & Group → Advanced → *Enable user home service*). Without it the deploy user has no `/var/services/homes/<user>`, so there is nowhere to write `~/.ssh/authorized_keys` and key auth cannot even be installed (`ssh-copy-id` fails with `Could not chdir to home directory`).
- **Deploy user in the `administrators` group** (Synology SSH only accepts administrators-group accounts) **with passwordless sudo scoped to the docker binary** — a drop-in at `/etc/sudoers.d/fleet_memory_docker` containing `<NAS_USER> ALL=(ALL) NOPASSWD: /usr/local/bin/docker`, `chmod 440`, validated with `sudo visudo -c -f`. Both scripts call `sudo -n /usr/local/bin/docker` (the `-n` never prompts), so without this the deploy dies at "Start container". **The path must match exactly** — confirm with `which docker` on the NAS (it was `/usr/local/bin/docker` on `whitestocks`, matching the hardcoded path in both scripts); if a future NAS differs, the sudoers rule *and* the scripts must agree.
- **Go/no-go gate — verify non-interactively from the executor's host** (not via an interactive NAS login): `ssh -i ~/.ssh/fleet_memory_nas_ed25519 -o BatchMode=yes -p $NAS_SSH_PORT $NAS_USER@$NAS_HOST 'sudo -n /usr/local/bin/docker ps'` must return a container table. That one command proves key auth + admin group + passwordless docker-sudo together.

**Durability caveat:** DSM major upgrades can wipe `/etc/sudoers.d/` drop-ins. If a previously-working deploy suddenly demands a sudo password, this rule getting cleared is the first suspect. The runbook's pre-flight (and eventually `deploy_compose`'s failure handling) should *verify* this gate rather than assume it persists — itself signal for OL-03: a `sudo: a password is required` stderr is a known, diagnosable failure worth surfacing distinctly.

### Spec & Plan Commands

```
/feature-spec "Hand-authored fleet-memory deploy runbook plus executor stand-up: a typed 2-step runbook (deploy_compose then run_smoke_tests, both targeting fleet-memory/deploy/nas with .env.deploy, NO approval gates because the target is local and reversible) persisted via the runbook model and executed via 'forge runbook run'; on success fleet-memory Postgres+pgvector is live on the NAS with smoke gates G3-G5 green; the runbook JSON saved as the first harvested exemplar under forge/runbooks/; an end-to-end test that runs the executor against a disposable compose target (fleet-memory deploy/local, NOT the real NAS) proving deploy then smoke then runbook-complete; then the same executor (forge runbook run with the real .env.deploy) runs the runbook against the real NAS as the actual stand-up — not a manual ./deploy.sh — which closes TASK-MEM-008 and ticks FEAT-MEM-01's NAS acceptance criterion"
/feature-plan "Fleet-memory Deploy Runbook" --context features/<slug>/<slug>_summary.md
```

### Acceptance Criteria

- [ ] The 2-step runbook executes end-to-end against a disposable target: deploy_compose passed → run_smoke_tests passed → runbook-complete
- [ ] **The executor** (`forge runbook run`, real `.env.deploy`) runs the runbook against the real NAS and stands fleet-memory up — not a manual `./deploy.sh`; `smoke.sh` G3–G5 green; **TASK-MEM-008 closed**, FEAT-MEM-01 NAS AC ticked
- [ ] Killing after deploy_compose and re-running re-enters at run_smoke_tests (resume proven on the real runbook)
- [ ] The runbook record + lifecycle events are present (status-per-step queryable; events fired)
- [ ] The runbook JSON is saved as the harvested exemplar; no inline shell in it (typed steps only)

---

## Build Sequence

| Day | Focus |
|---|---|
| 1 | FORGE-OL-01 (model + SQLite persistence) — spec → plan → build |
| 2 | FORGE-OL-02 (executor + NATS events) |
| 3 | FORGE-OL-03 (the two step types wrapping deploy.sh/smoke.sh) |
| 4 | FORGE-OL-04 (fleet-memory runbook; stand it up on the NAS; close TASK-MEM-008) |

(Low-stakes, no external deadline — runs in parallel with / behind the LPA demo per the session wrap-up sequencing. Each feature is a clean `/feature-spec → /feature-plan → AutoBuild → /task-review` cycle.)

## Resolved Decisions

| # | Decision | Notes |
|---|----------|-------|
| RD-1 | Executor wraps existing scripts, does not reimplement deploy | `deploy.sh`/`smoke.sh` are the harvested step bodies (D13) |
| RD-2 | Runbook record extends forge's existing SQLite lifecycle (sibling table) | One persistence store, not a parallel one |
| RD-3 | fleet-memory's runbook is hand-authored, not generated | Per D13, harvest generation later; the exemplar needs no generator |
| RD-4 | No run_autobuild / gates / fix-agent in this phase | fleet-memory deploys (already built), is reversible, and its smoke is pass/fail — see scope Out-of-scope |
| RD-5 | The smoke exit code is the verdict | The environment is the Coach (D4); no debug loop here |
| RD-6 | CLI entry (`forge runbook run`) is the executor surface for the exemplar | A serve-handler / NATS-triggered entry is a later concern (when generation + the dashboard arrive) |

## Open Decisions

| # | Question | Recommendation | Resolve by |
|---|----------|----------------|------------|
| OD-1 | Runbook table: extend `schema_v2.sql` vs a sibling table | Sibling table reusing the same connection + migration path; least coupling to the existing lifecycle state machine | OL-01 spec |
| OD-2 | NATS subjects for runbook events | `forge.runbook.<event>` under the existing envelope; mirror the `pipeline_publisher` subject convention | OL-02 spec |
| OD-3 | Where the harvested runbook JSON lives | `forge/runbooks/` (sibling to the docs runbooks) as fixtures the executor loads | OL-04 spec |
| OD-4 | Disposable test target for the e2e | fleet-memory's own `deploy/local/` ephemeral compose as the throwaway target (keeps the real NAS out of CI) | OL-03/04 spec |

## Risks

| Risk | Mitigation |
|---|---|
| `deploy.sh`/`smoke.sh` assume an env (SSH key, `.env.deploy`) a subprocess wrapper trips on | OL-04 pre-flight verifies key + env presence; the wrapper passes env-file + cwd exactly as a shell run would; the e2e uses `deploy/local/` to de-risk before the NAS |
| Secret leakage into the runbook record/events via captured stdout | OL-03 scrubs DSN/password patterns before storage/publish; negative test on a planted secret |
| Extending forge's lifecycle SQLite couples the runbook to the build-state machine | RD-2/OD-1: a sibling table on the same connection, not an extension of the state machine; keeps concerns separate |
| The abstraction is wrong (deploy/smoke do not generalise) | Cost is one local redeploy; success criterion 6 walks LPA's runbook against the executor on paper before committing to the gate-bearing target |
| Scope creep into the engine / generator / gates | The scope doc's Out-of-scope is explicit; this plan stops at "fleet-memory deployed + 2 step types + persisted record" |

---

*Build plan authored 21 June 2026 as the `output-loop-exemplar` pair. Maintained per the plan-update convention; spec/plan/build invocations auto-captured to history by the capture hook. The full output-loop `/system-arch` generalises from this exemplar once it exists.*

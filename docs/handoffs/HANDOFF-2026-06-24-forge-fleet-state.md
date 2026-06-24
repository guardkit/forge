# HANDOFF — forge / fleet-memory / NATS (2026-06-24)

Context-continuation handoff (the originating conversation ran out of context). Everything below is
on disk / committed; a fresh session can resume from here without the prior chat.

## TL;DR — what this session accomplished

1. **TASK-FMDR-005 real-NAS stand-up COMPLETE.** fleet-memory Postgres 16 + pgvector 0.8.3 is
   **live on the Synology NAS** (`whitestocks:5433`, backed-up volume), stood up **by the forge
   runbook executor** (`forge runbook run forge/runbooks/RUNBOOK-fleet-memory-nas.json`). All smoke
   gates green. Closed TASK-MEM-008, ticked FEAT-MEM-01 NAS AC. (Required first fixing TASK-FMDR-007
   bare-script cwd bug + TASK-FMDR-008 NATS auth — both already done.)
2. **forge NATS identity migrated** off the human principal `rich` to a **dedicated, subject-scoped
   `forge` user** in the APPMILLA account. Lifecycle events verified publishing **in order** to the
   live broker.
3. **Follow-up 2 (done):** rebuilt `forge:latest` from main (arm64, ≥ TASK-FMDR-008). forge-prod runs
   the new image. *Caveat:* 008's `FORGE_NATS_USER/PASSWORD` support is CLI-only; the `forge serve`
   daemon still parses inline-URL creds → `~/.config/forge/nats.env` stays inline-URL.
4. **Follow-up 3 (done):** restricted the nats entrypoint `envsubst` to `*_PASSWORD` vars (so `$JS`/`$KV`
   survive) and **subject-scoped** the `forge` user (`pipeline.> runbook.> agents.> fleet.> $JS.> $KV.>
   _INBOX.>`). Enforcement verified (forge denied `notifications.>`).
5. **Docs + status:** session-discoveries doc, build-plan marked all FORGE-OL phases Landed, FEAT-FMDR
   reconciled to `completed`, state-review doc (see References). All branches merged to main, pushed,
   cruft branches pruned.

## Current live state (GB10 `promaxgb10-41b1`, aarch64)

| Component | State | Verify |
|---|---|---|
| fleet-memory DB | PG16 + pgvector live, NAS `whitestocks:5433` | `psql postgresql://fleet_memory:<pw>@whitestocks.tailebf801.ts.net:5433/fleet_memory -c 'SELECT 1'` |
| forge-prod | container on `forge:latest` (rebuilt ≥008), connected as `forge` | `docker ps`; `curl -s 'http://127.0.0.1:8222/connz?auth=1'` → `{forge, rich}` |
| NATS broker | `ships-computer-nats` container (repo: `nats-infrastructure/`), `:4222`/`:8222`, healthy | `curl -s http://127.0.0.1:8222/varz` |
| forge NATS creds | `~/.config/forge/nats.env` (chmod 600, inline-URL, user `forge`) | consumed by forge-prod compose + `~/.bashrc` (CLI) |
| forge-prod deploy | `~/forge-prod/docker-compose.yml` (was ad-hoc `docker run`) | `docker compose -f ~/forge-prod/docker-compose.yml ps` |

## Git / commit state (all pushed to origin/main, github.com/guardkit/*)

- **forge** `main` → `5fcfddb` (007/008/005-complete + session docs + FEAT-FMDR status reconcile). Pushed. Only `main` branch (cruft pruned).
- **nats-infrastructure** `main` → `ab1bba4` (subject-scope forge user + restrict envsubst), `0e0a6a9` (add forge user). Pushed.
- **fleet-memory** `main` → `e83e4bc` (Synology deploy fixes: port 5433, ./pgdata+mkdir, initdb rsync, smoke G3/G5). Pushed.
- **MacBook:** just needs `git pull origin main` in forge to sync (do NOT run /feature-complete there — feature is already merged + reconciled). Optional local tidy: `git worktree prune`, remove any leftover `.guardkit/worktrees/FEAT-FMDR` / autobuild artifacts.

## Highest-leverage next move

**Build FEAT-MEM-04 (relay integration)** in `fleet-memory` — the FastStream durable consumer on the
MEMORY NATS stream → registry/chunking → deterministic writer. It is the **single missing write-path
component** blocking the Graphiti→fleet-memory migration, and it starts the trace-exhaust flywheel the
QA-Verifier later needs. ~3–5 days. Then: capture Graphiti baseline → FEAT-MEM-07 full re-index into
live NAS PG → probe-set parity eval → go/no-go → FEAT-MEM-08 cutover → FEAT-MEM-09 decommission
(reclaims ~28 GB of always-on Graphiti extraction). See the state-review doc for the full picture.

## Open follow-ups (operator / future, all documented)

1. **FEAT-MEM-04 relay** — the migration blocker (above).
2. **forge serve `FORGE_NATS_USER/PASSWORD` support (follow-up 1a)** — mirror `cli/runbook.py::_resolve_nats_auth`
   in the serve NATS connect path, then drop inline-URL from `nats.env` + rebuild image.
3. **Rotate NATS passwords** — rich/james/mark/admin were exposed in a transcript 2026-06-24; `rich` is
   used by `nats-core` (coordinate). `forge`'s password (`nats-infrastructure/.env`) is clean.
4. **Full output-loop `/system-arch`** — generalise from the FMDR exemplar (gates enforcement, more step
   types: run_autobuild / invoke_claude_code_debug / approval-gate, fix-agent, runbook generator, dashboard).
5. **guardkit `TASK-ABFIX-010`** — harness-side false-green fixes (out of these repos).

## Reference docs (read these to resume)

- `docs/reviews/forge-fleet-state-review-2026-06-24.md` — the zoom-out state review (executor / fleet-memory / QA-Verifier).
- `docs/handoffs/FMDR-NATS-SESSION-DISCOVERIES-2026-06-24.md` — every Synology deploy gotcha + NATS reality + the migration, consolidated.
- `nats-infrastructure/docs/forge-dedicated-user-migration.md` — the forge NATS user migration (broker side).
- `docs/research/ideas/output-loop-exemplar-build-plan.md` (+ scope) — FORGE-OL phases (all Landed) and what's deferred.
- `.guardkit/features/FEAT-FMDR.yaml` — feature status (completed).
- fleet-memory `docs/` + `tasks/` — FEAT-MEM-01..09 detail; FEAT-MEM-04 plan for the relay build.

## Gotchas to remember

- `forge runbook run` needs `~/.forge/` created + the runbook migration applied first (no shipped boot
  path does it): `connect_writer(~/.forge/forge.db)` → `apply_at_boot` → `persistence.migrations.runbook.apply`.
  Run from `~/Projects/appmilla_github` (parent dir) so relative step cwds resolve. Clear a prior row before
  re-running: `sqlite3 ~/.forge/forge.db "DELETE FROM runbooks WHERE runbook_id='fleet-memory-nas-deploy'"`.
- NATS broker config is bind-mounted from `nats-infrastructure/` (the in-container `/etc/nats/nats-server.conf`
  does not exist on the host). Accounts: APPMILLA (rich/james/**forge**), FINPROXY (mark), SYS (admin); nats-core uses rich.
- Synology: passwordless sudo is scoped to `/usr/local/bin/docker` only; docker won't auto-create bind-mount
  source dirs; DSM's own Postgres owns `127.0.0.1:5432` (hence fleet-memory on 5433).

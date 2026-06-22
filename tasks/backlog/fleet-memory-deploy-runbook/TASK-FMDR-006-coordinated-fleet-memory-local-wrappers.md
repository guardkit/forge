---
id: TASK-FMDR-006
title: "Coordinated fleet-memory: add deploy/local deploy.sh + smoke.sh wrappers"
status: backlog
created: 2026-06-22 00:00:00+00:00
priority: high
task_type: operator_handoff
documentation_level: standard
parent_review: TASK-REV-FMDR
feature_id: FEAT-FMDR
wave: 1
implementation_mode: task-work
complexity: 2
estimated_minutes: 30
dependencies: []
tags:
  - forge-output-loop
  - operator-handoff
  - fleet-memory
  - sibling-repo
test_results:
  status: pending
  coverage: null
  last_run: null
---

# TASK-FMDR-006 — Coordinated fleet-memory local wrappers (operator handoff)

This task is `task_type: operator_handoff` — it edits the **sibling `fleet-memory` repo**,
which an AutoBuild forge worktree **cannot modify** (project memory:
"Autobuild can't edit sibling repos"). It is a coordinated fleet-memory change made on
explicit instruction (build-plan §FORGE-OL-04: "If a wrapper needs a flag they do not
expose, that is a coordinated fleet-memory task created on explicit instruction").

## Why this is needed

The disposable end-to-end run (TASK-FMDR-004) needs the runbook's typed steps to dispatch
`deploy.sh` / `smoke.sh` against the **local** target. But today:

- `fleet-memory/deploy/nas/{deploy.sh,smoke.sh}` are hard-wired to the NAS over SSH
  (`source .env.deploy`; `ssh -i ~/.ssh/fleet_memory_nas_ed25519`; `rsync`). No local mode.
- `fleet-memory/deploy/local/` has only `docker-compose.yml` + `initdb/` — **no wrappers**.

Without local wrappers, the disposable runbook cannot share the NAS runbook's typed-step
shape, and scenario **D3** ("differ only in which deploy environment file they reference;
typed steps identical") cannot hold.

## What to add (in the sibling repo)

Create, in `../fleet-memory/deploy/local/`:

- **`deploy.sh`** (mirrors nas/deploy.sh's contract, local compose instead of SSH):
  ```bash
  #!/usr/bin/env bash
  set -euo pipefail
  cd "$(dirname "$0")"
  [ -f .env.deploy ] || { echo "ERROR: .env.deploy not found"; exit 1; }
  set -a; source .env.deploy; set +a
  docker compose up -d --wait          # GATE G2: non-zero if unhealthy
  ```
- **`smoke.sh`** (local analogues of gates G3–G5; exit non-zero on any failure):
  ```bash
  #!/usr/bin/env bash
  set -euo pipefail
  cd "$(dirname "$0")"
  [ -f .env.deploy ] || { echo "ERROR: .env.deploy not found"; exit 1; }
  set -a; source .env.deploy; set +a
  # G3: pg_isready + pgvector
  docker compose exec -T postgres pg_isready -U fleet_memory
  docker compose exec -T postgres psql -U fleet_memory -d fleet_memory -tAc \
    "SELECT extname FROM pg_extension WHERE extname='vector';" | grep -qx vector
  # G4: network path (local DSN)
  psql "postgresql://fleet_memory:${POSTGRES_PASSWORD:-fleet_memory}@localhost:${PGPORT:-5432}/fleet_memory" -c 'SELECT 1;' >/dev/null
  # G5: data volume present (local analogue — pgdata persisted by the compose volume)
  docker compose exec -T postgres test -f /var/lib/postgresql/data/PG_VERSION
  ```
- **`.env.deploy.example`** for the local target (e.g. `PGPORT=5432`, optional
  `POSTGRES_PASSWORD`), so the local runbook references `deploy/local/.env.deploy` and the
  only per-target differences are `cwd` + `env_file`.

Make both scripts executable (`chmod +x`) and idempotent (safe to re-run).

## Required operator follow-up

- **AC**: `fleet-memory/deploy/local/deploy.sh` and `smoke.sh` exist, are executable, and
  stand the local pgvector compose up + verify it (G3–G5 analogues) using only local
  Docker — no SSH, no NAS.
- **AC**: the scripts are committed in the fleet-memory repo (or pre-committed in the
  working tree) **before** `/feature-build` reaches Wave 2, so TASK-FMDR-004 can run.
- **AC (D3 enabler)**: the local and NAS runbooks now differ only in `cwd` + `env_file`;
  the typed step sequence and script basenames (`deploy.sh`, `smoke.sh`) are identical.

## Notes

- Coordinate the commit in the fleet-memory repo; this forge feature only consumes the
  scripts as-is.

---
id: TASK-FMDR-005
title: "Stand fleet-memory up on the real NAS via the executor (closes TASK-MEM-008)"
status: backlog
created: 2026-06-22 00:00:00+00:00
priority: high
task_type: operator_handoff
documentation_level: standard
parent_review: TASK-REV-FMDR
feature_id: FEAT-FMDR
wave: 3
implementation_mode: task-work
complexity: 2
estimated_minutes: 30
dependencies:
  - TASK-FMDR-003
  - TASK-FMDR-004
tags:
  - forge-output-loop
  - operator-handoff
  - fleet-memory
  - nas
test_results:
  status: pending
  coverage: null
  last_run: null
---

# TASK-FMDR-005 — Real-NAS stand-up (operator handoff)

This task is `task_type: operator_handoff` — AutoBuild will **not** attempt it. The
real-NAS stand-up is a one-shot **operational act** (summary §Deferred Items): it is not
a repeatable automated test. The operator runs the runbook against the real NAS, verifies
the runtime acceptance criteria below, then marks the task complete via `/task-complete`.

## Pre-flight (resolve before the run)

> The full manual Synology/SSH provisioning is **already documented** — see
> `fleet-memory/docs/runbooks/RUNBOOK-nas-postgres-deploy.md`, §"Provisioning record &
> corrections — 2026-06-21 (executed on the GB10, target `whitestocks`)" (the as-built
> record, including the two DSM gaps the original plan missed: *Enable user home service*
> and *administrators-group* membership). Do not re-derive it; just verify the checks
> below still hold.

- Forge runs on the **GB10 host** (`promaxgb10-41b1`), not the Mac (ASSUM-007, confirmed
  by the provisioning record). The `fleet_memory_nas_ed25519` key and the filled
  `.env.deploy` live on that host.
- **`.env.deploy` must sit in the step's `cwd`** — `fleet-memory/deploy/nas/.env.deploy`
  on the GB10 — because `deploy.sh`/`smoke.sh` do `source .env.deploy` relative to their
  working directory and **ignore** the handler's `ENV_FILE` variable. If the file is
  anywhere else (e.g. elsewhere in the forge tree), the deploy step fails pre-flight with
  `ERROR: .env.deploy not found`. Verify on the GB10:
  ```bash
  ls -la ~/Projects/appmilla_github/fleet-memory/deploy/nas/.env.deploy
  git -C ~/Projects/appmilla_github/fleet-memory check-ignore deploy/nas/.env.deploy   # must echo the path
  ```
- Worked values (from the provisioning record): `NAS_HOST=whitestocks.tailebf801.ts.net`,
  `NAS_USER=RichardWoollcott`, `NAS_DOCKER_ROOT=/volume1/docker/fleet_memory`.
- Confirm the NAS grants passwordless `docker` to the deploy user — the sudoers drop-in is
  `/etc/sudoers.d/fleet_memory_docker` (a **wiped drop-in after a DSM upgrade** surfaces as
  a diagnosable permissions failure — D4):
  ```bash
  ssh -i ~/.ssh/fleet_memory_nas_ed25519 -o BatchMode=yes -p 22 \
    RichardWoollcott@whitestocks.tailebf801.ts.net 'sudo -n /usr/local/bin/docker ps'
  ```
- **Tailscale key expiry** on `whitestocks` was set to lapse ~1 month after 2026-06-21 —
  if the NAS has dropped off the tailnet, re-enable/disable expiry in the Tailscale admin
  console before running (forward caveat in the runbook doc).

## Run

```bash
forge runbook run forge/runbooks/RUNBOOK-fleet-memory-nas.json
```

## Required operator follow-up

The operator must verify the runtime acceptance criteria below manually, then mark the
task complete.

- **AC (D1)**: With the runbook pointed at the real NAS and the real `.env.deploy`, the
  executor runs it and fleet-memory Postgres-with-pgvector is **live on the NAS**, the
  smoke gates are all green, and the stand-up was performed **by the executor** — not a
  manual `./deploy.sh`.
- **AC (TASK-MEM-008 / FEAT-MEM-01)**: This run **closes TASK-MEM-008** and **ticks
  FEAT-MEM-01's NAS-deploy acceptance criterion** (Postgres + pgvector up on the NAS,
  reachable over LAN/Tailscale, data on a backed-up volume).
- **AC (harvest)**: `RUNBOOK-fleet-memory-nas.json` is confirmed saved under
  `forge/runbooks/` as the first reusable exemplar; the per-step record is queryable and
  the lifecycle events fired in order.

## Notes

- Failure modes to recognise (already covered as scenarios): unreachable NAS fails the
  deploy cleanly with nothing half-stood-up (D8); a revoked passwordless-docker permission
  surfaces as a permissions problem, not a generic error (D4).

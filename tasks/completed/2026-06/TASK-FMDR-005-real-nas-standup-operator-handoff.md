---
id: TASK-FMDR-005
title: "Stand fleet-memory up on the real NAS via the executor (closes TASK-MEM-008)"
status: completed
created: 2026-06-22 00:00:00+00:00
completed: 2026-06-23 00:00:00+00:00
completed_location: tasks/completed/2026-06/
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
  - TASK-FMDR-007
  - TASK-FMDR-008
tags:
  - forge-output-loop
  - operator-handoff
  - fleet-memory
  - nas
test_results:
  status: passed
  coverage: null
  last_run: 2026-06-23
  note: >-
    Real-NAS run via `forge runbook run` completed successfully — deploy_compose
    passed (G2) and run_smoke_tests passed (G3.1 pg_isready, G3.2 pgvector 0.8.3,
    G4 network on 5433, G5 PG_VERSION=16 on the backed-up pgdata volume). Postgres
    16.14 + pgvector live on the NAS by the executor. NATS lifecycle events NOT
    published live (no broker creds; capability shipped in TASK-FMDR-008, ordering
    proven by the BDD suite).
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
- **DSM rsync service must be ENABLED** (Control Panel → File Services → rsync → "Enable
  rsync service"). `deploy.sh` step 2a uses `rsync` to sync the compose folder, and Synology
  gates even SSH-mode `rsync --server` behind this toggle. When it is off, the deploy step
  fails with the (misleading) client message `Permission denied, please try again.` while the
  true remote error is `rsync error: rsync service is no running (code 43)`. The 2026-06-21
  provisioning established SSH/sudo/docker but **not** this. Verify from the GB10 with a **real
  rsync dry-run** (the authoritative check — a hand-rolled `rsync --server …` one-liner returns a
  false `43` even when the service is ON, so do not use it):
  ```bash
  cd ~/Projects/appmilla_github/fleet-memory/deploy/nas
  rsync -avzn -e "ssh -i ~/.ssh/fleet_memory_nas_ed25519 -p 22 -o BatchMode=yes" \
    docker-compose.yml RichardWoollcott@whitestocks.tailebf801.ts.net:/tmp/   # exit 0 ⇒ serving
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

## Run attempt — 2026-06-23 (GB10 `promaxgb10-41b1`) — BLOCKED

The operator handoff was executed on the GB10. Pre-flight was **all green**; the run was then
blocked by two latent defects in the shipped FMDR artifacts, now filed as **TASK-FMDR-007** and
**TASK-FMDR-008**. Nothing was stood up on the NAS (clean failure — D8 held).

**Pre-flight (verified green):** host = `promaxgb10-41b1`; `forge` CLI present; code current
(forge `0df93b9`, fleet-memory deploy/nas `56e2de9`); `forge/runbooks/RUNBOOK-fleet-memory-nas.json`
present; `.env.deploy` present, git-ignored, all worked values correct; `deploy.sh`/`smoke.sh`
executable; NAS reachable over Tailscale with passwordless docker (`sudo -n docker ps` → 0,
`falkordb` up, no `fleet_memory` container yet); NATS port 4222 open.

**Two undocumented prerequisites discovered (handoff doc omits these):**
1. `~/.forge/` does not exist and `connect_writer` does not auto-create it.
2. `forge runbook run` does **not** apply migrations — the `runbooks`/`runbook_steps` tables
   must be created first. No shipped boot/CLI path calls `persistence/migrations/runbook.apply()`
   (only tests do). Provision a fresh DB exactly as the test fixtures do:
   ```python
   conn = connect_writer(Path.home()/".forge"/"forge.db")   # mkdir parent first
   apply_at_boot(conn)                                       # core schema (v1, v2)
   from forge.persistence.migrations.runbook import apply; apply(conn)   # runbooks tables
   ```

**Blocker 1 → TASK-FMDR-007 (script resolution).** The run escalated at step 0 with handler
payload `exit_code: 127`. The exemplar uses `"script": "deploy.sh"` (bare) + relative `cwd`, but
the handler runs `subprocess.run([script], cwd=cwd)`, which on Python 3.12 does not resolve a
bare name relative to `cwd` (it needs `./deploy.sh`). Verified by reproduction. No test spans
this gap (`test_runbook_exemplar.py` asserts the bare form but never executes; the e2e/BDD tests
use `./` + absolute cwd). Third instance of the false-green pattern in
`docs/reviews/FEAT-FMDR-autobuild-false-green-analysis.md`.

**Blocker 2 → TASK-FMDR-008 (NATS auth).** The :4222 broker requires authorization; the CLI's
`nats.connect` has no auth plumbing → `Authorization Violation`, and it spun in a reconnect loop
rather than failing fast to NoOp. `--no-events` is the credential-free workaround; the chosen
proper fix is to wire CLI NATS auth so the "events fired in order" harvest sub-AC can be met live.

**To resume:** land TASK-FMDR-007 (unblocks the core deploy/smoke ACs), then re-provision the DB
as above and re-run `forge runbook run forge/runbooks/RUNBOOK-fleet-memory-nas.json` from
`~/Projects/appmilla_github`. Land TASK-FMDR-008 to also satisfy the live-events sub-AC (otherwise
run `--no-events` and note events ordering is proven by the BDD suite).

### Second attempt — 2026-06-23 (post-007/008) — new NAS-config blocker

With **TASK-FMDR-007 and TASK-FMDR-008 landed** (commits `7152fff`, `b15425c`), the run was
repeated. Both forge fixes are **confirmed working end-to-end**: the executor resolved and ran
`deploy.sh` (007), NATS made a single attempt then fell back cleanly with no spin (008), the real
script error was captured, and the runbook escalated cleanly (nothing half-stood-up — D8 held).

The deploy is now blocked by a **NAS-side configuration prerequisite, not forge**: DSM's **rsync
service is disabled**, so `deploy.sh` step 2a fails with remote `rsync error: rsync service is no
running (code 43)`. Verified that SSH key auth succeeds, `rsync` is installed (v3.1.2), and the
docker root is writable — only the DSM rsync toggle is missing. **Fix:** enable rsync in DSM
Control Panel → File Services → rsync, then re-run (everything else is staged: DB provisioned and
clean, code current, NAS reachable). NATS events still need creds (TASK-FMDR-008) to publish live;
otherwise the run proceeds with a clean NoOp fallback.

### RESOLVED — 2026-06-23 — runbook completed successfully ✅

`forge runbook run forge/runbooks/RUNBOOK-fleet-memory-nas.json` reported **"Runbook
fleet-memory-nas-deploy completed successfully"**. `deploy_compose` **passed** (G2) and
`run_smoke_tests` **passed** — **G3.1** pg_isready, **G3.2** pgvector 0.8.3, **G4** network path on
`5433` (real host `psql` over Tailscale), **G5** PG_VERSION=16 on `/volume1/docker/fleet_memory/pgdata`.
Postgres 16.14 + pgvector is **live on the NAS, stood up by the executor**. Per-step record is
queryable in `~/.forge/forge.db`; the runbook exemplar is saved under `forge/runbooks/`.

**Closes TASK-MEM-008; ticks FEAT-MEM-01's NAS-deploy AC.**

Getting there required several **NAS-environment + deploy-script** fixes that the never-run-against-a-real-Synology
scripts had latent (all surfaced by this run, exactly what the operator handoff is for):

| Area | Fix | Where |
|---|---|---|
| forge executor | bare `deploy.sh` not resolvable; NATS spin | TASK-FMDR-007 / -008 (merged) |
| NAS config | enable DSM **rsync service**; install **Compose V2** plugin | operator / one-time |
| port conflict | DSM's own Postgres owns `127.0.0.1:5432` → publish on **5433** | `deploy/nas/docker-compose.yml`, `smoke.sh` |
| compose volume | `${NAS_DOCKER_ROOT}` not in compose env → `./pgdata`; `mkdir` it (Synology won't auto-create binds) | `docker-compose.yml`, `deploy.sh` |
| initdb mount | rsync `initdb/`→`initdb` (trailing slash copied only contents) | `deploy.sh` |
| smoke gates | G3 readiness poll (first-init race); G5 read PG_VERSION via docker (pgdata chowned to postgres uid) | `smoke.sh` |
| host | install `postgresql-client` for G4 | GB10 / operator |

**Open follow-ups:** (1) the `deploy/nas` fixes above are **uncommitted in the sibling fleet-memory
repo** — commit them so the exemplar is reproducible. (2) NATS lifecycle events were **not published
live** (no broker creds; the auth path shipped in TASK-FMDR-008, ordering proven by the BDD suite) —
the live "events fired in order" sub-AC is deferred pending operator-supplied creds.

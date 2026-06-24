# Session discoveries — real-NAS stand-up + NATS integration (2026-06-23 → 24)

Consolidated record of everything surfaced while executing **TASK-FMDR-005** (real-NAS
fleet-memory stand-up) and then resolving forge's **NATS broker credentials**. Most of these
were invisible to the green test suite / Mac local-target e2e and only appeared against the
real GB10 + Synology NAS — which is exactly what the operator handoff exists to catch.

**Outcome:** fleet-memory Postgres 16 + pgvector 0.8.3 live on the NAS via the executor (all
smoke gates green); forge migrated to a dedicated NATS identity; lifecycle events verified
publishing in order to the real broker. TASK-MEM-008 closed, FEAT-MEM-01 NAS AC ticked.

---

## 1. Runtime topology (as-built, GB10 `promaxgb10-41b1`, aarch64)

| Thing | Reality |
|---|---|
| forge runtime | **`forge-prod`** container (`forge:latest`), `forge serve`, `network_mode: host`, mounts `~/forge-prod-state/.forge`→`/home/forge/.forge` and `~/forge-state`→`/var/forge`. |
| forge autobuild | `forge-autobuild-runner` + `forge-langgraph-sidecar` (systemd user units, langgraph dev on :8124). Do **not** publish to NATS directly. |
| NATS broker | **`ships-computer-nats`** container (image built from `~/Projects/appmilla_github/nats-infrastructure`), `:4222` client / `:8222` monitoring, JetStream on a named volume. Config is **bind-mounted** from that repo → `/etc/nats/nats-server.conf` is the *in-container* path (there is no such host file). |
| NAS | Synology DSM `whitestocks` (x86_64), reached over Tailscale (`whitestocks.tailebf801.ts.net`, `100.92.74.2`). |
| forge runbook DB | `~/.forge/forge.db` (host CLI). The prod container uses `~/forge-prod-state/.forge/forge.db`. |

## 2. `forge runbook run` operational prerequisites (not handled by any shipped boot path)

- **`~/.forge/` must exist** — `connect_writer` does *not* auto-create it.
- **Schema must be migrated first** — the `runbook run` CLI does **not** apply migrations. No
  shipped boot/CLI path calls `persistence/migrations/runbook.apply()` (only tests do). Provision
  exactly like the test fixtures:
  ```python
  conn = connect_writer(Path.home()/".forge"/"forge.db")   # mkdir parent first
  apply_at_boot(conn)                                       # core schema (v1, v2)
  from forge.persistence.migrations.runbook import apply; apply(conn)   # runbooks tables
  ```
- **Invoke from `~/Projects/appmilla_github`** (the parent dir) so the runbook's relative step
  `cwd: fleet-memory/deploy/nas` resolves; pass the runbook path absolute.
- **Re-run gotcha:** the CLI persists the runbook before execution and rejects a duplicate
  `runbook_id`, so a re-run needs the prior row cleared:
  `sqlite3 ~/.forge/forge.db "DELETE FROM runbooks WHERE runbook_id='fleet-memory-nas-deploy';"`

## 3. Synology NAS deploy gotchas (the deploy scripts had never run against a real DSM)

Fixed in the sibling **fleet-memory** repo (`deploy/nas/`, committed `e83e4bc`):

| # | Symptom | Root cause | Fix |
|---|---|---|---|
| 1 | rsync step: `Permission denied, please try again` / `rsync error: rsync service is no running (code 43)` | DSM **rsync service disabled** (gates SSH-mode `rsync --server`) | Operator: Control Panel → File Services → rsync → enable. (The misleading client message hides the real code-43.) |
| 2 | `docker: 'compose' is not a docker command` | NAS has only **docker-compose v1** (1.28.5); no V2 plugin | Installed the V2 plugin into `/usr/local/lib/docker/cli-plugins/` (Synology Docker pkg, docker 20.10.3). |
| 3 | container start: `listen tcp4 0.0.0.0:5432: bind: address already in use` | **DSM's own PostgreSQL** owns `127.0.0.1:5432` (serves `synoindex`) | Publish fleet-memory on **host 5433** (`"5433:5432"`); smoke G4 + DSN use 5433. |
| 4 | `Bind mount failed: '/pgdata' does not exists` | `${NAS_DOCKER_ROOT}` not in `docker compose`'s env on the NAS → blank | Use a **relative `./pgdata`** volume (like the sibling `./initdb`); deploy.sh `mkdir`s it first (Synology docker won't auto-create bind sources). |
| 5 | `Bind mount failed: '.../initdb' does not exists` | `rsync … initdb/` (**trailing slash**) copies *contents*, never the dir | `rsync … initdb` (no slash) copies the directory. |
| 6 | smoke **G3.1** `pg_isready failed` on a fresh deploy | First-init runs initdb scripts for several seconds; gate checked once (race) | smoke.sh G3 now **polls** for readiness. |
| 7 | smoke **G5** `Permission denied` reading `pgdata/PG_VERSION` | pgdata is chowned to the postgres uid on first init; SSH user can't read it | G5 reads `PG_VERSION` via the docker grant (still verifies the host backed-up path). |
| 8 | smoke **G4** can't connect | **host `psql` not installed** on the GB10 | `sudo apt-get install -y postgresql-client`. |

Sudo on the NAS is scoped to `/usr/local/bin/docker` (NOPASSWD) only — `sudo -n <anything else>`
needs a password. Plan deploy steps around the docker grant.

## 4. NATS broker reality + forge's dedicated identity

- The "missing `/etc/nats/nats-server.conf`" was a red herring — the broker is the
  `ships-computer-nats` **container**; config lives in `nats-infrastructure/config/`
  (`nats-server.conf` + `accounts/accounts.conf.template`, rendered at start via `envsubst`,
  passwords from `nats-infrastructure/.env`).
- Accounts (the privilege boundary): **APPMILLA** (`rich`, `james` full `>`, + new **`forge`**),
  **FINPROXY** (`mark`, `finproxy.>`), **SYS** (`admin`). `nats-core` clients connect as `rich`.
- forge had **no NATS identity** — `forge-prod` rode on `rich` via inline `FORGE_NATS_URL`, and the
  CLI had no creds (NoOp). Migrated forge to a **dedicated `forge` user** (APPMILLA), single
  source of truth `~/.config/forge/nats.env` (chmod 600), consumed by `forge-prod`
  (`~/forge-prod/docker-compose.yml`, was an ad-hoc `docker run`) + the CLI (`~/.bashrc`).
  See `nats-infrastructure/docs/forge-dedicated-user-migration.md` (committed `0e0a6a9`).
- **TASK-FMDR-008** shipped the CLI auth path (`FORGE_NATS_CREDS|TOKEN|USER+PASSWORD`) + the
  `allow_reconnect=False` fix (auth-reject → fail-fast to NoOp, no reconnect spin) + inline-URL
  log redaction.
- Lifecycle events verified publishing **in order** to the real broker:
  `runbook-started → step-started → step-result → step-started → step-result → runbook-complete`.

### Two infra constraints found (now being fixed — §6)
- **`envsubst` clobbers NATS system subjects.** The entrypoint's blanket `envsubst` substitutes
  *every* `$VAR`, so `$JS.API.>` / `$KV.>` in an ACL become `.API.>` / `.>` (invalid). That blocked
  subject-scoping the `forge` user (it uses full `>` within APPMILLA as a stopgap).
- **`forge:latest` predates TASK-FMDR-008** (image built 2026-05-12). It only parses inline-URL
  creds, not `FORGE_NATS_USER/PASSWORD` — so `nats.env` uses the inline-URL form as a stopgap.

## 5. The recurring "false-green" pattern (see docs/reviews/FEAT-FMDR-autobuild-false-green-analysis.md)

Each FMDR blocker was invisible to CI/local because **the exact production combination was never
exercised**: tests asserted JSON shape (not execution), the e2e used `./script` + absolute cwd
(not the exemplar's bare-name + relative cwd), and the smoke gates assumed an environment unlike
the real Synology NAS. Lesson reinforced: an operator handoff against real infra is the only thing
that surfaces this class of gap.

## 6. Follow-ups — status

1. ✅ **Rebuilt `forge:latest` from main (≥ 008)** (2026-06-24, arm64, via
   `forge/scripts/build-image.sh` form → `--build-context nats-core=../nats-core`). forge-prod now
   runs the current image (was 6 weeks stale) and its CLI has 008. **Caveat found:** 008's
   `FORGE_NATS_USER/PASSWORD` support lives only in `forge/cli/runbook.py`; the **`forge serve`
   daemon path doesn't read it** — it only parses inline-URL creds. So `~/.config/forge/nats.env`
   stays **inline-URL** (`nats://forge:<pw>@…`). → **new follow-up 1a.**
2. ✅ **Restricted the entrypoint `envsubst` to `*_PASSWORD` vars** (2026-06-24) and **subject-scoped
   the `forge` user** to `pipeline.> runbook.> agents.> fleet.> $JS.> $KV.> _INBOX.>`. Verified
   enforced: forge publishes `runbook.>` but is **denied** `notifications.>`; forge-prod reconnected
   with zero permission violations. (Gotcha fixed along the way: the template's header comment
   literally contained `${VAR}`, which the now-restricted envsubst left intact and the
   braced-`${VAR}` safeguard would reject — reworded the comment.)
3. **1a (NEW): make `forge serve` honour `FORGE_NATS_USER/PASSWORD`** (mirror `_resolve_nats_auth`
   in the serve NATS path), then `nats.env` can drop inline-URL. Forge code change + image rebuild.
4. **Rotate the NATS passwords** — rich/james/mark/admin were exposed in a session transcript on
   2026-06-24. `rich` is used by `nats-core` (coordinate that update). *Deferred — operator.*
5. **guardkit `TASK-ABFIX-010`** — harness-side false-green fixes. *Out of this repo.*

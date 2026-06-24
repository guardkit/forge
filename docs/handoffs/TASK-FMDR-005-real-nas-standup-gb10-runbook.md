# TASK-FMDR-005 — Real-NAS stand-up: GB10 run sequence

**Run host: the GB10 (`promaxgb10-41b1`)** — not the Mac. The Mac has the SSH key
but is missing the filled `.env.deploy` and doesn't trust the NAS host key. The
GB10 has the key + `.env.deploy` + the 2026-06-21 provisioning record + known_hosts,
and NATS JetStream is up there for lifecycle events (ASSUM-007).

This closes **TASK-MEM-008** and ticks **FEAT-MEM-01**'s NAS-deploy AC.

---

## ⚠️ Critical: run from the PARENT dir, not from `forge/`

The exemplar runbook uses **relative** paths:
- runbook arg: `forge/runbooks/RUNBOOK-fleet-memory-nas.json`
- step `cwd`: `fleet-memory/deploy/nas` (a *sibling* of `forge`)

The deploy handler runs `subprocess.run([script], cwd=cwd)` with `cwd` passed raw,
so a relative `cwd` resolves against the **invocation directory**. Both prefixes
(`forge/…` and `fleet-memory/…`) only resolve from `~/Projects/appmilla_github/`.

```bash
cd ~/Projects/appmilla_github     # NOT ~/Projects/appmilla_github/forge
```

If you run from inside `forge/`, the deploy step fails because
`forge/fleet-memory/deploy/nas` doesn't exist.

---

## Step 0 — get the code onto the GB10 (the deliverables are unpushed)

001's runbook JSON and 002's wired CLI live on the Mac's `main` and are **not yet
pushed**. On the Mac: `git push` (`4753b20..95ff3dc`). Then on the GB10:

```bash
cd ~/Projects/appmilla_github/forge && git pull        # gets RUNBOOK-*.json + real-handler CLI
cd ~/Projects/appmilla_github/fleet-memory && git log --oneline -1 -- deploy/nas   # wrappers already at d6cf3d4
pip install -e ~/Projects/appmilla_github/forge        # if `forge` CLI not already current on PATH
```

## Step 1 — pre-flight (from `~/Projects/appmilla_github`)

```bash
cd ~/Projects/appmilla_github
hostname                                                # expect promaxgb10-41b1
which forge && forge --help >/dev/null && echo "forge CLI OK"

# .env.deploy must be in the step cwd and git-ignored
ls -la fleet-memory/deploy/nas/.env.deploy
git -C fleet-memory check-ignore deploy/nas/.env.deploy # must echo the path

# NAS reachable on the tailnet + passwordless docker (D4 diagnoses a wiped sudoers drop-in)
ssh -i ~/.ssh/fleet_memory_nas_ed25519 -o BatchMode=yes -p 22 \
  RichardWoollcott@whitestocks.tailebf801.ts.net 'sudo -n /usr/local/bin/docker ps'
```

Pre-flight gotchas:
- **DSM rsync service must be ON** (Control Panel → File Services → rsync → "Enable rsync
  service"). `deploy.sh` step 2a syncs the compose folder with `rsync`, and Synology gates
  SSH-mode `rsync --server` behind this toggle. If off, the deploy step fails with the
  misleading client message `Permission denied, please try again.` — the true remote error is
  `rsync error: rsync service is no running (code 43)`. Check from the GB10 with a **real rsync
  dry-run** (authoritative — a hand-rolled `rsync --server …` one-liner gives a false `43` even when
  the service is ON):
  `cd ~/Projects/appmilla_github/fleet-memory/deploy/nas && rsync -avzn -e "ssh -i ~/.ssh/fleet_memory_nas_ed25519 -p 22 -o BatchMode=yes" docker-compose.yml RichardWoollcott@whitestocks.tailebf801.ts.net:/tmp/`
  (exit 0 ⇒ serving). Confirmed disabled on the 2026-06-23 run; enabling it unblocked the deploy.
- **Host key**: if you get `Host key verification failed`, accept the NAS host key
  once (`ssh-keyscan` into `known_hosts`, or connect interactively).
- **Tailscale expiry**: `whitestocks`'s key was set to lapse ~1 month after
  2026-06-21 (≈2026-07-21). If the NAS has dropped off the tailnet, re-enable/disable
  key expiry in the Tailscale admin console first.
- Worked values (provisioning record): `NAS_HOST=whitestocks.tailebf801.ts.net`,
  `NAS_USER=RichardWoollcott`, `NAS_DOCKER_ROOT=/volume1/docker/fleet_memory`.

## Step 2 — run the runbook (via the executor, not a manual ./deploy.sh)

```bash
cd ~/Projects/appmilla_github
forge runbook run forge/runbooks/RUNBOOK-fleet-memory-nas.json
# add --no-events to skip NATS publishing if the broker isn't wanted for this run
```

Expect: `Runbook fleet-memory-nas-deploy completed successfully` (deploy step passed,
smoke step passed). The runbook is idempotent; a second run reports "already complete".

## Step 3 — verify the runtime ACs (D1 / TASK-MEM-008 / FEAT-MEM-01)

```bash
# Postgres + pgvector live on the NAS, stood up BY THE EXECUTOR
ssh -i ~/.ssh/fleet_memory_nas_ed25519 RichardWoollcott@whitestocks.tailebf801.ts.net \
  'sudo /usr/local/bin/docker ps --filter name=fleet_memory'
```

Confirm:
- Postgres-with-pgvector container is **Up/healthy** on the NAS.
- Smoke gates green (the `run_smoke_tests` step exited 0 → G3 pgvector, G4 network
  path over LAN/Tailscale, G5 backed-up data volume).
- Per-step record is queryable and lifecycle events fired in order (NATS).

## Step 4 — close out

```bash
/task-complete TASK-FMDR-005
```

This is the only remaining FEAT-FMDR task; 001–004 + 006 are done on `main`
(`4753b20`, `8b8bed8`, `6d10d97`, `83719ed`; wrappers at fleet-memory `d6cf3d4`).

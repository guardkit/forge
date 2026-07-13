# forge ops — service supervision & boot recovery (O-30, Phase E2-S3)

This directory holds the **operator-facing supervision** artifacts that close
gap **O-30** (host-reboot / power-loss auto-recovery for the operator-facing
services). Re-verified live 2026-07-13: one of O-30's three named services (the
forge sidecar) turned out to be **already supervised** — so this pass grants a
restart policy to forge-prod, supplies the jarvis front-door unit, and
**version-controls** the already-running sidecar unit (see §In one minute).
Everything here is **authored + demonstrated**, never applied to live services by
this pass — live application is a coordinator step (handoff rule 3). Sibling:
`jarvis/ops/systemd/` (the jarvis front-door unit).

## In one minute

O-30 named three operator-facing services with no reboot supervision. Re-verified
on the live box (2026-07-13), the picture is:

1. **forge-prod** — the planning engine, started with `docker run -d … forge serve`
   and (per the canonical runbook) **no `--restart` policy**. *Still open* — needs
   the restart policy. (Live drift note: the container currently already carries
   `unless-stopped`, applied out-of-band; the durable gap is that the canonical
   `docker run` still omits it, so any recreate drops it.)
2. **the jarvis Slack front door** — a bare `langgraph dev` dev-server, no
   supervisor. *Still open* — `jarvis-frontdoor.service` is genuinely not installed.
3. **the forge autobuild-runner sidecar** (`langgraph dev` on :8124) — **already
   supervised.** A `forge-langgraph-sidecar.service` user unit is installed
   (2026-07-04), enabled AND active, carrying load-bearing env (guardkit resolver
   path, SDK-harness pin, config path, default repo, enriched PATH). What was
   actually missing is that the unit lived ONLY in `~/.config/systemd/user`, not in
   version control — a box rebuild / fresh clone would lose it. This pass
   **version-controls the running unit** so it is reproducible (and the install
   step below reconciles, never clobbers, the live env).

O-30 closes the set: a `docker update` restart policy for (1), a systemd **user**
unit for (2), and a version-controlled faithful capture of the already-running
supervisor for (3). On an overnight power blip a James/Rich Slack request finds the
front door and planning engine still up — or brought back automatically.

| # | Artifact | Closes | Applied by |
|---|---|---|---|
| a | `scripts/restart_policy.py` | forge-prod has no `--restart` | coordinator (`docker update --restart`, `--apply`) |
| b | `ops/systemd/forge-langgraph-sidecar.service` | sidecar unit not in version control (already supervised live) | coordinator (reconcile, don't clobber) |
| b | `jarvis/ops/systemd/jarvis-frontdoor.service` | front door un-supervised (not installed) | coordinator (jarvis venue) |
| c | this README §Boot order | who waits on whom | — |
| d | `ops/receipts/` | the demonstrations | — |

## a. forge-prod restart policy — `scripts/restart_policy.py`

Gives forge-prod `--restart unless-stopped` (matching nats' policy) via
`docker update` — a **metadata-only** change that does **not** stop, restart, or
recreate the container. Because no restart occurs, it needs **no Ack-Pending-0 /
worker-free drain** (that gate guards a *recreate*); the receipt says so
explicitly. JSON receipts, idempotent.

**Explicit-apply guard (E2-S3 fix).** The default is a **preview** — a bare run
inspects and prints the would-be change and runs **no** `docker update`. Mutating
the live container requires the explicit `--apply` flag, so no stray invocation
touches `forge-prod`. `--apply` and `--dry-run` are mutually exclusive.

```bash
# Preview / preflight (DEFAULT — read-only inspect; runs no docker update):
.venv/bin/python scripts/restart_policy.py
.venv/bin/python scripts/restart_policy.py --dry-run   # explicit, same effect

# Apply the policy to forge-prod (the coordinator's live step — REQUIRES --apply):
.venv/bin/python scripts/restart_policy.py --apply

# Roll back to docker's default ("no") — also a mutation, so also needs --apply:
.venv/bin/python scripts/restart_policy.py --rollback --apply

# Rehearse/demonstrate on a THROWAWAY scratch container (never forge-prod):
.venv/bin/python scripts/restart_policy.py --container <scratch> --apply
```

Receipt = the `HostConfig.RestartPolicy` dump before/after + the preflight
checklist (the Ack-Pending-0 non-requirement), written to `ops/receipts/`.

> **Live-state note (re-verified this session, 2026-07-13):** forge-prod
> *currently* already carries `unless-stopped` (see
> `ops/receipts/forge-prod-restart-policy-STATE-2026-07-13.md`) — applied
> out-of-band since the 07-13 gap snapshot, so an apply run is now a harmless
> **no-op** (the script is idempotent). The **durable** gap is that the canonical
> `docker run` command still omits `--restart`, so any recreate drops it. Two
> defences: (1) run this script after any recreate; (2) **recommended** — bake
> `--restart unless-stopped` into the canonical run command so a fresh
> `docker run` is born supervised. Example:
> ```bash
> docker run -d --name forge-prod --restart unless-stopped \
>     --network host -e FORGE_LOG_LEVEL=info \
>     -e FORGE_AUTOBUILD_RUNNER_URL="http://localhost:8124" \
>     -v ~/forge-state:/var/forge forge:latest forge serve
> ```

## b. the systemd USER units

Two long-running `langgraph dev` processes are supervised by the **user** systemd
manager (they bind the developer venv + a repo-local `.env`, both user-scoped — no
root). Both carry `Restart=on-failure` + `RestartSec=5`. **Their situations differ
— read the per-unit note before installing.**

### b1. `ops/systemd/forge-langgraph-sidecar.service` — ALREADY installed live; RECONCILE, don't clobber

This unit is a **faithful version-controlled capture of the unit already installed
and running** at `~/.config/systemd/user/forge-langgraph-sidecar.service`
(2026-07-04, enabled + active). Its functional body is byte-identical to the
running unit (same `Environment=`, same `--host 127.0.0.1`, same `Type=simple` +
direct-venv `ExecStart`); it only adds two `Documentation=` lines. So installing it
is a **no-op** against what is running — that is the point.

> **DO NOT blindly `cp` this over the installed unit without a diff.** The running
> unit carries **load-bearing `Environment=` lines that `.env` does NOT** (verified
> E2-S3): `FORGE_GUARDKIT_PATH`, `GUARDKIT_HARNESS=sdk`, `FORGE_CONFIG_PATH`,
> `FORGE_DEFAULT_REPO`, and an enriched `PATH=%h/.agentecflow/bin:…`. A `cp` of any
> unit that lacks them (e.g. an older draft that relied on `EnvironmentFile=-.env`
> alone) would launch the autobuild build-half **without** the guardkit resolver
> path / SDK-harness pin and break it. Reconcile explicitly:

```bash
mkdir -p ~/.config/systemd/user
# 1. If a unit is already installed, DIFF first — expect ONLY the two added
#    Documentation= lines (functional body identical):
diff ~/.config/systemd/user/forge-langgraph-sidecar.service \
     ops/systemd/forge-langgraph-sidecar.service || true
# 2a. Diff shows only Documentation= additions (or the unit is absent) -> safe to install:
cp ops/systemd/forge-langgraph-sidecar.service ~/.config/systemd/user/
# 2b. Diff shows Environment=/ExecStart/host differences -> STOP: the running unit
#     is the source of truth for the live env; do not overwrite. Re-capture it into
#     this file instead, then commit.
systemctl --user daemon-reload
systemctl --user status forge-langgraph-sidecar.service   # already active; no restart needed
```

> **Prerequisite B** (for a fresh box where the unit is NOT yet running): the forge
> venv must carry `langgraph-cli` + provider extras (`uv sync --extra providers`, or
> the runbook pip line) so `.venv/bin/langgraph` resolves. Until it does,
> `systemd-analyze verify` warns *"Command …/.venv/bin/langgraph is not executable"*
> — this is expected env drift, **not** a unit defect: the currently-installed unit
> reports the identical warning today (the venv was rebuilt 2026-07-12, dropping the
> `langgraph` entrypoint; the live process predates that). Restore the venv before a
> unit restart. Then `enable --now` only if it is not already active.

### b2. `jarvis/ops/systemd/jarvis-frontdoor.service` — NOT installed; safe to install

The Slack front door (`jarvis` + `jarvis_reasoner` graphs) is **genuinely
un-supervised** — `jarvis-frontdoor.service` is not installed (confirmed E2-S3);
the live `langgraph dev` front door runs unsupervised. This unit is
`systemd-analyze verify`-clean and NOT installed by this pass. Install (jarvis
venue, on the box running the front door):

```bash
mkdir -p ~/.config/systemd/user
cp <jarvis>/ops/systemd/jarvis-frontdoor.service ~/.config/systemd/user/
systemctl --user daemon-reload
loginctl enable-linger "$USER"        # so user units survive logout / start at boot
# Kill any bare `nohup langgraph dev &` front door first so :2024 is free:
systemctl --user enable --now jarvis-frontdoor.service
systemctl --user status jarvis-frontdoor.service
```

See `jarvis/ops/systemd/README.md` for the jarvis-side detail.

## c. Boot order

The dependency chain, first to last — **who waits on whom and why**:

```
1. ships-computer-nats            restart: unless-stopped (compose)   — the bus; everything needs it
        │  (all NATS clients reconnect once it answers on :4222)
        ▼
2. forge-langgraph-sidecar        systemd --user, Restart=on-failure  — MUST precede forge-prod:
   (autobuild_runner on :8124)                                          forge serve fail-fasts on a
        │                                                               dead FORGE_AUTOBUILD_RUNNER_URL
        ▼
3. forge-prod                     --restart unless-stopped (a)         — the planning engine; needs
        │                                                               the bus (1) + the sidecar (2)
        ▼
4. specialist containers          already supervised (dual-role)       — PO + architect; forge
        │                         restart policies                      dispatches to them over the bus
        ▼
5. jarvis-frontdoor               systemd --user, Restart=on-failure   — LAST: the operator door;
   (jarvis + jarvis_reasoner)                                           publishes intake + consumes
                                                                        approvals over the bus (1)
```

**Why order matters, and why it is still safe if it is violated.** A systemd
**user** unit cannot hard-order against a system-scoped docker container, so
steps 2/3/5 are not strictly sequenced by systemd. The **restart policies close
every race**: if the sidecar (2) starts before the bus, or forge-prod (3) starts
before the sidecar, or the front door (5) starts before the bus, the process that
finds its dependency absent exits and is auto-restarted (`Restart=on-failure` /
`unless-stopped`) until the dependency answers. Ordering is an optimisation that
avoids churn on boot, not a correctness requirement — the loud-restart loop is
the safety net. (Contrast: nats and the specialists are system-scoped and already
`unless-stopped`, so they self-recover independently.)

## d. Receipts — the demonstrations

- `ops/receipts/DEMO-restart-policy-scratch-container.md` — a throwaway scratch
  container: `restart_policy.py` flips the policy with no restart (StartedAt
  unchanged), then a crashed process auto-restarts (`RestartCount` 0→1→2).
  Plus the two JSON receipts in `ops/receipts/demo/`.
- `ops/receipts/forge-prod-restart-policy-STATE-2026-07-13.md` — read-only live
  state of forge-prod (never mutated by this pass).
- `../jarvis/ops/receipts/DEMO-systemd-user-unit-supervised-restart.md` — a
  throwaway `systemd --user` unit with the same `Restart=on-failure` policy:
  kill the process → journalctl shows `status=9/KILL` → `Scheduled restart` →
  `Started`. Proves the supervision mechanism behind both units.

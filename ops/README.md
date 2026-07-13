# forge ops — service supervision & boot recovery (O-30, Phase E2-S3)

This directory holds the **operator-facing supervision** artifacts that close
gap **O-30** (host-reboot / power-loss auto-recovery for the un-supervised
operator-facing halves). Everything here is **authored + demonstrated**, never
applied to live services by this pass — live application is a coordinator step
(handoff rule 3). Sibling: `jarvis/ops/systemd/` (the jarvis front-door unit).

## In one minute

Every factory service auto-recovers on a reboot **except** three:

1. **forge-prod** — the planning engine, started with `docker run -d … forge serve`
   and (per the canonical runbook) **no `--restart` policy**.
2. **the jarvis Slack front door** — a bare `langgraph dev` dev-server, no supervisor.
3. **the forge autobuild-runner sidecar** — a bare `nohup … langgraph dev` on :8124.

O-30 closes all three: a `docker update` restart policy for (1), a systemd **user**
unit for (2) and (3). On an overnight power blip a James/Rich Slack request now
finds the front door and planning engine still up — or brought back automatically.

| # | Artifact | Closes | Applied by |
|---|---|---|---|
| a | `scripts/restart_policy.py` | forge-prod has no `--restart` | coordinator (`docker update`) |
| b | `ops/systemd/forge-langgraph-sidecar.service` | sidecar un-supervised | coordinator (`systemctl --user`) |
| b | `jarvis/ops/systemd/jarvis-frontdoor.service` | front door un-supervised | coordinator (jarvis venue) |
| c | this README §Boot order | who waits on whom | — |
| d | `ops/receipts/` | the demonstrations | — |

## a. forge-prod restart policy — `scripts/restart_policy.py`

Gives forge-prod `--restart unless-stopped` (matching nats' policy) via
`docker update` — a **metadata-only** change that does **not** stop, restart, or
recreate the container. Because no restart occurs, it needs **no Ack-Pending-0 /
worker-free drain** (that gate guards a *recreate*); the receipt says so
explicitly. Grammar mirrors `scripts/activate_planning.py` (`--dry-run` / apply /
`--rollback`, JSON receipts, idempotent).

```bash
# Preflight (read-only inspect; runs no docker update):
.venv/bin/python scripts/restart_policy.py --dry-run

# Apply the policy to forge-prod (the coordinator's live step):
.venv/bin/python scripts/restart_policy.py

# Roll back to docker's default ("no"):
.venv/bin/python scripts/restart_policy.py --rollback

# Rehearse/demonstrate on a THROWAWAY scratch container (never forge-prod):
.venv/bin/python scripts/restart_policy.py --container <scratch> --dry-run
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

Two long-running `langgraph dev` processes get supervised by the **user**
systemd manager (they bind the developer venv + a repo-local `.env`, both
user-scoped — no root needed). Both carry `Restart=on-failure` + `RestartSec=5`.

- `ops/systemd/forge-langgraph-sidecar.service` — the autobuild-runner sidecar
  (`autobuild_runner` graph on :8124, from `forge.langgraph.json`). **Prerequisite
  B**: the forge venv must carry `langgraph-cli` + provider extras
  (`uv sync --extra providers`, or the runbook pip line) so `langgraph` resolves.
- `jarvis/ops/systemd/jarvis-frontdoor.service` — the Slack front door
  (`jarvis` + `jarvis_reasoner` graphs). See `jarvis/ops/systemd/README.md`.

Both are **`systemd-analyze verify`-clean** and **NOT installed/enabled** by this
pass. Install (coordinator, per box that runs each service):

```bash
mkdir -p ~/.config/systemd/user
# forge sidecar (on the box running forge-prod):
cp ops/systemd/forge-langgraph-sidecar.service ~/.config/systemd/user/
# jarvis front door (jarvis venue):
cp <jarvis>/ops/systemd/jarvis-frontdoor.service ~/.config/systemd/user/

systemctl --user daemon-reload
loginctl enable-linger "$USER"                 # so user units survive logout / start at boot
systemctl --user enable --now forge-langgraph-sidecar.service
systemctl --user enable --now jarvis-frontdoor.service
systemctl --user status forge-langgraph-sidecar.service jarvis-frontdoor.service
```

> Enabling these **replaces** the ad-hoc `nohup … langgraph dev &` launches — kill
> any bare `langgraph dev` first so the port (:8124 / :2024) is free, then enable.

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

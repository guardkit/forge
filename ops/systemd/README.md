# ops/systemd — forge runner sidecar unit

This directory version-controls the systemd **user** unit that supervises the
forge autobuild-runner sidecar (`langgraph dev` on port 8124). The running box
already has this unit installed under `~/.config/systemd/user/`; the copy here is
the reproducible source of truth so a rebuild or fresh clone does not silently
lose the unit and its load-bearing environment.

Files:

- `forge-langgraph-sidecar.service` — the active runner sidecar (install this one).
- `forge-autobuild-runner.service` — a stale 2026-05-12 sibling, installed but
  disabled. Superseded. Leave it alone.

## Why the environment lives in the unit

The runner spawns the guardkit build subprocess with `env=os.environ.copy()`
(`autobuild_runner.py:1881`). So whatever environment the sidecar process holds is
exactly what the build half inherits. The unit's `[Service]` `Environment=` block
is therefore the durable seam — set the env there, in version control, not by hand
before launch. A value set by hand disappears on the next restart.

The load-bearing vars (see the unit's header comment for the per-var reasons):

- `OPENAI_BASE_URL=http://localhost:9000/v1` — the local llama-swap seat. The
  Player's OpenAI-shaped client talks to this local server, never to any cloud.
- `OPENAI_API_KEY=dummy` — only satisfies the client's key check. Not a secret.
- `GUARDKIT_HARNESS=langgraph` — the mission default harness. The 2026-07-26
  end-to-end rehearsal (build `build-FEAT-UPT1-20260726112342`, merged api_test
  `c5a04be`) ran with this var **unset**, which resolves to `langgraph`; the unit
  pins it explicitly so the durable shape matches the proven one. (The old
  `GUARDKIT_HARNESS=sdk` was a stale 2026-05-15-era pin — do not reinstate it.)
- `FORGE_GUARDKIT_PATH`, `FORGE_CONFIG_PATH`, `FORGE_DEFAULT_REPO`, `PATH` — the
  guardkit resolver, the launch inputs, and a PATH rich enough for the spawned
  subprocess's own tool calls.

### FORGE_NATS_URL — host-local, never copied into this unit

The broker credential is **not** written into this unit and **not** committed
anywhere in this repo. It must already be present in `forge/.env` on the host (a
git-ignored, machine-local file). This keeps the credential in the one existing
place it already lives — no new plaintext copy (the F10 discipline).

If `FORGE_NATS_URL` is missing from `forge/.env`, the runner cannot reach the
broker. Add it to `forge/.env` on the host (not to this unit, not to any tracked
file), then restart the sidecar (below) so the process re-reads it.

## Install / reconcile procedure

Reconciling means making the installed unit match this version-controlled file.
This is a coordinator step, done attended — never as an automated side effect.

1. Copy the unit into the user systemd directory:

   ```
   cp ops/systemd/forge-langgraph-sidecar.service ~/.config/systemd/user/
   ```

2. Reload the unit definitions:

   ```
   systemctl --user daemon-reload
   ```

3. Restart with a stop-wait-start (not a bare `restart`). Port 8124 is released
   only after the old process fully exits; a too-fast restart races the old
   listener and the new `langgraph dev` fails to bind:

   ```
   systemctl --user stop forge-langgraph-sidecar
   # wait for the port to release (a second or two is enough; the unit's
   # TimeoutStopSec=15 bounds the worst case)
   systemctl --user start forge-langgraph-sidecar
   ```

`langgraph dev` runs with `--no-reload`, so a restart is the ONLY way new code or
new environment is picked up.

## POST-RESTART VERIFY law (do not skip)

The environment is re-read **only** on restart. A process left running keeps its
old values silently — this is what caused the May exit-3 wall (defect F4): the
sidecar looked healthy while serving a stale environment. After every restart,
confirm the live process actually carries the intended env by reading its own
`/proc/<pid>/environ`:

```
tr '\0' '\n' < /proc/$(systemctl --user show -p MainPID --value forge-langgraph-sidecar)/environ \
  | grep -E 'OPENAI|GUARDKIT_HARNESS'
```

Expect to see `OPENAI_BASE_URL=http://localhost:9000/v1`, `OPENAI_API_KEY=dummy`,
and `GUARDKIT_HARNESS=langgraph`. If any is missing or stale, the restart did not
take the new environment — stop, fix, restart, and re-check before running a build.

## Boot order

The sidecar must be UP before forge-prod: `forge serve` fail-fasts on a dead
`FORGE_AUTOBUILD_RUNNER_URL`. A user unit cannot hard-order against the
system-scoped forge-prod container, so the ordering is closed by retry: the
sidecar's `Restart=on-failure` and forge-prod's own restart policy mean forge-prod
keeps retrying until the sidecar answers on port 8124.

## forge-sandbox-runner@.service — the factory's services inside a repository's sandbox

Rich's rule (2026-09-07): nothing the factory runs on a repository runs on
the host. The build runner and the deploy sidecar for a repository run inside
that repository's Docker Sandbox, on the factory's own clone of it. This
template unit (`forge-sandbox-runner@<sandbox name>`) holds one session open
in the named sandbox running `deploy/sandbox-runner.sh`, the shared bootstrap
forge writes into every registered repository. That script copies the
factory's code out of the read-only forge and guardkit mounts, makes a venv
with uv once, installs from the copies, and then starts and supervises the two
services: the deploy sidecar on port 8125 and the build runner (`langgraph
dev`) on port 8124, both inside the sandbox. The sandbox publishes them to the
host's loopback on the ports the repository's `deploy/profile.yaml` names
(`sidecar_publish`, `runner_publish` — for api_test, 8925 and 8924). The
script sets uv's "never download an interpreter" switch on the one command
that makes the factory's venv and does not export it, so the two services —
and the repository's own build venv beneath them, which guardkit pins to the
floor of that repository's `requires-python` — start without it.

It is modelled on the keeper beside it: `ExecStart=/usr/bin/sbx exec %i
deploy/sandbox-runner.sh`, `Restart=always`, `KillMode=process`. The
repository's deploy wrapper starts it on every deploy when the profile names
the two service ports; installing the unit file is an attended step, exactly
as for the keeper:

```
cp ops/systemd/forge-sandbox-runner@.service ~/.config/systemd/user/
systemctl --user daemon-reload
```

A sandbox created before the profile named the service ports keeps its old
shape (no clone, no mounts, no service ports): `sbx` cannot add those to a
sandbox that exists, so it is removed and created again, attended, before
the unit can do anything useful there. A repository registered before this
lane also carries the older copy of the wrapper in its own `deploy/` folder;
forge ships the file, so the refresh is one attended copy at the go-live
(`cp src/forge/cli/deploy_templates/sandbox-deploy.sh <checkout>/deploy/`,
and the same for `sandbox-runner.sh`, which such a repository does not have
at all). The host unit above
(`forge-langgraph-sidecar.service`) keeps running for repositories whose
sandbox does not carry the factory yet; forge-prod is pointed at one or the
other per repository by the repository map (the spec's Part O, rule 71).

## What the runner writes to the ledger today (must move behind the bus for the sandbox runner — L3)

The spec's Part O, rule 72: the ledger (`forge.db`) stays on the host and
forge-prod is its only writer; the sandbox runner never opens it. This is the
inventory of every place `src/forge/subagents/autobuild_runner.py` reaches
the ledger today, taken on 2026-09-07 so the L3 lane knows exactly what has to
move. Each line is cited as `file:line` and quoted; a test checks that every
cited line still exists and still says what is quoted here.

**It reads the ledger in one place, and writes it nowhere.** Every ledger row
about a build is written by forge-prod from what the runner streams back over
the graph's state — the runner process itself has no ledger connection and no
bus connection:

- `src/forge/subagents/autobuild_runner.py:490` — `is never constructed in` (the lifecycle emitter adapter, which would write transitions, is never built in production)
- `src/forge/subagents/autobuild_runner.py:491` — `no forge.db / NATS` (the sidecar runs in a separate process with neither)

The one read is the requeue sweep's prior-build liveness guard, which asks
whether an earlier build of the same feature is still running before it sweeps
that build's worktrees and branches:

- `src/forge/subagents/autobuild_runner.py:81` — `import sqlite3`
- `src/forge/subagents/autobuild_runner.py:2988` — `def _prior_build_status(build_id: str) -> str | None:`
- `src/forge/subagents/autobuild_runner.py:3004` — `from forge.cli._db_resolve import resolve_db_path`
- `src/forge/subagents/autobuild_runner.py:3006` — `db_path = resolve_db_path()` (this is the `FORGE_DB_PATH` read: the env, else `~/.forge/forge.db`)
- `src/forge/subagents/autobuild_runner.py:3013` — `"requeue sweep: no forge ledger at %s — the prior-build "` (an absent ledger is logged and the guard fails open — the sweep proceeds as it did before the guard existed)
- `src/forge/subagents/autobuild_runner.py:3018` — `uri = f"{db_path.resolve().as_uri()}?mode=ro"`
- `src/forge/subagents/autobuild_runner.py:3019` — `conn = sqlite3.connect(uri, uri=True, timeout=2.0)`
- `src/forge/subagents/autobuild_runner.py:3022` — `"SELECT status FROM builds WHERE build_id = ?", (build_id,)`

Where the path comes from on the host, and how it reaches the build
subprocess:

- `ops/systemd/forge-langgraph-sidecar.service:94` — `Environment=FORGE_DB_PATH=%h/forge-prod-state/.forge/forge.db`
- `src/forge/subagents/autobuild_runner.py:3857` — `env=os.environ.copy(),` (the guardkit build subprocess inherits the whole environment, `FORGE_DB_PATH` included; guardkit itself never reads that name — checked by grep on 2026-09-07)

**What this means for the sandbox runner.** `deploy/sandbox-runner.sh` unsets
`FORGE_DB_PATH` before it starts the services, so inside the sandbox the guard
finds no ledger, logs the line at 2889, and fails open exactly as it does on a
host with no ledger. That is the pre-guard behaviour, not a new defect, but it
is weaker than today: a re-drive of a feature whose earlier build is still
running would not be held back. For L3, the guard's one question — "is build
X still in a live state?" — should be asked of forge-prod over the bus or the
sandbox sidecar, and answered from the ledger there, so the runner never needs
the file. Nothing else in the runner touches the ledger: the receipts it
writes go to the receipts root as files (mounted read-write into the sandbox
from the profile's `receipts_path`), and guardkit's own per-build ledger
(`.guardkit/features/<FEAT>.yaml`) lives inside the build's worktree.

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

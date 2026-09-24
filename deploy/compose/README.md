# Forge's services, as containers

*Written 24 September 2026, stage 2 of the containerisation rollout gate.*

This folder is Forge's part of the estate bundle — the small set of files a
clean machine copies in order to run the factory's own services. It carries no
machine's address, no home path, no checkout and no host network. Everything
that belongs to a particular machine lives in one file, `.env`, and that file
is the whole of the difference between a laptop and a cloud machine.

## The four files

| File | What it is |
|---|---|
| `compose.yaml` | the three services, the network and the volumes |
| `compose.sandbox-runner.yaml` | one project's sandbox, looked after by a container. Only on a machine that has one; composed alongside `compose.yaml` |
| `.env.example` | every setting name the bundle needs, with no machine's values. Copy it to `.env` and fill in the lines marked CHANGE THIS |
| `settings.example.yaml` | the coordinator's own settings file, with every address written as a `${NAME}`. Copy it onto the read-only settings volume |
| `README.md` | this page |

## The three services

**The coordinator** is `forge serve` — the same program that runs today,
started the same way, with the two things that tied it to one machine taken
away: it is no longer on the host's network, and it no longer binds two
folders out of somebody's home directory. It writes the record, reads the
settings (which it cannot write), and dispatches work.

**The answer service** is the read-only answer to two questions about that
record: what commit a build was recorded as starting from, and which build
holds a deployment target. It changes nothing — no verb but `GET`, and the
record is opened in read-only mode so the store itself refuses a write. It is
a service of its own, and not a thread inside the coordinator, because the
thing that asks it lives on the far side of a sandbox boundary and has to be
able to ask while the coordinator is busy, restarting or applying migrations.

**The publisher** is composed in from its own fragment
(`src/forge/publisher/compose-fragment.yml`) rather than copied here, so there
is one description of it and the rollout reads the same file. It holds the one
credential that can write to a project's remote, which is why it is a separate
image, a separate user and a separate network.

## The one service that touches the host

**The sandbox runner** (`compose.sandbox-runner.yaml`) is the fourth service,
and it is the odd one out. It looks after one project's sandbox, and it
replaces the two host units that used to do that:

| It replaces | Which did |
|---|---|
| `forge-sandbox-keeper@<sandbox>` | held the sandbox awake, so it did not go to sleep thirty seconds after the last session ended |
| `forge-sandbox-runner@<sandbox>` | ran the project's own bootstrap inside the sandbox, and stopped it again |

A machine has one of these per project sandbox, which is why it is a file of
its own rather than a fourth service in `compose.yaml`: a machine with no
sandbox should not have to comment anything out. Bring the two up together:

```
docker compose --env-file .env -f compose.yaml -f compose.sandbox-runner.yaml up -d
```

**It is the only thing in this bundle that is given anything of the machine
beyond Docker itself, and it is worth saying why that is allowed.** A sandbox
is a small machine of its own, and the thing that makes sandboxes is the
*sandbox daemon*, which runs on the host. There is no more to containerise
about it than about the Docker daemon underneath this whole file. So the
service is given exactly two things and nothing else:

- **the daemon's socket**, bound at the one path the client insists on —
  `<storage root>/state/sandboxes/sandboxes/sandboxd/sandboxd.sock`. Put it
  anywhere else and the client quietly decides it is talking to Docker's
  hosted service and complains about authentication, which has nothing to do
  with what is wrong;
- **the client binary**, read-only. One statically linked file.

Both arrive as names in `.env` (`SANDBOXD_SOCKET_PATH`, `SBX_BINARY_PATH`), so
neither this file nor the compose file carries a path belonging to a machine.
The service joins **no network at all** (`network_mode: none` — everything it
says, it says over that socket), publishes **no port**, and binds no folder.

**It runs as the machine's own user**, not as the image's `forge` user, because
the daemon's socket is owner-only: `FACTORY_HOST_UID` and `FACTORY_HOST_GID`.
That is the one exception in the bundle and the socket's permissions are the
whole of the reason.

**Its own small volume** (`sandbox-client-state`) is the client's writable
state folder, and it is the one volume in the bundle that still needs a
one-off hand-over — because this service alone runs as the *machine's* user
rather than the image's, so the image cannot know in advance who to make the
folder for. Hand it to the same two numbers `.env` gives:

```
docker compose --env-file .env -f compose.yaml -f compose.sandbox-runner.yaml create
set -a; . ./.env; set +a
docker run --rm -v <project>_sandbox-client-state:/v1 \
  alpine chown "${FACTORY_HOST_UID}:${FACTORY_HOST_GID}" /v1
```

### The stop is the point of it

The client runs out here and all the work runs in there, so **ending the client
ends nothing**: the project's bootstrap keeps running inside the sandbox and
keeps supervising its services. That is written out at length in the unit this
replaces, and it cost a day: on 11 September 2026 four supervisors had piled up
inside one sandbox, all fighting over the same two ports, the one holding them
older than the code installed underneath it — so a build served a mixture of
old and new code and died a second after its gate was tapped, with nothing
reporting a problem.

So a stop signal here goes back through the same door the start went through
and asks the project's own bootstrap to stop itself — one word,
`SANDBOX_BOOTSTRAP_STOP_ARGUMENT` — and **waits for it**. Docker must wait too,
which is why `stop_grace_period` is ninety seconds rather than the default ten
seconds. The same stop runs before every restart of the session, for the same
reason systemd ran its `ExecStop` before every automatic restart — and it is
made with the same settings the start was made with, so a bootstrap that finds
its own work through one of them is asked to stop the thing it was asked to
start.

**And the stop has to have worked.** A stop that ends nonzero, or that does not
finish within `SANDBOX_STOP_TIMEOUT_SECONDS`, means the work may still be
running in there, so nothing is started on top of it: the service tries the stop
`SANDBOX_STOP_ATTEMPTS` times (default three, `SANDBOX_STOP_RETRY_SECONDS`
apart), and if it still will not work it says so in one sentence and **exits
non-zero** rather than adding a second supervisor or reporting a stop it did not
achieve. `docker ps` then shows that exit instead of a tidy zero, the restart
policy brings the container back, and the first thing it does is try the same
stop again. In that one case the hold on the sandbox is **left in place on
purpose**: releasing it would let the sandbox fall asleep about thirty seconds
later and cut the work off mid-flight without it ever having been asked to stop,
and would leave the machine looking tidy while the problem was still inside.

**Check the project's bootstrap takes that word before you start this
service.** An older one reads no arguments at all: it would ignore the word and
start a *second* supervisor instead, and a restart would add two where it meant
to remove one.

### What is not proven here

**Recovery after the sandbox daemon is restarted.** The daemon is shared with
every other sandbox on the machine, including live ones, so that proof cannot
be made on a disposable sandbox on a working machine. It is rollout step 6,
with the owner present. Everything else was proven on a disposable sandbox on
24 September 2026: the client working from inside the container, the start, the
stop that really ends the work inside, ten stop/start cycles leaving exactly
one supervisor, and the container's own restart leaving one.

## Which network each one is on, and why

There is one declared network, `factory`. It replaces the host network the
coordinator runs on today.

- The **coordinator** is on `factory` *and* on the publisher's own network. It
  is the only thing that is on both, and that is the entire route to the
  publisher: nothing else on `factory` can reach it, and neither can anything
  on this machine, because the publisher publishes no port.
- The **answer service** is on `factory` only.
- The **publisher** is on its own network only.

The bus, the memory service and the model seats are on `factory` too, from
their own compose files. They are named here by address, never started here.

## Which volume each one uses, and who writes it

| Volume | Holds | Its one writer |
|---|---|---|
| `forge-ledger` | the record and the companion files beside it | the coordinator |
| `forge-settings` | the coordinator's settings file | nobody at run time — the machine puts it there before start |
| `forge-evidence` | the receipts and records a build writes | a build's runner inside its sandbox |
| `forge-home` | the coordinator's own small state | the coordinator |
| `forge-publisher-state` | the publisher's own copies of projects' commits | the publisher (declared by the included fragment) |
| `nats-jetstream` | the bus's own store | the bus (declared by the bus's own compose file) |

Splitting the first two is the point of the exercise: today the settings file
sits in the same writable folder as the record, so anything that can write the
record can rewrite the settings. Here the settings volume is mounted
read-only, and the answer service and the publisher get the record read-only
too.

The record is mounted as a **folder**, never as the one file. The store keeps
companion files beside the record while it is in use, and a reader given only
the one file finds no tables at all.

## One machine address, and one only

A sandbox is a separate small machine with its own engine and its own network.
It is not on `factory` and never will be, so a service name does not reach it
and it does not reach a service name. Every crossing of that boundary goes
through a single address this machine supplies — the **factory gateway
address** — and each crossing is permitted per route: this listener, this
port, this allowed source, refused to everything else including the local
network.

That is why exactly one port is published in `compose.yaml`: the answer
service's, on the factory gateway address, so a project's deploy helper inside
its sandbox can ask its question. Everything else talks by service name.

Section 7 of `docs/factory-containerisation-design-pass-2026-09-23.md` in the
ai-transition repository has the full route table and says why a blanket
"allow the sandbox subnet" rule is the wrong shape.

## Addresses are names

Every address in `settings.example.yaml` is written `${SOME_NAME}`. The
settings loader fills those in from the environment the service was started
with — which compose fills from `.env` — and **refuses by name** when nothing
set one, saying which setting and which line. It never picks an address for
you. An address quietly defaulted to a machine's loopback is how a
containerised coordinator ends up healthy and reaching nothing.

## Bring it up

```
cp .env.example .env
# edit .env: the factory gateway address, the two sandbox ports, and the two
# publisher file paths
docker compose --env-file .env up -d
docker compose ps
```

**There is no longer a one-off ownership step before the first start.** It
used to be here — a `chown 1000:1000` over the four coordinator volumes,
because Docker makes a fresh named volume root-owned wherever the image has no
folder of that name and the services are not root. Release `2026.09.24-4`
makes those four folders in the image, owned by the `forge` user, so Docker
copies that ownership onto a fresh volume and there is nothing left to hand
over. The publisher's own image has always done the same for its one folder.
(The publisher's settings file's `state_dir` must be `/home/publisher/state`,
which is what `settings.sample.json` says.)

One thing still has to be done once, before the first start.

**Put the two files where the services expect them.** The coordinator's
settings file goes on the `forge-settings` volume (copy
`settings.example.yaml`, fill in the project's `org/name` and its sandbox's
name), and the publisher's credential file goes where `.env` says. The
credential's contents never appear in any file here, in any image, or in any
log.

One more thing worth knowing: **compose prefers the shell's own environment
over `--env-file`.** If your shell exports one of the names in `.env` — a
`FORGE_NATS_URL` from an old habit, say — that value is what gets used, and
`.env` is silently ignored for it. Bring the bundle up from a clean shell.

## Take it down

```
docker compose down          # stops and removes the containers; keeps the volumes
docker compose down -v       # and removes the volumes too — the record with them
```

`down -v` removes the record. On a real machine that is never what you want.

## What is deliberately not in this file

- **The bus.** It is reached at the service name `FORGE_NATS_URL` gives, on
  the `factory` network, and it is started by its own compose file, which the
  estate bundle composes alongside this one. Forge does not own it. **Its
  storage must be provisioned before this bundle starts**: the coordinator
  needs the bus's `agent-registry` key-value bucket and its `PIPELINE` stream,
  both defined in the bus's own repository (`nats-infrastructure/kv/` and
  `streams/`, each with a provisioning script beside its definitions). Start
  the bus, run those two scripts, then start this bundle — otherwise the
  coordinator restarts in a loop whose first message is a programmer's error
  (`registry unavailable`, then `stream not found`). The live bus was
  provisioned months ago, which is why nobody met this until a reviewer brought
  the bundle up against a bare bus on 24 September 2026.
- **The memory service and the model seats.** The same: other repositories'
  compose files, addresses in `.env`.
- **The sandboxes themselves**, and the build runner and deploy helper that run
  inside each project's sandbox. Making a sandbox is still an attended step:
  the sandbox daemon does it, the project's own wrapper asks for it on a first
  deploy, and nothing in this bundle creates, removes or reconfigures one.
  `compose.sandbox-runner.yaml` holds an existing one awake and runs the
  project's own bootstrap inside it; the coordinator reaches into it at the
  factory gateway address.
- **The front door** (the Slack side) and the bus gateway — jarvis's own
  compose file, a later rollout row.

## Two things the design met in the code, recorded rather than smoothed over

1. **The coordinator's health endpoint is also a probe of the bus.** The
   image's own healthcheck asks `/healthz` with `curl -fs`, and that endpoint
   answers 503 until the daemon's subscription to the bus is live. As a
   container healthcheck that restarts a coordinator whenever the bus is slow.
   `compose.yaml` therefore overrides it with a probe that asks only whether
   this service is answering at all. Whether the bus is reached is the estate's
   own check, not a restart trigger.
2. **A project with a sandbox must still appear in
   `planning.target_repo_paths`**, which the model describes as an absolute
   local working-copy path. There is no checkout on the host in this design, so
   the example gives a path inside the container with nothing bound behind it.
   Untangling that coupling is its own small pass.

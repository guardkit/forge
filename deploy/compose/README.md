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

Two things have to be done once, before the first start.

**Make the volumes usable by the image's own user.** Docker creates a fresh
named volume owned by root wherever the image has no directory of that name,
and the services run as an unprivileged user, so the coordinator cannot write
its own record until the volumes are handed over:

```
docker compose --env-file .env create
docker run --rm \
  -v <project>_forge-ledger:/v1 -v <project>_forge-evidence:/v2 \
  -v <project>_forge-home:/v3 -v <project>_forge-settings:/v4 \
  alpine sh -c 'chown 1000:1000 /v1 /v2 /v3 /v4'
```

The proper fix is in the image — `/var/lib/forge`, `/var/lib/forge-evidence`
and `/etc/forge` created and owned by the `forge` user in the Dockerfile, at
which point Docker copies that ownership onto a fresh volume and this step
disappears. That is a release change, not a compose change, and it is written
down here so it is not forgotten.

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
  estate bundle composes alongside this one. Forge does not own it.
- **The memory service and the model seats.** The same: other repositories'
  compose files, addresses in `.env`.
- **The sandboxes**, and the build runner and deploy helper that run inside
  each project's sandbox. A sandbox is made by the sandbox daemon on the host,
  not by compose. The coordinator reaches into it at the factory gateway
  address.
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

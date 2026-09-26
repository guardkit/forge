# The estate bundle — the only thing a new machine clones

*Written 24 September 2026, stage 4a of the containerisation rollout gate.*

This folder starts the factory's own services on a clean machine — a laptop or
a cloud machine — from one set of files and one env file. It carries no
machine's address, no home path, no checkout and no host network.

**Forge owns it because Forge is the factory's coordinator.** The estate is
what the coordinator needs around it in order to work, so the list of those
parts belongs with the coordinator, in the same repository and at the same
release. As the estate grows to the memory service and the Slack front door,
those do **not** get copied in here: each of them has a compose file in its own
repository, and this file composes that one, exactly as it composes Forge's own
two today. One description of each service, in the repository that owns it.

## What is here

| File | What it is |
|---|---|
| `compose.yaml` | composes Forge's two compose files and adds the bus, the one-shot that provisions it, the one-shot that says the bus is ready, the memory service's two containers, and the Slack front door with its bus gateway |
| `compose.external-bus.yaml` | the overlay for **external bus mode** — the estate started against a bus that is already running and belongs to somebody else. *Whose bus is it* below says when |
| `.env.example` | every setting name the whole estate needs, with no machine's values. Copy to `.env` |
| `estate-pins.conf` | what the estate's own two images are built from. Part of the release, never edited per machine. Not named `.env`, because this repository ignores the whole `.env` family as a secrets fence and these are pins, not secrets |
| `build-estate-images.sh` | builds those two images, and fills the volume holding the bus's own config, from the bus repository at its pinned commit |
| `provisioner/Dockerfile` | the one-shot image: the NATS project's tool image plus `bash`, and the estate's own read-only comparison of a running bus with the pinned definitions |
| `provisioner/compare-bus-with-definitions.sh` | that comparison. It reads a bus's own monitoring route and the pinned definitions and compares them field by field. It never writes to a bus |
| `estate-check` | the checks, one sentence per item |
| `factory-hello` | asks the coordinator what it can reach, with the address it used |

`settings.example.yaml` — the coordinator's own settings file — is **Forge's**,
in `../compose/`, and is referenced from here rather than copied.

## Walk (a): a clean local machine

1. **Install Docker**, and `sbx` if this machine will look after a project's
   sandbox.
2. **Clone this bundle.** No product repository is cloned: the factory's code
   is in the release images, and a project's code lives in its own sandbox.
3. **Build the estate's own two images and fill the bus's volume:**
   `./build-estate-images.sh`. It fetches the bus repository from GitHub at the
   commit `estate-pins.conf` pins, builds the bus from **the bus repository's
   own Dockerfile**, builds the one-shot, and puts the bus's config and
   provisioning scripts into a volume. The five release images (`forge`,
   `forge-publisher`, `fleet-memory-mcp`, `fleet-memory-relay` and `jarvis`) come from
   `../../scripts/build-release-image.sh` or from a registry.
4. **`cp .env.example .env`** and fill in the lines marked CHANGE THIS: the
   factory gateway address, the two sandbox ports, and the paths of the secret
   files. Put the secret files where it says — **and put them there before
   `up`**: a path that does not exist when a container starts is created by
   Docker as an empty folder owned by root, in the wrong place, and the service
   then refuses a folder where it wanted a file. Two reviews met this on
   25 September 2026 with the publisher's two paths under `/etc/factory/`.
5. **Put the coordinator's settings file on the settings volume.** Copy
   `../compose/settings.example.yaml`, fill in the project's `org/name` and its
   sandbox's name, and put it on the `forge-settings` volume as `forge.yaml`.
   Nothing writes that volume at run time; it is mounted read-only.
6. **`sops exec-env "$NATS_SECRETS_FILE" './estate-check host'`.** Seven items,
   one sentence each. It exits non-zero if any of them is not met. Run it the
   way you are about to start the estate — one of the things it checks is that
   the bus's eight account passwords have values, and they only do in the child
   process.
7. **Start it**, with the bus's account passwords passed in from a child
   process:

   ```
   sops exec-env "$NATS_SECRETS_FILE" 'docker compose --env-file .env up -d'
   ```

   On a machine that looks after a project's sandbox, **change the env file's
   profiles line to `COMPOSE_PROFILES=local-bus,sandbox`** — do not pass
   `--profile sandbox` on the command line. Checked on Compose v5.2.0 on
   26 September 2026: `--profile` **replaces** the env file's profiles rather
   than adding to them, so `--profile sandbox` in local mode takes the bus and
   the provisioner out of the project. It fails loudly rather than quietly —
   Compose refuses the project outright, because `bus-ready` then names a
   service that is not there — but the line to change is the env file's.
8. **`./estate-check services`** — from an ordinary shell. Reading a running
   estate needs no password: the passwords reach the bus as files, so the
   bundle renders without them, and the one thing this check asks the bus
   itself goes over the bus's own monitoring route, which takes no credential.
9. **`./factory-hello`** — also from an ordinary shell. One line each for the
   bus, the answer service, the publisher and the two addresses into a
   project's sandbox, with the address it used on every line.

**Which of the four need a secret:** the start does, and the host check does
because it checks the values are there. The last two do not. Before 24
September's review all four did, because eight password names were interpolated
into the compose file with `:?` and every command that had to render it refused
in a clean shell — a reviewer ran steps 8 and 9 exactly as written against a
completely healthy estate and was told the coordinator was not running and the
bus was not in the estate. Both were. Neither tool could tell "this would not
render" from "that service is down"; both now say which, in those words.

## How the bus's passwords travel, and why no log has one in it

The bus has eight account passwords. They live encrypted in one file, they are
never in any file in this bundle, and `sops exec-env` puts them into the
environment of the `docker compose` command as a child process. From there:

- **each one goes to the bus as a file** under `/run/secrets`, and a four-line
  wrapper puts them into the environment of the bus's own entrypoint *process*.
  They are not in the container's declared environment, where `docker inspect`
  shows them to anybody who can reach the Docker daemon. The bus repository's
  own compose file does put them there; this is the one thing the estate does
  differently from it, and the bus's entrypoint cannot tell;
- **the one-shot that provisions the bus never holds one.** Its address is a
  plain `nats://nats:4222` with no credential, and the provisioning account's
  password arrives as a file and is written into the `nats` client's own
  context file. That is what fixed the blocker this bundle's review found on 24
  September 2026: the bus repository's two provisioning scripts *print* the
  address they are given, so an address of the usual
  `nats://user:password@host` form put the password into the one-shot's
  container log at every start, on every machine, and container logs routinely
  leave a cloud box. Nothing was ever exposed — the bundle is not rolled out,
  and only throwaway passwords ever went through it. Anything either script
  prints is passed through a redaction step as well, which is a second line of
  defence and not the fix;
- **one is still in a container's environment**: the coordinator's
  `FORGE_NATS_URL`, which carries the `forge` account's password because
  Forge's own code reads its bus address from that one setting. That is Forge's
  code to change, not this bundle's, and it is listed with the rollout
  preconditions below.

## Memory — what the factory remembers

**What it is.** The factory's memory across sessions and across builds: what was
decided, what a build found, what was said about a repository last week. Without
it every session starts from nothing.

**Two containers, because it does two things.**

- **`memory`** answers questions over MCP. It is the memory repository's
  resident service, and **its caller is a Claude session**, from outside the
  estate. Inside the estate it is reached at the service name `memory`; a
  project's sandbox is a separate small machine and reaches it at the factory
  gateway address, which is why it is the third and last published port here;
- **`memory-relay`** listens on the bus and writes what it hears into the store.
  It **publishes nothing** — nothing calls it — and it keeps one small file of
  its own, in its own volume, saying when it started and when it last wrote
  something. That file is the only sign of life it gives, because a clean write
  is completely silent: the memory flywheel once went dark for a month and
  nothing said so.

**The coordinator has a memory of its own, and it is NOT the service above.**
Forge reads the factory's memory itself, at gate time, **straight out of the
store** — it does not ask the `memory` service, and no Forge code reads
`FLEET_MEMORY_URL` at all. So there are two paths to memory here: a Claude
session's, through the service, and the coordinator's, through the database.
They use the same store and nothing else in common.

That matters because of what happens when the coordinator is not told about it.
Forge reads five names (`FLEET_MEMORY_ENABLED`, `FLEET_MEMORY_PG_DSN`,
`FLEET_MEMORY_EMBED_URL`, `FLEET_MEMORY_EMBED_MODEL`,
`FLEET_MEMORY_EMBED_DIMS`), and with them unset it does not refuse: it logs one
line — `memory: OFF` — gives the gate an empty reader, and carries on answering
its health route perfectly. **Until 25 September 2026 this bundle set none of
them**, so an estate brought up from it had a memory service answering, every
check passing, and a coordinator remembering nothing. Replacing the live
coordinator with it would have turned the factory's memory off and said nothing
— which is this file's own sentence about the flywheel going dark for a month,
happening again.

So now: `.env` **has to say** `FLEET_MEMORY_ENABLED=true` or `false` (there is
no default, and the coordinator refuses to start on anything else), the store's
address reaches the coordinator as the same **file** the two memory containers
get, the three embedding lines are the same three values, and **`estate-check
services` item 8c-forge** reads the coordinator's own memory line out of its own
log. Item 8c — the service answering — never was evidence that anything in the
estate used memory, and it is not now; 8c-forge is.

**What changed on 25 September 2026.** Both of them ran on the **host's own
network**, and the relay bound a folder under a home directory
(`~/.local/state/fleet-memory`). The design's inventory row says what that was:
habit. They now sit on the `factory` network like everything else, the relay's
folder is the named volume `memory-state` with the relay as its one writer, and
the marker's path inside the container is a setting rather than a guess.

**Its database is outside this estate.** It is a Postgres with pgvector, on
another machine, and this bundle reaches it at an **address** — one it never
writes down, because that address carries the store's password in it. The
address arrives the way the bus's account passwords do, from a child process,
and compose hands it to the two containers as a **file**; a small wrapper puts
it into the environment of each service's own process and nowhere else. It is in
no file here, no image, no container's declared environment and nothing either
service prints. **Where that database should live is an open question the design
has not answered** — it is not in the estate, it is not in the two walks, and a
clean machine reaches the existing one or is given one of its own. That is the
largest thing still outstanding about memory.

**What is deliberately not here:** the database above, and the **embedding
service** (the model that turns text into the numbers the store searches on).
Both are addresses in `.env`. The embedding service is a model seat, and model
seats are out of this gate by the same rule as the others: the GPU is where the
GPU is, and on another machine it is a line in the env file.

**Two things about the two memory images, said here rather than left to be
found.**

- **They are built on a newer Python than the ones running today.** A release
  refuses a Dockerfile that pins no base digest, so the memory repository's two
  Dockerfiles gained the release's one base digest on 25 September 2026 — and
  that digest is **Python 3.14.4**, where the images running today were built
  from a floating `python:3.12-slim` and are on **3.12.13**. That is a
  two-minor-version jump, and it arrives for anybody who rebuilds fleet-memory
  from its own compose file as well. It was tested, not assumed: the package
  installs, both entry modules import, and a real message went end to end on
  3.14. But it is a change of its own and should not be read as only a pin.
- **Both run as root**, which the release's other images do not — the
  publisher's own proof refuses root outright. That is how the memory
  repository's Dockerfiles have always been, so it is not a regression, but
  these are release images now. `../../scripts/verify-fleet-memory-image.sh`
  says so in every release build rather than leaving it unasked, and closing it
  is a change in **that** repository: a non-root user in both Dockerfiles, and
  `/var/lib/fleet-memory` created in the relay's image owned by that user,
  because Docker fills a fresh named volume from what the image has at that
  path, ownership included.

## The Slack front door, and the bus gateway

**What they are.** `front-door` is where Rich approves a spec, taps a build
gate and says merge — it serves the two graphs the jarvis repository declares.
`bus-gateway` carries that traffic onto the bus and the factory's answers back.
They are **two start commands over one image**, the same shape the coordinator
and the answer service already have, and they replace the two host units
`jarvis-frontdoor` and `jarvis-serve-nats`, which ran a checkout's virtual
environment against a settings file under a home directory.

**Neither publishes a port, and that is not an oversight.** Slack pushes
nothing to this estate. The reply path is **socket mode**: the front door dials
*out* to Slack over a WebSocket. So there is no inbound route to open, no
public address to arrange, and no tunnel — none of which would be true of a
service that receives Slack events over HTTP, which is why this is written down
rather than left to be inferred. What the front door does serve is its own
health route, on the `factory` network, which is what `estate-check services`
item 8h asks.

**The two Slack credentials are files.** A bot token (it posts and edits
messages) and an app-level token (it opens that WebSocket), both passed in from
a child process the way the bus's passwords are, handed to each service as a
file under `/run/secrets`, and put into the process's own environment by the
wrapper. Neither is in any file in this bundle, in the image, in either
container's declared environment, or in anything either service prints. The bus
password travels the same way; the bus address in `.env` is a plain service
name and the wrapper refuses one that carries a credential.

**Both services are given both tokens**, because both live host units have both
today. Whether both *should* run the Slack reply path is a question for this
front door's owner — it is not this bundle's to decide quietly, and it is not
changed here.

**The front door's saved threads survive being replaced** — added 26 September
2026, because until then they did not. A review drove the release image itself: a
thread created through the front door's API was still there after *stopping and
starting* the same container, and **gone — 404 — from a replacement container
built from the identical image**. Rich's approvals and merge words live in those
threads, and replacing a container is the ordinary way this estate takes a new
release, so that was a build's worth of conversation lost at every upgrade,
silently.

So `front-door` has a volume of its own, `front-door-state`, and the **front door
is its only writer** — the gateway keeps no files and mounts nothing. Three
things about it are worth knowing rather than finding out:

- **it is mounted at `/app/.langgraph_api`, which is not a tidy choice.** The
  development server writes its threads, runs and checkpoints under the
  *relative* path `.langgraph_api` in its working directory, and there is no
  setting or flag for anywhere else; the working directory has to stay `/app`
  because `langgraph.json` names its two graphs as `./src` paths. The volume
  therefore goes where the server already writes, rather than the code moving to
  suit the volume;
- **a fresh volume is writable with nobody chowning anything.** Docker fills a
  new named volume from what the *image* has at the mount point, ownership
  included, so the jarvis image creates that directory owned by the user it runs
  as. A jarvis image built **before 26 September 2026 does not**, and a fresh
  volume on one of those comes up root-owned — the front door would answer its
  health route and then lose every approval. The wrapper refuses to start on
  that, by name, rather than letting it happen quietly;
- **`docker compose down -v` deletes it**, the same as the bus's store and the
  relay's marker. That is a run's storage going, and it means those threads go
  with it.

What this does **not** cover: the routing-history **traces** both jarvis services
write under `/home/jarvis/.jarvis/traces`. They are diagnostic offload, nothing
reads them back, and both services discard them when their container goes. Giving
each service its own volume for them is a small change and a separate decision;
it is written down here rather than left unsaid, because the review that found
the threads found these as well.

### The open item: it is a development server

`langgraph dev` is the langgraph CLI's **development** server. It is what the
live host unit has always started, and it is what this image runs. Putting it
in a container changes *where* the front door runs, not *what* runs — and
inventing a serving layer the jarvis repository does not have would be a much
larger thing, done quietly, in the middle of a packaging pass.

The production path that CLI offers is `langgraph up`, which runs a
**closed-source API server image requiring a licence key**. No licence is baked
into this image and none is read. So:

- **what should serve these graphs in production is open**, and belongs to the
  front door's owner: a licence for that server, or a small server of the
  repository's own that runs the compiled graphs;
- **nothing about it is hidden.** The image's own Dockerfile says it, this page
  says it, and the commit that made the image says it.

One consequence already taken, in the jarvis repository: `langgraph-cli` was
declared only under `dev`, so installing the thing that serves the front door
also installed pytest, ruff and mypy. It is now named on its own as
`front-door`, and the image carries no test or lint tooling.

### What is not here

- **The Slack workspace itself.** The app, its scopes, the channel and who is
  in it are outside this bundle entirely. Nothing here creates or configures
  them, and no check in this bundle touches that workspace: item 8h proves the
  server is up and its graphs loaded, not that Slack answers.
- **Public reachability.** There is none to arrange, because nothing inbound is
  expected — see above. If this front door ever moves to receiving events over
  HTTP, a public address or a tunnel becomes the machine's business and a new
  published port, with its own per-route rule, becomes this bundle's.

## Walk (b): a cloud machine

**The same nine steps, the same bundle, the same images.** Only `.env` differs,
in three places:

1. **the addresses** — the model seats, and anything still on the first
   machine;
2. **storage** — if that machine's data disk is mounted somewhere of its own;
3. **the sandbox** — either a sandbox daemon on that machine (the same as
   local, and recommended first, because it is the same code path) or Docker's
   hosted sandbox service.

If a step in walk (b) has no counterpart in walk (a), the design has failed and
should be changed rather than documented around.

## Why the bus is provisioned before the coordinator starts

A bus that is merely running is not enough. The coordinator needs the bus's
`agent-registry` key-value bucket and its `PIPELINE` stream to register at all;
without them it restarts in a loop whose first message is a programmer's error.
The live bus was provisioned by hand months ago, so nobody met this until a
reviewer brought the bundle up against a bare bus on 24 September 2026.

So it is a line in the file rather than a sentence somebody has to remember:
`nats-provision` runs the **bus repository's own** provisioning scripts, from
the same pinned commit as the bus's config, and the coordinator waits for it to
finish **successfully** before it starts. The scripts are safe on every start —
each bucket and stream is checked, then created or updated — so this costs a
few seconds at every start and nothing else.

Two things worth knowing about them, met on 24 September 2026:

- the two scripts overlap. `provision-kv.sh` creates the four buckets with
  their settings, and `provision-streams.sh` then provisions the same four
  again from its own list, which has only a name and a time-to-live. For the
  two buckets that have a time-to-live it tries an update the broker refuses,
  and prints `[ERROR] KV pipeline-state — failed to update bucket`. The buckets
  are correct — the first script made them — and the one-shot still exits 0, by
  the scripts' own design. It is the bus repository's to fix, not the
  factory's;
- **because those scripts exit 0 whatever they print**, "the one-shot
  succeeded" is not evidence that the bus is provisioned. That is why
  `estate-check services` asks the **bus itself** what it holds and compares
  that with the bus repository's own definitions.

## Whose bus is it — local and external bus mode

*Added 26 September 2026, build item E1 of the rollout design.*

**Local bus mode** is what this bundle has always done: it starts its own bus
and provisions its storage. It is the clean-machine path, the cloud path, and
what every earlier proof used. Nothing about it has changed.

**External bus mode** is for a bus that is **already running and belongs to
somebody else**. There are two situations, and they are the same shape:

* the first rollout on this machine, where the live bus is **kept** while the
  record moves. It holds every reader's position and every message still
  waiting, and moving the record and swapping the bus in one window is two
  irreversible moves at once;
* a cloud machine joining a bus it did not start.

**One line in the env file decides, and three carry it:**

```
BUS_MODE=external
COMPOSE_PROFILES=
COMPOSE_FILE=compose.yaml:compose.external-bus.yaml
BUS_EXTERNAL_NETWORK=<the network that bus is already on>
```

Compose reads `COMPOSE_FILE` and `COMPOSE_PROFILES` out of the env file itself,
so `docker compose --env-file .env up -d` stays the whole command in both modes.
`estate-check host` **refuses** (item 7b) if `BUS_MODE` and those settings
disagree, because a half-set mode is the one state that could start a second bus
beside the one being kept.

**What external mode does NOT do**, and must never be made to do:

* **it never provisions.** The mutating one-shot is behind the `local-bus`
  profile, so in external mode it is not in the project at all — not for `up`,
  not for `restart`, not for anybody typing `docker compose run`;
* **it never mutates the bus in any other way.** It joins the bus's network as
  one that **already exists**, so `docker compose down` leaves the network
  exactly where it was, and it removes nothing and recreates nothing;
* **it never writes to the bus's storage.** What it does instead is **read**:
  `bus-ready` asks the bus's own monitoring route — which takes no credential —
  and compares the answer, **field by field**, with the definitions this release
  pins on the read-only `bus-source` volume. Anything missing, different or
  unreadable **refuses**, names the stream or bucket and the field, prints what
  the definitions say and what the bus says, and **updates nothing**. The
  coordinator then never starts. A difference between a running bus and the
  pinned definitions is settled before a rollout, not during one.

**Why the comparison is not the provisioning scripts' preview mode**, which was
the first idea: for a stream or bucket that already exists those scripts print
`Would check/update` and return **before comparing a single field**
(`streams/provision-streams.sh:146-152`, `kv/provision-kv.sh:134-141`). A clean
preview is not evidence of a matching bus, and its wording is not evidence of a
mismatched one.

**The two sides are spelt differently**, which is why this is a script rather
than a diff: the definitions say `work` and `7d` and the bus answers
`workqueue` and `604800000000000`; a bucket is a stream called `KV_<bucket>`
whose `max_msgs_per_subject` is the bucket's history and whose `max_msg_size` is
its maximum value size. Only the fields the definitions **name** are compared: a
field the bus reports and the definitions do not pin has nothing to be compared
against, and inventing an expectation for it here would be this repository
pinning the bus's storage, which belongs to the bus's repository.

**`bus-ready` is the one bus dependency, in both modes.** Until today four
services waited on the provisioner finishing. That is the right wait in local
mode and an impossible one in external mode, where the provisioner is not in the
project — and Compose does not shrug at that: **a service that depends on a
service whose profile is off makes Compose refuse the whole project.** So the
dependency is replaced rather than weakened. In local mode `bus-ready` waits for
the provisioner and then for the bus's health route, which means exactly what
the old line meant; in external mode it waits for the health route and makes the
comparison.

**In external mode the estate's own checks find the bus by address.** Items 8,
8b and 8i are asked at `BUS_MONITORING_ADDRESS` from inside the estate, never by
this project's own `docker compose ps -q nats` — which in external mode would
find nothing every time, and report a perfectly healthy bus as absent.

## The two checks

`estate-check host` — before anything starts. Eight items: the machine
qualifies for a sandbox at all; Docker; the sandbox tool and its daemon; the
release images; the volumes and the disk; the secret files (present or missing,
never a value, and readable by nobody but their owner); every setting name the
composed files require having a value; and (item 7b) that `BUS_MODE` and the
compose profiles and files in force say the same thing.

`estate-check services` — after. The bus answers; its buckets and streams
exist, asked of the bus itself over its own monitoring route rather than taken
from the one-shot's exit code; the coordinator's own health route; the answer
service answering for a build nobody wrote down; and the publisher answering
the coordinator **and refusing the answer service**.

It also asks the memory service at the address `.env` gives, and asks the memory
relay for its progress marker — the relay answers nobody over a network, so
"has it written the file it writes when it starts" is the honest question.

And it asks the **coordinator** whether its own memory is on (item 8c-forge),
by reading the line Forge writes about itself at every boot: `memory: ON`,
`memory: OFF` or `memory: DEGRADED`, and there is no fourth answer. This is a
different question from "does the memory service answer", because the
coordinator reads the store and not the service; see *Memory* above for why
that distinction cost this bundle a silent failure until 25 September 2026.

**One of its items asks nothing here, and says so.** The model seat is in the
design's item 8 and is not in this bundle, so with no address in `.env` it
prints *not checked here* and counts as **not passed**, exactly as item 9 does.
A run that reported it as a pass would be saying it had asked something it never
asked. Give it an address and the check asks it. (The memory service was in the
same position until 25 September 2026, when it joined the bundle.)

**What it does not prove about the routes.** Section 7 of the design asks this
check to prove every permitted direction and **every forbidden** one —
including from the local network and from unwanted factory or sandbox callers —
and to repeat them after a restart. It proves one forbidden direction: the
answer service cannot reach the publisher. It makes no probe from the local
network and it does not repeat anything after a restart. That is recorded with
the rollout preconditions below, and it is a gap in the **check**, not only in
the firewall rule.

**Item 9 has never run.** The design's ninth item is the answer service reached
**from inside a sandbox**, at the factory gateway address the sandbox's own
profile allows. It is the check that proves that route, and without it a
project's deploy helper refuses every deploy that names a commit. The probe is
written — it takes the sandbox's name from `.env` and asks from in there — but
on a machine with no sandbox of that name it prints *not checked here*, with
the reason, and **counts as NOT PASSED**. The whole run then exits non-zero.
An item that has not been checked is not a pass, and this bundle's gate is not
met until it runs for real.

## The closed-door check — `estate-check --pre-resume`

*Added 26 September 2026, build item E2 of the rollout design.*

A rollout has a phase where the two things that can bring work in — the Slack
front door and the bus gateway — are **deliberately stopped**, and everything
that can be checked with the door shut is checked with the door shut. The
ordinary `services` check cannot serve that phase: there, a stopped front door
is the failure. Here a **running** one is, and it is named. The same fact, read
opposite ways.

**Run it from a shell that can reach the user's service manager.** The item that
proves the legacy front-door units stopped asks both the user manager and the
system manager, and a manager it cannot reach is *unknown*, which refuses — it
is never read as "not on this machine" (that reading passed the closed door
with both live units running, on 26 September 2026, when there was no session
bus). So from `sudo`, a cron job, a unit, or a non-login `ssh` the check will
refuse every time the env file names user units; run it from the operator's own
login shell, or export `XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS` first.
This is the check being honest, not the check being broken.

**What it checks, in order:**

1. **item 10 — the door really is shut.** No front-door container and no
   bus-gateway container in this estate; the legacy host units inactive, if they
   are on this machine at all (read-only, with `systemctl show`); and nothing
   waiting and nothing unconfirmed on the bus's two durable readers, read from
   the bus's own monitoring route. **If that route cannot be read, this fails**
   — "could not be read" is not "nought";
2. **item 10b — what is running is the release this rollout is FOR.** The image
   the rollout names — `--for-image`, or `FORGE_IMAGE` in the env file, which is
   the line the estate was started from — is resolved to an image **id** on this
   machine and compared with the image id the coordinator is really running. A
   difference names both sides and refuses, so no record is written at all. An
   identity that could not be read — a tag that is not on this machine, a
   container whose image cannot be read — refuses in the same way. *Added 26
   September 2026 after a review: the first version wrote the named release and
   the running image id into the same record and never compared them, so a
   record could name a release the estate was not running and be accepted;*
3. **every other service item**, including the retained bus by address and item
   9, the answer service reached from inside a sandbox;
4. **items 8h and 8i are reported as NOT CHECKED**, with the sentence saying
   they ask a producer and are checked after the door opens. Never as passed and
   never as failed.

**What it leaves behind, and why.** The rollout's resume step is the point of no
return, and it must not be taken on an exit code somebody saw an hour ago on a
different estate. So this writes `pre-resume.json` into `ROLLOUT_STATE_DIR` — the
same folder the rollout's own records live in — carrying the estate's compose
project name, the coordinator's image **id** and the release it was named by,
the bus mode, the retained bus's identity, the time, and **every item's verdict
in its own words**.

**Three verdicts, and only one may be acted on:**

| Verdict | What it means |
|---|---|
| `passed` | every item asked something and passed, and the only items not checked are the two that ask a producer |
| `passed-with-items-not-checked` | nothing failed, but an item could not be asked on this machine — item 9 with no sandbox is the one that happens. The record is written and says which, the run is NOT PASSED, and reading it back **refuses** it. An unknown stays an unknown all the way through |
| *(no record)* | something failed. **No record is written, and any record already there is INVALIDATED** — renamed to `pre-resume.json.invalidated` — because a check that has just failed must never leave an earlier pass sitting where the resume step reads |

**`estate-check --read-pre-resume`** is the only way that record may be used,
and it refuses by name for six reasons: there is no record or it cannot be
read; it is not a pass; it was written for a different estate; it names a
different coordinator image than the one running now; it was written about a
different **release** than this rollout is for, or the name it gives that
release now points at a different image, or the record disagrees with itself
about the two; or it is older than the coordinator's own start or older than
`ROLLOUT_PRE_RESUME_MAX_AGE_S`.

The release check is the one that was missing. Comparing the recorded image id
with the running image id alone is the same number twice whenever the record was
written here, so the record's own **name** for the release is resolved again
when it is read — which is also how a tag moved onto a different image since the
record was written is caught.

**The two durable readers are named in the env file**, not in the check:
`ROLLOUT_BUS_CONSUMERS`. They are `forge-serve` and `forge-serve-planning` — and
**`forge-serve`, not `forge-consumer`**, which is the name the rollout design
used. `forge-consumer` is the durable in `src/forge/adapters/nats/
pipeline_consumer.py`; what the coordinator actually runs is `forge serve`,
whose durable is `DEFAULT_DURABLE_NAME = "forge-serve"`
(`src/forge/cli/_serve_config.py:55`). A tool that read the waiting count for
`forge-consumer` would be reading a reader that is not there — which is why a
reader the bus does not hold is a **refusal** here and never a nought.

## What is deliberately not here yet

- **The model seats**, including the embedding service memory uses. They are
  addresses in `.env`; the GPU is where the GPU is.
- **The memory store's own database.** Memory is here; its Postgres is not, and
  where it should live is an open question of the design (above).
- **Nothing in the estate can *send* memory yet.** The relay consumes
  `memory.episode.>` on the bus, and the only account allowed to publish there
  is `guardkit`, the identity a *build* runs under. The coordinator is given
  neither `FLEET_MEMORY_NATS_URL` nor `GUARDKIT_NATS_PASSWORD`, and it only
  forwards names it has, so a build launched from this estate writes nothing —
  silently. The live machine is the same (`ops/forge-prod-recreate.sh` gives
  the coordinator the same five names). Proven on 25 September 2026: a message
  published as `guardkit` went the whole way into the store; as `fleet-memory`
  the broker refused it, by design. So on a fresh machine the relay sits
  correct and silent until a build is handed that credential — which is the
  sandbox's business (a secret the sandbox's containers get by name), and is
  named in the plan as a gap, not fixed here. Related: the project name the
  coordinator reads memory under comes from a code default (`guardkit`) because
  `FORGE_MEMORY_PROJECT` is in no env file; right today, and its own docstring
  says when it stops being.
- **Nothing in the estate calls the memory *service*.** The coordinator reads
  the store directly and no Forge code reads `FLEET_MEMORY_URL` at all, so the
  service's only caller is a Claude session over MCP, from outside. The service
  is not pointless — that session is a real consumer, and it is why the service
  is published at the gateway address — but `estate-check` item 8c is a check of
  the service and not of the estate using memory. Item 8c-forge is the one that
  asks the coordinator. If the two ever want to be one thing, that is Forge's
  own work: the reader in `src/forge/adapters/fleet_memory/priors.py` would have
  to go through MCP instead of through the store.
- **The memory service's compose description lives here rather than in the
  memory repository.** The design says each service's compose file belongs to
  the repository that owns the service, and the estate composes it in — which is
  what it does for Forge's own two. Memory's two services are described in this
  bundle's own `compose.yaml` instead, because the work that added them was
  allowed to change the memory repository only where its Dockerfiles could not
  otherwise be built. Moving them into `fleet-memory/deploy/compose/` and
  including that file is a small change of its own, and until it is made there
  are two descriptions of those containers: the memory repository's own
  host-network ones, which are what runs today, and these.
- **The front door** (the Slack side) and the bus gateway — jarvis's own
  compose file, a later rollout row.
- **The sandboxes themselves.** Making one is still an attended step. The
  sandbox service in `../compose/compose.sandbox-runner.yaml` holds an existing
  one awake and runs the project's own bootstrap inside it; it is behind the
  `sandbox` profile here so that a machine without one starts nothing. (Compose
  fills in every setting name **before** it looks at profiles, so
  `SANDBOX_NAME` still needs a value even where the service never starts.)
- **The project bootstrap refresh.** Forge's own bootstrap template now runs
  the factory inside a sandbox from the release image and from nothing else
  (`../../src/forge/cli/deploy_templates/sandbox-runner.sh`, 24 September 2026),
  and `hand-release-image-to-sandbox.sh` beside this file carries the image in
  and checks what arrived. What is still outstanding is the **rollout step**:
  each project's own copy of that bootstrap has to be replaced with the new
  template, and its `deploy/profile.yaml` has to publish the two service ports
  on the factory gateway address and allow the answer service's address. That
  is the project's own file and a gate of its own.

## Carrying the release image into a sandbox

    ./hand-release-image-to-sandbox.sh <sandbox name> <release version>

A sandbox is a small machine of its own with its own Docker engine, and the
factory's two services for a project run in there as containers from the tested
release image. This saves the image on this machine, loads it inside through
the sandbox client, reads it back from in there and **refuses** unless what
arrived is made of the same layers and carries the same release labels. It
prints the four settings the sandbox's bootstrap then needs.

When the estate has a registry (open question 1 of the design pass), this
script is replaced by a `docker pull` at the pinned digest inside the sandbox.
The check does not change: it is the same comparison, on the same fingerprint,
and the bootstrap inside the sandbox makes it again before it runs anything.

**Why a fingerprint of the layers and not "the image id".** The two engines do
not agree on what an image's id is — this machine's reports the image's config,
a sandbox's reports its manifest — so the same bytes carried across come back
under a different name. What both report identically is the list of layers the
filesystem is made of, so that list, hashed, is what is compared.

## The rollout preconditions this bundle does not meet

Recorded here so nothing reads as finished that is not, from the build plan and
the design:

1. **Item 9 has not run** — the route from inside a sandbox to the answer
   service (above).
2. **The per-route access policy is not enforced, and the check is weaker than
   the design asks.** The design requires each published port to name its
   listener, its destination port and its allowed source, and to refuse every
   other caller including the local network, with the negative probes made and
   repeated after a restart. Two things are outstanding, not one: **the rule**
   — this bundle publishes two ports and establishes no such policy, so the
   factory gateway address must be a **private** address of the machine until
   it does; and **the check** — `estate-check services` proves one forbidden
   direction, makes no probe from the local network, and repeats nothing after
   a restart.
3. **The images are named by tag, not by digest.** Section 3's item 4 and the
   rollout table both say the bundle supplies the tested image *by digest* and
   records the digest it replaces. `.env.example` names four tags, `estate-check`
   item 4 looks for those tags, and its own sentence says so. A tag can be
   moved; a digest cannot, and the whole point of the gate is that the tested
   image is what runs. (The bus's base image *is* pinned by digest in
   `estate-pins.conf`; it is the four release tags that are not.)
4. **The model seat has not been asked for real** — it is not in the bundle, so
   that part of the design's item 8 counts as not passed (above). The memory
   service and its relay joined the bundle on 25 September 2026 and are asked
   for real.
5. **api_test's bootstrap has not been refreshed** from Forge's template.
6. **Recovery after a sandbox-daemon restart is unproven** — the daemon is
   shared with live sandboxes, so it needs the owner present.
7. **The two-machine acceptance has not been run**: the same images on a clean
   local machine and on a cloud machine, both digest lists identical.
8. **The coordinator's bus password is still in its container environment.**
   Every other password in the estate now travels as a file; the coordinator's
   `FORGE_NATS_URL` carries one because Forge's code reads its bus address from
   that single setting. Changing that is Forge's own work, not this bundle's.
9. **The jarvis image this bundle names is one release behind what this bundle
   now needs.** The front door's state volume, added 26 September 2026, is
   mounted at a directory the image has to create and own, and
   `jarvis:2026.09.26-2` — which `.env.example` names and the manifest pins —
   predates that line. Started on it, the front door **refuses by name** rather
   than losing approvals quietly (drive of 26 September 2026: it says so and
   does not serve). That is the intended order: the next release carries the
   jarvis commit, and the env file's tag and the manifest's pin move with it.
   Until then this bundle does not start its front door.
10. **Codex's sign-off, and the owner's go.** Nothing here is rollout approval.

## Why the bus's image is not in the release manifest

`../../release/manifest.yaml` is where a release's images belong, and the bus's
is not there. Two things used to keep every release image inside one
repository: **one** build-context root, and **one** base image digest that
every Dockerfile of the release must start FROM.

The first of those is gone. On 25 September 2026 an image entry gained a
`context:`, naming which of the manifest's own repositories it is built from,
so the memory service and its relay are release images built from the memory
repository's clone at the memory repository's pin. The second still stands, and
it is what keeps the bus out: the bus starts FROM a NATS base, not the Python
one, and a release with two bases is two supply chains under one name. Giving
the manifest per-image bases is a change to what a release *means* — every
image's labels say which base the release pins — and it deserves a pass of its
own.

So the bus's pin still lives in `estate-pins.conf`, its images are tagged with
the **same release version** as the release images, and a test holds all six
image lines in `.env.example` to the manifest's version. The estate still moves
as one release and nothing here is unpinned — but the bus's pin is advanced by
hand, in a second file, and that is the cost of leaving it out.

## Bring it down

```
docker compose --env-file .env down          # keeps the volumes
docker compose --env-file .env down -v       # and removes them — the record with them
```

`down -v` removes the record. On a real machine that is never what you want.
It does **not** remove the volume holding the bus's own config, which belongs
to the release rather than to the run; `docker volume rm` removes that, the
same as removing an image.

## One more thing worth knowing

**Compose prefers the shell's own environment over `--env-file`.** If your
shell exports one of the names in `.env` — a `FORGE_NATS_URL` from an old
habit, say — that value is what gets used and `.env` is silently ignored for
it. That is also exactly how the bus's passwords reach the estate, which is why
it is worth understanding rather than working around. Bring the estate up from
a clean shell.

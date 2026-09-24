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
| `compose.yaml` | composes Forge's two compose files and adds the bus and the one-shot that provisions it |
| `.env.example` | every setting name the whole estate needs, with no machine's values. Copy to `.env` |
| `estate-pins.conf` | what the estate's own two images are built from. Part of the release, never edited per machine. Not named `.env`, because this repository ignores the whole `.env` family as a secrets fence and these are pins, not secrets |
| `build-estate-images.sh` | builds those two images, and fills the volume holding the bus's own config, from the bus repository at its pinned commit |
| `provisioner/Dockerfile` | the one-shot image: the NATS project's tool image plus `bash` |
| `estate-check` | the two checks, one sentence per item |
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
   provisioning scripts into a volume. The two release images
   (`forge` and `forge-publisher`) come from
   `../../scripts/build-release-image.sh` or from a registry.
4. **`cp .env.example .env`** and fill in the lines marked CHANGE THIS: the
   factory gateway address, the two sandbox ports, and the paths of the secret
   files. Put the secret files where it says.
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

   On a machine that looks after a project's sandbox, add `--profile sandbox`.
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

## The two checks

`estate-check host` — before anything starts. Seven items, from section 3 of
the design: the machine qualifies for a sandbox at all; Docker; the sandbox
tool and its daemon; the release images; the volumes and the disk; the secret
files (present or missing, never a value, and readable by nobody but their
owner); and every setting name the composed files require having a value.

`estate-check services` — after. The bus answers; its buckets and streams
exist, asked of the bus itself over its own monitoring route rather than taken
from the one-shot's exit code; the coordinator's own health route; the answer
service answering for a build nobody wrote down; and the publisher answering
the coordinator **and refusing the answer service**.

**Two of its items ask nothing here, and say so.** The memory service and the
model seat are in the design's item 8 and are not in this bundle yet, so with
no address in `.env` they print *not checked here* and count as **not passed**,
exactly as item 9 does. A run that reported them as passes would be saying it
had asked something it never asked. Give either an address and the check asks
it.

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

## What is deliberately not here yet

- **The memory service and the model seats.** Their compose files are in their
  own repositories and will be composed in here, the same way Forge's are.
  Until then `.env` can name their addresses and `estate-check services` will
  ask them.
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
4. **The memory service and the model seat have not been asked for real** —
   they are not in the bundle, so two of the design's item 8 parts count as not
   passed (above).
5. **api_test's bootstrap has not been refreshed** from Forge's template.
6. **Recovery after a sandbox-daemon restart is unproven** — the daemon is
   shared with live sandboxes, so it needs the owner present.
7. **The two-machine acceptance has not been run**: the same images on a clean
   local machine and on a cloud machine, both digest lists identical.
8. **The coordinator's bus password is still in its container environment.**
   Every other password in the estate now travels as a file; the coordinator's
   `FORGE_NATS_URL` carries one because Forge's code reads its bus address from
   that single setting. Changing that is Forge's own work, not this bundle's.
9. **Codex's sign-off, and the owner's go.** Nothing here is rollout approval.

## Why the bus's image is not in the release manifest

`../../release/manifest.yaml` is where a release's images belong, and the bus's
is not there yet. The manifest and its build script have **one** build-context
root — every image's Dockerfile is a path inside the clone of the repository
the release is cut from — and **one** base image digest that every Dockerfile
of the release must start FROM. The bus is a different repository and starts
FROM a NATS base rather than the Python one, so putting it in the manifest
means changing what a release *is*: per-image context roots and per-image
bases. That is a real change to the release script and to the meaning of the
release's labels, and it deserves a pass of its own rather than a corner of
this one.

Until then the pin lives in `estate-pins.conf`, the images are tagged with the
**same release version** as the release images, and a test holds all four image
lines in `.env.example` to the manifest's version. So the estate still moves as
one release and nothing here is unpinned — but the bus's pin is advanced by
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

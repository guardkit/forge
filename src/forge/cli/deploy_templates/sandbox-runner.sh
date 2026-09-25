#!/usr/bin/env bash
#
# The sandbox runner bootstrap — the one script that runs INSIDE a repository's
# Docker Sandbox to bring up the factory's two services for that repository:
# the deploy helper (the deploy sidecar) and the build runner. The host-side
# sandbox service (forge/deploy/compose/compose.sandbox-runner.yaml and its
# sandbox-runner/run.sh) holds it open with
#
#     sbx exec <sandbox> [env NAME=VALUE ...] deploy/sandbox-runner.sh
#
# and stops it again through the same door with the word `stop` on the end.
# Those two lines are the whole contract with the host side; nothing else out
# there knows anything about what is in here.
#
# THIS FILE IS SHARED. It is the same bytes in every repository and holds no
# value belonging to any one of them, and none belonging to any machine. Every
# setting is a NAME read from the environment the host side handed in.
#
# WHAT CHANGED, AND WHY (24 September 2026, stage 4d of the containerisation
# rollout gate). Until now this script copied the factory's own code out of
# read-only mounts of five checkouts on the machine outside, built a virtual
# environment inside the sandbox from them, installed five packages into it and
# ran the two services out of that. None of those five checkouts exists on a
# clean machine, so the factory could not be brought up anywhere else, and what
# ran inside the sandbox was whatever those checkouts happened to be at rather
# than a tested release. Rich, 23 September: *"Why are we still using systemd
# after I asked for containerisation to allow easy deployment both locally and
# to the cloud?"*
#
# So: **the factory inside a sandbox now runs from the release image and
# nothing else.** The sandbox has its own Docker engine. The release image is
# already in it — pulled at its pinned digest where the sandbox can pull, or
# saved on the machine that has it and loaded in (forge/deploy/estate/
# hand-release-image-to-sandbox.sh) — and this script checks it is the image it
# was told to expect before it runs anything from it. There is no source-clone
# fallback and no virtual environment: a clone at the pinned commit is not the
# tested image, and the whole point of the gate is that the tested image is what
# runs, everywhere. See section 5 of
# ai-transition/docs/factory-containerisation-design-pass-2026-09-23.md,
# "How the pinned copy of the factory reaches the sandbox".
#
# WHAT IT DOES, IN ORDER:
#   1. Reads its settings from the sandbox's own environment — names only; it
#      never reads anyone's shell, home directory or settings file.
#   2. TURNS THE TAG INTO AN IMAGE ONCE, AND NEVER USES THE TAG AGAIN. It asks
#      the sandbox's own engine which image the name FORGE_IMAGE stands for,
#      and from that moment on every question and every start is about THAT
#      IMAGE, by the id this engine holds it under — the identity document,
#      both release labels, both containers and every repair the supervisor
#      makes later. It asks what that image IS — the platform it was built for,
#      the filesystem it is made of and the runtime configuration it carries —
#      hashes that one document, and refuses, by name, unless the hash is the
#      one the machine that handed the image over recorded. Where the release
#      version and the manifest hash are named too, their labels on the image
#      must match as well. A tag is a name that can be moved onto another image
#      between one question and the next; an id cannot, and an id this engine
#      no longer holds is a refusal and never a second look at the name. A
#      missing or different image is a refusal with a plain sentence, never a
#      fetch of anything.
#   3. MAKES THE FOLDERS THE TWO CONTAINERS SHARE, in the sandbox's own
#      filesystem, and refuses by name if one cannot be made (see the table
#      below).
#   4. Starts the deploy helper and the build runner as TWO CONTAINERS from
#      that one image, each with its own start command, the project's own clone
#      bound read-write at the path it already lives at, the shared folders
#      bound into BOTH at the same path, and the factory's settings passed in
#      BY NAME. The two ports the host side expects are published inside the
#      sandbox, on every interface in here, so the sandbox's own publish rule
#      forwards them out as it already does.
#   5. Supervises them: one supervisor (a lock, so a second start refuses with
#      exit 4), and a container that dies is started again after a short pause.
#   6. `stop` ends the supervisor it can prove is this script's, then stops and
#      removes both containers, and exits 0 only when it has SEEN that both are
#      really gone. That is what the host side's stop requires: out there the
#      client is ended, which ends NOTHING in here, so the stop word is the
#      only thing that does.
#
# WHAT IT EXITS WITH. 0 after a clean stop, or after a warm-up that only
# checked the image. 2 when it refused at the door — no image named, an image
# it cannot vouch for, no Docker client, a shared folder it cannot make, an
# unknown word. 4 when a supervisor of this checkout is ALREADY running in
# this sandbox and this start was therefore refused: nothing was started, and
# the non-zero status is there so anything reading a status rather than the
# words sees a refusal and not a success. 1 when the containers would not go.
# 5 when the stop COULD NOT BE ESTABLISHED — this sandbox's engine would not
# answer, or the supervisor would not exit — which is not the same as a stop
# that failed and is very much not the same as a stop that worked; either way
# the work in here may still be running, the supervisor record is LEFT WHERE IT
# IS so the next stop can pick up from it, and the host side (which starts
# nothing on top of a stop that did not work) is told so plainly.
#
# THE FOLDERS THE TWO CONTAINERS SHARE, AND WHY EACH ONE IS OR IS NOT ONE
# (24 September 2026, stage 4e, from the stage 4d reviewer's first two
# findings). A container is thrown away and made again by the supervisor here
# whenever it dies, so ANYTHING the two containers must both see, or that must
# outlive one container, has to be a folder of the SANDBOX's own filesystem
# bound into both at the same path. Anything else is container-local and dies
# with the container, which for some things is exactly right. Every path Forge
# writes that a later stage of a build reads, and the decision for each:
#
#   the project's own clone, and everything a build writes under it — the
#   build's branch, a build's inner worktrees (.guardkit/worktrees/<task or
#   feature id>), the conductor's own per-build tree (.forge/worktrees/<build
#   id>, cut by src/forge/cli/_conductor_worktree.py and acted on afterwards
#   by the deploy helper), a fix journey's gate evidence (qa/gates/evidence)
#       SHARED MOUNT, read-write, at the path the clone already lives at. It
#       was the only one before this stage. BOTH worktree folders named above
#       are INSIDE the clone, so that one mount already carries both and the
#       answer for them was right before this line was written — the second
#       name is here because a list that names one and not the other reads as
#       though the other were somewhere else (the stage 4e reviewer asked for
#       it, 24 September 2026).
#
#   the per-build git worktrees, FORGE_AUTOBUILD_WORKTREE_BASE
#       SHARED MOUNT. The build runner materialises <base>/<build id> as a
#       worktree of the branch being built, and the deploy helper inspects and
#       retires that same path on the same build's later stages. Two
#       containers, one folder — and the supervisor's own repair (throw the
#       container away, make another from the image) would otherwise delete a
#       running build's working copy. Unset, the factory's own default is a
#       folder inside the container, so this script names one in the sandbox
#       rather than hand a container-local path to two containers.
#
#   the receipts root, FORGE_RECEIPTS_DIR (or SANDBOX_RECEIPTS_PATH, which is
#   what the project's profile calls the same folder)
#       SHARED MOUNT. Receipts are a build's durable record: written by the
#       runner while it works, read afterwards from outside both containers
#       through the sandbox's own path. They must outlive a container, and the
#       factory's own default puts them inside one.
#       AND WHERE NOTHING NAMES IT, READ THIS (stage 4e review, 24 September
#       2026). With no setting, this script uses a folder of its own state in
#       the sandbox. That survives a container being replaced, which is all
#       this stage was about — but it is a path NOTHING OUTSIDE the sandbox
#       knows, so a project whose receipts are read from outside (through the
#       folder its own deploy profile calls receipts_path) has to name that
#       folder here, in that project's own .env, as SANDBOX_RECEIPTS_PATH.
#       Nothing does it for you, and the start log says so when it falls back.
#
#   the deploy helper's executor notes, FORGE_DEPLOY_NOTES_DIR
#       SHARED MOUNT (stage 4f, 24 September 2026, the stage 4d reviewer's
#       fourth item). The deploy helper writes one note per deployment target
#       before it runs anything: the target, the build, the deployment counter
#       it was granted, the process group it started and when. A note is what
#       stops a helper that came back finding an empty slot while the deploy
#       command it forgot about is still running. The factory's own default
#       for them is a folder inside the container (see
#       src/forge/deploy_sidecar/service.py, DEPLOY_NOTES_DEFAULT), and a
#       container in here is thrown away and made again by the supervisor
#       below — so without this the notes go with it and the next helper
#       starts blind. The folder is made in the sandbox, handed to both
#       containers by name and bound into both at the same path; the helper is
#       its only writer, and the runner is given the same folder rather than
#       the NAME with nothing under it, which is the one outcome this table
#       exists to prevent.
#
#   the build runner's launch declaration (its graph config, written below)
#       THE SANDBOX'S OWN FILESYSTEM, bound read-only into the runner alone.
#       Written fresh at every start, so it can never drift from this file.
#
#   this script's lock and process record
#       NEITHER. They belong to the bootstrap, which is not in a container at
#       all, and nothing in a container reads them.
#
#   planning worktrees (forge-planning-worktrees under the temporary folder)
#       CONTAINER-LOCAL, and right to be. One planning operation makes one and
#       removes it again before it returns; no later stage reads it.
#
#   the coordinator's record (FORGE_DB_PATH, ~/.forge/forge.db)
#       NEITHER, and never: it stays with the coordinator, out on the machine,
#       and nothing in this sandbox opens it (rule 72).
#
#   the coordinator's evidence volume (/var/lib/forge-evidence)
#       NOT HERE AT ALL. That is a volume of the coordinator's own container
#       out on the machine, where it is what FORGE_RECEIPTS_DIR names. Inside a
#       sandbox the receipts root above is that folder.
#
# WHAT IT NEVER DOES. It never mounts a checkout of the factory's code, makes a
# virtual environment, installs a package or fetches source. It never opens the
# coordinator's record (rule 72: the record stays with the coordinator and the
# coordinator is its only writer) — FORGE_DB_PATH is never handed to either
# container, even if a mount or a setting carried it. It never prints a
# setting's value, only whether the setting is there. It names no language, no
# test runner, no package manager and no project's layout: the factory is
# agnostic, and a project's own toolchain is the project's business.
#
# SETTINGS, all read from the sandbox's environment. The host-side service
# forwards the NAMES the project's own .env lists (SANDBOX_ENV_NAMES); what is
# not set arrives unset and is reported as unset.
#
#   REQUIRED — the image, and proof it is the right one:
#     FORGE_IMAGE            the release image, by the tag it has IN HERE. The
#                            tag is used ONCE, to find the image; nothing is
#                            ever started from it (see below)
#     FORGE_IMAGE_IDENTITY   the sha256 of that image's IDENTITY DOCUMENT, as
#                            the machine that handed the image over recorded
#                            it. This script builds the same document from the
#                            image in this sandbox and compares the two
#
#   WHAT THE IDENTITY DOCUMENT IS, AND WHY THE LAYER LIST WAS NOT ENOUGH (24
#   September 2026, stage 4f, the stage 4d reviewer's third finding). An image
#   is three things, and the OCI image configuration specification keeps them
#   apart on purpose: THE PLATFORM it was built for, THE FILESYSTEM it is made
#   of (its layer list), and THE RUNTIME CONFIGURATION it carries — the
#   environment, the entry point, the command, the user, the working directory,
#   the labels, the ports, the volumes and the stop signal. Stage 4d compared
#   the layer list alone, so an image with the very same filesystem and the
#   very same release labels, but an environment value changed after the review,
#   was accepted as the reviewed image. It is not: what a container does is
#   mostly its configuration.
#
#   So what is compared is ALL THREE, as one document. Each side asks its own
#   engine for those fields, in one fixed order, and hashes the result:
#
#       docker image inspect --format "<the document below>" <the image>
#           | sha256sum
#
#   AND THE FIELDS ARE WRITTEN DOWN UNAMBIGUOUSLY (25 September 2026, stage 4g,
#   the stage 4f reviewer's second finding). The first version of the document
#   wrote each value out raw, one per line, so a value with a newline in it read
#   as two values and two values read as one value with a newline in it. The
#   reviewer built an image whose single environment variable contained a
#   newline, and an image with two ordinary variables, and both rendered the
#   same two lines and hashed the same. Version 2 writes the fields as one line
#   of JSON through the engine's own encoder: a newline inside a value comes out
#   as the two characters \ and r or \ and n, an array keeps its brackets, and
#   nothing in a value can any longer look like the end of it. The long note
#   beside IMAGE_IDENTITY_DOCUMENT_FORMAT below says what that relies on.
#
#   WHY THAT IS PORTABLE. The two engines do not agree on what an image's "id"
#   IS: this machine's engine keeps images the old way and reports the digest of
#   the image's CONFIG, a sandbox's engine keeps them the containerd way and
#   reports the digest of the image's MANIFEST. Ask each for "the id" and the
#   same bytes come back under two names — they did, first time, on 24
#   September. But the fields above are not the engine's opinion of the image;
#   they are the image's own OCI configuration and its rootfs, which travel
#   with it, and both engines report them identically because they are reading
#   the same object. Same image, same document, same hash, on either engine.
#
#   WHY IT IS IMMUTABLE. Every line of the document is content, not a pointer:
#   there is no tag, no repository, no id, no date and nothing of any machine in
#   it. Change a layer, an environment value, the entry point, the user, a label
#   or the platform and the hash changes; and nothing anyone can do to a
#   REGISTRY or to a tag can change what this hash covers, because a tag is not
#   in it. An image cannot be edited in place either: changing any of this
#   produces a new image, which is exactly what the new hash says.
#
#   WHAT IS DELIBERATELY LEFT OUT. The architecture VARIANT (the two image
#   stores do not fill it in the same way — one says "v8" where the other says
#   nothing — and a build for another platform has different layers anyway, so
#   the platform is pinned regardless); and everything the engine says ABOUT the
#   image rather than reads FROM it: its id, its repository tags and digests,
#   when it was created, its history and its size. A health check is not part of
#   the OCI configuration and both containers here are started with none.
#
#   THE SAME DOCUMENT IS BUILT ON THE OTHER SIDE, by the script that hands the
#   image in (forge/deploy/estate/hand-release-image-to-sandbox.sh), which
#   prints the hash to put in the machine's env file — and by the same script's
#   already-present path, which makes every one of these checks before it says
#   there is nothing to carry. Where the estate pulls from a registry instead,
#   nothing here changes.
#
#   CHECKED WHEN SET (and recommended):
#     FORGE_RELEASE_VERSION  must equal the image's com.guardkit.release.version
#     FORGE_RELEASE_MANIFEST_SHA256
#                            must equal com.guardkit.release.manifest.sha256
#
#   THE TWO SERVICES:
#     SANDBOX_RUNNER_BIND    the address both containers publish on INSIDE the
#                            sandbox (default 0.0.0.0 — the sandbox's own
#                            publish rule is what limits who can reach them)
#     SANDBOX_SIDECAR_PORT   the helper's port inside the sandbox (8125)
#     SANDBOX_RUNNER_PORT    the runner's port inside the sandbox (8124)
#     SANDBOX_RUNNER_RESTART_SECONDS
#                            the pause before a container that died is started
#                            again (default 5)
#     SANDBOX_RUNNER_STOP_PATIENCE_SECONDS
#                            how long a stop waits for the supervisor it
#                            signalled to go before it calls it stuck (default
#                            30). A supervisor removes both containers on its
#                            way out and each removal may take the ten seconds
#                            Docker gives a container, so twenty seconds of
#                            that is an ordinary shutdown, not a fault
#     SANDBOX_CONTAINER_PREFIX
#                            what the two containers are called in here
#                            (default forge-sandbox → -helper and -runner)
#     SANDBOX_CONTAINER_USER the user the two containers run as, as uid:gid
#                            (default: whoever runs this script, so the
#                            project's own files keep their owner)
#     SANDBOX_RECEIPTS_PATH  the receipts root inside the sandbox, which is
#                            what a project's profile calls this folder. It is
#                            MADE here if it is not there, bound read-write
#                            into BOTH containers at the same path, and handed
#                            to them as FORGE_RECEIPTS_DIR unless that is set
#                            already. A folder that cannot be made is a refusal
#                            naming the setting, never a name handed in with
#                            nothing bound under it
#     FORGE_RECEIPTS_DIR     the same folder said the factory's own way. Set,
#                            it wins over SANDBOX_RECEIPTS_PATH; unset, this
#                            script names a folder in the sandbox's own state
#                            rather than let receipts land inside a container
#                            and die with it — a folder that outlives a
#                            container but that nothing OUTSIDE this sandbox
#                            is looking at, so a project read from outside
#                            must name its own (see the receipts root above)
#     FORGE_AUTOBUILD_WORKTREE_BASE
#                            where a build's per-build worktrees are cut. Made
#                            here, bound read-write into BOTH containers at the
#                            same path, and refused by name if it cannot be
#                            made — the runner writes them and the helper reads
#                            and retires them, so one folder has to be both
#                            containers' folder. Unset, this script names one
#                            in the sandbox's own state, because the factory's
#                            own default is a folder inside the container and
#                            the supervisor throws containers away
#     FORGE_DEPLOY_NOTES_DIR
#                            where the deploy helper writes its executor notes
#                            — one per deployment target, saying what it is
#                            running and under which counter. Made here, bound
#                            read-write into both containers at the same path,
#                            handed to them by name, and refused by name if it
#                            cannot be made. Unset, this script names one in the
#                            sandbox's own state: the factory's own default is a
#                            folder inside the container, and a helper that is
#                            thrown away and made again would come back with no
#                            note of a deploy command that is still running
#     SANDBOX_DOCKER         the Docker client in here (default docker)
#     SANDBOX_DOCKER_SOCKET  THE SANDBOX'S OWN engine socket (default
#                            /var/run/docker.sock), bound into the helper so a
#                            project's own deploy can run in here, which is
#                            what the helper is for. The helper is also given
#                            the GROUP that owns that socket in this sandbox,
#                            because a bound socket a container's user cannot
#                            open is no socket at all. Nothing of the machine
#                            outside is ever bound in
#     SANDBOX_CONTAINER_ENV_NAMES
#                            extra setting names, space or comma separated, to
#                            hand to both containers on top of the factory's
#                            own list below. A project whose services need a
#                            setting of their own names it here, in its own
#                            .env, and nothing in this file changes
#     SANDBOX_RUNNER_BOOTSTRAP_ONLY
#                            set to 1 to check the image and stop without
#                            starting anything — a warm-up, and what the tests
#                            drive
#
#   The factory's own settings, handed to both containers BY NAME (never by
#   value): FORGE_TARGET_OWNER_URL — where the coordinator's read-only answer
#   is, which the helper asks what commit a build was recorded as starting from
#   and who holds a deployment target; unset, it cannot ask, and refuses a
#   request naming a commit rather than believing it. FORGE_NATS_URL — the bus.
#   FACTORY_GATEWAY_ADDRESS — the one machine address every route across the
#   sandbox boundary goes through. And the rest of the list below.
#
# SAFETY. In the build lane this script is proven against a stand-in `docker`
# program on PATH that records every call and answers as told; no sandbox,
# image, container or service of the estate is touched by any test.
set -euo pipefail

# --- anchor to the repository root ------------------------------------------
# Inside the sandbox the factory's clone of the repository sits at the same
# path as the checkout on the host, and this file sits in its deploy/ folder.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

log() { printf '[sandbox-runner.sh] %s\n' "$*"; }

# --- step 1: the settings ---------------------------------------------------
DOCKER="${SANDBOX_DOCKER:-docker}"
IMAGE="${FORGE_IMAGE:-}"
EXPECTED_IDENTITY="${FORGE_IMAGE_IDENTITY:-}"
EXPECTED_VERSION="${FORGE_RELEASE_VERSION:-}"
EXPECTED_MANIFEST="${FORGE_RELEASE_MANIFEST_SHA256:-}"
BIND="${SANDBOX_RUNNER_BIND:-0.0.0.0}"
SIDECAR_PORT="${SANDBOX_SIDECAR_PORT:-8125}"
RUNNER_PORT="${SANDBOX_RUNNER_PORT:-8124}"
RESTART_SECONDS="${SANDBOX_RUNNER_RESTART_SECONDS:-5}"
# HOW LONG A STOP WAITS FOR THE SUPERVISOR IT SIGNALLED, and why it is this
# long (found by running it in a sandbox, 24 September 2026). The supervisor's
# own ending is not instant: it removes both containers on its way out, and
# `docker stop` gives a container ten seconds to go before it insists. Two
# containers is therefore twenty seconds of perfectly ordinary shutdown, and a
# stop that gave up at ten would call a supervisor that was doing exactly as it
# was told one that would not go — and the host side, which starts nothing on
# top of a stop that failed, would leave a project's factory down over it. Keep
# this comfortably below the host side's own stop timeout
# (SANDBOX_STOP_TIMEOUT_SECONDS out there, 45 seconds by default).
STOP_PATIENCE_SECONDS="${SANDBOX_RUNNER_STOP_PATIENCE_SECONDS:-30}"
PREFIX="${SANDBOX_CONTAINER_PREFIX:-forge-sandbox}"
HELPER_NAME="${PREFIX}-helper"
RUNNER_NAME="${PREFIX}-runner"
DOCKER_SOCKET="${SANDBOX_DOCKER_SOCKET:-/var/run/docker.sock}"
EXTRA_ENV_NAMES="${SANDBOX_CONTAINER_ENV_NAMES:-}"

# The process record and the lock belong to this exact checkout, so a sandbox
# holding two of them cannot confuse one for the other.
SCRIPT_PATH="${SCRIPT_DIR}/$(basename "${BASH_SOURCE[0]}")"
STATE_ROOT="${HOME}/.forge-runner/$(printf '%s' "${REPO_ROOT}" | sha256sum | cut -d' ' -f1)"
PID_FILE="${STATE_ROOT}/supervisor"

# THE IDENTITY DOCUMENT: what this script asks the engine an image IS. The long
# version is at the top of this file. Short version: the platform, the
# filesystem's layer list and the whole of the runtime configuration — the
# image's own OCI configuration, which both kinds of engine report identically,
# and none of it a name, a pointer or a date. Hashed, it is what is compared
# with FORGE_IMAGE_IDENTITY.
#
# VERSION 2, AND WHY VERSION 1 HAD TO GO (stage 4g, 25 September 2026, the
# stage 4f reviewer's second finding). Version 1 wrote each value out raw, one
# per line, with nothing marking where a value ended and the next one began. So
# these two environments — which are not the same environment —
#
#     ["MODE=reviewed\nenv FEATURE=off"]      one variable, with a newline in it
#     ["MODE=reviewed", "FEATURE=off"]        two variables
#
# both came out as the two lines `env MODE=reviewed` and `env FEATURE=off`, and
# hashed the same. The reviewer built both images and watched the second one
# accepted as the first. That was an ambiguous way of writing the fields down,
# not a broken hash.
#
# So the whole of the document below the header line is ONE LINE OF JSON, built
# by the engine's own JSON encoder ({{json}}), which escapes every newline,
# carriage return and tab inside a value and keeps the brackets round an array.
# A value can no longer look like the end of itself. What this relies on, and
# nothing else: that encoder writes a map's keys in key order (so the labels,
# the ports and the volumes come out in a fixed order), and it writes control
# characters as escapes (so the rendered document holds no literal control
# character of its own except the one newline after the header line — which is
# what makes stripping the carriage returns a transport adds safe, and it is
# only ever the ones at the ends of lines that are stripped).
#
# The `{{if}}` round each list and map is there because the two engines differ
# on nothing-at-all: one answers `null` for an image with no entry point and
# the other `[]`, and those are the same image. Empty is written `[]` and `{}`
# either way. Nothing else is normalised: string contents cross exactly as the
# image holds them.
IMAGE_IDENTITY_DOCUMENT_FORMAT='forge-image-identity/2
{"architecture":{{json .Architecture}},"os":{{json .Os}},"layers":{{if .RootFS.Layers}}{{json .RootFS.Layers}}{{else}}[]{{end}},"env":{{if .Config.Env}}{{json .Config.Env}}{{else}}[]{{end}},"entrypoint":{{if .Config.Entrypoint}}{{json .Config.Entrypoint}}{{else}}[]{{end}},"cmd":{{if .Config.Cmd}}{{json .Config.Cmd}}{{else}}[]{{end}},"user":{{json .Config.User}},"workdir":{{json .Config.WorkingDir}},"labels":{{if .Config.Labels}}{{json .Config.Labels}}{{else}}{}{{end}},"ports":{{if .Config.ExposedPorts}}{{json .Config.ExposedPorts}}{{else}}{}{{end}},"volumes":{{if .Config.Volumes}}{{json .Config.Volumes}}{{else}}{}{{end}},"stopsignal":{{json .Config.StopSignal}}}'

#: The first line of a whole identity document, and the only part of it that is
#: fixed text. Both this script and the script that hands the image in refuse
#: an answer that does not begin with it: a hash of half a document compares
#: perfectly well against another hash of half a document.
IDENTITY_DOCUMENT_HEADER='forge-image-identity/2'

# CARRIAGE RETURNS ARE LINE ENDINGS HERE, NEVER PART OF A VALUE (stage 4g).
# An answer that came through a sandbox client can arrive with CRLF line
# endings. Version 1 deleted every carriage return in the answer, which also
# deleted real ones out of the middle of configuration values — the reviewer's
# point. In version 2 a real carriage return inside a value is written by the
# JSON encoder as the two characters \ and r, so the only carriage returns that
# can be in the rendered document are the ones transport put at the ends of
# lines. Those, and only those, are what this takes out.
only_the_line_endings() {
  local text="$1"
  text="${text//$'\r'$'\n'/$'\n'}"
  printf '%s' "${text%$'\r'}"
}

# THE BUILD RUNNER'S GRAPH DECLARATION, and why it is written here. The runner
# is `langgraph dev`, and that wants a config FILE naming the graph to serve.
# In the repository that file is forge.langgraph.json and it says `"." ` — the
# checkout it sits in. There is no checkout in the release image and there is
# not going to be one: the factory's code is INSTALLED in the image, so the
# graph is named by its module path instead. The file is written into this
# script's own state folder inside the sandbox and bound into the runner
# read-only. It holds no path and no value belonging to any machine, and no
# project's anything: it is the factory's own launch declaration, four lines
# long, and it is here so that the runner needs nothing of the outside world.
RUNNER_GRAPH_CONFIG='{
    "dependencies": ["forge"],
    "graphs": {
        "autobuild_runner": "forge.subagents.autobuild_runner:graph"
    }
}'
RUNNER_CONFIG_FILE="${STATE_ROOT}/langgraph.json"
RUNNER_CONFIG_IN_CONTAINER="/opt/forge/runner/langgraph.json"

# THE FACTORY'S OWN SETTING NAMES, handed to both containers by name. Nothing
# here belongs to a target project: a project that needs a setting of its own
# names it in SANDBOX_CONTAINER_ENV_NAMES, in its own .env.
FACTORY_ENV_NAMES=(
  FORGE_TARGET_OWNER_URL
  FORGE_NATS_URL
  FORGE_CONFIG_PATH
  FORGE_RECEIPTS_DIR
  # Both containers are told where the per-build worktrees are, because both
  # of them work on them: the runner cuts them, the helper inspects and
  # retires them. Before stage 4e this name reached the bootstrap and stopped
  # here, so the two containers each used the factory's own default — a folder
  # inside themselves, and a different one each.
  FORGE_AUTOBUILD_WORKTREE_BASE
  # The deploy helper's executor notes. Named here AND bound as a folder of
  # this sandbox below (stage 4f): a helper that is replaced must come back to
  # the notes it wrote, or it cannot tell an old deploy command that is still
  # running from an empty slot.
  FORGE_DEPLOY_NOTES_DIR
  FACTORY_GATEWAY_ADDRESS
  FORGE_GUARDKIT_PATH
  GUARDKIT_HARNESS
  FORGE_SIDECAR_IN_SANDBOX
  OPENAI_BASE_URL
  OPENAI_API_KEY
  # THE FACTORY'S MEMORY, both directions (Codex's stage 4b sign-off, 25
  # September 2026: this list forwarded none of these, so a build inside a
  # sandbox neither read project memory nor wrote an outcome, even when the
  # machine supplied every one). The build system defaults memory to OFF
  # without the first; it reads the store with the DSN; it writes outcomes over
  # the bus as the account whose password is the last name. Two of these are
  # secrets: they travel by NAME from the sandbox's own environment into the
  # containers and are never written to a file or a log by this script.
  FLEET_MEMORY_ENABLED
  FLEET_MEMORY_PG_DSN
  FLEET_MEMORY_EMBED_URL
  FLEET_MEMORY_EMBED_MODEL
  FLEET_MEMORY_EMBED_DIMS
  FLEET_MEMORY_NATS_URL
  GUARDKIT_NATS_PASSWORD
)

# --- the containers ---------------------------------------------------------
#
# ASKING THE ENGINE A QUESTION IT MIGHT NOT ANSWER (stage 4f, 24 September 2026,
# the stage 4d reviewer's first finding). Until this pass "is that container
# there?" turned the engine's answer into a yes or a no and threw away whether
# there had been an answer at all — so an engine that could not be reached said
# exactly what an empty engine says, and `stop` reported both containers gone
# while both were running. An unreadable engine is not an empty engine. So the
# three answers are kept apart everywhere below:
#
#   0  yes, it is there          (the engine answered, and named it)
#   1  no, it is not there       (the engine answered, and named nothing)
#   2  THE ENGINE WOULD NOT SAY  (the query itself failed)
#
# and WHY_THE_ENGINE_WOULD_NOT_SAY holds what it said, so the sentence a person
# reads names the real trouble instead of guessing at it.
WHY_THE_ENGINE_WOULD_NOT_SAY=""

ask_the_engine_about() {
  local name="$1" scope="$2" listed=""
  if [[ "${scope}" == "whatever-state" ]]; then
    if ! listed="$("${DOCKER}" ps -a --filter "name=^${name}$" --format '{{.ID}}' 2>&1)"; then
      WHY_THE_ENGINE_WOULD_NOT_SAY="${listed}"
      return 2
    fi
  else
    if ! listed="$("${DOCKER}" ps --filter "name=^${name}$" --format '{{.ID}}' 2>&1)"; then
      WHY_THE_ENGINE_WOULD_NOT_SAY="${listed}"
      return 2
    fi
  fi
  [[ -n "${listed}" ]]
}

# Is a container of this name there at all, whatever state it is in?
container_exists() {
  ask_the_engine_about "$1" whatever-state
}

# Is it running right now?
container_running() {
  ask_the_engine_about "$1" running-now
}

# Stop it and remove it. Never a failure on its own: a container that was never
# there is already in the state this asks for. An engine that would not say
# whether it is there is asked to remove it anyway — asking costs nothing, and
# the answer that matters is the confirmation afterwards, which is where a
# silent engine becomes a non-zero stop.
remove_container() {
  local name="$1" answer=0
  container_exists "${name}" || answer=$?
  if ((answer == 1)); then
    return 0
  fi
  "${DOCKER}" stop -t 10 "${name}" >/dev/null 2>&1 || true
  "${DOCKER}" rm -f "${name}" >/dev/null 2>&1 || true
}

remove_both_containers() {
  remove_container "${HELPER_NAME}"
  remove_container "${RUNNER_NAME}"
}

# Both gone, said as an exit status. This is what the host side's stop needs to
# be told the truth about, and it has the same three answers as the question it
# is built on: 0 both gone, 1 at least one still there, 2 the engine would not
# say and therefore NOBODY KNOWS.
both_containers_are_gone() {
  local name answer
  for name in "${HELPER_NAME}" "${RUNNER_NAME}"; do
    answer=0
    container_exists "${name}" || answer=$?
    if ((answer == 2)); then
      return 2
    fi
    if ((answer == 0)); then
      return 1
    fi
  done
  return 0
}

# --- whose process is that? --------------------------------------------------
# A PROCESS RECORD NAMES A NUMBER, AND NUMBERS COME ROUND AGAIN (stage 4f, 24
# September 2026, the stage 4d reviewer's second finding). A supervisor that
# died leaves its record behind; the sandbox goes on making processes; sooner or
# later something unrelated is given that number. Stage 4d signalled whatever
# the record named, and the reviewer's drive had it kill an innocent process and
# report a clean stop. The template before it did this properly and the rewrite
# dropped it; this is that check, restored, and it asks two things of the
# process before ANY signal goes anywhere near it:
#
#   IS IT THE SAME PROCESS?  Its birth time, from the system's own record of it,
#       must equal the birth time written down when the record was made. A
#       number that came round again belongs to a process born later, so this
#       tells the two apart — and nothing a later process can do to itself can
#       make it look born when the original was.
#   IS IT THIS BOOTSTRAP?    One of its arguments, resolved from its own working
#       directory, must be THIS script. A process of the right age that is not
#       running this file is not this checkout's supervisor either.
#
# Neither question reads anything of the process beyond those two public facts:
# no environment, no arguments' values, nothing of anybody's.
process_birth_time() {
  local statline
  [[ -r "/proc/$1/stat" ]] || return 1
  statline="$(cat "/proc/$1/stat")" || return 1
  # Drop the number and the name first: a process's name can hold spaces and
  # brackets, and the field after the closing bracket is a fixed list. Birth
  # time is field 22 of the whole line, which is field 20 of what is left.
  printf '%s\n' "${statline##*) }" | awk '{print $20}'
}

it_is_this_checkouts_supervisor() {
  local pid="$1" born="$2" argument
  [[ "${pid}" =~ ^[0-9]+$ && "${pid}" != "$$" ]] || return 1
  [[ -n "${born}" ]] || return 1
  [[ "$(process_birth_time "${pid}")" == "${born}" ]] || return 1
  while IFS= read -r -d '' argument; do
    [[ "${argument}" == */* ]] || continue
    if [[ "$(realpath -m "/proc/${pid}/cwd/${argument}" 2>/dev/null)" == "${SCRIPT_PATH}" ||
          "${argument}" == "${SCRIPT_PATH}" ]]; then
      return 0
    fi
  done < "/proc/${pid}/cmdline"
  return 1
}

# --- the stop word, handled before anything else ----------------------------
# The host side stops this the same way it started it, and it may be stopping
# a supervisor that is no longer there (a session that dropped, a sandbox that
# was asleep). So the stop never depends on the supervisor: it ends the
# supervisor if it can prove there is one, then ends the two containers itself,
# and reports on the CONTAINERS, which are the work.
#
# WHAT IT REFUSES TO CALL A STOP. A supervisor that was signalled and did not go
# (it would make the containers again the moment they were removed), and an
# engine that would not say whether the containers are gone. Both end 5, both
# leave the record where it is, and both say which of the two happened.
stop_everything() {
  local owner="" born="" waited=0 gone=0
  if [[ -r "${PID_FILE}" ]]; then
    read -r owner born < "${PID_FILE}" || true
    if it_is_this_checkouts_supervisor "${owner}" "${born}"; then
      log "asking the supervisor ${owner} to stop"
      kill -TERM "${owner}" 2>/dev/null || true
      while ((waited < STOP_PATIENCE_SECONDS * 10)) &&
            it_is_this_checkouts_supervisor "${owner}" "${born}"; do
        sleep 0.1
        waited=$((waited + 1))
      done
      if it_is_this_checkouts_supervisor "${owner}" "${born}"; then
        log "FATAL: the supervisor ${owner} of this checkout was asked to stop, and ${STOP_PATIENCE_SECONDS} seconds later it is still running. It makes the two containers again whenever they are gone, so removing them now would achieve nothing and reporting a stop would be untrue. The record is left where it is. Nothing may be started in this sandbox until this supervisor has gone."
        return 5
      fi
    else
      log "the supervisor record names ${owner:-nothing}, which is not a running supervisor of this checkout (a process that has gone, or its number given to something else since); nothing was signalled, and the two containers are stopped directly"
    fi
  else
    log "no supervisor record; stopping the two containers directly"
  fi

  # BEFORE removing anything: if the engine cannot be asked, nothing that
  # follows can be established either, and saying so here names the real
  # trouble rather than a container that "would not go".
  both_containers_are_gone || gone=$?
  if ((gone == 2)); then
    log "FATAL: this sandbox's own engine would not say what is running in it, so nothing here can be established: ${WHY_THE_ENGINE_WOULD_NOT_SAY}. The factory's two containers may still be running. The supervisor record is left where it is so the next stop picks up from it, and nothing may be started on top of this."
    return 5
  fi

  remove_both_containers

  gone=0
  both_containers_are_gone || gone=$?
  if ((gone == 2)); then
    log "FATAL: the factory's two containers were asked to go, and then this sandbox's own engine would not say whether they had: ${WHY_THE_ENGINE_WOULD_NOT_SAY}. A stop that cannot be seen to have worked is not a stop. The supervisor record is left where it is, and nothing may be started on top of this."
    return 5
  fi
  if ((gone == 0)); then
    rm -f "${PID_FILE}"
    log "stopped: neither ${HELPER_NAME} nor ${RUNNER_NAME} is in this sandbox's engine any more"
    return 0
  fi
  log "FATAL: the factory's containers would not go. Still in this sandbox's engine: $("${DOCKER}" ps -a --filter "name=^${HELPER_NAME}$" --filter "name=^${RUNNER_NAME}$" --format '{{.Names}} ({{.Status}})' 2>/dev/null | tr '\n' ' ')"
  return 1
}

case "${1:-start}" in
  stop)
    stop_everything
    exit $?
    ;;
  start) ;;
  *) log "usage: $0 [start|stop]"; exit 2 ;;
esac

# --- one supervisor, and only one -------------------------------------------
mkdir -p "${STATE_ROOT}"
exec 9>"${STATE_ROOT}/lock"
if ! flock -n 9; then
  # EXIT 4, NOT 0 (stage 4e, the stage 4d reviewer's third finding). Refusing
  # is the right thing to do here and nothing was started — but "refusing to
  # start" and a status of 0 read oddly together, and anything that checks the
  # status instead of the words would call this a success. The host-side
  # service stops before it starts, so on the ordinary path the lock is free
  # and this cannot happen; when it does, it is a refusal and says so both
  # ways. Nothing has been started at this point and the cleanup trap is not
  # set yet, so exiting here takes nothing down with it.
  log "refusing to start: a supervisor of this checkout is already running in this sandbox, and a second one would leave two sets of the factory's containers behind. Nothing was started."
  exit 4
fi
# THE RECORD: this process's number and the time the SYSTEM says it was born —
# not the wall clock, which any later process could be made to agree with, and
# not the number alone, which comes round again. A stop reads both back and
# proves the process it is about to signal is this one (see above). Stage 4f,
# 24 September 2026: stage 4d wrote the wall clock here and never read it.
printf '%s %s\n' "$$" "$(process_birth_time $$)" > "${PID_FILE}.tmp"
mv "${PID_FILE}.tmp" "${PID_FILE}"

STOPPING=0
cleanup() {
  remove_both_containers
  rm -f "${PID_FILE}"
}
trap cleanup EXIT
trap 'STOPPING=1; log "asked to stop; stopping both containers"; exit 0' TERM INT

# A sleep a signal can cut short.
nap() {
  sleep "$1" &
  wait $! 2>/dev/null || true
}

# --- step 2: the release image, checked before anything runs from it --------
refuse() {
  log "FATAL: $*"
  exit 2
}

# ONE QUESTION TO THE ENGINE ABOUT ONE IMAGE, with the three answers kept apart
# the way they are for the containers above: what it said, why it would not say
# it, and whether it answered at all. An engine that will not answer is NOT an
# image with an empty field, and telling the two apart is the difference between
# refusing and taking a hash of nothing. The answer and the complaint go to
# separate places on purpose, so a refusal can quote the engine's own words
# without any of them ever reaching the document that gets hashed.
WHAT_THE_ENGINE_SAID=""
ask_the_engine_about_the_image() {
  local reference="$1" format="$2" complaint="${STATE_ROOT}/what-the-engine-said"
  WHAT_THE_ENGINE_SAID=""
  WHY_THE_ENGINE_WOULD_NOT_SAY=""
  if ! WHAT_THE_ENGINE_SAID="$("${DOCKER}" image inspect --format "${format}" "${reference}" 2>"${complaint}")"; then
    WHY_THE_ENGINE_WOULD_NOT_SAY="$(tr '\n' ' ' < "${complaint}" 2>/dev/null || true)"
    rm -f "${complaint}"
    return 1
  fi
  rm -f "${complaint}"
  return 0
}

if [[ -z "${IMAGE}" ]]; then
  refuse "FORGE_IMAGE is not set. This sandbox runs the factory from the release image and from nothing else, and it has not been told which image that is. There is no source fallback on purpose: a clone at the pinned commit is not the tested image. Refusing to start."
fi
if [[ -z "${EXPECTED_IDENTITY}" ]]; then
  if [[ -n "${FORGE_IMAGE_CONTENT_ID:-}" ]]; then
    refuse "FORGE_IMAGE_IDENTITY is not set, and FORGE_IMAGE_CONTENT_ID is. That older setting was a hash of the image's LAYERS alone, and an image can keep every layer and still have had its environment, its entry point or its user changed after it was reviewed — which is a different image. This sandbox now checks the image's whole identity: its platform, its layers AND its runtime configuration. Hand the image in again with forge/deploy/estate/hand-release-image-to-sandbox.sh, which prints the value to put under FORGE_IMAGE_IDENTITY. Refusing to start."
  fi
  refuse "FORGE_IMAGE_IDENTITY is not set. The machine that handed the image over records what that image IS — its platform, its filesystem and its runtime configuration, hashed as one — and this sandbox refuses to run an image it cannot check against that. Refusing to start."
fi
if ! command -v "${DOCKER}" >/dev/null 2>&1; then
  refuse "there is no Docker client at '${DOCKER}' in this sandbox. The factory's two services run as containers in the sandbox's OWN engine; without a client there is nothing to run them with. Refusing to start."
fi

# THE NAME IS TURNED INTO AN IMAGE ONCE, AND THEN NEVER USED AGAIN (stage 4g,
# 25 September 2026, the stage 4f reviewer's first finding). Stage 4f started
# and repaired the containers from the id — an immutable reference — but asked
# the engine for the identity document, and for both release labels, BY THE TAG
# again, after it had read the id. Move the tag in the gap between those two
# questions and every check passes on the reviewed image while both containers,
# and every repair afterwards, run the unreviewed one: immutable references,
# the wrong ones. The reviewer did exactly that and watched it happen.
#
# So the tag is resolved here, once. Everything below — the identity document,
# both release labels, both starts and every repair — asks about THAT IMAGE, by
# the id this engine holds it under. Nothing asks about the name again, and if
# this engine stops holding that id (a containerd store lets an image go when
# the last name leaves it) this refuses and says so: there is no falling back
# to the name, because by then the name is a name for something else.
ENGINE_IMAGE_ID=""
if ask_the_engine_about_the_image "${IMAGE}" '{{.Id}}'; then
  ENGINE_IMAGE_ID="$(only_the_line_endings "${WHAT_THE_ENGINE_SAID}")"
fi
if [[ -z "${ENGINE_IMAGE_ID}" ]]; then
  refuse "the release image ${IMAGE} is not in this sandbox's own engine${WHY_THE_ENGINE_WOULD_NOT_SAY:+ — this engine said: ${WHY_THE_ENGINE_WOULD_NOT_SAY}}. Hand it in first (save it on the machine that has it and load it in here, or pull it at its pinned digest where this sandbox can pull) — forge/deploy/estate/hand-release-image-to-sandbox.sh does that and checks it. Nothing is fetched from here. Refusing to start."
fi
IMAGE_REFERENCE="${ENGINE_IMAGE_ID}"

# THE IDENTITY, BUILT HERE AND COMPARED. The document is the engine's own
# answer about THAT IMAGE, with the carriage returns a transport put at the
# ends of lines taken out and exactly one newline at the end, on both sides, so
# the two hashes are of the same bytes however the answer travelled. An empty
# answer is refused rather than hashed: the hash of nothing is a perfectly
# good-looking hash.
if ! ask_the_engine_about_the_image "${IMAGE_REFERENCE}" "${IMAGE_IDENTITY_DOCUMENT_FORMAT}"; then
  refuse "this sandbox's own engine will not say what the image it holds as ${IMAGE_REFERENCE} — which is what the name ${IMAGE} meant a moment ago — is made of and how it is configured${WHY_THE_ENGINE_WOULD_NOT_SAY:+: ${WHY_THE_ENGINE_WOULD_NOT_SAY}}. The name is not asked again on purpose: a name can have been moved onto another image since, and an image this engine has let go is not one to run the factory from. Refusing to start."
fi
IDENTITY_DOCUMENT="$(only_the_line_endings "${WHAT_THE_ENGINE_SAID}")"
if [[ -z "${IDENTITY_DOCUMENT}" ]]; then
  refuse "this sandbox's own engine would not say what the image it holds as ${IMAGE_REFERENCE} (the name ${IMAGE} meant it) is made of and how it is configured, so there is nothing to check against the identity the machine recorded. Refusing to start."
fi
# AND IT HAS TO BE THE WHOLE DOCUMENT. The first line of the document is a
# fixed word, so an answer that does not begin with it is not an identity
# document: an engine that rendered only part of what was asked for, or
# answered something else entirely, would otherwise have a hash taken of
# whatever it did say — and a hash of half the truth compares perfectly well
# against another hash of half the truth (found on 24 September 2026, hashing
# the stage 4d reviewer's own stand-in engine, which answers with the layer
# list alone).
if [[ "${IDENTITY_DOCUMENT%%$'\n'*}" != "${IDENTITY_DOCUMENT_HEADER}" ]]; then
  refuse "this sandbox's own engine did not answer with an identity document for the image it holds as ${IMAGE_REFERENCE}: what came back does not begin with the line an identity document begins with, so it is not the whole of what was asked for and nothing can be concluded by hashing it. Refusing to start."
fi
ACTUAL_IDENTITY="$(printf '%s\n' "${IDENTITY_DOCUMENT}" | sha256sum | cut -d' ' -f1)"
if [[ "${ACTUAL_IDENTITY}" != "${EXPECTED_IDENTITY}" ]]; then
  refuse "the image called ${IMAGE} in this sandbox is not the one the machine handed over. It expected an image whose platform, layers and runtime configuration hash to ${EXPECTED_IDENTITY}, and this engine holds one (as ${IMAGE_REFERENCE}) that hashes to ${ACTUAL_IDENTITY}. That covers the environment, the entry point, the command, the user, the working directory, the labels, the ports and the volumes as well as the filesystem, so the same layers under a changed configuration land here too — and rightly: it would not be the image that was tested. Hand the release image in again. Refusing to start."
fi

if [[ -n "${EXPECTED_VERSION}" ]]; then
  if ! ask_the_engine_about_the_image "${IMAGE_REFERENCE}" '{{index .Config.Labels "com.guardkit.release.version"}}'; then
    refuse "this sandbox's own engine will not say what release the image it holds as ${IMAGE_REFERENCE} — the image whose identity was just checked — says it is${WHY_THE_ENGINE_WOULD_NOT_SAY:+: ${WHY_THE_ENGINE_WOULD_NOT_SAY}}. The name ${IMAGE} is not asked again; a name can have been moved since the check. Refusing to start."
  fi
  ACTUAL_VERSION="$(only_the_line_endings "${WHAT_THE_ENGINE_SAID}")"
  if [[ "${ACTUAL_VERSION}" != "${EXPECTED_VERSION}" ]]; then
    refuse "the image ${IMAGE} says it is release '${ACTUAL_VERSION:-nothing at all}' and this sandbox was told to expect '${EXPECTED_VERSION}'. Refusing to start."
  fi
fi
if [[ -n "${EXPECTED_MANIFEST}" ]]; then
  if ! ask_the_engine_about_the_image "${IMAGE_REFERENCE}" '{{index .Config.Labels "com.guardkit.release.manifest.sha256"}}'; then
    refuse "this sandbox's own engine will not say what manifest the image it holds as ${IMAGE_REFERENCE} — the image whose identity was just checked — was built from${WHY_THE_ENGINE_WOULD_NOT_SAY:+: ${WHY_THE_ENGINE_WOULD_NOT_SAY}}. The name ${IMAGE} is not asked again; a name can have been moved since the check. Refusing to start."
  fi
  ACTUAL_MANIFEST="$(only_the_line_endings "${WHAT_THE_ENGINE_SAID}")"
  if [[ "${ACTUAL_MANIFEST}" != "${EXPECTED_MANIFEST}" ]]; then
    refuse "the image ${IMAGE} was built from a manifest with hash '${ACTUAL_MANIFEST:-none recorded}' and this sandbox was told to expect '${EXPECTED_MANIFEST}'. Refusing to start."
  fi
fi

# FROM HERE ON, THE TAG IS NOT USED (stage 4f, and from the first question
# about the image in stage 4g). Everything below starts and repairs containers
# from the id this engine holds the CHECKED image under. A tag is a name, and a
# name can be moved onto another image a moment after it was inspected — the
# stage 4d reviewer moved one and watched both containers start from the
# replacement. An id is the image itself: whatever happens to the tag
# afterwards, this is the image that was checked, for the first start and for
# every repair the supervisor makes later.
log "release image ${IMAGE} checked: identity ${ACTUAL_IDENTITY} (its platform, its layers and its runtime configuration), and this engine holds it as ${ENGINE_IMAGE_ID}${EXPECTED_VERSION:+, release ${EXPECTED_VERSION}}"
log "everything below is started from ${IMAGE_REFERENCE}, not from the tag ${IMAGE}: a tag can be moved onto another image after it has been checked"
log "repo_root=${REPO_ROOT} helper=${HELPER_NAME}:${SIDECAR_PORT} runner=${RUNNER_NAME}:${RUNNER_PORT}"

if [[ "${SANDBOX_RUNNER_BOOTSTRAP_ONLY:-}" == "1" ]]; then
  log "bootstrap only: the release image is the one expected; not starting the two containers"
  trap - EXIT
  rm -f "${PID_FILE}"
  exit 0
fi

# --- step 3: the folders the two containers share ---------------------------
# Each one is a folder of THIS SANDBOX's own filesystem, made here before
# anything starts and bound into BOTH containers at the same path. The table
# at the top of this file says which folders these are and why every other
# path Forge writes is not one.
#
# MADE HERE, AND BY THIS SCRIPT'S OWN USER, which is also the user the two
# containers run as unless SANDBOX_CONTAINER_USER says otherwise — so the
# folder the containers are given is a folder they can write. Docker would
# make a missing bind source itself, owned by root, which is the one outcome
# that looks fine and then fails on the first write.
#
# A FOLDER THAT CANNOT BE MADE IS A REFUSAL NAMING THE SETTING. The thing not
# to do is hand the name to both containers with nothing bound under it: the
# setting would then point at a path that exists separately inside each
# container, which is believable and wrong, and a build's work would be lost
# the first time the supervisor replaced a container.
share_a_folder() {
  local what="$1" path="$2"
  if ! mkdir -p "${path}" 2>/dev/null; then
    refuse "${what} names ${path}, and this sandbox cannot make that folder. It is bound into both of the factory's containers, so there is nowhere for a build's work to go and nothing is started. Name a folder this sandbox's own user can make, or leave the setting out and one under ${STATE_ROOT} is used. Refusing to start."
  fi
  if [[ ! -w "${path}" ]]; then
    refuse "${what} names ${path}, and this sandbox's own user cannot write it. Both of the factory's containers are given that folder to work in. Refusing to start."
  fi
  MOUNTS+=(--volume "${path}:${path}:rw")
}

# The receipts root. Whichever name the machine used for it wins, and the
# factory's own name is what crosses into the containers.
RECEIPTS_ROOT="${FORGE_RECEIPTS_DIR:-${SANDBOX_RECEIPTS_PATH:-}}"
RECEIPTS_SETTING="FORGE_RECEIPTS_DIR"
if [[ -z "${FORGE_RECEIPTS_DIR:-}" && -n "${SANDBOX_RECEIPTS_PATH:-}" ]]; then
  RECEIPTS_SETTING="SANDBOX_RECEIPTS_PATH"
fi
if [[ -z "${RECEIPTS_ROOT}" ]]; then
  RECEIPTS_ROOT="${STATE_ROOT}/receipts"
  RECEIPTS_SETTING="the receipts root (no setting named one, so this script did)"
  log "no receipts root was named, so this sandbox's own ${RECEIPTS_ROOT} is used: the factory's own default is a folder inside a container, and a container here is thrown away and made again"
  log "that folder outlives a container, but nothing outside this sandbox is looking at it: if this project's receipts are read from outside, set SANDBOX_RECEIPTS_PATH in the project's own .env to the folder its deploy profile calls receipts_path"
fi
export FORGE_RECEIPTS_DIR="${RECEIPTS_ROOT}"

# The per-build worktree base.
WORKTREE_BASE="${FORGE_AUTOBUILD_WORKTREE_BASE:-}"
WORKTREE_SETTING="FORGE_AUTOBUILD_WORKTREE_BASE"
if [[ -z "${WORKTREE_BASE}" ]]; then
  WORKTREE_BASE="${STATE_ROOT}/autobuild-worktrees"
  WORKTREE_SETTING="the per-build worktree base (no setting named one, so this script did)"
  log "no per-build worktree base was named, so this sandbox's own ${WORKTREE_BASE} is used: the factory's own default is a folder inside a container, invisible to the other one and destroyed when the supervisor replaces it"
fi
export FORGE_AUTOBUILD_WORKTREE_BASE="${WORKTREE_BASE}"

# The deploy helper's executor notes (stage 4f, the stage 4d reviewer's fourth
# item). The factory's own default for them is a folder inside the container,
# and the supervisor below replaces containers, so a helper that was replaced
# would come back with no note of the deploy command it had started and no way
# to tell that from nothing running at all.
NOTES_ROOT="${FORGE_DEPLOY_NOTES_DIR:-}"
NOTES_SETTING="FORGE_DEPLOY_NOTES_DIR"
if [[ -z "${NOTES_ROOT}" ]]; then
  NOTES_ROOT="${STATE_ROOT}/deploy-executor-notes"
  NOTES_SETTING="the deploy helper's notes folder (no setting named one, so this script did)"
  log "no folder was named for the deploy helper's executor notes, so this sandbox's own ${NOTES_ROOT} is used: the factory's own default is a folder inside the container, and a helper that is replaced would lose the note of a deploy command that is still running"
fi
export FORGE_DEPLOY_NOTES_DIR="${NOTES_ROOT}"

# THE MOUNTS, and there are four kinds and no more: the project's own clone,
# the three shared folders above, and the sandbox's own engine socket (the
# helper's alone, added at its start). Every one of them belongs to this
# sandbox. Nothing of the machine outside is bound into anything here — no
# checkout of the factory's code, no home folder, no settings file. That is
# the change stage 4d was.
MOUNTS=(--volume "${REPO_ROOT}:${REPO_ROOT}:rw")
share_a_folder "${RECEIPTS_SETTING}" "${RECEIPTS_ROOT}"
share_a_folder "${WORKTREE_SETTING}" "${WORKTREE_BASE}"
share_a_folder "${NOTES_SETTING}" "${NOTES_ROOT}"
log "folders shared by both containers: ${REPO_ROOT} (the project's clone), ${RECEIPTS_ROOT} (receipts), ${WORKTREE_BASE} (a build's worktrees), ${NOTES_ROOT} (the deploy helper's executor notes)"

# --- step 4: the settings the two containers are given, BY NAME -------------
# `--env NAME` hands the value this script's own environment holds under that
# name to the container without that value ever appearing in this file, in a
# log line or on a command line. A name with nothing set is left out, and said
# to be unset.
# This helper is the one INSIDE a repository's sandbox, and it says so: the
# deploy stage sends it the repository's own deploy script rather than the host
# wrapper, which calls the sandbox client and cannot run from in here.
export FORGE_SIDECAR_IN_SANDBOX=1

ENV_ARGUMENTS=()
FORWARDED=()
SKIPPED=()
for name in "${FACTORY_ENV_NAMES[@]}" ${EXTRA_ENV_NAMES//,/ }; do
  # The coordinator's record is never opened from in here (rule 72), whatever
  # a setting or a mount may have carried in.
  if [[ "${name}" == "FORGE_DB_PATH" ]]; then
    log "FORGE_DB_PATH was named as a setting to hand in; leaving it out — the record stays with the coordinator and nothing in this sandbox opens it (rule 72)"
    continue
  fi
  if [[ -n "${!name:-}" ]]; then
    ENV_ARGUMENTS+=(--env "${name}")
    FORWARDED+=("${name}")
  else
    SKIPPED+=("${name}")
  fi
done
log "settings handed to the two containers (names only): ${FORWARDED[*]:-none}"
log "settings named but not set here: ${SKIPPED[*]:-none}"

CONTAINER_USER="${SANDBOX_CONTAINER_USER:-$(id -u):$(id -g)}"
if [[ -n "${SANDBOX_CONTAINER_USER:-}" ]]; then
  log "the two containers run as ${CONTAINER_USER}, which this sandbox was told to use; the shared folders above were made by this script's own user, so that user and this one have to be able to write the same folders"
fi

# NO INHERITED HEALTH PROBE on either container. The release image carries one
# of its own and it is the COORDINATOR's: it curls a coordinator's /healthz on
# the coordinator's port. Neither of these two services is that, so the
# inherited probe marks both unhealthy for ever, from the first minute, while
# they are doing their job perfectly (seen on 24 September 2026, and the same
# thing the host-side sandbox service met in stage 3). Each service has a
# health route of its own — the helper's /healthz and the runner's /ok — and
# what asks them is the estate's own check, from outside the sandbox, which is
# where the question "is this project's factory answering" belongs.

# The runner's graph declaration, written fresh at every start so it can never
# drift from this file.
printf '%s\n' "${RUNNER_GRAPH_CONFIG}" > "${RUNNER_CONFIG_FILE}"

start_helper() {
  log "starting the deploy helper from ${IMAGE_REFERENCE} on ${BIND}:${SIDECAR_PORT}"
  local socket_mount=()
  if [[ -S "${DOCKER_SOCKET}" ]]; then
    # THE SANDBOX'S OWN engine, not the machine's. The helper's job is to run
    # a project's own vetted deploy and merge scripts, and a project's deploy
    # ordinarily brings containers up in here.
    socket_mount=(--volume "${DOCKER_SOCKET}:/var/run/docker.sock")
    # AND THE GROUP THAT OWNS IT. Binding the socket is not enough: it is
    # owner-and-group only, and the container runs as a plain user who is in
    # none of this sandbox's groups. Without this the socket is there, the
    # client is there, and every call answers "permission denied while trying
    # to connect to the docker API" — which is what happened the first time
    # this was run, 24 September 2026. The group added is whichever group owns
    # the socket in this sandbox, read from the socket itself; nothing else
    # about the container changes, and the runner container, which is given no
    # socket, is given no group either.
    local socket_group
    socket_group="$(stat -c '%g' "${DOCKER_SOCKET}" 2>/dev/null || true)"
    if [[ -n "${socket_group}" && "${socket_group}" =~ ^[0-9]+$ ]]; then
      socket_mount+=(--group-add "${socket_group}")
      log "the helper is given group ${socket_group}, the group that owns this sandbox's engine socket; without it the socket would be bound and unusable"
    else
      log "note: the group owning ${DOCKER_SOCKET} could not be read, so the helper is started without it; a project's deploy that runs containers will say permission denied if the container's user cannot reach the socket"
    fi
  else
    log "note: there is no engine socket at ${DOCKER_SOCKET} in this sandbox, so the helper is started without one; a project whose deploy runs containers will say so when it runs"
  fi
  "${DOCKER}" run --detach \
    --name "${HELPER_NAME}" \
    --user "${CONTAINER_USER}" \
    --no-healthcheck \
    --publish "${BIND}:${SIDECAR_PORT}:${SIDECAR_PORT}" \
    --env "FORGE_DEPLOY_SIDECAR_PORT=${SIDECAR_PORT}" \
    --env "FORGE_DEPLOY_SIDECAR_HOST=${BIND}" \
    ${ENV_ARGUMENTS[@]+"${ENV_ARGUMENTS[@]}"} \
    "${MOUNTS[@]}" \
    ${socket_mount[@]+"${socket_mount[@]}"} \
    --workdir "${REPO_ROOT}" \
    --entrypoint python \
    "${IMAGE_REFERENCE}" \
    -c 'import os; from forge.deploy_sidecar.service import serve; serve(host=os.environ["FORGE_DEPLOY_SIDECAR_HOST"], port=int(os.environ["FORGE_DEPLOY_SIDECAR_PORT"]))' \
    >/dev/null
}

start_runner() {
  log "starting the build runner from ${IMAGE_REFERENCE} on ${BIND}:${RUNNER_PORT}"
  "${DOCKER}" run --detach \
    --name "${RUNNER_NAME}" \
    --user "${CONTAINER_USER}" \
    --no-healthcheck \
    --publish "${BIND}:${RUNNER_PORT}:${RUNNER_PORT}" \
    ${ENV_ARGUMENTS[@]+"${ENV_ARGUMENTS[@]}"} \
    "${MOUNTS[@]}" \
    --volume "${RUNNER_CONFIG_FILE}:${RUNNER_CONFIG_IN_CONTAINER}:ro" \
    --workdir "${REPO_ROOT}" \
    --entrypoint langgraph \
    "${IMAGE_REFERENCE}" \
    dev \
    --config "${RUNNER_CONFIG_IN_CONTAINER}" \
    --host "${BIND}" \
    --port "${RUNNER_PORT}" \
    --no-browser \
    --no-reload \
    --allow-blocking \
    >/dev/null
}

# Start one, whatever state it is in: a container left behind by an earlier
# supervisor is removed first, so what runs is always freshly made from the
# checked image.
ensure_helper() {
  remove_container "${HELPER_NAME}"
  start_helper
}
ensure_runner() {
  remove_container "${RUNNER_NAME}"
  start_runner
}

ensure_helper
ensure_runner
log "both containers are up from ${IMAGE_REFERENCE}: ${HELPER_NAME} and ${RUNNER_NAME}"

# --- step 4: one supervisor, watching the two containers --------------------
# A container that dies is made again from the same checked image after a short
# pause. Docker's own restart policy is deliberately not used for this: the
# host side has to be able to end everything with one word, and a restart
# policy would bring a container back after that word had been given.
#
# AND AN ENGINE THAT WILL NOT ANSWER IS NOT A CONTAINER THAT DIED (stage 4f).
# When the question itself fails, this says so and waits for the next round
# rather than tearing down and remaking a container that is very probably
# running perfectly well behind an engine that is merely busy or restarting.
watch_one() {
  local what="$1" name="$2" answer=0
  container_running "${name}" || answer=$?
  if ((answer == 2)); then
    log "this sandbox's own engine would not say whether ${what} is running (${WHY_THE_ENGINE_WOULD_NOT_SAY}); nothing was changed, and it is asked again in ${RESTART_SECONDS}s"
    return 0
  fi
  if ((answer == 1)); then
    log "${what} is no longer running; starting it again from ${IMAGE_REFERENCE}, the image that was checked"
    return 1
  fi
  return 0
}

while ((STOPPING == 0)); do
  nap "${RESTART_SECONDS}"
  ((STOPPING == 1)) && break
  watch_one "the deploy helper" "${HELPER_NAME}" || ensure_helper
  ((STOPPING == 1)) && break
  watch_one "the build runner" "${RUNNER_NAME}" || ensure_runner
done

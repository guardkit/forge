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
#   2. CHECKS THE RELEASE IMAGE. It asks the sandbox's own engine for the image
#      the machine named, and refuses, by name, unless the image is there AND
#      its image id is the one the machine handed over. Where the release
#      version and the manifest hash are named too, their labels on the image
#      must match those as well. A missing or different image is a refusal with
#      a plain sentence, never a fetch of anything.
#   3. Starts the deploy helper and the build runner as TWO CONTAINERS from
#      that one image, each with its own start command, the project's own clone
#      bound read-write at the path it already lives at, and the factory's
#      settings passed in BY NAME. The two ports the host side expects are
#      published inside the sandbox, on every interface in here, so the
#      sandbox's own publish rule forwards them out as it already does.
#   4. Supervises them: one supervisor (a lock, so a second start refuses), and
#      a container that dies is started again after a short pause.
#   5. `stop` stops and removes both containers and exits 0 only when both are
#      really gone. That is what the host side's stop requires: out there the
#      client is ended, which ends NOTHING in here, so the stop word is the
#      only thing that does.
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
#     FORGE_IMAGE            the release image, by the tag it has IN HERE
#     FORGE_IMAGE_CONTENT_ID the fingerprint of the image's CONTENTS that the
#                            machine outside recorded for that tag: the sha256
#                            of the image's layer list, one layer digest per
#                            line. Two engines holding the same fingerprint
#                            hold the same filesystem, layer for layer
#
#   WHY A FINGERPRINT OF THE LAYERS AND NOT "THE IMAGE ID" (learned here, 24
#   September 2026, and the design pass's section 5 says "its digest checked"
#   without saying which). The two engines do not agree on what an image's id
#   IS. This machine's engine keeps images the old way and reports the id of
#   the image's CONFIG; a sandbox's engine keeps them the containerd way and
#   reports the digest of the image's MANIFEST. Carrying one image from one to
#   the other and asking each for "the id" gives two different answers for the
#   same bytes — it did, first time, on 24 September. What both engines DO
#   report identically is the list of layers that make up the filesystem, so
#   that list, hashed, is what is compared. A pull and a transfer are checked
#   the same way.
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
#     SANDBOX_CONTAINER_PREFIX
#                            what the two containers are called in here
#                            (default forge-sandbox → -helper and -runner)
#     SANDBOX_CONTAINER_USER the user the two containers run as, as uid:gid
#                            (default: whoever runs this script, so the
#                            project's own files keep their owner)
#     SANDBOX_RECEIPTS_PATH  the receipts root inside the sandbox; bound
#                            read-write into both containers at the same path
#                            and handed to them as FORGE_RECEIPTS_DIR unless
#                            that is set already
#     SANDBOX_DOCKER         the Docker client in here (default docker)
#     SANDBOX_DOCKER_SOCKET  THE SANDBOX'S OWN engine socket (default
#                            /var/run/docker.sock), bound into the helper so a
#                            project's own deploy can run in here, which is
#                            what the helper is for. Nothing of the machine
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
EXPECTED_CONTENT_ID="${FORGE_IMAGE_CONTENT_ID:-}"
EXPECTED_VERSION="${FORGE_RELEASE_VERSION:-}"
EXPECTED_MANIFEST="${FORGE_RELEASE_MANIFEST_SHA256:-}"
BIND="${SANDBOX_RUNNER_BIND:-0.0.0.0}"
SIDECAR_PORT="${SANDBOX_SIDECAR_PORT:-8125}"
RUNNER_PORT="${SANDBOX_RUNNER_PORT:-8124}"
RESTART_SECONDS="${SANDBOX_RUNNER_RESTART_SECONDS:-5}"
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
  FACTORY_GATEWAY_ADDRESS
  FORGE_GUARDKIT_PATH
  GUARDKIT_HARNESS
  FORGE_SIDECAR_IN_SANDBOX
  OPENAI_BASE_URL
  OPENAI_API_KEY
)

# --- the containers ---------------------------------------------------------

# Is a container of this name there at all, whatever state it is in?
container_exists() {
  [[ -n "$("${DOCKER}" ps -a --filter "name=^${1}$" --format '{{.ID}}' 2>/dev/null)" ]]
}

# Is it running right now?
container_running() {
  [[ -n "$("${DOCKER}" ps --filter "name=^${1}$" --format '{{.ID}}' 2>/dev/null)" ]]
}

# Stop it and remove it. Never a failure on its own: a container that was never
# there is already in the state this asks for.
remove_container() {
  local name="$1"
  if container_exists "${name}"; then
    "${DOCKER}" stop -t 10 "${name}" >/dev/null 2>&1 || true
    "${DOCKER}" rm -f "${name}" >/dev/null 2>&1 || true
  fi
}

remove_both_containers() {
  remove_container "${HELPER_NAME}"
  remove_container "${RUNNER_NAME}"
}

# Both gone, said as an exit status. This is what the host side's stop needs to
# be told the truth about.
both_containers_are_gone() {
  ! container_exists "${HELPER_NAME}" && ! container_exists "${RUNNER_NAME}"
}

# --- the stop word, handled before anything else ----------------------------
# The host side stops this the same way it started it, and it may be stopping
# a supervisor that is no longer there (a session that dropped, a sandbox that
# was asleep). So the stop never depends on the supervisor: it ends the
# supervisor if there is one, then ends the two containers itself, and reports
# on the CONTAINERS, which are the work.
stop_everything() {
  local owner="" token="" waited=0
  if [[ -r "${PID_FILE}" ]]; then
    read -r owner token < "${PID_FILE}" || true
    if [[ -n "${owner}" && "${owner}" =~ ^[0-9]+$ ]] && kill -0 "${owner}" 2>/dev/null; then
      log "asking the supervisor ${owner} to stop"
      kill -TERM "${owner}" 2>/dev/null || true
      while ((waited < 100)) && kill -0 "${owner}" 2>/dev/null; do
        sleep 0.1
        waited=$((waited + 1))
      done
    else
      log "no supervisor of this checkout is running; stopping the two containers directly"
    fi
  else
    log "no supervisor record; stopping the two containers directly"
  fi
  remove_both_containers
  if both_containers_are_gone; then
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
  log "refusing to start: a supervisor of this checkout is already running in this sandbox, and a second one would leave two sets of the factory's containers behind. Nothing was started."
  exit 0
fi
printf '%s %s\n' "$$" "$(date -u +%s)" > "${PID_FILE}.tmp"
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

if [[ -z "${IMAGE}" ]]; then
  refuse "FORGE_IMAGE is not set. This sandbox runs the factory from the release image and from nothing else, and it has not been told which image that is. There is no source fallback on purpose: a clone at the pinned commit is not the tested image. Refusing to start."
fi
if [[ -z "${EXPECTED_CONTENT_ID}" ]]; then
  refuse "FORGE_IMAGE_CONTENT_ID is not set. The machine that handed the image in records the fingerprint of the image's contents, and this sandbox refuses to run an image it cannot check against that fingerprint. Refusing to start."
fi
if ! command -v "${DOCKER}" >/dev/null 2>&1; then
  refuse "there is no Docker client at '${DOCKER}' in this sandbox. The factory's two services run as containers in the sandbox's OWN engine; without a client there is nothing to run them with. Refusing to start."
fi

image_field() {
  "${DOCKER}" image inspect --format "$1" "${IMAGE}" 2>/dev/null
}

ENGINE_IMAGE_ID="$(image_field '{{.Id}}' || true)"
if [[ -z "${ENGINE_IMAGE_ID}" ]]; then
  refuse "the release image ${IMAGE} is not in this sandbox's own engine. Hand it in first (save it on the machine that has it and load it in here, or pull it at its pinned digest where this sandbox can pull) — forge/deploy/estate/hand-release-image-to-sandbox.sh does that and checks it. Nothing is fetched from here. Refusing to start."
fi
ACTUAL_CONTENT_ID="$(image_field '{{range .RootFS.Layers}}{{.}}{{"\n"}}{{end}}' | sha256sum | cut -d' ' -f1)"
if [[ "${ACTUAL_CONTENT_ID}" != "${EXPECTED_CONTENT_ID}" ]]; then
  refuse "the image called ${IMAGE} in this sandbox is not the one the machine handed over: it expected an image whose layers fingerprint to ${EXPECTED_CONTENT_ID} and this engine holds one that fingerprints to ${ACTUAL_CONTENT_ID}. Two engines holding the same fingerprint hold the same filesystem, and these do not. Hand the release image in again. Refusing to start."
fi

if [[ -n "${EXPECTED_VERSION}" ]]; then
  ACTUAL_VERSION="$(image_field '{{index .Config.Labels "com.guardkit.release.version"}}' || true)"
  if [[ "${ACTUAL_VERSION}" != "${EXPECTED_VERSION}" ]]; then
    refuse "the image ${IMAGE} says it is release '${ACTUAL_VERSION:-nothing at all}' and this sandbox was told to expect '${EXPECTED_VERSION}'. Refusing to start."
  fi
fi
if [[ -n "${EXPECTED_MANIFEST}" ]]; then
  ACTUAL_MANIFEST="$(image_field '{{index .Config.Labels "com.guardkit.release.manifest.sha256"}}' || true)"
  if [[ "${ACTUAL_MANIFEST}" != "${EXPECTED_MANIFEST}" ]]; then
    refuse "the image ${IMAGE} was built from a manifest with hash '${ACTUAL_MANIFEST:-none recorded}' and this sandbox was told to expect '${EXPECTED_MANIFEST}'. Refusing to start."
  fi
fi

log "release image ${IMAGE} checked: contents ${ACTUAL_CONTENT_ID}, this engine calls it ${ENGINE_IMAGE_ID}${EXPECTED_VERSION:+, release ${EXPECTED_VERSION}}"
log "repo_root=${REPO_ROOT} helper=${HELPER_NAME}:${SIDECAR_PORT} runner=${RUNNER_NAME}:${RUNNER_PORT}"

if [[ "${SANDBOX_RUNNER_BOOTSTRAP_ONLY:-}" == "1" ]]; then
  log "bootstrap only: the release image is the one expected; not starting the two containers"
  trap - EXIT
  rm -f "${PID_FILE}"
  exit 0
fi

# --- step 3: the settings the two containers are given, BY NAME -------------
# `--env NAME` hands the value this script's own environment holds under that
# name to the container without that value ever appearing in this file, in a
# log line or on a command line. A name with nothing set is left out, and said
# to be unset.
if [[ -z "${FORGE_RECEIPTS_DIR:-}" && -n "${SANDBOX_RECEIPTS_PATH:-}" ]]; then
  export FORGE_RECEIPTS_DIR="${SANDBOX_RECEIPTS_PATH}"
fi
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

# THE MOUNTS, and there are three kinds and no more: the project's own clone,
# the project's receipts root, and the sandbox's own engine socket. Every one
# of them belongs to this sandbox. Nothing of the machine outside is bound into
# anything here — no checkout of the factory's code, no home folder, no
# settings file. That is the change this stage is.
MOUNTS=(--volume "${REPO_ROOT}:${REPO_ROOT}:rw")
if [[ -n "${SANDBOX_RECEIPTS_PATH:-}" && -d "${SANDBOX_RECEIPTS_PATH}" ]]; then
  MOUNTS+=(--volume "${SANDBOX_RECEIPTS_PATH}:${SANDBOX_RECEIPTS_PATH}:rw")
fi

CONTAINER_USER="${SANDBOX_CONTAINER_USER:-$(id -u):$(id -g)}"

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
  log "starting the deploy helper from ${IMAGE} on ${BIND}:${SIDECAR_PORT}"
  local socket_mount=()
  if [[ -S "${DOCKER_SOCKET}" ]]; then
    # THE SANDBOX'S OWN engine, not the machine's. The helper's job is to run
    # a project's own vetted deploy and merge scripts, and a project's deploy
    # ordinarily brings containers up in here.
    socket_mount=(--volume "${DOCKER_SOCKET}:/var/run/docker.sock")
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
    "${IMAGE}" \
    -c 'import os; from forge.deploy_sidecar.service import serve; serve(host=os.environ["FORGE_DEPLOY_SIDECAR_HOST"], port=int(os.environ["FORGE_DEPLOY_SIDECAR_PORT"]))' \
    >/dev/null
}

start_runner() {
  log "starting the build runner from ${IMAGE} on ${BIND}:${RUNNER_PORT}"
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
    "${IMAGE}" \
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
log "both containers are up from ${IMAGE}: ${HELPER_NAME} and ${RUNNER_NAME}"

# --- step 4: one supervisor, watching the two containers --------------------
# A container that dies is made again from the same checked image after a short
# pause. Docker's own restart policy is deliberately not used for this: the
# host side has to be able to end everything with one word, and a restart
# policy would bring a container back after that word had been given.
while ((STOPPING == 0)); do
  nap "${RESTART_SECONDS}"
  ((STOPPING == 1)) && break
  if ! container_running "${HELPER_NAME}"; then
    log "the deploy helper is no longer running; starting it again from ${IMAGE}"
    ensure_helper
  fi
  ((STOPPING == 1)) && break
  if ! container_running "${RUNNER_NAME}"; then
    log "the build runner is no longer running; starting it again from ${IMAGE}"
    ensure_runner
  fi
done

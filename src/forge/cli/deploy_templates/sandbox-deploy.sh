#!/usr/bin/env bash
#
# The sandbox deploy wrapper — the one script the factory's deploy step runs for
# the compose stage (deploy/profile.yaml -> compose.script).
#
# WHAT IT IS FOR (Rich's decision, 2026-09-06). Every merge now deploys the
# feature into a Docker Sandbox: a small virtual machine with its own kernel and
# its own Docker engine, made by Docker's `sbx` tool. The host's own Docker
# engine is no longer in the deployment path. This wrapper is the only thing
# that knows about the sandbox; deploy/deploy.sh is unchanged and simply runs
# inside it.
#
# THIS FILE IS SHARED. It is written to be the same file in every repository:
# it holds no value belonging to any one repository. The name of the sandbox,
# its size and its rules all arrive in the environment, and the checkout it
# works on is worked out from where this file itself sits. forge ships the same
# bytes as the template it writes into a newly registered repository.
#
# WHAT IT DOES, IN ORDER, AND NOTHING ELSE:
#   1. Make sure this repository's sandbox exists. If `sbx ls` does not list it,
#      create it, bind-mounting this checkout at its own host path so the path
#      is the same inside the sandbox and out, with the memory, processor count
#      and published ports the profile asked for. When the profile says the
#      sandbox also carries the factory (see below), it is created with the
#      factory's own clone of this repository, the factory's code mounted
#      read-only, the receipts root mounted read-write, the two service ports
#      published, and the sandbox's own environment file.
#   2. Make sure the sandbox is allowed to reach the addresses the build needs.
#      A sandbox refuses every outbound address it has not been told about, so
#      the Debian mirrors and the Python package index have to be named. We ask
#      the sandbox tool about each address in turn and add the rules only when
#      one of them is not allowed yet.
#   3. Start the keeper, a small user service that holds one session open inside
#      the sandbox so it does not put itself to sleep thirty seconds after the
#      last session ends. When the sandbox carries the factory, start the
#      runner unit too: it holds deploy/sandbox-runner.sh open inside the
#      sandbox, which brings up the factory's two services there.
#   4. Run deploy/deploy.sh inside the sandbox and exit with its exit code,
#      unchanged, so a failing deploy still fails the stage.
#
# HOW IT IS CONFIGURED. Everything arrives in the environment, threaded in by
# the deploy stage from the profile's `sandbox` block. This script never reads
# YAML.
#   SANDBOX_NAME           the sandbox's name              (required)
#   SANDBOX_MEMORY         memory size, as `sbx` accepts it, e.g. 6g
#   SANDBOX_CPUS           how many processors, e.g. 4
#   SANDBOX_PUBLISH        ports handed back to the host, comma-separated
#   SANDBOX_ALLOW_NETWORK  addresses the sandbox may reach, comma-separated
#
# THE SANDBOX THAT CARRIES THE FACTORY (Rich's rule, 2026-09-07; the spec's
# Part O, rule 68). Nothing the factory runs on a repository runs on the host
# any more: the build runner, which makes the worktrees and runs the
# repository's install and tests, and the deploy sidecar, which merges and
# deploys, both run inside this same sandbox, on the factory's own clone of
# the repository rather than on anyone's checkout. The settings below switch
# that shape on. Without them the sandbox is exactly what it was before, byte
# for byte, and nothing about the factory's services is touched.
#   SANDBOX_SIDECAR_PUBLISH  the deploy sidecar's port, HOST:PORT:8125 on the
#                            host's loopback, e.g. 127.0.0.1:8925:8125
#   SANDBOX_RUNNER_PUBLISH   the build runner's port, HOST:PORT:8124 likewise,
#                            e.g. 127.0.0.1:8924:8124
#                            (these two go together: both, or neither)
#   SANDBOX_FORGE_PATH       the forge checkout, mounted read-only
#                            (default: the folder "forge" beside this checkout)
#   SANDBOX_GUARDKIT_PATH    the guardkit checkout, likewise ("guardkit")
#   SANDBOX_RECEIPTS_PATH    the receipts root, mounted read-write — data the
#                            pipeline reads, never code it runs
#   SANDBOX_ENV_FILE         the sandbox's own environment: the router's
#                            address on the host's LAN and its key, the bus,
#                            the receipts root, the build settings. A file
#                            rendered by sops at deploy time, passed to `sbx`
#                            with --env-file. Never anyone's shell.
#   The factory's code is mounted from five checkouts, not two: forge's own
#   pyproject names nats-core and fleet-memory as the folders beside it, and
#   guardkitfactory is the harness the runner's builds import. They are
#   mounted read-only from beside the forge checkout and must all be there.
#   A sandbox created before these settings existed keeps its old shape until
#   it is removed and created again, attended: `sbx` cannot add a clone, a
#   mount or a port to a sandbox that already exists.
#
# BEFORE THIS CAN WORK the sandbox daemon must already be running for this user
# (`sbx daemon start -d --policy balanced`), the Docker sign-in must have been
# done once on this box, and forge-sandbox-keeper@.service (and, for a sandbox
# that carries the factory, forge-sandbox-runner@.service) must be installed in
# ~/.config/systemd/user/. See forge's ops/README.md and ops/systemd/README.md.
#
# SAFETY. This script is run by forge at the attended deploy step. In the build
# lane it is proven against fake `sbx` and `systemctl` programs placed first on
# PATH (deploy/tests/run_sandbox_deploy_tests.sh); no real sandbox is ever
# created, started, stopped or removed by a build agent.
set -euo pipefail

# --- anchor to the repository root ------------------------------------------
# deploy/deploy.sh anchors itself the same way. This directory is also the
# profile's `cwd`, which is the path the checkout is bind-mounted at inside the
# sandbox, so it is what we hand to `sbx` for both the mount and the working
# directory of the inner run.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

log() { printf '[sandbox-deploy.sh] %s\n' "$*"; }

# --- settings from the environment ------------------------------------------
SANDBOX_NAME="${SANDBOX_NAME:-}"
SANDBOX_MEMORY="${SANDBOX_MEMORY:-}"
SANDBOX_CPUS="${SANDBOX_CPUS:-}"
SANDBOX_PUBLISH="${SANDBOX_PUBLISH:-}"
SANDBOX_ALLOW_NETWORK="${SANDBOX_ALLOW_NETWORK:-}"

# The settings for a sandbox that carries the factory. The two paths default
# to the folders beside this checkout, which is where the estate keeps them;
# they are exported because `sbx create --env NAME` takes a value from this
# script's own environment, the same way `sbx exec -e NAME` does below, and
# that is how the bootstrap inside the sandbox learns where the mounts are.
SANDBOX_SIDECAR_PUBLISH="${SANDBOX_SIDECAR_PUBLISH:-}"
SANDBOX_RUNNER_PUBLISH="${SANDBOX_RUNNER_PUBLISH:-}"
SANDBOX_ENV_FILE="${SANDBOX_ENV_FILE:-}"
SANDBOX_RECEIPTS_PATH="${SANDBOX_RECEIPTS_PATH:-}"
ESTATE_ROOT="$(cd "${REPO_ROOT}/.." && pwd)"
SANDBOX_FORGE_PATH="${SANDBOX_FORGE_PATH:-${ESTATE_ROOT}/forge}"
SANDBOX_GUARDKIT_PATH="${SANDBOX_GUARDKIT_PATH:-${ESTATE_ROOT}/guardkit}"
export SANDBOX_FORGE_PATH SANDBOX_GUARDKIT_PATH SANDBOX_RECEIPTS_PATH

# The folders beside the forge checkout that forge's own code needs: the two
# its pyproject names as path sources, and the harness the runner's builds
# import. The bootstrap inside the sandbox installs from all of them.
ESTATE_SIBLINGS=(nats-core fleet-memory guardkitfactory)

if [[ -z "${SANDBOX_NAME}" ]]; then
  log "FATAL: SANDBOX_NAME is not set. This repository's deploy profile must carry a sandbox block naming the sandbox to deploy into, and the deploy stage must thread it in. Refusing to deploy."
  exit 2
fi

# True when the profile asked for the sandbox to carry the factory's services.
carries_the_factory() {
  [[ -n "${SANDBOX_SIDECAR_PUBLISH}" && -n "${SANDBOX_RUNNER_PUBLISH}" ]]
}

if [[ -n "${SANDBOX_SIDECAR_PUBLISH}" || -n "${SANDBOX_RUNNER_PUBLISH}" ]] && ! carries_the_factory; then
  log "FATAL: SANDBOX_SIDECAR_PUBLISH and SANDBOX_RUNNER_PUBLISH go together — a sandbox carries both of the factory's services or neither. Set both in the profile's sandbox block (sidecar_publish and runner_publish), or leave both out. Refusing to deploy."
  exit 2
fi

# The keeper is a user service, one instance per sandbox name; so is the
# runner unit, which holds the factory's services open inside the sandbox.
KEEPER_UNIT="forge-sandbox-keeper@${SANDBOX_NAME}"
RUNNER_UNIT="forge-sandbox-runner@${SANDBOX_NAME}"

# --- step 1: the sandbox exists ---------------------------------------------

# True when `sbx ls` names this sandbox. Matches a whole field so a sandbox
# called "widget-deploy" is not confused with "widget-deploy-2".
sandbox_exists() {
  local listing
  listing="$(sbx ls 2>/dev/null || true)"
  printf '%s\n' "${listing}" |
    awk -v name="${SANDBOX_NAME}" '{ for (i = 1; i <= NF; i++) if ($i == name) found = 1 } END { exit(found ? 0 : 1) }'
}

# The checkouts a sandbox that carries the factory mounts, read-only, in the
# order they are handed to `sbx`: forge, guardkit, then the folders beside
# forge. Filled into FACTORY_MOUNTS by check_factory_mounts, which refuses in
# one sentence when any of them is not there — `sbx` would fail on the mount
# anyway, and its message would not say why the folder matters.
FACTORY_MOUNTS=()

check_factory_mounts() {
  FACTORY_MOUNTS=("${SANDBOX_FORGE_PATH}" "${SANDBOX_GUARDKIT_PATH}")
  local sibling
  for sibling in "${ESTATE_SIBLINGS[@]}"; do
    FACTORY_MOUNTS+=("$(dirname "${SANDBOX_FORGE_PATH}")/${sibling}")
  done
  local path
  for path in "${FACTORY_MOUNTS[@]}"; do
    if [[ ! -d "${path}" ]]; then
      log "FATAL: the sandbox is to carry the factory, but there is no checkout at ${path}. The factory's code is mounted from forge, guardkit and the folders beside forge (${ESTATE_SIBLINGS[*]}); every one of them must be there. Refusing to create the sandbox."
      exit 2
    fi
  done
}

create_sandbox() {
  local argv=(sbx create shell "${REPO_ROOT}")
  if carries_the_factory; then
    if [[ -n "${SANDBOX_ENV_FILE}" && ! -f "${SANDBOX_ENV_FILE}" ]]; then
      log "FATAL: SANDBOX_ENV_FILE names ${SANDBOX_ENV_FILE}, and there is no such file. Render it first (it is the sops-rendered environment the sandbox is created with) or leave the setting out of the profile. Refusing to create the sandbox."
      exit 2
    fi
    check_factory_mounts
    local mount
    for mount in "${FACTORY_MOUNTS[@]}"; do
      argv+=("${mount}:ro")
    done
    if [[ -n "${SANDBOX_RECEIPTS_PATH}" ]]; then
      # Read-write, and the only mount that is: it is data the pipeline reads
      # and writes, never code it runs.
      if [[ ! -d "${SANDBOX_RECEIPTS_PATH}" ]]; then
        log "FATAL: SANDBOX_RECEIPTS_PATH names ${SANDBOX_RECEIPTS_PATH}, and there is no such folder. That is where the factory's receipts are written, mounted into the sandbox; make it first, or leave the setting out of the profile. Refusing to create the sandbox."
        exit 2
      fi
      argv+=("${SANDBOX_RECEIPTS_PATH}")
    fi
  fi
  argv+=(--name "${SANDBOX_NAME}")
  if carries_the_factory; then
    # The factory's own clone of this repository: the checkout is mounted
    # read-only and the sandbox works on its private clone, whose commits
    # reach the host as the git remote sandbox-<name>.
    argv+=(--clone)
  fi
  if [[ -n "${SANDBOX_MEMORY}" ]]; then
    argv+=(--memory "${SANDBOX_MEMORY}")
  fi
  if [[ -n "${SANDBOX_CPUS}" ]]; then
    argv+=(--cpus "${SANDBOX_CPUS}")
  fi
  # One --publish for each entry in the comma-separated list.
  if [[ -n "${SANDBOX_PUBLISH}" ]]; then
    local entry
    local -a publishes=()
    IFS=',' read -r -a publishes <<<"${SANDBOX_PUBLISH}"
    for entry in "${publishes[@]}"; do
      if [[ -n "${entry}" ]]; then
        argv+=(--publish "${entry}")
      fi
    done
  fi
  if carries_the_factory; then
    argv+=(-p "${SANDBOX_SIDECAR_PUBLISH}" -p "${SANDBOX_RUNNER_PUBLISH}")
    if [[ -n "${SANDBOX_ENV_FILE}" ]]; then
      argv+=(--env-file "${SANDBOX_ENV_FILE}")
    fi
    argv+=(--env SANDBOX_FORGE_PATH --env SANDBOX_GUARDKIT_PATH)
    if [[ -n "${SANDBOX_RECEIPTS_PATH}" ]]; then
      argv+=(--env SANDBOX_RECEIPTS_PATH)
    fi
  fi
  if carries_the_factory; then
    log "creating sandbox ${SANDBOX_NAME} on the factory's clone of ${REPO_ROOT}, carrying the deploy sidecar and the build runner"
  else
    log "creating sandbox ${SANDBOX_NAME} on ${REPO_ROOT}"
  fi
  "${argv[@]}"
}

# --- step 2: the outbound addresses, allowed once ---------------------------

# How an entry from the profile's list is asked about. The sandbox tool judges a
# bare host name as if it were being reached over HTTPS on port 443, but the
# Debian mirrors are fetched over plain HTTP, so a bare host is asked about as
# "http://<host>". An entry that already names a port, such as
# "172.30.1.253:4000", is asked about exactly as written, and so is an entry
# that already begins with a scheme.
check_target_for() {
  local entry="$1"
  if [[ "${entry}" == *"://"* || "${entry}" =~ :[0-9]+$ ]]; then
    printf '%s' "${entry}"
  else
    printf 'http://%s' "${entry}"
  fi
}

# True when every address in the list is already allowed for this sandbox.
#
# We ask the sandbox tool itself, one address at a time:
#   sbx policy check network --sandbox <name> <target>
# which is read-only — it changes nothing, it only answers. WE READ THE ANSWER
# FROM THE EXIT CODE: zero means the address is allowed, anything else means it
# is not. Anything else also covers a tool that cannot answer at all, and that
# is the safe way round: we then add the rules, which is harmless if they are
# already there, rather than skipping them and letting the build fail.
network_rules_present() {
  if [[ -z "${SANDBOX_ALLOW_NETWORK}" ]]; then
    return 0 # nothing was asked for
  fi
  local entry target
  local -a rules=()
  IFS=',' read -r -a rules <<<"${SANDBOX_ALLOW_NETWORK}"
  for entry in "${rules[@]}"; do
    if [[ -z "${entry}" ]]; then
      continue
    fi
    target="$(check_target_for "${entry}")"
    if ! sbx policy check network --sandbox "${SANDBOX_NAME}" "${target}" >/dev/null 2>&1; then
      log "the sandbox is not yet allowed to reach ${target}"
      return 1
    fi
  done
  return 0
}

allow_network() {
  log "allowing outbound addresses for ${SANDBOX_NAME}: ${SANDBOX_ALLOW_NETWORK}"
  sbx policy allow network --sandbox "${SANDBOX_NAME}" "${SANDBOX_ALLOW_NETWORK}"
}

# --- step 4: the deploy itself, inside the sandbox --------------------------

# A bare `-e NAME` tells sbx to take that variable's value from this script's own
# environment, so the mode signal the deploy stage sets (a normal deploy, the
# candidate leg, promote, revert, or the candidate teardown) reaches deploy.sh
# inside the sandbox unchanged. A name that is not set arrives empty, which is
# exactly what deploy.sh already expects when the signal is off.
run_deploy_inside() {
  local rc=0
  log "running deploy/deploy.sh inside ${SANDBOX_NAME} (working directory ${REPO_ROOT})"
  sbx exec -w "${REPO_ROOT}" \
    -e CANDIDATE \
    -e PROMOTE \
    -e REVERT \
    -e CANDIDATE_DOWN \
    -e CANDIDATE_PORT \
    -e ROLLBACK_IMAGE_REF \
    -e ENV_FILE \
    "${SANDBOX_NAME}" deploy/deploy.sh || rc=$?
  return "${rc}"
}

main() {
  log "repo_root=${REPO_ROOT} sandbox=${SANDBOX_NAME}"

  if sandbox_exists; then
    log "sandbox ${SANDBOX_NAME} already exists"
  else
    create_sandbox
  fi

  if network_rules_present; then
    log "outbound network rules already in place for ${SANDBOX_NAME}"
  else
    allow_network
  fi

  log "starting the keeper so the sandbox stays awake: ${KEEPER_UNIT}"
  systemctl --user start "${KEEPER_UNIT}"

  if carries_the_factory; then
    log "starting the factory's services inside the sandbox: ${RUNNER_UNIT}"
    systemctl --user start "${RUNNER_UNIT}"
  fi

  local rc=0
  run_deploy_inside || rc=$?
  log "deploy/deploy.sh inside ${SANDBOX_NAME} exited ${rc}"
  exit "${rc}"
}

main "$@"

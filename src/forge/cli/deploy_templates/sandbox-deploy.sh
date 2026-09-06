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
#      and published ports the profile asked for.
#   2. Make sure the sandbox is allowed to reach the addresses the build needs.
#      A sandbox refuses every outbound address it has not been told about, so
#      the Debian mirrors and the Python package index have to be named. We ask
#      the sandbox tool about each address in turn and add the rules only when
#      one of them is not allowed yet.
#   3. Start the keeper, a small user service that holds one session open inside
#      the sandbox so it does not put itself to sleep thirty seconds after the
#      last session ends.
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
# BEFORE THIS CAN WORK the sandbox daemon must already be running for this user
# (`sbx daemon start -d --policy balanced`), the Docker sign-in must have been
# done once on this box, and forge-sandbox-keeper@.service must be installed in
# ~/.config/systemd/user/. See forge's ops/README.md.
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

if [[ -z "${SANDBOX_NAME}" ]]; then
  log "FATAL: SANDBOX_NAME is not set. This repository's deploy profile must carry a sandbox block naming the sandbox to deploy into, and the deploy stage must thread it in. Refusing to deploy."
  exit 2
fi

# The keeper is a user service, one instance per sandbox name.
KEEPER_UNIT="forge-sandbox-keeper@${SANDBOX_NAME}"

# --- step 1: the sandbox exists ---------------------------------------------

# True when `sbx ls` names this sandbox. Matches a whole field so a sandbox
# called "widget-deploy" is not confused with "widget-deploy-2".
sandbox_exists() {
  local listing
  listing="$(sbx ls 2>/dev/null || true)"
  printf '%s\n' "${listing}" |
    awk -v name="${SANDBOX_NAME}" '{ for (i = 1; i <= NF; i++) if ($i == name) found = 1 } END { exit(found ? 0 : 1) }'
}

create_sandbox() {
  local argv=(sbx create shell "${REPO_ROOT}" --name "${SANDBOX_NAME}")
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
  log "creating sandbox ${SANDBOX_NAME} on ${REPO_ROOT}"
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

  local rc=0
  run_deploy_inside || rc=$?
  log "deploy/deploy.sh inside ${SANDBOX_NAME} exited ${rc}"
  exit "${rc}"
}

main "$@"

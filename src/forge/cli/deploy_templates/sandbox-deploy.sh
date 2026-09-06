#!/usr/bin/env bash
#
# @@NAME@@ sandbox wrapper — the only script the factory's deploy step runs.
#
# WHAT IT DOES, in three steps and nothing else:
#   a. makes sure this repository's Docker Sandbox exists, is allowed to reach
#      the hosts it needs, and is being kept awake;
#   b. runs this repository's own deploy/deploy.sh INSIDE that sandbox, with
#      the mode signal the factory set passed straight through;
#   c. exits with deploy.sh's exit code, unchanged.
#
# A Docker Sandbox is a small virtual machine with its own kernel and its own
# Docker engine, made by Docker's `sbx` tool. This repository owns one, named
# below, which bind-mounts this checkout at its host path — so the path is the
# same inside the sandbox and out — and publishes its ports back to the host's
# loopback, so the health checks and the live gate keep running from the host.
#
# WHERE THE SETTINGS COME FROM: the deploy step reads deploy/profile.yaml and
# puts the sandbox block into this script's environment. This script never
# reads YAML itself.
#
#   SANDBOX_NAME           the sandbox's name, e.g. @@SANDBOX@@
#   SANDBOX_MEMORY         how much memory it gets, e.g. 6g   (may be empty)
#   SANDBOX_CPUS           how many processors it gets        (may be empty)
#   SANDBOX_PUBLISH        ports to publish, comma separated  (may be empty)
#   SANDBOX_ALLOW_NETWORK  hosts it may reach, comma separated (may be empty)
#
# The mode signal (CANDIDATE / PROMOTE / REVERT / CANDIDATE_DOWN) and the
# addressing the factory sets (CANDIDATE_PORT, ROLLBACK_IMAGE_REF, ENV_FILE)
# are passed into the sandbox by NAME, so deploy.sh sees exactly what it would
# have seen had it run on the host.
#
# BEFORE THIS CAN WORK: the sandbox daemon must be running for this user
# (`sbx daemon start -d --policy balanced`), the Docker sign-in must have been
# done once on this box, and forge-sandbox-keeper@.service must be installed in
# ~/.config/systemd/user/. See forge's ops/README.md.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

SBX="${SBX:-sbx}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"

SANDBOX_NAME="${SANDBOX_NAME:-}"
SANDBOX_MEMORY="${SANDBOX_MEMORY:-}"
SANDBOX_CPUS="${SANDBOX_CPUS:-}"
SANDBOX_PUBLISH="${SANDBOX_PUBLISH:-}"
SANDBOX_ALLOW_NETWORK="${SANDBOX_ALLOW_NETWORK:-}"

log() { printf '[sandbox-deploy.sh] %s\n' "$*"; }

if [[ -z "${SANDBOX_NAME}" ]]; then
  log "FATAL: no SANDBOX_NAME in the environment — the deploy step passes it"
  log "from the sandbox block in deploy/profile.yaml; without it there is no"
  log "sandbox to deploy into and this script refuses to guess."
  exit 2
fi

# --- (a) the sandbox exists, is allowed out, and is kept awake ---------------

# `sbx ls` lists the sandboxes this user has. The name is matched as a whole
# word so a sandbox called "api-test-deploy-old" is never mistaken for this one.
sandbox_exists() {
  "${SBX}" ls 2>/dev/null | grep -qE "(^|[[:space:]])${SANDBOX_NAME}([[:space:]]|$)"
}

create_sandbox() {
  local argv=("${SBX}" create shell "${REPO_ROOT}" --name "${SANDBOX_NAME}")
  [[ -n "${SANDBOX_MEMORY}" ]] && argv+=(--memory "${SANDBOX_MEMORY}")
  [[ -n "${SANDBOX_CPUS}" ]] && argv+=(--cpus "${SANDBOX_CPUS}")
  if [[ -n "${SANDBOX_PUBLISH}" ]]; then
    local rule
    while IFS= read -r rule; do
      [[ -n "${rule}" ]] && argv+=(--publish "${rule}")
    done < <(printf '%s\n' "${SANDBOX_PUBLISH}" | tr ',' '\n')
  fi
  log "creating sandbox ${SANDBOX_NAME} on ${REPO_ROOT}"
  "${argv[@]}"
}

# The network rules are added in ONE call. `sbx policy show` is asked first so
# a sandbox that already carries the rules is left alone; if that question
# cannot be answered (an older sbx, a sandbox just created) the rules are added
# anyway — adding a rule that is already there changes nothing.
network_rules_present() {
  local shown rule
  shown="$("${SBX}" policy show --sandbox "${SANDBOX_NAME}" 2>/dev/null)" || return 1
  [[ -n "${shown}" ]] || return 1
  while IFS= read -r rule; do
    [[ -z "${rule}" ]] && continue
    printf '%s' "${shown}" | grep -qF -- "${rule}" || return 1
  done < <(printf '%s\n' "${SANDBOX_ALLOW_NETWORK}" | tr ',' '\n')
  return 0
}

if sandbox_exists; then
  log "sandbox ${SANDBOX_NAME} is already there"
else
  create_sandbox
fi

if [[ -n "${SANDBOX_ALLOW_NETWORK}" ]]; then
  if network_rules_present; then
    log "network rules already allowed for ${SANDBOX_NAME}"
  else
    log "allowing ${SANDBOX_NAME} to reach: ${SANDBOX_ALLOW_NETWORK}"
    "${SBX}" policy allow network --sandbox "${SANDBOX_NAME}" "${SANDBOX_ALLOW_NETWORK}"
  fi
fi

# A sandbox stops itself about thirty seconds after its last session ends. The
# keeper unit holds one session open so the deployed app keeps running. Starting
# a unit that is already running does nothing.
log "keeping ${SANDBOX_NAME} awake"
"${SYSTEMCTL}" --user start "forge-sandbox-keeper@${SANDBOX_NAME}"

# --- (b) run this repository's own deploy script inside the sandbox ----------
#
# Each bare `-e KEY` passes that variable's CURRENT value in, so the mode signal
# the factory set reaches deploy.sh unchanged. `-w` is this checkout's path,
# which is the same inside the sandbox as it is out here.
log "running deploy/deploy.sh inside ${SANDBOX_NAME}"
set +e
"${SBX}" exec -w "${REPO_ROOT}" \
  -e CANDIDATE \
  -e PROMOTE \
  -e REVERT \
  -e CANDIDATE_DOWN \
  -e CANDIDATE_PORT \
  -e ROLLBACK_IMAGE_REF \
  -e ENV_FILE \
  "${SANDBOX_NAME}" deploy/deploy.sh
inner_status=$?
set -e

# --- (c) the inner script's exit code, unchanged -----------------------------
log "deploy/deploy.sh finished with exit code ${inner_status}"
exit "${inner_status}"

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
#      NOT on a machine whose estate looks after the sandbox with its Compose
#      supervisor: a masked unit is skipped, and a container on this machine's
#      Docker engine labelled com.guardkit.sandbox-supervisor=<SANDBOX_NAME>
#      skips both. If either question cannot be answered, nothing is started
#      or deployed (exit 2). Asked before step 1.
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
#   FORGE_TARGET_OWNER_URL   where the coordinator's read-only answer is, with
#                            its route on the end. The deploy sidecar inside
#                            this sandbox asks it two things — what commit a
#                            build was recorded as starting from, and which
#                            build holds a deployment target — and sends it
#                            nothing but those names. Carried in when it is
#                            set; the address must be one that resolves from
#                            INSIDE the sandbox, and SANDBOX_ALLOW_NETWORK must
#                            name it. Unset means the sidecar in here cannot
#                            ask, and it then refuses a request that names a
#                            commit rather than believing it.
#   The factory's code is mounted from five checkouts, not two: forge's own
#   pyproject names nats-core and fleet-memory as the folders beside it, and
#   guardkitfactory is the harness the runner's builds import. They are
#   mounted read-only from beside the forge checkout and must all be there.
#   A sandbox created before these settings existed keeps its old shape until
#   it is removed and created again, attended: `sbx` cannot add a clone, a
#   mount or a port to a sandbox that already exists.
#
# A PROJECT'S OWN SANDBOX (17 October 2026, one sandbox per project; design
# docs/designs/one-sandbox-per-project-2026-10-17.md in the estate's records).
# Every project builds in a sandbox of its own, which reaches only its own
# repositories and its own memory. SANDBOX_SLOT switches that shape on; without
# it everything above is exactly as it was. A slot sandbox:
#   - is created on the factory's own clone of this checkout (--clone), with
#     NONE of the five factory source mounts: the factory runs from the release
#     image, not from source;
#   - is given its own block of ports from the slot number k:
#       build runner   PUBLISH_ADDRESS:(8924 + 10k) -> 8124
#       deploy helper  PUBLISH_ADDRESS:(8925 + 10k) -> 8125
#       git daemon     PUBLISH_ADDRESS:(8918 + 10k) -> 9418
#       two previews   127.0.0.1:(8950 + 10k) and 127.0.0.1:(8951 + 10k),
#                      the same number inside and out
#     Slot 0 is the shared sandbox's block (api-test-deploy), which is not
#     made by this shape; a slot sandbox is 1 or more;
#   - may reach only its own two doors and the three shared services on the
#     gateway: its Git door at (31830 + k), its memory door at (31850 + k), the
#     model door, the answer service and the embedder. Never the bus or the
#     model router. The project's own rules (package sources, github.com:443)
#     come from SANDBOX_ALLOW_NETWORK as before, and a rule there that names
#     the gateway address, or the bus's or the router's port, is refused: the
#     doors come from the slot and from nothing else;
#   - starts at 4 GiB and 2 processors unless SANDBOX_MEMORY / SANDBOX_CPUS say
#     otherwise (measured in stage S1 before the size is fixed);
#   - is created with disks big enough for real builds (S1 window, 18 October
#     2026: sbx's default Docker disk left 2.1 GB free, too little for a second
#     release image). SANDBOX_DOCKER_DISK sizes its /var/lib/docker disk
#     (default 40g, at least 512m) and SANDBOX_WORKSPACE_DISK its clone's disk,
#     which also holds the build worktrees (default 60g). Each is a whole
#     number and m or g. sbx sizes them only at creation, from its
#     DOCKER_SANDBOXES_DOCKER_SIZE and DOCKER_SANDBOXES_CLONED_WORKSPACE_SIZE
#     settings, which this script sets for that one command; the disk files
#     are sparse, so space is taken only as it is used. An existing sandbox's
#     disks are never resized here: grow them while it is stopped (the estate's
#     sandbox restart and disk growth runbook);
#   - has its own receipts folder (SANDBOX_RECEIPTS_PATH, required): a folder
#     kept for this project alone on the machine the coordinator runs on,
#     shared to this machine at the same path, and mounted into this one
#     sandbox at runtime with `sbx mount`, because a restart does not bring a
#     runtime mount back (the supervisor's run.sh mounts it again too);
#   - is looked after by its own Compose supervisor (`up.sh <name> <version>`
#     on the machine), so no host unit is ever started for it.
#   SANDBOX_SLOT             the slot number, 1 to 19 (from 20 the Git door
#                            ports would meet the memory door ports)
#   SANDBOX_PUBLISH_ADDRESS  the address the runner, helper and git daemon are
#                            published on (the machine's direct-link address)
#   SANDBOX_GATEWAY_ADDRESS  the address the doors are reached at
#   The three shared services are FIXED for a slot sandbox (Codex R2): the
#   model door 31823, the answer service 31821 and the embedder 9000.
#   SANDBOX_MODEL_DOOR_PORT, SANDBOX_ANSWER_PORT and SANDBOX_EMBEDDER_PORT
#   may be set only to those same numbers; any other value is refused.
#   SANDBOX_REPO_ROOT        the checkout the clone is made from, when this file
#                            is run from somewhere other than its deploy/
#                            folder (default: the folder above this file)
# Run it as `sandbox-deploy.sh prepare` to create the sandbox, set its rules
# and mount its receipts without running deploy/deploy.sh (a new project may
# have none yet). With no argument it deploys, as it always has.
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
log() { printf '[sandbox-deploy.sh] %s\n' "$*"; }
if [[ -n "${SANDBOX_REPO_ROOT:-}" ]]; then
  if [[ "${SANDBOX_REPO_ROOT}" != /* || ! -d "${SANDBOX_REPO_ROOT}" ]]; then
    log "FATAL: SANDBOX_REPO_ROOT names ${SANDBOX_REPO_ROOT}, which is not an absolute path to a folder. Name the project's checkout, or leave the setting out to use the folder above this file. Refusing to deploy."
    exit 2
  fi
  REPO_ROOT="$(cd "${SANDBOX_REPO_ROOT}" && pwd)"
else
  REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
fi
cd "${REPO_ROOT}"

# What to do: deploy (no argument, as always), or only prepare the sandbox.
ACTION="${1:-deploy}"
case "${ACTION}" in
  deploy|prepare) ;;
  *) log "FATAL: the only argument this script takes is 'prepare' (create the sandbox, set its rules and mount its receipts, and deploy nothing). It was given '${ACTION}'. Refusing."; exit 2 ;;
esac

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

# --- a project's own sandbox: its slot, its ports and its doors ------------
SANDBOX_SLOT="${SANDBOX_SLOT:-}"
SANDBOX_PUBLISH_ADDRESS="${SANDBOX_PUBLISH_ADDRESS:-}"
# A slot sandbox's two disks, made at creation (see above).
SANDBOX_DOCKER_DISK="${SANDBOX_DOCKER_DISK:-}"
SANDBOX_WORKSPACE_DISK="${SANDBOX_WORKSPACE_DISK:-}"
SLOT_DOCKER_DISK_DEFAULT="40g"
SLOT_WORKSPACE_DISK_DEFAULT="60g"
SANDBOX_GATEWAY_ADDRESS="${SANDBOX_GATEWAY_ADDRESS:-}"
# The shared services a slot sandbox reaches, fixed: no sandbox chooses its own
# (Codex R2, round 2).
SLOT_MODEL_DOOR_PORT=31823
SLOT_ANSWER_PORT=31821
SLOT_EMBEDDER_PORT=9000
# The two memory ports a slot sandbox may never name on the gateway: the full
# memory door for the machine's own tools, and the shared sandbox's door.
NEVER_ON_THE_GATEWAY_FROM_A_SLOT=(31822 31824)

# The slot scheme's bases. Slot k adds 10 times k to each. The same numbers are
# in forge's register-repo, which writes the coordinator's side of the slot.
SLOT_RUNNER_BASE=8924
SLOT_HELPER_BASE=8925
SLOT_GIT_DAEMON_BASE=8918
SLOT_PREVIEW_BASES=(8950 8951)
SLOT_GIT_DOOR_BASE=31830
SLOT_MEMORY_DOOR_BASE=31850
# The two gateway services a project's own sandbox may never reach.
NEVER_FROM_A_SLOT_PORTS=(4222 4000)
# The highest slot. From slot 20 the Git door (31830 + k) would meet the
# memory door of slot k - 20 (31850 + k).
SLOT_MAX=19
# The shared sandbox, which is never made, given doors or given a project's
# receipts by this shape (one sandbox per project, Codex R1).
SHARED_SANDBOX_NAMES=(api-test-deploy)

is_a_slot_sandbox() { [[ -n "${SANDBOX_SLOT}" ]]; }

# A dotted IPv4 address, not loopback, not 0.0.0.0. Used for the two addresses
# a slot sandbox is given; anything else is refused with a sentence.
plain_ipv4() {
  local value="$1" part
  [[ "${value}" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
  for part in "${BASH_REMATCH[@]:1}"; do
    ((10#${part} <= 255)) || return 1
  done
  [[ "${value}" != 0.* && "${value}" != 127.* && "${value}" != 255.255.255.255 ]]
}


SLOT_PUBLISHES=()
SLOT_DOOR_RULES=()
if is_a_slot_sandbox; then
  for shared in "${SHARED_SANDBOX_NAMES[@]}"; do
    if [[ "${SANDBOX_NAME}" == "${shared}" ]]; then
      log "FATAL: ${SANDBOX_NAME} is the shared sandbox, and SANDBOX_SLOT makes a project's own sandbox. Give the project a sandbox name of its own; the shared sandbox gets no door rules and no project's receipts from this script. Refusing to deploy."
      exit 2
    fi
  done
  # Slots stop at 19: from slot 20 the Git door (31830 + k) would meet the
  # memory door of slot k - 20 (31850 + k).
  if [[ ! "${SANDBOX_SLOT}" =~ ^[1-9][0-9]?$ ]] || ((10#${SANDBOX_SLOT} > SLOT_MAX)); then
    log "FATAL: SANDBOX_SLOT is '${SANDBOX_SLOT}', and a project's own sandbox takes a slot from 1 to 19 (slot 0 is the shared sandbox, which is not made this way; from 20 the door ports would collide). Refusing to deploy."
    exit 2
  fi
  if [[ -n "${SANDBOX_SIDECAR_PUBLISH}${SANDBOX_RUNNER_PUBLISH}" ]]; then
    log "FATAL: SANDBOX_SLOT gives this sandbox its ports, and SANDBOX_SIDECAR_PUBLISH or SANDBOX_RUNNER_PUBLISH names some as well. Leave both out for a slot sandbox. Refusing to deploy."
    exit 2
  fi
  if ! plain_ipv4 "${SANDBOX_PUBLISH_ADDRESS}"; then
    log "FATAL: a slot sandbox publishes its runner, helper and git daemon on SANDBOX_PUBLISH_ADDRESS, which must be this machine's direct-link IPv4 address (not loopback); it is '${SANDBOX_PUBLISH_ADDRESS}'. Refusing to deploy."
    exit 2
  fi
  if ! plain_ipv4 "${SANDBOX_GATEWAY_ADDRESS}"; then
    log "FATAL: a slot sandbox reaches its doors at SANDBOX_GATEWAY_ADDRESS, which must be an IPv4 address (not loopback); it is '${SANDBOX_GATEWAY_ADDRESS}'. Refusing to deploy."
    exit 2
  fi
  for pair in SANDBOX_MODEL_DOOR_PORT:SLOT_MODEL_DOOR_PORT SANDBOX_ANSWER_PORT:SLOT_ANSWER_PORT SANDBOX_EMBEDDER_PORT:SLOT_EMBEDDER_PORT; do
    setting="${pair%%:*}"
    fixed="${pair#*:}"
    if [[ -n "${!setting:-}" && "${!setting}" != "${!fixed}" ]]; then
      log "FATAL: ${setting} is '${!setting}', and for a project's own sandbox it is fixed at ${!fixed}. Leave it out. Refusing to deploy."
      exit 2
    fi
  done
  if [[ -z "${SANDBOX_RECEIPTS_PATH}" || "${SANDBOX_RECEIPTS_PATH}" != /* ]]; then
    log "FATAL: a slot sandbox has its own receipts folder, and SANDBOX_RECEIPTS_PATH must name it by its absolute path on this machine. Refusing to deploy."
    exit 2
  fi
  [[ -n "${SANDBOX_MEMORY}" ]] || SANDBOX_MEMORY="4g"
  [[ -n "${SANDBOX_CPUS}" ]] || SANDBOX_CPUS="2"
  [[ -n "${SANDBOX_DOCKER_DISK}" ]] || SANDBOX_DOCKER_DISK="${SLOT_DOCKER_DISK_DEFAULT}"
  [[ -n "${SANDBOX_WORKSPACE_DISK}" ]] || SANDBOX_WORKSPACE_DISK="${SLOT_WORKSPACE_DISK_DEFAULT}"
  for setting in SANDBOX_DOCKER_DISK SANDBOX_WORKSPACE_DISK; do
    if [[ ! "${!setting}" =~ ^[1-9][0-9]{0,5}[mMgG]$ ]]; then
      log "FATAL: ${setting} is '${!setting}', and a disk size is a whole number of mebibytes or gibibytes, such as 40g or 2048m. Refusing to deploy."
      exit 2
    fi
  done
  if [[ "${SANDBOX_DOCKER_DISK}" =~ ^([0-9]+)[mM]$ ]] && ((10#${BASH_REMATCH[1]} < 512)); then
    log "FATAL: SANDBOX_DOCKER_DISK is '${SANDBOX_DOCKER_DISK}', and sbx makes a sandbox's Docker disk no smaller than 512m. Refusing to deploy."
    exit 2
  fi
  slot_offset=$((10 * 10#${SANDBOX_SLOT}))
  SLOT_PUBLISHES=(
    "${SANDBOX_PUBLISH_ADDRESS}:$((SLOT_HELPER_BASE + slot_offset)):8125"
    "${SANDBOX_PUBLISH_ADDRESS}:$((SLOT_RUNNER_BASE + slot_offset)):8124"
    "${SANDBOX_PUBLISH_ADDRESS}:$((SLOT_GIT_DAEMON_BASE + slot_offset)):9418"
  )
  for preview_base in "${SLOT_PREVIEW_BASES[@]}"; do
    SLOT_PUBLISHES+=("127.0.0.1:$((preview_base + slot_offset)):$((preview_base + slot_offset))")
  done
  # Its own two doors, then the three shared services, all on the gateway.
  SLOT_DOOR_RULES=(
    "${SANDBOX_GATEWAY_ADDRESS}:$((SLOT_GIT_DOOR_BASE + 10#${SANDBOX_SLOT}))"
    "${SANDBOX_GATEWAY_ADDRESS}:$((SLOT_MEMORY_DOOR_BASE + 10#${SANDBOX_SLOT}))"
    "${SANDBOX_GATEWAY_ADDRESS}:${SLOT_MODEL_DOOR_PORT}"
    "${SANDBOX_GATEWAY_ADDRESS}:${SLOT_ANSWER_PORT}"
    "${SANDBOX_GATEWAY_ADDRESS}:${SLOT_EMBEDDER_PORT}"
  )
  # The project's own rules may not name the gateway, a wildcard for every
  # address, or the bus's or the router's port: the doors come from the slot.
  project_rules=()
  if [[ -n "${SANDBOX_ALLOW_NETWORK}" ]]; then
    IFS=',' read -r -a project_rules <<<"${SANDBOX_ALLOW_NETWORK}"
  fi
  kept_rules=()
  for rule in "${project_rules[@]}"; do
    [[ -n "${rule}" ]] || continue
    if [[ "${rule}" =~ [[:space:]] ]]; then
      log "FATAL: SANDBOX_ALLOW_NETWORK holds a rule with a space in it ('${rule}'). Write each rule as host, host:port or scheme://host, with no spaces. Refusing to deploy."
      exit 2
    fi
    rule_host="${rule#*://}"
    if [[ "${rule_host}" == */* ]]; then
      log "FATAL: SANDBOX_ALLOW_NETWORK holds '${rule}', which names an address range or a path. A project's own sandbox may reach only named hosts, one rule each. Refusing to deploy."
      exit 2
    fi
    rule_port=""
    if [[ "${rule_host}" =~ :([0-9]+)$ ]]; then
      rule_port="${BASH_REMATCH[1]}"
    fi
    rule_host="${rule_host%:*}"
    if [[ "${rule}" == "*" || "${rule_host}" == "*" ]]; then
      log "FATAL: SANDBOX_ALLOW_NETWORK holds a rule for every address ('${rule}'), and a project's own sandbox may reach only what is named. Refusing to deploy."
      exit 2
    fi
    if [[ "${rule_host}" == "${SANDBOX_GATEWAY_ADDRESS}" ]]; then
      log "FATAL: SANDBOX_ALLOW_NETWORK names the gateway (${rule}). A slot sandbox reaches the gateway only through its own doors, which come from its slot; take that rule out. Refusing to deploy."
      exit 2
    fi
    for never in "${NEVER_FROM_A_SLOT_PORTS[@]}"; do
      if [[ "${rule_port}" == "${never}" ]]; then
        log "FATAL: SANDBOX_ALLOW_NETWORK names port ${never} (${rule}), which is the bus's or the model router's. A project's own sandbox reaches neither; take that rule out. Refusing to deploy."
        exit 2
      fi
    done
    kept_rules+=("${rule}")
  done
  SANDBOX_ALLOW_NETWORK="$(IFS=','; printf '%s' "${kept_rules[*]:+${kept_rules[*]},}${SLOT_DOOR_RULES[*]}")"
  # THE WHOLE POLICY, CHECKED BEFORE ANYTHING IS CALLED (Codex R2): the
  # generated rules as well as the project's, so a shared-service port set to
  # the bus's, the router's or another project's door is refused too. No rule
  # may name the bus or the router, or a port in the two project-door ranges
  # other than this slot's own two, and the five gateway rules must be five
  # different ports.
  own_git_door=$((SLOT_GIT_DOOR_BASE + 10#${SANDBOX_SLOT}))
  own_memory_door=$((SLOT_MEMORY_DOOR_BASE + 10#${SANDBOX_SLOT}))
  IFS=',' read -r -a whole_policy <<<"${SANDBOX_ALLOW_NETWORK}"
  for rule in "${whole_policy[@]}"; do
    [[ "${rule}" =~ :([0-9]+)$ ]] || continue
    rule_port=$((10#${BASH_REMATCH[1]}))
    for never in "${NEVER_FROM_A_SLOT_PORTS[@]}"; do
      if ((rule_port == never)); then
        log "FATAL: the policy for ${SANDBOX_NAME} would name port ${never} (${rule}), which is the bus's or the model router's. Check SANDBOX_ALLOW_NETWORK. Refusing to deploy."
        exit 2
      fi
    done
    if [[ "${rule%:*}" == "${SANDBOX_GATEWAY_ADDRESS}" ]]; then
      for never in "${NEVER_ON_THE_GATEWAY_FROM_A_SLOT[@]}"; do
        if ((rule_port == never)); then
          log "FATAL: the policy for ${SANDBOX_NAME} would name ${rule}, a memory door that is not this project's (the machine's own tools' door, or the shared sandbox's). Refusing to deploy."
          exit 2
        fi
      done
    fi
    if (( (rule_port > SLOT_GIT_DOOR_BASE && rule_port <= SLOT_GIT_DOOR_BASE + SLOT_MAX && rule_port != own_git_door) ||
          (rule_port > SLOT_MEMORY_DOOR_BASE && rule_port <= SLOT_MEMORY_DOOR_BASE + SLOT_MAX && rule_port != own_memory_door) )); then
      log "FATAL: the policy for ${SANDBOX_NAME} would name port ${rule_port} (${rule}), which is another project's Git door or memory door. Slot ${SANDBOX_SLOT} reaches only ${own_git_door} and ${own_memory_door}. Refusing to deploy."
      exit 2
    fi
  done
  gateway_ports="$(printf '%s\n' "${SLOT_DOOR_RULES[@]##*:}" | sort -u | wc -l)"
  if ((gateway_ports != ${#SLOT_DOOR_RULES[@]})); then
    log "FATAL: two of the doors for ${SANDBOX_NAME} would share a port (${SLOT_DOOR_RULES[*]}). Give the model door, the answer service and the embedder ports of their own. Refusing to deploy."
    exit 2
  fi
fi

# True when the profile asked for the sandbox to carry the factory's services.
# A slot sandbox always does: its slot gives it both ports.
carries_the_factory() {
  is_a_slot_sandbox || [[ -n "${SANDBOX_SIDECAR_PUBLISH}" && -n "${SANDBOX_RUNNER_PUBLISH}" ]]
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

# A project's own sandbox: its own clone, no factory source mounts, its slot's
# ports, and its own settings. The receipts folder is mounted afterwards, at
# runtime (mount_the_receipts below), the same way the supervisor mounts it
# again after a restart.
create_slot_sandbox() {
  if ! git -C "${REPO_ROOT}" rev-parse --git-dir >/dev/null 2>&1; then
    log "FATAL: a slot sandbox is made on the factory's own clone of ${REPO_ROOT}, and that folder is not a git checkout. Clone the project there first, or point SANDBOX_REPO_ROOT at its checkout. Refusing to create the sandbox."
    exit 2
  fi
  if [[ -n "${SANDBOX_ENV_FILE}" && ! -f "${SANDBOX_ENV_FILE}" ]]; then
    log "FATAL: SANDBOX_ENV_FILE names ${SANDBOX_ENV_FILE}, and there is no such file. Render it first or leave the setting out. Refusing to create the sandbox."
    exit 2
  fi
  local argv=(sbx create shell "${REPO_ROOT}" --name "${SANDBOX_NAME}" --clone
    --memory "${SANDBOX_MEMORY}" --cpus "${SANDBOX_CPUS}")
  local entry
  if [[ -n "${SANDBOX_PUBLISH}" ]]; then
    local -a publishes=()
    IFS=',' read -r -a publishes <<<"${SANDBOX_PUBLISH}"
    for entry in "${publishes[@]}"; do
      [[ -n "${entry}" ]] && argv+=(--publish "${entry}")
    done
  fi
  for entry in "${SLOT_PUBLISHES[@]}"; do
    argv+=(-p "${entry}")
  done
  if [[ -n "${SANDBOX_ENV_FILE}" ]]; then
    argv+=(--env-file "${SANDBOX_ENV_FILE}")
  fi
  argv+=(--env SANDBOX_RECEIPTS_PATH)
  if [[ -n "${FORGE_TARGET_OWNER_URL:-}" ]]; then
    argv+=(--env FORGE_TARGET_OWNER_URL)
  fi
  log "creating sandbox ${SANDBOX_NAME} in slot ${SANDBOX_SLOT} on the factory's clone of ${REPO_ROOT}, with no factory source mounts, ${SANDBOX_MEMORY} and ${SANDBOX_CPUS} processors, a ${SANDBOX_DOCKER_DISK} Docker disk and a ${SANDBOX_WORKSPACE_DISK} workspace disk, ports ${SLOT_PUBLISHES[*]}"
  # The disk sizes are sbx's own settings, for this one command only.
  DOCKER_SANDBOXES_DOCKER_SIZE="${SANDBOX_DOCKER_DISK}" \
    DOCKER_SANDBOXES_CLONED_WORKSPACE_SIZE="${SANDBOX_WORKSPACE_DISK}" \
    "${argv[@]}"
}

# The receipts folder of a slot sandbox: mounted at runtime with `sbx mount`
# when it is not already a mount point inside, then checked.
mount_the_receipts() {
  if sbx exec "${SANDBOX_NAME}" mountpoint -q "${SANDBOX_RECEIPTS_PATH}" >/dev/null 2>&1; then
    log "the receipts folder ${SANDBOX_RECEIPTS_PATH} is already mounted inside ${SANDBOX_NAME}"
    return 0
  fi
  if [[ ! -d "${SANDBOX_RECEIPTS_PATH}" ]]; then
    log "FATAL: SANDBOX_RECEIPTS_PATH names ${SANDBOX_RECEIPTS_PATH}, and there is no such folder on this machine. Make this project's own receipts folder first. Nothing was deployed."
    exit 2
  fi
  log "mounting this project's own receipts folder ${SANDBOX_RECEIPTS_PATH} inside ${SANDBOX_NAME}"
  sbx mount "${SANDBOX_NAME}" "${SANDBOX_RECEIPTS_PATH}"
  if ! sbx exec "${SANDBOX_NAME}" mountpoint -q "${SANDBOX_RECEIPTS_PATH}" >/dev/null 2>&1; then
    log "FATAL: sbx mount answered, but ${SANDBOX_RECEIPTS_PATH} is still not a mount point inside ${SANDBOX_NAME}. Nothing was deployed."
    exit 2
  fi
}

create_sandbox() {
  if is_a_slot_sandbox; then
    create_slot_sandbox
    return
  fi
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
    # WHERE THE COORDINATOR'S READ-ONLY ANSWER IS, carried in when this script
    # was given it. The deploy sidecar in here asks it what commit a build was
    # recorded as starting from before it reads that project's declarations at
    # one, and who owns a deployment target when its own note is gone. Only the
    # name of a build or a target is ever sent to it. Left unset, the sidecar in
    # here cannot ask, and refuses a request naming a commit rather than
    # believing it — nothing is deployed and the sentence says why.
    if [[ -n "${FORGE_TARGET_OWNER_URL:-}" ]]; then
      argv+=(--env FORGE_TARGET_OWNER_URL)
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

# --- step 3, first: who looks after this sandbox? ---------------------------
# (1 October 2026.) On a machine whose
# estate runs the sandbox's supervisor as a Compose container, the two host
# units must never start: the runner unit's stop step would end the LIVE
# Compose supervisor (its process record names the same script), take both of
# the factory's containers in the sandbox down with it, and then restart every
# five seconds against it. Two questions, asked before anything is created,
# started or deployed:
#   (a) is a unit masked? A masked unit is skipped, with a sentence;
#   (b) does this machine's Docker engine hold a container, in any state,
#       labelled com.guardkit.sandbox-supervisor=<this sandbox>? Then BOTH
#       unit starts are skipped; the deploy itself still runs.
# A question that cannot be answered (systemctl or docker failing) is a
# refusal, exit 2, before any unit starts and before any deploy. With neither
# condition this script starts both units exactly as it always has.
SUPERVISOR_LABEL="com.guardkit.sandbox-supervisor"
SUPERVISOR_CONTAINER="absent"
KEEPER_STATE="unmasked"
RUNNER_STATE="unmasked"

# Sets UNIT_STATE to "masked" or "unmasked"; never prints into a capture, so
# its refusal is always seen.
UNIT_STATE=""
unit_file_state() {
  local unit="$1" state
  if ! state="$(systemctl --user show -p UnitFileState --value "${unit}" 2>/dev/null)"; then
    log "FATAL: systemctl would not say whether ${unit} is masked, so it cannot be told whether the estate's Compose supervisor looks after ${SANDBOX_NAME}. Starting the unit on top of that supervisor would take the factory's containers down. Nothing was started and nothing was deployed. Refusing."
    exit 2
  fi
  case "${state}" in
    masked|masked-runtime) UNIT_STATE="masked" ;;
    *) UNIT_STATE="unmasked" ;;
  esac
}

who_looks_after_the_sandbox() {
  local found
  if ! found="$(docker ps -a -q --filter "label=${SUPERVISOR_LABEL}=${SANDBOX_NAME}" 2>/dev/null)"; then
    log "FATAL: this machine's Docker engine would not say whether a container labelled ${SUPERVISOR_LABEL}=${SANDBOX_NAME} exists, so it cannot be told whether the estate's Compose supervisor looks after this sandbox. Starting the host units on top of that supervisor would take the factory's containers down. Nothing was started and nothing was deployed. Refusing."
    exit 2
  fi
  if [[ -n "${found}" ]]; then
    SUPERVISOR_CONTAINER="present"
  fi
  unit_file_state "${KEEPER_UNIT}"
  KEEPER_STATE="${UNIT_STATE}"
  if carries_the_factory; then
    unit_file_state "${RUNNER_UNIT}"
    RUNNER_STATE="${UNIT_STATE}"
  fi
}

main() {
  log "repo_root=${REPO_ROOT} sandbox=${SANDBOX_NAME}"

  # Asked before anything is created, started or deployed, so a refusal here
  # changes nothing. A slot sandbox is never looked after by a host unit, so
  # nothing is asked about one.
  if ! is_a_slot_sandbox; then
    who_looks_after_the_sandbox
  fi

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

  if is_a_slot_sandbox; then
    mount_the_receipts
    log "slot ${SANDBOX_SLOT}: ${SANDBOX_NAME} is looked after by its own Compose supervisor (up.sh ${SANDBOX_NAME} <version> on this machine), so no host unit is started"
  elif [[ "${SUPERVISOR_CONTAINER}" == present ]]; then
    log "a container labelled ${SUPERVISOR_LABEL}=${SANDBOX_NAME} is on this machine's Docker engine: the estate's Compose supervisor looks after this sandbox, so neither ${KEEPER_UNIT} nor ${RUNNER_UNIT} is started"
  else
    if [[ "${KEEPER_STATE}" == masked ]]; then
      log "${KEEPER_UNIT} is masked: the estate's Compose supervisor looks after this sandbox, so the keeper is not started"
    else
      log "starting the keeper so the sandbox stays awake: ${KEEPER_UNIT}"
      systemctl --user start "${KEEPER_UNIT}"
    fi

    if carries_the_factory; then
      if [[ "${RUNNER_STATE}" == masked ]]; then
        log "${RUNNER_UNIT} is masked: the estate's Compose supervisor looks after this sandbox, so the runner unit is not started"
      else
        log "starting the factory's services inside the sandbox: ${RUNNER_UNIT}"
        systemctl --user start "${RUNNER_UNIT}"
      fi
    fi
  fi

  if [[ "${ACTION}" == prepare ]]; then
    log "prepared ${SANDBOX_NAME}; nothing was deployed (prepare)"
    exit 0
  fi

  local rc=0
  run_deploy_inside || rc=$?
  log "deploy/deploy.sh inside ${SANDBOX_NAME} exited ${rc}"
  exit "${rc}"
}

main

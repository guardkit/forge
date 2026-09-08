#!/usr/bin/env bash
#
# The sandbox runner bootstrap — the one script that runs INSIDE a repository's
# Docker Sandbox to bring up the factory's two services for that repository:
# the deploy sidecar and the build runner. The host user unit
# forge-sandbox-runner@<sandbox> (forge's ops/systemd/) holds it open with
# `sbx exec <sandbox> deploy/sandbox-runner.sh`, exactly as the keeper unit
# holds `sleep infinity` open.
#
# WHY (Rich's rule, 2026-09-07; the spec's Part O, rule 69). Nothing the
# factory runs on a repository runs on the host any more. The runner that makes
# the build worktrees and runs the repository's install and tests, and the
# sidecar that merges and deploys, both live in here now, on the factory's own
# clone of the repository. Everything they install or run happens inside this
# sandbox's own kernel and disk, and nowhere else.
#
# THIS FILE IS SHARED. It is the same file in every repository and holds no
# value belonging to any one of them; forge ships the same bytes as the
# template it writes into a newly registered repository.
#
# WHAT IT DOES, IN ORDER:
#   1. Reads its settings from the sandbox's own environment. The sandbox was
#      created with an environment file rendered by sops, and the wrapper that
#      created it passed the two mount paths; this script never reads anyone's
#      shell or home directory.
#   2. Copies the factory's own code out of the read-only mounts into
#      ~/.forge-src/<name>, one folder per checkout: forge and guardkit, and
#      the three folders beside forge that forge's own code needs (nats-core
#      and fleet-memory, which forge's pyproject names as its neighbours, and
#      guardkitfactory, the harness the runner's builds import). Each copy is
#      `git archive` of the mount's HEAD — tracked files only, so nobody's
#      .env or .venv comes along — and is made again only when that HEAD has
#      moved. A copy is needed because forge builds with setuptools, which
#      writes into the tree it builds from, and a read-only mount refuses that.
#   3. Makes ~/.forge-venv with uv from the sandbox's own Python, once. uv's
#      "never download an interpreter" switch is set on that one command only,
#      never exported (see step 3 below for why).
#   4. Installs into it from the copies, in the order forge's own Dockerfile
#      uses: nats-core and fleet-memory first (neither can come from a package
#      index — fleet-memory is published nowhere and the nats-core wheel on the
#      index is malformed), then forge with its providers, memory and sidecar
#      extras, then guardkitfactory with the deepagents band the Dockerfile
#      pins, then guardkit — and proves the install with the same import line
#      the Dockerfile uses. Done again only when a copy changed. The guardkit
#      the runner shells is the one in this venv, never one on anyone's disk
#      (rule 61).
#   5. Starts the two services and keeps them running: the deploy sidecar on
#      port 8125 and the build runner (`langgraph dev`) on port 8124, both
#      bound on every interface INSIDE the sandbox so the ports the wrapper
#      published reach them. The sandbox publishes those two ports to the
#      host's loopback only, so nothing else can reach them. A service that
#      exits is started again after a short pause; a stop signal stops both.
#
# WHAT IT NEVER DOES. It never opens the forge ledger (rule 72: the ledger
# stays on the host, and forge-prod is its only writer) — FORGE_DB_PATH is
# unset before the services start, so the host's forge.db is out of reach even
# if a mount carried it. It never installs anything but the factory's own code
# and what forge's own pyproject asks for. It never hands the two services uv's
# "never download an interpreter" switch: that belongs to the one command that
# makes the factory's own venv, and a repository's build venv must be free to
# ask uv for the interpreter its requires-python floor names. It never prints a
# setting's value, only whether the setting is there.
#
# SETTINGS, all read from the sandbox's environment:
#   SANDBOX_FORGE_PATH       where the forge checkout is mounted
#                            (default: the folder "forge" beside this checkout)
#   SANDBOX_GUARDKIT_PATH    where the guardkit checkout is mounted ("guardkit")
#   SANDBOX_RECEIPTS_PATH    the receipts root; becomes FORGE_RECEIPTS_DIR
#                            unless that is already set
#   SANDBOX_RUNNER_BIND      the address both services bind (default 0.0.0.0)
#   SANDBOX_SIDECAR_PORT     the sidecar's port inside the sandbox (8125)
#   SANDBOX_RUNNER_PORT      the runner's port inside the sandbox (8124)
#   SANDBOX_RUNNER_RESTART_SECONDS
#                            the pause before a service that exited is started
#                            again (default 5)
#   SANDBOX_RUNNER_BOOTSTRAP_ONLY
#                            set to 1 to stop after step 4 without starting the
#                            services — a warm-up, and what the tests drive
#   Everything the services themselves read — OPENAI_BASE_URL and its key,
#   FORGE_CONFIG_PATH, GUARDKIT_HARNESS, the bus address, the memory keys —
#   arrives in the same environment from the sandbox's environment file.
#
# SAFETY. In the build lane this script is proven against fake `uv`, `python`
# and `langgraph` programs in a temporary home, with real git in temporary
# repositories standing in for the mounts; no sandbox, venv or service of the
# estate is touched by any test.
set -euo pipefail

# --- anchor to the repository root ------------------------------------------
# Inside the sandbox the factory's clone of the repository sits at the same
# path as the checkout on the host, and this file sits in its deploy/ folder.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

log() { printf '[sandbox-runner.sh] %s\n' "$*"; }

# --- step 1: the settings ---------------------------------------------------
ESTATE_ROOT="$(cd "${REPO_ROOT}/.." && pwd)"
FORGE_MOUNT="${SANDBOX_FORGE_PATH:-${ESTATE_ROOT}/forge}"
GUARDKIT_MOUNT="${SANDBOX_GUARDKIT_PATH:-${ESTATE_ROOT}/guardkit}"
ESTATE_SIBLINGS=(nats-core fleet-memory guardkitfactory)
SRC_ROOT="${HOME}/.forge-src"
VENV="${HOME}/.forge-venv"
BIND="${SANDBOX_RUNNER_BIND:-0.0.0.0}"
SIDECAR_PORT="${SANDBOX_SIDECAR_PORT:-8125}"
RUNNER_PORT="${SANDBOX_RUNNER_PORT:-8124}"
RESTART_SECONDS="${SANDBOX_RUNNER_RESTART_SECONDS:-5}"
# The deepagents band forge's Dockerfile pins beside guardkitfactory, for the
# reasons written there (0.7.x silently drops the supervisor's protocol prompt).
DEEPAGENTS_BAND='deepagents>=0.6.7,<0.7'

# The mounts, by name, in install order. Each must be a git checkout.
MOUNT_NAMES=(forge guardkit "${ESTATE_SIBLINGS[@]}")
mount_path_of() {
  case "$1" in
    forge) printf '%s' "${FORGE_MOUNT}" ;;
    guardkit) printf '%s' "${GUARDKIT_MOUNT}" ;;
    *) printf '%s' "$(dirname "${FORGE_MOUNT}")/$1" ;;
  esac
}

# The mounts belong to the host's user, not to the sandbox's, and git refuses
# to read a checkout owned by someone else unless told it is safe.
head_of() {
  git -c safe.directory='*' -C "$1" rev-parse HEAD 2>/dev/null
}

log "repo_root=${REPO_ROOT} forge=${FORGE_MOUNT} guardkit=${GUARDKIT_MOUNT} venv=${VENV}"

for name in "${MOUNT_NAMES[@]}"; do
  path="$(mount_path_of "${name}")"
  if [[ ! -d "${path}" ]] || ! head_of "${path}" >/dev/null; then
    log "FATAL: ${name} is not mounted at ${path} (or is not a git checkout). The sandbox must be created with forge, guardkit and the folders beside forge (${ESTATE_SIBLINGS[*]}) mounted read-only; deploy/sandbox-deploy.sh does that when the profile's sandbox block names the two service ports. Refusing to start."
    exit 2
  fi
done

# --- step 2: the copies -----------------------------------------------------
mkdir -p "${SRC_ROOT}"

# Copies one mount's tracked files at its HEAD into ${SRC_ROOT}/<name>, unless
# the copy already there was made from that same commit.
stage_tree() {
  local name="$1" path head target stamp
  path="$(mount_path_of "${name}")"
  head="$(head_of "${path}")"
  target="${SRC_ROOT}/${name}"
  stamp="${SRC_ROOT}/${name}.commit"
  if [[ -d "${target}" && -f "${stamp}" && "$(cat "${stamp}")" == "${head}" ]]; then
    log "${name}: copy already at ${head:0:12}"
    return 0
  fi
  log "${name}: copying the tracked files at ${head:0:12} from ${path}"
  rm -rf "${target}"
  mkdir -p "${target}"
  git -c safe.directory='*' -C "${path}" archive --format=tar HEAD | tar -x -C "${target}"
  printf '%s\n' "${head}" >"${stamp}"
}

for name in "${MOUNT_NAMES[@]}"; do
  stage_tree "${name}"
done

# --- step 3: the venv, once -------------------------------------------------
# The factory's own venv is made from the sandbox's own Python and never from
# an interpreter uv fetches, so uv's "never download" switch is set ON THIS ONE
# COMMAND. It is deliberately not exported: it would then be inherited by the
# two services below and by everything they start, and a repository's own build
# venv is pinned by guardkit to the floor of that repository's requires-python,
# which the sandbox's Python may be newer than. Exporting it made every work
# leg inside api_test's sandbox fail for want of an interpreter on 2026-09-08.
if [[ -x "${VENV}/bin/python" ]]; then
  log "venv already at ${VENV}"
else
  log "making the venv at ${VENV} from the sandbox's python3 with $(command -v uv)"
  UV_PYTHON_DOWNLOADS=never uv venv --python python3 "${VENV}"
fi

# --- step 4: the install, when a copy changed ------------------------------
# The stamp is the commits every copy was made from, so a changed copy means a
# fresh install and an unchanged set means none.
INSTALL_STAMP="${VENV}/.forge-installed"
wanted=""
for name in "${MOUNT_NAMES[@]}"; do
  wanted="${wanted}${name}=$(cat "${SRC_ROOT}/${name}.commit") "
done
if [[ -f "${INSTALL_STAMP}" && "$(cat "${INSTALL_STAMP}")" == "${wanted}" ]]; then
  log "install already matches the copies"
else
  log "installing the factory's code into ${VENV} from the copies"
  # The two folders beside forge that no package index can supply come first,
  # exactly as forge's own Dockerfile installs them before forge, so nothing
  # in this venv is ever fetched from the index under those names.
  uv pip install --python "${VENV}/bin/python" "${SRC_ROOT}/nats-core" "${SRC_ROOT}/fleet-memory"
  uv pip install --python "${VENV}/bin/python" "${SRC_ROOT}/forge[providers,memory,sidecar]"
  uv pip install --python "${VENV}/bin/python" "${SRC_ROOT}/guardkitfactory" "${DEEPAGENTS_BAND}"
  uv pip install --python "${VENV}/bin/python" "${SRC_ROOT}/guardkit"
  "${VENV}/bin/python" -c "import forge, guardkit, guardkit._installer_core, guardkitfactory"
  # guardkit's console script is guardkit-py; the runner shells `guardkit`.
  ln -sfn guardkit-py "${VENV}/bin/guardkit"
  printf '%s' "${wanted}" >"${INSTALL_STAMP}"
  log "install proven: forge, guardkit and guardkitfactory import"
fi

if [[ "${SANDBOX_RUNNER_BOOTSTRAP_ONLY:-}" == "1" ]]; then
  log "bootstrap only: the venv is ready; not starting the services"
  exit 0
fi

# --- step 5: the two services, kept running ---------------------------------
export PATH="${VENV}/bin:${PATH}"
export FORGE_GUARDKIT_PATH="${VENV}/bin/guardkit"
export GUARDKIT_HARNESS="${GUARDKIT_HARNESS:-langgraph}"
# This sidecar is the one INSIDE a repository's sandbox, and it says so. The
# deploy stage sends it the repository's own deploy/deploy.sh rather than the
# host wrapper deploy/sandbox-deploy.sh, which calls sbx and cannot run from
# in here; the sidecar's script allowlist permits that inner script only when
# this value is set, so the sidecar on the HOST — where the wrapper is exactly
# what keeps the work off the host — still permits the wrapper and nothing
# else (L3b's coach, 2026-09-08).
export FORGE_SIDECAR_IN_SANDBOX=1
if [[ -z "${FORGE_RECEIPTS_DIR:-}" && -n "${SANDBOX_RECEIPTS_PATH:-}" ]]; then
  export FORGE_RECEIPTS_DIR="${SANDBOX_RECEIPTS_PATH}"
fi
if [[ -n "${FORGE_DB_PATH:-}" ]]; then
  log "FORGE_DB_PATH was set in this environment; unsetting it — the ledger stays on the host and this runner never opens it (rule 72)"
fi
unset FORGE_DB_PATH

# Which of the load-bearing settings the services will find. Names only.
for name in OPENAI_BASE_URL OPENAI_API_KEY FORGE_CONFIG_PATH FORGE_NATS_URL FORGE_RECEIPTS_DIR GUARDKIT_HARNESS FORGE_SIDECAR_IN_SANDBOX; do
  if [[ -n "${!name:-}" ]]; then
    log "setting ${name}: set"
  else
    log "setting ${name}: unset"
  fi
done

SIDECAR_PID=""
RUNNER_PID=""

# The deploy sidecar, through the same function `python -m forge.deploy_sidecar`
# runs, but bound on every interface inside the sandbox rather than its
# loopback default, so the port the wrapper published reaches it.
start_sidecar() {
  log "starting the deploy sidecar on ${BIND}:${SIDECAR_PORT}"
  SANDBOX_RUNNER_BIND="${BIND}" FORGE_DEPLOY_SIDECAR_PORT="${SIDECAR_PORT}" \
    "${VENV}/bin/python" -c 'import os; from forge.deploy_sidecar.service import serve; serve(host=os.environ["SANDBOX_RUNNER_BIND"], port=int(os.environ["FORGE_DEPLOY_SIDECAR_PORT"]))' &
  SIDECAR_PID=$!
}

# The build runner: `langgraph dev` serving the autobuild_runner graph from
# forge's own forge.langgraph.json in the copy, with the same flags the host
# unit used. The copy has no .env for the config to read: only tracked files
# were copied, and the runner's environment is the sandbox's.
start_runner() {
  log "starting the build runner on ${BIND}:${RUNNER_PORT}"
  (
    cd "${SRC_ROOT}/forge" &&
      exec "${VENV}/bin/langgraph" dev \
        --config forge.langgraph.json \
        --host "${BIND}" \
        --port "${RUNNER_PORT}" \
        --no-browser \
        --no-reload \
        --allow-blocking
  ) &
  RUNNER_PID=$!
}

stop_services() {
  local pid
  for pid in "${SIDECAR_PID}" "${RUNNER_PID}"; do
    if [[ -n "${pid}" ]]; then
      kill "${pid}" 2>/dev/null || true
    fi
  done
  wait 2>/dev/null || true
}

trap 'log "asked to stop; stopping both services"; stop_services; exit 0' TERM INT

start_sidecar
start_runner
log "both services are up: sidecar pid ${SIDECAR_PID}, runner pid ${RUNNER_PID}"

while true; do
  died=""
  # `wait -n -p` puts the pid of whichever service ended into `died` and
  # returns that service's exit status, which is what is logged.
  set +e
  wait -n -p died "${SIDECAR_PID}" "${RUNNER_PID}"
  rc=$?
  set -e
  if [[ "${died}" == "${SIDECAR_PID}" ]]; then
    log "the deploy sidecar exited ${rc}; starting it again in ${RESTART_SECONDS}s"
    sleep "${RESTART_SECONDS}"
    start_sidecar
  elif [[ "${died}" == "${RUNNER_PID}" ]]; then
    log "the build runner exited ${rc}; starting it again in ${RESTART_SECONDS}s"
    sleep "${RESTART_SECONDS}"
    start_runner
  fi
done

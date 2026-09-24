#!/usr/bin/env bash
#
# THE SANDBOX RUNNER — the container that replaces the two host units
# forge-sandbox-keeper@ and forge-sandbox-runner@ for one project's sandbox.
# Written 24 September 2026, stage 3 of the containerisation rollout gate.
#
# WHAT IT DOES, and it is only these three things:
#
#   1. KEEPS THE SANDBOX AWAKE. A sandbox stops itself about thirty seconds
#      after its last session ends, so this holds one session open doing
#      nothing (what the keeper unit did with `sleep infinity`). The held
#      process is given a distinctive name so this service can end its own
#      hold and nobody else's.
#   2. STARTS THE PROJECT'S OWN BOOTSTRAP INSIDE THE SANDBOX, the way the
#      runner unit did: one session, running the command the project declares,
#      with the settings it needs handed in on the front of the line. This
#      service knows none of those settings by name: it forwards the NAMES
#      listed in SANDBOX_ENV_NAMES and never looks at a value.
#   3. ON STOP, ENDS THE WORK INSIDE. This is the heart of it. The client runs
#      out here and all the work runs in there, so ending the client ends
#      NOTHING: the bootstrap keeps running inside the sandbox and keeps
#      supervising its services. That is written out at length in the unit this
#      replaces (forge/ops/systemd/forge-sandbox-runner@.service, "WHY
#      ExecStop"), and it cost a whole day of a build serving a mixture of old
#      and new code on 2026-09-11, when four supervisors had piled up inside
#      one sandbox. So a stop signal here goes back through the SAME door the
#      start went through and asks the project's own bootstrap to stop itself —
#      and WAITS for it. Docker's stop grace period has to be long enough for
#      that; the compose file sets it.
#
#      The same stop runs before every restart of the bootstrap session, for
#      the same reason systemd runs ExecStop before every automatic restart: a
#      session that merely dropped has left its supervisor running in there,
#      and starting a second one is how the pile-up happens.
#
# WHAT IT IS NOT. It is not a deploy, it never creates, removes or reconfigures
# a sandbox, and it knows nothing about the project inside it — no language, no
# test runner, no package manager, no layout. It runs one command the project
# declares and stops it again.
#
# WHY THIS ONE CONTAINER TOUCHES THE HOST. Everything else in this bundle talks
# over a declared network to another container. This service talks to the
# SANDBOX DAEMON, which makes the sandboxes and therefore is the host. It is
# given exactly one thing of the host's: that daemon's socket, and the static
# client binary that speaks to it. It publishes no port, joins no network, and
# binds no folder of anybody's. See README.md, "The one service that touches
# the host".
#
# SETTINGS, all names, all from the environment the container was started with:
#
#   SANDBOX_NAME            REQUIRED. The sandbox this service looks after, by
#                           the name the project declares.
#   SANDBOX_BOOTSTRAP       REQUIRED. The command to run inside the sandbox —
#                           the project's own bootstrap, as it is invoked
#                           today.
#   SANDBOX_BOOTSTRAP_STOP_ARGUMENT
#                           The word that bootstrap takes to stop itself
#                           (default: stop). Nothing else about stopping is
#                           known out here: no process names, no ports. If the
#                           bootstrap does not take that word, do not run this
#                           service against it — see README.md.
#   SANDBOX_ENV_NAMES       The names, space or comma separated, to hand to the
#                           bootstrap. Each name that has a value in this
#                           container's environment is passed through with it;
#                           a name with nothing set is left out, and the
#                           bootstrap reports it unset. Values are never logged.
#   SANDBOX_CLIENT          The sandbox client binary (default /usr/bin/sbx).
#   SANDBOX_CLIENT_ARGUMENTS
#                           Anything the client needs before the verb, e.g. a
#                           flag pinning it to the local daemon. Empty by
#                           default.
#   SANDBOX_RESTART_SECONDS The pause before a dropped session is opened again
#                           (default 5).
#   SANDBOX_STOP_TIMEOUT_SECONDS
#                           How long the stop inside the sandbox is given
#                           before it is abandoned (default 45). Keep the
#                           compose file's stop_grace_period comfortably above
#                           it.
#   SANDBOXES_STORAGE_ROOT  The client's own writable state folder, and the
#                           folder under which it looks for the daemon's
#                           socket at state/sandboxes/sandboxes/sandboxd/
#                           sandboxd.sock. Put it anywhere else and the client
#                           quietly decides it is talking to a hosted service
#                           and complains about authentication.
#
# EXIT. 0 after a clean stop. 2 when a required setting is missing — said in
# one sentence, at the door, rather than started half-configured.

set -uo pipefail

log() { printf '[sandbox-runner] %s\n' "$*"; }

CLIENT="${SANDBOX_CLIENT:-/usr/bin/sbx}"
NAME="${SANDBOX_NAME:-}"
BOOTSTRAP="${SANDBOX_BOOTSTRAP:-}"
STOP_ARGUMENT="${SANDBOX_BOOTSTRAP_STOP_ARGUMENT:-stop}"
RESTART_SECONDS="${SANDBOX_RESTART_SECONDS:-5}"
STOP_TIMEOUT_SECONDS="${SANDBOX_STOP_TIMEOUT_SECONDS:-45}"

# The name the held session runs under inside the sandbox, so that this
# service can end its own hold and nothing else. It is not a setting: nobody
# outside this file needs to know it, and two copies of this file looking
# after the same sandbox would be a mistake either way.
KEEPER_HOLD_NAME="forge-sandbox-keeper-hold"

if [[ -z "${NAME}" ]]; then
  log "FATAL: SANDBOX_NAME is not set. This service looks after one sandbox, named by the project it belongs to. Refusing to start."
  exit 2
fi
if [[ -z "${BOOTSTRAP}" ]]; then
  log "FATAL: SANDBOX_BOOTSTRAP is not set. This service runs the command the project declares inside its sandbox, and it has not been told what that command is. Refusing to start."
  exit 2
fi
if [[ ! -x "${CLIENT}" ]]; then
  log "FATAL: the sandbox client is not at ${CLIENT}. It is bound into this container read-only from the machine; see the compose file. Refusing to start."
  exit 2
fi

# The client's own state folder, and the socket under it, are the two things
# learned the hard way (the probe of 24 September 2026). Say plainly which is
# wrong rather than letting the client fall back to a hosted service and
# complain about authentication.
STORAGE_ROOT="${SANDBOXES_STORAGE_ROOT:-}"
if [[ -z "${STORAGE_ROOT}" ]]; then
  log "FATAL: SANDBOXES_STORAGE_ROOT is not set. The client needs a writable folder of its own. Refusing to start."
  exit 2
fi
SOCKET="${STORAGE_ROOT}/state/sandboxes/sandboxes/sandboxd/sandboxd.sock"
if [[ ! -w "${STORAGE_ROOT}" ]]; then
  log "FATAL: ${STORAGE_ROOT} is not writable by this container's user. A fresh volume belongs to root until it is handed over; see README.md. Refusing to start."
  exit 2
fi
if [[ ! -S "${SOCKET}" ]]; then
  log "FATAL: the sandbox daemon's socket is not at ${SOCKET}. The client looks for it at exactly that path under its storage root; anywhere else and it decides it is talking to a hosted service. Refusing to start."
  exit 2
fi

# The client call, once: everything below reaches the sandbox through this.
# SANDBOX_CLIENT_ARGUMENTS is deliberately word-split — it is a list of flags.
# shellcheck disable=SC2206
CLIENT_CALL=("${CLIENT}" ${SANDBOX_CLIENT_ARGUMENTS:-} exec "${NAME}")

# The settings handed to the bootstrap: the NAMES the project declares, each
# with whatever this container's environment has under it. A name with nothing
# set is left out, so the bootstrap's own "setting X: unset" line stays true.
# No value is ever printed here.
ENV_PREFIX=()
FORWARDED=()
SKIPPED=()
NAMES_TO_HAND_IN="${SANDBOX_ENV_NAMES:-}"
for name in ${NAMES_TO_HAND_IN//,/ }; do
  if [[ -n "${!name:-}" ]]; then
    ENV_PREFIX+=("${name}=${!name}")
    FORWARDED+=("${name}")
  else
    SKIPPED+=("${name}")
  fi
done
if ((${#ENV_PREFIX[@]})); then
  ENV_PREFIX=(env "${ENV_PREFIX[@]}")
fi

log "sandbox=${NAME} bootstrap=${BOOTSTRAP} client=${CLIENT}"
log "settings handed in (names only): ${FORWARDED[*]:-none}"
log "settings named but not set here: ${SKIPPED[*]:-none}"

STOPPING=0
KEEPER_PID=""
BOOTSTRAP_PID=""

# A sleep a signal can cut short: a plain `sleep` would hold the trap until it
# finished, and a stop is not something to keep anybody waiting for.
nap() {
  local seconds="$1"
  sleep "${seconds}" &
  wait $! 2>/dev/null
}

start_keeper() {
  log "holding the sandbox awake"
  "${CLIENT_CALL[@]}" bash -c "exec -a ${KEEPER_HOLD_NAME} sleep infinity" &
  KEEPER_PID=$!
}

start_bootstrap() {
  log "starting the project's bootstrap inside the sandbox"
  "${CLIENT_CALL[@]}" "${ENV_PREFIX[@]}" "${BOOTSTRAP}" &
  BOOTSTRAP_PID=$!
}

# The stop that is the point of this service: through the same door, asking
# the project's own bootstrap to stop itself, and waited for.
stop_the_work_inside() {
  local rc=0
  log "asking the bootstrap inside ${NAME} to stop, and waiting for it"
  timeout "${STOP_TIMEOUT_SECONDS}" \
    "${CLIENT_CALL[@]}" "${BOOTSTRAP}" "${STOP_ARGUMENT}" || rc=$?
  if ((rc == 124)); then
    log "the stop inside ${NAME} did not finish within ${STOP_TIMEOUT_SECONDS}s; abandoning it (the work may still be running in there)"
  elif ((rc != 0)); then
    log "the stop inside ${NAME} ended ${rc} (the door may be shut: sandbox removed, or the daemon out of reach)"
  else
    log "the work inside ${NAME} has stopped"
  fi
  return "${rc}"
}

# End this service's own hold, and only its own: the held process runs under a
# name nothing else uses. Nothing to kill is the ordinary case, not a failure.
release_the_hold() {
  timeout "${STOP_TIMEOUT_SECONDS}" \
    "${CLIENT_CALL[@]}" pkill -f "^${KEEPER_HOLD_NAME}" >/dev/null 2>&1
  log "released this service's hold on ${NAME}"
}

on_stop_signal() {
  if ((STOPPING == 1)); then return; fi
  STOPPING=1
  log "asked to stop"
}
trap on_stop_signal TERM INT

start_keeper
start_bootstrap

while ((STOPPING == 0)); do
  died=""
  wait -n -p died "${KEEPER_PID}" "${BOOTSTRAP_PID}"
  rc=$?
  ((STOPPING == 1)) && break
  if [[ -z "${died}" ]]; then
    # A signal we handle, or nothing left to wait for. Round the loop.
    ((rc > 128)) && continue
    log "no session left to wait for; starting both again"
    nap "${RESTART_SECONDS}"
    ((STOPPING == 1)) && break
    start_keeper
    start_bootstrap
    continue
  fi
  if [[ "${died}" == "${BOOTSTRAP_PID}" ]]; then
    log "the bootstrap session ended ${rc}"
    # Before opening another one: the session ending out here left the
    # supervisor running in there. Starting a second is the pile-up.
    stop_the_work_inside
    ((STOPPING == 1)) && break
    nap "${RESTART_SECONDS}"
    ((STOPPING == 1)) && break
    start_bootstrap
  elif [[ "${died}" == "${KEEPER_PID}" ]]; then
    log "the hold on ${NAME} ended ${rc}"
    ((STOPPING == 1)) && break
    nap "${RESTART_SECONDS}"
    ((STOPPING == 1)) && break
    start_keeper
  fi
done

# The stop, in order: end the work inside first, then let the sandbox sleep.
stop_the_work_inside
release_the_hold
for pid in "${BOOTSTRAP_PID}" "${KEEPER_PID}"; do
  [[ -n "${pid}" ]] && kill "${pid}" 2>/dev/null
done
wait 2>/dev/null
log "stopped"
exit 0

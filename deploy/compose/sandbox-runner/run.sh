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
#      listed in SANDBOX_ENV_NAMES and never prints a value. It looks at a
#      value only to refuse one that carries a password (see "nothing
#      credential-shaped is handed into the sandbox" below).
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
#      AND THE STOP HAS TO HAVE WORKED. A stop that ends nonzero, or does not
#      finish inside its timeout, means the work in there may still be running.
#      So nothing is ever started on top of it: the stop is tried a few times,
#      and if it still will not work this service starts nothing and exits
#      non-zero, rather than adding a second supervisor or reporting a clean
#      stop it did not achieve. It is also the reason the stop is made with
#      exactly the settings the start was made with: a bootstrap that picks
#      out its own process record, or its target, from one of those settings
#      would otherwise be asked to stop something else, or nothing at all.
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
#   SANDBOX_STOP_ATTEMPTS   How many times a stop that failed is tried again
#                           before this service gives up (default 3). Keep
#                           attempts times (timeout + pause) below the compose
#                           file's stop_grace_period as well.
#   SANDBOX_STOP_RETRY_SECONDS
#                           The pause between those tries (default 5).
#   SANDBOXES_STORAGE_ROOT  The client's own writable state folder, and the
#                           folder under which it looks for the daemon's
#                           socket at state/sandboxes/sandboxes/sandboxd/
#                           sandboxd.sock. Put it anywhere else and the client
#                           quietly decides it is talking to a hosted service
#                           and complains about authentication.
#
# EXIT. 0 after a clean stop. 2 when a required setting is missing, or when
# SANDBOX_ENV_NAMES would hand something credential-shaped into the sandbox —
# said in one sentence, at the door, rather than started half-configured. 3 when the
# work inside would not stop: nothing was started on top of it, nothing claims
# to have stopped, and the container comes back under its restart policy to try
# the same stop again.

set -uo pipefail

log() { printf '[sandbox-runner] %s\n' "$*"; }

CLIENT="${SANDBOX_CLIENT:-/usr/bin/sbx}"
NAME="${SANDBOX_NAME:-}"
BOOTSTRAP="${SANDBOX_BOOTSTRAP:-}"
STOP_ARGUMENT="${SANDBOX_BOOTSTRAP_STOP_ARGUMENT:-stop}"
RESTART_SECONDS="${SANDBOX_RESTART_SECONDS:-5}"
STOP_TIMEOUT_SECONDS="${SANDBOX_STOP_TIMEOUT_SECONDS:-45}"
STOP_ATTEMPTS="${SANDBOX_STOP_ATTEMPTS:-3}"
STOP_RETRY_SECONDS="${SANDBOX_STOP_RETRY_SECONDS:-5}"

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

# NOTHING CREDENTIAL-SHAPED IS HANDED INTO THE SANDBOX (14 October 2026).
# Whatever crosses here ends up in the settings of the bootstrap's own process
# inside the sandbox, and from there in both factory containers, where the code
# a build runs can read it. The factory's keys stay outside: model calls go
# through the model door on the gateway and memory through the memory door.
# So a name in SANDBOX_ENV_NAMES that looks like a credential stops the start
# here, before the sandbox client is called at all, with one sentence naming
# each setting and never a value. The same rule, word for word, is in the
# bootstrap inside the sandbox (src/forge/cli/deploy_templates/
# sandbox-runner.sh):
#   - a name containing one of Forge's own credential words
#     (RESERVED_CREDENTIAL_WORDS in src/forge/launch_environment.py), or DSN;
#   - a name with KEY as a whole word in it (A_SERVICE_KEY, KEY_FILE);
#   - a value that is an address with any user information in it, an address
#     whose query or fragment names a credential, or a password pair (with =
#     or :), including the same written with escapes: every percent-escape
#     is decoded, however many times over (up to five), and a value whose
#     encoding cannot be read safely is refused too.
# ONE EXACT NAME IS EXCUSED, and no pattern is: the stamp check's token count,
# whose name contains TOKEN. It may cross only as a plain number; anything
# else in it (a letter, a space, nothing at all) refuses the start too.
CREDENTIAL_WORDS=(TOKEN SECRET PASSWORD PASSWD API_KEY APIKEY CREDENTIAL PRIVATE_KEY DSN)
# Words that make a name a credential only as a whole word between
# underscores: KEY in A_SERVICE_KEY, KEY_FILE or MY_KEY_ID, but not in
# KEYBOARD_LAYOUT or MONKEY_COUNT.
CREDENTIAL_WHOLE_WORDS=(KEY)
CREDENTIAL_SHAPED_BUT_ALLOWED=(GUARDKIT_STAMP_MODEL_MAX_TOKENS)
# The shapes of a value that carries a credential, matched on the value in
# lower case, and again once escapes are undone (every percent-escape,
# decoded again and again until it stops changing, JSON's \/, and a backslash
# used for a slash, as in http:\\someone@host), so an encoded copy is caught
# too:
#   an address with any user information in it, a user alone or a user and a
#   password (scheme://someone@ or scheme://someone:secret@);
#   an address whose query names a credential (?access_token=, &api_key=,
#   ;pwd=, ?auth=);
#   an address whose fragment, after #, holds user information or names a
#   credential (#access_token=);
#   a password pair anywhere, with = or : (password=, pwd: x, "passwd":).
USER_IN_AN_ADDRESS='[a-z][a-z0-9+.-]*://[^/?#[:space:]]*@'
CREDENTIAL_IN_A_QUERY='://[^?#[:space:]]*[?]([^#[:space:]]*[&;])?[^=&;#[:space:]]*(pass|pwd|token|secret|key|auth)[^=&;#[:space:]]*='
CREDENTIAL_IN_A_FRAGMENT='://[^#[:space:]]*#[^[:space:]]*(@|(pass|pwd|token|secret|key|auth)[^=&;[:space:]]*=)'
PASSWORD_PAIR="(^|[^a-z0-9])(password|passwd|pwd)[\"']?[[:space:]]*[=:]"

# A value as a reader of it would see it, put in UNESCAPED: every %XX escape
# decoded, one level a round, until a round changes nothing (at most five
# rounds), so an escape-spelt word (%74oken) and a value encoded twice are
# caught; then JSON's \/ and a backslash used for a slash turned into
# slashes; then folded to lower case. It fails, and the value is refused
# rather than judged half-read, when a % is not followed by two hex digits,
# when an escape is a NUL byte, or when the value is still encoded after
# five rounds. Used only to judge a value, never printed. The C locale makes
# it work byte by byte.
UNESCAPED=""
the_value_unescaped() {
  local LC_ALL=C value="$1" rest decoded hex char round escaped_slash='\/' backslash='\'
  UNESCAPED=""
  for round in 1 2 3 4 5 6; do
    [[ "${value}" == *%* ]] || break
    ((round <= 5)) || return 1
    rest="${value}"
    decoded=""
    while [[ "${rest}" == *%* ]]; do
      decoded+="${rest%%\%*}"
      rest="${rest#*%}"
      [[ "${rest}" =~ ^[0-9A-Fa-f]{2} ]] || return 1
      hex="${rest:0:2}"
      [[ "${hex}" != "00" ]] || return 1
      printf -v char "\\x${hex}"
      decoded+="${char}"
      rest="${rest:2}"
    done
    value="${decoded}${rest}"
  done
  value="${value//"${escaped_slash}"/\/}"
  value="${value//"${backslash}"/\/}"
  UNESCAPED="${value,,}"
}

# Prints why one named setting may not be handed in, or nothing when it may.
# Arguments: the entry, the name of the list it came from, and its position
# in that list. It reads the value only to judge it, and never prints it; an
# entry that is not a setting name is named by its list and position only,
# because its text may be a value written into the list by mistake.
why_this_setting_may_not_cross() {
  local name="$1" list="$2" position="$3" word allowed value seen
  if [[ ! "${name}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    printf 'an entry in %s is not a setting name (position %s)' "${list}" "${position}"
    return 0
  fi
  for allowed in "${CREDENTIAL_SHAPED_BUT_ALLOWED[@]}"; do
    if [[ "${name}" == "${allowed}" ]]; then
      if [[ -n "${!name+set}" && ! "${!name}" =~ ^[0-9]+$ ]]; then
        printf '%s (it may be handed in only as a plain number)' "${name}"
      fi
      return 0
    fi
  done
  for word in "${CREDENTIAL_WORDS[@]}"; do
    if [[ "${name^^}" == *"${word}"* ]]; then
      printf '%s (its name says it is a credential)' "${name}"
      return 0
    fi
  done
  for word in "${CREDENTIAL_WHOLE_WORDS[@]}"; do
    if [[ "_${name^^}_" == *"_${word}_"* ]]; then
      printf '%s (its name says it is a credential)' "${name}"
      return 0
    fi
  done
  value="${!name:-}"
  [[ -n "${value}" ]] || return 0
  if ! the_value_unescaped "${value}"; then
    printf '%s (its value is percent-encoded in a way that cannot be read safely)' "${name}"
    return 0
  fi
  for seen in "${value,,}" "${UNESCAPED}"; do
    if [[ "${seen}" =~ ${USER_IN_AN_ADDRESS} ]]; then
      printf '%s (its value is an address with a user or a password in it)' "${name}"
      return 0
    fi
    if [[ "${seen}" =~ ${CREDENTIAL_IN_A_QUERY} ]]; then
      printf '%s (its value is an address whose query carries a credential)' "${name}"
      return 0
    fi
    if [[ "${seen}" =~ ${CREDENTIAL_IN_A_FRAGMENT} ]]; then
      printf '%s (its value is an address whose fragment carries a credential)' "${name}"
      return 0
    fi
    if [[ "${seen}" =~ ${PASSWORD_PAIR} ]]; then
      printf '%s (its value carries a password pair)' "${name}"
      return 0
    fi
  done
  return 0
}

NAMES_TO_HAND_IN="${SANDBOX_ENV_NAMES:-}"
REFUSED_SETTINGS=()
position=0
for name in ${NAMES_TO_HAND_IN//,/ }; do
  position=$((position + 1))
  reason="$(why_this_setting_may_not_cross "${name}" "SANDBOX_ENV_NAMES" "${position}")"
  if [[ -n "${reason}" ]]; then
    REFUSED_SETTINGS+=("${reason}")
  fi
done
if ((${#REFUSED_SETTINGS[@]})); then
  joined="$(printf '%s, ' "${REFUSED_SETTINGS[@]}")"
  log "FATAL: refusing to start, because no key belongs in the sandbox and SANDBOX_ENV_NAMES would hand these settings in: ${joined%, }. Take them off SANDBOX_ENV_NAMES; the model door and the memory door on the gateway hold the factory's keys outside the sandbox. Nothing was started inside ${NAME}."
  exit 2
fi

# The settings handed to the bootstrap: the NAMES the project declares, each
# with whatever this container's environment has under it. A name with nothing
# set is left out, so the bootstrap's own "setting X: unset" line stays true.
# No value is ever printed here.
ENV_PREFIX=()
FORWARDED=()
SKIPPED=()
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

# THE ONE WAY THE PROJECT'S BOOTSTRAP IS REACHED, built once and used by both
# the start and the stop, so the two cannot drift apart. The stop therefore
# carries exactly the settings the start carried: a bootstrap that finds its own
# process record, or its target, through one of those settings is asked to stop
# the thing it was asked to start, and not something else. It stays an array all
# the way to the client, so a value with spaces in it arrives as one value.
BOOTSTRAP_CALL=("${CLIENT_CALL[@]}" "${ENV_PREFIX[@]}" "${BOOTSTRAP}")

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
  "${BOOTSTRAP_CALL[@]}" &
  BOOTSTRAP_PID=$!
}

# The stop that is the point of this service: through the same door, with the
# same settings, asking the project's own bootstrap to stop itself, and waited
# for. Returns what the stop returned; 124 is the timeout.
stop_the_work_inside() {
  local rc=0
  log "asking the bootstrap inside ${NAME} to stop, and waiting for it"
  timeout "${STOP_TIMEOUT_SECONDS}" \
    "${BOOTSTRAP_CALL[@]}" "${STOP_ARGUMENT}" || rc=$?
  if ((rc == 124)); then
    log "the stop inside ${NAME} did not finish within ${STOP_TIMEOUT_SECONDS}s; abandoning it (the work may still be running in there)"
  elif ((rc != 0)); then
    log "the stop inside ${NAME} ended ${rc} (the door may be shut: sandbox removed, or the daemon out of reach)"
  else
    log "the work inside ${NAME} has stopped"
  fi
  return "${rc}"
}

# NOTHING IS STARTED ON TOP OF WORK THAT MAY STILL BE RUNNING. Every route to a
# bootstrap start goes through this, and so does the shutdown. A stop that ends
# nonzero or times out is tried again, a bounded number of times with a pause;
# only a stop that really worked lets anything else happen.
require_the_work_inside_to_stop() {
  local attempt=1
  while :; do
    if stop_the_work_inside; then
      return 0
    fi
    if ((attempt >= STOP_ATTEMPTS)); then
      log "the stop inside ${NAME} has now failed ${attempt} time(s)"
      return 1
    fi
    attempt=$((attempt + 1))
    log "trying the stop inside ${NAME} again in ${STOP_RETRY_SECONDS}s (try ${attempt} of ${STOP_ATTEMPTS})"
    nap "${STOP_RETRY_SECONDS}"
  done
}

# The client sessions this container is holding open. Ending them ends NOTHING
# inside the sandbox — that is the whole premise of this file — but it lets this
# process go.
end_the_local_sessions() {
  local pid
  for pid in "${BOOTSTRAP_PID}" "${KEEPER_PID}"; do
    [[ -n "${pid}" ]] && kill "${pid}" 2>/dev/null
  done
  wait 2>/dev/null
  return 0
}

# WHAT HAPPENS TO THE HOLD WHEN THE STOP WILL NOT WORK: it is LEFT IN PLACE, on
# purpose, and that is the only case in which this service leaves it. Releasing
# it would let the sandbox fall asleep about thirty seconds later and cut the
# work off mid-flight without it ever having been asked to stop — the abrupt
# ending this whole file exists to avoid — and it would leave the machine
# looking tidy while the problem was still in there. Left alone, the sandbox
# stays awake, what is running in it can be looked at and stopped properly, and
# the next container's stop does not have to wake the sandbox first. The hold
# runs under one fixed name, so the next release of it ends it and any second
# copy of it together.
#
# The container's restart policy brings this service back, and the first thing
# it does is the same stop again — which is why the sentence below says so.
give_up_because_the_stop_failed() {
  end_the_local_sessions
  log "this service's hold on ${NAME} is being left in place on purpose, so the sandbox stays awake and the work in there can be stopped properly instead of being cut off when the sandbox falls asleep"
  log "FATAL: the work inside ${NAME} would not stop after ${STOP_ATTEMPTS} tries and may still be running in there, so this service has started nothing on top of it and is exiting non-zero rather than reporting a stop it did not achieve; the container's restart policy will bring it back and it will try the same stop again before it starts anything, and if it keeps failing, look inside ${NAME} and stop the project's bootstrap by hand."
  exit 3
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

# ONCE, BEFORE THE FIRST START: ask whatever is already running inside to stop.
# The unit this replaces got this for free, because systemd runs ExecStop even
# when the client was killed; a container gets no such courtesy. So a container
# that died without stopping — killed, or the machine went down — and is started
# again would otherwise start a SECOND supervisor beside the one still running
# inside (the second review of 24 September 2026 produced exactly that against
# a bootstrap with no lock of its own). Nothing running inside is the ordinary
# case, and the bootstrap's stop says so and exits; that is not a failure. A
# stop that will not work IS one, and then nothing starts.
require_the_work_inside_to_stop || give_up_because_the_stop_failed
# A stop signal that arrived WHILE that first stop was being retried means
# nothing should start at all: the same guard the two later routes have (the
# third review of 24 September 2026 caught this one starting a project's whole
# stack only to tear it straight down again, inside the grace period).
if ((STOPPING == 1)); then
  log "asked to stop before anything was started; nothing to do"
  log "stopped"
  exit 0
fi
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
    # Both sessions are gone out here, which says nothing about what is still
    # running in there: the same stop, and it has to have worked.
    require_the_work_inside_to_stop || give_up_because_the_stop_failed
    ((STOPPING == 1)) && break
    start_keeper
    start_bootstrap
    continue
  fi
  if [[ "${died}" == "${BOOTSTRAP_PID}" ]]; then
    log "the bootstrap session ended ${rc}"
    # A NON-ZERO STATUS HERE IS LOGGED AND HANDLED LIKE ANY OTHER ENDING, and
    # it cannot loop fast: the stop below has to work first, and then there is
    # the pause. From 24 September 2026 the shipped bootstrap answers 4 when it
    # refuses to start because a supervisor of that checkout is already running
    # in the sandbox — which on this path cannot happen, because the stop above
    # succeeded before anything was started and succeeds again before anything
    # is started here. If it ever does appear in these logs, the sandbox holds
    # a supervisor this service did not start and nothing more should be
    # started on top of it, which is exactly what happens below.
    #
    # Before opening another one: the session ending out here left the
    # supervisor running in there. Starting a second is the pile-up, so the
    # stop has to have worked before another one is opened.
    require_the_work_inside_to_stop || give_up_because_the_stop_failed
    ((STOPPING == 1)) && break
    nap "${RESTART_SECONDS}"
    ((STOPPING == 1)) && break
    start_bootstrap
  elif [[ "${died}" == "${KEEPER_PID}" ]]; then
    # Only the hold is opened again here. The bootstrap's own session is still
    # up, so nothing is being started on top of it and no stop belongs here.
    log "the hold on ${NAME} ended ${rc}"
    ((STOPPING == 1)) && break
    nap "${RESTART_SECONDS}"
    ((STOPPING == 1)) && break
    start_keeper
  fi
done

# The stop, in order: end the work inside first, then let the sandbox sleep.
# If the work inside will not stop, this says so and exits non-zero: `docker
# compose stop` and `docker ps` then show the truth instead of a tidy 0, and
# the hold stays (see above) so the sandbox does not fall asleep on top of it.
require_the_work_inside_to_stop || give_up_because_the_stop_failed
release_the_hold
end_the_local_sessions
log "stopped"
exit 0

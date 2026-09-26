#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# THE GATEWAY WATCH — is the Slack door actually carrying traffic?
# (26 September 2026, rollout step 1, build item E3-e.)
#
# WHAT IT REPLACES. A host timer on this machine, jarvis-serve-nats-watchdog,
# fired every fifteen minutes, asked systemd whether the Slack gateway's user
# unit was active, and posted to Slack when it was not. The gateway is a
# container now, so that question has no answer any more — and after the rollout
# that unit is meant to be inactive for good, so the old alarm would have called
# a healthy factory dead every quarter of an hour. It is retired. This is its
# named replacement, and it asks the right question of the right thing.
#
# WHAT IT REFUSES TO DO: report one boolean. The gateway holds TWO things open —
# a connection to the bus and a Slack Socket Mode websocket — and either can die
# without the other. "Connected to the bus, subscribed, and answering nothing
# from Slack" is a real state and it is the one the retired alarm existed for. So
# this says three things separately:
#
#   bus connection   is the gateway's OWN connection on the bus?
#   Slack session    what does the gateway say about its Socket Mode session?
#   recent activity  is its container still logging, or has it gone silent?
#
# and never collapses them. An 'unknown' is reported as an unknown.
#
# WHY THE BUS QUESTION NEEDS A NAME. The estate gives the bus gateway and the
# Slack front door the SAME bus account on purpose, so "the account has a live
# connection" is TRUE while the gateway is stopped and the front door is
# running: that is a stopped door reported healthy. A connection counts as the
# gateway's only when all THREE hold — the account, the client name the gateway
# sends for itself, and the subscription the gateway is the only thing to hold.
#
# IT READS AND IT TELLS. It restarts nothing, writes nothing to the bus, and
# needs no credential for either of the two things it reads (the bus's
# monitoring route takes none, and neither does a container's log). The only
# secret it ever touches is the Slack token it posts its own alarm with, and in
# rehearsal it is pointed at a stand-in that writes a file instead.
#
# IT DOES NOT RUN DURING A ROLLOUT WINDOW. Its compose profile is added after
# the estate is up and the door is open, which is what keeps a window quiet.
#
# EXIT CODES — a broken watch must never look like a healthy door:
#    0  every component ok. Nothing was sent.
#   10  unhappy. ONE message was sent, naming which component and why.
#   20  the watch itself failed — bad arguments, or a message it could not
#       deliver. A silent success is the one thing this must never be.
# ---------------------------------------------------------------------------
set -uo pipefail

readonly EXIT_OK=0
readonly EXIT_UNHAPPY=10
readonly EXIT_BROKEN=20

# The retired alarm's backstop, kept — with its reason corrected by the E3
# review of 26 September 2026: a healthy but idle door in its CONTAINER is
# silent (zero log lines in four and a half minutes of steady state; the old
# alarm read a journal that also carried the unit's own chatter). So this
# window rests on one thing only: the Slack session rotates roughly every five
# hours and logs as it goes. Six hours is longer than a rotation, with about an
# hour to spare — thin. A workspace whose rotation ran past six hours would get
# a wrong "gone silent" every look; that is what GATEWAY_WATCH_MAX_SILENCE_S is
# for, and the heartbeat's own freshness is the other, independent signal.
readonly DEFAULT_MAX_SILENCE_S=21600
# The same number and the same reason for the heartbeat: a rotation rewrites that
# file, so a file older than a full rotation means the writer stopped.
readonly DEFAULT_HEARTBEAT_MAX_AGE_S=21600
readonly DEFAULT_SUBJECT="agents.command.jarvis"
readonly DEFAULT_HEARTBEAT_PATH="/var/lib/jarvis/slack-heartbeat.json"
readonly DEFAULT_GATEWAY_SERVICE="bus-gateway"
readonly DEFAULT_DOCKER_SOCKET="/var/run/docker.sock"
readonly DEFAULT_LOG_LINES=300
readonly SLACK_POST_URL="https://slack.com/api/chat.postMessage"

# ---------------------------------------------------------------------------
# WHICH LINES OF THE LOG ARE ABOUT SLACK AT ALL (26 September 2026, the fix of
# the one blocker an independent review of this watch found).
#
# THE GATEWAY CONTAINER'S LOG IS NOT A SLACK LOG. jarvis's own bus client writes
# into the very same stream: 'nats_disconnect' when the bus connection drops,
# 'nats_reconnect' when it comes back, and 'nats_error' carrying the words of
# whatever failed — 'Connect call failed', 'ConnectionRefusedError'
# (jarvis/src/jarvis/infrastructure/nats_client.py). The retired alarm's trouble
# words — 'disconnect', 'reconnect', 'connection ... refused', which were copied
# in here verbatim from ops/systemd/serve_nats_watchdog.py — match every one of
# those, and the only line that CLEARS the signal, 'has been established', is
# written by slack-sdk alone. So a bus event could never be cleared by a bus
# event.
#
# MEASURED, NOT IMAGINED. A reviewer restarted a throwaway bus container and
# touched nothing else: Slack was up, the gateway reconnected to the bus by
# itself, and this watch sent one message headed 'The factory's Slack door has
# lost its Slack session'. On an idle door the heartbeat only moves when Slack
# sends something — roughly every five hours — so that wrong message would have
# been repeated every fifteen minutes for hours. The alarm this replaces was
# retired for crying wolf; a replacement that names the WRONG component is worse
# than no alarm at all.
#
# SO THE SLACK QUESTION IS ASKED OF SLACK'S OWN WORDS ONLY, two ways over:
#   * jarvis's own bus records are dropped from the lines first — by the structlog
#     logger name that wrote them, and by their 'nats_' event names;
#   * and what is left has to match one of slack-sdk's OWN sentences, quoted from
#     the Socket Mode client jarvis really runs (slack_sdk/socket_mode/aiohttp/
#     __init__.py lines 183, 188, 194, 205, 274, 305, 368, 415, 438 and
#     socket_mode/client.py line 60, at the version in jarvis's lock file),
#     rather than a general pattern of connection-sounding words.
#
# Either one of the two would have been enough for the bus restart; both are here
# because the cost of being wrong is a wrong word in the one alarm that reaches a
# person, and because an error string jarvis passes through from somewhere else
# can carry any words at all.
#
# THE SILENCE BACKSTOP IS UNCHANGED and still reads the WHOLE log: "has this
# container said anything at all" is a question about the container, not about
# Slack.
# ---------------------------------------------------------------------------
readonly BUS_CLIENTS_OWN_RECORDS='"logger":[[:space:]]*"[^"]*nats_client"|logger=[^[:space:]]*nats_client|"event":[[:space:]]*"nats_[a-z_]*"'
readonly SLACK_SESSION_ESTABLISHED='has been established'
readonly SLACK_SESSION_IN_TROUBLE='has been abandoned|seems to be (already closed|stale)|Received CLOSE event|Failed to (retrieve WSS URL|check the current session|send a ping message|send a message|receive or enqueue a message)|is no longer active'

# The retired alarm's OWN setting names, unchanged, so nothing new has to be
# configured and Rich's alarms keep arriving where they always did
# (jarvis/ops/systemd/serve_nats_watchdog.py).
readonly ENV_BOT_TOKEN="JARVIS_SLACK_BOT_TOKEN"
readonly ENV_CHANNEL_ID="JARVIS_SLACK_CHANNEL_ID"
readonly ENV_ALERT_CHANNEL="JARVIS_WATCHDOG_ALERT_CHANNEL_ID"

say()   { printf 'gateway watch: %s\n' "$*"; }
oops()  { printf 'gateway watch: %s\n' "$*" >&2; }

usage() {
    cat <<'USAGE'
gateway-watch.sh — is the Slack door carrying traffic? Reads three things and
reports each separately. Reads only; restarts nothing.

  --once                  look once and exit (the default)
  --every SECONDS         look, report, sleep, repeat — the fifteen-minute
                          cadence, kept in a container rather than a host timer
  --help                  this

TEST SEAMS — substitute an answer instead of asking for it, the way the retired
alarm's own --journal-file and --now-epoch did:
  --connz-file PATH       the bus's connz answer, from a file
  --log-file PATH         the gateway container's log lines, from a file
  --now EPOCH             what time it is, for a deterministic age

SETTINGS (all from the env file; the estate's compose file sets them):
  BUS_MONITORING_ADDRESS              where the bus answers, e.g. nats:8222
  JARVIS_NATS_USER                    the bus account the gateway connects as
  GATEWAY_WATCH_CLIENT_NAME           the name the gateway sends for itself
  GATEWAY_WATCH_SUBJECT               the subscription only it holds
  GATEWAY_WATCH_HEARTBEAT_PATH        the gateway's heartbeat file, read-only
  GATEWAY_WATCH_HEARTBEAT_MAX_AGE_S   older than this and the session is lost
  GATEWAY_WATCH_MAX_SILENCE_S         silent longer than this and it is stalled
  GATEWAY_WATCH_GATEWAY_SERVICE       which compose service the gateway is
  GATEWAY_WATCH_CONTAINER             or name its container outright
  GATEWAY_WATCH_DOCKER_SOCKET         the socket the container's log is read over
  GATEWAY_WATCH_NOTIFIER              'slack', or 'file' for a stand-in
  GATEWAY_WATCH_NOTIFIER_FILE         where the stand-in writes
  JARVIS_SLACK_BOT_TOKEN              the retired alarm's own names, unchanged
  JARVIS_WATCHDOG_ALERT_CHANNEL_ID
  JARVIS_SLACK_CHANNEL_ID
USAGE
}

# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------
MODE="once"
EVERY_SECONDS=""
CONNZ_FILE=""
LOG_FILE=""
NOW_OVERRIDE=""

while [ "$#" -gt 0 ]; do
    case "$1" in
        --once)        MODE="once"; shift ;;
        --every)       MODE="every"; EVERY_SECONDS="${2:-}"; shift 2 ;;
        --connz-file)  CONNZ_FILE="${2:-}"; shift 2 ;;
        --log-file)    LOG_FILE="${2:-}"; shift 2 ;;
        --now)         NOW_OVERRIDE="${2:-}"; shift 2 ;;
        --help|-h)     usage; exit "${EXIT_OK}" ;;
        *)             oops "I do not know the argument '$1'. --help says what I take."; exit "${EXIT_BROKEN}" ;;
    esac
done

if [ "${MODE}" = "every" ]; then
    case "${EVERY_SECONDS}" in
        ''|*[!0-9]*) oops "--every wants a whole number of seconds and was given '${EVERY_SECONDS}'."; exit "${EXIT_BROKEN}" ;;
    esac
    [ "${EVERY_SECONDS}" -gt 0 ] || { oops "--every wants more than nought seconds."; exit "${EXIT_BROKEN}"; }
fi

# ---------------------------------------------------------------------------
# Settings, each with the default its comment explains
# ---------------------------------------------------------------------------
BUS_ADDRESS="${BUS_MONITORING_ADDRESS:-}"
ACCOUNT="${JARVIS_NATS_USER:-}"
CLIENT_NAME="${GATEWAY_WATCH_CLIENT_NAME:-}"
SUBJECT="${GATEWAY_WATCH_SUBJECT:-${DEFAULT_SUBJECT}}"
HEARTBEAT_PATH="${GATEWAY_WATCH_HEARTBEAT_PATH:-${DEFAULT_HEARTBEAT_PATH}}"
HEARTBEAT_MAX_AGE_S="${GATEWAY_WATCH_HEARTBEAT_MAX_AGE_S:-${DEFAULT_HEARTBEAT_MAX_AGE_S}}"
MAX_SILENCE_S="${GATEWAY_WATCH_MAX_SILENCE_S:-${DEFAULT_MAX_SILENCE_S}}"
GATEWAY_SERVICE="${GATEWAY_WATCH_GATEWAY_SERVICE:-${DEFAULT_GATEWAY_SERVICE}}"
GATEWAY_CONTAINER="${GATEWAY_WATCH_CONTAINER:-}"
DOCKER_SOCKET="${GATEWAY_WATCH_DOCKER_SOCKET:-${DEFAULT_DOCKER_SOCKET}}"
NOTIFIER="${GATEWAY_WATCH_NOTIFIER:-slack}"
NOTIFIER_FILE="${GATEWAY_WATCH_NOTIFIER_FILE:-}"
LOG_LINES="${GATEWAY_WATCH_LOG_LINES:-${DEFAULT_LOG_LINES}}"

now_epoch() {
    if [ -n "${NOW_OVERRIDE}" ]; then printf '%s' "${NOW_OVERRIDE}"; else date -u +%s; fi
}

# An ISO-8601 instant to epoch seconds, done in jq rather than in date, because
# this runs on busybox date and the times carry fractions of a second and an
# offset spelt '+00:00'. Prints nothing when it cannot be read.
iso_to_epoch() {
    printf '%s' "$1" | jq -Rr '
        rtrimstr("\n")
        | sub("\\.[0-9]+"; "")
        | sub("\\+00:00$"; "Z")
        | try fromdateiso8601 catch empty
    ' 2>/dev/null
}

plural_seconds() {
    local seconds="$1"
    if [ -z "${seconds}" ]; then printf 'unknown'; else printf '%ss ago' "${seconds}"; fi
}

# ---------------------------------------------------------------------------
# COMPONENT ONE — the gateway's OWN connection on the bus.
#
# The bus's monitoring route, asked with authorisation AND subscriptions, from
# inside the estate. It takes no credential, which is why this can be asked at
# all without a password anywhere near it. Account names and client names are
# printed; no password is read and none is printable this way.
# ---------------------------------------------------------------------------
BUS_VERDICT=""    # ok | lost | unknown
BUS_SENTENCE=""
BUS_MATCHES=""
BUS_ACCOUNT_ONLY=""

read_the_bus() {
    if [ -n "${CONNZ_FILE}" ]; then
        cat "${CONNZ_FILE}" 2>/dev/null
        return
    fi
    curl -sf --max-time 8 "http://${BUS_ADDRESS}/connz?auth=1&subs=1" 2>/dev/null
}

check_the_bus() {
    local answer counts

    if [ -z "${ACCOUNT}" ]; then
        BUS_VERDICT="unknown"
        BUS_SENTENCE="the env file names no bus account for the gateway (JARVIS_NATS_USER), so the bus could not be asked whose connections these are."
        return
    fi
    if [ -z "${CLIENT_NAME}" ]; then
        BUS_VERDICT="unknown"
        BUS_SENTENCE="the env file names no client name for the gateway (GATEWAY_WATCH_CLIENT_NAME), and the account alone cannot tell the gateway from the front door: they share it. Asking on the account alone would call a stopped gateway healthy, so this refuses to guess."
        return
    fi
    if [ -z "${BUS_ADDRESS}" ] && [ -z "${CONNZ_FILE}" ]; then
        BUS_VERDICT="unknown"
        BUS_SENTENCE="the env file names no monitoring address for the bus (BUS_MONITORING_ADDRESS), so the bus could not be asked who is connected to it."
        return
    fi

    answer="$(read_the_bus)"
    if [ -z "${answer}" ]; then
        BUS_VERDICT="unknown"
        BUS_SENTENCE="the bus did not answer connz at ${BUS_ADDRESS:-${CONNZ_FILE}}, so who is connected to it could not be read at all. This is NOT the gateway being down: it is a bus this watch could not read."
        return
    fi

    # ALL THREE, in one pass, and the count of account-only matches beside it so
    # the sentence can say what was there instead.
    counts="$(printf '%s' "${answer}" | jq -r \
        --arg account "${ACCOUNT}" \
        --arg name "${CLIENT_NAME}" \
        --arg subject "${SUBJECT}" '
        def subs: (.subscriptions_list // ([(.subscriptions_list_detail // [])[] | .subject]));
        (.connections // null) as $c
        | if $c == null then "unparseable" else
            [ $c[] | select(.authorized_user == $account) ] as $mine
            | [ $mine[] | select((.name // "") == $name and (subs | index($subject))) ] as $gateway
            | [ $mine[] | select((.name // "") != "") ] as $named
            | "\($gateway | length) \($mine | length) \($named | length)"
          end
    ' 2>/dev/null)"

    if [ -z "${counts}" ] || [ "${counts}" = "unparseable" ]; then
        BUS_VERDICT="unknown"
        BUS_SENTENCE="the bus answered at ${BUS_ADDRESS:-${CONNZ_FILE}} and the answer was not the list of connections this expects, so it could not be read. An answer this watch cannot read is not agreement."
        return
    fi

    set -- ${counts}
    BUS_MATCHES="${1:-0}"
    BUS_ACCOUNT_ONLY="${2:-0}"
    local with_a_name="${3:-0}"

    if [ "${BUS_MATCHES}" -gt 0 ]; then
        BUS_VERDICT="ok"
        BUS_SENTENCE="the bus holds ${BUS_MATCHES} connection(s) that are the gateway's own — the '${ACCOUNT}' account, the client name '${CLIENT_NAME}', and the subscription ${SUBJECT}. All three, because the account alone is shared with the front door."
    elif [ "${with_a_name}" -eq 0 ] && [ "${BUS_ACCOUNT_ONLY}" -gt 0 ]; then
        # A GATEWAY FROM A RELEASE BEFORE 26 SEPTEMBER 2026 sends no name at all,
        # so the bus reports an empty one and this cannot find it. That is not the
        # gateway being down, and calling it down would alarm Rich about a door
        # that is working. It is unknown — which is still never healthy — and the
        # estate's own item 8i says exactly the same thing in the same case.
        BUS_VERDICT="unknown"
        BUS_SENTENCE="the bus holds ${BUS_ACCOUNT_ONLY} connection(s) on the '${ACCOUNT}' account and NONE of them sends a client name, which is what a jarvis image built before 26 September 2026 does. So which of them is the gateway cannot be told from here, and the account alone would report a stopped gateway as healthy. A release carrying the client name is what makes this answerable."
    else
        # The account-only count is spelt out because it is the whole point: a
        # check that stopped at the account would have called this healthy.
        local also
        if [ "${BUS_ACCOUNT_ONLY}" -gt 0 ]; then
            also="It does hold ${BUS_ACCOUNT_ONLY} other connection(s) on that same account — the front door shares it — and those are not the gateway, which is why the account on its own is not asked."
        else
            also="Nothing at all is connected on that account."
        fi
        BUS_VERDICT="lost"
        BUS_SENTENCE="the bus at ${BUS_ADDRESS:-${CONNZ_FILE}} holds NO connection that is the gateway's own: nothing there carries the client name '${CLIENT_NAME}' with the subscription ${SUBJECT} on the '${ACCOUNT}' account. ${also} Slack traffic has no way onto the bus and the factory's answers have no way back."
    fi
}

# ---------------------------------------------------------------------------
# COMPONENT TWO — what the gateway says about its Slack session.
#
# The gateway writes one small file (JARVIS_SLACK_HEARTBEAT_PATH), rewritten on
# every connect, disconnect, inbound envelope, session close and transport
# error. This mounts that volume read-only. A file and not a route, because the
# gateway publishes no port on purpose.
# ---------------------------------------------------------------------------
SLACK_VERDICT=""
SLACK_SENTENCE=""

check_slack() {
    local body state last_event_at last_epoch age now

    if [ ! -e "${HEARTBEAT_PATH}" ]; then
        SLACK_VERDICT="unknown"
        SLACK_SENTENCE="the gateway has written no heartbeat at ${HEARTBEAT_PATH}, so how its Slack session is cannot be known from here. An absent file is unknown and never healthy: either the gateway has not reached its Slack start-up, or it was not given the setting, or this watch is not mounting the volume it writes."
        return
    fi
    body="$(cat "${HEARTBEAT_PATH}" 2>/dev/null)"
    if [ -z "${body}" ]; then
        SLACK_VERDICT="unknown"
        SLACK_SENTENCE="the gateway's heartbeat at ${HEARTBEAT_PATH} could not be read, so how its Slack session is cannot be known from here."
        return
    fi
    state="$(printf '%s' "${body}" | jq -r '.state // empty' 2>/dev/null)"
    last_event_at="$(printf '%s' "${body}" | jq -r '.last_event_at // empty' 2>/dev/null)"
    if [ -z "${state}" ] || [ -z "${last_event_at}" ]; then
        SLACK_VERDICT="unknown"
        SLACK_SENTENCE="the gateway's heartbeat at ${HEARTBEAT_PATH} is not in the shape this expects (a state and the time of its last event), so it could not be read."
        return
    fi

    last_epoch="$(iso_to_epoch "${last_event_at}")"
    if [ -z "${last_epoch}" ]; then
        SLACK_VERDICT="unknown"
        SLACK_SENTENCE="the gateway's heartbeat at ${HEARTBEAT_PATH} says its last event was at '${last_event_at}', which is not a time this could read."
        return
    fi
    now="$(now_epoch)"
    age=$(( now - last_epoch ))
    [ "${age}" -ge 0 ] || age=0

    if [ "${state}" = "disconnected" ]; then
        SLACK_VERDICT="lost"
        SLACK_SENTENCE="the gateway says its Slack Socket Mode session is DISCONNECTED, as of $(plural_seconds "${age}"). Its bus connection may be perfectly fine and Slack requests will still go unanswered."
        return
    fi
    if [ "${age}" -gt "${HEARTBEAT_MAX_AGE_S}" ]; then
        SLACK_VERDICT="lost"
        SLACK_SENTENCE="the gateway last said anything about its Slack session $(plural_seconds "${age}"), which is longer than the ${HEARTBEAT_MAX_AGE_S}s allowed — longer than a full Socket Mode rotation, and a rotation rewrites that file. It says '${state}', and it has stopped saying it."
        return
    fi
    if [ "${state}" = "connecting" ]; then
        SLACK_VERDICT="lost"
        SLACK_SENTENCE="the gateway says it is still CONNECTING to Slack, as of $(plural_seconds "${age}") — it has not got a session, so nothing from Slack is reaching the factory."
        return
    fi

    # THE RETIRED ALARM'S SECOND SIGNAL, and the case the heartbeat cannot cover
    # on its own: the library reconnects before it runs the listeners that write
    # that file, so a reconnect that THROWS leaves the file saying 'connected'
    # and never touched again. The log says what happened. Three things have to
    # hold before this speaks, so that a rotation and an old recovered blip stay
    # quiet: there is a trouble line, nothing established after it, and it is
    # newer than the last thing the gateway itself said.
    #
    # AND THE TROUBLE LINE HAS TO BE SLACK'S OWN (the 26 September 2026 fix). The
    # three conditions below do NOT make a bus event safe: jarvis's bus client
    # writes its drops into this same log, and on an idle door the heartbeat only
    # moves every five hours, so a bus blip cleared the third condition easily and
    # was reported here as a lost Slack session. Which lines count is decided in
    # check_activity, against slack-sdk's own sentences only.
    if [ -n "${LOG_TROUBLE_AT}" ] \
       && { [ -z "${LOG_HEALTHY_AT}" ] || [ "${LOG_TROUBLE_AT}" -gt "${LOG_HEALTHY_AT}" ]; } \
       && [ "${LOG_TROUBLE_AT}" -gt "${last_epoch}" ]; then
        SLACK_VERDICT="lost"
        SLACK_SENTENCE="the gateway's own log shows its Slack session in trouble $(( $(now_epoch) - LOG_TROUBLE_AT ))s ago with NO 'session established' after it, and that is newer than the last thing the gateway itself said about Slack (it still says '${state}', from $(plural_seconds "${age}")). A healthy session rotates roughly every five hours and always logs its recovery immediately, so a trouble line with nothing after it is a drop that has not come back."
        return
    fi

    SLACK_VERDICT="ok"
    SLACK_SENTENCE="the gateway says its Slack Socket Mode session is ${state}, and it said so $(plural_seconds "${age}") (within the ${HEARTBEAT_MAX_AGE_S}s allowed). Its log shows no unrecovered drop after that."
}

# ---------------------------------------------------------------------------
# COMPONENT THREE — has the gateway gone silent?
#
# The retired alarm's stalled-log check, kept, against the CONTAINER's log
# rather than a journal, because the gateway is a container now. Its whole
# reason is in the retired script: a healthy IDLE door is not silent, and the
# Socket Mode session rotates roughly every five hours and logs when it does, so
# a door that has logged nothing for longer than a full rotation is wedged even
# while the process lingers.
#
# HOW A CONTAINER'S LOG IS READ. Over the Docker socket. The mount is read_only,
# which guards the socket FILE and not requests through it: this is engine
# control, a real privilege named here rather than hidden: reading a
# container's log means asking the engine, and there is no other way to ask. A
# machine that will not grant it gets 'unknown' — never a false 'ok'.
# ---------------------------------------------------------------------------
ACTIVITY_VERDICT=""
ACTIVITY_SENTENCE=""
# The retired alarm's SECOND log signal, and why it is here (found 26 September
# 2026, building E3-j2 against the real client library):
#
# the gateway's heartbeat is written from the library's own lifecycle listeners,
# and on a CLOSE the library RECONNECTS FIRST and only then runs those listeners
# — so when the reconnect itself throws, which is what an unrecovered Slack drop
# looks like, the listeners never run and the file is simply never rewritten
# again. The freshness window would still catch it, but only after six hours,
# whereas the retired alarm caught it at the next quarter of an hour by reading
# the SHAPE of the connection lifecycle in the log: a trouble line with no
# 'session established' after it. Dropping that would have been a real loss of
# cover, so it is kept, from the same log this already reads.
#
# IT IS PRECISE ON PURPOSE. A healthy session rotates roughly every five hours
# and ALWAYS logs its trouble line immediately followed by an established line,
# so the newest-first comparison never fires on a rotation. And it only speaks
# when the trouble is NEWER than the last thing the gateway itself said about
# Slack — otherwise a line the gateway has already recovered from, or somebody
# else's word "disconnect" in the same log, would be read as a live failure.
LOG_TROUBLE_AT=""     # epoch of the newest connection-trouble line, if any
LOG_HEALTHY_AT=""     # epoch of the newest 'session established' line, if any

docker_api() {
    curl -sf --max-time 8 --unix-socket "${DOCKER_SOCKET}" "http://localhost/$1" 2>/dev/null
}

# WHICH CONTAINER IS THE GATEWAY. Named outright if the env file says so;
# otherwise found by its compose labels, with the project taken from THIS
# container's own labels — so the watch finds the gateway of its own estate and
# not of somebody else's on the same machine.
find_the_gateway() {
    local me project answer
    if [ -n "${GATEWAY_CONTAINER}" ]; then printf '%s' "${GATEWAY_CONTAINER}"; return; fi
    me="$(cat /etc/hostname 2>/dev/null)"
    [ -n "${me}" ] || return 1
    project="$(docker_api "containers/${me}/json" \
        | jq -r '.Config.Labels["com.docker.compose.project"] // empty' 2>/dev/null)"
    [ -n "${project}" ] || return 1
    # 'all=true' MATTERS: the engine lists only RUNNING containers by default, so
    # without it a STOPPED gateway — the case this watch exists for — could not be
    # found and its log would be reported as unreadable rather than as silent.
    # Found by stopping the gateway on a rehearsal estate, 26 September 2026.
    answer="$(curl -sfG --max-time 8 --unix-socket "${DOCKER_SOCKET}" \
        "http://localhost/containers/json" \
        --data-urlencode "all=true" \
        --data-urlencode "filters={\"label\":[\"com.docker.compose.project=${project}\",\"com.docker.compose.service=${GATEWAY_SERVICE}\"]}" \
        2>/dev/null)"
    printf '%s' "${answer}" | jq -r '.[0].Id // empty' 2>/dev/null
}

read_the_log() {
    local id
    if [ -n "${LOG_FILE}" ]; then cat "${LOG_FILE}" 2>/dev/null; return; fi
    if [ ! -S "${DOCKER_SOCKET}" ]; then return 1; fi
    id="$(find_the_gateway)"
    [ -n "${id}" ] || return 1
    # THE ENGINE RETURNS A FRAMED STREAM, not plain lines: every line is preceded
    # by an eight-byte header that says which stream it came from and how long it
    # is, and those headers contain null bytes. Only the times and the words are
    # wanted here, so the nulls are dropped as the answer is read — otherwise the
    # shell drops them itself and says so on every single run.
    docker_api "containers/${id}/logs?stdout=1&stderr=1&timestamps=1&tail=${LOG_LINES}" \
        | tr -d '\000'
}

# The newest line in the log that matches a pattern, as epoch seconds. Prints
# nothing when no line matches.
_newest_matching() {
    local lines="$1" pattern="$2" line stamp
    line="$(printf '%s\n' "${lines}" | grep -iE "${pattern}" | tail -1)"
    [ -n "${line}" ] || return 0
    stamp="$(printf '%s' "${line}" \
        | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})' \
        | head -1)"
    [ -n "${stamp}" ] || return 0
    iso_to_epoch "${stamp}"
}

check_activity() {
    local lines slack_lines newest newest_epoch age now
    lines="$(read_the_log)"
    if [ -z "${lines}" ]; then
        ACTIVITY_VERDICT="unknown"
        ACTIVITY_SENTENCE="the gateway container's log could not be read (over ${DOCKER_SOCKET}), so whether it has gone silent is not known. An unreadable log is unknown and never healthy."
        return
    fi
    # Each line the engine returns starts with an RFC 3339 time. The stream is
    # framed, so the frame headers are stepped over by looking for the times
    # rather than by splitting on columns.
    newest="$(printf '%s' "${lines}" \
        | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})' \
        | tail -1)"
    if [ -z "${newest}" ]; then
        ACTIVITY_VERDICT="unknown"
        ACTIVITY_SENTENCE="the gateway container's log was read and carried no line with a time in it, so how long ago it last said anything is not known."
        return
    fi
    newest_epoch="$(iso_to_epoch "${newest}")"
    if [ -z "${newest_epoch}" ]; then
        ACTIVITY_VERDICT="unknown"
        ACTIVITY_SENTENCE="the gateway container's newest log line is timed '${newest}', which is not a time this could read."
        return
    fi
    # The two connection markers for the SLACK question, and they are asked of
    # the Slack-only view of these same lines — jarvis's own bus records taken
    # out, and only slack-sdk's own sentences counted as trouble. A bus restart
    # is not a Slack failure, and until 26 September 2026 this read it as one.
    # See BUS_CLIENTS_OWN_RECORDS at the top for what was measured.
    slack_lines="$(printf '%s\n' "${lines}" | grep -vE "${BUS_CLIENTS_OWN_RECORDS}")"
    LOG_HEALTHY_AT="$(_newest_matching "${slack_lines}" "${SLACK_SESSION_ESTABLISHED}")"
    LOG_TROUBLE_AT="$(_newest_matching "${slack_lines}" "${SLACK_SESSION_IN_TROUBLE}")"
    now="$(now_epoch)"
    age=$(( now - newest_epoch ))
    [ "${age}" -ge 0 ] || age=0
    if [ "${age}" -gt "${MAX_SILENCE_S}" ]; then
        ACTIVITY_VERDICT="stalled"
        ACTIVITY_SENTENCE="the gateway container has logged nothing for ${age}s, which is longer than the ${MAX_SILENCE_S}s backstop — longer than a full Socket Mode rotation, which logs as it happens. A door that has said nothing for that long is wedged even though its process is still there."
    else
        ACTIVITY_VERDICT="ok"
        ACTIVITY_SENTENCE="the gateway container's newest log line is $(plural_seconds "${age}") (within the ${MAX_SILENCE_S}s backstop), so it is still saying things."
    fi
}

# ---------------------------------------------------------------------------
# HOW IT TELLS ANYBODY — the retired alarm's own notifier, its own setting
# names, and a stand-in that writes a file for rehearsal. No rehearsal ever
# posts to the real workspace.
# ---------------------------------------------------------------------------
tell_somebody() {
    local text="$1" token channel body response
    case "${NOTIFIER}" in
        file)
            if [ -z "${NOTIFIER_FILE}" ]; then
                oops "GATEWAY_WATCH_NOTIFIER says 'file' and GATEWAY_WATCH_NOTIFIER_FILE names no file, so this message has nowhere to go."
                return 1
            fi
            # ONE LINE PER MESSAGE (jq -c), so a test can count them: exactly one
            # message per unhappy run is part of what this has to prove.
            printf '%s\n' "${text}" | jq -cRs --arg at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
                '{at: $at, text: .}' >> "${NOTIFIER_FILE}" || return 1
            say "the message was written to the stand-in notifier at ${NOTIFIER_FILE}. Nothing was sent to Slack."
            return 0
            ;;
        slack) ;;
        *)
            oops "GATEWAY_WATCH_NOTIFIER says '${NOTIFIER}' and it has to say 'slack' or 'file'."
            return 1
            ;;
    esac

    token="$(printenv "${ENV_BOT_TOKEN}" 2>/dev/null)"
    channel="$(printenv "${ENV_ALERT_CHANNEL}" 2>/dev/null)"
    [ -n "${channel}" ] || channel="$(printenv "${ENV_CHANNEL_ID}" 2>/dev/null)"
    if [ -z "${token}" ]; then
        oops "the door needs telling about and ${ENV_BOT_TOKEN} has no value, so the message could not be sent. The value is not printed here and never is."
        return 1
    fi
    if [ -z "${channel}" ]; then
        oops "the door needs telling about and no channel is named (${ENV_ALERT_CHANNEL} or ${ENV_CHANNEL_ID}), so the message could not be sent."
        return 1
    fi
    body="$(printf '%s' "${text}" | jq -Rs --arg channel "${channel}" \
        '{channel: $channel, text: ., mrkdwn: false}')"
    response="$(curl -s --max-time 15 -X POST "${SLACK_POST_URL}" \
        -H "Authorization: Bearer ${token}" \
        -H 'Content-Type: application/json; charset=utf-8' \
        --data-binary "${body}" 2>/dev/null)"
    if [ "$(printf '%s' "${response}" | jq -r '.ok // false' 2>/dev/null)" != "true" ]; then
        # The response's own words, never the token. An alarm that did not land
        # is a failure of this watch, not a quiet success.
        oops "the message was not accepted: $(printf '%s' "${response}" | jq -r '.error // "no answer at all"' 2>/dev/null)"
        return 1
    fi
    say "the message was sent."
    return 0
}

# ---------------------------------------------------------------------------
# THE VERDICT. Unhappy if ANY component is lost or stalled — and also if any is
# unknown, because an unknown that tells nobody is a component nobody is
# watching, and the whole reason this exists is that a stopped door once looked
# healthy. The sentence always says WHICH component and never "a unit is
# inactive".
# ---------------------------------------------------------------------------
build_the_message() {
    local headline="$1"
    cat <<MESSAGE
${headline}

bus connection   ${BUS_VERDICT}
   ${BUS_SENTENCE}

Slack session    ${SLACK_VERDICT}
   ${SLACK_SENTENCE}

recent activity  ${ACTIVITY_VERDICT}
   ${ACTIVITY_SENTENCE}

What this means: the Slack door is how Rich's approvals, build taps and merge
words reach the factory, and how its answers come back. A component above that
is not ok means some of that is not getting through. Nothing has been restarted
and nothing has been changed by this watch: it only reads and tells.
MESSAGE
}

look_once() {
    local unhappy=0 headline

    BUS_VERDICT=""; SLACK_VERDICT=""; ACTIVITY_VERDICT=""
    LOG_TROUBLE_AT=""; LOG_HEALTHY_AT=""
    check_the_bus
    # The log is read BEFORE the Slack question, because the Slack question uses
    # what the log says as well as what the heartbeat says.
    check_activity
    check_slack

    say "bus connection   ${BUS_VERDICT} — ${BUS_SENTENCE}"
    say "Slack session    ${SLACK_VERDICT} — ${SLACK_SENTENCE}"
    say "recent activity  ${ACTIVITY_VERDICT} — ${ACTIVITY_SENTENCE}"

    case "${BUS_VERDICT}:${SLACK_VERDICT}:${ACTIVITY_VERDICT}" in
        ok:ok:ok) ;;
        *) unhappy=1 ;;
    esac

    if [ "${unhappy}" -eq 0 ]; then
        say "every component ok. Nobody was told anything, which is the point."
        return "${EXIT_OK}"
    fi

    case "${BUS_VERDICT}" in lost) headline="The factory's Slack door is not on the bus." ;; esac
    if [ -z "${headline:-}" ] && [ "${SLACK_VERDICT}" = "lost" ]; then
        headline="The factory's Slack door has lost its Slack session."
    fi
    if [ -z "${headline:-}" ] && [ "${ACTIVITY_VERDICT}" = "stalled" ]; then
        headline="The factory's Slack door has gone silent."
    fi
    [ -n "${headline:-}" ] || headline="The factory's Slack door cannot be checked, so whether it is working is unknown."

    if tell_somebody "$(build_the_message "${headline}")"; then
        return "${EXIT_UNHAPPY}"
    fi
    oops "the door needs telling about and the message could not be delivered."
    return "${EXIT_BROKEN}"
}

if [ "${MODE}" = "once" ]; then
    look_once
    exit "$?"
fi

# THE FIFTEEN-MINUTE CADENCE, IN A CONTAINER. A compose file has no timer, and a
# host unit is the machine-shaped thing this whole rollout exists to remove, so
# the cadence is a loop in this container with 'restart: unless-stopped' behind
# it. Neither an unhappy look nor a broken one stops the loop: it says so and
# looks again, because a watch that exits on the first bad news stops watching.
say "looking every ${EVERY_SECONDS}s. Each look is reported in full below."
while true; do
    look_once || say "that look ended $? — carrying on."
    sleep "${EVERY_SECONDS}"
done

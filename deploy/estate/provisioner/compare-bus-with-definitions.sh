#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# DOES THIS BUS ALREADY HOLD WHAT THE PINNED DEFINITIONS SAY IT SHOULD?
#
# WHY THIS EXISTS (26 September 2026, build item E1 of the rollout design).
# When the estate is started against a bus that is ALREADY RUNNING — the live
# one, kept because it holds every reader's position and every message still
# waiting — nothing in the estate may write to it. But something has to say
# whether that bus is the bus this release expects, because a coordinator
# started against a bus whose PIPELINE stream has a different retention will
# behave differently and nothing will say so.
#
# SO THIS READS, FIELD BY FIELD, AND NEVER WRITES. Two things are compared:
#
#   what the bus says   its own monitoring route, 'jsz' with the streams and
#                       their configuration asked for. It takes NO credential,
#                       which is why this can run from anywhere on the bus's
#                       network and why no password is anywhere near it
#   what is pinned      the bus repository's own definitions, at the commit the
#                       release pins, from the read-only volume the build step
#                       fills: streams/stream-definitions.json and
#                       kv/kv-definitions.json
#
# WHAT IT IS NOT. It is not the provisioning scripts' preview mode. For a
# stream or bucket that already exists those print "Would check/update" and
# return before comparing a single field (streams/provision-streams.sh:146-152,
# kv/provision-kv.sh:134-141), so a clean preview is not evidence of a matching
# bus and its wording is not evidence of a mismatched one. This compares the
# fields.
#
# THREE ANSWERS, AND THEY ARE NOT THE SAME ANSWER:
#
#   0   every stream and bucket the definitions name is on the bus and every
#       field the definitions name agrees
#   2   REFUSED — something the definitions name is missing, or a field differs.
#       The stream or bucket, the field, what was wanted and what was found are
#       all printed. Nothing is updated: a difference between a running bus and
#       the pinned definitions is settled before a rollout, not during one
#   3   UNKNOWN — the route could not be read, or its answer could not be
#       parsed, or a field the definitions name is not in the answer at all.
#       Printed as "could not be read", never as agreement and never as nought
#
# HOW THE TWO SIDES ARE SPELT DIFFERENTLY, which is the whole reason this is a
# script and not a diff. The definitions are written the way the 'nats' command
# line takes them and the bus answers in its own units:
#
#   retention   "work" in the definitions, "workqueue" on the bus
#   max_age     "7d" in the definitions, 604800000000000 (nanoseconds) on the bus
#   replicas    "replicas" in the definitions, "num_replicas" on the bus
#   a bucket    is a stream called KV_<bucket> on the bus, and its own fields
#               are stream fields: history is max_msgs_per_subject, ttl is
#               max_age, max_value_size is max_msg_size
#   sizes       "256KB" in the definitions, 262144 on the bus (1KB = 1024)
#   counts      max_msgs, and a bucket's history, are numbers of MESSAGES and are
#               read as plain numbers only. "10K" messages is not 10240 and is
#               not guessed at: it is reported as unreadable (26 September 2026)
#   subjects    two lists, compared as a SET. The same two subjects in the other
#               order is the same stream, and was called a difference until
#               26 September 2026
#
# ONLY THE FIELDS THE DEFINITIONS NAME ARE COMPARED. A field the bus reports
# and the definitions do not pin — max_bytes, discard, duplicate_window on
# today's definitions — has nothing to be compared against, and inventing an
# expectation for it here would be this repository pinning the bus's storage,
# which belongs to the bus's repository. If a definition gains one of those
# fields it is compared from then on, with no change here.
#
#   compare-bus-with-definitions.sh --monitoring-address nats:8222 [--definitions /bus]
#   compare-bus-with-definitions.sh --jsz-file answer.json --definitions ./fixture
#
# --jsz-file is how the tests feed it a saved answer: the same comparison, with
# nothing asked over a network.
# ---------------------------------------------------------------------------
set -uo pipefail

ADDRESS=""
JSZ_FILE=""
DEFINITIONS="/bus"
TIMEOUT=10
QUERY='jsz?accounts=true&streams=true&consumers=true&config=true'

usage() { sed -n '2,72p' "${BASH_SOURCE[0]}"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --monitoring-address) ADDRESS="${2:?--monitoring-address needs host:port}"; shift 2 ;;
        --jsz-file)           JSZ_FILE="${2:?--jsz-file needs a file}"; shift 2 ;;
        --definitions)        DEFINITIONS="${2:?--definitions needs the folder holding streams/ and kv/}"; shift 2 ;;
        --timeout)            TIMEOUT="${2:?--timeout needs seconds}"; shift 2 ;;
        --help|-h)            usage; exit 0 ;;
        *) printf 'compare-bus-with-definitions: unknown argument "%s". Try --help.\n' "$1" >&2; exit 4 ;;
    esac
done

say()      { printf '%s\n' "$*"; }
unknown()  { printf 'could not be read: %s\n' "$*" >&2; exit 3; }
refuse()   { printf 'REFUSED: %s\n' "$*" >&2; exit 2; }

STREAM_DEFS="${DEFINITIONS}/streams/stream-definitions.json"
KV_DEFS="${DEFINITIONS}/kv/kv-definitions.json"

command -v jq >/dev/null 2>&1 || unknown "jq is not in this image, and the bus's answer is JSON."
[ -f "${STREAM_DEFS}" ] || unknown "the pinned stream definitions are not at ${STREAM_DEFS}. This is the read-only volume the release's build step fills from the bus repository at its pinned commit."
[ -f "${KV_DEFS}" ]     || unknown "the pinned key-value definitions are not at ${KV_DEFS}."

# ---------------------------------------------------------------------------
# WHAT THE BUS SAYS. Read once, kept, and compared twice if the caller asks
# twice — so "before" and "after" are read exactly the same way.
# ---------------------------------------------------------------------------
ANSWER=""
if [ -n "${JSZ_FILE}" ]; then
    [ -f "${JSZ_FILE}" ] || unknown "there is no saved answer at ${JSZ_FILE}."
    ANSWER="$(cat "${JSZ_FILE}")"
    say "Read a saved answer from ${JSZ_FILE} (nothing was asked over a network)."
elif [ -n "${ADDRESS}" ]; then
    if command -v curl >/dev/null 2>&1; then
        ANSWER="$(curl -sf --max-time "${TIMEOUT}" "http://${ADDRESS}/${QUERY}" 2>/dev/null)"
    else
        ANSWER="$(wget -qO- --timeout="${TIMEOUT}" "http://${ADDRESS}/${QUERY}" 2>/dev/null)"
    fi
    [ -n "${ANSWER}" ] || unknown "the bus's monitoring route at http://${ADDRESS}/${QUERY} did not answer. Nothing was compared, and this is NOT agreement: it is a bus this check could not read."
    say "Asked the bus at http://${ADDRESS}/${QUERY} — no credential is used or needed on that route."
else
    printf 'compare-bus-with-definitions: give --monitoring-address or --jsz-file. Try --help.\n' >&2
    exit 4
fi

printf '%s' "${ANSWER}" | jq -e 'type == "object"' >/dev/null 2>&1 \
    || unknown "the bus's answer could not be parsed as JSON. Nothing was compared."

# Some builds of the route answer without the per-account detail unless it is
# asked for. If the detail is not there at all, that is unknown and not "no
# streams" — the distinction the estate's own check had to learn twice.
printf '%s' "${ANSWER}" | jq -e '(.account_details | type) == "array"' >/dev/null 2>&1 \
    || unknown "the bus's answer carries no per-account stream detail, so no stream's configuration could be read. The option spelling this asks for is '${QUERY}'; check it against this bus's own version."

printf '%s' "${ANSWER}" | jq -e 'any(.account_details[]?; (.stream_detail | type) == "array")' >/dev/null 2>&1 \
    || unknown "the bus's answer carries account detail but no stream detail, so no stream's configuration could be read. Ask the route with streams and config both."

printf '%s' "${ANSWER}" | jq -e 'any(.account_details[]?.stream_detail[]?; (.config | type) == "object")' >/dev/null 2>&1 \
    || unknown "the bus's answer carries streams but not their configuration, so nothing could be compared field by field. This route needs the configuration asked for by name: '${QUERY}'."

# ---------------------------------------------------------------------------
# THE COMPARISON. One jq program, because every difference between the two
# spellings is a conversion and conversions belong beside the fields they
# convert. It prints one line per resource and per difference:
#
#   OK|<resource>|<how many fields agreed>
#   MISSING|<resource>|<what the definitions call it>
#   ABSENT_FIELD|<resource>|<field>|<wanted>
#   MISMATCH|<resource>|<field>|<wanted>|<found>
# ---------------------------------------------------------------------------
REPORT="$(printf '%s' "${ANSWER}" | jq -r \
    --slurpfile stream_defs "${STREAM_DEFS}" \
    --slurpfile kv_defs "${KV_DEFS}" '
    # --- the bus s own spellings, as conversions ---------------------------
    def to_ns:
        if . == null or . == "" or . == "null" then 0
        elif (. | type) == "number" then .
        elif test("^-?[0-9]+$") then (tonumber)
        elif test("^[0-9]+(\\.[0-9]+)?ns$") then ((.[:-2] | tonumber))
        elif test("^[0-9]+(\\.[0-9]+)?us$") then ((.[:-2] | tonumber) * 1000)
        elif test("^[0-9]+(\\.[0-9]+)?ms$") then ((.[:-2] | tonumber) * 1000000)
        elif test("^[0-9]+(\\.[0-9]+)?s$")  then ((.[:-1] | tonumber) * 1000000000)
        elif test("^[0-9]+(\\.[0-9]+)?m$")  then ((.[:-1] | tonumber) * 60000000000)
        elif test("^[0-9]+(\\.[0-9]+)?h$")  then ((.[:-1] | tonumber) * 3600000000000)
        elif test("^[0-9]+(\\.[0-9]+)?d$")  then ((.[:-1] | tonumber) * 86400000000000)
        elif test("^[0-9]+(\\.[0-9]+)?w$")  then ((.[:-1] | tonumber) * 604800000000000)
        else "UNREADABLE-DURATION:\(.)" end;

    # A COUNT IS NOT A SIZE (26 September 2026, the review of this script).
    # max_msgs is a number of MESSAGES, and putting it through the size
    # converter would read a definition written "10K" messages as 10240. So a
    # count is a plain number and nothing else: anything carrying a unit is
    # UNREADABLE here and leaves by the unknown door, rather than being quietly
    # multiplied by 1024. The definitions of today write it as a plain number.
    def to_count:
        if . == null or . == "" or . == "null" then -1
        elif (. | type) == "number" then .
        elif test("^-?[0-9]+$") then (tonumber)
        else "UNREADABLE-COUNT:\(.)" end;

    def to_bytes:
        if . == null or . == "" or . == "null" then -1
        elif (. | type) == "number" then .
        elif test("^-?[0-9]+$") then (tonumber)
        elif test("^[0-9]+(\\.[0-9]+)?[Bb]$")   then ((.[:-1] | tonumber))
        elif test("^[0-9]+(\\.[0-9]+)?[Kk][Bb]$") then ((.[:-2] | tonumber) * 1024)
        elif test("^[0-9]+(\\.[0-9]+)?[Mm][Bb]$") then ((.[:-2] | tonumber) * 1048576)
        elif test("^[0-9]+(\\.[0-9]+)?[Gg][Bb]$") then ((.[:-2] | tonumber) * 1073741824)
        elif test("^[0-9]+(\\.[0-9]+)?[Kk]$")   then ((.[:-1] | tonumber) * 1024)
        elif test("^[0-9]+(\\.[0-9]+)?[Mm]$")   then ((.[:-1] | tonumber) * 1048576)
        elif test("^[0-9]+(\\.[0-9]+)?[Gg]$")   then ((.[:-1] | tonumber) * 1073741824)
        else "UNREADABLE-SIZE:\(.)" end;

    # "work" is what the nats command line takes; "workqueue" is what the bus
    # answers. Anything else is passed through and compared as it is.
    def retention_as_the_bus_says_it:
        if . == null then null
        elif . == "work" or . == "workqueue" then "workqueue"
        elif . == "limits" then "limits"
        elif . == "interest" then "interest"
        else . end;

    # Every stream the bus holds, keyed by name, across every account.
    ([.account_details[]? | .stream_detail[]? | select(.config != null) | {key: .config.name, value: .config}] | from_entries) as $held

    # A LIST IS A SET HERE, NOT AN ORDER (26 September 2026, the review of this
    # script). The subjects of a stream are two JSON arrays on the two sides, and
    # comparing them with == called a bus that answered the same two subjects in
    # the other order a MISMATCH. Nothing on either bus read here ever did that,
    # and it erred towards refusing rather than towards agreeing, but it is
    # still wrong: a field marked sorted is compared as a set, and both sides
    # are still PRINTED as they are written.
    #
    # One comparison: a resource name, and a list of {on, want, sorted?}.
    | def as_a_set($sorted; $v): if $v == null or ($sorted | not) then $v else ($v | sort) end;
      def compare($resource; $wanted_fields):
        if ($held[$resource] == null)
        then ["MISSING|\($resource)"]
        else ($held[$resource]) as $found
        | ([ $wanted_fields[]
             | . as $f
             | ($f.sorted // false) as $set
             | ($found[$f.on]) as $got
             | if $got == null then "ABSENT_FIELD|\($resource)|\($f.on)|\($f.want | tostring)"
               elif (as_a_set($set; $got) == as_a_set($set; $f.want)) then empty
               else "MISMATCH|\($resource)|\($f.on)|\($f.want | tostring)|\($got | tostring)"
               end ]) as $problems
        | if ($problems | length) == 0
          then ["OK|\($resource)|\($wanted_fields | length)"]
          else $problems end
        end;

    # --- the streams the definitions name --------------------------------
    ([ $stream_defs[0].streams[]?
       | . as $d
       | compare($d.name;
           ([ {on: "subjects",     want: ($d.subjects), sorted: true} ]
            + (if $d.retention == null then [] else [{on: "retention", want: ($d.retention | retention_as_the_bus_says_it)}] end)
            + (if ($d | has("max_age"))  then [{on: "max_age",  want: ($d.max_age  | to_ns)}]    else [] end)
            + (if ($d | has("max_msgs")) then [{on: "max_msgs", want: ($d.max_msgs | to_count)}] else [] end)
            + (if ($d | has("max_bytes")) then [{on: "max_bytes", want: ($d.max_bytes | to_bytes)}] else [] end)
            + (if ($d | has("storage"))  then [{on: "storage",  want: ($d.storage)}]  else [] end)
            + (if ($d | has("replicas")) then [{on: "num_replicas", want: ($d.replicas)}] else [] end)
            + (if ($d | has("discard"))  then [{on: "discard",  want: ($d.discard)}]  else [] end)
            + (if ($d | has("duplicate_window")) then [{on: "duplicate_window", want: ($d.duplicate_window | to_ns)}] else [] end)
           )
         )
     ] | flatten)

    # --- the buckets the definitions name, each one a KV_<name> stream ----
    + ([ $kv_defs[0].kv_buckets[]?
         | . as $d
         | compare("KV_\($d.name)";
             ([ ]
              + (if ($d | has("ttl"))            then [{on: "max_age",              want: ($d.ttl | to_ns)}]              else [] end)
              + (if ($d | has("history"))        then [{on: "max_msgs_per_subject", want: ($d.history | to_count)}]       else [] end)
              + (if ($d | has("max_value_size")) then [{on: "max_msg_size",         want: ($d.max_value_size | to_bytes)}] else [] end)
              + (if ($d | has("storage"))        then [{on: "storage",              want: ($d.storage)}]                  else [] end)
              + (if ($d | has("replicas"))       then [{on: "num_replicas",         want: ($d.replicas)}]                 else [] end)
             )
           )
       ] | flatten)
    | .[]
' 2>&1)"

if [ $? -ne 0 ] || [ -z "${REPORT}" ]; then
    unknown "the bus's answer and the pinned definitions could not be compared: ${REPORT:-the comparison produced nothing}"
fi

# A conversion this script could not do is an unknown, not a mismatch: it means
# the definitions are written in units this does not read, and saying "differs"
# about that would be a lie about the bus.
if printf '%s' "${REPORT}" | /bin/grep -q 'UNREADABLE-DURATION\|UNREADABLE-SIZE\|UNREADABLE-COUNT'; then
    printf '%s\n' "${REPORT}" | /bin/grep 'UNREADABLE-' >&2
    unknown "a field in the pinned definitions is written in units this comparison does not read (above). Nothing is claimed about the bus."
fi

AGREED=0
PROBLEMS=0
while IFS='|' read -r kind resource field wanted found; do
    [ -n "${kind}" ] || continue
    case "${kind}" in
        OK)
            AGREED=$((AGREED+1))
            say "  agrees       ${resource} — all ${field} field(s) the definitions name"
            ;;
        MISSING)
            PROBLEMS=$((PROBLEMS+1))
            say "  MISSING      ${resource} — the pinned definitions name it and this bus does not hold it"
            ;;
        ABSENT_FIELD)
            PROBLEMS=$((PROBLEMS+1))
            say "  UNREADABLE   ${resource} — the definitions pin '${field}' (${wanted}) and the bus's answer does not carry that field at all"
            ;;
        MISMATCH)
            PROBLEMS=$((PROBLEMS+1))
            say "  DIFFERS      ${resource} — '${field}': the pinned definitions say ${wanted}, this bus says ${found}"
            ;;
    esac
done <<< "${REPORT}"

# A field the answer does not carry is an unknown about the bus, so it leaves by
# the unknown door even though it was found during the comparison.
if printf '%s' "${REPORT}" | /bin/grep -q '^ABSENT_FIELD|'; then
    unknown "a field the pinned definitions name is not in this bus's answer (above), so those fields were not compared. Nothing was updated."
fi

if [ "${PROBLEMS}" -gt 0 ]; then
    refuse "${PROBLEMS} difference(s) between this bus and the pinned definitions (above). NOTHING WAS UPDATED — a running bus that does not match the definitions is settled before a rollout, not during one. The pinned definitions are ${STREAM_DEFS} and ${KV_DEFS}, at the commit this release names."
fi

say "This bus already holds every stream and bucket the pinned definitions name, and every field they name agrees (${AGREED} of them). Nothing was written to the bus."
exit 0

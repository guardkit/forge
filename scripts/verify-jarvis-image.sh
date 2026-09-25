#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Prove the JARVIS image, from inside it.
#
# Written 25 September 2026, stage 4c of the containerisation rollout gate, the
# day the Slack front door and the bus gateway became a release image. Before
# that both ran as host units out of a checkout and a virtual environment under
# a home directory, so "the same front door on two machines" was not a thing
# anybody could have.
#
# ONE IMAGE, TWO SERVICES. The front door serves the two graphs the jarvis
# repository declares; the bus gateway is the same package's own command
# running the supervisor on the bus. They share every dependency and are built
# from one commit, which is why they are one image with two start commands —
# the same shape the coordinator and the answer service already have.
#
# THIS IS THE SMALLEST HONEST PROOF, and it says so rather than pretending to
# be more. What can be asked of the image alone, with no bus, no Slack
# workspace, no model seat and no network, is asked here. What cannot — that
# the front door really answers, that the gateway really joins the bus — is
# proved by bringing the estate up, which is the estate's own check
# (`estate-check services`) and not something an image can answer by itself.
#
# NOTHING HERE TOUCHES SLACK. No token is read, set, printed or sent anywhere,
# and nothing in this script can reach a Slack workspace.
#
# WHAT IT ASKS:
#
#   1. WHICH IMAGE THIS IS. The release labels are there and the role is the
#      one this script knows how to prove. It reads that from the image rather
#      than being told.
#   2. BOTH START COMMANDS ARE REALLY IN IT. The front door's server answers
#      --help and the package's own command line answers its version, inside
#      the image, so the two things the estate starts are installed rather than
#      merely named. An image that installed nothing passes every other check.
#   3. THE PACKAGE ITSELF IMPORTS, including the bus transport the gateway
#      needs — which is an optional extra of that repository, so an image built
#      without it would start the gateway and have it refuse the bus.
#   4. THE ESTATE'S TWO COMMANDS ARE THE ONES THIS IMAGE CAN RUN. Both estate
#      services name an entrypoint — each is given its secrets as files rather
#      than as values anybody with the Docker daemon can read — and naming an
#      entrypoint EMPTIES the image's own command, so the compose file has to
#      write each command out. The front door's is the image's own CMD; the
#      gateway's is the package's own command. Nothing held those together
#      until this check.
#   5. WHICH USER IT RUNS AS. The release has a standard about this: the
#      publisher's own proof refuses an image that runs as root.
#   6. IT CARRIES NOTHING OF ANY MACHINE, and no Slack token of any shape. No
#      user name, no home path, no projects folder, no machine name from
#      wherever it was built, and nothing shaped like a bot or app token, in
#      its configuration or its labels.
#
# Nothing here names a target project's language, test runner, package manager
# or layout. It asks about one image of this factory's own estate.
#
# Usage:  verify-jarvis-image.sh <image tag>
# ---------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ESTATE_COMPOSE="${REPO_ROOT}/deploy/estate/compose.yaml"

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

IMAGE="${1:-}"
[ -n "${IMAGE}" ] || fail "usage: $0 <image tag>. This script proves one image; it never picks one."

command -v docker >/dev/null 2>&1 \
    || fail "docker is not on PATH, so the image cannot be opened and nothing here can be checked. Unknown is not a pass."

label_of() {
    docker image inspect "${IMAGE}" --format "{{index .Config.Labels \"$1\"}}" 2>/dev/null
}

# --- (1) which image is this ------------------------------------------------
ROLE="$(label_of com.guardkit.release.image.role)"
VERSION="$(label_of com.guardkit.release.version)"
[ -n "${VERSION}" ] \
    || fail "${IMAGE} carries no com.guardkit.release.version label, so it did not come out of a release build. Every image of a release says which release it is."
[ "${ROLE}" = "front-door" ] \
    || fail "${IMAGE} has role '${ROLE}'. This script proves the jarvis image, whose role is 'front-door' — one image that runs both the Slack front door and the bus gateway."
echo "  OK  identity    ${IMAGE} is the ${ROLE} image of release ${VERSION}"

# --- (2) both start commands are really in it -------------------------------
#
# Asked with --help and --version, which run the real programs and reach no
# network, no bus and no Slack workspace.
if ! docker run --rm --entrypoint langgraph "${IMAGE}" dev --help >/dev/null 2>&1; then
    fail "the front door's server does not answer inside ${IMAGE}. The estate starts it with 'langgraph dev', and an image that cannot run that command starts nothing."
fi
echo "  OK  front door  the server the estate starts answers --help inside the image"

if ! docker run --rm --entrypoint jarvis "${IMAGE}" version >/dev/null 2>&1; then
    fail "the package's own command line does not answer inside ${IMAGE}. The estate starts the bus gateway with it, so an image that cannot run it starts nothing."
fi
echo "  OK  gateway     the package's own command line answers inside the image"

# --- (3) the package imports, bus transport included ------------------------
IMPORT_ERR="${TMPDIR:-/tmp}/verify-jarvis-import.$$"
if ! docker run --rm --entrypoint python "${IMAGE}" \
        -c "import jarvis, jarvis.cli.main, nats_core, nats" >/dev/null 2>"${IMPORT_ERR}"; then
    WHY="$(tr '\n' ' ' < "${IMPORT_ERR}")"
    rm -f "${IMPORT_ERR}"
    fail "the package or its bus transport does not import inside ${IMAGE}: ${WHY}. The live bus client is an optional extra of that repository, so an image built without it starts the gateway and then refuses the bus."
fi
rm -f "${IMPORT_ERR}"
echo "  OK  program     the package and its bus transport import inside the image"

# --- (4) the estate's two commands are the ones this image can run ----------
[ -f "${ESTATE_COMPOSE}" ] \
    || fail "there is no estate compose file at ${ESTATE_COMPOSE}, so the commands the estate writes out cannot be compared with the image. Unknown is not a pass."

command_of_service() {
    awk -v svc="  $1:" '
        $0 == svc { inside = 1; next }
        inside && /^  [a-z]/ { inside = 0 }
        inside && $0 ~ /^    command: \[/ {
            line = $0
            sub(/^    command: /, "", line)
            gsub(/ /, "", line)
            print line
            exit
        }
    ' "${ESTATE_COMPOSE}"
}

IMAGE_CMD="$(docker image inspect --format '{{json .Config.Cmd}}' "${IMAGE}" | tr -d ' ')"
FRONT_DOOR_CMD="$(command_of_service front-door)"
GATEWAY_CMD="$(command_of_service bus-gateway)"

[ -n "${FRONT_DOOR_CMD}" ] \
    || fail "the estate's 'front-door' service has no one-line 'command: [...]' in ${ESTATE_COMPOSE}, and it names an entrypoint — so it would start this image with no command at all."
[ -n "${GATEWAY_CMD}" ] \
    || fail "the estate's 'bus-gateway' service has no one-line 'command: [...]' in ${ESTATE_COMPOSE}, and it names an entrypoint — so it would start this image with no command at all."
[ "${IMAGE_CMD}" = "${FRONT_DOOR_CMD}" ] \
    || fail "${IMAGE} starts with CMD ${IMAGE_CMD}, and the estate's 'front-door' service writes command: ${FRONT_DOOR_CMD}. The front door is what this image's own CMD is for, so the two have to agree."
echo "  OK  command     the estate's 'front-door' command is the image's own CMD: ${IMAGE_CMD}"

case "${GATEWAY_CMD}" in
    *'"jarvis"'*'"serve-nats"'*) ;;
    *) fail "the estate's 'bus-gateway' service writes command: ${GATEWAY_CMD}. The bus gateway is the package's own 'jarvis serve-nats', and this image is what runs it." ;;
esac
echo "  OK  command     the estate's 'bus-gateway' command is the package's own: ${GATEWAY_CMD}"

# --- (5) which user it runs as ----------------------------------------------
ID_LINE="$(docker run --rm --entrypoint id "${IMAGE}" -u 2>/dev/null | tr -d '\r')" \
    || fail "${IMAGE} could not be asked which user it runs as. Unknown is not a pass."
[ -n "${ID_LINE}" ] \
    || fail "${IMAGE} gave no answer when asked which user it runs as. Unknown is not a pass."
[ "${ID_LINE}" != "0" ] \
    || fail "${IMAGE} runs as root. The release's standard is set by the publisher's own proof, which refuses root outright, and this image is reachable from a Slack workspace."
echo "  OK  user        runs as uid ${ID_LINE}, not root"

# --- (6) nothing of any machine, and no token of any shape ------------------
#
# The image's whole configuration and every label, swept for the things a build
# on somebody's laptop leaves behind. The words come from THIS machine at the
# moment of the sweep, so the sweep is about wherever the image was built. The
# token shapes are swept for because this is the one image of the release whose
# settings are Slack credentials.
CONFIGURATION="$(docker image inspect --format '{{json .Config}}' "${IMAGE}")"
FOUND=""
for word in "$(id -un)" "${HOME}" "$(hostname -s 2>/dev/null || true)"; do
    [ -n "${word}" ] || continue
    case "${word}" in root|/root) continue ;; esac
    case "${CONFIGURATION}" in
        *"${word}"*) FOUND="${FOUND} ${word}" ;;
    esac
done
[ -z "${FOUND}" ] \
    || fail "${IMAGE} carries something belonging to the machine it was built on:${FOUND}. A release image belongs to the release, not to a laptop."
echo "  OK  no machine  nothing of the building machine is in the image's configuration or labels"

for shape in "xoxb-" "xoxp-" "xapp-"; do
    case "${CONFIGURATION}" in
        *"${shape}"*) fail "${IMAGE} carries something beginning '${shape}' in its configuration or labels, which is the shape of a Slack token. A credential belongs in a file the machine mounts, never in an image." ;;
    esac
done
echo "  OK  no token    nothing shaped like a Slack token is in the image's configuration or labels"

echo "jarvis image verification PASSED for ${IMAGE} [${ROLE}]"

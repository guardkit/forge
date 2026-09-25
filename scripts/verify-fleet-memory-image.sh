#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Prove one of the MEMORY images, from inside it.
#
# Written 25 September 2026, stage 4b of the containerisation rollout gate, the
# day the memory service and its relay became release images. Before that they
# were built by hand on this machine from a checkout, with `docker compose
# up -d --build`, on a floating base tag — so "the same memory service on two
# machines" was not a thing anybody could have.
#
# THIS IS THE SMALLEST HONEST PROOF, and it says so rather than pretending to
# be more. The memory repository ships no proof script of its own and the
# factory does not put one there; what can be asked of the image alone, with no
# database, no bus, no embedder and no network, is asked here. What cannot —
# that the service really answers, that the relay really connects — is proved
# by bringing the estate up, which is the estate's own check (`estate-check
# services`) and not something an image can answer by itself.
#
# WHAT IT ASKS:
#
#   1. WHICH IMAGE THIS IS. The release labels are there, and the role is one
#      of the two this script knows how to prove. Everything below depends on
#      knowing which of the two it has been handed, and it reads that from the
#      image rather than being told.
#   2. ITS OWN PROGRAM IS IN IT. The module the image's CMD names imports
#      inside the image — the package is installed, its dependencies are, and
#      the interpreter in the image can load it. An image that installed
#      nothing passes every other check here.
#   3. ITS CMD IS THE ONE THE ESTATE WRITES OUT. The estate's compose file
#      names an entrypoint for each of these two services, because each is
#      given its database address as a file rather than as a value anybody with
#      the Docker daemon can read — and naming an entrypoint EMPTIES the
#      image's own command, so the compose file has to write the command out.
#      Nothing held the two together for the bus until a reviewer asked on 24
#      September 2026; this is the same guard, made at the same moment.
#   4. IT CARRIES NOTHING OF ANY MACHINE. No user name, no home path, no
#      projects folder, no machine name from wherever it was built, in its
#      configuration or its labels.
#
# Nothing here names a target project's language, test runner, package manager
# or layout. It asks about one image of this factory's own estate.
#
# Usage:  verify-fleet-memory-image.sh <image tag>
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

case "${ROLE}" in
    memory)
        SERVICE="memory"
        MODULE="fleet_memory.mcp"
        ;;
    memory-relay)
        SERVICE="memory-relay"
        MODULE="fleet_memory.app"
        ;;
    *)
        fail "${IMAGE} has role '${ROLE}'. This script proves the two memory images, whose roles are 'memory' and 'memory-relay'."
        ;;
esac
echo "  OK  identity    ${IMAGE} is the ${ROLE} image of release ${VERSION}"

# --- (2) its own program is in it -------------------------------------------
if ! docker run --rm --entrypoint python "${IMAGE}" -c "import ${MODULE}" >/dev/null 2>"${TMPDIR:-/tmp}/verify-fleet-memory-import.$$"; then
    WHY="$(tr '\n' ' ' < "${TMPDIR:-/tmp}/verify-fleet-memory-import.$$")"
    rm -f "${TMPDIR:-/tmp}/verify-fleet-memory-import.$$"
    fail "${MODULE} does not import inside ${IMAGE}: ${WHY}. The image is meant to run that module and cannot load it."
fi
rm -f "${TMPDIR:-/tmp}/verify-fleet-memory-import.$$"
echo "  OK  program     ${MODULE} imports inside the image"

# --- (3) its CMD is the one the estate writes out ---------------------------
#
# The estate names an entrypoint for this service, which empties the image's
# own command, so the compose file writes the command out by hand. If a future
# pin of the memory repository changes its CMD, the estate would start the
# wrong program and every other test would still pass.
IMAGE_CMD="$(docker image inspect --format '{{json .Config.Cmd}}' "${IMAGE}" | tr -d ' ')"
[ -f "${ESTATE_COMPOSE}" ] \
    || fail "there is no estate compose file at ${ESTATE_COMPOSE}, so the image's command cannot be compared with the one the estate writes out. Unknown is not a pass."
COMPOSE_CMD="$(awk -v svc="  ${SERVICE}:" '
    $0 == svc { inside = 1; next }
    inside && /^  [a-z]/ { inside = 0 }
    inside && $0 ~ /^    command: \[/ {
        line = $0
        sub(/^    command: /, "", line)
        gsub(/ /, "", line)
        print line
        exit
    }
' "${ESTATE_COMPOSE}")"
[ -n "${COMPOSE_CMD}" ] \
    || fail "the estate's '${SERVICE}' service has no one-line 'command: [...]' in ${ESTATE_COMPOSE}, and it names an entrypoint — so it would start that image with no command at all."
[ "${IMAGE_CMD}" = "${COMPOSE_CMD}" ] \
    || fail "${IMAGE} starts with CMD ${IMAGE_CMD}, and the estate's '${SERVICE}' service writes command: ${COMPOSE_CMD}. Make the estate say what the image says, then build again."
echo "  OK  command     the estate's '${SERVICE}' command is the image's own CMD: ${IMAGE_CMD}"

# --- (4) nothing of any machine ---------------------------------------------
#
# The image's whole configuration and every label, swept for the things a build
# on somebody's laptop leaves behind. The words come from THIS machine at the
# moment of the sweep, so the sweep is about wherever the image was built.
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

echo "memory image verification PASSED for ${IMAGE} [${ROLE}]"

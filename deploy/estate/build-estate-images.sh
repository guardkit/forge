#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# THE TWO IMAGES THE ESTATE NEEDS THAT THE RELEASE SCRIPT DOES NOT BUILD.
#
#   the bus              built from the BUS REPOSITORY'S OWN Dockerfile, from a
#                        fresh fetch of that repository at the commit pinned in
#                        estate-pins.conf — never from a checkout beside this
#                        one, because a checkout belongs to a machine
#   the one-shot that    built from provisioner/Dockerfile: the NATS project's
#   provisions the bus   own tool image (the `nats` command line and `jq`) with
#                        `bash` added, because the bus repository's provisioning
#                        scripts are bash scripts
#
# It also fills a VOLUME with the bus repository's OWN config and provisioning
# scripts, from the same pinned commit. The estate's compose file mounts that
# volume read-only: the bus's config and the definition of its storage belong
# to the bus's repository, the factory keeps no copy of either, and because it
# is a volume rather than a folder on a disk, nothing in the running estate
# names a path belonging to any machine.
#
# WHY THESE TWO ARE NOT IN release/manifest.yaml, which is where a release's
# images belong. Two things used to keep every release image inside one
# repository: one build-context root, and one base image digest that every
# Dockerfile of the release must start FROM.
#
# The first is gone — on 25 September 2026 an image entry gained a `context:`,
# naming which of the manifest's own repositories it is built from, and the
# memory service and its relay are release images on that footing. The second
# still stands, and it is what keeps the bus out: the bus starts FROM a NATS
# base rather than the Python one, and a release with two bases is two supply
# chains under one name. Per-image bases are a change to what a release MEANS —
# every image's labels say which base the release pins — and that deserves its
# own pass rather than a corner of this one. Until then the pin lives in
# estate-pins.conf, the tag carries the same release version as the release
# images, and a test holds the example env file to it — so the estate still
# moves as one release, and nothing here is unpinned.
#
#   ./build-estate-images.sh                 # tags both at the release version
#   ./build-estate-images.sh --version 9.9   # a throwaway tag, for a proof
#   ./build-estate-images.sh --help
# ---------------------------------------------------------------------------
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die() { printf 'estate images: %s\n' "$*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }

VERSION=""
BUS_IMAGE=""
PROVISION_IMAGE=""
BUS_SOURCE_VOLUME=""
KEEP_TEMP="no"

while [ $# -gt 0 ]; do
    case "$1" in
        --version)           VERSION="${2:?--version needs the release version}"; shift 2 ;;
        --bus-image)         BUS_IMAGE="${2:?--bus-image needs a name:tag}"; shift 2 ;;
        --provision-image)   PROVISION_IMAGE="${2:?--provision-image needs a name:tag}"; shift 2 ;;
        --bus-source-volume) BUS_SOURCE_VOLUME="${2:?--bus-source-volume needs a volume name}"; shift 2 ;;
        --keep-temp)         KEEP_TEMP="yes"; shift ;;
        --help|-h)           sed -n '2,37p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *)                   die "unknown option '$1'. --help lists them." ;;
    esac
done

command -v docker >/dev/null 2>&1 || die "docker is not on PATH, so nothing can be built."
command -v git    >/dev/null 2>&1 || die "git is not on PATH, so the bus repository cannot be fetched at its pin."

PINS="${HERE}/estate-pins.conf"
[ -f "${PINS}" ] || die "estate-pins.conf is missing from ${HERE}; it is what says which commit the bus is built from."
# shellcheck disable=SC1090
set -a; . "${PINS}"; set +a

for name in BUS_REPOSITORY_URL BUS_REPOSITORY_BRANCH BUS_REPOSITORY_COMMIT \
            BUS_IMAGE_NAME BUS_PROVISION_BASE_IMAGE BUS_PROVISION_BASE_DIGEST \
            BUS_PROVISION_IMAGE_NAME BUS_SOURCE_VOLUME_NAME; do
    eval "value=\${${name}:-}"
    [ -n "${value}" ] || die "estate-pins.conf sets no ${name}."
done

# The release version, so the estate's images and the release's images carry
# one name between them.
if [ -z "${VERSION}" ]; then
    MANIFEST="${HERE}/../../release/manifest.yaml"
    [ -f "${MANIFEST}" ] || die "the release manifest is not at ${MANIFEST}, so the release version is unknown. Pass --version to build under a name of your own."
    VERSION="$(sed -n 's/^version:[[:space:]]*\([^[:space:]]*\)[[:space:]]*$/\1/p' "${MANIFEST}" | head -1)"
    [ -n "${VERSION}" ] || die "the release manifest has no version line."
fi

[ -n "${BUS_IMAGE}" ]          || BUS_IMAGE="${BUS_IMAGE_NAME}:${VERSION}"
[ -n "${PROVISION_IMAGE}" ]    || PROVISION_IMAGE="${BUS_PROVISION_IMAGE_NAME}:${VERSION}"
[ -n "${BUS_SOURCE_VOLUME}" ]  || BUS_SOURCE_VOLUME="${BUS_SOURCE_VOLUME_NAME}-${VERSION}"

say "The bus        ${BUS_IMAGE}        from ${BUS_REPOSITORY_URL} @ ${BUS_REPOSITORY_COMMIT}"
say "The one-shot   ${PROVISION_IMAGE}  from ${BUS_PROVISION_BASE_IMAGE} (${BUS_PROVISION_BASE_DIGEST}) plus bash"
say "The bus's own  ${BUS_SOURCE_VOLUME}  config and provisioning scripts, from the same pin"
say ""

TMP="$(mktemp -d)"
cleanup() {
    if [ "${KEEP_TEMP}" = "yes" ]; then
        say "the fetched clone was left in ${TMP}"
    else
        rm -rf "${TMP}"
    fi
}
trap cleanup EXIT

# --- fetch the bus at its pin ----------------------------------------------
say "Fetching the bus repository at its pin into a temporary folder"
export GIT_TERMINAL_PROMPT=0
CLONE="${TMP}/bus"
mkdir -p "${CLONE}"
git -C "${CLONE}" init --quiet
git -C "${CLONE}" remote add origin "${BUS_REPOSITORY_URL}"
if ! git -C "${CLONE}" fetch --quiet --depth 1 --no-tags origin "${BUS_REPOSITORY_COMMIT}" 2>/dev/null; then
    say "  (the host would not serve that commit directly; trying the ${BUS_REPOSITORY_BRANCH} branch)"
    git -C "${CLONE}" fetch --quiet --depth 50 --no-tags origin "${BUS_REPOSITORY_BRANCH}" \
        || die "could not fetch ${BUS_REPOSITORY_URL}. Neither the pin ${BUS_REPOSITORY_COMMIT} nor the branch ${BUS_REPOSITORY_BRANCH} was served. Check the pin and this machine's access to the host."
fi
git -C "${CLONE}" checkout --quiet --detach "${BUS_REPOSITORY_COMMIT}" \
    || die "the pin ${BUS_REPOSITORY_COMMIT} is not in what ${BUS_REPOSITORY_URL} served. Either it never was on ${BUS_REPOSITORY_BRANCH}, or it has left it, or it was never pushed."
LANDED="$(git -C "${CLONE}" rev-parse HEAD)"
[ "${LANDED}" = "${BUS_REPOSITORY_COMMIT}" ] \
    || die "the fresh clone landed on ${LANDED} and the pin is ${BUS_REPOSITORY_COMMIT}. Refusing to build."
say "  ok the bus repository at ${LANDED}"

# The pin has to be ON the branch the pins file says it came from, or the
# estate would record a branch nobody checked.
TIP="$(git -C "${CLONE}" ls-remote origin "refs/heads/${BUS_REPOSITORY_BRANCH}" | awk 'NR==1 {print $1}')"
[ -n "${TIP}" ] || die "${BUS_REPOSITORY_URL} has no branch ${BUS_REPOSITORY_BRANCH}."
if [ "${TIP}" = "${BUS_REPOSITORY_COMMIT}" ]; then
    say "  ok the pin is the tip of ${BUS_REPOSITORY_BRANCH}"
else
    for depth in 50 500 5000 2147483647; do
        git -C "${CLONE}" fetch --quiet --depth "${depth}" --no-tags origin \
            "+refs/heads/${BUS_REPOSITORY_BRANCH}:refs/remotes/origin/${BUS_REPOSITORY_BRANCH}" 2>/dev/null || true
        if git -C "${CLONE}" merge-base --is-ancestor "${BUS_REPOSITORY_COMMIT}" \
                "refs/remotes/origin/${BUS_REPOSITORY_BRANCH}" 2>/dev/null; then
            say "  ok the pin is on ${BUS_REPOSITORY_BRANCH}, behind its tip ${TIP}"
            break
        fi
        [ "${depth}" = "2147483647" ] && die "the pin ${BUS_REPOSITORY_COMMIT} is not on ${BUS_REPOSITORY_BRANCH} at ${BUS_REPOSITORY_URL} (which is at ${TIP})."
    done
fi

for needed in Dockerfile config/nats-server.conf config/accounts kv/provision-kv.sh \
              kv/kv-definitions.json streams/provision-streams.sh streams/stream-definitions.json; do
    [ -e "${CLONE}/${needed}" ] || die "the bus repository at ${BUS_REPOSITORY_COMMIT} has no ${needed}, which the estate mounts or builds from."
done

# --- the bus image, from the bus repository's own Dockerfile ----------------
say ""
say "Building the bus image from the bus repository's own Dockerfile"
docker build --file "${CLONE}/Dockerfile" \
    --tag "${BUS_IMAGE}" \
    --label "com.guardkit.estate.bus.commit=${BUS_REPOSITORY_COMMIT}" \
    --label "com.guardkit.estate.bus.source=${BUS_REPOSITORY_URL}" \
    --label "com.guardkit.estate.bus.branch=${BUS_REPOSITORY_BRANCH}" \
    --label "com.guardkit.estate.version=${VERSION}" \
    "${CLONE}" >/dev/null || die "the bus image would not build."
say "  ok ${BUS_IMAGE}"

# THE COMPOSE FILE'S COMMAND MUST BE THE IMAGE'S OWN. compose.yaml names an
# entrypoint for the bus (a wrapper), and naming an entrypoint empties the
# image's CMD, so the compose file writes the command out by hand. Nothing held
# the two together (the stage 4a reviewer, 24 September 2026): a future pin whose
# Dockerfile changed its CMD would start the broker on the wrong command and every
# test would still pass. So the build refuses when they differ.
IMAGE_CMD="$(docker image inspect --format '{{json .Config.Cmd}}' "${BUS_IMAGE}")"
COMPOSE_CMD="$(sed -nE 's/^[[:space:]]*command:[[:space:]]*(\[.*\])[[:space:]]*$/\1/p' "${HERE}/compose.yaml" | head -n 1 | tr -d ' ')"
[ -n "${COMPOSE_CMD}" ] || die "compose.yaml's bus service has no one-line 'command: [...]' to compare with the image's CMD."
[ "$(printf '%s' "${IMAGE_CMD}" | tr -d ' ')" = "${COMPOSE_CMD}" ] || die "the bus image at ${BUS_REPOSITORY_COMMIT} starts with CMD ${IMAGE_CMD}, but compose.yaml's bus service writes command: ${COMPOSE_CMD}. Make the compose file say what the image says, then build again."
say "  ok the compose file's bus command matches the image's CMD: ${IMAGE_CMD}"

# --- the one-shot ----------------------------------------------------------
BASE_REPOSITORY="${BUS_PROVISION_BASE_IMAGE%%:*}"
say ""
say "Building the one-shot that provisions the bus's storage"
docker build --file "${HERE}/provisioner/Dockerfile" \
    --build-arg "NATS_BOX_IMAGE=${BASE_REPOSITORY}@${BUS_PROVISION_BASE_DIGEST}" \
    --tag "${PROVISION_IMAGE}" \
    --label "com.guardkit.estate.provision.base=${BUS_PROVISION_BASE_IMAGE}" \
    --label "com.guardkit.estate.provision.base.digest=${BUS_PROVISION_BASE_DIGEST}" \
    --label "com.guardkit.estate.version=${VERSION}" \
    "${HERE}/provisioner" >/dev/null || die "the one-shot image would not build."
say "  ok ${PROVISION_IMAGE}"

# --- the bus's own config and provisioning scripts, into a volume ----------
say ""
say "Filling ${BUS_SOURCE_VOLUME} with the bus repository's config and provisioning scripts, at the same pin"
docker volume inspect "${BUS_SOURCE_VOLUME}" >/dev/null 2>&1 \
    && docker volume rm "${BUS_SOURCE_VOLUME}" >/dev/null \
    && say "  (replacing the volume of that name this build step made before)"
docker volume create "${BUS_SOURCE_VOLUME}" >/dev/null \
    || die "could not make the volume ${BUS_SOURCE_VOLUME}."
# Copied by the bus's own image, so this step needs no other image and no tool
# on the machine. The clone is a temporary folder of this build, never of the
# running estate.
docker run --rm --entrypoint /bin/sh \
    --volume "${BUS_SOURCE_VOLUME}:/dst" \
    --volume "${CLONE}:/src:ro" \
    "${BUS_IMAGE}" -c '
        set -e
        cp -R /src/config /src/kv /src/streams /dst/
        printf "%s\n" "$1" > /dst/.pinned-commit
    ' _ "${BUS_REPOSITORY_COMMIT}" \
    || die "could not fill ${BUS_SOURCE_VOLUME}."
say "  ok config/, kv/ and streams/ at ${BUS_REPOSITORY_COMMIT}"

say ""
say "Done. Name these three in .env:"
say "  NATS_IMAGE=${BUS_IMAGE}"
say "  NATS_PROVISION_IMAGE=${PROVISION_IMAGE}"
say "  BUS_SOURCE_VOLUME=${BUS_SOURCE_VOLUME}"

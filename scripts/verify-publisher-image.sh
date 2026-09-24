#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Prove the PUBLISHER's image, from inside it.
#
# Written 2026-09-24, stage 2b of the containerisation rollout gate, the day
# that image was built for the first time. Until then the release built one
# image, the coordinator's, and every proof of the publisher ran THAT image
# with the publisher's start line. A stand-in proves the start line; it proves
# nothing about the image, and the publisher's image is the one that holds the
# credential able to write to a project's remote. So it gets a proof of its
# own, and the release runs it before calling itself built.
#
# WHAT THIS ASKS, and why each one:
#
#   1. PROVENANCE — the publisher's code inside the image is, file for file,
#      the publisher's code in the tree this script was invoked from. The
#      coordinator's image learned this the hard way on 11 September 2026: a
#      build printed every source step as executed and shipped the PREVIOUS
#      commit's code. Same guard, same reason.
#   2. IT IS NOT ROOT, and it is the user its own volume is made for: the
#      container's own id is 1000. A fresh named volume takes its ownership
#      from the image, so the id here and the folder below are the whole of
#      why the publisher's volume needs no hand-over on a clean machine.
#   3. ITS STATE FOLDER EXISTS IN THE IMAGE, owned by that user, and is
#      writable by it. This is the fix for the crash loop of 24 September
#      2026: eleven restarts in two minutes on a root-owned fresh volume.
#   4. GIT IS THERE. It is the whole of the publisher's toolchain.
#   5. ITS OWN PROGRAM ANSWERS. Not a stand-in start line: the module this
#      image's ENTRYPOINT names, run inside this image.
#   6. IT CANNOT BUILD OR DEPLOY ANYTHING — no docker client, no docker
#      socket, no sandbox client, no compiler. The wall around the credential
#      is the process boundary, and a build tool inside it is a hole in that
#      wall.
#   7. IT CARRIES NO CREDENTIAL. The one file the credential lives in is
#      MOUNTED at run time; an image that had one baked in would put it in
#      every copy of that image for ever.
#
# Nothing here names a target project's language, test runner, package
# manager, database or layout. It asks about one image of this factory's own.
#
# Usage:  verify-publisher-image.sh <image tag>
# ---------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

IMAGE="${1:-}"
[ -n "${IMAGE}" ] || fail "usage: $0 <image tag>. This script proves one image; it never picks one."

command -v docker >/dev/null 2>&1 \
    || fail "docker is not on PATH, so the image cannot be opened and nothing here can be checked. Unknown is not a pass."
command -v git >/dev/null 2>&1 \
    || fail "git is not on PATH, so the code in the image cannot be compared with the code in this tree. Unknown is not a pass."

#: Where the image puts the sources, and the user it runs as. Both are the
#: image's own arrangement, read here rather than assumed further down.
IN_IMAGE_SRC="/opt/forge/src"
STATE_DIR="/home/publisher/state"

echo "Checking ${IMAGE} carries the publisher code in ${REPO_ROOT}"

git -C "${REPO_ROOT}" rev-parse --git-dir >/dev/null 2>&1 \
    || fail "${REPO_ROOT} is not a git checkout, so there is no tree to compare the image against. Unknown is not a pass."

# --- (1) provenance ---------------------------------------------------------
# The tree's side: every tracked file under the publisher's package, hashed,
# keyed by its path inside src/. The image's side: the same paths, hashed
# inside the container with the same tool. A path in one and not the other is
# as much a failure as a hash that differs.
: > "${WORK}/tree.tsv"
while IFS= read -r path; do
    case "${path}" in
        */__pycache__/*) continue ;;
    esac
    [ -f "${REPO_ROOT}/${path}" ] \
        || fail "the tree lists ${path} as tracked but the file is not on disk, so the comparison cannot be made."
    printf '%s\t%s\n' "${path#src/}" "$(sha256sum "${REPO_ROOT}/${path}" | cut -c1-64)" >> "${WORK}/tree.tsv"
done < <(git -C "${REPO_ROOT}" ls-files -- src/forge/publisher)

[ -s "${WORK}/tree.tsv" ] \
    || fail "no tracked files under src/forge/publisher in ${REPO_ROOT}, so there is nothing to compare. Unknown is not a pass."

# POSIX sh, so the same text runs under this machine's bash and the image's
# /bin/sh. __pycache__ is not source and is skipped on both sides.
IMAGE_MANIFEST_SH='
set -eu
cd "$1"
find forge/publisher -type d -name __pycache__ -prune -o -type f -print \
    | LC_ALL=C sort \
    | while IFS= read -r f; do
        printf "%s\t%s\n" "$f" "$(sha256sum "$f" | cut -c1-64)"
      done
'
if ! docker run --rm --entrypoint sh "${IMAGE}" -c "${IMAGE_MANIFEST_SH}" publisher-manifest "${IN_IMAGE_SRC}" \
        > "${WORK}/image.tsv" 2> "${WORK}/image.err"; then
    fail "the publisher package inside ${IMAGE} could not be listed at ${IN_IMAGE_SRC}, so the comparison cannot be made: $(tr '\n' ' ' < "${WORK}/image.err")"
fi
[ -s "${WORK}/image.tsv" ] \
    || fail "the publisher package inside ${IMAGE} has no files in it, so the comparison cannot be made. Unknown is not a pass."

LC_ALL=C sort -o "${WORK}/tree.tsv" "${WORK}/tree.tsv"
LC_ALL=C sort -o "${WORK}/image.tsv" "${WORK}/image.tsv"
if ! diff -u "${WORK}/tree.tsv" "${WORK}/image.tsv" > "${WORK}/diff" 2>&1; then
    {
        echo "FAIL: the publisher code in ${IMAGE} is not the publisher code in ${REPO_ROOT}."
        echo "      '-' is the tree, '+' is the image; a path on one side only is a missing or extra file."
        sed -n '4,40p' "${WORK}/diff"
        echo "      Do not ship this image; build it again from the tree you mean to ship."
    } >&2
    exit 1
fi
echo "  OK  provenance  $(wc -l < "${WORK}/tree.tsv" | tr -d ' ') files compared byte for byte and every one matches."

# --- (2) it is not root -----------------------------------------------------
ID_LINE="$(docker run --rm --entrypoint sh "${IMAGE}" -c 'printf "%s:%s:%s" "$(id -u)" "$(id -g)" "$(id -un)"')"
case "${ID_LINE}" in
    0:*) fail "${IMAGE} runs as root (${ID_LINE}). The publisher holds the one credential that can write to a project's remote; it does not run as root." ;;
esac
[ "${ID_LINE%%:*}" = "1000" ] \
    || fail "${IMAGE} runs as ${ID_LINE}, and the compose bundle's volume is made for user id 1000. A fresh named volume takes its ownership from the image, so these two have to agree or the publisher cannot write its own state."
echo "  OK  user        runs as ${ID_LINE}, not root"

# --- (3) the state folder, in the image, owned by that user and writable ----
STATE_CHECK_SH='
set -eu
d="$1"
[ -d "$d" ] || { echo "no such directory in the image: $d" >&2; exit 1; }
printf "%s:%s " "$(stat -c %u "$d")" "$(stat -c %g "$d")"
t="$d/.can-this-user-write-here"
: > "$t"
rm -f "$t"
printf "writable\n"
'
if ! STATE_LINE="$(docker run --rm --entrypoint sh "${IMAGE}" -c "${STATE_CHECK_SH}" publisher-state "${STATE_DIR}" 2> "${WORK}/state.err")"; then
    fail "${STATE_DIR} in ${IMAGE} is missing, or the user this image runs as cannot write to it: $(tr '\n' ' ' < "${WORK}/state.err"). Docker fills a fresh named volume from what the image has at that path, ownership included — with nothing there, the publisher gets a root-owned folder it cannot write, and crash-loops."
fi
case "${STATE_LINE}" in
    "1000:1000 writable") ;;
    *) fail "${STATE_DIR} in ${IMAGE} reads '${STATE_LINE}'; it has to be owned by 1000:1000 and writable, because that ownership is what Docker copies onto a fresh volume." ;;
esac
echo "  OK  state       ${STATE_DIR} exists in the image, owned 1000:1000 and writable by the running user"

# --- (4) git, the whole of its toolchain ------------------------------------
docker run --rm --entrypoint sh "${IMAGE}" -c 'command -v git >/dev/null 2>&1' \
    || fail "git is not on PATH inside ${IMAGE}. It is the whole of the publisher's toolchain; without it the publisher can read nothing and send nothing."
echo "  OK  toolchain   git is on PATH"

# --- (5) its own program answers --------------------------------------------
# The module this image's ENTRYPOINT names, run inside this image. It is asked
# for its usage, which needs no settings file, no credential and no network.
if ! docker run --rm --entrypoint python "${IMAGE}" -m forge.publisher --help \
        > "${WORK}/help" 2> "${WORK}/help.err"; then
    fail "the publisher's own program did not answer inside ${IMAGE}: $(tr '\n' ' ' < "${WORK}/help.err")"
fi
grep -q -- "--settings" "${WORK}/help" \
    || fail "the publisher's own program answered inside ${IMAGE} but did not offer --settings, which is how the service is given its settings file. Something other than the publisher answered."
echo "  OK  program     the image's own entrypoint module answers"

# --- (6) nothing to build or deploy with ------------------------------------
NOTHING_TO_BUILD_WITH_SH='
set -eu
found=""
for t in docker sbx podman make gcc cc; do
    if command -v "$t" >/dev/null 2>&1; then found="$found $t"; fi
done
for s in /var/run/docker.sock /run/docker.sock; do
    if [ -S "$s" ]; then found="$found $s"; fi
done
printf "%s" "$found"
'
FOUND="$(docker run --rm --entrypoint sh "${IMAGE}" -c "${NOTHING_TO_BUILD_WITH_SH}" publisher-tools)"
[ -z "${FOUND}" ] \
    || fail "${IMAGE} carries something it can build or deploy with:${FOUND}. The wall around the credential is the process boundary, and this is a hole in it."
echo "  OK  no build    no docker client, no docker socket, no sandbox client, no compiler"

# --- (7) no credential baked in ---------------------------------------------
# The compose fragment mounts the credential at this path, read-only, at RUN
# time. An image that already had a file there would carry a secret into every
# copy of itself.
docker run --rm --entrypoint sh "${IMAGE}" -c '[ ! -e /etc/forge-publisher/credential ]' \
    || fail "${IMAGE} has a file at /etc/forge-publisher/credential. The credential is MOUNTED at run time and is never in an image."
echo "  OK  no secret   nothing at the path the credential is mounted at"

echo "publisher image verification PASSED for ${IMAGE}"

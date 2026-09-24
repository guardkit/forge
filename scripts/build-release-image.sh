#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# scripts/build-release-image.sh — build ONE Forge release image from a
# manifest, from nothing but GitHub.
#
# Written 2026-09-24 for the containerisation rollout gate.
#
# WHY THIS EXISTS, ALONGSIDE scripts/build-image.sh
#
# build-image.sh builds from four checkouts that happen to sit beside forge/
# on the machine running it. That is fine for a developer rebuilding what is
# on their own disk, and every one of its existing consumers (the validation
# runbook and three test files that match its buildx line byte for byte) still
# works exactly as before — this script does not touch it.
#
# But a build from whatever is on one machine's disk cannot be reproduced on a
# second machine, and an image built that way cannot say what went into it. So
# a RELEASE is built differently, and this is that path:
#
#   * every input is named in a manifest by repository URL, exact commit and
#     branch (release/manifest.yaml);
#   * each repository is fetched fresh from GitHub at that exact commit into a
#     brand-new temporary folder, and the clone is refused if it lands on any
#     other commit;
#   * the image is built from those clones ALONE — no directory beside this
#     script, or beside the working directory, is read;
#   * the resulting image is tagged by the commit it was built from and by the
#     manifest version, and labelled with all five commits, the manifest
#     version and the manifest's own hash, so the image can always say what it
#     is made of;
#   * the image is NEVER tagged `latest`, and never tags over an image that
#     already exists. A floating tag is the thing this whole path is replacing.
#
# The only things a machine needs to run this: this script, the manifest,
# Docker (with buildx), and network access to GitHub. All five repositories
# are public, so no credential is involved; git is run with prompting disabled
# so that if anything ever DOES ask for one, the build stops loudly instead of
# hanging.
#
# Usage:
#   build-release-image.sh [manifest-path] [options]
#
#     manifest-path        default: ./manifest.yaml in the current directory
#     --keep-temp          leave the fetched clones behind (for diagnosis)
#     --receipt <path>     where to write the JSON receipt
#                          (default: ./<image>-<version>.json beside the manifest's
#                           reader, i.e. in the current directory)
#     --allow-existing-tag permit building over a tag that already exists.
#                          Off by default: a release tag is written once.
#     --skip-proof         skip the image's own proof step. For diagnosis only;
#                          a release is never cut with this.
#     --plan-only          read and check the manifest, print what would be
#                          fetched and built, and stop. Nothing is fetched,
#                          nothing is built, no tag is written.
#
# Exit status is non-zero, with a sentence saying which input was wrong, on any
# drift: a commit that cannot be fetched, a clone at the wrong commit, a
# repository missing from the manifest, a base image digest that no longer
# matches the Dockerfile, or a tag that already exists.
# ---------------------------------------------------------------------------

set -euo pipefail

# Never let git ask for a credential. These repositories are public; a prompt
# would mean something is wrong, and an unattended build must fail rather than
# wait for a person who is not there.
export GIT_TERMINAL_PROMPT=0
export GIT_ASKPASS=/bin/echo
export GIT_CONFIG_NOSYSTEM=1

MANIFEST=""
KEEP_TEMP=0
RECEIPT=""
ALLOW_EXISTING_TAG=0
RUN_PROOF=1
PLAN_ONLY=0

die() { echo "ERROR: $*" >&2; exit 1; }
say() { echo "$*" >&2; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --keep-temp) KEEP_TEMP=1; shift ;;
        --allow-existing-tag) ALLOW_EXISTING_TAG=1; shift ;;
        --skip-proof) RUN_PROOF=0; shift ;;
        --plan-only) PLAN_ONLY=1; shift ;;
        --receipt) [ "$#" -ge 2 ] || die "--receipt needs a path"; RECEIPT="$2"; shift 2 ;;
        -h|--help) sed -n '1,60p' "$0"; exit 0 ;;
        -*) die "unknown option: $1" ;;
        *) [ -z "${MANIFEST}" ] || die "more than one manifest given: ${MANIFEST} and $1"; MANIFEST="$1"; shift ;;
    esac
done

[ -n "${MANIFEST}" ] || MANIFEST="manifest.yaml"
[ -f "${MANIFEST}" ] || die "there is no manifest at ${MANIFEST}. Give its path, or run this from the folder that holds manifest.yaml."
MANIFEST="$(cd "$(dirname "${MANIFEST}")" && pwd)/$(basename "${MANIFEST}")"

command -v docker >/dev/null 2>&1 || die "docker is not on PATH; this script cannot build an image without it."
command -v git   >/dev/null 2>&1 || die "git is not on PATH; this script cannot fetch the pinned commits without it."
command -v awk   >/dev/null 2>&1 || die "awk is not on PATH; this script reads the manifest with it."
command -v sha256sum >/dev/null 2>&1 || die "sha256sum is not on PATH; the manifest's own hash goes into the image's labels."

MANIFEST_SHA="$(sha256sum "${MANIFEST}" | cut -c1-64)"

# ---------------------------------------------------------------------------
# Read the manifest.
#
# Deliberately awk, not a YAML library: the point of this path is that a
# second machine needs the manifest, Docker and GitHub and nothing else. The
# manifest is a small flat file this repository owns, so a small strict reader
# is honest rather than clever. Anything the reader does not recognise is
# reported by name rather than ignored.
# ---------------------------------------------------------------------------
PARSED="$(
    awk '
        function trim(s) { sub(/^[ \t]+/, "", s); sub(/[ \t]+$/, "", s); return s }
        function flush_repo() {
            if (have_repo) {
                printf "REPO|%s|%s|%s|%s|%s\n", r_name, r_url, r_branch, r_commit, r_role
                have_repo = 0; r_name = ""; r_url = ""; r_branch = ""; r_commit = ""; r_role = ""
            }
        }
        {
            line = $0
            sub(/[ \t]*#.*$/, "", line)          # strip comments
            if (trim(line) == "") next
        }
        # top-level "key: value"
        /^[a-z_]+:[ \t]*[^ \t]/ {
            flush_repo()
            in_repos = 0
            k = line; sub(/:.*$/, "", k)
            v = line; sub(/^[a-z_]+:[ \t]*/, "", v)
            printf "TOP|%s|%s\n", trim(k), trim(v)
            next
        }
        # the list header
        /^repositories:[ \t]*$/ { flush_repo(); in_repos = 1; next }
        # a new list entry
        in_repos && /^[ \t]*-[ \t]*[a-z_]+:/ {
            flush_repo()
            have_repo = 1
            e = line; sub(/^[ \t]*-[ \t]*/, "", e)
            k = e; sub(/:.*$/, "", k); k = trim(k)
            v = e; sub(/^[a-z_]+:[ \t]*/, "", v); v = trim(v)
            if (k == "name") r_name = v; else if (k == "url") r_url = v;
            else if (k == "branch") r_branch = v; else if (k == "commit") r_commit = v;
            else if (k == "role") r_role = v; else printf "UNKNOWN|%s\n", k
            next
        }
        # a continuation key of the current list entry
        in_repos && have_repo && /^[ \t]+[a-z_]+:/ {
            e = trim(line)
            k = e; sub(/:.*$/, "", k); k = trim(k)
            v = e; sub(/^[a-z_]+:[ \t]*/, "", v); v = trim(v)
            if (k == "name") r_name = v; else if (k == "url") r_url = v;
            else if (k == "branch") r_branch = v; else if (k == "commit") r_commit = v;
            else if (k == "role") r_role = v; else printf "UNKNOWN|%s\n", k
            next
        }
        { printf "UNPARSED|%s\n", trim(line) }
        END { flush_repo() }
    ' "${MANIFEST}"
)" || die "the manifest at ${MANIFEST} could not be read."

if echo "${PARSED}" | grep -q '^UNPARSED'; then
    echo "ERROR: the manifest at ${MANIFEST} has lines this reader does not understand:" >&2
    echo "${PARSED}" | awk -F'[|]' '$1 == "UNPARSED" { print "       " $2 }' >&2
    die "fix the manifest, or the reader, before building a release from it."
fi
if echo "${PARSED}" | grep -q '^UNKNOWN'; then
    echo "ERROR: the manifest names repository keys this reader does not understand:" >&2
    echo "${PARSED}" | awk -F'[|]' '$1 == "UNKNOWN" { print "       " $2 }' >&2
    die "the four keys per repository are name, url, branch, commit (plus role)."
fi

top() { echo "${PARSED}" | awk -F'[|]' -v k="$2" '$1 == "TOP" && $2 == k { print $3; exit }'; }

SCHEMA="$(top _ schema)"
VERSION="$(top _ version)"
IMAGE_NAME="$(top _ image_name)"
BASE_DIGEST="$(top _ python_base_digest)"

[ "${SCHEMA}" = "1" ] || die "the manifest says schema '${SCHEMA}'; this script reads schema 1."
[ -n "${VERSION}" ] || die "the manifest has no 'version' line, so the image would have no release name."
[ -n "${IMAGE_NAME}" ] || die "the manifest has no 'image_name' line."
case "${BASE_DIGEST}" in
    sha256:[0-9a-f]*) ;;
    *) die "the manifest's python_base_digest is not a sha256 digest: '${BASE_DIGEST}'" ;;
esac

REPO_LINES="$(echo "${PARSED}" | awk -F'[|]' '$1 == "REPO"')"
[ -n "${REPO_LINES}" ] || die "the manifest names no repositories."

ROOT_NAME=""
ROOT_URL=""
ROOT_COMMIT=""

while IFS='|' read -r _tag name url branch commit role; do
    [ -n "${name}" ]   || die "a repository entry in the manifest has no name."
    [ -n "${url}" ]    || die "repository '${name}' has no url."
    [ -n "${branch}" ] || die "repository '${name}' has no branch, so the manifest could not say where its commit came from."
    case "${commit}" in
        [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]) ;;
        *) die "repository '${name}' is pinned to '${commit}', which is not a full 40-character commit. A release pin is the whole commit, never a short one and never a branch name." ;;
    esac
    case "${role}" in
        build-context-root)
            [ -z "${ROOT_NAME}" ] || die "two repositories claim role build-context-root: ${ROOT_NAME} and ${name}."
            ROOT_NAME="${name}"; ROOT_URL="${url}"; ROOT_COMMIT="${commit}"
            ;;
        named-context) ;;
        *) die "repository '${name}' has role '${role}'; the two roles are build-context-root and named-context." ;;
    esac
done <<< "${REPO_LINES}"

[ -n "${ROOT_NAME}" ] || die "no repository in the manifest has role build-context-root, so there is no Dockerfile to build."

REPO_COUNT="$(echo "${REPO_LINES}" | wc -l | tr -d ' ')"
say "Release ${VERSION} — ${REPO_COUNT} repositories, manifest ${MANIFEST_SHA}"

# ---------------------------------------------------------------------------
# The tags, decided before anything is fetched or built.
# ---------------------------------------------------------------------------
TAG_COMMIT="${IMAGE_NAME}:${ROOT_COMMIT}"
TAG_VERSION="${IMAGE_NAME}:${VERSION}"

for t in "${TAG_COMMIT}" "${TAG_VERSION}"; do
    case "${t}" in
        *:latest) die "this script will not produce a '${t}' tag. A release is named by the commit it was built from; a floating tag is what this path replaces." ;;
    esac
    if docker image inspect "${t}" >/dev/null 2>&1; then
        [ "${ALLOW_EXISTING_TAG}" = "1" ] \
            || die "the tag ${t} already exists on this machine. A release tag is written once, and moving it would change what that name means for anything already using it. Delete it deliberately, or pass --allow-existing-tag if you meant to rebuild it."
    fi
done

if [ "${PLAN_ONLY}" = "1" ]; then
    echo "Release ${VERSION} (manifest ${MANIFEST_SHA})"
    echo "  would tag : ${TAG_COMMIT}"
    echo "              ${TAG_VERSION}"
    echo "  base image: ${BASE_DIGEST}"
    echo "  would fetch:"
    while IFS='|' read -r _tag name url branch commit role; do
        echo "    ${name} ${commit} (${branch}) ${url} [${role}]"
    done <<< "${REPO_LINES}"
    echo "Nothing was fetched and nothing was built (--plan-only)."
    exit 0
fi

# ---------------------------------------------------------------------------
# Fetch every repository, fresh, at exactly its pin.
# ---------------------------------------------------------------------------
TMP="$(mktemp -d "${TMPDIR:-/tmp}/forge-release-XXXXXXXX")"
cleanup() {
    if [ "${KEEP_TEMP}" = "1" ]; then
        say "the fetched clones were kept at ${TMP} (--keep-temp)"
    else
        rm -rf "${TMP}"
    fi
}
trap cleanup EXIT

fetch_pinned() {
    local name="$1" url="$2" branch="$3" commit="$4" dest="$5"
    say "  fetching ${name} @ ${commit} (${branch}) from ${url}"
    mkdir -p "${dest}"
    git -C "${dest}" init --quiet
    git -C "${dest}" remote add origin "${url}"
    # A shallow fetch of exactly the pinned commit. GitHub serves an arbitrary
    # reachable commit this way; if a host does not, fall back to a shallow
    # fetch of the named branch and look for the commit in it.
    if ! git -C "${dest}" fetch --quiet --depth 1 --no-tags origin "${commit}" 2>/dev/null; then
        say "    (the host would not serve that commit directly; trying the ${branch} branch)"
        git -C "${dest}" fetch --quiet --depth 50 --no-tags origin "${branch}" \
            || die "could not fetch ${name} from ${url}. The pin ${commit} was not served, and neither was the branch ${branch}. Check the manifest's url and commit, and this machine's network access to the host."
    fi
    git -C "${dest}" checkout --quiet --detach "${commit}" \
        || die "the pin ${commit} is not in what ${url} served for ${name}. Either the commit is not on ${branch} any more, or the manifest names a commit that never existed there."
    local landed
    landed="$(git -C "${dest}" rev-parse HEAD)"
    [ "${landed}" = "${commit}" ] \
        || die "the fresh clone of ${name} landed on ${landed}, and the manifest pins ${commit}. Refusing to build: an image built from a different commit than the one recorded is the whole failure this path exists to prevent."
    say "    ok ${name} at ${landed}"
}

say "Fetching the pinned commits into ${TMP}"
while IFS='|' read -r _tag name url branch commit role; do
    fetch_pinned "${name}" "${url}" "${branch}" "${commit}" "${TMP}/${name}"
done <<< "${REPO_LINES}"

ROOT_DIR="${TMP}/${ROOT_NAME}"
[ -f "${ROOT_DIR}/Dockerfile" ] \
    || die "${ROOT_NAME} at ${ROOT_COMMIT} has no Dockerfile, so there is nothing to build."

# ---------------------------------------------------------------------------
# Drift check: the base image the manifest pins must be the base image the
# fetched Dockerfile actually starts FROM — every stage of it.
# ---------------------------------------------------------------------------
FROM_DIGESTS="$(grep -E '^FROM ' "${ROOT_DIR}/Dockerfile" | sed -n 's/.*@\(sha256:[0-9a-f]\{64\}\).*/\1/p' | sort -u)"
[ -n "${FROM_DIGESTS}" ] \
    || die "the Dockerfile at ${ROOT_COMMIT} pins no base image digest, so the manifest's python_base_digest cannot be checked against it."
if [ "$(echo "${FROM_DIGESTS}" | wc -l | tr -d ' ')" != "1" ]; then
    echo "ERROR: the Dockerfile starts FROM more than one base digest:" >&2
    echo "${FROM_DIGESTS}" | sed 's/^/       /' >&2
    die "the manifest records one base image; fix the Dockerfile or widen the manifest."
fi
[ "${FROM_DIGESTS}" = "${BASE_DIGEST}" ] \
    || die "the manifest pins the base image ${BASE_DIGEST} and the Dockerfile at ${ROOT_COMMIT} starts FROM ${FROM_DIGESTS}. One of them has moved; a release does not guess which."
say "  ok base image ${BASE_DIGEST} agrees with the Dockerfile"

# ---------------------------------------------------------------------------
# Build, from the fresh clones alone.
# ---------------------------------------------------------------------------
BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# The image's "created" label is the date of the commit this release is built
# FROM, not the moment this build happened to run.
#
# The reason is not tidiness. A label is part of the image's configuration, so
# a label that changes every run gives a different image id every run, even
# when every single input is identical — and then two builds of the same
# release cannot be compared by their ids at all. Worse, the build clock is a
# fact about the machine that ran the build, and this path exists to take the
# machine out of the release. The commit date is a fact about the release
# itself, and it is the same on every machine.
RELEASE_DATE="$(git -C "${ROOT_DIR}" show -s --format=%cI HEAD 2>/dev/null)" \
    || die "could not read the date of commit ${ROOT_COMMIT} from the fetched clone."
[ -n "${RELEASE_DATE}" ] \
    || die "the fetched clone of ${ROOT_NAME} gave no date for commit ${ROOT_COMMIT}."

BUILD_ARGS=()
LABEL_ARGS=()
while IFS='|' read -r _tag name url branch commit role; do
    if [ "${role}" = "named-context" ]; then
        [ -d "${TMP}/${name}" ] || die "the build context ${name} is missing from ${TMP}; the fetch above did not leave one."
        BUILD_ARGS+=(--build-context "${name}=${TMP}/${name}")
    fi
    LABEL_ARGS+=(--label "com.guardkit.release.commit.${name}=${commit}")
    LABEL_ARGS+=(--label "com.guardkit.release.source.${name}=${url}")
    LABEL_ARGS+=(--label "com.guardkit.release.branch.${name}=${branch}")
done <<< "${REPO_LINES}"

LABEL_ARGS+=(--label "com.guardkit.release.version=${VERSION}")
LABEL_ARGS+=(--label "com.guardkit.release.manifest.sha256=${MANIFEST_SHA}")
LABEL_ARGS+=(--label "com.guardkit.release.base.digest=${BASE_DIGEST}")
LABEL_ARGS+=(--label "org.opencontainers.image.revision=${ROOT_COMMIT}")
LABEL_ARGS+=(--label "org.opencontainers.image.source=${ROOT_URL}")
LABEL_ARGS+=(--label "org.opencontainers.image.version=${VERSION}")
LABEL_ARGS+=(--label "org.opencontainers.image.created=${RELEASE_DATE}")

IIDFILE="${TMP}/image-id"

say "Building ${TAG_COMMIT} (also tagged ${TAG_VERSION}) from ${TMP} only"
docker buildx build \
    "${BUILD_ARGS[@]}" \
    "${LABEL_ARGS[@]}" \
    -t "${TAG_COMMIT}" \
    -t "${TAG_VERSION}" \
    -f "${ROOT_DIR}/Dockerfile" \
    "${ROOT_DIR}" \
    --build-arg "FORGE_GIT_SHA=${ROOT_COMMIT}" \
    --build-arg "FORGE_GIT_DIRTY=false" \
    --build-arg "PYTHON_BASE_DIGEST=${BASE_DIGEST}" \
    --iidfile "${IIDFILE}" \
    || die "the image build failed. Nothing was tagged."

IMAGE_ID="$(cat "${IIDFILE}")"
say "  built ${IMAGE_ID}"

# ---------------------------------------------------------------------------
# The image's own proof, from the fetched clone — not from any checkout on
# this machine. The proof script compares the code inside the image with the
# tree it was invoked from and then smokes the in-container oracles; invoked
# here, that tree IS the fresh clone at the pinned commit, so the whole proof
# is self-contained.
# ---------------------------------------------------------------------------
if [ "${RUN_PROOF}" = "1" ]; then
    PROOF="${ROOT_DIR}/scripts/verify-forge-oracles.sh"
    [ -x "${PROOF}" ] || [ -f "${PROOF}" ] \
        || die "${ROOT_NAME} at ${ROOT_COMMIT} has no scripts/verify-forge-oracles.sh, so the image cannot prove itself."
    say "Proving ${TAG_COMMIT} against the fetched clone at ${ROOT_DIR}"
    bash "${PROOF}" "${TAG_COMMIT}" \
        || die "the image's own proof failed for ${TAG_COMMIT}. The tags are still on this machine; do not ship it."
fi

# ---------------------------------------------------------------------------
# The receipt: what went in, and what came out.
# ---------------------------------------------------------------------------
[ -n "${RECEIPT}" ] || RECEIPT="./${IMAGE_NAME}-${VERSION}.json"

REPO_DIGESTS="$(docker image inspect "${IMAGE_ID}" --format '{{json .RepoDigests}}')"

{
    printf '{\n'
    # The manifest is named by its file name and its hash, never by where it
    # sat on the machine that ran this. A receipt that carried an absolute
    # path would make the release look like it belonged to one machine, which
    # is the whole thing this path exists to end.
    printf '  "manifest_file": "%s",\n' "$(basename "${MANIFEST}")"
    printf '  "manifest_sha256": "%s",\n' "${MANIFEST_SHA}"
    printf '  "release_version": "%s",\n' "${VERSION}"
    printf '  "release_date": "%s",\n' "${RELEASE_DATE}"
    printf '  "this_build_ran_at_utc": "%s",\n' "${BUILT_AT}"
    printf '  "image_id": "%s",\n' "${IMAGE_ID}"
    printf '  "tags": ["%s", "%s"],\n' "${TAG_COMMIT}" "${TAG_VERSION}"
    printf '  "repo_digests": %s,\n' "${REPO_DIGESTS}"
    printf '  "python_base_digest": "%s",\n' "${BASE_DIGEST}"
    printf '  "pins": {\n'
    first=1
    while IFS='|' read -r _tag name url branch commit role; do
        [ "${first}" = "1" ] || printf ',\n'
        first=0
        printf '    "%s": {"url": "%s", "branch": "%s", "commit": "%s", "role": "%s"}' \
            "${name}" "${url}" "${branch}" "${commit}" "${role}"
    done <<< "${REPO_LINES}"
    printf '\n  },\n'
    printf '  "labels": %s\n' "$(docker image inspect "${IMAGE_ID}" --format '{{json .Config.Labels}}')"
    printf '}\n'
} > "${RECEIPT}"

say "Receipt written to ${RECEIPT}"
say ""
say "Release ${VERSION} built."
say "  image id : ${IMAGE_ID}"
say "  tags     : ${TAG_COMMIT}"
say "             ${TAG_VERSION}"
say "  from     : ${REPO_COUNT} pinned repositories and the base image ${BASE_DIGEST}"
say ""
say "There is no registry digest until this image is pushed; the image id above is"
say "what identifies it on this machine. Reproducing it elsewhere needs this manifest,"
say "Docker, and network access to the repository host."

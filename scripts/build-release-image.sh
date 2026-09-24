#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# scripts/build-release-image.sh — build a release's IMAGES from a manifest,
# from nothing but GitHub.
#
# Written 2026-09-24 for the containerisation rollout gate. Extended the same
# day (stage 2b) from one image to the SET of images a release is made of,
# because the estate runs two: the coordinator's and the publisher's. Until
# then only the coordinator's was ever built, so every proof of the publisher
# had run the coordinator's image with the publisher's start line — a
# stand-in, and the one image holding the credential that can write to a
# project's remote was the one image nobody had built.
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
#     other commit, or if that commit is not on the branch the manifest names
#     (a host serves any commit by its sha, so without that second check the
#     branch written into the image's labels would be a claim nobody tested);
#   * EVERY image the manifest names is built from those clones ALONE — no
#     directory beside this script, or beside the working directory, is read.
#     One run, one set of clones, so the images of a release cannot be built
#     from different code and still carry the same release name;
#   * each resulting image is tagged by the commit it was built from and by
#     the manifest version, and labelled with all five commits, the manifest
#     version, the manifest's own hash and its own ROLE in the release, so any
#     image can say both what it is made of and which of the release's images
#     it is;
#   * no image is EVER tagged `latest`, and none tags over an image that
#     already exists. A floating tag is the thing this whole path is replacing.
#     Every refusal below applies to every image in the set, and it is made
#     for all of them BEFORE anything is fetched or built, so a release never
#     half-lands;
#
# The only things a machine needs to run this: this script, the manifest,
# Docker (with buildx), git, awk, and either sha256sum (Linux) or shasum
# (macOS) for the manifest's own hash. All five repositories are public, so no
# credential is involved; git is run with prompting disabled so that if
# anything ever DOES ask for one, the build stops loudly instead of hanging.
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
#     --skip-proof         skip every image's own proof step. For diagnosis
#                          only; a release is never cut with this.
#     --plan-only          read and check the manifest, print what would be
#                          fetched and built — every image, its file, its role
#                          and its two tags — and stop. Nothing is fetched,
#                          nothing is built, no tag is written.
#
# Exit status is non-zero, with a sentence saying which input was wrong, on any
# drift: a commit that cannot be fetched, a clone at the wrong commit, a commit
# that is not on the branch the manifest names, a branch that does not exist, a
# repository missing from the manifest, a base image digest that no longer
# matches one of the Dockerfiles, or a tag that already exists.
#
# THE TWO MANIFEST SCHEMAS
#
#   schema: 1   names ONE image, in `image_name`. It is read as the
#               coordinator's image, built from the build-context root's own
#               `Dockerfile` and proved by `scripts/verify-forge-oracles.sh` —
#               which is exactly what this script did before it could build a
#               set. A release cut before the publisher's image existed still
#               builds, unchanged.
#   schema: 2   names a SET, under `images:`. Each entry has a name, the
#               dockerfile to build it from (a path inside the build-context
#               root's clone), its role in the release, and optionally the
#               proof script that has to pass before the release is called
#               built. `image_name` stays, and must be the name of the one
#               entry whose role is `coordinator`: it is the image this
#               repository's own operator scripts mean by "the release image".
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
        -h|--help) sed -n '1,92p' "$0"; exit 0 ;;
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
# The manifest's own hash goes into the image's labels. Linux spells the tool
# sha256sum and macOS spells it `shasum -a 256`; both print the same 64
# characters, so either will do and the script says so rather than telling a
# Mac it is missing something.
if command -v sha256sum >/dev/null 2>&1; then
    hash_of() { sha256sum "$1" | cut -c1-64; }
elif command -v shasum >/dev/null 2>&1; then
    hash_of() { shasum -a 256 "$1" | cut -c1-64; }
else
    die "neither sha256sum nor shasum is on PATH, so the manifest's own hash cannot be taken, and the image could not record which manifest it was built from. Install GNU coreutils, or use a machine that has shasum."
fi

MANIFEST_SHA="$(hash_of "${MANIFEST}")"

# ---------------------------------------------------------------------------
# Read the manifest.
#
# Deliberately awk, not a YAML library: the point of this path is that a
# second machine needs the manifest, Docker and GitHub and nothing else. The
# manifest is a small flat file this repository owns, so a small strict reader
# is honest rather than clever. Anything the reader does not recognise is
# reported by name rather than ignored.
# ---------------------------------------------------------------------------
#
# TWO lists, not one, since stage 2b: `repositories:` (what goes in) and
# `images:` (what comes out). They are read by the same three rules — a header
# on its own line, an entry that starts with a dash, and the entry's further
# keys indented under it — with a different set of permitted keys each. A key
# that belongs to the other list is reported by name rather than ignored, so
# an `images:` entry cannot quietly carry a `commit:` and look pinned.
PARSED="$(
    awk '
        function trim(s) { sub(/^[ \t]+/, "", s); sub(/[ \t]+$/, "", s); return s }
        function flush_item() {
            if (!have_item) return
            if (cur_list == "repositories")
                printf "REPO|%s|%s|%s|%s|%s\n", f_name, f_url, f_branch, f_commit, f_role
            else if (cur_list == "images")
                printf "IMAGE|%s|%s|%s|%s\n", f_name, f_dockerfile, f_role, f_proof
            have_item = 0
            f_name = ""; f_url = ""; f_branch = ""; f_commit = ""; f_role = ""
            f_dockerfile = ""; f_proof = ""
        }
        function setkey(key, val) {
            if (cur_list == "repositories") {
                if (key == "name") f_name = val
                else if (key == "url") f_url = val
                else if (key == "branch") f_branch = val
                else if (key == "commit") f_commit = val
                else if (key == "role") f_role = val
                else printf "UNKNOWN|%s|%s\n", cur_list, key
            } else if (cur_list == "images") {
                if (key == "name") f_name = val
                else if (key == "dockerfile") f_dockerfile = val
                else if (key == "role") f_role = val
                else if (key == "proof") f_proof = val
                else printf "UNKNOWN|%s|%s\n", cur_list, key
            } else {
                printf "UNKNOWN|%s|%s\n", cur_list, key
            }
        }
        {
            line = $0
            sub(/[ \t]*#.*$/, "", line)          # strip comments
            if (trim(line) == "") next
        }
        # top-level "key: value"
        line ~ /^[a-z_]+:[ \t]*[^ \t]/ {
            flush_item()
            cur_list = ""
            k = line; sub(/:.*$/, "", k)
            v = line; sub(/^[a-z_]+:[ \t]*/, "", v)
            printf "TOP|%s|%s\n", trim(k), trim(v)
            next
        }
        # a list header, on a line of its own
        line ~ /^[a-z_]+:[ \t]*$/ {
            flush_item()
            cur_list = line; sub(/:.*$/, "", cur_list); cur_list = trim(cur_list)
            if (cur_list != "repositories" && cur_list != "images")
                printf "UNKNOWNLIST|%s\n", cur_list
            next
        }
        # a new list entry
        cur_list != "" && line ~ /^[ \t]*-[ \t]*[a-z_]+:/ {
            flush_item()
            have_item = 1
            e = line; sub(/^[ \t]*-[ \t]*/, "", e); e = trim(e)
            k = e; sub(/:.*$/, "", k); k = trim(k)
            v = e; sub(/^[a-z_]+:[ \t]*/, "", v); v = trim(v)
            setkey(k, v)
            next
        }
        # a continuation key of the current list entry
        cur_list != "" && have_item && line ~ /^[ \t]+[a-z_]+:/ {
            e = trim(line)
            k = e; sub(/:.*$/, "", k); k = trim(k)
            v = e; sub(/^[a-z_]+:[ \t]*/, "", v); v = trim(v)
            setkey(k, v)
            next
        }
        { printf "UNPARSED|%s\n", trim(line) }
        END { flush_item() }
    ' "${MANIFEST}"
)" || die "the manifest at ${MANIFEST} could not be read."

if echo "${PARSED}" | grep -q '^UNPARSED'; then
    echo "ERROR: the manifest at ${MANIFEST} has lines this reader does not understand:" >&2
    echo "${PARSED}" | awk -F'[|]' '$1 == "UNPARSED" { print "       " $2 }' >&2
    die "fix the manifest, or the reader, before building a release from it."
fi
if echo "${PARSED}" | grep -q '^UNKNOWNLIST'; then
    echo "ERROR: the manifest at ${MANIFEST} has lists this reader does not understand:" >&2
    echo "${PARSED}" | awk -F'[|]' '$1 == "UNKNOWNLIST" { print "       " $2 ":" }' >&2
    die "the two lists are 'repositories:' (what goes into the release) and 'images:' (what comes out of it)."
fi
if echo "${PARSED}" | grep -q '^UNKNOWN|'; then
    echo "ERROR: the manifest names keys this reader does not understand:" >&2
    echo "${PARSED}" | awk -F'[|]' '$1 == "UNKNOWN" { print "       " $3 "  (under " $2 ":)" }' >&2
    die "a repository has name, url, branch, commit and role; an image has name, dockerfile, role and an optional proof."
fi

top() { echo "${PARSED}" | awk -F'[|]' -v k="$2" '$1 == "TOP" && $2 == k { print $3; exit }'; }

SCHEMA="$(top _ schema)"
VERSION="$(top _ version)"
IMAGE_NAME="$(top _ image_name)"
BASE_DIGEST="$(top _ python_base_digest)"

[ -n "${VERSION}" ] || die "the manifest has no 'version' line, so the images would have no release name."
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

# ---------------------------------------------------------------------------
# The images this release is made of.
#
# Schema 1 named one, in `image_name`, and built it from the build-context
# root's own Dockerfile. That is still exactly what a schema 1 manifest means,
# written out here rather than assumed further down, so the rest of this
# script has ONE shape to handle and an old manifest keeps building.
# ---------------------------------------------------------------------------
IMAGE_LINES="$(echo "${PARSED}" | awk -F'[|]' '$1 == "IMAGE"')"

case "${SCHEMA}" in
    1)
        [ -z "${IMAGE_LINES}" ] \
            || die "the manifest says schema 1 and also carries an 'images:' list. Schema 1 names exactly one image, in 'image_name'; a manifest that names the set of images a release builds is schema 2."
        IMAGE_LINES="IMAGE|${IMAGE_NAME}|Dockerfile|coordinator|scripts/verify-forge-oracles.sh"
        ;;
    2)
        [ -n "${IMAGE_LINES}" ] \
            || die "the manifest says schema 2 and names no images. Schema 2 lists every image the release builds under 'images:'."
        ;;
    *)
        die "the manifest says schema '${SCHEMA}'; this script reads schema 1 (one image, named by 'image_name') and schema 2 (a set of images, listed under 'images:')."
        ;;
esac

COORDINATOR_NAME=""
SEEN_IMAGE_NAMES=""

while IFS='|' read -r _tag iname idockerfile irole iproof; do
    [ -n "${iname}" ] || die "an image entry in the manifest has no name."
    [ -n "${idockerfile}" ] \
        || die "image '${iname}' has no dockerfile, so this script would not know what to build for it. It is a path inside the fetched clone of ${ROOT_NAME}, for example Dockerfile."
    [ -n "${irole}" ] \
        || die "image '${iname}' has no role, so the image could not say which of the release's images it is."
    case "${idockerfile}" in
        /*) die "image '${iname}' names the dockerfile '${idockerfile}'. It is a path INSIDE the fetched clone, so it is relative — an absolute path would be a path on whichever machine ran the build." ;;
        *..*) die "image '${iname}' names the dockerfile '${idockerfile}', which climbs out of the fetched clone. Everything a release is built from is inside the clones this run fetched." ;;
    esac
    case "${iproof}" in
        "") ;;
        /*) die "image '${iname}' names the proof '${iproof}'. Like the dockerfile it is a path inside the fetched clone, so it is relative." ;;
        *..*) die "image '${iname}' names the proof '${iproof}', which climbs out of the fetched clone." ;;
    esac
    # A role is a plain lower-case word. It goes into a label and into the
    # receipt, so it says which of the release's images this is and nothing
    # more: no spaces, no path, nothing that could carry a machine's anything.
    case "${irole}" in
        *[!a-z0-9-]*|-*|*-) die "image '${iname}' has role '${irole}'. A role is a plain lower-case word saying which of the release's images this is, for example coordinator or publisher." ;;
    esac
    case " ${SEEN_IMAGE_NAMES} " in
        *" ${iname} "*) die "two images in the manifest are both called '${iname}'. Each image in a release has a name of its own; two of them under one name would tag over each other." ;;
    esac
    SEEN_IMAGE_NAMES="${SEEN_IMAGE_NAMES} ${iname}"
    if [ "${irole}" = "coordinator" ]; then
        [ -z "${COORDINATOR_NAME}" ] \
            || die "two images claim role coordinator: ${COORDINATOR_NAME} and ${iname}. One image of a release is the one this repository's own operator scripts mean by 'the release image', and 'image_name' names it."
        COORDINATOR_NAME="${iname}"
    fi
done <<< "${IMAGE_LINES}"

[ -n "${COORDINATOR_NAME}" ] \
    || die "no image in the manifest has role coordinator, so nothing in the release answers to 'image_name'."
[ "${COORDINATOR_NAME}" = "${IMAGE_NAME}" ] \
    || die "the manifest's image_name is '${IMAGE_NAME}' and the image whose role is coordinator is called '${COORDINATOR_NAME}'. They are the same image said twice, so they have to agree: ops/forge-prod-recreate.sh reads image_name to name the release it expects."

REPO_COUNT="$(echo "${REPO_LINES}" | wc -l | tr -d ' ')"
IMAGE_COUNT="$(echo "${IMAGE_LINES}" | wc -l | tr -d ' ')"
say "Release ${VERSION} — ${REPO_COUNT} repositories, ${IMAGE_COUNT} images, manifest ${MANIFEST_SHA}"

# ---------------------------------------------------------------------------
# The tags, decided for EVERY image before anything is fetched or built.
#
# All of them are checked first, and the run stops on the first one that is
# wrong, so a release never lands half of its images and then refuses. The two
# tags an image gets are the commit the release was built from and the release
# version; the commit tag is shared by every image of the release, which is
# what says they were built together.
# ---------------------------------------------------------------------------
tags_of() { echo "$1:${ROOT_COMMIT} $1:${VERSION}"; }

while IFS='|' read -r _tag iname idockerfile irole iproof; do
    for t in $(tags_of "${iname}"); do
        case "${t}" in
            *:latest) die "this script will not produce a '${t}' tag. A release is named by the commit it was built from; a floating tag is what this path replaces." ;;
        esac
        if docker image inspect "${t}" >/dev/null 2>&1 </dev/null; then
            [ "${ALLOW_EXISTING_TAG}" = "1" ] \
                || die "the tag ${t} already exists on this machine. A release tag is written once, and moving it would change what that name means for anything already using it. Delete it deliberately, or pass --allow-existing-tag if you meant to rebuild it."
        fi
    done
done <<< "${IMAGE_LINES}"

if [ "${PLAN_ONLY}" = "1" ]; then
    echo "Release ${VERSION} (manifest ${MANIFEST_SHA}, schema ${SCHEMA})"
    echo "  base image: ${BASE_DIGEST}"
    echo "  would build ${IMAGE_COUNT} image(s), all from the same fetched clones:"
    while IFS='|' read -r _tag iname idockerfile irole iproof; do
        echo "    ${iname} [${irole}] from ${ROOT_NAME}/${idockerfile}"
        for t in $(tags_of "${iname}"); do
            echo "      would tag : ${t}"
        done
        if [ -n "${iproof}" ]; then
            echo "      proved by : ${ROOT_NAME}/${iproof}"
        else
            echo "      proved by : nothing — this image names no proof"
        fi
    done <<< "${IMAGE_LINES}"
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

# Is the pinned commit actually ON the branch the manifest says it came from?
#
# This is not the same question as "does the commit exist". A host serves any
# commit it can reach by its sha, whatever branch it sits on, so without this
# check a commit from a side branch — or one that has since left main — would
# be fetched, would match its pin, and would be labelled `branch: main` as
# though that had been confirmed. Nothing untrue about the image's CONTENTS
# could get through that way; an untrue branch label could, and a label nobody
# checks is worse than no label.
#
# The cheap case is the common one: the pin IS the branch tip, which one
# ls-remote settles without fetching anything more. Otherwise the branch is
# deepened a step at a time until the commit is inside what was fetched, and
# merge-base answers. (A shallow clone cannot answer this: history stops at the
# graft, and everything beyond it looks unrelated. Hence the deepening.)
verify_on_branch() {
    local name="$1" url="$2" branch="$3" commit="$4" dest="$5"
    local tip depth
    tip="$(git -C "${dest}" ls-remote origin "refs/heads/${branch}" 2>/dev/null | awk 'NR==1 {print $1}')" \
        || die "could not ask ${url} which commit ${branch} is at, so the manifest's branch for ${name} could not be checked."
    [ -n "${tip}" ] \
        || die "the manifest says ${name}'s commit came from branch ${branch}, and ${url} has no branch of that name. A release records where each commit came from; it does not guess."
    if [ "${tip}" = "${commit}" ]; then
        say "    ok ${name} ${commit} is the tip of ${branch}"
        return 0
    fi
    for depth in 50 500 5000 2147483647; do
        git -C "${dest}" fetch --quiet --depth "${depth}" --no-tags origin \
            "+refs/heads/${branch}:refs/remotes/origin/${branch}" 2>/dev/null || true
        if git -C "${dest}" merge-base --is-ancestor "${commit}" "refs/remotes/origin/${branch}" 2>/dev/null; then
            say "    ok ${name} ${commit} is on ${branch}, behind its tip ${tip}"
            return 0
        fi
    done
    die "the manifest says ${name}'s commit ${commit} came from ${branch}, and it is not on that branch at ${url} (${branch} is at ${tip}). The commit itself exists — the host served it — so it sits on some other branch, or it has since left ${branch}. Refusing to build: the image would carry a label saying ${branch} for code that did not come from there."
}

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
    verify_on_branch "${name}" "${url}" "${branch}" "${commit}" "${dest}"
}

say "Fetching the pinned commits into ${TMP}"
while IFS='|' read -r _tag name url branch commit role; do
    fetch_pinned "${name}" "${url}" "${branch}" "${commit}" "${TMP}/${name}"
done <<< "${REPO_LINES}"

ROOT_DIR="${TMP}/${ROOT_NAME}"

# Every image's dockerfile and every image's proof has to be IN the fetched
# clone, and this is checked for all of them before the first build, so a
# manifest that names a file the pin does not carry stops before anything is
# tagged rather than after the first image has landed.
while IFS='|' read -r _tag iname idockerfile irole iproof; do
    [ -f "${ROOT_DIR}/${idockerfile}" ] \
        || die "image '${iname}' is built from ${idockerfile}, and ${ROOT_NAME} at ${ROOT_COMMIT} has no such file. The dockerfile comes from the fetched clone at the pin, never from a checkout beside this script."
    if [ -n "${iproof}" ]; then
        [ -f "${ROOT_DIR}/${iproof}" ] \
            || die "image '${iname}' names the proof ${iproof}, and ${ROOT_NAME} at ${ROOT_COMMIT} has no such file. A release proves itself with the code it is made of."
    fi
done <<< "${IMAGE_LINES}"

# ---------------------------------------------------------------------------
# Drift check: the base image the manifest pins must be the base image EVERY
# fetched Dockerfile actually starts FROM — every stage of every one of them.
#
# One manifest line, every image: the images of a release share a base, and an
# image that quietly started from another one would be a second supply chain
# inside a release that claims to have one.
# ---------------------------------------------------------------------------
while IFS='|' read -r _tag iname idockerfile irole iproof; do
    FROM_DIGESTS="$(grep -E '^FROM ' "${ROOT_DIR}/${idockerfile}" | sed -n 's/.*@\(sha256:[0-9a-f]\{64\}\).*/\1/p' | sort -u)"
    [ -n "${FROM_DIGESTS}" ] \
        || die "${idockerfile} at ${ROOT_COMMIT} (image '${iname}') pins no base image digest, so the manifest's python_base_digest cannot be checked against it."
    if [ "$(echo "${FROM_DIGESTS}" | wc -l | tr -d ' ')" != "1" ]; then
        echo "ERROR: ${idockerfile} (image '${iname}') starts FROM more than one base digest:" >&2
        echo "${FROM_DIGESTS}" | sed 's/^/       /' >&2
        die "the manifest records one base image; fix the Dockerfile or widen the manifest."
    fi
    [ "${FROM_DIGESTS}" = "${BASE_DIGEST}" ] \
        || die "the manifest pins the base image ${BASE_DIGEST} and ${idockerfile} at ${ROOT_COMMIT} (image '${iname}') starts FROM ${FROM_DIGESTS}. One of them has moved; a release does not guess which."
    say "  ok base image ${BASE_DIGEST} agrees with ${idockerfile}"
done <<< "${IMAGE_LINES}"

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

# What each image turned into. One line per image, written as it is built and
# read again for the receipt, so the receipt cannot say a different set than
# the run produced.
BUILT="${TMP}/built-images"
: > "${BUILT}"

# ---------------------------------------------------------------------------
# One build per image, ALL FROM THE SAME FETCHED CLONES.
#
# Same context root, same named contexts, same labels, same build arguments —
# only the dockerfile, the tags and the role label differ. That is the whole
# of what makes the images of a release a set rather than a coincidence: they
# cannot be built from different code, because there is only one copy of the
# code in this run and it is thrown away at the end.
#
# The named contexts are offered to every image. BuildKit resolves a named
# context only where a stage actually names it, so an image that uses none
# (the publisher's does not) neither reads nor carries them.
# ---------------------------------------------------------------------------
while IFS='|' read -r _tag iname idockerfile irole iproof; do
    TAG_COMMIT="${iname}:${ROOT_COMMIT}"
    TAG_VERSION="${iname}:${VERSION}"
    IIDFILE="${TMP}/image-id-${iname}"

    say "Building ${TAG_COMMIT} (also tagged ${TAG_VERSION}) from ${ROOT_NAME}/${idockerfile} in ${TMP} only"
    docker buildx build \
        "${BUILD_ARGS[@]}" \
        "${LABEL_ARGS[@]}" \
        --label "com.guardkit.release.image.role=${irole}" \
        --label "com.guardkit.release.image.name=${iname}" \
        --label "com.guardkit.release.image.dockerfile=${idockerfile}" \
        --label "org.opencontainers.image.title=${iname}" \
        -t "${TAG_COMMIT}" \
        -t "${TAG_VERSION}" \
        -f "${ROOT_DIR}/${idockerfile}" \
        "${ROOT_DIR}" \
        --build-arg "FORGE_GIT_SHA=${ROOT_COMMIT}" \
        --build-arg "FORGE_GIT_DIRTY=false" \
        --build-arg "PYTHON_BASE_DIGEST=${BASE_DIGEST}" \
        --iidfile "${IIDFILE}" \
        </dev/null \
        || die "the build of image '${iname}' failed. Anything built before it in this run is still tagged on this machine; the release is NOT built."

    ibuilt="$(cat "${IIDFILE}")"
    say "  built ${iname} ${ibuilt}"
    printf '%s|%s|%s|%s|%s|%s\n' "${iname}" "${irole}" "${idockerfile}" "${ibuilt}" "${TAG_COMMIT}" "${TAG_VERSION}" >> "${BUILT}"
done <<< "${IMAGE_LINES}"

# ---------------------------------------------------------------------------
# Each image's own proof, from the fetched clone — not from any checkout on
# this machine. A proof script is given the tag and compares the code inside
# that image with the tree it was invoked from, then smokes what that image is
# for; invoked here, that tree IS the fresh clone at the pinned commit, so the
# whole proof is self-contained.
#
# The proofs run AFTER every image is built, not between builds, because a
# release is proved as a set: a machine with the coordinator's image proved
# and the publisher's unbuilt is the arrangement this stage exists to end.
# ---------------------------------------------------------------------------
if [ "${RUN_PROOF}" = "1" ]; then
    while IFS='|' read -r iname irole idockerfile ibuilt itagcommit itagversion; do
        iproof="$(echo "${IMAGE_LINES}" | awk -F'[|]' -v n="${iname}" '$2 == n { print $5; exit }')"
        if [ -z "${iproof}" ]; then
            say "Image '${iname}' names no proof, so nothing was proved about it."
            continue
        fi
        say "Proving ${itagcommit} with ${ROOT_NAME}/${iproof}, against the fetched clone at ${ROOT_DIR}"
        bash "${ROOT_DIR}/${iproof}" "${itagcommit}" </dev/null \
            || die "the proof of image '${iname}' failed for ${itagcommit}. Every tag of this run is still on this machine; do not ship any of them."
    done < "${BUILT}"
fi

# ---------------------------------------------------------------------------
# The receipt: what went in, and what came out — EVERY image of the release,
# each with its own id, its own two tags and its own labels.
#
# The coordinator's id, tags, digests and labels are ALSO written at the top
# level, under the same names they have always had, because that is where
# everything that reads a receipt today looks for "the release image". They
# are the same facts as the coordinator's entry in "images", said twice on
# purpose rather than moved.
# ---------------------------------------------------------------------------
[ -n "${RECEIPT}" ] || RECEIPT="./${IMAGE_NAME}-${VERSION}.json"

coordinator_field() {
    awk -F'[|]' -v n="${IMAGE_NAME}" -v f="$1" '$1 == n { print $f; exit }' "${BUILT}"
}
COORDINATOR_ID="$(coordinator_field 4)"
COORDINATOR_TAG_COMMIT="$(coordinator_field 5)"
COORDINATOR_TAG_VERSION="$(coordinator_field 6)"
[ -n "${COORDINATOR_ID}" ] \
    || die "the coordinator image '${IMAGE_NAME}' is not among the images this run built, so no receipt can be written."

{
    printf '{\n'
    # The manifest is named by its file name and its hash, never by where it
    # sat on the machine that ran this. A receipt that carried an absolute
    # path would make the release look like it belonged to one machine, which
    # is the whole thing this path exists to end.
    printf '  "manifest_file": "%s",\n' "$(basename "${MANIFEST}")"
    printf '  "manifest_sha256": "%s",\n' "${MANIFEST_SHA}"
    printf '  "manifest_schema": "%s",\n' "${SCHEMA}"
    printf '  "release_version": "%s",\n' "${VERSION}"
    printf '  "release_date": "%s",\n' "${RELEASE_DATE}"
    printf '  "this_build_ran_at_utc": "%s",\n' "${BUILT_AT}"
    printf '  "image_id": "%s",\n' "${COORDINATOR_ID}"
    printf '  "tags": ["%s", "%s"],\n' "${COORDINATOR_TAG_COMMIT}" "${COORDINATOR_TAG_VERSION}"
    printf '  "repo_digests": %s,\n' "$(docker image inspect "${COORDINATOR_ID}" --format '{{json .RepoDigests}}')"
    printf '  "images": [\n'
    first=1
    while IFS='|' read -r iname irole idockerfile ibuilt itagcommit itagversion; do
        [ "${first}" = "1" ] || printf ',\n'
        first=0
        printf '    {"name": "%s", "role": "%s", "dockerfile": "%s", "image_id": "%s", "tags": ["%s", "%s"], "repo_digests": %s, "labels": %s}' \
            "${iname}" "${irole}" "${idockerfile}" "${ibuilt}" "${itagcommit}" "${itagversion}" \
            "$(docker image inspect "${ibuilt}" --format '{{json .RepoDigests}}')" \
            "$(docker image inspect "${ibuilt}" --format '{{json .Config.Labels}}')"
    done < "${BUILT}"
    printf '\n  ],\n'
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
    printf '  "labels": %s\n' "$(docker image inspect "${COORDINATOR_ID}" --format '{{json .Config.Labels}}')"
    printf '}\n'
} > "${RECEIPT}"

say "Receipt written to ${RECEIPT}"
say ""
say "Release ${VERSION} built — ${IMAGE_COUNT} image(s)."
while IFS='|' read -r iname irole idockerfile ibuilt itagcommit itagversion; do
    say "  ${iname} [${irole}]"
    say "    image id : ${ibuilt}"
    say "    tags     : ${itagcommit}"
    say "               ${itagversion}"
done < "${BUILT}"
say "  from     : ${REPO_COUNT} pinned repositories and the base image ${BASE_DIGEST}"
say ""
say "There is no registry digest until an image is pushed; the ids above are"
say "what identify them on this machine. Reproducing them elsewhere needs this manifest,"
say "Docker, and network access to the repository host."

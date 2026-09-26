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
#                          nothing is built, no tag is written. It is READ-ONLY
#                          and needs no override flag: a tag that already exists
#                          is REPORTED in the plan ("a real build would refuse")
#                          rather than refusing the plan. Until 25 September
#                          2026 the existing-tag check sat before this branch,
#                          so anybody who had already built a release had to
#                          pass --allow-existing-tag to read a plan — and the
#                          cost of that is people learning to reach for the
#                          override flag out of habit.
#
# Exit status is non-zero, with a sentence saying which input was wrong, on any
# drift: a commit that cannot be fetched, a clone at the wrong commit, a commit
# that is not on the branch the manifest names, a branch that does not exist, a
# repository missing from the manifest, a base image digest that no longer
# matches one of the Dockerfiles, a tag that already exists, or a built image
# carrying one of this machine's own names (see THE SWEEP below).
#
# THE SWEEP: NOTHING OF THE BUILDING MACHINE GOES OUT IN A RELEASE IMAGE
#
# Rich, on containerisation: a machine's name as a default value is the worst
# form of this defect. It is not theoretical here — on 25 September 2026 a
# developer's compiled files rode into an image carrying the absolute path of
# the machine that compiled them, and the jarvis package's own settings module
# hard-coded a host name as a default, so that name was in a PUBLIC image.
# Both were found by sweeping an image BY HAND. Each image's own proof script
# reads the image's CONFIGURATION and its labels, which is a different and much
# smaller question, and both of those hits were in the FILESYSTEM.
#
# So, after each image is built, this script searches that image with
# /bin/grep -F — its config, its labels, its history, its flattened filesystem
# (docker create + docker export) and EVERY LAYER A PUSH WOULD SEND
# (docker save, every blob unpacked). A hit REFUSES the release and names the
# image and the file — and says WHICH of the words matched by its position in
# RELEASE_SWEEP_TERMS rather than printing it, because a build log is kept and
# the word is one of this machine's own names. The tags this run wrote are
# removed, and so they are on any other unfinished ending.
#
# WHY THE LAYERS AND NOT ONLY THE FILESYSTEM. An export is the flattened FINAL
# filesystem: a file a Dockerfile copies in and a later step deletes is gone
# from it and still in the image, because the layer holding it is still one of
# the image's layers and is still what a save or a registry push sends. Found
# by measurement on 25 September 2026: the jarvis image copied another
# repository's whole clone to /tmp and deleted it four lines later, and its
# export carried this estate's account name in 0 files while its own layers
# carried it in 2,194. A check that says a clean thing about a dirty image is
# worse than no check.
#
# WHERE THE WORDS COME FROM, AND WHY NOT FROM A FILE HERE. A tracked list of
# this machine's names would BE the defect it is looking for — a real host name
# and a real user name, written into a public repository, so that a checker can
# look for them. So the words come from the MACHINE, at build time, in the
# environment variable RELEASE_SWEEP_TERMS: space-separated, whatever this
# operator's machine, account, home directory and projects folder are called.
# The estate's .env.example carries the NAME with a comment and NO VALUE.
#
# WITH RELEASE_SWEEP_TERMS UNSET, NO SWEEP RUNS, and this script says so in
# plain words rather than printing a tick. A release built without it is not
# swept, and the operator can see that it was not.
#
# THE EXCEPTIONS ARE NAMED BY PATH, NEVER BY WORD. A file may legitimately
# contain one of these words — today guardkit's own feature-plan.md carries
# this estate's host names as the PATTERNS its live-infrastructure detector
# looks FOR, and removing them would switch a check off rather than make
# anything portable. The manifest lists such files under `sweep_exceptions:`,
# by image and by path, each with its reason. A hit in one of those files is
# REPORTED and allowed; a hit anywhere else refuses. An exception cannot be
# written as "allow this word", because that would put the word back into a
# tracked file.
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
#               dockerfile to build it from (a path inside the clone of the
#               repository named by its `context`, which defaults to the
#               build-context root), its role in the release, and optionally
#               the proof script that has to pass before the release is called
#               built. `image_name` stays, and must be the name of the one
#               entry whose role is `coordinator`: it is the image this
#               repository's own operator scripts mean by "the release image".
#
# PER-IMAGE CONTEXT ROOTS (25 September 2026, stage 4b of the rollout gate).
# Until this pass every image of a release was built from the clone of the ONE
# repository the release is cut from, so a service whose code lives in another
# repository could not be a release image at all — which is why the memory
# service and its relay were outside the release and the bus still is. An image
# entry may now name `context: <repository>`, one of the manifest's own
# repositories, and it is built from THAT clone, at THAT pin. Two things follow
# and both are deliberate:
#
#   * an image's commit tag is the commit of the repository IT was built from,
#     not the release's root commit. `forge:<forge commit>` means what it always
#     meant; `fleet-memory-mcp:<fleet-memory commit>` means the same kind of
#     thing about the repository that image really comes from. The release
#     VERSION tag is what says they were built together, and every image still
#     carries every repository's commit in its labels;
#   * a proof script is still a path inside the BUILD-CONTEXT ROOT's clone. The
#     proofs belong to the release — they are how this repository satisfies
#     itself about an image before shipping it — and asking another repository
#     to carry the factory's proof script would put the factory into it.
#
# The one base image digest still applies to every Dockerfile of the release,
# whichever repository it came from: a release has one base, or it has two
# supply chains and one name.
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
# The tags this run writes, and the ones that were already on this machine
# before it started. A refused sweep removes the first and never touches the
# second — see remove_this_runs_tags below.
RUN_TAGS=""
PRE_EXISTING_TAGS=""
# Whether the tags this run wrote have already been taken back, and whether
# this run got far enough to be allowed to keep them. Both are read by the
# EXIT trap installed further down, which is what makes the clean-up cover
# EVERY unsuccessful ending and not only the ones that call die().
TAGS_ALREADY_REMOVED=0
BUILD_SUCCEEDED=0

die() {
    echo "ERROR: $*" >&2
    # The tags are taken back by the EXIT trap, whatever the ending — a die(),
    # an ordinary failing command under `set -e`, or an interrupt. Until 26
    # September 2026 this removal lived HERE, so a failure that did not route
    # through die() left both tags behind: Codex's review of that day wrote a
    # clean image set and then failed on the receipt, and the tags stayed.
    exit 1
}
say() { echo "$*" >&2; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --keep-temp) KEEP_TEMP=1; shift ;;
        --allow-existing-tag) ALLOW_EXISTING_TAG=1; shift ;;
        --skip-proof) RUN_PROOF=0; shift ;;
        --plan-only) PLAN_ONLY=1; shift ;;
        --receipt) [ "$#" -ge 2 ] || die "--receipt needs a path"; RECEIPT="$2"; shift 2 ;;
        -h|--help) sed -n '1,142p' "$0"; exit 0 ;;
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
# THREE lists: `repositories:` (what goes in), `images:` (what comes out) and
# `sweep_exceptions:` (the named files a sweep hit is allowed in). They are
# read by the same three rules — a header on its own line, an entry that starts
# with a dash, and the entry's further keys indented under it — with a
# different set of permitted keys each. A key that belongs to another list is
# reported by name rather than ignored, so an `images:` entry cannot quietly
# carry a `commit:` and look pinned.
#
# A `sweep_exceptions:` entry has `image`, `path` and `reason`. It says "a hit
# in THIS file of THIS image is expected, for this stated reason" — never "this
# word is allowed", which would put a machine's name back into a tracked file.
PARSED="$(
    awk '
        function trim(s) { sub(/^[ \t]+/, "", s); sub(/[ \t]+$/, "", s); return s }
        function flush_item() {
            if (!have_item) return
            if (cur_list == "repositories")
                printf "REPO|%s|%s|%s|%s|%s\n", f_name, f_url, f_branch, f_commit, f_role
            else if (cur_list == "images")
                printf "IMAGE|%s|%s|%s|%s|%s\n", f_name, f_dockerfile, f_role, f_proof, f_context
            else if (cur_list == "sweep_exceptions")
                printf "SWEEPOK|%s|%s|%s\n", f_image, f_path, f_reason
            have_item = 0
            f_name = ""; f_url = ""; f_branch = ""; f_commit = ""; f_role = ""
            f_dockerfile = ""; f_proof = ""; f_context = ""
            f_image = ""; f_path = ""; f_reason = ""
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
                else if (key == "context") f_context = val
                else printf "UNKNOWN|%s|%s\n", cur_list, key
            } else if (cur_list == "sweep_exceptions") {
                if (key == "image") f_image = val
                else if (key == "path") f_path = val
                else if (key == "reason") f_reason = val
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
            if (cur_list != "repositories" && cur_list != "images" && cur_list != "sweep_exceptions")
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
    die "the three lists are 'repositories:' (what goes into the release), 'images:' (what comes out of it) and 'sweep_exceptions:' (the named files a sweep hit is allowed in)."
fi
if echo "${PARSED}" | grep -q '^UNKNOWN|'; then
    echo "ERROR: the manifest names keys this reader does not understand:" >&2
    echo "${PARSED}" | awk -F'[|]' '$1 == "UNKNOWN" { print "       " $3 "  (under " $2 ":)" }' >&2
    die "a repository has name, url, branch, commit and role; an image has name, dockerfile, role, an optional context and an optional proof; a sweep exception has image, path and reason."
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
            ROOT_NAME="${name}"; ROOT_COMMIT="${commit}"
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
        IMAGE_LINES="IMAGE|${IMAGE_NAME}|Dockerfile|coordinator|scripts/verify-forge-oracles.sh|"
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

# One repository's pinned commit, by name. Used for an image's commit tag,
# which is the commit of the repository THAT image was built from.
commit_of_repository() {
    echo "${REPO_LINES}" | awk -F'[|]' -v n="$1" '$2 == n { print $5; exit }'
}

# One repository's address, by name. Used for an image's own
# org.opencontainers.image.source, which — like its commit tag — names the
# repository THAT image was built from. See the labels section below.
url_of_repository() {
    echo "${REPO_LINES}" | awk -F'[|]' -v n="$1" '$2 == n { print $3; exit }'
}

# Which repository's clone an image is built from. Empty means the
# build-context root, which is what every image meant before per-image
# contexts existed and what a schema 1 manifest still means.
context_of_image() {
    if [ -z "$1" ]; then printf '%s' "${ROOT_NAME}"; else printf '%s' "$1"; fi
}

while IFS='|' read -r _tag iname idockerfile irole iproof icontext; do
    [ -n "${iname}" ] || die "an image entry in the manifest has no name."
    icontext="$(context_of_image "${icontext}")"
    [ -n "$(commit_of_repository "${icontext}")" ] \
        || die "image '${iname}' says it is built from the repository '${icontext}', and the manifest names no repository of that name. An image's context is one of the repositories this release pins, so the image can be built from a clone at a pin and tagged by that pin's commit."
    [ -n "${idockerfile}" ] \
        || die "image '${iname}' has no dockerfile, so this script would not know what to build for it. It is a path inside the fetched clone of ${icontext}, for example Dockerfile."
    [ -n "${irole}" ] \
        || die "image '${iname}' has no role, so the image could not say which of the release's images it is."
    case "${idockerfile}" in
        /*) die "image '${iname}' names the dockerfile '${idockerfile}'. It is a path INSIDE the fetched clone of ${icontext}, so it is relative — an absolute path would be a path on whichever machine ran the build." ;;
        *..*) die "image '${iname}' names the dockerfile '${idockerfile}', which climbs out of the fetched clone. Everything a release is built from is inside the clones this run fetched." ;;
    esac
    case "${iproof}" in
        "") ;;
        /*) die "image '${iname}' names the proof '${iproof}'. Like the dockerfile it is a path inside a fetched clone, so it is relative." ;;
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

# ---------------------------------------------------------------------------
# The files a sweep hit is allowed in — by image and by PATH, never by word.
#
# Checked here, with everything else, so a manifest naming an exception for an
# image the release does not build is a refusal at the door rather than a line
# nobody ever reads. Each entry must carry its reason: an exception that cannot
# say why it exists is one nobody can review later.
# ---------------------------------------------------------------------------
SWEEP_OK_LINES="$(echo "${PARSED}" | awk -F'[|]' '$1 == "SWEEPOK"')"
if [ -n "${SWEEP_OK_LINES}" ]; then
    while IFS='|' read -r _tag simage spath sreason; do
        [ -n "${simage}" ] \
            || die "a sweep_exceptions entry in the manifest names no image, so nothing would know which image the exception belongs to."
        [ -n "${spath}" ] \
            || die "the sweep exception for image '${simage}' names no path. An exception is a named FILE, never a word — allowing a word would put a machine's name back into this file."
        [ -n "${sreason}" ] \
            || die "the sweep exception for '${simage}' at '${spath}' gives no reason. An exception that cannot say why it exists is one nobody can review later."
        case " ${SEEN_IMAGE_NAMES} " in
            *" ${simage} "*) ;;
            *) die "the manifest has a sweep exception for an image called '${simage}', and this release builds no image of that name." ;;
        esac
    done <<< "${SWEEP_OK_LINES}"
fi

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
#
# AN IMAGE'S COMMIT TAG IS ITS OWN REPOSITORY'S COMMIT, since per-image
# contexts (25 September 2026). For every image whose context is the
# build-context root — which is every image of every release cut before that —
# this is exactly what it was.
tags_of() { echo "$1:$2 $1:${VERSION}"; }

# A FLOATING TAG IS REFUSED WHATEVER THE MODE, INCLUDING A PLAN. It is a fact
# about the manifest — this release is called `latest` — so a plan that printed
# it as though it would work would be telling somebody something untrue about a
# file they can fix right now. The EXISTING-TAG refusal is a different kind of
# thing entirely and it has moved below: it is a fact about THIS MACHINE'S
# image store, and a read-only plan neither reads nor writes that.
while IFS='|' read -r _tag iname idockerfile irole iproof icontext; do
    icontext="$(context_of_image "${icontext}")"
    for t in $(tags_of "${iname}" "$(commit_of_repository "${icontext}")"); do
        case "${t}" in
            *:latest) die "this script will not produce a '${t}' tag. A release is named by the commit it was built from; a floating tag is what this path replaces." ;;
        esac
    done
done <<< "${IMAGE_LINES}"

if [ "${PLAN_ONLY}" = "1" ]; then
    echo "Release ${VERSION} (manifest ${MANIFEST_SHA}, schema ${SCHEMA})"
    echo "  base image: ${BASE_DIGEST}"
    if [ -n "${RELEASE_SWEEP_TERMS:-}" ]; then
        echo "  sweep     : $(set -- ${RELEASE_SWEEP_TERMS}; echo "$#") term(s) from RELEASE_SWEEP_TERMS would be swept for in every built image"
    else
        echo "  sweep     : RELEASE_SWEEP_TERMS is not set, so a real build would sweep nothing"
    fi
    echo "  would build ${IMAGE_COUNT} image(s), all from the same fetched clones:"
    while IFS='|' read -r _tag iname idockerfile irole iproof icontext; do
        icontext="$(context_of_image "${icontext}")"
        echo "    ${iname} [${irole}] from ${icontext}/${idockerfile}"
        for t in $(tags_of "${iname}" "$(commit_of_repository "${icontext}")"); do
            # READ-ONLY, AND IT SAYS SO RATHER THAN REFUSING. `docker image
            # inspect` only looks; nothing is fetched, built or tagged by a
            # plan. Until 25 September 2026 the refusal below sat above this
            # branch, so reading a plan on a machine that had already built the
            # release meant passing --allow-existing-tag — and what that
            # teaches people is to reach for the override flag.
            if docker image inspect "${t}" >/dev/null 2>&1 </dev/null; then
                echo "      would tag : ${t}  <-- ALREADY EXISTS: a real build would refuse this (--allow-existing-tag rebuilds it)"
            else
                echo "      would tag : ${t}"
            fi
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
# The existing-tag refusal, for a real build.
#
# Every image is checked before anything is fetched, so a release never lands
# half of its images and then refuses. A release tag is written once: moving it
# would change what that name means for anything already using it.
# ---------------------------------------------------------------------------
while IFS='|' read -r _tag iname idockerfile irole iproof icontext; do
    icontext="$(context_of_image "${icontext}")"
    for t in $(tags_of "${iname}" "$(commit_of_repository "${icontext}")"); do
        if docker image inspect "${t}" >/dev/null 2>&1 </dev/null; then
            [ "${ALLOW_EXISTING_TAG}" = "1" ] \
                || die "the tag ${t} already exists on this machine. A release tag is written once, and moving it would change what that name means for anything already using it. Delete it deliberately, or pass --allow-existing-tag if you meant to rebuild it."
            # Written down so that a refused sweep can remove the tags THIS run
            # wrote without touching one that was here before it.
            PRE_EXISTING_TAGS="${PRE_EXISTING_TAGS} ${t}"
        fi
    done
done <<< "${IMAGE_LINES}"

# ---------------------------------------------------------------------------
# Fetch every repository, fresh, at exactly its pin.
# ---------------------------------------------------------------------------
TMP="$(mktemp -d "${TMPDIR:-/tmp}/forge-release-XXXXXXXX")"

# WHAT AN UNSUCCESSFUL RUN LEAVES BEHIND: the fetched clones go, and so do the
# tags this run wrote.
#
# This runs on EVERY ending from here on, which is the point of it. Until 26
# September 2026 the tag clean-up lived inside die() alone, so it covered the
# refusals this script writes by hand and nothing else — and an ordinary
# failing command under `set -e` is not a die(). Codex's review of that day
# drove exactly that: a run that built and swept a clean image set, then failed
# writing its receipt into a directory that did not exist, exited 1 and left
# both tags on the machine. The next attempt at the same commit then meets "the
# tag already exists" and the operator learns to reach for
# --allow-existing-tag, which is the habit this script spent two passes taking
# out of its own plan.
#
# SUCCESS IS MARKED ONLY AFTER THE RECEIPT IS SAFELY WRITTEN (BUILD_SUCCEEDED),
# so "the images are built" and "the run finished" cannot come apart: a release
# whose receipt never landed is not a release anybody can read afterwards.
#
# It preserves the exit status it was called with — the diagnosis of WHY the
# run ended belongs to whatever ended it, not to the clean-up.
#
# What it cannot do is survive a SIGKILL, and it does not pretend to: a killed
# run leaves its tags, and the next attempt says so by name.
cleanup() {
    local status=$?
    if [ "${KEEP_TEMP}" = "1" ]; then
        say "the fetched clones were kept at ${TMP} (--keep-temp)"
    else
        rm -rf "${TMP}"
    fi
    if [ -n "${SWEEP_ALLOWED_LOG:-}" ]; then
        rm -f "${SWEEP_ALLOWED_LOG}"
    fi
    if [ "${status}" -ne 0 ] \
        && [ "${BUILD_SUCCEEDED}" != "1" ] \
        && [ -n "${RUN_TAGS}" ] \
        && [ "${TAGS_ALREADY_REMOVED}" != "1" ]; then
        echo "ERROR: this run ended without finishing (exit status ${status}) and it had already written tags:" >&2
        remove_this_runs_tags
    fi
    return "${status}"
}
trap cleanup EXIT
# An interrupt or a termination does not run an EXIT trap by itself: the shell
# dies of the signal. Turning each into an exit does, so a ctrl-C in the middle
# of a build takes this run's tags with it as any other unfinished run does.
trap 'exit 130' INT
trap 'exit 143' TERM

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

# Every image's dockerfile has to be IN the clone of the repository that image
# is built from, and every image's proof in the build-context root's clone.
# Both are checked for all of them before the first build, so a manifest that
# names a file a pin does not carry stops before anything is tagged rather than
# after the first image has landed.
while IFS='|' read -r _tag iname idockerfile irole iproof icontext; do
    icontext="$(context_of_image "${icontext}")"
    icommit="$(commit_of_repository "${icontext}")"
    [ -f "${TMP}/${icontext}/${idockerfile}" ] \
        || die "image '${iname}' is built from ${idockerfile}, and ${icontext} at ${icommit} has no such file. The dockerfile comes from the fetched clone at the pin, never from a checkout beside this script."
    if [ -n "${iproof}" ]; then
        [ -f "${ROOT_DIR}/${iproof}" ] \
            || die "image '${iname}' names the proof ${iproof}, and ${ROOT_NAME} at ${ROOT_COMMIT} has no such file. A release proves itself with the code it is made of, and the proofs belong to the repository the release is cut from."
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
while IFS='|' read -r _tag iname idockerfile irole iproof icontext; do
    icontext="$(context_of_image "${icontext}")"
    icommit="$(commit_of_repository "${icontext}")"
    FROM_DIGESTS="$(grep -E '^FROM ' "${TMP}/${icontext}/${idockerfile}" | sed -n 's/.*@\(sha256:[0-9a-f]\{64\}\).*/\1/p' | sort -u)"
    [ -n "${FROM_DIGESTS}" ] \
        || die "${icontext}/${idockerfile} at ${icommit} (image '${iname}') pins no base image digest, so the manifest's python_base_digest cannot be checked against it."
    if [ "$(echo "${FROM_DIGESTS}" | wc -l | tr -d ' ')" != "1" ]; then
        echo "ERROR: ${icontext}/${idockerfile} (image '${iname}') starts FROM more than one base digest:" >&2
        echo "${FROM_DIGESTS}" | sed 's/^/       /' >&2
        die "the manifest records one base image; fix the Dockerfile or widen the manifest."
    fi
    [ "${FROM_DIGESTS}" = "${BASE_DIGEST}" ] \
        || die "the manifest pins the base image ${BASE_DIGEST} and ${icontext}/${idockerfile} at ${icommit} (image '${iname}') starts FROM ${FROM_DIGESTS}. One of them has moved; a release does not guess which."
    say "  ok base image ${BASE_DIGEST} agrees with ${icontext}/${idockerfile}"
done <<< "${IMAGE_LINES}"

# ---------------------------------------------------------------------------
# THE SWEEP — see the header. Nothing of the building machine goes out in a
# release image, and the words it looks for come from the machine, not from
# any file in this repository.
# ---------------------------------------------------------------------------
SWEEP_TERMS="${RELEASE_SWEEP_TERMS:-}"
if [ -z "${SWEEP_TERMS}" ]; then
    say ""
    say "NO SWEEP WILL RUN. RELEASE_SWEEP_TERMS is not set, so the images of this"
    say "release will NOT be searched for anything belonging to this machine. Set it"
    say "to this machine's own names — its host name, its account name, its home"
    say "directory, its projects folder — separated by spaces, and build again if you"
    say "want them swept. It is deliberately not a list in any tracked file: such a"
    say "list would itself be this machine's names written into a public repository."
    say ""
fi

# Is a hit in this file, in this image, one the manifest has already named?
#
# By PATH. The manifest's path is matched as the END of the file's path inside
# the image, so an exception can be written the way a person reads it —
# ".../guardkit/_installer_core/commands/feature-plan.md" — without anybody
# having to know which site-packages directory a base image happens to use.
sweep_exception_reason() {
    local iname="$1" relpath="$2"
    local _t simage spath sreason norm
    [ -n "${SWEEP_OK_LINES}" ] || return 1
    while IFS='|' read -r _t simage spath sreason; do
        [ "${simage}" = "${iname}" ] || continue
        norm="${spath#"${spath%%[!./]*}"}"          # drop any leading dots and slashes
        case "/${relpath}" in
            *"/${norm}") printf '%s' "${sreason}"; return 0 ;;
        esac
    done <<< "${SWEEP_OK_LINES}"
    return 1
}

# WHAT A REFUSED RUN LEAVES BEHIND: nothing that could be shipped, and nothing
# standing in the way of trying again.
#
# Both of an image's tags are written by `docker build`, before anything can be
# swept. Until 25 September 2026 a refused sweep said "every tag of this run is
# still on this machine; do not ship any of them" and then left them there — so
# the next attempt at the same commit refused with "the tag ... already exists"
# and the operator reached for --allow-existing-tag, which is exactly the habit
# this script has just finished taking out of its plan. The tags this run wrote
# are removed and named. A tag that was already on this machine when the run
# started — which only happens with --allow-existing-tag — is left exactly as
# it was found, because it is not this run's to remove.
remove_this_runs_tags() {
    TAGS_ALREADY_REMOVED=1
    local t
    for t in ${RUN_TAGS}; do
        case " ${PRE_EXISTING_TAGS} " in
            *" ${t} "*)
                echo "       ${t} was on this machine before this run started, so it has been left alone." >&2
                continue
                ;;
        esac
        if docker rmi "${t}" >/dev/null 2>&1 </dev/null; then
            echo "       removed ${t}, which this run wrote, so the same commit can be built again without --allow-existing-tag." >&2
        else
            echo "       ${t} was written by this run and could NOT be removed; remove it before building this release again." >&2
        fi
    done
    echo "       Nothing this run built is to be shipped." >&2
}

# WHICH WORD MATCHED, WITHOUT THE WORD.
#
# A refusal has to be diagnosable, and it must not publish the thing it is
# refusing: RELEASE_SWEEP_TERMS holds this machine's own names, and a build log
# is kept, read and pasted. So every refusal says WHICH of the words matched by
# its POSITION in that variable — "number 3 of the 6" — and never its value.
# Codex's review of 26 September 2026 found the allowed-exception line redacted
# and the refusal line still printing the word in full.
#
# A "?" means the word is not in the list any more, which cannot happen while a
# run is in flight and is still better than printing something.
term_position() {
    local wanted="$1" i=1 t
    for t in ${SWEEP_TERMS}; do
        if [ "${t}" = "${wanted}" ]; then
            printf '%s' "${i}"
            return 0
        fi
        i=$((i + 1))
    done
    printf '%s' "?"
}

# A PATH CAN CARRY ONE OF THE WORDS TOO, and this sweep counts a file whose
# NAME holds a word as a hit — so printing the path prints the word, which is
# how "no sweep word is printed" was still not true. Every word inside a piece
# of text is replaced by its position before that text is printed, written into
# the refusals file, or written into the receipt.
redact_terms() {
    local text="$1" i=1 t
    for t in ${SWEEP_TERMS}; do
        case "${text}" in
            *"${t}"*) text="${text//"${t}"/<word ${i} of ${SWEEP_TERM_COUNT}>}" ;;
        esac
        i=$((i + 1))
    done
    printf '%s' "${text}"
}

# One unpacked copy of an image, searched file by file so a hit can be named.
#
# `where` is what a refusal calls this copy — the running filesystem, or the
# layer a push sends. A file is a hit if its CONTENTS carry the word or if its
# PATH does; the path is matched with the unpacking directory stripped off, so
# where this run happens to unpack has nothing to do with the answer.
sweep_tree() {
    local iname="$1" root="$2" where="$3" refusals="$4"
    local term hits path relpath safepath reason
    for term in ${SWEEP_TERMS}; do
        hits="$(
            {
                /bin/grep -r -l -F -e "${term}" "${root}" 2>/dev/null || true
                find "${root}" -type f 2>/dev/null | while IFS= read -r path; do
                    case "${path#"${root}"}" in
                        *"${term}"*) printf '%s\n' "${path}" ;;
                    esac
                done
            } | sort -u
        )"
        [ -n "${hits}" ] || continue
        while IFS= read -r path; do
            [ -n "${path}" ] || continue
            relpath="${path#"${root}/"}"
            # The path is redacted as well as the word, because a file whose
            # NAME holds one of the words is a hit here, and the path is what
            # gets printed and written down.
            safepath="$(redact_terms "${relpath}${where}")"
            if reason="$(sweep_exception_reason "${iname}" "${relpath}")"; then
                say "    allowed  ${safepath} carries one of the words (not printed here; build logs get kept) — the manifest names this file: ${reason}"
                printf '%s|%s|%s\n' "${iname}" "${safepath}" "${reason}" >> "${SWEEP_ALLOWED_LOG}"
            else
                printf '%s|%s|%s\n' "${iname}" "${safepath}" "$(term_position "${term}")" >> "${refusals}"
            fi
        done <<< "${hits}"
    done
}

# THE LAYERS, WHICH ARE WHAT A PUSH SENDS.
#
# `docker export` (below) is the container's FLATTENED FINAL filesystem. A file
# a Dockerfile copies in and a later step deletes is gone from it and is still
# in the image, because the layer that holds it is still one of the image's
# layers and is still what `docker save` and a registry push send. That is not
# a hypothetical: on 25 September 2026 the jarvis image copied another
# repository's whole clone to /tmp and deleted it four lines later, and the
# export of that image carried this estate's account name in 0 files while the
# image's own layers carried it in 2,194. A check that says a clean thing about
# a dirty image is worse than no check, so the sweep reads what ships.
#
# Every blob `docker save` writes is asked what it is rather than guessed from
# its name, because the daemon writes two different shapes (a directory per
# layer with a layer.tar in it, or an OCI layout of blobs by digest, some of
# them gzipped). Each layer is unpacked into a directory of its own, so files
# at the same path in different layers do not overwrite each other.
sweep_layers() {
    local iname="$1" itag="$2" dir="$3" refusals="$4"
    local save="${dir}/save" meta="${dir}/save-meta"
    mkdir -p "${save}" "${meta}" "${dir}/layers"

    docker save "${itag}" -o "${dir}/image.tar" </dev/null 2>"${dir}/save-errors.txt" \
        || die "image '${iname}' could not be saved, so the layers a push would send could not be swept. Not swept is not a pass. Docker said: $(tr '\n' ' ' < "${dir}/save-errors.txt")"
    tar -xf "${dir}/image.tar" -C "${save}" --no-same-owner --no-same-permissions \
        2>> "${dir}/save-errors.txt" \
        || die "the saved copy of image '${iname}' could not be unpacked, so its layers were never searched. Not swept is not a pass."
    rm -f "${dir}/image.tar"
    chmod -R u+rwX "${save}" 2>/dev/null || true

    local blob src magic layers=0 files=0 out in_tar on_disk
    while IFS= read -r blob; do
        [ -f "${blob}" ] || continue
        src=""
        magic="$(head -c 2 "${blob}" 2>/dev/null | od -An -tx1 | tr -d ' \n')"
        if [ "${magic}" = "1f8b" ]; then
            gzip -dc "${blob}" > "${dir}/blob.tar" 2>/dev/null \
                || die "a compressed layer of image '${iname}' could not be read, so it was never searched. Not swept is not a pass."
            src="${dir}/blob.tar"
        elif tar -tf "${blob}" >/dev/null 2>&1; then
            src="${blob}"
        else
            # Not an archive: the image's own json — its manifest, its config.
            # Small, and swept as metadata below.
            cp "${blob}" "${meta}/$(printf '%s' "${blob#"${save}/"}" | tr '/' '_')" 2>/dev/null || true
            continue
        fi

        layers=$((layers + 1))
        out="${dir}/layers/${layers}"
        mkdir -p "${out}"
        tar -xf "${src}" -C "${out}" --no-same-owner --no-same-permissions \
            2>> "${dir}/layer-errors.txt" || true
        chmod -R u+rwX "${out}" 2>/dev/null || true

        in_tar="$(tar -tvf "${src}" 2>/dev/null | awk '$1 ~ /^-/' | wc -l | tr -d ' ')"
        on_disk="$(find "${out}" -type f 2>/dev/null | wc -l | tr -d ' ')"
        [ "${on_disk}" -ge "${in_tar}" ] \
            || die "layer ${layers} of image '${iname}' unpacked ${on_disk} of the ${in_tar} files in it, so part of what a push would send was never searched. Not swept is not a pass. The unpacking said: $(tr '\n' ' ' < "${dir}/layer-errors.txt")"
        files=$((files + on_disk))
        rm -f "${dir}/blob.tar"
    done <<< "$(find "${save}" -type f | sort)"

    # NOT SWEPT IS NOT A PASS. An image has at least one layer, and the daemon
    # says how many: fewer unpacked than that means part of the image was never
    # opened at all.
    local expected
    expected="$(docker image inspect --format '{{len .RootFS.Layers}}' "${itag}" </dev/null 2>/dev/null || echo 0)"
    [ "${layers}" -ge 1 ] \
        || die "no layer of image '${iname}' could be unpacked from its saved copy, so nothing of what a push would send was searched. Not swept is not a pass."
    [ "${layers}" -ge "${expected}" ] \
        || die "image '${iname}' has ${expected} layers and only ${layers} of them could be unpacked, so part of what a push would send was never searched. Not swept is not a pass."

    say "    layers: ${files} files across ${layers} layer(s) — what a push sends, deleted files included"

    local i=1
    while [ "${i}" -le "${layers}" ]; do
        sweep_tree "${iname}" "${dir}/layers/${i}" " (in layer ${i} of ${layers}, which a push sends even when a later layer deletes it)" "${refusals}"
        i=$((i + 1))
    done
    sweep_tree "${iname}" "${meta}" " (in the image's own manifest or config, which a push sends)" "${refusals}"

    rm -rf "${save}" "${meta}" "${dir}/layers"
}

sweep_image() {
    local iname="$1" itag="$2"
    if [ -z "${SWEEP_TERMS}" ]; then
        say "  not swept (RELEASE_SWEEP_TERMS is not set), so nothing is known about what ${iname} carries"
        return 0
    fi

    local dir="${TMP}/sweep-${iname}"
    rm -rf "${dir}"
    mkdir -p "${dir}/fs"

    # The image's own metadata: its configuration, its labels and its history.
    docker image inspect --format '{{json .Config}}' "${itag}" > "${dir}/config.json" </dev/null \
        || die "the configuration of image '${iname}' could not be read, so it could not be swept. Not swept is not a pass."
    docker image inspect --format '{{json .Config.Labels}}' "${itag}" > "${dir}/labels.json" </dev/null \
        || die "the labels of image '${iname}' could not be read, so it could not be swept."
    docker image history --no-trunc "${itag}" > "${dir}/history.txt" </dev/null \
        || die "the history of image '${iname}' could not be read, so it could not be swept."

    # THE FILESYSTEM, FLATTENED. `docker create` makes a container without
    # starting one — nothing in the image runs — and `docker export` writes its
    # whole filesystem as a tar. This is the part each image's own proof script
    # does NOT do, and it is where both of this estate's real hits were found.
    local cid
    cid="$(docker create "${itag}" </dev/null 2>/dev/null)" \
        || cid="$(docker create --entrypoint /bin/true "${itag}" </dev/null 2>/dev/null)" \
        || die "no container could be created from image '${iname}' to export its filesystem, so it could not be swept. Not swept is not a pass."
    docker export "${cid}" > "${dir}/fs.tar" </dev/null \
        || { docker rm -f "${cid}" >/dev/null 2>&1 </dev/null || true; die "the filesystem of image '${iname}' could not be exported, so it could not be swept."; }
    docker rm -f "${cid}" >/dev/null 2>&1 </dev/null || true

    # Extracted as this user, so nothing in the image can set an owner or a
    # mode here, and then made readable — a file with mode 000 inside an image
    # would otherwise be a file the sweep silently skipped.
    tar -xf "${dir}/fs.tar" -C "${dir}/fs" --no-same-owner --no-same-permissions \
        2> "${dir}/tar-errors.txt" || true
    chmod -R u+rwX "${dir}/fs" 2>/dev/null || true

    # NOT SWEPT IS NOT A PASS, so what came out is counted against what went in.
    # An export holds device nodes and sockets that cannot be recreated here;
    # those carry no text and are not counted. Every REGULAR file must be.
    local in_tar on_disk
    in_tar="$(tar -tvf "${dir}/fs.tar" 2>/dev/null | awk '$1 ~ /^-/' | wc -l | tr -d ' ')"
    on_disk="$(find "${dir}/fs" -type f 2>/dev/null | wc -l | tr -d ' ')"
    [ "${on_disk}" -ge "${in_tar}" ] \
        || die "the sweep of image '${iname}' unpacked ${on_disk} of the ${in_tar} files in it, so part of that image was never searched. Not swept is not a pass. The unpacking said: $(tr '\n' ' ' < "${dir}/tar-errors.txt")"

    say "  sweeping ${iname}: ${on_disk} files in the running filesystem, its configuration, its labels and its history"

    local refusals="${dir}/refusals.txt"
    : > "${refusals}"
    local term meta
    for term in ${SWEEP_TERMS}; do
        # The metadata first — small, and a hit there is never excusable.
        for meta in config.json labels.json history.txt; do
            if /bin/grep -q -F -e "${term}" "${dir}/${meta}"; then
                printf '%s|%s|%s\n' "${iname}" "the image's ${meta%%.*}" "$(term_position "${term}")" >> "${refusals}"
            fi
        done
    done
    # Then the flattened filesystem — what a container of this image would see.
    sweep_tree "${iname}" "${dir}/fs" "" "${refusals}"

    # The export and its unpacked copy are large; the layers are larger again,
    # so the first is let go of before the second is written.
    rm -rf "${dir}/fs" "${dir}/fs.tar"

    # AND THEN WHAT A PUSH WOULD SEND, which is not the same thing at all.
    sweep_layers "${iname}" "${itag}" "${dir}" "${refusals}"

    if [ -s "${refusals}" ]; then
        echo "ERROR: image '${iname}' (${itag}) carries names belonging to the machine that built it:" >&2
        # WHICH FILE, AND WHICH OF THE WORDS BY ITS POSITION — never the word
        # itself, and never a path with the word still in it. A refusal has to
        # be diagnosable without publishing the name it is refusing, because a
        # build log is kept and pasted (Codex's review, 26 September 2026).
        while IFS='|' read -r _rimage rpath rposition; do
            echo "       in ${rpath}" >&2
            echo "          the word: number ${rposition} of the ${SWEEP_TERM_COUNT} in RELEASE_SWEEP_TERMS — not printed here, because it is one of this machine's own names" >&2
        done < "${refusals}"
        remove_this_runs_tags
        die "a release image belongs to the release, not to a machine. Take the name out of the source that puts it there — or, if the file needs it (a detector's own patterns, say), name that FILE under sweep_exceptions: in the manifest, with its reason."
    fi
    say "    ok ${iname} carries none of the ${SWEEP_TERM_COUNT} words RELEASE_SWEEP_TERMS names, in its running filesystem or in any layer a push would send"
    SWEPT_IMAGES="${SWEPT_IMAGES} ${iname}"
}

SWEEP_TERM_COUNT="$(set -- ${SWEEP_TERMS}; echo "$#")"
# What the sweep did, kept for the receipt (the fourth review of release
# 2026.09.26-1, 25 September 2026: from a receipt a swept release and an
# unswept one were indistinguishable). Term COUNT only, never a term.
SWEEP_ALLOWED_LOG="$(mktemp)"
SWEPT_IMAGES=""

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
while IFS='|' read -r _tag iname idockerfile irole iproof icontext; do
    icontext="$(context_of_image "${icontext}")"
    icommit="$(commit_of_repository "${icontext}")"
    iurl="$(url_of_repository "${icontext}")"
    ICONTEXT_DIR="${TMP}/${icontext}"
    TAG_COMMIT="${iname}:${icommit}"
    TAG_VERSION="${iname}:${VERSION}"
    IIDFILE="${TMP}/image-id-${iname}"

    # AN IMAGE'S OWN REPOSITORY, IN THE STANDARD LABELS TOO.
    #
    # org.opencontainers.image.revision and .source mean "the commit this
    # image was built from" and "the repository it came from", and every
    # ordinary tool that reads an image reads them. Until 25 September 2026
    # this script set both ONCE, from the build-context root — which was right
    # while every image came from this repository, and became wrong the moment
    # an image could be built from another repository's clone: the memory
    # service's image said Forge's commit and Forge's address, while its own
    # tag and its com.guardkit.* labels said fleet-memory's. Two answers to one
    # question, and the one the rest of the world reads was the wrong one.
    # They are per image now, and they say the same thing the image's commit
    # tag says. For every image built from the build-context root — which is
    # every image of every release cut before per-image contexts — this is
    # exactly the value it had.
    say "Building ${TAG_COMMIT} (also tagged ${TAG_VERSION}) from ${icontext}/${idockerfile} in ${TMP} only"
    docker buildx build \
        "${BUILD_ARGS[@]}" \
        "${LABEL_ARGS[@]}" \
        --label "org.opencontainers.image.revision=${icommit}" \
        --label "org.opencontainers.image.source=${iurl}" \
        --label "com.guardkit.release.image.role=${irole}" \
        --label "com.guardkit.release.image.name=${iname}" \
        --label "com.guardkit.release.image.context=${icontext}" \
        --label "com.guardkit.release.image.dockerfile=${idockerfile}" \
        --label "org.opencontainers.image.title=${iname}" \
        -t "${TAG_COMMIT}" \
        -t "${TAG_VERSION}" \
        -f "${ICONTEXT_DIR}/${idockerfile}" \
        "${ICONTEXT_DIR}" \
        --build-arg "FORGE_GIT_SHA=${icommit}" \
        --build-arg "FORGE_GIT_DIRTY=false" \
        --build-arg "PYTHON_BASE_DIGEST=${BASE_DIGEST}" \
        --iidfile "${IIDFILE}" \
        </dev/null \
        || die "the build of image '${iname}' failed. Anything built before it in this run is still tagged on this machine; the release is NOT built."

    ibuilt="$(cat "${IIDFILE}")"
    say "  built ${iname} ${ibuilt}"
    RUN_TAGS="${RUN_TAGS} ${TAG_COMMIT} ${TAG_VERSION}"

    # SWEPT THE MOMENT IT EXISTS, not at the end of the run. A release is built
    # as a set and a refusal stops the whole set either way, but sweeping here
    # means the image that carries a machine's name is named while it is the
    # thing that just happened, rather than four builds later.
    sweep_image "${iname}" "${TAG_COMMIT}"

    printf '%s|%s|%s|%s|%s|%s|%s\n' "${iname}" "${irole}" "${idockerfile}" "${ibuilt}" "${TAG_COMMIT}" "${TAG_VERSION}" "${icontext}" >> "${BUILT}"
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
    while IFS='|' read -r iname irole idockerfile ibuilt itagcommit itagversion icontext; do
        iproof="$(echo "${IMAGE_LINES}" | awk -F'[|]' -v n="${iname}" '$2 == n { print $5; exit }')"
        if [ -z "${iproof}" ]; then
            say "Image '${iname}' names no proof, so nothing was proved about it."
            continue
        fi
        say "Proving ${itagcommit} with ${ROOT_NAME}/${iproof}, against the fetched clone at ${ROOT_DIR}"
        # A FAILED PROOF REMOVES THIS RUN'S TAGS, as a sweep refusal does (the
        # follow-up review of 25 September 2026): a refused run that left its
        # tags behind taught the operator to reach for --allow-existing-tag.
        if ! bash "${ROOT_DIR}/${iproof}" "${itagcommit}" </dev/null; then
            echo "ERROR: the proof of image '${iname}' failed for ${itagcommit}." >&2
            remove_this_runs_tags
            die "the proof of image '${iname}' failed; this run's tags were removed. Do not ship anything from it."
        fi
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
    while IFS='|' read -r iname irole idockerfile ibuilt itagcommit itagversion icontext; do
        [ "${first}" = "1" ] || printf ',\n'
        first=0
        printf '    {"name": "%s", "role": "%s", "context": "%s", "dockerfile": "%s", "image_id": "%s", "tags": ["%s", "%s"], "repo_digests": %s, "labels": %s}' \
            "${iname}" "${irole}" "${icontext}" "${idockerfile}" "${ibuilt}" "${itagcommit}" "${itagversion}" \
            "$(docker image inspect "${ibuilt}" --format '{{json .RepoDigests}}')" \
            "$(docker image inspect "${ibuilt}" --format '{{json .Config.Labels}}')"
    done < "${BUILT}"
    printf '\n  ],\n'
    printf '  "python_base_digest": "%s",\n' "${BASE_DIGEST}"
    # THE SWEEP, ON THE RECORD: how many words (never which), which images
    # were swept, and every exception the manifest allowed — or the plain
    # sentence that no sweep ran.
    if [ "${SWEEP_TERM_COUNT}" -gt 0 ]; then
        printf '  "sweep": {"terms_counted": %s, "images_swept": [' "${SWEEP_TERM_COUNT}"
        first=1
        for sw in ${SWEPT_IMAGES}; do [ "${first}" = "1" ] || printf ', '; printf '"%s"' "${sw}"; first=0; done
        printf '], "exceptions_allowed": ['
        first=1
        if [ -s "${SWEEP_ALLOWED_LOG}" ]; then
            sort -u "${SWEEP_ALLOWED_LOG}" | while IFS='|' read -r ai apath areason; do
                [ "${first}" = "1" ] || printf ', '
                printf '{"image": "%s", "path": "%s", "reason": "%s"}' "${ai}" "${apath}" "${areason}"
                first=0
            done
        fi
        printf ']},\n'
    else
        printf '  "sweep": {"terms_counted": 0, "images_swept": [], "exceptions_allowed": [], "note": "no sweep ran: RELEASE_SWEEP_TERMS was not set when this release was built"},\n'
    fi
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

# THE RUN IS A SUCCESS FROM HERE AND NOT ONE MOMENT EARLIER.
#
# Everything above can fail — including this write, which is the failure Codex's
# review of 26 September 2026 drove: a receipt destination whose parent
# directory does not exist. Until this line the tags this run wrote are removed
# by the EXIT trap on any ending, so a release that nobody can read afterwards
# does not leave images behind that look shippable.
[ -s "${RECEIPT}" ] \
    || die "the receipt at ${RECEIPT} is empty or was not written, so this release has nothing that says what it is made of. The images this run built have been removed."
BUILD_SUCCEEDED=1

say "Receipt written to ${RECEIPT}"
say ""
say "Release ${VERSION} built — ${IMAGE_COUNT} image(s)."
while IFS='|' read -r iname irole idockerfile ibuilt itagcommit itagversion icontext; do
    say "  ${iname} [${irole}] built from ${icontext}/${idockerfile}"
    say "    image id : ${ibuilt}"
    say "    tags     : ${itagcommit}"
    say "               ${itagversion}"
done < "${BUILT}"
say "  from     : ${REPO_COUNT} pinned repositories and the base image ${BASE_DIGEST}"
say ""
say "There is no registry digest until an image is pushed; the ids above are"
say "what identify them on this machine. Reproducing them elsewhere needs this manifest,"
say "Docker, and network access to the repository host."

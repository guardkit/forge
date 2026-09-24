#!/usr/bin/env bash
#
# HAND THE RELEASE IMAGE TO A SANDBOX, AND CHECK IT ARRIVED WHOLE.
# 24 September 2026, stage 4d of the containerisation rollout gate.
#
#     ./hand-release-image-to-sandbox.sh <sandbox name> <release version>
#
# WHY THIS EXISTS. A sandbox is a small machine of its own with its own Docker
# engine. The factory's two services for a project — the deploy helper and the
# build runner — now run inside that project's sandbox as containers from the
# tested release image, and from nothing else (no checkout of the factory's
# code mounted in, no virtual environment built in there). So the image has to
# GET in there first. Section 5 of the design pass
# (ai-transition/docs/factory-containerisation-design-pass-2026-09-23.md, "How
# the pinned copy of the factory reaches the sandbox") gives two ways and only
# two: the sandbox's own engine PULLS the image at its pinned digest, or the
# image is TRANSFERRED — saved on the machine that has it, loaded inside — and
# checked. This script is the second. There is no third: a clone at the pinned
# commit is not the tested image, and there is no source fallback anywhere in
# this path.
#
# WHEN THE ESTATE HAS A REGISTRY (open question 1 of the design pass, whose
# recommendation is a private registry under the guardkit organisation), THIS
# SCRIPT IS REPLACED by one line inside the sandbox —
# `docker pull <registry>/forge@<digest>` — and the two walks then differ only
# in the credential used to pull. **The check does not change either way.**
# What is checked is what the sandbox's own engine ends up holding: the SAME
# IMAGE as the one the machine that handed it over holds. A pull that lands a
# different image and a transfer that lands a different image fail the same
# check, with the same sentence, and the bootstrap inside the sandbox makes the
# same check again before it runs anything from the image.
#
# WHAT IS COMPARED (24 September 2026, stage 4f, after the stage 4d reviewer's
# third finding). An image is three things, kept apart on purpose by the OCI
# image configuration specification: THE PLATFORM it was built for, THE
# FILESYSTEM it is made of, and THE RUNTIME CONFIGURATION it carries — the
# environment, the entry point, the command, the user, the working directory,
# the labels, the ports, the volumes and the stop signal. This script asks each
# engine for all three, in one fixed order, one per line — the IDENTITY
# DOCUMENT — strips carriage returns, and hashes it. Same image, same document,
# same hash.
#
# The first version of this script compared the LAYER LIST alone. That is the
# filesystem and nothing else: an image can keep every layer it had and still
# have had its environment or its entry point changed after it was reviewed,
# and that is a different image doing different things. The reviewer built
# exactly that image and watched it accepted.
#
# AND WHY NOT "THE IMAGE ID" (learned here, 24 September 2026, the first time
# this was run). The two engines do not agree on what an image's id IS. This
# machine's engine keeps images the old way and reports the digest of the
# image's CONFIG; a sandbox's engine keeps them the containerd way and reports
# the digest of the image's MANIFEST. The very same bytes, carried across, came
# back under a different "id" and this script refused them — correct by its own
# rule, and wrong about the facts. The identity document is not the engine's
# opinion of the image: it is the image's own configuration and rootfs, which
# both engines read from the image itself and report identically. The two
# release labels (the version, and the hash of the manifest the image was built
# from) are inside the document, and are compared on their own as well so that
# a mismatch can say which label it was.
#
# The one field deliberately left out is the architecture VARIANT: the two
# image stores do not fill it in the same way, and a build for another platform
# has different layers in any case. The bootstrap inside the sandbox
# (forge/src/forge/cli/deploy_templates/sandbox-runner.sh) builds the very same
# document with the very same format string, and the top of that file is the
# long version of this note.
#
# WHAT IT DOES NOT DO. It never creates, removes or reconfigures a sandbox, it
# never builds an image, and it never touches a running one. It reads one image
# on this machine and writes one image into the sandbox named on the command
# line.
#
# SETTINGS (names, with defaults; nothing here holds a value of any machine):
#   FORGE_IMAGE_NAME   the image's name without its tag (default: forge)
#   FORGE_IMAGE        the whole tag to hand over; overrides the two above
#   SANDBOX_CLIENT     the sandbox client (default: sbx)
#   SANDBOX_CLIENT_ARGUMENTS
#                      anything the client needs before the verb (default: none)
#   SANDBOX_TRANSFER_TIMEOUT_SECONDS
#                      how long the load inside is given (default: 900). A
#                      release image is most of a gigabyte and the first
#                      transfer into a cold sandbox is the slow one
#
# EXIT. 0 when the sandbox's own engine holds the image at the expected tag and
# it is the same image as this machine's, by the identity above and by its
# release labels. 2 when something was missing or misnamed at the door. 3 when
# the transfer itself failed. 4 when the image in there is not this machine's
# image — the one case that must never be shrugged off, because from there on
# everything downstream would be running bytes nobody tested.
#
# THE ALREADY-PRESENT PATH MAKES EXACTLY THE SAME CHECKS (stage 4f). It used to
# compare the layer list and stop there, so an image already in the sandbox
# with the right layers and the wrong configuration — or the wrong release
# labels — was waved through with "nothing to carry". Now both paths end in the
# same check, and the only difference between them is whether anything was
# carried in first.

set -uo pipefail

log() { printf '[hand-release-image] %s\n' "$*"; }

SANDBOX_NAME="${1:-}"
RELEASE_VERSION="${2:-}"
CLIENT="${SANDBOX_CLIENT:-sbx}"
TIMEOUT_SECONDS="${SANDBOX_TRANSFER_TIMEOUT_SECONDS:-900}"

if [[ -z "${SANDBOX_NAME}" ]]; then
  log "FATAL: name the sandbox to hand the image to. Usage: $0 <sandbox name> <release version>"
  exit 2
fi
IMAGE="${FORGE_IMAGE:-${FORGE_IMAGE_NAME:-forge}:${RELEASE_VERSION}}"
if [[ -z "${RELEASE_VERSION}" && -z "${FORGE_IMAGE:-}" ]]; then
  log "FATAL: name the release version to hand over (or set FORGE_IMAGE to the whole tag). Usage: $0 <sandbox name> <release version>"
  exit 2
fi
if ! command -v "${CLIENT}" >/dev/null 2>&1; then
  log "FATAL: there is no sandbox client at '${CLIENT}' on this machine. Refusing."
  exit 2
fi

# shellcheck disable=SC2206
CLIENT_CALL=("${CLIENT}" ${SANDBOX_CLIENT_ARGUMENTS:-} exec "${SANDBOX_NAME}")

#: THE IDENTITY DOCUMENT: what this script asks each engine an image IS. The
#: platform, the filesystem's layer list and the whole of the runtime
#: configuration, in one fixed order, one per line. Both kinds of engine read
#: these from the image itself, so both answer the same for the same image.
#: THESE ARE THE SAME BYTES AS THE BOOTSTRAP'S OWN COPY — see
#: forge/src/forge/cli/deploy_templates/sandbox-runner.sh,
#: IMAGE_IDENTITY_DOCUMENT_FORMAT. If one is ever changed, the other has to
#: change with it, and every machine's recorded identity has to be taken again.
IMAGE_IDENTITY_DOCUMENT_FORMAT='forge-image-identity/1
architecture {{.Architecture}}
os {{.Os}}
{{range .RootFS.Layers}}layer {{.}}
{{end}}{{range .Config.Env}}env {{.}}
{{end}}{{range .Config.Entrypoint}}entrypoint {{.}}
{{end}}{{range .Config.Cmd}}cmd {{.}}
{{end}}user {{.Config.User}}
workdir {{.Config.WorkingDir}}
{{range $name, $value := .Config.Labels}}label {{$name}}={{$value}}
{{end}}{{range $port, $ignored := .Config.ExposedPorts}}port {{$port}}
{{end}}{{range $path, $ignored := .Config.Volumes}}volume {{$path}}
{{end}}stopsignal {{.Config.StopSignal}}'

#: The same two lines on both sides: carriage returns out (the answer from
#: inside travels through the sandbox client), exactly one newline at the end,
#: then hashed. An empty answer hashes to a perfectly ordinary-looking hash, so
#: every caller checks for one before it believes a comparison.
hash_the_document() {
  local document="$1"
  [[ -n "${document}" ]] || return 1
  # The whole document or none of it: its first line is a fixed word, so an
  # engine that rendered only part of what was asked for is caught here rather
  # than having a hash taken of whatever it did say.
  [[ "${document%%$'\n'*}" == "forge-image-identity/1" ]] || return 1
  printf '%s\n' "${document}" | sha256sum | cut -d' ' -f1
}
identity_here() {
  local document
  document="$(docker image inspect --format "${IMAGE_IDENTITY_DOCUMENT_FORMAT}" "$1" 2>/dev/null | tr -d '\r')"
  hash_the_document "${document}"
}
identity_inside() {
  local document
  document="$(timeout 120 "${CLIENT_CALL[@]}" docker image inspect --format "${IMAGE_IDENTITY_DOCUMENT_FORMAT}" "$1" 2>/dev/null | tr -d '\r')"
  hash_the_document "${document}"
}
label_here() {
  docker image inspect --format "{{index .Config.Labels \"$2\"}}" "$1" 2>/dev/null
}
label_inside() {
  timeout 120 "${CLIENT_CALL[@]}" docker image inspect --format "{{index .Config.Labels \"$2\"}}" "$1" 2>/dev/null |
    tr -d '\r' | tail -1
}

# --- what this machine holds ------------------------------------------------
HOST_ENGINE_ID="$(docker image inspect --format '{{.Id}}' "${IMAGE}" 2>/dev/null)"
if [[ -z "${HOST_ENGINE_ID}" ]]; then
  log "FATAL: this machine has no image called ${IMAGE}. Build or fetch the release first; this script only carries an image that is already here. Refusing."
  exit 2
fi
HOST_IDENTITY="$(identity_here "${IMAGE}")"
if [[ -z "${HOST_IDENTITY}" ]]; then
  log "FATAL: this machine's engine would not say what ${IMAGE} is made of and how it is configured, so there is nothing to compare anything with. Refusing."
  exit 2
fi
HOST_VERSION="$(label_here "${IMAGE}" com.guardkit.release.version)"
HOST_MANIFEST="$(label_here "${IMAGE}" com.guardkit.release.manifest.sha256)"
log "this machine holds ${IMAGE}"
log "  image identity      ${HOST_IDENTITY} (its platform, its layers and its runtime configuration)"
log "  this engine's id    ${HOST_ENGINE_ID} (not comparable across engines — see the top of this file)"
log "  release version     ${HOST_VERSION:-none recorded}"
log "  manifest hash       ${HOST_MANIFEST:-none recorded}"

# --- the one check, wherever the image came from ----------------------------
# Both paths below end here: the one that carried the image in, and the one
# that found it already there. Before stage 4f the second path compared the
# layer list and nothing else, and never looked at the labels at all.
say_the_settings() {
  log ""
  log "Give the sandbox's bootstrap these settings (names, with these values):"
  log "  FORGE_IMAGE=${IMAGE}"
  log "  FORGE_IMAGE_IDENTITY=${HOST_IDENTITY}"
  log "  FORGE_RELEASE_VERSION=${HOST_VERSION:-}"
  log "  FORGE_RELEASE_MANIFEST_SHA256=${HOST_MANIFEST:-}"
}

# Says why not, and answers with a status; the caller decides what a no means.
# After a transfer a no is fatal — there is nothing left to try. Before one it
# is the reason to carry the image in.
it_is_this_machines_image() {
  local inside_identity="$1" here there label
  if [[ "${inside_identity}" != "${HOST_IDENTITY}" ]]; then
    log "the image ${SANDBOX_NAME} holds under the name ${IMAGE} is not this machine's image: it is ${inside_identity:-not readable at all} where this machine's is ${HOST_IDENTITY}. That covers the platform, the layers and the whole runtime configuration, so a changed environment or entry point lands here as surely as a changed filesystem."
    return 1
  fi
  for label in com.guardkit.release.version com.guardkit.release.manifest.sha256; do
    here="$(label_here "${IMAGE}" "${label}")"
    there="$(label_inside "${IMAGE}" "${label}")"
    if [[ "${here}" != "${there}" ]]; then
      log "the image in ${SANDBOX_NAME} says ${label} is '${there}' and this machine's says '${here}'."
      return 1
    fi
  done
  return 0
}

# --- is it already in there, and already right? -----------------------------
# THE CHECK HERE IS THE WHOLE CHECK (stage 4f). What is already in there gets
# no easier a time of it than what is carried in: same identity, same labels.
# Only then is there nothing to carry.
already="$(identity_inside "${IMAGE}")"
if [[ -n "${already}" ]]; then
  log "${SANDBOX_NAME} already holds something called ${IMAGE}; checking it is this machine's image before saying there is nothing to carry"
  if it_is_this_machines_image "${already}"; then
    log "${SANDBOX_NAME} already holds ${IMAGE}, and it is this machine's image (${HOST_IDENTITY}); nothing to carry"
    say_the_settings
    exit 0
  fi
  log "so it is carried in again, over the one that is in there"
fi

# --- the transfer -----------------------------------------------------------
# `docker save` writes the image to this machine's standard output and
# `docker load` reads one from the sandbox's standard input, so the whole
# transfer is one pipe through the sandbox client. Nothing is written to a file
# on either side, so there is no half-carried tar left anywhere if it fails.
log "carrying ${IMAGE} into ${SANDBOX_NAME} (this takes a while: a release image is most of a gigabyte)"
set -o pipefail
if ! docker save "${IMAGE}" | timeout "${TIMEOUT_SECONDS}" "${CLIENT_CALL[@]}" docker load; then
  log "FATAL: the transfer into ${SANDBOX_NAME} failed or did not finish within ${TIMEOUT_SECONDS}s. Nothing downstream may assume the image is in there. Refusing."
  exit 3
fi

# --- read it back from inside, and refuse if it differs ---------------------
# This is the point of the script. What matters is not that a transfer ran, but
# that the sandbox's own engine now holds an image made of the SAME LAYERS as
# this machine's, carrying the same release labels.
INSIDE_ENGINE_ID="$(timeout 120 "${CLIENT_CALL[@]}" docker image inspect --format '{{.Id}}' "${IMAGE}" 2>/dev/null | tr -d '\r' | tail -1)"
if [[ -z "${INSIDE_ENGINE_ID}" ]]; then
  log "FATAL: after the transfer, ${SANDBOX_NAME} still has no image called ${IMAGE}. Refusing."
  exit 3
fi
INSIDE_IDENTITY="$(identity_inside "${IMAGE}")"
if ! it_is_this_machines_image "${INSIDE_IDENTITY}"; then
  log "FATAL: the transfer ran and what ${SANDBOX_NAME} now holds is not this machine's image (the line above says how), so nothing may be run from it. Refusing."
  exit 4
fi

log "${SANDBOX_NAME} holds ${IMAGE}, and it is this machine's image (${INSIDE_IDENTITY})"
log "  (that engine calls the image ${INSIDE_ENGINE_ID}; this one calls it ${HOST_ENGINE_ID}. The same bytes, two engines, two names for them — which is why the identity above is what is compared, and not either name)"
say_the_settings
exit 0

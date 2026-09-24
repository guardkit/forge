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
# What is checked is what the sandbox's own engine ends up holding: an image
# made of the same layers, carrying the same release labels, as the one the
# machine that handed it over holds. A pull that lands a different image and a
# transfer that lands a different image fail the same check, with the same
# sentence, and the bootstrap inside the sandbox refuses on that same
# fingerprint before it runs anything from the image.
#
# WHAT IS COMPARED, AND WHY IT IS NOT "THE IMAGE ID" (learned here, 24
# September 2026, the first time this was run). The two engines do not agree on
# what an image's id IS. This machine's engine keeps images the old way and
# reports the id of the image's CONFIG; a sandbox's engine keeps them the
# containerd way and reports the digest of the image's MANIFEST. The very same
# bytes, carried across, came back under a different "id" and this script
# refused them — correct by its own rule, and wrong about the facts. What both
# engines report identically is the LIST OF LAYERS the filesystem is made of,
# so that list, hashed, is the fingerprint compared here and inside. The two
# release labels (the version, and the hash of the manifest the image was built
# from) are checked as well, and they cross untouched.
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
# EXIT. 0 when the sandbox's own engine holds the image at the expected tag,
# made of the same layers and carrying the same release labels as this
# machine's. 2 when something was missing or misnamed at the door. 3 when the
# transfer itself failed. 4 when the image arrived but is not made of this
# machine's layers, or its release labels differ — the one case that must never
# be shrugged off, because from there on everything downstream would be running
# bytes nobody tested.

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

#: The one format string both engines answer the same way: the image's layer
#: list, one digest per line. Hashed, it is the fingerprint everything here
#: compares, and the same one the bootstrap inside the sandbox checks.
LAYERS_FORMAT='{{range .RootFS.Layers}}{{.}}{{"\n"}}{{end}}'

fingerprint_here() {
  docker image inspect --format "${LAYERS_FORMAT}" "$1" 2>/dev/null | sha256sum | cut -d' ' -f1
}
fingerprint_inside() {
  timeout 120 "${CLIENT_CALL[@]}" docker image inspect --format "${LAYERS_FORMAT}" "$1" 2>/dev/null |
    tr -d '\r' | sha256sum | cut -d' ' -f1
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
HOST_CONTENT_ID="$(fingerprint_here "${IMAGE}")"
HOST_VERSION="$(label_here "${IMAGE}" com.guardkit.release.version)"
HOST_MANIFEST="$(label_here "${IMAGE}" com.guardkit.release.manifest.sha256)"
log "this machine holds ${IMAGE}"
log "  layers fingerprint  ${HOST_CONTENT_ID}"
log "  this engine's id    ${HOST_ENGINE_ID} (not comparable across engines — see the top of this file)"
log "  release version     ${HOST_VERSION:-none recorded}"
log "  manifest hash       ${HOST_MANIFEST:-none recorded}"

# --- is it already in there, and already right? -----------------------------
already="$(fingerprint_inside "${IMAGE}")"
if [[ "${already}" == "${HOST_CONTENT_ID}" ]]; then
  log "${SANDBOX_NAME} already holds ${IMAGE} made of the same layers (${HOST_CONTENT_ID}); nothing to carry"
  log ""
  log "Give the sandbox's bootstrap these settings (names, with these values):"
  log "  FORGE_IMAGE=${IMAGE}"
  log "  FORGE_IMAGE_CONTENT_ID=${HOST_CONTENT_ID}"
  log "  FORGE_RELEASE_VERSION=${HOST_VERSION:-}"
  log "  FORGE_RELEASE_MANIFEST_SHA256=${HOST_MANIFEST:-}"
  exit 0
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
INSIDE_CONTENT_ID="$(fingerprint_inside "${IMAGE}")"
if [[ "${INSIDE_CONTENT_ID}" != "${HOST_CONTENT_ID}" ]]; then
  log "FATAL: the image ${SANDBOX_NAME} now holds under the name ${IMAGE} is made of different layers (${INSIDE_CONTENT_ID}) from this machine's (${HOST_CONTENT_ID}), so nothing may be run from it. Refusing."
  exit 4
fi
for label in com.guardkit.release.version com.guardkit.release.manifest.sha256; do
  here="$(label_here "${IMAGE}" "${label}")"
  there="$(label_inside "${IMAGE}" "${label}")"
  if [[ "${here}" != "${there}" ]]; then
    log "FATAL: the image in ${SANDBOX_NAME} says ${label} is '${there}' and this machine's says '${here}'. Refusing."
    exit 4
  fi
done

log "${SANDBOX_NAME} holds ${IMAGE}, made of the same layers as this machine's (${INSIDE_CONTENT_ID})"
log "  (that engine calls the image ${INSIDE_ENGINE_ID}; this one calls it ${HOST_ENGINE_ID}. The same bytes, two engines, two names for them — which is why the layers are what is compared)"
log ""
log "Give the sandbox's bootstrap these settings (names, with these values):"
log "  FORGE_IMAGE=${IMAGE}"
log "  FORGE_IMAGE_CONTENT_ID=${INSIDE_CONTENT_ID}"
log "  FORGE_RELEASE_VERSION=${HOST_VERSION:-}"
log "  FORGE_RELEASE_MANIFEST_SHA256=${HOST_MANIFEST:-}"
exit 0

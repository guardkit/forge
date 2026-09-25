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
# engine for all three, in one fixed order, as one line of JSON — the IDENTITY
# DOCUMENT — and hashes it. Same image, same document, same hash.
#
# AND WHY JSON AND NOT ONE FIELD PER LINE (25 September 2026, stage 4g, the
# stage 4f reviewer's second finding). Version 1 of the document wrote each
# value out raw, one per line, with nothing marking where a value ended. One
# environment variable whose value contained a newline and two ordinary
# variables came out as the same two lines and hashed the same; the reviewer
# built both images and watched the second accepted as the first. Version 2
# writes the fields through the engine's own JSON encoder, which escapes every
# newline, carriage return and tab inside a value and keeps an array's
# brackets, so nothing in a value can look like the end of it. The long note
# beside the bootstrap's copy of the format string says what that relies on.
#
# THE NAME IS RESOLVED ONCE ON EACH SIDE, AND THEN NOT USED (the same review,
# first finding). Each side asks its own engine what the name means, once, and
# every question after that — the identity, the labels — is about that image by
# the id that engine holds it under. A tag moved between two of those questions
# used to let the checks pass on one image while another was the one carried.
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
#: configuration, in one fixed order, as one line of JSON. Both kinds of engine
#: read these from the image itself, so both answer the same for the same image.
#: THESE ARE THE SAME BYTES AS THE BOOTSTRAP'S OWN COPY — see
#: forge/src/forge/cli/deploy_templates/sandbox-runner.sh,
#: IMAGE_IDENTITY_DOCUMENT_FORMAT, whose note says in full why it is JSON and
#: what that relies on. If one is ever changed, the other has to change with it,
#: and every machine's recorded identity has to be taken again.
IMAGE_IDENTITY_DOCUMENT_FORMAT='forge-image-identity/2
{"architecture":{{json .Architecture}},"os":{{json .Os}},"layers":{{if .RootFS.Layers}}{{json .RootFS.Layers}}{{else}}[]{{end}},"env":{{if .Config.Env}}{{json .Config.Env}}{{else}}[]{{end}},"entrypoint":{{if .Config.Entrypoint}}{{json .Config.Entrypoint}}{{else}}[]{{end}},"cmd":{{if .Config.Cmd}}{{json .Config.Cmd}}{{else}}[]{{end}},"user":{{json .Config.User}},"workdir":{{json .Config.WorkingDir}},"labels":{{if .Config.Labels}}{{json .Config.Labels}}{{else}}{}{{end}},"ports":{{if .Config.ExposedPorts}}{{json .Config.ExposedPorts}}{{else}}{}{{end}},"volumes":{{if .Config.Volumes}}{{json .Config.Volumes}}{{else}}{}{{end}},"stopsignal":{{json .Config.StopSignal}}}'

#: The first line of a whole identity document, and the only fixed text in one.
IDENTITY_DOCUMENT_HEADER='forge-image-identity/2'

#: CARRIAGE RETURNS ARE LINE ENDINGS HERE, NEVER PART OF A VALUE (25 September
#: 2026, stage 4g). The answer from inside travels through the sandbox client
#: and can arrive with CRLF line endings. Until this pass both scripts deleted
#: EVERY carriage return in the answer, which also deleted real ones out of the
#: middle of configuration values. In version 2 of the document a real carriage
#: return inside a value is written by the engine's JSON encoder as the two
#: characters \ and r, so the only carriage returns that can be in the rendered
#: document are the ones transport put at the ends of lines. Those, and only
#: those, are what this takes out.
only_the_line_endings() {
  local text="$1"
  text="${text//$'\r'$'\n'/$'\n'}"
  printf '%s' "${text%$'\r'}"
}

#: The same two lines on both sides: line endings normalised, exactly one
#: newline at the end, then hashed. An empty answer hashes to a perfectly
#: ordinary-looking hash, so every caller checks for one before it believes a
#: comparison.
hash_the_document() {
  local document="$1"
  [[ -n "${document}" ]] || return 1
  # The whole document or none of it: its first line is a fixed word, so an
  # engine that rendered only part of what was asked for is caught here rather
  # than having a hash taken of whatever it did say.
  [[ "${document%%$'\n'*}" == "${IDENTITY_DOCUMENT_HEADER}" ]] || return 1
  printf '%s\n' "${document}" | sha256sum | cut -d' ' -f1
}
#: AN ID IS NOT A CONFIGURATION VALUE. It is hexadecimal with a prefix and
#: holds no newline and no carriage return of its own, so taking the transport's
#: out of it, and keeping the last line of what the client printed, can lose
#: nothing that belongs to the image. The document above gets neither.
id_here() {
  docker image inspect --format '{{.Id}}' "$1" 2>/dev/null | tr -d '\r' | tail -1
}
id_inside() {
  timeout 120 "${CLIENT_CALL[@]}" docker image inspect --format '{{.Id}}' "$1" 2>/dev/null |
    tr -d '\r' | tail -1
}
identity_here() {
  local document
  document="$(only_the_line_endings "$(docker image inspect --format "${IMAGE_IDENTITY_DOCUMENT_FORMAT}" "$1" 2>/dev/null)")"
  hash_the_document "${document}"
}
identity_inside() {
  local document
  document="$(only_the_line_endings "$(timeout 120 "${CLIENT_CALL[@]}" docker image inspect --format "${IMAGE_IDENTITY_DOCUMENT_FORMAT}" "$1" 2>/dev/null)")"
  hash_the_document "${document}"
}
#: A label read as JSON, so that a value with a newline in it is one line here
#: too and the two sides are compared character for character. `tail -1` is
#: then safe against anything the sandbox client says before the answer, and so
#: is taking the carriage returns out: a real one inside the value is written
#: by the encoder as the two characters \ and r, so a literal one in what comes
#: back is the transport's.
label_here() {
  docker image inspect --format "{{json (index .Config.Labels \"$2\")}}" "$1" 2>/dev/null |
    tr -d '\r' | tail -1
}
label_inside() {
  timeout 120 "${CLIENT_CALL[@]}" docker image inspect --format "{{json (index .Config.Labels \"$2\")}}" "$1" 2>/dev/null |
    tr -d '\r' | tail -1
}
#: What a label's JSON says, as a person reads it: the quotes off, and the word
#: for nothing where there was nothing. Used only in the sentences this script
#: prints; what is COMPARED is the JSON.
plainly() {
  local value="$1"
  value="${value#\"}"
  value="${value%\"}"
  printf '%s' "${value}"
}

# --- what this machine holds ------------------------------------------------
# THE NAME IS TURNED INTO AN IMAGE ONCE, ON EACH SIDE (25 September 2026, stage
# 4g, the stage 4f reviewer's first finding). Until this pass both sides asked
# their engine for the id and then asked it again, BY THE NAME, for the identity
# and for the labels — so a tag moved between two of those questions had the
# checks pass on one image while the id belonged to another. Now the name is
# resolved once here, and every question after it is about that image, by the id
# this engine holds it under.
HOST_ENGINE_ID="$(id_here "${IMAGE}")"
if [[ -z "${HOST_ENGINE_ID}" ]]; then
  log "FATAL: this machine has no image called ${IMAGE}. Build or fetch the release first; this script only carries an image that is already here. Refusing."
  exit 2
fi
HOST_IDENTITY="$(identity_here "${HOST_ENGINE_ID}")"
if [[ -z "${HOST_IDENTITY}" ]]; then
  log "FATAL: this machine's engine will not say what the image it holds as ${HOST_ENGINE_ID} — which is what the name ${IMAGE} meant a moment ago — is made of and how it is configured, so there is nothing to compare anything with. The name is not asked again: a name can have been moved onto another image since. Refusing."
  exit 2
fi
HOST_VERSION="$(label_here "${HOST_ENGINE_ID}" com.guardkit.release.version)"
HOST_MANIFEST="$(label_here "${HOST_ENGINE_ID}" com.guardkit.release.manifest.sha256)"
log "this machine holds ${IMAGE}"
log "  image identity      ${HOST_IDENTITY} (its platform, its layers and its runtime configuration)"
log "  this engine's id    ${HOST_ENGINE_ID} (not comparable across engines — see the top of this file)"
HOST_VERSION_PLAINLY="$(plainly "${HOST_VERSION}")"
HOST_MANIFEST_PLAINLY="$(plainly "${HOST_MANIFEST}")"
log "  release version     ${HOST_VERSION_PLAINLY:-none recorded}"
log "  manifest hash       ${HOST_MANIFEST_PLAINLY:-none recorded}"

# --- the one check, wherever the image came from ----------------------------
# Both paths below end here: the one that carried the image in, and the one
# that found it already there. Before stage 4f the second path compared the
# layer list and nothing else, and never looked at the labels at all.
say_the_settings() {
  log ""
  log "Give the sandbox's bootstrap these settings (names, with these values):"
  log "  FORGE_IMAGE=${IMAGE}"
  log "  FORGE_IMAGE_IDENTITY=${HOST_IDENTITY}"
  log "  FORGE_RELEASE_VERSION=${HOST_VERSION_PLAINLY:-}"
  log "  FORGE_RELEASE_MANIFEST_SHA256=${HOST_MANIFEST_PLAINLY:-}"
}

# Says why not, and answers with a status; the caller decides what a no means.
# After a transfer a no is fatal — there is nothing left to try. Before one it
# is the reason to carry the image in.
#
# BOTH SIDES BY THEIR OWN ID (stage 4g). It is handed the id the sandbox's
# engine holds the image under, not the name, and it compares against the id
# this machine's engine holds it under: neither side looks the name up again,
# so a tag moved on either machine between two of these questions cannot make
# the checks pass on one image while another is the one carried or run.
it_is_this_machines_image() {
  local inside_reference="$1" inside_identity="$2" here there label
  if [[ "${inside_identity}" != "${HOST_IDENTITY}" ]]; then
    log "the image ${SANDBOX_NAME} holds as ${inside_reference} — what the name ${IMAGE} means in there — is not this machine's image: it is ${inside_identity:-not readable at all} where this machine's is ${HOST_IDENTITY}. That covers the platform, the layers and the whole runtime configuration, so a changed environment or entry point lands here as surely as a changed filesystem."
    return 1
  fi
  for label in com.guardkit.release.version com.guardkit.release.manifest.sha256; do
    here="$(label_here "${HOST_ENGINE_ID}" "${label}")"
    there="$(label_inside "${inside_reference}" "${label}")"
    if [[ "${here}" != "${there}" ]]; then
      log "the image in ${SANDBOX_NAME} says ${label} is '$(plainly "${there}")' and this machine's says '$(plainly "${here}")'."
      return 1
    fi
  done
  return 0
}

# --- is it already in there, and already right? -----------------------------
# THE CHECK HERE IS THE WHOLE CHECK (stage 4f). What is already in there gets
# no easier a time of it than what is carried in: same identity, same labels.
# Only then is there nothing to carry.
ALREADY_ID="$(id_inside "${IMAGE}")"
if [[ -n "${ALREADY_ID}" ]]; then
  log "${SANDBOX_NAME} already holds something called ${IMAGE} (as ${ALREADY_ID}); checking it is this machine's image before saying there is nothing to carry"
  already="$(identity_inside "${ALREADY_ID}")"
  if [[ -z "${already}" ]]; then
    log "that sandbox's engine will not say what it holds as ${ALREADY_ID} is made of and how it is configured, so nothing about it can be believed"
  elif it_is_this_machines_image "${ALREADY_ID}" "${already}"; then
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
#
# WHY THE SAVE IS STILL BY NAME, AND WHY THAT IS SAFE (stage 4g). `docker save`
# given an id carries the image with no name on it, and the sandbox's bootstrap
# has to be able to find it in there by the name it was told — so the name is
# what is saved. The name is confirmed to still mean the image whose identity
# was just recorded, immediately before the save; and after the transfer what
# came out the other end is compared against THAT IDENTITY, which was taken
# from the id. A tag moved in either gap therefore ends in a refusal here, and
# never in an image nobody checked being run.
STILL_MEANS="$(id_here "${IMAGE}")"
if [[ "${STILL_MEANS}" != "${HOST_ENGINE_ID}" ]]; then
  log "FATAL: on this machine the name ${IMAGE} no longer means the image whose identity was just recorded (${HOST_ENGINE_ID}); it now means ${STILL_MEANS:-no image at all}. Something moved the tag while this script was reading it, so what would be carried in is not what was checked. Nothing was carried. Refusing."
  exit 2
fi
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
INSIDE_ENGINE_ID="$(id_inside "${IMAGE}")"
if [[ -z "${INSIDE_ENGINE_ID}" ]]; then
  log "FATAL: after the transfer, ${SANDBOX_NAME} still has no image called ${IMAGE}. Refusing."
  exit 3
fi
INSIDE_IDENTITY="$(identity_inside "${INSIDE_ENGINE_ID}")"
if ! it_is_this_machines_image "${INSIDE_ENGINE_ID}" "${INSIDE_IDENTITY}"; then
  log "FATAL: the transfer ran and what ${SANDBOX_NAME} now holds is not this machine's image (the line above says how), so nothing may be run from it. Refusing."
  exit 4
fi

log "${SANDBOX_NAME} holds ${IMAGE}, and it is this machine's image (${INSIDE_IDENTITY})"
log "  (that engine calls the image ${INSIDE_ENGINE_ID}; this one calls it ${HOST_ENGINE_ID}. The same bytes, two engines, two names for them — which is why the identity above is what is compared, and not either name)"
say_the_settings
exit 0

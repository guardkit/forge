# ---------------------------------------------------------------------------
# THE ONE-SHOT THAT PROVISIONS THE BUS'S STORAGE.
#
# It exists only because the bus repository's two provisioning scripts need
# three things that neither the bus image nor any image already here carries
# together: the `nats` command line, `jq`, and `bash`.
#
# THE BASE IS THE NATS PROJECT'S OWN TOOL IMAGE (`natsio/nats-box`), because
# that is what it is for: it already carries the `nats` command line and `jq`,
# both from upstream and both at versions the NATS project ships together. The
# alternative was a plain base plus a downloaded release binary, which would
# put a second, hand-made supply chain into the estate for no gain.
#
# THE ONE LINE ON TOP is `bash`. The bus repository's provisioning scripts are
# bash scripts — arrays and `[[ ]]` — and `nats-box` is Alpine with `sh` only.
# Adding the shell the scripts are written in is smaller and honester than
# rewriting somebody else's scripts to fit an image.
#
# The base is PINNED in estate-pins.conf and passed in by
# ./build-estate-images.sh, so this file names no version of its own: a pin in
# two places is a pin in neither.
# ---------------------------------------------------------------------------
ARG NATS_BOX_IMAGE
FROM ${NATS_BOX_IMAGE}

RUN apk add --no-cache bash

# NO DEFINITION AND NO SCRIPT OF THE BUS'S IS COPIED IN. The bus repository's
# own provisioning scripts and its definitions arrive as a read-only mount from
# that repository at its pinned commit, so this image holds no copy of them and
# cannot drift from the bus.
#
# ONE SCRIPT OF THE ESTATE'S OWN IS (26 September 2026, build item E1): the
# read-only comparison of a bus that is already running against those pinned
# definitions, field by field. It is the estate's, not the bus's — the bus
# repository has nothing that reads a bus and compares it — and it belongs in
# this image because this is the image that already carries `curl`, `jq` and
# `bash`. It is a FILE in the image rather than a command written into the
# compose file so that a test can run it on its own against a saved answer, the
# way the estate's tests do, and so the compose file stays readable.
COPY compare-bus-with-definitions.sh /usr/local/bin/compare-bus-with-definitions.sh
RUN chmod +x /usr/local/bin/compare-bus-with-definitions.sh

# A SECOND SCRIPT OF THE ESTATE'S OWN (26 September 2026, build item E3-e): the
# gateway watch, the named replacement for the retired host alarm that asked
# systemd whether a unit was active. It is here for the same reason the
# comparison is — it needs `curl`, `jq` and `bash` together and this is the image
# that already carries all three — and it is a file rather than a command in the
# compose file so that a test can run it on its own against saved answers.
#
# THIS IMAGE IS NOW TWO THINGS AND ITS NAME SAYS ONE, which is worth noticing
# rather than papering over: it is the small tool image the estate's OWN scripts
# run in, and it happens to have been born to provision the bus. A third image in
# every release for two shell scripts that need the identical three tools would
# be worse.
COPY gateway-watch.sh /usr/local/bin/gateway-watch.sh
RUN chmod +x /usr/local/bin/gateway-watch.sh

ENTRYPOINT ["/bin/bash", "-c"]
CMD ["echo 'this image runs the bus repository''s own provisioning scripts; the estate compose file gives it the command'"]

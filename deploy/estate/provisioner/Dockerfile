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

# Nothing is copied in. The scripts and their definitions arrive as a
# read-only mount from the bus repository at its pinned commit, so this image
# holds no copy of them and cannot drift from the bus.
ENTRYPOINT ["/bin/bash", "-c"]
CMD ["echo 'this image runs the bus repository''s own provisioning scripts; the estate compose file gives it the command'"]

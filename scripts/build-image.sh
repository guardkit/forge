#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# scripts/build-image.sh — canonical Contract A producer for the forge
# production image (TASK-F009-005, FEAT-FORGE-009).
#
# Per scoping §11.4 Q4=(c), nats-core is resolved into the build via a
# BuildKit named context (``--build-context nats-core=../nats-core``).
# The relative ``../nats-core`` path is interpreted relative to the
# directory ``docker buildx`` is invoked from — that's why this script
# changes into ``forge/`` (the directory containing the Dockerfile)
# before running buildx, regardless of where the operator invokes the
# script from. From inside ``forge/``, ``../nats-core`` resolves to
# the sibling working tree (TASK-FORGE-FRR-003).
#
# Layout assumed:
#
#   …/appmilla_github/forge/                ← this project
#                    /scripts/build-image.sh ← this script
#   …/appmilla_github/nats-core/            ← sibling working tree
#   …/appmilla_github/guardkit/             ← sibling working tree (oracle payload)
#
# guardkit is wired the SAME way as nats-core — a BuildKit named context
# ``--build-context guardkit=../guardkit`` — so the Dockerfile can pip-install
# the target-terminal oracles (normalizer + ``guardkit feature validate``) that
# the B4 run 4b3b0893 caught missing from the image.
#
# After the cd, buildx runs from ``…/appmilla_github/forge/``, so:
#   * ``--build-context nats-core=../nats-core`` resolves to
#     ``…/appmilla_github/nats-core`` (the sibling).
#   * ``-f Dockerfile .`` references this project's Dockerfile and
#     uses ``forge/`` as the build context root.
#
# The canonical invocation matches RUNBOOK-FEAT-FORGE-008-validation.md
# §6.1 (LES1 §3 DKRX): the runbook and this script share the exact
# same ``docker buildx build ...`` line so a copy-paste from one to
# the other reproduces the build (TASK-F009-005 AC, B3 scenario).
#
# C3 scenario: if the BuildKit ``nats-core`` context is omitted (e.g.
# someone runs ``docker buildx build ... -f Dockerfile .`` directly
# without the ``--build-context`` flag), the build fails with a
# diagnostic naming the missing context. This script removes that
# foot-gun by always supplying the flag.
# ---------------------------------------------------------------------------

set -euo pipefail

# Resolve the script's own location and cd into forge/. The script
# lives at forge/scripts/build-image.sh, so one parent up from its
# dirname is forge/ — the directory whose Dockerfile we build and
# whose sibling ``../nats-core`` is the BuildKit named context source
# (TASK-FORGE-FRR-003).
FORGE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$FORGE_DIR"

# Sanity check the sibling working tree before invoking buildx.
# Without this, the BuildKit ``--build-context nats-core=../nats-core``
# flag would silently dereference into a non-existent directory and
# the failure would surface deep inside the Dockerfile's COPY layer
# rather than here at the entry point. The path checked here MUST be
# the same path buildx will dereference (``../nats-core`` from inside
# forge/, i.e. the sibling working tree).
if [[ ! -d "../nats-core" ]]; then
    echo "ERROR: sibling working tree ../nats-core not found relative to ${FORGE_DIR}" >&2
    echo "       The BuildKit named context --build-context nats-core=../nats-core" >&2
    echo "       requires nats-core to be checked out as a sibling of forge/." >&2
    exit 1
fi

if [[ ! -d "../nats-core/src/nats_core" ]]; then
    echo "ERROR: ../nats-core does not contain src/nats_core — layout invalid" >&2
    echo "       Expected the canonical layout from RUNBOOK-FEAT-FORGE-008-validation.md." >&2
    exit 1
fi

# Sanity check the sibling guardkit working tree — the forge-side mirror of the
# nats-core check above. guardkit supplies the target-terminal oracles (the
# normalizer + ``guardkit feature validate``); the Dockerfile installs it from
# the BuildKit named context ``--build-context guardkit=../guardkit``, so
# ``../guardkit`` (from inside forge/) must be the sibling working tree with an
# importable ``guardkit`` package. Missing here → the same class of
# deep-in-the-COPY-layer failure the nats-core check prevents.
if [[ ! -d "../guardkit" ]]; then
    echo "ERROR: sibling working tree ../guardkit not found relative to ${FORGE_DIR}" >&2
    echo "       The BuildKit named context --build-context guardkit=../guardkit" >&2
    echo "       requires guardkit to be checked out as a sibling of forge/." >&2
    exit 1
fi

if [[ ! -d "../guardkit/guardkit" ]]; then
    echo "ERROR: ../guardkit does not contain the guardkit/ package — layout invalid" >&2
    echo "       Expected the guardkit-py source checkout (packages=[\"guardkit\"])." >&2
    exit 1
fi

# Receipt line: record the guardkit commit sha being installed into the image.
# guardkit-py has no VCS-derived version (hatch version reads a static
# __version__), so the sibling checkout's HEAD sha is the honest provenance of
# the oracle payload baked into this build.
GUARDKIT_SHA="$(git -C ../guardkit rev-parse HEAD 2>/dev/null || echo unknown)"
echo "RECEIPT: installing guardkit oracle payload from ../guardkit @ ${GUARDKIT_SHA}" >&2

# Canonical BuildKit invocation — Contract A producer. Do NOT alter
# this line without updating the runbook (§6.1) and the Dockerfile-side
# literal-match test in lockstep. The whitespace and argument order
# are part of the contract. The ``guardkit`` named context (added for the
# target-terminal oracle payload, B4 run 4b3b0893) sits alongside nats-core.
docker buildx build --build-context nats-core=../nats-core --build-context guardkit=../guardkit -t forge:production-validation -f Dockerfile .

# In-container oracle smokes — every build proves its target-terminal oracles
# resolve before it can ship (the specialist verify-template-payload.sh pattern).
# A build that produced a guardkit-less image (the B4 run 4b3b0893 failure mode)
# fails HERE, at build time, instead of live mid-run.
"${FORGE_DIR}/scripts/verify-forge-oracles.sh" forge:production-validation

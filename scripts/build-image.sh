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
#   …/<checkouts>/forge/                ← this project
#                    /scripts/build-image.sh ← this script
#   …/<checkouts>/nats-core/            ← sibling working tree
#   …/<checkouts>/guardkit/             ← sibling working tree (oracle payload)
#   …/<checkouts>/fleet-memory/         ← sibling working tree (priors read)
#   …/<checkouts>/guardkitfactory/      ← sibling working tree (leg harness)
#
# guardkit is wired the SAME way as nats-core — a BuildKit named context
# ``--build-context guardkit=../guardkit`` — so the Dockerfile can pip-install
# the target-terminal oracles (normalizer + ``guardkit feature validate``) that
# the B4 run 4b3b0893 caught missing from the image.
#
# After the cd, buildx runs from ``…/<checkouts>/forge/``, so:
#   * ``--build-context nats-core=../nats-core`` resolves to
#     ``…/<checkouts>/nats-core`` (the sibling).
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

# Sanity check the sibling fleet-memory working tree — the third named
# context (the gate's priors read, forge ``memory`` extra). fleet-memory is
# not on PyPI, so the Dockerfile installs it from the BuildKit named context
# ``--build-context fleet-memory=../fleet-memory``; a missing sibling here
# is the same class of deep-in-the-COPY-layer failure the checks above
# prevent.
if [[ ! -d "../fleet-memory" ]]; then
    echo "ERROR: sibling working tree ../fleet-memory not found relative to ${FORGE_DIR}" >&2
    echo "       The BuildKit named context --build-context fleet-memory=../fleet-memory" >&2
    echo "       requires fleet-memory to be checked out as a sibling of forge/." >&2
    exit 1
fi

if [[ ! -d "../fleet-memory/src/fleet_memory" ]]; then
    echo "ERROR: ../fleet-memory does not contain src/fleet_memory — layout invalid" >&2
    echo "       Expected the fleet-memory source checkout (src/ layout)." >&2
    exit 1
fi

# Sanity check the sibling guardkitfactory working tree — the fourth named
# context (the LangGraph leg harness guardkit's ``GUARDKIT_HARNESS=langgraph``
# path imports at runtime). guardkitfactory is not on PyPI, so the Dockerfile
# installs it from the BuildKit named context
# ``--build-context guardkitfactory=../guardkitfactory``; a missing sibling
# here is the same class of deep-in-the-COPY-layer failure the checks above
# prevent — and the failure the conductor's first real leg actually hit
# in-container ("guardkitfactory is not importable").
if [[ ! -d "../guardkitfactory" ]]; then
    echo "ERROR: sibling working tree ../guardkitfactory not found relative to ${FORGE_DIR}" >&2
    echo "       The BuildKit named context --build-context guardkitfactory=../guardkitfactory" >&2
    echo "       requires guardkitfactory to be checked out as a sibling of forge/." >&2
    exit 1
fi

if [[ ! -d "../guardkitfactory/src/guardkitfactory" ]]; then
    echo "ERROR: ../guardkitfactory does not contain src/guardkitfactory — layout invalid" >&2
    echo "       Expected the guardkitfactory source checkout (src/ layout)." >&2
    exit 1
fi

# Receipt line: record the guardkit commit sha being installed into the image.
# guardkit-py has no VCS-derived version (hatch version reads a static
# __version__), so the sibling checkout's HEAD sha is the honest provenance of
# the oracle payload baked into this build.
GUARDKIT_SHA="$(git -C ../guardkit rev-parse HEAD 2>/dev/null || echo unknown)"
echo "RECEIPT: installing guardkit oracle payload from ../guardkit @ ${GUARDKIT_SHA}" >&2

# ---------------------------------------------------------------------------
# PROVENANCE — the tree this build is building, carried INTO the image.
#
# LIVE INCIDENT (2026-09-11, the go-live of forge a24a825 + 2ad935f). This
# script was run from a clean checkout. The build log printed "COPY src ./src"
# as executed, the forge wheel was rebuilt, and the runtime stage's
# "COPY --from=builder /opt/venv /opt/venv" also printed as executed, not
# cached. The oracle verification below then passed. The image nevertheless
# carried the PREVIOUS commit's code: the runner module installed in the image
# was 4,907 lines (the previous commit) against 5,061 in the tree it was built
# from, and neither new function was in it. The stale content entered at the
# runtime stage's copy of the virtual environment; nothing in the build
# noticed, because nothing in the build was comparing.
#
# Two things follow, and both are below.
#
#   1. The commit — and whether the working tree is dirty — is passed in and
#      written into the image, so any image can be asked which tree it came
#      from, and so the verification can compare the two.
#   2. Because the runtime stage declares and consumes the commit BEFORE it
#      copies the virtual environment, a runtime layer built from a DIFFERENT
#      commit can never be reused for this one. That is deliberately an input
#      rather than a cache-disabling flag: a flag makes every build slow and
#      can be left off, while an input that changes with the source keeps the
#      cache working for repeat builds of the SAME commit and cannot be
#      forgotten.
#
# A dirty working tree does NOT refuse the build — this estate builds from
# working trees — but it is recorded truthfully in the image's stamp and
# printed here in one line, so an image built from uncommitted work says so.
if ! FORGE_GIT_SHA="$(git -C "${FORGE_DIR}" rev-parse HEAD 2>/dev/null)"; then
    echo "ERROR: cannot read the commit of ${FORGE_DIR} — git is unavailable, or this is not a checkout." >&2
    echo "       Without it the image would carry no honest record of the code inside it," >&2
    echo "       and the check that compares the two could not run. Fix that before building." >&2
    exit 1
fi

if [[ -n "$(git -C "${FORGE_DIR}" status --porcelain 2>/dev/null)" ]]; then
    FORGE_GIT_DIRTY=true
    echo "PROVENANCE: building from commit ${FORGE_GIT_SHA} PLUS uncommitted changes in ${FORGE_DIR} — the image will say so." >&2
else
    FORGE_GIT_DIRTY=false
    echo "PROVENANCE: building from commit ${FORGE_GIT_SHA}, with no uncommitted changes." >&2
fi

# Canonical BuildKit invocation — Contract A producer. Do NOT alter
# this line without updating the runbook (§6.1) and the Dockerfile-side
# literal-match test in lockstep. The whitespace and argument order
# are part of the contract. The ``guardkit`` named context (added for the
# target-terminal oracle payload, B4 run 4b3b0893) sits alongside nats-core;
# the ``fleet-memory`` named context (the gate's priors read) sits third; the
# ``guardkitfactory`` named context (the LangGraph leg harness, missing from
# the image when the conductor's first real leg ran) sits fourth.
#
# The two provenance arguments are APPENDED after the context ``.`` on
# purpose: the contract line above them is matched byte for byte by the
# runbook and by three test files, so adding anything inside it would break
# four consumers at once. Docker accepts flags after the positional context,
# and appending is the only placement where every existing byte keeps its
# position.
docker buildx build --build-context nats-core=../nats-core --build-context guardkit=../guardkit --build-context fleet-memory=../fleet-memory --build-context guardkitfactory=../guardkitfactory -t forge:production-validation -f Dockerfile . --build-arg FORGE_GIT_SHA="${FORGE_GIT_SHA}" --build-arg FORGE_GIT_DIRTY="${FORGE_GIT_DIRTY}"

# The image's own proof, run before it can ship. It now does two jobs, in this
# order: first it compares the forge package inside the image with the forge
# package in this tree, file by file, and fails the build if they differ (the
# 2026-09-11 incident above); then the in-container oracle smokes — every build
# proves its target-terminal oracles resolve (the specialist
# verify-template-payload.sh pattern). A build that produced a guardkit-less
# image (the B4 run 4b3b0893 failure mode), or an image carrying another
# commit's code, fails HERE, at build time, instead of live mid-run.
"${FORGE_DIR}/scripts/verify-forge-oracles.sh" forge:production-validation

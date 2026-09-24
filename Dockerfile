# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# Forge production image — multi-stage (TASK-F009-002 skeleton +
# TASK-F009-005 install layer).
#
# Per ASSUM-010 + ADR-ARCH-032:
#   * Both stages MUST start FROM python:3.14-slim-bookworm pinned to the
#     SAME sha256 digest. A floating tag would re-introduce the supply
#     chain regression that scenario E1.1 forbids.
#   * The runtime stage MUST run as a non-root ``forge`` user (UID 1000)
#     so ``docker run --rm forge:skel id -u`` returns 1000 (scenario C2,
#     acceptance criterion AC-G).
#   * No real provider API keys, no .env, no SSH server, no remote
#     debugger may appear in either stage (scenarios C1, E1.3).
#
# TASK-F009-005 wires nats-core via the operator-decided
# ``Q4 = (c) BuildKit named build context`` (scoping §11.4):
#   * ``COPY --from=nats-core / /tmp/nats-core`` pulls the sibling
#     working tree into the build via
#     ``--build-context nats-core=../nats-core``.
#   * nats-core is passed as a local path in the same resolver transaction as
#     Forge, GuardKit and guardkitfactory, so pip never reaches PyPI for the
#     malformed 0.2.0 wheel (TASK-FIX-F0E6 / TASK-REV-F0E4 §5.1).
#   * ``pyproject.toml`` is NOT mutated inside the layer — the dev-host
#     ``[tool.uv.sources]`` semantics are preserved (scoping §11.4).
#   * Only the resolved venv crosses the builder→runtime boundary; gcc,
#     build-essential, and apt-cache state stay behind in the discarded
#     builder layer.
#
# Digest source: ``docker buildx imagetools inspect
# python:3.14-slim-bookworm`` resolved on 2026-04-22 (image revision
# 6cc07b27ad0df3769bbd1a2a1000a842634681d2, python 3.14.4-slim-bookworm).
# T7's update-annotations CI workflow watches this digest for drift.
# ---------------------------------------------------------------------------

ARG PYTHON_BASE_DIGEST=sha256:2e256d0381371566ed96980584957ed31297f437569b79b0e5f7e17f2720e53a

# ---------------------------------------------------------------------------
# Stage 1: builder
#
# Compiles the production venv at /opt/venv. The stage adds
# build-essential/gcc transiently to handle wheel compilation for any
# dependency that lacks a pre-built distribution; those packages do
# NOT cross to the runtime stage — only ``/opt/venv`` does.
# ---------------------------------------------------------------------------
FROM python:3.14-slim-bookworm@sha256:2e256d0381371566ed96980584957ed31297f437569b79b0e5f7e17f2720e53a AS builder

# Sensible Python defaults for build environments — avoids stale .pyc
# layers and silences pip's root-user warning.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Build-time toolchain: gcc + build-essential cover the C-extension
# wheels (e.g. cryptography fallbacks) that PyPI may not pre-build for
# Python 3.14. ``apt-get clean`` + the ``rm -rf`` keep the layer lean
# in case a future change adds a builder-stage publish step. None of
# this crosses to the runtime stage.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        gcc \
        build-essential \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Create the production virtualenv at /opt/venv and front-load it on
# PATH so subsequent ``pip install`` calls write into the venv rather
# than the system Python. Only this directory crosses to runtime.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# Pull the BuildKit-named ``nats-core`` context. The named context is
# supplied by ``scripts/build-image.sh`` via
# ``--build-context nats-core=../nats-core``. Mirroring to a known
# absolute path (/tmp/nats-core) decouples the install commands below
# from the buildx invocation cwd.
COPY --from=nats-core / /tmp/nats-core

# R3 mitigation: refuse to proceed if the COPYed working tree is
# missing the expected ``src/nats_core`` package layout. Without this
# gate, a stale or empty sibling checkout would surface as a confusing
# pip resolution error several layers down. The ``echo ... >&2`` writes
# the diagnostic to stderr so ``docker buildx build`` highlights it.
RUN test -d /tmp/nats-core/src/nats_core || (echo "nats-core layout invalid" >&2; exit 1)

# Keep this private source staged until the combined resolver transaction
# below. Passing the local directory there prevents use of the malformed
# public nats-core wheel.

# Pull the BuildKit-named ``fleet-memory`` context — the gate's priors
# read (forge.adapters.fleet_memory, the ``memory`` extra). fleet-memory
# is not on PyPI; like nats-core it resolves from the sibling working
# tree, supplied by ``scripts/build-image.sh`` via
# ``--build-context fleet-memory=../fleet-memory``.
COPY --from=fleet-memory / /tmp/fleet-memory

# R3-style layout gate (mirrors the nats-core gate above): refuse to
# proceed if the COPYed working tree is missing the expected
# ``src/fleet_memory`` package layout, so a stale or empty sibling
# checkout fails fast with a named diagnostic instead of a confusing
# pip resolution error several layers down.
RUN test -d /tmp/fleet-memory/src/fleet_memory || (echo "fleet-memory layout invalid" >&2; exit 1)

# Keep fleet-memory staged for the combined transaction. It has no public
# distribution, so the local path is the only permitted source.

# Copy the forge sources late so changes to forge code don't bust the
# nats-core install cache layer above. ``pyproject.toml`` is NOT
# mutated — scoping §11.4 mandates preserving dev-host editable
# semantics, and pip already considers nats-core satisfied above.
COPY pyproject.toml ./
COPY README.md ./
# The image lock freezes every third-party distribution from the last accepted
# Python 3.14 image. Local project distributions remain source-path inputs to
# the single resolver transaction below.
COPY requirements-image-py314.lock ./
COPY src ./src

# Forge is staged until guardkitfactory and GuardKit have also been copied.
# The single transaction below resolves one dependency set for every project.

# ---------------------------------------------------------------------------
# guardkitfactory — the LangGraph leg-harness runtime.
#
# LIVE INCIDENT (the conductor's first real leg, 2026-08-03): the leg died
# in-container with ``GUARDKIT_HARNESS=langgraph but guardkitfactory is not
# importable`` (guardkit/orchestrator/harness/selector.py:425). This image
# baked guardkit but never the harness runtime guardkit's langgraph path
# imports, so NO langgraph-harness leg could run at all.
#
# Wired exactly like nats-core / fleet-memory: a BuildKit named context
# supplied by ``scripts/build-image.sh`` via
# ``--build-context guardkitfactory=../guardkitfactory``. guardkitfactory has
# no PyPI distribution, so the sibling working tree is the only source.
COPY --from=guardkitfactory / /tmp/guardkitfactory

# R3-style layout gate (mirrors the nats-core / fleet-memory gates above): a
# stale or empty sibling checkout fails fast with a named diagnostic instead
# of a confusing pip resolution error several layers down.
RUN test -d /tmp/guardkitfactory/src/guardkitfactory || (echo "guardkitfactory layout invalid" >&2; exit 1)

# guardkitfactory is a private BuildKit context and participates in the same
# transaction as Forge and GuardKit. One resolver must accept every project's
# constraints; sequential "last install wins" overrides are forbidden. The
# image oracle later checks real graph capability, Forge's constructed async
# protocol, and the exact Deep Agents version.

# ---------------------------------------------------------------------------
# guardkit oracle payload + CLI — forge-side mirror of the specialist's
# template-payload fix (specialist-agent 2708d0a).
#
# LIVE INCIDENT (B4 run 4b3b0893, round 5): the target-terminal pre-commit
# oracles are guardkit code this image never installed. The normalizer leg
# (``python -m installer.core.commands.lib.feature_spec_normalize``) died
# in-container with ``ModuleNotFoundError: No module named 'installer'`` AFTER
# the reply had been projected and the branch written; the ``guardkit feature
# validate`` plan-leg oracle would have hit the same wall a step later (its
# ``/usr/local/bin/guardkit`` binary did not exist either).
#
# FIX: install guardkit from the BuildKit named context ``guardkit`` (supplied
# by scripts/build-image.sh via ``--build-context guardkit=../guardkit``), the
# same mechanism nats-core already uses. This image is python:3.14 and
# guardkit-py declares requires-python>=3.12, so a REAL pip install resolves —
# unlike the specialist's python:3.11 image, which had to vendor the
# distribution as data. The pip install yields BOTH seams at once:
#   * seam 1 — the DF-011 wheel exposes the normalizer at
#     ``guardkit._installer_core.commands.lib.feature_spec_normalize`` (hatch
#     force-include of ``installer/core`` under the guardkit namespace); the
#     forge normalizer resolver (target_terminal_tools.resolve_normalizer_command)
#     prefers this path in-container.
#   * seam 2 — the ``guardkit-py`` console-script entry point, symlinked to
#     ``/usr/local/bin/guardkit`` in the runtime stage below so the frozen
#     ``forge.adapters.guardkit.run`` boundary (``_GUARDKIT_BINARY``) resolves.
#
# NOT ONE guardkit byte is authored or altered (DF-019); we only install and
# invoke. The sibling ../guardkit source is required at build time — the build
# script verifies it, stages it, and records the installed commit sha as a
# receipt line. Only ``/opt/venv`` crosses to the runtime stage.
COPY --from=guardkit /pyproject.toml /tmp/guardkit/pyproject.toml
COPY --from=guardkit /README.md /tmp/guardkit/README.md
COPY --from=guardkit /LICENSE /tmp/guardkit/LICENSE
COPY --from=guardkit /guardkit /tmp/guardkit/guardkit
COPY --from=guardkit /installer/core /tmp/guardkit/installer/core

# R3-style layout gate (mirrors the nats-core gate): refuse to proceed if the
# COPYed guardkit tree is missing the ``guardkit`` package dir, so a stale or
# empty sibling checkout fails fast with a named diagnostic instead of a
# confusing pip resolution error several layers down.
RUN test -d /tmp/guardkit/guardkit || (echo "guardkit layout invalid" >&2; exit 1)

# Resolve every project and private source once. The explicit SDK argument
# makes the actual image install command agree with Forge's declaration.
# ``pip check`` must be green before the runtime imports are accepted.
RUN pip install \
        /tmp/nats-core \
        /tmp/fleet-memory \
        '.[providers,memory]' \
        /tmp/guardkitfactory \
        /tmp/guardkit \
        --requirement /build/requirements-image-py314.lock \
        'deepagents==0.7.14' \
    && pip check \
    && python -c "import importlib.metadata as m; import forge, guardkit, guardkit._installer_core, guardkitfactory; assert m.version('deepagents') == '0.7.14'"

# ---------------------------------------------------------------------------
# Stage 2: runtime
#
# Minimal surface: copy the resolved venv from the builder stage, add
# curl for the HEALTHCHECK probe and git for the Mode P planning
# PLANNED-HANDOFF terminal (in-process ``WorktreeGitRunner`` shells out
# to ``git worktree add``; TASK-FWD-PLAN-GITMOUNT), and run as the
# unprivileged ``forge`` user. No package install beyond curl and git,
# no SSH, no debugger, no secrets.
# ---------------------------------------------------------------------------
FROM python:3.14-slim-bookworm@sha256:2e256d0381371566ed96980584957ed31297f437569b79b0e5f7e17f2720e53a AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Default forge daemon URL per ASSUM-001. Operators override at
    # ``docker run -e FORGE_NATS_URL=...`` time.
    FORGE_NATS_URL=nats://127.0.0.1:4222

# Healthz port mirrors ``forge.cli.serve.DEFAULT_HEALTHZ_PORT`` (Contract
# B consumer; ASSUM-005). Both the HEALTHCHECK below and ``forge serve``
# read this env so they cannot drift. The seam test in
# ``tests/dockerfile/test_install_layer.py`` enforces equivalence at
# CI time. This directive is intentionally on its own line so the
# regex ``^ENV\s+FORGE_HEALTHZ_PORT=`` (re.MULTILINE) anchors against it.
ENV FORGE_HEALTHZ_PORT=8080

# Front-load the venv shim onto PATH so ``forge`` resolves to the
# console-script entry produced by the combined builder install rather
# than the system-python executable. Setting PATH on its own line
# (not folded into the multi-line ENV above) avoids a continuation
# backslash splitting the literal across lines.
ENV PATH="/opt/venv/bin:${PATH}"

# curl is required by HEALTHCHECK and git by the Mode P planning
# PLANNED-HANDOFF terminal (in-process ``WorktreeGitRunner``); neither
# is in the slim-bookworm base. Install with ``--no-install-recommends``
# to keep the layer small and ``rm -rf /var/lib/apt/lists/*`` to drop
# the apt cache. These are the only packages added to the runtime
# stage; gcc and build-essential live exclusively in the discarded
# builder stage.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends curl git nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 2026-09-06 — the live gate's Hurl twins run in this container (the live-gate
# driver is an in-process subprocess here), and the first real deploy into a
# Docker Sandbox reported "hurl binary not on PATH" for exactly that reason.
# Same version as the host (hurl 8.0.1, aarch64). The release's Debian package
# is used so its shared libraries (libxml2, libcurl) come with it; the bare
# tarball binary does not run on slim-bookworm. Proven to run in this layer.
ARG HURL_VERSION=8.0.1
RUN curl -fsSL "https://github.com/Orange-OpenSource/hurl/releases/download/${HURL_VERSION}/hurl_${HURL_VERSION}_arm64.deb" \
        -o /tmp/hurl.deb \
    && apt-get update \
    && apt-get install --yes --no-install-recommends /tmp/hurl.deb \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/hurl.deb \
    && hurl --version | grep -q "hurl ${HURL_VERSION}"

# ---------------------------------------------------------------------------
# THE DOCKER CLIENT — THE COMMAND ONLY, AND NEVER A DAEMON
# 2026-09-24, stage 4e of the containerisation rollout gate.
#
# WHY THE FACTORY NEEDS THIS, and it is the factory's own need and not any
# project's. One of the two services that run from this image is the DEPLOY
# HELPER, and the helper's whole job is to run the deploy, health-check and
# live-gate commands A PROJECT DECLARES in its own profile — whatever they are.
# The factory does not know or care what a project's deploy is written in; it
# knows only that it runs the project's own vetted script. A great many
# projects deploy by bringing containers up, so the helper has to be able to
# run a `docker` command when a project's declared deploy uses one. Nothing
# here names a project, a language, a test runner or a package manager, and no
# project is required to use containers: this is the factory being able to run
# what it is handed.
#
# From stage 4d the helper runs inside a project's Docker Sandbox, and the
# bootstrap there (src/forge/cli/deploy_templates/sandbox-runner.sh) binds THE
# SANDBOX'S OWN engine socket into the helper's container for exactly this.
# Until this layer existed that socket had nothing in the image to use it, and
# a project whose deploy runs containers failed inside the helper — the stage
# 4d reviewer confirmed `command -v docker` in the release image found nothing.
#
# WHAT IS INSTALLED, AND WHAT IS NOT. Only `docker`, the client: one static
# binary lifted out of Docker's own static release tarball, pinned by version
# and verified against the sha256 recorded here before anything is unpacked.
# The tarball also holds `dockerd`, `containerd`, `runc`, `ctr`,
# `docker-proxy`, `docker-init` and the runc shim, and NONE of them is
# extracted: there is no daemon in this image, nothing in it listens, and the
# client can only ever speak to an engine whose socket something outside
# deliberately binds in. The version matches the engine the sandboxes run
# (29.8.1), and the architecture follows the hurl layer above — this image is
# built for arm64 today.
ARG DOCKER_CLI_VERSION=29.8.1
ARG DOCKER_CLI_SHA256=667395fbffab52901b80181dfbb39ea76da2fbd7642c4fbddd24e42146b07b48
RUN curl -fsSL "https://download.docker.com/linux/static/stable/aarch64/docker-${DOCKER_CLI_VERSION}.tgz" \
        -o /tmp/docker-cli.tgz \
    && echo "${DOCKER_CLI_SHA256}  /tmp/docker-cli.tgz" | sha256sum --check --strict - \
    && tar --extract --file /tmp/docker-cli.tgz --directory /usr/local/bin \
        --strip-components=1 docker/docker \
    && rm -f /tmp/docker-cli.tgz \
    && chmod 0755 /usr/local/bin/docker \
    && docker --version | grep -q "${DOCKER_CLI_VERSION}" \
    && test ! -e /usr/local/bin/dockerd \
    && test ! -e /usr/local/bin/containerd \
    && test ! -e /usr/local/bin/runc

# 2026-08-15 — HISTORICAL REASON, LIVE PACKAGE. guardkit deleted the DCL spec
# track outright (guardkit b138d92c) and forge's W1-S2 leg went with it, so
# nothing shells the vendored checker any more. ``nodejs`` and the flag below
# are LEFT IN PLACE deliberately: removing a runtime package is an image
# question of its own, not a side effect of striking a planning leg.
#
# The original reason, kept for the record: guardkit's vendored DCL checker
# (qa/dcl/bin/dcl_check.mjs, WASM) needs a node runtime — the ``guardkit dcl
# author``/oracle legs shelled it in-container (first live-caught 2026-07-18,
# run s3dcl-6e6bdabea57c: exit-2 instrument error, node absent). Bookworm ships
# node 18, whose Go wasm_exec requires the webcrypto global behind a flag
# (global by default only from node 19).
ENV NODE_OPTIONS=--experimental-global-webcrypto

# ---------------------------------------------------------------------------
# PROVENANCE — which tree this image was built from, and the cache barrier.
#
# LIVE INCIDENT (2026-09-11, the go-live of forge a24a825 + 2ad935f): a build
# from a clean checkout printed "COPY src ./src" and the runtime stage's
# "COPY --from=builder /opt/venv /opt/venv" as executed rather than cached,
# rebuilt the forge wheel, passed the oracle verification — and shipped the
# PREVIOUS commit's code. The installed runner module was 4,907 lines against
# 5,061 in the tree it was built from. Rebuilding with the builder stage's
# cache disabled changed nothing; rebuilding with the RUNTIME stage's cache
# disabled produced the correct code. The stale content entered here, at the
# copy of the virtual environment out of the builder.
#
# These three instructions are the guard, and their PLACEMENT is the whole of
# it: they are declared and consumed BEFORE the COPY below, so every layer
# from here down carries the commit in its cache key and cannot be reused from
# a build of a different commit. They sit after the apt and hurl layers on
# purpose, so those stay cached and a rebuild does not re-download anything.
#
# The stamp file is what ``scripts/verify-forge-oracles.sh`` reads to prove the
# image knows its own origin; the two environment variables are the same two
# facts, readable from a running container without shelling in for a file.
# ``dirty=true`` is honest, not fatal: this estate builds from working trees.
ARG FORGE_GIT_SHA
ARG FORGE_GIT_DIRTY=unknown

ENV FORGE_GIT_SHA=${FORGE_GIT_SHA} \
    FORGE_GIT_DIRTY=${FORGE_GIT_DIRTY}

RUN test -n "${FORGE_GIT_SHA}" \
        || (echo "FORGE_GIT_SHA build argument is empty — build this image with scripts/build-image.sh, which computes the commit and passes it in" >&2; exit 1) \
    && printf 'commit=%s\ndirty=%s\n' "${FORGE_GIT_SHA}" "${FORGE_GIT_DIRTY}" > /etc/forge-image-provenance \
    && cat /etc/forge-image-provenance

# Bring the resolved venv across from the builder stage. Owned by root
# so the unprivileged ``forge`` user can read but not modify the
# installed distributions — matches a hardened production posture.
COPY --from=builder /opt/venv /opt/venv

# seam 2: the frozen ``forge.adapters.guardkit.run`` boundary shells the
# guardkit binary at the absolute path ``/usr/local/bin/guardkit``
# (``_GUARDKIT_BINARY``). The guardkit-py distribution installs its console
# script as ``guardkit-py`` (pyproject [project.scripts]); symlink the canonical
# name so the ``guardkit feature validate`` plan-leg oracle resolves without
# touching the frozen adapter. The venv crossed from the builder above, so the
# target exists at this point.
RUN ln -s /opt/venv/bin/guardkit-py /usr/local/bin/guardkit

# The sandbox runner's supervisor script (added 2026-09-24, stage 3 of the
# containerisation rollout gate). It is the start command of the compose
# service deploy/compose/compose.sandbox-runner.yaml, which replaces the two
# host units that looked after a project's sandbox. It lives in the image
# rather than being bound in from a folder, because a bind is a path and a
# path belongs to one machine — which is the whole of what this gate is about.
# It is a leaf: nothing else in the image reads it, and every other start
# command is unaffected.
COPY deploy/compose/sandbox-runner/run.sh /opt/forge/sandbox-runner/run.sh
RUN chmod 0755 /opt/forge/sandbox-runner/run.sh

# Create the unprivileged runtime user *before* WORKDIR/COPY-into-home
# so any files copied later inherit the correct ownership when --chown
# is used. UID 1000 is mandated by AC-C and the ``id -u`` runtime
# assertion. ``useradd`` is kept on a single line so static-analysis
# tools that scan the Dockerfile per-instruction (and the digest-pinning
# test in tests/dockerfile/) can match the UID-1000 assertion without
# needing to span backslash-continued lines.
RUN groupadd --system --gid 1000 forge \
    && useradd --system --uid 1000 --gid 1000 --home-dir /home/forge --create-home --shell /usr/sbin/nologin forge

# ---------------------------------------------------------------------------
# THE FOUR FOLDERS THE COMPOSE BUNDLE MOUNTS VOLUMES ON, made here and owned
# by the user this image runs as (24 September 2026, stage 2b of the
# containerisation rollout gate).
#
# WHY THIS IS AN IMAGE CHANGE AND NOT A COMPOSE ONE. Docker fills a FRESH
# named volume from whatever the image has at that path — contents and
# ownership together — and where the image has nothing, it makes an empty
# folder owned by root. All four of these mounts used to land on nothing, so
# the first start on a clean machine handed an unprivileged service four
# root-owned folders: the coordinator could not write its own record, and
# deploy/compose/README.md had to tell the operator to run a chown over the
# four volumes before the first start. That step is now deleted, because this
# is where it belonged: a step a person has to remember is a step a clean
# machine gets wrong.
#
# The paths are exactly the four volume mounts in deploy/compose/compose.yaml:
# the record, the evidence, the settings (mounted read-only there — the
# machine puts the settings file on the volume before anything starts) and
# this service's own small state under its home. Keep the two in step.
#
# The publisher's image does the same thing for its own one folder, in its own
# way: it runs ``mkdir -p /home/publisher/state`` AFTER dropping to the
# publisher user, so the folder is made owned by that user without a chown.
RUN mkdir -p /var/lib/forge /var/lib/forge-evidence /etc/forge /home/forge/.forge \
    && chown forge:forge /var/lib/forge /var/lib/forge-evidence /etc/forge /home/forge/.forge

WORKDIR /home/forge

# Drop privileges before declaring the entrypoint so the container's
# PID 1 is the unprivileged ``forge`` user (scenario C2, AC-G).
USER forge

# Health probe lives at TCP 8080 per ASSUM-005. Documenting the port
# now makes ``docker run -p 8080:8080`` work without surprises. Only
# port 8080 may be EXPOSEd — listing other ports here would signal
# that they exist (E1.3 forbids SSH/debug surfaces).
EXPOSE 8080

# Contract B consumer: probe the same /healthz endpoint the daemon
# binds in ``forge.cli._serve_healthz``. ``-fs`` makes curl exit
# non-zero on HTTP 4xx/5xx and silences progress output; ``|| exit 1``
# guarantees an explicit non-zero healthcheck exit code so Docker
# reports the container as ``unhealthy`` rather than ``starting``.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fs http://localhost:${FORGE_HEALTHZ_PORT:-8080}/healthz || exit 1

# Exec form is required: shell form would route through /bin/sh and
# break signal forwarding to the Python daemon (SIGTERM-on-stop must
# reach forge serve cleanly so JetStream consumer drains gracefully).
ENTRYPOINT ["forge"]
CMD ["serve"]

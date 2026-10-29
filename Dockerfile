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
#   * Only the resolved venv crosses the builder→runtime boundary; the
#     builder's gcc, build-essential, and apt-cache state stay behind in the
#     discarded builder layer. (Since 12 October 2026 the runtime stage
#     installs its own gcc and libc6-dev, for Go's race detector — see the
#     toolchain layers there. build-essential and g++ are still builder-only.)
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
# Stage 1b: the Opengrep security rules for the nightly security sweep
# (security check design, 17 October 2026, piece 2).
#
# WHAT IT MAKES. One file, /out/security-rules.yaml, holding every rule of
# the opengrep/opengrep-rules repository, at the commit pinned below, whose
# metadata says "category: security", except the rules that detect a secret
# value, which are Gitleaks' job (1,338 rules kept at this commit, 225 left
# out and listed in deploy/security/dropped-secret-rules.txt;
# deploy/security/build_opengrep_rules.py says exactly which files it reads
# and which rules it leaves out). The runtime stage copies only that file
# and its sha256 record; the checkout and git stay here, in a stage that is
# thrown away.
#
# HOW THE DOWNLOAD IS TRUSTED, said plainly. It is NOT a release archive with
# a published checksum like the tools in the runtime stage. Git fetches the
# one pinned commit by its id and checks every object it receives against
# that id, so the content is the commit's — but that check is git's SHA-1,
# weaker than the sha256 checks used for the archives, and nobody signs the
# commit. On top of it, the sha256 of the FILTERED RESULT is pinned here
# (OPENGREP_RULES_SHA256): a different download, a changed filter or a
# PyYAML that reads the rules differently all fail the build rather than
# shipping different rules. The filter needs only PyYAML, copied from the
# builder's own environment (the image lock's 6.0.3); nothing is installed
# for it. Network is needed at image build time only.
#
# THE LICENCE. The rules carry the "Commons Clause" condition: internal use is
# allowed, selling the rules is not. Fine for the factory; noted for any
# future client sale.
# ---------------------------------------------------------------------------
FROM python:3.14-slim-bookworm@sha256:2e256d0381371566ed96980584957ed31297f437569b79b0e5f7e17f2720e53a AS opengrep-rules

ARG OPENGREP_RULES_COMMIT=f1d2b562b414783763fd02a6ed2736eaed622efa
ARG OPENGREP_RULES_SHA256=ba63c213f10965b80822c3427eb691e7c1b958e6e92b5ba5b9c004a9030f61d2
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates git \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && git init --quiet /src/opengrep-rules \
    && git -C /src/opengrep-rules fetch --quiet --depth 1 \
        https://github.com/opengrep/opengrep-rules.git "${OPENGREP_RULES_COMMIT}" \
    && git -C /src/opengrep-rules -c advice.detachedHead=false checkout --quiet FETCH_HEAD \
    && test "$(git -C /src/opengrep-rules rev-parse HEAD)" = "${OPENGREP_RULES_COMMIT}"
COPY --from=builder /opt/venv/lib/python3.14/site-packages/yaml /opt/rules-build/yaml
COPY deploy/security/build_opengrep_rules.py /opt/rules-build/build_opengrep_rules.py
RUN PYTHONPATH=/opt/rules-build python /opt/rules-build/build_opengrep_rules.py \
        /src/opengrep-rules /out/security-rules.yaml \
    && echo "${OPENGREP_RULES_SHA256}  /out/security-rules.yaml" | sha256sum --check --strict - \
    && echo "${OPENGREP_RULES_SHA256}  security-rules.yaml" > /out/security-rules.yaml.sha256

# ---------------------------------------------------------------------------
# Stage 2: runtime
#
# Copy the resolved venv from the builder stage, add curl for the
# HEALTHCHECK probe and git for the Mode P planning PLANNED-HANDOFF
# terminal (in-process ``WorktreeGitRunner`` shells out to ``git worktree
# add``; TASK-FWD-PLAN-GITMOUNT), add the pinned tools the builds and
# checks run (hurl, the docker client and Compose, code search, and since
# 12 October 2026 Node, Go, Staticcheck, gcc and the Playwright browsers;
# since 17 October 2026 Gitleaks, OSV-Scanner and Opengrep), and run as the
# unprivileged ``forge`` user. No SSH, no debugger, no
# secrets.
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
# is in the slim-bookworm base. xz-utils unpacks the Node release
# archive further down (12 October 2026). Install with
# ``--no-install-recommends`` to keep the layer small and
# ``rm -rf /var/lib/apt/lists/*`` to drop the apt cache. Debian's
# ``nodejs`` (Node 18, no npm) was removed from this line on 12 October
# 2026: the pinned Node 22 below replaces it. build-essential and g++
# still live only in the discarded builder stage.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends curl git xz-utils \
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

# 2 October 2026: the Compose plugin too. A project's own test suite may bring a
# whole stack up with `docker compose` (api_test's round-trip smoke does), and
# the build runner runs that suite in this image against the sandbox's engine.
# Without the plugin `docker compose` is "unknown command", that test can never
# pass, and the build's checker rejected every task of the first live feature on
# 2 October. One static plugin binary, pinned to the version the sandboxes run
# and verified against the sha256 Docker publishes for it; still no daemon.
ARG DOCKER_COMPOSE_VERSION=5.5.0
ARG DOCKER_COMPOSE_SHA256=ff42489f5a9b879d5d117c5ffea6defc27390b3286da8ad52cbc9c6ab5df590e
RUN mkdir -p /usr/local/lib/docker/cli-plugins \
    && curl -fsSL "https://github.com/docker/compose/releases/download/v${DOCKER_COMPOSE_VERSION}/docker-compose-linux-aarch64" \
        -o /usr/local/lib/docker/cli-plugins/docker-compose \
    && echo "${DOCKER_COMPOSE_SHA256}  /usr/local/lib/docker/cli-plugins/docker-compose" | sha256sum --check --strict - \
    && chmod 0755 /usr/local/lib/docker/cli-plugins/docker-compose \
    && docker compose version | grep -q "${DOCKER_COMPOSE_VERSION}"

# 12 October 2026 — code search for the planner (through the sandbox helper)
# and the coding Player and Coach: ripgrep, ast-grep and Probe, each one
# official Linux ARM64 release archive pinned by version and sha256 and
# checked before anything is unpacked. They go in /opt/code-search/bin, which
# is deliberately on no PATH: the agents' shell runs with PATH=/usr/bin:/bin,
# where `sg` is the system's newgrp, and Deep Agents' built-in grep would
# silently start using an `rg` it found on PATH. GuardKitFactory's
# code_search module calls them by absolute path and checks each executable's
# own sha256 and version again before first use. No index, daemon, model or
# download at run time. ripgrep and Probe are static builds; ast-grep needs
# glibc 2.18 at most. The self-check further down runs them in this image.
ARG RIPGREP_VERSION=15.2.0
ARG RIPGREP_SHA256=800b1e7206afe799dfb5a6901f23147cfaabe0e52210538100f61e86e1740915
ARG AST_GREP_VERSION=0.50.0
ARG AST_GREP_SHA256=3a4ad22dd1dca7a4900a173de65885578617af57fa13dd93b8d345d6b352770e
ARG PROBE_VERSION=0.6.0-rc341
ARG PROBE_SHA256=4fa43d6259428727444f7b2415f407e0697e0da8424ded3a16b7784832773032
RUN mkdir -p /opt/code-search/bin \
    && curl -fsSL "https://github.com/BurntSushi/ripgrep/releases/download/${RIPGREP_VERSION}/ripgrep-${RIPGREP_VERSION}-aarch64-unknown-linux-musl.tar.gz" \
        -o /tmp/ripgrep.tgz \
    && echo "${RIPGREP_SHA256}  /tmp/ripgrep.tgz" | sha256sum --check --strict - \
    && tar --extract --file /tmp/ripgrep.tgz --directory /opt/code-search/bin \
        --strip-components=1 "ripgrep-${RIPGREP_VERSION}-aarch64-unknown-linux-musl/rg" \
    && curl -fsSL "https://github.com/ast-grep/ast-grep/releases/download/${AST_GREP_VERSION}/app-aarch64-unknown-linux-gnu.zip" \
        -o /tmp/ast-grep.zip \
    && echo "${AST_GREP_SHA256}  /tmp/ast-grep.zip" | sha256sum --check --strict - \
    && python -c "import zipfile; zipfile.ZipFile('/tmp/ast-grep.zip').extract('ast-grep', '/opt/code-search/bin')" \
    && curl -fsSL "https://github.com/probelabs/probe/releases/download/v${PROBE_VERSION}/probe-v${PROBE_VERSION}-aarch64-unknown-linux-musl.tar.gz" \
        -o /tmp/probe.tgz \
    && echo "${PROBE_SHA256}  /tmp/probe.tgz" | sha256sum --check --strict - \
    && tar --extract --file /tmp/probe.tgz --directory /opt/code-search/bin \
        --strip-components=1 "probe-v${PROBE_VERSION}-aarch64-unknown-linux-musl/probe" \
    && rm -f /tmp/ripgrep.tgz /tmp/ast-grep.zip /tmp/probe.tgz \
    && chmod 0755 /opt/code-search/bin/rg /opt/code-search/bin/ast-grep /opt/code-search/bin/probe \
    && /opt/code-search/bin/rg --version | grep -q "ripgrep ${RIPGREP_VERSION}" \
    && /opt/code-search/bin/ast-grep --version | grep -q "ast-grep ${AST_GREP_VERSION}" \
    && /opt/code-search/bin/probe --version | grep -q "probe-code" \
    && test ! -e /usr/local/bin/rg && test ! -e /usr/local/bin/sg

# ---------------------------------------------------------------------------
# NODE, GO, STATICCHECK, GCC AND THE PLAYWRIGHT BROWSERS
# 12 October 2026, Phase 1 web-app readiness (package W1).
#
# WHY THESE ARE IN THE FACTORY'S IMAGE. The coding Player and Coach, and the
# checks run after them, are children of the sandbox runner, which runs this
# image. Until now nothing here could run a project whose declared install
# and checks call node, npm, go or a browser: Debian's Node 18 had no npm and
# there was no Go at all. The tools are generic — nothing below names a
# project, and a project still says in its own declaration which commands it
# runs. Product images built by projects stay their own business.
#
# WHERE THEY ARE FOUND. The Player's and Coach's shell does not inherit this
# image's environment: GuardKitFactory's harness gives it PATH=/usr/bin:/bin,
# HOME=<worktree> and its own TMPDIR. So every executable a project's checks
# call is linked into /usr/bin, and no setting the shell needs is left to an
# image ENV. Each link is a plain `ln -s`, which refuses to replace a file
# that is already there, so no Debian binary is shadowed.
#
# HOW EACH DOWNLOAD IS TRUSTED. Node, Go and Staticcheck are official release
# archives, pinned by version and checked against the sha256 recorded here
# before anything is unpacked (the same rule as hurl, docker and code search
# above). Each of the three is unpacked with --no-same-owner: the Staticcheck
# archive's files belong to uid 1000 and Node's to uid 1001, and uid 1000 is
# the forge user, which must not own a program it runs.
#
# NO SHARED CACHE IS BAKED IN. GOPATH, GOCACHE and npm's cache are not set
# here. They resolve under the HOME each build is given, or wherever the
# project's own declaration puts them, so nothing in the image is a cache that
# two builds write to.
# ---------------------------------------------------------------------------

# Node 22 under /usr/local (node, npm, npx and corepack land in
# /usr/local/bin). The archive's three top-level text files are left out so
# they do not litter /usr/local.
ARG NODE_VERSION=22.23.2
ARG NODE_SHA256=fff4078c5def658577f92c88db7db3bc0072924bfb93fe52c1e744a54e94abb8
# npm 11, laid over the npm 10.9.8 that Node 22.23.2 ships (18 October 2026).
# npm 10.9.8 crashes with "Cannot read properties of null (reading
# 'edgesOut')" when it resolves Vitest 4.1 beside Vite 8, so a project could
# not upgrade Vitest in this image. 11.21.0 is the newest npm 11 release that
# was at least 14 days old on that day (published 30 September 2026); it
# supports Node ^20.17.0 || >=22.9.0. The registry tarball is pinned by the
# sha256 recorded here, which was checked against the registry's own sha512
# integrity record for npm 11.21.0. It carries all of npm's dependencies, so
# it is unpacked in place of the bundled copy and nothing runs to install it;
# the npm and npx links in /usr/local/bin already point into that folder.
ARG NODE_NPM_VERSION=11.21.0
ARG NODE_NPM_SHA256=783e7c92bf73b442fb800c2d6ef3921e86da8894a700fed45140e37916877482
RUN curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-arm64.tar.xz" \
        -o /tmp/node.tar.xz \
    && echo "${NODE_SHA256}  /tmp/node.tar.xz" | sha256sum --check --strict - \
    && tar --extract --xz --file /tmp/node.tar.xz --directory /usr/local \
        --strip-components=1 --no-same-owner \
        --exclude="node-v${NODE_VERSION}-linux-arm64/CHANGELOG.md" \
        --exclude="node-v${NODE_VERSION}-linux-arm64/LICENSE" \
        --exclude="node-v${NODE_VERSION}-linux-arm64/README.md" \
    && rm -f /tmp/node.tar.xz \
    && ln -s /usr/local/bin/node /usr/bin/node \
    && ln -s /usr/local/bin/npm /usr/bin/npm \
    && ln -s /usr/local/bin/npx /usr/bin/npx \
    && /usr/bin/node --version | grep -qx "v${NODE_VERSION}" \
    && curl -fsSL "https://registry.npmjs.org/npm/-/npm-${NODE_NPM_VERSION}.tgz" \
        -o /tmp/npm.tgz \
    && echo "${NODE_NPM_SHA256}  /tmp/npm.tgz" | sha256sum --check --strict - \
    && rm -rf /usr/local/lib/node_modules/npm \
    && mkdir /usr/local/lib/node_modules/npm \
    && tar --extract --gzip --file /tmp/npm.tgz --directory /usr/local/lib/node_modules/npm \
        --strip-components=1 --no-same-owner \
    && rm -f /tmp/npm.tgz \
    && test "$(/usr/bin/npm --version)" = "${NODE_NPM_VERSION}"

# Go 1.26 in /usr/local/go. `go` finds its root by resolving its own link, so
# the /usr/bin links are enough. GOTOOLCHAIN=local is written into Go's own
# installation defaults file, go.env, which Go reads even when the environment
# carries no Go settings — the Player's shell included — so by default a
# project's go.mod cannot make Go download a different toolchain. It is the
# image's DEFAULT, not a lock: a GOTOOLCHAIN environment variable, or a
# per-user $HOME/.config/go/env (written by `go env -w`), overrides it. The
# Player's HOME is its worktree, so a project closes that second route by
# declaring XDG_CONFIG_HOME as a launch scratch name, as the React/Go template
# does. gcc and libc6-dev are here because Go's race
# detector needs cgo on arm64; build-essential and g++ are not installed.
ARG GO_VERSION=1.26.9
ARG GO_SHA256=4a97373d49fcacdcf3694fea368a500b00ee3e963974f3e7514132717632f052
RUN apt-get update \
    && apt-get install --yes --no-install-recommends gcc libc6-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-arm64.tar.gz" \
        -o /tmp/go.tgz \
    && echo "${GO_SHA256}  /tmp/go.tgz" | sha256sum --check --strict - \
    && tar --extract --gzip --file /tmp/go.tgz --directory /usr/local --no-same-owner \
    && rm -f /tmp/go.tgz \
    && sed 's/^GOTOOLCHAIN=auto$/GOTOOLCHAIN=local/' /usr/local/go/go.env > /tmp/go.env \
    && mv /tmp/go.env /usr/local/go/go.env \
    && grep -qx 'GOTOOLCHAIN=local' /usr/local/go/go.env \
    && ln -s /usr/local/go/bin/go /usr/bin/go \
    && ln -s /usr/local/go/bin/gofmt /usr/bin/gofmt \
    && /usr/bin/go version | grep -q " go${GO_VERSION} linux/arm64$"

# Staticcheck, the one binary out of its release archive.
ARG STATICCHECK_VERSION=2026.2.1
ARG STATICCHECK_SHA256=594421f28ba620ea14b98377cb84a309cf73cc420ba0f52f30c7fa4d92fd2b0e
RUN curl -fsSL "https://github.com/dominikh/go-tools/releases/download/${STATICCHECK_VERSION}/staticcheck_linux_arm64.tar.gz" \
        -o /tmp/staticcheck.tgz \
    && echo "${STATICCHECK_SHA256}  /tmp/staticcheck.tgz" | sha256sum --check --strict - \
    && tar --extract --gzip --file /tmp/staticcheck.tgz --directory /usr/local/bin \
        --strip-components=1 --no-same-owner staticcheck/staticcheck \
    && rm -f /tmp/staticcheck.tgz \
    && chmod 0755 /usr/local/bin/staticcheck \
    && ln -s /usr/local/bin/staticcheck /usr/bin/staticcheck

# uv, the Python package and project manager (23 October 2026). A project that
# declares a uv workspace ([tool.uv.sources] in its pyproject.toml) needs uv on
# PATH: GuardKit's environment set-up refuses to install such a project with
# pip, and a build stopped at its second wave because this image had no uv. It
# is a toolchain like Node and Go: nothing here names a project, and a project
# still declares its own commands. uv and uvx are the two binaries in the
# official Linux ARM64 (glibc) release archive, pinned by version and checked
# against the sha256 recorded here, which matches the release's own published
# checksum. Like Go's and npm's caches, uv's cache and the Pythons it installs
# are not set here: they resolve under the HOME each build is given (or where
# the project's declaration puts them), so nothing in the image is shared
# between builds. A project that needs a Python other than this image's own
# gets it from uv, over whatever network its sandbox allows.
ARG UV_VERSION=0.12.24
ARG UV_SHA256=5231be65f496304623895dacdbf1de8504fec90303684bdf05805aa34414dd21
RUN curl -fsSL "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-aarch64-unknown-linux-gnu.tar.gz" \
        -o /tmp/uv.tgz \
    && echo "${UV_SHA256}  /tmp/uv.tgz" | sha256sum --check --strict - \
    && tar --extract --gzip --file /tmp/uv.tgz --directory /usr/local/bin \
        --strip-components=1 --no-same-owner \
        uv-aarch64-unknown-linux-gnu/uv uv-aarch64-unknown-linux-gnu/uvx \
    && rm -f /tmp/uv.tgz \
    && chmod 0755 /usr/local/bin/uv /usr/local/bin/uvx \
    && ln -s /usr/local/bin/uv /usr/bin/uv \
    && ln -s /usr/local/bin/uvx /usr/bin/uvx

# 29 October 2026 — the language servers behind GuardKitFactory's
# code_references tool ("who uses this, what does a change reach"; design:
# ai-transition docs/designs/find-references-tool-2026-10-28.md): gopls,
# pyright and typescript-language-server, at the versions the 28 October
# measurement used. They go in /opt/language-servers, which is on no PATH: the
# tool runs a server a project declares only from /opt/language-servers/bin,
# with its own fixed PATH (that folder, /usr/local/go/bin and /usr/local/bin).
# The planner's image (specialist-agent) copies this folder, /usr/local/go and
# /usr/local/bin/node from this image, so one set of pins serves both.
#
# gopls is built from source with the pinned Go above (GOTOOLCHAIN=local, no
# cgo, so the binary needs nothing from the system); `go install` checks every
# module it downloads against gopls's go.sum and Go's checksum database. Its
# home, module cache and build cache, npm's cache and node's compile cache are
# a temporary folder, removed here.
# pyright, typescript-language-server and the typescript it runs come from the
# npm registry at the exact versions in deploy/language-servers/package-lock.json,
# checked by npm against the integrity recorded there; none of the three has an
# install script, and --ignore-scripts keeps it that way. The two launchers run
# them with the image's node by absolute path. pyright-langserver has no
# --version, so the pyright package's own command answers for it.
ARG GOPLS_VERSION=0.23.0
ARG PYRIGHT_VERSION=1.1.414
ARG TYPESCRIPT_LANGUAGE_SERVER_VERSION=6.0.1
ARG TYPESCRIPT_VERSION=6.0.3
COPY deploy/language-servers/package.json deploy/language-servers/package-lock.json /opt/language-servers/node/
RUN build_home="$(mktemp -d)" \
    && export NODE_COMPILE_CACHE="${build_home}/node-compile-cache" \
    && mkdir -p /opt/language-servers/bin \
    && HOME="${build_home}" GOPATH="${build_home}/go" GOCACHE="${build_home}/go-build" \
       GOTOOLCHAIN=local CGO_ENABLED=0 GOBIN=/opt/language-servers/bin \
       /usr/local/go/bin/go install -trimpath "golang.org/x/tools/gopls@v${GOPLS_VERSION}" \
    && (cd /opt/language-servers/node \
        && npm_config_cache="${build_home}/npm" /usr/local/bin/npm ci --omit=dev --ignore-scripts \
           --no-audit --no-fund) \
    && printf '#!/bin/sh\nexec /usr/local/bin/node /opt/language-servers/node/node_modules/pyright/langserver.index.js "$@"\n' \
        > /opt/language-servers/bin/pyright-langserver \
    && printf '#!/bin/sh\nexec /usr/local/bin/node /opt/language-servers/node/node_modules/typescript-language-server/lib/cli.mjs "$@"\n' \
        > /opt/language-servers/bin/typescript-language-server \
    && chmod 0755 /opt/language-servers/bin/pyright-langserver /opt/language-servers/bin/typescript-language-server \
    && HOME="${build_home}" /opt/language-servers/bin/gopls version | grep -qx "golang.org/x/tools/gopls v${GOPLS_VERSION}" \
    && test "$(/usr/local/bin/node /opt/language-servers/node/node_modules/pyright/index.js --version)" = "pyright ${PYRIGHT_VERSION}" \
    && test "$(/opt/language-servers/bin/typescript-language-server --version)" = "${TYPESCRIPT_LANGUAGE_SERVER_VERSION}" \
    && test "$(/usr/local/bin/node /opt/language-servers/node/node_modules/typescript/bin/tsc --version)" = "Version ${TYPESCRIPT_VERSION}" \
    && rm -rf "${build_home}" \
    && chmod -R a+rX,go-w /opt/language-servers

# ---------------------------------------------------------------------------
# THE SECURITY SCANNERS (security check design, 17 October 2026, piece 2).
#
# WHY THEY ARE IN THE FACTORY'S IMAGE. The build runner in each project's
# sandbox runs this image, and GuardKit's per-change security check runs
# Gitleaks and OSV-Scanner there on every task. The coordinator runs this
# image too, and its nightly security sweep runs all three: OSV-Scanner for
# vulnerable dependencies, Gitleaks for secrets, Opengrep for risky code.
# Nothing here names a project or a language.
#
# HOW EACH DOWNLOAD IS TRUSTED. Each is one binary from a pinned release,
# checked against the sha256 recorded here before it is unpacked or
# installed, the same rule as Staticcheck above. The Gitleaks and OSV-Scanner
# values were checked against the projects' own published checksum files.
# Opengrep publishes none for this binary; its value is the one recorded in
# the design. Each lands in /usr/local/bin, root-owned (--no-same-owner /
# install -o root), with a plain link in /usr/bin where the Player's shell
# (PATH=/usr/bin:/bin) finds it.
#
# OPENGREP is the open-source fork of Semgrep, one binary, with no usage
# reporting; it replaces Semgrep, which publishes no current Python package
# for arm64. 1.30.1 was chosen for being a month old on 17 October 2026.
#
# THE FACTORY'S OWN SCANNER SETTINGS, in /opt/factory-security (Codex R8, R9):
# an empty osv-scanner.toml every OSV-Scanner run is given (it overrides any
# osv-scanner.toml in the scanned tree), an empty folder Gitleaks looks in for
# an ignore file, and the factory's six Compose rules
# (deploy/security/compose-rules.yaml). The project's own scanner settings are
# never read. The Opengrep security rules come from the stage above, with the
# sha256 record the self-check checks.
# ---------------------------------------------------------------------------
ARG GITLEAKS_VERSION=8.30.1
ARG GITLEAKS_SHA256=e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080
ARG OSV_SCANNER_VERSION=2.7.0
ARG OSV_SCANNER_SHA256=4061ef4c021ed3c47f203b818f337e351336cc5cd67c0274fc36bd1dfc728dd7
ARG OPENGREP_VERSION=1.30.1
ARG OPENGREP_SHA256=a730f6fdce1e978ea29e610a2e0503bfe1ec31fce08979a9a84e3699f03e16ee
RUN curl -fsSL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_arm64.tar.gz" \
        -o /tmp/gitleaks.tgz \
    && echo "${GITLEAKS_SHA256}  /tmp/gitleaks.tgz" | sha256sum --check --strict - \
    && tar --extract --gzip --file /tmp/gitleaks.tgz --directory /usr/local/bin \
        --no-same-owner gitleaks \
    && rm -f /tmp/gitleaks.tgz \
    && chmod 0755 /usr/local/bin/gitleaks \
    && ln -s /usr/local/bin/gitleaks /usr/bin/gitleaks \
    && curl -fsSL "https://github.com/google/osv-scanner/releases/download/v${OSV_SCANNER_VERSION}/osv-scanner_linux_arm64" \
        -o /tmp/osv-scanner \
    && echo "${OSV_SCANNER_SHA256}  /tmp/osv-scanner" | sha256sum --check --strict - \
    && install -m 0755 -o root -g root /tmp/osv-scanner /usr/local/bin/osv-scanner \
    && rm -f /tmp/osv-scanner \
    && ln -s /usr/local/bin/osv-scanner /usr/bin/osv-scanner \
    && curl -fsSL "https://github.com/opengrep/opengrep/releases/download/v${OPENGREP_VERSION}/opengrep_manylinux_aarch64" \
        -o /tmp/opengrep \
    && echo "${OPENGREP_SHA256}  /tmp/opengrep" | sha256sum --check --strict - \
    && install -m 0755 -o root -g root /tmp/opengrep /usr/local/bin/opengrep \
    && rm -f /tmp/opengrep \
    && ln -s /usr/local/bin/opengrep /usr/bin/opengrep
COPY deploy/security/compose-rules.yaml /opt/factory-security/compose-rules.yaml
COPY --from=opengrep-rules /out/security-rules.yaml /out/security-rules.yaml.sha256 /opt/opengrep/
RUN mkdir -p /opt/factory-security/gitleaks-ignore \
    && touch /opt/factory-security/osv-scanner.toml \
    && test ! -s /opt/factory-security/osv-scanner.toml \
    && chmod -R a+rX,go-w /opt/factory-security /opt/opengrep

# The Playwright 1.63 browsers (Chromium, Firefox, WebKit) and the Debian
# libraries they need, in /opt/ms-playwright, readable by every user and
# writable by none but root. A project locks @playwright/test 1.63.0 to use
# them, and its builds never download browsers. PLAYWRIGHT_BROWSERS_PATH is set
# for the runner's environment; the harness hands it to a project's shell only
# when the project declares it.
#
# TRUSTED DIFFERENTLY FROM THE ARCHIVES ABOVE, and said plainly: `npx` fetches
# the playwright package from the npm registry at image build time (pinned by
# version, checked by npm against the registry's own integrity record), and
# that package downloads the browser builds its version names from
# Playwright's own download host. No sha256 is recorded here for either. Like
# every download in this file this needs network at image build time only;
# nothing is fetched when the image runs. npm's and Playwright's working files
# from the install are removed in the same layer.
ARG PLAYWRIGHT_VERSION=1.63.0
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN npx -y "playwright@${PLAYWRIGHT_VERSION}" install --with-deps chromium firefox webkit \
    && npx -y "playwright@${PLAYWRIGHT_VERSION}" --version | grep -qx "Version ${PLAYWRIGHT_VERSION}" \
    && chmod -R a+rX,go-w "${PLAYWRIGHT_BROWSERS_PATH}" \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /root/.npm /root/.cache

# THE TOOLCHAIN SELF-CHECK. Every check runs as uid 1000 (the forge user; it
# is created further down, so setpriv takes the number) in the Player's own
# minimal environment — `env -i PATH=/usr/bin:/bin HOME=<fresh folder>` — so it
# proves what the Player and Coach will actually find, not what root with this
# image's PATH finds. Exact versions; cgo on, proved by building with the race
# detector; gofmt finding nothing to change in a well-formatted file;
# GOTOOLCHAIN=local from go.env alone; and the two Chromium binaries
# Playwright 1.63 installs on linux-arm64. The headless shell
# (chromium_headless_shell-<rev>/chrome-headless-shell-linux-arm64/
# chrome-headless-shell), which Playwright's default headless mode runs, must
# load a page; --no-sandbox is what Playwright itself passes by default. The
# full browser (chromium-<rev>/chrome-linux-arm64/chrome) must only answer
# --version: on 13 October 2026 its --dump-dom run in the first real build
# never exited (D-Bus and GCM noise) and held the build for over two hours.
# Every browser run here is therefore under `timeout --kill-after`, writes to
# a file rather than a pipe (a pipe would wait on any browser child process
# still holding it), and prints what the browser said if it fails, so a hang
# fails the build within about a minute. Firefox and WebKit are checked for presence and
# readability only: they have no one-line headless launch outside Playwright's
# own driver, so launching all three browsers is the acceptance smoke run in
# the built image, not this step. Since 17 October 2026 it also checks the
# three security scanners' exact versions, the Opengrep rule file against its
# recorded sha256, and that Opengrep, run as the Player under a time limit,
# loads both rule files and finds the factory's privileged-service rule in a
# small Compose file and the community secret-in-build-arg rule in a two-line
# Dockerfile (the second proves the rule about handling secrets was kept).
# Since 23 October 2026 it also checks uv's and uvx's exact versions.
RUN check_home="$(mktemp -d)" \
    && chown 1000:1000 "${check_home}" \
    && as_player="setpriv --reuid=1000 --regid=1000 --clear-groups env -i PATH=/usr/bin:/bin HOME=${check_home}" \
    && test "$(${as_player} node --version)" = "v${NODE_VERSION}" \
    && test "$(${as_player} npm --version)" = "${NODE_NPM_VERSION}" \
    && test "$(${as_player} npx --version)" = "${NODE_NPM_VERSION}" \
    && ${as_player} go version | grep -q " go${GO_VERSION} linux/arm64$" \
    && test "$(${as_player} go env GOTOOLCHAIN)" = "local" \
    && test "$(${as_player} go env CGO_ENABLED)" = "1" \
    && printf 'package main\n\nfunc main() {}\n' > "${check_home}/race.go" \
    && ${as_player} go run -race "${check_home}/race.go" \
    && gofmt_out="$(${as_player} gofmt -l "${check_home}/race.go")" \
    && test -z "${gofmt_out}" \
    && test "$(${as_player} staticcheck -version)" = "staticcheck ${STATICCHECK_VERSION} (0.8.1)" \
    && test "$(${as_player} uv --version)" = "uv ${UV_VERSION} (aarch64-unknown-linux-gnu)" \
    && test "$(${as_player} uvx --version)" = "uvx ${UV_VERSION} (aarch64-unknown-linux-gnu)" \
    && test "$(${as_player} gitleaks version)" = "${GITLEAKS_VERSION}" \
    && ${as_player} osv-scanner --version | grep -qx "osv-scanner version: ${OSV_SCANNER_VERSION}" \
    && test "$(${as_player} opengrep --version)" = "${OPENGREP_VERSION}" \
    && (cd /opt/opengrep && sha256sum --check --strict security-rules.yaml.sha256) \
    && printf 'services:\n  app:\n    image: x\n    privileged: true\n' > "${check_home}/compose.yaml" \
    && printf 'FROM scratch\nARG API_TOKEN\n' > "${check_home}/Dockerfile" \
    && { timeout --kill-after=5 120 ${as_player} opengrep scan --config /opt/opengrep/security-rules.yaml \
             --config /opt/factory-security/compose-rules.yaml --disable-version-check --json --quiet \
             "${check_home}/compose.yaml" "${check_home}/Dockerfile" \
             < /dev/null > "${check_home}/opengrep.json" 2> "${check_home}/opengrep.log" \
         && grep -q 'factory-compose-privileged' "${check_home}/opengrep.json" \
         && grep -q 'secret-in-build-arg' "${check_home}/opengrep.json" \
         || { cat "${check_home}/opengrep.log" >&2; exit 1; }; } \
    && chrome="$(find "${PLAYWRIGHT_BROWSERS_PATH}" -type f -path '*/chromium-*/chrome-linux-arm64/chrome' -name chrome)" \
    && headless_shell="$(find "${PLAYWRIGHT_BROWSERS_PATH}" -type f -path '*/chromium_headless_shell-*/chrome-headless-shell-linux-arm64/chrome-headless-shell' -name chrome-headless-shell)" \
    && test -x "${chrome}" && test -x "${headless_shell}" \
    && { timeout --kill-after=5 30 ${as_player} "${chrome}" --version \
             < /dev/null > "${check_home}/chrome-version.txt" 2>&1 \
         && grep -Eq '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' "${check_home}/chrome-version.txt" \
         || { cat "${check_home}/chrome-version.txt" >&2; exit 1; }; } \
    && { timeout --kill-after=5 60 ${as_player} "${headless_shell}" --no-sandbox --disable-gpu \
             --disable-dev-shm-usage --user-data-dir="${check_home}/profile" --dump-dom about:blank \
             < /dev/null > "${check_home}/dom.html" 2> "${check_home}/headless-shell.log" \
         && grep -q '<body>' "${check_home}/dom.html" \
         || { cat "${check_home}/dom.html" "${check_home}/headless-shell.log" >&2; exit 1; }; } \
    && ${as_player} ls -d "${PLAYWRIGHT_BROWSERS_PATH}"/firefox-* "${PLAYWRIGHT_BROWSERS_PATH}"/webkit-* \
    && rm -rf "${check_home}"

# 2026-08-15 — HISTORICAL REASON, KEPT AS AN INERT SETTING. guardkit deleted
# the DCL spec track outright (guardkit b138d92c) and forge's W1-S2 leg went
# with it, so nothing shells the vendored checker any more. On 12 October 2026
# Debian's Node 18 left this image for the pinned Node 22 above, where the
# webcrypto global is on by default (from Node 19) and this flag is accepted
# and changes nothing. It is kept, as the Phase 1 design records, rather than
# removed as a side effect of the toolchain change. It reaches only processes
# started with this image's own environment; the Player's shell does not
# inherit it.
#
# The original reason, kept for the record: guardkit's vendored DCL checker
# (qa/dcl/bin/dcl_check.mjs, WASM) needs a node runtime — the ``guardkit dcl
# author``/oracle legs shelled it in-container (first live-caught 2026-07-18,
# run s3dcl-6e6bdabea57c: exit-2 instrument error, node absent). Bookworm ships
# node 18, whose Go wasm_exec requires the webcrypto global behind a flag
# (global by default only from node 19).
ENV NODE_OPTIONS=--experimental-global-webcrypto

# 19 October 2026 — EVERY GuardKit PROCESS IN THIS IMAGE IS A FACTORY LAUNCH.
# GuardKit's frontier-model fence, and its rule that a factory build uses only
# the memory name it is handed, both apply when GUARDKIT_FACTORY_LAUNCH is set
# to anything non-empty. Forge's own launches set it every time
# (forge.launch_environment.build_launch_env); this sets it for the whole
# image as well, so a GuardKit started inside a factory container by a path
# that forgot is still fenced. It is set after the self-check above, which runs
# in the Player's minimal environment and does not need it. A project's own
# programs never see it: their environment is built from nothing, and every
# GUARDKIT_ name is the factory's own. A person running GuardKit by hand in a
# factory container, as a person and not as the factory, clears it for that
# one command: `env -u GUARDKIT_FACTORY_LAUNCH guardkit ...`.
ENV GUARDKIT_FACTORY_LAUNCH=1

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
# a build of a different commit. They sit after the apt, download and toolchain
# layers on purpose, so those stay cached and a rebuild does not re-download
# anything.
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

# 12 October 2026 — the code search programs, run by GuardKitFactory's own
# runner inside this exact image: every operation on a fresh neutral fixture,
# including a missing path, an unsupported language and an invalid pattern.
# A program that cannot run on this base fails the build here.
RUN python -m guardkitfactory.code_search self-check

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

# The sandbox starter (19 October 2026, one sandbox per project, stage S2). The
# start command of deploy/compose/compose.sandbox-starter.yaml: a standard-
# library program that starts and stops the project sandboxes' supervisor
# containers. A leaf, like the supervisor script above.
COPY deploy/compose/sandbox-starter/starter.py /opt/forge/sandbox-starter/starter.py
# Read, never executed directly: compose runs it as the machine's user with
# `python -I`, so it must be readable by that user (COPY leaves it root 0600).
RUN chmod 0644 /opt/forge/sandbox-starter/starter.py

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

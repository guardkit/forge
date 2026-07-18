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
#   * nats-core is installed from /tmp/nats-core BEFORE
#     ``pip install .[providers]`` so pip's resolver treats nats-core
#     as already-satisfied and never reaches PyPI for the malformed
#     0.2.0 wheel (TASK-FIX-F0E6 / TASK-REV-F0E4 §5.1).
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

# Install nats-core from the BuildKit context BEFORE forge so pip's
# resolver treats nats-core>=0.3.0,<0.6 (declared in pyproject.toml)
# as already-satisfied. Without this step, pip would attempt to fetch
# the malformed PyPI 0.2.0 wheel (TASK-FIX-F0E6) and the install would
# fail with ``ModuleNotFoundError: No module named 'nats_core'`` at
# import time. We deliberately use a non-editable install here so the
# resulting venv contains nats-core as a regular distribution rather
# than a ``.pth`` editable pointer to a path that won't exist in the
# runtime stage.
RUN pip install /tmp/nats-core

# Copy the forge sources late so changes to forge code don't bust the
# nats-core install cache layer above. ``pyproject.toml`` is NOT
# mutated — scoping §11.4 mandates preserving dev-host editable
# semantics, and pip already considers nats-core satisfied above.
COPY pyproject.toml ./
COPY README.md ./
COPY src ./src

# Literal-match to RUNBOOK-FEAT-FORGE-008-validation.md §0.4 and §6.1
# (LES1 §3 DKRX, AC-E / B3 scenario). The runbook validation steps and
# this Dockerfile share this exact install command — drift here breaks
# the equivalence claim that FEAT-FORGE-008 relies on. forge installs
# as a regular distribution (no ``-e``); the C4 scenario asserts the
# runtime venv contains forge with no ``.pth`` editable pointer.
RUN pip install .[providers]

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

# Real pip install of the guardkit-py distribution (python floor >=3.12 is
# satisfied). Builds the DF-011 wheel from the staged tree — packages=["guardkit"]
# plus the ``installer/core`` -> ``guardkit/_installer_core`` force-include — and
# installs the ``guardkit-py`` console script into /opt/venv/bin.
RUN pip install /tmp/guardkit \
    && python -c "import guardkit, guardkit._installer_core; print('guardkit installed at', guardkit.__file__)"

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
# console-script entry produced by ``pip install .[providers]`` rather
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

# guardkit's vendored DCL checker (qa/dcl/bin/dcl_check.mjs, WASM) needs a
# node runtime — the ``guardkit dcl author``/oracle legs shell it in-container
# (first live-caught 2026-07-18, run s3dcl-6e6bdabea57c: exit-2 instrument
# error, node absent). Bookworm ships node 18, whose Go wasm_exec requires
# the webcrypto global behind a flag (global by default only from node 19).
ENV NODE_OPTIONS=--experimental-global-webcrypto

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

# Create the unprivileged runtime user *before* WORKDIR/COPY-into-home
# so any files copied later inherit the correct ownership when --chown
# is used. UID 1000 is mandated by AC-C and the ``id -u`` runtime
# assertion. ``useradd`` is kept on a single line so static-analysis
# tools that scan the Dockerfile per-instruction (and the digest-pinning
# test in tests/dockerfile/) can match the UID-1000 assertion without
# needing to span backslash-continued lines.
RUN groupadd --system --gid 1000 forge \
    && useradd --system --uid 1000 --gid 1000 --home-dir /home/forge --create-home --shell /usr/sbin/nologin forge

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
    CMD curl -fs http://localhost:8080/healthz || exit 1

# Exec form is required: shell form would route through /bin/sh and
# break signal forwarding to the Python daemon (SIGTERM-on-stop must
# reach forge serve cleanly so JetStream consumer drains gracefully).
ENTRYPOINT ["forge"]
CMD ["serve"]

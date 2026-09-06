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

# Install fleet-memory from the BuildKit context BEFORE forge so pip's
# resolver treats fleet-memory>=0.1,<1 (the ``memory`` extra) as
# already-satisfied and never reaches PyPI, where no fleet-memory
# distribution exists. Non-editable for the same reason as nats-core:
# the runtime stage must not depend on a ``.pth`` pointer to a builder
# path.
RUN pip install /tmp/fleet-memory

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
RUN pip install .[providers,memory]

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

# INSTALL ORDER — deliberately AFTER ``pip install .[providers,memory]``,
# unlike nats-core / fleet-memory which install BEFORE it. Two facts decide
# it, and both were checked against the pyprojects rather than assumed:
#
#   1. Dependency direction. guardkitfactory declares nothing from this
#      estate in its DEPENDENCIES (deepagents / langgraph / langchain /
#      langchain-core / langchain-openai / tree-sitter, all on PyPI), and
#      guardkit is the one that declares IT —
#      ``guardkit-py[autobuild]`` -> ``guardkitfactory>=0.2.0,<1``, an extra
#      this image never installs, so nothing forge installs can reach PyPI
#      looking for a guardkitfactory distribution (there is none to find).
#      forge itself never names it.
#
#      DO NOT read that as "guardkitfactory is independent of guardkit". Its
#      runtime IMPORT graph runs the other way:
#      guardkitfactory/harness/langgraph_harness.py does a module-level
#      ``from guardkit.orchestrator.harness import ...`` and
#      ``import guardkitfactory`` reaches it eagerly via ``__init__`` ->
#      ``.harness``. The two are mutually importing. That is harmless here
#      only because installation never imports, and both are present in the
#      final venv before anything imports either (the oracles run
#      post-build) — but by import direction guardkitfactory would more
#      properly FOLLOW the guardkit block, not precede it. Placement here is
#      the deepagents decision below, nothing more.
#
#   2. The deepagents band decides the rest, and it is PINNED here on
#      purpose. forge pins ``deepagents>=0.5.3,<0.6``; guardkitfactory
#      requires ``deepagents>=0.6.7,<1``. That pair is UNSATISFIABLE in one
#      venv, and pip's sequential installs make the LAST install the winner.
#      Installing guardkitfactory first would let forge's install DOWNGRADE
#      deepagents to 0.5.x, where ``create_deep_agent`` has no
#      ``state_schema`` keyword (added upstream in 0.6.6 and passed by
#      guardkitfactory's harness). ``import guardkitfactory`` would still
#      succeed and the leg would then die at call time — the false-green
#      class this bake exists to kill. So guardkitfactory installs LAST.
#
#      But LAST alone is not enough, and the bare install is a TRAP: pip
#      resolves ``deepagents>=0.6.7,<1`` to the NEWEST match on PyPI, and
#      that band today runs 0.6.7…0.6.12 then 0.7.0…0.7.3 — so a bare
#      ``pip install /tmp/guardkitfactory`` lands 0.7.3, NOT the 0.6.7 this
#      estate is developed against (guardkitfactory's own .venv carries
#      0.6.7 and its floor comment stops there). NOTE the ``<0.7`` pin
#      below bakes the NEWEST 0.6.x — today that is 0.6.12, not 0.6.7;
#      the reviewed band is what the pin holds, and the oracle's
#      protocol-prompt probe (not this comment) is what makes any 0.6.x
#      safe. 0.7.x is a SILENT
#      regression for the daemon on two counts:
#
#        (a) deepagents 0.7.x DELETED the module constant
#            ``ASYNC_TASK_SYSTEM_PROMPT`` and changed
#            ``AsyncSubAgentMiddleware.__init__``'s ``system_prompt``
#            default from that constant to ``None``.
#            src/forge/cli/serve.py constructs
#            ``AsyncSubAgentMiddleware(async_subagents=[spec])`` with no
#            ``system_prompt``, so under 0.7.x the supervisor silently loses
#            the entire async-subagent operating protocol (start / check /
#            update / cancel / list rules plus the stale-status rules) it
#            carries today. NOTHING RAISES — the leg just gets a dumber
#            supervisor.
#
#        (b) 0.7.3 cascade-upgrades the daemon's whole LLM stack unreviewed:
#            langchain>=1.3.14, langchain-core>=1.5.0,
#            langchain-anthropic>=1.5.3, langchain-google-genai>=4.3.1,
#            langsmith>=0.10.9. All sit inside forge's declared ranges so
#            pip refuses nothing — but forge's SSE contract fixtures
#            (tests/forge/lifecycle_bridge/test_translation_contract.py)
#            were recorded against the current set, and forge pins
#            langgraph-sdk~=0.3.13 / langgraph-api~=0.8.0 around them.
#
#      The ``state_schema`` oracle CANNOT catch either: the keyword exists
#      in 0.6.7 and 0.7.3 alike — it is a FLOOR probe, not a version probe.
#      Hence the explicit ``deepagents>=0.6.7,<0.7`` on the install line
#      below, and the version-band assertion in the oracle. Widening that
#      band is a deliberate act: re-check serve.py's middleware construction
#      and the SSE contract fixtures first.
#
#      Why the daemon tolerates the newer deepagents at all: forge's ENTIRE
#      deepagents surface is one import —
#      ``deepagents.middleware.async_subagents.AsyncSubAgentMiddleware``
#      (src/forge/cli/serve.py) — and that module is identical between
#      0.5.9 and 0.6.7 apart from prompt-text markdown formatting.
#      pip WILL print a ``forge 0.1.0 requires deepagents<0.6`` conflict
#      line at this layer: expected, and loud by design. Reconciling forge's
#      DECLARED pin with the harness floor is a pyproject ruling, not an
#      image change — raise it before the next pin edit.
#
# scripts/verify-forge-oracles.sh proves the result inside the built image:
# ``import guardkitfactory`` (which eagerly imports guardkitfactory.harness,
# hence the whole deepagents/langchain/langgraph stack), the resolved
# deepagents version band, the ``state_schema`` capability itself, and
# ``guardkit task-review --help``.
RUN pip install /tmp/guardkitfactory 'deepagents>=0.6.7,<0.7'

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
    CMD curl -fs http://localhost:${FORGE_HEALTHZ_PORT:-8080}/healthz || exit 1

# Exec form is required: shell form would route through /bin/sh and
# break signal forwarding to the Python daemon (SIGTERM-on-stop must
# reach forge serve cleanly so JetStream consumer drains gracefully).
ENTRYPOINT ["forge"]
CMD ["serve"]

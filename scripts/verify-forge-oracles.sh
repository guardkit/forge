#!/usr/bin/env bash
# Prove the forge target-terminal oracles resolve INSIDE a built image.
#
# Regression guard for the B4 run 4b3b0893 (round 5) incident: the forge image
# shipped no guardkit, so the target-terminal pre-commit oracles — guardkit code
# the image never installed — failed in-container. The normalizer leg died with
# ``ModuleNotFoundError: No module named 'installer'`` AFTER the reply had been
# projected and the branch written, and the ``guardkit feature validate`` plan
# leg would have hit the same wall (its binary did not exist either).
#
# This is the forge-side mirror of specialist-agent's verify-template-payload.sh.
# It proves, all from inside the built image, that BOTH oracle seams work:
#   (i)   the normalizer module resolves and runs (exit 0 on a trivial fixture)
#         at the module path forge's resolver prefers in-container
#         (guardkit._installer_core.commands.lib.feature_spec_normalize);
#   (ii)  the guardkit CLI binary answers at /usr/local/bin/guardkit — the
#         absolute path the frozen forge.adapters.guardkit.run boundary shells
#         (``guardkit feature validate --help``);
#   (iii) forge's own resolver (resolve_normalizer_command) picks an importable
#         candidate rather than raising NormalizerModuleUnresolved.
#   (iv)  the LangGraph leg harness is real: ``import guardkitfactory`` (which
#         eagerly imports guardkitfactory.harness, hence the whole
#         deepagents/langchain/langgraph stack), the deepagents that actually
#         landed is exactly 0.7.14, Forge's constructed middleware carries its
#         application-owned async protocol and five tools, a prompt-less
#         middleware is rejected, AND the ``guardkit task-review`` CLI leg
#         answers. Added after the conductor's first real leg died
#         in-container with ``GUARDKIT_HARNESS=langgraph but guardkitfactory is
#         not importable`` — the image baked guardkit but not its harness
#         runtime, and nothing at build time noticed.
#   (v)   the fix-task PRODUCER imports, under the leg's REAL binding order:
#         bind guardkitfactory's modules first (as select_harness does), then
#         require ``guardkit.orchestrator.review_runner._import_producer()`` to
#         hand back a callable. Added after the lib-shadow bake: clauses (i)-(iv)
#         probe imports / the deepagents band / the protocol prompt / the CLI —
#         NONE of them imports the producer, so a top-level ``lib`` distribution
#         package shipped by guardkitfactory shadowed the producer's
#         ``from lib.review_parser import …`` and baked undetected. Exactly ONE
#         receipt in the estate records ``producer.called: true`` and it died at
#         that import.
# Every future forge build proves its oracles before it can ship.
#
# The Python program is passed via ``python -c`` (NOT a stdin heredoc): a heredoc
# into ``docker run … python -`` silently reads empty input unless ``-i`` is
# attached and exits 0 — a false pass. ``-c`` has no such dependency.
#
# ---------------------------------------------------------------------------
# BEFORE ANY OF THAT: does the image carry THIS tree's code?
#
# LIVE INCIDENT (2026-09-11, the go-live of forge a24a825 + 2ad935f). A build
# from a clean checkout printed every source step as executed rather than
# cached, rebuilt the forge wheel, and this script then printed "forge oracle
# verification PASSED". The image was carrying the PREVIOUS commit's code: the
# installed runner module was 4,907 lines against 5,061 in the tree it was
# built from, and neither new function was in it. A passing verification had
# proved the oracles resolved and had proved NOTHING about the code.
#
# So the first thing this script now does is compare, by content, the forge
# package installed in the image against the forge package in the tree this
# script was invoked from, and refuse the image if they differ in any file. It
# also reads the image's provenance stamp and requires it to name this tree's
# commit. An image with no stamp predates the guard and FAILS with that said
# plainly — it is never waved through. If the comparison cannot be made at all
# (no git, no docker, the image will not run, no installed package), this
# script FAILS: unknown is not a pass.
#
# Usage:
#   ./scripts/verify-forge-oracles.sh [image-tag]   # default: forge:production-validation
#
# Two further modes exist so the comparison logic itself can be tested without
# docker (tests/forge/test_image_provenance.py uses both):
#   ./scripts/verify-forge-oracles.sh --manifest-dir <dir>
#       print one line per Python file under <dir>: "<relative path><TAB><sha256>"
#   ./scripts/verify-forge-oracles.sh --compare <manifest-a> <manifest-b>
#       compare two such manifests; exit 0 if identical, 1 with the differing,
#       missing and extra paths named otherwise.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

TAB="$(printf '\t')"

# Set by compare_manifests so the success sentence can report how many files
# were actually compared.
COMPARED_COUNT=0

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

# --- the file walker, run on BOTH sides of the comparison --------------------
# Deliberately POSIX sh so the SAME text runs under the host's bash and under
# the image's /bin/sh. With no argument it locates the installed forge package
# itself; with one argument it walks that directory. __pycache__ directories,
# .pyc files and *.dist-info directories are ignored — none of them is source.
# Output is sha256sum's own format ("<hash>  <path>"), normalised below.
# (A source file whose name contained a tab or a newline would defeat this;
# no Python package has one.)
MANIFEST_SH="$(cat <<'SH'
set -eu
ROOT="${1:-}"
if [ -z "${ROOT}" ]; then
    ROOT="$(python -c 'import importlib.util as u
spec = u.find_spec("forge")
locations = list(getattr(spec, "submodule_search_locations", None) or []) if spec else []
if not locations:
    raise SystemExit("the forge package is not installed here")
print(locations[0])')"
fi
if [ ! -d "${ROOT}" ]; then
    echo "not a directory: ${ROOT}" >&2
    exit 3
fi
cd "${ROOT}"
find . -type d -name '__pycache__' -prune -o \
       -type d -name '*.dist-info' -prune -o \
       -type f -name '*.py' -print0 \
    | xargs -0 -r sha256sum
SH
)"

# Turn sha256sum's "<hash>  ./<path>" into "<path><TAB><hash>", which is what
# the comparison joins on.
normalise_manifest() {
    sed -E "s|^([0-9a-f]{64})  (\./)?(.*)\$|\3${TAB}\1|"
}

# --- the comparison itself ---------------------------------------------------
# Takes two manifests and reports every path that differs, is missing from the
# image, or is present in the image and not in the tree. Returns 0 only when
# all three lists are empty.
compare_manifests() {
    local tree_manifest="$1" image_manifest="$2"
    local out="${WORK}/compare.$$"
    rm -rf "${out}"
    mkdir -p "${out}"

    # Every step below is checked by hand rather than left to ``set -e``:
    # this function is called from an ``if``, and bash suspends errexit
    # inside a condition, so a failure here would otherwise read as a pass.
    [ -f "${tree_manifest}" ] \
        || fail "there is no manifest at ${tree_manifest}, so the comparison cannot be made. Unknown is not a pass."
    [ -f "${image_manifest}" ] \
        || fail "there is no manifest at ${image_manifest}, so the comparison cannot be made. Unknown is not a pass."

    LC_ALL=C sort -t "${TAB}" -k1,1 "${tree_manifest}" > "${out}/tree" \
        || fail "${tree_manifest} could not be read, so the comparison cannot be made."
    LC_ALL=C sort -t "${TAB}" -k1,1 "${image_manifest}" > "${out}/image" \
        || fail "${image_manifest} could not be read, so the comparison cannot be made."

    LC_ALL=C join -t "${TAB}" -j 1 -o '0,1.2,2.2' "${out}/tree" "${out}/image" > "${out}/common" \
        || fail "the two manifests could not be compared."
    awk -F"${TAB}" '$2 != $3 { print $1 }' "${out}/common" > "${out}/differs" \
        || fail "the two manifests could not be compared."
    LC_ALL=C join -t "${TAB}" -j 1 -v 1 -o '1.1' "${out}/tree" "${out}/image" > "${out}/missing" \
        || fail "the two manifests could not be compared."
    LC_ALL=C join -t "${TAB}" -j 1 -v 2 -o '2.1' "${out}/tree" "${out}/image" > "${out}/extra" \
        || fail "the two manifests could not be compared."

    COMPARED_COUNT="$(wc -l < "${out}/tree" | tr -d ' ')"
    local n_differs n_missing n_extra
    n_differs="$(wc -l < "${out}/differs" | tr -d ' ')"
    n_missing="$(wc -l < "${out}/missing" | tr -d ' ')"
    n_extra="$(wc -l < "${out}/extra" | tr -d ' ')"

    if [ "$((n_differs + n_missing + n_extra))" -eq 0 ]; then
        return 0
    fi

    {
        echo "FAIL: the forge package in the image is not the forge package in this tree."
        echo "      files compared: ${COMPARED_COUNT}"
        echo "      contents differ: ${n_differs}; in the tree but not in the image: ${n_missing}; in the image but not in the tree: ${n_extra}"
        if [ "${n_differs}" -gt 0 ]; then
            echo "      files whose contents differ (first ten):"
            head -n 10 "${out}/differs" | sed 's|^|        |'
        fi
        if [ "${n_missing}" -gt 0 ]; then
            echo "      files in the tree that the image does not have (first ten):"
            head -n 10 "${out}/missing" | sed 's|^|        |'
        fi
        if [ "${n_extra}" -gt 0 ]; then
            echo "      files the image has that git does not track (first ten):"
            head -n 10 "${out}/extra" | sed 's|^|        |'
        fi
        # What to do next depends on WHICH of the three lists is non-empty, and
        # telling the operator the wrong one sends them round a loop: building
        # again cannot cure a file that has never been committed.
        if [ "$((n_differs + n_missing))" -eq 0 ]; then
            echo "      Every file the image and this tree share matches, and nothing is missing."
            echo "      The only difference is the file or files listed above, which the image has"
            echo "      and git does not track. That is almost always a new source file that has not"
            echo "      been committed yet: the build copied it in, git does not list it, so there is"
            echo "      nothing here to compare it against. Building again will fail in the same way."
            echo "      Commit the file — or delete it, if it was not meant to be there — and build again."
        else
            echo "      This is the 2026-09-11 failure: an image can carry another commit's code while"
            echo "      every build step reports as executed. Do not deploy this image; build it again."
        fi
    } >&2
    return 1
}

# --- the tree side -----------------------------------------------------------
# The repository's own statement of what belongs to it: tracked Python files
# under src/forge, hashed from the working tree, keyed by their path inside the
# package so they line up with the installed package's own layout.
tree_manifest() {
    local path absolute
    while IFS= read -r path; do
        case "${path}" in
            *.py) ;;
            *) continue ;;
        esac
        case "${path}" in
            */__pycache__/*) continue ;;
        esac
        absolute="${REPO_ROOT}/${path}"
        if [ ! -f "${absolute}" ]; then
            fail "the tree lists ${path} as tracked but the file is not on disk, so the comparison cannot be made."
        fi
        printf '%s\t%s\n' "${path#src/forge/}" "$(sha256sum "${absolute}" | cut -c1-64)"
    done < <(git -C "${REPO_ROOT}" ls-files -- src/forge)
}

# --- the whole provenance check, run before any oracle ----------------------
check_image_provenance() {
    local image="$1"

    echo "Checking ${image} carries the code in ${REPO_ROOT}"

    command -v git >/dev/null 2>&1 \
        || fail "git is not on PATH, so the code in the image cannot be compared with the code in this tree. Unknown is not a pass."
    command -v docker >/dev/null 2>&1 \
        || fail "docker is not on PATH, so the image cannot be opened and the comparison cannot be made. Unknown is not a pass."
    git -C "${REPO_ROOT}" rev-parse --git-dir >/dev/null 2>&1 \
        || fail "${REPO_ROOT} is not a git checkout, so there is no tree to compare the image against. Unknown is not a pass."

    local tree_sha
    tree_sha="$(git -C "${REPO_ROOT}" rev-parse HEAD)"

    if ! docker run --rm --entrypoint cat "${image}" /etc/forge-image-provenance \
            > "${WORK}/stamp" 2> "${WORK}/stamp.err"; then
        fail "${image} has no provenance stamp at /etc/forge-image-provenance. Either it was built before this guard existed — build it again with scripts/build-image.sh — or it cannot be run at all: $(tr '\n' ' ' < "${WORK}/stamp.err")"
    fi

    local image_sha image_dirty
    image_sha="$(sed -n 's/^commit=//p' "${WORK}/stamp")"
    image_dirty="$(sed -n 's/^dirty=//p' "${WORK}/stamp")"
    [ -n "${image_sha}" ] \
        || fail "the provenance stamp in ${image} names no commit, so the image cannot say which tree it came from."
    [ "${image_sha}" = "${tree_sha}" ] \
        || fail "${image} was built from commit ${image_sha}, and this tree is at commit ${tree_sha}. Build the image again from the tree you mean to deploy."

    tree_manifest > "${WORK}/tree.tsv"
    [ -s "${WORK}/tree.tsv" ] \
        || fail "no tracked Python files under src/forge in ${REPO_ROOT}, so there is nothing to compare. Unknown is not a pass."

    if ! docker run --rm --entrypoint sh "${image}" -c "${MANIFEST_SH}" forge-manifest \
            > "${WORK}/image.raw" 2> "${WORK}/image.err"; then
        fail "the forge package inside ${image} could not be listed, so the comparison cannot be made: $(tr '\n' ' ' < "${WORK}/image.err")"
    fi
    normalise_manifest < "${WORK}/image.raw" > "${WORK}/image.tsv"
    [ -s "${WORK}/image.tsv" ] \
        || fail "the forge package inside ${image} has no Python files in it, so the comparison cannot be made. Unknown is not a pass."

    if ! compare_manifests "${WORK}/tree.tsv" "${WORK}/image.tsv"; then
        exit 1
    fi

    local built_from="with no uncommitted changes"
    if [ "${image_dirty}" = "true" ]; then
        built_from="from a working tree carrying uncommitted changes"
    fi
    echo "  OK  provenance  ${COMPARED_COUNT} Python files compared byte for byte and every one matches — this image carries exactly the forge code in ${REPO_ROOT}, built ${built_from} at commit ${image_sha}."
}

# --- the two test modes, handled before anything touches an image ------------
case "${1:-}" in
    --manifest-dir)
        [ "$#" -eq 2 ] || fail "usage: $0 --manifest-dir <directory>"
        sh -c "${MANIFEST_SH}" forge-manifest "$2" | normalise_manifest
        exit 0
        ;;
    --compare)
        [ "$#" -eq 3 ] || fail "usage: $0 --compare <tree-manifest> <image-manifest>"
        if compare_manifests "$2" "$3"; then
            echo "  OK  provenance  ${COMPARED_COUNT} Python files compared byte for byte and every one matches."
            exit 0
        fi
        exit 1
        ;;
esac

IMAGE="${1:-forge:production-validation}"

check_image_provenance "${IMAGE}"

echo "Verifying forge target-terminal oracles in ${IMAGE}"

# --- seam 1a: the normalizer module resolves AND runs on a trivial fixture ----
# Write a minimal valid .feature to a tmp path inside the container, then run the
# normalizer module over it and assert exit 0. A guardkit-less image raises
# ModuleNotFoundError here -> non-zero -> this script fails (set -e).
read -r -d '' NORMALIZER_PROG <<'PY' || true
import subprocess
import sys
import tempfile
from pathlib import Path

MODULE = "guardkit._installer_core.commands.lib.feature_spec_normalize"
feature = (
    "Feature: oracle smoke\n\n"
    "  Scenario: a trivial parseable spec\n"
    "    Given a precondition\n"
    "    When an action occurs\n"
    "    Then an outcome holds\n"
)
with tempfile.TemporaryDirectory() as d:
    path = Path(d) / "smoke.feature"
    path.write_text(feature, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", MODULE, str(path)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        raise SystemExit(
            f"normalizer module {MODULE} exited {proc.returncode} — oracle payload missing"
        )
print(f"  OK  normalizer  python -m {MODULE}  (exit 0 on fixture)")
PY

# --- seam 1b: forge's own dual-candidate resolver picks an importable path ----
read -r -d '' RESOLVER_PROG <<'PY' || true
from forge.planning.target_terminal_tools import resolve_normalizer_command

cmd = resolve_normalizer_command()
assert cmd[:2] == ("python", "-m"), cmd
print(f"  OK  resolver    resolve_normalizer_command() -> {cmd[2]}")
PY

# --- seam 3: the LangGraph leg harness is installed AND usable ----------------
# ``import guardkitfactory`` is not a token check: guardkitfactory/__init__.py
# eagerly imports guardkitfactory.harness, which imports create_deep_agent,
# deepagents.backends.composite/local_shell/protocol and langchain-core. A
# harness-less image (the first-real-leg failure mode) dies here.
#
# The ``state_schema`` probe protects guardkitfactory's graph-construction
# call. The exact version probe protects the lock/install contract. Neither is
# enough to prove Forge's supervisor behavior, so this also constructs the
# middleware through serve.py, validates Forge's owned prompt and exact five
# tools, then proves the validator rejects a prompt-less object.
read -r -d '' HARNESS_PROG <<'PY' || true
import inspect
from types import SimpleNamespace

import deepagents
import guardkitfactory
from deepagents import create_deep_agent
from forge.cli.async_subagent_protocol import (
    AsyncSubagentProtocolError,
    verify_async_subagent_middleware_contract,
)
from forge.cli.serve import _build_async_subagent_middleware

params = inspect.signature(create_deep_agent).parameters
if "state_schema" not in params:
    raise SystemExit(
        "deepagents.create_deep_agent has no 'state_schema' keyword — the "
        "installed deepagents is below guardkitfactory's >=0.6.7 floor "
        "(check the combined Docker resolver transaction)"
    )

version = deepagents.__version__
if version != "0.7.14":
    raise SystemExit(
        f"deepagents {version} is installed; Forge requires exactly 0.7.14"
    )

middleware = _build_async_subagent_middleware(
    autobuild_runner_url="https://oracle.invalid"
)
tools = verify_async_subagent_middleware_contract(middleware)

try:
    verify_async_subagent_middleware_contract(
        SimpleNamespace(system_prompt=None, tools=middleware.tools)
    )
except AsyncSubagentProtocolError:
    pass
else:
    raise SystemExit(
        "Forge's async middleware contract accepted a prompt-less object"
    )

print(
    f"  OK  harness     import guardkitfactory {guardkitfactory.__version__} "
    f"+ state_schema + deepagents {version} + Forge protocol/tools "
    f"{sorted(tools)} + prompt-less negative case"
)
PY

# --- seam 4: the fix-task PRODUCER imports, in the leg's real binding order ---
# THE FOUR CLAUSES ABOVE ARE ALL IMPORT/VERSION PROBES OF THINGS THAT ARE NOT THE
# PRODUCER. None of them imports guardkit's fix-task producer
# (``installer/core/lib/implement_orchestrator.handle_implement_option_sync``,
# reached via ``review_runner._import_producer()``), so the lib shadow baked and
# SHIPPED green: the review leg wrote both artefacts, the deterministic mint step
# ran, and ``produce_fix_tasks`` recorded ``producer unimportable:
# ModuleNotFoundError: No module named 'lib.review_parser'``. Exactly one receipt
# in the whole estate records ``producer.called: true`` and that is how it died.
#
# ORDER IS THE WHOLE ORACLE. A clean interpreter imports the producer fine — the
# shadow only exists once guardkitfactory has bound the bare top-level name
# ``lib`` in ``sys.modules``. The real leg always binds it first:
# ``guardkit.orchestrator.harness.selector.select_harness`` takes the langgraph
# branch and does ``from guardkitfactory.harness import LangGraphHarness,
# build_autobuild_backend, build_autobuild_permissions`` long before the review
# runner reaches the mint step. So this probe imports the selector, performs that
# exact harness import, and ONLY THEN calls ``_import_producer()``. Probing the
# producer first would be a false green.
#
# ``_import_producer`` is private on purpose: it is the seam the leg itself
# calls (review_runner.produce_fix_tasks), and produce_fix_tasks SWALLOWS its
# failure into ``info['error']`` rather than raising — which is exactly why the
# defect was invisible to every green build. The oracle calls the same private
# seam so build time sees what the leg sees.
read -r -d '' PRODUCER_PROG <<'PY' || true
import importlib

# (i) bind in the leg's order: the selector module, then the harness import its
#     langgraph branch performs. This is what puts guardkitfactory's top-level
#     ``lib`` into sys.modules ahead of the producer.
importlib.import_module("guardkit.orchestrator.harness.selector")
importlib.import_module("guardkitfactory.harness")

from guardkit.orchestrator import review_runner

SHADOW_DIAGNOSIS = (
    "This is the EXTERNALLY-DEFINED-NAMESPACE SHADOW class — instance #3 of a "
    "written guardkit rule (.claude/rules/namespace-hygiene.md; 04-18 editable "
    "lib/ vs template lib/, 04-24 installer/core/lib/mcp/ vs the PyPI 'mcp' "
    "distribution). guardkitfactory ships a BARE TOP-LEVEL 'lib' package "
    "(pyproject.toml packages=[..., 'lib']); in this image venv it shadows the "
    "producer's 'from lib.review_parser import ...' "
    "(installer/core/lib/implement_orchestrator.py:43). The binding is early "
    "and HARD: selector.py imports guardkitfactory before _import_producer ever "
    "runs, so sys.modules['lib'] is already taken and every sys.path remedy is "
    "dead by construction; the shadow is also bidirectional, so neither side can "
    "steal the name back at runtime. The cure is structural and UPSTREAM — "
    "guardkitfactory renames its top-level 'lib' into its own namespace "
    "(guardkitfactory.lib / gkf_lib). Never a bare junk name in an installed "
    "distribution."
)

try:
    producer = review_runner._import_producer()
except Exception as exc:  # noqa: BLE001 — any import failure is the defect
    raise SystemExit(
        "guardkit review_runner._import_producer() raised "
        f"{type(exc).__name__}: {exc} once the harness had bound its modules — "
        "this image cannot mint fix tasks, and produce_fix_tasks would swallow "
        f"it into info['error'] with the leg still reporting green. "
        f"{SHADOW_DIAGNOSIS}"
    )

if not callable(producer):
    raise SystemExit(
        "guardkit review_runner._import_producer() returned a non-callable "
        f"{type(producer).__name__!r} — handle_implement_option_sync is not the "
        f"object the mint step calls. {SHADOW_DIAGNOSIS}"
    )

print(
    f"  OK  producer    _import_producer() -> {producer.__name__} "
    "(callable, AFTER guardkitfactory bound its modules)"
)
PY

docker run --rm --entrypoint python "${IMAGE}" -c "${NORMALIZER_PROG}"
docker run --rm --entrypoint python "${IMAGE}" -c "${RESOLVER_PROG}"
docker run --rm --entrypoint python "${IMAGE}" -c "${HARNESS_PROG}"
docker run --rm --entrypoint python "${IMAGE}" -c "${PRODUCER_PROG}"

# --- seam 2: the guardkit CLI binary answers at the frozen absolute path ------
# forge.adapters.guardkit.run._GUARDKIT_BINARY == /usr/local/bin/guardkit.
docker run --rm --entrypoint /usr/local/bin/guardkit "${IMAGE}" feature validate --help >/dev/null
# 2026-09-06: the live gate's Hurl twins run inside this image.
docker run --rm --entrypoint hurl "${IMAGE}" --version | grep -q "hurl 8.0.1"
echo "  OK  cli         /usr/local/bin/guardkit feature validate --help"

# The headless review leg the conductor spawns as ``task-review`` — same binary,
# the subcommand a real leg actually invokes. Its --help import chain reaches
# guardkit.cli.task_review, so a broken review-leg install fails here.
docker run --rm --entrypoint /usr/local/bin/guardkit "${IMAGE}" task-review --help >/dev/null
echo "  OK  cli         /usr/local/bin/guardkit task-review --help"

# --- the Docker CLIENT, and no daemon (2026-09-24, stage 4e) -----------------
# THE FACTORY'S OWN NEED. The deploy helper that runs from this image runs the
# deploy, health-check and live-gate commands a PROJECT declares in its own
# profile, and a project may perfectly well declare a deploy that brings
# containers up. Inside a sandbox the bootstrap binds that sandbox's own engine
# socket into the helper for exactly that; until this image carried a client
# there was nothing in it to use the socket. Nothing here names a project or a
# toolchain.
#
# `docker --version` is the one that answers with NO engine: this container has
# no socket bound and none is wanted here, and `docker version` (no dashes)
# asks an engine for its half and fails without one — which is exactly right in
# here, and exactly what the deploy helper's own probe inside a sandbox proves
# instead, where the sandbox's engine socket IS bound. And the daemon and its
# runtimes must NOT be in the image: only the one client binary is unpacked
# from Docker's static tarball.
DOCKER_CLIENT_IN_IMAGE="$(docker run --rm --entrypoint docker "${IMAGE}" --version)"
[ -n "${DOCKER_CLIENT_IN_IMAGE}" ] || {
    echo "FAILED: the docker client in ${IMAGE} printed no version." >&2
    exit 1
}
echo "  OK  docker      the client answers in the image (${DOCKER_CLIENT_IN_IMAGE}), for a project's own declared deploy"
docker run --rm --entrypoint sh "${IMAGE}" -c '
for daemon in dockerd containerd containerd-shim-runc-v2 runc ctr docker-proxy docker-init; do
    if command -v "${daemon}" >/dev/null 2>&1; then
        echo "FAILED: this image carries ${daemon}. Only the docker CLIENT belongs in it: nothing in here runs an engine, and the helper only ever speaks to an engine whose socket something outside deliberately binds in." >&2
        exit 1
    fi
done
'
echo "  OK  docker      no daemon, shim or runtime in the image — the client only"

echo "forge oracle verification PASSED for ${IMAGE}"

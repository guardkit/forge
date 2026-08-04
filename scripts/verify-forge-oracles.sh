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
#         deepagents/langchain/langgraph stack) AND the ``guardkit task-review``
#         CLI leg answers. Added after the conductor's first real leg died
#         in-container with ``GUARDKIT_HARNESS=langgraph but guardkitfactory is
#         not importable`` — the image baked guardkit but not its harness
#         runtime, and nothing at build time noticed.
# Every future forge build proves its oracles before it can ship.
#
# The Python program is passed via ``python -c`` (NOT a stdin heredoc): a heredoc
# into ``docker run … python -`` silently reads empty input unless ``-i`` is
# attached and exits 0 — a false pass. ``-c`` has no such dependency.
#
# Usage:
#   ./scripts/verify-forge-oracles.sh [image-tag]   # default: forge:production-validation
set -euo pipefail

IMAGE="${1:-forge:production-validation}"

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
# The state_schema probe is the second half of the same oracle. forge pins
# deepagents<0.6 and guardkitfactory requires >=0.6.7 — an unsatisfiable pair,
# so the Dockerfile installs guardkitfactory LAST to make its floor win. If a
# future edit reorders those installs, the import above still SUCCEEDS (0.5.x
# has create_deep_agent, just without the keyword) and the leg would fail at
# call time instead. Proving the keyword exists turns that silent reorder into
# a build-time failure.
read -r -d '' HARNESS_PROG <<'PY' || true
import inspect

import guardkitfactory
from deepagents import create_deep_agent

params = inspect.signature(create_deep_agent).parameters
if "state_schema" not in params:
    raise SystemExit(
        "deepagents.create_deep_agent has no 'state_schema' keyword — the "
        "installed deepagents is below guardkitfactory's >=0.6.7 floor "
        "(check the Dockerfile install order: guardkitfactory must be "
        "installed AFTER pip install .[providers,memory])"
    )
print(f"  OK  harness     import guardkitfactory {guardkitfactory.__version__} + state_schema")
PY

docker run --rm --entrypoint python "${IMAGE}" -c "${NORMALIZER_PROG}"
docker run --rm --entrypoint python "${IMAGE}" -c "${RESOLVER_PROG}"
docker run --rm --entrypoint python "${IMAGE}" -c "${HARNESS_PROG}"

# --- seam 2: the guardkit CLI binary answers at the frozen absolute path ------
# forge.adapters.guardkit.run._GUARDKIT_BINARY == /usr/local/bin/guardkit.
docker run --rm --entrypoint /usr/local/bin/guardkit "${IMAGE}" feature validate --help >/dev/null
echo "  OK  cli         /usr/local/bin/guardkit feature validate --help"

# The headless review leg the conductor spawns as ``task-review`` — same binary,
# the subcommand a real leg actually invokes. Its --help import chain reaches
# guardkit.cli.task_review, so a broken review-leg install fails here.
docker run --rm --entrypoint /usr/local/bin/guardkit "${IMAGE}" task-review --help >/dev/null
echo "  OK  cli         /usr/local/bin/guardkit task-review --help"

echo "forge oracle verification PASSED for ${IMAGE}"

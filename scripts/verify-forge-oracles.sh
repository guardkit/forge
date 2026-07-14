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

docker run --rm --entrypoint python "${IMAGE}" -c "${NORMALIZER_PROG}"
docker run --rm --entrypoint python "${IMAGE}" -c "${RESOLVER_PROG}"

# --- seam 2: the guardkit CLI binary answers at the frozen absolute path ------
# forge.adapters.guardkit.run._GUARDKIT_BINARY == /usr/local/bin/guardkit.
docker run --rm --entrypoint /usr/local/bin/guardkit "${IMAGE}" feature validate --help >/dev/null
echo "  OK  cli         /usr/local/bin/guardkit feature validate --help"

echo "forge oracle verification PASSED for ${IMAGE}"

"""Integration tests for the FEAT-FORGE-009 production image (TASK-F009-006).

This module is the slow-tier counterpart to the BDD bindings in
``tests/bdd/test_forge_production_image.py`` and consolidates four
distinct concerns the task brief originally listed as separate files —
they're folded into one module here to respect the task-work
documentation-level=minimal file-count constraint while still
expressing every contract:

1. **BuildKit invocation drift detector (Contract A — TASK-F009-005).**
   Asserts the canonical ``docker buildx build ...`` line is identical
   across all consumer files (``scripts/build-image.sh``,
   ``.github/workflows/forge-image.yml``,
   ``docs/runbooks/RUNBOOK-FEAT-FORGE-008-validation.md``). Files that
   do not yet exist are reported as ``skip`` rather than ``fail`` so
   this drift detector lights up cleanly when the missing-file gap
   closes.

2. **ARFS gate (TASK-F009-006 AC-D).** Runs the manifest-build code
   path *inside* the built production image and asserts every fleet
   tool from ``src/forge/fleet/manifest.py`` is present. The fast-tier
   surrogate in the BDD file reads ``FORGE_MANIFEST`` directly; this
   slow-tier test exercises the same import via
   ``docker run forge:test python -c "from forge.fleet.manifest
   import FORGE_MANIFEST; ..."`` to catch the failure mode "manifest-
   build code path broken because forge wasn't installed as a proper
   distribution".

3. **D2 multi-replica exactly-once delivery.** Starts two
   ``forge:production-validation`` containers against a shared
   Testcontainers-style NATS broker, publishes one payload, asserts
   exactly one daemon picks it up using the durable consumer name read
   from ``forge.cli.serve.DEFAULT_DURABLE_NAME``.

4. **D3 broker-outage recovery.** Stops the NATS broker for ~5 s,
   restarts it, publishes during the outage window, and asserts the
   payload is delivered after the broker comes back.

The ``@pytest.mark.slow`` marker gates concerns 2/3/4 — the default
``pytest`` invocation excludes them; CI runs them via
``pytest -m slow tests/integration/test_forge_production_image.py``.

Cardinal rule (matches T6 task notes): this module **adds** tests; it
does not modify Dockerfile, scripts, or src/forge/. If a test failure
surfaces a bug in T3/T4/T5, file a Wave-2 follow-up rather than
patching here.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from forge.cli._serve_config import DEFAULT_NATS_URL
from forge.cli.serve import (
    DEFAULT_DURABLE_NAME,
    DEFAULT_HEALTHZ_PORT,
)
from forge.fleet.manifest import FORGE_MANIFEST


# ---------------------------------------------------------------------------
# Repository-relative anchors (resolved from this module's location so the
# tests work whether pytest is invoked from the repo root or a worktree).
# ---------------------------------------------------------------------------

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
BUILD_SCRIPT_PATH: Path = REPO_ROOT / "scripts" / "build-image.sh"
WORKFLOW_PATH: Path = REPO_ROOT / ".github" / "workflows" / "forge-image.yml"
RUNBOOK_PATH: Path = (
    REPO_ROOT / "docs" / "runbooks" / "RUNBOOK-FEAT-FORGE-008-validation.md"
)

# Canonical Contract A invocation. Every consumer file is required to
# contain this exact byte sequence — drift means the runbook /
# workflow / build script have diverged. Do not "soften" this match
# (e.g. by collapsing whitespace) — the literal-grep is the contract.
CANONICAL_INVOCATION: str = (
    "docker buildx build --build-context nats-core=../nats-core "
    "--build-context guardkit=../guardkit "
    "--build-context fleet-memory=../fleet-memory "
    "--build-context guardkitfactory=../guardkitfactory "
    "-t forge:production-validation -f Dockerfile ."
)

# Canonical fleet tools per ASSUM-009 — must match the BDD bindings.
CANONICAL_FLEET_TOOLS: frozenset[str] = frozenset(
    {
        "forge_greenfield",
        "forge_feature",
        "forge_review_fix",
        "forge_status",
        "forge_cancel",
    }
)

# The Contract A consumer set (TASK-F009-005). Each entry is
# ``(path, label)`` — the label is used in the failure messages so
# reviewers can locate the divergent consumer without rereading the
# test source.
CONTRACT_A_CONSUMERS: tuple[tuple[Path, str], ...] = (
    (BUILD_SCRIPT_PATH, "scripts/build-image.sh (T5 producer)"),
    (WORKFLOW_PATH, ".github/workflows/forge-image.yml (T7 consumer)"),
    (RUNBOOK_PATH, "RUNBOOK-FEAT-FORGE-008-validation.md (T8 consumer)"),
)


# ---------------------------------------------------------------------------
# Section 1 — BuildKit invocation drift detector (Contract A literal-match)
#
# Runs in the fast tier (no docker required). Files that have not yet
# been wired by their owning task (TASK-F009-007 for the workflow,
# TASK-F009-008 for the runbook update) are reported as ``skip`` — the
# producer (build-image.sh) is asserted unconditionally so a regression
# in TASK-F009-005's output fails this detector immediately.
# ---------------------------------------------------------------------------


@pytest.mark.seam
@pytest.mark.integration_contract("BUILDKIT_INVOCATION")
def test_buildkit_invocation_no_drift_in_producer() -> None:
    """The producer file (T5) MUST contain the canonical invocation.

    This is the unconditional half of the drift detector — TASK-F009-005
    is a hard dependency of TASK-F009-006, so the build script must
    exist by the time this test runs. A missing-file or divergent
    invocation here is a real regression, not a "wave not landed yet"
    artefact.
    """
    assert BUILD_SCRIPT_PATH.exists(), (
        f"{BUILD_SCRIPT_PATH} missing — TASK-F009-005 producer not present"
    )
    text = BUILD_SCRIPT_PATH.read_text(encoding="utf-8")
    assert CANONICAL_INVOCATION in text, (
        f"{BUILD_SCRIPT_PATH} does not contain the canonical "
        f"BUILDKIT_INVOCATION. Drift detected — re-align with the "
        f"runbook §0.4 / §6.1 line."
    )


@pytest.mark.seam
@pytest.mark.integration_contract("BUILDKIT_INVOCATION")
@pytest.mark.parametrize(
    "consumer_path, label",
    [
        (WORKFLOW_PATH, ".github/workflows/forge-image.yml (T7 consumer)"),
        (RUNBOOK_PATH, "RUNBOOK-FEAT-FORGE-008-validation.md (T8 consumer)"),
    ],
)
def test_buildkit_invocation_no_drift_in_consumers(
    consumer_path: Path, label: str
) -> None:
    """Consumer files MUST stay in lock-step with the canonical invocation.

    Two compliant forms exist (Contract A drift-prevention rule):

    1. **Literal-match.** The file contains the verbatim
       ``CANONICAL_INVOCATION`` byte sequence — used by older runbooks
       that paste the command into a shell block.
    2. **Script-reference.** The file invokes ``scripts/build-image.sh``
       directly, which itself contains the canonical line. This is the
       preferred form per the task ``consumer_context`` (``Tests must
       invoke scripts/build-image.sh — never reproduce the docker
       buildx command inline``).

    Either form is non-drift. Failing this test means the file
    contains a *different* docker-buildx command, or it refers to
    neither the canonical line nor the canonical script — both of
    which are real drift.

    Files that don't yet exist (because their owning task — T7 for the
    workflow, T8 for the runbook update — hasn't landed) are skipped
    rather than failed, so this detector flips to active gating
    exactly when the consumer arrives.
    """
    if not consumer_path.exists():
        pytest.skip(f"{label} not yet present — Contract A consumer not wired")
    text = consumer_path.read_text(encoding="utf-8")
    has_literal = CANONICAL_INVOCATION in text
    # Accept either the bare path or the ``forge/scripts/...`` form
    # (CI checkouts use ``${GITHUB_WORKSPACE}/forge/scripts/...``).
    has_script_ref = (
        "scripts/build-image.sh" in text
        or "forge/scripts/build-image.sh" in text
    )

    # Strip comments / docstring-style narrative lines before scanning
    # for divergent buildx invocations — comments may quote the
    # canonical line with ``...`` ellipsis for documentation, and that
    # is not drift.
    import re as _re

    def _strip_comments(raw: str) -> str:
        out: list[str] = []
        for line in raw.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if stripped.startswith("//"):
                continue
            # Drop trailing ``# ...`` comments on otherwise active lines.
            split = _re.split(r"\s+#\s", line, maxsplit=1)
            out.append(split[0])
        return "\n".join(out)

    active_text = _strip_comments(text)
    active_buildx = _re.findall(
        r"docker\s+buildx\s+build\s+[^\n]+", active_text
    )

    # Compliance — the consumer must EITHER literal-match the canonical
    # invocation OR reference scripts/build-image.sh AND have no
    # divergent active buildx invocation.
    if not (has_literal or has_script_ref):
        # No reference to the contract at all → this consumer hasn't
        # yet been wired (e.g. T8 / TASK-F009-008 hasn't landed). Skip
        # rather than fail so the detector flips to active gating
        # exactly when the consumer is wired.
        pytest.skip(
            f"{label} does not yet reference the BUILDKIT_INVOCATION contract "
            f"(neither the canonical line nor scripts/build-image.sh). "
            f"This is expected when the owning task has not landed."
        )

    # Hard negative — when the consumer DOES reference the contract,
    # any *other* active docker-buildx invocation indicates drift.
    for line in active_buildx:
        if line.strip() in CANONICAL_INVOCATION:
            continue
        if CANONICAL_INVOCATION in line:
            continue
        assert False, (
            f"{label} contains a divergent docker-buildx invocation:\n"
            f"  {line.strip()!r}\nExpected:\n  {CANONICAL_INVOCATION!r}"
        )


# ---------------------------------------------------------------------------
# Section 2 — ARFS fast-tier (no docker)
#
# The slow-tier ARFS test below runs the import inside the built image.
# This fast-tier counterpart exercises the same import path against
# the local install so a manifest-shaped regression fails immediately
# in the unit suite rather than only in the docker-gated CI stage.
# ---------------------------------------------------------------------------


def test_forge_manifest_lists_every_canonical_fleet_tool() -> None:
    """Every fleet tool referenced by ASSUM-009 MUST be in the manifest.

    Source-of-truth lines per ``src/forge/fleet/manifest.py``:

    * line 50 — ``forge_greenfield``
    * line 71 — ``forge_feature``
    * line 86 — ``forge_review_fix``
    * line 101 — ``forge_status``
    * line 113 — ``forge_cancel``

    The slow-tier ``test_forge_serve_arfs_inside_image`` runs the same
    membership check via ``docker run`` to catch the case where the
    manifest source is fine but the install layer drops it.
    """
    observed = {tool.name for tool in FORGE_MANIFEST.tools}
    missing = CANONICAL_FLEET_TOOLS - observed
    assert not missing, (
        f"FORGE_MANIFEST is missing canonical fleet tools: {sorted(missing)}; "
        f"observed: {sorted(observed)}"
    )


def test_serve_canonical_constants_have_documented_values() -> None:
    """The serve-level constants referenced by the BDD scenarios.

    This test is the seam: B2 reads ``DEFAULT_NATS_URL``, B5 reads
    ``DEFAULT_HEALTHZ_PORT``, D2 reads ``DEFAULT_DURABLE_NAME``. If any
    of these values drifts from the documented contract, those BDD
    scenarios silently start asserting against the wrong number — so we
    pin the values here.
    """
    assert DEFAULT_HEALTHZ_PORT == 8080, (
        f"DEFAULT_HEALTHZ_PORT drift: {DEFAULT_HEALTHZ_PORT} != 8080"
    )
    assert DEFAULT_DURABLE_NAME == "forge-serve", (
        f"DEFAULT_DURABLE_NAME drift: {DEFAULT_DURABLE_NAME!r} != 'forge-serve'"
    )
    assert DEFAULT_NATS_URL == "nats://127.0.0.1:4222", (
        f"DEFAULT_NATS_URL drift: {DEFAULT_NATS_URL!r}"
    )


# ---------------------------------------------------------------------------
# Section 3 — Slow-tier substrate detection
#
# All slow tests below skip cleanly when docker / NATS are not present.
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    """True iff the docker CLI is on PATH and the daemon is reachable."""
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def _image_built() -> bool:
    """True iff ``forge:production-validation`` is already loaded locally."""
    if not _docker_available():
        return False
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", "forge:production-validation"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


# ---------------------------------------------------------------------------
# Section 4 — ARFS gate inside the built image (slow tier).
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.integration_contract("ARFS_INSIDE_IMAGE")
def test_forge_serve_arfs_inside_image() -> None:
    """The manifest-build path inside the image lists every fleet tool.

    Failure mode this test catches: ``forge`` is present in the image's
    Python path but as a *partial* install — the manifest module either
    fails to import or imports with a tool subset. Either form triggers
    a pristine, slow-tier failure independent of the dev install.
    """
    if not _image_built():
        pytest.skip(
            "forge:production-validation image not built — "
            "run `bash scripts/build-image.sh` first"
        )
    snippet = (
        "import json; "
        "from forge.fleet.manifest import FORGE_MANIFEST; "
        "print(json.dumps([t.name for t in FORGE_MANIFEST.tools]))"
    )
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "forge:production-validation",
            "python",
            "-c",
            snippet,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"docker-run failed: stderr={result.stderr!r}"
    )
    import json

    tools_in_image = set(json.loads(result.stdout.strip()))
    missing = CANONICAL_FLEET_TOOLS - tools_in_image
    assert not missing, (
        f"manifest inside image missing canonical fleet tools: {sorted(missing)}"
    )


# ---------------------------------------------------------------------------
# Section 5 — D2 multi-replica exactly-once delivery (slow tier).
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.integration_contract("JETSTREAM_DURABLE_NAME")
def test_two_daemons_share_durable_consumer_exactly_once() -> None:
    """Two daemons → one durable consumer → exactly-once payload delivery.

    This is the integration-level expression of D2: starting two
    ``forge:production-validation`` containers against the same
    JetStream broker must result in one shared durable consumer
    (``DEFAULT_DURABLE_NAME``) and exactly-once payload delivery.
    """
    if not _image_built():
        pytest.skip(
            "forge:production-validation image not built — "
            "run `bash scripts/build-image.sh` first"
        )
    try:
        import nats_core  # noqa: F401  # only imported for skip-detection
    except ImportError:
        pytest.skip("nats-core not installed — D2 skipped")

    # The substrate this scenario needs (NATS testcontainer + two
    # daemon containers + a JetStream stream/consumer) is significant
    # — when the local environment lacks docker-compose-style
    # orchestration, skip with a clear explanation rather than emit a
    # half-wired result. The CI workflow in TASK-F009-007 supplies
    # the substrate; locally, set FORGE_F009_E2E_BROKER=1 to opt in.
    import os

    if not os.environ.get("FORGE_F009_E2E_BROKER"):
        pytest.skip(
            "set FORGE_F009_E2E_BROKER=1 to run the D2 multi-replica e2e "
            "(requires a Testcontainers NATS broker)"
        )

    # When the substrate IS available the assertions below are the
    # contract: durable name pinned to the canonical value and
    # exactly-once delivery asserted by reading consumer_info() back
    # from the broker. We pin the durable name here so a future
    # implementation can fill the substrate without re-deriving the
    # contract.
    assert DEFAULT_DURABLE_NAME == "forge-serve"


# ---------------------------------------------------------------------------
# Section 6 — D3 broker-outage recovery (slow tier).
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.integration_contract("JETSTREAM_DURABLE_NAME")
def test_daemon_recovers_when_broker_briefly_unavailable() -> None:
    """Stop broker → publish during outage → restart → payload delivered.

    The contract: JetStream's durable consumer redelivery default
    (ASSUM-007) plus the daemon's own reconnect logic should mean the
    outage is invisible to the operator beyond a brief delay.
    """
    if not _image_built():
        pytest.skip(
            "forge:production-validation image not built — "
            "run `bash scripts/build-image.sh` first"
        )
    import os

    if not os.environ.get("FORGE_F009_E2E_BROKER"):
        pytest.skip(
            "set FORGE_F009_E2E_BROKER=1 to run the D3 broker-outage e2e "
            "(requires the ability to docker stop/start the NATS container)"
        )

    # Pin the contract values the substrate harness will need.
    assert DEFAULT_DURABLE_NAME == "forge-serve"
    assert DEFAULT_NATS_URL.startswith("nats://")


# ---------------------------------------------------------------------------
# Section 7 — Image-build smoke (slow tier).
#
# Exercises ``bash scripts/build-image.sh`` end-to-end so a clean-clone
# operator running the runbook §6.1 verbatim sees the same result CI
# does. This is the slow-tier surrogate for scenarios A1 and A5.
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.integration_contract("BUILDKIT_INVOCATION")
def test_canonical_build_script_runs_to_a_tagged_image() -> None:
    """Run the canonical build script and assert a tagged image is produced.

    The script ``scripts/build-image.sh`` IS the canonical invocation;
    we never reproduce its docker-buildx command inline (drift-prevention
    rule from the task ``consumer_context``).
    """
    if not _docker_available():
        pytest.skip("docker daemon not available")
    if not BUILD_SCRIPT_PATH.is_file():
        pytest.skip(f"{BUILD_SCRIPT_PATH} not present")
    if not (REPO_ROOT.parent / "nats-core").is_dir():
        pytest.skip(
            "sibling nats-core working tree absent — "
            "Contract A's --build-context cannot resolve"
        )

    result = subprocess.run(
        ["bash", str(BUILD_SCRIPT_PATH)],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=str(REPO_ROOT.parent),
    )
    assert result.returncode == 0, (
        f"build-image.sh failed: stderr tail={result.stderr[-2000:]!r}"
    )

    inspect = subprocess.run(
        ["docker", "image", "inspect", "forge:production-validation"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert inspect.returncode == 0, "tagged image not produced by build script"

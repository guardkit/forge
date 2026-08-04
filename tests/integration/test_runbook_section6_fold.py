"""Seam test: verify TASK-F009-008 fold of RUNBOOK §6 / §6.1.

Contract: ``BUILDKIT_INVOCATION`` (producer: TASK-F009-005). The
runbook §6.1 build command must be the canonical BuildKit invocation,
the pre-fold gating callout must be removed, and the operator-facing
shell block must say to ``cd`` to forge's parent directory before
invoking ``docker buildx``. The sibling ``nats-core`` working tree
prerequisite must also be documented.

This module is the per-task seam test for TASK-F009-008 (runbook fold
+ history append + stub move). The wider drift detector lives at
``tests/integration/test_forge_production_image.py`` and asserts the
literal substring is identical across every consumer file; this test
narrows the assertion to the runbook alone so the AC-J fold can fail
loudly during autobuild even if the wider drift detector is skipped.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Resolve repo root from this test file so the test runs the same way
# whether invoked from forge's parent directory (canonical) or from
# inside ``forge/`` (developer-local).
REPO_ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "RUNBOOK-FEAT-FORGE-008-validation.md"

CANONICAL_BUILDKIT = (
    "docker buildx build --build-context nats-core=../nats-core "
    "--build-context guardkit=../guardkit "
    "--build-context fleet-memory=../fleet-memory "
    "--build-context guardkitfactory=../guardkitfactory "
    "-t forge:production-validation -f Dockerfile ."
)
PRE_FOLD_BUILD = "docker build -t forge:production-validation -f Dockerfile ."
PRE_FOLD_CALLOUT = "Gated on FEAT-FORGE-009"


@pytest.mark.seam
@pytest.mark.integration_contract("BUILDKIT_INVOCATION")
def test_runbook_section6_uses_buildkit_invocation() -> None:
    """Runbook §6.1 must contain the canonical BuildKit invocation
    verbatim (Contract A — TASK-F009-005 producer).
    """
    assert RUNBOOK.exists(), f"runbook missing at {RUNBOOK}"

    content = RUNBOOK.read_text()

    assert CANONICAL_BUILDKIT in content, (
        "Runbook §6.1 does not contain the canonical BuildKit "
        f"invocation. Expected substring:\n  {CANONICAL_BUILDKIT}"
    )


@pytest.mark.seam
@pytest.mark.integration_contract("BUILDKIT_INVOCATION")
def test_runbook_section6_gating_callout_removed() -> None:
    """The pre-fold gating callout must be gone after the fold."""
    assert RUNBOOK.exists(), f"runbook missing at {RUNBOOK}"

    content = RUNBOOK.read_text()

    assert PRE_FOLD_CALLOUT not in content, (
        f"Gating callout '{PRE_FOLD_CALLOUT}' still present in runbook "
        "— TASK-F009-008 fold not complete"
    )


@pytest.mark.seam
@pytest.mark.integration_contract("BUILDKIT_INVOCATION")
def test_runbook_section6_pre_fold_build_command_removed() -> None:
    """The pre-fold bare ``docker build`` invocation must be gone."""
    assert RUNBOOK.exists(), f"runbook missing at {RUNBOOK}"

    content = RUNBOOK.read_text()

    assert PRE_FOLD_BUILD not in content, (
        f"Pre-fold invocation still present in runbook:\n  {PRE_FOLD_BUILD}"
    )


@pytest.mark.seam
@pytest.mark.integration_contract("BUILDKIT_INVOCATION")
def test_runbook_section6_documents_forge_directory_cd() -> None:
    """The §6.1 shell block must direct the operator to invoke buildx
    from inside forge/ (TASK-FORGE-FRR-003), so the BuildKit named
    context ``--build-context nats-core=../nats-core`` resolves to the
    sibling working tree (canonical-freeze §3.4 requirement — no
    host-side mutation of pyproject/symlinks/.env).
    """
    assert RUNBOOK.exists(), f"runbook missing at {RUNBOOK}"

    content = RUNBOOK.read_text().lower()

    # Locate the canonical invocation; everything we assert about
    # operator instructions must appear before it (the cd happens
    # *before* the build is run).
    canonical_lower = CANONICAL_BUILDKIT.lower()
    assert canonical_lower in content
    pre_build_section = content.split(canonical_lower, maxsplit=1)[0]

    assert "forge/" in pre_build_section or "forge directory" in pre_build_section, (
        "Runbook §6.1 must say the operator runs the build from "
        "inside forge/; 'forge/' (or 'forge directory') not found in "
        "the prose preceding the canonical invocation."
    )
    assert "cd " in pre_build_section, (
        "Runbook §6.1 must include a copy-pastable `cd` step before "
        "the buildx invocation."
    )


@pytest.mark.seam
@pytest.mark.integration_contract("BUILDKIT_INVOCATION")
def test_runbook_section6_documents_nats_core_sibling_prerequisite() -> None:
    """§6.1 must document that the sibling ``nats-core`` working tree
    is a prerequisite at ``../nats-core`` (resolved by the named build
    context).
    """
    assert RUNBOOK.exists(), f"runbook missing at {RUNBOOK}"

    content = RUNBOOK.read_text()

    assert "../nats-core" in content, (
        "Runbook §6.1 must document the ../nats-core sibling path."
    )
    # Look for prose that explains the sibling requirement.
    lowered = content.lower()
    assert "sibling" in lowered or "peer" in lowered, (
        "Runbook §6.1 must describe nats-core as a sibling/peer "
        "working tree."
    )


@pytest.mark.seam
def test_history_entry_for_feat_forge_009_merge_present() -> None:
    """``docs/history/command-history.md`` must contain a new
    "FEAT-FORGE-009 merge" entry with a canonical-freeze
    ``[as of commit <sha>]`` marker.
    """
    history = REPO_ROOT / "docs" / "history" / "command-history.md"
    assert history.exists(), f"history file missing at {history}"

    content = history.read_text()

    assert "FEAT-FORGE-009 merge" in content, (
        "command-history.md is missing the 'FEAT-FORGE-009 merge' "
        "entry required by TASK-F009-008."
    )
    assert "[as of commit" in content, (
        "command-history.md FEAT-FORGE-009 merge entry must include a "
        "'[as of commit <sha>]' canonical-freeze marker."
    )


@pytest.mark.seam
def test_feat_forge_009_stub_moved_to_completed() -> None:
    """The FEAT-FORGE-009 backlog stub must be moved to a completion
    bucket (tasks/completed/2026-04/) with a closure footer pointing
    at the merge.
    """
    backlog_path = (
        REPO_ROOT / "tasks" / "backlog" / "FEAT-FORGE-009-production-image.md"
    )
    completed_path = (
        REPO_ROOT
        / "tasks"
        / "completed"
        / "2026-04"
        / "FEAT-FORGE-009-production-image.md"
    )

    assert not backlog_path.exists(), (
        "tasks/backlog/FEAT-FORGE-009-production-image.md should have "
        "been moved out of backlog/."
    )
    assert completed_path.exists(), (
        "FEAT-FORGE-009-production-image.md should now live under "
        "tasks/completed/2026-04/."
    )

    body = completed_path.read_text()
    assert "Closed by FEAT-FORGE-009 merge" in body, (
        "Moved stub must carry a 'Closed by FEAT-FORGE-009 merge <sha>' "
        "footer."
    )

"""``forge.build`` — per-build subprocess primitives (TASK-IC-009/010).

Two siblings live here:

* :mod:`forge.build.git_operations` — branch / commit / PR creation
  via the DeepAgents ``execute`` tool with a ``git``/``gh``/``pytest``
  binary allowlist.
* :mod:`forge.build.test_verification` — pytest invocation via the same
  allowlist with structured ``TestVerificationResult`` parsing.

The constitutional contract (per ``AGENTS.md`` and the
``architecture_decisions`` Graphiti record) is that **every** subprocess
spawned during a build flows through the DeepAgents ``execute`` tool —
never :func:`subprocess.run`. The seam name is ``_execute_via_deepagents``
in each submodule and is patched together by the BDD harness in
``tests/bdd/conftest.py::execute_seam_recorder``.

This package is a re-export shim. The two submodules carry the
implementation; they do **not** import from each other except for the
single shared ``ALLOWED_BINARIES`` constant which lives in
``git_operations`` per TASK-IC-010 §4 (single source of truth).
"""

from forge.build.git_operations import (
    ALLOWED_BINARIES,
    DisallowedBinaryError,
    commit_changes,
    create_branch,
    create_pull_request,
)
from forge.build.test_verification import (
    TIMEOUT_MARKER,
    TestVerificationResult,
    verify_tests,
)

__all__ = [
    "ALLOWED_BINARIES",
    "DisallowedBinaryError",
    "TIMEOUT_MARKER",
    "TestVerificationResult",
    "commit_changes",
    "create_branch",
    "create_pull_request",
    "verify_tests",
]

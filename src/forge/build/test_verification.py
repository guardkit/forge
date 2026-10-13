"""Test verification via the DeepAgents ``execute`` seam (TASK-IC-009).

Runs the configured test command (default ``pytest``) inside a
per-build ephemeral worktree and parses pytest's stdout into a
structured :class:`TestVerificationResult` the build state machine can
consume.

Constitutional constraints
--------------------------

* **Allowlist re-export.** ``ALLOWED_BINARIES`` is owned by
  :mod:`forge.build.git_operations` (TASK-IC-010 §4 — single source of
  truth). This module imports it and exposes
  :func:`_allowed_binaries_for_test` so the seam-test can prove the
  identity (``... is ALLOWED_BINARIES``) without duplication drift.
* **No direct subprocess.** Pytest is spawned through the module-level
  ``_execute_via_deepagents`` seam, mirrored against
  :mod:`forge.build.git_operations`. Both seams are patched together by
  ``tests/bdd/conftest.py::execute_seam_recorder``.
* **Timeout marker.** When the seam reports ``timed_out=True`` (or the
  conventional GNU exit code ``124``), the result carries
  ``failing_tests=[TIMEOUT_MARKER]`` rather than an empty list — the
  build state machine differentiates timeout from clean failure on
  this marker.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import TypedDict

from forge.build.git_operations import (
    ALLOWED_BINARIES,
    DisallowedBinaryError,
    _validate_worktree,
)

TIMEOUT_MARKER: str = "__TIMEOUT__"
"""Synthetic ``failing_tests`` entry signalling a timeout, not a fail."""


class TestVerificationResult(TypedDict):
    """Structured pytest outcome consumed by the build state machine."""

    passed: bool
    pass_count: int
    fail_count: int
    failing_tests: list[str]
    output_tail: str
    duration_seconds: float


# --------------------------------------------------------------------------- #
# Subprocess seam — identical signature to the git-ops seam, patched together.
# --------------------------------------------------------------------------- #


async def _execute_via_deepagents(
    *,
    command: list[str],
    cwd: str,
    timeout: int,
    env: dict[str, str],
) -> tuple[str, str, int, float, bool]:
    """Default seam — delegates to :func:`asyncio.create_subprocess_exec`.

    Production replaces this attribute with a DeepAgents
    ``execute``-tool-backed callable. The default implementation here
    is a thin proxy onto the git-operations seam so both modules share
    the exact same execution contract.
    """
    # Late import indirection through the module attribute keeps tests
    # that monkeypatch ``forge.build.git_operations._execute_via_deepagents``
    # observable through this default — but tests typically patch both
    # seams independently via ``execute_seam_recorder``.
    from forge.build import git_operations as _git

    return await _git._execute_via_deepagents(
        command=command, cwd=cwd, timeout=timeout, env=env
    )


def _allowed_binaries_for_test() -> frozenset[str]:
    """Return the shared allowlist constant (identity-preserving).

    The seam test in ``TASK-IC-010 §Seam Tests`` asserts
    ``_allowed_binaries_for_test() is ALLOWED_BINARIES`` to prove there
    is no duplicate constant drifting between modules.
    """
    return ALLOWED_BINARIES


# --------------------------------------------------------------------------- #
# Output parsing.
# --------------------------------------------------------------------------- #


_SUMMARY_RE = re.compile(
    r"=+\s*"
    r"(?:(?P<failed>\d+)\s+failed[, ]+)?"
    r"(?:(?P<passed>\d+)\s+passed)?"
    r"(?:[, ]+(?P<skipped>\d+)\s+skipped)?"
    r".*?in\s+(?P<duration>[\d.]+)s",
    re.IGNORECASE,
)
"""Pytest summary line — tolerant of common combinations.

Examples matched:

* ``=== 12 passed in 4.20s ===``
* ``=== 1 failed, 3 passed in 1.00s ===``
* ``=== 2 failed in 0.50s ===``
"""

_FAILED_LINE_RE = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)
"""``FAILED tests/test_x.py::test_y`` — captures the test identifier."""


def _parse_pytest_output(
    stdout: str,
    *,
    exit_code: int,
    fallback_duration: float,
) -> tuple[bool, int, int, list[str], float]:
    """Parse pytest stdout into ``(passed, pass_count, fail_count,
    failing_tests, duration_seconds)``.

    Falls back to ``exit_code`` authority with ``pass_count == -1`` /
    ``fail_count == -1`` if the summary regex doesn't match (older
    pytest, plugin-altered formatting).
    """
    failing_tests = _FAILED_LINE_RE.findall(stdout)

    match = _SUMMARY_RE.search(stdout)
    if match is None:
        return (
            exit_code == 0,
            -1,
            -1,
            failing_tests,
            fallback_duration,
        )

    pass_count = int(match.group("passed") or 0)
    fail_count = int(match.group("failed") or 0)
    duration = float(match.group("duration"))
    passed = exit_code == 0 and fail_count == 0
    return passed, pass_count, fail_count, failing_tests, duration


# --------------------------------------------------------------------------- #
# Public surface.
# --------------------------------------------------------------------------- #


async def verify_tests(
    worktree_path: Path,
    test_command: str = "pytest",
    timeout_seconds: int = 600,
    output_tail_chars: int = 4000,
) -> TestVerificationResult:
    """Run ``test_command`` inside ``worktree_path`` and parse its output.

    Parameters
    ----------
    worktree_path:
        Absolute path to the per-build ephemeral worktree.
    test_command:
        Shell-style command string. The first token is validated
        against :data:`ALLOWED_BINARIES`; subsequent tokens are passed
        as argv elements (no shell). Default ``"pytest"`` runs the
        worktree's tests with project defaults.
    timeout_seconds:
        Max wall-clock seconds. On timeout, returns
        ``passed=False`` with
        ``failing_tests=[TIMEOUT_MARKER]``.
    output_tail_chars:
        Last N chars of stdout to keep on the result. Default 4000.

    Returns
    -------
    TestVerificationResult
        Always returned — never raises past the adapter boundary
        except for pre-flight validation (relative path or disallowed
        binary), which raise before any process spawns.
    """
    _validate_worktree(worktree_path)

    argv = shlex.split(test_command)
    if not argv:
        raise DisallowedBinaryError("empty test_command is not allowed")
    if argv[0] not in ALLOWED_BINARIES:
        raise DisallowedBinaryError(
            f"binary {argv[0]!r} is not on the allowlist "
            f"{sorted(ALLOWED_BINARIES)!r}; allowlist changes require an ADR"
        )

    # THE PROJECT'S TESTS ARE THE PROJECT'S OWN PROGRAM (13 October 2026), so
    # they are given an environment built from nothing — none of the
    # factory's credentials, a private HOME and TMPDIR in the repository's
    # own git directory.
    from forge.launch_environment import build_project_command_env

    env = build_project_command_env(worktree_path)
    stdout, _stderr, exit_code, duration, timed_out = await _execute_via_deepagents(
        command=argv,
        cwd=str(worktree_path),
        timeout=timeout_seconds,
        env=env,
    )

    output_tail = stdout[-output_tail_chars:] if output_tail_chars > 0 else ""

    if timed_out or exit_code == 124:
        return TestVerificationResult(
            passed=False,
            pass_count=0,
            fail_count=0,
            failing_tests=[TIMEOUT_MARKER],
            output_tail=output_tail,
            duration_seconds=duration,
        )

    passed, pass_count, fail_count, failing_tests, parsed_duration = (
        _parse_pytest_output(
            stdout,
            exit_code=exit_code,
            fallback_duration=duration,
        )
    )

    return TestVerificationResult(
        passed=passed,
        pass_count=pass_count,
        fail_count=fail_count,
        failing_tests=failing_tests,
        output_tail=output_tail,
        duration_seconds=parsed_duration,
    )

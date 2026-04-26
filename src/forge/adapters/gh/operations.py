"""gh adapter — thin wrapper for ``gh pr create`` over a subprocess primitive.

This module is the **boundary code** for the GuardKit Command Invocation
Engine feature (FEAT-FORGE-005, TASK-GCI-007). It wraps a single
``gh pr create`` invocation and converts its outcome into a
:class:`forge.adapters.git.models.PRResult`.

Per ``docs/design/contracts/API-subprocess.md`` §4 (git/gh adapter return
contract — never raises past the adapter boundary, ADR-ARCH-025) and
§4.1 (gh authentication via ``GH_TOKEN``):

- A missing or empty ``GH_TOKEN`` env var short-circuits to
  ``PRResult(status="failed", error_code="missing_credentials", ...)``
  **without invoking gh** — the BDD scenario "A pull-request creation
  without GitHub credentials returns a structured error" is the oracle
  for that contract. The token is re-read on every call (the env may
  legitimately change between builds in a long-running process; see
  TASK-GCI-007 implementation notes).
- The command is built as a list of separate tokens passed to the
  subprocess primitive. Backticks, dollar signs, and other shell
  metacharacters in the title/body are passed through literally — the
  BDD scenario "Shell metacharacters in subprocess arguments are passed
  as literal tokens" is the oracle for that contract.
- The PR URL is parsed from gh's stdout via a small regex matching
  ``https://github.com/<owner>/<repo>/pull/<n>`` over the trailing
  slash component. ``pr_url`` and ``pr_number`` populate
  :class:`PRResult` on success.
- Any non-zero exit, missing URL, or raised exception is converted into
  ``status="failed"`` — never propagated as an exception (ADR-ARCH-025).

The module-level :func:`_execute` helper is the seam tests patch in
order to mock gh without invoking the real binary or reaching the
network. Production code calls ``asyncio.create_subprocess_exec``;
that's the same primitive DeepAgents' ``execute`` ultimately wraps.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

from forge.adapters.git.models import PRResult

logger = logging.getLogger(__name__)

# Trailing-slash component carries the numeric PR id; matches gh's
# canonical URL shape for both github.com and ghe.com installs that
# follow the same path layout.
_PR_URL_PATTERN = re.compile(r"https://github\.com/[^/]+/[^/]+/pull/(\d+)")

# Stable error_code emitted when GH_TOKEN is unset/empty.
_MISSING_CREDENTIALS_CODE = "missing_credentials"
_MISSING_CREDENTIALS_STDERR = "GH_TOKEN not set in environment"


async def _execute(
    command: list[str],
    cwd: str,
    timeout: float | None = None,
) -> tuple[int, str, str]:
    """Run ``command`` as a subprocess and capture (exit_code, stdout, stderr).

    This is the seam tests patch — it mirrors the DeepAgents ``execute``
    contract (list of argv tokens, working directory, optional timeout)
    while remaining a real, callable Python function so the adapter is
    self-contained.

    Args:
        command: argv tokens to invoke. Must NOT be a shell string —
            metacharacters are passed literally.
        cwd: working directory for the subprocess (the build's worktree).
        timeout: optional wall-clock timeout in seconds. ``None`` = wait
            forever.

    Returns:
        Tuple of ``(exit_code, stdout, stderr)``. ``exit_code`` is ``-1``
        if the process terminated without setting a return code (rare;
        typically a kill).
    """
    proc = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise

    exit_code = proc.returncode if proc.returncode is not None else -1
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    return exit_code, stdout, stderr


def _build_command(
    title: str,
    body: str,
    base: str,
    draft: bool,
) -> list[str]:
    """Assemble the ``gh pr create`` argv as a list of separate tokens.

    Building the command in a dedicated helper keeps the
    shell-metacharacter contract obvious: every user-supplied value
    becomes its own list element.
    """
    command: list[str] = [
        "gh",
        "pr",
        "create",
        "--title",
        title,
        "--body",
        body,
        "--base",
        base,
    ]
    if draft:
        command.append("--draft")
    return command


def _missing_credentials_result() -> PRResult:
    """Construct the structured failure for an unset/empty ``GH_TOKEN``."""

    return PRResult(
        status="failed",
        error_code=_MISSING_CREDENTIALS_CODE,
        stderr=_MISSING_CREDENTIALS_STDERR,
    )


async def create_pr(
    worktree: Path,
    title: str,
    body: str,
    base: str = "main",
    draft: bool = False,
) -> PRResult:
    """Create a GitHub pull request via ``gh pr create``.

    Returns a structured :class:`PRResult` on every code path — never
    raises (ADR-ARCH-025).

    Args:
        worktree: Path to the build's worktree (becomes the subprocess
            ``cwd``).
        title: PR title. Shell metacharacters are passed literally as a
            single argv token.
        body: PR body. Shell metacharacters are passed literally as a
            single argv token.
        base: Base branch (default ``"main"``).
        draft: When ``True``, append ``--draft`` to the command.

    Returns:
        :class:`PRResult` — ``status="success"`` with ``pr_url`` and
        ``pr_number`` populated on the happy path; ``status="failed"``
        with ``stderr`` (and ``error_code="missing_credentials"`` when
        ``GH_TOKEN`` was unset/empty) otherwise.
    """
    # Pre-flight: re-read GH_TOKEN on every call (do not cache at import
    # time; the env may legitimately change between builds in a
    # long-running process). gh would otherwise fall through to its
    # interactive auth prompt with a confusing TTY error.
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        return _missing_credentials_result()

    command = _build_command(title=title, body=body, base=base, draft=draft)

    try:
        exit_code, stdout, stderr = await _execute(
            command=command,
            cwd=str(worktree),
        )
    except Exception as exc:  # noqa: BLE001 — adapter must never raise.
        # ADR-ARCH-025: convert any execute-layer exception (transport
        # error, subprocess spawn failure, timeout, etc.) into a
        # structured failure. Logging at WARN so operators can still see
        # it without the call site needing a try/except.
        logger.warning(
            "gh pr create raised %s: %s", type(exc).__name__, exc
        )
        return PRResult(
            status="failed",
            stderr=f"{type(exc).__name__}: {exc}",
        )

    if exit_code != 0:
        # Preserve gh's stderr verbatim (trimmed); fall back to stdout if
        # gh wrote its diagnostic there instead.
        message = stderr.strip() or stdout.strip() or None
        return PRResult(status="failed", stderr=message)

    match = _PR_URL_PATTERN.search(stdout.strip())
    if match is None:
        # gh exited 0 but emitted no recognisable PR URL — surface this
        # as ``failed`` rather than fabricating an empty success.
        return PRResult(
            status="failed",
            stderr=(
                "gh pr create exited 0 but no PR URL was found in stdout: "
                f"{stdout.strip()!r}"
            ),
        )

    pr_url = match.group(0)
    pr_number = int(match.group(1))
    return PRResult(status="success", pr_url=pr_url, pr_number=pr_number)


__all__ = ["create_pr"]

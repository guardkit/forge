"""Git/gh operations via the DeepAgents ``execute`` seam (TASK-IC-010).

Three async operations needed by the build state machine:

* :func:`create_branch` — ``git checkout -b <branch>``
* :func:`commit_changes` — ``git add -A`` + ``git commit -m <message>``
* :func:`create_pull_request` — ``gh pr create --title --body --base``

There is no push here (14 October 2026): ``push_branch`` was called by
nothing in production and pushed to whatever the clone's ``origin`` named,
so it was removed. A git command reaching the default seam is run through
:func:`forge.project_git.run_git` (a fixed environment, no hooks or filters
from the project's settings, and a fixed sentence instead of git's own
error text); ``gh`` and ``pytest`` are started as before.

Constitutional constraints
--------------------------

* **Allowlist (single source of truth).** ``ALLOWED_BINARIES`` is the one
  named constant gating which binaries the build-time subprocess seam
  may invoke. ``forge.build.test_verification`` imports it; nothing else
  should redefine it. Any change to the set requires an ADR + an
  allowlist-change review (TASK-IC-010 Risk-5). The allowlist check
  uses :class:`Path` basename so an absolute path like
  ``/usr/local/bin/git`` is accepted while ``/usr/bin/rm`` is refused.
* **No direct subprocess.** Every operation flows through the
  module-level ``_execute_via_deepagents`` seam. Tests patch this name
  to assert on the ``(command, cwd)`` tuple without spawning a real
  process. Production replaces the default with a DeepAgents
  ``execute``-tool-backed callable.
* **Working-directory invariant.** ``worktree_path`` must be
  :class:`Path`-absolute. Relative paths raise :class:`ValueError`
  *before* the seam is touched so a misconfigured caller cannot leak
  into the wrong cwd (covered by ``@security
  security-working-directory-allowlist`` in the BDD scenarios).
* **Env-only credentials.** ``create_pull_request`` reads ``GH_TOKEN`` /
  ``GITHUB_TOKEN`` from the process environment only — never from
  ``forge.yaml``. Missing or empty creds, or a gh auth-failure stderr,
  produce a graceful ``None`` return so the caller can record
  ``cred_missing=True`` on the :class:`SessionOutcome` without the
  build crashing. Any other non-zero exit surfaces as
  :class:`RuntimeError` so genuine failures don't go silent.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

from forge.project_git import run_git_async

ALLOWED_BINARIES: frozenset[str] = frozenset({"git", "gh", "pytest"})
"""Binaries the build subprocess seam may invoke.

Shared with :mod:`forge.build.test_verification` (TASK-IC-009 imports
this constant rather than redefining it). Adding a binary requires an
ADR + an allowlist-change review per TASK-IC-010 Risk-5.
"""


class DisallowedBinaryError(ValueError):
    """Raised when a caller tries to invoke a binary outside the allowlist."""


# --------------------------------------------------------------------------- #
# Subprocess seam — patched by tests, defaults to asyncio.create_subprocess_exec.
# --------------------------------------------------------------------------- #


async def _execute_via_deepagents(
    *,
    command: list[str],
    cwd: str,
    timeout: int,
    env: dict[str, str],
) -> tuple[str, str, int, float, bool]:
    """Default seam — async subprocess executor (no shell).

    ``env`` is REQUIRED (13 October 2026): the caller decides exactly what
    the child is given. The gh operation below passes the factory's own
    environment by name; a git command is handed to the git helper, which
    gives git its own fixed environment and ignores ``env`` (14 October
    2026); the build's test run
    (:func:`forge.build.test_verification.verify_tests`) passes a project
    program's, built from nothing, so it carries none of the factory's
    credentials.

    The kw-only signature matches the recorder in
    ``tests/bdd/conftest.py::execute_seam_recorder``. Production
    deployments replace this attribute with a DeepAgents
    ``execute``-tool-backed callable; tests replace it with a
    list-recording fake.

    Returns
    -------
    tuple
        ``(stdout, stderr, exit_code, duration_seconds, timed_out)``.
        On timeout, ``timed_out=True`` and ``exit_code`` reflects the
        terminating signal (124, the GNU ``timeout`` convention).
    """
    loop = asyncio.get_event_loop()
    start = loop.time()

    if command and Path(command[0]).name == "git":
        # git goes through the one local git helper (14 October 2026). The
        # helper gives git its own fixed environment, so ``env`` is not
        # passed on, and a failure's ``stderr`` is its fixed sentence.
        try:
            done = await run_git_async(
                ["git", *command[1:]],
                cwd=cwd,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            sentence = exc.stderr if isinstance(exc.stderr, str) else ""
            return "", sentence, 124, loop.time() - start, True
        return (
            done.stdout or "",
            done.stderr or "",
            done.returncode or 0,
            loop.time() - start,
            False,
        )

    proc = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
        timed_out = False
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        stdout_bytes, stderr_bytes = b"", b""
        timed_out = True

    duration = loop.time() - start
    exit_code = 124 if timed_out else (proc.returncode or 0)
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    return stdout, stderr, exit_code, duration, timed_out


# --------------------------------------------------------------------------- #
# Validation helpers.
# --------------------------------------------------------------------------- #


def _validate_worktree(worktree_path: Path) -> None:
    """Reject relative worktree paths before any process spawns.

    The execute-tool's working-directory allowlist also enforces this
    invariant downstream, but rejecting at the adapter boundary keeps
    the contract explicit and gives BDD scenarios a clean
    ``ValueError`` to assert against.
    """
    if not worktree_path.is_absolute():
        raise ValueError(
            f"worktree_path must be absolute (got {worktree_path!r}); "
            "cwd-allowlist requires absolute paths"
        )


def _binary_basename(command: list[str]) -> str:
    """Return the basename of ``command[0]`` for allowlist comparison.

    A caller that wires up ``["/usr/local/bin/git", "status"]`` is
    legitimate — the real-world ``git`` binary often lives at an
    absolute path. The allowlist keys on the basename so the path
    prefix is irrelevant; conversely, ``["/usr/bin/rm", ...]`` is
    rejected because the basename is not on the allowlist.
    """
    return Path(command[0]).name


def _validate_binary(command: list[str]) -> None:
    """Reject command lists whose binary basename is not on the allowlist."""
    if not command:
        raise DisallowedBinaryError("empty command is not allowed")
    binary = _binary_basename(command)
    if binary not in ALLOWED_BINARIES:
        raise DisallowedBinaryError(
            f"binary {command[0]!r} (basename {binary!r}) is not on the "
            f"allowlist {sorted(ALLOWED_BINARIES)!r}; allowlist changes "
            "require an ADR"
        )


async def _run_via_execute(
    worktree_path: Path,
    command: list[str],
    *,
    timeout: int = 120,
) -> tuple[str, str, int, float, bool]:
    """Validate then dispatch through the module-level seam.

    Public-private — the underscore prefix marks this as an internal
    helper, but TASK-IC-010 unit tests address it directly to exercise
    the allowlist guard without going through one of the four public
    operations. Tests patch ``_execute_via_deepagents`` so this helper
    never spawns a real process.
    """
    _validate_worktree(worktree_path)
    _validate_binary(command)
    # Read the seam through the module so monkeypatching
    # ``_execute_via_deepagents`` at module scope is visible here.
    is_git = _binary_basename(command) == "git"
    return await _execute_via_deepagents(
        command=command,
        cwd=str(worktree_path),
        timeout=timeout,
        # gh is the factory's own program and reads its token from this
        # process's environment, so it is given it, by name. git is given
        # nothing from this process: the git helper builds its environment.
        env={} if is_git else dict(os.environ),
    )


def _check_exit(
    operation: str,
    command: list[str],
    exit_code: int,
    stderr: str,
) -> None:
    """Surface non-zero exits as :class:`RuntimeError` with diagnostic context.

    Used by the three public operations to convert seam-level failure
    into a clean exception that the build state machine can log without
    digging through subprocess internals. The PR-creation path does not
    use this helper directly because it differentiates auth-failure
    stderr (soft None) from genuine failure (hard raise).
    """
    if exit_code == 0:
        return
    rendered_cmd = " ".join(command)
    raise RuntimeError(
        f"{operation} failed (exit {exit_code}): {rendered_cmd}\n"
        f"stderr: {stderr.strip()}"
    )


# --------------------------------------------------------------------------- #
# Public surface.
# --------------------------------------------------------------------------- #


async def create_branch(worktree_path: Path, branch_name: str) -> None:
    """Create and check out ``branch_name`` inside ``worktree_path``."""
    if not branch_name:
        raise ValueError("branch_name must be a non-empty string")
    command = ["git", "checkout", "-b", branch_name]
    _stdout, stderr, exit_code, _duration, _timed_out = await _run_via_execute(
        worktree_path, command
    )
    _check_exit("git checkout", command, exit_code, stderr)


async def commit_changes(worktree_path: Path, message: str) -> None:
    """Stage all and commit with ``message`` inside ``worktree_path``.

    Two seam calls — ``git add -A`` then ``git commit -m <message>``.
    The commit message is passed as a separate argv token (no shell
    quoting) so metacharacters are preserved verbatim.
    """
    if not message:
        raise ValueError("commit message must be a non-empty string")

    add_cmd = ["git", "add", "-A"]
    _stdout, stderr, exit_code, _duration, _timed_out = await _run_via_execute(
        worktree_path, add_cmd
    )
    _check_exit("git add", add_cmd, exit_code, stderr)

    commit_cmd = ["git", "commit", "-m", message]
    _stdout, stderr, exit_code, _duration, _timed_out = await _run_via_execute(
        worktree_path, commit_cmd
    )
    _check_exit("git commit", commit_cmd, exit_code, stderr)


def _has_credentials() -> bool:
    """Return True iff a non-empty ``GH_TOKEN`` or ``GITHUB_TOKEN`` is set.

    Empty-string env vars are treated as missing — they're a common CI
    misconfiguration and the gh binary itself rejects them, so
    short-circuiting here keeps the soft-fail contract explicit.
    """
    for var in ("GH_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(var)
        if value:
            return True
    return False


def _is_auth_failure(stderr: str) -> bool:
    r"""Heuristic: gh auth-failure messages contain ``authentication``.

    The current gh CLI emits things like ``"error: authentication
    required, run \`gh auth login\`"`` and ``"HTTP 401: Bad credentials"``.
    Anything else (e.g. ``"remote: base branch 'develop' not found"``)
    is a genuine failure and should not be silenced.
    """
    lowered = stderr.lower()
    return any(
        marker in lowered
        for marker in (
            "authentication",
            "auth login",
            "bad credentials",
            "401",
            "403",
        )
    )


def _parse_pr_url(stdout: str) -> str | None:
    """Return the last URL-shaped line from ``gh pr create`` stdout.

    A pathological gh run might exit zero without printing a URL line
    — the caller treats that as ``None`` rather than mis-recording the
    last "Done." line as a PR URL.
    """
    for line in reversed(stdout.splitlines()):
        candidate = line.strip()
        if candidate.startswith(("http://", "https://")):
            return candidate
    return None


async def create_pull_request(
    worktree_path: Path,
    title: str,
    body: str,
    base: str = "main",
) -> str | None:
    """Open a PR via ``gh pr create``; return the URL or ``None``.

    Returns ``None`` for the soft-failure cases — missing/empty
    credentials and gh auth-failure stderr — so the caller can record
    ``cred_missing=True`` on the :class:`SessionOutcome` and continue.
    Any other non-zero exit raises :class:`RuntimeError` so genuine
    failures don't go silent.

    The PR URL is the last ``http(s)://...`` line on stdout; if no
    URL-shaped line is present (pathological gh output), returns
    ``None``.
    """
    if not title:
        raise ValueError("PR title must be a non-empty string")

    if not _has_credentials():
        return None

    command = [
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
    stdout, stderr, exit_code, _duration, _timed_out = await _run_via_execute(
        worktree_path, command
    )
    if exit_code != 0:
        if _is_auth_failure(stderr):
            return None
        _check_exit("gh pr create", command, exit_code, stderr)

    return _parse_pr_url(stdout)

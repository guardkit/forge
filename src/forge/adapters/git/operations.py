"""Git adapter operations — TASK-GCI-006.

Thin async wrappers around the project ``execute`` subprocess primitive
(see ``docs/design/contracts/API-subprocess.md`` §4) that funnel every
``git`` invocation into a structured :class:`GitOpResult` and never
raise past the adapter boundary (ADR-ARCH-025).

Four operations:

- :func:`prepare_worktree` — ``git worktree add`` for the build's path.
- :func:`commit_all` — ``git add -A`` + ``git commit -m`` + ``git rev-parse HEAD``.
- :func:`push` — ``git push origin <branch>``.
- :func:`cleanup_worktree` — ``git worktree remove --force``; **best-effort**:
  failure is logged and returned as ``status="failed"`` but never raises,
  so callers (the build state machine) can treat it as a warning rather
  than a blocker (Scenario "A failed worktree cleanup is logged but does
  not prevent build completion").

Design notes
------------

* **List-token safety (Scenario "Shell metacharacters in subprocess
  arguments are passed as literal tokens").**
  All shell arguments — including commit messages, branch names, and
  worktree paths — are passed as separate tokens in the ``command``
  list. There is no ``shell=True`` and no f-string concatenation into a
  single shell line. A commit message containing ``;`` or ``&&`` is
  passed verbatim as a single ``argv`` element to git.

* **No direct ``subprocess.run`` / ``os.system``.** Every git call goes
  through the injected ``execute`` callable. The default
  :func:`_default_execute` uses :func:`asyncio.create_subprocess_exec`
  (the list-token, no-shell equivalent). Production deployments swap in
  a DeepAgents ``execute``-tool-backed implementation; tests inject a
  fake.

* **Adapter boundary contract (ADR-ARCH-025).** Each function is
  wrapped in ``try/except Exception`` and converts unexpected exceptions
  into ``GitOpResult(status="failed", stderr=f"{type(exc).__name__}: {exc}")``.
  Callers never see a raised exception from this module.

* **PR creation lives in TASK-GCI-007** (``forge.adapters.gh``) so gh
  credentials stay separate from the git surface.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Sequence

from forge.adapters.git.models import GitOpResult

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Subprocess primitive — injectable for tests, default for production.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ExecuteResult:
    """Lightweight result returned by an injected ``execute`` callable.

    The shape is intentionally minimal — only the three fields the
    adapter needs to construct a :class:`GitOpResult`.
    """

    exit_code: int
    stdout: str
    stderr: str


ExecuteCallable = Callable[..., Awaitable[ExecuteResult]]
"""Async callable signature for the subprocess primitive.

Concrete signature (kw-only):

    async def execute(
        *, command: Sequence[str], cwd: str | None = None,
        timeout: float | None = None,
    ) -> ExecuteResult: ...

The default implementation is :func:`_default_execute`. Tests inject a
fake via the keyword-only ``execute`` parameter on each operation.
"""


async def _default_execute(
    *,
    command: Sequence[str],
    cwd: str | None = None,
    timeout: float | None = None,
) -> ExecuteResult:
    """Default async subprocess executor.

    Uses :func:`asyncio.create_subprocess_exec` — the no-shell,
    list-token equivalent of ``subprocess.run``. This is the production
    fallback when no ``execute`` callable is injected.

    Per ADR-ARCH-023 + ADR-ARCH-025, real deployments wire this through
    the DeepAgents ``execute`` tool / sandbox backend so that
    permissions are enforced framework-side. The default here mirrors
    that contract (list tokens, no shell) so unit tests and seam tests
    behave identically to production.
    """
    proc = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if timeout is None:
        stdout_b, stderr_b = await proc.communicate()
    else:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return ExecuteResult(
        exit_code=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout_b.decode("utf-8", errors="replace"),
        stderr=stderr_b.decode("utf-8", errors="replace"),
    )


# --------------------------------------------------------------------------- #
# Defaults & helpers.
# --------------------------------------------------------------------------- #


_DEFAULT_BUILDS_ROOT = Path("/var/forge/builds")
"""Allowlisted ephemeral worktree root (ADR-ARCH-028)."""


def _failure_stderr(stderr: str | None, stdout: str | None) -> str | None:
    """Pick the most useful failure-message text without dropping signal.

    Some git invocations emit their diagnostic only on stdout (e.g.
    ``git commit`` with no staged changes prints "nothing to commit" on
    stdout, exit code 1). We prefer stderr but fall back to stdout so
    callers can branch on the message.
    """
    if stderr and stderr.strip():
        return stderr
    if stdout and stdout.strip():
        return stdout
    return None


def _exception_failure(operation: str, exc: BaseException) -> GitOpResult:
    """Convert an unexpected exception into a failure GitOpResult.

    Keeps the adapter boundary contract (ADR-ARCH-025): the adapter
    never raises past its own surface.
    """
    return GitOpResult(
        status="failed",
        operation=operation,
        stderr=f"{type(exc).__name__}: {exc}",
        exit_code=-1,
    )


# --------------------------------------------------------------------------- #
# Operations.
# --------------------------------------------------------------------------- #


async def prepare_worktree(
    build_id: str,
    repo: Path,
    branch: str,
    *,
    execute: ExecuteCallable = _default_execute,
    builds_root: Path = _DEFAULT_BUILDS_ROOT,
) -> GitOpResult:
    """Create a build's ephemeral worktree (ADR-ARCH-028).

    Runs ``git worktree add <builds_root>/<build_id> <branch>`` from
    inside the source ``repo`` (the worktree path does not exist yet,
    so cwd cannot be the worktree itself — this is the one operation
    where cwd is the source repo rather than the build worktree).

    On success returns ``GitOpResult.worktree_path`` populated with the
    absolute path of the created worktree. On any non-zero exit or
    raised exception returns ``status="failed"`` with stderr preserved.
    """
    operation = "prepare_worktree"
    worktree_path = builds_root / build_id
    try:
        result = await execute(
            command=["git", "worktree", "add", str(worktree_path), branch],
            cwd=str(repo),
        )
        if result.exit_code == 0:
            return GitOpResult(
                status="success",
                operation=operation,
                worktree_path=str(worktree_path),
                exit_code=0,
            )
        return GitOpResult(
            status="failed",
            operation=operation,
            stderr=_failure_stderr(result.stderr, result.stdout),
            exit_code=result.exit_code,
        )
    except Exception as exc:  # noqa: BLE001 — adapter boundary, ADR-ARCH-025
        logger.exception(
            "prepare_worktree failed (build_id=%s, branch=%s)", build_id, branch
        )
        return _exception_failure(operation, exc)


async def commit_all(
    worktree: Path,
    message: str,
    *,
    execute: ExecuteCallable = _default_execute,
) -> GitOpResult:
    """Stage all changes and commit them inside ``worktree``.

    Issues three separate, list-token git invocations:

    1. ``git add -A``
    2. ``git commit -m <message>`` — the message is passed as a single
       argv token, so shell metacharacters are literal.
    3. ``git rev-parse HEAD`` — to retrieve the commit SHA.

    If any step exits non-zero, the function short-circuits and returns
    ``status="failed"`` with that step's captured stderr (or stdout —
    "nothing to commit" lands on stdout). Note that "no staged changes"
    is a real, expected failure path: the caller decides whether to
    treat it as success-with-no-push or escalate.
    """
    operation = "commit_all"
    cwd = str(worktree)
    try:
        add_res = await execute(command=["git", "add", "-A"], cwd=cwd)
        if add_res.exit_code != 0:
            return GitOpResult(
                status="failed",
                operation=operation,
                stderr=_failure_stderr(add_res.stderr, add_res.stdout),
                exit_code=add_res.exit_code,
            )

        commit_res = await execute(command=["git", "commit", "-m", message], cwd=cwd)
        if commit_res.exit_code != 0:
            return GitOpResult(
                status="failed",
                operation=operation,
                stderr=_failure_stderr(commit_res.stderr, commit_res.stdout),
                exit_code=commit_res.exit_code,
            )

        sha_res = await execute(command=["git", "rev-parse", "HEAD"], cwd=cwd)
        if sha_res.exit_code != 0:
            return GitOpResult(
                status="failed",
                operation=operation,
                stderr=_failure_stderr(sha_res.stderr, sha_res.stdout),
                exit_code=sha_res.exit_code,
            )
        sha = sha_res.stdout.strip() or None
        return GitOpResult(
            status="success",
            operation=operation,
            sha=sha,
            exit_code=0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("commit_all failed (worktree=%s)", worktree)
        return _exception_failure(operation, exc)


async def push(
    worktree: Path,
    remote_branch: str,
    *,
    execute: ExecuteCallable = _default_execute,
) -> GitOpResult:
    """Push HEAD to ``origin/<remote_branch>`` from ``worktree``.

    Runs ``git push origin <remote_branch>``. Non-zero exit becomes
    ``status="failed"`` with stderr preserved (e.g. ``! [rejected]``,
    ``error: failed to push some refs``). On success returns
    ``status="success"``. The caller may follow up with
    :func:`commit_all`'s SHA if the canonical pushed-commit SHA is
    needed.
    """
    operation = "push"
    try:
        result = await execute(
            command=["git", "push", "origin", remote_branch],
            cwd=str(worktree),
        )
        if result.exit_code == 0:
            return GitOpResult(
                status="success",
                operation=operation,
                exit_code=0,
            )
        return GitOpResult(
            status="failed",
            operation=operation,
            stderr=_failure_stderr(result.stderr, result.stdout),
            exit_code=result.exit_code,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("push failed (branch=%s)", remote_branch)
        return _exception_failure(operation, exc)


async def cleanup_worktree(
    build_id: str,
    worktree: Path,
    *,
    execute: ExecuteCallable = _default_execute,
) -> GitOpResult:
    """Remove the build's worktree — **best-effort**.

    Runs ``git worktree remove <worktree> --force`` from inside the
    worktree's parent (``/var/forge/builds``) so the command itself
    succeeds even if the path is the current working directory. A
    non-zero exit is logged at WARNING and returned as
    ``status="failed"`` *but the function still returns normally* — the
    caller (build state machine) treats this as a warning, not a
    blocker (Scenario "A failed worktree cleanup is logged but does not
    prevent build completion").

    Exceptions from the execute primitive are equally trapped and
    converted to a failure result (ADR-ARCH-025). This guarantees the
    state machine can drive a build to a terminal state regardless of
    cleanup success.
    """
    operation = "cleanup_worktree"
    try:
        # Run from the worktree's parent so we don't rely on the
        # to-be-removed path being a valid cwd.
        cwd = str(worktree.parent)
        result = await execute(
            command=["git", "worktree", "remove", str(worktree), "--force"],
            cwd=cwd,
        )
        if result.exit_code == 0:
            return GitOpResult(
                status="success",
                operation=operation,
                exit_code=0,
            )
        # Best-effort: log + return failed without raising.
        stderr = _failure_stderr(result.stderr, result.stdout)
        logger.warning(
            "cleanup_worktree non-zero exit (best-effort, build_id=%s, "
            "worktree=%s, exit=%d): %s",
            build_id,
            worktree,
            result.exit_code,
            stderr,
        )
        return GitOpResult(
            status="failed",
            operation=operation,
            stderr=stderr,
            exit_code=result.exit_code,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "cleanup_worktree exception (best-effort, build_id=%s, worktree=%s)",
            build_id,
            worktree,
        )
        return _exception_failure(operation, exc)


__all__ = [
    "ExecuteCallable",
    "ExecuteResult",
    "cleanup_worktree",
    "commit_all",
    "prepare_worktree",
    "push",
]

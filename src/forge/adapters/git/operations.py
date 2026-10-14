"""Git adapter operations — TASK-GCI-006.

Thin async wrappers around the project ``execute`` subprocess primitive
(see ``docs/design/contracts/API-subprocess.md`` §4) that funnel every
``git`` invocation into a structured :class:`GitOpResult` and never
raise past the adapter boundary (ADR-ARCH-025).

Three operations:

- :func:`prepare_worktree` — ``git worktree add`` for the build's path,
  optionally ``-b <branch> <path> <base>`` when the branch must be
  CREATED (the fix journey's per-build branch, conductor activation §1).
- :func:`commit_all` — ``git add -A`` + ``git commit -m`` + ``git rev-parse HEAD``.
- :func:`cleanup_worktree` — ``git worktree remove --force``; **best-effort**:
  failure is logged and returned as ``status="failed"`` but never raises,
  so callers (the build state machine) can treat it as a warning rather
  than a blocker (Scenario "A failed worktree cleanup is logged but does
  not prevent build completion").

There is no push here (14 October 2026). The ``push`` operation that used
to sit here was called by nothing in production, and it pushed to whatever
address the clone's own ``origin`` named, so it was removed rather than
kept as an unused way to reach another repository.

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
  :func:`_default_execute` hands the command to
  :func:`forge.project_git.run_git` (14 October 2026), the one way Forge
  runs git on a repository: a fixed environment with no factory secret,
  no hooks or filters from the project's settings, and git's own error
  text replaced by a fixed sentence. Tests inject a fake.

* **Git's own words never leave this module.** On failure
  ``GitOpResult.stderr`` carries the helper's fixed sentence (for example
  "git commit stopped with exit code 1."), never git's text and never the
  failed command's stdout.

* **Adapter boundary contract (ADR-ARCH-025).** Each function is
  wrapped in ``try/except Exception`` and converts unexpected exceptions
  into ``GitOpResult(status="failed", stderr=f"{type(exc).__name__}: {exc}")``.
  Callers never see a raised exception from this module.

* **PR creation lives in TASK-GCI-007** (``forge.adapters.gh``) so gh
  credentials stay separate from the git surface.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Sequence

from forge.adapters.git.models import GitOpResult
from forge.project_git import run_git_async

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
    """Default executor: run one git command through the local git helper.

    ``command`` is the git command line, starting with ``"git"``. Since 14
    October 2026 it is handed to :func:`forge.project_git.run_git` (off the
    event loop), so git runs with a fixed environment instead of Forge's,
    the project's own settings cannot make it start another program, and
    on failure ``stderr`` is the helper's fixed sentence ("git worktree add
    stopped with exit code 128."), never git's own text. stdout is returned
    whole, for the callers that read data from it on success.

    Only git is run here; any other program is refused with a plain
    sentence rather than started.

    **Timeout discipline.** When ``timeout`` is not ``None`` and git
    overruns it, the helper stops and reaps the child. The overrun is
    surfaced as a *normal* :class:`ExecuteResult` with ``exit_code=-1`` and
    a ``stderr`` that says which git command did not finish in time, so the
    caller sees a loud, retryable failure rather than an unbounded hang.
    """
    argv = [str(token) for token in command]
    if not argv or argv[0] != "git":
        return ExecuteResult(
            exit_code=-1,
            stdout="",
            stderr="the git adapter runs only git commands; nothing was started.",
        )
    try:
        done = await run_git_async(
            argv, cwd=cwd, timeout=timeout, encoding="utf-8", errors="replace"
        )
    except subprocess.TimeoutExpired as exc:
        # The helper's timeout text names only the command and the limit.
        sentence = exc.stderr if isinstance(exc.stderr, str) else ""
        return ExecuteResult(
            exit_code=-1,
            stdout="",
            stderr=sentence or f"git did not finish within {timeout} seconds.",
        )
    return ExecuteResult(
        exit_code=done.returncode if done.returncode is not None else -1,
        stdout=done.stdout or "",
        stderr=done.stderr or "",
    )


# --------------------------------------------------------------------------- #
# Defaults & helpers.
# --------------------------------------------------------------------------- #


_DEFAULT_BUILDS_ROOT = Path("/var/forge/builds")
"""Allowlisted ephemeral worktree root (ADR-ARCH-028)."""


def _failure_stderr(stderr: str | None, stdout: str | None = None) -> str | None:
    """The failure text a failed git step reports: its ``stderr`` only.

    Since 14 October 2026 the default executor's ``stderr`` is the git
    helper's fixed sentence. A failed command's stdout is never used as a
    fallback any more: it can carry git's own words ("nothing to commit"
    and the like), which must not reach a log, a record or a card. Callers
    that need to know whether there is anything to commit ask git directly
    (``git diff --cached --quiet``) instead of reading its text.
    ``stdout`` is accepted and ignored so existing callers keep working.
    """
    del stdout  # deliberately unused: see above
    if stderr and stderr.strip():
        return stderr
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
    create_branch: bool = False,
    base_ref: str | None = None,
) -> GitOpResult:
    """Create a build's ephemeral worktree (ADR-ARCH-028).

    Runs ``git worktree add <builds_root>/<build_id> <branch>`` from
    inside the source ``repo`` (the worktree path does not exist yet,
    so cwd cannot be the worktree itself — this is the one operation
    where cwd is the source repo rather than the build worktree).

    On success returns ``GitOpResult.worktree_path`` populated with the
    absolute path of the created worktree. On any non-zero exit or
    raised exception returns ``status="failed"`` with the failure sentence.

    Creating the branch (conductor activation §1)
    ---------------------------------------------

    As originally built this operation could only check out a branch that
    ALREADY EXISTS: ``git worktree add <path> <branch>`` hard-fails
    (exit 128, "invalid reference") on a name git has never seen. The fix
    journey needs the opposite — a NEW per-journey branch cut from the
    trunk — so the operation grew two explicit parameters:

    * ``create_branch=True`` emits ``-b <branch>``, i.e.
      ``git worktree add -b <branch> <path> [<base_ref>]``.
    * ``base_ref`` names the commit-ish the new branch is cut from
      (``"main"`` for the fix journey). Omitted, git cuts from ``HEAD``
      — legal, but the caller is then trusting whatever the source
      checkout happens to have checked out, so production passes it.

    ``base_ref`` WITHOUT ``create_branch`` is a caller bug, not a git
    invocation: ``git worktree add <path> <branch> <base>`` is not a
    valid form, and silently dropping the base would materialise a tree
    off the wrong commit. It returns ``status="failed"`` (adapter
    boundary — never raises) rather than guessing.

    Args:
        build_id: The build whose worktree this is; the leaf directory
            name under ``builds_root``.
        repo: The source checkout the worktree is registered against.
        branch: Branch to check out, or — with ``create_branch`` — the
            branch to CREATE.
        execute: Injected subprocess primitive.
        builds_root: Parent directory of the per-build worktree.
        create_branch: Emit ``-b <branch>`` so git creates the branch.
        base_ref: Commit-ish the created branch is cut from. Requires
            ``create_branch``.
    """
    operation = "prepare_worktree"
    worktree_path = builds_root / build_id
    if base_ref is not None and not create_branch:
        return GitOpResult(
            status="failed",
            operation=operation,
            stderr=(
                f"prepare_worktree: base_ref={base_ref!r} was given without "
                "create_branch=True. 'git worktree add <path> <branch> <base>' "
                "is not a valid invocation, and dropping the base would "
                "materialise the tree off the wrong commit — refusing rather "
                "than guessing."
            ),
            exit_code=-1,
        )
    if create_branch:
        command = ["git", "worktree", "add", "-b", branch, str(worktree_path)]
        if base_ref is not None:
            command.append(base_ref)
    else:
        command = ["git", "worktree", "add", str(worktree_path), branch]
    try:
        result = await execute(command=command, cwd=str(repo))
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
    ``status="failed"`` with that step's ``stderr`` (with the default
    executor, the git helper's fixed sentence). "No staged changes" makes
    the commit step fail; a caller that wants to treat that as success asks
    ``git diff --cached --quiet`` before calling this, rather than reading
    git's text afterwards.
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
]

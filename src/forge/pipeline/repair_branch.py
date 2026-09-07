"""The repair branch: a task file the review leg can find, committed where the build's worktree sees it.

Why this exists
===============

Journey one refused in four seconds. guardkit's review leg is id-form only:
it loads its subject from ``tasks/backlog/**/<TASK-id>*.md`` (or
``in_progress``, ``design_approved``, ``in_review``, ``blocked``) in the
build's worktree, and that worktree is a branch the conductor cuts from the
build's branch (:func:`forge.cli._conductor_worktree.prepare_journey_worktree`)
— so only COMMITTED files on the branch are visible to the legs. The repair
admission used to write one uncommitted YAML into the shared checkout, which
the worktree never saw.

So a repair now rides a branch of its own, ``repair/<task id>``, cut from the
build's target branch and carrying the task file and the YAML as committed
files. The git work happens in a temporary worktree at
``<checkout>/.forge/repair-<task id>/``; ``.forge/`` is written into the
checkout's ``.git/info/exclude`` once, so the shared checkout never looks
dirty for it. The shared checkout's own working tree and index are never
touched, and the temporary worktree is removed whichever way the work ends.

The rules (rewrite-on-refusal spec 2026-09-06, Part L, rules 48 and 50):

- **Idempotent.** A second materialisation of the same repair reuses the
  branch; identical files mean no second commit.
- **Clean.** A failure to write or commit leaves behind no branch and no
  worktree that this call made, and raises :class:`RepairBranchError` with
  a plain sentence.
- **Small and synchronous.** Every git call is a short subprocess; the
  admission runs the whole thing on a worker thread.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

logger = logging.getLogger(__name__)

#: The branch a repair rides: ``repair/<task id>``.
REPAIR_BRANCH_PREFIX: str = "repair/"

#: The directory under a checkout that forge owns for its own scratch trees
#: (the conductor's worktrees already live under it).
FORGE_DIRNAME: str = ".forge"

#: The line written once into the checkout's ``.git/info/exclude``.
FORGE_EXCLUDE_LINE: str = ".forge/"

#: The temporary worktree's name under ``.forge/``: ``repair-<task id>``.
REPAIR_WORKTREE_PREFIX: str = "repair-"

#: Where guardkit's loader looks for a task file, in its order.
TASK_SEARCH_DIRS: tuple[str, ...] = (
    "backlog",
    "in_progress",
    "design_approved",
    "in_review",
    "blocked",
)

#: The identity a commit is made under when the checkout has none configured
#: (inside a container, say). The host's own identity wins when it is set.
FALLBACK_COMMIT_IDENTITY: tuple[str, str] = ("forge", "forge@localhost")

#: How long any one git call may take.
GIT_TIMEOUT_SECONDS: int = 60


class RepairBranchError(RuntimeError):
    """The repair branch could not be cut, written or committed.

    The message is one plain sentence a person can read on the queue.
    """


@dataclass(frozen=True, slots=True)
class RepairBranchResult:
    """What materialising the branch did."""

    branch: str
    commit: str
    created_branch: bool
    committed: bool
    files: tuple[str, ...]


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


def repair_branch_name(task_id: str) -> str:
    """``repair/<task id>``."""
    return f"{REPAIR_BRANCH_PREFIX}{task_id}"


def is_repair_branch(branch: str | None) -> bool:
    """Whether ``branch`` is one a repair rides."""
    return bool(branch) and str(branch).startswith(REPAIR_BRANCH_PREFIX)


def repair_worktree_path(repo_root: Path | str, task_id: str) -> Path:
    """``<checkout>/.forge/repair-<task id>`` — the temporary worktree."""
    return Path(repo_root) / FORGE_DIRNAME / f"{REPAIR_WORKTREE_PREFIX}{task_id}"


# ---------------------------------------------------------------------------
# Reading the checkout (plumbing only: never the working tree or the index)
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one git command and return it, never raising."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(
            ["git", *args],
            returncode=-1,
            stdout="",
            stderr=f"{type(exc).__name__}: {exc}",
        )


def _last_line(proc: subprocess.CompletedProcess[str]) -> str:
    text = (proc.stderr or proc.stdout or "").strip()
    return text.splitlines()[-1] if text else f"git exited {proc.returncode}"


def is_git_checkout(repo_root: Path | str) -> bool:
    """Whether ``repo_root`` is a directory git recognises as a checkout."""
    repo = Path(repo_root)
    if not repo.is_dir():
        return False
    return _git(repo, "rev-parse", "--git-dir").returncode == 0


def branch_exists(repo_root: Path | str, branch: str) -> bool:
    """Whether ``branch`` is a LOCAL branch of the checkout (the runner's own rule)."""
    proc = _git(
        Path(repo_root), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"
    )
    return proc.returncode == 0


def repair_task_ids_on_branches(repo_root: Path | str) -> set[str]:
    """Every task id that already has a ``repair/<task id>`` branch."""
    proc = _git(
        Path(repo_root),
        "for-each-ref",
        "--format=%(refname:short)",
        f"refs/heads/{REPAIR_BRANCH_PREFIX}",
    )
    if proc.returncode != 0:
        return set()
    found: set[str] = set()
    for line in proc.stdout.splitlines():
        name = line.strip()
        if name.startswith(REPAIR_BRANCH_PREFIX):
            tail = name[len(REPAIR_BRANCH_PREFIX) :].strip()
            if tail:
                found.add(tail.upper())
    return found


def list_branch_files(
    repo_root: Path | str, branch: str, prefix: str | None = None
) -> list[str]:
    """The paths committed on ``branch`` (under ``prefix`` when given)."""
    args = ["ls-tree", "-r", "-z", "--name-only", branch]
    if prefix:
        args += ["--", prefix]
    proc = _git(Path(repo_root), *args)
    if proc.returncode != 0:
        return []
    return [path for path in proc.stdout.split("\0") if path]


def read_branch_file(repo_root: Path | str, branch: str, path: str) -> str | None:
    """The text of ``path`` as committed on ``branch``, or None when it is not there."""
    proc = _git(Path(repo_root), "show", f"{branch}:{path}")
    return proc.stdout if proc.returncode == 0 else None


def find_task_file_on_branch(
    repo_root: Path | str, branch: str, task_id: str
) -> str | None:
    """guardkit's loader rule, applied to the branch's tree instead of a working tree.

    The loader takes the first ``tasks/<dir>/**/<task id>*.md`` in its
    directory order; so does this. None when the branch carries no such file.
    """
    files = list_branch_files(repo_root, branch, "tasks")
    for dir_name in TASK_SEARCH_DIRS:
        head = f"tasks/{dir_name}/"
        for path in sorted(files):
            if (
                path.startswith(head)
                and path.endswith(".md")
                and Path(path).name.startswith(task_id)
            ):
                return path
    return None


# ---------------------------------------------------------------------------
# The exclude line
# ---------------------------------------------------------------------------


def _exclude_file(repo: Path) -> Path:
    """Where this checkout's ``info/exclude`` lives — right for a worktree too."""
    proc = _git(repo, "rev-parse", "--git-path", "info/exclude")
    if proc.returncode != 0:
        raise RepairBranchError(
            f"git could not say where {repo}'s info/exclude lives: {_last_line(proc)}"
        )
    path = Path(proc.stdout.strip())
    return path if path.is_absolute() else repo / path


def ensure_forge_excluded(repo_root: Path | str) -> bool:
    """Write ``.forge/`` into the checkout's ``.git/info/exclude`` once.

    Returns True when this call wrote the line, False when it was already
    there. Raises :class:`RepairBranchError` when the file cannot be written.
    """
    repo = Path(repo_root)
    exclude = _exclude_file(repo)
    try:
        existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
        if FORGE_EXCLUDE_LINE in {line.strip() for line in existing.splitlines()}:
            return False
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a", encoding="utf-8") as handle:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            handle.write(FORGE_EXCLUDE_LINE + "\n")
        return True
    except OSError as exc:
        raise RepairBranchError(
            f"could not write {FORGE_EXCLUDE_LINE!r} into {exclude}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# The branch itself
# ---------------------------------------------------------------------------


def _remove_worktree(repo: Path, worktree: Path) -> None:
    """Take the temporary worktree away, however it was left; never raises."""
    if worktree.exists():
        _git(repo, "worktree", "remove", "--force", str(worktree))
    if worktree.exists():
        shutil.rmtree(worktree, ignore_errors=True)
    _git(repo, "worktree", "prune")


def _identity_args(worktree: Path) -> list[str]:
    """``-c user.name/-c user.email`` only when the checkout has no identity of its own."""
    email = _git(worktree, "config", "user.email")
    name = _git(worktree, "config", "user.name")
    if (
        email.returncode == 0
        and email.stdout.strip()
        and name.returncode == 0
        and name.stdout.strip()
    ):
        return []
    user, address = FALLBACK_COMMIT_IDENTITY
    return ["-c", f"user.name={user}", "-c", f"user.email={address}"]


def _write_files(worktree: Path, files: Mapping[str, str]) -> list[str]:
    root = worktree.resolve()
    written: list[str] = []
    for relpath, text in files.items():
        rel = str(relpath).strip()
        if not rel or Path(rel).is_absolute():
            raise RepairBranchError(
                f"a file on the repair branch needs a relative path, not {relpath!r}"
            )
        target = (root / rel).resolve()
        if root not in target.parents:
            raise RepairBranchError(
                f"the path {relpath!r} would escape the repair worktree"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        written.append(rel)
    return written


def materialise_repair_branch(
    repo_root: Path | str,
    *,
    task_id: str,
    base_branch: str,
    files: Mapping[str, str],
    message: str,
) -> RepairBranchResult:
    """Put ``files`` on ``repair/<task id>``, cut from ``base_branch``, in one commit.

    The branch is created when it does not exist and reused when it does. The
    files are written in a temporary worktree under the checkout's ``.forge/``
    directory and committed there; nothing changes when they are already what
    the branch carries. The worktree is removed on every path. A failure to
    write or commit raises :class:`RepairBranchError` and, when this call
    created the branch, deletes it again.

    Never touches the shared checkout's working tree or index: the only
    commands run against the checkout itself are ``worktree add/remove/prune``,
    ``rev-parse``, ``for-each-ref`` and ``branch -D``.
    """
    repo = Path(repo_root)
    if not files:
        raise RepairBranchError("no files were given to put on the repair branch")
    if not is_git_checkout(repo):
        raise RepairBranchError(
            f"{repo} is not a git checkout, so no repair branch can be cut there"
        )
    if not branch_exists(repo, base_branch):
        raise RepairBranchError(
            f"there is no local branch called {base_branch!r} in {repo} to cut "
            "the repair branch from"
        )

    branch = repair_branch_name(task_id)
    ensure_forge_excluded(repo)
    worktree = repair_worktree_path(repo, task_id)
    _remove_worktree(repo, worktree)  # a leftover from a crash, if any

    created = not branch_exists(repo, branch)
    if created:
        added = _git(repo, "worktree", "add", "-b", branch, str(worktree), base_branch)
    else:
        added = _git(repo, "worktree", "add", str(worktree), branch)
    if added.returncode != 0:
        _remove_worktree(repo, worktree)
        if created and branch_exists(repo, branch):
            _git(repo, "branch", "-D", branch)
        raise RepairBranchError(
            f"git could not make a worktree for {branch} in {repo}: {_last_line(added)}"
        )

    committed = False
    try:
        written = _write_files(worktree, files)
        staged = _git(worktree, "add", "-f", "--", *written)
        if staged.returncode != 0:
            raise RepairBranchError(
                f"git could not stage the repair's files on {branch}: {_last_line(staged)}"
            )
        changed = _git(worktree, "diff", "--cached", "--quiet")
        if changed.returncode == 1:
            commit = _git(
                worktree, *_identity_args(worktree), "commit", "-q", "-m", message
            )
            if commit.returncode != 0:
                raise RepairBranchError(
                    f"git could not commit the repair's files on {branch}: "
                    f"{_last_line(commit)}"
                )
            committed = True
        elif changed.returncode != 0:
            raise RepairBranchError(
                f"git could not read the repair's staged files on {branch}: "
                f"{_last_line(changed)}"
            )
        head = _git(worktree, "rev-parse", "HEAD")
        if head.returncode != 0:
            raise RepairBranchError(
                f"git could not read the tip of {branch}: {_last_line(head)}"
            )
        result = RepairBranchResult(
            branch=branch,
            commit=head.stdout.strip(),
            created_branch=created,
            committed=committed,
            files=tuple(written),
        )
    except Exception as exc:
        _remove_worktree(repo, worktree)
        if created:
            _git(repo, "branch", "-D", branch)
        if isinstance(exc, RepairBranchError):
            raise
        raise RepairBranchError(
            f"the repair branch {branch} could not be written: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    finally:
        _remove_worktree(repo, worktree)

    logger.info(
        "repair branch: %s %s at %s (%s)",
        branch,
        "created" if created else "reused",
        result.commit[:12],
        "one commit" if committed else "nothing changed, no commit",
    )
    return result


__all__ = [
    "FALLBACK_COMMIT_IDENTITY",
    "FORGE_DIRNAME",
    "FORGE_EXCLUDE_LINE",
    "REPAIR_BRANCH_PREFIX",
    "REPAIR_WORKTREE_PREFIX",
    "TASK_SEARCH_DIRS",
    "RepairBranchError",
    "RepairBranchResult",
    "branch_exists",
    "ensure_forge_excluded",
    "find_task_file_on_branch",
    "is_git_checkout",
    "is_repair_branch",
    "list_branch_files",
    "materialise_repair_branch",
    "read_branch_file",
    "repair_branch_name",
    "repair_task_ids_on_branches",
    "repair_worktree_path",
]

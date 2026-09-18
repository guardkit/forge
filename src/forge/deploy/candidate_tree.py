"""The candidate's tree — where a feature branch is laid out for its sandbox check.

Protect-main (the rewrite-on-refusal spec, Part J, 2026-09-07, rule 38): the
merge word now checks the feature branch in the Docker Sandbox BEFORE the merge
lands. The sandbox bind-mounts the repository checkout at its own host path, so
the branch's tree is laid out INSIDE that checkout, at::

    <checkout>/.forge-candidates/<FEAT-id>/

That directory is added to the checkout's ``.git/info/exclude`` once, so the
shared checkout stays clean for the merge command's dirty-tree check, and it is
removed when the run ends — after the promote or after the refusal, whichever
comes.

WHY AN EXTRACTED ``git archive`` AND NOT A ``git worktree``. Both give the exact
tree of the commit. An archive was chosen because:

* it leaves nothing behind in the shared checkout's ``.git`` — a run that dies
  half way leaves only a directory to delete, never a stale worktree
  registration that makes the next press refuse "already exists";
* the laid-out tree carries no ``.git`` file, so nothing that runs inside the
  sandbox against it can reach the shared checkout's git state;
* the tree ids compared before the promote (rule 37) are read from the
  checkout with ``git rev-parse``, so the laid-out tree never needs git.

A lay-out that fails leaves NOTHING behind (coach's refutation, 2026-09-07):
whether git refused the commit or the extraction died half way, the empty or
half-filled directory is removed before the error is raised. The executor has
no path to remove at that point, so this module is the only place that can.

The one thing an archive does that a worktree does not: it honours
``export-ignore`` attributes. A repository that marks files it needs for its
build as export-ignore would lay out a tree that builds differently. None of the
registered repositories does; it is written down here so nobody meets it as a
puzzle.

Nothing here runs docker, ``sbx``, or the deploy script.

THE VENUE SEAM (sandbox first, 2026-09-07, rule 89). The five git operations
the merge press needs — reading a commit, asking whether one commit is in
another, keeping the laid-out trees out of the checkout's eyes, laying one
out, removing it — are gathered into one small surface,
:class:`CandidateGit`, so the press can be told WHERE they happen instead of
assuming they happen here. :class:`InContainerCandidateGit` is this file's own
functions, called in the order the press has always called them, for every
repository that has no sandbox; :class:`~forge.deploy.sidecar_git.
SidecarCandidateGit` is the same five over the sidecar inside a repository's
sandbox, where a sandboxed repository's branches actually are. Neither knows
about the other.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

__all__ = [
    "CANDIDATE_TREES_DIRNAME",
    "CANDIDATE_TREES_EXCLUDE_LINE",
    "CandidateGit",
    "CandidateTreeError",
    "CandidateTreeLayout",
    "InContainerCandidateGit",
    "candidate_tree_path",
    "candidate_trees_root",
    "ensure_candidate_trees_excluded",
    "git_is_ancestor",
    "git_rev_parse",
    "is_candidate_tree_path",
    "materialise_candidate_tree",
    "remove_candidate_tree",
]

#: The directory, directly under the repository checkout, that holds one laid
#: out tree per feature. The deploy sidecar accepts a working directory other
#: than the profile's ONLY when it is a directory directly under this one.
CANDIDATE_TREES_DIRNAME: str = ".forge-candidates"

#: The line written once into ``.git/info/exclude`` so the laid-out trees never
#: make the shared checkout look dirty.
CANDIDATE_TREES_EXCLUDE_LINE: str = ".forge-candidates/"


class CandidateTreeError(RuntimeError):
    """The candidate's tree could not be laid out, excluded, or read."""


def candidate_trees_root(repo_root: Path | str) -> Path:
    """``<checkout>/.forge-candidates``."""
    return Path(repo_root) / CANDIDATE_TREES_DIRNAME


def candidate_tree_path(repo_root: Path | str, feature_id: str) -> Path:
    """``<checkout>/.forge-candidates/<FEAT-id>`` — refusing an id that is not one path segment."""
    feature = str(feature_id or "").strip()
    if not feature or "/" in feature or "\\" in feature or feature in {".", ".."}:
        raise CandidateTreeError(
            f"a candidate tree needs a feature id that is one plain path "
            f"segment, not {feature_id!r}"
        )
    return candidate_trees_root(repo_root) / feature


def is_candidate_tree_path(repo_root: Path | str, candidate: Path | str) -> bool:
    """True when ``candidate`` names a directory DIRECTLY under the trees root.

    Paths are resolved on both sides so ``..`` and symlinks cannot walk out of
    the root; the root itself does not count. The directory need not exist —
    callers that require existence check that themselves.
    """
    try:
        root = candidate_trees_root(repo_root).resolve()
        wanted = Path(candidate).resolve()
    except OSError:
        return False
    return wanted != root and wanted.parent == root


def _git(repo_root: Path, *args: str) -> str:
    """Run one git command in ``repo_root`` and return its stdout, trimmed.

    Raises :class:`CandidateTreeError` with git's own words when it fails.
    """
    try:
        done = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", *args],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise CandidateTreeError(f"git could not be run in {repo_root}: {exc}") from exc
    if done.returncode != 0:
        said = (done.stderr or done.stdout or "").strip().splitlines()
        last = said[-1] if said else f"exit code {done.returncode}"
        raise CandidateTreeError(f"git {' '.join(args)} failed: {last}")
    return done.stdout.strip()


async def git_rev_parse(repo_root: Path | str, ref: str) -> str | None:
    """``git rev-parse <ref>`` in the checkout, or None when git cannot answer.

    Used for the branch tip (``autobuild/<FEAT>``) and for tree ids
    (``<sha>^{tree}``). Never raises: an unanswerable question is an honest
    None, and the caller says what it could not read.
    """
    try:
        return await asyncio.to_thread(_git, Path(repo_root), "rev-parse", "--verify", ref)
    except CandidateTreeError as exc:
        logger.warning("candidate tree: %s", exc)
        return None


def _materialise_sync(repo_root: Path, dest: Path, sha: str) -> None:
    if dest.exists():
        # A run that died half way left its tree behind; a fresh one replaces it.
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=False)
    try:
        _extract_archive_into(repo_root, dest, sha)
    except BaseException:
        # Rule 38: the tree is removed when the run ends, and a lay-out that
        # fails IS the end of the run. Nothing stays behind in the shared
        # checkout — not an empty directory, not a half-extracted one.
        shutil.rmtree(dest, ignore_errors=True)
        raise


def _extract_archive_into(repo_root: Path, dest: Path, sha: str) -> None:
    """Stream ``git archive <sha>`` into ``dest``; raise with git's own words."""
    try:
        proc = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            ["git", "archive", "--format=tar", sha],
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise CandidateTreeError(f"git could not be run in {repo_root}: {exc}") from exc
    assert proc.stdout is not None and proc.stderr is not None
    extract_error: Exception | None = None
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as archive:
            archive.extractall(dest, filter="data")
    except (tarfile.TarError, OSError) as exc:
        extract_error = exc
    finally:
        proc.stdout.close()
        stderr = proc.stderr.read().decode("utf-8", errors="replace").strip()
        proc.stderr.close()
        returncode = proc.wait()
    if returncode != 0:
        last = stderr.splitlines()[-1] if stderr else f"exit code {returncode}"
        raise CandidateTreeError(f"git archive {sha} failed: {last}")
    if extract_error is not None:
        raise CandidateTreeError(
            f"the archive of {sha} could not be extracted into {dest}: {extract_error}"
        )


async def materialise_candidate_tree(
    repo_root: Path | str, feature_id: str, sha: str
) -> Path:
    """Lay the tree of ``sha`` out at ``<checkout>/.forge-candidates/<FEAT-id>``.

    Returns the directory. Raises :class:`CandidateTreeError` when it cannot —
    the caller turns that into a refusal before the merge, in plain words.
    """
    root = Path(repo_root)
    dest = candidate_tree_path(root, feature_id)
    if not sha or not str(sha).strip():
        raise CandidateTreeError("no commit was named for the candidate tree")
    await asyncio.to_thread(_materialise_sync, root, dest, str(sha).strip())
    return dest


def _remove_sync(path: Path) -> bool:
    if not path.exists():
        return True
    shutil.rmtree(path)
    return True


async def remove_candidate_tree(path: Path | str) -> bool:
    """Remove a laid-out tree. Never raises; False when it could not be removed."""
    try:
        return await asyncio.to_thread(_remove_sync, Path(path))
    except OSError as exc:
        logger.warning("candidate tree: %s could not be removed (%s)", path, exc)
        return False


def _exclude_file(repo_root: Path) -> Path:
    """Where this checkout's ``info/exclude`` lives — right for a worktree too."""
    where = _git(repo_root, "rev-parse", "--git-path", "info/exclude")
    path = Path(where)
    return path if path.is_absolute() else repo_root / path


def _ensure_excluded_sync(repo_root: Path) -> bool:
    exclude = _exclude_file(repo_root)
    try:
        existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
        lines = {line.strip() for line in existing.splitlines()}
        if CANDIDATE_TREES_EXCLUDE_LINE in lines:
            return False
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a", encoding="utf-8") as handle:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            handle.write(CANDIDATE_TREES_EXCLUDE_LINE + "\n")
        return True
    except OSError as exc:
        raise CandidateTreeError(
            f"could not write {CANDIDATE_TREES_EXCLUDE_LINE!r} into {exclude}: {exc}"
        ) from exc


async def ensure_candidate_trees_excluded(repo_root: Path | str) -> bool:
    """Write ``.forge-candidates/`` into the checkout's ``.git/info/exclude`` once.

    Returns True when the line was written by this call, False when it was
    already there. Idempotent: a second call never adds a second line. Raises
    :class:`CandidateTreeError` when the file cannot be written, because a tree
    the checkout counts as dirt would make the merge command refuse for a
    reason nobody could see.
    """
    return await asyncio.to_thread(_ensure_excluded_sync, Path(repo_root))


async def git_is_ancestor(
    repo_root: Path | str, ancestor: str, descendant: str
) -> bool | None:
    """Is ``ancestor`` a commit that ``descendant`` already contains?

    ``git merge-base --is-ancestor`` answers exit 0 (yes) or exit 1 (no), and
    anything else means git could not say — a commit it does not know, or git
    not running at all. Only a plain yes or no is returned as a bool; "could
    not say" is an honest ``None`` and the caller decides what to do with a
    question it could not have answered.
    """
    root = Path(repo_root)
    args = ("merge-base", "--is-ancestor", str(ancestor), str(descendant))
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(root),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
    except Exception as exc:  # noqa: BLE001 — best-effort probe, honest None
        logger.warning(
            "candidate tree: git %s could not be run in %s (%s)",
            " ".join(args),
            root,
            exc,
        )
        return None
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    logger.warning(
        "candidate tree: git %s exited %s in %s — git could not say",
        " ".join(args),
        proc.returncode,
        root,
    )
    return None


# ---------------------------------------------------------------------------
# The venue seam — the same five operations, in the place the repository lives
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateTreeLayout:
    """What laying a candidate's tree out produced.

    ``path`` is the directory the tree is in — the same path whichever venue
    laid it out, because a repository's sandbox holds its clone at the path
    the checkout has on this side, which is what lets the deploy stage and the
    live gate run against it with no translation.

    ``tree`` is the commit's tree id when the venue answered one while it was
    laying the tree out (the sandbox route does; the functions here do not,
    and the press reads it with :meth:`CandidateGit.rev_parse` as it always
    has). ``exclude_written`` says whether THIS call wrote the exclude line.
    """

    path: str
    tree: str | None = None
    exclude_written: bool | None = None


@runtime_checkable
class CandidateGit(Protocol):
    """The merge press's five git operations, wherever they happen.

    One repository, one surface. Every implementation is written never to
    raise except where the press already expects a raise
    (:meth:`materialise_candidate_tree`, which raises
    :class:`CandidateTreeError`); everything else answers ``None`` or
    ``False`` when it could not do the thing, and says why in the log.
    """

    async def rev_parse(self, ref: str) -> str | None:
        """The commit (or tree) ``ref`` names, or ``None``."""

    async def is_ancestor(self, ancestor: str, descendant: str) -> bool | None:
        """Is ``ancestor`` in ``descendant``? ``None`` = git could not say."""

    async def ensure_candidate_trees_excluded(self) -> bool | None:
        """Keep the laid-out trees out of the checkout's eyes, once.

        ``True`` when this call wrote the line, ``False`` when it was already
        there, ``None`` when this venue does it as part of laying the tree out
        and there is nothing to do on its own.
        """

    async def materialise_candidate_tree(
        self, feature_id: str, sha: str
    ) -> CandidateTreeLayout:
        """Lay ``sha``'s tree out for ``feature_id``; raise on failure."""

    async def remove_candidate_tree(
        self, feature_id: str, path: str | None = None
    ) -> bool:
        """Remove the laid-out tree. Never raises; ``False`` when it could not."""

    async def inspect_autobuild_worktree(
        self, build_id: str, path: str
    ) -> dict[str, Any]:
        """Read one exact retained autobuild worktree identity."""

    async def retire_autobuild_worktree(
        self, build_id: str, path: str, expected: dict[str, Any]
    ) -> dict[str, Any]:
        """Retire an offer-pinned autobuild worktree after lifecycle success."""


class InContainerCandidateGit:
    """The five operations run here, against ``repo_root``, exactly as before.

    Every method is one of this module's own functions, called with the
    arguments the merge press has always called it with. This is the venue for
    every repository that has no sandbox — which is every repository until an
    operator gives one a sandbox — so nothing about such a press changes.
    """

    def __init__(self, repo_root: Path | str) -> None:
        self._repo_root = Path(repo_root)

    @property
    def repo_root(self) -> Path:
        return self._repo_root

    @property
    def venue(self) -> str:
        """Where the work happened, for a sentence a person reads."""
        return f"in {self._repo_root}"

    async def rev_parse(self, ref: str) -> str | None:
        return await git_rev_parse(self._repo_root, ref)

    async def is_ancestor(self, ancestor: str, descendant: str) -> bool | None:
        return await git_is_ancestor(self._repo_root, ancestor, descendant)

    async def ensure_candidate_trees_excluded(self) -> bool | None:
        return await ensure_candidate_trees_excluded(self._repo_root)

    async def materialise_candidate_tree(
        self, feature_id: str, sha: str
    ) -> CandidateTreeLayout:
        path = await materialise_candidate_tree(self._repo_root, feature_id, sha)
        return CandidateTreeLayout(path=str(path))

    async def remove_candidate_tree(
        self, feature_id: str, path: str | None = None
    ) -> bool:
        where = path or str(candidate_tree_path(self._repo_root, feature_id))
        return await remove_candidate_tree(where)

    @staticmethod
    def _autobuild_base() -> Path:
        from forge.subagents.autobuild_worktree_lifecycle import (
            DEFAULT_AUTOBUILD_WORKTREE_BASE,
            FORGE_AUTOBUILD_WORKTREE_BASE_ENV,
        )

        return Path(
            os.environ.get(FORGE_AUTOBUILD_WORKTREE_BASE_ENV, "").strip()
            or DEFAULT_AUTOBUILD_WORKTREE_BASE
        ).expanduser()

    async def inspect_autobuild_worktree(
        self, build_id: str, path: str
    ) -> dict[str, Any]:
        from forge.subagents.autobuild_worktree_lifecycle import (
            inspect_autobuild_worktree,
        )

        return await asyncio.to_thread(
            inspect_autobuild_worktree,
            repo=self._repo_root,
            base=self._autobuild_base(),
            build_id=build_id,
            path=Path(path),
        )

    async def retire_autobuild_worktree(
        self, build_id: str, path: str, expected: dict[str, Any]
    ) -> dict[str, Any]:
        from forge.subagents.autobuild_worktree_lifecycle import (
            retire_autobuild_worktree,
        )

        return await asyncio.to_thread(
            retire_autobuild_worktree,
            repo=self._repo_root,
            base=self._autobuild_base(),
            build_id=build_id,
            path=Path(path),
            expected=expected,
        )

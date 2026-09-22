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

THE VENUE SEAM (sandbox first, 2026-09-07, rule 89). The git operations the
merge press needs — reading a commit, asking whether one commit is in
another, keeping the laid-out trees out of the checkout's eyes, laying one
out, removing it — and, since the one-true-copy lane (item 1, 2026-09-21),
the one operation a NEW piece of work starts with — fetching the project's
remote and asking where its default branch is — are gathered into one small
surface,
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
import re
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
    "REMOTE_NAME",
    "REMOTE_TIMEOUT_SECONDS",
    "RemoteStartPoint",
    "candidate_tree_path",
    "candidate_trees_root",
    "ensure_candidate_trees_excluded",
    "fetch_remote_start_point",
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


# ---------------------------------------------------------------------------
# The starting point — the remote's own default branch and where it is
# (one true copy, item 1, 2026-09-21)
# ---------------------------------------------------------------------------
#
# A new piece of work must start from the commit the project's remote holds,
# not from whatever the factory's own copy happens to have checked out. This
# is the one operation that asks: fetch the remote named ``origin``, and say
# which branch that remote calls its default and which commit that branch is
# at.
#
# WHY ``git ls-remote --symref origin HEAD`` AND NOT THE OTHER TWO WAYS.
# ``git symbolic-ref refs/remotes/origin/HEAD`` reads a ref this copy wrote
# when it was cloned: it answers even when the remote is gone, and it goes on
# answering the old branch after the remote's default changes, so it cannot
# tell the truth about the remote. ``git remote show origin`` does ask the
# remote, but it is porcelain meant for a person to read and its wording is
# free to change. ``ls-remote --symref`` is plumbing, it asks the remote
# itself, and it works against a bare repository on a local path exactly as
# it does against one reached over a network — which is what lets every case
# here be driven without touching anybody's real repository.
#
# Nothing here knows what the project contains, who hosts the remote, or what
# the default branch is called. Two facts are used and no others: there is a
# remote named ``origin``, and that remote says which branch is its default.

#: The one remote a project's copy is asked about. A copy that has no remote
#: by this name is refused, not guessed at.
REMOTE_NAME: str = "origin"

#: How long a command that talks to the remote may take.
REMOTE_TIMEOUT_SECONDS: float = 180.0

#: The shape a branch name must have before it is put on a git command line:
#: it starts with a letter or a digit (so it can never be read as an option)
#: and carries only the characters branch names use.
_REMOTE_BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]*$")


@dataclass(frozen=True)
class RemoteStartPoint:
    """Where a new piece of work starts, or one plain sentence saying why not.

    ``branch`` and ``commit`` are the remote's default branch and the commit
    it is at, both filled in when the answer is yes. ``refusal`` is filled in
    instead when there is nothing to start from — no remote of that name, a
    remote that could not be reached, a remote that names no default branch,
    or a branch that could not be fetched. Exactly one of the two sides is
    ever filled in, and the sentence is written for a person to read.
    """

    branch: str | None = None
    commit: str | None = None
    refusal: str | None = None

    @property
    def ok(self) -> bool:
        """True when this is a starting point rather than a refusal."""
        return bool(self.branch and self.commit and not self.refusal)

    def to_wire(self) -> dict[str, Any]:
        """The answer as the sandbox's helper service sends it."""
        return {"branch": self.branch, "commit": self.commit, "refusal": self.refusal}

    @classmethod
    def from_wire(cls, decoded: Any) -> "RemoteStartPoint":
        """The answer as it came back, or a refusal saying it made no sense."""
        if not isinstance(decoded, dict):
            return cls(
                refusal="the answer was not an object with a starting point in it"
            )
        branch = decoded.get("branch")
        commit = decoded.get("commit")
        refusal = decoded.get("refusal")
        if isinstance(refusal, str) and refusal.strip():
            return cls(refusal=refusal.strip())
        if not isinstance(branch, str) or not branch.strip():
            return cls(refusal="the answer named no branch and gave no reason")
        if not isinstance(commit, str) or not commit.strip():
            return cls(refusal="the answer named no commit and gave no reason")
        return cls(branch=branch.strip(), commit=commit.strip())


def _git_said(done: "subprocess.CompletedProcess[str]") -> str:
    """What git said, in one line, for a sentence a person reads.

    Git says the useful thing FIRST and then advises ("Please make sure you
    have the correct access rights / and the repository exists"), so the first
    ``fatal:`` line is taken where there is one — quoting the last line would
    hand a person the tail of a sentence with no subject.
    """
    said = ((done.stderr or "") + "\n" + (done.stdout or "")).strip().splitlines()
    lines = [line.strip() for line in said if line.strip()]
    if not lines:
        return f"exit code {done.returncode}"
    for line in lines:
        if line.lower().startswith("fatal:") or line.lower().startswith("error:"):
            return line
    return lines[0]


def _run_git(repo_root: Path, *args: str) -> "subprocess.CompletedProcess[str]":
    """One git command, fixed argv, no shell, bounded."""
    return subprocess.run(  # noqa: S603 — fixed argv, no shell
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        timeout=REMOTE_TIMEOUT_SECONDS,
        check=False,
    )


def _fetch_remote_start_point_sync(repo_root: Path) -> RemoteStartPoint:
    """The four steps, in order, each one's failure ending it with a sentence."""
    where = str(repo_root)
    try:
        named = _run_git(repo_root, "remote", "get-url", REMOTE_NAME)
        if named.returncode != 0:
            return RemoteStartPoint(
                refusal=(
                    f"the copy of this project at {where} has no remote named "
                    f"'{REMOTE_NAME}', so there is nothing to start the work "
                    f"from. Add that remote to the copy, then ask again."
                )
            )

        asked = _run_git(repo_root, "ls-remote", "--symref", REMOTE_NAME, "HEAD")
        if asked.returncode != 0:
            return RemoteStartPoint(
                refusal=(
                    f"the remote named '{REMOTE_NAME}' could not be reached "
                    f"from {where}, so the work cannot be started from it: "
                    f"{_git_said(asked)}"
                )
            )

        branch: str | None = None
        for line in (asked.stdout or "").splitlines():
            stripped = line.strip()
            if stripped.startswith("ref:"):
                rest = stripped[len("ref:") :].strip().split()
                ref = rest[0] if rest else ""
                if ref.startswith("refs/heads/"):
                    branch = ref[len("refs/heads/") :]
                break
        if not branch or not _REMOTE_BRANCH_PATTERN.match(branch):
            return RemoteStartPoint(
                refusal=(
                    f"the remote named '{REMOTE_NAME}' does not say which "
                    f"branch is its default, so there is nothing to start the "
                    f"work from. Set that remote's default branch, then ask "
                    f"again."
                )
            )

        fetched = _run_git(
            repo_root,
            "fetch",
            "--no-tags",
            REMOTE_NAME,
            f"+refs/heads/{branch}:refs/remotes/{REMOTE_NAME}/{branch}",
        )
        if fetched.returncode != 0:
            return RemoteStartPoint(
                refusal=(
                    f"the branch '{branch}' could not be fetched from the "
                    f"remote named '{REMOTE_NAME}': {_git_said(fetched)}"
                )
            )

        read = _run_git(
            repo_root,
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/remotes/{REMOTE_NAME}/{branch}^{{commit}}",
        )
        commit = (read.stdout or "").strip()
        if read.returncode != 0 or not commit:
            return RemoteStartPoint(
                refusal=(
                    f"the branch '{branch}' was fetched from the remote named "
                    f"'{REMOTE_NAME}' but git could not say which commit it is "
                    f"at: {_git_said(read)}"
                )
            )
        return RemoteStartPoint(branch=branch, commit=commit)
    except OSError as exc:
        return RemoteStartPoint(
            refusal=f"git could not be run in {where}: {type(exc).__name__}: {exc}"
        )
    except subprocess.TimeoutExpired:
        return RemoteStartPoint(
            refusal=(
                f"the remote named '{REMOTE_NAME}' did not answer within "
                f"{REMOTE_TIMEOUT_SECONDS:.0f} seconds, so the work cannot be "
                f"started from it"
            )
        )


async def fetch_remote_start_point(repo_root: Path | str) -> RemoteStartPoint:
    """Fetch ``origin``'s default branch and say which commit it is at.

    Never raises: everything that can go wrong comes back as ``refusal`` with
    one plain sentence in it. Nothing this does changes the branch the copy
    has checked out or writes anything into its working folder — it updates
    one remote-tracking ref and reads it.
    """
    return await asyncio.to_thread(_fetch_remote_start_point_sync, Path(repo_root))


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
    """One repository's git operations, wherever they happen.

    One repository, one surface. Every implementation is written never to
    raise except where the press already expects a raise
    (:meth:`materialise_candidate_tree`, which raises
    :class:`CandidateTreeError`); everything else answers ``None`` or
    ``False`` when it could not do the thing, and says why in the log.
    """

    async def rev_parse(self, ref: str) -> str | None:
        """The commit (or tree) ``ref`` names, or ``None``."""

    async def fetch_remote_start_point(self) -> RemoteStartPoint:
        """Fetch the remote named ``origin`` and say where its default branch is.

        The starting rule's one operation (one true copy, item 1): the answer
        is a branch and a commit, or a plain-sentence refusal. It never raises,
        never changes a checked-out branch and never touches a working folder.
        """

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

    async def fetch_remote_start_point(self) -> RemoteStartPoint:
        return await fetch_remote_start_point(self._repo_root)

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

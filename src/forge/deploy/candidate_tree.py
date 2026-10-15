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
import base64
import logging
import os
import re
import shutil
import subprocess
import tarfile
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from forge import network_git
from forge.project_git import failure_sentence, open_git, run_git, run_git_async

logger = logging.getLogger(__name__)

__all__ = [
    "CANDIDATE_TREES_DIRNAME",
    "CANDIDATE_TREES_EXCLUDE_LINE",
    "CandidateGit",
    "CandidateTreeError",
    "CandidateTreeLayout",
    "FileAtCommit",
    "InContainerCandidateGit",
    "MAX_FILE_AT_COMMIT_BYTES",
    "READ_AT_COMMIT_TIMEOUT_SECONDS",
    "REMOTE_NAME",
    "REMOTE_TIMEOUT_SECONDS",
    "RemoteStartPoint",
    "TreeEntry",
    "TreeListing",
    "answered_as_ordinary",
    "answered_as_raw",
    "answered_for_branch",
    "candidate_tree_path",
    "candidate_trees_root",
    "ensure_candidate_trees_excluded",
    "fetch_remote_start_point",
    "git_is_ancestor",
    "read_file_at_commit",
    "read_file_at_commit_sync",
    "git_rev_parse",
    "is_candidate_tree_path",
    "list_tree_at_commit",
    "list_tree_at_commit_sync",
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

    Through the one git helper (14 October 2026). Raises
    :class:`CandidateTreeError` with the helper's fixed sentence when it
    fails; git's own words are never repeated.
    """
    try:
        done = run_git(list(args), cwd=repo_root)
    except OSError as exc:
        raise CandidateTreeError(
            f"git could not be run in {repo_root} ({type(exc).__name__})"
        ) from exc
    if done.returncode != 0:
        raise CandidateTreeError(
            done.stderr or failure_sentence(args[0] if args else "git", done.returncode)
        )
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
# is the one operation that asks: fetch the project's remote, and say which
# branch that remote calls its default and which commit that branch is at.
#
# THE REMOTE IS THE RECORDED ADDRESS, NEVER THE CLONE'S ORIGIN (Forge git
# hardening, 14 October 2026). The clone's settings can be written by the
# project or the model, and plain git obeys an address rewrite, an ssh
# program, a credential helper or an upload-pack program named there. So the
# address is the one the factory recorded (``planning.fetch_addresses``), and
# :mod:`forge.network_git` fetches it into the factory's own copy, with no
# credential and one transport, then brings the commit into this clone as
# ``refs/remotes/origin/<branch>`` (the name every reader here uses) and
# checks it is the same commit.
#
# WHY ``ls-remote --symref`` AND NOT ``symbolic-ref refs/remotes/origin/HEAD``.
# That ref was written when the copy was cloned: it answers even when the
# remote is gone, and it goes on answering the old branch after the remote's
# default changes. ``ls-remote --symref`` is plumbing and asks the remote
# itself.
#
# Nothing here knows what the project contains, who hosts the remote, or what
# the default branch is called.

#: The remote-tracking namespace the starting point is brought into
#: (``refs/remotes/origin/<branch>``). The clone's own ``origin`` setting is
#: never read.
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
    # The commit of a NAMED branch, from the same call (4 October 2026, a
    # prepared feature through the normal build route). A feature planned
    # elsewhere is queued on its own branch; admission needs both the default
    # branch (where the work will be merged) and the commit that branch names
    # (what will be built). Only set when a branch was asked for; for the
    # default branch itself it is the same commit as ``commit``.
    branch_commit: str | None = None

    @property
    def ok(self) -> bool:
        """True when this is a starting point rather than a refusal."""
        return bool(self.branch and self.commit and not self.refusal)

    def to_wire(self) -> dict[str, Any]:
        """The answer as the sandbox's helper service sends it.

        ``branch_commit`` is sent only when there is one, so an answer to a
        caller that asked for no branch is exactly what it always was.
        """
        wire: dict[str, Any] = {
            "branch": self.branch,
            "commit": self.commit,
            "refusal": self.refusal,
        }
        if self.branch_commit:
            wire["branch_commit"] = self.branch_commit
        return wire

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
        branch_commit = decoded.get("branch_commit")
        return cls(
            branch=branch.strip(),
            commit=commit.strip(),
            branch_commit=(
                branch_commit.strip()
                if isinstance(branch_commit, str) and branch_commit.strip()
                else None
            ),
        )


def answered_for_branch(
    answer: RemoteStartPoint, branch: str | None
) -> RemoteStartPoint:
    """``answer``, or a refusal when a named branch was asked for and not answered.

    A helper service older than the ``branch`` input ignores it and answers
    the default branch alone. Taking that as an answer about the named branch
    would build the wrong commit, so a caller that asked for one and got no
    ``branch_commit`` back is told so in plain words instead.
    """
    if not branch or not answer.ok or answer.branch_commit:
        return answer
    return RemoteStartPoint(
        refusal=(
            f"the answer did not say which commit the branch '{branch}' is at; "
            f"the sandbox's helper service may be older than this factory"
        )
    )


def answered_as_raw(answer: "FileAtCommit", asked: bool) -> "FileAtCommit":
    """``answer``, or a refusal when a raw read was asked for and the answer
    found the file without saying what kind of entry it is — a helper service
    older than the raw read, whose text may have been altered on the way."""
    if not asked or not answer.ok or not answer.found or answer.mode:
        return answer
    return FileAtCommit(
        refusal=(
            "the answer did not say what kind of file this is or confirm its "
            "exact bytes; the sandbox's helper service may be older than this "
            "factory"
        )
    )


def answered_as_ordinary(answer: "FileAtCommit", asked: bool) -> "FileAtCommit":
    """``answer``, or a refusal when an ordinary-file check was asked for and
    not confirmed (the same reasoning as :func:`answered_for_branch`)."""
    if not asked or not answer.ok or not answer.found or answer.ordinary:
        return answer
    return FileAtCommit(
        refusal=(
            "the answer did not confirm the file is an ordinary file rather "
            "than a symbolic link; the sandbox's helper service may be older "
            "than this factory"
        )
    )


def _git_said(done: "subprocess.CompletedProcess[str]") -> str:
    """Why a git command failed, for a sentence a person reads.

    Since 14 October 2026 that is the git helper's fixed sentence ("git
    ls-tree stopped with exit code 128."): git's own words are discarded,
    because they can carry the project's text (a file name, a remote's
    message) into a card, a record or a model's prompt.
    """
    return (done.stderr or "").strip() or f"exit code {done.returncode}"


def _run_git(
    repo_root: Path, *args: str, timeout: float | None = None
) -> "subprocess.CompletedProcess[str]":
    """One local git command through the one git helper, bounded.

    ``errors="replace"`` because the bytes may come out of a commit the factory
    did not write: a file that is not text is then a sentence a person can read
    rather than an exception nobody catches.
    """
    return run_git(
        ["-C", str(repo_root), *args],
        errors="replace",
        timeout=REMOTE_TIMEOUT_SECONDS if timeout is None else timeout,
    )


def _fetch_remote_start_point_sync(
    repo_root: Path,
    branch: str | None = None,
    *,
    fetch_address: str | None = None,
    repo: str | None = None,
) -> RemoteStartPoint:
    """The starting point, fetched from the project's RECORDED address.

    Since 14 October 2026 (the Forge git hardening design, part 2) this never
    asks the clone's own ``origin``: the clone's settings can be written by
    the project or the model, and an address rewrite, an ssh program or a
    credential helper there would be obeyed. Instead
    :func:`forge.network_git.fetch_start_point` fetches the address the
    factory recorded (``planning.fetch_addresses``) into the factory's own
    copy, with no credential and one transport, then brings the commit into
    this clone's ``refs/remotes/origin/<branch>`` and checks it is the same
    commit. A project with no recorded address is refused, with no fallback
    to the clone's origin.

    ``branch`` (4 October 2026) names one more branch to fetch in the same
    call and answer the commit of, as ``branch_commit``.
    """
    wanted = (branch or "").strip() or None
    if wanted is not None and not _REMOTE_BRANCH_PATTERN.match(wanted):
        return RemoteStartPoint(
            refusal=(
                f"the branch name {wanted!r} is not one this factory will pass "
                f"to git"
            )
        )
    try:
        start = network_git.fetch_start_point(
            Path(repo_root),
            fetch_address,
            branch=wanted,
            repo=repo,
            timeout=REMOTE_TIMEOUT_SECONDS,
        )
    except OSError as exc:
        return RemoteStartPoint(
            refusal=f"git could not be run in {repo_root} ({type(exc).__name__})"
        )
    except subprocess.TimeoutExpired:
        return RemoteStartPoint(
            refusal=(
                f"the project's recorded address did not answer within "
                f"{REMOTE_TIMEOUT_SECONDS:.0f} seconds, so the work cannot be "
                f"started from it"
            )
        )
    if start.refusal or not start.branch or not start.commit:
        return RemoteStartPoint(
            refusal=start.refusal or "the starting point could not be read"
        )
    if wanted is None:
        return RemoteStartPoint(branch=start.branch, commit=start.commit)
    return RemoteStartPoint(
        branch=start.branch,
        commit=start.commit,
        branch_commit=start.branch_commit or start.commit,
    )


async def fetch_remote_start_point(
    repo_root: Path | str,
    branch: str | None = None,
    *,
    fetch_address: str | None = None,
    repo: str | None = None,
) -> RemoteStartPoint:
    """Fetch the project's default branch and say which commit it is at.

    ``fetch_address`` is the project's recorded address
    (``planning.fetch_addresses``); without one the answer is a refusal that
    names that setting. ``repo`` is the project's ``org/name``, for the
    sentence.

    Never raises: everything that can go wrong comes back as ``refusal`` with
    one plain sentence in it. Nothing this does changes the branch the copy
    has checked out or writes anything into its working folder — it updates
    one remote-tracking ref and reads it (two, when ``branch`` names another
    branch, whose commit then comes back as ``branch_commit``).
    """
    return await asyncio.to_thread(
        partial(
            _fetch_remote_start_point_sync,
            Path(repo_root),
            branch,
            fetch_address=fetch_address,
            repo=repo,
        )
    )


# ---------------------------------------------------------------------------
# One file, out of one commit (the project's own memory, item 2, 2026-09-21)
# ---------------------------------------------------------------------------
#
# The starting rule writes down the commit a piece of work starts from. This
# operation reads ONE file out of exactly that commit — never the working
# folder, never the branch the copy happens to have checked out — so the
# project's own declaration is read from the copy the work actually starts
# from and a stale checkout cannot supply it.
#
# WHY IT IS ITS OWN OPERATION AND NOT ``read_file_from_branch``. That one
# answers ``None`` for "the file is not there", "the commit is not there" and
# "the venue could not be reached" alike. Here those are three different
# things to say to a person: a project that declares nothing is told which two
# lines to add; a copy that could not be read at that commit is told that
# instead, and is never accused of declaring nothing.
#
# BOUNDED, because the file comes out of a commit the factory did not write:
# the blob's size is asked for first and a file bigger than the bound is not
# read at all.

#: How long a plain local git read may take.
READ_AT_COMMIT_TIMEOUT_SECONDS: float = 60.0

#: A file bigger than this is not read out of a commit. The caller's parse is
#: bounded too; this keeps the bytes off the wire in the first place.
MAX_FILE_AT_COMMIT_BYTES: int = 256 * 1024


@dataclass(frozen=True)
class FileAtCommit:
    """One file as it is at one commit, or why it could not be read.

    ``found`` is False when the commit is readable and simply does not carry
    that file — a fact about the project, not a fault. ``refusal`` is set only
    when the read itself could not be made: the commit is not in this copy, the
    file is too big to read, git could not be run, the venue could not be
    reached. ``content`` is set only when ``found`` is True and there is no
    refusal.
    """

    content: str | None = None
    found: bool = False
    refusal: str | None = None
    # True only when the caller asked for an ordinary file and the reader
    # checked the commit's tree entry and found one (4 October 2026). A
    # symbolic link stored in git reads back as its target's NAME, so a
    # document declared as binding could otherwise be satisfied by a link's
    # text. A caller that asked is told by this flag that the check was made —
    # a helper too old to make it never sets it.
    ordinary: bool = False
    # The commit's tree-entry mode for the path (``100644``, ``100755``,
    # ``120000`` for a symbolic link, ``040000`` for a folder, ``160000`` for a
    # submodule), set only by a RAW read (4 October 2026, the one reading rule
    # for a project's documents). A raw read of a link answers ``found`` with
    # the link's target NAME as ``content`` and this mode, so a caller follows a
    # link only when the reader says the entry IS one. A folder or submodule
    # answers ``found=False`` with its mode. A helper too old to make a raw read
    # never sets it.
    mode: str | None = None

    @property
    def ok(self) -> bool:
        """True when the read was made, whether or not the file was there."""
        return self.refusal is None

    @property
    def is_link(self) -> bool:
        """True when a raw read found a symbolic link at the path."""
        return self.found and self.mode == _LINK_MODE

    def to_wire(self) -> dict[str, Any]:
        """The answer as the sandbox's helper service sends it.

        ``ordinary`` is sent only when the check was asked for and made, so an
        answer to every other caller is exactly what it always was.
        """
        wire: dict[str, Any] = {
            "content": self.content,
            "found": self.found,
            "refusal": self.refusal,
        }
        if self.ordinary:
            wire["ordinary"] = True
        if self.mode is not None:
            wire["mode"] = self.mode
        return wire

    @classmethod
    def from_wire(cls, decoded: Any) -> "FileAtCommit":
        """The answer as it came back, or a refusal saying it made no sense."""
        if not isinstance(decoded, dict):
            return cls(refusal="the answer was not an object with a file in it")
        refusal = decoded.get("refusal")
        if isinstance(refusal, str) and refusal.strip():
            return cls(refusal=refusal.strip())
        found = bool(decoded.get("found"))
        content = decoded.get("content")
        raw_mode = decoded.get("mode")
        mode = raw_mode if isinstance(raw_mode, str) and raw_mode else None
        if not found:
            return cls(found=False, mode=mode)
        if not isinstance(content, str):
            return cls(
                refusal="the answer said the file is there but sent no contents"
            )
        return cls(
            content=content,
            found=True,
            ordinary=decoded.get("ordinary") is True,
            mode=mode,
        )


#: The tree-entry modes git gives an ordinary file (plain and executable).
#: A symbolic link is ``120000``; a submodule is ``160000``.
_ORDINARY_FILE_MODES: frozenset[str] = frozenset({"100644", "100755"})

#: The tree-entry mode of a symbolic link.
_LINK_MODE: str = "120000"


def _tree_entry(
    repo_root: Path, commit: str, file_path: str
) -> tuple[str | None, str | None, str | None]:
    """``(mode, object id, None)`` for ``file_path``'s entry at ``commit``,
    ``(None, None, None)`` when there is none, or ``(None, None, why)``."""
    listed = _run_git(
        repo_root,
        "ls-tree",
        "-z",
        f"{commit}^{{commit}}",
        "--",
        file_path,
        timeout=READ_AT_COMMIT_TIMEOUT_SECONDS,
    )
    if listed.returncode != 0:
        return None, None, _git_said(listed)
    # ``-z``: names are not quoted, entries end in NUL.
    for line in (listed.stdout or "").split("\0"):
        meta, _, name = line.partition("\t")
        if name == file_path:
            fields = meta.split()
            if len(fields) >= 3:
                return fields[0], fields[2], None
    return None, None, None


def _read_raw_at_commit_sync(
    repo_root: Path, commit: str, file_path: str, *, mode_only: bool = False
) -> FileAtCommit:
    """The RAW read: the tree entry's mode, then the blob's exact bytes.

    The one reading rule for a project's documents (4 October 2026): the bytes
    are ``git cat-file blob`` of the entry's object, decoded strictly as UTF-8
    (invalid UTF-8 is refused) with line endings kept, so the text's UTF-8
    encoding is the committed bytes exactly. A link answers its target's name
    with ``mode`` ``120000``; a folder or submodule answers ``found=False``
    with its mode; no entry answers ``found=False``. Only this read uses bytes:
    every other read of this module is unchanged.
    """
    where = str(repo_root)
    mode, oid, why = _tree_entry(repo_root, commit, file_path)
    if why is not None:
        return FileAtCommit(
            refusal=(
                f"git could not say what kind of file {file_path} is at "
                f"{commit} in {where}: {why}"
            )
        )
    if mode is None or oid is None:
        return FileAtCommit(found=False)
    if mode not in _ORDINARY_FILE_MODES and mode != _LINK_MODE:
        return FileAtCommit(found=False, mode=mode)
    if mode_only:
        # Existence and kind only (4 October 2026, Codex review round 2, R7):
        # a reference target need only be an ordinary committed file, so its
        # bytes are neither read nor decoded — a picture is a fine target.
        return FileAtCommit(
            content="", found=True, ordinary=mode in _ORDINARY_FILE_MODES, mode=mode
        )
    sized = _run_git(
        repo_root, "cat-file", "-s", oid, timeout=READ_AT_COMMIT_TIMEOUT_SECONDS
    )
    try:
        size = int((sized.stdout or "").strip()) if sized.returncode == 0 else -1
    except ValueError:
        size = -1
    if size < 0:
        return FileAtCommit(
            refusal=(
                f"git could not say how big {file_path} is at {commit} in "
                f"{where}: {_git_said(sized)}"
            )
        )
    if size > MAX_FILE_AT_COMMIT_BYTES:
        return FileAtCommit(
            refusal=(
                f"{file_path} at {commit} is {size} bytes, larger than the "
                f"{MAX_FILE_AT_COMMIT_BYTES} this factory will read"
            )
        )
    shown = run_git(
        ["-C", str(repo_root), "cat-file", "blob", oid],
        text=False,
        timeout=READ_AT_COMMIT_TIMEOUT_SECONDS,
    )
    if shown.returncode != 0:
        return FileAtCommit(
            refusal=(
                f"{file_path} could not be read at {commit} in {where}: "
                f"{failure_sentence('cat-file', shown.returncode)}"
            )
        )
    try:
        text = (shown.stdout or b"").decode("utf-8")
    except UnicodeDecodeError as exc:
        return FileAtCommit(
            refusal=(
                f"{file_path} at {commit} is not UTF-8 text (invalid byte at "
                f"position {exc.start})"
            )
        )
    return FileAtCommit(
        content=text,
        found=True,
        ordinary=mode in _ORDINARY_FILE_MODES,
        mode=mode,
    )


def _tree_entry_mode(
    repo_root: Path, commit: str, file_path: str
) -> tuple[str | None, str | None]:
    """``(mode, None)`` for ``file_path``'s entry at ``commit``, or ``(None, why)``.

    ``git ls-tree <commit> -- <path>`` prints ``<mode> <type> <id>\t<path>``.
    No entry is ``(None, None)``.
    """
    listed = _run_git(
        repo_root,
        "ls-tree",
        f"{commit}^{{commit}}",
        "--",
        file_path,
        timeout=READ_AT_COMMIT_TIMEOUT_SECONDS,
    )
    if listed.returncode != 0:
        return None, _git_said(listed)
    for line in (listed.stdout or "").splitlines():
        meta, _, name = line.partition("\t")
        if name == file_path:
            return (meta.split() or [None])[0], None
    return None, None


def read_file_at_commit_sync(
    repo_root: Path,
    commit: str,
    file_path: str,
    *,
    ordinary_file_only: bool = False,
    raw: bool = False,
    mode_only: bool = False,
) -> FileAtCommit:
    """The three steps, in order: is the commit here, is the file, read it.

    The same read as :func:`read_file_at_commit`, for a caller that has no
    event loop to await in. ONE reader serves both venues on purpose: the
    helper that launches a project's own commands and the coordinator that
    composes what it sends read a project's declarations through this one
    function, so they cannot come to disagree about what the project said at
    a commit.

    ``ordinary_file_only`` (4 October 2026): the commit's tree entry for the
    path is checked first, and a symbolic link is refused in plain words
    rather than read — ``git show`` would otherwise hand back the link's
    target name as if it were the file. A successful read then says
    ``ordinary=True``. Without it, nothing extra is asked of git.

    ``raw`` (4 October 2026, the one reading rule for a project's documents):
    after the commit check, the read is :func:`_read_raw_at_commit_sync` —
    exact bytes, strict UTF-8, and the entry's ``mode`` reported. It takes
    precedence over ``ordinary_file_only``; the caller judges the mode.
    ``mode_only`` (with ``raw``) answers the entry's existence and mode with
    empty content: nothing is read out of the blob.
    """
    where = str(repo_root)
    try:
        present = _run_git(
            repo_root,
            "rev-parse",
            "--verify",
            "--quiet",
            f"{commit}^{{commit}}",
            timeout=READ_AT_COMMIT_TIMEOUT_SECONDS,
        )
        if present.returncode != 0 or not (present.stdout or "").strip():
            return FileAtCommit(
                refusal=(
                    f"the copy of this project at {where} does not have the "
                    f"commit {commit}, so nothing can be read out of it"
                )
            )

        if raw or mode_only:
            return _read_raw_at_commit_sync(
                repo_root, commit, file_path, mode_only=mode_only
            )

        # A FILE, not a folder (4 October 2026, coach finding). ``cat-file -s``
        # answers for a folder too (its tree object has a size) and ``git show``
        # then prints the folder's listing as if it were the file, so a folder
        # used to read back as "found". The object's type is asked first: only
        # a blob is a file. No such path, and a folder, are both "not there".
        typed = _run_git(
            repo_root,
            "cat-file",
            "-t",
            f"{commit}:{file_path}",
            timeout=READ_AT_COMMIT_TIMEOUT_SECONDS,
        )
        if typed.returncode != 0 or (typed.stdout or "").strip() != "blob":
            return FileAtCommit(found=False)

        sized = _run_git(
            repo_root,
            "cat-file",
            "-s",
            f"{commit}:{file_path}",
            timeout=READ_AT_COMMIT_TIMEOUT_SECONDS,
        )
        if sized.returncode != 0:
            # The blob was there a moment ago; whatever git now says, the
            # file cannot be read as one.
            return FileAtCommit(found=False)
        try:
            size = int((sized.stdout or "").strip())
        except ValueError:
            size = -1
        if size > MAX_FILE_AT_COMMIT_BYTES:
            return FileAtCommit(
                refusal=(
                    f"{file_path} at {commit} is {size} bytes, larger than the "
                    f"{MAX_FILE_AT_COMMIT_BYTES} this factory will read"
                )
            )

        if ordinary_file_only:
            mode, why = _tree_entry_mode(repo_root, commit, file_path)
            if why is not None:
                return FileAtCommit(
                    refusal=(
                        f"git could not say what kind of file {file_path} is at "
                        f"{commit} in {where}: {why}"
                    )
                )
            if mode == "120000":
                return FileAtCommit(
                    refusal=(
                        f"{file_path} at {commit} is a symbolic link, not an "
                        f"ordinary file; a document the project's builds are "
                        f"held to must be the file itself"
                    )
                )
            if mode not in _ORDINARY_FILE_MODES:
                return FileAtCommit(found=False)

        shown = _run_git(
            repo_root,
            "show",
            f"{commit}:{file_path}",
            timeout=READ_AT_COMMIT_TIMEOUT_SECONDS,
        )
        if shown.returncode != 0:
            return FileAtCommit(
                refusal=(
                    f"{file_path} could not be read at {commit} in {where}: "
                    f"{_git_said(shown)}"
                )
            )
        return FileAtCommit(
            content=shown.stdout or "", found=True, ordinary=ordinary_file_only
        )
    except OSError as exc:
        return FileAtCommit(
            refusal=f"git could not be run in {where}: {type(exc).__name__}: {exc}"
        )
    except subprocess.TimeoutExpired:
        return FileAtCommit(
            refusal=(
                f"reading {file_path} at {commit} in {where} did not finish "
                f"within {READ_AT_COMMIT_TIMEOUT_SECONDS:.0f} seconds"
            )
        )


# ---------------------------------------------------------------------------
# A whole tree's listing at one commit: names, modes, types and object ids,
# never any file's content (15 October 2026, the merge-check design)
# ---------------------------------------------------------------------------

#: The most entries one listing answers with. A tree bigger than this is a
#: refusal in plain words rather than a partial answer, because a listing that
#: stopped early could hide the one entry that mattered.
MAX_TREE_ENTRIES: int = 200_000


@dataclass(frozen=True)
class TreeEntry:
    """One leaf of a commit's tree, as ``git ls-tree -r`` prints it. ``path``
    keeps git's exact bytes through ``os.fsdecode``, as GuardKit's merge does,
    so a name that is not UTF-8 is still its own path."""

    path: str
    mode: str
    kind: str
    object: str


@dataclass(frozen=True)
class TreeListing:
    """Every leaf of one commit's tree, or why it could not be listed."""

    entries: tuple[TreeEntry, ...] | None = None
    refusal: str | None = None

    @property
    def ok(self) -> bool:
        return self.refusal is None and self.entries is not None

    def to_wire(self) -> dict[str, Any]:
        """The JSON answer. Each name travels as base64 of its raw bytes
        (review R2, 15 October 2026): a name git stores that is not valid
        UTF-8 is carried exactly, so two different names can never arrive as
        one. ``names`` says so, and a reader refuses an answer without it."""
        return {
            "entries": (
                [
                    [
                        e.mode,
                        e.kind,
                        e.object,
                        base64.b64encode(os.fsencode(e.path)).decode("ascii"),
                    ]
                    for e in self.entries
                ]
                if self.entries is not None
                else None
            ),
            "names": "base64",
            "refusal": self.refusal,
        }

    @classmethod
    def from_wire(cls, decoded: Any) -> "TreeListing":
        if not isinstance(decoded, dict):
            return cls(refusal="the listing came back in a shape the factory does not read")
        if decoded.get("refusal"):
            return cls(refusal=str(decoded["refusal"]))
        raw = decoded.get("entries")
        if not isinstance(raw, list):
            return cls(refusal="the listing came back with no entries field")
        if decoded.get("names") != "base64":
            return cls(
                refusal=(
                    "the listing came back without its file names carried "
                    "exactly; the sandbox's helper service may be older than "
                    "this factory"
                )
            )
        entries: list[TreeEntry] = []
        for item in raw:
            if not (isinstance(item, list) and len(item) == 4 and all(isinstance(x, str) for x in item)):
                return cls(refusal="the listing came back with an entry the factory does not read")
            mode, kind, obj, carried = item
            try:
                name = base64.b64decode(carried.encode("ascii"), validate=True)
            except (ValueError, UnicodeEncodeError):
                return cls(refusal="the listing came back with a file name the factory does not read")
            if not name:
                return cls(refusal="the listing came back with an empty file name")
            entries.append(TreeEntry(path=os.fsdecode(name), mode=mode, kind=kind, object=obj))
        return cls(entries=tuple(entries))


def list_tree_at_commit_sync(repo_root: Path, commit: str) -> TreeListing:
    """``git ls-tree -r -z --full-tree <commit>``: every leaf of the commit's
    tree with its mode, type and object id. Nothing is read out of any file.
    Never raises; a commit this copy does not have is a refusal."""
    where = str(repo_root)
    try:
        present = _run_git(
            repo_root,
            "rev-parse",
            "--verify",
            "--quiet",
            f"{commit}^{{commit}}",
            timeout=READ_AT_COMMIT_TIMEOUT_SECONDS,
        )
        if present.returncode != 0 or not (present.stdout or "").strip():
            return TreeListing(
                refusal=(
                    f"the copy of this project at {where} does not have the "
                    f"commit {commit}, so its files cannot be listed"
                )
            )
        # Read as bytes (review R2): a name is kept byte-exact with
        # ``os.fsdecode``, as GuardKit's merge keeps it, so two different
        # names never become one.
        done = run_git(
            [
                "-C",
                where,
                "ls-tree",
                "-r",
                "-z",
                "--full-tree",
                f"{commit}^{{commit}}",
            ],
            text=False,
            timeout=READ_AT_COMMIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return TreeListing(
            refusal=(
                f"listing the files at {commit} did not finish within "
                f"{READ_AT_COMMIT_TIMEOUT_SECONDS:.0f} seconds"
            )
        )
    except OSError as exc:
        return TreeListing(refusal=f"git could not be run to list the files ({exc.strerror or exc})")
    if done.returncode != 0:
        return TreeListing(refusal=f"git could not list the files at {commit} (exit {done.returncode})")
    entries: list[TreeEntry] = []
    for record in (done.stdout or b"").split(b"\0"):
        if not record:
            continue
        meta, _tab, path = record.partition(b"\t")
        parts = meta.split()
        if len(parts) != 3 or not path:
            return TreeListing(refusal=f"git listed the files at {commit} in a shape the factory does not read")
        entries.append(
            TreeEntry(
                path=os.fsdecode(path),
                mode=os.fsdecode(parts[0]),
                kind=os.fsdecode(parts[1]),
                object=os.fsdecode(parts[2]),
            )
        )
        if len(entries) > MAX_TREE_ENTRIES:
            return TreeListing(
                refusal=f"the commit {commit} has more than {MAX_TREE_ENTRIES} files, too many to list here"
            )
    return TreeListing(entries=tuple(entries))


async def list_tree_at_commit(repo_root: Path | str, commit: str) -> TreeListing:
    """:func:`list_tree_at_commit_sync`, off the event loop."""
    return await asyncio.to_thread(list_tree_at_commit_sync, Path(repo_root), str(commit))


async def read_file_at_commit(
    repo_root: Path | str,
    commit: str,
    file_path: str,
    *,
    ordinary_file_only: bool = False,
    raw: bool = False,
    mode_only: bool = False,
) -> FileAtCommit:
    """Read ``file_path`` exactly as it is at ``commit``.

    Never raises, never writes, never touches the working folder and never
    changes the branch the copy has checked out: it is ``git show`` and two
    plumbing probes (three with ``ordinary_file_only``, which refuses a
    symbolic link). "The file is not in that commit" is an answer, not a
    refusal; "the commit is not in this copy" is a refusal.
    """
    return await asyncio.to_thread(
        partial(
            read_file_at_commit_sync,
            Path(repo_root),
            str(commit),
            str(file_path),
            ordinary_file_only=ordinary_file_only,
            raw=raw,
            mode_only=mode_only,
        )
    )


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
    """Stream ``git archive <sha>`` into ``dest``; raise with a fixed sentence.

    Through the one git helper's streaming reader (14 October 2026): a
    project's smudge filter is switched off, and git's own words are never
    repeated.
    """
    try:
        proc = open_git(["archive", "--format=tar", sha], cwd=repo_root)
    except OSError as exc:
        raise CandidateTreeError(
            f"git archive could not be run in {repo_root}: {exc}"
        ) from exc
    assert proc.stdout is not None
    extract_error: Exception | None = None
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as archive:
            archive.extractall(dest, filter="data")
    except (tarfile.TarError, OSError) as exc:
        extract_error = exc
    finally:
        proc.stdout.close()
        returncode = proc.wait()
    if returncode != 0:
        raise CandidateTreeError(
            f"git archive {sha} failed: {failure_sentence('archive', returncode)}"
        )
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
        # Through the one git helper (14 October 2026).
        proc = await run_git_async(list(args), cwd=root)
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


@dataclass(frozen=True)
class WorkingFolder:
    """A working folder of its own, made at a named commit — or why not.

    One-true-copy design pass, item 1, "Every step happens in a working folder
    of its own". The merge used to switch the branch checked out in the
    project's main copy; now it is done in a git worktree made at the commit
    being joined onto, so the main copy is never switched, reset or merged
    into by the factory at all.

    ``ok`` is True when the folder is there — ``reused`` says whether this
    call made it or found it already made, which is exactly what a pick-up
    needs to know. ``refusal`` carries one plain sentence when it is not.
    """

    ok: bool
    path: str | None = None
    branch: str | None = None
    base_ref: str | None = None
    reused: bool = False
    refusal: str | None = None


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

    async def fetch_remote_start_point(
        self, branch: str | None = None
    ) -> RemoteStartPoint:
        """Fetch the remote named ``origin`` and say where its default branch is.

        The starting rule's one operation (one true copy, item 1): the answer
        is a branch and a commit, or a plain-sentence refusal. It never raises,
        never changes a checked-out branch and never touches a working folder.
        ``branch`` also fetches that branch and answers its commit as
        ``branch_commit``.
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

    async def add_working_folder(
        self, leaf: str, branch: str, base_ref: str
    ) -> WorkingFolder:
        """Make a working folder at ``base_ref``, on a new branch ``branch``.

        ``leaf`` is the folder's own NAME, never a path: the venue decides
        where the repository is, because only the venue knows. A repository
        that lives in a sandbox has a path in there that has nothing to do
        with the path the coordinator knows it by, and a path sent from this
        side would be a path into somewhere else entirely. The answer carries
        the path the folder really has, where the folder really is.

        The merge word's join happens in here. Never raises: everything that
        can go wrong comes back as ``ok=False`` with a plain sentence.
        """

    async def remove_working_folder(self, path: str) -> bool:
        """Remove a working folder. A path already gone is a success."""

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

    def __init__(
        self,
        repo_root: Path | str,
        *,
        fetch_address: str | None = None,
        repo: str | None = None,
    ) -> None:
        self._repo_root = Path(repo_root)
        # The project's recorded fetch address (planning.fetch_addresses) and
        # its org/name; the starting point is fetched from that address only.
        self._fetch_address = fetch_address
        self._repo = repo

    @property
    def repo_root(self) -> Path:
        return self._repo_root

    @property
    def venue(self) -> str:
        """Where the work happened, for a sentence a person reads."""
        return f"in {self._repo_root}"

    async def rev_parse(self, ref: str) -> str | None:
        return await git_rev_parse(self._repo_root, ref)

    async def fetch_remote_start_point(
        self, branch: str | None = None
    ) -> RemoteStartPoint:
        return await fetch_remote_start_point(
            self._repo_root, branch, fetch_address=self._fetch_address, repo=self._repo
        )

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

    async def add_working_folder(
        self, leaf: str, branch: str, base_ref: str
    ) -> WorkingFolder:
        """``git worktree add -b <branch> <repo>/.forge/worktrees/<leaf> <base_ref>``.

        The same function the planning chain already uses to cut a journey's
        tree (:func:`forge.cli._conductor_worktree.cut_worktree_in_checkout`),
        called with the commit to start from. Nothing new had to be built for
        this and no new route had to be added: the operation the factory
        already has does exactly this.
        """
        from forge.cli._conductor_worktree import (
            WORKTREES_DIR,
            cut_worktree_in_checkout,
        )

        leaf = Path(str(leaf)).name
        path = self._repo_root / WORKTREES_DIR / leaf
        try:
            cut = await cut_worktree_in_checkout(
                checkout=self._repo_root,
                build_id=leaf,
                branch=str(branch),
                base_ref=str(base_ref),
            )
        except Exception as exc:  # noqa: BLE001 — a refusal, never an exception
            return WorkingFolder(
                ok=False,
                branch=str(branch),
                base_ref=str(base_ref),
                refusal=(
                    f"a working folder for the join could not be made at "
                    f"{path}: {type(exc).__name__}: {exc}"
                ),
            )
        return WorkingFolder(
            ok=bool(cut.ok),
            path=cut.path or (str(path) if cut.ok else None),
            branch=cut.branch,
            base_ref=cut.base_ref,
            reused=bool(cut.reused),
            refusal=None if cut.ok else (cut.reason or "the folder was not made"),
        )

    async def remove_working_folder(self, path: str) -> bool:
        from forge.cli._conductor_worktree import remove_journey_worktree

        try:
            removed = await remove_journey_worktree(worktree=str(path))
        except Exception as exc:  # noqa: BLE001 — cleanup never raises past here
            logger.warning(
                "working folder: %s could not be removed (%s: %s)",
                path,
                type(exc).__name__,
                exc,
            )
            return False
        if not removed.ok:
            logger.warning(
                "working folder: %s could not be removed (%s)", path, removed.reason
            )
        return bool(removed.ok)

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

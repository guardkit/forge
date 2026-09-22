"""The join — the merge word makes a commit of the remote's work and the build's.

One-true-copy design pass, 21 September 2026, item 1, "The merge word" and
"Every step happens in a working folder of its own"; the first revision's item
1; the second revision's section A.

WHAT CHANGED. The merge word used to merge the build's branch into whatever
the factory's own copy had checked out, in that copy, and call the result
"merged and running". Now:

1. the branch of the remote this work is aimed at — the one recorded when the
   work STARTED, never one chosen now — is fetched, and the commit it is at is
   called **G**;
2. a working folder of its own is made at G, on a branch of its own;
3. the build system's merge is run IN THAT FOLDER, which produces a new commit
   with two parents, G and the build's own tip. That commit is called **J**;
4. everything after this is about J, and never about the build's own commit.

The project's main copy is never switched, reset or merged into. That is the
whole point of the working folder: the copy becomes what git intends it to be
here, a local store of commits that working folders are cut from, and several
people and the factory can share one repository in the ordinary way.

PICKING UP. A join can finish a moment before the line saying so is written.
So when a record's last line is an "about to" for the join, the factory does
not assume either way: it asks whether this attempt's branch exists and
whether it is a merge of exactly G and the build's tip. If it is, that is J
and the step is marked done. If it is anything else, the leftover is set aside
under a name of its own — never deleted, never reused — and the join is made
afresh on the next attempt.

AGNOSTIC. Nothing here names a language, a test runner, a web protocol, a
database, a package manager or a host. Two facts are used: there is a remote
named ``origin``, and the ledger recorded which branch of it this work is
aimed at.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forge.deploy.candidate_tree import CandidateGit, RemoteStartPoint

logger = logging.getLogger(__name__)

__all__ = [
    "INTEGRATION_BRANCH_PREFIX",
    "JoinOutcome",
    "LeftoverJoin",
    "integration_branch",
    "working_folder_leaf",
    "working_folder_path",
    "look_at_the_leftover_join",
    "make_the_working_folder",
    "target_branch_now",
]

#: Every joined branch the factory makes starts with this. One prefix, so a
#: person, and a later clean-up, can tell the factory's integration branches
#: from anything anybody else made.
INTEGRATION_BRANCH_PREFIX: str = "factory-integration/"

#: A feature id is put in a branch name and in one path segment, so it has to
#: be plain: letters, digits, dots, dashes and underscores, starting with a
#: letter or a digit.
_PLAIN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class JoinRefused(ValueError):
    """The join was asked for something it will not put on a git command line."""


def _plain(feature_id: str) -> str:
    name = str(feature_id or "").strip()
    if not _PLAIN_NAME.match(name):
        raise JoinRefused(
            f"the join needs a feature name that is one plain word (letters, "
            f"digits, dots, dashes and underscores), not {feature_id!r}"
        )
    return name


def integration_branch(feature_id: str, attempt: int = 1) -> str:
    """The branch this attempt's join is made on.

    The first attempt is ``factory-integration/<FEAT>``, which is the name the
    design writes. A later attempt — one made because the remote moved under a
    send, or because a leftover had to be set aside — gets its own name, so
    that every joined commit of every attempt is kept under a name of its own
    until the build's record reaches its end. Nothing is ever overwritten.
    """
    name = _plain(feature_id)
    if int(attempt) <= 1:
        return f"{INTEGRATION_BRANCH_PREFIX}{name}"
    return f"{INTEGRATION_BRANCH_PREFIX}{name}-attempt-{int(attempt)}"


# HOW A LEFTOVER IS "SET ASIDE UNDER ITS OWN NAME". It already is. Each
# attempt has a branch and a folder of its own, so a half-finished join simply
# stays where it is, under the name that attempt gave it, and the fresh join
# is made on the next attempt's name. Nothing is renamed, nothing is deleted
# and nothing is overwritten — which is what the design asks for, and it needs
# no operation the factory did not already have.


def working_folder_leaf(feature_id: str, attempt: int = 1) -> str:
    """The folder's own name, one plain path segment."""
    name = _plain(feature_id)
    if int(attempt) <= 1:
        return f"integration-{name}"
    return f"integration-{name}-{int(attempt)}"


def working_folder_path(repo_root: Path | str, feature_id: str, attempt: int = 1) -> str:
    """Where the join's working folder goes: ``<repo>/.forge/worktrees/<leaf>``.

    The same place the planning chain's own trees go, because it is the same
    kind of thing and the venue's existing operation already acts there and
    nowhere else. The text is identical on both routes: a sandbox holds its
    clone at the path the checkout has on this side.
    """
    from forge.cli._conductor_worktree import WORKTREES_DIR

    return str(Path(repo_root) / WORKTREES_DIR / working_folder_leaf(feature_id, attempt))


# ---------------------------------------------------------------------------
# Which branch of the remote, and where it is now
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TargetNow:
    """The recorded target branch, and the commit it is at now — or a refusal.

    ``commit`` is G. ``refusal`` is one plain sentence when there is nothing
    to join onto: no remote, a remote that could not be reached, or — the case
    the design calls out by name — a remote whose default branch is no longer
    the one this work was started against. That last one is not a guess to be
    made now; it is said, and the press stops.
    """

    branch: str | None = None
    commit: str | None = None
    refusal: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.branch and self.commit and not self.refusal)


async def target_branch_now(
    git: CandidateGit, *, recorded_branch: str | None
) -> TargetNow:
    """Fetch the remote and say where the RECORDED target branch is.

    One target branch, decided once: the name comes off the record the work
    started with and is used for the fetch, the join, and everything after. If
    the remote's default branch has moved to a different name since, that is
    said in plain words rather than quietly followed.

    A build with nothing recorded — every build queued before the starting
    rule — is refused here rather than started from a guess, because the whole
    point of the join is that it is made onto a branch somebody wrote down.
    """
    recorded = str(recorded_branch or "").strip()
    if not recorded:
        return TargetNow(
            refusal=(
                "this build has no target branch on its record, so there is "
                "nothing to join its work onto. It was started before the "
                "factory began writing down which branch of the remote a piece "
                "of work is aimed at; run it again and it will be recorded."
            )
        )
    start: RemoteStartPoint = await git.fetch_remote_start_point()
    if start.refusal:
        return TargetNow(branch=recorded, refusal=start.refusal)
    if (start.branch or "") != recorded:
        return TargetNow(
            branch=recorded,
            refusal=(
                f"this work is aimed at the branch '{recorded}', but the remote "
                f"named 'origin' now says its default branch is "
                f"'{start.branch}'. Nothing was joined and nothing was sent: "
                f"which branch the work belongs on is a decision for a person, "
                f"not a guess for the factory."
            ),
        )
    return TargetNow(branch=recorded, commit=start.commit)


# ---------------------------------------------------------------------------
# The working folder
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JoinOutcome:
    """Where the join's working folder is, or why there is not one."""

    ok: bool
    path: str | None = None
    branch: str | None = None
    reused: bool = False
    refusal: str | None = None


async def make_the_working_folder(
    git: CandidateGit,
    *,
    repo_root: Path | str,
    feature_id: str,
    attempt: int,
    at_commit: str,
) -> JoinOutcome:
    """A working folder of its own, at G, on this attempt's branch.

    Through the venue the press was told to use, so a repository that lives in
    a sandbox has its folder made in there and a repository that does not has
    it made here. The operation is the one the factory already had; no route
    was added for this.
    """
    branch = integration_branch(feature_id, attempt)
    leaf = working_folder_leaf(feature_id, attempt)
    # Where it will be on THIS side, for the record and the receipt. The venue
    # answers where it really is, which for a sandboxed repository is a path
    # inside that sandbox.
    path = working_folder_path(repo_root, feature_id, attempt)
    made = await git.add_working_folder(leaf, branch, str(at_commit))
    if not made.ok:
        return JoinOutcome(
            ok=False,
            path=path,
            branch=branch,
            refusal=(
                f"a working folder for the join could not be made at {path} "
                f"on {branch} at {str(at_commit)[:10]}: "
                f"{made.refusal or 'no reason was given'}"
            ),
        )
    return JoinOutcome(
        ok=True,
        path=made.path or path,
        branch=made.branch or branch,
        reused=bool(made.reused),
    )


# ---------------------------------------------------------------------------
# Picking up: looking at what an interrupted join left behind
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LeftoverJoin:
    """What this attempt's branch turned out to be.

    ``is_the_join`` is True only when the branch is a commit with exactly two
    parents, the first G and the second the build's own tip — which is what
    the build system's merge makes, and nothing else does. Anything else,
    including a branch sitting at G with nothing merged into it, is
    ``set_aside``: the join did not finish, so it is kept under a name of its
    own and made afresh.
    """

    exists: bool
    is_the_join: bool = False
    commit: str | None = None
    parents: tuple[str | None, ...] = ()
    why: str | None = None

    @property
    def set_aside(self) -> bool:
        return self.exists and not self.is_the_join


async def look_at_the_leftover_join(
    git: CandidateGit,
    *,
    feature_id: str,
    attempt: int,
    g_commit: str,
    build_tip: str,
) -> LeftoverJoin:
    """Does this attempt's branch exist, and is it a merge of exactly G and the tip?

    Asked with the venue's ordinary "what commit is this" operation and no
    other: ``<branch>^1`` and ``<branch>^2`` are the two parents a merge
    commit has, and ``<branch>^3`` answering anything at all means it is not
    the two-parent commit the merge makes.
    """
    branch = integration_branch(feature_id, attempt)
    head = await git.rev_parse(branch)
    if not head:
        return LeftoverJoin(exists=False, why=f"{branch} does not exist")
    first = await git.rev_parse(f"{branch}^1")
    second = await git.rev_parse(f"{branch}^2")
    third = await git.rev_parse(f"{branch}^3")
    parents = (first, second, third)
    if third:
        return LeftoverJoin(
            exists=True,
            commit=head,
            parents=parents,
            why=f"{branch} has more than two parents, so it is not the join",
        )
    if not second:
        return LeftoverJoin(
            exists=True,
            commit=head,
            parents=parents,
            why=(
                f"{branch} is not a merge commit — the join had not been made "
                f"when the run stopped"
            ),
        )
    if first != str(g_commit) or second != str(build_tip):
        return LeftoverJoin(
            exists=True,
            commit=head,
            parents=parents,
            why=(
                f"{branch} is a merge of {str(first)[:10]} and "
                f"{str(second)[:10]}, not of {str(g_commit)[:10]} and "
                f"{str(build_tip)[:10]}"
            ),
        )
    return LeftoverJoin(exists=True, is_the_join=True, commit=head, parents=parents)


def join_inputs(
    *,
    feature_id: str,
    attempt: int,
    target_branch: str,
    g_commit: str,
    build_tip: str,
    branch_to_merge: str,
    folder: str,
) -> dict[str, Any]:
    """The exact inputs of one join attempt, for the record's "about to" line.

    Exact on purpose: a pick-up asks the world about THIS attempt, and it can
    only do that if the line says which branch, which commit and which folder
    the attempt used.
    """
    return {
        "feature_id": str(feature_id),
        "attempt": int(attempt),
        "target_branch": str(target_branch),
        "g_commit": str(g_commit),
        "build_tip": str(build_tip),
        "branch_merged": str(branch_to_merge),
        "integration_branch": integration_branch(feature_id, attempt),
        "working_folder": str(folder),
    }

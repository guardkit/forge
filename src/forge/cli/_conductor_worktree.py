"""The fix journey's worktree writer (conductor activation §1).

Why this module exists
----------------------

``builds.worktree_path`` had **zero write sites**. Three consumers read
it and all three — correctly — refuse to guess when it is NULL: the
conductor dispatcher refuses pre-spawn ("has no worktree path"), the
commit probe fails, and the gates reader answers UNKNOWN. So every
production fix-journey dispatch died before a single leg ran, not because
anything was broken but because nothing materialised the tree the journey
would run in.

The pipeline already owned a purpose-built materialiser —
:func:`forge.adapters.git.operations.prepare_worktree` (ADR-ARCH-028),
exported and never called. This module gives it the seat, with the one
change the design pass proved necessary: as built it could not CREATE a
branch, and the fix journey's whole point is a NEW per-journey branch cut
from the trunk (hence ``create_branch`` / ``base_ref``).

Where the tree goes, and why
----------------------------

``<registered-checkout>/.forge/worktrees/<build_id>`` on branch
``fix/<task_id>-<build8>`` off the branch the build was queued on — ``main``
by default, ``repair/<task id>`` when the repair admission put the task file
there, or whatever ``forge queue --mode c --branch`` named (Part L of the
2026-09-06 spec and Part M rule 56, 2026-09-07: a repair's task file and
YAML are committed on its branch, and the review leg loads its task from
the worktree, so the tree has to be cut from the branch that carries the
file; a tree cut from ``main`` does not have it, and journey one refused in
four seconds for exactly that reason). The branch it cuts is recorded on the
row as ``builds.merge_branch`` (Part M, rule 54), so the merge word later
merges the branch the build actually made rather than the feature's own
``autobuild/<feature id>``:

* The registered checkout is ALREADY inside
  ``permissions.filesystem.allowlist`` and ALREADY bind-mounted
  **same-path** into the daemon container, so a path recorded under it is
  true for the daemon AND for every host-side reader. The materialiser's
  designed default (``/var/forge/builds``) is mounted, but NOT same-path
  (``~/forge-state`` ↔ ``/var/forge``) — a path recorded there would be a
  lie to half the estate. Autobuild's ``/tmp/forge-autobuild-worktrees``
  is not allowlisted at all.
* The per-build branch SUFFIX is load-bearing: a bare ``fix/<task_id>``
  would collide forever on a second journey for the same task, because
  git refuses a branch that another worktree already has checked out.
* A NAMED branch, never ``--detach``: the build system's work leg detects
  the HEAD branch and a detached HEAD degrades that detection to a
  ``'main'`` fallback.
* The ``.forge/.gitignore`` guard (see :func:`_ensure_forge_gitignore`)
  is what stops a ``git add -A`` at the checkout root staging the live
  nested worktree as an embedded gitlink. The writer creates and verifies
  it wherever it runs, so the hazard is closed by the code that opens it
  rather than by an edit in somebody else's repository.

Every arm is loud
-----------------

The writer answers :class:`WorktreeReady` or :class:`WorktreeRefused` and
never raises. A refusal becomes a ``TakenTerminal`` at the router seam —
FAILED with the reason on the row, slot acked, ``build-failed`` emitted —
never a silent downgrade onto the routine path.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "WorktreeReady",
    "WorktreeRefused",
    "WorktreeOutcome",
    "WorktreeCut",
    "JOURNEY_BASE_REF",
    "WORKTREES_DIR",
    "cut_worktree_in_checkout",
    "journey_base_ref",
    "journey_branch_name",
    "journey_worktree_path",
    "prepare_journey_worktree",
    "remove_journey_worktree",
    "short_build_id",
]


#: The trunk a journey branch is cut from when the row names no branch —
#: :func:`journey_base_ref` otherwise names the row's own branch. Matches the
#: commit probe's ``base_branch`` default (``git rev-list --count
#: <base>..HEAD`` is the journey's ONLY commit evidence), and the probe reads
#: the base through the same function (:mod:`forge.pipeline.mode_c_commit_probe`),
#: so the branch the writer creates and the base the probe counts against are
#: one statement, not two.
JOURNEY_BASE_REF = "main"

#: Directory the writer owns inside a registered checkout. The worktrees
#: live under ``<checkout>/.forge/worktrees/``; the gitignore guard sits
#: at ``<checkout>/.forge/.gitignore``.
_FORGE_DIR = ".forge"
_WORKTREES_DIR = "worktrees"

#: The same two names as one relative directory, for the readers outside
#: this module that have to recognise a journey worktree path: the sidecar's
#: worktree routes will act on ``<repo>/.forge/worktrees/<build id>`` and on
#: nothing else (sandbox first, 2026-09-07, rule 76).
WORKTREES_DIR = f"{_FORGE_DIR}/{_WORKTREES_DIR}"

#: How long cutting one tree over the wire may take. A worktree add is a
#: checkout of one branch; two minutes is generous and still bounded.
SIDECAR_WORKTREE_TIMEOUT_S: float = 120.0

#: The guard file's content. ``*`` ignores everything under ``.forge/``
#: including the nested worktrees, which is exactly the embedded-gitlink
#: hazard being closed.
_GITIGNORE_CONTENT = "*\n"

#: Characters git tolerates in a ref component without argument. Anything
#: else in a task id or build id is replaced so a hostile-looking subject
#: can never smuggle a second ref path component (or a ``..``) into the
#: branch name.
_REF_SAFE = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"


@dataclass(frozen=True)
class WorktreeReady:
    """The journey's tree exists at :attr:`path` on :attr:`branch`.

    Attributes:
        path: Absolute path of the worktree, already recorded on
            ``builds.worktree_path``.
        branch: The journey branch checked out there.
        reused: ``True`` when a redelivery found its OWN earlier tree
            (path AND branch matched for this build) and reused it rather
            than materialising a second one.
        base_ref: What the journey branch was cut from — the branch the
            build was queued on (``main`` when the row names none).
    """

    path: str
    branch: str
    reused: bool = False
    base_ref: str = JOURNEY_BASE_REF


@dataclass(frozen=True)
class WorktreeRefused:
    """The journey may not open, with the one-line reason to carry.

    Attributes:
        reason: Non-blank one-line refusal. It lands on ``builds.error``
            and rides the ``TakenTerminal`` onto the emitted
            ``build-failed``, so it must say WHY without a database
            re-read.
    """

    reason: str

    def __post_init__(self) -> None:
        if not self.reason or not self.reason.strip():
            raise ValueError(
                "WorktreeRefused.reason must be a non-blank one-line refusal"
            )


WorktreeOutcome = WorktreeReady | WorktreeRefused


@dataclass(frozen=True)
class WorktreeCut:
    """What cutting (or reusing) one tree in one checkout produced.

    This is the half of the writer that touches the checkout: the reuse
    look-up, the base-branch check, the gitignore guard and ``git worktree
    add``. It is a separate answer from :class:`WorktreeOutcome` because
    the same work now runs in two places — in the forge container for a
    repository that has no sandbox, and inside the repository's sandbox,
    behind the deploy sidecar's ``/git/worktree-add`` route, for one that
    does (sandbox first, 2026-09-07, rule 76). The row-reading and the
    recording stay in :func:`prepare_journey_worktree` either way.

    Attributes:
        ok: The tree is there.
        path: Absolute path of the tree, as the side that made it saw it.
        branch: The branch checked out in it.
        base_ref: What that branch was cut from.
        reused: The tree was already there for this same build.
        reason: One plain sentence, set only when ``ok`` is ``False``.
    """

    ok: bool
    path: str = ""
    branch: str = ""
    base_ref: str = JOURNEY_BASE_REF
    reused: bool = False
    reason: str = ""


def short_build_id(build_id: str) -> str:
    """The build id's short form — its last eight ref-safe characters.

    Build ids are ``build-<feature_id>-<YYYYMMDDHHMMSS>``, so the tail is
    the queue timestamp: an operator reading ``fix/TASK-X-03142530`` off
    a branch listing can find the build without a lookup table, which a
    hash prefix would not allow (the §6 runbook's recovery step is
    ``git branch -D fix/<task_id>-<build8>``).

    Deterministic by construction: the SAME build id always yields the
    same suffix, which is what lets the reuse arm recognise its own
    earlier work on a redelivery.
    """
    safe = _ref_safe(build_id)
    return safe[-8:] if len(safe) >= 8 else (safe or "00000000")


def _ref_safe(value: str) -> str:
    """Make ``value`` legal as ONE git ref component.

    Two passes, both load-bearing: the character map (so a space or a
    ``~`` can never reach the argv), and the ``..`` collapse — git refuses
    a ref containing a double dot outright, and a subject that smuggled
    one in would turn every journey for that task into a materialise
    failure. Leading/trailing dots go the same way (also illegal), and a
    ``.lock`` tail is defused.
    """
    mapped = "".join(ch if ch in _REF_SAFE else "-" for ch in value)
    while ".." in mapped:
        mapped = mapped.replace("..", ".-")
    mapped = mapped.strip(".")
    if mapped.endswith(".lock"):
        mapped = f"{mapped}-ref"
    return mapped


def journey_branch_name(task_id: str, build_id: str) -> str:
    """``fix/<task_id>-<build8>`` — one journey, one branch, forever unique."""
    return f"fix/{_ref_safe(task_id)}-{short_build_id(build_id)}"


def journey_base_ref(row_branch: Any) -> str:
    """The commit-ish the journey branch is cut from: the row's own branch.

    A mode-C row's ``branch`` is the branch it was queued on, and that is
    its base (Part M, rule 56): ``main`` when the row says ``main``, the
    ``repair/<task id>`` branch the repair admission put the task file on
    (Part L, rule 48), or whatever ``forge queue --mode c --branch`` named.
    The review leg loads its task from the worktree, so a tree cut from
    anywhere but the branch that carries the task file would not have it and
    the journey would refuse in its first leg — which is what journey one
    did, and what a repair queued on ``lane/x`` with the file did next. A
    row that names no branch at all is cut from ``main``
    (:data:`JOURNEY_BASE_REF`).
    """
    base = str(row_branch or "").strip()
    return base or JOURNEY_BASE_REF


def _sandbox_for(config: Any, repo: Any) -> Any | None:
    """The repository's sandbox entry, or ``None`` when it has none.

    ``planning.sandboxes`` is empty by default, and a repository that is not
    in it is handled exactly as it always was — in the forge container, on
    the operator's checkout. Never raises: a config shape that carries no
    such field simply has no sandboxes.
    """
    sandboxes = getattr(getattr(config, "planning", None), "sandboxes", None) or {}
    try:
        return sandboxes.get(str(repo))
    except AttributeError:  # pragma: no cover — a mapping is what the model gives
        return None


def _refuse(reason: str, *, log: logging.Logger) -> WorktreeRefused:
    log.error("conductor worktree: %s", reason)
    return WorktreeRefused(reason=reason)


def _normalise(path: Any) -> str:
    """Absolute, ``..``-free string form — the CONTAINMENT comparison unit.

    Deliberately does NOT resolve symlinks: this is the same spelling the
    estate's own ``_normalise_root`` uses for the filesystem allowlist, and
    the two must agree or a path this writer clears would be a path the
    leg's cwd check then refuses (or vice versa). Identity comparisons use
    :func:`_realpath` instead.
    """
    return os.path.normpath(os.path.abspath(str(path)))


def _realpath(path: Any) -> str:
    """Symlink-resolved string form — the IDENTITY comparison unit.

    ``git worktree list --porcelain`` reports the REALPATH of every
    registration. So when a registered checkout is reached through a
    symlink (a very ordinary estate shape — ``~/repos/x`` pointing at a
    volume), the writer's own target path and git's answer are two
    spellings of ONE directory, and a textual comparison calls them
    different: the reuse arm then misses, the branch match fires, and every
    redelivery of a build refuses as "branch busy at another path" — its
    own tree mistaken for a stranger's. Both sides resolve here; the
    allowlist check above stays on :func:`_normalise`.
    """
    return os.path.realpath(str(path))


def _is_inside_allowlist(path: Path, allowlist: "list[Any]") -> bool:
    """Is ``path`` contained in one of the operator-declared roots?

    Resolved-path + :func:`os.path.commonpath` containment, NOT a textual
    ``startswith``: ``/work/build-1`` must not admit ``/work/build-12345``.
    The path need not exist yet — this is checked at WRITE time, before
    the tree is materialised, which is the whole point (the leg's cwd is
    checked against the same allowlist later, and a tree that fails there
    would already be on disk).
    """
    candidate = _normalise(path)
    for entry in allowlist:
        root = _normalise(entry)
        try:
            if os.path.commonpath([candidate, root]) == root:
                return True
        except ValueError:  # pragma: no cover - different drives (win32)
            continue
    return False


def _ensure_forge_gitignore(forge_dir: Path) -> str | None:
    """Create/verify ``<forge_dir>/.gitignore`` so ``.forge/`` is never staged.

    THE embedded-gitlink cure, owned by the writer rather than by an edit
    in each target repository: a live nested worktree under a checkout is
    staged by a repo-root ``git add -A`` as an embedded gitlink, which is
    how a fix journey's private tree ends up in somebody's commit. A
    ``.gitignore`` containing ``*`` inside the directory the writer itself
    creates closes it wherever the writer runs — no other repo is touched,
    and a freshly-cloned checkout is protected the first time a journey
    lands in it.

    Returns:
        ``None`` when the guard is in place, else a one-line reason. An
        EXISTING file that does not ignore everything is a refusal, not a
        silent overwrite: the operator put it there, and materialising
        into an unguarded ``.forge/`` would re-open the hazard.
    """
    guard = forge_dir / ".gitignore"
    try:
        forge_dir.mkdir(parents=True, exist_ok=True)
        if not guard.exists():
            guard.write_text(_GITIGNORE_CONTENT, encoding="utf-8")
            logger.info(
                "conductor worktree: wrote the %s guard (ignore-everything) so "
                "a repo-root 'git add -A' can never stage a journey worktree "
                "as an embedded gitlink",
                guard,
            )
            return None
        lines = [
            line.strip()
            for line in guard.read_text(encoding="utf-8").splitlines()
        ]
    except OSError as exc:
        return (
            f"the {guard} gitignore guard could not be verified or created "
            f"({type(exc).__name__}: {exc}); refusing rather than materialising "
            "a worktree a 'git add -A' could stage as an embedded gitlink"
        )
    if "*" not in lines:
        return (
            f"{guard} exists but does not ignore everything (no bare '*' line), "
            "so a repo-root 'git add -A' could stage this journey's worktree as "
            "an embedded gitlink; refusing rather than overwriting an operator's "
            "file"
        )
    return None


def _hollow_worktree_reason(
    path: str, build_id: str, branch: str, checkout: Path
) -> str | None:
    """Is the reuse candidate a REAL checkout on disk, or only a record?

    The reuse arm's match is made against ``git worktree list``, which
    answers from the administrative records under ``.git/worktrees/`` —
    records that outlive the directory they describe. Deleted by hand,
    wiped by a cleanup script, lost with a tmpfs: the listing still names
    the path, and this writer would hand back
    :class:`WorktreeReady` for a tree that is not there.

    Returns:
        ``None`` when ``path`` is a directory containing a ``.git`` entry
        (a linked worktree's ``.git`` is a FILE holding ``gitdir: …``, so
        existence is the test, not directory-ness), else a one-line reason
        naming the missing or hollow path and the recovery.
    """
    recovery = (
        f"recover with 'git worktree prune' in {checkout} (and 'git branch -D "
        f"{branch}' if the branch survives), then re-queue"
    )
    if not os.path.isdir(path):
        return (
            f"git still registers {path} as build_id={build_id}'s worktree on "
            f"branch {branch}, but there is no such directory on disk — the "
            "registration outlived the tree. Refusing rather than reporting a "
            f"ready worktree the journey's first leg would not find; {recovery}"
        )
    if not os.path.exists(os.path.join(path, ".git")):
        return (
            f"git still registers {path} as build_id={build_id}'s worktree on "
            f"branch {branch}, but that directory is hollow — it carries no "
            "'.git' entry, so it is not a checkout git can work in. Refusing "
            f"rather than reusing an empty shell of an earlier tree; {recovery}"
        )
    return None


def _parse_worktree_list(porcelain: str) -> "list[tuple[str, str | None]]":
    """Parse ``git worktree list --porcelain`` into ``(path, branch)`` pairs.

    ``branch`` is the short name (``refs/heads/x`` → ``x``) or ``None``
    for a detached-HEAD registration.
    """
    entries: list[tuple[str, str | None]] = []
    path: str | None = None
    branch: str | None = None
    for raw in porcelain.splitlines():
        line = raw.rstrip("\n")
        if line.startswith("worktree "):
            if path is not None:
                entries.append((path, branch))
            path = line[len("worktree ") :].strip()
            branch = None
        elif line.startswith("branch "):
            ref = line[len("branch ") :].strip()
            branch = ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref
    if path is not None:
        entries.append((path, branch))
    return entries


def journey_worktree_path(checkout: "Path | str", build_id: str) -> Path:
    """``<checkout>/.forge/worktrees/<build id>`` — the one place a journey's
    tree is ever made.

    Named in one function because two sides now have to agree on it: this
    writer, and the deploy sidecar's ``/git/worktree-add`` route, which acts
    on this path under the repository it was given and refuses every other
    path (sandbox first, 2026-09-07, rule 76).
    """
    return Path(checkout) / _FORGE_DIR / _WORKTREES_DIR / build_id


async def cut_worktree_in_checkout(
    *,
    checkout: "Path | str",
    build_id: str,
    branch: str,
    base_ref: str = JOURNEY_BASE_REF,
    execute: Any = None,
    log: logging.Logger | None = None,
) -> WorktreeCut:
    """Make — or recognise as already made — build ``build_id``'s tree.

    This is everything the writer does *inside a checkout*, in the order it
    has always done it: the reuse look-up against ``git worktree list``, the
    check that a base branch other than the trunk actually exists, the
    gitignore guard, and ``git worktree add -b <branch> <path> <base>``.

    It is its own function because the same work now runs in two places. For
    a repository with no sandbox it runs in the forge container exactly as
    before. For a repository that has one it runs inside that sandbox,
    called by the deploy sidecar's ``/git/worktree-add`` route on the
    factory's own clone — so nothing the factory runs on a repository runs
    on the host (Rich, 2026-09-07).

    Never raises, and never logs a refusal: the caller owns the wording it
    puts on the record, and a refusal logged twice reads as two failures.

    Args:
        checkout: The checkout the worktree is registered against.
        build_id: The build whose tree this is (also the leaf directory).
        branch: The journey branch to create.
        base_ref: The commit-ish that branch is cut from.
        execute: Injected subprocess primitive.
        log: Where the reuse note goes.
    """
    from forge.adapters.git.operations import _default_execute, prepare_worktree

    _log = log or logger
    _execute = execute if execute is not None else _default_execute
    checkout_path = Path(_normalise(checkout))
    forge_dir = checkout_path / _FORGE_DIR
    builds_root = forge_dir / _WORKTREES_DIR
    target = builds_root / build_id

    def _no(reason: str) -> WorktreeCut:
        return WorktreeCut(ok=False, branch=branch, base_ref=base_ref, reason=reason)

    # 4 — the reuse arm.
    try:
        listing = await _execute(
            command=["git", "worktree", "list", "--porcelain"],
            cwd=str(checkout_path),
        )
    except Exception as exc:  # noqa: BLE001 — a refusal, never an exception
        return _no(
            f"listing the existing worktrees in {checkout_path} raised "
            f"{type(exc).__name__}: {exc}, so build_id={build_id}'s worktree "
            "cannot be materialised without risking a collision"
        )
    if listing.exit_code != 0:
        return _no(
            f"'git worktree list --porcelain' in {checkout_path} exited "
            f"{listing.exit_code} ({(listing.stderr or '').strip()}), so "
            f"build_id={build_id}'s worktree cannot be materialised without "
            "risking a collision"
        )

    target_str = _normalise(target)
    target_real = _realpath(target)
    for entry_path, entry_branch in _parse_worktree_list(listing.stdout or ""):
        # Identity, not containment: both sides symlink-resolved because
        # git's porcelain always reports the realpath (see _realpath).
        same_path = _realpath(entry_path) == target_real
        same_branch = entry_branch == branch
        if same_path and same_branch:
            hollow = _hollow_worktree_reason(
                target_str, build_id, branch, checkout_path
            )
            if hollow is not None:
                # A REGISTRATION is not a tree. git keeps the administrative
                # record in .git/worktrees/<id> long after the directory is
                # deleted (or emptied) by hand, by a cleanup script, or by a
                # tmpfs reboot — and 'git worktree add' then refuses the path
                # as already registered, so re-materialising is not even
                # available. Handing back reused=True here would report a
                # ready tree onto builds.worktree_path and the journey would
                # die several stages later, in the leg, with the real cause
                # out of sight. Refuse loudly instead, in the lane's posture.
                return _no(hollow)
            _log.info(
                "conductor worktree: build_id=%s already has its own worktree "
                "at %s on %s — REUSING it (this is a redelivery of the same "
                "build, not a collision)",
                build_id,
                target_str,
                branch,
            )
            return WorktreeCut(
                ok=True,
                path=target_str,
                branch=branch,
                base_ref=base_ref,
                reused=True,
            )
        if same_path:
            return _no(
                f"{target} is already registered as a worktree on branch "
                f"{entry_branch!r}, not build_id={build_id}'s own "
                f"{branch!r}; refusing rather than reusing somebody else's "
                "tree"
            )
        if same_branch:
            return _no(
                f"branch {branch!r} is already checked out by the worktree at "
                f"{entry_path}, not at build_id={build_id}'s own {target}; "
                "refusing rather than materialising a second tree on one "
                "branch"
            )

    # 4b — a base other than main must exist before a tree can be cut from
    # it. The refusal names the branch, because "invalid reference" from git
    # would not say that the build was queued on a branch nobody made.
    if base_ref != JOURNEY_BASE_REF:
        immutable_commit = len(base_ref) == 40 and all(
            char in "0123456789abcdefABCDEF" for char in base_ref
        )
        base_spec = (
            f"{base_ref}^{{commit}}" if immutable_commit else f"refs/heads/{base_ref}"
        )
        try:
            exists = await _execute(
                command=[
                    "git",
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    base_spec,
                ],
                cwd=str(checkout_path),
            )
        except Exception as exc:  # noqa: BLE001 — a refusal, never an exception
            return _no(
                f"checking that the branch {base_ref!r} exists in "
                f"{checkout_path} raised {type(exc).__name__}: {exc}, so "
                f"build_id={build_id}'s worktree cannot be cut from it"
            )
        if exists.exit_code != 0:
            return _no(
                f"build_id={build_id} was queued on the branch {base_ref!r}, but "
                f"that branch does not exist in {checkout_path}, so its worktree "
                "cannot be cut from it (a tree cut from "
                f"{JOURNEY_BASE_REF} would not carry the task file the build was "
                "queued with, and the review leg would refuse)"
            )

    # 5 — the gitignore guard, then the tree.
    guard_problem = _ensure_forge_gitignore(forge_dir)
    if guard_problem is not None:
        return _no(guard_problem)

    result = await prepare_worktree(
        build_id,
        checkout_path,
        branch,
        execute=_execute,
        builds_root=builds_root,
        create_branch=True,
        base_ref=base_ref,
    )
    if result.status != "success" or not result.worktree_path:
        detail = (result.stderr or "").strip() or "no diagnostic was captured"
        return _no(
            f"materialising build_id={build_id}'s worktree at {target} on "
            f"branch {branch} off {base_ref} FAILED: {detail}"
        )
    return WorktreeCut(
        ok=True, path=result.worktree_path, branch=branch, base_ref=base_ref
    )


async def remove_journey_worktree(
    *,
    worktree: "Path | str",
    build_id: str = "",
    execute: Any = None,
) -> WorktreeCut:
    """Remove one journey tree from ``checkout`` — ``git worktree remove
    --force``, through the adapter the rest of the estate uses.

    The same two-places story as :func:`cut_worktree_in_checkout`: in the
    forge container for a repository with no sandbox, inside the sandbox
    behind ``/git/worktree-remove`` for one that has (rule 76). A path that
    is already gone is a success — there is nothing left to remove — so a
    second call is safe.
    """
    from forge.adapters.git.operations import _default_execute, cleanup_worktree

    target = Path(_normalise(worktree))
    if not target.exists():
        return WorktreeCut(ok=True, path=str(target))
    try:
        result = await cleanup_worktree(
            build_id or target.name,
            target,
            execute=execute if execute is not None else _default_execute,
        )
    except Exception as exc:  # noqa: BLE001 — a refusal, never an exception
        return WorktreeCut(
            ok=False,
            path=str(target),
            reason=(
                f"removing the worktree at {target} raised "
                f"{type(exc).__name__}: {exc}"
            ),
        )
    if result.status == "success":
        return WorktreeCut(ok=True, path=str(target))
    detail = (result.stderr or "").strip() or "no diagnostic was captured"
    return WorktreeCut(
        ok=False,
        path=str(target),
        reason=f"removing the worktree at {target} FAILED: {detail}",
    )


async def prepare_journey_worktree(
    pool: Any,
    config: Any,
    build_id: str,
    *,
    execute: Any = None,
    log: logging.Logger | None = None,
    post: Any = None,
) -> WorktreeOutcome:
    """Materialise (or reuse) the fix journey's worktree and record it.

    Called at the router seam AFTER the cap-law belt and BEFORE anything
    is spawned — the daemon, the component that owns the spawn, makes the
    tree at the moment it is needed rather than trusting a path some
    earlier process inferred.

    The arms, in order:

    1. **Read the row.** No row / no ``repo`` / no ``task_id`` — a fix
       journey with no durable subject cannot name its branch — refuses.
    2. **Resolve the checkout** through ``planning.target_repo_paths``,
       the same map the gates reader already trusts. An unregistered
       repo refuses NAMING the known keys (fix journeys are possible on
       registered checkouts only, and saying so beats a mystery).
    3. **Allowlist-check the target path at write time.**
    4. **The reuse arm** — ``git worktree list --porcelain`` in the
       canonical checkout, matched on path AND branch for THIS build.
       The path match resolves symlinks on BOTH sides (git reports
       realpaths), and a match is only REUSE once the tree is proven on
       disk — a directory carrying a ``.git`` entry. A registration
       without its tree refuses loudly. ANY other collision refuses too.
    5. **The gitignore guard**, then ``prepare_worktree`` with
       ``create_branch=True`` off the branch the row was queued on
       (:func:`journey_base_ref`: ``main``, or the row's own branch, which
       must exist in the checkout).
    6. **Record** through ``pool.record_worktree_path`` and
       ``pool.record_merge_branch`` (Part M, rule 54: the merge word reads
       the journey branch from the row). A recorded path is the invariant
       every downstream consumer enforces, and an unrecorded branch would
       send the merge word to the feature's own branch instead of this
       one, so a write that does not land is a refusal too.

    Never raises: every failure is a :class:`WorktreeRefused` the caller
    turns into a taken-and-terminal outcome.

    Args:
        pool: The lifecycle persistence facade (``get_build_row`` +
            ``record_worktree_path`` + ``record_merge_branch``).
        config: The loaded :class:`~forge.config.models.ForgeConfig`.
        build_id: The mode-c build whose tree this is.
        execute: Injected subprocess primitive (see
            :data:`forge.adapters.git.operations.ExecuteCallable`).
            Defaults to the adapter's own.
        log: Caller's logger, so refusals name the caller's seam.
    """
    from forge.adapters.git.operations import _default_execute

    _log = log or logger
    _execute = execute if execute is not None else _default_execute

    if not build_id:
        return _refuse(
            "the worktree writer was called with no build_id", log=_log
        )

    # 1 — the row.
    try:
        row = pool.get_build_row(build_id)
    except Exception as exc:  # noqa: BLE001 — a refusal, never an exception
        return _refuse(
            f"reading build_id={build_id} to materialise its worktree raised "
            f"{type(exc).__name__}: {exc}",
            log=_log,
        )
    if row is None:
        return _refuse(
            f"there is no builds row for build_id={build_id} to materialise a "
            "worktree against",
            log=_log,
        )
    repo = getattr(row, "repo", None)
    task_id = getattr(row, "task_id", None)
    if not repo:
        return _refuse(
            f"build_id={build_id} carries no repo, so no registered checkout "
            "can be resolved to materialise its worktree in",
            log=_log,
        )
    if not task_id:
        return _refuse(
            f"build_id={build_id} is a fix journey with no task_id on the row, "
            "so its branch cannot be named (fix/<task_id>-<build8>)",
            log=_log,
        )
    # What the journey branch is cut from: the branch the row was queued on
    # (main when it names none) — Part M, rule 56.
    base_ref = journey_base_ref(getattr(row, "branch", None))

    # 2 — the registered checkout.
    paths = getattr(getattr(config, "planning", None), "target_repo_paths", None) or {}
    checkout_raw = paths.get(repo)
    if not checkout_raw:
        known = ", ".join(sorted(paths)) or "<none>"
        return _refuse(
            f"repo {repo!r} is not in planning.target_repo_paths, so there is "
            f"no registered checkout to materialise build_id={build_id}'s "
            f"worktree in. Registered repos: {known}",
            log=_log,
        )
    checkout = Path(_normalise(checkout_raw))
    # 2b — does this repository have a sandbox? A repository listed in
    # planning.sandboxes has its tree cut inside that sandbox, on the
    # factory's own clone at this same path, so the checkout is NOT expected
    # to be readable here — forge-prod does not mount it any more. Only a
    # repository without a sandbox is checked on this host.
    sandbox = _sandbox_for(config, repo)
    if sandbox is None and not (checkout / ".git").exists():
        return _refuse(
            f"the registered checkout for repo {repo!r} ({checkout}) is not a "
            "git checkout on this host, so no worktree can be added from it",
            log=_log,
        )

    branch = journey_branch_name(str(task_id), build_id)
    target = journey_worktree_path(checkout, build_id)

    # 3 — the allowlist, at WRITE time.
    allowlist = list(
        getattr(
            getattr(getattr(config, "permissions", None), "filesystem", None),
            "allowlist",
            [],
        )
        or []
    )
    if not _is_inside_allowlist(target, allowlist):
        roots = ", ".join(str(entry) for entry in allowlist) or "<empty>"
        return _refuse(
            f"the journey worktree path {target} is NOT inside "
            f"permissions.filesystem.allowlist ({roots}); materialising it "
            "would produce a tree the leg's own cwd check then refuses",
            log=_log,
        )

    # 4, 4b and 5 — the reuse look-up, the base-branch check, the gitignore
    # guard and the tree itself. In the forge container for a repository with
    # no sandbox; inside the repository's own sandbox, over its deploy
    # sidecar's route, for one that has (sandbox first, 2026-09-07, rule 76).
    if sandbox is None:
        cut = await cut_worktree_in_checkout(
            checkout=checkout,
            build_id=build_id,
            branch=branch,
            base_ref=base_ref,
            execute=_execute,
            log=_log,
        )
    else:
        cut = await _cut_in_sandbox(
            sandbox=sandbox,
            repo=str(repo),
            build_id=build_id,
            worktree=target,
            branch=branch,
            base_ref=base_ref,
            post=post,
            log=_log,
        )
    if not cut.ok:
        return _refuse(cut.reason, log=_log)

    # 6 — the record. A path nobody recorded is a path the dispatch refuses,
    # and a branch nobody recorded is a branch the merge word never merges.
    recorded = _record(pool, build_id, cut.path, branch, log=_log)
    if recorded is not None:
        return recorded
    if not cut.reused:
        _log.info(
            "conductor worktree: build_id=%s materialised at %s on branch %s "
            "(cut from %s) and recorded on builds.worktree_path and "
            "builds.merge_branch",
            build_id,
            cut.path,
            branch,
            base_ref,
        )
    return WorktreeReady(
        path=cut.path, branch=branch, reused=cut.reused, base_ref=base_ref
    )


async def _cut_in_sandbox(
    *,
    sandbox: Any,
    repo: str,
    build_id: str,
    worktree: Path,
    branch: str,
    base_ref: str,
    post: Any = None,
    log: logging.Logger,
) -> WorktreeCut:
    """Ask the sidecar inside ``repo``'s sandbox to cut the journey's tree.

    One POST to ``/git/worktree-add`` with the repository's key, the tree's
    path, the branch and its base. The sidecar runs the very same function
    this module runs in the container, on the factory's clone, so there is
    one statement of what a journey tree is and not two. A refusal comes back
    as a plain sentence and becomes this writer's refusal unchanged.
    """
    # One HTTP seam for the whole estate: the planning chain's sidecar client
    # already has it, and a second copy here would be a second thing to keep
    # right.
    from forge.deploy_sidecar.service import GIT_WORKTREE_ADD_ROUTE
    from forge.planning.sidecar_git_runner import _urllib_post

    sender = post if post is not None else _urllib_post
    url = f"{str(sandbox.sidecar_url).rstrip('/')}{GIT_WORKTREE_ADD_ROUTE}"
    body = {
        "repo": repo,
        "path": str(worktree),
        "branch": branch,
        "base_ref": base_ref,
    }
    log.info(
        "conductor worktree: build_id=%s cuts its tree inside sandbox %s "
        "(%s) — the repository's code never comes to the host",
        build_id,
        getattr(sandbox, "name", "?"),
        url,
    )
    try:
        status, decoded = await asyncio.to_thread(
            sender, url, body, SIDECAR_WORKTREE_TIMEOUT_S
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — a refusal, never an exception
        return WorktreeCut(
            ok=False,
            branch=branch,
            base_ref=base_ref,
            reason=(
                f"the sidecar in sandbox {getattr(sandbox, 'name', '?')} could "
                f"not be reached at {url} to cut build_id={build_id}'s "
                f"worktree: {type(exc).__name__}: {exc}"
            ),
        )
    answer = decoded if isinstance(decoded, dict) else {}
    if status != 200:
        sentence = str(answer.get("error") or "").strip() or f"HTTP {status}"
        return WorktreeCut(
            ok=False,
            branch=branch,
            base_ref=base_ref,
            reason=(
                f"the sidecar in sandbox {getattr(sandbox, 'name', '?')} "
                f"refused to cut build_id={build_id}'s worktree: {sentence}"
            ),
        )
    if answer.get("status") != "success":
        sentence = (
            str(answer.get("detail") or answer.get("error") or "").strip()
            or "no reason was given"
        )
        return WorktreeCut(
            ok=False, branch=branch, base_ref=base_ref, reason=sentence
        )
    path = str(answer.get("path") or "").strip()
    if not path:
        return WorktreeCut(
            ok=False,
            branch=branch,
            base_ref=base_ref,
            reason=(
                f"the sidecar in sandbox {getattr(sandbox, 'name', '?')} said "
                f"build_id={build_id}'s worktree was made but did not say "
                "where, so there is no path to record"
            ),
        )
    return WorktreeCut(
        ok=True,
        path=path,
        branch=branch,
        base_ref=base_ref,
        reused=bool(answer.get("reused")),
    )


def _record(
    pool: Any, build_id: str, path: str, branch: str, *, log: logging.Logger
) -> WorktreeRefused | None:
    """Write the path and the journey branch onto the row; a refusal when either cannot land.

    The branch goes on ``builds.merge_branch`` (Part M, rule 54): the merge
    word reads it from the row and, finding it empty, would merge the
    feature's own ``autobuild/<feature id>`` — for a repair, a branch that is
    already on main — so a journey whose branch was not recorded is refused
    here rather than merged wrongly later.
    """
    try:
        pool.record_worktree_path(build_id, path)
    except Exception as exc:  # noqa: BLE001 — a refusal, never an exception
        return _refuse(
            f"build_id={build_id}'s worktree exists at {path} but recording it "
            f"on builds.worktree_path raised {type(exc).__name__}: {exc}; the "
            "journey is refused because every downstream consumer reads the "
            "column, not the disk (the tree is left in place for recovery)",
            log=log,
        )
    try:
        pool.record_merge_branch(build_id, branch)
    except Exception as exc:  # noqa: BLE001 — a refusal, never an exception
        return _refuse(
            f"build_id={build_id}'s worktree exists at {path} on branch {branch} "
            f"but recording that branch on builds.merge_branch raised "
            f"{type(exc).__name__}: {exc}; the journey is refused because the "
            "merge word reads the branch from the row and, finding none, would "
            "merge the feature's own branch instead (the tree is left in place "
            "for recovery)",
            log=log,
        )
    return None

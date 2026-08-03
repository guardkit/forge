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
``fix/<task_id>-<build8>`` off ``main``:

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
    "JOURNEY_BASE_REF",
    "journey_branch_name",
    "prepare_journey_worktree",
    "short_build_id",
]


#: The trunk every journey branch is cut from. Matches the commit probe's
#: ``base_branch`` default (``git rev-list --count main..HEAD`` is the
#: journey's ONLY commit evidence), so the branch the writer creates and
#: the base the probe counts against are one statement, not two.
JOURNEY_BASE_REF = "main"

#: Directory the writer owns inside a registered checkout. The worktrees
#: live under ``<checkout>/.forge/worktrees/``; the gitignore guard sits
#: at ``<checkout>/.forge/.gitignore``.
_FORGE_DIR = ".forge"
_WORKTREES_DIR = "worktrees"

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
    """

    path: str
    branch: str
    reused: bool = False


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


def _refuse(reason: str, *, log: logging.Logger) -> WorktreeRefused:
    log.error("conductor worktree: %s", reason)
    return WorktreeRefused(reason=reason)


def _normalise(path: Any) -> str:
    """Absolute, ``..``-free string form — the containment comparison unit."""
    return os.path.normpath(os.path.abspath(str(path)))


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


async def prepare_journey_worktree(
    pool: Any,
    config: Any,
    build_id: str,
    *,
    execute: Any = None,
    log: logging.Logger | None = None,
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
       A clean match is reuse (a redelivery must not fail on its own
       earlier work); ANY other collision refuses loudly.
    5. **The gitignore guard**, then ``prepare_worktree`` with
       ``create_branch=True`` off :data:`JOURNEY_BASE_REF`.
    6. **Record** through ``pool.record_worktree_path``. A recorded path
       is the invariant every downstream consumer enforces, so a write
       that does not land is a refusal too.

    Never raises: every failure is a :class:`WorktreeRefused` the caller
    turns into a taken-and-terminal outcome.

    Args:
        pool: The lifecycle persistence facade (``get_build_row`` +
            ``record_worktree_path``).
        config: The loaded :class:`~forge.config.models.ForgeConfig`.
        build_id: The mode-c build whose tree this is.
        execute: Injected subprocess primitive (see
            :data:`forge.adapters.git.operations.ExecuteCallable`).
            Defaults to the adapter's own.
        log: Caller's logger, so refusals name the caller's seam.
    """
    from forge.adapters.git.operations import _default_execute, prepare_worktree

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
    if not (checkout / ".git").exists():
        return _refuse(
            f"the registered checkout for repo {repo!r} ({checkout}) is not a "
            "git checkout on this host, so no worktree can be added from it",
            log=_log,
        )

    branch = journey_branch_name(str(task_id), build_id)
    forge_dir = checkout / _FORGE_DIR
    builds_root = forge_dir / _WORKTREES_DIR
    target = builds_root / build_id

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

    # 4 — the reuse arm.
    try:
        listing = await _execute(
            command=["git", "worktree", "list", "--porcelain"],
            cwd=str(checkout),
        )
    except Exception as exc:  # noqa: BLE001 — a refusal, never an exception
        return _refuse(
            f"listing the existing worktrees in {checkout} raised "
            f"{type(exc).__name__}: {exc}, so build_id={build_id}'s worktree "
            "cannot be materialised without risking a collision",
            log=_log,
        )
    if listing.exit_code != 0:
        return _refuse(
            f"'git worktree list --porcelain' in {checkout} exited "
            f"{listing.exit_code} ({(listing.stderr or '').strip()}), so "
            f"build_id={build_id}'s worktree cannot be materialised without "
            "risking a collision",
            log=_log,
        )

    target_str = _normalise(target)
    for entry_path, entry_branch in _parse_worktree_list(listing.stdout or ""):
        same_path = _normalise(entry_path) == target_str
        same_branch = entry_branch == branch
        if same_path and same_branch:
            _log.info(
                "conductor worktree: build_id=%s already has its own worktree "
                "at %s on %s — REUSING it (this is a redelivery of the same "
                "build, not a collision)",
                build_id,
                target_str,
                branch,
            )
            recorded = _record(pool, build_id, target_str, log=_log)
            if recorded is not None:
                return recorded
            return WorktreeReady(path=target_str, branch=branch, reused=True)
        if same_path:
            return _refuse(
                f"{target} is already registered as a worktree on branch "
                f"{entry_branch!r}, not build_id={build_id}'s own "
                f"{branch!r}; refusing rather than reusing somebody else's "
                "tree",
                log=_log,
            )
        if same_branch:
            return _refuse(
                f"branch {branch!r} is already checked out by the worktree at "
                f"{entry_path}, not at build_id={build_id}'s own {target}; "
                "refusing rather than materialising a second tree on one "
                "branch",
                log=_log,
            )

    # 5 — the gitignore guard, then the tree.
    guard_problem = _ensure_forge_gitignore(forge_dir)
    if guard_problem is not None:
        return _refuse(guard_problem, log=_log)

    result = await prepare_worktree(
        build_id,
        checkout,
        branch,
        execute=_execute,
        builds_root=builds_root,
        create_branch=True,
        base_ref=JOURNEY_BASE_REF,
    )
    if result.status != "success" or not result.worktree_path:
        detail = (result.stderr or "").strip() or "no diagnostic was captured"
        return _refuse(
            f"materialising build_id={build_id}'s worktree at {target} on "
            f"branch {branch} off {JOURNEY_BASE_REF} FAILED: {detail}",
            log=_log,
        )

    # 6 — the record. A path nobody recorded is a path the dispatch refuses.
    recorded = _record(pool, build_id, result.worktree_path, log=_log)
    if recorded is not None:
        return recorded
    _log.info(
        "conductor worktree: build_id=%s materialised at %s on branch %s "
        "(cut from %s) and recorded on builds.worktree_path",
        build_id,
        result.worktree_path,
        branch,
        JOURNEY_BASE_REF,
    )
    return WorktreeReady(path=result.worktree_path, branch=branch)


def _record(
    pool: Any, build_id: str, path: str, *, log: logging.Logger
) -> WorktreeRefused | None:
    """Write the path onto the row; a refusal when the write cannot land."""
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
    return None

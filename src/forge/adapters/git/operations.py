"""Git adapter operations — TASK-GCI-006.

Thin async wrappers around the project ``execute`` subprocess primitive
(see ``docs/design/contracts/API-subprocess.md`` §4) that funnel every
``git`` invocation into a structured :class:`GitOpResult` and never
raise past the adapter boundary (ADR-ARCH-025).

Three operations:

- :func:`prepare_worktree` — ``git worktree add`` for the build's path,
  optionally ``-b <branch> <path> <base>`` when the branch must be
  CREATED (the fix journey's per-build branch, conductor activation §1).
- :func:`commit_all` — :func:`prepare_index` + ``git commit -m`` + ``git rev-parse HEAD``.
- :func:`prepare_index` — stage the working folder with the factory's own
  records left out, and say whether anything is left to commit.
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

import asyncio
import hashlib
import logging
import os
import stat
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Sequence

from forge.adapters.git.models import GitOpResult
from forge.factory_files import (
    factory_record_content_pathspecs,
    factory_record_exclusions,
    factory_record_pathspecs,
    is_factory_record,
)
from forge.project_git import PatternPathspec, run_git_async

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
    # A pathspec the factory wrote as a pattern keeps its marker: the git
    # helper takes every other name literally.
    argv = [
        token if isinstance(token, PatternPathspec) else str(token)
        for token in command
    ]
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


#: The warning given when moves or copies of a record were left out.
MOVED_RECORDS_WARNING = (
    "Left %d file(s) out of the commit in %s: they are moves or copies of "
    "GuardKit's own records (first: %s), which can hold private paths. They "
    "stay on disk and are never committed."
)

#: The warning given when records that were staged were taken out.
STAGED_RECORDS_WARNING = (
    "Left %d of the factory's own record file(s) out of the commit in %s "
    "(%s). They stay on disk and are never committed; nothing reads them "
    "from git."
)


@dataclass(frozen=True)
class PreparedIndex:
    """What :func:`prepare_index` found: whether anything is left to commit,
    or the failure that stopped it (then ``staged`` means nothing)."""

    staged: bool
    failure: GitOpResult | None = None


#: The empty blob, in both object formats. An empty file is never a copy of
#: a record: git pairs empty files with each other (coach review 1, F1).
_EMPTY_BLOBS = frozenset(
    {
        "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391",
        "473a0f4c3be8a93681a267e3b1e9a7dcda1185436fe141f7749120a303721813",
    }
)

#: How many file names one ``git reset`` is given.
_NAMES_PER_COMMAND = 200


def _moved_out_of_records(raw: str) -> list[str]:
    """Destinations of staged renames or copies whose source is a record and
    whose destination is neither a record nor empty, from ``git diff --raw
    -z`` output with rename detection on. A rename or copy names two paths;
    every other status names one."""
    fields = raw.split("\0")
    found: list[str] = []
    index = 0
    while index < len(fields):
        head = fields[index].lstrip("\n")
        index += 1
        if not head.startswith(":"):
            continue
        parts = head[1:].split()
        status = parts[4] if len(parts) >= 5 else ""
        if status[:1] in ("R", "C"):
            source, destination = (fields[index : index + 2] + ["", ""])[:2]
            index += 2
            if (
                is_factory_record(source)
                and destination
                and not is_factory_record(destination)
                and parts[3] not in _EMPTY_BLOBS
            ):
                found.append(destination)
        else:
            index += 1
    return found


#: Inside each inner build folder (``.guardkit/worktrees/<id>``), the record
#: folders that are read. Its product files are never read. The same as
#: GuardKit's ``_INNER_RECORD_FOLDERS`` (GuardKit e0b72800).
INNER_RECORD_FOLDERS: tuple[str, ...] = (
    ".guardkit/autobuild",
    ".guardkit/autobuild-private",
    ".guardkit/qav-shadow",
    "conversation_history",
    "large_tool_results",
)

#: How long a commit waits for record files to be read. A read on a stalled
#: filesystem (a hung network mount) cannot be interrupted, and the planning
#: writer must never block on it: every other step it takes has a time limit
#: (GuardKit e0b72800 waits the same 5 seconds).
RECORD_READ_BOUND_S: float = 5.0

#: Records smaller than this are not compared with product files at commit
#: time (coordinator decision, 14 October 2026). A tiny record such as ``[]``
#: carries nothing worth protecting, and a product file holding the same few
#: bytes must never be silently left out. Private text in any file is still
#: refused by the publisher, whose own content look keeps no size exemption.
SMALLEST_COMPARED_RECORD: int = 64


def git_blob_id(data: bytes, object_format: str) -> str:
    """The id git gives a file holding exactly ``data``: the ``sha1`` or
    ``sha256`` digest of ``b"blob <length>\\0"`` followed by the bytes, as
    lower-case hex. No line-ending or filter conversion is applied."""
    digest = hashlib.new(object_format)
    digest.update(b"blob %d\0" % len(data))
    digest.update(data)
    return digest.hexdigest()


def _read_regular_file(path: Path) -> bytes | None:
    """A regular file's bytes, or ``None`` when it is gone, unreadable, empty
    or not a regular file. A link is never followed, and opening never waits
    on a pipe. GuardKit replaces records by renaming a temporary file over
    them, so a file listed a moment ago may be gone: that is never an error."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        handle = os.open(path, flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(handle).st_mode):
            return None
        chunks: list[bytes] = []
        while True:
            chunk = os.read(handle, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
    except OSError:
        return None
    finally:
        os.close(handle)
    return b"".join(chunks) or None


def _inner_build_record_files(root: Path) -> list[Path]:
    """The record files of each inner build folder under the top-level
    ``.guardkit/worktrees/``, found by walking :data:`INNER_RECORD_FOLDERS`
    and the ``.guardkit`` folder's own record files, never following a link.
    git's own listing stops at an inner build folder, because each is a
    repository of its own."""
    worktrees = root / ".guardkit" / "worktrees"
    if any(p.is_symlink() for p in (root / ".guardkit", worktrees)):
        return []
    try:
        inner_folders = [e for e in os.scandir(worktrees) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return []
    found: list[Path] = []
    for entry in inner_folders:
        inner = Path(entry.path)
        guardkit = inner / ".guardkit"
        if guardkit.is_symlink():
            continue
        for relative in INNER_RECORD_FOLDERS:
            top = inner / relative
            if top.is_symlink() or not top.is_dir():
                continue
            for folder, _subfolders, names in os.walk(top):
                found.extend(Path(folder) / name for name in names)
        try:
            names = [e.name for e in os.scandir(guardkit) if e.is_file(follow_symlinks=False)]
        except OSError:
            continue
        found.extend(
            guardkit / name
            for name in names
            if name == "bootstrap_state.json" or name.startswith("memory-query-log.jsonl")
        )
    return found


async def _record_blob_ids(root: Path, listed: list[str], object_format: str) -> set[str]:
    """The blob id of every regular file of at least
    :data:`SMALLEST_COMPARED_RECORD` bytes at a record path (as git listed
    them) and of each inner build folder's records, computed here from the
    bytes (:func:`git_blob_id`).

    The files are read in a thread of their own, waited for at most
    :data:`RECORD_READ_BOUND_S`. Files not read by then are left out of the
    comparison, with one warning, and the thread is left behind."""
    ids: set[str] = set()
    progress = {"listed": 0, "read": 0, "finished": False}
    lock = threading.Lock()
    loop = asyncio.get_running_loop()
    finished = asyncio.Event()

    def read_all() -> None:
        try:
            paths = {root / name for name in listed if not name.endswith("/")}
            paths.update(_inner_build_record_files(root))
            with lock:
                progress["listed"] = len(paths)
            for path in sorted(paths):
                data = _read_regular_file(path)
                with lock:
                    if data is not None and len(data) >= SMALLEST_COMPARED_RECORD:
                        ids.add(git_blob_id(data, object_format))
                    progress["read"] += 1
            with lock:
                progress["finished"] = True
        finally:
            try:
                loop.call_soon_threadsafe(finished.set)
            except RuntimeError:
                pass  # the commit stopped waiting and its loop is gone

    threading.Thread(
        target=read_all, name="forge-commit: reading records", daemon=True
    ).start()
    try:
        await asyncio.wait_for(finished.wait(), timeout=RECORD_READ_BOUND_S)
    except asyncio.TimeoutError:
        pass
    with lock:
        found = set(ids)
        done = progress["finished"]
        unread = progress["listed"] - progress["read"]
    if not done:
        logger.warning(
            "Reading the factory's record files in %s did not finish within "
            "%.0fs (%s not read); exact copies of those were not looked for in "
            "this commit.",
            root,
            RECORD_READ_BOUND_S,
            f"{unread} file(s)" if unread > 0 else "the folders could not be listed",
        )
    return found


def _staged_outside_records(raw: str) -> list[tuple[str, str]]:
    """``(blob id, path)`` for every file staged as added, changed or retyped
    outside the record paths, from ``git diff --cached --raw -z --no-renames``
    output. Embedded-repository links and empty files are left out."""
    fields = raw.split("\0")
    staged: list[tuple[str, str]] = []
    index = 0
    while index + 1 < len(fields):
        meta, path = fields[index].lstrip("\n").split(), fields[index + 1]
        index += 2
        if len(meta) < 4 or meta[1] == "160000" or is_factory_record(path):
            continue
        if meta[3] in _EMPTY_BLOBS:
            continue
        staged.append((meta[3], path))
    return staged


async def prepare_index(
    worktree: Path,
    *,
    execute: ExecuteCallable = _default_execute,
    operation: str = "commit_all",
) -> PreparedIndex:
    """Stage ``worktree`` for a commit with the factory's records left out.

    Design "Factory records never enter a project's commits" (14 October
    2026); the same step as GuardKit's checkpoint, written again here because
    Forge does not import GuardKit:

    1. ``git add -A -- . <record exclusions>`` — record paths 1 to 6 and 8
       are never staged in the first place (path 7, the top-level transcript
       folder, cannot be named here without git refusing the add in a project
       that ignores it);
    2. before anything is restored, every staged rename or copy whose source
       is a record and whose destination is not has its DESTINATION
       unstaged, with one warning. ``--find-copies-harder`` is needed for a
       plain move on disk (the source's deletion is never staged, so git sees
       a copy), and ``-l0`` stops git skipping the comparison quietly on a
       large change. An empty destination is not a copy of anything. Then
       every staged file outside the record paths whose content is exactly
       that of a record file on disk, committed or not, is unstaged too;
    3. whatever is still staged under any of the eight record paths (a record
       force-added earlier, a change to a tracked record, the transcript
       folder) is unstaged, with one warning naming the count and the first
       five paths;
    4. ``git diff --cached --quiet`` answers whether anything is left.

    Files left out stay on disk. Safe to run twice. Never raises.
    """
    cwd = str(worktree)

    def failed(result: ExecuteResult) -> PreparedIndex:
        return PreparedIndex(
            staged=False,
            failure=GitOpResult(
                status="failed",
                operation=operation,
                stderr=_failure_stderr(result.stderr, result.stdout),
                exit_code=result.exit_code,
            ),
        )

    async def _reset(names: list[str]) -> ExecuteResult | None:
        """Unstage ``names`` literally, in batches; the failed step, if any."""
        for start in range(0, len(names), _NAMES_PER_COMMAND):
            done = await execute(
                command=["git", "reset", "-q", "--", *names[start : start + _NAMES_PER_COMMAND]],
                cwd=cwd,
            )
            if done.exit_code != 0:
                return done
        return None

    async def _exact_copies_of_records() -> list[str] | ExecuteResult:
        """Staged files outside the record paths whose content is byte for
        byte that of a record file on disk, committed or not (GuardKit
        e6bd14d2). Records are never committed, so git's copy detection,
        which only looks at what ``HEAD`` or the index holds, cannot see an
        exact copy of one. The record files are listed tracked, untracked and
        ignored alike, and each inner build folder's own records are walked
        (never its product files). Each non-empty regular file's blob id is
        computed here from its bytes (GuardKit e0b72800), so a record
        replaced while this runs is skipped, never an error.
        """
        staged_res = await execute(
            command=[
                "git", "diff", "--cached", "--raw", "-z", "--no-abbrev",
                "--no-renames", "--diff-filter=AMT",
            ],
            cwd=cwd,
        )
        if staged_res.exit_code != 0:
            return staged_res
        staged = _staged_outside_records(staged_res.stdout or "")
        if not staged:
            return []
        listed = await execute(
            command=[
                "git", "ls-files", "-z", "--cached", "--others", "--",
                *factory_record_content_pathspecs(),
            ],
            cwd=cwd,
        )
        if listed.exit_code != 0:
            return listed
        shown = await execute(command=["git", "rev-parse", "--show-object-format"], cwd=cwd)
        object_format = (shown.stdout or "").strip()
        if shown.exit_code != 0 or object_format not in ("sha1", "sha256"):
            return shown if shown.exit_code != 0 else ExecuteResult(
                exit_code=1,
                stdout="",
                stderr="git rev-parse named an object format the factory does not know.",
            )
        record_ids = await _record_blob_ids(
            Path(cwd),
            sorted({n for n in (listed.stdout or "").split("\0") if n}),
            object_format,
        )
        return [path for blob, path in staged if blob in record_ids]

    try:
        added = await execute(
            command=["git", "add", "-A", "--", ".", *factory_record_exclusions()],
            cwd=cwd,
        )
        if added.exit_code != 0:
            return failed(added)

        moves = await execute(
            command=[
                "git", "diff", "--cached", "--raw", "--no-abbrev", "-z",
                "-M", "-C", "--find-copies-harder", "-l0",
            ],
            cwd=cwd,
        )
        if moves.exit_code != 0:
            return failed(moves)
        moved = _moved_out_of_records(moves.stdout or "")
        if moved:
            reset = await _reset(moved)
            if reset is not None:
                return failed(reset)
            logger.warning(MOVED_RECORDS_WARNING, len(moved), worktree, moved[0])

        copied = await _exact_copies_of_records()
        if isinstance(copied, ExecuteResult):
            return failed(copied)
        if copied:
            reset = await _reset(copied)
            if reset is not None:
                return failed(reset)
            logger.warning(MOVED_RECORDS_WARNING, len(copied), worktree, copied[0])

        # ``--no-renames``: a record renamed to another record path must list
        # both names, or the source's deletion stays staged (coach review 1, F3).
        listed = await execute(
            command=[
                "git", "diff", "--cached", "--name-only", "--no-renames", "-z", "--",
                *factory_record_pathspecs(),
            ],
            cwd=cwd,
        )
        if listed.exit_code != 0:
            return failed(listed)
        records = [name for name in (listed.stdout or "").split("\0") if name]
        if records:
            reset = await _reset(records)
            if reset is not None:
                return failed(reset)
            logger.warning(
                STAGED_RECORDS_WARNING,
                len(records),
                worktree,
                ", ".join(records[:5]) + (", ..." if len(records) > 5 else ""),
            )

        quiet = await execute(command=["git", "diff", "--cached", "--quiet"], cwd=cwd)
        if quiet.exit_code == 0:
            return PreparedIndex(staged=False)
        if quiet.exit_code == 1:
            return PreparedIndex(staged=True)
        return failed(quiet)
    except Exception as exc:  # noqa: BLE001 — adapter boundary, ADR-ARCH-025
        logger.exception("prepare_index failed (worktree=%s)", worktree)
        return PreparedIndex(staged=False, failure=_exception_failure(operation, exc))


async def commit_all(
    worktree: Path,
    message: str,
    *,
    execute: ExecuteCallable = _default_execute,
) -> GitOpResult:
    """Stage all changes, without the factory's records, and commit them.

    Issues these list-token git invocations:

    1. :func:`prepare_index` — ``git add -A`` with the factory's own record
       paths left out, then any staged record (or move or copy of one) taken
       out again (14 October 2026; before, a plain ``git add -A``);
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
        prepared = await prepare_index(worktree, execute=execute, operation=operation)
        if prepared.failure is not None:
            return prepared.failure

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
    "PreparedIndex",
    "cleanup_worktree",
    "git_blob_id",
    "commit_all",
    "prepare_index",
    "prepare_worktree",
]

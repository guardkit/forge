"""Ownership checks and terminal cleanup for retained autobuild worktrees.

Successful feature builds normally discard their detached outer worktree.  A
GuardKit build can, however, leave registered task worktrees below
``.guardkit/worktrees``; removing the outer tree at that point destroys the
offered candidate's checkout and leaves Git administration behind.  This
module supplies the small, shared Git operation used by both the runner and
the sandbox sidecar:

* inspect one exact ``<configured base>/<build id>`` tree and its registered
  descendants;
* retain it while an offer is outstanding; and
* remove the registered task trees followed by the outer tree only after the
  merge lifecycle has ended successfully and the offer-time identity still
  matches.

It never scans or removes unrelated worktrees, Docker objects, caches or
volumes.  Every destructive operation is derived from Git's registrations and
is fail-closed when path ownership or offer-time identity cannot be proved.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
from pathlib import Path
from typing import Any, Mapping

from forge.project_git import run_git

FORGE_AUTOBUILD_WORKTREE_BASE_ENV = "FORGE_AUTOBUILD_WORKTREE_BASE"
DEFAULT_AUTOBUILD_WORKTREE_BASE = "/tmp/forge-autobuild-worktrees"
GIB = 1024**3

SAFE_BUILD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
INNER_WORKTREES_REL = Path(".guardkit/worktrees")


def _git(repo: Path, *args: str) -> tuple[int, bytes]:
    """Run ``git -C <repo> <args>``; ``(returncode, stdout or the failure sentence)``.

    Through :func:`forge.project_git.run_git` (14 October 2026), the one way
    Forge runs git. On failure the bytes are the helper's fixed sentence
    ("git status stopped with exit code 128."), never git's own error text,
    because callers copy them into the cleanup receipt and the logs.
    """
    try:
        done = run_git(["-C", str(repo), *args], text=False)
    except OSError as exc:
        return 127, f"{type(exc).__name__}: {exc}".encode()
    return done.returncode, done.stdout if done.returncode == 0 else done.stderr


def _registrations(repo: Path) -> tuple[list[dict[str, str | None]] | None, str | None]:
    code, raw = _git(repo, "worktree", "list", "--porcelain", "-z")
    if code != 0:
        return None, raw.decode("utf-8", errors="replace").strip()
    rows: list[dict[str, str | None]] = []
    current: dict[str, str | None] = {}
    for field in raw.split(b"\0"):
        if not field:
            if current:
                rows.append(current)
                current = {}
            continue
        text = field.decode("utf-8", errors="surrogateescape")
        key, sep, value = text.partition(" ")
        if key == "worktree" and sep:
            current["path"] = str(Path(value).resolve())
        elif key == "HEAD" and sep:
            current["head"] = value
        elif key == "branch" and sep:
            current["branch"] = value
        elif key in {"detached", "bare", "prunable", "locked"}:
            current[key] = value if sep else "true"
    if current:
        rows.append(current)
    return rows, None


def _capacity(path: Path) -> dict[str, int] | None:
    try:
        stat = os.statvfs(path)
    except OSError:
        return None
    return {
        "available_bytes": int(stat.f_bavail * stat.f_frsize),
        "available_inodes": int(stat.f_favail),
    }


def inspect_worktree_capacity(
    base: Path,
    *,
    min_available_bytes: int,
    reserved_bytes: int = 0,
    own_reserve_bytes: int = 0,
) -> dict[str, Any]:
    """Check the filesystem that will hold ``base`` before creating anything.

    ``min_available_bytes`` is the floor: free space that must REMAIN after
    this build starts. ``reserved_bytes`` is what other builds running beside
    this one have reserved, and ``own_reserve_bytes`` what this build will
    reserve (4 October 2026, concurrent builds); both count as spent. Zero
    for both, the default, is the check as it always was.
    """
    probe = base.expanduser()
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    capacity = _capacity(probe)
    report: dict[str, Any] = {
        "ok": False,
        "base": str(base.expanduser()),
        "probed_path": str(probe),
        "min_available_bytes": int(min_available_bytes),
        "reserved_bytes": int(reserved_bytes),
        "own_reserve_bytes": int(own_reserve_bytes),
        "capacity": capacity,
    }
    if min_available_bytes <= 0:
        report["detail"] = "the configured minimum available bytes must be positive"
    elif capacity is None:
        report["detail"] = "worktree filesystem capacity could not be read"
    elif capacity["available_inodes"] <= 0:
        report["detail"] = "worktree filesystem has no available inodes"
    elif (
        capacity["available_bytes"] - reserved_bytes - own_reserve_bytes
        < min_available_bytes
    ):
        if reserved_bytes or own_reserve_bytes:
            left = capacity["available_bytes"] - reserved_bytes - own_reserve_bytes
            report["detail"] = (
                f"worktree filesystem has {capacity['available_bytes']} available "
                f"bytes; other builds running now reserve {reserved_bytes} and this "
                f"build reserves {own_reserve_bytes}, which would leave {left}, "
                f"below the {min_available_bytes} that must stay free"
            )
        else:
            report["detail"] = (
                f"worktree filesystem has {capacity['available_bytes']} available "
                f"bytes, below the required {min_available_bytes}"
            )
    else:
        report.update(ok=True, detail="worktree filesystem capacity is sufficient")
    return report


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return path != root


def _unsupported_entry(path: Path) -> str | None:
    """Refuse Git-invisible special entries without following directory links.

    Git omits sockets and named pipes from its status output. The exact owned
    tree must therefore also be checked before its Git identity can authorize
    recursive removal. Ignored entries are checked too; nothing is deleted here.
    """
    def unreadable(error: OSError) -> None:
        raise error

    try:
        for root, directories, files, descriptor in os.fwalk(
            path, topdown=True, onerror=unreadable, follow_symlinks=False
        ):
            for name in directories + files:
                mode = os.stat(name, dir_fd=descriptor, follow_symlinks=False).st_mode
                if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
                    return f"unsupported filesystem entry: {Path(root) / name}"
    except OSError as exc:
        return f"worktree filesystem could not be inspected: {exc}"
    return None


def _status_sha(
    path: Path, *, registered_descendants: tuple[Path, ...] = ()
) -> tuple[str | None, str | None]:
    filesystem_error = _unsupported_entry(path)
    if filesystem_error:
        return None, filesystem_error
    code, status_raw = _git(
        path, "status", "--porcelain=v1", "-z", "--untracked-files=all"
    )
    if code != 0:
        return None, status_raw.decode("utf-8", errors="replace").strip()
    code, index_raw = _git(path, "ls-files", "--stage", "-z")
    if code != 0:
        return None, index_raw.decode("utf-8", errors="replace").strip()
    code, names_raw = _git(
        path,
        "ls-files",
        "--modified",
        "--deleted",
        "--others",
        "--exclude-standard",
        "-z",
    )
    if code != 0:
        return None, names_raw.decode("utf-8", errors="replace").strip()

    root = path.resolve()
    registered = {child.resolve() for child in registered_descendants}
    digest = hashlib.sha256()
    digest.update(b"status\0" + status_raw)
    digest.update(b"index\0" + index_raw)
    for raw_name in sorted({name for name in names_raw.split(b"\0") if name}):
        name = os.fsdecode(raw_name)
        relative = Path(name)
        if relative.is_absolute() or any(part == ".." for part in relative.parts):
            return None, f"dirty worktree entry escapes its root: {name!r}"
        candidate = root / relative
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            return None, f"dirty worktree entry escapes or is unreadable: {name!r}: {exc}"
        digest.update(b"path\0" + raw_name + b"\0")
        if resolved in registered:
            # Git reports a nested registered worktree as an untracked
            # directory in its outer owner. Its own identity row below binds
            # its HEAD, branch, status and dirty contents; only that exact
            # registration is exempt here, never an arbitrary directory.
            digest.update(b"registered-worktree\0")
            continue
        try:
            before = candidate.lstat()
        except FileNotFoundError:
            digest.update(b"missing\0")
            continue
        except OSError as exc:
            return None, f"dirty worktree entry is unreadable: {name!r}: {exc}"
        digest.update(f"mode:{before.st_mode:o}\0".encode())
        if stat.S_ISLNK(before.st_mode):
            try:
                target = os.readlink(candidate)
            except OSError as exc:
                return None, f"dirty symlink is unreadable: {name!r}: {exc}"
            digest.update(b"symlink\0" + os.fsencode(target) + b"\0")
            continue
        if not stat.S_ISREG(before.st_mode):
            return None, f"dirty worktree entry is not a regular file: {name!r}"
        try:
            with candidate.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
            after = candidate.stat()
        except OSError as exc:
            return None, f"dirty worktree entry is unreadable: {name!r}: {exc}"
        stable_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        stable_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if stable_before != stable_after:
            return None, f"dirty worktree entry changed while read: {name!r}"
        digest.update(b"\0")
    return digest.hexdigest(), None


def _owned_path(base: Path, build_id: str, path: Path) -> tuple[Path | None, str | None]:
    if not SAFE_BUILD_ID.fullmatch(str(build_id or "")):
        return None, f"build id {build_id!r} is not one safe path segment"
    try:
        root = base.expanduser().resolve()
        wanted = path.expanduser().resolve()
    except OSError as exc:
        return None, f"could not resolve the autobuild worktree path: {exc}"
    expected = root / build_id
    if wanted != expected:
        return None, (
            f"{wanted} is not the configured autobuild worktree "
            f"{expected} for {build_id}"
        )
    return wanted, None


def inspect_autobuild_worktree(
    *, repo: Path, base: Path, build_id: str, path: Path
) -> dict[str, Any]:
    """Return the exact Git-owned identity of one autobuild worktree.

    ``ok=False`` is a fail-closed answer.  The registration and status digest
    lists are stable JSON values suitable for carrying on the durable offer.
    """
    wanted, error = _owned_path(base, build_id, path)
    answer: dict[str, Any] = {
        "ok": False,
        "build_id": build_id,
        "path": str(path),
        "base": str(base),
        "registrations": [],
        "nested_registrations": [],
        "capacity": _capacity(base if base.exists() else base.parent),
    }
    if error or wanted is None:
        answer["detail"] = error
        return answer
    answer["path"] = str(wanted)
    answer["base"] = str(base.expanduser().resolve())
    rows, error = _registrations(repo)
    if rows is None:
        answer["detail"] = f"git worktree registrations could not be read: {error}"
        return answer
    by_path = {str(row.get("path")): row for row in rows if row.get("path")}
    outer = by_path.get(str(wanted))
    if outer is None:
        answer["detail"] = f"{wanted} is not registered as a worktree of {repo}"
        return answer
    inner_root = wanted / INNER_WORKTREES_REL
    descendants: list[dict[str, str | None]] = []
    unexpected: list[str] = []
    for row in rows:
        raw_path = row.get("path")
        if not raw_path or raw_path == str(wanted):
            continue
        candidate = Path(str(raw_path))
        try:
            candidate.relative_to(wanted)
        except ValueError:
            continue
        try:
            candidate.relative_to(inner_root)
        except ValueError:
            unexpected.append(str(candidate))
        else:
            descendants.append(dict(row))
    if unexpected:
        answer["detail"] = (
            "registered descendants outside .guardkit/worktrees were found: "
            + ", ".join(sorted(unexpected))
        )
        answer["unexpected_registrations"] = sorted(unexpected)
        return answer
    owned = [dict(outer), *sorted(descendants, key=lambda row: str(row["path"]))]
    for row in owned:
        row_path = Path(str(row["path"]))
        registered_descendants = tuple(
            Path(str(other["path"]))
            for other in owned
            if other is not row
            and _is_relative_to(Path(str(other["path"])), row_path)
        )
        status_sha, status_error = _status_sha(
            row_path, registered_descendants=registered_descendants
        )
        if status_sha is None:
            answer["detail"] = (
                f"git status could not be read in {row['path']}: {status_error}"
            )
            return answer
        row["status_sha256"] = status_sha
    answer.update(
        {
            "ok": True,
            "detail": "registered autobuild worktree identity read",
            "registrations": owned,
            "nested_registrations": owned[1:],
        }
    )
    return answer


# READ-ONLY FOLDERS (15 October 2026, FEAT-6AC9). Tools write caches with
# folders their owner cannot write (a language's module cache is the usual
# one), inside a worktree or inside the scratch area GuardKit keeps in Git's
# own record folder for it. Git cannot delete inside such a folder: the live
# `git worktree remove --force` deleted the task's working folder, stopped on
# the record folder with exit 255 and left a "prunable" registration. So,
# just before Git removes an offer-pinned worktree, its folder and its record
# folder are made owner-writable. Only folders need it (deleting a file needs
# a writable folder, not a writable file). Symbolic links are never followed:
# a link is not a folder here, whatever it points at.


def _make_owner_writable(top: Path) -> None:
    """Give the owner full access to every folder under ``top``, links skipped.

    Best effort: a folder that cannot be changed is left for Git to report,
    and the receipt says so in the helper's fixed sentence.
    """

    def widen(name: str, dir_fd: int | None) -> None:
        try:
            mode = os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode
            if not stat.S_ISDIR(mode) or stat.S_IMODE(mode) & 0o700 == 0o700:
                return
            # Checked without following links just above; chmod cannot be told
            # not to follow one on Linux, and nothing else writes here.
            os.chmod(name, stat.S_IMODE(mode) | 0o700, dir_fd=dir_fd)
        except OSError:
            return

    widen(str(top), None)
    try:
        for _root, directories, _files, descriptor in os.fwalk(
            str(top), topdown=True, follow_symlinks=False
        ):
            for name in directories:
                widen(name, descriptor)
    except OSError:
        return


def _worktrees_record_root(repo: Path) -> Path | None:
    """``<common git folder>/worktrees`` of ``repo``: where Git keeps the
    record folder of every worktree registered on it."""
    code, raw = _git(repo, "rev-parse", "--git-common-dir")
    if code != 0:
        return None
    common = Path(os.fsdecode(raw.strip()))
    if not common.is_absolute():
        common = repo / common
    return (common / "worktrees").resolve()


def _points_at(record: Path, worktree: Path) -> bool:
    """Does Git's record folder ``record`` belong to ``worktree``?"""
    try:
        pointer = Path(
            (record / "gitdir").read_text(encoding="utf-8", errors="surrogateescape")
            .strip()
        )
    except OSError:
        return False
    if not pointer.is_absolute():
        pointer = record / pointer
    return pointer.resolve() == (worktree / ".git").resolve()


def _record_folder(repo: Path, worktree: Path) -> Path | None:
    """Git's own record folder for one worktree registered on ``repo``.

    Read from the worktree's ``.git`` file while the folder is there, else
    found among the record folders by its back pointer. Either way it must be
    a direct child of ``repo``'s own ``worktrees`` record folder and point back
    at exactly ``worktree``; anything else is ``None`` and nothing is touched.
    """
    root = _worktrees_record_root(repo)
    if root is None or not root.is_dir():
        return None
    candidates: list[Path] = []
    marker = worktree / ".git"
    if marker.is_file() and not marker.is_symlink():
        try:
            line = marker.read_text(encoding="utf-8", errors="surrogateescape")
        except OSError:
            line = ""
        key, _sep, value = line.strip().partition(":")
        if key == "gitdir" and value.strip():
            named = Path(value.strip())
            candidates.append(named if named.is_absolute() else worktree / named)
    else:
        try:
            candidates.extend(sorted(root.iterdir()))
        except OSError:
            return None
    for candidate in candidates:
        if candidate.is_symlink():
            continue
        record = candidate.resolve()
        if record.parent == root and record.is_dir() and _points_at(record, worktree):
            return record
    return None


def _clear_gone_registrations(
    repo: Path, base: Path, build_id: str, path: Path, expected: Mapping[str, Any]
) -> list[str]:
    """Clear an offer-pinned registration whose working folder is already gone.

    An earlier attempt can delete a worktree's folder and then stop on its
    record folder, leaving a "prunable" registration that would keep the tree
    forever. For each exact path the offer named for clean-up (and the outer
    tree itself, once nothing else is registered below it) that Git still
    lists as prunable with no folder on disk, its own record folder is made
    writable and removed, which is what ``git worktree prune`` does for that
    one entry and no other. Returns the paths cleared.
    """
    wanted, error = _owned_path(base, build_id, path)
    if error or wanted is None or str(expected.get("path")) != str(wanted):
        return []
    cleanup = expected.get("cleanup_registrations")
    if not isinstance(cleanup, list):
        return []
    inner_root = wanted / INNER_WORKTREES_REL
    pinned: list[Path] = []
    for selected in cleanup:
        if not isinstance(selected, Mapping):
            return []
        nested = Path(str(selected.get("path") or ""))
        if not _is_relative_to(nested, inner_root):
            return []
        pinned.append(nested)
    cleared: list[str] = []

    def clear(target: Path) -> bool:
        rows, _error = _registrations(repo)
        if rows is None:
            return False
        row = next((r for r in rows if r.get("path") == str(target)), None)
        if row is None or "prunable" not in row or os.path.lexists(target):
            return False
        record = _record_folder(repo, target)
        if record is None:
            return False
        _make_owner_writable(record)
        try:
            shutil.rmtree(record)
        except OSError:
            return False
        cleared.append(str(target))
        return True

    for nested in sorted(pinned, key=lambda item: len(str(item)), reverse=True):
        clear(nested)
    rows, _error = _registrations(repo)
    if rows is not None and not any(
        _is_relative_to(Path(str(r.get("path"))), wanted) for r in rows
    ):
        clear(wanted)
    return cleared


def _remove_worktree(repo: Path, worktree: Path) -> tuple[int, bytes]:
    """``git worktree remove --force`` for one offer-pinned worktree, with its
    folder and its record folder made owner-writable first."""
    record = _record_folder(repo, worktree)
    _make_owner_writable(worktree)
    if record is not None:
        _make_owner_writable(record)
    return _git(repo, "worktree", "remove", "--force", str(worktree))


def retire_autobuild_worktree(
    *, repo: Path, base: Path, build_id: str, path: Path, expected: Mapping[str, Any]
) -> dict[str, Any]:
    """Remove one offer-pinned tree, nested registrations first.

    A missing tree is idempotent success only when Git also has no registration
    for it.  Any mismatch from the offer-time registration/status identity
    preserves the whole tree.  Each offer-pinned tree, and Git's record folder
    for it, is made owner-writable just before Git removes it, and a pinned
    registration whose folder an earlier attempt already deleted is cleared
    first (15 October 2026, FEAT-6AC9).
    """
    # A retry after an attempt that deleted a folder but not its record:
    # clear exactly those offer-pinned registrations first, then read the
    # identity as it is now.
    cleared = _clear_gone_registrations(repo, base, build_id, path, expected)
    before = inspect_autobuild_worktree(
        repo=repo, base=base, build_id=build_id, path=path
    )
    report: dict[str, Any] = {
        "status": "kept",
        "build_id": build_id,
        "path": str(path),
        "before": before,
        "removed_nested": [],
    }
    if cleared:
        report["cleared_registrations"] = cleared
    if not before.get("ok"):
        # Idempotent retry: exact owned path absent and no Git registration.
        wanted, ownership_error = _owned_path(base, build_id, path)
        rows, rows_error = _registrations(repo)
        if (
            ownership_error is None
            and wanted is not None
            and not wanted.exists()
            and rows is not None
            and all(str(row.get("path")) != str(wanted) for row in rows)
        ):
            report.update(status="already-gone", detail="owned worktree already gone")
            return report
        report["detail"] = before.get("detail") or rows_error or "identity unreadable"
        return report
    # A registration cleared above is gone from Git's list now, and Git's
    # status of the tree that held it no longer shows it, so that tree's
    # status digest cannot match its offer-time one. Its path, commit and
    # branch must still match exactly, and every other row matches in full.
    gone = set(cleared)
    expected_registrations = expected.get("registrations")
    current_registrations = before.get("registrations")
    if gone and isinstance(expected_registrations, list):

        def holds_a_cleared_tree(row: Any) -> bool:
            return isinstance(row, Mapping) and any(
                _is_relative_to(Path(g), Path(str(row.get("path")))) for g in gone
            )

        def without_status(rows: list[Any]) -> list[Any]:
            return [
                {k: v for k, v in row.items() if k != "status_sha256"}
                if holds_a_cleared_tree(row)
                else row
                for row in rows
            ]

        expected_registrations = without_status(
            [
                row
                for row in expected_registrations
                if not (isinstance(row, Mapping) and row.get("path") in gone)
            ]
        )
        if isinstance(current_registrations, list):
            current_registrations = without_status(current_registrations)
    expected_identity = {
        "build_id": expected.get("build_id"),
        "path": expected.get("path"),
        "base": expected.get("base"),
        "registrations": expected_registrations,
    }
    current_identity = {
        "build_id": before.get("build_id"),
        "path": before.get("path"),
        "base": before.get("base"),
        "registrations": current_registrations,
    }
    if current_identity != expected_identity:
        report["detail"] = (
            "offer-time worktree identity no longer matches; preserving the tree"
        )
        return report
    nested = list(before.get("nested_registrations") or [])
    cleanup = expected.get("cleanup_registrations")
    if not isinstance(cleanup, list) or not cleanup:
        report["detail"] = (
            "offer-time identity names no exact nested cleanup registration; "
            "preserving the tree"
        )
        return report
    cleanup_rows: list[dict[str, Any]] = []
    for selected in cleanup:
        if not isinstance(selected, Mapping):
            report["detail"] = "cleanup registration identity is malformed"
            return report
        if selected.get("path") in gone:
            continue
        matches = [row for row in nested if row == dict(selected)]
        if len(matches) != 1:
            report["detail"] = (
                "cleanup registration does not exactly match the current "
                "offer-pinned identity; preserving the tree"
            )
            return report
        cleanup_rows.append(matches[0])
    if len({str(row["path"]) for row in cleanup_rows}) != len(cleanup_rows):
        report["detail"] = "cleanup registration identity contains duplicate paths"
        return report
    for row in sorted(
        cleanup_rows, key=lambda item: len(str(item["path"])), reverse=True
    ):
        nested_path = str(row["path"])
        code, raw = _remove_worktree(repo, Path(nested_path))
        if code != 0:
            report["detail"] = (
                f"could not remove registered nested worktree {nested_path}: "
                + raw.decode("utf-8", errors="replace").strip()
            )
            return report
        report["removed_nested"].append(nested_path)
    # Re-read after nested removal.  An unexpected descendant appearing here
    # is preserved rather than being recursively destroyed with the outer.
    rows, error = _registrations(repo)
    wanted = Path(str(before["path"]))
    if rows is None:
        report["detail"] = f"registrations could not be re-read before removal: {error}"
        return report
    descendants = []
    for row in rows:
        raw_path = row.get("path")
        if not raw_path or raw_path == str(wanted):
            continue
        try:
            Path(str(raw_path)).relative_to(wanted)
        except ValueError:
            continue
        descendants.append(str(raw_path))
    if descendants:
        report["detail"] = (
            "registered descendants appeared before outer removal; preserving: "
            + ", ".join(sorted(descendants))
        )
        return report
    code, raw = _remove_worktree(repo, wanted)
    if code != 0:
        report["detail"] = (
            f"could not remove outer worktree {wanted}: "
            + raw.decode("utf-8", errors="replace").strip()
        )
        return report
    report.update(
        status="removed",
        detail="offer-pinned nested worktrees and outer worktree removed",
        after_capacity=_capacity(Path(str(before["base"]))),
    )
    # A prepared feature's build makes its own local branch at the commit it
    # was admitted at (``forge/source/<build_id>``, 4 October 2026); it goes
    # with the worktree. Best effort: a branch that is not there, or cannot be
    # deleted, changes nothing about the removal.
    source = f"forge/source/{build_id}"
    code, _ = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{source}")
    if code == 0:
        deleted, _raw = _git(repo, "branch", "-D", source)
        if deleted == 0:
            report["source_branch_removed"] = source
    return report

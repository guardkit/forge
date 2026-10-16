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


def _subfolders(descriptor: int) -> list[str]:
    """The names of the real folders (never links) in an open folder."""
    with os.scandir(descriptor) as entries:
        return [
            entry.name for entry in entries if entry.is_dir(follow_symlinks=False)
        ]


_OPEN_FOLDER = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _widen_by_handle(descriptor: int) -> None:
    """Give the owner full access to the open folder ``descriptor`` and to
    every real folder below it, each opened without following links.

    The walk only adds owner access to folders the cleaning user already
    owns, which that user could change anyway, so a walk redirected
    elsewhere gains nothing. A folder owned by anyone else is skipped with
    everything below it, and so is anything that cannot be opened as a folder
    without following a link; Git's removal then says "kept".
    """
    mode = os.fstat(descriptor)
    if not stat.S_ISDIR(mode.st_mode) or mode.st_uid != os.geteuid():
        return
    if stat.S_IMODE(mode.st_mode) & 0o700 != 0o700:
        os.fchmod(descriptor, stat.S_IMODE(mode.st_mode) | 0o700)
    for child in _subfolders(descriptor):
        try:
            opened = os.open(child, _OPEN_FOLDER, dir_fd=descriptor)
        except OSError:
            continue
        try:
            _widen_by_handle(opened)
        finally:
            os.close(opened)


# Passing through a folder needs only search access, so the folders on the
# way down are opened as paths (Linux), and only the last one for reading.
# Read as a plain attribute, never as getattr(os, ...): handing the os module
# to getattr hides which attribute is reached, and the estate's launch check
# rightly refuses that for a module that can start programs.
try:
    _OPEN_AS_A_PATH = os.O_PATH
except AttributeError:  # not Linux: open for reading instead
    _OPEN_AS_A_PATH = os.O_RDONLY
_PASS_THROUGH = _OPEN_AS_A_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _open_folder_strictly(path: Path) -> int | None:
    """Open the folder at absolute ``path`` one name at a time from ``/``,
    never following a link at any step; ``None`` if any step is a link, is
    not a folder, or cannot be opened. The caller closes the handle."""
    parts = path.parts
    if not path.is_absolute() or any(part in {"", ".", ".."} for part in parts[1:]):
        return None
    try:
        current = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return None
    for index, part in enumerate(parts[1:], start=2):
        flags = _OPEN_FOLDER if index == len(parts) else _PASS_THROUGH
        try:
            following = os.open(part, flags, dir_fd=current)
        except OSError:
            os.close(current)
            return None
        os.close(current)
        current = following
    return current


def _make_owner_writable(top: Path) -> None:
    """Give the owner full access to every folder under ``top``, links skipped.

    ``top`` is reached one name at a time from ``/`` without following a link
    at any step, and every folder below it is opened from its parent's open
    handle the same way, so a link put in any folder's place, above the tree
    or inside it, is never followed. If ``top`` cannot be reached that way,
    nothing is changed. Best effort: what cannot be changed is left for Git
    to report ("kept").
    """
    descriptor = _open_folder_strictly(top)
    if descriptor is None:
        return
    try:
        _widen_by_handle(descriptor)
    except (OSError, RecursionError):
        return
    finally:
        os.close(descriptor)


def _worktrees_record_root(repo: Path) -> Path | None:
    """``<repository's .git folder>/worktrees``: where Git keeps the record
    folder of every worktree registered on ``repo``.

    Built from the path Git gives, made absolute and tidied by text alone,
    never by following links: a link anywhere on it must stay visible so
    that :func:`_open_folder_strictly` can refuse it.
    """
    code, raw = _git(repo, "rev-parse", "--git-common-dir")
    if code != 0:
        return None
    common = Path(os.fsdecode(raw.strip()))
    if not common.is_absolute():
        common = repo / common
    if not common.is_absolute():
        return None
    return Path(os.path.normpath(common / "worktrees"))


def _read_small_file(folder: int, name: str) -> str | None:
    """Read the regular file ``name`` inside the open folder ``folder``, never
    through a link; ``None`` if it is anything else or cannot be read."""
    try:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=folder
        )
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        return os.read(descriptor, 64 * 1024).decode("utf-8", errors="surrogateescape")
    except OSError:
        return None
    finally:
        os.close(descriptor)


def _open_record_folder(repo: Path, worktree: Path) -> int | None:
    """An open handle on Git's own record folder for ``worktree``, or ``None``.

    The worktree's ``.git`` file names the record folder. That name must be,
    by text alone, exactly ``<repository's .git folder>/worktrees/<name>``;
    the folder is reached from ``/`` without following a link at any step;
    and its ``gitdir`` file, read through that same handle, must point back
    at exactly ``worktree``. Anything else is ``None`` and nothing is
    touched. The caller closes the handle.
    """
    root = _worktrees_record_root(repo)
    worktree_folder = _open_folder_strictly(worktree)
    if root is None or worktree_folder is None:
        if worktree_folder is not None:
            os.close(worktree_folder)
        return None
    try:
        marker = _read_small_file(worktree_folder, ".git")
    finally:
        os.close(worktree_folder)
    if marker is None:
        return None
    key, _sep, value = marker.strip().partition(":")
    if key != "gitdir" or not value.strip():
        return None
    named = Path(value.strip())
    if not named.is_absolute():
        named = worktree / named
    record = Path(os.path.normpath(named))
    if record.parent != root or not record.name:
        return None
    descriptor = _open_folder_strictly(record)
    if descriptor is None:
        return None
    pointer_text = _read_small_file(descriptor, "gitdir")
    if pointer_text is not None:
        pointer = Path(pointer_text.strip())
        if not pointer.is_absolute():
            pointer = record / pointer
        if os.path.normpath(pointer) == os.path.normpath(worktree / ".git"):
            return descriptor
    os.close(descriptor)
    return None


def make_worktree_removable(worktree: Path) -> None:
    """Make a linked worktree's folder and Git's record folder for it
    owner-writable, just before Git is asked to remove it. Best effort.

    15 October 2026, FEAT-D267: the merge's join folder held a Go module
    cache (read-only folders) in GuardKit's scratch area inside its record
    folder, so ``git worktree remove --force`` deleted the folder and then
    stopped with exit 255, exactly as the retained worktree did for
    FEAT-6AC9. The same two walks are used here, and nothing else: the
    record folder is found from the worktree itself and must point back at
    exactly it, and no link is followed. Anything that cannot be reached
    that way is left alone, and Git reports what it could not remove.
    """
    try:
        _make_owner_writable(worktree)
        record = _open_record_folder(worktree, worktree)
        if record is None:
            return
        try:
            _widen_by_handle(record)
        finally:
            os.close(record)
    except (OSError, RecursionError):
        return


def _remove_worktree(repo: Path, worktree: Path) -> tuple[int, bytes]:
    """``git worktree remove --force`` for one offer-pinned worktree, with its
    folder and its record folder made owner-writable first.

    The record folder is walked from the very handle its back pointer was
    read through, so what was checked is what is changed.
    """
    # FAIL CLOSED (Codex review, R3, round 5): if either the worktree or its
    # record folder cannot be reached without following a link, Git is not
    # asked to remove anything, because Git itself would follow that link.
    top = _open_folder_strictly(worktree)
    if top is None:
        return 1, b"the worktree could not be reached without following a link"
    os.close(top)
    record = _open_record_folder(repo, worktree)
    if record is None:
        return 1, b"its record folder could not be reached and checked without following a link"
    _make_owner_writable(worktree)
    try:
        _widen_by_handle(record)
    except (OSError, RecursionError):
        pass
    finally:
        os.close(record)
    return _git(repo, "worktree", "remove", "--force", str(worktree))


def retire_autobuild_worktree(
    *, repo: Path, base: Path, build_id: str, path: Path, expected: Mapping[str, Any]
) -> dict[str, Any]:
    """Remove one offer-pinned tree, nested registrations first.

    A missing tree is idempotent success only when Git also has no registration
    for it.  Any mismatch from the offer-time registration/status identity
    preserves the whole tree.  Each offer-pinned tree, and Git's record folder
    for it, is made owner-writable just before Git removes it (15 October
    2026, FEAT-6AC9). Nothing else is ever cleared: a registration an
    earlier attempt left behind stays, and so does its tree ("kept").
    """
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
    expected_identity = {
        "build_id": expected.get("build_id"),
        "path": expected.get("path"),
        "base": expected.get("base"),
        "registrations": expected.get("registrations"),
    }
    current_identity = {
        "build_id": before.get("build_id"),
        "path": before.get("path"),
        "base": before.get("base"),
        "registrations": before.get("registrations"),
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

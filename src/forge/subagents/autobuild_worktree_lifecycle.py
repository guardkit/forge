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
import subprocess
from pathlib import Path
from typing import Any, Mapping

FORGE_AUTOBUILD_WORKTREE_BASE_ENV = "FORGE_AUTOBUILD_WORKTREE_BASE"
DEFAULT_AUTOBUILD_WORKTREE_BASE = "/tmp/forge-autobuild-worktrees"
GIB = 1024**3

SAFE_BUILD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
INNER_WORKTREES_REL = Path(".guardkit/worktrees")


def _git(repo: Path, *args: str) -> tuple[int, bytes]:
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(repo), *args],
            capture_output=True,
            check=False,
        )
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


def inspect_worktree_capacity(base: Path, *, min_available_bytes: int) -> dict[str, Any]:
    """Check the filesystem that will hold ``base`` before creating anything."""
    probe = base.expanduser()
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    capacity = _capacity(probe)
    report: dict[str, Any] = {
        "ok": False,
        "base": str(base.expanduser()),
        "probed_path": str(probe),
        "min_available_bytes": int(min_available_bytes),
        "capacity": capacity,
    }
    if min_available_bytes <= 0:
        report["detail"] = "the configured minimum available bytes must be positive"
    elif capacity is None:
        report["detail"] = "worktree filesystem capacity could not be read"
    elif capacity["available_inodes"] <= 0:
        report["detail"] = "worktree filesystem has no available inodes"
    elif capacity["available_bytes"] < min_available_bytes:
        report["detail"] = (
            f"worktree filesystem has {capacity['available_bytes']} available bytes, "
            f"below the required {min_available_bytes}"
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


def retire_autobuild_worktree(
    *, repo: Path, base: Path, build_id: str, path: Path, expected: Mapping[str, Any]
) -> dict[str, Any]:
    """Remove one offer-pinned tree, nested registrations first.

    A missing tree is idempotent success only when Git also has no registration
    for it.  Any mismatch from the offer-time registration/status identity
    preserves the whole tree.
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
        code, raw = _git(repo, "worktree", "remove", "--force", nested_path)
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
    code, raw = _git(repo, "worktree", "remove", "--force", str(wanted))
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
    return report

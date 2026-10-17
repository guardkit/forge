"""Fetching a project into the sweep's own mirror, and writing out its files.

Public surface
==============

- :func:`fetch_mirror` — the project's default branch, with its history,
  fetched from the recorded address into the sweep's own bare copy
  (:func:`forge.network_git.fetch_default_branch_copy`). The coordinator's
  project clones are never touched.
- :func:`export_committed_files` — the committed files of one commit, written
  out straight from the mirror's object store (Codex R12).

Why not ``git archive`` (Codex R12)
-----------------------------------

``git archive`` obeys the repository's own ``.gitattributes``, so an
``export-ignore`` line there would drop a lockfile or a Compose file before
any scanner saw it. The export instead lists the tree with ``git ls-tree -r
-z -l <commit>`` and writes each file's exact bytes from ``git cat-file
--batch``. No attributes, filters, keyword substitution or line-ending
conversion apply to either command.

- **Symlinks are not followed.** Each is recorded and skipped.
- **Submodules are skipped** and recorded.
- **A path that could escape the export folder** (``..``, a ``.git`` folder,
  an absolute path) is skipped and counted.
- **The scanners' own settings files are left out** (Codex R9):
  ``.gitleaks.toml``, ``.gitleaksignore``, ``osv-scanner.toml`` (and
  ``.osv-scanner.toml``) and ``.semgrepignore``, at any depth. The coding
  model can write any file in a project, so none of them may decide what a
  scanner reports.
- **Very large files** (over :data:`LARGEST_FILE_BYTES`) are left out and
  counted; the scanners skip files that size anyway.

Every git command runs through :func:`forge.network_git.run_network_git`
with no transport allowed, in the factory's own copy, as the rest of Forge's
git does.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from forge import network_git

#: Settings files a scanner would read from the scanned tree.
SCANNER_SETTINGS_FILES: frozenset[str] = frozenset(
    {".gitleaks.toml", ".gitleaksignore", "osv-scanner.toml", ".osv-scanner.toml", ".semgrepignore"}
)

#: The largest file the export writes out.
LARGEST_FILE_BYTES = 20 * 1024 * 1024

#: How many bytes one ``cat-file --batch`` call may return.
BATCH_BYTES = 64 * 1024 * 1024

#: Time limit for one local git command against the mirror.
GIT_TIMEOUT_S = 300.0

_MODE_SYMLINK = b"120000"
_MODE_SUBMODULE = b"160000"


@dataclass(frozen=True)
class Mirror:
    """A fetched project: its mirror, default branch and commit, or why not."""

    path: Path | None = None
    branch: str | None = None
    commit: str | None = None
    refusal: str | None = None


def fetch_mirror(address: str, mirrors_root: Path, *, timeout: float = 600.0) -> Mirror:
    """Fetch the default branch of ``address`` into the sweep's own mirror."""
    point = network_git.fetch_default_branch_copy(
        address, copies_root=mirrors_root, timeout=timeout
    )
    if point.refusal or not point.commit:
        return Mirror(refusal=point.refusal or "the project could not be fetched")
    return Mirror(
        path=network_git.factory_copy_for(address, mirrors_root),
        branch=point.branch,
        commit=point.commit,
    )


@dataclass
class ExportReport:
    """What the export wrote and what it left out (paths, never contents)."""

    files: int = 0
    symlinks: list[str] = field(default_factory=list)
    submodules: list[str] = field(default_factory=list)
    settings_left_out: list[str] = field(default_factory=list)
    too_large: list[str] = field(default_factory=list)
    unsafe_paths: int = 0


class ExportFailed(RuntimeError):
    """The committed files could not be written out."""


def _safe_path(raw: bytes) -> PurePosixPath | None:
    text = os.fsdecode(raw)
    pure = PurePosixPath(text)
    if pure.is_absolute() or not pure.parts:
        return None
    for part in pure.parts:
        if part in ("", ".", "..") or part.lower() == ".git":
            return None
    return pure


def _git(args: list[str], mirror: Path, *, data: bytes | None = None) -> bytes:
    done = network_git.run_network_git(
        args,
        transport="none",
        git_dir=mirror,
        timeout=GIT_TIMEOUT_S,
        binary=True,
        input_bytes=data,
    )
    if not done.ok:
        raise ExportFailed(done.said or "git could not read the mirror")
    return done.data


def export_committed_files(mirror: Path, commit: str, destination: Path) -> ExportReport:
    """Write the committed files of ``commit`` into ``destination``.

    ``destination`` must be an empty folder the sweep made. Raises
    :class:`ExportFailed` when git cannot read the mirror.
    """
    report = ExportReport()
    listing = _git(["ls-tree", "-r", "-z", "-l", "--full-tree", commit], mirror)
    wanted: list[tuple[bytes, PurePosixPath, int]] = []
    for entry in listing.split(b"\x00"):
        if not entry:
            continue
        meta, _, raw_path = entry.partition(b"\t")
        fields = meta.split()
        if len(fields) < 4:
            continue
        mode, kind, sha, size = fields[0], fields[1], fields[2], fields[3]
        path = _safe_path(raw_path)
        if path is None:
            report.unsafe_paths += 1
            continue
        if mode == _MODE_SYMLINK:
            report.symlinks.append(path.as_posix())
            continue
        if mode == _MODE_SUBMODULE or kind == b"commit":
            report.submodules.append(path.as_posix())
            continue
        if kind != b"blob":
            continue
        if path.name in SCANNER_SETTINGS_FILES:
            report.settings_left_out.append(path.as_posix())
            continue
        try:
            length = int(size)
        except ValueError:
            continue
        if length > LARGEST_FILE_BYTES:
            report.too_large.append(path.as_posix())
            continue
        wanted.append((sha, path, length))

    root = destination.resolve()
    batch: list[tuple[bytes, PurePosixPath, int]] = []
    batch_bytes = 0
    for item in wanted:
        if batch and batch_bytes + item[2] > BATCH_BYTES:
            report.files += _write_batch(mirror, batch, root)
            batch, batch_bytes = [], 0
        batch.append(item)
        batch_bytes += item[2]
    if batch:
        report.files += _write_batch(mirror, batch, root)
    return report


def _write_batch(mirror: Path, batch: list[tuple[bytes, PurePosixPath, int]], root: Path) -> int:
    request = b"".join(sha + b"\n" for sha, _path, _size in batch)
    output = _git(["cat-file", "--batch"], mirror, data=request)
    offset = 0
    written = 0
    for sha, path, _size in batch:
        newline = output.find(b"\n", offset)
        if newline < 0:
            raise ExportFailed("git's file listing ended early")
        header = output[offset:newline].split()
        offset = newline + 1
        if len(header) < 3 or header[1] != b"blob" or header[0] != sha:
            raise ExportFailed("git's file listing did not match what was asked for")
        length = int(header[2])
        body = output[offset : offset + length]
        offset += length + 1  # the content, then one newline
        target = root.joinpath(*path.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Created new, never through an existing name: the export folder is
        # fresh and nothing in it is a link.
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
        written += 1
    return written


__all__ = [
    "BATCH_BYTES",
    "ExportFailed",
    "ExportReport",
    "LARGEST_FILE_BYTES",
    "Mirror",
    "SCANNER_SETTINGS_FILES",
    "export_committed_files",
    "fetch_mirror",
]

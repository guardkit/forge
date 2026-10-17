"""The sweep's own folder: its saved state, its history, its key and its lock.

Everything lives in one folder under the Forge state volume (beside the
coordinator's database, ``<database folder>/security-sweep/``):

- ``state.json`` — every finding the sweep has seen, by fingerprint, with its
  status (``pending``, ``not-real``, ``real``, ``drafted`` with its queue row,
  or ``gone``) and the allowlisted fields of :class:`~.findings.Finding`;
  plus the date of the last weekly all-clear. Written whole, through a
  temporary file and a rename, so a crash leaves the old file or the new one.
- ``history.jsonl`` — one line per run: when, how it ended, and per project
  the counts. Allowlisted fields only.
- ``fingerprint.key`` — the 32 random bytes the line fingerprints are keyed
  with (Codex R11), made on the first run, readable by this user only
  (mode 0600). Losing it changes every secret and code fingerprint, so it is
  kept apart from the state file: deleting ``state.json`` keeps it.
- ``sweep.lock`` — the lock one sweep holds while it runs. The scheduled run
  and a hand-run ``forge security-sweep --once`` take the same lock, so two
  sweeps never run together (Codex R6).
- ``mirrors/`` — the sweep's own bare copies of each project, fetched from
  the recorded address (never the coordinator's project clones).
- ``work/`` — a project's exported files while it is being scanned; removed
  after each project.

**The queue, not this file, is the record of what was drafted** (Codex R4).
The state file is a convenience: if it is lost or was never saved, the next
run reads the drafted fingerprints back from the work queue.
"""

from __future__ import annotations

import errno
import fcntl
import json
import logging
import os
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

#: The folder's name beside the coordinator's database.
FOLDER_NAME = "security-sweep"

STATE_FILE = "state.json"
HISTORY_FILE = "history.jsonl"
KEY_FILE = "fingerprint.key"
LOCK_FILE = "sweep.lock"
MIRRORS_FOLDER = "mirrors"
WORK_FOLDER = "work"

#: How many random bytes the fingerprint key holds.
KEY_BYTES = 32

STATE_VERSION = 1

#: The statuses a finding can have (design step 4).
STATUSES: tuple[str, ...] = ("pending", "not-real", "real", "drafted", "gone")


def sweep_folder(db_path: "str | os.PathLike[str]") -> Path:
    """The sweep's folder for the coordinator database at ``db_path``."""
    return Path(os.fspath(db_path)).expanduser().resolve().parent / FOLDER_NAME


def make_folder(folder: Path) -> Path:
    """Make the sweep's folder (and its two sub-folders), readable by this user only."""
    for path in (folder, folder / MIRRORS_FOLDER, folder / WORK_FOLDER):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return folder


class SweepAlreadyRunning(RuntimeError):
    """Another sweep holds the lock."""

    def __init__(self) -> None:
        super().__init__(
            "A security sweep is already running, so this one did not start."
        )


class SweepLock:
    """The one-sweep-at-a-time lock (an advisory file lock, never waited on)."""

    def __init__(self, folder: Path) -> None:
        self._path = folder / LOCK_FILE
        self._handle: Any = None

    def __enter__(self) -> "SweepLock":
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = open(self._path, "a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                raise SweepAlreadyRunning() from None
            raise
        self._handle = handle
        return self

    def __exit__(self, *exc: object) -> None:
        if self._handle is not None:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            finally:
                self._handle.close()
                self._handle = None


def fingerprint_key(folder: Path) -> bytes:
    """The installation's fingerprint key, made on first use (mode 0600).

    A key file of the wrong size is refused rather than silently replaced:
    replacing it would rename every finding, and each would be sorted (and
    possibly drafted) again.
    """
    path = folder / KEY_FILE
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        data = path.read_bytes()
        if len(data) != KEY_BYTES:
            raise RuntimeError(
                f"the sweep's fingerprint key at {path} is not {KEY_BYTES} bytes; "
                "move it aside only if every finding should be sorted again"
            ) from None
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode != 0o600:
            os.chmod(path, 0o600)
        return data
    try:
        key = os.urandom(KEY_BYTES)
        os.write(fd, key)
    finally:
        os.close(fd)
    return key


@dataclass
class SweepState:
    """The saved state: findings by fingerprint, and the all-clear date."""

    findings: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_all_clear: str | None = None
    #: The files the code scanner could not fully check on each project's last
    #: scan (shown paths only), so a file newly on that list is news and the
    #: same files every night are not (Codex implementation review 4, F5).
    code_partial: dict[str, list[str]] = field(default_factory=dict)

    def entries_for(self, project: str) -> Iterator[tuple[str, dict[str, Any]]]:
        for fingerprint, entry in self.findings.items():
            if entry.get("project") == project:
                yield fingerprint, entry

    def status(self, fingerprint: str) -> str | None:
        entry = self.findings.get(fingerprint)
        return str(entry.get("status")) if entry else None


def load_state(folder: Path) -> SweepState:
    """The saved state, or an empty one when there is none or it cannot be read.

    An unreadable file is moved aside (never deleted) and the run starts from
    an empty state: what was drafted is read back from the queue anyway.
    """
    path = folder / STATE_FILE
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return SweepState()
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("findings"), dict):
            raise ValueError("not a sweep state")
    except ValueError:
        aside = path.with_suffix(".unreadable")
        os.replace(path, aside)
        logger.warning(
            "security sweep: the saved state could not be read; it was moved "
            "aside and the run starts from an empty state"
        )
        return SweepState()
    findings = {
        str(fp): dict(entry)
        for fp, entry in data["findings"].items()
        if isinstance(entry, dict) and entry.get("status") in STATUSES
    }
    last = data.get("last_all_clear")
    partial_raw = data.get("code_partial")
    partial = {
        str(project): [str(path) for path in paths if isinstance(path, str)]
        for project, paths in (partial_raw.items() if isinstance(partial_raw, dict) else [])
        if isinstance(paths, list)
    }
    return SweepState(
        findings=findings,
        last_all_clear=str(last) if isinstance(last, str) else None,
        code_partial=partial,
    )


def save_state(folder: Path, state: SweepState) -> None:
    """Write the state whole: a temporary file, then a rename (mode 0600)."""
    body = json.dumps(
        {
            "version": STATE_VERSION,
            "findings": state.findings,
            "last_all_clear": state.last_all_clear,
            "code_partial": state.code_partial,
        },
        sort_keys=True,
        indent=1,
    )
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=str(folder))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, folder / STATE_FILE)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def append_history(folder: Path, line: dict[str, Any]) -> None:
    """Add one run's line to the history (allowlisted fields only)."""
    path = folder / HISTORY_FILE
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, sort_keys=True) + "\n")


__all__ = [
    "FOLDER_NAME",
    "HISTORY_FILE",
    "KEY_BYTES",
    "KEY_FILE",
    "LOCK_FILE",
    "MIRRORS_FOLDER",
    "STATE_FILE",
    "STATUSES",
    "SweepAlreadyRunning",
    "SweepLock",
    "SweepState",
    "WORK_FOLDER",
    "append_history",
    "fingerprint_key",
    "load_state",
    "make_folder",
    "save_state",
    "sweep_folder",
]

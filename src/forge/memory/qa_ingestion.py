"""Q&A history ingestion pipeline (TASK-IC-005).

This module scans operator Q&A history files and — for files whose
SHA-256 content hash differs from the snapshot store — parses their
Q&A entries and emits :class:`~forge.memory.models.CalibrationEvent`
entities into the ``forge_calibration_history`` Graphiti group via
:func:`forge.memory.writer.fire_and_forget_write`.

Behavioural contract
--------------------

* **Idempotent on unchanged content** — an unchanged file produces
  zero parse work and zero writes
  (``@data-integrity re-scan-zero-writes``).
* **Deterministic ``entity_id``** — derived from
  ``SHA-256(f"{source_file}:{line_range_hash}")`` where
  ``line_range_hash`` is the SHA-256 of the verbatim block text that
  produced a Q&A pair. Re-ingesting the same content under the same
  source path yields the same set of ``entity_id`` values
  (``@data-integrity deterministic-qa-identity``).
* **Partial-parse tolerance** — malformed blocks are skipped; the
  surrounding well-formed Q&A pairs are still ingested and the
  per-file snapshot record is flagged ``partial=True``
  (``@negative partial-parse-tolerance``).
* **Atomic snapshot updates** — :class:`HashSnapshotStore` buffers
  every staged update in memory and only persists once, via a
  ``tempfile + os.replace`` rename, so the on-disk snapshot file is
  never observed in a half-updated state.
* **Build-start AND post-build invocation** — the ``async def`` entry
  point is intended to be awaited at build start AND after each
  successful build (``@edge-case post-build-ingestion-refresh``); the
  function is identical at both call sites and is safe to call
  back-to-back because the snapshot comparison short-circuits on
  unchanged files.

Q&A file format
---------------

Operator-defined and tolerant of leading whitespace, BOM, and mixed
line endings. The default parser recognises blocks separated by blank
lines where each block contains a ``Q:`` line and an ``A:`` line. Lines
that do not match either prefix are ignored within the block. Blocks
missing either prefix count as a partial parse and are skipped — the
file is still ingested for its valid blocks.

Example::

    Q: How should the gating threshold be tuned?
    A: Increase to 0.85 for high-stakes builds.

    Q: When should we override the recommendation?
    A: Only when the rationale is grounded in evidence.

Module purity
-------------

* Imports are restricted to the standard library, this package's
  models, and the fire-and-forget writer (TASK-IC-002). No
  ``langgraph``, ``deepagents``, or ``forge.adapters.*`` imports.
* All Graphiti writes go through :func:`fire_and_forget_write` so a
  Graphiti outage never blocks the build pipeline (the lesson from
  ``guardkit__task_outcomes`` recorded in TASK-IC-002).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

from .models import CalibrationEvent
from .writer import fire_and_forget_write

#: Graphiti ``group_id`` for the calibration-history group. Mirrors
#: the constant documented in :mod:`forge.memory.models`. Kept local
#: so callers of this module do not need to know the group name.
CALIBRATION_GROUP_ID = "forge_calibration_history"

logger = logging.getLogger("forge.memory.qa_ingestion")


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class IngestionReport:
    """Counts produced by a single :func:`ingest_qa_history` invocation.

    Attributes:
        scanned: Total number of files inspected (includes files that
            were unreadable; those are logged and skipped without a
            hash comparison).
        changed: Number of files whose hash differs from the snapshot
            and were therefore re-parsed.
        events_emitted: Number of :class:`CalibrationEvent` entities
            successfully scheduled for Graphiti write.
        partial_parses: Number of malformed Q&A blocks skipped across
            all changed files. A non-zero value also flips the
            per-file ``partial`` flag in the snapshot store.
    """

    scanned: int = 0
    changed: int = 0
    events_emitted: int = 0
    partial_parses: int = 0


@dataclasses.dataclass(frozen=True)
class _QAPair:
    """Internal representation of a single parsed Q&A block.

    ``line_range_hash`` is the SHA-256 of the verbatim block text. It
    is the line-range component of the deterministic entity id: see
    :func:`_make_entity_id`.
    """

    question: str
    answer: str
    line_range_hash: str


# ---------------------------------------------------------------------------
# Hash snapshot store
# ---------------------------------------------------------------------------


class HashSnapshotStore:
    """JSON-backed map of ``source_file -> {hash, partial}``.

    The store buffers all updates in memory and only flushes them via
    :meth:`commit`. ``commit`` writes to a tempfile in the *target
    directory* and then renames it over the live file via
    :func:`os.replace`, so the on-disk snapshot is never observed in
    a half-updated state.

    Loading is defensive: a missing file is treated as "no snapshot
    yet" (empty map) and a corrupt or non-dict file is logged at
    warning level and also treated as empty so a single bad write
    doesn't brick subsequent ingest cycles.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._snapshots: dict[str, dict[str, object]] = self._load()

    # -- internal load ----------------------------------------------------

    def _load(self) -> dict[str, dict[str, object]]:
        """Return the persisted snapshot map, or ``{}`` on any failure."""
        if not self._path.exists():
            return {}
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "qa_snapshot_load_failed",
                extra={
                    "path": str(self._path),
                    "error_class": type(exc).__name__,
                    "error_message": str(exc),
                },
            )
            return {}
        if not isinstance(data, dict):
            logger.warning(
                "qa_snapshot_load_invalid_root",
                extra={
                    "path": str(self._path),
                    "type": type(data).__name__,
                },
            )
            return {}
        # Defensive copy + per-record type check so a partially-corrupt
        # file with one bad record doesn't poison every lookup.
        return {
            key: dict(value)
            for key, value in data.items()
            if isinstance(key, str) and isinstance(value, dict)
        }

    # -- read -------------------------------------------------------------

    def get_hash(self, file_path: str) -> str | None:
        """Return the stored content hash for ``file_path`` (or ``None``)."""
        record = self._snapshots.get(file_path)
        if record is None:
            return None
        value = record.get("hash")
        return value if isinstance(value, str) else None

    def get_partial(self, file_path: str) -> bool | None:
        """Return the stored ``partial`` flag for ``file_path``.

        Returns ``None`` when no record exists; otherwise coerces the
        stored value to ``bool``. ``None`` is distinguishable from
        ``False`` so callers that care about "have we ever seen this
        file" vs. "we saw this file with partial=False" can branch.
        """
        record = self._snapshots.get(file_path)
        if record is None:
            return None
        value = record.get("partial")
        return bool(value) if isinstance(value, bool) else None

    # -- write ------------------------------------------------------------

    def stage(self, file_path: str, content_hash: str, partial: bool) -> None:
        """Stage an update for ``file_path``. Not persisted until commit.

        Args:
            file_path: Key in the snapshot map (the string form of the
                ingested ``Path``).
            content_hash: 64-char hex SHA-256 of the file bytes; must
                be a non-empty string.
            partial: ``True`` when the file had at least one malformed
                block, ``False`` when every block parsed cleanly.

        Raises:
            ValueError: ``content_hash`` is empty or not a string.
        """
        if not isinstance(content_hash, str) or not content_hash:
            raise ValueError("content_hash must be a non-empty string")
        self._snapshots[file_path] = {
            "hash": content_hash,
            "partial": bool(partial),
        }

    def commit(self) -> None:
        """Atomically persist staged updates to disk.

        Writes to a tempfile in the *same directory* as the target so
        that the final rename is a same-filesystem rename (cross-device
        renames are not atomic on POSIX). Cleans up the tempfile on
        any failure path so a failed commit does not leak orphan files.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_str = tempfile.mkstemp(
            prefix=self._path.name + ".",
            suffix=".tmp",
            dir=str(self._path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._snapshots, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_str, self._path)
        except Exception:
            # Best-effort cleanup; never mask the original error. The
            # tempfile sits in the target directory, so it's the only
            # artefact that needs unlinking.
            if os.path.exists(tmp_str):
                try:
                    os.unlink(tmp_str)
                except OSError:  # pragma: no cover — defensive
                    pass
            raise


# ---------------------------------------------------------------------------
# Decoding and parsing helpers
# ---------------------------------------------------------------------------


def _decode_text(file_bytes: bytes) -> str:
    """Decode bytes as UTF-8 (BOM-tolerant) and normalise line endings.

    ``utf-8-sig`` strips a leading BOM if present; ``errors='replace'``
    means a malformed byte sequence becomes ``\\ufffd`` rather than
    aborting the ingest. Both ``\\r\\n`` and lone ``\\r`` are folded to
    ``\\n`` so the block-splitting logic only has to consider one
    newline form.
    """
    text = file_bytes.decode("utf-8-sig", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _content_hash(file_bytes: bytes) -> str:
    """Return ``hashlib.sha256(file_bytes).hexdigest()``."""
    return hashlib.sha256(file_bytes).hexdigest()


def _line_range_hash(block_lines: Sequence[str]) -> str:
    """Hash the verbatim block text used to construct a Q&A pair.

    The block lines are joined with ``\\n`` so the hash is invariant
    under cross-file copy-paste regardless of the original line
    endings (which were already normalised by :func:`_decode_text`).
    """
    payload = "\n".join(block_lines).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _make_entity_id(source_file: str, line_range_hash: str) -> str:
    """Deterministic ``entity_id`` from ``(source_file, line_range_hash)``.

    The :class:`CalibrationEvent.entity_id` contract requires that the
    same source file + line range round-trip to the same opaque id.
    Hashing the combined key gives a fixed-length 64-char hex digest
    that satisfies the model's ``min_length=1`` constraint and is
    stable across processes, machines, and Python versions.
    """
    payload = f"{source_file}:{line_range_hash}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def parse_qa_pairs(text: str) -> tuple[list[_QAPair], int]:
    """Parse ``text`` into ``(valid_pairs, partial_block_count)``.

    A "block" is a run of non-blank lines separated from neighbouring
    blocks by one or more blank lines. Within a block:

    * The first line whose stripped form starts with ``Q:`` (case
      insensitive) supplies the question.
    * The first line whose stripped form starts with ``A:`` (case
      insensitive) supplies the answer.

    Blocks missing either side are tallied in
    ``partial_block_count``. Surrounding well-formed blocks are still
    returned — partial-parse tolerance is a per-block property, not a
    per-file abort.

    Returns:
        A tuple ``(pairs, partial)`` where ``pairs`` is the ordered
        list of well-formed :class:`_QAPair` instances and ``partial``
        is the count of blocks that were skipped due to a missing
        question or answer.
    """
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in text.split("\n"):
        if line.strip() == "":
            if current:
                blocks.append(current)
                current = []
            continue
        current.append(line)
    if current:
        blocks.append(current)

    pairs: list[_QAPair] = []
    partial = 0
    for block in blocks:
        question: str | None = None
        answer: str | None = None
        for raw_line in block:
            stripped = raw_line.lstrip()
            lowered = stripped.lower()
            if question is None and lowered.startswith("q:"):
                question = stripped[2:].strip()
            elif answer is None and lowered.startswith("a:"):
                answer = stripped[2:].strip()
        if question and answer:
            pairs.append(
                _QAPair(
                    question=question,
                    answer=answer,
                    line_range_hash=_line_range_hash(block),
                )
            )
        else:
            partial += 1
    return pairs, partial


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def ingest_qa_history(
    file_paths: Sequence[Path],
    snapshot_store: HashSnapshotStore,
) -> IngestionReport:
    """Scan ``file_paths``; emit calibration events for changed files.

    For each file:

    1. Read the bytes (read errors are logged and the file skipped —
       it does not count as a hash change but does count as scanned).
    2. Compute the SHA-256 content hash.
    3. If the hash matches the snapshot, skip the file — no parse, no
       write. This is the ``re-scan-zero-writes`` contract.
    4. Otherwise decode the bytes (BOM- and CRLF-tolerant), parse the
       Q&A blocks, and emit one :class:`CalibrationEvent` per
       well-formed block via :func:`fire_and_forget_write`. Stage a
       snapshot update tagged with the file's new hash and a
       ``partial`` flag derived from whether any block in the file
       was malformed.

    The snapshot store is committed exactly once — at the end of the
    scan, after every file has been processed — so the on-disk
    snapshot file is never observed in a half-updated state. When no
    file changed, no commit is performed and the snapshot file is
    left untouched.

    Args:
        file_paths: Files to ingest. Order does not matter — the
            snapshot is keyed on each path's string form. Duplicates
            are tolerated (the second occurrence becomes a same-hash
            no-op).
        snapshot_store: An initialised :class:`HashSnapshotStore`
            with its in-memory state already loaded from disk.

    Returns:
        An :class:`IngestionReport` with scan counts.
    """
    scanned = 0
    changed = 0
    events_emitted = 0
    partial_parses = 0
    pending_updates: list[tuple[str, str, bool]] = []

    for path in file_paths:
        scanned += 1
        path_str = str(path)
        try:
            file_bytes = Path(path).read_bytes()
        except (OSError, FileNotFoundError) as exc:
            # An unreadable file is a transient operational concern,
            # not a fatal ingest error — log and continue. The file
            # does NOT get a snapshot update because we can't compute
            # its content hash, so the next ingest cycle will retry.
            logger.warning(
                "qa_file_unreadable",
                extra={
                    "path": path_str,
                    "error_class": type(exc).__name__,
                    "error_message": str(exc),
                },
            )
            continue

        content_hash = _content_hash(file_bytes)
        if snapshot_store.get_hash(path_str) == content_hash:
            # Unchanged file — re-scan-zero-writes contract.
            continue

        changed += 1
        text = _decode_text(file_bytes)
        pairs, partial_count = parse_qa_pairs(text)
        partial_parses += partial_count
        captured_at = datetime.now(UTC)

        for pair in pairs:
            entity = CalibrationEvent(
                entity_id=_make_entity_id(path_str, pair.line_range_hash),
                source_file=path_str,
                question=pair.question,
                answer=pair.answer,
                captured_at=captured_at,
                # AC-005: malformed blocks are SKIPPED, not flagged on
                # the event. The ``partial`` flag on a CalibrationEvent
                # would only be ``True`` if the parser recovered ONE
                # side of the Q&A — which this parser does not do. The
                # file-level partial signal lives on the snapshot
                # record, not on the emitted events.
                partial=False,
            )
            try:
                fire_and_forget_write(entity, CALIBRATION_GROUP_ID)
            except Exception as exc:  # noqa: BLE001 — boundary swallow
                # ``fire_and_forget_write`` is documented to never
                # raise, but defensively guard against future
                # regressions so a single emit failure cannot poison
                # the rest of the scan.
                logger.error(
                    "qa_emit_failed",
                    extra={
                        "entity_id": entity.entity_id,
                        "source_file": path_str,
                        "error_class": type(exc).__name__,
                        "error_message": str(exc),
                    },
                )
                continue
            events_emitted += 1

        pending_updates.append((path_str, content_hash, partial_count > 0))

    # Atomic commit: stage every pending update in memory, then call
    # commit() exactly once. If no file changed, the snapshot file is
    # left untouched.
    if pending_updates:
        for path_str, content_hash, has_partial in pending_updates:
            snapshot_store.stage(path_str, content_hash, has_partial)
        snapshot_store.commit()

    return IngestionReport(
        scanned=scanned,
        changed=changed,
        events_emitted=events_emitted,
        partial_parses=partial_parses,
    )


__all__ = [
    "CALIBRATION_GROUP_ID",
    "HashSnapshotStore",
    "IngestionReport",
    "ingest_qa_history",
    "parse_qa_pairs",
]

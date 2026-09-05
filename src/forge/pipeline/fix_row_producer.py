"""A failed build, or a merge that did not stay green, becomes a repair job.

Public surface
==============

- :func:`maybe_mint_fix_row` — the producer itself. Two callers, one
  behaviour: file one ``work_queue`` row of kind ``fix`` for the build that
  just ended badly, and return the row's id (``None`` when nothing was
  filed).
- :data:`SOURCE_BUILD_FAILED` / :data:`SOURCE_MERGE_REPORT` — the two words
  a caller names itself with, written down on the row's filing event.
- :func:`fix_correlation_id` and
  :func:`source_build_id_from_correlation_id` — the convention that links a
  repair back to the build it repairs, both directions.
- :func:`make_failure_pack_source_reader` — the conductor's seam built on
  that convention: which failed build's pack a running journey must read.

Why this exists
---------------

The conductor — the forge's own repair journey — has been switched on and
idle since 4 August: nothing has ever put a repair into its queue, so every
repair since then was done by an attended frontier session. This is the step
that puts one there. A build that lands FAILED, and a merge whose live
checks went red afterwards, are the two moments the factory learns something
is broken; each of them now files a row that says so, in a sentence a person
can read.

The three rules the producer keeps
----------------------------------

**One row per failure, however many times the news arrives.** The queue's
``correlation_id`` is UNIQUE and this producer always spells it
``fix-<the failed build's id>``. A redelivered envelope, a second boot that
replays the same terminal, or a merge report published twice therefore file
the SAME row — the database refuses the duplicate rather than the code
remembering not to make one.

**No row without evidence.** A FAILED build is only worth reviewing if it
left its failure pack behind (the receipts, the manifest, the log). When
:func:`~forge.pipeline.fix_journey_receipts.read_failure_pack` finds nothing,
the producer files nothing and says so in one log line: a repair journey with
no evidence would review blind. The merge half is different on purpose — a
merged build *succeeded*, so it has no failure pack, and the red live check
IS the evidence.

**No repair of a repair.** A mode-c build IS a repair journey. When one
of those fails, filing a repair of it would put the factory in a circle:
journey two reviews journey one's failure, fails in its turn, and files
journey three. So a source build whose mode is ``mode-c`` files nothing,
and says so in one log line naming the build. A person reads the failed
journey and decides what to do.

**It never raises.** Both callers are on a terminal write path: the build
state recorder is the single writer of ``builds.status``, and the merge
executor's report is the only durable record of what the merge did. A defect
in here must cost a queue row, never a build's ending. Every failure mode
lands in the log and returns ``None``.

References
----------
- ``docs/conductor-rewire-spec-2026-09-05.md`` rule 1.
- ``docs/work-queue-spec-2026-09-05.md`` contract 4 (the table).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


#: The build-state recorder's hook: a build's terminal hop landed FAILED.
SOURCE_BUILD_FAILED: str = "build-failed"

#: The merge executor's hook: the merge landed but the live checks went red.
SOURCE_MERGE_REPORT: str = "merge-report"

#: The prefix every fix row's correlation id carries. The whole idempotence
#: rule lives in this string plus the table's UNIQUE index.
FIX_CORRELATION_PREFIX: str = "fix-"

#: Who the producer is, on the events row it writes.
PRODUCER_ACTOR: str = "forge-fix-producer"

#: The filing event's action, as the spec names it.
MINTED_ACTION: str = "minted"

#: The build mode that IS a repair journey. A build of this mode never gets a
#: repair of its own (see the module docstring): the circle has to stop
#: somewhere and it stops at the first failed journey.
FIX_JOURNEY_MODE: str = "mode-c"

#: Longest sentence the producer will write. A failure reason can be a whole
#: stack trace; the queue's list is read by a person, so the sentence is cut
#: at a readable length and the rest stays in the build row and the pack.
MAX_SENTENCE_CHARS: int = 400


def fix_correlation_id(source_build_id: str) -> str:
    """The correlation id a repair of ``source_build_id`` is always filed under."""
    return f"{FIX_CORRELATION_PREFIX}{source_build_id}"


def source_build_id_from_correlation_id(correlation_id: str | None) -> str | None:
    """The failed build a fix correlation id names, or None if it names none.

    The inverse of :func:`fix_correlation_id`. This is how the conductor's
    pack reader finds the build a journey is repairing without a new column:
    the fix build carries the queue row's own correlation id, and that id
    spells out the build it came from.
    """
    if not correlation_id:
        return None
    if not correlation_id.startswith(FIX_CORRELATION_PREFIX):
        return None
    source = correlation_id[len(FIX_CORRELATION_PREFIX) :].strip()
    return source or None


def make_failure_pack_source_reader(pool: Any) -> Any:
    """``(fix build id) -> the failed build it is repairing, or None``.

    The seam the conductor's composition was missing: without it a fix
    journey reads the failure pack under its OWN build id, finds nothing,
    and reviews blind. There is no parent-build column on ``builds`` and
    this lane adds none — it does not need one. A repair's correlation id
    is always ``fix-<the failed build's id>`` (that is the queue's
    idempotence rule), the build carries the queue row's correlation id
    verbatim, so the link is already written down and this reads it back.

    A build that was queued some other way (an operator typing
    ``forge queue --mode c`` with a correlation id of their own) answers
    None, and the journey falls back to its own receipts directory — the
    documented default at ``fix_task_context_builder``.

    Never raises: a reader that threw would take a journey down with it.
    """

    def read(build_id: str) -> str | None:
        try:
            row = pool.get_build_row(build_id)
        except Exception as exc:  # noqa: BLE001 — a read never stops a journey
            logger.warning(
                "fix pack reader: could not read the builds row for %s "
                "(%s: %s)",
                build_id,
                type(exc).__name__,
                exc,
            )
            return None
        if row is None:
            return None
        source = source_build_id_from_correlation_id(
            getattr(row, "correlation_id", None)
        )
        if source is None:
            logger.info(
                "fix pack reader: %s does not name a failed build in its "
                "correlation id — the journey reads its own receipts",
                build_id,
            )
        return source

    return read


def maybe_mint_fix_row(
    *,
    pool: Any,
    build_id: str,
    source: str,
    detail: str | None = None,
    receipts_root: Any = None,
    clock: Any = None,
) -> int | None:
    """File one repair row for the build that just ended badly.

    Args:
        pool: The lifecycle persistence facade — read for the build's row
            (``feature_id``, ``repo``, ``originating_user``) and for the
            SQLite connection the queue lives on.
        build_id: The build that failed. This is the SOURCE build: the row's
            correlation id is ``fix-<build_id>`` and the fix journey reads
            this build's failure pack.
        source: :data:`SOURCE_BUILD_FAILED` or :data:`SOURCE_MERGE_REPORT` —
            which of the two moments this is, written down on the row.
        detail: What failed, in whatever words the caller has (the failure
            reason, or the merge report's one line). Trimmed to one readable
            sentence on the row; the full text stays where it already is.
        receipts_root: Injectable receipts root, for tests. ``None`` uses the
            routine path's own law.
        clock: Injectable clock handed to the queue store, for tests.

    Returns:
        The queue row's id — the ``#12`` a person types — whether this call
        filed it or found it already filed. ``None`` when nothing was filed:
        no build row, no failure pack behind a FAILED build, or anything at
        all going wrong (which is logged, never raised).
    """
    try:
        row = _build_row(pool, build_id)
        if row is None:
            logger.warning(
                "fix producer: no builds row for %s — no repair row filed",
                build_id,
            )
            return None

        if _is_fix_journey(row):
            logger.info(
                "fix producer: %s is itself a repair journey (mode-c) that "
                "ended badly (%s) — filing no repair row: a repair of a "
                "repair would go round in a circle, so this one is for a "
                "person to read",
                build_id,
                source,
            )
            return None

        pack_path = _pack_path(build_id, receipts_root=receipts_root)
        if source == SOURCE_BUILD_FAILED and pack_path is None:
            logger.info(
                "fix producer: build %s failed but left no failure pack — "
                "filing no repair row (a review with no evidence reviews "
                "blind)",
                build_id,
            )
            return None

        connection = _connection(pool)
        if connection is None:
            logger.warning(
                "fix producer: no database connection behind the pool for "
                "%s — no repair row filed",
                build_id,
            )
            return None

        from forge.planning.work_queue_store import WorkQueueStore

        store = WorkQueueStore(connection, clock=clock)
        feature_id = str(getattr(row, "feature_id", "") or "") or build_id
        target_repo = getattr(row, "repo", None)
        filed = store.file_sentence(
            correlation_id=fix_correlation_id(build_id),
            sentence=_sentence(
                source=source,
                feature_id=feature_id,
                target_repo=target_repo,
                detail=detail,
            ),
            originating_user=str(getattr(row, "originating_user", None) or "forge"),
            target_repo=str(target_repo) if target_repo else None,
            kind="fix",
            actor_identity=PRODUCER_ACTOR,
            action=MINTED_ACTION,
            triggered_by="forge-internal",
            extra_details={
                "source": source,
                "source_build_id": build_id,
                "failure_pack_path": pack_path,
            },
        )
        if filed.created:
            logger.info(
                "fix producer: %s failed (%s) — filed repair row #%d for "
                "%s in %s",
                build_id,
                source,
                filed.queue_id,
                feature_id,
                target_repo,
            )
        else:
            logger.info(
                "fix producer: %s already has repair row #%d — the news "
                "arrived twice and the queue kept one row",
                build_id,
                filed.queue_id,
            )
        return filed.queue_id
    except Exception as exc:  # noqa: BLE001 — a queue row never costs a build
        logger.warning(
            "fix producer: could not file a repair row for %s (%s: %s) — the "
            "build's own ending is unaffected",
            build_id,
            type(exc).__name__,
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _build_row(pool: Any, build_id: str) -> Any:
    reader = getattr(pool, "get_build_row", None)
    if reader is None:
        return None
    return reader(build_id)


def _is_fix_journey(row: Any) -> bool:
    """Whether this build was itself a repair journey (mode c).

    Reads the row's ``mode`` however it is spelled — the enum member, its
    value, or the raw text a database row hands back — because the two
    callers hand over whatever their own layer had.
    """
    mode = getattr(row, "mode", None)
    if mode is None:
        return False
    spelled = str(getattr(mode, "value", mode)).strip().lower().replace("_", "-")
    return spelled == FIX_JOURNEY_MODE


def _connection(pool: Any) -> Any:
    """The writer connection the queue tables live on, or None."""
    return getattr(pool, "connection", None)


def _pack_path(build_id: str, *, receipts_root: Any) -> str | None:
    """The failure pack's directory for this build, or None when there is none."""
    from forge.pipeline.fix_journey_receipts import read_failure_pack

    pack = read_failure_pack(build_id, receipts_root=receipts_root)
    return str(pack.pack_dir) if pack is not None else None


def _sentence(
    *,
    source: str,
    feature_id: str,
    target_repo: Any,
    detail: str | None,
) -> str:
    """One plain line naming the feature and what went wrong."""
    where = f" in {target_repo}" if target_repo else ""
    if source == SOURCE_MERGE_REPORT:
        opening = f"{feature_id} was merged{where} but the checks after it went red"
    else:
        opening = f"The build of {feature_id}{where} failed"
    trimmed = _one_line(detail)
    sentence = f"{opening}: {trimmed}" if trimmed else f"{opening}."
    if len(sentence) > MAX_SENTENCE_CHARS:
        sentence = sentence[: MAX_SENTENCE_CHARS - 1].rstrip() + "…"
    return sentence


def _one_line(detail: str | None) -> str:
    """A multi-line failure reason as one readable line."""
    if not detail:
        return ""
    return " ".join(str(detail).split())


__all__ = [
    "FIX_CORRELATION_PREFIX",
    "FIX_JOURNEY_MODE",
    "MAX_SENTENCE_CHARS",
    "MINTED_ACTION",
    "PRODUCER_ACTOR",
    "SOURCE_BUILD_FAILED",
    "SOURCE_MERGE_REPORT",
    "fix_correlation_id",
    "make_failure_pack_source_reader",
    "maybe_mint_fix_row",
    "source_build_id_from_correlation_id",
]

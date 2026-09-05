"""M5 — how often the factory closed its own defect, without a person.

Public surface
==============

- :data:`M5_SINCE` — the cutoff, ``2026-09-05``. Nothing before it counts.
- :func:`self_closed_defect_rate` — ``(numerator, denominator)``.

Why the cutoff exists, and why it is a constant
-----------------------------------------------

The conductor has completed exactly one production journey ever, on 4 August
2026, and that journey **fixed nothing**: its close-out reads
``clean-review-no-fixes``. A rate that counted it would mint a false "one out
of one" on the first day the number was ever shown. So the measure is
pre-registered here, in code, *before* the first number: every repair row
filed before 2026-09-05 is outside the window, and so is every mode-c build
from the drive era (there are 71 of them, and none of them is a
``work_queue`` row at all — the queue is younger than they are).

The cutoff is a module constant rather than a caller's argument default so
that moving it is a visible edit to this file, in a commit, rather than
something a call site can quietly do.

What counts
-----------

**Denominator** — every ``work_queue`` row with ``kind='fix'`` filed on or
after the cutoff, except the ones somebody withdrew. A withdrawn row is a
repair nobody wanted, not a repair the factory failed.

**Numerator** — those of them whose admitted build got all the way through:
the build has a merge-ready checkpoint stage row (the gates were proven
green and Rich was shown the card), and after it a merge report saying
``merged-and-running`` (it merged, it deployed, and the live checks stayed
green). Rich's two card taps do not count as intervention — they are the
mission's control points, not a rescue.

A repair row and its build are joined by the correlation id: the queue row's
own id is handed to :func:`forge.pipeline.fix_admission.admit_fix_build`
unchanged, so ``builds.correlation_id = work_queue.correlation_id`` is the
spine both ends share.

A note on where the merge report is read from
---------------------------------------------

The merge report is a ``stage-complete`` message the merge executor
publishes (``merge_executor._publish_report``), and its outcome word lives
in the report's ``result`` field. A message cannot be queried and neither
can the receipt file beside it, so the executor also writes the report to
``stage_log`` as a ``merge-deploy`` row identified ``merge_deploy_executor``
— this function reads the outcome word out of that row's ``result`` field
by name. Reading the field, not searching the whole details blob, is
deliberate: the report also carries a free-text ``detail`` line, and a
failure whose prose happened to mention ``merged-and-running`` must never
be counted as a success. A dry run writes no row, so it can never count.

References
----------
- ``docs/conductor-rewire-spec-2026-09-05.md`` rule 6.
"""

from __future__ import annotations

import sqlite3
from typing import Any

#: The day the measure starts. Every repair row filed before this, and every
#: mode-c build from the drive era, is outside the window — see the module
#: docstring for why. ISO dates compare correctly as plain text, so this
#: reads directly against ``queued_at``.
M5_SINCE: str = "2026-09-05"

#: The stage row that says the gates were proven green and the merge-ready
#: card was raised (``cli/_serve_gate_activation.py``).
MERGE_READY_TARGET_IDENTIFIER: str = "merge_ready_checkpoint"

#: The stage label the merge executor reports its outcome under
#: (``pipeline/merge_executor.py``).
MERGE_REPORT_STAGE_LABEL: str = "merge-deploy"

#: The report row's own identity under that label — the merge and deploy
#: step rows share the label but are not the report.
MERGE_REPORT_TARGET_IDENTIFIER: str = "merge_deploy_executor"

#: The one outcome word that means the repair closed itself: merged,
#: deployed, and still green afterwards.
MERGED_AND_RUNNING: str = "merged-and-running"


#: Every repair row inside the window that somebody has not withdrawn.
DENOMINATOR_SQL: str = """
SELECT COUNT(*)
  FROM work_queue
 WHERE kind = 'fix'
   AND status <> 'WITHDRAWN'
   AND queued_at >= ?
"""

#: Of those, the ones whose build reached a merge-ready checkpoint and then
#: reported ``merged-and-running``. The outcome word is read out of the
#: report's own ``result`` field by name — never searched for across the
#: whole details blob, which also holds a free-text ``detail`` line that can
#: quote the word while saying the opposite. ``report`` must come after
#: ``gate``: later in wall-clock time, or — when a seeded or same-second
#: pair shares a timestamp — later in the stage log's own insertion order.
NUMERATOR_SQL: str = """
SELECT COUNT(*)
  FROM work_queue AS q
 WHERE q.kind = 'fix'
   AND q.status <> 'WITHDRAWN'
   AND q.queued_at >= ?
   AND EXISTS (
         SELECT 1
           FROM builds AS b
           JOIN stage_log AS gate
             ON gate.build_id = b.build_id
            AND gate.target_identifier = ?
           JOIN stage_log AS report
             ON report.build_id = b.build_id
            AND report.stage_label = ?
            AND report.target_identifier = ?
            AND json_extract(report.details_json, '$.result') = ?
            AND (
                  report.started_at > gate.started_at
                  OR (report.started_at = gate.started_at
                      AND report.id > gate.id)
                )
          WHERE b.correlation_id = q.correlation_id
       )
"""


def _connection(pool: Any) -> sqlite3.Connection:
    """The database behind whatever the caller handed us.

    Accepts a bare :class:`sqlite3.Connection` (what ``forge status`` opens,
    read-only) or the lifecycle persistence facade (which exposes one as
    ``connection``), so the same function serves the CLI and the daemon.
    """
    if isinstance(pool, sqlite3.Connection):
        return pool
    candidate = getattr(pool, "connection", None)
    if isinstance(candidate, sqlite3.Connection):
        return candidate
    raise TypeError(
        "self_closed_defect_rate needs a sqlite connection, or something "
        f"holding one on .connection; got {type(pool).__name__}"
    )


def self_closed_defect_rate(pool: Any, since: str = M5_SINCE) -> tuple[int, int]:
    """How many repairs the factory closed by itself, out of how many it took.

    Args:
        pool: A :class:`sqlite3.Connection`, or the lifecycle persistence
            facade holding one.
        since: The cutoff date, ISO ``YYYY-MM-DD``. Defaults to
            :data:`M5_SINCE`; passing anything else is for tests.

    Returns:
        ``(closed by the factory, repairs taken on)`` — the two halves of
        the line ``forge status --m5`` prints, in that order. ``(0, 0)``
        means no repair row has been filed since the cutoff, which is not
        a rate of zero; it is no rate yet.
    """
    cx = _connection(pool)
    denominator = int(cx.execute(DENOMINATOR_SQL, (since,)).fetchone()[0])
    numerator = int(
        cx.execute(
            NUMERATOR_SQL,
            (
                since,
                MERGE_READY_TARGET_IDENTIFIER,
                MERGE_REPORT_STAGE_LABEL,
                MERGE_REPORT_TARGET_IDENTIFIER,
                MERGED_AND_RUNNING,
            ),
        ).fetchone()[0]
    )
    return numerator, denominator


__all__ = [
    "DENOMINATOR_SQL",
    "M5_SINCE",
    "MERGED_AND_RUNNING",
    "MERGE_READY_TARGET_IDENTIFIER",
    "MERGE_REPORT_STAGE_LABEL",
    "MERGE_REPORT_TARGET_IDENTIFIER",
    "NUMERATOR_SQL",
    "self_closed_defect_rate",
]

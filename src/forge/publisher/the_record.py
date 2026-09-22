"""The publisher reads the publication record itself, and read-only.

One-true-copy design pass, item 1, second revision section D:

    *Before it sends, it checks for itself, reading the ledger read-only,
    that a publication record exists for that build whose steps 3 and 4
    passed on exactly that commit.*

…and the third revision's section E:

    *the publisher is given the turn number with each request and reads the
    current one from the ledger itself before sending; an old number is
    refused.*

WHAT THE PUBLISHER TRUSTS. The ledger, and only the ledger. It does not trust
the request it was sent — the request says which PROJECT, which build, which
turn and which joined commit, and every one of those four is checked against
the record, the project included: a build's record is bound to the project it
was built for, and a request naming another project is refused. It
does not trust the folder of exported records, which a sandbox can write, and
it never reads it. It does not trust a step's existence: a ``done`` line says
a step finished, not that it was green, and a red run writes one too. So a
step counts only when the line says it ran on EXACTLY this joined commit AND
its verdict was a pass.

READ-ONLY IN THE STRONG SENSE. The connection is opened through the URI form
with ``mode=ro``, so SQLite itself refuses a write on it: it is not a habit
kept by this module, it is a property of the handle. Nothing here writes, and
nothing here can.

Nothing in this module names a language, a hosting provider or a product. It
reads text out of a table.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "STEP_CANDIDATE_CHECK",
    "STEP_MERGE_CHECKS",
    "TheLedgerIsUnreadable",
    "TheRecord",
    "TheRecordReader",
    "a_step_that_passed_on",
]

#: The two kinds of check the design requires, by the names the coordinator
#: writes them under (:mod:`forge.pipeline.publication_record`). They are
#: written out here rather than imported, so that the publisher is a service
#: that reads a table rather than a thing that imports the coordinator; a
#: test pins the two spellings together.
STEP_MERGE_CHECKS: str = "merge-checks"
STEP_CANDIDATE_CHECK: str = "candidate-check"

#: The two line kinds.
LINE_DONE: str = "done"


class TheLedgerIsUnreadable(RuntimeError):
    """The ledger could not be opened or read, said in one sentence."""


@dataclass(frozen=True)
class TheRecord:
    """One build's publication record as the publisher reads it."""

    build_id: str
    recorded: bool = False
    turn: int = 0
    attempt: int = 0
    #: WHICH PROJECT THIS BUILD BELONGS TO, as the coordinator wrote it down.
    #: A build's name is not a project's name. Without this the publisher
    #: would take a request naming one project, read the record of a build
    #: belonging to another, and send that other project's commit to the
    #: first project's remote. It is read so that the refusal can be made.
    project: str | None = None
    target_branch: str | None = None
    g_commit: str | None = None
    build_tip: str | None = None
    j_commit: str | None = None
    result: str | None = None
    lines: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def a_step_that_passed_on(
    record: TheRecord, step: str, joined_commit: str
) -> dict[str, Any] | None:
    """The ``done`` line saying this step PASSED on exactly this commit.

    Both halves, every time:

    * **the inputs** — the line has to say it ran on this joined commit. A
      line that names another one is somebody else's answer to somebody
      else's question, and a line that names none at all is not evidence
      about this commit;
    * **the verdict** — ``verify_ok`` has to be exactly ``True``. Anything
      else, including its absence, is not a pass.

    ``None`` when there is no such line, which is every "the step never ran"
    and every "the step ran and went red".
    """
    wanted = str(joined_commit)
    found: dict[str, Any] | None = None
    for line in record.lines:
        if str(line.get("kind")) != LINE_DONE or str(line.get("step")) != str(step):
            continue
        detail = line.get("detail")
        if not isinstance(detail, dict):
            continue
        ran_on = str(detail.get("ran_on") or detail.get("j_commit") or "")
        if ran_on != wanted:
            continue
        if detail.get("verify_ok") is not True:
            continue
        found = dict(line)
    return found


class TheRecordReader:
    """Opens the ledger read-only and reads one build's record."""

    def __init__(self, ledger: str | Path) -> None:
        self._path = Path(ledger).expanduser()

    @property
    def path(self) -> Path:
        return self._path

    def _connect(self) -> sqlite3.Connection:
        if not self._path.exists():
            raise TheLedgerIsUnreadable(
                f"the publisher cannot read the record: there is no ledger at "
                f"{self._path}"
            )
        uri = f"file:{self._path}?mode=ro"
        try:
            return sqlite3.connect(uri, uri=True, timeout=10.0)
        except sqlite3.Error as exc:
            raise TheLedgerIsUnreadable(
                f"the publisher cannot read the record: the ledger at "
                f"{self._path} could not be opened read-only ({exc})"
            ) from exc

    def read(self, build_id: str) -> TheRecord:
        """This build's record, or ``recorded=False`` because there is none."""
        connection = self._connect()
        try:
            try:
                row = connection.execute(
                    """
                    SELECT build_id, target_branch, g_commit, build_tip,
                           j_commit, attempt, result, turn, lines_json,
                           repo
                      FROM publication_records
                     WHERE build_id = ?
                    """,
                    (str(build_id),),
                ).fetchone()
            except sqlite3.Error as exc:
                raise TheLedgerIsUnreadable(
                    f"the publisher cannot read the record for {build_id}: "
                    f"the ledger at {self._path} would not answer ({exc})"
                ) from exc
        finally:
            connection.close()
        if row is None:
            return TheRecord(build_id=str(build_id), recorded=False)
        lines: list[dict[str, Any]] = []
        if row[8]:
            try:
                decoded = json.loads(row[8])
            except ValueError:
                decoded = []
            if isinstance(decoded, list):
                lines = [entry for entry in decoded if isinstance(entry, dict)]
        return TheRecord(
            build_id=str(row[0]),
            recorded=True,
            project=(str(row[9]) if row[9] else None),
            target_branch=(str(row[1]) if row[1] else None),
            g_commit=(str(row[2]) if row[2] else None),
            build_tip=(str(row[3]) if row[3] else None),
            j_commit=(str(row[4]) if row[4] else None),
            attempt=int(row[5] or 0),
            result=(str(row[6]) if row[6] else None),
            turn=int(row[7] or 0),
            lines=tuple(lines),
        )

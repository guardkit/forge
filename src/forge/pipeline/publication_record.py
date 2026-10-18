"""The publication record — what the merge word is about to do, and what it did.

One-true-copy design pass, 22 September 2026: item 1's second revision,
section A ("Say what is about to be done before doing it, and look at the
world when picking up") and third revision, section E ("Taking over a build
cancels the previous worker's authority").

WHAT THIS IS FOR, in one paragraph. The merge word does several things in a
row, each of which can succeed a moment before the coordinator stops. Writing
each step down only *after* it finished means a step that finished and was
never written is done again; writing it down only *before* means a step that
never started is skipped. So each step is written down twice — "about to",
with the attempt number and the exact inputs, before anything is done, and
"done", with the result, after — and picking up is a matter of LOOKING at the
world when the last line is an "about to", never of assuming either way.

THE TURN NUMBER, and why a lease alone is not enough. A lease with a holder
and an expiry stops a worker that has died. It does not stop one that merely
stalled: A stops responding, its lease runs out, B takes over, and then A
wakes up and carries on, and now two workers are acting on one build. So the
record carries a turn number that goes up by one on every take or takeover, in
the same transaction as the lease, and **every write is made only where the
stored turn number still equals the writer's own, in the same statement**. A
write that changes no row means "you have been replaced": the worker stops at
once and tidies nothing up, because tidying up is itself a change and the new
holder is the one entitled to make it.

NOT RECORDED. A build with no row reads back as
:attr:`PublicationRecord.recorded` ``False``. Every build pressed before this
existed reads that way, and so does every build that has not reached the merge
word. It is a fact about the record, never a guess at what happened.

WHAT IS DELIBERATELY NOT HERE. The publisher does not exist yet, so nothing
here sends anything anywhere and no record can reach "published" or "running".
The deployment target's own lock row (the design's section F) and the fixed
identity of what was checked (section C) belong to the later stages; the
vocabulary below names all three results so the later stages do not have to
invent words, and only ``publication pending`` is reachable in this version.

Nothing in this module knows what a project contains, what language it is
written in, who hosts its remote or how it is deployed. It stores text.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "LINE_ABOUT_TO",
    "LINE_DONE",
    "PUBLICATION_RESULTS",
    "RESULT_MERGED_AND_RUNNING",
    "RESULT_PUBLICATION_PENDING",
    "RESULT_PUBLISHED_DEPLOYMENT_PENDING",
    "RESULT_PUBLISHED_TO_MISSION_BRANCH",
    "STEP_CANDIDATE_CHECK",
    "STEP_DEPLOY",
    "STEP_JOIN",
    "STEP_MERGE_CHECKS",
    "STEP_SEND",
    "PublicationLine",
    "PublicationRecord",
    "PublicationRecordStore",
    "StoredOutcome",
    "a_report_names_the_press",
    "REPORT_PRESS_TURN_KEY",
    "ensure_outcome_table",
    "Replaced",
    "default_lease_seconds",
]


# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------

#: The joined commit is made in a working folder of its own.
STEP_JOIN: str = "join"

#: The build system's own checks after the merge, run on the joined commit.
STEP_MERGE_CHECKS: str = "merge-checks"

#: The factory's live check of the candidate, run on the joined commit's tree.
STEP_CANDIDATE_CHECK: str = "candidate-check"

#: Sending the joined commit to the remote. NOT reachable in this version —
#: the publisher does not exist. Named so the later stage does not rename it.
STEP_SEND: str = "send"

#: Deploying what was checked. NOT reachable in this version.
STEP_DEPLOY: str = "deploy"

#: A line written BEFORE acting: the attempt number and the exact inputs.
LINE_ABOUT_TO: str = "about to"

#: A line written after acting: what it produced.
LINE_DONE: str = "done"

#: The remote does not have the joined commit. Nothing is deployed, and the
#: reason is said.
RESULT_PUBLICATION_PENDING: str = "publication pending"

#: The remote has the joined commit; the deploy has not finished. It is never
#: called a merge that is running.
RESULT_PUBLISHED_DEPLOYMENT_PENDING: str = "published, deployment pending"

#: The remote has the joined commit, and what was checked is deployed.
#:
#: REACHABLE SINCE 23 SEPTEMBER 2026 (the executor stage), on ONE path and no
#: other: the press deployed the very thing it checked, and the project's own
#: deploy step reported back, as text, the identity it was handed. These three
#: comments used to say "NOT reachable in this version", which was true of the
#: two stages before this one; what is pinned by test now is that path, not
#: the absence of it.
#:
#: IT NAMES NO HOSTING PROVIDER. It used to read "merged into GitHub and
#: running", which was the design's own wording and wrong for central
#: orchestration: the factory knows only "the remote named origin", and a
#: project whose remote is somewhere else would have been told, in the
#: factory's own vocabulary, that it was merged into a service it has never
#: heard of. Renamed 22 September 2026 with its sibling in the press
#: (``RESULT_WORD_MERGED_AND_RUNNING``).
RESULT_MERGED_AND_RUNNING: str = "merged into the remote and running"

#: A mission feature's joined commit is on the mission branch, and nothing is
#: deployed or owed: a feature inside a mission is merged into its mission's
#: branch with ``deploy=False``, and only the mission's own merge into main
#: deploys (missions design, 17 October 2026, section 6). It is NOT one of the
#: three below, because it is not a step on the way to "running"; every reader
#: of these results must take it as finished, never as a deploy still owed.
RESULT_PUBLISHED_TO_MISSION_BRANCH: str = "published to the mission branch"

#: The three, in the order a build passes through them.
PUBLICATION_RESULTS: tuple[str, ...] = (
    RESULT_PUBLICATION_PENDING,
    RESULT_PUBLISHED_DEPLOYMENT_PENDING,
    RESULT_MERGED_AND_RUNNING,
)


def default_lease_seconds() -> int:
    """How long a worker holds a build's record before it may be taken over.

    Long enough that a healthy press is never taken over mid-step, short
    enough that a dead one is not waited on all day. A step that may run
    longer than this pushes the lease out first and puts it back afterwards:
    since 13 October 2026 the merge press renews it to cover the merge
    command's whole wall before sending that command, and back to this length
    once it has answered. Nothing else renews it.
    """
    return 1800


#: One row per build, for the LATEST press on its record (press lease
#: recovery, 18 October 2026), outside the migration ladder: made where it is
#: needed and at the coordinator's boot, so the schema version stays as it is.
#:
#: A press makes the row afresh in the same transaction that takes its lease
#: (``turn`` is that press's turn); its result write marks ``result_written``
#: in the result's own transaction, with what might still be standing then;
#: the press writes its ending into ``outcome_json``. Then ONE idempotent
#: finishing routine (``merge_executor.finish_the_press``), run by the press
#: and by the recovery alike, does every bookkeeping step for that turn, each
#: one first asking whether it is already done — the report row (a report
#: naming this ``press_turn``), the bus message (``bus_sent_at``), the build
#: row's ending, the repair row (``repair_at``), a mission landing's warning
#: (``warning_json``/``warning_delivered_at``), the release — and only then
#: sets ``finished_at``. A stop between any two steps leaves the rest to the
#: next pass.
_OUTCOME_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS publication_outcomes ("
    "build_id TEXT PRIMARY KEY, "
    "turn INTEGER NOT NULL, "
    "attempt_id TEXT, "
    "outcome_json TEXT NOT NULL DEFAULT '{}', "
    "recorded_at TEXT NOT NULL, "
    "result_written INTEGER NOT NULL DEFAULT 0, "
    "standing_json TEXT, "
    "bus_sent_at TEXT, "
    "repair_at TEXT, "
    "warning_json TEXT, "
    "warning_delivered_at TEXT, "
    "finished_at TEXT)"
)

#: Columns added after the table was first made, each added where missing,
#: so an older table shape can never make a result write or a mark refuse.
_OUTCOME_COLUMNS_ADDED: tuple[tuple[str, str], ...] = (
    ("result_written", "INTEGER NOT NULL DEFAULT 0"),
    ("standing_json", "TEXT"),
    ("bus_sent_at", "TEXT"),
    ("repair_at", "TEXT"),
    ("warning_json", "TEXT"),
    ("warning_delivered_at", "TEXT"),
    ("finished_at", "TEXT"),
)

#: The marks :meth:`PublicationRecordStore.mark_outcome` may write.
_OUTCOME_MARKS: frozenset[str] = frozenset(
    {"bus_sent_at", "repair_at", "warning_json", "warning_delivered_at", "finished_at"}
)

#: The report detail that names the press whose ending a merge report is.
REPORT_PRESS_TURN_KEY: str = "press_turn"


def ensure_outcome_table(cx: sqlite3.Connection) -> None:
    """Create ``publication_outcomes`` if it is missing, and add any column an
    older shape of it lacks. Idempotent."""
    cx.execute(_OUTCOME_TABLE_SQL)
    have = {str(row[1]) for row in cx.execute("PRAGMA table_info(publication_outcomes)")}
    for name, declared in _OUTCOME_COLUMNS_ADDED:
        if name not in have:
            cx.execute(f"ALTER TABLE publication_outcomes ADD COLUMN {name} {declared}")


def a_report_names_the_press(
    cx: sqlite3.Connection, *, build_id: str, turn: int
) -> bool:
    """Is a merge report on this build's record that names the press of
    ``turn`` (its ``press_turn`` detail)? The finishing routine's own check
    for its first step. ``False`` on a ledger with no stage log."""
    try:
        return (
            cx.execute(
                "SELECT 1 FROM stage_log WHERE build_id = ? "
                "AND target_identifier = 'merge_deploy_executor' "
                "AND json_valid(details_json) "
                f"AND json_extract(details_json, '$.{REPORT_PRESS_TURN_KEY}') = ? LIMIT 1",
                (str(build_id), int(turn)),
            ).fetchone()
            is not None
        )
    except sqlite3.OperationalError:
        return False


def _no_outcome_table(exc: sqlite3.Error) -> bool:
    return "no such table" in str(exc).lower()


@dataclass(frozen=True)
class StoredOutcome:
    """The latest press's row: its turn, attempt, written-down ending (``{}``
    until written), whether its result was written, what might still have
    been standing then, and each finishing mark (``None``: not yet)."""

    turn: int
    attempt_id: str | None
    outcome: dict[str, Any]
    result_written: bool = False
    standing: dict[str, Any] = field(default_factory=dict)
    bus_sent_at: str | None = None
    repair_at: str | None = None
    warning: dict[str, Any] = field(default_factory=dict)
    warning_delivered_at: str | None = None
    finished_at: str | None = None


class Replaced(RuntimeError):
    """This worker's turn is no longer the record's — it has been replaced.

    Raised by nothing here: every operation returns ``False`` instead, so the
    caller decides. The class exists for a caller that would rather raise than
    branch, and so that "replaced" has one name in the estate.
    """


# ---------------------------------------------------------------------------
# What a record looks like when it is read
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PublicationLine:
    """One line of the record: a step, said before it happened or after.

    ``kind`` is ``"about to"`` or ``"done"``; ``step`` is one of the five
    names above; ``attempt`` is which attempt at that step this is; ``detail``
    is the exact inputs (on an "about to") or the result (on a "done"), as
    whatever plain JSON the press wrote.
    """

    kind: str
    step: str
    attempt: int
    at: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "step": self.step,
            "attempt": self.attempt,
            "at": self.at,
            "detail": dict(self.detail),
        }

    @classmethod
    def from_wire(cls, decoded: Any) -> "PublicationLine | None":
        if not isinstance(decoded, dict):
            return None
        kind = str(decoded.get("kind") or "")
        step = str(decoded.get("step") or "")
        if kind not in (LINE_ABOUT_TO, LINE_DONE) or not step:
            return None
        detail = decoded.get("detail")
        try:
            attempt = int(decoded.get("attempt") or 0)
        except (TypeError, ValueError):
            attempt = 0
        return cls(
            kind=kind,
            step=step,
            attempt=attempt,
            at=str(decoded.get("at") or ""),
            detail=dict(detail) if isinstance(detail, dict) else {},
        )


@dataclass(frozen=True)
class PublicationRecord:
    """One build's publication record, or the honest absence of one."""

    build_id: str
    recorded: bool = False
    feature_id: str | None = None
    repo: str | None = None
    decided_by: str | None = None
    decided_at: str | None = None
    target_branch: str | None = None
    g_commit: str | None = None
    build_tip: str | None = None
    j_commit: str | None = None
    attempt: int = 0
    result: str | None = None
    checked: dict[str, Any] = field(default_factory=dict)
    lease_holder: str | None = None
    lease_expires_at: str | None = None
    turn: int = 0
    lines: tuple[PublicationLine, ...] = ()

    @property
    def sentence(self) -> str:
        """What a person is shown, either way."""
        if not self.recorded:
            return "not recorded"
        where = self.result or "in progress"
        return f"{where} (turn {self.turn}, attempt {self.attempt})"

    def last_line(self) -> PublicationLine | None:
        return self.lines[-1] if self.lines else None

    def unfinished(self) -> PublicationLine | None:
        """The last line, when it is an "about to" nothing answered.

        This is the whole of "picking up": a record whose last line is an
        "about to" is a step that may or may not have happened, and the
        factory settles it by looking at the world rather than by assuming.
        ``None`` when the record is complete as far as it goes.
        """
        last = self.last_line()
        if last is None or last.kind != LINE_ABOUT_TO:
            return None
        return last

    def done_steps(self) -> tuple[str, ...]:
        """Every step this record says finished, in the order it finished."""
        return tuple(line.step for line in self.lines if line.kind == LINE_DONE)

    def is_done(self, step: str) -> bool:
        return step in self.done_steps()

    def lease_is_live(self, now: datetime) -> bool:
        """Is somebody holding this record right now?"""
        if not self.lease_holder or not self.lease_expires_at:
            return False
        expiry = _parse_time(self.lease_expires_at)
        if expiry is None:
            return False
        return expiry > now


def _parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LeaseGrant:
    """The lease was granted: this worker's turn, and when it runs out."""

    build_id: str
    holder: str
    turn: int
    expires_at: str
    took_over_from: str | None = None


class PublicationRecordStore:
    """Read and write the publication record, one build at a time.

    Every write takes the writer's turn number and is refused — as a plain
    ``False``, never an exception — when the record has moved on. That is the
    whole safety property, and it is enforced in SQL, in the same statement
    that does the writing, so no read-then-write window exists for a replaced
    worker to slip through.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._cx = connection
        #: Why the last :meth:`take_lease` answered ``None``: ``"held"`` (a
        #: live lease), ``"an-ending-is-owed"`` (a stopped press's ending is
        #: still to be reported), or ``None``.
        self.last_refusal: str | None = None

    @contextmanager
    def _transaction(self):
        """One write transaction, whatever the connection's own mode is.

        The estate's writer connection is in autocommit mode and manages its
        transactions by hand; a connection made some other way may already be
        in one. Both are handled here so that the two callers below do not
        each have to know.
        """
        started = False
        if not self._cx.in_transaction:
            self._cx.execute("BEGIN IMMEDIATE")
            started = True
        try:
            yield
        except BaseException:
            if started:
                self._cx.rollback()
            raise
        else:
            if started:
                self._cx.commit()

    # -- reading -----------------------------------------------------------

    def read(self, build_id: str) -> PublicationRecord:
        """This build's record, or ``recorded=False`` when there is none."""
        try:
            row = self._cx.execute(
                """
                SELECT build_id, feature_id, repo, decided_by, decided_at,
                       target_branch, g_commit, build_tip, j_commit, attempt,
                       result, checked_json, lease_holder, lease_expires_at,
                       turn, lines_json
                  FROM publication_records
                 WHERE build_id = ?
                """,
                (str(build_id),),
            ).fetchone()
        except sqlite3.Error as exc:
            logger.warning(
                "publication record: %s could not be read (%s) — reading it "
                "as not recorded",
                build_id,
                exc,
            )
            return PublicationRecord(build_id=str(build_id), recorded=False)
        if row is None:
            return PublicationRecord(build_id=str(build_id), recorded=False)
        values = list(row)
        checked: dict[str, Any] = {}
        if values[11]:
            try:
                decoded = json.loads(values[11])
                if isinstance(decoded, dict):
                    checked = decoded
            except ValueError:
                checked = {}
        lines: list[PublicationLine] = []
        if values[15]:
            try:
                decoded_lines = json.loads(values[15])
            except ValueError:
                decoded_lines = []
            if isinstance(decoded_lines, list):
                for entry in decoded_lines:
                    line = PublicationLine.from_wire(entry)
                    if line is not None:
                        lines.append(line)
        return PublicationRecord(
            build_id=str(values[0]),
            recorded=True,
            feature_id=_as_text(values[1]),
            repo=_as_text(values[2]),
            decided_by=_as_text(values[3]),
            decided_at=_as_text(values[4]),
            target_branch=_as_text(values[5]),
            g_commit=_as_text(values[6]),
            build_tip=_as_text(values[7]),
            j_commit=_as_text(values[8]),
            attempt=int(values[9] or 0),
            result=_as_text(values[10]),
            checked=checked,
            lease_holder=_as_text(values[12]),
            lease_expires_at=_as_text(values[13]),
            turn=int(values[14] or 0),
            lines=tuple(lines),
        )

    def which_commit_was_checked_as(
        self, identity: str, *, repo: str | None = None, limit: int = 200
    ) -> str | None:
        """The joined commit a running identity belongs to, or ``None``.

        Added 24 September 2026, after the second review of the executor stage.
        A target can only be reasoned about once what it says it is running has
        been PLACED: the only-forwards rule compares commits, and the project
        answers in identities. Every publication record already holds both — the
        joined commit it was for, and the identity captured when that commit was
        checked — so this is that map, read back out.

        ``None`` means no record here was checked as that identity, and the
        caller then treats the target as one it cannot account for and deploys
        nothing. That is the point: guessing would put an older result over a
        newer one, which is the failure this exists to stop.
        """
        wanted = str(identity or "").strip()
        if not wanted:
            return None
        sql = (
            "SELECT j_commit, checked_json FROM publication_records "
            "WHERE j_commit IS NOT NULL"
        )
        params: list[Any] = []
        if repo:
            sql += " AND repo = ?"
            params.append(str(repo))
        sql += " ORDER BY rowid DESC LIMIT ?"
        params.append(int(limit))
        try:
            rows = self._cx.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            logger.warning(
                "publication record: the records could not be searched for the "
                "identity %s (%s)",
                wanted,
                exc,
            )
            return None
        for row in rows:
            commit = _as_text(row[0])
            if not commit or not row[1]:
                continue
            try:
                decoded = json.loads(row[1])
            except ValueError:
                continue
            if not isinstance(decoded, dict):
                continue
            block = decoded.get("identity")
            candidates: list[Any] = [decoded.get("artifact")]
            if isinstance(block, dict):
                candidates += [
                    block.get("handed_to_the_check"),
                    block.get("artifact"),
                    block.get("identity"),
                ]
            for value in candidates:
                if value and str(value).strip() == wanted:
                    return commit
        return None

    # -- the lease and the turn number -------------------------------------

    def take_lease(
        self,
        *,
        build_id: str,
        holder: str,
        now: datetime,
        seconds: int | None = None,
        feature_id: str | None = None,
        repo: str | None = None,
        decided_by: str | None = None,
        target_branch: str | None = None,
        starts_a_press: bool = False,
        refuse_if_an_ending_is_owed: bool = False,
        expected_turn: int | None = None,
    ) -> LeaseGrant | None:
        """Take this build's record, or answer ``None`` because someone holds it.

        ``starts_a_press`` (press lease recovery, 18 October 2026): a merge
        press beginning, so in the same transaction this build's
        ``publication_outcomes`` row is made afresh for the new turn — any
        earlier press's ending, result mark and report mark go with it. A
        recovery taking the record over passes False and changes none of it.

        ``refuse_if_an_ending_is_owed``: a new press, refused
        (``last_refusal`` = ``"an-ending-is-owed"``) while the latest press's
        result is written and its finishing not yet done, so the recovery
        finishes it first.

        ``expected_turn`` (compare and swap): the take succeeds only while the
        record is still on that turn (``last_refusal`` = ``"moved-on"``), so a
        recovery that read the record and then took it can never act on a
        record another worker moved on in between.

        One transaction, and the turn number goes up by one inside it, on a
        first take and on a takeover alike. A takeover is only possible once
        the previous holder's lease has run out: a live lease is left alone.

        The four descriptive fields are written only when the row is created,
        so a takeover can never rewrite who gave the merge word or which
        branch the work is aimed at.
        """
        ttl = int(seconds if seconds is not None else default_lease_seconds())
        expires = (now + timedelta(seconds=ttl)).isoformat()
        stamp = now.isoformat()
        self.last_refusal = None
        try:
            with self._transaction():
                row = self._cx.execute(
                    """
                    SELECT turn, lease_holder, lease_expires_at
                      FROM publication_records
                     WHERE build_id = ?
                    """,
                    (str(build_id),),
                ).fetchone()
                if row is None and expected_turn is not None:
                    self.last_refusal = "moved-on"
                    return None
                if row is None:
                    self._cx.execute(
                        """
                        INSERT INTO publication_records (
                            build_id, feature_id, repo, decided_by, decided_at,
                            target_branch, attempt, lease_holder,
                            lease_expires_at, turn, lines_json,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, 1, '[]', ?, ?)
                        """,
                        (
                            str(build_id),
                            _as_text(feature_id),
                            _as_text(repo),
                            _as_text(decided_by),
                            stamp,
                            _as_text(target_branch),
                            str(holder),
                            expires,
                            stamp,
                            stamp,
                        ),
                    )
                    if starts_a_press:
                        self._a_press_begins(build_id, 1, stamp)
                    return LeaseGrant(
                        build_id=str(build_id),
                        holder=str(holder),
                        turn=1,
                        expires_at=expires,
                    )
                current_turn = int(row[0] or 0)
                if expected_turn is not None and current_turn != int(expected_turn):
                    self.last_refusal = "moved-on"
                    return None
                current_holder = _as_text(row[1])
                expiry = _parse_time(row[2])
                held_by_another = (
                    current_holder is not None
                    and current_holder != str(holder)
                    and expiry is not None
                    and expiry > now
                )
                if held_by_another:
                    self.last_refusal = "held"
                    return None
                # A NEW PRESS NEVER WIPES AN ENDING STILL OWED (coach
                # follow-up, 18 October 2026): a press that stopped after its
                # result, whose hold has run out but whose ending the recovery
                # has not reported yet, keeps its row. The new press is turned
                # away and the recovery reports the stopped one first.
                if refuse_if_an_ending_is_owed and self._an_ending_is_owed(build_id):
                    self.last_refusal = "an-ending-is-owed"
                    return None
                changed = self._cx.execute(
                    """
                    UPDATE publication_records
                       SET turn = turn + 1,
                           lease_holder = ?,
                           lease_expires_at = ?,
                           updated_at = ?
                     WHERE build_id = ? AND turn = ?
                    """,
                    (str(holder), expires, stamp, str(build_id), current_turn),
                ).rowcount
                if changed != 1:
                    return None
                if starts_a_press:
                    self._a_press_begins(build_id, current_turn + 1, stamp)
                return LeaseGrant(
                    build_id=str(build_id),
                    holder=str(holder),
                    turn=current_turn + 1,
                    expires_at=expires,
                    took_over_from=(
                        current_holder if current_holder != str(holder) else None
                    ),
                )
        except sqlite3.Error as exc:
            logger.warning(
                "publication record: the lease on %s could not be taken (%s)",
                build_id,
                exc,
            )
            return None

    def _an_ending_is_owed(self, build_id: str) -> bool:
        """Is this build's latest press's result written and its finishing
        not yet done (no ``finished_at``)?"""
        try:
            row = self._cx.execute(
                "SELECT turn, result_written, finished_at FROM publication_outcomes "
                "WHERE build_id = ?",
                (str(build_id),),
            ).fetchone()
        except sqlite3.OperationalError:
            return False
        if row is None or not row[1] or row[2] is not None:
            return False
        return True

    def _a_press_begins(self, build_id: str, turn: int, stamp: str) -> None:
        """Inside :meth:`take_lease`'s transaction: this press's row, afresh."""
        ensure_outcome_table(self._cx)
        self._cx.execute(
            "INSERT OR REPLACE INTO publication_outcomes "
            "(build_id, turn, attempt_id, outcome_json, recorded_at, result_written) "
            "VALUES (?, ?, NULL, '{}', ?, 0)",
            (str(build_id), int(turn), stamp),
        )

    def _mark_the_result(self, build_id: str, turn: int) -> None:
        """Inside a result write's transaction: the press of ``turn`` wrote
        it, with what it says might still be standing (``result_context``,
        set by the press on its own store; nothing otherwise)."""
        context = getattr(self, "result_context", None)
        standing: dict[str, Any] = {}
        if callable(context):
            try:
                standing = dict(context() or {})
            except Exception:  # noqa: BLE001 — never costs a result
                standing = {"unknown": True}
        try:
            self._cx.execute(
                "UPDATE publication_outcomes SET result_written = 1, standing_json = ? "
                "WHERE build_id = ? AND turn = ?",
                (json.dumps(standing, sort_keys=True, default=str), str(build_id), int(turn)),
            )
        except sqlite3.OperationalError as exc:
            # NEVER COSTS A RESULT: a mark that cannot be written is logged,
            # and the result stands (the recovery then leaves it alone).
            if not _no_outcome_table(exc):
                logger.warning(
                    "publication record: %s's result could not be marked as its "
                    "press's (%s)",
                    build_id,
                    exc,
                )

    def renew_lease(
        self, *, build_id: str, turn: int, now: datetime, seconds: int | None = None
    ) -> bool:
        """Push this worker's lease out. Renewing does NOT change the turn."""
        ttl = int(seconds if seconds is not None else default_lease_seconds())
        return self._write(
            build_id=build_id,
            turn=turn,
            now=now,
            assignments={"lease_expires_at": (now + timedelta(seconds=ttl)).isoformat()},
        )

    def release_lease(
        self, *, build_id: str, turn: int, holder: str, now: datetime
    ) -> bool:
        """Put this worker's lease down, so the next merge word need not wait.

        Added 3 October 2026: a lease was only ever taken, so every press
        held its build for the whole lease however it ended, and a retry a
        few minutes later was refused. Only the holder can put it down, and
        only on its own turn — the same condition every other write carries,
        plus the holder's name — so a worker that has been replaced changes
        nothing. The turn number is left as it is: the next worker's take
        raises it. ``False`` (never an exception) when nothing was changed.
        """
        return self._write(
            build_id=build_id,
            turn=turn,
            now=now,
            assignments={"lease_holder": None, "lease_expires_at": None},
            holder=holder,
        )

    def record_outcome(
        self,
        *,
        build_id: str,
        turn: int,
        holder: str,
        now: datetime,
        outcome: dict[str, Any],
        attempt_id: str | None,
    ) -> bool:
        """Write down the press's ending, before its clean-up and its report.

        Press lease recovery (18 October 2026): one record result covers
        several different reports, so the ending itself is kept, on the
        press's own row, and a recovery reports from it through the press's
        own report path. Only while the record is still on this press's turn
        and held by it, and only onto the row this press began. ``False`` when nothing was written (replaced, or no row).
        """
        try:
            with self._transaction():
                held = self._cx.execute(
                    "SELECT 1 FROM publication_records WHERE build_id = ? AND turn = ? "
                    "AND lease_holder = ?",
                    (str(build_id), int(turn), str(holder)),
                ).fetchone()
                if held is None:
                    return False
                changed = self._cx.execute(
                    "UPDATE publication_outcomes SET outcome_json = ?, attempt_id = ?, "
                    "recorded_at = ? WHERE build_id = ? AND turn = ?",
                    (
                        json.dumps(outcome, sort_keys=True, default=str),
                        attempt_id,
                        now.isoformat(),
                        str(build_id),
                        int(turn),
                    ),
                ).rowcount
                if changed != 1:
                    return False
        except sqlite3.Error as exc:
            logger.warning(
                "publication record: %s's outcome could not be written (%s)", build_id, exc
            )
            return False
        return True

    def read_outcome(self, build_id: str) -> "StoredOutcome | None":
        """The latest press's row for this build, or ``None``."""
        try:
            row = self._cx.execute(
                "SELECT turn, attempt_id, outcome_json, result_written, standing_json, "
                "bus_sent_at, repair_at, warning_json, warning_delivered_at, finished_at "
                "FROM publication_outcomes WHERE build_id = ?",
                (str(build_id),),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if _no_outcome_table(exc) or "no such column" in str(exc).lower():
                return None
            raise
        if row is None:
            return None

        def decoded(text: Any) -> dict[str, Any]:
            try:
                value = json.loads(text or "{}")
            except (TypeError, ValueError):
                return {}
            return value if isinstance(value, dict) else {}

        return StoredOutcome(
            turn=int(row[0]),
            attempt_id=row[1],
            outcome=decoded(row[2]),
            result_written=bool(row[3]),
            standing=decoded(row[4]),
            bus_sent_at=row[5],
            repair_at=row[6],
            warning=decoded(row[7]),
            warning_delivered_at=row[8],
            finished_at=row[9],
        )

    def mark_outcome(self, *, build_id: str, turn: int, **marks: Any) -> bool:
        """Write finishing marks onto the press of ``turn``'s row.

        Each mark is a fact about that press's own bookkeeping; the row is
        keyed by the press's turn, so a mark can never land on another
        press's row. A dict is stored as JSON. ``False`` when no row matched.
        """
        unknown = set(marks) - _OUTCOME_MARKS
        if unknown:
            raise KeyError(f"{sorted(unknown)} are not finishing marks")
        values = [
            json.dumps(v, sort_keys=True, default=str) if isinstance(v, dict) else v
            for v in marks.values()
        ]
        columns = ", ".join(f"{name} = ?" for name in marks)
        try:
            with self._transaction():
                changed = self._cx.execute(
                    f"UPDATE publication_outcomes SET {columns} "
                    "WHERE build_id = ? AND turn = ?",
                    (*values, str(build_id), int(turn)),
                ).rowcount
        except sqlite3.Error as exc:
            logger.warning(
                "publication record: %s's finishing mark could not be written (%s)",
                build_id,
                exc,
            )
            return False
        return changed == 1

    def settle(
        self, *, build_id: str, turn: int, holder: str, now: datetime, result: str
    ) -> bool:
        """Write the result AND put the lease down, in one statement.

        Missions design section 11 (Codex implementation round 1, R1): a
        settlement written in one transaction and a release in another can be
        split by a crash, leaving a record with a result that is still held —
        which nothing revisits, and which keeps a stopped mission from ever
        draining. So the two go together, under the same turn and holder
        condition every write carries. ``False`` (never an exception) when
        this worker has been replaced and nothing was changed.
        """
        return self._write(
            build_id=build_id,
            turn=turn,
            now=now,
            assignments={"result": result, "lease_holder": None, "lease_expires_at": None},
            holder=holder,
        )

    # -- writing (always against the writer's own turn) ---------------------

    def _write(
        self,
        *,
        build_id: str,
        turn: int,
        now: datetime,
        assignments: dict[str, Any],
        lines_json: str | None = None,
        holder: str | None = None,
    ) -> bool:
        """One conditional UPDATE. ``False`` means the writer was replaced.

        The condition is in the same statement as the write, which is what
        makes it safe: there is no moment between checking the turn number and
        changing the row for a replaced worker to act in. ``holder``, when
        given, is part of the same condition.
        """
        sets = dict(assignments)
        if lines_json is not None:
            sets["lines_json"] = lines_json
        sets["updated_at"] = now.isoformat()
        columns = ", ".join(f"{name} = ?" for name in sets)
        values = list(sets.values()) + [str(build_id), int(turn)]
        held_by = ""
        if holder is not None:
            held_by = " AND lease_holder = ?"
            values.append(str(holder))
        try:
            with self._transaction():
                changed = self._cx.execute(
                    f"""
                    UPDATE publication_records
                       SET {columns}
                     WHERE build_id = ? AND turn = ?{held_by}
                    """,
                    values,
                ).rowcount
                # A RESULT IS MARKED AS THIS PRESS'S, in the same transaction
                # (press lease recovery): a result left by an earlier press is
                # then never mistaken for this one's.
                if changed == 1 and assignments.get("result"):
                    self._mark_the_result(str(build_id), int(turn))
        except sqlite3.Error as exc:
            logger.warning(
                "publication record: %s could not be written (%s)", build_id, exc
            )
            return False
        if changed != 1:
            logger.warning(
                "publication record: the write to %s on turn %s changed no row "
                "— this worker has been replaced and stops here",
                build_id,
                turn,
            )
            return False
        return True

    def record(
        self,
        *,
        build_id: str,
        turn: int,
        now: datetime,
        **fields: Any,
    ) -> bool:
        """Write the named fields onto the record. ``False`` = replaced.

        ``checked`` is given as a plain dictionary and stored as JSON; every
        other field is stored as it is. An unknown field name is a programming
        mistake and raises, because a silently dropped write is exactly what
        this record exists to prevent.
        """
        allowed = {
            "feature_id",
            "repo",
            "decided_by",
            "decided_at",
            "target_branch",
            "g_commit",
            "build_tip",
            "j_commit",
            "attempt",
            "result",
            "lease_holder",
            "lease_expires_at",
        }
        assignments: dict[str, Any] = {}
        for name, value in fields.items():
            if name == "checked":
                assignments["checked_json"] = json.dumps(
                    dict(value or {}), sort_keys=True, default=str
                )
                continue
            if name not in allowed:
                raise KeyError(
                    f"{name!r} is not a field of the publication record; the "
                    f"fields are {sorted(allowed | {'checked'})}"
                )
            assignments[name] = value
        if not assignments:
            return True
        return self._write(
            build_id=build_id, turn=turn, now=now, assignments=assignments
        )

    def _append_line(
        self,
        *,
        build_id: str,
        turn: int,
        now: datetime,
        line: PublicationLine,
        assignments: dict[str, Any] | None = None,
    ) -> bool:
        """Append one line, conditional on the turn, in one statement.

        The list is read first and written whole. A replaced worker's write is
        refused by the same condition as every other write, so the read cannot
        be acted on out of turn; a worker that is still current is the only
        one writing, so the list cannot be lost.
        """
        current = self.read(build_id)
        if not current.recorded:
            return False
        lines = [existing.to_wire() for existing in current.lines]
        lines.append(line.to_wire())
        return self._write(
            build_id=build_id,
            turn=turn,
            now=now,
            assignments=dict(assignments or {}),
            lines_json=json.dumps(lines, default=str),
        )

    def about_to(
        self,
        *,
        build_id: str,
        turn: int,
        now: datetime,
        step: str,
        attempt: int,
        inputs: dict[str, Any] | None = None,
        **fields: Any,
    ) -> bool:
        """Write down what is about to be done, BEFORE doing it.

        The attempt number and the exact inputs go on the line, so that a
        pick-up can ask the world about exactly this attempt rather than about
        the step in general.
        """
        assignments: dict[str, Any] = {"attempt": int(attempt)}
        for name, value in fields.items():
            assignments[name] = value
        return self._append_line(
            build_id=build_id,
            turn=turn,
            now=now,
            line=PublicationLine(
                kind=LINE_ABOUT_TO,
                step=str(step),
                attempt=int(attempt),
                at=now.isoformat(),
                detail=dict(inputs or {}),
            ),
            assignments=assignments,
        )

    def done(
        self,
        *,
        build_id: str,
        turn: int,
        now: datetime,
        step: str,
        attempt: int,
        result: dict[str, Any] | None = None,
        **fields: Any,
    ) -> bool:
        """Write down what that step produced, after it produced it."""
        assignments: dict[str, Any] = {}
        for name, value in fields.items():
            if name == "checked":
                assignments["checked_json"] = json.dumps(
                    dict(value or {}), sort_keys=True, default=str
                )
                continue
            assignments[name] = value
        return self._append_line(
            build_id=build_id,
            turn=turn,
            now=now,
            line=PublicationLine(
                kind=LINE_DONE,
                step=str(step),
                attempt=int(attempt),
                at=now.isoformat(),
                detail=dict(result or {}),
            ),
            assignments=assignments,
        )


def lines_in_words(lines: Iterable[PublicationLine]) -> Sequence[str]:
    """The record's lines as sentences, for a receipt or a page.

    Plain English on purpose: "about to join, attempt 1" reads the same to
    anybody, and the detail is beside it as data for anything that needs it.
    """
    said: list[str] = []
    for line in lines:
        said.append(f"{line.kind} {line.step}, attempt {line.attempt} ({line.at})")
    return said

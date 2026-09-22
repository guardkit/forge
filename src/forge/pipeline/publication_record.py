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
    "STEP_CANDIDATE_CHECK",
    "STEP_DEPLOY",
    "STEP_JOIN",
    "STEP_MERGE_CHECKS",
    "STEP_SEND",
    "PublicationLine",
    "PublicationRecord",
    "PublicationRecordStore",
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
#: reason is said. The only result this version can produce.
RESULT_PUBLICATION_PENDING: str = "publication pending"

#: The remote has the joined commit; the deploy has not finished. It is never
#: called a merge that is running. NOT reachable in this version.
RESULT_PUBLISHED_DEPLOYMENT_PENDING: str = "published, deployment pending"

#: The remote has the joined commit, and what was checked is deployed. NOT
#: reachable in this version.
#:
#: IT NAMES NO HOSTING PROVIDER. It used to read "merged into GitHub and
#: running", which was the design's own wording and wrong for central
#: orchestration: the factory knows only "the remote named origin", and a
#: project whose remote is somewhere else would have been told, in the
#: factory's own vocabulary, that it was merged into a service it has never
#: heard of. Renamed 22 September 2026 with its sibling in the press
#: (``RESULT_WORD_MERGED_AND_RUNNING``). Still unreachable, and pinned so.
RESULT_MERGED_AND_RUNNING: str = "merged into the remote and running"

#: The three, in the order a build passes through them.
PUBLICATION_RESULTS: tuple[str, ...] = (
    RESULT_PUBLICATION_PENDING,
    RESULT_PUBLISHED_DEPLOYMENT_PENDING,
    RESULT_MERGED_AND_RUNNING,
)


def default_lease_seconds() -> int:
    """How long a worker holds a build's record before it may be taken over.

    Long enough that a healthy press is never taken over mid-step, short
    enough that a dead one is not waited on all day. A worker renews while it
    works, so the number only bounds how long a *stopped* worker keeps its
    claim.
    """
    return 1800


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
    ) -> LeaseGrant | None:
        """Take this build's record, or answer ``None`` because someone holds it.

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
                    return LeaseGrant(
                        build_id=str(build_id),
                        holder=str(holder),
                        turn=1,
                        expires_at=expires,
                    )
                current_turn = int(row[0] or 0)
                current_holder = _as_text(row[1])
                expiry = _parse_time(row[2])
                held_by_another = (
                    current_holder is not None
                    and current_holder != str(holder)
                    and expiry is not None
                    and expiry > now
                )
                if held_by_another:
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

    # -- writing (always against the writer's own turn) ---------------------

    def _write(
        self,
        *,
        build_id: str,
        turn: int,
        now: datetime,
        assignments: dict[str, Any],
        lines_json: str | None = None,
    ) -> bool:
        """One conditional UPDATE. ``False`` means the writer was replaced.

        The condition is in the same statement as the write, which is what
        makes it safe: there is no moment between checking the turn number and
        changing the row for a replaced worker to act in.
        """
        sets = dict(assignments)
        if lines_json is not None:
            sets["lines_json"] = lines_json
        sets["updated_at"] = now.isoformat()
        columns = ", ".join(f"{name} = ?" for name in sets)
        values = list(sets.values()) + [str(build_id), int(turn)]
        try:
            with self._transaction():
                changed = self._cx.execute(
                    f"""
                    UPDATE publication_records
                       SET {columns}
                     WHERE build_id = ? AND turn = ?
                    """,
                    values,
                ).rowcount
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

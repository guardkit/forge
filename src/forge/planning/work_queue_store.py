"""The work queue the factory keeps for itself (Lane B stage one).

Public surface
==============

- :class:`WorkQueueStore` — sqlite adapter for ``work_queue`` /
  ``work_queue_events`` (schema v10).
- :class:`FiledRow` — what filing a sentence returns: the row's id, how many
  open rows are ahead of it, and whether this call is the one that created it.

Design notes
------------

Written beside :mod:`forge.planning.run_store` and in the same style: a
sqlite connection handed in, idempotency keyed on ``correlation_id``, and an
events row written for anything that changes a row.

**Rank.** The order of the queue is a REAL number per row, so putting one row
in front of another is arithmetic rather than a rewrite of every row:

- a new row goes to the back: ``max(rank) + 1.0`` (``1.0`` on an empty queue);
- ``next:`` and ``#12 next`` go to the front: ``min(rank) - 1.0``;
- ``before #12`` takes the midpoint between #12 and the row in front of it.

Repeated midpoints eventually run out of floating-point room. When two
neighbours collide, or come within ``RANK_EPSILON`` of one another, the open
rows are renumbered ``1.0, 2.0, ...`` in their intended order **inside the
same transaction** as the change that caused it, so a reader never sees a
half-renumbered queue.

**Nothing is ever deleted.** ``drop`` closes a row WITHDRAWN. The record of
what was asked for outlives the decision not to build it.

**One change, one transaction.** Every change to a row and the note of who
made it are written inside a single ``BEGIN IMMEDIATE``, so the queue is
never read with a row that moved and no record of who moved it.

References
----------
- ``docs/work-queue-spec-2026-09-05.md`` contracts 4 and 5.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Iterator, Sequence


#: Statuses that mean "still in the queue" — waiting, or handed to the chain
#: but not finished. Everything else is closed and never counts as ahead of
#: anything.
OPEN_STATUSES: tuple[str, ...] = ("QUEUED", "ADMITTED")

#: The event written on a row whose build the pipeline refused before it
#: wrote any build row (see :meth:`WorkQueueStore.record_build_rejection`).
BUILD_REJECTED_ACTION: str = "build_rejected"

#: Who writes that event: the build pipeline, not a person.
BUILD_REJECTED_ACTOR: str = "forge-pipeline"

#: Closed statuses, in the vocabulary the schema's CHECK allows.
CLOSED_STATUSES: tuple[str, ...] = ("DONE", "WITHDRAWN", "BLOCKED")

#: The kinds a row may carry. ``feature`` is the default a plain sentence gets.
KINDS: tuple[str, ...] = ("feature", "fix", "question")

#: Where a new row may be filed: the back (a plain sentence), the front
#: (``next:``) or in front of a named row (``before #12:``).
_POSITIONS: tuple[str, ...] = ("back", "front", "before")

#: Two neighbouring ranks closer than this are treated as a collision: the
#: open rows are renumbered 1.0, 2.0, ... rather than the gap being halved
#: again into the noise of the float.
RANK_EPSILON: float = 1e-6

#: The largest id this store will look for. SQLite counts further, but no
#: queue a person types into reaches two billion rows, and a bound here keeps
#: an absurd number away from the database driver, which raises on anything
#: past 64 bits rather than answering "no such row".
MAX_QUEUE_ID: int = 2**31 - 1


def valid_queue_id(value: Any) -> int | None:
    """The value as a row id, or None when it could never be one.

    Jarvis checks the *shape* of what someone typed and forwards whatever it
    matched, so this is the first place an id is really checked. An id is a
    plain counting number written in ordinary digits, and nothing else:
    ``True``, ``12.0``, ``"12"`` and ``"\u0661\u0662"`` are all refused, because
    Python would quietly read the last two as twelve and someone would have
    moved a row they never named.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 1 or value > MAX_QUEUE_ID:
        return None
    return value


def database_file(connection: sqlite3.Connection) -> str:
    """The full path of the file a connection's main database lives in.

    An empty string for a database held only in memory, which has no file
    and is never the same database as any other connection's.
    """
    for row in connection.execute("PRAGMA database_list").fetchall():
        if str(row[1]) == "main":
            path = str(row[2] or "")
            return os.path.realpath(path) if path else ""
    return ""


def same_database(
    first: sqlite3.Connection, second: sqlite3.Connection
) -> bool:
    """True when two connections read and write the one database file."""
    if first is second:
        return True
    first_file = database_file(first)
    return bool(first_file) and first_file == database_file(second)


@dataclass(frozen=True, slots=True)
class FiledRow:
    """The answer to "file this sentence".

    Attributes:
        queue_id: The row's id — the ``#12`` a person types.
        ahead: How many open rows sit in front of it right now.
        created: True when this call created the row; False when the
            correlation id was already filed (a redelivered message), which
            is how the caller knows not to say the same thing twice.
    """

    queue_id: int
    ahead: int
    created: bool


class WorkQueueStore:
    """Durable store for the work queue, backed by SQLite (schema v10).

    Thread-safety: instances are NOT thread-safe — one connection per thread,
    the same discipline as :class:`~forge.planning.run_store.SqlitePlanningRunStore`.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Initialise the store with a writable connection (schema v10+).

        ``clock`` reads the current time for every timestamp the store writes.
        It defaults to the wall clock; a test hands in one it can move, so the
        staleness week can pass without anyone waiting a week.
        """
        self._connection = connection
        self._connection.row_factory = sqlite3.Row
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # -- reads ----------------------------------------------------------

    @property
    def connection(self) -> sqlite3.Connection:
        """The connection this store reads and writes.

        It is the forge's own database, where the planning runs, the builds,
        the merge cards and the publication records are kept too; the
        take-next loop reads those through it, so what it counts and what it
        claims are read in the one place.
        """
        return self._connection

    def database_file(self) -> str:
        """The file this store's database lives in (see :func:`database_file`)."""
        return database_file(self._connection)

    def get(self, queue_id: Any) -> sqlite3.Row | None:
        """Return one row by id, open or closed; None when there is no such id.

        An id that could never name a row — nought, a negative, an absurd
        number, or anything that is not a plain whole number — reads as None
        and never reaches the database.
        """
        queue_id = valid_queue_id(queue_id)
        if queue_id is None:
            return None
        cursor = self._connection.execute(
            "SELECT * FROM work_queue WHERE id = ?", (queue_id,)
        )
        return cursor.fetchone()

    def get_by_correlation_id(self, correlation_id: str) -> sqlite3.Row | None:
        """Return the row filed under ``correlation_id``, or None."""
        cursor = self._connection.execute(
            "SELECT * FROM work_queue WHERE correlation_id = ?", (correlation_id,)
        )
        return cursor.fetchone()

    def list_open(self) -> list[sqlite3.Row]:
        """Every row still in the queue, in the order it would be taken."""
        placeholders = ", ".join("?" for _ in OPEN_STATUSES)
        cursor = self._connection.execute(
            f"""
            SELECT * FROM work_queue
            WHERE status IN ({placeholders})
            ORDER BY rank ASC, queued_at ASC, id ASC
            """,
            OPEN_STATUSES,
        )
        return list(cursor.fetchall())

    def count_ahead(self, queue_id: int) -> int:
        """How many open rows sit in front of ``queue_id`` right now."""
        rows = self.list_open()
        for index, row in enumerate(rows):
            if int(row["id"]) == queue_id:
                return index
        return 0

    def list_events(self, queue_id: int) -> list[sqlite3.Row]:
        """Every event written against one row, oldest first."""
        cursor = self._connection.execute(
            "SELECT * FROM work_queue_events WHERE queue_id = ? ORDER BY id ASC",
            (queue_id,),
        )
        return list(cursor.fetchall())

    #: The actions that mean "this row was filed" — the event that carries
    #: where the sentence came from (see :meth:`origin_details`). ``minted``
    #: is the fix producer's filing action (conductor rewire rule 1): a
    #: repair row is filed by the factory rather than typed by a person, and
    #: its filing event carries the failed build it came from.
    _FILING_ACTIONS: tuple[str, ...] = (
        "queued",
        "add_front",
        "add_before",
        "minted",
    )

    def origin_details(self, queue_id: int) -> dict[str, Any]:
        """Where the sentence came from, as written down when it was filed.

        The queue table has no column for the Slack thread anchor, the adapter
        or the trigger — the spec's table does not carry them — so the filing
        event's ``details_json`` does, and this reads it back. An empty dict
        when nothing was recorded, so the caller falls back to its defaults.
        """
        for row in self.list_events(queue_id):
            if str(row["action"]) in self._FILING_ACTIONS and row["details_json"]:
                loaded = json.loads(str(row["details_json"]))
                return loaded if isinstance(loaded, dict) else {}
        return {}

    def latest_event_at(self, queue_id: int, action: str) -> str | None:
        """When ``action`` was last written against the row, or None."""
        latest: str | None = None
        for row in self.list_events(queue_id):
            if str(row["action"]) == action:
                latest = str(row["recorded_at"])
        return latest

    def has_event(self, queue_id: int, action: str) -> bool:
        """True when ``action`` has ever been written against the row."""
        return self.latest_event_at(queue_id, action) is not None

    # -- filing ---------------------------------------------------------

    def file_sentence(
        self,
        *,
        correlation_id: str,
        sentence: str,
        originating_user: str,
        target_repo: str | None = None,
        kind: str = "feature",
        position: str = "back",
        before_id: int | None = None,
        actor_identity: str | None = None,
        action: str | None = None,
        parent_request_id: str | None = None,
        originating_adapter: str | None = None,
        triggered_by: str | None = None,
        extra_details: dict[str, Any] | None = None,
    ) -> FiledRow:
        """File one sentence and return its id and how many are ahead of it.

        Idempotent on ``correlation_id``: filing the same correlation id twice
        returns the existing row with ``created=False`` and writes nothing.

        Parameters
        ----------
        position:
            ``"back"`` (a plain sentence), ``"front"`` (``next: ...``) or
            ``"before"`` (``before #12: ...``, which needs ``before_id``).
        actor_identity:
            Who is filing it; defaults to ``originating_user``.
        action:
            The events-row action; defaults to ``queued`` / ``add_front`` /
            ``add_before`` to match ``position``.
        parent_request_id, originating_adapter, triggered_by:
            Where the sentence came from. The table in the spec has no column
            for these three, but the planning run the take-next loop creates
            later needs them or the person's Slack thread goes quiet — so they
            ride along in the filing event's ``details_json`` and
            :meth:`origin_details` reads them back at admission time.
        extra_details:
            Anything else worth writing down about where this row came from,
            merged into the same filing event. The fix producer puts the
            failed build's id and the path of its failure pack here, so the
            row and the evidence behind it commit together.
        """
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}; expected one of {KINDS}")
        if position not in _POSITIONS:
            raise ValueError(
                f"unknown position {position!r}; expected one of {_POSITIONS}"
            )

        existing = self.get_by_correlation_id(correlation_id)
        if existing is not None:
            # A redelivered message: the row is already there, whatever has
            # happened to the queue since.
            return FiledRow(
                queue_id=int(existing["id"]),
                ahead=self.count_ahead(int(existing["id"])),
                created=False,
            )

        if position == "before":
            antecedent = self.get(before_id)
            if antecedent is None or antecedent["status"] not in OPEN_STATUSES:
                # Never quietly fall back to the back of the queue: someone
                # asking for a row to go in front of one that is gone has to
                # be told, not obeyed differently.
                raise ValueError(
                    f"cannot file in front of #{before_id}: there is no such open row"
                )
            before_id = int(antecedent["id"])

        queued_at = self._now()
        default_action = {
            "back": "queued",
            "front": "add_front",
            "before": "add_before",
        }[position]
        try:
            # The row, its place in the order and the event that says who
            # filed it all commit together: a reader never sees a row with no
            # history, or a history with no row.
            with self._transaction():
                cursor = self._connection.execute(
                    """
                    INSERT INTO work_queue (
                        sentence, target_repo, kind, status, rank,
                        originating_user, correlation_id, queued_at
                    ) VALUES (?, ?, ?, 'QUEUED', ?, ?, ?, ?)
                    """,
                    (
                        sentence,
                        target_repo,
                        kind,
                        0.0,  # provisional; _place writes the real rank below
                        originating_user,
                        correlation_id,
                        queued_at,
                    ),
                )
                queue_id = int(cursor.lastrowid or 0)
                self._place(queue_id, position=position, before_id=before_id)
                details: dict[str, Any] = {
                    "kind": kind,
                    "target_repo": target_repo,
                    "parent_request_id": parent_request_id,
                    "originating_adapter": originating_adapter,
                    "triggered_by": triggered_by,
                }
                if extra_details:
                    details.update(extra_details)
                self._record_event(
                    queue_id=queue_id,
                    action=action or default_action,
                    actor_identity=actor_identity or originating_user,
                    details=details,
                )
        except sqlite3.IntegrityError:
            # Race: another worker filed this correlation id between the read
            # and the insert. The queue is idempotent, so report theirs.
            existing = self.get_by_correlation_id(correlation_id)
            if existing is None:
                raise
            return FiledRow(
                queue_id=int(existing["id"]),
                ahead=self.count_ahead(int(existing["id"])),
                created=False,
            )

        return FiledRow(
            queue_id=queue_id, ahead=self.count_ahead(queue_id), created=True
        )

    # -- changing the order --------------------------------------------

    def promote(self, queue_id: Any, *, actor_identity: str) -> bool:
        """Move an open row to the front. False when it is not open."""
        row = self.get(queue_id)
        if row is None or row["status"] not in OPEN_STATUSES:
            return False
        queue_id = int(row["id"])
        with self._transaction():
            self._place(queue_id, position="front", before_id=None)
            self._record_event(
                queue_id=queue_id, action="promote", actor_identity=actor_identity
            )
        return True

    def link(self, queue_id: Any, after_id: Any, *, actor_identity: str) -> bool:
        """Make one row wait for another. False when the wait cannot be made.

        Only the ``after_id`` column moves: the waiting row keeps its place in
        the order and is simply not taken until its antecedent's work has
        landed (the take-next loop decides that; see its ``_is_eligible``).

        Refused, with nothing written and nothing recorded, when either id
        names no row, when the row is closed, when a row is asked to wait for
        itself, and when the row it would wait for is already waiting on it —
        that last one would leave the two of them waiting for each other for
        ever, and the queue would quietly stop.
        """
        row = self.get(queue_id)
        antecedent = self.get(after_id)
        if row is None or antecedent is None or row["status"] not in OPEN_STATUSES:
            return False
        queue_id = int(row["id"])
        after_id = int(antecedent["id"])
        if queue_id == after_id or self.waits_on(after_id, queue_id):
            return False
        with self._transaction():
            self._connection.execute(
                "UPDATE work_queue SET after_id = ? WHERE id = ?",
                (after_id, queue_id),
            )
            self._record_event(
                queue_id=queue_id,
                action="link",
                actor_identity=actor_identity,
                details={"after_id": after_id},
            )
        return True

    def waits_on(self, queue_id: Any, other_id: Any) -> bool:
        """True when one row already waits for another, however far apart.

        Follows the chain of "wait for that one" links from ``queue_id``. A
        chain that loops back on itself (an older database could hold one)
        stops the walk rather than spinning.
        """
        current = valid_queue_id(queue_id)
        target = valid_queue_id(other_id)
        if current is None or target is None:
            return False
        seen: set[int] = set()
        while current is not None and current not in seen:
            if current == target:
                return True
            seen.add(current)
            row = self.get(current)
            if row is None:
                return False
            after = row["after_id"]
            current = int(after) if after is not None else None
        return False

    # -- admission (the take-next loop) ---------------------------------

    def admit(
        self,
        queue_id: Any,
        *,
        actor_identity: str,
        max_in_flight: int | None = None,
        count_in_flight: Callable[[], int] | None = None,
    ) -> bool:
        """Mark a QUEUED row ADMITTED — the loop is about to start its run.

        Compare-and-swap on QUEUED, so two loops (or a loop racing a drop)
        can never both admit the same row. False when the row was not QUEUED.

        **Room and the claim are one step.** Given ``max_in_flight`` and
        ``count_in_flight``, the count is taken inside the same
        ``BEGIN IMMEDIATE`` transaction as the claim, and the row is admitted
        only when the count is below the limit; otherwise nothing is written
        and the answer is False. ``BEGIN IMMEDIATE`` takes the database's one
        write lock before the count is read, so no other admitter — another
        loop, another process, or anything else writing a planning run or a
        build — can change what was counted until this claim has committed.
        Two admitters can no longer both see the last place free and both take
        it. The count must read this store's own database for that to hold;
        the loop checks so when it is wired (``in_flight_database``).
        """
        if (max_in_flight is None) != (count_in_flight is None):
            raise ValueError(
                "admit() takes max_in_flight and count_in_flight together, "
                "or neither"
            )
        checked = valid_queue_id(queue_id)
        if checked is None:
            return False
        if count_in_flight is not None and self._connection.in_transaction:
            # The count and the claim must run under this store's own BEGIN
            # IMMEDIATE; joining a transaction someone else opened (perhaps a
            # deferred one, holding no write lock) would let the count go stale.
            raise RuntimeError(
                "admit() with a count must open its own BEGIN IMMEDIATE "
                "transaction; this connection is already inside one"
            )
        with self._transaction():
            if count_in_flight is not None and max_in_flight is not None:
                if count_in_flight() >= max_in_flight:
                    return False
            cursor = self._connection.execute(
                """
                UPDATE work_queue
                SET status = 'ADMITTED', admitted_at = ?
                WHERE id = ? AND status = 'QUEUED'
                """,
                (self._now(), checked),
            )
            if cursor.rowcount != 1:
                return False
            self._record_event(
                queue_id=checked, action="admitted", actor_identity=actor_identity
            )
        return True

    def requeue(
        self, queue_id: Any, *, actor_identity: str, reason: str | None = None
    ) -> bool:
        """Put an ADMITTED row back in the queue, so the next tick retries it.

        Used when starting the planning run itself failed: the sentence is
        still wanted, and leaving it ADMITTED with no run behind it would stop
        the queue for good.
        """
        checked = valid_queue_id(queue_id)
        if checked is None:
            return False
        with self._transaction():
            cursor = self._connection.execute(
                """
                UPDATE work_queue
                SET status = 'QUEUED', admitted_at = NULL
                WHERE id = ? AND status = 'ADMITTED'
                """,
                (checked,),
            )
            if cursor.rowcount != 1:
                return False
            self._record_event(
                queue_id=checked,
                action="requeued",
                actor_identity=actor_identity,
                details={"reason": reason} if reason else None,
            )
        return True

    def mark_stale_pinged(self, queue_id: Any, *, at: str) -> None:
        """Record that the forge has just asked about this row's age."""
        checked = valid_queue_id(queue_id)
        if checked is None:
            return
        with self._transaction():
            self._connection.execute(
                "UPDATE work_queue SET stale_pinged_at = ? WHERE id = ?",
                (at, checked),
            )

    # -- keeping and closing --------------------------------------------

    def keep(self, queue_id: Any, *, actor_identity: str) -> bool:
        """Reset the staleness clock on an open row and count the keep."""
        row = self.get(queue_id)
        if row is None or row["status"] not in OPEN_STATUSES:
            return False
        queue_id = int(row["id"])
        with self._transaction():
            self._connection.execute(
                """
                UPDATE work_queue
                SET keep_count = keep_count + 1, stale_pinged_at = NULL
                WHERE id = ?
                """,
                (queue_id,),
            )
            self._record_event(
                queue_id=queue_id, action="keep", actor_identity=actor_identity
            )
        return True

    def drop(
        self, queue_id: Any, *, actor_identity: str, reason: str | None = None
    ) -> bool:
        """Close an open row WITHDRAWN. The row is never deleted."""
        return self._close(
            queue_id,
            status="WITHDRAWN",
            reason=reason,
            action="drop",
            actor_identity=actor_identity,
        )

    def close(
        self,
        queue_id: Any,
        *,
        status: str,
        actor_identity: str,
        reason: str | None = None,
    ) -> bool:
        """Close a row DONE or BLOCKED when its planning run reaches the end."""
        if status not in ("DONE", "BLOCKED"):
            raise ValueError(f"close() takes DONE or BLOCKED, not {status!r}")
        return self._close(
            queue_id,
            status=status,
            reason=reason,
            action=f"close_{status.lower()}",
            actor_identity=actor_identity,
        )

    # -- events ----------------------------------------------------------

    def record_event(
        self,
        *,
        queue_id: int,
        action: str,
        actor_identity: str,
        details: dict[str, Any] | None = None,
    ) -> int:
        """Write one ``work_queue_events`` row of its own; return its id.

        Every change the store makes writes its event inside the same
        transaction as the change (see :meth:`_record_event`); this public
        one is for an event that stands alone, like the loop noting what it
        would have picked.
        """
        with self._transaction():
            return self._record_event(
                queue_id=queue_id,
                action=action,
                actor_identity=actor_identity,
                details=details,
            )

    def record_build_rejection(
        self,
        correlation_id: str,
        reason: str,
        *,
        actor_identity: str = BUILD_REJECTED_ACTOR,
    ) -> bool:
        """Note on a queue row that its build was refused before it started.

        The pipeline refuses some builds before it writes any build row — an
        originator that is not approved, a feature file outside the allowed
        folders, a repository the sandbox policy will not build. Without a
        note, nothing durable says so, and a row told to wait "after" this one
        would wait for ever. This writes one ``build_rejected`` event, with
        the reason, on the row filed under ``correlation_id``; the take-next
        loop reads it and asks "hold or go".

        A no-op returning False when no row was filed under that correlation
        id (a build queued directly, not through the queue), and when the row
        already carries the note (a redelivered refusal says nothing new).
        """
        row = self.get_by_correlation_id(correlation_id)
        if row is None:
            return False
        queue_id = int(row["id"])
        if self.has_event(queue_id, BUILD_REJECTED_ACTION):
            return False
        self.record_event(
            queue_id=queue_id,
            action=BUILD_REJECTED_ACTION,
            actor_identity=actor_identity,
            details={"reason": reason},
        )
        return True

    def build_rejection_reason(self, correlation_id: str) -> str | None:
        """The reason the row's build was refused before it started, or None."""
        row = self.get_by_correlation_id(correlation_id)
        if row is None:
            return None
        for event in reversed(self.list_events(int(row["id"]))):
            if str(event["action"]) != BUILD_REJECTED_ACTION:
                continue
            details: Any = {}
            if event["details_json"]:
                try:
                    details = json.loads(str(event["details_json"]))
                except ValueError:
                    details = {}
            reason = details.get("reason") if isinstance(details, dict) else None
            return str(reason or "no reason was given")
        return None

    def _record_event(
        self,
        *,
        queue_id: int,
        action: str,
        actor_identity: str,
        details: dict[str, Any] | None = None,
    ) -> int:
        """Write one events row inside a transaction the caller owns."""
        cursor = self._connection.execute(
            """
            INSERT INTO work_queue_events (
                queue_id, action, actor_identity, details_json, recorded_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                queue_id,
                action,
                actor_identity,
                json.dumps(details) if details else None,
                self._now(),
            ),
        )
        return int(cursor.lastrowid or 0)

    # -- internals -------------------------------------------------------

    def _now(self) -> str:
        return self._clock().isoformat()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Everything inside lands together, or none of it lands.

        The forge's write connection is opened in autocommit mode
        (``isolation_level=None``, ``adapters/sqlite/connect.py``), so
        ``with connection`` starts nothing and rolls nothing back — each
        statement would commit on its own and a row could change with no note
        of who changed it. The estate's answer, the one the build queue and
        the bridge registry already use, is to say ``BEGIN IMMEDIATE`` out
        loud. A transaction someone else has already opened on this
        connection is left to them.
        """
        if self._connection.in_transaction:
            yield
            return
        self._connection.execute("BEGIN IMMEDIATE;")
        try:
            yield
        except BaseException:
            try:
                self._connection.execute("ROLLBACK;")
            except sqlite3.Error:  # pragma: no cover - rollback failure is rare
                pass
            raise
        self._connection.execute("COMMIT;")

    def _close(
        self,
        queue_id: Any,
        *,
        status: str,
        reason: str | None,
        action: str,
        actor_identity: str,
    ) -> bool:
        """Close a row and write why, both in one transaction."""
        row = self.get(queue_id)
        if row is None or row["status"] not in OPEN_STATUSES:
            return False
        queue_id = int(row["id"])
        with self._transaction():
            cursor = self._connection.execute(
                """
                UPDATE work_queue
                SET status = ?, closed_at = ?, closed_reason = ?
                WHERE id = ? AND status = ?
                """,
                (status, self._now(), reason, queue_id, row["status"]),
            )
            if cursor.rowcount != 1:
                return False
            self._record_event(
                queue_id=queue_id,
                action=action,
                actor_identity=actor_identity,
                details={"reason": reason} if reason else None,
            )
        return True

    def _place(self, queue_id: int, *, position: str, before_id: int | None) -> None:
        """Give ``queue_id`` its rank, renumbering the queue if it has to.

        Called inside a transaction the caller owns, so a renumber and the
        change that caused it commit together.
        """
        others = [
            row for row in self._open_rows() if int(row["id"]) != queue_id
        ]
        index, rank = self._rank_for(others, position=position, before_id=before_id)
        self._connection.execute(
            "UPDATE work_queue SET rank = ? WHERE id = ?", (rank, queue_id)
        )

        intended: list[int] = [int(row["id"]) for row in others]
        intended.insert(index, queue_id)
        ranks = [
            rank if identifier == queue_id else self._rank_of(others, identifier)
            for identifier in intended
        ]
        if self._needs_renumber(ranks):
            self._renumber(intended)

    def _open_rows(self) -> list[sqlite3.Row]:
        placeholders = ", ".join("?" for _ in OPEN_STATUSES)
        cursor = self._connection.execute(
            f"""
            SELECT id, rank FROM work_queue
            WHERE status IN ({placeholders})
            ORDER BY rank ASC, queued_at ASC, id ASC
            """,
            OPEN_STATUSES,
        )
        return list(cursor.fetchall())

    @staticmethod
    def _rank_of(rows: Sequence[sqlite3.Row], queue_id: int) -> float:
        for row in rows:
            if int(row["id"]) == queue_id:
                return float(row["rank"])
        return 0.0

    @staticmethod
    def _rank_for(
        others: Sequence[sqlite3.Row], *, position: str, before_id: int | None
    ) -> tuple[int, float]:
        """Return (index in the open order, rank) for the row being placed."""
        if not others:
            return 0, 1.0
        if position == "back":
            return len(others), float(others[-1]["rank"]) + 1.0
        if position == "front":
            return 0, float(others[0]["rank"]) - 1.0
        if position == "before":
            index = next(
                (
                    position_index
                    for position_index, row in enumerate(others)
                    if int(row["id"]) == before_id
                ),
                None,
            )
            if index is None:
                # The named row is not open. Never fall back to the back of
                # the queue: someone who asked for "in front of #12" would
                # silently get the opposite. file_sentence checks the id
                # first, so this is the last line of defence.
                raise ValueError(
                    f"cannot file in front of #{before_id}: there is no such open row"
                )
            if index == 0:
                return 0, float(others[0]["rank"]) - 1.0
            previous = float(others[index - 1]["rank"])
            target = float(others[index]["rank"])
            return index, (previous + target) / 2.0
        raise ValueError(f"unknown position {position!r}")

    @staticmethod
    def _needs_renumber(ranks: Sequence[float]) -> bool:
        """True when two neighbours collide or are within RANK_EPSILON."""
        return any(
            (later - earlier) < RANK_EPSILON
            for earlier, later in zip(ranks, ranks[1:])
        )

    def _renumber(self, ordered_ids: Iterable[int]) -> None:
        """Rewrite the open rows as 1.0, 2.0, ... in the order given."""
        for position, queue_id in enumerate(ordered_ids, start=1):
            self._connection.execute(
                "UPDATE work_queue SET rank = ? WHERE id = ?",
                (float(position), queue_id),
            )


__all__ = [
    "BUILD_REJECTED_ACTION",
    "BUILD_REJECTED_ACTOR",
    "CLOSED_STATUSES",
    "FiledRow",
    "KINDS",
    "MAX_QUEUE_ID",
    "OPEN_STATUSES",
    "RANK_EPSILON",
    "WorkQueueStore",
    "database_file",
    "same_database",
    "valid_queue_id",
]

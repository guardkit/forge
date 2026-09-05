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

References
----------
- ``docs/work-queue-spec-2026-09-05.md`` contracts 4 and 5.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence


#: Statuses that mean "still in the queue" — waiting, or handed to the chain
#: but not finished. Everything else is closed and never counts as ahead of
#: anything.
OPEN_STATUSES: tuple[str, ...] = ("QUEUED", "ADMITTED")

#: Closed statuses, in the vocabulary the schema's CHECK allows.
CLOSED_STATUSES: tuple[str, ...] = ("DONE", "WITHDRAWN", "BLOCKED")

#: The kinds a row may carry. ``feature`` is the default a plain sentence gets.
KINDS: tuple[str, ...] = ("feature", "fix", "question")

#: Two neighbouring ranks closer than this are treated as a collision: the
#: open rows are renumbered 1.0, 2.0, ... rather than the gap being halved
#: again into the noise of the float.
RANK_EPSILON: float = 1e-6


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

    def __init__(self, connection: sqlite3.Connection) -> None:
        """Initialise the store with a writable connection (schema v10+)."""
        self._connection = connection
        self._connection.row_factory = sqlite3.Row

    # -- reads ----------------------------------------------------------

    def get(self, queue_id: int) -> sqlite3.Row | None:
        """Return one row by id, open or closed; None when there is no such id."""
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
        """
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}; expected one of {KINDS}")

        existing = self.get_by_correlation_id(correlation_id)
        if existing is not None:
            return FiledRow(
                queue_id=int(existing["id"]),
                ahead=self.count_ahead(int(existing["id"])),
                created=False,
            )

        queued_at = self._now()
        try:
            with self._connection:
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

        default_action = {
            "back": "queued",
            "front": "add_front",
            "before": "add_before",
        }[position]
        self.record_event(
            queue_id=queue_id,
            action=action or default_action,
            actor_identity=actor_identity or originating_user,
            details={"kind": kind, "target_repo": target_repo},
        )
        return FiledRow(
            queue_id=queue_id, ahead=self.count_ahead(queue_id), created=True
        )

    # -- changing the order --------------------------------------------

    def promote(self, queue_id: int, *, actor_identity: str) -> bool:
        """Move an open row to the front. False when it is not open."""
        row = self.get(queue_id)
        if row is None or row["status"] not in OPEN_STATUSES:
            return False
        with self._connection:
            self._place(queue_id, position="front", before_id=None)
        self.record_event(
            queue_id=queue_id, action="promote", actor_identity=actor_identity
        )
        return True

    def link(self, queue_id: int, after_id: int, *, actor_identity: str) -> bool:
        """Make one row wait for another. False when either row is unknown.

        Only the ``after_id`` column moves: the waiting row keeps its place in
        the order and is simply not taken until its antecedent is DONE.
        """
        row = self.get(queue_id)
        antecedent = self.get(after_id)
        if row is None or antecedent is None or row["status"] not in OPEN_STATUSES:
            return False
        with self._connection:
            self._connection.execute(
                "UPDATE work_queue SET after_id = ? WHERE id = ?",
                (after_id, queue_id),
            )
        self.record_event(
            queue_id=queue_id,
            action="link",
            actor_identity=actor_identity,
            details={"after_id": after_id},
        )
        return True

    # -- keeping and closing --------------------------------------------

    def keep(self, queue_id: int, *, actor_identity: str) -> bool:
        """Reset the staleness clock on an open row and count the keep."""
        row = self.get(queue_id)
        if row is None or row["status"] not in OPEN_STATUSES:
            return False
        with self._connection:
            self._connection.execute(
                """
                UPDATE work_queue
                SET keep_count = keep_count + 1, stale_pinged_at = NULL
                WHERE id = ?
                """,
                (queue_id,),
            )
        self.record_event(
            queue_id=queue_id, action="keep", actor_identity=actor_identity
        )
        return True

    def drop(
        self, queue_id: int, *, actor_identity: str, reason: str | None = None
    ) -> bool:
        """Close an open row WITHDRAWN. The row is never deleted."""
        if not self._close(queue_id, status="WITHDRAWN", reason=reason):
            return False
        self.record_event(
            queue_id=queue_id, action="drop", actor_identity=actor_identity
        )
        return True

    def close(
        self,
        queue_id: int,
        *,
        status: str,
        actor_identity: str,
        reason: str | None = None,
    ) -> bool:
        """Close a row DONE or BLOCKED when its planning run reaches the end."""
        if status not in ("DONE", "BLOCKED"):
            raise ValueError(f"close() takes DONE or BLOCKED, not {status!r}")
        if not self._close(queue_id, status=status, reason=reason):
            return False
        self.record_event(
            queue_id=queue_id,
            action=f"close_{status.lower()}",
            actor_identity=actor_identity,
            details={"reason": reason} if reason else None,
        )
        return True

    # -- events ----------------------------------------------------------

    def record_event(
        self,
        *,
        queue_id: int,
        action: str,
        actor_identity: str,
        details: dict[str, Any] | None = None,
    ) -> int:
        """Write one ``work_queue_events`` row; return its id."""
        with self._connection:
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

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _close(self, queue_id: int, *, status: str, reason: str | None) -> bool:
        row = self.get(queue_id)
        if row is None or row["status"] not in OPEN_STATUSES:
            return False
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE work_queue
                SET status = ?, closed_at = ?, closed_reason = ?
                WHERE id = ? AND status = ?
                """,
                (status, self._now(), reason, queue_id, row["status"]),
            )
        return cursor.rowcount == 1

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
                # The named row is no longer open — the back is the honest
                # place for it, and the caller has already checked the id.
                return len(others), float(others[-1]["rank"]) + 1.0
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
    "CLOSED_STATUSES",
    "FiledRow",
    "KINDS",
    "OPEN_STATUSES",
    "RANK_EPSILON",
    "WorkQueueStore",
]

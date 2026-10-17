"""The mission record: a batch of dependent features on one branch (17 October 2026).

A mission is a short list of feature sentences for one repository, with
"needs" links between them (missions design, sections 1 and 2). It is shown
on one plan card; approving it creates ``mission/m-<6 hex>`` from main, and
each feature is planned and built once everything it needs has landed on that
branch. This module is the only writer of the four mission tables.

THE TABLES ARE OUTSIDE THE MIGRATION LADDER, ON PURPOSE, exactly as
``merge_card_revisions`` is (``forge.lifecycle.card_revisions``). The ledger
stays at schema 19 so rolling back to an image without missions is a plain
image swap: that image refuses a schema it does not know, but ignores tables
it does not use. :func:`ensure_tables` creates them ``IF NOT EXISTS`` at the
coordinator's boot (and the estate rollout helper's copy of the boot steps
does the same), and adds ``builds.mission_id`` and ``builds.mission_role``
only when a probe finds them missing.

MISSION WORK NEVER GOES INTO ``work_queue`` (design round 2, R10). The older
image would admit ordinary queue rows from main with no prerequisites and with
per-feature cards. A mission's features and attempts live only here.

ATTEMPTS. Each feature is built by one attempt, and at most one more after a
conflict on the mission branch. An attempt's correlation id is
``<mission correlation id>-<KEY>-a<attempt>``; keys are letters and digits
only, so the id is unambiguous and passes both the routing check and the
intake contract. ``UNIQUE`` on that id, and ``CHECK (attempt IN (1, 2))``,
make filing idempotent and a third attempt impossible.

LINKS ARE BETWEEN KEYS, NOT ATTEMPTS. A feature may start when the latest
attempt of every key it needs has LANDED, so a rebuild's dependants follow it.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

__all__ = [
    "ACTIVE_MISSION_STATUSES",
    "ATTEMPT_STATUSES",
    "CONFLICT_REASON",
    "MISSION_KEY_PATTERN",
    "MISSION_ROLE_FEATURE",
    "MISSION_ROLE_MERGE",
    "MISSION_STATUSES",
    "Mission",
    "MissionAttempt",
    "MissionFeature",
    "MissionStore",
    "attempt_correlation_id",
    "builds_record_missions",
    "ensure_tables",
    "mission_branch",
    "new_mission_id",
]

#: Every state a mission can be in (design section 2).
MISSION_STATUSES: tuple[str, ...] = (
    "PROPOSED",
    "REJECTED",
    "NOT_STARTED",
    "STARTING",
    "RUNNING",
    "READY",
    "MERGING",
    "MERGED",
    "NOT_MERGED",
)

#: The states in which a mission has work under way or a card still to come.
#: The rollback query and ``router_restart_blockers`` both read this list.
ACTIVE_MISSION_STATUSES: tuple[str, ...] = ("STARTING", "RUNNING", "READY", "MERGING")

#: The states that end a mission; ``closed_at`` is written on entering one.
_CLOSED_MISSION_STATUSES: frozenset[str] = frozenset(
    {"REJECTED", "NOT_STARTED", "MERGED", "NOT_MERGED"}
)

#: Every state an attempt can be in.
ATTEMPT_STATUSES: tuple[str, ...] = ("WAITING", "ADMITTED", "LANDED", "FAILED", "HELD")

#: ``builds.mission_role`` for a feature build, and for the mission's own merge.
MISSION_ROLE_FEATURE: str = "feature"
MISSION_ROLE_MERGE: str = "mission-merge"

#: A key, after upper-casing: 1-12 letters or digits, no hyphen.
MISSION_KEY_PATTERN: str = r"^[A-Z0-9]{1,12}$"

#: The reason an attempt-1 closes with when its merge into the mission branch
#: stopped on a conflict, and the one reason that files attempt 2.
CONFLICT_REASON: str = "conflict"

_TABLES_SQL: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS missions ("
    "id TEXT PRIMARY KEY, "
    "title TEXT NOT NULL, "
    "target_repo TEXT NOT NULL, "
    "branch TEXT NOT NULL UNIQUE, "
    "base_commit TEXT, "
    "final_commit TEXT, "
    "status TEXT NOT NULL CHECK (status IN ("
    + ", ".join(f"'{s}'" for s in MISSION_STATUSES)
    + ")), "
    "correlation_id TEXT NOT NULL UNIQUE, "
    "originating_user TEXT NOT NULL, "
    "plan_request_id TEXT, "
    "created_at TEXT NOT NULL, approved_at TEXT, approved_by TEXT, "
    "closed_at TEXT, closed_reason TEXT"
    ") STRICT",
    "CREATE TABLE IF NOT EXISTS mission_features ("
    "mission_id TEXT NOT NULL REFERENCES missions(id), "
    "key TEXT NOT NULL, "
    "sentence TEXT NOT NULL, "
    "no_sign_in INTEGER NOT NULL DEFAULT 0, "
    "PRIMARY KEY (mission_id, key)"
    ") STRICT",
    "CREATE TABLE IF NOT EXISTS mission_needs ("
    "mission_id TEXT NOT NULL, key TEXT NOT NULL, needs_key TEXT NOT NULL, "
    "PRIMARY KEY (mission_id, key, needs_key)"
    ") STRICT",
    "CREATE TABLE IF NOT EXISTS mission_attempts ("
    "mission_id TEXT NOT NULL, key TEXT NOT NULL, "
    "attempt INTEGER NOT NULL CHECK (attempt IN (1, 2)), "
    "correlation_id TEXT NOT NULL UNIQUE, "
    "status TEXT NOT NULL CHECK (status IN ("
    + ", ".join(f"'{s}'" for s in ATTEMPT_STATUSES)
    + ")), "
    "reason TEXT, admitted_at TEXT, closed_at TEXT, "
    "PRIMARY KEY (mission_id, key, attempt)"
    ") STRICT",
)

#: The two ``builds`` columns, added only when missing.
_BUILD_COLUMNS: tuple[tuple[str, str], ...] = (
    ("mission_id", "TEXT"),
    ("mission_role", "TEXT"),
)


def ensure_tables(cx: sqlite3.Connection) -> None:
    """Create the mission tables and the two ``builds`` columns. Idempotent.

    Called at the coordinator's boot (``bind_production_serve``) and by the
    estate rollout helper's copy of the boot steps. A ledger with no
    ``builds`` table yet (a bare test database) gets the tables only.
    """
    for statement in _TABLES_SQL:
        cx.execute(statement)
    try:
        names = {row[1] for row in cx.execute("PRAGMA table_info(builds)")}
    except sqlite3.Error:
        names = set()
    if not names:
        return
    for column, kind in _BUILD_COLUMNS:
        if column not in names:
            cx.execute(f"ALTER TABLE builds ADD COLUMN {column} {kind}")


def builds_record_missions(cx: sqlite3.Connection) -> bool:
    """Does this ledger have ``builds.mission_id`` and ``builds.mission_role``?"""
    try:
        names = {row[1] for row in cx.execute("PRAGMA table_info(builds)")}
    except sqlite3.Error:
        return False
    return {"mission_id", "mission_role"} <= names


def new_mission_id() -> str:
    """``m-`` and six lower-case hexadecimal digits."""
    return f"m-{secrets.token_hex(3)}"


def mission_branch(mission_id: str) -> str:
    """The mission's branch: ``mission/<mission id>``."""
    return f"mission/{mission_id}"


def attempt_correlation_id(mission_correlation_id: str, key: str, attempt: int) -> str:
    """``<mission correlation id>-<KEY>-a<attempt>``."""
    return f"{mission_correlation_id}-{key}-a{int(attempt)}"


# ---------------------------------------------------------------------------
# What the store hands back
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MissionFeature:
    """One feature of a mission, as the mission message gave it."""

    key: str
    sentence: str
    needs: tuple[str, ...] = ()
    no_sign_in: bool = False


@dataclass(frozen=True, slots=True)
class Mission:
    """One ``missions`` row."""

    id: str
    title: str
    target_repo: str
    branch: str
    status: str
    correlation_id: str
    originating_user: str
    created_at: str
    base_commit: str | None = None
    final_commit: str | None = None
    plan_request_id: str | None = None
    approved_at: str | None = None
    approved_by: str | None = None
    closed_at: str | None = None
    closed_reason: str | None = None

    @classmethod
    def from_row(cls, row: Any) -> "Mission":
        return cls(
            id=str(row[0]),
            title=str(row[1]),
            target_repo=str(row[2]),
            branch=str(row[3]),
            base_commit=row[4],
            final_commit=row[5],
            status=str(row[6]),
            correlation_id=str(row[7]),
            originating_user=str(row[8]),
            plan_request_id=row[9],
            created_at=str(row[10]),
            approved_at=row[11],
            approved_by=row[12],
            closed_at=row[13],
            closed_reason=row[14],
        )


@dataclass(frozen=True, slots=True)
class MissionAttempt:
    """One ``mission_attempts`` row."""

    mission_id: str
    key: str
    attempt: int
    correlation_id: str
    status: str
    reason: str | None = None
    admitted_at: str | None = None
    closed_at: str | None = None

    @classmethod
    def from_row(cls, row: Any) -> "MissionAttempt":
        return cls(
            mission_id=str(row[0]),
            key=str(row[1]),
            attempt=int(row[2]),
            correlation_id=str(row[3]),
            status=str(row[4]),
            reason=row[5],
            admitted_at=row[6],
            closed_at=row[7],
        )


@dataclass(frozen=True, slots=True)
class NeedsAnswer:
    """Where one attempt's needs have got to (:meth:`MissionStore.needs_resolution`).

    ``ready`` when every key it needs has a LANDED latest attempt; ``blocked_by``
    names the first key whose final attempt FAILED or was HELD (it will never
    land); otherwise it waits.
    """

    ready: bool
    blocked_by: str | None = None
    waiting_for: tuple[str, ...] = field(default=())


_MISSION_COLUMNS = (
    "id, title, target_repo, branch, base_commit, final_commit, status, "
    "correlation_id, originating_user, plan_request_id, created_at, "
    "approved_at, approved_by, closed_at, closed_reason"
)
_ATTEMPT_COLUMNS = (
    "mission_id, key, attempt, correlation_id, status, reason, admitted_at, closed_at"
)


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


class MissionStore:
    """Reads and writes the mission tables on the forge's own ledger connection.

    Every change is one ``BEGIN IMMEDIATE`` transaction (joining one the caller
    already opened), the way every ledger write is made. Changes are
    compare-and-swap on the state they expect, so a replay or a racing writer
    changes nothing and gets ``False``.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._cx = connection
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        ensure_tables(connection)

    @property
    def connection(self) -> sqlite3.Connection:
        return self._cx

    def _now(self) -> str:
        return self._clock().isoformat()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """``BEGIN IMMEDIATE`` … ``COMMIT``; joins a transaction already open."""
        if self._cx.in_transaction:
            yield self._cx
            return
        self._cx.execute("BEGIN IMMEDIATE")
        try:
            yield self._cx
        except BaseException:
            try:
                self._cx.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover - already rolled back
                pass
            raise
        else:
            self._cx.execute("COMMIT")

    # -- missions ---------------------------------------------------------

    def create_mission(
        self,
        *,
        title: str,
        target_repo: str,
        correlation_id: str,
        originating_user: str,
        features: Sequence[MissionFeature],
        mission_id: str | None = None,
    ) -> tuple[Mission, bool]:
        """File a PROPOSED mission with its features and needs.

        Idempotent on the mission message's correlation id: a redelivered
        message returns the mission already filed and ``False``. The caller
        validates first; this writes what it is given.
        """
        existing = self.get_by_correlation_id(correlation_id)
        if existing is not None:
            return existing, False
        with self.transaction() as cx:
            again = self.get_by_correlation_id(correlation_id)
            if again is not None:
                return again, False
            while True:
                chosen = mission_id or new_mission_id()
                taken = cx.execute(
                    "SELECT 1 FROM missions WHERE id = ? OR branch = ?",
                    (chosen, mission_branch(chosen)),
                ).fetchone()
                if taken is None:
                    break
                if mission_id is not None:
                    raise ValueError(f"mission id {mission_id} is already used")
            cx.execute(
                "INSERT INTO missions (id, title, target_repo, branch, status, "
                "correlation_id, originating_user, created_at) "
                "VALUES (?, ?, ?, ?, 'PROPOSED', ?, ?, ?)",
                (
                    chosen,
                    title,
                    target_repo,
                    mission_branch(chosen),
                    correlation_id,
                    originating_user,
                    self._now(),
                ),
            )
            for feature in features:
                cx.execute(
                    "INSERT INTO mission_features (mission_id, key, sentence, no_sign_in) "
                    "VALUES (?, ?, ?, ?)",
                    (chosen, feature.key, feature.sentence, 1 if feature.no_sign_in else 0),
                )
                for needed in feature.needs:
                    cx.execute(
                        "INSERT OR IGNORE INTO mission_needs (mission_id, key, needs_key) "
                        "VALUES (?, ?, ?)",
                        (chosen, feature.key, needed),
                    )
        made = self.get(chosen)
        assert made is not None
        return made, True

    def get(self, mission_id: str) -> Mission | None:
        row = self._cx.execute(
            f"SELECT {_MISSION_COLUMNS} FROM missions WHERE id = ?", (mission_id,)
        ).fetchone()
        return Mission.from_row(row) if row is not None else None

    def get_by_correlation_id(self, correlation_id: str) -> Mission | None:
        row = self._cx.execute(
            f"SELECT {_MISSION_COLUMNS} FROM missions WHERE correlation_id = ?",
            (correlation_id,),
        ).fetchone()
        return Mission.from_row(row) if row is not None else None

    def list_by_status(self, statuses: Iterable[str]) -> list[Mission]:
        """Missions in any of ``statuses``, oldest first."""
        wanted = tuple(statuses)
        if not wanted:
            return []
        marks = ", ".join("?" for _ in wanted)
        rows = self._cx.execute(
            f"SELECT {_MISSION_COLUMNS} FROM missions WHERE status IN ({marks}) "
            "ORDER BY created_at ASC, id ASC",
            wanted,
        ).fetchall()
        return [Mission.from_row(row) for row in rows]

    def list_active(self) -> list[Mission]:
        """Missions STARTING, RUNNING, READY or MERGING, oldest first."""
        return self.list_by_status(ACTIVE_MISSION_STATUSES)

    def features(self, mission_id: str) -> list[MissionFeature]:
        """The mission's features with their needs, in the order they were filed."""
        rows = self._cx.execute(
            "SELECT key, sentence, no_sign_in FROM mission_features "
            "WHERE mission_id = ? ORDER BY rowid ASC",
            (mission_id,),
        ).fetchall()
        needs: dict[str, list[str]] = {}
        for key, needed in self._cx.execute(
            "SELECT key, needs_key FROM mission_needs WHERE mission_id = ? "
            "ORDER BY rowid ASC",
            (mission_id,),
        ).fetchall():
            needs.setdefault(str(key), []).append(str(needed))
        return [
            MissionFeature(
                key=str(row[0]),
                sentence=str(row[1]),
                needs=tuple(needs.get(str(row[0]), ())),
                no_sign_in=bool(row[2]),
            )
            for row in rows
        ]

    def record_approval(
        self, mission_id: str, *, approved_by: str, plan_request_id: str
    ) -> bool:
        """Write who approved the plan card, when, and which request it was.

        Only on a PROPOSED mission with no approval yet: the first answer wins.
        The status is not changed here; the approval handler moves it next.
        """
        with self.transaction() as cx:
            cursor = cx.execute(
                "UPDATE missions SET approved_by = ?, approved_at = ?, plan_request_id = ? "
                "WHERE id = ? AND status = 'PROPOSED' AND approved_at IS NULL",
                (approved_by, self._now(), plan_request_id, mission_id),
            )
            return cursor.rowcount == 1

    def record_plan_request(self, mission_id: str, plan_request_id: str) -> None:
        """Remember the plan card's request id once it has been posted."""
        with self.transaction() as cx:
            cx.execute(
                "UPDATE missions SET plan_request_id = ? WHERE id = ? AND status = 'PROPOSED'",
                (plan_request_id, mission_id),
            )

    def set_status(
        self,
        mission_id: str,
        status: str,
        *,
        expected: Iterable[str],
        reason: str | None = None,
    ) -> bool:
        """Move the mission to ``status`` from one of ``expected``. False if not there.

        Entering a closing state (REJECTED, NOT_STARTED, MERGED, NOT_MERGED)
        writes ``closed_at`` and ``reason`` as ``closed_reason``.
        """
        if status not in MISSION_STATUSES:
            raise ValueError(f"not a mission status: {status!r}")
        allowed = tuple(expected)
        marks = ", ".join("?" for _ in allowed)
        with self.transaction() as cx:
            if status in _CLOSED_MISSION_STATUSES:
                cursor = cx.execute(
                    f"UPDATE missions SET status = ?, closed_at = ?, closed_reason = ? "
                    f"WHERE id = ? AND status IN ({marks})",
                    (status, self._now(), reason, mission_id, *allowed),
                )
            else:
                cursor = cx.execute(
                    f"UPDATE missions SET status = ? WHERE id = ? AND status IN ({marks})",
                    (status, mission_id, *allowed),
                )
            return cursor.rowcount == 1

    def set_base_commit(self, mission_id: str, commit: str) -> bool:
        """Store the main tip the branch is to be made at. Never moves a stored one."""
        with self.transaction() as cx:
            cursor = cx.execute(
                "UPDATE missions SET base_commit = ? WHERE id = ? "
                "AND (base_commit IS NULL OR base_commit = ?)",
                (commit, mission_id, commit),
            )
            return cursor.rowcount == 1

    def set_final_commit(self, mission_id: str, commit: str) -> bool:
        """Store the mission tip frozen at READY. Never moves a stored one."""
        with self.transaction() as cx:
            cursor = cx.execute(
                "UPDATE missions SET final_commit = ? WHERE id = ? "
                "AND (final_commit IS NULL OR final_commit = ?)",
                (commit, mission_id, commit),
            )
            return cursor.rowcount == 1

    # -- attempts ---------------------------------------------------------

    def file_first_attempts(self, mission_id: str) -> int:
        """File one WAITING attempt 1 per feature; returns how many were new.

        Idempotent by correlation id, so a restart that files again adds
        nothing.
        """
        mission = self.get(mission_id)
        if mission is None:
            raise ValueError(f"no mission {mission_id}")
        filed = 0
        with self.transaction() as cx:
            for feature in self.features(mission_id):
                cursor = cx.execute(
                    "INSERT OR IGNORE INTO mission_attempts "
                    "(mission_id, key, attempt, correlation_id, status) "
                    "VALUES (?, ?, 1, ?, 'WAITING')",
                    (
                        mission_id,
                        feature.key,
                        attempt_correlation_id(mission.correlation_id, feature.key, 1),
                    ),
                )
                filed += cursor.rowcount
        return filed

    def attempts(self, mission_id: str) -> list[MissionAttempt]:
        """Every attempt of the mission, in filing order (key order, then attempt)."""
        rows = self._cx.execute(
            f"SELECT {_ATTEMPT_COLUMNS} FROM mission_attempts WHERE mission_id = ? "
            "ORDER BY rowid ASC",
            (mission_id,),
        ).fetchall()
        return [MissionAttempt.from_row(row) for row in rows]

    def attempt_by_correlation_id(self, correlation_id: str) -> MissionAttempt | None:
        row = self._cx.execute(
            f"SELECT {_ATTEMPT_COLUMNS} FROM mission_attempts WHERE correlation_id = ?",
            (correlation_id,),
        ).fetchone()
        return MissionAttempt.from_row(row) if row is not None else None

    def mission_for_attempt(
        self, correlation_id: str
    ) -> tuple[Mission | None, MissionAttempt] | None:
        """``(mission, attempt)`` for an attempt's correlation id, or None.

        The mission is None when the attempt names a mission that is missing,
        which the planning start point treats as inconsistent metadata.
        """
        attempt = self.attempt_by_correlation_id(correlation_id)
        if attempt is None:
            return None
        return self.get(attempt.mission_id), attempt

    def latest_attempts(self, mission_id: str) -> dict[str, MissionAttempt]:
        """The latest attempt of every key that has one."""
        latest: dict[str, MissionAttempt] = {}
        for attempt in self.attempts(mission_id):
            held = latest.get(attempt.key)
            if held is None or attempt.attempt > held.attempt:
                latest[attempt.key] = attempt
        return latest

    def needs_resolution(self, mission_id: str, key: str) -> NeedsAnswer:
        """May ``key`` start now? Read from the latest attempt of each key it needs.

        A needed key whose latest attempt FAILED or was HELD is final: an
        attempt 1 that failed on a conflict always has its attempt 2 filed in
        the same transaction, so it is never the latest.
        """
        needs = next(
            (f.needs for f in self.features(mission_id) if f.key == key), ()
        )
        latest = self.latest_attempts(mission_id)
        waiting: list[str] = []
        for needed in needs:
            attempt = latest.get(needed)
            if attempt is not None and attempt.status in ("FAILED", "HELD"):
                return NeedsAnswer(ready=False, blocked_by=needed)
            if attempt is None or attempt.status != "LANDED":
                waiting.append(needed)
        return NeedsAnswer(ready=not waiting, waiting_for=tuple(waiting))

    def admit_attempt(
        self,
        correlation_id: str,
        *,
        max_in_flight: int | None = None,
        count_in_flight: Callable[[], int] | None = None,
    ) -> bool:
        """WAITING → ADMITTED, with the in-flight count taken in the same transaction.

        The same rule as ``WorkQueueStore.admit``: under ``BEGIN IMMEDIATE`` the
        count is read before the claim, so two admitters can never both take
        the last place. Refuses to join a transaction someone else opened.
        """
        if (max_in_flight is None) != (count_in_flight is None):
            raise ValueError("admit_attempt takes max_in_flight and count_in_flight together")
        if count_in_flight is not None and self._cx.in_transaction:
            raise RuntimeError(
                "admit_attempt with a count must open its own BEGIN IMMEDIATE transaction"
            )
        with self.transaction() as cx:
            if count_in_flight is not None and max_in_flight is not None:
                if count_in_flight() >= max_in_flight:
                    return False
            cursor = cx.execute(
                "UPDATE mission_attempts SET status = 'ADMITTED', admitted_at = ? "
                "WHERE correlation_id = ? AND status = 'WAITING'",
                (self._now(), correlation_id),
            )
            return cursor.rowcount == 1

    def requeue_attempt(self, correlation_id: str) -> bool:
        """ADMITTED → WAITING: the attempt's run was never created."""
        with self.transaction() as cx:
            cursor = cx.execute(
                "UPDATE mission_attempts SET status = 'WAITING', admitted_at = NULL "
                "WHERE correlation_id = ? AND status = 'ADMITTED'",
                (correlation_id,),
            )
            return cursor.rowcount == 1

    def _close_attempt(
        self, correlation_id: str, status: str, reason: str | None, expected: Sequence[str]
    ) -> bool:
        marks = ", ".join("?" for _ in expected)
        cursor = self._cx.execute(
            f"UPDATE mission_attempts SET status = ?, reason = ?, closed_at = ? "
            f"WHERE correlation_id = ? AND status IN ({marks})",
            (status, reason, self._now(), correlation_id, *expected),
        )
        return cursor.rowcount == 1

    def mark_landed(self, correlation_id: str, reason: str | None = None) -> bool:
        """ADMITTED → LANDED."""
        with self.transaction():
            return self._close_attempt(correlation_id, "LANDED", reason, ("ADMITTED",))

    def mark_failed(self, correlation_id: str, reason: str) -> bool:
        """WAITING or ADMITTED → FAILED, with the reason's first sentence.

        Idempotent: an attempt already closed is left as it is. This is also
        what the shared refusal recorder calls for a refusal before any build
        row exists (design section 2a, round 3 R12).
        """
        with self.transaction():
            return self._close_attempt(
                correlation_id, "FAILED", reason, ("WAITING", "ADMITTED")
            )

    def mark_held(self, correlation_id: str, reason: str) -> bool:
        """WAITING → HELD: something it needs will never land."""
        with self.transaction():
            return self._close_attempt(correlation_id, "HELD", reason, ("WAITING",))

    def fail_with_rebuild(self, correlation_id: str, reason: str = CONFLICT_REASON) -> bool:
        """Close attempt 1 FAILED on a conflict and file attempt 2 WAITING, together.

        One transaction, so the dependants never see a final failure while the
        rebuild is pending. Only an ADMITTED attempt 1 is closed this way; for
        attempt 2 (or a replay) it is :meth:`mark_failed` or nothing, and
        ``CHECK (attempt IN (1, 2))`` means a third attempt cannot be filed.
        """
        attempt = self.attempt_by_correlation_id(correlation_id)
        if attempt is None:
            return False
        if attempt.attempt != 1:
            return self.mark_failed(correlation_id, reason)
        mission = self.get(attempt.mission_id)
        if mission is None:
            return False
        with self.transaction() as cx:
            if not self._close_attempt(correlation_id, "FAILED", reason, ("ADMITTED",)):
                return False
            cx.execute(
                "INSERT OR IGNORE INTO mission_attempts "
                "(mission_id, key, attempt, correlation_id, status) "
                "VALUES (?, ?, 2, ?, 'WAITING')",
                (
                    attempt.mission_id,
                    attempt.key,
                    attempt_correlation_id(mission.correlation_id, attempt.key, 2),
                ),
            )
        return True

    def counts(self, mission_id: str) -> Mapping[str, int]:
        """How many keys' latest attempts are in each state."""
        tally: dict[str, int] = {status: 0 for status in ATTEMPT_STATUSES}
        for attempt in self.latest_attempts(mission_id).values():
            tally[attempt.status] = tally.get(attempt.status, 0) + 1
        return tally

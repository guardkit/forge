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
    "STOPPED_BEFORE_IT_STARTED",
    "STOPPED_REASON",
    "StopAnswer",
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
    "mission_membership",
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

#: The reasons a stop (section 11) closes attempts with: a WAITING one is
#: HELD, and an ADMITTED one that never got a run or a build is FAILED.
STOPPED_REASON: str = "mission stopped"
STOPPED_BEFORE_IT_STARTED: str = "mission stopped before it started"

#: The condition every statement that creates or re-admits work carries, so a
#: stop that commits first is always seen (section 11, step 1).
_NOT_STOPPED: str = (
    "NOT EXISTS (SELECT 1 FROM missions AS m WHERE m.id = mission_attempts.mission_id "
    "AND m.stop_requested_at IS NOT NULL)"
)

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
    "closed_at TEXT, closed_reason TEXT, "
    # Where the mission message came from, so every feature's planning run
    # answers in the same Slack thread the mission message started.
    "parent_request_id TEXT, originating_adapter TEXT, triggered_by TEXT, "
    # A stop asked for (section 11): when, and by whom. Added to an older
    # table by ensure_tables when missing.
    "stop_requested_at TEXT, stop_requested_by TEXT"
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

#: The two ``missions`` columns a stop request is written in (section 11,
#: 18 October 2026), added only when missing: no schema version bump, and an
#: older image ignores them.
_STOP_COLUMNS: tuple[tuple[str, str], ...] = (
    ("stop_requested_at", "TEXT"),
    ("stop_requested_by", "TEXT"),
)


def ensure_tables(cx: sqlite3.Connection) -> None:
    """Create the mission tables and the two ``builds`` columns. Idempotent.

    Called at the coordinator's boot (``bind_production_serve``) and by the
    estate rollout helper's copy of the boot steps. A ledger with no
    ``builds`` table yet (a bare test database) gets the tables only.
    """
    for statement in _TABLES_SQL:
        cx.execute(statement)
    have = {row[1] for row in cx.execute("PRAGMA table_info(missions)")}
    for column, kind in _STOP_COLUMNS:
        if column not in have:
            cx.execute(f"ALTER TABLE missions ADD COLUMN {column} {kind}")
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
    parent_request_id: str | None = None
    originating_adapter: str | None = None
    triggered_by: str | None = None
    stop_requested_at: str | None = None
    stop_requested_by: str | None = None

    @property
    def stopping(self) -> bool:
        """A stop has been asked for (section 11)."""
        return bool(self.stop_requested_at)

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
            parent_request_id=row[15],
            originating_adapter=row[16],
            triggered_by=row[17],
            stop_requested_at=row[18] if len(row) > 18 else None,
            stop_requested_by=row[19] if len(row) > 19 else None,
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
class StopAnswer:
    """What :meth:`MissionStore.request_stop` did, in one plain sentence."""

    accepted: bool
    sentence: str
    mission: "Mission | None" = None


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
    "approved_at, approved_by, closed_at, closed_reason, "
    "parent_request_id, originating_adapter, triggered_by, "
    "stop_requested_at, stop_requested_by"
)
_ATTEMPT_COLUMNS = (
    "mission_id, key, attempt, correlation_id, status, reason, admitted_at, closed_at"
)


def mission_membership(
    cx: Any, correlation_id: str | None
) -> tuple[Mission | None, MissionAttempt, MissionFeature | None] | None:
    """``(mission, attempt, feature)`` when ``correlation_id`` is a mission
    attempt's, else None. Read only; creates nothing.

    Membership is the attempt's correlation id in ``mission_attempts`` and
    nothing else — no queue row is involved (design round 3, R11). The mission
    or the feature is None when the attempt names one that is missing, which
    the caller treats as inconsistent metadata. A ledger without the mission
    tables, or anything that is not a ledger connection, has no members.
    """
    if not correlation_id or not isinstance(cx, sqlite3.Connection):
        return None
    try:
        row = cx.execute(
            f"SELECT {_ATTEMPT_COLUMNS} FROM mission_attempts WHERE correlation_id = ?",
            (correlation_id,),
        ).fetchone()
        if row is None:
            return None
        attempt = MissionAttempt.from_row(tuple(row))
        mission_row = cx.execute(
            f"SELECT {_MISSION_COLUMNS} FROM missions WHERE id = ?",
            (attempt.mission_id,),
        ).fetchone()
        feature_row = cx.execute(
            "SELECT key, sentence, no_sign_in FROM mission_features "
            "WHERE mission_id = ? AND key = ?",
            (attempt.mission_id, attempt.key),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return None
        raise
    mission = Mission.from_row(tuple(mission_row)) if mission_row is not None else None
    feature = (
        MissionFeature(
            key=str(feature_row[0]),
            sentence=str(feature_row[1]),
            no_sign_in=bool(feature_row[2]),
        )
        if feature_row is not None
        else None
    )
    return mission, attempt, feature


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
        parent_request_id: str | None = None,
        originating_adapter: str | None = None,
        triggered_by: str | None = None,
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
                "correlation_id, originating_user, created_at, parent_request_id, "
                "originating_adapter, triggered_by) "
                "VALUES (?, ?, ?, ?, 'PROPOSED', ?, ?, ?, ?, ?, ?)",
                (
                    chosen,
                    title,
                    target_repo,
                    mission_branch(chosen),
                    correlation_id,
                    originating_user,
                    self._now(),
                    parent_request_id,
                    originating_adapter,
                    triggered_by,
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
        """Write who approved the plan card, when, which request it was, and STARTING.

        One statement, so the approval and the move to STARTING land together:
        an approved mission is never left PROPOSED, where every maintenance
        check would read it as idle while its routing seed is being made
        (Codex implementation review, round 2, R7). Only on a PROPOSED mission
        with no approval yet: the first answer wins. The routing seed and the
        branch request come after, from STARTING.
        """
        with self.transaction() as cx:
            cursor = cx.execute(
                "UPDATE missions SET approved_by = ?, approved_at = ?, plan_request_id = ?, "
                "status = 'STARTING' "
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
        """Store the mission tip frozen at READY. Never moves a stored one.

        Never once a stop is asked for (section 11): the finisher's first
        committing step checks the stop in the same statement.
        """
        with self.transaction() as cx:
            cursor = cx.execute(
                "UPDATE missions SET final_commit = ? WHERE id = ? "
                "AND (final_commit IS NULL OR final_commit = ?) "
                "AND stop_requested_at IS NULL",
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
                f"WHERE correlation_id = ? AND status = 'WAITING' AND {_NOT_STOPPED}",
                (self._now(), correlation_id),
            )
            return cursor.rowcount == 1

    def requeue_attempt(self, correlation_id: str) -> bool:
        """ADMITTED → WAITING: the attempt's run was never created.

        Never once a stop is asked for (section 11): checked in the same
        statement, so a stop that commits first leaves the attempt to be
        closed as abandoned instead.
        """
        with self.transaction() as cx:
            cursor = cx.execute(
                "UPDATE mission_attempts SET status = 'WAITING', admitted_at = NULL "
                f"WHERE correlation_id = ? AND status = 'ADMITTED' AND {_NOT_STOPPED}",
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
            # A STOP ASKED FOR (section 11, Codex round 2 R5) still closes the
            # conflicted attempt, and files no attempt 2 — read in this same
            # transaction, so a stop that commits first is always seen.
            stopping = self._stop_requested(cx, attempt.mission_id)
            closing_reason = f"{reason}; mission stopped" if stopping else reason
            if not self._close_attempt(
                correlation_id, "FAILED", closing_reason, ("ADMITTED",)
            ):
                return False
            if stopping:
                return True
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

    # -- stopping (section 11, 18 October 2026) ----------------------------

    @staticmethod
    def _stop_requested(cx: sqlite3.Connection, mission_id: str) -> bool:
        row = cx.execute(
            "SELECT stop_requested_at FROM missions WHERE id = ?", (mission_id,)
        ).fetchone()
        return row is not None and row[0] is not None

    def list_stopping(self) -> list[Mission]:
        """Missions still active with a stop asked for, oldest first."""
        marks = ", ".join("?" for _ in ACTIVE_MISSION_STATUSES)
        rows = self._cx.execute(
            f"SELECT {_MISSION_COLUMNS} FROM missions WHERE status IN ({marks}) "
            "AND stop_requested_at IS NOT NULL ORDER BY created_at ASC, id ASC",
            ACTIVE_MISSION_STATUSES,
        ).fetchall()
        return [Mission.from_row(row) for row in rows]

    def start_running(self, mission_id: str) -> int | None:
        """STARTING → RUNNING and its WAITING attempts, in one transaction.

        The branch answer's step (section 11, Codex round 1 R3): the move is
        made only from STARTING and only with no stop asked for, in the same
        transaction as the attempts are filed, so a late "created" after a
        stop files nothing. Returns how many attempts were filed, or None when
        the mission was not moved (not STARTING, or stopping). Idempotent by
        correlation id.
        """
        mission = self.get(mission_id)
        if mission is None:
            raise ValueError(f"no mission {mission_id}")
        features = self.features(mission_id)
        with self.transaction() as cx:
            moved = cx.execute(
                "UPDATE missions SET status = 'RUNNING' WHERE id = ? "
                "AND status = 'STARTING' AND stop_requested_at IS NULL",
                (mission_id,),
            )
            if moved.rowcount != 1:
                return None
            filed = 0
            for feature in features:
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

    def hold_for_stop(self, mission_id: str) -> int:
        """Every WAITING attempt of a stopping mission → HELD ("mission stopped").

        One transaction; nothing when no stop is asked for. Returns how many.
        """
        with self.transaction() as cx:
            if not self._stop_requested(cx, mission_id):
                return 0
            cursor = cx.execute(
                "UPDATE mission_attempts SET status = 'HELD', reason = ?, closed_at = ? "
                "WHERE mission_id = ? AND status = 'WAITING'",
                (STOPPED_REASON, self._now(), mission_id),
            )
            return cursor.rowcount

    def close_abandoned(self, mission_id: str) -> int:
        """ADMITTED attempts of a stopping mission with no run and no build → FAILED.

        "mission stopped before it started", and never put back (section 11,
        R5). The run and the build are looked for in the same transaction.
        """
        with self.transaction() as cx:
            if not self._stop_requested(cx, mission_id):
                return 0
            cursor = cx.execute(
                "UPDATE mission_attempts SET status = 'FAILED', reason = ?, closed_at = ? "
                "WHERE mission_id = ? AND status = 'ADMITTED' "
                "AND NOT EXISTS (SELECT 1 FROM planning_runs AS p "
                "WHERE p.correlation_id = mission_attempts.correlation_id) "
                "AND NOT EXISTS (SELECT 1 FROM builds AS b "
                "WHERE b.correlation_id = mission_attempts.correlation_id)",
                (STOPPED_BEFORE_IT_STARTED, self._now(), mission_id),
            )
            return cursor.rowcount

    def close_drained(self, mission_id: str, *, active_build_states: Sequence[str]) -> str | None:
        """A stopping mission whose work has drained → NOT_MERGED. Its reason, or None.

        Drained (section 11, step 5): no attempt WAITING or ADMITTED, none of
        the mission's builds in ``active_build_states``, and none of their
        publication records unsettled (no result yet, or a lease held). All
        read in the one transaction that closes it, from whatever active state
        it is in. The reason is "stopped by <who> at <when>; <n> landed on the
        branch, <m> did not".
        """
        build_marks = ", ".join("?" for _ in active_build_states)
        with self.transaction() as cx:
            row = cx.execute(
                "SELECT status, stop_requested_at, stop_requested_by FROM missions "
                "WHERE id = ?",
                (mission_id,),
            ).fetchone()
            if row is None or row[1] is None or row[0] not in ACTIVE_MISSION_STATUSES:
                return None
            if row[0] == "MERGING":
                return None
            open_attempts = cx.execute(
                "SELECT COUNT(*) FROM mission_attempts WHERE mission_id = ? "
                "AND status IN ('WAITING', 'ADMITTED')",
                (mission_id,),
            ).fetchone()[0]
            if open_attempts:
                return None
            try:
                active = cx.execute(
                    f"SELECT COUNT(*) FROM builds WHERE mission_id = ? "
                    f"AND status IN ({build_marks})",
                    (mission_id, *active_build_states),
                ).fetchone()[0]
                unsettled = cx.execute(
                    "SELECT COUNT(*) FROM publication_records AS p "
                    "JOIN builds AS b ON b.build_id = p.build_id "
                    "WHERE b.mission_id = ? AND (p.result IS NULL "
                    "OR (p.lease_holder IS NOT NULL AND trim(p.lease_holder) <> ''))",
                    (mission_id,),
                ).fetchone()[0]
            except sqlite3.OperationalError as exc:
                if "no such" not in str(exc).lower():
                    raise
                active, unsettled = 0, 0
            if active or unsettled:
                return None
            latest = self.latest_attempts(mission_id)
            features = self.features(mission_id)
            landed = sum(
                1
                for f in features
                if f.key in latest and latest[f.key].status == "LANDED"
            )
            reason = (
                f"stopped by {row[2] or 'someone'} at {row[1]}; {landed} landed on "
                f"the branch, {len(features) - landed} did not"
            )
            cx.execute(
                "UPDATE missions SET status = 'NOT_MERGED', closed_at = ?, closed_reason = ? "
                "WHERE id = ? AND status = ?",
                (self._now(), reason, mission_id, row[0]),
            )
        return reason

    def request_stop(self, mission_id: str, *, requested_by: str) -> "StopAnswer":
        """Ask for a mission to stop. The one function Slack and the command line call.

        Section 11. One ``BEGIN IMMEDIATE`` transaction: the stop is written
        (``stop_requested_at``, ``stop_requested_by``) only for a mission in
        PROPOSED, STARTING, RUNNING, or READY with no merge build and no card
        offered. A PROPOSED mission is REJECTED at once, since nothing started.
        Everything else — holding what is waiting, letting running work end,
        closing the mission when it has drained — is done by the coordinator,
        in the places that already own each step. Refused, in one sentence:
        READY with a merge build or a card (decline the card instead), MERGING,
        and a mission already ended. Asking again is answered, not refused.
        """
        with self.transaction() as cx:
            row = cx.execute(
                f"SELECT {_MISSION_COLUMNS} FROM missions WHERE id = ?", (mission_id,)
            ).fetchone()
            if row is None:
                return StopAnswer(False, f"there is no mission {mission_id}")
            mission = Mission.from_row(row)
            if mission.status in ("REJECTED", "NOT_STARTED", "MERGED", "NOT_MERGED"):
                return StopAnswer(False, f"mission {mission_id} has already ended", mission)
            if mission.status == "MERGING":
                return StopAnswer(
                    False,
                    f"mission {mission_id}'s merge is running and can't be stopped now",
                    mission,
                )
            if self._has_merge_build(cx, mission_id):
                return StopAnswer(
                    False,
                    f"mission {mission_id} has its merge card: decline the merge card "
                    "instead",
                    mission,
                )
            if mission.stopping:
                return StopAnswer(
                    True,
                    f"a stop of mission {mission_id} was already asked for by "
                    f"{mission.stop_requested_by} at {mission.stop_requested_at}",
                    mission,
                )
            now = self._now()
            if mission.status == "PROPOSED":
                cx.execute(
                    "UPDATE missions SET stop_requested_at = ?, stop_requested_by = ?, "
                    "status = 'REJECTED', closed_at = ?, closed_reason = ? "
                    "WHERE id = ? AND status = 'PROPOSED'",
                    (now, requested_by, now, f"stopped by {requested_by} at {now} "
                     "before it started", mission_id),
                )
                return StopAnswer(
                    True,
                    f"mission {mission_id} is stopped: it had not started, so "
                    "nothing was built",
                    mission,
                )
            waiting = cx.execute(
                "SELECT COUNT(*) FROM mission_attempts WHERE mission_id = ? "
                "AND status = 'WAITING'",
                (mission_id,),
            ).fetchone()[0]
            cx.execute(
                "UPDATE missions SET stop_requested_at = ?, stop_requested_by = ? "
                "WHERE id = ? AND stop_requested_at IS NULL",
                (now, requested_by, mission_id),
            )
        features = "feature" if waiting == 1 else "features"
        return StopAnswer(
            True,
            f"stop requested; {waiting} waiting {features} won't start; running "
            "work will finish without merging, and work already merging into the "
            "mission branch will finish",
            mission,
        )

    @staticmethod
    def _has_merge_build(cx: sqlite3.Connection, mission_id: str) -> bool:
        try:
            row = cx.execute(
                "SELECT 1 FROM builds WHERE mission_id = ? AND mission_role = ? LIMIT 1",
                (mission_id, MISSION_ROLE_MERGE),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such" not in str(exc).lower():
                raise
            return False
        return row is not None

    def counts(self, mission_id: str) -> Mapping[str, int]:
        """How many keys' latest attempts are in each state."""
        tally: dict[str, int] = {status: 0 for status in ATTEMPT_STATUSES}
        for attempt in self.latest_attempts(mission_id).values():
            tally[attempt.status] = tally.get(attempt.status, 0) + 1
        return tally

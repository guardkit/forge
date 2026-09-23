"""The deployment lock, and the target's own counter (sections F and I).

One-true-copy design pass, 21 September 2026, item 1:

* third revision, **F** — *"the lock for a project's running product is held in
  the ledger, as one row per deployment target, taken in a transaction, with
  the holder's build, its turn number and an expiry … held from before reading
  what is running, through the ancestor rule of B, the project's deploy step,
  and the confirmation of what is now running. It is released only after that
  confirmation is recorded, or when it expires."*
* fifth revision, **I** — *"the target's deployment counter is separate and
  belongs to the deployment target … it goes up by one every time that lock is
  granted or taken over, by any build. Granting the lock records the counter
  together with the build it was granted to."*

WHY TWO COUNTERS. The fourth revision had the executor compare a *build's*
turn number across a deployment target, and that cannot work: build A is
picked up twice, so its own turn number is 3, and finishes; build B starts
normally at turn 1 of its own record and legitimately owns the target next.
Comparing 1 against 3 would refuse B. The two numbers count different things:
the build's turn counts takeovers of ONE BUILD'S RECORD, and the target's
counter counts grants of ONE TARGET'S LOCK. This module owns the second.

WHY THE LEDGER. The reservation this factory already has says of itself that
it is "correct within a single forge process". A lock that protects one
process cannot stop two coordinators, and cannot survive a restart — and the
whole point of the lock is to be the thing that decides who may ask when
something has gone wrong. So it is a row, taken in a transaction, with every
write conditional on the stored counter still equalling the writer's own, in
the same statement.

WHAT IS RUNNING lives here too, as R: the commit recorded at the last deploy
and the identity the running thing REPORTED when that deploy was confirmed.
Both are text. Nothing in this module knows what a project deploys, what an
identity is, what language anything is written in or where anything is hosted.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "DeploymentGrant",
    "DeploymentLockStore",
    "DeploymentTarget",
    "default_deploy_lease_seconds",
    "deployment_target_name",
]


def default_deploy_lease_seconds() -> int:
    """How long the deployment lock is held before it may be taken over.

    It has to be LONGER than the hard time limit the executor gives a deploy
    command (the design's H: "the executor gives every deploy command a hard
    time limit, and a worker's lease for the deploy step is always longer than
    that limit, so a healthy deploy is not taken over"). The executor's limit
    is its own setting; this is the number the press uses when nothing says
    otherwise, and the press checks the two against each other.
    """
    return 1800


def deployment_target_name(repo: str, env_id: str | None) -> str:
    """The name of a deployment target, as one piece of text.

    A project can have more than one thing it deploys to, so the name is the
    project and the environment the project's own profile declares. Central
    code composes it and never takes it apart; the executor treats it as an
    opaque key.
    """
    project = str(repo or "").strip() or "unnamed-project"
    where = str(env_id or "").strip()
    return f"{project}::{where}" if where else project


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


@dataclass(frozen=True)
class DeploymentTarget:
    """One deployment target's row, or the honest absence of one."""

    target: str
    recorded: bool = False
    counter: int = 0
    holder_build: str | None = None
    holder_turn: int | None = None
    holder_name: str | None = None
    granted_at: str | None = None
    expires_at: str | None = None
    #: R — what is running, as far as anybody here knows.
    running_commit: str | None = None
    running_identity: str | None = None
    running_build: str | None = None
    running_at: str | None = None

    def held_now(self, now: datetime) -> bool:
        if not self.holder_build or not self.expires_at:
            return False
        expiry = _parse_time(self.expires_at)
        if expiry is None:
            return False
        return expiry > now

    @property
    def nothing_is_running(self) -> bool:
        return not self.running_commit


@dataclass(frozen=True)
class DeploymentGrant:
    """The lock was granted: the target's counter, and when it runs out.

    ``counter`` is the number everything downstream is enforced on. It is
    recorded here together with the build it was granted to, and the executor
    refuses a lower one, refuses an equal one from a DIFFERENT build, and
    accepts a higher one under the stop-and-confirm rule.
    """

    target: str
    counter: int
    build_id: str
    turn: int
    holder: str
    expires_at: str
    took_over_from: str | None = None
    #: R at the moment of the grant, so the holder never has to read it twice.
    running_commit: str | None = None
    running_identity: str | None = None


class DeploymentLockStore:
    """Read and take the deployment lock, one target at a time.

    Every write carries the counter the writer was granted and is refused —
    as a plain ``False``, never an exception — when the target has moved on.
    The condition is in the same statement as the write, so a holder that has
    been taken over has no window to act in.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._cx = connection

    @contextmanager
    def _transaction(self):
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

    def read(self, target: str) -> DeploymentTarget:
        """This target's row, or ``recorded=False`` when nothing was deployed."""
        try:
            row = self._cx.execute(
                """
                SELECT target, counter, holder_build, holder_turn, holder_name,
                       granted_at, expires_at, running_commit, running_identity,
                       running_build, running_at
                  FROM deployment_targets
                 WHERE target = ?
                """,
                (str(target),),
            ).fetchone()
        except sqlite3.Error as exc:
            logger.warning(
                "deployment lock: %s could not be read (%s) — reading it as "
                "not recorded",
                target,
                exc,
            )
            return DeploymentTarget(target=str(target), recorded=False)
        if row is None:
            return DeploymentTarget(target=str(target), recorded=False)
        values = list(row)
        return DeploymentTarget(
            target=str(values[0]),
            recorded=True,
            counter=int(values[1] or 0),
            holder_build=_as_text(values[2]),
            holder_turn=int(values[3]) if values[3] is not None else None,
            holder_name=_as_text(values[4]),
            granted_at=_as_text(values[5]),
            expires_at=_as_text(values[6]),
            running_commit=_as_text(values[7]),
            running_identity=_as_text(values[8]),
            running_build=_as_text(values[9]),
            running_at=_as_text(values[10]),
        )

    # -- taking it ---------------------------------------------------------

    def grant(
        self,
        *,
        target: str,
        build_id: str,
        turn: int,
        holder: str,
        now: datetime,
        seconds: int | None = None,
    ) -> DeploymentGrant | None:
        """Take the lock, or answer ``None`` because somebody holds it.

        One transaction, and the TARGET'S counter goes up by one inside it —
        on a first grant and on a takeover alike, by any build. A live lock is
        left alone; one that has expired may be taken over, and the takeover
        cancels the previous holder because the counter it was granted is no
        longer the target's.

        The SAME build asking again while it still holds the lock is a fresh
        grant with a new counter, not a refusal: it has not been replaced, and
        raising the counter costs nothing because the executor is told the new
        one. That keeps this method total — a press that lost its own answer
        can ask again — without ever letting two builds hold one target.
        """
        ttl = int(seconds if seconds is not None else default_deploy_lease_seconds())
        expires = (now + timedelta(seconds=ttl)).isoformat()
        stamp = now.isoformat()
        try:
            with self._transaction():
                row = self._cx.execute(
                    """
                    SELECT counter, holder_build, expires_at, running_commit,
                           running_identity
                      FROM deployment_targets
                     WHERE target = ?
                    """,
                    (str(target),),
                ).fetchone()
                if row is None:
                    self._cx.execute(
                        """
                        INSERT INTO deployment_targets (
                            target, counter, holder_build, holder_turn,
                            holder_name, granted_at, expires_at,
                            created_at, updated_at
                        ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(target),
                            str(build_id),
                            int(turn),
                            str(holder),
                            stamp,
                            expires,
                            stamp,
                            stamp,
                        ),
                    )
                    return DeploymentGrant(
                        target=str(target),
                        counter=1,
                        build_id=str(build_id),
                        turn=int(turn),
                        holder=str(holder),
                        expires_at=expires,
                    )
                current_counter = int(row[0] or 0)
                current_build = _as_text(row[1])
                expiry = _parse_time(row[2])
                held_by_another = (
                    current_build is not None
                    and current_build != str(build_id)
                    and expiry is not None
                    and expiry > now
                )
                if held_by_another:
                    return None
                changed = self._cx.execute(
                    """
                    UPDATE deployment_targets
                       SET counter = counter + 1,
                           holder_build = ?,
                           holder_turn = ?,
                           holder_name = ?,
                           granted_at = ?,
                           expires_at = ?,
                           updated_at = ?
                     WHERE target = ? AND counter = ?
                    """,
                    (
                        str(build_id),
                        int(turn),
                        str(holder),
                        stamp,
                        expires,
                        stamp,
                        str(target),
                        current_counter,
                    ),
                ).rowcount
                if changed != 1:
                    return None
                return DeploymentGrant(
                    target=str(target),
                    counter=current_counter + 1,
                    build_id=str(build_id),
                    turn=int(turn),
                    holder=str(holder),
                    expires_at=expires,
                    took_over_from=(
                        current_build if current_build != str(build_id) else None
                    ),
                    running_commit=_as_text(row[3]),
                    running_identity=_as_text(row[4]),
                )
        except sqlite3.Error as exc:
            logger.warning(
                "deployment lock: %s could not be taken (%s)", target, exc
            )
            return None

    # -- writing (always against the counter this holder was granted) -------

    def _write(
        self, *, target: str, counter: int, now: datetime, assignments: dict[str, Any]
    ) -> bool:
        sets = dict(assignments)
        sets["updated_at"] = now.isoformat()
        columns = ", ".join(f"{name} = ?" for name in sets)
        values = list(sets.values()) + [str(target), int(counter)]
        try:
            with self._transaction():
                changed = self._cx.execute(
                    f"""
                    UPDATE deployment_targets
                       SET {columns}
                     WHERE target = ? AND counter = ?
                    """,
                    values,
                ).rowcount
        except sqlite3.Error as exc:
            logger.warning(
                "deployment lock: %s could not be written (%s)", target, exc
            )
            return False
        if changed != 1:
            logger.warning(
                "deployment lock: the write to %s on counter %s changed no row "
                "— this holder has been taken over and stops here",
                target,
                counter,
            )
            return False
        return True

    def renew(
        self,
        *,
        target: str,
        counter: int,
        now: datetime,
        seconds: int | None = None,
    ) -> bool:
        """Push this holder's lock out. Renewing does NOT change the counter."""
        ttl = int(seconds if seconds is not None else default_deploy_lease_seconds())
        return self._write(
            target=target,
            counter=counter,
            now=now,
            assignments={"expires_at": (now + timedelta(seconds=ttl)).isoformat()},
        )

    def record_running(
        self,
        *,
        target: str,
        counter: int,
        now: datetime,
        commit: str,
        identity: str,
        build_id: str,
    ) -> bool:
        """Write down R — what is now running — against this holder's counter.

        Called only after the deploy step has returned AND the identity it
        reported has been compared with the identity it was handed. It is the
        confirmation the design says the lock is held until.
        """
        return self._write(
            target=target,
            counter=counter,
            now=now,
            assignments={
                "running_commit": str(commit),
                "running_identity": str(identity),
                "running_build": str(build_id),
                "running_at": now.isoformat(),
            },
        )

    def release(self, *, target: str, counter: int, now: datetime) -> bool:
        """Put the lock down. Refused if this holder was taken over.

        The counter is NOT lowered: it is the target's own count of grants and
        goes only one way. What is released is the holder and the expiry, so
        the next build takes it without waiting the lease out.
        """
        return self._write(
            target=target,
            counter=counter,
            now=now,
            assignments={
                "holder_build": None,
                "holder_turn": None,
                "holder_name": None,
                "expires_at": None,
            },
        )

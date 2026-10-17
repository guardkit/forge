"""Which builds belong to a mission, read off the ``builds`` row (17 October 2026).

Missions design, sections 6 and 7. A mission's feature builds carry
``builds.mission_id`` and ``builds.mission_role = 'feature'`` (copied at the
one build INSERT); the mission's own merge into main is one more row of its
own with ``mission_role = 'mission-merge'``. These small readers are what the
merge offer, the press and the two repair producers ask, so each of them
reads the same two columns the same way.

Every reader answers ``None`` (or ``False``) on a ledger without the two
columns, so nothing here can make an older ledger fail.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from typing import Any

from forge.lifecycle.missions import (
    MISSION_ROLE_FEATURE,
    MISSION_ROLE_MERGE,
    builds_record_missions,
)

__all__ = [
    "BuildMission",
    "is_mission_build",
    "landing_attempt_id",
    "mission_join_name",
    "mission_merge_build_id",
    "mission_of_build",
]


@dataclass(frozen=True, slots=True)
class BuildMission:
    """The mission a build belongs to, and the part it plays in it."""

    mission_id: str
    role: str

    @property
    def is_feature(self) -> bool:
        return self.role == MISSION_ROLE_FEATURE

    @property
    def is_merge(self) -> bool:
        return self.role == MISSION_ROLE_MERGE


def _connection(pool_or_connection: Any) -> sqlite3.Connection | None:
    if isinstance(pool_or_connection, sqlite3.Connection):
        return pool_or_connection
    cx = getattr(pool_or_connection, "connection", None)
    return cx if isinstance(cx, sqlite3.Connection) else None


def mission_of_build(pool_or_connection: Any, build_id: str) -> BuildMission | None:
    """``BuildMission`` for a build that belongs to a mission, else ``None``.

    Never raises: a ledger without the columns, a pool with no connection, or
    a read that fails all answer ``None`` — the build is then treated exactly
    as every build before missions was.
    """
    cx = _connection(pool_or_connection)
    if cx is None or not build_id:
        return None
    try:
        if not builds_record_missions(cx):
            return None
        row = cx.execute(
            "SELECT mission_id, mission_role FROM builds WHERE build_id = ?",
            (str(build_id),),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None or not row[0] or not row[1]:
        return None
    return BuildMission(mission_id=str(row[0]), role=str(row[1]))


def is_mission_build(pool_or_connection: Any, build_id: str) -> bool:
    """Does this build belong to a mission (either role)?"""
    return mission_of_build(pool_or_connection, build_id) is not None


def mission_join_name(mission_id: str) -> str:
    """The integration name the mission's merge into main joins under.

    ``mission-<id>``, so its folders and branches are
    ``factory-integration/mission-<id>[-attempt-N]`` and can never meet the
    last feature's own ``factory-integration/<FEAT>`` (design round 2, R1).
    """
    return f"mission-{mission_id}"


def landing_attempt_id(build_id: str) -> str:
    """The execution attempt id of a mission feature's landing, from the build.

    Thirty-two lower-case hexadecimal digits, the shape every reader of the
    decision row checks (``lifecycle/feature_routing.py``), derived from the
    build id alone so a landing picked up after a restart carries the same
    attempt as the one that started.
    """
    digest = hashlib.sha256(f"mission-landing:{build_id}".encode("utf-8"))
    return digest.hexdigest()[:32]


def mission_merge_build_id(pool_or_connection: Any, mission_id: str) -> str | None:
    """The mission's own merge build, when it has been recorded."""
    cx = _connection(pool_or_connection)
    if cx is None:
        return None
    try:
        if not builds_record_missions(cx):
            return None
        row = cx.execute(
            "SELECT build_id FROM builds WHERE mission_id = ? AND mission_role = ? "
            "ORDER BY queued_at ASC LIMIT 1",
            (str(mission_id), MISSION_ROLE_MERGE),
        ).fetchone()
    except sqlite3.Error:
        return None
    return str(row[0]) if row is not None else None

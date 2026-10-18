"""Is anything unfinished for a sandbox's repositories? Read from the ledger.

Design section 2.3a (18 October 2026, Codex S2 rounds 1 to 4): a managed
sandbox may be stopped only when, for every repository mapped to it, the
ledger shows

- no work-queue row, planning run, build or mission in any non-final state,
  paused and interrupted ones included (boot recovery would resume them);
- no merge card without a decision row, of any age: a card past the queue's
  hold still counts, because only an answer closes it;
- no preview lease in ``preparing``, ``ready`` or ``ending``;
- no unsettled publication: a record whose result is not written yet, or whose
  lease holder is still set (even past its expiry, even with a result) — the
  same rule the estate's own idle check uses;
- no deployment lock held: a deployment target with a holder.

:func:`what_keeps_it_up` answers with the first thing found, in words, or
``None`` when nothing is unfinished. It reads, never writes, and a table a
ledger does not have yet holds nothing.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence

__all__ = ["what_keeps_it_up", "repositories_with_unfinished_work"]

#: The states that end a work-queue row.
_QUEUE_OPEN = ("QUEUED", "ADMITTED")
#: The states that end a planning run (``BUILD_QUEUED`` hands over to a build).
_PLANNING_FINAL = ("FAILED", "CANCELLED", "TIMED_OUT", "PLANNED_HANDOFF", "BUILD_QUEUED")
#: The states that end a build. ``INTERRUPTED`` is not one: boot resumes it.
_BUILD_FINAL = ("COMPLETE", "FAILED", "CANCELLED", "SKIPPED")
#: The states that end a mission.
_MISSION_FINAL = ("REJECTED", "NOT_STARTED", "MERGED", "NOT_MERGED")
#: The preview lease states that are still up, or still being taken down.
_PREVIEW_LIVE = ("preparing", "ready", "ending")

_MERGE_OFFER_ROW = "merge_deploy_offer"
_MERGE_DECISION_ROW = "merge_deploy_decision"


def _marks(values: Sequence[object]) -> str:
    return ", ".join("?" for _ in values)


def _first(connection: sqlite3.Connection, sql: str, params: Sequence[object]) -> sqlite3.Row | tuple | None:
    try:
        return connection.execute(sql, tuple(params)).fetchone()
    except sqlite3.OperationalError as exc:
        text = str(exc).lower()
        if "no such table" in text or "no such column" in text:
            return None
        raise


def _target_clause(repos: Sequence[str]) -> tuple[str, list[object]]:
    """``deployment_targets.target`` is the repository, or ``<repo>::<env>``."""
    parts: list[str] = []
    params: list[object] = []
    for repo in repos:
        parts.append("(target = ? OR substr(target, 1, ?) = ?)")
        params.extend([repo, len(repo) + 2, f"{repo}::"])
    return " OR ".join(parts), params


def what_keeps_it_up(connection: sqlite3.Connection, repos: Iterable[str]) -> str | None:
    """The first unfinished thing for any of ``repos``, in words; ``None`` if idle."""
    names = sorted({str(r).strip() for r in repos if str(r or "").strip()})
    if not names:
        return None
    rm = _marks(names)

    row = _first(
        connection,
        f"SELECT id, target_repo, status FROM work_queue WHERE target_repo IN ({rm}) "
        f"AND status IN ({_marks(_QUEUE_OPEN)}) ORDER BY id LIMIT 1",
        [*names, *_QUEUE_OPEN],
    )
    if row is not None:
        return f"queue row #{row[0]} for {row[1]} is {str(row[2]).lower()}"

    row = _first(
        connection,
        f"SELECT correlation_id, target_repo, state FROM planning_runs "
        f"WHERE target_repo IN ({rm}) AND state NOT IN ({_marks(_PLANNING_FINAL)}) LIMIT 1",
        [*names, *_PLANNING_FINAL],
    )
    if row is not None:
        return f"a planning run for {row[1]} is {str(row[2]).lower()}"

    row = _first(
        connection,
        f"SELECT build_id, repo, status FROM builds WHERE repo IN ({rm}) "
        f"AND status NOT IN ({_marks(_BUILD_FINAL)}) LIMIT 1",
        [*names, *_BUILD_FINAL],
    )
    if row is not None:
        return f"build {row[0]} for {row[1]} is {str(row[2]).lower()}"

    row = _first(
        connection,
        f"SELECT id, target_repo, status FROM missions WHERE target_repo IN ({rm}) "
        f"AND status NOT IN ({_marks(_MISSION_FINAL)}) LIMIT 1",
        [*names, *_MISSION_FINAL],
    )
    if row is not None:
        return f"mission {row[0]} for {row[1]} is {str(row[2]).lower()}"

    row = _first(
        connection,
        f"""
        SELECT offer.build_id, builds.repo FROM stage_log AS offer
          JOIN builds ON builds.build_id = offer.build_id
         WHERE offer.target_identifier = ? AND builds.repo IN ({rm})
           AND NOT EXISTS (
               SELECT 1 FROM stage_log AS decision
                WHERE decision.build_id = offer.build_id
                  AND decision.target_identifier = ?
           )
         LIMIT 1
        """,
        [_MERGE_OFFER_ROW, *names, _MERGE_DECISION_ROW],
    )
    if row is not None:
        return f"the merge card for build {row[0]} ({row[1]}) has not been answered"

    row = _first(
        connection,
        f"SELECT lease_id, project, state FROM preview_leases WHERE project IN ({rm}) "
        f"AND state IN ({_marks(_PREVIEW_LIVE)}) LIMIT 1",
        [*names, *_PREVIEW_LIVE],
    )
    if row is not None:
        return f"a preview for {row[1]} is {row[2]}"

    row = _first(
        connection,
        f"""
        SELECT build_id, repo FROM publication_records
         WHERE repo IN ({rm})
           AND (result IS NULL
                OR (lease_holder IS NOT NULL AND trim(lease_holder) <> ''))
         LIMIT 1
        """,
        names,
    )
    if row is not None:
        return f"the publication of build {row[0]} ({row[1]}) has not settled"

    clause, params = _target_clause(names)
    row = _first(
        connection,
        f"SELECT target, holder_build FROM deployment_targets WHERE ({clause}) "
        "AND holder_build IS NOT NULL AND trim(holder_build) <> '' LIMIT 1",
        params,
    )
    if row is not None:
        return f"the deployment lock on {row[0]} is held by build {row[1]}"
    return None


def repositories_with_unfinished_work(
    connection: sqlite3.Connection, repos: Iterable[str]
) -> list[str]:
    """Those of ``repos`` the ledger shows anything unfinished for (boot uses it)."""
    return [repo for repo in sorted(set(repos)) if what_keeps_it_up(connection, [repo])]

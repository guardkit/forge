"""Finishing a mission: one merge card for the whole branch (17 October 2026).

Missions design, section 7. The mission step in the work queue's tick moves a
mission to READY once nothing more can run; everything after READY is here:

1. **Nothing landed:** the mission is NOT_MERGED, one line says so in its
   thread, and there is no card.
2. **The tip is checked:** the mission branch is fetched and its tip ``T``
   must be the last confirmed landing ``J`` (from the feature builds'
   publication records). Anything else changed the branch, so it is
   NOT_MERGED.
3. **The routing receipt** of the mission's own correlation id must exist when
   the routing gate is configured. Missing is NOT_MERGED and never retried.
4. ``T`` is stored as ``final_commit``, and the mission gets one new
   ``builds`` row of its own (``mission_role = 'mission-merge'``), written in
   one transaction by ``record_mission_merge_build``.
5. **The frozen source:** ``forge/source/<build_id>`` is made at ``T`` in the
   clone the press works in, by the rule of the prepared route's
   ``_make_source_branch`` (local commits only, ``branch --force``), and read
   back before anything is offered. A restart puts it back at ``final_commit``.
6. **The card** is the ordinary merge offer for that build, with the mission's
   own words and its list of files to look at first.
7. **The answer:** approve is the ordinary press (deploy on, preview pin on,
   integration names ``mission-<id>``): READY → MERGING → MERGED. Reject, or a
   press that did not reach main, is NOT_MERGED, and the branch is kept.

The work queue's tick calls :func:`run_finishing_step`, one line, right after
its own mission step. What that runs is installed at boot with
:func:`install_finisher`; until then it does nothing. Each mission is finished
in a task of its own, because preparing a preview can take minutes and the
queue must not wait for it.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from forge.lifecycle.mission_builds import mission_merge_build_id
from forge.lifecycle.missions import (
    MISSION_ROLE_FEATURE,
    Mission,
    MissionAttempt,
    MissionFeature,
    MissionStore,
)

logger = logging.getLogger(__name__)

__all__ = [
    "MissionFinishDeps",
    "RISKY_WORDS",
    "compose_mission_finisher",
    "finish_ready_mission",
    "finish_ready_missions",
    "install_finisher",
    "make_frozen_source",
    "mission_card_words",
    "risky_files",
    "run_finishing_step",
]

#: The words that put a changed file on the "look at these first" list
#: (design section 7): a path segment or file name containing one of them,
#: case ignored. ``.sql`` files are on the list whatever they are called.
RISKY_WORDS: tuple[str, ...] = (
    "auth",
    "login",
    "session",
    "security",
    "secret",
    "token",
    "password",
    "keycloak",
    "oauth",
    "permission",
    "migration",
)

#: The mission states this step acts on.
_FINISHING_STATES: tuple[str, ...] = ("READY", "MERGING")

#: Merge report words for a deploy that failed after the mission reached main.
_DEPLOY_FAILED_WORDS: frozenset[str] = frozenset(
    {"merged-deploy-failed", "merged-deploy-reverted"}
)

#: A mission's card that keeps being refused for reasons that may pass (a
#: preview service that raises, a commit that cannot be read) is tried at most
#: once every OFFER_RETRY_SECONDS, and given up on (NOT_MERGED, the branch
#: kept) once OFFER_ATTEMPTS failed offers have been made over at least
#: OFFER_GIVE_UP_SECONDS. Each failed offer is one durable stage row on the
#: mission's merge build, so a restart does not start the count again.
OFFER_ATTEMPTS: int = 6
OFFER_RETRY_SECONDS: int = 300
OFFER_GIVE_UP_SECONDS: int = 1800
MISSION_OFFER_ATTEMPT_TARGET_IDENTIFIER: str = "mission_offer_attempt"

#: How long after an approved decision the finishing step waits before it
#: treats a mission merge with no report and no live lease as stopped: the
#: approved press takes its lease within moments of the decision.
MERGE_RESUME_GRACE_SECONDS: int = 120

#: How many files the card names before it says "and N more".
_RISKY_SHOWN: int = 12


def risky_files(paths: Iterable[str]) -> list[str]:
    """The changed files to look at first: sign-in, security, database changes.

    A list to look at, never a gate. A path is on it when any of its segments
    (folders or the file name) contains one of :data:`RISKY_WORDS`, ignoring
    case, or when it is a ``.sql`` file. Order is kept; duplicates are not.
    """
    found: list[str] = []
    for raw in paths:
        path = str(raw or "").strip()
        if not path or path in found:
            continue
        lowered = path.lower()
        segments = [s for s in re.split(r"[\\/]", lowered) if s]
        if lowered.endswith(".sql") or any(
            word in segment for segment in segments for word in RISKY_WORDS
        ):
            found.append(path)
    return found


def _first_sentence(text: str | None, limit: int = 160) -> str:
    """One readable sentence, on one line, trimmed."""
    flat = " ".join(str(text or "").split())
    if not flat:
        return ""
    match = re.search(r"(?<=[.!?])\s", flat)
    sentence = flat[: match.start()] if match else flat
    if len(sentence) > limit:
        sentence = sentence[: limit - 1].rstrip() + "…"
    return sentence


def _label(sentence: str, limit: int = 40) -> str:
    """A feature's sentence, short enough to sit on one card line."""
    flat = " ".join(str(sentence or "").split())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit].rsplit(" ", 1)[0] or flat[:limit]
    return cut.rstrip(" ,.;:") + "…"


def mission_card_words(
    *,
    mission: Mission,
    features: Sequence[MissionFeature],
    latest: Mapping[str, MissionAttempt],
    risky: Sequence[str] | None,
    risky_unread: str | None = None,
) -> str:
    """The mission merge card's own sentences (design section 7).

    The offer adds the preview line and the merge-check line under these,
    as it does for every card.
    """
    landed = [f for f in features if _status(latest, f.key) == "LANDED"]
    not_landed = [f for f in features if _status(latest, f.key) != "LANDED"]
    lines = [
        f'Mission "{mission.title}" — {len(landed)} of {len(features)} '
        f"features landed on {mission.branch}"
    ]
    lines.append(
        "Landed:  " + " · ".join(f"{f.key} {_label(f.sentence)}" for f in landed)
    )
    if not_landed:
        parts = []
        for feature in not_landed:
            attempt = latest.get(feature.key)
            why = _first_sentence(getattr(attempt, "reason", None)) or (
                "it never started"
                if attempt is None
                else f"it ended {str(attempt.status).lower()}"
            )
            parts.append(f'{feature.key} {_label(feature.sentence)} — "{why}"')
        lines.append("Not landed:  " + " · ".join(parts))
    if risky_unread:
        lines.append(
            "Look at these first: the changed files could not be listed "
            f"({_first_sentence(risky_unread)})."
        )
    elif risky:
        shown = list(risky[:_RISKY_SHOWN])
        more = len(risky) - len(shown)
        lines.append("Look at these first (sign-in, security, database changes):")
        lines.append(
            "  " + " · ".join(shown) + (f" and {more} more" if more > 0 else "")
        )
    else:
        lines.append(
            "Look at these first: no changed file touches sign-in, security or "
            "the database."
        )
    lines.append(
        "Approve = merge the mission into main and deploy. Reject = nothing "
        "reaches main; the branch is kept."
    )
    return "\n".join(lines)


def _status(latest: Mapping[str, MissionAttempt], key: str) -> str | None:
    attempt = latest.get(key)
    return attempt.status if attempt is not None else None


# ---------------------------------------------------------------------------
# What the step is given
# ---------------------------------------------------------------------------


@dataclass
class MissionFinishDeps:
    """Everything finishing needs, composed once at boot.

    ``offer`` is :meth:`forge.pipeline.merge_offer.MergeOfferService.offer`.
    ``git_for`` is where this repository's git happens (the press's own
    venue). ``tree_reader_for`` reads a commit's file listing through the same
    venue the merge card's merge-check reading uses; ``None`` lists nothing,
    and the card says the files could not be listed. ``notify`` says one line
    in the mission's thread: ``notify(mission, message)``.
    """

    pool: Any
    config: Any
    offer: Callable[..., Awaitable[bool]]
    git_for: Callable[[str, Path], Any]
    notify: Callable[[Mission, str], Awaitable[None]]
    tree_reader_for: Callable[[str, Path], Any] | None = None
    #: ``MergeApprovalConsumer.land_mission_feature``: a landing that was
    #: started and never reported (the coordinator stopped part-way) is
    #: carried on with it. ``None`` resumes nothing.
    land: Callable[[str], Awaitable[Any]] | None = None
    #: ``MergeApprovalConsumer.resume_mission_merge``: an approved mission
    #: merge into main that never reported (the coordinator stopped after
    #: the decision) is pressed again through the approved press's own locked
    #: entry. ``None`` resumes nothing.
    resume_merge: Callable[[str], Awaitable[Any]] | None = None
    #: ``MergeApprovalConsumer.reconcile_mission_landing``: a reported landing
    #: whose publication record never settled is settled by looking at the
    #: remote (section 11). ``None`` settles nothing.
    reconcile: Callable[[str], Awaitable[Any]] | None = None
    #: ``MergeApprovalConsumer.finish_a_settled_landing``: a landing whose
    #: outcome is settled but whose report or release never happened (the
    #: press stopped after its result) is finished from the record (R2).
    #: ``None`` finishes nothing.
    finish_settled: Callable[[str], Awaitable[Any]] | None = None
    feature_routing_gate: Any | None = None
    feature_routing_required: bool = False
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# The tick's one line, and what boot installs behind it
# ---------------------------------------------------------------------------

_finisher: Callable[[], Awaitable[Any]] | None = None
_in_progress: set[str] = set()
_tasks: set[asyncio.Task[Any]] = set()
#: Landings being carried on right now, so one tick never starts a second.
_resuming: set[str] = set()

#: How long a mission feature build may sit COMPLETE with no decision row
#: before the finishing step lands it itself. The offer hook asks for the
#: landing within moments of the build finishing, and decides "failed tasks"
#: there, so the step only steps in for a hook that never ran (a restart).
LANDING_GRACE_SECONDS: int = 120


def install_finisher(finisher: Callable[[], Awaitable[Any]] | None) -> None:
    """What :func:`run_finishing_step` runs. ``None`` switches it off."""
    global _finisher
    _finisher = finisher


async def run_finishing_step() -> None:
    """The work queue tick's call. Does nothing until boot installs a finisher;
    never raises, so the queue never stops for a mission."""
    finisher = _finisher
    if finisher is None:
        return
    try:
        await finisher()
    except Exception as exc:  # noqa: BLE001 — the queue must keep ticking
        logger.error(
            "missions: the finishing step raised (%s: %s); the queue carries on",
            type(exc).__name__,
            exc,
        )


async def finish_ready_missions(
    deps: MissionFinishDeps, *, wait: bool = False
) -> list[str]:
    """Start finishing every READY or MERGING mission not already being finished,
    and carry on every mission landing that started and never reported.

    Each runs as a task of its own and the call returns at once, so a preview
    being prepared never holds the queue. ``wait=True`` (tests) waits for them.
    Returns the build ids resumed, then the mission ids started.
    """
    cx = getattr(deps.pool, "connection", None)
    if cx is None:
        return []
    try:
        missions = MissionStore(cx, clock=deps.clock).list_by_status(_FINISHING_STATES)
    except Exception as exc:  # noqa: BLE001 — a read never stops the queue
        logger.warning("missions: could not list missions to finish (%s)", exc)
        return []
    started: list[str] = []
    for build_id, decided in _landings_to_resume(deps, cx):
        _resuming.add(build_id)
        started.append(build_id)
        task = asyncio.create_task(_resume_guarded(deps, build_id, decided=decided))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    for build_id in _settled_landings_to_finish(deps, cx):
        _resuming.add(build_id)
        started.append(build_id)
        task = asyncio.create_task(_finish_settled_guarded(deps, build_id))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    for build_id in _landings_to_reconcile(deps, cx):
        _resuming.add(build_id)
        started.append(build_id)
        task = asyncio.create_task(_reconcile_guarded(deps, build_id))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    for mission in missions:
        if mission.stopping:
            continue  # a stopped mission is never offered; it drains
        if mission.id in _in_progress:
            continue
        _in_progress.add(mission.id)
        started.append(mission.id)
        task = asyncio.create_task(_finish_guarded(deps, mission.id))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    if wait:
        while _tasks:
            await asyncio.gather(*list(_tasks), return_exceptions=True)
    return started


def _landings_to_resume(deps: MissionFinishDeps, cx: Any) -> list[tuple[str, bool]]:
    """Mission features whose landing has not reported, and is not running.

    On every pass, so a restart carries every landing on: a COMPLETE feature
    build of a RUNNING mission, whose attempt is still ADMITTED, with no merge
    report and no live lease on its publication record, and either a
    decision row (the landing was asked for) or more than
    :data:`LANDING_GRACE_SECONDS` since it finished (the offer hook never ran).
    A build whose landing reported is never pressed again; one whose press
    keeps raising is given a FAILED landing report by the press's own bound.

    THE DECISION ROW, OR THE LANDING REQUEST ROW, IS THE ONLY DURABLE PROOF OF
    A CLEAN FINISH. The offer hook asks for the landing only after the build's
    own terminal event said no task failed, and the request row is written
    then, before the landing waits for the repository's lock (the decision is
    made inside it, section 11); the ledger keeps no failed-task count. So a
    build with either row is pressed, and one with neither (the hook never
    ran) is handed over with its count unknown, which gives it a FAILED
    landing report and no press.
    Answers ``(build id, has a decision row)``.
    """
    if deps.land is None:
        return []
    from forge.pipeline.merge_executor import (
        MERGE_DECISION_TARGET_IDENTIFIER,
        MERGE_REPORT_TARGET_IDENTIFIER,
        MISSION_LANDING_REQUEST_TARGET_IDENTIFIER,
    )
    from forge.pipeline.publication_record import PublicationRecordStore

    try:
        rows = cx.execute(
            "SELECT b.build_id, COALESCE(b.completed_at, b.queued_at), "
            "EXISTS (SELECT 1 FROM stage_log AS d WHERE d.build_id = b.build_id "
            "AND d.target_identifier IN (?, ?)) "
            "FROM builds AS b "
            "JOIN missions AS m ON m.id = b.mission_id "
            "JOIN mission_attempts AS a ON a.correlation_id = b.correlation_id "
            "WHERE b.mission_role = ? AND b.status = 'COMPLETE' "
            "AND m.status = 'RUNNING' AND a.status = 'ADMITTED' "
            "AND NOT EXISTS (SELECT 1 FROM stage_log AS r WHERE r.build_id = b.build_id "
            "AND r.target_identifier = ?) "
            "ORDER BY b.queued_at ASC",
            (
                MERGE_DECISION_TARGET_IDENTIFIER,
                MISSION_LANDING_REQUEST_TARGET_IDENTIFIER,
                MISSION_ROLE_FEATURE,
                MERGE_REPORT_TARGET_IDENTIFIER,
            ),
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 — a read never stops the queue
        logger.warning("missions: could not look for landings to resume (%s)", exc)
        return []
    records = PublicationRecordStore(cx)
    now = deps.clock()
    due: list[tuple[str, bool]] = []
    for build_id, finished_at, decided in rows:
        build_id = str(build_id)
        if build_id in _resuming:
            continue
        if not decided:
            try:
                finished = datetime.fromisoformat(str(finished_at))
            except (TypeError, ValueError):
                continue
            if finished.tzinfo is None:
                finished = finished.replace(tzinfo=timezone.utc)
            if (now - finished).total_seconds() < LANDING_GRACE_SECONDS:
                continue
        if records.read(build_id).lease_is_live(now):
            continue
        due.append((build_id, bool(decided)))
    return due


def _landings_to_reconcile(deps: MissionFinishDeps, cx: Any) -> list[str]:
    """Mission features with a landing report but an unsettled publication record.

    Section 11, R4: a press that reported but left its record with no result
    (it tried a send whose answer was lost) is re-checked against the remote
    by the press's own reconciliation, so the mission never drains, or
    counts a feature as not landed, on an outcome nobody knows. Only for
    missions still active, and never while a press holds the record's lease.
    """
    if deps.reconcile is None:
        return []
    from forge.lifecycle.missions import ACTIVE_MISSION_STATUSES
    from forge.pipeline.merge_executor import MERGE_REPORT_TARGET_IDENTIFIER
    from forge.pipeline.publication_record import PublicationRecordStore

    marks = ", ".join("?" for _ in ACTIVE_MISSION_STATUSES)
    try:
        rows = cx.execute(
            "SELECT b.build_id FROM builds AS b "
            "JOIN missions AS m ON m.id = b.mission_id "
            "JOIN publication_records AS p ON p.build_id = b.build_id "
            f"WHERE b.mission_role = ? AND m.status IN ({marks}) "
            "AND p.result IS NULL "
            "AND EXISTS (SELECT 1 FROM stage_log AS r WHERE r.build_id = b.build_id "
            "AND r.target_identifier = ?) "
            "ORDER BY b.queued_at ASC",
            (MISSION_ROLE_FEATURE, *ACTIVE_MISSION_STATUSES, MERGE_REPORT_TARGET_IDENTIFIER),
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 — a read never stops the queue
        logger.warning("missions: could not look for landings to settle (%s)", exc)
        return []
    records = PublicationRecordStore(cx)
    now = deps.clock()
    return [
        str(build_id)
        for (build_id,) in rows
        if str(build_id) not in _resuming and not records.read(str(build_id)).lease_is_live(now)
    ]


def _settled_landings_to_finish(deps: MissionFinishDeps, cx: Any) -> list[str]:
    """Mission features whose landing outcome is settled but whose bookkeeping is not.

    Section 11, Codex implementation rounds 2 and 3 (R2): a publication
    record with a result, AND either no landing report or a holder whose
    lease has expired, for a feature of an active mission — whatever its
    attempt's state. The press writes its result, then its report, then puts
    its lease down; a coordinator that stopped between any two of those is
    picked up here on the next pass, and the next pass after that if it
    stops again. A live, unexpired lease is never offered.
    """
    if deps.finish_settled is None:
        return []
    from forge.lifecycle.missions import ACTIVE_MISSION_STATUSES
    from forge.pipeline.merge_executor import MERGE_REPORT_TARGET_IDENTIFIER
    from forge.pipeline.publication_record import PublicationRecordStore

    marks = ", ".join("?" for _ in ACTIVE_MISSION_STATUSES)
    try:
        rows = cx.execute(
            "SELECT b.build_id FROM builds AS b "
            "JOIN missions AS m ON m.id = b.mission_id "
            "JOIN publication_records AS p ON p.build_id = b.build_id "
            f"WHERE b.mission_role = ? AND m.status IN ({marks}) "
            "AND p.result IS NOT NULL "
            "AND (NOT EXISTS (SELECT 1 FROM stage_log AS r WHERE r.build_id = b.build_id "
            "AND r.target_identifier = ?) "
            "OR (p.lease_holder IS NOT NULL AND trim(p.lease_holder) <> '')) "
            "ORDER BY b.queued_at ASC",
            (MISSION_ROLE_FEATURE, *ACTIVE_MISSION_STATUSES, MERGE_REPORT_TARGET_IDENTIFIER),
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 — a read never stops the queue
        logger.warning("missions: could not look for settled landings (%s)", exc)
        return []
    settled_records = PublicationRecordStore(cx)
    now = deps.clock()
    return [
        str(build_id)
        for (build_id,) in rows
        if str(build_id) not in _resuming
        and not settled_records.read(str(build_id)).lease_is_live(now)
    ]


async def _finish_settled_guarded(deps: MissionFinishDeps, build_id: str) -> None:
    try:
        assert deps.finish_settled is not None
        said = await deps.finish_settled(build_id)
        logger.info("missions: %s's settled landing, its bookkeeping finished: %s", build_id, said)
    except Exception as exc:  # noqa: BLE001 — logged; tried again next pass
        logger.error(
            "missions: finishing %s's settled landing raised (%s: %s)",
            build_id,
            type(exc).__name__,
            exc,
        )
    finally:
        _resuming.discard(build_id)


async def _reconcile_guarded(deps: MissionFinishDeps, build_id: str) -> None:
    try:
        assert deps.reconcile is not None
        said = await deps.reconcile(build_id)
        logger.info("missions: %s's unsettled landing, looked at again: %s", build_id, said)
    except Exception as exc:  # noqa: BLE001 — logged; tried again next pass
        logger.error(
            "missions: settling %s's landing raised (%s: %s)",
            build_id,
            type(exc).__name__,
            exc,
        )
    finally:
        _resuming.discard(build_id)


async def _resume_guarded(
    deps: MissionFinishDeps, build_id: str, *, decided: bool
) -> None:
    try:
        logger.info("missions: carrying on %s's landing, which has not reported", build_id)
        assert deps.land is not None
        if decided:
            await deps.land(build_id)
        else:
            # Its failed-task count is not on record: unknown counts as failed.
            await deps.land(build_id, tasks_failed=None)
    except Exception as exc:  # noqa: BLE001 — logged; tried again next pass
        logger.error(
            "missions: carrying on %s's landing raised (%s: %s)",
            build_id,
            type(exc).__name__,
            exc,
        )
    finally:
        _resuming.discard(build_id)


async def _finish_guarded(deps: MissionFinishDeps, mission_id: str) -> None:
    try:
        await finish_ready_mission(deps, mission_id)
    except Exception as exc:  # noqa: BLE001 — logged, tried again next tick
        logger.error(
            "missions: finishing %s raised (%s: %s); it is tried again on the "
            "next tick",
            mission_id,
            type(exc).__name__,
            exc,
        )
    finally:
        _in_progress.discard(mission_id)


# ---------------------------------------------------------------------------
# One mission
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Landing:
    key: str
    build_id: str
    feature_id: str
    repo: str
    feature_yaml_path: str
    j_commit: str
    sent_at: str


async def finish_ready_mission(deps: MissionFinishDeps, mission_id: str) -> str:
    """Take one READY or MERGING mission one step further. Returns what happened.

    Safe to call again and again: every step reads where the mission has got
    to from the ledger and changes it by compare-and-swap.
    """
    cx = deps.pool.connection
    store = MissionStore(cx, clock=deps.clock)
    mission = store.get(mission_id)
    if mission is None or mission.status not in _FINISHING_STATES:
        return "nothing to do"
    build_id = mission_merge_build_id(cx, mission.id)
    if build_id is None:
        if mission.status != "READY":
            return await _not_merged(
                deps,
                store,
                mission,
                "the mission is merging but has no merge build on record",
                expected=("MERGING",),
            )
        return await _offer_the_mission(deps, store, mission)
    return await _follow_the_card(deps, store, mission, build_id)


async def _not_merged(
    deps: MissionFinishDeps,
    store: MissionStore,
    mission: Mission,
    reason: str,
    *,
    expected: Sequence[str] = ("READY",),
    line: str | None = None,
) -> str:
    """Close the mission NOT_MERGED and say so once, in its thread."""
    if not store.set_status(mission.id, "NOT_MERGED", expected=expected, reason=reason):
        return "already moved on"
    message = line or (
        f'Mission "{mission.title}" is not offered for merging: {reason}. '
        f"Nothing reached main and the branch {mission.branch} is kept."
    )
    await _say(deps, mission, message)
    logger.info("missions: %s is NOT_MERGED — %s", mission.id, reason)
    return "not merged"


async def _say(deps: MissionFinishDeps, mission: Mission, message: str) -> None:
    try:
        await deps.notify(mission, message)
    except Exception as exc:  # noqa: BLE001 — a thread line never costs a state
        logger.warning(
            "missions: the thread line for %s could not be sent (%s: %s)",
            mission.id,
            type(exc).__name__,
            exc,
        )


def _stopping(store: MissionStore, mission_id: str) -> bool:
    current = store.get(mission_id)
    return current is not None and current.stopping


def _confirmed_landings(
    cx: Any, mission: Mission, latest: Mapping[str, MissionAttempt]
) -> list[_Landing]:
    """Every landed feature's confirmed landing on the mission branch.

    Read from the publication record of the build made under the key's
    latest LANDED attempt: its result is "published to the mission branch",
    its target is the mission branch, and a "done send" line says the send
    was published (or found there by looking).
    """
    from forge.pipeline.publication_record import (
        LINE_DONE,
        RESULT_PUBLISHED_TO_MISSION_BRANCH,
        STEP_SEND,
        PublicationRecordStore,
    )

    records = PublicationRecordStore(cx)
    found: list[_Landing] = []
    for key, attempt in latest.items():
        if attempt.status != "LANDED":
            continue
        row = cx.execute(
            "SELECT build_id, feature_id, repo, feature_yaml_path FROM builds "
            "WHERE mission_id = ? AND mission_role = ? AND correlation_id = ? "
            "ORDER BY queued_at DESC",
            (mission.id, MISSION_ROLE_FEATURE, attempt.correlation_id),
        ).fetchall()
        for build_id, feature_id, repo, yaml_path in row:
            record = records.read(str(build_id))
            if (
                not record.recorded
                or record.result != RESULT_PUBLISHED_TO_MISSION_BRANCH
                or record.target_branch != mission.branch
                or not record.j_commit
            ):
                continue
            sends = [
                line
                for line in record.lines
                if line.kind == LINE_DONE
                and line.step == STEP_SEND
                and line.detail.get("published") is True
            ]
            if not sends:
                continue
            found.append(
                _Landing(
                    key=key,
                    build_id=str(build_id),
                    feature_id=str(feature_id),
                    repo=str(repo),
                    feature_yaml_path=str(yaml_path),
                    j_commit=str(record.j_commit),
                    sent_at=str(sends[-1].at),
                )
            )
            break
    return found


async def _offer_the_mission(
    deps: MissionFinishDeps, store: MissionStore, mission: Mission
) -> str:
    """READY with no merge build yet: check, freeze, record, offer.

    A stop asked for (section 11) is checked again at each step that commits
    something — the frozen tip, the merge build, the offer — each in its own
    transaction, so a stop that lands while this is paused (fetching, say)
    leaves nothing recorded after it and no card.
    """
    if mission.stopping:
        return "stopping"
    features = store.features(mission.id)
    latest = store.latest_attempts(mission.id)
    if not any(_status(latest, f.key) == "LANDED" for f in features):
        return await _not_merged(
            deps,
            store,
            mission,
            "nothing landed",
            line=(
                f'Mission "{mission.title}": none of its {len(features)} '
                f"features landed on {mission.branch}, so there is no merge "
                "card. Nothing reached main and the branch is kept."
            ),
        )
    cx = deps.pool.connection
    landings = _confirmed_landings(cx, mission, latest)
    if not landings:
        return await _not_merged(
            deps,
            store,
            mission,
            "no landed feature has a confirmed landing on the mission branch",
        )
    last = max(landings, key=lambda landing: landing.sent_at)
    repo_root_raw = deps.config.planning.target_repo_paths.get(last.repo)
    if not repo_root_raw:
        return await _not_merged(
            deps,
            store,
            mission,
            f"the repository {last.repo} is not one this factory knows",
        )
    repo_root = Path(repo_root_raw)
    git = deps.git_for(last.repo, repo_root)

    start = await git.fetch_remote_start_point(mission.branch)
    if not getattr(start, "ok", False) or not getattr(start, "branch_commit", None):
        # A BRANCH THAT IS GONE IS A FACT ABOUT THE MISSION: the remote
        # answered, and it has no mission branch. Waiting for it would keep
        # the mission active for ever, and an active mission blocks router
        # restarts, so it is closed.
        if await _the_branch_is_gone(git, mission.branch, start):
            return await _not_merged(
                deps,
                store,
                mission,
                f"its branch {mission.branch} is no longer on the remote, so "
                "something else changed the mission branch",
            )
        # A remote that could not be read is not a fact about the mission:
        # the step tries again on the next tick and says nothing yet.
        logger.warning(
            "missions: %s's branch %s could not be read (%s) — tried again on "
            "the next tick",
            mission.id,
            mission.branch,
            getattr(start, "refusal", None),
        )
        return "waiting for the remote"
    tip = str(start.branch_commit)
    if tip != last.j_commit:
        return await _not_merged(
            deps,
            store,
            mission,
            f"its branch {mission.branch} is at {tip[:10]}, but the last "
            f"feature to land ({last.key}) left it at {last.j_commit[:10]}, so "
            "something else changed the branch",
        )

    if deps.feature_routing_required:
        try:
            if deps.feature_routing_gate is None:
                raise RuntimeError("no routing gate is composed")
            deps.feature_routing_gate.require_committed_success(mission.correlation_id)
        except Exception as exc:  # noqa: BLE001 — missing is final, never retried
            logger.warning(
                "missions: %s has no committed routing receipt (%s: %s)",
                mission.id,
                type(exc).__name__,
                exc,
            )
            return await _not_merged(
                deps,
                store,
                mission,
                "the factory's model routing for this mission has no committed "
                "receipt, so its merge cannot run",
            )

    if not store.set_final_commit(mission.id, tip):
        if _stopping(store, mission.id):
            return "stopping"  # a stop arrived first: nothing is recorded
        return await _not_merged(
            deps,
            store,
            mission,
            "a different frozen tip is already recorded for this mission",
        )

    build_id = deps.pool.record_mission_merge_build(
        mission_id=mission.id,
        correlation_id=mission.correlation_id,
        feature_id=last.feature_id,
        repo=last.repo,
        branch=mission.branch,
        feature_yaml_path=last.feature_yaml_path,
        originating_user=mission.originating_user,
        start_commit=str(start.commit),
        target_branch=str(start.branch),
        queued_at=deps.clock(),
        # The build's declared settings, as the last landed feature recorded
        # them: the preview's deploy and the press read them off this row.
        memory_project=deps.pool.read_memory_project(last.build_id),
        launch_settings=deps.pool.read_launch_settings(last.build_id),
    )
    if build_id is None:
        return "stopping"  # a stop arrived first: no merge build, no card
    refreshed = store.get(mission.id) or mission
    return await _freeze_and_offer(
        deps,
        store,
        refreshed,
        build_id=build_id,
        git=git,
        repo=last.repo,
        repo_root=repo_root,
        features=features,
        latest=latest,
    )


async def _the_branch_is_gone(git: Any, branch: str, refused: Any) -> bool:
    """Did the remote answer, and say it has no such branch?

    Both halves, so an unreachable remote is never read as a missing branch:
    the refusal is the fetch's own "has no branch called" sentence, and the
    default branch alone is then fetched successfully from the same remote.
    """
    said = str(getattr(refused, "refusal", None) or "")
    if "no branch called" not in said or branch not in said:
        return False
    default = await git.fetch_remote_start_point()
    return bool(getattr(default, "ok", False))


async def make_frozen_source(git: Any, build_id: str, commit: str) -> str | None:
    """``forge/source/<build_id>`` at ``commit``, read back. ``None`` or why not.

    Through the venue's ``make_source_branch`` (the prepared route's rule:
    local commits only, ``branch --force``, so a stale branch is put back),
    then read back with ``rev_parse``: anything that does not resolve to
    exactly ``commit`` is a refusal.
    """
    make = getattr(git, "make_source_branch", None)
    if not callable(make):
        return "this repository's git venue cannot make the frozen source branch"
    made = await make(build_id, commit)
    refusal = getattr(made, "refusal", None)
    if refusal:
        return str(refusal)
    branch = getattr(made, "branch", None) or f"forge/source/{build_id}"
    read_back = await git.rev_parse(f"refs/heads/{branch}")
    if read_back != commit:
        return (
            f"the frozen source branch {branch} reads back as "
            f"{str(read_back or 'nothing')[:10]}, not {commit[:10]}"
        )
    return None


async def _the_risky_files(
    deps: MissionFinishDeps,
    *,
    repo: str,
    repo_root: Path,
    base: str | None,
    tip: str,
) -> tuple[list[str] | None, str | None]:
    """``(files to look at first, None)`` or ``(None, why they could not be listed)``.

    The files changed between the mission's base commit and its tip, by
    comparing the two commits' file listings (mode and object of each path).
    """
    if not base:
        return None, "the mission has no recorded base commit"
    if deps.tree_reader_for is None:
        return None, "nothing here can list a commit's files"
    try:
        reader = deps.tree_reader_for(repo, repo_root)
        before = await reader.list_tree_at_commit(str(repo_root), base)
        after = await reader.list_tree_at_commit(str(repo_root), tip)
    except Exception as exc:  # noqa: BLE001 — a reading never stops the card
        return None, f"{type(exc).__name__}: {exc}"
    for listing in (before, after):
        if not getattr(listing, "ok", False):
            return None, str(getattr(listing, "refusal", None) or "a listing failed")
    was = {e.path: (e.mode, e.object) for e in before.entries}
    now = {e.path: (e.mode, e.object) for e in after.entries}
    changed = sorted(p for p in set(was) | set(now) if was.get(p) != now.get(p))
    return risky_files(changed), None


async def _freeze_and_offer(
    deps: MissionFinishDeps,
    store: MissionStore,
    mission: Mission,
    *,
    build_id: str,
    git: Any,
    repo: str,
    repo_root: Path,
    features: Sequence[MissionFeature],
    latest: Mapping[str, MissionAttempt],
) -> str:
    """Make the frozen source, read it back, and offer the card once."""
    from forge.pipeline.merge_offer import MERGE_OFFER_TARGET_IDENTIFIER

    failed_offers = _failed_offers(deps, build_id)
    if failed_offers:
        since_last = (deps.clock() - failed_offers[-1]).total_seconds()
        if since_last < OFFER_RETRY_SECONDS:
            return "waiting to offer"
    tip = str(mission.final_commit or "")
    refusal = await make_frozen_source(git, build_id, tip)
    if refusal is not None:
        return await _not_merged(
            deps,
            store,
            mission,
            f"the frozen copy of the mission tip could not be made ({refusal})",
        )
    if any(
        s.target_identifier == MERGE_OFFER_TARGET_IDENTIFIER
        for s in deps.pool.read_stages(build_id)
    ):
        return "offered"
    if _stopping(store, mission.id):
        # Never expected: the request is refused once the merge build exists.
        return "stopping"
    risky, unread = await _the_risky_files(
        deps, repo=repo, repo_root=repo_root, base=mission.base_commit, tip=tip
    )
    words = mission_card_words(
        mission=mission,
        features=features,
        latest=latest,
        risky=risky,
        risky_unread=unread,
    )
    row = deps.pool.get_build_row(build_id)
    landed = [f.key for f in features if _status(latest, f.key) == "LANDED"]
    refusals: list[tuple[str, bool]] = []
    offered = await deps.offer(
        build_id=build_id,
        feature_id=str(row.feature_id),
        card_words=lambda _branch, _merge_branch: words,
        extra_details={
            "mission": {
                "id": mission.id,
                "title": mission.title,
                "branch": mission.branch,
                "base_commit": mission.base_commit,
                "final_commit": tip,
                "landed": landed,
                "not_landed": [f.key for f in features if f.key not in landed],
                "risky_files": risky,
                "risky_files_unread": unread,
            }
        },
        on_refusal=lambda sentence, may_pass: refusals.append((sentence, may_pass)),
    )
    if offered or any(
        s.target_identifier == MERGE_OFFER_TARGET_IDENTIFIER
        for s in deps.pool.read_stages(build_id)
    ):
        return "offered"
    # A REFUSAL THAT MAY PASS IS TRIED AGAIN ON THE NEXT TICK (a commit that
    # could not be read just now, an offer already being prepared, a preview
    # service that raised). Only a refusal that will not pass closes the
    # mission. An offer that gave no reason is tried again too.
    final = [sentence for sentence, may_pass in refusals if not may_pass]
    if not final:
        why = "; ".join(sentence for sentence, _ in refusals) or "no reason given"
        _record_failed_offer(deps, build_id, why)
        failed_offers = _failed_offers(deps, build_id)
        span = (failed_offers[-1] - failed_offers[0]).total_seconds()
        if len(failed_offers) >= OFFER_ATTEMPTS and span >= OFFER_GIVE_UP_SECONDS:
            return await _not_merged(
                deps,
                store,
                mission,
                f"the merge card could not be offered after {len(failed_offers)} "
                f"tries over {int(span // 60)} minutes (last: {why})",
            )
        logger.warning(
            "missions: %s's merge card was not offered (%s) — tried again on "
            "the next tick",
            mission.id,
            "; ".join(sentence for sentence, _ in refusals) or "no reason given",
        )
        return "waiting to offer"
    return await _not_merged(
        deps,
        store,
        mission,
        f"the merge card could not be offered: {final[0]}",
    )


async def _carry_the_merge_on(
    deps: MissionFinishDeps, mission: Mission, row: Any, repo_root: Path, decision: Any
) -> str:
    """An approved mission merge with no report: carry it on if it stopped.

    Implementation review round 2, R6. Only when the decision is older than
    :data:`MERGE_RESUME_GRACE_SECONDS` and the publication record holds no
    live lease (a press running anywhere holds one); the press itself also
    refuses a build it is pressing right now. The frozen source is put back
    at the recorded tip first, so the press finds the very candidate the card
    pinned. The press's own pick-up settles a merge already published.
    """
    from forge.pipeline.publication_record import PublicationRecordStore

    if deps.resume_merge is None:
        return "merging"
    now = deps.clock()
    decided_at = decision.started_at
    if decided_at.tzinfo is None:
        decided_at = decided_at.replace(tzinfo=timezone.utc)
    if (now - decided_at).total_seconds() < MERGE_RESUME_GRACE_SECONDS:
        return "merging"
    if PublicationRecordStore(deps.pool.connection).read(row.build_id).lease_is_live(now):
        return "merging"
    tip = str(mission.final_commit or "")
    branch = str(row.merge_branch or f"forge/source/{row.build_id}")
    git = deps.git_for(row.repo, repo_root)
    if tip and await git.rev_parse(f"refs/heads/{branch}") != tip:
        refusal = await make_frozen_source(git, row.build_id, tip)
        if refusal is not None:
            logger.warning(
                "missions: %s's frozen source could not be put back before its "
                "merge was carried on (%s)",
                mission.id,
                refusal,
            )
    if await deps.resume_merge(row.build_id):
        return "merge carried on"
    return "merging"


def _failed_offers(deps: MissionFinishDeps, build_id: str) -> list[datetime]:
    """When each failed offer of this mission's card was made, oldest first."""
    moments: list[datetime] = []
    for stage in deps.pool.read_stages(build_id):
        if stage.target_identifier != MISSION_OFFER_ATTEMPT_TARGET_IDENTIFIER:
            continue
        at = stage.started_at
        moments.append(at if at.tzinfo else at.replace(tzinfo=timezone.utc))
    return moments


def _record_failed_offer(deps: MissionFinishDeps, build_id: str, why: str) -> None:
    """One durable row per failed offer of the mission's card."""
    from forge.lifecycle.persistence import StageLogEntry

    now = deps.clock()
    deps.pool.record_stage(
        StageLogEntry(
            build_id=build_id,
            stage_label="mission merge card",
            target_kind="local_tool",
            target_identifier=MISSION_OFFER_ATTEMPT_TARGET_IDENTIFIER,
            status="SKIPPED",
            gate_mode=None,
            started_at=now,
            completed_at=now,
            duration_secs=0.0,
            details={"mission_offer_attempt": {"refused": _first_sentence(why, 240)}},
        )
    )


async def _follow_the_card(
    deps: MissionFinishDeps, store: MissionStore, mission: Mission, build_id: str
) -> str:
    """The mission has its merge build: keep its source pinned and read the answer."""
    from forge.pipeline.merge_executor import (
        MERGE_DECISION_DETAILS_KEY,
        MERGE_DECISION_TARGET_IDENTIFIER,
        MERGE_REPORT_TARGET_IDENTIFIER,
    )
    from forge.pipeline.merge_offer import MERGE_OFFER_TARGET_IDENTIFIER

    stages = deps.pool.read_stages(build_id)
    offered = any(s.target_identifier == MERGE_OFFER_TARGET_IDENTIFIER for s in stages)
    decisions = [s for s in stages if s.target_identifier == MERGE_DECISION_TARGET_IDENTIFIER]
    row = deps.pool.get_build_row(build_id)
    repo_root_raw = deps.config.planning.target_repo_paths.get(row.repo) if row else None
    if row is None or not repo_root_raw:
        return await _not_merged(
            deps,
            store,
            mission,
            "its merge build or repository can no longer be read",
            expected=("READY", "MERGING"),
        )
    repo_root = Path(repo_root_raw)

    if not decisions:
        git = deps.git_for(row.repo, repo_root)
        if not offered:
            # A restart between the build row and the card: the card is
            # offered now, from the frozen tip already recorded.
            return await _freeze_and_offer(
                deps,
                store,
                mission,
                build_id=build_id,
                git=git,
                repo=row.repo,
                repo_root=repo_root,
                features=store.features(mission.id),
                latest=store.latest_attempts(mission.id),
            )
        # THE PIN IS KEPT ACROSS A RESTART: a clone made afresh has no local
        # frozen source, so it is put back at the recorded tip.
        tip = str(mission.final_commit or "")
        branch = str(row.merge_branch or f"forge/source/{build_id}")
        if tip and await git.rev_parse(f"refs/heads/{branch}") != tip:
            refusal = await make_frozen_source(git, build_id, tip)
            if refusal is not None:
                logger.warning(
                    "missions: %s's frozen source could not be put back (%s) — "
                    "tried again on the next tick",
                    mission.id,
                    refusal,
                )
        return "waiting for the card"

    decided_at = max(
        i for i, s in enumerate(stages) if s.target_identifier == MERGE_DECISION_TARGET_IDENTIFIER
    )
    decision = stages[decided_at]
    said = (decision.details or {}).get(MERGE_DECISION_DETAILS_KEY) or {}
    if decision.status != "PASSED" or said.get("decision") == "reject":
        who = said.get("decided_by") or "the approver"
        return await _not_merged(
            deps,
            store,
            mission,
            f"{who} declined the merge card",
            expected=("READY", "MERGING"),
            line=(
                f'Mission "{mission.title}": {who} declined the merge card. '
                f"Nothing reached main and the branch {mission.branch} is kept."
            ),
        )
    if mission.status == "READY":
        store.set_status(mission.id, "MERGING", expected=("READY",))
        mission = store.get(mission.id) or mission
    reports = [
        s
        for s in stages[decided_at + 1 :]
        if s.target_identifier == MERGE_REPORT_TARGET_IDENTIFIER
    ]
    if not reports:
        return await _carry_the_merge_on(deps, mission, row, repo_root, decision)
    report = reports[-1]
    word = str((report.details or {}).get("result") or "")
    detail = str((report.details or {}).get("detail") or "")
    # ON MAIN IS READ OFF THE PUBLICATION RECORD, never off the report's
    # word: a deploy that failed after the send still left the mission on
    # main ("merged-deploy-failed", with the record "published, deployment
    # pending"), and calling that "not merged" would be untrue.
    from forge.pipeline.publication_record import (
        RESULT_MERGED_AND_RUNNING,
        RESULT_PUBLISHED_DEPLOYMENT_PENDING,
        PublicationRecordStore,
    )

    record = PublicationRecordStore(deps.pool.connection).read(build_id)
    target = str(row.target_branch or "main")
    on_main = record.recorded and record.result in (
        RESULT_PUBLISHED_DEPLOYMENT_PENDING,
        RESULT_MERGED_AND_RUNNING,
    ) and (record.target_branch or target) == target
    if on_main:
        if record.result == RESULT_MERGED_AND_RUNNING:
            line = f'Mission "{mission.title}" is merged into {target} and deployed.'
        elif word in _DEPLOY_FAILED_WORDS:
            line = (
                f'Mission "{mission.title}" is merged into {target}, but its '
                f"deploy failed: {_first_sentence(detail, 240)}"
            )
        else:
            line = (
                f'Mission "{mission.title}" is merged into {target}; it is not '
                f"deployed yet: {_first_sentence(detail, 240)}"
            )
        if not store.set_status(
            mission.id,
            "MERGED",
            expected=("MERGING",),
            reason=_first_sentence(detail) or word,
        ):
            return "already moved on"
        await _say(deps, mission, line)
        return "merged"
    reason = _first_sentence(detail, 240) or f"the merge ended {word or 'without a word'}"
    return await _not_merged(
        deps,
        store,
        mission,
        reason,
        expected=("MERGING",),
        line=(
            f'Mission "{mission.title}" was not merged: {reason} '
            f"The branch {mission.branch} is kept."
        ),
    )


# ---------------------------------------------------------------------------
# Composition at boot
# ---------------------------------------------------------------------------


def compose_mission_finisher(
    *,
    forge_config: Any,
    pool: Any,
    offer_service: Any,
    nats_client: Any,
    land: Callable[[str], Awaitable[Any]] | None = None,
    resume_merge: Callable[[str], Awaitable[Any]] | None = None,
    reconcile: Callable[[str], Awaitable[Any]] | None = None,
    finish_settled: Callable[[str], Awaitable[Any]] | None = None,
    git_surface: Callable[[str, Path], Any] | None = None,
    tree_reader_for: Callable[[str, Path], Any] | None = None,
    feature_routing_gate: Any | None = None,
    feature_routing_required: bool = False,
) -> Callable[[], Awaitable[Any]]:
    """The finisher :func:`install_finisher` takes, from the coordinator's parts.

    ``git_surface`` is the press's own venue chooser (``None`` for a
    repository with no sandbox, which then runs its git in this container
    against the project's recorded fetch address, as the offer does). Thread
    lines go to ``jarvis.notification.slack`` under the mission's own
    correlation id, naming the person who sent the mission.
    """
    from forge.deploy.candidate_tree import InContainerCandidateGit
    from forge.planning.notifications import build_planning_notification_envelope

    addresses = dict(getattr(forge_config.planning, "fetch_addresses", None) or {})

    def git_for(repo: str, repo_root: Path) -> Any:
        chosen = git_surface(repo, repo_root) if git_surface is not None else None
        return chosen or InContainerCandidateGit(
            repo_root, fetch_address=addresses.get(repo), repo=repo
        )

    async def notify(mission: Mission, message: str) -> None:
        envelope = build_planning_notification_envelope(
            correlation_id=mission.correlation_id,
            message=message,
            level="info",
            target_user=mission.originating_user,
        )
        await nats_client.publish(
            "jarvis.notification.slack", envelope.model_dump_json().encode("utf-8")
        )

    deps = MissionFinishDeps(
        pool=pool,
        config=forge_config,
        offer=offer_service.offer,
        git_for=git_for,
        notify=notify,
        tree_reader_for=tree_reader_for,
        land=land,
        resume_merge=resume_merge,
        reconcile=reconcile,
        finish_settled=finish_settled,
        feature_routing_gate=feature_routing_gate,
        feature_routing_required=feature_routing_required,
    )

    async def finisher() -> list[str]:
        return await finish_ready_missions(deps)

    return finisher

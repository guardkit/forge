"""When the nightly sweep runs, and how ``forge serve`` and the CLI start it.

Public surface
==============

- :func:`factory_is_idle` — nothing in flight, by the same count the work
  queue's admission uses (``count_in_flight``), and no planning run open.
- :class:`NightlySchedule` — the background task ``forge serve`` runs when
  ``security_sweep.enabled`` is true.
- :func:`compose_sweep` — builds a run's :class:`~.sweep.SweepDeps` from the
  coordinator's own settings, database and bus client.

Running only when the factory is idle (Codex R6)
------------------------------------------------

At ``at`` (UTC) the sweep starts only if the factory is idle. If it is busy,
it looks again every 15 minutes until ``until``; a night with no quiet moment
posts one line: "skipped: the factory was busy all night". One sweep at a
time: the scheduled run and a hand-run ``forge security-sweep --once`` take
the same file lock, and a second start says so and stops. If a build starts
during a sweep, the sweep finishes the project it is on and stops.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from .state import SweepAlreadyRunning
from .sweep import RunReport, SweepDeps, run_sweep

logger = logging.getLogger(__name__)

#: How long the sweep waits before looking again while the factory is busy.
RECHECK_SECONDS = 15 * 60

#: The line for a night with no quiet moment.
SKIPPED_LINE = "Nightly security check: skipped: the factory was busy all night."


def factory_is_idle(connection: sqlite3.Connection, *, merge_offer_hold_seconds: int = 0) -> bool:
    """True when nothing is in flight and no planning run is open.

    "In flight" is the work queue's own admission count
    (:func:`forge.planning.work_queue_loop.count_in_flight`): admitted rows not
    yet started, active planning runs, active builds and unanswered merge
    cards. "No planning run open" adds any planning run not yet in a terminal
    state. A database that cannot be read counts as busy.
    """
    from forge.planning.work_queue_loop import PLANNING_TERMINAL_STATES, count_in_flight

    try:
        if count_in_flight(connection, merge_offer_hold_seconds=merge_offer_hold_seconds):
            return False
        terminal = sorted(PLANNING_TERMINAL_STATES)
        placeholders = ",".join("?" for _ in terminal)
        try:
            row = connection.execute(
                f"SELECT COUNT(*) FROM planning_runs WHERE state NOT IN ({placeholders})",
                tuple(terminal),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return True
            raise
        return not (row and int(row[0]))
    except Exception as exc:  # noqa: BLE001 — unreadable means busy, never idle
        logger.warning("security sweep: could not tell whether the factory is idle (%s)", type(exc).__name__)
        return False


def _at(day: datetime, text: str) -> datetime:
    hours, minutes = (int(part) for part in text.split(":"))
    return datetime.combine(day.date(), time(hours, minutes), tzinfo=timezone.utc)


@dataclass
class NightlySchedule:
    """The background task: one sweep a night, only when the factory is idle."""

    at: str
    until: str
    #: ``async () -> RunReport`` — one sweep (raises SweepAlreadyRunning).
    run_once: Callable[[], Awaitable[RunReport]]
    is_idle: Callable[[], bool]
    #: ``async (text) -> None`` — the skipped line.
    publish: Callable[[str], Awaitable[None]]
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    def window(self, now: datetime) -> tuple[datetime, datetime]:
        """The next night's ``(start, latest start)`` at or after ``now``.

        A night whose start has passed but whose latest start has not is
        tonight's, so a coordinator restarted at 03:00 still sweeps.
        """
        for day in (now - timedelta(days=1), now, now + timedelta(days=1)):
            start = _at(day, self.at)
            latest = _at(day, self.until)
            if latest <= start:
                latest += timedelta(days=1)
            if now < latest:
                return start, latest
        raise AssertionError("unreachable")  # pragma: no cover

    async def run_night(self, start: datetime, latest: datetime) -> str:
        """One night: wait for ``start``, then start when idle, by ``latest``.

        Returns what happened: ``swept``, ``skipped`` or ``already-running``.
        """
        delay = (start - self.clock()).total_seconds()
        if delay > 0:
            await self.sleep(delay)
        while True:
            if self.is_idle():
                try:
                    await self.run_once()
                except SweepAlreadyRunning:
                    logger.info("security sweep: another sweep is running; tonight's is left to it")
                    return "already-running"
                return "swept"
            now = self.clock()
            if now + timedelta(seconds=RECHECK_SECONDS) > latest:
                try:
                    await self.publish(SKIPPED_LINE)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("security sweep: the skipped line could not be published (%s)", type(exc).__name__)
                logger.info("security sweep: skipped — the factory was busy all night")
                return "skipped"
            await self.sleep(RECHECK_SECONDS)

    async def run_forever(self) -> None:
        """Night after night. Never returns; a fault in one night never stops the next."""
        while True:
            start, latest = self.window(self.clock())
            try:
                await self.run_night(start, latest)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — a fault must not stop the next night
                logger.error("security sweep: tonight's sweep failed (%s)", type(exc).__name__)
            # Let tonight's window close, so the same night is never swept twice.
            gap = (latest - self.clock()).total_seconds()
            if gap > 0:
                await self.sleep(gap)


def notification_publisher(nats_client: Any, approver: str | None) -> Callable[[str], Awaitable[None]]:
    """``async (text) -> None``: one top-level Slack line that mentions Rich.

    Published on ``jarvis.notification.slack`` the way Forge's planning lines
    are (:func:`forge.planning.notifications.build_planning_notification_envelope`),
    with no thread anchor, so it is a top-level post, and the approver as
    ``target_user``, so it mentions him.
    """
    from forge.planning.notifications import (
        NOTIFICATION_SUBJECT,
        build_planning_notification_envelope,
    )

    async def publish(text: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        envelope = build_planning_notification_envelope(
            correlation_id=f"security-sweep-{stamp}",
            message=text,
            level="info",
            parent_request_id=None,
            target_user=approver,
        )
        await nats_client.publish(NOTIFICATION_SUBJECT, envelope.model_dump_json().encode("utf-8"))
        flush = getattr(nats_client, "flush", None)
        if flush is not None:
            await flush()

    return publish


def compose_sweep(
    forge_config: Any,
    *,
    db_path: Path,
    publish: Callable[[str], Awaitable[None]],
    connection: sqlite3.Connection | None = None,
    sandbox_post: Any = None,
) -> tuple[SweepDeps, Callable[[], bool]]:
    """``(deps, is_idle)`` for the coordinator's own settings and database.

    ``sandbox_post`` replaces the sandbox helper's HTTP call (tests only).

    The projects are ``planning.fetch_addresses``; the approver is
    ``approval.expected_approver``; the folder is ``security-sweep`` beside
    the database; the model is the coordinator's existing route
    (:func:`~.sorting.default_asker`); the scanners are where the image puts
    them.
    """
    from forge.adapters.sqlite import connect_writer
    from forge.planning.work_queue_store import WorkQueueStore

    from .scanners import ScannerSetup
    from .sorting import default_asker
    from .state import sweep_folder

    settings = forge_config.security_sweep
    hold = int(getattr(getattr(forge_config, "queue", None), "merge_offer_hold_seconds", 0) or 0)
    cx = connection if connection is not None else connect_writer(db_path)

    def is_idle() -> bool:
        return factory_is_idle(cx, merge_offer_hold_seconds=hold)

    asker, why_not = default_asker()
    deps = SweepDeps(
        projects=dict(forge_config.planning.fetch_addresses),
        folder=sweep_folder(db_path),
        queue=WorkQueueStore(cx),
        approver=getattr(forge_config.approval, "expected_approver", None),
        serious_score=float(settings.serious_score),
        draft_repairs=bool(settings.draft_repairs),
        weekly_all_clear=settings.weekly_all_clear,
        setup=ScannerSetup.from_image(),
        publish=publish,
        is_idle=is_idle,
        asker=asker,
        asker_missing=why_not or "the model is not configured",
        # A public project from GitHub; a private one through its own
        # sandbox (19 October 2026). The coordinator never fetches through a
        # Git door: the door answers only the project's own sandbox.
        fetch=_fetch_for_each_project(forge_config, sandbox_post=sandbox_post),
        merge_executor_enabled=bool(
            getattr(getattr(forge_config, "merge_executor", None), "enabled", False)
        ),
    )
    return deps, is_idle


def _fetch_for_each_project(
    forge_config: Any,
    *,
    sandbox_post: Any = None,
    fetch_public: Any = None,
    fetch_private: Any = None,
) -> Any:
    """``fetch(address, folder)`` for the project whose recorded address it is.

    A project whose recorded address goes through its sandbox's Git door is
    private: it is fetched through that sandbox (the helper updates the
    sandbox's copy of GitHub, then the sandbox's read-only export is read,
    :func:`~.repository.fetch_through_sandbox`), with the export address the
    coordinator's sandbox entry records (``git_export``). Every other
    project is fetched from its https address exactly as before
    (:func:`~.repository.fetch_mirror`). The door address itself is never
    fetched from here.
    """
    from forge.config.sandboxes import git_door_for, sandbox_for
    from forge.network_git import transport_of

    from .repository import fetch_mirror, fetch_through_sandbox

    public = fetch_public or fetch_mirror
    private = fetch_private or fetch_through_sandbox
    repos = {
        str(address).strip(): repo
        for repo, address in dict(forge_config.planning.fetch_addresses).items()
    }

    def fetch(address: str, folder: Path) -> Any:
        repo = repos.get(str(address).strip())
        door = git_door_for(forge_config, repo) if repo else None
        if repo and door is not None and transport_of(str(address)) == "http":
            entry = sandbox_for(forge_config, repo)
            return private(
                repo,
                str(address).strip(),
                folder,
                sidecar_url=getattr(entry, "sidecar_url", None),
                git_door=door,
                git_export=getattr(entry, "git_export", None),
                post=sandbox_post,
            )
        return public(address, folder)

    return fetch


def compose_nightly_schedule(forge_config: Any, *, db_path: Path, nats_client: Any) -> NightlySchedule:
    """The background task ``forge serve`` runs when the sweep is switched on."""
    settings = forge_config.security_sweep
    publish = notification_publisher(
        nats_client, getattr(forge_config.approval, "expected_approver", None)
    )
    deps, is_idle = compose_sweep(forge_config, db_path=db_path, publish=publish)

    async def run_once() -> RunReport:
        return await run_sweep(deps)

    return NightlySchedule(
        at=settings.at, until=settings.until, run_once=run_once, is_idle=is_idle, publish=publish
    )


__all__ = [
    "NightlySchedule",
    "RECHECK_SECONDS",
    "SKIPPED_LINE",
    "compose_nightly_schedule",
    "compose_sweep",
    "factory_is_idle",
    "notification_publisher",
]

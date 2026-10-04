"""Interrupt the run the ledger recorded for a build (best effort).

After a factory-only restart a build's run can still be going in its runner
while the factory no longer holds a place for it: boot settlement refuses it,
or a recovered build's card is declined, expires or is stopped. Nothing else
would ever tell that run to stop. This sends it the ordinary
``runs.cancel(action="interrupt")`` on the runner it was launched on — the
repository's sandbox runner when it has one, else the global runner — and the
runner's own cancel handler then stops everything the build owns. No
confirmation is awaited; a missing row, a run that has already ended or an
unreachable runner is logged and is not an error.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

__all__ = [
    "interrupt_recorded_run",
    "launch_replacing_recorded_run",
    "runner_url_for_repo",
]


def runner_url_for_repo(forge_config: Any, repo: str | None) -> str | None:
    """The runner a build of ``repo`` is launched on.

    The repository's sandbox runner (``planning.sandboxes``, as the launch
    routes it), else ``FORGE_AUTOBUILD_RUNNER_URL``.
    """
    if forge_config is not None and repo:
        from forge.config.sandboxes import sandbox_for

        entry = sandbox_for(forge_config, repo)
        url = str(getattr(entry, "runner_url", "") or "").strip()
        if url:
            return url
    return os.environ.get("FORGE_AUTOBUILD_RUNNER_URL") or None


def _recorded(sqlite_pool: Any, build_id: str) -> tuple[str | None, str | None]:
    """``(thread_id, repo)`` the ledger recorded for ``build_id``.

    A ledger with no ``async_tasks`` table has recorded no run.
    """
    try:
        row = sqlite_pool.connection.execute(
            "SELECT a.task_id, b.repo FROM builds b LEFT JOIN async_tasks a "
            "ON a.build_id = b.build_id WHERE b.build_id = ? "
            "ORDER BY a.started_at DESC LIMIT 1",
            (build_id,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return None, None
    if row is None:
        return None, None
    return (str(row[0]) if row[0] else None), (str(row[1]) if row[1] else None)


async def interrupt_recorded_run(
    sqlite_pool: Any,
    forge_config: Any,
    build_id: str,
    *,
    thread_id: str | None = None,
    repo: str | None = None,
) -> bool:
    """Interrupt ``build_id``'s recorded run. Never raises.

    ``True`` when the interrupt was issued or none was needed (no recorded
    run, or it has already ended); ``False`` only when it could not be
    issued (no runner address, the runner unreachable or refusing), so a
    caller about to let the build's place go can hold instead and retry.
    ``thread_id``/``repo`` may be given when the caller already read them
    (the row may be gone by then).
    """
    try:
        if thread_id is None or repo is None:
            recorded_thread, recorded_repo = _recorded(sqlite_pool, build_id)
            thread_id = thread_id or recorded_thread
            repo = repo or recorded_repo
        if not thread_id:
            return True
        url = runner_url_for_repo(forge_config, repo)
        if not url:
            logger.warning(
                "interrupt_recorded_run: no runner address for build_id=%s "
                "(repo=%s); its run, if any, is not interrupted",
                build_id,
                repo,
            )
            return False
        from langgraph_sdk import get_client

        client = get_client(url=url)
        runs = await client.runs.list(thread_id, limit=1)
        if not runs:
            return True
        run = runs[0]
        run_id = run.get("run_id") if isinstance(run, dict) else getattr(run, "run_id", None)
        status = run.get("status") if isinstance(run, dict) else getattr(run, "status", None)
        if not run_id or status not in ("pending", "running"):
            return True
        await client.runs.cancel(thread_id, run_id, action="interrupt")
    except Exception as exc:  # noqa: BLE001 — best effort, said once
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if status_code in (404, 409):
            # The right runner does not know the thread or run (it restarted)
            # or the run already finished: there is nothing to interrupt.
            logger.info(
                "interrupt_recorded_run: no run to interrupt for build_id=%s "
                "(the runner answered %s)",
                build_id,
                status_code,
            )
            return True
        logger.warning(
            "interrupt_recorded_run: could not interrupt the run of build_id=%s "
            "(%s: %s)",
            build_id,
            type(exc).__name__,
            exc,
        )
        return False
    logger.warning(
        "interrupt_recorded_run: interrupted the run of build_id=%s "
        "(thread %s, run %s) on %s",
        build_id,
        thread_id,
        run_id,
        url,
    )
    return True


async def launch_replacing_recorded_run(
    sqlite_pool: Any,
    build_id: str,
    launch: Callable[[], Awaitable[Any]],
) -> bool:
    """Launch a build again in place of the run the ledger recorded for it.

    The caller has already interrupted that earlier run (before the build's
    card was shown). Its ``async_tasks`` row is deleted just before the
    relaunch, so the relaunch's observer binds the relaunch's own thread and
    run. If the launch does not complete — it raises, or the task is
    cancelled (a daemon shutdown) — the row is put back before the error
    goes on, so the earlier run is never left without its identity. A build
    with no recorded run is simply launched. ``False``: the identity could
    not be read or cleared, nothing was launched, and the caller holds the
    build's message without acknowledging it.
    """
    try:
        cursor = sqlite_pool.connection.execute(
            "SELECT * FROM async_tasks WHERE build_id = ?", (build_id,)
        )
        columns = [d[0] for d in cursor.description]
        kept = [tuple(row) for row in cursor.fetchall()]
        if kept:
            sqlite_pool.connection.execute(
                "DELETE FROM async_tasks WHERE build_id = ?", (build_id,)
            )
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            return _hold(build_id, exc)
        kept = []
    except sqlite3.Error as exc:
        return _hold(build_id, exc)
    try:
        await launch()
    except BaseException:
        # Synchronous on purpose (no await): a cancellation cannot cut it.
        for values in kept:
            sqlite_pool.connection.execute(
                f"INSERT OR REPLACE INTO async_tasks ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' * len(values))})",
                values,
            )
        raise
    return True


def _hold(build_id: str, exc: Exception) -> bool:
    logger.error(
        "launch_replacing_recorded_run: could not clear the earlier run's "
        "identity for build_id=%s (%s); nothing launched, holding the "
        "message WITHOUT ack",
        build_id,
        exc,
    )
    return False

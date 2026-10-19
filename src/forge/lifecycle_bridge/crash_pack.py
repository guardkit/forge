"""A failure pack for a build whose runner stopped without reporting an end.

19 October 2026, build FEAT-B874. The ts-api-test runner's ``running_wave``
node raised, the LangGraph run ended in an error, and the coordinator saw the
stream end with no terminal. It published a synthetic ``build-failed``
(``stream-ended-without-terminal``), and the repair it filed was refused
because "the failure pack has no readable manifest": every failure pack is
written by a terminal inside the runner, and a crashed run reaches none.

The runner now turns an error of its own while reading a build into an
ordinary failure with a full pack. This module covers what the runner cannot:
a runner that was killed, ran out of memory, or raised somewhere else. When
the coordinator takes the no-terminal path it writes a minimal, honest pack
through the runner's own manifest writer (one writer, one shape):

* ``terminal`` is ``runner-crashed`` and ``reason`` says the runner stopped
  without reporting an end, with the run's status and the runner's error
  text (credentials replaced, cut);
* which tasks finished, from the build's last heartbeat (``in-flight.json``,
  which a killed runner leaves behind) or else the last state the runner
  reported, with that source named; unknown when neither exists;
* ``evidence.subprocess_ran`` is ``null`` (unknown) and ``candidate`` says
  that nothing verified a retained candidate. Nothing here invents a
  worktree, a branch commit or a success. The repair admission reads this and
  refuses with that reason instead of "no readable manifest".

A pack this run already has (same correlation id) is never replaced. Never
raises.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

__all__ = ["CANDIDATE_NOT_VERIFIED", "TASKS_SOURCE_HEARTBEAT", "write_crash_pack"]

#: Provenance of the task counts in a crash pack.
TASKS_SOURCE_HEARTBEAT: str = "in-flight-heartbeat"
TASKS_SOURCE_LAST_REPORTED: str = "last-reported-state"

#: The sentence a crash pack's ``candidate`` block carries.
CANDIDATE_NOT_VERIFIED: str = (
    "not verified — the build's runner stopped without reporting an end, so "
    "nothing checked whether coding began or which candidate it left"
)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _opt_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def write_crash_pack(
    *,
    build_id: str,
    feature_id: str,
    correlation_id: str,
    receipts_root: Path,
    branch: str | None = None,
    run_status: str | None = None,
    runner_error: str | None = None,
    last_values: Mapping[str, Any] | None = None,
) -> Path | None:
    """Write ``failure-manifest.json`` for a run that ended with no terminal.

    Args:
        build_id: The failed build (its receipts folder name).
        feature_id / correlation_id: The build's identity, as the row has it.
        receipts_root: The project's receipts folder
            (:func:`forge.receipts.receipts_root_for`).
        branch: The build row's branch, recorded so the repair's identity
            check matches it.
        run_status: The LangGraph run's status, when it was read.
        runner_error: The runner's error text, when it was read.
        last_values: The thread's last state values, when they were read.

    Returns:
        The manifest path, or ``None`` when nothing was written (this run
        already has a pack, or the write failed).
    """
    try:
        from forge.subagents import build_monitor
        from forge.subagents.autobuild_runner import (
            FAILURE_MANIFEST_NAME,
            IN_FLIGHT_STATE_NAME,
            STDOUT_LOG_NAME,
            TERMINAL_RUNNER_CRASHED,
            _write_failure_manifest,
        )

        pack_dir = Path(receipts_root) / build_id
        existing = _read_json(pack_dir / FAILURE_MANIFEST_NAME)
        if existing is not None and existing.get("correlation_id") == correlation_id:
            logger.info(
                "crash_pack: %s already has this run's failure pack; not "
                "replacing it",
                build_id,
            )
            return None

        heartbeat = _read_json(pack_dir / IN_FLIGHT_STATE_NAME)
        if heartbeat is not None and heartbeat.get("build_id") not in (None, build_id):
            heartbeat = None
        task_counts = None
        if heartbeat is not None and _opt_int(heartbeat.get("tasks_completed")) is not None:
            task_counts = build_monitor.TaskCounts(
                tasks_completed=int(heartbeat["tasks_completed"]),
                tasks_failed=_opt_int(heartbeat.get("tasks_failed")) or 0,
                wave_index=_opt_int(heartbeat.get("current_wave")) or 0,
                source=TASKS_SOURCE_HEARTBEAT,
            )
        else:
            snap = None
            tasks = (last_values or {}).get("async_tasks")
            if isinstance(tasks, Mapping):
                snap = tasks.get(feature_id)
            if isinstance(snap, Mapping) and _opt_int(snap.get("tasks_completed")) is not None:
                task_counts = build_monitor.TaskCounts(
                    tasks_completed=int(snap["tasks_completed"]),
                    tasks_failed=_opt_int(snap.get("tasks_failed")) or 0,
                    wave_index=_opt_int(snap.get("wave_index")) or 0,
                    source=TASKS_SOURCE_LAST_REPORTED,
                )

        if run_status:
            reason = (
                "the build's runner stopped without reporting an end "
                f"(run status: {run_status})"
            )
        else:
            reason = (
                "the build's event stream ended without a terminal and the "
                "run's final state could not be read"
            )
        if runner_error:
            from forge.subagents.autobuild_runner import _plain_error_text

            reason += f": {_plain_error_text(runner_error)}"
        else:
            reason += "; the runner's own error text could not be read"

        return _write_failure_manifest(
            build_id=build_id,
            payload={"feature_id": feature_id, "correlation_id": correlation_id},
            reason=reason,
            timed_out=False,
            exit_code=None,
            worktree_path=None,
            branch=branch or None,
            receipts=None,
            wedged=False,
            task_counts=task_counts,
            terminal=TERMINAL_RUNNER_CRASHED,
            stdout_log=pack_dir / STDOUT_LOG_NAME,
            terminal_class=build_monitor.TERMINAL_CLASS_ERROR,
            terminal_class_evidence=(
                "the build's runner stopped without reporting an end; no "
                "timeout of the build's is known to have fired"
            ),
            runner_error=runner_error,
            receipts_root=Path(receipts_root),
            subprocess_ran=None,
            extra={
                "run_status": run_status,
                "last_heartbeat": heartbeat,
                "candidate": {"verified": False, "branch": None, "why": CANDIDATE_NOT_VERIFIED},
            },
        )
    except Exception as exc:  # noqa: BLE001 — a pack must never mask the failure
        logger.warning(
            "crash_pack: failure pack NOT written for %s (%s: %s) — the "
            "build's failure stands as it was",
            build_id,
            type(exc).__name__,
            exc,
        )
        return None

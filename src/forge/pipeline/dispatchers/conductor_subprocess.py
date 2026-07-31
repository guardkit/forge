"""The conductor→dispatcher seam — one adapter, two translations.

Conductor revival Stage 2, shakeout items 1 and 2
(``supervisor-revival-design-pass-2026-07-31``).

The gap this closes
-------------------

The supervisor's Mode C turn calls its injected ``subprocess_dispatcher``
with a *supervisor* vocabulary::

    stage, build_id, feature_id, rationale, [fix_task], [forward_context]

:func:`~forge.pipeline.dispatchers.subprocess.dispatch_subprocess_stage`
speaks a *dispatcher* vocabulary and shares only the first three names. Two
things were therefore broken at this seam and neither could be seen from
either side alone:

1. **``task_id`` never arrived.** ``task-review`` is dispatched BY SUBJECT
   and the dispatcher refuses a subject-less fix-journey dispatch rather
   than review an inferred one — so the very first turn of every fix
   journey failed. The subject lives on the build row (``builds.task_id``,
   ``schema_v8.sql``), which is a durable read, not something the
   supervisor holds; binding it belongs here, in the layer that reads the
   row.
2. **``rationale`` and ``forward_context`` had no parameters.** The
   dispatcher declares neither and has no ``**kwargs``, so the call would
   have raised ``TypeError`` before doing any work.

The cure is split along a line, not smeared:

* ``forward_context`` is a genuine dispatcher concern — it is context for
  the subprocess — so the dispatcher gained the parameter *deliberately*,
  and it is the ONE source of forward context for a fix-journey stage
  (see ``_build_argv_for_stage``).
* ``rationale`` is supervisor vocabulary — the planner's reason for
  choosing this stage. It is not a subprocess argument and never becomes
  one. This adapter records it on the audit trail and drops it at the
  boundary, which is where a word that does not cross a boundary should
  be dropped.

Everything else this closure supplies (the repo path, the allowlists, the
runner, the stage_log writer, the per-dispatch correlation id) is
composition-root knowledge the supervisor deliberately does not hold.

The exact-match identity law (FTR, design pass §b.3)
----------------------------------------------------

**Every stage dispatch mints its own correlation id.** A fix journey runs N
``/task-work`` siblings per cycle, and resolving a terminal on anything
looser than THIS dispatch's id is how a healthy build gets killed by a
sibling's failure — which has happened once already on the routine path.
The build's own correlation id is the prefix so a human can still group a
journey by eye; the suffix makes each dispatch's identity exact.

Domain-adjacent module: no NATS, no SQLite types. The build row arrives
through an injected reader.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from forge.pipeline.dispatchers.subprocess import (
    MODE_C_STAGES,
    StageDispatchResult,
    StageDispatchStatus,
    dispatch_subprocess_stage,
)
from forge.pipeline.stage_taxonomy import StageClass

logger = logging.getLogger(__name__)

__all__ = [
    "make_conductor_subprocess_dispatcher",
    "mint_stage_correlation_id",
]


def mint_stage_correlation_id(
    *, build_correlation_id: str, stage: StageClass, subject: str | None
) -> str:
    """Mint this dispatch's own correlation id (the FTR exact-match law).

    Shape: ``<build corr>:<stage>:<subject>:<8 hex>``. The prefix keeps a
    journey greppable as one thing; the random tail makes every dispatch's
    identity unique, so a terminal resolves against exactly the dispatch
    that produced it and never against a sibling's.
    """
    tail = uuid.uuid4().hex[:8]
    parts = [build_correlation_id or "no-corr", stage.value]
    if subject:
        parts.append(subject)
    parts.append(tail)
    return ":".join(parts)


def make_conductor_subprocess_dispatcher(
    *,
    build_row_reader: Callable[[str], Any],
    read_allowlist: list[Path],
    worktree_allowlist: Any,
    forward_context_builder: Any,
    stage_log_writer: Any,
    subprocess_runner: Any,
    dispatch: Callable[..., Awaitable[Any]] = dispatch_subprocess_stage,
    repo_path_reader: Callable[[Any], Path | None] | None = None,
    correlation_id_minter: Callable[..., str] = mint_stage_correlation_id,
    timeout_seconds: int = 600,
    with_nats_streaming: bool = True,
) -> Callable[..., Awaitable[Any]]:
    """Build the ``subprocess_dispatcher`` the conductor's Supervisor calls.

    Args:
        build_row_reader: ``(build_id) -> BuildRow | None``. Production
            passes ``sqlite_pool.get_build_row``. The row is the durable
            anchor for the journey's subject (``task_id``), its worktree,
            its fix-task YAML and its correlation id — all of which must
            survive a daemon restart mid-journey.
        read_allowlist: Filesystem allowlist forwarded to the runner.
        worktree_allowlist: FEAT-FORGE-005 allowlist, forwarded for the
            output-side artefact re-check.
        forward_context_builder: The daemon's shipped builder. Threaded
            through for the dispatcher's signature; for a fix-journey stage
            the CONDUCTOR's context wins (see the module docstring), so
            this is the planning-stage path's builder, unchanged.
        stage_log_writer: The fix journey's ``stage_log`` writer — the
            ``fix_tasks`` producer
            (:func:`forge.cli._serve_deps_stage_log.build_fix_journey_stage_log_writer`).
            When it exposes ``for_fix_task``, a ``task-work`` dispatch is
            written through a writer bound to the fix task it is working,
            so the row is attributable and the planner's walk cannot
            dispatch the same fix twice.
        subprocess_runner: Async runner with the ``guardkit.run`` shape.
        dispatch: The dispatcher under the adapter. Injected so a test can
            observe the translated kwargs without a subprocess.
        repo_path_reader: ``(row) -> Path | None``. Defaults to the row's
            ``worktree_path`` — the build's own worktree, which is where a
            fix journey's stages must run.
        correlation_id_minter: See :func:`mint_stage_correlation_id`.
        timeout_seconds / with_nats_streaming: Forwarded verbatim.

    Returns:
        ``async (**supervisor_kwargs) -> StageDispatchResult``.
    """

    def _repo_path(row: Any) -> Path | None:
        if repo_path_reader is not None:
            return repo_path_reader(row)
        raw = getattr(row, "worktree_path", None)
        return Path(raw) if raw else None

    async def conductor_subprocess_dispatcher(
        *,
        stage: StageClass,
        build_id: str,
        feature_id: str | None = None,
        rationale: str = "",
        fix_task: Any = None,
        forward_context: Mapping[str, Any] | None = None,
        **unexpected: Any,
    ) -> Any:
        if unexpected:
            # A new supervisor kwarg with no translation here would be
            # silently dropped otherwise — and a silently dropped dispatch
            # argument is exactly how this seam broke in the first place.
            logger.error(
                "conductor dispatcher adapter: unrecognised supervisor "
                "kwarg(s) %s for stage=%s build_id=%s — DROPPED. Add a "
                "translation rather than letting it vanish",
                sorted(unexpected),
                getattr(stage, "value", stage),
                build_id,
            )

        row = None
        try:
            row = build_row_reader(build_id)
        except Exception as exc:  # noqa: BLE001 — a read defect is not a crash
            logger.error(
                "conductor dispatcher adapter: build row read raised %s: %s "
                "for build_id=%s — dispatching with no durable anchors",
                type(exc).__name__,
                exc,
                build_id,
            )

        task_id = getattr(row, "task_id", None) if row is not None else None
        fix_task_yaml = (
            getattr(row, "feature_yaml_path", None) if row is not None else None
        )
        build_correlation_id = (
            getattr(row, "correlation_id", "") if row is not None else ""
        )

        if stage in MODE_C_STAGES and stage is StageClass.TASK_REVIEW and not task_id:
            # Say it HERE, where the cause is legible, before the
            # dispatcher's (correct, structured) refusal fires downstream.
            logger.error(
                "conductor dispatcher adapter: build_id=%s carries no "
                "builds.task_id — a fix journey's review has no subject. The "
                "queue writes it for every mode-c build (schema_v8); a row "
                "without one predates the column or was not queued as a fix "
                "journey",
                build_id,
            )

        subject = (
            str(getattr(fix_task, "fix_task_id", "") or "")
            if stage is StageClass.TASK_WORK
            else (str(task_id) if task_id else "")
        )
        correlation_id = correlation_id_minter(
            build_correlation_id=build_correlation_id,
            stage=stage,
            subject=subject or None,
        )

        writer = stage_log_writer
        if stage is StageClass.TASK_WORK:
            bind = getattr(stage_log_writer, "for_fix_task", None)
            if callable(bind):
                writer = bind(subject or None)

        repo_path = _repo_path(row) if row is not None else None
        if repo_path is None:
            # No worktree, no dispatch. Running a fix journey's stage from
            # an inferred directory is the filesystem twin of reviewing an
            # inferred subject — refuse, structured, in the dispatcher's
            # own result shape so the supervisor sees one uniform outcome.
            reason = (
                f"fix-journey stage {getattr(stage, 'value', stage)!r} has no "
                f"worktree path on build_id={build_id!r}; refusing rather "
                "than run the build system from an inferred directory"
            )
            logger.error("conductor dispatcher adapter: %s", reason)
            return StageDispatchResult(
                status=StageDispatchStatus.FAILED,
                stage=stage,
                build_id=build_id,
                feature_id=None,
                correlation_id=correlation_id,
                artefact_paths=(),
                rationale=reason,
                exit_code=-1,
                duration_secs=0.0,
                subcommand=getattr(stage, "value", str(stage)),
            )

        logger.info(
            "conductor dispatcher adapter: %s build_id=%s subject=%s "
            "correlation_id=%s (planner rationale: %s)",
            getattr(stage, "value", stage),
            build_id,
            subject or "none",
            correlation_id,
            rationale or "none",
        )

        return await dispatch(
            stage,
            build_id,
            correlation_id=correlation_id,
            repo_path=repo_path,
            read_allowlist=read_allowlist,
            forward_context_builder=forward_context_builder,
            worktree_allowlist=worktree_allowlist,
            stage_log_writer=writer,
            subprocess_runner=subprocess_runner,
            feature_id=feature_id,
            task_id=str(task_id) if task_id else None,
            fix_task=fix_task,
            fix_task_yaml=str(fix_task_yaml) if fix_task_yaml else None,
            forward_context=forward_context,
            timeout_seconds=timeout_seconds,
            with_nats_streaming=with_nats_streaming,
        )

    return conductor_subprocess_dispatcher

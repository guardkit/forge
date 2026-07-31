"""The fix task's forward context — the review's findings AND the failure pack.

Revival design pass §a.3 / §b.2 (``supervisor-revival-design-pass-2026-07-31``),
Stage 1c.

The conductor's ``fix_task_context_builder`` seam is consulted once per
``/task-work`` dispatch (``supervisor.py`` Mode C turn). Until now it was
``None`` in production, so a fix task was dispatched with its fix-task
reference alone.

This adapter fills it, and does exactly two things:

1. **Delegates** to the shipped
   :class:`~forge.pipeline.forward_context_builder.ForwardContextBuilder`
   for the review→work data dependency — the ``--fix-task`` entry plus one
   allow-listed ``--context`` entry per review artefact. That builder owns
   the allowlist gating; this adapter never re-implements it.
2. **Extends** the context with the failed build's **failure pack** index
   (:mod:`forge.pipeline.fix_journey_receipts`) so the fix task starts
   from the evidence the failed build left rather than from a reason
   string.

It is an *adapter*, not a second builder: no allowlist logic, no stage_log
reads, no path arithmetic of its own.

Never raises. A pack that cannot be read degrades to "no pack" — the
supervisor's own call site also guards, but a context builder that can
kill a fix journey would be the wrong shape to hand it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Mapping

from forge.lifecycle.modes import BuildMode
from forge.pipeline.fix_journey_receipts import read_failure_pack
from forge.pipeline.stage_taxonomy import StageClass

logger = logging.getLogger(__name__)

__all__ = ["FixTaskContextBuilder"]


class FixTaskContextBuilder:
    """Adapter over :class:`ForwardContextBuilder` that also reads the pack.

    Call shape matches the supervisor's ``fix_task_context_builder``
    field exactly — ``(stage, build_id, fix_task) -> Mapping[str, Any]``
    — so it drops straight onto the dataclass.

    Args:
        forward_context_builder: The shipped Mode C forward-context
            builder. Consulted through its public ``build_for``.
        source_build_id_reader: ``(fix_build_id) -> str | None`` — which
            FAILED build's pack this journey is repairing. ``None`` (the
            default) reads the pack from the fix journey's OWN build id,
            which is where the queue step that mints a fix build from a
            terminal failure lands it (design pass §b.2). Injected rather
            than derived because the ``builds`` table carries no
            parent-build column today — an honest seam, not a guess.
        receipts_root: Injectable receipts root (tests point it at a
            ``tmp_path``); ``None`` uses the routine path's own law.
        review_artefact_paths_reader: ``(build_id, fix_task) ->
            Iterable[str]`` — the artefact paths the originating
            ``/task-review`` emitted, which the forward-context builder
            gates through the worktree allowlist and threads onto
            ``--context``. ``None`` (the default) yields no paths; the
            review's findings then reach the fix task through the
            failure-pack index alone. See :meth:`_translate_fix_task`.
    """

    def __init__(
        self,
        forward_context_builder: Any,
        *,
        source_build_id_reader: Callable[[str], str | None] | None = None,
        receipts_root: "Path | str | None" = None,
        review_artefact_paths_reader: Callable[[str, Any], Any] | None = None,
    ) -> None:
        self._forward = forward_context_builder
        self._source_reader = source_build_id_reader
        self._receipts_root = receipts_root
        self._review_artefact_paths_reader = review_artefact_paths_reader

    def __call__(
        self,
        stage: StageClass,
        build_id: str,
        fix_task: Any,
    ) -> Mapping[str, Any]:
        """Return the forward context for one ``/task-work`` dispatch.

        Returns:
            ``{"context_entries": [...], "failure_pack": {...} | None}``.
            ``context_entries`` is a list of plain
            ``{"flag", "value", "kind"}`` dicts so the mapping is
            JSON-safe end to end (it rides a dispatch payload and a
            ``stage_log`` row).
        """
        entries = self._build_entries(stage, build_id, fix_task)
        pack = self._read_pack(build_id)
        context: dict[str, Any] = {
            "context_entries": entries,
            "failure_pack": pack.to_context() if pack is not None else None,
        }
        return context

    # -- internals ----------------------------------------------------

    def _translate_fix_task(self, build_id: str, fix_task: Any) -> Any:
        """Translate the PLANNER's fix-task ref into the BUILDER's.

        Two distinct ``FixTaskRef`` types exist in the tree and the
        conductor sits between them:

        * :class:`forge.pipeline.mode_c_planner.FixTaskRef` — what the
          planner mints and the supervisor threads (``fix_task_id`` /
          ``review_history_index`` / ``review_stage_label``).
        * :class:`forge.pipeline.forward_context_builder.FixTaskRef` —
          what ``build_for`` consumes (``fix_task_id`` /
          ``task_review_entry_id`` / ``review_artefact_paths``), and whose
          ``to_json()`` becomes the ``--fix-task`` argv payload.

        Handing the planner's value straight to the builder raised
        ``AttributeError: 'FixTaskRef' object has no attribute 'to_json'``
        — which this adapter's own except-clause then swallowed into "no
        forward context entries". The fix task was dispatched with the
        review's findings silently missing, and nothing said so. The
        translation is the adapter's actual job; doing it here is what
        makes the ``--fix-task`` entry appear at all.

        ``review_artefact_paths`` come from the injected
        ``review_artefact_paths_reader`` when one is wired; absent it they
        are empty, and the review's findings ride the failure-pack index
        instead. Empty is honest — a guessed path list is not.
        """
        from forge.pipeline.forward_context_builder import (
            FixTaskRef as ForwardFixTaskRef,
        )

        if fix_task is None or isinstance(fix_task, ForwardFixTaskRef):
            return fix_task
        fix_task_id = getattr(fix_task, "fix_task_id", None)
        if not fix_task_id:
            return fix_task
        entry_id = getattr(fix_task, "task_review_entry_id", None)
        if not entry_id:
            # The planner's back-reference is an INDEX into its history,
            # not a stage_log entry_id. Render it as the audit anchor it
            # is rather than inventing a row identifier.
            label = getattr(fix_task, "review_stage_label", "task-review")
            index = getattr(fix_task, "review_history_index", None)
            entry_id = f"{label}#{index}" if index is not None else str(label)
        paths: tuple[str, ...] = ()
        if self._review_artefact_paths_reader is not None:
            try:
                raw = self._review_artefact_paths_reader(build_id, fix_task)
                paths = tuple(str(p) for p in (raw or ()))
            except Exception as exc:  # noqa: BLE001 — a reader defect is not fatal
                logger.warning(
                    "fix_task_context_builder: review_artefact_paths_reader "
                    "raised %s: %s for build_id=%s fix_task_id=%s — the fix "
                    "task carries no review artefact paths",
                    type(exc).__name__,
                    exc,
                    build_id,
                    fix_task_id,
                )
        return ForwardFixTaskRef(
            fix_task_id=str(fix_task_id),
            task_review_entry_id=str(entry_id),
            review_artefact_paths=paths,
        )

    def _build_entries(
        self, stage: StageClass, build_id: str, fix_task: Any
    ) -> list[dict[str, Any]]:
        try:
            entries = self._forward.build_for(
                stage,
                build_id,
                None,
                mode=BuildMode.MODE_C,
                fix_task=self._translate_fix_task(build_id, fix_task),
            )
        except Exception as exc:  # noqa: BLE001 — a builder defect is not fatal
            logger.warning(
                "fix_task_context_builder: forward-context build_for raised "
                "%s: %s for build_id=%s stage=%s — dispatching with no "
                "forward context entries",
                type(exc).__name__,
                exc,
                build_id,
                getattr(stage, "value", stage),
            )
            return []
        rendered: list[dict[str, Any]] = []
        for entry in entries or ():
            rendered.append(
                {
                    "flag": getattr(entry, "flag", None),
                    "value": getattr(entry, "value", None),
                    "kind": getattr(entry, "kind", None),
                }
            )
        return rendered

    def _read_pack(self, build_id: str):
        source_build_id = build_id
        if self._source_reader is not None:
            try:
                resolved = self._source_reader(build_id)
            except Exception as exc:  # noqa: BLE001 — reader defect is not fatal
                logger.warning(
                    "fix_task_context_builder: source_build_id_reader raised "
                    "%s: %s for build_id=%s — falling back to the fix "
                    "journey's own receipts directory",
                    type(exc).__name__,
                    exc,
                    build_id,
                )
                resolved = None
            if resolved:
                source_build_id = resolved
        return read_failure_pack(
            source_build_id, receipts_root=self._receipts_root
        )

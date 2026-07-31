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
    """

    def __init__(
        self,
        forward_context_builder: Any,
        *,
        source_build_id_reader: Callable[[str], str | None] | None = None,
        receipts_root: "Path | str | None" = None,
    ) -> None:
        self._forward = forward_context_builder
        self._source_reader = source_build_id_reader
        self._receipts_root = receipts_root

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

    def _build_entries(
        self, stage: StageClass, build_id: str, fix_task: Any
    ) -> list[dict[str, Any]]:
        try:
            entries = self._forward.build_for(
                stage,
                build_id,
                None,
                mode=BuildMode.MODE_C,
                fix_task=fix_task,
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

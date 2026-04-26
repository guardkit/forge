"""Stage-event write-ordering guard (TASK-IC-003).

Every stage-completion hook in Forge funnels through a single helper —
:func:`record_stage_event` — so the *authoritative* SQLite write
(FEAT-FORGE-001) always commits **before** the fire-and-forget Graphiti
mirror (TASK-IC-002) is dispatched. The ordering is the load-bearing
invariant that lets reconcile-backfill (TASK-IC-004) treat the SQLite
ledger as the single source of truth: anything in SQLite but missing
from Graphiti can be backfilled deterministically; anything in Graphiti
without a SQLite anchor would be an orphan we cannot reconcile.

This module is deliberately a *thin coordinator*. It owns no retry
logic, no compensation, and no logging beyond what the underlying
helpers already emit:

* The SQLite repository's ``persist_to_sqlite`` callable is responsible
  for its own durability semantics. We invoke it synchronously and let
  any exception propagate verbatim — the caller will roll up the
  failure into the pipeline state machine.
* :func:`forge.memory.writer.fire_and_forget_write` already swallows
  every Graphiti-side failure into a structured log line, so a Graphiti
  dispatch failure must NEVER undo the SQLite commit. Compensation for
  the resulting drift is reconcile-backfill's job (TASK-IC-004).

Acceptance criteria from ``TASK-IC-003-write-ordering-guard.md``:

* AC-001 — :func:`record_stage_event` is the single helper every stage
  hook calls.
* AC-002 — SQLite commit happens BEFORE Graphiti dispatch (no
  parallelism, no reordering).
* AC-003 — If the SQLite commit raises, Graphiti dispatch does NOT
  happen (no orphan Graphiti writes without a SQLite anchor).
* AC-004 — If Graphiti dispatch fails, the SQLite entry is still
  durable (verified by reconcile-backfill picking it up next build).
* AC-005 — ``@edge-case write-ordering``: the SQLite-committed
  timestamp precedes the Graphiti dispatch timestamp.
"""

from __future__ import annotations

from typing import Callable

from .writer import PipelineHistoryEntity, fire_and_forget_write

__all__ = ["record_stage_event"]


def record_stage_event(
    persist_to_sqlite: Callable[[], PipelineHistoryEntity],
    group_id: str,
) -> PipelineHistoryEntity:
    """Commit to SQLite first, then dispatch the Graphiti mirror.

    The contract is intentionally narrow: this is the **only** helper
    every stage-completion hook should call. Centralising the ordering
    in one place keeps the SQLite-first / Graphiti-second invariant
    out of every individual call site.

    Sequencing:

    1. ``persist_to_sqlite()`` is invoked synchronously. Its return
       value — a pydantic ``PipelineHistoryEntity`` carrying the
       SQLite-row UUID as ``entity_id`` (ASSUM-007 resolution) — is
       captured for the Graphiti dispatch.
    2. *Only after* the SQLite call returns does
       :func:`fire_and_forget_write` run, scheduling the Graphiti
       mirror without blocking the caller.
    3. The persisted entity is returned to the caller so the stage
       hook can stamp it into the pipeline state machine.

    Failure modes:

    * SQLite raises — the exception propagates verbatim. Graphiti
      dispatch is **not** invoked (AC-003): no orphan Graphiti write
      can exist without its SQLite anchor.
    * Graphiti raises — :func:`fire_and_forget_write` is contractually
      forbidden from raising, so any failure is logged structurally
      and the SQLite entry remains durable (AC-004). Reconcile-
      backfill (TASK-IC-004) will pick the entry up on the next build.

    Why a callable rather than the entity directly:

    The repository abstraction — not :mod:`forge.memory` — owns the
    SQLite transaction. Passing a callable lets the caller frame the
    transaction (e.g. ``with session.begin():``) and lets this helper
    stay I/O-free except for the dispatch call. The callable returns
    the persisted entity so we can dispatch the *post-commit* row,
    which is the only row guaranteed to carry the final
    SQLite-assigned ``entity_id``.

    Args:
        persist_to_sqlite: A zero-argument callable that performs the
            authoritative SQLite write and returns the persisted
            entity. MUST raise on commit failure — the helper relies
            on the exception to skip Graphiti dispatch.
        group_id: Graphiti group_id, e.g. ``"forge_pipeline_history"``
            or ``"forge_calibration_history"``. Forwarded verbatim to
            :func:`fire_and_forget_write`; that function validates the
            value, so we do not pre-validate here.

    Returns:
        The :class:`~forge.memory.writer.PipelineHistoryEntity`
        produced by ``persist_to_sqlite()``.

    Raises:
        Exception: Any exception raised by ``persist_to_sqlite`` is
            propagated verbatim. The Graphiti dispatch is not
            attempted in that case.
    """
    entity = persist_to_sqlite()
    fire_and_forget_write(entity, group_id)
    return entity

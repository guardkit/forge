"""Async autobuild runner subagent (TASK-FW10-002, FEAT-FORGE-010).

This module is the production implementation of the long-running
autobuild stage. The supervisor dispatches it via
DeepAgents ``start_async_task`` (per ADR-ARCH-031); the compiled
:data:`graph` exported here is the addressable surface the
``AsyncSubAgentMiddleware`` looks up by ``graph_id="autobuild_runner"``
when ``langgraph.json`` resolves the ``autobuild_runner`` entry.

DDR-006 + DDR-007 — single transition site
==========================================

DDR-006 defines the ``async_tasks`` state-channel entry shape
(:class:`AutobuildState`) and mandates that every lifecycle transition
flow through one ``_update_state(...)`` helper. DDR-007 places the
``PipelineLifecycleEmitter`` call at the **same** boundary:

.. code-block:: text

    state-channel write   ─┐
                           ├── inside _update_state(), one function call
    emitter.on_transition ─┘

If a transition writes the channel but skips the emit (or vice versa),
operators see inconsistent live progress — that is a test failure (see
``tests/forge/test_autobuild_runner.py``).

Lifecycle progression (per DDR-006 ``Literal``):

.. code-block:: text

    starting → planning_waves → running_wave → awaiting_approval
              → pushing_pr → completed | cancelled | failed

ASSUM-018 — stage-complete envelope shape
=========================================

When the runner emits ``stage_complete`` from inside the subagent, the
envelope's ``target_kind`` is ``"subagent"`` and ``target_identifier``
is the runner's own ``task_id`` (the value returned by
``start_async_task``). The supervisor's emits for stages dispatched
*outside* the subagent retain the existing taxonomy.

Worktree confinement (Group E security scenario)
================================================

Filesystem writes performed by the subagent must fall under the
build's worktree allowlist. :func:`assert_within_worktree` resolves
the candidate path and rejects anything escaping the supplied root
(symlink-aware via :meth:`Path.resolve`).

Failure-mode contract (ADR-ARCH-008, DDR-007 §Failure-mode contract)
====================================================================

If the emitter call raises (NATS publish failure, broker outage, etc.)
the runner logs at ``WARNING`` and continues. SQLite remains the
authoritative source of truth; the build is not regressed by a
transient publish hiccup.

Forward compatibility
=====================

The subagent receives the emitter as an in-process Python object via
the ``start_async_task`` context payload (DDR-007 Option A). This
relies on DeepAgents ``0.5.3`` accepting non-serialisable context
under ASGI co-deployment (per ADR-ARCH-031). The smoke test in
``tests/forge/test_autobuild_runner.py`` exercises this contract; if
DeepAgents rejects the in-process emitter, the test is the canary —
the F3 risk on FEAT-FORGE-010 is that a runtime upgrade silently
flips this contract.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Literal,
    Protocol,
    TextIO,
    get_args,
    runtime_checkable,
)

import yaml

from langgraph.graph.message import add_messages
from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import NotRequired, Required, TypedDict

if TYPE_CHECKING:  # pragma: no cover - import-time only
    from forge.pipeline import BuildContext, PipelineLifecycleEmitter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifecycle literal & validation set (DDR-006)
# ---------------------------------------------------------------------------


#: DDR-006 lifecycle literal. Order mirrors the canonical progression
#: ``starting → planning_waves → running_wave → awaiting_approval →
#: pushing_pr → completed | cancelled | failed``. Adding states requires
#: a DDR-006 update — the literal is the contract.
AutobuildLifecycle = Literal[
    "starting",
    "planning_waves",
    "running_wave",
    "awaiting_approval",
    "pushing_pr",
    "completed",
    "cancelled",
    "failed",
]


#: Frozen view of the lifecycle literal — used for membership checks at
#: ``_update_state`` boundary so an out-of-set string raises
#: :class:`ValueError` instead of silently writing a corrupt entry.
LIFECYCLE_VALUES: frozenset[str] = frozenset(get_args(AutobuildLifecycle))


#: Terminal lifecycle states (DDR-006). Once a state-channel entry is
#: in one of these, no further transitions are emitted from the
#: subagent. The supervisor reads the terminal state via
#: ``check_async_task`` and reconciles with SQLite on restart.
TERMINAL_LIFECYCLES: frozenset[str] = frozenset({"completed", "cancelled", "failed"})


#: Subagent name registered with DeepAgents ``AsyncSubAgentMiddleware``.
#: Mirrors :data:`forge.pipeline.dispatchers.autobuild_async.AUTOBUILD_RUNNER_NAME`
#: — re-exported here so the runner module is self-contained.
AUTOBUILD_RUNNER_NAME: str = "autobuild_runner"


# ---------------------------------------------------------------------------
# AutobuildState — Pydantic model (DDR-006)
# ---------------------------------------------------------------------------


class AutobuildState(BaseModel):
    """Pydantic model for one ``async_tasks`` state-channel entry.

    Schema is verbatim from DDR-006. Serialised to ``dict`` when written
    to the LangGraph state channel (LangGraph channel requirement). The
    ``model_config`` uses ``extra="ignore"`` so additive evolution does
    not break older readers.

    Attributes:
        task_id: Identifier returned by ``start_async_task``.
        build_id: Build the autobuild belongs to.
        feature_id: Feature the autobuild targets.
        lifecycle: Current lifecycle string — must appear in
            :data:`LIFECYCLE_VALUES`.
        wave_index: 0-indexed current wave.
        wave_total: Total wave count for the autobuild.
        task_index: 0-indexed task within the current wave.
        task_total: Total tasks in the current wave.
        current_task_label: Reasoning-model-chosen description of the
            in-flight task (or None when between tasks).
        tasks_completed: Cumulative completed task count.
        tasks_failed: Cumulative failed task count.
        last_coach_score: Coach quality score for the most recent task,
            or None.
        aggregate_coach_score: Weighted average across completed tasks,
            or None.
        waiting_for: Set when ``lifecycle="awaiting_approval"`` (e.g.
            ``"approval:Architecture Review"``); cleared on resume.
        pending_directives: Supervisor-injected directives queued via
            ``update_async_task``.
        started_at: UTC timestamp when ``start_async_task`` minted this
            entry.
        last_activity_at: UTC timestamp of the most recent state mutation
            — refreshed on every ``_update_state`` invocation.
        estimated_completion_at: UTC ETA computed from tasks remaining
            and per-task average duration (or None).
        worktree_path: Absolute path to the build's worktree allowlist
            root. Used by :func:`assert_within_worktree` for filesystem
            confinement.
        correlation_id: Originating correlation ID threaded through the
            dispatch (FEAT-FORGE-002).
    """

    model_config = ConfigDict(extra="ignore")

    # Identity
    task_id: str
    build_id: str
    feature_id: str

    # Progress
    lifecycle: AutobuildLifecycle
    wave_index: int = 0
    wave_total: int = 0
    task_index: int = 0
    task_total: int = 0
    current_task_label: str | None = None
    tasks_completed: int = 0
    tasks_failed: int = 0

    # Quality
    last_coach_score: float | None = None
    aggregate_coach_score: float | None = None

    # Approval coupling (ADR-ARCH-021)
    waiting_for: str | None = None

    # Steering
    pending_directives: list[str] = Field(default_factory=list)

    # Timing
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_activity_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    estimated_completion_at: datetime | None = None

    # Confinement (Group E security scenario)
    worktree_path: str | None = None

    # Correlation (FEAT-FORGE-002)
    correlation_id: str | None = None


# ---------------------------------------------------------------------------
# Protocols (the only I/O surfaces the subagent depends on)
# ---------------------------------------------------------------------------


@runtime_checkable
class SubagentEmitter(Protocol):
    """Sync structural Protocol for the DDR-007 transition publish hook.

    The subagent calls ``emitter.on_transition(new_state)`` from inside
    :func:`_update_state` at the same boundary as the state-channel
    write. The Protocol is structural (``runtime_checkable``) so any
    object exposing a sync ``on_transition(state)`` method satisfies it
    — the production wiring threads an adapter around
    :class:`forge.pipeline.PipelineLifecycleEmitter` whose async
    ``emit_*`` methods are scheduled by the daemon's running event loop.
    """

    def on_transition(self, state: AutobuildState) -> None: ...


@runtime_checkable
class StateChannelWriter(Protocol):
    """Sync Protocol for the DDR-006 ``async_tasks`` channel writer.

    Production wires the LangGraph ``AsyncSubAgentMiddleware``
    ``async_tasks`` reducer; tests inject an in-memory recording fake.
    Calls are upsert-shaped on ``(build_id, feature_id, task_id)``.
    """

    def write(self, state: AutobuildState) -> None: ...


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _NullStateWriter:
    """No-op writer used as the default for :func:`_update_state`.

    The ASGI co-deployed runtime threads its own writer in via the
    ``AsyncSubAgentMiddleware`` reducer; tests that want to assert
    state-channel writes inject their own recording fake. Using a real
    object (not ``None``) keeps the call site linear and avoids an
    ``if writer is None`` branch around every transition.
    """

    def __init__(self) -> None:
        self.writes: list[AutobuildState] = []

    def write(self, state: AutobuildState) -> None:
        # Record so a default-constructed runner is still introspectable
        # in tests without forcing every caller to inject a writer.
        self.writes.append(state)


# ---------------------------------------------------------------------------
# Worktree confinement helper (Group E security scenario)
# ---------------------------------------------------------------------------


class WorktreeConfinementError(ValueError):
    """Raised when a filesystem write would escape the worktree allowlist."""


def assert_within_worktree(
    path: str | os.PathLike[str],
    worktree_root: str | os.PathLike[str],
) -> Path:
    """Resolve ``path`` and verify it falls under ``worktree_root``.

    Returns the resolved absolute :class:`Path` on success; raises
    :class:`WorktreeConfinementError` otherwise. Resolution uses
    :meth:`Path.resolve` so symlinks pointing outside the worktree
    root are caught alongside literal ``../`` escapes.

    Args:
        path: Filesystem path the subagent is about to write.
        worktree_root: The build's worktree allowlist root (per
            ``forward_context.worktree_path``).

    Raises:
        WorktreeConfinementError: ``path`` resolves outside
            ``worktree_root``, or ``worktree_root`` is empty.
    """
    if not worktree_root or not os.fspath(worktree_root).strip():
        raise WorktreeConfinementError(
            "worktree_root must be a non-empty path; refusing to evaluate "
            f"confinement of {path!r}"
        )
    root = Path(os.fspath(worktree_root)).resolve()
    candidate = Path(os.fspath(path)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise WorktreeConfinementError(
            f"path {candidate} escapes worktree allowlist {root}"
        ) from exc
    return candidate


# ---------------------------------------------------------------------------
# _update_state — co-locates the DDR-006 write and DDR-007 emit
# ---------------------------------------------------------------------------


def _update_state(
    state: AutobuildState,
    *,
    emitter: SubagentEmitter,
    lifecycle: str | None = None,
    state_writer: StateChannelWriter | None = None,
    **deltas: Any,
) -> AutobuildState:
    """Apply state mutations and fire the publish hook in one boundary.

    Co-locates the DDR-006 ``async_tasks`` channel write and the
    DDR-007 ``emitter.on_transition`` call. Either both happen or
    neither does — the function is intentionally tight so the boundary
    cannot drift between two destinations the subagent must keep
    consistent (DDR-006 §Consequences).

    Args:
        state: Current :class:`AutobuildState`.
        emitter: A :class:`SubagentEmitter` (DDR-007). The transition
            publish runs *after* the state-channel write so observers
            never see an emitted lifecycle that is missing from the
            channel.
        lifecycle: New lifecycle string. Must appear in
            :data:`LIFECYCLE_VALUES` or :class:`ValueError` is raised.
            ``None`` keeps the current lifecycle (valid for delta-only
            updates such as ``current_task_label`` bumps); the emitter
            is still notified — every state mutation is observable.
        state_writer: A :class:`StateChannelWriter` for the
            ``async_tasks`` channel. When omitted defaults to the
            local :class:`_NullStateWriter` (used by tests that only
            care about the emitter side of the boundary; production
            threads a real writer).
        **deltas: Forwarded to :meth:`AutobuildState.model_copy`'s
            ``update=`` mapping. ``last_activity_at`` is always
            refreshed so observers can tell stale entries from active
            ones.

    Returns:
        The new :class:`AutobuildState`.

    Raises:
        ValueError: ``lifecycle`` is provided but is not a member of
            :data:`LIFECYCLE_VALUES`.
    """
    if lifecycle is not None and lifecycle not in LIFECYCLE_VALUES:
        raise ValueError(
            f"_update_state: lifecycle {lifecycle!r} is not in DDR-006's "
            f"literal set; allowed values are {sorted(LIFECYCLE_VALUES)!r}"
        )

    update_map: dict[str, Any] = {
        "last_activity_at": datetime.now(timezone.utc),
        **deltas,
    }
    if lifecycle is not None:
        update_map["lifecycle"] = lifecycle

    new_state = state.model_copy(update=update_map)

    # DDR-006: write the async_tasks channel FIRST. Observers (e.g.
    # ``forge status``) reading the channel before the emit fires see a
    # consistent view; if the emit then fails (NATS down, etc.) the
    # channel still reflects the new state.
    writer = state_writer if state_writer is not None else _NullStateWriter()
    writer.write(new_state)

    # DDR-007: emit at the SAME boundary. Failures are caught and
    # logged at WARNING per DDR-007 §Failure-mode contract — SQLite
    # (and the just-written async_tasks entry) remain authoritative
    # so the build does not regress on a transient publish hiccup
    # (ADR-ARCH-008).
    try:
        emitter.on_transition(new_state)
    except Exception as exc:  # noqa: BLE001 — DDR-007 demands swallow+log
        logger.warning(
            "autobuild_runner: emitter.on_transition raised %s for "
            "task_id=%s lifecycle=%s — build continues; SQLite remains "
            "authoritative (ADR-ARCH-008, DDR-007 §Failure-mode contract)",
            exc,
            new_state.task_id,
            new_state.lifecycle,
        )

    return new_state


# ---------------------------------------------------------------------------
# Stage-complete envelope helper (ASSUM-018)
# ---------------------------------------------------------------------------


def build_stage_complete_kwargs(state: AutobuildState) -> dict[str, str]:
    """Return the ``target_kind`` / ``target_identifier`` pair (ASSUM-018).

    When the runner emits ``stage_complete`` from inside the subagent
    the envelope MUST be tagged ``target_kind="subagent"`` with
    ``target_identifier`` equal to the runner's own ``task_id``. The
    supervisor's emits (for stages dispatched *outside* the subagent)
    use the existing taxonomy unchanged.

    Args:
        state: The :class:`AutobuildState` whose ``task_id`` identifies
            this subagent instance.

    Returns:
        Mapping suitable for splat into
        :meth:`PipelineLifecycleEmitter.emit_stage_complete` keyword
        arguments.

    Raises:
        ValueError: ``state.task_id`` is empty.
    """
    if not state.task_id:
        raise ValueError(
            "build_stage_complete_kwargs: state.task_id must be non-empty "
            "(ASSUM-018: target_identifier == task_id)"
        )
    return {
        "target_kind": "subagent",
        "target_identifier": state.task_id,
    }


# ---------------------------------------------------------------------------
# LifecycleEmitterAdapter — DDR-007 production wiring (TASK-FW10-010)
# ---------------------------------------------------------------------------


#: Mapping from DDR-006 lifecycle strings to the
#: :class:`PipelineLifecycleEmitter` async coroutine name that publishes the
#: matching ``pipeline.*`` envelope. ``None`` means "no publish for this
#: lifecycle" — the channel write still happens via ``_update_state``;
#: only the wire emit is suppressed (e.g. ``starting`` is observable via
#: ``async_tasks`` but produces no separate envelope).
#:
#: ``awaiting_approval`` routes to ``emit_paused`` (DDR-007 pause publish).
#: Resume-emit ownership does NOT live here: the daemon subscriber seam
#: (``forge.adapters.nats.approval_subscriber``, FW10-010) is the single
#: ``pipeline.build-resumed`` emit owner, firing on the real approve/override
#: decision. The former runner-side resume special-case (the C1
#: ``mark_resume_pending`` / ``_resume_pending`` mechanism) was removed by
#: TASK-GATE-D659 §D5: the ``LifecycleEmitterAdapter`` is never constructed in
#: production (the sidecar runs in a separate process with no forge.db / NATS),
#: so the mechanism was dead-and-broken (DDR-007:46 places the resume emit in
#: the subscriber path). The ``awaiting_approval → emit_paused`` row stays.
LIFECYCLE_TO_PIPELINE_EMIT: dict[str, str | None] = {
    "starting": None,
    "planning_waves": None,
    "running_wave": None,
    "awaiting_approval": "emit_paused",
    "pushing_pr": None,
    "completed": "emit_complete",
    "cancelled": "emit_cancelled",
    "failed": "emit_failed",
}


class LifecycleEmitterAdapter:
    """Bridge :class:`SubagentEmitter` (sync) → :class:`PipelineLifecycleEmitter` (async).

    DDR-007 §Decision (Option A) threads the in-process
    :class:`PipelineLifecycleEmitter` onto the subagent's context payload.
    The runner calls :meth:`SubagentEmitter.on_transition` synchronously
    from inside :func:`_update_state`; this adapter routes that single
    sync entry point to the async ``emit_*`` coroutines on the wrapped
    :class:`PipelineLifecycleEmitter`.

    Routing table (TASK-FW10-010; resume special-case removed by
    TASK-GATE-D659 §D5):

    * ``awaiting_approval`` → ``emit_paused`` (publishes
      ``pipeline.build-paused.<feature_id>``).
    * ``completed`` / ``cancelled`` / ``failed`` → terminal emits.
    * Other lifecycles are observable via the ``async_tasks`` channel
      only; the adapter is a no-op for them so this task stays scoped.

    Resume-emit ownership: ``pipeline.build-resumed`` is emitted **only** by
    the daemon subscriber seam (FW10-010), on the real approve/override
    decision. The former runner-side resume edge (``mark_resume_pending`` /
    ``_resume_pending``) was dead-and-broken in production (this adapter is
    never constructed sidecar-side) and has been removed.

    Failure-mode contract (DDR-007 §Failure-mode contract, ADR-ARCH-008):
    every scheduled coroutine is wrapped so any :class:`PublishFailure`
    or unexpected exception is logged at ``WARNING`` and swallowed —
    SQLite remains authoritative.

    Args:
        emitter: The in-process :class:`PipelineLifecycleEmitter` produced
            by ``forge.cli._serve_deps_lifecycle.build_publisher_and_emitter``.
        ctx: The originating :class:`BuildContext` (carries the
            correlation_id threaded onto every envelope per AC-002 of
            TASK-NFI-008).
        loop: Optional event loop. When omitted, the adapter resolves the
            running loop at call time (the autobuild_runner executes
            inside the daemon's running loop, so this is the typical
            production path).
    """

    def __init__(
        self,
        emitter: "PipelineLifecycleEmitter",
        ctx: "BuildContext",
        *,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._emitter = emitter
        self._ctx = ctx
        self._loop = loop

    # ------------------------------------------------------------------
    # SubagentEmitter Protocol
    # ------------------------------------------------------------------

    def on_transition(self, state: AutobuildState) -> None:
        """Route ``state.lifecycle`` to the matching async emit coroutine.

        Synchronous entry point that schedules the async ``emit_*`` call
        on the running event loop. The autobuild_runner subagent always
        executes inside an async runtime so a running loop is expected;
        if no loop is found the adapter falls back to running the
        coroutine to completion via :func:`asyncio.run` so unit tests
        that drive ``_update_state`` synchronously still observe the
        publish.
        """
        try:
            method_name = LIFECYCLE_TO_PIPELINE_EMIT.get(state.lifecycle)

            if method_name is None:
                # No publish for this lifecycle — async_tasks channel
                # write already happened in _update_state, which is the
                # authoritative observability surface.
                return

            coro = self._build_coroutine(method_name, state)
            if coro is None:
                return

            self._schedule(coro, method_name=method_name, lifecycle=state.lifecycle)
        except Exception as exc:  # noqa: BLE001 — DDR-007 swallow+log
            logger.warning(
                "LifecycleEmitterAdapter: routing failed lifecycle=%s err=%s "
                "— SQLite remains authoritative",
                state.lifecycle,
                exc,
            )

    # ------------------------------------------------------------------
    # Coroutine builders — one per emit method we route to
    # ------------------------------------------------------------------

    def _build_coroutine(self, method_name: str, state: AutobuildState) -> Any | None:
        """Construct the awaitable for the given emit method.

        Each emit method on :class:`PipelineLifecycleEmitter` requires a
        different keyword set; we synthesise minimal but contract-honest
        defaults from the :class:`AutobuildState` so the publish is a
        valid envelope. Operators wanting richer payloads (real coach
        score, real rationale, etc.) wire a richer emitter at the
        composition root — this adapter is the *floor*, not the ceiling.
        """
        emit = getattr(self._emitter, method_name, None)
        if emit is None:
            logger.warning(
                "LifecycleEmitterAdapter: emitter has no method %r — "
                "skipping emit for lifecycle=%s",
                method_name,
                state.lifecycle,
            )
            return None

        now_iso = datetime.now(timezone.utc).isoformat()

        if method_name == "emit_paused":
            return emit(
                self._ctx,
                stage_label=state.waiting_for or "awaiting_approval",
                gate_mode="MANDATORY_HUMAN_APPROVAL",
                coach_score=state.last_coach_score,
                rationale=state.waiting_for or "autobuild paused for approval",
                approval_subject=(f"agents.approval.forge.{state.build_id}"),
                paused_at=now_iso,
            )
        if method_name == "emit_complete":
            return emit(
                self._ctx,
                repo=None,
                branch=None,
                tasks_completed=state.tasks_completed,
                tasks_failed=state.tasks_failed,
                tasks_total=state.tasks_completed + state.tasks_failed,
                pr_url=None,
                duration_seconds=0,
                summary="autobuild completed",
            )
        if method_name == "emit_cancelled":
            return emit(
                self._ctx,
                reason="autobuild cancelled",
                cancelled_by="autobuild_runner",
                cancelled_at=now_iso,
            )
        if method_name == "emit_failed":
            return emit(
                self._ctx,
                failure_reason="autobuild failed",
                recoverable=False,
                failed_task_id=state.task_id,
            )
        # Unknown method — the routing table is the source of truth, so
        # an unrecognised entry is a programmer error.
        logger.warning(
            "LifecycleEmitterAdapter: no coroutine builder for method=%r "
            "lifecycle=%s — emit skipped",
            method_name,
            state.lifecycle,
        )
        return None

    # ------------------------------------------------------------------
    # Scheduling — schedules the async coroutine on the running loop
    # ------------------------------------------------------------------

    def _schedule(self, coro: Any, *, method_name: str, lifecycle: str) -> None:
        """Schedule ``coro`` on the running loop or run-to-completion.

        In production the autobuild_runner runs inside an async runtime,
        so :func:`asyncio.get_running_loop` resolves and we attach via
        :meth:`asyncio.AbstractEventLoop.create_task`. In synchronous
        unit tests, no loop is running; we fall back to
        :func:`asyncio.run` so the publish still happens (and tests
        can assert against the captured envelope).
        """
        loop = self._loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

        if loop is not None and loop.is_running():
            task = loop.create_task(self._safe_run(coro, method_name, lifecycle))
            # Detach by name only — caller does not own the task lifetime.
            task.set_name(f"lifecycle-emit-{method_name}-{lifecycle}")
            return

        # No running loop — run the coroutine to completion synchronously.
        try:
            asyncio.run(self._safe_run(coro, method_name, lifecycle))
        except Exception as exc:  # noqa: BLE001 — DDR-007 swallow+log
            logger.warning(
                "LifecycleEmitterAdapter: synchronous emit failed "
                "method=%s lifecycle=%s err=%s",
                method_name,
                lifecycle,
                exc,
            )

    async def _safe_run(self, coro: Any, method_name: str, lifecycle: str) -> None:
        """Await ``coro`` and swallow any non-cancellation error."""
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — DDR-007 swallow+log
            logger.warning(
                "LifecycleEmitterAdapter: emit coroutine raised "
                "method=%s lifecycle=%s err=%s",
                method_name,
                lifecycle,
                exc,
            )


# ---------------------------------------------------------------------------
# AutobuildRunnerState — graph state schema (TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-B-FIX)
# ---------------------------------------------------------------------------
#
# The autobuild_runner is launched by the supervisor's
# ``AsyncSubAgentMiddleware.start_async_task`` and runs in a separate
# LangGraph thread. The :class:`forge.lifecycle_bridge.LifecycleBridge`
# attaches an SSE consumer against that thread and reads the
# ``stream_mode="values"`` projection.
#
# Pre-fix history (FOLLOWUP-B spike): the runner graph was built via
# ``deepagents.create_deep_agent(...)``. That builder fixes the state
# schema at ``langchain.agents.middleware.types.AgentState`` plus the
# deepagents middleware extensions (``messages``/``todos``/``files``).
# The DDR-006 ``async_tasks`` channel is **not** in that schema, so a
# values projection of the runner's run never carried an ``async_tasks``
# key. The bridge's translator (``_extract_state`` in
# :mod:`forge.lifecycle_bridge.translation`) looks first for
# ``data["async_tasks"][feature_id]`` — found nothing on the wire,
# returned ``None`` for every part, and dropped 30/30 incoming
# ``event="values"`` parts on a real run. AC-3 envelopes never made it
# to the wire; ``ack_floor`` stuck at 11.
#
# This module now builds the runner graph via :class:`langgraph.graph.StateGraph`
# directly, with a state TypedDict that carries:
#
# 1. ``messages`` — preserves the
#    ``AsyncSubAgentMiddleware.start_async_task`` launch contract; the
#    middleware threads the launch description as the first user message.
#    Without this field the middleware's ``runs.create`` input shape is
#    rejected at thread-creation time.
# 2. ``async_tasks`` — the DDR-006 channel keyed by ``feature_id``. The
#    channel uses an additive merge reducer so successive transitions
#    update one entry per build without overwriting siblings (a forge
#    daemon may run multiple autobuild_runner threads in parallel).
#
# The lifecycle nodes drive a placeholder progression
# ``starting → planning_waves → running_wave → completed`` so the
# translator sees real transitions on the wire. Each node returns a
# state update with the new ``AutobuildState`` snapshot under
# ``async_tasks[feature_id]`` — a real LangGraph state mutation, surfaced
# in the values projection. The placeholder bodies are deliberate: this
# fix's scope is the state-shape contract between the runner and the
# bridge translator (TASK-FORGE-FRR-PEBR-WIREUP::AC-11). Wiring real
# autobuild work into these node bodies is a follow-up — the lifecycle
# state-machine apparatus (``_update_state``,
# :class:`LifecycleEmitterAdapter`, :class:`AutobuildState`) is fully
# defined above and can be invoked from richer node bodies without
# changing the graph shape exposed to ``langgraph.json``.


def _async_tasks_reducer(
    current: dict[str, dict[str, Any]] | None,
    update: dict[str, dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    """Merge ``async_tasks`` channel updates keyed by ``feature_id``.

    LangGraph state-channel reducer for the ``async_tasks`` field of
    :class:`AutobuildRunnerState`. The semantics are last-write-wins per
    ``feature_id``: a node returning
    ``{"async_tasks": {"FEAT-X": {...new_state}}}`` overwrites the
    ``"FEAT-X"`` entry while leaving any other in-flight ``feature_id``
    entries untouched. This is the same posture
    :class:`AsyncSubAgentMiddleware` uses on the supervisor's parent
    graph (its ``Command(update={"async_tasks": ...})`` shape merges the
    same way), so the runner's reducer matches operator expectations
    when reading either graph's values projection.

    A ``None`` ``update`` is a no-op — used by short-circuit paths that
    return state without an ``async_tasks`` change.

    Args:
        current: The current ``async_tasks`` value (``None`` on the
            first state mutation).
        update: The patch returned by the most recent node.

    Returns:
        The merged ``async_tasks`` mapping. The result is a fresh
        dict; callers do not need to copy.
    """
    merged: dict[str, dict[str, Any]] = dict(current or {})
    if update:
        for feature_id, snapshot in update.items():
            if isinstance(snapshot, Mapping):
                merged[feature_id] = dict(snapshot)
    return merged


class AutobuildRunnerState(TypedDict):
    """LangGraph state schema for the autobuild_runner subagent.

    Two channels:

    * ``messages`` — Required. Preserves the launch contract used by
      :class:`deepagents.middleware.async_subagents.AsyncSubAgentMiddleware`.
      The middleware's ``start_async_task`` tool passes the launch
      ``description`` as the first user message; the runner reads
      ``state["messages"][0].content`` to extract the dispatch payload
      (``build_id``, ``feature_id``, ``correlation_id``).
    * ``async_tasks`` — NotRequired. The DDR-006 lifecycle channel keyed
      by ``feature_id``. Lifecycle nodes write the
      :class:`AutobuildState` snapshot here; the
      :mod:`forge.lifecycle_bridge.translation` translator's
      ``_extract_state`` finds the snapshot via
      ``data["async_tasks"][feature_id]`` and emits the matching
      ``pipeline.*`` envelope.
    """

    messages: Required[Annotated[list[Any], add_messages]]
    async_tasks: NotRequired[Annotated[dict[str, dict[str, Any]], _async_tasks_reducer]]


# ---------------------------------------------------------------------------
# Launch-payload parsing
# ---------------------------------------------------------------------------


#: Regex extracting the JSON payload from the launch description. The
#: description shape is owned by
#: :func:`forge.cli._serve_async_task_starter._synthesise_description`
#: and looks like ``"RUN_AUTOBUILD subagent=<name> payload={...json...}"``.
#: A grouped ``payload=(...)`` capture is sufficient because the dispatch
#: payload always JSON-serialises (the ``lifecycle_emitter`` field is
#: stripped before serialisation, so no in-process Python objects leak).
_LAUNCH_PAYLOAD_PATTERN: re.Pattern[str] = re.compile(
    r"payload=(?P<payload>\{.*\})\s*$",
    flags=re.DOTALL,
)


def _extract_launch_payload(messages: list[Any]) -> dict[str, Any]:
    """Pull the dispatch payload out of the launch description.

    The :class:`AsyncSubAgentMiddleware.start_async_task` tool threads
    the launch description as ``state["messages"][0]``. The
    :class:`forge.cli._serve_async_task_starter._StructuredToolAsyncTaskStarter`
    formats that description as
    ``"RUN_AUTOBUILD subagent=<name> payload=<json>"``. This helper
    extracts the JSON payload and parses it.

    Returns the empty dict on any parse failure — the lifecycle nodes
    fall back to defaults so a malformed launch does not crash the
    runner thread (the daemon would lose the AutobuildState transitions
    but keep the LangGraph thread healthy).

    Args:
        messages: The launched thread's ``messages`` channel.

    Returns:
        The parsed payload dict, or ``{}`` if the launch description
        cannot be parsed.
    """
    if not messages:
        return {}
    first = messages[0]
    content = getattr(first, "content", None)
    if not isinstance(content, str):
        # Some langgraph versions deliver the first message as a dict.
        if isinstance(first, Mapping):
            content = first.get("content")
    if not isinstance(content, str):
        return {}
    match = _LAUNCH_PAYLOAD_PATTERN.search(content)
    if match is None:
        return {}
    try:
        payload = json.loads(match.group("payload"))
    except json.JSONDecodeError as exc:
        logger.warning(
            "autobuild_runner: launch payload JSON decode failed (%s) — "
            "falling back to empty payload; lifecycle transitions will "
            "use placeholder identifiers",
            exc,
        )
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _build_snapshot(
    payload: Mapping[str, Any],
    *,
    lifecycle: str,
    wave_index: int = 0,
    task_index: int = 0,
    tasks_completed: int = 0,
    tasks_failed: int = 0,
) -> dict[str, Any]:
    """Construct an :class:`AutobuildState` snapshot dict for the channel.

    The snapshot mirrors the DDR-006 schema and matches the shape the
    bridge translator's ``_extract_state`` expects. We construct a
    Pydantic :class:`AutobuildState` first (so the ``Literal`` lifecycle
    validation runs and any future schema drift surfaces as a
    :class:`pydantic.ValidationError`), then ``model_dump`` to a plain
    dict for the LangGraph state channel — channels are JSON-shaped, not
    Pydantic-shaped.

    Args:
        payload: Parsed launch payload (``build_id``, ``feature_id``,
            ``correlation_id`` keys consulted; missing keys fall back
            to placeholder strings so the runner never crashes on a
            malformed launch).
        lifecycle: Target lifecycle for this snapshot. Must be a member
            of :data:`LIFECYCLE_VALUES`.
        wave_index: 0-indexed wave the runner is currently in.
        task_index: 0-indexed task within the current wave.
        tasks_completed: Cumulative completed task count.
        tasks_failed: Cumulative failed task count.

    Returns:
        A JSON-serialisable dict mirroring
        :class:`AutobuildState.model_dump(mode="json")` — safe to write
        into the ``async_tasks`` LangGraph channel.
    """
    feature_id = str(payload.get("feature_id") or "FEAT-UNKNOWN")
    build_id = str(payload.get("build_id") or f"build-{feature_id}-pending")
    correlation_id = payload.get("correlation_id")
    wave_total_raw = payload.get("wave_total")
    wave_total = int(wave_total_raw) if wave_total_raw is not None else 1
    task_total_raw = payload.get("task_total")
    task_total = int(task_total_raw) if task_total_raw is not None else 1
    state = AutobuildState(
        task_id=str(payload.get("task_id") or build_id),
        build_id=build_id,
        feature_id=feature_id,
        lifecycle=lifecycle,  # type: ignore[arg-type] - validated by AutobuildState's Literal
        wave_index=wave_index,
        wave_total=wave_total,
        task_index=task_index,
        task_total=task_total,
        tasks_completed=tasks_completed,
        tasks_failed=tasks_failed,
        correlation_id=str(correlation_id) if correlation_id else None,
    )
    return state.model_dump(mode="json")


def _snapshot_update(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Wrap a snapshot into the ``async_tasks`` reducer-shaped update."""
    return {"async_tasks": {snapshot["feature_id"]: snapshot}}


# ---------------------------------------------------------------------------
# Lifecycle nodes — placeholder bodies (TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-B-FIX)
# ---------------------------------------------------------------------------
#
# Each node returns a state update writing the next AutobuildState to
# ``async_tasks[feature_id]``. The bodies are deliberately empty of real
# autobuild work: the contract this fix closes is the state-shape one,
# not the autobuild-orchestration one. Real wave/task execution is
# wired into these nodes in a follow-up; the LangGraph topology and
# state schema established here remain stable across that follow-up.


def _node_starting(state: AutobuildRunnerState) -> dict[str, Any]:
    """Emit the ``starting`` snapshot — entry point of the runner."""
    payload = _extract_launch_payload(list(state.get("messages", [])))
    snapshot = _build_snapshot(payload, lifecycle="starting")
    return _snapshot_update(snapshot)


def _node_planning_waves(state: AutobuildRunnerState) -> dict[str, Any]:
    """Transition to ``planning_waves``, reading the feature task graph.

    Resolves the target repo checkout via ``_resolve_repo_path``, reads
    ``.guardkit/features/<feature_id>.yaml``, and populates
    ``wave_total`` and ``task_total`` from the parsed task graph.
    On any failure (missing repo, missing/malformed yaml, absent feature
    id in file), falls back to the placeholder snapshot and emits a
    WARNING naming the resolved path.
    """
    payload = _extract_launch_payload(list(state.get("messages", [])))
    feature_id = str(payload.get("feature_id") or "FEAT-UNKNOWN")

    # Resolve the repo path using the same helper as _node_running_wave.
    repo_path = _resolve_repo_path(payload)

    wave_total = 0
    task_total = 0

    if repo_path is not None:
        feature_yaml_path = (
            repo_path / ".guardkit" / "features" / f"{feature_id}.yaml"
        )
        try:
            if feature_yaml_path.exists():
                with open(feature_yaml_path, "r") as f:
                    feature_data = yaml.safe_load(f)
                if isinstance(feature_data, dict):
                    tasks = feature_data.get("tasks")
                    if isinstance(tasks, list):
                        task_total = len(tasks)
                    parallel_groups = (
                        feature_data.get("orchestration", {})
                        .get("parallel_groups")
                    )
                    if isinstance(parallel_groups, list):
                        wave_total = len(parallel_groups)
            else:
                logger.warning(
                    "autobuild_runner: feature yaml not found at %s "
                    "(feature_id=%r) — emitting placeholder planning_waves "
                    "snapshot; run will proceed",
                    feature_yaml_path,
                    feature_id,
                )
        except yaml.YAMLError as exc:
            logger.warning(
                "autobuild_runner: failed to parse feature yaml at %s "
                "(feature_id=%r, error=%s) — emitting placeholder "
                "planning_waves snapshot; run will proceed",
                feature_yaml_path,
                feature_id,
                exc,
            )
        except OSError as exc:
            logger.warning(
                "autobuild_runner: failed to read feature yaml at %s "
                "(feature_id=%r, error=%s) — emitting placeholder "
                "planning_waves snapshot; run will proceed",
                feature_yaml_path,
                feature_id,
                exc,
            )

    enriched_payload = dict(payload)
    enriched_payload["wave_total"] = wave_total
    enriched_payload["task_total"] = task_total
    snapshot = _build_snapshot(enriched_payload, lifecycle="planning_waves")
    return _snapshot_update(snapshot)


# ---------------------------------------------------------------------------
# guardkit subprocess wiring (TASK-ABW-001)
# ---------------------------------------------------------------------------
#
# The runner shells out to ``guardkit autobuild feature <feature_id> --fresh
# --verbose`` against a resolved local checkout of the target repo. The
# helpers below resolve the repo and guardkit binary paths from the launch
# payload + environment, and the rewritten ``_node_running_wave`` body
# orchestrates the subprocess with timeout + exit-code mapping.
#
# ADR-ARCH-033: this deliberately bypasses ``adapters/guardkit/run.py`` (the
# one-shot "single boundary") because autobuild is a long-running streaming
# build. COACH-SCORE GAP CLOSED (TASK-UBS1C-001): the drain loop now parses
# ``Completed turn N: success|feedback - ...`` decision lines and populates
# ``last_coach_score`` (1.0 for success, 0.0 for feedback) and
# ``aggregate_coach_score`` (success ratio over decision-bearing turns).
# See docs/research/evidence/autobuild-transcripts-2026-07-26/README.md for
# the evidence backing these decision-derived semantics.


#: Environment override for the base directory containing local repo
#: checkouts. The resolver expects ``<FORGE_REPO_BASE>/<basename>`` to be a
#: cloned checkout of ``payload["repo"]``. Defaults to
#: ``~/Projects/appmilla_github`` per the source plan's single-host layout.
FORGE_REPO_BASE_ENV: str = "FORGE_REPO_BASE"

#: Environment override for the absolute path to the ``guardkit`` binary.
#: When unset, :func:`_resolve_guardkit_path` falls back to
#: :func:`shutil.which("guardkit")`.
FORGE_GUARDKIT_PATH_ENV: str = "FORGE_GUARDKIT_PATH"

#: Environment override for the autobuild subprocess timeout, in seconds.
#: Defaults to ``3600`` (60 minutes) per TASK-ABW-001 §Scope item 5.
FORGE_AUTOBUILD_TIMEOUT_ENV: str = "FORGE_AUTOBUILD_TIMEOUT_SECONDS"

#: Default subprocess timeout (seconds). Operators may override via
#: :data:`FORGE_AUTOBUILD_TIMEOUT_ENV`.
DEFAULT_AUTOBUILD_TIMEOUT_SECONDS: int = 3600

#: Default base directory for repo checkouts when
#: :data:`FORGE_REPO_BASE_ENV` is unset. Resolved at call time via
#: :meth:`Path.expanduser` so a different ``$HOME`` in the sidecar still
#: works.
DEFAULT_FORGE_REPO_BASE: str = "~/Projects/appmilla_github"

#: Environment override for the base directory that holds per-build ISOLATED
#: git worktrees (DEFECT #19, B4 round-17). Each branch-aware autobuild
#: materialises ``<base>/<build_id>`` as a worktree of ``payload["branch"]``
#: so the SHARED checkout is never mutated by a build.
FORGE_AUTOBUILD_WORKTREE_BASE_ENV: str = "FORGE_AUTOBUILD_WORKTREE_BASE"

#: Default per-build worktree base when
#: :data:`FORGE_AUTOBUILD_WORKTREE_BASE_ENV` is unset.
DEFAULT_AUTOBUILD_WORKTREE_BASE: str = "/tmp/forge-autobuild-worktrees"

#: Regex matching one ``[guardkit-checkpoint] Turn N complete (tests: ...)``
#: line in guardkit's verbose stdout. The runner counts these to drive the
#: stage_complete fallback (TASK-ABW-001 §Scope item 3).
_GUARDKIT_CHECKPOINT_PATTERN: re.Pattern[str] = re.compile(
    r"\[guardkit-checkpoint\]\s+Turn\s+\d+\s+complete\s+\(tests:\s+(pass|fail)",
    flags=re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Coach-score grammar (TASK-UBS1C-001 — decision-derived semantics)
# ---------------------------------------------------------------------------
#
# ADR-ARCH-033 documented the coach-score gap: ``guardkit autobuild`` emits
# NO numeric score on stdout. The evidence transcripts (docs/research/evidence/
# autobuild-transcripts-2026-07-26/) prove the only coach-derived signal is
# the per-turn decision line:
#
#   INFO:guardkit.orchestrator.progress:[<ISO8601>] Completed turn <N>: success - ...
#   INFO:guardkit.orchestrator.progress:[<ISO8601>] Completed turn <N>: feedback - ...
#
# The verdict-emission-failure edge (CV4M archive) emits a WARNING followed by
# a normal ``Completed turn N: feedback - ...`` line — parsers must treat it
# as a feedback turn, not a crash.
#
# Semantics (evidence-backed, TASK-UBS1C-001):
#   * ``last_coach_score`` = 1.0 for ``success``, 0.0 for ``feedback``.
#   * ``aggregate_coach_score`` = success-turn ratio over decision-bearing turns.
#   * Timeout (no decision lines) leaves scores at their last value / None.

#: Regex matching one decision-bearing progress line.
#: Captures ``turn_number`` (int) and ``decision`` (``"success"`` | ``"feedback"``).
#: The verdict-emission-failure WARNING line does NOT match this pattern; only
#: the subsequent ``Completed turn N: ...`` line does.
_DECISION_LINE_PATTERN: re.Pattern[str] = re.compile(
    r"Completed\s+turn\s+(\d+)\s*:\s*(success|feedback)\s+-",
    flags=re.IGNORECASE,
)


def _parse_decision_line(line: str) -> tuple[int, str] | None:
    """Extract (turn_number, decision) from a decision-bearing progress line.

    Returns ``(turn_number, decision)`` where ``decision`` is ``"success"`` or
    ``"feedback"``, or ``None`` when the line does not match the decision
    grammar.

    Args:
        line: A decoded stdout line from the guardkit subprocess.

    Returns:
        ``(turn_number, decision)`` or ``None``.
    """
    match = _DECISION_LINE_PATTERN.search(line)
    if match is None:
        return None
    return int(match.group(1)), match.group(2).lower()


def _resolve_guardkit_path() -> Path | None:
    """Resolve the absolute path of the ``guardkit`` executable.

    Resolution order (TASK-ABW-001 §Scope item 2):

    1. :data:`FORGE_GUARDKIT_PATH_ENV` env var, if it points to an existing
       executable file.
    2. :func:`shutil.which("guardkit")`.

    Returns ``None`` and logs a WARNING when no executable resolves; the
    caller (``_node_running_wave``) treats this as a ``failed`` transition.
    The return type is :class:`Path` so the subprocess wiring can pass
    ``str(path)`` to :func:`asyncio.create_subprocess_exec` without any
    further coercion.
    """
    override = os.environ.get(FORGE_GUARDKIT_PATH_ENV, "").strip()
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
        logger.warning(
            "autobuild_runner: %s=%r does not resolve to an executable file "
            "— falling back to shutil.which('guardkit')",
            FORGE_GUARDKIT_PATH_ENV,
            override,
        )

    which_result = shutil.which("guardkit")
    if which_result:
        return Path(which_result).resolve()

    logger.warning(
        "autobuild_runner: guardkit binary not found on PATH and "
        "%s is unset — _node_running_wave will transition to 'failed'",
        FORGE_GUARDKIT_PATH_ENV,
    )
    return None


def _load_filesystem_allowlist() -> list[Path] | None:
    """Best-effort loader for ``forge_config.permissions.filesystem.allowlist``.

    The runner subagent does not receive ``ForgeConfig`` directly (it is a
    LangGraph thread launched by the supervisor). For the allowlist gate,
    we attempt to load ``./forge.yaml`` (or ``$FORGE_CONFIG_PATH``) lazily;
    on any failure we return ``None`` and the resolver falls back to a
    permissive base-dir-only check. The integration tests bypass this
    entirely by monkey-patching :func:`_resolve_repo_path` at the module
    surface.

    Returns:
        A list of allowlisted :class:`Path` roots, or ``None`` when no
        config could be loaded.
    """
    config_path_env = os.environ.get("FORGE_CONFIG_PATH", "").strip()
    candidate_paths: list[Path] = []
    if config_path_env:
        candidate_paths.append(Path(config_path_env).expanduser())
    candidate_paths.append(Path("forge.yaml"))

    for cfg_path in candidate_paths:
        if not cfg_path.is_file():
            continue
        try:
            # Local import keeps the module import-light when no config exists.
            from forge.config.loader import load_config  # type: ignore[import-not-found]

            cfg = load_config(cfg_path)
        except Exception as exc:  # noqa: BLE001 — best-effort loader
            logger.warning(
                "autobuild_runner: failed to load forge config from %s: %s",
                cfg_path,
                exc,
            )
            return None
        try:
            return list(cfg.permissions.filesystem.allowlist)
        except AttributeError:
            return None
    return None


def _resolve_repo_path(payload: Mapping[str, Any]) -> Path | None:
    """Resolve the absolute local checkout for ``payload['repo']``.

    Maps ``payload["repo"]`` (e.g. ``"appmilla/api_test"``) to
    ``<FORGE_REPO_BASE>/<basename>`` and validates that the resolved path:

    1. Exists on disk.
    2. Is a git repo (``.git/`` present as a directory or file — git
       worktrees use a ``.git`` file).
    3. Is inside the configured filesystem allowlist (when discoverable
       via :func:`_load_filesystem_allowlist`).

    On any failure, returns ``None`` and logs a WARNING with the structured
    reason. The caller transitions to ``failed`` with that reason on the
    snapshot.

    Args:
        payload: Parsed launch payload — must carry the ``repo`` key
            shaped as ``"<org>/<repo>"``.

    Returns:
        Resolved absolute :class:`Path` on success, or ``None`` on any
        validation failure (missing key, non-existent path, not a git
        repo, outside allowlist).
    """
    repo_raw = payload.get("repo")
    if not isinstance(repo_raw, str) or not repo_raw.strip():
        # TEMP HOTFIX (TASK-ABW-002 tracked): the upstream dispatcher closure
        # (forge.cli.serve.dispatcher) only forwards build_id/feature_id/
        # rationale to dispatch_autobuild_async, so payload.repo is absent
        # in production launches. Fall back to FORGE_DEFAULT_REPO until the
        # upstream contract is widened to plumb the BuildQueuedPayload
        # repo/branch/feature_yaml_path through to launch_payload.
        env_repo = os.environ.get("FORGE_DEFAULT_REPO", "").strip()
        if env_repo:
            logger.info(
                "autobuild_runner: payload.repo missing; using "
                "FORGE_DEFAULT_REPO=%s",
                env_repo,
            )
            repo_raw = env_repo
        else:
            logger.warning(
                "autobuild_runner: missing or empty 'repo' in launch payload "
                "and FORGE_DEFAULT_REPO unset — cannot resolve checkout path"
            )
            return None

    # Accept ``org/repo`` and bare ``repo`` (defensive — the BuildQueuedPayload
    # field is loosely shaped; only the basename matters for the local layout).
    basename = repo_raw.strip().split("/")[-1]
    if not basename:
        logger.warning(
            "autobuild_runner: repo=%r has empty basename after split — "
            "cannot resolve checkout path",
            repo_raw,
        )
        return None

    base_dir_raw = (
        os.environ.get(FORGE_REPO_BASE_ENV, "").strip() or DEFAULT_FORGE_REPO_BASE
    )
    base_dir = Path(base_dir_raw).expanduser().resolve()
    candidate = (base_dir / basename).resolve()

    if not candidate.exists():
        logger.warning(
            "autobuild_runner: resolved repo path %s does not exist on disk "
            "(repo=%r, base=%s)",
            candidate,
            repo_raw,
            base_dir,
        )
        return None

    if not candidate.is_dir():
        logger.warning(
            "autobuild_runner: resolved repo path %s is not a directory " "(repo=%r)",
            candidate,
            repo_raw,
        )
        return None

    git_marker = candidate / ".git"
    if not git_marker.exists():
        logger.warning(
            "autobuild_runner: resolved repo path %s is not a git repo "
            "(missing .git marker, repo=%r)",
            candidate,
            repo_raw,
        )
        return None

    # Allowlist gate. When no config is discoverable we fall back to
    # FORGE_REPO_BASE itself — the resolver convention already constrains
    # paths to that root, so a bare base-dir check is equivalent to the
    # default permissions and avoids hard-failing test environments that
    # ship without a forge.yaml.
    allowlist = _load_filesystem_allowlist()
    if allowlist is None:
        allowlist = [base_dir]

    # Local import to avoid a hard adapter→subagent dep at module load.
    from forge.adapters.nats.pipeline_consumer import _path_inside_allowlist

    if not _path_inside_allowlist(str(candidate), allowlist):
        logger.warning(
            "autobuild_runner: resolved repo path %s is outside the "
            "configured filesystem allowlist (repo=%r)",
            candidate,
            repo_raw,
        )
        return None

    return candidate


def _resolve_autobuild_timeout_seconds() -> float:
    """Read :data:`FORGE_AUTOBUILD_TIMEOUT_ENV` with a safe default fallback.

    Malformed values fall back to :data:`DEFAULT_AUTOBUILD_TIMEOUT_SECONDS`
    rather than raising — the subagent must not crash on a stray env-var
    typo. Non-positive values are also coerced to the default because a
    zero/negative timeout would short-circuit every autobuild before the
    subprocess could even start.
    """
    raw = os.environ.get(FORGE_AUTOBUILD_TIMEOUT_ENV, "").strip()
    if not raw:
        return float(DEFAULT_AUTOBUILD_TIMEOUT_SECONDS)
    try:
        parsed = float(raw)
    except ValueError:
        logger.warning(
            "autobuild_runner: %s=%r is not a number — using default %s",
            FORGE_AUTOBUILD_TIMEOUT_ENV,
            raw,
            DEFAULT_AUTOBUILD_TIMEOUT_SECONDS,
        )
        return float(DEFAULT_AUTOBUILD_TIMEOUT_SECONDS)
    if parsed <= 0:
        return float(DEFAULT_AUTOBUILD_TIMEOUT_SECONDS)
    return parsed


def _resolve_budget_wallclock_seconds(payload: Mapping[str, Any]) -> float | None:
    """Extract the per-build wall-clock budget cap from the launch payload.

    FEAT-UBS-002 (Option-B, stage 1 — the run bounds ITSELF). The serve-side
    dispatch attaches ``payload["budget"] = {"max_wallclock_seconds", ...}``
    only when the resolved profile carries a wall-clock cap (an unattended
    profile). The caller MIN()s this against the env/default subprocess timeout
    so a profile can only TIGHTEN the bound; on expiry the existing
    ``proc.kill`` + failed-terminal path is the honest hard stop.

    Returns the positive cap in seconds, or ``None`` when the ``budget`` entry
    is absent / malformed / non-positive — in which case the caller keeps the
    env/default timeout unchanged, so an attended / NULL-profile build behaves
    byte-equivalently to the pre-budget path.
    """
    budget = payload.get("budget")
    if not isinstance(budget, Mapping):
        return None
    raw = budget.get("max_wallclock_seconds")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return seconds


# ---------------------------------------------------------------------------
# Branch-aware isolated worktrees (DEFECT #19, B4 round-17)
# ---------------------------------------------------------------------------
#
# Before this fix the runner ran ``guardkit autobuild`` with cwd = the SHARED
# repo checkout AS-IS, ignoring the ``branch`` the dispatch was scoped to. The
# machine-made feature artifacts live on the planning branch; the shared
# checkout may be on a different lane's branch entirely — so a build targeted
# the WRONG TREE (or refused on a missing feature YAML). The runner now
# materialises an isolated git worktree of ``payload["branch"]`` and runs the
# subprocess there, never touching the shared checkout. Worktree creation reads
# the LOCAL ref only: no fetch / pull / checkout is ever issued against the
# shared tree (a missing branch is a loud failure, not a fetch trigger).


class WorktreeMaterialisationError(RuntimeError):
    """Raised when a branch-isolated worktree cannot be created (DEFECT #19)."""


async def _run_git(args: list[str], *, cwd: Path) -> tuple[int, str]:
    """Run ``git <args>`` in ``cwd``; return ``(returncode, combined output)``.

    Uses :func:`asyncio.create_subprocess_exec` — the same subprocess seam the
    guardkit invocation uses — so tests that stub the guardkit call can
    dispatch on ``argv[0]`` and let real git run against a throwaway repo. Only
    LOCAL git verbs are ever passed here (``rev-parse``, ``worktree``); no
    network verb (``fetch``/``pull``) is issued, honouring the DEFECT #19
    "read the local ref only" rule.
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out_bytes, _ = await proc.communicate()
    code = proc.returncode if proc.returncode is not None else -1
    return code, out_bytes.decode("utf-8", errors="replace").strip()


async def _local_branch_exists(repo_path: Path, branch: str) -> bool:
    """Return ``True`` iff ``branch`` resolves as a LOCAL ref in ``repo_path``.

    Uses ``git rev-parse --verify --quiet refs/heads/<branch>`` so only a
    local branch ref counts — a remote-tracking ref alone is NOT enough. No
    fetch is performed; a branch that exists only on the remote reads as
    missing here, which the caller turns into a loud failure (DEFECT #19).
    """
    code, _ = await _run_git(
        ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repo_path,
    )
    return code == 0


def _feature_task_ids(repo_path: Path, feature_id: str) -> list[str] | None:
    """Return the task ids declared in ``.guardkit/features/<feature_id>.yaml``.

    guardkit turns each task id into an ``autobuild/<task_id>`` branch + a
    ``.guardkit/worktrees/<task_id>`` inner worktree
    (``WorktreeManager._build_branch_name``), so these are the ONLY refs this
    build's F3 preflight sweep is allowed to touch — a concurrent build of
    ANOTHER feature owns its own task ids and must be left alone.

    Reads the SAME file :func:`_node_planning_waves` consults, NOT the payload's
    ``feature_yaml_path`` — that field is the plan-tree yaml the allowlist gate
    validates; dispatch never uses it to locate the spec (see
    ``forge.cli._serve_planning._resolve_feature_yaml_path``), and it may point
    at a plan branch not checked out here. The ``.guardkit/features`` yaml is
    the authoritative task graph guardkit itself consumes.

    Returns ``None`` (never raises) when the file is missing or unparseable so
    the caller can fall back to prune-only per the loud-warn-never-crash
    convention (FEAT-UBS1C).
    """
    feature_yaml_path = (
        repo_path / ".guardkit" / "features" / f"{feature_id}.yaml"
    )
    try:
        if not feature_yaml_path.exists():
            return None
        with open(feature_yaml_path, "r") as f:
            feature_data = yaml.safe_load(f)
    except (yaml.YAMLError, OSError):
        return None
    if not isinstance(feature_data, dict):
        return None
    tasks = feature_data.get("tasks")
    if not isinstance(tasks, list):
        return None
    task_ids: list[str] = []
    for task in tasks:
        if isinstance(task, dict):
            tid = task.get("id")
            if isinstance(tid, str) and tid.strip():
                task_ids.append(tid.strip())
    return task_ids


async def _list_registered_worktrees(
    repo_path: Path,
) -> list[tuple[Path, str | None]]:
    """Parse ``git worktree list --porcelain`` → ``[(path, branch_or_None)]``.

    ``branch`` is the short branch a worktree has checked out
    (``refs/heads/<name>`` → ``<name>``), or ``None`` for a detached HEAD.
    Returns ``[]`` on any git failure so the sweep degrades gracefully rather
    than crashing.
    """
    code, output = await _run_git(
        ["worktree", "list", "--porcelain"], cwd=repo_path
    )
    if code != 0:
        return []
    entries: list[tuple[Path, str | None]] = []
    current_path: Path | None = None
    current_branch: str | None = None
    for line in output.splitlines():
        if line.startswith("worktree "):
            if current_path is not None:
                entries.append((current_path, current_branch))
            current_path = Path(line[len("worktree ") :])
            current_branch = None
        elif line.startswith("branch "):
            ref = line[len("branch ") :].strip()
            if ref.startswith("refs/heads/"):
                current_branch = ref[len("refs/heads/") :]
            else:
                current_branch = ref
    if current_path is not None:
        entries.append((current_path, current_branch))
    return entries


async def _sweep_build_refs(repo_path: Path, feature_id: str) -> None:
    """Preflight sweep of THIS build's residue before worktree add (F3).

    Failed/prior autobuild attempts leave guardkit's inner
    ``.guardkit/worktrees/<task_id>`` registrations + ``autobuild/<task_id>``
    branches in the shared repo; they outlive the outer worktree's removal and
    poison the next run. This clears ONLY this feature's residue, loudly and
    itemised:

    1. ``git worktree prune`` — drops registrations whose dirs are gone.
    2. For each of this feature's task ids ``T`` with a live ``autobuild/<T>``
       branch: if that branch is still checked out in an on-disk (stale, prior
       build) worktree, ``git -C <that-worktree> checkout --detach`` FIRST — the
       F11 read-only-forensics law: preserve the kept files at their commit,
       never destroy them — then ``git branch -D autobuild/<T>``.

    NEVER touches branches/worktrees of OTHER features (concurrent builds are
    legal). When the feature yaml cannot be read for task ids, logs a loud
    warning and falls back to prune-only. Never raises: a sweep failure must
    not crash the build (the FEAT-UBS1C loud-warn-never-crash convention).
    """
    try:
        code, output = await _run_git(["worktree", "prune"], cwd=repo_path)
        if code == 0:
            logger.info(
                "autobuild_runner: F3 preflight sweep — `git worktree prune` "
                "done in %s%s",
                repo_path,
                f" ({output})" if output else "",
            )
        else:
            logger.warning(
                "autobuild_runner: F3 preflight sweep — `git worktree prune` "
                "exit=%s in %s: %s (continuing)",
                code,
                repo_path,
                output,
            )

        task_ids = _feature_task_ids(repo_path, feature_id)
        if task_ids is None:
            logger.warning(
                "autobuild_runner: F3 preflight sweep — could not read task ids "
                "from .guardkit/features/%s.yaml in %s; falling back to "
                "prune-only (no branch sweep). OTHER-feature refs are untouched "
                "either way.",
                feature_id,
                repo_path,
            )
            return

        registered = await _list_registered_worktrees(repo_path)
        branch_to_worktree = {
            branch: path
            for path, branch in registered
            if branch is not None
        }
        for task_id in task_ids:
            branch = f"autobuild/{task_id}"
            if not await _local_branch_exists(repo_path, branch):
                continue
            holder = branch_to_worktree.get(branch)
            if holder is not None and holder.exists():
                code, output = await _run_git(
                    ["-C", str(holder), "checkout", "--detach"],
                    cwd=repo_path,
                )
                if code == 0:
                    logger.info(
                        "autobuild_runner: F3 sweep — detached stale inner "
                        "worktree %s off %s (forensic files preserved at "
                        "commit)",
                        holder,
                        branch,
                    )
                else:
                    logger.warning(
                        "autobuild_runner: F3 sweep — could not detach %s off "
                        "%s (exit=%s): %s; skipping branch delete to avoid data "
                        "loss",
                        holder,
                        branch,
                        code,
                        output,
                    )
                    continue
            code, output = await _run_git(["branch", "-D", branch], cwd=repo_path)
            if code == 0:
                logger.info(
                    "autobuild_runner: F3 sweep — deleted stale branch %s",
                    branch,
                )
            else:
                logger.warning(
                    "autobuild_runner: F3 sweep — `git branch -D %s` exit=%s: "
                    "%s (continuing)",
                    branch,
                    code,
                    output,
                )
    except Exception as exc:  # never crash the build on sweep failure
        logger.warning(
            "autobuild_runner: F3 preflight sweep failed unexpectedly (%s) — "
            "continuing to worktree materialisation; a poisoned prior ref may "
            "surface as a loud worktree-add failure, which is the safe outcome",
            exc,
        )


def _worktree_base_dir() -> Path:
    """Resolve the per-build worktree base dir (env-overridable)."""
    raw = (
        os.environ.get(FORGE_AUTOBUILD_WORKTREE_BASE_ENV, "").strip()
        or DEFAULT_AUTOBUILD_WORKTREE_BASE
    )
    return Path(raw).expanduser()


async def _materialise_worktree(
    repo_path: Path, branch: str, build_id: str
) -> Path:
    """``git worktree add --detach <base>/<build_id> <branch>`` (LOCAL ref).

    Uses ``--detach`` (F2, receipted live 2026-07-26 against ddd-demo): the
    worktree checks out the branch's COMMIT without claiming the branch REF, so
    it never collides with a checkout (or another worktree) already holding
    that branch. guardkit's inner flow does not need the outer HEAD to be a
    named branch — its ``WorktreeManager.create`` bases inner worktrees on an
    explicit ``base_branch`` (default ``main``) and never queries the outer
    current branch — so detaching is safe.

    Returns the resolved worktree path on success. Raises
    :class:`WorktreeMaterialisationError` (carrying git's output) on failure —
    on that failure git has created nothing, so there is no worktree litter to
    clean up. Never fetches; the branch is assumed already verified present via
    :func:`_local_branch_exists`.
    """
    base = _worktree_base_dir()
    base.mkdir(parents=True, exist_ok=True)
    worktree_path = (base / build_id).resolve()
    code, output = await _run_git(
        ["worktree", "add", "--detach", str(worktree_path), branch],
        cwd=repo_path,
    )
    if code != 0:
        raise WorktreeMaterialisationError(
            f"git worktree add {worktree_path} for branch {branch!r} failed "
            f"(exit={code}): {output}"
        )
    return worktree_path


#: Env var naming the durable receipts root (FEAT-DRC). Default rides
#: ``~/forge-state`` (bind-mounted at ``/var/forge`` in forge-prod, so the
#: daemon and accrual counters can read ``/var/forge/receipts/<build_id>/``).
RECEIPTS_DIR_ENV: str = "FORGE_RECEIPTS_DIR"
DEFAULT_RECEIPTS_DIR: str = "~/forge-state/receipts"

#: The receipt families exported before the success-path worktree removal
#: (FEAT-DRC / register 2a4): coach verdicts + evidence dossiers + the
#: FEAT-SCG conformance snapshot live under ``autobuild-private``; the shadow
#: judge's queue under ``qav-shadow``; review summaries under ``autobuild``.
_RECEIPT_FAMILIES: tuple[str, ...] = (
    ".guardkit/autobuild-private",
    ".guardkit/qav-shadow",
    ".guardkit/autobuild",
)

#: FEAT-DRF — the per-build failure pack, written beside the exported receipt
#: families in ``$FORGE_RECEIPTS_DIR/<build_id>/``.
STDOUT_LOG_NAME: str = "autobuild-stdout.log"
FAILURE_MANIFEST_NAME: str = "failure-manifest.json"

#: Stable prefix of the per-run separator line the stdout tee writes at the
#: top of each run's segment (07-30 coach finding 2 — build_id reuse appends
#: a second run's narrative to the same log; the separator makes the segments
#: explicit). Kept as a module constant so diagnosers and tests share one
#: grammar.
STDOUT_RUN_HEADER_PREFIX: str = "===== autobuild run"


def _stdout_run_header(payload: Mapping[str, Any], feature_id: str) -> str:
    """One-line run separator for the (append-mode) per-build stdout log."""
    build_id = payload.get("build_id")
    correlation_id = payload.get("correlation_id")
    return (
        f"{STDOUT_RUN_HEADER_PREFIX} "
        f"started={datetime.now(timezone.utc).isoformat()} "
        f"feature_id={feature_id} "
        f"build_id={build_id if build_id else '<none>'} "
        f"correlation_id={correlation_id if correlation_id else '<none>'} ====="
    )


def _receipts_root() -> Path:
    """Resolve the durable receipts root (FEAT-DRC / FEAT-DRF).

    ``$FORGE_RECEIPTS_DIR`` wins; the default rides
    ``~/forge-state/receipts``. Path arithmetic only — never touches disk and
    never raises (a home-less environment falls back to the literal default).
    """
    raw = os.environ.get(RECEIPTS_DIR_ENV, DEFAULT_RECEIPTS_DIR)
    try:
        return Path(raw).expanduser()
    except (RuntimeError, OSError):  # pragma: no cover — no resolvable HOME
        return Path(raw)


def _harden_pack_permissions(root: Path) -> None:
    """Restrict a receipts pack to the owner (0700 dirs / 0600 files).

    FEAT-DRF coach finding: the pack persists the FULL guardkit subprocess
    narrative durably (and ~/forge-state is bind-mounted into forge-prod);
    under the operator umask the defaults were group/world-readable — the
    wrong posture for an estate with a live credential-exposure history.
    Best-effort: permission errors are swallowed (the pack's existence beats
    its mode; never fail an export over chmod).
    """
    try:
        if not root.exists():
            return
        for path in [root, *root.rglob("*")]:
            try:
                path.chmod(0o700 if path.is_dir() else 0o600)
            except OSError:
                continue
    except OSError:
        pass


def _resolve_receipt_build_id(
    payload: Mapping[str, Any], worktree_path: Path | None, feature_id: str
) -> str:
    """The build's receipts directory name — one resolution for every consumer.

    The success-path export, the FEAT-DRF failure-path export, the stdout tee
    and the failure manifest must all land in the SAME
    ``<receipts_root>/<build_id>/`` directory, so they share this helper.
    Byte-equivalent to the expression the FEAT-DRC success call site used
    (``payload build_id`` else the worktree directory name) on the first two
    tiers.

    LEGACY fallback (07-30 coach finding 3): a payload with NO ``build_id``
    and NO worktree previously collapsed EVERY such run of a feature into one
    shared ``build-<feature_id>-pending`` pack — a second legacy run silently
    interleaved its logs and overwrote the first run's manifest. The fallback
    is now unique PER RUN (UTC stamp + random token). The wire-side snapshot
    identity (``_build_snapshot``'s ``build-<feature_id>-pending``) is
    deliberately untouched — this names the RECEIPTS directory only, and the
    manifest's ``feature_id``/``correlation_id`` keep the pack correlatable.
    """
    raw = payload.get("build_id")
    if raw:
        return str(raw)
    if worktree_path is not None:
        return worktree_path.name
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"build-{feature_id}-pending-{stamp}-{uuid.uuid4().hex[:6]}"


class _StdoutTee:
    """Best-effort tee of the guardkit subprocess's stdout (FEAT-DRF, Lane 1).

    Until this lane the drain loop scraped its regexes and threw every line
    away (``logger.debug`` only, and the sidecar unit does not run at DEBUG),
    so the guardkit orchestration narrative — decision lines, warnings,
    orchestrator tracebacks — survived nowhere per-build. Each decoded line is
    now appended to ``<receipts_root>/<build_id>/autobuild-stdout.log``.

    Posture (identical to :func:`_export_receipts`): the tee NEVER alters the
    build outcome. The file is opened LAZILY on the first line, so a build that
    prints nothing leaves no file; the handle is line-buffered so a killed or
    timed-out build still leaves the narrative up to the last drained line; and
    the FIRST file error logs one WARNING and permanently disables the tee for
    the run (no per-line log storm, no exception into the drain loop).

    Run scoping (07-30 coach finding 2): the log is APPEND-mode, so a reused
    ``build_id`` (JetStream redelivery / runless re-dispatch) lands a second
    run's narrative in the SAME file. When ``run_header`` is given it is
    written as the first line of this run's segment (on the lazy open, so a
    silent build still leaves no file) — distinct runs are then explicitly
    delimited instead of silently interleaved. A header-less construction
    behaves byte-identically to the pre-finding tee.
    """

    def __init__(self, path: Path, *, run_header: str | None = None) -> None:
        self._path = path
        self._run_header = run_header
        self._handle: TextIO | None = None
        self._disabled = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def disabled(self) -> bool:
        return self._disabled

    def write(self, line: str) -> None:
        if self._disabled:
            return
        try:
            if self._handle is None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._handle = self._path.open(
                    "a", encoding="utf-8", errors="replace", buffering=1
                )
                _harden_pack_permissions(self._path.parent)
                if self._run_header is not None:
                    self._handle.write(f"{self._run_header}\n")
                logger.info(
                    "autobuild_runner: teeing subprocess stdout -> %s", self._path
                )
            self._handle.write(f"{line}\n")
        except Exception as exc:  # noqa: BLE001 — best-effort: never block the drain
            self._disable(exc)

    def _disable(self, exc: BaseException) -> None:
        self._disabled = True
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.close()
            except Exception:  # noqa: BLE001 — already failing; nothing to add
                pass
        logger.warning(
            "autobuild_runner: stdout tee DISABLED for %s (%s: %s) — the build "
            "is unaffected; no further tee attempts this run",
            self._path,
            type(exc).__name__,
            exc,
        )

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.close()
        except Exception as exc:  # noqa: BLE001 — best-effort to the last byte
            logger.warning(
                "autobuild_runner: stdout tee close failed for %s (%s: %s)",
                self._path,
                type(exc).__name__,
                exc,
            )


def _archive_prior_manifest(manifest_path: Path) -> None:
    """Move an earlier run's ``failure-manifest.json`` aside, uniquely named.

    07-30 coach finding 2: a reused ``build_id`` (JetStream redelivery /
    runless re-dispatch) previously OVERWROTE the prior run's manifest — the
    first failure's index was silently lost. The prior file is renamed to
    ``failure-manifest.<stamp>.json`` (stamp from its own ``failed_at``, else
    its mtime), with a numeric suffix on a same-stamp collision, so
    ``failure-manifest.json`` stays the LATEST run (no consumer-visible layout
    change) while every earlier run's manifest survives.

    Best-effort: on ANY error it logs one WARNING and returns — the caller
    then overwrites, which is exactly the pre-finding behaviour (degrade,
    never block the pack).
    """
    try:
        stamp: str | None = None
        try:
            prior = json.loads(manifest_path.read_text(encoding="utf-8"))
            raw = prior.get("failed_at")
            if isinstance(raw, str) and raw:
                stamp = datetime.fromisoformat(raw).strftime("%Y%m%dT%H%M%S%f")
        except Exception:  # noqa: BLE001 — unparseable prior manifest: fall back
            stamp = None
        if stamp is None:
            stamp = datetime.fromtimestamp(
                manifest_path.stat().st_mtime, tz=timezone.utc
            ).strftime("%Y%m%dT%H%M%S%f")
        candidate = manifest_path.with_name(f"failure-manifest.{stamp}.json")
        counter = 1
        while candidate.exists():
            candidate = manifest_path.with_name(
                f"failure-manifest.{stamp}-{counter}.json"
            )
            counter += 1
        manifest_path.rename(candidate)
        logger.info(
            "autobuild_runner: prior failure manifest archived -> %s "
            "(build_id reuse — the earlier run's index is preserved)",
            candidate,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort: never block the pack
        logger.warning(
            "autobuild_runner: could not archive the prior failure manifest "
            "%s (%s: %s) — it will be overwritten (pre-finding behaviour)",
            manifest_path,
            type(exc).__name__,
            exc,
        )


def _write_failure_manifest(
    *,
    build_id: str,
    payload: Mapping[str, Any],
    reason: str,
    timed_out: bool,
    exit_code: int,
    worktree_path: Path | None,
    branch: str | None,
    exported_families: list[str],
) -> None:
    """Write the failure pack's machine-readable index (FEAT-DRF, Lane 1).

    The only cross-layer pointer a failed build left before this lane was the
    reason STRING on ``pipeline.build-failed`` / SQLite; a diagnoser had to
    walk a ``/tmp`` tree that the next reboot deletes. This drops
    ``failure-manifest.json`` beside the exported receipts so the pack is
    self-describing: which build/feature/correlation failed, why, whether the
    kill was a timeout, the exit code, WHERE the kept worktree is, on which
    branch, when, and which receipt families made it out.

    ``failure-manifest.json`` always indexes the LATEST run; an existing
    manifest from an earlier run of a reused ``build_id`` is archived aside
    first (:func:`_archive_prior_manifest`), never silently destroyed.

    Best-effort by the same principle as everything else in the pack: it never
    raises and never alters the build's outcome.
    """
    try:
        dest_root = _receipts_root() / build_id
        dest_root.mkdir(parents=True, exist_ok=True)
        manifest_path = dest_root / FAILURE_MANIFEST_NAME
        if manifest_path.exists():
            _archive_prior_manifest(manifest_path)
        feature_id = payload.get("feature_id")
        correlation_id = payload.get("correlation_id")
        manifest = {
            "build_id": build_id,
            "feature_id": str(feature_id) if feature_id is not None else None,
            "correlation_id": (
                str(correlation_id) if correlation_id is not None else None
            ),
            "reason": reason,
            "timed_out": timed_out,
            "exit_code": exit_code,
            # The failure path KEEPS its worktree (DEFECT #19) — name it so the
            # pack points back at the exact tree the build ran against.
            "worktree_path": (
                str(worktree_path) if worktree_path is not None else None
            ),
            "branch": branch,
            "failed_at": datetime.now(timezone.utc).isoformat(),
            "receipt_families_exported": exported_families,
        }
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        _harden_pack_permissions(dest_root)
        logger.info(
            "autobuild_runner: failure manifest written for %s -> %s",
            build_id,
            manifest_path,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort: never block the terminal flow
        logger.warning(
            "autobuild_runner: failure manifest NOT written for %s (%s: %s) — "
            "the build outcome is unaffected",
            build_id,
            type(exc).__name__,
            exc,
        )


def _export_receipts(worktree_path: Path, build_id: str) -> tuple[bool, list[str]]:
    """Copy the build's receipts out of the worktree before removal (FEAT-DRC).

    On build SUCCESS the outer worktree is removed, which — until this lane —
    destroyed every receipt guardkit wrote there under the isolated topology
    (coach verdicts, evidence dossiers, the spec-conformance snapshot, the
    qav-shadow receipt; proven on FEAT-UDBE 2026-07-28, the M4 blocker). This
    helper copies the :data:`_RECEIPT_FAMILIES` that exist to
    ``$FORGE_RECEIPTS_DIR/<build_id>/`` (default
    ``~/forge-state/receipts/<build_id>/``), preserving relative layout.

    Best-effort by the same principle as :func:`_remove_worktree`: it NEVER
    raises and never alters the build's outcome. Missing families are fine
    (an export of whatever exists — including nothing — is still a success).
    Returns ``(ok, exported_families)``: ``ok`` is ``False`` only on a real
    copy failure, logged at WARNING; the caller then KEEPS the worktree so the
    receipts are never silently lost. ``exported_families`` names ONLY the
    families THIS RUN actually copied (07-30 coach finding 2: the manifest
    previously read the destination back, so families left by an earlier run
    of a reused ``build_id`` were falsely claimed as this run's exports).

    FEAT-DRF also calls this on the FAILURE path, where it is purely additive:
    the worktree is kept there either way, so the copy is a durability upgrade
    (a reboot's ``/tmp`` sweep can no longer destroy the forensics) and the
    ``ok`` component has no bearing on the outcome.
    """
    exported: list[str] = []
    try:
        dest_root = _receipts_root() / build_id
        for family in _RECEIPT_FAMILIES:
            src = worktree_path / family
            if not src.is_dir():
                continue
            dest = dest_root / family
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, dest, dirs_exist_ok=True)
            exported.append(family)
        if exported:
            logger.info(
                "autobuild_runner: receipts exported for %s -> %s (%s)",
                build_id,
                dest_root,
                ", ".join(exported),
            )
        else:
            logger.info(
                "autobuild_runner: no receipt families present in %s for %s "
                "— nothing to export",
                worktree_path,
                build_id,
            )
        _harden_pack_permissions(dest_root)
        return True, exported
    except Exception as exc:  # noqa: BLE001 — best-effort: never block the terminal flow
        logger.warning(
            "autobuild_runner: receipt export FAILED for %s (%s: %s) — the "
            "worktree will be KEPT so the receipts are not lost",
            build_id,
            type(exc).__name__,
            exc,
        )
        return False, exported


async def _finalize_success_worktree(
    repo_path: Path, worktree_path: Path, build_id: str
) -> None:
    """Success-path worktree finalization: export receipts, THEN remove.

    FEAT-DRC ordering crux: the removal is CONDITIONAL on the export —
    an export failure keeps the worktree on disk (the failure path's own
    forensics posture; the F3 preflight prune does not delete directories,
    and a kept tree never regresses a succeeded build).
    """
    ok, _families = _export_receipts(worktree_path, build_id)
    if ok:
        await _remove_worktree(repo_path, worktree_path)
    else:
        logger.warning(
            "autobuild_runner: keeping worktree %s — receipts were not "
            "exported (see the export WARNING above)",
            worktree_path,
        )


async def _remove_worktree(repo_path: Path, worktree_path: Path) -> None:
    """Best-effort worktree removal — called ONLY on the success path.

    On failure the worktree is deliberately KEPT (see DEFECT #19: loud
    failures carry their own forensics), so this helper is never invoked
    there. A cleanup failure on the success path is logged at WARNING and
    swallowed — a leftover worktree does not regress a build that already
    succeeded.
    """
    code, output = await _run_git(
        ["worktree", "remove", "--force", str(worktree_path)],
        cwd=repo_path,
    )
    if code != 0:
        logger.warning(
            "autobuild_runner: worktree cleanup failed for %s (exit=%s): %s "
            "— leaving it on disk; not fatal to the succeeded build",
            worktree_path,
            code,
            output,
        )


def _with_worktree_forensics(reason: str, worktree_path: Path | None) -> str:
    """Append the kept-worktree forensics pointer to a failure ``reason``.

    DEFECT #19: a failed branch-aware build KEEPS its worktree and NAMES it in
    the failure event so an operator can inspect the exact tree the build ran
    against. When no worktree was created (legacy path or pre-worktree
    failure) the reason is returned unchanged.
    """
    if worktree_path is not None:
        return f"{reason} (worktree KEPT for forensics: {worktree_path})"
    return reason


def _build_failed_snapshot(
    payload: Mapping[str, Any],
    *,
    reason: str,
    budget_cap_killed: bool = False,
) -> dict[str, Any]:
    """Construct a ``failed`` snapshot carrying a structured reason.

    The ``reason`` rides the snapshot as the flat ``error_message`` field —
    the exact shape the bridge translator's ``_extract_error_metadata``
    (``forge.lifecycle_bridge.translation``) reads — so the translator's
    ``_build_failed`` puts it on the wire as ``failure_reason`` on the
    ``pipeline.build-failed.<feature_id>`` envelope. (07-30 coach finding 5:
    an earlier revision of this docstring CLAIMED the wire ride without
    setting the field, so every runner failure surfaced as the generic
    ``"autobuild failed (sse)"``; the flat field is what makes the claim
    true, pinned by ``test_translation.py``'s runner-shape wire test.)
    ``error_message`` is an EXTRA key beside the ``AutobuildState`` dump —
    the model's ``extra="ignore"`` keeps any re-validation safe, and the
    ``async_tasks`` reducer copies raw dicts, so the key survives the state
    channel.

    We always set ``tasks_failed=1`` so the bridge's stage_complete delta
    also fires where applicable.

    Args:
        payload: Parsed launch payload (consulted for ``feature_id``,
            ``build_id``, ``correlation_id``).
        reason: Free-form failure reason — written into the runner log
            and surfaced to operators reading the snapshot AND the wire.
        budget_cap_killed: ``True`` ONLY when the runner killed the guardkit
            subprocess at its per-build budget wall-clock cap (FEAT-UBS-002
            stage 1, ``budget_bound`` timeout). Rides the snapshot as a flat
            ``budget_cap_killed`` marker so the daemon can ARM the
            TASK-GATE-D659 pre-dispatch breach gate (Rich's 2026-07-30
            ruling: a cap-KILL is a budget breach; without the marker a
            cap-killed feature's re-queue silently skipped the gate, because
            ``builds.budget_breach`` was only ever written by the
            stage-complete observer and a cap kill precedes any
            stage-complete).

    Returns:
        A snapshot dict suitable for :func:`_snapshot_update`.
    """
    logger.warning("autobuild_runner: transitioning to failed: %s", reason)
    snapshot = _build_snapshot(
        payload,
        lifecycle="failed",
        wave_index=0,
        task_index=0,
        tasks_completed=0,
        tasks_failed=1,
    )
    snapshot["error_message"] = reason
    if budget_cap_killed:
        snapshot["budget_cap_killed"] = True
    return snapshot


async def _node_running_wave(state: AutobuildRunnerState) -> dict[str, Any]:
    """Invoke ``guardkit autobuild`` against the resolved local checkout.

    TASK-ABW-001 — replaces the previous lifecycle-stub body with the
    real subprocess wiring. Responsibilities:

    1. Extract ``repo`` + ``feature_id`` from the launch payload.
    2. Resolve the local checkout via :func:`_resolve_repo_path` and the
       ``guardkit`` binary via :func:`_resolve_guardkit_path`.
    3. On any validation/resolution failure, return a ``failed`` snapshot
       carrying a structured reason — the conditional edge then routes
       the graph to :func:`_node_failed`.
    4. On success, invoke
       ``asyncio.create_subprocess_exec(guardkit_path, "autobuild",
       "feature", feature_id, "--fresh", "--verbose",
       cwd=resolved_repo_path, env=os.environ.copy())`` and stream the
       combined stdout/stderr line-by-line. Each
       ``[guardkit-checkpoint] Turn N complete (tests: pass|fail)`` line
       bumps an internal counter so the returned ``running_wave``
       snapshot carries ``tasks_completed=1`` (stage_complete fallback).
    5. On exit code 0, return a ``running_wave`` snapshot with
       ``tasks_completed=1`` — the conditional edge then routes to
       :func:`_node_completed`.
    6. On non-zero exit, signal, or timeout, kill any surviving process
       and return a ``failed`` snapshot with ``tasks_failed=1`` and
       ``"guardkit autobuild exit=<code>"`` as the reason — the
       conditional edge routes to :func:`_node_failed`.
    """
    payload = _extract_launch_payload(list(state.get("messages", [])))

    feature_id_raw = payload.get("feature_id")
    if not isinstance(feature_id_raw, str) or not feature_id_raw.strip():
        return _snapshot_update(
            _build_failed_snapshot(
                payload, reason="missing feature_id in launch payload"
            )
        )
    feature_id = feature_id_raw.strip()

    # TEMP HOTFIX (TASK-ABW-002): the early guard previously short-circuited
    # before _resolve_repo_path's FORGE_DEFAULT_REPO fallback could fire.
    # Delegate the missing-repo decision entirely to the resolver so the
    # fallback path is reachable.

    repo_path = _resolve_repo_path(payload)
    if repo_path is None:
        return _snapshot_update(
            _build_failed_snapshot(
                payload,
                reason=f"unable to resolve repo path for repo={payload.get('repo')!r}",
            )
        )

    guardkit_path = _resolve_guardkit_path()
    if guardkit_path is None:
        return _snapshot_update(
            _build_failed_snapshot(
                payload,
                reason="guardkit binary not found (PATH lookup + "
                f"{FORGE_GUARDKIT_PATH_ENV} both failed)",
            )
        )

    # DEFECT #19 — branch-aware ISOLATED worktree resolution. When the launch
    # payload carries a ``branch``, materialise a git worktree of that branch
    # from the resolved (shared) checkout and run guardkit there so the shared
    # tree is never mutated. When ``branch`` is absent (legacy CLI launches,
    # the F2-proven path), preserve today's behaviour: run in the shared
    # checkout AS-IS. Either way we log which mode was taken.
    branch_raw = payload.get("branch")
    worktree_path: Path | None = None
    # FEAT-DRF — the branch this build ran against, recorded in the failure
    # manifest (stays None on the legacy no-branch path).
    payload_branch: str | None = None
    # F12 — the base branch guardkit must build its inner autobuild branch on.
    # Threaded from the SAME payload ``branch`` the outer worktree checks out,
    # in the branch-aware path only; stays ``None`` on the legacy no-branch
    # path so that launch is byte-identical. Set below and consumed when the
    # guardkit argv is assembled.
    base_branch: str | None = None
    if isinstance(branch_raw, str) and branch_raw.strip():
        branch = branch_raw.strip()
        base_branch = branch
        payload_branch = branch
        build_id = str(payload.get("build_id") or f"build-{feature_id}-pending")
        if not await _local_branch_exists(repo_path, branch):
            return _snapshot_update(
                _build_failed_snapshot(
                    payload,
                    reason=(
                        f"branch {branch!r} does not exist locally in "
                        f"{repo_path} — refusing to fetch (DEFECT #19: the "
                        "runner reads the local ref only and never touches the "
                        "shared checkout)"
                    ),
                )
            )
        # F3 — preflight residue sweep, scoped to THIS feature's task refs,
        # BEFORE materialising the worktree so a poisoned prior run cannot
        # collide with the fresh add. Never crashes the build.
        await _sweep_build_refs(repo_path, feature_id)
        try:
            worktree_path = await _materialise_worktree(repo_path, branch, build_id)
        except WorktreeMaterialisationError as exc:
            # worktree add failed → git created nothing → no litter to clean.
            return _snapshot_update(
                _build_failed_snapshot(payload, reason=str(exc))
            )
        run_cwd = worktree_path
        logger.info(
            "autobuild_runner: DEFECT#19 isolated-worktree mode feature_id=%s "
            "branch=%s worktree=%s (shared checkout %s left untouched)",
            feature_id,
            branch,
            worktree_path,
            repo_path,
        )
    else:
        run_cwd = repo_path
        logger.info(
            "autobuild_runner: legacy shared-checkout mode feature_id=%s cwd=%s "
            "(no 'branch' in launch payload — F2-proven CLI path preserved, "
            "byte-compatible)",
            feature_id,
            repo_path,
        )

    # FEAT-DRC/FEAT-DRF — one receipts directory name for this build, shared by
    # the stdout tee, the success-path export and the failure pack.
    receipt_build_id = _resolve_receipt_build_id(payload, worktree_path, feature_id)

    # FEAT-UBS-002 (Option-B, stage 1) — the env/default subprocess timeout is
    # the ceiling; a per-build budget wall-clock cap may only TIGHTEN it. Take
    # the MIN so a profile can never LOOSEN the existing bound. When the budget
    # cap is the strictly-binding one, remember it so the timeout-failure reason
    # names the cap (the honest boundary: on expiry the subprocess is genuinely
    # killed below — this lane never claims a stop it did not effect).
    timeout_seconds = _resolve_autobuild_timeout_seconds()
    budget_wallclock = _resolve_budget_wallclock_seconds(payload)
    budget_bound = False
    budget_profile: Any = None
    if budget_wallclock is not None and budget_wallclock < timeout_seconds:
        timeout_seconds = budget_wallclock
        budget_bound = True
        budget_meta = payload.get("budget")
        if isinstance(budget_meta, Mapping):
            budget_profile = budget_meta.get("profile_name")
    argv: list[str] = [
        str(guardkit_path),
        "autobuild",
        "feature",
        feature_id,
        "--fresh",
        "--verbose",
    ]
    # F12 — the outer worktree is materialised DETACHED (F2, _materialise_worktree
    # uses ``git worktree add --detach``), so guardkit's cwd carries NO current
    # branch. guardkit resolves its inner build-branch base as
    # ``--base-branch flag > cwd current branch > 'main'`` (guardkit
    # cli/autobuild.py:582, _detect_base_branch:131). A detached cwd silently
    # falls through to 'main', so a branch-scoped build lands on the WRONG base
    # (receipted live 2026-07-26: FEAT-UCNT built on main's tip d6969df, cured
    # by selective merge 8403739). Pin guardkit's tier-1 precedence explicitly
    # with the SAME branch the outer worktree checked out. The legacy no-branch
    # path leaves ``base_branch`` None → no flag → guardkit's cwd-current-branch
    # resolution keeps working there (that shared checkout is on a named branch).
    if base_branch is not None:
        argv += ["--base-branch", base_branch]

    logger.info(
        "autobuild_runner: launching subprocess feature_id=%s cwd=%s timeout=%ss",
        feature_id,
        run_cwd,
        timeout_seconds,
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(run_cwd),
            env=os.environ.copy(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except (OSError, FileNotFoundError) as exc:
        # Spawn failed AFTER a worktree may have been created — keep it for
        # forensics and name it in the failure event (DEFECT #19).
        return _snapshot_update(
            _build_failed_snapshot(
                payload,
                reason=_with_worktree_forensics(
                    f"failed to spawn guardkit subprocess: {exc!r}",
                    worktree_path,
                ),
            )
        )

    stage_complete_count = 0
    # Coach-score state (TASK-UBS1C-001).
    last_coach_score: float | None = None
    decision_turns: list[str] = []  # "success" or "feedback" per turn

    # FEAT-DRF — durable per-build stdout. Constructed here (not opened: the
    # handle is created lazily on the first line) so a build that prints
    # nothing leaves no empty file.
    stdout_tee = _StdoutTee(
        _receipts_root() / receipt_build_id / STDOUT_LOG_NAME,
        run_header=_stdout_run_header(payload, feature_id),
    )

    async def _drain_stdout() -> None:
        nonlocal stage_complete_count, last_coach_score
        if proc.stdout is None:  # defensive — PIPE was requested above
            return
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip()
                if _GUARDKIT_CHECKPOINT_PATTERN.search(decoded):
                    stage_complete_count += 1
                # Coach-score grammar: parse decision-bearing lines (TASK-UBS1C-001).
                decision = _parse_decision_line(decoded)
                if decision is not None:
                    turn_number, decision_type = decision
                    decision_turns.append(decision_type)
                    last_coach_score = 1.0 if decision_type == "success" else 0.0
                # FEAT-DRF: tee BEFORE the debug log so the narrative survives
                # whatever the journald level is. Never raises (see _StdoutTee).
                stdout_tee.write(decoded)
                logger.debug("autobuild_runner[stdout]: %s", decoded)
        finally:
            # Runs on normal EOF, on the timeout cancel and on the FEAT-FCT
            # interrupt cancel alike — the log is always flushed and closed.
            stdout_tee.close()

    timed_out = False
    try:
        await asyncio.wait_for(
            asyncio.gather(_drain_stdout(), proc.wait()),
            timeout=timeout_seconds,
        )
    except asyncio.CancelledError:
        # FEAT-FCT: a langgraph interrupt (runs.cancel action="interrupt")
        # cancels this node's task. Reap the guardkit child BEFORE the
        # cancellation propagates — without this the child survives
        # orphaned and keeps building (the 2026-07-28 orphan class). The
        # re-raise lets langgraph record the interrupt; no snapshot is
        # emitted, and the worktree survives (removal is success-path-only)
        # so the build's receipts are preserved.
        logger.warning(
            "autobuild_runner: run cancelled (interrupt) feature_id=%s — "
            "killing guardkit subprocess pid=%s",
            feature_id,
            proc.pid,
        )
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            logger.warning(
                "autobuild_runner: subprocess not confirmed dead after "
                "kill() on cancel — pid=%s feature_id=%s",
                proc.pid,
                feature_id,
            )
        raise
    except asyncio.TimeoutError:
        timed_out = True
        logger.warning(
            "autobuild_runner: subprocess timeout after %.1fs feature_id=%s "
            "— killing process",
            timeout_seconds,
            feature_id,
        )
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning(
                "autobuild_runner: subprocess did not exit after kill() — "
                "leaking pid=%s feature_id=%s",
                proc.pid,
                feature_id,
            )

    exit_code = proc.returncode if proc.returncode is not None else -1

    if timed_out or exit_code != 0:
        # A non-zero exit also covers guardkit's own refusal on a missing
        # feature YAML in the (worktree) tree — we delegate that detection to
        # guardkit rather than reimplementing its feature-file discovery, and
        # surface it here as a loud failure that KEEPS the worktree so the
        # exact tree is inspectable (DEFECT #19).
        if timed_out and budget_bound:
            # FEAT-UBS-002 — the per-build budget cap (not the env default) was
            # the binding bound, so name it in the honest failure reason.
            reason = (
                f"guardkit autobuild exceeded the budget wall-clock cap of "
                f"{timeout_seconds}s (profile={budget_profile!r}) — killed "
                "(UBS-002)"
            )
        elif timed_out:
            reason = f"guardkit autobuild timed out after {timeout_seconds}s"
        else:
            reason = f"guardkit autobuild exit={exit_code}"
        # FEAT-DRF (Lane 1) — the FAILURE PACK. Strictly ADDITIVE: the worktree
        # is still KEPT (nothing below removes it) and the reason still carries
        # the forensics pointer; we merely COPY the same three receipt families
        # FEAT-DRC exports on success into <receipts>/<build_id>/ so the
        # evidence outlives the next reboot's /tmp sweep, then drop a manifest
        # indexing the pack. Both are best-effort and cannot alter the outcome.
        # 07-30 coach finding 2: the manifest reports ONLY the families THIS
        # RUN exported (the per-run list `_export_receipts` returns) — never a
        # destination read-back that would claim an earlier run's leftovers.
        exported_families: list[str] = []
        if worktree_path is not None:
            _ok, exported_families = _export_receipts(
                worktree_path, receipt_build_id
            )
        _write_failure_manifest(
            build_id=receipt_build_id,
            payload=payload,
            reason=reason,
            timed_out=timed_out,
            exit_code=exit_code,
            worktree_path=worktree_path,
            branch=payload_branch,
            exported_families=exported_families,
        )
        return _snapshot_update(
            _build_failed_snapshot(
                payload,
                reason=_with_worktree_forensics(reason, worktree_path),
                # Rich's 2026-07-30 ruling: a budget-cap KILL arms the D659
                # breach gate. Only the budget-bound timeout qualifies — an
                # env-default timeout or a plain non-zero exit is NOT a
                # budget breach and must not arm the gate.
                budget_cap_killed=timed_out and budget_bound,
            )
        )

    # Success — clean up the isolated worktree (DEFECT #19: remove on SUCCESS,
    # keep on failure) then return a running_wave snapshot with
    # tasks_completed=1 so the bridge translator's stage_complete delta can
    # fire (and so the state-channel visibly carries a stage_complete-shaped
    # snapshot for the integration test's mid-stream assertion).
    # Coach scores are populated from the decision grammar (TASK-UBS1C-001).
    if worktree_path is not None:
        # FEAT-DRC: export the build's receipts BEFORE removal; on export
        # failure the worktree is kept (see _finalize_success_worktree).
        await _finalize_success_worktree(
            repo_path,
            worktree_path,
            receipt_build_id,
        )
    tasks_completed = max(stage_complete_count, 1)
    # Compute aggregate_coach_score from the decision-bearing turns.
    aggregate_coach_score: float | None = None
    if decision_turns:
        success_count = sum(1 for d in decision_turns if d == "success")
        aggregate_coach_score = success_count / len(decision_turns)
    snapshot = _build_snapshot(
        payload,
        lifecycle="running_wave",
        wave_index=0,
        task_index=0,
        tasks_completed=tasks_completed,
        tasks_failed=0,
    )
    # Inject coach scores into the snapshot dict (TASK-UBS1C-001).
    snapshot["last_coach_score"] = last_coach_score
    snapshot["aggregate_coach_score"] = aggregate_coach_score
    return _snapshot_update(snapshot)


def _node_completed(state: AutobuildRunnerState) -> dict[str, Any]:
    """Transition to ``completed`` — terminal lifecycle.

    Preserves ``last_coach_score`` / ``aggregate_coach_score`` from the
    preceding ``running_wave`` snapshot (TASK-UBS1C-001).
    """
    payload = _extract_launch_payload(list(state.get("messages", [])))
    # Inherit coach scores from the running_wave snapshot if present.
    async_tasks = state.get("async_tasks") or {}
    feature_id = str(payload.get("feature_id") or "FEAT-UNKNOWN")
    prev_snapshot = (
        async_tasks.get(feature_id) if isinstance(async_tasks, Mapping) else None
    )
    last_coach_score = (
        prev_snapshot.get("last_coach_score")
        if isinstance(prev_snapshot, Mapping)
        else None
    )
    aggregate_coach_score = (
        prev_snapshot.get("aggregate_coach_score")
        if isinstance(prev_snapshot, Mapping)
        else None
    )
    snapshot = _build_snapshot(
        payload,
        lifecycle="completed",
        wave_index=int(payload.get("wave_total") or 1) - 1,
        task_index=int(payload.get("task_total") or 1) - 1,
        tasks_completed=int(payload.get("task_total") or 1),
    )
    if last_coach_score is not None:
        snapshot["last_coach_score"] = last_coach_score
    if aggregate_coach_score is not None:
        snapshot["aggregate_coach_score"] = aggregate_coach_score
    return _snapshot_update(snapshot)


def _node_failed(state: AutobuildRunnerState) -> dict[str, Any]:
    """Terminal ``failed`` node (TASK-ABW-001).

    Reachable via the conditional edge from ``running_wave`` when the
    subprocess exits non-zero, times out, or any preconditions fail.
    The body refreshes the snapshot timestamp so observers see the
    transition land as a fresh state-channel write; the lifecycle is
    already ``failed`` from :func:`_node_running_wave`'s return value,
    so this node simply ensures the channel carries a terminal-shaped
    snapshot with ``tasks_failed >= 1``.
    """
    payload = _extract_launch_payload(list(state.get("messages", [])))
    # Preserve any tasks_failed already on the channel; default to 1 so
    # the bridge translator's _build_failed has a non-trivial counter.
    existing = (
        state.get("async_tasks", {}).get(
            str(payload.get("feature_id") or "FEAT-UNKNOWN"), {}
        )
        if isinstance(state.get("async_tasks"), Mapping)
        else {}
    )
    tasks_failed = max(int(existing.get("tasks_failed") or 0), 1)
    snapshot = _build_snapshot(
        payload,
        lifecycle="failed",
        wave_index=int(existing.get("wave_index") or 0),
        task_index=int(existing.get("task_index") or 0),
        tasks_completed=int(existing.get("tasks_completed") or 0),
        tasks_failed=tasks_failed,
    )
    # Preserve the failure metadata the running_wave node stamped on the
    # channel. This refresh is the FINAL state a fetch-on-empty replay
    # (wireup._fetch_and_replay_on_empty) will translate — dropping the flat
    # fields here would strip the wire's failure_reason back to the generic
    # fallback and lose the cap-kill marker the D659 gate arming rides on
    # (Rich's 2026-07-30 ruling) exactly on the fast-failure path where the
    # run finishes before the SSE stream opens.
    error_message = existing.get("error_message")
    if error_message is not None:
        snapshot["error_message"] = error_message
    if existing.get("budget_cap_killed"):
        snapshot["budget_cap_killed"] = True
    return _snapshot_update(snapshot)


def _node_finalize(state: AutobuildRunnerState) -> dict[str, Any]:
    """Structural loud-no-op guard (DEFECT #18b, B4 round-17).

    Every path through the graph funnels here before ``END``. A runner run
    that reaches its end WITHOUT a terminal lifecycle (``completed`` /
    ``cancelled`` / ``failed``) written to the ``async_tasks`` channel is the
    exact silent no-op the July-3 sidecar exhibited: the run ended
    ``status='success'`` having emitted zero lifecycle, and the forge-side
    observer had to infer the failure from a truncated stream. This node makes
    that structurally impossible: if the channel is not terminal for this run's
    ``feature_id``, it forces a loud ``failed`` snapshot with a named error
    rather than letting the graph end clean. The check is centralised here (not
    scattered per node) so it holds regardless of payload shape or any future
    node body.
    """
    payload = _extract_launch_payload(list(state.get("messages", [])))
    feature_id = str(payload.get("feature_id") or "FEAT-UNKNOWN")
    async_tasks = state.get("async_tasks") or {}
    snapshot = (
        async_tasks.get(feature_id) if isinstance(async_tasks, Mapping) else None
    )
    lifecycle = snapshot.get("lifecycle") if isinstance(snapshot, Mapping) else None
    if lifecycle in TERMINAL_LIFECYCLES:
        # Terminal state reached the honest way — nothing to force.
        return {}
    logger.error(
        "autobuild_runner: graph reached finalize WITHOUT a terminal lifecycle "
        "(feature_id=%s observed=%r) — forcing a loud failure (DEFECT #18b "
        "silent-no-op guard). A runner run must never end 'success' without "
        "emitting completed/cancelled/failed.",
        feature_id,
        lifecycle,
    )
    return _snapshot_update(
        _build_failed_snapshot(
            payload,
            reason=(
                "autobuild_runner ended without reaching a terminal lifecycle "
                f"(observed lifecycle={lifecycle!r}); forced failure by the "
                "DEFECT #18b silent-no-op guard so the run fails LOUD instead "
                "of ending 'success' silently"
            ),
        )
    )


def _route_after_running_wave(state: AutobuildRunnerState) -> str:
    """Conditional-edge resolver: ``running_wave`` → ``completed`` | ``failed``.

    Reads ``async_tasks[feature_id].lifecycle`` (the snapshot that
    :func:`_node_running_wave` just wrote) and selects the matching
    terminal node. Returns the key strings registered with
    :meth:`StateGraph.add_conditional_edges`.
    """
    payload = _extract_launch_payload(list(state.get("messages", [])))
    feature_id = str(payload.get("feature_id") or "FEAT-UNKNOWN")
    async_tasks = state.get("async_tasks") or {}
    snapshot = async_tasks.get(feature_id) if isinstance(async_tasks, Mapping) else None
    lifecycle = snapshot.get("lifecycle") if isinstance(snapshot, Mapping) else None
    if lifecycle == "failed":
        return "failed"
    return "completed"


# ---------------------------------------------------------------------------
# Compiled graph — exported for langgraph.json
# ---------------------------------------------------------------------------


#: Captured at module scope by :func:`_build_runner_graph`'s except branch when
#: the real StateGraph fails to construct at import (the DEFECT #18a
#: dependency-drift scenario). The placeholder graph's node reads it so every
#: gate-approved run served by a sidecar that could NOT build its real graph
#: emits a LOUD ``failed`` lifecycle naming this original error — instead of a
#: no-op graph silently 'succeeding'. ``None`` means the real graph built
#: cleanly and the placeholder is never served.
_RUNNER_GRAPH_CONSTRUCTION_ERROR: Exception | None = None


def _build_runner_graph() -> Any:
    """Compile the autobuild_runner graph for ``langgraph.json``.

    Builds a :class:`langgraph.graph.StateGraph` with the
    :class:`AutobuildRunnerState` schema and four lifecycle nodes
    chained linearly. The graph compiles to a
    :class:`CompiledStateGraph` addressable by
    ``AsyncSubAgentMiddleware`` as
    ``graph_id="autobuild_runner"``.

    Why not :func:`deepagents.create_deep_agent`?
        ``create_deep_agent`` fixes the state schema at
        ``AgentState`` + middleware extensions (``messages``, ``todos``,
        ``files``); it does not expose a ``state_schema`` parameter (see
        deepagents 0.5.3 ``graph.py:218``). Without ``async_tasks`` in
        the state schema, the runner's ``stream_mode="values"``
        projection has no channel for the bridge translator's
        ``_extract_state`` to read — every transition is silently
        dropped (FOLLOWUP-B spike, exit branch (b)). Building a
        purpose-shaped ``StateGraph`` directly is the smallest-blast-radius
        fix that closes that contract.

    Fallback: any unexpected construction error returns a minimal
    placeholder graph so ``langgraph.json`` parsing still succeeds. The
    fallback is a safety net for partially-installed dev shells, not a
    production path; the warning log surfaces the regression at startup.

    Returns:
        A compiled state graph addressable as
        ``./src/forge/subagents/autobuild_runner.py:graph``.
    """
    try:
        from langgraph.graph import END, START, StateGraph

        sg: StateGraph[AutobuildRunnerState] = StateGraph(AutobuildRunnerState)
        sg.add_node("starting", _node_starting)
        sg.add_node("planning_waves", _node_planning_waves)
        sg.add_node("running_wave", _node_running_wave)
        sg.add_node("completed", _node_completed)
        sg.add_node("failed", _node_failed)
        # DEFECT #18b: the terminal loud-no-op guard. Every terminal node
        # funnels through it before END so the graph is structurally incapable
        # of ending without a terminal lifecycle on the channel.
        sg.add_node("finalize", _node_finalize)
        sg.add_edge(START, "starting")
        sg.add_edge("starting", "planning_waves")
        sg.add_edge("planning_waves", "running_wave")
        # TASK-ABW-001: ``running_wave`` may write either a
        # ``running_wave`` snapshot (success) or a ``failed`` snapshot
        # (resolution / subprocess failure). The conditional edge below
        # routes to the matching terminal node.
        sg.add_conditional_edges(
            "running_wave",
            _route_after_running_wave,
            {"completed": "completed", "failed": "failed"},
        )
        sg.add_edge("completed", "finalize")
        sg.add_edge("failed", "finalize")
        sg.add_edge("finalize", END)
        return sg.compile()
    except Exception as exc:  # noqa: BLE001 - construction-time safety net
        # Capture the original construction error at module scope so the
        # placeholder graph's node can NAME it on every run (DEFECT #18a).
        global _RUNNER_GRAPH_CONSTRUCTION_ERROR
        _RUNNER_GRAPH_CONSTRUCTION_ERROR = exc
        logger.error(
            "autobuild_runner: StateGraph construction raised %s — "
            "exporting a LOUD-FAILING placeholder graph so every "
            "gate-approved build served by this sidecar fails 'failed' "
            "(never silent 'success'); investigate the construction error "
            "before relying on the subagent",
            exc,
        )
        return _build_placeholder_graph()


def _node_graph_construction_failed(state: AutobuildRunnerState) -> dict[str, Any]:
    """Emit a LOUD ``failed`` snapshot naming the import-time construction error.

    The single node of :func:`_build_placeholder_graph`. When the real runner
    graph could not be constructed at import (DEFECT #18a dependency drift), the
    sidecar STILL boots and serves this placeholder — but instead of the old
    silent no-op (which let every gate-approved build end ``status='success'``
    having emitted zero lifecycle), this node writes a terminal ``failed``
    snapshot to the ``async_tasks`` channel whose reason NAMES the captured
    :data:`_RUNNER_GRAPH_CONSTRUCTION_ERROR`. The bridge translator publishes
    that reason on ``pipeline.build-failed.<feature_id>`` so the failure is
    visible on the wire, not inferred from a truncated stream.
    """
    payload = _extract_launch_payload(list(state.get("messages", [])))
    exc = _RUNNER_GRAPH_CONSTRUCTION_ERROR
    reason = f"autobuild_runner graph failed to construct at import: {exc!r}"
    logger.error(
        "autobuild_runner: serving the placeholder graph — forcing a loud "
        "failed lifecycle for feature_id=%s because the real graph failed to "
        "construct at import (%r)",
        payload.get("feature_id"),
        exc,
    )
    return _snapshot_update(_build_failed_snapshot(payload, reason=reason))


def _build_placeholder_graph() -> Any:
    """Return a LOUD-FAILING compiled :class:`StateGraph`.

    Served only when the production graph cannot be constructed at import
    (:func:`_build_runner_graph`'s except branch, DEFECT #18a). It is a valid,
    servable graph — the sidecar must still boot so it can REPORT the failure —
    but its single node (:func:`_node_graph_construction_failed`) emits a
    terminal ``failed`` lifecycle naming the original construction error rather
    than the old silent no-op that let a broken sidecar 'succeed' every build.

    It uses the same :class:`AutobuildRunnerState` schema (``messages`` +
    ``async_tasks``) as the real graph so the ``AsyncSubAgentMiddleware`` launch
    contract holds and the bridge translator's ``_extract_state`` finds the
    ``failed`` snapshot on the ``async_tasks`` channel.
    """
    from langgraph.graph import END, START, StateGraph

    sg: StateGraph[AutobuildRunnerState] = StateGraph(AutobuildRunnerState)
    sg.add_node("graph_construction_failed", _node_graph_construction_failed)
    sg.add_edge(START, "graph_construction_failed")
    sg.add_edge("graph_construction_failed", END)
    return sg.compile()


def _resolve_runner_code_version() -> str:
    """Best-effort code-version string for the boot-time staleness stamp (#18a).

    Prefers the git rev of the running tree — that is precisely what
    distinguishes a code-stale sidecar (serving an old checkout, as in the B4
    round-17 failure) from a fresh one. The langgraph-api ``/version`` endpoint
    (see :mod:`forge.lifecycle_bridge.version_check`) only reports the SDK
    package version, which is unchanged whether forge's graph code is fresh or
    months old — so it cannot see this. Falls back to the installed ``forge``
    package version, then ``"unknown"``. Never raises: the stamp must not block
    module import at sidecar boot.
    """
    module_dir = Path(__file__).resolve().parent
    try:
        import subprocess  # local import — runs once at module import

        result = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", "-C", str(module_dir), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        rev = result.stdout.strip()
        if rev:
            return f"git-{rev}"
    except Exception:  # noqa: BLE001 — never block import on the stamp
        pass
    try:
        from importlib.metadata import version

        return f"pkg-{version('forge')}"
    except Exception:  # noqa: BLE001
        return "unknown"


#: Import-time code-version stamp (DEFECT #18a). Logged at module import so a
#: code-stale sidecar is visible in the journal — grep for "code version
#: stamp" after any deploy to confirm the sidecar is serving the intended rev.
RUNNER_CODE_VERSION: str = _resolve_runner_code_version()


#: Module-level compiled graph addressed by ``langgraph.json`` as
#: ``./src/forge/subagents/autobuild_runner.py:graph``. Built once at
#: import time; the LangGraph dev server resolves the ``autobuild_runner``
#: graph entry to this object.
graph = _build_runner_graph()


# DEFECT #18a — boot-visible code-version stamp. A ``--no-reload`` sidecar
# only picks up new code on restart; this line is how an operator confirms the
# running process is serving current code (a stale sidecar prints an old rev).
logger.info(
    "autobuild_runner: import-time code version stamp rev=%s "
    "(DEFECT #18a boot-visible staleness signal)",
    RUNNER_CODE_VERSION,
)


__all__ = [
    "AUTOBUILD_RUNNER_NAME",
    "DEFAULT_AUTOBUILD_TIMEOUT_SECONDS",
    "DEFAULT_AUTOBUILD_WORKTREE_BASE",
    "DEFAULT_FORGE_REPO_BASE",
    "FORGE_AUTOBUILD_TIMEOUT_ENV",
    "FORGE_AUTOBUILD_WORKTREE_BASE_ENV",
    "FORGE_GUARDKIT_PATH_ENV",
    "FORGE_REPO_BASE_ENV",
    "RUNNER_CODE_VERSION",
    "AutobuildLifecycle",
    "AutobuildRunnerState",
    "AutobuildState",
    "LIFECYCLE_TO_PIPELINE_EMIT",
    "LIFECYCLE_VALUES",
    "LifecycleEmitterAdapter",
    "StateChannelWriter",
    "SubagentEmitter",
    "TERMINAL_LIFECYCLES",
    "WorktreeConfinementError",
    "WorktreeMaterialisationError",
    "_async_tasks_reducer",
    "_local_branch_exists",
    "_materialise_worktree",
    "_node_failed",
    "_node_finalize",
    "_node_running_wave",
    "_resolve_guardkit_path",
    "_resolve_repo_path",
    "_route_after_running_wave",
    "_update_state",
    "assert_within_worktree",
    "build_stage_complete_kwargs",
    "graph",
]

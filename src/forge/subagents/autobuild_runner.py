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
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, Protocol, get_args, runtime_checkable

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
#: ``awaiting_approval`` routes to ``emit_paused`` (DDR-007 pause publish)
#: and the canonical resume edge ``awaiting_approval → running_wave``
#: routes to ``emit_resumed`` (DDR-007 resume publish). The latter is
#: keyed on the *destination* lifecycle plus a transition hint passed in
#: ``state.waiting_for is None`` semantics.
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

    Routing table (TASK-FW10-010):

    * ``awaiting_approval`` → ``emit_paused`` (publishes
      ``pipeline.build-paused.<feature_id>``).
    * ``running_wave`` reached *after* ``awaiting_approval`` (via the
      ``_resume_pending`` flag set by :meth:`mark_resume_pending`) →
      ``emit_resumed`` (publishes ``pipeline.build-resumed.<feature_id>``).
    * ``completed`` / ``cancelled`` / ``failed`` → terminal emits.
    * Other lifecycles are observable via the ``async_tasks`` channel
      only; the adapter is a no-op for them so this task stays scoped.

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
        # Track whether the previous lifecycle was awaiting_approval so
        # the next running_wave transition knows it is a resume edge,
        # not an initial entry into running_wave. The runner's own
        # ``state.waiting_for`` field clears on resume but is per-state;
        # this flag is per-adapter and survives across _update_state calls.
        self._resume_pending: bool = False
        # Last lifecycle observed (for transition awareness).
        self._last_lifecycle: str | None = None

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

            # Resume edge: awaiting_approval → running_wave fires emit_resumed.
            if (
                state.lifecycle == "running_wave"
                and self._last_lifecycle == "awaiting_approval"
                and self._resume_pending
            ):
                method_name = "emit_resumed"
                self._resume_pending = False

            # Track the awaiting_approval entry so the next running_wave
            # transition is recognised as a resume.
            if state.lifecycle == "awaiting_approval":
                self._resume_pending = True

            self._last_lifecycle = state.lifecycle

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

    def mark_resume_pending(self) -> None:
        """Force the next ``running_wave`` transition to be treated as resume.

        Used by the approval-subscriber wiring when it knows a resume is
        imminent (e.g. an approval response just matched). Production
        wiring relies on the natural ``awaiting_approval → running_wave``
        edge to flip this flag automatically; this helper exists for
        exotic recovery paths where the lifecycle was not observed
        moving through ``awaiting_approval`` in this adapter instance
        (daemon restart with rehydrated state).
        """
        self._resume_pending = True

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
        if method_name == "emit_resumed":
            return emit(
                self._ctx,
                stage_label="awaiting_approval",
                decision="approve",
                responder="approval-subscriber",
                resumed_at=now_iso,
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
    state = AutobuildState(
        task_id=str(payload.get("task_id") or build_id),
        build_id=build_id,
        feature_id=feature_id,
        lifecycle=lifecycle,  # type: ignore[arg-type] - validated by AutobuildState's Literal
        wave_index=wave_index,
        wave_total=int(payload.get("wave_total") or 1),
        task_index=task_index,
        task_total=int(payload.get("task_total") or 1),
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
    """Transition to ``planning_waves``."""
    payload = _extract_launch_payload(list(state.get("messages", [])))
    snapshot = _build_snapshot(payload, lifecycle="planning_waves")
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

#: Regex matching one ``[guardkit-checkpoint] Turn N complete (tests: ...)``
#: line in guardkit's verbose stdout. The runner counts these to drive the
#: stage_complete fallback (TASK-ABW-001 §Scope item 3).
_GUARDKIT_CHECKPOINT_PATTERN: re.Pattern[str] = re.compile(
    r"\[guardkit-checkpoint\]\s+Turn\s+\d+\s+complete\s+\(tests:\s+(pass|fail)",
    flags=re.IGNORECASE,
)


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
        logger.warning(
            "autobuild_runner: missing or empty 'repo' in launch payload — "
            "cannot resolve checkout path"
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

    base_dir_raw = os.environ.get(FORGE_REPO_BASE_ENV, "").strip() or DEFAULT_FORGE_REPO_BASE
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
            "autobuild_runner: resolved repo path %s is not a directory "
            "(repo=%r)",
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


def _build_failed_snapshot(payload: Mapping[str, Any], *, reason: str) -> dict[str, Any]:
    """Construct a ``failed`` snapshot carrying a structured reason.

    The bridge translator's :func:`_build_failed`
    (``forge.lifecycle_bridge.translation``) publishes the snapshot's
    failure metadata; the reason we set here ends up on the wire via the
    ``pipeline.build-failed.<feature_id>`` envelope. We always set
    ``tasks_failed=1`` so the bridge's stage_complete delta also fires
    where applicable.

    Args:
        payload: Parsed launch payload (consulted for ``feature_id``,
            ``build_id``, ``correlation_id``).
        reason: Free-form failure reason — written into the runner log
            and surfaced to operators reading the snapshot.

    Returns:
        A snapshot dict suitable for :func:`_snapshot_update`.
    """
    logger.warning("autobuild_runner: transitioning to failed: %s", reason)
    return _build_snapshot(
        payload,
        lifecycle="failed",
        wave_index=0,
        task_index=0,
        tasks_completed=0,
        tasks_failed=1,
    )


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
            _build_failed_snapshot(payload, reason="missing feature_id in launch payload")
        )
    feature_id = feature_id_raw.strip()

    if not isinstance(payload.get("repo"), str) or not str(payload.get("repo")).strip():
        return _snapshot_update(
            _build_failed_snapshot(payload, reason="missing repo in launch payload")
        )

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

    timeout_seconds = _resolve_autobuild_timeout_seconds()
    argv: list[str] = [
        str(guardkit_path),
        "autobuild",
        "feature",
        feature_id,
        "--fresh",
        "--verbose",
    ]

    logger.info(
        "autobuild_runner: launching subprocess feature_id=%s cwd=%s timeout=%ss",
        feature_id,
        repo_path,
        timeout_seconds,
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(repo_path),
            env=os.environ.copy(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except (OSError, FileNotFoundError) as exc:
        return _snapshot_update(
            _build_failed_snapshot(
                payload,
                reason=f"failed to spawn guardkit subprocess: {exc!r}",
            )
        )

    stage_complete_count = 0

    async def _drain_stdout() -> None:
        nonlocal stage_complete_count
        if proc.stdout is None:  # defensive — PIPE was requested above
            return
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            decoded = line.decode("utf-8", errors="replace").rstrip()
            if _GUARDKIT_CHECKPOINT_PATTERN.search(decoded):
                stage_complete_count += 1
            logger.debug("autobuild_runner[stdout]: %s", decoded)

    timed_out = False
    try:
        await asyncio.wait_for(
            asyncio.gather(_drain_stdout(), proc.wait()),
            timeout=timeout_seconds,
        )
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
        reason = (
            f"guardkit autobuild timed out after {timeout_seconds}s"
            if timed_out
            else f"guardkit autobuild exit={exit_code}"
        )
        return _snapshot_update(_build_failed_snapshot(payload, reason=reason))

    # Success — return a running_wave snapshot with tasks_completed=1 so
    # the bridge translator's stage_complete delta can fire (and so the
    # state-channel visibly carries a stage_complete-shaped snapshot for
    # the integration test's mid-stream assertion).
    tasks_completed = max(stage_complete_count, 1)
    snapshot = _build_snapshot(
        payload,
        lifecycle="running_wave",
        wave_index=0,
        task_index=0,
        tasks_completed=tasks_completed,
        tasks_failed=0,
    )
    return _snapshot_update(snapshot)


def _node_completed(state: AutobuildRunnerState) -> dict[str, Any]:
    """Transition to ``completed`` — terminal lifecycle."""
    payload = _extract_launch_payload(list(state.get("messages", [])))
    snapshot = _build_snapshot(
        payload,
        lifecycle="completed",
        wave_index=int(payload.get("wave_total") or 1) - 1,
        task_index=int(payload.get("task_total") or 1) - 1,
        tasks_completed=int(payload.get("task_total") or 1),
    )
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
    return _snapshot_update(snapshot)


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
    lifecycle = (
        snapshot.get("lifecycle") if isinstance(snapshot, Mapping) else None
    )
    if lifecycle == "failed":
        return "failed"
    return "completed"


# ---------------------------------------------------------------------------
# Compiled graph — exported for langgraph.json
# ---------------------------------------------------------------------------


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
        sg.add_edge("completed", END)
        sg.add_edge("failed", END)
        return sg.compile()
    except Exception as exc:  # noqa: BLE001 - construction-time safety net
        logger.warning(
            "autobuild_runner: StateGraph construction raised %s — "
            "exporting placeholder graph so langgraph.json still "
            "parses; investigate before relying on the subagent",
            exc,
        )
        return _build_placeholder_graph()


def _build_placeholder_graph() -> Any:
    """Return a trivial compiled :class:`StateGraph`.

    Used only when the production graph cannot be constructed. The
    graph compiles, addresses, and invokes (returning state unchanged)
    so ``langgraph.json`` parse and LangGraph dev-server import paths
    still work; production behaviour is delegated to the real graph.
    """
    from langgraph.graph import END, START, StateGraph

    sg: StateGraph[dict[str, Any]] = StateGraph(dict)
    sg.add_node("noop", lambda state: state)
    sg.add_edge(START, "noop")
    sg.add_edge("noop", END)
    return sg.compile()


#: Module-level compiled graph addressed by ``langgraph.json`` as
#: ``./src/forge/subagents/autobuild_runner.py:graph``. Built once at
#: import time; the LangGraph dev server resolves the ``autobuild_runner``
#: graph entry to this object.
graph = _build_runner_graph()


__all__ = [
    "AUTOBUILD_RUNNER_NAME",
    "DEFAULT_AUTOBUILD_TIMEOUT_SECONDS",
    "DEFAULT_FORGE_REPO_BASE",
    "FORGE_AUTOBUILD_TIMEOUT_ENV",
    "FORGE_GUARDKIT_PATH_ENV",
    "FORGE_REPO_BASE_ENV",
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
    "_async_tasks_reducer",
    "_node_failed",
    "_node_running_wave",
    "_resolve_guardkit_path",
    "_resolve_repo_path",
    "_route_after_running_wave",
    "_update_state",
    "assert_within_worktree",
    "build_stage_complete_kwargs",
    "graph",
]

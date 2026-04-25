"""Pipeline lifecycle emission — wires state-machine transitions to the publisher.

This module is the producer side of the FEAT-FORGE-002 ``pipeline.*`` event
family. It owns the mapping from the FEAT-FORGE-001 build state machine's
transitions to the eight publish methods on
:class:`forge.adapters.nats.PipelinePublisher` (TASK-NFI-006).

Responsibilities (per TASK-NFI-008 acceptance criteria):

- Every state-machine transition listed in the task brief triggers exactly
  one publish call (AC-001).
- Every published payload carries the originating
  :attr:`~nats_core.events.BuildQueuedPayload.correlation_id`. v1 payloads
  (``BuildStarted``, ``BuildProgress``, ``BuildComplete``, ``BuildFailed``)
  declare ``model_config = ConfigDict(extra="ignore")`` and have no
  ``correlation_id`` field; we attach it post-construction with
  :func:`object.__setattr__` so the publisher's
  ``getattr(payload, "correlation_id", None)`` still threads it onto the
  envelope without forcing a nats-core schema change (AC-002).
- ``publish_build_paused`` is awaited **before** the LangGraph
  ``interrupt()`` callback fires — :meth:`PipelineLifecycleEmitter.emit_paused_then_interrupt`
  enforces the ordering even if the publish raises (AC-003, Group D).
- :class:`~forge.adapters.nats.PublishFailure` is logged and swallowed.
  The state machine has already written the SQLite row that motivated the
  emission; rolling back on a transient NATS hiccup would corrupt the
  source of truth (AC-004, Group E).
- A long-running ``RUNNING`` stage fires
  :meth:`forge.adapters.nats.PipelinePublisher.publish_build_progress`
  at least every
  :attr:`~forge.config.models.PipelineConfig.progress_interval_seconds`
  (ASSUM-005) using an injected :class:`Clock`; tests inject
  :class:`FakeClock` to advance time deterministically (AC-005).
- The same publish is also fired on wave boundaries via
  :meth:`PipelineLifecycleEmitter.emit_wave_boundary_progress`, called by
  ``autobuild_runner`` when a wave commits (AC-006).

Design notes:

- The emitter is I/O-thin. It owns the publisher reference, the
  :class:`PipelineConfig`, and a :class:`Clock`; it never touches SQLite
  or the NATS client directly.
- Progress tasks are tracked per ``build_id`` so concurrent builds (a
  defensive position — ADR-ARCH-014 caps in-flight builds at one) cannot
  leak ``asyncio.Task`` instances.
- :class:`State` mirrors the FEAT-FORGE-001 build state names so callers
  can pass them straight through ``on_transition``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Protocol, runtime_checkable

from nats_core.events import (
    BuildCancelledPayload,
    BuildCompletePayload,
    BuildFailedPayload,
    BuildPausedPayload,
    BuildProgressPayload,
    BuildResumedPayload,
    BuildStartedPayload,
    StageCompletePayload,
)

from forge.adapters.nats import PipelinePublisher, PublishFailure
from forge.config.models import PipelineConfig

logger = logging.getLogger(__name__)


__all__ = [
    "BuildContext",
    "Clock",
    "FakeClock",
    "PipelineLifecycleEmitter",
    "RealClock",
    "State",
    "TRANSITION_TO_EMITTER",
    "attach_correlation_id",
]


# ---------------------------------------------------------------------------
# State enum — mirrors FEAT-FORGE-001 build states
# ---------------------------------------------------------------------------


class State(str, Enum):
    """Build state names mirrored from the FEAT-FORGE-001 state machine.

    Stored as a ``str`` Enum so callers that already hold the underlying
    string (``"RUNNING"`` etc.) can pass it without coercion.
    """

    PREPARING = "PREPARING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    FINALISING = "FINALISING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# ---------------------------------------------------------------------------
# Clock protocol + implementations (real + fake)
# ---------------------------------------------------------------------------


@runtime_checkable
class Clock(Protocol):
    """Minimal clock surface the progress loop depends on.

    Two methods only — :meth:`sleep` to wait for the next tick, and
    :meth:`now` for elapsed-time stamping. Tests substitute
    :class:`FakeClock`; production wires :class:`RealClock`.
    """

    async def sleep(self, seconds: float) -> None:  # pragma: no cover - protocol stub
        ...

    def now(self) -> float:  # pragma: no cover - protocol stub
        ...


class RealClock:
    """Production :class:`Clock`. Delegates to :mod:`asyncio` and :mod:`time`."""

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    def now(self) -> float:
        return time.monotonic()


@dataclass
class FakeClock:
    """Deterministic clock for tests.

    Coroutines that ``await fake_clock.sleep(N)`` block until enough virtual
    time has been advanced via :meth:`advance`. This sidesteps both wall-clock
    flakiness and the need for ``pytest-asyncio``'s ``event_loop`` plumbing.

    The implementation is intentionally simple — a sorted list of
    (deadline, :class:`asyncio.Event`) tuples. ``advance`` walks the list
    once per call and sets every event whose deadline is now in the past.
    """

    now_value: float = 0.0
    _waiters: list[tuple[float, asyncio.Event]] = field(default_factory=list)

    def now(self) -> float:
        return self.now_value

    async def sleep(self, seconds: float) -> None:
        deadline = self.now_value + seconds
        event = asyncio.Event()
        self._waiters.append((deadline, event))
        await event.wait()

    async def advance(self, seconds: float) -> None:
        """Advance virtual time by ``seconds`` and wake any due waiters.

        After waking, yields several times with ``await asyncio.sleep(0)``
        so the woken tasks chain through whatever internal awaits they
        own (``_safe_publish`` → ``publish_method`` → ``emit_progress``)
        and re-register on the next ``clock.sleep`` waiter before this
        coroutine returns. Three iterations covers the deepest await
        chain the progress loop produces today; if the loop body ever
        grows more await points, bump this count.
        """
        self.now_value += seconds
        due = [w for w in self._waiters if w[0] <= self.now_value]
        self._waiters = [w for w in self._waiters if w[0] > self.now_value]
        for _, event in due:
            event.set()
        # Drain pending awaits so the woken loop body can complete one
        # iteration and re-suspend on the next clock.sleep.
        for _ in range(5):
            await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# BuildContext — per-build identity threaded through every transition
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BuildContext:
    """Per-build identity carried through every lifecycle emission.

    Constructed once at ``PREPARING → RUNNING`` from the originating
    :class:`~nats_core.events.BuildQueuedPayload` and reused on every
    subsequent transition. The frozen + slotted shape makes it cheap to
    pass by reference and impossible to mutate the correlation_id by
    accident downstream.

    Attributes:
        feature_id: ``FEAT-XXXX`` identifier of the build.
        build_id: ``build-{feature_id}-{YYYYMMDDHHMMSS}`` identifier
            allocated when the state machine writes the SQLite row for
            ``PREPARING``.
        correlation_id: Originating ID from
            :class:`~nats_core.events.BuildQueuedPayload`. Threaded
            unchanged onto every published envelope.
        wave_total: Total number of waves planned for the build. Pinned
            here so progress payloads are consistent across the run.
    """

    feature_id: str
    build_id: str
    correlation_id: str
    wave_total: int


# ---------------------------------------------------------------------------
# Helper: attach correlation_id onto a v1 payload
# ---------------------------------------------------------------------------


def attach_correlation_id(payload: object, correlation_id: str) -> None:
    """Attach ``correlation_id`` to a Pydantic v1 payload post-construction.

    The v1 lifecycle payloads (``BuildStartedPayload``,
    ``BuildProgressPayload``, ``BuildCompletePayload``,
    ``BuildFailedPayload``) declare ``model_config = ConfigDict(extra="ignore")``
    and so silently drop a ``correlation_id`` kwarg passed to ``__init__``.
    The publisher reads correlation_id off the payload via
    ``getattr(payload, "correlation_id", None)``; we therefore attach the
    value with :func:`object.__setattr__`, bypassing pydantic's
    ``__setattr__`` which validates against the declared field set.

    Net effect: the envelope's ``correlation_id`` is set, the payload's
    ``model_dump`` output is unchanged (still v1-compatible on the wire),
    and no nats-core schema change is required.

    Args:
        payload: Any pydantic v1 lifecycle payload.
        correlation_id: The originating correlation ID to thread.
    """
    object.__setattr__(payload, "correlation_id", correlation_id)


# ---------------------------------------------------------------------------
# Transition → emitter-method map
# ---------------------------------------------------------------------------


# Mapping from a (from_state, to_state) tuple to the name of the
# :class:`PipelineLifecycleEmitter` method that handles it. ``None`` for
# either side acts as a wildcard ("any state"). The dispatcher in
# :meth:`PipelineLifecycleEmitter.on_transition` walks this table top-down
# and the first match wins, so ordering matters: specific rules first,
# wildcards last.
TRANSITION_TO_EMITTER: list[tuple[State | None, State | None, str]] = [
    (State.PREPARING, State.RUNNING, "emit_started"),
    (State.RUNNING, State.PAUSED, "emit_paused"),
    (State.PAUSED, State.RUNNING, "emit_resumed"),
    (State.FINALISING, State.COMPLETE, "emit_complete"),
    (None, State.FAILED, "emit_failed"),
    (None, State.CANCELLED, "emit_cancelled"),
]


# ---------------------------------------------------------------------------
# Emitter
# ---------------------------------------------------------------------------


class PipelineLifecycleEmitter:
    """Bridge between the build state machine and :class:`PipelinePublisher`.

    The emitter exposes one ``emit_*`` coroutine per lifecycle event plus
    a generic :meth:`on_transition` dispatcher. Every emit method:

    1. Builds the typed payload.
    2. Threads ``ctx.correlation_id`` onto the payload (via
       :func:`attach_correlation_id` for v1 payloads).
    3. Awaits the corresponding publisher method.
    4. Catches :class:`PublishFailure` and logs at ``ERROR`` — the SQLite
       row stays intact (AC-004).

    Args:
        publisher: The :class:`PipelinePublisher` from TASK-NFI-006.
        config: Pipeline configuration; the only field consumed today is
            :attr:`~forge.config.models.PipelineConfig.progress_interval_seconds`.
        clock: Injected :class:`Clock`. Defaults to :class:`RealClock`;
            tests inject :class:`FakeClock`.
    """

    def __init__(
        self,
        publisher: PipelinePublisher,
        config: PipelineConfig,
        clock: Clock | None = None,
    ) -> None:
        self._publisher = publisher
        self._config = config
        self._clock: Clock = clock if clock is not None else RealClock()
        # build_id -> running progress task. Exposed as a property for
        # test introspection without making the dict itself mutable.
        self._progress_tasks: dict[str, asyncio.Task[None]] = {}

    # ------------------------------------------------------------------
    # Public introspection helpers (used by tests)
    # ------------------------------------------------------------------

    @property
    def progress_tasks(self) -> dict[str, asyncio.Task[None]]:
        """Snapshot of the per-build progress tasks (read-only by convention)."""
        return self._progress_tasks

    # ------------------------------------------------------------------
    # Emit methods — one per lifecycle event
    # ------------------------------------------------------------------

    async def emit_started(self, ctx: BuildContext) -> None:
        """Publish ``pipeline.build-started.{feature_id}`` (PREPARING → RUNNING)."""
        payload = BuildStartedPayload(
            feature_id=ctx.feature_id,
            build_id=ctx.build_id,
            wave_total=ctx.wave_total,
        )
        attach_correlation_id(payload, ctx.correlation_id)
        await self._safe_publish(
            self._publisher.publish_build_started, payload, "build-started"
        )

    async def emit_progress(
        self,
        ctx: BuildContext,
        *,
        wave: int,
        overall_progress_pct: float,
        elapsed_seconds: int,
    ) -> None:
        """Publish ``pipeline.build-progress.{feature_id}`` (heartbeat / wave)."""
        payload = BuildProgressPayload(
            feature_id=ctx.feature_id,
            build_id=ctx.build_id,
            wave=wave,
            wave_total=ctx.wave_total,
            overall_progress_pct=overall_progress_pct,
            elapsed_seconds=elapsed_seconds,
        )
        attach_correlation_id(payload, ctx.correlation_id)
        await self._safe_publish(
            self._publisher.publish_build_progress, payload, "build-progress"
        )

    async def emit_stage_complete(
        self,
        ctx: BuildContext,
        *,
        stage_label: str,
        target_kind: str,
        target_identifier: str,
        status: str,
        gate_mode: str | None,
        coach_score: float | None,
        duration_secs: float,
        completed_at: str,
    ) -> None:
        """Publish ``pipeline.stage-complete.{feature_id}`` (per-stage commit).

        Called *after* the ``StageLogEntry`` row is committed in SQLite by
        the state machine — the row is the source of truth and is never
        rolled back even if this publish raises.
        """
        payload = StageCompletePayload(
            feature_id=ctx.feature_id,
            build_id=ctx.build_id,
            stage_label=stage_label,
            target_kind=target_kind,  # type: ignore[arg-type]
            target_identifier=target_identifier,
            status=status,  # type: ignore[arg-type]
            gate_mode=gate_mode,  # type: ignore[arg-type]
            coach_score=coach_score,
            duration_secs=duration_secs,
            completed_at=completed_at,
            correlation_id=ctx.correlation_id,
        )
        await self._safe_publish(
            self._publisher.publish_stage_complete, payload, "stage-complete"
        )

    async def emit_paused(
        self,
        ctx: BuildContext,
        *,
        stage_label: str,
        gate_mode: str,
        coach_score: float | None,
        rationale: str,
        approval_subject: str,
        paused_at: str,
    ) -> None:
        """Publish ``pipeline.build-paused.{feature_id}`` (gate fired).

        Use :meth:`emit_paused_then_interrupt` instead at the actual gate
        site to satisfy the "publish BEFORE ``interrupt()`` fires" ordering
        guarantee (Group D). This method exists for the test suite and for
        callers that handle the interrupt themselves.
        """
        payload = BuildPausedPayload(
            feature_id=ctx.feature_id,
            build_id=ctx.build_id,
            stage_label=stage_label,
            gate_mode=gate_mode,  # type: ignore[arg-type]
            coach_score=coach_score,
            rationale=rationale,
            approval_subject=approval_subject,
            paused_at=paused_at,
            correlation_id=ctx.correlation_id,
        )
        await self._safe_publish(
            self._publisher.publish_build_paused, payload, "build-paused"
        )

    async def emit_paused_then_interrupt(
        self,
        ctx: BuildContext,
        *,
        stage_label: str,
        gate_mode: str,
        coach_score: float | None,
        rationale: str,
        approval_subject: str,
        paused_at: str,
        interrupt: Callable[[], Awaitable[None]],
    ) -> None:
        """Publish ``build-paused`` BEFORE awaiting ``interrupt()``.

        Group D scenario "Flagging a stage for human review publishes a
        build-paused event" demands strict ordering: the paused event must
        be observable on the wire before the LangGraph ``interrupt()``
        callback runs. We therefore await the publish first, swallow any
        :class:`PublishFailure` (so the interrupt still fires — the SQLite
        row already says PAUSED, so consumers will see consistent state on
        replay), and only then await ``interrupt()``.

        Args:
            ctx: Build context with the originating correlation_id.
            stage_label: Pipeline stage that triggered the pause.
            gate_mode: One of FLAG_FOR_REVIEW / HARD_STOP /
                MANDATORY_HUMAN_APPROVAL.
            coach_score: Coach quality score, or None.
            rationale: Human-readable explanation.
            approval_subject: NATS subject for the approval response.
            paused_at: ISO 8601 timestamp.
            interrupt: Async callable that triggers the LangGraph
                ``interrupt()``. Awaited *after* the publish.
        """
        await self.emit_paused(
            ctx,
            stage_label=stage_label,
            gate_mode=gate_mode,
            coach_score=coach_score,
            rationale=rationale,
            approval_subject=approval_subject,
            paused_at=paused_at,
        )
        await interrupt()

    async def emit_resumed(
        self,
        ctx: BuildContext,
        *,
        stage_label: str,
        decision: str,
        responder: str,
        resumed_at: str,
    ) -> None:
        """Publish ``pipeline.build-resumed.{feature_id}`` (after approval)."""
        payload = BuildResumedPayload(
            feature_id=ctx.feature_id,
            build_id=ctx.build_id,
            stage_label=stage_label,
            decision=decision,  # type: ignore[arg-type]
            responder=responder,
            resumed_at=resumed_at,
            correlation_id=ctx.correlation_id,
        )
        await self._safe_publish(
            self._publisher.publish_build_resumed, payload, "build-resumed"
        )

    async def emit_complete(
        self,
        ctx: BuildContext,
        *,
        repo: str | None,
        branch: str | None,
        tasks_completed: int,
        tasks_failed: int,
        tasks_total: int,
        pr_url: str | None,
        duration_seconds: int,
        summary: str,
    ) -> None:
        """Publish ``pipeline.build-complete.{feature_id}`` (terminal: PR open)."""
        payload = BuildCompletePayload(
            feature_id=ctx.feature_id,
            build_id=ctx.build_id,
            repo=repo,
            branch=branch,
            tasks_completed=tasks_completed,
            tasks_failed=tasks_failed,
            tasks_total=tasks_total,
            pr_url=pr_url,
            duration_seconds=duration_seconds,
            summary=summary,
        )
        attach_correlation_id(payload, ctx.correlation_id)
        await self._safe_publish(
            self._publisher.publish_build_complete, payload, "build-complete"
        )

    async def emit_failed(
        self,
        ctx: BuildContext,
        *,
        failure_reason: str,
        recoverable: bool,
        failed_task_id: str | None,
    ) -> None:
        """Publish ``pipeline.build-failed.{feature_id}`` (terminal failure)."""
        payload = BuildFailedPayload(
            feature_id=ctx.feature_id,
            build_id=ctx.build_id,
            failure_reason=failure_reason,
            recoverable=recoverable,
            failed_task_id=failed_task_id,
        )
        attach_correlation_id(payload, ctx.correlation_id)
        await self._safe_publish(
            self._publisher.publish_build_failed, payload, "build-failed"
        )

    async def emit_cancelled(
        self,
        ctx: BuildContext,
        *,
        reason: str,
        cancelled_by: str,
        cancelled_at: str,
    ) -> None:
        """Publish ``pipeline.build-cancelled.{feature_id}`` (operator cancel)."""
        payload = BuildCancelledPayload(
            feature_id=ctx.feature_id,
            build_id=ctx.build_id,
            reason=reason,
            cancelled_by=cancelled_by,
            cancelled_at=cancelled_at,
            correlation_id=ctx.correlation_id,
        )
        await self._safe_publish(
            self._publisher.publish_build_cancelled, payload, "build-cancelled"
        )

    # ------------------------------------------------------------------
    # Wave-boundary hook — called by autobuild_runner on wave commits
    # ------------------------------------------------------------------

    async def emit_wave_boundary_progress(
        self,
        ctx: BuildContext,
        *,
        wave: int,
        overall_progress_pct: float,
        elapsed_seconds: int,
    ) -> None:
        """Fire a progress publish on a wave boundary (autobuild_runner hook).

        The orchestrator's ``autobuild_runner`` calls this when a wave's
        last task commits. It is functionally identical to
        :meth:`emit_progress` but exists as a named hook so the wave-boundary
        emission point is auditable in code (AC-006).
        """
        await self.emit_progress(
            ctx,
            wave=wave,
            overall_progress_pct=overall_progress_pct,
            elapsed_seconds=elapsed_seconds,
        )

    # ------------------------------------------------------------------
    # Generic transition dispatcher
    # ------------------------------------------------------------------

    async def on_transition(
        self,
        from_state: State,
        to_state: State,
        ctx: BuildContext,
        **payload_kwargs: object,
    ) -> None:
        """Dispatch a state-machine transition to the matching emit method.

        Walks :data:`TRANSITION_TO_EMITTER` and calls the first method whose
        (from_state, to_state) pattern matches. Wildcard rules (``None``)
        match any state on that side. Transitions that do not appear in the
        table (e.g. ``PREPARING → FAILED`` while still validating inputs)
        log at ``DEBUG`` and silently no-op — the table is intentionally
        the only source of truth for "which transitions emit".

        Extra keyword args are forwarded as-is to the chosen emit method.
        Mismatched kwargs surface as :class:`TypeError` from the emit
        method, which is the right level of strictness.

        Args:
            from_state: State the build is transitioning from.
            to_state: State the build is transitioning to.
            ctx: Build context.
            **payload_kwargs: Forwarded to the emit method.
        """
        for from_pat, to_pat, method_name in TRANSITION_TO_EMITTER:
            if from_pat is not None and from_pat != from_state:
                continue
            if to_pat is not None and to_pat != to_state:
                continue
            method = getattr(self, method_name)
            await method(ctx, **payload_kwargs)
            return
        logger.debug(
            "pipeline lifecycle: no emitter for transition %s -> %s; skipping",
            from_state,
            to_state,
        )

    # ------------------------------------------------------------------
    # Periodic progress loop
    # ------------------------------------------------------------------

    async def start_progress_loop(
        self,
        ctx: BuildContext,
        get_progress: Callable[[], tuple[int, float, int]],
    ) -> asyncio.Task[None]:
        """Schedule a periodic progress publish for ``ctx``.

        The returned :class:`asyncio.Task` calls ``get_progress`` after
        every :attr:`PipelineConfig.progress_interval_seconds` tick of the
        injected :class:`Clock` and then awaits :meth:`emit_progress` with
        the returned ``(wave, overall_progress_pct, elapsed_seconds)``
        tuple. The task runs until :meth:`stop_progress_loop` cancels it
        (or the build leaves ``RUNNING`` and the state machine forgets to
        — see the cancellation handling in
        :meth:`stop_progress_loop`).

        Args:
            ctx: Build context.
            get_progress: Sync callable that snapshots the current
                ``(wave, overall_progress_pct, elapsed_seconds)``. Sync
                because callers typically read it from in-memory counters.

        Returns:
            The scheduled task. Stored internally keyed by
            ``ctx.build_id`` so :meth:`stop_progress_loop` can cancel it.
        """
        existing = self._progress_tasks.get(ctx.build_id)
        if existing is not None and not existing.done():
            logger.warning(
                "progress loop already running for build_id=%s; not starting "
                "a second one",
                ctx.build_id,
            )
            return existing

        interval = float(self._config.progress_interval_seconds)
        clock = self._clock
        emitter = self

        async def _loop() -> None:
            try:
                while True:
                    await clock.sleep(interval)
                    wave, pct, elapsed = get_progress()
                    await emitter.emit_progress(
                        ctx,
                        wave=wave,
                        overall_progress_pct=pct,
                        elapsed_seconds=elapsed,
                    )
            except asyncio.CancelledError:
                # Re-raise so the task ends with CANCELLED state (the
                # documented contract for cancelled tasks); callers must
                # not treat cancellation as a failure.
                raise

        task = asyncio.create_task(
            _loop(), name=f"pipeline-progress-{ctx.build_id}"
        )
        self._progress_tasks[ctx.build_id] = task
        # Yield once so the task starts and registers its first
        # clock.sleep waiter before this coroutine returns. Without
        # this, a caller that immediately advances the clock would
        # find no waiters to wake (the task hasn't run yet).
        await asyncio.sleep(0)
        return task

    async def stop_progress_loop(self, build_id: str) -> None:
        """Cancel and reap the progress loop for ``build_id``.

        Idempotent — calling on an already-stopped or never-started build
        is a debug-level no-op.
        """
        task = self._progress_tasks.pop(build_id, None)
        if task is None:
            logger.debug(
                "stop_progress_loop: no task registered for build_id=%s",
                build_id,
            )
            return
        if task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            # Expected — we just cancelled it.
            pass

    # ------------------------------------------------------------------
    # Internal: publish wrapper that swallows PublishFailure
    # ------------------------------------------------------------------

    async def _safe_publish(
        self,
        publish_method: Callable[..., Awaitable[None]],
        payload: object,
        event_label: str,
    ) -> None:
        """Await ``publish_method(payload)``; log + swallow PublishFailure.

        AC-004: the SQLite row that motivated the emission has already been
        written. Re-raising would force the state machine to choose between
        rolling back (data loss) or wrapping every transition in its own
        try/except. We do the latter once, here, so callers remain linear.

        Any other exception escapes — they indicate logic bugs (e.g. payload
        validation failure, programmer error in the transition map) and the
        state machine should fail loudly so the build is marked FAILED.
        """
        try:
            await publish_method(payload)
        except PublishFailure as exc:
            logger.error(
                "pipeline lifecycle: publish failed event=%s subject=%s "
                "cause=%s — SQLite state retained",
                event_label,
                getattr(exc, "subject", "<unknown>"),
                getattr(exc, "cause", exc),
            )

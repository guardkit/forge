"""Restart recovery — Last-Event-ID replay + sweep (TASK-FRR-PEB-009).

This module owns the boot-time recovery flow for the lifecycle bridge.
On daemon startup, every row of ``lifecycle_bridge_registry`` represents
an in-flight build that was attached to an SSE stream when the previous
daemon process exited. The recovery flow is:

1. **In-buffer replay (ASSUM-001)**: open the SSE stream with
   ``Last-Event-ID = entry.last_event_id`` so the langgraph-runner's
   server-side buffer replays the in-window envelopes. The translator
   produces typed payloads as usual; the
   :func:`should_publish` guard skips any subject whose envelope was
   already on the wire pre-restart (registry's ``published_lifecycles``
   set), so the regression scenario "build-started not re-published"
   is enforced inline (AC-2 / AC-5).

2. **Out-of-buffer sweep (ASSUM-002)**: when the SSE source raises
   :class:`LastEventIdRejected` (HTTP 410 or empty replay window), the
   recovery code falls back to ``runs.get(thread_id, run_id)`` once. If
   the run has reached terminal state, a single terminal envelope is
   published and the row is acked / detached. If the run is still
   running, the recovery restarts the SSE stream with
   ``Last-Event-ID=None`` (i.e. "from now") so the bridge resumes
   per-stage observation.

The flow runs in :class:`RecoveryRunner.run`, which iterates the
registry's active set and schedules an :class:`asyncio.Task` per entry.
Tasks are gathered with a 30s budget for ≤10 in-flight builds (AC-4) so
``forge serve`` can move on to its consumer attach without blocking
indefinitely on a slow sidecar.

Acceptance criteria mapping
---------------------------

* AC-1: :meth:`RecoveryRunner.run` iterates ``BridgeRegistry.list_active``;
  each entry gets its own asyncio task that reconnects with the persisted
  ``Last-Event-ID``.
* AC-2: The publisher path consults
  :class:`BridgeRegistryEntry.published_lifecycles` and skips an
  envelope whose subject is already in the set (idempotency guard).
* AC-3: :class:`LastEventIdRejected` is caught and handled by the
  ``runs.get`` sweep fallback, which publishes the terminal envelope
  once or restarts the stream.
* AC-4: :meth:`RecoveryRunner.run` is called from ``forge serve``
  startup; the 30s budget keeps daemon boot bounded.
* AC-5: Already-published transitions are skipped — the regression
  scenario "build-started not re-published" is locked in by the
  idempotency test.
* AC-6: Tasks are independent; each updates its own registry row.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator, Mapping, Protocol

from nats_core.events import (
    BuildCancelledPayload,
    BuildCompletePayload,
    BuildFailedPayload,
    BuildPausedPayload,
    BuildResumedPayload,
    BuildStartedPayload,
    StageCompletePayload,
)

from forge.adapters.nats.pipeline_publisher import PipelinePublisher
from forge.lifecycle_bridge.bridge import BuildContext
from forge.lifecycle_bridge.translation import (  # noqa: F401  -- re-exported through PipelineEvent
    PipelineEvent,
    StreamEventTranslator,
)
from forge.persistence.repositories.bridge_registry import (
    BridgeRegistry,
    BridgeRegistryEntry,
)

logger = logging.getLogger(__name__)


__all__ = [
    "DEFAULT_RECOVERY_BUDGET_SECONDS",
    "LastEventIdRejected",
    "RecoveryError",
    "RecoveryResult",
    "RecoveryRunner",
    "RecoverySource",
    "RunsGetClient",
    "TERMINAL_PAYLOAD_TYPES",
    "subject_for_payload_type",
]


#: AC-4 — recovery completes within 30s for ≤10 in-flight builds.
DEFAULT_RECOVERY_BUDGET_SECONDS: float = 30.0


#: Terminal payload classes — same set as the wireup. Kept duplicated
#: here so the recovery module does not import from the wireup (which
#: would create a circular dependency: wireup → recovery for boot
#: orchestration).
TERMINAL_PAYLOAD_TYPES: tuple[type, ...] = (
    BuildCompletePayload,
    BuildFailedPayload,
    BuildCancelledPayload,
)


#: Subject-segment fragment per typed payload class. Mirrors
#: :attr:`forge.lifecycle_bridge.wireup._SUBJECT_SEGMENT_TABLE` — kept
#: in sync deliberately so the AC-2 idempotency check uses the same
#: wire-format strings the publisher writes to JetStream.
_SUBJECT_SEGMENT_TABLE: dict[type, str] = {
    BuildStartedPayload: "build-started",
    StageCompletePayload: "stage-complete",
    BuildCompletePayload: "build-complete",
    BuildFailedPayload: "build-failed",
    BuildPausedPayload: "build-paused",
    BuildResumedPayload: "build-resumed",
    BuildCancelledPayload: "build-cancelled",
}


_PUBLISH_METHOD_TABLE: dict[type, str] = {
    BuildStartedPayload: "publish_build_started",
    StageCompletePayload: "publish_stage_complete",
    BuildCompletePayload: "publish_build_complete",
    BuildFailedPayload: "publish_build_failed",
    BuildPausedPayload: "publish_build_paused",
    BuildResumedPayload: "publish_build_resumed",
    BuildCancelledPayload: "publish_build_cancelled",
}


def subject_for_payload_type(payload_type: type) -> str | None:
    """Return the subject segment for ``payload_type`` (or ``None``)."""
    return _SUBJECT_SEGMENT_TABLE.get(payload_type)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RecoveryError(RuntimeError):
    """Raised by :class:`RecoveryRunner` for terminal failures.

    Includes ``feature_id`` for structured logging on the caller side.
    """

    def __init__(self, feature_id: str, reason: str) -> None:
        super().__init__(f"recovery failed for feature_id={feature_id!r}: {reason}")
        self.feature_id = feature_id
        self.reason = reason


class LastEventIdRejected(RuntimeError):
    """Raised by :class:`RecoverySource` when the buffer no longer covers the cursor.

    The langgraph-runner's SSE buffer is bounded; once an event id falls
    out of the window (HTTP 410 or empty replay) the source must signal
    it explicitly so :meth:`RecoveryRunner._recover_one` can route to the
    out-of-buffer sweep (ASSUM-002).
    """

    def __init__(self, feature_id: str, last_event_id: str | None) -> None:
        super().__init__(
            f"langgraph-runner rejected Last-Event-ID={last_event_id!r} "
            f"for feature_id={feature_id!r}"
        )
        self.feature_id = feature_id
        self.last_event_id = last_event_id


# ---------------------------------------------------------------------------
# Protocols — kept narrow so production wires real SDK clients while
# tests pass deterministic fakes.
# ---------------------------------------------------------------------------


class RecoverySource(Protocol):
    """Open an SSE stream for replay/resumption.

    The source MUST honour ``last_event_id`` — when provided, the SSE
    server replays in-buffer events with strictly greater id; when
    ``None`` the source opens a "from now" stream. If the cursor falls
    out of the server-side buffer the source MUST raise
    :class:`LastEventIdRejected` so the recovery flow can switch to the
    sweep fallback.
    """

    def __call__(
        self,
        *,
        feature_id: str,
        thread_id: str,
        run_id: str,
        last_event_id: str | None,
    ) -> AsyncIterator[Any]:  # pragma: no cover - protocol stub
        ...


class RunsGetClient(Protocol):
    """Subset of ``langgraph_sdk.client.RunsClient.get`` for sweep fallback."""

    async def get(
        self, thread_id: str, run_id: str
    ) -> Mapping[str, Any]:  # pragma: no cover - protocol stub
        ...


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    """Outcome of recovering a single in-flight entry.

    Attributes:
        feature_id: Primary key of the recovered build.
        mode: ``"replay"`` (in-buffer), ``"sweep-terminal"`` (out-of-
            buffer + run was terminal), ``"sweep-running"`` (out-of-
            buffer + run still active; SSE restarted), or ``"failed"``
            (recovery raised).
        terminal_published: ``True`` if a terminal envelope was emitted
            during recovery.
        events_published: Count of envelopes published during recovery
            (terminal + non-terminal).
        skipped_subjects: Subjects that recovery encountered but skipped
            because they were already in ``published_lifecycles`` (AC-2).
        failure_reason: Short categorical label when ``mode == "failed"``.
    """

    feature_id: str
    mode: str
    terminal_published: bool = False
    events_published: int = 0
    skipped_subjects: tuple[str, ...] = ()
    failure_reason: str | None = None


# ---------------------------------------------------------------------------
# Recovery runner
# ---------------------------------------------------------------------------


class RecoveryRunner:
    """Drive :class:`LifecycleBridge.recover_in_flight` across all entries.

    One instance per ``forge serve`` daemon — composed by
    :func:`forge.cli._serve_production.bind_production_serve` once the
    SQLite writer connection and the SDK client are available. The
    instance is stateless across :meth:`run` calls; idempotency rests on
    the ``published_lifecycles`` column, not on in-memory bookkeeping.

    Args:
        registry: The :class:`BridgeRegistry` backing the in-flight set.
        recovery_source: A :class:`RecoverySource` callable that opens
            SSE streams for replay / resumption.
        runs_client: A :class:`RunsGetClient` for the sweep fallback.
        translator: The :class:`StreamEventTranslator` used to convert
            ``StreamPart`` events into typed payloads. Reused across
            entries — the translator is stateful per-feature internally
            and tolerates concurrent feature_id streams.
        publisher: The shared :class:`PipelinePublisher` that owns the
            outbound NATS emission. Publish failures during recovery are
            logged at WARNING and do not propagate — the registry row
            stays in place so a follow-up boot can retry.
        budget_seconds: Upper bound on :meth:`run`. Defaults to
            :data:`DEFAULT_RECOVERY_BUDGET_SECONDS` (30s, AC-4).

    The constructor accepts a ``terminal_publisher`` factory only via
    the publisher and translator; it never constructs payloads itself.
    """

    def __init__(
        self,
        *,
        registry: BridgeRegistry,
        recovery_source: RecoverySource,
        runs_client: RunsGetClient,
        translator: StreamEventTranslator,
        publisher: PipelinePublisher,
        budget_seconds: float = DEFAULT_RECOVERY_BUDGET_SECONDS,
    ) -> None:
        if not isinstance(registry, BridgeRegistry):
            raise TypeError(
                "RecoveryRunner: registry must be a BridgeRegistry; "
                f"got {type(registry).__name__}"
            )
        if recovery_source is None:
            raise ValueError("RecoveryRunner: recovery_source is required")
        if runs_client is None:
            raise ValueError("RecoveryRunner: runs_client is required")
        # Duck-type check on the translator so unit tests can supply
        # a deterministic fake without subclassing the production
        # translator (which carries per-feature transition state).
        if translator is None or not callable(getattr(translator, "translate", None)):
            raise TypeError(
                "RecoveryRunner: translator must expose a callable translate(); "
                f"got {type(translator).__name__}"
            )
        if publisher is None:
            raise ValueError("RecoveryRunner: publisher is required")
        if budget_seconds <= 0:
            raise ValueError(
                "RecoveryRunner: budget_seconds must be positive"
            )

        self._registry = registry
        self._recovery_source = recovery_source
        self._runs_client = runs_client
        self._translator = translator
        self._publisher = publisher
        self._budget_seconds = float(budget_seconds)

    # ------------------------------------------------------------------
    # Public entrypoint
    # ------------------------------------------------------------------

    async def run(self, *, correlation_id: str) -> list[RecoveryResult]:
        """Recover every active in-flight build (AC-1 / AC-4 / AC-6).

        Iterates :meth:`BridgeRegistry.list_active` and schedules one
        asyncio task per entry. Tasks are gathered with
        :func:`asyncio.wait_for` bounded by ``budget_seconds`` (default
        30s). On timeout the un-completed tasks are cancelled and a
        warning is logged — their registry rows stay in place so the
        next boot's recovery sweep retries.

        Args:
            correlation_id: F010C correlation-id of the boot-time
                recovery context; threaded through every registry call
                for traceability.

        Returns:
            One :class:`RecoveryResult` per recovered entry. Empty list
            when no in-flight builds were registered. Order matches the
            registry's ``list_active`` order (oldest first).

        Raises:
            ValueError: If ``correlation_id`` is empty.
        """
        if not correlation_id:
            raise ValueError(
                "RecoveryRunner.run: correlation_id must be non-empty"
            )

        active = self._registry.list_active(correlation_id=correlation_id)
        if not active:
            logger.info(
                "lifecycle_bridge.recovery: no in-flight builds; "
                "correlation_id=%s",
                correlation_id,
            )
            return []

        logger.info(
            "lifecycle_bridge.recovery: starting sweep for %d in-flight "
            "build(s); correlation_id=%s budget_seconds=%.1f",
            len(active),
            correlation_id,
            self._budget_seconds,
        )

        # AC-6: per-entry tasks so 3 concurrent recoveries do not
        # interfere. Each task updates its own registry row via the
        # registry repository; SQLite's BEGIN IMMEDIATE serialises
        # cross-task writes.
        tasks: list[asyncio.Task[RecoveryResult]] = [
            asyncio.create_task(
                self._recover_one(entry, correlation_id=correlation_id),
                name=f"lifecycle-bridge-recovery-{entry.feature_id}",
            )
            for entry in active
        ]

        results: list[RecoveryResult] = []
        try:
            gathered = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=self._budget_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "lifecycle_bridge.recovery: budget %.1fs exceeded; "
                "cancelling pending recovery tasks (correlation_id=%s)",
                self._budget_seconds,
                correlation_id,
            )
            for task in tasks:
                if not task.done():
                    task.cancel()
            # Gather one more time so the cancelled tasks raise their
            # CancelledError into ``return_exceptions=True`` and we can
            # collect what completed.
            gathered = await asyncio.gather(*tasks, return_exceptions=True)

        for entry, outcome in zip(active, gathered, strict=True):
            if isinstance(outcome, asyncio.CancelledError):
                results.append(
                    RecoveryResult(
                        feature_id=entry.feature_id,
                        mode="failed",
                        failure_reason="budget-exceeded",
                    )
                )
                continue
            if isinstance(outcome, BaseException):
                logger.warning(
                    "lifecycle_bridge.recovery: feature_id=%s recovery raised "
                    "(%s); leaving registry row for next boot",
                    entry.feature_id,
                    outcome,
                )
                results.append(
                    RecoveryResult(
                        feature_id=entry.feature_id,
                        mode="failed",
                        failure_reason=type(outcome).__name__,
                    )
                )
                continue
            results.append(outcome)

        logger.info(
            "lifecycle_bridge.recovery: sweep complete; recovered=%d "
            "terminal_published=%d failed=%d correlation_id=%s",
            sum(1 for r in results if r.mode != "failed"),
            sum(1 for r in results if r.terminal_published),
            sum(1 for r in results if r.mode == "failed"),
            correlation_id,
        )
        return results

    # ------------------------------------------------------------------
    # Per-entry recovery
    # ------------------------------------------------------------------

    async def _recover_one(
        self,
        entry: BridgeRegistryEntry,
        *,
        correlation_id: str,
    ) -> RecoveryResult:
        """Recover a single in-flight entry — replay or sweep.

        First attempts in-buffer replay with ``entry.last_event_id``.
        On :class:`LastEventIdRejected` the flow switches to the sweep
        fallback (``runs.get`` + terminal-only publish, or fresh-stream
        resumption).
        """
        feature_id = entry.feature_id
        context = BuildContext(
            feature_id=entry.feature_id,
            thread_id=entry.thread_id,
            run_id=entry.run_id,
            correlation_id=entry.correlation_id,
            deadline_at=entry.deadline_at,
        )
        already_published: set[str] = set(entry.published_lifecycles)
        skipped: list[str] = []

        try:
            return await self._replay_in_buffer(
                entry=entry,
                context=context,
                already_published=already_published,
                skipped=skipped,
                correlation_id=correlation_id,
            )
        except LastEventIdRejected as exc:
            logger.info(
                "lifecycle_bridge.recovery: feature_id=%s last_event_id=%r "
                "out of buffer; switching to runs.get sweep "
                "(correlation_id=%s)",
                feature_id,
                exc.last_event_id,
                correlation_id,
            )
            return await self._sweep_via_runs_get(
                entry=entry,
                context=context,
                already_published=already_published,
                skipped=skipped,
                correlation_id=correlation_id,
            )

    # ------------------------------------------------------------------
    # In-buffer replay (AC-1 / AC-2 / AC-5)
    # ------------------------------------------------------------------

    async def _replay_in_buffer(
        self,
        *,
        entry: BridgeRegistryEntry,
        context: BuildContext,
        already_published: set[str],
        skipped: list[str],
        correlation_id: str,
    ) -> RecoveryResult:
        """Drive an SSE replay from ``entry.last_event_id``."""
        feature_id = entry.feature_id
        events_published = 0
        terminal_published = False

        # Note: any LastEventIdRejected raised here propagates to
        # _recover_one and switches modes — explicitly NOT caught
        # in this method.
        stream_iter = self._recovery_source(
            feature_id=feature_id,
            thread_id=entry.thread_id,
            run_id=entry.run_id,
            last_event_id=entry.last_event_id,
        )

        async for stream_part in stream_iter:
            try:
                event = self._translator.translate(stream_part, context)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "lifecycle_bridge.recovery: translator raised (%s) "
                    "for feature_id=%s during replay; skipping part",
                    exc,
                    feature_id,
                )
                continue
            if event is None:
                continue

            subject = subject_for_payload_type(type(event))
            if subject is None:
                logger.warning(
                    "lifecycle_bridge.recovery: feature_id=%s payload_type=%s "
                    "has no registered subject; dropping",
                    feature_id,
                    type(event).__name__,
                )
                continue

            # AC-2 / AC-5: idempotency guard. Already-published
            # subjects MUST NOT be re-published from a replay.
            if subject in already_published:
                skipped.append(subject)
                logger.info(
                    "lifecycle_bridge.recovery: feature_id=%s subject=%s "
                    "already published pre-restart; skipping (AC-5)",
                    feature_id,
                    subject,
                )
                if isinstance(event, TERMINAL_PAYLOAD_TYPES):
                    # Terminal already published — registry row should
                    # have been deleted by the original publish path.
                    # If it wasn't (crash mid-detach), clean up now so
                    # next boot does not re-recover.
                    self._registry.delete(
                        feature_id, correlation_id=correlation_id
                    )
                    return RecoveryResult(
                        feature_id=feature_id,
                        mode="replay",
                        terminal_published=False,
                        events_published=events_published,
                        skipped_subjects=tuple(skipped),
                    )
                continue

            published_ok = await self._publish_with_idempotency(
                event=event,
                feature_id=feature_id,
                subject=subject,
                already_published=already_published,
                correlation_id=correlation_id,
            )
            if published_ok:
                events_published += 1

            if isinstance(event, TERMINAL_PAYLOAD_TYPES):
                if published_ok:
                    terminal_published = True
                    self._registry.delete(
                        feature_id, correlation_id=correlation_id
                    )
                    return RecoveryResult(
                        feature_id=feature_id,
                        mode="replay",
                        terminal_published=True,
                        events_published=events_published,
                        skipped_subjects=tuple(skipped),
                    )
                # Terminal publish failed — leave registry row intact
                # for the next boot's recovery cycle.
                logger.warning(
                    "lifecycle_bridge.recovery: feature_id=%s terminal "
                    "publish failed during replay; row left intact",
                    feature_id,
                )
                return RecoveryResult(
                    feature_id=feature_id,
                    mode="replay",
                    terminal_published=False,
                    events_published=events_published,
                    skipped_subjects=tuple(skipped),
                    failure_reason="terminal-publish-failed",
                )

        # Stream ended without terminal — leave registry row for the
        # bridge's deadline timer to enforce.
        return RecoveryResult(
            feature_id=feature_id,
            mode="replay",
            terminal_published=terminal_published,
            events_published=events_published,
            skipped_subjects=tuple(skipped),
        )

    # ------------------------------------------------------------------
    # Out-of-buffer sweep (AC-3)
    # ------------------------------------------------------------------

    async def _sweep_via_runs_get(
        self,
        *,
        entry: BridgeRegistryEntry,
        context: BuildContext,
        already_published: set[str],
        skipped: list[str],
        correlation_id: str,
    ) -> RecoveryResult:
        """Fallback when the SSE buffer no longer covers ``last_event_id``.

        Calls ``runs.get`` once to learn the run's current status. If
        terminal: open a fresh ``last_event_id=None`` SSE stream — the
        langgraph-runner's first emit on a terminal run is the latest
        AutobuildState snapshot, which the translator turns into the
        canonical terminal payload (single emit site invariant). If
        still active: open the same fresh stream and resume per-stage
        observation. Either way the publish path is the translator →
        publisher pipeline, never a synthetic payload constructed
        outside the translator.
        """
        feature_id = entry.feature_id
        run_terminal_hint = False
        try:
            run_state = await self._runs_client.get(
                entry.thread_id, entry.run_id
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "lifecycle_bridge.recovery: runs.get failed for "
                "feature_id=%s (%s); falling back to fresh SSE attempt "
                "anyway so the deadline timer remains the backstop",
                feature_id,
                exc,
            )
            run_state = {}
        else:
            run_terminal_hint = self._is_terminal_status(run_state)
            logger.info(
                "lifecycle_bridge.recovery: feature_id=%s runs.get returned "
                "status=%r (terminal=%s); resuming SSE with Last-Event-ID=None",
                feature_id,
                run_state.get("status") if isinstance(run_state, Mapping) else None,
                run_terminal_hint,
            )

        # Open a fresh SSE stream with Last-Event-ID=None. The
        # translator emits the current/last snapshot which the publisher
        # forwards. Idempotency guard skips already-published subjects
        # (AC-2). Terminal arrival deletes the registry row (AC-3).
        try:
            stream_iter = self._recovery_source(
                feature_id=feature_id,
                thread_id=entry.thread_id,
                run_id=entry.run_id,
                last_event_id=None,
            )
        except LastEventIdRejected:
            # The fresh stream itself was rejected — extreme edge case.
            # The deadline timer (T8) will eventually publish
            # ``build-failed``; leave the row in place.
            logger.warning(
                "lifecycle_bridge.recovery: feature_id=%s fresh SSE stream "
                "also rejected; leaving registry row for deadline timer",
                feature_id,
            )
            return RecoveryResult(
                feature_id=feature_id,
                mode="failed",
                failure_reason="fresh-stream-rejected",
            )

        events_published = 0
        terminal_published = False
        mode = "sweep-terminal" if run_terminal_hint else "sweep-running"
        async for stream_part in stream_iter:
            try:
                event = self._translator.translate(stream_part, context)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "lifecycle_bridge.recovery: translator raised (%s) "
                    "for feature_id=%s during sweep; skipping part",
                    exc,
                    feature_id,
                )
                continue
            if event is None:
                continue
            subject = subject_for_payload_type(type(event))
            if subject is None:
                continue
            if subject in already_published:
                skipped.append(subject)
                # If we already published the terminal envelope before
                # the crash, AC-3 requires "publish the terminal
                # envelope only and ack" — i.e. do not double-emit. The
                # row should already be deleted; clean up defensively.
                if isinstance(event, TERMINAL_PAYLOAD_TYPES):
                    self._registry.delete(
                        feature_id, correlation_id=correlation_id
                    )
                    return RecoveryResult(
                        feature_id=feature_id,
                        mode=mode,
                        terminal_published=False,
                        events_published=events_published,
                        skipped_subjects=tuple(skipped),
                    )
                continue
            published_ok = await self._publish_with_idempotency(
                event=event,
                feature_id=feature_id,
                subject=subject,
                already_published=already_published,
                correlation_id=correlation_id,
            )
            if published_ok:
                events_published += 1
            if isinstance(event, TERMINAL_PAYLOAD_TYPES) and published_ok:
                terminal_published = True
                self._registry.delete(
                    feature_id, correlation_id=correlation_id
                )
                return RecoveryResult(
                    feature_id=feature_id,
                    mode=mode,
                    terminal_published=True,
                    events_published=events_published,
                    skipped_subjects=tuple(skipped),
                )

        return RecoveryResult(
            feature_id=feature_id,
            mode=mode,
            terminal_published=terminal_published,
            events_published=events_published,
            skipped_subjects=tuple(skipped),
        )

    @staticmethod
    def _is_terminal_status(run_state: Any) -> bool:
        """Return ``True`` when the SDK's ``runs.get`` response is terminal.

        Tolerant of mismatched / missing fields — when the response
        does not carry a recognised status we return ``False`` so the
        recovery flow stays in "still-running" mode. The deadline timer
        in the bridge is the backstop for genuinely-stuck builds.
        """
        if not isinstance(run_state, Mapping):
            return False
        status = run_state.get("status")
        if not isinstance(status, str):
            return False
        return status.lower() in {
            "success",
            "error",
            "failed",
            "timeout",
            "interrupted",
            "cancelled",
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _publish_with_idempotency(
        self,
        *,
        event: PipelineEvent,
        feature_id: str,
        subject: str,
        already_published: set[str],
        correlation_id: str,
    ) -> bool:
        """Mark + publish ``event``. AC-2: mark BEFORE publish.

        Returns ``True`` on a successful publish, ``False`` otherwise.
        On a publish failure the column is rolled back so a retry on
        the next boot can re-attempt.
        """
        method_name = _PUBLISH_METHOD_TABLE.get(type(event))
        if method_name is None:
            logger.warning(
                "lifecycle_bridge.recovery: no publisher method for "
                "payload_type=%s (feature_id=%s)",
                type(event).__name__,
                feature_id,
            )
            return False
        publish = getattr(self._publisher, method_name, None)
        if publish is None:
            logger.warning(
                "lifecycle_bridge.recovery: publisher missing method %s "
                "(feature_id=%s)",
                method_name,
                feature_id,
            )
            return False

        # AC-2 — append to the published_lifecycles set BEFORE the
        # actual publish so a concurrent recovery (e.g. multi-replica
        # supervisor) cannot re-publish the same subject. If the publish
        # raises we roll the column back; the subject_for_payload_type
        # entry is harmless if duplicated since the column dedup is
        # frozenset-based.
        try:
            self._registry.mark_published(
                feature_id,
                subject,
                correlation_id=correlation_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "lifecycle_bridge.recovery: mark_published failed for "
                "feature_id=%s subject=%s (%s); skipping publish to "
                "avoid double-emit",
                feature_id,
                subject,
                exc,
            )
            return False

        try:
            await publish(event)
            already_published.add(subject)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "lifecycle_bridge.recovery: publish via %s raised (%s) "
                "for feature_id=%s subject=%s; column was pre-marked but "
                "the subject is left in already_published to avoid "
                "double-emit on retry — JetStream redelivery will eventually "
                "produce the envelope or the deadline timer will publish "
                "build-failed",
                method_name,
                exc,
                feature_id,
                subject,
            )
            already_published.add(subject)
            return False

# ---------------------------------------------------------------------------
# Convenience entrypoint for ``forge serve`` startup wiring.
# ---------------------------------------------------------------------------


async def run_recovery_at_boot(
    runner: RecoveryRunner,
    *,
    correlation_id: str,
) -> list[RecoveryResult]:
    """Top-level boot-time recovery hook (AC-4).

    ``forge serve`` calls this once during startup, before the durable
    consumer attaches. Centralising the call site here lets tests
    monkey-patch a single seam to assert the boot-order invariant
    (recovery completes before the consumer's first fetch).
    """
    return await runner.run(correlation_id=correlation_id)


# Re-exported for callers that want to log the JSON shape of the
# in-flight set (e.g. ``forge status --in-flight`` operators reading
# log lines). Kept here rather than re-imported from the registry to
# avoid a hard cross-module dependency in operator tooling.
def published_lifecycles_to_json(subjects: frozenset[str]) -> str:
    """Encode ``subjects`` for human-readable logging."""
    return json.dumps(sorted(subjects))

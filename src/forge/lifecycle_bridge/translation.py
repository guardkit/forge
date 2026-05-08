"""SSE → typed pipeline envelope translation layer (TASK-FRR-PEB-003).

This module exposes :class:`StreamEventTranslator`, the producer side of
the §4 ``STREAM_EVENT_SCHEMA`` integration contract. The translator maps
``langgraph_sdk`` :class:`~langgraph_sdk.schema.StreamPart` events from
``client.runs.join_stream(stream_mode="values")`` into typed ``pipeline.*``
envelope payloads:

* :class:`~nats_core.events.BuildStartedPayload`
* :class:`~nats_core.events.StageCompletePayload`
* :class:`~nats_core.events.BuildCompletePayload`
* :class:`~nats_core.events.BuildFailedPayload`
* :class:`~nats_core.events.BuildPausedPayload`
* :class:`~nats_core.events.BuildResumedPayload`
* :class:`~nats_core.events.BuildCancelledPayload`

The translator is **stateful** per build: it remembers the previously
observed ``AutobuildState`` snapshot keyed by ``feature_id`` so it can
detect transitions (``planning_waves → running_wave``,
``awaiting_approval → running_wave``, monotonic ``tasks_completed``
deltas, etc.). The ``stream_mode="values"`` SSE channel carries full
``AutobuildState`` snapshots — the translator never gets a delta — so
diffing against the prior snapshot is the canonical detection mechanism
(see :mod:`forge.subagents.autobuild_runner`).

Acceptance-criteria mapping
---------------------------

* AC-1: :class:`StreamEventTranslator` exposes
  ``translate(stream_part, context) -> PipelineEvent | None``.
* AC-2: every documented :attr:`StreamPart.event` value is handled —
  ``"values"`` triggers transition detection, every other event (
  ``"metadata"``, ``"messages"``, ``"updates"``, ``"events"``, ...) is
  logged at ``DEBUG`` and returns ``None``. **Unknown events are routine
  during langgraph-api minor bumps**, hence DEBUG (not WARNING).
* AC-3: :attr:`BuildContext.correlation_id` is required — a missing
  field raises :class:`MissingCorrelationIdError` (no silent fallback).

Option C / Option E note (from the scoping doc)
------------------------------------------------

This translator is the dominant Option C risk surface — if the
``StreamPart`` shape proves insufficient (silent schema drift across
``langgraph-api`` minor versions), the contract test fixture replay will
fail and the wave is reshaped to consume D-NATS per-stage events
(Option E). The pivot decision lives at the smoke-gate failure of Wave
2; this module does not pivot mid-implementation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping

from langgraph_sdk.schema import StreamPart
from nats_core.events import (
    BuildCancelledPayload,
    BuildCompletePayload,
    BuildFailedPayload,
    BuildPausedPayload,
    BuildResumedPayload,
    BuildStartedPayload,
    StageCompletePayload,
)

from forge.lifecycle_bridge.bridge import BuildContext

logger = logging.getLogger(__name__)


__all__ = [
    "MissingCorrelationIdError",
    "PipelineEvent",
    "StreamEventTranslator",
    "VALUES_STREAM_EVENT",
    "attach_correlation_id_to_v1_payload",
]


#: ``StreamPart.event`` value that carries the ``AutobuildState`` snapshot.
#: Other event values (``"metadata"``, ``"messages"``, ``"updates"``,
#: ``"events"``, ``"end"``) are observable but not actionable at this
#: layer; the translator returns ``None`` for them after a DEBUG log.
VALUES_STREAM_EVENT: str = "values"


#: Union of typed ``pipeline.*`` payloads the translator may construct.
#: The set mirrors the eight publish methods on
#: :class:`forge.adapters.nats.PipelinePublisher`, minus
#: ``BuildProgressPayload`` (heartbeat publishes are owned by
#: :class:`forge.pipeline.PipelineLifecycleEmitter` directly, not by the
#: SSE translator).
PipelineEvent = (
    BuildStartedPayload
    | StageCompletePayload
    | BuildCompletePayload
    | BuildFailedPayload
    | BuildPausedPayload
    | BuildResumedPayload
    | BuildCancelledPayload
)


class MissingCorrelationIdError(ValueError):
    """Raised when :attr:`BuildContext.correlation_id` is missing or empty.

    AC-3: the translator MUST NOT emit an envelope without
    ``correlation_id`` — the F010C / DDR-029 contract requires every
    outbound ``pipeline.*`` envelope to thread the inbound id, and a
    silent ``None`` would corrupt the audit trail for the build.
    """


def attach_correlation_id_to_v1_payload(
    payload: object, correlation_id: str
) -> None:
    """Attach ``correlation_id`` to a Pydantic v1 payload post-construction.

    The v1 lifecycle payloads (``BuildStartedPayload``,
    ``BuildCompletePayload``, ``BuildFailedPayload``) declare
    ``model_config = ConfigDict(extra="ignore")`` and therefore silently
    drop a ``correlation_id`` kwarg passed to ``__init__``. The
    publisher's central envelope-construction reads the field via
    ``getattr(payload, "correlation_id", None)``, so we attach it via
    :func:`object.__setattr__` (bypassing pydantic's validating
    ``__setattr__``). Net effect: ``model_dump`` output is unchanged
    (still wire-compatible with v1 consumers) and the envelope's
    ``correlation_id`` field is populated.

    Mirrors :func:`forge.pipeline.attach_correlation_id` so the
    SSE-translation producer and the in-process emitter share one
    canonical attachment shape (no schema drift).

    Args:
        payload: A Pydantic v1 lifecycle payload instance.
        correlation_id: The originating correlation id to attach.
    """
    object.__setattr__(payload, "correlation_id", correlation_id)


# ---------------------------------------------------------------------------
# AutobuildState snapshot extraction
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Snapshot:
    """Subset of :class:`AutobuildState` fields the translator diffs against.

    The translator only needs the fields that participate in transition
    detection or payload construction. Keeping the projection narrow:

    * Insulates the translator from additive ``AutobuildState`` schema
      changes (extra fields are ignored at extraction time).
    * Makes the prior-state diff cheap to compute and to store in the
      ``_last_snapshot`` map.
    """

    feature_id: str
    build_id: str
    lifecycle: str
    wave_total: int
    wave_index: int
    task_index: int
    tasks_completed: int
    tasks_failed: int
    last_coach_score: float | None
    waiting_for: str | None
    #: Operator-readable async-failure metadata (TASK-FRR-PEB-011 AC-4).
    #: When the SSE stream reports a failed lifecycle, the runner forwards
    #: the originating exception's class name and message so the typed
    #: :class:`BuildFailedPayload` can carry a ``failure_reason`` of the
    #: form ``"{ExceptionClass}: {message}"``. Both fields are ``None``
    #: when the snapshot does not include exception metadata (e.g.
    #: pre-T3-runner builds, deterministic-failure paths).
    error_class: str | None
    error_message: str | None


def _extract_error_metadata(
    snap: Mapping[str, Any],
) -> tuple[str | None, str | None]:
    """Pull async-failure metadata out of an ``AutobuildState`` snapshot.

    Supports two shapes for forward-compatibility with the T3 runner's
    error-propagation contract:

    1. **Flat fields**: ``snap["error_class"]`` / ``snap["error_message"]``
       — the canonical shape. Used by the runner when the failure
       originates inside a deterministic step (Pydantic validation,
       schema check, etc.).
    2. **Nested ``last_error``**: ``snap["last_error"] = {"class": ...,
       "message": ...}`` (or ``"type"`` instead of ``"class"``) — the
       legacy shape carried by older runner builds. Accepted as a
       fallback so a mixed-version fleet does not lose the failure
       context across the SSE bridge.

    Returns ``(error_class, error_message)`` with either / both
    components ``None`` when the snapshot does not carry the field.
    The translator's :meth:`StreamEventTranslator._build_failed` formats
    a ``failure_reason`` of ``"{class}: {message}"`` when both are set,
    or falls back to the legacy ``"autobuild failed (sse)"`` string when
    neither is present (TASK-FRR-PEB-011 AC-4).
    """
    error_class = snap.get("error_class")
    error_message = snap.get("error_message")

    if error_class is None and error_message is None:
        last_error = snap.get("last_error")
        if isinstance(last_error, Mapping):
            error_class = last_error.get("class") or last_error.get("type")
            error_message = last_error.get("message")

    return (
        str(error_class) if error_class else None,
        str(error_message) if error_message else None,
    )


def _extract_state(data: Mapping[str, Any], feature_id: str) -> _Snapshot | None:
    """Pull the :class:`AutobuildState` snapshot for ``feature_id`` out of ``data``.

    The ``async_tasks`` channel reducer keys snapshots by ``feature_id``
    (see DDR-006); the translator therefore looks first in
    ``data["async_tasks"]`` and falls back to ``data`` itself for
    runners that emit a flat snapshot. Returns ``None`` when no
    AutobuildState is observable (e.g. metadata-only "values" events
    before the runner has written its first snapshot).
    """
    if not isinstance(data, Mapping):
        return None

    # Prefer the channel-shaped snapshot.
    async_tasks = data.get("async_tasks")
    snap: Mapping[str, Any] | None = None
    if isinstance(async_tasks, Mapping):
        candidate = async_tasks.get(feature_id)
        if isinstance(candidate, Mapping):
            snap = candidate
    if snap is None and "lifecycle" in data and "build_id" in data:
        # Flat snapshot fallback — used by some test fixtures.
        snap = data

    if snap is None:
        return None

    error_class, error_message = _extract_error_metadata(snap)

    try:
        return _Snapshot(
            feature_id=str(snap.get("feature_id", feature_id)),
            build_id=str(snap["build_id"]),
            lifecycle=str(snap["lifecycle"]),
            wave_total=int(snap.get("wave_total", 1) or 1),
            wave_index=int(snap.get("wave_index", 0) or 0),
            task_index=int(snap.get("task_index", 0) or 0),
            tasks_completed=int(snap.get("tasks_completed", 0) or 0),
            tasks_failed=int(snap.get("tasks_failed", 0) or 0),
            last_coach_score=(
                float(snap["last_coach_score"])
                if snap.get("last_coach_score") is not None
                else None
            ),
            waiting_for=(
                str(snap["waiting_for"])
                if snap.get("waiting_for") is not None
                else None
            ),
            error_class=error_class,
            error_message=error_message,
        )
    except (KeyError, TypeError, ValueError) as exc:
        logger.debug(
            "translation: malformed AutobuildState snapshot for feature_id=%s "
            "err=%s — returning None (no envelope emitted)",
            feature_id,
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# StreamEventTranslator
# ---------------------------------------------------------------------------


class StreamEventTranslator:
    """Translate ``StreamPart`` events into typed pipeline envelopes.

    The translator is stateful: it stores the previous
    :class:`_Snapshot` per ``feature_id`` so it can detect transitions
    between consecutive ``stream_mode="values"`` events. State is NOT
    persisted across process restarts — recovery on boot is owned by
    :meth:`forge.lifecycle_bridge.LifecycleBridge.recover_in_flight`,
    not by this translator.

    Concurrency: the translator is **not** thread-safe; one instance per
    SSE consumer task. Two builds on one translator are safe as long as
    they have distinct ``feature_id`` values.

    Args:
        clock: Optional callable returning a UTC :class:`datetime`. Tests
            pass a fake to make ``completed_at`` / ``cancelled_at`` /
            ``paused_at`` deterministic. Defaults to
            :meth:`datetime.now` with :data:`UTC`.
    """

    def __init__(
        self,
        *,
        clock: "datetime | None | type[None] | object" = None,
    ) -> None:
        # ``clock`` is intentionally typed loosely: tests pass a callable
        # that returns a datetime; production omits and we use the
        # default ``_default_clock``.
        self._clock = clock if callable(clock) else self._default_clock
        self._last_snapshot: dict[str, _Snapshot] = {}

    @staticmethod
    def _default_clock() -> datetime:
        return datetime.now(UTC)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def translate(
        self,
        stream_part: StreamPart,
        context: BuildContext,
    ) -> PipelineEvent | None:
        """Translate a single :class:`StreamPart` to a typed envelope payload.

        Returns ``None`` when the stream part does not correspond to a
        wire-emittable transition. Specifically:

        * Non-``"values"`` events return ``None`` (DEBUG-logged).
        * ``"values"`` events that do not advance the lifecycle or
          stage counters return ``None``.
        * Events whose ``data`` does not contain an ``AutobuildState``
          snapshot for ``context.run_id``/``feature_id`` return ``None``.

        Args:
            stream_part: The :class:`StreamPart` from
                ``client.runs.join_stream(...)``.
            context: The :class:`BuildContext` carrying the correlation
                id (and run/thread/feature ids for fixture-replay tests).

        Returns:
            A typed :class:`PipelineEvent` payload, or ``None`` when no
            envelope should be emitted.

        Raises:
            MissingCorrelationIdError: ``context.correlation_id`` is
                missing or empty (AC-3 — no silent fallback).
        """
        self._require_correlation_id(context)

        event_name = getattr(stream_part, "event", None)
        if event_name != VALUES_STREAM_EVENT:
            # FOLLOWUP-B trace: temporary INFO (was DEBUG) so the
            # forge-prod log captures whether non-values events are
            # arriving and being silently dropped. Revert to DEBUG on
            # AC-5 cleanup. (Original comment: AC-2 — DEBUG so a
            # langgraph-api minor bump that adds a new event type does
            # not flood WARNING.)
            logger.info(
                "translation: ignoring StreamPart event=%r (only %r is "
                "actioned by this translator)",
                event_name,
                VALUES_STREAM_EVENT,
            )
            return None

        feature_id = self._infer_feature_id(stream_part, context)
        if feature_id is None:
            logger.debug(
                "translation: cannot infer feature_id from StreamPart data "
                "for run_id=%s — returning None",
                context.run_id,
            )
            return None

        snap = _extract_state(stream_part.data or {}, feature_id)
        if snap is None:
            return None

        prev = self._last_snapshot.get(feature_id)
        # Update before dispatch so reentrancy on raise still leaves a
        # consistent prior-snapshot state. The dispatch is pure
        # construction — no I/O — so the update-then-dispatch ordering
        # is safe.
        self._last_snapshot[feature_id] = snap

        return self._dispatch(prev, snap, context)

    # ------------------------------------------------------------------
    # Internal: transition dispatch
    # ------------------------------------------------------------------

    def _dispatch(
        self,
        prev: _Snapshot | None,
        snap: _Snapshot,
        context: BuildContext,
    ) -> PipelineEvent | None:
        """Choose the typed payload for the (prev, snap) transition.

        Order of precedence (specific → general):

        1. Terminal lifecycles (``failed`` / ``cancelled`` / ``completed``)
           always win — once a terminal state is observed the build is
           done and no further transitions matter.
        2. ``awaiting_approval`` entry → :class:`BuildPausedPayload`.
        3. ``awaiting_approval → running_wave`` → :class:`BuildResumedPayload`.
        4. First ``running_wave`` observation → :class:`BuildStartedPayload`.
        5. ``tasks_completed`` or ``tasks_failed`` increased → one
           :class:`StageCompletePayload` per increment (in this snapshot,
           we emit a single payload representing the most recent stage
           — additional deltas in the same snapshot would require a list
           return; the contract is one envelope per StreamPart).
        """
        cid = context.correlation_id

        # 1) Terminal states.
        if snap.lifecycle == "failed" and (prev is None or prev.lifecycle != "failed"):
            return self._build_failed(snap, cid)
        # AC-2 (TASK-FRR-PEB-007): operator-cancel via the SDK surfaces
        # over SSE as a terminal lifecycle of either "cancelled" (the
        # canonical autobuild lifecycle) or "interrupted" (the
        # langgraph-runner label for an SDK
        # ``runs.cancel(action="interrupt")``). Both map to a single
        # ``BuildCancelledPayload`` so downstream consumers see exactly
        # one cancel envelope per operator-cancel — even if the runner
        # emits the lifecycle under either label.
        if snap.lifecycle in ("cancelled", "interrupted") and (
            prev is None or prev.lifecycle not in ("cancelled", "interrupted")
        ):
            return self._build_cancelled(snap, cid)
        if snap.lifecycle == "completed" and (
            prev is None or prev.lifecycle != "completed"
        ):
            return self._build_complete(snap, cid)

        # 2) Pause edge — entered awaiting_approval.
        if snap.lifecycle == "awaiting_approval" and (
            prev is None or prev.lifecycle != "awaiting_approval"
        ):
            return self._build_paused(snap, cid)

        # 3) Resume edge — leaving awaiting_approval back to running_wave.
        if (
            snap.lifecycle == "running_wave"
            and prev is not None
            and prev.lifecycle == "awaiting_approval"
        ):
            return self._build_resumed(snap, cid)

        # 4) First entry into running_wave (build start).
        if snap.lifecycle == "running_wave" and (
            prev is None or prev.lifecycle in {"starting", "planning_waves"}
        ):
            return self._build_started(snap, cid)

        # 5) Stage-complete delta — tasks_completed or tasks_failed grew
        #    while still inside running_wave.
        if (
            snap.lifecycle == "running_wave"
            and prev is not None
            and prev.lifecycle == "running_wave"
            and (
                snap.tasks_completed > prev.tasks_completed
                or snap.tasks_failed > prev.tasks_failed
            )
        ):
            return self._build_stage_complete(prev, snap, cid)

        # No emit-worthy transition.
        return None

    # ------------------------------------------------------------------
    # Payload constructors — one per typed envelope
    # ------------------------------------------------------------------

    def _build_started(self, snap: _Snapshot, correlation_id: str) -> BuildStartedPayload:
        payload = BuildStartedPayload(
            feature_id=snap.feature_id,
            build_id=snap.build_id,
            wave_total=max(snap.wave_total, 1),
        )
        attach_correlation_id_to_v1_payload(payload, correlation_id)
        return payload

    def _build_complete(self, snap: _Snapshot, correlation_id: str) -> BuildCompletePayload:
        total = snap.tasks_completed + snap.tasks_failed
        if total < 1:
            # BuildCompletePayload requires tasks_total >= 1 — synthesise
            # a 1-task build when the snapshot has no per-task counters
            # yet (e.g. zero-task autobuild — pathological but observable).
            total = 1
        payload = BuildCompletePayload(
            feature_id=snap.feature_id,
            build_id=snap.build_id,
            repo=None,
            branch=None,
            tasks_completed=snap.tasks_completed,
            tasks_failed=max(total - snap.tasks_completed, 0),
            tasks_total=total,
            pr_url=None,
            duration_seconds=0,
            summary="autobuild completed (sse)",
        )
        attach_correlation_id_to_v1_payload(payload, correlation_id)
        return payload

    def _build_failed(self, snap: _Snapshot, correlation_id: str) -> BuildFailedPayload:
        # TASK-FRR-PEB-011 AC-4: format ``failure_reason`` as
        # ``"{ExceptionClass}: {message}"`` when the runner forwards
        # async-failure metadata; fall back to a generic legacy string
        # for snapshots that do not carry it.
        if snap.error_class and snap.error_message:
            failure_reason = f"{snap.error_class}: {snap.error_message}"
        elif snap.error_class:
            failure_reason = snap.error_class
        elif snap.error_message:
            failure_reason = snap.error_message
        else:
            failure_reason = "autobuild failed (sse)"
        payload = BuildFailedPayload(
            feature_id=snap.feature_id,
            build_id=snap.build_id,
            failure_reason=failure_reason,
            recoverable=False,
            failed_task_id=None,
        )
        attach_correlation_id_to_v1_payload(payload, correlation_id)
        return payload

    def _build_cancelled(
        self, snap: _Snapshot, correlation_id: str
    ) -> BuildCancelledPayload:
        return BuildCancelledPayload(
            feature_id=snap.feature_id,
            build_id=snap.build_id,
            reason="autobuild cancelled (sse)",
            cancelled_by="lifecycle_bridge",
            cancelled_at=self._clock().isoformat(),
            correlation_id=correlation_id,
        )

    def _build_paused(self, snap: _Snapshot, correlation_id: str) -> BuildPausedPayload:
        stage = snap.waiting_for or "awaiting_approval"
        return BuildPausedPayload(
            feature_id=snap.feature_id,
            build_id=snap.build_id,
            stage_label=stage,
            gate_mode="MANDATORY_HUMAN_APPROVAL",
            coach_score=snap.last_coach_score,
            rationale=stage,
            approval_subject=f"agents.approval.forge.{snap.build_id}",
            paused_at=self._clock().isoformat(),
            correlation_id=correlation_id,
        )

    def _build_resumed(self, snap: _Snapshot, correlation_id: str) -> BuildResumedPayload:
        return BuildResumedPayload(
            feature_id=snap.feature_id,
            build_id=snap.build_id,
            stage_label="awaiting_approval",
            decision="approve",
            responder="lifecycle_bridge",
            resumed_at=self._clock().isoformat(),
            correlation_id=correlation_id,
        )

    def _build_stage_complete(
        self,
        prev: _Snapshot,
        snap: _Snapshot,
        correlation_id: str,
    ) -> StageCompletePayload:
        # FAILED status when tasks_failed advanced; PASSED otherwise.
        status: str = "FAILED" if snap.tasks_failed > prev.tasks_failed else "PASSED"
        return StageCompletePayload(
            feature_id=snap.feature_id,
            build_id=snap.build_id,
            stage_label=f"task-{snap.task_index}",
            target_kind="subagent",
            target_identifier="autobuild_runner",
            status=status,  # type: ignore[arg-type]
            gate_mode=None,
            coach_score=snap.last_coach_score,
            duration_secs=0.0,
            completed_at=self._clock().isoformat(),
            correlation_id=correlation_id,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _require_correlation_id(context: BuildContext) -> None:
        cid = getattr(context, "correlation_id", None)
        if not isinstance(cid, str) or not cid:
            raise MissingCorrelationIdError(
                "StreamEventTranslator.translate: BuildContext.correlation_id "
                "must be a non-empty string (AC-3 — no silent fallback). "
                f"Got correlation_id={cid!r}."
            )

    @staticmethod
    def _infer_feature_id(
        stream_part: StreamPart, context: BuildContext
    ) -> str | None:
        """Pull a feature_id from the stream data, falling back to the context.

        :class:`BuildContext` carries ``feature_id`` directly, so the
        common path is "use the context". The fallback to data is for
        contract-test fixtures that pre-date the context plumbing.
        """
        ctx_feature = getattr(context, "feature_id", None)
        if isinstance(ctx_feature, str) and ctx_feature:
            return ctx_feature
        data = stream_part.data
        if isinstance(data, Mapping):
            async_tasks = data.get("async_tasks")
            if isinstance(async_tasks, Mapping) and async_tasks:
                # First key wins — the runner pins the feature_id per build.
                first = next(iter(async_tasks.keys()), None)
                if isinstance(first, str):
                    return first
            flat_feature = data.get("feature_id")
            if isinstance(flat_feature, str) and flat_feature:
                return flat_feature
        return None

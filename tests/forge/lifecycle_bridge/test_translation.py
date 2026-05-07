"""Unit tests for ``forge.lifecycle_bridge.translation`` (TASK-FRR-PEB-003).

Acceptance-criteria coverage map:

* AC-1: :class:`StreamEventTranslator.translate` exists with the
  documented signature — :class:`TestTranslatorSurface`.
* AC-2: every documented :attr:`StreamPart.event` value is handled;
  unknown events return ``None`` and DEBUG-log (no WARNING) —
  :class:`TestUnknownEventBehaviour`.
* AC-3: :class:`MissingCorrelationIdError` is raised on missing
  correlation id (no fallback) — :class:`TestCorrelationIdRequired`.
* AC-4 partial: lifecycle transitions map to the correct typed
  envelope — :class:`TestLifecycleTransitions`.
"""

from __future__ import annotations

import inspect
import logging
from datetime import UTC, datetime, timedelta

import pytest
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
from forge.lifecycle_bridge.translation import (
    MissingCorrelationIdError,
    PipelineEvent,
    StreamEventTranslator,
    VALUES_STREAM_EVENT,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context(
    *,
    feature_id: str = "FEAT-XLAT-001",
    correlation_id: str = "corr-xlat-001",
) -> BuildContext:
    return BuildContext(
        feature_id=feature_id,
        thread_id="thread-xlat",
        run_id="run-xlat",
        correlation_id=correlation_id,
        deadline_at=datetime.now(UTC) + timedelta(seconds=300),
    )


def _state_part(
    feature_id: str,
    *,
    lifecycle: str,
    build_id: str = "build-FEAT-XLAT-001-20260507120000",
    wave_total: int = 1,
    wave_index: int = 0,
    task_index: int = 0,
    tasks_completed: int = 0,
    tasks_failed: int = 0,
    waiting_for: str | None = None,
    last_coach_score: float | None = None,
) -> StreamPart:
    return StreamPart(
        event=VALUES_STREAM_EVENT,
        data={
            "async_tasks": {
                feature_id: {
                    "feature_id": feature_id,
                    "build_id": build_id,
                    "lifecycle": lifecycle,
                    "wave_total": wave_total,
                    "wave_index": wave_index,
                    "task_index": task_index,
                    "tasks_completed": tasks_completed,
                    "tasks_failed": tasks_failed,
                    "waiting_for": waiting_for,
                    "last_coach_score": last_coach_score,
                }
            }
        },
        id=None,
    )


# ---------------------------------------------------------------------------
# AC-1: surface contract
# ---------------------------------------------------------------------------


class TestTranslatorSurface:
    """``StreamEventTranslator.translate(stream_part, context)`` exists."""

    def test_class_is_importable(self) -> None:
        assert StreamEventTranslator is not None

    def test_translate_signature(self) -> None:
        translator = StreamEventTranslator()
        sig = inspect.signature(translator.translate)
        params = list(sig.parameters.keys())
        assert params[:2] == ["stream_part", "context"]


# ---------------------------------------------------------------------------
# AC-2: unknown events return None and log at DEBUG (not WARNING)
# ---------------------------------------------------------------------------


class TestUnknownEventBehaviour:
    """Unknown ``StreamPart.event`` values are routine during langgraph-api
    minor bumps; the translator returns ``None`` and logs at DEBUG.
    """

    def test_unknown_event_returns_none(self) -> None:
        translator = StreamEventTranslator()
        part = StreamPart(event="metadata", data={"thread_id": "t-1"}, id="evt-1")
        assert translator.translate(part, _make_context()) is None

    def test_unknown_event_logs_at_debug_not_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        translator = StreamEventTranslator()
        part = StreamPart(event="messages", data={}, id=None)
        with caplog.at_level(logging.DEBUG, logger="forge.lifecycle_bridge.translation"):
            translator.translate(part, _make_context())
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings == [], (
            "unknown StreamPart events MUST NOT emit WARNING-level logs "
            "(AC-2 — minor langgraph-api bumps would otherwise spam the "
            f"daemon's log with noise). Got: {warnings!r}"
        )

    def test_unknown_event_does_not_raise(self) -> None:
        translator = StreamEventTranslator()
        for evt in ("metadata", "messages", "updates", "events", "end", "custom"):
            part = StreamPart(event=evt, data={}, id=None)
            translator.translate(part, _make_context())  # MUST NOT raise.


# ---------------------------------------------------------------------------
# AC-3: missing correlation_id raises MissingCorrelationIdError
# ---------------------------------------------------------------------------


class TestCorrelationIdRequired:
    """``BuildContext.correlation_id`` is required; missing raises."""

    def test_empty_correlation_id_raises(self) -> None:
        translator = StreamEventTranslator()
        ctx = _make_context(correlation_id="")
        part = _state_part("FEAT-XLAT-001", lifecycle="running_wave")
        with pytest.raises(MissingCorrelationIdError):
            translator.translate(part, ctx)

    def test_missing_correlation_id_message_mentions_field(self) -> None:
        translator = StreamEventTranslator()
        ctx = _make_context(correlation_id="")
        part = _state_part("FEAT-XLAT-001", lifecycle="running_wave")
        with pytest.raises(MissingCorrelationIdError) as excinfo:
            translator.translate(part, ctx)
        assert "correlation_id" in str(excinfo.value)


# ---------------------------------------------------------------------------
# AC-4 partial: lifecycle transitions map to typed envelopes
# ---------------------------------------------------------------------------


class TestLifecycleTransitions:
    """Each documented lifecycle transition produces the correct typed payload."""

    def test_first_running_wave_emits_build_started(self) -> None:
        translator = StreamEventTranslator()
        ctx = _make_context()
        # planning_waves first observation → no envelope yet.
        translator.translate(
            _state_part("FEAT-XLAT-001", lifecycle="planning_waves"), ctx
        )
        out = translator.translate(
            _state_part("FEAT-XLAT-001", lifecycle="running_wave", wave_total=3), ctx
        )
        assert isinstance(out, BuildStartedPayload)
        assert out.feature_id == "FEAT-XLAT-001"
        assert out.wave_total == 3
        assert getattr(out, "correlation_id", None) == ctx.correlation_id

    def test_completed_emits_build_complete(self) -> None:
        translator = StreamEventTranslator()
        ctx = _make_context()
        translator.translate(
            _state_part(
                "FEAT-XLAT-001",
                lifecycle="running_wave",
                tasks_completed=2,
            ),
            ctx,
        )
        out = translator.translate(
            _state_part(
                "FEAT-XLAT-001",
                lifecycle="completed",
                tasks_completed=2,
                tasks_failed=0,
            ),
            ctx,
        )
        assert isinstance(out, BuildCompletePayload)
        assert getattr(out, "correlation_id", None) == ctx.correlation_id

    def test_failed_emits_build_failed(self) -> None:
        translator = StreamEventTranslator()
        ctx = _make_context()
        out = translator.translate(
            _state_part("FEAT-XLAT-001", lifecycle="failed"), ctx
        )
        assert isinstance(out, BuildFailedPayload)
        assert getattr(out, "correlation_id", None) == ctx.correlation_id

    def test_cancelled_emits_build_cancelled(self) -> None:
        translator = StreamEventTranslator()
        ctx = _make_context()
        out = translator.translate(
            _state_part("FEAT-XLAT-001", lifecycle="cancelled"), ctx
        )
        assert isinstance(out, BuildCancelledPayload)
        assert out.correlation_id == ctx.correlation_id

    def test_awaiting_approval_emits_build_paused(self) -> None:
        translator = StreamEventTranslator()
        ctx = _make_context()
        translator.translate(
            _state_part("FEAT-XLAT-001", lifecycle="running_wave"), ctx
        )
        out = translator.translate(
            _state_part(
                "FEAT-XLAT-001",
                lifecycle="awaiting_approval",
                waiting_for="approval:Architecture Review",
                last_coach_score=0.74,
            ),
            ctx,
        )
        assert isinstance(out, BuildPausedPayload)
        assert out.stage_label == "approval:Architecture Review"
        assert out.coach_score == 0.74
        assert out.correlation_id == ctx.correlation_id

    def test_awaiting_approval_to_running_wave_emits_resumed(self) -> None:
        translator = StreamEventTranslator()
        ctx = _make_context()
        translator.translate(
            _state_part("FEAT-XLAT-001", lifecycle="running_wave"), ctx
        )
        translator.translate(
            _state_part("FEAT-XLAT-001", lifecycle="awaiting_approval"), ctx
        )
        out = translator.translate(
            _state_part("FEAT-XLAT-001", lifecycle="running_wave"), ctx
        )
        assert isinstance(out, BuildResumedPayload)
        assert out.correlation_id == ctx.correlation_id

    def test_stage_completion_within_running_wave_emits_stage_complete(self) -> None:
        translator = StreamEventTranslator()
        ctx = _make_context()
        translator.translate(
            _state_part(
                "FEAT-XLAT-001",
                lifecycle="running_wave",
                tasks_completed=0,
            ),
            ctx,
        )
        out = translator.translate(
            _state_part(
                "FEAT-XLAT-001",
                lifecycle="running_wave",
                tasks_completed=1,
                task_index=1,
                last_coach_score=0.92,
            ),
            ctx,
        )
        assert isinstance(out, StageCompletePayload)
        assert out.status == "PASSED"
        assert out.coach_score == 0.92
        assert out.correlation_id == ctx.correlation_id

    def test_stage_failure_within_running_wave_emits_failed_status(self) -> None:
        translator = StreamEventTranslator()
        ctx = _make_context()
        translator.translate(
            _state_part(
                "FEAT-XLAT-001",
                lifecycle="running_wave",
                tasks_completed=0,
                tasks_failed=0,
            ),
            ctx,
        )
        out = translator.translate(
            _state_part(
                "FEAT-XLAT-001",
                lifecycle="running_wave",
                tasks_completed=0,
                tasks_failed=1,
            ),
            ctx,
        )
        assert isinstance(out, StageCompletePayload)
        assert out.status == "FAILED"

    def test_no_change_returns_none(self) -> None:
        translator = StreamEventTranslator()
        ctx = _make_context()
        translator.translate(
            _state_part("FEAT-XLAT-001", lifecycle="running_wave"), ctx
        )
        out = translator.translate(
            _state_part("FEAT-XLAT-001", lifecycle="running_wave"), ctx
        )
        assert out is None


# ---------------------------------------------------------------------------
# Property: every StreamPart produces ≤ 1 envelope (no double-emits)
# ---------------------------------------------------------------------------


class TestNoDoubleEmits:
    """Per AC test requirement: every StreamPart produces exactly one
    envelope or ``None`` — never a list, never two events for one part.
    """

    def test_translate_returns_payload_or_none(self) -> None:
        translator = StreamEventTranslator()
        ctx = _make_context()
        sequence = [
            _state_part("FEAT-XLAT-001", lifecycle="starting"),
            _state_part("FEAT-XLAT-001", lifecycle="planning_waves"),
            _state_part("FEAT-XLAT-001", lifecycle="running_wave"),
            _state_part(
                "FEAT-XLAT-001",
                lifecycle="running_wave",
                tasks_completed=1,
            ),
            _state_part(
                "FEAT-XLAT-001",
                lifecycle="completed",
                tasks_completed=1,
            ),
        ]
        outputs: list[PipelineEvent | None] = [
            translator.translate(part, ctx) for part in sequence
        ]
        # Each output is either None or a single PipelineEvent — never a list.
        for out in outputs:
            assert out is None or hasattr(out, "model_dump"), (
                f"translate() returned non-payload {type(out).__name__}"
            )

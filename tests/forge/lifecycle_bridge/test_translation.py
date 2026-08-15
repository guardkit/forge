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
# TASK-FRR-PEB-011 AC-4: operator-readable failure_reason format
# ---------------------------------------------------------------------------


class TestFailureReasonFormat:
    """AC-4 (TASK-FRR-PEB-011): ``failure_reason = "{ExceptionClass}: {message}"``.

    When the SSE stream's failed-lifecycle snapshot carries
    ``error_class`` + ``error_message`` (T3 runner forwards the
    originating exception), the translator's :class:`BuildFailedPayload`
    must surface it in the form ``RuntimeError: model output failed
    Pydantic validation``.
    """

    @staticmethod
    def _failed_part_with_error(
        feature_id: str,
        *,
        error_class: str | None,
        error_message: str | None,
    ) -> StreamPart:
        snap: dict[str, object] = {
            "feature_id": feature_id,
            "build_id": f"build-{feature_id}-x",
            "lifecycle": "failed",
            "wave_total": 1,
            "wave_index": 0,
            "task_index": 0,
            "tasks_completed": 0,
            "tasks_failed": 1,
            "waiting_for": None,
            "last_coach_score": None,
        }
        if error_class is not None:
            snap["error_class"] = error_class
        if error_message is not None:
            snap["error_message"] = error_message
        return StreamPart(
            event=VALUES_STREAM_EVENT,
            data={"async_tasks": {feature_id: snap}},
            id=None,
        )

    def test_failure_reason_is_class_colon_message_when_metadata_present(
        self,
    ) -> None:
        translator = StreamEventTranslator()
        ctx = _make_context()
        # Prime with a running_wave snapshot so the translator has prior state.
        translator.translate(
            _state_part(ctx.feature_id, lifecycle="running_wave"), ctx
        )
        out = translator.translate(
            self._failed_part_with_error(
                ctx.feature_id,
                error_class="RuntimeError",
                error_message="model output failed Pydantic validation",
            ),
            ctx,
        )
        assert isinstance(out, BuildFailedPayload)
        assert (
            out.failure_reason
            == "RuntimeError: model output failed Pydantic validation"
        )

    def test_failure_reason_falls_back_when_metadata_absent(self) -> None:
        translator = StreamEventTranslator()
        ctx = _make_context()
        translator.translate(
            _state_part(ctx.feature_id, lifecycle="running_wave"), ctx
        )
        out = translator.translate(
            self._failed_part_with_error(
                ctx.feature_id,
                error_class=None,
                error_message=None,
            ),
            ctx,
        )
        assert isinstance(out, BuildFailedPayload)
        assert out.failure_reason == "autobuild failed (sse)"


class TestRunnerFailedSnapshotRidesTheWire:
    """07-30 coach finding 5 — pin the runner↔translator failure contract.

    ``autobuild_runner._build_failed_snapshot``'s docstring long CLAIMED the
    reason "ends up on the wire via the pipeline.build-failed envelope", but
    the snapshot carried no error metadata, so every runner failure surfaced
    as the generic ``"autobuild failed (sse)"``. These tests drive the
    RUNNER'S OWN snapshot (not a hand-built fixture) through the real
    translator, so any future drift in either module's shape breaks here.
    """

    @staticmethod
    def _translate_runner_failure(
        *,
        reason: str,
        budget_cap_killed: bool = False,
        terminal_class: str | None = None,
    ) -> BuildFailedPayload:
        from forge.subagents import autobuild_runner as ar

        feature_id = "FEAT-XLAT-001"
        snap = ar._build_failed_snapshot(
            {
                "feature_id": feature_id,
                "build_id": "build-FEAT-XLAT-001-wire",
                "correlation_id": "corr-xlat-001",
            },
            reason=reason,
            budget_cap_killed=budget_cap_killed,
            terminal_class=terminal_class,
        )
        translator = StreamEventTranslator()
        ctx = _make_context()
        translator.translate(
            _state_part(ctx.feature_id, lifecycle="running_wave"), ctx
        )
        out = translator.translate(
            StreamPart(
                event=VALUES_STREAM_EVENT,
                data={"async_tasks": {feature_id: snap}},
                id=None,
            ),
            ctx,
        )
        assert isinstance(out, BuildFailedPayload)
        return out

    def test_runner_failure_reason_rides_pipeline_build_failed(self) -> None:
        reason = "guardkit autobuild exit=1 (worktree KEPT for forensics: /tmp/wt)"
        out = self._translate_runner_failure(reason=reason)
        assert out.failure_reason == reason, (
            "the runner's reason must ride the wire verbatim — the generic "
            "'autobuild failed (sse)' fallback means the flat error_message "
            "contract broke"
        )

    def test_cap_kill_marker_threads_onto_the_typed_payload(self) -> None:
        reason = (
            "guardkit autobuild exceeded the budget wall-clock cap of 60.0s "
            "(profile='unattended') — killed (UBS-002)"
        )
        out = self._translate_runner_failure(reason=reason, budget_cap_killed=True)
        assert getattr(out, "budget_cap_killed", False) is True
        assert out.failure_reason == reason
        # The marker is attachment-only (correlation_id shape): the wire
        # bytes of the v1 payload stay unchanged.
        assert "budget_cap_killed" not in out.model_dump()

    def test_plain_failure_carries_no_cap_kill_marker(self) -> None:
        out = self._translate_runner_failure(reason="guardkit autobuild exit=2")
        assert getattr(out, "budget_cap_killed", False) is False

    def test_terminal_class_threads_onto_the_typed_payload(self) -> None:
        """Timeout truth: WHICH death this was reaches the wireup's seam."""
        out = self._translate_runner_failure(
            reason="guardkit autobuild exit=2",
            terminal_class="timeout-in-band",
        )
        assert getattr(out, "terminal_class", None) == "timeout-in-band"
        assert out.failure_reason == "guardkit autobuild exit=2", (
            "the class rides BESIDE the reason, never rewrites it"
        )

    def test_the_class_never_changes_the_wire_bytes(self) -> None:
        """The whole additive claim, proven: identical ``model_dump``.

        Attachment-only, exactly the ``correlation_id`` / ``budget_cap_killed``
        shape. A classified failure and an unclassified one serialise to the
        same v1 bytes, so no downstream consumer of
        ``pipeline.build-failed`` sees anything new unless it asks.
        """
        reason = "guardkit autobuild exit=2"
        classified = self._translate_runner_failure(
            reason=reason, terminal_class="timeout-wedge"
        )
        plain = self._translate_runner_failure(reason=reason)
        assert "terminal_class" not in classified.model_dump()
        assert classified.model_dump() == plain.model_dump()

    def test_a_plain_failure_carries_no_class(self) -> None:
        out = self._translate_runner_failure(reason="guardkit autobuild exit=2")
        assert getattr(out, "terminal_class", None) is None

    def test_the_error_class_is_never_stamped(self) -> None:
        """``error`` is the absent-by-default value — even asked for by name."""
        out = self._translate_runner_failure(
            reason="guardkit autobuild exit=1", terminal_class="error"
        )
        assert getattr(out, "terminal_class", None) is None


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


# ---------------------------------------------------------------------------
# The build terminal names the branch that holds the code (jarvis's ask)
#
# ``BuildCompletePayload.branch`` used to publish as ``None``, so the owner was
# told a build finished without being told WHERE the built code is. The runner's
# own convention — stated verbatim in ``forge.subagents.autobuild_runner``'s
# prior-build sweep ("the ref is named after the FEATURE, not any task id") — is
# ``autobuild/<FEATURE_ID>``, and it is derivable from the snapshot the
# translator already holds. ``repo`` STAYS None: neither ``AutobuildState`` nor
# ``BuildContext`` carries a repo name, and an invented one is worse than none.
# ---------------------------------------------------------------------------


class TestBuildCompleteNamesTheBranch:
    def test_branch_is_the_feature_autobuild_ref(self) -> None:
        translator = StreamEventTranslator()
        ctx = _make_context()
        translator.translate(
            _state_part("FEAT-XLAT-001", lifecycle="running_wave", tasks_completed=2),
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
        assert out.branch == "autobuild/FEAT-XLAT-001"

    def test_branch_follows_the_snapshot_feature_id_not_the_context(self) -> None:
        """The ref is named after the FEATURE THE SNAPSHOT REPORTS. The channel
        is keyed by the context's id, but the snapshot's own ``feature_id`` is
        what the payload (and hence the branch) carries — the same precedence
        ``_extract_state`` already applies to ``feature_id`` itself."""
        translator = StreamEventTranslator()
        ctx = _make_context(feature_id="FEAT-KEY-001")

        def _part(lifecycle: str, tasks_completed: int) -> StreamPart:
            part = _state_part(
                "FEAT-KEY-001",
                lifecycle=lifecycle,
                tasks_completed=tasks_completed,
            )
            part.data["async_tasks"]["FEAT-KEY-001"]["feature_id"] = "FEAT-SNAP-002"
            return part

        translator.translate(_part("running_wave", 1), ctx)
        out = translator.translate(_part("completed", 1), ctx)
        assert isinstance(out, BuildCompletePayload)
        assert out.feature_id == "FEAT-SNAP-002"
        assert out.branch == "autobuild/FEAT-SNAP-002"

    def test_repo_stays_none_because_nothing_on_the_wire_carries_it(self) -> None:
        """Honest absence: no repo name is invented. Pinned as a contract so a
        later fill has to come from a real source (a snapshot/context field),
        not from a guess."""
        from forge.lifecycle_bridge.bridge import BuildContext as _Ctx
        from forge.subagents.autobuild_runner import AutobuildState

        assert "repo" not in AutobuildState.model_fields
        assert "repo" not in _Ctx.__dataclass_fields__

        translator = StreamEventTranslator()
        ctx = _make_context()
        translator.translate(
            _state_part("FEAT-XLAT-001", lifecycle="running_wave", tasks_completed=1),
            ctx,
        )
        out = translator.translate(
            _state_part(
                "FEAT-XLAT-001",
                lifecycle="completed",
                tasks_completed=1,
                tasks_failed=0,
            ),
            ctx,
        )
        assert isinstance(out, BuildCompletePayload)
        assert out.repo is None

    def test_branch_rides_the_v1_wire_bytes(self) -> None:
        """``branch`` is a declared v1 field, so it must appear in the dumped
        envelope the wireup publishes — not merely on the object."""
        translator = StreamEventTranslator()
        ctx = _make_context()
        translator.translate(
            _state_part("FEAT-XLAT-001", lifecycle="running_wave", tasks_completed=1),
            ctx,
        )
        out = translator.translate(
            _state_part(
                "FEAT-XLAT-001",
                lifecycle="completed",
                tasks_completed=1,
                tasks_failed=0,
            ),
            ctx,
        )
        assert isinstance(out, BuildCompletePayload)
        assert out.model_dump()["branch"] == "autobuild/FEAT-XLAT-001"

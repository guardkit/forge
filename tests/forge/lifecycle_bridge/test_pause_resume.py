"""Pause/resume canonicalisation tests for the lifecycle bridge translator.

TASK-FRR-PEB-006 — bridge owns both pause and resume emit sites.
================================================================

The lifecycle bridge's :class:`StreamEventTranslator` is the **canonical**
producer of ``pipeline.build-paused`` / ``pipeline.build-resumed``
envelopes when a bridge is wired into ``forge serve``. FW10-010's
:class:`forge.adapters.nats.approval_subscriber.ApprovalSubscriber`
amends out its own resume emit when the bridge reports an active
registry entry — see ``tests/forge/test_pause_resume_publish.py`` for
the subscriber-side tests.

This module covers the bridge-side acceptance criteria:

* AC-1 (pause edge):
  ``running_wave → awaiting_approval`` → :class:`BuildPausedPayload`
  threaded with the inbound correlation_id.
* AC-1 (resume edge):
  ``awaiting_approval → running_wave`` → :class:`BuildResumedPayload`
  threaded with the inbound correlation_id.
* AC-4 (exactly one envelope per transition): repeated
  ``awaiting_approval`` snapshots produce **one** paused envelope; only
  the transition edge fires. Same invariant for resume.

The translator itself is stateful per-feature (it diffs against the
prior ``AutobuildState`` snapshot), so the tests drive it with two
``StreamPart`` events back-to-back to exercise the transition edges.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from langgraph_sdk.schema import StreamPart
from nats_core.events import (
    BuildPausedPayload,
    BuildResumedPayload,
)

from forge.lifecycle_bridge.bridge import BuildContext
from forge.lifecycle_bridge.translation import (
    StreamEventTranslator,
    VALUES_STREAM_EVENT,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FEATURE_ID = "FEAT-PEBR-006"
_BUILD_ID = "build-FEAT-PEBR-006-20260507120000"
_CORRELATION_ID = "corr-pebr-006"


def _make_context(
    *,
    feature_id: str = _FEATURE_ID,
    correlation_id: str = _CORRELATION_ID,
) -> BuildContext:
    """Construct the bridge :class:`BuildContext` used across tests."""
    return BuildContext(
        feature_id=feature_id,
        thread_id="thread-pebr-006",
        run_id="run-pebr-006",
        correlation_id=correlation_id,
        deadline_at=datetime.now(UTC) + timedelta(seconds=300),
    )


def _state_part(
    *,
    lifecycle: str,
    feature_id: str = _FEATURE_ID,
    build_id: str = _BUILD_ID,
    waiting_for: str | None = None,
    last_coach_score: float | None = None,
) -> StreamPart:
    """Build a ``stream_mode='values'`` :class:`StreamPart` snapshot.

    The translator extracts ``AutobuildState`` from
    ``data['async_tasks'][feature_id]``; this helper mirrors the runner
    shape so tests stay close to the wire contract.
    """
    return StreamPart(
        event=VALUES_STREAM_EVENT,
        data={
            "async_tasks": {
                feature_id: {
                    "feature_id": feature_id,
                    "build_id": build_id,
                    "lifecycle": lifecycle,
                    "wave_total": 1,
                    "wave_index": 0,
                    "task_index": 0,
                    "tasks_completed": 0,
                    "tasks_failed": 0,
                    "waiting_for": waiting_for,
                    "last_coach_score": last_coach_score,
                }
            }
        },
        id=None,
    )


# ---------------------------------------------------------------------------
# AC-1 (pause edge): awaiting_approval → BuildPausedPayload
# ---------------------------------------------------------------------------


class TestPauseEdgeProducesBuildPausedPayload:
    """``running_wave → awaiting_approval`` produces exactly one paused payload."""

    def test_pause_edge_returns_build_paused_payload(self) -> None:
        """The transition edge yields a typed :class:`BuildPausedPayload`."""
        translator = StreamEventTranslator()
        ctx = _make_context()

        # Prime translator with a non-pause snapshot so the edge fires
        # on the next part.
        translator.translate(_state_part(lifecycle="running_wave"), ctx)

        payload = translator.translate(
            _state_part(
                lifecycle="awaiting_approval",
                waiting_for="approval:Architecture Review",
                last_coach_score=0.91,
            ),
            ctx,
        )

        assert isinstance(payload, BuildPausedPayload), (
            "TASK-FRR-PEB-006 AC-1: awaiting_approval edge MUST map to "
            f"BuildPausedPayload, got {type(payload).__name__}"
        )

    def test_pause_payload_carries_inbound_correlation_id(self) -> None:
        """AC-4: correlation-id is threaded onto the paused envelope."""
        translator = StreamEventTranslator()
        ctx = _make_context(correlation_id="corr-from-build-queued")

        translator.translate(_state_part(lifecycle="running_wave"), ctx)
        payload = translator.translate(
            _state_part(
                lifecycle="awaiting_approval",
                waiting_for="approval:Architecture Review",
            ),
            ctx,
        )

        assert isinstance(payload, BuildPausedPayload)
        assert payload.correlation_id == "corr-from-build-queued", (
            "TASK-FRR-PEB-006 AC-4: BuildPausedPayload MUST carry the "
            f"inbound correlation_id, got {payload.correlation_id!r}"
        )

    def test_pause_payload_uses_waiting_for_as_stage_label(self) -> None:
        """The ``waiting_for`` field on the snapshot becomes the stage label."""
        translator = StreamEventTranslator()
        ctx = _make_context()

        translator.translate(_state_part(lifecycle="running_wave"), ctx)
        payload = translator.translate(
            _state_part(
                lifecycle="awaiting_approval",
                waiting_for="approval:Implementation Review",
            ),
            ctx,
        )

        assert isinstance(payload, BuildPausedPayload)
        assert payload.stage_label == "approval:Implementation Review"

    def test_pause_only_fires_once_per_transition(self) -> None:
        """AC-4 / @edge-case: re-emitting the same lifecycle yields no envelope.

        ``stream_mode='values'`` carries full snapshots; the runner can
        publish the same ``awaiting_approval`` snapshot more than once
        between heartbeats. The translator must emit a paused envelope
        only on the transition edge — subsequent identical snapshots
        return ``None``.
        """
        translator = StreamEventTranslator()
        ctx = _make_context()

        translator.translate(_state_part(lifecycle="running_wave"), ctx)
        first = translator.translate(
            _state_part(
                lifecycle="awaiting_approval",
                waiting_for="approval:Architecture Review",
            ),
            ctx,
        )
        second = translator.translate(
            _state_part(
                lifecycle="awaiting_approval",
                waiting_for="approval:Architecture Review",
            ),
            ctx,
        )

        assert isinstance(first, BuildPausedPayload), (
            "first awaiting_approval observation MUST emit BuildPausedPayload"
        )
        assert second is None, (
            "TASK-FRR-PEB-006 AC-4: a repeated awaiting_approval snapshot "
            f"MUST NOT re-emit, got {second!r}"
        )


# ---------------------------------------------------------------------------
# AC-1 (resume edge): awaiting_approval → running_wave → BuildResumedPayload
# ---------------------------------------------------------------------------


class TestResumeEdgeProducesBuildResumedPayload:
    """``awaiting_approval → running_wave`` → exactly one resumed payload."""

    def test_resume_edge_returns_build_resumed_payload(self) -> None:
        """The transition edge yields a typed :class:`BuildResumedPayload`."""
        translator = StreamEventTranslator()
        ctx = _make_context()

        # Prime translator with the awaiting_approval snapshot so the
        # next part triggers the resume edge.
        translator.translate(
            _state_part(
                lifecycle="awaiting_approval",
                waiting_for="approval:Architecture Review",
            ),
            ctx,
        )

        payload = translator.translate(
            _state_part(lifecycle="running_wave"),
            ctx,
        )

        assert isinstance(payload, BuildResumedPayload), (
            "TASK-FRR-PEB-006 AC-1: awaiting_approval → running_wave "
            "edge MUST map to BuildResumedPayload, got "
            f"{type(payload).__name__}"
        )

    def test_resume_payload_carries_inbound_correlation_id(self) -> None:
        """AC-4: the resumed envelope threads the build's correlation_id."""
        translator = StreamEventTranslator()
        ctx = _make_context(correlation_id="corr-from-build-queued")

        translator.translate(
            _state_part(
                lifecycle="awaiting_approval",
                waiting_for="approval:Architecture Review",
            ),
            ctx,
        )
        payload = translator.translate(
            _state_part(lifecycle="running_wave"),
            ctx,
        )

        assert isinstance(payload, BuildResumedPayload)
        assert payload.correlation_id == "corr-from-build-queued", (
            "TASK-FRR-PEB-006 AC-4: BuildResumedPayload MUST carry the "
            f"inbound correlation_id, got {payload.correlation_id!r}"
        )

    def test_resume_only_fires_once_per_transition(self) -> None:
        """AC-4: a steady-state running_wave snapshot does not re-emit."""
        translator = StreamEventTranslator()
        ctx = _make_context()

        translator.translate(
            _state_part(
                lifecycle="awaiting_approval",
                waiting_for="approval:Architecture Review",
            ),
            ctx,
        )
        first = translator.translate(
            _state_part(lifecycle="running_wave"),
            ctx,
        )
        second = translator.translate(
            _state_part(lifecycle="running_wave"),
            ctx,
        )

        assert isinstance(first, BuildResumedPayload), (
            "first running_wave-after-awaiting_approval MUST emit resume"
        )
        # The second snapshot is steady-state running_wave; no transition
        # edge → no envelope. Some lifecycle paths may emit a stage
        # delta, but with task counters unchanged it returns None.
        assert second is None, (
            "TASK-FRR-PEB-006 AC-4: a repeated running_wave snapshot "
            f"with no counter delta MUST NOT re-emit resumed, got {second!r}"
        )

    def test_resume_payload_stage_label_is_awaiting_approval(self) -> None:
        """The resumed payload's stage_label reflects the gate that paused."""
        translator = StreamEventTranslator()
        ctx = _make_context()

        translator.translate(
            _state_part(
                lifecycle="awaiting_approval",
                waiting_for="approval:Architecture Review",
            ),
            ctx,
        )
        payload = translator.translate(
            _state_part(lifecycle="running_wave"),
            ctx,
        )

        assert isinstance(payload, BuildResumedPayload)
        assert payload.stage_label == "awaiting_approval", (
            "TASK-FRR-PEB-006: the resumed envelope MUST identify the "
            "gate the build was paused at as the stage_label."
        )


# ---------------------------------------------------------------------------
# AC-4 cross-cutting: full pause/resume sequence threads correlation_id
# ---------------------------------------------------------------------------


class TestPauseResumeSequenceThreadsCorrelationId:
    """End-to-end: a single pause/resume round-trip emits exactly two envelopes."""

    def test_full_sequence_emits_paused_then_resumed_with_correlation(
        self,
    ) -> None:
        """One pause + one resume envelope, both tagged with the correlation_id."""
        translator = StreamEventTranslator()
        ctx = _make_context(correlation_id="corr-pause-resume-001")

        events: list[object] = []
        snapshots = [
            _state_part(lifecycle="running_wave"),
            _state_part(
                lifecycle="awaiting_approval",
                waiting_for="approval:Architecture Review",
            ),
            _state_part(
                lifecycle="awaiting_approval",
                waiting_for="approval:Architecture Review",
            ),  # idempotent observation — must not re-emit
            _state_part(lifecycle="running_wave"),
            _state_part(lifecycle="running_wave"),  # steady-state — no emit
        ]
        for part in snapshots:
            event = translator.translate(part, ctx)
            if event is not None:
                events.append(event)

        # AC-4: exactly one paused + one resumed
        paused = [e for e in events if isinstance(e, BuildPausedPayload)]
        resumed = [e for e in events if isinstance(e, BuildResumedPayload)]
        assert len(paused) == 1, (
            "TASK-FRR-PEB-006 AC-4: exactly ONE BuildPausedPayload per "
            f"pause transition, got {len(paused)}"
        )
        assert len(resumed) == 1, (
            "TASK-FRR-PEB-006 AC-4: exactly ONE BuildResumedPayload per "
            f"resume transition, got {len(resumed)}"
        )

        # AC-4: both carry the inbound correlation_id
        assert paused[0].correlation_id == "corr-pause-resume-001"
        assert resumed[0].correlation_id == "corr-pause-resume-001"

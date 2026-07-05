"""Tests for ``forge.pipeline.cli_steering`` (TASK-MAG7-011).

Validates :class:`CliSteeringHandler` — the executor-layer surface that
turns ``forge cancel`` / ``forge skip`` / mid-flight directive CLI
commands into the corresponding pause-resolution / async-task-middleware
side effects.

Acceptance-criteria coverage map (TASK-MAG7-011):

* AC-001: ``CliSteeringHandler`` exists at
  ``src/forge/pipeline/cli_steering.py`` —
  :class:`TestHandlerExists`.
* AC-002 (handle_cancel branches):
    - cancel during pause → synthetic reject → CANCELLED —
      :class:`TestHandleCancelPausePath`.
    - cancel during autobuild → cancel_async_task + CANCELLED, no PR —
      :class:`TestHandleCancelAutobuildPath`.
    - cancel in any other non-terminal state → CANCELLED, no further
      dispatch — :class:`TestHandleCancelDirectPath`.
    - cancel on already-terminal build → no-op —
      :class:`TestHandleCancelTerminalNoop`.
* AC-003 / AC-006 / AC-007 (handle_skip branches):
    - constitutional refusal → SKIP_REFUSED_CONSTITUTIONAL, paused —
      :class:`TestHandleSkipRefused`.
    - permitted skip → SKIPPED + resume —
      :class:`TestHandleSkipPermitted`.
* AC-004 (handle_directive):
    - active autobuild → update_async_task append, returns immediately —
      :class:`TestHandleDirectiveQueued`.
    - no live autobuild for the feature → rejected without side effect —
      :class:`TestHandleDirectiveNoAutobuild`.
* AC-008: unit tests exercise all three methods with mocked supervisor +
  state channel + constitutional guard — every test class above uses
  in-memory fakes for the seven Protocols.
* Defensive boundaries (empty-string args, malformed snapshots) —
  :class:`TestInputValidation`.

All collaborators are satisfied by in-memory fakes so the suite runs
without SQLite, NATS, or LangGraph.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from forge.pipeline.cli_steering import (
    CANCEL_AUTOBUILD_RATIONALE,
    CANCEL_DIRECT_RATIONALE,
    CANCEL_NOOP_TERMINAL_RATIONALE,
    CANCEL_REJECT_RATIONALE,
    DIRECTIVE_NO_AUTOBUILD_RATIONALE,
    DIRECTIVE_QUEUED_RATIONALE,
    SKIP_RECORDED_RATIONALE,
    SKIP_REFUSED_RATIONALE,
    BuildLifecycle,
    BuildSnapshot,
    CancelOutcome,
    CancelStatus,
    CliSteeringHandler,
    DirectiveOutcome,
    DirectiveStatus,
    SkipOutcome,
    SkipStatus,
)
from forge.pipeline.constitutional_guard import (
    ConstitutionalGuard,
    SkipDecision,
    SkipVerdict,
)
from forge.pipeline.stage_taxonomy import (
    CONSTITUTIONAL_STAGES,
    StageClass,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class FakeSnapshotReader:
    """In-memory ``BuildSnapshotReader``; returns pre-seeded snapshots."""

    snapshots: dict[str, BuildSnapshot] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def get_snapshot(self, build_id: str) -> BuildSnapshot:
        self.calls.append(build_id)
        try:
            return self.snapshots[build_id]
        except KeyError as exc:  # pragma: no cover - test wiring guard
            raise AssertionError(
                f"FakeSnapshotReader: no snapshot seeded for {build_id!r}"
            ) from exc


@dataclass
class FakePauseRejectResolver:
    """Records every synthetic-reject resolution."""

    calls: list[dict[str, Any]] = field(default_factory=list)

    def resolve_as_reject(
        self,
        build_id: str,
        stage: StageClass,
        feature_id: str | None,
        rationale: str,
    ) -> Any:
        self.calls.append(
            {
                "build_id": build_id,
                "stage": stage,
                "feature_id": feature_id,
                "rationale": rationale,
            }
        )
        return {"resolved": "reject"}


@dataclass
class FakeAsyncTaskCanceller:
    """Records every ``cancel_async_task`` invocation."""

    cancelled: list[str] = field(default_factory=list)

    def cancel_async_task(self, task_id: str) -> Any:
        self.cancelled.append(task_id)
        return {"cancelled": task_id}


@dataclass
class FakeAsyncTaskUpdater:
    """Records every ``update_async_task`` invocation."""

    appends: list[dict[str, Any]] = field(default_factory=list)

    def update_async_task(
        self,
        task_id: str,
        *,
        append_pending_directive: str,
    ) -> Any:
        self.appends.append(
            {
                "task_id": task_id,
                "append_pending_directive": append_pending_directive,
            }
        )
        return {"task_id": task_id, "queued": append_pending_directive}


@dataclass
class FakeBuildCanceller:
    """Records every terminal-cancel transition request."""

    cancelled: list[dict[str, Any]] = field(default_factory=list)

    def mark_cancelled(self, build_id: str, rationale: str) -> Any:
        self.cancelled.append({"build_id": build_id, "rationale": rationale})
        return {"build_id": build_id, "state": "CANCELLED"}


@dataclass
class FakeStageSkipRecorder:
    """Records both permitted-skip and refused-skip rows."""

    skipped: list[dict[str, Any]] = field(default_factory=list)
    refused: list[dict[str, Any]] = field(default_factory=list)

    def record_skipped(
        self,
        build_id: str,
        stage: StageClass,
        rationale: str,
    ) -> Any:
        self.skipped.append(
            {"build_id": build_id, "stage": stage, "rationale": rationale}
        )

    def record_skip_refused(
        self,
        build_id: str,
        stage: StageClass,
        rationale: str,
    ) -> Any:
        self.refused.append(
            {"build_id": build_id, "stage": stage, "rationale": rationale}
        )


@dataclass
class FakeBuildResumer:
    """Records every resume-after-skip nudge."""

    resumed: list[dict[str, Any]] = field(default_factory=list)

    def resume_after_skip(
        self,
        build_id: str,
        skipped_stage: StageClass,
    ) -> Any:
        self.resumed.append({"build_id": build_id, "skipped_stage": skipped_stage})


# ---------------------------------------------------------------------------
# Fixtures — assembled handler with happy-path defaults
# ---------------------------------------------------------------------------


@pytest.fixture
def snapshot_reader() -> FakeSnapshotReader:
    return FakeSnapshotReader()


@pytest.fixture
def pause_resolver() -> FakePauseRejectResolver:
    return FakePauseRejectResolver()


@pytest.fixture
def task_canceller() -> FakeAsyncTaskCanceller:
    return FakeAsyncTaskCanceller()


@pytest.fixture
def task_updater() -> FakeAsyncTaskUpdater:
    return FakeAsyncTaskUpdater()


@pytest.fixture
def build_canceller() -> FakeBuildCanceller:
    return FakeBuildCanceller()


@pytest.fixture
def skip_recorder() -> FakeStageSkipRecorder:
    return FakeStageSkipRecorder()


@pytest.fixture
def build_resumer() -> FakeBuildResumer:
    return FakeBuildResumer()


@pytest.fixture
def constitutional_guard() -> ConstitutionalGuard:
    # Default canonical guard; individual tests override with empty-set
    # negative-control instances where AC-007 demands it.
    return ConstitutionalGuard()


@pytest.fixture
def handler(
    snapshot_reader: FakeSnapshotReader,
    pause_resolver: FakePauseRejectResolver,
    task_canceller: FakeAsyncTaskCanceller,
    task_updater: FakeAsyncTaskUpdater,
    build_canceller: FakeBuildCanceller,
    skip_recorder: FakeStageSkipRecorder,
    build_resumer: FakeBuildResumer,
    constitutional_guard: ConstitutionalGuard,
) -> CliSteeringHandler:
    return CliSteeringHandler(
        snapshot_reader=snapshot_reader,
        pause_reject_resolver=pause_resolver,
        async_task_canceller=task_canceller,
        async_task_updater=task_updater,
        build_canceller=build_canceller,
        skip_recorder=skip_recorder,
        build_resumer=build_resumer,
        constitutional_guard=constitutional_guard,
    )


# ---------------------------------------------------------------------------
# AC-001 — class exists at the documented module path
# ---------------------------------------------------------------------------


class TestHandlerExists:
    """AC-001: ``CliSteeringHandler`` is importable from the canonical path."""

    def test_handler_class_lives_at_pipeline_cli_steering(self) -> None:
        from forge.pipeline import cli_steering

        assert cli_steering.CliSteeringHandler is CliSteeringHandler

    def test_default_constitutional_guard_uses_canonical_set(
        self,
        snapshot_reader: FakeSnapshotReader,
        pause_resolver: FakePauseRejectResolver,
        task_canceller: FakeAsyncTaskCanceller,
        task_updater: FakeAsyncTaskUpdater,
        build_canceller: FakeBuildCanceller,
        skip_recorder: FakeStageSkipRecorder,
        build_resumer: FakeBuildResumer,
    ) -> None:
        handler = CliSteeringHandler(
            snapshot_reader=snapshot_reader,
            pause_reject_resolver=pause_resolver,
            async_task_canceller=task_canceller,
            async_task_updater=task_updater,
            build_canceller=build_canceller,
            skip_recorder=skip_recorder,
            build_resumer=build_resumer,
        )

        assert isinstance(handler.constitutional_guard, ConstitutionalGuard)
        assert (
            handler.constitutional_guard.constitutional_stages == CONSTITUTIONAL_STAGES
        )


# ---------------------------------------------------------------------------
# AC-002 — handle_cancel: pause-reject path
# ---------------------------------------------------------------------------


class TestHandleCancelPausePath:
    """AC-002 first branch: cancel during pause → synthetic reject."""

    @pytest.fixture
    def paused_build_id(self) -> str:
        return "build-FEAT-X-20260426"

    @pytest.fixture(autouse=True)
    def _seed_snapshot(
        self,
        snapshot_reader: FakeSnapshotReader,
        paused_build_id: str,
    ) -> None:
        snapshot_reader.snapshots[paused_build_id] = BuildSnapshot(
            build_id=paused_build_id,
            lifecycle=BuildLifecycle.PAUSED_AT_GATE,
            paused_stage=StageClass.PULL_REQUEST_REVIEW,
            paused_feature_id="FEAT-X",
        )

    def test_handle_cancel_during_pause_returns_synthetic_reject_status(
        self,
        handler: CliSteeringHandler,
        paused_build_id: str,
    ) -> None:
        outcome = handler.handle_cancel(paused_build_id)

        assert isinstance(outcome, CancelOutcome)
        assert outcome.status is CancelStatus.CANCELLED_VIA_PAUSE_REJECT
        assert outcome.is_terminal is True
        assert outcome.paused_stage is StageClass.PULL_REQUEST_REVIEW
        assert outcome.paused_feature_id == "FEAT-X"
        assert outcome.cancelled_task_id is None

    def test_handle_cancel_during_pause_resolves_pause_as_reject(
        self,
        handler: CliSteeringHandler,
        paused_build_id: str,
        pause_resolver: FakePauseRejectResolver,
    ) -> None:
        handler.handle_cancel(paused_build_id)

        assert len(pause_resolver.calls) == 1
        call = pause_resolver.calls[0]
        assert call["build_id"] == paused_build_id
        assert call["stage"] is StageClass.PULL_REQUEST_REVIEW
        assert call["feature_id"] == "FEAT-X"
        assert "FEAT-FORGE-004 ASSUM-005" in call["rationale"]

    def test_handle_cancel_during_pause_marks_build_cancelled(
        self,
        handler: CliSteeringHandler,
        paused_build_id: str,
        build_canceller: FakeBuildCanceller,
    ) -> None:
        handler.handle_cancel(paused_build_id)

        assert len(build_canceller.cancelled) == 1
        assert build_canceller.cancelled[0]["build_id"] == paused_build_id
        assert "FEAT-FORGE-004 ASSUM-005" in build_canceller.cancelled[0]["rationale"]

    def test_handle_cancel_during_pause_does_not_invoke_async_canceller(
        self,
        handler: CliSteeringHandler,
        paused_build_id: str,
        task_canceller: FakeAsyncTaskCanceller,
    ) -> None:
        handler.handle_cancel(paused_build_id)

        assert task_canceller.cancelled == []

    def test_handle_cancel_during_pause_uses_canonical_rationale_template(
        self,
        handler: CliSteeringHandler,
        paused_build_id: str,
    ) -> None:
        outcome = handler.handle_cancel(paused_build_id)

        expected = CANCEL_REJECT_RATIONALE.format(
            stage=StageClass.PULL_REQUEST_REVIEW,
            feature_id="FEAT-X",
            build_id=paused_build_id,
        )
        assert outcome.rationale == expected


# ---------------------------------------------------------------------------
# AC-002 — handle_cancel: autobuild path
# ---------------------------------------------------------------------------


class TestHandleCancelAutobuildPath:
    """AC-002 second branch: cancel during autobuild → cancel_async_task."""

    @pytest.fixture
    def autobuild_build_id(self) -> str:
        return "build-FEAT-Y-20260426"

    @pytest.fixture
    def autobuild_task_id(self) -> str:
        return "autobuild-task-001"

    @pytest.fixture(autouse=True)
    def _seed_snapshot(
        self,
        snapshot_reader: FakeSnapshotReader,
        autobuild_build_id: str,
        autobuild_task_id: str,
    ) -> None:
        snapshot_reader.snapshots[autobuild_build_id] = BuildSnapshot(
            build_id=autobuild_build_id,
            lifecycle=BuildLifecycle.AUTOBUILD_RUNNING,
            active_autobuild_task_id=autobuild_task_id,
            active_autobuild_feature_id="FEAT-Y",
        )

    def test_handle_cancel_with_autobuild_returns_autobuild_status(
        self,
        handler: CliSteeringHandler,
        autobuild_build_id: str,
        autobuild_task_id: str,
    ) -> None:
        outcome = handler.handle_cancel(autobuild_build_id)

        assert outcome.status is CancelStatus.CANCELLED_VIA_AUTOBUILD
        assert outcome.is_terminal is True
        assert outcome.cancelled_task_id == autobuild_task_id
        assert outcome.paused_stage is None
        assert outcome.paused_feature_id is None

    def test_handle_cancel_with_autobuild_invokes_cancel_async_task(
        self,
        handler: CliSteeringHandler,
        autobuild_build_id: str,
        autobuild_task_id: str,
        task_canceller: FakeAsyncTaskCanceller,
    ) -> None:
        handler.handle_cancel(autobuild_build_id)

        assert task_canceller.cancelled == [autobuild_task_id]

    def test_handle_cancel_with_autobuild_marks_build_cancelled(
        self,
        handler: CliSteeringHandler,
        autobuild_build_id: str,
        autobuild_task_id: str,
        build_canceller: FakeBuildCanceller,
    ) -> None:
        handler.handle_cancel(autobuild_build_id)

        assert len(build_canceller.cancelled) == 1
        rationale = build_canceller.cancelled[0]["rationale"]
        assert autobuild_task_id in rationale
        assert "no PR-creation" in rationale

    def test_handle_cancel_with_autobuild_skips_pause_resolver(
        self,
        handler: CliSteeringHandler,
        autobuild_build_id: str,
        pause_resolver: FakePauseRejectResolver,
    ) -> None:
        handler.handle_cancel(autobuild_build_id)

        assert pause_resolver.calls == []

    def test_handle_cancel_with_autobuild_uses_canonical_rationale_template(
        self,
        handler: CliSteeringHandler,
        autobuild_build_id: str,
        autobuild_task_id: str,
    ) -> None:
        outcome = handler.handle_cancel(autobuild_build_id)

        expected = CANCEL_AUTOBUILD_RATIONALE.format(
            build_id=autobuild_build_id,
            task_id=autobuild_task_id,
            feature_id="FEAT-Y",
        )
        assert outcome.rationale == expected


# ---------------------------------------------------------------------------
# AC-002 — handle_cancel: direct path
# ---------------------------------------------------------------------------


class TestHandleCancelDirectPath:
    """AC-002 third branch: cancel from any other non-terminal state."""

    @pytest.fixture
    def direct_build_id(self) -> str:
        return "build-FEAT-Z-20260426"

    @pytest.fixture(autouse=True)
    def _seed_snapshot(
        self,
        snapshot_reader: FakeSnapshotReader,
        direct_build_id: str,
    ) -> None:
        snapshot_reader.snapshots[direct_build_id] = BuildSnapshot(
            build_id=direct_build_id,
            lifecycle=BuildLifecycle.OTHER_RUNNING,
        )

    def test_handle_cancel_from_other_running_returns_direct_status(
        self,
        handler: CliSteeringHandler,
        direct_build_id: str,
    ) -> None:
        outcome = handler.handle_cancel(direct_build_id)

        assert outcome.status is CancelStatus.CANCELLED_DIRECT
        assert outcome.is_terminal is True
        assert outcome.cancelled_task_id is None
        assert outcome.paused_stage is None

    def test_handle_cancel_from_other_running_marks_build_cancelled(
        self,
        handler: CliSteeringHandler,
        direct_build_id: str,
        build_canceller: FakeBuildCanceller,
    ) -> None:
        handler.handle_cancel(direct_build_id)

        assert len(build_canceller.cancelled) == 1
        assert build_canceller.cancelled[0]["build_id"] == direct_build_id

    def test_handle_cancel_from_other_running_invokes_no_async_middleware(
        self,
        handler: CliSteeringHandler,
        direct_build_id: str,
        task_canceller: FakeAsyncTaskCanceller,
        pause_resolver: FakePauseRejectResolver,
    ) -> None:
        handler.handle_cancel(direct_build_id)

        assert task_canceller.cancelled == []
        assert pause_resolver.calls == []

    def test_handle_cancel_from_other_running_uses_canonical_rationale(
        self,
        handler: CliSteeringHandler,
        direct_build_id: str,
    ) -> None:
        outcome = handler.handle_cancel(direct_build_id)

        expected = CANCEL_DIRECT_RATIONALE.format(
            build_id=direct_build_id,
            lifecycle=BuildLifecycle.OTHER_RUNNING,
        )
        assert outcome.rationale == expected


# ---------------------------------------------------------------------------
# AC-002 — handle_cancel: terminal no-op
# ---------------------------------------------------------------------------


class TestHandleCancelTerminalNoop:
    """Cancel on an already-terminal build is a no-op (no double-cancel)."""

    @pytest.fixture
    def terminal_build_id(self) -> str:
        return "build-FEAT-DONE-20260101"

    @pytest.fixture(autouse=True)
    def _seed_snapshot(
        self,
        snapshot_reader: FakeSnapshotReader,
        terminal_build_id: str,
    ) -> None:
        snapshot_reader.snapshots[terminal_build_id] = BuildSnapshot(
            build_id=terminal_build_id,
            lifecycle=BuildLifecycle.TERMINAL,
        )

    def test_handle_cancel_on_terminal_returns_noop_status(
        self,
        handler: CliSteeringHandler,
        terminal_build_id: str,
    ) -> None:
        outcome = handler.handle_cancel(terminal_build_id)

        assert outcome.status is CancelStatus.NOOP_ALREADY_TERMINAL
        assert outcome.is_terminal is False

    def test_handle_cancel_on_terminal_invokes_no_side_effects(
        self,
        handler: CliSteeringHandler,
        terminal_build_id: str,
        build_canceller: FakeBuildCanceller,
        task_canceller: FakeAsyncTaskCanceller,
        pause_resolver: FakePauseRejectResolver,
    ) -> None:
        handler.handle_cancel(terminal_build_id)

        assert build_canceller.cancelled == []
        assert task_canceller.cancelled == []
        assert pause_resolver.calls == []

    def test_handle_cancel_on_terminal_uses_canonical_rationale(
        self,
        handler: CliSteeringHandler,
        terminal_build_id: str,
    ) -> None:
        outcome = handler.handle_cancel(terminal_build_id)

        assert outcome.rationale == CANCEL_NOOP_TERMINAL_RATIONALE.format(
            build_id=terminal_build_id
        )


# ---------------------------------------------------------------------------
# AC-003 / AC-007 — handle_skip refused on constitutional stage
# ---------------------------------------------------------------------------


class TestHandleSkipRefused:
    """AC-007 / Group C @regression: skip on PR-review is refused."""

    @pytest.fixture
    def build_id(self) -> str:
        return "build-FEAT-CONST-20260426"

    def test_handle_skip_on_pull_request_review_returns_refused_status(
        self,
        handler: CliSteeringHandler,
        build_id: str,
    ) -> None:
        outcome = handler.handle_skip(build_id, StageClass.PULL_REQUEST_REVIEW)

        assert isinstance(outcome, SkipOutcome)
        assert outcome.status is SkipStatus.REFUSED_CONSTITUTIONAL
        assert outcome.is_refused is True
        assert outcome.stage is StageClass.PULL_REQUEST_REVIEW

    def test_handle_skip_on_pull_request_review_records_refusal(
        self,
        handler: CliSteeringHandler,
        build_id: str,
        skip_recorder: FakeStageSkipRecorder,
    ) -> None:
        handler.handle_skip(build_id, StageClass.PULL_REQUEST_REVIEW)

        assert len(skip_recorder.refused) == 1
        assert skip_recorder.refused[0]["build_id"] == build_id
        assert skip_recorder.refused[0]["stage"] is StageClass.PULL_REQUEST_REVIEW
        assert skip_recorder.skipped == []

    def test_handle_skip_refused_does_not_resume_build(
        self,
        handler: CliSteeringHandler,
        build_id: str,
        build_resumer: FakeBuildResumer,
    ) -> None:
        handler.handle_skip(build_id, StageClass.PULL_REQUEST_REVIEW)

        # Build remains paused — no resume nudge issued.
        assert build_resumer.resumed == []

    def test_handle_skip_refused_outcome_carries_underlying_decision(
        self,
        handler: CliSteeringHandler,
        build_id: str,
    ) -> None:
        outcome = handler.handle_skip(build_id, StageClass.PULL_REQUEST_REVIEW)

        assert isinstance(outcome.guard_decision, SkipDecision)
        assert outcome.guard_decision.verdict is SkipVerdict.REFUSED_CONSTITUTIONAL
        assert outcome.guard_decision.stage is StageClass.PULL_REQUEST_REVIEW

    def test_handle_skip_refused_rationale_quotes_guard_rationale(
        self,
        handler: CliSteeringHandler,
        build_id: str,
    ) -> None:
        outcome = handler.handle_skip(build_id, StageClass.PULL_REQUEST_REVIEW)

        # Cite ADR-ARCH-026 + name the stage; the guard's rationale
        # itself names ADR-ARCH-026, so the templated wrapper inherits
        # the citation. We assert both for belt-and-braces.
        assert "ADR-ARCH-026" in outcome.rationale
        assert "pull-request-review" in outcome.rationale
        # And the rationale must be the canonical SKIP_REFUSED_RATIONALE.
        expected = SKIP_REFUSED_RATIONALE.format(
            build_id=build_id,
            stage=StageClass.PULL_REQUEST_REVIEW,
            guard_rationale=outcome.guard_decision.rationale,
        )
        assert outcome.rationale == expected


# ---------------------------------------------------------------------------
# AC-003 / AC-006 — handle_skip permitted on non-constitutional stage
# ---------------------------------------------------------------------------


class TestHandleSkipPermitted:
    """AC-006 / Group D: permitted skip → SKIPPED + resume."""

    @pytest.fixture
    def build_id(self) -> str:
        return "build-FEAT-NORMAL-20260426"

    def test_handle_skip_on_non_constitutional_stage_returns_skipped(
        self,
        handler: CliSteeringHandler,
        build_id: str,
    ) -> None:
        outcome = handler.handle_skip(build_id, StageClass.SYSTEM_DESIGN)

        assert outcome.status is SkipStatus.SKIPPED
        assert outcome.is_refused is False
        assert outcome.stage is StageClass.SYSTEM_DESIGN

    def test_handle_skip_on_non_constitutional_stage_records_skipped(
        self,
        handler: CliSteeringHandler,
        build_id: str,
        skip_recorder: FakeStageSkipRecorder,
    ) -> None:
        handler.handle_skip(build_id, StageClass.SYSTEM_DESIGN)

        assert len(skip_recorder.skipped) == 1
        assert skip_recorder.skipped[0]["build_id"] == build_id
        assert skip_recorder.skipped[0]["stage"] is StageClass.SYSTEM_DESIGN
        assert skip_recorder.refused == []

    def test_handle_skip_on_non_constitutional_stage_resumes_build(
        self,
        handler: CliSteeringHandler,
        build_id: str,
        build_resumer: FakeBuildResumer,
    ) -> None:
        handler.handle_skip(build_id, StageClass.SYSTEM_DESIGN)

        assert build_resumer.resumed == [
            {"build_id": build_id, "skipped_stage": StageClass.SYSTEM_DESIGN}
        ]

    def test_handle_skip_permitted_outcome_carries_allowed_decision(
        self,
        handler: CliSteeringHandler,
        build_id: str,
    ) -> None:
        outcome = handler.handle_skip(build_id, StageClass.SYSTEM_DESIGN)

        assert outcome.guard_decision.verdict is SkipVerdict.ALLOWED

    def test_handle_skip_permitted_uses_canonical_recorded_rationale(
        self,
        handler: CliSteeringHandler,
        build_id: str,
    ) -> None:
        outcome = handler.handle_skip(build_id, StageClass.SYSTEM_DESIGN)

        expected = SKIP_RECORDED_RATIONALE.format(
            build_id=build_id,
            stage=StageClass.SYSTEM_DESIGN,
        )
        assert outcome.rationale == expected

    def test_handle_skip_with_negative_control_guard_permits_pr_review(
        self,
        snapshot_reader: FakeSnapshotReader,
        pause_resolver: FakePauseRejectResolver,
        task_canceller: FakeAsyncTaskCanceller,
        task_updater: FakeAsyncTaskUpdater,
        build_canceller: FakeBuildCanceller,
        skip_recorder: FakeStageSkipRecorder,
        build_resumer: FakeBuildResumer,
        build_id: str,
    ) -> None:
        # Inject an empty-set guard (Group E negative-control shape) to
        # confirm the handler defers permit/refuse to the guard rather
        # than hard-coding the constitutional set itself.
        empty_guard = ConstitutionalGuard(constitutional_stages=frozenset())
        empty_handler = CliSteeringHandler(
            snapshot_reader=snapshot_reader,
            pause_reject_resolver=pause_resolver,
            async_task_canceller=task_canceller,
            async_task_updater=task_updater,
            build_canceller=build_canceller,
            skip_recorder=skip_recorder,
            build_resumer=build_resumer,
            constitutional_guard=empty_guard,
        )

        outcome = empty_handler.handle_skip(build_id, StageClass.PULL_REQUEST_REVIEW)

        assert outcome.status is SkipStatus.SKIPPED
        assert skip_recorder.skipped[0]["stage"] is StageClass.PULL_REQUEST_REVIEW
        assert skip_recorder.refused == []
        assert build_resumer.resumed[0]["build_id"] == build_id


# ---------------------------------------------------------------------------
# AC-004 — handle_directive: queued onto an active autobuild
# ---------------------------------------------------------------------------


class TestHandleDirectiveQueued:
    """AC-004: directive on an active autobuild → update_async_task append."""

    @pytest.fixture
    def build_id(self) -> str:
        return "build-FEAT-Q-20260426"

    @pytest.fixture
    def feature_id(self) -> str:
        return "FEAT-Q"

    @pytest.fixture
    def task_id(self) -> str:
        return "autobuild-task-Q-7"

    @pytest.fixture(autouse=True)
    def _seed_snapshot(
        self,
        snapshot_reader: FakeSnapshotReader,
        build_id: str,
        feature_id: str,
        task_id: str,
    ) -> None:
        snapshot_reader.snapshots[build_id] = BuildSnapshot(
            build_id=build_id,
            lifecycle=BuildLifecycle.AUTOBUILD_RUNNING,
            active_autobuild_task_id=task_id,
            active_autobuild_feature_id=feature_id,
        )

    def test_handle_directive_returns_queued_status(
        self,
        handler: CliSteeringHandler,
        build_id: str,
        feature_id: str,
        task_id: str,
    ) -> None:
        outcome = handler.handle_directive(
            build_id, feature_id, "increase test coverage to 90%"
        )

        assert isinstance(outcome, DirectiveOutcome)
        assert outcome.status is DirectiveStatus.QUEUED
        assert outcome.is_queued is True
        assert outcome.task_id == task_id
        assert outcome.directive_text == "increase test coverage to 90%"

    def test_handle_directive_invokes_update_async_task(
        self,
        handler: CliSteeringHandler,
        build_id: str,
        feature_id: str,
        task_id: str,
        task_updater: FakeAsyncTaskUpdater,
    ) -> None:
        handler.handle_directive(build_id, feature_id, "increase test coverage to 90%")

        assert len(task_updater.appends) == 1
        assert task_updater.appends[0] == {
            "task_id": task_id,
            "append_pending_directive": "increase test coverage to 90%",
        }

    def test_handle_directive_does_not_call_cancel_or_pause_paths(
        self,
        handler: CliSteeringHandler,
        build_id: str,
        feature_id: str,
        task_canceller: FakeAsyncTaskCanceller,
        pause_resolver: FakePauseRejectResolver,
        build_canceller: FakeBuildCanceller,
        skip_recorder: FakeStageSkipRecorder,
    ) -> None:
        handler.handle_directive(build_id, feature_id, "tighten linting")

        assert task_canceller.cancelled == []
        assert pause_resolver.calls == []
        assert build_canceller.cancelled == []
        assert skip_recorder.skipped == []
        assert skip_recorder.refused == []

    def test_handle_directive_uses_canonical_queued_rationale(
        self,
        handler: CliSteeringHandler,
        build_id: str,
        feature_id: str,
        task_id: str,
    ) -> None:
        outcome = handler.handle_directive(build_id, feature_id, "use semver")

        expected = DIRECTIVE_QUEUED_RATIONALE.format(
            build_id=build_id,
            feature_id=feature_id,
            task_id=task_id,
        )
        assert outcome.rationale == expected


# ---------------------------------------------------------------------------
# AC-004 — handle_directive: no live autobuild to receive it
# ---------------------------------------------------------------------------


class TestHandleDirectiveNoAutobuild:
    """Directive aimed at a build with no live autobuild is rejected."""

    @pytest.fixture
    def build_id(self) -> str:
        return "build-FEAT-NA-20260426"

    def test_handle_directive_returns_no_active_when_lifecycle_other(
        self,
        snapshot_reader: FakeSnapshotReader,
        handler: CliSteeringHandler,
        build_id: str,
    ) -> None:
        snapshot_reader.snapshots[build_id] = BuildSnapshot(
            build_id=build_id,
            lifecycle=BuildLifecycle.OTHER_RUNNING,
        )

        outcome = handler.handle_directive(build_id, "FEAT-NA", "speed things up")

        assert outcome.status is DirectiveStatus.NO_ACTIVE_AUTOBUILD
        assert outcome.is_queued is False
        assert outcome.task_id is None

    def test_handle_directive_no_active_invokes_no_middleware(
        self,
        snapshot_reader: FakeSnapshotReader,
        handler: CliSteeringHandler,
        build_id: str,
        task_updater: FakeAsyncTaskUpdater,
    ) -> None:
        snapshot_reader.snapshots[build_id] = BuildSnapshot(
            build_id=build_id,
            lifecycle=BuildLifecycle.PAUSED_AT_GATE,
            paused_stage=StageClass.PULL_REQUEST_REVIEW,
            paused_feature_id="FEAT-NA",
        )

        outcome = handler.handle_directive(build_id, "FEAT-NA", "speed things up")

        assert outcome.status is DirectiveStatus.NO_ACTIVE_AUTOBUILD
        assert task_updater.appends == []

    def test_handle_directive_rejected_when_autobuild_runs_for_other_feature(
        self,
        snapshot_reader: FakeSnapshotReader,
        handler: CliSteeringHandler,
        build_id: str,
        task_updater: FakeAsyncTaskUpdater,
    ) -> None:
        # Active autobuild is for FEAT-OTHER; directive is aimed at
        # FEAT-NA. The handler must reject — directing a directive at
        # the wrong feature would cross-attribute the operator's
        # intent.
        snapshot_reader.snapshots[build_id] = BuildSnapshot(
            build_id=build_id,
            lifecycle=BuildLifecycle.AUTOBUILD_RUNNING,
            active_autobuild_task_id="autobuild-task-OTHER",
            active_autobuild_feature_id="FEAT-OTHER",
        )

        outcome = handler.handle_directive(build_id, "FEAT-NA", "speed things up")

        assert outcome.status is DirectiveStatus.NO_ACTIVE_AUTOBUILD
        assert task_updater.appends == []

    def test_handle_directive_no_active_uses_canonical_rationale(
        self,
        snapshot_reader: FakeSnapshotReader,
        handler: CliSteeringHandler,
        build_id: str,
    ) -> None:
        snapshot_reader.snapshots[build_id] = BuildSnapshot(
            build_id=build_id,
            lifecycle=BuildLifecycle.OTHER_RUNNING,
        )

        outcome = handler.handle_directive(build_id, "FEAT-NA", "speed things up")

        expected = DIRECTIVE_NO_AUTOBUILD_RATIONALE.format(
            build_id=build_id,
            feature_id="FEAT-NA",
        )
        assert outcome.rationale == expected


# ---------------------------------------------------------------------------
# Defensive — input validation + malformed snapshot guards
# ---------------------------------------------------------------------------


class TestInputValidation:
    """Empty primary keys and malformed snapshots are refused loudly."""

    def test_handle_cancel_refuses_empty_build_id(
        self, handler: CliSteeringHandler
    ) -> None:
        with pytest.raises(ValueError, match="build_id"):
            handler.handle_cancel("")

    def test_handle_skip_refuses_empty_build_id(
        self, handler: CliSteeringHandler
    ) -> None:
        with pytest.raises(ValueError, match="build_id"):
            handler.handle_skip("", StageClass.SYSTEM_DESIGN)

    def test_handle_directive_refuses_empty_build_id(
        self, handler: CliSteeringHandler
    ) -> None:
        with pytest.raises(ValueError, match="build_id"):
            handler.handle_directive("", "FEAT-X", "do thing")

    def test_handle_directive_refuses_empty_feature_id(
        self, handler: CliSteeringHandler
    ) -> None:
        with pytest.raises(ValueError, match="feature_id"):
            handler.handle_directive("build-1", "", "do thing")

    def test_handle_directive_refuses_empty_directive_text(
        self, handler: CliSteeringHandler
    ) -> None:
        with pytest.raises(ValueError, match="directive_text"):
            handler.handle_directive("build-1", "FEAT-X", "")

    def test_handle_cancel_raises_on_paused_snapshot_missing_stage(
        self,
        snapshot_reader: FakeSnapshotReader,
        handler: CliSteeringHandler,
    ) -> None:
        snapshot_reader.snapshots["build-bad"] = BuildSnapshot(
            build_id="build-bad",
            lifecycle=BuildLifecycle.PAUSED_AT_GATE,
            paused_stage=None,
        )
        with pytest.raises(ValueError, match="paused_stage is None"):
            handler.handle_cancel("build-bad")

    def test_handle_cancel_raises_on_autobuild_snapshot_missing_task_id(
        self,
        snapshot_reader: FakeSnapshotReader,
        handler: CliSteeringHandler,
    ) -> None:
        snapshot_reader.snapshots["build-bad-2"] = BuildSnapshot(
            build_id="build-bad-2",
            lifecycle=BuildLifecycle.AUTOBUILD_RUNNING,
            active_autobuild_task_id=None,
            active_autobuild_feature_id="FEAT-X",
        )
        with pytest.raises(ValueError, match="active_autobuild_task_id"):
            handler.handle_cancel("build-bad-2")


# ---------------------------------------------------------------------------
# TASK-JNB-102 — cancelled_notifier seam (build-cancelled wire signal)
# ---------------------------------------------------------------------------


@dataclass
class _SpyCancelledNotifier:
    """Records notify_cancelled calls; optionally raises (DDR-007 probe)."""

    calls: list[dict[str, str]] = field(default_factory=list)
    raise_on_call: bool = False

    def notify_cancelled(
        self, *, build_id: str, reason: str, cancelled_by: str
    ) -> None:
        if self.raise_on_call:
            raise RuntimeError("nats down")
        self.calls.append(
            {
                "build_id": build_id,
                "reason": reason,
                "cancelled_by": cancelled_by,
            }
        )


class TestCancelledNotifierSeam:
    """TASK-JNB-102: each cancel branch fires the notifier exactly once.

    The notifier is best-effort and fires strictly AFTER
    ``mark_cancelled``; the TERMINAL no-op never fires it; a raising
    notifier is swallowed with a WARNING (DDR-007 — the SQLite
    transition stands).
    """

    def _spy(self, handler: CliSteeringHandler) -> _SpyCancelledNotifier:
        spy = _SpyCancelledNotifier()
        handler.cancelled_notifier = spy
        return spy

    def test_pause_reject_branch_notifies_once_with_responder(
        self,
        handler: CliSteeringHandler,
        snapshot_reader: FakeSnapshotReader,
    ) -> None:
        snapshot_reader.snapshots["b-1"] = BuildSnapshot(
            build_id="b-1",
            lifecycle=BuildLifecycle.PAUSED_AT_GATE,
            paused_stage=StageClass.PULL_REQUEST_REVIEW,
            paused_feature_id="FEAT-X",
        )
        spy = self._spy(handler)

        handler.handle_cancel("b-1", reason="stop it", responder="rich")

        assert spy.calls == [
            {"build_id": "b-1", "reason": "stop it", "cancelled_by": "rich"}
        ]

    def test_autobuild_branch_notifies_once(
        self,
        handler: CliSteeringHandler,
        snapshot_reader: FakeSnapshotReader,
    ) -> None:
        snapshot_reader.snapshots["b-2"] = BuildSnapshot(
            build_id="b-2",
            lifecycle=BuildLifecycle.AUTOBUILD_RUNNING,
            active_autobuild_task_id="task-9",
            active_autobuild_feature_id="FEAT-X",
        )
        spy = self._spy(handler)

        handler.handle_cancel("b-2", reason=None, responder="rich")

        assert spy.calls == [
            {"build_id": "b-2", "reason": "cli cancel", "cancelled_by": "rich"}
        ]

    def test_other_running_branch_notifies_with_defaults(
        self,
        handler: CliSteeringHandler,
        snapshot_reader: FakeSnapshotReader,
    ) -> None:
        snapshot_reader.snapshots["b-3"] = BuildSnapshot(
            build_id="b-3",
            lifecycle=BuildLifecycle.OTHER_RUNNING,
        )
        spy = self._spy(handler)

        handler.handle_cancel("b-3")

        assert spy.calls == [
            {
                "build_id": "b-3",
                "reason": "cli cancel",
                "cancelled_by": "forge-cli",
            }
        ]

    def test_terminal_noop_never_notifies(
        self,
        handler: CliSteeringHandler,
        snapshot_reader: FakeSnapshotReader,
    ) -> None:
        snapshot_reader.snapshots["b-4"] = BuildSnapshot(
            build_id="b-4",
            lifecycle=BuildLifecycle.TERMINAL,
        )
        spy = self._spy(handler)

        outcome = handler.handle_cancel("b-4", responder="rich")

        assert outcome.status is CancelStatus.NOOP_ALREADY_TERMINAL
        assert spy.calls == []

    def test_default_none_notifier_preserves_pre_jnb102_behaviour(
        self,
        handler: CliSteeringHandler,
        snapshot_reader: FakeSnapshotReader,
    ) -> None:
        snapshot_reader.snapshots["b-5"] = BuildSnapshot(
            build_id="b-5",
            lifecycle=BuildLifecycle.OTHER_RUNNING,
        )
        assert handler.cancelled_notifier is None

        outcome = handler.handle_cancel("b-5")

        assert outcome.status is CancelStatus.CANCELLED_DIRECT

    def test_raising_notifier_is_swallowed_after_mark_cancelled(
        self,
        handler: CliSteeringHandler,
        snapshot_reader: FakeSnapshotReader,
        build_canceller: FakeBuildCanceller,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        snapshot_reader.snapshots["b-6"] = BuildSnapshot(
            build_id="b-6",
            lifecycle=BuildLifecycle.OTHER_RUNNING,
        )
        handler.cancelled_notifier = _SpyCancelledNotifier(raise_on_call=True)

        with caplog.at_level(logging.WARNING):
            outcome = handler.handle_cancel("b-6", responder="rich")

        # SQLite transition already recorded; no exception propagated.
        assert outcome.status is CancelStatus.CANCELLED_DIRECT
        assert len(build_canceller.cancelled) == 1
        assert any("build-cancelled notify failed" in r.message for r in caplog.records)

    def test_notifier_fires_strictly_after_mark_cancelled(
        self,
        handler: CliSteeringHandler,
        snapshot_reader: FakeSnapshotReader,
    ) -> None:
        # AC wording: transition-then-publish. Shared order log pins
        # the sequencing so a regression that emitted before the
        # SQLite write would fail here.
        order: list[str] = []

        class _OrderedCanceller:
            def mark_cancelled(self, build_id: str, rationale: str) -> Any:
                order.append("mark_cancelled")
                return {"build_id": build_id, "state": "CANCELLED"}

        class _OrderedNotifier:
            def notify_cancelled(self, **_: Any) -> None:
                order.append("notify_cancelled")

        snapshot_reader.snapshots["b-7"] = BuildSnapshot(
            build_id="b-7",
            lifecycle=BuildLifecycle.OTHER_RUNNING,
        )
        handler.build_canceller = _OrderedCanceller()
        handler.cancelled_notifier = _OrderedNotifier()

        handler.handle_cancel("b-7")

        assert order == ["mark_cancelled", "notify_cancelled"]

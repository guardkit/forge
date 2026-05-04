"""Tests for ``forge.pipeline.dispatchers.autobuild_async`` (TASK-MAG7-009).

Validates :func:`dispatch_autobuild_async` — the launch contract that
turns a "the supervisor wants autobuild for this feature" decision into
(a) a ``stage_log`` row (durable evidence of the attempt),
(b) a ``start_async_task`` invocation (the runtime launch), and
(c) an ``async_tasks`` state-channel entry (advisory live state).

Test cases mirror TASK-MAG7-009 acceptance criteria one-for-one and the
FEAT-FORGE-007 Group A / Group F / Group I scenarios that touch async
dispatch. All four collaborator Protocols
(:class:`AsyncTaskStarter`, :class:`StageLogRecorder`,
:class:`AutobuildStateInitialiser`, and the
:class:`ForwardContextBuilder` from TASK-MAG7-006) are satisfied by
in-memory test doubles so the suite runs without LangGraph, SQLite, or
the FEAT-FORGE-005 allowlist subsystem.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import pytest

from forge.pipeline import forward_context_builder as fcb_module
from forge.pipeline.dispatchers import autobuild_async as autobuild_module
from forge.pipeline.dispatchers.autobuild_async import (
    AUTOBUILD_RUNNER_NAME,
    AUTOBUILD_STARTING_LIFECYCLE,
    AsyncTaskStarter,
    AutobuildDispatchHandle,
    AutobuildStateInitialiser,
    StageLogRecorder,
    dispatch_autobuild_async,
)
from forge.pipeline.forward_context_builder import (
    ApprovedStageEntry,
    ContextEntry,
    ForwardContextBuilder,
)
from forge.pipeline.stage_taxonomy import StageClass


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class FakeStageLogReader:
    """In-memory ``StageLogReader`` for the ForwardContextBuilder."""

    entries: dict[
        tuple[str, StageClass, str | None], ApprovedStageEntry
    ] = field(default_factory=dict)

    def get_approved_stage_entry(
        self,
        build_id: str,
        stage: StageClass,
        feature_id: str | None = None,
    ) -> ApprovedStageEntry | None:
        return self.entries.get((build_id, stage, feature_id))


@dataclass
class FakeWorktreeAllowlist:
    """In-memory ``WorktreeAllowlist`` for the ForwardContextBuilder."""

    roots_by_build: dict[str, str] = field(default_factory=dict)

    def is_allowed(self, build_id: str, path: str) -> bool:
        root = self.roots_by_build.get(build_id)
        if root is None:
            return False
        return path == root or path.startswith(root.rstrip("/") + "/")


@dataclass
class StageLogRecord:
    """A single recorded ``stage_log`` write captured by the fake."""

    build_id: str
    feature_id: str
    stage: StageClass
    details_json: dict[str, Any]


@dataclass
class FakeStageLogRecorder:
    """In-memory upsert recorder; tracks every call in order."""

    calls: list[StageLogRecord] = field(default_factory=list)

    def record_running(
        self,
        build_id: str,
        feature_id: str,
        stage: StageClass,
        details_json: Mapping[str, Any],
    ) -> None:
        self.calls.append(
            StageLogRecord(
                build_id=build_id,
                feature_id=feature_id,
                stage=stage,
                details_json=dict(details_json),
            )
        )


@dataclass
class StateChannelRecord:
    """A single recorded ``async_tasks`` initialisation."""

    build_id: str
    feature_id: str
    task_id: str
    correlation_id: str
    lifecycle: str
    wave_index: int
    task_index: int


@dataclass
class FakeStateChannel:
    """In-memory ``AutobuildStateInitialiser`` capturing each call."""

    calls: list[StateChannelRecord] = field(default_factory=list)

    def initialise_autobuild_state(
        self,
        build_id: str,
        feature_id: str,
        task_id: str,
        correlation_id: str,
        lifecycle: str,
        wave_index: int,
        task_index: int,
    ) -> None:
        self.calls.append(
            StateChannelRecord(
                build_id=build_id,
                feature_id=feature_id,
                task_id=task_id,
                correlation_id=correlation_id,
                lifecycle=lifecycle,
                wave_index=wave_index,
                task_index=task_index,
            )
        )


@dataclass
class FakeAsyncTaskStarter:
    """In-memory ``AsyncTaskStarter`` that mints sequential task IDs.

    Each call to :meth:`astart_async_task` returns a fresh ``task_id``
    drawn from a thread-safe counter, so concurrent dispatches receive
    distinct identifiers (Group F @concurrency assertion).

    TASK-FORGE-FRR-F010G: the production dispatcher now awaits the
    Protocol's async ``astart_async_task`` method; the legacy sync
    ``start_async_task`` is preserved for symmetry with the Protocol
    and so isinstance checks against ``AsyncTaskStarter`` still pass.
    """

    prefix: str = "autobuild-task-"
    _counter: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def _mint(
        self,
        subagent_name: str,
        context: Mapping[str, Any],
    ) -> str:
        with self._lock:
            self._counter += 1
            task_id = f"{self.prefix}{self._counter:04d}"
            self.calls.append((subagent_name, dict(context)))
        return task_id

    def start_async_task(
        self,
        subagent_name: str,
        context: Mapping[str, Any],
    ) -> str:
        return self._mint(subagent_name, context)

    async def astart_async_task(
        self,
        subagent_name: str,
        context: Mapping[str, Any],
    ) -> str:
        return self._mint(subagent_name, context)


class EmptyTaskIdStarter:
    """Contract-violating starter that returns an empty task_id.

    Used to assert :func:`dispatch_autobuild_async` rejects the
    contract violation rather than silently writing a state-channel
    entry keyed on ``""``.
    """

    def start_async_task(
        self,
        subagent_name: str,
        context: Mapping[str, Any],
    ) -> str:
        return ""

    async def astart_async_task(
        self,
        subagent_name: str,
        context: Mapping[str, Any],
    ) -> str:
        return ""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def reader() -> FakeStageLogReader:
    return FakeStageLogReader()


@pytest.fixture
def allowlist() -> FakeWorktreeAllowlist:
    return FakeWorktreeAllowlist(
        roots_by_build={"build-1": "/work/build-1"},
    )


@pytest.fixture
def builder(
    reader: FakeStageLogReader,
    allowlist: FakeWorktreeAllowlist,
) -> ForwardContextBuilder:
    return ForwardContextBuilder(
        stage_log_reader=reader,
        worktree_allowlist=allowlist,
    )


@pytest.fixture
def starter() -> FakeAsyncTaskStarter:
    return FakeAsyncTaskStarter()


@pytest.fixture
def stage_log() -> FakeStageLogRecorder:
    return FakeStageLogRecorder()


@pytest.fixture
def state_channel() -> FakeStateChannel:
    return FakeStateChannel()


@pytest.fixture
def approved_feature_plan(
    reader: FakeStageLogReader,
) -> ApprovedStageEntry:
    """Pre-stage an approved feature-plan row for FEAT-1 in build-1.

    The autobuild stage's :data:`PROPAGATION_CONTRACT` recipe consumes
    the feature-plan stage's path artefact (per TASK-MAG7-002), so most
    tests need this row in place to get a non-empty context.
    """
    entry = ApprovedStageEntry(
        gate_decision="approved",
        artefact_paths=("/work/build-1/plans/feature-plan-FEAT-1.md",),
        artefact_text=None,
    )
    reader.entries[("build-1", StageClass.FEATURE_PLAN, "FEAT-1")] = entry
    return entry


# ---------------------------------------------------------------------------
# AC-001 — function exists at the documented module path
# ---------------------------------------------------------------------------


class TestDispatchAutobuildAsyncExists:
    """AC-001 — function exists at
    ``src/forge/pipeline/dispatchers/autobuild_async.py``.
    """

    def test_module_path_is_dispatchers_autobuild_async(self) -> None:
        assert (
            autobuild_module.__name__
            == "forge.pipeline.dispatchers.autobuild_async"
        )

    def test_module_file_lives_under_dispatchers_directory(self) -> None:
        path = Path(autobuild_module.__file__)
        assert path.name == "autobuild_async.py"
        assert path.parent.name == "dispatchers"
        assert path.parent.parent.name == "pipeline"
        assert path.parent.parent.parent.name == "forge"

    def test_dispatch_function_is_callable(self) -> None:
        assert callable(dispatch_autobuild_async)

    def test_dispatch_function_is_a_coroutine_function(self) -> None:
        # TASK-FORGE-FRR-F010G: the dispatcher is ``async def`` so it
        # can ``await`` the deepagents middleware's async launch path
        # (``StructuredTool.coroutine``) — the sync path raises on
        # url=None which the autobuild_runner registration omits.
        import asyncio

        assert asyncio.iscoroutinefunction(dispatch_autobuild_async)

    def test_handle_dataclass_is_frozen(self) -> None:
        with pytest.raises(Exception):  # FrozenInstanceError subclasses Exception
            handle = AutobuildDispatchHandle(
                task_id="t",
                feature_id="f",
                build_id="b",
                correlation_id="c",
            )
            handle.task_id = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# AC-002 — calls ForwardContextBuilder.build_for(AUTOBUILD, build_id, feature_id)
# ---------------------------------------------------------------------------


class TestForwardContextResolution:
    """AC-002 — the dispatcher resolves forward context via the builder."""

    @pytest.mark.asyncio
    async def test_calls_builder_with_autobuild_stage_and_feature_id(
        self,
        builder: ForwardContextBuilder,
        starter: FakeAsyncTaskStarter,
        stage_log: FakeStageLogRecorder,
        state_channel: FakeStateChannel,
        approved_feature_plan: ApprovedStageEntry,
    ) -> None:
        # Arrange: spy on builder.build_for to capture the args.
        captured: dict[str, Any] = {}
        original_build_for = builder.build_for

        def spy_build_for(
            stage: StageClass, build_id: str, feature_id: str | None
        ) -> list[ContextEntry]:
            captured["stage"] = stage
            captured["build_id"] = build_id
            captured["feature_id"] = feature_id
            return original_build_for(
                stage=stage, build_id=build_id, feature_id=feature_id
            )

        builder.build_for = spy_build_for  # type: ignore[method-assign]

        # Act
        await dispatch_autobuild_async(
            build_id="build-1",
            feature_id="FEAT-1",
            correlation_id="corr-1",
            forward_context_builder=builder,
            async_task_starter=starter,
            stage_log_recorder=stage_log,
            state_channel=state_channel,
        )

        # Assert
        assert captured == {
            "stage": StageClass.AUTOBUILD,
            "build_id": "build-1",
            "feature_id": "FEAT-1",
        }


# ---------------------------------------------------------------------------
# AC-003 — invokes start_async_task with the autobuild_runner subagent name
# ---------------------------------------------------------------------------


class TestStartAsyncTaskInvocation:
    """AC-003 — middleware invocation contract."""

    @pytest.mark.asyncio
    async def test_invokes_start_async_task_with_autobuild_runner(
        self,
        builder: ForwardContextBuilder,
        starter: FakeAsyncTaskStarter,
        stage_log: FakeStageLogRecorder,
        state_channel: FakeStateChannel,
        approved_feature_plan: ApprovedStageEntry,
    ) -> None:
        await dispatch_autobuild_async(
            build_id="build-1",
            feature_id="FEAT-1",
            correlation_id="corr-1",
            forward_context_builder=builder,
            async_task_starter=starter,
            stage_log_recorder=stage_log,
            state_channel=state_channel,
        )

        assert len(starter.calls) == 1
        subagent_name, context = starter.calls[0]
        assert subagent_name == AUTOBUILD_RUNNER_NAME
        # The context payload must carry the four identifiers plus
        # the resolved forward context.
        assert context["build_id"] == "build-1"
        assert context["feature_id"] == "FEAT-1"
        assert context["correlation_id"] == "corr-1"
        # Approved feature-plan path is threaded into the launch payload.
        assert context["context_entries"] == [
            {
                "flag": "--context",
                "value": "/work/build-1/plans/feature-plan-FEAT-1.md",
                "kind": "path",
            }
        ]


# ---------------------------------------------------------------------------
# AC-004 — returns task_id immediately; does not await completion
# ---------------------------------------------------------------------------


class TestSynchronousReturn:
    """AC-004 — dispatch is non-blocking on the runner's body."""

    @pytest.mark.asyncio
    async def test_returns_handle_with_minted_task_id(
        self,
        builder: ForwardContextBuilder,
        starter: FakeAsyncTaskStarter,
        stage_log: FakeStageLogRecorder,
        state_channel: FakeStateChannel,
        approved_feature_plan: ApprovedStageEntry,
    ) -> None:
        handle = await dispatch_autobuild_async(
            build_id="build-1",
            feature_id="FEAT-1",
            correlation_id="corr-1",
            forward_context_builder=builder,
            async_task_starter=starter,
            stage_log_recorder=stage_log,
            state_channel=state_channel,
        )
        assert isinstance(handle, AutobuildDispatchHandle)
        # The fake starter mints sequential IDs; the first call returns
        # autobuild-task-0001.
        assert handle.task_id == "autobuild-task-0001"
        assert handle.build_id == "build-1"
        assert handle.feature_id == "FEAT-1"
        assert handle.correlation_id == "corr-1"

    @pytest.mark.asyncio
    async def test_dispatch_does_not_await_runner_completion(
        self,
        builder: ForwardContextBuilder,
        stage_log: FakeStageLogRecorder,
        state_channel: FakeStateChannel,
        approved_feature_plan: ApprovedStageEntry,
    ) -> None:
        # A starter that records work but does NOT execute the runner
        # body confirms the dispatcher only invokes the launch hook.
        runner_executed: list[bool] = []

        class _NonExecutingStarter:
            def start_async_task(
                self,
                subagent_name: str,
                context: Mapping[str, Any],
            ) -> str:
                # Note: we deliberately do not call any "execute" hook
                # — the dispatcher's contract is "submit and return".
                return "task-x"

            async def astart_async_task(
                self,
                subagent_name: str,
                context: Mapping[str, Any],
            ) -> str:
                return "task-x"

        starter = _NonExecutingStarter()
        handle = await dispatch_autobuild_async(
            build_id="build-1",
            feature_id="FEAT-1",
            correlation_id="corr-1",
            forward_context_builder=builder,
            async_task_starter=starter,
            stage_log_recorder=stage_log,
            state_channel=state_channel,
        )
        assert handle.task_id == "task-x"
        # Dispatcher never reached any "runner body" hook — no list entries.
        assert runner_executed == []


# ---------------------------------------------------------------------------
# AC-005 — initialises async_tasks entry with starting lifecycle
# ---------------------------------------------------------------------------


class TestAsyncTasksStateChannelInit:
    """AC-005 — initial AutobuildState shape."""

    @pytest.mark.asyncio
    async def test_state_channel_entry_has_starting_lifecycle(
        self,
        builder: ForwardContextBuilder,
        starter: FakeAsyncTaskStarter,
        stage_log: FakeStageLogRecorder,
        state_channel: FakeStateChannel,
        approved_feature_plan: ApprovedStageEntry,
    ) -> None:
        await dispatch_autobuild_async(
            build_id="build-1",
            feature_id="FEAT-1",
            correlation_id="corr-1",
            forward_context_builder=builder,
            async_task_starter=starter,
            stage_log_recorder=stage_log,
            state_channel=state_channel,
        )
        assert len(state_channel.calls) == 1
        record = state_channel.calls[0]
        assert record.lifecycle == AUTOBUILD_STARTING_LIFECYCLE == "starting"
        assert record.wave_index == 0
        assert record.task_index == 0
        assert record.feature_id == "FEAT-1"
        assert record.build_id == "build-1"

    @pytest.mark.asyncio
    async def test_state_channel_task_id_matches_starter_minted_value(
        self,
        builder: ForwardContextBuilder,
        starter: FakeAsyncTaskStarter,
        stage_log: FakeStageLogRecorder,
        state_channel: FakeStateChannel,
        approved_feature_plan: ApprovedStageEntry,
    ) -> None:
        handle = await dispatch_autobuild_async(
            build_id="build-1",
            feature_id="FEAT-1",
            correlation_id="corr-1",
            forward_context_builder=builder,
            async_task_starter=starter,
            stage_log_recorder=stage_log,
            state_channel=state_channel,
        )
        # The task_id in the returned handle MUST match the one
        # recorded on the state channel — this is the seam-test contract.
        assert handle.task_id == state_channel.calls[0].task_id


# ---------------------------------------------------------------------------
# AC-006 — correlation_id threaded onto AutobuildState
# ---------------------------------------------------------------------------


class TestCorrelationIdThreading:
    """AC-006 — correlation_id is threaded through state-channel + launch."""

    @pytest.mark.asyncio
    async def test_correlation_id_appears_on_state_channel_entry(
        self,
        builder: ForwardContextBuilder,
        starter: FakeAsyncTaskStarter,
        stage_log: FakeStageLogRecorder,
        state_channel: FakeStateChannel,
        approved_feature_plan: ApprovedStageEntry,
    ) -> None:
        await dispatch_autobuild_async(
            build_id="build-1",
            feature_id="FEAT-1",
            correlation_id="corr-FEAT-1-2026",
            forward_context_builder=builder,
            async_task_starter=starter,
            stage_log_recorder=stage_log,
            state_channel=state_channel,
        )
        assert state_channel.calls[0].correlation_id == "corr-FEAT-1-2026"

    @pytest.mark.asyncio
    async def test_correlation_id_appears_in_launch_context(
        self,
        builder: ForwardContextBuilder,
        starter: FakeAsyncTaskStarter,
        stage_log: FakeStageLogRecorder,
        state_channel: FakeStateChannel,
        approved_feature_plan: ApprovedStageEntry,
    ) -> None:
        await dispatch_autobuild_async(
            build_id="build-1",
            feature_id="FEAT-1",
            correlation_id="corr-FEAT-1-2026",
            forward_context_builder=builder,
            async_task_starter=starter,
            stage_log_recorder=stage_log,
            state_channel=state_channel,
        )
        _, context = starter.calls[0]
        assert context["correlation_id"] == "corr-FEAT-1-2026"


# ---------------------------------------------------------------------------
# AC-007 — stage_log row records task_id in details_json + state="running"
# ---------------------------------------------------------------------------


class TestStageLogRecording:
    """AC-007 — stage_log row carries the task_id in details_json."""

    @pytest.mark.asyncio
    async def test_stage_log_recorded_with_running_state_and_autobuild_stage(
        self,
        builder: ForwardContextBuilder,
        starter: FakeAsyncTaskStarter,
        stage_log: FakeStageLogRecorder,
        state_channel: FakeStateChannel,
        approved_feature_plan: ApprovedStageEntry,
    ) -> None:
        await dispatch_autobuild_async(
            build_id="build-1",
            feature_id="FEAT-1",
            correlation_id="corr-1",
            forward_context_builder=builder,
            async_task_starter=starter,
            stage_log_recorder=stage_log,
            state_channel=state_channel,
        )
        # Two upserts on the happy path: pre-dispatch and post-dispatch.
        assert len(stage_log.calls) == 2
        for record in stage_log.calls:
            assert record.build_id == "build-1"
            assert record.feature_id == "FEAT-1"
            assert record.stage == StageClass.AUTOBUILD

    @pytest.mark.asyncio
    async def test_post_dispatch_stage_log_carries_task_id(
        self,
        builder: ForwardContextBuilder,
        starter: FakeAsyncTaskStarter,
        stage_log: FakeStageLogRecorder,
        state_channel: FakeStateChannel,
        approved_feature_plan: ApprovedStageEntry,
    ) -> None:
        handle = await dispatch_autobuild_async(
            build_id="build-1",
            feature_id="FEAT-1",
            correlation_id="corr-1",
            forward_context_builder=builder,
            async_task_starter=starter,
            stage_log_recorder=stage_log,
            state_channel=state_channel,
        )
        post = stage_log.calls[-1]
        assert post.details_json["task_id"] == handle.task_id
        assert post.details_json["correlation_id"] == "corr-1"
        assert post.details_json["subagent"] == AUTOBUILD_RUNNER_NAME

    @pytest.mark.asyncio
    async def test_pre_dispatch_stage_log_has_null_task_id(
        self,
        builder: ForwardContextBuilder,
        starter: FakeAsyncTaskStarter,
        stage_log: FakeStageLogRecorder,
        state_channel: FakeStateChannel,
        approved_feature_plan: ApprovedStageEntry,
    ) -> None:
        # Pre-dispatch row exists so a crash between submit and ack still
        # leaves durable evidence. AC: "record stage_log BEFORE start_async_task".
        await dispatch_autobuild_async(
            build_id="build-1",
            feature_id="FEAT-1",
            correlation_id="corr-1",
            forward_context_builder=builder,
            async_task_starter=starter,
            stage_log_recorder=stage_log,
            state_channel=state_channel,
        )
        pre = stage_log.calls[0]
        assert pre.details_json["task_id"] is None
        assert pre.details_json["correlation_id"] == "corr-1"


# ---------------------------------------------------------------------------
# Crash-recovery invariant — stage_log written BEFORE start_async_task
# ---------------------------------------------------------------------------


class TestCrashRecoveryOrdering:
    """The dispatcher must record ``stage_log`` **before** invoking
    ``start_async_task``. This is the implementation-note invariant
    that satisfies Group D @edge-case crash-recovery semantics.
    """

    @pytest.mark.asyncio
    async def test_stage_log_write_precedes_start_async_task_call(
        self,
        builder: ForwardContextBuilder,
        stage_log: FakeStageLogRecorder,
        state_channel: FakeStateChannel,
        approved_feature_plan: ApprovedStageEntry,
    ) -> None:
        events: list[str] = []

        class _OrderTrackingStageLog:
            def record_running(
                self,
                build_id: str,
                feature_id: str,
                stage: StageClass,
                details_json: Mapping[str, Any],
            ) -> None:
                events.append("stage_log")
                stage_log.record_running(
                    build_id=build_id,
                    feature_id=feature_id,
                    stage=stage,
                    details_json=details_json,
                )

        class _OrderTrackingStarter:
            def start_async_task(
                self,
                subagent_name: str,
                context: Mapping[str, Any],
            ) -> str:
                events.append("start_async_task")
                return "task-1"

            async def astart_async_task(
                self,
                subagent_name: str,
                context: Mapping[str, Any],
            ) -> str:
                events.append("start_async_task")
                return "task-1"

        await dispatch_autobuild_async(
            build_id="build-1",
            feature_id="FEAT-1",
            correlation_id="corr-1",
            forward_context_builder=builder,
            async_task_starter=_OrderTrackingStarter(),
            stage_log_recorder=_OrderTrackingStageLog(),
            state_channel=state_channel,
        )
        # First event must be stage_log; start_async_task must come after.
        assert events[0] == "stage_log"
        assert "start_async_task" in events
        assert events.index("stage_log") < events.index("start_async_task")


# ---------------------------------------------------------------------------
# AC-008 — concurrent dispatches receive distinct task_id values
# ---------------------------------------------------------------------------


class TestConcurrentDispatchesGetDistinctTaskIds:
    """AC-008 — Group F @concurrency: two concurrent builds dispatching
    autobuild at the same time receive distinct ``task_id`` values.

    TASK-FORGE-FRR-F010G: ``dispatch_autobuild_async`` is now ``async
    def`` so concurrency is exercised via :func:`asyncio.gather` instead
    of OS threads — the contract is unchanged (two concurrent dispatches
    must mint distinct task_ids), just expressed in the async idiom.
    """

    @pytest.mark.asyncio
    async def test_two_concurrent_dispatches_get_distinct_task_ids(
        self,
    ) -> None:
        import asyncio

        # Each concurrent dispatch gets its own collaborator set; the
        # shared piece is the AsyncTaskStarter so its counter is the
        # source of distinctness.
        shared_starter = FakeAsyncTaskStarter()

        async def _dispatch_one(
            build_id: str, feature_id: str, correlation_id: str
        ) -> AutobuildDispatchHandle:
            reader = FakeStageLogReader()
            allowlist = FakeWorktreeAllowlist(
                roots_by_build={build_id: f"/work/{build_id}"},
            )
            reader.entries[(build_id, StageClass.FEATURE_PLAN, feature_id)] = (
                ApprovedStageEntry(
                    gate_decision="approved",
                    artefact_paths=(
                        f"/work/{build_id}/plan-{feature_id}.md",
                    ),
                    artefact_text=None,
                )
            )
            builder = ForwardContextBuilder(
                stage_log_reader=reader,
                worktree_allowlist=allowlist,
            )
            return await dispatch_autobuild_async(
                build_id=build_id,
                feature_id=feature_id,
                correlation_id=correlation_id,
                forward_context_builder=builder,
                async_task_starter=shared_starter,
                stage_log_recorder=FakeStageLogRecorder(),
                state_channel=FakeStateChannel(),
            )

        results = await asyncio.gather(
            _dispatch_one("build-A", "FEAT-A", "corr-A"),
            _dispatch_one("build-B", "FEAT-B", "corr-B"),
        )

        assert len(results) == 2
        task_ids = [h.task_id for h in results]
        assert len(set(task_ids)) == 2, (
            f"Concurrent dispatches must mint distinct task_ids; got {task_ids}"
        )


# ---------------------------------------------------------------------------
# Empty-identifier guards
# ---------------------------------------------------------------------------


class TestEmptyIdentifierGuards:
    """Defensive: empty build_id / feature_id / correlation_id are rejected
    rather than launching an autobuild that cannot be correlated.
    """

    @pytest.mark.parametrize(
        "build_id, feature_id, correlation_id, expected_substring",
        [
            ("", "FEAT-1", "corr-1", "build_id"),
            ("build-1", "", "corr-1", "feature_id"),
            ("build-1", "FEAT-1", "", "correlation_id"),
        ],
    )
    @pytest.mark.asyncio
    async def test_empty_identifier_raises_value_error(
        self,
        builder: ForwardContextBuilder,
        starter: FakeAsyncTaskStarter,
        stage_log: FakeStageLogRecorder,
        state_channel: FakeStateChannel,
        build_id: str,
        feature_id: str,
        correlation_id: str,
        expected_substring: str,
    ) -> None:
        with pytest.raises(ValueError) as excinfo:
            await dispatch_autobuild_async(
                build_id=build_id,
                feature_id=feature_id,
                correlation_id=correlation_id,
                forward_context_builder=builder,
                async_task_starter=starter,
                stage_log_recorder=stage_log,
                state_channel=state_channel,
            )
        assert expected_substring in str(excinfo.value)
        # No dispatch side-effects on guard failure.
        assert stage_log.calls == []
        assert starter.calls == []
        assert state_channel.calls == []


# ---------------------------------------------------------------------------
# Empty task_id from starter is a contract violation
# ---------------------------------------------------------------------------


class TestEmptyTaskIdContractViolation:
    """A starter that returns an empty ``task_id`` is in contract
    violation. The dispatcher must refuse to write a state-channel
    entry keyed on ``""`` (which would alias every subsequent dispatch).
    """

    @pytest.mark.asyncio
    async def test_empty_task_id_raises_value_error_and_does_not_init_state(
        self,
        builder: ForwardContextBuilder,
        stage_log: FakeStageLogRecorder,
        state_channel: FakeStateChannel,
        approved_feature_plan: ApprovedStageEntry,
    ) -> None:
        with pytest.raises(ValueError) as excinfo:
            await dispatch_autobuild_async(
                build_id="build-1",
                feature_id="FEAT-1",
                correlation_id="corr-1",
                forward_context_builder=builder,
                async_task_starter=EmptyTaskIdStarter(),
                stage_log_recorder=stage_log,
                state_channel=state_channel,
            )
        assert "empty task_id" in str(excinfo.value)
        # The pre-dispatch stage_log row IS still written (durability).
        assert len(stage_log.calls) == 1
        assert stage_log.calls[0].details_json["task_id"] is None
        # State channel is NOT initialised on contract violation.
        assert state_channel.calls == []


# ---------------------------------------------------------------------------
# Protocol conformance — runtime_checkable Protocols
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    """The fakes must satisfy the runtime-checkable Protocols, which is
    the suite-level guarantee that the production wiring will too.
    """

    def test_fake_starter_satisfies_async_task_starter_protocol(
        self, starter: FakeAsyncTaskStarter
    ) -> None:
        assert isinstance(starter, AsyncTaskStarter)

    def test_fake_recorder_satisfies_stage_log_recorder_protocol(
        self, stage_log: FakeStageLogRecorder
    ) -> None:
        assert isinstance(stage_log, StageLogRecorder)

    def test_fake_state_channel_satisfies_initialiser_protocol(
        self, state_channel: FakeStateChannel
    ) -> None:
        assert isinstance(state_channel, AutobuildStateInitialiser)


# ---------------------------------------------------------------------------
# Empty-context fallback — feature plan not yet approved
# ---------------------------------------------------------------------------


class TestEmptyContextFallback:
    """If the upstream feature-plan stage is not yet approved the builder
    returns an empty list. The dispatcher still proceeds — refusing to
    dispatch on an empty context is StageOrderingGuard's job
    (TASK-MAG7-003), not this dispatcher's.
    """

    @pytest.mark.asyncio
    async def test_dispatch_proceeds_with_empty_context_when_plan_unapproved(
        self,
        builder: ForwardContextBuilder,
        starter: FakeAsyncTaskStarter,
        stage_log: FakeStageLogRecorder,
        state_channel: FakeStateChannel,
    ) -> None:
        # No approved_feature_plan fixture used — builder returns [].
        handle = await dispatch_autobuild_async(
            build_id="build-1",
            feature_id="FEAT-1",
            correlation_id="corr-1",
            forward_context_builder=builder,
            async_task_starter=starter,
            stage_log_recorder=stage_log,
            state_channel=state_channel,
        )
        assert handle.task_id == "autobuild-task-0001"
        # Context entries serialised as an empty list everywhere.
        _, context = starter.calls[0]
        assert context["context_entries"] == []
        for record in stage_log.calls:
            assert record.details_json["context_entries"] == []


# ---------------------------------------------------------------------------
# Smoke: re-export surface
# ---------------------------------------------------------------------------


class TestModuleExports:
    """The module exports the public symbols documented in ``__all__``."""

    def test_all_exports_resolvable(self) -> None:
        for name in autobuild_module.__all__:
            assert hasattr(autobuild_module, name), (
                f"forge.pipeline.dispatchers.autobuild_async missing export "
                f"{name!r}"
            )

    def test_forward_context_builder_module_unchanged(self) -> None:
        # Sanity guard: the dispatcher imports from
        # ``forge.pipeline.forward_context_builder`` — make sure the
        # public symbol surface is still there.
        assert hasattr(fcb_module, "ForwardContextBuilder")
        assert hasattr(fcb_module, "ContextEntry")

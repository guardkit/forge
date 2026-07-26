"""Tests for TASK-FW10-008 — supervisor async-subagent wiring + dispatcher emitter.

Validates that:

1. ``dispatch_autobuild_async`` accepts a ``lifecycle_emitter`` parameter
   and threads it into the ``start_async_task`` context payload as
   ``ctx['lifecycle_emitter']`` (DDR-007 §Decision Option A; AC-001).
2. The supervisor's tool list includes the
   ``AsyncSubAgentMiddleware`` start/check/update/cancel/list tools so
   the reasoning loop can dispatch (and observe) the autobuild stage as
   an :class:`AsyncSubAgent` per ADR-ARCH-031 (AC-002).
3. The supervisor stays responsive — answers status queries — while an
   autobuild's async task is in flight (FEAT-FORGE-007 Group A scenario;
   AC-003).
4. ``build_supervisor`` constructs the supervisor with the emitter +
   the four wave-2 collaborators; no second emitter is constructed
   (AC-004).
5. ``dispatch_autobuild_async`` exposes exactly five collaborator
   parameters (DDR-007 §Consequences; AC-005).

All collaborators are satisfied by in-memory test doubles so the suite
runs without LangGraph, SQLite, or NATS.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from dataclasses import dataclass, field
from typing import Any, Mapping
from unittest.mock import MagicMock

import pytest

from forge.cli.serve import (
    _make_autobuild_dispatcher_closure,
    build_supervisor,
)
from forge.pipeline.constitutional_guard import ConstitutionalGuard
from forge.pipeline.dispatchers.autobuild_async import (
    AUTOBUILD_RUNNER_NAME,
    AUTOBUILD_STARTING_LIFECYCLE,
    dispatch_autobuild_async,
)
from forge.pipeline.forward_context_builder import (
    ApprovedStageEntry,
    ForwardContextBuilder,
)
from forge.pipeline.per_feature_sequencer import PerFeatureLoopSequencer
from forge.pipeline.stage_ordering_guard import StageOrderingGuard
from forge.pipeline.stage_taxonomy import StageClass
from forge.pipeline.supervisor import (
    BuildState,
    DispatchChoice,
    Supervisor,
    TurnOutcome,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class FakeStageLogReader:
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
    roots_by_build: dict[str, str] = field(default_factory=dict)

    def is_allowed(self, build_id: str, path: str) -> bool:
        root = self.roots_by_build.get(build_id)
        if root is None:
            return False
        return path == root or path.startswith(root.rstrip("/") + "/")


@dataclass
class CapturingAsyncTaskStarter:
    """Records each launch call's context for inspection.

    TASK-FORGE-FRR-F010G: production now awaits ``astart_async_task``
    rather than calling sync ``start_async_task``, so this fake exposes
    both methods. They share a single counter so tests can stay
    method-agnostic.
    """

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    _counter: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _mint(
        self,
        subagent_name: str,
        context: Mapping[str, Any],
    ) -> str:
        with self._lock:
            self._counter += 1
            task_id = f"task-{self._counter:04d}"
            # Preserve the in-process emitter object identity by storing
            # the live context reference rather than a deep copy.
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


@dataclass
class FakeStageLogRecorder:
    calls: list[tuple[str, str, StageClass, dict[str, Any]]] = field(
        default_factory=list
    )

    def record_running(
        self,
        build_id: str,
        feature_id: str,
        stage: StageClass,
        details_json: Mapping[str, Any],
    ) -> None:
        self.calls.append((build_id, feature_id, stage, dict(details_json)))


@dataclass
class FakeStateChannel:
    calls: list[dict[str, Any]] = field(default_factory=list)

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
            {
                "build_id": build_id,
                "feature_id": feature_id,
                "task_id": task_id,
                "correlation_id": correlation_id,
                "lifecycle": lifecycle,
                "wave_index": wave_index,
                "task_index": task_index,
            }
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def reader() -> FakeStageLogReader:
    return FakeStageLogReader()


@pytest.fixture
def builder(reader: FakeStageLogReader) -> ForwardContextBuilder:
    allowlist = FakeWorktreeAllowlist(roots_by_build={"build-1": "/work/build-1"})
    return ForwardContextBuilder(
        stage_log_reader=reader, worktree_allowlist=allowlist
    )


@pytest.fixture
def starter() -> CapturingAsyncTaskStarter:
    return CapturingAsyncTaskStarter()


@pytest.fixture
def stage_log() -> FakeStageLogRecorder:
    return FakeStageLogRecorder()


@pytest.fixture
def state_channel() -> FakeStateChannel:
    return FakeStateChannel()


@pytest.fixture
def fake_emitter() -> MagicMock:
    """A stand-in :class:`PipelineLifecycleEmitter` with a tracked identity."""
    return MagicMock(name="PipelineLifecycleEmitter")


# ---------------------------------------------------------------------------
# AC-005 — exactly five collaborator parameters
# ---------------------------------------------------------------------------


class TestDispatchAutobuildSignature:
    """AC-005 — the five-collaborator parameter contract."""

    def test_signature_has_exactly_five_collaborator_kwargs(self) -> None:
        sig = inspect.signature(dispatch_autobuild_async)
        # Keyword-only parameters after the positional build/feature/correlation
        # triple. The five COLLABORATORS lead; ``branch``/``repo`` are additive
        # DATA kwargs (B4 round-17 DEFECT #19 wire-up) forwarded verbatim from
        # the BuildQueuedPayload — not collaborators, so AC-005's five-
        # collaborator contract is unchanged.
        kw_only = [
            name
            for name, p in sig.parameters.items()
            if p.kind is inspect.Parameter.KEYWORD_ONLY
        ]
        collaborators = [
            "forward_context_builder",
            "async_task_starter",
            "stage_log_recorder",
            "state_channel",
            "lifecycle_emitter",
        ]
        additive_data_kwargs = ["branch", "repo"]
        assert kw_only == collaborators + additive_data_kwargs, (
            f"dispatch_autobuild_async must expose exactly the five collaborator "
            f"parameters {collaborators!r} followed only by the additive data "
            f"kwargs {additive_data_kwargs!r}; got {kw_only!r}"
        )
        # The additive kwargs are pure data (default None), never collaborators.
        for name in additive_data_kwargs:
            assert sig.parameters[name].default is None

    def test_lifecycle_emitter_defaults_to_none(self) -> None:
        sig = inspect.signature(dispatch_autobuild_async)
        assert sig.parameters["lifecycle_emitter"].default is None


# ---------------------------------------------------------------------------
# AC-001 — emitter is threaded into the start_async_task context
# ---------------------------------------------------------------------------


class TestEmitterThreadedIntoContext:
    """AC-001 + DDR-007 Option A seam test."""

    @pytest.mark.asyncio
    async def test_dispatch_threads_emitter_into_ctx(
        self,
        builder: ForwardContextBuilder,
        starter: CapturingAsyncTaskStarter,
        stage_log: FakeStageLogRecorder,
        state_channel: FakeStateChannel,
        fake_emitter: MagicMock,
    ) -> None:
        # Pre-stage an approved feature-plan so the builder returns a
        # non-empty context and dispatch proceeds through every step.
        builder._reader.entries[  # type: ignore[attr-defined]
            ("build-1", StageClass.FEATURE_PLAN, "FEAT-1")
        ] = ApprovedStageEntry(
            gate_decision="approved",
            artefact_paths=("/work/build-1/plans/feature-plan-FEAT-1.md",),
            artefact_text=None,
        )

        handle = await dispatch_autobuild_async(
            build_id="build-1",
            feature_id="FEAT-1",
            correlation_id="corr-1",
            forward_context_builder=builder,
            async_task_starter=starter,
            stage_log_recorder=stage_log,
            state_channel=state_channel,
            lifecycle_emitter=fake_emitter,
        )

        # The dispatcher returns the handle synchronously…
        assert handle.task_id.startswith("task-")
        # …and the launched task's context carries the emitter by
        # identity (Option A — in-process Python object).
        assert len(starter.calls) == 1
        subagent_name, ctx = starter.calls[0]
        assert subagent_name == AUTOBUILD_RUNNER_NAME
        assert ctx["lifecycle_emitter"] is fake_emitter, (
            "dispatch_autobuild_async must thread the emitter into the "
            "subagent's context payload per DDR-007 Option A"
        )
        # Sanity — the existing context fields are still threaded.
        assert ctx["build_id"] == "build-1"
        assert ctx["feature_id"] == "FEAT-1"
        assert ctx["correlation_id"] == "corr-1"

    @pytest.mark.asyncio
    async def test_state_channel_still_initialised_with_starting_lifecycle(
        self,
        builder: ForwardContextBuilder,
        starter: CapturingAsyncTaskStarter,
        stage_log: FakeStageLogRecorder,
        state_channel: FakeStateChannel,
        fake_emitter: MagicMock,
    ) -> None:
        await dispatch_autobuild_async(
            build_id="build-1",
            feature_id="FEAT-1",
            correlation_id="corr-1",
            forward_context_builder=builder,
            async_task_starter=starter,
            stage_log_recorder=stage_log,
            state_channel=state_channel,
            lifecycle_emitter=fake_emitter,
        )
        assert state_channel.calls, "state-channel entry must be initialised"
        assert state_channel.calls[0]["lifecycle"] == AUTOBUILD_STARTING_LIFECYCLE


# ---------------------------------------------------------------------------
# AC-004 — build_supervisor wires the emitter + 4 wave-2 collaborators
# ---------------------------------------------------------------------------


def _supervisor_kwargs(**overrides: Any) -> dict[str, Any]:
    """Return a minimal kwargs dict for :func:`build_supervisor`."""
    base: dict[str, Any] = {
        "ordering_guard": StageOrderingGuard(),
        "per_feature_sequencer": PerFeatureLoopSequencer(),
        "constitutional_guard": ConstitutionalGuard(),
        "state_reader": MagicMock(name="StateMachineReader"),
        "ordering_stage_log_reader": MagicMock(name="OrderingStageLogReader"),
        "per_feature_stage_log_reader": MagicMock(name="PerFeatureStageLogReader"),
        "async_task_reader": MagicMock(name="AsyncTaskReader"),
        "reasoning_model": MagicMock(name="ReasoningModelPort"),
        "turn_recorder": MagicMock(name="StageLogTurnRecorder"),
        "specialist_dispatcher": MagicMock(name="SpecialistDispatcher"),
        "subprocess_dispatcher": MagicMock(name="SubprocessDispatcher"),
        "pr_review_gate": MagicMock(name="PRReviewGate"),
    }
    base.update(overrides)
    return base


class TestBuildSupervisorWiring:
    """AC-004 — production factory wires the emitter + 4 wave-2 collaborators."""

    def test_supervisor_receives_lifecycle_emitter(
        self,
        builder: ForwardContextBuilder,
        starter: CapturingAsyncTaskStarter,
        stage_log: FakeStageLogRecorder,
        state_channel: FakeStateChannel,
        fake_emitter: MagicMock,
    ) -> None:
        sup = build_supervisor(
            forward_context_builder=builder,
            async_task_starter=starter,
            stage_log_recorder=stage_log,
            state_channel=state_channel,
            lifecycle_emitter=fake_emitter,
            async_subagent_middleware=MagicMock(tools=[]),
            **_supervisor_kwargs(),
        )
        assert isinstance(sup, Supervisor)
        # AC-004: supervisor carries the same emitter object identity.
        assert sup.lifecycle_emitter is fake_emitter

    def test_factory_does_not_construct_a_second_emitter(
        self,
        builder: ForwardContextBuilder,
        starter: CapturingAsyncTaskStarter,
        stage_log: FakeStageLogRecorder,
        state_channel: FakeStateChannel,
        fake_emitter: MagicMock,
    ) -> None:
        # AC-004 invariant — only the caller-supplied emitter is wired.
        sup = build_supervisor(
            forward_context_builder=builder,
            async_task_starter=starter,
            stage_log_recorder=stage_log,
            state_channel=state_channel,
            lifecycle_emitter=fake_emitter,
            async_subagent_middleware=MagicMock(tools=[]),
            **_supervisor_kwargs(),
        )
        # The dispatcher closure exposes the bound emitter for diagnostics.
        bound_emitter = getattr(
            sup.autobuild_dispatcher, "__wrapped_emitter__", None
        )
        assert bound_emitter is fake_emitter

    @pytest.mark.asyncio
    async def test_dispatcher_closure_threads_emitter_to_dispatch(
        self,
        builder: ForwardContextBuilder,
        starter: CapturingAsyncTaskStarter,
        stage_log: FakeStageLogRecorder,
        state_channel: FakeStateChannel,
        fake_emitter: MagicMock,
    ) -> None:
        # Pre-stage an approved feature-plan so the builder returns a
        # non-empty context.
        builder._reader.entries[  # type: ignore[attr-defined]
            ("build-1", StageClass.FEATURE_PLAN, "FEAT-1")
        ] = ApprovedStageEntry(
            gate_decision="approved",
            artefact_paths=("/work/build-1/plans/feature-plan-FEAT-1.md",),
            artefact_text=None,
        )
        dispatcher = _make_autobuild_dispatcher_closure(
            forward_context_builder=builder,
            async_task_starter=starter,
            stage_log_recorder=stage_log,
            state_channel=state_channel,
            lifecycle_emitter=fake_emitter,
        )
        await dispatcher(
            build_id="build-1", feature_id="FEAT-1", rationale="r"
        )
        assert len(starter.calls) == 1
        _name, ctx = starter.calls[0]
        assert ctx["lifecycle_emitter"] is fake_emitter


class TestBuildSupervisorBudgetPassThrough:
    """FEAT-UBS-002 — build_supervisor threads every budget DI kwarg onto the
    Supervisor, and the defaults keep the guard a strict no-op (inert until
    activation)."""

    def test_budget_kwargs_default_to_supervisor_defaults(
        self,
        builder: ForwardContextBuilder,
        starter: CapturingAsyncTaskStarter,
        stage_log: FakeStageLogRecorder,
        state_channel: FakeStateChannel,
        fake_emitter: MagicMock,
    ) -> None:
        # No budget kwargs supplied — every field lands on the dataclass
        # default so the guard is a no-op (ASSUM-010) and existing callers are
        # unchanged.
        sup = build_supervisor(
            forward_context_builder=builder,
            async_task_starter=starter,
            stage_log_recorder=stage_log,
            state_channel=state_channel,
            lifecycle_emitter=fake_emitter,
            async_subagent_middleware=MagicMock(tools=[]),
            **_supervisor_kwargs(),
        )
        assert sup.budget_guards is None
        assert sup.budget_profile_name == "attended"
        assert sup.budget_wall_clock is None
        assert sup.budget_started_at_reader is None
        assert sup.budget_coach_score_reader is None
        assert sup.budget_pause is None

    def test_budget_kwargs_pass_through_by_identity(
        self,
        builder: ForwardContextBuilder,
        starter: CapturingAsyncTaskStarter,
        stage_log: FakeStageLogRecorder,
        state_channel: FakeStateChannel,
        fake_emitter: MagicMock,
    ) -> None:
        guards = MagicMock(name="BudgetGuards")
        wall_clock = MagicMock(name="budget_wall_clock")
        started_at_reader = MagicMock(name="budget_started_at_reader")
        coach_score_reader = MagicMock(name="budget_coach_score_reader")
        pause = MagicMock(name="budget_pause")

        sup = build_supervisor(
            forward_context_builder=builder,
            async_task_starter=starter,
            stage_log_recorder=stage_log,
            state_channel=state_channel,
            lifecycle_emitter=fake_emitter,
            async_subagent_middleware=MagicMock(tools=[]),
            budget_guards=guards,
            budget_profile_name="unattended",
            budget_wall_clock=wall_clock,
            budget_started_at_reader=started_at_reader,
            budget_coach_score_reader=coach_score_reader,
            budget_pause=pause,
            **_supervisor_kwargs(),
        )
        # Every kwarg reaches the Supervisor by object identity — no rewrapping.
        assert sup.budget_guards is guards
        assert sup.budget_profile_name == "unattended"
        assert sup.budget_wall_clock is wall_clock
        assert sup.budget_started_at_reader is started_at_reader
        assert sup.budget_coach_score_reader is coach_score_reader
        assert sup.budget_pause is pause


# ---------------------------------------------------------------------------
# AC-002 — supervisor's tool list includes AsyncSubAgentMiddleware tools
# ---------------------------------------------------------------------------


class _FakeStructuredTool:
    """Stand-in for a langchain ``StructuredTool`` carrying just the name."""

    def __init__(self, name: str) -> None:
        self.name = name


class TestAsyncSubAgentToolListExposed:
    """AC-002 — supervisor exposes the five middleware tool surface."""

    def test_supervisor_tools_contain_async_subagent_tool_names(
        self,
        builder: ForwardContextBuilder,
        starter: CapturingAsyncTaskStarter,
        stage_log: FakeStageLogRecorder,
        state_channel: FakeStateChannel,
        fake_emitter: MagicMock,
    ) -> None:
        expected_names = {
            "start_async_task",
            "check_async_task",
            "update_async_task",
            "cancel_async_task",
            "list_async_tasks",
        }
        fake_middleware = MagicMock(
            tools=[_FakeStructuredTool(n) for n in expected_names]
        )
        sup = build_supervisor(
            forward_context_builder=builder,
            async_task_starter=starter,
            stage_log_recorder=stage_log,
            state_channel=state_channel,
            lifecycle_emitter=fake_emitter,
            async_subagent_middleware=fake_middleware,
            **_supervisor_kwargs(),
        )
        actual_names = {t.name for t in sup.tools}
        assert expected_names <= actual_names, (
            f"Supervisor.tools must include the AsyncSubAgentMiddleware "
            f"tool surface {expected_names!r}; got {actual_names!r}"
        )

    def test_default_middleware_factory_emits_five_tools(self) -> None:
        # Importing the default middleware factory and confirming it
        # produces the canonical five-tool surface so AC-002 holds when
        # the production wiring builds its own middleware (no fake).
        deepagents = pytest.importorskip("deepagents.middleware.async_subagents")
        from forge.cli.serve import _build_async_subagent_middleware

        middleware = _build_async_subagent_middleware()
        tool_names = {getattr(t, "name", None) for t in middleware.tools}
        assert tool_names == {
            "start_async_task",
            "check_async_task",
            "update_async_task",
            "cancel_async_task",
            "list_async_tasks",
        }
        assert isinstance(middleware, deepagents.AsyncSubAgentMiddleware)


# ---------------------------------------------------------------------------
# AC-003 — supervisor stays responsive while autobuild's async task is in flight
# ---------------------------------------------------------------------------


@dataclass
class _ResponsiveStateReader:
    """A state reader that returns RUNNING for every build_id queried."""

    queries: list[str] = field(default_factory=list)

    def get_build_state(self, build_id: str) -> BuildState:
        self.queries.append(build_id)
        return BuildState.RUNNING


@dataclass
class _ResponsiveOrderingReader:
    """Ordering reader returning an empty permitted set + empty catalogue.

    Empty permitted set forces the supervisor down the WAITING branch —
    which is precisely the "answers status queries" path: when autobuild
    is already running the per-feature sequencer keeps the permitted
    set empty for AUTOBUILD, so the next supervisor turn returns WAITING
    rather than blocking on the in-flight async task.
    """

    def is_approved(
        self, build_id: str, stage: StageClass, feature_id: str | None = None
    ) -> bool:
        return False

    def latest_state(
        self, build_id: str, stage: StageClass, feature_id: str | None = None
    ) -> str | None:
        return None

    def feature_catalogue(self, build_id: str) -> tuple[str, ...]:
        return ()


@dataclass
class _RecordingTurnRecorder:
    rows: list[tuple[str, TurnOutcome]] = field(default_factory=list)

    def record_turn(
        self,
        *,
        build_id: str,
        outcome: TurnOutcome,
        permitted_stages: Any,
        chosen_stage: Any,
        chosen_feature_id: Any,
        rationale: str,
        gate_verdict: Any,
    ) -> None:
        self.rows.append((build_id, outcome))


@dataclass
class _NoOpReasoningModel:
    """Reasoning model that always declines to dispatch."""

    def choose_dispatch(self, **kwargs: Any) -> DispatchChoice | None:
        return None


class TestSupervisorRemainsResponsive:
    """AC-003 / FEAT-FORGE-007 Group A — supervisor answers while autobuild runs."""

    @pytest.mark.asyncio
    async def test_next_turn_returns_waiting_while_autobuild_in_flight(
        self,
        builder: ForwardContextBuilder,
        starter: CapturingAsyncTaskStarter,
        stage_log: FakeStageLogRecorder,
        state_channel: FakeStateChannel,
        fake_emitter: MagicMock,
    ) -> None:
        """The supervisor's next_turn must NOT await the in-flight async task.

        We dispatch an autobuild via the closure (which awaits
        ``astart_async_task`` and returns immediately because the
        deepagents middleware's launch contract is "submit and return")
        and then invoke ``next_turn`` on a fresh build to assert the
        reasoning loop completes its turn without waiting on the
        autobuild's async work — the FEAT-FORGE-007 Group A scenario
        ("supervisor stays responsive during long runs").

        TASK-FORGE-FRR-F010G: the dispatcher closure is now ``async def``
        because :func:`dispatch_autobuild_async` is async — the
        responsiveness contract is unchanged (the launch awaits, but the
        runner's body does not), only the dispatch idiom changed.
        """
        # Pre-stage feature plan so the dispatcher proceeds.
        builder._reader.entries[  # type: ignore[attr-defined]
            ("build-1", StageClass.FEATURE_PLAN, "FEAT-1")
        ] = ApprovedStageEntry(
            gate_decision="approved",
            artefact_paths=("/work/build-1/plans/feature-plan-FEAT-1.md",),
            artefact_text=None,
        )

        kwargs = _supervisor_kwargs(
            state_reader=_ResponsiveStateReader(),
            ordering_stage_log_reader=_ResponsiveOrderingReader(),
            reasoning_model=_NoOpReasoningModel(),
            turn_recorder=_RecordingTurnRecorder(),
        )
        # Use the real ordering guard so its query path executes; the
        # reader returns empty so permitted_stages stays empty.
        sup = build_supervisor(
            forward_context_builder=builder,
            async_task_starter=starter,
            stage_log_recorder=stage_log,
            state_channel=state_channel,
            lifecycle_emitter=fake_emitter,
            async_subagent_middleware=MagicMock(tools=[]),
            **kwargs,
        )

        # 1. Dispatch the autobuild; the closure awaits the launch and
        #    returns the handle. start_async_task is non-blocking on the
        #    runner's body by contract.
        handle = await sup.autobuild_dispatcher(
            build_id="build-1", feature_id="FEAT-1", rationale="initial"
        )
        assert handle.task_id.startswith("task-")
        assert len(starter.calls) == 1

        # 2. While that "task" is in flight, run a supervisor turn for a
        #    different build and assert it returns WAITING (i.e. the
        #    reasoning loop kept its turn budget — it did not block).
        report = await asyncio.wait_for(sup.next_turn("build-2"), timeout=2.0)
        # AC-003: the supervisor's reasoning loop COMPLETED its turn
        # without blocking on the in-flight autobuild's async task.
        # Either WAITING (no permitted stages) or NO_OP (reasoning model
        # declined) is acceptable — both are non-blocking outcomes that
        # answer the status query.
        assert report.outcome in (TurnOutcome.WAITING, TurnOutcome.NO_OP)
        assert report.build_id == "build-2"

    @pytest.mark.asyncio
    async def test_supervisor_dispatcher_closure_is_async(
        self,
        builder: ForwardContextBuilder,
        starter: CapturingAsyncTaskStarter,
        stage_log: FakeStageLogRecorder,
        state_channel: FakeStateChannel,
        fake_emitter: MagicMock,
    ) -> None:
        """The autobuild dispatcher closure is ``async def``.

        TASK-FORGE-FRR-F010G: the closure was sync pre-F010G; it is now
        async because :func:`dispatch_autobuild_async` is async (the
        launch path awaits the deepagents middleware's
        ``StructuredTool.coroutine`` so the autobuild_runner registration
        can stay URL-less / in-process).

        The Group A responsiveness invariant ("the runner's body does
        not block the supervisor") is unchanged — the launch contract is
        still "await the submit, return immediately"; only the call
        idiom changed from sync to async.
        """
        builder._reader.entries[  # type: ignore[attr-defined]
            ("build-1", StageClass.FEATURE_PLAN, "FEAT-1")
        ] = ApprovedStageEntry(
            gate_decision="approved",
            artefact_paths=("/work/build-1/plans/feature-plan-FEAT-1.md",),
            artefact_text=None,
        )
        dispatcher = _make_autobuild_dispatcher_closure(
            forward_context_builder=builder,
            async_task_starter=starter,
            stage_log_recorder=stage_log,
            state_channel=state_channel,
            lifecycle_emitter=fake_emitter,
        )
        # The closure is async; calling it produces a coroutine that
        # resolves to the handle when awaited.
        coro = dispatcher(build_id="build-1", feature_id="FEAT-1", rationale="")
        assert inspect.iscoroutine(coro)
        result = await coro
        assert result.task_id.startswith("task-")

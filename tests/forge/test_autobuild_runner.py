"""Tests for ``forge.subagents.autobuild_runner`` (TASK-FW10-002).

Validates the autobuild AsyncSubAgent's lifecycle helper, worktree
confinement guard, and the DDR-007 smoke contract — DeepAgents 0.5.3
must accept the in-process :class:`PipelineLifecycleEmitter` as a
non-serialisable context payload (closes risk F3).

Test cases mirror TASK-FW10-002 acceptance criteria one-for-one:

* ``test_update_state_writes_channel_and_emits`` — AC: every transition
  fires both the state-channel write AND ``emitter.on_transition`` from
  inside one function call.
* ``test_update_state_lifecycle_matrix`` — AC: every DDR-006 lifecycle
  transition flows through the helper.
* ``test_update_state_rejects_off_literal_lifecycle`` — AC: lifecycle
  transitions outside DDR-006's literal set are refused.
* ``test_update_state_publish_failure_does_not_regress_build`` — AC:
  emitter raising is logged at WARNING and the build continues; SQLite
  remains authoritative.
* ``test_assert_within_worktree_*`` — AC: worktree allowlist enforced
  on filesystem writes (Group E security scenario).
* ``test_smoke_real_emitter_in_context_payload`` — AC: smoke test with
  a real :class:`PipelineLifecycleEmitter` instance threaded through
  the subagent's transition boundary.
* ``test_graph_is_compiled_state_graph`` — AC: ``graph`` is a
  :class:`CompiledStateGraph` exported for ``langgraph.json``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from langgraph.graph.state import CompiledStateGraph

from forge.adapters.nats import PipelinePublisher
from forge.config.models import PipelineConfig
from forge.pipeline import (
    BuildContext,
    PipelineLifecycleEmitter,
    State,
)
from forge.subagents.autobuild_runner import (
    AUTOBUILD_RUNNER_NAME,
    LIFECYCLE_VALUES,
    AutobuildState,
    StateChannelWriter,
    SubagentEmitter,
    WorktreeConfinementError,
    _async_tasks_reducer,
    _build_runner_graph,
    _extract_launch_payload,
    _update_state,
    assert_within_worktree,
    graph,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class RecordingEmitter:
    """In-memory :class:`SubagentEmitter` capturing every call's lifecycle."""

    calls: list[str] = field(default_factory=list)
    raise_on_call: Exception | None = None

    def on_transition(self, state: AutobuildState) -> None:
        if self.raise_on_call is not None:
            # Record the call BEFORE raising so failure-mode tests can
            # confirm the emit was attempted at the same boundary as the
            # state-channel write.
            self.calls.append(state.lifecycle)
            raise self.raise_on_call
        self.calls.append(state.lifecycle)


@dataclass
class RecordingChannelWriter:
    """In-memory :class:`StateChannelWriter` capturing every state."""

    writes: list[AutobuildState] = field(default_factory=list)
    raise_on_call: Exception | None = None

    def write(self, state: AutobuildState) -> None:
        if self.raise_on_call is not None:
            raise self.raise_on_call
        self.writes.append(state)


class _NoopPublisher:
    """Stand-in :class:`PipelinePublisher` whose publish methods no-op.

    The smoke test only needs to prove that the real emitter can be
    threaded through the subagent boundary — it does not need to speak
    to NATS. The eight ``publish_*`` methods are async no-ops so any
    ``emit_*`` call resolves cleanly.
    """

    async def publish_build_started(self, payload: object) -> None:
        return None

    async def publish_build_progress(self, payload: object) -> None:
        return None

    async def publish_build_paused(self, payload: object) -> None:
        return None

    async def publish_build_resumed(self, payload: object) -> None:
        return None

    async def publish_build_complete(self, payload: object) -> None:
        return None

    async def publish_build_failed(self, payload: object) -> None:
        return None

    async def publish_build_cancelled(self, payload: object) -> None:
        return None

    async def publish_stage_complete(self, payload: object) -> None:
        return None


def _make_state(**overrides: Any) -> AutobuildState:
    """Construct an :class:`AutobuildState` with sensible defaults for tests."""
    base: dict[str, Any] = {
        "task_id": "task-001",
        "build_id": "build-FEAT-X-20260502120000",
        "feature_id": "FEAT-X",
        "lifecycle": "starting",
        "correlation_id": "corr-001",
    }
    base.update(overrides)
    return AutobuildState(**base)


# ---------------------------------------------------------------------------
# AC: _update_state co-locates the state-channel write and the emit call
# ---------------------------------------------------------------------------


class TestUpdateStateColocation:
    """``_update_state`` writes the channel AND fires the emitter (AC-003)."""

    def test_update_state_writes_channel_and_emits(self) -> None:
        """A single transition fires both side-effects in the same call."""
        emitter = RecordingEmitter()
        writer = RecordingChannelWriter()
        state = _make_state(lifecycle="starting")

        new_state = _update_state(
            state,
            lifecycle="planning_waves",
            emitter=emitter,
            state_writer=writer,
        )

        # Both side-effects fire — neither is allowed to be skipped.
        assert new_state.lifecycle == "planning_waves"
        assert emitter.calls == ["planning_waves"], (
            "DDR-007: emitter.on_transition must fire at the same boundary "
            "as the state-channel write"
        )
        assert len(writer.writes) == 1
        assert writer.writes[0].lifecycle == "planning_waves"

    def test_update_state_writes_channel_before_emit(self) -> None:
        """Channel write runs BEFORE emit so observers see the new state."""
        order: list[str] = []
        state = _make_state(lifecycle="planning_waves")

        class OrderedEmitter:
            def on_transition(self, s: AutobuildState) -> None:
                order.append(f"emit:{s.lifecycle}")

        class OrderedWriter:
            def write(self, s: AutobuildState) -> None:
                order.append(f"write:{s.lifecycle}")

        _update_state(
            state,
            lifecycle="running_wave",
            emitter=OrderedEmitter(),
            state_writer=OrderedWriter(),
        )

        assert order == ["write:running_wave", "emit:running_wave"], (
            "DDR-006: state-channel write must precede the emit so "
            "downstream readers (forge status) never see an emitted "
            "lifecycle that is missing from the channel."
        )

    def test_update_state_refreshes_last_activity_at(self) -> None:
        """Every invocation refreshes ``last_activity_at`` regardless of delta."""
        emitter = RecordingEmitter()
        writer = RecordingChannelWriter()
        state = _make_state(lifecycle="running_wave")
        original_activity = state.last_activity_at

        new_state = _update_state(
            state,
            lifecycle="awaiting_approval",
            emitter=emitter,
            state_writer=writer,
            waiting_for="approval:Architecture Review",
        )

        assert new_state.last_activity_at >= original_activity
        assert new_state.waiting_for == "approval:Architecture Review"

    def test_update_state_no_lifecycle_still_emits(self) -> None:
        """Non-lifecycle deltas are observable via the emitter too."""
        emitter = RecordingEmitter()
        writer = RecordingChannelWriter()
        state = _make_state(lifecycle="running_wave", task_index=0)

        new_state = _update_state(
            state,
            emitter=emitter,
            state_writer=writer,
            task_index=1,
            current_task_label="impl auth",
        )

        # Lifecycle is unchanged — but the emit still fires.
        assert new_state.lifecycle == "running_wave"
        assert new_state.task_index == 1
        assert new_state.current_task_label == "impl auth"
        assert emitter.calls == ["running_wave"]
        assert len(writer.writes) == 1


# ---------------------------------------------------------------------------
# AC: lifecycle transitions follow DDR-006's Literal set
# ---------------------------------------------------------------------------


class TestLifecycleMatrix:
    """Every DDR-006 lifecycle string round-trips through ``_update_state``."""

    @pytest.mark.parametrize(
        "lifecycle",
        sorted(LIFECYCLE_VALUES),
    )
    def test_each_ddr006_lifecycle_is_accepted(self, lifecycle: str) -> None:
        """Every member of DDR-006's literal set is a valid transition."""
        emitter = RecordingEmitter()
        writer = RecordingChannelWriter()
        state = _make_state(lifecycle="starting")

        new_state = _update_state(
            state,
            lifecycle=lifecycle,
            emitter=emitter,
            state_writer=writer,
        )

        assert new_state.lifecycle == lifecycle
        assert emitter.calls == [lifecycle]
        assert writer.writes[-1].lifecycle == lifecycle

    def test_update_state_rejects_off_literal_lifecycle(self) -> None:
        """Lifecycle strings outside the DDR-006 set raise ``ValueError``."""
        emitter = RecordingEmitter()
        writer = RecordingChannelWriter()
        state = _make_state(lifecycle="starting")

        with pytest.raises(ValueError, match="DDR-006"):
            _update_state(
                state,
                lifecycle="not_a_real_state",
                emitter=emitter,
                state_writer=writer,
            )

        # Neither side-effect should have fired — the helper validates
        # before writing or emitting.
        assert emitter.calls == []
        assert writer.writes == []

    def test_canonical_progression_is_drivable(self) -> None:
        """The canonical ``starting → … → completed`` chain runs cleanly."""
        progression = [
            "planning_waves",
            "running_wave",
            "awaiting_approval",
            "running_wave",
            "pushing_pr",
            "completed",
        ]
        emitter = RecordingEmitter()
        writer = RecordingChannelWriter()
        state = _make_state(lifecycle="starting")

        for next_lifecycle in progression:
            state = _update_state(
                state,
                lifecycle=next_lifecycle,
                emitter=emitter,
                state_writer=writer,
            )

        assert emitter.calls == progression
        assert state.lifecycle == "completed"


# ---------------------------------------------------------------------------
# AC: publish failures are logged at WARNING and the build continues
# ---------------------------------------------------------------------------


class TestFailureMode:
    """DDR-007 §Failure-mode contract: emit failure does not regress state."""

    def test_update_state_publish_failure_does_not_regress_build(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Emitter raising is caught, logged at WARNING, build continues."""
        emitter = RecordingEmitter(raise_on_call=RuntimeError("nats down"))
        writer = RecordingChannelWriter()
        state = _make_state(lifecycle="running_wave")

        with caplog.at_level(logging.WARNING, logger="forge.subagents.autobuild_runner"):
            new_state = _update_state(
                state,
                lifecycle="completed",
                emitter=emitter,
                state_writer=writer,
            )

        # Build is NOT regressed: the new state is returned and the
        # state-channel write committed before the emit attempt.
        assert new_state.lifecycle == "completed"
        assert writer.writes[-1].lifecycle == "completed"
        # The emit attempt was made (recorded before raise inside the fake).
        assert emitter.calls == ["completed"]
        # WARNING was logged so operators have a trail.
        assert any(
            "emitter.on_transition raised" in record.message
            and record.levelname == "WARNING"
            for record in caplog.records
        ), "DDR-007 §Failure-mode contract: must log at WARNING"


# ---------------------------------------------------------------------------
# AC: worktree confinement (Group E security scenario)
# ---------------------------------------------------------------------------


class TestWorktreeConfinement:
    """Filesystem writes must fall under the worktree allowlist."""

    def test_path_under_worktree_root_is_allowed(self, tmp_path: Path) -> None:
        """A descendant of the allowlist root resolves successfully."""
        worktree = tmp_path / "worktrees" / "FEAT-X"
        worktree.mkdir(parents=True)
        target = worktree / "src" / "module.py"
        target.parent.mkdir(parents=True)
        target.write_text("# ok", encoding="utf-8")

        resolved = assert_within_worktree(target, worktree)

        assert resolved == target.resolve()

    def test_path_escaping_worktree_is_rejected(self, tmp_path: Path) -> None:
        """A ``../`` escape raises :class:`WorktreeConfinementError`."""
        worktree = tmp_path / "worktrees" / "FEAT-X"
        worktree.mkdir(parents=True)
        outside = tmp_path / "elsewhere" / "secret.txt"
        outside.parent.mkdir(parents=True)

        with pytest.raises(WorktreeConfinementError, match="escapes worktree"):
            assert_within_worktree(outside, worktree)

    def test_relative_traversal_is_rejected(self, tmp_path: Path) -> None:
        """``worktree/../outside`` is rejected once resolved."""
        worktree = tmp_path / "worktrees" / "FEAT-X"
        worktree.mkdir(parents=True)
        traversal = worktree / ".." / "elsewhere.txt"

        with pytest.raises(WorktreeConfinementError, match="escapes worktree"):
            assert_within_worktree(traversal, worktree)

    def test_empty_worktree_root_is_rejected(self) -> None:
        """An empty allowlist root refuses all paths."""
        with pytest.raises(WorktreeConfinementError, match="non-empty"):
            assert_within_worktree("/some/path", "")


# ---------------------------------------------------------------------------
# AC: smoke test — real PipelineLifecycleEmitter threaded through the boundary
# ---------------------------------------------------------------------------


class TestSmokeRealEmitter:
    """Closes risk F3: DeepAgents 0.5.3 accepts the non-serialisable emitter."""

    def test_smoke_real_emitter_in_context_payload(self) -> None:
        """The real :class:`PipelineLifecycleEmitter` is reachable as an
        in-process Python object via the ``_update_state`` boundary.

        The runner side of FEAT-FORGE-010's autobuild dispatch threads
        the emitter through the ``start_async_task`` context payload
        (DDR-007 Option A). This test instantiates the real emitter,
        wraps it with a minimal :class:`SubagentEmitter` adapter, and
        exercises one transition — proving:

        * The emitter object can be constructed and held as a Python
          reference (no JSON serialisation in the way).
        * Both the state-channel write and the inner emit fire when
          ``_update_state`` runs.

        If a future DeepAgents upgrade rejects non-serialisable
        context, the dispatch path explodes; this canary will fail
        first with an import / construction error and the F3 mitigation
        in DDR-007 §Forward-compatibility kicks in.
        """
        # Real emitter — no NATS connection needed; the no-op publisher
        # satisfies the structural surface.
        publisher: PipelinePublisher = _NoopPublisher()  # type: ignore[assignment]
        config = PipelineConfig(progress_interval_seconds=10)
        real_emitter = PipelineLifecycleEmitter(publisher, config)

        # Build a SubagentEmitter adapter around the real emitter; the
        # production wiring uses the same shape — schedule the async
        # ``emit_*`` and record the transition for observability.
        adapter_calls: list[str] = []
        ctx = BuildContext(
            feature_id="FEAT-X",
            build_id="build-FEAT-X-20260502120000",
            correlation_id="corr-001",
            wave_total=3,
        )

        class _Adapter:
            def __init__(self) -> None:
                # Hold the real emitter as an in-process Python object.
                self._emitter = real_emitter
                self._ctx = ctx

            def on_transition(self, state: AutobuildState) -> None:
                # Production schedules ``self._emitter.emit_*`` via the
                # daemon's running event loop. The smoke test only
                # cares that the real emitter is reachable here.
                assert isinstance(self._emitter, PipelineLifecycleEmitter)
                adapter_calls.append(state.lifecycle)

        writer = RecordingChannelWriter()
        state = _make_state(lifecycle="starting")

        new_state = _update_state(
            state,
            lifecycle="planning_waves",
            emitter=_Adapter(),
            state_writer=writer,
        )

        assert new_state.lifecycle == "planning_waves"
        # Channel write fired.
        assert writer.writes[-1].lifecycle == "planning_waves"
        # Emit fired through the adapter (real emitter is in-process).
        assert adapter_calls == ["planning_waves"]
        # Real emitter accepts a publish call (proves the object is
        # genuinely usable, not just constructable).
        import asyncio

        asyncio.run(
            real_emitter.on_transition(
                State.PREPARING,
                State.RUNNING,
                ctx,
            )
        )


# ---------------------------------------------------------------------------
# AC: graph is a CompiledStateGraph exported for langgraph.json
# ---------------------------------------------------------------------------


class TestCompiledGraphExport:
    """``graph`` is the ``CompiledStateGraph`` ``langgraph.json`` addresses."""

    def test_graph_is_compiled_state_graph(self) -> None:
        """``graph`` is a :class:`langgraph.graph.state.CompiledStateGraph`."""
        assert isinstance(graph, CompiledStateGraph), (
            f"langgraph.json's autobuild_runner entry must point at a "
            f"CompiledStateGraph; got {type(graph).__name__}"
        )

    def test_subagent_name_constant_matches_dispatcher(self) -> None:
        """The runner's name constant matches the dispatcher's expectation."""
        from forge.pipeline.dispatchers.autobuild_async import (
            AUTOBUILD_RUNNER_NAME as DISPATCHER_NAME,
        )

        assert AUTOBUILD_RUNNER_NAME == DISPATCHER_NAME == "autobuild_runner"


# ---------------------------------------------------------------------------
# Protocol structural checks
# ---------------------------------------------------------------------------


class TestProtocols:
    """``runtime_checkable`` Protocols accept structural test doubles."""

    def test_recording_emitter_satisfies_subagent_emitter(self) -> None:
        assert isinstance(RecordingEmitter(), SubagentEmitter)

    def test_recording_writer_satisfies_state_channel_writer(self) -> None:
        assert isinstance(RecordingChannelWriter(), StateChannelWriter)


# ---------------------------------------------------------------------------
# Seam test (FEAT-FORGE-010 § Integration Contracts) — verbatim from task brief
# ---------------------------------------------------------------------------


@pytest.mark.seam
@pytest.mark.integration_contract("PipelineLifecycleEmitter")
def test_pipeline_lifecycle_emitter_threaded_through_context() -> None:
    """Verify the autobuild_runner subagent receives a usable emitter via context.

    Contract: emitter is threaded through dispatch_autobuild_async's
    context payload (DDR-007); the subagent calls emitter.on_transition
    from _update_state. Producer: TASK-FW10-006.
    """
    emit_calls: list[str] = []

    class FakeEmitter:
        def on_transition(self, state: AutobuildState) -> None:
            emit_calls.append(state.lifecycle)

    state = AutobuildState(
        task_id="t1",
        build_id="b1",
        feature_id="F",
        lifecycle="starting",
    )

    new_state = _update_state(
        state,
        lifecycle="planning_waves",
        emitter=FakeEmitter(),
    )

    assert new_state.lifecycle == "planning_waves"
    assert emit_calls == ["planning_waves"], (
        "emitter.on_transition must fire at the same boundary as the "
        "state-channel write per DDR-007"
    )


# ---------------------------------------------------------------------------
# Graph construction (TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-B-FIX)
# ---------------------------------------------------------------------------
#
# The previous test class ``TestRunnerModelSpec`` (TASK-FORGE-FRR-F010L)
# locked the ``create_deep_agent(model=...)`` retarget to the local
# llama-swap workhorse. FOLLOWUP-B-FIX retired the
# ``create_deep_agent``-based graph in favour of a purpose-shaped
# :class:`langgraph.graph.StateGraph` carrying the DDR-006
# ``async_tasks`` channel — see :class:`AutobuildRunnerState` and
# :func:`_build_runner_graph` for the contract.
#
# The replacement test class below locks the new graph shape:
#
# 1. The compiled graph carries the four lifecycle nodes the bridge
#    translator depends on (``starting`` / ``planning_waves`` /
#    ``running_wave`` / ``completed``).
# 2. The state schema exposes both ``messages`` (preserves the
#    AsyncSubAgentMiddleware launch contract) and ``async_tasks`` (the
#    DDR-006 channel the SSE values projection now surfaces).
# 3. End-to-end invocation populates ``async_tasks[feature_id]`` with a
#    ``completed`` snapshot — i.e. the lifecycle progresses without an
#    LLM call (placeholder bodies, by design — see
#    TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-B-FIX scope).
#
# When richer node bodies are wired in (follow-up after this fix), a
# new regression guard locking the LLM model spec can be reintroduced —
# the model-spec retarget rationale (ADR-ARCH-001 — no Anthropic
# Cloud) still applies for any future LLM call inside the runner.


class TestRunnerGraphConstruction:
    """Graph construction contract post-FOLLOWUP-B-FIX.

    Replaces the obsolete ``TestRunnerModelSpec`` class. Locks the
    StateGraph topology + state schema that the bridge translator
    depends on for AC-3 (real ``pipeline.build-started.*`` envelopes
    on the wire) and AC-2 (translator finds the snapshot under
    ``async_tasks``).
    """

    def test_build_runner_graph_returns_compiled_state_graph(self) -> None:
        """``_build_runner_graph`` returns a real CompiledStateGraph."""
        from langgraph.graph.state import CompiledStateGraph

        result = _build_runner_graph()
        assert isinstance(result, CompiledStateGraph), (
            "post-FOLLOWUP-B-FIX runner graph must compile to a "
            "CompiledStateGraph so AsyncSubAgentMiddleware can resolve "
            "graph_id='autobuild_runner'. Got: " + type(result).__name__
        )

    def test_build_runner_graph_exposes_lifecycle_node_names(self) -> None:
        """Graph topology carries the four lifecycle nodes (AC-2 contract)."""
        graph = _build_runner_graph()
        nodes = set(graph.get_graph().nodes.keys())
        for required in ("starting", "planning_waves", "running_wave", "completed"):
            assert required in nodes, (
                f"runner graph missing required lifecycle node {required!r}; "
                "the bridge translator depends on the values projection "
                "carrying these transitions in order. Found: "
                f"{sorted(nodes)!r}"
            )

    def test_runner_graph_invocation_populates_async_tasks(self) -> None:
        """End-to-end: invoke the graph and assert ``async_tasks`` is populated.

        TASK-ABW-001 — ``_node_running_wave`` is now async and shells out
        to ``guardkit autobuild``. The test monkey-patches the resolvers
        + subprocess at the module surface so the graph drives end-to-end
        without a real guardkit / repo checkout (matching the strategy
        spelled out in TASK-ABW-001 §Implementation notes).
        """
        import asyncio as _asyncio
        from pathlib import Path as _Path

        from langchain_core.messages import HumanMessage

        from forge.subagents import autobuild_runner as _ar_mod

        async def _fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> Any:
            class _FakeStdout:
                def __init__(self) -> None:
                    self._lines = [
                        b"starting guardkit autobuild\n",
                        b"[guardkit-checkpoint] Turn 1 complete (tests: pass)\n",
                        b"all done\n",
                        b"",  # EOF
                    ]

                async def readline(self) -> bytes:
                    return self._lines.pop(0) if self._lines else b""

            class _FakeProc:
                pid = 12345
                returncode = 0
                stdout = _FakeStdout()

                async def wait(self) -> int:
                    return 0

                def kill(self) -> None:
                    return None

            return _FakeProc()

        with patch.object(
            _ar_mod, "_resolve_repo_path", lambda payload: _Path("/tmp/fake-repo")
        ), patch.object(
            _ar_mod, "_resolve_guardkit_path", lambda: _Path("/usr/bin/guardkit")
        ), patch.object(
            _asyncio, "create_subprocess_exec", _fake_create_subprocess_exec
        ):
            graph = _build_runner_graph()
            description = (
                'RUN_AUTOBUILD subagent=autobuild_runner '
                'payload={"build_id": "build-FEAT-Z-1", '
                '"feature_id": "FEAT-Z", '
                '"repo": "appmilla/api_test", '
                '"correlation_id": "corr-Z", '
                '"wave_total": 2, "task_total": 3}'
            )
            result = _asyncio.run(
                graph.ainvoke({"messages": [HumanMessage(content=description)]})
            )

        assert "async_tasks" in result, (
            "runner graph terminal state must include async_tasks; "
            "without it the bridge translator's _extract_state returns "
            "None for every part on the wire. Got keys: "
            f"{sorted(result.keys())!r}"
        )
        snap = result["async_tasks"].get("FEAT-Z")
        assert snap is not None, (
            "async_tasks must be keyed by feature_id with a snapshot dict; "
            f"got {result['async_tasks']!r}"
        )
        assert snap["lifecycle"] == "completed"
        assert snap["build_id"] == "build-FEAT-Z-1"
        assert snap["feature_id"] == "FEAT-Z"
        assert snap["correlation_id"] == "corr-Z"

    def test_runner_graph_values_stream_surfaces_each_lifecycle(self) -> None:
        """``stream_mode='values'`` surfaces every lifecycle transition.

        TASK-ABW-001 — with the subprocess wiring stubbed, the values
        stream still surfaces ``starting`` / ``planning_waves`` /
        ``running_wave`` / ``completed`` in order (the AC-2 contract).
        """
        import asyncio as _asyncio
        from pathlib import Path as _Path

        from langchain_core.messages import HumanMessage

        from forge.subagents import autobuild_runner as _ar_mod

        async def _fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> Any:
            class _FakeStdout:
                def __init__(self) -> None:
                    self._lines = [
                        b"[guardkit-checkpoint] Turn 1 complete (tests: pass)\n",
                        b"",
                    ]

                async def readline(self) -> bytes:
                    return self._lines.pop(0) if self._lines else b""

            class _FakeProc:
                pid = 1
                returncode = 0
                stdout = _FakeStdout()

                async def wait(self) -> int:
                    return 0

                def kill(self) -> None:
                    return None

            return _FakeProc()

        async def _run_stream() -> list[str]:
            seen: list[str] = []
            with patch.object(
                _ar_mod, "_resolve_repo_path", lambda payload: _Path("/tmp/fake-repo")
            ), patch.object(
                _ar_mod, "_resolve_guardkit_path", lambda: _Path("/usr/bin/guardkit")
            ), patch.object(
                _asyncio, "create_subprocess_exec", _fake_create_subprocess_exec
            ):
                graph = _build_runner_graph()
                description = (
                    'RUN_AUTOBUILD subagent=autobuild_runner '
                    'payload={"build_id": "build-FEAT-Y-1", '
                    '"feature_id": "FEAT-Y", '
                    '"repo": "appmilla/api_test", '
                    '"correlation_id": "corr-Y"}'
                )
                async for chunk in graph.astream(
                    {"messages": [HumanMessage(content=description)]},
                    stream_mode="values",
                ):
                    ats = chunk.get("async_tasks") if isinstance(chunk, dict) else None
                    if not ats:
                        continue
                    snap = ats.get("FEAT-Y")
                    if snap and snap.get("lifecycle") not in seen:
                        seen.append(snap.get("lifecycle"))
            return seen

        lifecycles_seen = _asyncio.run(_run_stream())

        assert lifecycles_seen == [
            "starting",
            "planning_waves",
            "running_wave",
            "completed",
        ], (
            "values stream must surface every lifecycle in order; "
            f"got {lifecycles_seen!r}"
        )


class TestLaunchPayloadParser:
    """Defensive-path coverage for ``_extract_launch_payload``.

    The runner nodes call this helper at every boundary; if it raises
    on a malformed launch description, the LangGraph thread crashes
    and the daemon loses an in-flight autobuild. The defensive paths
    below MUST always return ``{}`` rather than raise.
    """

    def test_empty_messages_returns_empty(self) -> None:
        assert _extract_launch_payload([]) == {}

    def test_message_without_string_content_returns_empty(self) -> None:
        """A message whose ``content`` is neither str nor a dict yields ``{}``."""

        class _NotAMessage:
            content = 12345  # not a string, not a Mapping

        assert _extract_launch_payload([_NotAMessage()]) == {}

    def test_dict_message_with_content_extracts_payload(self) -> None:
        """Dict-shaped first messages are also accepted (langgraph variant)."""
        msg = {
            "role": "user",
            "content": (
                'RUN_AUTOBUILD subagent=autobuild_runner '
                'payload={"feature_id": "FEAT-X", "build_id": "b-1", '
                '"correlation_id": "c-1"}'
            ),
        }
        payload = _extract_launch_payload([msg])
        assert payload["feature_id"] == "FEAT-X"
        assert payload["build_id"] == "b-1"

    def test_content_without_payload_marker_returns_empty(self) -> None:
        """Content lacking the ``payload=...`` marker is a benign no-op."""

        class _PlainMsg:
            content = "Hello world — no RUN_AUTOBUILD prefix here."

        assert _extract_launch_payload([_PlainMsg()]) == {}

    def test_malformed_json_payload_returns_empty(self) -> None:
        """A JSONDecodeError is logged at WARNING and yields ``{}``."""

        class _BrokenMsg:
            content = "RUN_AUTOBUILD subagent=autobuild_runner payload={not valid json}"

        assert _extract_launch_payload([_BrokenMsg()]) == {}

    def test_non_dict_json_payload_returns_empty(self) -> None:
        """A JSON value that parses to a list (not dict) yields ``{}``."""

        class _ListMsg:
            content = 'RUN_AUTOBUILD subagent=autobuild_runner payload=["a","b"]'

        assert _extract_launch_payload([_ListMsg()]) == {}


class TestAsyncTasksReducer:
    """``_async_tasks_reducer`` keeps multi-build state isolated per feature_id.

    The reducer is the load-bearing piece behind the
    ``async_tasks`` channel — without these properties a forge daemon
    running multiple builds in parallel would clobber siblings on
    every transition.
    """

    def test_none_inputs_yield_empty_dict(self) -> None:
        assert _async_tasks_reducer(None, None) == {}

    def test_none_update_preserves_current(self) -> None:
        current = {"FEAT-X": {"feature_id": "FEAT-X", "lifecycle": "starting"}}
        result = _async_tasks_reducer(current, None)
        assert result == current
        # Result is a fresh dict — caller can mutate without aliasing.
        assert result is not current

    def test_update_overwrites_only_named_feature(self) -> None:
        """Last-write-wins per ``feature_id``, no sibling clobber."""
        current = {
            "FEAT-X": {"feature_id": "FEAT-X", "lifecycle": "starting"},
            "FEAT-Y": {"feature_id": "FEAT-Y", "lifecycle": "running_wave"},
        }
        update = {"FEAT-X": {"feature_id": "FEAT-X", "lifecycle": "completed"}}
        result = _async_tasks_reducer(current, update)
        assert result["FEAT-X"]["lifecycle"] == "completed"
        # FEAT-Y untouched — multi-build isolation property.
        assert result["FEAT-Y"]["lifecycle"] == "running_wave"

    def test_non_mapping_snapshot_in_update_is_skipped(self) -> None:
        """Defensive: a malformed snapshot does not corrupt the channel."""
        current = {"FEAT-X": {"lifecycle": "starting"}}
        # A node that returns a non-dict snapshot for FEAT-X is treated
        # as a no-op for that key (the existing entry is preserved).
        result = _async_tasks_reducer(current, {"FEAT-X": "not-a-dict"})  # type: ignore[arg-type]
        assert result == current

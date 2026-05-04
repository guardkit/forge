"""Tests for the AsyncTaskStarter adapter (TASK-FORGE-FRR-F010E).

The adapter at :mod:`forge.cli._serve_async_task_starter` bridges the
impedance between
:class:`forge.pipeline.dispatchers.autobuild_async.AsyncTaskStarter`
(the dispatcher's purpose-shaped Protocol) and the deepagents
``AsyncSubAgentMiddleware`` ``start_async_task``
:class:`langchain_core.tools.StructuredTool` (the LangChain
tool-invocation surface). This module covers the four translations the
adapter performs in
:meth:`_StructuredToolAsyncTaskStarter.start_async_task`:

1. Argument-name mapping: ``subagent_name`` → ``subagent_type``.
2. Description synthesis from the dispatcher's structured ``context``
   payload (with ``lifecycle_emitter`` dropped).
3. ``ToolRuntime`` synthesis from ``context["correlation_id"]``.
4. ``Command`` unpacking to extract the freshly-minted ``task_id``,
   plus the ``"Failed to launch ..."`` error-string failure path.

The factory's composition-time duck-type check
(:func:`build_async_task_starter` raises ``TypeError`` on a tool
without ``.func``) is also covered.

Each ``Test*`` class mirrors one acceptance criterion of
TASK-FORGE-FRR-F010E so the criterion → verifier mapping stays
explicit. AAA pattern throughout.

References:
    * TASK-FORGE-FRR-F010E — this task.
    * TASK-FW10-002 — owner of the ``AsyncTaskStarter`` Protocol.
    * TASK-FORGE-FRR-F010B — the precedent
      ``test_serve_deps_stage_log.py`` adapter test pattern.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from forge.cli._serve_async_task_starter import (
    _StructuredToolAsyncTaskStarter,
    build_async_task_starter,
)
from forge.pipeline.dispatchers.autobuild_async import AsyncTaskStarter


# ---------------------------------------------------------------------------
# Shared fixtures / fakes
# ---------------------------------------------------------------------------


class _RecordingTool:
    """Stand-in for the deepagents ``start_async_task`` StructuredTool.

    Records each ``func`` call so tests can assert the four-piece
    translation the adapter performs. Returns whatever ``func_return``
    was configured with — typically a :class:`SimpleNamespace` with an
    ``update`` mapping mimicking ``langgraph.types.Command`` so tests
    do not have to import ``langgraph.types``.
    """

    def __init__(self, *, func_return: Any) -> None:
        self.calls: list[dict[str, Any]] = []
        self._func_return = func_return

    def func(
        self,
        *,
        description: str,
        subagent_type: str,
        runtime: Any,
    ) -> Any:
        self.calls.append(
            {
                "description": description,
                "subagent_type": subagent_type,
                "runtime": runtime,
            }
        )
        return self._func_return


def _command_like(task_id: str) -> SimpleNamespace:
    """Build a minimal ``Command``-shaped object with one async_tasks entry."""
    return SimpleNamespace(
        update={
            "messages": [],
            "async_tasks": {
                task_id: {"task_id": task_id, "status": "running"}
            },
        }
    )


# ---------------------------------------------------------------------------
# AC-3 — happy-path translation: name-mapping + description + ToolRuntime
# ---------------------------------------------------------------------------


class TestHappyPathTranslation:
    """AC-3: the adapter maps Protocol args into the tool's three-arg shape."""

    def test_start_async_task_translates_subagent_name_to_subagent_type(
        self,
    ) -> None:
        tool = _RecordingTool(func_return=_command_like("task-1"))
        adapter = build_async_task_starter(tool)

        adapter.start_async_task(
            subagent_name="autobuild_runner",
            context={
                "correlation_id": "corr-1",
                "build_id": "build-1",
                "feature_id": "FEAT-1",
            },
        )

        assert len(tool.calls) == 1
        assert tool.calls[0]["subagent_type"] == "autobuild_runner"

    def test_start_async_task_returns_task_id_from_command(self) -> None:
        tool = _RecordingTool(func_return=_command_like("task-abc"))
        adapter = build_async_task_starter(tool)

        task_id = adapter.start_async_task(
            subagent_name="autobuild_runner",
            context={"correlation_id": "corr-1"},
        )

        assert task_id == "task-abc"

    def test_description_carries_structured_payload(self) -> None:
        tool = _RecordingTool(func_return=_command_like("task-1"))
        adapter = build_async_task_starter(tool)

        adapter.start_async_task(
            subagent_name="autobuild_runner",
            context={
                "correlation_id": "corr-xyz",
                "build_id": "build-1",
                "feature_id": "FEAT-1",
                "context_entries": [
                    {"flag": "--plan", "value": "x.yaml", "kind": "path"}
                ],
            },
        )

        description = tool.calls[0]["description"]
        # The synthesised prompt is parseable: it begins with the
        # RUN_AUTOBUILD prefix and embeds the JSON-serialised payload.
        assert description.startswith(
            "RUN_AUTOBUILD subagent=autobuild_runner payload="
        )
        payload_json = description.split("payload=", 1)[1]
        payload = json.loads(payload_json)
        assert payload["build_id"] == "build-1"
        assert payload["feature_id"] == "FEAT-1"
        assert payload["correlation_id"] == "corr-xyz"
        assert payload["context_entries"] == [
            {"flag": "--plan", "value": "x.yaml", "kind": "path"}
        ]

    def test_runtime_tool_call_id_derives_from_correlation_id(self) -> None:
        tool = _RecordingTool(func_return=_command_like("task-1"))
        adapter = build_async_task_starter(tool)

        adapter.start_async_task(
            subagent_name="autobuild_runner",
            context={"correlation_id": "corr-deadbeef"},
        )

        runtime = tool.calls[0]["runtime"]
        assert runtime.tool_call_id == "autobuild-dispatch-corr-deadbeef"

    def test_runtime_tool_call_id_falls_back_when_correlation_id_missing(
        self,
    ) -> None:
        tool = _RecordingTool(func_return=_command_like("task-1"))
        adapter = build_async_task_starter(tool)

        adapter.start_async_task(
            subagent_name="autobuild_runner",
            context={},  # no correlation_id
        )

        runtime = tool.calls[0]["runtime"]
        assert runtime.tool_call_id == "autobuild-dispatch"


# ---------------------------------------------------------------------------
# AC-3 — lifecycle_emitter is dropped before description serialisation
# ---------------------------------------------------------------------------


class TestLifecycleEmitterDropped:
    """AC-3: ``lifecycle_emitter`` (in-process Python obj) cannot cross the boundary.

    DDR-007 §Decision Option A wired ``lifecycle_emitter`` onto the
    dispatcher's launch_payload. The middleware tool serialises the
    payload as a JSON string forwarded to the LangGraph deployment;
    in-process Python objects cannot survive that hop. The adapter
    drops the key so the JSON serialisation succeeds (and the runner
    is not handed an unparseable object).
    """

    def test_lifecycle_emitter_is_not_in_description_payload(self) -> None:
        tool = _RecordingTool(func_return=_command_like("task-1"))
        adapter = build_async_task_starter(tool)

        sentinel_emitter = object()
        adapter.start_async_task(
            subagent_name="autobuild_runner",
            context={
                "correlation_id": "corr-1",
                "build_id": "build-1",
                "feature_id": "FEAT-1",
                "lifecycle_emitter": sentinel_emitter,
            },
        )

        description = tool.calls[0]["description"]
        payload = json.loads(description.split("payload=", 1)[1])
        assert "lifecycle_emitter" not in payload

    def test_lifecycle_emitter_present_does_not_raise(self) -> None:
        # Regression: an earlier prototype tried to JSON-serialise the
        # full context (including the emitter) and raised TypeError.
        # The adapter must drop the key BEFORE serialisation.
        tool = _RecordingTool(func_return=_command_like("task-1"))
        adapter = build_async_task_starter(tool)

        # Use an emitter that would deliberately break repr/str if
        # touched, to catch any accidental coercion path.
        class _Hostile:
            def __repr__(self) -> str:  # pragma: no cover - sanity guard
                raise AssertionError(
                    "repr was called on lifecycle_emitter; the adapter "
                    "must drop the key before serialisation"
                )

        adapter.start_async_task(
            subagent_name="autobuild_runner",
            context={"correlation_id": "corr-1", "lifecycle_emitter": _Hostile()},
        )


# ---------------------------------------------------------------------------
# AC-3 — Command unpacking edge cases
# ---------------------------------------------------------------------------


class TestCommandUnpacking:
    """AC-3: ``Command`` shape variants raise typed errors."""

    def test_failure_string_raises_runtime_error(self) -> None:
        # Mirror the deepagents ``async_subagents.py:298-299`` failure
        # branch: ``return f"Failed to launch async subagent ..."``.
        tool = _RecordingTool(
            func_return=(
                "Failed to launch async subagent 'autobuild_runner': "
                "deployment unreachable"
            )
        )
        adapter = build_async_task_starter(tool)

        with pytest.raises(RuntimeError, match="Failed to launch"):
            adapter.start_async_task(
                subagent_name="autobuild_runner",
                context={"correlation_id": "corr-1"},
            )

    def test_unexpected_string_return_raises_runtime_error(self) -> None:
        tool = _RecordingTool(func_return="some unexpected string")
        adapter = build_async_task_starter(tool)

        with pytest.raises(RuntimeError, match="unexpected string"):
            adapter.start_async_task(
                subagent_name="autobuild_runner",
                context={"correlation_id": "corr-1"},
            )

    def test_command_without_async_tasks_raises_runtime_error(self) -> None:
        tool = _RecordingTool(
            func_return=SimpleNamespace(update={"messages": []})
        )
        adapter = build_async_task_starter(tool)

        with pytest.raises(RuntimeError, match="async_tasks"):
            adapter.start_async_task(
                subagent_name="autobuild_runner",
                context={"correlation_id": "corr-1"},
            )

    def test_command_with_zero_async_tasks_entries_raises(self) -> None:
        tool = _RecordingTool(
            func_return=SimpleNamespace(update={"async_tasks": {}})
        )
        adapter = build_async_task_starter(tool)

        with pytest.raises(RuntimeError, match="exactly one entry"):
            adapter.start_async_task(
                subagent_name="autobuild_runner",
                context={"correlation_id": "corr-1"},
            )

    def test_command_with_two_async_tasks_entries_raises(self) -> None:
        tool = _RecordingTool(
            func_return=SimpleNamespace(
                update={
                    "async_tasks": {
                        "task-1": {"status": "running"},
                        "task-2": {"status": "running"},
                    }
                }
            )
        )
        adapter = build_async_task_starter(tool)

        with pytest.raises(RuntimeError, match="exactly one entry"):
            adapter.start_async_task(
                subagent_name="autobuild_runner",
                context={"correlation_id": "corr-1"},
            )

    def test_command_without_update_mapping_raises(self) -> None:
        tool = _RecordingTool(func_return=object())  # no .update at all
        adapter = build_async_task_starter(tool)

        with pytest.raises(RuntimeError, match="without an 'update' mapping"):
            adapter.start_async_task(
                subagent_name="autobuild_runner",
                context={"correlation_id": "corr-1"},
            )

    def test_empty_string_task_id_raises(self) -> None:
        tool = _RecordingTool(
            func_return=SimpleNamespace(
                update={"async_tasks": {"": {"status": "running"}}}
            )
        )
        adapter = build_async_task_starter(tool)

        with pytest.raises(RuntimeError, match="non-string or empty task_id"):
            adapter.start_async_task(
                subagent_name="autobuild_runner",
                context={"correlation_id": "corr-1"},
            )


# ---------------------------------------------------------------------------
# Composition-time duck-type guard on the factory
# ---------------------------------------------------------------------------


class TestFactoryDuckTypeGuard:
    """``build_async_task_starter`` fails fast at composition on bad inputs."""

    def test_factory_raises_typeerror_when_tool_has_no_func(self) -> None:
        # The raw StructuredTool shape exposes ``.func``; an object
        # without it is a wiring bug — surface it at composition time.
        class _NoFunc:
            name = "start_async_task"

        with pytest.raises(TypeError, match="callable 'func'"):
            build_async_task_starter(_NoFunc())

    def test_factory_raises_typeerror_when_func_is_not_callable(self) -> None:
        class _NonCallableFunc:
            name = "start_async_task"
            func = "not callable"

        with pytest.raises(TypeError, match="callable 'func'"):
            build_async_task_starter(_NonCallableFunc())


# ---------------------------------------------------------------------------
# Protocol conformance — the adapter satisfies AsyncTaskStarter
# ---------------------------------------------------------------------------


class TestAsyncTaskStarterProtocolConformance:
    """The adapter is structurally compatible with ``AsyncTaskStarter``.

    The ``AsyncTaskStarter`` Protocol is ``runtime_checkable``;
    asserting :func:`isinstance` against it locks in the regression-
    catching invariant that the dispatcher's call site at
    ``autobuild_async.py:473`` sees a Protocol-shaped object.
    """

    def test_adapter_satisfies_async_task_starter_protocol(self) -> None:
        tool = _RecordingTool(func_return=_command_like("task-1"))
        adapter = build_async_task_starter(tool)

        assert isinstance(adapter, _StructuredToolAsyncTaskStarter)
        assert isinstance(adapter, AsyncTaskStarter)

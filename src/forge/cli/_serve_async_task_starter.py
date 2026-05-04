"""Production binding for ``AsyncTaskStarter`` (TASK-FORGE-FRR-F010E).

This module bridges the impedance between the autobuild dispatcher's
:class:`~forge.pipeline.dispatchers.autobuild_async.AsyncTaskStarter`
Protocol (a *named-method* shape:
``start_async_task(subagent_name, context) -> str``) and the
``deepagents.middleware.async_subagents.AsyncSubAgentMiddleware``
``start_async_task`` :class:`langchain_core.tools.StructuredTool` (a
LangChain *tool-invocation* shape:
``tool.invoke({"description": ..., "subagent_type": ..., "runtime": ...})``).

Without this adapter the production composer returns the raw
``StructuredTool`` from
:func:`forge.cli._serve_production._resolve_async_task_starter`, which
the dispatcher then invokes as ``tool.start_async_task(...)`` — a method
``StructuredTool`` does not expose. The result on the GB10 rerun
(correlation_id ``dfad8e7f-92af-4b5f-896f-ca75ad8343bf``) was:

    AttributeError: 'StructuredTool' object has no attribute 'start_async_task'

…raised inside the consumer's outer ``try/except``, leaving the
``builds`` row in QUEUED state with no outbound
``pipeline.build-started.*`` envelope and no autobuild_runner thread
launched. See TASK-FORGE-FRR-F010E for the full investigation.

Design rules (mirrors the F010.B :mod:`_serve_deps_stage_log` precedent)
-----------------------------------------------------------------------

* **Adapter at the composition seam.** The dispatcher's Protocol stays
  purpose-shaped; the adapter does the four translations the brief did
  not anticipate (name-mapping, description synthesis, ToolRuntime
  synthesis, ``Command`` unpacking) at exactly one place.
* **Bypass the StructuredTool wrapper.** ``tool.invoke({...})`` from
  outside a LangGraph tool-execution loop fails with
  ``TypeError: ... missing 1 required positional argument: 'runtime'``
  because the wrapper does not auto-synthesise ``ToolRuntime``. We call
  ``tool.func`` directly with a synthesised runtime so the seam stays
  testable and decoupled from the LangGraph runtime injection
  machinery.
* **Drop ``lifecycle_emitter``.** The dispatcher's launch_payload at
  ``autobuild_async.py:466-472`` carries an in-process
  :class:`PipelineLifecycleEmitter` (per DDR-007 §Decision Option A).
  That object cannot cross the LangGraph deployment boundary — the
  runner runs in a remote thread and only sees JSON-serialisable input.
  The adapter drops the key from the payload before the description is
  synthesised; the dispatcher's own
  :meth:`AutobuildStateInitialiser.initialise_autobuild_state` write
  remains the in-process state-channel anchor.
* **Fail fast on tool-side errors.** The middleware's
  ``start_async_task`` closure catches its own LangGraph SDK exceptions
  and returns an error string starting with
  ``"Failed to launch async subagent"``. The adapter raises
  :class:`RuntimeError` so the dispatcher's outer try/except logs the
  failure at WARNING (matching the existing observability) rather than
  silently writing an empty task_id to the state channel.

References:
    * TASK-FORGE-FRR-F010E — this task.
    * TASK-FORGE-FRR-F010B — the precedent ``StageLogReader`` adapter
      at the same seam (commit ``751995f``).
    * TASK-FW10-002 — owner of the ``AsyncTaskStarter`` Protocol.
    * TASK-FW10-008 — owner of the ``AsyncSubAgentMiddleware`` tool
      surface.
    * ADR-ARCH-031 — ``AsyncSubAgent`` / ``start_async_task``
      architectural decision.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any, Mapping

from forge.pipeline.dispatchers.autobuild_async import AsyncTaskStarter

logger = logging.getLogger(__name__)


__all__ = [
    "build_async_task_starter",
]


#: Substring that flags an error-string return from the middleware's
#: ``start_async_task`` closure (see deepagents
#: ``async_subagents.py:298-299`` and the symmetric ``astart_async_task``
#: branch at ``:339``). The closure catches its own SDK exceptions and
#: returns this prefix instead of a :class:`Command`; the adapter
#: surfaces it as a :class:`RuntimeError` so the dispatcher's outer
#: WARNING-and-ack path fires with a typed exception.
_TOOL_FAILURE_MARKER: str = "Failed to launch async subagent"


def _synthesise_description(
    subagent_name: str, context: Mapping[str, Any]
) -> str:
    """Build the natural-language prompt the launched runner will receive.

    The middleware tool forwards ``description`` verbatim as the first
    user message on the launched LangGraph thread (see
    ``async_subagents.py:295``). The runner side
    (``autobuild_runner`` per TASK-FW10-002) is responsible for parsing
    it back into structured fields. To keep the prompt parseable
    without forcing a one-off schema, the adapter emits a single
    JSON object on a "RUN_AUTOBUILD" prefix line — easy for both an
    LLM and a parser to recognise.

    The ``lifecycle_emitter`` key (an in-process Python object — see
    DDR-007 §Decision Option A) is dropped before serialisation: it is
    not JSON-safe and cannot cross the LangGraph deployment boundary
    regardless. Any other non-JSON-safe values are likewise stripped
    via :func:`json.dumps`'s ``default`` callback rather than raising —
    the dispatcher's contract is that the structured fields it cares
    about (``build_id``, ``feature_id``, ``correlation_id``,
    ``context_entries``) are JSON-safe by construction.
    """
    sanitised: dict[str, Any] = {
        key: value
        for key, value in context.items()
        if key != "lifecycle_emitter"
    }
    payload_json = json.dumps(sanitised, default=str, sort_keys=True)
    return (
        f"RUN_AUTOBUILD subagent={subagent_name} "
        f"payload={payload_json}"
    )


def _extract_task_id(result: Any) -> str:
    """Pull the freshly-minted ``task_id`` out of the tool's return.

    The middleware's success path returns a
    :class:`langgraph.types.Command` whose ``update`` dict contains an
    ``"async_tasks"`` mapping with exactly one entry — the
    just-launched task keyed by its ``thread_id``. The failure path
    returns a string beginning with :data:`_TOOL_FAILURE_MARKER`.

    Args:
        result: Return value of ``tool.func(...)``. Either a
            :class:`Command` (success) or an error string (failure).

    Returns:
        The minted ``task_id`` string.

    Raises:
        RuntimeError: When the tool returned an error string, or a
            :class:`Command` whose ``update.async_tasks`` did not
            contain exactly one entry. Both shapes are contract
            violations from the dispatcher's perspective; surfacing
            them as :class:`RuntimeError` lets the consumer's outer
            try/except WARNING fire with a typed exception.
    """
    if isinstance(result, str):
        if result.startswith(_TOOL_FAILURE_MARKER):
            raise RuntimeError(
                "_StructuredToolAsyncTaskStarter: middleware tool "
                f"returned launch failure: {result!r}"
            )
        raise RuntimeError(
            "_StructuredToolAsyncTaskStarter: middleware tool returned an "
            f"unexpected string ({result!r}); expected a "
            "langgraph.types.Command on success"
        )

    update = getattr(result, "update", None)
    if not isinstance(update, Mapping):
        raise RuntimeError(
            "_StructuredToolAsyncTaskStarter: middleware tool returned "
            f"{type(result).__name__} without an 'update' mapping; "
            "expected a langgraph.types.Command"
        )

    async_tasks = update.get("async_tasks")
    if not isinstance(async_tasks, Mapping) or len(async_tasks) != 1:
        raise RuntimeError(
            "_StructuredToolAsyncTaskStarter: middleware tool returned a "
            "Command whose update['async_tasks'] is missing or does not "
            f"contain exactly one entry; got {async_tasks!r}"
        )

    (task_id,) = async_tasks.keys()
    if not isinstance(task_id, str) or not task_id:
        raise RuntimeError(
            "_StructuredToolAsyncTaskStarter: middleware tool minted a "
            f"non-string or empty task_id ({task_id!r}); refusing to "
            "thread it through to the dispatcher"
        )
    return task_id


class _StructuredToolAsyncTaskStarter:
    """:class:`AsyncTaskStarter` adapter over a LangChain ``StructuredTool``.

    Module-private — callers construct instances via
    :func:`build_async_task_starter`. Holds the underlying tool plus the
    deterministic ``tool_call_id`` derived from each dispatch's
    ``correlation_id`` (carried in ``context``); never opens any
    handles or connections of its own.

    The class implements exactly one method (``start_async_task``)
    matching the
    :class:`~forge.pipeline.dispatchers.autobuild_async.AsyncTaskStarter`
    Protocol. The Protocol is ``runtime_checkable`` so callers can
    :func:`isinstance` against it directly.

    Args:
        tool: A LangChain ``StructuredTool`` exposing both ``.func``
            (sync entry point) and matching the deepagents
            ``start_async_task`` shape
            ``(description: str, subagent_type: str, runtime: ToolRuntime)``.
            The duck-typed pre-check happens in
            :func:`build_async_task_starter`; the class assumes a valid
            ``.func`` callable here.
    """

    __slots__ = ("_tool",)

    def __init__(self, tool: Any) -> None:
        self._tool = tool

    def start_async_task(
        self,
        subagent_name: str,
        context: Mapping[str, Any],
    ) -> str:
        """Launch ``subagent_name`` via the wrapped middleware tool.

        Translates the dispatcher's purpose-shaped Protocol call into
        the four-piece middleware-tool invocation and unpacks the
        :class:`Command` return into a bare ``task_id`` string.

        Args:
            subagent_name: Registered async-subagent name (e.g.
                :data:`forge.pipeline.dispatchers.autobuild_async.AUTOBUILD_RUNNER_NAME`).
                Forwarded to the tool as ``subagent_type``.
            context: JSON-safe mapping carrying the dispatch payload.
                Must include ``correlation_id`` (used as the
                synthesised ``ToolRuntime.tool_call_id`` so the
                downstream :class:`ToolMessage` is attributable to a
                single dispatch). May include ``lifecycle_emitter``;
                if present it is dropped before description
                serialisation (see module docstring).

        Returns:
            The freshly-minted ``task_id`` extracted from
            ``Command.update["async_tasks"]``.

        Raises:
            RuntimeError: If the underlying tool returns an error
                string, or a :class:`Command` whose shape does not
                carry exactly one ``async_tasks`` entry. The
                dispatcher's outer ``pipeline_consumer.handle_message``
                try/except converts this into a WARNING-and-ack so
                the JetStream queue does not wedge.
        """
        correlation_id = context.get("correlation_id") or ""
        if not isinstance(correlation_id, str):
            correlation_id = str(correlation_id)
        tool_call_id = (
            f"autobuild-dispatch-{correlation_id}"
            if correlation_id
            else "autobuild-dispatch"
        )

        description = _synthesise_description(subagent_name, context)
        runtime = SimpleNamespace(tool_call_id=tool_call_id)

        result = self._tool.func(
            description=description,
            subagent_type=subagent_name,
            runtime=runtime,
        )

        task_id = _extract_task_id(result)
        logger.debug(
            "async_task_starter: launched subagent_name=%s task_id=%s "
            "correlation_id=%s",
            subagent_name,
            task_id,
            correlation_id or "<unset>",
        )
        return task_id


def build_async_task_starter(tool: Any) -> AsyncTaskStarter:
    """Build the production :class:`AsyncTaskStarter` for the autobuild dispatch.

    Wraps the resolved ``start_async_task`` :class:`StructuredTool`
    from :class:`AsyncSubAgentMiddleware` in the
    :class:`_StructuredToolAsyncTaskStarter` adapter so the autobuild
    dispatcher can invoke it through its purpose-shaped Protocol
    surface (``start_async_task(subagent_name, context) -> str``)
    without touching LangChain or LangGraph internals.

    The function is the single documented entry point for wiring this
    collaborator on
    :func:`forge.pipeline.dispatchers.autobuild_async.dispatch_autobuild_async`,
    matching the precedent set by
    :func:`forge.cli._serve_deps_stage_log.build_stage_log_recorder`
    and siblings.

    Args:
        tool: An object exposing a callable ``func`` attribute that
            accepts the deepagents ``start_async_task`` keyword
            arguments (``description: str``, ``subagent_type: str``,
            ``runtime: ToolRuntime``). In production this is the
            :class:`StructuredTool` returned by
            :func:`AsyncSubAgentMiddleware._build_async_subagent_tools`.

    Returns:
        An :class:`AsyncTaskStarter` Protocol implementation. The
        Protocol is ``runtime_checkable``; callers may
        :func:`isinstance` against it.

    Raises:
        TypeError: If ``tool`` does not expose a callable ``func``
            attribute. Raising at composition time means a wiring
            regression surfaces at boot rather than on the first
            inbound dispatch envelope.
    """
    func = getattr(tool, "func", None)
    if not callable(func):
        raise TypeError(
            "build_async_task_starter: tool must expose a callable 'func' "
            "attribute matching the deepagents start_async_task shape "
            "(description, subagent_type, runtime); got "
            f"{type(tool).__name__} (func={func!r})"
        )
    return _StructuredToolAsyncTaskStarter(tool)

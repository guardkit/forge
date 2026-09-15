"""Forge-owned operating contract for remote async autobuild tasks.

Deep Agents 0.7.14 deliberately leaves
``AsyncSubAgentMiddleware.system_prompt`` unset unless the application
supplies one.  Forge depends on more than the tool schemas: its supervisor
must know when task state is live, how updates and cancellation behave, and
where a completed task's result comes from.  Keep that contract here so both
runtime construction and the image oracle verify the same text and tools.
"""

from __future__ import annotations

from typing import Any


FORGE_ASYNC_SUBAGENT_SYSTEM_PROMPT = """## Forge async autobuild protocol

Use `start_async_task` to dispatch long-running autobuild work. Preserve the
returned task ID, report it to the operator, and continue the supervisor loop;
do not treat dispatch as completion or immediately poll a task that was just
started.

Task status is live only when returned by `check_async_task` or
`list_async_tasks`. Status text already present in the conversation is stale.
Use `check_async_task` when the operator asks about one task or needs its
result, and `list_async_tasks` when reconciling several tracked tasks.

Use `update_async_task` to send corrected or additional instructions. An
update interrupts the current run, starts another run on the same thread, and
keeps the task ID. Use `cancel_async_task` when work is no longer required,
and confirm cancellation from the tool result rather than assuming it.

A terminal status is not itself the terminal result. When a task succeeds,
call `check_async_task` and return the result it reports. When a task errors
or is cancelled, report that terminal state and the tool's diagnostic. Never
invent a result from a launch acknowledgement, an earlier status, or local
filesystem state; the remote async task channel is authoritative.
"""

ASYNC_SUBAGENT_TOOL_NAMES = frozenset(
    {
        "start_async_task",
        "check_async_task",
        "update_async_task",
        "cancel_async_task",
        "list_async_tasks",
    }
)


class AsyncSubagentProtocolError(RuntimeError):
    """The constructed middleware does not expose Forge's async contract."""


def verify_async_subagent_middleware_contract(
    middleware: Any,
) -> dict[str, Any]:
    """Return the middleware tools after validating Forge's live contract.

    This is intentionally a construction-time check rather than a package
    version gate.  It catches a missing application prompt, a renamed tool, or
    a partial middleware object even when the installed SDK imports cleanly.
    """

    prompt = getattr(middleware, "system_prompt", None)
    if not isinstance(prompt, str) or not prompt.startswith(
        FORGE_ASYNC_SUBAGENT_SYSTEM_PROMPT
    ):
        raise AsyncSubagentProtocolError(
            "Forge async subagent protocol is absent from the constructed "
            "middleware system prompt"
        )

    tools = {
        getattr(tool, "name", None): tool
        for tool in getattr(middleware, "tools", ())
        if isinstance(getattr(tool, "name", None), str)
    }
    actual = frozenset(tools)
    if actual != ASYNC_SUBAGENT_TOOL_NAMES:
        missing = sorted(ASYNC_SUBAGENT_TOOL_NAMES - actual)
        unexpected = sorted(actual - ASYNC_SUBAGENT_TOOL_NAMES)
        raise AsyncSubagentProtocolError(
            "Forge async subagent tool surface differs from its contract: "
            f"missing={missing!r}, unexpected={unexpected!r}"
        )
    return tools

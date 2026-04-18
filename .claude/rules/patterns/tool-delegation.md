---
paths: agents/**/*.py, lib/**/*.py, tests/**/*.py
---

# Tool Delegation Pattern (Orchestrator Template)

Tool separation contract for the four-role orchestrator: Orchestrator owns the four orchestrator tools, Implementer delegates to those same four, Evaluator has **NO tools** (purely reasoning-based), Builder is an async-remote subagent whose tool surface is owned by the remote graph.

Enforced at factory exit by ``_validate_subagent_tools()`` in ``agents/agents.py`` and the vendored ``lib/factory_guards.py``. Fixes prevented: TASK-REV-32D2 F2 (DeepAgents ``SubAgentMiddleware`` middleware-tool injection into subagents declared with ``tools=[]``).

> **Cross-reference**: See the base ``langchain-deepagents`` template's ``patterns/tool-delegation.md`` and ``patterns/factory.md`` for the broader adversarial-cooperation rationale. This file is the orchestrator-template-local adaptation.

## Role → Tool Inventory Contract

| Role | Allowed Tools | Enforcement |
|------|--------------|-------------|
| Orchestrator | 4 orchestrator tools (`analyse_context`, `plan_pipeline`, `execute_command`, `verify_output`) — plus whatever ``create_deep_agent()`` middleware injects | ``create_deep_agent(tools=...)`` call site |
| Implementer | Exactly the 4 orchestrator tools | ``IMPLEMENTER_ALLOWED_TOOLS`` + ``_validate_subagent_tools()`` |
| Evaluator | NONE (empty set) | ``EVALUATOR_ALLOWED_TOOLS = set()`` + ``_validate_subagent_tools()`` |
| Builder | Owned by remote LangGraph deployment | Skipped — async-remote surface |

## Why SubAgents need post-construction assertion

DeepAgents' ``create_deep_agent()`` accepts a ``subagents=[...]`` parameter. Each ``SubAgent`` TypedDict spec has a ``tools`` field. The spec's ``tools=[]`` parameter only controls *user-provided* tools. When ``SubAgentMiddleware`` compiles the subagent into a sub-graph, the framework can still inject middleware tools (``write_file``, ``edit_file``, ``execute``, ``write_todos`` …).

This is the **same** F2 bug class TASK-REV-32D2 identified on ``create_deep_agent(tools=[])`` in the base adversarial-cooperation template. The fix here is symmetrical: assert the *realised* tool inventory of each sync subagent post-construction, not just the declared spec.

## The Assertion

```python
from lib.factory_guards import ToolLeakageError, assert_tool_inventory

IMPLEMENTER_ALLOWED_TOOLS: set[str] = {
    "analyse_context", "plan_pipeline", "execute_command", "verify_output",
}
EVALUATOR_ALLOWED_TOOLS: set[str] = set()

def create_orchestrator(...) -> CompiledStateGraph:
    ...
    graph = create_deep_agent(..., subagents=subagents, ...)

    # Fail loud at construction, not at runtime.
    _validate_subagent_tools(graph, "implementer", IMPLEMENTER_ALLOWED_TOOLS)
    _validate_subagent_tools(graph, "evaluator",   EVALUATOR_ALLOWED_TOOLS)

    return graph
```

## Introspection approach

The realised tool inventory of a SubAgent is not directly accessible from the ``SubAgent`` TypedDict — that only exposes the declared ``tools`` list. ``_extract_subagent_tools()`` walks the compiled parent graph's ``.nodes`` mapping looking for a node whose name matches the subagent, then reads ``.tools`` (or ``.runnable.tools``) from that node.

If the DeepAgents layout changes such that the helper cannot locate the node, it returns ``None`` and ``_validate_subagent_tools()`` **logs a warning and skips** — intentionally failing open rather than silently passing. The paired unit test patches ``create_deep_agent`` with a ``nodes``-compatible mock so the happy path and the leak-detection path both have coverage in CI.

## ``ToolLeakageError`` message shape

The vendored ``assert_tool_inventory()`` and the inline ``_validate_subagent_tools()`` both raise ``ToolLeakageError`` with a consistent shape so downstream logging can parse it across templates:

```
<context>: Tool inventory mismatch: unexpected=[...]; missing=[...].
Expected: [...], Actual: [...]
```

Keep this shape aligned with the base template's ``lib/factory_guards.py`` — consumers that monitor multiple agent templates in one pipeline will regex against this format.

## ``lib/factory_guards.py`` is vendored

The orchestrator template does not ``extends: langchain-deepagents``, so importing the base template's ``factory_guards`` is not possible. The module is vendored into ``lib/factory_guards.py`` with a header note recording the source of truth. If a future refactor switches the orchestrator template to ``extends: langchain-deepagents``, delete the vendored copy and import from the base template instead — do not carry two diverging copies.

## When to use this pattern

- Any agent system where subagents have different privilege levels
- Anywhere ``create_deep_agent(subagents=[...])`` is called with SubAgent specs that declare ``tools=[]`` or a curated allowlist
- Cross-template parity: match the assertion shape to the base template so a multi-template consumer gets consistent leakage reports

## When NOT to use

- AsyncSubAgents whose tool surface is owned by a remote graph (the Builder here)
- Single-agent systems where there are no subagents to separate
- Agents intentionally built via ``create_deep_agent()`` with the full middleware stack

## Related

- Base template: ``.claude/rules/patterns/tool-delegation.md``, ``.claude/rules/patterns/factory.md``
- Review: ``TASK-REV-LES1`` §HIGH-1 and TASK-REV-32D2 F2
- Vendored source: ``lib/factory_guards.py``

---
paths: "**/agents.py, **/agent.py"
---

# SubAgent Composition

## Overview

SubAgent and AsyncSubAgent TypedDict factory functions for composing hierarchical agent graphs. Each factory function builds a typed spec dictionary consumed by `create_deep_agent(subagents=[...])`. This pattern separates agent definition from agent wiring, making each role independently testable.

## Implementation

### Sync SubAgent (with tools)

```python
from deepagents import SubAgent

def implementer_subagent(model: str) -> SubAgent:
    """Build the Implementer SubAgent spec."""
    if not model or not isinstance(model, str):
        raise ValueError(f"model must be a non-empty string, got: {model!r}")

    return SubAgent(
        name="implementer",
        description="Focused execution agent that implements plans...",
        system_prompt=IMPLEMENTER_SYSTEM_PROMPT.format(date=today),
        model=model,
        tools=list(_ORCHESTRATOR_TOOLS),
    )
```

### Sync SubAgent (no tools)

```python
from deepagents import SubAgent

def evaluator_subagent(model: str) -> SubAgent:
    """Build the Evaluator SubAgent spec (pure assessment, no tools)."""
    if not model or not isinstance(model, str):
        raise ValueError(f"model must be a non-empty string, got: {model!r}")

    return SubAgent(
        name="evaluator",
        description="Objective quality-assurance agent...",
        system_prompt=EVALUATOR_SYSTEM_PROMPT.format(date=today),
        model=model,
        tools=[],  # Evaluator has no tools — evaluation is purely analytical
    )
```

### Async SubAgent (remote deployment)

```python
from deepagents import AsyncSubAgent

def builder_async_subagent(
    url: str | None = None,
    graph_id: str = "builder",
) -> AsyncSubAgent:
    """Build the Builder AsyncSubAgent spec for remote LangGraph deployment."""
    spec: dict[str, Any] = AsyncSubAgent(
        name="builder",
        description="Non-blocking async subagent for long-running tasks...",
        graph_id=graph_id,
    )
    if url is not None:
        spec["url"] = url
    return spec
```

### Wiring into the orchestrator

```python
subagents = [
    implementer_subagent(model=implementation_model),
    evaluator_subagent(model=reasoning_model),
    builder_async_subagent(),
]

graph = create_deep_agent(
    model=reasoning_model,
    tools=list(_ORCHESTRATOR_TOOLS),
    system_prompt=system_prompt,
    subagents=subagents,
    memory=["./AGENTS.md"],
)
```

## SubAgent TypedDict Fields

| Field | Type | Required | Purpose |
|-------|------|----------|---------|
| `name` | str | Yes | Unique identifier for delegation |
| `description` | str | Yes | Tells orchestrator when to delegate to this agent |
| `system_prompt` | str | Yes (Sync) | Role-specific instructions |
| `model` | str | Yes (Sync) | Provider:model identifier |
| `tools` | list | Yes (Sync) | Tools available to this agent (empty list for none) |
| `graph_id` | str | Yes (Async) | Graph name on remote server |
| `url` | str | No (Async) | Remote server URL (omit for local ASGI) |

## When to Use

- Defining agent roles as independent, testable factory functions
- Composing multi-agent graphs with `create_deep_agent()`
- Separating agent configuration from agent assembly

## Best Practices

- One factory function per agent role
- Validate model strings at the factory boundary, not at wiring time
- Use `list()` when passing tool lists to avoid shared mutable state
- Format system prompts with runtime values (date, domain) inside the factory
- Keep `description` fields detailed — the orchestrator uses them to decide delegation

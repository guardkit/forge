---
paths: "**/agent.py, **/agents.py, **/orchestrator*.py, **/config*.yaml"
---

# Two-Model Orchestration

## Overview

The core DeepAgents pattern: a reasoning model drives high-level decisions (planning, evaluation, delegation) while a separate implementation model executes concrete tasks. This separation optimises cost and latency — the reasoning model handles complex orchestration while the cheaper/faster implementation model handles execution.

## Implementation

Model selection is configured via YAML and wired at startup:

```yaml
# orchestrator-config.yaml
orchestrator:
  reasoning_model: "anthropic:claude-sonnet-4-6"       # Orchestrator + Evaluator
  implementation_model: "anthropic:claude-haiku-4-5"    # Implementer
```

The entrypoint loads config and passes models to the orchestrator factory:

```python
from agents import create_orchestrator

config = _load_config(project_root / "orchestrator-config.yaml")
orch_config = config.get("orchestrator", _DEFAULT_CONFIG["orchestrator"])

agent = create_orchestrator(
    reasoning_model=orch_config["reasoning_model"],
    implementation_model=orch_config["implementation_model"],
    domain_prompt=domain_prompt,
)
```

Model strings use `provider:model` format for `init_chat_model()` compatibility.

## Routing Rules

| Role | Model Tier | Rationale |
|------|-----------|-----------|
| Orchestrator | Reasoning (Sonnet) | Complex planning, delegation, verification |
| Evaluator | Reasoning (Sonnet) | Objective quality assessment requires reasoning |
| Implementer | Implementation (Haiku) | Focused execution, cost-effective |
| Builder | N/A (remote) | Delegated to remote LangGraph deployment |

## Fallback Behaviour

Always provide hardcoded defaults so the orchestrator starts even without a config file:

```python
_DEFAULT_CONFIG: dict[str, Any] = {
    "orchestrator": {
        "reasoning_model": "anthropic:claude-sonnet-4-6",
        "implementation_model": "anthropic:claude-haiku-4-5",
    },
}
```

Config loading must never raise — log a warning and return defaults on any error (FileNotFoundError, YAMLError, unexpected structure).

## When to Use

- Building multi-agent systems where different roles have different capability requirements
- Optimising LLM costs by routing simple execution to cheaper models
- Separating "thinking" from "doing" in agent architectures

## Best Practices

- Keep model identifiers in config, not hardcoded in agent factories
- Always validate model strings are non-empty before passing to `create_deep_agent()`
- Use the same reasoning model for orchestrator and evaluator to maintain consistent quality assessment
- Provide sensible defaults for every config value

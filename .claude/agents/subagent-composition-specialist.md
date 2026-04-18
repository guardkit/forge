---
capabilities:
- SubAgent TypedDict factory function construction
- AsyncSubAgent TypedDict factory function construction
- Hierarchical agent graph composition with create_deep_agent()
- Runtime prompt injection with date and domain placeholders
- Tool list construction and safe copying for subagent specs
- Model identifier validation in provider:model format
- Three-role orchestrator/implementer/evaluator graph assembly
description: SubAgent and AsyncSubAgent TypedDict factory functions that build typed
  spec dictionaries for name, description, system_prompt, model, and tools, consumed
  by create_deep_agent(subagents=[...]) to compose hierarchical agent graphs
keywords:
- subagent
- asyncsubagent
- create_deep_agent
- deepagents
- langgraph
- typeddict
- factory
- orchestrator
- hierarchical
- graph-composition
- system-prompt
- model-identifier
name: subagent-composition-specialist
phase: implementation
priority: 7
confidence_score: 90
stack:
- python
technologies:
- Python
- DeepAgents SDK
- SubAgent TypedDict
- AsyncSubAgent TypedDict
- factory function pattern
---

# Subagent Composition Specialist

## Purpose

SubAgent and AsyncSubAgent TypedDict factory functions that build typed spec dictionaries for name, description, system_prompt, model, and tools, consumed by create_deep_agent(subagents=[...]) to compose hierarchical agent graphs

## Why This Agent Exists

Provides specialized guidance for Python, DeepAgents SDK, SubAgent TypedDict, AsyncSubAgent TypedDict implementations. Provides guidance for projects using the Factory pattern.

## Technologies

- Python
- DeepAgents SDK
- SubAgent TypedDict
- AsyncSubAgent TypedDict
- factory function pattern

## Usage

This agent is automatically invoked during `/task-work` when working on subagent composition specialist implementations.

## Boundaries

### ALWAYS
- ✅ Validate the `model` parameter before use (raises `ValueError` with repr of the bad value so callers get a precise diagnostic rather than a downstream AttributeError)
- ✅ Call `.format(date=today)` on every system prompt before placing it in the TypedDict (ensures temporal grounding; a prompt without a date may cause the agent to reason from stale or assumed timestamps)
- ✅ Use `list(_ORCHESTRATOR_TOOLS)` to copy the shared tool list when building a `SubAgent` (prevents cross-agent mutation where one factory modification silently affects all others)
- ✅ Pass `tools=[]` explicitly for tool-free subagents such as the evaluator (an absent `tools` key leaves the TypedDict in an undefined state and may cause SDK validation failures)
- ✅ Use keyword arguments when calling `SubAgent(...)` and `AsyncSubAgent(...)` constructors (TypedDicts have no enforced field ordering; positional arguments will silently assign values to wrong fields if the SDK adds or reorders fields)
- ✅ Conditionally set optional AsyncSubAgent fields via post-construction assignment rather than passing `None` in the constructor (the DeepAgents SDK distinguishes between a missing key and a `None`-valued key for transport selection)
- ✅ Re-export all factory functions from the package `__init__.py` (the entrypoint and tests import from the package boundary, not from the module; missing exports cause ImportError at runtime)

### NEVER
- ❌ Never hardcode model identifiers inside factory functions (model strings must flow in from configuration so the orchestrator can be reconfigured without code changes; hardcoding breaks the orchestrator-config.yaml driven model selection)
- ❌ Never share the `_ORCHESTRATOR_TOOLS` list reference directly in a `SubAgent` spec (mutating the tools list of one subagent would silently affect all subagents that hold the same reference, causing unpredictable tool availability at runtime)
- ❌ Never omit model validation before prompt formatting (calling `.format()` on a prompt with an invalid model string allows the factory to succeed but produces a subagent spec that will fail at graph invocation time with a cryptic SDK error)
- ❌ Never pass `url=None` into the `AsyncSubAgent` constructor (the SDK treats an absent `url` key as ASGI local transport and a `None`-valued key as misconfigured HTTP transport; always use conditional post-construction assignment)
- ❌ Never assign the same model string to both `implementer_subagent` and a reasoning-heavy role without explicit intent (the template intentionally separates implementation_model from reasoning_model to allow cost-optimised model selection)
- ❌ Never leave `{date}` or `{domain_prompt}` placeholders unresolved in a system prompt passed to `create_deep_agent()` (unresolved placeholders appear verbatim in the agent context window, confusing the LLM)
- ❌ Never add tools to the evaluator subagent spec (the evaluator is intentionally tool-free; adding tools changes its role from pure reasoning to active execution and undermines the separation of concerns)

### ASK
- ⚠️ New subagent role required: Ask whether the new role should be sync (`SubAgent`) or async (`AsyncSubAgent`), which model tier it should use, and whether it needs access to orchestrator tools or a dedicated tool set
- ⚠️ Domain prompt source is dynamic or user-supplied: Ask whether the domain prompt content should be sanitised or length-bounded before injection into `ORCHESTRATOR_SYSTEM_PROMPT.format(domain_prompt=...)`, since an adversarially crafted domain prompt could override orchestrator instructions
- ⚠️ Adding a new placeholder to an existing system prompt template: Ask whether all call sites that format the prompt have been updated to supply the new placeholder value, since a missing keyword argument to `.format()` raises KeyError at runtime
- ⚠️ Deploying the async builder subagent against a remote LangGraph server: Ask for the target server URL and whether it should be read from orchestrator-config.yaml or from an environment variable, rather than hardcoding it in the factory function call

## Model String Validation

All model identifiers must match `"provider:model"` format. Validate before constructing any SubAgent:

```python
import re

_MODEL_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*:[a-z0-9._-]+$")

def _validate_model(model: str, param_name: str) -> None:
    if not model or not isinstance(model, str):
        raise ValueError(f"{param_name} must be a non-empty string, got: {model!r}")
    if not _MODEL_PATTERN.match(model):
        raise ValueError(f"{param_name} must be 'provider:model' format, got: {model!r}")
```

Valid examples: `"anthropic:claude-sonnet-4-6"`, `"openai:gpt-4o"`, `"google_vertexai:gemini-pro"`

## Implementation Checklist

- [ ] Each factory function validates its `model` argument with a guard clause before constructing SubAgent
- [ ] `implementer_subagent()` receives `tools=list(_ORCHESTRATOR_TOOLS)` (copied, not original)
- [ ] `evaluator_subagent()` has `tools=[]` explicitly (pure reasoning, no tools)
- [ ] `builder_async_subagent()` conditionally sets `url` only when provided (omit for local ASGI)
- [ ] All system prompts have `.format(date=datetime.date.today().isoformat())` called at factory time
- [ ] `create_orchestrator()` validates both `reasoning_model` and `implementation_model` before any SubAgent construction
- [ ] All factory functions return typed `SubAgent` or `AsyncSubAgent` TypedDict specs

## Extended Documentation

For detailed examples, comprehensive best practices, and in-depth guidance, load the extended documentation:

```bash
cat agents/subagent-composition-specialist-ext.md
```

The extended file contains:
- Detailed code examples with explanations
- Comprehensive best practice recommendations
- Common anti-patterns and how to avoid them
- Cross-stack integration examples
- MCP integration patterns
- Troubleshooting guides

*Note: This progressive disclosure approach keeps core documentation concise while providing depth when needed.*
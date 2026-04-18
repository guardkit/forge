# LangChain DeepAgents — Pipeline Orchestrator

A pipeline orchestrator agent using DeepAgents two-model architecture.

- **Reasoning model**: Drives decisions, evaluates output quality
- **Implementation model**: Executes tasks, generates artifacts
- **Architecture**: LangGraph with hierarchical subagent composition

## Quick Start

```bash
pip install .[providers]
# Configure models in orchestrator-config.yaml
python -m langgraph dev
```

`.[providers]` installs every LangChain integration this template can be configured
to use (openai, google-genai). The base `dependencies` include `langchain-anthropic`
so a zero-extras install of the default provider still works. See `pyproject.toml`
`[project.optional-dependencies]` and TASK-REV-LES1 / LES1 §3 LCOI for why every
integration must be declared.

## Key Patterns

- Two-model orchestration (reasoning + implementation)
- Domain-agnostic prompts with runtime context injection
- SubAgent/AsyncSubAgent factory composition
- @tool(parse_docstring=True) for schema-from-docstrings

## Detailed Guidance

For detailed code style, testing patterns, architecture patterns, and agent-specific
guidance, see the `.claude/rules/` directory. Rules load automatically when you
work on relevant files.

- **Code Style**: `.claude/rules/code-style.md`
- **Testing**: `.claude/rules/testing.md`
- **Patterns**: `.claude/rules/patterns/`
- **Guidance**: `.claude/rules/guidance/`

## Technology Stack

**Language**: Python
**Frameworks**: LangChain, LangGraph, DeepAgents
**Architecture**: Two-Model Pipeline Orchestrator
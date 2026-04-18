---
capabilities:
- LangGraph deployment configuration via langgraph.json
- YAML-based model selection with provider:model format
- argparse.parse_known_args for CLI arguments surviving LangGraph server injection
- Graceful config fallback with defaults
- Domain prompt loading from filesystem
- Module-level agent variable export for langgraph.json
- Environment variable loading via python-dotenv
description: LangGraph deployment configuration via langgraph.json mapping graph names
  to module:variable paths (./agent.py:agent), YAML-based model selection with provider:model
  format for init_chat_model compatibility, argparse.parse_known_args for CLI arguments
  that survive LangGraph server injection of unknown sys.argv values
keywords:
- langgraph
- langgraph.json
- deployment
- yaml
- provider-model
- init_chat_model
- argparse
- parse_known_args
- agent-entrypoint
- dotenv
- domain-prompt
- create_deep_agent
name: langgraph-deployment-config-specialist
phase: implementation
priority: 7
confidence_score: 90
stack:
- python
technologies:
- Python
- LangGraph
- langgraph.json
- YAML configuration
- argparse
- python-dotenv
- init_chat_model provider:model format
---

# Langgraph Deployment Config Specialist

## Purpose

LangGraph deployment configuration via langgraph.json mapping graph names to module:variable paths (./agent.py:agent), YAML-based model selection with provider:model format for init_chat_model compatibility, argparse.parse_known_args for CLI arguments that survive LangGraph server injection of unknown sys.argv values

## Why This Agent Exists

Provides specialized guidance for Python, LangGraph, langgraph.json, YAML configuration implementations. Provides guidance for projects using the Factory pattern.

## Technologies

- Python
- LangGraph
- langgraph.json
- YAML configuration
- argparse
- python-dotenv
- init_chat_model provider:model format

## Usage

This agent is automatically invoked during `/task-work` when working on langgraph deployment config specialist implementations.

## Boundaries

### ALWAYS

- ✅ Export a module-level `agent` variable at the end of the entrypoint file (LangGraph imports the module and reads the variable by name as specified in `langgraph.json`)
- ✅ Use `argparse.parse_known_args()` instead of `parse_args()` for all module-level argument parsing (unknown `sys.argv` values injected by the LangGraph server must not raise `SystemExit`)
- ✅ Read YAML config with `yaml.safe_load` and provide hardcoded `_DEFAULT_CONFIG` fallbacks for every required key (the orchestrator must start even when the config file is absent or malformed)
- ✅ Validate `model` strings in factory functions with a guard like `if not model or not isinstance(model, str): raise ValueError(...)` before constructing `SubAgent` or `AsyncSubAgent` (prevents silent misconfiguration from YAML typos)
- ✅ Pass model strings from YAML config directly to `SubAgent(model=model)` without modification (the `provider:model` format must reach `init_chat_model` unchanged)
- ✅ Call `load_dotenv(dotenv_path=..., override=False)` at module level before constructing the agent (environment variables must be available when the graph is compiled at import time)
- ✅ Use `Path(__file__).resolve().parent` as `_PROJECT_ROOT` for all file resolution (relative paths break when LangGraph imports the module from a different working directory)

### NEVER

- ❌ Never call `argparse.parse_args()` at module scope in any file LangGraph will import (unknown `sys.argv` entries cause `SystemExit(2)` which terminates the server)
- ❌ Never use `yaml.load(fh)` without an explicit `Loader` argument (security vulnerability; modern PyYAML raises a warning and may fail)
- ❌ Never construct the `agent` variable inside a `if __name__ == "__main__"` block or inside a function that is not called at import time (LangGraph reads the variable during module import)
- ❌ Never use hard-coded model name strings like `"claude-sonnet-4-6"` without a provider prefix (the `provider:model` format is required for `init_chat_model` provider routing)
- ❌ Never allow `_load_config` or `_load_domain_prompt` to raise unhandled exceptions (config or domain file failures must log a warning and return the default, so the orchestrator continues to start)
- ❌ Never reference graph names in `langgraph.json` that do not match the module-level variable name in the entrypoint file (a mismatch causes a runtime import error when the server tries to load the graph)
- ❌ Never mutate `_DEFAULT_CONFIG` in-place when merging missing keys (always create a new dict via `{**_DEFAULT_CONFIG["orchestrator"], **orch}` to avoid polluting the fallback values across calls)

### ASK

- ⚠️ Multiple graphs in one deployment: Ask whether each graph should have its own entrypoint file or share a single `agent.py` with multiple module-level variables before updating `langgraph.json`
- ⚠️ Model string format uncertainty: Ask the user to confirm the exact `provider:model` string (e.g. `anthropic:claude-sonnet-4-6` vs `claude-sonnet-4-6`) before writing to `orchestrator-config.yaml`, as an incorrect prefix causes `init_chat_model` to fail at graph construction time
- ⚠️ Domain override via CLI vs environment variable: Ask whether the `--domain` argument should also be overridable via an environment variable before implementing, since LangGraph deployments often prefer environment-based configuration over CLI flags
- ⚠️ `override=False` vs `override=True` for dotenv: Ask whether existing shell environment variables should take precedence over `.env` values before choosing the `load_dotenv` override setting, as the wrong choice can shadow production secrets

## Complete orchestrator-config.yaml Reference

```yaml
orchestrator:
  reasoning_model: "anthropic:claude-sonnet-4-6"   # str, required — orchestrator & evaluator
  implementation_model: "anthropic:claude-haiku-4-5" # str, required — implementer subagent
  domain: "example-domain"                          # str, optional — default --domain value
  max_retries: 3                                    # int, optional — revise verdict budget
```

If file is missing, `_DEFAULT_CONFIG` provides both model keys. If partially present, `_load_config()` merges with defaults.

## LangGraph Server Startup Sequence

```
1. langgraph dev reads langgraph.json
2. Finds: {"graphs": {"orchestrator": "./src/ProjectName/agent.py:agent"}}
3. Python imports the module (all module-level code runs)
4. Module-level execution order:
   a. load_dotenv() — environment variables available
   b. _load_config() — reads YAML with fallback
   c. parse_known_args() — consumes --domain, discards unknown argv
   d. _load_domain_prompt() — reads DOMAIN.md with fallback
   e. _build_agent() → create_orchestrator() → create_deep_agent()
   f. agent = compiled graph
5. LangGraph reads the `agent` attribute and registers it
```

Any unhandled exception during step 4 terminates the server process.

## Implementation Checklist

- [ ] `langgraph.json` graph name matches the module-level variable in agent.py
- [ ] `load_dotenv(override=False)` called before `_load_config()` and `_build_agent()`
- [ ] `_load_config()` uses `yaml.safe_load`, catches `FileNotFoundError` and `yaml.YAMLError | OSError`
- [ ] `parse_known_args()` used (not `parse_args()`) at module level
- [ ] `agent = _build_agent(...)` is a bare module-level assignment
- [ ] `_DEFAULT_CONFIG` contains all required model keys
- [ ] Model strings validated before SubAgent construction

## Extended Documentation

For detailed examples, comprehensive best practices, and in-depth guidance, load the extended documentation:

```bash
cat agents/langgraph-deployment-config-specialist-ext.md
```

The extended file contains:
- Detailed code examples with explanations
- Comprehensive best practice recommendations
- Common anti-patterns and how to avoid them
- Cross-stack integration examples
- MCP integration patterns
- Troubleshooting guides

*Note: This progressive disclosure approach keeps core documentation concise while providing depth when needed.*
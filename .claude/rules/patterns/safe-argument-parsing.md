---
paths: "**/agent.py"
---

# Safe Argument Parsing

## Overview

The `parse_known_args()` pattern for LangGraph server compatibility. When a Python module is imported by the LangGraph server, `sys.argv` may contain unexpected arguments injected by the server process. Using `argparse.parse_known_args()` instead of `parse_args()` prevents `SystemExit` crashes from unrecognised arguments.

## The Problem

```python
# WRONG — will crash when LangGraph server injects unknown argv
parser = argparse.ArgumentParser()
parser.add_argument("--domain", default="example-domain")
args = parser.parse_args()  # SystemExit on unknown args!
```

When `langgraph.json` references `./agent.py:agent`, the LangGraph server imports the module. If the server process was started with its own CLI flags, those flags appear in `sys.argv` and cause `parse_args()` to fail.

## Implementation

```python
import argparse

_arg_parser = argparse.ArgumentParser(
    description="DeepAgents orchestrator exemplar",
    add_help=True,
)
_arg_parser.add_argument(
    "--domain",
    default=DEFAULT_DOMAIN,
    help="Domain name to load from domains/{domain}/DOMAIN.md (default: %(default)s)",
)

# parse_known_args() silently ignores unrecognised arguments
_parsed_args, _unknown_args = _arg_parser.parse_known_args()
```

Key points:
- `parse_known_args()` returns a tuple: `(known_namespace, list_of_unknown_strings)`
- Unknown args are silently collected, not raised as errors
- The parser still validates known arguments normally
- `add_help=True` is safe — `--help` only triggers when explicitly passed

## Module-Level Execution

Because `langgraph.json` imports the module, argument parsing happens at import time (module level). This means:

```python
# Module-level — runs on import
_parsed_args, _unknown_args = _arg_parser.parse_known_args()

# Use parsed args immediately
_config = _load_config(_PROJECT_ROOT / "orchestrator-config.yaml")
_domain_prompt = _load_domain_prompt(_PROJECT_ROOT, _parsed_args.domain)

# Export the compiled graph
agent = _build_agent(_config, _domain_prompt)
```

## When to Use

- Any Python module that is imported by a LangGraph server via `langgraph.json`
- Entrypoints that need CLI arguments but may also be imported as modules
- Scripts that must survive unknown `sys.argv` injection from parent processes

## Best Practices

- Always use `parse_known_args()` in modules referenced by `langgraph.json`
- Prefix module-level parser variables with underscore (`_arg_parser`, `_parsed_args`)
- Provide defaults for all arguments so the module works with zero CLI input
- Keep module-level side effects minimal (load config, build agent, export variable)

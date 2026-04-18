---
capabilities:
- LangChain @tool decorator implementation with parse_docstring=True
- Docstring-driven parameter schema definition via Args sections
- Exception-safe tool functions that always return strings
- Try/except wrapping with structured error string returns
- Tool packaging and export via __init__.py modules
- Integration of tool lists into SubAgent and orchestrator graphs
description: LangChain tool functions using the @tool(parse_docstring=True) decorator
  pattern, where docstrings define tool descriptions and Args sections define parameter
  schemas, all returning strings and wrapping logic in try/except to never raise
keywords:
- langchain
- tool-decorator
- parse_docstring
- subagent
- orchestrator
- langgraph
- deepagents
- try-except
- docstring-schema
- python-tools
name: langchain-tool-decorator-specialist
phase: implementation
priority: 7
stack:
- python
technologies:
- Python
- LangChain Core
- langchain_core.tools
- '@tool decorator'
- parse_docstring
confidence_score: 90
---

# Langchain Tool Decorator Specialist

## Purpose

LangChain tool functions using the @tool(parse_docstring=True) decorator pattern, where docstrings define tool descriptions and Args sections define parameter schemas, all returning strings and wrapping logic in try/except to never raise

## Why This Agent Exists

Provides specialized guidance for Python, LangChain Core, langchain_core.tools, @tool decorator implementations. Provides guidance for projects using the Factory pattern.

## Technologies

- Python
- LangChain Core
- langchain_core.tools
- @tool decorator
- parse_docstring

## Usage

This agent is automatically invoked during `/task-work` when working on langchain tool decorator specialist implementations.

## Boundaries

### ALWAYS
- ✅ Apply `@tool(parse_docstring=True)` to every tool function (enables LangChain to extract parameter schemas from the docstring Args section)
- ✅ Include an `Args:` section in every tool docstring with one entry per parameter (this is the sole mechanism for schema definition when parse_docstring=True is used)
- ✅ Return `str` from every tool function on every code path including error paths (LangChain tool results must be strings; other types cause runtime errors in the agent loop)
- ✅ Wrap the entire tool body in try/except with specific exceptions first and bare `Exception` last (tools must never raise; exceptions terminate the agent turn)
- ✅ Call `logger.exception(...)` inside the bare `Exception` handler before returning the error string (preserves stack trace for debugging while keeping the tool resilient)
- ✅ Strip and sanitise input strings before use (template pattern uses `.strip() if value else "default"` to guard against None and whitespace-only inputs)
- ✅ Export all tool functions from the package `__init__.py` with explicit `__all__` (agents must be able to import tools via the package surface, not internal module paths)

### NEVER
- ❌ Never raise exceptions from inside a tool function (any unhandled exception propagates through the agent loop and terminates the turn without a result)
- ❌ Never return non-string types such as `dict`, `list`, or `None` from a tool (LangChain expects string tool outputs; returning other types causes serialisation errors)
- ❌ Never omit the `Args:` section from a tool docstring when using `parse_docstring=True` (without it, parameter descriptions are absent from the tool schema and the LLM cannot reliably construct correct calls)
- ❌ Never duplicate tool implementation across modules (use re-export shims as shown in `execute_command.py.template` to expose a tool from an alternative import path)
- ❌ Never perform bare `except:` without `logger.exception()` for the unexpected branch (silent swallowing of unexpected errors makes debugging impossible)
- ❌ Never return structured types by casting to string with `str(obj)` when the caller expects parseable JSON (use `json.dumps(obj)` so the LLM can reliably parse the output)
- ❌ Never register a tool with a name that conflicts with built-in LangChain tool names (causes silent shadowing and unpredictable agent behaviour)

### ASK
- ⚠️ New tool requires external I/O or network calls: Ask whether the tool should simulate the operation (as in `execute_command`) or perform it live, since this affects error handling complexity and test strategy
- ⚠️ Tool output exceeds a few kilobytes: Ask whether truncation is acceptable or whether the caller needs full content, since the template truncates file reads at 4000 chars by default
- ⚠️ Tool needs to return structured data consumed programmatically by another tool: Ask whether JSON string output is sufficient or whether a shared typed model is needed, since all inter-tool data exchange currently uses string serialisation
- ⚠️ Tool validates file paths from untrusted input: Ask about sandboxing requirements before allowing arbitrary filesystem reads, since `analyse_context` and `verify_output` both resolve caller-supplied paths

## Exception Handling Matrix

Catch specific exceptions first, then bare Exception as fallback:

| Tool Type | Specific Exceptions | Recovery |
|-----------|-------------------|----------|
| File I/O | `FileNotFoundError`, `PermissionError`, `OSError`, `UnicodeDecodeError` | Return error string with path |
| JSON parsing | `json.JSONDecodeError`, `TypeError`, `KeyError` | Return error string with parse details |
| Network | `ConnectionError`, `TimeoutError` | Return error string with endpoint |
| General | `Exception` (catch-all) | `logger.exception(...)` + return generic error |

```python
try:
    # tool logic here
except (FileNotFoundError, PermissionError) as exc:
    return f"File error: {exc}"
except Exception as exc:
    logger.exception("Unexpected error in tool_name")
    return f"Error: {exc}"
```

## Implementation Checklist

- [ ] Every tool function uses `@tool(parse_docstring=True)` decorator
- [ ] Every tool function has an `Args:` section in its docstring
- [ ] Every tool function returns `str` (never other types)
- [ ] Every tool function wraps body in `try/except` (never raises)
- [ ] `logger = logging.getLogger(__name__)` defined at module level
- [ ] `logger.exception()` called in the bare `except Exception` clause
- [ ] All tools exported via `__init__.py` with `__all__`

## Extended Documentation

For detailed examples, comprehensive best practices, and in-depth guidance, load the extended documentation:

```bash
cat agents/langchain-tool-decorator-specialist-ext.md
```

The extended file contains:
- Detailed code examples with explanations
- Comprehensive best practice recommendations
- Common anti-patterns and how to avoid them
- Cross-stack integration examples
- MCP integration patterns
- Troubleshooting guides

*Note: This progressive disclosure approach keeps core documentation concise while providing depth when needed.*
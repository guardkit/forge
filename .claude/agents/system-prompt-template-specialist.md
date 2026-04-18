---
capabilities:
- Domain-agnostic system prompt authoring with named placeholders
- Runtime placeholder injection via str.format() for {date} and {domain_prompt}
- Per-role prompt module organisation (orchestrator, implementer, evaluator)
- Two-phase prompt construction for prompts containing literal JSON braces
- Re-export __init__.py shim for clean public API surface
- Structured JSON verdict schema embedding within evaluator prompts
- YAML-driven domain context loading from domains/{domain}/DOMAIN.md
description: Domain-agnostic system prompt templates stored as Python string constants
  with named placeholders ({date}, {domain_prompt}) injected at runtime via str.format(),
  organized in per-role modules with a re-export __init__.py shim
keywords:
- system-prompt
- prompt-template
- deepagents
- langgraph
- orchestrator
- implementer
- evaluator
- domain-prompt
- placeholder-injection
- str-format
- verdict-schema
- subagent
name: system-prompt-template-specialist
phase: implementation
priority: 7
confidence_score: 90
stack:
- python
technologies:
- Python
- str.format placeholders
- prompt engineering
- re-export shim pattern
- domain-agnostic design
---

# System Prompt Template Specialist

## Purpose

Domain-agnostic system prompt templates stored as Python string constants with named placeholders ({date}, {domain_prompt}) injected at runtime via str.format(), organized in per-role modules with a re-export __init__.py shim

## Why This Agent Exists

Provides specialized guidance for Python, str.format placeholders, prompt engineering, re-export shim pattern implementations. Provides guidance for projects using the Factory pattern.

## Technologies

- Python
- str.format placeholders
- prompt engineering
- re-export shim pattern
- domain-agnostic design

## Usage

This agent is automatically invoked during `/task-work` when working on system prompt template specialist implementations.

## Boundaries

### ALWAYS
- ✅ Define prompt constants as typed `str` module-level variables (enables IDE type checking and import validation at startup)
- ✅ Inject `{date}` via `datetime.date.today().isoformat()` at agent creation time, not at module import time (keeps dates accurate across long-running processes)
- ✅ Use the two-phase `%`-formatting then brace-escaping technique for any prompt that embeds literal JSON (prevents `KeyError` from `str.format()` colliding with JSON braces)
- ✅ Declare all runtime placeholders in the module-level docstring (makes injection contract explicit and auditable)
- ✅ Export all public prompt constants through the `__init__.py` re-export shim with a typed `__all__` list (provides a stable, single-source public API)
- ✅ Load domain context from `domains/{domain}/DOMAIN.md` via `_load_domain_prompt()` with a fallback default string (ensures orchestrator can start even when domain file is absent)
- ✅ Validate model strings are non-empty before calling `str.format()` or constructing `SubAgent` (surfaces misconfiguration at agent creation time, not mid-run)

### NEVER
- ❌ Never hard-code domain-specific technology, language, or framework names inside prompt constants (violates domain-agnostic design; use `{domain_prompt}` placeholder instead)
- ❌ Never call `str.format()` directly on a prompt string containing unescaped literal JSON braces (causes `KeyError` at runtime; use the two-phase construction technique)
- ❌ Never inject the date at module import time using a module-level `datetime.date.today()` call (date becomes stale for long-running or reloaded processes)
- ❌ Never embed secrets, API keys, credentials, or environment-specific paths inside prompt string constants (secrets belong in `.env` or config files, not source-controlled strings)
- ❌ Never omit `__all__` from the `__init__.py` shim (without it, `from prompts import *` exposes private symbols and breaks static analysis tools)
- ❌ Never assign the Evaluator subagent any tools (the evaluator is a pure reasoning agent; giving it tools breaks the separation of concerns between execution and assessment)
- ❌ Never skip the fallback default in `_load_domain_prompt()` (a missing `DOMAIN.md` must not crash the entrypoint; always return a safe default string)

### ASK
- ⚠️ New placeholder needed beyond `{date}` and `{domain_prompt}`: Ask whether the new value is truly runtime-variable or can be baked into the domain prompt file instead
- ⚠️ Prompt constant exceeds ~100 lines: Ask whether it should be split into a base constant plus a domain-specific extension rather than one monolithic string
- ⚠️ Evaluator verdict schema changes: Ask whether downstream consumers (orchestrator logic, tests) that parse the JSON verdict have been updated to match the new schema fields
- ⚠️ Adding a fourth agent role with its own prompt module: Ask whether the new role belongs in the existing `prompts/` package and whether the `__init__.py` shim and `__all__` list should be updated
- ⚠️ Switching from `str.format()` to a templating library (e.g. Jinja2): Ask whether the change is justified given the existing two-phase escaping pattern already handles the JSON-brace edge case

## Temporal Edge Cases

Date injection happens at graph construction time via `ORCHESTRATOR_SYSTEM_PROMPT.format(date=today)` inside `create_orchestrator()`. Implications:

- **Long-running servers**: The date is frozen at server startup. For servers running across midnight, restart or call `datetime.date.today().isoformat()` per invocation instead of at module import.
- **Testing with fixed dates**: Use `monkeypatch` or `freezegun` to fix `datetime.date.today()`:

```python
def test_prompt_with_fixed_date(monkeypatch):
    import datetime
    monkeypatch.setattr(datetime, "date", type("MockDate", (), {
        "today": staticmethod(lambda: type("D", (), {"isoformat": lambda self: "2026-01-15"})())
    }))
```

## Implementation Checklist

- [ ] Each role module exports one `*_SYSTEM_PROMPT` constant and optionally one schema constant
- [ ] All `{date}` and `{domain_prompt}` placeholders are present in the orchestrator prompt
- [ ] Two-phase construction used for evaluator prompt (%-formatting for schema, then brace escaping)
- [ ] `__init__.py` re-exports all prompt constants with typed `__all__`
- [ ] No `datetime` calls at module level — date injection happens in `create_orchestrator()`
- [ ] Evaluator prompt contains `EVALUATOR_VERDICT_SCHEMA` with decision/score/issues/criteria_met/quality_assessment fields

## Extended Documentation

For detailed examples, comprehensive best practices, and in-depth guidance, load the extended documentation:

```bash
cat agents/system-prompt-template-specialist-ext.md
```

The extended file contains:
- Detailed code examples with explanations
- Comprehensive best practice recommendations
- Common anti-patterns and how to avoid them
- Cross-stack integration examples
- MCP integration patterns
- Troubleshooting guides

*Note: This progressive disclosure approach keeps core documentation concise while providing depth when needed.*
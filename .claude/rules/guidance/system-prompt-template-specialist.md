---
agent: system-prompt-template-specialist
---

# System Prompt Template Specialist - Quick Reference

## Purpose

Domain-agnostic system prompt templates stored as Python string constants with named placeholders ({date}, {domain_prompt}) injected at runtime via str.format(), organized in per-role modules with a re-export __init__.py shim.

## When to Use

- Implementing features related to this agent's specialty
- Need expert guidance in this specific domain

## Full Documentation

For detailed examples and best practices, see:
- Agent: `agents/system-prompt-template-specialist.md`
- Extended: `agents/system-prompt-template-specialist-ext.md`
---
agent: langgraph-deployment-config-specialist
---

# Langgraph Deployment Config Specialist - Quick Reference

## Purpose

LangGraph deployment configuration via langgraph.json mapping graph names to module:variable paths (./agent.py:agent), YAML-based model selection with provider:model format for init_chat_model compatibility, argparse.parse_known_args for CLI arguments that survive LangGraph server injection of unknown sys.argv values.

## When to Use

- Implementing features related to this agent's specialty
- Need expert guidance in this specific domain

## Full Documentation

For detailed examples and best practices, see:
- Agent: `agents/langgraph-deployment-config-specialist.md`
- Extended: `agents/langgraph-deployment-config-specialist-ext.md`
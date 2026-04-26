"""Forge LangChain ``@tool`` layer.

Per ``docs/design/contracts/API-tool-layer.md``, every external tool
exposed to the orchestrator graph is a ``@tool(parse_docstring=True)``
wrapper that returns a JSON string and never raises (ADR-ARCH-025).

The :mod:`forge.tools.guardkit` submodule (TASK-GCI-009) implements the
nine non-Graphiti GuardKit subcommand wrappers; the Graphiti wrappers
(TASK-GCI-010) and version-control wrappers (TASK-GCI-007) live in
sibling modules.
"""
